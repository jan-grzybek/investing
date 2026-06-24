"""Google Sheets ingestion: row schemas, validators, and
``pull_data`` (the only function in here that touches the
network).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _ColSpec:
    """One column in a worksheet's row schema.

    ``index`` is the 0-based column position in the raw list returned
    by gspread; ``label`` is the human-facing field name used in
    error messages and ``column`` is the 1-based sheet coordinate the
    operator will see in the source spreadsheet (so "col 5" in an
    error message lines up with the source row).
    """

    label: str
    index: int

    @property
    def column(self) -> int:
        return self.index + 1


@dataclass(frozen=True)
class _WorksheetSchema:
    """Column layout for one worksheet.

    The schema is the single source of truth for "which column carries
    which field" -- every parser pulls cells through this object, so a
    column move in the source spreadsheet is a one-line edit here
    rather than a coordinated sweep across the parsers and their
    matching error-message column / field strings.
    """

    name: str
    columns: tuple[_ColSpec, ...]

    @property
    def width(self) -> int:
        """Minimum row length required to safely address every column."""
        return len(self.columns)

    def check_shape(self, row_index: int, row: list[str]) -> None:
        """Raise ``SheetParseError`` if ``row`` is narrower than the schema."""
        if len(row) < self.width:
            raise SheetParseError(
                worksheet=self.name,
                row=row_index,
                reason=(f"row has {len(row)} columns, expected at least {self.width}"),
            )

    def cell(self, row: list[str], spec: _ColSpec) -> str:
        return row[spec.index]


# Per-worksheet schemas. Each ``_ColSpec`` carries the field name we
# use in error messages so a renamed column never goes out of sync
# with the diagnostic strings the legacy implementation hand-coded
# at every raise site.
class _EquitiesCols:
    DATE = _ColSpec("date", 1)
    TICKER = _ColSpec("ticker", 2)
    QUANTITY = _ColSpec("quantity", 3)
    PRICE = _ColSpec("price_per_share", 4)
    ACTION = _ColSpec("action", 5)
    INCLUDE = _ColSpec("include", 6)


class _ReturnCols:
    DATE = _ColSpec("date", 1)
    VALUE = _ColSpec("value", 2)
    FLOW = _ColSpec("flow", 3)
    INCLUDE = _ColSpec("include", 4)


class _CashCols:
    CURRENCY = _ColSpec("currency_code", 2)
    AMOUNT = _ColSpec("amount", 3)
    INCLUDE = _ColSpec("include", 4)


_EQUITIES_SCHEMA = _WorksheetSchema(
    name="Equities",
    columns=(
        _ColSpec("(unused)", 0),
        _EquitiesCols.DATE,
        _EquitiesCols.TICKER,
        _EquitiesCols.QUANTITY,
        _EquitiesCols.PRICE,
        _EquitiesCols.ACTION,
        _EquitiesCols.INCLUDE,
    ),
)


# Fixed Income worksheet shares the Equities row schema column-for-column
# (date / ticker / quantity / price / action / include); the only
# difference is the worksheet name + the asset class the resulting
# transactions are tagged with downstream. Re-using ``_EquitiesCols`` for
# the column indices keeps the maintenance contract honest -- a future
# column move only has to update one ``_ColSpec``.
_FIXED_INCOME_SCHEMA = _WorksheetSchema(
    name="Fixed Income",
    columns=(
        _ColSpec("(unused)", 0),
        _EquitiesCols.DATE,
        _EquitiesCols.TICKER,
        _EquitiesCols.QUANTITY,
        _EquitiesCols.PRICE,
        _EquitiesCols.ACTION,
        _EquitiesCols.INCLUDE,
    ),
)


_RETURN_SCHEMA = _WorksheetSchema(
    name="Return",
    columns=(
        _ColSpec("(unused)", 0),
        _ReturnCols.DATE,
        _ReturnCols.VALUE,
        _ReturnCols.FLOW,
        _ReturnCols.INCLUDE,
    ),
)


_CASH_SCHEMA = _WorksheetSchema(
    name="Cash & Cash Equivalents",
    columns=(
        _ColSpec("(unused)", 0),
        _ColSpec("(unused)", 1),
        _CashCols.CURRENCY,
        _CashCols.AMOUNT,
        _CashCols.INCLUDE,
    ),
)


_SCHEMAS_BY_NAME: dict[str, _WorksheetSchema] = {
    s.name: s for s in (_EQUITIES_SCHEMA, _FIXED_INCOME_SCHEMA, _RETURN_SCHEMA, _CASH_SCHEMA)
}


# Public alias preserved so the batched-path padding can look up the
# expected width by worksheet name without reaching into the schema
# object. ``_pad_rows`` consumes this mapping.
_SCHEMAS: dict[str, int] = {name: s.width for name, s in _SCHEMAS_BY_NAME.items()}


def _parse_number_cell(
    schema: _WorksheetSchema,
    spec: _ColSpec,
    *,
    row_index: int,
    row: list[str],
    convert: type[int] | type[float],
) -> int | float:
    """Pull a numeric cell through the schema, raising with full context.

    Bundles the two recurring patterns -- index into the row, strip
    thousands separators, convert via ``int`` / ``float`` -- so the
    parser bodies below stay focused on the per-worksheet field
    layout instead of restating the same try/except/raise dance for
    every numeric field.
    """
    raw = schema.cell(row, spec)
    try:
        return _to_int(raw) if convert is int else _to_float(raw)
    except ValueError as exc:
        kind = "an integer" if convert is int else "a number"
        raise SheetParseError(
            worksheet=schema.name,
            row=row_index,
            column=spec.column,
            field=spec.label,
            reason=f"not {kind} ({exc})",
        ) from None


def _parse_equity_row(
    row_index: int,
    row: list[str],
    *,
    schema: _WorksheetSchema = _EQUITIES_SCHEMA,
) -> EquityTransaction | None:
    """Parse one ``Equities`` (or ``Fixed Income``) row into the transaction dict shape.

    Returns ``None`` when the include flag is anything other than a YES
    token (matches the historical "skip" semantics). Raises
    :class:`SheetParseError` with row / column context on any other
    validation failure so the caller knows precisely what to fix in
    the source sheet.

    The ``Fixed Income`` worksheet shares the equity row schema
    column-for-column, so the parser is reused for both inputs by
    swapping the active ``schema``. The worksheet name still
    propagates into any raised :class:`SheetParseError` so the
    operator sees the correct sheet name in error messages.
    """
    schema.check_shape(row_index, row)
    if schema.cell(row, _EquitiesCols.INCLUDE) not in _YES_TOKENS:
        return None
    action_token = schema.cell(row, _EquitiesCols.ACTION)
    if action_token in _BUY_TOKENS:
        action = "BUY"
    elif action_token in _SELL_TOKENS:
        action = "SELL"
    else:
        raise SheetParseError(
            worksheet=schema.name,
            row=row_index,
            column=_EquitiesCols.ACTION.column,
            field=_EquitiesCols.ACTION.label,
            reason=f"unknown action token {action_token!r}",
        )
    quantity = int(
        _parse_number_cell(
            schema,
            _EquitiesCols.QUANTITY,
            row_index=row_index,
            row=row,
            convert=int,
        )
    )
    price = float(
        _parse_number_cell(
            schema,
            _EquitiesCols.PRICE,
            row_index=row_index,
            row=row,
            convert=float,
        )
    )
    # Positivity guard. ``quantity`` is a share count used as a divisor
    # when computing the burst volume-weighted average price
    # (``trades.combine_and_sort`` / ``_combine_trade_events``), so a
    # zero would raise ``ZeroDivisionError`` deep in the pipeline; a
    # negative quantity or price would silently flip cashflow signs and
    # corrupt MoIC / IRR / weights. Reject both at ingestion with the
    # same coordinate-only ``SheetParseError`` contract the rest of the
    # parser uses (no cell value in the message, so the leak-safe
    # wrapper stays leak-safe).
    if quantity <= 0:
        raise SheetParseError(
            worksheet=schema.name,
            row=row_index,
            column=_EquitiesCols.QUANTITY.column,
            field=_EquitiesCols.QUANTITY.label,
            reason="must be a positive whole number",
        )
    if price <= 0:
        raise SheetParseError(
            worksheet=schema.name,
            row=row_index,
            column=_EquitiesCols.PRICE.column,
            field=_EquitiesCols.PRICE.label,
            reason="must be a positive number",
        )
    return {
        "date": schema.cell(row, _EquitiesCols.DATE),
        "ticker": schema.cell(row, _EquitiesCols.TICKER),
        "quantity": quantity,
        "price_per_share": price,
        "action": action,
    }


def _parse_return_row(row_index: int, row: list[str]) -> Valuation | None:
    schema = _RETURN_SCHEMA
    schema.check_shape(row_index, row)
    if schema.cell(row, _ReturnCols.INCLUDE) not in _YES_TOKENS:
        return None
    raw_date = schema.cell(row, _ReturnCols.DATE)
    try:
        date = datetime.strptime(raw_date, "%d-%m-%Y")
    except ValueError as exc:
        raise SheetParseError(
            worksheet=schema.name,
            row=row_index,
            column=_ReturnCols.DATE.column,
            field=_ReturnCols.DATE.label,
            reason=f"expected DD-MM-YYYY ({exc})",
        ) from None
    # ``value`` and ``flow`` share a single error surface: the legacy
    # parser raised "value/flow" for either failure rather than
    # pinpointing the offending column. Preserve that contract so the
    # tests asserting on ``field == "value/flow"`` keep working; the
    # individual cell labels are still discoverable via the schema.
    try:
        value = _to_float(schema.cell(row, _ReturnCols.VALUE))
        flow = _to_float(schema.cell(row, _ReturnCols.FLOW))
    except ValueError as exc:
        raise SheetParseError(
            worksheet=schema.name,
            row=row_index,
            field="value/flow",
            reason=f"not a number ({exc})",
        ) from None
    return {"date": date, "value": value, "flow": flow}


def _parse_cash_row(row_index: int, row: list[str]) -> CashBalance | None:
    schema = _CASH_SCHEMA
    schema.check_shape(row_index, row)
    if schema.cell(row, _CashCols.INCLUDE) not in _YES_TOKENS:
        return None
    amount = float(
        _parse_number_cell(
            schema,
            _CashCols.AMOUNT,
            row_index=row_index,
            row=row,
            convert=float,
        )
    )
    return {
        "currency_code": schema.cell(row, _CashCols.CURRENCY),
        "amount": amount,
    }


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


def pull_data() -> tuple[
    list[EquityTransaction],
    list[EquityTransaction],
    list[Valuation],
    list[CashBalance],
]:
    """Pull every input worksheet through the gspread API in one round trip.

    Returns four lists in a fixed order: equity transactions, fixed-income
    transactions, return-series valuations, and cash balances. The
    fixed-income list shares the :class:`EquityTransaction` shape because
    the upstream "Fixed Income" worksheet is column-for-column identical
    to "Equities"; the asset-class distinction is propagated into the
    pipeline downstream by tagging the resulting :class:`Holding`
    objects rather than by carrying a flag on every row dict.
    """
    gc = _gspread_client()
    sh = gc.open_by_key(os.environ["GSHEET_ID"])

    # Single batched request for every worksheet. The legacy
    # implementation called ``sh.worksheet(name).get_all_values()``
    # three times in sequence -- each call is a separate Sheets
    # API round-trip, which dominated the cold-start latency of
    # the build. ``values_batch_get`` ships one HTTPS request and
    # comes back with every range; the per-sheet shape stays
    # identical to ``get_all_values`` (a 2-D list of strings) so
    # the parsers below need no changes. The Fixed Income range
    # rides alongside Equities so a portfolio that grows a
    # fixed-income sleeve costs the build no extra HTTPS hops.
    range_names = ("Equities", "Fixed Income", "Return", "Cash & Cash Equivalents")
    sheets = _batch_get_values(sh, range_names)

    transactions: list[EquityTransaction] = []
    for row_index, row in _iter_data_rows(sheets["Equities"]):
        parsed_txn = _parse_equity_row(row_index, row, schema=_EQUITIES_SCHEMA)
        if parsed_txn is not None:
            transactions.append(parsed_txn)

    fixed_income_transactions: list[EquityTransaction] = []
    for row_index, row in _iter_data_rows(sheets["Fixed Income"]):
        parsed_txn = _parse_equity_row(
            row_index,
            row,
            schema=_FIXED_INCOME_SCHEMA,
        )
        if parsed_txn is not None:
            fixed_income_transactions.append(parsed_txn)

    valuations: list[Valuation] = []
    for row_index, row in _iter_data_rows(sheets["Return"]):
        parsed_val = _parse_return_row(row_index, row)
        if parsed_val is not None:
            valuations.append(parsed_val)

    cash: list[CashBalance] = []
    for row_index, row in _iter_data_rows(sheets["Cash & Cash Equivalents"]):
        parsed_cash = _parse_cash_row(row_index, row)
        if parsed_cash is not None:
            cash.append(parsed_cash)

    return transactions, fixed_income_transactions, valuations, cash


def _batch_get_values(
    sh,
    range_names: tuple[str, ...],
) -> dict[str, list[list[str]]]:
    """Fetch all requested worksheet ranges in a single API call.

    Returns a ``{sheet_name: rows}`` mapping. The implementation
    prefers :py:meth:`gspread.Spreadsheet.values_batch_get` (one
    HTTPS round-trip) and falls back to per-worksheet
    ``get_all_values`` calls for older gspread versions / mock
    surfaces that don't implement the batch method -- the
    fallback preserves the historical behaviour for tests that
    plant ``sh.worksheet(name).get_all_values`` stubs without
    teaching them about ``values_batch_get``.

    Sheets API normalisation: the underlying ``values.batchGet``
    endpoint trims trailing empty cells per row (a row whose
    right-most columns are blank comes back as a 5-element list
    even when the worksheet schema is 7 columns wide). The
    legacy per-worksheet ``Worksheet.get_all_values`` quietly
    pads each row up to a uniform width via gspread's
    ``fill_gaps`` helper before returning, which is what every
    parser downstream assumes. We replicate that contract on
    the batched path here by padding every row out to the
    schema-defined minimum so a sheet whose right-most column
    happens to be blank doesn't trip ``_check_row_shape`` after
    the batched call. The per-worksheet fallback path keeps the
    raw mock contract -- tests that plant short rows directly
    through ``get_all_values`` continue to exercise the
    parser-side shape check unchanged.
    """
    batch = getattr(sh, "values_batch_get", None)
    if batch is not None:
        try:
            response = batch(list(range_names))
        except Exception:
            # The fallback path below covers any failure mode the
            # batch call exposes -- a stub that doesn't implement
            # the API, a transient HTTP error gspread surfaces as
            # a generic exception, etc. We deliberately swallow
            # broadly here: the per-worksheet path that follows is
            # the strictly-equivalent legacy contract, so a
            # failure that's also reproducible there will still
            # surface, just from a function the caller already
            # knew about.
            response = None
        if isinstance(response, dict):
            value_ranges = response.get("valueRanges", [])
            if len(value_ranges) == len(range_names):
                return {
                    name: _pad_rows(vr.get("values") or [], _SCHEMAS.get(name, 0))
                    for name, vr in zip(range_names, value_ranges, strict=True)
                }
    # Per-worksheet fallback path; preserves the legacy contract
    # for any caller (or test) that hasn't migrated to the batched
    # API. ``Worksheet.get_all_values`` already runs gspread's
    # ``fill_gaps`` internally so production never hits a short
    # row through here.
    return {name: sh.worksheet(name).get_all_values() for name in range_names}


def _pad_rows(rows: list[list[str]], width: int) -> list[list[str]]:
    """Pad short rows out to ``width`` columns with empty strings.

    The Sheets ``values.batchGet`` API trims trailing empty cells
    per row, so a sheet whose right-most columns happen to be
    blank on a given row comes back narrower than the schema
    expects. Every parser downstream addresses cells by positional
    index (``row[5]`` for the action token, etc.) and relies on
    the row being at least ``_SCHEMAS[name]`` wide; padding here
    makes the per-row shape consistent with the legacy
    ``get_all_values`` contract. Rows that are already at or
    above ``width`` are passed through unchanged.
    """
    if not width:
        return list(rows)
    padded: list[list[str]] = []
    for row in rows:
        if len(row) < width:
            padded.append(list(row) + [""] * (width - len(row)))
        else:
            padded.append(list(row))
    return padded
