"""Per-section rendering: returns + benchmarks capsule, holding
cards, the marquee ticker, the trades table, and the
per-section sort control."""

from __future__ import annotations

import math
import re
from datetime import datetime
from unittest.mock import MagicMock

from investing.paths import LOGOS_ADDRESS
from investing.webpage import Webpage
from tests._webpage_support import (
    _benchmark,
    _holding,
    _total_return,
    _trade_event,
    stub_logo_lookup,
)


class _AspectStubCache:
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
        from investing.logos import _DEFAULT_LOGO_ASPECT

        return self._aspects.get(ticker, _DEFAULT_LOGO_ASPECT)


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
        self,
        stub_logo_lookup,
        freeze_today,
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
        assert '<time datetime="2024-01-01">Jan 1, 2024</time>' in w.return_html
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
        w.add_return(
            tr,
            [
                _benchmark()
                | {
                    "history": [
                        (datetime(2024, 1, 1), 1.0),
                        (datetime(2024, 6, 1), 1.05),
                        (datetime(2024, 12, 1), 1.1),
                    ]
                }
            ],
        )
        # The chart's caption owns the period and wraps the date
        # as a machine-readable <time> element. This caption reads
        # as prose ("Since Jan 1, 2024 . X months"), so it carries
        # the long-form ``%b %-d, %Y`` label from
        # ``_fmt_date_long`` -- the slash-separated DD/MM/YYYY
        # format used everywhere else on the page would break the
        # sentence rhythm. ISO ``datetime`` attribute stays in
        # W3C YYYY-MM-DD.
        assert '<time datetime="2024-01-01">Jan 1, 2024</time>' in w.return_html
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
            line
            for line in w.return_html.split("<")
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
        self,
        stub_logo_lookup,
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
        self,
        stub_logo_lookup,
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
        assert ('class="returns-compare__delta-sep" aria-hidden="true"') in out
        # The narrow-viewport stack rule lives in its own breakpoint.
        # We bumped the threshold from 480px to 540px when the label
        # grew from "TR" to "Total Return" so the stack kicks in
        # before the row gets visually cramped.
        from tests._css_helpers import contains_at_rule

        full_html = w._head() + out  # styles live in <head>
        assert contains_at_rule(full_html, "@media (max-width: 540px)")

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
        w.add_return(
            tr,
            [
                _benchmark()
                | {
                    "history": [
                        (datetime(2024, 1, 1), 1.0),
                        (datetime(2024, 6, 1), 1.05),
                        (datetime(2024, 12, 1), 1.1),
                    ]
                }
            ],
        )
        chart_idx = w.return_html.index('class="return-chart"')
        compare_idx = w.return_html.index('class="returns-compare"')
        assert chart_idx < compare_idx

    def test_intro_paragraph_precedes_chart_and_comparison(
        self,
        stub_logo_lookup,
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
        w.add_return(
            tr,
            [
                _benchmark()
                | {
                    "history": [
                        (datetime(2024, 1, 1), 1.0),
                        (datetime(2024, 6, 1), 1.05),
                        (datetime(2024, 12, 1), 1.1),
                    ]
                }
            ],
        )
        assert 'class="section__intro"' in w.return_html
        intro_idx = w.return_html.index('class="section__intro"')
        chart_idx = w.return_html.index('class="return-chart"')
        compare_idx = w.return_html.index('class="returns-compare"')
        assert intro_idx < chart_idx < compare_idx
        # Benchmark name (escaped) is woven into the prose.
        assert (
            "Cumulative return of the portfolio tracked against the S&amp;P 500." in w.return_html
        )

    def test_intro_paragraph_omits_benchmark_when_none_configured(
        self,
        stub_logo_lookup,
    ):
        # No benchmark -> the comparison block renders the portfolio
        # column on its own, and the intro phrasing follows suit so we
        # don't dangle a "vs the S&P 500" reference with nothing to
        # compare against.
        w = Webpage()
        w.add_return(_total_return(), [])
        assert 'class="section__intro"' in w.return_html
        assert '<p class="section__intro">Cumulative return of the portfolio.</p>' in w.return_html
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

    def test_return_and_irr_under_100_render_decimal_without_wrapper(
        self,
        stub_logo_lookup,
    ):
        # Under the 100 threshold every viewport keeps the ``.X``
        # decimal -- the integer part is at most two digits, so the
        # extra precision sits naturally next to it without needing
        # a CSS-hidable wrapper. The renderer emits plain text in
        # this branch so the ``.holding__decimal`` span never
        # appears for two-digit returns.
        w = Webpage()
        w.add_holding(_holding(tsr=12.3, cagr=4.5))
        card = w.current[0]
        assert ">12.3%<" in card
        assert ">4.5%<" in card
        assert "holding__decimal" not in card

    def test_return_and_irr_at_or_above_100_wrap_decimal_for_mobile_hide(
        self,
        stub_logo_lookup,
    ):
        # On wider viewports the Return / IRR rows stack vertically
        # under their labels, so the renderer keeps the ``.X``
        # decimal even when the integer part has reached three
        # figures (``217.4%`` reads as a single number in the
        # stats column). On narrower viewports the same metrics
        # reflow into a horizontal 3-column row alongside Weight,
        # where the trailing decimal would crowd the layout. The
        # capsule emits both shapes from one DOM node: the integer
        # part sits in the ``<dd>`` text and the ``.X`` tail is
        # wrapped in ``<span class="holding__decimal">`` so the
        # mobile media query can drop it via ``display: none``.
        w = Webpage()
        w.add_holding(_holding(tsr=217.4, cagr=143.7))
        card = w.current[0]
        # Three-digit integers carry the wrapper for both Return
        # and IRR.
        assert '217<span class="holding__decimal">.4</span>%' in card
        assert '143<span class="holding__decimal">.7</span>%' in card

    def test_negative_return_at_or_above_100_still_wraps_the_decimal(
        self,
        stub_logo_lookup,
    ):
        # Sign-aware threshold: a -123.4% drawdown is just as
        # cramped horizontally as a +123.4% gain, so the wrapper
        # follows the magnitude of the value rather than the sign.
        # The leading minus stays in the integer part of the
        # ``<dd>`` text so the visible value reads continuously
        # on every viewport.
        w = Webpage()
        w.add_holding(_holding(tsr=-123.4, cagr=-101.0))
        card = w.current[0]
        assert '-123<span class="holding__decimal">.4</span>%' in card
        assert '-101<span class="holding__decimal">.0</span>%' in card
        # Negative-magnitude wrapping still triggers the colour
        # class -- the helper only changes how the digits render.
        assert "value--negative" in card

    def test_weight_uses_plain_pct_formatter_without_decimal_wrapper(
        self,
        stub_logo_lookup,
    ):
        # Weight stays on the plain ``_fmt_pct`` formatter -- the
        # responsive ``.holding__decimal`` wrapper only applies to
        # Return / IRR, where 3-digit magnitudes are realistic
        # enough to need the desktop ``.X`` precision. Portfolio
        # weights are always under 100 by construction (no single
        # holding can exceed the whole portfolio), so the wrapper
        # would be pointless markup churn there. Pin the
        # serialisation so a future widening of the helper to
        # Weight has to update this test deliberately.
        w = Webpage()
        w.add_holding(_holding(weight=10.0))
        card = w.current[0]
        # Weight rendering is unchanged from the pre-responsive
        # behaviour.
        assert ">10.0%<" in card

    def test_holding_decimal_is_hidden_under_the_mobile_breakpoint(
        self,
        stub_logo_lookup,
    ):
        # The decimal wrapper is rendered unconditionally on the
        # holding capsule once Return / IRR reach 100; the only
        # thing that switches it on / off is a ``display: none``
        # rule inside the ``@media (max-width: 540px)`` block. The
        # 540px breakpoint is the same one that reflows the stats
        # grid from the desktop vertical stack into the mobile
        # horizontal row, so the wrapper hides exactly when the
        # row geometry stops having room for the ``.X`` tail.
        from tests._css_helpers import at_rule_bodies, has_declaration, normalize

        w = Webpage()
        w.add_holding(_holding(tsr=217.4, cagr=143.7))
        full_html = w._head()
        mobile_bodies = at_rule_bodies(full_html, "@media (max-width: 540px)")
        assert mobile_bodies, "@media (max-width: 540px) missing"
        # Any of the (possibly several) mobile blocks may carry
        # the hide rule -- the union is what matters.
        assert any(
            has_declaration(body, "display", "none")
            and ".holding__decimal" in body
            for body in mobile_bodies
        )
        # And no global ``.holding__decimal{display:none}`` lives
        # at the top level -- the desktop default must keep the
        # wrapper visible so the ``.X`` precision shows on the
        # vertical stats stack. ``contains_selector`` looks at the
        # whole stylesheet; we strip the bodies of every mobile
        # block before checking to leave only desktop-scope
        # declarations.
        css = normalize(full_html)
        for body in mobile_bodies:
            css = css.replace(body, "")
        assert ".holding__decimal{display:none}" not in css

    def test_period_dates_are_wrapped_in_time_elements(self, stub_logo_lookup):
        # Wrapping each rendered date in <time datetime="..."> makes
        # the holding period machine-readable for crawlers and screen
        # readers without altering the human-facing label. The label
        # uses the page-wide DD/MM/YYYY convention; the ISO
        # ``datetime`` attribute keeps the W3C YYYY-MM-DD format --
        # two conventions serving two different audiences.
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:CLO",
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 11, 4), "end": datetime(2024, 4, 12)},
                ],
            )
        )
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
        w.add_holding(
            _holding(
                periods=[
                    {"start": datetime(2024, 1, 1), "end": None},
                ]
            )
        )
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
        w.add_holding(
            _holding(
                ticker="NMS:GRID",
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 8, 5), "end": datetime(2023, 6, 9)},
                    {"start": datetime(2024, 1, 22), "end": datetime(2024, 11, 30)},
                ],
            )
        )
        card = w.historical[0]
        # Newest-first (per the defensive sort in _build_card).
        expected_top = (
            "<li>"
            '<time datetime="2024-01-22">22/01/2024</time>'
            "<span>-</span>"
            '<time datetime="2024-11-30">30/11/2024</time>'
            "</li>"
        )
        expected_bottom = (
            "<li>"
            '<time datetime="2022-08-05">05/08/2022</time>'
            "<span>-</span>"
            '<time datetime="2023-06-09">09/06/2023</time>'
            "</li>"
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
        from tests._css_helpers import blocks_for, has_declaration

        full_html = w._head() + card
        # The 3-track grid template is declared on ``.holding__periods``
        # itself, and each ``li`` collapses via ``display: contents`` so
        # its three children land directly in those tracks. ``justify-
        # content: start`` keeps the grid hugging the body's left edge;
        # without it, leftover horizontal space gets distributed
        # between the tracks and opens visible gaps on wide viewports.
        ul_bodies = blocks_for(full_html, ".holding__periods")
        assert ul_bodies, ".holding__periods rule missing"
        assert any(
            has_declaration(
                b,
                "grid-template-columns",
                "max-content min-content max-content",
            )
            for b in ul_bodies
        )
        assert any(has_declaration(b, "justify-content", "start") for b in ul_bodies)
        li_bodies = blocks_for(full_html, ".holding__periods li")
        assert li_bodies, ".holding__periods li rule missing"
        assert any(has_declaration(b, "display", "contents") for b in li_bodies)
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
        from tests._css_helpers import normalize as _normalize_css

        last_child_bodies = blocks_for(full_html, ".holding__periods li>:last-child")
        assert last_child_bodies
        assert any(has_declaration(b, "text-align", "end") for b in last_child_bodies)
        span_last_bodies = blocks_for(
            full_html,
            ".holding__periods li>span:last-child",
        )
        assert span_last_bodies
        assert any(has_declaration(b, "text-align", "start") for b in span_last_bodies)
        assert not blocks_for(full_html, ".holding__periods li>:first-child")
        assert "holding__period--open" not in _normalize_css(full_html)
        # Sanity guards against the prior fixed-width variants
        # ("Present" desktop layout looked off because the start
        # date's variable trailing slack created asymmetric gaps
        # around the dash) and the prior loose 7em sizing.
        assert "grid-template-columns: 6.5em" not in full_html
        assert "grid-template-columns: 7em" not in full_html
        assert "6.5em auto 6.5em" not in full_html

    def test_multiple_periods_stack_newest_first_as_list(
        self,
        stub_logo_lookup,
    ):
        # The visual order (newest period on top) is a UX guarantee
        # that ``_build_card`` enforces internally via ``sorted(...,
        # reverse=True)`` -- regardless of the order the caller hands
        # the periods over in. Pass them in *oldest-first* on purpose
        # to prove the render is order-agnostic.
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:MULTI",
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 1, 5), "end": datetime(2023, 3, 9)},
                    {"start": datetime(2024, 6, 1), "end": datetime(2025, 2, 1)},
                ],
            )
        )
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
        self,
        stub_logo_lookup,
    ):
        # An open position (end is None) is by definition the most
        # recent ownership window, so it must land at the top of the
        # stack even when older closed periods sit alongside it.
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:OPEN",
                periods=[
                    {"start": datetime(2020, 5, 1), "end": datetime(2021, 8, 1)},
                    {"start": datetime(2024, 9, 1), "end": None},
                ],
            )
        )
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
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        card = w.historical[0]
        assert ' id="holding-NMS-OLD"' in card

    def test_current_holding_card_carries_sort_attributes(
        self,
        stub_logo_lookup,
    ):
        # The sort toolbar above each holdings list re-orders cards
        # by reading ``data-sort-*`` attributes on each
        # ``<article class="holding">``. Sanity-check the contract
        # so the toolbar (which has no Python visibility into
        # the values) lines up with what the renderer emits.
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:NVDA",
                name="NVIDIA Corporation",
                tsr=217.4,
                cagr=64.2,
                weight=21.4,
            )
        )
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
        self,
        stub_logo_lookup,
    ):
        # Historical positions have no ``current_weight%`` so the
        # card MUST NOT advertise a weight sort key -- the
        # historical toolbar omits the matching button, but a
        # stray ``data-sort-weight`` would still leak the
        # attribute into the DOM (and a sort-by-weight applied
        # to the *current* list could resort historical rows
        # if the JS ever queried by selector globally).
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                name="Old Co.",
                is_current=False,
                weight=None,
                tsr=-12.5,
                cagr=-7.3,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        card = w.historical[0]
        assert 'data-sort-ticker="old"' in card
        assert 'data-sort-name="old co."' in card
        assert 'data-sort-tsr="-12.5000"' in card
        assert 'data-sort-cagr="-7.3000"' in card
        assert "data-sort-weight" not in card

    def test_holding_title_keeps_exchange_prefix_for_display(
        self,
        stub_logo_lookup,
    ):
        # The visible title still reads as ``EXCHANGE:SYMBOL -
        # Company`` so the row stays unambiguous; only the
        # *sort key* drops the prefix. Guards against an
        # accidental refactor that lower-cases the displayed
        # ticker too.
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:NVDA",
                name="NVIDIA Corporation",
            )
        )
        card = w.current[0]
        assert "NMS:NVDA - NVIDIA Corporation" in card


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


