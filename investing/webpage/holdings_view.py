"""Holdings card + per-section "Sort by" toolbar renderers.

Each ``<article class="holding">`` capsule shows the ticker /
name title, the period(s) the position was open, and a stats
list (TSR / CAGR / Weight). The toolbar rendered above each
list (Current / Historical) is responsible for the click-to-sort
controls -- the per-row ``data-sort-*`` attributes emitted here
feed the inline ``_HOLDINGS_SORT_SCRIPT`` so a click on a sort
button can reorder cards without rerunning Python.

The module is intentionally view-only: no FX, no aggregation,
no Logo lookup of its own. Callers pass the resolved logo URL
in -- the renderer's ``_get_logo_url`` continues to own the
per-page logo cache.
"""
from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Mapping

from ..errors import InvariantError
from ..formatting import _fmt_date, _fmt_pct, _format_sort_number, _value_class
from ..holdings import CAGR_TBA_THRESHOLD
from .anchors import holding_anchor

# Sort options surfaced above each holdings list. ``key`` is the
# ``data-holdings-sort-key`` consumed by the holdings-sort
# script and matched against the ``data-sort-<key>`` attribute
# on each ``<article class="holding">``; ``label`` is the
# displayed text; ``kind`` controls the default direction the
# JS picks the first time the user activates a column ("text"
# -> ascending, "number" -> descending). The "default" key
# special-cases the restore-DOM-order button.
SORT_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("default", "Default", "default"),
    ("ticker",  "Ticker",  "text"),
    ("name",    "Name",    "text"),
    ("tsr",     "TSR",     "number"),
    ("cagr",    "CAGR",    "number"),
    ("weight",  "Weight",  "number"),
)


def build_sort_control(*, scope: str, include_weight: bool) -> str:
    """Render the per-section "Sort by" toolbar above a holdings list.

    ``scope`` is the value the wrapping
    ``data-holdings-list="..."`` element carries on its inner
    list, used by the inline sort script to wire each toolbar
    to its own list independently. ``include_weight`` controls
    whether the "Weight" button is rendered -- it is meaningless
    for historical holdings (no current weight) so the
    historical toolbar omits it.

    The "Default" button is rendered as the active option on
    first paint to mirror the order ``get_holdings`` already
    emits (most recent buy / most recent sell first).
    """
    buttons: list[str] = []
    for key, label, kind in SORT_OPTIONS:
        if key == "weight" and not include_weight:
            continue
        is_default = key == "default"
        indicator_html = (
            ""
            if is_default
            else '<span class="holdings__sort-indicator" aria-hidden="true">'
                 '</span>'
        )
        buttons.append(
            f'<button type="button" class="holdings__sort-btn" '
            f'data-holdings-sort-key="{key}" '
            f'data-holdings-sort-kind="{kind}" '
            f'aria-pressed="{"true" if is_default else "false"}" '
            f'aria-sort="none">'
            f'{html.escape(label)}{indicator_html}'
            '</button>'
        )
    scope_label = "current" if scope == "current" else "historical"
    return (
        f'<div class="holdings__sort" role="group" '
        f'aria-label="Sort {scope_label} holdings" '
        f'data-holdings-sort="{html.escape(scope)}">'
        '<span class="holdings__sort-label" aria-hidden="true">'
        'Sort by'
        '</span>'
        f'{"".join(buttons)}'
        '</div>'
    )


