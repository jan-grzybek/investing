"""Tests for the NumPy-only PCHIP interpolator."""

from __future__ import annotations

import numpy as np
import pytest

from investing.pchip import Pchip


class TestPchip:
    def test_linear_segment_matches_endpoints(self):
        x = np.array([0.0, 1.0, 2.0])
        y = np.array([0.0, 1.0, 2.0])
        interp = Pchip(x, y)
        assert interp(np.array([0.5, 1.5])) == pytest.approx([0.5, 1.5])

    def test_monotone_data_stays_monotone(self):
        x = np.array([0.0, 1.0, 2.0, 3.0])
        y = np.array([1.0, 2.0, 2.5, 4.0])
        interp = Pchip(x, y)
        queries = np.linspace(0.0, 3.0, 25)
        values = interp(queries)
        assert np.all(np.diff(values) >= -1e-12)

    def test_preserves_knot_values(self):
        x = np.array([0.0, 1.0, 2.0])
        y = np.array([10.0, 15.0, 12.0])
        interp = Pchip(x, y)
        assert interp(x) == pytest.approx(y)

    def test_two_point_segment_is_linear(self):
        x = np.array([0.0, 1.0])
        y = np.array([2.0, 6.0])
        interp = Pchip(x, y)
        assert interp(np.array([0.25, 0.75])) == pytest.approx([3.0, 5.0])
