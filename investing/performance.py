"""Portfolio-wide rollups: TWR, allocations, top-10
weights, benchmark fetch.
"""
from __future__ import annotations

import math
from datetime import datetime

from .formatting import _fmt_pct
from .fx import _fx_or_default
from .holdings import DAYS_YEAR, Holding
from .log import logger
from .trades import ACTIONS, Trade, combine_and_sort

# Display labels for benchmarks whose ticker is not user-friendly.
_BENCHMARK_DISPLAY_NAMES = {"LSE:VUAA.L": "S&P 500"}




# ---------------------------------------------------------------------------
# Holdings -> summaries
# ---------------------------------------------------------------------------


def get_holdings(transactions, *, fx=None):
    """Roll up transactions into per-ticker Holding summaries.

    ``fx`` is forwarded to every ``Holding`` so production can share a
    single ``ExchangeRate`` cache across the whole portfolio rather
    than re-fetching the same currency per ticker.
    """
    fx = _fx_or_default(fx)
    trades = combine_and_sort(transactions)

    holdings: dict[str, Holding] = {}
    for trade in trades:
        if trade.ticker not in holdings:
            holdings[trade.ticker] = Holding(trade.ticker, fx=fx)
        assert trade.action in ACTIONS
        if trade.action == "BUY":
            holdings[trade.ticker].buy(trade)
        else:
            holdings[trade.ticker].sell(trade)

    current_holdings = []
    historical_holdings = []
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


def calc_twr(valuations, current_value):
    if not valuations:
        return {"start_date": datetime.today(), "history": [], "twr%": 0.0, "cagr%": 0.0}
    valuations = sorted(valuations, key=lambda item: item["date"])
    total_return = {
        "start_date": valuations[0]["date"],
        "history": [],
    }
    start_value = valuations[0]["value"] + valuations[0]["flow"]
    twr = 1.0
    total_return["history"].append((valuations[0]["date"], twr))
    for valuation in valuations[1:]:
        twr *= (valuation["value"] / start_value)
        start_value = valuation["value"] + valuation["flow"]
        total_return["history"].append((valuation["date"], twr))
    if datetime.today().date() > valuations[-1]["date"].date():
        twr *= (current_value / start_value)
        total_return["history"].append((datetime.today(), twr))
    cagr = twr ** (DAYS_YEAR / max((datetime.today() - total_return["start_date"]).days, 1)) - 1.0
    twr -= 1.0
    # Store unrounded percentages; consumers (capsule delta vs
    # benchmark, chart pp-delta overlay, OG-image headline) require
    # the full precision so subtraction doesn't compound the 0.05 pp
    # error of single-decimal rounding. Display sites round at
    # format time with ``:.1f``.
    total_return["twr%"] = twr * 100
    total_return["cagr%"] = cagr * 100
    logger.info(
        "JG - Jan Grzybek - TWR: %s%% - CAGR: %s%%",
        _fmt_pct(total_return["twr%"]),
        _fmt_pct(total_return["cagr%"]),
    )
    return total_return




