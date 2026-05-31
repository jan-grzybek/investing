"""Bar / chart rendering primitives, embedded JS payloads,
pointer interaction styles, and the end-to-end ``save()`` flow."""
from __future__ import annotations

import math
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import update
from update import Webpage, LOGOS_ADDRESS

from tests._webpage_support import (
    _holding,
    _total_return,
    _benchmark,
    stub_logo_lookup,
)


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
