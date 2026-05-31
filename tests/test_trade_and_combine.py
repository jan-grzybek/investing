"""Tests for the ``Trade`` data class and the ``combine_and_sort`` helper."""
from __future__ import annotations

from datetime import datetime

import pytest

import investing.trades as _trades
from investing.trades import Trade, combine_and_sort


def _txn(date, ticker, qty, price, action):
    return {
        "date": date,
        "ticker": ticker,
        "quantity": qty,
        "price_per_share": price,
        "action": action,
    }


class TestTrade:
    def test_attributes_are_preserved(self):
        t = Trade(datetime(2024, 1, 2), "AAPL", 10, 150.0, "BUY")
        assert t.date == datetime(2024, 1, 2)
        assert t.ticker == "AAPL"
        assert t.quantity == 10
        assert t.price == 150.0
        assert t.action == "BUY"


class TestCombineAndSort:
    def test_empty_input_returns_empty_list(self):
        assert combine_and_sort([]) == []

    def test_single_transaction_produces_single_trade(self):
        trades = combine_and_sort([_txn("01-01-2024", "AAPL", 5, 100.0, "BUY")])

        assert len(trades) == 1
        trade = trades[0]
        assert trade.ticker == "AAPL"
        assert trade.date == datetime(2024, 1, 1)
        assert trade.quantity == 5
        assert trade.price == pytest.approx(100.0)
        assert trade.action == "BUY"

    def test_same_day_same_action_is_aggregated_with_weighted_price(self):
        # 10 shares @ 100 + 30 shares @ 200 -> 40 shares @ weighted avg 175
        trades = combine_and_sort([
            _txn("01-01-2024", "AAPL", 10, 100.0, "BUY"),
            _txn("01-01-2024", "AAPL", 30, 200.0, "BUY"),
        ])

        assert len(trades) == 1
        assert trades[0].quantity == 40
        assert trades[0].price == pytest.approx((10 * 100 + 30 * 200) / 40)

    def test_buy_and_sell_on_same_day_become_two_trades(self):
        trades = combine_and_sort([
            _txn("01-01-2024", "AAPL", 10, 100.0, "BUY"),
            _txn("01-01-2024", "AAPL", 3, 110.0, "SELL"),
        ])

        # BUY is sorted before SELL on the same day.
        assert [t.action for t in trades] == ["BUY", "SELL"]
        assert trades[0].quantity == 10
        assert trades[1].quantity == 3

    def test_trades_are_sorted_chronologically(self):
        trades = combine_and_sort([
            _txn("10-03-2024", "AAPL", 1, 10.0, "BUY"),
            _txn("01-01-2024", "MSFT", 2, 20.0, "BUY"),
            _txn("05-02-2024", "AAPL", 3, 30.0, "BUY"),
        ])

        dates = [t.date for t in trades]
        assert dates == sorted(dates)

    def test_buys_precede_sells_on_same_date_across_tickers(self):
        # combine_and_sort sorts by (date, action_name). "BUY" < "SELL"
        # lexicographically, so all buys for the day come first.
        trades = combine_and_sort([
            _txn("01-01-2024", "AAPL", 5, 100.0, "SELL"),
            _txn("01-01-2024", "MSFT", 2, 50.0, "BUY"),
        ])

        same_day = [t for t in trades if t.date == datetime(2024, 1, 1)]
        assert [t.action for t in same_day] == ["BUY", "SELL"]

    def test_multiple_tickers_remain_separate(self):
        trades = combine_and_sort([
            _txn("01-01-2024", "AAPL", 1, 10.0, "BUY"),
            _txn("01-01-2024", "MSFT", 2, 20.0, "BUY"),
        ])

        tickers = {t.ticker for t in trades}
        assert tickers == {"AAPL", "MSFT"}

    def test_unknown_action_raises(self):
        # combine_and_sort previously used a bare ``assert`` for this
        # invariant; the contract is now a load-bearing
        # ``InvariantError`` so it survives ``python -O``.
        from investing.errors import InvariantError
        with pytest.raises(InvariantError):
            combine_and_sort([_txn("01-01-2024", "AAPL", 1, 1.0, "HOLD")])

    def test_actions_constant_is_well_formed(self):
        # combine_and_sort relies on this; an explicit check guards against
        # accidental edits that would silently break the sort order.
        assert _trades.ACTIONS == ["BUY", "SELL"]
