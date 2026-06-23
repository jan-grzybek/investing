"""Shared fixtures and synthetic-data factories for the Webpage
test files. Split out of the historical monolithic ``test_webpage.py``
so each topical test module can import the same helpers without
duplicating them.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from investing.logos import _DEFAULT_LOGO_ASPECT
from investing.paths import LOGOS_ADDRESS


class AspectStubCache:
    """Test double that satisfies both halves of the logo resolver API.

    Production renders go through :class:`investing.logos.LogoCache`,
    which exposes ``__call__(ticker) -> str`` for the URL lookup and
    ``aspect_ratio(ticker) -> float`` for the equal-area sizing math
    (see :mod:`investing.webpage.sector_treemap`). The default
    ``stub_logo_lookup`` fixture only patches ``LogoCache.__call__``
    and lets ``aspect_ratio`` parse whatever local SVG file matches
    the ticker (defaulting to ``_DEFAULT_LOGO_ASPECT`` when no SVG
    is on disk). This helper is the explicit-aspect counterpart:
    it returns the configured aspect for any ticker in ``aspects``
    and the parser's default for anything else, with the URL lookup
    mirroring the fixture's deterministic ``ticker.svg`` shape so
    the renderer can still emit a usable ``src``.
    """

    def __init__(self, aspects):
        self._aspects = aspects

    def __call__(self, ticker):
        encoded = ticker.replace(":", "%3A")
        return f"{LOGOS_ADDRESS}{encoded}.svg"

    def aspect_ratio(self, ticker):
        return self._aspects.get(ticker, _DEFAULT_LOGO_ASPECT)


def _holding(
    *,
    ticker="NMS:AAA",
    name="Alpha",
    tsr=12.3,
    cagr=4.5,
    is_current=True,
    weight=10.0,
    periods=None,
    website="https://www.alpha.example",
    sector="Technology",
    asset_class="equity",
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
        # Click target wired onto the capsule's logo wrapper. The
        # production summary path fills this from yfinance via
        # ``Holding.resolve_company_url``; tests pin a deterministic
        # value so assertions on the rendered ``href`` don't depend
        # on the live ``info`` payload.
        "website": website,
        # Sector tag mirrors ``info["sector"]`` from yfinance. The
        # default is ``"Technology"`` so the synthetic holding gets
        # a stable, non-empty bucket in the equities treemap; tests
        # that exercise the empty-sector / "Other" fallback can
        # pass ``sector=""`` explicitly.
        "sector": sector,
        # Asset class tag the renderer's bucketing reads. Defaults to
        # ``"equity"`` so the legacy fixture path keeps producing
        # equity-side cards; pass ``asset_class="fixed_income"`` to
        # exercise the dedicated Fixed Income sub-section paths.
        "asset_class": asset_class,
    }


MOBILE_TREEMAP_CANVAS_W = 358.0
MOBILE_TREEMAP_CANVAS_H = MOBILE_TREEMAP_CANVAS_W * 0.75
DESKTOP_TREEMAP_CANVAS_W = 832.0
DESKTOP_TREEMAP_CANVAS_H = DESKTOP_TREEMAP_CANVAS_W / 2


def _stub_logo_url(ticker: str) -> str:
    encoded = ticker.replace(":", "%3A")
    return f"{LOGOS_ADDRESS}{encoded}.svg"


def treemap_layout_block(
    holdings,
    *,
    canvas_w: float = MOBILE_TREEMAP_CANVAS_W,
    canvas_h: float = MOBILE_TREEMAP_CANVAS_H,
    logo_url_for=_stub_logo_url,
    logo_aspect_for=None,
    logo_coverage_for=None,
) -> str:
    """Client-parity treemap tile HTML at a reference canvas size."""
    from investing.webpage.sector_treemap import layout_at_canvas_block

    return layout_at_canvas_block(
        holdings,
        canvas_w,
        canvas_h,
        logo_url_for=logo_url_for,
        logo_aspect_for=logo_aspect_for,
        logo_coverage_for=logo_coverage_for,
    )


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
    """Avoid all HTTP traffic from ``Webpage._get_logo_url``.

    Replaces :class:`investing.logos.LogoCache.__call__` with a
    deterministic stub that mirrors the historical "all extensions
    return 200" behaviour: the first probed extension (``.svg``)
    wins, so the resolved URL is ``<LOGOS_ADDRESS><ticker>.svg``
    with the colon URL-encoded the way ``LogoCache`` does it.
    """
    from investing.logos import LogoCache

    def _stub(self, ticker):  # noqa: ARG001
        encoded = ticker.replace(":", "%3A")
        return f"{LOGOS_ADDRESS}{encoded}.svg"

    monkeypatch.setattr(LogoCache, "__call__", _stub)


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