class TestAddTrades:
    def test_renders_one_row_per_event(self, stub_logo_lookup):
        w = Webpage()
        w.add_trades(
            [
                _trade_event(ticker="NMS:AAA", category="OPEN"),
                _trade_event(ticker="NMS:BBB", category="CLOSE", start=datetime(2024, 5, 1)),
            ]
        )
        assert len(w.trades) == 2
        # Each event materialises as a single ``<tr class="trades__row">``;
        # the surrounding ``<table>`` chrome is added by the section
        # builder in ``save()``.
        assert all('class="trades__row"' in row for row in w.trades)
        assert all(row.startswith("<tr ") for row in w.trades)

    def test_no_trades_means_no_rows(self, stub_logo_lookup):
        w = Webpage()
        w.add_trades([])
        assert w.trades == []

    def test_action_collapses_categories_to_bought_or_sold(
        self,
        stub_logo_lookup,
    ):
        # The "Action" column collapses the four-category space onto
        # a single buy-vs-sell axis: OPEN / INCREASE -> "Bought"
        # (green), DECREASE / CLOSE -> "Sold" (red). Direction is
        # the only thing a glance at this column needs to convey;
        # the lifecycle distinction (was this the first fill or a
        # top-up? did this SELL close the position?) lives in the
        # adjacent "Details" column instead.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(category="OPEN"),
                _trade_event(category="INCREASE", delta_pct=30.0),
                _trade_event(category="DECREASE", delta_pct=25.0),
                _trade_event(category="CLOSE"),
            ]
        )
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
        from investing.assets import _PAGE_STYLES
        from tests._css_helpers import blocks_for, has_declaration

        # Every ``.trade__badge`` declaration block that touches sizing
        # (base rule + any surviving per-breakpoint override) must pin
        # the pill to ``width: 7em``. The base rule additionally
        # centres the label; the 540px override doesn't need to repeat
        # that since it inherits ``text-align`` from the base. Colour-
        # only overrides (e.g. the dark-mode pill text flip) don't
        # restate width and so don't need to repeat ``width: 7em`` --
        # we only enforce the rule on blocks that already declare a
        # ``width``. ``has_declaration`` normalises whitespace so the
        # checks work whether the served CSS is formatted (dev) or
        # minified (prod). ``min-width`` is explicitly excluded from
        # every block to prevent the "longer label grows the pill"
        # regression from creeping back in.
        bodies = blocks_for(_PAGE_STYLES, ".trade__badge")
        assert len(bodies) >= 2  # base + at least the 540px override
        assert has_declaration(bodies[0], "text-align", "center")
        sizing_bodies = [body for body in bodies if "width:" in body]
        assert len(sizing_bodies) >= 2  # base + 540px both restate width
        for body in sizing_bodies:
            assert has_declaration(body, "width", "7em")
        for body in bodies:
            assert "min-width" not in body

    def test_details_column_uses_past_tense_initiated_and_divested(
        self,
        stub_logo_lookup,
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
        w.add_trades(
            [
                _trade_event(category="OPEN", delta_pct=None),
                _trade_event(category="INCREASE", delta_pct=30.0),
                _trade_event(category="DECREASE", delta_pct=25.0),
                _trade_event(category="CLOSE", delta_pct=None),
            ]
        )
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
        self,
        stub_logo_lookup,
    ):
        # The Date column shows calendar quarters instead of
        # to-the-day stamps. A burst that lives inside a single
        # quarter renders the bare quarter label ("Q1 2025") with
        # no separator -- the trade happened in Q1 2025, full stop.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(start=datetime(2025, 1, 14)),
            ]
        )
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
        self,
        stub_logo_lookup,
    ):
        # A burst that's spread over several days but doesn't cross
        # a quarter boundary still renders a single quarter label --
        # the page commits to quarter granularity regardless of how
        # many days the underlying fills span.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(
                    start=datetime(2024, 5, 22),
                    end=datetime(2024, 6, 11),
                ),
            ]
        )
        row = w.trades[0]
        # Both 22 May 2024 and 11 Jun 2024 sit in Q2 2024.
        assert '<time datetime="2024-04">Q2 2024</time>' in row
        # No to-the-day stamps and no separator since it's a
        # single quarter.
        assert "22/05/2024" not in row
        assert "11/06/2024" not in row
        assert "trades__date-sep" not in row

    def test_burst_spanning_two_quarters_same_year_uses_slash(
        self,
        stub_logo_lookup,
    ):
        # A burst that crosses a quarter boundary inside one
        # calendar year renders a single slash-joined label
        # ("Q3/Q4 2024") -- the rolling-quarter aggregation window
        # makes this the only realistic multi-quarter case inside
        # a single year, so a slash reads naturally as "spans
        # these two".
        w = Webpage()
        w.add_trades(
            [
                _trade_event(
                    start=datetime(2024, 9, 20),  # Q3 2024
                    end=datetime(2024, 10, 5),  # Q4 2024
                ),
            ]
        )
        row = w.trades[0]
        assert '<time datetime="2024-07">Q3/Q4 2024</time>' in row
        # Slash format collapses to one ``<time>`` element, no
        # trailing separator span.
        assert "trades__date-sep" not in row

    def test_burst_crossing_year_boundary_uses_hyphen_separator(
        self,
        stub_logo_lookup,
    ):
        # A burst that crosses a calendar-year boundary (Q4 of one
        # year into Q1 of the next) renders as a hyphen-separated
        # range with two full ``<time>`` elements, since the
        # slash-joined "Q4/Q1 2026" form would be ambiguous about
        # which year owns the Q1. The separator span mirrors what
        # the equity capsules use for multi-period dates so the
        # eye can scan ranges across both surfaces.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(
                    start=datetime(2024, 12, 15),  # Q4 2024
                    end=datetime(2025, 1, 20),  # Q1 2025
                ),
            ]
        )
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
        w.add_trades(
            [
                _trade_event(price=247.85, currency="USD"),
                _trade_event(price=181.25, currency="EUR"),
            ]
        )
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
        w.add_trades(
            [
                _trade_event(price=4820.50, currency="GBp"),
            ]
        )
        assert ">4,820.50 GBp<" in w.trades[0]

    def test_details_pct_renders_as_whole_number(self, stub_logo_lookup):
        # Whole-number percentages by design in this section: the
        # one-decimal page convention from ``_fmt_pct`` is reserved
        # for the performance rows where that extra digit is
        # meaningful. For position-change magnitudes a 4% vs 4.3%
        # split is below the noise floor of how we report sizes.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(category="INCREASE", delta_pct=30.0),
                _trade_event(category="INCREASE", delta_pct=100.0),
                _trade_event(category="DECREASE", delta_pct=99.5),
                _trade_event(category="INCREASE", delta_pct=42.4),
            ]
        )
        assert ">+30%<" in w.trades[0]
        assert ">+100%<" in w.trades[1]
        # 99.5 rounds up to 100; 42.4 rounds down to 42 -- standard
        # banker's-rounding-adjacent ``{:.0f}`` behaviour, which is
        # close enough to "round half to even" that the rendering
        # convention is uncontroversial for the values that show up
        # in practice. The minus sign is U+2212.
        assert ">\u2212100%<" in w.trades[2]
        assert ">+42%<" in w.trades[3]

    def test_table_has_no_logo_cell(self, stub_logo_lookup):
        # Logos were removed from the trades table -- the ticker
        # column is the row's anchor now and a 32px glyph stacked
        # to the left of every row added visual noise without
        # carrying information the ticker / company columns
        # didn't already convey. Both the rendered ``<tr>`` and
        # the surrounding ``<table>`` chrome must be logo-free.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(ticker="NMS:NVDA", name="NVIDIA Corporation"),
            ]
        )
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
        w.add_trades(
            [
                _trade_event(ticker="NMS:NVDA", name="NVIDIA Corporation"),
            ]
        )
        row = w.trades[0]
        assert ">NVDA<" in row
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
        self,
        stub_logo_lookup,
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
        w.add_trades(
            [
                _trade_event(
                    ticker="NMS:NVDA",
                    name="NVIDIA Corporation",
                    category="INCREASE",
                    start=datetime(2024, 5, 22),
                    end=datetime(2024, 6, 11),
                    delta_pct=30.0,
                ),
            ]
        )
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
        w.add_trades(
            [
                _trade_event(category="OPEN"),
                _trade_event(category="INCREASE", delta_pct=10.0),
                _trade_event(category="DECREASE", delta_pct=10.0),
                _trade_event(category="CLOSE"),
            ]
        )
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
        from investing.assets import _TRADES_SORT_SCRIPT

        assert "DOMContentLoaded" in _TRADES_SORT_SCRIPT
        assert "document.readyState==='loading'" in _TRADES_SORT_SCRIPT

    def test_short_log_does_not_render_show_all_toggle(
        self,
        stub_logo_lookup,
    ):
        # When the trade log already fits in the default visible
        # window (``_TRADES_VISIBLE_DEFAULT``) there's nothing to
        # collapse, so no toggle chrome should appear -- the page
        # stays clean and no inert button confuses a screen-reader
        # user. The threshold lives on the ``Webpage`` class so this
        # test reads it rather than hard-coding 10.
        from investing.webpage import Webpage as _Webpage

        threshold = _Webpage._TRADES_VISIBLE_DEFAULT
        w = Webpage()
        w.add_trades([_trade_event(start=datetime(2025, 1, i + 1)) for i in range(threshold)])
        w.add_return(_total_return(), [])
        # Build the table HTML directly so we exercise the toggle
        # decision without having to call ``save()``.
        table_html = _Webpage._build_trades_table(w.trades)
        assert "trades__toggle" not in table_html
        # The hide-overflow CSS rule still ships unconditionally (a
        # short log just never trips it), so its presence isn't
        # part of the contract this test enforces.

    def test_long_log_renders_show_all_toggle_with_total_count(
        self,
        stub_logo_lookup,
    ):
        # Once the log exceeds the threshold the renderer emits a
        # ``<button class="trades__toggle">`` after the table whose
        # text labels the full count and whose ``data-total``
        # attribute lets the inline script rebuild the label each
        # time the user collapses the section. ``aria-expanded`` /
        # ``data-expanded`` start out closed so the page paints in
        # the collapsed state without the JS having to "fix it up"
        # post-DOMContentLoaded.
        from investing.webpage import Webpage as _Webpage

        threshold = _Webpage._TRADES_VISIBLE_DEFAULT
        w = Webpage()
        w.add_trades([_trade_event(start=datetime(2025, 1, i + 1)) for i in range(threshold + 5)])
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
        from investing.assets import _PAGE_STYLES
        from investing.webpage import Webpage as _Webpage
        from tests._css_helpers import contains_selector

        threshold = _Webpage._TRADES_VISIBLE_DEFAULT
        # Threshold + 1 is the first row hidden, which matches the
        # ``:nth-of-type(n+11)`` index in the stylesheet rule.
        # ``contains_selector`` normalises whitespace so the assertion
        # works against both formatted and minified CSS.
        rule = (
            f'.trades:not([data-expanded="true"]) tbody '
            f"tr.trades__row:nth-of-type(n+{threshold + 1})"
        )
        assert contains_selector(_PAGE_STYLES, rule)

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
        from investing.assets import _TRADES_SORT_SCRIPT

        for needle in (
            ".trades__toggle",
            "data-expanded",
            "aria-expanded",
            "Show fewer trades",
            "Show all ",
        ):
            assert needle in _TRADES_SORT_SCRIPT

    def test_wrap_is_a_named_inline_size_container(self):
        # ``.trades__wrap`` is declared as a ``container-type:
        # inline-size`` query container with the name ``trades`` so
        # the matching ``@container trades (max-width: ...)`` rules
        # below can hide the Company and Action columns against the
        # wrap's actual rendered width (not the viewport). The two
        # CSS declarations are the load-bearing prerequisite for the
        # rest of the responsive column-hiding contract -- without
        # them the ``@container`` rules below would never match and
        # both columns would stay visible at every viewport down to
        # the wrapper-scroll fallback. Assert they ship in the
        # served stylesheet.
        import re

        from investing.assets import _PAGE_STYLES
        from tests._css_helpers import contains_at_rule

        # The CSS compressor strips the space after ``:`` in
        # declarations, so match either spacing on the type / name
        # pair.
        assert re.search(r"container-type:\s*inline-size", _PAGE_STYLES)
        assert re.search(r"container-name:\s*trades", _PAGE_STYLES)
        # And the two ``@container`` rules that actually drive the
        # responsive hiding. Both target the named ``trades``
        # container and collapse the appropriate ``<th>`` / ``<td>``
        # cells via ``display: none``.
        assert contains_at_rule(_PAGE_STYLES, "@container trades (max-width: 600px)")
        assert contains_at_rule(_PAGE_STYLES, "@container trades (max-width: 430px)")

    def test_container_query_thresholds_are_well_separated(self):
        # The Company / Action drop order is intentional (Company
        # first, since the ticker still uniquely identifies the
        # security; Action second, since BUY / SELL is redundantly
        # encoded by the Details column). Just as importantly, the
        # two thresholds sit far enough apart that a continuous
        # resize through the boundary produces two clearly separated
        # visual transitions rather than dropping both columns in
        # lockstep at one viewport change. Lock that property by
        # checking the threshold gap stays at least ~150px (the
        # current design ships 170px of headroom between Company at
        # 600px and Action at 430px). A future tweak that pushes
        # them within ~100px of each other would risk reintroducing
        # the perceived simultaneous-hide bug this test exists to
        # prevent.
        import re

        from investing.assets import _PAGE_STYLES

        thresholds = [
            int(m.group(1))
            for m in re.finditer(
                r"@container\s+trades\s*\(max-width:\s*(\d+)px\)", _PAGE_STYLES
            )
        ]
        assert len(thresholds) >= 2, thresholds
        thresholds.sort(reverse=True)
        # First (widest) hides Company, second hides Action.
        assert thresholds[0] - thresholds[1] >= 150, thresholds

    def test_name_and_currency_are_html_escaped(self, stub_logo_lookup):
        # Even though tickers/names are sourced from a trusted sheet,
        # we still escape so an "&" or "<" in a security name can't
        # break the rendered HTML.
        w = Webpage()
        w.add_trades(
            [
                _trade_event(name="S&P Global Inc."),
            ]
        )
        row = w.trades[0]
        assert "S&amp;P Global Inc." in row
        # No raw ``&P`` leaks.
        assert "S&P Global" not in row


