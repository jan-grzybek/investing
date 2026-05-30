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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

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
        if current_quantity == 0:
            self._periods.append({"start": trade.date, "end": None})
        elif trade.date > self._positions[-1]["date"]:
            current_quantity = self._apply_splits_between(
                current_quantity, self._positions[-1]["date"], trade.date)
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
        if current_quantity - trade.quantity == 0:
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
            "tsr%": round(tsr * 100, 1),
            "cagr%": round(cagr * 100, 1),
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
    for holding in holdings.values():
        summary = holding.summary()
        if summary["is_current"]:
            current_holdings.append(summary)
        else:
            historical_holdings.append(summary)

    return {
        "current": sorted(current_holdings, key=lambda item: item["latest_buy"], reverse=True),
        "historical": sorted(historical_holdings, key=lambda item: item["latest_sell"], reverse=True),
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
    total_return["twr%"] = round(twr * 100, 1)
    total_return["cagr%"] = round(cagr * 100, 1)
    print(f"\nJG - Jan Grzybek - TWR: {total_return['twr%']}% - CAGR: {total_return['cagr%']}%")
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
        holdings["allocation%"] = {
            "Equities": round(100 * total_equity_value_usd / total_value_usd, 1),
            "Cash & Cash Equivalents": round(100 * total_cash_value_usd / total_value_usd, 1),
        }
        print(f"Equity allocation: {holdings['allocation%']['Equities']}%")
        print(f"Cash allocation: {holdings['allocation%']['Cash & Cash Equivalents']}%\n")
    else:
        holdings["allocation%"] = None

    holdings["top_10"] = None
    weights: dict[str, float] = {}
    for holding in holdings["current"]:
        holding["current_weight%"] = round(100 * holding["current_value_usd"] / total_value_usd, 1)
        weights[holding["ticker"]] = holding["current_weight%"]
        print(f"{holding['ticker']} - {holding['name']} - Weight: {holding['current_weight%']}% - "
              f"TSR: {holding['tsr%']}% - CAGR: {holding['cagr%']}%")
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
              f"TSR: {benchmarks[-1]['tsr%']}% - CAGR: {benchmarks[-1]['cagr%']}%")
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
    --line: #4a5260;
    --accent: #f29a4f;
    --accent-bench: #6ea8d8;
    --positive: #58c97f;
    --negative: #ff6b63;
    /* Card surfaces (holding capsules, JG/benchmark compare
       capsule, ticker strip) sit a clear elevation step above
       the page background so the predominantly dark brand
       wordmarks (Adobe, S&P Global, Baidu, Meta, Salesforce,
       Samsung, ...) gain enough surrounding luminance to be
       legible against the card without resorting to a stark
       white backdrop behind each logo. Light foreground text
       (#e8eaed) on this surface still clears WCAG AA for body
       text (~6.5:1 contrast). */
    --card-bg: #3a424e;
  }
  /* The dashed 0% reference line on the return chart inherits
     ``var(--muted)`` (see the base rule above). In dark mode
     that mid-grey wash blends into the deep page background, so
     swap the stroke to the lighter foreground colour while
     keeping the base opacity / stroke-width. The dashed pattern
     stays elegant but the baseline is now clearly traceable
     across the chart. */
  .return-chart__ref { stroke: var(--fg); }
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
}
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
.ticker__logo {
  width: 48px;
  height: 48px;
  object-fit: contain;
  flex: 0 0 auto;
  opacity: 0.92;
}
@keyframes ticker-scroll {
  from { transform: translateX(0); }
  to { transform: translateX(-50%); }
}
.section { margin-top: 36px; scroll-margin-top: 88px; }
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
  /* Three columns -- start date, separator, end date -- with FIXED
     widths in em units rather than ``max-content``. Every holding
     capsule on the page reaches the same body x-offset (the .holding
     grid pins logo width identically on each card), so locking the
     period tracks to the same fixed widths means the dash column
     and the end-date column line up vertically not just within a
     single card's multi-period stack but across every card on the
     page -- the eye reads the entire holdings list as a tidy column.
     The 7em date track comfortably fits the longest possible
     "Mmm DD, YYYY" rendering ("Sep 30, 2024" etc.) at the
     0.875rem period font size; "Present" (7 chars) is shorter than
     any date so it tucks neatly into the same end-date track.
     The middle track is ``min-content`` (not ``auto``) and the
     grid sets ``justify-content: start`` -- both choices defeat
     CSS Grid's "Expand Stretched auto Tracks" step, which would
     otherwise inflate an ``auto`` middle track to consume the
     ``<ul>``'s leftover horizontal space and push the end-date
     column far to the right on wide viewports. With those two
     guards, leftover container width spills past the last track
     instead of opening a chasm between the dash and the end date.
     Default ``justify-items: start`` leaves a little trailing
     whitespace inside short cells, which is the standard look for
     tabular layouts. */
  display: grid;
  grid-template-columns: 7em min-content 7em;
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
/* Right-align the start-date <time> within its 7em cell so the
   date hugs the dash, leaving a symmetric ~0.5ch gap on each side
   of the dash (instead of the trailing whitespace + column-gap
   asymmetry that left-aligning produced). The <time> element is
   blockified into a grid item, so it stretches to fill the cell;
   text-align: end aligns the visible text to the cell's right
   edge. Cross-card alignment still holds because the cell width
   is fixed at 7em -- only the date's position INSIDE the cell
   changes from start to end. */
