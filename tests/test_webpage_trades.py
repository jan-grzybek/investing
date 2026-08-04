"""Trades table rendering and save()-time section wiring."""

from __future__ import annotations

import re
from datetime import datetime

from investing.webpage import Webpage
from tests._webpage_support import (
    _holding,
    _total_return,
    _trade_event,
    stub_logo_lookup,
)


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

    def test_action_badge_is_a_swatch_and_a_label_not_a_filled_pill(
        self,
        stub_logo_lookup,
    ):
        # The badge used to be a solid 7em lozenge: saturated fill,
        # white text, rounded ends. The design draws it as a small
        # square swatch followed by the label in the same colour, and
        # for a reason -- Activity is the page's supporting section,
        # and a column of saturated pills made its least informative
        # column (two values, repeated twenty times) the loudest thing
        # on the screen.
        #
        # What has to hold: no background fill on the badge, the
        # direction colour on the text itself, and a fixed square
        # swatch. Colour on the *text* is what keeps the meaning for a
        # reader who cannot resolve a 7px square.
        from investing.assets import _PAGE_STYLES
        from tests._css_helpers import blocks_for, has_declaration

        bodies = blocks_for(_PAGE_STYLES, ".trade__badge")
        assert bodies
        base = bodies[0]
        assert has_declaration(base, "text-transform", "uppercase")
        # ``inline-block``, never ``inline-flex``. A flex container
        # takes its baseline from its first flex item -- here the
        # swatch, an empty box whose baseline is its own bottom edge --
        # so an inline-flex badge floats its label 3px above the Detail
        # and Date beside it. ``test_action_badge_label_shares_the_row
        # _baseline`` measures the consequence in a browser; this pins
        # the cause so the two cannot drift apart.
        for body in bodies:
            assert "display:inline-flex" not in body.replace(" ", "")
        # No fill, and no fixed box to centre a label inside: both are
        # the pill, and both are gone.
        for body in bodies:
            assert "background" not in body
            assert "border-radius" not in body
            assert "width" not in body

        # The direction colour lands on the text (``color``), never as
        # a fill. The swatch picks it up through ``currentcolor``, so
        # the two can never disagree.
        for modifier in (".trade__badge--buy", ".trade__badge--sell"):
            blocks = blocks_for(_PAGE_STYLES, modifier)
            assert blocks, modifier
            assert all("background" not in b for b in blocks), modifier
            assert any("color:var(" in b.replace(" ", "") for b in blocks), modifier

        swatches = blocks_for(_PAGE_STYLES, ".trade__badge-swatch")
        assert swatches
        assert has_declaration(swatches[0], "background", "currentcolor")
        # Square, and the same square at every breakpoint: a swatch
        # that is 7x6 reads as a rendering bug, not as a swatch.
        size = re.compile(r"(?:^|;)(width|height):([^;]+)")
        for body in swatches:
            sized = dict(size.findall(body))
            if sized:
                assert sized.get("width") == sized.get("height"), body

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
            # Boundary rows carry the ``--label`` modifier: no
            # percentage or minus glyph is rendered.
            assert "trades__detail--label" in row
            assert "%" not in row.split("trades__cell--detail")[1].split("</td>")[0]
        # INCREASE / DECREASE: signed-percent readouts with the
        # ``--pct`` modifier, which buys tabular figures and nothing
        # else.
        assert ">+30%<" in inc_row
        assert "trades__detail--pct" in inc_row
        # The minus is the typographically correct U+2212 sign,
        # not the ASCII hyphen-minus, so it aligns with ``+`` in
        # tabular-numbers fonts.
        assert ">\u221225%<" in dec_row
        assert "trades__detail--pct" in dec_row
        # No direction colour anywhere in the column. The row already
        # states its direction twice -- in the Action badge and in the
        # sign on the percentage -- and painting a third of the log
        # green or red made the page's quietest section its most
        # saturated. The design keeps this column on the body tone in
        # both frames.
        for row in w.trades:
            detail = row.split("trades__cell--detail")[1].split("</td>")[0]
            assert "value--positive" not in detail
            assert "value--negative" not in detail

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
        assert f">Show all {total} entries<" in table_html
        # The button sits after the table but *inside* the wrap, which
        # is where the design draws it: a full-width strip along the
        # card's bottom edge, reading as the last row of the table it
        # opens rather than as a control parked underneath. The card
        # chrome moved to the wrap for this, since a ``<button>`` is
        # not valid inside a ``<table>``.
        toggle_idx = table_html.index('class="trades__toggle"')
        table_close = table_html.index("</table>")
        wrap_close = table_html.rindex("</div>")
        assert table_close < toggle_idx < wrap_close

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
            "Show fewer entries",
            "Show all ",
        ):
            assert needle in _TRADES_SORT_SCRIPT

    def test_wrap_is_a_named_inline_size_container(self):
        # ``.trades__wrap`` is declared as a ``container-type:
        # inline-size`` query container named ``trades`` so the
        # matching ``@container`` rule fires on the wrap's own
        # rendered width rather than the viewport's. Those two
        # declarations are the load-bearing prerequisite for the
        # mobile layout -- without them the rule never matches and
        # the phone gets the desktop table.
        from investing.assets import _PAGE_STYLES
        from tests._css_helpers import contains_at_rule

        # The CSS compressor strips the space after ``:`` in
        # declarations, so match either spacing on the type / name
        # pair.
        assert re.search(r"container-type:\s*inline-size", _PAGE_STYLES)
        assert re.search(r"container-name:\s*trades", _PAGE_STYLES)
        # And the ``@container`` rule that rebuilds the log as the
        # design's two-line cards on a phone. It targets the named
        # ``trades`` container, so it fires on the wrap's own width.
        assert contains_at_rule(_PAGE_STYLES, "@container trades (max-width: 620px)")

    def test_mobile_keeps_every_column_rather_than_dropping_them(self):
        # The narrow layout used to shed Company at 600px and Action
        # at 430px. The design does not drop either: it puts them on
        # a second line, so a phone reader gets the whole trade
        # instead of a progressively poorer one.
        from investing.assets import _PAGE_STYLES
        from tests._css_helpers import at_rule_body, blocks_for, normalize

        body = at_rule_body(_PAGE_STYLES, "@container trades (max-width: 620px)") or ""
        assert body
        for cell in ("--ticker", "--name", "--action", "--detail", "--date", "--price"):
            rules = blocks_for(body, f".trades__cell{cell}")
            assert rules, f"{cell} is not placed on the mobile grid"
            # Placed, not hidden. The point of the two-line card is
            # that nothing has to be sacrificed to fit.
            for rule in rules:
                assert "display:none" not in normalize(rule), f"{cell} is hidden on mobile"

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
        # "Activity" is the same word the nav link uses, keeping the
        # section name and the nav label in lock-step; the fragment is
        # ``#activity`` to match.
        assert 'id="activity"' in out
        assert ">Activity</h2>" in out
        # Previous headings are fully retired -- a leftover "Recent
        # trades", "Trade log" or bare "Trades" heading anywhere on the
        # page would mean we missed a label during the rename.
        assert "Recent trades" not in out
        assert "Trade log" not in out
        assert ">Trades</h2>" not in out
        # Subtitle covers the section's three methodology facts: it
        # spans the full ownership history (no trailing-year cutoff),
        # rolling-quarter bursts are combined, and sizes are never
        # published.
        assert "Every executed trade since inception" in out
        assert "rolling quarter" in out
        assert "Sizes are never published" in out
        # Nav picks up the new section once trades are present.
        assert 'href="#activity"' in out
        # Section sits below historical / current sections in the
        # source order so the activity log reads as detail after the
        # high-level portfolio summary.
        idx_perf = out.index('id="performance"')
        idx_activity = out.index('id="activity"')
        assert idx_perf < idx_activity
        # Single ``<table class="trades">`` per page, with the
        # sortable thead and the row tbody wired up via
        # ``data-sort-*`` attributes. Sanity-check the contract the
        # inline ``_TRADES_SORT_SCRIPT`` depends on (one such table,
        # default sort key + direction declared on it).
        assert out.count('<table class="trades"') == 1
        assert 'data-sort-default="date"' in out
        assert 'data-sort-default-dir="desc"' in out
        # Every sortable column header carries its sort key so the
        # click handler can dispatch on it.
        for key in ("ticker", "name", "action", "detail", "date", "price"):
            assert f'data-sort-key="{key}"' in out
        # Price sorts by currency first and by amount within each
        # currency -- a real ordering, where a bare numeric sort across
        # USD / EUR / GBp would not be. The header says so rather than
        # leaving the reader to discover it.
        assert 'data-sort-currency="USD"' in out
        assert "Sorts by currency first" in out

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
        assert 'id="activity"' not in out
        assert ">Activity</h2>" not in out
        assert 'href="#activity"' not in out
        # No actual rendered table either.
        assert '<table class="trades"' not in out
        assert 'class="trades__row"' not in out

    def test_save_trades_after_historical_section(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        # When the page carries both the closed-positions section and
        # the activity section, activity appears last so the page reads
        # as: performance -> allocation -> holdings -> closed ->
        # activity -> method.
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
        idx_closed = out.index('id="closed"')
        idx_activity = out.index('id="activity"')
        idx_method = out.index('id="method"')
        assert idx_closed < idx_activity < idx_method
