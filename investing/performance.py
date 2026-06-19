"""Portfolio-wide rollups: TWR, allocations, top-10
weights, benchmark fetch.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from .clock import NowFn
from .errors import InvariantError
from .formatting import _fmt_pct
from .fx import FxRate, _fx_or_default
from .holdings import DAYS_YEAR, Holding
from .log import logger
from .market_data_store import MarketDataStore
from .trades import ACTIONS, combine_and_sort
from .types import (
    BenchmarkSummary,
    CashBalance,
    EquityTransaction,
    HoldingsRollup,
    HoldingSummary,
    TotalReturn,
    TradeEvent,
    Valuation,
    YearlyReturn,
)

# Public surface of this module. ``_BENCHMARK_DISPLAY_NAMES`` is the
# only leading-underscore name advertised here -- the renderer
# (``investing.webpage._page`` / ``return_chart``) consumes it
# directly. Listing it in ``__all__`` is the canonical opt-in that
# tells CodeQL's ``py/unused-global-variable`` query the binding is a
# cross-module export rather than a module-local helper its
# leading underscore would otherwise imply.
__all__ = [
    "BENCHMARKS",
    "_BENCHMARK_DISPLAY_NAMES",
    "Benchmark",
    "BenchmarkConfig",
    "PortfolioRollup",
    "apply_rollup",
    "calc_twr",
    "calc_yearly_returns",
    "compute_rollup",
    "get_benchmarks",
    "get_holdings",
]


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for a single reference index rendered alongside the JG portfolio.

    Bundles the upstream ticker with the human-facing display name so
    adding a new benchmark is a single ``BenchmarkConfig(...)`` append
    rather than a coordinated edit across parallel structures.
    """

    ticker: str
    display_name: str


# Module-level registry of reference indices the page renders next to
# the JG portfolio curve. A future second-benchmark experiment is just
# an append away; the renderer reads display names through the
# ``_BENCHMARK_DISPLAY_NAMES`` mapping below so existing call sites
# continue to work.
BENCHMARKS: tuple[BenchmarkConfig, ...] = (
    BenchmarkConfig(ticker="VUAA.L", display_name="S&P 500"),
)


def _display_name_map() -> dict[str, str]:
    """Build the ``EXCH:SYMBOL`` -> display-name mapping the renderer reads.

    Yahoo normalises ``VUAA.L`` to ``LSE:VUAA.L`` at fetch time, so the
    map key carries the exchange prefix to match what
    ``Holding.summary()`` emits.
    """
    return {
        f"LSE:{cfg.ticker}" if "." in cfg.ticker else cfg.ticker: cfg.display_name
        for cfg in BENCHMARKS
    }


# Backwards-compatible alias for the renderer; preserved so existing
# ``from .performance import _BENCHMARK_DISPLAY_NAMES`` imports continue
# to work while callers migrate to ``BENCHMARKS`` / ``BenchmarkConfig``.
# Listed in ``__all__`` further down so CodeQL's
# ``py/unused-global-variable`` query recognises this leading-underscore
# binding as an intentional cross-module export.
_BENCHMARK_DISPLAY_NAMES = _display_name_map()


# ---------------------------------------------------------------------------
# Holdings -> summaries
# ---------------------------------------------------------------------------


