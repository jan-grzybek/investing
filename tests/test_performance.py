"""Tests for ``investing.performance`` -- specifically the
:class:`Benchmark` class and the contract between the per-benchmark
TSR (in the comparison capsule) and the chart's right-edge sample.

The other rollup helpers in ``performance.py`` (``get_holdings``,
``calc_twr``, ``summarize``) are covered by their own dedicated
modules (``test_holding.py``, ``test_calc_twr.py``, ``test_summarize.py``).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import investing.holdings as _holdings
from investing.performance import Benchmark


def _date_key(d: datetime) -> str:
    """Mirror the yfinance string format the production parsers expect."""
    return d.strftime("%Y-%m-%d 00:00:00")


def _make_benchmark_ticker(
    *,
    price: float,
    history: dict[datetime, tuple[float, float]],
    currency: str = "USD",
    symbol: str = "VUAA.L",
    exchange: str = "LSE",
    long_name: str = "Vanguard S&P 500 UCITS ETF",
    splits: dict | None = None,
    dividends: dict | None = None,
    index_tz: str | None = None,
):
    """Build a ``yf.Ticker``-shaped mock whose ``history()`` returns a
    DataFrame with the columns ``Benchmark.__init__`` reads.

    ``history`` maps trading-day datetime -> ``(open, adj_close)``;
    the DataFrame's index is built from the dict keys (in insertion
    order) so the test controls the timeline directly.

    ``index_tz`` (e.g. ``"Europe/London"``) localises the synthetic
    DatetimeIndex to mirror what real yfinance returns for an
    exchange-listed ticker. The default ``None`` keeps the index
    tz-naive so legacy tests, which were written before the
    timezone-aware contract was reproduced in fixtures, continue to
    exercise the same code path they always did.
    """
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.get_info.return_value = {
        "currency": currency,
        "exchange": exchange,
        "symbol": symbol,
        "longName": long_name,
        "regularMarketPrice": price,
    }
    mock.splits = splits or {}
    mock.get_dividends.return_value = dividends or {}
    dates = list(history.keys())
    opens = [history[d][0] for d in dates]
    adj_closes = [history[d][1] for d in dates]
    index = pd.DatetimeIndex(dates)
    if index_tz is not None:
        index = index.tz_localize(index_tz)
    frame = pd.DataFrame(
        {"Open": opens, "Adj Close": adj_closes},
        index=index,
    )
    mock.history.return_value = frame
    return mock


@pytest.fixture
def install_ticker(monkeypatch):
    """Patch ``yf.Ticker`` so :class:`Holding` (and therefore
    :class:`Benchmark`, which composes a ``Holding``) wires up to
    the mock instead of hitting Yahoo."""
    def _install(ticker_mock):
        monkeypatch.setattr(
            _holdings.yf,
            "Ticker",
            lambda symbol: ticker_mock,  # noqa: ARG005
        )
        return ticker_mock

    return _install


class TestBenchmarkStartBasisPrice:
    def test_uses_adjusted_close_on_first_trading_day(
        self, install_ticker, stub_exchange_rate
    ):
        # Open=150, Adj Close=152 on the start day. The basis used
        # for both the chart curve's denominator and the synthetic
        # 1-share trade must be the Adj Close (152), not the Open.
        install_ticker(_make_benchmark_ticker(
            price=200.0,
            history={
                datetime(2024, 1, 2): (150.0, 152.0),
                datetime(2024, 6, 3): (180.0, 180.0),
                datetime(2024, 12, 31): (190.0, 190.0),
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        assert benchmark.start_basis_price == pytest.approx(152.0)

    def test_basis_is_split_adjusted_on_the_start_day(
        self, install_ticker, stub_exchange_rate
    ):
        # If the response's Adj Close column already back-adjusts the
        # start-day close for a later split (Yahoo's contract under
        # ``auto_adjust=False``), the basis must reflect that
        # adjustment, not the raw Open which Yahoo leaves unscaled.
        install_ticker(_make_benchmark_ticker(
            price=200.0,
            # On 2024-01-02 the raw open was 300 but the close was
            # already-back-adjusted to 150 for a future 2:1 split.
            history={
                datetime(2024, 1, 2): (300.0, 150.0),
                datetime(2024, 12, 31): (160.0, 160.0),
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        # The basis is the back-adjusted close (150) -- comparing
        # today's 160 against it yields the post-split total return.
        assert benchmark.start_basis_price == pytest.approx(150.0)


class TestCumulativeReturnSeries:
    def test_rightmost_sample_uses_regular_market_price_when_ref_past_last_yahoo_day(
        self, install_ticker, stub_exchange_rate
    ):
        # Yahoo's last trading day is Dec 31; "today" (Jan 5) is
        # past it. The resampler used to clip the last sample to
        # ``Adj Close[Dec 31] / Adj Close[Jan 2]`` (= 190/152) --
        # the new contract overrides that with
        # ``regularMarketPrice / Adj Close[Jan 2]`` (= 200/152)
        # so the chart's right edge matches the TSR's
        # ``regularMarketPrice`` numerator.
        install_ticker(_make_benchmark_ticker(
            price=200.0,
            history={
                datetime(2024, 1, 2): (150.0, 152.0),
                datetime(2024, 6, 3): (180.0, 180.0),
                datetime(2024, 12, 31): (190.0, 190.0),
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2024, 1, 2), 1.0),
            (datetime(2024, 6, 3), 1.05),
            (datetime(2025, 1, 5), 1.20),
        ]
        series = benchmark.cumulative_return_series(ref_history)
        assert series[-1][0] == datetime(2025, 1, 5)
        # 200 / 152 ≈ 1.3158 -- NOT 190 / 152 ≈ 1.25
        assert series[-1][1] == pytest.approx(200.0 / 152.0)
        assert series[-1][1] != pytest.approx(190.0 / 152.0)

    def test_rightmost_sample_uses_regular_market_price_when_ref_equals_last_yahoo_day(
        self, install_ticker, stub_exchange_rate
    ):
        # Even when the last ref date IS a Yahoo trading day, the
        # ``regularMarketPrice`` snapshot tracks the live tape while
        # the Yahoo response's Adj Close lags by the time between
        # the two ``yfinance`` round-trips -- so the override still
        # kicks in to keep the chart and the TSR in lockstep.
        install_ticker(_make_benchmark_ticker(
            price=195.0,
            history={
                datetime(2024, 1, 2): (150.0, 152.0),
                datetime(2024, 12, 31): (190.0, 190.0),  # stale close
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2024, 1, 2), 1.0),
            (datetime(2024, 12, 31), 1.10),
        ]
        series = benchmark.cumulative_return_series(ref_history)
        assert series[-1][1] == pytest.approx(195.0 / 152.0)

    def test_intermediate_samples_still_use_adjusted_close(
        self, install_ticker, stub_exchange_rate
    ):
        # The override only touches the right edge -- every other
        # sample reads its value from the adjusted-close array so
        # the curve's shape between the endpoints is the actual
        # benchmark trajectory.
        install_ticker(_make_benchmark_ticker(
            price=999.0,
            history={
                datetime(2024, 1, 2): (150.0, 152.0),
                datetime(2024, 6, 3): (180.0, 180.0),
                datetime(2024, 12, 31): (190.0, 190.0),
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2024, 1, 2), 1.0),
            (datetime(2024, 6, 3), 1.05),
            (datetime(2025, 1, 5), 1.20),
        ]
        series = benchmark.cumulative_return_series(ref_history)
        # Midpoint sample still uses Adj Close[Jun 3] / Adj Close[Jan 2].
        assert series[1][1] == pytest.approx(180.0 / 152.0)
        # First sample pinned to 1.0 by convention.
        assert series[0][1] == 1.0


class TestCumulativeReturnSeriesTimezoneHandling:
    """Yahoo returns a tz-aware ``DatetimeIndex`` for exchange-listed
    tickers. The resampler's ``np.searchsorted`` lookup compares those
    timestamps against tz-naive reference dates parsed from the
    spreadsheet, so the construction-time ``datetime64[D]`` conversion
    has to preserve the **exchange-local** calendar date -- not the
    UTC-shifted one -- or every BST trading day silently maps to the
    next session's adj close."""

    def test_bst_ref_date_resolves_to_same_days_adj_close(
        self, install_ticker, stub_exchange_rate
    ):
        # Yahoo returns LSE bars as ``YYYY-MM-DD 00:00:00+01:00``
        # during BST. The naive ``.to_numpy().astype("datetime64[D]")``
        # collapses each timestamp to its UTC date, which subtracts an
        # hour and shifts every BST trading day back one calendar day.
        # The resampler's ``searchsorted`` then maps the ref date
        # ``2026-03-31`` to the **Apr 1** row's adj close (overstating
        # the curve by a session's move). The fix re-localises to
        # naive before the date conversion so the lookup picks the
        # actual Mar 31 row.
        install_ticker(_make_benchmark_ticker(
            price=999.0,
            history={
                # Winter (UTC+0): conversion is a no-op even without
                # the fix -- included to anchor the start basis.
                datetime(2026, 1, 2): (131.64, 131.64),
                # BST (UTC+1): exactly the regression case from the
                # production page. Without the fix, ref Mar 31 picks
                # up Apr 1's close (127.04) and the chart reads
                # ``-3.49%`` instead of the correct ``-5.85%``.
                datetime(2026, 3, 31): (123.94, 123.94),
                datetime(2026, 4, 1): (127.04, 127.04),
                # Anchor the right edge of the Yahoo history past
                # the last ref date so the resampler's last-sample
                # ``regularMarketPrice`` override stays inactive and
                # we can assert directly on the adj-close-derived
                # value the bug would otherwise produce.
                datetime(2026, 4, 30): (130.00, 130.00),
            },
            index_tz="Europe/London",
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2026, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2026, 1, 1), 1.0),
            (datetime(2026, 3, 31), 0.95),
        ]
        series = benchmark.cumulative_return_series(ref_history)
        assert series[1][1] == pytest.approx(123.94 / 131.64)
        # And NOT the next-session number the bug produced.
        assert series[1][1] != pytest.approx(127.04 / 131.64)

    def test_winter_ref_date_unchanged_by_tz_handling(
        self, install_ticker, stub_exchange_rate
    ):
        # Sanity check: outside DST the conversion is already a
        # no-op (UTC+0 == local), so the resampler must produce the
        # same value before and after the fix. Guards against an
        # over-zealous rewrite that drops the tz on a value that
        # would have round-tripped correctly anyway.
        #
        # The Yahoo history extends past the last ref date so the
        # right-edge override (which would otherwise pin the last
        # sample to ``regularMarketPrice / start_basis``) stays
        # inactive and we can assert the adj-close-derived value.
        install_ticker(_make_benchmark_ticker(
            price=200.0,
            history={
                datetime(2026, 1, 2): (131.64, 131.64),
                datetime(2026, 1, 12): (135.00, 135.00),
                datetime(2026, 1, 18): (134.66, 134.66),
                datetime(2026, 2, 2): (137.00, 137.00),
            },
            index_tz="Europe/London",
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2026, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2026, 1, 1), 1.0),
            (datetime(2026, 1, 12), 1.02),
            (datetime(2026, 1, 18), 1.02),
        ]
        series = benchmark.cumulative_return_series(ref_history)
        assert series[1][1] == pytest.approx(135.00 / 131.64)
        assert series[2][1] == pytest.approx(134.66 / 131.64)

    def test_naive_index_still_supported(
        self, install_ticker, stub_exchange_rate
    ):
        # Tests have always synthesised a tz-naive ``DatetimeIndex``
        # because that's the natural default from
        # ``pd.DatetimeIndex(naive_datetimes)``. The fix must leave
        # that path untouched (no exception, same numbers) so the
        # broader test suite doesn't have to be rewritten around
        # the new contract.
        install_ticker(_make_benchmark_ticker(
            price=200.0,
            history={
                datetime(2024, 1, 2): (150.0, 152.0),
                datetime(2024, 6, 3): (180.0, 180.0),
                datetime(2024, 12, 31): (190.0, 190.0),
            },
            # index_tz=None by default -> naive DatetimeIndex
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2024, 1, 2), 1.0),
            (datetime(2024, 6, 3), 1.05),
            (datetime(2024, 12, 31), 1.20),
        ]
        series = benchmark.cumulative_return_series(ref_history)
        assert series[1][1] == pytest.approx(180.0 / 152.0)


