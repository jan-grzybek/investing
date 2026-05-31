"""Build the JG Investing portfolio page.

Pipeline:

    pull_data        ->  rows from a Google Sheet
    get_holdings     ->  per-ticker summaries (TSR, CAGR, periods)
    summarize        ->  allocations & weights, total value
    calc_twr         ->  time-weighted return for the whole portfolio
    get_benchmarks   ->  benchmark (S&P 500 ETF) summary + history
    generate_webpage ->  index.html (single responsive page, inline SVG)

The page is intentionally self-contained: bar charts and the return chart
are emitted as inline SVG / CSS bars so there are no separate image
artefacts to deploy.
"""
from __future__ import annotations

import base64
import bisect
import hashlib
import html
import io
import json
import math
import os
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import gspread
import numpy as np
import requests
import yfinance as yf
from dateutil.relativedelta import relativedelta
from scipy.interpolate import PchipInterpolator


LOGOS_ADDRESS = "https://jan-grzybek.github.io/investing/logos/"
COURAGE_LOGO = LOGOS_ADDRESS + "courage.png"
LOGO_EXTENSIONS = (".svg", ".png", ".jpg")

# Local mirror of ``LOGOS_ADDRESS`` -- the same files served at the URL
# above live next to ``update.py`` in the repo (and ship as part of the
# Pages artifact). The OG image renderer rasterises logos for the
# top-10 strip and reads them straight from disk so it doesn't depend
# on the previous deploy being reachable.
_REPO_LOGOS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logos"
)

WITHHOLDING_TAX_RATE = 0.15
DAYS_YEAR = 365.2425

# When a Holding has near-zero average capital deployed (e.g. fully sold and
# repurchased within a small window) the CAGR formula explodes. Anything
# above this sentinel is rendered as "TBA" rather than a misleading number.
CAGR_TBA_THRESHOLD = math.nextafter(1_000_000, 0)

# Display labels for benchmarks whose ticker is not user-friendly.
_BENCHMARK_DISPLAY_NAMES = {"LSE:VUAA.L": "S&P 500"}


def _ts_to_datetime(ts) -> datetime:
    """Convert a pandas Timestamp (or any ISO-stringifiable date) to a
    naive ``datetime`` at midnight."""
    iso_date = str(ts).split()[0]
    return datetime.strptime(iso_date, "%Y-%m-%d")


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------


class ExchangeRate:
    def __init__(self):
        self._rates: dict[str, float] = {}
        self._history: dict[str, tuple[list, list]] = {}

    def _current(self, currency):
        if currency == "USD":
            return 1.0
        if currency in self._rates:
            return self._rates[currency]
        rate = yf.Ticker(f"{currency}USD=X").info["regularMarketPrice"]
        if currency == "GBp":
            rate /= 100
        self._rates[currency] = rate
        return rate

    def _historical(self, currency, date):
        if currency == "USD":
            return 1.0
        if currency not in self._history:
            hist = yf.Ticker(f"{currency}USD=X").history(
                period="max", interval="1d", auto_adjust=False)
            dates, rates = [], []
            for ts, close in hist["Close"].items():
                if math.isnan(close):
                    continue
                dates.append(_ts_to_datetime(ts).date())
                rates.append(float(close))
            self._history[currency] = (dates, rates)
        dates, rates = self._history[currency]
        if not dates:
            return self._current(currency)
        target = date.date() if isinstance(date, datetime) else date
        idx = max(bisect.bisect_right(dates, target) - 1, 0)
        rate = rates[idx]
        if currency == "GBp":
            rate /= 100
        return rate

    def __call__(self, currency, date=None):
        if date is None:
            return self._current(currency)
        return self._historical(currency, date)


exchange_rate = ExchangeRate()


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    date: datetime
    ticker: str
    quantity: int
    price: float
    action: str


ACTIONS = ["BUY", "SELL"]


def combine_and_sort(transactions):
    """Bucket transactions by (ticker, date, action), then aggregate each
    bucket into a single :class:`Trade` whose price is the volume-weighted
    average of its constituents.

    The result is sorted by ``(date, action)`` so that on intraday tie-breaks
    BUYs are processed before SELLs (matters for tax-loss harvesting cases).
    """
    buckets: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for txn in transactions:
        assert txn["action"] in ACTIONS, f"Action unknown: {txn['action']}"
        buckets[(txn["ticker"], txn["date"], txn["action"])].append(txn)

    trades: list[Trade] = []
    for (ticker, date, action), txns in buckets.items():
        total_quantity = sum(t["quantity"] for t in txns)
        total_value = sum(t["quantity"] * t["price_per_share"] for t in txns)
        trades.append(Trade(
            date=datetime.strptime(date, "%d-%m-%Y"),
            ticker=ticker,
            quantity=total_quantity,
            price=total_value / total_quantity,
            action=action,
        ))

    assert "BUY" in ACTIONS and "SELL" in ACTIONS
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
TRADES_YEARS_BACK = 1

# Reading these as a buy-vs-sell action partitions the four categories
# along the only axis that matters for grouping (same-action trades go
# together) and for picking the group's effective category (first event
# decides BUY bursts, last event decides SELL bursts).
_BUY_CATEGORIES = frozenset({"OPEN", "INCREASE"})

# Order used by the renderer; values double as the BEM modifier slug
# appended to the badge class. Keys are the canonical category tokens
# stored on each trade event. Labels are in the past tense because
# this section is an executed-trades log -- everything shown has
# already happened, so "Initiated" / "Increased" read as accurate
# event reports rather than the ongoing-action gerunds we'd want on
# a live order book. The verbs themselves ("Initiated" / "Divested")
# come from the long-term-investor / fund-letter idiom that
# describes positions as ownership stakes rather than tradable
# instruments. The renderer attaches " by X%" to the magnitude-
# bearing INCREASE / DECREASE labels at render time, so the dict
# stays a flat verb table and an INCREASE row missing a ``delta_pct``
# degrades to a sensible "Increased" badge rather than a broken
# "Increased by" prefix. The BEM modifiers stay aligned with the
# underlying token (``open`` / ``close``) so the CSS keeps reading
# as "this badge marks an open / close" regardless of which surface
# verb we pick.
_TRADE_CATEGORY_DISPLAY: dict[str, tuple[str, str]] = {
    "OPEN":     ("Initiated", "open"),
    "INCREASE": ("Increased", "increase"),
    "DECREASE": ("Decreased", "decrease"),
    "CLOSE":    ("Divested",  "close"),
}


def _combine_trade_events(events, *, window_days: int = TRADE_WINDOW_DAYS):
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
    for event in events:
        action = "BUY" if event["category"] in _BUY_CATEGORIES else "SELL"
        if groups:
            head = groups[-1][0]
            head_action = "BUY" if head["category"] in _BUY_CATEGORIES else "SELL"
            within_window = (event["date"] - head["date"]).days <= window_days
            if head_action == action and within_window:
                groups[-1].append(event)
                continue
        groups.append([event])

    combined: list[dict] = []
    for group in groups:
        total_qty = sum(e["quantity"] for e in group)
        # ``quantity`` is always positive here (the sheet ingestion
        # rejects zero / negative rows), so the divide is safe.
        weighted_price = (
            sum(e["quantity"] * e["price"] for e in group) / total_qty
        )
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
        combined.append({
            "start_date": group[0]["date"],
            "end_date": group[-1]["date"],
            "price": weighted_price,
            "category": category,
            "delta_pct": delta_pct,
        })
    return combined


# ---------------------------------------------------------------------------
# Per-ticker bookkeeping
# ---------------------------------------------------------------------------


class Holding:
    def __init__(self, ticker):
        self._ticker = yf.Ticker(ticker)
        self._info = self._ticker.get_info()
        self._splits, self._dividends = self._get_splits_dividends()
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
        splits = []
        splits_acc = []
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
            "value": trade.quantity * trade.price * exchange_rate(self._info["currency"], trade.date),
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
            "value": trade.quantity * trade.price * exchange_rate(self._info["currency"], trade.date),
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
                                      exchange_rate(self._info["currency"], dividend["date"])),
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
        years_back: int = TRADES_YEARS_BACK,
        today: datetime | None = None,
    ) -> list[dict]:
        """Return this ticker's burst-aggregated trades for the
        "Recent trades" section.

        Bursts older than ``years_back`` (measured against the burst's
        most recent event) are dropped so the page focuses on recent
        activity. The filter is intentionally lenient at the boundary:
        a multi-fill burst that started outside the window but whose
        last fill landed inside it survives whole, so a rolling-quarter
        accumulation that finished in the retention window reads as a
        single recent action rather than being chopped in half. Each
        kept row is decorated with the identifying ``ticker`` / ``name``
        / ``currency`` so the renderer can produce a self-contained
        card without holding a reference to the originating
        ``Holding``."""
        today = today or datetime.today()
        # Use the calendar-aware ``years`` accessor (via ``timedelta``
        # times the average year length) rather than ``replace(year=...)``,
        # which would fail on Feb 29 -- the cutoff doesn't need to be
        # exact to the day for the retention window.
        cutoff = today - timedelta(days=DAYS_YEAR * years_back)
        combined = _combine_trade_events(
            self._trade_events, window_days=window_days,
        )
        result: list[dict] = []
        for event in combined:
            if event["end_date"] < cutoff:
                continue
            result.append({
                **event,
                "ticker": f"{self._info['exchange']}:{self._info['symbol']}",
                "name": self._info["longName"],
                "currency": self._info["currency"],
            })
        return result

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
                              exchange_rate(self._info["currency"])),
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
                             exchange_rate(self._info["currency"]))
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


# ---------------------------------------------------------------------------
# Sheet ingestion
# ---------------------------------------------------------------------------


_YES_TOKENS = frozenset({"Y", "YES", "y", "yes"})
_BUY_TOKENS = frozenset({"B", "BUY", "b", "buy"})
_SELL_TOKENS = frozenset({"S", "SELL", "s", "sell"})


def _to_float(value: str) -> float:
    return float(value.replace(",", ""))


def _to_int(value: str) -> int:
    return int(value.replace(",", ""))


def pull_data():
    gc = gspread.service_account(filename="/tmp/gsheet_creds.json")
    sh = gc.open_by_key(os.environ["GSHEET_ID"])

    transactions = []
    for row in sh.worksheet("Equities").get_all_values()[2:]:
        if row[6] not in _YES_TOKENS:
            continue
        if row[5] in _BUY_TOKENS:
            action = "BUY"
        elif row[5] in _SELL_TOKENS:
            action = "SELL"
        else:
            assert False, f"Unknown action token: {row[5]!r}"
        transactions.append({
            "date": row[1],
            "ticker": row[2],
            "quantity": _to_int(row[3]),
            "price_per_share": _to_float(row[4]),
            "action": action,
        })

    valuations = []
    for row in sh.worksheet("Return").get_all_values()[2:]:
        if row[4] not in _YES_TOKENS:
            continue
        valuations.append({
            "date": datetime.strptime(row[1], "%d-%m-%Y"),
            "value": _to_float(row[2]),
            "flow": _to_float(row[3]),
        })

    cash = []
    for row in sh.worksheet("Cash & Cash Equivalents").get_all_values()[2:]:
        if row[4] not in _YES_TOKENS:
            continue
        cash.append({
            "currency_code": row[2],
            "amount": _to_float(row[3]),
        })

    return transactions, valuations, cash


# ---------------------------------------------------------------------------
# Holdings -> summaries
# ---------------------------------------------------------------------------


def get_holdings(transactions):
    trades = combine_and_sort(transactions)

    holdings: dict[str, Holding] = {}
    for trade in trades:
        if trade.ticker not in holdings:
            holdings[trade.ticker] = Holding(trade.ticker)
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
    print(f"\nJG - Jan Grzybek - TWR: {_fmt_pct(total_return['twr%'])}% - "
          f"CAGR: {_fmt_pct(total_return['cagr%'])}%")
    return total_return


def summarize(holdings, cash):
    total_equity_value_usd = 0.0
    total_cash_value_usd = 0.0
    for holding in holdings["current"]:
        assert holding["current_value_usd"] > 0.0
        total_equity_value_usd += holding["current_value_usd"]
    for currency in cash:
        total_cash_value_usd += currency["amount"] * exchange_rate(currency["currency_code"])

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
        print(f"Equity allocation: {_fmt_pct(holdings['allocation%']['Equities'])}%")
        print(f"Cash allocation: "
              f"{_fmt_pct(holdings['allocation%']['Cash & Cash Equivalents'])}%\n")
    else:
        holdings["allocation%"] = None

    holdings["top_10"] = None
    weights: dict[str, float] = {}
    for holding in holdings["current"]:
        holding["current_weight%"] = 100 * holding["current_value_usd"] / total_value_usd
        weights[holding["ticker"]] = holding["current_weight%"]
        print(f"{holding['ticker']} - {holding['name']} - "
              f"Weight: {_fmt_pct(holding['current_weight%'])}% - "
              f"TSR: {_fmt_pct(holding['tsr%'])}% - "
              f"CAGR: {_fmt_pct(holding['cagr%'])}%")
    if weights:
        ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) > 11:
            holdings["top_10"] = dict(ranked[:10] + [("Other equities", sum(w for _, w in ranked[10:]))])
        else:
            holdings["top_10"] = dict(ranked)

    return total_value_usd


def get_benchmarks(total_return_history):
    start_date = total_return_history[0][0]
    start_date_str = start_date.strftime("%Y-%m-%d")
    benchmarks = []
    for ticker in ["VUAA.L"]:
        holding = Holding(ticker)
        holding.buy(Trade(
            start_date,
            ticker,
            1,
            holding._ticker.history(start=start_date_str, interval="1d", auto_adjust=False)["Open"].iloc[0],
            "BUY",
        ))
        summary = holding.summary()

        history = holding._ticker.history(start=start_date_str, interval="1d", auto_adjust=True)
        start_price = float(history["Open"].iloc[0])
        summary["history"] = [(start_date, 1.0)]
        ref_idx = 1
        for idx, row in enumerate(history.itertuples()):
            close_price = float(history["Close"].iloc[idx])
            prev_close_price = float(history["Close"].iloc[idx - 1])
            if math.isnan(close_price):
                assert not math.isnan(prev_close_price)
                close_price = prev_close_price
            ref_date = total_return_history[ref_idx][0]
            date = row.Index.to_pydatetime()
            if date.date() < ref_date.date():
                continue
            elif date.date() == ref_date.date():
                summary["history"].append((ref_date, close_price / start_price))
                ref_idx += 1
            else:
                summary["history"].append((ref_date, prev_close_price / start_price))
                ref_idx += 1
                ref_date = total_return_history[ref_idx][0]
                if date.date() == ref_date.date():
                    summary["history"].append((ref_date, close_price / start_price))
                    ref_idx += 1
        if len(summary["history"]) < len(total_return_history):
            close_price = float(history["Close"].iloc[-1])
            if math.isnan(close_price):
                close_price = float(history["Close"].iloc[-2])
                assert not math.isnan(close_price)
            summary["history"].append((total_return_history[-1][0], close_price / start_price))
        assert len(summary["history"]) == len(total_return_history), (
            len(summary["history"]), len(total_return_history))

        benchmarks.append(summary)
        print(f"{benchmarks[-1]['ticker']} - {benchmarks[-1]['name']} - "
              f"TSR: {_fmt_pct(benchmarks[-1]['tsr%'])}% - "
              f"CAGR: {_fmt_pct(benchmarks[-1]['cagr%'])}%")
    return benchmarks


# ---------------------------------------------------------------------------
# Date / formatting helpers (used by the renderer)
# ---------------------------------------------------------------------------


def _fmt_date(dt) -> str:
    # ``%-d`` (GNU/BSD) drops the leading zero on the day number, so
    # we get "Mar 7, 2026" rather than "Mar 07, 2026". Long-form date
    # prose reads more naturally without the zero pad, which is also
    # how the page already renders the footer's "last updated" line.
    # The ISO ``<time datetime="...">`` attributes that wrap each
    # rendered date stay zero-padded -- that's the W3C machine
    # format and a separate concern from the human-facing label.
    return dt.strftime("%b %-d, %Y")


def _pluralize(count: int, singular: str) -> str:
    return f"1 {singular}" if count == 1 else f"{count} {singular}s"


def _format_duration(delta: relativedelta) -> str:
    """Format a ``relativedelta`` as 'N years, M months' (decade-capped)."""
    if delta.years >= 10:
        return _pluralize(delta.years, "year")
    parts = []
    if delta.years > 0:
        parts.append(_pluralize(delta.years, "year"))
    if delta.months > 0:
        parts.append(_pluralize(delta.months, "month"))
    if not parts:
        return "less than a month"
    return ", ".join(parts)


