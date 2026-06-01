"""``Trade`` records and the burst-aggregation logic that
powers the "Trades" section.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .errors import InvariantError
from .types import EquityTransaction

# Public surface of this module. The leading-underscore display tables
# (``_BUY_CATEGORIES`` / ``_TRADE_ACTION_DISPLAY`` / ``_TRADE_DETAIL_LABELS``)
# are imported by ``investing.webpage.trades_view`` and
# ``investing.webpage._page``; ``__all__`` is the canonical opt-in
# that tells CodeQL's ``py/unused-global-variable`` query they're
# cross-module exports rather than module-local bindings the leading
# underscore would otherwise imply.
__all__ = [
    "ACTIONS",
    "TRADE_WINDOW_DAYS",
    "_BUY_CATEGORIES",
    "_TRADE_ACTION_DISPLAY",
    "_TRADE_DETAIL_LABELS",
    "Trade",
    "_combine_trade_events",
    "combine_and_sort",
]


@dataclass
class Trade:
    date: datetime
    ticker: str
    quantity: int
    price: float
    action: str


ACTIONS = ["BUY", "SELL"]


def combine_and_sort(transactions: list[EquityTransaction]) -> list[Trade]:
    """Bucket transactions by (ticker, date, action), then aggregate each
    bucket into a single :class:`Trade` whose price is the volume-weighted
    average of its constituents.

    The result is sorted by ``(date, action)`` so that on intraday tie-breaks
    BUYs are processed before SELLs (matters for tax-loss harvesting cases).
    """
    buckets: dict[tuple[str, str, str], list[EquityTransaction]] = defaultdict(list)
    for txn in transactions:
        if txn["action"] not in ACTIONS:
            # Should be unreachable -- ``_parse_equity_row`` already
            # normalises action tokens against the same set. Treat as
            # an internal invariant rather than a sheet-parse error so
            # the maintainer can chase down the upstream regression.
            raise InvariantError(
                f"transaction action {txn['action']!r} is not one of {ACTIONS}",
            )
        buckets[(txn["ticker"], txn["date"], txn["action"])].append(txn)

    trades: list[Trade] = []
    for (ticker, date, action), txns in buckets.items():
        # Single pass over the bucket: the historical ``sum(...) +
        # sum(...)`` pair walked the same list twice for the volume-
        # weighted average. One loop accumulates both running totals
        # and is what every other reduction in this file already does.
        total_quantity = 0
        total_value = 0.0
        for t in txns:
            qty = t["quantity"]
            total_quantity += qty
            total_value += qty * t["price_per_share"]
        trades.append(
            Trade(
                date=datetime.strptime(date, "%d-%m-%Y"),
                ticker=ticker,
                quantity=total_quantity,
                price=total_value / total_quantity,
                action=action,
            )
        )

    return sorted(trades, key=lambda t: (t.date, t.action))


# ---------------------------------------------------------------------------
# Recent-trades aggregation
# ---------------------------------------------------------------------------
#
# Each ``Holding`` records a raw "trade event" for every BUY/SELL it
# processes, tagged with one of four semantic categories that describe
# what the trade did to the position:
#
#   OPEN     - first BUY after the position was empty (0 -> >0)
#   INCREASE - BUY on top of an existing position (>0 -> >0)
#   DECREASE - SELL that leaves a non-zero residual position (>0 -> >0)
#   CLOSE    - SELL that brings the position back to zero (>0 -> 0)
#
# Bursts of small same-action trades within a rolling 90-day window get
# folded into a single reported trade with a volume-weighted average
# per-share price -- the granularity that matters to the reader is "did
# the position open / grow / shrink / close around this time?", not
# every individual fill. 90 days approximates a fiscal quarter, which
# is the natural cadence for a long-term-investor portfolio: a stake
# accumulated through three or four tranches over a quarter reads as a
# single deliberate action, not four separate trades.

TRADE_WINDOW_DAYS = 90


# Reading these as a buy-vs-sell action partitions the four categories
# along the only axis that matters for grouping (same-action trades go
# together) and for picking the group's effective category (first event
# decides BUY bursts, last event decides SELL bursts).
_BUY_CATEGORIES = frozenset({"OPEN", "INCREASE"})


# Display tables used by the trades-section renderer.
#
# The four semantic categories collapse onto a single buy-vs-sell
# axis for the user-facing "Action" column -- a long-term-investor
# trade log only needs the reader to spot direction at a glance; the
# finer "was this the first fill or a top-up?" granularity lives in
# the "Details" column instead. Past-tense verbs ("Bought" / "Sold")
# match the executed-trades framing -- everything shown has already
# happened. The BEM modifiers (``buy`` / ``sell``) drive the green /
# red pill fills and stay aligned with the action axis so the
# stylesheet keeps describing exactly what the badge marks.
_TRADE_ACTION_DISPLAY: dict[str, tuple[str, str]] = {
    "OPEN": ("Bought", "buy"),
    "INCREASE": ("Bought", "buy"),
    "DECREASE": ("Sold", "sell"),
    "CLOSE": ("Sold", "sell"),
}


# Static "Details" labels for the boundary events. INCREASE and
# DECREASE carry a magnitude percentage in the same column, computed
# at render time from ``event["delta_pct"]`` -- a relative move
# ("+30%", "-25%") describes the scale of the action without ever
# leaking the nominal share count, which the page deliberately
# keeps private. The two boundary labels stay in the past-tense
# fund-letter idiom the rest of the page uses ("Initiated" for
# the first fill that brings the position into existence,
# "Divested" for the trade that closes it out), so the Details
# column reads as a tight verb log next to the magnitude rows
# rather than mixing noun phrases like "Initial stake" with
# percentage values.
_TRADE_DETAIL_LABELS: dict[str, str] = {
    "OPEN": "Initiated",
    "CLOSE": "Divested",
}


def _combine_trade_events(
    events: list[dict],
    *,
    window_days: int = TRADE_WINDOW_DAYS,
) -> list[dict]:
    """Fold a ticker's raw trade events into burst-level rows.

    Walks ``events`` chronologically and joins each event to the
    running group iff (a) the group has the same action (BUY/SELL),
    and (b) the span between the group's first event and the new event
    is at most ``window_days``. Anchoring on the FIRST event (rather
    than the most recent) caps each combined burst at ~one fiscal
    quarter -- the user-facing meaning of "rolling quarter" here is
    "a contiguous run of small trades whose first-to-last span fits
    inside a 90-day window", not a sliding window that can keep
    extending indefinitely as long as consecutive trades stay close.

    Each combined record carries:

    * ``start_date`` / ``end_date`` -- first and last event in the burst;
    * ``price``                     -- volume-weighted average of the burst;
    * ``category``                  -- ``OPEN`` / ``INCREASE`` for BUYs and
                                       ``DECREASE`` / ``CLOSE`` for SELLs.

    Category resolution follows the boundary that matters semantically:
    a BUY burst is "Initiated" if the first event opened the position
    (regardless of any subsequent INCREASEs that piled on within the
    window); a SELL burst is "Divested" if the last event zeroed the
    position out (regardless of preceding partial DECREASEs).
    """
    if not events:
        return []
    events = sorted(events, key=lambda e: e["date"])
    groups: list[list[dict]] = []
    # ``head_action`` is invariant across the lifetime of a group, so
    # we cache it on the side rather than recomputing from
    # ``head["category"]`` on every event. Tiny saving in absolute
    # terms; the win is reading the loop top-to-bottom without an
    # implicit "what does the head look like?" branch.
    head_action: str | None = None
    head_date = None
    for event in events:
        action = "BUY" if event["category"] in _BUY_CATEGORIES else "SELL"
        if groups and head_action == action:
            within_window = (event["date"] - head_date).days <= window_days
            if within_window:
                groups[-1].append(event)
                continue
        groups.append([event])
        head_action = action
        head_date = event["date"]

    combined: list[dict] = []
    for group in groups:
        total_qty = sum(e["quantity"] for e in group)
        # ``quantity`` is always positive here (the sheet ingestion
        # rejects zero / negative rows), so the divide is safe.
        weighted_price = sum(e["quantity"] * e["price"] for e in group) / total_qty
        # BUY bursts inherit their effective category from the FIRST
        # event (did this burst open the position?); SELL bursts from
        # the LAST one (did this burst close the position?).
        if group[0]["category"] in _BUY_CATEGORIES:
            category = group[0]["category"]
        else:
            category = group[-1]["category"]
        # Magnitude of the position change expressed as a percentage
        # of the pre-burst holding -- e.g. holding 1,000 shares and
        # buying another 1,000 reads as "+100%"; holding 1,000 and
        # selling 500 reads as "50%". Only meaningful for INCREASE /
        # DECREASE rows: OPEN has no prior position to compare to
        # (division by zero) and CLOSE always zeros the holding out,
        # so the badge text "Divested" already conveys the magnitude.
        # The denominator is the FIRST event's pre-trade quantity --
        # i.e. the holding right before the burst started -- so the
        # ratio reads as "what fraction did this whole burst add to /
        # remove from what we held going in?". Numerator is the sum
        # of raw trade quantities in the burst. We accept a small
        # inaccuracy when a stock-split lands mid-burst (the
        # split-adjusted denominator is the right share frame for
        # the first event but later events live in a post-split
        # frame); splits inside a 90-day window are vanishingly rare
        # on the portfolios this page targets.
        pre_quantity = group[0].get("pre_quantity", 0)
        delta_pct: float | None = None
        if category in ("INCREASE", "DECREASE") and pre_quantity > 0:
            delta_pct = total_qty / pre_quantity * 100
        combined.append(
            {
                "start_date": group[0]["date"],
                "end_date": group[-1]["date"],
                "price": weighted_price,
                "category": category,
                "delta_pct": delta_pct,
            }
        )
    return combined
