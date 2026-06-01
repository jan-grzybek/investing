"""``Holding`` -- per-ticker bookkeeping (positions,
periods, inflows / outflows / dividends, return / IRR).
"""

from __future__ import annotations

import bisect
import math
from datetime import datetime

import numpy as np
import yfinance as yf

from .clock import NowFn
from .errors import InvariantError
from .formatting import _ts_to_datetime
from .fx import FxRate, _fx_or_default
from .market_data import _call_with_retry
from .trades import _BUY_CATEGORIES, TRADE_WINDOW_DAYS, Trade, _combine_trade_events
from .types import HoldingPeriod, HoldingSummary, TradeEvent

WITHHOLDING_TAX_RATE = 0.15


DAYS_YEAR = 365.2425


# When a Holding has near-zero invested capital across an extreme runup
# (e.g. a $1 starter position 10x'd before a large top-up at the new high)
# the XIRR can blow up to silly annualised rates. Anything above this
# sentinel is rendered as "TBA" rather than a misleading headline.
CAGR_TBA_THRESHOLD = math.nextafter(1_000_000, 0)


def _xirr(
    cashflows: list[tuple[datetime, float]],
    *,
    low: float = -0.999,
    high: float = 1.0e4,
    max_iter: int = 200,
    tol: float = 1.0e-9,
) -> float:
    """Annualised internal rate of return for an irregular cashflow series.

    ``cashflows`` is a list of ``(date, amount)`` pairs in any order;
    negative amounts represent outflows from the investor (BUYs),
    positive amounts represent inflows (SELLs, dividends, the
    open-position mark-to-market). Returns the rate ``r`` such that
    the net present value of every cashflow discounted at ``r`` is
    zero -- i.e. the same number Excel's ``=XIRR(...)`` and a typical
    brokerage "Personal Rate of Return" figure would report.

    Algorithm: bisection on ``[low, high]``. The NPV function is
    monotonic in ``r`` whenever the cashflow series has a single
    sign change (the typical retail BUY-then-everything-positive
    pattern, by Descartes' rule), so the root is unique and the
    bracket converges quickly. Bisection is preferred over Newton
    for robustness -- the derivative can pass close to zero on
    degenerate inputs and a Newton step would overshoot wildly.

    Returns ``math.nan`` if the bracket can't capture a sign change
    after one widening pass; the caller is expected to gate on the
    existing ``CAGR_TBA_THRESHOLD`` sentinel and render "TBA" in
    that case.
    """
    if len(cashflows) < 2:
        return math.nan
    cashflows = sorted(cashflows, key=lambda cf: cf[0])
    base_date = cashflows[0][0]
    items = [((cf[0] - base_date).days / DAYS_YEAR, cf[1]) for cf in cashflows]
    has_pos = any(amount > 0 for _, amount in items)
    has_neg = any(amount < 0 for _, amount in items)
    if not (has_pos and has_neg):
        return math.nan

    def npv(r: float) -> float:
        return sum(amount / ((1.0 + r) ** t) for t, amount in items)

    f_low = npv(low)
    f_high = npv(high)
    # One widening pass: a tiny initial position that has 100x'd in a
    # week can land outside the default ``[-0.999, 1e4]`` window.
    # Walk the upper bound upward (geometric) before giving up so the
    # bisect still has a hope of bracketing.
    while f_low * f_high > 0 and high < 1.0e8:
        high *= 10.0
        f_high = npv(high)
    if f_low * f_high > 0:
        return math.nan

    for _ in range(max_iter):
        mid = (low + high) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol:
            return mid
        if f_low * f_mid < 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
        if abs(high - low) < tol:
            break
    return (low + high) / 2.0


# ---------------------------------------------------------------------------
# Per-ticker bookkeeping
# ---------------------------------------------------------------------------