class TestSummaryChartAgreement:
    def test_chart_right_edge_equals_one_plus_tsr_pct_over_hundred(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # The whole reason for the start-basis + right-edge
        # alignment: the chart's rightmost sample and the capsule's
        # ``tsr%`` must reduce to the same arithmetic so hovering
        # over the right edge of the chart agrees with the capsule
        # below. Both should land on
        # ``regularMarketPrice / Adj Close[start_day]``.
        freeze_today(datetime(2025, 1, 5))
        install_ticker(_make_benchmark_ticker(
            price=200.0,
            history={
                datetime(2024, 1, 2): (150.0, 152.0),
                datetime(2024, 6, 3): (180.0, 180.0),
                datetime(2024, 12, 31): (190.0, 190.0),
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2024, 1, 2), 1.0),
            (datetime(2024, 6, 3), 1.05),
            (datetime(2025, 1, 5), 1.20),
        ]
        summary = benchmark.summary(ref_history)
        # TSR is the buy-and-hold ratio
        # (regularMarketPrice / Adj Close[Jan 2]) - 1.
        expected_pct = (200.0 / 152.0 - 1.0) * 100.0
        assert summary["tsr%"] == pytest.approx(expected_pct)
        # Chart's rightmost sample is the same ratio.
        chart_last = summary["history"][-1][1]
        assert chart_last == pytest.approx(1.0 + expected_pct / 100.0)
        # And the two numbers really are the same arithmetic
        # (defensive cross-check in case the assertions above
        # both drift in the same direction).
        assert chart_last - 1.0 == pytest.approx(summary["tsr%"] / 100.0)

    def test_agreement_holds_with_a_split_inside_the_period(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # A 2:1 split inside the period. Yahoo's Adj Close
        # back-adjusts the pre-split close (304 -> 152), so the
        # basis used by both the synthetic trade and the chart is
        # the split-adjusted value (152). After the split a real
        # 1-share holding becomes 2 shares worth 220 total at
        # regularMarketPrice=110, for a true -27.6% return; the
        # synthetic trade's "1 share at 152, now worth 110"
        # reduces to the SAME per-share split-adjusted return
        # because the basis is already expressed in post-split
        # units -- so the math comes out right without the
        # benchmark needing to model the split's quantity bump.
        # The whole point of this test: TSR and chart's right
        # edge agree at the split-adjusted value, not the raw
        # pre-split number.
        freeze_today(datetime(2025, 1, 5))
        install_ticker(_make_benchmark_ticker(
            price=110.0,
            splits={_date_key(datetime(2024, 6, 3)): 2.0},
            history={
                datetime(2024, 1, 2): (300.0, 152.0),
                datetime(2024, 6, 3): (160.0, 160.0),
                datetime(2024, 12, 31): (100.0, 100.0),
            },
        ))
        benchmark = Benchmark(
            "VUAA.L", datetime(2024, 1, 1), fx=stub_exchange_rate,
        )
        ref_history = [
            (datetime(2024, 1, 2), 1.0),
            (datetime(2024, 6, 3), 1.05),
            (datetime(2025, 1, 5), 1.10),
        ]
        summary = benchmark.summary(ref_history)
        expected_pct = (110.0 / 152.0 - 1.0) * 100.0
        assert summary["tsr%"] == pytest.approx(expected_pct)
        chart_last = summary["history"][-1][1]
        assert chart_last == pytest.approx(1.0 + expected_pct / 100.0)
        assert chart_last - 1.0 == pytest.approx(summary["tsr%"] / 100.0)
