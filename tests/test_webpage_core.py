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


class TestLogoCacheMaintenanceHints:
    """The renderer falls back to ``COURAGE_LOGO`` whenever a ticker's
    logo can't be resolved through any of the configured probes. Each
    such fallback should also record a maintenance hint so the
    curated build summary can prompt the maintainer to add the
    missing file under ``logos/``. These tests pin that contract at
    the cache boundary so the wiring stays intact across refactors
    of either the cache or the hint module.
    """

    def test_fallback_to_courage_records_hint(self):
        from investing.sector_overrides import consume_hints

        session, _ = _make_session_stub(ok_extensions=())
        cache = LogoCache(session=session, local_dir=None)
        url = cache("NMS:NOLOGO")
        assert url == COURAGE_LOGO
        hints = consume_hints()
        assert hints.missing_logos == ["NMS:NOLOGO"]

    def test_successful_lookup_records_no_hint(self):
        # A successful HEAD probe means a hand-curated logo IS on
        # file (just not in the local mirror this cache instance
        # checks); no maintenance action needed.
        from investing.sector_overrides import consume_hints

        session, _ = _make_session_stub(ok_extensions=(".svg",))
        cache = LogoCache(session=session, local_dir=None)
        cache("NMS:HASLOGO")
        assert consume_hints().is_empty

    def test_repeat_lookups_record_hint_once(self):
        # The cache returns ``COURAGE_LOGO`` on the second call
        # without re-probing the network; the hint registry should
        # likewise stay at a single entry per ticker (it's set-based
        # so a duplicate ``record`` would be absorbed silently
        # anyway, but the cache short-circuit means the second call
        # never even reaches the recorder).
        from investing.sector_overrides import consume_hints

        session, _ = _make_session_stub(ok_extensions=())
        cache = LogoCache(session=session, local_dir=None)
        cache("NMS:NOLOGO")
        cache("NMS:NOLOGO")
        assert consume_hints().missing_logos == ["NMS:NOLOGO"]


