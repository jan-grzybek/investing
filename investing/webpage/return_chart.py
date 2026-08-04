"""Inline-SVG return chart: two curves, a shaded alpha band, real axes.

The chart used to be two unlabelled polylines on a dashed baseline,
stretched to whatever aspect ratio the viewport gave it, with the
outperformance drawn as a 1.75px vertical bracket near the right
edge. Nothing on it could be read without hovering, and on mobile a
forced 5:3 box distorted the curve non-uniformly.

Three changes fix that, and they are the whole module:

* **Axes.** A labelled y-axis in cumulative percent, year ticks along
  the bottom, a real 0% baseline, and both series labelled at their
  end point. Every value on the chart is now readable without a
  pointer, which also makes it readable on a phone and in print.
* **The alpha is the area, not a bracket.** The gap between the two
  curves is the outperformance, so it is filled. It widens as the
  lead grows, which is the argument the page is making, drawn. Runs
  where the portfolio trails are filled in the negative colour
  instead -- the band is split at every crossing rather than being
  painted one colour throughout.
* **Uniform scaling.** A fixed ``viewBox`` with the default
  ``preserveAspectRatio`` replaces ``preserveAspectRatio="none"``
  plus a CSS ``aspect-ratio``, so the curve keeps its shape at every
  width.

The public entrypoint is :func:`render`. The densely-sampled curve
and the plot geometry are encoded in the ``data-chart`` JSON so the
client-side scrubber can place markers without re-running the
interpolation.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

import numpy as np

from ..formatting import _fmt_pct
from ..pchip import Pchip

# Type alias for ``benchmark -> friendly display name`` resolver
# (a thin function rather than reaching into the renderer's
# ``_BENCHMARK_DISPLAY_NAMES`` map directly, so the chart module
# stays decoupled from the renderer's lookup convention).
BenchmarkLabeller = Callable[[dict], str]

# Chart geometry, in viewBox units. The right inset carries the two
# end-of-period labels, the left carries the y-axis, and the strip
# below the plot carries the year ticks.
#
# Both insets are sized for the *phone* case rather than the desktop
# one. The SVG scales uniformly, so on a 358px-wide screen the whole
# chart is about a third of its desktop size and the axis labels have
# to grow back in viewBox units to stay legible -- roughly 28 units
# against the 15 desktop uses. The insets are what has to hold those
# larger labels: 80 units on the left fits "+40%" at 28, and 114 on
# the right fits "+48.4%" at 26. On desktop the same insets are
# simply generous.
VIEW_W = 1000.0
VIEW_H = 372.0
PLOT_X0 = 80.0
PLOT_X1 = 886.0
PLOT_Y0 = 16.0
PLOT_Y1 = 318.0

# Vertical room one end label needs. Two labels closer than this are
# pushed apart so the "JG +48.4% / S&P +41.7%" stack never collides
# on a window where the two series finish within a point of each
# other.
_LABEL_SLOT = 42.0

# Step ladder for the y-axis, in percentage points. Picking from a
# fixed 1 / 2 / 2.5 / 5 ladder (times a power of ten) is what keeps
# gridlines on numbers a reader can do arithmetic with -- +10%, +20%
# -- rather than on whatever 1/5th of the data range happens to be.
_TICK_STEPS: tuple[float, ...] = (1.0, 2.0, 2.5, 5.0)
_TARGET_TICKS = 5


def _nice_step(span_pct: float) -> float:
    """Smallest ladder step that covers ``span_pct`` in ~5 intervals."""
    if span_pct <= 0:
        return 1.0
    raw = span_pct / _TARGET_TICKS
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for step in _TICK_STEPS:
        if step * magnitude >= raw:
            return step * magnitude
    return 10.0 * magnitude


def _y_domain(min_pct: float, max_pct: float) -> tuple[float, float, float]:
    """Return ``(domain_min, domain_max, step)`` in percentage points.

    Zero is always inside the domain: the baseline is the reference
    every value on the chart is measured against, so a window where
    the portfolio never dipped below its starting value still draws
    the line it is above.
    """
    lo = min(0.0, min_pct)
    hi = max(0.0, max_pct)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    step = _nice_step(hi - lo)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _year_ticks(start: date | datetime, total_days: int) -> list[tuple[int, float]]:
    """Return ``(year, day-offset)`` pairs for every Jan 1 in range.

    The first tick is pinned to the start of the history rather than
    to the following January so a chart always names the year it
    begins in; later ticks are thinned to at most eight labels so a
    twenty-year history doesn't render a solid band of text.

    ``start`` arrives as whatever the history carries -- ``date`` in
    tests, ``datetime`` from the production pipeline -- so it is
    normalised here rather than at every call site.
    """
    start = start.date() if isinstance(start, datetime) else start
    end = start + timedelta(days=total_days)
    ticks: list[tuple[int, float]] = [(start.year, 0.0)]
    for year in range(start.year + 1, end.year + 1):
        boundary = date(year, 1, 1)
        if boundary > end:
            break
        ticks.append((year, float((boundary - start).days)))
    stride = max(1, math.ceil(len(ticks) / 8))
    return ticks[::stride]


def _sign_runs(delta: np.ndarray) -> list[tuple[int, int, bool]]:
    """Split an index range into maximal runs of constant delta sign.

    Returns ``(start, stop, positive)`` slices with ``stop``
    exclusive. Runs overlap by one sample so consecutive band
    polygons meet exactly on the crossing instead of leaving a
    hairline gap where the curves touch.

    Samples where the gap is exactly zero inherit the sign of the run
    they belong to rather than counting as positive. Both series start
    at a multiplier of 1.0 by construction, so a naive ``>= 0`` test
    reads that shared origin as a lead and paints a two-sample green
    sliver at the left edge of a chart that trails for its entire
    life. Zero is not a lead.
    """
    if delta.size == 0:
        return []
    sign = np.sign(delta)
    non_zero = np.nonzero(sign)[0]
    if non_zero.size == 0:
        # The two series never diverge, so there is no band to draw.
        return []
    # Forward-fill each zero with the last non-zero sign before it,
    # then back-fill the leading zeros from the first non-zero one.
    carry = np.where(sign != 0, np.arange(sign.size), 0)
    np.maximum.accumulate(carry, out=carry)
    filled = sign[carry]
    filled[: non_zero[0]] = sign[non_zero[0]]

    positive = filled > 0
    runs: list[tuple[int, int, bool]] = []
    start = 0
    for index in range(1, delta.size):
        if bool(positive[index]) != bool(positive[start]):
            runs.append((start, index, bool(positive[start])))
            start = index - 1
    runs.append((start, delta.size, bool(positive[start])))
    return [run for run in runs if run[1] - run[0] >= 2]


def render(
    total_return: dict,
    benchmarks: list[dict],
    *,
    benchmark_label: BenchmarkLabeller,
) -> str:
    """Render an inline SVG of the portfolio return curve.

    Returns an empty string when the history has fewer than two
    samples, since there is nothing to draw.
    """
    history = total_return.get("history", [])
    if len(history) < 2:
        return ""

    start_date = history[0][0]
    time_x = np.array([int((d - start_date).days) for d, _ in history], dtype=float)
    jg_y = np.array([v for _, v in history], dtype=float)

    series: list[tuple[str, str, np.ndarray]] = [("jg", "Portfolio", jg_y)]
    for benchmark in benchmarks or []:
        bh = benchmark.get("history", [])
        if len(bh) < 2:
            continue
        series.append(
            ("bench", benchmark_label(benchmark), np.array([v for _, v in bh], dtype=float)),
        )

    # The upstream contract is: each series' rightmost sample IS the
    # cumulative return at "now", expressed as a multiplier (1 +
    # twr%/100 for JG, 1 + tsr%/100 for each benchmark). JG gets
    # there by construction in ``calc_twr``; ``Benchmark.summary``
    # pins its chart sample at "now" to the same
    # ``regularMarketPrice / Adj Close[0]`` numerator the TSR is
    # computed from. So the chart's right edge, the scrubber's
    # far-right value and the hero's headline all read the same
    # number arithmetically rather than being re-aligned at render
    # time.
    min_pct = min(float(s[2].min()) - 1.0 for s in series) * 100.0
    max_pct = max(float(s[2].max()) - 1.0 for s in series) * 100.0
    domain_min, domain_max, step = _y_domain(min_pct, max_pct)
    view_min = 1.0 + domain_min / 100.0
    view_max = 1.0 + domain_max / 100.0
    y_span = (view_max - view_min) or 1.0

    x_min = float(time_x.min())
    x_max = float(time_x.max())
    x_span = (x_max - x_min) or 1.0
    total_days = int(x_max - x_min)

    def map_x(day: float) -> float:
        return PLOT_X0 + (day - x_min) / x_span * (PLOT_X1 - PLOT_X0)

    def map_y(value: float) -> float:
        return PLOT_Y1 - (value - view_min) / y_span * (PLOT_Y1 - PLOT_Y0)

    # Smooth interpolation when there are three or more points,
    # straight segments for two. Two preconditions gate the dense
    # log-space fit:
    #   * ``time_x`` strictly increasing -- ``Pchip`` requires it, and
    #     two valuation snapshots dated the same day would otherwise
    #     raise ``ValueError`` and abort the whole build.
    #   * every sample strictly positive -- the fit runs in log space,
    #     and a multiplier of 0 (a full liquidation) or below would
    #     make ``np.log`` emit -inf / NaN that lands both in the SVG
    #     ``points`` (malformed path) and in the ``data-chart`` JSON
    #     as the literal ``NaN`` token (invalid JSON, so the
    #     scrubber's ``JSON.parse`` throws).
    # Fall back conservatively: raw points when the timeline isn't
    # strictly increasing, a linear-space Pchip when any sample is
    # non-positive.
    strictly_increasing = bool(np.all(np.diff(time_x) > 0))
    all_positive = all(bool(np.all(s[2] > 0.0)) for s in series)
    if len(time_x) >= 3 and strictly_increasing:
        interp_x = np.linspace(x_min, x_max, 200)
        if all_positive:
            fitted = {id(s[2]): np.exp(Pchip(time_x, np.log(s[2]))(interp_x)) for s in series}
        else:
            fitted = {
                id(s[2]): np.asarray(Pchip(time_x, s[2])(interp_x), dtype=float) for s in series
            }
    else:
        interp_x = time_x
        fitted = {id(s[2]): s[2] for s in series}

    px = np.array([map_x(x) for x in interp_x])

    def to_points(ys: np.ndarray) -> str:
        py = PLOT_Y1 - (ys - view_min) / y_span * (PLOT_Y1 - PLOT_Y0)
        return " ".join(f"{a:.2f},{b:.2f}" for a, b in zip(px, py, strict=True))

    svg: list[str] = [
        f'<svg viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="{html.escape(_chart_alt(series, total_return, benchmarks))}">'
    ]
    svg.extend(_axis_svg(domain_min, domain_max, step, map_y))
    svg.extend(_year_tick_svg(start_date, total_days, map_x))
    if len(series) >= 2:
        svg.extend(
            _band_svg(px, fitted[id(series[0][2])], fitted[id(series[1][2])], y_span, view_min)
        )
    for kind, _label, ys in series:
        svg.append(
            f'<polyline class="return-chart__line return-chart__line--{kind}" '
            f'points="{to_points(fitted[id(ys)])}"/>'
        )
    svg.extend(_end_label_svg(series, map_y))
    svg.append("</svg>")

    chart_data = {
        "start": start_date.strftime("%Y-%m-%d"),
        "totalDays": total_days,
        "view": {"w": VIEW_W, "h": VIEW_H},
        "plot": {"x0": PLOT_X0, "x1": PLOT_X1, "y0": PLOT_Y0, "y1": PLOT_Y1},
        "yMin": round(float(view_min), 6),
        "yMax": round(float(view_max), 6),
        "series": [
            {
                "kind": kind,
                "label": label,
                "x": [round(float(x), 2) for x in interp_x.tolist()],
                "y": [round(float(v), 6) for v in fitted[id(ys)].tolist()],
            }
            for kind, label, ys in series
        ],
    }
    chart_data_attr = html.escape(json.dumps(chart_data, separators=(",", ":")), quote=True)

    plot_html = (
        f'<div class="return-chart__plot">{"".join(svg)}'
        f"{_build_hover_html(has_delta=len(series) >= 2)}</div>"
    )
    return f'<figure class="return-chart" data-chart="{chart_data_attr}">{plot_html}</figure>'


def _chart_alt(
    series: Sequence[tuple[str, str, np.ndarray]],
    total_return: dict,
    benchmarks: list[dict],
) -> str:
    """One sentence describing the chart for a non-visual reader.

    The old label ("Portfolio return curve") described the genre, not
    the content. Someone who cannot see the chart needs the numbers
    it draws, which is what the sighted reader takes from it too.
    """
    twr = total_return.get("twr%")
    if len(series) < 2 or not benchmarks or twr is None:
        return "Cumulative return of the portfolio since inception"
    tsr = benchmarks[0].get("tsr%")
    if tsr is None:
        return "Cumulative return of the portfolio since inception"
    return (
        f"Cumulative return of the portfolio versus the {series[1][1]} since inception: "
        f"{_fmt_pct(float(twr))}% against {_fmt_pct(float(tsr))}%"
    )


def _axis_svg(
    domain_min: float,
    domain_max: float,
    step: float,
    map_y: Callable[[float], float],
) -> list[str]:
    """Gridlines + y labels, with the 0% line drawn as a real baseline."""
    out: list[str] = []
    ticks = round((domain_max - domain_min) / step)
    for index in range(ticks + 1):
        pct = domain_min + index * step
        y = map_y(1.0 + pct / 100.0)
        is_zero = abs(pct) < 1e-9
        cls = "return-chart__base" if is_zero else "return-chart__grid"
        out.append(
            f'<line class="{cls}" x1="{PLOT_X0:.2f}" y1="{y:.2f}" x2="{PLOT_X1:.2f}" y2="{y:.2f}"/>'
        )
        out.append(
            f'<text class="{_tick_class(index)}" x="{PLOT_X0 - 12:.2f}" y="{y + 5:.2f}" '
            f'text-anchor="end">{_tick_text(pct)}</text>'
        )
    return out


def _tick_class(index: int) -> str:
    """Class for an axis label, marking every other one as minor.

    The SVG scales uniformly, so on a phone the whole axis shrinks
    with the frame and the labels have to grow back in viewBox units
    to stay legible -- at which point there is no longer room for all
    of them. Marking alternates as minor lets the stylesheet drop
    every second label at narrow widths and keep the gridlines, which
    is the readable trade: fewer numbers, same grid.
    """
    return (
        "return-chart__tick" if index % 2 == 0 else "return-chart__tick return-chart__tick--minor"
    )


def _tick_text(pct: float) -> str:
    """Format an axis tick, dropping the decimal on whole numbers.

    ``_fmt_pct`` always renders one decimal below 100, which is right
    for a readout of a real quantity and wrong for an axis: the tick
    values come off a 1 / 2 / 2.5 / 5 ladder precisely so a reader
    can do arithmetic with them, and "+50%" is easier to do
    arithmetic with than "+50.0%". The half-steps of the 2.5 ladder
    keep their decimal, because there the digit carries information.
    """
    body = f"{pct:.0f}" if abs(pct - round(pct)) < 1e-9 else f"{pct:.1f}"
    return f"{'+' if pct >= 0 else ''}{body}%"


def _year_tick_svg(
    start: date,
    total_days: int,
    map_x: Callable[[float], float],
) -> list[str]:
    """Year labels under the plot, one per January the history spans."""
    out: list[str] = []
    for index, (year, day) in enumerate(_year_ticks(start, total_days)):
        x = map_x(day)
        # The first label sits flush with the axis origin rather than
        # centred on it, so it can't hang off the left edge of the
        # frame the way a centred one would.
        anchor = "start" if index == 0 else "middle"
        out.append(
            f'<text class="{_tick_class(index)}" x="{x:.2f}" y="{PLOT_Y1 + 26:.2f}" '
            f'text-anchor="{anchor}">{year}</text>'
        )
    return out


def _band_svg(
    px: np.ndarray,
    jg: np.ndarray,
    bench: np.ndarray,
    y_span: float,
    view_min: float,
) -> list[str]:
    """Fill the area between the two curves, split at every crossing.

    A single polygon painted one colour would claim a lead across
    windows where the portfolio was behind. Splitting on the sign of
    the gap costs one pass over the samples and keeps the fill
    honest: green where the portfolio leads, red where it trails.
    """

    def to_y(values: np.ndarray) -> np.ndarray:
        return PLOT_Y1 - (values - view_min) / y_span * (PLOT_Y1 - PLOT_Y0)

    jg_y, bench_y = to_y(jg), to_y(bench)
    out: list[str] = []
    for lo, hi, positive in _sign_runs(jg - bench):
        forward = " ".join(f"{px[i]:.2f},{jg_y[i]:.2f}" for i in range(lo, hi))
        backward = " ".join(f"{px[i]:.2f},{bench_y[i]:.2f}" for i in range(hi - 1, lo - 1, -1))
        modifier = "pos" if positive else "neg"
        out.append(
            f'<polygon class="return-chart__band return-chart__band--{modifier}" '
            f'points="{forward} {backward}"/>'
        )
    return out


def _end_label_svg(
    series: Sequence[tuple[str, str, np.ndarray]],
    map_y: Callable[[float], float],
) -> list[str]:
    """Dot + name + final value for each series, in the right inset.

    Labels are pushed apart when the two series finish within
    :data:`_LABEL_SLOT` of each other, so a dead-heat window renders
    two readable stacks rather than one overlapping smear. The dots
    stay on the curve; only the text moves.
    """
    finals = [(kind, label, float(ys[-1])) for kind, label, ys in series]
    anchors = [map_y(value) for _, _, value in finals]
    order = sorted(range(len(anchors)), key=lambda i: anchors[i])
    for position in range(1, len(order)):
        above, below = order[position - 1], order[position]
        overlap = _LABEL_SLOT - (anchors[below] - anchors[above])
        if overlap > 0:
            anchors[above] -= overlap / 2
            anchors[below] += overlap / 2
    out: list[str] = []
    for (kind, label, value), text_y in zip(finals, anchors, strict=True):
        dot_y = map_y(value)
        out.append(
            f'<circle class="return-chart__end-dot return-chart__end-dot--{kind}" '
            f'cx="{PLOT_X1:.2f}" cy="{dot_y:.2f}" r="4.5"/>'
        )
        out.append(
            f'<text class="return-chart__end-name" x="{PLOT_X1 + 12:.2f}" '
            f'y="{text_y - 6:.2f}">{html.escape(_short_label(kind, label))}</text>'
        )
        out.append(
            f'<text class="return-chart__end-value return-chart__end-value--{kind}" '
            f'x="{PLOT_X1 + 12:.2f}" y="{text_y + 15:.2f}">'
            f"{_fmt_pct((value - 1.0) * 100.0, signed=True)}%</text>"
        )
    return out


def _short_label(kind: str, label: str) -> str:
    """A series token for the right inset.

    The portfolio's own curve is initialled: "Portfolio" has no
    shorter form that is still a word, and the inset cannot hold the
    whole of it.

    The benchmark keeps its index number. "S&P" alone is not a
    shorter way of writing "S&P 500" on this page -- S&P Global is a
    *holding*, named three times in the tables below, so the bare
    abbreviation reads as the company that compiles the index rather
    than the index itself. Only the redundant " Index" suffix is
    dropped, and the cap is generous enough that "S&P 500" survives
    it intact.
    """
    if kind == "jg":
        return "JG"
    compact = label.replace(" Index", "").strip()
    return compact if len(compact) <= 12 else compact[:12]


def _build_hover_html(*, has_delta: bool) -> str:
    """Empty containers the scrubber script fills in on the fly."""
    hover_delta_bar_html = '<div class="return-chart__hover-delta-bar"></div>' if has_delta else ""
    tooltip_delta_html = '<div class="return-chart__tooltip-delta"></div>' if has_delta else ""
    return (
        '<div class="return-chart__hover" aria-hidden="true">'
        '<div class="return-chart__guide"></div>'
        f"{hover_delta_bar_html}"
        '<div class="return-chart__tooltip">'
        '<div class="return-chart__tooltip-date"></div>'
        '<div class="return-chart__tooltip-rows"></div>'
        f"{tooltip_delta_html}"
        "</div>"
        "</div>"
    )
