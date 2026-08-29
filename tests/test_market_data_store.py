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

    def test_live_failure_degrades_to_archive_without_a_price(self, tmp_path, monkeypatch):
        """A failed ``get_info`` serves archived metadata and flags it.

        The store no longer decides whether that is acceptable -- it
        cannot, because "is this position still open?" only falls out
        of replaying the trade ledger. It records ``from_archive`` and
        omits ``regularMarketPrice`` entirely; ``Holding.ledger``
        rejects the snapshot if the position turns out to be open.
        """
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

        resolved = store.resolve_ticker("OLD")

        assert resolved.from_archive is True
        # No stale price is invented; the key is simply absent.
        assert "regularMarketPrice" not in resolved.info
        assert resolved.info["longName"] == "Delisted"

    def test_a_degraded_read_is_never_persisted(self, tmp_path, monkeypatch):
        """The archive is the fallback of record; an outage must not touch it.

        Rewriting it with a copy of itself would at best be a no-op and
        at worst refresh ``updated_at`` to claim a currency the data
        does not have.
        """
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))
        store = MarketDataStore(tmp_path, persist=True)

        archived_path = tmp_path / "tickers" / "OLD.json"
        archived_path.parent.mkdir(parents=True)
        archived_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "info": {"currency": "USD", "exchange": "NMS", "symbol": "OLD"},
                    "splits": [],
                    "dividends": [],
                }
            ),
            encoding="utf-8",
        )
        before = archived_path.read_bytes()

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

        store.resolve_ticker("OLD")

        assert archived_path.read_bytes() == before
        assert not (tmp_path / "manifest.json").exists()

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


