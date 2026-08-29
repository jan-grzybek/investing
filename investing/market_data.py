"""Resilience layer around the yfinance integration.

yfinance reads talk to Yahoo Finance's HTTP endpoints; the upstream
API periodically returns 429 / 5xx / empty frames during peak load
or vendor-side maintenance. A failed read used to crash the build
with no retry, even though most of these conditions clear within a
second or two.

This module binds the shared policy in :mod:`investing.retry` to the
yfinance side: :func:`_call_with_retry` is
:func:`investing.retry.call_with_retry` with
:class:`MarketDataError` as its exhaustion type, so callers keep a
recognisable failure category in the sanitized traceback that
:mod:`investing.safe_run` renders.

The helper is deliberately untyped to the exception classes
yfinance raises -- the vendor surface area covers ``requests``
exceptions, custom ``yfinance.exceptions.YF*`` types, ``KeyError``
deep inside the parser, etc., and the catch-all keeps that
churn contained here rather than in every call site. The one
exception is an integration-shape break (``AttributeError`` /
``TypeError``), which :mod:`investing.retry` refuses to retry so a
yfinance API change can't masquerade as a transient outage.
"""

from __future__ import annotations

from collections.abc import Callable

from .retry import _DEFAULT_ATTEMPTS, _DEFAULT_BASE_DELAY_S, call_with_retry


class MarketDataError(RuntimeError):
    """A yfinance read failed even after the configured retry budget.

    Carries no message-side payload beyond a hand-written description
    so the leak-safe wrapper's "drop ``str(exc)``" policy still
    surfaces a useful class name in the sanitized traceback.
    """


def _call_with_retry[T](
    fn: Callable[[], T],
    *,
    description: str,
    attempts: int = _DEFAULT_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY_S,
) -> T:
    """Retry a yfinance read, raising :class:`MarketDataError` on exhaustion.

    Thin binding over :func:`investing.retry.call_with_retry`; see that
    function for the back-off, jitter and non-retryable-exception
    policy shared with the Google Sheets integration.
    """
    return call_with_retry(
        fn,
        description=description,
        error_type=MarketDataError,
        attempts=attempts,
        base_delay=base_delay,
    )
