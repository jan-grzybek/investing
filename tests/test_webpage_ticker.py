"""Marquee ticker strip rendering."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from investing.webpage import Webpage
from tests._webpage_support import _holding, stub_logo_lookup


class TestTicker:
    def test_returns_empty_string_when_no_current_holdings(self, stub_logo_lookup):
        w = Webpage()
        # Only a closed/historical position.
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
            )
        )
        assert w._build_ticker() == ""

    def test_renders_one_logo_per_current_holding_doubled(self, stub_logo_lookup):
        # Track is rendered with two copies of the logo set so the
        # marquee keyframe can loop seamlessly via translateX(-50%).
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha Inc."))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta Co."))
        out = w._build_ticker()
        assert 'class="ticker"' in out
        assert 'aria-hidden="true"' in out
        assert out.count('class="ticker__logo"') == 4  # 2 logos x 2 copies
        # Ticker + name surface via the ``title`` attribute so hovering
        # any logo identifies the underlying holding.
        assert 'title="NMS:AAA - Alpha Inc."' in out
        assert 'title="NMS:BBB - Beta Co."' in out
        # Image attrs: ticker is above the fold so loads eagerly, but
        # async decode keeps the marquee painting as soon as the
        # first logo is ready. Both width and height are pinned to
        # the desktop cell dimensions (56x28 -- a landscape 2:1 box
        # that ``object-fit: contain`` letterboxes wide wordmarks
        # and square logos into for similar visual prominence) so
        # the browser reserves the exact box up-front and the
        # marquee paints with zero layout shift even before
        # individual SVGs decode. CSS overrides scale the cell down
        # on narrow viewports.
        assert 'decoding="async"' in out
        assert 'width="56"' in out
        assert 'height="28"' in out
        # Lazy loading would be wrong here: the marquee animates from
        # the moment the page paints, off-screen logos must already be
        # decoded.
        assert 'loading="lazy"' not in out

    def test_excludes_historical_holdings(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:LIVE", is_current=True))
        w.add_holding(
            _holding(
                ticker="NMS:DEAD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
            )
        )
        out = w._build_ticker()
        assert "NMS:LIVE" in out
        assert "NMS:DEAD" not in out

    def test_each_logo_is_wrapped_in_anchor_to_holding_capsule(
        self,
        stub_logo_lookup,
    ):
        # Clicking a marquee logo should scroll to the matching
        # holding capsule below. Every logo gets its own ``<a>``
        # wrapper with ``href`` pointing at the capsule's ``id`` and
        # ``tabindex="-1"`` so the visually-hidden marquee never
        # captures keyboard focus -- pointer users still get the
        # navigation, keyboard users keep their tab order intact.
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha Inc."))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta Co."))
        out = w._build_ticker()
        # One anchor per logo per copy (2 logos x 2 copies = 4).
        assert out.count('class="ticker__link"') == 4
        # Each anchor targets the matching capsule's slug-id.
        assert 'href="#holding-NMS-AAA"' in out
        assert 'href="#holding-NMS-BBB"' in out
        # Keyboard-skip the marquee: links carry ``tabindex="-1"``.
        assert out.count('tabindex="-1"') == 4

    def test_logo_anchors_wrap_the_img(self, stub_logo_lookup):
        # The ``<img>`` must sit *inside* the ``<a>`` so the entire
        # logo cell is the click target (not just a 1px-wide gap
        # next to it). The renderer emits ``<a ...><img ...></a>``
        # in that order on a single line.
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA"))
        out = w._build_ticker()
        # Find the first anchor opening and confirm the img tag
        # appears between it and the matching closing </a>.
        anchor_open = '<a class="ticker__link"'
        assert anchor_open in out
        slice_ = out.split(anchor_open, 1)[1]
        anchor_block = slice_.split("</a>", 1)[0]
        assert '<img class="ticker__logo"' in anchor_block

    def test_logo_lookups_are_cached(self):
        # Adding the same ticker twice (e.g. both a current and a past
        # position for the same instrument under different test setups)
        # should only HEAD-probe its logo extensions once. The cache
        # now lives on the injectable ``LogoCache`` rather than as a
        # per-Webpage dict, so we plant a session-level stub and
        # check the call count there.
        from investing.logos import LogoCache

        calls = []

        def fake_head(url, timeout=None):  # noqa: ARG001
            calls.append(url)
            resp = MagicMock()
            resp.status_code = 200  # First extension wins immediately.
            return resp

        session = MagicMock()
        session.head.side_effect = fake_head
        w = Webpage(logo_cache=LogoCache(session=session))
        w._get_logo_url("NMS:AAA")
        w._get_logo_url("NMS:AAA")
        w._get_logo_url("NMS:AAA")
        # Single probe even though we asked for the URL three times.
        assert len(calls) == 1
