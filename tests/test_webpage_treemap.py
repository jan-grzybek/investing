"""Equity sector treemap and fixed-income section."""

from __future__ import annotations

import re
from datetime import datetime

from investing.webpage import Webpage
from tests._webpage_support import (
    AspectStubCache,
    _holding,
    _total_return,
    _trade_event,
    stub_logo_lookup,
    treemap_layout_block,
)


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
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha", weight=60.0, sector="Technology"))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta", weight=40.0, sector="Healthcare"))
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
        # Payload + empty canvas shell; tiles paint client-side.
        assert '<figure class="treemap"' in out
        assert 'class="treemap__canvas"' in out
        assert 'class="treemap__payload"' in out
        holdings = [
            _holding(ticker="NMS:AAA", name="Alpha", weight=60.0, sector="Technology"),
            _holding(ticker="NMS:BBB", name="Beta", weight=40.0, sector="Healthcare"),
        ]
        block = treemap_layout_block(holdings)
        assert block.count('class="treemap__tile"') == 2
        assert 'data-sector="Technology"' in block
        assert 'data-sector="Healthcare"' in block
        # The historical-only ticker never receives a tile because
        # ``add_holding`` filters on ``is_current`` before pushing
        # onto the treemap list, and an absent ``current_weight%``
        # would still be rejected by the renderer's weight guard.
        assert (
            'href="#holding-NMS-OLD"'
            not in out[
                out.index('<figure class="treemap"') : out.index(
                    "</figure>", out.index('<figure class="treemap"')
                )
            ]
        )

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
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha", weight=70.0, sector="Technology"))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta", weight=30.0, sector="Technology"))
        w.save()

        holdings = [
            _holding(ticker="NMS:AAA", name="Alpha", weight=70.0, sector="Technology"),
            _holding(ticker="NMS:BBB", name="Beta", weight=30.0, sector="Technology"),
        ]
        treemap_block = treemap_layout_block(holdings)
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
        w.add_holding(_holding(ticker="NMS:AAA", name="Alpha", weight=50.0, sector=""))
        w.add_holding(_holding(ticker="NMS:BBB", name="Beta", weight=50.0, sector="Technology"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'class="treemap__payload"' in out
        block = treemap_layout_block(
            [
                _holding(ticker="NMS:AAA", name="Alpha", weight=50.0, sector=""),
                _holding(ticker="NMS:BBB", name="Beta", weight=50.0, sector="Technology"),
            ]
        )
        assert 'data-sector="Other"' in block
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
        assert 'class="treemap__payload"' in out
        block = treemap_layout_block(
            [_holding(ticker="NMS:AAA", name="Alpha Inc.", weight=100.0, sector="Technology")]
        )
        assert 'aria-label="NMS:AAA - Alpha Inc. (Technology): 100%"' in block
        assert 'title="NMS:AAA - Alpha Inc. (Technology): 100%"' in block

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

        block = treemap_layout_block(
            [
                _holding(ticker="NMS:AAA", weight=60.0, sector="Technology"),
                _holding(ticker="NMS:BBB", weight=40.0, sector="Technology"),
            ]
        )
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
        cache = AspectStubCache(aspect_table)
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

        holdings = []
        for ticker in aspect_table:
            holdings.append(
                _holding(
                    ticker=ticker,
                    weight=100.0 / len(aspect_table),
                    sector="Technology",
                )
            )
        block = treemap_layout_block(
            holdings,
            logo_url_for=cache,
            logo_aspect_for=cache.aspect_ratio,
        )
        # For each tile, parse the inline factors off the <img>
        for ticker in aspect_table:
            label_idx = block.index(f'aria-label="{ticker}')
            tile = block[block.rfind("<a", 0, label_idx) : block.index("</a>", label_idx)]
            w_factor = float(re.search(r"--logo-w-factor:\s*([\d.]+)", tile).group(1))
            h_factor = float(re.search(r"--logo-h-factor:\s*([\d.]+)", tile).group(1))
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
        wide_tile = block[block.rfind("<a", 0, wide_tile_idx) : block.index("</a>", wide_tile_idx)]
        narrow_tile = block[
            block.rfind("<a", 0, narrow_tile_idx) : block.index("</a>", narrow_tile_idx)
        ]
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
        # the readability threshold once squarify subdivides the
        # sector (wide-but-thin strips fail the px-based probe even
        # when their canvas-% width looks generous).
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

        holdings = [_holding(ticker="NMS:HVY", weight=94.0, sector="Technology")]
        for i in range(10):
            holdings.append(
                _holding(
                    ticker=f"NMS:T{i:02d}",
                    weight=0.6,
                    sector="Technology",
                ),
            )
        block = treemap_layout_block(holdings)
        # The aggregated tile renders as a ``<div>`` (no holding
        # card to anchor to), uses the Other sector class hook, and
        # carries an ``Other`` label.
        assert '<div class="treemap__tile treemap__tile--aggregated"' in block
        assert 'data-sector="Other"' in block
        # The pseudo-row has no logo and no anchor; the tooltip lists
        # the folded tickers so a hover surfaces what got combined.
        agg_start = block.index("treemap__tile--aggregated")
        agg_tile = block[block.rfind("<div", 0, agg_start) : block.index("</div>", agg_start)]
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

        holdings = [_holding(ticker="NMS:HVY", weight=95.0, sector="Technology")]
        for i in range(5):
            holdings.append(
                _holding(
                    ticker=f"NMS:T{i}",
                    weight=1.0,
                    sector="Technology",
                ),
            )
        block = treemap_layout_block(holdings)
        # ``Other`` tile renders with the rolled-up weight.
        agg_idx = block.index("treemap__tile--aggregated")
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

        block = treemap_layout_block(
            [
                _holding(ticker="NMS:AAA", weight=50.0, sector="Technology"),
                _holding(ticker="NMS:BBB", weight=50.0, sector="Technology"),
            ]
        )
        assert "treemap__tile--aggregated" not in block

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
        # Two holdings so the >1 toolbar gate also fires; otherwise
        # the section's sort-toolbar slot disappears and the layout
        # contract below has nothing to anchor on.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_allocations(
            {"Equities": 100.0, "Cash & Cash Equivalents": 0.0},
            {"NMS:AAA": 60.0, "NMS:BBB": 40.0},
        )
        w.add_holding(
            _holding(ticker="NMS:AAA", weight=60.0, sector="Technology"),
        )
        w.add_holding(
            _holding(
                ticker="NMS:BBB",
                name="Beta",
                weight=40.0,
                sector="Healthcare",
            ),
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


class TestFixedIncomeSection:
    """The dedicated Fixed Income sub-section inside Current /
    Historical holdings. Mirrors the equity sub-section's capsule
    + sort affordances but skips the sector treemap (the chart
    exists to surface the equity sleeve's GICS-style sector
    composition, and bond / treasury tickers don't carry that
    signal).
    """

    def _fi(self, *, is_current=True, ticker="NMS:TLT", **kwargs):
        return _holding(
            ticker=ticker,
            asset_class="fixed_income",
            is_current=is_current,
            **kwargs,
        )

    def test_current_fi_subsection_renders_below_equities_with_no_treemap(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Adds an equity + a single FI position so both Current
        # sub-sections are present. The FI sub-section should sit
        # AFTER the Equities sub-section, carry its own ``<h3>``
        # subheading + capsule list, and NOT render a treemap.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA", weight=80.0, sector="Technology"))
        w.add_holding(self._fi(ticker="NMS:TLT", name="Treasuries", weight=20.0))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        equities_idx = out.index('id="equities" class="section__subtitle"')
        fi_idx = out.index('id="fixed-income" class="section__subtitle"')
        assert equities_idx < fi_idx
        # Treemap renders for equities; nothing FI-specific should
        # spawn another one. The single ``<figure class="treemap">``
        # in the document is the equity treemap.
        assert out.count('<figure class="treemap"') == 1
        # FI capsules feed their own list scope so the renderer
        # can wire sort toolbars per sub-section.
        assert 'data-holdings-list="current-fixed-income"' in out

    def test_fixed_income_subsection_omitted_when_no_fi_positions(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # An equity-only portfolio renders no Fixed Income subheading
        # under either Current or Historical -- empty sub-sections
        # contribute no titles to the DOM.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'id="fixed-income"' not in out
        assert 'id="historical-fixed-income"' not in out
        assert 'data-holdings-list="current-fixed-income"' not in out
        assert 'data-holdings-list="historical-fixed-income"' not in out

    def test_single_fi_position_omits_sort_toolbar(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Sort toolbar gates on >1 position; a single FI position
        # renders the heading + the capsule but no toolbar (sorting
        # one row is a no-op).
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(self._fi(ticker="NMS:TLT", weight=100.0))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Sub-section heading present.
        assert 'id="fixed-income"' in out
        # The list itself renders, but the toolbar does not.
        assert 'data-holdings-list="current-fixed-income"' in out
        assert 'data-holdings-sort="current-fixed-income"' not in out

    def test_two_fi_positions_emit_sort_toolbar(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # >1 position -> sort toolbar wired to the FI scope so the
        # script can reorder the FI list independently of the
        # equity list above.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(self._fi(ticker="NMS:TLT", name="Treasuries", weight=12.0))
        w.add_holding(self._fi(ticker="NMS:LQD", name="Corp Bonds", weight=8.0))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'data-holdings-sort="current-fixed-income"' in out
        # Toolbar carries the same Weight button as the equity
        # toolbar (FI capsules also carry a current weight).
        toolbar_idx = out.index('data-holdings-sort="current-fixed-income"')
        toolbar_end = out.index("</div>", toolbar_idx)
        toolbar = out[toolbar_idx:toolbar_end]
        assert 'data-holdings-sort-key="weight"' in toolbar

    def test_historical_fi_subsection_below_historical_equities(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Mirrors the Current behaviour for historical holdings:
        # Historical Equities -> Historical Fixed Income, both
        # gated on at least one position. The historical toolbar
        # never carries a Weight button (closed positions have no
        # current weight).
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            ),
        )
        w.add_holding(
            _holding(
                ticker="NMS:OLD2",
                name="Older",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2021, 1, 1), "end": datetime(2022, 1, 1)}],
            ),
        )
        w.add_holding(
            self._fi(
                ticker="NMS:SHY",
                name="Short Treasuries",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2023, 6, 1), "end": datetime(2024, 1, 31)}],
            ),
        )
        w.add_holding(
            self._fi(
                ticker="NMS:IEF",
                name="Intermediate Treasuries",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 6, 1), "end": datetime(2023, 6, 1)}],
            ),
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        eq_idx = out.index('id="historical-equities"')
        fi_idx = out.index('id="historical-fixed-income"')
        assert eq_idx < fi_idx
        # FI toolbar present (>1 row) but with no Weight button.
        toolbar_idx = out.index('data-holdings-sort="historical-fixed-income"')
        toolbar_end = out.index("</div>", toolbar_idx)
        toolbar = out[toolbar_idx:toolbar_end]
        assert 'data-holdings-sort-key="weight"' not in toolbar

    def test_allocation_chart_links_fixed_income_row_when_present(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # When the allocation dict carries a ``"Fixed Income"``
        # entry AND the renderer has at least one FI capsule, the
        # corresponding bar row is emitted as a link to the FI
        # sub-section (analogous to the equity row).
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_allocations(
            {"Equities": 70.0, "Fixed Income": 20.0, "Cash & Cash Equivalents": 10.0},
            {"NMS:AAA": 70.0},
        )
        w.add_holding(_holding(ticker="NMS:AAA", weight=70.0))
        w.add_holding(self._fi(ticker="NMS:TLT", weight=20.0))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'href="#equities"' in out
        assert 'href="#fixed-income"' in out
        # Cash row stays unlinked.
        cash_block = out.split("Cash &amp;", 1)[1].split("</div></div>", 1)[0]
        assert "bars__row--link" not in cash_block

    def test_allocation_chart_does_not_link_missing_fi_subsection(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Defensive guard: even if a caller hands the renderer an
        # allocation dict that names "Fixed Income" but never adds
        # any FI capsules (the sub-section therefore doesn't
        # render), the bar row must NOT carry an anchor pointing
        # at a missing target -- the row stays a plain ``<div>``.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_allocations(
            {"Equities": 70.0, "Fixed Income": 20.0, "Cash & Cash Equivalents": 10.0},
            {"NMS:AAA": 70.0},
        )
        w.add_holding(_holding(ticker="NMS:AAA", weight=70.0))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'href="#fixed-income"' not in out
        assert 'id="fixed-income"' not in out

    def test_nav_link_to_current_present_when_only_fi_positions(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # Asymmetric portfolio (no equities, only FI) still
        # surfaces the "Current" nav link and the "Current
        # holdings" section -- the gate looks at either bucket
        # being non-empty rather than the equity bucket alone.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(self._fi(ticker="NMS:TLT", weight=100.0))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'href="#current"' in out
        assert 'id="current"' in out
        assert "Current holdings" in out
        # Equities sub-section absent (no equity capsules).
        assert 'id="equities" class="section__subtitle"' not in out
        # FI sub-section is the only one in the section.
        assert 'id="fixed-income"' in out

    def test_trades_intermix_fi_and_equity_tickers(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # The Trades section reads as a chronological feed of
        # every executed fill; FI and equity entries appear in
        # the same table without an asset-class split -- the
        # renderer takes the events in whatever order
        # ``add_trades`` was called with.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.add_holding(self._fi(ticker="NMS:TLT"))
        w.add_trades(
            [
                _trade_event(ticker="NMS:AAA", name="Alpha Inc."),
                _trade_event(ticker="NMS:TLT", name="Treasuries"),
            ]
        )
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        # Both rows live inside a single ``<section id="trades">``
        # so the table renders as a single body. The trades table
        # strips the exchange prefix (``NMS:AAA`` -> ``AAA``) but
        # keeps the bare symbol in the ticker cell, so we anchor on
        # the symbol portion.
        trades_idx = out.index('id="trades"')
        trades_end = out.index("</section>", trades_idx)
        trades_block = out[trades_idx:trades_end]
        assert ">AAA<" in trades_block
        assert ">TLT<" in trades_block
