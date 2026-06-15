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
from ..holdings import CAGR_TBA_THRESHOLD, google_search_url
from ..safehtml import SafeHtml, escape
from .anchors import holding_anchor


def _fmt_holding_pct_html(value: float) -> SafeHtml:
    """Format a Return / IRR percentage with a CSS-hidable decimal.

    On wider viewports the Return / IRR rows of a holding capsule
    stack vertically, leaving the right column of the stats grid
    plenty of room for the trailing ``.X`` digit even when the
    integer portion has reached three figures. On narrower
    viewports the same metrics reflow into a horizontal row
    alongside Weight (the ``@media (max-width: 540px)`` block in
    ``page.css``), where ``100.0`` / ``217.4`` would crowd the
    3-column grid and the original ``_fmt_pct`` truncation to
    plain ``100`` / ``217`` reads tidier.

    The helper emits both shapes from a single DOM node: when the
    rounded magnitude reaches 100 it wraps the ``.X`` tail in a
    ``<span class="holding__decimal">`` element that the mobile
    media query hides via ``display: none``. Under 100 the
    formatter returns the bare ``.1f`` text (every viewport keeps
    the decimal there because the integer part is at most two
    digits, so the extra precision doesn't crowd the row).

    Boundary handling mirrors :func:`_fmt_pct` -- ``round(abs(value),
    1) >= 100`` catches values that round UP to 100 (e.g. ``99.96``
    -> ``100.0``) so they shed the decimal on mobile alongside the
    natively-3-digit cases.
    """
    full = format(value, ".1f")
    if round(abs(value), 1) < 100:
        return SafeHtml(html.escape(full))
    # ``format(..., ".1f")`` always emits ``<int>.<digit>``, so the
    # split is unconditional and the wrapper carries the leading
    # ``.`` so the desktop layout reads as a continuous number while
    # the mobile rule simply drops the wrapper.
    integer_part, decimal_part = full.rsplit(".", 1)
    return SafeHtml(
        f"{html.escape(integer_part)}"
        f'<span class="holding__decimal">.{html.escape(decimal_part)}</span>'
    )

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
    ("ticker", "Ticker", "text"),
    ("name", "Name", "text"),
    # ``tsr`` and ``cagr`` are kept as the sort *keys* (they match
    # the ``data-sort-tsr`` / ``data-sort-cagr`` attributes the
    # holdings-sort script reads, and the on-disk DOM order tests
    # pin those names). The visible labels read "Return" and "IRR"
    # because the underlying figures are now MoIC-based and IRR-
    # based rather than TWR/CAGR -- see the disclaimer methodology
    # bullet for the full rationale.
    ("tsr", "Return", "number"),
    ("cagr", "IRR", "number"),
    ("weight", "Weight", "number"),
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
            else '<span class="holdings__sort-indicator" aria-hidden="true"></span>'
        )
        buttons.append(
            f'<button type="button" class="holdings__sort-btn" '
            f'data-holdings-sort-key="{key}" '
            f'data-holdings-sort-kind="{kind}" '
            f'aria-pressed="{"true" if is_default else "false"}" '
            f'aria-sort="none">'
            f"{html.escape(label)}{indicator_html}"
            "</button>"
        )
    # Aria label flexes per scope so screen readers announce the
    # sub-section the toolbar reorders ("current equity holdings" /
    # "current fixed income holdings" / "historical equity
    # holdings" / "historical fixed income holdings"). Falls back
    # to a generic "current" / "historical" wording when the scope
    # doesn't carry an explicit asset-class suffix, preserving the
    # historical equity-only labelling for the legacy
    # ``"current"`` / ``"historical"`` scopes.
    if scope.startswith("current"):
        scope_label = "current"
    elif scope.startswith("historical"):
        scope_label = "historical"
    else:
        scope_label = scope
    if scope.endswith("-fixed-income"):
        scope_label += " fixed income"
    elif scope in ("current", "historical"):
        # Pure-equity legacy scope: keep the historical wording so
        # tests and existing snapshots don't churn on the new
        # explicit "equity" qualifier.
        pass
    else:
        scope_label += " equity"
    return (
        f'<div class="holdings__sort" role="group" '
        f'aria-label="Sort {scope_label} holdings" '
        f'data-holdings-sort="{html.escape(scope)}">'
        '<span class="holdings__sort-label" aria-hidden="true">'
        "Sort by"
        "</span>"
        f"{''.join(buttons)}"
        "</div>"
    )


