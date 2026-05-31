"""Google Sheets ingestion: row schemas, validators, and
``pull_data`` (the only function in here that touches the
network).
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import gspread

from .types import CashBalance, EquityTransaction, Valuation

# ---------------------------------------------------------------------------
# Sheet ingestion
# ---------------------------------------------------------------------------


_YES_TOKENS = frozenset({"Y", "YES", "y", "yes"})


_BUY_TOKENS = frozenset({"B", "BUY", "b", "buy"})


_SELL_TOKENS = frozenset({"S", "SELL", "s", "sell"})




class SheetParseError(ValueError):
    """A row from the source spreadsheet failed validation.

    Carries enough context (worksheet name, 1-indexed row number on the
    sheet, column position, raw value) to point a human at the offending
    cell -- the previous ``assert False, f"Unknown action token: ..."``
    pattern lost that context inside the leak-safe wrapper's sanitized
    traceback, where the offending value couldn't be surfaced. The new
    error class wraps everything into the exception type so the wrapper
    keeps it private (it drops ``str(exc)``) while a human-driven local
    run still gets a readable diagnostic via ``repr``.
    """

    def __init__(
        self,
        *,
        worksheet: str,
        row: int,
        column: int | None = None,
        field: str | None = None,
        reason: str,
    ):
        loc = f"{worksheet} row {row}"
        if column is not None:
            loc += f" col {column}"
        if field:
            loc += f" ({field})"
        super().__init__(f"{loc}: {reason}")
        self.worksheet = worksheet
        self.row = row
        self.column = column
        self.field = field
        self.reason = reason




def _to_float(value: str) -> float:
    return float(value.replace(",", ""))




def _to_int(value: str) -> int:
    return int(value.replace(",", ""))




# Number of leading rows on each worksheet that pull_data should skip.
# The source Google Sheet uses the first two rows for headers / spacer;
# data starts at row 3 (1-indexed). Surfaced as a constant so the row-
# number arithmetic in error messages stays in sync with the slice.
_SHEET_DATA_OFFSET = 2



# Per-worksheet schema: the minimum number of columns we need to be able
# to address. Used to validate row shape before extracting fields so a
# truncated row produces a precise "row N has K columns, expected M"
# error rather than an opaque IndexError deep inside the per-row parser.
_SCHEMAS: dict[str, int] = {
    "Equities": 7,
    "Return": 5,
    "Cash & Cash Equivalents": 5,
}




def _check_row_shape(worksheet: str, row_index: int, row: list[str]) -> None:
    expected = _SCHEMAS[worksheet]
    if len(row) < expected:
        raise SheetParseError(
            worksheet=worksheet,
            row=row_index,
            reason=f"row has {len(row)} columns, expected at least {expected}",
        )




def _parse_equity_row(row_index: int, row: list[str]) -> EquityTransaction | None:
    """Parse one ``Equities`` row into the transaction dict shape.

    Returns ``None`` when the include flag is anything other than a YES
    token (matches the historical "skip" semantics). Raises
    :class:`SheetParseError` with row / column context on any other
    validation failure so the caller knows precisely what to fix in
    the source sheet.
    """
    _check_row_shape("Equities", row_index, row)
    include = row[6]
    if include not in _YES_TOKENS:
        return None
    action_token = row[5]
    if action_token in _BUY_TOKENS:
        action = "BUY"
    elif action_token in _SELL_TOKENS:
        action = "SELL"
    else:
        raise SheetParseError(
            worksheet="Equities",
            row=row_index,
            column=6,
            field="action",
            reason=f"unknown action token {action_token!r}",
        )
    try:
        quantity = _to_int(row[3])
    except ValueError as exc:
        raise SheetParseError(
            worksheet="Equities", row=row_index, column=4,
            field="quantity", reason=f"not an integer ({exc})",
        ) from None
    try:
        price = _to_float(row[4])
    except ValueError as exc:
        raise SheetParseError(
            worksheet="Equities", row=row_index, column=5,
            field="price_per_share", reason=f"not a number ({exc})",
        ) from None
    return {
        "date": row[1],
        "ticker": row[2],
        "quantity": quantity,
        "price_per_share": price,
        "action": action,
    }




def _parse_return_row(row_index: int, row: list[str]) -> Valuation | None:
    _check_row_shape("Return", row_index, row)
    if row[4] not in _YES_TOKENS:
        return None
    try:
        date = datetime.strptime(row[1], "%d-%m-%Y")
    except ValueError as exc:
        raise SheetParseError(
            worksheet="Return", row=row_index, column=2, field="date",
            reason=f"expected DD-MM-YYYY ({exc})",
        ) from None
    try:
        value = _to_float(row[2])
        flow = _to_float(row[3])
    except ValueError as exc:
        raise SheetParseError(
            worksheet="Return", row=row_index, field="value/flow",
            reason=f"not a number ({exc})",
        ) from None
    return {"date": date, "value": value, "flow": flow}




def _parse_cash_row(row_index: int, row: list[str]) -> CashBalance | None:
    _check_row_shape("Cash & Cash Equivalents", row_index, row)
    if row[4] not in _YES_TOKENS:
        return None
    try:
        amount = _to_float(row[3])
    except ValueError as exc:
        raise SheetParseError(
            worksheet="Cash & Cash Equivalents", row=row_index, column=4,
            field="amount", reason=f"not a number ({exc})",
        ) from None
    return {"currency_code": row[2], "amount": amount}




def _gspread_client():
    """Build a gspread client from credentials in the environment.

    Prefers ``GSHEET_CREDS`` (the full service-account JSON, passed in
    directly by the CI workflow) so the secret never needs to be
    materialised on disk. Falls back to ``GSHEET_CREDS_FILE`` for local
    runs that prefer a file-on-disk workflow, then to the legacy
    ``/tmp/gsheet_creds.json`` path that earlier versions of the
    workflow used to write to (kept so a roll-back of the workflow
    file alone still works)."""
    creds_json = os.environ.get("GSHEET_CREDS")
    if creds_json:
        return gspread.service_account_from_dict(json.loads(creds_json))
    creds_file = os.environ.get("GSHEET_CREDS_FILE", "/tmp/gsheet_creds.json")
    return gspread.service_account(filename=creds_file)




def _iter_data_rows(rows: list[list[str]]):
    """Yield ``(spreadsheet_row_number, row)`` for the data portion.

    Skips the two leading rows (``_SHEET_DATA_OFFSET``) and computes the
    1-indexed sheet row number for diagnostic messages.
    """
    yield from enumerate(rows[_SHEET_DATA_OFFSET:], start=_SHEET_DATA_OFFSET + 1)




def pull_data() -> tuple[list[EquityTransaction], list[Valuation], list[CashBalance]]:
    gc = _gspread_client()
    sh = gc.open_by_key(os.environ["GSHEET_ID"])

    transactions: list[EquityTransaction] = []
    for row_index, row in _iter_data_rows(sh.worksheet("Equities").get_all_values()):
        parsed_txn = _parse_equity_row(row_index, row)
        if parsed_txn is not None:
            transactions.append(parsed_txn)

    valuations: list[Valuation] = []
    for row_index, row in _iter_data_rows(sh.worksheet("Return").get_all_values()):
        parsed_val = _parse_return_row(row_index, row)
        if parsed_val is not None:
            valuations.append(parsed_val)

    cash: list[CashBalance] = []
    for row_index, row in _iter_data_rows(
        sh.worksheet("Cash & Cash Equivalents").get_all_values()
    ):
        parsed_cash = _parse_cash_row(row_index, row)
        if parsed_cash is not None:
            cash.append(parsed_cash)

    return transactions, valuations, cash
