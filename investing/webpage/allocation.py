"""Allocation as two stacked part-of-a-whole bars.

This replaces roughly 500px of chart with about 120px, and answers
the same two questions better.

What was here before: a three-row horizontal bar chart for three
percentages (~90px of height, with the widest column -- the track --
carrying the least precision), and below it a 416px squarified
treemap of the equity sleeve. The treemap showed the same ten weights
the holdings list already showed, and its knockout filter flattened
every logo into a monochrome silhouette, losing the brand recognition
that justified putting logos in a chart in the first place. Once
weight lives in the holdings table as an in-row bar, the treemap has
nothing left to say.

What is here now: one stacked bar for the asset-class split and one
for the equity sleeve's sector mix. A stacked bar is the honest
primitive for a part-of-a-whole quantity -- the segments sum to the
frame, which is the entire claim -- and it survives to phone widths
without a layout engine.

Sector colours come from the same ``--treemap-color-*`` palette the
treemap used, so a reader who knew "blue-green means Technology"
still does.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence

from ..formatting import _fmt_pct

# Segments narrower than this can't fit their own percentage without
# the label spilling into a neighbour, so the number moves to the
# legend chip instead. 6% of the bar is roughly 50px at the page's
# 880px content width, which is about what "12.3%" needs at 12px.
_LABEL_MIN_PCT = 6.0

_OTHER_SECTOR = "Other"

# The cash bucket's key in the allocation rollup. Spelled out in
# ``investing.performance.summarize`` and mirrored here rather than
# imported, so the view layer keeps its one-way dependency on the
# rest of the package.
CASH_LABEL = "Cash & Cash Equivalents"

# Asset-class swatches. Equities take the JG accent because they are
# the sleeve every other accent-coloured surface on the page refers
# to (the chart's portfolio curve, the holdings weight bars, the OG
# card's Portfolio swatch); fixed income takes the benchmark navy and
# cash the neutral grey, so the three read as "the thing, the ballast,
# the dry powder" rather than as three arbitrary hues.
_ASSET_CLASS_VARS: tuple[tuple[str, str], ...] = (
    ("Equities", "--accent"),
    ("Fixed Income", "--accent-bench"),
    (CASH_LABEL, "--treemap-color-other"),
)
_ASSET_CLASS_COLORS: dict[str, str] = dict(_ASSET_CLASS_VARS)

# Sector swatches, keyed off the canonical sector name yfinance
# reports. Kept byte-identical to the treemap's palette so the
# colour vocabulary the page taught its readers survives the chart
# it was introduced with.
_SECTOR_VARS: tuple[tuple[str, str], ...] = (
    ("Technology", "--treemap-color-tech"),
    ("Communication Services", "--treemap-color-comm"),
    ("Consumer Cyclical", "--treemap-color-cyclical"),
    ("Consumer Defensive", "--treemap-color-defensive"),
    ("Healthcare", "--treemap-color-healthcare"),
    ("Financial Services", "--treemap-color-financial"),
    ("Industrials", "--treemap-color-industrials"),
    ("Energy", "--treemap-color-energy"),
    ("Utilities", "--treemap-color-utilities"),
    ("Basic Materials", "--treemap-color-materials"),
    ("Real Estate", "--treemap-color-realestate"),
    (_OTHER_SECTOR, "--treemap-color-other"),
)
_SECTOR_COLORS: dict[str, str] = dict(_SECTOR_VARS)


def _sector_color(sector: str) -> str:
    """CSS variable holding the swatch for ``sector``.

    Unknown sectors fall back to the ``Other`` swatch so the colour
    vocabulary stays bounded; a stray non-GICS label would otherwise
    render against a browser default and break the legend's promise
    that the same hue means the same sector.
    """
    return _SECTOR_COLORS.get(sector, _SECTOR_COLORS[_OTHER_SECTOR])


def sector_totals(holdings: Iterable[dict]) -> list[tuple[str, float]]:
    """Aggregate equity weights by sector, heaviest first.

    Weights are re-based onto the equity sleeve rather than left as
    shares of the whole portfolio: the bar is captioned "share of
    equities", and a set of segments that only sums to 78.7% of its
    own frame is exactly the kind of two-denominators-one-picture
    problem the treemap had.
    """
    totals: dict[str, float] = {}
    for holding in holdings:
        weight = holding.get("current_weight%")
        if weight is None or weight <= 0:
            continue
        sector = (holding.get("sector") or "").strip() or _OTHER_SECTOR
        totals[sector] = totals.get(sector, 0.0) + float(weight)
    grand = sum(totals.values())
    if grand <= 0:
        return []
    # Ties break on the sector name so the segment order is stable
    # across rebuilds and the page doesn't reshuffle its colours on
    # a run where nothing moved.
    return sorted(
        ((name, total / grand * 100.0) for name, total in totals.items()),
        key=lambda item: (-item[1], item[0]),
    )


def _bar(
    *,
    title: str,
    note: str,
    segments: Sequence[tuple[str, float, str]],
) -> str:
    """Render one titled stacked bar plus its legend.

    ``segments`` is ``(label, percent, css-colour-variable)`` in
    draw order. Each segment carries its own percentage inline when
    it is wide enough to hold one; narrow segments defer to the
    legend so the bar never renders a clipped number.
    """
    if not segments:
        return ""
    parts = []
    chips = []
    for label, pct, color in segments:
        inline = f"{_fmt_pct(pct)}%" if pct >= _LABEL_MIN_PCT else ""
        parts.append(
            f'<div class="allocation__segment" style="width: {pct:.2f}%; '
            f'background: var({color})" title="{html.escape(f"{label} {_fmt_pct(pct)}%")}">'
            f'<span class="allocation__segment-value">{html.escape(inline)}</span>'
            "</div>"
        )
        chip_value = (
            "" if inline else f' <span class="allocation__key-value">{_fmt_pct(pct)}%</span>'
        )
        chips.append(
            '<li class="allocation__key-item">'
            f'<span class="allocation__key-swatch" style="background: var({color})"></span>'
            f"{html.escape(label)}{chip_value}"
            "</li>"
        )
    note_html = f'<span class="allocation__note">{html.escape(note)}</span>' if note else ""
    return (
        '<div class="allocation__block">'
        '<div class="allocation__head">'
        f'<h3 class="allocation__title">{html.escape(title)}</h3>'
        f"{note_html}"
        "</div>"
        f'<div class="allocation__bar">{"".join(parts)}</div>'
        f'<ul class="allocation__key">{"".join(chips)}</ul>'
        "</div>"
    )


def render(
    allocation_pct: dict[str, float] | None,
    sectors: Sequence[tuple[str, float]],
) -> str:
    """Render the Allocation section, or an empty string with no data.

    Either bar may be missing independently: a portfolio holding only
    cash has an asset-class split but no sector mix, and a synthetic
    fixture may carry sector data without a rollup. Rendering
    whichever is available keeps the section honest instead of
    all-or-nothing.
    """
    blocks = []
    if allocation_pct:
        blocks.append(
            _bar(
                title="By asset class",
                note="",
                segments=[
                    (
                        label,
                        float(value),
                        _ASSET_CLASS_COLORS.get(label, "--treemap-color-other"),
                    )
                    for label, value in allocation_pct.items()
                    if float(value) > 0
                ],
            )
        )
    if sectors:
        blocks.append(
            _bar(
                title="Equity sleeve by sector",
                note="share of equities",
                segments=[(name, pct, _sector_color(name)) for name, pct in sectors],
            )
        )
    body = "".join(b for b in blocks if b)
    if not body:
        return ""
    return f'<div class="allocation">{body}</div>'