def get_holdings(
    transactions: list[EquityTransaction],
    *,
    fixed_income: list[EquityTransaction] | None = None,
    fx: FxRate | None = None,
    now: NowFn | None = None,
    store: MarketDataStore | None = None,
) -> HoldingsRollup:
    """Roll up transactions into per-ticker Holding summaries.

    ``fx`` is forwarded to every ``Holding`` so production can share a
    single ``ExchangeRate`` cache across the whole portfolio rather
    than re-fetching the same currency per ticker. ``now`` is the
    wall-clock plug used by :meth:`Holding.summary` to anchor any
    open-ended period; ``None`` keeps the legacy
    ``datetime.today`` behaviour.

    ``fixed_income`` is the second transaction list -- rows out of
    the upstream "Fixed Income" worksheet. It shares the
    :class:`EquityTransaction` shape (the source schema is identical)
    but the resulting :class:`Holding` objects are tagged with
    ``asset_class="fixed_income"`` so the renderer can bucket their
    summaries into the dedicated fixed-income sub-sections. The list
    defaults to empty for backwards compatibility with callers that
    only carry equities.
    """
    fx = _fx_or_default(fx)
    fixed_income = list(fixed_income or [])
    # Each ticker is parsed into trades inside its own asset-class
    # bucket. The ticker -> asset_class map captures the binding so
    # the per-ticker constructor below can pick the right tag and
    # the renderer can downstream split the resulting summaries into
    # the matching Current / Historical sections.
    asset_class_by_ticker: dict[str, str] = {}
    for txn in transactions:
        asset_class_by_ticker.setdefault(txn["ticker"], "equity")
    for txn in fixed_income:
        asset_class_by_ticker.setdefault(txn["ticker"], "fixed_income")

    trades = combine_and_sort(transactions + fixed_income)

    # ``Holding.__init__`` is dominated by sequential yfinance round
    # trips (``get_info``, ``splits``, ``get_dividends`` -- each a
    # separate HTTPS request behind the cached ``Ticker.actions``
    # frame). Walking trades serially would construct each Holding
    # one at a time and pay that latency N times in a row; a small
    # thread pool collapses the wall-clock cost to roughly
    # ``ceil(N / pool_size) * per-ticker latency`` while leaving
    # the per-Trade application loop strictly serial (positions /
    # periods / inflows are stateful and rely on chronological
    # processing).
    unique_tickers: list[str] = []
    seen: set[str] = set()
    for trade in trades:
        if trade.ticker not in seen:
            seen.add(trade.ticker)
            unique_tickers.append(trade.ticker)

    holdings: dict[str, Holding] = {}
    if unique_tickers:
        # Cap the pool at a modest size: yfinance is rate-limited
        # and a wider fan-out trades latency for HTTP 429 retries.
        # Eight workers is a comfortable middle ground for the
        # 10-30 ticker portfolios this page targets.
        max_workers = min(8, len(unique_tickers))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            constructed = pool.map(
                lambda t: (
                    t,
                    Holding(
                        t,
                        fx=fx,
                        now=now,
                        asset_class=asset_class_by_ticker.get(t, "equity"),
                        store=store,
                    ),
                ),
                unique_tickers,
            )
            for ticker, holding in constructed:
                holdings[ticker] = holding

    for trade in trades:
        if trade.action not in ACTIONS:
            raise InvariantError(
                f"trade action {trade.action!r} is not one of {ACTIONS}",
            )
        if trade.action == "BUY":
            holdings[trade.ticker].buy(trade)
        else:
            holdings[trade.ticker].sell(trade)

    current_equities: list[HoldingSummary] = []
    historical_equities: list[HoldingSummary] = []
    current_fi: list[HoldingSummary] = []
    historical_fi: list[HoldingSummary] = []
    trade_events: list[TradeEvent] = []
    for holding in holdings.values():
        summary = holding.summary()
        is_fi = summary.get("asset_class") == "fixed_income"
        if summary["is_current"]:
            (current_fi if is_fi else current_equities).append(summary)
        else:
            (historical_fi if is_fi else historical_equities).append(summary)
        # Per-ticker bursts are grouped within each ``Holding`` and
        # then merged into one global, newest-first list so the page
        # reads like a chronological activity log across the whole
        # portfolio. Equity and fixed-income trades intermix in this
        # single section by design -- the user-facing log reads as a
        # chronological activity feed, not as an asset-class report.
        trade_events.extend(holding.trade_events())

    # ``latest_sell`` is typed ``datetime | None`` on the TypedDict
    # because OPEN positions still have ``None`` -- but the
    # ``historical`` bucket here was filtered on ``is_current=False``
    # which only happens after a closing SELL, so every entry has a
    # concrete date. The ``or _MIN_SORT_DATE`` fallback satisfies
    # ``sorted`` typing without altering the runtime behaviour.
    def _sort_current(items: list[HoldingSummary]) -> list[HoldingSummary]:
        return sorted(items, key=lambda item: item["latest_buy"], reverse=True)

    def _sort_historical(items: list[HoldingSummary]) -> list[HoldingSummary]:
        return sorted(
            items,
            key=lambda item: item["latest_sell"] or datetime.min,
            reverse=True,
        )

    rollup: HoldingsRollup = {
        "current": _sort_current(current_equities),
        "historical": _sort_historical(historical_equities),
        "current_fixed_income": _sort_current(current_fi),
        "historical_fixed_income": _sort_historical(historical_fi),
        # Sort by the burst's most recent event (so a multi-day burst
        # ranks by when it finished). Ties are broken by start date,
        # which only matters on synthetic / same-day-only data sets.
        "trades": sorted(
            trade_events,
            key=lambda e: (e["end_date"], e["start_date"]),
            reverse=True,
        ),
    }
    return rollup


