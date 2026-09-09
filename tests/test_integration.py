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
def stub_world(monkeypatch):
    """Mock Yahoo + requests for full-pipeline tests."""
    from investing.logos import LogoCache

    tickers = {
        "AAA": _mk_ticker("AAA", price=150.0),
        "BBB": _mk_ticker("BBB", price=80.0),
    }
    monkeypatch.setattr(_holdings.yf, "Ticker", lambda s: tickers[s])
    fake_session = MagicMock()
    fake_session.head.return_value = MagicMock(status_code=404)
    monkeypatch.setattr(
        "investing.webpage._page.LogoCache",
        lambda: LogoCache(session=fake_session),
    )
    return tickers


class TestGetHoldings:
    def test_splits_open_vs_closed_positions(self, stub_world, stub_exchange_rate, at_datetime):
        when = datetime(2025, 1, 1)
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
        result = get_holdings(
            transactions,
            fx=stub_exchange_rate,
            now=at_datetime(when),
        )

        assert {h["ticker"] for h in result["current"]} == {"NMS:AAA"}
        assert {h["ticker"] for h in result["historical"]} == {"NMS:BBB"}
        assert result["current"][0]["current_value_usd"] == pytest.approx(1500.0)

    def test_current_sorted_by_latest_buy_descending(
        self, stub_world, stub_exchange_rate, at_datetime
    ):
        when = datetime(2025, 1, 1)
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
        result = get_holdings(
            transactions,
            fx=stub_exchange_rate,
            now=at_datetime(when),
        )
        assert [h["ticker"] for h in result["current"]] == ["NMS:BBB", "NMS:AAA"]

    def test_fixed_income_routed_to_dedicated_buckets(
        self, stub_world, stub_exchange_rate, at_datetime
    ):
        when = datetime(2025, 1, 1)
        equities = [
            {
                "date": "01-01-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 100.0,
                "action": "BUY",
            },
        ]
        fixed_income = [
            {
                "date": "01-02-2024",
                "ticker": "BBB",
                "quantity": 5,
                "price_per_share": 70.0,
                "action": "BUY",
            },
        ]
        result = get_holdings(
            equities,
            fixed_income=fixed_income,
            fx=stub_exchange_rate,
            now=at_datetime(when),
        )

        assert [h["ticker"] for h in result["current"]] == ["NMS:AAA"]
        assert result["current"][0]["asset_class"] == "equity"
        assert [h["ticker"] for h in result["current_fixed_income"]] == ["NMS:BBB"]
        assert result["current_fixed_income"][0]["asset_class"] == "fixed_income"
        trade_tickers = {t["ticker"] for t in result["trades"]}
        assert {"NMS:AAA", "NMS:BBB"}.issubset(trade_tickers)


class TestGenerateWebpage:
    def test_full_render_writes_index_html(
        self, stub_world, stub_exchange_rate, chdir_tmp, at_datetime
    ):
        when = datetime(2025, 6, 1)
        transactions = [
            {
                "date": "01-01-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 100.0,
                "action": "BUY",
            },
        ]
        holdings = get_holdings(
            transactions,
            fx=stub_exchange_rate,
            now=at_datetime(when),
        )
        holdings["current"][0]["current_weight%"] = 100.0

        total_return = {
            "start_date": datetime(2024, 1, 1),
            "history": [(datetime(2024, 1, 1), 1.0)],
            "twr%": 50.0,
            "cagr%": 50.0,
        }
        benchmarks = [
            {
                "ticker": "LSE:VUAA.L",
                "name": "Vanguard S&P 500 UCITS ETF",
                "tsr%": 20.0,
                "cagr%": 20.0,
                "periods": [{"start": datetime(2024, 1, 1), "end": None}],
                "history": [(datetime(2024, 1, 1), 1.0)],
            }
        ]

        generate_webpage(total_return, benchmarks, holdings, now=at_datetime(when))

        html = (chdir_tmp / "index.html").read_text()
        assert "<title>Investment Portfolio</title>" in html
        assert "NMS:AAA" in html
        assert "S&amp;P 500" in html
        assert "50.0%" in html
        assert "Current holdings" in html

    def test_full_render_includes_fixed_income_subsection(
        self, stub_world, stub_exchange_rate, chdir_tmp, at_datetime
    ):
        when = datetime(2025, 6, 1)
        equities = [
            {
                "date": "01-01-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 100.0,
                "action": "BUY",
            },
        ]
        fixed_income = [
            {
                "date": "01-02-2024",
                "ticker": "BBB",
                "quantity": 5,
                "price_per_share": 70.0,
                "action": "BUY",
            },
        ]
        holdings = get_holdings(
            equities,
            fixed_income=fixed_income,
            fx=stub_exchange_rate,
            now=at_datetime(when),
        )
        holdings["current"][0]["current_weight%"] = 60.0
        holdings["current_fixed_income"][0]["current_weight%"] = 40.0
        holdings["allocation%"] = {
            "Equities": 60.0,
            "Fixed Income": 40.0,
            "Cash & Cash Equivalents": 0.0,
        }
        holdings["top_10"] = {"NMS:AAA": 60.0}

        total_return = {
            "start_date": datetime(2024, 1, 1),
            "history": [(datetime(2024, 1, 1), 1.0)],
            "twr%": 50.0,
            "cagr%": 50.0,
        }
        generate_webpage(total_return, [], holdings, now=at_datetime(when))

        html = (chdir_tmp / "index.html").read_text()
        # Both asset classes render as group bands inside the one
        # holdings table, each quoting its share of the portfolio from
        # the same rollup the allocation bar uses.
        assert "Equities · 60.0% of portfolio" in html
        assert "Fixed Income · 40.0% of portfolio" in html
        # Cash is a residual with no rows of its own, so it rides along
        # with the fixed-income band rather than being the one
        # allocation slice with nowhere in the table to land.
        assert "Cash 0.0%" in html
        assert "AAA Inc." in html
        assert "BBB Inc." in html
        # A bond ETF is not the equity sleeve, so its weight bar takes
        # the neutral fill rather than the JG accent.
        assert "holdings__bar-fill--muted" in html
