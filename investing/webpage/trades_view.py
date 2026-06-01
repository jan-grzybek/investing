"""Sortable trades table renderer.

Two public entrypoints:

* :func:`build_row` renders one burst-aggregated trade as a
  ``<tr>``; the renderer calls it once per event so it can
  collect the strings and pass them to :func:`build_table`.
* :func:`build_table` wraps the row fragments in the sortable
  ``<table>`` and adds the "Show all" toggle when the log is
  longer than :data:`VISIBLE_DEFAULT`.

The constants here are the single source of truth for "which
columns are sortable" and "where do INCREASE/DECREASE rows fall
in dict-order"; the matching :data:`_TRADES_SORT_SCRIPT` reads
the same ``data-sort-*`` attributes.
"""
from __future__ import annotations

import html

from ..formatting import _fmt_quarter_range
from ..trades import _BUY_CATEGORIES, _TRADE_ACTION_DISPLAY, _TRADE_DETAIL_LABELS
from .anchors import strip_exchange

# Numeric sort indices for the Action and Details columns. The
# buy-vs-sell axis is binary (action == 0 for BUY, 1 for SELL),
# so sorting ascending groups Bought rows above Sold rows. The
# finer-grained "Details" sort uses the dict-order index --
# OPEN -> INCREASE -> DECREASE -> CLOSE, ascending -- so an
# ascending sweep flows through the position's lifecycle.
TRADE_DETAIL_SORT_INDEX: dict[str, int] = {
    category: index
    for index, category in enumerate(
        ("OPEN", "INCREASE", "DECREASE", "CLOSE")
    )
}


TRADE_ACTION_SORT_INDEX: dict[str, int] = {
    category: 0 if category in _BUY_CATEGORIES else 1
    for category in _TRADE_ACTION_DISPLAY
}


# Headers for the sortable columns. ``key`` is the
# ``data-sort-key`` consumed by the inline trades-sort script
# and matched against the per-row ``data-sort-*`` attributes;
# ``label`` is the displayed text; ``modifier`` is the BEM
# modifier added to the ``<th>``.
SORTABLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("ticker", "Ticker",  "trades__col--ticker"),
    ("name",   "Company", "trades__col--name"),
    ("action", "Action",  "trades__col--action"),
    ("detail", "Details", "trades__col--detail"),
    ("date",   "Date",    "trades__col--date"),
)


# How many rows the trades table shows by default before the
# rest are tucked behind the "Show all" toggle. Kept
# conservative so a glance at the section reads as "recent
# activity" rather than "every trade ever"; the full log is one
# click away. The same constant lives in the CSS rule that
# hides overflow rows (``:nth-of-type(n+11)``); the two must
# stay in sync.
VISIBLE_DEFAULT: int = 10


def _detail_text(event: dict) -> str:
    """Human-facing text for the "Details" column.

    OPEN / CLOSE return the static lifecycle labels (the
    position came into existence / was disposed of,
    respectively). INCREASE / DECREASE return a signed
    whole-number percentage of the burst's magnitude relative
    to the prior position -- ``+30%`` reads as "this BUY grew
    the existing stake by 30%", ``-25%`` as "this SELL trimmed
    it by 25%". The minus glyph is the typographically
    correct ``\u2212`` (U+2212), not the ASCII hyphen-minus.
    """
    category = event["category"]
    if category in _TRADE_DETAIL_LABELS:
        return _TRADE_DETAIL_LABELS[category]
    delta_pct = event.get("delta_pct")
    if delta_pct is None:
        return _TRADE_ACTION_DISPLAY[category][0]
    sign = "+" if category == "INCREASE" else "\u2212"
    return f"{sign}{delta_pct:.0f}%"