# ---------------------------------------------------------------------------
# Portfolio-wide return / allocation
# ---------------------------------------------------------------------------


def calc_twr(
    valuations: list[Valuation],
    current_value: float,
    *,
    now: NowFn | None = None,
) -> TotalReturn:
    """Bootstrap the portfolio's time-weighted return curve.

    ``now`` is the wall-clock plug; ``None`` falls through to
    ``datetime.today`` so the legacy ``freeze_today`` fixture (which
    monkeypatches this module's bound ``datetime`` symbol) keeps
    working. New callers can pass an explicit closure to inject a
    fixed timestamp without the cross-module patch.
    """
    _now: NowFn = now if now is not None else datetime.today
    if not valuations:
        return {"start_date": _now(), "history": [], "twr%": 0.0, "cagr%": 0.0}
    valuations = sorted(valuations, key=lambda item: item["date"])
    start_date = valuations[0]["date"]
    history: list[tuple[datetime, float]] = []
    start_value = valuations[0]["value"] + valuations[0]["flow"]
    twr = 1.0
    history.append((valuations[0]["date"], twr))
    for valuation in valuations[1:]:
        twr *= valuation["value"] / start_value
        start_value = valuation["value"] + valuation["flow"]
        history.append((valuation["date"], twr))
    today = _now()
    if today.date() > valuations[-1]["date"].date():
        twr *= current_value / start_value
        history.append((today, twr))
    cagr = twr ** (DAYS_YEAR / max((today - start_date).days, 1)) - 1.0
    twr -= 1.0
    # Store unrounded percentages; consumers (capsule delta vs
    # benchmark, chart pp-delta overlay, OG-image headline) require
    # the full precision so subtraction doesn't compound the 0.05 pp
    # error of single-decimal rounding. Display sites round at
    # format time with ``:.1f``.
    twr_pct = twr * 100
    cagr_pct = cagr * 100
    logger.info(
        "JG - Jan Grzybek - TWR: %s%% - CAGR: %s%%",
        _fmt_pct(twr_pct),
        _fmt_pct(cagr_pct),
    )
    return {
        "start_date": start_date,
        "history": history,
        "twr%": twr_pct,
        "cagr%": cagr_pct,
    }


@dataclass(frozen=True)
class PortfolioRollup:
    """Allocation / top-10 / per-holding weights derived from a holdings dict.

    Computed in a pure function (:func:`compute_rollup`) and applied to
    the carrier dict in an explicit step (:func:`apply_rollup`) so the
    pipeline orchestrator in :mod:`investing.cli` reads as "compute,
    then apply" rather than burying side effects inside a helper that
    also returns a float.
    """

    total_value_usd: float
    allocation_pct: dict[str, float] | None
    top_10: dict[str, float] | None
    weights_by_ticker: dict[str, float]


