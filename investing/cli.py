"""Command-line entrypoint: thread one shared ``ExchangeRate``
cache through the data pipeline and the renderer.
"""
from __future__ import annotations

import logging
import sys

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
    diagnostic lines below carry. Local invocations of ``python
    update.py`` keep stderr connected to the terminal, so the same
    handler now surfaces useful progress on a developer's machine
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




def main():
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
