"""Tests for ``calc_twr`` (time-weighted return)."""
from __future__ import annotations

from datetime import datetime

import pytest

from update import calc_twr, DAYS_YEAR


def _val(date, value, flow=0.0):
    return {"date": date, "value": value, "flow": flow}


class TestCalcTwr:
    def test_no_valuations_returns_zeroed_stub(self):
        result = calc_twr([], current_value=0.0)
        assert result["twr%"] == 0.0
        assert result["cagr%"] == 0.0
        assert result["history"] == []

    def test_single_valuation_returns_no_growth(self, freeze_today):
        freeze_today(datetime(2024, 1, 1))
        result = calc_twr([_val(datetime(2024, 1, 1), 100.0)], current_value=100.0)
        # Today == valuation date, so the current_value branch is skipped.
        assert result["twr%"] == pytest.approx(0.0)
        assert result["cagr%"] == pytest.approx(0.0)
        assert result["history"] == [(datetime(2024, 1, 1), 1.0)]

    def test_two_valuations_no_flow_compounds_correctly(self, freeze_today):
        # 100 -> 110 with no external flows -> +10% TWR.
        freeze_today(datetime(2024, 6, 1))  # same as last valuation, no extension
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 110.0),
            ],
            current_value=110.0,
        )
        assert result["twr%"] == pytest.approx(10.0)
        assert result["history"][-1][1] == pytest.approx(1.10)

    def test_flow_is_stripped_so_only_market_move_counts(self, freeze_today):
        # First period: start = 100, end value = 110 -> +10%.
        # An inflow of 10 occurs at the second valuation, so start of period
        # 2 is 110 + 10 = 120. Second period end value = 132 -> +10%.
        # Compound TWR = 1.10 * 1.10 - 1 = 21%.
        freeze_today(datetime(2024, 12, 1))
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 110.0, flow=10.0),
                _val(datetime(2024, 12, 1), 132.0),
            ],
            current_value=132.0,
        )
        assert result["twr%"] == pytest.approx(21.0)

    def test_current_value_extends_history_when_today_is_after_last(self, freeze_today):
        freeze_today(datetime(2025, 1, 1))
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2024, 6, 1), 110.0),
            ],
            current_value=121.0,  # +10% more between Jun and Jan
        )
        # 1.10 * (121/110) = 1.21
        assert result["history"][-1][0] == datetime(2025, 1, 1)
        assert result["twr%"] == pytest.approx(21.0)

    def test_cagr_uses_calendar_year_basis(self, freeze_today):
        # Pick a 2-year horizon with 21% TWR -> CAGR ≈ 10%.
        freeze_today(datetime(2026, 1, 1))
        result = calc_twr(
            [
                _val(datetime(2024, 1, 1), 100.0),
                _val(datetime(2026, 1, 1), 121.0),
            ],
            current_value=121.0,
        )
        length = (datetime(2026, 1, 1) - datetime(2024, 1, 1)).days
        expected = round(((1.21) ** (DAYS_YEAR / length) - 1) * 100, 1)
        assert result["cagr%"] == pytest.approx(expected)

    def test_history_is_sorted_internally(self, freeze_today):
        freeze_today(datetime(2024, 12, 31))
        # Provide valuations out of order; calc_twr should sort them.
        result = calc_twr(
            [
                _val(datetime(2024, 6, 1), 110.0),
                _val(datetime(2024, 1, 1), 100.0),
            ],
            current_value=110.0,
        )
        dates = [d for d, _ in result["history"]]
        assert dates == sorted(dates)

    def test_start_date_is_first_valuation(self, freeze_today):
        freeze_today(datetime(2024, 6, 1))
        result = calc_twr(
            [_val(datetime(2024, 1, 1), 100.0), _val(datetime(2024, 6, 1), 100.0)],
            current_value=100.0,
        )
        assert result["start_date"] == datetime(2024, 1, 1)