.holding__periods li > :first-child { text-align: end; }
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
.holding__stats dt { color: var(--muted); margin: 0; font-weight: 400; }
.holding__stats dd { margin: 0; text-align: right; font-weight: 600; }
.value--positive { color: var(--positive); }
.value--negative { color: var(--negative); }
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
  padding-left: 28px;
  border-left: 1px solid var(--line);
  margin-left: -28px;
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
  .site-nav { width: 100%; }
  .ticker {
    margin: 0 -16px 20px;
    padding: 12px 0;
  }
  .ticker__track { gap: 30px; animation-duration: 45s; }
  .ticker__logo { width: 40px; height: 40px; }
  .section { margin-top: 28px; scroll-margin-top: 108px; }
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
    grid-area: stats;
    justify-content: start;
    column-gap: 12px;
    padding-top: 4px;
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
  .ticker__track { gap: 24px; animation-duration: 35s; }
  .ticker__logo { width: 34px; height: 34px; }
  .holding {
    grid-template-columns: 44px minmax(0, 1fr);
    gap: 10px 12px;
    padding: 12px;
  }
  .holding__logo { max-width: 44px; max-height: 44px; }
  .holding__title { font-size: 0.9375rem; }
  .holding__stats { font-size: 0.875rem; column-gap: 10px; }
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


