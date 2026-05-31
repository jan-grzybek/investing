"""Shared fixtures and synthetic-data factories for the Webpage
test files. Split out of the historical monolithic ``test_webpage.py``
so each topical test module can import the same helpers without
duplicating them.
"""
from __future__ import annotations

import math
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import update
from update import Webpage, LOGOS_ADDRESS


def _holding(
    *,
    ticker="NMS:AAA",
    name="Alpha",
    tsr=12.3,
    cagr=4.5,
    is_current=True,
    weight=10.0,
    periods=None,
):
    return {
        "ticker": ticker,
        "name": name,
        "tsr%": tsr,
        "cagr%": cagr,
        "is_current": is_current,
        "current_weight%": weight,
        "current_value_usd": 1000.0,
        "periods": periods or [{"start": datetime(2024, 1, 1), "end": None}],
        "latest_buy": datetime(2024, 1, 1),
        "latest_sell": None,
    }


def _total_return():
    return {
        "start_date": datetime(2024, 1, 1),
        "history": [(datetime(2024, 1, 1), 1.0)],
        "twr%": 25.0,
        "cagr%": 12.5,
    }


def _benchmark():
    return {
        "ticker": "LSE:VUAA.L",
        "name": "S&P 500 ETF",
        "tsr%": 10.0,
        "cagr%": 5.0,
        "periods": [{"start": datetime(2024, 1, 1), "end": None}],
    }


@pytest.fixture
def stub_logo_lookup(monkeypatch):
    """Avoid all HTTP traffic from ``_get_logo_url``."""
    resp = MagicMock()
    resp.status_code = 200
    monkeypatch.setattr(update.requests, "head", lambda url: resp)  # noqa: ARG005


def _trade_event(
    *,
    ticker="NMS:AAA",
    name="Alpha Inc.",
    currency="USD",
    category="OPEN",
    price=100.0,
    start=None,
    end=None,
    delta_pct=None,
):
    """Match the shape ``Holding.trade_events`` produces."""
    start = start or datetime(2024, 6, 1)
    end = end or start
    return {
        "ticker": ticker,
        "name": name,
        "currency": currency,
        "category": category,
        "price": price,
        "start_date": start,
        "end_date": end,
        "delta_pct": delta_pct,
    }


