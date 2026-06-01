"""Inline CSS / JS payloads loaded verbatim from the
``assets/`` directory at import time so the source-of-truth
lives in real ``.css`` / ``.js`` files that editors can
lint and format.
"""

from __future__ import annotations

from .paths import _read_asset

# ---------------------------------------------------------------------------
# Webpage renderer
# ---------------------------------------------------------------------------


# Embedded styles. Kept verbatim as a single string so ``save()`` stays
# linear and the dark-mode / print rules are easy to audit.
_PAGE_STYLES = _read_asset("page.css").strip()


# Tiny inline script that strips the URL hash the moment the user
# takes manual control of scrolling. The ``Performance`` / ``Current``
# / ``Historical`` nav links in the sticky header are plain in-page
# anchors -- clicking ``Current`` appends ``#current`` to the URL and
# the browser scrolls to that section. Without this script the hash
# sticks around even after the user wheels elsewhere on the page, so
# a subsequent refresh makes the browser re-jump to the section they
# last clicked on instead of restoring their actual scroll position
# -- which on a long holdings page reads as the page "scrolling down
# uncontrollably" on every refresh.
#
# We only react to user-initiated input events (``wheel``,
# ``touchmove``, and the keys that scroll the page). That way the
# initial smooth-scroll triggered by a nav click does NOT clear the
# hash -- the hash stays in the URL while the user is "at" the
# section they navigated to (so the link is still shareable), and
# only gets dropped the instant the user starts exploring on their
# own. Listeners are passive so they never block scrolling.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_HASH_CLEAR_SCRIPT = _read_asset("hash_clear.js")


# Custom smooth-scroll for in-page anchor links. Native CSS
# ``scroll-behavior: smooth`` is fast and abrupt, and on iOS Safari
# the sticky header's ``backdrop-filter`` re-composites mid-scroll
# which reads as a brief "blink" right after the tap. Driving the
# scroll from JS lets us:
#
#   * use an ease-out quartic animation that genuinely "slides"
#     between sections instead of snapping. ``easeOutQuart``
#     (``1 - (1-t)^4``) front-loads motion: the scroll picks up
#     speed in the first frame and decelerates smoothly into the
#     target. The earlier ``easeInOutCubic`` curve started slow
#     (perceived as input lag), then accelerated through the
#     middle, then decelerated -- on a long page that "slow start,
#     fast middle, slow end" reads as "the page is stuttering and
#     then catching up" rather than as a smooth slide. Ease-out
#     also lets us shorten the overall duration without the
#     animation feeling rushed, since the user immediately sees
#     meaningful motion;
#   * cancel ``preventDefault()`` the anchor click so the browser
#     never performs the instant-jump that fights our animation;
#   * call ``window.scrollTo`` programmatically (which does NOT
#     fire wheel/touchmove), so the animation runs uninterrupted
#     while the existing ``_HASH_CLEAR_SCRIPT`` happily stays put;
#   * still write the section anchor into the URL via
#     ``history.pushState`` so the link is shareable, matching
#     pre-existing behaviour.
#
# The selector covers every same-page anchor on the page -- nav
# links, marquee logos (``.ticker__link``), and clickable bar rows
# (``.bars__row--link``) -- except for the visually-hidden
# ``.skip-link``, which assistive-tech users expect to jump
# instantly. Honours ``prefers-reduced-motion`` by jumping directly
# to the target.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
#
# ``slide`` locks the destination ``targetY(el)`` at the moment of
# the click and animates against it for the rest of the duration.
# An earlier version re-read ``targetY`` on every frame to absorb
# layout shifts in flight (iOS Safari URL-bar collapse, lazy logos
# finishing decode), but with explicit ``width``/``height`` on
# every holding logo there is no CLS to absorb, programmatic
# ``scrollTo`` does not trigger the iOS URL-bar transition, and
# the per-frame re-read introduced a subtle but visible jitter:
# each ``targetY`` call rescales the entire trajectory, so any
# sub-pixel shift was amplified through the ease curve into a
# visible micro-stutter -- the user-reported "the animation
# looks odd" feel on short hops from the allocation chart. A
# single ``scrollTo`` to the current ``targetY`` at the very end
# of the slide still catches any pixel-level layout drift that
# happened in flight without contaminating the easing curve.
#
# ``scrollTo`` is invoked with ``{behavior: 'auto'}`` to
# explicitly opt out of any user-agent / page CSS smooth scroll
# that might otherwise layer a second animation on top of our
# rAF loop.
_NAV_SCROLL_SCRIPT = _read_asset("nav_scroll.js")


# Pointer-driven scrubber for the return chart. A finger or cursor
# dragged across the plot reveals the date and per-series total
# return at that x-coordinate via a vertical guide line, a marker
# dot riding each curve, and a small tooltip card with the values.
#
# The script is intentionally data-agnostic: every figure that wants
# the interaction declares ``data-chart='{...}'`` on its
# ``.return-chart`` element, with ``start`` (ISO date), ``totalDays``
# (integer span), ``rightPct`` (the chart's right margin reserved
# for the delta annotation), ``yMin``/``yMax`` (the SVG y-domain),
# and one entry per series in ``series`` with ``kind`` (``jg`` or
# ``bench``), ``label``, ``x`` (day offsets from ``start``), and
# ``y`` (return multiples). Keeping the data in the DOM rather than
# baking it into the script means the script's payload is identical
# for every page render -- and so its SHA-256 is stable and can be
# pinned in CSP without re-hashing on every update.
#
# Linear interpolation between adjacent (x, y) samples gives the
# tooltip its values: the visual curve uses a Pchip spline, but
# linear is a faithful enough approximation between dense samples
# (we hover with sub-pixel precision; the difference is invisible
# to the eye), and it keeps the script small and dependency-free.
#
# ``touch-action: pan-y`` on the plot (set in CSS) allows vertical
# page scrolling to start from a touch on the chart while horizontal
# motion is captured for scrubbing. ``pointer*`` events unify mouse
# and touch handling.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_RETURN_CHART_SCRIPT = _read_asset("return_chart.js")


