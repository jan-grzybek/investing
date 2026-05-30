"""Tests for the ``Holding`` class.

These tests stub ``yf.Ticker`` so no network is hit, and pin the exchange
rate at 1.0 so we only have to reason about share counts and prices.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import update
from update import Holding, Trade, DAYS_YEAR


def _date_key(d: datetime) -> str:
    """yfinance stringifies timestamps as 'YYYY-MM-DD HH:MM:SS[+TZ]'.
    Our code parses only the first whitespace-separated token, so any
    string starting with the ISO date works.
    """
    return d.strftime("%Y-%m-%d 00:00:00")


def _make_ticker(
    *,
    price: float = 100.0,
    currency: str = "USD",
    splits=None,
    dividends=None,
    long_name: str = "Test Co.",
    symbol: str = "TST",
    exchange: str = "NMS",
):
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.get_info.return_value = {
        "currency": currency,
        "exchange": exchange,
        "symbol": symbol,
        "longName": long_name,
        "regularMarketPrice": price,
    }
    mock.splits = splits or {}
    mock.get_dividends.return_value = dividends or {}
    return mock


@pytest.fixture
def install_ticker(monkeypatch):
    def _install(ticker_mock):
        monkeypatch.setattr(
            update.yf,
            "Ticker",
            lambda symbol: ticker_mock,  # noqa: ARG005
        )
        return ticker_mock

    return _install


class TestSplitsAndDividendsBootstrap:
    def test_splits_are_accumulated_in_reverse(self, install_ticker):
        # Three splits: 2:1, then 3:1, then 5:1. The earliest split is
        # multiplied by all subsequent splits (2 * 3 * 5 = 30).
        ticker = install_ticker(_make_ticker(splits={
            _date_key(datetime(2020, 1, 1)): 2.0,
            _date_key(datetime(2021, 1, 1)): 3.0,
            _date_key(datetime(2022, 1, 1)): 5.0,
        }))
        holding = Holding("TST")

        assert ticker.get_info.called
        # Raw splits (not cumulative).
        assert [s["split"] for s in holding._splits] == [2.0, 3.0, 5.0]

    def test_dividends_are_adjusted_for_later_splits(self, install_ticker):
        # A dividend paid before the 2:1 split should be doubled to express
        # it in post-split share units.
        install_ticker(_make_ticker(
            splits={_date_key(datetime(2022, 1, 1)): 2.0},
            dividends={
                _date_key(datetime(2021, 6, 1)): 1.00,
                _date_key(datetime(2023, 6, 1)): 1.00,
            },
        ))
        holding = Holding("TST")

        by_date = {d["date"]: d["dividend"] for d in holding._dividends}
        assert by_date[datetime(2021, 6, 1)] == pytest.approx(2.00)
        assert by_date[datetime(2023, 6, 1)] == pytest.approx(1.00)


class TestBuy:
    def test_first_buy_opens_a_position_and_period(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))

        assert len(holding._positions) == 1
        assert holding._positions[-1]["quantity"] == 10
        assert holding._periods == [{"start": datetime(2024, 1, 1), "end": None}]
        assert holding._inflows == [
            {"date": datetime(2024, 1, 1), "value": 10 * 50.0}
        ]

    def test_same_day_buy_aggregates_quantity(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 5, 60.0, "BUY"))

        assert len(holding._positions) == 1
        assert holding._positions[-1]["quantity"] == 15
        # Two separate inflows recorded but only one position row.
        assert len(holding._inflows) == 2

    def test_buy_after_split_scales_existing_quantity(self, install_ticker, stub_exchange_rate):
        # 4:1 split between Jan and Mar. 10 shares held -> 40 shares before
        # the next buy of 5 -> position becomes 45.
        install_ticker(_make_ticker(
            price=100.0,
            splits={_date_key(datetime(2024, 2, 1)): 4.0},
        ))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.buy(Trade(datetime(2024, 3, 1), "TST", 5, 25.0, "BUY"))

        assert holding._positions[-1]["quantity"] == 45


class TestSell:
    def test_partial_sell_keeps_period_open(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 1), "TST", 4, 60.0, "SELL"))

        assert holding._positions[-1]["quantity"] == 6
        assert holding._periods[-1]["end"] is None
        assert holding._outflows == [
            {"date": datetime(2024, 6, 1), "value": 4 * 60.0}
        ]

    def test_full_sell_closes_the_period(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 1), "TST", 10, 60.0, "SELL"))

        assert holding._positions[-1]["quantity"] == 0
        assert holding._periods[-1]["end"] == datetime(2024, 6, 1)

    def test_rebuy_after_full_sell_starts_new_period(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 1), "TST", 10, 60.0, "SELL"))
        holding.buy(Trade(datetime(2024, 9, 1), "TST", 7, 55.0, "BUY"))

        assert len(holding._periods) == 2
        assert holding._periods[0] == {
            "start": datetime(2024, 1, 1),
            "end": datetime(2024, 6, 1),
        }
        assert holding._periods[1] == {
            "start": datetime(2024, 9, 1),
            "end": None,
        }


class TestAddDividends:
    def test_dividend_during_open_position_is_recorded_after_tax(
        self, install_ticker, stub_exchange_rate
    ):
        # 1.00 USD/share dividend, 10 shares held, 15% withholding.
        install_ticker(_make_ticker(
            price=100.0,
            dividends={_date_key(datetime(2024, 6, 1)): 1.00},
        ))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))

        outflows = holding._add_dividends()
        # The dividend appended after-tax: 10 * 1.00 * (1 - 0.15).
        div = [o for o in outflows if o["date"] == datetime(2024, 6, 1)]
        assert len(div) == 1
        assert div[0]["value"] == pytest.approx(10 * 1.00 * (1 - update.WITHHOLDING_TAX_RATE))

    def test_dividend_before_first_buy_is_ignored(
        self, install_ticker, stub_exchange_rate
    ):
        install_ticker(_make_ticker(
            price=100.0,
            dividends={_date_key(datetime(2023, 6, 1)): 1.00},
        ))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))

        outflows = holding._add_dividends()
        assert outflows == []  # no sells either


class TestSummary:
    def test_open_position_summary_shape_and_signs(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(
            price=200.0,
            symbol="TST",
            exchange="NMS",
            long_name="Test Co.",
        ))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()

        assert summary["ticker"] == "NMS:TST"
        assert summary["name"] == "Test Co."
        assert summary["is_current"] is True
        assert summary["current_weight%"] is None  # filled in by summarize()
        assert summary["current_value_usd"] == pytest.approx(10 * 200.0)
        assert summary["latest_buy"] == datetime(2024, 1, 1)
        assert summary["latest_sell"] is None
        # Bought at 100, current price 200 -> roughly +100% over a year.
        # Modified Dietz with a single inflow at period start gives exactly 100%.
        assert summary["tsr%"] == pytest.approx(100.0)
        # 100% gain over ~1 year -> CAGR ~100%.
        assert summary["cagr%"] == pytest.approx(100.0, rel=0.02)

    def test_closed_position_summary_uses_period_end_value(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=999.0))  # current price shouldn't matter
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))
        holding.sell(Trade(datetime(2025, 1, 1), "TST", 10, 150.0, "SELL"))

        summary = holding.summary()

        assert summary["is_current"] is False
        # Bought at 100, sold at 150 -> exactly 50% TSR over a year.
        assert summary["tsr%"] == pytest.approx(50.0)
        # 1 year duration -> CAGR ≈ TSR.
        assert summary["cagr%"] == pytest.approx(50.0, rel=0.02)
        assert summary["latest_sell"] == datetime(2025, 1, 1)

    def test_periods_are_returned_in_reverse_chronological_order(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 6, 1))
        install_ticker(_make_ticker(price=120.0))
        holding = Holding("TST")
        # Two distinct ownership windows.
        holding.buy(Trade(datetime(2023, 1, 1), "TST", 10, 100.0, "BUY"))
        holding.sell(Trade(datetime(2023, 7, 1), "TST", 10, 110.0, "SELL"))
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 5, 100.0, "BUY"))

        summary = holding.summary()
        starts = [p["start"] for p in summary["periods"]]
        assert starts == [datetime(2024, 1, 1), datetime(2023, 1, 1)]

    def test_cagr_formula_matches_definition(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Force a known total ownership length so we can compute the
        # expected CAGR from first principles.
        freeze_today(datetime(2026, 1, 1))  # 2 years after purchase
        install_ticker(_make_ticker(price=144.0))
        holding = Holding("TST")
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 1, 100.0, "BUY"))

        summary = holding.summary()
        # TSR = 0.44 -> CAGR = (1.44) ** (DAYS_YEAR / length) - 1
        length = max((datetime(2026, 1, 1) - datetime(2024, 1, 1)).days, 1)
        # ``Holding.summary`` stores unrounded percentages so downstream
        # callers can subtract or compound without leaking rounding
        # error -- the expected matches that full precision.
        expected_cagr = ((1.44) ** (DAYS_YEAR / length) - 1) * 100
        assert summary["cagr%"] == pytest.approx(expected_cagr)
