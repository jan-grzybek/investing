"""Return section and benchmark comparison rendering."""

from __future__ import annotations

from datetime import datetime

from investing.webpage import Webpage
from tests._webpage_support import (
    _benchmark,
    _total_return,
    stub_logo_lookup,
)


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

    def test_yearly_returns_table_renders_when_provided(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(
            _total_return(),
            [_benchmark()],
            yearly_returns=[
                {"year": 2025, "jg%": 8.2, "bench%": 5.1, "is_ytd": True},
                {"year": 2024, "jg%": 12.0, "bench%": 10.5, "is_ytd": False},
            ],
        )
        assert 'class="returns-yearly"' in w.return_html
        assert 'class="returns-yearly__table"' in w.return_html
        assert "Returns by year" in w.return_html
        assert '2025 <span class="returns-yearly__ytd">(YTD)</span>' in w.return_html
        assert "2024" in w.return_html
        assert "8.2%" in w.return_html
        assert "5.1%" in w.return_html
        assert "+3.1 pp" in w.return_html
        assert 'class="returns-yearly__col-delta' in w.return_html
        assert "returns-yearly__toggle" not in w.return_html
        compare_idx = w.return_html.index('class="returns-compare"')
        yearly_idx = w.return_html.index('class="returns-yearly"')
        assert compare_idx < yearly_idx

    def test_yearly_returns_toggle_when_many_rows(self, stub_logo_lookup):
        w = Webpage()
        rows = [
            {"year": 2026 - i, "jg%": float(i), "bench%": float(i) - 1.0, "is_ytd": i == 0}
            for i in range(6)
        ]
        w.add_return(_total_return(), [_benchmark()], yearly_returns=rows)
        assert 'class="returns-yearly__toggle"' in w.return_html
        assert "Show all 6 years" in w.return_html

    def test_yearly_returns_omitted_when_empty(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()], yearly_returns=[])
        assert "returns-yearly" not in w.return_html

    def test_yearly_returns_without_benchmark_omits_bench_columns(
        self,
        stub_logo_lookup,
    ):
        w = Webpage()
        w.add_return(
            _total_return(),
            [],
            yearly_returns=[{"year": 2024, "jg%": 12.0, "is_ytd": False}],
        )
        assert 'class="returns-yearly"' in w.return_html
        assert "S&amp;P 500" not in w.return_html
        assert " pp" not in w.return_html
