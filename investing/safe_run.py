"""Leak-safe wrapper around ``main`` for the CI workflow.
Redacts stderr for the duration of the build and emits a
sanitized failure summary on exceptions.
"""
from __future__ import annotations

import os
import sys
import traceback

# Module-level binding so the test suite can swap ``main`` for a fake
# via ``monkeypatch.setattr(investing.safe_run, "main", fake_main)``.
# ``_run_main_safely`` reads this attribute through the module
# namespace (``main()``), not a locally-captured reference, so the
# patch is visible at call time.
from .cli import main  # noqa: F401  (re-bound; used via module namespace below)

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
    # Lifecycle is deliberately spread across the function: opened
    # here, closed inside ``_restore`` so it stays alive for the
    # duration of ``main()`` while ``sys.stderr`` is pointed at it.
    # A ``with`` block would close it on early returns inside the
    # function, defeating the whole point of the redaction.
    devnull_py = open(os.devnull, "w")  # noqa: SIM115
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

    # Read ``main`` through the module namespace so a test-time
    # ``monkeypatch.setattr(_safe_run, "main", fake)`` is honoured.
    from . import safe_run as _self

    os.dup2(devnull_fd, 2)
    sys.stderr = devnull_py
    try:
        _self.main()
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
