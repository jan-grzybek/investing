"""Exceptions raised by the page generator on invariant / data faults.

Production code used to express these as bare ``assert`` statements,
which silently disappear under ``python -O``. Keeping them as real
exceptions makes them load-bearing regardless of optimisation flag
and gives the leak-safe wrapper in :mod:`investing.safe_run` a
qualified type to render in its sanitized failure summary.

The classes carry no message-side payload beyond a hand-written
description, so the wrapper's "drop ``str(exc)``" policy does not
suppress diagnostic context that ``__qualname__`` already conveys.
"""
from __future__ import annotations


class InvariantError(RuntimeError):
    """An internal invariant the generator relies on was violated.

    Use this for "the code's assumption about its own state is wrong";
    use :class:`investing.sheets.SheetParseError` for "input data
    failed validation". The distinction matters because invariants
    indicate a bug the maintainer must fix, while sheet errors point
    a human at a specific cell to correct.
    """
