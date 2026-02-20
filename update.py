import os
import gspread
import requests
import numpy as np
import yfinance as yf
import plotly.graph_objects as go

from datetime import datetime
from plotly.subplots import make_subplots
from scipy.interpolate import PchipInterpolator


LOGOS_ADDRESS = "https://raw.githubusercontent.com/jan-grzybek/investing/refs/heads/main/logos/"
WITHHOLDING_TAX_RATE = 0.15


class ExchangeRate:
    def __init__(self):
        self._rates = {}

    def __call__(self, currency):
        if currency == "USD":
            return 1.
        try:
            return self._rates[currency]
        except KeyError:
            self._rates[currency] = yf.Ticker(f"{currency}USD=X").info["regularMarketPrice"]
            return self._rates[currency]

exchange_rate = ExchangeRate()


class Trade:
    def __init__(self, date, ticker, quantity, price, action):
        self.date = date
        self.ticker = ticker
        self.quantity = quantity
        self.price = price
        self.action = action


def combine_and_sort(transactions):
    trades = {}
    for transaction in transactions:
        assert transaction["action"] in ["BUY", "SELL"], f"Action unknown: {transaction['action']}"
        if transaction["ticker"] not in trades.keys():
            if transaction["action"] == "BUY":
                trades[transaction["ticker"]] = {transaction["date"]: {"BUY": [transaction], "SELL": []}}
            else:
                trades[transaction["ticker"]] = {transaction["date"]: {"BUY": [], "SELL": [transaction]}}
        elif transaction["date"] not in trades[transaction["ticker"]].keys():
            if transaction["action"] == "BUY":
                trades[transaction["ticker"]][transaction["date"]] = {"BUY": [transaction], "SELL": []}
            else:
                trades[transaction["ticker"]][transaction["date"]] = {"BUY": [], "SELL": [transaction]}
        else:
            if transaction["action"] == "BUY":
                trades[transaction["ticker"]][transaction["date"]]["BUY"].append(transaction)
            else:
                trades[transaction["ticker"]][transaction["date"]]["SELL"].append(transaction)

    _trades = []
    for ticker, transactions in trades.items():
        for date, _transactions in transactions.items():
            for action in ["BUY", "SELL"]:
                __transactions = _transactions[action]
                if len(__transactions) == 0:
                    continue
                quantity = 0
                value = 0.
                for transaction in __transactions:
                    assert ticker == transaction["ticker"]
                    assert date == transaction["date"]
                    quantity += transaction["quantity"]
                    value += transaction["quantity"] * transaction["price_per_share"]
                _trades.append(Trade(
                    datetime.strptime(date, "%d-%m-%Y"), ticker, quantity, value / quantity, action))

    return sorted(_trades, key=lambda item: item.date)


