"""Inline CSS / JS payloads loaded verbatim from the
``assets/`` directory at import time so the source-of-truth
lives in real ``.css`` / ``.js`` files that editors can
lint and format.
"""

from __future__ import annotations

from .paths import _read_asset

# Names imported by ``investing.webpage.head`` to assemble the inline
# CSS / JS payloads of the rendered page. Declaring ``__all__`` keeps
# the leading-underscore ``private to this module`` convention honest
# (CodeQL's ``py/unused-global-variable`` query treats the underscore
# prefix as a hard hint that the binding is module-local; ``__all__``
# is the canonical opt-in to advertise these as cross-module exports).
__all__ = [
    "_HASH_CLEAR_SCRIPT",
    "_HOLDINGS_SORT_SCRIPT",
    "_NAV_SCROLL_SCRIPT",
    "_PAGE_STYLES",
    "_RETURN_CHART_SCRIPT",
    "_TICKER_MARQUEE_SCRIPT",
    "_TRADES_SORT_SCRIPT",
    "_YEARLY_RETURNS_SCRIPT",
]

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
# stay flush across every sort + collapse permutation. On resize
# the handler does two things, in this order: it first runs
# ``unfreeze`` synchronously -- clearing ``table-layout: fixed`` and
# all per-``<th>`` pixel widths -- so the table reflows naturally
# with the new wrap dimensions as the user drags the window edge
# (without this immediate relax, the table would stay pinned to its
# previous wider column widths and visibly overflow the wrap until
# the debounced re-measure caught up, which is exactly the
# "everything jumps at once" effect the redesign exists to avoid).
# A 150ms debounce then re-runs ``freezeColumns`` to lock the new
# natural widths in for the next sort/expand cycle.
#
# Responsive column hiding is handled by the stylesheet now, not the
# script: ``.trades__wrap`` is declared as a named ``trades``
# inline-size container, and the matching ``@container trades
# (max-width: ...)`` rules drop the Company column at ~600px wrap
# width and the Action pill at ~430px wrap width. Doing the visibility
# decision in CSS rather than JS means every resize frame gets the
# correct column set as a synchronous side-effect of layout, without
# the debounce delay an earlier JS-driven version had between the
# user crossing a threshold and the column actually disappearing.
# The two thresholds are 170px apart, so a continuous resize through
# the boundary produces two clearly separated visual transitions --
# Company drops first, the 5-column layout survives all the way down
# to phone widths, and only on the narrowest viewports does Action
# follow. ``freezeColumns`` re-runs on resize so the locked pixel
# widths refresh once a column has dropped out and the remaining
# columns redistribute.
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


# Collapse / expand for the calendar-year returns table when the
# history spans more than the default visible window.
_YEARLY_RETURNS_SCRIPT = _read_asset("yearly_returns.js")


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
#   * ``<button class="holdings__toggle" data-holdings-toggle="<scope>">``
#     sits below a list when it carries more than one position.
#     The button's scope matches the list's ``data-holdings-list``
#     value so each sub-section (current equities, current fixed
#     income, historical equities, historical fixed income) can
#     collapse / expand independently. CSS hides overflow capsules
#     via ``:nth-of-type(n+4)`` until the user sets
#     ``data-expanded="true"`` on the list.
#
# When an in-page anchor targets a holding capsule that is currently
# hidden by that collapse rule (treemap tile, marquee logo, etc.),
# a capture-phase click listener expands the matching list before
# ``_NAV_SCROLL_SCRIPT`` scrolls, so ``getBoundingClientRect`` sees
# the real layout instead of a ``display: none`` row.
#
# The "Default" button is special-cased: it never carries a
# direction, and pressing it restores the upstream DOM order
# (most recent buy first for the current list, most recent sell
# first for the historical list -- the order ``get_holdings``
# already produces). The original sequence is captured at boot
# into a per-list array so re-pressing "Default" after any
# number of sorts always lands on the same starting state.
#
# ``aria-pressed`` on the active button drives the screen-reader
# announcement; ``data-sort-dir`` on the active directional button
# drives the visible sort-indicator triangle (CSS) without placing
# ``aria-sort`` on ``<button>`` elements, which axe flags as invalid.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_HOLDINGS_SORT_SCRIPT = _read_asset("holdings_sort.js")


# Drives the decorative current-holdings marquee at the top of the
# page. The track is a flex row of one ``<a class="ticker__link">``
# per current holding, doubled (two identical copies of the logo
# set) so a wrap-by-half-width strategy produces a seamless loop:
# each ``requestAnimationFrame`` tick decrements an in-memory
# ``offset`` variable and writes ``transform: translate3d(<offset>
# px, 0, 0)`` to the track; once ``offset`` crosses ``-halfWidth``
# (the natural width of one copy of the logo set), the script adds
# ``halfWidth`` back so the second copy is now exactly where the
# first one was. The visual is identical, but the offset variable
# has just been rebased into the loop's valid range.
#
# The animation was previously a CSS ``@keyframes`` rule driving a
# ``transform: translate3d(0, 0, 0) -> translate3d(-50%, 0, 0)``
# cycle, with ``animation-duration`` switched at viewport
# breakpoints and ``animation-play-state: paused`` on
# ``.ticker:hover``. That implementation accumulated three distinct
# failure modes that all surfaced as the user-reported "the bar
# sometimes jams / blinks / resets position", and each new CSS-only
# patch only addressed a subset:
#
#   * Tab-visibility resume: CSS animations are wall-clock timed
#     and on some browsers the transform jumps to "where the
#     animation would be now" when the tab becomes visible again,
#     rather than continuing from the paused offset. The rAF loop
#     stops on ``visibilitychange`` and resumes from the same
#     offset variable, so the jump is structurally impossible.
#   * Compositor layer demotion: the GPU layer carrying the
#     animated transform can be evicted under memory pressure, by
#     neighbouring repaints (sticky-header ``backdrop-filter``
#     rebuild on iOS Safari while the URL bar collapses), or when
#     ``animation-play-state`` flips on hover. The rebuild snaps
#     the interpolated transform to a fresh raster, which the eye
#     reads as a shift. Writing a single ``translate3d`` value per
#     frame from the main thread leaves no keyframe interpolation
#     for the compositor to lose.
#   * Mid-flight ``animation-duration`` changes: crossing a
#     breakpoint (or the iOS URL bar collapsing across one) instant-
#     ly re-evaluates the keyframe position against the new
#     duration, which jumps the transform. JS computes ``pxPerMs``
#     from the current viewport and re-reads it on debounced
#     ``resize``, so the offset variable stays continuous across
#     breakpoints.
#
# Pause-on-hover is reimplemented in JS (only on real pointer
# devices, gated on ``matchMedia('(hover: hover)')`` the same way
# the previous CSS rule was) by flipping a ``paused`` flag that
# skips the offset decrement -- ``offset`` is preserved verbatim,
# so resume is byte-for-byte continuous with pause. ``prefers-
# reduced-motion`` short-circuits boot entirely; the matching
# CSS block collapses the ``width: max-content`` track into a
# wrapping centred row so all logos remain visible at once
# without any motion.
#
# Kept as a tight ES5-flavoured IIFE so the inline payload stays
# small and gets a single stable SHA-256 hash (pinned in CSP).
_TICKER_MARQUEE_SCRIPT = _read_asset("ticker_marquee.js")
