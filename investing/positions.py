"""Turn per-ticker :class:`~investing.holdings.Holding` objects into
one summary per *economic position*.

Most positions are one ticker and pass straight through. When
``position_groups.toml`` declares that several listings are the same
company (see :mod:`investing.position_groups`), their legs are merged
into a single :class:`~investing.types.HoldingSummary` here.

The merge happens on the USD cashflow timeline, never on the finished
percentages -- :class:`~investing.holdings.PositionLedger` documents
why (short version: an XIRR cannot be recovered by weighting two
IRRs). What this module adds on top is *identity*: which listing's
logo, anchor, sector and issuer URL the combined position inherits,
and what it is called.

Trade events are untouched. They are collected per-leg in
:func:`investing.performance.get_holdings` and stay per-leg on the
rendered page: a trade happened against one specific listing at one
specific price in one specific currency, and flattening that would be
a loss of real information rather than a simplification.
"""

from __future__ import annotations

from collections.abc import Iterable

from .errors import InvariantError
from .holdings import Holding, ledger_metrics, merge_ledgers
from .position_groups import PositionGroup, resolve_groups
from .types import HoldingSummary, TradeEvent


def _short_symbol(ticker: str) -> str:
    """``DUS:SSU.DU`` -> ``SSU.DU``.

    Duplicates :func:`investing.webpage.anchors.strip_exchange` rather
    than importing it: that module is renderer-side, and the data
    layer importing from ``investing.webpage`` would invert the
    dependency direction the rest of the package maintains.
    :mod:`investing.sector_overrides` duplicates its sector list for
    the same reason.
    """
    _, _, symbol = ticker.partition(":")
    return symbol or ticker


def _combined_summary(
    group: PositionGroup,
    legs: list[Holding],
) -> HoldingSummary:
    """Fold one group's legs into a single summary.

    Identity comes from the primary leg; the numbers come from the
    merged ledger. Every leg is required to agree on ``asset_class``
    -- an equity grouped with a bond ETF would land in one of the
    renderer's two sections while its cashflows came from both, so
    the mismatch is raised rather than silently resolved.

    Only the primary's :meth:`~investing.holdings.Holding.summary` is
    built. The other legs contribute a ledger and nothing else: their
    name, website and sector are all discarded in favour of the
    primary's, so summarising them would redo the cashflow walk *and*
    record maintenance hints (e.g. "no sector for IOB:SMSN.IL") for
    values this position never reads -- hints the notifier would then
    turn into GitHub issues nobody can act on.
    """
    by_ticker = {holding.canonical_ticker: holding for holding in legs}
    primary = by_ticker.get(group.primary)
    if primary is None:
        raise InvariantError(
            f"position group {group.key!r} has no held leg matching its primary {group.primary!r}",
        )

    asset_classes = {holding.asset_class for holding in legs}
    if len(asset_classes) > 1:
        raise InvariantError(
            f"position group {group.key!r} mixes asset classes "
            f"{sorted(asset_classes)} -- every leg of a position must share one",
        )

    primary_summary = primary.summary()
    merged = merge_ledgers([holding.ledger() for holding in legs])
    tsr_pct, cagr_pct = ledger_metrics(merged)
    if merged.latest_buy is None:
        raise InvariantError(
            f"position group {group.key!r} has no recorded BUY across its legs",
        )

    # Order the ticker list the way the group declares it (primary
    # first), keeping only legs actually held -- ``resolve_groups``
    # has already narrowed ``group.members`` to those.
    ordered = [t for t in group.tickers if t in by_ticker]

    return {
        # The primary's id: it keys the logo file, the anchor, and the
        # portfolio weights map, so the combined position has to carry
        # it verbatim rather than inventing a synthetic identifier.
        "ticker": group.primary,
        "name": group.name or primary_summary["name"],
        "tsr%": tsr_pct,
        "cagr%": cagr_pct,
        "is_current": merged.is_current,
        "current_weight%": None,
        "current_value_usd": merged.current_value_usd,
        "periods": merged.periods,
        "latest_buy": merged.latest_buy,
        "latest_sell": merged.latest_sell,
        "website": primary_summary.get("website", ""),
        "sector": primary_summary.get("sector", ""),
        "asset_class": next(iter(asset_classes)),
        "tickers": ordered,
        "short_label": group.label or _short_symbol(group.primary),
    }


def build_position_summaries(
    holdings: Iterable[Holding],
    *,
    groups_path: str | None = None,
) -> list[HoldingSummary]:
    """One summary per position, combining grouped listings.

    ``groups_path`` overrides the config location for tests; production
    leaves it ``None`` and reads ``position_groups.toml`` from the repo
    root.

    Group membership is resolved from the cheap
    :attr:`~investing.holdings.Holding.canonical_ticker` before any
    summary is built, so an ungrouped holding is summarised exactly
    once and a merged-away leg is never summarised at all. A portfolio
    with no config file (or no applicable group) therefore produces
    byte-identical output, and the same maintenance hints, as the
    pre-grouping pipeline.
    """
    legs = list(holdings)
    held = {holding.canonical_ticker for holding in legs}
    groups = resolve_groups(held, path=groups_path)
    if not groups:
        return [holding.summary() for holding in legs]

    group_by_ticker: dict[str, PositionGroup] = {}
    for group in groups:
        for ticker in group.tickers:
            group_by_ticker[ticker] = group
    legs_by_group: dict[str, list[Holding]] = {}
    for holding in legs:
        owner = group_by_ticker.get(holding.canonical_ticker)
        if owner is not None:
            legs_by_group.setdefault(owner.key, []).append(holding)

    summaries: list[HoldingSummary] = []
    emitted: set[str] = set()
    for holding in legs:
        owner = group_by_ticker.get(holding.canonical_ticker)
        if owner is None:
            summaries.append(holding.summary())
            continue
        if owner.key in emitted:
            # A later leg of a group already folded into the combined
            # position emitted at the first leg's slot. ``get_holdings``
            # re-sorts by trade recency afterwards, so which slot the
            # combined entry occupies here doesn't affect the page.
            continue
        emitted.add(owner.key)
        summaries.append(_combined_summary(owner, legs_by_group[owner.key]))
    return summaries


def apply_group_trade_names(
    summaries: Iterable[HoldingSummary],
    trade_events: Iterable[TradeEvent],
) -> None:
    """Rewrite grouped legs' trade rows to the position's display name.

    The Trades table keeps one row per listing -- that is the point of
    the section -- but each leg arrives carrying its own yfinance
    ``longName``, and those differ between listings of one company
    ("Samsung Electronics Co Ltd" for the Düsseldorf line, "Samsung
    Electronics Co., Ltd." for the London GDR). Sorting the table by
    Name would scatter the company's activity across two places.
    Normalising the label keeps the rows together while the Ticker
    column continues to say exactly which listing each trade hit.

    Reads the combined summaries rather than re-reading the config, so
    a leg is renamed only when its group was actually applied -- a
    group with just one held leg renders as an ordinary holding and
    keeps the name its capsule shows.

    Mutates ``trade_events`` in place; a portfolio with no combined
    positions is a no-op.
    """
    name_by_ticker: dict[str, str] = {}
    for summary in summaries:
        tickers = summary.get("tickers")
        if not tickers:
            continue
        for ticker in tickers:
            name_by_ticker[ticker] = summary["name"]
    if not name_by_ticker:
        return
    for event in trade_events:
        name = name_by_ticker.get(event.get("ticker", ""))
        if name is not None:
            event["name"] = name
