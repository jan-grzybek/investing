"""Webpage construction, logo URL resolution, anchor helpers,
the allocation chart, and the sticky site header."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


class TestParseSvgAspectRatio:
    """The parser behind the OG card's equal-area logo strip.

    Every cell in that strip is sized from the ratio this returns, so
    a wrong answer letterboxes one brand's wordmark against another's.
    It is a pure function over SVG source, which makes it cheap to pin
    directly rather than through a rendered card."""

    @staticmethod
    def _parse(text):
        from investing.logos import _parse_svg_aspect_ratio

        return _parse_svg_aspect_ratio(text)

    def test_reads_viewbox_width_and_height(self):
        # viewBox is "min-x min-y width height", so the ratio comes
        # from the third and fourth numbers, not the first two.
        assert self._parse('<svg viewBox="0 0 300 100">') == pytest.approx(3.0)
        assert self._parse('<svg viewBox="-10 -20 200 100">') == pytest.approx(2.0)

    def test_viewbox_wins_over_width_and_height(self):
        # viewBox is the canonical sizing source; the attribute pair
        # is only a fallback for documents that omit it.
        svg = '<svg viewBox="0 0 300 100" width="50" height="50">'
        assert self._parse(svg) == pytest.approx(3.0)

    def test_falls_back_to_width_and_height_attributes(self):
        assert self._parse('<svg width="120" height="60">') == pytest.approx(2.0)
        assert self._parse("<svg width='120' height='60'>") == pytest.approx(2.0)
        # Unquoted and unit-suffixed values still yield their numbers.
        assert self._parse('<svg width="120px" height="60px">') == pytest.approx(2.0)

    def test_accepts_decimal_dimensions(self):
        assert self._parse('<svg viewBox="0 0 264.58 132.29">') == pytest.approx(2.0, rel=1e-4)

    def test_short_or_unparseable_viewbox_falls_through(self):
        # A viewBox that can't yield two positive numbers must not
        # swallow the document -- the attribute pair is still there.
        assert self._parse('<svg viewBox="0 0 300" width="80" height="40">') == pytest.approx(2.0)
        assert self._parse('<svg viewBox="a b c d" width="80" height="40">') == pytest.approx(2.0)

    def test_non_positive_dimensions_are_rejected(self):
        assert self._parse('<svg viewBox="0 0 0 100">') is None
        assert self._parse('<svg viewBox="0 0 100 0">') is None
        assert self._parse('<svg width="0" height="10">') is None

    def test_missing_dimensions_return_none(self):
        assert self._parse("<svg>") is None
        assert self._parse('<svg width="100">') is None
        assert self._parse("") is None

    def test_matches_past_a_declaration_or_comment_prologue(self):
        # The parser is regex-based precisely so an XML declaration,
        # DOCTYPE or leading comment needs no special-casing.
        svg = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!-- Created with Inkscape -->\n"
            '<svg viewBox="0 0 400 100">'
        )
        assert self._parse(svg) == pytest.approx(4.0)

    def test_committed_favicon_parses(self):
        # The site's own mark is the one SVG guaranteed to be on disk,
        # so it doubles as a fixture that can't drift out of date.
        from pathlib import Path as _Path

        source = _Path(__file__).resolve().parents[1] / "favicon.svg"
        assert self._parse(source.read_text(encoding="utf-8")) == pytest.approx(1.0)


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
    def test_renders_brand_and_links_to_existing_sections(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(is_current=True))
        w.add_trades([])

        out = w._build_site_header()
        assert '<header class="site-header">' in out
        # The brand lockup is name + section, not the old 30px title.
        assert "Jan Grzybek" in out
        assert "Investment Portfolio" in out
        # Links appear in document order, one per reachable section.
        perf = out.index('href="#performance"')
        hold = out.index('href="#holdings"')
        method = out.index('href="#method"')
        assert perf < hold < method
        # Nav exposes an aria-label so screen readers can identify it.
        assert 'aria-label="Page sections"' in out

    def test_activity_link_only_when_trades_exist(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        assert 'href="#activity"' not in w._build_site_header()

    def test_omits_nav_when_only_one_section_exists(self):
        # A bare Webpage reaches only the unconditional Method link;
        # a single-link nav is visual noise without value.
        w = Webpage()
        out = w._build_site_header()
        assert "Jan Grzybek" in out
        assert "site-nav" not in out


class TestAllocationSectors:
    """``sector_totals`` re-bases equity weights onto the equity
    sleeve, which is what lets the bar be captioned "share of
    equities" honestly. The rest of the allocation module is plain
    string assembly asserted against rendered HTML elsewhere."""

    @staticmethod
    def _totals(rows):
        from investing.webpage.allocation import sector_totals

        return sector_totals(rows)

    def test_weights_are_rebased_onto_the_equity_sleeve(self):
        totals = self._totals(
            [
                {"sector": "Technology", "current_weight%": 30.0},
                {"sector": "Healthcare", "current_weight%": 10.0},
            ]
        )
        assert [name for name, _ in totals] == ["Technology", "Healthcare"]
        assert totals[0][1] == pytest.approx(75.0)
        assert totals[1][1] == pytest.approx(25.0)
        assert sum(pct for _, pct in totals) == pytest.approx(100.0)

    def test_same_sector_accumulates(self):
        totals = self._totals(
            [
                {"sector": "Technology", "current_weight%": 20.0},
                {"sector": "Technology", "current_weight%": 20.0},
            ]
        )
        assert totals == [("Technology", pytest.approx(100.0))]

    def test_blank_sector_folds_into_other(self):
        totals = self._totals([{"sector": "", "current_weight%": 5.0}])
        assert totals[0][0] == "Other"

    def test_ties_break_on_name_so_colours_do_not_reshuffle(self):
        rows = [
            {"sector": "Healthcare", "current_weight%": 10.0},
            {"sector": "Energy", "current_weight%": 10.0},
        ]
        assert [name for name, _ in self._totals(rows)] == ["Energy", "Healthcare"]
        assert [name for name, _ in self._totals(list(reversed(rows)))] == [
            "Energy",
            "Healthcare",
        ]

    def test_missing_or_non_positive_weights_are_skipped(self):
        totals = self._totals(
            [
                {"sector": "Technology", "current_weight%": 10.0},
                {"sector": "Energy", "current_weight%": None},
                {"sector": "Utilities", "current_weight%": 0.0},
            ]
        )
        assert totals == [("Technology", pytest.approx(100.0))]

    def test_no_positive_weights_renders_nothing(self):
        assert self._totals([{"sector": "Technology", "current_weight%": 0.0}]) == []


class TestApportionPct:
    """Largest-remainder rounding for shares of a whole.

    The property that matters: what the reader can add up must equal
    what the portfolio actually is. Rounding each share on its own
    breaks that, and the error is invisible in any single figure.
    """

    def test_the_reported_case_sums_to_a_hundred(self):
        from investing.formatting import apportion_pct

        # 86.04 / 13.83 / 0.13 rounds independently to 86.0 + 13.8 +
        # 0.1 = 99.9%, which is the drift this exists to remove.
        out = apportion_pct([86.04, 13.83, 0.13])
        assert sum(out) == pytest.approx(100.0)
        assert out == [86.1, 13.8, 0.1]

    def test_thirds_sum_to_a_hundred(self):
        from investing.formatting import apportion_pct

        out = apportion_pct([100 / 3] * 3)
        assert sum(out) == pytest.approx(100.0)
        assert out == [33.4, 33.3, 33.3]

    def test_values_already_exact_are_untouched(self):
        from investing.formatting import apportion_pct

        assert apportion_pct([50.0, 50.0]) == [50.0, 50.0]
        assert apportion_pct([100.0]) == [100.0]

    def test_no_value_moves_more_than_one_unit_in_the_last_place(self):
        from investing.formatting import apportion_pct

        raw = [55.44, 28.51, 6.79, 5.26, 4.00]
        out = apportion_pct(raw)
        assert sum(out) == pytest.approx(100.0)
        for original, shown in zip(raw, out, strict=True):
            assert abs(shown - original) < 0.1

    def test_a_partial_total_is_preserved_not_inflated(self):
        """Values that are not a whole must not be scaled up to fill one.

        Only the asset-class and sector bars are true partitions. If a
        caller hands over a subset, the honest answer is to fix the
        rounding *within* their total, not to invent the difference.
        """
        from investing.formatting import apportion_pct

        assert apportion_pct([78.7, 10.7]) == [78.7, 10.7]

    def test_ties_resolve_by_position_so_rebuilds_are_stable(self):
        from investing.formatting import apportion_pct

        # Three identical values, one spare tenth: it must always land
        # on the first, or a no-op deploy shows a diff.
        out = apportion_pct([100 / 3] * 3)
        assert out[0] > out[1]
        assert apportion_pct([100 / 3] * 3) == out

    def test_a_vanishing_share_rounds_to_zero_without_breaking_the_sum(self):
        from investing.formatting import apportion_pct

        out = apportion_pct([99.95, 0.03, 0.02])
        assert sum(out) == pytest.approx(100.0)
        assert out == [100.0, 0.0, 0.0]

    def test_empty_and_all_zero_inputs_are_returned_unchanged(self):
        from investing.formatting import apportion_pct

        assert apportion_pct([]) == []
        assert apportion_pct([0.0, 0.0]) == [0.0, 0.0]

    def test_two_decimal_places_are_supported(self):
        from investing.formatting import apportion_pct

        out = apportion_pct([86.044, 13.833, 0.123], places=2)
        assert sum(out) == pytest.approx(100.0)

    def test_many_random_partitions_all_sum_to_a_hundred(self):
        """The invariant has to hold for shapes nobody hand-picked."""
        import random

        from investing.formatting import apportion_pct

        rng = random.Random(20260829)
        for _ in range(500):
            n = rng.randint(2, 12)
            parts = [rng.random() for _ in range(n)]
            scale = 100.0 / sum(parts)
            out = apportion_pct([p * scale for p in parts])
            assert sum(out) == pytest.approx(100.0), out
