"""Additional tests for investing.cli helpers not covered elsewhere."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from investing.cli import (
    _collect_market_data_tickers,
    _configure_logging,
    snapshot_market_data,
)
from investing.log import logger
from investing.market_data_store import MarketDataStore


def test_configure_logging_skips_when_handlers_present():
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        before = len(logger.handlers)
        _configure_logging()
        assert len(logger.handlers) == before
    finally:
        logger.removeHandler(handler)


def test_configure_logging_adds_stderr_handler():
    saved = list(logger.handlers)
    for handler in saved:
        logger.removeHandler(handler)
    try:
        _configure_logging()
        assert logger.handlers
        assert logger.level == logging.INFO
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.propagate = True


def test_collect_market_data_tickers_deduplicates_and_includes_benchmarks():
    txns = [
        {"ticker": "NMS:AAPL"},
        {"ticker": "NMS:MSFT"},
        {"ticker": "NMS:AAPL"},
    ]
    fixed: list = []
    tickers = _collect_market_data_tickers(txns, fixed)
    assert tickers[0] == "NMS:AAPL"
    assert tickers[1] == "NMS:MSFT"
    assert "VUAA.L" in tickers


def test_snapshot_market_data_noop_when_disabled(monkeypatch, caplog):
    monkeypatch.setenv("INVESTING_MARKET_DATA_DISABLE", "1")
    monkeypatch.setattr("investing.cli._configure_logging", lambda *args, **kwargs: None)
    store = MarketDataStore.from_env()
    assert not store.enabled

    def _pull():
        raise AssertionError("pull should not run when store is disabled")

    with caplog.at_level(logging.INFO, logger=logger.name):
        snapshot_market_data(pull=_pull, store=store)
    assert "market-data snapshots disabled" in caplog.text


def test_snapshot_market_data_refreshes_universe(monkeypatch):
    monkeypatch.delenv("INVESTING_MARKET_DATA_DISABLE", raising=False)
    store = MagicMock(spec=MarketDataStore)
    store.enabled = True

    def _pull():
        return ([{"ticker": "NMS:AAA"}], [], [], [])

    snapshot_market_data(pull=_pull, store=store)
    store.refresh_universe.assert_called_once()
    tickers = store.refresh_universe.call_args[0][0]
    assert "NMS:AAA" in tickers
