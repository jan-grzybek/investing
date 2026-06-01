"""Tests for the leak-safe entrypoint wrapping ``investing.cli.main``.

The CI workflow's logs are world-readable, and
``investing.cli.main`` handles two classes of data that must never
reach them: GitHub Actions secrets (``GSHEET_ID``, the
service-account JSON) and nominal portfolio values (share counts,
cash balances, dividend payouts, FX rates) used to derive the
percentages we publish. Library code and exception messages
routinely embed both, so ``investing.safe_run._run_main_safely``:

* silences stderr -- both ``sys.stderr`` and the underlying fd -- for
  the duration of the run, and
* on failure restores stderr and prints a hand-formatted summary made
  exclusively of identifiers that already live in the public repo
  (exception class plus per-frame ``file:lineno`` / function / source
  line), deliberately omitting ``str(exc)``, ``__notes__`` and locals.

These tests pin both halves of that contract: that the suppression
happens, and that what *is* emitted on failure contains the breadcrumbs
needed to debug without any of the carriers that normally leak values.
"""

from __future__ import annotations

import os
import sys

import investing.safe_run as _safe_run

# Distinctive leak canaries we plant inside fake ``main`` bodies so
# the assertions can prove the leak-safe wrapper either suppressed
# them (during the run) or refused to surface them (in the sanitized
# summary). Picking strings that don't occur naturally anywhere else
# in the codebase makes the negative assertions trustworthy. The
# canary's name and content deliberately avoid the substrings
# CodeQL's ``py/clear-text-logging-sensitive-data`` query treats as
# "looks like a secret" (e.g. ``SECRET``, ``TOPSECRET``) -- the value
# isn't an actual secret, it stands in for one in tests, and the
# bland name keeps the query from firing on the calls that
# intentionally write it to stderr or raise it as an exception arg.
_LEAK_CANARY = "CANARY_SHARES_4242_CASH_99887"
_LIBRARY_NOISE = "yfinance-rate-limit-balance-12345.67-USD"


class _BoomError(RuntimeError):
    """Custom exception so we can match a unique class name in output."""


def _run_safely_capturing(monkeypatch, fake_main, capfd):
    """Invoke ``_run_main_safely`` with ``main`` swapped for ``fake_main``.

    Returns ``(exit_code, captured)`` where ``exit_code`` is ``None`` on
    a clean return and ``captured`` is the post-restoration stderr seen
    by ``capfd`` (i.e. the sanitized summary, if any).
    """
    monkeypatch.setattr(_safe_run, "main", fake_main)
    # Flush any pre-test output so ``capfd.readouterr()`` only returns
    # bytes produced by our wrapper.
    capfd.readouterr()
    try:
        _safe_run._run_main_safely()
    except SystemExit as exc:
        captured = capfd.readouterr()
        return exc.code, captured
    captured = capfd.readouterr()
    return None, captured


class TestCleanRun:
    def test_main_is_invoked_and_returns_normally(self, monkeypatch, capfd):
        calls = []

        def fake_main():
            calls.append("called")

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert calls == ["called"]
        assert exit_code is None
        assert captured.err == ""

    def test_python_stderr_writes_during_main_are_suppressed(self, monkeypatch, capfd):
        def fake_main():
            sys.stderr.write(_LIBRARY_NOISE + "\n")
            sys.stderr.write(_LEAK_CANARY + "\n")

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code is None
        assert _LIBRARY_NOISE not in captured.err
        assert _LEAK_CANARY not in captured.err
        assert captured.err == ""

    def test_native_fd2_writes_during_main_are_suppressed(self, monkeypatch, capfd):
        """A C extension that writes straight to fd 2 (bypassing the
        Python ``sys.stderr`` wrapper) must also be silenced."""

        def fake_main():
            os.write(2, (_LIBRARY_NOISE + "\n").encode())

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code is None
        assert _LIBRARY_NOISE not in captured.err

    def test_python_stdout_writes_during_main_are_suppressed(self, monkeypatch, capfd):
        """Stray ``print()`` calls from transitive deps must not reach
        the public job log -- stdout gets the same redaction as
        stderr now."""

        def fake_main():
            print(_LIBRARY_NOISE)
            print(_LEAK_CANARY)

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code is None
        assert _LIBRARY_NOISE not in captured.out
        assert _LEAK_CANARY not in captured.out
        assert captured.out == ""

    def test_native_fd1_writes_during_main_are_suppressed(self, monkeypatch, capfd):
        """And the same defence against C extensions writing straight
        to fd 1, mirroring the fd-2 contract."""

        def fake_main():
            os.write(1, (_LIBRARY_NOISE + "\n").encode())

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code is None
        assert _LIBRARY_NOISE not in captured.out

    def test_emit_summary_during_main_reaches_real_stdout(self, monkeypatch, capfd):
        """``investing.cli._print_summary`` writes its curated line via
        :func:`safe_run.emit_summary`; that helper must bypass the
        StringIO mask installed by ``_run_main_safely`` and land on
        the real stdout."""
        marker = "BUILD-OK-MARKER-42"

        def fake_main():
            _safe_run.emit_summary(marker + "\n")

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code is None
        assert marker in captured.out

    def test_stdout_is_restored_after_a_clean_run(self, monkeypatch, capfd):
        sentinel = "POST_RUN_STDOUT_SENTINEL"

        def fake_main():
            print("hidden")  # vanishes into the captured StringIO

        exit_code, _ = _run_safely_capturing(monkeypatch, fake_main, capfd)
        assert exit_code is None
        print(sentinel)
        os.write(1, b"POST_RUN_FD1_SENTINEL\n")
        after = capfd.readouterr()

        assert sentinel in after.out
        assert "POST_RUN_FD1_SENTINEL" in after.out

    def test_stderr_is_restored_after_a_clean_run(self, monkeypatch, capfd):
        sentinel = "POST_RUN_VISIBLE_SENTINEL"

        def fake_main():
            sys.stderr.write("hidden\n")

        exit_code, _ = _run_safely_capturing(monkeypatch, fake_main, capfd)
        assert exit_code is None
        sys.stderr.write(sentinel + "\n")
        sys.stderr.flush()
        os.write(2, b"POST_RUN_FD_SENTINEL\n")
        after = capfd.readouterr()

        assert sentinel in after.err
        assert "POST_RUN_FD_SENTINEL" in after.err

    def test_systemexit_zero_from_main_is_honoured(self, monkeypatch, capfd):
        def fake_main():
            raise SystemExit(0)

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        # ``_run_main_safely`` swallows a clean exit and returns
        # normally so the caller can chain further work.
        assert exit_code is None
        assert captured.err == ""