class TestArchiveFallbackBoundary:
    """Where the archive may stand in for a failed fetch -- and where it may not.

    The rule these tests pin down: the archive covers history, never
    the state of a position as of today. A build that cannot verify
    today's price or today's share count must fail rather than publish
    a number it has not confirmed.
    """

    @staticmethod
    def _archived_ticker(tmp_path, symbol="TST"):
        path = tmp_path / "tickers" / f"{symbol}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "info": {
                        "currency": "USD",
                        "exchange": "NMS",
                        "symbol": symbol,
                        "longName": "Test Corp",
                    },
                    "splits": [{"date": "2015-06-01", "split": 2.0}],
                    "dividends": [{"date": "2015-06-01", "dividend": 0.2}],
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _install(monkeypatch, mock):
        monkeypatch.setattr(
            "investing.market_data_store.yf.Ticker",
            lambda _symbol: mock,
        )
        monkeypatch.setattr(
            "investing.market_data_store._call_with_retry",
            lambda fn, **kwargs: fn(),
        )

    def _store(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))
        return MarketDataStore(tmp_path, persist=False)

    def test_dividend_failure_without_archive_still_raises(self, tmp_path, monkeypatch):
        """No archive means nothing to fall back to."""
        store = self._store(tmp_path, monkeypatch)

        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "regularMarketPrice": 10.0,
        }
        mock.splits = {}
        mock.get_dividends.side_effect = MarketDataError("yfinance get_dividends failed")
        self._install(monkeypatch, mock)

        with pytest.raises(MarketDataError, match="get_dividends"):
            store.resolve_ticker("TST")

    def test_split_failure_degrades_and_flags(self, tmp_path, monkeypatch):
        """A stale split inventory would misstate an *open* share count.

        The store cannot tell open from closed, so it serves the
        archived inventory and flags the snapshot. ``Holding.ledger``
        is what refuses to publish it if shares are still held --
        see ``TestArchivedDataIsOnlyForClosedPositions``.
        """
        store = self._store(tmp_path, monkeypatch)
        self._archived_ticker(tmp_path)

        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "regularMarketPrice": 10.0,
        }
        type(mock).splits = property(
            lambda _self: (_ for _ in ()).throw(MarketDataError("yfinance splits failed"))
        )
        mock.get_dividends.return_value = {}
        self._install(monkeypatch, mock)

        resolved = store.resolve_ticker("TST")

        assert resolved.from_archive is True
        # The archived inventory is what got served.
        assert [s["split"] for s in resolved.splits] == [pytest.approx(2.0)]
        # A live price was available, so it is present -- but the flag
        # still stands, because the share count is the unverified part.
        assert resolved.info["regularMarketPrice"] == pytest.approx(10.0)

    def test_live_price_failure_degrades_and_drops_the_price(self, tmp_path, monkeypatch):
        """No archived price is invented; the key is simply absent."""
        store = self._store(tmp_path, monkeypatch)
        self._archived_ticker(tmp_path)

        mock = MagicMock()
        mock.get_info.side_effect = MarketDataError("yfinance get_info failed")
        mock.splits = {}
        mock.get_dividends.return_value = {}
        self._install(monkeypatch, mock)

        resolved = store.resolve_ticker("TST")

        assert resolved.from_archive is True
        assert "regularMarketPrice" not in resolved.info

    def test_a_clean_read_is_not_flagged(self, tmp_path, monkeypatch):
        """The flag must not fire on the ordinary path."""
        store = self._store(tmp_path, monkeypatch)
        self._archived_ticker(tmp_path)

        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "regularMarketPrice": 10.0,
        }
        mock.splits = {}
        mock.get_dividends.return_value = {}
        self._install(monkeypatch, mock)

        assert store.resolve_ticker("TST").from_archive is False

    def test_a_dividend_only_failure_is_not_flagged(self, tmp_path, monkeypatch):
        """Dividends are historical; losing them says nothing about today."""
        store = self._store(tmp_path, monkeypatch)
        self._archived_ticker(tmp_path)

        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "regularMarketPrice": 10.0,
        }
        mock.splits = {}
        mock.get_dividends.side_effect = MarketDataError("yfinance get_dividends failed")
        self._install(monkeypatch, mock)

        resolved = store.resolve_ticker("TST")
        assert resolved.from_archive is False
        assert [d["dividend"] for d in resolved.dividends] == [pytest.approx(0.2)]

    def test_history_failure_falls_back_to_archive(self, tmp_path, monkeypatch):
        """Adjusted-close history is retrospective: the archive serves it."""
        store = self._store(tmp_path, monkeypatch)
        history_path = tmp_path / "history" / "TST.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "splits": [],
                    "adj_close": [
                        {"date": "2020-01-02", "adj_close": 100.0},
                        {"date": "2020-01-03", "adj_close": 101.0},
                    ],
                }
            ),
            encoding="utf-8",
        )

        def boom():
            raise MarketDataError("yfinance ticker history failed")

        frame = store.resolve_price_history(
            "TST",
            "2020-01-01",
            boom,
            merged_splits=[],
        )
        assert len(frame) == 2

    def test_history_failure_without_archive_raises(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)

        def boom():
            raise MarketDataError("yfinance ticker history failed")

        with pytest.raises(MarketDataError, match="history"):
            store.resolve_price_history("TST", "2020-01-01", boom, merged_splits=[])

    def test_info_failure_without_an_archive_propagates(self, tmp_path, monkeypatch):
        """No snapshot on disk means nothing to degrade to."""
        store = self._store(tmp_path, monkeypatch)

        mock = MagicMock()
        mock.get_info.side_effect = MarketDataError("yfinance get_info failed")
        mock.splits = {}
        mock.get_dividends.return_value = {}
        self._install(monkeypatch, mock)

        with pytest.raises(MarketDataError, match="get_info"):
            store.resolve_ticker("TST")

    def test_split_failure_without_an_archive_propagates(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)

        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "regularMarketPrice": 10.0,
        }
        type(mock).splits = property(
            lambda _self: (_ for _ in ()).throw(MarketDataError("yfinance splits failed"))
        )
        mock.get_dividends.return_value = {}
        self._install(monkeypatch, mock)

        with pytest.raises(MarketDataError, match="splits"):
            store.resolve_ticker("TST")

    def test_resolved_ticker_unpacks_as_a_triple(self, tmp_path, monkeypatch):
        """The dataclass keeps the historical tuple call shape working."""
        store = self._store(tmp_path, monkeypatch)
        mock = MagicMock()
        mock.get_info.return_value = {
            "currency": "USD",
            "exchange": "NMS",
            "symbol": "TST",
            "regularMarketPrice": 10.0,
        }
        mock.splits = {}
        mock.get_dividends.return_value = {}
        self._install(monkeypatch, mock)

        resolved = store.resolve_ticker("TST")
        info, splits, dividends = resolved

        assert info is resolved.info
        assert splits is resolved.splits
        assert dividends is resolved.dividends


