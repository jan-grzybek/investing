"""Holdings tables: one row per position, sortable by column.

Holdings used to render as ``<article class="holding">`` capsules,
three visible with the rest behind a "Show all" toggle, each metric
boxed in its own cell of a per-capsule grid. Two things were wrong
with that. Most of the portfolio was hidden by default, so the
treemap above it linked to rows that were ``display: none`` and a
script had to force-expand the list on anchor click. And the capsule
layout defeats the one thing a reader wants from a holdings list --
comparison -- because no two numbers ever share a column.

So: one table, every row visible, click a column header to sort.
Weight renders as an in-row bar so the shape of the book is legible
without reading a single number, and the money-weighted metrics sit
in single columns that run the length of the table.

Rows are grouped into ``<tbody>`` sections (equities, fixed income,
closed) with a band row naming each group and its share of the
portfolio. Sorting is per-section: the JS reorders rows inside their
own ``<tbody>`` so a sort can never shuffle a bond into the equity
sleeve.

The module is intentionally view-only: no FX, no aggregation, no
logo lookup of its own. Callers pass the resolved logo URL in -- the
renderer's ``_get_logo_url`` continues to own the per-page logo
cache.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Sequence
from datetime import date

from ..errors import InvariantError
from ..formatting import _fmt_date, _fmt_pct, _format_sort_number, _value_class
from ..holdings import CAGR_TBA_THRESHOLD, google_search_url
from .anchors import holding_anchor

# Column spec: ``(key, label, kind, css_modifier)``.
#
# ``key`` is the ``data-sort-key`` the header carries and the suffix
# of the ``data-sort-<key>`` attribute each row exposes; ``kind``
# drives the direction the sort script picks the first time a column
# is activated ("text" -> ascending A-Z, "number" -> descending
# high-low), matching the natural reading direction for each datatype.
#
# The ``tsr`` / ``cagr`` keys are historical and deliberately kept:
# they match the ``data-sort-*`` contract the script and the on-disk
# DOM-order tests already pin. Only the visible labels read "Return"
# and "IRR", because the underlying figures are MoIC- and IRR-based
# rather than TWR/CAGR -- see the Method block for the rationale.
OPEN_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("name", "Holding", "text", "name"),
    ("since", "Held since", "text", "since"),
    ("weight", "Weight", "number", "weight"),
    ("tsr", "Return", "number", "num"),
    ("cagr", "IRR", "number", "num"),
)

CLOSED_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("name", "Holding", "text", "name"),
    ("held", "Dates held", "text", "periods"),
    ("tsr", "Return", "number", "num"),
    ("cagr", "IRR", "number", "num"),
)


def _weight_bar(weight: float, *, muted: bool) -> str:
    """Render the in-row weight bar plus its numeric label.

    The row publishes its own weight as ``--w`` and the stylesheet
    divides it by the table's ``--holdings-weight-scale`` (the
    largest weight on the page, set once on the ``<table>``). Doing
    the normalisation in CSS rather than in Python means a row can be
    rendered the moment its holding arrives, without the renderer
    having to see the whole book first.

    Scaling to the largest position rather than to 100% is what makes
    the column readable: a book whose top holding is 21% would
    otherwise draw every bar inside the left fifth of the track,
    where the differences that matter are a few pixels wide.

    Fixed income gets the neutral fill rather than the JG accent. The
    accent means "this is the equity sleeve" everywhere else on the
    page -- chart curve, allocation bar, OG card swatch -- and a bond
    ETF is not that.
    """
    fill = "holdings__bar-fill holdings__bar-fill--muted" if muted else "holdings__bar-fill"
    return (
        '<span class="holdings__bar">'
        f'<span class="{fill}" style="--w: {max(weight, 0.0):.2f}"></span>'
        "</span>"
        f'<span class="holdings__weight-value">{_fmt_pct(weight)}%</span>'
    )


def _logo_cell(*, logo_url: str, website_url: str, company_name: str) -> str:
    """Render the leading logo cell, linked to the issuer's own site.

    Decorative ``alt=""`` on the ``<img>`` means the link needs its
    own accessible name; ``aria-label`` / ``title`` carry that and a
    mouse tooltip. ``target="_blank"`` keeps the reader's place on the
    portfolio page and ``rel="noopener noreferrer"`` blocks the target
    from reaching back through ``window.opener``.

    Explicit ``width`` / ``height`` reserve the box before the SVG
    decodes, so a table of 12 logos settles at zero layout shift.
    """
    label = f"Open {company_name or 'company'} website"
    return (
        '<td class="holdings__logo-cell">'
        f'<a class="holdings__logo-link" href="{html.escape(website_url)}" '
        'target="_blank" rel="noopener noreferrer" '
        f'aria-label="{html.escape(label)}" title="{html.escape(label)}">'
        f'<img class="holdings__logo" src="{html.escape(logo_url)}" alt="" '
        'loading="lazy" decoding="async" width="48" height="30">'
        "</a>"
        "</td>"
    )


def _name_cell(holding: dict) -> str:
    """Render the company name with its listing(s) underneath.

    The name is the answer to "what do I own"; the ticker is a detail
    of the transaction, so it sits below at a smaller size rather
    than competing for the row's first line. A position assembled
    from several listings (see :mod:`investing.positions`) names all
    of them, joined with ``+``, because no single symbol identifies
    it honestly.
    """
    listings = " + ".join(holding.get("tickers") or [holding["ticker"]])
    return (
        '<th class="holdings__name-cell" scope="row">'
        f'<span class="holdings__name">{html.escape(holding["name"])}</span>'
        f'<span class="holdings__ticker">{html.escape(listings)}</span>'
        "</th>"
    )


def _period_start(holding: dict) -> date:
    """The date the position the reader is looking at was opened.

    A re-entered position has several periods; the open one is what
    "Held since" means, so prefer the period with no end date and
    fall back to the most recent start when every period is closed.
    """
    periods = holding["periods"]
    open_periods = [p for p in periods if p["end"] is None]
    return max(p["start"] for p in (open_periods or periods))


def _periods_cell(holding: dict) -> str:
    """Render every ownership window of a closed position, newest first.

    ``Holding.summary`` already returns newest-first in production,
    but preview / synthetic data might not, and the visual order is a
    UX guarantee rather than an upstream accident.
    """
    ordered = sorted(holding["periods"], key=lambda p: p["start"], reverse=True)
    items = []
    for period in ordered:
        start, end = period["start"], period["end"]
        start_html = f'<time datetime="{start.strftime("%Y-%m-%d")}">{_fmt_date(start)}</time>'
        if end is None:
            end_html = "<span>Present</span>"
        else:
            end_html = f'<time datetime="{end.strftime("%Y-%m-%d")}">{_fmt_date(end)}</time>'
        items.append(f"<li>{start_html}<span>&ndash;</span>{end_html}</li>")
    return f'<td class="holdings__periods"><ul>{"".join(items)}</ul></td>'


def _metric_cells(holding: dict) -> str:
    """Return the Return / IRR cells for one row.

    IRR renders as "TBA" while the position is too young for an
    annualised figure to mean anything (see
    :data:`investing.holdings.CAGR_TBA_THRESHOLD`); the cell carries
    no sign colour in that case because there is no sign yet.
    """
    tsr = holding["tsr%"]
    cells = [
        f'<td class="holdings__num {_value_class(tsr)}">{_fmt_pct(tsr, signed=True)}%</td>',
    ]
    cagr = holding["cagr%"]
    if cagr > CAGR_TBA_THRESHOLD:
        cells.append('<td class="holdings__num holdings__num--tba">TBA</td>')
    else:
        cells.append(
            f'<td class="holdings__num holdings__num--soft {_value_class(cagr)}">'
            f"{_fmt_pct(cagr, signed=True)}%</td>"
        )
    return "".join(cells)


def build_row(holding: dict, *, logo_url_for: Callable[[str], str]) -> str:
    """Render one holding as a ``<tr>``.

    Open positions get a "Held since" date and a weight bar; closed
    ones get the list of windows they were held over instead, and no
    weight at all -- they have none.
    """
    is_current = holding["is_current"]
    website_url = holding.get("website") or google_search_url(holding["name"])
    sort_attrs = {
        "name": holding["name"].casefold(),
        "tsr": _format_sort_number(holding["tsr%"]),
        "cagr": _format_sort_number(holding["cagr%"]),
    }

    cells = [
        _logo_cell(
            logo_url=logo_url_for(holding["ticker"]),
            website_url=website_url,
            company_name=holding["name"],
        ),
        _name_cell(holding),
    ]

    if is_current:
        weight = holding["current_weight%"]
        if weight is None:
            raise InvariantError(
                f"current holding {holding['ticker']!r} reached the "
                "renderer with no weight -- apply_rollup() did not run",
            )
        start = _period_start(holding)
        sort_attrs["since"] = start.strftime("%Y-%m-%d")
        sort_attrs["weight"] = _format_sort_number(weight)
        cells.append(
            f'<td class="holdings__since">'
            f'<time datetime="{start.strftime("%Y-%m-%d")}">{_fmt_date(start)}</time>'
            "</td>"
        )
        muted = holding.get("asset_class") == "fixed_income"
        cells.append(f'<td class="holdings__weight">{_weight_bar(weight, muted=muted)}</td>')
    else:
        # Closed rows sort by their most recent exit, which is the
        # date a reader scanning "when did this end" is looking for.
        sort_attrs["held"] = max(
            p["end"].strftime("%Y-%m-%d") for p in holding["periods"] if p["end"] is not None
        )
        cells.append(_periods_cell(holding))

    cells.append(_metric_cells(holding))
    attrs = "".join(
        f' data-sort-{key}="{html.escape(sort_attrs[key])}"' for key in sorted(sort_attrs)
    )
    return (
        f'<tr class="holdings__row" id="{html.escape(holding_anchor(holding["ticker"]))}"{attrs}>'
        f"{''.join(cells)}"
        "</tr>"
    )


def build_group(*, label: str, rows: Sequence[str], columns: int) -> str:
    """Wrap ``rows`` in a ``<tbody>`` under a band row naming the group.

    The band is a full-width row rather than a second ``<thead>`` so
    the table keeps one header, one ``aria-sort`` state and one set
    of column widths across every group. Empty groups render nothing
    at all -- "no title for an empty section" is the same
    asymmetric-portfolio contract the rest of the page follows.
    """
    if not rows:
        return ""
    return (
        '<tbody class="holdings__section">'
        f'<tr class="holdings__band"><td colspan="{columns}">{html.escape(label)}</td></tr>'
        f"{''.join(rows)}"
        "</tbody>"
    )


def build_table(
    *,
    scope: str,
    groups: Iterable[str],
    columns: tuple[tuple[str, str, str, str], ...] = OPEN_COLUMNS,
    caption: str,
    weight_scale: float = 0.0,
) -> str:
    """Assemble the header + groups into one sortable table.

    ``caption`` is a visually-hidden ``<caption>``: the visible
    heading above the table is an ``<h2>``, but a table still owes
    screen-reader users a name of its own, and "Holdings" alone does
    not distinguish the open book from the closed one.

    ``weight_scale`` is the largest weight in the table; every row's
    bar is drawn as a fraction of it (see :func:`_weight_bar`). Zero
    means no weight column, in which case the property is omitted
    rather than published as a division by zero waiting to happen.
    """
    body = "".join(groups)
    if not body:
        return ""
    scale_attr = f' style="--holdings-weight-scale: {weight_scale:.2f}"' if weight_scale > 0 else ""
    # ``+ 1`` for the logo column, which is decorative and carries no
    # sort affordance of its own.
    header_cells = ['<th class="holdings__col-logo"><span class="visually-hidden">Logo</span></th>']
    for key, label, kind, modifier in columns:
        header_cells.append(
            f'<th class="holdings__col holdings__col--{modifier}" '
            f'data-sort-key="{key}" data-sort-kind="{kind}" aria-sort="none">'
            '<button type="button" class="holdings__sort">'
            f"{html.escape(label)}"
            '<span class="holdings__indicator" aria-hidden="true"></span>'
            "</button>"
            "</th>"
        )
    return (
        '<div class="holdings__wrap">'
        f'<table class="holdings" data-holdings-table="{html.escape(scope)}"{scale_attr}>'
        f'<caption class="visually-hidden">{html.escape(caption)}</caption>'
        f"<thead><tr>{''.join(header_cells)}</tr></thead>"
        f"{body}"
        "</table>"
        "</div>"
    )


def build_holding_card(holding: dict, *, logo_url_for: Callable[[str], str]) -> str:
    """Backwards-compatible alias for :func:`build_row`.

    The historical name is kept because ``Webpage._build_holding_card``
    delegates to it and external snapshots bind to that call surface;
    the capsule it used to build is gone.
    """
    return build_row(holding, logo_url_for=logo_url_for)


__all__ = [
    "CLOSED_COLUMNS",
    "OPEN_COLUMNS",
    "build_group",
    "build_holding_card",
    "build_row",
    "build_table",
]
