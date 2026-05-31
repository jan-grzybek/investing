"""Shared pytest fixtures for the test suite.

Import-path bootstrapping (``update.py`` and the ``investing/``
package live at the repository root) is declared in
``pyproject.toml`` under ``[tool.pytest.ini_options] pythonpath``;
no ``sys.path`` mutation is needed here.

The package no longer carries a mutable ``exchange_rate`` singleton --
FX is threaded through the API as an explicit ``fx`` parameter on
``Holding``/``get_holdings``/``summarize``/``get_benchmarks``. Tests
pass the :func:`stub_exchange_rate` callable directly to whichever
constructor they're exercising; no module-level patching is needed
and no autouse cleanup fixture is required to keep tests isolated.
"""
from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def stub_exchange_rate():
    """A stub fx callable that always returns ``1.0``.

    Many tests use USD-denominated tickers; pinning the rate to 1.0
    makes expected values trivial to reason about regardless of
    currency. Pass the returned callable to ``Holding(fx=...)``,
    ``get_holdings(..., fx=...)``, ``summarize(..., fx=...)``, or
    ``get_benchmarks(..., fx=...)`` -- there is no module-level
    state to patch.
    """
    def _rate(currency, date=None):  # noqa: ARG001
        return 1.0

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
    import investing.holdings as _holdings

    def _install(mapping):
        def _factory(ticker):
            if ticker not in mapping:
                raise AssertionError(f"Unexpected ticker requested: {ticker!r}")
            return mapping[ticker]

        monkeypatch.setattr(_holdings.yf, "Ticker", _factory)
        return mapping

    return _install


@pytest.fixture
def freeze_today(monkeypatch):
    """Pin ``datetime.today()`` / ``datetime.now()`` across the package.

    The page generator is composed of several modules
    (``investing.holdings``, ``investing.performance``,
    ``investing.webpage._page``, ...) and each one holds its own
    import-bound ``datetime`` symbol. Patching just one leaves the
    others on the real clock and breaks any cross-module test, so
    we walk the whole list and swap ``datetime`` everywhere it
    appears.

    New code prefers the explicit ``now=`` parameter exposed on
    :class:`Holding` / :class:`Webpage` / :func:`calc_twr` /
    :func:`get_benchmarks` -- pass a closure and skip this fixture
    entirely.
    """
    import investing.holdings as _holdings
    import investing.performance as _performance
    import investing.webpage._page as _webpage_page

    def _freeze(when: datetime):
        class _FrozenDateTime(datetime):
            @classmethod
            def today(cls):
                return when

            @classmethod
            def now(cls, tz=None):  # noqa: ARG002
                return when

        for mod in (_holdings, _performance, _webpage_page):
            monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
        return when

    return _freeze


@pytest.fixture
def chdir_tmp(tmp_path, monkeypatch):
    """Run the test inside a temp directory so files written by the code
    under test (``index.html``, ``assets/*.svg``) do not litter the repo."""
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "assets", exist_ok=True)
    return tmp_path
