"""Command-line entrypoint: thread one shared ``ExchangeRate``
cache through the data pipeline and the renderer.

The pipeline is exposed as :func:`build_page`: an injectable
orchestrator that takes data providers (``pull``, ``fx``, ``now``,
``save``) as parameters so test harnesses and the local preview
helper at ``scripts/preview.py`` can share the same composition
as production while substituting their own data sources.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from dateutil.relativedelta import relativedelta

from .clock import NowFn
from .formatting import _fmt_pct, _format_duration
from .fx import ExchangeRate, FxRate
from .log import logger
from .maintenance_notifier import NotifierOutcome, notify_github
from .performance import (
    apply_rollup,
    calc_twr,
    compute_rollup,
    get_benchmarks,
    get_holdings,
)
from .sector_overrides import (
    MaintenanceHints,
    append_missing_sector_stubs,
    consume_hints,
    reset_hints,
)
from .sheets import pull_data as _pull_data
from .types import (
    BenchmarkSummary,
    CashBalance,
    EquityTransaction,
    HoldingsRollup,
    TotalReturn,
    Valuation,
)
from .webpage import generate_webpage

# Stashed reference to the operator's real stdout while the leak-safe
# wrapper (:func:`investing.safe_run._run_main_safely`) is active. The
# wrapper assigns this attribute before redirecting ``sys.stdout`` to a
# StringIO and clears it after restoring stdout. :func:`emit_summary`
# consults the stash to write the curated build line directly to the
# original terminal / job log, bypassing the StringIO that's currently
# masquerading as ``sys.stdout``. Outside the wrapper (e.g. unit tests)
# it stays ``None`` and :func:`emit_summary` writes to whatever
# ``sys.stdout`` happens to be, which lets ``capsys`` capture summary
# output normally. Lives in ``cli`` (rather than ``safe_run``) so the
# render path doesn't have to import back into ``safe_run`` -- breaking
# the historical ``cli`` <-> ``safe_run`` import cycle.
_REAL_STDOUT: TextIO | None = None


def emit_summary(line: str) -> None:
    """Write the curated build-summary line to the real stdout.

    Routes around the redaction in
    :func:`investing.safe_run._run_main_safely`: when the wrapper is
    active, writes land on the stashed real stdout (visible in the job
    log); when the wrapper is inactive, writes follow ``sys.stdout`` so
    ``capsys``-style test capture continues to work. ``flush`` is
    unconditional because job-log streams are line-buffered against a
    pipe and a missing flush could swallow the line on a fast process
    exit.
    """
    stream = _REAL_STDOUT if _REAL_STDOUT is not None else sys.stdout
    stream.write(line)
    stream.flush()


def _configure_logging(level: int = logging.INFO) -> None:
    """Attach a stderr handler to ``investing.update`` if none is set.

    The production entrypoint (``_run_main_safely``) redirects stderr
    fd 2 to ``/dev/null`` for the duration of ``main()``, so any output
    these handlers emit is silently dropped in CI -- which is exactly
    what we want for portfolio identifiers / nominal amounts that the
    diagnostic lines below carry. Local invocations of
    ``python -m investing`` keep stderr connected to the terminal, so
    the same handler surfaces useful progress on a developer's machine
    where the previous ``print()`` calls used to land. Skips
    installation if the logger already has handlers attached (e.g.
    when a test runner configured logging up front) to avoid
    duplicating messages.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def _format_maintenance_hints(hints: MaintenanceHints) -> str:
    """Render :class:`MaintenanceHints` into a one-line summary fragment.

    Returns the empty string when the hints object has no entries so
    the caller can splice the output into :func:`_print_summary`'s
    line without an extra branch. Each non-empty category produces a
    semicolon-separated chunk of ``"label: ticker, ticker, ..."``.

    Ticker symbols are public (they appear in the rendered page
    body, in ``logos/`` filenames, and in the sitemap entries) so
    surfacing them through the curated build summary -- which
    bypasses the leak-safe wrapper's stdout redaction via
    :func:`emit_summary` -- does not breach the privacy contract the
    rest of the build observes. The maintainer workflow this enables:
    a fork's owner reads the CI log, sees ``Maintenance: ...``,
    and lands a follow-up PR with the matching
    ``sector_overrides.toml`` / ``logos/`` updates.
    """
    parts: list[str] = []
    if hints.missing_sector:
        parts.append("missing sectors: " + ", ".join(hints.missing_sector))
    if hints.invalid_overrides:
        invalid = ", ".join(
            f"{t}={v!r}" for t, v in sorted(hints.invalid_overrides.items())
        )
        parts.append(f"invalid sector overrides: {invalid}")
    if hints.missing_logos:
        parts.append("missing logos: " + ", ".join(hints.missing_logos))
    if not parts:
        return ""
    return " | ".join(parts)


