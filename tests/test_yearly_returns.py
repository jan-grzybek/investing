"""Tests for calendar-year return rollups.

The table shows **complete calendar years only**: the portfolio must
have been open from the year's start (an inception up to a week into
January is tolerated) and a recorded valuation snapshot must close the
year (dated on/after 31 Dec). The inception stub and the still-running
current year are dropped until a closing snapshot makes them whole.
Year-boundary multipliers are interpolated (log-linear) between
snapshots so a snapshot that straddles a boundary is apportioned by
day-count.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from investing.performance import Benchmark, calc_twr, calc_yearly_returns
from tests.test_calc_twr import _val
from tests.test_performance import _make_benchmark_ticker


def _history(*points: tuple[datetime, float]) -> dict:
    return {
        "start_date": points[0][0],
        "history": list(points),
        "twr%": 0.0,
        "cagr%": 0.0,
    }


class TestCalcYearlyReturns:
    def test_complete_years_only_drops_current_year(self, at_datetime):
        # Snapshots land on clean year boundaries. The current year
        # (2025) has no closing snapshot, so only the two finished
        # calendar years appear.
        when = datetime(2025, 6, 1)
        total_return = calc_twr(
            [
                _val(datetime(2023, 1, 1), 100.0),
                _val(datetime(2023, 12, 31), 110.0),
                _val(datetime(2024, 12, 31), 121.0),
            ],
            current_value=121.0,
            now=at_datetime(when),
        )
        rows = calc_yearly_returns(total_return, now=at_datetime(when))
        assert rows == [
            {"year": 2024, "jg%": pytest.approx(10.0), "is_ytd": False},
            {"year": 2023, "jg%": pytest.approx(10.0), "is_ytd": False},
        ]

    def test_apportions_return_across_year_boundary(self, at_datetime):
        # A single snapshot interval (2024-01-01 -> 2025-01-02) doubles
        # the portfolio while straddling the 2024/2025 boundary. The
        # 2024 row must capture the geometric fraction of that move that
        # belongs to 2024 -- NOT 0% (the old last-snapshot-on-or-before
        # rule would have found only the inception value at year-end).
        when = datetime(2025, 1, 2)
        total_return = _history(
            (datetime(2024, 1, 1), 1.0),
            (datetime(2025, 1, 2), 2.0),
        )
        rows = calc_yearly_returns(total_return, now=at_datetime(when))
        span = (date(2025, 1, 2) - date(2024, 1, 1)).days
        frac = (date(2024, 12, 31) - date(2024, 1, 1)).days / span
        expected = (2.0**frac - 1.0) * 100.0
        assert len(rows) == 1
        assert rows[0]["year"] == 2024
        assert rows[0]["jg%"] == pytest.approx(expected)
        # Geometric apportionment sits just under the linear split.
        assert rows[0]["jg%"] == pytest.approx(99.24, abs=0.05)

    def test_waits_for_closing_snapshot(self, at_datetime):
        # We're three days into 2025 but the last *recorded* snapshot is
        # 2024-12-20 -- the year isn't closed yet, so 2024 must not show
        # even though the live mark exists. The synthetic "today" mark
        # the curve carries must not stand in for a recorded year-end.
        when = datetime(2025, 1, 3)
        total_return = _history(
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 20), 1.5),
        )
        rows = calc_yearly_returns(
            total_return,
            now=at_datetime(when),
            last_snapshot=date(2024, 12, 20),
        )
        assert rows == []

        # Once a boundary-crossing snapshot is recorded, 2024 closes and
        # its Dec-31 boundary is interpolated between the two snapshots.
        closed = _history(
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 20), 1.5),
            (datetime(2025, 1, 3), 1.6),
        )
        rows = calc_yearly_returns(
            closed,
            now=at_datetime(when),
            last_snapshot=date(2025, 1, 3),
        )
        assert len(rows) == 1
        assert rows[0]["year"] == 2024
        # Interpolated strictly between the Dec-20 (+50%) and Jan-3
        # (+60%) marks.
        assert 50.0 < rows[0]["jg%"] < 60.0

    def test_inception_within_first_week_counts_as_complete(self, at_datetime):
        # Opened 5 Jan 2024 -> treated as a full 2024 (one-week start
        # tolerance), closed by the 31 Dec snapshot.
        when = datetime(2025, 6, 1)
        total_return = _history(
            (datetime(2024, 1, 5), 1.0),
            (datetime(2024, 12, 31), 1.2),
        )
        rows = calc_yearly_returns(
            total_return,
            now=at_datetime(when),
            last_snapshot=date(2024, 12, 31),
        )
        assert len(rows) == 1
        assert rows[0]["year"] == 2024
        assert rows[0]["jg%"] == pytest.approx(20.0)

    def test_inception_mid_year_is_dropped(self, at_datetime):
        # Opened 1 Feb 2024 -> 2024 is a partial stub (beyond the
        # one-week tolerance) and is dropped entirely.
        when = datetime(2025, 6, 1)
        total_return = _history(
            (datetime(2024, 2, 1), 1.0),
            (datetime(2024, 12, 31), 1.2),
        )
        rows = calc_yearly_returns(
            total_return,
            now=at_datetime(when),
            last_snapshot=date(2024, 12, 31),
        )
        assert rows == []

    def test_flow_is_stripped_within_a_year(self, at_datetime):
        # A mid-year contribution must not distort the calendar-year TWR.
        when = datetime(2024, 6, 1)
        total_return = calc_twr(
            [
                _val(datetime(2023, 1, 1), 100.0),
                _val(datetime(2023, 6, 1), 110.0, flow=10.0),
                _val(datetime(2023, 12, 31), 132.0),
            ],
            current_value=132.0,
            now=at_datetime(when),
        )
        rows = calc_yearly_returns(total_return, now=at_datetime(when))
        assert len(rows) == 1
        assert rows[0]["year"] == 2023
        assert rows[0]["jg%"] == pytest.approx(21.0)

    def test_uses_resampled_benchmark_history_when_provided(self, at_datetime):
        when = datetime(2025, 1, 1)
        total_return = _history(
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 31), 1.10),
            (when, 1.21),
        )
        bench_history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 31), 1.05),
            (when, 1.10),
        ]
        rows = calc_yearly_returns(
            total_return,
            benchmark_history=bench_history,
            now=at_datetime(when),
        )
        assert len(rows) == 1
        assert rows[0]["year"] == 2024
        assert rows[0]["jg%"] == pytest.approx(10.0)
        assert rows[0]["bench%"] == pytest.approx(5.0)

    def test_returns_empty_when_history_missing(self):
        assert calc_yearly_returns({"start_date": datetime(2024, 1, 1), "history": []}) == []

    def test_benchmark_period_uses_daily_prices(
        self,
        patch_yf_ticker,
        stub_exchange_rate,
        at_datetime,
    ):
        when = datetime(2025, 1, 1)
        patch_yf_ticker(
            {
                "VUAA.L": _make_benchmark_ticker(
                    price=220.0,
                    history={
                        datetime(2023, 12, 29): (180.0, 180.0),
                        datetime(2024, 6, 3): (190.0, 190.0),
                        datetime(2024, 12, 31): (200.0, 200.0),
                        when: (210.0, 210.0),
                    },
                ),
            },
        )
        total_return = _history(
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 31), 1.10),
            (when, 1.21),
        )
        benchmark = Benchmark(
            "VUAA.L",
            datetime(2024, 1, 1),
            fx=stub_exchange_rate,
            now=at_datetime(when),
        )
        rows = calc_yearly_returns(
            total_return,
            benchmark=benchmark,
            now=at_datetime(when),
        )
        assert len(rows) == 1
        assert rows[0]["year"] == 2024
        assert rows[0]["jg%"] == pytest.approx(10.0)
        assert rows[0]["bench%"] == pytest.approx(11.1111111111, rel=1e-6)


class TestBenchmarkPeriodReturnPct:
    def test_pins_live_price_for_ytd_end(self, patch_yf_ticker, stub_exchange_rate):
        patch_yf_ticker(
            {
                "VUAA.L": _make_benchmark_ticker(
                    price=220.0,
                    history={
                        datetime(2024, 12, 30): (180.0, 180.0),
                        datetime(2024, 12, 31): (200.0, 200.0),
                    },
                ),
            },
        )
        benchmark = Benchmark(
            "VUAA.L",
            datetime(2024, 1, 1),
            fx=stub_exchange_rate,
            now=lambda: datetime(2025, 6, 19),
        )
        pct = benchmark.period_return_pct(
            date(2024, 12, 31),
            date(2025, 6, 19),
            pin_live_end=True,
        )
        assert pct == pytest.approx(10.0)