def compute_rollup(
    holdings: HoldingsRollup,
    cash: list[CashBalance],
    *,
    fx: FxRate | None = None,
) -> PortfolioRollup:
    """Compute allocation percentages, per-holding weights and top-10 buckets.

    Returns a :class:`PortfolioRollup` snapshot; the input ``holdings``
    dict is read but not mutated, so callers can decide whether to
    apply the rollup back onto the dict (the legacy carrier shape via
    :func:`apply_rollup`) or thread the snapshot through explicitly.

    Accepts an explicit ``fx`` rather than reaching for a module
    global so callers can plug in a stub for tests and a single
    shared :class:`investing.fx.ExchangeRate` for production.

    Asset-class allocation: equities and fixed income each get their
    own row in ``allocation%``. The cash row trails so the natural
    reading order ("riskiest first, cash last") matches the
    rendered Asset Allocation chart.
    """
    fx = _fx_or_default(fx)
    total_equity_value_usd = 0.0
    total_fixed_income_value_usd = 0.0
    total_cash_value_usd = 0.0

    # Single guard helper that rejects zero/negative USD valuations
    # for any current holding regardless of asset class -- both
    # equities and fixed income sit on the same upstream price feed
    # (yfinance) and a degenerate value on either side is the same
    # data fault.
    def _check_value(holding: HoldingSummary) -> None:
        if holding["current_value_usd"] <= 0.0:
            raise InvariantError(
                f"current holding {holding['ticker']!r} has non-positive "
                f"USD valuation: {holding['current_value_usd']!r}",
            )

    for holding in holdings["current"]:
        _check_value(holding)
        total_equity_value_usd += holding["current_value_usd"]
    for holding in holdings.get("current_fixed_income", []) or []:
        _check_value(holding)
        total_fixed_income_value_usd += holding["current_value_usd"]
    for currency in cash:
        total_cash_value_usd += currency["amount"] * fx(currency["currency_code"])

    total_value_usd = total_equity_value_usd + total_fixed_income_value_usd + total_cash_value_usd

    allocation_pct: dict[str, float] | None
    if total_value_usd > 0.0:
        # Unrounded percentages: ``current_weight%`` is summed when
        # building the "Other equities" bucket, and ``allocation%``
        # entries are read directly elsewhere. Rounding here would
        # leak into those derived numbers; we round only at display
        # time (``_render_bars`` formats with ``:.1f`` / ``:.2f``).
        # Reading order is Equities -> Fixed Income -> Cash so the
        # rendered chart leads with the riskiest bucket and ends on
        # the cash row.
        allocation_pct = {
            "Equities": 100 * total_equity_value_usd / total_value_usd,
        }
        # Fixed Income only appears in the rollup when the portfolio
        # actually carries a fixed-income sleeve. Including a 0%
        # row for portfolios that don't would surface a confusing
        # empty bar chart entry; the equity / cash rows have always
        # been emitted unconditionally so that contract is
        # preserved for them.
        if total_fixed_income_value_usd > 0.0:
            allocation_pct["Fixed Income"] = 100 * total_fixed_income_value_usd / total_value_usd
        allocation_pct["Cash & Cash Equivalents"] = 100 * total_cash_value_usd / total_value_usd
        logger.info(
            "Equity allocation: %s%%",
            _fmt_pct(allocation_pct["Equities"]),
        )
        if "Fixed Income" in allocation_pct:
            logger.info(
                "Fixed Income allocation: %s%%",
                _fmt_pct(allocation_pct["Fixed Income"]),
            )
        logger.info(
            "Cash allocation: %s%%",
            _fmt_pct(allocation_pct["Cash & Cash Equivalents"]),
        )
    else:
        allocation_pct = None

    weights: dict[str, float] = {}
    # Equities first -- they're the only buckets that feed the
    # top-10 weights below (the bar chart / OG image strip is
    # equity-only by design). Fixed-income holdings still get a
    # ``current_weight%`` so their capsules can show the same
    # Weight stat as equity capsules; the dict is just not consumed
    # by the top-10 chart.
    for holding in holdings["current"]:
        # ``total_value_usd`` is guaranteed positive here: every
        # ``current`` holding has been validated as strictly positive
        # USD above, so iterating ``current`` implies a positive
        # denominator. The empty-portfolio branch above already
        # short-circuited when there was nothing to weight.
        weight = 100 * holding["current_value_usd"] / total_value_usd
        weights[holding["ticker"]] = weight
        logger.info(
            "%s - %s - Weight: %s%% - Return: %s%% - IRR: %s%%",
            holding["ticker"],
            holding["name"],
            _fmt_pct(weight),
            _fmt_pct(holding["tsr%"]),
            _fmt_pct(holding["cagr%"]),
        )
    fi_weights: dict[str, float] = {}
    for holding in holdings.get("current_fixed_income", []) or []:
        weight = 100 * holding["current_value_usd"] / total_value_usd
        fi_weights[holding["ticker"]] = weight
        logger.info(
            "%s - %s - Weight: %s%% - Return: %s%% - IRR: %s%% (fixed income)",
            holding["ticker"],
            holding["name"],
            _fmt_pct(weight),
            _fmt_pct(holding["tsr%"]),
            _fmt_pct(holding["cagr%"]),
        )

    top_10: dict[str, float] | None = None
    if weights:
        ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) > 11:
            top_10 = dict(
                ranked[:10] + [("Other equities", sum(w for _, w in ranked[10:]))],
            )
        else:
            top_10 = dict(ranked)

    # Combined map fed to ``apply_rollup`` so each per-class capsule
    # picks up its own weight regardless of bucket. The dicts are
    # disjoint (equities vs fixed-income tickers can't collide --
    # the same ticker can't be in both worksheets).
    weights_combined = {**weights, **fi_weights}

    return PortfolioRollup(
        total_value_usd=total_value_usd,
        allocation_pct=allocation_pct,
        top_10=top_10,
        weights_by_ticker=weights_combined,
    )


