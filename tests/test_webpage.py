"""Tests for the ``Webpage`` HTML builder.

We don't validate the exact markup byte-for-byte; instead we assert on
structural invariants (sections present, holding cards rendered once,
sentinel values appearing in the right form, etc.).
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


class TestInit:
    def test_starts_empty(self):
        w = Webpage()
        assert w.desktop_return == ""
        assert w.mobile_return == ""
        assert w.desktop_current == []
        assert w.desktop_historical == []
        assert w.mobile_current == []
        assert w.mobile_historical == []


class TestGetLogoUrl:
    def test_returns_first_extension_that_responds_200(self, monkeypatch):
        calls = []

        def fake_head(url):
            calls.append(url)
            resp = MagicMock()
            # PNG (the second extension probed) is the first one that exists.
            resp.status_code = 200 if url.endswith(".png") else 404
            return resp

        monkeypatch.setattr(update.requests, "head", fake_head)

        w = Webpage()
        url = w._get_logo_url("NMS:AAA")
        assert url == LOGOS_ADDRESS + "NMS%3AAAA.png"
        # Confirms we tried .svg first.
        assert calls[0].endswith(".svg")

    def test_falls_back_to_courage_when_no_extension_matches(self, monkeypatch):
        resp = MagicMock()
        resp.status_code = 404
        monkeypatch.setattr(update.requests, "head", lambda url: resp)  # noqa: ARG005

        w = Webpage()
        assert w._get_logo_url("NMS:UNKNOWN") == LOGOS_ADDRESS + "courage.png"


class TestAddReturn:
    def test_desktop_and_mobile_strings_are_populated(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])

        assert "TWR:" in w.desktop_return
        assert "25.0%" in w.desktop_return
        assert "CAGR:" in w.desktop_return
        assert "12.5%" in w.desktop_return
        assert "LSE:VUAA.L" in w.desktop_return
        assert "TWR:" in w.mobile_return
        assert "LSE:VUAA.L" in w.mobile_return

    def test_works_with_no_benchmarks(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        assert "TWR:" in w.desktop_return
        # No benchmark sections rendered.
        assert "VUAA" not in w.desktop_return


class TestAddHolding:
    def test_current_holding_appears_in_current_bucket(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(is_current=True))

        assert len(w.desktop_current) == 1
        assert len(w.mobile_current) == 1
        assert w.desktop_historical == []
        assert w.mobile_historical == []
        assert "Weight:" in w.desktop_current[0]
        assert "10.0%" in w.desktop_current[0]

    def test_historical_holding_appears_in_historical_bucket(self, stub_logo_lookup):
        h = _holding(
            is_current=False,
            weight=None,
            periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
        )
        w = Webpage()
        w.add_holding(h)

        assert len(w.desktop_historical) == 1
        assert len(w.mobile_historical) == 1
        assert w.desktop_current == []
        # No weight rendered for closed positions.
        assert "Weight:" not in w.desktop_historical[0]
        # Closed period renders a real end date, not "Present".
        assert "Jan 01, 2024" in w.desktop_historical[0]

    def test_cagr_above_sentinel_renders_as_tba(self, stub_logo_lookup):
        # The check uses `math.nextafter(1_000_000, 0)`; anything strictly
        # greater than that triggers the "TBA" branch.
        sentinel_cagr = 1_000_000  # > nextafter(1_000_000, 0)
        assert sentinel_cagr > math.nextafter(1_000_000, 0)

        h = _holding(cagr=sentinel_cagr)
        w = Webpage()
        w.add_holding(h)
        assert "TBA" in w.desktop_current[0]
        assert "TBA" in w.mobile_current[0]

    def test_open_period_renders_present(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(periods=[{"start": datetime(2024, 1, 1), "end": None}]))
        assert "Present" in w.desktop_current[0]


class TestSave:
    def test_writes_index_html_with_key_sections(
        self, stub_logo_lookup, chdir_tmp, freeze_today
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        w.add_holding(_holding(ticker="NMS:CURR", is_current=True))
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}
                ],
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert out.startswith("<!DOCTYPE html>")
        assert out.rstrip().endswith("</html>")
        assert "<title>JG Investing</title>" in out
        assert "All-time performance" in out
        assert "Current holdings" in out
        assert "Historical holdings" in out
        # Both desktop and mobile sections are emitted.
        assert "desktop-version" in out
        assert "mobile-version" in out
        # The frozen date appears in the footer.
        assert "Jun 1, 2025" in out

    def test_save_without_current_holdings_skips_section(
        self, stub_logo_lookup, chdir_tmp, freeze_today
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        # Only a historical holding.
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}
                ],
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert "Historical holdings" in out
        assert "Current holdings" not in out