class TestRefreshEntrypoints:
    def test_refresh_ticker_is_a_no_op_when_read_only(self, tmp_path, monkeypatch):
        """The monthly cron is the only writer; routine deploys must not persist."""
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path, persist=False)
        called = []
        monkeypatch.setattr(store, "resolve_ticker", lambda t: called.append(t))

        store.refresh_ticker("TST")
        assert called == []

    def test_refresh_universe_walks_every_ticker(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path, persist=True)
        seen: list[str] = []
        monkeypatch.setattr(store, "resolve_ticker", lambda t: seen.append(t))

        store.refresh_universe(["AAA", "BBB"])
        assert seen == ["AAA", "BBB"]

    def test_fx_path_sanitises_a_separator_bearing_code(self, tmp_path, monkeypatch):
        """A currency code from the sheet must not escape the snapshot tree."""
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path)

        path = store._fx_path("../../etc/passwd")

        assert tmp_path.resolve() in path.resolve().parents
        assert path.name == "..-..-etc-passwd.npz"

    def test_history_is_persisted_when_writes_are_enabled(self, tmp_path, monkeypatch):
        """The monthly cron writes the merged adj-close series back."""
        import pandas as pd

        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))
        store = MarketDataStore(tmp_path, persist=True)

        frame = pd.DataFrame(
            {"Adj Close": [100.0, 101.0]},
            index=pd.DatetimeIndex([datetime(2024, 1, 2), datetime(2024, 1, 3)]),
        )
        store.resolve_price_history("TST", "2024-01-01", lambda: frame, merged_splits=[])

        payload = json.loads((tmp_path / "history" / "TST.json").read_text(encoding="utf-8"))
        assert [row["adj_close"] for row in payload["adj_close"]] == [100.0, 101.0]
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert "TST" in manifest["history"]

    def test_history_is_not_persisted_when_read_only(self, tmp_path, monkeypatch):
        import pandas as pd

        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        monkeypatch.setenv("INVESTING_MARKET_DATA_DIR", str(tmp_path))
        store = MarketDataStore(tmp_path, persist=False)

        frame = pd.DataFrame(
            {"Adj Close": [100.0]},
            index=pd.DatetimeIndex([datetime(2024, 1, 2)]),
        )
        store.resolve_price_history("TST", "2024-01-01", lambda: frame, merged_splits=[])

        assert not (tmp_path / "history").exists()

    def test_a_rebase_mismatch_prefers_the_live_row(self, caplog):
        """When a re-based archive row disagrees with live, live wins.

        The archive is re-based across newly-observed splits so its old
        rows land in the current share frame. If that arithmetic lands
        somewhere other than the live value for the same date, one of
        the two is wrong about the split -- and live is the side that
        just came from the vendor.
        """
        archived_splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        merged_splits = [
            {"date": _dt(2020, 1, 1), "split": 2.0},
            {"date": _dt(2025, 5, 1), "split": 2.0},
        ]
        archived = [{"date": _dt(2018, 6, 1), "dividend": 1.00}]
        # Re-basing 1.00 across the new 2:1 gives 0.50; live disagrees.
        live = [{"date": _dt(2018, 6, 1), "dividend": 0.90}]

        with caplog.at_level("WARNING"):
            merged = merge_time_series(
                archived,
                live,
                value_key="dividend",
                archived_splits=archived_splits,
                merged_splits=merged_splits,
            )

        assert merged[0]["dividend"] == pytest.approx(0.90)
        assert "differs after re-base" in caplog.text


class TestMergeHelperEdges:
    def test_a_revised_factor_on_a_known_date_counts_as_changed(self):
        """Yahoo restating a split factor must invalidate the archive.

        A date already on file with a *different* factor is a revision,
        not a no-op: every dividend and close before it needs re-basing
        into the corrected share frame.
        """
        archived = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        merged = [{"date": _dt(2020, 1, 1), "split": 3.0}]
        assert split_inventory_changed(archived, merged) is True

    def test_an_identical_inventory_is_unchanged(self):
        splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        assert split_inventory_changed(splits, list(splits)) is False

    def test_rebasing_across_no_new_splits_is_the_identity(self):
        """A row after every new split needs no adjustment at all."""
        from investing.market_data_store import _rebase_amount

        new_splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        # Dividend dated *after* the split: factor stays 1.0.
        assert _rebase_amount(1.25, _dt(2021, 6, 1), new_splits) == pytest.approx(1.25)

    def test_rebasing_before_a_new_split_divides_it_out(self):
        from investing.market_data_store import _rebase_amount

        new_splits = [{"date": _dt(2020, 1, 1), "split": 2.0}]
        assert _rebase_amount(1.00, _dt(2019, 6, 1), new_splits) == pytest.approx(0.5)


class TestDisabledStore:
    """With no root every read is a miss and every write a no-op."""

    def test_loading_a_snapshot_returns_nothing(self):
        store = MarketDataStore(None)
        assert store.enabled is False
        assert store._load_ticker_snapshot("TST") is None

    def test_touching_the_manifest_is_a_no_op(self):
        store = MarketDataStore(None)
        store._touch_manifest("tickers", "TST", "deadbeef")  # must not raise

    def test_loading_fx_history_returns_nothing(self):
        assert MarketDataStore(None).load_fx_history("EUR") is None

    def test_saving_fx_history_is_a_no_op(self):
        MarketDataStore(None).save_fx_history(
            "EUR",
            np.array(["2024-01-01"], dtype="datetime64[D]"),
            np.array([1.1], dtype=float),
        )

    def test_an_unwritable_fx_path_is_swallowed(self, tmp_path, monkeypatch):
        """A failed snapshot write must not take the build down.

        The FX archive is a convenience, not a correctness input: the
        live series was already fetched and is in memory. Losing the
        write costs one refetch next run.
        """
        monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
        store = MarketDataStore(tmp_path, persist=True)
        # A *file* where the ``fx/`` directory needs to be: mkdir fails.
        (tmp_path / "fx").write_text("not a directory", encoding="utf-8")

        store.save_fx_history(
            "EUR",
            np.array(["2024-01-01"], dtype="datetime64[D]"),
            np.array([1.1], dtype=float),
        )