def build_card(
    *,
    logo_url: str,
    title: str,
    stats: Iterable[tuple[str, str, float | None]],
    periods: Iterable[tuple] | None = None,
    note: str | None = None,
    card_id: str | None = None,
    data_attrs: Mapping[str, str] | None = None,
) -> str:
    """Render a capsule with logo, title/period(s)/note, and right-aligned stats.

    ``data_attrs`` is an optional mapping of ``data-*`` attribute
    names (without the ``data-`` prefix) to string values that
    will be emitted on the outer ``<article>``. Used by the
    holdings sort control to read per-card sort keys (ticker,
    name, TSR, CAGR, weight) without having to re-parse the
    rendered card body.
    """
    body_parts = [f'<h3 class="holding__title">{html.escape(title)}</h3>']
    if periods:
        # Always render the most-recent period first so it sits
        # at the top of the visual stack. ``Holding.summary``
        # already returns newest-first in production, but
        # preview / synthetic data and any future call site might
        # not; the visual order is a UX guarantee.
        ordered = sorted(periods, key=lambda p: p[0], reverse=True)
        items = []
        for start, end in ordered:
            start_html = (
                f'<time datetime="{start.strftime("%Y-%m-%d")}">'
                f'{_fmt_date(start)}</time>'
            )
            if end is None:
                end_html = '<span>Present</span>'
            else:
                end_html = (
                    f'<time datetime="{end.strftime("%Y-%m-%d")}">'
                    f'{_fmt_date(end)}</time>'
                )
            items.append(
                f'<li>{start_html}<span>-</span>{end_html}</li>'
            )
        body_parts.append(
            f'<ul class="holding__periods">{"".join(items)}</ul>'
        )
    if note:
        body_parts.append(
            f'<p class="holding__note">{html.escape(note)}</p>'
        )

    stat_parts = []
    for label, value, sign in stats:
        attr = ""
        if sign is not None:
            attr = f' class="{_value_class(sign)}"'
        stat_parts.append(
            '<div class="holding__stat">'
            f'<dt>{html.escape(label)}</dt>'
            f'<dd{attr}>{html.escape(value)}</dd>'
            '</div>'
        )

    id_attr = f' id="{html.escape(card_id)}"' if card_id else ""
    data_attr_html = ""
    if data_attrs:
        # Emit attributes in a stable order so the rendered
        # markup is deterministic across calls; ``dict``
        # preserves insertion order, but the explicit ``sorted``
        # pass keeps the output reproducible regardless of how
        # the caller built the mapping.
        data_attr_html = "".join(
            f' data-{key}="{html.escape(data_attrs[key])}"'
            for key in sorted(data_attrs)
        )
    return (
        f'<article class="holding"{id_attr}{data_attr_html}>'
        # Below-the-fold logos load lazily; explicit dimensions
        # reserve space and keep CLS at zero.
        f'<img class="holding__logo" src="{html.escape(logo_url)}" '
        'alt="" loading="lazy" decoding="async" '
        'width="64" height="64">'
        f'<div class="holding__body">{"".join(body_parts)}</div>'
        f'<dl class="holding__stats">{"".join(stat_parts)}</dl>'
        '</article>'
    )


def build_holding_card(
    holding: dict,
    *,
    logo_url_for: Callable[[str], str],
) -> str:
    """Convenience wrapper that derives the card kwargs from a holding dict.

    ``logo_url_for`` is the renderer's per-page logo cache
    accessor (typically ``Webpage._get_logo_url``); passing it
    in keeps the view module logo-cache-agnostic.
    """
    stats: list[tuple[str, str, float | None]] = [
        # ``tsr%``/``cagr%``/``current_weight%`` are unrounded
        # floats; ``_fmt_pct`` chooses one decimal under 100 and
        # whole-number from 100 up. The raw float still flows to
        # ``_value_class`` for sign-based colouring.
        ("TSR:", f"{_fmt_pct(holding['tsr%'])}%", holding["tsr%"]),
    ]
    if holding["cagr%"] > CAGR_TBA_THRESHOLD:
        stats.append(("CAGR:", "TBA", None))
    else:
        stats.append(
            ("CAGR:", f"{_fmt_pct(holding['cagr%'])}%", holding["cagr%"]),
        )
    if holding["is_current"]:
        weight = holding["current_weight%"]
        if weight is None:
            raise InvariantError(
                f"current holding {holding['ticker']!r} reached the "
                "renderer with no weight -- summarize() did not run",
            )
        stats.append(("Weight:", f"{_fmt_pct(weight)}%", None))

    periods = [(p["start"], p["end"]) for p in holding["periods"]]
    # The ticker key drops the exchange prefix so ordering by
    # "Ticker" reads as an alphabetical run of company symbols
    # (NVDA before SPGI). The displayed title still carries the
    # ``EXCHANGE:SYMBOL`` form so the row is unambiguous.
    ticker_key = holding["ticker"].rsplit(":", 1)[-1].casefold()
    sort_attrs: dict[str, str] = {
        "sort-ticker": ticker_key,
        "sort-name": holding["name"].casefold(),
        "sort-tsr": _format_sort_number(holding["tsr%"]),
        "sort-cagr": _format_sort_number(holding["cagr%"]),
    }
    if holding["is_current"]:
        sort_attrs["sort-weight"] = _format_sort_number(
            holding["current_weight%"]
        )

    return build_card(
        logo_url=logo_url_for(holding["ticker"]),
        title=f'{holding["ticker"]} - {holding["name"]}',
        stats=stats,
        periods=periods,
        card_id=holding_anchor(holding["ticker"]),
        data_attrs=sort_attrs,
    )