def _format_notifier_outcome(outcome: NotifierOutcome) -> str:
    """Render the notifier counters into a one-line status fragment.

    Returns the empty string when the notifier was disabled OR ran
    cleanly with nothing to report (no hints at all). Both cases
    suppress the line so a clean build stays terse and a local
    ``python -m investing`` run (no env vars set) doesn't surface a
    "notifier: 0 opened" line that would be confusing without the
    CI context.

    A non-empty return ALWAYS prefixes ``"Notifier: "`` so the
    operator can grep the public job log for it without ambiguity
    when triaging a missing-issue report (the failure mode that
    motivated this format: a repo with Issues turned off shows up
    here as ``failed: N`` instead of the previous total opacity).
    """
    if not outcome.enabled or outcome.is_empty:
        return ""
    chunks: list[str] = []
    if outcome.opened:
        chunks.append(
            f"{len(outcome.opened)} opened ("
            + ", ".join(outcome.opened)
            + ")"
        )
    if outcome.already_tracked:
        chunks.append(f"{len(outcome.already_tracked)} already tracked")
    if outcome.failed:
        # Failures get the verbose ticker list because that's the
        # debugging surface the operator needs to investigate (was
        # it auth? was it Issues being disabled on the repo? is the
        # API down for this one endpoint?).
        chunks.append(
            f"{len(outcome.failed)} failed ("
            + ", ".join(outcome.failed)
            + ")"
        )
    return " | ".join(chunks)


def _format_appended_stubs(stubs: list[str]) -> str:
    """Render the auto-populate counter into a one-line status fragment.

    Returns the empty string when no stub was appended so a clean
    build (or a CI run on a runner whose filesystem changes get
    discarded anyway) doesn't surface a noisy line. The maintainer
    on a local checkout reads the resulting "Auto-populated: ..."
    note as a cue to open ``sector_overrides.toml`` and uncomment
    the freshly added entries.
    """
    if not stubs:
        return ""
    return ", ".join(stubs)


def _print_summary(
    total_return: TotalReturn,
    holdings: HoldingsRollup,
    benchmarks: list[BenchmarkSummary],
    *,
    now: NowFn = datetime.today,
    maintenance: MaintenanceHints | None = None,
    notifier: NotifierOutcome | None = None,
    appended_stubs: list[str] | None = None,
) -> None:
    """Emit a redacted one-line build summary on stdout.

    The leak-safe wrapper in :mod:`investing.safe_run` redirects
    ``sys.stderr`` (and fd 2) for the duration of ``main``, but
    leaves stdout untouched. The diagnostic stream that logger sends
    is therefore invisible in CI logs by design -- which is the
    right default for the bulk of pipeline output, since it carries
    identifiers and values that mustn't leak.

    The summary printed here is a deliberate, hand-formatted line
    composed exclusively of quantities the rendered page also
    publishes (TWR + CAGR percentages, holding counts). It gives the
    GitHub Actions log a positive signal beyond "exit-code 0"
    without surfacing any of the privacy-sensitive material the
    redaction is in place to protect.
    """
    twr = total_return.get("twr%")
    cagr = total_return.get("cagr%")
    start_date = total_return.get("start_date")
    current_count = len(holdings.get("current", []) or [])
    historical_count = len(holdings.get("historical", []) or [])
    bench_part = ""
    if benchmarks:
        bench = benchmarks[0]
        bench_cagr = bench.get("cagr%")
        if bench_cagr is not None and cagr is not None:
            bench_part = (
                f" vs benchmark CAGR {_fmt_pct(bench_cagr)}% "
                f"(delta {_fmt_pct(cagr - bench_cagr, signed=True)} pp)"
            )
    period = ""
    if start_date is not None:
        period = " over " + _format_duration(
            relativedelta(now(), start_date),
        )
    twr_part = f"TWR {_fmt_pct(twr)}%" if twr is not None else "TWR n/a"
    cagr_part = f"CAGR {_fmt_pct(cagr)}%" if cagr is not None else "CAGR n/a"
    line = (
        f"Build OK: {twr_part} / {cagr_part}{period}{bench_part}; "
        f"{current_count} current / {historical_count} historical holdings.\n"
    )
    # Route the curated summary through :func:`emit_summary` so it
    # lands on the real stdout while the leak-safe wrapper's
    # redaction is active (stray ``print`` calls from transitive
    # dependencies go to a discarded StringIO during
    # ``_run_main_safely``). Outside the wrapper (tests / direct
    # callers) this falls through to ``sys.stdout`` so existing
    # ``capsys`` consumers continue to see the line.
    emit_summary(line)
    if maintenance is not None and not maintenance.is_empty:
        # Maintenance hints ride on a separate line so they only
        # appear when there's something to fix and the existing
        # "Build OK: ..." line stays diff-stable across clean runs.
        # The body is composed entirely of ticker symbols (already
        # public) and category labels, so it is safe to emit on the
        # real stdout under the leak-safe wrapper.
        emit_summary(f"Maintenance: {_format_maintenance_hints(maintenance)}\n")
    if appended_stubs:
        # Local-dev convenience: the auto-populate hook appended
        # commented-out stubs to ``sector_overrides.toml`` for the
        # missing tickers; let the operator know which lines to
        # uncomment + fill in. On CI the runner's filesystem
        # changes don't persist so this line is mostly informational
        # there, but the same ticker list also shows up in any
        # GitHub issue the notifier files, so the two signals
        # complement rather than duplicate each other.
        emit_summary(
            "Auto-populated sector_overrides.toml stubs for: "
            + _format_appended_stubs(appended_stubs)
            + "\n"
        )
    if notifier is not None:
        notifier_line = _format_notifier_outcome(notifier)
        if notifier_line:
            emit_summary(f"Notifier: {notifier_line}\n")


