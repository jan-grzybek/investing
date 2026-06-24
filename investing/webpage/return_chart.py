"""Inline-SVG return-curve chart with delta overlay + scrubber data.

Extracted from :mod:`investing.webpage._page` so the renderer
class can focus on per-section HTML assembly while the chart's
NumPy math (Pchip interpolation in log-space, vectorised SVG
point projection, delta-bracket geometry) lives in one place.

The public entrypoint is :func:`render`. The output is a
``<figure class="return-chart" data-chart="...">`` block ready
to drop into the rendered page; the densely-sampled curve and
viewport box are encoded in the ``data-chart`` JSON so the
client-side scrubber script can render markers without
re-running the interpolation.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable

import numpy as np
from dateutil.relativedelta import relativedelta

from ..formatting import _fmt_date_long, _fmt_pct, _format_duration, _value_class
from ..pchip import Pchip

# Type alias for ``benchmark -> friendly display name`` resolver
# (a thin function rather than reaching into the renderer's
# ``_BENCHMARK_DISPLAY_NAMES`` map directly, so the chart module
# stays decoupled from the renderer's lookup convention).
BenchmarkLabeller = Callable[[dict], str]


def render(
    total_return: dict,
    benchmarks: list[dict],
    *,
    benchmark_label: BenchmarkLabeller,
) -> str:
    """Render an inline SVG of the portfolio return curve.

    When a benchmark is present we reserve a slice on the right
    edge of the chart for an outperformance annotation: a
    vertical line connecting the JG and benchmark endpoints with
    a "+X.X pp" label showing the cumulative-return delta in
    percentage points.

    Returns an empty string when the history has fewer than two
    samples (since there is nothing to draw).
    """
    history = total_return.get("history", [])
    if len(history) < 2:
        return ""

    # Collect series (JG + each benchmark) and the global y-range.
    start_date = history[0][0]
    time_x = np.array(
        [int((d - start_date).days) for d, _ in history],
        dtype=float,
    )
    jg_y = np.array([v for _, v in history], dtype=float)

    series: list[tuple[str, str, np.ndarray]] = [("jg", "JG", jg_y)]
    for benchmark in benchmarks or []:
        bh = benchmark.get("history", [])
        if len(bh) < 2:
            continue
        label = benchmark_label(benchmark)
        series.append(
            ("bench", label, np.array([v for _, v in bh], dtype=float)),
        )

    # The upstream contract is: each series' rightmost sample IS
    # the cumulative return at "now", expressed as a multiplier
    # (1 + twr%/100 for JG, 1 + tsr%/100 for each benchmark). JG
    # gets there by construction in ``calc_twr`` (TWR's final
    # entry IS its history's last point); ``Benchmark.summary``
    # pins its chart sample at "now" to the same
    # ``regularMarketPrice / Adj Close[0]`` numerator the TSR is
    # computed from. So the chart's right edge, the hover
    # scrubber's far-right value, and the comparison capsule
    # below all read the same number arithmetically rather than
    # being re-aligned at render time.

    min_y = min(float(s[2].min()) for s in series)
    max_y = max(float(s[2].max()) for s in series)
    # Add a little headroom so the curves don't sit on the frame.
    pad_y = max((max_y - min_y) * 0.05, 0.01)
    view_max = max_y + pad_y
    view_min = min_y - pad_y

    width = 1000.0
    height = 400.0
    # Reserve 12% on the right when we'll be drawing a delta
    # annotation so its bar+label don't overlap the curves.
    has_delta = len(series) >= 2 and series[0][0] == "jg" and series[1][0] == "bench"
    right_margin_pct = 12.0 if has_delta else 0.0
    chart_x_end = width * (1 - right_margin_pct / 100.0)

    # Hoist the per-axis spans out of the closures: ``map_x`` /
    # ``map_y`` used to recompute ``time_x.min()`` / ``.max()``
    # on every call. The ``or 1.0`` guards collapse a
    # single-point timeline (or a flat-line series with
    # min == max) onto the left edge / reference line rather
    # than dividing by zero.
    x_min = float(time_x.min())
    x_max = float(time_x.max())
    x_span = (x_max - x_min) or 1.0
    y_span = (view_max - view_min) or 1.0

    def map_y(value: float) -> float:
        return height - (value - view_min) / y_span * height

    # Smooth interpolation when there are three or more points,
    # straight segments for two. Two preconditions gate the dense
    # log-space fit:
    #   * ``time_x`` strictly increasing -- ``Pchip`` requires it, and
    #     two valuation snapshots dated the same day would otherwise
    #     raise ``ValueError`` and abort the whole build.
    #   * every sample strictly positive -- the fit runs in log space,
    #     and a multiplier of 0 (a full liquidation) or below (a
    #     net-negative wipeout) would make ``np.log`` emit -inf / NaN
    #     that lands both in the SVG ``points`` (malformed path) and in
    #     the ``data-chart`` JSON as the literal ``NaN`` token (invalid
    #     JSON -> the client scrubber's ``JSON.parse`` throws).
    # Fall back conservatively: raw points when the timeline isn't
    # strictly increasing, a linear-space Pchip when any sample is
    # non-positive.
    strictly_increasing = bool(np.all(np.diff(time_x) > 0))
    all_positive = all(bool(np.all(s[2] > 0.0)) for s in series)
    if len(time_x) >= 3 and strictly_increasing:
        dense = np.linspace(x_min, x_max, 200)
        interp_x = dense
        if all_positive:
            interp_targets = {id(s[2]): np.exp(Pchip(time_x, np.log(s[2]))(dense)) for s in series}
        else:
            interp_targets = {
                id(s[2]): np.asarray(Pchip(time_x, s[2])(dense), dtype=float) for s in series
            }
    else:
        interp_x = time_x
        interp_targets = {id(s[2]): s[2] for s in series}

    def to_points(ys: np.ndarray) -> str:
        # Vectorise the projection so the inner loop only does
        # the f-string formatting; avoids 200 Python-level
        # ``map_x`` / ``map_y`` invocations per series.
        px = (interp_x - x_min) / x_span * chart_x_end
        py = height - (ys - view_min) / y_span * height
        return " ".join(f"{a:.2f},{b:.2f}" for a, b in zip(px, py, strict=False))

    ref_y = map_y(1.0)
    svg_lines = [
        f'<svg viewBox="0 0 {int(width)} {int(height)}" '
        'xmlns="http://www.w3.org/2000/svg" '
        'preserveAspectRatio="none" role="img" '
        'aria-label="Portfolio return curve">',
        (
            f'<line class="return-chart__ref" x1="0" y1="{ref_y:.2f}" '
            f'x2="{chart_x_end:.2f}" y2="{ref_y:.2f}"/>'
        ),
    ]
    for kind, _label, ys in series:
        svg_lines.append(
            f'<polyline class="return-chart__line return-chart__line--{kind}" '
            f'points="{to_points(interp_targets[id(ys)])}"/>'
        )
    svg_lines.append("</svg>")

    delta_html = (
        _build_delta_html(
            series=series,
            total_return=total_return,
            benchmarks=benchmarks,
            map_y=map_y,
            height=height,
        )
        if has_delta
        else ""
    )

    legend_html = _build_legend_html(series) if len(series) > 1 else ""

    # Caption: when this chart sits above the comparison block
    # we rely on it to anchor the period (the comparison block
    # omits its own period header in that case to avoid
    # repetition). Long-form date here -- the caption reads as
    # prose, not as a tabular slot, so DD/MM/YYYY would break
    # the sentence rhythm.
    duration = _format_duration(relativedelta(history[-1][0], start_date))
    caption = (
        f'<div class="return-chart__caption">'
        f'Since <time datetime="{start_date.strftime("%Y-%m-%d")}">'
        f"{_fmt_date_long(start_date)}</time> &middot; "
        f"{html.escape(duration)}</div>"
    )

    hover_html = _build_hover_html(has_delta=has_delta)

    # Pack the scrubber data into a JSON blob on the <figure>.
    # We embed the SAME densely-sampled curve the SVG polyline
    # draws so the marker dots track the rendered line exactly.
    dense_x = [round(float(x), 2) for x in interp_x.tolist()]
    chart_data = {
        "start": start_date.strftime("%Y-%m-%d"),
        "totalDays": int(time_x[-1] - time_x[0]),
        "rightPct": right_margin_pct,
        "yMin": round(float(view_min), 6),
        "yMax": round(float(view_max), 6),
        "series": [
            {
                "kind": kind,
                "label": label,
                "x": dense_x,
                "y": [round(float(v), 6) for v in interp_targets[id(ys)].tolist()],
            }
            for kind, label, ys in series
        ],
    }
    chart_data_attr = html.escape(
        json.dumps(chart_data, separators=(",", ":")),
        quote=True,
    )

    plot_html = (
        f'<div class="return-chart__plot">{"".join(svg_lines)}{hover_html}{delta_html}</div>'
    )
    return (
        f'<figure class="return-chart" data-chart="{chart_data_attr}">'
        f"{plot_html}{legend_html}{caption}</figure>"
    )


def _build_delta_html(
    *,
    series: list[tuple[str, str, np.ndarray]],
    total_return: dict,
    benchmarks: list[dict],
    map_y: Callable[[float], float],
    height: float,
) -> str:
    """Outperformance overlay: vertical bracket + percentage-point label."""
    jg_final = float(series[0][2][-1])
    bench_final = float(series[1][2][-1])
    # The series' rightmost samples are the same numbers
    # ``total_return["twr%"]`` / ``benchmarks[0]["tsr%"]`` carry
    # (see the long comment above the series loop in ``render``),
    # so differencing them yields the same value the comparison
    # capsule below the chart shows -- both reduce to
    # ``twr% - tsr%``. We prefer the explicit canonical numbers
    # when available so a single arithmetic source of truth feeds
    # the label even when a test fixture passes a bare ``history``
    # without ``twr%`` / ``tsr%``.
    twr_pct = total_return.get("twr%")
    tsr_pct = benchmarks[0].get("tsr%") if benchmarks else None
    if twr_pct is not None and tsr_pct is not None:
        delta_pp = float(twr_pct) - float(tsr_pct)
    else:
        delta_pp = (jg_final - bench_final) * 100.0
    jg_y_pct = map_y(jg_final) / height * 100.0
    bench_y_pct = map_y(bench_final) / height * 100.0
    top_pct = min(jg_y_pct, bench_y_pct)
    height_pct = abs(jg_y_pct - bench_y_pct)
    delta_color = "var(--positive)" if delta_pp >= 0 else "var(--negative)"
    return (
        '<div class="return-chart__delta" '
        f'style="--top: {top_pct:.2f}%; --height: {height_pct:.2f}%; '
        f'--delta-color: {delta_color};">'
        '<span class="return-chart__delta-bar"></span>'
        f'<span class="return-chart__delta-label {_value_class(delta_pp)}">'
        f"{_fmt_pct(delta_pp, signed=True)} pp</span>"
        "</div>"
    )


def _build_legend_html(series: list[tuple[str, str, np.ndarray]]) -> str:
    """Render the per-series legend chip row."""
    chips = []
    for kind, label, _ in series:
        chips.append(
            f'<span><span class="return-chart__swatch '
            f'return-chart__swatch--{kind}" '
            f'style="background: var(--{"accent" if kind == "jg" else "accent-bench"});">'
            f"</span>{html.escape(label)}</span>"
        )
    return f'<div class="return-chart__legend">{"".join(chips)}</div>'


def _build_hover_html(*, has_delta: bool) -> str:
    """Empty containers the scrubber script fills in on the fly.

    ``.return-chart__hover`` sits BEFORE ``.return-chart__delta``
    in source order so a CSS sibling selector can dim the
    static end-of-period delta while the scrubber is active.
    The hover overlay still paints on top thanks to its
    explicit ``z-index`` in the page stylesheet.
    """
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
