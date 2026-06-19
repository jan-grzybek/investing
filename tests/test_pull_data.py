"""Tests for ``pull_data`` which talks to Google Sheets via ``gspread``.

The Google API stack is fully mocked. We focus on row parsing rules:
the include flag, action normalisation, and numeric/comma stripping.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from investing.sheets import SheetParseError, _pad_rows, pull_data


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


def _build_spreadsheet(equities, returns, cash, fixed_income=None):
    """Build the nested gspread API mock chain.

    ``fixed_income`` mirrors the equities sheet (identical column
    schema; rows are parsed by the same row parser). Defaulting to
    an empty list keeps the historical equity-only tests honest.
    """
    sh = MagicMock()
    fixed_income = fixed_income if fixed_income is not None else []

    def _worksheet(name):
        ws = MagicMock()
        if name == "Equities":
            ws.get_all_values.return_value = _equities_header() + equities
        elif name == "Fixed Income":
            ws.get_all_values.return_value = _equities_header() + fixed_income
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
        monkeypatch.setattr(
            "investing.sheets.gspread.service_account",
            lambda filename: gc,  # noqa: ARG005
        )
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

        transactions, fixed_income, valuations, cash = pull_data()

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
        assert fixed_income == []
        assert valuations == [{"date": datetime(2024, 1, 1), "value": 1000.0, "flow": 0.0}]
        assert cash == [{"currency_code": "USD", "amount": 250.0}]

    def test_thousands_separators_are_stripped(self, patch_gspread):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1,234", "1,500.75", "BUY")],
            returns=[_return_row("01-01-2024", "1,000,000.00", "10,000.00")],
            cash=[_cash_row("EUR", "50,000.25")],
        )
        patch_gspread(sh)

        transactions, _fixed_income, valuations, cash = pull_data()
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

        transactions, _fixed_income, valuations, cash = pull_data()
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

        transactions, _fixed_income, valuations, cash = pull_data()
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
        transactions, _, _, _ = pull_data()
        assert transactions[0]["action"] == "BUY"

    @pytest.mark.parametrize("token", ["S", "SELL", "s", "sell"])
    def test_sell_action_is_normalised(self, patch_gspread, token):
        sh = _build_spreadsheet(
            equities=[_equity_row("01-01-2024", "AAPL", "1", "1.0", token)],
            returns=[],
            cash=[],
        )
        patch_gspread(sh)
        transactions, _, _, _ = pull_data()
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
        transactions, fixed_income, valuations, cash = pull_data()
        assert transactions == []
        assert fixed_income == []
        assert valuations == []
        assert cash == []

    def test_fixed_income_sheet_is_parsed_with_equities_schema(self, patch_gspread):
        # The "Fixed Income" worksheet uses the same column layout
        # as "Equities" -- shared row parser + shared schema constant.
        # Two BUYs land in the dedicated fixed-income list while the
        # equities list stays untouched, so the renderer can later
        # bucket them into the dedicated "Fixed Income" sub-section
        # without confusing them with stock positions.
        sh = _build_spreadsheet(
            equities=[
                _equity_row("01-01-2024", "AAPL", "10", "150.50", "BUY"),
            ],
            returns=[],
            cash=[],
            fixed_income=[
                _equity_row("15-03-2024", "TLT", "100", "90.25", "BUY"),
                _equity_row("01-06-2024", "TLT", "50", "92.10", "SELL"),
            ],
        )
        patch_gspread(sh)
        transactions, fixed_income, _valuations, _cash = pull_data()

        assert transactions == [
            {
                "date": "01-01-2024",
                "ticker": "AAPL",
                "quantity": 10,
                "price_per_share": 150.5,
                "action": "BUY",
            },
        ]
        assert fixed_income == [
            {
                "date": "15-03-2024",
                "ticker": "TLT",
                "quantity": 100,
                "price_per_share": 90.25,
                "action": "BUY",
            },
            {
                "date": "01-06-2024",
                "ticker": "TLT",
                "quantity": 50,
                "price_per_share": 92.10,
                "action": "SELL",
            },
        ]

    def test_fixed_income_parse_error_points_at_fixed_income_sheet(self, patch_gspread):
        # Errors in the fixed-income sheet must surface
        # ``worksheet="Fixed Income"`` so an operator editing the
        # source can find the offending row. Reuses the same
        # SheetParseError contract the equities sheet uses.
        sh = _build_spreadsheet(
            equities=[],
            returns=[],
            cash=[],
            fixed_income=[_equity_row("01-01-2024", "TLT", "10", "1.0", "HOLD")],
        )
        patch_gspread(sh)
        with pytest.raises(SheetParseError) as excinfo:
            pull_data()
        assert excinfo.value.worksheet == "Fixed Income"
        assert "HOLD" in str(excinfo.value)


def _batched_response(equities, returns, cash, fixed_income=None):
    """Compose a gspread ``values_batch_get`` response payload.

    The batched endpoint trims trailing empty cells per row -- a
    behaviour the production fix has to absorb explicitly via
    :func:`investing.sheets._pad_rows`. This helper hands the test
    direct control over the per-row shape so the regression case
    (a row whose right-most column is blank and therefore comes
    back narrower than the schema) can be expressed verbatim.

    The Fixed Income worksheet is added as a second value range
    between Equities and Return so production callers always see
    the full four-sheet payload regardless of whether the test
    cares about fixed income rows.
    """

    def _value_range(rows):
        return {"range": "ignored", "majorDimension": "ROWS", "values": rows}

    return {
        "spreadsheetId": "fake-sheet-id",
        "valueRanges": [
            _value_range(_equities_header() + equities),
            _value_range(_equities_header() + (fixed_income or [])),
            _value_range(_return_header() + returns),
            _value_range(_cash_header() + cash),
        ],
    }


@pytest.fixture
def patch_gspread_batched(monkeypatch):
    """Plant a spreadsheet that responds via ``values_batch_get``.

    Mirrors :func:`patch_gspread` but the resulting mock answers
    the batched API rather than the per-worksheet ``get_all_values``
    chain, so the test exercises the production path through
    :func:`investing.sheets._batch_get_values`.
    """

    def _install(payload):
        sh = MagicMock()
        sh.values_batch_get = MagicMock(return_value=payload)
        sh.worksheet.side_effect = AssertionError(
            "values_batch_get should serve the entire request -- the "
            "per-worksheet fallback must not be reached when the "
            "batched API is available."
        )
        gc = MagicMock()
        gc.open_by_key.return_value = sh
        monkeypatch.setattr(
            "investing.sheets.gspread.service_account",
            lambda filename: gc,  # noqa: ARG005
        )
        monkeypatch.setenv("GSHEET_ID", "fake-sheet-id")
        return sh

    return _install


class TestBatchedPath:
    """Regression tests for the ``values_batch_get`` shape contract.

    The Sheets ``values.batchGet`` endpoint trims trailing empty
    cells from each row before serialising the response. The
    legacy per-worksheet ``Worksheet.get_all_values`` ran
    ``fill_gaps`` internally so every parser downstream got rows
    of uniform width; the batched path needs an explicit pad to
    match that contract, otherwise a sheet whose right-most
    column happens to be blank trips ``_check_row_shape`` and
    fails the build.
    """

    def test_trimmed_trailing_cells_are_padded_for_parsers(self, patch_gspread_batched):
        # Equities row missing the trailing include flag (the API
        # would have trimmed that blank cell). Reproduces the
        # production failure observed when a row's include column
        # was empty: ``len(row) == 6`` < ``_SCHEMAS['Equities']``
        # would raise ``SheetParseError`` before the fix.
        payload = _batched_response(
            equities=[
                ["", "01-01-2024", "AAPL", "1", "100.0", "BUY"],
            ],
            returns=[],
            cash=[],
        )
        patch_gspread_batched(payload)

        transactions, _fixed_income, _, _ = pull_data()
        # Padded include flag ("") is not a YES token, so the row
        # is silently skipped -- which is the same outcome the
        # legacy per-worksheet path would have produced for a
        # row whose include cell was blank rather than absent.
        assert transactions == []

    def test_full_width_rows_pass_through_unchanged(self, patch_gspread_batched):
        # Sanity check: the padding step must not interfere with a
        # row that already has all schema-required columns
        # populated.
        payload = _batched_response(
            equities=[
                ["", "01-01-2024", "AAPL", "1", "100.0", "BUY", "YES"],
            ],
            returns=[],
            cash=[],
        )
        patch_gspread_batched(payload)

        transactions, _fixed_income, _, _ = pull_data()
        assert len(transactions) == 1
        assert transactions[0]["ticker"] == "AAPL"

    def test_empty_value_range_is_treated_as_no_rows(self, patch_gspread_batched):
        # The Sheets API omits the ``"values"`` key entirely when a
        # range is empty; the loader must treat that the same as
        # an empty list rather than propagate ``None`` into the
        # per-row iterator.
        payload = {
            "spreadsheetId": "fake-sheet-id",
            "valueRanges": [
                {"range": "ignored"},
                {"range": "ignored"},
                {"range": "ignored"},
                {"range": "ignored"},
            ],
        }
        patch_gspread_batched(payload)

        transactions, fixed_income, valuations, cash = pull_data()
        assert transactions == []
        assert fixed_income == []
        assert valuations == []
        assert cash == []

    def test_fixed_income_value_range_is_parsed_in_batched_path(self, patch_gspread_batched):
        # The batched response carries the Fixed Income range
        # alongside Equities / Return / Cash; ensure the loader
        # routes it to the dedicated bucket rather than mixing it
        # into the equities list.
        payload = _batched_response(
            equities=[
                ["", "01-01-2024", "AAPL", "1", "100.0", "BUY", "YES"],
            ],
            returns=[],
            cash=[],
            fixed_income=[
                ["", "01-02-2024", "TLT", "5", "90.0", "BUY", "YES"],
            ],
        )
        patch_gspread_batched(payload)

        transactions, fixed_income, _, _ = pull_data()
        assert [t["ticker"] for t in transactions] == ["AAPL"]
        assert [t["ticker"] for t in fixed_income] == ["TLT"]


class TestPadRows:
    """Direct coverage for :func:`investing.sheets._pad_rows`.

    The batched path's regression tests above cover the
    end-to-end contract; this class exercises the helper in
    isolation so an edge case (zero-width target, already-wide
    row, mixed-width input) doesn't have to be reproduced through
    the whole gspread mock chain.
    """

    def test_short_row_is_padded_to_target_width(self):
        assert _pad_rows([["a", "b"]], 4) == [["a", "b", "", ""]]

    def test_already_wide_row_is_passed_through(self):
        assert _pad_rows([["a", "b", "c", "d"]], 3) == [["a", "b", "c", "d"]]

    def test_zero_width_returns_input_copy(self):
        rows = [["a"], ["b"]]
        assert _pad_rows(rows, 0) == rows

    def test_mixed_widths_normalise_to_target(self):
        assert _pad_rows([["a"], ["a", "b", "c"], []], 2) == [
            ["a", ""],
            ["a", "b", "c"],
            ["", ""],
        ]
