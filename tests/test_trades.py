"""Tests for the trade-event categorisation and the activity combiner.

The "Activity" section on the webpage is built from per-ticker
events that ``Holding`` tags with one of four semantic categories
(OPEN/INCREASE/DECREASE/CLOSE), then folded by
``_combine_trade_events`` into one entry per window. These tests pin
both halves:

* the combiner's grouping rule (every fill within ``window_days`` of
  the first event in the group, in either direction) and how a group
  is reduced to its net effect on the position;
* ``Holding.buy`` / ``Holding.sell`` correctly tagging each transaction
  as the position quantity transitions across the 0 boundary, plus the
  ``trade_events`` helper that combines and decorates rows for the
  renderer over the full ownership history.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from investing.holdings import Holding
from investing.performance import get_holdings
from investing.trades import Trade, _combine_trade_events

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
        # The run started from nothing held, so it reads as an
        # opening even though a later INCREASE piled on within the
        # same window.
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
        # The run ended with nothing held, so it reads as a closing
        # even though a partial DECREASE preceded the final sale.
        assert burst["category"] == "CLOSE"
        # VWAP: (4*200 + 6*195) / 10 = 197.0
        assert burst["price"] == pytest.approx(197.0)

    def test_two_buys_outside_window_do_not_merge(self):
        events = [
            _ev(datetime(2024, 1, 1), 100.0, 10, "OPEN"),
            _ev(datetime(2024, 2, 5), 110.0, 5, "INCREASE"),
        ]
        # 35-day gap -- past the 30-day window used by this test
        # (production uses 90; the algorithm is parametric over the
        # window size). Two separate rows.
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 2
        assert out[0]["category"] == "OPEN"
        assert out[1]["category"] == "INCREASE"

    def test_window_boundary_is_inclusive(self):
        # Anchoring at the FIRST event, a gap equal to ``window_days``
        # still counts as "within the rolling window". Anything strictly
        # larger starts a new burst.
        events_in = [
            _ev(datetime(2024, 1, 1), 100.0, 1, "OPEN"),
            _ev(datetime(2024, 1, 31), 100.0, 1, "INCREASE"),
        ]
        events_out = [
            _ev(datetime(2024, 1, 1), 100.0, 1, "OPEN"),
            _ev(datetime(2024, 2, 1), 100.0, 1, "INCREASE"),
        ]
        assert len(_combine_trade_events(events_in, window_days=30)) == 1
        assert len(_combine_trade_events(events_out, window_days=30)) == 2

    def test_three_buys_within_window_collapse_to_one_opening(self):
        # Three small fills landing close together over ~3 weeks become
        # a single "Initiated" row. Verifies that the group keeps
        # absorbing subsequent same-action events as long as each is
        # within the window of the GROUP's first event.
        events = [
            _ev(datetime(2024, 3, 1), 100.0, 2, "OPEN"),
            _ev(datetime(2024, 3, 10), 105.0, 3, "INCREASE"),
            _ev(datetime(2024, 3, 20), 110.0, 5, "INCREASE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        burst = out[0]
        assert burst["category"] == "OPEN"
        assert burst["start_date"] == datetime(2024, 3, 1)
        assert burst["end_date"] == datetime(2024, 3, 20)
        assert burst["price"] == pytest.approx((2 * 100 + 3 * 105 + 5 * 110) / 10)

    def test_window_is_anchored_on_first_event_not_last(self):
        # Three events spaced 25 days apart: t1, t1+25, t1+50. The
        # last is 50 days from the first, well beyond the 30-day window
        # this test parametrises with, so it must NOT be absorbed into
        # the leading burst -- otherwise the rolling-window contract
        # is violated (we'd produce a burst spanning 50 days against a
        # 30-day budget).
        events = [
            _ev(datetime(2024, 1, 1), 100.0, 1, "OPEN"),
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


def _fill(date, price, quantity, category, pre_quantity):
    """An event with an explicit ``pre_quantity``.

    Netting is about what a run of fills did to the holding going in,
    so these cases have to say how large that holding was. ``_ev``
    defaults it to zero, which suits the grouping tests above and
    would make every percentage here undefined.
    """
    return _ev(date, price, quantity, category, pre_quantity=pre_quantity)


class TestCombineNetsAcrossDirections:
    """A change of direction does not end an entry; the window does.

    The reader's question is what the position did around a given
    time, and a sale followed by a purchase a day later answers it
    with one number, not two.
    """

    def test_trim_then_larger_add_reads_as_one_net_increase(self):
        events = [
            _fill(datetime(2024, 6, 1), 110.0, 250, "DECREASE", 1000),
            _fill(datetime(2024, 6, 2), 111.0, 260, "INCREASE", 750),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        entry = out[0]
        assert entry["category"] == "INCREASE"
        # Net +10 on the 1,000 held before the first fill -- not the
        # +35% the purchase alone was against the trimmed holding.
        assert entry["delta_pct"] == pytest.approx(1.0)
        # The price of a net purchase is what the purchases cost; the
        # sale inside the window does not pull it down.
        assert entry["price"] == pytest.approx(111.0)
        assert entry["start_date"] == datetime(2024, 6, 1)
        assert entry["end_date"] == datetime(2024, 6, 2)

    def test_add_then_larger_trim_reads_as_one_net_decrease(self):
        events = [
            _fill(datetime(2024, 6, 1), 50.0, 100, "INCREASE", 1000),
            _fill(datetime(2024, 6, 9), 60.0, 300, "DECREASE", 1100),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        entry = out[0]
        assert entry["category"] == "DECREASE"
        # Net -200 on the 1,000 held going in.
        assert entry["delta_pct"] == pytest.approx(20.0)
        # ... at what the sales fetched.
        assert entry["price"] == pytest.approx(60.0)

    def test_trim_fully_bought_back_leaves_no_entry(self):
        # The holding ends the window exactly where it started, so
        # there is no change to report.
        events = [
            _fill(datetime(2024, 6, 1), 110.0, 200, "DECREASE", 1000),
            _fill(datetime(2024, 6, 2), 111.0, 200, "INCREASE", 800),
        ]
        assert _combine_trade_events(events, window_days=30) == []

    def test_opening_absorbs_a_trim_and_a_re_add_inside_its_window(self):
        # The position came into existence in this window; a trim and
        # a top-up a month later are part of the same act of building
        # it, so the whole run reads "Initiated".
        events = [
            _fill(datetime(2024, 5, 3), 100.0, 1000, "OPEN", 0),
            _fill(datetime(2024, 6, 1), 110.0, 250, "DECREASE", 1000),
            _fill(datetime(2024, 6, 2), 111.0, 260, "INCREASE", 750),
        ]
        out = _combine_trade_events(events, window_days=90)
        assert len(out) == 1
        entry = out[0]
        assert entry["category"] == "OPEN"
        assert entry["delta_pct"] is None
        assert entry["price"] == pytest.approx((1000 * 100.0 + 260 * 111.0) / 1260)
        assert entry["start_date"] == datetime(2024, 5, 3)
        assert entry["end_date"] == datetime(2024, 6, 2)

    def test_exit_absorbs_an_earlier_add_inside_its_window(self):
        events = [
            _fill(datetime(2024, 6, 1), 50.0, 100, "INCREASE", 1000),
            _fill(datetime(2024, 6, 9), 55.0, 1100, "CLOSE", 1100),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        entry = out[0]
        assert entry["category"] == "CLOSE"
        assert entry["delta_pct"] is None
        assert entry["price"] == pytest.approx(55.0)

    def test_round_trip_inside_one_window_keeps_both_legs(self):
        # Opened and fully closed within the window nets to nothing,
        # but it is a whole position with a result of its own in the
        # closed-positions table. It has to stay visible here.
        events = [
            _ev(datetime(2024, 4, 1), 100.0, 10, "OPEN"),
            _ev(datetime(2024, 4, 5), 110.0, 10, "CLOSE"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert [b["category"] for b in out] == ["OPEN", "CLOSE"]

    def test_interleaved_round_trip_still_shows_two_legs(self):
        # Buys and sells alternating between the opening and the exit
        # collapse into one row per direction, each at its own average
        # and spanning its own fills.
        events = [
            _fill(datetime(2024, 4, 1), 100.0, 10, "OPEN", 0),
            _fill(datetime(2024, 4, 2), 104.0, 4, "DECREASE", 10),
            _fill(datetime(2024, 4, 3), 102.0, 6, "INCREASE", 6),
            _fill(datetime(2024, 4, 5), 110.0, 12, "CLOSE", 12),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert [b["category"] for b in out] == ["OPEN", "CLOSE"]
        bought, sold = out
        assert bought["price"] == pytest.approx((10 * 100.0 + 6 * 102.0) / 16)
        assert (bought["start_date"], bought["end_date"]) == (
            datetime(2024, 4, 1),
            datetime(2024, 4, 3),
        )
        assert sold["price"] == pytest.approx((4 * 104.0 + 12 * 110.0) / 16)
        assert (sold["start_date"], sold["end_date"]) == (
            datetime(2024, 4, 2),
            datetime(2024, 4, 5),
        )

    def test_exit_and_re_entry_inside_one_window_reads_as_the_net_change(self):
        # Selling everything and buying back the next day is, to the
        # reader, a position that grew by 5%. The holdings table makes
        # the same call: it reports a round trip this quick as
        # uninterrupted ownership.
        events = [
            _fill(datetime(2024, 1, 5), 100.0, 1000, "CLOSE", 1000),
            _fill(datetime(2024, 1, 6), 101.0, 1050, "OPEN", 0),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        entry = out[0]
        assert entry["category"] == "INCREASE"
        assert entry["delta_pct"] == pytest.approx(5.0)
        assert entry["price"] == pytest.approx(101.0)

    def test_open_close_reopen_inside_one_window_is_one_opening(self):
        # Nothing held going in, something held coming out: one
        # "Initiated", at what the purchases cost. The exit in between
        # does not get a row and does not move the price.
        events = [
            _ev(datetime(2024, 1, 1), 100.0, 5, "OPEN"),
            _ev(datetime(2024, 1, 5), 130.0, 5, "CLOSE"),
            _ev(datetime(2024, 1, 8), 120.0, 5, "OPEN"),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        assert out[0]["category"] == "OPEN"
        assert out[0]["price"] == pytest.approx(110.0)
        assert out[0]["start_date"] == datetime(2024, 1, 1)
        assert out[0]["end_date"] == datetime(2024, 1, 8)

    def test_opposite_fill_outside_the_window_starts_its_own_entry(self):
        # Netting is bounded by the window like everything else: an
        # add and a trim five weeks apart, against a 30-day window,
        # are two decisions.
        events = [
            _fill(datetime(2024, 1, 1), 50.0, 100, "INCREASE", 1000),
            _fill(datetime(2024, 2, 5), 60.0, 100, "DECREASE", 1100),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert [b["category"] for b in out] == ["INCREASE", "DECREASE"]

    def test_window_stays_anchored_on_the_first_fill_across_directions(self):
        # Same contract as the same-direction case above: the third
        # fill is 50 days from the first, so it cannot ride on the
        # second one's proximity into the first entry.
        events = [
            _fill(datetime(2024, 1, 1), 50.0, 100, "INCREASE", 1000),
            _fill(datetime(2024, 1, 26), 60.0, 40, "DECREASE", 1100),
            _fill(datetime(2024, 2, 20), 55.0, 30, "INCREASE", 1060),
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 2
        assert out[0]["delta_pct"] == pytest.approx(6.0)
        assert out[0]["end_date"] == datetime(2024, 1, 26)
        assert out[1]["start_date"] == datetime(2024, 2, 20)


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
        events = [
            {
                "date": datetime(2024, 2, 1),
                "price": 50.0,
                "quantity": 1000,
                "category": "INCREASE",
                "pre_quantity": 1000,
            }
        ]
        out = _combine_trade_events(events)
        assert out[0]["delta_pct"] == pytest.approx(100.0)

    def test_decrease_delta_pct_is_qty_over_pre_quantity(self):
        # 1000 shares held, 500 sold -> 50%.
        events = [
            {
                "date": datetime(2024, 2, 1),
                "price": 50.0,
                "quantity": 500,
                "category": "DECREASE",
                "pre_quantity": 1000,
            }
        ]
        out = _combine_trade_events(events)
        assert out[0]["delta_pct"] == pytest.approx(50.0)

    def test_increase_burst_sums_quantities_over_first_pre_quantity(self):
        # Three BUYs within a 30-day window (test parameter -- the
        # algorithm itself is parametric and prod uses 90) starting
        # from a 1000-share
        # holding. The denominator is the position right before the
        # FIRST event in the burst -- so the percentage answers "what
        # fraction did this whole burst add to what we had going in?".
        events = [
            {
                "date": datetime(2024, 3, 1),
                "price": 100.0,
                "quantity": 100,
                "category": "INCREASE",
                "pre_quantity": 1000,
            },
            {
                "date": datetime(2024, 3, 10),
                "price": 105.0,
                "quantity": 200,
                "category": "INCREASE",
                "pre_quantity": 1100,
            },
            {
                "date": datetime(2024, 3, 20),
                "price": 110.0,
                "quantity": 300,
                "category": "INCREASE",
                "pre_quantity": 1300,
            },
        ]
        out = _combine_trade_events(events, window_days=30)
        assert len(out) == 1
        # (100 + 200 + 300) / 1000 -> 60.0
        assert out[0]["delta_pct"] == pytest.approx(60.0)

    def test_decrease_burst_uses_first_pre_quantity(self):
        # 1000 held, then SELL 200, SELL 200 within the rolling window
        # -- both DECREASE (position never hits 0). Burst delta is
        # 400 / 1000 = 40%.
        events = [
            {
                "date": datetime(2024, 4, 1),
                "price": 100.0,
                "quantity": 200,
                "category": "DECREASE",
                "pre_quantity": 1000,
            },
            {
                "date": datetime(2024, 4, 10),
                "price": 95.0,
                "quantity": 200,
                "category": "DECREASE",
                "pre_quantity": 800,
            },
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
            {
                "date": datetime(2024, 5, 1),
                "price": 100.0,
                "quantity": 400,
                "category": "DECREASE",
                "pre_quantity": 1000,
            },
            {
                "date": datetime(2024, 5, 10),
                "price": 90.0,
                "quantity": 600,
                "category": "CLOSE",
                "pre_quantity": 600,
            },
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
            _ev(datetime(2024, 1, 1), 100.0, 10, "OPEN"),
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
        currency="USD",
        exchange="NMS",
        symbol="AAPL",
        long_name="Apple Inc.",
        price=200.0,
    )
    patch_yf_ticker({"AAPL": mock})
    return mock


def _trade(date, qty, price, action):
    return Trade(
        date=date,
        ticker="AAPL",
        quantity=qty,
        price=price,
        action=action,
    )


class TestHoldingCategorisesTrades:
    def test_first_buy_is_open(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        assert len(h._trade_events) == 1
        assert h._trade_events[0]["category"] == "OPEN"

    def test_second_buy_is_increase(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 2, 1), 5, 110.0, "BUY"))
        assert [e["category"] for e in h._trade_events] == [
            "OPEN",
            "INCREASE",
        ]

    def test_partial_sell_is_decrease(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 3, 1), 4, 120.0, "SELL"))
        assert h._trade_events[-1]["category"] == "DECREASE"

    def test_full_sell_is_close(self, stub_exchange_rate, fake_apple):
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 3, 1), 10, 120.0, "SELL"))
        assert h._trade_events[-1]["category"] == "CLOSE"

    def test_buy_records_pre_quantity_zero_for_open(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # OPEN trades have no prior position; the pre_quantity field
        # must be 0 so the combiner short-circuits and emits
        # ``delta_pct=None`` (rather than dividing by zero).
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        assert h._trade_events[0]["pre_quantity"] == 0

    def test_subsequent_buy_records_pre_quantity_of_prior_holding(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # After a 10-share OPEN, a follow-up BUY's pre_quantity must
        # reflect the holding right before the new trade (10 here),
        # not the post-trade total. That's the denominator the
        # combiner needs for the "+X%" readout.
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 2, 1), 5, 110.0, "BUY"))
        assert h._trade_events[-1]["pre_quantity"] == 10

    def test_sell_records_pre_quantity_of_prior_holding(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # A partial SELL exposes the holding right before the sell so
        # "X% decrease" is denominated against what we were holding.
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 3, 1), 4, 120.0, "SELL"))
        assert h._trade_events[-1]["pre_quantity"] == 10

    def test_reopening_after_close_is_open_again(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # Closing then re-buying must be *recorded* as a fresh
        # "Opening", not an "Increase": the raw events are what the
        # combiner reads its boundaries from, so they have to say what
        # each fill did to the position. Whether the page then shows
        # the three as one entry is the combiner's call, not this
        # bookkeeping's.
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 10, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 2, 1), 10, 110.0, "SELL"))
        h.buy(_trade(datetime(2024, 3, 1), 5, 120.0, "BUY"))
        cats = [e["category"] for e in h._trade_events]
        assert cats == ["OPEN", "CLOSE", "OPEN"]


class TestHoldingTradeEventsDecoration:
    def test_attaches_ticker_name_currency(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        h = Holding("AAPL", fx=stub_exchange_rate)
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

    def test_returns_all_bursts_regardless_of_age(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # The trades section is a complete activity log -- every
        # burst this holding has ever recorded must come through,
        # even ones from years before "today". The reader can still
        # focus on recent activity via the rendered table's
        # sortable date column.
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2018, 1, 1), 10, 90.0, "BUY"))
        h.sell(_trade(datetime(2018, 6, 1), 10, 100.0, "SELL"))
        h.buy(_trade(datetime(2025, 1, 1), 5, 150.0, "BUY"))
        events = h.trade_events()
        assert len(events) == 3
        # Chronological order is preserved from the combiner.
        assert [e["category"] for e in events] == ["OPEN", "CLOSE", "OPEN"]
        assert [e["start_date"] for e in events] == [
            datetime(2018, 1, 1),
            datetime(2018, 6, 1),
            datetime(2025, 1, 1),
        ]

    def test_combines_within_holding(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # Per-ticker combining flows through ``trade_events``: two
        # BUYs nine days apart should surface as a single OPENING row
        # with a volume-weighted price.
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 6, 1), 2, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 6, 10), 8, 110.0, "BUY"))
        events = h.trade_events()
        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "OPEN"
        assert ev["price"] == pytest.approx((2 * 100 + 8 * 110) / 10)
        assert ev["start_date"] == datetime(2024, 6, 1)
        assert ev["end_date"] == datetime(2024, 6, 10)

    def test_trim_and_re_add_net_out_through_trade_events(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # The same netting, driven through the real bookkeeping: the
        # categories and ``pre_quantity`` values come from ``buy`` /
        # ``sell`` rather than being written out by hand. A trim and a
        # slightly larger re-add a day apart, long after the opening,
        # surface as one small net increase.
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 1000, 100.0, "BUY"))
        h.sell(_trade(datetime(2024, 6, 1), 250, 110.0, "SELL"))
        h.buy(_trade(datetime(2024, 6, 2), 260, 111.0, "BUY"))
        events = h.trade_events()
        assert [e["category"] for e in events] == ["OPEN", "INCREASE"]
        assert events[1]["delta_pct"] == pytest.approx(1.0)
        assert events[1]["price"] == pytest.approx(111.0)

    def test_delta_pct_flows_through_trade_events(
        self,
        stub_exchange_rate,
        fake_apple,
    ):
        # OPEN the position with 1,000 shares, then INCREASE by
        # another 1,000 inside a fresh burst (well outside the
        # rolling window from the OPEN). The INCREASE row
        # should expose ``delta_pct = 100`` so the badge renders as
        # "Increased by 100%".
        h = Holding("AAPL", fx=stub_exchange_rate)
        h.buy(_trade(datetime(2024, 1, 1), 1000, 100.0, "BUY"))
        h.buy(_trade(datetime(2024, 6, 1), 1000, 110.0, "BUY"))
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
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        freeze_today,
    ):
        # Two tickers with trades interleaved in time. After
        # processing, the "trades" list must contain bursts from both
        # tickers, sorted newest-first by end_date so the page reads
        # as a single chronological activity log.
        freeze_today(datetime(2024, 12, 1))
        patch_yf_ticker(
            {
                "AAA": make_ticker_mock(
                    currency="USD",
                    exchange="NMS",
                    symbol="AAA",
                    long_name="Alpha Inc.",
                ),
                "BBB": make_ticker_mock(
                    currency="EUR",
                    exchange="DUS",
                    symbol="BBB",
                    long_name="Beta GmbH",
                ),
            }
        )
        transactions = [
            {
                "date": "01-02-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 100.0,
                "action": "BUY",
            },
            {
                "date": "15-05-2024",
                "ticker": "BBB",
                "quantity": 4,
                "price_per_share": 50.0,
                "action": "BUY",
            },
            {
                "date": "01-08-2024",
                "ticker": "AAA",
                "quantity": 10,
                "price_per_share": 120.0,
                "action": "SELL",
            },
        ]
        holdings = get_holdings(transactions, fx=stub_exchange_rate)
        trades = holdings["trades"]
        assert len(trades) == 3
        # Newest end_date first.
        end_dates = [t["end_date"] for t in trades]
        assert end_dates == sorted(end_dates, reverse=True)
        # Both tickers appear in the log.
        tickers = {t["ticker"] for t in trades}
        assert tickers == {"NMS:AAA", "DUS:BBB"}

    def test_returns_empty_trades_list_for_empty_transactions(
        self,
        stub_exchange_rate,
    ):
        holdings = get_holdings([], fx=stub_exchange_rate)
        assert holdings["trades"] == []
