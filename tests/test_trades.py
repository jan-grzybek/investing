"""Tests for the trade-event categorisation and burst combiner.

The "Recent trades" section on the webpage is built from per-ticker
events that ``Holding`` tags with one of four semantic categories
(OPEN/INCREASE/DECREASE/CLOSE), then folded by
``_combine_trade_events`` into burst-level rows. These tests pin both
halves:

* the combiner's grouping rules (same-action, within ``window_days``
  from the first event in the group) and category resolution (first
  event for BUY bursts, last event for SELL bursts);
* ``Holding.buy`` / ``Holding.sell`` correctly tagging each transaction
  as the position quantity transitions across the 0 boundary, plus the
  ``trade_events`` helper that combines, filters, and decorates rows
  for the renderer.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import update
from update import (
    Holding,
    Trade,
    _combine_trade_events,
    get_holdings,
)


# ---------------------------------------------------------------------------
# _combine_trade_events
# ---------------------------------------------------------------------------


def _ev(date, price, quantity, category, *, pre_quantity=0):
    """Shape used by ``Holding._trade_events`` and the combiner.

    ``pre_quantity`` defaults to 0 because most grouping-focused
    tests don't care about the percentage readout -- they assert on
    dates, prices, and category resolution. Tests in
    ``TestCombineDeltaPct`` build events directly with realistic
    ``pre_quantity`` values instead of going through this helper.
    """
    return {
        "date": date,
        "price": price,
        "quantity": quantity,
        "category": category,
        "pre_quantity": pre_quantity,
    }


class TestCombineEmpty:
    def test_empty_input_yields_empty_output(self):
        assert _combine_trade_events([]) == []


class TestCombineSingleEvent:
    def test_single_open_event_passes_through(self):
        events = [_ev(datetime(2024, 1, 10), 100.0, 5, "OPEN")]
        out = _combine_trade_events(events)
        assert len(out) == 1
        assert out[0]["category"] == "OPEN"
        assert out[0]["price"] == pytest.approx(100.0)
        assert out[0]["start_date"] == datetime(2024, 1, 10)
        assert out[0]["end_date"] == datetime(2024, 1, 10)

    def test_single_close_event_passes_through(self):
        events = [_ev(datetime(2024, 1, 10), 200.0, 5, "CLOSE")]
        out = _combine_trade_events(events)
        assert len(out) == 1
        assert out[0]["category"] == "CLOSE"


class TestCombineWindow:
    def test_two_buys_within_window_merge_into_one_opening(self):
        events = [
            _ev(datetime(2024, 1, 1), 100.0, 10, "OPEN"),
            _ev(datetime(2024, 1, 20), 110.0, 5, "INCREASE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        burst = out[0]
        # First event decides the BUY-burst category: was the position
        # opened with this run? Yes -> "Opening" even though a later
        # INCREASE piled on within the same window.
        assert burst["category"] == "OPEN"
        # Volume-weighted price: (10*100 + 5*110) / 15 = 103.333...
        assert burst["price"] == pytest.approx((1000 + 550) / 15)
        assert burst["start_date"] == datetime(2024, 1, 1)
        assert burst["end_date"] == datetime(2024, 1, 20)

    def test_two_sells_within_window_merge_into_one_closing(self):
        events = [
            _ev(datetime(2024, 2, 1), 200.0, 4, "DECREASE"),
            _ev(datetime(2024, 2, 20), 195.0, 6, "CLOSE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        burst = out[0]
        # Last event decides the SELL-burst category: did the run end
        # with the position fully closed? Yes -> "Closing", even though
        # a prior partial DECREASE preceded it.
        assert burst["category"] == "CLOSE"
        # VWAP: (4*200 + 6*195) / 10 = 197.0
        assert burst["price"] == pytest.approx(197.0)

    def test_two_buys_outside_window_do_not_merge(self):
        events = [
            _ev(datetime(2024, 1, 1), 100.0, 10, "OPEN"),
            _ev(datetime(2024, 2, 5), 110.0, 5, "INCREASE"),
        ]
        # 35-day gap -- past the 30-day window. Two separate rows.
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 2
        assert out[0]["category"] == "OPEN"
        assert out[1]["category"] == "INCREASE"

    def test_window_boundary_is_inclusive(self):
        # Anchoring at the FIRST event, a gap equal to ``window_days``
        # still counts as "within a rolling month". Anything strictly
        # larger starts a new burst.
        events_in = [
            _ev(datetime(2024, 1, 1), 100.0, 1, "OPEN"),
            _ev(datetime(2024, 1, 31), 100.0, 1, "INCREASE"),
        ]
        events_out = [
            _ev(datetime(2024, 1, 1), 100.0, 1, "OPEN"),
            _ev(datetime(2024, 2, 1), 100.0, 1, "INCREASE"),
        ]
        assert len(_combine_trade_events(events_in,  window_days=30)) == 1
        assert len(_combine_trade_events(events_out, window_days=30)) == 2

    def test_three_buys_within_window_collapse_to_one_opening(self):
        # Three small fills landing close together over ~3 weeks become
        # a single "Opening" row. Verifies that the group keeps absorbing
        # subsequent same-action events as long as each is within the
        # window of the GROUP's first event.
        events = [
            _ev(datetime(2024, 3, 1),  100.0, 2, "OPEN"),
            _ev(datetime(2024, 3, 10), 105.0, 3, "INCREASE"),
            _ev(datetime(2024, 3, 20), 110.0, 5, "INCREASE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        burst = out[0]
        assert burst["category"] == "OPEN"
        assert burst["start_date"] == datetime(2024, 3, 1)
        assert burst["end_date"] == datetime(2024, 3, 20)
        assert burst["price"] == pytest.approx(
            (2 * 100 + 3 * 105 + 5 * 110) / 10
        )

    def test_window_is_anchored_on_first_event_not_last(self):
        # Three events spaced 25 days apart: t1, t1+25, t1+50. The
        # last is 50 days from the first, well beyond a 30-day window,
        # so it must NOT be absorbed into the leading burst -- otherwise
        # the "rolling month" claim in the section caption is violated
        # (we'd produce a burst spanning 50 days).
        events = [
            _ev(datetime(2024, 1, 1),  100.0, 1, "OPEN"),
            _ev(datetime(2024, 1, 26), 100.0, 1, "INCREASE"),
            _ev(datetime(2024, 2, 20), 100.0, 1, "INCREASE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        # First two combine (gap 25 days, anchor at t1 => 25 <= 30).
        # The third event is 50 days from the anchor -- starts a new
        # burst, which itself is just one event.
        assert len(out) == 2
        assert out[0]["start_date"] == datetime(2024, 1, 1)
        assert out[0]["end_date"] == datetime(2024, 1, 26)
        assert out[1]["start_date"] == datetime(2024, 2, 20)


class TestCombineCrossActionSplits:
    def test_buy_then_sell_within_window_are_not_merged(self):
        # Even when the two events sit only days apart, BUY vs SELL
        # never merges -- the category space wouldn't make sense.
        events = [
            _ev(datetime(2024, 4, 1), 100.0, 10, "OPEN"),
            _ev(datetime(2024, 4, 5), 110.0, 10, "CLOSE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 2
        assert out[0]["category"] == "OPEN"
        assert out[1]["category"] == "CLOSE"

    def test_buy_sell_buy_yields_three_rows(self):
        # OPEN -> CLOSE -> OPEN-again pattern: each ownership cycle
        # should surface as its own row, including the second OPEN
        # (a re-entry, not an INCREASE of the first holding).
        events = [
            _ev(datetime(2024, 1, 1),  100.0, 5, "OPEN"),
            _ev(datetime(2024, 1, 5),  110.0, 5, "CLOSE"),
            _ev(datetime(2024, 1, 8),  120.0, 5, "OPEN"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert [b["category"] for b in out] == ["OPEN", "CLOSE", "OPEN"]
        assert [b["start_date"] for b in out] == [
            datetime(2024, 1, 1),
            datetime(2024, 1, 5),
            datetime(2024, 1, 8),
        ]


class TestCombineDeltaPct:
    def test_open_has_no_delta_pct(self):
        events = [_ev(datetime(2024, 1, 1), 100.0, 5, "OPEN")]
        out = _combine_trade_events(events)
        # No prior position to compare to -- "Initiated" already
        # conveys the magnitude (the whole position is new).
        assert out[0]["delta_pct"] is None

    def test_close_has_no_delta_pct(self):
        events = [_ev(datetime(2024, 1, 1), 100.0, 5, "CLOSE")]
        out = _combine_trade_events(events)
        # Position zeroes out -- "Divested" already conveys "100%"
        # implicitly, and rendering it would be redundant.
        assert out[0]["delta_pct"] is None

    def test_increase_delta_pct_is_qty_over_pre_quantity(self):
        # 1000 shares held, then 1000 more bought -> "+100%".
        events = [{
            "date": datetime(2024, 2, 1),
            "price": 50.0,
            "quantity": 1000,
            "category": "INCREASE",
            "pre_quantity": 1000,
        }]
        out = _combine_trade_events(events)
        assert out[0]["delta_pct"] == pytest.approx(100.0)

    def test_decrease_delta_pct_is_qty_over_pre_quantity(self):
        # 1000 shares held, 500 sold -> 50%.
        events = [{
            "date": datetime(2024, 2, 1),
            "price": 50.0,
            "quantity": 500,
            "category": "DECREASE",
            "pre_quantity": 1000,
        }]
        out = _combine_trade_events(events)
        assert out[0]["delta_pct"] == pytest.approx(50.0)

    def test_increase_burst_sums_quantities_over_first_pre_quantity(self):
        # Three BUYs within a 30-day window starting from a 1000-share
        # holding. The denominator is the position right before the
        # FIRST event in the burst -- so the percentage answers "what
        # fraction did this whole burst add to what we had going in?".
        events = [
            {"date": datetime(2024, 3, 1),  "price": 100.0,
             "quantity": 100, "category": "INCREASE", "pre_quantity": 1000},
            {"date": datetime(2024, 3, 10), "price": 105.0,
             "quantity": 200, "category": "INCREASE", "pre_quantity": 1100},
            {"date": datetime(2024, 3, 20), "price": 110.0,
             "quantity": 300, "category": "INCREASE", "pre_quantity": 1300},
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        # (100 + 200 + 300) / 1000 -> 60.0
        assert out[0]["delta_pct"] == pytest.approx(60.0)

    def test_decrease_burst_uses_first_pre_quantity(self):
        # 1000 held, then SELL 200, SELL 200 within a 30-day window
        # -- both DECREASE (position never hits 0). Burst delta is
        # 400 / 1000 = 40%.
        events = [
            {"date": datetime(2024, 4, 1), "price": 100.0,
             "quantity": 200, "category": "DECREASE", "pre_quantity": 1000},
            {"date": datetime(2024, 4, 10), "price": 95.0,
             "quantity": 200, "category": "DECREASE", "pre_quantity": 800},
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        assert out[0]["delta_pct"] == pytest.approx(40.0)

    def test_close_burst_overrides_decrease_and_drops_delta_pct(self):
        # SELL of 400 (DECREASE) then SELL of 600 (CLOSE) over a
        # short window: the burst as a whole closed the position, so
        # the badge becomes "Divested" and the delta_pct field falls
        # away -- "100% Divested" would just be visual noise next to
        # the verb.
        events = [
            {"date": datetime(2024, 5, 1), "price": 100.0,
             "quantity": 400, "category": "DECREASE", "pre_quantity": 1000},
            {"date": datetime(2024, 5, 10), "price": 90.0,
             "quantity": 600, "category": "CLOSE", "pre_quantity": 600},
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        assert out[0]["category"] == "CLOSE"
        assert out[0]["delta_pct"] is None


class TestCombineSorting:
    def test_unsorted_input_is_normalised_before_grouping(self):
        # Defensive sort: the renderer should not need to pre-sort
        # events. Hand them over in reverse-chronological order and
        # confirm the combiner still produces the right grouping and
        # date range.
        events = [
            _ev(datetime(2024, 1, 20), 110.0, 5, "INCREASE"),
            _ev(datetime(2024, 1, 1),  100.0, 10, "OPEN"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        assert out[0]["category"] == "OPEN"
        assert out[0]["start_date"] == datetime(2024, 1, 1)
        assert out[0]["end_date"] == datetime(2024, 1, 20)


# ---------------------------------------------------------------------------
# Holding categorisation + trade_events()
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_apple(patch_yf_ticker, make_ticker_mock):
    """A USD-denominated stub ticker with no splits and no dividends."""
    mock = make_ticker_mock(
        currency="USD", exchange="NMS", symbol="AAPL",
        long_name="Apple Inc.", price=200.0,
    )
    patch_yf_ticker({"AAPL": mock})
    return mock


def _trade(date, qty, price, action):
    return Trade(
        date=date, ticker="AAPL", quantity=qty, price=price, action=action,
    )


class TestHoldingCategorisesTrades:
    def test_first_buy_is_open(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        assert len(h._trade_events) == 1
        assert h._trade_events[0]["category"] == "OPEN"

    def test_second_buy_is_increase(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 2, 1), 5, 110.0, "BUY"))
        assert [e["category"] for e in h._trade_events] == [
            "OPEN", "INCREASE",
        ]

    def test_partial_sell_is_decrease(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 3, 1), 4, 120.0, "SELL"))
        assert h._trade_events[-1]["category"] == "DECREASE"

    def test_full_sell_is_close(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 3, 1), 10, 120.0, "SELL"))
        assert h._trade_events[-1]["category"] == "CLOSE"

    def test_buy_records_pre_quantity_zero_for_open(
        self, stub_exchange_rate, fake_apple,
    ):
        # OPEN trades have no prior position; the pre_quantity field
        # must be 0 so the combiner short-circuits and emits
        # ``delta_pct=None`` (rather than dividing by zero).
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        assert h._trade_events[0]["pre_quantity"] == 0

    def test_subsequent_buy_records_pre_quantity_of_prior_holding(
        self, stub_exchange_rate, fake_apple,
    ):
        # After a 10-share OPEN, a follow-up BUY's pre_quantity must
        # reflect the holding right before the new trade (10 here),
        # not the post-trade total. That's the denominator the
        # combiner needs for the "+X%" readout.
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 2, 1), 5,  110.0, "BUY"))
        assert h._trade_events[-1]["pre_quantity"] == 10

    def test_sell_records_pre_quantity_of_prior_holding(
        self, stub_exchange_rate, fake_apple,
    ):
        # A partial SELL exposes the holding right before the sell so
        # "X% decrease" is denominated against what we were holding.
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 3, 1), 4, 120.0, "SELL"))
        assert h._trade_events[-1]["pre_quantity"] == 10

    def test_reopening_after_close_is_open_again(
        self, stub_exchange_rate, fake_apple,
    ):
        # Closing then re-buying must read as a fresh "Opening", not
        # an "Increase". The page's category column distinguishes
        # entries from add-ons; collapsing them would lie about the
        # nature of the action.
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 2, 1), 10, 110.0, "SELL"))
        h.buy(_trade(datetime(2024, 3, 1), 5, 120.0, "BUY"))
        cats = [e["category"] for e in h._trade_events]
        assert cats == ["OPEN", "CLOSE", "OPEN"]


class TestHoldingTradeEventsDecoration:
    def test_attaches_ticker_name_currency(
        self, stub_exchange_rate, fake_apple, freeze_today,
    ):
        freeze_today(datetime(2024, 6, 1))
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        events = h.trade_events()
        assert len(events) == 1
        ev = events[0]
        assert ev["ticker"] == "NMS:AAPL"
        assert ev["name"] == "Apple Inc."
        assert ev["currency"] == "USD"
        # Combined fields survive.
        assert ev["category"] == "OPEN"
        assert ev["start_date"] == datetime(2024, 1, 1)
        assert ev["end_date"] == datetime(2024, 1, 1)
        assert ev["price"] == pytest.approx(100.0)

    def test_drops_events_older_than_years_back(
        self, stub_exchange_rate, fake_apple, freeze_today,
    ):
        # The default 5-year window means anything ending more than
        # ~5 years before "today" is excluded. Pin today and stage
        # one ancient event + one recent event; only the recent one
        # should come through.
        freeze_today(datetime(2025, 1, 1))
        h = Holding("AAPL")
        h.buy(_trade(datetime(2018, 1, 1), 10, 90.0, "BUY"))   # too old
        h.sell(_trade(datetime(2018, 6, 1), 10, 100.0, "SELL"))  # too old
        h.buy(_trade(datetime(2024, 1, 1), 5, 150.0, "BUY"))   # kept
        events = h.trade_events()
        assert len(events) == 1
        assert events[0]["start_date"] == datetime(2024, 1, 1)
        assert events[0]["category"] == "OPEN"

    def test_combines_within_holding(
        self, stub_exchange_rate, fake_apple, freeze_today,
    ):
        # Per-ticker combining flows through ``trade_events``: two
        # BUYs nine days apart should surface as a single OPENING row
        # with a volume-weighted price.
        freeze_today(datetime(2024, 12, 1))
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 6, 1),  2, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 6, 10), 8, 110.0, "BUY"))
        events = h.trade_events()
        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "OPEN"
        assert ev["price"] == pytest.approx((2 * 100 + 8 * 110) / 10)
        assert ev["start_date"] == datetime(2024, 6, 1)
        assert ev["end_date"] == datetime(2024, 6, 10)

    def test_delta_pct_flows_through_trade_events(
        self, stub_exchange_rate, fake_apple, freeze_today,
    ):
        # OPEN the position with 1,000 shares, then INCREASE by
        # another 1,000 inside a fresh burst (well outside the
        # 30-day rolling window from the OPEN). The INCREASE row
        # should expose ``delta_pct = 100`` so the badge renders as
        # "Increased by 100%".
        freeze_today(datetime(2024, 12, 1))
        h = Holding("AAPL")
        h.buy(_trade(datetime(2024, 1, 1),  1000, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 6, 1),  1000, 110.0, "BUY"))
        events = h.trade_events()
        assert len(events) == 2
        # Newest first inside a single Holding is implementation
        # detail, but the combiner's output preserves chronological
        # order so the OPEN row comes first.
        open_row, inc_row = events[0], events[1]
        assert open_row["category"] == "OPEN"
        assert open_row["delta_pct"] is None
        assert inc_row["category"] == "INCREASE"
        assert inc_row["delta_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# get_holdings: returns a globally-sorted, newest-first trade log
# ---------------------------------------------------------------------------


class TestGetHoldingsTradesKey:
    def test_trades_key_is_globally_sorted_newest_first(
        self, patch_yf_ticker, make_ticker_mock, stub_exchange_rate,
        freeze_today,
    ):
        # Two tickers with trades interleaved in time. After
        # processing, the "trades" list must contain bursts from both
        # tickers, sorted newest-first by end_date so the page reads
        # as a single chronological activity log.
        freeze_today(datetime(2024, 12, 1))
        patch_yf_ticker({
            "AAA": make_ticker_mock(
                currency="USD", exchange="NMS", symbol="AAA",
                long_name="Alpha Inc.",
            ),
            "BBB": make_ticker_mock(
                currency="EUR", exchange="DUS", symbol="BBB",
                long_name="Beta GmbH",
            ),
        })
        transactions = [
            {"date": "01-02-2024", "ticker": "AAA",
             "quantity": 10, "price_per_share": 100.0, "action": "BUY"},
            {"date": "15-05-2024", "ticker": "BBB",
             "quantity": 4,  "price_per_share": 50.0,  "action": "BUY"},
            {"date": "01-08-2024", "ticker": "AAA",
             "quantity": 10, "price_per_share": 120.0, "action": "SELL"},
        ]
        holdings = get_holdings(transactions)
        trades = holdings["trades"]
        assert len(trades) == 3
        # Newest end_date first.
        end_dates = [t["end_date"] for t in trades]
        assert end_dates == sorted(end_dates, reverse=True)
        # Both tickers appear in the log.
        tickers = {t["ticker"] for t in trades}
        assert tickers == {"NMS:AAA", "DUS:BBB"}

    def test_returns_empty_trades_list_for_empty_transactions(
        self, stub_exchange_rate,
    ):
        holdings = get_holdings([])
        assert holdings["trades"] == []