def _value_class(value: float) -> str:
    """CSS modifier reflecting the sign of a TSR/CAGR/TWR percentage."""
    return "value--negative" if value < 0 else "value--positive"


def _fmt_pct(value: float, *, signed: bool = False) -> str:
    """Format a percentage with one decimal up to 99.9 and as a whole
    number once the displayed magnitude reaches 100.

    A trailing ``.x`` next to a 3-digit integer part is visually
    noisy and adds no real precision to the reader -- ``100.3%``
    reads tidier as ``100%`` and ``672.9%`` as ``673%``. We apply
    the same rule to ``pp`` deltas (capsule + chart overlay + OG
    image) so the page is uniform: any quantity expressed in
    percent or percentage points drops its decimal once it hits
    triple digits.

    Boundary handling uses ``round(abs(value), 1) >= 100`` rather
    than the raw magnitude so values that round UP to the 100
    threshold (e.g. ``99.95`` -> ``100.0``) also shed the now-
    redundant decimal instead of rendering as ``100.0%``.
    ``signed=True`` prefixes a leading ``+`` for non-negative
    values, matching the existing ``:+.1f`` behaviour at delta
    sites.
    """
    sign_spec = "+" if signed else ""
    if round(abs(value), 1) >= 100:
        return format(value, f"{sign_spec}.0f")
    return format(value, f"{sign_spec}.1f")


def _sha256_b64(payload: str) -> str:
    """Base64 SHA-256 digest in the form CSP expects for hash sources.

    Browsers compute the digest of the inline script/style content
    (verbatim, without surrounding ``<script>``/``<style>`` tags) and
    require it to match a ``'sha256-<b64>'`` entry in the matching
    directive of the page's Content-Security-Policy."""
    return base64.b64encode(
        hashlib.sha256(payload.encode("utf-8")).digest()
    ).decode("ascii")


# ---------------------------------------------------------------------------
# Webpage renderer
# ---------------------------------------------------------------------------


