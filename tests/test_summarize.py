"""Tests for ``summarize``: allocation %, top-10 weights, totals."""
from __future__ import annotations

from datetime import datetime

import pytest

import update
from update import summarize


def _holding(ticker, value, name=None):
    return {
        "ticker": ticker,
        "name": name or ticker,
        "tsr%": 0.0,
        "cagr%": 0.0,
        "is_current": True,
        "current_weight%": None,
        "current_value_usd": value,
        "periods": [],
        "latest_buy": datetime(2024, 1, 1),
        "latest_sell": None,
    }


@pytest.fixture
def fx_one_to_one():
    """A stub fx callable that pins every rate to 1.0.

    Returned to the test which passes it as ``summarize(..., fx=fx_one_to_one)``.
    """
    def _rate(currency, date=None):  # noqa: ARG001
        return 1.0
    return _rate


class TestSummarize:
    def test_empty_portfolio(self, fx_one_to_one):
        holdings = {"current": [], "historical": []}
        total = summarize(holdings, cash=[], fx=fx_one_to_one)

        assert total == 0.0
        assert holdings["allocation%"] is None
        assert holdings["top_10"] is None

    def test_equity_only_portfolio(self, fx_one_to_one):
        holdings = {
            "current": [_holding("AAA", 400.0), _holding("BBB", 600.0)],
            "historical": [],
        }
        total = summarize(holdings, cash=[], fx=fx_one_to_one)

        assert total == pytest.approx(1000.0)
        assert holdings["allocation%"] == {
            "Equities": 100.0,
            "Cash & Cash Equivalents": 0.0,
        }
        # Weights sum to 100%.
        weights = [h["current_weight%"] for h in holdings["current"]]
        assert sum(weights) == pytest.approx(100.0)

    def test_cash_only_portfolio(self, fx_one_to_one):
        holdings = {"current": [], "historical": []}
        total = summarize(
            holdings,
            cash=[{"currency_code": "USD", "amount": 500.0}],
            fx=fx_one_to_one,
        )

        assert total == pytest.approx(500.0)
        assert holdings["allocation%"] == {
            "Equities": 0.0,
            "Cash & Cash Equivalents": 100.0,
        }
        assert holdings["top_10"] is None  # no equities

    def test_mixed_allocation_uses_fx_conversion(self):
        # EUR cash is worth 1.10 USD per unit.
        rates = {"USD": 1.0, "EUR": 1.10}

        def fx(currency, date=None):  # noqa: ARG001
            return rates[currency]

        holdings = {"current": [_holding("AAA", 100.0)], "historical": []}
        total = summarize(
            holdings,
            cash=[
                {"currency_code": "USD", "amount": 50.0},
                {"currency_code": "EUR", "amount": 100.0},  # = 110 USD
            ],
            fx=fx,
        )

        assert total == pytest.approx(100 + 50 + 110)
        # ``summarize`` stores unrounded percentages so downstream
        # math (the "Other equities" bucket sums these) stays
        # precise; the expected values match that full precision.
        # Equity weight = 100 / 260
        assert holdings["allocation%"]["Equities"] == pytest.approx(
            100 * 100 / 260
        )
        assert holdings["allocation%"]["Cash & Cash Equivalents"] == pytest.approx(
            100 * 160 / 260
        )

    def test_holding_weights_are_in_percent_of_total(self, fx_one_to_one):
        holdings = {
            "current": [
                _holding("AAA", 250.0),
                _holding("BBB", 750.0),
            ],
            "historical": [],
        }
        summarize(holdings, cash=[], fx=fx_one_to_one)

        weights = {h["ticker"]: h["current_weight%"] for h in holdings["current"]}
        assert weights == {"AAA": 25.0, "BBB": 75.0}

    def test_top_10_includes_all_when_eleven_or_fewer(self, fx_one_to_one):
        holdings = {
            "current": [_holding(f"T{i}", 10.0) for i in range(11)],
            "historical": [],
        }
        summarize(holdings, cash=[], fx=fx_one_to_one)

        assert "Other equities" not in holdings["top_10"]
        assert len(holdings["top_10"]) == 11

    def test_top_10_truncates_and_buckets_remainder(self, fx_one_to_one):
        holdings = {
            "current": [_holding(f"T{i:02d}", float(100 - i)) for i in range(12)],
            "historical": [],
        }
        summarize(holdings, cash=[], fx=fx_one_to_one)

        assert "Other equities" in holdings["top_10"]
        assert len(holdings["top_10"]) == 11  # 10 named + bucket
        # Bucket holds at least one ticker's worth of weight.
        assert holdings["top_10"]["Other equities"] > 0

    def test_top_10_is_sorted_by_weight_descending(self, fx_one_to_one):
        holdings = {
            "current": [
                _holding("SMALL", 10.0),
                _holding("BIG", 90.0),
                _holding("MID", 50.0),
            ],
            "historical": [],
        }
        summarize(holdings, cash=[], fx=fx_one_to_one)

        keys = list(holdings["top_10"].keys())
        assert keys == ["BIG", "MID", "SMALL"]

    def test_historical_holdings_are_ignored_in_totals(self, fx_one_to_one):
        # historical entries should not contribute to value or weights.
        historical = _holding("OLD", 9999.0)
        historical["is_current"] = False
        holdings = {
            "current": [_holding("AAA", 100.0)],
            "historical": [historical],
        }
        total = summarize(holdings, cash=[], fx=fx_one_to_one)

        assert total == pytest.approx(100.0)
        assert holdings["current"][0]["current_weight%"] == pytest.approx(100.0)
