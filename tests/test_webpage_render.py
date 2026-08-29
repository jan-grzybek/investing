"""Bar / chart rendering primitives, embedded JS payloads,
pointer interaction styles, and the end-to-end ``save()`` flow."""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime

import pytest

from investing.assets import _NAV_SCROLL_SCRIPT, _RETURN_CHART_SCRIPT
from investing.formatting import _sha256_b64
from investing.webpage import Webpage
from investing.webpage.return_chart import render as _render_chart
from tests._webpage_support import (
    _benchmark,
    _holding,
    _total_return,
    stub_logo_lookup,
)


class TestAllocationBars:
    """Two stacked part-of-a-whole bars replace the three-row bar chart
    and the 416px treemap that used to sit under it."""

    @staticmethod
    def _render(allocation=None, sectors=()):
        from investing.webpage.allocation import render

        return render(allocation, list(sectors))

    def test_returns_empty_string_with_no_data(self):
        assert self._render() == ""
        assert self._render({}, []) == ""

    def test_asset_class_bar_renders_one_segment_per_slice(self):
        out = self._render({"Equities": 78.7, "Fixed Income": 10.7})
        assert out.count('class="allocation__segment"') == 2
        assert "width: 78.70%" in out
        assert "width: 10.70%" in out

    def test_segments_keep_input_order(self):
        out = self._render({"Equities": 60.0, "Fixed Income": 40.0})
        assert out.index("60.00%") < out.index("40.00%")

    def test_zero_weight_slices_are_dropped(self):
        # A zero-width segment is invisible but still renders a legend
        # chip claiming the portfolio holds something it does not.
        out = self._render({"Equities": 100.0, "Fixed Income": 0.0})
        assert "Fixed Income" not in out

    def test_wide_segments_label_themselves(self):
        out = self._render({"Equities": 78.7, "Fixed Income": 21.3})
        assert "78.7%" in out.split("allocation__key", 1)[0]

    def test_every_segment_and_every_chip_carries_its_share(self):
        # Which of the two the reader ends up seeing is a CSS question
        # -- a container query on the segment hides a label the segment
        # is too narrow to hold, at whatever width that happens to be.
        # The renderer's job is to put the figure in both places.
        #
        # This replaced a render-time ``pct >= 6`` rule, which decided
        # in *percent* a question that is really about *pixels*: six
        # percent of an 880px bar holds a label and six percent of a
        # 340px one does not, so on a narrow page two neighbours both
        # kept labels neither could fit and the numbers collided. It
        # also left the legend half-labelled, since a chip showed its
        # share only when the bar had dropped it.
        out = self._render({"Equities": 97.0, "Fixed Income": 3.0})
        bar, _, key = out.partition("allocation__key")
        drawn = [chunk.split("<", 1)[0] for chunk in bar.split('allocation__segment-value">')[1:]]
        assert drawn == ["97.0%", "3.0%"]
        # Every chip, not just the ones the bar gave up on.
        chips = [chunk.split("<", 1)[0] for chunk in key.split('allocation__key-value">')[1:]]
        assert chips == ["97.0%", "3.0%"]

    def test_a_segment_is_its_own_container_so_labels_hide_when_they_do_not_fit(self):
        # The segment has to establish an inline-size containment
        # context for the query to have anything to ask, and it has to
        # clip: without ``overflow: hidden`` a ``nowrap`` label simply
        # spills into its neighbour's, which is how the bar came to
        # read "10.7%10.6%" with no gap between two different slices.
        from investing.assets import _PAGE_STYLES
        from tests._css_helpers import blocks_for, contains_at_rule, has_declaration

        bodies = blocks_for(_PAGE_STYLES, ".allocation__segment")
        assert bodies
        joined = " ".join(bodies).replace(" ", "")
        assert "inline-size" in joined
        assert has_declaration(bodies[0], "overflow", "hidden")
        assert contains_at_rule(_PAGE_STYLES, "@container allocation-segment (max-width: 46px)")

    def test_sector_bar_is_captioned_with_its_denominator(self):
        # The sector bar is a share of the equity sleeve, not of the
        # whole portfolio, and saying so is what keeps the two bars
        # from looking like they disagree about what 100% means.
        out = self._render(None, [("Technology", 55.4), ("Healthcare", 44.6)])
        assert "Equity sleeve by sector" in out
        assert "share of equities" in out

    def test_each_bar_renders_independently(self):
        # A cash-only portfolio has an asset-class split and no sector
        # mix; a synthetic fixture can have the reverse.
        assert "By asset class" in self._render({"Equities": 100.0})
        assert "Equity sleeve by sector" not in self._render({"Equities": 100.0})
        assert "By asset class" not in self._render(None, [("Technology", 100.0)])

    def test_sector_colours_come_from_the_shared_palette(self):
        # A reader who learned "blue-green means Technology" from the
        # treemap still knows it.
        out = self._render(None, [("Technology", 100.0)])
        assert "var(--treemap-color-tech)" in out

    def test_unknown_sector_falls_back_to_the_other_swatch(self):
        # An unbounded colour vocabulary would break the legend's
        # promise that the same hue means the same sector.
        out = self._render(None, [("Cryptozoology", 100.0)])
        assert "var(--treemap-color-other)" in out

    def test_segment_label_is_html_escaped(self):
        out = self._render({'Equities" onload="x': 50.0, "Fixed Income": 50.0})
        assert 'onload="x' not in out
        assert "&quot;" in out or "&#34;" in out


