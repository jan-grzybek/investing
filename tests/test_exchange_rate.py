"""Tests for the ``ExchangeRate`` helper class.

We replace ``yf.Ticker`` with a stub so the tests are fully offline and
deterministic.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

import investing.fx as _fx
from investing.fx import ExchangeRate


def _close_series(items):
    """Return an object that quacks like a pandas Series for ``Close``.

    The production code does ``hist["Close"].items()``; a plain dict mirrors
    that interface perfectly.
    """
    return dict(items)


def _hist_for(items):
    """Build the object returned by ``yf.Ticker(...).history(...)``."""
    return {"Close": _close_series(items)}


def _stub_ticker(monkeypatch, mapping):
    """Patch ``investing.fx.yf.Ticker`` to return per-symbol stubs.

    ``mapping`` is keyed by the FX symbol used by yfinance (e.g. "EURUSD=X").
    """
    def _factory(symbol):
        if symbol not in mapping:
            raise AssertionError(f"Unexpected FX ticker requested: {symbol!r}")
        return mapping[symbol]

    monkeypatch.setattr(_fx.yf, "Ticker", _factory)


class TestCurrent:
    def test_usd_is_always_unity(self):
        fx = ExchangeRate()
        assert fx("USD") == 1.0

    def test_non_usd_is_fetched_once_and_cached(self, monkeypatch):
        ticker = MagicMock()
        ticker.info = {"regularMarketPrice": 1.1}
        _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

        fx = ExchangeRate()
        assert fx("EUR") == pytest.approx(1.1)
        # Mutating the underlying info between calls proves the second call
        # is served from cache, not refetched.
        ticker.info["regularMarketPrice"] = 999.0
        assert fx("EUR") == pytest.approx(1.1)

    def test_gbp_minor_unit_is_scaled(self, monkeypatch):
        ticker = MagicMock()
        ticker.info = {"regularMarketPrice": 130.0}  # 130 pence = 1.30 GBP-USD
        _stub_ticker(monkeypatch, {"GBpUSD=X": ticker})

        fx = ExchangeRate()
        assert fx("GBp") == pytest.approx(1.30)


class TestHistorical:
    def _build_fx(self, monkeypatch, currency, samples):
        ticker = MagicMock()
        ticker.history.return_value = _hist_for(samples)
        _stub_ticker(monkeypatch, {f"{currency}USD=X": ticker})
        return ExchangeRate()

    def test_usd_historical_is_unity(self):
        fx = ExchangeRate()
        assert fx("USD", datetime(2024, 6, 1)) == 1.0

    def test_bisect_picks_the_latest_entry_on_or_before_date(self, monkeypatch):
        fx = self._build_fx(monkeypatch, "EUR", [
            ("2024-01-01 00:00:00", 1.10),
            ("2024-02-01 00:00:00", 1.20),
            ("2024-03-01 00:00:00", 1.30),
        ])

        # Date right on a sample.
        assert fx("EUR", datetime(2024, 2, 1)) == pytest.approx(1.20)
        # Date in between samples uses the previous one.
        assert fx("EUR", datetime(2024, 2, 15)) == pytest.approx(1.20)
        # Date after the latest sample uses the latest.
        assert fx("EUR", datetime(2024, 6, 1)) == pytest.approx(1.30)

    def test_date_before_first_sample_uses_first_sample(self, monkeypatch):
        fx = self._build_fx(monkeypatch, "EUR", [
            ("2024-01-01 00:00:00", 1.10),
            ("2024-02-01 00:00:00", 1.20),
        ])

        # idx becomes -1 then clamped to 0.
        assert fx("EUR", datetime(2023, 12, 31)) == pytest.approx(1.10)

    def test_accepts_date_objects_not_just_datetimes(self, monkeypatch):
        fx = self._build_fx(monkeypatch, "EUR", [
            ("2024-01-01 00:00:00", 1.10),
        ])

        assert fx("EUR", date(2024, 5, 1)) == pytest.approx(1.10)

    def test_nan_close_values_are_skipped(self, monkeypatch):
        fx = self._build_fx(monkeypatch, "EUR", [
            ("2024-01-01 00:00:00", 1.10),
            ("2024-01-02 00:00:00", float("nan")),
            ("2024-01-03 00:00:00", 1.30),
        ])

        # The Jan-2 NaN row must be dropped, so a query for that day should
        # fall back to the Jan-1 value.
        assert fx("EUR", datetime(2024, 1, 2)) == pytest.approx(1.10)
        assert fx("EUR", datetime(2024, 1, 3)) == pytest.approx(1.30)

    def test_history_is_cached(self, monkeypatch):
        ticker = MagicMock()
        ticker.history.return_value = _hist_for([
            ("2024-01-01 00:00:00", 1.10),
        ])
        _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

        fx = ExchangeRate()
        fx("EUR", datetime(2024, 5, 1))
        fx("EUR", datetime(2024, 6, 1))
        assert ticker.history.call_count == 1

    def test_empty_history_falls_back_to_current(self, monkeypatch, caplog):
        ticker = MagicMock()
        ticker.history.return_value = _hist_for([])
        ticker.info = {"regularMarketPrice": 1.42}
        _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

        fx = ExchangeRate()
        with caplog.at_level("WARNING"):
            assert fx("EUR", datetime(2024, 5, 1)) == pytest.approx(1.42)
            # Repeat lookups for the same currency must not re-emit the
            # warning -- the operator only needs to learn about the
            # missing history once per process.
            assert fx("EUR", datetime(2024, 6, 1)) == pytest.approx(1.42)

        empty_warnings = [
            rec for rec in caplog.records
            if rec.levelname == "WARNING" and "FX history for EUR/USD is empty" in rec.message
        ]
        assert len(empty_warnings) == 1

    def test_gbp_minor_unit_is_scaled_for_historical(self, monkeypatch):
        fx = self._build_fx(monkeypatch, "GBp", [
            ("2024-01-01 00:00:00", 125.0),
        ])

        assert fx("GBp", datetime(2024, 5, 1)) == pytest.approx(1.25)


class TestCallable:
    def test_dispatches_on_date_argument(self, monkeypatch):
        current = MagicMock()
        current.info = {"regularMarketPrice": 1.0}
        historical = MagicMock()
        historical.history.return_value = _hist_for([
            ("2024-01-01 00:00:00", 2.0),
        ])
        # Same FX symbol; both code paths use the same ticker instance.
        ticker = MagicMock()
        ticker.info = current.info
        ticker.history = historical.history
        _stub_ticker(monkeypatch, {"EURUSD=X": ticker})

        fx = ExchangeRate()
        assert fx("EUR") == pytest.approx(1.0)
        assert fx("EUR", datetime(2024, 5, 1)) == pytest.approx(2.0)