# Embedded styles. Kept verbatim as a single string so ``save()`` stays
# linear and the dark-mode / print rules are easy to audit.
_PAGE_STYLES = """
:root {
  --bg: #ffffff;
  --fg: #111111;
  --muted: #555555;
  --line: #d8dde3;
  --accent: #e67d22;
  --accent-bench: #1f4e79;
  --positive: #1f7a3d;
  --negative: #b3261e;
  --card-bg: #fafafa;
  --max-width: 880px;
  --radius: 12px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418;
    --fg: #e8eaed;
    --muted: #a0a4ab;
    --line: #2c3138;
    --accent: #f29a4f;
    --accent-bench: #6ea8d8;
    --positive: #58c97f;
    --negative: #ff6b63;
    --card-bg: #181c22;
  }
  /* The dashed 0% reference line on the return chart inherits
     ``var(--muted)`` (see the base rule above). In dark mode
     that mid-grey wash blends into the deep page background, so
     swap the stroke to the lighter foreground colour while
     keeping the base opacity / stroke-width. The dashed pattern
     stays elegant but the baseline is now clearly traceable
     across the chart. */
  .return-chart__ref { stroke: var(--fg); }
  /* Recolour every wordmark logo for dark surfaces by negating
     luminance while preserving hue. Many holdings ship dark-on-
     transparent SVGs (Adobe, S&P Global, Baidu, Meta, Salesforce,
     Samsung, ...) that brands authored to sit on a light page
     and that all but disappear against the dark page / card
     background here. Rather than wrap each logo in a light chip
     (which paints chrome around every image), we transform the
     logo's own pixels:

       * ``invert(1)`` negates every RGB channel, so pure black
         flips to pure white, dark SAP blue flips to a pale
         peach, dark Meta blue flips to a sand orange, NVIDIA's
         brand green flips to magenta, etc. Crucially the alpha
         channel is untouched, so transparent areas of the SVG
         stay transparent and the page background still shows
         through around each glyph.
       * ``hue-rotate(180deg)`` then rotates the hue wheel by a
         half-turn, which undoes the colour-channel flip
         introduced by ``invert(1)`` (invert is equivalent to a
         180deg rotation of hue plus an inversion of luminance,
         so chaining a second 180deg rotation cancels the hue
         flip and leaves only the luminance inversion behind).
         The net effect: dark colours become light versions of
         the same hue (SAP and Salesforce stay blue, NVIDIA
         stays green, Adobe stays red, ...), and any near-white
         pixels become near-black ones -- which we accept because
         brand-coloured glyphs sit on transparent backgrounds in
         the SVGs we ship, not on opaque white plates, so this
         degenerate case doesn't actually arise.

     Identical rule for the standalone holding card logo, the
     trade card logo, the JG/benchmark compare capsule logo, and
     the marquee ticker logos because they share the same
     vanishing-on-dark-surface problem. ``opacity: 1`` overrides
     the marquee ticker's ``opacity: 0.92`` (set globally to
     soften the strip against a pure-white card in light mode)
     so the recoloured glyphs stay fully crisp in dark mode where
     they're doing all the readability work.

     Two assets are explicitly opted out via attribute-``:not()``
     because they're not single-hue wordmarks that benefit from
     a luminance flip:

       * ``LSE:VUAA.L`` (the S&P 500 ETF benchmark) ships as the
         US flag; inverting it turns the red stripes cyan and
         the blue canton orange, which reads as "some other
         country's flag" rather than "USA". The flag's mid-tone
         palette is already legible on the dark surface.
       * ``courage.png`` is the per-ticker fallback shown when a
         holding has no on-file logo; it's a coloured
         illustration whose meaning would be similarly destroyed
         by a luminance flip, and it's also already legible.

     The exclusion matches the URL's basename (``$=`` suffix
     match) rather than the full path so the rule stays robust
     against ``LOGOS_ADDRESS`` changes, and ``:where()`` keeps
     the four logo selectors compact while leaving the rule at
     zero specificity (which matters only if a future override
     needs to win against it). */
  :where(
    .ticker__logo, .holding__logo,
    .trade__logo,  .returns-compare__logo
  ):not([src$="courage.png"], [src$="VUAA.L.svg"]) {
    filter: invert(1) hue-rotate(180deg);
    opacity: 1;
  }
}
@media print {
  body { background: white; color: black; }
  .holding, .bars, .return-chart, .section { break-inside: avoid; }
  .ticker { display: none; }
}
*, *::before, *::after { box-sizing: border-box; }
html {
  color-scheme: light dark;
  background: var(--bg);
}
body {
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 16px;
  line-height: 1.5;
  margin: 0 auto;
  max-width: var(--max-width);
  padding: 32px 24px 48px;
  -webkit-font-smoothing: antialiased;
}
img { max-width: 100%; height: auto; }
main { display: block; }
/* Smooth-scroll for in-page nav (clicking ``Performance`` /
   ``Current`` / ``Historical`` in the sticky header). The
   ``:focus-within`` gate is intentional and not an accident:
   applying ``scroll-behavior: smooth`` directly to ``html`` makes
   Chromium-based browsers ANIMATE the scroll when restoring
   position on a normal refresh -- which on a page deep into the
   holdings list (or whose URL carries a section hash from a prior
   nav click) feels like the page is scrolling down uncontrollably,
   especially while lazy-loaded logos progressively grow the
   document and keep retargeting the smooth-scroll destination.
   Anchor link clicks naturally focus the target element, so this
   selector matches at exactly the right moment for smooth nav
   scrolling without ever triggering the refresh-restoration
   animation. */
html:focus-within { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) {
  html:focus-within { scroll-behavior: auto; }
  /* Don't animate the ticker for users who request reduced motion;
     wrap into a static row so all logos remain visible at once. */
  .ticker__track {
    animation: none;
    flex-wrap: wrap;
    width: auto;
    justify-content: center;
  }
}
/* Skip link: invisible until a keyboard user tabs to it, at which
   point it pops in above the sticky header so screen-reader and
   keyboard users can bypass the nav and jump straight to <main>. */
.skip-link {
  position: absolute;
  left: 8px;
  top: 8px;
  padding: 8px 14px;
  background: var(--bg);
  color: var(--fg);
  border: 2px solid var(--accent);
  border-radius: 8px;
  text-decoration: none;
  font-weight: 600;
  font-size: 0.9375rem;
  z-index: 100;
  transform: translateY(-150%);
  transition: transform 120ms ease-out;
}
.skip-link:focus,
.skip-link:focus-visible { transform: translateY(0); outline: none; }
main:focus-visible { outline: none; }
.site-header {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px 24px;
  margin: -32px -24px 32px;
  padding: 14px 24px;
  background: color-mix(in srgb, var(--bg) 85%, transparent);
  backdrop-filter: saturate(180%) blur(14px);
  -webkit-backdrop-filter: saturate(180%) blur(14px);
  border-bottom: 1px solid var(--line);
  /* Force a dedicated GPU compositing layer so the sticky header
     does not flicker on iOS Safari while the page scrolls. Without
     this hint Safari briefly drops the header's backdrop-filter
     on each anchor-driven scroll (visible as a "blink" when
     tapping the nav links on iPhone). The translateZ(0) +
     isolation pair stabilises both the stacking context and the
     paint layer; ``will-change`` keeps the layer warm so the
     blur isn't rebuilt on every frame. */
  -webkit-transform: translateZ(0);
          transform: translateZ(0);
  isolation: isolate;
  will-change: backdrop-filter;
}
/* When the decorative current-holdings ticker is on the page it
   sits directly under the sticky nav. The header's bottom margin
   becomes wasteful empty space in that case -- the nav already
   carries its own ``border-bottom`` and the ticker has its own
   internal padding, so a gap on top of all that just pushes the
   first content section needlessly far down. Collapse the bottom
   margin to zero so the ticker hugs the nav cleanly. The
   ``:has()`` selector keeps this conditional: pages without a
   ticker keep the original 32px gap before their first section.
   ``:has()`` is supported in all current evergreen browsers
   (Chrome 105+, Firefox 121+, Safari 15.4+); older engines simply
   fall back to the original spacing, which is benign. */
body:has(.ticker) .site-header { margin-bottom: 0; }
.site-title {
  font-size: 1.875rem;
  font-weight: 800;
  letter-spacing: -0.03em;
  margin: 0;
  line-height: 1.15;
}
.site-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  font-size: 0.9375rem;
}
.site-nav a {
  color: var(--muted);
  text-decoration: none;
  font-weight: 500;
  padding: 4px 12px;
  border-radius: 999px;
  transition: background 150ms ease, color 150ms ease;
}
.site-nav a:hover,
.site-nav a:focus-visible {
  background: var(--card-bg);
  color: var(--fg);
  outline: none;
}
/* Slow horizontal "stocks tape" of current-holdings logos. The track
   contains two copies of the logo set so a 0 -> -50% translateX
   keyframe loops seamlessly. The mask gradient fades both ends so
   logos enter and exit gracefully rather than popping at the edges. */
.ticker {
  margin: 0 -24px 24px;
  overflow: hidden;
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
  background: var(--card-bg);
  -webkit-mask-image: linear-gradient(to right,
    transparent 0%, #000 6%, #000 94%, transparent 100%);
          mask-image: linear-gradient(to right,
    transparent 0%, #000 6%, #000 94%, transparent 100%);
}
.ticker__track {
  display: flex;
  align-items: center;
  gap: 40px;
  width: max-content;
  animation: ticker-scroll 60s linear infinite;
}
.ticker:hover .ticker__track,
.ticker:focus-within .ticker__track {
  animation-play-state: paused;
}
/* Normalize logos to a uniform-area cell so every holding reads
   at the same visual prominence along the marquee. The portfolio's
   wordmarks span a wide aspect-ratio range -- from square-ish (SAP,
   Apple, Meta, ~1:1) to long banners (Salesforce, NVIDIA, S&P
   Global, ~4:1) -- and earlier strategies each had a failure mode:

     * Pinning both axes to a square (the original 48x48) lets
       ``object-fit: contain`` work, but it letterboxes wide logos
       into a tiny strip at the centre of the cell while square
       logos fill the whole box, so the wide ones look 3-4x
       smaller in ink even though they occupy the same cell.
     * Pinning height only (the height-only step we tried after
       that) gives every logo identical height, but wide logos
       then claim their full natural width on the marquee track
       and the strip reads as a parade of giant brand banners
       sandwiching tiny square ones.

   The fix is a landscape 2:1 cell with ``object-fit: contain``:
   each ``<img>`` becomes a fixed 56x28 box and the SVG is fitted
   inside, preserving its aspect ratio. The math behind the 2:1
   ratio is intentional -- a square logo hits the height cap and
   renders as 28x28 (ink area 784 px^2), a 4:1 wide logo hits the
   width cap and renders as 56x14 (ink area also 784 px^2), and
   2:1 logos (the geometric mean) hit both caps and render at the
   full 56x28 (ink area 1568 px^2). The result is a symmetric
   prominence curve around the 2:1 sweet spot rather than a
   monotonic "wider == bigger" or "wider == smaller" bias, so the
   strip feels uniform regardless of which logos happen to be in
   the current portfolio. The 28px height is also a touch smaller
   than the prior 32px height-only target so the logos sit
   visibly inside the marquee strip rather than crowding its
   vertical padding. */
.ticker__logo {
  width: 56px;
  height: 28px;
  object-fit: contain;
  flex: 0 0 auto;
  opacity: 0.92;
}
@keyframes ticker-scroll {
  from { transform: translateX(0); }
  to { transform: translateX(-50%); }
}
/* ``scroll-margin-top`` reserves space at the top of the viewport
   when an anchor (``#trades`` etc.) targets the section so the
   sticky ``.site-header`` doesn't cover the title. The reserve has
   to clear the *worst-case* header height: at standard desktop
   widths the four nav items wrap to a second row under the title
   (because title 449px + 24px gap + nav 366px exceeds the 832px
   content area inside an 880px max-width body with 24px padding),
   which produces a ~102px header. Adding a small visible buffer on
   top of that lets the title breathe instead of sitting flush
   against the header edge. */
.section { margin-top: 36px; scroll-margin-top: 120px; }
.section:first-of-type { margin-top: 8px; }
.section__title {
  font-size: 1.375rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin: 0 0 14px;
  padding-bottom: 8px;
  border-bottom: 2px solid currentColor;
}
.section__subtitle {
  font-size: 1.0625rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  margin: 24px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--line);
}
.holding {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px 24px;
  padding: 16px 20px;
  margin-top: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--card-bg);
}
.holding__logo {
  width: 100%;
  max-width: 88px;
  max-height: 64px;
  object-fit: contain;
  justify-self: center;
}
.holding__body { min-width: 0; }
.holding__title {
  font-size: 1.0625rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  margin: 0;
  overflow-wrap: anywhere;
}
.holding__periods {
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
  /* Three columns -- start date, separator, end date -- sized to
     ``max-content``. Each card's grid widths are derived from the
     widest start and end actually present in that card's
     period(s), which makes a single open period (the common case
     for a current holding) collapse to a tight phrase
     "Aug 14, 2023 - Present" where the dash sits between two
     content-tight columns. Combined with ``text-align: end`` on
     real end dates and ``text-align: start`` on the "Present"
     placeholder (rules below), the layout achieves a single
     unified rule:
       - first date always at the left of column 1;
       - separator at a fixed x-offset within the card (col 1
         right edge + 0.5ch column-gap, identical for every period
         row in a multi-period stack);
       - second date at the right of column 3 when it's a real
         date, OR at the left of column 3 (locally symmetric with
         the start date around the dash) when it's "Present".
     Multi-period stacks within one card still align dashes
     vertically because all rows share the same grid columns; the
     ``min-content`` middle track keeps the dash at a fixed offset
     even if other end dates in the same card are wider than
     "Present". The ``max-content`` choice over a fixed em width
     gives up cross-card dash alignment in exchange for getting
     "Present" symmetric to the start date for free in the typical
     single-period current holding -- the alternative was a
     per-row CSS variable that estimated start-date pixel width
     server-side, which would have been brittle across font /
     platform variations. */
  display: grid;
  grid-template-columns: max-content min-content max-content;
  justify-content: start;
  column-gap: 0.5ch;
  row-gap: 2px;
  color: var(--muted);
  font-size: 0.875rem;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
/* ``display: contents`` lets each <li>'s three children (start
   <time>, dash <span>, end <time>/<span>) participate directly in
   the parent grid while the <li> itself stays in the DOM tree --
   so assistive tech still announces it as a list item, but it
   contributes nothing to layout. Modern browsers (Chromium,
   Firefox, Safari) preserve list semantics across this property. */
.holding__periods li { display: contents; }
/* Right-align the end-date <time>/<span> within its column 3 cell
   so the row's right edge lines up with the widest end date in
   this card. The start-date <time> in column 1 keeps the default
   ``text-align: start`` so it hugs the left edge of its cell.
   With ``max-content`` columns above, this produces:
     - closed periods: spread layout, "<start>  -  <end>" where
       both halves touch their column's outer edge (the dash sits
       at a card-fixed offset between them);
     - open periods (end is "Present"): see the ``span:last-child``
       override below -- "Present" gets ``text-align: start`` so
       its left edge tucks against the dash, mirroring the start
       date which is at the left edge of column 1.
   <time>/<span> is blockified into a grid item so it stretches to
   fill its column; ``text-align: end`` then aligns the visible
   text to the cell's right edge. */
.holding__periods li > :last-child { text-align: end; }
/* Special-case the "Present" placeholder so it ends up locally
   symmetric to the start date around the dash. The placeholder
   renders as a plain <span> (real end dates use <time>), and
   here we left-align it so it sits at the left edge of column 3
   right next to the dash. In the typical single-period current
   holding the column-3 width collapses to "Present"'s own
   ~3.5em (because there's no longer end date in the same card
   to widen the column), so the row reads as a tight phrase
   "<start> - Present" with the dash flanked by only the 0.5ch
   column-gap on each side -- perfectly symmetric. In multi-row
   cards that mix open and closed periods the column-3 width is
   driven by the widest closed end date instead, but the
   left-aligned "Present" still tucks against the dash, leaving
   the (invisible) trailing whitespace inside the cell rather
   than a visible chasm before the placeholder. The selector
   ``span:last-child`` matches the placeholder unambiguously:
   real end dates use <time>, and the dash <span> is the middle
   child, not the last. */
.holding__periods li > span:last-child { text-align: start; }
.holding__note {
  margin: 10px 0 0;
  font-size: 0.875rem;
  line-height: 1.45;
  color: var(--muted);
  max-width: 60ch;
}
.holding__stats {
  display: grid;
  grid-template-columns: max-content max-content;
  column-gap: 14px;
  row-gap: 3px;
  margin: 0;
  font-variant-numeric: tabular-nums;
  font-size: 0.9375rem;
}
/* Each stat pair is wrapped in ``<div class="holding__stat">`` so the
   mobile media query can spread TSR/CAGR/Weight as flex items across
   the full row. On desktop ``display: contents`` makes the wrapper
   transparent so dt/dd participate directly in the parent's 2-column
   grid, preserving the original right-column layout exactly. */
.holding__stat { display: contents; }
.holding__stats dt { color: var(--muted); margin: 0; font-weight: 400; }
.holding__stats dd { margin: 0; text-align: right; font-weight: 600; }
.value--positive { color: var(--positive); }
.value--negative { color: var(--negative); }
/* Intro paragraph under a section title (e.g. the methodology note
   beneath "Recent trades"). Sits on its own row, slightly muted, so
   the reader can skip it once they've internalised the rule. */
.section__intro {
  margin: -4px 0 16px;
  color: var(--muted);
  font-size: 0.9375rem;
  line-height: 1.5;
  max-width: 64ch;
}
/* Burst-aggregated trade capsule. Visually parallel to ``.holding`` so
   the page reads as a single family of cards: a logo on the left, a
   ticker/date block in the middle, and right-rail metadata (category
   badge + per-share price). The logo column is narrower than on a
   holding card (these capsules carry less per-row content and benefit
   from a tighter look) and the meta column packs the badge above the
   price in a small vertical stack. */
.trade {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px 18px;
  padding: 14px 18px;
  margin-top: 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--card-bg);
}
.trade__logo {
  width: 100%;
  max-width: 64px;
  max-height: 48px;
  object-fit: contain;
  justify-self: center;
}
.trade__body { min-width: 0; }
.trade__title {
  font-size: 1rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  margin: 0;
  overflow-wrap: anywhere;
}
.trade__period {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 0.875rem;
  font-variant-numeric: tabular-nums;
}
.trade__meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  white-space: nowrap;
}
/* Solid-pill badge so the four categories pop visually at a glance
   when scanning a long list of trades. Uppercase + letter-spacing
   gives it the "label" affordance a typographic reader expects from
   a category tag and distinguishes it cleanly from the prose around
   it. ``color: #fff`` (and the matching pure-black override in dark
   mode below) is set independently of ``--bg`` so the contrast
   against the saturated fills stays high regardless of the page's
   ambient surface luminance. */
.trade__badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #ffffff;
  /* The badge fills sit on coloured chips and overlap any
     ``background`` ancestor; making the box-shadow none keeps the
     pill flush. */
  box-shadow: none;
}
/* Four category fills collapsed onto a single BUY-vs-SELL axis:
   green marks any move INTO a position (Initiated full-on or
   Increased exposure) and red marks any move OUT (Decreased
   exposure or Divested fully). The badge label itself already
   spells out the size of the move ("Increased by 30%", "Divested"),
   so a reader scanning a long list of trades can identify
   direction at a glance from the colour and reach for the label
   only when they care about magnitude. The earlier four-colour
   diverging palette (green / blue for buys, orange / red for sells)
   read as four equally-distinct buckets and asked the reader to
   memorise which warm hue meant "partial sell" vs "full sell";
   collapsing to two semantic colours is faster to scan and the
   ``--open`` / ``--increase`` / ``--decrease`` / ``--close`` BEM
   modifiers stay in place so the markup keeps describing exactly
   what happened. */
.trade__badge--open     { background: var(--positive); }
.trade__badge--increase { background: var(--positive); }
.trade__badge--decrease { background: var(--negative); }
.trade__badge--close    { background: var(--negative); }
.trade__price {
  font-variant-numeric: tabular-nums;
  font-size: 0.9375rem;
  font-weight: 600;
}
.bars {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 12px 0 24px;
}
.bars__row {
  display: grid;
  grid-template-columns: minmax(0, 14rem) 4rem minmax(0, 1fr);
  align-items: center;
  column-gap: 14px;
}
.bars__label {
  font-size: 0.9375rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bars__value {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--muted);
  font-size: 0.9375rem;
}
.bars__track {
  background: var(--line);
  border-radius: 999px;
  height: 10px;
  overflow: hidden;
}
.bars__fill { height: 100%; border-radius: inherit; }
.bars--allocation .bars__fill { background: var(--accent-bench); }
.bars--equities .bars__fill { background: var(--accent); }
.return-chart { margin: 0 0 24px; }
.return-chart__plot { position: relative; }
.return-chart svg { width: 100%; height: auto; display: block; }
/* ``vector-effect: non-scaling-stroke`` on both the reference
   line and the data curves makes their stroke widths render in
   screen pixels rather than viewBox units. Without it, the
   1000x400 viewBox compresses strokes to ~1px wide on phone
   widths (the SVG uses ``preserveAspectRatio="none"``, so x and
   y both scale down), leaving the chart looking hair-thin and
   washed out. With it, the curves stay at a consistent 2.4px
   thickness from a wide desktop all the way down to a 320px
   phone, and the baseline at 1.75px keeps a clear hierarchy
   beneath the data lines. */
.return-chart__ref { stroke: var(--muted); stroke-width: 1.75; stroke-dasharray: 4 6; opacity: 0.7; vector-effect: non-scaling-stroke; }
.return-chart__line { fill: none; stroke-width: 2.4; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.return-chart__line--jg { stroke: var(--accent); }
.return-chart__line--bench { stroke: var(--accent-bench); }
/* The delta overlay is a full-bleed positioning canvas; the bar and
   label inside it use CSS variables so their position is decoupled
   from the overlay's dimensions and stays glued to the chart-end
   x-coordinate (always 12% from the right) on every viewport. */
.return-chart__delta {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
/* The bracket is styled like a measurement caliper: a vertical
   spine running between the two curve endpoints, with short
   horizontal "jaws" (the ``::before`` / ``::after`` pseudos) at
   the top and bottom that visibly hook onto each curve's last
   point. The whole annotation picks up the directional
   ``--delta-color`` set inline by the renderer -- green when JG
   outperformed the benchmark, red when it didn't -- so the
   bracket itself carries the verdict and the reader doesn't
   have to glance at the label to know which way the wind is
   blowing. ``var(--muted)`` is the safety fallback if the
   inline custom property is missing for any reason. */
.return-chart__delta-bar {
  position: absolute;
  left: 88%;
  top: var(--top);
  height: var(--height);
  width: 0;
  border-left: 1.75px solid var(--delta-color, var(--muted));
}
.return-chart__delta-bar::before,
.return-chart__delta-bar::after {
  content: "";
  position: absolute;
  left: -8px;
  width: 8px;
  height: 1.75px;
  background: var(--delta-color, var(--muted));
}
.return-chart__delta-bar::before { top: 0; transform: translateY(-50%); }
.return-chart__delta-bar::after { bottom: 0; transform: translateY(50%); }
.return-chart__delta-label {
  position: absolute;
  left: 88%;
  margin-left: 8px;
  top: calc(var(--top) + var(--height) / 2);
  transform: translateY(-50%);
  font-size: 0.875rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.return-chart__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  font-size: 0.875rem;
  color: var(--muted);
  margin-top: 8px;
}
.return-chart__swatch {
  display: inline-block;
  width: 14px;
  height: 4px;
  vertical-align: middle;
  margin-right: 6px;
  border-radius: 2px;
}
.return-chart__caption { color: var(--muted); font-size: 0.875rem; margin-top: 4px; }
/* Pointer-driven scrubber. The hover overlay paints on top of the
   SVG curves + delta annotation; ``pointer-events: none`` on every
   piece inside ``.return-chart__hover`` lets the plot capture
   pointermove for the entire chart while the guide/markers/tooltip
   ride along. The plot itself owns ``touch-action: pan-y`` so a
   finger can still scroll the page vertically through the chart;
   horizontal swipes are reserved for scrubbing. */
.return-chart__plot { touch-action: pan-y; }
.return-chart__hover {
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s ease;
  z-index: 2;
}
.return-chart__hover.is-active { opacity: 1; }
.return-chart__guide {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  border-left: 1px solid var(--muted);
  pointer-events: none;
}
.return-chart__marker {
  position: absolute;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--bg);
  transform: translate(-50%, -50%);
  pointer-events: none;
  border: 2px solid currentColor;
}
.return-chart__marker--jg { color: var(--accent); }
.return-chart__marker--bench { color: var(--accent-bench); }
.return-chart__tooltip {
  position: absolute;
  top: 8px;
  background: var(--card-bg);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 0.8125rem;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.08);
  pointer-events: none;
  white-space: nowrap;
  color: var(--fg);
  /* Hidden until the script positions it; ``visibility`` keeps the
     tooltip out of the paint pipeline while the chart is idle but
     leaves its dimensions queryable if we ever want to measure them
     later. The parent ``.is-active`` toggle is what reveals it. */
}
.return-chart__hover.is-active .return-chart__tooltip { visibility: visible; }
.return-chart__hover:not(.is-active) .return-chart__tooltip { visibility: hidden; }
.return-chart__tooltip-date {
  color: var(--muted);
  font-size: 0.75rem;
  margin-bottom: 4px;
}
.return-chart__tooltip-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.return-chart__tooltip-row + .return-chart__tooltip-row { margin-top: 2px; }
.return-chart__tooltip-swatch {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.return-chart__tooltip-swatch--jg { background: var(--accent); }
.return-chart__tooltip-swatch--bench { background: var(--accent-bench); }
.return-chart__tooltip-label { color: var(--muted); }
.return-chart__tooltip-value { font-weight: 600; margin-left: auto; }
.returns-compare {
  margin: 0;
  padding: 18px 20px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--card-bg);
}
.returns-compare__period {
  margin: 0 0 14px;
  color: var(--muted);
  font-size: 0.875rem;
  font-variant-numeric: tabular-nums;
}
.returns-compare__period-meta { opacity: 0.85; }
.returns-compare__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(0, 1fr));
  align-items: start;
  column-gap: 28px;
  row-gap: 16px;
}
/* Each column lays its identity (logo + short/long name) on the left
   and its metrics (TWR/TSR/CAGR) on the right, vertically centred so
   the name sits at the same height as the numbers. */
.returns-compare__col {
  display: grid;
  grid-template-columns: minmax(0, auto) minmax(0, 1fr);
  align-items: center;
  column-gap: 18px;
  min-width: 0;
}
.returns-compare__col + .returns-compare__col {
  /* Centre the vertical divider in the 28px ``column-gap`` so it
     gets equal breathing room on both sides. With ``padding-left``
     and ``margin-left`` matching at half-gap (14px each), the box
     shifts left into the gap by 14px, the border lands smack in
     the middle, and the inner padding restores the content to its
     natural track position so column alignment is preserved.
     Setting them equal to the FULL gap (the previous geometry)
     pulled the box flush against column 1, leaving the divider
     touching JG's TWR/CAGR values with all the whitespace falling
     on the benchmark side -- a visibly lopsided look. */
  padding-left: 14px;
  border-left: 1px solid var(--line);
  margin-left: -14px;
}
.returns-compare__name {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 1.0625rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  margin: 0;
  min-width: 0;
}
.returns-compare__logo {
  width: 32px;
  height: 32px;
  object-fit: contain;
  flex: 0 0 auto;
}
.returns-compare__name-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  min-width: 0;
  overflow-wrap: anywhere;
}
.returns-compare__name-sub {
  font-size: 0.75rem;
  font-weight: 400;
  color: var(--muted);
  letter-spacing: 0;
  margin-top: 2px;
}
.returns-compare__stats {
  display: grid;
  grid-template-columns: max-content 1fr;
  column-gap: 16px;
  row-gap: 4px;
  margin: 0;
  font-variant-numeric: tabular-nums;
  justify-self: end;
  min-width: 0;
}
.returns-compare__stats dt { color: var(--muted); margin: 0; font-weight: 400; font-size: 0.9375rem; }
.returns-compare__stats dd { margin: 0; text-align: right; font-weight: 700; font-size: 1.0625rem; }
.returns-compare__delta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  column-gap: 8px;
  row-gap: 4px;
  margin: 16px 0 0;
  padding-top: 12px;
  border-top: 1px solid var(--line);
  font-size: 0.9375rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
/* Each token (prefix / metric / separator) stays on a single line so
   "+6.7 pp Total Return" never breaks mid-phrase. The flex parent
   above lets the tokens themselves wrap as whole units. */
.returns-compare__delta-prefix,
.returns-compare__delta-metric,
.returns-compare__delta-sep { white-space: nowrap; }
.returns-compare__delta strong,
.returns-compare__delta .value--positive,
.returns-compare__delta .value--negative { font-weight: 700; }
footer {
  color: var(--muted);
  font-size: 0.875rem;
  margin-top: 40px;
  padding: 24px 0 0;
  border-top: 1px solid var(--line);
  line-height: 1.6;
}
footer a { color: var(--accent-bench); }
.footer__notes {
  margin: 0 0 16px;
  padding-left: 22px;
}
.footer__notes li { margin-bottom: 6px; }
.footer__notes li:last-child { margin-bottom: 0; }
.footer__disclaimer,
.footer__legal { margin: 0 0 12px; }
.footer__updated {
  margin: 16px 0 0;
  font-size: 0.8125rem;
  opacity: 0.85;
}
/* Stack the JG/benchmark comparison columns earlier than the rest of
   the mobile layout: with two columns inside the capsule, the
   logo+name+subtitle on the left and TSR/CAGR stats on the right run
   out of horizontal headroom around 600-680px viewport widths and
   start visually overlapping. Switching to a single-column stack
   here keeps each side fully legible until the global mobile layout
   kicks in below 540px. */
@media (max-width: 680px) {
  .returns-compare__grid { grid-template-columns: 1fr; row-gap: 14px; }
  .returns-compare__col + .returns-compare__col {
    padding-left: 0;
    margin-left: 0;
    padding-top: 14px;
    border-left: none;
    border-top: 1px solid var(--line);
  }
}
@media (max-width: 540px) {
  body { padding: 20px 16px 32px; }
  .site-header {
    margin: -20px -16px 24px;
    padding: 10px 16px;
    gap: 6px 16px;
  }
  .site-title { font-size: 1.375rem; }
  /* The desktop nav uses 12px horizontal padding and 4px gap, which
     pushes the four current section labels (Performance / Current /
     Historical / Trades) past the content width on common iPhone
     viewports (375px and 390px both wrap "Trades" to a second row,
     and a wrapped nav inflates header height past the
     ``scroll-margin-top`` reserve below, hiding the section title
     when the user taps an anchor). Compact the padding and gap on
     mobile so all four pills fit in a single row down to ~360px
     content width (iPhone SE 2/3, iPhone 12 mini, iPhone 12/13/14
     std). Slightly smaller type also pulls each label in by ~5%
     which gives us the headroom we need without crossing the
     accessible-text floor. Below ~360px (legacy iPhone SE 1st
     gen / Android compact) the nav will still wrap, but the
     fallback degrades gracefully and the scroll-margin reserve
     below still keeps the title visible. */
  .site-nav { width: 100%; gap: 2px; font-size: 0.875rem; }
  .site-nav a { padding: 4px 8px; }
  .ticker {
    margin: 0 -16px 20px;
    padding: 12px 0;
  }
  .ticker__track { gap: 28px; animation-duration: 45s; }
  .ticker__logo { width: 48px; height: 24px; }
  /* With the compacted mobile nav above, the header collapses to a
     single row at iPhone widths (~81px tall). 100px of reserve
     gives us a comfortable ~18px buffer between header bottom and
     section title top. Matches the desktop buffer above so the
     anchor-jump feel is consistent across viewports. */
  .section { margin-top: 28px; scroll-margin-top: 100px; }
  .section__title { font-size: 1.1875rem; margin-bottom: 12px; }
  .section__subtitle { font-size: 1rem; margin: 22px 0 10px; }
  .return-chart { margin-bottom: 16px; }
  /* The chart SVG is authored with a 1000x400 viewBox (a wide
     2.5:1 aspect ratio that suits desktop). On phone widths
     ``height: auto`` + ``preserveAspectRatio="none"`` would
     collapse the chart to ~140-160px tall, where the curves
     bunch up into a near-flat strip and the +/- pp delta is
     hard to read. Override the aspect ratio so the chart gains
     meaningful vertical room (~225-240px tall at typical
     iPhone widths) and the JG vs benchmark divergence reads at
     a glance again. ``preserveAspectRatio="none"`` already
     allows the SVG to stretch to whatever box CSS hands it. */
  .return-chart svg { aspect-ratio: 5 / 3; height: auto; }
  .holding {
    grid-template-columns: 56px minmax(0, 1fr);
    grid-template-areas:
      "logo body"
      "stats stats";
    align-items: start;
    gap: 12px 14px;
    padding: 14px;
  }
  .holding__logo {
    grid-area: logo;
    max-width: 56px;
    max-height: 56px;
    align-self: center;
  }
  .holding__body { grid-area: body; }
  .holding__title { font-size: 1rem; }
  .holding__note { margin-top: 8px; }
  .holding__stats {
    /* On mobile the stats row spans the full width below the
       logo+body. Lay it out as a fixed 3-column grid -- TSR in
       the left third, CAGR in the middle, Weight (when present)
       in the right third -- so vertical scanning down the
       holdings list always finds the same metric in the same
       column. The earlier ``flex / space-between`` distribution
       achieved that for *current* holdings (which have all three
       stats), but historical holdings only carry TSR + CAGR and
       ``space-between`` would have pushed their CAGR all the way
       to the right edge -- aligning it with the Weight column of
       current rows above and below, which is misleading. The
       three-track grid keeps CAGR centered for both shapes. */
    grid-area: stats;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    column-gap: 14px;
    row-gap: 6px;
    padding-top: 4px;
    border-top: 1px solid var(--line);
  }
  .holding__stat {
    /* Each wrapper becomes a real flex container on mobile so its
       label and value sit tight together with a small gap, while
       the parent grid decides which column the whole pair lands
       in. */
    display: flex;
    gap: 6px;
    align-items: baseline;
  }
  /* TSR (1st child) keeps the default ``justify-self: start`` and
     hugs the left edge of column 1; CAGR (2nd) sits centered
     within column 2 so it aligns across current and historical
     rows; Weight (3rd, current rows only) hugs the right edge of
     column 3. Historical rows simply leave column 3 empty. */
  .holding__stat:nth-child(2) { justify-self: center; }
  .holding__stat:nth-child(3) { justify-self: end; }
  /* Trade capsule stacks the right-rail meta (badge + price)
     into its own row underneath the logo+body block. Same
     layout idea as ``.holding`` on mobile, scaled for the
     trade card's denser content. The meta row uses
     ``justify-content: space-between`` so the badge hugs the
     left (under the logo column visually) and the price hugs
     the right -- mirroring how a reader scans the desktop row
     left-to-right. */
  .trade {
    grid-template-columns: 44px minmax(0, 1fr);
    grid-template-areas:
      "logo body"
      "meta meta";
    align-items: start;
    gap: 10px 12px;
    padding: 12px;
  }
  .trade__logo {
    grid-area: logo;
    max-width: 44px;
    max-height: 44px;
    align-self: center;
  }
  .trade__body { grid-area: body; }
  .trade__title { font-size: 0.9375rem; }
  .trade__meta {
    grid-area: meta;
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    padding-top: 6px;
    border-top: 1px solid var(--line);
  }
  .returns-compare { padding: 14px 16px; }
  .returns-compare__col { column-gap: 14px; }
  .returns-compare__name { font-size: 1rem; gap: 10px; }
  .returns-compare__logo { width: 28px; height: 28px; }
  /* Phones don't have horizontal headroom for the label to flow to
     the right of the bar without overflowing the chart figure. Move
     it just LEFT of the bar with a translucent backdrop so it stays
     legible over the curve endpoints while the bar continues to mark
     the actual chart-end x-coordinate. */
  .return-chart__delta-label {
    left: auto;
    margin-left: 0;
    right: calc(12% + 6px);
    background: color-mix(in srgb, var(--card-bg) 88%, transparent);
    padding: 1px 5px;
    border-radius: 4px;
    -webkit-backdrop-filter: blur(2px);
    backdrop-filter: blur(2px);
    font-size: 0.8125rem;
  }
  .bars { margin-bottom: 20px; }
  .bars__row {
    grid-template-columns: minmax(0, 1fr) max-content;
    grid-template-areas:
      "label value"
      "bar bar";
    column-gap: 12px;
    row-gap: 4px;
  }
  .bars__label {
    grid-area: label;
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
  }
  .bars__value { grid-area: value; }
  .bars__track { grid-area: bar; }
  footer { font-size: 0.8125rem; margin-top: 32px; padding-top: 20px; }
}
/* Below ~540px the JG vs benchmark delta line "JG vs S&P 500: +6.7
   pp Total Return \u00b7 +1.3 pp CAGR" no longer comfortably fits on
   a single row (the "Total Return" wording is wider than the older
   "TR" abbreviation, so we stack a bit earlier than we used to).
   Force each piece onto its own line and hide the now-redundant dot
   separator. */
@media (max-width: 540px) {
  .returns-compare__delta-prefix,
  .returns-compare__delta-metric { flex: 1 0 100%; }
  .returns-compare__delta-sep { display: none; }
}
@media (max-width: 380px) {
  body { padding: 16px 12px 28px; }
  .site-header {
    margin: -16px -12px 20px;
    padding: 10px 12px;
  }
  .site-title { font-size: 1.25rem; }
  .ticker { margin: 0 -12px 16px; padding: 10px 0; }
  .ticker__track { gap: 22px; animation-duration: 35s; }
  .ticker__logo { width: 40px; height: 20px; }
  .holding {
    grid-template-columns: 44px minmax(0, 1fr);
    gap: 10px 12px;
    padding: 12px;
  }
  .holding__logo { max-width: 44px; max-height: 44px; }
  .holding__title { font-size: 0.9375rem; }
  .holding__stats { font-size: 0.875rem; gap: 6px 10px; }
}
""".strip()


