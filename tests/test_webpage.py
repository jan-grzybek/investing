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
        assert w.return_html == ""
        assert w.current == []
        assert w.historical == []
        assert w.allocation_pct is None
        assert w.top_10 is None


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
    def test_return_html_is_populated(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])

        # Head-to-head comparison block instead of standalone capsules.
        assert 'class="returns-compare"' in w.return_html
        assert ">TWR<" in w.return_html
        assert ">CAGR<" in w.return_html
        assert "25.0%" in w.return_html
        assert "12.5%" in w.return_html
        # Benchmark column is labelled with the friendly display name.
        assert "S&amp;P 500" in w.return_html
        # The ticker still appears in the logo URL even when the friendly
        # name is shown, so we can still locate the benchmark logo.
        assert "VUAA" in w.return_html

    def test_works_with_no_benchmarks(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        # JG side still rendered, but no benchmark column or delta line.
        assert ">TWR<" in w.return_html
        assert "returns-compare__delta" not in w.return_html
        assert "VUAA" not in w.return_html

    def test_positive_returns_get_positive_class(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        # 25.0% TWR and 12.5% CAGR are both positive -> green class.
        assert "value--positive" in w.return_html
        assert "value--negative" not in w.return_html

    def test_negative_returns_get_negative_class(self, stub_logo_lookup):
        w = Webpage()
        tr = _total_return()
        tr["twr%"] = -5.0
        tr["cagr%"] = -2.5
        w.add_return(tr, [])
        assert "value--negative" in w.return_html

    def test_twr_note_is_not_in_section_block(self, stub_logo_lookup):
        # The TWR explanation lives in the page footer now, not in the
        # comparison block above it.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert "Time-weighted return" not in w.return_html
        assert "holding__note" not in w.return_html

    def test_period_is_shared_across_jg_and_benchmark(
        self, stub_logo_lookup, freeze_today,
    ):
        # With a single-point history (no chart) the comparison block
        # picks up the "Since {start} · {duration}" header itself, so
        # the period is still printed exactly once for both sides.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert w.return_html.count('"returns-compare__period"') == 1
        # Date is wrapped in a machine-readable <time> element. The
        # human-facing label drops the leading zero on the day
        # ("Jan 1" not "Jan 01") -- that's the page-wide convention
        # set in ``_fmt_date``. The ISO ``datetime`` attribute keeps
        # the zero-pad because that's the W3C machine format.
        assert (
            '<time datetime="2024-01-01">Jan 1, 2024</time>'
            in w.return_html
        )
        # The duration ("1 year, 5 months") sits alongside the start
        # date so the header conveys both anchor and length.
        assert "1 year, 5 months" in w.return_html
        # And the date appears just once, not on each side.
        assert w.return_html.count("Jan 1, 2024") == 1

    def test_period_lives_in_chart_caption_when_chart_present(self, stub_logo_lookup):
        # When the chart is rendered it owns the "Since {start}" header
        # and the comparison block omits its own period to avoid
        # repeating the start date and its length.
        tr = _total_return()
        tr["history"] = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        w = Webpage()
        w.add_return(tr, [_benchmark() | {"history": [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.05),
            (datetime(2024, 12, 1), 1.1),
        ]}])
        # The chart's caption owns the period and wraps the date as a
        # machine-readable <time> element. Day numbers render without
        # a leading zero ("Jan 1") in the human label; the ISO
        # ``datetime`` attribute keeps the zero-pad.
        assert (
            '<time datetime="2024-01-01">Jan 1, 2024</time>'
            in w.return_html
        )
        # Single occurrence of the start date in the entire section.
        assert w.return_html.count("Jan 1, 2024") == 1
        # And no period header on the comparison block.
        assert '"returns-compare__period"' not in w.return_html

    def test_full_names_are_rendered_as_subtitles(self, stub_logo_lookup):
        # JG carries "Jan Grzybek" under it, the benchmark carries the
        # underlying ticker so the full identity is always disclosed.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert "Jan Grzybek" in w.return_html
        assert "LSE:VUAA.L" in w.return_html
        # Both rendered through the dedicated subtitle class.
        assert w.return_html.count("returns-compare__name-sub") == 2

    def test_compare_col_uses_h3_not_h4(self, stub_logo_lookup):
        # Parent <section> is at h2; jumping to h4 in the comparison
        # block would skip a heading level (a WCAG and SEO smell).
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert '<h3 class="returns-compare__name">' in w.return_html
        assert "<h4" not in w.return_html
        assert "</h4>" not in w.return_html

    def test_compare_col_logos_have_image_attrs(self, stub_logo_lookup):
        # Compare-col logos sit in the first viewport so they don't get
        # ``loading="lazy"`` (eager is fine), but they still need
        # async decode + dimensions for stable layout.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert 'class="returns-compare__logo"' in w.return_html
        compare_imgs = [
            line for line in w.return_html.split("<")
            if line.startswith("img") and "returns-compare__logo" in line
        ]
        assert len(compare_imgs) == 2
        for img in compare_imgs:
            assert 'decoding="async"' in img
            assert 'width="48"' in img
            assert 'height="48"' in img

    def test_outperformance_delta_line_uses_correct_signs(self, stub_logo_lookup):
        # JG 25 vs bench 10 = +15 pp Total Return, JG 12.5 vs bench
        # 5 = +7.5 pp CAGR. The delta line spells "Total Return" out
        # in title case -- it sits visually parallel to the ``CAGR``
        # token next to it (both reading as data labels), and the
        # capsule columns above already provide the precise per-side
        # metric ("TWR" for JG, "TSR" for the benchmark) so this
        # summary line just states what's being compared.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert "+15.0 pp Total Return" in w.return_html
        assert "+7.5 pp CAGR" in w.return_html
        # Neither the older "TWR" abbreviation, the short-lived "TR"
        # label, nor the lower-case interim form leak through.
        assert "pp TWR" not in w.return_html
        assert "pp TR<" not in w.return_html
        assert "pp TR " not in w.return_html
        assert "pp total return" not in w.return_html
        # Both deltas are positive -> green class on the spans.
        assert "value--positive" in w.return_html

    def test_outperformance_delta_line_uses_negative_when_underperforming(
        self, stub_logo_lookup,
    ):
        # JG -5 TWR vs bench +10 TSR = -15.0 pp Total Return.
        w = Webpage()
        tr = _total_return()
        tr["twr%"] = -5.0
        tr["cagr%"] = -2.5
        w.add_return(tr, [_benchmark()])
        assert "-15.0 pp Total Return" in w.return_html
        assert "-7.5 pp CAGR" in w.return_html
        assert "value--negative" in w.return_html

    def test_outperformance_delta_pieces_can_wrap_independently(
        self, stub_logo_lookup,
    ):
        # Each piece (prefix, two metrics, separator) is wrapped in
        # its own span so a flex parent can break them across lines
        # under viewport pressure without splitting "+6.7 pp Total
        # Return" mid-phrase. The narrow-viewport CSS hides the dot
        # separator and forces each metric onto its own row.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        out = w.return_html
        assert 'class="returns-compare__delta-prefix">JG vs ' in out
        # Two metric spans (Total Return + CAGR), each with its sign class.
        assert out.count("returns-compare__delta-metric") == 2
        # Separator carries aria-hidden so screen readers don't read
        # an out-of-context middle dot when the narrow layout has
        # already turned it into noise.
        assert (
            'class="returns-compare__delta-sep" aria-hidden="true"'
        ) in out
        # The narrow-viewport stack rule lives in its own breakpoint.
        # We bumped the threshold from 480px to 540px when the label
        # grew from "TR" to "Total Return" so the stack kicks in
        # before the row gets visually cramped.
        full_html = w._head() + out  # styles live in <head>
        assert "@media (max-width: 540px)" in full_html

    def test_chart_renders_above_returns_comparison(self, stub_logo_lookup):
        # Multi-point history triggers the chart; it should appear above
        # the comparison block.
        tr = _total_return()
        tr["history"] = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        w = Webpage()
        w.add_return(tr, [_benchmark() | {"history": [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.05),
            (datetime(2024, 12, 1), 1.1),
        ]}])
        chart_idx = w.return_html.index('class="return-chart"')
        compare_idx = w.return_html.index('class="returns-compare"')
        assert chart_idx < compare_idx


