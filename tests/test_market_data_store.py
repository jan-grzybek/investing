"""Tests for yfinance snapshot merge + persistence."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pytest

from investing.market_data import MarketDataError
from investing.market_data_store import (
    FORBIDDEN_SNAPSHOT_KEYS,
    MarketDataStore,
    _validate_snapshot_privacy,
    merge_info,
    merge_splits,
    merge_time_series,
    split_inventory_changed,
)


def _dt(y, m, d) -> datetime:
    return datetime(y, m, d)


class TestMergeSplits:
    def test_union_keeps_archived_when_live_drops(self):
        archived = [{"date": _dt(2010, 1, 1), "split": 2.0}]
        live: list[dict] = []
        merged = merge_splits(archived, live)
        assert len(merged) == 1
        assert merged[0]["split"] == pytest.approx(2.0)

    def test_live_wins_on_same_date_conflict(self):
        archived = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        live = [{"date": _dt(2020, 1, 1), "split": 3.0}]
        merged = merge_splits(archived, live)
        assert merged[0]["split"] == pytest.approx(3.0)


class TestSplitAwareDividends:
    def test_unchanged_inventory_archive_wins_conflict(self):
        splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        archived = [{"date": _dt(2015, 6, 1), "dividend": 0.40}]
        live = [{"date": _dt(2015, 6, 1), "dividend": 0.20}]
        merged = merge_time_series(
            archived,
            live,
            value_key="dividend",
            archived_splits=splits,
            merged_splits=splits,
        )
        assert merged[0]["dividend"] == pytest.approx(0.40)

    def test_unchanged_inventory_live_adds_new_dates(self):
        splits: list[dict] = []
        archived = [{"date": _dt(2012, 6, 1), "dividend": 1.00}]
        live = [
            {"date": _dt(2012, 6, 1), "dividend": 1.00},
            {"date": _dt(2024, 6, 1), "dividend": 1.10},
        ]
        merged = merge_time_series(
            archived,
            live,
            value_key="dividend",
            archived_splits=splits,
            merged_splits=splits,
        )
        by_date = {d["date"]: d["dividend"] for d in merged}
        assert by_date[_dt(2012, 6, 1)] == pytest.approx(1.00)
        assert by_date[_dt(2024, 6, 1)] == pytest.approx(1.10)

    def test_new_split_rebases_archived_only_rows(self):
        archived_splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        merged_splits = [
            {"date": _dt(2020, 1, 1), "split": 2.0},
            {"date": _dt(2025, 5, 1), "split": 2.0},
        ]
        archived = [{"date": _dt(2015, 6, 1), "dividend": 1.00}]
        live = [
            {"date": _dt(2015, 6, 1), "dividend": 0.50},
            {"date": _dt(2025, 6, 1), "dividend": 0.55},
        ]
        merged = merge_time_series(
            archived,
            live,
            value_key="dividend",
            archived_splits=archived_splits,
            merged_splits=merged_splits,
        )
        by_date = {d["date"]: d["dividend"] for d in merged}
        assert by_date[_dt(2015, 6, 1)] == pytest.approx(0.50)
        assert by_date[_dt(2025, 6, 1)] == pytest.approx(0.55)

    def test_new_split_preserves_yahoo_dropped_row_via_rebase(self):
        archived_splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        merged_splits = [
            {"date": _dt(2020, 1, 1), "split": 2.0},
            {"date": _dt(2025, 5, 1), "split": 2.0},
        ]
        archived = [
            {"date": _dt(2012, 6, 1), "dividend": 1.00},
            {"date": _dt(2018, 6, 1), "dividend": 0.50},
        ]
        live = [{"date": _dt(2018, 6, 1), "dividend": 0.25}]
        merged = merge_time_series(
            archived,
            live,
            value_key="dividend",
            archived_splits=archived_splits,
            merged_splits=merged_splits,
        )
        by_date = {d["date"]: d["dividend"] for d in merged}
        assert by_date[_dt(2012, 6, 1)] == pytest.approx(0.50)
        assert by_date[_dt(2018, 6, 1)] == pytest.approx(0.25)


class TestMergeInfo:
    def test_live_price_wins(self):
        archived = {"regularMarketPrice": 90.0, "longName": "Old Name"}
        live = {"regularMarketPrice": 100.0, "longName": "New Name"}
        merged = merge_info(archived, live, ticker="TST")
        assert merged["regularMarketPrice"] == pytest.approx(100.0)
        assert merged["longName"] == "New Name"

    def test_archive_fills_blank_live_sector(self):
        archived = {"sector": "Technology", "regularMarketPrice": 1.0}
        live = {"sector": "", "regularMarketPrice": 2.0}
        merged = merge_info(archived, live, ticker="TST")
        assert merged["regularMarketPrice"] == pytest.approx(2.0)
        assert merged["sector"] == "Technology"

    def test_missing_live_price_raises(self):
        archived = {"regularMarketPrice": 90.0, "longName": "Old Name"}
        live = {"longName": "New Name"}
        with pytest.raises(MarketDataError, match="regularMarketPrice"):
            merge_info(archived, live, ticker="TST")

    def test_archived_price_does_not_fill_missing_live(self):
        archived = {"regularMarketPrice": 90.0}
        live: dict[str, float] = {}
        with pytest.raises(MarketDataError, match="regularMarketPrice"):
            merge_info(archived, live, ticker="TST")


class TestPrivacyGuard:
    def test_rejects_ledger_keys(self):
        with pytest.raises(ValueError, match="quantity"):
            _validate_snapshot_privacy({"quantity": 100})

    def test_forbidden_keys_frozen(self):
        assert "price" in FORBIDDEN_SNAPSHOT_KEYS
        assert "regularMarketPrice" in FORBIDDEN_SNAPSHOT_KEYS


class TestMarketDataStore:
    def test_resolve_ticker_merges_and_persists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))

        store = MarketDataStore(tmp_path)
        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "longName": "Test",
            "regularMarketPrice": 10.0,
        }
        mock.splits = {_dt(2020, 1, 1): 2.0}
        mock.get_dividends.return_value = {_dt(2021, 6, 1): 0.5}

        monkeypatch.setattr(
            "investing.market_data_store.yf.Ticker",
            lambda _symbol: mock,
        )

        info, splits, dividends = store.resolve_ticker("TST")
        assert info["regularMarketPrice"] == pytest.approx(10.0)
        assert len(splits) == 1
        assert len(dividends) == 1

        path = tmp_path / "tickers" / "TST.json"
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        _validate_snapshot_privacy(payload)
        assert "regularMarketPrice" not in payload["info"]

        mock.get_dividends.return_value = {
            _dt(2021, 6, 1): 0.25,
            _dt(2012, 6, 1): 0.10,
        }
        info2, _, dividends2 = store.resolve_ticker("TST")
        assert info2["regularMarketPrice"] == pytest.approx(10.0)
        by_date = {d["date"]: d["dividend"] for d in dividends2}
        assert by_date[_dt(2021, 6, 1)] == pytest.approx(0.5)
        assert by_date[_dt(2012, 6, 1)] == pytest.approx(0.10)

    def test_live_failure_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))
        store = MarketDataStore(tmp_path)

        archived_path = tmp_path / "tickers" / "OLD.json"
        archived_path.parent.mkdir(parents=True)
        archived_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "info": {
                        "currency": "USD",
                        "exchange": "NMS",
                        "symbol": "OLD",
                        "longName": "Delisted",
                    },
                    "splits": [],
                    "dividends": [{"date": "2015-06-01", "dividend": 0.2}],
                }
            ),
            encoding="utf-8",
        )

        mock = MagicMock()
        mock.get_info.side_effect = MarketDataError("yfinance get_info failed")
        mock.splits = {}
        mock.get_dividends.return_value = {}

        monkeypatch.setattr(
            "investing.market_data_store.yf.Ticker",
            lambda _symbol: mock,
        )
        monkeypatch.setattr(
            "investing.market_data_store._call_with_retry",
            lambda fn, **kwargs: fn(),
        )

        with pytest.raises(MarketDataError, match="get_info failed"):
            store.resolve_ticker("OLD")

    def test_merge_fx_history_preserves_old_dates(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path)
        dates = np.array(["2010-01-01", "2011-01-01"], dtype="datetime64[D]")
        rates = np.array([1.1, 1.2], dtype=float)
        store.save_fx_history("EUR", dates, rates)

        live_dates = [datetime(2011, 1, 1).date(), datetime(2024, 1, 1).date()]
        live_rates = [1.25, 1.3]
        out_dates, out_rates = store.merge_fx_history("EUR", live_dates, live_rates)
        assert out_dates.size == 3
        assert out_rates[0] == pytest.approx(1.1)
        assert out_rates[-1] == pytest.approx(1.3)

    def test_concurrent_ticker_persist_updates_manifest(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))

        store = MarketDataStore(tmp_path)
        tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]

        def _mock_ticker(symbol: str) -> MagicMock:
            mock = MagicMock()
            mock.get_info.return_value = {
                "currency": "USD",
                "exchange": "NMS",
                "symbol": symbol,
                "longName": symbol,
                "regularMarketPrice": 1.0,
            }
            mock.splits = {}
            mock.get_dividends.return_value = {}
            return mock

        monkeypatch.setattr(
            "investing.market_data_store.yf.Ticker",
            _mock_ticker,
        )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(store.resolve_ticker, tickers))

        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert set(manifest["tickers"]) == set(tickers)
        for symbol in tickers:
            assert (tmp_path / "tickers" / f"{symbol}.json").is_file()

    def test_resolve_ticker_read_only_skips_disk_write(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("INVESTING_MARKET_DATA_PERSIST", "0")

        store = MarketDataStore(tmp_path, persist=False)
        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "longName": "Test",
            "regularMarketPrice": 10.0,
        }
        mock.splits = {}
        mock.get_dividends.return_value = {}

        monkeypatch.setattr(
            "investing.market_data_store.yf.Ticker",
            lambda _symbol: mock,
        )

        info, _, _ = store.resolve_ticker("TST")
        assert info["regularMarketPrice"] == pytest.approx(10.0)
        assert not (tmp_path / "tickers" / "TST.json").exists()
        assert not store.persist


class TestSplitInventoryChanged:
    def test_detects_new_split(self):
        archived = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        merged = [
            {"date": _dt(2020, 1, 1), "split": 2.0},
            {"date": _dt(2025, 1, 1), "split": 2.0},
        ]
        assert split_inventory_changed(archived, merged)

    def test_unchanged(self):
        splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        assert not split_inventory_changed(splits, splits)