class Holding:
    def __init__(self, ticker):
        self._ticker = yf.Ticker(ticker)
        self._info = self._ticker.get_info()
        self._splits, self._dividends = self._get_splits_dividends()
        self._positions = []
        self._periods = []
        self._inflows = []
        self._outflows = []

    def _get_splits_dividends(self):
        splits = []
        splits_acc = []
        for date, split in self._ticker.splits.items():
            date = datetime.strptime(date.__str__().split()[0], "%Y-%m-%d")
            splits.append({"date": date, "split": split})
            for _split in splits_acc:
                _split["split"] *= split
            splits_acc.append({"date": date, "split": split})
        # readjust dividends for splits
        dividends = []
        split_idx = 0
        for date, dividend in self._ticker.get_dividends().items():
            date = datetime.strptime(date.__str__().split()[0], "%Y-%m-%d")
            for split in splits_acc[split_idx:]:
                if split["date"] >= date:
                    dividend *= split["split"]
                    break
                split_idx += 1
            dividends.append({"date": date, "dividend": dividend})
        return splits, dividends

    def buy(self, trade: Trade):
        try:
            current_quantity = self._positions[-1]["quantity"]
        except IndexError:
            current_quantity = 0
        if current_quantity == 0:
            self._periods.append({"start": trade.date, "end": None})
        elif trade.date > self._positions[-1]["date"]:
            for split in self._splits:
                assert trade.date != split["date"]
                if trade.date <= split["date"]:
                    break
                assert self._positions[-1]["date"] != split["date"]
                if split["date"] > self._positions[-1]["date"]:
                    current_quantity = int(current_quantity * split["split"])
        self._inflows.append({
            "date": trade.date,
            "value": trade.quantity * trade.price
        })
        if len(self._positions) > 0 and self._positions[-1]["date"] == trade.date:
            self._positions[-1]["quantity"] += trade.quantity
        else:
            self._positions.append({
                "date": trade.date,
                "quantity": current_quantity + trade.quantity
            })

    def sell(self, trade: Trade):
        current_quantity = self._positions[-1]["quantity"]
        if trade.date > self._positions[-1]["date"]:
            for split in self._splits:
                assert trade.date != split["date"]
                if trade.date <= split["date"]:
                    break
                assert self._positions[-1]["date"] != split["date"]
                if split["date"] > self._positions[-1]["date"]:
                    current_quantity = int(current_quantity * split["split"])
        if current_quantity - trade.quantity == 0:
            self._periods[-1]["end"] = trade.date
        self._outflows.append({
            "date": trade.date,
            "value": trade.quantity * trade.price
        })
        if self._positions[-1]["date"] == trade.date:
            self._positions[-1]["quantity"] -= trade.quantity
        else:
            self._positions.append({
                "date": trade.date,
                "quantity": current_quantity - trade.quantity
            })

    def _add_dividends(self):
        outflows = [outflow for outflow in self._outflows]
        position_idx = 0
        split_idx = 0
        for dividend in self._dividends:
            if position_idx >= len(self._positions):
                break
            while True:
                position = self._positions[position_idx]
                if dividend["date"] > position["date"]:
                    if (position_idx + 1 < len(self._positions) and
                            self._positions[position_idx+1]["date"] < dividend["date"]):
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
                            "value": quantity * dividend["dividend"] * (1. - WITHHOLDING_TAX_RATE)
                        })
                        break
                    else:
                        break
                else:
                    break
        return outflows

    def summary(self):
        outflows = self._add_dividends()
        tsr = 1.
        total_ownership_length = 0
        for period in self._periods:
            start = period["start"]
            if period["end"] is None:
                end = datetime.today()
                outflows.append({
                    "date": end,
                    "value": self._positions[-1]["quantity"] * self._info["regularMarketPrice"]
                })
            else:
                end = period["end"]
            length = max((end - start).days, 1)
            total_ownership_length += length
            gain = 0.
            avg_capital = 0.
            for inflow in self._inflows:
                if start <= inflow["date"] < end:
                    gain -= inflow["value"]
                    avg_capital += (max((end - inflow["date"]).days, 1) / length) * inflow["value"]
            for outflow in outflows:
                if start < outflow["date"] <= end:
                    gain += outflow["value"]
                    avg_capital -= ((end - outflow["date"]).days / length) * outflow["value"]
            tsr *= (1. + gain / avg_capital)
        cagr = tsr ** (365.25 / total_ownership_length) - 1.
        tsr -= 1.
        current_value_usd = (self._positions[-1]["quantity"] * self._info["regularMarketPrice"] *
                             exchange_rate(self._info["currency"]))
        return {
            "ticker": f"{self._info['exchange']}:{self._info['symbol']}",
            "name": self._info['longName'],
            "tsr%": round(tsr * 100, 1),
            "cagr%": round(cagr * 100, 1),
            "is_current": self._positions[-1]["quantity"] > 0,
            "current_weight%": None,
            "current_value_usd": current_value_usd,
            "periods": list(reversed(self._periods)),
            "latest_buy": self._inflows[-1]["date"],
            "latest_sell": self._outflows[-1]["date"] if len(self._outflows) > 0 else None
        }


def pull_data():
    gc = gspread.service_account(filename="/tmp/gsheet_creds.json")
    sh = gc.open_by_key(os.environ["GSHEET_ID"])
    transactions = []
    for transaction in sh.worksheet("Equities").get_all_values()[2:]:
        if transaction[6] not in ["Y", "YES", "y", "yes"]:
            continue
        if transaction[5] in ["B", "BUY", "b", "buy"]:
            action = "BUY"
        elif transaction[5] in ["S", "SELL", "s", "sell"]:
            action = "SELL"
        else:
            assert False
        transactions.append({
            "date": transaction[1],
            "ticker": transaction[2],
            "quantity": int(transaction[3].replace(",", "")),
            "price_per_share":  float(transaction[4].replace(",", "")),
            "action": action
        })
    valuations = []
    for valuation in sh.worksheet("Return").get_all_values()[2:]:
        if valuation[4] not in ["Y", "YES", "y", "yes"]:
            continue
        valuations.append({
            "date": datetime.strptime(valuation[1], "%d-%m-%Y"),
            "value": float(valuation[2].replace(",", "")),
            "flow": float(valuation[3].replace(",", ""))
        })
    cash = []
    for currency in sh.worksheet("Cash & Cash Equivalents").get_all_values()[2:]:
        if currency[4] not in ["Y", "YES", "y", "yes"]:
            continue
        cash.append({
            "currency_code": currency[2],
            "amount": float(currency[3].replace(",", ""))
        })
    return transactions, valuations, cash


