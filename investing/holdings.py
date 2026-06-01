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
from .fx import _fx_or_default
from .trades import TRADE_WINDOW_DAYS, Trade, _combine_trade_events
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
    def __init__(self, ticker, *, fx=None, now: NowFn | None = None):
        self._ticker = yf.Ticker(ticker)
        self._info = self._ticker.get_info()
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

    def _get_splits_dividends(self):
        """Bootstrap the per-ticker splits / dividends timelines.

        Splits are stored in chronological order with their raw
        per-event factor; the *cumulative* factor used by the
        dividend adjustment below is derived in one ``np.cumprod``
        sweep over the reversed sequence (so each entry carries the
        product of itself and every later split). The legacy
        nested-loop version was O(K^2) in the number of splits and
        mutated the accumulator in-place; the vectorised path is
        O(K) and keeps the sidecar arrays we expose as
        ``_split_dates`` / ``_split_factors`` consistent with the
        original ``_splits`` list.

        Dividend adjustment uses ``bisect_right`` to find the first
        split strictly after the dividend's record date in O(log K)
        per dividend, replacing the manually-walked ``split_idx``
        cursor whose advancement only happened on certain branches
        and was easy to misread.
        """
        splits: list[dict] = []
        split_dates: list[datetime] = []
        for ts, split in self._ticker.splits.items():
            date = _ts_to_datetime(ts)
            splits.append({"date": date, "split": float(split)})
            split_dates.append(date)
        if splits:
            factors = np.array([s["split"] for s in splits], dtype=float)
            # Cumulative product walked from the future back to the
            # past: ``cum_factor[i]`` is the multiplier that lifts
            # share counts in effect right after split ``i`` into
            # post-all-future-splits units. Equivalent to the legacy
            # in-place ``_split *= split`` accumulation for every
            # earlier entry, but in one numpy pass.
            cum = np.flip(np.cumprod(np.flip(factors)))
        else:
            cum = np.empty(0, dtype=float)

        dividends: list[dict] = []
        for ts, dividend in self._ticker.get_dividends().items():
            date = _ts_to_datetime(ts)
            dividend = float(dividend)
            # Find the first split at or after the dividend's record
            # date; any split strictly earlier than the dividend has
            # already been baked into the live share count, so we
            # only need to scale by the cumulative product of
            # remaining splits to express the per-share dividend in
            # post-all-splits units. ``bisect_left`` matches the
            # legacy ``split.date >= dividend.date`` boundary -- a
            # dividend that lands on a split date still picks up
            # that split's factor. O(log K).
            idx = bisect.bisect_left(split_dates, date)
            if idx < len(cum):
                dividend *= cum[idx]
            dividends.append({"date": date, "dividend": dividend})
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

    def buy(self, trade: Trade):
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

    def sell(self, trade: Trade):
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

    def _add_dividends(self):
        """Compose the cashflow timeline for TSR/CAGR by appending each
        in-position dividend (after withholding tax + FX) to the
        outflows list.

        For every dividend record, locate the position row that was
        active on the dividend date via ``bisect`` (positions are
        recorded in chronological order by ``buy``/``sell``), then
        scale the live share count for any splits that landed
        strictly between ``position.date`` and ``dividend.date``.
        Same exclusive-both-ends contract as
        :meth:`_apply_splits_between`; a split colliding with the
        position date raises ``InvariantError`` so the operator can
        spot the ambiguity rather than silently double the holding.
        The legacy implementation walked positions and splits with
        two manually-advanced cursors and was easy to misread; the
        new code is one ``bisect`` per dividend (positions) plus
        the same ``bisect`` slice the splits helper already uses.
        """
        outflows = list(self._outflows)
        if not self._positions or not self._dividends:
            return outflows
        position_dates = [p["date"] for p in self._positions]
        for dividend in self._dividends:
            div_date = dividend["date"]
            # ``bisect_right`` -> 1 + index of last position with
            # date <= div_date. Any dividend strictly before the
            # first position is skipped (no holding to attach to).
            idx = bisect.bisect_right(position_dates, div_date) - 1
            if idx < 0:
                continue
            position = self._positions[idx]
            if div_date <= position["date"] or position["quantity"] <= 0:
                # ``<=`` mirrors the legacy ``dividend.date >
                # position.date`` boundary: a dividend recorded
                # **on** the same day as a position write is
                # ambiguous (was the new share count already
                # entitled to the dividend?), so we drop it the
                # same way the previous implementation did.
                continue
            quantity = position["quantity"]
            # Walk every split strictly between the position date
            # and the dividend date. ``bisect_right`` on the
            # position end skips any same-day split (matches the
            # legacy ``split.date > position.date`` test); the
            # dividend end uses ``bisect_left`` so a split landing
            # on the dividend record date is excluded as well
            # (matches ``dividend.date <= split.date``).
            lo = bisect.bisect_right(self._split_dates, position["date"])
            hi = bisect.bisect_left(self._split_dates, div_date)
            if self._split_dates[
                bisect.bisect_left(self._split_dates, position["date"]) : lo
            ]:
                # ``bisect_left != bisect_right`` on the position
                # date means at least one split sits exactly on it.
                raise InvariantError(
                    "stock split coincides with a position "
                    "boundary -- cannot determine which "
                    "side of the split receives the dividend",
                )
            if lo < hi:
                quantity = int(
                    quantity * float(self._split_factors[lo:hi].prod())
                )
            outflows.append({
                "date": div_date,
                "value": (quantity * dividend["dividend"]
                          * (1.0 - WITHHOLDING_TAX_RATE)
                          * self._fx(self._info["currency"], div_date)),
            })
        return outflows

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

    def summary(self) -> dict:
        # Returns a :class:`investing.types.HoldingSummary`-shaped
        # dict; signature kept loose so mypy doesn't fight the
        # incremental dict construction below.
        outflows = self._add_dividends()
        tsr = 1.0
        total_ownership_length = 0
        for period in self._periods:
            start = period["start"]
            if period["end"] is None:
                end = self._now()
                outflows.append({
                    "date": end,
                    "value": (self._positions[-1]["quantity"] * self._info["regularMarketPrice"] *
                              self._fx(self._info["currency"])),
                })
            else:
                end = period["end"]
            length = max((end - start).days, 1)
            total_ownership_length += length
            gain = 0.0
            avg_capital = 0.0
            for inflow in self._inflows:
                if start <= inflow["date"] < end:
                    gain -= inflow["value"]
                    avg_capital += (max((end - inflow["date"]).days, 1) / length) * inflow["value"]
            for outflow in outflows:
                if start < outflow["date"] <= end:
                    gain += outflow["value"]
                    avg_capital -= ((end - outflow["date"]).days / length) * outflow["value"]
            tsr *= (1.0 + gain / avg_capital)
        cagr = tsr ** (DAYS_YEAR / total_ownership_length) - 1.0
        tsr -= 1.0
        current_value_usd = (self._positions[-1]["quantity"] * self._info["regularMarketPrice"] *
                             self._fx(self._info["currency"]))
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
