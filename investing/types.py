"""TypedDict shapes for the dictionaries threaded through the pipeline.

The data pipeline ``pull_data -> get_holdings -> calc_twr ->
get_benchmarks -> generate_webpage`` is held together by plain
dictionaries that evolved organically. ``mypy`` previously could not
tell whether a typo (``holding["tsr"]`` vs ``holding["tsr%"]``) was a
bug until the renderer crashed.

The TypedDicts below codify the shape at each pipe boundary so a
lookup against the wrong key flags up in the lint pass rather than
at render time. The dicts are still passed around as plain ``dict``
-- TypedDict is structural, not runtime-enforced, so no existing call
site needs to change to start benefiting.

Several keys use ``%`` as a suffix to mark that the value is already
denormalised to percentage points (``42.0`` for "42%"). That isn't a
valid Python identifier, so the alternative-syntax form
(``TypedDict("Name", {...})``) is used wherever such a key appears.
"""
from __future__ import annotations

from datetime import datetime
from typing import TypedDict

# ---------------------------------------------------------------------------
# Sheet ingestion
# ---------------------------------------------------------------------------


class EquityTransaction(TypedDict):
    """One BUY/SELL row out of the ``Equities`` worksheet.

    Dates come through verbatim as ``DD-MM-YYYY`` strings -- parsing
    happens in :func:`investing.trades.combine_and_sort` so the
    original cell value can surface in error messages on parse
    failures.
    """

    date: str
    ticker: str
    quantity: int
    price_per_share: float
    action: str  # "BUY" | "SELL"


class Valuation(TypedDict):
    """One row out of the ``Return`` worksheet.

    ``value`` is the portfolio value on ``date`` and ``flow`` is the
    net deposit / withdrawal that happened on that date. Both feed
    the time-weighted return computation in :func:`calc_twr`.
    """

    date: datetime
    value: float
    flow: float


class CashBalance(TypedDict):
    """One row out of the ``Cash & Cash Equivalents`` worksheet."""

    currency_code: str
    amount: float


# ---------------------------------------------------------------------------
# Holdings / per-ticker summaries
# ---------------------------------------------------------------------------


class HoldingPeriod(TypedDict):
    """A continuous span of ownership for a single ticker.

    ``end`` is ``None`` while the position is still open (the rendered
    page shows "Present" in that case)."""

    start: datetime
    end: datetime | None


# Alternative-syntax TypedDict because the ``%`` suffix on the
# percentage keys isn't a valid Python identifier. ``total=False``
# marks every field as optional so a partial dict (e.g. one freshly
# returned by ``Holding.summary`` before ``summarize`` enriches it)
# still satisfies the contract.
HoldingSummary = TypedDict(
    "HoldingSummary",
    {
        "ticker": str,
        "name": str,
        "tsr%": float,
        "cagr%": float,
        "is_current": bool,
        "current_weight%": float | None,
        "current_value_usd": float,
        "periods": list[HoldingPeriod],
        "latest_buy": datetime,
        "latest_sell": datetime | None,
    },
    total=False,
)


# ---------------------------------------------------------------------------
# Trade events
# ---------------------------------------------------------------------------


class TradeEvent(TypedDict, total=False):
    """One row in the rendered "Trades" table.

    Produced by :func:`investing.trades._combine_trade_events` after
    bursts of small same-action trades are folded together.
    ``category`` is one of ``OPEN`` / ``INCREASE`` / ``DECREASE`` /
    ``CLOSE``; ``ticker`` / ``name`` / ``currency`` are filled in by
    :meth:`Holding.trade_events`, not by the combiner itself.
    """

    start_date: datetime
    end_date: datetime
    price: float
    category: str
    delta_pct: float | None
    ticker: str
    name: str
    currency: str


# ---------------------------------------------------------------------------
# Portfolio rollups
# ---------------------------------------------------------------------------


HoldingsRollup = TypedDict(
    "HoldingsRollup",
    {
        "current": list[HoldingSummary],
        "historical": list[HoldingSummary],
        "trades": list[TradeEvent],
        "allocation%": dict[str, float] | None,
        "top_10": dict[str, float] | None,
    },
    total=False,
)


TotalReturn = TypedDict(
    "TotalReturn",
    {
        "start_date": datetime,
        "history": list[tuple[datetime, float]],
        "twr%": float,
        "cagr%": float,
    },
    total=False,
)


BenchmarkSummary = TypedDict(
    "BenchmarkSummary",
    {
        "ticker": str,
        "name": str,
        "tsr%": float,
        "cagr%": float,
        "periods": list[HoldingPeriod],
        "history": list[tuple[datetime, float]],
        "is_current": bool,
        "current_weight%": float | None,
        "current_value_usd": float,
        "latest_buy": datetime,
        "latest_sell": datetime | None,
    },
    total=False,
)
