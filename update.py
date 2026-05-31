"""Backwards-compatibility shim for the legacy single-file entrypoint.

The page generator now lives in the :mod:`investing` package. This
module re-exports the names that ``preview.py``, the test suite, and
any external consumers historically imported from ``update``. New code
should import directly from the relevant ``investing.<module>``.

The shim also keeps the production CLI surface stable: ``python
update.py`` still runs the leak-safe wrapper that the GitHub Actions
workflow invokes.
"""
from __future__ import annotations

# Module-level names that tests historically patch via
# ``monkeypatch.setattr(update, <name>, ...)``. Keeping them here as
# top-level imports preserves the patch surface for the test suite
# (datetime stubs for ``freeze_today``, ``yf`` / ``requests`` /
# ``gspread`` for the network mocks).
from datetime import datetime  # noqa: F401

import gspread  # noqa: F401
import numpy as np  # noqa: F401
import requests  # noqa: F401
import yfinance as yf  # noqa: F401

# Public API re-exports. Wildcarded by module so any private name a
# test reaches for (``_PAGE_STYLES``, ``_HASH_CLEAR_SCRIPT``,
# ``_fmt_pct``, ``_TRADE_ACTION_DISPLAY``, etc.) stays addressable.
from investing.assets import (  # noqa: F401
    _HASH_CLEAR_SCRIPT,
    _HOLDINGS_SORT_SCRIPT,
    _NAV_SCROLL_SCRIPT,
    _PAGE_STYLES,
    _RETURN_CHART_SCRIPT,
    _TRADES_SORT_SCRIPT,
)
from investing.cli import _configure_logging, main  # noqa: F401
from investing.formatting import (  # noqa: F401
    _fmt_date,
    _fmt_date_long,
    _fmt_pct,
    _fmt_quarter_range,
    _format_duration,
    _format_sort_number,
    _pluralize,
    _quarter_of,
    _sha256_b64,
    _ts_to_datetime,
    _value_class,
)
from investing.fx import ExchangeRate, FxRate, _fx_or_default  # noqa: F401
from investing.holdings import (  # noqa: F401
    CAGR_TBA_THRESHOLD,
    DAYS_YEAR,
    WITHHOLDING_TAX_RATE,
    Holding,
)
from investing.log import logger  # noqa: F401
from investing.paths import (  # noqa: F401
    _ASSETS_DIR,
    _REPO_DIR,
    _REPO_LOGOS_DIR,
    COURAGE_LOGO,
    LOGO_EXTENSIONS,
    LOGOS_ADDRESS,
    _read_asset,
)
from investing.performance import (  # noqa: F401
    _BENCHMARK_DISPLAY_NAMES,
    Benchmark,
    calc_twr,
    get_benchmarks,
    get_holdings,
    summarize,
)
from investing.safe_run import _print_sanitized_failure, _run_main_safely  # noqa: F401
from investing.sheets import (  # noqa: F401
    _BUY_TOKENS,
    _SCHEMAS,
    _SELL_TOKENS,
    _SHEET_DATA_OFFSET,
    _YES_TOKENS,
    SheetParseError,
    _check_row_shape,
    _gspread_client,
    _iter_data_rows,
    _parse_cash_row,
    _parse_equity_row,
    _parse_return_row,
    _to_float,
    _to_int,
    pull_data,
)
from investing.trades import (  # noqa: F401
    _BUY_CATEGORIES,
    _TRADE_ACTION_DISPLAY,
    _TRADE_DETAIL_LABELS,
    ACTIONS,
    TRADE_WINDOW_DAYS,
    Trade,
    _combine_trade_events,
    combine_and_sort,
)
from investing.webpage import Webpage, generate_webpage  # noqa: F401

if __name__ == "__main__":
    _run_main_safely()