# Tiny inline script that strips the URL hash the moment the user
# takes manual control of scrolling. The ``Performance`` / ``Current``
# / ``Historical`` nav links in the sticky header are plain in-page
# anchors -- clicking ``Current`` appends ``#current`` to the URL and
# the browser scrolls to that section. Without this script the hash
# sticks around even after the user wheels elsewhere on the page, so
# a subsequent refresh makes the browser re-jump to the section they
# last clicked on instead of restoring their actual scroll position
# -- which on a long holdings page reads as the page "scrolling down
# uncontrollably" on every refresh.
#
# We only react to user-initiated input events (``wheel``,
# ``touchmove``, and the keys that scroll the page). That way the
# initial smooth-scroll triggered by a nav click does NOT clear the
# hash -- the hash stays in the URL while the user is "at" the
# section they navigated to (so the link is still shareable), and
# only gets dropped the instant the user starts exploring on their
# own. Listeners are passive so they never block scrolling.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_HASH_CLEAR_SCRIPT = (
    "(function(){"
    "function clearHash(){"
    "if(!location.hash)return;"
    "history.replaceState(null,'',location.pathname+location.search);"
    "}"
    "var opts={passive:true};"
    "addEventListener('wheel',clearHash,opts);"
    "addEventListener('touchmove',clearHash,opts);"
    "addEventListener('keydown',function(e){"
    "var k=e.key;"
    "if(k==='ArrowDown'||k==='ArrowUp'||k==='PageDown'||k==='PageUp'"
    "||k==='Home'||k==='End'||k===' '||k==='Spacebar')clearHash();"
    "},opts);"
    "})();"
)


# Custom smooth-scroll for the sticky-nav links. Native CSS
# ``scroll-behavior: smooth`` is fast and abrupt, and on iOS Safari
# the sticky header's ``backdrop-filter`` re-composites mid-scroll
# which reads as a brief "blink" right after the tap. Driving the
# scroll from JS lets us:
#
#   * use a slower, cubic-eased animation that genuinely "slides"
#     between sections instead of snapping;
#   * cancel ``preventDefault()`` the anchor click so the browser
#     never performs the instant-jump that fights our animation;
#   * call ``window.scrollTo`` programmatically (which does NOT
#     fire wheel/touchmove), so the animation runs uninterrupted
#     while the existing ``_HASH_CLEAR_SCRIPT`` happily stays put;
#   * still write the section anchor into the URL via
#     ``history.pushState`` so the link is shareable, matching
#     pre-existing behaviour.
#
# Scoped to ``.site-nav a[href^='#']`` only -- the skip link and any
# other in-page anchors keep their default (instant) behaviour, which
# is what assistive-tech users expect from a skip link. Honours
# ``prefers-reduced-motion`` by jumping directly to the target.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
# ``slide`` re-reads ``targetY(el)`` on every frame instead of locking
# the destination in at click time. iOS Safari's URL bar collapses
# mid-animation (extending the visual viewport) and lazy-loaded logos
# in the sections above the target can finish painting while we're
# in flight, both of which nudge the target section's document
# position by a handful of pixels. With a static target the original
# version landed slightly above the section title on the *first*
# tap from the top of the page (most visible on the bottom-most
# ``Trades`` link, which has the longest distance to travel);
# subsequent taps worked because the URL bar was already collapsed
# and logos already laid out. Tracking a moving target makes the
# slide self-correcting, and a final ``scrollTo(targetY(el))``
# guarantees we settle exactly on the section's current position
# even if the last layout shift happened after the easing reached 1.
_NAV_SCROLL_SCRIPT = (
    "(function(){"
    "var rm=false;"
    "try{rm=matchMedia('(prefers-reduced-motion: reduce)').matches;}"
    "catch(e){}"
    "function ease(t){"
    "return t<0.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;"
    "}"
    "function sy(){"
    "return window.pageYOffset||document.documentElement.scrollTop||0;"
    "}"
    "var raf=null;"
    "function targetY(el){"
    "var r=el.getBoundingClientRect(),top=r.top+sy(),smt=0;"
    "try{smt=parseInt(getComputedStyle(el).scrollMarginTop,10)||0;}"
    "catch(e){}"
    "return Math.max(0,top-smt);"
    "}"
    "function slide(el,d){"
    "if(raf!==null)cancelAnimationFrame(raf);"
    "var sy0=sy(),t0=null;"
    "function step(ts){"
    "if(t0===null)t0=ts;"
    "var t=Math.min(1,(ts-t0)/d);"
    "var ty=targetY(el);"
    "window.scrollTo(0,sy0+(ty-sy0)*ease(t));"
    "if(t<1){raf=requestAnimationFrame(step);}"
    "else{window.scrollTo(0,targetY(el));raf=null;}"
    "}"
    "raf=requestAnimationFrame(step);"
    "}"
    "document.addEventListener('click',function(e){"
    "if(e.defaultPrevented)return;"
    "if(e.button!==0)return;"
    "if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;"
    "var t=e.target;"
    "if(!t||!t.closest)return;"
    "var a=t.closest('.site-nav a[href^=\"#\"]');"
    "if(!a)return;"
    "var href=a.getAttribute('href');"
    "if(!href||href==='#')return;"
    "var el=document.getElementById(href.slice(1));"
    "if(!el)return;"
    "e.preventDefault();"
    "var ty=targetY(el);"
    "if(rm){window.scrollTo(0,ty);}"
    "else{"
    "var dist=Math.abs(ty-sy());"
    "var dur=Math.min(900,Math.max(450,dist*0.45));"
    "slide(el,dur);"
    "}"
    "try{history.pushState(null,'',href);}catch(err){}"
    "});"
    "})();"
)


# Pointer-driven scrubber for the return chart. A finger or cursor
# dragged across the plot reveals the date and per-series total
# return at that x-coordinate via a vertical guide line, a marker
# dot riding each curve, and a small tooltip card with the values.
#
# The script is intentionally data-agnostic: every figure that wants
# the interaction declares ``data-chart='{...}'`` on its
# ``.return-chart`` element, with ``start`` (ISO date), ``totalDays``
# (integer span), ``rightPct`` (the chart's right margin reserved
# for the delta annotation), ``yMin``/``yMax`` (the SVG y-domain),
# and one entry per series in ``series`` with ``kind`` (``jg`` or
# ``bench``), ``label``, ``x`` (day offsets from ``start``), and
# ``y`` (return multiples). Keeping the data in the DOM rather than
# baking it into the script means the script's payload is identical
# for every page render -- and so its SHA-256 is stable and can be
# pinned in CSP without re-hashing on every update.
#
# Linear interpolation between adjacent (x, y) samples gives the
# tooltip its values: the visual curve uses a Pchip spline, but
# linear is a faithful enough approximation between dense samples
# (we hover with sub-pixel precision; the difference is invisible
# to the eye), and it keeps the script small and dependency-free.
#
# ``touch-action: pan-y`` on the plot (set in CSS) allows vertical
# page scrolling to start from a touch on the chart while horizontal
# motion is captured for scrubbing. ``pointer*`` events unify mouse
# and touch handling.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_RETURN_CHART_SCRIPT = (
    "(function(){"
    "var M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];"
    "function fmtDate(ts){"
    "var d=new Date(ts);"
    "return M[d.getUTCMonth()]+' '+d.getUTCDate()+', '+d.getUTCFullYear();"
    "}"
    "function fmtPct(v){"
    "var p=(v-1)*100,a=Math.abs(p),s=p>=0?'+':'';"
    "var n=Math.round(a*10)/10>=100?p.toFixed(0):p.toFixed(1);"
    "return s+n+'%';"
    "}"
    "function lerp(x0,y0,x1,y1,x){"
    "if(x1===x0)return y0;"
    "return y0+(y1-y0)*(x-x0)/(x1-x0);"
    "}"
    "function valueAt(s,d){"
    "var x=s.x,y=s.y,n=x.length;"
    "if(n===0)return null;"
    "if(d<=x[0])return y[0];"
    "if(d>=x[n-1])return y[n-1];"
    "var lo=0,hi=n-1;"
    "while(lo+1<hi){var m=(lo+hi)>>1;if(x[m]<=d)lo=m;else hi=m;}"
    "return lerp(x[lo],y[lo],x[hi],y[hi],d);"
    "}"
    "function init(fig){"
    "var raw=fig.getAttribute('data-chart');"
    "if(!raw)return;"
    "var data;try{data=JSON.parse(raw);}catch(e){return;}"
    "var plot=fig.querySelector('.return-chart__plot');"
    "if(!plot)return;"
    "var hover=plot.querySelector('.return-chart__hover');"
    "if(!hover)return;"
    "var guide=hover.querySelector('.return-chart__guide');"
    "var tip=hover.querySelector('.return-chart__tooltip');"
    "var dateEl=hover.querySelector('.return-chart__tooltip-date');"
    "var rowsEl=hover.querySelector('.return-chart__tooltip-rows');"
    "var startMs=Date.parse(data.start+'T00:00:00Z');"
    "var totalDays=data.totalDays||0;"
    "var rightPct=data.rightPct||0;"
    "var yMin=data.yMin,yMax=data.yMax;"
    "var W=1000,H=400;"
    "var ySpan=(yMax-yMin)||1;"
    "var chartXEnd=W*(1-rightPct/100);"
    "rowsEl.innerHTML='';"
    "data.series.forEach(function(s){"
    "var row=document.createElement('div');"
    "row.className='return-chart__tooltip-row';"
    "var sw=document.createElement('span');"
    "sw.className='return-chart__tooltip-swatch return-chart__tooltip-swatch--'+s.kind;"
    "var lbl=document.createElement('span');"
    "lbl.className='return-chart__tooltip-label';"
    "lbl.textContent=s.label;"
    "var val=document.createElement('span');"
    "val.className='return-chart__tooltip-value';"
    "row.appendChild(sw);row.appendChild(lbl);row.appendChild(val);"
    "rowsEl.appendChild(row);"
    "s._val=val;"
    "var mk=document.createElement('div');"
    "mk.className='return-chart__marker return-chart__marker--'+s.kind;"
    "hover.appendChild(mk);"
    "s._mk=mk;"
    "});"
    "function show(){hover.classList.add('is-active');}"
    "function hide(){hover.classList.remove('is-active');}"
    "function update(clientX){"
    "var r=plot.getBoundingClientRect();"
    "if(r.width<=0)return;"
    "var usableW=r.width*(1-rightPct/100);"
    "var px=Math.max(0,Math.min(usableW,clientX-r.left));"
    "var frac=usableW>0?px/usableW:0;"
    "var days=frac*totalDays;"
    "var ts=startMs+days*86400000;"
    "dateEl.textContent=fmtDate(ts);"
    "data.series.forEach(function(s){"
    "var v=valueAt(s,days);"
    "s._val.textContent=fmtPct(v);"
    "var svgX=totalDays>0?(days/totalDays)*chartXEnd:0;"
    "var svgY=H-(v-yMin)/ySpan*H;"
    "s._mk.style.left=(svgX/W*100)+'%';"
    "s._mk.style.top=(svgY/H*100)+'%';"
    "});"
    "guide.style.left=(px/r.width*100)+'%';"
    "var tipFrac=px/r.width;"
    "if(tipFrac>0.55){"
    "tip.style.left='auto';"
    "tip.style.right=((1-tipFrac)*100)+'%';"
    "tip.style.transform='translateX(-12px)';"
    "}else{"
    "tip.style.right='auto';"
    "tip.style.left=(tipFrac*100)+'%';"
    "tip.style.transform='translateX(12px)';"
    "}"
    "}"
    "plot.addEventListener('pointerenter',function(e){if(e.pointerType==='mouse'){update(e.clientX);show();}});"
    "plot.addEventListener('pointermove',function(e){update(e.clientX);show();});"
    "plot.addEventListener('pointerdown',function(e){"
    "update(e.clientX);show();"
    "try{plot.setPointerCapture(e.pointerId);}catch(err){}"
    "});"
    "plot.addEventListener('pointerup',function(e){"
    "try{plot.releasePointerCapture(e.pointerId);}catch(err){}"
    "if(e.pointerType!=='mouse')hide();"
    "});"
    "plot.addEventListener('pointerleave',function(){hide();});"
    "plot.addEventListener('pointercancel',function(){hide();});"
    "}"
    "function boot(){"
    "var figs=document.querySelectorAll('.return-chart[data-chart]');"
    "for(var i=0;i<figs.length;i++)init(figs[i]);"
    "}"
    "if(document.readyState==='loading'){"
    "document.addEventListener('DOMContentLoaded',boot);"
    "}else{boot();}"
    "})();"
)