def apply_rollup(holdings: HoldingsRollup, rollup: PortfolioRollup) -> None:
    """Write a :class:`PortfolioRollup` onto the carrier holdings dict.

    The renderer reads ``allocation%`` / ``top_10`` off the holdings
    dict and ``current_weight%`` off each current-holdings entry, so
    the orchestrator applies the rollup to that shape before handing
    the dict to :func:`generate_webpage`. Keeping the mutation in one
    visible call (rather than buried inside :func:`compute_rollup`)
    makes it obvious where and when the carrier dict changes.
    """
    holdings["allocation%"] = rollup.allocation_pct
    holdings["top_10"] = rollup.top_10
    for holding in holdings["current"]:
        holding["current_weight%"] = rollup.weights_by_ticker.get(
            holding["ticker"],
        )
    # Fixed-income capsules render the same Weight stat as equities
    # do; the weight is computed against the same total portfolio
    # USD denominator so the percentages across both buckets read
    # apples-to-apples ("this position is X% of the whole
    # portfolio").
    for holding in holdings.get("current_fixed_income", []) or []:
        holding["current_weight%"] = rollup.weights_by_ticker.get(
            holding["ticker"],
        )


class Benchmark:
    """A reference index resampled onto the portfolio's TWR timeline.

    Wraps the awkward Yahoo Finance bootstrap that ``get_benchmarks``
    used to inline. Both the live-tape ``regularMarketPrice`` and
    the historical ``Adj Close`` series live on this class behind
    well-named methods so a future change (different data source,
    multiple benchmarks, currency-converted prices, ...) has a
    single edit surface and ``Holding`` keeps its private Yahoo
    Ticker handle to itself.

    The history fetch returns a single DataFrame whose ``Adj Close``
    column powers both the chart's cumulative-return curve and the
    start-day basis price :meth:`summary` divides
    ``regularMarketPrice`` by to produce the buy-and-hold TSR.
    """

    def __init__(
        self,
        ticker: str,
        start_date: datetime,
        fx: FxRate | None = None,
        now: NowFn | None = None,
        store: MarketDataStore | None = None,
    ):
        self._ticker_symbol = ticker
        self._start_date = start_date
        self._start_date_str = start_date.strftime("%Y-%m-%d")
        self._fx = _fx_or_default(fx)
        # Stashed so :meth:`summary` can compute the period length
        # for CAGR without reaching into ``_holding`` (which has its
        # own clock plug for the same purpose).
        self._now: NowFn = now if now is not None else datetime.today
        self._holding = Holding(ticker, fx=self._fx, now=now, store=store)
        # Single ``auto_adjust=False`` fetch carries the dividend /
        # split adjusted ``Adj Close`` column we use as both the
        # start-day basis (TSR denominator) and the cumulative-return
        # curve. Pre-converting to a numpy array here means
        # ``cumulative_return_series`` never has to pay the per-row
        # pandas indexing tax the legacy implementation did.
        history = self._holding.fetch_market_history(
            start=self._start_date_str,
        )
        self._history = history
        adj_closes = history["Adj Close"].to_numpy(dtype=float)
        # Forward-fill any NaN runs so a missing day inherits the
        # last known close. ``bfill`` after handles a leading NaN
        # (vanishingly rare on a real Yahoo response, but the chart
        # renderer downstream calls ``np.log`` on the result and
        # would propagate any NaN into an invalid polyline). The
        # historical implementation walked the series row-by-row and
        # had a subtle bug at index 0 where ``iloc[idx - 1]`` wrapped
        # to the **last** row instead of failing, so a NaN on the
        # first day would have been silently filled with the most
        # recent close from years later -- the vectorised path here
        # closes that hole as a side-effect.
        self._adj_closes = _ffill(adj_closes)
        # Yahoo returns a ``DatetimeIndex``; ``datetime64[D]``
        # collapses each timestamp to its date so the per-ref-date
        # ``np.searchsorted`` lookup compares apples to apples.
        #
        # The index is tz-aware for exchange-listed tickers
        # (Europe/London for LSE, US/Eastern for NYSE/Nasdaq, ...).
        # A naive ``.to_numpy()`` would convert each timestamp to
        # UTC before stripping the tz, which during DST shifts
        # every local-trading-day timestamp back one calendar day
        # (e.g. an LSE bar at ``2026-03-31 00:00:00+01:00`` becomes
        # UTC ``2026-03-30 23:00:00`` -> date ``2026-03-30``).
        # The ref dates ``cumulative_return_series`` searches with
        # are tz-naive ``datetime`` objects parsed from the
        # spreadsheet, so a UTC-shifted ``_dates`` array would map
        # every BST ref date to the *next* trading day's adj close
        # and silently inflate the chart's curve by one session.
        # ``tz_localize(None)`` drops the tz without converting,
        # preserving the exchange-local calendar date the trading
        # bar actually represents. Guarded so already-naive indices
        # (the test fixtures synthesise these directly) pass through.
        idx = history.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        self._dates = idx.to_numpy().astype("datetime64[D]")
        # Stashed at construction so :meth:`cumulative_return_series`
        # can pin the chart's right-edge sample to the same number
        # :meth:`summary` uses to compute the buy-and-hold TSR --
        # otherwise the chart endpoint clips to the latest adj-close
        # already in the ``history()`` response and disagrees with
        # the capsule by intraday / overnight movement against the
        # live tape.
        self._current_market_price = self._holding.current_market_price

    @property
    def start_basis_price(self) -> float:
        """Split-adjusted close on the first trading day at / after start_date.

        The chart's cumulative-return resampler normalises every
        sample against this same starting basis
        (``Adj Close[t] / Adj Close[0]``), and :meth:`summary`
        divides today's ``regularMarketPrice`` by it to produce the
        buy-and-hold TSR -- so the two computations share a single
        denominator and the chart's right edge equals
        ``1 + tsr%/100`` by construction.

        Yahoo's ``Adj Close`` back-adjusts historical closes for any
        splits (and, for distributing tickers, dividends) that
        happen after the sample date. For an accumulating ETF with
        no distributions and no splits in the period (e.g.
        ``VUAA.L``) the adjusted close equals the raw close on that
        day, so the basis reduces to ``Close[start_day]``.
        """
        return float(self._adj_closes[0])

    def cumulative_return_series(self, reference_history):
        """Resample the benchmark's adjusted-close series onto the
        portfolio's TWR timeline.

        For each reference timestamp ``t`` (other than the first,
        which is pinned at 1.0 by convention), ``np.searchsorted``
        finds the most recent benchmark trading day with
        ``date <= t`` -- the same "use the last known close on or
        before the reference" rule the row-by-row walker
        implemented, expressed in O(log n) per query instead of
        O(n) per ref date. Yahoo trading days that fall *between*
        two reference points are correctly skipped: only the close
        active *as of* the ref date contributes.

        The previous implementation also had a wrap-around edge
        case at index 0 where ``iloc[idx - 1]`` evaluated to
        ``iloc[-1]`` (the last row in the response). The vectorised
        lookup below cannot reach for an out-of-bounds index by
        construction.
        """
        if self._adj_closes.size == 0:
            raise InvariantError(
                "benchmark history is empty -- nothing to resample",
            )
        # Cumulative-return convention: the first reference point
        # is pinned at 1.0 so the curve reads as a multiplier
        # relative to the timeline start regardless of the gap
        # between ``start_date`` and the first benchmark trading
        # day. Subsequent points are ``adj_close[t] /
        # adj_close[start]``.
        start_price = float(self._adj_closes[0])
        if start_price == 0.0:
            raise InvariantError(
                "benchmark start-day adjusted close is zero -- "
                "cannot normalise cumulative-return curve",
            )
        ref_dates = np.array(
            [d.date() for d, _ in reference_history],
            dtype="datetime64[D]",
        )
        # ``side="right"`` then ``- 1`` returns the index of the
        # last benchmark trading day with date <= ref_date.
        # ``np.clip`` floors at the first benchmark row (so a ref
        # date earlier than every benchmark day still produces a
        # finite multiplier) and ceilings at the last (so a ref
        # date past the response's tail back-fills to the most
        # recent close, mirroring the legacy "if the loop ran out
        # before covering the timeline, append the final close"
        # branch).
        idx = np.searchsorted(self._dates, ref_dates, side="right") - 1
        idx = np.clip(idx, 0, len(self._adj_closes) - 1)
        multipliers = self._adj_closes[idx] / start_price
        # Pin the right-edge sample to the same ``regularMarketPrice``
        # numerator :meth:`summary` divides ``start_basis_price`` by
        # to compute the capsule TSR. Without this override the
        # chart's last point clips to the most recent adj-close in
        # the Yahoo ``history()`` response, which disagrees with the
        # live ``regularMarketPrice`` by an intraday move (when
        # today is a trading day) or by a full session (when today
        # is past the last trading day). Only kicks in when the
        # chart's last reference date is at / past the last yahoo
        # trading day -- earlier ref dates legitimately want the
        # in-history adjusted close at that earlier date, not a
        # "today" stand-in.
        if len(multipliers) > 1 and ref_dates[-1] >= self._dates[-1] and start_price > 0.0:
            multipliers[-1] = self._current_market_price / start_price
        # Pin the first entry at 1.0 by convention (the chart's
        # normalising denominator is by definition the start basis,
        # so the curve always starts at the reference line).
        multipliers[0] = 1.0
        # Materialise as a list of (datetime, float) tuples to
        # match the historical output type the renderer consumes.
        return [
            (ref_date, float(m))
            for (ref_date, _), m in zip(reference_history, multipliers, strict=True)
        ]

    def summary(self, reference_history) -> BenchmarkSummary:
        """Produce the per-benchmark dict the renderer consumes.

        Computes a buy-and-hold TSR / CAGR from
        :attr:`start_basis_price` (Yahoo's back-adjusted close on
        the first trading day) to ``regularMarketPrice`` (the live
        tape today). The same numerator + denominator pair anchors
        the chart's resampler, so the capsule TSR and the chart's
        right edge fall out of a single arithmetic source and agree
        by construction.

        Note we deliberately do NOT route this through
        :meth:`Holding.summary`: that path is a chained sub-period
        TWR over actual trade events with explicit ex-dividend
        boundaries, none of which a benchmark "1 share" synthetic
        needs. The buy-and-hold ratio
        ``regularMarketPrice / Adj Close[start]`` collapses the same
        arithmetic into a single line and keeps the Benchmark's
        price-frame contract local and unambiguous (Yahoo's
        ``Adj Close`` is already in post-all-future-splits units, so
        a synthetic trade routed through the chain would have to
        avoid the split-rebase that genuine trades depend on).
        """
        info = self._holding.info
        currency = info["currency"]
        invested_native = self.start_basis_price
        current_native = self._holding.current_market_price
        current_value_usd = current_native * self._fx(currency)
        # Single buy-and-hold TSR: ``regularMarketPrice / Adj Close[start]
        # - 1``. Reduces to the chart's rightmost sample minus 1 by
        # construction (see :meth:`cumulative_return_series`).
        tsr = current_native / invested_native - 1.0
        length = max((self._now() - self._start_date).days, 1)
        cagr = (1.0 + tsr) ** (DAYS_YEAR / length) - 1.0
        return {
            "ticker": f"{info['exchange']}:{info['symbol']}",
            "name": info["longName"],
            # Unrounded percentages so downstream callers (delta vs
            # benchmark capsule, OG-image hero) can do further math
            # without compounding rounding error -- display sites
            # round at format time.
            "tsr%": tsr * 100.0,
            "cagr%": cagr * 100.0,
            "is_current": True,
            "current_weight%": None,
            "current_value_usd": current_value_usd,
            "periods": [{"start": self._start_date, "end": None}],
            "latest_buy": self._start_date,
            "latest_sell": None,
            "history": self.cumulative_return_series(reference_history),
        }

    def _price_on_or_before(self, day: date, *, pin_live: bool = False) -> float:
        """Adjusted close (or live price) on the last trading day at / before ``day``.

        When ``pin_live`` is true and ``day`` is at / past the last
        trading day in the Yahoo response, returns
        ``regularMarketPrice`` so calendar-year windows ending today
        agree with the headline TSR / chart endpoint.
        """
        target = np.datetime64(day)
        idx = int(np.searchsorted(self._dates, target, side="right")) - 1
        idx = int(np.clip(idx, 0, len(self._adj_closes) - 1))
        if pin_live and target >= self._dates[-1]:
            return float(self._current_market_price)
        return float(self._adj_closes[idx])

    def period_return_pct(self, anchor: date, end: date, *, pin_live_end: bool = False) -> float:
        """Buy-and-hold total return from ``anchor`` through ``end``, in percent."""
        start_price = self._price_on_or_before(anchor)
        end_price = self._price_on_or_before(end, pin_live=pin_live_end)
        if start_price == 0.0:
            raise InvariantError(
                "benchmark start price is zero -- cannot compute period return",
            )
        return (end_price / start_price - 1.0) * 100.0


