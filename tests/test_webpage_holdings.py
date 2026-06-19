"""Holding capsules, sort controls, and collapse toggles."""

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
            has_declaration(body, "display", "none") and ".holding__decimal" in body
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
        # The toolbar only renders when more than one capsule sits
        # in the list (sorting a single row is a no-op), so the
        # fixture deliberately seeds two current holdings.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR", name="Curr Co."))
        w.add_holding(_holding(ticker="NMS:OTHR", name="Other Co."))
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
        # Two historical rows so the >1 toolbar gate emits the
        # sort buttons; otherwise sort options would be (correctly)
        # suppressed and the test would have nothing to assert
        # against.
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
        w.add_holding(
            _holding(
                ticker="NMS:OLD2",
                name="Older Co.",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2021, 1, 1), "end": datetime(2022, 1, 1)}],
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
        # rows below. Two rows per side so the >1 toolbar gate
        # emits the sortable scopes the assertion below checks.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR", name="Curr Co."))
        w.add_holding(_holding(ticker="NMS:CURR2", name="Curr2 Co."))
        w.add_holding(
            _holding(
                ticker="NMS:OLD",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2022, 1, 1), "end": datetime(2023, 1, 1)}],
            )
        )
        w.add_holding(
            _holding(
                ticker="NMS:OLD2",
                name="Older Co.",
                is_current=False,
                weight=None,
                periods=[{"start": datetime(2021, 1, 1), "end": datetime(2022, 1, 1)}],
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
        assert head.count("'sha256-") >= 7  # JSON-LD + 6 IIFEs

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
        # Two holdings so the toolbar emits at all under the
        # >1 sort-control gate.
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR", name="Curr Co."))
        w.add_holding(_holding(ticker="NMS:OTHR", name="Other Co."))
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


class TestHoldingsCollapse:
    def test_single_position_omits_toggle(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        w.add_holding(_holding(ticker="NMS:CURR", name="Curr Co."))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'class="holdings__toggle"' not in out

    def test_up_to_visible_default_omits_toggle(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        from investing.webpage import Webpage as _Webpage

        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        for i in range(_Webpage._HOLDINGS_VISIBLE_DEFAULT):
            w.add_holding(_holding(ticker=f"NMS:C{i}", name=f"Co {i}"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'class="holdings__toggle"' not in out

    def test_multiple_positions_render_show_all_toggle(
        self,
        stub_logo_lookup,
        chdir_tmp,
        freeze_today,
    ):
        from investing.webpage import Webpage as _Webpage

        threshold = _Webpage._HOLDINGS_VISIBLE_DEFAULT
        total = threshold + 1
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        for i in range(total):
            w.add_holding(_holding(ticker=f"NMS:C{i}", name=f"Co {i}"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        assert 'class="holdings__toggle"' in out
        assert 'data-holdings-toggle="current"' in out
        assert f'data-total="{total}"' in out
        assert 'aria-expanded="false"' in out
        assert f">Show all {total} holdings<" in out
        toggle_idx = out.index('data-holdings-toggle="current"')
        list_close = out.index('data-holdings-list="current"')
        list_end = out.index("</div>", list_close)
        assert list_end < toggle_idx

    def test_collapse_rule_hides_overflow_capsules_by_default(self):
        from investing.assets import _PAGE_STYLES
        from investing.webpage import Webpage as _Webpage
        from tests._css_helpers import contains_selector

        threshold = _Webpage._HOLDINGS_VISIBLE_DEFAULT
        rule = (
            f'.holdings__list:not([data-expanded="true"]) .holding:nth-of-type(n+{threshold + 1})'
        )
        assert contains_selector(_PAGE_STYLES, rule)

    def test_toggle_script_flips_state_and_relabels_button(self):
        from investing.assets import _HOLDINGS_SORT_SCRIPT

        for needle in (
            ".holdings__toggle",
            "data-holdings-toggle",
            "data-expanded",
            "aria-expanded",
            "Show fewer holdings",
            "Show all ",
        ):
            assert needle in _HOLDINGS_SORT_SCRIPT

    def test_toggle_sits_below_its_list(self, stub_logo_lookup, chdir_tmp, freeze_today):
        freeze_today(datetime(2025, 6, 1))
        w = Webpage()
        w.add_return(_total_return(), [])
        for i in range(4):
            w.add_holding(_holding(ticker=f"NMS:C{i}", name=f"Co {i}"))
        w.save()

        out = (chdir_tmp / "index.html").read_text()
        list_idx = out.index('data-holdings-list="current"')
        toggle_idx = out.index('data-holdings-toggle="current"')
        list_end = out.index("</div>", list_idx)
        assert list_end < toggle_idx