class Webpage:
    """Builds the JG Investing index page as a single responsive document."""

    def __init__(self):
        self.return_html: str = ""
        self.current: list[str] = []
        self.historical: list[str] = []
        self.allocation_pct: dict[str, float] | None = None
        self.top_10: dict[str, float] | None = None
        # Pre-rendered HTML for each row in the "Recent trades"
        # section, in newest-first order. Populated by
        # ``add_trades``; an empty list omits the whole section
        # (and its nav link) cleanly.
        self.trades: list[str] = []
        # Logo URLs are looked up via HTTP HEAD; cache them so the
        # ticker and the holding card don't probe the same ticker
        # twice.
        self._logo_cache: dict[str, str] = {}
        # ``(ticker, name, logo_url)`` tuples for current holdings, in
        # the order they were added. Drives the marquee ticker.
        self._current_logos: list[tuple[str, str, str]] = []
        # Stashed for OG image generation in ``save()``.
        self._total_return: dict | None = None
        self._benchmarks: list | None = None

    # ------------------------------------------------------------------ API

    def add_return(self, total_return, benchmarks):
        self._total_return = total_return
        self._benchmarks = benchmarks
        self.return_html = self._build_return_section(total_return, benchmarks)

    def add_holding(self, holding):
        if holding["is_current"]:
            self._current_logos.append((
                holding["ticker"],
                holding["name"],
                self._get_logo_url(holding["ticker"]),
            ))
        card = self._build_holding_card(holding)
        bucket = self.current if holding["is_current"] else self.historical
        bucket.append(card)

    def add_allocations(self, allocation_pct, top_10):
        self.allocation_pct = allocation_pct
        self.top_10 = top_10

    def add_trades(self, trade_events):
        """Render each burst-aggregated trade event into a card.

        ``trade_events`` is the newest-first list produced by
        ``get_holdings`` (or by ``Holding.trade_events`` directly in
        the preview/test paths). Cards are stored pre-rendered so the
        page assembly in ``save()`` stays linear."""
        self.trades = [
            self._build_trade_card(event) for event in trade_events
        ]

    def save(self):
        now = datetime.now()
        # Reuse the shared ``_fmt_date`` helper so the footer's
        # "last updated" label and every other human-facing date on
        # the page agree on a single formatting convention.
        update_date = _fmt_date(now)
        update_iso = now.strftime("%Y-%m-%d")
        # Best-effort: generate the OG image first so its filename can
        # be referenced from <head>. If Pillow / fonts aren't available
        # the page still renders, just without a fresh social preview.
        self._render_og_image()
        parts: list[str] = []
        parts.append('<!DOCTYPE html>')
        parts.append('<html lang="en">')
        parts.append(self._head())
        parts.append('<body>')
        # Skip link: visually hidden until focused, lets keyboard users
        # bypass the sticky nav and jump straight to <main>.
        parts.append(
            '<a class="skip-link" href="#main-content">Skip to content</a>'
        )
        parts.append(self._build_site_header())
        parts.append('<main id="main-content" tabindex="-1">')

        ticker = self._build_ticker()
        if ticker:
            parts.append(ticker)

        parts.append('<section id="performance" class="section section--return">')
        parts.append('<h2 class="section__title">All-time performance</h2>')
        parts.append(self.return_html or '<p>No data yet.</p>')
        parts.append('</section>')

        if self.current:
            parts.append('<section id="current" class="section section--current">')
            parts.append('<h2 class="section__title">Current holdings</h2>')
            if self.allocation_pct:
                parts.append('<h3 class="section__subtitle">Asset allocation</h3>')
                parts.append(self._render_bars(list(self.allocation_pct.items()), "allocation"))
            parts.append('<h3 class="section__subtitle">Equities</h3>')
            if self.top_10:
                parts.append(self._render_bars(
                    list(self.top_10.items()), "equities", scale_to_max=True
                ))
            parts.append('\n'.join(self.current))
            parts.append('</section>')

        if self.historical:
            parts.append('<section id="historical" class="section section--historical">')
            parts.append('<h2 class="section__title">Historical holdings</h2>')
            parts.append('\n'.join(self.historical))
            parts.append('</section>')

        if self.trades:
            parts.append('<section id="trades" class="section section--trades">')
            parts.append('<h2 class="section__title">Recent trades</h2>')
            # Subtitle states the two methodology details the reader
            # would otherwise have to infer from the data: how far
            # back the section reaches, and what "combined" rows
            # represent. The "rolling quarter" wording matches the
            # long-term-investor framing of the page (a fund-letter
            # cadence rather than a high-frequency trade log) and is
            # the natural human reading of the 90-day numerical
            # ``TRADE_WINDOW_DAYS`` constant. The horizon phrase
            # collapses the ``N == 1`` case to a bare "Last year"
            # because "Last 1 years" reads as a string-formatting bug.
            # Keeping the subtitle short avoids cluttering the cards
            # themselves.
            if TRADES_YEARS_BACK == 1:
                horizon = "Last year."
            else:
                horizon = f"Last {TRADES_YEARS_BACK} years."
            parts.append(
                '<p class="section__intro">'
                f'{horizon} Trades within a '
                'rolling quarter are combined into a single entry at '
                'their volume-weighted average per-share price.'
                '</p>'
            )
            parts.append('\n'.join(self.trades))
            parts.append('</section>')

        parts.append('</main>')
        parts.append(self._footer(update_date, update_iso))
        parts.append(
            "<!-- Cloudflare Web Analytics -->"
            "<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
            "data-cf-beacon='{\"token\": \"8f450af27c86439fb0e9ab0031c76d6e\"}'></script>"
            "<!-- End Cloudflare Web Analytics -->"
        )
        parts.append('</body>')
        parts.append('</html>')

        with open("index.html", "w") as f:
            f.write("\n".join(parts))
        self._write_sitemap()
        self._write_robots_txt()

    def _write_sitemap(self) -> None:
        """Emit a single-URL ``sitemap.xml`` next to ``index.html``.

        Search engines use ``<lastmod>`` as a hint to recrawl pages
        whose content has changed; bumping it on every regeneration
        means new holdings/returns surface in indexes faster than they
        otherwise would on a static GitHub Pages site."""
        last_mod = datetime.now().strftime("%Y-%m-%d")
        url = html.escape(self.SITE_URL)
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '  <url>\n'
            f'    <loc>{url}</loc>\n'
            f'    <lastmod>{last_mod}</lastmod>\n'
            '    <changefreq>daily</changefreq>\n'
            '    <priority>1.0</priority>\n'
            '  </url>\n'
            '</urlset>\n'
        )
        with open("sitemap.xml", "w") as f:
            f.write(sitemap)

    def _write_robots_txt(self) -> None:
        """Emit ``robots.txt`` alongside ``index.html``.

        Generating this at build time (rather than committing a static
        file) keeps the canonical URL and sitemap location in sync
        with ``Webpage.SITE_URL`` -- a single source of truth -- and
        lines up with how ``index.html``, ``sitemap.xml`` and
        ``og-image.png`` are also produced."""
        sitemap_url = f"{self.SITE_URL.rstrip('/')}/sitemap.xml"
        body = (
            "# Allow all well-behaved crawlers to index everything.\n"
            "User-agent: *\n"
            "Allow: /\n"
            "\n"
            f"Sitemap: {sitemap_url}\n"
        )
        with open("robots.txt", "w") as f:
            f.write(body)

    # ----------------------------------------------------------- internals

    # Page title + nav links rendered above ``<main>``. Nav is built
    # dynamically so we never produce dead anchors when a section is
    # absent (e.g. an account with no historical positions yet).
    SITE_TITLE = "Jan Grzybek Investment Portfolio"
    # Used in <title>, OG/Twitter title, and JSON-LD. Keep it short so
    # search engines render it without truncation in SERPs (~60 chars).
    SEO_TITLE = "Jan Grzybek - Investment Portfolio"
    SITE_URL = "https://jan-grzybek.github.io/investing/"
    # ~155 chars: long enough to surface keywords, short enough that
    # search engines won't truncate the snippet on result pages.
    SITE_DESCRIPTION = (
        "Personal investment portfolio of Jan Grzybek: time-weighted "
        "return (TWR) vs the S&P 500, current asset allocation, equity "
        "holdings, and historical positions with TSR/CAGR."
    )
    # The OG image is regenerated on every ``save()`` with the latest
    # numbers baked in. Cache-busting on the social-platform side
    # happens via the ``og:updated_time`` header below.
    SOCIAL_IMAGE = "https://jan-grzybek.github.io/investing/og-image.png"
    _NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
        ("performance", "Performance", "return_html"),
        ("current", "Current", "current"),
        ("historical", "Historical", "historical"),
        ("trades", "Trades", "trades"),
    )

    def _build_site_header(self) -> str:
        links = []
        for anchor, label, attr in self._NAV_ITEMS:
            value = getattr(self, attr)
            if value:
                links.append(f'<a href="#{anchor}">{html.escape(label)}</a>')
        nav_html = (
            f'<nav class="site-nav" aria-label="Page sections">{"".join(links)}</nav>'
            if len(links) > 1 else ""
        )
        return (
            '<header class="site-header">'
            f'<h1 class="site-title">{html.escape(self.SITE_TITLE)}</h1>'
            f'{nav_html}'
            '</header>'
        )

    def _build_ticker(self) -> str:
        """Render a slow horizontal marquee of current-holdings logos.

        Each logo carries the ticker + name in its ``title`` attribute
        for sighted users who hover. The track contains two copies of
        the logo set so the keyframe can translate by exactly -50%
        and the loop is seamless. The whole strip is decorative
        (``aria-hidden="true"``); the actual holding details live in
        the cards below."""
        if not self._current_logos:
            return ""
        items = "".join(
            # Ticker is above the fold so we don't lazy-load, but we
            # still set ``decoding="async"`` so the marquee paints as
            # soon as the first logo is ready. Both ``width`` and
            # ``height`` are pinned at the desktop cell dimensions
            # (56x28 -- the landscape 2:1 cell that normalizes wide
            # and square wordmarks to similar visual prominence; see
            # the ``.ticker__logo`` CSS for the rationale) so the
            # browser reserves the exact box up-front and the
            # marquee paints with zero layout shift even before
            # individual SVGs decode. CSS ``object-fit: contain``
            # fits each logo inside that box without distortion;
            # smaller viewports override the dimensions further down
            # in ``_PAGE_STYLES`` so the cell scales gracefully on
            # mobile.
            f'<img class="ticker__logo" src="{html.escape(url)}" alt="" '
            f'title="{html.escape(f"{ticker} - {name}")}" '
            f'decoding="async" width="56" height="28">'
            for ticker, name, url in self._current_logos
        )
        return (
            '<div class="ticker" aria-hidden="true">'
            f'<div class="ticker__track">{items}{items}</div>'
            '</div>'
        )

    @classmethod
    def _head(cls) -> str:
        title = html.escape(cls.SEO_TITLE)
        desc = html.escape(cls.SITE_DESCRIPTION)
        site = html.escape(cls.SITE_TITLE)
        url = html.escape(cls.SITE_URL)
        image = html.escape(cls.SOCIAL_IMAGE)
        # Hash the inline JSON-LD payload so we can pin it in CSP
        # without ``unsafe-inline`` -- this is where actual XSS would
        # live and the hash makes any drift fail loudly.
        #
        # Styles are split using the CSP3 directives:
        #   - ``style-src-elem`` hashes the single inline <style> block
        #     (the only stylesheet container we emit).
        #   - ``style-src-attr 'unsafe-inline'`` permits inline
        #     ``style="..."`` attributes -- bar widths, delta
        #     positions, legend swatch colours -- which are
        #     programmatically generated and can't all be hashed.
        # The ``style-src`` line is the CSP2 fallback for browsers
        # that don't understand the CSP3 directives; it's intentionally
        # permissive on inline styles since the script-src lock is what
        # actually blocks XSS.
        jsonld_str = cls._jsonld()
        style_hash = _sha256_b64(_PAGE_STYLES)
        jsonld_hash = _sha256_b64(jsonld_str)
        hash_clear_hash = _sha256_b64(_HASH_CLEAR_SCRIPT)
        nav_scroll_hash = _sha256_b64(_NAV_SCROLL_SCRIPT)
        return_chart_hash = _sha256_b64(_RETURN_CHART_SCRIPT)
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'sha256-{jsonld_hash}' "
            f"'sha256-{hash_clear_hash}' "
            f"'sha256-{nav_scroll_hash}' "
            f"'sha256-{return_chart_hash}' "
            "https://static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline'; "
            f"style-src-elem 'self' 'sha256-{style_hash}'; "
            "style-src-attr 'unsafe-inline'; "
            "img-src 'self' https: data:; "
            "connect-src 'self' https://cloudflareinsights.com; "
            "font-src 'self'; "
            "base-uri 'self'; "
            "form-action 'none'; "
            "frame-ancestors 'none'"
        )
        return (
            '<head>\n'
            '<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f'<title>{title}</title>\n'
            f'<meta name="description" content="{desc}">\n'
            '<meta name="author" content="Jan Grzybek">\n'
            # Explicitly invite indexing and large preview images in
            # SERPs. ``max-image-preview:large`` is the Google-recommended
            # opt-in for thumbnail-rich results.
            '<meta name="robots" content="index,follow,max-image-preview:large">\n'
            f'<link rel="canonical" href="{url}">\n'
            # GitHub Pages can't set HTTP security headers, so the next
            # best thing is the ``http-equiv`` meta variants. CSP locks
            # down what scripts/styles/etc. can load. Referrer-Policy
            # avoids leaking the URL of pages users came from.
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
            '<meta name="referrer" content="strict-origin-when-cross-origin">\n'
            # Tints the mobile browser chrome (Safari/Chrome address bar) so it
            # blends with the page background in both colour schemes.
            '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">\n'
            '<meta name="theme-color" content="#111418" media="(prefers-color-scheme: dark)">\n'
            '<link rel="icon" type="image/svg+xml" href="favicon.svg">\n'
            '<link rel="icon" type="image/png" href="favicon.png">\n'
            '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
            '<link rel="icon" href="favicon.ico">\n'
            # Open Graph -- powers rich previews on Facebook, LinkedIn,
            # Slack, Discord, etc. ``og:image:width/height`` are used by
            # platforms to reserve preview space without a HEAD probe.
            f'<meta property="og:title" content="{title}">\n'
            f'<meta property="og:description" content="{desc}">\n'
            f'<meta property="og:image" content="{image}">\n'
            '<meta property="og:image:type" content="image/png">\n'
            '<meta property="og:image:width" content="1200">\n'
            '<meta property="og:image:height" content="630">\n'
            f'<meta property="og:image:alt" content="{title}">\n'
            f'<meta property="og:url" content="{url}">\n'
            '<meta property="og:type" content="website">\n'
            '<meta property="og:locale" content="en_US">\n'
            f'<meta property="og:site_name" content="{site}">\n'
            # Twitter Card -- ``summary_large_image`` shows the OG image
            # at full width in the X/Twitter timeline preview.
            '<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:title" content="{title}">\n'
            f'<meta name="twitter:description" content="{desc}">\n'
            f'<meta name="twitter:image" content="{image}">\n'
            f'<meta name="twitter:image:alt" content="{title}">\n'
            # JSON-LD structured data -- Google parses this to enrich
            # the SERP entry (knowledge-graph signals, sitelinks, etc.).
            f'<script type="application/ld+json">{jsonld_str}</script>\n'
            # Drops the URL hash on the first user-initiated scroll
            # so refresh restores the actual scroll position instead
            # of re-jumping to the last-clicked nav section. See the
            # ``_HASH_CLEAR_SCRIPT`` docstring for the full rationale.
            f'<script>{_HASH_CLEAR_SCRIPT}</script>\n'
            # Custom smooth-scroll for the in-page nav links: cubic
            # easing across the section gap, plus the iOS Safari
            # ``backdrop-filter`` flicker workaround in
            # ``.site-header``. Falls back to instant scroll when
            # ``prefers-reduced-motion`` is set.
            f'<script>{_NAV_SCROLL_SCRIPT}</script>\n'
            # Pointer-driven scrubber for the return chart: a vertical
            # guide line + per-curve markers + tooltip card show the
            # date and JG/benchmark return at any x-coordinate as the
            # user slides a finger or cursor across the plot. See
            # ``_RETURN_CHART_SCRIPT`` for the data-attribute contract.
            f'<script>{_RETURN_CHART_SCRIPT}</script>\n'
            f'<style>{_PAGE_STYLES}</style>\n'
            '</head>'
        )

    @classmethod
    def _jsonld(cls) -> str:
        """Schema.org structured data identifying the site and its author.

        We emit a ``WebSite`` graph with a nested ``Person`` so search
        engines can attribute the portfolio to Jan Grzybek and use the
        description/title in knowledge-graph cards. The output is
        JSON-encoded with ``ensure_ascii=False`` so unicode (e.g.
        en-dashes) round-trips cleanly, and ``</`` is escaped so the
        payload can't accidentally close the surrounding ``<script>``."""
        payload = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": cls.SITE_TITLE,
            "alternateName": "JG Investing",
            "url": cls.SITE_URL,
            "description": cls.SITE_DESCRIPTION,
            "inLanguage": "en",
            "author": {
                "@type": "Person",
                "name": "Jan Grzybek",
                "url": cls.SITE_URL,
            },
        }
        return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    # ----------------------------------------------------- OG image

    # Search order for sans-serif fonts. Picks the first installed
    # candidate; falls back to Pillow's bitmap default if none exist
    # (still readable, just less crisp).
    _FONT_CANDIDATES = {
        "regular": (
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
            ("/usr/share/fonts/dejavu/DejaVuSans.ttf", 0),
            ("/Library/Fonts/Arial.ttf", 0),
            ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
            ("/System/Library/Fonts/Helvetica.ttc", 0),
            ("C:/Windows/Fonts/arial.ttf", 0),
        ),
        "bold": (
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
            ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", 0),
            ("/Library/Fonts/Arial Bold.ttf", 0),
            ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
            ("/System/Library/Fonts/Helvetica.ttc", 1),
            ("C:/Windows/Fonts/arialbd.ttf", 0),
        ),
    }

    @classmethod
    def _load_font(cls, weight: str, size: int):
        """Pick the first installed candidate font for the requested
        weight/size and fall back to Pillow's bitmap default."""
        from PIL import ImageFont
        for path, idx in cls._FONT_CANDIDATES.get(weight, ()):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size, index=idx)
                except Exception:
                    continue
        return ImageFont.load_default()

    def _render_og_image(self) -> None:
        """Render a 1200x630 PNG with the headline numbers for sharing.

        The image is what platforms like LinkedIn, Slack, Discord, X,
        and Facebook display when the URL is pasted into a chat or
        feed. The composition is tuned for that single-glance context:
        a prominent ``Jan Grzybek`` byline, the headline outperformance
        vs the S&P 500 on CAGR (the metric that matters once a long
        enough track record exists), and a strip of the top-10 equity
        holdings' logos so the preview hints at *what* is in the
        portfolio without needing a click. Failures (Pillow missing,
        unwritable disk, etc.) are swallowed - the page still renders
        fine without a regenerated OG image."""
        if self._total_return is None:
            return
        try:
            from PIL import Image, ImageDraw  # noqa: F401  (used below)
        except ImportError:
            return
        try:
            self._render_og_image_unsafe(self._total_return, self._benchmarks or [])
        except Exception:
            # Best-effort: never fail the whole page build because the
            # OG image couldn't be drawn (e.g. on a system with no
            # truetype fonts at all). The static fallback referenced
            # by ``SOCIAL_IMAGE`` will keep working until the next
            # successful regeneration.
            return

    def _render_og_image_unsafe(self, total_return, benchmarks) -> None:
        from PIL import Image, ImageDraw

        W, H = 1200, 630
        # Transparent canvas: we draw on RGBA with a fully-clear
        # background so the same OG image looks correct whether a
        # social platform places it on a light, dark, or branded
        # surface. Readability on both extremes is preserved by:
        #   - a tiny crisp white stroke around dark text (invisible
        #     against white -- white-on-white blends away -- and
        #     just enough of a bright edge to lift dark glyphs off
        #     dark backgrounds without the fat outlined-bubble
        #     letter look that wider strokes produce);
        #   - a semi-transparent white pill with its own soft outer
        #     glow behind the holdings logo strip (most logos are
        #     dark wordmarks that would otherwise vanish on dark
        #     backgrounds).
        TRANSPARENT = (255, 255, 255, 0)
        HALO = (255, 255, 255)
        FG = (17, 17, 17)
        MUTED = (95, 99, 106)
        ACCENT = (230, 125, 34)
        POS = (31, 122, 61)
        NEG = (179, 38, 30)

        # Tiny stroke width (in px) for the dark-mode readability
        # outline around the byline. Sized down hard from the
        # original 5px so the stroke reads as a crisp edge, not a
        # chunky outlined glyph; PIL renders ``stroke_width`` as
        # opaque pixels, so the stroke disappears completely on
        # white backgrounds regardless of width. The caption,
        # footer, and hero number are drawn without a stroke
        # because:
        #   - the caption (32pt) and footer (22pt) are small
        #     enough that PIL's minimum 1px stroke is
        #     proportionally chunky and renders the glyphs as
        #     puffy outlined letters rather than crisp text;
        #     dropping the stroke keeps them razor-sharp on white
        #     and trades a little dark-mode contrast for the
        #     sharpness;
        #   - the hero number's saturated accent green / red
        #     already carries enough contrast on both modes, and
        #     a stroke around a vivid 140pt numeral muddies the
        #     colour rather than helping read it.
        STROKE_BIG = 2    # 96pt display type ("Jan Grzybek")

        bench = benchmarks[0] if benchmarks else None
        cagr = float(total_return.get("cagr%", 0.0))
        bench_cagr = float(bench["cagr%"]) if bench else None
        cagr_delta = (cagr - bench_cagr) if bench_cagr is not None else None
        bench_label = self._benchmark_label(bench) if bench else None
        history = list(total_return.get("history") or [])
        start_date = (
            total_return.get("start_date")
            or (history[0][0] if history else datetime.today())
        )
        duration = _format_duration(relativedelta(datetime.today(), start_date))

        f_name = self._load_font("bold", 96)
        f_hero = self._load_font("bold", 140)
        f_caption = self._load_font("regular", 32)
        f_caption_b = self._load_font("bold", 32)
        f_foot = self._load_font("regular", 22)

        img = Image.new("RGBA", (W, H), TRANSPARENT)
        draw = ImageDraw.Draw(img)

        pad_l = 60

        # ``Jan Grzybek`` is the byline header -- promoted from a
        # small eyebrow to the dominant identity element so the
        # share preview is recognisable from the name first.
        draw.text(
            (pad_l, 36), "Jan Grzybek", font=f_name, fill=FG,
            stroke_width=STROKE_BIG, stroke_fill=HALO,
        )
        # Accent rule under the name doubles as a visual anchor for
        # the rest of the layout. Mid-luminance orange is legible on
        # both light and dark backgrounds without a halo.
        draw.rectangle((pad_l, 168, pad_l + 96, 176), fill=ACCENT)

        # Hero: outperformance vs the benchmark on CAGR. CAGR is the
        # metric that compares portfolios fairly across periods, so
        # it earns the headline slot. When no benchmark is available
        # yet we fall back to the portfolio's own CAGR so the image
        # still has a meaningful headline.
        if cagr_delta is not None:
            hero_text = f"{_fmt_pct(cagr_delta, signed=True)} pp"
            hero_color = POS if cagr_delta >= 0 else NEG
            label = "Outperformance of "
            label_emph = bench_label or "S&P 500"
            label_tail = " on CAGR"
        else:
            hero_text = f"{_fmt_pct(cagr, signed=True)}%"
            hero_color = POS if cagr >= 0 else NEG
            label = "Annualized return ("
            label_emph = "CAGR"
            label_tail = ")"

        draw.text((pad_l, 210), hero_text, font=f_hero, fill=hero_color)

        # Caption below the hero: "Outperformance of S&P 500 on CAGR"
        # with the benchmark name bolded so the reader's eye lands on
        # the comparison subject. Rendered without a stroke so the
        # 32pt glyphs stay sharp on both modes; the bold benchmark
        # name carries the same MUTED fill as the surrounding text
        # so the emphasis lives in the weight alone -- a darker
        # fill on the emphasis would look great on white but vanish
        # on a dark background where MUTED is already at the dim
        # end of legible.
        cap_y = 388
        draw.text((pad_l, cap_y), label, font=f_caption, fill=MUTED)
        label_w = int(draw.textlength(label, font=f_caption))
        draw.text(
            (pad_l + label_w, cap_y), label_emph,
            font=f_caption_b, fill=MUTED,
        )
        emph_w = int(draw.textlength(label_emph, font=f_caption_b))
        draw.text(
            (pad_l + label_w + emph_w, cap_y), label_tail,
            font=f_caption, fill=MUTED,
        )

        # Logo strip: top-10 current holdings by weight. The strip
        # is the visual proof that the headline number is backed by
        # a real portfolio, and it's the only place on the image
        # that hints at *what* is held.
        self._draw_top_holdings_strip(
            img,
            x=pad_l,
            y=470,
            w=W - 2 * pad_l,
            h=90,
        )

        # Footer line: anchor period + URL for visual grounding.
        foot = (
            f"Since {_fmt_date(start_date)}  \u00b7  {duration}  \u00b7  "
            "jan-grzybek.github.io/investing"
        )
        draw.text((pad_l, H - 40), foot, font=f_foot, fill=MUTED)

        img.save("og-image.png", optimize=True)

    # Tickers in ``top_10`` keys that are not real holdings (e.g. the
    # synthetic "Other equities" bucket added when there are >11
    # current positions). Skipped when picking logos for the strip.
    _NON_TICKER_TOP10_KEYS = frozenset({"Other equities"})

    def _top_holdings_for_og(self, limit: int = 10) -> list[str]:
        """Return up to ``limit`` ticker symbols for the OG logo strip.

        ``self.top_10`` is already sorted by weight (descending) and
        may contain a synthetic "Other equities" key when there are
        more than 11 current positions; we filter that out so only
        real tickers reach the logo loader."""
        if not self.top_10:
            return []
        tickers: list[str] = []
        for ticker in self.top_10.keys():
            if ticker in self._NON_TICKER_TOP10_KEYS:
                continue
            tickers.append(ticker)
            if len(tickers) >= limit:
                break
        return tickers

    @staticmethod
    def _load_logo_for_og(ticker: str, max_w: int, max_h: int):
        """Load a ticker's logo as an RGBA ``PIL.Image`` fitted to a
        ``max_w x max_h`` box (preserving aspect ratio).

        Reads from the local ``logos/`` directory next to ``update.py``
        rather than going over HTTP, so the OG image is reproducible
        without a network round-trip and works the first time the
        site is deployed (before any logo is live behind
        ``LOGOS_ADDRESS``). SVG logos are rasterised with ``cairosvg``
        at 2x the target dimensions for crispness; raster logos
        (PNG/JPG) are loaded directly. Falls back to ``courage.png``
        when no per-ticker logo is on file, and returns ``None`` when
        even that fails so the caller can leave a gap rather than
        crash the whole image."""
        from PIL import Image

        candidates = [
            os.path.join(_REPO_LOGOS_DIR, f"{ticker}{ext}")
            for ext in LOGO_EXTENSIONS
        ]
        candidates.append(os.path.join(_REPO_LOGOS_DIR, "courage.png"))

        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                if path.lower().endswith(".svg"):
                    import cairosvg

                    # Pass ``output_height`` only -- cairosvg would
                    # *stretch* the SVG to a non-native aspect ratio
                    # if both dimensions were pinned, which squashes
                    # wide logos (Salesforce, NVIDIA, etc.). Pinning
                    # the height alone keeps the natural aspect
                    # ratio; the LANCZOS resize below caps the width
                    # at ``max_w``. 2x supersample for crispness.
                    png_bytes = cairosvg.svg2png(
                        url=path,
                        output_height=max_h * 2,
                    )
                    src = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
                else:
                    src = Image.open(path).convert("RGBA")
            except Exception:
                continue

            # Fit to the target box while preserving aspect ratio.
            scale = min(max_w / src.width, max_h / src.height)
            new_w = max(1, int(round(src.width * scale)))
            new_h = max(1, int(round(src.height * scale)))
            return src.resize((new_w, new_h), Image.LANCZOS)

        return None

    def _draw_top_holdings_strip(
        self, canvas, *, x: int, y: int, w: int, h: int,
    ) -> None:
        """Render up to 10 logos of the largest current holdings in a
        single horizontal row inside the ``(x, y, w, h)`` rectangle.

        Each logo is fitted into a same-width cell with consistent
        gaps so the strip reads as a uniform "what's inside" row
        regardless of the underlying logos' aspect ratios. A
        semi-transparent white pill sits behind the row so that the
        predominantly dark logo wordmarks (Adobe, Lam Research,
        Samsung, ...) stay legible when the OG image is composited
        on a dark surface. The pill itself is rendered with a soft
        outer halo (a Gaussian-blurred copy of the same shape)
        composited underneath so the strip feels lifted off the
        background rather than stamped on top of it; on white
        backgrounds the halo blends in and is invisible. The strip
        is left untouched when there are no current holdings yet
        (e.g. on the very first build) so the rest of the layout
        still reads cleanly."""
        from PIL import Image, ImageDraw, ImageFilter

        tickers = self._top_holdings_for_og(limit=10)
        if not tickers:
            return

        slots = max(len(tickers), 1)
        # Tight gap on small counts, looser gap once the row fills up,
        # so a 3-ticker row doesn't look unintentionally airy.
        gap = 20 if slots >= 6 else 28
        cell_w = (w - gap * (slots - 1)) // slots
        cell_h = h
        # Center the strip horizontally within the requested width
        # when there are fewer than 10 slots so a short row still
        # feels balanced under the hero number.
        used_w = slots * cell_w + (slots - 1) * gap
        offset_x = x + (w - used_w) // 2

        # Card backdrop. Semi-transparent white pill (alpha 225
        # gives a slight "frosted glass" softness on dark
        # backgrounds while keeping dark logo wordmarks high
        # contrast) wrapped in a tight Gaussian-blurred outer halo
        # of the same shape, lifting the card visually off dark
        # backgrounds; on white pages both layers blend into the
        # page so the strip reads as plain logos. The blur radius
        # is intentionally tight (~6px) so the halo's bottom edge
        # stays well clear of the footer text below the card --
        # a wider glow looks pretty in isolation but its faint
        # white wash crosses into the footer and dims the contrast
        # of the small muted glyphs there. The padding values are
        # tuned so the pill hugs the row of logos with a bit of
        # breathing room on all sides.
        pad_x = 24
        pad_y = 18
        card_rect = (
            offset_x - pad_x,
            y - pad_y,
            offset_x + used_w + pad_x,
            y + cell_h + pad_y,
        )
        # White-RGB transparent canvas (not (0,0,0,0)) so the
        # GaussianBlur pass below doesn't average dark transparent
        # pixels into the halo's RGB and fringe the pill's outer
        # glow with grey on white backgrounds. Only the alpha
        # channel needs to spread; RGB stays pure white throughout.
        card_layer = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
        ImageDraw.Draw(card_layer).rounded_rectangle(
            card_rect, radius=24, fill=(255, 255, 255, 225),
        )
        glow_layer = card_layer.filter(ImageFilter.GaussianBlur(radius=6))
        canvas.alpha_composite(glow_layer)
        canvas.alpha_composite(card_layer)

        for idx, ticker in enumerate(tickers):
            cell_x = offset_x + idx * (cell_w + gap)
            logo = self._load_logo_for_og(ticker, cell_w, cell_h)
            if logo is None:
                continue
            # Center the logo within its cell -- horizontally because
            # narrow logos otherwise hug the left edge, and
            # vertically so wide logos line up on a consistent
            # midline with square ones.
            paste_x = cell_x + (cell_w - logo.width) // 2
            paste_y = y + (cell_h - logo.height) // 2
            canvas.paste(logo, (paste_x, paste_y), logo)

    @staticmethod
    def _footer(update_date: str, update_iso: str) -> str:
        return (
            '<footer>\n'
            '<ul class="footer__notes">\n'
            '<li>All performance metrics on this page (TSR, TWR, CAGR) were '
            'calculated using USD as the base currency.</li>\n'
            '<li>TSR figures were calculated using the modified Dietz method, '
            'with dividends assumed to be subject to a 15% withholding tax '
            'and cashed out.</li>\n'
            '<li>The portfolio-level time-weighted return (TWR) was calculated '
            'excluding the impact of capital gains taxes, but including the '
            'effects of withholding taxes and transaction costs.</li>\n'
            '<li>The latest stock prices and dividend data used in the '
            'calculations were obtained from '
            '<a href="https://finance.yahoo.com/markets/stocks/trending/" '
            'title="Yahoo Finance" rel="noopener noreferrer">'
            'Yahoo Finance</a>.</li>\n'
            '</ul>\n'
            '<p class="footer__disclaimer">For informational purposes only. '
            'Nothing contained herein should be construed as a recommendation '
            'to buy, sell or hold any security or pursue any investment '
            'strategy.</p>\n'
            '<p class="footer__legal">Logos are trademarks of their respective '
            'owners and are used for identification purposes only. This webpage '
            'uses Cloudflare Web Analytics to measure anonymous traffic '
            'statistics. No cookies or tracking identifiers are used.</p>\n'
            f'<p class="footer__updated">Updated on '
            f'<time datetime="{update_iso}">{update_date}</time></p>\n'
            '</footer>'
        )

    def _get_logo_url(self, ticker):
        cached = self._logo_cache.get(ticker)
        if cached is not None:
            return cached
        encoded = ticker.replace(":", "%3A")
        for extension in LOGO_EXTENSIONS:
            url = LOGOS_ADDRESS + encoded + extension
            response = requests.head(url)
            if response.status_code == 200:
                self._logo_cache[ticker] = url
                return url
        self._logo_cache[ticker] = COURAGE_LOGO
        return COURAGE_LOGO

    # ---- per-section builders ------------------------------------------

    def _build_return_section(self, total_return, benchmarks) -> str:
        lines: list[str] = []
        # Chart leads the section as the headline visual; its caption
        # carries the start date so the comparison block below can stay
        # focused on the head-to-head numbers without restating the
        # period. When there's no chart (single-point history) we move
        # the "Since {start}" header into the comparison block instead.
        chart = self._render_return_chart(total_return, benchmarks)
        if chart:
            lines.append(chart)
        lines.append(self._build_returns_comparison(
            total_return, benchmarks, include_period=not chart,
        ))
        return "\n".join(lines)

    def _build_returns_comparison(
        self, total_return, benchmarks, *, include_period: bool,
    ) -> str:
        """Render JG vs benchmark side-by-side with shared metrics.

        When ``include_period`` is true a "Since {start}" header is
        prepended to the block; otherwise the chart caption already
        carries that information so we omit it here to avoid repeating
        the period (and its length) twice.

        A delta line at the bottom summarises the outperformance (or
        underperformance) in percentage points so the comparison reads
        head-to-head over the same measurement window."""
        period_html = ""
        if include_period:
            start_date = total_return["start_date"]
            duration = _format_duration(relativedelta(datetime.today(), start_date))
            period_html = (
                '<p class="returns-compare__period">'
                f'Since <time datetime="{start_date.strftime("%Y-%m-%d")}">'
                f'{_fmt_date(start_date)}</time> &middot; '
                f'{html.escape(duration)}'
                '</p>'
            )

        cols: list[str] = [self._render_compare_col(
            name="JG",
            subtitle="Jan Grzybek",
            logo_url=COURAGE_LOGO,
            rows=[
                ("TWR", total_return["twr%"]),
                ("CAGR", total_return["cagr%"]),
            ],
        )]
        for benchmark in benchmarks or []:
            cols.append(self._render_compare_col(
                name=self._benchmark_label(benchmark),
                subtitle=benchmark.get("ticker") or "",
                logo_url=self._get_logo_url(benchmark["ticker"]),
                rows=[
                    ("TSR", benchmark["tsr%"]),
                    ("CAGR", benchmark["cagr%"]),
                ],
            ))

        delta_html = ""
        if benchmarks:
            b = benchmarks[0]
            twr_delta = total_return["twr%"] - b["tsr%"]
            cagr_delta = total_return["cagr%"] - b["cagr%"]
            # Each piece is its own span with ``white-space: nowrap``
            # so a narrow viewport never breaks "+6.7 pp Total Return"
            # or "+1.3 pp CAGR" mid-phrase. The container is a flex
            # row that wraps under pressure; at <=540px each piece
            # gets ``flex: 1 0 100%`` and stacks vertically.
            # The TWR vs benchmark TSR delta is labelled "Total
            # Return" here -- the capsule columns above already give
            # the precise per-side metric ("TWR" for JG, "TSR" for
            # the benchmark), so this summary line just states what's
            # being compared. Title-cased to sit visually parallel
            # with the ``CAGR`` token next to it, both reading as
            # data labels rather than prose.
            delta_html = (
                '<p class="returns-compare__delta">'
                '<span class="returns-compare__delta-prefix">JG vs '
                f'{html.escape(self._benchmark_label(b))}:</span>'
                f'<span class="returns-compare__delta-metric '
                f'{_value_class(twr_delta)}">'
                f'{_fmt_pct(twr_delta, signed=True)} pp Total Return</span>'
                '<span class="returns-compare__delta-sep" '
                'aria-hidden="true">&middot;</span>'
                f'<span class="returns-compare__delta-metric '
                f'{_value_class(cagr_delta)}">'
                f'{_fmt_pct(cagr_delta, signed=True)} pp CAGR</span>'
                '</p>'
            )

        return (
            '<section class="returns-compare">'
            f'{period_html}'
            f'<div class="returns-compare__grid">{"".join(cols)}</div>'
            f'{delta_html}'
            '</section>'
        )

    @staticmethod
    def _benchmark_label(benchmark) -> str:
        """Friendly display name for a benchmark, falling back gracefully."""
        ticker = benchmark.get("ticker", "")
        return (
            _BENCHMARK_DISPLAY_NAMES.get(ticker)
            or benchmark.get("name")
            or ticker
            or "Benchmark"
        )

    @staticmethod
    def _render_compare_col(*, name, subtitle, logo_url, rows) -> str:
        stat_html = []
        for label, value in rows:
            # ``value`` is the unrounded percentage straight off
            # ``total_return`` / benchmark dicts; ``_fmt_pct`` decides
            # at format time whether to render one decimal (<100%)
            # or whole-number (>=100%, where the decimal is just
            # noise next to a 3-digit integer part).
            stat_html.append(
                f'<dt>{html.escape(label)}</dt>'
                f'<dd class="{_value_class(value)}">{_fmt_pct(value)}%</dd>'
            )
        sub_html = ""
        if subtitle:
            sub_html = (
                f'<small class="returns-compare__name-sub">'
                f'{html.escape(subtitle)}</small>'
            )
        # ``h3`` keeps the heading tree contiguous: the parent section
        # is at h2, so jumping to h4 here would skip a level (a WCAG
        # and SEO smell).
        return (
            '<article class="returns-compare__col">'
            '<h3 class="returns-compare__name">'
            f'<img class="returns-compare__logo" src="{html.escape(logo_url)}" '
            'alt="" decoding="async" width="48" height="48">'
            f'<span class="returns-compare__name-text">'
            f'{html.escape(name)}{sub_html}</span>'
            '</h3>'
            f'<dl class="returns-compare__stats">{"".join(stat_html)}</dl>'
            '</article>'
        )

    def _build_trade_card(self, event) -> str:
        """Render one burst-aggregated trade as a capsule.

        The layout mirrors the holding cards (logo / body / right-rail
        metadata) so the page reads as a consistent family of capsules
        even when the reader scrolls between sections. The per-share
        price is shown in the security's native currency, prefixed by
        ``@`` and the ISO code (e.g. ``@ EUR 76.32``) -- the ``@``
        glyph reads as the finance-shorthand "at the price of",
        making the right-rail value unambiguously a transaction
        price rather than some balance. Quantities are deliberately
        absent: the page commits to publishing only relative
        percentages and per-share prices, never nominal sizes.
        """
        label, modifier = _TRADE_CATEGORY_DISPLAY[event["category"]]
        # INCREASE / DECREASE rows attach " by X%" so the scale of
        # the action lands in the same glance as the verb. OPEN /
        # CLOSE rows pass the bare label through -- the long-term-
        # investor verbs ("Initiated" / "Divested") already read as
        # complete actions, so no "position" suffix is needed to
        # avoid sounding half-finished next to the magnitude-bearing
        # rows. The percentage is rendered as a whole number -- this
        # is the user-facing readout convention for this section
        # specifically; the page-wide ``_fmt_pct`` helper that gives
        # one decimal under 100 is reserved for the performance /
        # return rows where that extra digit is meaningful.
        delta_pct = event.get("delta_pct")
        if (
            event["category"] in ("INCREASE", "DECREASE")
            and delta_pct is not None
        ):
            label = f"{label} by {delta_pct:.0f}%"
        start = event["start_date"]
        end = event["end_date"]
        if start == end:
            # Single-day bursts (the common case for one-off trades)
            # render as a plain date rather than a "X - X" range,
            # which would look like a typo.
            period_html = (
                f'<time datetime="{start.strftime("%Y-%m-%d")}">'
                f'{_fmt_date(start)}</time>'
            )
        else:
            period_html = (
                f'<time datetime="{start.strftime("%Y-%m-%d")}">'
                f'{_fmt_date(start)}</time>'
                ' &ndash; '
                f'<time datetime="{end.strftime("%Y-%m-%d")}">'
                f'{_fmt_date(end)}</time>'
            )
        # Thousands separator + 2 decimals reads well across the full
        # range of equity prices we ingest (sub-dollar US tickers up
        # through GBp pence quotes in the thousands). The currency
        # code prefix is unambiguous in a multi-market portfolio --
        # a leading "$" would silently misrepresent a EUR or GBp
        # trade as USD.
        price_html = html.escape(
            f"@ {event['currency']} {event['price']:,.2f}"
        )
        logo_url = self._get_logo_url(event["ticker"])
        title = f"{event['ticker']} - {event['name']}"
        return (
            '<article class="trade">'
            # Below-the-fold: lazy decoding + reserved dimensions to
            # avoid CLS while logos stream in, mirroring the holding
            # card's image attributes.
            f'<img class="trade__logo" src="{html.escape(logo_url)}" '
            'alt="" loading="lazy" decoding="async" '
            'width="48" height="48">'
            '<div class="trade__body">'
            f'<h3 class="trade__title">{html.escape(title)}</h3>'
            f'<p class="trade__period">{period_html}</p>'
            '</div>'
            '<div class="trade__meta">'
            f'<span class="trade__badge trade__badge--{modifier}">'
            f'{html.escape(label)}</span>'
            f'<span class="trade__price">{price_html}</span>'
            '</div>'
            '</article>'
        )

    def _build_holding_card(self, holding) -> str:
        stats: list[tuple[str, str, float | None]] = [
            # ``tsr%``/``cagr%``/``current_weight%`` are unrounded
            # floats on the data dict; ``_fmt_pct`` chooses one
            # decimal under 100 and whole-number from 100 up so a
            # 3-digit TSR (e.g. NVDA at +217%) doesn't carry a
            # noisy ``.4`` next to it. The raw float still flows
            # to ``_value_class`` for sign-based colouring.
            ("TSR:", f"{_fmt_pct(holding['tsr%'])}%", holding["tsr%"]),
        ]
        if holding["cagr%"] > CAGR_TBA_THRESHOLD:
            stats.append(("CAGR:", "TBA", None))
        else:
            stats.append(("CAGR:", f"{_fmt_pct(holding['cagr%'])}%", holding["cagr%"]))
        if holding["is_current"]:
            assert holding["current_weight%"] is not None
            stats.append(("Weight:", f"{_fmt_pct(holding['current_weight%'])}%", None))

        periods = [(p["start"], p["end"]) for p in holding["periods"]]

        return self._build_card(
            logo_url=self._get_logo_url(holding["ticker"]),
            title=f'{holding["ticker"]} - {holding["name"]}',
            stats=stats,
            periods=periods,
        )

    @staticmethod
    def _build_card(*, logo_url, title, stats, periods=None, note: str | None = None) -> str:
        """Render a capsule with logo, title/period(s)/note, and right-aligned stats."""
        body_parts = [f'<h3 class="holding__title">{html.escape(title)}</h3>']
        if periods:
            # Always render the most-recent period first so it sits at
            # the top of the visual stack -- that's what readers scan
            # first. We sort here defensively rather than trusting the
            # caller: ``Holding.summary`` already returns newest-first
            # in production, but the preview/synthetic data and any
            # future call sites might not, and the visual order is a
            # UX guarantee, not an upstream invariant. Sorting by
            # ``start`` descending puts the period with the latest
            # opening date on top; periods don't overlap, so this also
            # implicitly sorts by ``end`` descending.
            ordered = sorted(periods, key=lambda p: p[0], reverse=True)
            items = []
            for start, end in ordered:
                # Each <li> emits three children -- start <time>, the
                # dash separator, and the end (either a <time> or a
                # plain <span> for "Present"). Combined with the
                # ``display: contents`` rule on .holding__periods li,
                # those three pieces become grid items in the parent
                # <ul>'s 3-column grid, so the dash and end-date
                # column line up vertically across multiple periods
                # even when day numbers have different digit counts.
                start_html = (
                    f'<time datetime="{start.strftime("%Y-%m-%d")}">'
                    f'{_fmt_date(start)}</time>'
                )
                if end is None:
                    end_html = '<span>Present</span>'
                else:
                    end_html = (
                        f'<time datetime="{end.strftime("%Y-%m-%d")}">'
                        f'{_fmt_date(end)}</time>'
                    )
                items.append(
                    f'<li>{start_html}<span>-</span>{end_html}</li>'
                )
            body_parts.append(
                f'<ul class="holding__periods">{"".join(items)}</ul>'
            )
        if note:
            body_parts.append(f'<p class="holding__note">{html.escape(note)}</p>')

        stat_parts = []
        for label, value, sign in stats:
            attr = ""
            if sign is not None:
                attr = f' class="{_value_class(sign)}"'
            # Each label-value pair gets its own ``<div>`` wrapper so
            # mobile CSS can treat the pair as a single flex item and
            # spread TSR/CAGR/Weight across the full row width with
            # ``justify-content: space-between`` (instead of clumping
            # them on the left and leaving an awkward gap on the
            # right). Desktop neutralises the wrapper with
            # ``display: contents`` so dt/dd still feed the parent's
            # 2-column grid as before. ``<div>`` is a valid grouping
            # element inside ``<dl>`` per HTML5.
            stat_parts.append(
                '<div class="holding__stat">'
                f'<dt>{html.escape(label)}</dt>'
                f'<dd{attr}>{html.escape(value)}</dd>'
                '</div>'
            )

        return (
            '<article class="holding">'
            # Below-the-fold logos load lazily; explicit dimensions
            # reserve space and keep CLS at zero.
            f'<img class="holding__logo" src="{html.escape(logo_url)}" '
            'alt="" loading="lazy" decoding="async" '
            'width="64" height="64">'
            f'<div class="holding__body">{"".join(body_parts)}</div>'
            f'<dl class="holding__stats">{"".join(stat_parts)}</dl>'
            '</article>'
        )

    # ---- chart / bar primitives (also covered directly by tests) -------

    @staticmethod
    def _render_bars(rows, variant: str, *, scale_to_max: bool = False) -> str:
        """Render a horizontal CSS bar chart.

        ``rows`` is an iterable of ``(label, value)`` pairs where ``value``
        is a percentage (0..100). Each row renders as
        ``label | value | bar`` so the percentages sit between the title
        and the bar. ``variant`` is the BEM modifier controlling the fill
        colour (e.g. ``"allocation"`` or ``"equities"``).

        With ``scale_to_max=True`` the widest bar fills its track and the
        rest are sized proportionally to the largest value. Useful when
        the rows do not sum to 100% (e.g. the top-N equities) and the
        viewer cares about relative weight rather than absolute share.
        """
        if not rows:
            return ""
        rows = list(rows)
        denom = max((value for _, value in rows), default=0.0) if scale_to_max else 100.0
        if not denom:
            denom = 100.0

        row_html = []
        for label, value in rows:
            # ``value`` arrives unrounded (allocation% / weight%);
            # the bar's CSS width gets two decimals for sub-pixel
            # precision while the visible label uses ``_fmt_pct`` --
            # one decimal under 100, whole-number from 100 up.
            width = value / denom * 100 if scale_to_max else value
            row_html.append(
                '<div class="bars__row">'
                f'<div class="bars__label">{html.escape(str(label))}</div>'
                f'<div class="bars__value">{_fmt_pct(value)}%</div>'
                f'<div class="bars__track"><div class="bars__fill" '
                f'style="width: {width:.2f}%"></div></div>'
                '</div>'
            )
        return f'<div class="bars bars--{variant}">{"".join(row_html)}</div>'

    @classmethod
    def _render_return_chart(cls, total_return, benchmarks) -> str:
        """Render an inline SVG of the portfolio return curve.

        When a benchmark is present we reserve a slice on the right edge
        of the chart for an outperformance annotation: a vertical line
        connecting the JG and benchmark endpoints with a "+X.X pp" label
        showing the cumulative-return delta in percentage points.

        Returns an empty string when the history has fewer than two
        samples (since there is nothing to draw)."""
        history = total_return.get("history", [])
        if len(history) < 2:
            return ""

        # Collect series (JG + each benchmark) and the global y-range.
        start_date = history[0][0]
        time_x = np.array([int((d - start_date).days) for d, _ in history], dtype=float)
        jg_y = np.array([v for _, v in history], dtype=float)

        series: list[tuple[str, str, np.ndarray]] = [("jg", "JG", jg_y)]
        for benchmark in benchmarks or []:
            bh = benchmark.get("history", [])
            if len(bh) < 2:
                continue
            label = cls._benchmark_label(benchmark)
            series.append(("bench", label, np.array([v for _, v in bh], dtype=float)))

        # JSON payload consumed by ``_RETURN_CHART_SCRIPT`` to drive
        # the pointer-driven scrubber. The chart visualises every
        # series on a shared x-axis (JG's dates) -- bench y-values
        # are plotted positionally against JG dates rather than
        # against their own -- so we hand the script the same shared
        # x-array per series to keep tooltip dates and curve geometry
        # in lockstep.
        shared_x_days = [int(d) for d in time_x.tolist()]

        min_y = min(float(s[2].min()) for s in series)
        max_y = max(float(s[2].max()) for s in series)
        # Add a little headroom so the curves don't sit on the frame.
        pad_y = max((max_y - min_y) * 0.05, 0.01)
        view_max = max_y + pad_y
        view_min = min_y - pad_y

        # Viewport (unitless; the CSS picks the rendered size).
        width = 1000.0
        height = 400.0
        # Reserve 12% on the right when we'll be drawing a delta
        # annotation so its bar+label don't overlap the curves.
        has_delta = (
            len(series) >= 2 and series[0][0] == "jg" and series[1][0] == "bench"
        )
        right_margin_pct = 12.0 if has_delta else 0.0
        chart_x_end = width * (1 - right_margin_pct / 100.0)

        def map_x(x_days: float) -> float:
            span = float(time_x.max() - time_x.min()) or 1.0
            return (x_days - float(time_x.min())) / span * chart_x_end

        def map_y(value: float) -> float:
            span = view_max - view_min or 1.0
            return height - (value - view_min) / span * height

        # Smooth interpolation when there are three or more points,
        # straight segments for two.
        if len(time_x) >= 3:
            dense = np.linspace(time_x.min(), time_x.max(), 200)
            interp_x = dense
            interp_targets = {id(s[2]): np.exp(PchipInterpolator(time_x, np.log(s[2]))(dense)) for s in series}
        else:
            interp_x = time_x
            interp_targets = {id(s[2]): s[2] for s in series}

        def to_points(ys: np.ndarray) -> str:
            return " ".join(f"{map_x(x):.2f},{map_y(y):.2f}" for x, y in zip(interp_x, ys))

        ref_y = map_y(1.0)
        svg_lines = [
            f'<svg viewBox="0 0 {int(width)} {int(height)}" xmlns="http://www.w3.org/2000/svg" '
            'preserveAspectRatio="none" role="img" aria-label="Portfolio return curve">',
            f'<line class="return-chart__ref" x1="0" y1="{ref_y:.2f}" x2="{chart_x_end:.2f}" y2="{ref_y:.2f}"/>',
        ]
        for kind, _label, ys in series:
            svg_lines.append(
                f'<polyline class="return-chart__line return-chart__line--{kind}" '
                f'points="{to_points(interp_targets[id(ys)])}"/>'
            )
        svg_lines.append('</svg>')

        # Outperformance overlay: a vertical bar between the two curve
        # endpoints with a percentage-point delta label. Built as
        # absolutely-positioned HTML (rather than SVG text or a single
        # bordered box) so the bar can stay glued to the chart-end
        # x-coordinate at every viewport while the label flows around
        # it - on wide screens to its right, on phones to its left
        # with a translucent backdrop. SVG text would also be
        # unreadably small on phones because of viewBox scaling.
        delta_html = ""
        if has_delta:
            jg_final = float(series[0][2][-1])
            bench_final = float(series[1][2][-1])
            # Prefer the canonical TWR (JG) - TSR (benchmark) delta
            # straight off ``total_return`` / ``benchmarks``: the JG
            # vs S&P 500 capsule directly below the chart shows the
            # exact same delta as ``+X.X pp Total Return``, and we
            # don't want the two numbers to drift apart. Modified
            # Dietz TWR/TSR are computed cashflow-aware over the
            # exact period, while the chart's curves are sampled at
            # discrete dates, so naively differencing the last-point
            # values can disagree with the canonical metric by
            # several tenths of a percentage point. Falling back to
            # the curve endpoints when those metrics aren't supplied
            # keeps the renderer usable from unit tests and any
            # future caller that only has a history.
            twr_pct = total_return.get("twr%")
            tsr_pct = benchmarks[0].get("tsr%") if benchmarks else None
            if twr_pct is not None and tsr_pct is not None:
                delta_pp = float(twr_pct) - float(tsr_pct)
            else:
                delta_pp = (jg_final - bench_final) * 100.0
            jg_y_pct = map_y(jg_final) / height * 100.0
            bench_y_pct = map_y(bench_final) / height * 100.0
            top_pct = min(jg_y_pct, bench_y_pct)
            height_pct = abs(jg_y_pct - bench_y_pct)
            # ``--delta-color`` is consumed by the caliper bracket
            # (vertical spine + top/bottom jaws) in ``_PAGE_STYLES``.
            # Encoding the sign here keeps the colour logic in one
            # place: the same green/red mapping that drives the label
            # also tints the bracket so its visual meaning is
            # self-evident.
            delta_color = (
                "var(--positive)" if delta_pp >= 0 else "var(--negative)"
            )
            delta_html = (
                '<div class="return-chart__delta" '
                f'style="--top: {top_pct:.2f}%; --height: {height_pct:.2f}%; '
                f'--delta-color: {delta_color};">'
                '<span class="return-chart__delta-bar"></span>'
                f'<span class="return-chart__delta-label {_value_class(delta_pp)}">'
                f'{_fmt_pct(delta_pp, signed=True)} pp</span>'
                '</div>'
            )

        # Legend (only when there is more than one series).
        legend_html = ""
        if len(series) > 1:
            chips = []
            for kind, label, _ in series:
                chips.append(
                    f'<span><span class="return-chart__swatch return-chart__swatch--{kind}" '
                    f'style="background: var(--{"accent" if kind == "jg" else "accent-bench"});"></span>'
                    f'{html.escape(label)}</span>'
                )
            legend_html = f'<div class="return-chart__legend">{"".join(chips)}</div>'

        # Caption: when this chart sits above the comparison block we
        # rely on it to anchor the period (the comparison block omits
        # its own period header in that case to avoid repetition).
        # The duration follows the start date so the reader sees both
        # the anchor point ("since when?") and the elapsed window
        # ("how long?") in one glance.
        duration = _format_duration(relativedelta(history[-1][0], start_date))
        caption = (
            f'<div class="return-chart__caption">'
            f'Since <time datetime="{start_date.strftime("%Y-%m-%d")}">'
            f'{_fmt_date(start_date)}</time> &middot; '
            f'{html.escape(duration)}</div>'
        )

        # Hover overlay: empty containers the scrubber script fills
        # in on the fly. The guide line and tooltip stay invisible
        # until a pointer enters the plot (CSS toggles
        # ``.is-active``). Markers/rows are injected by the script
        # so the markup is identical for one- and two-series charts.
        hover_html = (
            '<div class="return-chart__hover" aria-hidden="true">'
            '<div class="return-chart__guide"></div>'
            '<div class="return-chart__tooltip">'
            '<div class="return-chart__tooltip-date"></div>'
            '<div class="return-chart__tooltip-rows"></div>'
            '</div>'
            '</div>'
        )

        # Pack the scrubber data into a JSON blob on the <figure>.
        # Values are rounded to six decimals -- well past the chart's
        # visual precision -- so the inline payload stays compact
        # even for histories with hundreds of samples.
        chart_data = {
            "start": start_date.strftime("%Y-%m-%d"),
            "totalDays": int(time_x[-1] - time_x[0]),
            "rightPct": right_margin_pct,
            "yMin": round(float(view_min), 6),
            "yMax": round(float(view_max), 6),
            "series": [
                {
                    "kind": kind,
                    "label": label,
                    "x": shared_x_days,
                    "y": [round(float(v), 6) for v in ys.tolist()],
                }
                for kind, label, ys in series
            ],
        }
        chart_data_attr = html.escape(
            json.dumps(chart_data, separators=(",", ":")), quote=True
        )

        plot_html = (
            f'<div class="return-chart__plot">'
            f'{"".join(svg_lines)}{delta_html}{hover_html}'
            f'</div>'
        )
        return (
            f'<figure class="return-chart" data-chart="{chart_data_attr}">'
            f'{plot_html}{legend_html}{caption}</figure>'
        )


