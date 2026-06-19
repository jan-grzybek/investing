"""Tests for the ``Holding`` class.

These tests stub ``yf.Ticker`` so no network is hit, and pin the exchange
rate at 1.0 so we only have to reason about share counts and prices.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from investing.holdings import DAYS_YEAR, WITHHOLDING_TAX_RATE, Holding, _xirr
from investing.trades import Trade


def _date_key(d: datetime) -> str:
    """yfinance stringifies timestamps as 'YYYY-MM-DD HH:MM:SS[+TZ]'.
    Our code parses only the first whitespace-separated token, so any
    string starting with the ISO date works.
    """
    return d.strftime("%Y-%m-%d 00:00:00")


def _make_ticker(
    *,
    price: float = 100.0,
    currency: str = "USD",
    splits=None,
    dividends=None,
    long_name: str = "Test Co.",
    symbol: str = "TST",
    exchange: str = "NMS",
    history=None,
    sector: str | None = None,
):
    from unittest.mock import MagicMock

    import pandas as pd

    mock = MagicMock()
    info: dict = {
        "currency": currency,
        "exchange": exchange,
        "symbol": symbol,
        "longName": long_name,
        "regularMarketPrice": price,
    }
    # Only set ``sector`` when the test cares about it -- leaving the
    # key absent exercises the same ``info.get("sector") or ""`` path
    # the production summary takes when yfinance omits the field
    # entirely (the empty-sector + maintenance-hint branch). Tests
    # that want to pin a real sector pass ``sector="Technology"``;
    # tests that want to exercise the missing-sector pin pass
    # ``sector=""`` (or leave it unset).
    if sector is not None:
        info["sector"] = sector
    mock.get_info.return_value = info
    mock.splits = splits or {}
    mock.get_dividends.return_value = dividends or {}
    # ``history`` is a ``{datetime: close}`` dict that mirrors the
    # slice of yfinance's ``Ticker.history`` output our chained-TWR
    # walk consumes. Only tests with dividend events need to wire
    # this up -- the close-on-date helper is invoked exclusively
    # to bound the ex-dividend sub-period, so dividend-free tests
    # can leave ``history`` unset and the MagicMock default never
    # gets dereferenced.
    if history is not None:
        sorted_dates = sorted(history)
        df = pd.DataFrame(
            {"Close": [history[d] for d in sorted_dates]},
            index=pd.DatetimeIndex(sorted_dates, name="Date"),
        )
        mock.history.return_value = df
    return mock


@pytest.fixture
def install_ticker(monkeypatch):
    def _install(ticker_mock):
        monkeypatch.setattr(
            "investing.holdings.yf.Ticker",
            lambda symbol: ticker_mock,  # noqa: ARG005
        )
        return ticker_mock

    return _install


class TestSplitsAndDividendsBootstrap:
    def test_splits_are_accumulated_in_reverse(self, install_ticker):
        # Three splits: 2:1, then 3:1, then 5:1. The earliest split is
        # multiplied by all subsequent splits (2 * 3 * 5 = 30).
        ticker = install_ticker(
            _make_ticker(
                splits={
                    _date_key(datetime(2020, 1, 1)): 2.0,
                    _date_key(datetime(2021, 1, 1)): 3.0,
                    _date_key(datetime(2022, 1, 1)): 5.0,
                }
            )
        )
        holding = Holding("TST")

        assert ticker.get_info.called
        # Raw splits (not cumulative).
        assert [s["split"] for s in holding._splits] == [2.0, 3.0, 5.0]

    def test_dividends_stored_verbatim_from_yfinance(self, install_ticker):
        # ``Ticker.dividends`` returns per-share values denominated in
        # **post-all-splits** share units (yfinance back-adjusts the
        # series at the source). The chained-TWR walk in ``summary``
        # tracks ``quantity`` in the same current frame, so storing
        # dividends verbatim means the per-share value and the share
        # count agree without any retroactive multiplication step.
        install_ticker(
            _make_ticker(
                splits={_date_key(datetime(2022, 1, 1)): 2.0},
                dividends={
                    _date_key(datetime(2021, 6, 1)): 1.00,
                    _date_key(datetime(2023, 6, 1)): 1.00,
                },
            )
        )
        holding = Holding("TST")

        by_date = {d["date"]: d["dividend"] for d in holding._dividends}
        assert by_date[datetime(2021, 6, 1)] == pytest.approx(1.00)
        assert by_date[datetime(2023, 6, 1)] == pytest.approx(1.00)


class TestBuy:
    def test_first_buy_opens_a_position_and_period(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))

        assert len(holding._positions) == 1
        assert holding._positions[-1]["quantity"] == 10
        assert holding._periods == [{"start": datetime(2024, 1, 1), "end": None}]
        assert holding._inflows == [{"date": datetime(2024, 1, 1), "value": 10 * 50.0}]

    def test_same_day_buy_aggregates_quantity(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 5, 60.0, "BUY"))

        assert len(holding._positions) == 1
        assert holding._positions[-1]["quantity"] == 15
        # Two separate inflows recorded but only one position row.
        assert len(holding._inflows) == 2

    def test_buy_after_split_scales_existing_quantity(self, install_ticker, stub_exchange_rate):
        # 4:1 split between Jan and Mar. 10 shares held -> 40 shares before
        # the next buy of 5 -> position becomes 45.
        install_ticker(
            _make_ticker(
                price=100.0,
                splits={_date_key(datetime(2024, 2, 1)): 4.0},
            )
        )
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.buy(Trade(datetime(2024, 3, 1), "TST", 5, 25.0, "BUY"))

        assert holding._positions[-1]["quantity"] == 45


class TestSell:
    def test_partial_sell_keeps_period_open(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 1), "TST", 4, 60.0, "SELL"))

        assert holding._positions[-1]["quantity"] == 6
        assert holding._periods[-1]["end"] is None
        assert holding._outflows == [{"date": datetime(2024, 6, 1), "value": 4 * 60.0}]

    def test_full_sell_closes_the_period(self, install_ticker, stub_exchange_rate):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 1), "TST", 10, 60.0, "SELL"))

        assert holding._positions[-1]["quantity"] == 0
        assert holding._periods[-1]["end"] == datetime(2024, 6, 1)

    def test_rebuy_within_trade_window_reopens_the_period(
        self,
        install_ticker,
        stub_exchange_rate,
    ):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 19), "TST", 10, 60.0, "SELL"))
        holding.buy(Trade(datetime(2024, 9, 2), "TST", 7, 55.0, "BUY"))

        assert holding._periods == [
            {"start": datetime(2024, 1, 1), "end": None},
        ]

    def test_rebuy_after_trade_window_starts_new_period(
        self,
        install_ticker,
        stub_exchange_rate,
    ):
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 1), "TST", 10, 60.0, "SELL"))
        # 92 days later -- outside the 90-day rolling quarter.
        holding.buy(Trade(datetime(2024, 9, 1), "TST", 7, 55.0, "BUY"))

        assert len(holding._periods) == 2
        assert holding._periods[0] == {
            "start": datetime(2024, 1, 1),
            "end": datetime(2024, 6, 1),
        }
        assert holding._periods[1] == {
            "start": datetime(2024, 9, 1),
            "end": None,
        }


class TestPeriodMergeDisplayOnly:
    """Quick divest/rebuy round-trips merge ``_periods`` for the webpage.

    The contract under test: capsule date spans may read as uninterrupted
    ownership, but MoIC / XIRR still reflect every real trade on its
    actual date (including the gap while flat).
    """

    def test_rebuy_within_window_merges_periods_but_not_returns(
        self,
        install_ticker,
        stub_exchange_rate,
        freeze_today,
    ):
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=100.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 50.0, "BUY"))
        holding.sell(Trade(datetime(2024, 6, 19), "TST", 10, 60.0, "SELL"))
        holding.buy(Trade(datetime(2024, 9, 2), "TST", 7, 55.0, "BUY"))

        summary = holding.summary()

        assert summary["periods"] == [
            {"start": datetime(2024, 1, 1), "end": None},
        ]
        # MoIC uses every inflow/outflow regardless of the merged span.
        gross_invested = 10 * 50.0 + 7 * 55.0
        gross_returned = 10 * 60.0 + 7 * 100.0
        expected_tsr = (gross_returned / gross_invested - 1.0) * 100.0
        assert summary["tsr%"] == pytest.approx(expected_tsr)
        # XIRR still brackets the actual dated cashflows (incl. the
        # Jun-19 outflow and Sep-2 inflow), not a synthetic hold-through.
        expected_cashflows = [
            (datetime(2024, 1, 1), -10 * 50.0),
            (datetime(2024, 6, 19), +10 * 60.0),
            (datetime(2024, 9, 2), -7 * 55.0),
            (datetime(2025, 1, 1), +7 * 100.0),
        ]
        expected_cagr = _xirr(expected_cashflows) * 100.0
        assert summary["cagr%"] == pytest.approx(expected_cagr)


class TestSummaryDividends:
    """Dividend handling now lives inside the chained-TWR walk in
    :meth:`Holding.summary` rather than a standalone ``_add_dividends``
    helper. Each test pins the close-on-div-date in the history mock so
    the price-only sub-period return is a known value (typically 1.0)
    and the assertions can isolate the dividend-yield contribution.
    """

    def test_dividend_during_open_position_is_recorded_after_tax(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # No price movement -- only contribution to TSR is the
        # after-tax dividend yield: 5.00 USD * (1 - 0.15) / 100.
        freeze_today(datetime(2025, 1, 1))
        install_ticker(
            _make_ticker(
                price=100.0,
                dividends={_date_key(datetime(2024, 6, 1)): 5.00},
                history={
                    datetime(2024, 1, 1): 100.0,
                    datetime(2024, 6, 1): 100.0,
                },
            )
        )
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()

        expected_yield_pct = 5.00 * (1.0 - WITHHOLDING_TAX_RATE) / 100.0 * 100
        assert summary["tsr%"] == pytest.approx(expected_yield_pct)

    def test_dividend_before_first_buy_is_ignored(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Holder didn't own shares on the dividend date, so the
        # cash never reached them. With no other movement, TSR is 0%.
        freeze_today(datetime(2025, 1, 1))
        install_ticker(
            _make_ticker(
                price=100.0,
                dividends={_date_key(datetime(2023, 6, 1)): 5.00},
                history={
                    datetime(2023, 6, 1): 100.0,
                    datetime(2024, 1, 1): 100.0,
                },
            )
        )
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()
        assert summary["tsr%"] == pytest.approx(0.0, abs=1e-9)

    def test_dividend_after_full_close_is_ignored(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # The position was closed before the ex-div date; the dividend
        # never landed in the holder's account and must not leak into
        # the chained TWR.
        freeze_today(datetime(2025, 1, 1))
        install_ticker(
            _make_ticker(
                price=100.0,
                dividends={_date_key(datetime(2024, 6, 1)): 5.00},
                history={
                    datetime(2024, 1, 1): 100.0,
                    datetime(2024, 3, 1): 100.0,
                    datetime(2024, 6, 1): 100.0,
                },
            )
        )
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))
        holding.sell(Trade(datetime(2024, 3, 1), "TST", 10, 100.0, "SELL"))

        summary = holding.summary()
        assert summary["tsr%"] == pytest.approx(0.0, abs=1e-9)

    def test_capital_return_on_sell_is_untaxed(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # 50% capital gain on a closed position -- the withholding
        # tax must apply ONLY to dividend yields, never to the
        # price-only sub-period return. A round-trip from $100 to
        # $150 with no dividends should report exactly 50% TSR.
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=999.0))  # current price irrelevant -- closed
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))
        holding.sell(Trade(datetime(2025, 1, 1), "TST", 10, 150.0, "SELL"))

        summary = holding.summary()
        assert summary["tsr%"] == pytest.approx(50.0)


class TestSummary:
    def test_open_position_summary_shape_and_signs(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 1, 1))
        install_ticker(
            _make_ticker(
                price=200.0,
                symbol="TST",
                exchange="NMS",
                long_name="Test Co.",
            )
        )
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()

        assert summary["ticker"] == "NMS:TST"
        assert summary["name"] == "Test Co."
        assert summary["is_current"] is True
        assert summary["current_weight%"] is None  # filled in by apply_rollup()
        assert summary["current_value_usd"] == pytest.approx(10 * 200.0)
        assert summary["latest_buy"] == datetime(2024, 1, 1)
        assert summary["latest_sell"] is None
        # Bought at 100, current price 200 -> roughly +100% over a year.
        # Modified Dietz with a single inflow at period start gives exactly 100%.
        assert summary["tsr%"] == pytest.approx(100.0)
        # 100% gain over ~1 year -> CAGR ~100%.
        assert summary["cagr%"] == pytest.approx(100.0, rel=0.02)

    def test_closed_position_summary_uses_period_end_value(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=999.0))  # current price shouldn't matter
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))
        holding.sell(Trade(datetime(2025, 1, 1), "TST", 10, 150.0, "SELL"))

        summary = holding.summary()

        assert summary["is_current"] is False
        # Bought at 100, sold at 150 -> exactly 50% TSR over a year.
        assert summary["tsr%"] == pytest.approx(50.0)
        # 1 year duration -> CAGR ≈ TSR.
        assert summary["cagr%"] == pytest.approx(50.0, rel=0.02)
        assert summary["latest_sell"] == datetime(2025, 1, 1)

    def test_open_position_through_split_is_marked_to_market_in_post_split_units(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Bought 100 shares at $100 each ($10,000 invested), then a
        # 2:1 split happened with no subsequent trade. Live tape
        # ``regularMarketPrice`` quotes in post-split units; if the
        # underlying tracked the split flat, the post-split price is
        # $50 and the position is now 200 shares worth $10,000 -- a
        # 0% TSR. Without applying the split to the live quantity,
        # ``current_value_usd`` and the synthetic outflow that caps
        # the open period would multiply 100 (pre-split count) by
        # $50 (post-split price) and report a phantom -50% loss.
        freeze_today(datetime(2025, 1, 1))
        install_ticker(
            _make_ticker(
                price=50.0,
                splits={_date_key(datetime(2024, 6, 1)): 2.0},
            )
        )
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 100, 100.0, "BUY"))

        summary = holding.summary()

        assert summary["is_current"] is True
        assert summary["current_value_usd"] == pytest.approx(200 * 50.0)
        assert summary["tsr%"] == pytest.approx(0.0, abs=1e-9)

    def test_periods_are_returned_in_reverse_chronological_order(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 6, 1))
        install_ticker(_make_ticker(price=120.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        # Two distinct ownership windows.
        holding.buy(Trade(datetime(2023, 1, 1), "TST", 10, 100.0, "BUY"))
        holding.sell(Trade(datetime(2023, 7, 1), "TST", 10, 110.0, "SELL"))
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 5, 100.0, "BUY"))

        summary = holding.summary()
        starts = [p["start"] for p in summary["periods"]]
        assert starts == [datetime(2024, 1, 1), datetime(2023, 1, 1)]

    def test_cagr_formula_matches_definition(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Force a known total ownership length so we can compute the
        # expected CAGR from first principles.
        freeze_today(datetime(2026, 1, 1))  # 2 years after purchase
        install_ticker(_make_ticker(price=144.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 1, 100.0, "BUY"))

        summary = holding.summary()
        # TSR = 0.44 -> CAGR = (1.44) ** (DAYS_YEAR / length) - 1
        length = max((datetime(2026, 1, 1) - datetime(2024, 1, 1)).days, 1)
        # ``Holding.summary`` stores unrounded percentages so downstream
        # callers can subtract or compound without leaking rounding
        # error -- the expected matches that full precision.
        expected_cagr = ((1.44) ** (DAYS_YEAR / length) - 1) * 100
        assert summary["cagr%"] == pytest.approx(expected_cagr)

    def test_top_up_after_runup_reflects_actual_money_journey(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Regression coverage for the headline-figure honesty
        # invariant of the MoIC + XIRR pair: a small initial
        # position 10x's, then the holder adds a large top-up near
        # the live price, and finally we mark-to-market a smidge
        # above. Under TWR semantics the figures looked like a
        # 10x return on the security (technically true!), but the
        # investor's actual money saw only ~2% return because the
        # vast majority of the dollars were deployed only in the
        # last month. MoIC + XIRR are the standard tool to read
        # this honestly.
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=1010.0))
        holding = Holding("TST", fx=stub_exchange_rate)
        # Buy 1 share at $100 (Jan 2023), watch it ride to $1000,
        # top up 100 more shares at $1000 (Dec 2024). From the
        # second buy to today the price only moves another 1%.
        holding.buy(Trade(datetime(2023, 1, 1), "TST", 1, 100.0, "BUY"))
        holding.buy(Trade(datetime(2024, 12, 1), "TST", 100, 1000.0, "BUY"))

        summary = holding.summary()
        # MoIC = (101 * $1010) / ($100 + $100,000) = 102,010 /
        # 100,100 ≈ 1.0191x -> Return % ≈ 1.91%. Tiny, because
        # the investor's actual capital-weighted exposure was
        # almost entirely on the late top-up that barely moved.
        assert summary["tsr%"] == pytest.approx(1.91, abs=0.05)
        # IRR is much higher than MoIC because the late capital
        # was deployed for only ~31 days and a 1% return over 31
        # days annualises to ~12% p.a.; the early $100's 10x over
        # 2 years pulls that up further. The exact value depends
        # on the bisection but is unambiguously double-digit
        # positive p.a.
        assert summary["cagr%"] > 10.0
        assert summary["cagr%"] < 50.0


class TestSummarySector:
    """``Holding.summary`` routes the upstream ``info["sector"]`` field
    through :func:`investing.sector_overrides.resolve_sector` so a
    blank value can be repaired via the maintainer-curated
    ``sector_overrides.toml`` file before reaching the renderer. The
    tests below pin both halves of that contract -- the value passed
    through to the dict and the maintenance hint recorded when no
    repair is available.
    """

    def test_summary_passes_through_yfinance_sector(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        from investing.sector_overrides import consume_hints

        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=200.0, sector="Technology"))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()
        assert summary["sector"] == "Technology"
        # Sector was present upstream so no hint should fire -- the
        # maintainer registry exists to flag *missing* data, not
        # confirmed-good data.
        assert consume_hints().is_empty

    def test_summary_records_hint_when_sector_missing(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        from investing.sector_overrides import consume_hints

        freeze_today(datetime(2025, 1, 1))
        # ``sector=""`` explicitly mimics yfinance returning a blank
        # value (some ADRs / brand-new listings behave this way). The
        # production override file at the repo root has no entry for
        # the synthetic ``NMS:TST`` so the resolver falls through to
        # the empty + hint branch.
        install_ticker(_make_ticker(price=200.0, sector=""))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()
        assert summary["sector"] == ""
        hints = consume_hints()
        assert hints.missing_sector == ["NMS:TST"]

    def test_summary_records_hint_when_sector_field_absent(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Same behaviour when ``info`` doesn't carry the ``sector``
        # key at all -- ``get("sector") or ""`` produces the empty
        # string and the recorder fires identically. Covers the
        # common case where a legacy mock just doesn't set the
        # field rather than explicitly pinning it to ``""``.
        from investing.sector_overrides import consume_hints

        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=200.0))  # no sector kwarg
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        holding.summary()
        assert consume_hints().missing_sector == ["NMS:TST"]


class TestAssetClass:
    """Per-Holding asset-class tag exposed through ``Holding.summary``.

    Drives the renderer's bucketing into the dedicated Equities /
    Fixed Income sub-sections. Defaults to ``"equity"`` so the
    historical equity-only call path stays unchanged; bond /
    treasury / fixed-income-ETF holdings opt in via the explicit
    ``asset_class="fixed_income"`` kwarg.
    """

    def test_summary_defaults_to_equity_asset_class(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 1, 1))
        install_ticker(_make_ticker(price=200.0, sector="Technology"))
        holding = Holding("TST", fx=stub_exchange_rate)
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 10, 100.0, "BUY"))

        summary = holding.summary()
        assert summary["asset_class"] == "equity"

    def test_summary_carries_explicit_fixed_income_tag(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        freeze_today(datetime(2025, 1, 1))
        # Fixed-income tickers go through yfinance the same way
        # equities do; the only call-site difference is the explicit
        # ``asset_class`` kwarg on the constructor.
        install_ticker(_make_ticker(price=92.5, sector="Government"))
        holding = Holding(
            "TST",
            fx=stub_exchange_rate,
            asset_class="fixed_income",
        )
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 100, 90.0, "BUY"))

        summary = holding.summary()
        assert summary["asset_class"] == "fixed_income"

    def test_unknown_asset_class_raises(self, install_ticker, stub_exchange_rate):
        # Guards against typos / future expansions slipping through
        # silently. The renderer only knows the two canonical values
        # so a misspelling here would land the holding in the equity
        # bucket by default and the maintainer would have no signal
        # the tag was wrong.
        from investing.errors import InvariantError

        install_ticker(_make_ticker(price=100.0, sector="Technology"))
        with pytest.raises(InvariantError):
            Holding(
                "TST",
                fx=stub_exchange_rate,
                asset_class="commodity",
            )

    def test_fixed_income_summary_skips_sector_resolver(
        self, install_ticker, stub_exchange_rate, freeze_today
    ):
        # Fixed-income instruments (treasury / corporate-bond ETFs)
        # never carry a GICS-style sector and the renderer never
        # feeds them into the equity treemap, so the resolver should
        # be skipped entirely for ``asset_class="fixed_income"`` --
        # otherwise every bond holding would record a "missing
        # sector" maintenance hint that the maintainer cannot
        # meaningfully act on (no GICS sector applies). The ``sector``
        # field stays as a stable empty string so the dict shape
        # is uniform across asset classes.
        from investing.sector_overrides import consume_hints

        freeze_today(datetime(2025, 1, 1))
        # No ``sector`` field on the upstream info -- mimics the
        # blank yfinance response a treasury ETF would actually
        # produce. Were the resolver still invoked, the empty string
        # would fall through and record a maintenance hint.
        install_ticker(_make_ticker(price=92.5))
        holding = Holding(
            "TST",
            fx=stub_exchange_rate,
            asset_class="fixed_income",
        )
        holding.buy(Trade(datetime(2024, 1, 1), "TST", 100, 90.0, "BUY"))

        summary = holding.summary()
        assert summary["sector"] == ""
        # Crucially: no maintenance hint recorded -- the resolver
        # never ran, so the registry stays empty.
        assert consume_hints().is_empty