class Webpage:
    """Builds the JG Investing index page as a single responsive document."""

    def __init__(self):
        self.return_html: str = ""
        self.current: list[str] = []
        self.historical: list[str] = []
        self.allocation_pct: dict[str, float] | None = None
        self.top_10: dict[str, float] | None = None
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
            # still set decode=async + dimensions to keep layout
            # stable while images stream in.
            f'<img class="ticker__logo" src="{html.escape(url)}" alt="" '
            f'title="{html.escape(f"{ticker} - {name}")}" '
            f'decoding="async" width="48" height="48">'
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
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'sha256-{jsonld_hash}' "
            f"'sha256-{hash_clear_hash}' "
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
        #   - a white halo (stroke) around every text run (invisible
        #     against white, provides a legible outline against
        #     dark);
        #   - a soft white card behind the holdings logo strip
        #     (most logos are dark wordmarks that would otherwise
        #     vanish on dark backgrounds).
        TRANSPARENT = (255, 255, 255, 0)
        HALO = (255, 255, 255)
        FG = (17, 17, 17)
        MUTED = (95, 99, 106)
        ACCENT = (230, 125, 34)
        POS = (31, 122, 61)
        NEG = (179, 38, 30)

        # Halo widths tuned per font size so the outline is visible
        # on dark backgrounds without bloating glyphs visibly on
        # light ones.
        HALO_BIG = 5    # 96pt+ display type
        HALO_MED = 3    # 32pt caption
        HALO_SM = 2     # 22pt footer

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
            stroke_width=HALO_BIG, stroke_fill=HALO,
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
            hero_text = f"{cagr_delta:+.1f} pp"
            hero_color = POS if cagr_delta >= 0 else NEG
            label = "Outperformance of "
            label_emph = bench_label or "S&P 500"
            label_tail = " on CAGR"
        else:
            hero_text = f"{cagr:+.1f}%"
            hero_color = POS if cagr >= 0 else NEG
            label = "Annualized return ("
            label_emph = "CAGR"
            label_tail = ")"

        draw.text(
            (pad_l, 210), hero_text, font=f_hero, fill=hero_color,
            stroke_width=HALO_BIG, stroke_fill=HALO,
        )

        # Caption below the hero: "Outperformance of S&P 500 on CAGR"
        # with the benchmark name bolded so the reader's eye lands on
        # the comparison subject.
        cap_y = 388
        draw.text(
            (pad_l, cap_y), label, font=f_caption, fill=MUTED,
            stroke_width=HALO_MED, stroke_fill=HALO,
        )
        label_w = int(draw.textlength(label, font=f_caption))
        draw.text(
            (pad_l + label_w, cap_y), label_emph, font=f_caption_b, fill=FG,
            stroke_width=HALO_MED, stroke_fill=HALO,
        )
        emph_w = int(draw.textlength(label_emph, font=f_caption_b))
        draw.text(
            (pad_l + label_w + emph_w, cap_y),
            label_tail, font=f_caption, fill=MUTED,
            stroke_width=HALO_MED, stroke_fill=HALO,
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
        draw.text(
            (pad_l, H - 40), foot, font=f_foot, fill=MUTED,
            stroke_width=HALO_SM, stroke_fill=HALO,
        )

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
        regardless of the underlying logos' aspect ratios. A subtle
        white card sits behind the row so that the predominantly
        dark logo wordmarks (Adobe, Lam Research, Samsung, ...) stay
        legible when the OG image is composited on a dark surface;
        on white backgrounds the card blends in and is invisible.
        The strip is left untouched when there are no current
        holdings yet (e.g. on the very first build) so the rest of
        the layout still reads cleanly."""
        from PIL import ImageDraw

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

        # Card backdrop. Opaque white with rounded corners; invisible
        # against a white-page bg, visible as a soft pill against a
        # dark bg -- gives the dark logo wordmarks somewhere to live.
        # The padding values are tuned so the card hugs the row of
        # logos with a bit of breathing room on all sides.
        backdrop = ImageDraw.Draw(canvas)
        pad_x = 24
        pad_y = 18
        backdrop.rounded_rectangle(
            (
                offset_x - pad_x,
                y - pad_y,
                offset_x + used_w + pad_x,
                y + cell_h + pad_y,
            ),
            radius=20,
            fill=(255, 255, 255, 255),
        )

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
                f'{twr_delta:+.1f} pp Total Return</span>'
                '<span class="returns-compare__delta-sep" '
                'aria-hidden="true">&middot;</span>'
                f'<span class="returns-compare__delta-metric '
                f'{_value_class(cagr_delta)}">'
                f'{cagr_delta:+.1f} pp CAGR</span>'
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
            stat_html.append(
                f'<dt>{html.escape(label)}</dt>'
                f'<dd class="{_value_class(value)}">{value}%</dd>'
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

    def _build_holding_card(self, holding) -> str:
        stats: list[tuple[str, str, float | None]] = [
            ("TSR:", f"{holding['tsr%']}%", holding["tsr%"]),
        ]
        if holding["cagr%"] > CAGR_TBA_THRESHOLD:
            stats.append(("CAGR:", "TBA", None))
        else:
            stats.append(("CAGR:", f"{holding['cagr%']}%", holding["cagr%"]))
        if holding["is_current"]:
            assert holding["current_weight%"] is not None
            stats.append(("Weight:", f"{holding['current_weight%']}%", None))

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
            stat_parts.append(
                f'<dt>{html.escape(label)}</dt>'
                f'<dd{attr}>{html.escape(value)}</dd>'
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
            width = round(value / denom * 100, 2) if scale_to_max else value
            row_html.append(
                '<div class="bars__row">'
                f'<div class="bars__label">{html.escape(str(label))}</div>'
                f'<div class="bars__value">{value}%</div>'
                f'<div class="bars__track"><div class="bars__fill" style="width: {width}%"></div></div>'
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
                f'{delta_pp:+.1f} pp</span>'
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

        plot_html = (
            f'<div class="return-chart__plot">{"".join(svg_lines)}{delta_html}</div>'
        )
        return f'<figure class="return-chart">{plot_html}{legend_html}{caption}</figure>'


def generate_webpage(total_return, benchmarks, holdings):
    webpage = Webpage()
    webpage.add_return(total_return, benchmarks)
    webpage.add_allocations(holdings.get("allocation%"), holdings.get("top_10"))
    for holding in holdings["current"]:
        webpage.add_holding(holding)
    for holding in holdings["historical"]:
        webpage.add_holding(holding)
    webpage.save()


def main():
    transactions, valuations, cash = pull_data()
    holdings = get_holdings(transactions)
    total_return = calc_twr(valuations, summarize(holdings, cash))
    benchmarks = get_benchmarks(total_return["history"])
    generate_webpage(total_return, benchmarks, holdings)


if __name__ == "__main__":
    main()
