"""Shared pytest fixtures and import-path setup for the test suite.

`update.py` lives at the repository root, so we prepend the project root
to ``sys.path`` before any test imports it. We also reset the module-level
``exchange_rate`` singleton between tests so that cached state from one
test never leaks into another.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import update  # noqa: E402  (import after sys.path mutation)


@pytest.fixture(autouse=True)
def _reset_exchange_rate_cache():
    """Ensure the global ExchangeRate cache is empty for every test."""
    update.exchange_rate._rates = {}
    update.exchange_rate._history = {}
    yield
    update.exchange_rate._rates = {}
    update.exchange_rate._history = {}


@pytest.fixture
def stub_exchange_rate(monkeypatch):
    """Replace the global exchange rate with one that always returns 1.0.

    Many tests use USD-denominated tickers; pinning the rate to 1.0 makes
    expected values trivial to reason about, regardless of currency.
    """
    def _rate(currency, date=None):  # noqa: ARG001
        return 1.0

    monkeypatch.setattr(update, "exchange_rate", _rate)
    return _rate


def _make_ticker_mock(
    *,
    currency: str = "USD",
    exchange: str = "NMS",
    symbol: str = "AAPL",
    long_name: str = "Apple Inc.",
    price: float = 100.0,
    splits: dict | None = None,
    dividends: dict | None = None,
):
    """Build a MagicMock that imitates the slice of ``yf.Ticker`` we use."""
    mock = MagicMock()
    mock.get_info.return_value = {
        "currency": currency,
        "exchange": exchange,
        "symbol": symbol,
        "longName": long_name,
        "regularMarketPrice": price,
    }
    # `Holding._get_splits_dividends` iterates with `.items()`; a plain dict
    # mirrors the pandas Series interface closely enough for our needs.
    mock.splits = splits or {}
    mock.get_dividends.return_value = dividends or {}
    return mock


@pytest.fixture
def make_ticker_mock():
    """Factory fixture exposing the helper above to tests."""
    return _make_ticker_mock


@pytest.fixture
def patch_yf_ticker(monkeypatch):
    """Patch ``yf.Ticker`` to return preconfigured per-ticker mocks.

    Usage::

        def test_x(patch_yf_ticker, make_ticker_mock):
            patch_yf_ticker({"AAPL": make_ticker_mock(price=42.0)})
    """
    def _install(mapping):
        def _factory(ticker):
            if ticker not in mapping:
                raise AssertionError(f"Unexpected ticker requested: {ticker!r}")
            return mapping[ticker]

        monkeypatch.setattr(update.yf, "Ticker", _factory)
        return mapping

    return _install


@pytest.fixture
def freeze_today(monkeypatch):
    """Pin ``datetime.today()`` / ``datetime.now()`` inside update.py."""
    def _freeze(when: datetime):
        class _FrozenDateTime(datetime):
            @classmethod
            def today(cls):
                return when

            @classmethod
            def now(cls, tz=None):  # noqa: ARG002
                return when

        monkeypatch.setattr(update, "datetime", _FrozenDateTime)
        return when

    return _freeze


@pytest.fixture
def chdir_tmp(tmp_path, monkeypatch):
    """Run the test inside a temp directory so files written by the code
    under test (``index.html``, ``assets/*.svg``) do not litter the repo."""
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "assets", exist_ok=True)
    return tmp_path