class TestAddHolding:
    def test_current_holding_appears_in_current_bucket(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(is_current=True))

        assert len(w.current) == 1
        assert w.historical == []
        assert "Weight:" in w.current[0]
        assert "10.0%" in w.current[0]

    def test_historical_holding_appears_in_historical_bucket(self, stub_logo_lookup):
        h = _holding(
            is_current=False,
            weight=None,
            periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
        )
        w = Webpage()
        w.add_holding(h)

        assert len(w.historical) == 1
        assert w.current == []
        # No weight rendered for closed positions.
        assert "Weight:" not in w.historical[0]
        # Closed period renders a real end date, not "Present".
        # Day number drops the leading zero per ``_fmt_date``.
        assert "Jan 1, 2024" in w.historical[0]

    def test_cagr_above_sentinel_renders_as_tba(self, stub_logo_lookup):
        # The check uses `math.nextafter(1_000_000, 0)`; anything strictly
        # greater than that triggers the "TBA" branch.
        sentinel_cagr = 1_000_000  # > nextafter(1_000_000, 0)
        assert sentinel_cagr > math.nextafter(1_000_000, 0)

        h = _holding(cagr=sentinel_cagr)
        w = Webpage()
        w.add_holding(h)
        assert "TBA" in w.current[0]

    def test_open_period_renders_present(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(periods=[{"start": datetime(2024, 1, 1), "end": None}]))
        assert "Present" in w.current[0]

    def test_negative_holding_returns_get_negative_class(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(tsr=-14.2, cagr=-27.4))
        assert "value--negative" in w.current[0]

    def test_period_dates_are_wrapped_in_time_elements(self, stub_logo_lookup):
        # Wrapping each rendered date in <time datetime="..."> makes the
        # holding period machine-readable for crawlers and screen readers
        # without altering the human-facing label. The label uses
        # ``%-d`` (no leading zero on the day) while the ISO attribute
        # keeps ``%Y-%m-%d`` zero-padded -- two different conventions
        # serving two different audiences.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:CLO",
            is_current=False,
            weight=None,
            periods=[
                {"start": datetime(2022, 11, 4), "end": datetime(2024, 4, 12)},
            ],
        ))
        card = w.historical[0]
        # Day 4 -> "Nov 4" in the label, but ``2022-11-04`` in the
        # machine-readable ISO attribute.
        assert '<time datetime="2022-11-04">Nov 4, 2022</time>' in card
        # Day 12 has two digits already, so the visible label is
        # unchanged whether we zero-pad or not -- the assertion still
        # exercises the wrapping.
        assert '<time datetime="2024-04-12">Apr 12, 2024</time>' in card

    def test_open_period_only_wraps_the_start_date(self, stub_logo_lookup):
        # "Present" is a label, not a date, so we don't wrap it in
        # a <time> element. It still gets its own <span> so it can
        # participate as a grid item alongside the start <time> and
        # the dash separator -- that 3-column grid is what aligns
        # multi-period stacks vertically.
        w = Webpage()
        w.add_holding(_holding(periods=[
            {"start": datetime(2024, 1, 1), "end": None},
        ]))
        card = w.current[0]
        # Day number 1 renders as "Jan 1" (no leading zero); ISO
        # datetime attribute keeps the zero-pad.
        assert '<time datetime="2024-01-01">Jan 1, 2024</time>' in card
        # "Present" never gets a <time> wrapper.
        assert "<time>Present" not in card
        # The end-of-period section is the dash span followed by the
        # "Present" span -- two separate grid items, no inline " - "
        # separator left over from the old single-row layout.
        assert "<span>-</span><span>Present</span>" in card

    def test_periods_render_as_three_grid_columns(self, stub_logo_lookup):
        # Multi-period cards use a 3-column grid (start, dash, end)
        # so dates and the separator stay aligned vertically across
        # rows even when day numbers have different digit counts
        # ("Jan 22, 2024" vs "Aug 5, 2022"). Each <li> is therefore
        # required to emit exactly three children in that order.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:GRID",
            is_current=False,
            weight=None,
            periods=[
                {"start": datetime(2022, 8, 5), "end": datetime(2023, 6, 9)},
                {"start": datetime(2024, 1, 22), "end": datetime(2024, 11, 30)},
            ],
        ))
        card = w.historical[0]
        # Newest-first (per the defensive sort in _build_card).
        expected_top = (
            '<li>'
            '<time datetime="2024-01-22">Jan 22, 2024</time>'
            '<span>-</span>'
            '<time datetime="2024-11-30">Nov 30, 2024</time>'
            '</li>'
        )
        expected_bottom = (
            '<li>'
            '<time datetime="2022-08-05">Aug 5, 2022</time>'
            '<span>-</span>'
            '<time datetime="2023-06-09">Jun 9, 2023</time>'
            '</li>'
        )
        assert expected_top in card
        assert expected_bottom in card
        # And the parent <ul> drives the 3-column grid layout from
        # CSS in <head>; the <li>s themselves use display: contents
        # so their three children land directly in those tracks.
        # The track widths are FIXED (in em units) rather than
        # ``max-content`` so every holding card on the page uses the
        # same column widths -- that's what makes the dash and end
        # date column line up across capsules, not just within a
        # single card's multi-period stack.
        full_html = w._head() + card
        assert "grid-template-columns: 7em min-content 7em" in full_html
        assert ".holding__periods li { display: contents; }" in full_html
        # ``justify-content: start`` is essential here: without it,
        # CSS Grid's "Expand Stretched auto Tracks" step would
        # distribute the leftover horizontal space inside the <ul>
        # to any ``auto``-sized track, pushing the end-date column
        # far to the right on wide viewports. ``min-content`` for
        # the middle track is the second guard against that step.
        assert "justify-content: start" in full_html
        # The start-date <time> is right-aligned inside its 7em
        # cell so the date hugs the dash, giving symmetric ~0.5ch
        # gaps on each side of the separator. Without this rule
        # the trailing whitespace inside a left-aligned start cell
        # (~1-2em) plus the column-gap on the dash's far side make
        # the gap before the dash visibly larger than the gap after
        # it -- which reads as broken visual rhythm.
        assert ".holding__periods li > :first-child { text-align: end; }" in full_html
        # Sanity guards: the old self-sizing variant (per-card
        # alignment, no cross-card alignment) and the buggy
        # auto-middle-track variant (cross-card alignment but a
        # huge gap before the end date) must not regress.
        assert "max-content max-content max-content" not in full_html
        assert "7em auto 7em" not in full_html

    def test_multiple_periods_stack_newest_first_as_list(
        self, stub_logo_lookup,
    ):
        # The visual order (newest period on top) is a UX guarantee
        # that ``_build_card`` enforces internally via ``sorted(...,
        # reverse=True)`` -- regardless of the order the caller hands
        # the periods over in. Pass them in *oldest-first* on purpose
        # to prove the render is order-agnostic.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:MULTI",
            is_current=False,
            weight=None,
            periods=[
                {"start": datetime(2022, 1, 5), "end": datetime(2023, 3, 9)},
                {"start": datetime(2024, 6, 1), "end": datetime(2025, 2, 1)},
            ],
        ))
        card = w.historical[0]
        assert '<ul class="holding__periods">' in card
        # Two list items, no inline bullet separator left behind.
        assert card.count("<li>") == 2
        assert "::before" not in card
        # Even though we passed them oldest-first, the newest period's
        # start date appears earlier in the rendered HTML -- meaning
        # it occupies the first <li> and renders at the top of the
        # visual stack.
        newest = '<time datetime="2024-06-01">'
        oldest = '<time datetime="2022-01-05">'
        assert newest in card and oldest in card
        assert card.index(newest) < card.index(oldest)

    def test_open_period_sorts_to_top_among_multiple(
        self, stub_logo_lookup,
    ):
        # An open position (end is None) is by definition the most
        # recent ownership window, so it must land at the top of the
        # stack even when older closed periods sit alongside it.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:OPEN",
            periods=[
                {"start": datetime(2020, 5, 1), "end": datetime(2021, 8, 1)},
                {"start": datetime(2024, 9, 1), "end": None},
            ],
        ))
        card = w.current[0]
        open_marker = '<time datetime="2024-09-01">'
        closed_marker = '<time datetime="2020-05-01">'
        assert card.index(open_marker) < card.index(closed_marker)
        # And the open period renders the "Present" label, not a date.
        # The dash + "Present" each sit in their own <span> so the
        # 3-column grid layout can align them with sibling periods.
        assert "<span>-</span><span>Present</span>" in card

    def test_holding_logo_has_lazy_loading_and_dimensions(self, stub_logo_lookup):
        # ``loading="lazy"`` defers below-the-fold loads, ``decoding=
        # "async"`` keeps decode off the main thread, and explicit
        # ``width``/``height`` attributes give the browser the aspect
        # ratio so layout space is reserved before the image arrives
        # (zero CLS).
        w = Webpage()
        w.add_holding(_holding())
        card = w.current[0]
        assert 'class="holding__logo"' in card
        assert 'loading="lazy"' in card
        assert 'decoding="async"' in card
        assert 'width="64"' in card
        assert 'height="64"' in card


