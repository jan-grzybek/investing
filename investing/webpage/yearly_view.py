"""Calendar-year returns: the whole track record, always visible.

Three of seven years used to start collapsed behind a "Show all"
toggle -- that is, most of the evidence for the page's central claim
was one click away by default. The delta column was headed ``Δ``,
a bare glyph with no unit, and neither the portfolio column nor the
benchmark column said what kind of return it was showing.

So: every year renders, the column is called "Alpha" and carries
``pp``, and a pair of bars per row makes the shape of the record
scannable before any number is read. The bars are normalised to the
largest magnitude in the table, so the tallest pair fills its track
and the rest read as fractions of the best year.
"""

from __future__ import annotations

import html

from ..formatting import _fmt_pct, _value_class
from ..types import BenchmarkSummary, YearlyReturn


def _summary_line(rows: list[YearlyReturn], has_benchmark: bool) -> str:
    """Render the "N of M ..." line under the heading.

    Counts only what can be counted: years without a benchmark
    figure are excluded from the "ahead of" tally rather than
    silently treated as losses.
    """
    total = len(rows)
    positive = sum(1 for row in rows if row["jg%"] > 0)
    parts = [f"{positive} of {total} years positive"]
    if has_benchmark:
        comparable = [row for row in rows if row.get("bench%") is not None]
        ahead = sum(1 for row in comparable if row["jg%"] > row["bench%"])
        parts.append(f"{ahead} of {len(comparable)} ahead of the benchmark")
    return " &middot; ".join(parts)


def _bar_scale(rows: list[YearlyReturn]) -> float:
    """Largest magnitude across both series, used to size the bars."""
    values = [abs(row["jg%"]) for row in rows]
    values += [abs(row["bench%"]) for row in rows if row.get("bench%") is not None]
    return max(values, default=0.0)


def render(
    yearly_returns: list[YearlyReturn],
    benchmarks: list[BenchmarkSummary],
    *,
    benchmark_label: str,
) -> str:
    """Render the year-by-year block, or an empty string with no data."""
    if not yearly_returns:
        return ""

    has_benchmark = bool(benchmarks) and any("bench%" in row for row in yearly_returns)
    comparable = [row for row in yearly_returns if row.get("bench%") is not None]
    every_year_ahead = (
        has_benchmark
        and len(comparable) == len(yearly_returns)
        and all(row["jg%"] > row["bench%"] for row in comparable)
    )
    heading = "Ahead every year" if every_year_ahead else "Year by year"
    scale = _bar_scale(yearly_returns)

    header_cells = [
        '<th scope="col">Year</th>',
        '<th scope="col"><span class="visually-hidden">Relative size</span></th>',
        '<th scope="col" class="yearly__num">JG</th>',
    ]
    if has_benchmark:
        header_cells.append(
            f'<th scope="col" class="yearly__num">{html.escape(_short(benchmark_label))}</th>'
        )
        header_cells.append('<th scope="col" class="yearly__num">Alpha</th>')

    body: list[str] = []
    for row in yearly_returns:
        year = row["year"]
        if row.get("is_ytd"):
            year_cell = f'{year} <span class="yearly__ytd">(YTD)</span>'
        else:
            year_cell = html.escape(str(year))
        bench_pct = row.get("bench%")
        bars = [_bar(row["jg%"], scale, "jg")]
        if bench_pct is not None:
            bars.append(_bar(bench_pct, scale, "bench"))
        cells = [
            f'<th scope="row" class="yearly__year">{year_cell}</th>',
            # Same reason as the holdings weight cell: a flex ``<td>``
            # drops out of the table's formatting context, so the
            # column stops aligning with the figures beside it.
            f'<td class="yearly__bars"><span>{"".join(bars)}</span></td>',
            f'<td class="yearly__num {_value_class(row["jg%"])}">{_fmt_pct(row["jg%"])}%</td>',
        ]
        if has_benchmark:
            if bench_pct is None:
                cells.append('<td class="yearly__num yearly__empty">&mdash;</td>')
                cells.append('<td class="yearly__num yearly__empty">&mdash;</td>')
            else:
                delta = row["jg%"] - bench_pct
                cells.append(f'<td class="yearly__num yearly__bench">{_fmt_pct(bench_pct)}%</td>')
                cells.append(
                    f'<td class="yearly__num yearly__alpha {_value_class(delta)}">'
                    f"{_fmt_pct(delta, signed=True)} pp</td>"
                )
        body.append(f'<tr class="yearly__row">{"".join(cells)}</tr>')

    return (
        '<section class="yearly">'
        '<div class="yearly__head">'
        f'<h2 class="yearly__heading">{heading}</h2>'
        '<span class="yearly__caption">Time-weighted return, per calendar year</span>'
        "</div>"
        f'<p class="yearly__summary">{_summary_line(yearly_returns, has_benchmark)}</p>'
        '<table class="yearly__table">'
        f"<thead><tr>{''.join(header_cells)}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
        "</section>"
    )


def _bar(value: float, scale: float, kind: str) -> str:
    """One normalised bar. Negative years render in the loss colour."""
    width = 0.0 if scale <= 0 else min(100.0, abs(value) / scale * 100.0)
    sign = "neg" if value < 0 else "pos"
    return (
        f'<span class="yearly__bar yearly__bar--{kind} yearly__bar--{sign}" '
        f'style="width: {width:.1f}%"></span>'
    )


def _short(label: str) -> str:
    """Trim a benchmark name to a column-header-sized token.

    The index number stays, for the same reason it stays on the chart:
    S&P Global is a *holding* on this page, named in the tables below,
    so a column headed "S&P" invites the reader to match it against
    the wrong thing. Only the redundant " Index" suffix is dropped.
    """
    compact = label.replace(" Index", "").strip()
    return compact if len(compact) <= 12 else compact[:12]
