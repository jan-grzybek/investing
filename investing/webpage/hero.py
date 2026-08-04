"""The claim, above the fold.

The page exists to say one thing: this portfolio beat the S&P 500.
That sentence used to appear nowhere on the first screen -- the top
200px carried a name, four nav pills and an animated logo strip, and
no number at all until the reader scrolled past them. This block is
the fix: the alpha at 88px, the sentence that gives its unit
meaning, and a four-up strip of the supporting figures underneath.

The freshness date rides in the eyebrow rather than in the footer.
For a page built on live market data, "Updated Aug 4, 2026" is part
of the claim, not a filing note four screens down.
"""

from __future__ import annotations

import html
from datetime import date, datetime

from dateutil.relativedelta import relativedelta

from ..formatting import _fmt_date_long, _fmt_pct, _format_duration, _value_class
from ..types import BenchmarkSummary, TotalReturn, YearlyReturn


def _stat(*, label: str, value: str, note: str = "", swatch: str = "") -> str:
    """One cell of the four-up strip under the headline.

    ``swatch`` names the CSS modifier for the little colour chip that
    ties a figure to its curve on the chart below ("jg" / "bench");
    cells without a counterpart on the chart render without one.
    ``note`` is the secondary figure some cells carry ("vs 9.2%",
    "3 closed"), set smaller and muted beside the primary.
    """
    chip = f'<span class="hero__swatch hero__swatch--{swatch}"></span>' if swatch else ""
    tail = f'<span class="hero__stat-note">{html.escape(note)}</span>' if note else ""
    return (
        '<div class="hero__stat">'
        f'<dt class="hero__stat-label">{chip}{html.escape(label)}</dt>'
        f'<dd class="hero__stat-value">{value}{tail}</dd>'
        "</div>"
    )


def _every_year_ahead(yearly_returns: list[YearlyReturn]) -> bool:
    """True when the portfolio led the benchmark in every listed year.

    Used only to decide whether the gloss under the headline may
    claim it. A single year without a benchmark figure makes the
    claim unprovable, so it is withheld rather than approximated.
    """
    if not yearly_returns:
        return False
    return all(
        row.get("bench%") is not None and row["jg%"] > row["bench%"] for row in yearly_returns
    )


def render(
    *,
    total_return: TotalReturn,
    benchmarks: list[BenchmarkSummary],
    yearly_returns: list[YearlyReturn],
    benchmark_label: str,
    position_counts: tuple[int, int],
    now: date | datetime,
    update_date: str,
    update_iso: str,
) -> str:
    """Render the hero block.

    ``position_counts`` is ``(open, closed)``. With no benchmark
    configured the headline falls back to the portfolio's own total
    return -- there is no alpha to lead with, and inventing a
    comparison the page cannot draw would be worse than leading with
    a smaller true claim.
    """
    bench = benchmarks[0] if benchmarks else None
    twr = float(total_return["twr%"])
    bench_tsr = float(bench["tsr%"]) if bench else None
    start_date = total_return["start_date"]
    duration = _format_duration(relativedelta(now, start_date))
    since = (
        f'since <time datetime="{start_date.strftime("%Y-%m-%d")}">'
        f"{_fmt_date_long(start_date)}</time>"
    )

    if bench_tsr is None:
        eyebrow_metric = "Time-weighted return"
        figure = f'{_fmt_pct(twr)}<span class="hero__unit">%</span>'
        figure_class = _value_class(twr)
        claim = "total return since inception"
        gloss = f"over {html.escape(duration)}, {since}."
    else:
        delta = twr - bench_tsr
        eyebrow_metric = f"Time-weighted return vs {benchmark_label}"
        figure = f'{_fmt_pct(delta, signed=True)}<span class="hero__unit">pp</span>'
        figure_class = _value_class(delta)
        claim = f"{'ahead of' if delta >= 0 else 'behind'} the {benchmark_label}"
        gloss = f"over {html.escape(duration)}, {since}"
        if delta >= 0 and _every_year_ahead(yearly_returns):
            gloss += " &mdash; and ahead in every calendar year"
        gloss += "."

    stats = [
        _stat(label="Portfolio TWR", value=f"{_fmt_pct(twr)}%", swatch="jg"),
    ]
    if bench_tsr is not None:
        stats.append(
            _stat(
                label=f"{benchmark_label} TSR",
                value=f'<span class="hero__stat-bench">{_fmt_pct(bench_tsr)}%</span>',
                swatch="bench",
            )
        )
    cagr = total_return.get("cagr%")
    if cagr is not None:
        note = ""
        if bench is not None and bench.get("cagr%") is not None:
            note = f"vs {_fmt_pct(float(bench['cagr%']))}%"
        stats.append(_stat(label="Annualised", value=f"{_fmt_pct(float(cagr))}%", note=note))
    open_count, closed_count = position_counts
    stats.append(
        _stat(
            label="Positions",
            value=str(open_count),
            note=f"{closed_count} closed" if closed_count else "",
        )
    )

    return (
        '<section class="hero" aria-labelledby="hero-claim">'
        '<p class="hero__eyebrow">'
        f'<span class="hero__metric">{html.escape(eyebrow_metric)}</span>'
        '<span class="hero__eyebrow-sep" aria-hidden="true"></span>'
        '<span class="hero__updated" title="The page is rebuilt a few times a day '
        '&mdash; it does not tick during market hours">Updated '
        f'<time datetime="{html.escape(update_iso)}">{html.escape(update_date)}</time>'
        "</span>"
        "</p>"
        '<div class="hero__claim">'
        f'<p class="hero__figure {figure_class}">{figure}</p>'
        '<div class="hero__gloss">'
        f'<h2 class="hero__headline" id="hero-claim">{html.escape(claim)}</h2>'
        f'<p class="hero__period">{gloss}</p>'
        "</div>"
        "</div>"
        f'<dl class="hero__stats">{"".join(stats)}</dl>'
        "</section>"
    )
