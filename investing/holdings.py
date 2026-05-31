"""``Holding`` -- per-ticker bookkeeping (positions,
periods, inflows / outflows / dividends, TSR / CAGR).
"""
from __future__ import annotations

import math
from datetime import datetime

import yfinance as yf

from .formatting import _ts_to_datetime
from .fx import _fx_or_default
from .trades import TRADE_WINDOW_DAYS, Trade, _combine_trade_events

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
    def __init__(self, ticker, *, fx=None):
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

    def _get_splits_dividends(self):
        splits: list[dict] = []
        splits_acc: list[dict] = []
        for ts, split in self._ticker.splits.items():
            date = _ts_to_datetime(ts)
            splits.append({"date": date, "split": split})
            for _split in splits_acc:
                _split["split"] *= split
            splits_acc.append({"date": date, "split": split})
        # readjust dividends for splits
        dividends = []
        split_idx = 0
        for ts, dividend in self._ticker.get_dividends().items():
            date = _ts_to_datetime(ts)
            for split in splits_acc[split_idx:]:
                if split["date"] >= date:
                    dividend *= split["split"]
                    break
                split_idx += 1
            dividends.append({"date": date, "dividend": dividend})
        return splits, dividends

    def _apply_splits_between(self, quantity, after_date, before_date):
        """Adjust ``quantity`` for every split strictly between ``after_date``
        and ``before_date`` (exclusive both ends)."""
        for split in self._splits:
            if before_date <= split["date"]:
                break
            assert after_date != split["date"]
            assert before_date != split["date"]
            if split["date"] > after_date:
                quantity = int(quantity * split["split"])
        return quantity

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
        outflows = list(self._outflows)
        position_idx = 0
        split_idx = 0
        for dividend in self._dividends:
            if position_idx >= len(self._positions):
                break
            while True:
                position = self._positions[position_idx]
                if dividend["date"] > position["date"]:
                    if (position_idx + 1 < len(self._positions) and
                            self._positions[position_idx + 1]["date"] < dividend["date"]):
                        position_idx += 1
                    elif position["quantity"] > 0:
                        quantity = position["quantity"]
                        for split in self._splits[split_idx:]:
                            if dividend["date"] <= split["date"]:
                                break
                            assert split["date"] != position["date"]
                            if split["date"] > position["date"]:
                                quantity = int(quantity * split["split"])
                            else:
                                split_idx += 1
                        outflows.append({
                            "date": dividend["date"],
                            "value": (quantity * dividend["dividend"] * (1.0 - WITHHOLDING_TAX_RATE) *
                                      self._fx(self._info["currency"], dividend["date"])),
                        })
                        break
                    else:
                        break
                else:
                    break
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

    def summary(self):
        outflows = self._add_dividends()
        tsr = 1.0
        total_ownership_length = 0
        for period in self._periods:
            start = period["start"]
            if period["end"] is None:
                end = datetime.today()
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
