import gspread
import requests
import yfinance as yf
from datetime import datetime

LOGOS_ADDRESS = "https://raw.githubusercontent.com/jan-grzybek/investing/refs/heads/main/logos/"
WITHHOLDING_TAX_RATE = 0.15


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
        self._dividends = self._ticker.get_dividends()
        self._positions = []
        self._periods = []
        self._inflows = []
        self._outflows = []

    def buy(self, trade: Trade):
        try:
            current_quantity = self._positions[-1]["quantity"]
        except IndexError:
            current_quantity = 0
        if current_quantity == 0:
            self._periods.append({"start": trade.date, "end": None})
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
        if self._positions[-1]["quantity"] - trade.quantity == 0:
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
                "quantity": self._positions[-1]["quantity"] - trade.quantity
            })

    def _add_dividends(self, disable_dividends=False):
        if disable_dividends:
            return self._outflows
        outflows = [outflow for outflow in self._outflows]
        position_idx = 0
        for date, dividend in self._dividends.items():
            if position_idx >= len(self._positions):
                break
            date = datetime.strptime(date.__str__().split()[0], "%Y-%m-%d")
            while True:
                position = self._positions[position_idx]
                if date > position["date"]:
                    if position_idx + 1 < len(self._positions) and self._positions[position_idx+1]["date"] < date:
                        position_idx += 1
                    elif position["quantity"] > 0:
                        outflows.append({
                            "date": date,
                            "value": position["quantity"] * dividend * (1. - WITHHOLDING_TAX_RATE)
                        })
                        break
                    else:
                        break
                else:
                    break
        return outflows

    def summary(self, disable_dividends=False):
        outflows = self._add_dividends(disable_dividends)
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
                    avg_capital += ((end - inflow["date"]).days / length) * inflow["value"]
            for outflow in outflows:
                if start < outflow["date"] <= end:
                    gain += outflow["value"]
                    avg_capital -= ((end - outflow["date"]).days / length) * outflow["value"]
            tsr *= (1. + gain / avg_capital)
        cagr = tsr ** (365.25 / total_ownership_length) - 1.
        tsr -= 1.
        print(f"{self._info['exchange']}:{self._info['symbol']} - {self._info['longName']} - TSR: {round(tsr * 100, 1)}% - CAGR: {round(cagr * 100, 1)}%")
        return {
            "ticker": f"{self._info['exchange']}:{self._info['symbol']}",
            "name": self._info['longName'],
            "tsr%": round(tsr * 100, 1),
            "cagr%": round(cagr * 100, 1),
            "current": self._positions[-1]["quantity"] > 0,
            "periods": list(reversed(self._periods)),
            "latest_buy": self._inflows[-1]["date"],
            "latest_sell": self._outflows[-1]["date"] if len(self._outflows) > 0 else None
        }


def pull_transactions():
    gc = gspread.service_account(filename="/tmp/gsheet_creds.json")
    sh = gc.open_by_key("1N1S95dIGEISRY7UR47yDrYYoByHarsVbsFZDfLXVDQk")
    transactions = []
    for transaction in sh.worksheet("Trades").get_all_values()[2:]:
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
            "quantity": int(transaction[3]),
            "price_per_share":  float(transaction[4]),
            "action": action
        })
    return transactions


def get_holdings():
    trades = combine_and_sort(pull_transactions())

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
        if summary["current"] is True:
            current_holdings.append(summary)
        else:
            historical_holdings.append(summary)

    return {"current": sorted(current_holdings, key=lambda item: item["latest_buy"], reverse=True),
            "historical": sorted(historical_holdings, key=lambda item: item["latest_sell"], reverse=True)}


