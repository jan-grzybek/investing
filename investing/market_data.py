"""Resilience layer around the yfinance integration.

yfinance reads talk to Yahoo Finance's HTTP endpoints; the upstream
API periodically returns 429 / 5xx / empty frames during peak load
or vendor-side maintenance. A failed read used to crash the build
with no retry, even though most of these conditions clear within a
second or two.

This module exposes a small :func:`_call_with_retry` helper plus a
:class:`MarketDataError` exception type. Callers that need resilience
wrap their yfinance call in ``_call_with_retry(...)``; on exhaustion
the helper raises :class:`MarketDataError` so the leak-safe wrapper
in :mod:`investing.safe_run` can render a recognisable failure
category rather than an opaque library exception.

The helper is deliberately untyped to the exception classes
yfinance raises -- the vendor surface area covers ``requests``
exceptions, custom ``yfinance.exceptions.YF*`` types, ``KeyError``
deep inside the parser, etc., and the catch-all keeps that
churn contained here rather than in every call site.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

from .log import logger


class MarketDataError(RuntimeError):
    """A yfinance read failed even after the configured retry budget.

    Carries no message-side payload beyond a hand-written description
    so the leak-safe wrapper's "drop ``str(exc)``" policy still
    surfaces a useful class name in the sanitized traceback.
    """


# Default policy: three attempts with exponential back-off
# (0.5s + 1.0s = 1.5s of sleep worst case). yfinance rate-limit
# windows usually clear in well under that, and any failure that
# survives three round-trips is unlikely to clear on a fourth.
_DEFAULT_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_S = 0.5


def _retry_disabled() -> bool:
    """Return True when the test suite has opted retries out.

    Tests that intentionally exercise the failure path (e.g. a
    ``side_effect`` that always raises) would otherwise eat the
    full back-off budget on every assertion. Setting
    ``INVESTING_DISABLE_RETRY=1`` in those test bodies turns the
    helper into a thin pass-through that raises on the first
    failure.
    """
    return os.environ.get("INVESTING_DISABLE_RETRY") == "1"


def _call_with_retry[T](
    fn: Callable[[], T],
    *,
    description: str,
    attempts: int = _DEFAULT_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY_S,
) -> T:
    """Retry ``fn()`` with exponential back-off on any exception.

    ``description`` is what we log on every retry attempt; it
    should identify the call site (e.g. ``"yfinance get_info"``)
    but must NOT carry any nominal value or identifier that the
    public-repo job log is supposed to keep private. The leak-safe
    wrapper redirects logger output to ``/dev/null`` in CI so the
    log is only visible to a developer running locally; treat the
    string as a public-prose identifier regardless.

    Raises :class:`MarketDataError` chained off the last underlying
    exception when the budget is exhausted. ``__cause__`` preserves
    the original frame so the sanitized traceback in
    :mod:`investing.safe_run` still walks to the offending yfinance
    call.
    """
    effective_attempts = 1 if _retry_disabled() else attempts
    last_exc: BaseException | None = None
    for attempt in range(effective_attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            remaining = effective_attempts - attempt - 1
            if remaining > 0:
                delay = base_delay * (2 ** attempt)
                # Identifier-only log line: ``description`` is a
                # static call-site label, not a runtime value.
                logger.warning(
                    "%s failed (attempt %d/%d); retrying in %.2fs",
                    description,
                    attempt + 1,
                    effective_attempts,
                    delay,
                )
                time.sleep(delay)
    raise MarketDataError(
        f"{description} failed after {effective_attempts} attempt(s)"
    ) from last_exc
