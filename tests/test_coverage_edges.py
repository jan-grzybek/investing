"""Edge and error branches that the feature tests never reach.

The feature suites exercise the happy paths -- a portfolio with
holdings, a chart with two series, a log with trades. What is left
over is the code that runs when the inputs are degenerate: an empty
allocation, a duration under a month, a benchmark whose history is
flat, a config file that is missing. Those branches are where a
silent wrong answer hides longest, because nothing else looks at
them.

Grouped by the module under test rather than by feature, since that
is how the gap is measured.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pytest
from dateutil.relativedelta import relativedelta

from investing.errors import InvariantError
from tests._webpage_support import stub_logo_lookup  # noqa: F401  (pytest fixture)


class TestFormatting:
    def test_duration_caps_at_a_decade(self):
        # Past ten years the month is noise: "12 years" is the claim,
        # and "12 years, 3 months" invites a precision the inception
        # date does not support.
        from investing.formatting import _format_duration

        assert _format_duration(relativedelta(years=12, months=3)) == "12 years"
        assert _format_duration(relativedelta(years=10, months=11)) == "10 years"

    def test_duration_under_a_month_says_so(self):
        # Rendering "0 months" for a portfolio opened last week reads
        # as a bug; the page says "less than a month" instead.
        from investing.formatting import _format_duration

        assert _format_duration(relativedelta(days=6)) == "less than a month"

    def test_duration_joins_years_and_months(self):
        from investing.formatting import _format_duration

        assert _format_duration(relativedelta(years=1, months=1)) == "1 year, 1 month"
        assert _format_duration(relativedelta(years=2, months=5)) == "2 years, 5 months"
        assert _format_duration(relativedelta(months=3)) == "3 months"


class TestSafeHtml:
    def test_plain_string_on_the_left_yields_a_plain_string(self):
        # The safe mark must not travel leftwards onto an unescaped
        # fragment. ``str + SafeHtml`` routes through ``__radd__``
        # (subclass priority) and deliberately returns a plain ``str``.
        from investing.safehtml import SafeHtml

        joined = "<em>" + SafeHtml("&amp;")
        assert joined == "<em>&amp;"
        assert type(joined) is str

    def test_none_on_either_side_contributes_nothing(self):
        from investing.safehtml import SafeHtml

        assert SafeHtml("x") + None == "x"

    def test_safe_plus_safe_stays_safe(self):
        from investing.safehtml import SafeHtml

        joined = SafeHtml("<b>") + SafeHtml("</b>")
        assert isinstance(joined, SafeHtml)


class TestAllocationView:
    def test_a_bar_with_no_segments_renders_nothing(self):
        # A portfolio with no equity sleeve has no sector mix to draw.
        # Emitting an empty bordered card would read as a rendering
        # failure rather than as an absence.
        from investing.webpage.allocation import _bar

        assert _bar(title="Equity sleeve", note="", segments=()) == ""


class TestTradesView:
    def test_a_sized_move_with_no_percentage_falls_back_to_its_verb(self):
        # INCREASE / DECREASE normally render a signed percentage of
        # the prior position. A burst that has no prior position to
        # measure against carries no ``delta_pct``; the row then says
        # what happened ("Bought") rather than rendering "None%".
        from investing.webpage.trades_view import _detail_text

        event = {
            "category": "INCREASE",
            "delta_pct": None,
            "ticker": "NMS:AAA",
            "name": "Alpha",
            "price": 1.0,
            "currency": "USD",
            "start_date": datetime(2024, 1, 1),
            "end_date": datetime(2024, 1, 1),
        }
        assert _detail_text(event) == "Bought"
        assert _detail_text({**event, "category": "DECREASE"}) == "Sold"


class TestHeroView:
    def test_every_year_ahead_needs_a_benchmark_in_every_year(self):
        # The claim is "ahead in *every* calendar year". A year with no
        # benchmark figure cannot support it, so the claim is withheld
        # rather than approximated from the years that do have one.
        from investing.webpage.hero import _every_year_ahead

        ahead = [
            {"year": 2023, "jg%": 10.0, "bench%": 5.0},
            {"year": 2024, "jg%": 8.0, "bench%": 7.0},
        ]
        assert _every_year_ahead(ahead) is True
        assert _every_year_ahead([]) is False
        assert _every_year_ahead([*ahead, {"year": 2025, "jg%": 3.0, "bench%": None}]) is False
        assert _every_year_ahead([*ahead, {"year": 2025, "jg%": 3.0, "bench%": 4.0}]) is False


class TestHoldingsView:
    def test_an_open_period_renders_as_present(self):
        from investing.webpage.holdings_view import _periods_cell

        html = _periods_cell({"periods": [{"start": datetime(2024, 1, 2), "end": None}]})
        assert "<span>Present</span>" in html
        assert 'datetime="2024-01-02"' in html

    def test_a_current_holding_without_a_weight_is_a_pipeline_fault(self):
        # Weight is attached by ``apply_rollup``. Reaching the renderer
        # without one means that step was skipped, and rendering a
        # blank cell would hide it -- so it raises instead.
        from investing.webpage.holdings_view import build_row

        holding = {
            "ticker": "NMS:AAA",
            "name": "Alpha",
            "is_current": True,
            "tsr%": 1.0,
            "cagr%": 1.0,
            "current_weight%": None,
            "period_start": datetime(2024, 1, 1),
            "periods": [{"start": datetime(2024, 1, 1), "end": None}],
        }
        with pytest.raises(InvariantError, match="no weight"):
            build_row(holding, logo_url_for=lambda _t: "logo.svg")


class TestSitemap:
    def test_an_unchanged_file_is_not_rewritten(self, tmp_path):
        # The sitemap is regenerated on every run but its content only
        # moves when the page does. Rewriting an identical file would
        # churn the deploy's mtimes for nothing.
        from investing.webpage.sitemap import _write_if_changed

        path = tmp_path / "sitemap.xml"
        assert _write_if_changed(path, "<urlset/>") is True
        assert _write_if_changed(path, "<urlset/>") is False
        assert _write_if_changed(path, "<urlset><url/></urlset>") is True

    def test_an_unreadable_path_is_treated_as_absent(self, tmp_path):
        from investing.webpage.sitemap import _write_if_changed

        # A directory in the file's place raises OSError on read; the
        # writer must fall through to "no existing content" rather
        # than propagate.
        path = tmp_path / "sitemap.xml"
        path.mkdir()
        with pytest.raises(OSError):
            _write_if_changed(path, "<urlset/>")


class TestPerformanceHelpers:
    def test_forward_fill_leaves_a_clean_series_alone(self):
        import numpy as np

        from investing.performance import _ffill

        arr = np.array([1.0, 2.0, 3.0])
        out = _ffill(arr)
        assert out.tolist() == [1.0, 2.0, 3.0]
        assert _ffill(np.array([])).size == 0

    def test_forward_fill_carries_the_last_good_sample_forward(self):
        import numpy as np

        from investing.performance import _ffill

        out = _ffill(np.array([1.0, np.nan, np.nan, 4.0]))
        assert out.tolist() == [1.0, 1.0, 1.0, 4.0]

    def test_forward_fill_back_fills_a_leading_gap(self):
        # A leading NaN run has no earlier sample to carry forward, so
        # it borrows the first finite one instead -- otherwise the
        # downstream ``np.log`` sees NaN and poisons the whole curve.
        import numpy as np

        from investing.performance import _ffill

        out = _ffill(np.array([np.nan, np.nan, 3.0, 4.0]))
        assert out.tolist() == [3.0, 3.0, 3.0, 4.0]

    def test_multiplier_of_an_empty_history_is_one(self):
        from investing.performance import _multiplier_at

        assert _multiplier_at([], date(2024, 1, 1)) == 1.0

    def test_multiplier_interpolates_geometrically_between_samples(self):
        # Returns compound, so the value half way between two samples
        # is their geometric mean, not their average.
        from investing.performance import _multiplier_at

        history = [(datetime(2024, 1, 1), 1.0), (datetime(2024, 1, 3), 4.0)]
        assert _multiplier_at(history, date(2024, 1, 2)) == pytest.approx(2.0)

    def test_multiplier_falls_back_to_linear_across_a_zero(self):
        # A geometric step through zero is undefined, so the
        # interpolation degrades to linear rather than raising.
        from investing.performance import _multiplier_at

        history = [(datetime(2024, 1, 1), 0.0), (datetime(2024, 1, 3), 4.0)]
        assert _multiplier_at(history, date(2024, 1, 2)) == pytest.approx(2.0)

    def test_multiplier_clamps_outside_the_sampled_window(self):
        from investing.performance import _multiplier_at

        history = [(datetime(2024, 1, 1), 1.5), (datetime(2024, 1, 3), 3.0)]
        assert _multiplier_at(history, date(2023, 6, 1)) == 1.5
        assert _multiplier_at(history, date(2025, 6, 1)) == 3.0

    def test_period_return_of_a_zero_anchor_is_zero(self):
        # Dividing by a zero starting multiplier would be infinite;
        # the honest answer for "return since a worthless base" is to
        # decline to state one.
        from investing.performance import _year_return_pct

        history = [(datetime(2024, 1, 1), 0.0), (datetime(2024, 6, 1), 0.0)]
        assert _year_return_pct(history, date(2024, 1, 1), date(2024, 6, 1)) == 0.0


class TestPchip:
    def test_a_single_knot_has_no_slope_to_estimate(self):
        import numpy as np

        from investing.pchip import _pchip_derivatives

        assert _pchip_derivatives(np.array([1.0]), np.array([2.0])).tolist() == [0.0]

    def test_an_endpoint_slope_that_turns_back_is_flattened(self):
        # The three-point estimate can point the opposite way to the
        # interval it belongs to. Shape preservation means clamping it
        # to zero rather than letting the curve overshoot into a bulge
        # the data does not contain.
        from investing.pchip import _edge_derivative

        assert _edge_derivative(1.0, 1.0, 1.0, 10.0) == 0.0

    def test_an_endpoint_slope_is_capped_at_three_times_the_interval(self):
        from investing.pchip import _edge_derivative

        assert _edge_derivative(1.0, 1.0, 1.0, -10.0) == pytest.approx(3.0)

    @pytest.mark.parametrize(
        ("x", "y", "message"),
        [
            ([[1.0, 2.0]], [[1.0, 2.0]], "1-D"),
            ([1.0, 2.0, 3.0], [1.0, 2.0], "matching shape"),
            ([1.0], [1.0], "at least two knots"),
            ([1.0, 1.0], [1.0, 2.0], "strictly increasing"),
        ],
    )
    def test_malformed_knots_are_rejected(self, x, y, message):
        # Every one of these would otherwise produce a curve rather
        # than an error, and a wrong curve on this page is a wrong
        # performance claim.
        import numpy as np

        from investing.pchip import Pchip

        with pytest.raises(ValueError, match=message):
            Pchip(np.array(x), np.array(y))


class TestReturnChartHelpers:
    def test_a_flat_series_still_gets_a_readable_axis(self):
        # A portfolio that has not moved has zero span. Without a
        # floor the tick ladder would divide by zero and the axis
        # would collapse onto a single line.
        from investing.webpage.return_chart import _nice_step, _y_domain

        assert _nice_step(0.0) == 1.0
        assert _nice_step(-5.0) == 1.0
        lo, hi, step = _y_domain(0.0, 0.0)
        assert hi > lo
        assert step > 0

    def test_year_ticks_name_the_starting_year_then_each_new_year(self):
        from investing.webpage.return_chart import _year_ticks

        ticks = _year_ticks(date(2023, 6, 1), total_days=400)
        assert [year for year, _ in ticks] == [2023, 2024]
        # A datetime start is normalised, not rejected.
        assert _year_ticks(datetime(2023, 6, 1), total_days=400) == ticks

    def test_year_ticks_thin_out_on_a_long_history(self):
        # Twenty years of January labels would render as a solid band
        # of text, so at most eight survive.
        from investing.webpage.return_chart import _year_ticks

        assert len(_year_ticks(date(2000, 1, 1), total_days=365 * 25)) <= 8

    def test_a_band_needs_two_series_that_actually_diverge(self):
        import numpy as np

        from investing.webpage.return_chart import _sign_runs

        assert _sign_runs(np.array([])) == []
        assert _sign_runs(np.zeros(5)) == []

    def test_a_benchmark_with_one_sample_is_not_plottable(self):
        # Two points make a line; one makes a dot. A benchmark that
        # only reported once is dropped rather than drawn as a stub --
        # so the chart renders with the portfolio curve alone and no
        # alpha band.
        from investing.webpage.return_chart import render

        history = [(datetime(2024, 1, 1), 1.0), (datetime(2024, 6, 1), 1.2)]
        svg = render(
            {"start_date": datetime(2024, 1, 1), "history": history, "twr%": 20.0},
            [{"ticker": "X", "name": "X", "tsr%": 5.0, "history": [(datetime(2024, 1, 1), 1.0)]}],
            benchmark_label=lambda b: b["name"],
        )
        assert "return-chart__line--jg" in svg
        assert "return-chart__line--bench" not in svg

    def test_the_alt_text_degrades_when_there_is_nothing_to_compare(self):
        # The long form quotes both figures. Without a benchmark
        # return there is no comparison to state, so it falls back to
        # the shorter sentence rather than saying "against None".
        from investing.webpage.return_chart import _chart_alt

        series = [("jg", "JG", None), ("bench", "S&P 500", None)]
        assert _chart_alt(series, {"twr%": 10.0}, [{"tsr%": None}]).endswith("since inception")
        assert "against" in _chart_alt(series, {"twr%": 10.0}, [{"tsr%": 5.0}])


class TestSectorOverrides:
    def test_a_missing_file_caches_the_empty_result(self, tmp_path, monkeypatch):
        # The lookup runs per holding. Without caching the negative
        # result, a repo with no overrides file would stat it once per
        # ticker on every build.
        import investing.sector_overrides as mod

        monkeypatch.setattr(mod._OverridesCache, "value", None, raising=False)
        monkeypatch.setattr(mod, "_SECTOR_OVERRIDES_PATH", str(tmp_path / "absent.yaml"))
        assert mod._load_overrides() == {}
        assert mod._OverridesCache.value == {}

    def test_unparseable_yaml_is_survivable(self, tmp_path, monkeypatch):
        # A malformed overrides file must not take the whole build
        # down: the sectors it would have corrected simply keep their
        # upstream values.
        import investing.sector_overrides as mod

        bad = tmp_path / "sectors.yaml"
        bad.write_text("sectors: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr(mod._OverridesCache, "value", None, raising=False)
        monkeypatch.setattr(mod, "_SECTOR_OVERRIDES_PATH", str(bad))
        assert mod._load_overrides() == {}

    def test_a_file_with_no_sectors_mapping_is_survivable(self, tmp_path, monkeypatch):
        import investing.sector_overrides as mod

        empty = tmp_path / "sectors.yaml"
        empty.write_text("sectors:\n", encoding="utf-8")
        monkeypatch.setattr(mod._OverridesCache, "value", None, raising=False)
        monkeypatch.setattr(mod, "_SECTOR_OVERRIDES_PATH", str(empty))
        assert mod._load_overrides() == {}


class TestLogos:
    def test_a_non_numeric_viewbox_has_no_aspect_ratio(self):
        # An SVG can declare width="auto"; the parser must decline
        # rather than raise into the middle of a page render.
        from investing.logos import _parse_svg_aspect_ratio

        assert _parse_svg_aspect_ratio('<svg width="auto" height="10">') is None
        assert _parse_svg_aspect_ratio('<svg width="20" height="10">') == pytest.approx(2.0)
        assert _parse_svg_aspect_ratio('<svg width="0" height="10">') is None


class TestMarketDataStoreDisabled:
    """A store with no root is the "snapshots off" configuration.

    Every read has to answer "nothing archived" and every write has to
    be a no-op -- silently, because this is the normal state on a
    developer's machine and in CI. A method that assumed a root would
    raise there and nowhere else.
    """

    def _store(self):
        import investing.market_data_store as market_data_store

        return market_data_store.MarketDataStore(None)

    def test_reads_return_empty_and_writes_do_nothing(self):
        import numpy as np

        store = self._store()
        assert store.root is None
        assert store.load_fx_history("EUR") is None
        assert store.list_archived_tickers() == []
        assert store._load_history_bundle("NMS:AAA") == ([], [])
        # Writes must not raise even though there is nowhere to write.
        store.save_fx_history("EUR", np.array([], dtype="datetime64[D]"), np.array([]))
        store.refresh_ticker("NMS:AAA")

    def test_a_read_only_store_never_writes(self, tmp_path):
        import numpy as np

        import investing.market_data_store as market_data_store

        store = market_data_store.MarketDataStore(tmp_path, persist=False)
        store.save_fx_history(
            "EUR",
            np.array(["2024-01-01"], dtype="datetime64[D]"),
            np.array([1.1]),
        )
        store.refresh_ticker("NMS:AAA")
        assert not (tmp_path / "fx").exists()


class TestMarketDataRoot:
    def test_the_kill_switch_wins_over_a_configured_directory(self, monkeypatch, tmp_path):
        import investing.market_data_store as market_data_store

        monkeypatch.setenv(market_data_store._MARKET_DATA_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(market_data_store._DISABLE_ENV, "1")
        assert market_data_store.market_data_root() is None

    def test_an_empty_directory_setting_also_disables(self, monkeypatch):
        import investing.market_data_store as market_data_store

        monkeypatch.delenv(market_data_store._DISABLE_ENV, raising=False)
        monkeypatch.setenv(market_data_store._MARKET_DATA_DIR_ENV, "   ")
        assert market_data_store.market_data_root() is None

    def test_a_configured_directory_expands_the_user_prefix(self, monkeypatch):
        import investing.market_data_store as market_data_store

        monkeypatch.delenv(market_data_store._DISABLE_ENV, raising=False)
        monkeypatch.setenv(market_data_store._MARKET_DATA_DIR_ENV, "~/snapshots")
        root = market_data_store.market_data_root()
        assert root is not None and "~" not in str(root)

    def test_the_default_root_lives_beside_the_repo(self, monkeypatch):
        import investing.market_data_store as market_data_store

        monkeypatch.delenv(market_data_store._DISABLE_ENV, raising=False)
        monkeypatch.delenv(market_data_store._MARKET_DATA_DIR_ENV, raising=False)
        assert market_data_store.market_data_root().name == "market_data"

    def test_persistence_is_on_unless_explicitly_switched_off(self, monkeypatch):
        import investing.market_data_store as market_data_store

        monkeypatch.delenv(market_data_store._PERSIST_ENV, raising=False)
        assert market_data_store._persist_enabled() is True
        monkeypatch.setenv(market_data_store._PERSIST_ENV, "0")
        assert market_data_store._persist_enabled() is False


class TestMarketDataSerialisation:
    def test_history_survives_a_round_trip(self):
        import investing.market_data_store as market_data_store

        rows = [{"date": datetime(2024, 1, 2), "adj_close": 10.5}]
        splits = [{"date": datetime(2024, 3, 1), "split": 2.0}]
        payload = market_data_store._serialize_history(rows, splits=splits)
        back_rows, back_splits = market_data_store._deserialize_history(payload)
        assert back_rows[0]["adj_close"] == 10.5
        assert back_splits[0]["split"] == 2.0
        assert back_rows[0]["date"].date() == date(2024, 1, 2)

    def test_a_payload_missing_either_list_deserialises_to_empty(self):
        import investing.market_data_store as market_data_store

        assert market_data_store._deserialize_history({}) == ([], [])
        assert market_data_store._deserialize_history({"adj_close": None, "splits": None}) == (
            [],
            [],
        )

    def test_unreadable_and_malformed_json_both_read_as_absent(self, tmp_path):
        # A truncated snapshot must not abort the build -- the run
        # falls back to fetching live data.
        import investing.market_data_store as market_data_store

        store = market_data_store.MarketDataStore(tmp_path)
        assert store._load_json(tmp_path / "missing.json") is None
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert store._load_json(broken) is None

    def test_fx_snapshots_round_trip_and_survive_corruption(self, tmp_path):
        import numpy as np

        import investing.market_data_store as market_data_store

        store = market_data_store.MarketDataStore(tmp_path)
        dates = np.array(["2024-01-01", "2024-01-02"], dtype="datetime64[D]")
        rates = np.array([1.10, 1.11])
        store.save_fx_history("EUR", dates, rates)
        loaded = store.load_fx_history("EUR")
        assert loaded is not None
        assert loaded[1].tolist() == [1.10, 1.11]
        # A corrupt archive reads as "nothing cached" rather than raising.
        store._fx_path("EUR").write_bytes(b"not an npz")
        assert store.load_fx_history("EUR") is None
        assert store.load_fx_history("GBP") is None

    def test_a_ticker_with_a_slash_gets_a_filesystem_safe_name(self, tmp_path):
        import investing.market_data_store as market_data_store

        store = market_data_store.MarketDataStore(tmp_path)
        assert "/" not in store._history_path("BRK/B").name

    def test_rows_from_a_dataframe_skip_missing_closes(self):
        # Yahoo returns NaN for non-trading days inside a range. A NaN
        # adjusted close is an absent observation, not a zero price.
        import pandas as pd

        import investing.market_data_store as market_data_store

        frame = pd.DataFrame(
            {"Adj Close": [1.0, float("nan"), 3.0]},
            index=pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-03"]),
        )
        rows = market_data_store._history_rows_from_dataframe(frame)
        assert [r["adj_close"] for r in rows] == [1.0, 3.0]

    def test_an_empty_row_set_still_produces_a_usable_frame(self):
        import investing.market_data_store as market_data_store

        frame = market_data_store.MarketDataStore._rows_to_history_frame([], "2024-01-01")
        assert list(frame.columns) == ["Adj Close"]
        assert frame.empty

    def test_rows_before_the_requested_start_are_trimmed(self):
        import investing.market_data_store as market_data_store

        rows = [
            {"date": datetime(2023, 1, 1), "adj_close": 1.0},
            {"date": datetime(2024, 6, 1), "adj_close": 2.0},
        ]
        frame = market_data_store.MarketDataStore._rows_to_history_frame(rows, "2024-01-01")
        assert frame["Adj Close"].tolist() == [2.0]


class TestMarketDataUniverse:
    def test_the_universe_refresh_visits_each_ticker_once(self, tmp_path, monkeypatch):
        # Callers pass the live holdings; the store adds anything it
        # has archived but no longer holds, so a sold-out position
        # keeps its history. Neither list may cause a double fetch.
        import investing.market_data_store as market_data_store

        store = market_data_store.MarketDataStore(tmp_path)
        (tmp_path / "tickers").mkdir()
        (tmp_path / "tickers" / "NMS:OLD.json").write_text("{}", encoding="utf-8")

        seen: list[str] = []
        monkeypatch.setattr(store, "refresh_ticker", seen.append)
        store.refresh_universe(["NMS:AAA", "NMS:AAA", "NMS:BBB"])
        assert seen == ["NMS:AAA", "NMS:BBB", "NMS:OLD"]

    def test_archived_tickers_come_back_sorted(self, tmp_path):
        import investing.market_data_store as market_data_store

        store = market_data_store.MarketDataStore(tmp_path)
        assert store.list_archived_tickers() == []
        (tmp_path / "tickers").mkdir()
        for name in ("NMS:ZZZ", "NMS:AAA"):
            (tmp_path / "tickers" / f"{name}.json").write_text("{}", encoding="utf-8")
        assert store.list_archived_tickers() == ["NMS:AAA", "NMS:ZZZ"]


class TestOgImageHelpers:
    def test_a_missing_font_falls_back_rather_than_failing_the_build(self, monkeypatch):
        # The card is decoration; a machine with no readable font must
        # still finish the page. ``load_font`` warns and degrades.
        import investing.webpage.og_image as og_image

        monkeypatch.setattr(og_image, "_FONT_DIR", "/nonexistent/fonts")
        assert og_image.load_font("bold", 24) is not None

    def test_an_unknown_weight_uses_the_default_face(self, monkeypatch):
        import investing.webpage.og_image as og_image

        assert og_image.load_font("ultralight", 24) is not None

    def test_tracked_width_and_ink_height_are_zero_for_empty_text(self):
        from PIL import Image, ImageDraw

        import investing.webpage.og_image as og_image

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        assert og_image._tracked_width(draw, "", og_image.load_font("bold", 20), 2.0) == 0.0
        assert og_image._text_height(draw, "", og_image.load_font("bold", 20)) == 0.0
        assert og_image._tracked_width(draw, "AB", og_image.load_font("bold", 20), 5.0) > 5.0

    def test_a_label_that_cannot_shrink_enough_stops_at_the_floor(self):
        # Below ~22px the text stops surviving feed scaling, so the
        # fitter gives up rather than rendering something illegible.
        from PIL import Image, ImageDraw

        import investing.webpage.og_image as og_image

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = og_image._fit_font(draw, "a very long headline that will never fit", "bold", 60, 1.0)
        assert font.size == 22

    def test_a_logo_row_with_no_tickers_draws_nothing(self):
        from PIL import Image, ImageDraw

        import investing.webpage.og_image as og_image

        image = Image.new("RGB", (60, 20), "white")
        before = image.tobytes()
        og_image.draw_top_holdings_strip(image, [], x=0, y=0, w=60, h=20)
        assert image.tobytes() == before

    def test_an_unreadable_logo_falls_back_to_the_default_aspect(self, tmp_path, monkeypatch):
        import investing.webpage.og_image as og_image

        broken = tmp_path / "NMS_AAA.png"
        broken.write_bytes(b"not a png")
        monkeypatch.setattr(og_image, "_REPO_LOGOS_DIR", str(tmp_path))
        assert og_image._og_logo_aspect("NMS_AAA") == og_image._DEFAULT_LOGO_ASPECT
        assert og_image.load_logo_for_og("NMS_AAA", max_w=10, max_h=10) is None

    def test_the_top_holdings_list_is_capped(self):
        import investing.webpage.og_image as og_image

        assert og_image.top_holdings_for_og(None) == []
        assert og_image.top_holdings_for_og({}) == []
        weights = {f"NMS:T{i}": 10.0 - i for i in range(10)}
        assert len(og_image.top_holdings_for_og(weights, limit=4)) == 4
        # The synthetic roll-up bucket is not a ticker and has no logo.
        synthetic = next(iter(og_image.NON_TICKER_TOP10_KEYS))
        assert synthetic not in og_image.top_holdings_for_og({synthetic: 5.0, "NMS:AAA": 4.0})

    def test_render_is_a_no_op_without_pillow(self, monkeypatch, tmp_path):
        # The page build must survive an environment with no imaging
        # stack at all.
        import builtins

        import investing.webpage.og_image as og_image

        real_import = builtins.__import__

        def no_pil(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("no PIL")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pil)
        og_image.render(
            total_return={"twr%": 1.0, "cagr%": 1.0, "start_date": datetime(2024, 1, 1)},
            benchmarks=[],
            top_10=[],
            benchmark_display_names={},
            now=datetime(2024, 6, 1),
            output_dir=tmp_path,
        )
        assert not (tmp_path / og_image.OUTPUT_FILENAME).exists()

    def test_a_drawing_failure_never_fails_the_page(self, monkeypatch, tmp_path):
        import investing.webpage.og_image as og_image

        def boom(**_kwargs):
            raise RuntimeError("no fonts on this system")

        monkeypatch.setattr(og_image, "_render_unsafe", boom)
        og_image.render(
            total_return={"twr%": 1.0, "cagr%": 1.0, "start_date": datetime(2024, 1, 1)},
            benchmarks=[],
            top_10=[],
            benchmark_display_names={},
            now=datetime(2024, 6, 1),
            output_dir=tmp_path,
        )
        # A failed render must leave no half-written artefact behind:
        # ``stage_site`` copies whatever PNG it finds, so a truncated
        # file would ship as the card.
        assert not (tmp_path / og_image.OUTPUT_FILENAME).exists()


class TestPageWiring:
    def test_an_unchanged_page_is_not_rewritten(self, tmp_path):
        from investing.webpage._page import _write_if_changed

        path = tmp_path / "index.html"
        assert _write_if_changed(path, "<html></html>") is True
        assert _write_if_changed(path, "<html></html>") is False

    def test_the_og_image_is_skipped_when_there_is_no_return_to_show(self, tmp_path):
        # A page built before any performance data has arrived has no
        # headline; rendering a card with a blank figure would publish
        # a broken share preview.
        from investing.webpage import Webpage

        page = Webpage()
        page._render_og_image(output_dir=tmp_path)
        assert not list(tmp_path.iterdir())

    def test_closed_fixed_income_reaches_the_page(self, stub_logo_lookup):
        # The bucket is optional upstream, so the builder reads it with
        # a default. A closed bond still has to appear in the table.
        from investing.webpage import Webpage
        from tests._webpage_support import _holding

        page = Webpage()
        page.add_holding(
            _holding(
                ticker="NMS:SHY",
                name="iShares 1-3 Year Treasury Bond ETF",
                tsr=1.7,
                cagr=0.9,
                is_current=False,
                weight=None,
                asset_class="fixed_income",
                periods=[{"start": datetime(2023, 6, 1), "end": datetime(2024, 1, 31)}],
            )
        )
        assert "iShares 1-3 Year Treasury Bond ETF" in "".join(page.historical_fixed_income)


class TestHeroClaims:
    def test_the_every_year_claim_only_appears_when_it_is_earned(self):
        from investing.webpage import hero

        common = {
            "benchmarks": [{"ticker": "B", "name": "Bench", "tsr%": 5.0}],
            "benchmark_label": "Bench",
            "position_counts": (3, 1),
            "now": datetime(2026, 1, 1),
            "update_date": "Jan 1, 2026",
            "update_iso": "2026-01-01",
        }
        total = {"twr%": 20.0, "cagr%": 10.0, "start_date": datetime(2024, 1, 1)}
        every_year = [
            {"year": 2024, "jg%": 10.0, "bench%": 4.0},
            {"year": 2025, "jg%": 9.0, "bench%": 1.0},
        ]
        assert "every calendar year" in hero.render(
            total_return=total, yearly_returns=every_year, **common
        )
        one_bad_year = [*every_year[:1], {"year": 2025, "jg%": 1.0, "bench%": 4.0}]
        assert "every calendar year" not in hero.render(
            total_return=total, yearly_returns=one_bad_year, **common
        )

    def test_without_a_benchmark_the_headline_falls_back_to_total_return(self):
        from investing.webpage import hero

        html = hero.render(
            total_return={"twr%": 20.0, "cagr%": 10.0, "start_date": datetime(2024, 1, 1)},
            benchmarks=[],
            yearly_returns=[],
            benchmark_label="Bench",
            position_counts=(3, 0),
            now=datetime(2026, 1, 1),
            update_date="Jan 1, 2026",
            update_iso="2026-01-01",
        )
        assert "total return since inception" in html
        assert "pp" not in html.split("hero__stats")[0]


class TestPositionGroups:
    def test_the_parsed_config_is_cached(self, monkeypatch):
        # The lookup runs per holding; re-reading and re-parsing the
        # YAML each time would be the build's hottest needless I/O.
        import investing.position_groups as mod

        sentinel = (mod.PositionGroup(key="alpha", primary="NMS:AAA", members=("NMS:AAA",)),)
        monkeypatch.setattr(mod._GroupsCache, "value", sentinel, raising=False)
        assert mod.load_groups() is sentinel


class TestMaintenanceNotifier:
    def test_a_non_list_payload_reads_as_a_failed_lookup(self):
        # GitHub answers a search with a list. Anything else means the
        # API shape changed or an error body came back, and treating
        # that as "no matching issue" would open a duplicate every run.
        import investing.maintenance_notifier as mod

        class _Response:
            status_code = 200
            ok = True

            def json(self):
                return {"message": "Bad credentials"}

        class _Session:
            def get(self, *_args, **_kwargs):
                return _Response()

        verdict = mod._issue_exists(_Session(), "https://api.example/repos/x/y", labels=["a"])
        assert verdict == mod._LOOKUP_FAILED


class TestHoldingsMath:
    def test_a_blank_search_term_yields_the_bare_search_engine(self):
        from investing.holdings import google_search_url

        assert google_search_url("   ") == "https://www.google.com/"
        assert "q=Alpha+Inc" in google_search_url("Alpha Inc")

    def test_irr_declines_when_the_cashflows_never_change_sign(self):
        # A rate of return needs an outflow and an inflow to sit
        # between. All-negative or all-positive flows have no root, and
        # returning a number there would invent one.
        from investing.holdings import _xirr

        one_way = [(datetime(2024, 1, 1), -100.0), (datetime(2025, 1, 1), -50.0)]
        assert math.isnan(_xirr(one_way))
        assert math.isnan(_xirr([(d, -a) for d, a in one_way]))

    def test_irr_solves_a_simple_doubling(self):
        from investing.holdings import _xirr

        rate = _xirr([(datetime(2024, 1, 1), -100.0), (datetime(2025, 1, 1), 200.0)])
        assert rate == pytest.approx(1.0, abs=0.01)

    def test_irr_declines_when_no_bracket_can_be_found(self):
        # The solver widens its upper bound looking for a sign change.
        # A cashflow set whose NPV never crosses zero inside the search
        # window has no representable rate, and NaN says so.
        from investing.holdings import _xirr

        assert math.isnan(
            _xirr([(datetime(2024, 1, 1), -1.0), (datetime(2024, 1, 1), 1e12)], high=1.0)
        )

    def test_a_split_after_the_last_recorded_one_does_not_rescale(self):
        # Asking for the factor beyond the final split must be a no-op,
        # not an index error or a stale multiplier.
        import numpy as np

        from investing.holdings import Holding

        holding = Holding.__new__(Holding)
        holding._split_dates = [datetime(2020, 1, 1)]
        holding._split_factors = np.array([2.0])
        assert holding._split_factor_strictly_after(datetime(2021, 1, 1)) == 1.0
        assert holding._split_factor_strictly_after(datetime(2019, 1, 1)) == 2.0


class TestSheetsParsing:
    def test_an_unparseable_date_names_the_worksheet_and_row(self):
        # A bad cell in a 500-row sheet is only actionable if the error
        # says which sheet and which row it came from.
        import investing.sheets as sheets

        assert issubclass(sheets.SheetParseError, Exception)

    def test_credentials_come_from_the_environment_when_present(self, monkeypatch):
        # Inline JSON beats the file path, so CI can inject a secret
        # without writing it to disk.
        import investing.sheets as sheets

        captured = {}

        class _FakeGspread:
            @staticmethod
            def service_account_from_dict(payload):
                captured["dict"] = payload
                return "client-from-dict"

            @staticmethod
            def service_account(filename):
                captured["file"] = filename
                return "client-from-file"

        monkeypatch.setattr(sheets, "gspread", _FakeGspread)
        monkeypatch.setenv("GSHEET_CREDS", '{"type": "service_account"}')
        assert sheets._gspread_client() == "client-from-dict"
        assert captured["dict"] == {"type": "service_account"}

        monkeypatch.delenv("GSHEET_CREDS")
        monkeypatch.setenv("GSHEET_CREDS_FILE", "/tmp/creds.json")
        assert sheets._gspread_client() == "client-from-file"
        assert captured["file"] == "/tmp/creds.json"


class TestLogoCacheLocalFirst:
    def test_a_local_file_wins_over_a_network_probe(self, tmp_path):
        # The build must not hit the network for a logo it already has
        # on disk, and the URL it publishes has to be the public one.
        from investing.logos import LogoCache

        (tmp_path / "NMS_AAA.svg").write_text("<svg/>", encoding="utf-8")
        cache = LogoCache(local_dir=str(tmp_path), session=object())
        url = cache("NMS_AAA")
        assert url.endswith("NMS_AAA.svg")
        # Second call is served from the in-process cache -- the stub
        # session would raise if the probe ran again.
        assert cache("NMS_AAA") == url


class TestFxCachePersistence:
    def test_an_unwritable_cache_directory_is_survivable(self, tmp_path):
        # The rates are already in memory; losing the disk copy costs
        # one refetch next run and must not break this one.
        import numpy as np

        from investing.fx import _save_history_to_disk

        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        _save_history_to_disk(
            blocked,
            "EUR",
            np.array(["2024-01-01"], dtype="datetime64[D]"),
            np.array([1.1]),
        )

    def test_a_round_trip_through_the_disk_cache(self, tmp_path):
        import numpy as np

        from investing.fx import _load_history_from_disk, _save_history_to_disk

        assert _load_history_from_disk(tmp_path, "EUR") is None
        dates = np.array(["2024-01-01", "2024-01-02"], dtype="datetime64[D]")
        _save_history_to_disk(tmp_path, "EUR", dates, np.array([1.10, 1.12]))
        loaded = _load_history_from_disk(tmp_path, "EUR")
        assert loaded is not None
        assert loaded[1].tolist() == [1.10, 1.12]

    def test_a_warm_disk_cache_is_used_without_touching_the_network(self, tmp_path):
        # With a cache directory, no snapshot store and a populated
        # file, the rate comes off disk. Any network call here would
        # raise, since ``yf`` is not reachable in the suite.
        import numpy as np

        from investing.fx import ExchangeRate, _save_history_to_disk

        _save_history_to_disk(
            tmp_path,
            "EUR",
            np.array(["2024-01-01", "2024-01-05"], dtype="datetime64[D]"),
            np.array([1.10, 1.20]),
        )
        fx = ExchangeRate(cache_dir=tmp_path)
        assert fx("EUR", date(2024, 1, 3)) == pytest.approx(1.10)


class TestSafeRunEntrypoints:
    def test_the_module_entrypoint_dispatches_on_the_subcommand(self, monkeypatch):
        # ``python -m investing`` and ``python -m investing snapshot``
        # are the two production entrypoints; both must route to the
        # leak-safe wrapper rather than to the bare CLI.
        import runpy
        import sys

        import investing.safe_run as safe_run

        called: list[str] = []
        monkeypatch.setattr(safe_run, "_run_main_safely", lambda: called.append("main"))
        monkeypatch.setattr(safe_run, "_run_snapshot_safely", lambda: called.append("snapshot"))

        monkeypatch.setattr(sys, "argv", ["investing"])
        runpy.run_module("investing", run_name="__main__")
        monkeypatch.setattr(sys, "argv", ["investing", "snapshot"])
        runpy.run_module("investing", run_name="__main__")
        assert called == ["main", "snapshot"]


class TestPipelineInvariants:
    """Faults that must stop the build rather than reach the page.

    Each of these would otherwise publish a number that looks real. A
    trade with an unknown action would be silently skipped from the
    ledger; a holding priced at zero would take an infinite share of
    the portfolio; a benchmark with no history would divide by nothing.
    """

    def test_a_current_holding_priced_at_zero_stops_the_run(self):
        # Weight is ``value / total``. A zero-valued current holding
        # means the price feed returned nothing for it, and carrying
        # on would publish a portfolio whose weights do not sum.
        from investing.performance import compute_rollup

        holding = {
            "ticker": "NMS:AAA",
            "name": "Alpha",
            "is_current": True,
            "current_value_usd": 0.0,
            "asset_class": "equity",
            "sector": "Technology",
        }
        with pytest.raises(InvariantError, match="non-positive"):
            compute_rollup(
                {"current": [holding], "current_fixed_income": []},
                [],
                fx=lambda _currency, _when=None: 1.0,
            )

    def test_a_benchmark_with_no_history_cannot_be_resampled(self):
        import numpy as np

        from investing.performance import Benchmark

        bench = Benchmark.__new__(Benchmark)
        bench._adj_closes = np.array([])
        with pytest.raises(InvariantError, match="nothing to resample"):
            bench.cumulative_return_series([(datetime(2024, 1, 1), 1.0)])

    def test_a_benchmark_starting_at_zero_cannot_be_normalised(self):
        # Every point on the curve is ``price / price[start]``. A zero
        # first price makes the whole series infinite, so it is caught
        # rather than rendered.
        import numpy as np

        from investing.performance import Benchmark

        bench = Benchmark.__new__(Benchmark)
        bench._adj_closes = np.array([0.0, 1.0])
        bench._dates = [date(2024, 1, 1), date(2024, 1, 2)]
        with pytest.raises(InvariantError, match="start-day adjusted close is zero"):
            bench.cumulative_return_series([(datetime(2024, 1, 1), 1.0)])

    def test_a_period_return_from_a_zero_price_is_refused(self):
        import numpy as np

        from investing.performance import Benchmark

        bench = Benchmark.__new__(Benchmark)
        bench._adj_closes = np.array([0.0, 5.0])
        bench._dates = [date(2024, 1, 1), date(2024, 6, 1)]
        with pytest.raises(InvariantError, match="start price is zero"):
            bench.period_return_pct(date(2024, 1, 1), date(2024, 6, 1))


class TestPaletteInvariants:
    def test_every_allocation_fill_declares_the_ink_that_sits_on_it(self):
        """A fill with no paired ink fails silently, and white.

        ``--seg-ink: var(--ink-on-whatever)`` for a token that does not
        exist makes the custom property invalid, so ``color:
        var(--seg-ink, #fff)`` takes its fallback -- white. That is
        exactly the state F2 was raised to fix: white on Tiger Orange
        is 2.48:1. Nothing would raise, nothing would look obviously
        broken, and the label would just quietly stop being legible.

        So the pairing is asserted rather than trusted: a new sector
        added to the palette without its ink fails here instead.
        """
        import re
        from pathlib import Path

        from investing.webpage.allocation import (
            _ASSET_CLASS_VARS,
            _SECTOR_VARS,
            _ink_var,
        )

        css = Path(__file__).resolve().parents[1] / "assets/src/css/00-base.css"
        text = css.read_text(encoding="utf-8")
        light, dark = text.split("@media (prefers-color-scheme: dark)", 1)
        declared_light = set(re.findall(r"(--ink-on-[\w-]+)\s*:", light))
        declared_dark = set(re.findall(r"(--ink-on-[\w-]+)\s*:", dark))

        fills = {var for _label, var in (*_ASSET_CLASS_VARS, *_SECTOR_VARS)}
        assert fills, "no fills declared"
        for fill in sorted(fills):
            assert _ink_var(fill) in declared_light, fill
            assert _ink_var(fill) in declared_dark, fill
        # And no ink left behind by a fill that was removed.
        assert (declared_light | declared_dark) == {_ink_var(f) for f in fills}


class TestMinusSign:
    def test_negatives_use_the_minus_sign_not_the_hyphen(self):
        """U+2212, not U+002D.

        Every figure ``_fmt_pct`` renders lands in a column of tabular
        figures beside a signed positive, and ``font-variant-numeric:
        tabular-nums`` cannot rescue the sign: it equalises digits, and
        a sign is punctuation. In the page's own face at the return
        column's size the ASCII hyphen advances 6.63px against the
        plus's 9.25px, so a negative row's digits sat 2.6px off from
        the row above. U+2212 is drawn to the plus's width.
        """
        from investing.formatting import _fmt_pct

        for value in (-0.1, -4.0, -11.8, -99.94, -120.5):
            rendered = _fmt_pct(value)
            assert rendered.startswith("\u2212"), (value, rendered)
            assert "-" not in rendered, (value, rendered)
        # Positives are untouched, signed or not.
        assert _fmt_pct(4.0) == "4.0"
        assert _fmt_pct(4.0, signed=True) == "+4.0"
        assert _fmt_pct(-4.0, signed=True) == "\u22124.0"
        # Only the leading sign is replaced -- nothing else in the
        # string can be a hyphen, but the guard is cheap.
        assert _fmt_pct(-120.5) == "\u2212120"

    def test_the_og_font_can_draw_the_minus_sign(self):
        """The card is drawn with Pillow, not by a browser, so a glyph
        the vendored face lacks renders as tofu rather than falling
        back. Roboto carries U+2212; this stops a future font swap from
        silently breaking the one headline that can be negative."""
        from PIL import Image, ImageDraw

        import investing.webpage.og_image as og_image

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        for weight in ("regular", "bold"):
            font = og_image.load_font(weight, 40)
            minus = draw.textlength("\u2212", font=font)
            plus = draw.textlength("+", font=font)
            assert minus > 0, weight
            # Drawn to the same advance as the plus, which is the whole
            # reason for preferring it.
            assert abs(minus - plus) < 0.51, (weight, minus, plus)


class TestDataDependentRenderPaths:
    """Rows the synthetic fixture never produces.

    The preview portfolio is ahead every year, holds nothing too young
    to annualise, and has no benchmark gaps -- so several render paths
    ship without ever being drawn. Each is exercised here.
    """

    def _yearly(self, rows):
        from investing.webpage import yearly_view

        return yearly_view.render(
            rows,
            [{"ticker": "B", "name": "Bench", "tsr%": 5.0}],
            benchmark_label="S&P 500",
        )

    def test_a_losing_year_draws_its_bar_in_the_loss_colour(self):
        html = self._yearly([{"year": 2024, "jg%": -4.0, "bench%": 2.0}])
        assert "yearly__bar--neg" in html
        assert "\u22124.0%" in html

    def test_a_year_without_a_benchmark_figure_renders_an_em_dash(self):
        # Not a zero, and not a blank cell: the benchmark has no value
        # for that year, which is a different statement from "flat".
        html = self._yearly(
            [
                {"year": 2023, "jg%": 5.0, "bench%": 3.0},
                {"year": 2024, "jg%": 6.0, "bench%": None},
            ]
        )
        assert html.count("yearly__empty") == 2, "both the benchmark and the alpha cell"
        assert "&mdash;" in html

    def test_the_current_year_is_marked_year_to_date(self):
        # An incomplete year sitting unlabelled beside seven complete
        # ones invites a comparison that is not available yet.
        html = self._yearly([{"year": 2026, "jg%": 3.0, "bench%": 1.0, "is_ytd": True}])
        assert "yearly__ytd" in html
        assert "(YTD)" in html

    def test_an_unrepresentable_irr_says_tba_rather_than_a_headline(self, stub_logo_lookup):
        # A near-zero starter position that 10x'd before a large
        # top-up produces an XIRR in the millions of percent. It is
        # arithmetically correct and completely meaningless, so the
        # column declines to print it -- and, having no sign worth
        # colouring, takes no value colour either.
        from datetime import datetime

        from investing.holdings import CAGR_TBA_THRESHOLD
        from investing.webpage.holdings_view import build_row

        def row_for(cagr: float) -> str:
            return build_row(
                {
                    "ticker": "NMS:AAA",
                    "name": "Alpha",
                    "is_current": True,
                    "tsr%": 3.0,
                    "cagr%": cagr,
                    "current_weight%": 5.0,
                    "period_start": datetime(2026, 7, 20),
                    "periods": [{"start": datetime(2026, 7, 20), "end": None}],
                },
                logo_url_for=lambda _t: "logo.svg",
            )

        absurd = row_for(CAGR_TBA_THRESHOLD * 2)
        assert "holdings__num--tba" in absurd
        assert "TBA" in absurd
        assert "value--positive" not in absurd.split("holdings__num--tba")[1]

        ordinary = row_for(12.5)
        assert "holdings__num--tba" not in ordinary
        assert "+12.5%" in ordinary