def get_holdings(transactions):
    trades = combine_and_sort(transactions)

    holdings = {}
    for trade in trades:
        if trade.ticker not in holdings.keys():
            holdings[trade.ticker] = Holding(trade.ticker)
        assert trade.action in ["BUY", "SELL"]
        if trade.action == "BUY":
            holdings[trade.ticker].buy(trade)
        else:
            holdings[trade.ticker].sell(trade)

    current_holdings = []
    historical_holdings = []
    for holding in holdings.values():
        summary = holding.summary()
        if summary["is_current"] is True:
            current_holdings.append(summary)
        else:
            historical_holdings.append(summary)

    return {"current": sorted(current_holdings, key=lambda item: item["latest_buy"], reverse=True),
            "historical": sorted(historical_holdings, key=lambda item: item["latest_sell"], reverse=True)}


class Webpage:
    def __init__(self):
        self.desktop_return = ""
        self.desktop_current = []
        self.desktop_historical = []
        self.mobile_return = ""
        self.mobile_current = []
        self.mobile_historical = []

    def save(self):
        update_date = datetime.now().strftime("%b %-d, %Y")
        txt = []
        txt.append('<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n'
                   '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
                   '<meta name="description" content="Overview of my investment portfolio, including historical '
                   'holdings with TSR, CAGR, and ownership periods.">\n<title>JG Investing</title>\n'
                   '<link rel="icon" type="image/png" href="favicon.png">\n'
                   '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
                   '<link rel="icon" href="favicon.ico">\n'
                   '<meta property="og:title" content="JG Investing">\n'
                   '<meta property="og:description" content="Overview of my investment portfolio, including historical '
                   'holdings with TSR, CAGR, and ownership periods.">\n'
                   '<meta property="og:image" content="https://raw.githubusercontent.com/jan-grzybek/investing/refs/heads/main/apple-touch-icon.png">\n'
                   '<meta property="og:url" content="https://jan-grzybek.github.io/investing/">\n'
                   '<meta property="og:type" content="website">')
        txt.append('<style>\n.desktop-version {display: block;}\n.mobile-version {display: none;}\n'
                   '@media (max-width: 600px) {\n.desktop-version {display: none;}\n'
                   '.mobile-version {display: block;}\n}\nbody {box-sizing: border-box; padding-left: 20px; '
                   'padding-right: 20px;}\n.grid-return {\ndisplay: grid;\ngrid-template-columns: '
                   '70px 90px;\ncolumn-gap: 15px; \nrow-gap: 2px;\n}\n.left-col {\ntext-align: left;\n'
                   '}\n.right-col {\ntext-align: right;\n}\n</style>\n</head>\n<body>')

        txt.append('<div class="desktop-version">')
        txt.append('<br>\n<div style="font-size: 26px; font-weight: bold;">\nAll-time performance\n</div>\n'
                   '<hr style="height: 1px; background-color: black;">\n<br>\n<div style="padding-left: 30px;">')
        txt.append(self.desktop_return)
        txt.append('</div>\n<br>')
        if len(self.desktop_current) > 0:
            txt.append('<div style="font-size: 26px; font-weight: bold;">\nCurrent holdings\n</div>\n'
                       '<hr style="height: 1px; background-color: black;">')
            # equities, fixed income, cash & cash equivalents, other / alternatives
            txt.append('<div style="padding-left: 20px;">\n'
                       '<img src="https://media.githubusercontent.com/media/jan-grzybek/investing/refs/heads/main/allocation.svg"/>\n'
                       '<br>')
            txt.append('<div style="font-size: 20px; font-weight: bold;">\nEquities:\n</div>\n<hr>\n'
                       '<div style="padding-left: 20px;">\n'
                       '<div style="font-size: 14px;">\nTop equities by weight in the total portfolio:\n</div>\n'
                       '<img src="https://media.githubusercontent.com/media/jan-grzybek/investing/refs/heads/main/equity_allocation.svg"/>\n'
                       '<hr>\n<br>')
            txt.append('\n<br>\n<hr>\n<br>\n'.join(self.desktop_current))
            txt.append('</div>\n</div>\n<br>\n<br>\n<br>')
        txt.append('<div style="font-size: 26px; font-weight: bold;">\nHistorical holdings\n</div>\n'
                   '<hr style="height: 1px; background-color: black;">\n<br>\n<div style="padding-left: 20px;">')
        txt.append('<div style="font-size: 20px; font-weight: bold;">\nEquities:\n</div>\n<hr>\n<div style="padding-left: 20px;">')
        txt.append('\n<br>\n<hr>\n<br>\n'.join(self.desktop_historical))
        txt.append('</div>\n</div>\n<br>\n<br>\n<br>\n<div style="font-size: 14px;">\n'
                   'All TSR figures were calculated using the modified Dietz method, with dividends assumed to be '
                   'subject to a 15% withholding tax and cashed out.\n<br>\n'
                   'For informational purposes only. Nothing contained herein should be construed as a recommendation '
                   'to buy, sell or hold any security or pursue any investment strategy.\n<br>\n'
                   'Logos are trademarks of their respective owners and are used for identification purposes only. '
                   'The latest stock prices and dividend data used in the calculations were obtained from '
                   '<a href="https://finance.yahoo.com/markets/stocks/trending/" '
                   'title="Yahoo Finance">Yahoo Finance</a>.\n<br>\n<br>\n'
                   f'Updated on {update_date}\n</div>\n<br>')

        txt.append('</div>\n<div class="mobile-version">')
        txt.append('<br>\n<div style="font-size: 26px; font-weight: bold;">\nAll-time performance\n</div>\n'
                   '<hr style="height: 1px; background-color: black;">\n<div style="padding-left: 10px;">')
        txt.append(self.mobile_return)
        txt.append('</div>\n<br>')
        if len(self.mobile_current) > 0:
            txt.append('<div style="font-size: 26px; font-weight: bold;">\nCurrent holdings\n</div>\n'
                       '<hr style="height: 1px; background-color: black;">')
            # equities, fixed income, cash & cash equivalents, other / alternatives
            txt.append('<div style="padding-left: 10px;">\n'
                       '<img src="https://media.githubusercontent.com/media/jan-grzybek/investing/refs/heads/main/allocation.svg" width="250"/>\n'
                       '<br>')
            txt.append('<div style="font-size: 20px; font-weight: bold;">\nEquities:\n</div>\n<hr>\n'
                       '<div style="padding-left: 10px;">\n'
                       '<div style="font-size: 14px;">\nTop equities by weight in the total portfolio:\n</div>\n'
                       '<img src="https://media.githubusercontent.com/media/jan-grzybek/investing/refs/heads/main/equity_allocation.svg" width="250"/>\n'
                       '<hr>')
            txt.append('\n<hr>\n'.join(self.mobile_current))
            txt.append('</div>\n</div>\n<br>\n<br>')
        txt.append('<div style="font-size: 26px; font-weight: bold;">\nHistorical holdings\n</div>\n'
                   '<hr style="height: 1px; background-color: black;">\n<div style="padding-left: 10px;">')
        txt.append('<div style="font-size: 20px; font-weight: bold;">\nEquities:\n</div>\n<hr>\n'
                   '<div style="padding-left: 10px;">')
        txt.append('\n<hr>\n'.join(self.mobile_historical))
        txt.append('</div>\n</div>\n<br>\n<br>\n<div style="font-size: 14px;">\n'
                   'All TSR figures were calculated using the modified Dietz method, with dividends assumed to be '
                   'subject to a 15% withholding tax and cashed out.\n<br>\n<br>\n'
                   'For informational purposes only. Nothing contained herein should be construed as a recommendation '
                   'to buy, sell or hold any security or pursue any investment strategy.\n<br>\n<br>\n'
                   'Logos are trademarks of their respective owners and are used for identification purposes only. The '
                   'latest stock prices and dividend data used in the calculations were obtained from '
                   '<a href="https://finance.yahoo.com/markets/stocks/trending/" title="Yahoo Finance">'
                   'Yahoo Finance</a>.\n<br>\n<br>\n'
                   f'Updated on {update_date}\n</div>\n<br>')
        txt.append('</div>\n</body>\n</html>\n')
        with open("index.html", "w") as f:
            f.write("\n".join(txt))

    def _get_logo_url(self, ticker):
        for extension in [".svg", ".png", ".jpg"]:
            url = LOGOS_ADDRESS + ticker.replace(":", "%3A") + extension
            response = requests.head(url)
            if response.status_code == 200:
                return url
        else:
            return LOGOS_ADDRESS + "courage.png"

    def add_return_desktop(self, total_return, benchmarks):
        lines = []
        lines.append('<div style="display: flex; align-items: center;">')
        lines.append(f'<img src="https://raw.githubusercontent.com/jan-grzybek/investing/refs/heads/main/logos/courage.png" width="90"/>')
        lines.append('<div style="padding-left: 46px;">')
        lines.append('<div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">')
        lines.append('JG - Jan Grzybek')
        lines.append('</div>')
        lines.append('<div class="grid-return">')
        lines.append('<div class="left-col">TWR:</div>')
        lines.append(f'<div class="right-col">{total_return["twr%"]}%</div>')
        lines.append('<div class="left-col">CAGR:</div>')
        lines.append(f'<div class="right-col">{total_return["cagr%"]}%</div>')
        lines.append('</div>')
        lines.append('<div style="margin-top: 8px; display: grid; grid-template-columns: '
                     'max-content max-content max-content; column-gap: 20px; row-gap: 2px;">')
        lines.append(f'<div>{total_return["start_date"].strftime("%b %d, %Y")}</div><div>-</div><div>Present</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('<br>')
        lines.append('<div style="font-size: 14px;">')
        lines.append('Time-weighted return (TWR) calculated excluding the impact of capital gains taxes, but including '
                     'the effects of withholding taxes and transaction costs.')
        lines.append('</div>\n<br>\n<div style="font-size: 20px; font-weight: bold;">\nBenchmark:\n</div>')
        for benchmark in benchmarks:
            lines.append('<hr>')
            lines.append('<br>')
            lines.append('<div style="display: flex; align-items: center;">')
            lines.append(f'<img src="{self._get_logo_url(benchmark["ticker"])}" width="100"/>')
            lines.append('<div style="padding-left: 36px;">')
            lines.append('<div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">')
            lines.append(f'{benchmark["ticker"]} - {benchmark["name"]}')
            lines.append('</div>')
            lines.append('<div class="grid-return">')
            lines.append('<div class="left-col">TSR:</div>')
            lines.append(f'<div class="right-col">{benchmark["tsr%"]}%</div>')
            lines.append('<div class="left-col">CAGR:</div>')
            lines.append(f'<div class="right-col">{benchmark["cagr%"]}%</div>')
            lines.append('</div>')
            lines.append('<div style="margin-top: 8px; display: grid; grid-template-columns: '
                         'max-content max-content max-content; column-gap: 20px; row-gap: 2px;">')
            lines.append(f'<div>{benchmark["periods"][0]["start"].strftime("%b %d, %Y")}'
                         f'</div><div>-</div><div>Present</div>')
            lines.append('</div>')
            lines.append('</div>')
            lines.append('</div>')
            lines.append('<br>')
        lines.append('<br>')
        lines.append('<img src="https://media.githubusercontent.com/media/jan-grzybek/investing/refs/heads/main/return.svg" width="380"/>')
        lines.append('<br>\n<br>')
        self.desktop_return = "\n".join(lines)

    def add_holding_desktop(self, holding):
        lines = []
        lines.append('<div style="display: flex; align-items: center;">')
        lines.append(f'<img src="{self._get_logo_url(holding["ticker"])}" width="100"/>')
        lines.append('<div style="padding-left: 36px;">')
        lines.append('<div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">')
        lines.append(f'{holding["ticker"]} - {holding["name"]}')
        lines.append('</div>')
        lines.append('<div class="grid-return">')
        lines.append('<div class="left-col">TSR:</div>')
        lines.append(f'<div class="right-col">{holding["tsr%"]}%</div>')
        lines.append('<div class="left-col">CAGR:</div>')
        lines.append(f'<div class="right-col">{holding["cagr%"]}%</div>')
        if holding["is_current"] is True:
            assert holding["current_weight%"] is not None
            lines.append('<div class="left-col">Weight:</div>')
            lines.append(f'<div class="right-col">{holding["current_weight%"]}%</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('<div style="padding-left: 136px; margin-top: 8px; display: grid; grid-template-columns: '
                             'max-content max-content max-content; column-gap: 20px; row-gap: 2px;">')
        for period in holding["periods"]:
            if period["end"] is None:
                end = "Present"
            else:
                end = period["end"].strftime("%b %d, %Y")
            lines.append(f'<div>{period["start"].strftime("%b %d, %Y")}</div><div>-</div><div>{end}</div>')
        lines.append('</div>')
        if holding["is_current"] is True:
            self.desktop_current.append("\n".join(lines))
        else:
            self.desktop_historical.append("\n".join(lines))

    def add_return_mobile(self, total_return, benchmarks):
        lines = []
        lines.append('<div style="display: flex; align-items: center;">')
        lines.append(f'<img src="https://raw.githubusercontent.com/jan-grzybek/investing/refs/heads/main/logos/courage.png" width="70"/>')
        lines.append('<div style="padding-left: 24px;">')
        lines.append('<div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">')
        lines.append('JG - Jan Grzybek')
        lines.append('</div>')
        lines.append('<div class="grid-return">')
        lines.append('<div class="left-col">TWR:</div>')
        lines.append(f'<div class="right-col">{total_return["twr%"]}%</div>')
        lines.append('<div class="left-col">CAGR:</div>')
        lines.append(f'<div class="right-col">{total_return["cagr%"]}%</div>')
        lines.append('</div>')
        lines.append('<div style="margin-top: 8px; display: grid; grid-template-columns: '
                     'max-content max-content max-content; column-gap: 15px; row-gap: 2px;">')
        lines.append(f'<div>{total_return["start_date"].strftime("%b %d, %Y")}</div><div>-</div><div>Present</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('<br>')
        lines.append('<div style="font-size: 14px;">')
        lines.append('Time-weighted return (TWR) calculated excluding the impact of capital gains taxes, but including '
                     'the effects of withholding taxes and transaction costs.')
        lines.append('</div>\n<br>\n<div style="font-size: 20px; font-weight: bold;">\nBenchmark:\n</div>')
        for benchmark in benchmarks:
            lines.append('<hr>')
            lines.append('<div style="display: flex; align-items: center;">')
            lines.append(f'<img src="{self._get_logo_url(benchmark["ticker"])}" width="70"/>')
            lines.append('<div style="padding-left: 24px;">')
            lines.append('<div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">')
            lines.append(f'{benchmark["ticker"]} - {benchmark["name"]}')
            lines.append('</div>')
            lines.append('<div class="grid-return">')
            lines.append('<div class="left-col">TSR:</div>')
            lines.append(f'<div class="right-col">{benchmark["tsr%"]}%</div>')
            lines.append('<div class="left-col">CAGR:</div>')
            lines.append(f'<div class="right-col">{benchmark["cagr%"]}%</div>')
            lines.append('</div>')
            lines.append('<div style="margin-top: 8px; display: grid; grid-template-columns: '
                         'max-content max-content max-content; column-gap: 15px; row-gap: 2px;">')
            lines.append(f'<div>{benchmark["periods"][0]["start"].strftime("%b %d, %Y")}'
                         f'</div><div>-</div><div>Present</div>')
            lines.append('</div>')
            lines.append('</div>')
            lines.append('</div>')
        lines.append('<br>\n<br>')
        lines.append('<img src="https://media.githubusercontent.com/media/jan-grzybek/investing/refs/heads/main/return.svg" width="320"/>')
        lines.append('<br>\n<br>')
        self.mobile_return = "\n".join(lines)

    def add_holding_mobile(self, holding):
        lines = []
        lines.append('<div style="display: flex; align-items: center;">')
        lines.append(f'<img src="{self._get_logo_url(holding["ticker"])}" width="70"/>')
        lines.append('<div style="padding-left: 24px;">')
        lines.append('<div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">')
        lines.append(f'{holding["ticker"]} - {holding["name"]}')
        lines.append('</div>')
        lines.append('<div class="grid-return">')
        lines.append('<div class="left-col">TSR:</div>')
        lines.append(f'<div class="right-col">{holding["tsr%"]}%</div>')
        lines.append('<div class="left-col">CAGR:</div>')
        lines.append(f'<div class="right-col">{holding["cagr%"]}%</div>')
        if holding["is_current"] is True:
            assert holding["current_weight%"] is not None
            lines.append('<div class="left-col">Weight:</div>')
            lines.append(f'<div class="right-col">{holding["current_weight%"]}%</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('</div>')
        lines.append('<div style="padding-left: 94px; margin-top: 8px; display: grid; grid-template-columns: '
                             'max-content max-content max-content; column-gap: 15px; row-gap: 2px;">')
        for period in holding["periods"]:
            if period["end"] is None:
                end = "Present"
            else:
                end = period["end"].strftime("%b %d, %Y")
            lines.append(f'<div>{period["start"].strftime("%b %d, %Y")}</div><div>-</div><div>{end}</div>')
        lines.append('</div>')
        if holding["is_current"] is True:
            self.mobile_current.append("\n".join(lines))
        else:
            self.mobile_historical.append("\n".join(lines))

    def add_holding(self, holding):
        self.add_holding_desktop(holding)
        self.add_holding_mobile(holding)

    def add_return(self, total_return, benchmarks):
        self.add_return_desktop(total_return, benchmarks)
        self.add_return_mobile(total_return, benchmarks)


def generate_webpage(total_return, benchmarks, holdings):
    webpage = Webpage()
    webpage.add_return(total_return, benchmarks)
    for holding in holdings["current"]:
        webpage.add_holding(holding)
    for holding in holdings["historical"]:
        webpage.add_holding(holding)
    webpage.save()


def calc_twr(valuations, current_value):
    if len(valuations) == 0:
        return {"start_date": datetime.today(), "history": [], "twr%": 0., "cagr%": 0.}
    valuations = sorted(valuations, key=lambda item: item["date"])
    total_return = {
        "start_date": valuations[0]["date"],
        "history": []
    }
    start_value = valuations[0]["value"] + valuations[0]["flow"]
    twr = 1.
    total_return["history"].append((valuations[0]["date"], twr))
    for valuation in valuations[1:]:
        twr *= (valuation["value"] / start_value)
        start_value = valuation["value"] + valuation["flow"]
        total_return["history"].append((valuation["date"], twr))
    twr *= (current_value / start_value)
    total_return["history"].append((datetime.today(), twr))
    cagr = twr ** (365.25 / max((datetime.today() - total_return["start_date"]).days, 1)) - 1.
    twr -= 1.
    total_return["twr%"] = round(twr * 100, 1)
    total_return["cagr%"] = round(cagr * 100, 1)
    print(f"\nJG - Jan Grzybek - TWR: {total_return['twr%']}% - CAGR: {total_return['cagr%']}%")
    return total_return


def summarize(holdings, cash):
    total_equity_value_usd = 0.
    total_cash_value_usd = 0.
    total_value_usd = 0.
    for holding in holdings["current"]:
        assert holding["current_value_usd"] > 0.
        total_equity_value_usd += holding["current_value_usd"]
        total_value_usd += holding["current_value_usd"]
    for currency in cash:
        cash_value_usd = currency["amount"] * exchange_rate(currency["currency_code"])
        total_cash_value_usd += cash_value_usd
        total_value_usd += cash_value_usd

    if total_value_usd > 0.:
        holdings["allocation%"] = {}
        holdings["allocation%"]["Equities"] = round(100 * total_equity_value_usd / total_value_usd, 1)
        holdings["allocation%"]["Cash & Cash Equivalents"] = round(100 * total_cash_value_usd / total_value_usd, 1)
        print(f"Equity allocation: {holdings['allocation%']['Equities']}%")
        print(f"Cash allocation: {holdings['allocation%']['Cash & Cash Equivalents']}%\n")
    else:
        holdings["allocation%"] = None

    holdings["top_10"] = None
    weights = {}
    for holding in holdings["current"]:
        holding["current_weight%"] = round(100 * holding["current_value_usd"] / total_value_usd, 1)
        weights[holding['ticker']] = holding["current_weight%"]
        print(f"{holding['ticker']} - {holding['name']} - Weight: {holding['current_weight%']}% - "
              f"TSR: {holding['tsr%']}% - CAGR: {holding['cagr%']}%")
    if len(weights) > 0:
        weights = sorted(weights.items(), key=lambda item: item[1], reverse=True)
        if len(weights) > 11:
            holdings["top_10"] = dict(weights[:10] + [("Other equities", sum([w[1] for w in weights[10:]]))])
        else:
            holdings["top_10"] = dict(weights)

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
            "BUY")
        )
        summary = holding.summary()

        history = holding._ticker.history(start=start_date_str, interval="1d", auto_adjust=True)
        start_price = history["Open"].iloc[0]
        summary["history"] = [(start_date, 1.)]
        ref_idx = 1
        for idx, row in enumerate(history.itertuples()):
            ref_date = total_return_history[ref_idx][0]
            date = row.Index.to_pydatetime()
            if date.date() < ref_date.date():
                continue
            elif date.date() == ref_date.date():
                summary["history"].append((ref_date, float(history["Close"].iloc[idx] / start_price)))
                ref_idx += 1
            else:
                summary["history"].append((ref_date, float(history["Close"].iloc[idx-1] / start_price)))
                ref_idx += 1
                ref_date = total_return_history[ref_idx][0]
                if date.date() == ref_date.date():
                    summary["history"].append((ref_date, float(history["Close"].iloc[idx] / start_price)))
                    ref_idx += 1
        if len(summary["history"]) < len(total_return_history):
            summary["history"].append((total_return_history[-1][0], float(history["Close"].iloc[-1] / start_price)))
        assert len(summary["history"]) == len(total_return_history)

        benchmarks.append(summary)
        print(f"{benchmarks[-1]['ticker']} - {benchmarks[-1]['name']} - "
              f"TSR: {benchmarks[-1]['tsr%']}% - CAGR: {benchmarks[-1]['cagr%']}%")
    return benchmarks


def generate_horizontal_bar(data, chart_name, color):
    if data is None:
        return

    subplots = make_subplots(
        rows=len(data),
        cols=1,
        subplot_titles=list(data.keys()),
        shared_xaxes=True,
        print_grid=False
    )
    subplots["layout"].update(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=14),
        showlegend=False
    )
    for i, d in enumerate(data.items()):
        subplots.add_trace(dict(
            type="bar",
            orientation="h",
            y=[d[0]],
            x=[d[1]],
            text=[f"{d[1]}%"],
            hoverinfo="text",
            textposition="auto",
            textfont_size=10,
            marker=dict(color=color),
        ), i + 1, 1)
    for x in subplots["layout"]["annotations"]:
        x["x"] = 0
        x["xanchor"] = 'left'
        x["align"] = 'left'
        x["font"] = dict(size=14)
    for axis in subplots["layout"]:
        if axis.startswith("yaxis") or axis.startswith("xaxis"):
            subplots["layout"][axis]["visible"] = False
    subplots["layout"]["margin"] = {"l": 0, "r": 0, "t": 30, "b": 10}
    subplots["layout"]["height"] = 50 * len(data) + 15
    subplots["layout"]["width"] = 300
    subplots.write_image(f"{chart_name}.svg")