class TestLogoCoverageRatio:
    """The treemap's equal-VISUAL-area sizing pass leans on
    :meth:`LogoCache.coverage_ratio` -- the rasterised fraction of a
    logo's bounding box that survives the SVG knockout filter. These
    tests pin the contract at the cache boundary: missing files /
    rasterisation failures fall back to the constant default, hits
    are cached per ticker, and the measurement itself yields
    plausible 0..1 numbers on synthetic inputs whose densities are
    known by construction.
    """

    @staticmethod
    def _write_svg(tmp_path, ticker, body):
        path = tmp_path / f"{ticker}.svg"
        path.write_text(body, encoding="utf-8")
        return path

    def test_missing_file_returns_default_density(self, tmp_path):
        from investing.logos import _DEFAULT_LOGO_DENSITY

        cache = LogoCache(local_dir=str(tmp_path))
        assert cache.coverage_ratio("NMS:NOTHERE") == _DEFAULT_LOGO_DENSITY

    def test_no_local_dir_returns_default_density(self):
        from investing.logos import _DEFAULT_LOGO_DENSITY

        cache = LogoCache(local_dir=None)
        assert cache.coverage_ratio("NMS:ANY") == _DEFAULT_LOGO_DENSITY

    def test_solid_black_square_yields_full_density(self, tmp_path):
        # 100% opaque non-white pixels -> the knockout filter keeps
        # the whole bounding box; density should round to ~1.0.
        self._write_svg(
            tmp_path,
            "NMS:SOLID",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="10" height="10" fill="#000000"/>'
            "</svg>",
        )
        cache = LogoCache(local_dir=str(tmp_path))
        density = cache.coverage_ratio("NMS:SOLID")
        assert density > 0.95

    def test_pure_white_fill_collapses_to_zero_density(self, tmp_path):
        # The whole bbox is opaque-near-white; the knockout filter
        # would erase the whole logo, so the measured density
        # collapses to ~0.
        self._write_svg(
            tmp_path,
            "NMS:WHITE",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="10" height="10" fill="#ffffff"/>'
            "</svg>",
        )
        cache = LogoCache(local_dir=str(tmp_path))
        density = cache.coverage_ratio("NMS:WHITE")
        assert density < 0.05

    def test_half_covered_yields_intermediate_density(self, tmp_path):
        # A 5x10 black strip on a 10x10 transparent canvas covers
        # exactly half the bbox; the rasterised density should land
        # near 0.5 modulo edge anti-aliasing.
        self._write_svg(
            tmp_path,
            "NMS:HALF",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="5" height="10" fill="#000000"/>'
            "</svg>",
        )
        cache = LogoCache(local_dir=str(tmp_path))
        density = cache.coverage_ratio("NMS:HALF")
        assert 0.45 < density < 0.55

    def test_results_are_cached_per_ticker(self, tmp_path):
        # The cache must not re-rasterise the SVG on repeat lookups.
        # We assert that by patching ``_measure_svg_density`` to a
        # counter and checking it ran exactly once.
        from investing import logos as logos_mod

        self._write_svg(
            tmp_path,
            "NMS:CACHED",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="10" height="10" fill="#000000"/>'
            "</svg>",
        )
        cache = LogoCache(local_dir=str(tmp_path))
        calls = {"n": 0}
        original = logos_mod._measure_svg_density

        def counting(path):
            calls["n"] += 1
            return original(path)

        try:
            logos_mod._measure_svg_density = counting  # type: ignore[assignment]
            first = cache.coverage_ratio("NMS:CACHED")
            second = cache.coverage_ratio("NMS:CACHED")
        finally:
            logos_mod._measure_svg_density = original  # type: ignore[assignment]
        assert first == second
        assert calls["n"] == 1


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

    def test_tile_empty_probe_catches_thin_strips(self):
        from investing.webpage.sector_treemap import _Tile, _tile_must_fold_into_other

        # Wide enough in canvas-% terms but too short on both reference
        # canvases (classic squarify strip).
        assert _tile_must_fold_into_other(_Tile(0.0, 0.0, 10.0, 8.0))
        assert not _tile_must_fold_into_other(_Tile(0.0, 0.0, 25.0, 20.0))

    def test_tile_empty_probe_checks_mobile_and_desktop_references(self):
        from investing.webpage.sector_treemap import (
            _DESKTOP_REF_CANVAS_H_PX,
            _DESKTOP_REF_CANVAS_W_PX,
            _MOBILE_REF_CANVAS_H_PX,
            _MOBILE_REF_CANVAS_W_PX,
            _Tile,
            _tile_must_fold_into_other,
            _tile_would_be_empty_on_canvas,
        )

        # Too short on the mobile reference but tall enough for a ticker
        # on the desktop reference -- stays an individual tile so desktop
        # can show the logo / ticker while mobile falls back to a swatch.
        narrow = _Tile(0.0, 0.0, 17.0, 14.0)
        assert _tile_would_be_empty_on_canvas(
            narrow, _MOBILE_REF_CANVAS_W_PX, _MOBILE_REF_CANVAS_H_PX
        )
        assert not _tile_would_be_empty_on_canvas(
            narrow, _DESKTOP_REF_CANVAS_W_PX, _DESKTOP_REF_CANVAS_H_PX
        )
        assert not _tile_must_fold_into_other(narrow)

    def test_merge_keeps_desktop_legible_tail_without_cascade(self):
        from investing.webpage.sector_treemap import _merge_small_into_other, _Row

        rows = [
            _Row(ticker="NMS:HVY", name="Heavy", sector="Technology", weight=21.4, logo_url="x"),
            _Row(
                ticker="NMS:GOOGL",
                name="Alpha",
                sector="Communication Services",
                weight=13.7,
                logo_url="x",
            ),
            _Row(
                ticker="NMS:META",
                name="Meta",
                sector="Communication Services",
                weight=11.5,
                logo_url="x",
            ),
            _Row(ticker="NMS:ADBE", name="Adobe", sector="Technology", weight=9.1, logo_url="x"),
            _Row(ticker="NMS:AMAT", name="Amat", sector="Technology", weight=7.9, logo_url="x"),
            _Row(ticker="NMS:LRCX", name="Lam", sector="Technology", weight=6.4, logo_url="x"),
            _Row(
                ticker="NMS:SPGI",
                name="SPGI",
                sector="Financial Services",
                weight=6.0,
                logo_url="x",
            ),
            _Row(ticker="NMS:UNH", name="UNH", sector="Healthcare", weight=4.7, logo_url="x"),
            _Row(ticker="NMS:CRM", name="CRM", sector="Technology", weight=4.1, logo_url="x"),
            _Row(ticker="NMS:SAP", name="SAP", sector="Other", weight=3.5, logo_url="x"),
        ]
        merged = _merge_small_into_other(rows)
        # SAP's strip is colour-only on mobile but legible on desktop;
        # the loop must not fold it (or cascade into Other).
        assert len(merged) == len(rows)
        assert not any(row.is_aggregated for row in merged)