class TestTicker:
    def test_returns_empty_string_when_no_current_holdings(self, stub_logo_lookup):
        w = Webpage()
        # Only a closed/historical position.
        w.add_holding(_holding(
            ticker="NMS:OLD",
            is_current=False,
            weight=None,
            periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
        ))
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
        # async decode + explicit dimensions keep layout stable while
        # the bitmaps stream in. The width/height match the largest
        # rendered size (desktop) so the browser reserves enough
        # space; CSS scales them down on narrow viewports.
        assert 'decoding="async"' in out
        assert 'width="48"' in out
        assert 'height="48"' in out
        # Lazy loading would be wrong here: the marquee animates from
        # the moment the page paints, off-screen logos must already be
        # decoded.
        assert 'loading="lazy"' not in out

    def test_excludes_historical_holdings(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:LIVE", is_current=True))
        w.add_holding(_holding(
            ticker="NMS:DEAD",
            is_current=False,
            weight=None,
            periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
        ))
        out = w._build_ticker()
        assert "NMS:LIVE" in out
        assert "NMS:DEAD" not in out

    def test_logo_lookups_are_cached(self, monkeypatch):
        # Adding the same ticker twice (e.g. both a current and a past
        # position for the same instrument under different test setups)
        # should only HEAD-probe its logo extensions once.
        calls = []

        def fake_head(url):
            calls.append(url)
            resp = MagicMock()
            resp.status_code = 200  # First extension wins immediately.
            return resp

        monkeypatch.setattr(update.requests, "head", fake_head)

        w = Webpage()
        w._get_logo_url("NMS:AAA")
        w._get_logo_url("NMS:AAA")
        w._get_logo_url("NMS:AAA")
        # Single probe even though we asked for the URL three times.
        assert len(calls) == 1


class TestRenderBars:
    def test_returns_empty_string_when_no_rows(self):
        assert Webpage._render_bars([], "allocation") == ""
        assert Webpage._render_bars(None, "allocation") == ""

    def test_renders_one_row_per_entry_with_widths(self):
        out = Webpage._render_bars(
            [("Equities", 95.4), ("Cash & Cash Equivalents", 4.6)],
            "allocation",
        )
        assert 'class="bars bars--allocation"' in out
        assert "Equities" in out
        # Special characters in labels get HTML-escaped.
        assert "Cash &amp; Cash Equivalents" in out
        # In allocation mode bar widths match the raw percentages.
        assert "width: 95.4%" in out
        assert "width: 4.6%" in out
        assert out.count('class="bars__row"') == 2

    def test_value_is_emitted_between_label_and_bar(self):
        out = Webpage._render_bars([("Equities", 95.4)], "allocation")
        # Title -> percentage -> bar (so percentages sit between the
        # title and the visual bar).
        label_idx = out.index('bars__label')
        value_idx = out.index('bars__value')
        track_idx = out.index('bars__track')
        assert label_idx < value_idx < track_idx

    def test_variant_class_is_applied(self):
        out = Webpage._render_bars([("X", 1.0)], "equities")
        assert "bars--equities" in out
        assert "bars--allocation" not in out

    def test_preserves_input_order(self):
        out = Webpage._render_bars(
            [("Z", 1.0), ("A", 2.0), ("M", 3.0)], "equities"
        )
        # Labels appear in the input order, not sorted.
        assert out.index("Z") < out.index("A") < out.index("M")

    def test_scale_to_max_makes_largest_value_fill_the_bar(self):
        out = Webpage._render_bars(
            [("AAA", 50.0), ("BBB", 25.0), ("CCC", 10.0)],
            "equities",
            scale_to_max=True,
        )
        # Largest holding fills its track entirely.
        assert "width: 100.0%" in out
        # 25 / 50 = 50; 10 / 50 = 20.
        assert "width: 50.0%" in out
        assert "width: 20.0%" in out
        # Displayed percentages are still the raw values (not the scaled ones).
        assert ">50.0%</div>" in out
        assert ">25.0%</div>" in out
        assert ">10.0%</div>" in out

    def test_scale_to_max_with_zero_values_does_not_crash(self):
        # All-zero values fall back to the 100% denominator so the bars
        # render as empty rather than dividing by zero.
        out = Webpage._render_bars(
            [("AAA", 0.0), ("BBB", 0.0)], "equities", scale_to_max=True,
        )
        assert "width: 0.0%" in out


class TestRenderReturnChart:
    def test_returns_empty_when_history_too_short(self):
        out = Webpage._render_return_chart(
            {"history": [(datetime(2024, 1, 1), 1.0)]}, []
        )
        assert out == ""

    def test_renders_jg_line_and_reference_line(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        out = Webpage._render_return_chart({"history": history}, [])
        assert 'class="return-chart"' in out
        assert "return-chart__line--jg" in out
        assert "return-chart__ref" in out
        # Without a benchmark there is no second line and no delta overlay.
        assert "return-chart__line--bench" not in out
        assert "return-chart__delta" not in out
        # The svg has a viewBox and no fixed pixel dimensions.
        assert "viewBox=" in out
        assert "<svg " in out and 'width="' not in out.split("<svg ", 1)[1].split(">", 1)[0]

    def test_renders_benchmark_line_and_legend_when_provided(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
        ]
        benchmark = {"ticker": "LSE:VUAA.L",
                     "history": [(datetime(2024, 1, 1), 1.0),
                                 (datetime(2024, 6, 1), 1.05)]}
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        assert "return-chart__line--bench" in out
        assert "S&amp;P 500" in out

    def test_renders_outperformance_overlay_with_benchmark(self):
        # JG ends at 1.20 (+20%), bench ends at 1.05 (+5%) -> +15 pp.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        benchmark = {"ticker": "LSE:VUAA.L",
                     "history": [(datetime(2024, 1, 1), 1.0),
                                 (datetime(2024, 6, 1), 1.02),
                                 (datetime(2024, 12, 1), 1.05)]}
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        # The delta overlay sits inside its own positioning wrapper and
        # exposes bar+label as separate elements so CSS can keep the
        # bar pinned to the chart-end x-coordinate at every viewport.
        assert 'class="return-chart__plot"' in out
        assert 'class="return-chart__delta"' in out
        assert 'class="return-chart__delta-bar"' in out
        assert "+15.0 pp" in out
        # Positive delta -> green class on the label.
        assert "return-chart__delta-label value--positive" in out
        # The overlay communicates positions via CSS custom properties
        # so the bar/label can be styled independently of each other.
        delta = out.split('class="return-chart__delta"', 1)[1].split("</div>", 1)[0]
        assert "--top:" in delta
        assert "--height:" in delta

    def test_outperformance_overlay_uses_negative_class_when_underperforming(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 0.95),
            (datetime(2024, 12, 1), 0.92),
        ]
        benchmark = {"ticker": "LSE:VUAA.L",
                     "history": [(datetime(2024, 1, 1), 1.0),
                                 (datetime(2024, 6, 1), 1.02),
                                 (datetime(2024, 12, 1), 1.05)]}
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        assert "-13.0 pp" in out
        assert "return-chart__delta-label value--negative" in out

    def test_caption_uses_since_start_date_with_duration(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 5, 1), 1.2),
        ]
        out = Webpage._render_return_chart({"history": history}, [])
        # Caption anchors the period via the start date and follows it
        # with the elapsed window so the reader gets both at a glance.
        caption = out.split('return-chart__caption', 1)[1].split("</div>", 1)[0]
        # Date is wrapped in a machine-readable <time> element. The
        # human label drops the leading zero on the day number while
        # the ISO ``datetime`` attribute keeps it zero-padded.
        assert (
            '<time datetime="2024-01-01">Jan 1, 2024</time>'
            in caption
        )
        assert "4 months" in caption
        # The old "range X-Yx" caption format is gone.
        assert "range" not in caption


