"""Holdings and Closed positions: rows, groups, and the sort contract."""

from __future__ import annotations

import math
from datetime import datetime

from investing.assets import _PAGE_STYLES
from investing.webpage import Webpage
from tests._css_helpers import (
    at_rule_body,
    blocks_for,
    contains_at_rule,
    contains_selector,
    normalize,
)
from tests._html_helpers import parse_html
from tests._webpage_support import (
    _holding,
    _total_return,
    stub_logo_lookup,
)


def _open_table(w: Webpage) -> str:
    return w._build_open_holdings_table()


def _closed_table(w: Webpage) -> str:
    return w._build_closed_holdings_table()


class TestAddHolding:
    def test_current_holding_appears_in_current_bucket(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(is_current=True))

        assert len(w.current) == 1
        assert w.historical == []
        assert 'class="holdings__row"' in w.current[0]
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
        # Closed positions have no weight, so no weight cell.
        assert "holdings__weight" not in w.historical[0]
        # Closed period renders a real end date, not "Present", in the
        # page-wide DD/MM/YYYY format.
        assert "01/01/2024" in w.historical[0]

    def test_cagr_above_sentinel_renders_as_tba(self, stub_logo_lookup):
        # The check uses `math.nextafter(1_000_000, 0)`; anything strictly
        # greater than that triggers the "TBA" branch.
        sentinel_cagr = 1_000_000  # > nextafter(1_000_000, 0)
        assert sentinel_cagr > math.nextafter(1_000_000, 0)

        w = Webpage()
        w.add_holding(_holding(cagr=sentinel_cagr))
        assert "TBA" in w.current[0]
        # "TBA" carries no direction, so it must not carry a sign
        # colour either.
        assert "value--positive" not in w.current[0].split("TBA")[0].rsplit("<td", 1)[1]

    def test_open_position_renders_held_since_not_present(self, stub_logo_lookup):
        # An open row answers "how long have you held this", which is
        # a date -- the "start to Present" range belongs to the closed
        # table, where the end date is the information.
        w = Webpage()
        w.add_holding(_holding(periods=[{"start": datetime(2024, 3, 7), "end": None}]))
        row = w.current[0]
        assert "Present" not in row
        assert '<time datetime="2024-03-07">07/03/2024</time>' in row

    def test_held_since_prefers_the_open_period(self, stub_logo_lookup):
        # A re-entered position has several windows; "Held since" means
        # the one that is still open, not the earliest ever.
        w = Webpage()
        w.add_holding(
            _holding(
                periods=[
                    {"start": datetime(2020, 1, 1), "end": datetime(2021, 1, 1)},
                    {"start": datetime(2024, 6, 1), "end": None},
                ],
            )
        )
        assert 'datetime="2024-06-01"' in w.current[0]
        assert 'data-sort-since="2024-06-01"' in w.current[0]

    def test_negative_holding_returns_get_negative_class(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(tsr=-5.0, cagr=-2.0))
        assert "value--negative" in w.current[0]

    def test_returns_are_signed_so_the_column_reads_as_a_column(self, stub_logo_lookup):
        # In a single column of numbers the sign is the comparison, so
        # gains carry an explicit "+" rather than leaving the reader to
        # infer direction from the colour alone.
        w = Webpage()
        w.add_holding(_holding(tsr=12.3, cagr=4.5))
        row = w.current[0]
        assert "+12.3%" in row
        assert "+4.5%" in row

    def test_weight_publishes_its_raw_value_for_the_css_bar(self, stub_logo_lookup):
        # The bar's width is ``--w / --holdings-weight-scale`` in CSS,
        # so the row publishes its own weight and the table publishes
        # the page-wide maximum. Doing the division in CSS is what lets
        # a row render before the renderer has seen the whole book.
        w = Webpage()
        w.add_holding(_holding(weight=10.0))
        assert 'style="--w: 10.00"' in w.current[0]
        assert "10.0%" in w.current[0]

    def test_table_publishes_the_largest_weight_as_the_bar_scale(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA", weight=21.4))
        w.add_holding(_holding(ticker="NMS:BBB", weight=4.1))
        assert "--holdings-weight-scale: 21.40" in _open_table(w)

    def test_closed_table_publishes_no_weight_scale(self, stub_logo_lookup):
        # No weight column, so no scale -- publishing one would be a
        # division waiting to happen against nothing.
        w = Webpage()
        w.add_holding(
            _holding(
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        assert "--holdings-weight-scale" not in _closed_table(w)

    def test_fixed_income_weight_bar_uses_the_neutral_fill(self, stub_logo_lookup):
        # The accent means "equity sleeve" everywhere else on the page.
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:TLT", asset_class="fixed_income"))
        assert "holdings__bar-fill--muted" in w.current_fixed_income[0]

    def test_equity_weight_bar_uses_the_accent_fill(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding())
        assert "holdings__bar-fill--muted" not in w.current[0]
        assert "holdings__bar-fill" in w.current[0]


class TestClosedPeriods:
    def test_period_dates_are_wrapped_in_time_elements(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(
            _holding(
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 5, 4), "end": datetime(2023, 6, 9)}],
            )
        )
        row = w.historical[0]
        assert '<time datetime="2022-05-04">04/05/2022</time>' in row
        assert '<time datetime="2023-06-09">09/06/2023</time>' in row

    def test_multiple_periods_stack_newest_first(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(
            _holding(
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 8, 5), "end": datetime(2023, 6, 9)},
                    {"start": datetime(2025, 7, 22), "end": datetime(2025, 12, 30)},
                ],
            )
        )
        row = w.historical[0]
        assert row.index("2025-07-22") < row.index("2022-08-05")
        assert row.count("<li>") == 2

    def test_closed_rows_sort_by_their_most_recent_exit(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(
            _holding(
                is_current=False,
                weight=None,
                periods=[
                    {"start": datetime(2022, 8, 5), "end": datetime(2023, 6, 9)},
                    {"start": datetime(2025, 7, 22), "end": datetime(2025, 12, 30)},
                ],
            )
        )
        assert 'data-sort-held="2025-12-30"' in w.historical[0]


class TestRowContract:
    def test_holding_logo_has_lazy_loading_and_dimensions(self, stub_logo_lookup):
        # ``loading="lazy"`` defers below-the-fold loads, ``decoding=
        # "async"`` keeps decode off the main thread, and explicit
        # ``width``/``height`` reserve the box before the image arrives
        # (zero CLS across a twelve-row table).
        w = Webpage()
        w.add_holding(_holding())
        row = w.current[0]
        assert 'class="holdings__logo"' in row
        assert 'loading="lazy"' in row
        assert 'decoding="async"' in row
        # A landscape cell, not a square one: most holding marks are
        # wordmarks and a square box letterboxes them into invisibility.
        # The attributes have to state the box the stylesheet actually
        # draws -- their whole job is to reserve it before the image
        # arrives, so a stale pair reserves the wrong space and
        # reintroduces the layout shift they exist to prevent.
        assert 'width="34"' in row
        assert 'height="22"' in row

    def test_fallback_logo_opts_out_of_the_dark_mode_inversion(self, stub_logo_lookup):
        # The placeholder is a coloured illustration, not a single-hue
        # wordmark, so negating its luminance destroys it. The opt-out
        # is a class rather than a match on the URL's suffix, which
        # stops matching the moment the URL changes shape.
        from investing.paths import COURAGE_LOGO
        from investing.webpage.holdings_view import _logo_cell

        fallback = _logo_cell(logo_url=COURAGE_LOGO, website_url="#", company_name="X")
        assert "holdings__logo--literal" in fallback

        real = _logo_cell(
            logo_url="https://example.test/logos/tight/NMS%3AAAA.svg",
            website_url="#",
            company_name="X",
        )
        assert "holdings__logo--literal" not in real

    def test_weight_grid_is_inside_the_cell_not_on_it(self, stub_logo_lookup):
        # A ``display: grid`` table cell leaves the table's formatting
        # context, so ``vertical-align`` stops applying and the weight
        # drifts off the baseline its row-mates sit on.
        w = Webpage()
        w.add_holding(_holding(weight=10.0))
        assert (
            '<td class="holdings__weight" role="cell"><span class="holdings__weight-grid">'
        ) in w.current[0]

    def test_logo_links_to_the_issuer_in_a_new_tab(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(name="Alpha", website="https://www.alpha.example"))
        row = w.current[0]
        assert 'href="https://www.alpha.example"' in row
        assert 'target="_blank"' in row
        assert 'rel="noopener noreferrer"' in row
        # Decorative alt="" means the link needs its own name.
        assert 'aria-label="Open Alpha website"' in row

    def test_row_carries_anchor_id(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA"))
        row = w.current[0]
        assert f' id="{Webpage._holding_anchor("NMS:AAA")}"' in row
        # The slug strips punctuation that would otherwise need to be
        # percent-encoded inside a URL fragment.
        assert "holding-NMS-AAA" in row

    def test_closed_row_also_carries_anchor_id(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        assert ' id="holding-NMS-OLD"' in w.historical[0]

    def test_open_row_carries_sort_attributes(self, stub_logo_lookup):
        # The header re-orders rows by reading ``data-sort-*``; sanity
        # -check the contract so the script (which has no Python
        # visibility into the values) lines up with what is emitted.
        w = Webpage()
        w.add_holding(
            _holding(
                ticker="NMS:NVDA",
                name="NVIDIA Corporation",
                tsr=217.4,
                cagr=64.2,
                weight=21.4,
                periods=[{"start": datetime(2024, 8, 14), "end": None}],
            )
        )
        row = w.current[0]
        # No ticker sort key: rows are identified by company name, and
        # a combined position has no single ticker to sort by honestly.
        assert "data-sort-ticker" not in row
        # Names case-fold so a name sort reads as a clean A->Z run.
        assert 'data-sort-name="nvidia corporation"' in row
        # Numeric keys use a fixed-decimal serialisation so int / float
        # upstream values render identically and the JS can parseFloat.
        assert 'data-sort-tsr="217.4000"' in row
        assert 'data-sort-cagr="64.2000"' in row
        assert 'data-sort-weight="21.4000"' in row
        # Dates sort ISO so a lexical compare is a chronological one.
        assert 'data-sort-since="2024-08-14"' in row

    def test_closed_row_omits_weight_sort_key(self, stub_logo_lookup):
        # Closed positions have no ``current_weight%``, so the row must
        # not advertise a weight key -- the closed table omits the
        # column, but a stray attribute would still be a lie in the DOM.
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
        row = w.historical[0]
        assert 'data-sort-name="old co."' in row
        assert 'data-sort-tsr="-12.5000"' in row
        assert 'data-sort-cagr="-7.3000"' in row
        assert "data-sort-weight" not in row
        assert "data-sort-since" not in row

    def test_name_leads_and_the_listing_follows(self, stub_logo_lookup):
        # The Holdings section answers "what do I own", and the answer
        # is a company; the listing is a detail of the transaction, so
        # it sits below at a smaller size rather than leading the row.
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:NVDA", name="NVIDIA Corporation"))
        row = w.current[0]
        assert '<span class="holdings__name">NVIDIA Corporation</span>' in row
        assert '<span class="holdings__ticker">NMS:NVDA</span>' in row
        assert row.index("holdings__name") < row.index("holdings__ticker")

    def test_combined_position_names_every_listing(self, stub_logo_lookup):
        # No single symbol identifies a position assembled from several
        # listings, so the row names all of them.
        w = Webpage()
        holding = _holding(ticker="DUS:SSU.DU", name="Samsung Electronics")
        holding["tickers"] = ["DUS:SSU.DU", "IOB:SMSN.IL"]
        w.add_holding(holding)
        assert "DUS:SSU.DU + IOB:SMSN.IL" in w.current[0]


class TestTableAssembly:
    def test_open_table_groups_equities_and_fixed_income(self, stub_logo_lookup):
        w = Webpage()
        w.add_allocations(
            {"Equities": 78.7, "Fixed Income": 10.7, "Cash & Cash Equivalents": 10.6},
            None,
        )
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.add_holding(_holding(ticker="NMS:TLT", asset_class="fixed_income"))
        table = _open_table(w)
        soup = parse_html(table)
        sections = soup.find_all("tbody", class_="holdings__section")
        assert len(sections) == 2
        bands = [s.find("tr", class_="holdings__band").get_text() for s in sections]
        assert bands[0] == "Equities · 78.7% of portfolio"
        # Cash is a residual with no rows of its own, so it rides along
        # with fixed income rather than being the one allocation slice
        # with nowhere in the table to land.
        assert bands[1] == "Fixed Income · 10.7% of portfolio · Cash 10.6%"

    def test_band_share_comes_from_the_rollup_not_a_row_sum(self, stub_logo_lookup):
        # Two numbers on one page that disagree about what 100% means
        # is exactly the defect this redesign set out to fix, so the
        # band quotes the same denominator the allocation bar uses.
        w = Webpage()
        w.add_allocations({"Equities": 78.7}, None)
        w.add_holding(_holding(weight=21.4))
        assert "Equities · 78.7% of portfolio" in _open_table(w)

    def test_band_falls_back_to_a_count_without_a_rollup(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding(ticker="NMS:AAA"))
        w.add_holding(_holding(ticker="NMS:BBB"))
        assert "Equities · 2 positions" in _open_table(w)

    def test_empty_group_renders_nothing(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding())
        table = _open_table(w)
        assert "Fixed Income" not in table
        assert len(parse_html(table).find_all("tbody", class_="holdings__section")) == 1

    def test_no_holdings_renders_no_table(self):
        assert _open_table(Webpage()) == ""
        assert _closed_table(Webpage()) == ""

    def test_open_table_headers_carry_the_sort_contract(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(_holding())
        soup = parse_html(_open_table(w))
        heads = soup.find_all("th", attrs={"data-sort-key": True})
        assert [th["data-sort-key"] for th in heads] == [
            "name",
            "since",
            "weight",
            "tsr",
            "cagr",
        ]
        # ``kind`` is what makes the first click on a column land in
        # the direction that datatype reads naturally in.
        kinds = {th["data-sort-key"]: th["data-sort-kind"] for th in heads}
        assert kinds == {
            "name": "text",
            "since": "text",
            "weight": "number",
            "tsr": "number",
            "cagr": "number",
        }
        # One source of truth for "which column is sorted, which way".
        assert all(th["aria-sort"] == "none" for th in heads)

    def test_closed_table_swaps_since_and_weight_for_dates_held(self, stub_logo_lookup):
        w = Webpage()
        w.add_holding(
            _holding(
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        soup = parse_html(_closed_table(w))
        keys = [th["data-sort-key"] for th in soup.find_all("th", attrs={"data-sort-key": True})]
        assert keys == ["name", "held", "tsr", "cagr"]

    def test_band_spans_every_column(self, stub_logo_lookup):
        # A short colspan would leave the band ending mid-table, which
        # reads as a broken row rather than as a group header.
        w = Webpage()
        w.add_holding(_holding())
        soup = parse_html(_open_table(w))
        band = soup.find("tr", class_="holdings__band").find("td")
        header_cells = soup.find("thead").find_all("th")
        assert int(band["colspan"]) == len(header_cells)

    def test_tables_carry_a_screen_reader_caption(self, stub_logo_lookup):
        # The visible heading is an ``<h2>`` outside the table, but a
        # table still owes assistive tech a name of its own -- and
        # "Holdings" alone does not distinguish open from closed.
        w = Webpage()
        w.add_holding(_holding())
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        assert "<caption" in _open_table(w)
        assert "Current holdings" in _open_table(w)
        assert "Closed positions" in _closed_table(w)


class TestHoldingsStyles:
    def test_weight_bar_divides_row_value_by_table_scale(self):
        # The whole point of the two custom properties: normalising to
        # the top position rather than to 100% is what keeps the column
        # readable when the largest holding is only a fifth of the book.
        bodies = blocks_for(_PAGE_STYLES, ".holdings__bar-fill")
        assert bodies
        # Matched with whitespace collapsed rather than byte-for-byte:
        # csscompressor leaves a space after ``/`` and ``*`` inside
        # ``calc()``, and that spacing is its business, not the
        # contract's. What matters is that the width is the row's own
        # ``--w`` divided by the table's scale.
        widths = [b.split("width:", 1)[1] for b in bodies if "width:" in b]
        assert widths
        collapsed = widths[0].replace(" ", "")
        assert collapsed.startswith("calc(var(--w,0)/var(--holdings-weight-scale,100)*100%)")

    def test_sorted_column_indicator_is_driven_by_aria_sort(self):
        for selector in (
            '.holdings th[aria-sort="ascending"] .holdings__indicator',
            '.holdings th[aria-sort="descending"] .holdings__indicator',
        ):
            assert contains_selector(_PAGE_STYLES, selector), selector

    def test_narrow_containers_rebuild_the_row_as_a_card(self):
        # Below the threshold the table stops being a table: a
        # six-column grid on a 358px screen is not a narrower table,
        # it is an unreadable one. The design's mobile row is a card,
        # so the cells are re-placed rather than progressively hidden.
        assert contains_at_rule(_PAGE_STYLES, "@container holdings (max-width:620px)")
        body = at_rule_body(_PAGE_STYLES, "@container holdings (max-width:620px)")
        assert body
        # The row becomes a four-column grid, and no cell is dropped
        # outright -- the old layout shed Held since, then the weight
        # bar, then the logo, one threshold at a time.
        #
        # Four, not three: the second line carries the listing, the
        # date it was bought and the IRR. With three columns there was
        # no cell for the date, so it was pinned to the far end of the
        # row and read as a caption on the IRR rather than on the
        # ticker it belongs to.
        assert "grid-template-columns:30px minmax(0,auto)minmax(0,1fr)auto" in normalize(
            body
        ).replace(" auto", "auto").replace("auto ", "auto")
        assert "display:none" not in normalize(body).split(".holdings__row")[1][:400]

    def test_table_semantics_survive_the_layout_change(self, stub_logo_lookup):
        # Setting ``display`` to anything non-``table-*`` strips a
        # table's implicit ARIA roles, which would cost a screen-reader
        # user every row/column association the desktop layout gives
        # them. The renderer declares them explicitly so the semantics
        # are independent of the layout.
        w = Webpage()
        w.add_holding(_holding())
        table = _open_table(w)
        for role in (
            'role="table"',
            'role="rowgroup"',
            'role="row"',
            'role="columnheader"',
            'role="rowheader"',
            'role="cell"',
        ):
            assert role in table, f"missing {role}"

    def test_no_collapse_rule_hides_holdings(self):
        # Every position renders. The ``nth-of-type`` cutoff that used
        # to hide 7 of 10 rows is gone, along with the script that had
        # to force-expand the list before an in-page anchor could
        # scroll to a ``display: none`` row.
        css = normalize(_PAGE_STYLES)
        assert "holdings__toggle" not in css
        assert "data-holdings-list" not in css


class TestSortScriptWiring:
    def test_sort_script_is_embedded_in_head(self, stub_logo_lookup, chdir_tmp, freeze_today):
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding())
        w.save(chdir_tmp)
        html = (chdir_tmp / "index.html").read_text(encoding="utf-8")
        assert "data-holdings-table" in html
        # The script and the markup have to agree on the same three
        # attributes or sorting silently does nothing.
        assert 'data-holdings-table="open"' in html
        assert 'data-sort-kind="number"' in html
        assert 'aria-sort="none"' in html
