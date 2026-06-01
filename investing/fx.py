"""FX rate lookups via yfinance, cached per ``ExchangeRate``
instance. Threaded through the API as the ``fx`` parameter
rather than reached for as a module-level singleton.
"""
from __future__ import annotations

import math
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import yfinance as yf

from .formatting import _ts_to_datetime
from .log import logger
from .market_data import _call_with_retry

# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------


_FX_CACHE_DIR_ENV = "INVESTING_FX_CACHE_DIR"


def _fx_cache_dir() -> Path | None:
    """Return the directory backing the optional on-disk FX cache.

    Set ``INVESTING_FX_CACHE_DIR`` to opt in (e.g. ``~/.cache/investing``).
    When unset the cache is a no-op so production behaviour is
    unchanged unless an operator explicitly wires up persistence --
    most useful for local development where ``scripts/preview.py`` is run
    repeatedly against the same FX pairs. CI runs can opt in by
    pointing the env var at a directory persisted across job runs
    via ``actions/cache``.
    """
    raw = os.environ.get(_FX_CACHE_DIR_ENV)
    if not raw:
        return None
    return Path(raw).expanduser()


def _load_history_from_disk(
    cache_dir: Path, currency: str,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return the cached ``(dates, rates)`` arrays for ``currency``.

    Returns ``None`` when no cache file exists or it can't be loaded
    cleanly (corrupt file, permission denied, ...). Cache misses are
    silent: the helper exists to accelerate the happy path, not to
    surface filesystem errors at the call site.
    """
    path = cache_dir / f"fx-{currency}.npz"
    if not path.is_file():
        return None
    try:
        data = np.load(path)
        return data["dates"], data["rates"]
    except (OSError, ValueError, KeyError):
        # Corrupt cache file -- swallow and re-fetch from yfinance.
        return None


def _save_history_to_disk(
    cache_dir: Path, currency: str,
    dates: np.ndarray, rates: np.ndarray,
) -> None:
    """Persist ``(dates, rates)`` to ``cache_dir``; swallow filesystem errors."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.savez(
            cache_dir / f"fx-{currency}.npz", dates=dates, rates=rates,
        )
    except OSError as exc:
        # Best-effort: the build keeps the data in-memory regardless,
        # so a write failure (read-only FS, quota, ...) doesn't break
        # the live run. Log at debug so a local developer can see why
        # the persistence isn't kicking in without spamming CI logs.
        logger.debug("FX cache write failed for %s: %s", currency, exc)


class ExchangeRate:
    def __init__(self, *, cache_dir: Path | None = None):
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
        # Optional on-disk persistence layer. The default resolves
        # the directory from ``INVESTING_FX_CACHE_DIR`` once per
        # instance so a test that mutates the env var between calls
        # can still re-create an ``ExchangeRate`` and pick up the
        # new value.
        self._cache_dir: Path | None = (
            cache_dir if cache_dir is not None else _fx_cache_dir()
        )

    def _current(self, currency):
        if currency == "USD":
            return 1.0
        if currency in self._rates:
            return self._rates[currency]
        rate = _call_with_retry(
            lambda: yf.Ticker(f"{currency}USD=X").info["regularMarketPrice"],
            description="yfinance fx info",
        )
        if currency == "GBp":
            rate /= 100
        self._rates[currency] = rate
        return rate

    def _historical(self, currency, date):
        if currency == "USD":
            return 1.0
        if currency not in self._history:
            # Try the disk cache first; on a hit we skip the yfinance
            # round trip entirely. The cache stores raw arrays so the
            # in-memory layout downstream is identical regardless of
            # the source.
            cached = (
                _load_history_from_disk(self._cache_dir, currency)
                if self._cache_dir is not None
                else None
            )
            if cached is not None:
                self._history[currency] = cached
            else:
                hist = _call_with_retry(
                    lambda: yf.Ticker(f"{currency}USD=X").history(
                        period="max", interval="1d", auto_adjust=False,
                    ),
                    description="yfinance fx history",
                )
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
                # Persist on every fresh fetch so the next process
                # starts warm. Subsequent in-memory hits bypass this
                # branch entirely, so the write happens at most once
                # per currency per process.
                if self._cache_dir is not None and date_arr.size > 0:
                    _save_history_to_disk(
                        self._cache_dir, currency, date_arr, rate_arr,
                    )
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
# autouse reset fixture. The alias accepts either an ``ExchangeRate``
# instance or any plain callable matching the same shape so test stubs
# (``lambda currency, date=None: 1.0``) satisfy the contract.
type FxRate = "ExchangeRate" | Callable[..., float]




def _fx_or_default(fx: FxRate | None) -> FxRate:
    """Return ``fx`` if provided, otherwise a fresh ``ExchangeRate``.

    Each caller that doesn't supply an ``fx`` gets its own private
    cache -- which is the safe default for ad-hoc usage (tests,
    one-off scripts) and benign for production because ``main``
    always passes an explicit instance, so the fallback path is
    effectively unreachable on the hot path.
    """
    return fx if fx is not None else ExchangeRate()
