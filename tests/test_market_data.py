"""Tests for :mod:`investing.market_data`.

The retry helper is small but load-bearing: it wraps every
yfinance read in the production pipeline. The tests exercise its
shape (success path, failure path, attempt budget, chained
exception) without touching the network.
"""

from __future__ import annotations

import pytest

from investing import market_data
from investing.market_data import MarketDataError, _call_with_retry


def test_returns_value_on_first_success():
    """Successful calls must not retry or sleep."""
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert _call_with_retry(fn, description="probe") == "ok"
    assert calls == [1]


def test_retries_until_success(monkeypatch):
    """A transient failure is absorbed; the eventual value reaches the caller."""
    sleeps: list[float] = []
    monkeypatch.setattr(market_data.time, "sleep", lambda d: sleeps.append(d))

    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] < 3:
            raise RuntimeError("transient")
        return 42

    assert _call_with_retry(fn, description="probe") == 42
    assert state["calls"] == 3
    # Back-off doubles each step.
    assert sleeps == pytest.approx([0.5, 1.0])


def test_raises_marketdataerror_after_exhausting_attempts(monkeypatch):
    """After the configured budget the helper raises ``MarketDataError``."""
    monkeypatch.setattr(market_data.time, "sleep", lambda d: None)  # noqa: ARG005

    def fn():
        raise ValueError("permanent failure")

    with pytest.raises(MarketDataError) as excinfo:
        _call_with_retry(fn, description="probe", attempts=2, base_delay=0.0)

    # The underlying cause is preserved so the leak-safe traceback
    # walker can still surface the offending frame.
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_disabled_via_env(monkeypatch):
    """``INVESTING_DISABLE_RETRY=1`` collapses the budget to one attempt."""
    monkeypatch.setenv("INVESTING_DISABLE_RETRY", "1")
    calls = []

    def fn():
        calls.append(1)
        raise RuntimeError("nope")

    with pytest.raises(MarketDataError):
        _call_with_retry(fn, description="probe")
    assert calls == [1]


def test_respects_custom_attempts(monkeypatch):
    """``attempts=`` argument overrides the default budget."""
    monkeypatch.setattr(market_data.time, "sleep", lambda d: None)  # noqa: ARG005
    calls = []

    def fn():
        calls.append(1)
        raise RuntimeError("nope")

    with pytest.raises(MarketDataError):
        _call_with_retry(fn, description="probe", attempts=5, base_delay=0.0)
    assert len(calls) == 5
