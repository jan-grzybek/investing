"""Tests for :mod:`investing.market_data`.

The retry helper is small but load-bearing: it wraps every
yfinance read in the production pipeline. The tests exercise its
shape (success path, failure path, attempt budget, chained
exception) without touching the network.
"""

from __future__ import annotations

import pytest

from investing import retry
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
    monkeypatch.setattr(retry.time, "sleep", lambda d: sleeps.append(d))

    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] < 3:
            raise RuntimeError("transient")
        return 42

    assert _call_with_retry(fn, description="probe") == 42
    assert state["calls"] == 3
    # Equal jitter: each delay sits in ``[nominal/2, nominal]`` so the
    # back-off still grows while eight concurrent workers land on
    # different schedules. Asserting the band rather than the point
    # keeps the test honest about what the policy actually promises.
    assert len(sleeps) == 2
    assert 0.25 <= sleeps[0] <= 0.5
    assert 0.5 <= sleeps[1] <= 1.0
    assert sleeps[1] >= sleeps[0]


def test_raises_marketdataerror_after_exhausting_attempts(monkeypatch):
    """After the configured budget the helper raises ``MarketDataError``."""
    monkeypatch.setattr(retry.time, "sleep", lambda d: None)  # noqa: ARG005

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
    monkeypatch.setattr(retry.time, "sleep", lambda d: None)  # noqa: ARG005
    calls = []

    def fn():
        calls.append(1)
        raise RuntimeError("nope")

    with pytest.raises(MarketDataError):
        _call_with_retry(fn, description="probe", attempts=5, base_delay=0.0)
    assert len(calls) == 5


def test_integration_shape_errors_are_not_retried(monkeypatch):
    """``AttributeError`` / ``TypeError`` propagate unwrapped, first try.

    These signal a vendor API break (a renamed method, a changed
    return shape), not a transient outage. Retrying them burns the
    budget and, worse, relabels the break as ``MarketDataError`` --
    so a yfinance release that dropped ``Ticker.get_info`` would read
    as "Yahoo is down" in the job log instead of "the integration no
    longer matches the library".
    """
    monkeypatch.setattr(retry.time, "sleep", lambda d: None)  # noqa: ARG005

    for exc_type in (AttributeError, TypeError):
        calls = []

        def fn(exc_type=exc_type):
            calls.append(1)
            raise exc_type("shape changed")

        with pytest.raises(exc_type):
            _call_with_retry(fn, description="probe")
        assert calls == [1], f"{exc_type.__name__} should not be retried"


def test_key_error_is_still_retried(monkeypatch):
    """``KeyError`` stays retryable: a partial vendor payload clears."""
    monkeypatch.setattr(retry.time, "sleep", lambda d: None)  # noqa: ARG005
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] < 2:
            raise KeyError("partial payload")
        return "ok"

    assert _call_with_retry(fn, description="probe") == "ok"
    assert state["calls"] == 2