def generate_return_plot(total_return, benchmarks):
    def interpolate(values):
        return np.exp(PchipInterpolator(time, np.log(values))(time_dense))

    time = [0]
    return_benchmarks = {benchmark["ticker"]: [1.] for benchmark in benchmarks}
    return_jg = [1.]
    start_date = total_return["history"][0][0]
    for date, value in total_return["history"][1:]:
        time.append(int((date - start_date).days))
        return_jg.append(value)
    for benchmark in benchmarks:
        for _, value in benchmark["history"][1:]:
            return_benchmarks[benchmark["ticker"]].append(value)
    time = np.array(time)
    time_dense = np.linspace(time.min(), time.max(), 800)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=time_dense, y=interpolate(np.array(return_jg)),
        marker=dict(color="#e67d22"), mode="lines", name="JG", line=dict(width=6)))
    for k, v in return_benchmarks.items():
        try:
            k = {"LSE:VUAA.L": f"S&P 500{5*' '}"}[k]
        except KeyError:
            pass
        fig.add_trace(go.Scatter(
            x=time_dense, y=interpolate(np.array(v)),
            marker=dict(color="#1f4e79"), mode="lines", name=k, line=dict(width=6)))
    fig["layout"]["width"] = 800
    fig["layout"]["height"] = 400
    fig["layout"]["margin"] = {"l": 0, "r": 0, "t": 0, "b": 0}
    fig["layout"]["xaxis"] = dict(showticklabels=False, showgrid=False, showline=False, zeroline=False, title="Time")
    fig["layout"]["yaxis"] = dict(showticklabels=False, showgrid=False, showline=False, zeroline=False, title="Return")
    fig["layout"]["font"] = dict(size=24)
    fig["layout"]["legend"]["font"] = dict(size=30)
    fig.add_hline(y=1.0, line_width=4, opacity=0.7, line_dash="dash")
    fig["layout"].update(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    fig.write_image("return.svg")

def generate_charts(holdings, total_return, benchmarks):
    generate_return_plot(total_return, benchmarks)
    generate_horizontal_bar(holdings["top_10"], "equity_allocation", "#e67d22")
    generate_horizontal_bar(holdings["allocation%"], "allocation", "#1f4e79")


def main():
    transactions, valuations, cash = pull_data()
    holdings = get_holdings(transactions)
    total_return = calc_twr(valuations, summarize(holdings, cash))
    benchmarks = get_benchmarks(total_return["history"])
    generate_charts(holdings, total_return, benchmarks)
    generate_webpage(total_return, benchmarks, holdings)


if __name__ == "__main__":
    main()
