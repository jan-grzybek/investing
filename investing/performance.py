"""Portfolio-wide rollups: TWR, allocations, top-10
weights, benchmark fetch.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .clock import NowFn
from .errors import InvariantError
from .formatting import _fmt_pct
from .fx import FxRate, _fx_or_default
from .holdings import DAYS_YEAR, Holding
from .log import logger
from .trades import ACTIONS, combine_and_sort
from .types import (  # re-exported for type-aware callers; functions below
    BenchmarkSummary,  # noqa: F401
    CashBalance,
    EquityTransaction,
    HoldingsRollup,  # noqa: F401
    HoldingSummary,  # noqa: F401
    TotalReturn,  # noqa: F401
    Valuation,
)


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
_BENCHMARK_DISPLAY_NAMES = _display_name_map()




# ---------------------------------------------------------------------------
# Holdings -> summaries
# ---------------------------------------------------------------------------


def get_holdings(
    transactions: list[EquityTransaction],
    *,
    fx: FxRate | None = None,
    now: NowFn | None = None,
) -> dict:
    # Returns a :class:`investing.types.HoldingsRollup`-shaped dict;
    # kept as plain ``dict`` in the signature so the construction
    # below (which incrementally builds the payload) doesn't have
    # to satisfy ``mypy``'s strict-TypedDict narrowing.
    """Roll up transactions into per-ticker Holding summaries.

    ``fx`` is forwarded to every ``Holding`` so production can share a
    single ``ExchangeRate`` cache across the whole portfolio rather
    than re-fetching the same currency per ticker. ``now`` is the
    wall-clock plug used by :meth:`Holding.summary` to anchor any
    open-ended period; ``None`` keeps the legacy
    ``datetime.today`` behaviour.
    """
    fx = _fx_or_default(fx)
    trades = combine_and_sort(transactions)

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
                lambda t: (t, Holding(t, fx=fx, now=now)),
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

    current_holdings: list[dict] = []
    historical_holdings: list[dict] = []
    trade_events: list[dict] = []
    for holding in holdings.values():
        summary = holding.summary()
        if summary["is_current"]:
            current_holdings.append(summary)
        else:
            historical_holdings.append(summary)
        # Per-ticker bursts are grouped within each ``Holding`` and
        # then merged into one global, newest-first list so the page
        # reads like a chronological activity log across the whole
        # portfolio.
        trade_events.extend(holding.trade_events())

    return {
        "current": sorted(current_holdings, key=lambda item: item["latest_buy"], reverse=True),
        "historical": sorted(historical_holdings, key=lambda item: item["latest_sell"], reverse=True),
        # Sort by the burst's most recent event (so a multi-day burst
        # ranks by when it finished). Ties are broken by start date,
        # which only matters on synthetic / same-day-only data sets.
        "trades": sorted(
            trade_events,
            key=lambda e: (e["end_date"], e["start_date"]),
            reverse=True,
        ),
    }




# ---------------------------------------------------------------------------
# Portfolio-wide return / allocation
# ---------------------------------------------------------------------------


def calc_twr(
    valuations: list[Valuation],
    current_value: float,
    *,
    now: NowFn | None = None,
) -> dict:  # ``TotalReturn``-shaped dict; see :class:`investing.types.TotalReturn`.
    # Returns a :class:`investing.types.TotalReturn`-shaped dict;
    # see ``get_holdings`` for the rationale on keeping the
    # signature loose.
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
        twr *= (valuation["value"] / start_value)
        start_value = valuation["value"] + valuation["flow"]
        history.append((valuation["date"], twr))
    today = _now()
    if today.date() > valuations[-1]["date"].date():
        twr *= (current_value / start_value)
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




def summarize(
    holdings: dict,
    cash: list[CashBalance],
    *,
    fx: FxRate | None = None,
) -> float:
    """Compute allocations and weights, mutating ``holdings`` in place.

    Accepts an explicit ``fx`` rather than reaching for a module global
    so callers can plug in a stub for tests and a single shared
    ``ExchangeRate`` for production.
    """
    fx = _fx_or_default(fx)
    total_equity_value_usd = 0.0
    total_cash_value_usd = 0.0
    for holding in holdings["current"]:
        if holding["current_value_usd"] <= 0.0:
            # A current holding with zero/negative USD valuation is a
            # data fault upstream (yfinance returning no price, FX
            # collapsing to zero, ...). Bail out loudly so the build
            # stops rather than silently shipping a degenerate page.
            raise InvariantError(
                f"current holding {holding['ticker']!r} has non-positive "
                f"USD valuation: {holding['current_value_usd']!r}",
            )
        total_equity_value_usd += holding["current_value_usd"]
    for currency in cash:
        total_cash_value_usd += currency["amount"] * fx(currency["currency_code"])

    total_value_usd = total_equity_value_usd + total_cash_value_usd

    if total_value_usd > 0.0:
        # Unrounded percentages: ``current_weight%`` is summed when
        # building the "Other equities" bucket, and ``allocation%``
        # entries are read directly elsewhere. Rounding here would
        # leak into those derived numbers; we round only at display
        # time (``_render_bars`` formats with ``:.1f`` / ``:.2f``).
        holdings["allocation%"] = {
            "Equities": 100 * total_equity_value_usd / total_value_usd,
            "Cash & Cash Equivalents": 100 * total_cash_value_usd / total_value_usd,
        }
        logger.info(
            "Equity allocation: %s%%",
            _fmt_pct(holdings["allocation%"]["Equities"]),
        )
        logger.info(
            "Cash allocation: %s%%",
            _fmt_pct(holdings["allocation%"]["Cash & Cash Equivalents"]),
        )
    else:
        holdings["allocation%"] = None

    holdings["top_10"] = None
    weights: dict[str, float] = {}
    for holding in holdings["current"]:
        holding["current_weight%"] = 100 * holding["current_value_usd"] / total_value_usd
        weights[holding["ticker"]] = holding["current_weight%"]
        logger.info(
            "%s - %s - Weight: %s%% - TSR: %s%% - CAGR: %s%%",
            holding["ticker"],
            holding["name"],
            _fmt_pct(holding["current_weight%"]),
            _fmt_pct(holding["tsr%"]),
            _fmt_pct(holding["cagr%"]),
        )
    if weights:
        ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) > 11:
            holdings["top_10"] = dict(ranked[:10] + [("Other equities", sum(w for _, w in ranked[10:]))])
        else:
            holdings["top_10"] = dict(ranked)

    return total_value_usd




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
    ):
        self._ticker_symbol = ticker
        self._start_date = start_date
        self._start_date_str = start_date.strftime("%Y-%m-%d")
        self._fx = _fx_or_default(fx)
        # Stashed so :meth:`summary` can compute the period length
        # for CAGR without reaching into ``_holding`` (which has its
        # own clock plug for the same purpose).
        self._now: NowFn = now if now is not None else datetime.today
        self._holding = Holding(ticker, fx=self._fx, now=now)
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
        self._dates = history.index.to_numpy().astype("datetime64[D]")
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
            [d.date() for d, _ in reference_history], dtype="datetime64[D]",
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
        if (
            len(multipliers) > 1
            and ref_dates[-1] >= self._dates[-1]
            and start_price > 0.0
        ):
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

    def summary(self, reference_history) -> dict:
        """Produce the per-benchmark dict the renderer consumes.

        Computes a buy-and-hold TSR / CAGR from
        :attr:`start_basis_price` (Yahoo's back-adjusted close on
        the first trading day) to ``regularMarketPrice`` (the live
        tape today). The same numerator + denominator pair anchors
        the chart's resampler, so the capsule TSR and the chart's
        right edge fall out of a single arithmetic source and agree
        by construction.

        Note we deliberately do NOT route this through
        :meth:`Holding.summary`: that path is Modified-Dietz over
        actual cashflows in the as-of-trade-date share frame, and
        marks open positions to market by walking forward through
        any intervening splits to translate the position quantity
        into post-all-splits units. Our basis is already in
        post-all-future-splits units (Yahoo back-adjusts ``Adj
        Close``), so any further split-adjustment of a "1 share"
        synthetic trade would double-count the split and overstate
        the return. Computing the ratio directly here keeps the
        Benchmark's price-frame contract local and unambiguous.
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


def get_benchmarks(
    total_return_history: list[tuple[datetime, float]],
    *,
    fx: FxRate | None = None,
    now: NowFn | None = None,
) -> list[dict]:
    # Each entry is a :class:`investing.types.BenchmarkSummary`-
    # shaped dict; signature uses ``list[dict]`` to keep call sites
    # in :func:`cli.main` agnostic to mypy's TypedDict narrowing.
    fx = _fx_or_default(fx)
    start_date = total_return_history[0][0]
    benchmarks: list[dict] = []
    for cfg in BENCHMARKS:
        benchmark = Benchmark(cfg.ticker, start_date, fx=fx, now=now)
        summary = benchmark.summary(total_return_history)
        benchmarks.append(summary)
        logger.info(
            "%s - %s - TSR: %s%% - CAGR: %s%%",
            summary["ticker"],
            summary["name"],
            _fmt_pct(summary["tsr%"]),
            _fmt_pct(summary["cagr%"]),
        )
    return benchmarks