def generate_webpage(total_return, benchmarks, holdings):
    webpage = Webpage()
    webpage.add_return(total_return, benchmarks)
    webpage.add_allocations(holdings.get("allocation%"), holdings.get("top_10"))
    for holding in holdings["current"]:
        webpage.add_holding(holding)
    for holding in holdings["historical"]:
        webpage.add_holding(holding)
    webpage.add_trades(holdings.get("trades") or [])
    webpage.save()


def main():
    transactions, valuations, cash = pull_data()
    holdings = get_holdings(transactions)
    total_return = calc_twr(valuations, summarize(holdings, cash))
    benchmarks = get_benchmarks(total_return["history"])
    generate_webpage(total_return, benchmarks, holdings)


# ---------------------------------------------------------------------------
# Leak-safe entrypoint
# ---------------------------------------------------------------------------
#
# The CI workflow that drives this script (``.github/workflows/main.yml``)
# runs in a public repository, so its job logs are world-readable. The
# run handles two classes of data that must not surface there:
#
#   1. Secrets injected by GitHub Actions: ``GSHEET_ID`` and the
#      service-account JSON written to ``/tmp/gsheet_creds.json``.
#   2. Nominal portfolio values used to derive the percentages we *do*
#      publish: share counts, cash balances, per-trade prices, dividend
#      payouts, FX rates, etc.
#
# Both leak easily through stderr. Library code (``yfinance`` rate-limit
# notices, ``gspread`` HTTP error bodies, NumPy/Pandas runtime warnings)
# echoes amounts and identifiers back; Python tracebacks routinely
# embed offending values via ``str(exc)`` -- e.g. ``KeyError: '<sheet
# id>'`` or ``ValueError: could not convert string to float: '12,345.67'``.
# The previous mitigation was a blanket ``2>/dev/null`` on the workflow
# command, which traded leakage for total opacity: a failed run gave
# zero signal as to *why* it failed.
#
# ``_run_main_safely`` is the structured replacement. While ``main``
# executes, stderr is fully suppressed -- both ``sys.stderr`` and the
# underlying file descriptor, so output from C extensions that bypass
# the Python wrapper is silenced too. On a clean run nothing leaks. On
# failure we restore stderr and emit a *hand-formatted* summary made up
# exclusively of identifiers that already live in the public repository
# (or in third-party packages on PyPI): the exception class name and,
# for every frame in the chained traceback, the file path, line number,
# function name and the offending source line. We deliberately omit
# ``str(exc)``, exception ``__notes__`` and any local variables, since
# those are the channels through which runtime values normally surface.