def build_card(
    *,
    logo_url: str,
    title: str,
    stats: Iterable[tuple[str, str | SafeHtml, float | None]],
    periods: Iterable[tuple] | None = None,
    note: str | None = None,
    card_id: str | None = None,
    data_attrs: Mapping[str, str] | None = None,
    website_url: str | None = None,
    company_name: str | None = None,
) -> str:
    """Render a capsule with logo, title/period(s)/note, and right-aligned stats.

    ``data_attrs`` is an optional mapping of ``data-*`` attribute
    names (without the ``data-`` prefix) to string values that
    will be emitted on the outer ``<article>``. Used by the
    holdings sort control to read per-card sort keys (ticker,
    name, return, IRR, weight) without having to re-parse the
    rendered card body. The attribute names use the historical
    ``tsr`` / ``cagr`` keys to keep the JS contract stable; only
    the visible labels and the underlying formulas have moved
    to MoIC / XIRR semantics.

    Stat values are HTML-escaped by default; callers that need to
    embed inline markup (e.g. the ``<span class="holding__decimal">``
    wrapper emitted by :func:`_fmt_holding_pct_html` so the mobile
    layout can hide the trailing ``.X``) can pass a :class:`SafeHtml`
    value to bypass escaping. The :func:`escape` helper is idempotent
    on :class:`SafeHtml`, so the call site stays a single line.

    ``website_url`` is the click target wired onto the capsule's
    logo wrapper. When provided, the ``<img>`` is wrapped in an
    ``<a>`` so the company logo doubles as a navigation affordance
    (typically the issuer's own site, falling back to investor
    relations and then to a Google search via
    :func:`investing.holdings.resolve_company_url`). ``company_name``
    is the human-readable label used for the link's
    ``aria-label`` / ``title`` so screen readers and tooltips
    announce ``"Open <name> website"`` rather than the bare URL.
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
            start_html = f'<time datetime="{start.strftime("%Y-%m-%d")}">{_fmt_date(start)}</time>'
            if end is None:
                end_html = "<span>Present</span>"
            else:
                end_html = f'<time datetime="{end.strftime("%Y-%m-%d")}">{_fmt_date(end)}</time>'
            items.append(f"<li>{start_html}<span>-</span>{end_html}</li>")
        body_parts.append(f'<ul class="holding__periods">{"".join(items)}</ul>')
    if note:
        body_parts.append(f'<p class="holding__note">{html.escape(note)}</p>')

    stat_parts = []
    for label, value, sign in stats:
        attr = ""
        if sign is not None:
            attr = f' class="{_value_class(sign)}"'
        stat_parts.append(
            '<div class="holding__stat">'
            f"<dt>{html.escape(label)}</dt>"
            f"<dd{attr}>{escape(value)}</dd>"
            "</div>"
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
            f' data-{key}="{html.escape(data_attrs[key])}"' for key in sorted(data_attrs)
        )
    # Below-the-fold logos load lazily; explicit dimensions
    # reserve space and keep CLS at zero.
    img_html = (
        f'<img class="holding__logo" src="{html.escape(logo_url)}" '
        'alt="" loading="lazy" decoding="async" '
        'width="64" height="64">'
    )
    if website_url:
        # Decorative ``alt=""`` on the inner ``<img>`` means the
        # link itself needs an accessible name; ``aria-label`` /
        # ``title`` carry that and a sighted-mouse tooltip without
        # changing the existing visual layout. ``target="_blank"``
        # opens the issuer site in a new tab so the reader doesn't
        # lose their place on the portfolio page; ``rel="noopener
        # noreferrer"`` blocks the target page from reaching back
        # into ``window.opener`` (the OWASP "tabnabbing" mitigation
        # the page already applies to the Yahoo Finance footer
        # link).
        label_source = company_name or "company"
        label = f"Open {label_source} website"
        logo_html = (
            f'<a class="holding__logo-link" href="{html.escape(website_url)}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'aria-label="{html.escape(label)}" title="{html.escape(label)}">'
            f"{img_html}"
            "</a>"
        )
    else:
        logo_html = img_html
    return (
        f'<article class="holding"{id_attr}{data_attr_html}>'
        f"{logo_html}"
        f'<div class="holding__body">{"".join(body_parts)}</div>'
        f'<dl class="holding__stats">{"".join(stat_parts)}</dl>'
        "</article>"
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
    stats: list[tuple[str, str | SafeHtml, float | None]] = [
        # ``tsr%``/``cagr%``/``current_weight%`` are unrounded
        # floats. Return and IRR run through
        # ``_fmt_holding_pct_html`` so the ``.X`` decimal renders
        # on the desktop vertical stack but the mobile horizontal
        # row drops it via the ``.holding__decimal`` CSS hide rule.
        # Weight keeps the plain ``_fmt_pct`` formatter (weights are
        # always under 100 in practice, so the decimal-wrapping
        # branch never fires there anyway). The raw float still
        # flows to ``_value_class`` for sign-based colouring.
        #
        # The visible labels read "Return" (cumulative MoIC - 1)
        # and "IRR" (annualised XIRR over the holding's actual
        # cashflow series). The dict keys ``tsr%``/``cagr%`` are
        # retained so the OG image / sort attrs / capsule layout
        # don't churn; the methodology bullet in the footer
        # disclaimer carries the formula change.
        ("Return:", SafeHtml(f"{_fmt_holding_pct_html(holding['tsr%'])}%"), holding["tsr%"]),
    ]
    if holding["cagr%"] > CAGR_TBA_THRESHOLD:
        stats.append(("IRR:", "TBA", None))
    else:
        stats.append(
            (
                "IRR:",
                SafeHtml(f"{_fmt_holding_pct_html(holding['cagr%'])}%"),
                holding["cagr%"],
            ),
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
        sort_attrs["sort-weight"] = _format_sort_number(holding["current_weight%"])

    # ``Holding.summary`` always fills ``website`` (issuer site ->
    # IR site -> Google search on company name); the ``.get`` +
    # local fallback here is a renderer-side safety net for
    # synthetic preview / test dicts that don't go through the
    # production summary path. Either way the link's ``href`` is
    # never empty so the wrapper always routes a click somewhere
    # actionable.
    website_url = holding.get("website") or google_search_url(holding["name"])

    return build_card(
        logo_url=logo_url_for(holding["ticker"]),
        title=f"{holding['ticker']} - {holding['name']}",
        stats=stats,
        periods=periods,
        card_id=holding_anchor(holding["ticker"]),
        data_attrs=sort_attrs,
        website_url=website_url,
        company_name=holding["name"],
    )