class TestSaveTradesSection:
    def test_save_emits_trades_section_when_present(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_trades(
            [
                _trade_event(ticker="NMS:AAA", category="OPEN", start=datetime(2024, 1, 1)),
            ]
        )
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # When the page carries both the historical holdings section
        # and the trades section, trades appears last so the page
        # reads as: performance -> current -> historical -> activity.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.add_trades([_trade_event(start=datetime(2024, 1, 1))])
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        idx_hist = out.index('id="historical"')
        idx_trades = out.index('id="trades"')
        assert idx_hist < idx_trades


class TestHoldingsSortControl:
    def test_current_section_renders_full_sort_button_set(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
            'data-holdings-sort-key="default" data-holdings-sort-kind="default" aria-pressed="true"'
        ) in out

    def test_historical_section_omits_weight_button(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Find the historical toolbar slice and assert against
        # *that*; the current section may also render a Weight
        # button which is not what this test guards.
        hist_idx = out.index('data-holdings-sort="historical"')
        hist_end = out.index("</div>", hist_idx)
        hist_toolbar = out[hist_idx:hist_end]
        for key in ("default", "ticker", "name", "tsr", "cagr"):
            assert f'data-holdings-sort-key="{key}"' in hist_toolbar
        assert 'data-holdings-sort-key="weight"' not in hist_toolbar

    def test_each_section_wraps_cards_in_holdings_list(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'data-holdings-list="current"' in out
        assert 'data-holdings-list="historical"' in out
        # The toolbar always sits *above* its list on the page so
        # the script's "next sibling" pairing model works without
        # extra wiring.
        assert out.index('data-holdings-sort="current"') < out.index('data-holdings-list="current"')
        assert out.index('data-holdings-sort="historical"') < out.index(
            'data-holdings-list="historical"'
        )

    def test_sort_script_is_embedded_in_head(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        button_start = out.rfind("<button", 0, default_open)
        button_end = out.index("</button>", default_open)
        default_button = out[button_start:button_end]
        assert "holdings__sort-indicator" not in default_button


class TestEquitySectorTreemap:
    """Sector treemap lives inside the Equities sub-section and
    visualises the current equity holdings only. Cash and historical
    positions never reach the renderer; ``add_holding`` filters the
    payload list down to current rows before the treemap is built."""

    def test_treemap_renders_with_current_equity_holdings(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Two current equities + one historical row. Only the
        # current ones should produce tiles.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:AAA", name="Alpha", weight=60.0, sector="Technology")
        )
        w.add_holding(
            _holding(ticker="NMS:BBB", name="Beta", weight=40.0, sector="Healthcare")
        )
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                name="Old Co.",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Container + per-sector tiles + legend chips are all
        # emitted.
        assert '<figure class="treemap"' in out
        assert 'class="treemap__canvas"' in out
        assert out.count('class="treemap__tile"') == 2
        assert 'data-sector="Technology"' in out
        assert 'data-sector="Healthcare"' in out
        # The historical-only ticker never receives a tile because
        # ``add_holding`` filters on ``is_current`` before pushing
        # onto the treemap list, and an absent ``current_weight%``
        # would still be rejected by the renderer's weight guard.
        assert 'href="#holding-NMS-OLD"' not in out[out.index('<figure class="treemap"'):out.index("</figure>", out.index('<figure class="treemap"'))]

    def test_treemap_omitted_when_no_current_equities(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # A cash-only / historical-only portfolio has no tiles to
        # plot, so the renderer should return an empty string and
        # nothing belonging to the treemap (figure, canvas, legend)
        # should appear at all.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert '<figure class="treemap"' not in out
        assert 'class="treemap__canvas"' not in out
        assert 'class="treemap__legend"' not in out

    def test_treemap_tile_links_to_matching_holding_card(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Each tile is an ``<a href="#holding-...">`` pointing at
        # the same capsule anchor the marquee and bar chart use, so
        # clicking a coloured rectangle scrolls the reader to the
        # full holding row below.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:AAA", name="Alpha", weight=70.0, sector="Technology")
        )
        w.add_holding(
            _holding(ticker="NMS:BBB", name="Beta", weight=30.0, sector="Technology")
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        treemap_block = out[
            out.index('<figure class="treemap"'):
            out.index("</figure>", out.index('<figure class="treemap"'))
        ]
        assert 'href="#holding-NMS-AAA"' in treemap_block
        assert 'href="#holding-NMS-BBB"' in treemap_block
        # Both tiles share the same sector swatch CSS variable so
        # they read as a contiguous block of colour in the chart;
        # the legend chip below also references that variable, for
        # a total of three occurrences inside the figure.
        assert treemap_block.count("--treemap-color-tech") == 3

    def test_missing_sector_falls_back_to_other_bucket(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # yfinance occasionally returns an empty ``sector`` string
        # for exotic instruments; those tickers should land in a
        # stable "Other" bucket rather than producing their own
        # one-off tile colours.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:AAA", name="Alpha", weight=50.0, sector="")
        )
        w.add_holding(
            _holding(ticker="NMS:BBB", name="Beta", weight=50.0, sector="Technology")
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'data-sector="Other"' in out
        # The fallback colour variable is wired in too.
        assert "--treemap-color-other" in out

    def test_tiles_carry_accessible_label_and_tooltip(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # ``aria-label`` carries the ``ticker - name (sector):
        # weight%`` summary so screen readers announce the full
        # context for each rectangle; ``title`` mirrors that for a
        # sighted-mouse hover tooltip.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(
                ticker="NMS:AAA",
                name="Alpha Inc.",
                weight=100.0,
                sector="Technology",
            )
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'aria-label="NMS:AAA - Alpha Inc. (Technology): 100%"' in out
        assert 'title="NMS:AAA - Alpha Inc. (Technology): 100%"' in out

    def test_tiles_emit_both_logo_and_ticker_for_css_swap(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Design contract: every non-aggregated tile emits *both* a
        # ``<img class="treemap__tile-logo">`` and a
        # ``<span class="treemap__tile-ticker">``. Which one is
        # visible is decided by a per-tile CSS container size query
        # (see ``page.css``); the renderer no longer commits to one
        # answer per tile. That's what makes the swap responsive to
        # viewport changes -- a tile that's too small for the logo
        # on mobile shows the ticker symbol, the same tile on a wide
        # desktop shows the logo, and the transition happens live
        # without re-rendering the HTML.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:AAA", weight=60.0, sector="Technology"),
        )
        w.add_holding(
            _holding(ticker="NMS:BBB", weight=40.0, sector="Technology"),
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        fig_start = out.index('<figure class="treemap"')
        fig_end = out.index("</figure>", fig_start)
        block = out[fig_start:fig_end]
        # Two real holdings -> two logos AND two visible ticker spans.
        assert block.count('<img class="treemap__tile-logo"') == 2
        assert block.count('class="treemap__tile-ticker"') == 2
        # The ticker labels are emitted as visible text content (not
        # only as ``aria-label`` attribute payloads -- the ``>AAA<``
        # / ``>BBB<`` boundary check looks at the text inside the
        # ticker span, which is what the CSS swap toggles).
        assert ">AAA<" in block
        assert ">BBB<" in block
        # Pre-equal-area per-tile size variables stay absent
        # (sizing is CSS clamp-driven, with per-logo factors set on
        # the ``<img>`` style).
        assert "--logo-w:" not in block
        assert "--logo-h:" not in block

    def test_tile_img_carries_equal_area_logo_factors(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Equal-area sizing contract: each logo's ``<img>`` carries
        # ``--logo-w-factor`` and ``--logo-h-factor`` custom
        # properties whose product is approximately ``1``. The CSS
        # rules multiply those onto the base width / height clamps,
        # so a constant product preserves ``width * height``
        # (i.e. equal pixel area) across all logos regardless of
        # their intrinsic aspect ratio.
        freeze_today(datetime(2025, 6, 1))
        # Aspects spanning the wide / medium / narrow spectrum so
        # the test exercises non-trivial factor values, not just
        # the ``1.0 / 1.0`` defaults.
        aspect_table = {
            "NMS:WIDE": 5.0,
            "NMS:MED": 3.0,
            "NMS:NARROW": 1.5,
        }
        cache = _AspectStubCache(aspect_table)
        w = Webpage(logo_cache=cache)
        w.add_return(_total_return(), [])
        for ticker, _aspect in aspect_table.items():
            w.add_holding(
                _holding(
                    ticker=ticker,
                    weight=100.0 / len(aspect_table),
                    sector="Technology",
                )
            )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        fig_start = out.index('<figure class="treemap"')
        fig_end = out.index("</figure>", fig_start)
        block = out[fig_start:fig_end]

        # For each tile, parse the inline factors off the <img>
        # and check the equal-area invariant.
        for ticker in aspect_table:
            label_idx = block.index(f'aria-label="{ticker}')
            tile = block[block.rfind("<a", 0, label_idx):block.index("</a>", label_idx)]
            w_factor = float(
                re.search(r"--logo-w-factor:\s*([\d.]+)", tile).group(1)
            )
            h_factor = float(
                re.search(r"--logo-h-factor:\s*([\d.]+)", tile).group(1)
            )
            # Product of the two factors is the equal-area invariant.
            # Allow a small slack for the 3-decimal rounding the
            # renderer emits.
            assert abs(w_factor * h_factor - 1.0) < 0.01, (
                f"{ticker}: w_factor={w_factor} h_factor={h_factor} "
                f"product={w_factor * h_factor} expected ~1.0"
            )
        # The wide aspect should produce a w_factor > 1 and an
        # h_factor < 1 (wider but shorter than the base box); the
        # narrow aspect should produce the inverse.
        wide_tile_idx = block.index('aria-label="NMS:WIDE')
        narrow_tile_idx = block.index('aria-label="NMS:NARROW')
        wide_tile = block[block.rfind("<a", 0, wide_tile_idx):block.index("</a>", wide_tile_idx)]
        narrow_tile = block[block.rfind("<a", 0, narrow_tile_idx):block.index("</a>", narrow_tile_idx)]
        wide_w = float(re.search(r"--logo-w-factor:\s*([\d.]+)", wide_tile).group(1))
        wide_h = float(re.search(r"--logo-h-factor:\s*([\d.]+)", wide_tile).group(1))
        narrow_w = float(re.search(r"--logo-w-factor:\s*([\d.]+)", narrow_tile).group(1))
        narrow_h = float(re.search(r"--logo-h-factor:\s*([\d.]+)", narrow_tile).group(1))
        assert wide_w > 1.0 > wide_h
        assert narrow_w < 1.0 < narrow_h

    def test_tiny_holdings_folded_into_aggregated_other_tile(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # When a portfolio has a long tail of small-weight holdings
        # whose individual tiles would be too narrow to host even
        # the ticker + percent labels, the renderer folds them into
        # a single aggregated ``Other`` tile (no logo, no link,
        # neutral grey swatch). Construct a 1 + 10 holdings portfolio
        # where the 10 tail holdings each carry 0.6 % -- well below
        # the 14 %-canvas-width readability threshold once they get
        # subdivided.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:HVY", weight=94.0, sector="Technology"),
        )
        for i in range(10):
            w.add_holding(
                _holding(
                    ticker=f"NMS:T{i:02d}",
                    weight=0.6,
                    sector="Technology",
                ),
            )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        fig_start = out.index('<figure class="treemap"')
        fig_end = out.index("</figure>", fig_start)
        block = out[fig_start:fig_end]
        # The aggregated tile renders as a ``<div>`` (no holding
        # card to anchor to), uses the Other sector class hook, and
        # carries an ``Other`` label.
        assert '<div class="treemap__tile treemap__tile--aggregated"' in block
        assert 'data-sector="Other"' in block
        # The pseudo-row has no logo and no anchor; the tooltip lists
        # the folded tickers so a hover surfaces what got combined.
        agg_start = block.index('treemap__tile--aggregated')
        agg_tile = block[block.rfind("<div", 0, agg_start):block.index("</div>", agg_start)]
        assert "<img" not in agg_tile
        assert "href=" not in agg_tile
        # At least one of the tail tickers shows up in the tooltip.
        assert "T00" in agg_tile or "T09" in agg_tile
        # The heavy tile renders normally with its logo intact.
        assert 'aria-label="NMS:HVY' in block

    def test_other_tile_total_weight_matches_folded_holdings_sum(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # The aggregated tile's displayed percent has to equal the
        # sum of the folded holding weights -- otherwise the chart
        # under-/over-reports the portfolio's tail allocation. Five
        # 1 %-weight tail holdings plus a 95 %-weight head produces
        # an Other tile that should read ``5%``.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:HVY", weight=95.0, sector="Technology"),
        )
        for i in range(5):
            w.add_holding(
                _holding(
                    ticker=f"NMS:T{i}",
                    weight=1.0,
                    sector="Technology",
                ),
            )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        fig_start = out.index('<figure class="treemap"')
        fig_end = out.index("</figure>", fig_start)
        block = out[fig_start:fig_end]
        # ``Other`` tile renders with the rolled-up weight.
        agg_idx = block.index('treemap__tile--aggregated')
        # The visible weight label sits inside the tile's text span.
        agg_end = block.index("</div>", agg_idx)
        agg_segment = block[agg_idx:agg_end]
        assert ">5%<" in agg_segment or ">5.0%<" in agg_segment

    def test_no_merging_when_all_tiles_fit(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Two equal-weight holdings produce two 50%-wide tiles, both
        # comfortably above the readability threshold -- the merge
        # loop must not synthesise an Other tile in that case
        # (otherwise it'd be confusingly emitting an empty Other on
        # well-balanced portfolios).
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(ticker="NMS:AAA", weight=50.0, sector="Technology"),
        )
        w.add_holding(
            _holding(ticker="NMS:BBB", weight=50.0, sector="Technology"),
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Scope the check to the figure body -- the class name itself
        # appears once in the inlined stylesheet block (rule
        # definition), but the rendered tiles inside the figure must
        # not carry the aggregated marker.
        fig_start = out.index('<figure class="treemap"')
        fig_end = out.index("</figure>", fig_start)
        assert "treemap__tile--aggregated" not in out[fig_start:fig_end]

    def test_treemap_sits_between_equities_heading_and_sort_control(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Layout contract for the Equities sub-section: the
        # ``<h3 id="equities">`` heading leads, the treemap follows
        # as the by-sector overview (it has replaced the older
        # top-N ticker bar chart), and the holdings sort toolbar
        # + capsule list close out the section. Any future shuffle
        # has to update this guard intentionally.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_allocations(
            {"Equities": 100.0, "Cash & Cash Equivalents": 0.0},
            {"NMS:AAA": 100.0},
        )
        w.add_holding(
            _holding(ticker="NMS:AAA", weight=100.0, sector="Technology"),
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        equities_idx = out.index('id="equities" class="section__subtitle"')
        treemap_idx = out.index('<figure class="treemap"')
        sort_idx = out.index('data-holdings-sort="current"')
        assert equities_idx < treemap_idx < sort_idx
        # Defensive guard against a regression that re-introduces
        # the ticker-level equities bar chart between the heading
        # and the treemap.
        assert '<div class="bars bars--equities"' not in out
