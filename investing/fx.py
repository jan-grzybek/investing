"""FX rate lookups via yfinance, cached per ``ExchangeRate``
instance. Threaded through the API as the ``fx`` parameter
rather than reached for as a module-level singleton.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import yfinance as yf

from .formatting import _ts_to_datetime

# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------


class ExchangeRate:
    def __init__(self):
        self._rates: dict[str, float] = {}
        # Per-currency historical cache: parallel numpy arrays of
        # ``datetime64[D]`` keys and ``float64`` USD rates, sorted
        # ascending. ``np.searchsorted`` then resolves a date to an
        # array index in O(log n) without the per-row Python loop the
        # legacy ``(list[date], list[float])`` pair forced. The
        # ``"GBp"`` minor-unit scaling is applied **after** the
        # array lookup so the cached values stay denominated in the
        # currency yfinance returned (matches the legacy contract
        # the test suite asserts on).
        self._history: dict[str, tuple[np.ndarray, np.ndarray]] = {}

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
            # The production object is a pandas Series; the test
            # fixtures hand us a plain dict that mirrors ``.items()``
            # closely enough for the legacy implementation. Walk via
            # ``.items()`` once to stay compatible with both, then
            # commit the result to numpy arrays so subsequent lookups
            # bypass any Python-level iteration.
            dates: list = []
            rates: list = []
            for ts, close in hist["Close"].items():
                if math.isnan(close):
                    continue
                dates.append(_ts_to_datetime(ts).date())
                rates.append(float(close))
            if dates:
                date_arr = np.array(dates, dtype="datetime64[D]")
                rate_arr = np.array(rates, dtype=float)
            else:
                date_arr = np.empty(0, dtype="datetime64[D]")
                rate_arr = np.empty(0, dtype=float)
            self._history[currency] = (date_arr, rate_arr)
        date_arr, rate_arr = self._history[currency]
        if date_arr.size == 0:
            return self._current(currency)
        target_date = date.date() if isinstance(date, datetime) else date
        target = np.datetime64(target_date, "D")
        idx = int(np.searchsorted(date_arr, target, side="right")) - 1
        if idx < 0:
            idx = 0
        rate = float(rate_arr[idx])
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