class TestRenderReturnChart:
    def test_returns_empty_when_history_too_short(self):
        out = Webpage._render_return_chart({"history": [(datetime(2024, 1, 1), 1.0)]}, [])
        assert out == ""

    def test_renders_jg_line_and_a_real_baseline(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        out = Webpage._render_return_chart({"history": history}, [])
        assert 'class="return-chart"' in out
        assert "return-chart__line--jg" in out
        # A labelled 0% baseline, not an unlabelled dashed suggestion.
        assert "return-chart__base" in out
        assert "return-chart__ref" not in out
        # Without a benchmark there is no second line and no band.
        assert "return-chart__line--bench" not in out
        assert "return-chart__band" not in out
        # The svg has a viewBox and no fixed pixel dimensions.
        assert "viewBox=" in out
        assert "<svg " in out and 'width="' not in out.split("<svg ", 1)[1].split(">", 1)[0]

    def test_renders_benchmark_line_and_end_labels_when_provided(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
        ]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [(datetime(2024, 1, 1), 1.0), (datetime(2024, 6, 1), 1.05)],
        }
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        assert "return-chart__line--bench" in out
        # The benchmark keeps its index number. "S&P" alone is not a
        # shorter way of writing "S&P 500" on this page: S&P Global is
        # a *holding*, named in the tables below, so the bare
        # abbreviation reads as the company that compiles the index
        # rather than the index itself.
        assert ">S&amp;P 500<" in out
        assert ">S&amp;P<" not in out
        assert ">Portfolio<" in out

    def test_only_the_redundant_index_suffix_is_dropped(self):
        history = [(datetime(2024, 1, 1), 1.0), (datetime(2024, 6, 1), 1.1)]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "name": "S&P 500 Index",
            "history": [(datetime(2024, 1, 1), 1.0), (datetime(2024, 6, 1), 1.05)],
        }
        out = _render_chart({"history": history}, [benchmark], benchmark_label=lambda b: b["name"])
        assert ">S&amp;P 500<" in out
        assert "Index<" not in out

    def test_end_values_read_the_series_endpoints(self):
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.02),
                (datetime(2024, 12, 1), 1.05),
            ],
        }
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        assert ">+20.0%<" in out
        assert ">+5.0%<" in out

    def test_overlapping_end_labels_are_pushed_apart(self):
        # A dead-heat window would otherwise render two labels on top
        # of each other. The dots stay on the curve; only the text moves.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.05),
            (datetime(2024, 12, 1), 1.1),
        ]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.049),
                (datetime(2024, 12, 1), 1.099),
            ],
        }
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        names = [
            float(m)
            for m in re.findall(r'class="return-chart__end-name" x="[\d.]+" y="([\d.-]+)"', out)
        ]
        assert len(names) == 2
        assert abs(names[0] - names[1]) >= 40

    def test_no_bracket_overlay_survives(self):
        # The 1.75px vertical bracket the shaded band replaces is gone,
        # along with its label and its CSS custom properties.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 12, 1), 1.2),
        ]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [(datetime(2024, 1, 1), 1.0), (datetime(2024, 12, 1), 1.05)],
        }
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        assert "return-chart__delta-label" not in out
        assert "--top:" not in out
        assert "return-chart__caption" not in out


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
        # The plot box is what the scrubber converts through: the axes
        # reserve room on both sides of the viewBox, so a naive
        # pointer-x-over-container-width mapping would report a date
        # two months off at either edge.
        assert data["plot"]["x0"] > 0
        assert data["plot"]["x1"] < data["view"]["w"]
        assert data["plot"]["y0"] < data["plot"]["y1"]
        # The domain is snapped to the tick ladder rather than padded
        # by a percentage of the range, and zero is always inside it:
        # the baseline is the reference every value is measured
        # against. Here the data never dips below its starting value,
        # so the floor lands exactly on it.
        assert data["yMin"] == 1.0
        assert data["yMax"] >= 1.2
        # Only the JG series is present; bench is absent.
        kinds = [s["kind"] for s in data["series"]]
        assert kinds == ["jg"]
        jg = data["series"][0]
        # The tooltip has room for the whole word; only the chart's
        # 114-unit right inset has to initial it.
        assert jg["label"] == "Portfolio"
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
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.02),
                (datetime(2024, 12, 1), 1.05),
            ],
        }
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        data = self._parse_chart_attr(out)
        kinds = [s["kind"] for s in data["series"]]
        assert kinds == ["jg", "bench"]
        bench = data["series"][1]
        assert bench["label"] == "S&P 500"
        # The portfolio's own series is labelled for the tooltip, where
        # there is room for the whole word (the chart's end label
        # initials it because the inset is 114 units wide).
        assert data["series"][0]["label"] == "Portfolio"
        # Both series share the same densely-sampled x-axis so the
        # tooltip date, marker dots, and local caliper stay in
        # lockstep across the two curves.
        assert bench["x"] == data["series"][0]["x"]
        # Endpoints of the Pchip-interpolated bench curve match the
        # raw history.
        assert bench["y"][0] == 1.0
        assert bench["y"][-1] == 1.05

    def test_curve_faithfully_reproduces_input_history(self):
        # The renderer is a pure projection of its inputs: the chart's
        # right-edge sample IS whatever ``history[-1][1]`` says, with
        # no rescale, clamp, or other in-renderer adjustment. The
        # invariant that the chart's right edge equals
        # ``1 + tsr%/100`` is upheld UPSTREAM in
        # ``Benchmark.summary``, which feeds the chart a history
        # whose last point is computed from the same
        # ``regularMarketPrice / Adj Close[0]`` numerator the TSR
        # uses (see ``tests/test_performance.py``). Here we just
        # pin the projection invariant: whatever the caller hands
        # us, the embedded scrubber payload mirrors it.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "tsr%": 5.7,  # intentionally inconsistent with history[-1]
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.02),
                (datetime(2024, 12, 1), 1.05),
            ],
        }
        out = Webpage._render_return_chart({"history": history, "twr%": 18.4}, [benchmark])
        data = self._parse_chart_attr(out)
        jg, bench = data["series"]
        # Even though ``tsr%`` / ``twr%`` are present and disagree
        # with the histories, the chart projects the history as-is.
        assert jg["y"][0] == 1.0
        assert jg["y"][-1] == 1.2
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
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [(datetime(2024, 1, 1), 1.0), (datetime(2024, 12, 1), 1.05)],
        }
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
        assert "return-chart__hover-delta-bar" not in out
        assert "return-chart__tooltip-delta" not in out

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
        benchmark = {
            "ticker": "LSE:VUAA.L",
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.02),
                (datetime(2024, 12, 1), 1.05),
            ],
        }
        out = Webpage._render_return_chart({"history": history}, [benchmark])
        assert 'class="return-chart__hover-delta-bar"' in out
        assert 'class="return-chart__tooltip-delta"' in out
        # The hover overlay is the last thing in the plot block so it
        # paints above the SVG without needing a stacking hack. There
        # is no static delta annotation left to sit before -- the
        # shaded band replaced it.
        assert out.index('class="return-chart__hover"') > out.index("<svg ")
        assert 'class="return-chart__delta"' not in out

    def test_short_history_omits_chart_and_data(self):
        # Single-sample history -> no chart, no scrubber data.
        out = Webpage._render_return_chart({"history": [(datetime(2024, 1, 1), 1.0)]}, [])
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
        assert _RETURN_CHART_SCRIPT in head
        # And its SHA-256 hash is referenced from the CSP meta tag.
        digest = _sha256_b64(_RETURN_CHART_SCRIPT)
        assert f"sha256-{digest}" in head

    def test_script_initialises_pointer_event_handlers(self):
        # The script must own the contract its rendered chart expects:
        # it has to react to pointer movement and project values onto
        # the curves. The exact wiring is JS, so we sanity-check the
        # payload references the key DOM hooks and APIs.
        script = _RETURN_CHART_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
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
        script = _NAV_SCROLL_SCRIPT
        blur_idx = script.index("a.blur")
        slide_idx = script.index("slide(el,dur)")
        assert blur_idx < slide_idx


