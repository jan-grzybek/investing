"""Shared retry primitive for the build's outbound network calls.

The page build depends on two third-party services, and both fail
transiently in production: Yahoo Finance (rate limits, 5xx, empty
frames) and the Google Sheets API (``APIError``, ``ConnectionError``
on the initial ``open_by_key`` handshake). Retry policy used to live
inside :mod:`investing.market_data`, bound to yfinance and to
:class:`~investing.market_data.MarketDataError`; the Sheets side had
no retry at all, which is what actually broke scheduled deploys.

:func:`call_with_retry` is that policy with the vendor-specific parts
lifted out: callers supply the failure description and the exception
type to raise on exhaustion, so each integration keeps reporting a
recognisable error category to the leak-safe wrapper in
:mod:`investing.safe_run`.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable

from .log import logger

# Default policy: three attempts with exponential back-off.
_DEFAULT_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_S = 0.5

# Exceptions that signal "the integration's shape is wrong", not "the
# service is briefly unhappy". Retrying these wastes the budget and,
# worse, launders a vendor API break into whatever error type the
# caller reports on exhaustion -- a yfinance release that renames a
# method would surface as a market-data outage rather than as the
# breaking change it is. ``KeyError`` is deliberately absent: a
# partially-parsed vendor payload raises it from deep inside the
# parser and does clear on a retry.
_NON_RETRYABLE = (AttributeError, TypeError)

_DISABLE_RETRY_ENV = "INVESTING_DISABLE_RETRY"


def _retry_disabled() -> bool:
    """Return True when the test suite has opted retries out.

    Tests that intentionally exercise the failure path (e.g. a
    ``side_effect`` that always raises) would otherwise eat the full
    back-off budget on every assertion. Setting
    ``INVESTING_DISABLE_RETRY=1`` in those test bodies turns the
    helper into a thin pass-through that raises on the first failure.
    """
    return os.environ.get(_DISABLE_RETRY_ENV) == "1"


def _backoff_delay(attempt: int, base_delay: float) -> float:
    """Exponential back-off for ``attempt`` (0-indexed), with jitter.

    ``get_holdings`` fans every ticker out across eight worker threads,
    so a vendor-side rate limit trips all eight at the same instant. A
    bare ``base_delay * 2**attempt`` would then march them through the
    identical sleep schedule and re-collide on every retry -- the
    classic thundering herd, which reads to the vendor as a burst and
    makes the rate limit *more* likely to persist.

    Equal jitter (half the nominal delay, plus a uniform draw over the
    other half) decorrelates the workers while still guaranteeing the
    back-off grows. A caller that passes ``base_delay=0`` (the test
    suite, to keep the budget free) gets exactly zero, so the jitter
    never reintroduces sleeping into a test that opted out of it.
    """
    nominal = base_delay * (2**attempt)
    half = nominal / 2.0
    return half + random.uniform(0.0, half)


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    description: str,
    error_type: type[Exception],
    attempts: int = _DEFAULT_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY_S,
) -> T:
    """Retry ``fn()`` with jittered exponential back-off.

    ``description`` is what we log on every retry attempt; it should
    identify the call site (e.g. ``"yfinance get_info"``) but must NOT
    carry any nominal value or identifier that the public-repo job log
    is supposed to keep private. The leak-safe wrapper redirects logger
    output to ``/dev/null`` in CI so the log is only visible to a
    developer running locally; treat the string as a public-prose
    identifier regardless.

    ``error_type`` is raised when the budget is exhausted, chained off
    the last underlying exception. ``__cause__`` preserves the original
    frame so the sanitized traceback in :mod:`investing.safe_run` still
    walks to the offending vendor call.

    Exceptions in :data:`_NON_RETRYABLE` propagate immediately, unwrapped
    and unretried.
    """
    effective_attempts = 1 if _retry_disabled() else attempts
    last_exc: BaseException | None = None
    for attempt in range(effective_attempts):
        try:
            return fn()
        except _NON_RETRYABLE:
            # An integration-shape break. Let it out with its own type
            # and frame intact rather than spending the budget on it.
            raise
        except Exception as exc:
            last_exc = exc
            remaining = effective_attempts - attempt - 1
            if remaining > 0:
                delay = _backoff_delay(attempt, base_delay)
                # Identifier-only log line: ``description`` is a static
                # call-site label, not a runtime value.
                logger.warning(
                    "%s failed (attempt %d/%d); retrying in %.2fs",
                    description,
                    attempt + 1,
                    effective_attempts,
                    delay,
                )
                time.sleep(delay)
    raise error_type(
        f"{description} failed after {effective_attempts} attempt(s)"
    ) from last_exc