def build_row(event: dict) -> str:
    """Render one burst-aggregated trade as a ``<tr>``.

    Five columns: ticker (without exchange prefix), company
    name, action badge (Bought / Sold), details (initial stake
    / signed percentage / disposal), date / range, per-share
    price. ``data-sort-*`` attributes carry the sort key for
    each sortable column so the inline trades-sort script can
    re-order rows without re-parsing cell text. The per-share
    price stays in the security's native currency (e.g. ``EUR
    76.32``); we use the ISO code rather than the symbol because
    a leading ``$`` would silently misrepresent a EUR / GBp
    trade as USD in a multi-market portfolio. Nominal share
    counts are deliberately absent: the page commits to
    publishing only relative percentages and per-share prices,
    never sizes.
    """
    category = event["category"]
    action_label, action_modifier = _TRADE_ACTION_DISPLAY[category]
    detail_label = _detail_text(event)
    # The two "boundary" labels (Initial stake / Disposal) are
    # qualitative; the magnitude rows (+30% / -25%) are
    # quantitative and benefit from a tabular-numbers
    # treatment plus a sign-driven colour cue.
    if category in ("INCREASE", "DECREASE"):
        detail_modifier = "pct"
        detail_class = (
            "trades__detail trades__detail--pct "
            + ("value--positive" if category == "INCREASE" else "value--negative")
        )
    else:
        detail_modifier = "label"
        detail_class = "trades__detail trades__detail--label"
    start = event["start_date"]
    end = event["end_date"]
    # Quarter-granularity timing -- see ``_fmt_quarter_range``
    # for the layout rules. The row-level ``data-sort-date``
    # still carries the burst's ISO end date below, so sorting
    # by date stays fine-grained even though the visible label
    # is coarse.
    period_html = _fmt_quarter_range(start, end)
    price_html = html.escape(
        f"{event['price']:,.2f} {event['currency']}"
    )
    symbol = strip_exchange(event["ticker"])
    name = event["name"]
    sort_date = end.strftime("%Y-%m-%d")
    sort_ticker = symbol.lower()
    sort_name = name.lower()
    sort_action = TRADE_ACTION_SORT_INDEX[category]
    sort_detail = TRADE_DETAIL_SORT_INDEX[category]
    return (
        '<tr class="trades__row"'
        f' data-sort-date="{sort_date}"'
        f' data-sort-ticker="{html.escape(sort_ticker)}"'
        f' data-sort-name="{html.escape(sort_name)}"'
        f' data-sort-action="{sort_action}"'
        f' data-sort-detail="{sort_detail}">'
        f'<td class="trades__cell trades__cell--ticker">{html.escape(symbol)}</td>'
        f'<td class="trades__cell trades__cell--name">{html.escape(name)}</td>'
        '<td class="trades__cell trades__cell--action">'
        f'<span class="trade__badge trade__badge--{action_modifier}">'
        f'{html.escape(action_label)}</span>'
        '</td>'
        '<td class="trades__cell trades__cell--detail">'
        f'<span class="{detail_class}" '
        f'data-detail-kind="{detail_modifier}">'
        f'{html.escape(detail_label)}</span>'
        '</td>'
        f'<td class="trades__cell trades__cell--date">{period_html}</td>'
        f'<td class="trades__cell trades__cell--price">{price_html}</td>'
        '</tr>'
    )


def build_table(rows: list[str]) -> str:
    """Wrap pre-rendered ``<tr>`` fragments in the sortable
    ``<table>`` and add the "Show all" toggle when the log is
    longer than the default visible window.

    The header row exposes click-to-sort buttons on the ticker,
    company, action, details, and date columns. The default
    sort (the order the rows are emitted in) is by date
    descending so the most recent activity sits at the top
    before the user touches anything.
    """
    headers: list[str] = []
    for key, label, modifier in SORTABLE_COLUMNS:
        headers.append(
            f'<th class="trades__col {modifier}" scope="col" '
            f'data-sort-key="{key}" aria-sort="none">'
            f'<button type="button" class="trades__sort">'
            f'{html.escape(label)}'
            '<span class="trades__sort-indicator" aria-hidden="true"></span>'
            '</button></th>'
        )
    # Price column is not sortable -- mixing currencies in a
    # numeric sort would imply a meaningful ordering across
    # USD / EUR / GBp etc. that doesn't exist without an FX
    # conversion.
    headers.append(
        '<th class="trades__col trades__col--price" scope="col">Price</th>'
    )
    thead = f'<thead><tr>{"".join(headers)}</tr></thead>'
    tbody = f'<tbody>{"".join(rows)}</tbody>'
    table_html = (
        '<div class="trades__wrap">'
        '<table class="trades" '
        'data-sort-default="date" '
        'data-sort-default-dir="desc">'
        f'{thead}{tbody}'
        '</table>'
        '</div>'
    )
    toggle_html = ""
    total = len(rows)
    if total > VISIBLE_DEFAULT:
        toggle_html = (
            '<button type="button" class="trades__toggle" '
            f'data-total="{total}" aria-expanded="false">'
            f'Show all {total} trades</button>'
        )
    return table_html + toggle_html
