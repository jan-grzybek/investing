"""Numpy-only Piecewise Cubic Hermite Interpolating Polynomial.

Replaces ``scipy.interpolate.PchipInterpolator``, which was the only
reason scipy was on the dependency list. The return-chart renderer
inflates each ``(x, y)`` series to 200 dense samples before turning
them into the SVG polyline -- swapping the implementation for a
small numpy-only function cuts roughly 60 MB off the install footprint
and shaves the cold-start cost of every CI run.

The implementation follows Fritsch & Carlson (1980): per-interval
slopes are clamped to preserve monotonicity, and the harmonic-mean
formula is used to choose the derivative at each interior knot so
local extrema in the data don't introduce spurious overshoot. This
matches scipy's behaviour on monotonic-segment inputs (which all of
the return-series we feed it are).

Public API mirrors the scipy class enough for a drop-in swap:

    interp = Pchip(x, y)
    ys = interp(query_xs)

Inputs are expected to be 1-D float arrays, strictly increasing in
``x``. The class evaluates in O(log n) per query via ``searchsorted``.
"""

from __future__ import annotations

import numpy as np


def _pchip_derivatives(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson monotone derivatives at the interior + endpoint knots.

    Internal knots take the weighted-harmonic-mean of the adjacent
    secant slopes (set to zero when the slopes change sign, which
    enforces monotonicity around local extrema). Endpoints use a
    three-point shape-preserving formula adapted from Fritsch & Butland
    (1984) and used by scipy under the hood.
    """
    n = len(x)
    if n < 2:
        return np.zeros_like(y)
    h = np.diff(x)
    delta = np.diff(y) / h
    d = np.zeros(n, dtype=float)

    if n == 2:
        d[0] = d[1] = delta[0]
        return d

    # Interior knots: harmonic mean of adjacent secant slopes, with a
    # sign-change short-circuit that zeroes the derivative to keep the
    # interpolant monotone across an extremum.
    sign_change = np.sign(delta[:-1]) * np.sign(delta[1:]) <= 0
    with np.errstate(divide="ignore", invalid="ignore"):
        w1 = 2.0 * h[1:] + h[:-1]
        w2 = h[1:] + 2.0 * h[:-1]
        d_interior = (w1 + w2) / (w1 / delta[:-1] + w2 / delta[1:])
    d_interior = np.where(sign_change, 0.0, d_interior)
    d[1:-1] = d_interior

    # Endpoints (shape-preserving three-point formula).
    d[0] = _edge_derivative(h[0], h[1], delta[0], delta[1])
    d[-1] = _edge_derivative(h[-1], h[-2], delta[-1], delta[-2])
    return d


def _edge_derivative(h0: float, h1: float, d0: float, d1: float) -> float:
    """Three-point shape-preserving derivative for an interval endpoint."""
    d = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if np.sign(d) != np.sign(d0):
        return 0.0
    if np.sign(d0) != np.sign(d1) and abs(d) > abs(3.0 * d0):
        return 3.0 * d0
    return float(d)


class Pchip:
    """Drop-in numpy-only replacement for ``scipy.interpolate.PchipInterpolator``."""

    def __init__(
        self,
        x: np.ndarray | list[float],
        y: np.ndarray | list[float],
    ) -> None:
        self._x = np.asarray(x, dtype=float)
        self._y = np.asarray(y, dtype=float)
        if self._x.ndim != 1 or self._y.ndim != 1:
            raise ValueError("Pchip expects 1-D x and y arrays")
        if self._x.shape != self._y.shape:
            raise ValueError("Pchip requires x and y to have matching shape")
        if len(self._x) < 2:
            raise ValueError("Pchip requires at least two knots")
        if not np.all(np.diff(self._x) > 0):
            raise ValueError("Pchip requires strictly increasing x")
        self._d = _pchip_derivatives(self._x, self._y)
        self._h = np.diff(self._x)

    def __call__(self, query: np.ndarray | float) -> np.ndarray | float:
        """Evaluate the interpolant at ``query`` (scalar or array)."""
        q = np.asarray(query, dtype=float)
        flat = q.ravel()
        # ``searchsorted`` puts every query into an interval index in
        # [0, n-2]; values below the first / above the last knot get
        # clamped to the boundary cubic, matching scipy's edge
        # behaviour for the closed-domain case.
        idx = np.searchsorted(self._x, flat, side="right") - 1
        idx = np.clip(idx, 0, len(self._x) - 2)
        x0 = self._x[idx]
        h = self._h[idx]
        t = (flat - x0) / h
        y0 = self._y[idx]
        y1 = self._y[idx + 1]
        d0 = self._d[idx]
        d1 = self._d[idx + 1]
        # Cubic Hermite basis. Numerically stable and matches the
        # closed form scipy emits for the same knots / derivatives.
        h00 = (1.0 + 2.0 * t) * (1.0 - t) ** 2
        h10 = t * (1.0 - t) ** 2
        h01 = t**2 * (3.0 - 2.0 * t)
        h11 = t**2 * (t - 1.0)
        out = h00 * y0 + h10 * h * d0 + h01 * y1 + h11 * h * d1
        return out.reshape(q.shape) if q.shape else float(out[0])