def _ffill(arr: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs in a 1-D float array, then back-fill any
    leading NaN run.

    Only used by :class:`Benchmark` for its Yahoo response; lifted
    out so the cumulative-return resampler can stay focused on the
    timeline arithmetic. Returns a fresh array; the input is left
    untouched.
    """
    if arr.size == 0:
        return arr
    out = arr.copy()
    nan_mask = np.isnan(out)
    if not nan_mask.any():
        return out
    # Forward-fill: replace each NaN with the most recent non-NaN
    # value via ``np.maximum.accumulate`` over an "index of last
    # valid sample" running max.
    valid_idx = np.where(~nan_mask, np.arange(len(out)), 0)
    valid_idx = np.maximum.accumulate(valid_idx)
    out = out[valid_idx]
    # Any leading NaN run still has ``valid_idx == 0`` pointing at a
    # NaN; back-fill those with the first finite value so the
    # downstream ``np.log`` doesn't choke.
    if np.isnan(out[0]):
        finite = np.where(~np.isnan(out))[0]
        if finite.size:
            out[: finite[0]] = out[finite[0]]
    return out


def _multiplier_on_or_before(
    history: list[tuple[datetime, float]],
    day: date,
) -> float:
    """Last TWR multiplier in ``history`` with sample date on or before ``day``."""
    result = 1.0
    for sample_date, multiplier in history:
        if sample_date.date() <= day:
            result = multiplier
        else:
            break
    return result


def _period_return_from_multipliers(
    history: list[tuple[datetime, float]],
    anchor: date,
    end: date,
) -> float:
    """Sub-period TWR between ``anchor`` and ``end``, expressed in percent."""
    start_mult = _multiplier_on_or_before(history, anchor)
    end_mult = _multiplier_on_or_before(history, end)
    if start_mult == 0.0:
        return 0.0
    return (end_mult / start_mult - 1.0) * 100.0


def calc_yearly_returns(
    total_return: TotalReturn,
    *,
    benchmark: Benchmark | None = None,
    benchmark_history: list[tuple[datetime, float]] | None = None,
    now: NowFn | None = None,
) -> list[YearlyReturn]:
    """Calendar-year (and YTD) total returns for JG vs the primary benchmark.

    Each row links the TWR multiplier curve at year boundaries using
    the last known sample on or before each anchor/end date -- no
    interpolation between spreadsheet valuations. The benchmark side
    uses daily ``Adj Close`` (or the resampled ``benchmark_history``
    fallback used by preview/tests) over the same anchor/end windows.

    Rows are returned newest-first. The current calendar year is
    flagged ``is_ytd`` and runs through ``now()`` rather than 31 Dec.
    """
    _now: NowFn = now if now is not None else datetime.today
    history = total_return.get("history") or []
    if not history:
        return []

    start_date = total_return["start_date"]
    portfolio_start = start_date.date()
    today = _now().date()
    first_year = portfolio_start.year
    last_year = today.year
    rows: list[YearlyReturn] = []

    for year in range(last_year, first_year - 1, -1):
        if year == portfolio_start.year:
            anchor = portfolio_start
        else:
            anchor = date(year - 1, 12, 31)

        period_end = min(date(year, 12, 31), today)
        is_ytd = year == today.year and period_end < date(year, 12, 31)
        if anchor > period_end:
            continue

        jg_pct = _period_return_from_multipliers(history, anchor, period_end)
        bench_pct: float | None = None
        if benchmark is not None:
            bench_pct = benchmark.period_return_pct(
                anchor,
                period_end,
                pin_live_end=period_end == today,
            )
        elif benchmark_history:
            bench_pct = _period_return_from_multipliers(
                benchmark_history,
                anchor,
                period_end,
            )

        row: YearlyReturn = {
            "year": year,
            "jg%": jg_pct,
            "is_ytd": is_ytd,
        }
        if bench_pct is not None:
            row["bench%"] = bench_pct
        rows.append(row)

    return rows


def get_benchmarks(
    total_return: TotalReturn,
    *,
    fx: FxRate | None = None,
    now: NowFn | None = None,
    store: MarketDataStore | None = None,
) -> tuple[list[BenchmarkSummary], list[YearlyReturn]]:
    fx = _fx_or_default(fx)
    history = total_return.get("history") or []
    if not history:
        return [], []
    start_date = total_return["start_date"]
    benchmarks: list[BenchmarkSummary] = []
    yearly_returns: list[YearlyReturn] = []
    for index, cfg in enumerate(BENCHMARKS):
        benchmark = Benchmark(cfg.ticker, start_date, fx=fx, now=now, store=store)
        summary = benchmark.summary(history)
        benchmarks.append(summary)
        logger.info(
            "%s - %s - TSR: %s%% - CAGR: %s%%",
            summary["ticker"],
            summary["name"],
            _fmt_pct(summary["tsr%"]),
            _fmt_pct(summary["cagr%"]),
        )
        if index == 0:
            yearly_returns = calc_yearly_returns(
                total_return,
                benchmark=benchmark,
                now=now,
            )
    return benchmarks, yearly_returns