def summarize(holdings, cash, *, fx=None):
    """Compute allocations and weights, mutating ``holdings`` in place.

    Accepts an explicit ``fx`` rather than reaching for a module global
    so callers can plug in a stub for tests and a single shared
    ``ExchangeRate`` for production.
    """
    fx = _fx_or_default(fx)
    total_equity_value_usd = 0.0
    total_cash_value_usd = 0.0
    for holding in holdings["current"]:
        assert holding["current_value_usd"] > 0.0
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
    used to inline. The previous code reached into ``Holding._ticker``
    twice -- once with ``auto_adjust=False`` to discover the
    first-day opening price (so a Trade could be planted at the
    timeline start) and once with ``auto_adjust=True`` for the
    cumulative return curve. Both reads now live on this class
    behind well-named methods so a future change (different data
    source, multiple benchmarks, currency-converted prices, ...)
    has a single edit surface and ``Holding`` keeps its private
    Yahoo Ticker handle to itself.
    """

    def __init__(self, ticker: str, start_date, fx=None):
        self._ticker_symbol = ticker
        self._start_date = start_date
        self._start_date_str = start_date.strftime("%Y-%m-%d")
        self._fx = _fx_or_default(fx)
        # Cache the un-adjusted (for the first-day opening price) and
        # adjusted (for the cumulative-return curve) histories. Two
        # separate Yahoo calls are unavoidable -- auto_adjust=True
        # rewrites the Open series, losing the actual trade price.
        self._holding = Holding(ticker, fx=self._fx)
        self._unadj_history = self._holding._ticker.history(
            start=self._start_date_str, interval="1d", auto_adjust=False,
        )
        self._adj_history = self._holding._ticker.history(
            start=self._start_date_str, interval="1d", auto_adjust=True,
        )

    @property
    def start_open_price(self) -> float:
        """Opening price on the first trading day at / after start_date.

        Used to plant a synthetic 1-share Trade so :meth:`Holding.summary`
        can produce the TSR / CAGR numbers next to the benchmark name.
        """
        return float(self._unadj_history["Open"].iloc[0])

    def cumulative_return_series(self, reference_history):
        """Walk the adjusted-close series and emit ``(date, multiplier)``
        pairs aligned to ``reference_history``'s timeline.

        The portfolio's TWR has one data point per valuation upload;
        the benchmark trading days don't line up exactly with that
        cadence (weekends, holidays, sample-day shifts), so we walk
        both timelines in lockstep and pick the right Yahoo close for
        each reference timestamp. The first entry pins the curve at
        ``1.0`` so subsequent values read as cumulative multipliers
        relative to the portfolio start.
        """
        history = self._adj_history
        start_price = float(history["Open"].iloc[0])
        series = [(self._start_date, 1.0)]
        ref_idx = 1
        for idx, row in enumerate(history.itertuples()):
            close_price = float(history["Close"].iloc[idx])
            prev_close_price = float(history["Close"].iloc[idx - 1])
            if math.isnan(close_price):
                assert not math.isnan(prev_close_price)
                close_price = prev_close_price
            ref_date = reference_history[ref_idx][0]
            date = row.Index.to_pydatetime()
            if date.date() < ref_date.date():
                continue
            elif date.date() == ref_date.date():
                series.append((ref_date, close_price / start_price))
                ref_idx += 1
            else:
                series.append((ref_date, prev_close_price / start_price))
                ref_idx += 1
                ref_date = reference_history[ref_idx][0]
                if date.date() == ref_date.date():
                    series.append((ref_date, close_price / start_price))
                    ref_idx += 1
        if len(series) < len(reference_history):
            close_price = float(history["Close"].iloc[-1])
            if math.isnan(close_price):
                close_price = float(history["Close"].iloc[-2])
                assert not math.isnan(close_price)
            series.append((reference_history[-1][0], close_price / start_price))
        assert len(series) == len(reference_history), (
            len(series), len(reference_history))
        return series

    def summary(self, reference_history) -> dict:
        """Produce the per-benchmark dict the renderer consumes.

        Bootstraps a synthetic Trade at the start date so
        :meth:`Holding.summary` returns the TSR / CAGR / period
        metadata, then attaches the resampled cumulative-return
        series under ``history`` (the chart's data lane).
        """
        self._holding.buy(Trade(
            self._start_date,
            self._ticker_symbol,
            1,
            self.start_open_price,
            "BUY",
        ))
        summary = self._holding.summary()
        summary["history"] = self.cumulative_return_series(reference_history)
        return summary


# Benchmark tickers the page renders alongside the JG portfolio
# curve. Kept module-level so the renderer can look up display
# labels via ``_BENCHMARK_DISPLAY_NAMES`` without re-deriving the
# list, and so a future second-benchmark experiment is just an
# append away.
_BENCHMARK_TICKERS: list[str] = ["VUAA.L"]


def get_benchmarks(total_return_history, *, fx=None):
    fx = _fx_or_default(fx)
    start_date = total_return_history[0][0]
    benchmarks: list[dict] = []
    for ticker in _BENCHMARK_TICKERS:
        benchmark = Benchmark(ticker, start_date, fx=fx)
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
