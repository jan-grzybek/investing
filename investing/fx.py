"""FX rate lookups via yfinance, cached per ``ExchangeRate``
instance. Threaded through the API as the ``fx`` parameter
rather than reached for as a module-level singleton.
"""
from __future__ import annotations

import bisect
import math
from datetime import datetime

import yfinance as yf

from .formatting import _ts_to_datetime

# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------


class ExchangeRate:
    def __init__(self):
        self._rates: dict[str, float] = {}
        self._history: dict[str, tuple[list, list]] = {}

    def _current(self, currency):
        if currency == "USD":
            return 1.0
        if currency in self._rates:
            return self._rates[currency]
        rate = yf.Ticker(f"{currency}USD=X").info["regularMarketPrice"]
        if currency == "GBp":
            rate /= 100
        self._rates[currency] = rate
        return rate

    def _historical(self, currency, date):
        if currency == "USD":
            return 1.0
        if currency not in self._history:
            hist = yf.Ticker(f"{currency}USD=X").history(
                period="max", interval="1d", auto_adjust=False)
            dates, rates = [], []
            for ts, close in hist["Close"].items():
                if math.isnan(close):
                    continue
                dates.append(_ts_to_datetime(ts).date())
                rates.append(float(close))
            self._history[currency] = (dates, rates)
        dates, rates = self._history[currency]
        if not dates:
            return self._current(currency)
        target = date.date() if isinstance(date, datetime) else date
        idx = max(bisect.bisect_right(dates, target) - 1, 0)
        rate = rates[idx]
        if currency == "GBp":
            rate /= 100
        return rate

    def __call__(self, currency, date=None):
        if date is None:
            return self._current(currency)
        return self._historical(currency, date)




# Type alias for "anything callable as ``fx(currency, date=None) -> float``".
# Threading the fx callable through the API rather than reaching for a
# module-level singleton keeps construction explicit (production wires
# one shared instance in ``main``; tests inject their own stub) and
# lets parallel test workers operate on independent caches without an
# autouse reset fixture.
FxRate = "ExchangeRate"




def _fx_or_default(fx):
    """Return ``fx`` if provided, otherwise a fresh ``ExchangeRate``.

    Each caller that doesn't supply an ``fx`` gets its own private
    cache -- which is the safe default for ad-hoc usage (tests,
    one-off scripts) and benign for production because ``main``
    always passes an explicit instance, so the fallback path is
    effectively unreachable on the hot path.
    """
    return fx if fx is not None else ExchangeRate()
