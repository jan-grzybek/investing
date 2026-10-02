"""``Trade`` records and the aggregation that powers the "Activity"
section: one entry per holding per rolling quarter, stating the net
change to the position.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .errors import InvariantError
from .types import EquityTransaction

# Public surface of this module. The leading-underscore display tables
# (``_BUY_CATEGORIES`` / ``_TRADE_ACTION_DISPLAY`` / ``_TRADE_DETAIL_LABELS``)
# are imported by ``investing.webpage.trades_view``, and the first of
# them by ``investing.holdings``; ``__all__`` is the canonical opt-in
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
                # ``total_quantity`` is a sum of strictly-positive share
                # counts (``sheets._parse_equity_row`` rejects zero /
                # negative quantities), so it is always > 0 here.
                price=total_value / total_quantity,
                action=action,
            )
        )

    return sorted(trades, key=lambda t: (t.date, t.action))


# ---------------------------------------------------------------------------
# Activity aggregation
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
# Every fill of a holding within a rolling 90-day window is folded
# into one reported entry stating the *net* effect on the position,
# at a volume-weighted average per-share price -- the granularity that
# matters to the reader is "did the position open / grow / shrink /
# close around this time?", not every individual fill. 90 days
# approximates a fiscal quarter, which is the natural cadence for a
# long-term-investor portfolio: a stake accumulated through three or
# four tranches over a quarter reads as a single deliberate action,
# not four separate trades.
#
# Direction does not end an entry. It used to: a sale always started a
# new one, so trimming a quarter of a position and buying slightly
# more back the next day was published as "Decreased by 25%" followed
# by "Increased by 35%", when what happened to the position was +1%.
# Each figure was true and the pair was misleading -- and the page's
# own description of the section ("fills within a rolling quarter are
# combined") had been promising the net all along.

TRADE_WINDOW_DAYS = 90


# Reading these as a buy-vs-sell action partitions the four categories
# along the one axis the aggregation needs: which fills added to the
# position and which took from it. That gives each quantity its sign
# in the net, and decides which fills an entry's price is averaged
# over.
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


def _vwap(events: list[dict]) -> float:
    """Volume-weighted average per-share price of ``events``.

    ``quantity`` is always positive here -- ``sheets._parse_equity_row``
    rejects zero / negative quantities at ingestion -- so the divide is
    safe for any non-empty list, and no caller passes an empty one.
    """
    total_quantity = sum(e["quantity"] for e in events)
    return sum(e["quantity"] * e["price"] for e in events) / total_quantity


def _combine_trade_events(
    events: list[dict],
    *,
    window_days: int = TRADE_WINDOW_DAYS,
) -> list[dict]:
    """Fold a ticker's raw trade events into one entry per window.

    Walks ``events`` chronologically and joins each event to the
    running group iff the span between the group's first event and the
    new event is at most ``window_days`` -- whichever way the fill
    went -- and no stock split separates the two. Anchoring on the
    FIRST event (rather than the most recent)
    caps each entry at ~one fiscal quarter -- the user-facing meaning
    of "rolling quarter" here is "a contiguous run of fills whose
    first-to-last span fits inside a 90-day window", not a sliding
    window that can keep extending indefinitely as long as consecutive
    trades stay close.

    A split ends the group because it changes the unit. Everything
    below adds quantities and averages per-share prices, and neither
    means anything across a split: 400 old shares sold and 500 new
    ones bought either side of a 2:1 is a position that *shrank*,
    though the raw numbers net to +100. ``Holding`` tags each event
    with the ``share_frame`` it was recorded in; events without the
    tag (hand-built in tests) all count as one frame.

    Each group is then reduced to what it did to the position:

    * nothing held, then something   -- ``OPEN``;
    * something held, then nothing   -- ``CLOSE``;
    * opened *and* closed in between -- an ``OPEN`` and a ``CLOSE``;
    * ends larger than it started    -- ``INCREASE``;
    * ends smaller than it started   -- ``DECREASE``;
    * ends where it started          -- no entry.

    The two boundaries are read off the events themselves: a group
    started from nothing iff its first event is an ``OPEN``, and ended
    at nothing iff its last is a ``CLOSE``. ``Holding`` assigns those
    categories in split-adjusted units, which makes them a sounder
    witness than re-deriving the position here. Between the
    boundaries the direction is the sign of the net quantity. A
    position that passes through zero mid-window (sold out and bought
    back) is deliberately not special -- only where it started and
    ended counts, the same call ``Holding`` makes when it reports a
    round trip that quick as uninterrupted ownership.

    The opened-and-closed pair is the one exception to "net". It nets
    to nothing, but it is a whole position with a realised result in
    the closed-positions table, so both legs are reported, each over
    its own fills.

    Each combined record carries:

    * ``start_date`` / ``end_date`` -- first and last event in the
      window (in the leg, for the opened-and-closed pair);
    * ``price``     -- volume-weighted average of the fills in the
      entry's own direction: buys for ``OPEN`` / ``INCREASE``, sells
      for ``CLOSE`` / ``DECREASE``. A net purchase is priced at what
      the purchases cost. Averaging in the sales it was netted against
      would publish a figure nobody paid or received;
    * ``category``  -- as listed above;
    * ``delta_pct`` -- the net change as a percentage of the holding
      right before the window's first event, on ``INCREASE`` /
      ``DECREASE`` only.
    """
    if not events:
        return []
    events = sorted(events, key=lambda e: e["date"])
    groups: list[list[dict]] = []
    head_date = events[0]["date"]
    head_frame = events[0].get("share_frame", 0)
    for event in events:
        frame = event.get("share_frame", 0)
        if groups and frame == head_frame and (event["date"] - head_date).days <= window_days:
            groups[-1].append(event)
            continue
        groups.append([event])
        head_date = event["date"]
        head_frame = frame

    combined: list[dict] = []
    for group in groups:
        buys = [e for e in group if e["category"] in _BUY_CATEGORIES]
        sells = [e for e in group if e["category"] not in _BUY_CATEGORIES]
        opened = group[0]["category"] == "OPEN"
        closed = group[-1]["category"] == "CLOSE"
        if opened and closed:
            # A whole position inside one window. Each leg spans its
            # own fills rather than the window, so a position bought
            # in week one and sold in week ten does not read as two
            # ten-week-long actions.
            for category, legs in (("OPEN", buys), ("CLOSE", sells)):
                combined.append(
                    {
                        "start_date": legs[0]["date"],
                        "end_date": legs[-1]["date"],
                        "price": _vwap(legs),
                        "category": category,
                        "delta_pct": None,
                    }
                )
            continue
        net = sum(e["quantity"] for e in buys) - sum(e["quantity"] for e in sells)
        if opened:
            category, legs = "OPEN", buys
        elif closed:
            category, legs = "CLOSE", sells
        elif net > 0:
            category, legs = "INCREASE", buys
        elif net < 0:
            category, legs = "DECREASE", sells
        else:
            # Whatever was sold was bought back (or the reverse) before
            # the window closed. The position is where it started, and
            # an entry would have nothing to say about it.
            continue
        # Magnitude of the position change expressed as a percentage
        # of the holding going into the window -- e.g. holding 1,000
        # shares and buying another 1,000 reads as "+100%"; holding
        # 1,000, selling 250 and buying 260 back reads as "+1%". Only
        # meaningful for INCREASE / DECREASE rows: OPEN has no prior
        # position to compare to (division by zero) and CLOSE always
        # zeros the holding out, so the badge text "Divested" already
        # conveys the magnitude. The denominator is the FIRST event's
        # pre-trade quantity -- i.e. the holding right before the
        # window started -- so the ratio reads as "what fraction did
        # this whole run add to / remove from what we held going in?".
        # Numerator is the net of the raw trade quantities, which is
        # sound because a group never spans a split: the denominator
        # and every quantity in the numerator are in one share frame.
        pre_quantity = group[0].get("pre_quantity", 0)
        delta_pct: float | None = None
        if category in ("INCREASE", "DECREASE") and pre_quantity > 0:
            delta_pct = abs(net) / pre_quantity * 100
        combined.append(
            {
                "start_date": group[0]["date"],
                "end_date": group[-1]["date"],
                "price": _vwap(legs),
                "category": category,
                "delta_pct": delta_pct,
            }
        )
    return combined
