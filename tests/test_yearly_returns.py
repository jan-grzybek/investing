"""Tests for calendar-year return rollups."""

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
    def test_links_multiplier_at_year_boundaries(self, at_datetime):
        when = datetime(2025, 6, 1)
        total_return = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 12, 1), 110.0),
                _val(when, 121.0),
            ],
            current_value=121.0,
            now=at_datetime(when),
        )
        rows = calc_yearly_returns(total_return, now=at_datetime(when))
        assert len(rows) == 2
        assert rows[0] == {
            "year": 2025,
            "jg%": pytest.approx(10.0),
            "is_ytd": True,
        }
        assert rows[1] == {
            "year": 2024,
            "jg%": pytest.approx(10.0),
            "is_ytd": False,
        }

    def test_first_year_starts_at_portfolio_start(self, at_datetime):
        when = datetime(2024, 12, 31)
        total_return = calc_twr(
            [
                _val(datetime(2024, 6, 1), 100.0),
                _val(datetime(2024, 12, 1), 120.0),
            ],
            current_value=120.0,
            now=at_datetime(when),
        )
        rows = calc_yearly_returns(total_return, now=at_datetime(when))
        assert len(rows) == 1
        assert rows[0]["year"] == 2024
        assert rows[0]["jg%"] == pytest.approx(20.0)
        assert rows[0]["is_ytd"] is False

    def test_flow_is_stripped_within_a_year(self, at_datetime):
        when = datetime(2024, 12, 31)
        total_return = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 110.0, flow=10.0),
                _val(datetime(2024, 12, 1), 132.0),
            ],
            current_value=132.0,
            now=at_datetime(when),
        )
        rows = calc_yearly_returns(total_return, now=at_datetime(when))
        assert len(rows) == 1
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
        assert rows[0]["bench%"] == pytest.approx(4.7619047619, rel=1e-6)
        assert rows[1]["bench%"] == pytest.approx(5.0)

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
        assert rows[0]["bench%"] == pytest.approx(10.0)
        assert rows[1]["bench%"] == pytest.approx(11.1111111111, rel=1e-6)


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
