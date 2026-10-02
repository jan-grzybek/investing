"""Return section and benchmark comparison rendering."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from investing.webpage import Webpage
from investing.webpage.return_chart import render as _render_chart
from tests._webpage_support import (
    _benchmark,
    _total_return,
    stub_logo_lookup,
)

# ``_total_return`` carries a single history point, which is below the
# chart's two-sample floor -- fine for the sections that only read the
# headline numbers, useless for asserting on axes. These build a real
# three-point pair so the chart actually renders.
_DATES = [datetime(2024, 1, 1), datetime(2024, 6, 1), datetime(2024, 12, 1)]


def _charted_return(values=(1.0, 1.15, 1.25)):
    return {
        "start_date": _DATES[0],
        "history": list(zip(_DATES, values, strict=True)),
        "twr%": (values[-1] - 1) * 100,
        "cagr%": 12.5,
    }


def _charted_benchmark(values=(1.0, 1.05, 1.10)):
    bench = _benchmark()
    bench["history"] = list(zip(_DATES, values, strict=True))
    bench["tsr%"] = (values[-1] - 1) * 100
    return bench


class TestAddReturn:
    def test_return_html_is_populated(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])

        assert w.return_html
        # The section leads with its own title and the chart key, then
        # the chart, then the year table -- in that order.
        assert "Cumulative return" in w.return_html
        assert "return-chart" in w.return_html
        assert w.return_html.index("section__head") < w.return_html.index("return-chart")

    def test_works_with_no_benchmarks(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [])
        assert "return-chart" in w.return_html
        # No benchmark means no alpha band and no benchmark chip.
        assert "legend__swatch--band" not in w.return_html
        assert "return-chart__band" not in w.return_html

    def test_chart_key_names_both_series_and_the_band(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert "legend__swatch--jg" in w.return_html
        assert "legend__swatch--bench" in w.return_html
        assert "legend__swatch--band" in w.return_html
        assert "Alpha" in w.return_html

    def test_intro_explains_the_band_when_a_benchmark_is_present(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert "The shaded band is the running gap between the two." in w.return_html

    def test_intro_omits_the_band_sentence_without_a_benchmark(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [])
        assert "shaded band" not in w.return_html

    def test_comparison_capsules_are_gone(self, stub_logo_lookup):
        # The head-to-head capsules moved into the hero's four-up strip,
        # where they sit beside the claim they support instead of
        # repeating it a screen further down.
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()])
        assert "returns-compare" not in w.return_html


class TestReturnChartAxes:
    """The chart's whole point after the redesign: every value on it is
    readable without a pointer."""

    @staticmethod
    def _chart(w: Webpage) -> str:
        return w.return_html

    def test_y_axis_labels_are_cumulative_percentages(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        chart = self._chart(w)
        assert "return-chart__tick" in chart
        # A real 0% baseline, not an unlabelled dashed line.
        assert "return-chart__base" in chart
        # The leading "+" sits in its own tspan so the phone frame can
        # drop it -- the design's phone axis reads "0% / 20% / 40%".
        assert '<tspan class="return-chart__tick-sign">+</tspan>0%<' in chart

    def test_axis_ticks_drop_the_decimal_on_whole_numbers(self, stub_logo_lookup):
        # Tick values come off a 1 / 2 / 2.5 / 5 ladder precisely so a
        # reader can do arithmetic with them, and "+10%" is easier to do
        # arithmetic with than "+10.0%".
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        assert "</tspan>0.0%<" not in self._chart(w)

    def test_gridlines_accompany_every_tick(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        chart = self._chart(w)
        assert chart.count("return-chart__grid") + chart.count("return-chart__base") >= 3

    def test_year_ticks_name_the_span(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        # ``_total_return`` starts in 2024; the first tick always names
        # the year the history begins in rather than the next January.
        assert ">2024<" in self._chart(w)

    def test_every_other_label_is_marked_minor_for_narrow_screens(self, stub_logo_lookup):
        # The SVG scales uniformly, so on a phone the labels grow back
        # in viewBox units and there is no longer room for all of them.
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        assert "return-chart__tick--minor" in self._chart(w)

    def test_both_series_are_labelled_at_their_end_point(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        chart = self._chart(w)
        assert "return-chart__end-dot--jg" in chart
        assert "return-chart__end-dot--bench" in chart
        assert "return-chart__end-value--jg" in chart
        assert "return-chart__end-value--bench" in chart
        assert ">Portfolio<" in chart

    def test_alpha_is_the_filled_area_not_a_bracket(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        chart = self._chart(w)
        assert "return-chart__band--pos" in chart
        # The 1.75px vertical bracket the band replaces is gone.
        assert "return-chart__delta-bar" not in chart

    def test_chart_scales_uniformly(self, stub_logo_lookup):
        # ``preserveAspectRatio="none"`` plus a CSS aspect-ratio is what
        # stretched the curve non-uniformly on a phone.
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        assert 'preserveAspectRatio="none"' not in self._chart(w)

    def test_alt_text_carries_the_numbers_not_the_genre(self, stub_logo_lookup):
        # Someone who cannot see the chart needs what it draws, which is
        # what the sighted reader takes from it too.
        w = Webpage()
        w.add_return(_charted_return(), [_charted_benchmark()])
        chart = self._chart(w)
        assert "aria-label=" in chart
        assert "Portfolio return curve" not in chart

    def test_scrubber_payload_carries_the_plot_box(self):
        # The axes reserve room on both sides of the viewBox, so a naive
        # pointer-x-over-container-width mapping would report a date two
        # months off at either edge. The script converts through the
        # same box the SVG drew into.
        import json

        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 12, 1), 1.2),
        ]
        tr = {"start_date": history[0][0], "history": history, "twr%": 20.0, "cagr%": 20.0}
        out = _render_chart(tr, [], benchmark_label=lambda _b: "X")
        raw = out.split('data-chart="', 1)[1].split('"', 1)[0]
        data = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
        assert set(data["plot"]) == {"x0", "x1", "y0", "y1"}
        assert data["plot"]["x0"] < data["plot"]["x1"]
        assert data["view"] == {"w": 1000.0, "h": 372.0}
        assert data["series"][0]["kind"] == "jg"


class TestBandSplitsAtCrossings:
    """A single polygon painted one colour would claim a lead across
    windows where the portfolio was behind."""

    @staticmethod
    def _render(jg, bench):
        dates = [datetime(2024, 1, 1), datetime(2024, 6, 1), datetime(2024, 12, 1)]
        tr = {
            "start_date": dates[0],
            "history": list(zip(dates, jg, strict=True)),
            "twr%": (jg[-1] - 1) * 100,
            "cagr%": 0.0,
        }
        b = {
            "ticker": "BENCH",
            "name": "Bench",
            "tsr%": (bench[-1] - 1) * 100,
            "cagr%": 0.0,
            "history": list(zip(dates, bench, strict=True)),
        }
        return _render_chart(tr, [b], benchmark_label=lambda _b: "Bench")

    def test_leading_throughout_paints_one_positive_band(self):
        out = self._render([1.0, 1.2, 1.4], [1.0, 1.1, 1.2])
        assert out.count("return-chart__band--pos") == 1
        assert "return-chart__band--neg" not in out

    def test_trailing_throughout_paints_a_negative_band(self):
        out = self._render([1.0, 1.05, 1.1], [1.0, 1.2, 1.4])
        assert "return-chart__band--pos" not in out
        assert out.count("return-chart__band--neg") == 1

    def test_a_crossing_splits_the_band_into_both_colours(self):
        out = self._render([1.0, 0.9, 1.4], [1.0, 1.1, 1.2])
        assert "return-chart__band--neg" in out
        assert "return-chart__band--pos" in out


class TestYearlyReturns:
    def test_yearly_returns_table_renders_when_provided(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(
            _total_return(),
            [_benchmark()],
            yearly_returns=[
                {"year": 2025, "jg%": 5.0, "bench%": 3.0, "is_ytd": True},
                {"year": 2024, "jg%": 12.0, "bench%": 10.0, "is_ytd": False},
            ],
        )
        html = w.return_html
        assert 'class="yearly"' in html
        assert "(YTD)" in html
        assert "+2.0 pp" in html
        # The column is named, with its unit, rather than being a bare
        # delta glyph.
        assert ">Alpha<" in html
        assert ">Δ<" not in html

    def test_heading_claims_the_streak_only_when_it_is_true(self, stub_logo_lookup):
        ahead = [
            {"year": 2025, "jg%": 5.0, "bench%": 3.0, "is_ytd": False},
            {"year": 2024, "jg%": 12.0, "bench%": 10.0, "is_ytd": False},
        ]
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()], yearly_returns=ahead)
        assert "Ahead every year" in w.return_html

        mixed = [
            {"year": 2025, "jg%": 1.0, "bench%": 3.0, "is_ytd": False},
            {"year": 2024, "jg%": 12.0, "bench%": 10.0, "is_ytd": False},
        ]
        w2 = Webpage()
        w2.add_return(_total_return(), [_benchmark()], yearly_returns=mixed)
        assert "Ahead every year" not in w2.return_html
        assert "Year by year" in w2.return_html

    def test_summary_counts_only_comparable_years(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(
            _total_return(),
            [_benchmark()],
            yearly_returns=[
                {"year": 2025, "jg%": 5.0, "bench%": 3.0, "is_ytd": False},
                {"year": 2024, "jg%": 12.0, "is_ytd": False},
            ],
        )
        # Two years listed, but only one has a benchmark to be ahead of.
        assert "2 of 2 years positive" in w.return_html
        assert "1 of 1 ahead of the benchmark" in w.return_html

    def test_every_year_is_visible(self, stub_logo_lookup):
        # Three of seven years used to start collapsed behind a toggle,
        # which hid most of the evidence for the page's central claim.
        rows = [
            {"year": year, "jg%": 5.0, "bench%": 3.0, "is_ytd": False}
            for year in range(2025, 2018, -1)
        ]
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()], yearly_returns=rows)
        html = w.return_html
        assert "returns-yearly__toggle" not in html
        assert "Show all" not in html
        assert html.count('class="yearly__row"') == 7

    def test_bars_are_normalised_to_the_best_year(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(
            _total_return(),
            [_benchmark()],
            yearly_returns=[
                {"year": 2025, "jg%": 10.0, "bench%": 5.0, "is_ytd": False},
                {"year": 2024, "jg%": 5.0, "bench%": 2.5, "is_ytd": False},
            ],
        )
        html = w.return_html
        assert "width: 100.0%" in html
        assert "width: 50.0%" in html

    @staticmethod
    def _yearly(years: list[tuple[float, float]]) -> str:
        """The block for ``(portfolio, benchmark)`` returns, newest year first."""
        w = Webpage()
        w.add_return(
            _total_return(),
            [_benchmark()],
            yearly_returns=[
                {"year": 2025 - i, "jg%": jg, "bench%": bench, "is_ytd": False}
                for i, (jg, bench) in enumerate(years)
            ],
        )
        return w.return_html

    @staticmethod
    def _bar_widths(html: str) -> list[str]:
        return re.findall(r'class="yearly__bar [^"]*" style="width: ([^%"]*)%"', html)

    @pytest.mark.parametrize(
        ("record", "prints_the_same"),
        [
            pytest.param(
                [(31.24, 18.44), (12.31, 7.02)],
                [(31.16, 18.36), (12.27, 6.98)],
                id="portfolio-sets-the-scale",
            ),
            pytest.param(
                [(12.31, 31.24), (7.02, 18.44)],
                [(12.27, 31.16), (6.98, 18.36)],
                id="benchmark-sets-the-scale",
            ),
            pytest.param(
                [(-31.20, 18.40), (12.31, -7.02)],
                [(-31.17, 18.43), (12.28, -7.01)],
                id="a-loss-sets-the-scale",
            ),
            pytest.param(
                [(131.24, 18.44), (12.31, 7.02)],
                [(130.90, 18.36), (12.27, 6.98)],
                id="three-digits",
            ),
            pytest.param([(0.04, -0.03)], [(0.03, -0.04)], id="all-zeros"),
        ],
    )
    def test_bars_say_no_more_than_the_figures_beside_them(
        self, stub_logo_lookup, record, prints_the_same
    ):
        # A bar's width is a return in another form, in markup as
        # public as the cell next to it. Drawn from the unrounded
        # return it would carry digits the cell does not print. So two
        # records that print the same in every cell are the same bytes
        # underneath.
        assert self._yearly(prints_the_same) == self._yearly(record)

    def test_bars_are_the_printed_figures_over_the_largest(self, stub_logo_lookup):
        # 31.2 and 18.4, then 12.3 and 7.0, each over 31.2.
        widths = self._bar_widths(self._yearly([(31.24, 18.44), (12.31, 7.02)]))
        assert widths == ["100.0", "59.0", "39.4", "22.4"]
        # A difference the reader can see moves its bar: 12.4 over 31.2.
        moved = self._bar_widths(self._yearly([(31.24, 18.44), (12.41, 7.02)]))
        assert moved == ["100.0", "59.0", "39.7", "22.4"]

    def test_a_loss_can_be_the_longest_bar(self, stub_logo_lookup):
        # Lengths are magnitudes: the worst year fills its track and
        # the rest are measured against it.
        widths = self._bar_widths(self._yearly([(-31.2, 18.4), (12.3, -7.0)]))
        assert widths == ["100.0", "59.0", "39.4", "22.4"]
        # The same when the worst year is the benchmark's.
        widths = self._bar_widths(self._yearly([(18.4, -31.2), (-7.0, 12.3)]))
        assert widths == ["59.0", "100.0", "22.4", "39.4"]

    def test_a_table_of_zeros_draws_empty_bars(self, stub_logo_lookup):
        # Nothing to measure against once every figure prints as 0.0.
        assert self._bar_widths(self._yearly([(0.04, -0.03)])) == ["0.0", "0.0"]

    def test_a_losing_year_takes_the_loss_colour(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(
            _total_return(),
            [_benchmark()],
            yearly_returns=[{"year": 2024, "jg%": -8.0, "bench%": -10.0, "is_ytd": False}],
        )
        assert "yearly__bar--neg" in w.return_html

    def test_yearly_returns_omitted_when_empty(self, stub_logo_lookup):
        w = Webpage()
        w.add_return(_total_return(), [_benchmark()], yearly_returns=[])
        assert 'class="yearly"' not in w.return_html

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
        assert 'class="yearly"' in w.return_html
        assert "S&amp;P" not in w.return_html
        assert " pp" not in w.return_html


class TestReturnChartRobustness:
    @staticmethod
    def _tr(history):
        return {
            "start_date": history[0][0],
            "history": history,
            "twr%": 0.0,
            "cagr%": 0.0,
        }

    def test_non_positive_multiplier_does_not_emit_nan(self):
        # A full liquidation drives the TWR multiplier to 0 (a
        # net-negative wipeout below it). The log-space interpolation
        # would push -inf / NaN into both the SVG ``points`` and the
        # ``data-chart`` JSON; the linear-space fallback must keep every
        # emitted number finite.
        history = [
            (datetime(2024, 1, 1), 1.2),
            (datetime(2024, 6, 1), 0.6),
            (datetime(2024, 12, 1), 0.0),
        ]
        out = _render_chart(self._tr(history), [], benchmark_label=lambda _b: "X")
        assert out.startswith("<figure")
        assert "NaN" not in out
        assert "nan" not in out

    def test_negative_multiplier_does_not_emit_nan(self):
        history = [
            (datetime(2024, 1, 1), 1.2),
            (datetime(2024, 6, 1), 0.4),
            (datetime(2024, 12, 1), -0.3),
        ]
        out = _render_chart(self._tr(history), [], benchmark_label=lambda _b: "X")
        assert out.startswith("<figure")
        assert "NaN" not in out
        assert "nan" not in out

    def test_duplicate_sample_dates_do_not_crash(self):
        # Two valuations on the same calendar day make the timeline
        # non-strictly-increasing; the dense Pchip fit would raise
        # ValueError and abort the build. The renderer must fall back to
        # raw points instead.
        history = [
            (datetime(2024, 1, 1), 1.0),
            (datetime(2024, 6, 1), 1.1),
            (datetime(2024, 6, 1), 1.2),
            (datetime(2024, 12, 1), 1.3),
        ]
        out = _render_chart(self._tr(history), [], benchmark_label=lambda _b: "X")
        assert out.startswith("<figure")
        assert "NaN" not in out
        assert "nan" not in out
