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

from dateutil.relativedelta import relativedelta

from .clock import NowFn
from .formatting import _fmt_pct, _format_duration
from .fx import ExchangeRate, FxRate
from .log import logger
from .performance import (
    apply_rollup,
    calc_twr,
    compute_rollup,
    get_benchmarks,
    get_holdings,
)
from .sheets import pull_data as _pull_data
from .types import CashBalance, EquityTransaction, Valuation
from .webpage import generate_webpage


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


def _print_summary(
    total_return: dict,
    holdings: dict,
    benchmarks: list,
    *,
    now: NowFn = datetime.today,
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
    # Route the curated summary through the leak-safe wrapper's
    # :func:`emit_summary` so it lands on the real stdout while
    # the redaction is active (stray ``print`` calls from
    # transitive dependencies go to a discarded StringIO during
    # ``_run_main_safely``). Outside the wrapper (tests / direct
    # callers) this falls through to ``sys.stdout`` so existing
    # ``capsys`` consumers continue to see the line.
    from . import safe_run as _safe_run

    _safe_run.emit_summary(line)


# Pure data-source signature: ``pull()`` returns the same triple as
# ``investing.sheets.pull_data``. Lifted as a named type so test
# harnesses, preview scripts, and the production entrypoint all
# share one shape rather than depending on the module-level callable.
PullFn = Callable[
    [],
    tuple[list[EquityTransaction], list[Valuation], list[CashBalance]],
]

# Rendering side-effect: takes the three dicts the page consumes and
# writes the artefacts (``index.html``, ``og-image.png``, ...) to disk.
# Defaulting to :func:`generate_webpage` keeps production behaviour
# while letting the preview helper swap in a stub that targets a
# different directory.
SaveFn = Callable[[dict, list, dict], None]


def build_page(
    *,
    pull: PullFn | None = None,
    fx: FxRate | None = None,
    now: NowFn | None = None,
    save: SaveFn | None = None,
) -> None:
    """Run the data pipeline + renderer, with every IO step injectable.

    Production calls :func:`main`, which wires the real Google Sheets
    loader, a live :class:`ExchangeRate` cache and the
    :func:`generate_webpage` renderer. Tests / preview helpers can
    pass their own ``pull`` / ``fx`` / ``save`` callables to exercise
    the orchestrator end-to-end against synthetic data, without
    rewriting the composition that production goes through.

    All four arguments are keyword-only and default to the production
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

    transactions, valuations, cash = _pull()
    holdings = get_holdings(transactions, fx=_fx, now=_now)
    rollup = compute_rollup(holdings, cash, fx=_fx)
    apply_rollup(holdings, rollup)
    total_return = calc_twr(valuations, rollup.total_value_usd, now=_now)
    benchmarks = get_benchmarks(total_return["history"], fx=_fx, now=_now)
    _save(total_return, benchmarks, holdings)
    _print_summary(total_return, holdings, benchmarks, now=_now)


def main() -> None:
    """Production entrypoint: real Sheets loader, real FX cache."""
    _configure_logging()
    build_page()
