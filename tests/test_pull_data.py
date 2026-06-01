"""Tests for ``pull_data`` which talks to Google Sheets via ``gspread``.

The Google API stack is fully mocked. We focus on row parsing rules:
the include flag, action normalisation, and numeric/comma stripping.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

import investing.sheets as _sheets
from investing.sheets import SheetParseError, pull_data


def _equities_header():
    # ``pull_data`` skips the first two rows; their content doesn't matter.
    return [["", "", "", "", "", "", ""], ["hdr"] * 7]


def _return_header():
    return [["", "", "", "", ""], ["hdr"] * 5]


def _cash_header():
    return [["", "", "", "", ""], ["hdr"] * 5]


def _equity_row(date, ticker, qty, price, action, include="YES"):
    # Columns: 0?, date, ticker, quantity, price, action, include
    return ["", date, ticker, qty, price, action, include]


def _return_row(date, value, flow, include="YES"):
    return ["", date, value, flow, include]


def _cash_row(currency, amount, include="YES"):
    return ["", "", currency, amount, include]


def _build_spreadsheet(equities, returns, cash):
    """Build the nested gspread API mock chain."""
    sh = MagicMock()

    def _worksheet(name):
        ws = MagicMock()
        if name == "Equities":
            ws.get_all_values.return_value = _equities_header() + equities
        elif name == "Return":
            ws.get_all_values.return_value = _return_header() + returns
        elif name == "Cash & Cash Equivalents":
            ws.get_all_values.return_value = _cash_header() + cash
        else:
            raise AssertionError(f"Unexpected worksheet: {name!r}")
        return ws

    sh.worksheet.side_effect = _worksheet
    return sh


@pytest.fixture
def patch_gspread(monkeypatch):
    def _install(sh):
        gc = MagicMock()
        gc.open_by_key.return_value = sh
        monkeypatch.setattr(_sheets.gspread, "service_account", lambda filename: gc)  # noqa: ARG005
        monkeypatch.setenv("GSHEET_ID", "fake-sheet-id")
        return gc

    return _install


class TestPullData:
    def test_parses_typical_rows(self, patch_gspread):
        sh = _build_spreadsheet(
            equities=[
                _equity_row("01-01-2024", "AAPL", "10", "150.50", "BUY"),
                _equity_row("02-02-2024", "MSFT", "5", "300.00", "SELL"),
            ],
            returns=[_return_row("01-01-2024", "1000.00", "0.00")],
            cash=[_cash_row("USD", "250.00")],
        )
        patch_gspread(sh)

        transactions, valuations, cash = pull_data()

        assert transactions == [
            {
                "date": "01-01-2024",
                "ticker": "AAPL",
                "quantity": 10,
                "price_per_share": 150.5,
                "action": "BUY",
            },
            {
                "date": "02-02-2024",
                "ticker": "MSFT",
                "quantity": 5,
                "price_per_share": 300.0,
                "action": "SELL",
            },
        ]
        assert valuations == [
            {"date": datetime(2024, 1, 1), "value": 1000.0, "flow": 0.0}
        ]
        assert cash == [{"currency_code": "USD", "amount": 250.0}]

    def test_thousands_separators_are_stripped(self, patch_gspread):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1,234", "1,500.75", "BUY")],
            returns=[_return_row("01-01-2024", "1,000,000.00", "10,000.00")],
            cash=[_cash_row("EUR", "50,000.25")],
        )
        patch_gspread(sh)

        transactions, valuations, cash = pull_data()
        assert transactions[0]["quantity"] == 1234
        assert transactions[0]["price_per_share"] == pytest.approx(1500.75)
        assert valuations[0]["value"] == pytest.approx(1_000_000.0)
        assert valuations[0]["flow"] == pytest.approx(10_000.0)
        assert cash[0]["amount"] == pytest.approx(50_000.25)

    @pytest.mark.parametrize("flag", ["", "N", "no", "maybe"])
    def test_excluded_rows_are_dropped(self, patch_gspread, flag):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1", "1.0", "BUY", include=flag)],
            returns=[_return_row("01-01-2024", "1.0", "0.0", include=flag)],
            cash=[_cash_row("USD", "1.0", include=flag)],
        )
        patch_gspread(sh)

        transactions, valuations, cash = pull_data()
        assert transactions == []
        assert valuations == []
        assert cash == []

    @pytest.mark.parametrize("flag", ["Y", "YES", "y", "yes"])
    def test_all_yes_variants_are_accepted(self, patch_gspread, flag):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1", "1.0", "BUY", include=flag)],
            returns=[_return_row("01-01-2024", "1.0", "0.0", include=flag)],
            cash=[_cash_row("USD", "1.0", include=flag)],
        )
        patch_gspread(sh)

        transactions, valuations, cash = pull_data()
        assert len(transactions) == 1
        assert len(valuations) == 1
        assert len(cash) == 1

    @pytest.mark.parametrize("token", ["B", "BUY", "b", "buy"])
    def test_buy_action_is_normalised(self, patch_gspread, token):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1", "1.0", token)],
            returns=[],
            cash=[],
        )
        patch_gspread(sh)
        transactions, _, _ = pull_data()
        assert transactions[0]["action"] == "BUY"

    @pytest.mark.parametrize("token", ["S", "SELL", "s", "sell"])
    def test_sell_action_is_normalised(self, patch_gspread, token):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1", "1.0", token)],
            returns=[],
            cash=[],
        )
        patch_gspread(sh)
        transactions, _, _ = pull_data()
        assert transactions[0]["action"] == "SELL"

    def test_unknown_action_token_raises(self, patch_gspread):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1", "1.0", "HOLD")],
            returns=[],
            cash=[],
        )
        patch_gspread(sh)
        # The new :class:`SheetParseError` (a ``ValueError`` subclass)
        # replaces the legacy ``assert False`` -- ``pytest.raises``
        # against ``ValueError`` keeps the test resilient to the
        # original failure-mode contract being upgraded from
        # ``AssertionError`` to a structured error type with row /
        # column context.
        with pytest.raises(SheetParseError) as excinfo:
            pull_data()
        # The error must point at the offending sheet location so an
        # operator editing the source Google Sheet can find the bad
        # row without having to instrument the script.
        assert excinfo.value.worksheet == "Equities"
        assert excinfo.value.row == 3
        assert "HOLD" in str(excinfo.value)

    def test_short_row_raises_with_location(self, patch_gspread):
        # Rows missing the include flag (or any other required column)
        # should produce a precise error rather than an opaque
        # IndexError. ``Cash & Cash Equivalents`` has 5 columns; a
        # 4-column row trips the shape check.
        sh = _build_spreadsheet(
            equities=[],
            returns=[],
            cash=[["", "", "USD", "100.0"]],
        )
        patch_gspread(sh)
        with pytest.raises(SheetParseError) as excinfo:
            pull_data()
        assert excinfo.value.worksheet == "Cash & Cash Equivalents"
        assert excinfo.value.row == 3
        assert "5" in str(excinfo.value)

    def test_invalid_number_raises_with_location(self, patch_gspread):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "ten", "1.0", "BUY")],
            returns=[],
            cash=[],
        )
        patch_gspread(sh)
        with pytest.raises(SheetParseError) as excinfo:
            pull_data()
        assert excinfo.value.worksheet == "Equities"
        assert excinfo.value.field == "quantity"

    def test_empty_sheets_return_empty_collections(self, patch_gspread):
        sh = _build_spreadsheet(equities=[], returns=[], cash=[])
        patch_gspread(sh)
        transactions, valuations, cash = pull_data()
        assert transactions == []
        assert valuations == []
        assert cash == []
