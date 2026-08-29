"""Tests for the opt-in on-disk FX history cache.

The cache only kicks in when ``INVESTING_FX_CACHE_DIR`` is set
(or ``cache_dir=`` is passed explicitly). The tests exercise:
  * cold start (disk miss -> yfinance fetch + write)
  * warm start (disk hit -> no yfinance call)
  * corrupt cache file (silent fallback to yfinance)
  * default behaviour when env var is unset (no disk activity)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from investing.fx import ExchangeRate


def _hist_for(items):
    return {"Close": dict(items)}


def _stub_ticker(monkeypatch, mapping):
    def _factory(symbol):
        if symbol not in mapping:
            raise AssertionError(f"Unexpected FX ticker requested: {symbol!r}")
        return mapping[symbol]

    monkeypatch.setattr("investing.fx.yf.Ticker", _factory)


def test_disk_cache_disabled_by_default(monkeypatch, tmp_path):
    """No env var -> no cache file written even after a fetch."""
    monkeypatch.delenv("INVESTING_FX_CACHE_DIR", raising=False)
    ticker = MagicMock()
    ticker.history = MagicMock(return_value=_hist_for({"2024-01-01": 1.1}))
    _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

    fx = ExchangeRate()
    fx("EUR", "2024-01-15")
    # Directory under tmp_path stays untouched.
    assert list(tmp_path.iterdir()) == []


def test_disk_cache_writes_on_first_fetch(monkeypatch, tmp_path):
    """A fresh ExchangeRate writes its yfinance response to disk."""
    monkeypatch.setenv("INVESTING_FX_CACHE_DIR", str(tmp_path))
    ticker = MagicMock()
    ticker.history = MagicMock(return_value=_hist_for({"2024-01-01": 1.1, "2024-02-01": 1.2}))
    _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

    fx = ExchangeRate()
    fx("EUR", "2024-02-15")

    cache_file = tmp_path / "fx-EUR.npz"
    assert cache_file.is_file()
    data = np.load(cache_file)
    assert data["dates"].size == 2
    assert data["rates"].tolist() == [pytest.approx(1.1), pytest.approx(1.2)]


def test_disk_cache_hit_skips_yfinance(monkeypatch, tmp_path):
    """A pre-populated cache file means no yfinance call on lookup."""
    monkeypatch.setenv("INVESTING_FX_CACHE_DIR", str(tmp_path))
    # Plant a cache file directly.
    dates = np.array(["2024-01-01", "2024-02-01"], dtype="datetime64[D]")
    rates = np.array([1.1, 1.2], dtype=float)
    np.savez(tmp_path / "fx-EUR.npz", dates=dates, rates=rates)

    ticker = MagicMock()
    ticker.history = MagicMock(
        side_effect=AssertionError("yfinance must not be called on a cache hit"),
    )
    _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

    fx = ExchangeRate()
    # Should resolve straight off the cached arrays.
    assert fx("EUR", "2024-02-15") == pytest.approx(1.2)


def test_corrupt_cache_file_falls_back_to_yfinance(monkeypatch, tmp_path):
    """A junk file at the cache path is silently replaced via re-fetch."""
    monkeypatch.setenv("INVESTING_FX_CACHE_DIR", str(tmp_path))
    (tmp_path / "fx-EUR.npz").write_bytes(b"not a real npz file")

    ticker = MagicMock()
    ticker.history = MagicMock(return_value=_hist_for({"2024-01-01": 1.5}))
    _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

    fx = ExchangeRate()
    assert fx("EUR", "2024-01-15") == pytest.approx(1.5)
    # And the corrupt file gets replaced.
    data = np.load(tmp_path / "fx-EUR.npz")
    assert data["rates"].tolist() == [pytest.approx(1.5)]


def test_explicit_cache_dir_overrides_env(monkeypatch, tmp_path):
    """``cache_dir=`` constructor arg wins over ``INVESTING_FX_CACHE_DIR``."""
    monkeypatch.setenv("INVESTING_FX_CACHE_DIR", "/nonexistent/from-env")
    ticker = MagicMock()
    ticker.history = MagicMock(return_value=_hist_for({"2024-01-01": 2.0}))
    _stub_ticker(monkeypatch, {"GBPUSD=X": ticker})

    fx = ExchangeRate(cache_dir=tmp_path)
    fx("GBP", "2024-02-01")
    assert (tmp_path / "fx-GBP.npz").is_file()


class TestStoreBackedFxHistory:
    """When a ``MarketDataStore`` is wired in it owns the FX archive.

    The store path supersedes the loose ``INVESTING_FX_CACHE_DIR``
    files: it merges the fetched series with what is already committed
    under ``market_data/fx/`` so a rate Yahoo later drops is preserved.
    """

    def test_the_store_never_short_circuits_the_fetch(self, monkeypatch, tmp_path):
        """An FX archive is always a day behind, so the live call stands.

        Unlike a ticker snapshot, an FX series gains a row every
        trading day: the committed archive can supply history but never
        today's rate. The store's job is the older rows Yahoo may have
        dropped, not a cache hit.
        """
        from investing.market_data_store import MarketDataStore

        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path)
        store.save_fx_history(
            "EUR",
            np.array(["2024-01-01", "2024-02-01"], dtype="datetime64[D]"),
            np.array([1.10, 1.20], dtype=float),
        )

        ticker = MagicMock()
        ticker.history = MagicMock(return_value=_hist_for({"2024-03-01": 1.30}))
        _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

        fx = ExchangeRate(store=store)
        assert fx("EUR", "2024-03-15") == pytest.approx(1.30)
        assert ticker.history.call_count == 1

    def test_a_store_miss_fetches_then_merges_through_the_store(self, monkeypatch, tmp_path):
        from investing.market_data_store import MarketDataStore

        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path, persist=True)
        # An archived rate Yahoo will not return in the live response.
        store.save_fx_history(
            "EUR",
            np.array(["2010-01-04"], dtype="datetime64[D]"),
            np.array([1.44], dtype=float),
        )

        ticker = MagicMock()
        ticker.history = MagicMock(return_value=_hist_for({"2024-01-01": 1.10}))
        _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

        fx = ExchangeRate(store=store)

        # Live row resolves...
        assert fx("EUR", "2024-01-15") == pytest.approx(1.10)
        # ...and the archived row Yahoo dropped survived the merge.
        assert fx("EUR", "2010-01-05") == pytest.approx(1.44)
        assert ticker.history.call_count == 1