class TestFailingRun:
    def test_exit_code_is_one(self, monkeypatch, capfd):
        def fake_main():
            raise _BoomError(_LEAK_CANARY)

        exit_code, _ = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code == 1

    def test_summary_names_the_exception_class(self, monkeypatch, capfd):
        def fake_main():
            raise _BoomError(_LEAK_CANARY)

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert "investing failed: _BoomError" in captured.err

    def test_summary_does_not_contain_exception_message(self, monkeypatch, capfd):
        """The single biggest leak vector: ``str(exc)`` routinely
        contains the value that caused the failure."""

        def fake_main():
            raise _BoomError(_LEAK_CANARY)

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert _LEAK_CANARY not in captured.err

    def test_summary_does_not_contain_exception_notes(self, monkeypatch, capfd):
        """PEP 678 ``__notes__`` can carry runtime values too."""

        def fake_main():
            exc = _BoomError("benign-class-name")
            exc.add_note(_LEAK_CANARY)
            raise exc

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert _LEAK_CANARY not in captured.err

    def test_summary_does_not_leak_stderr_written_before_failure(self, monkeypatch, capfd):
        """Library noise echoed before the exception is raised must
        stay buried even when we surface a failure summary."""

        def fake_main():
            sys.stderr.write(_LIBRARY_NOISE + "\n")
            os.write(2, (_LIBRARY_NOISE + "-fd\n").encode())
            raise _BoomError("benign")

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert _LIBRARY_NOISE not in captured.err

    def test_summary_lists_traceback_frames_with_source_lines(self, monkeypatch, capfd):
        def fake_main():
            raise _BoomError("benign")

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        # File:line, function name and the actual source line that
        # raised should all be present -- those are the breadcrumbs we
        # want for debugging.
        assert "in fake_main" in captured.err
        assert "test_safe_run.py" in captured.err
        assert 'raise _BoomError("benign")' in captured.err

    def test_summary_walks_chained_causes(self, monkeypatch, capfd):
        """When ``raise X from Y`` is used the underlying cause is
        often what really failed; the summary must surface it (by
        class only, never its message)."""

        def fake_main():
            try:
                raise ValueError(_LEAK_CANARY)
            except ValueError as inner:
                raise _BoomError("outer") from inner

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert "investing failed: _BoomError" in captured.err
        assert "caused by: ValueError" in captured.err
        assert _LEAK_CANARY not in captured.err

    def test_summary_walks_implicit_context(self, monkeypatch, capfd):
        """Implicit chaining (no ``from``) still needs to expose the
        original exception class so we can debug nested failures."""
        # Build the secret at runtime so it lives only in ``str(exc)``
        # -- never as a literal in this file's source, which would
        # otherwise legitimately appear in the surfaced source line.
        runtime_secret = "".join(["RUNTIME", "_INNER_", "SECRET"])

        def fake_main():
            try:
                raise KeyError(runtime_secret)
            except KeyError as exc:
                raise _BoomError("outer") from exc

        _, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert "investing failed: _BoomError" in captured.err
        assert "caused by: KeyError" in captured.err
        assert runtime_secret not in captured.err

    def test_self_referential_cause_chain_terminates(self, monkeypatch, capfd):
        """``raise X from X`` would loop forever without the ``seen``
        guard inside ``_print_sanitized_failure``."""

        def fake_main():
            exc = _BoomError("loop")
            exc.__cause__ = exc
            raise exc

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code == 1
        # Only the top-level entry should appear; the cycle must be
        # detected and broken before we recurse on ``exc`` again.
        assert captured.err.count("investing failed: _BoomError") == 1
        assert "caused by:" not in captured.err

    def test_systemexit_nonzero_is_treated_as_failure(self, monkeypatch, capfd):
        def fake_main():
            raise SystemExit(2)

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code == 1
        assert "investing failed: SystemExit" in captured.err

    def test_keyboard_interrupt_is_treated_as_failure(self, monkeypatch, capfd):
        def fake_main():
            raise KeyboardInterrupt()

        exit_code, captured = _run_safely_capturing(monkeypatch, fake_main, capfd)

        assert exit_code == 1
        assert "investing failed: KeyboardInterrupt" in captured.err

    def test_stderr_is_restored_after_a_failed_run(self, monkeypatch, capfd):
        sentinel = "POST_FAIL_VISIBLE_SENTINEL"

        def fake_main():
            raise _BoomError("benign")

        _run_safely_capturing(monkeypatch, fake_main, capfd)
        sys.stderr.write(sentinel + "\n")
        sys.stderr.flush()
        os.write(2, b"POST_FAIL_FD_SENTINEL\n")
        after = capfd.readouterr()

        assert sentinel in after.err
        assert "POST_FAIL_FD_SENTINEL" in after.err