# Pure data-source signature: ``pull()`` returns the same triple as
# ``investing.sheets.pull_data``. Lifted as a named type so test
# harnesses, preview scripts, and the production entrypoint all
# share one shape rather than depending on the module-level callable.
PullFn = Callable[
    [],
    tuple[list[EquityTransaction], list[Valuation], list[CashBalance]],
]

# Rendering side-effect: takes the three dicts the page consumes plus
# an explicit output directory, and writes the artefacts
# (``index.html``, ``og-image.png``, ``sitemap.xml``, ``robots.txt``)
# under it. Defaulting to :func:`generate_webpage` keeps production
# behaviour while letting the preview helper swap in a stub that
# targets a different directory. ``output_dir`` is keyword-only so
# the signature stays compatible with legacy callers that pass three
# positional arguments and accept the CWD fallback.
SaveFn = Callable[..., None]


def build_page(
    *,
    pull: PullFn | None = None,
    fx: FxRate | None = None,
    now: NowFn | None = None,
    save: SaveFn | None = None,
    output_dir: Path | None = None,
) -> None:
    """Run the data pipeline + renderer, with every IO step injectable.

    Production calls :func:`main`, which wires the real Google Sheets
    loader, a live :class:`ExchangeRate` cache and the
    :func:`generate_webpage` renderer. Tests / preview helpers can
    pass their own ``pull`` / ``fx`` / ``save`` callables to exercise
    the orchestrator end-to-end against synthetic data, without
    rewriting the composition that production goes through.

    ``output_dir`` is forwarded to the renderer via the ``save``
    callable's keyword argument; ``None`` falls back to the current
    working directory so existing test paths and the production
    workflow (which writes alongside the repo checkout) keep working
    unchanged.

    All arguments are keyword-only and default to the production
    wiring so a bare ``build_page()`` call matches what ``python -m
    investing`` did before this refactor.
    """
    _pull = pull if pull is not None else _pull_data
    _save: SaveFn = save if save is not None else generate_webpage
    _now: NowFn = now if now is not None else datetime.today
    # Single shared FX cache for the whole build: every Holding reads
    # currencies through this instance so each USD/EUR/GBp lookup
    # against yfinance happens at most once per process.
    _fx: FxRate = fx if fx is not None else ExchangeRate()

    # Maintenance hint registry is process-scoped, so a long-lived
    # caller (e.g. the test suite, or a hypothetical future
    # ``build_page`` invoked twice in one process) starts each run
    # with a clean slate. The summary helper drains the registry at
    # the very end of the build so any hints recorded between here
    # and there are captured in the curated build summary line.
    reset_hints()

    transactions, valuations, cash = _pull()
    holdings = get_holdings(transactions, fx=_fx, now=_now)
    rollup = compute_rollup(holdings, cash, fx=_fx)
    apply_rollup(holdings, rollup)
    total_return = calc_twr(valuations, rollup.total_value_usd, now=_now)
    benchmarks = get_benchmarks(total_return["history"], fx=_fx, now=_now)
    _save(total_return, benchmarks, holdings, output_dir=output_dir)
    # Drain the hint registry once and share the snapshot between
    # every downstream consumer: the curated build summary
    # (always-on, public stdout), the auto-populate hook (writes
    # commented stubs into ``sector_overrides.toml``), and the
    # GitHub-Issues notifier (opt-in via ``INVESTING_NOTIFY_GITHUB``).
    # Draining twice would lose hints because ``consume_hints``
    # clears the underlying registry as a side effect; passing the
    # same snapshot keeps the consumers in lockstep without leaking
    # the abstraction.
    hints = consume_hints()
    # Auto-populate runs FIRST so the build summary's "appended"
    # line reflects the file as it actually exists on disk after
    # the build. The hook is a no-op when the TOML file is absent
    # (a fresh fork) or when every ticker already has a stub
    # (idempotent across rebuilds).
    appended_stubs = append_missing_sector_stubs(hints.missing_sector)
    # Notifier runs SECOND so its return value (filed / already-
    # tracked / failed counts) is available to ``_print_summary``
    # below. The order matters only for the call sequencing -- the
    # outcome itself doesn't depend on whether the auto-populate
    # hook ran first or second.
    notifier_outcome = notify_github(hints)
    _print_summary(
        total_return,
        holdings,
        benchmarks,
        now=_now,
        maintenance=hints,
        notifier=notifier_outcome,
        appended_stubs=appended_stubs,
    )


def main() -> None:
    """Production entrypoint: real Sheets loader, real FX cache."""
    _configure_logging()
    build_page()