class TestEqualVisualAreaLogoFactors:
    """The treemap's logo sizing pass combines aspect-ratio
    normalisation with ink-density normalisation. Both halves are
    pure math on per-logo scalars, so they're exercised directly at
    the helper-function level here -- the end-to-end CSS plumbing is
    asserted separately in
    :class:`tests.test_webpage_treemap.TestEquitySectorTreemap` against
    rendered HTML."""

    @staticmethod
    def _factors(aspect, density):
        from investing.webpage.sector_treemap import _equal_area_factors

        return _equal_area_factors(aspect, density)

    def test_reference_density_lands_on_min_clamp_when_min_above_one(self):
        from investing.webpage.sector_treemap import (
            _LOGO_DENSITY_MIN_SCALE,
            _LOGO_REFERENCE_ASPECT,
            _LOGO_REFERENCE_DENSITY,
        )

        # At the reference aspect and reference density the raw
        # density scale collapses to ``sqrt(D_ref / D_ref) = 1.0``.
        # When the MIN clamp is configured above 1.0 (the current
        # "combination of overall size and density" stance: dense
        # logos don't shrink, they grow by a uniform floor) the
        # neutral-input case lands on that floor in both width and
        # height -- the "no density data" callsites take a
        # different code path through ``_default_logo_coverage_for``
        # and are exercised separately below.
        w, h = self._factors(_LOGO_REFERENCE_ASPECT, _LOGO_REFERENCE_DENSITY)
        expected = max(1.0, _LOGO_DENSITY_MIN_SCALE)
        assert abs(w - expected) < 1e-9
        assert abs(h - expected) < 1e-9

    def test_aspect_only_path_runs_when_density_is_missing(self):
        # Zero / non-finite densities are the "no measurement
        # available" sentinel; the density pass is skipped entirely
        # and the bbox area falls back to the pre-density-correction
        # equal-area invariant w_aspect * h_aspect = 1.
        for sentinel in (0.0, -1.0, float("inf"), float("nan")):
            w, h = self._factors(5.0, sentinel)
            assert abs(w * h - 1.0) < 1e-9, (
                f"density={sentinel}: expected aspect-only product=1, got {w * h}"
            )
            assert w > 1.0 > h  # wide logo -> wider-than-base, shorter-than-base.

    def test_low_density_logo_grows_within_max_clamp(self):
        from investing.webpage.sector_treemap import (
            _LOGO_DENSITY_MAX_SCALE,
            _LOGO_REFERENCE_ASPECT,
        )

        # A density well below the reference would naively grow the
        # bbox by sqrt(D_ref / D); the max clamp caps the growth so
        # very sparse logos can't blow up to dominate their tile.
        w, h = self._factors(_LOGO_REFERENCE_ASPECT, 0.02)
        assert abs(w - _LOGO_DENSITY_MAX_SCALE) < 1e-9
        assert abs(h - _LOGO_DENSITY_MAX_SCALE) < 1e-9

    def test_high_density_logo_shrinks_within_min_clamp(self):
        from investing.webpage.sector_treemap import (
            _LOGO_DENSITY_MIN_SCALE,
            _LOGO_REFERENCE_ASPECT,
        )

        # The symmetric case: a very dense icon would otherwise
        # shrink past the legible-on-mobile floor; the min clamp
        # keeps it visible.
        w, h = self._factors(_LOGO_REFERENCE_ASPECT, 0.9)
        assert abs(w - _LOGO_DENSITY_MIN_SCALE) < 1e-9
        assert abs(h - _LOGO_DENSITY_MIN_SCALE) < 1e-9

    def test_degenerate_aspect_returns_unit_factors(self):
        # A zero or non-finite aspect can't be normalised; the
        # consumer treats (1, 1) as "use the CSS base size".
        for bad in (0.0, -1.0, float("inf"), float("nan")):
            assert self._factors(bad, 0.1) == (1.0, 1.0)