class TestAddAllocations:
    def test_stores_values_for_save(self):
        w = Webpage()
        w.add_allocations({"Equities": 95.4}, {"NMS:AAA": 50.0})
        assert w.allocation_pct == {"Equities": 95.4}
        assert w.top_10 == {"NMS:AAA": 50.0}


class TestSave:
    def test_writes_index_html_with_key_sections(
        self, stub_logo_lookup, chdir_tmp, freeze_today
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        w.add_allocations(
            {"Equities": 95.4, "Cash & Cash Equivalents": 4.6},
            {"NMS:CURR": 100.0},
        )
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
        assert '<html lang="en">' in out
        # The descriptive title is what renders on SERPs/tabs.
        assert "<title>Jan Grzybek - Investment Portfolio</title>" in out
        # Mobile readiness: viewport + theme-color metas, and at least one
        # narrow-width media query in the embedded stylesheet.
        assert 'name="viewport"' in out
        assert 'width=device-width' in out
        assert 'name="theme-color"' in out
        assert "@media (max-width: 540px)" in out
        # Page header with title + in-page nav anchored to each section.
        assert '<header class="site-header">' in out
        assert "Jan Grzybek Investment Portfolio" in out
        assert '<nav class="site-nav"' in out
        assert 'href="#performance"' in out
        assert 'href="#current"' in out
        assert 'href="#historical"' in out
        # Sections expose anchor IDs the nav links target.
        assert 'id="performance"' in out
        assert 'id="current"' in out
        assert 'id="historical"' in out
        assert "All-time performance" in out
        assert "Current holdings" in out
        assert "Historical holdings" in out
        # Single semantic structure (no desktop/mobile duplication).
        # <main> now carries an id so the skip link can target it
        # and a tabindex so screen readers can move focus there.
        assert '<main id="main-content"' in out
        assert "</main>" in out
        assert "<footer" in out
        assert 'class="holding"' in out
        # Skip link is the first interactive element in <body>, ahead
        # of the sticky header.
        assert 'class="skip-link" href="#main-content"' in out
        body_idx = out.index('<body>')
        skip_idx = out.index('class="skip-link"')
        header_idx = out.index('class="site-header"')
        assert body_idx < skip_idx < header_idx
        # Marquee ticker is rendered at the top of <main>.
        assert 'class="ticker"' in out
        ticker_idx = out.index('class="ticker"')
        main_idx = out.index('<main id="main-content"')
        performance_idx = out.index('id="performance"')
        assert main_idx < ticker_idx < performance_idx
        # Each current holding ticker now also appears in the marquee
        # (two copies for the seamless loop) plus the bars + card.
        assert out.count("NMS:CURR") == 4
        assert out.count("NMS:OLD") == 1  # historical -> not in ticker
        # Allocation bar charts rendered.
        assert "bars--allocation" in out
        assert "bars--equities" in out
        # Dark mode and responsive media queries are present.
        assert "prefers-color-scheme: dark" in out
        assert "@media print" in out
        # Methodology bullets in the footer cover the base currency
        # and the portfolio-level TWR scope.
        assert 'class="footer__notes"' in out
        assert "USD as the base currency" in out
        assert "portfolio-level time-weighted return (TWR)" in out
        # The frozen date appears in the footer, wrapped in a
        # machine-readable <time> element.
        assert '<time datetime="2025-06-01">Jun 1, 2025</time>' in out

    def test_save_emits_seo_metadata_in_head(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # All the moving pieces search engines and social platforms
        # look for: descriptive title, canonical URL, robots opt-in,
        # author, full Open Graph + Twitter Card sets, and a JSON-LD
        # WebSite graph identifying the author.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert "<title>Jan Grzybek - Investment Portfolio</title>" in out
        assert 'name="description"' in out
        assert 'name="author" content="Jan Grzybek"' in out
        # ``index,follow`` plus large image previews to invite rich SERP
        # treatment.
        assert 'name="robots"' in out
        assert "index,follow" in out
        assert "max-image-preview:large" in out
        # Canonical URL prevents duplicate-content dilution.
        assert ('rel="canonical" href="https://jan-grzybek.github.io/investing/"') in out
        # Open Graph: title, description, image, url, type, locale, site_name.
        for prop in (
            "og:title", "og:description", "og:image", "og:url",
            "og:type", "og:locale", "og:site_name",
        ):
            assert f'property="{prop}"' in out
        # Twitter Card variants for X/Twitter previews. Now using
        # ``summary_large_image`` since we ship a 1200x630 OG image.
        for tw in ("twitter:card", "twitter:title", "twitter:description",
                   "twitter:image", "twitter:image:alt"):
            assert f'name="{tw}"' in out
        assert 'name="twitter:card" content="summary_large_image"' in out
        # OG image dimensions are advertised so platforms can reserve
        # preview space without a HEAD probe.
        assert 'property="og:image:type" content="image/png"' in out
        assert 'property="og:image:width" content="1200"' in out
        assert 'property="og:image:height" content="630"' in out
        assert 'property="og:image:alt"' in out
        # OG image points at the dynamically-generated PNG, not the
        # static apple-touch icon.
        assert 'content="https://jan-grzybek.github.io/investing/og-image.png"' in out
        # JSON-LD structured data identifies the site + its author.
        assert 'type="application/ld+json"' in out
        assert '"@type": "WebSite"' in out or '"@type":"WebSite"' in out
        assert '"@type": "Person"' in out or '"@type":"Person"' in out
        assert "Jan Grzybek" in out

    def test_save_emits_security_headers_via_meta(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # GitHub Pages can't set HTTP headers, so the page sets the
        # equivalents via <meta>. The CSP allowlists exactly what the
        # page actually loads (Cloudflare beacon, inline JSON-LD by
        # hash, inline <style> by hash) and locks everything else down.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        # Referrer-Policy and CSP meta tags both present.
        assert 'name="referrer" content="strict-origin-when-cross-origin"' in out
        assert 'http-equiv="Content-Security-Policy"' in out
        # Sanity-check the CSP shape: default deny-ish + script source
        # for the Cloudflare beacon + hash-pinned inline payloads.
        assert "default-src 'self'" in out
        assert "https://static.cloudflareinsights.com" in out
        assert "frame-ancestors 'none'" in out
        # Both the inline JSON-LD and the inline <style> are still
        # hash-pinned (XSS-relevant payloads stay locked).
        assert "'sha256-" in out
        # Inline ``style="..."`` attributes are needed for
        # programmatically-generated values (bar widths, delta
        # positions, legend swatch colours), so the CSP3 split lets
        # those through via ``style-src-attr`` while keeping the
        # <style> block hash-pinned via ``style-src-elem``. Crucially,
        # ``script-src`` must NOT carry ``'unsafe-inline'`` -- that's
        # where actual code execution lives.
        assert "style-src-elem" in out
        assert "style-src-attr 'unsafe-inline'" in out
        script_src = out.split("script-src", 1)[1].split(";", 1)[0]
        assert "unsafe-inline" not in script_src

    def test_save_writes_og_image_png(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The OG image is regenerated on every save with the current
        # numbers baked in. We don't assert its pixels - just that a
        # well-formed PNG of the documented dimensions lands on disk.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        tr = _total_return()
        tr["history"] = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2025, 5, 1), 1.4),
        ]
        bench = _benchmark()
        bench["history"] = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.05),
            (datetime(2025, 5, 1), 1.2),
        ]
        w.add_return(tr, [bench])
        w.save()

        og_path = chdir_tmp / "og-image.png"
        assert og_path.exists()
        # PNG magic header.
        assert og_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        # Verify the actual image dimensions match what <head> claims.
        from PIL import Image
        with Image.open(og_path) as img:
            assert img.size == (1200, 630)

    def test_save_writes_sitemap_xml(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # Search engines use ``<lastmod>`` as a hint to recrawl, so we
        # regenerate the sitemap on every ``save()`` with the current
        # date stamped in.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.save()

        sitemap = (chdir_tmp / "sitemap.xml").read_text()
        assert sitemap.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' in sitemap
        assert "<loc>https://jan-grzybek.github.io/investing/</loc>" in sitemap
        # Lastmod uses the frozen "today".
        assert "<lastmod>2025-06-01</lastmod>" in sitemap
        assert "<changefreq>daily</changefreq>" in sitemap

    def test_save_writes_robots_txt(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # ``robots.txt`` is a build artifact like ``sitemap.xml`` /
        # ``og-image.png``: generating it at runtime keeps the canonical
        # URL and sitemap pointer in lockstep with ``Webpage.SITE_URL``
        # so a future move to a different domain only needs one edit.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.save()

        robots = (chdir_tmp / "robots.txt").read_text()
        # Permissive crawler policy.
        assert "User-agent: *" in robots
        assert "Allow: /" in robots
        # Sitemap pointer derived from ``SITE_URL`` (no trailing
        # double-slash even though SITE_URL ends with one).
        assert "Sitemap: https://jan-grzybek.github.io/investing/sitemap.xml" in robots
        assert "//sitemap.xml" not in robots

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
        # Nav drops the "Current" link and the corresponding anchor when
        # there are no current holdings to point at.
        assert 'href="#current"' not in out
        assert 'id="current"' not in out
        assert 'href="#historical"' in out
        assert 'href="#performance"' in out


class TestBuildSiteHeader:
    def test_renders_title_and_links_to_existing_sections(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(is_current=True))
        w.add_holding(
            _holding(
                ticker="NMS:OLD", is_current=False, weight=None,
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
