"""``Holding`` -- per-ticker bookkeeping (positions,
periods, inflows / outflows / dividends, TSR / CAGR).
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
from .types import HoldingSummary, TradeEvent  # noqa: F401 (re-exported as documentation)

WITHHOLDING_TAX_RATE = 0.15


DAYS_YEAR = 365.2425



# When a Holding has near-zero average capital deployed (e.g. fully sold and
# repurchased within a small window) the CAGR formula explodes. Anything
# above this sentinel is rendered as "TBA" rather than a misleading number.
CAGR_TBA_THRESHOLD = math.nextafter(1_000_000, 0)




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
            self._ticker.get_info, description="yfinance get_info",
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
        self._periods: list[dict] = []
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
            [s["split"] for s in self._splits], dtype=float,
        )
        # Lazy daily-close cache used only by :meth:`_close_on` to
        # resolve a sub-period's ex-dividend boundary in the chained
        # TWR walk. ``None`` means "not fetched yet" -- a holding
        # with no dividends never triggers the fetch.
        self._history_cache: tuple[np.ndarray, np.ndarray] | None = None

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
            lambda: self._ticker.splits, description="yfinance splits",
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
            dividends.append({
                "date": _ts_to_datetime(ts),
                "dividend": float(dividend),
            })
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
            bisect.bisect_left(self._split_dates, after_date)
            : bisect.bisect_right(self._split_dates, before_date)
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

    def _ensure_history(self) -> tuple[np.ndarray, np.ndarray] | None:
        """Lazily fetch + cache the ticker's daily ``Close`` series.

        Returns parallel ``(dates, closes)`` numpy arrays in the
        current (post-all-splits) share frame -- yfinance's
        ``Close`` column with ``auto_adjust=False`` is split-
        adjusted but not dividend-adjusted, which is exactly the
        frame the chained TWR walk operates in.

        Holdings with no dividends never call this method, so
        the extra HTTPS round trip is paid only when the chain
        genuinely needs an ex-dividend boundary close.
        """
        if self._history_cache is not None:
            return self._history_cache
        if not self._positions:
            return None
        start = self._positions[0]["date"].strftime("%Y-%m-%d")
        history = self.fetch_market_history(start=start)
        # ``ffill`` then ``bfill`` covers any NaN run in the response
        # (a missing day inherits the previous trading day's close;
        # a leading NaN inherits the first finite close). The
        # downstream ``np.searchsorted`` then doesn't have to dodge
        # NaNs at lookup time.
        closes = history["Close"].ffill().bfill().to_numpy(dtype=float)
        idx = history.index
        # See the same dance in :class:`Benchmark.__init__` -- a
        # tz-aware DatetimeIndex (LSE / NYSE / ...) collapsed via a
        # naive ``.to_numpy().astype("datetime64[D]")`` would shift
        # BST trading days back one calendar day in UTC. Drop the
        # tz without converting so the date keys preserve the
        # exchange-local calendar dates the bars actually represent.
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        dates = idx.to_numpy().astype("datetime64[D]")
        self._history_cache = (dates, closes)
        return self._history_cache

    def _close_on(self, date: datetime) -> float:
        """Return the ``Close`` on the trading day at-or-before ``date``.

        Forward-fills across non-trading days (weekends / holidays)
        so any calendar date the dividend timeline lands on resolves
        to a usable close. The first available row is the floor;
        a date earlier than every history row falls back to that
        first close (vanishingly rare in practice -- the history
        fetch starts from the earliest trade date).
        """
        cache = self._ensure_history()
        if cache is None or cache[0].size == 0:
            raise InvariantError(
                "no daily-history rows available -- cannot resolve "
                "close-on-date for the chained-TWR sub-period bounded "
                "by a dividend event",
            )
        dates, closes = cache
        target = np.datetime64(date.date(), "D")
        idx = int(np.searchsorted(dates, target, side="right")) - 1
        if idx < 0:
            idx = 0
        return float(closes[idx])

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
                current_quantity, self._positions[-1]["date"], trade.date)
        self._trade_events.append({
            "date": trade.date,
            "price": trade.price,
            "quantity": trade.quantity,
            "category": category,
            "pre_quantity": current_quantity,
        })
        self._inflows.append({
            "date": trade.date,
            "value": trade.quantity * trade.price * self._fx(self._info["currency"], trade.date),
        })
        if self._positions and self._positions[-1]["date"] == trade.date:
            self._positions[-1]["quantity"] += trade.quantity
        else:
            self._positions.append({
                "date": trade.date,
                "quantity": current_quantity + trade.quantity,
            })

    def sell(self, trade: Trade) -> None:
        current_quantity = self._positions[-1]["quantity"]
        if trade.date > self._positions[-1]["date"]:
            current_quantity = self._apply_splits_between(
                current_quantity, self._positions[-1]["date"], trade.date)
        is_closing = (current_quantity - trade.quantity == 0)
        # Categorise as CLOSE iff this SELL would zero the position
        # out (in the same split-adjusted units the rest of this
        # method uses). Otherwise it's a partial DECREASE.
        # ``pre_quantity`` carries the split-adjusted holding right
        # before this SELL so the combiner can compute "X% decrease
        # over previous state" using a denominator denominated in the
        # same share frame as ``trade.quantity``.
        self._trade_events.append({
            "date": trade.date,
            "price": trade.price,
            "quantity": trade.quantity,
            "category": "CLOSE" if is_closing else "DECREASE",
            "pre_quantity": current_quantity,
        })
        if is_closing:
            self._periods[-1]["end"] = trade.date
        self._outflows.append({
            "date": trade.date,
            "value": trade.quantity * trade.price * self._fx(self._info["currency"], trade.date),
        })
        if self._positions[-1]["date"] == trade.date:
            self._positions[-1]["quantity"] -= trade.quantity
        else:
            self._positions.append({
                "date": trade.date,
                "quantity": current_quantity - trade.quantity,
            })

    def trade_events(
        self,
        *,
        window_days: int = TRADE_WINDOW_DAYS,
    ) -> list[dict]:
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
            self._trade_events, window_days=window_days,
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
        its chart curve's right-edge sample to the same number the
        modified-Dietz TSR below the chart computes against, instead
        of clipping to the latest adjusted close already in the
        ``history()`` response (which lags by intraday / overnight
        movement against the live tape ``regularMarketPrice``
        reflects).
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

    def fetch_market_history(self, *, start, interval: str = "1d",
                             auto_adjust: bool = False):
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
                start=start, interval=interval, auto_adjust=auto_adjust,
            ),
            description="yfinance ticker history",
        )

    def summary(self) -> dict:
        # Returns a :class:`investing.types.HoldingSummary`-shaped
        # dict; signature kept loose so mypy doesn't fight the
        # incremental dict construction below.
        """Compute chained-TWR TSR/CAGR plus the renderer-facing payload.

        Walks BUY / SELL / DIVIDEND events chronologically and
        accumulates a multiplier across each sub-period bounded by
        adjacent events. Within a sub-period nothing happens except
        market motion; at a dividend event we add the after-tax
        dividend yield (``per_share_after_tax / close_on_div_date``)
        to the running multiplier. Closed-period gaps where the
        position was fully sold pause the walk -- the next BUY
        re-anchors a fresh sub-period at the actual trade price.

        Withholding tax (``WITHHOLDING_TAX_RATE``) only applies to
        the dividend-yield term; the price-only sub-period return
        carries no tax drag (capital-gains tax is intentionally
        not modelled in this report).

        Real BUY / SELL prices come from the spreadsheet (via the
        ``Trade`` records funnelled into ``_trade_events``); the
        close-on-date for DIVIDEND boundaries comes from yfinance
        daily history (fetched lazily on first use); the final
        mark-to-market for an open position uses the live
        ``regularMarketPrice``. All three sources are denominated
        in the **current** (post-all-splits) share frame, so
        spreadsheet trade prices/quantities are rebased on the fly
        via :meth:`_split_factor_strictly_after` before they enter
        the walk.
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
        # every quantity / price into the current frame on the fly,
        # so the chain reads as a single share-frame timeline.
        events: list[tuple[datetime, int, str, dict]] = []
        for ev in self._trade_events:
            factor = self._split_factor_strictly_after(ev["date"])
            kind = "BUY" if ev["category"] in _BUY_CATEGORIES else "SELL"
            events.append((
                ev["date"], 0, kind,
                {
                    "price_native": ev["price"] / factor,
                    "quantity": ev["quantity"] * factor,
                },
            ))
        for div in self._dividends:
            events.append((
                div["date"], 1, "DIV",
                {"per_share_native": div["dividend"]},
            ))
        events.sort(key=lambda e: (e[0], e[1]))

        twr = 1.0
        quantity = 0.0
        prev_price: float | None = None
        total_ownership_length = 0
        period_started: datetime | None = None

        for date, _, kind, payload in events:
            if kind in ("BUY", "SELL"):
                event_price = payload["price_native"]
            else:  # DIV
                event_price = self._close_on(date)

            # Sub-period price-only return -- only when shares were
            # held for the whole stretch from ``prev_price`` (the
            # previous event's marking price) to ``event_price``.
            if quantity > 0 and prev_price is not None and prev_price > 0:
                twr *= event_price / prev_price

            if kind == "BUY":
                if quantity == 0:
                    period_started = date
                quantity += payload["quantity"]
                # Re-anchor at the actual trade price the user paid;
                # the next sub-period measures appreciation from
                # *that* price, not the close on the trade date.
                prev_price = event_price
            elif kind == "SELL":
                quantity -= payload["quantity"]
                if quantity <= 1e-9:
                    if period_started is not None:
                        total_ownership_length += max(
                            (date - period_started).days, 1,
                        )
                        period_started = None
                    quantity = 0.0
                    # Reset so the next BUY (if any) starts a fresh
                    # sub-period without compounding the gap's
                    # market motion into TSR.
                    prev_price = None
                else:
                    prev_price = event_price
            elif kind == "DIV":
                if quantity > 0 and event_price > 0:
                    # Withholding tax applies *only* here: the
                    # capital-return component of the chained walk
                    # (price ratios across BUY/SELL/MTM boundaries)
                    # is tax-free by design.
                    div_after_tax = (
                        payload["per_share_native"]
                        * (1.0 - WITHHOLDING_TAX_RATE)
                    )
                    twr *= 1.0 + div_after_tax / event_price
                    prev_price = event_price

        # Final mark-to-market for an open position. Closes the
        # currently-open ownership period with the live tape price
        # so the chart reads ``Adj Close[start] -> regularMarketPrice``
        # -- the same numerator + denominator the comparison
        # benchmark uses for its capsule TSR.
        last_quantity = quantity
        if last_quantity > 0:
            live_price = float(self._info["regularMarketPrice"])
            if prev_price is not None and prev_price > 0:
                twr *= live_price / prev_price
            if period_started is not None:
                total_ownership_length += max(
                    (now - period_started).days, 1,
                )
                period_started = None

        if total_ownership_length > 0:
            cagr = twr ** (DAYS_YEAR / total_ownership_length) - 1.0
        else:
            cagr = 0.0
        tsr = twr - 1.0

        if last_quantity > 0:
            current_value_usd = (
                last_quantity
                * self._info["regularMarketPrice"]
                * self._fx(currency)
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
            "cagr%": cagr * 100,
            "is_current": self._positions[-1]["quantity"] > 0,
            "current_weight%": None,
            "current_value_usd": current_value_usd,
            "periods": list(reversed(self._periods)),
            "latest_buy": self._inflows[-1]["date"],
            "latest_sell": self._outflows[-1]["date"] if self._outflows else None,
        }