class Holding:
    def __init__(
        self,
        ticker: str,
        *,
        fx: FxRate | None = None,
        now: NowFn | None = None,
    ):
        # ``yf.Ticker(...)`` construction is cheap (no network); the
        # subsequent ``get_info`` call is what crosses the wire and
        # earns the retry wrapper. ``_get_splits_dividends`` reaches
        # for ``self._ticker.splits`` / ``get_dividends`` which are
        # the other two network round-trips on this critical path.
        self._ticker = yf.Ticker(ticker)
        self._info = _call_with_retry(
            self._ticker.get_info,
            description="yfinance get_info",
        )
        self._splits, self._dividends = self._get_splits_dividends()
        # FX callable: an ``ExchangeRate`` instance in production
        # (constructed once per ``main`` invocation and shared across
        # every Holding so each currency is fetched at most once), a
        # test stub in unit tests. Defaulting to a fresh instance
        # keeps ad-hoc construction working without leaking state
        # across tests via a module-level singleton.
        self._fx = _fx_or_default(fx)
        # ``now`` is the "what time is it?" plug used by
        # :meth:`summary` to bound any open-ended period at the
        # current moment. ``None`` falls back to ``datetime.today``
        # (which the legacy ``freeze_today`` fixture still patches);
        # new tests can pass an explicit closure to avoid the
        # cross-module monkeypatch.
        self._now: NowFn = now if now is not None else datetime.today
        self._positions: list[dict] = []
        self._periods: list[HoldingPeriod] = []
        self._inflows: list[dict] = []
        self._outflows: list[dict] = []
        # Per-trade events with their semantic category. Populated by
        # ``buy``/``sell`` as a side-effect; the categorisation needs
        # the pre-trade (BUY) or post-trade (SELL) position quantity
        # which those methods already have in hand, so we record the
        # event there rather than re-deriving it later.
        self._trade_events: list[dict] = []
        # Sidecars of ``self._splits`` -- the split dates as a sorted
        # list (so ``bisect`` can resolve "which splits land between
        # date A and date B?" in O(log K + slice)) and the matching
        # split factors as a numpy array of floats (so the running
        # product across a slice is one ``.prod()`` call rather than
        # a Python loop). Built once in ``_get_splits_dividends`` so
        # the per-trade ``_apply_splits_between`` lookup never has to
        # rebuild them.
        self._split_dates: list[datetime] = [s["date"] for s in self._splits]
        self._split_factors = np.array(
            [s["split"] for s in self._splits],
            dtype=float,
        )

    def _get_splits_dividends(self):
        """Bootstrap the per-ticker splits / dividends timelines.

        Splits are stored in chronological order with their raw
        per-event factor; the chained-TWR walk in :meth:`summary`
        rebases events into the current share frame on the fly via
        :meth:`_split_factor_strictly_after`, so no separate
        cumulative-product table is materialised here.

        Dividends are stored verbatim as yfinance reports them --
        ``Ticker.dividends`` returns per-share values denominated
        in **post-all-splits share units** (an empirically
        verifiable claim: AAPL's pre-2014-7:1-and-pre-2020-4:1
        dividends come back at ``raw_paid / 28``). The chained
        walk's ``quantity`` is also tracked in the current share
        frame, so multiplying the two gives the actual cash the
        holder received without an intermediate "raw at time of
        payment" hop.
        """
        splits: list[dict] = []
        # ``Ticker.splits`` is a cached attribute that triggers an
        # HTTP fetch on first access; wrap the iteration entry-point
        # so a transient 5xx / 429 is absorbed rather than aborting
        # the whole build.
        raw_splits = _call_with_retry(
            lambda: self._ticker.splits,
            description="yfinance splits",
        )
        for ts, split in raw_splits.items():
            date = _ts_to_datetime(ts)
            splits.append({"date": date, "split": float(split)})

        dividends: list[dict] = []
        raw_dividends = _call_with_retry(
            self._ticker.get_dividends,
            description="yfinance get_dividends",
        )
        for ts, dividend in raw_dividends.items():
            dividends.append(
                {
                    "date": _ts_to_datetime(ts),
                    "dividend": float(dividend),
                }
            )
        return splits, dividends

    def _apply_splits_between(self, quantity, after_date, before_date):
        """Adjust ``quantity`` for every split strictly between ``after_date``
        and ``before_date`` (exclusive both ends).

        Raises ``InvariantError`` if a split lands exactly on either
        boundary -- the surrounding code splits by ``trade.date`` and
        relies on the exclusive-both-ends contract to avoid double-
        counting; a same-day split would silently double the holding.

        ``bisect`` resolves both ends of the active slice in
        O(log K), and the running product is one ``.prod()`` over
        the affected entries -- the legacy linear scan walked the
        whole split list per call and rebuilt the cumulative factor
        with a Python multiply. K is usually small (most tickers
        have 0-2 splits over a portfolio's lifetime), so the
        absolute saving is tiny -- the value is in dropping the
        per-trade hot-path overhead and in the boundary check
        getting expressed as two array bounds instead of an inline
        equality test.
        """
        if not self._split_dates:
            return quantity
        # ``bisect_left`` finds the first split with date >= after.
        # We want strictly greater, so bump past any equal entries by
        # comparing the raw dates explicitly. Same trick on the
        # ``before`` end with ``bisect_right`` -> first split with
        # date > before, then trim equal-date splits off the right.
        lo = bisect.bisect_right(self._split_dates, after_date)
        hi = bisect.bisect_left(self._split_dates, before_date)
        # The exclusive-both-ends invariant: if any split's date
        # equals ``after_date`` or ``before_date``, ``bisect_left`` /
        # ``bisect_right`` collapse those entries onto the same
        # side; we still need to surface the explicit error so the
        # caller knows the split-vs-trade boundary is ambiguous.
        for date in self._split_dates[
            bisect.bisect_left(self._split_dates, after_date) : bisect.bisect_right(
                self._split_dates, before_date
            )
        ]:
            if date in (after_date, before_date):
                raise InvariantError(
                    "stock split coincides with a trade boundary -- "
                    "cannot determine which side of the split owns the shares",
                )
        if lo >= hi:
            return quantity
        factor = float(self._split_factors[lo:hi].prod())
        return int(quantity * factor)

    def _split_factor_strictly_after(self, date: datetime) -> float:
        """Product of split factors for splits with ``split.date > date``.

        Used by :meth:`summary` to lift a spreadsheet trade's raw
        share count and per-share price into the **current**
        (post-all-splits) share frame: ``current_qty = raw_qty *
        f`` and ``current_price = raw_price / f``. The two
        adjustments are inverses, so the trade's notional value is
        invariant -- the conversion just re-denominates both sides
        into the frame yfinance's ``Close`` column and the live
        ``regularMarketPrice`` already report in.

        Splits exactly on ``date`` are excluded because
        :meth:`_apply_splits_between` already raises
        ``InvariantError`` when a split coincides with a trade
        boundary; the strictly-after slice would silently pick a
        side without that explicit check.
        """
        if not self._split_dates:
            return 1.0
        idx = bisect.bisect_right(self._split_dates, date)
        if idx >= len(self._split_factors):
            return 1.0
        return float(self._split_factors[idx:].prod())

    def buy(self, trade: Trade) -> None:
        try:
            current_quantity = self._positions[-1]["quantity"]
        except IndexError:
            current_quantity = 0
        # Decide OPEN vs INCREASE on the pre-trade quantity. A
        # subsequent split adjustment is a multiplicative factor
        # that can't flip a non-zero quantity to zero, so this branch
        # is stable across the ``elif`` below -- but we DO need the
        # split-adjusted figure (computed in that ``elif``) for the
        # ``pre_quantity`` we expose to the renderer: the "% increase
        # over previous state" must be measured in the share frame
        # that ``trade.quantity`` is denominated in, otherwise a 2:1
        # split between the last position write and this BUY would
        # silently halve the reported denominator and overstate the
        # percentage.
        category = "OPEN" if current_quantity == 0 else "INCREASE"
        if current_quantity == 0:
            self._periods.append({"start": trade.date, "end": None})
        elif trade.date > self._positions[-1]["date"]:
            current_quantity = self._apply_splits_between(
                current_quantity, self._positions[-1]["date"], trade.date
            )
        self._trade_events.append(
            {
                "date": trade.date,
                "price": trade.price,
                "quantity": trade.quantity,
                "category": category,
                "pre_quantity": current_quantity,
            }
        )
        self._inflows.append(
            {
                "date": trade.date,
                "value": trade.quantity
                * trade.price
                * self._fx(self._info["currency"], trade.date),
            }
        )
        if self._positions and self._positions[-1]["date"] == trade.date:
            self._positions[-1]["quantity"] += trade.quantity
        else:
            self._positions.append(
                {
                    "date": trade.date,
                    "quantity": current_quantity + trade.quantity,
                }
            )

    def sell(self, trade: Trade) -> None:
        current_quantity = self._positions[-1]["quantity"]
        if trade.date > self._positions[-1]["date"]:
            current_quantity = self._apply_splits_between(
                current_quantity, self._positions[-1]["date"], trade.date
            )
        is_closing = current_quantity - trade.quantity == 0
        # Categorise as CLOSE iff this SELL would zero the position
        # out (in the same split-adjusted units the rest of this
        # method uses). Otherwise it's a partial DECREASE.
        # ``pre_quantity`` carries the split-adjusted holding right
        # before this SELL so the combiner can compute "X% decrease
        # over previous state" using a denominator denominated in the
        # same share frame as ``trade.quantity``.
        self._trade_events.append(
            {
                "date": trade.date,
                "price": trade.price,
                "quantity": trade.quantity,
                "category": "CLOSE" if is_closing else "DECREASE",
                "pre_quantity": current_quantity,
            }
        )
        if is_closing:
            self._periods[-1]["end"] = trade.date
        self._outflows.append(
            {
                "date": trade.date,
                "value": trade.quantity
                * trade.price
                * self._fx(self._info["currency"], trade.date),
            }
        )
        if self._positions[-1]["date"] == trade.date:
            self._positions[-1]["quantity"] -= trade.quantity
        else:
            self._positions.append(
                {
                    "date": trade.date,
                    "quantity": current_quantity - trade.quantity,
                }
            )

    def trade_events(
        self,
        *,
        window_days: int = TRADE_WINDOW_DAYS,
    ) -> list[TradeEvent]:
        """Return this ticker's burst-aggregated trades for the
        "Trades" section.

        Every burst this holding has ever recorded comes through -- the
        section is now a complete activity log rather than a rolling
        window. The reader can still drill into "what happened most
        recently?" via the sortable date column on the rendered table.
        Each row is decorated with the identifying ``ticker`` / ``name``
        / ``currency`` so the renderer can produce a self-contained
        row without holding a reference to the originating ``Holding``.
        """
        combined = _combine_trade_events(
            self._trade_events,
            window_days=window_days,
        )
        return [
            {
                **event,
                "ticker": f"{self._info['exchange']}:{self._info['symbol']}",
                "name": self._info["longName"],
                "currency": self._info["currency"],
            }
            for event in combined
        ]

    @property
    def current_market_price(self) -> float:
        """The ticker's latest reported price in its native currency.

        Reads ``regularMarketPrice`` straight off the ``get_info``
        snapshot fetched in ``__init__`` -- the same value
        :meth:`summary` uses to mark the open position to market.
        Exposed so :class:`investing.performance.Benchmark` can pin
        its chart curve's right-edge sample to the same number its
        capsule TSR computes against, instead of clipping to the
        latest adjusted close already in the ``history()`` response
        (which lags by intraday / overnight movement against the
        live tape ``regularMarketPrice`` reflects).
        """
        return float(self._info["regularMarketPrice"])

    @property
    def info(self) -> dict:
        """Read-only view of the cached ``get_info`` snapshot.

        Exposes the fields :class:`investing.performance.Benchmark`
        needs to render its summary dict (``currency`` for FX
        conversion, ``exchange`` / ``symbol`` for the ticker id,
        ``longName`` for the display label) without reaching into
        ``_info`` directly. The dict is the live cache, so callers
        must not mutate it.
        """
        return self._info

    def fetch_market_history(self, *, start, interval: str = "1d", auto_adjust: bool = False):
        """Return the underlying ticker's price history.

        Public accessor used by :class:`investing.performance.Benchmark`
        to pull the index's price series without reaching into the
        private ``_ticker`` attribute. Wrapped in the standard retry
        helper so transient yfinance failures get the same treatment
        the rest of the package gets.

        ``start`` is forwarded verbatim (yfinance accepts either an
        ISO date string or a ``date``-shaped object). The other
        keyword arguments default to the values the benchmark
        codepath has used historically.
        """
        return _call_with_retry(
            lambda: self._ticker.history(
                start=start,
                interval=interval,
                auto_adjust=auto_adjust,
            ),
            description="yfinance ticker history",
        )

    def summary(self) -> HoldingSummary:
        """Compute money-weighted return / IRR plus the renderer payload.

        The per-holding figures answer "how did *I* do on this
        position" rather than "how did the security itself perform".
        Concretely we build the actual cashflow timeline the
        investor experienced and reduce it to two numbers:

        * ``tsr%`` is the **money multiple minus one**, expressed
          as a percentage. A 1.45x multiple becomes ``tsr% =
          45.0``. Total dollars returned (sells + after-tax
          dividends + open mark-to-market) divided by total
          dollars invested. Dividends are treated as **cash** --
          no reinvestment assumption -- because that matches what
          the holder actually experienced (the cheque hit the
          brokerage account).

        * ``cagr%`` is the **annualised IRR** (Excel's ``=XIRR``)
          of the cashflow series. Solves for the rate that makes
          the NPV of every BUY (negative), SELL (positive),
          dividend (positive, post-15%-tax), and the synthetic
          mark-to-market at ``now`` (positive, open positions
          only) sum to zero. Captures the time-value-of-money
          effect that MoIC alone misses.

        Together they form the standard PE/VC + brokerage
        "personal performance" pair: MoIC for cumulative
        magnitude, XIRR for annualised time-weighted rate.
        Withholding tax (``WITHHOLDING_TAX_RATE``) reduces the
        dividend cashflow at the moment it's recorded; capital
        gains on closed sells are not taxed (the report does not
        model the investor's local capital-gains regime).

        Real BUY / SELL prices come from the spreadsheet (via
        ``Trade`` records funnelled into ``_trade_events``); the
        open-position mark-to-market uses the live
        ``regularMarketPrice``. Quantities are rebased into the
        current (post-all-splits) share frame on the fly via
        :meth:`_split_factor_strictly_after` so dividend
        cashflows (yfinance reports per-share values in current
        frame) multiply through cleanly with the running share
        count.
        """
        now = self._now()
        currency = self._info["currency"]

        # Defensive guard mirrored from the old ``_add_dividends`` and
        # ``_apply_splits_between`` checks: a split landing exactly on
        # a trade date makes the share-frame conversion below
        # ambiguous (was the trade in pre- or post-split units?).
        # ``_apply_splits_between`` already raises in ``buy``/``sell``
        # for any *subsequent* trade, but the very first trade has no
        # earlier position to anchor that check against -- this guard
        # closes that gap.
        trade_dates = {ev["date"] for ev in self._trade_events}
        collisions = trade_dates.intersection(set(self._split_dates))
        if collisions:
            raise InvariantError(
                "stock split coincides with a trade boundary -- "
                "cannot determine which side of the split owns the shares",
            )

        # Build the event timeline. Trades carry priority 0 (they
        # come first within a calendar day so a same-day BUY-then-DIV
        # records the new shares before the dividend is paid out);
        # dividends carry priority 1. Splits aren't first-class
        # events here -- ``_split_factor_strictly_after`` rebases
        # the quantity tracker into the current share frame on the
        # fly, which is the only frame ``_dividends`` is denominated
        # in.
        events: list[tuple[datetime, int, str, dict]] = []
        for ev in self._trade_events:
            factor = self._split_factor_strictly_after(ev["date"])
            kind = "BUY" if ev["category"] in _BUY_CATEGORIES else "SELL"
            events.append(
                (
                    ev["date"],
                    0,
                    kind,
                    {
                        # Native value of the trade -- multiplying raw
                        # quantity by raw price is invariant under any
                        # subsequent split (so we don't need to rebase
                        # this number, only the running quantity counter
                        # below).
                        "trade_value_native": ev["quantity"] * ev["price"],
                        # Quantity in current share frame, used to know
                        # how many shares were held when each dividend
                        # is paid out.
                        "qty_current": ev["quantity"] * factor,
                    },
                )
            )
        for div in self._dividends:
            events.append(
                (
                    div["date"],
                    1,
                    "DIV",
                    {"per_share_current": div["dividend"]},
                )
            )
        events.sort(key=lambda e: (e[0], e[1]))

        cashflows: list[tuple[datetime, float]] = []
        # Dollars-in vs dollars-out, both in USD, for MoIC. Tracked
        # alongside the cashflow timeline because the XIRR solver
        # only needs the signed timeline, not the magnitudes.
        gross_invested = 0.0
        gross_returned = 0.0

        quantity_current = 0.0
        period_started: datetime | None = None
        total_ownership_length = 0

        for date, _, kind, payload in events:
            if kind == "BUY":
                if quantity_current == 0:
                    period_started = date
                quantity_current += payload["qty_current"]
                cash_usd = payload["trade_value_native"] * self._fx(currency, date)
                cashflows.append((date, -cash_usd))
                gross_invested += cash_usd
            elif kind == "SELL":
                quantity_current -= payload["qty_current"]
                cash_usd = payload["trade_value_native"] * self._fx(currency, date)
                cashflows.append((date, +cash_usd))
                gross_returned += cash_usd
                if quantity_current <= 1e-9:
                    if period_started is not None:
                        total_ownership_length += max(
                            (date - period_started).days,
                            1,
                        )
                        period_started = None
                    quantity_current = 0.0
            elif kind == "DIV":
                if quantity_current > 0:
                    # Withholding tax applies *only* here. Capital
                    # returns flowing through SELL / MTM cashflows
                    # are untaxed by design (capital gains tax is
                    # not modelled).
                    cash_usd = (
                        quantity_current
                        * payload["per_share_current"]
                        * (1.0 - WITHHOLDING_TAX_RATE)
                        * self._fx(currency, date)
                    )
                    if cash_usd > 0:
                        cashflows.append((date, +cash_usd))
                        gross_returned += cash_usd

        # Synthetic mark-to-market for an open position: as if the
        # holder sold at ``regularMarketPrice`` today. Together with
        # the actual BUY/SELL/DIV cashflows this gives XIRR enough
        # signal to bracket a root.
        if quantity_current > 0:
            mtm_usd = (
                quantity_current * float(self._info["regularMarketPrice"]) * self._fx(currency)
            )
            cashflows.append((now, +mtm_usd))
            gross_returned += mtm_usd
            if period_started is not None:
                total_ownership_length += max(
                    (now - period_started).days,
                    1,
                )
                period_started = None

        # MoIC - 1, in percent. Reuses the historical ``tsr%`` key
        # so the renderer / sort attrs / OG image / capsule layout
        # don't have to plumb a new field; the disclaimer carries
        # the methodology change.
        if gross_invested > 0:
            tsr = gross_returned / gross_invested - 1.0
        else:
            tsr = 0.0

        # XIRR via the bisection solver. ``math.nan`` (no bracket
        # found, e.g. degenerate cashflow series) propagates through
        # the multiplication and the renderer will hit the
        # ``CAGR_TBA_THRESHOLD`` guard which already exists for
        # extreme rates -- ``nan > threshold`` is ``False`` in
        # Python, so we explicitly map ``nan`` to ``inf`` to take
        # the "TBA" branch.
        irr = _xirr(cashflows)
        if math.isnan(irr):
            irr = math.inf

        if quantity_current > 0:
            current_value_usd = (
                quantity_current * self._info["regularMarketPrice"] * self._fx(currency)
            )
        else:
            current_value_usd = 0.0

        return {
            "ticker": f"{self._info['exchange']}:{self._info['symbol']}",
            "name": self._info["longName"],
            # Store unrounded percentages so downstream callers
            # (delta vs benchmark, top-10 weight summing, OG-image
            # hero) can do further math without compounding rounding
            # error. Display sites round at format time with ``:.1f``.
            "tsr%": tsr * 100,
            "cagr%": irr * 100,
            "is_current": self._positions[-1]["quantity"] > 0,
            "current_weight%": None,
            "current_value_usd": current_value_usd,
            "periods": list(reversed(self._periods)),
            "latest_buy": self._inflows[-1]["date"],
            "latest_sell": self._outflows[-1]["date"] if self._outflows else None,
        }
