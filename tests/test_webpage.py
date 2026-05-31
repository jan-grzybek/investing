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
        # "Since X" caption reads as prose ("Since Jan 1, 2024 . 1
        # year, 5 months"), so this one specific spot uses the
        # long-form ``%b %-d, %Y`` format from ``_fmt_date_long``
        # rather than the page-wide DD/MM/YYYY convention -- the
        # slashes would break the sentence rhythm. The ISO
        # ``datetime`` attribute stays in W3C YYYY-MM-DD form.
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
        # The chart's caption owns the period and wraps the date
        # as a machine-readable <time> element. This caption reads
        # as prose ("Since Jan 1, 2024 . X months"), so it carries
        # the long-form ``%b %-d, %Y`` label from
        # ``_fmt_date_long`` -- the slash-separated DD/MM/YYYY
        # format used everywhere else on the page would break the
        # sentence rhythm. ISO ``datetime`` attribute stays in
        # W3C YYYY-MM-DD.
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

    def test_intro_paragraph_precedes_chart_and_comparison(
        self, stub_logo_lookup,
    ):
        # A one-liner sits at the top of the section so a first-time
        # reader knows what the chart + capsules below are showing
        # before they look at the numbers. With a benchmark configured
        # the intro names it explicitly; deeper acronym definitions
        # live in the footer "Methodology" block so the orientation
        # text stays scannable.
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
        assert 'class="section__intro"' in w.return_html
        intro_idx = w.return_html.index('class="section__intro"')
        chart_idx = w.return_html.index('class="return-chart"')
        compare_idx = w.return_html.index('class="returns-compare"')
        assert intro_idx < chart_idx < compare_idx
        # Benchmark name (escaped) is woven into the prose.
        assert (
            "Cumulative return of the portfolio tracked against the "
            "S&amp;P 500."
            in w.return_html
        )

    def test_intro_paragraph_omits_benchmark_when_none_configured(
        self, stub_logo_lookup,
    ):
        # No benchmark -> the comparison block renders the portfolio
        # column on its own, and the intro phrasing follows suit so we
        # don't dangle a "vs the S&P 500" reference with nothing to
        # compare against.
        w = Webpage()
        w.add_return(_total_return(), [])
        assert 'class="section__intro"' in w.return_html
        assert (
            '<p class="section__intro">Cumulative return of the '
            'portfolio.</p>'
            in w.return_html
        )
        assert "S&amp;P 500" not in w.return_html
        assert "benchmark" not in w.return_html.lower()


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
        # Date uses the page-wide DD/MM/YYYY format.
        assert "01/01/2024" in w.historical[0]

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
        # Wrapping each rendered date in <time datetime="..."> makes
        # the holding period machine-readable for crawlers and screen
        # readers without altering the human-facing label. The label
        # uses the page-wide DD/MM/YYYY convention; the ISO
        # ``datetime`` attribute keeps the W3C YYYY-MM-DD format --
        # two conventions serving two different audiences.
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
        # Day 4 -> "04/11/2022" in the visible label (zero-padded);
        # the ISO attribute is ``2022-11-04``.
        assert '<time datetime="2022-11-04">04/11/2022</time>' in card
        assert '<time datetime="2024-04-12">12/04/2024</time>' in card

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
        # DD/MM/YYYY for the visible label; ISO ``datetime``
        # attribute stays in YYYY-MM-DD.
        assert '<time datetime="2024-01-01">01/01/2024</time>' in card
        # "Present" never gets a <time> wrapper.
        assert "<time>Present" not in card
        # The end-of-period section is the dash span followed by the
        # "Present" span -- two separate grid items, no inline " - "
        # separator left over from the old single-row layout.
        assert "<span>-</span><span>Present</span>" in card

    def test_periods_render_as_three_grid_columns(self, stub_logo_lookup):
        # Multi-period cards use a 3-column grid (start, dash, end)
        # so dates and the separator stay aligned vertically across
        # rows. With the page-wide DD/MM/YYYY format every date now
        # has identical character width, which removes the
        # "Jan 22" vs "Aug 5" alignment hazard the grid was first
        # introduced to defend against -- but the structural three-
        # children-per-li contract still has to hold for the layout
        # to work.
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
            '<time datetime="2024-01-22">22/01/2024</time>'
            '<span>-</span>'
            '<time datetime="2024-11-30">30/11/2024</time>'
            '</li>'
        )
        expected_bottom = (
            '<li>'
            '<time datetime="2022-08-05">05/08/2022</time>'
            '<span>-</span>'
            '<time datetime="2023-06-09">09/06/2023</time>'
            '</li>'
        )
        assert expected_top in card
        assert expected_bottom in card
        # And the parent <ul> drives the 3-column grid layout from
        # CSS in <head>; the <li>s themselves use display: contents
        # so their three children land directly in those tracks.
        # Track widths are ``max-content`` so each card sizes its
        # own grid columns to the dates it actually contains. A
        # single open period collapses column 3 to "Present"'s own
        # ~3.5em width, which makes that row read as a tight phrase
        # "<start> - Present" with the dash flanked symmetrically
        # by only the column gap on each side -- no per-row CSS
        # variable required. The earlier fixed-width 6.5em variant
        # (and the special-case ``holding__period--open`` desktop
        # override that compensated for it) is gone.
        full_html = w._head() + card
        assert (
            "grid-template-columns: max-content min-content max-content"
            in full_html
        )
        assert ".holding__periods li { display: contents; }" in full_html
        # ``justify-content: start`` is essential here: without it,
        # CSS Grid would distribute leftover horizontal space inside
        # the <ul> across the tracks, opening visible gaps on wide
        # viewports. With ``justify-content: start`` and content-
        # sized tracks, the entire grid hugs the body's left edge
        # and any leftover width spills past the last column.
        assert "justify-content: start" in full_html
        # Default ``text-align: start`` for the start-date <time>
        # combined with ``text-align: end`` on :last-child gives the
        # spread "<start>  -  <end>" layout for closed periods,
        # while the ``span:last-child`` override left-aligns the
        # "Present" placeholder so it tucks against the dash --
        # locally symmetric with the start date around the dash.
        # The earlier inverse rule (start date hugging the dash,
        # end date hugging the dash) is gone, and so is the
        # desktop-only ``.holding__period--open > :first-child``
        # override that used to compensate for fixed-width slack.
        assert ".holding__periods li > :last-child { text-align: end; }" in full_html
        assert (
            ".holding__periods li > span:last-child { text-align: start; }"
            in full_html
        )
        assert ".holding__periods li > :first-child { text-align: end; }" not in full_html
        assert "holding__period--open" not in full_html
        # Sanity guards against the prior fixed-width variants
        # ("Present" desktop layout looked off because the start
        # date's variable trailing slack created asymmetric gaps
        # around the dash) and the prior loose 7em sizing.
        assert "grid-template-columns: 6.5em" not in full_html
        assert "grid-template-columns: 7em" not in full_html
        assert "6.5em auto 6.5em" not in full_html

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

    def test_holding_card_carries_anchor_id(self, stub_logo_lookup):
        # Every holding capsule exposes a stable ``id`` derived from
        # its ticker. The marquee logo and the equities-bar row
        # both produce ``href`` values from the same ``_holding_anchor``
        # slug, so any drift between the produced ID and the href
        # would break in-page scrolling.
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA"))
        card = w.current[0]
        assert f' id="{Webpage._holding_anchor("NMS:AAA")}"' in card
        # The slug strips punctuation that would otherwise need to
        # be percent-encoded inside a URL fragment.
        assert "holding-NMS-AAA" in card

    def test_historical_holding_card_also_carries_anchor_id(self, stub_logo_lookup):
        # Historical capsules get the same anchor wiring as current
        # ones so a future link surface (e.g. a "trades for X" cross-
        # reference) can scroll to them too without a renderer change.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:OLD",
            is_current=False,
            weight=None,
            periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
        ))
        card = w.historical[0]
        assert ' id="holding-NMS-OLD"' in card

    def test_current_holding_card_carries_sort_attributes(
        self, stub_logo_lookup,
    ):
        # The sort toolbar above each holdings list re-orders cards
        # by reading ``data-sort-*`` attributes on each
        # ``<article class="holding">``. Sanity-check the contract
        # so the toolbar (which has no Python visibility into
        # the values) lines up with what the renderer emits.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:NVDA", name="NVIDIA Corporation",
            tsr=217.4, cagr=64.2, weight=21.4,
        ))
        card = w.current[0]
        # Ticker key drops the exchange prefix and lower-cases so
        # "Sort by Ticker" reads as a clean A->Z run of company
        # symbols.
        assert 'data-sort-ticker="nvda"' in card
        # Names case-fold for the same reason.
        assert 'data-sort-name="nvidia corporation"' in card
        # Numeric keys are emitted with a fixed-decimal float
        # serialisation so int / float upstream values render
        # identically and the JS can ``parseFloat`` them directly.
        assert 'data-sort-tsr="217.4000"' in card
        assert 'data-sort-cagr="64.2000"' in card
        assert 'data-sort-weight="21.4000"' in card

    def test_historical_holding_card_omits_weight_sort_key(
        self, stub_logo_lookup,
    ):
        # Historical positions have no ``current_weight%`` so the
        # card MUST NOT advertise a weight sort key -- the
        # historical toolbar omits the matching button, but a
        # stray ``data-sort-weight`` would still leak the
        # attribute into the DOM (and a sort-by-weight applied
        # to the *current* list could resort historical rows
        # if the JS ever queried by selector globally).
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:OLD",
            name="Old Co.",
            is_current=False,
            weight=None,
            tsr=-12.5, cagr=-7.3,
            periods=[{"start": datetime(2022, 1, 1),
                      "end": datetime(2023, 1, 1)}],
        ))
        card = w.historical[0]
        assert 'data-sort-ticker="old"' in card
        assert 'data-sort-name="old co."' in card
        assert 'data-sort-tsr="-12.5000"' in card
        assert 'data-sort-cagr="-7.3000"' in card
        assert "data-sort-weight" not in card

    def test_holding_title_keeps_exchange_prefix_for_display(
        self, stub_logo_lookup,
    ):
        # The visible title still reads as ``EXCHANGE:SYMBOL -
        # Company`` so the row stays unambiguous; only the
        # *sort key* drops the prefix. Guards against an
        # accidental refactor that lower-cases the displayed
        # ticker too.
        w = Webpage()
        w.add_holding(_holding(
            ticker="NMS:NVDA", name="NVIDIA Corporation",
        ))
        card = w.current[0]
        assert "NMS:NVDA - NVIDIA Corporation" in card


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
        w.add_holding(_holding(
            ticker="NMS:DEAD",
            is_current=False,
            weight=None,
            periods=[{"start": datetime(2023, 1, 1), "end": datetime(2024, 1, 1)}],
        ))
        out = w._build_ticker()
        assert "NMS:LIVE" in out
        assert "NMS:DEAD" not in out

    def test_each_logo_is_wrapped_in_anchor_to_holding_capsule(
        self, stub_logo_lookup,
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


class TestAddTrades:
    def test_renders_one_row_per_event(self, stub_logo_lookup):
        w = Webpage()
        w.add_trades([
            _trade_event(ticker="NMS:AAA", category="OPEN"),
            _trade_event(ticker="NMS:BBB", category="CLOSE",
                         start=datetime(2024, 5, 1)),
        ])
        assert len(w.trades) == 2
        # Each event materialises as a single ``<tr class="trades__row">``;
        # the surrounding ``<table>`` chrome is added by the section
        # builder in ``save()``.
        assert all('class="trades__row"' in row for row in w.trades)
        assert all(row.startswith('<tr ') for row in w.trades)

    def test_no_trades_means_no_rows(self, stub_logo_lookup):
        w = Webpage()
        w.add_trades([])
        assert w.trades == []

    def test_action_collapses_categories_to_bought_or_sold(
        self, stub_logo_lookup,
    ):
        # The "Action" column collapses the four-category space onto
        # a single buy-vs-sell axis: OPEN / INCREASE -> "Bought"
        # (green), DECREASE / CLOSE -> "Sold" (red). Direction is
        # the only thing a glance at this column needs to convey;
        # the lifecycle distinction (was this the first fill or a
        # top-up? did this SELL close the position?) lives in the
        # adjacent "Details" column instead.
        w = Webpage()
        w.add_trades([
            _trade_event(category="OPEN"),
            _trade_event(category="INCREASE", delta_pct=30.0),
            _trade_event(category="DECREASE", delta_pct=25.0),
            _trade_event(category="CLOSE"),
        ])
        # Both BUY-side categories share the green pill modifier
        # ``--buy`` and the "Bought" label; both SELL-side categories
        # share the red ``--sell`` pill and the "Sold" label.
        for row in (w.trades[0], w.trades[1]):
            assert "trade__badge--buy" in row
            assert ">Bought<" in row
            assert "trade__badge--sell" not in row
        for row in (w.trades[2], w.trades[3]):
            assert "trade__badge--sell" in row
            assert ">Sold<" in row
            assert "trade__badge--buy" not in row

    def test_action_pill_is_pinned_to_a_fixed_width(self, stub_logo_lookup):
        # The "Bought" / "Sold" pills must render at byte-for-byte
        # identical width so the column reads as a stack of uniform
        # chips. ``min-width`` alone wasn't enough -- the longer
        # "BOUGHT" label still grew past the shorter "SOLD" one --
        # so the stylesheet pins both to an exact ``width`` box
        # with zero horizontal padding and centered content. ``7em``
        # leaves the longer "BOUGHT" with comfortable padding off
        # the rounded pill ends rather than touching them, and the
        # same value is reused at every mobile breakpoint so the
        # iPhone SE / Galaxy Fold widths don't crop the longer
        # label against the rounded ends (a regression that bit us
        # at 5.25em).
        # (We can't measure actual pixel widths from a static-HTML
        # test, but holding the CSS rule in place is what guarantees
        # the visual invariant downstream.)
        from update import _PAGE_STYLES
        # Locate the ``.trade__badge`` declaration block and assert
        # both ``width`` and ``text-align: center`` survive in it.
        block = _PAGE_STYLES.split(".trade__badge {", 1)[1].split("}", 1)[0]
        assert "width: 7em;" in block
        assert "text-align: center;" in block
        # ``min-width`` is explicitly NOT used here any more; if it
        # crept back in it would re-introduce the "longer label
        # grows the pill" regression.
        assert "min-width" not in block
        # Every mobile override of ``.trade__badge`` also pins to
        # ``width: 7em`` -- there are three ``.trade__badge``
        # declaration blocks total (base + two media-query
        # overrides), and all three must carry the same width
        # token. A future refactor that shrinks the mobile pill
        # back down would silently re-introduce the iPhone SE
        # cropping issue without this guard.
        badge_blocks = _PAGE_STYLES.split(".trade__badge {")
        assert len(badge_blocks) == 4  # 1 base + 2 overrides + leading "" split
        for declared in badge_blocks[1:]:
            assert "width: 7em;" in declared.split("}", 1)[0]

    def test_details_column_uses_past_tense_initiated_and_divested(
        self, stub_logo_lookup,
    ):
        # OPEN / CLOSE rows surface a qualitative label
        # ("Initiated" / "Divested") in the Details column -- the
        # position came into existence or was closed out, and the
        # reader doesn't need a number for those rows. Past-tense
        # verbs match the long-term-investor / fund-letter idiom
        # the rest of the page uses ("Bought" / "Sold" in the
        # action column, "Updated on ..." in the footer). INCREASE
        # / DECREASE rows surface a signed percentage instead
        # ("+30%" for a 30% top-up of the pre-burst position,
        # "-25%" for a 25% trim, using the typographically correct
        # U+2212 minus glyph). Shares never leak into either
        # branch -- the page commits to publishing relative
        # percentages and per-share prices only.
        w = Webpage()
        w.add_trades([
            _trade_event(category="OPEN",     delta_pct=None),
            _trade_event(category="INCREASE", delta_pct=30.0),
            _trade_event(category="DECREASE", delta_pct=25.0),
            _trade_event(category="CLOSE",    delta_pct=None),
        ])
        open_row, inc_row, dec_row, close_row = w.trades
        assert ">Initiated<" in open_row
        assert ">Divested<" in close_row
        # The earlier noun-phrase labels ("Initial stake", "Disposal")
        # are gone.
        for row in w.trades:
            assert "Initial stake" not in row
            assert "Disposal" not in row
        for row in (open_row, close_row):
            # Boundary rows ride the muted ``--label`` modifier so
            # the action badge stays the column's visual primary;
            # no percentage / minus glyph is rendered.
            assert "trades__detail--label" in row
            assert "%" not in row.split("trades__cell--detail")[1].split("</td>")[0]
        # INCREASE / DECREASE: signed-percent readouts with the
        # ``--pct`` modifier and the page's standard
        # ``value--positive`` / ``value--negative`` colour classes
        # so the cell speaks the same language as the holdings'
        # TSR / CAGR rows.
        assert ">+30%<" in inc_row
        assert "trades__detail--pct" in inc_row
        assert "value--positive" in inc_row
        # The minus is the typographically correct U+2212 sign,
        # not the ASCII hyphen-minus, so it aligns with ``+`` in
        # tabular-numbers fonts.
        assert ">\u221225%<" in dec_row
        assert "trades__detail--pct" in dec_row
        assert "value--negative" in dec_row

    def test_single_day_trade_renders_one_quarter_label(
        self, stub_logo_lookup,
    ):
        # The Date column shows calendar quarters instead of
        # to-the-day stamps. A burst that lives inside a single
        # quarter renders the bare quarter label ("Q1 2025") with
        # no separator -- the trade happened in Q1 2025, full stop.
        w = Webpage()
        w.add_trades([
            _trade_event(start=datetime(2025, 1, 14)),
        ])
        row = w.trades[0]
        # Quarter label is wrapped in a single ``<time>`` carrying
        # the first month of the quarter as a W3C ``YYYY-MM``
        # datetime attribute.
        assert '<time datetime="2025-01">Q1 2025</time>' in row
        # The previous DD/MM/YYYY stamp is gone; no separator
        # either (single quarter = single label).
        assert "14/01/2025" not in row
        assert "trades__date-sep" not in row

    def test_multi_day_burst_inside_one_quarter_still_shows_single_label(
        self, stub_logo_lookup,
    ):
        # A burst that's spread over several days but doesn't cross
        # a quarter boundary still renders a single quarter label --
        # the page commits to quarter granularity regardless of how
        # many days the underlying fills span.
        w = Webpage()
        w.add_trades([
            _trade_event(
                start=datetime(2024, 5, 22),
                end=datetime(2024, 6, 11),
            ),
        ])
        row = w.trades[0]
        # Both 22 May 2024 and 11 Jun 2024 sit in Q2 2024.
        assert '<time datetime="2024-04">Q2 2024</time>' in row
        # No to-the-day stamps and no separator since it's a
        # single quarter.
        assert "22/05/2024" not in row
        assert "11/06/2024" not in row
        assert "trades__date-sep" not in row

    def test_burst_spanning_two_quarters_same_year_uses_slash(
        self, stub_logo_lookup,
    ):
        # A burst that crosses a quarter boundary inside one
        # calendar year renders a single slash-joined label
        # ("Q3/Q4 2024") -- the rolling-quarter aggregation window
        # makes this the only realistic multi-quarter case inside
        # a single year, so a slash reads naturally as "spans
        # these two".
        w = Webpage()
        w.add_trades([
            _trade_event(
                start=datetime(2024, 9, 20),  # Q3 2024
                end=datetime(2024, 10, 5),    # Q4 2024
            ),
        ])
        row = w.trades[0]
        assert '<time datetime="2024-07">Q3/Q4 2024</time>' in row
        # Slash format collapses to one ``<time>`` element, no
        # trailing separator span.
        assert "trades__date-sep" not in row

    def test_burst_crossing_year_boundary_uses_hyphen_separator(
        self, stub_logo_lookup,
    ):
        # A burst that crosses a calendar-year boundary (Q4 of one
        # year into Q1 of the next) renders as a hyphen-separated
        # range with two full ``<time>`` elements, since the
        # slash-joined "Q4/Q1 2026" form would be ambiguous about
        # which year owns the Q1. The separator span mirrors what
        # the equity capsules use for multi-period dates so the
        # eye can scan ranges across both surfaces.
        w = Webpage()
        w.add_trades([
            _trade_event(
                start=datetime(2024, 12, 15),  # Q4 2024
                end=datetime(2025, 1, 20),     # Q1 2025
            ),
        ])
        row = w.trades[0]
        assert '<time datetime="2024-10">Q4 2024</time>' in row
        assert '<time datetime="2025-01">Q1 2025</time>' in row
        assert '<span class="trades__date-sep"> - </span>' in row

    def test_price_value_precedes_currency(self, stub_logo_lookup):
        # Prices render as ``<value> <ISO currency>`` (e.g.
        # ``247.85 USD``) so the magnitude lands first and the
        # currency reads as a trailing unit -- the same convention
        # every other quantity on the page uses ("+6.7 pp Total
        # Return", "48.8 %", "2 years"). The ISO code (not the
        # symbol) is still required: a leading ``$`` would silently
        # misrepresent a EUR or GBp trade as USD. The earlier
        # ``@`` "at-the-price-of" prefix is gone -- the column
        # header "Price" already tells the reader what the number
        # is.
        w = Webpage()
        w.add_trades([
            _trade_event(price=247.85, currency="USD"),
            _trade_event(price=181.25, currency="EUR"),
        ])
        assert ">247.85 USD<" in w.trades[0]
        assert ">181.25 EUR<" in w.trades[1]
        # Currency code never precedes the value any more.
        assert "USD 247.85" not in w.trades[0]
        assert "EUR 181.25" not in w.trades[1]
        # No stray ``@`` glyph anywhere on either row.
        assert "@" not in w.trades[0]
        assert "@" not in w.trades[1]

    def test_price_uses_thousands_separator(self, stub_logo_lookup):
        # Large prices (GBp pence quotes, JPY etc.) get a comma so a
        # 4-digit number is readable at a glance. Currency code
        # follows the value (the rest of the page reads quantities
        # the same way).
        w = Webpage()
        w.add_trades([
            _trade_event(price=4820.50, currency="GBp"),
        ])
        assert ">4,820.50 GBp<" in w.trades[0]

    def test_details_pct_renders_as_whole_number(self, stub_logo_lookup):
        # Whole-number percentages by design in this section: the
        # one-decimal page convention from ``_fmt_pct`` is reserved
        # for the performance rows where that extra digit is
        # meaningful. For position-change magnitudes a 4% vs 4.3%
        # split is below the noise floor of how we report sizes.
        w = Webpage()
        w.add_trades([
            _trade_event(category="INCREASE", delta_pct=30.0),
            _trade_event(category="INCREASE", delta_pct=100.0),
            _trade_event(category="DECREASE", delta_pct=99.5),
            _trade_event(category="INCREASE", delta_pct=42.4),
        ])
        assert ">+30%<"  in w.trades[0]
        assert ">+100%<" in w.trades[1]
        # 99.5 rounds up to 100; 42.4 rounds down to 42 -- standard
        # banker's-rounding-adjacent ``{:.0f}`` behaviour, which is
        # close enough to "round half to even" that the rendering
        # convention is uncontroversial for the values that show up
        # in practice. The minus sign is U+2212.
        assert ">\u2212100%<" in w.trades[2]
        assert ">+42%<"  in w.trades[3]

    def test_table_has_no_logo_cell(self, stub_logo_lookup):
        # Logos were removed from the trades table -- the ticker
        # column is the row's anchor now and a 32px glyph stacked
        # to the left of every row added visual noise without
        # carrying information the ticker / company columns
        # didn't already convey. Both the rendered ``<tr>`` and
        # the surrounding ``<table>`` chrome must be logo-free.
        w = Webpage()
        w.add_trades([
            _trade_event(ticker="NMS:NVDA", name="NVIDIA Corporation"),
        ])
        row = w.trades[0]
        assert "trade__logo" not in row
        assert "trades__cell--logo" not in row
        assert "<img" not in row

    def test_ticker_strips_exchange_prefix(self, stub_logo_lookup):
        # The table's ticker column shows only the security symbol --
        # the exchange prefix (``NMS:`` / ``NYQ:`` / ``DUS:``) is
        # redundant noise once the column reads vertically and would
        # bloat the cell width for no information gain. Both the
        # visible cell text and the case-folded sort key must drop it.
        w = Webpage()
        w.add_trades([
            _trade_event(ticker="NMS:NVDA", name="NVIDIA Corporation"),
        ])
        row = w.trades[0]
        assert '>NVDA<' in row
        # Ticker column should not surface the exchange anywhere on
        # the row -- the prefix is stripped before the cell text is
        # rendered.
        assert "NMS:NVDA" not in row
        # Sort key is case-folded so descending / ascending compare
        # ignores capitalisation oddities.
        assert 'data-sort-ticker="nvda"' in row
        # The company name still appears (in its own dedicated cell)
        # so the row remains identifiable even when the table is
        # sorted by symbol.
        assert "NVIDIA Corporation" in row

    def test_row_carries_sort_keys_for_all_sortable_columns(
        self, stub_logo_lookup,
    ):
        # The five sortable columns (ticker / name / action / detail /
        # date) are wired up via ``data-sort-*`` attributes the inline
        # ``_TRADES_SORT_SCRIPT`` reads off each row. Bursts spanning
        # multiple days anchor the date key on ``end_date`` (the most
        # recent event) so sorting by date matches what a reader sees
        # in the rendered cell. ``data-sort-action`` is binary
        # (0 = BUY-into-position, 1 = SELL-out-of-position) so
        # ascending groups Bought above Sold. ``data-sort-detail``
        # uses the four-category dict-order index (OPEN=0, INCREASE=1,
        # DECREASE=2, CLOSE=3) so ascending walks through the
        # position's lifecycle.
        w = Webpage()
        w.add_trades([
            _trade_event(
                ticker="NMS:NVDA", name="NVIDIA Corporation",
                category="INCREASE",
                start=datetime(2024, 5, 22),
                end=datetime(2024, 6, 11),
                delta_pct=30.0,
            ),
        ])
        row = w.trades[0]
        # Date key reflects the burst's most recent event in ISO form.
        assert 'data-sort-date="2024-06-11"' in row
        # Tickers and names are case-folded so the sort is case-
        # insensitive (avoids the "Z before a" surprise that ASCII
        # compare would otherwise produce).
        assert 'data-sort-ticker="nvda"' in row
        assert 'data-sort-name="nvidia corporation"' in row
        # INCREASE is a buy (action == 0) and the second entry in
        # the four-category order (detail == 1).
        assert 'data-sort-action="0"' in row
        assert 'data-sort-detail="1"' in row

    def test_action_sort_key_is_binary_buy_or_sell(self, stub_logo_lookup):
        # OPEN / INCREASE both map to action == 0 (BUY); DECREASE /
        # CLOSE both map to action == 1 (SELL). That collapse is
        # what makes the "Action" column sort group Bought rows
        # above Sold rows with a single numeric compare in the
        # inline sort script.
        w = Webpage()
        w.add_trades([
            _trade_event(category="OPEN"),
            _trade_event(category="INCREASE", delta_pct=10.0),
            _trade_event(category="DECREASE", delta_pct=10.0),
            _trade_event(category="CLOSE"),
        ])
        assert 'data-sort-action="0"' in w.trades[0]
        assert 'data-sort-action="0"' in w.trades[1]
        assert 'data-sort-action="1"' in w.trades[2]
        assert 'data-sort-action="1"' in w.trades[3]

    def test_sort_script_is_deferred_until_dom_ready(self):
        # The inline ``<script>`` ships from <head>, so the
        # ``<table class="trades">`` it queries for hasn't been
        # parsed yet at that point. Without a DOMContentLoaded
        # gate the script would silently bail out on every page
        # load -- which is the sorting bug we're fixing here.
        # The render-chart script uses the same defer pattern;
        # asserting the gate explicitly keeps a regression from
        # quietly re-introducing the issue.
        from update import _TRADES_SORT_SCRIPT
        assert "DOMContentLoaded" in _TRADES_SORT_SCRIPT
        assert "document.readyState===\'loading\'" in _TRADES_SORT_SCRIPT

    def test_short_log_does_not_render_show_all_toggle(
        self, stub_logo_lookup,
    ):
        # When the trade log already fits in the default visible
        # window (``_TRADES_VISIBLE_DEFAULT``) there's nothing to
        # collapse, so no toggle chrome should appear -- the page
        # stays clean and no inert button confuses a screen-reader
        # user. The threshold lives on the ``Webpage`` class so this
        # test reads it rather than hard-coding 10.
        from update import Webpage as _Webpage
        threshold = _Webpage._TRADES_VISIBLE_DEFAULT
        w = Webpage()
        w.add_trades([
            _trade_event(start=datetime(2025, 1, i + 1))
            for i in range(threshold)
        ])
        w.add_return(_total_return(), [])
        # Build the table HTML directly so we exercise the toggle
        # decision without having to call ``save()``.
        table_html = _Webpage._build_trades_table(w.trades)
        assert "trades__toggle" not in table_html
        # The hide-overflow CSS rule still ships unconditionally (a
        # short log just never trips it), so its presence isn't
        # part of the contract this test enforces.

    def test_long_log_renders_show_all_toggle_with_total_count(
        self, stub_logo_lookup,
    ):
        # Once the log exceeds the threshold the renderer emits a
        # ``<button class="trades__toggle">`` after the table whose
        # text labels the full count and whose ``data-total``
        # attribute lets the inline script rebuild the label each
        # time the user collapses the section. ``aria-expanded`` /
        # ``data-expanded`` start out closed so the page paints in
        # the collapsed state without the JS having to "fix it up"
        # post-DOMContentLoaded.
        from update import Webpage as _Webpage
        threshold = _Webpage._TRADES_VISIBLE_DEFAULT
        w = Webpage()
        w.add_trades([
            _trade_event(start=datetime(2025, 1, i + 1))
            for i in range(threshold + 5)
        ])
        table_html = _Webpage._build_trades_table(w.trades)
        total = threshold + 5
        assert 'class="trades__toggle"' in table_html
        assert f'data-total="{total}"' in table_html
        assert 'aria-expanded="false"' in table_html
        assert f">Show all {total} trades<" in table_html
        # The button is emitted AFTER the table closes so it sits
        # below the rows in the visual / reading order, not inside
        # the horizontal-scroll wrapper where it could be clipped.
        toggle_idx = table_html.index('class="trades__toggle"')
        table_close = table_html.index("</table>")
        wrap_close = table_html.index("</div>", table_close)
        assert wrap_close < toggle_idx

    def test_collapse_rule_hides_overflow_rows_by_default(self):
        # The actual hiding is purely CSS: a
        # ``.trades:not([data-expanded="true"]) tbody tr:nth-of-type
        # (n+11) { display: none; }`` rule that walks the current
        # DOM order. That means a sort re-applies the cutoff to
        # whatever the new top N rows are without any JS
        # coordination -- which is the whole point of doing it in
        # CSS instead of building a paging layer. Assert the rule
        # is present so a future refactor can't quietly drop it.
        from update import _PAGE_STYLES, Webpage as _Webpage
        threshold = _Webpage._TRADES_VISIBLE_DEFAULT
        # Threshold + 1 is the first row hidden, which matches the
        # ``:nth-of-type(n+11)`` index in the stylesheet rule.
        rule = (
            f'.trades:not([data-expanded="true"]) tbody '
            f'tr.trades__row:nth-of-type(n+{threshold + 1})'
        )
        assert rule in _PAGE_STYLES

    def test_toggle_script_flips_state_and_relabels_button(self):
        # The toggle handler is folded into ``_TRADES_SORT_SCRIPT``
        # so the trades section ships one inline script (one CSP
        # hash). Assert the script (a) queries for the toggle, (b)
        # flips ``data-expanded`` on the table on click, and (c)
        # rewrites the button label and ``aria-expanded`` so the
        # collapsed / expanded states stay in sync. We're checking
        # textual presence rather than running the script -- the
        # browser-level smoke test in /tmp/check_sort.py exercises
        # the full state machine end-to-end.
        from update import _TRADES_SORT_SCRIPT
        for needle in (
            ".trades__toggle",
            "data-expanded",
            "aria-expanded",
            "Show fewer trades",
            "Show all ",
        ):
            assert needle in _TRADES_SORT_SCRIPT

    def test_name_and_currency_are_html_escaped(self, stub_logo_lookup):
        # Even though tickers/names are sourced from a trusted sheet,
        # we still escape so an "&" or "<" in a security name can't
        # break the rendered HTML.
        w = Webpage()
        w.add_trades([
            _trade_event(name="S&P Global Inc."),
        ])
        row = w.trades[0]
        assert "S&amp;P Global Inc." in row
        # No raw ``&P`` leaks.
        assert "S&P Global" not in row


class TestSaveTradesSection:
    def test_save_emits_trades_section_when_present(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_trades([
            _trade_event(ticker="NMS:AAA", category="OPEN",
                         start=datetime(2024, 1, 1)),
        ])
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        # Section anchor + heading + methodology subtitle are present.
        # The visible heading is just "Trades" -- the same word the
        # nav link uses, keeping the section name and the nav label
        # in lock-step. The URL fragment stays ``#trades`` so old
        # bookmarks and the nav link don't break.
        assert 'id="trades"' in out
        assert ">Trades</h2>" in out
        # Previous headings are fully retired -- a leftover "Recent
        # trades" or "Trade log" anywhere on the page would mean we
        # missed a comment or label during the rename.
        assert "Recent trades" not in out
        assert "Trade log" not in out
        # Subtitle covers the section's two methodology facts: it
        # spans the full ownership history (no trailing-year cutoff
        # any more) and rolling-quarter bursts are combined.
        assert "Every executed trade since inception" in out
        assert "rolling quarter" in out
        # Nav picks up the new section once trades are present.
        assert 'href="#trades"' in out
        # Section sits below historical / current sections in the
        # source order so the activity log reads as detail after the
        # high-level portfolio summary.
        idx_perf = out.index('id="performance"')
        idx_trades = out.index('id="trades"')
        assert idx_perf < idx_trades
        # Single ``<table class="trades">`` per page, with the
        # sortable thead and the row tbody wired up via
        # ``data-sort-*`` attributes. Sanity-check the contract the
        # inline ``_TRADES_SORT_SCRIPT`` depends on (one such table,
        # default sort key + direction declared on it).
        assert out.count('<table class="trades"') == 1
        assert 'data-sort-default="date"' in out
        assert 'data-sort-default-dir="desc"' in out
        # All five sortable column headers carry their sort key so
        # the click handler can dispatch on it.
        for key in ("ticker", "name", "action", "detail", "date"):
            assert f'data-sort-key="{key}"' in out
        # The Price column is non-sortable (mixing currencies in a
        # numeric sort would imply an FX-converted ordering the
        # page doesn't compute), so it never carries a sort key.
        assert 'data-sort-key="price"' not in out

    def test_save_skips_trades_section_when_empty(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        # No add_trades call -> ``self.trades`` is empty.
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        # Anchor, heading element, and nav link are all gone. We
        # match the rendered ``<h2>`` heading rather than the bare
        # "Trades" substring -- the section's name also lives in
        # a CSS comment in the embedded stylesheet, so a plain
        # substring search would yield a false positive.
        assert 'id="trades"' not in out
        assert ">Trades</h2>" not in out
        assert 'href="#trades"' not in out
        # No actual rendered table either.
        assert '<table class="trades"' not in out
        assert 'class="trades__row"' not in out

    def test_save_trades_after_historical_section(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # When the page carries both the historical holdings section
        # and the trades section, trades appears last so the page
        # reads as: performance -> current -> historical -> activity.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(
            ticker="NMS:OLD",
            is_current=False, weight=None,
            periods=[{"start": datetime(2022, 1, 1),
                      "end": datetime(2023, 1, 1)}],
        ))
        w.add_trades([_trade_event(start=datetime(2024, 1, 1))])
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        idx_hist = out.index('id="historical"')
        idx_trades = out.index('id="trades"')
        assert idx_hist < idx_trades


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
        # Width is rendered with two decimals for sub-pixel precision
        # (the input ``value`` is now an unrounded float).
        assert "width: 95.40%" in out
        assert "width: 4.60%" in out
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
        # Largest holding fills its track entirely. Width uses two
        # decimals for sub-pixel precision since ``value`` is now an
        # unrounded float upstream.
        assert "width: 100.00%" in out
        # 25 / 50 = 50; 10 / 50 = 20.
        assert "width: 50.00%" in out
        assert "width: 20.00%" in out
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
        assert "width: 0.00%" in out

    def test_anchored_rows_render_as_links(self):
        # Rows whose label appears in the ``anchors`` map become
        # ``<a class="bars__row--link">`` elements pointing at the
        # target anchor; the rest stay as plain ``<div>`` rows.
        out = Webpage._render_bars(
            [("Equities", 95.4), ("Cash & Cash Equivalents", 4.6)],
            "allocation",
            anchors={"Equities": "equities"},
        )
        # The Equities row links to ``#equities``.
        assert 'href="#equities"' in out
        assert 'class="bars__row bars__row--link"' in out
        # The cash row has no anchor entry -> stays a non-linked
        # ``<div class="bars__row">``.
        cash_block = out.split("Cash &amp;", 1)[1].split("</div></div>", 1)[0]
        assert "bars__row--link" not in cash_block

    def test_unanchored_rows_stay_as_divs(self):
        # No ``anchors`` argument at all -> every row renders as a
        # plain ``<div class="bars__row">`` (the legacy shape).
        out = Webpage._render_bars(
            [("A", 50.0), ("B", 25.0)], "equities", scale_to_max=True,
        )
        assert "bars__row--link" not in out
        assert "<a " not in out
        assert out.count('class="bars__row"') == 2

    def test_anchors_for_unknown_labels_are_ignored(self):
        # A stray label in ``anchors`` that doesn't match any row is
        # silently ignored -- the caller doesn't have to filter down
        # to "real" tickers before passing the map.
        out = Webpage._render_bars(
            [("A", 50.0)], "equities",
            anchors={"A": "holding-A", "MISSING": "holding-MISSING"},
        )
        assert 'href="#holding-A"' in out
        assert "MISSING" not in out

    def test_anchored_rows_preserve_label_value_track_order(self):
        # The label-value-track ordering invariant from the non-linked
        # path must hold on the linked rows too, so the visual layout
        # is identical regardless of whether a row is clickable.
        out = Webpage._render_bars(
            [("Equities", 95.4)], "allocation",
            anchors={"Equities": "equities"},
        )
        label_idx = out.index('bars__label')
        value_idx = out.index('bars__value')
        track_idx = out.index('bars__track')
        assert label_idx < value_idx < track_idx

    def test_anchor_id_is_html_escaped(self):
        # Anchor values flow into an HTML attribute so the renderer
        # must escape them; otherwise a malformed slug (an unlikely
        # but cheap-to-guard regression) could break out of the
        # ``href`` and into surrounding markup.
        out = Webpage._render_bars(
            [("A", 50.0)], "equities",
            anchors={"A": 'evil"<script>'},
        )
        assert '<script>' not in out
        assert "&lt;script&gt;" in out or "&quot;" in out


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

    def test_outperformance_label_uses_canonical_twr_minus_tsr_when_provided(self):
        # When ``total_return["twr%"]`` and ``benchmark["tsr%"]`` are
        # available (the production path), the chart's pp-delta label
        # must come from those canonical metrics so it stays in sync
        # with the JG vs S&P 500 capsule below the chart -- which
        # also displays ``twr% - tsr%`` as its ``Total Return`` delta.
        # The discrete history endpoints (1.20 vs 1.05 = +15.0 pp)
        # are intentionally chosen NOT to match the TWR/TSR pair
        # (+18.4 vs +5.7 = +12.7 pp) so a regression to history-based
        # math would surface as a wrong assertion here.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "tsr%": 5.7,
            "history": [(datetime(2024, 1, 1), 1.0),
                        (datetime(2024, 6, 1), 1.02),
                        (datetime(2024, 12, 1), 1.05)],
        }
        out = Webpage._render_return_chart(
            {"history": history, "twr%": 18.4}, [benchmark]
        )
        assert "+12.7 pp" in out
        # And explicitly: the history-derived value must NOT appear
        # as the chart label. (``+15.0 pp`` could in theory show up
        # elsewhere on the page in some other test-data scenario, but
        # here it would only come from a regression in this code
        # path, since no other call site emits it.)
        assert "+15.0 pp" not in out

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
        # "Since X" caption reads as prose, so this one specific
        # spot uses the long-form ``%b %-d, %Y`` from
        # ``_fmt_date_long`` rather than the page-wide DD/MM/YYYY
        # convention. ISO ``datetime`` attribute stays in W3C
        # YYYY-MM-DD.
        assert (
            '<time datetime="2024-01-01">Jan 1, 2024</time>'
            in caption
        )
        assert "4 months" in caption
        # The old "range X-Yx" caption format is gone.
        assert "range" not in caption


class TestReturnChartScrubber:
    """The pointer-driven scrubber overlay and its data contract.

    The interactive layer is driven by ``_RETURN_CHART_SCRIPT`` at
    runtime; here we verify the static markup the renderer emits so
    that JS contract stays intact: the JSON payload on the
    ``<figure>``, the empty hover container (guide line + tooltip
    skeleton) inside ``.return-chart__plot``, and the CSS hooks the
    script targets.
    """

    @staticmethod
    def _parse_chart_attr(out):
        import html as _html
        import json as _json
        # The ``data-chart`` attribute on the figure is a double-quoted
        # HTML-escaped JSON blob. Extract and decode it.
        marker = 'data-chart="'
        start = out.index(marker) + len(marker)
        end = out.index('"', start)
        return _json.loads(_html.unescape(out[start:end]))

    def test_jg_only_chart_embeds_single_series_payload(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        out = Webpage._render_return_chart({"history": history}, [])
        data = self._parse_chart_attr(out)
        # Anchor date is the JG history's first sample, in ISO form.
        assert data["start"] == "2024-01-01"
        # Total day span is the distance from start to last sample.
        assert data["totalDays"] == (datetime(2024, 12, 1) - datetime(2024, 1, 1)).days
        # Single-series chart has no right-margin reserve (no delta).
        assert data["rightPct"] == 0
        # y-domain frames the values with a small headroom on both
        # sides; here the data spans 1.0..1.2 -> bounds straddle that.
        assert data["yMin"] < 1.0 < data["yMax"]
        assert data["yMax"] > 1.2
        # Only the JG series is present; bench is absent.
        kinds = [s["kind"] for s in data["series"]]
        assert kinds == ["jg"]
        jg = data["series"][0]
        assert jg["label"] == "JG"
        # With three or more history points the renderer embeds the
        # SAME densely-sampled Pchip curve the SVG polyline draws,
        # so the marker dots track the rendered line exactly.
        assert len(jg["x"]) == 200
        assert len(jg["y"]) == 200
        # Dense samples span the full history range.
        assert jg["x"][0] == 0
        assert jg["x"][-1] == 335
        # Endpoints match the raw history; the Pchip spline goes
        # through every original sample.
        assert jg["y"][0] == 1.0
        assert jg["y"][-1] == 1.2
        # Mid-sample is the interpolated value at day 152 (the June
        # 1st sample). Pchip preserves the original points to
        # numerical precision.
        mid = jg["x"].index(min(jg["x"], key=lambda x: abs(x - 152)))
        assert jg["y"][mid] == pytest.approx(1.1, abs=5e-3)

    def test_chart_with_benchmark_embeds_both_series(self):
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
        data = self._parse_chart_attr(out)
        # Right-margin reserve matches the delta overlay width so the
        # scrubber doesn't run the guide past the curves' last point.
        assert data["rightPct"] == 12.0
        kinds = [s["kind"] for s in data["series"]]
        assert kinds == ["jg", "bench"]
        bench = data["series"][1]
        assert bench["label"] == "S&P 500"
        # Both series share the same densely-sampled x-axis so the
        # tooltip date, marker dots, and local caliper stay in
        # lockstep across the two curves.
        assert bench["x"] == data["series"][0]["x"]
        # Endpoints of the Pchip-interpolated bench curve match the
        # raw history.
        assert bench["y"][0] == 1.0
        assert bench["y"][-1] == 1.05

    def test_two_point_history_skips_dense_interpolation(self):
        # With only two samples there's nothing to spline through;
        # the renderer plots straight segments, so the embedded
        # payload mirrors the raw history rather than a 200-point
        # dense curve.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 1), 1.2),
        ]
        out = Webpage._render_return_chart({"history": history}, [])
        data = self._parse_chart_attr(out)
        jg = data["series"][0]
        assert len(jg["x"]) == 2
        assert jg["x"] == [0, 335]
        assert jg["y"] == [1.0, 1.2]

    def test_data_chart_attribute_is_html_escaped(self):
        # The bench label may contain an ``&`` (e.g. "S&P 500") which
        # would otherwise terminate the surrounding attribute. The
        # renderer must HTML-escape the JSON blob so the page parses
        # cleanly and the browser hands JS the original characters.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 1), 1.2),
        ]
        benchmark = {"ticker": "LSE:VUAA.L",
                     "history": [(datetime(2024, 1, 1), 1.0),
                                 (datetime(2024, 12, 1), 1.05)]}
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        # The raw ``&`` in "S&P 500" must NOT appear unescaped inside
        # the attribute, and double quotes used by JSON must be
        # encoded so they don't terminate the attribute.
        chart_attr_block = out.split('data-chart="', 1)[1].split('"', 1)[0]
        assert "&amp;" in chart_attr_block
        assert "S&P" not in chart_attr_block
        assert "&quot;" in chart_attr_block
        # And once unescaped + parsed, the label round-trips intact.
        data = self._parse_chart_attr(out)
        assert data["series"][1]["label"] == "S&P 500"

    def test_hover_overlay_skeleton_is_present(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        out = Webpage._render_return_chart({"history": history}, [])
        # All the DOM hooks the scrubber script queries must be
        # rendered ahead of time so the script doesn't have to build
        # the skeleton on init.
        assert 'class="return-chart__hover"' in out
        assert 'class="return-chart__guide"' in out
        assert 'class="return-chart__tooltip"' in out
        assert 'class="return-chart__tooltip-date"' in out
        assert 'class="return-chart__tooltip-rows"' in out
        # The hover overlay sits INSIDE ``.return-chart__plot`` so it
        # can be positioned relative to the curves (and so the SVG +
        # delta + hover share a single positioning canvas).
        plot_block = out.split('class="return-chart__plot"', 1)[1].split("</div>", 2)
        # The opening tag's enclosing </div> is the last token; we
        # only need to know the hover element appears before the plot
        # block closes -- ie. inside its content.
        assert 'class="return-chart__hover"' in plot_block[0] + plot_block[1]
        # aria-hidden keeps screen readers focused on the surrounding
        # comparison block (which already carries the numeric story).
        assert 'aria-hidden="true"' in out
        # No benchmark -> no local outperformance caliper or pp row
        # (the moving caliper has no second curve to anchor against).
        assert 'return-chart__hover-delta-bar' not in out
        assert 'return-chart__tooltip-delta' not in out

    def test_hover_delta_elements_render_when_benchmark_present(self):
        # With a benchmark there's a second curve to compare against,
        # so the renderer emits the moving caliper bar + pp row that
        # mirror the static end-of-period annotation -- the script
        # positions them at the cursor's x at runtime.
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
        assert 'class="return-chart__hover-delta-bar"' in out
        assert 'class="return-chart__tooltip-delta"' in out
        # The hover overlay sits BEFORE the static delta so the CSS
        # rule ``.return-chart__hover.is-active ~ .return-chart__delta``
        # can dim the static label while the scrubber is active.
        hover_idx = out.index('class="return-chart__hover"')
        static_delta_idx = out.index('class="return-chart__delta"')
        assert hover_idx < static_delta_idx

    def test_short_history_omits_chart_and_data(self):
        # Single-sample history -> no chart, no scrubber data.
        out = Webpage._render_return_chart(
            {"history": [(datetime(2024, 1, 1), 1.0)]}, []
        )
        assert out == ""


class TestReturnChartScript:
    """The inline ``_RETURN_CHART_SCRIPT`` payload + its CSP wiring."""

    def test_script_is_loaded_from_head_with_csp_hash(self, stub_logo_lookup):
        # The scrubber script ships in <head> so it's parsed before
        # the chart paints, and its SHA-256 must be pinned in CSP
        # ``script-src`` -- otherwise the browser refuses to execute
        # it and the chart loses the interaction.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        head = Webpage._head()
        # The script body itself is in the head.
        assert update._RETURN_CHART_SCRIPT in head
        # And its SHA-256 hash is referenced from the CSP meta tag.
        digest = update._sha256_b64(update._RETURN_CHART_SCRIPT)
        assert f"sha256-{digest}" in head

    def test_script_initialises_pointer_event_handlers(self):
        # The script must own the contract its rendered chart expects:
        # it has to react to pointer movement and project values onto
        # the curves. The exact wiring is JS, so we sanity-check the
        # payload references the key DOM hooks and APIs.
        script = update._RETURN_CHART_SCRIPT
        # Reads its data from the figure attribute.
        assert "data-chart" in script
        # Wires up the unified pointer events (covers mouse + touch).
        assert "pointermove" in script
        assert "pointerleave" in script
        # Toggles the active state CSS hook.
        assert "is-active" in script
        # Populates the tooltip and markers via the documented hooks.
        assert "return-chart__tooltip-rows" in script
        assert "return-chart__marker" in script
        # Drives the moving caliper bar + tooltip pp row when a
        # benchmark is present.
        assert "return-chart__hover-delta-bar" in script
        assert "return-chart__tooltip-delta" in script
        # Uses the same ``--delta-color`` custom property the static
        # caliper consumes so green/red mapping stays uniform.
        assert "--delta-color" in script


class TestNavScrollScript:
    """The inline ``_NAV_SCROLL_SCRIPT`` payload + the smooth-scroll
    contract that drives clicks on every in-page anchor."""

    def test_selector_targets_all_in_page_anchors_except_skip_link(self):
        # The smooth-scroll handler used to be scoped to ``.site-nav``
        # only. With marquee logos and equities-bar rows also acting
        # as in-page anchors, the selector is broadened to cover every
        # same-page link -- minus ``.skip-link``, which assistive-tech
        # users expect to jump instantly.
        script = update._NAV_SCROLL_SCRIPT
        assert 'a[href^="#"]:not(.skip-link)' in script
        # And the old narrow selector is gone (regression guard).
        assert ".site-nav a[" not in script

    def test_easing_is_ease_out_quart_not_ease_in_out_cubic(self):
        # ``easeOutQuart`` (``1 - (1-t)^4``) front-loads motion so the
        # scroll picks up speed in the first frame and decelerates
        # into the target. The earlier ``easeInOutCubic`` curve
        # (``t < 0.5 ? 4t^3 : 1 - (-2t + 2)^3 / 2``) felt as though
        # the page lagged at the start and then "caught up" through
        # an accelerating middle, which is what the user-reported
        # "accelerates with lag" complaint was describing.
        script = update._NAV_SCROLL_SCRIPT
        # The new curve uses a quartic decay over ``1 - t``.
        assert "var u=1-t;return 1-u*u*u*u;" in script
        # And the old cubic-in-out branches are gone.
        assert "4*t*t*t" not in script
        assert "Math.pow(-2*t+2,3)" not in script

    def test_duration_window_is_tightened(self):
        # ``Math.min(650,Math.max(280,dist*0.30))`` -- shorter and
        # more responsive than the previous ``min(900, max(450,
        # dist*0.45))``. Tight enough that even a top-of-page-to-
        # bottom slide completes in ~650ms while a same-section
        # hop is nearly instantaneous (280ms).
        script = update._NAV_SCROLL_SCRIPT
        assert "Math.min(650,Math.max(280,dist*0.30))" in script
        # Sanity-check the old window isn't still hiding somewhere.
        assert "Math.max(450" not in script
        assert "Math.min(900" not in script

    def test_blurs_clicked_anchor_so_marquee_can_resume(self):
        # When a marquee logo is clicked the browser focuses the
        # ``<a>``, which (combined with a ``.ticker:focus-within``
        # CSS rule) used to keep the strip paused until the user
        # clicked elsewhere. Even though the CSS rule is gone, we
        # also blur the activated anchor: it stops any sticky-
        # focus highlight (e.g. on the equities-allocation rows
        # on touch) and is robust to a future CSS regression that
        # accidentally re-introduces a focus-within pause.
        script = update._NAV_SCROLL_SCRIPT
        # ``a`` is the local variable holding the closest matching
        # anchor; blur is wrapped in a try/catch so an environment
        # without a blur method never crashes the handler.
        assert "a.blur" in script

    def test_reads_scroll_margin_top_so_anchored_targets_clear_header(self):
        # The slide must respect ``scroll-margin-top`` -- otherwise
        # holding capsules (which set it to 120px so the sticky
        # header doesn't cover them) would land underneath the
        # header. The renderer relies on this contract when it
        # plumbs ``scroll-margin-top`` onto ``.holding`` /
        # ``.section__subtitle``.
        script = update._NAV_SCROLL_SCRIPT
        assert "scrollMarginTop" in script

    def test_target_is_locked_at_slide_start(self):
        # An earlier version re-read ``targetY(el)`` on every frame
        # to absorb mid-flight layout shifts. With explicit logo
        # dimensions reserving the layout, that re-read no longer
        # absorbs anything -- but it does amplify sub-pixel drift
        # into visible jitter because each frame rescales the full
        # trajectory. The slide now locks ``ty0`` once at start and
        # only re-reads ``targetY`` once more at the very end (the
        # "settle") to catch any pixel-level shift without
        # contaminating the easing curve.
        script = update._NAV_SCROLL_SCRIPT
        # Both the start scroll position and the start target are
        # captured at slide entry.
        assert "var sy0=sy(),ty0=targetY(el)" in script
        # The body of the easing loop drives off the locked target
        # rather than re-reading every frame.
        assert "sy0+(ty0-sy0)*ease(t)" in script
        # And the no-longer-needed mid-flight ``ty`` lookup that
        # used to live inside ``step`` is gone.
        assert "var ty=targetY(el);" not in script

    def test_scroll_uses_explicit_behavior_auto(self):
        # The page used to ship ``html:focus-within { scroll-
        # behavior: smooth }`` so anchor clicks would smooth-
        # scroll via CSS. With ``_NAV_SCROLL_SCRIPT`` driving the
        # animation in JS that rule turned into a hazard: it
        # promoted every ``window.scrollTo`` in our easing loop
        # to a *browser-native* smooth scroll on top of our own
        # frame-by-frame motion, producing the user-reported
        # "the animation looks odd" double-animation feel.
        # Even with the CSS rule removed (see the
        # ``TestPageStyles`` regression guard), the script still
        # opts out of any future / inherited smooth-scroll by
        # passing ``behavior: 'auto'`` explicitly.
        script = update._NAV_SCROLL_SCRIPT
        assert "behavior:'auto'" in script
        # A graceful fallback to the legacy positional form is
        # in place for engines that don't accept the options
        # object (very old WebKit etc.).
        assert "window.scrollTo(0,y)" in script

    def test_blur_happens_before_the_slide_starts(self):
        # If the activated anchor stays focused when ``slide``
        # begins, ``html:focus-within`` would (re-)match any
        # future CSS rule that gates on focus, and the marquee's
        # pause-on-focus rule (defensive, kept gated behind
        # ``@media (hover: hover)``) plus any user-style
        # extensions could re-introduce the double-scroll glitch.
        # Blurring before the slide guarantees the rAF loop runs
        # with focus already off the anchor.
        script = update._NAV_SCROLL_SCRIPT
        blur_idx = script.index("a.blur")
        slide_idx = script.index("slide(el,dur)")
        assert blur_idx < slide_idx


class TestInteractionStyles:
    """CSS gating around interactive states (marquee pause + linked
    rows). Verified against the saved page so we exercise the same
    inline stylesheet a browser would render."""

    def test_marquee_pauses_only_on_real_pointer_hover(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The pause-on-hover rule lives inside ``@media (hover:
        # hover)`` so a tap on a touch device (which historically
        # latched into a sticky ``:hover`` state) never freezes
        # the marquee. The ``:focus-within`` variant is gone too:
        # mouse-clicking a marquee anchor focuses it by default,
        # and the previous CSS used to keep the strip parked
        # until the user clicked somewhere else.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.save()
        out = (chdir_tmp / "index.html").read_text()

        # The pause rule is wrapped in a hover-capable media query.
        assert (
            "@media (hover: hover) {\n"
            "  .ticker:hover .ticker__track { animation-play-state: paused; }\n"
            "}"
        ) in out
        # And the focus-within variant that used to keep the bar
        # parked after a click is gone.
        assert ".ticker:focus-within" not in out
        # Regression guard: the unconditional ``.ticker:hover``
        # rule (the previous shape) is absent.
        assert (
            ".ticker:hover .ticker__track,\n.ticker:focus-within"
        ) not in out

    def test_marquee_link_hover_is_gated_to_pointer_devices(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # Same touch-device caveat as the strip itself: the
        # logo-lift hover effect lives behind ``@media (hover:
        # hover)`` so a tap doesn't leave a logo permanently
        # brightened. ``:focus-visible`` stays outside the gate
        # for keyboard users.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.save()
        out = (chdir_tmp / "index.html").read_text()

        assert ".ticker__link:focus-visible .ticker__logo { opacity: 1; }" in out
        assert (
            "@media (hover: hover) {\n"
            "  .ticker__link:hover .ticker__logo { opacity: 1; }\n"
            "}"
        ) in out

    def test_no_css_smooth_scroll_layered_on_top_of_js_animation(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The previous shape of the page shipped
        # ``html:focus-within { scroll-behavior: smooth; }`` so the
        # browser would smooth-scroll once an anchor was focused.
        # ``_NAV_SCROLL_SCRIPT`` now drives the scroll itself, and
        # the CSS rule -- which still matched the moment a click
        # focused the anchor -- promoted every per-frame
        # ``window.scrollTo`` to a browser-native smooth scroll
        # too, producing two competing animations and the "the
        # animation looks odd" feel from short hops in the
        # allocation chart. The fix is to keep the JS animation as
        # the sole driver: no ``html:focus-within {scroll-behavior:
        # smooth}`` (or the matching reduced-motion override) in
        # the rendered stylesheet.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.save()
        out = (chdir_tmp / "index.html").read_text()

        # The page emits no ``scroll-behavior`` *declaration* (a
        # semicolon-terminated CSS property): the only ``scroll-
        # behavior`` text left in the file is inside an explanatory
        # CSS comment, which never reaches the rendered style.
        # ``smooth;`` / ``auto;`` are what a real declaration would
        # look like, so this catches both the old rule and any
        # accidental reintroduction.
        assert "scroll-behavior: smooth;" not in out
        assert "scroll-behavior: auto;" not in out
        # The reduced-motion media query is still emitted (the
        # ticker animation override needs to live), but no
        # ``html:focus-within`` *selector* should appear in the
        # reduced-motion block either. We tokenise the block by
        # its selector-introducing newline + ``html:focus-within``
        # so a comment-text mention doesn't trip the check.
        rm_split = out.split("@media (prefers-reduced-motion: reduce)", 1)
        assert len(rm_split) > 1, "reduced-motion block missing"
        rm_tail = rm_split[1].split("\n}\n", 1)[0]
        assert "\n  html:focus-within " not in rm_tail
        assert "\nhtml:focus-within " not in rm_tail

    def test_bars_row_link_hover_is_gated_to_pointer_devices(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The user-reported regression: tapping a ticker row in the
        # equities allocation chart on a touch device left the row
        # highlighted after the finger lifted (``:hover`` sticks on
        # touch). Gating on ``@media (hover: hover)`` keeps the
        # hover affordance for mouse / trackpad readers while touch
        # users only see the highlight while their finger is
        # actually on the row.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_allocations({"Equities": 95.0}, {"NMS:AAA": 50.0})
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.save()
        out = (chdir_tmp / "index.html").read_text()

        # The hover branch is gated.
        assert "@media (hover: hover)" in out
        # The keyboard focus branch is not, so the row still gets
        # a visible state when reached via the tab order.
        assert ".bars__row--link:focus-visible {" in out
        # Regression guard: the comma-joined unconditional
        # ``:hover, :focus-visible`` shape that produced the
        # sticky-tap behaviour is gone.
        assert (
            ".bars__row--link:hover,\n.bars__row--link:focus-visible"
        ) not in out


class TestAddAllocations:
    def test_stores_values_for_save(self):
        w = Webpage()
        w.add_allocations({"Equities": 95.4}, {"NMS:AAA": 50.0})
        assert w.allocation_pct == {"Equities": 95.4}
        assert w.top_10 == {"NMS:AAA": 50.0}


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
        # machine-readable <time> element. The "Updated on X"
        # line reads as prose, so the human label uses the
        # long-form ``%b %-d, %Y`` from ``_fmt_date_long`` (the
        # slash-separated DD/MM/YYYY format used in the tabular
        # parts of the page would break the sentence rhythm).
        # The ISO ``datetime`` attribute stays in W3C YYYY-MM-DD.
        assert '<time datetime="2025-06-01">Jun 1, 2025</time>' in out

    def test_save_footer_has_methodology_and_disclaimer_headings(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The footer is split into two labelled blocks: the
        # "Methodology" heading sits above the bulleted notes
        # (base currency / Dietz / TWR scope / data source), and the
        # "Disclaimer" heading sits above the informational-purposes
        # paragraph and the logos/analytics legal note. Heading level
        # mirrors ``.section__title`` (h2) inside ``<main>`` so the
        # document outline stays linear.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        w.add_holding(_holding(ticker="NMS:CURR", name="Currentco"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Both headings present, rendered as ``<h2 class="footer__title">``.
        assert '<h2 class="footer__title">Methodology</h2>' in out
        assert '<h2 class="footer__title">Disclaimer</h2>' in out
        # Methodology heading sits above the bullet list; Disclaimer
        # heading sits above the informational-purposes paragraph
        # (and consequently above the logos/analytics legal note).
        methodology_idx = out.index('<h2 class="footer__title">Methodology</h2>')
        notes_idx = out.index('class="footer__notes"')
        disclaimer_heading_idx = out.index('<h2 class="footer__title">Disclaimer</h2>')
        disclaimer_para_idx = out.index('class="footer__disclaimer"')
        legal_idx = out.index('class="footer__legal"')
        assert methodology_idx < notes_idx < disclaimer_heading_idx
        assert disclaimer_heading_idx < disclaimer_para_idx < legal_idx
        # And explicitly: neither heading shows up in the in-page nav
        # -- the nav only lists portfolio sections, the footer remains
        # a tail-of-page reference without nav targets.
        nav_start = out.index('<nav class="site-nav"')
        nav_end = out.index('</nav>', nav_start)
        nav_html = out[nav_start:nav_end]
        assert "Methodology" not in nav_html
        assert "Disclaimer" not in nav_html

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
        # for the Cloudflare beacon + hash-pinned inline payloads. Pull
        # the CSP directives apart so we can assert the beacon URL is
        # an explicit token of ``script-src`` (rather than just any
        # substring of ``out``, which CodeQL flags as
        # ``py/incomplete-url-substring-sanitization``); this is also
        # a stronger check -- a typo that drops the URL outside
        # ``script-src`` would no longer pass. The token check uses
        # explicit ``==`` per element rather than ``URL in <list>``,
        # because CodeQL doesn't reliably distinguish list-membership
        # from substring containment and still flags the latter shape.
        csp = out.split(
            'http-equiv="Content-Security-Policy" content="', 1
        )[1].split('"', 1)[0]
        directives: dict[str, list[str]] = {}
        for directive in csp.split(";"):
            tokens = directive.strip().split()
            if tokens:
                directives[tokens[0]] = tokens[1:]

        def _contains(tokens: list[str], expected: str) -> bool:
            return any(token == expected for token in tokens)

        assert _contains(directives["default-src"], "'self'")
        assert _contains(
            directives["script-src"], "https://static.cloudflareinsights.com"
        )
        assert _contains(directives["frame-ancestors"], "'none'")
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

    def test_save_wires_click_to_scroll_targets_across_sections(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # End-to-end contract for the three new click affordances:
        #   * a marquee logo links to the matching holding capsule;
        #   * the "Equities" allocation bar links to the equities
        #     sub-section right below the allocation chart;
        #   * each ticker row in the equities chart links to the
        #     matching holding capsule, while the synthetic
        #     "Other equities" bucket stays non-clickable.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_allocations(
            {"Equities": 95.4, "Cash & Cash Equivalents": 4.6},
            {"NMS:AAA": 60.0, "NMS:BBB": 25.0, "Other equities": 10.0},
        )
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha"))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta"))
        w.save()
        out = (chdir_tmp / "index.html").read_text()

        # Holding capsules expose stable anchor ids.
        assert ' id="holding-NMS-AAA"' in out
        assert ' id="holding-NMS-BBB"' in out
        # Equities sub-heading exposes the anchor the allocation
        # chart's "Equities" row targets.
        assert 'id="equities" class="section__subtitle"' in out
        # Marquee logos link to the matching capsule.
        assert 'href="#holding-NMS-AAA"' in out
        assert 'href="#holding-NMS-BBB"' in out
        # Allocation chart: "Equities" row links to the sub-section,
        # cash row stays unlinked (no anchor block created for it).
        assert 'href="#equities"' in out
        # The two click-target classes are present (marquee + bar rows).
        assert 'class="ticker__link"' in out
        assert 'class="bars__row bars__row--link"' in out
        # "Other equities" is rendered as a plain non-linked bar
        # row: its label appears, but no ``href="#holding-Other-...
        # "`` ever does (which would be a 404 anchor anyway since
        # there's no card behind it).
        assert "Other equities" in out
        assert "holding-Other" not in out

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


class TestHoldingsSortControl:
    def test_current_section_renders_full_sort_button_set(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The current-holdings toolbar exposes the full set
        # (Default / Ticker / Name / TSR / CAGR / Weight) since
        # current rows carry all five sort dimensions; the
        # historical toolbar drops Weight (no current weight on
        # closed positions). Default starts ``aria-pressed="true"``
        # so first paint reads as the upstream order
        # (most-recent-trade-first) rather than an ambiguous
        # "no sort applied" state.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Toolbar present and scoped to the current list.
        assert 'class="holdings__sort"' in out
        assert 'data-holdings-sort="current"' in out
        # All five sort keys + Default render as buttons.
        for key in ("default", "ticker", "name", "tsr", "cagr", "weight"):
            assert f'data-holdings-sort-key="{key}"' in out
        # Default is pre-pressed; the others start inert.
        assert (
            'data-holdings-sort-key="default" '
            'data-holdings-sort-kind="default" '
            'aria-pressed="true"'
        ) in out

    def test_historical_section_omits_weight_button(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(
            ticker="NMS:OLD",
            is_current=False, weight=None,
            periods=[{"start": datetime(2022, 1, 1),
                      "end": datetime(2023, 1, 1)}],
        ))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Find the historical toolbar slice and assert against
        # *that*; the current section may also render a Weight
        # button which is not what this test guards.
        hist_idx = out.index('data-holdings-sort="historical"')
        hist_end = out.index('</div>', hist_idx)
        hist_toolbar = out[hist_idx:hist_end]
        for key in ("default", "ticker", "name", "tsr", "cagr"):
            assert f'data-holdings-sort-key="{key}"' in hist_toolbar
        assert 'data-holdings-sort-key="weight"' not in hist_toolbar

    def test_each_section_wraps_cards_in_holdings_list(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The script pairs each toolbar with its sibling list via
        # the matching ``data-holdings-list`` value, so the two
        # lists on the page must each carry their own scoped
        # wrapper -- a single shared container would let a "Sort
        # current by Weight" click also reorder the historical
        # rows below.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR"))
        w.add_holding(_holding(
            ticker="NMS:OLD",
            is_current=False, weight=None,
            periods=[{"start": datetime(2022, 1, 1),
                      "end": datetime(2023, 1, 1)}],
        ))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'data-holdings-list="current"' in out
        assert 'data-holdings-list="historical"' in out
        # The toolbar always sits *above* its list on the page so
        # the script's "next sibling" pairing model works without
        # extra wiring.
        assert (
            out.index('data-holdings-sort="current"')
            < out.index('data-holdings-list="current"')
        )
        assert (
            out.index('data-holdings-sort="historical"')
            < out.index('data-holdings-list="historical"')
        )

    def test_sort_script_is_embedded_in_head(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # The inline sort script ships in <head> so the toolbar is
        # interactive on the first paint; the script itself defers
        # its DOM queries to ``DOMContentLoaded`` so the lists
        # below are already parsed by the time it runs.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # A signature substring from the inline script body is
        # enough to confirm it's embedded -- the full payload is
        # verified end-to-end by the CSP-hash test below.
        assert "data-holdings-sort-key" in out
        assert "data-holdings-list" in out
        # CSP hashes the inline script source; if the renderer
        # ever drops the <script> tag without also dropping the
        # hash entry the page would refuse to load it. Both must
        # appear together.
        head_end = out.index("</head>")
        head = out[:head_end]
        assert head.count("'sha256-") >= 6  # JSON-LD + 5 IIFEs

    def test_default_button_lacks_indicator_triangle(
        self, stub_logo_lookup, chdir_tmp, freeze_today,
    ):
        # Only the directional buttons carry the
        # ``.holdings__sort-indicator`` triangle; the Default
        # button represents "no direction" so a triangle on it
        # would be misleading. Guards against a refactor that
        # accidentally folds the indicator into every button.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Slice out the Default button HTML between its opening
        # ``<button`` and its closing ``</button>`` and assert
        # the indicator span is absent from that span only.
        default_open = out.index('data-holdings-sort-key="default"')
        button_start = out.rfind('<button', 0, default_open)
        button_end = out.index('</button>', default_open)
        default_button = out[button_start:button_end]
        assert "holdings__sort-indicator" not in default_button


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
