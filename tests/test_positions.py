"""Tests for multi-listing positions.

Coverage focus:

* :func:`investing.holdings.merge_ledgers` -- cashflow concatenation,
  gross totals, period union, currency-agnostic combination.
* :func:`investing.holdings.ledger_metrics` -- and specifically that
  a combined IRR is the IRR of the merged series, NOT a blend of the
  per-leg IRRs (the property that forces the merge to happen on the
  timeline rather than on the finished percentages).
* :func:`investing.positions.build_position_summaries` -- identity
  inheritance, validation, pass-through for ungrouped holdings.
* End-to-end through :func:`investing.performance.get_holdings`,
  including that Trades stay per-listing.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import pytest

from investing.errors import InvariantError
from investing.holdings import (
    PositionLedger,
    _merge_periods,
    _xirr,
    ledger_metrics,
    merge_ledgers,
)
from investing.performance import get_holdings
from investing.position_groups import PositionGroup
from investing.positions import (
    apply_group_trade_names,
    build_position_summaries,
)

TWO_LEG_CONFIG = """
[samsung]
primary = "DUS:SSU.DU"
members = ["IOB:SMSN.IL"]
name = "Samsung Electronics"
label = "Samsung"
"""


def _write_groups(tmp_path: Path, body: str) -> str:
    path = tmp_path / "position_groups.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _ledger(
    *,
    cashflows,
    invested,
    returned,
    value=0.0,
    is_current=False,
    periods=None,
    latest_buy=None,
    latest_sell=None,
) -> PositionLedger:
    return PositionLedger(
        cashflows=list(cashflows),
        gross_invested=invested,
        gross_returned=returned,
        current_value_usd=value,
        is_current=is_current,
        periods=list(periods or []),
        latest_buy=latest_buy,
        latest_sell=latest_sell,
    )


class TestMergePeriods:
    def test_overlapping_spans_collapse_into_one(self):
        merged = _merge_periods(
            [
                {"start": datetime(2024, 1, 1), "end": datetime(2024, 6, 1)},
                {"start": datetime(2024, 3, 1), "end": datetime(2024, 9, 1)},
            ]
        )
        assert merged == [{"start": datetime(2024, 1, 1), "end": datetime(2024, 9, 1)}]

    def test_disjoint_spans_stay_separate_newest_first(self):
        # A position exited and later re-entered still shows both
        # stints; ordering matches the single-ticker contract.
        merged = _merge_periods(
            [
                {"start": datetime(2020, 1, 1), "end": datetime(2020, 6, 1)},
                {"start": datetime(2024, 1, 1), "end": datetime(2024, 6, 1)},
            ]
        )
        assert merged == [
            {"start": datetime(2024, 1, 1), "end": datetime(2024, 6, 1)},
            {"start": datetime(2020, 1, 1), "end": datetime(2020, 6, 1)},
        ]

    def test_open_span_absorbs_later_spans(self):
        # ``end is None`` means "to the present", so a later span
        # cannot extend past it and must not be compared as a date.
        merged = _merge_periods(
            [
                {"start": datetime(2024, 1, 1), "end": None},
                {"start": datetime(2024, 5, 1), "end": datetime(2024, 8, 1)},
            ]
        )
        assert merged == [{"start": datetime(2024, 1, 1), "end": None}]

    def test_closed_span_extended_by_an_open_one_stays_open(self):
        merged = _merge_periods(
            [
                {"start": datetime(2024, 1, 1), "end": datetime(2024, 6, 1)},
                {"start": datetime(2024, 3, 1), "end": None},
            ]
        )
        assert merged == [{"start": datetime(2024, 1, 1), "end": None}]

    def test_empty_input(self):
        assert _merge_periods([]) == []


class TestMergeLedgers:
    def test_sums_grosses_and_values(self):
        merged = merge_ledgers(
            [
                _ledger(cashflows=[], invested=100.0, returned=150.0, value=150.0),
                _ledger(cashflows=[], invested=300.0, returned=330.0, value=330.0),
            ]
        )
        assert merged.gross_invested == 400.0
        assert merged.gross_returned == 480.0
        assert merged.current_value_usd == 480.0

    def test_is_current_when_any_leg_is_open(self):
        # Holding one listing after closing another is still holding
        # the company.
        merged = merge_ledgers(
            [
                _ledger(cashflows=[], invested=1.0, returned=1.0, is_current=False),
                _ledger(cashflows=[], invested=1.0, returned=1.0, is_current=True),
            ]
        )
        assert merged.is_current is True

    def test_latest_dates_take_the_max_across_legs(self):
        merged = merge_ledgers(
            [
                _ledger(
                    cashflows=[],
                    invested=1.0,
                    returned=1.0,
                    latest_buy=datetime(2024, 1, 1),
                    latest_sell=datetime(2024, 2, 1),
                ),
                _ledger(
                    cashflows=[],
                    invested=1.0,
                    returned=1.0,
                    latest_buy=datetime(2025, 1, 1),
                    latest_sell=None,
                ),
            ]
        )
        assert merged.latest_buy == datetime(2025, 1, 1)
        assert merged.latest_sell == datetime(2024, 2, 1)

    def test_empty_merge_raises(self):
        # A group with no legs is a config fault; a zero-valued ledger
        # would render a phantom capsule instead of surfacing it.
        with pytest.raises(InvariantError):
            merge_ledgers([])


class TestCombinedMetrics:
    def test_moic_is_the_ratio_of_summed_totals(self):
        # 100 -> 150 (1.5x) merged with 300 -> 330 (1.1x) is 480/400
        # = 1.2x, i.e. +20% -- capital-weighted, not the 1.3x a naive
        # average of the two multiples would give.
        merged = merge_ledgers(
            [
                _ledger(cashflows=[], invested=100.0, returned=150.0),
                _ledger(cashflows=[], invested=300.0, returned=330.0),
            ]
        )
        tsr_pct, _ = ledger_metrics(merged)
        assert tsr_pct == pytest.approx(20.0)

    def test_irr_is_solved_on_the_merged_series_not_blended(self):
        # The property that forces the whole design. Two legs held
        # over very different horizons, equal capital in each:
        #
        #   leg A: -100 (2024-01-01) -> +200 (2025-01-01)  ~99.71%/yr
        #   leg B: -100 (2021-01-01) -> +110 (2025-01-01)   ~2.41%/yr
        #
        # Both the plain mean and the capital-weighted mean of those
        # rates come to 51.06%, because the legs carry equal capital.
        # The true rate on the merged series is 17.75% -- roughly a
        # third of it -- because leg B's capital was tied up three
        # years longer, which a rate-space blend cannot see.
        leg_a = _ledger(
            cashflows=[(datetime(2024, 1, 1), -100.0), (datetime(2025, 1, 1), +200.0)],
            invested=100.0,
            returned=200.0,
        )
        leg_b = _ledger(
            cashflows=[(datetime(2021, 1, 1), -100.0), (datetime(2025, 1, 1), +110.0)],
            invested=100.0,
            returned=110.0,
        )
        _, combined_pct = ledger_metrics(merge_ledgers([leg_a, leg_b]))

        assert combined_pct == pytest.approx(17.7457, abs=1e-3)

        # Spelled out rather than derived, so the test fails loudly if
        # anyone "simplifies" the merge into a blend of per-leg rates.
        irr_a_pct = _xirr(leg_a.cashflows) * 100
        irr_b_pct = _xirr(leg_b.cashflows) * 100
        assert irr_a_pct == pytest.approx(99.7133, abs=1e-3)
        assert irr_b_pct == pytest.approx(2.4113, abs=1e-3)
        assert (irr_a_pct + irr_b_pct) / 2 == pytest.approx(51.0623, abs=1e-3)
        assert combined_pct < irr_b_pct * 10  # nowhere near the blend

    def test_merged_irr_does_not_depend_on_leg_order(self):
        # ``merge_ledgers`` concatenates and the solver sorts, so the
        # order legs arrive in cannot change the reported rate.
        leg_a = _ledger(
            cashflows=[(datetime(2024, 1, 1), -100.0), (datetime(2025, 1, 1), +200.0)],
            invested=100.0,
            returned=200.0,
        )
        leg_b = _ledger(
            cashflows=[(datetime(2021, 1, 1), -100.0), (datetime(2025, 1, 1), +110.0)],
            invested=100.0,
            returned=110.0,
        )
        forward = ledger_metrics(merge_ledgers([leg_a, leg_b]))
        reverse = ledger_metrics(merge_ledgers([leg_b, leg_a]))
        assert forward == pytest.approx(reverse)

    def test_zero_invested_yields_zero_moic_and_tba_irr(self):
        merged = merge_ledgers([_ledger(cashflows=[], invested=0.0, returned=0.0)])
        tsr_pct, cagr_pct = ledger_metrics(merged)
        assert tsr_pct == 0.0
        # No bracket for the solver -> ``inf`` so the renderer takes
        # its existing "TBA" branch rather than printing a number.
        assert math.isinf(cagr_pct)

    def test_legs_in_different_currencies_combine_without_reconciliation(self):
        # Ledger amounts are already USD -- each leg applied its own
        # FX at each event's date. So a EUR line and a USD line merge
        # by concatenation, and share counts (which for a GDR are not
        # even comparable) never enter the calculation.
        eur_leg = _ledger(
            cashflows=[(datetime(2024, 1, 1), -1000.0), (datetime(2025, 1, 1), +1200.0)],
            invested=1000.0,
            returned=1200.0,
        )
        usd_leg = _ledger(
            cashflows=[(datetime(2024, 1, 1), -1000.0), (datetime(2025, 1, 1), +1000.0)],
            invested=1000.0,
            returned=1000.0,
        )
        tsr_pct, _ = ledger_metrics(merge_ledgers([eur_leg, usd_leg]))
        assert tsr_pct == pytest.approx(10.0)


def _two_leg_world(patch_yf_ticker, make_ticker_mock):
    """Two listings of one company, held in parallel.

    ``SSU.DU`` is priced in EUR and ``SMSN.IL`` in USD, mirroring the
    real Düsseldorf line and London GDR, so the fixture also exercises
    the cross-currency path.
    """
    return patch_yf_ticker(
        {
            "SSU.DU": make_ticker_mock(
                exchange="DUS",
                symbol="SSU.DU",
                long_name="Samsung Electronics Co Ltd",
                currency="EUR",
                price=100.0,
            ),
            "SMSN.IL": make_ticker_mock(
                exchange="IOB",
                symbol="SMSN.IL",
                long_name="Samsung Electronics Co., Ltd.",
                currency="USD",
                price=200.0,
            ),
        }
    )


def _txn(ticker, date, quantity, price, action="BUY"):
    return {
        "date": date,
        "ticker": ticker,
        "quantity": quantity,
        "price_per_share": price,
        "action": action,
    }


class TestBuildPositionSummaries:
    def test_ungrouped_holdings_pass_through_unchanged(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        _two_leg_world(patch_yf_ticker, make_ticker_mock)
        rollup = get_holdings(
            [_txn("SSU.DU", "01-01-2024", 10, 90.0)],
            fx=stub_exchange_rate,
            now=at_datetime(datetime(2025, 6, 1)),
            groups_path=str(tmp_path / "absent.toml"),
        )
        (summary,) = rollup["current"]
        assert summary["ticker"] == "DUS:SSU.DU"
        assert summary["name"] == "Samsung Electronics Co Ltd"
        # No grouping keys on an ordinary holding: consumers fall back
        # to ``ticker``.
        assert "tickers" not in summary
        assert "short_label" not in summary

    def test_two_listings_report_as_one_position(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        _two_leg_world(patch_yf_ticker, make_ticker_mock)
        rollup = get_holdings(
            [
                _txn("SSU.DU", "01-01-2024", 10, 90.0),
                _txn("SMSN.IL", "01-02-2024", 5, 180.0),
            ],
            fx=stub_exchange_rate,
            now=at_datetime(datetime(2025, 6, 1)),
            groups_path=_write_groups(tmp_path, TWO_LEG_CONFIG),
        )
        # One capsule, not two.
        assert len(rollup["current"]) == 1
        (summary,) = rollup["current"]
        # Identity comes from the primary; the display name and the
        # compact label from the config.
        assert summary["ticker"] == "DUS:SSU.DU"
        assert summary["name"] == "Samsung Electronics"
        assert summary["short_label"] == "Samsung"
        assert summary["tickers"] == ["DUS:SSU.DU", "IOB:SMSN.IL"]

    def test_combined_value_is_the_sum_of_both_legs(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        _two_leg_world(patch_yf_ticker, make_ticker_mock)
        transactions = [
            _txn("SSU.DU", "01-01-2024", 10, 90.0),
            _txn("SMSN.IL", "01-02-2024", 5, 180.0),
        ]
        now = at_datetime(datetime(2025, 6, 1))
        separate = get_holdings(
            transactions,
            fx=stub_exchange_rate,
            now=now,
            groups_path=str(tmp_path / "absent.toml"),
        )
        combined = get_holdings(
            transactions,
            fx=stub_exchange_rate,
            now=now,
            groups_path=_write_groups(tmp_path, TWO_LEG_CONFIG),
        )
        expected = sum(h["current_value_usd"] for h in separate["current"])
        (position,) = combined["current"]
        assert position["current_value_usd"] == pytest.approx(expected)

    def test_group_with_one_held_leg_renders_standalone(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        # The config can name a pairing before the second listing is
        # actually bought; until then nothing changes.
        _two_leg_world(patch_yf_ticker, make_ticker_mock)
        rollup = get_holdings(
            [_txn("SSU.DU", "01-01-2024", 10, 90.0)],
            fx=stub_exchange_rate,
            now=at_datetime(datetime(2025, 6, 1)),
            groups_path=_write_groups(tmp_path, TWO_LEG_CONFIG),
        )
        (summary,) = rollup["current"]
        assert summary["name"] == "Samsung Electronics Co Ltd"
        assert "tickers" not in summary

    def test_trades_stay_per_listing_with_a_normalised_name(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        _two_leg_world(patch_yf_ticker, make_ticker_mock)
        rollup = get_holdings(
            [
                _txn("SSU.DU", "01-01-2024", 10, 90.0),
                _txn("SMSN.IL", "01-02-2024", 5, 180.0),
            ],
            fx=stub_exchange_rate,
            now=at_datetime(datetime(2025, 6, 1)),
            groups_path=_write_groups(tmp_path, TWO_LEG_CONFIG),
        )
        # A trade hit one specific security at one specific price in
        # one specific currency, so the rows are NOT merged.
        tickers = {event["ticker"] for event in rollup["trades"]}
        assert tickers == {"DUS:SSU.DU", "IOB:SMSN.IL"}
        currencies = {event["currency"] for event in rollup["trades"]}
        assert currencies == {"EUR", "USD"}
        # Only the label is unified, so sorting the table by Name
        # keeps the company's activity together.
        assert {event["name"] for event in rollup["trades"]} == {"Samsung Electronics"}

    def test_mixed_asset_classes_in_one_group_raise(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        # An equity grouped with a bond ETF would land in one of the
        # renderer's two sections while its cashflows came from both.
        _two_leg_world(patch_yf_ticker, make_ticker_mock)
        with pytest.raises(InvariantError, match="asset class"):
            get_holdings(
                [_txn("SSU.DU", "01-01-2024", 10, 90.0)],
                fixed_income=[_txn("SMSN.IL", "01-02-2024", 5, 180.0)],
                fx=stub_exchange_rate,
                now=at_datetime(datetime(2025, 6, 1)),
                groups_path=_write_groups(tmp_path, TWO_LEG_CONFIG),
            )

    def test_missing_primary_leg_raises_when_combining(self):
        # Guards the internal contract directly: ``resolve_groups``
        # already filters this case out, so reaching the combiner
        # without the primary means the two have drifted apart.
        group = PositionGroup(
            key="samsung",
            primary="DUS:SSU.DU",
            members=("DUS:SSU.DU", "IOB:SMSN.IL"),
        )
        from investing.positions import _combined_summary

        summary = {"ticker": "IOB:SMSN.IL", "name": "Other", "asset_class": "equity"}
        with pytest.raises(InvariantError, match="primary"):
            _combined_summary(group, [(summary, _ledger(cashflows=[], invested=1, returned=1))])

    def test_grouped_and_ungrouped_positions_coexist(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        # The realistic shape: one combined position alongside
        # holdings the config says nothing about.
        patch_yf_ticker(
            {
                "SSU.DU": make_ticker_mock(
                    exchange="DUS",
                    symbol="SSU.DU",
                    long_name="Samsung Electronics Co Ltd",
                    currency="EUR",
                    price=100.0,
                ),
                "SMSN.IL": make_ticker_mock(
                    exchange="IOB",
                    symbol="SMSN.IL",
                    long_name="Samsung Electronics Co., Ltd.",
                    price=200.0,
                ),
                "NVDA": make_ticker_mock(
                    exchange="NMS",
                    symbol="NVDA",
                    long_name="NVIDIA Corporation",
                    price=500.0,
                ),
            }
        )
        rollup = get_holdings(
            [
                _txn("SSU.DU", "01-01-2024", 10, 90.0),
                _txn("SMSN.IL", "01-02-2024", 5, 180.0),
                _txn("NVDA", "01-03-2024", 2, 400.0),
            ],
            fx=stub_exchange_rate,
            now=at_datetime(datetime(2025, 6, 1)),
            groups_path=_write_groups(tmp_path, TWO_LEG_CONFIG),
        )
        # Three listings collapse to two positions.
        assert len(rollup["current"]) == 2
        by_ticker = {item["ticker"]: item for item in rollup["current"]}
        assert set(by_ticker) == {"DUS:SSU.DU", "NMS:NVDA"}
        assert by_ticker["DUS:SSU.DU"]["name"] == "Samsung Electronics"
        # The ungrouped holding is untouched, keeping its yfinance
        # name and carrying no grouping keys.
        assert by_ticker["NMS:NVDA"]["name"] == "NVIDIA Corporation"
        assert "tickers" not in by_ticker["NMS:NVDA"]
        # All three listings still appear separately under Trades.
        assert len(rollup["trades"]) == 3

    def test_label_defaults_to_the_primary_symbol(
        self,
        patch_yf_ticker,
        make_ticker_mock,
        stub_exchange_rate,
        at_datetime,
        tmp_path,
    ):
        # Two share classes of one issuer: no ``label`` needed,
        # because the primary's symbol is already the recognisable
        # form. This is the common case; Samsung is the exception.
        patch_yf_ticker(
            {
                "GOOGL": make_ticker_mock(
                    exchange="NMS", symbol="GOOGL", long_name="Alphabet Inc.", price=200.0
                ),
                "GOOG": make_ticker_mock(
                    exchange="NMS", symbol="GOOG", long_name="Alphabet Inc.", price=201.0
                ),
            }
        )
        rollup = get_holdings(
            [
                _txn("GOOGL", "01-01-2024", 10, 150.0),
                _txn("GOOG", "01-02-2024", 10, 152.0),
            ],
            fx=stub_exchange_rate,
            now=at_datetime(datetime(2025, 6, 1)),
            groups_path=_write_groups(
                tmp_path,
                """
                [alphabet]
                primary = "NMS:GOOGL"
                members = ["NMS:GOOG"]
                """,
            ),
        )
        (position,) = rollup["current"]
        assert position["short_label"] == "GOOGL"
        # No ``name`` override either: the primary's yfinance name is
        # already the company's.
        assert position["name"] == "Alphabet Inc."

    def test_short_symbol_falls_back_to_a_bare_ticker(self):
        # A primary without an exchange prefix (synthetic data /
        # a future feed that omits it) still yields a usable label
        # rather than an empty string.
        from investing.positions import _short_symbol

        assert _short_symbol("NMS:NVDA") == "NVDA"
        assert _short_symbol("NVDA") == "NVDA"

    def test_empty_portfolio(self, tmp_path):
        assert build_position_summaries([], groups_path=str(tmp_path / "absent.toml")) == []


class TestApplyGroupTradeNames:
    def test_only_grouped_legs_are_renamed(self):
        summaries = [
            {
                "ticker": "DUS:SSU.DU",
                "name": "Samsung Electronics",
                "tickers": ["DUS:SSU.DU", "IOB:SMSN.IL"],
            },
            {"ticker": "NMS:NVDA", "name": "NVIDIA Corporation"},
        ]
        events = [
            {"ticker": "DUS:SSU.DU", "name": "Samsung Electronics Co Ltd"},
            {"ticker": "IOB:SMSN.IL", "name": "Samsung Electronics Co., Ltd."},
            {"ticker": "NMS:NVDA", "name": "NVIDIA Corporation"},
        ]
        apply_group_trade_names(summaries, events)
        assert [event["name"] for event in events] == [
            "Samsung Electronics",
            "Samsung Electronics",
            "NVIDIA Corporation",
        ]

    def test_no_combined_positions_is_a_no_op(self):
        events = [{"ticker": "NMS:NVDA", "name": "NVIDIA Corporation"}]
        apply_group_trade_names([{"ticker": "NMS:NVDA", "name": "NVIDIA"}], events)
        assert events[0]["name"] == "NVIDIA Corporation"
