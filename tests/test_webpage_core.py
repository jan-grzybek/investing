"""Webpage construction, logo URL resolution, anchor helpers,
the allocation chart, and the sticky site header."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from investing.logos import LogoCache
from investing.paths import COURAGE_LOGO, LOGOS_ADDRESS
from investing.webpage import Webpage
from tests._webpage_support import (
    _holding,
    _total_return,
    stub_logo_lookup,
)


def _make_session_stub(*, ok_extensions):
    """Build a ``requests.Session`` substitute whose ``head`` returns 200
    for URLs ending in any of ``ok_extensions`` and 404 otherwise."""
    calls: list[str] = []

    def fake_head(url, timeout=None):  # noqa: ARG001
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200 if any(url.endswith(ext) for ext in ok_extensions) else 404
        return resp

    session = MagicMock()
    session.head.side_effect = fake_head
    return session, calls


class TestInit:
    def test_starts_empty(self):
        w = Webpage()
        assert w.return_html == ""
        assert w.current == []
        assert w.historical == []
        assert w.allocation_pct is None
        assert w.top_10 is None


class TestGetLogoUrl:
    def test_returns_first_extension_that_responds_200(self):
        session, calls = _make_session_stub(ok_extensions=(".png",))
        w = Webpage(logo_cache=LogoCache(session=session))

        url = w._get_logo_url("NMS:AAA")
        assert url == LOGOS_ADDRESS + "NMS%3AAAA.png"
        # Confirms we tried .svg first.
        assert calls[0].endswith(".svg")

    def test_falls_back_to_courage_when_no_extension_matches(self):
        session, _ = _make_session_stub(ok_extensions=())
        w = Webpage(logo_cache=LogoCache(session=session))

        assert w._get_logo_url("NMS:UNKNOWN") == COURAGE_LOGO

    def test_caches_both_hits_and_misses(self):
        """Looking up the same ticker twice must not re-probe the network."""
        session, calls = _make_session_stub(ok_extensions=())
        w = Webpage(logo_cache=LogoCache(session=session))

        w._get_logo_url("NMS:X")
        first_round = list(calls)
        w._get_logo_url("NMS:X")
        assert calls == first_round  # No additional HEADs on the second call.

    def test_network_error_falls_through_to_next_extension(self):
        """A RequestException on one extension must not abort the resolution."""
        import requests as _requests

        calls: list[str] = []

        def flaky_head(url, timeout=None):  # noqa: ARG001
            calls.append(url)
            if url.endswith(".svg"):
                raise _requests.ConnectionError("simulated network drop")
            resp = MagicMock()
            resp.status_code = 200 if url.endswith(".png") else 404
            return resp

        session = MagicMock()
        session.head.side_effect = flaky_head
        w = Webpage(logo_cache=LogoCache(session=session))

        url = w._get_logo_url("NMS:X")
        assert url == LOGOS_ADDRESS + "NMS%3AX.png"
        # .svg raised; .png returned 200; we never reached .jpg.
        assert [c.rsplit(".", 1)[1] for c in calls] == ["svg", "png"]


class TestHoldingAnchor:
    def test_strips_punctuation_to_a_dash_form(self):
        # Tickers carry exchange prefixes and dotted suffixes
        # (``NMS:AAPL``, ``LSE:VUAA.L``) that aren't URL-fragment
        # friendly. The slug keeps alphanumerics and replaces every
        # other run with a single dash so the produced ``id`` /
        # ``href`` round-trip cleanly through ``location.hash``.
        assert Webpage._holding_anchor("NMS:AAPL") == "holding-NMS-AAPL"
        assert Webpage._holding_anchor("LSE:VUAA.L") == "holding-LSE-VUAA-L"

    def test_trims_leading_and_trailing_punctuation(self):
        # Defensive: a degenerate ticker shouldn't yield a hanging
        # trailing dash that turns into a brittle ``id``.
        assert Webpage._holding_anchor(".AAA.") == "holding-AAA"

    def test_is_deterministic(self):
        # The marquee, the bar chart, and the capsule renderer all
        # call this independently; their results have to agree.
        same = [Webpage._holding_anchor("NMS:AAA") for _ in range(3)]
        assert same == ["holding-NMS-AAA"] * 3


class TestAddAllocations:
    def test_stores_values_for_save(self):
        w = Webpage()
        w.add_allocations({"Equities": 95.4}, {"NMS:AAA": 50.0})
        assert w.allocation_pct == {"Equities": 95.4}
        assert w.top_10 == {"NMS:AAA": 50.0}


class TestBuildSiteHeader:
    def test_renders_title_and_links_to_existing_sections(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(is_current=True))
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )

        out = w._build_site_header()
        assert '<header class="site-header">' in out
        assert "Jan Grzybek Investment Portfolio" in out
        # Three links, one per existing section, in document order.
        perf = out.index('href="#performance"')
        curr = out.index('href="#current"')
        hist = out.index('href="#historical"')
        assert perf < curr < hist
        # Nav exposes an aria-label so screen readers can identify it.
        assert 'aria-label="Page sections"' in out

    def test_omits_nav_when_only_one_section_exists(self):
        # Bare Webpage -> only the (empty) performance slot is reachable;
        # a single-link nav adds visual noise without value, so we drop it.
        w = Webpage()
        out = w._build_site_header()
        assert "Jan Grzybek Investment Portfolio" in out
        assert "site-nav" not in out


class TestSectorTreemapLayout:
    """The squarified algorithm has a couple of structural invariants
    that are easier to assert at the helper-function level than via
    the end-to-end ``Webpage`` flow: tile areas must sum to the canvas
    area, no tile can fall outside the canvas, and degenerate inputs
    (single item, all-zero weights, empty list) must short-circuit
    without raising."""

    @staticmethod
    def _placed_tiles(values):
        from investing.webpage.sector_treemap import _squarify, _Tile

        canvas = _Tile(0.0, 0.0, 100.0, 100.0)
        return _squarify(values, canvas)

    def test_single_item_fills_the_canvas(self):
        (tile,) = self._placed_tiles([42.0])
        assert (tile.x, tile.y) == (0.0, 0.0)
        assert tile.w == 100.0 and tile.h == 100.0

    def test_areas_sum_to_canvas_area(self):
        values = [50.0, 25.0, 15.0, 10.0]
        tiles = self._placed_tiles(values)
        total_area = sum(t.w * t.h for t in tiles)
        # 100 x 100 canvas -> 10_000 square percentage-points;
        # squarified layout must place every input atom and waste
        # nothing.
        assert abs(total_area - 10_000.0) < 1e-6

    def test_all_tiles_stay_inside_the_canvas(self):
        tiles = self._placed_tiles([10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0])
        for tile in tiles:
            assert 0.0 <= tile.x <= 100.0
            assert 0.0 <= tile.y <= 100.0
            assert 0.0 <= tile.w <= 100.0 + 1e-9
            assert 0.0 <= tile.h <= 100.0 + 1e-9
            assert tile.x + tile.w <= 100.0 + 1e-9
            assert tile.y + tile.h <= 100.0 + 1e-9

    def test_empty_input_returns_no_tiles(self):
        assert self._placed_tiles([]) == []

    def test_all_zero_weights_returns_collapsed_tiles(self):
        # Defensive guard: a portfolio whose ``current_weight%``
        # collapsed to zero everywhere (cash-only snapshot, before
        # the treemap is gated upstream) must not divide by zero;
        # the layout collapses to empty rectangles.
        tiles = self._placed_tiles([0.0, 0.0])
        assert all(t.w == 0.0 and t.h == 0.0 for t in tiles)

    def test_largest_input_gets_the_largest_area(self):
        values = [70.0, 20.0, 10.0]
        tiles = self._placed_tiles(values)
        areas = [t.w * t.h for t in tiles]
        # Tile order matches the input order, so areas line up by index.
        assert max(range(3), key=lambda i: areas[i]) == 0
