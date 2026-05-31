"""End-to-end integration tests that wire the offline-safe pieces together.

All external services (Yahoo Finance, ``requests.head`` for logos) are
mocked. We exercise:

* ``get_holdings`` from raw transactions through to per-ticker summaries.
* ``generate_webpage`` writing a complete ``index.html`` file.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

import investing.holdings as _holdings
from investing.performance import get_holdings
from investing.webpage import generate_webpage


def _mk_ticker(symbol, *, price, currency="USD"):
    mock = MagicMock()
    mock.get_info.return_value = {
        "currency": currency,
        "exchange": "NMS",
        "symbol": symbol,
        "longName": f"{symbol} Inc.",
        "regularMarketPrice": price,
    }
    mock.splits = {}
    mock.get_dividends.return_value = {}
    return mock


@pytest.fixture
def stub_fx():
    """A stub fx callable that always returns 1.0."""
    def _rate(currency, date=None):  # noqa: ARG001
        return 1.0
    return _rate


@pytest.fixture
def stub_world(monkeypatch):
    """Mock Yahoo + requests for full-pipeline tests.

    FX is injected via the ``fx`` parameter on ``get_holdings`` / etc.;
    use the :func:`stub_fx` fixture alongside this one when the test
    constructs holdings.
    """
    from investing.logos import LogoCache

    tickers = {
        "AAA": _mk_ticker("AAA", price=150.0),
        "BBB": _mk_ticker("BBB", price=80.0),
    }
    monkeypatch.setattr(_holdings.yf, "Ticker", lambda s: tickers[s])
    # Force the logo resolver to skip every probe -- the integration
    # tests run without network, so all extensions must return 404 and
    # the resolver should fall through to the bundled placeholder.
    fake_session = MagicMock()
    fake_session.head.return_value = MagicMock(status_code=404)
    monkeypatch.setattr(
        "investing.webpage._page.LogoCache",
        lambda: LogoCache(session=fake_session),
    )
    return tickers


class TestGetHoldings:
    def test_splits_open_vs_closed_positions(self, stub_world, stub_fx, freeze_today):
        freeze_today(datetime(2025, 1, 1))
        # AAA: bought and still holding -> current.
        # BBB: fully sold -> historical.
        transactions = [
            {
                "date": "01-01-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 100.0,
                "action": "BUY",
            },
            {
                "date": "01-01-2024",
                "ticker": "BBB",
                "quantity": 5,
                "price_per_share": 60.0,
                "action": "BUY",
            },
            {
                "date": "01-06-2024",
                "ticker": "BBB",
                "quantity": 5,
                "price_per_share": 80.0,
                "action": "SELL",
            },
        ]
        result = get_holdings(transactions, fx=stub_fx)

        assert {h["ticker"] for h in result["current"]} == {"NMS:AAA"}
        assert {h["ticker"] for h in result["historical"]} == {"NMS:BBB"}
        # current_value_usd for AAA = 10 * 150
        assert result["current"][0]["current_value_usd"] == pytest.approx(1500.0)

    def test_current_sorted_by_latest_buy_descending(self, stub_world, stub_fx, freeze_today):
        freeze_today(datetime(2025, 1, 1))
        transactions = [
            {
                "date": "01-01-2024",
                "ticker": "AAA",
                "quantity": 1,
                "price_per_share": 100.0,
                "action": "BUY",
            },
            {
                "date": "06-06-2024",
                "ticker": "BBB",
                "quantity": 1,
                "price_per_share": 50.0,
                "action": "BUY",
            },
        ]
        result = get_holdings(transactions, fx=stub_fx)
        # BBB bought later -> appears first.
        assert [h["ticker"] for h in result["current"]] == ["NMS:BBB", "NMS:AAA"]


class TestGenerateWebpage:
    def test_full_render_writes_index_html(
        self, stub_world, stub_fx, chdir_tmp, freeze_today, monkeypatch
    ):
        freeze_today(datetime(2025, 6, 1))

        # Build a full holdings dict by going through the real pipeline.
        transactions = [
            {
                "date": "01-01-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 100.0,
                "action": "BUY",
            },
        ]
        holdings = get_holdings(transactions, fx=stub_fx)
        # Mimic what summarize() does — generate_webpage assumes weights filled.
        holdings["current"][0]["current_weight%"] = 100.0

        total_return = {
            "start_date": datetime(2024, 1, 1),
            "history": [(datetime(2024, 1, 1), 1.0)],
            "twr%": 50.0,
            "cagr%": 50.0,
        }
        benchmarks = [{
            "ticker": "LSE:VUAA.L",
            "name": "Vanguard S&P 500 UCITS ETF",
            "tsr%": 20.0,
            "cagr%": 20.0,
            "periods": [{"start": datetime(2024, 1, 1), "end": None}],
            "history": [(datetime(2024, 1, 1), 1.0)],
        }]

        generate_webpage(total_return, benchmarks, holdings)

        html = (chdir_tmp / "index.html").read_text()
        assert "<title>Jan Grzybek - Investment Portfolio</title>" in html
        assert "NMS:AAA" in html
        # The benchmark column is now labelled by its friendly display
        # name (the raw ticker no longer appears in the document body
        # since all logo lookups fall back to courage.png in this test).
        assert "S&amp;P 500" in html
        assert "50.0%" in html  # TWR
        assert "Current holdings" in html