def _print_sanitized_failure(exc: BaseException) -> None:
    """Emit a leak-safe traceback for ``exc`` on the real stderr.

    Only identifiers drawn from public source code are written: the
    exception type, plus per-frame ``filename:lineno`` / function name
    / source line. Exception messages, ``__notes__`` and local
    variables -- the usual carriers of runtime values -- are dropped.
    """
    def _emit(prefix: str, error: BaseException) -> None:
        sys.stderr.write(f"{prefix}{type(error).__qualname__}\n")
        for frame in traceback.extract_tb(error.__traceback__):
            sys.stderr.write(
                f"  at {frame.filename}:{frame.lineno} in {frame.name}\n"
            )
            if frame.line:
                sys.stderr.write(f"    {frame.line}\n")

    _emit("update.py failed: ", exc)
    # Walk the cause/context chain so the root cause isn't lost when an
    # outer frame just re-raises. ``seen`` guards against pathological
    # cycles (``raise X from X``) that would otherwise loop forever.
    seen: set[int] = {id(exc)}
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        _emit("caused by: ", cause)
        cause = cause.__cause__ or cause.__context__
    sys.stderr.flush()


def _run_main_safely() -> None:
    """Run :func:`main` with stderr fully redacted.

    See the section comment above for rationale. The function exits
    the process with status 1 on any exception (including
    ``KeyboardInterrupt`` / ``SystemExit`` with a non-zero code) after
    printing a sanitized failure summary; on success it returns
    normally so the caller can chain further work if it ever needs to.
    """
    real_stderr = sys.stderr
    devnull_py = open(os.devnull, "w")
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stderr_fd = os.dup(2)

    def _restore() -> None:
        sys.stderr = real_stderr
        try:
            devnull_py.close()
        finally:
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stderr_fd)
            os.close(devnull_fd)

    os.dup2(devnull_fd, 2)
    sys.stderr = devnull_py
    try:
        main()
    except SystemExit as exc:
        _restore()
        # Preserve an explicit ``sys.exit(0)`` from inside ``main``; only
        # synthesise a sanitized report when the exit signals failure.
        code = exc.code if isinstance(exc.code, int) else 1
        if code != 0:
            _print_sanitized_failure(exc)
            sys.exit(1)
        return
    except BaseException as exc:
        _restore()
        _print_sanitized_failure(exc)
        sys.exit(1)
    else:
        _restore()


if __name__ == "__main__":
    _run_main_safely()