# Click-to-sort behaviour for the "Trades" table.
#
# Each ``<tr class="trades__row">`` carries the sort keys it can be
# ordered by on ``data-sort-*`` attributes (date / ticker / name /
# action / detail). The script wires every ``<th data-sort-key="...">``
# so a click on the inner ``.trades__sort`` button:
#
#   * toggles the direction when the same column is clicked twice in
#     a row (asc <-> desc);
#   * picks a sensible initial direction the first time the user lands
#     on a column -- "desc" for date (newest first matches the way the
#     section was already ordered by default), "asc" for everything
#     else (alphabetical A -> Z for ticker / name, BUY before SELL for
#     action, OPEN -> CLOSE for detail);
#   * updates ``aria-sort`` on the active ``<th>`` so screen readers
#     announce the new state, and resets the other columns to "none"
#     so only one indicator triangle ever reads as active;
#   * keeps a deterministic tie-break (date desc, then ticker asc) so
#     equal-key rows always reorder the same way and the table doesn't
#     visibly shuffle when the user sorts by action and several rows
#     share a label.
#
# Bursts span multiple days but only one ``data-sort-date`` value is
# emitted per row (the burst's ``end_date``, i.e. its most recent
# event) -- it's the natural anchor for the "when did this trade
# happen?" question and matches the desktop convention of headlining
# a burst by its last fill.
#
# Right after the initial sort the script also runs ``freezeColumns``
# to pin each ``<th>`` to the width it would naturally take with
# every row exposed. The default ``table-layout: auto`` recomputes
# column widths from whichever rows are currently visible, and the
# "Show fewer trades" cap (CSS hides ``tr:nth-of-type(n+11)``) means
# sorting can rotate a long name -- "UnitedHealth Group Inc.", "Lam
# Research Corporation" -- in or out of the top-10 window, which
# visibly squashes or widens the Company column. By measuring once
# with all rows displayed and then locking the table to
# ``table-layout: fixed`` with those pixel widths, the column edges
# stay flush across every sort + collapse permutation. The freeze is
# re-run on viewport resize so the @540px breakpoint (which hides
# the Company column entirely on phones) gets a fresh snapshot
# rather than carrying desktop widths into the narrower layout.
#
# ``boot`` is deferred to ``DOMContentLoaded`` because the script ships
# from <head> and the ``<table class="trades">`` body it queries for
# isn't parsed yet at that point. Without the defer the IIFE would
# observe a null table on every page load and bail out, leaving the
# sort headers silently inert (which is exactly the bug we're fixing
# here). The pattern matches ``_RETURN_CHART_SCRIPT`` further up.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_TRADES_SORT_SCRIPT = _read_asset("trades_sort.js")


# Click-to-sort behaviour for the "Current holdings" / "Historical
# holdings" lists.
#
# Both sections share a markup contract:
#
#   * ``<div class="holdings__sort" data-holdings-sort="<scope>">``
#     is the toolbar, hosting one ``<button>`` per sort option.
#     Each button carries ``data-holdings-sort-key`` (the field to
#     order by: ``ticker`` / ``name`` / ``tsr`` / ``cagr`` /
#     ``weight``, plus the ``default`` reset button) and
#     ``data-holdings-sort-kind`` (``text`` / ``number`` /
#     ``default``). The ``kind`` value drives the initial sort
#     direction the JS picks the first time the user activates a
#     button -- ``text`` ascends (A->Z) and ``number`` descends
#     (high->low), matching the natural reading direction for
#     each datatype. Re-clicks on the active button toggle
#     ascending <-> descending.
#   * ``<div class="holdings__list" data-holdings-list="<scope>">``
#     is the immediate sibling that holds the ``<article
#     class="holding">`` cards. The script pairs each toolbar
#     with its list by walking forward from the toolbar's
#     ``data-holdings-sort`` to the matching list's
#     ``data-holdings-list``; that lets the two lists on the
#     page (``current`` and ``historical``) be sorted
#     independently of each other.
#   * Each ``<article class="holding">`` carries the per-row keys
#     on ``data-sort-ticker`` / ``data-sort-name`` /
#     ``data-sort-tsr`` / ``data-sort-cagr`` /
#     ``data-sort-weight`` (the last one is current-only;
#     historical rows omit it and the historical toolbar omits
#     the corresponding "Weight" button).
#
# The "Default" button is special-cased: it never carries a
# direction, and pressing it restores the upstream DOM order
# (most recent buy first for the current list, most recent sell
# first for the historical list -- the order ``get_holdings``
# already produces). The original sequence is captured at boot
# into a per-list array so re-pressing "Default" after any
# number of sorts always lands on the same starting state.
#
# ``aria-pressed`` on the active button + ``aria-sort`` on the
# matching directional button is what assistive tech announces
# (the indicator triangles below the labels are aria-hidden
# decoration). Only one button per toolbar is ever
# ``aria-pressed="true"`` at a time so a screen reader hears one
# canonical "current sort" per section.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_HOLDINGS_SORT_SCRIPT = _read_asset("holdings_sort.js")
