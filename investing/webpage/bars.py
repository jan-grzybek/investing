"""Horizontal CSS bar chart for asset-allocation breakdowns."""

from __future__ import annotations

import html
from collections.abc import Iterable

from ..formatting import _fmt_pct


def render(
    rows: Iterable[tuple[str, float]] | None,
    variant: str,
    *,
    anchors: dict[str, str] | None = None,
) -> str:
    """Render a horizontal CSS bar chart.

    ``rows`` is an iterable of ``(label, value)`` pairs where
    ``value`` is a percentage (0..100). Each row renders as
    ``label | value | bar`` so the percentages sit between the
    title and the bar. ``variant`` is the BEM modifier
    controlling the fill colour (e.g. ``"allocation"``).

    ``anchors`` is an optional ``{label: anchor-id}`` map
    (anchor without the leading ``#``). When present for a row,
    that row is emitted as an ``<a>`` instead of a plain
    ``<div>`` so clicking it scrolls to the targeted section.
    """
    if not rows:
        return ""
    rows = list(rows)

    row_html = []
    for label, value in rows:
        # ``value`` arrives unrounded; the bar's CSS width gets
        # two decimals for sub-pixel precision while the visible
        # label uses ``_fmt_pct`` -- one decimal under 100,
        # whole-number from 100 up.
        inner = (
            f'<div class="bars__label">{html.escape(str(label))}</div>'
            f'<div class="bars__value">{_fmt_pct(value)}%</div>'
            f'<div class="bars__track"><div class="bars__fill" '
            f'style="width: {value:.2f}%"></div></div>'
        )
        anchor = anchors.get(label) if anchors else None
        if anchor:
            # ``bars__row--link`` opts the row into the
            # underlined-free, pointer-cursor styling and keeps
            # the grid layout (``<a>`` is treated as a grid
            # container the same way ``<div>`` is).
            row_html.append(
                f'<a class="bars__row bars__row--link" href="#{html.escape(anchor)}">{inner}</a>'
            )
        else:
            row_html.append(f'<div class="bars__row">{inner}</div>')
    return f'<div class="bars bars--{variant}">{"".join(row_html)}</div>'