class Webpage:
    def __init__(self):
        self.desktop_current = []
        self.desktop_historical = []
        self.mobile_current = []
        self.mobile_historical = []

    def save(self):
        update_date = datetime.now().strftime("%b %-d, %Y")
        txt = []
        txt.append('<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
                   '<meta name="description" content="Overview of investment portfolio, including historical holdings '
                   'with TSR, CAGR, and ownership periods.">')
        txt.append('<title>JG Investing</title>\n<link rel="icon" type="image/png" href="apple-touch-icon.png">\n'
                   '<link rel="apple-touch-icon" href="apple-touch-icon.png" sizes="180x180">\n'
                   '<link rel="icon" href="favicon.ico" sizes="any">')
        txt.append('<style>\n.desktop-version {display: block;}\n.mobile-version {display: none;}\n'
                   '@media (max-width: 600px) {\n.desktop-version {display: none;}\n'
                   '.mobile-version {display: block;}\n}\nbody {min-width: 333px; padding-left: 30px; '
                   'padding-right: 30px;}\n.grid-return {\ndisplay: grid;\ngrid-template-columns: '
                   'max-content max-content;\ncolumn-gap: 15px; \nrow-gap: 2px;\n}\n.left-col {\ntext-align: left;\n'
                   '}\n.right-col {\ntext-align: right;\n}\n</style>')
        txt.append('<div class="desktop-version">')
        txt.append('<br>\n<div style="font-size: 26px; font-weight: bold;">\nCurrent holdings\n</div>\n<br>\n'
                   '<hr style="height: 1px; background-color: black;">\n<br>')
        txt.append('\n<br>\n<hr>\n<br>\n'.join(self.desktop_current))
        txt.append('<br>\n<br>\n<br>\n<div style="font-size: 26px; font-weight: bold;">\nHistorical holdings\n</div>\n'
                   '<br>\n<hr style="height: 1px; background-color: black;">\n<br>')
        txt.append('\n<br>\n<hr>\n<br>\n'.join(self.desktop_historical))
        txt.append('<br>\n<br>\n<br>\n<div style="font-size: 14px;">\n'
                   'All TSR figures were calculated using the modified Dietz method. '
                   'Dividends were assumed to be subject to a 15% withholding tax.\n<br>\n'
                   'For informational purposes only. Nothing contained herein should be construed as a recommendation '
                   'to buy, sell or hold any security or pursue any investment strategy.\n<br>\n'
                   'Logos are trademarks of their respective owners and are used for identification purposes only.\n<br>\n<br>\n'
                   f'Updated on {update_date}.\n</div>\n<br>')
        txt.append('</div>\n<div class="mobile-version">')
        txt.append('<br>\n<div style="font-size: 26px; font-weight: bold;">\nCurrent holdings\n</div>\n'
                   '<hr style="height: 1px; background-color: black;">')
        txt.append('\n<hr>\n'.join(self.mobile_current))
        txt.append('<br>\n<br>\n<div style="font-size: 26px; font-weight: bold;">\nHistorical holdings\n</div>\n'
                   '<hr style="height: 1px; background-color: black;">')
        txt.append('\n<hr>\n'.join(self.mobile_historical))
        txt.append('<br>\n<br>\n<div style="font-size: 14px;">\n'
                   'All TSR figures were calculated using the modified Dietz method. '
                   'Dividends were assumed to be subject to a 15% withholding tax.\n<br>\n<br>\n'
                   'For informational purposes only. Nothing contained herein should be construed as a recommendation '
                   'to buy, sell or hold any security or pursue any investment strategy.\n<br>\n<br>\n'
                   'Logos are trademarks of their respective owners and are used for identification purposes only.\n<br>\n<br>\n'
                   f'Updated on {update_date}.\n</div>\n<br>')
        txt.append('</div>\n')
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
        if holding["current"] is True:
            self.desktop_current.append("\n".join(lines))
        else:
            self.desktop_historical.append("\n".join(lines))

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
        if holding["current"] is True:
            self.mobile_current.append("\n".join(lines))
        else:
            self.mobile_historical.append("\n".join(lines))

    def add_holding(self, holding):
        self.add_holding_desktop(holding)
        self.add_holding_mobile(holding)


def generate_webpage(holdings):
    webpage = Webpage()
    for holding in holdings["current"]:
        webpage.add_holding(holding)
    for holding in holdings["historical"]:
        webpage.add_holding(holding)
    webpage.save()


def main():
    generate_webpage(get_holdings())


if __name__ == "__main__":
    main()
