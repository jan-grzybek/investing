"""Command-line entrypoint: thread one shared ``ExchangeRate``
cache through the data pipeline and the renderer.
"""
from __future__ import annotations

import logging
import sys

from dateutil.relativedelta import relativedelta

from .formatting import _fmt_pct, _format_duration
from .fx import ExchangeRate
from .log import logger
from .performance import calc_twr, get_benchmarks, get_holdings, summarize
from .sheets import pull_data
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




def _print_summary(total_return: dict, holdings: dict, benchmarks: list) -> None:
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
        from datetime import datetime as _dt
        period = " over " + _format_duration(
            relativedelta(_dt.today(), start_date),
        )
    twr_part = (
        f"TWR {_fmt_pct(twr)}%" if twr is not None else "TWR n/a"
    )
    cagr_part = (
        f"CAGR {_fmt_pct(cagr)}%" if cagr is not None else "CAGR n/a"
    )
    # ``flush=True`` so the line lands in the workflow log even when
    # stdout is line-buffered against a pipe (which is the default
    # under ``actions/runner``).
    print(
        f"Build OK: {twr_part} / {cagr_part}{period}{bench_part}; "
        f"{current_count} current / {historical_count} historical holdings.",
        flush=True,
    )


def main() -> None:
    _configure_logging()
    # Single shared FX cache for the whole build: every Holding reads
    # currencies through this instance so each USD/EUR/GBp lookup
    # against yfinance happens at most once per process. The previous
    # module-level ``exchange_rate = ExchangeRate()`` singleton served
    # the same role but coupled every test run to a shared mutable
    # cache that had to be cleared via an autouse fixture; threading
    # the instance through the API removes both that test-time
    # plumbing and the (theoretical) risk of test parallelism
    # corrupting it.
    fx = ExchangeRate()
    transactions, valuations, cash = pull_data()
    holdings = get_holdings(transactions, fx=fx)
    total_return = calc_twr(valuations, summarize(holdings, cash, fx=fx))
    benchmarks = get_benchmarks(total_return["history"], fx=fx)
    generate_webpage(total_return, benchmarks, holdings)
    _print_summary(total_return, holdings, benchmarks)