class TestInteractionStyles:
    """Pointer-interaction rules that only exist in the served CSS.

    The marquee and the clickable allocation-bar rows are gone, so the
    ``@media (hover: hover)`` gates that guarded their hover states
    went with them; what survives here is the one rule that has to
    stay absent.
    """

    def test_no_css_smooth_scroll_layered_on_top_of_js_animation(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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

        from tests._css_helpers import at_rule_body, normalize

        # The page emits no ``scroll-behavior`` *declaration*: the
        # only ``scroll-behavior`` text left should be inside an
        # explanatory CSS comment (stripped at minify time anyway),
        # never as a real declaration. ``scroll-behavior:`` after
        # normalisation matches both the old formatted form
        # (``scroll-behavior: smooth;``) and any minified
        # reintroduction (``scroll-behavior:smooth;``).
        assert "scroll-behavior:" not in normalize(out)
        # The reduced-motion media query is still emitted (the
        # ticker animation override needs to live), but no
        # ``html:focus-within`` selector should appear in its body.
        rm_body = at_rule_body(out, "@media (prefers-reduced-motion: reduce)")
        assert rm_body is not None, "reduced-motion block missing"
        assert "html:focus-within" not in rm_body

    def test_removed_surfaces_leave_no_orphaned_rules(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Dead selectors in a hashed inline stylesheet are shipped
        # bytes that no element can ever match, and they make the next
        # reader think the surface still exists.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        for token in ("ticker__", "treemap__", "bars__", "returns-compare", "holding__"):
            assert token not in out, f"orphaned {token} rules survived"


class TestOgImageHeroCopy:
    """Direct unit tests on :func:`investing.webpage.og_image._hero_copy`
    -- every string the share card's left-hand column renders.

    Lives at unit-helper granularity rather than driving the full PNG
    renderer because the rendered text isn't round-trippable out of
    the raster output. Two contracts are pinned here. The comparison
    is *annualised* on both sides -- a total-return gap is a function
    of how long the portfolio has been open as much as of how it has
    been run, and seven years of a slender annual edge compounds into
    a headline that sounds like one spectacular year. And the wording
    flips with the sign, so a losing window never ships a card
    claiming a lead."""

    @staticmethod
    def _copy(cagr, delta, label="S&P 500"):
        from investing.webpage.og_image import _hero_copy

        return _hero_copy(cagr, delta, label)

    def test_positive_delta_reads_as_ahead(self):
        copy = self._copy(5.3, 0.6)
        assert copy.number == "+0.6"
        assert copy.unit == "pp"
        assert copy.claim == "ahead of the S&P 500"
        assert copy.eyebrow == "Annualised return vs S&P 500"
        assert copy.positive

    def test_zero_delta_still_reads_as_ahead(self):
        # A dead heat is not a loss; the sign colour and the wording
        # have to agree, and both treat zero as non-negative.
        copy = self._copy(41.7, 0.0)
        assert copy.claim == "ahead of the S&P 500"
        assert copy.positive

    def test_negative_delta_reads_as_behind(self):
        copy = self._copy(35.0, -3.4)
        # U+2212 MINUS SIGN, not the ASCII hyphen: the card sets this
        # beside signed positives and the two have to share an advance
        # width. The vendored Roboto carries the glyph in both weights.
        assert copy.number == "\u22123.4"
        assert copy.claim == "behind the S&P 500"
        assert not copy.positive

    def test_uses_the_benchmark_label(self):
        assert self._copy(20.0, 2.0, "MSCI World").claim == "ahead of the MSCI World"
        assert self._copy(20.0, -2.0, "MSCI World").claim == "behind the MSCI World"

    def test_falls_back_to_sp500_when_bench_label_missing(self):
        # Never let the claim collapse to "ahead of the ".
        assert self._copy(20.0, 1.0, None).claim == "ahead of the S&P 500"
        assert self._copy(20.0, 1.0, "").claim == "ahead of the S&P 500"

    def test_no_benchmark_reads_as_a_standalone_metric(self):
        # With nothing to compare against, leading with a smaller true
        # claim beats inventing a comparison the page cannot draw.
        copy = self._copy(5.3, None)
        assert copy.number == "5.3"
        assert copy.unit == "%"
        assert copy.claim == "annualised since inception"
        assert copy.eyebrow == "Annualised return"
        assert copy.positive

    def test_negative_standalone_return_is_not_positive(self):
        assert not self._copy(-12.0, None).positive

    def test_the_card_and_the_page_are_both_annualised(self):
        """The two assets must not headline different numbers.

        The card used to lead with the *total* return delta while the
        page's hero leads with the annualised one, so the same
        portfolio argued +6.7 pp in a feed and +0.6 pp on the page it
        linked to. Whichever form is chosen, both have to choose it.

        This pins the card's half; ``test_preview_html`` pins that the
        page's own two figures are arithmetically consistent.
        """
        from investing.webpage import og_image

        source = inspect.getsource(og_image._render_unsafe)
        hero_call = re.search(r"_hero_copy\(([^)]*)\)", source)
        assert hero_call, "no _hero_copy call in _render_unsafe"
        args = hero_call.group(1)
        assert "cagr" in args and "twr" not in args, (
            f"the hero is fed {args!r} -- it has to be the annualised pair"
        )


class TestOgImageFootCopy:
    """Direct unit tests on :func:`investing.webpage.og_image._foot_copy`
    -- the credibility line beneath the logo strip.

    Unit-helper granularity for the same reason as the hero copy: the
    text is drawn into a raster and cannot be read back out. The one
    contract worth pinning is that the count describes the portfolio
    and not the strip of logos it sits under.
    """

    @staticmethod
    def _foot(count, when=date(2019, 1, 1), duration="7 years, 7 months"):
        from investing.webpage.og_image import _foot_copy

        return _foot_copy(when, duration, count)

    def test_the_count_is_the_portfolios_not_the_strips(self):
        # The strip holds at most ten logos. A portfolio of seventeen
        # says seventeen, or the line reads as a caption on the row
        # above it and states something false about the holdings.
        assert self._foot(17).endswith("17 equities")

    def test_singular_count_reads_as_one_equity(self):
        assert self._foot(1).endswith("1 equity")
        assert "equities" not in self._foot(1)

    def test_absent_count_drops_the_clause_entirely(self):
        # Not "0 equities", and not a guess from the strip: with
        # nothing to say, the line says nothing.
        assert self._foot(None) == "Since Jan 1, 2019  \u00b7  7 years, 7 months"
        assert self._foot(0) == self._foot(None)

    def test_the_inception_half_survives_either_way(self):
        for count in (None, 1, 17):
            line = self._foot(count)
            assert line.startswith("Since Jan 1, 2019")
            assert "7 years, 7 months" in line


class TestOgImageEquityCount:
    """Where the count the card prints comes from.

    :class:`TestOgImageFootCopy` pins what the string does with a
    count; this pins that the count handed to it is the number of
    equities held, which is the claim the word "equities" makes.
    """

    def test_the_page_counts_equities_not_every_line_item(self):
        # ``current`` is the equity sleeve; bond ETFs are tracked
        # separately in ``current_fixed_income``. Counting rows
        # instead would file two Treasury ETFs under "equities".
        from investing.webpage import _page

        source = inspect.getsource(_page.Webpage._render_og_image)
        assert "equity_count=len(self.current)" in "".join(source.split())


class TestOgImageFont:
    """The card's typeface is committed, not discovered.

    It used to probe the host -- DejaVu, then Arial, then Helvetica,
    then Pillow's bitmap default -- so a local render and the CI
    render of identical inputs came out in different faces. These
    tests pin the fix: the file ships in the repo, and ``load_font``
    actually loads it rather than silently falling back."""

    def test_vendored_font_files_are_committed(self):
        from pathlib import Path as _Path

        from investing.webpage import og_image

        for name in og_image._FONT_FILES.values():
            path = _Path(og_image._FONT_DIR) / name
            assert path.is_file(), f"missing vendored font {name}"
        # The upstream licence ships beside them; redistributing the
        # binaries without it is the one thing Apache-2.0 asks.
        assert (_Path(og_image._FONT_DIR) / "LICENSE").is_file()

    def test_load_font_returns_the_vendored_face_not_the_fallback(self):
        from investing.webpage.og_image import load_font

        for weight in ("regular", "bold"):
            font = load_font(weight, 32)
            # Pillow's bitmap default has no ``path``; a real
            # FreeType face does, and it has to be ours.
            assert getattr(font, "path", "").endswith("Roboto-Regular.ttf") or getattr(
                font, "path", ""
            ).endswith("Roboto-Bold.ttf"), f"{weight} fell back to a host font"
            assert font.size == 32

    def test_the_two_weights_are_actually_different_faces(self):
        # A bold that silently resolves to the regular file would make
        # every emphasis on the card a no-op.
        from investing.webpage.og_image import load_font

        assert load_font("regular", 32).path != load_font("bold", 32).path

    def test_unknown_weight_falls_back_rather_than_raising(self):
        # Best-effort is the contract for the whole OG path: never
        # take the page build down over a missing glyph set.
        from investing.webpage.og_image import load_font

        assert load_font("ultralight", 32) is not None

    def test_no_host_font_probing_survives(self):
        # The candidate ladder is what made the card non-deterministic.
        from investing.webpage import og_image

        assert not hasattr(og_image, "_FONT_CANDIDATES")


class TestSave:
    def test_writes_index_html_with_key_sections(self, stub_logo_lookup, chdir_tmp, freeze_today):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
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
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.add_return(_total_return(), [_benchmark()])
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert out.startswith("<!DOCTYPE html>")
        assert out.rstrip().endswith("</html>")
        assert '<html lang="en">' in out
        # The descriptive title is what renders on SERPs/tabs.
        assert "<title>Jan Grzybek - Investment Portfolio</title>" in out
        # Mobile readiness: viewport + theme-color metas, and at least
        # one narrow-width media query in the embedded stylesheet.
        assert 'name="viewport"' in out
        assert "width=device-width" in out
        assert 'name="theme-color"' in out
        from tests._css_helpers import contains_at_rule

        assert contains_at_rule(out, "@media (max-width: 560px)")
        # Brand lockup + in-page nav anchored to each section.
        assert '<header class="site-header">' in out
        assert '<p class="site-brand">' in out
        assert "Jan Grzybek" in out
        assert '<nav class="site-nav"' in out
        assert 'href="#performance"' in out
        assert 'href="#holdings"' in out
        assert 'href="#method"' in out
        # Sections expose the anchor IDs the nav links target.
        for section_id in ("performance", "allocation", "holdings", "closed", "method"):
            assert f'id="{section_id}"' in out, f"missing #{section_id}"
        # The claim is above the fold, before any section.
        hero_idx = out.index('class="hero"')
        performance_idx = out.index('id="performance"')
        main_idx = out.index('<main id="main-content"')
        assert main_idx < hero_idx < performance_idx
        assert "ahead of the S&amp;P 500" in out
        assert 'class="hero__figure' in out
        # The freshness date rides in the hero, not the footer.
        assert '<time datetime="2025-06-01">Jun 1, 2025</time>' in out
        assert out.index("Updated") < performance_idx
        # Section headings.
        assert ">Cumulative return</h2>" in out
        assert ">Allocation</h2>" in out
        assert ">Holdings</h2>" in out
        assert ">Closed positions</h2>" in out
        # Single semantic structure (no desktop/mobile duplication).
        # <main> carries an id so the skip link can target it and a
        # tabindex so screen readers can move focus there.
        assert '<main id="main-content"' in out
        assert "</main>" in out
        assert "<footer" in out
        assert 'class="holdings__row"' in out
        # Skip link is the first interactive element in <body>, ahead
        # of the sticky header.
        assert 'class="skip-link" href="#main-content"' in out
        body_idx = out.index("<body>")
        skip_idx = out.index('class="skip-link"')
        header_idx = out.index('class="site-header"')
        assert body_idx < skip_idx < header_idx
        # The marquee is gone: its job -- "here are the brands I own"
        # -- is done better by logos in the holdings table, which also
        # carry numbers.
        assert 'class="ticker"' not in out
        assert '<figure class="treemap"' not in out
        # An open position's listing appears once, in its row. The
        # marquee used to print it twice more and the treemap payload
        # twice again.
        assert out.count("NMS:CURR") == 1
        # A closed position's listing appears once too, in the closed
        # table -- it used to be absent from the page entirely.
        assert out.count("NMS:OLD") == 1
        # Two stacked allocation bars replace the bar chart + treemap.
        assert 'class="allocation"' in out
        assert "By asset class" in out
        # Dark mode and print rules are present.
        assert "prefers-color-scheme: dark" in out
        assert "@media print" in out

    def test_save_method_block_pairs_the_two_return_definitions(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # The two things a reader needs from the block are a *pair*:
        # what the portfolio-level number means and what the
        # per-holding numbers mean. Setting them side by side is the
        # point -- the time-weighted / money-weighted distinction is
        # the most misreadable thing on the page, and burying it as
        # bullet three of four made it look like boilerplate.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        w.add_holding(_holding(ticker="NMS:CURR", name="Currentco"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert '<h2 class="method__title">Method &amp; disclaimer</h2>' in out
        assert '<div class="method__grid">' in out
        assert "<strong>Portfolio TWR</strong>" in out
        assert "<strong>Per-holding Return and IRR</strong>" in out
        assert "<strong>USD</strong>" in out
        # The old bullet-list footer is fully retired.
        assert "footer__notes" not in out
        assert "footer__disclaimer" not in out
        # Disclaimer and legal note follow the definitions.
        twr_idx = out.index("<strong>Portfolio TWR</strong>")
        legal_idx = out.index('class="method__legal"')
        assert twr_idx < legal_idx
        assert "informational purposes" in out
        assert "no cookies or tracking identifiers are used." in out
        # Unlike the old footer, Method IS a nav target: a reader who
        # wants to know how a number was computed should not have to
        # scroll to find out.
        nav_start = out.index('<nav class="site-nav"')
        nav_end = out.index("</nav>", nav_start)
        assert 'href="#method"' in out[nav_start:nav_end]
        assert '<footer id="method"' in out

    def test_save_emits_seo_metadata_in_head(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
            "og:title",
            "og:description",
            "og:image",
            "og:url",
            "og:type",
            "og:locale",
            "og:site_name",
        ):
            assert f'property="{prop}"' in out
        # Twitter Card variants for X/Twitter previews. Now using
        # ``summary_large_image`` since we ship a 1200x630 OG image.
        for tw in (
            "twitter:card",
            "twitter:title",
            "twitter:description",
            "twitter:image",
            "twitter:image:alt",
        ):
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        csp = out.split('http-equiv="Content-Security-Policy" content="', 1)[1].split('"', 1)[0]
        directives: dict[str, list[str]] = {}
        for directive in csp.split(";"):
            tokens = directive.strip().split()
            if tokens:
                directives[tokens[0]] = tokens[1:]

        def _contains(tokens: list[str], expected: str) -> bool:
            return any(token == expected for token in tokens)

        assert _contains(directives["default-src"], "'self'")
        assert _contains(directives["script-src"], "https://static.cloudflareinsights.com")
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
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

    def test_save_wires_nav_anchors_to_every_section(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # The cross-section click affordances the marquee and the
        # treemap used to provide are gone with them; what is left is
        # the nav, and every link in it has to land on a real element.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_allocations(
            {"Equities": 95.4, "Cash & Cash Equivalents": 4.6},
            {"NMS:AAA": 60.0, "NMS:BBB": 25.0, "Other equities": 10.0},
        )
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha"))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta"))
        w.add_return(_total_return(), [])
        w.save()
        out = (chdir_tmp / "index.html").read_text()

        nav_start = out.index('<nav class="site-nav"')
        nav_end = out.index("</nav>", nav_start)
        nav_html = out[nav_start:nav_end]
        targets = re.findall(r'href="#([^"]+)"', nav_html)
        assert targets
        for target in targets:
            assert f'id="{target}"' in out, f"nav points at missing #{target}"

        # Rows still expose stable anchor ids so a future cross-
        # reference (or a shared deep link) can scroll to them -- and,
        # unlike before, every row is visible, so no script has to
        # force-expand a list before the browser can find one.
        assert ' id="holding-NMS-AAA"' in out
        assert ' id="holding-NMS-BBB"' in out
        # ``top_10`` may carry a synthetic "Other equities" rollup
        # label, but that bucket never becomes a holding anchor.
        assert "holding-Other" not in out

    def test_save_without_current_holdings_skips_the_holdings_section(
        self, stub_logo_lookup, chdir_tmp, freeze_today
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        # Only a closed position.
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.add_return(_total_return(), [_benchmark()])
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert ">Closed positions</h2>" in out
        assert ">Holdings</h2>" not in out
        # Nav drops the Holdings link and the section anchor with it.
        assert 'href="#holdings"' not in out
        assert 'id="holdings"' not in out
        assert 'href="#performance"' in out
        assert 'href="#method"' in out

    def test_hero_counts_open_and_closed_positions(self, stub_logo_lookup, chdir_tmp, freeze_today):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.add_holding(_holding(ticker="NMS:BBB"))
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.add_return(_total_return(), [_benchmark()])
        w.save()
        out = (chdir_tmp / "index.html").read_text()
        hero = out[out.index('class="hero"') : out.index("</section>", out.index('class="hero"'))]
        assert ">Positions</dt>" in hero
        assert ">2<" in hero
        assert "1 closed" in hero
