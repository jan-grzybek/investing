"""Tests for ``calc_twr`` (time-weighted return)."""

from __future__ import annotations

from datetime import datetime
from math import isfinite

import pytest

from investing.holdings import DAYS_YEAR
from investing.performance import calc_twr


def _val(date, value, flow=0.0):
    return {"date": date, "value": value, "flow": flow}


class TestCalcTwr:
    def test_no_valuations_returns_zeroed_stub(self):
        result = calc_twr([], current_value=0.0)
        assert result["twr%"] == 0.0
        assert result["cagr%"] == 0.0
        assert result["history"] == []

    def test_zero_base_period_does_not_divide_by_zero(self, at_datetime):
        # A snapshot whose value + flow nets to zero (portfolio fully
        # withdrawn, then re-funded on a later row) leaves no capital
        # base for the next sub-period. calc_twr must skip that
        # sub-period's multiply rather than raising ZeroDivisionError,
        # and keep every history multiplier finite.
        when = datetime(2024, 12, 31)
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 100.0, flow=-100.0),
                _val(datetime(2024, 9, 1), 50.0, flow=50.0),
            ],
            current_value=60.0,
            now=at_datetime(when),
        )
        assert all(isfinite(m) for _, m in result["history"])
        # Flat through the drained period, then 60/100 on the re-funded
        # leg: 1.0 * 1.0 * 0.6 = 0.6 -> -40%.
        assert result["twr%"] == pytest.approx(-40.0)

    def test_single_valuation_returns_no_growth(self, at_datetime):
        when = datetime(2024, 1, 1)
        result = calc_twr(
            [_val(when, 100.0)],
            current_value=100.0,
            now=at_datetime(when),
        )
        # Today == valuation date, so the current_value branch is skipped.
        assert result["twr%"] == pytest.approx(0.0)
        assert result["cagr%"] == pytest.approx(0.0)
        assert result["history"] == [(when, 1.0)]

    def test_two_valuations_no_flow_compounds_correctly(self, at_datetime):
        when = datetime(2024, 6, 1)
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(when, 110.0),
            ],
            current_value=110.0,
            now=at_datetime(when),
        )
        assert result["twr%"] == pytest.approx(10.0)
        assert result["history"][-1][1] == pytest.approx(1.10)

    def test_flow_is_stripped_so_only_market_move_counts(self, at_datetime):
        when = datetime(2024, 12, 1)
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 110.0, flow=10.0),
                _val(when, 132.0),
            ],
            current_value=132.0,
            now=at_datetime(when),
        )
        assert result["twr%"] == pytest.approx(21.0)

    def test_current_value_extends_history_when_today_is_after_last(self, at_datetime):
        when = datetime(2025, 1, 1)
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 110.0),
            ],
            current_value=121.0,
            now=at_datetime(when),
        )
        assert result["history"][-1][0] == when
        assert result["twr%"] == pytest.approx(21.0)

    def test_cagr_uses_calendar_year_basis(self, at_datetime):
        when = datetime(2026, 1, 1)
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(when, 121.0),
            ],
            current_value=121.0,
            now=at_datetime(when),
        )
        length = (when - datetime(2024, 1, 1)).days
        expected = ((1.21) ** (DAYS_YEAR / length) - 1) * 100
        assert result["cagr%"] == pytest.approx(expected)

    def test_history_is_sorted_internally(self, at_datetime):
        when = datetime(2024, 12, 31)
        result = calc_twr(
            [
                _val(datetime(2024, 6, 1), 110.0),
                _val(datetime(2024, 1, 1), 100.0),
            ],
            current_value=110.0,
            now=at_datetime(when),
        )
        dates = [d for d, _ in result["history"]]
        assert dates == sorted(dates)

    def test_start_date_is_first_valuation(self, at_datetime):
        when = datetime(2024, 6, 1)
        result = calc_twr(
            [_val(datetime(2024, 1, 1), 100.0), _val(when, 100.0)],
            current_value=100.0,
            now=at_datetime(when),
        )
        assert result["start_date"] == datetime(2024, 1, 1)
