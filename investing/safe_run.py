"""Leak-safe wrapper around ``main`` for the CI workflow.
Redacts both stderr and stdout for the duration of the build and
emits a sanitized failure summary on exceptions.
"""

from __future__ import annotations

import io
import os
import sys
import traceback

# Module-level binding so the test suite can swap ``main`` for a fake
# via ``monkeypatch.setattr(investing.safe_run, "main", fake_main)``.
# ``_run_main_safely`` resolves ``main`` through this module's globals
# at call time (Python's ``LOAD_GLOBAL``), so the patch is visible.
# We also keep a reference to the ``cli`` module so the wrapper can
# park the real stdout on :data:`investing.cli._REAL_STDOUT` -- the
# render path's :func:`emit_summary` reads it from there to bypass
# the redaction. Importing ``cli`` only in this direction (and not the
# other way around) keeps the module DAG acyclic.
from . import cli
from .cli import main

# Re-export ``emit_summary`` so existing ``investing.safe_run.emit_summary``
# call sites (and the dedicated test in ``tests/test_safe_run.py``)
# keep working after the helper moved into ``investing.cli``.
emit_summary = cli.emit_summary

# ---------------------------------------------------------------------------
# Leak-safe entrypoint
# ---------------------------------------------------------------------------
#
# The CI workflow that drives this entrypoint
# (``.github/workflows/main.yml`` invokes ``python -m investing``) runs
# in a public repository, so its job logs are world-readable. The run
# handles two classes of data that must not surface there:
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


# Prefix used by the sanitized-failure summary. The historical
# ``"update.py failed: "`` predates the package having a
# ``__main__`` entrypoint; ``"investing failed: "`` matches what the
# operator now sees in the workflow log (``python -m investing``).
_FAILURE_PREFIX = "investing failed: "


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
            sys.stderr.write(f"  at {frame.filename}:{frame.lineno} in {frame.name}\n")
            if frame.line:
                sys.stderr.write(f"    {frame.line}\n")

    _emit(_FAILURE_PREFIX, exc)
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
    """Run :func:`main` with stderr fully redacted and stdout buffered.

    Stderr is redirected as before (Python-level ``sys.stderr`` AND
    file descriptor 2) so library output from C extensions can't
    leak past the Python wrapper. Stdout receives the same
    treatment: a stray ``print()`` from a transitive dependency
    (``tqdm`` progress bars, debug ``httpx`` modes, ...) cannot
    smuggle nominal portfolio values into the public job log
    either. The captured stdout is discarded on completion; the
    build's curated summary line is emitted by
    ``investing.cli._print_summary`` via
    :func:`investing.cli.emit_summary`, which writes directly to the
    stashed real stdout (parked on :data:`investing.cli._REAL_STDOUT`)
    while the redaction is in place.

    The function exits the process with status 1 on any exception
    (including ``KeyboardInterrupt`` and ``SystemExit`` with a non-zero
    code) after printing a sanitized failure summary; on success it
    returns normally so the caller can chain further work if it ever
    needs to. ``BaseException`` itself is never caught directly --
    we enumerate the two non-``Exception`` classes we care about
    (``SystemExit`` / ``KeyboardInterrupt``) so the wrapper still
    surfaces the *truly* exceptional control-flow paths Python
    reserves for itself (asyncio's ``CancelledError`` on 3.8+,
    e.g.) instead of swallowing them.
    """
    real_stderr = sys.stderr
    real_stdout = sys.stdout

    # All resources are allocated *inside* the outer try/finally so a
    # failure in any single allocation step (e.g. ``os.dup`` running
    # out of fds) doesn't leak the ones already opened. ``_restore``
    # is idempotent: it only undoes work that actually happened, so
    # calling it on a half-built setup is safe.
    devnull_py: io.TextIOWrapper | None = None
    saved_stderr_fd = -1
    saved_stdout_fd = -1
    redirected = False
    restored = False
    captured_stdout = io.StringIO()

    def _restore() -> None:
        # Idempotent so the try/finally below can call it
        # unconditionally for the rare setup-failure path without
        # double-closing fds the inner branch already cleaned up.
        nonlocal restored
        if restored:
            return
        restored = True
        cli._REAL_STDOUT = None
        sys.stderr = real_stderr
        sys.stdout = real_stdout
        if redirected:
            if saved_stderr_fd != -1:
                os.dup2(saved_stderr_fd, 2)
            if saved_stdout_fd != -1:
                os.dup2(saved_stdout_fd, 1)
        if saved_stderr_fd != -1:
            os.close(saved_stderr_fd)
        if saved_stdout_fd != -1:
            os.close(saved_stdout_fd)
        # ``devnull_py.close()`` releases the underlying fd that
        # ``dup2`` cloned onto fds 1 / 2. Both clones were already
        # overwritten by the ``dup2(saved_*_fd, ...)`` calls above,
        # so the original ``/dev/null`` open file description has
        # only ``devnull_py``'s reference left to drop.
        if devnull_py is not None:
            devnull_py.close()

    try:
        # Resources allocated incrementally so a failure midway
        # (``os.dup`` exhausting fds, for example) leaves the
        # tracking variables accurate; ``_restore`` then tears down
        # exactly what was allocated and skips the rest. A single
        # Python-level handle on ``/dev/null`` covers both halves of
        # the redaction: ``sys.stderr`` is rebound to it directly,
        # and fds 1 / 2 are ``dup2``'d from ``devnull_py.fileno()``
        # so non-Python writes (C extensions, raw ``os.write``)
        # also disappear. Sharing one fd avoids a parallel
        # ``os.open(os.devnull, ...)`` whose lifetime would have to
        # be threaded through a closure -- the construct CodeQL's
        # ``py/file-not-closed`` query (correctly) struggles to
        # prove safe.
        devnull_py = open(os.devnull, "w")  # noqa: SIM115
        saved_stderr_fd = os.dup(2)
        saved_stdout_fd = os.dup(1)

        cli._REAL_STDOUT = real_stdout
        os.dup2(devnull_py.fileno(), 2)
        os.dup2(devnull_py.fileno(), 1)
        redirected = True
        sys.stderr = devnull_py
        sys.stdout = captured_stdout

        try:
            main()
        except SystemExit as exc:
            _restore()
            # Preserve an explicit ``sys.exit(0)`` from inside
            # ``main``; only synthesise a sanitized report when the
            # exit signals failure.
            code = exc.code if isinstance(exc.code, int) else 1
            if code != 0:
                _print_sanitized_failure(exc)
                sys.exit(1)
            return
        except (Exception, KeyboardInterrupt) as exc:
            _restore()
            _print_sanitized_failure(exc)
            sys.exit(1)
    finally:
        # Belt-and-braces cleanup for the setup-failure path (the
        # inner ``except`` blocks already restored on the handled
        # paths, and ``_restore`` is idempotent).
        _restore()
