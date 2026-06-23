"""Sector-grouped treemap for the current equity holdings.

The chart visualises the **equity-only** slice of the portfolio
(cash and any other asset class never make it into the rendered
tiles) grouped by GICS-style sector. Tile area is proportional to
the holding's portfolio weight, sectors are coloured from a stable
palette so the same sector reads as the same hue across runs, and
each ticker tile is wrapped in an anchor pointing at the matching
holding capsule below -- mirroring the click-to-scroll affordance
the top-N bar chart already exposes.

Layout: a two-level squarified treemap (Bruls / Huijse / van Wijk,
2000). The outer level partitions the canvas between sectors using
the sum of each sector's holding weights; the inner level partitions
each sector rectangle between the tickers inside it. The squarified
algorithm produces tile aspect ratios close to 1:1, which keeps the
embedded ticker / weight labels readable even on dense canvases.

The chart is rendered as absolutely-positioned ``<a>`` tiles inside
a ``position: relative`` container with a fixed CSS ``aspect-ratio``.
Percentages let the chart reflow into any container width without
recomputing coordinates client-side.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..formatting import _fmt_pct
from ..logos import _DEFAULT_LOGO_ASPECT
from .anchors import holding_anchor, strip_exchange

# Reference content widths for the **fold-into-Other** empty-tile probe
# (see :func:`_tile_must_fold_into_other`). The client-side layout
# script and :func:`_merge_small_into_other_at_canvas` sample several
# figure widths that mirror the page's responsive contract: narrow
# phone, typical phone, the ``540px`` aspect-ratio breakpoint, and
# desktop / max-content widths
# from ``00-base.css`` / ``90-responsive.css``. For each width the
# canvas height follows the same aspect rule as CSS (4 : 3 up to
# ``540px``, 2 : 1 above). A holding folds when its squarified tile
# would be a colour-only swatch at **any** sampled width -- individual
# equity tiles must never ship unlabeled on a supported viewport.
#
# Thresholds below match the ``@container tile`` rules in
# ``50-treemap.css`` (logo swap at 80 x 50 px; ticker / weight stack
# at 60 x 46 px once the logo is hidden).
_REF_ASPECT_MOBILE_BREAK_PX = 540.0
_REF_CONTENT_WIDTHS_PX: tuple[float, ...] = (328.0, 358.0, 541.0, 832.0, 848.0)

_LOGO_MIN_TILE_W_PX = 80.0
_LOGO_MIN_TILE_H_PX = 50.0
_TEXT_MIN_TILE_W_PX = 60.0
_TEXT_MIN_TILE_H_PX = 46.0

# Shave a pixel off the fold probe so tiles that barely clear the CSS
# ``@container tile`` thresholds (font rounding, inner padding) still
# fold into ``Other equities`` rather than shipping as colour-only
# swatches. Matches ``FOLD_SAFETY_PX`` in ``treemap_layout.js``.
_FOLD_SAFETY_PX = 1.0


def _ref_canvas_wh(content_w_px: float) -> tuple[float, float]:
    """Return ``(canvas_w, canvas_h)`` for a figure at ``content_w_px``."""
    if content_w_px <= _REF_ASPECT_MOBILE_BREAK_PX:
        return content_w_px, content_w_px * 3 / 4
    return content_w_px, content_w_px / 2


_REF_CANVAS_SPECS: tuple[tuple[float, float], ...] = tuple(
    _ref_canvas_wh(w) for w in _REF_CONTENT_WIDTHS_PX
)

# Inset (in canvas-relative percent) shaved off **each side** of every
# sector's bounding rectangle before the inner squarify packs the
# ticker tiles inside it. The visual purpose is to encode the chart's
# two-level hierarchy as two-level gap thickness: tickers that share a
# sector abut directly (their identical 1 px ``outline`` produces a
# 2 px hairline seam), and the gap between *different* sectors widens
# to ``2 * _SECTOR_INSET_PCT`` of the canvas plus the same 2 px
# outline pair. On a 1000 px-wide canvas, ``0.4`` adds ~8 px of breathing
# room between sectors -- enough for the eye to read the partition as
# "different cluster" without making the chart look gappy. This mirrors
# the ``paddingOuter`` knob the D3 / Tableau treemap implementations
# expose for exactly the same hierarchy cue.
#
# Trade-off: the inset removes area from the squarified packing, so
# the strict "tile area is proportional to weight" invariant softens
# slightly. The distortion scales with each sector's perimeter (an
# inset costs more, in relative terms, on a slim sector than on a
# fat one) but for any realistic portfolio it stays well below the
# eye's noticing threshold at ``0.4`` percent.
#
# Knock-on effect on the merge loop: a tile that was just clearing
# ``_TEXT_MIN_TILE_*_PX`` could dip below after the inset shrinks
# its sector's usable area, which means
# :func:`_merge_small_into_other` might fold one extra holding into
# the aggregated bucket. That is an acceptable consequence -- the
# readability promise is "every visible tile holds its label", not
# "every original holding gets its own tile".
_SECTOR_INSET_PCT = 0.4

# Reference aspect ratio for the equal-area logo sizing. Each logo
# renders at ``(base_w * sqrt(R / R_ref), base_h * sqrt(R_ref / R))``
# where ``R`` is the logo's intrinsic aspect ratio and ``R_ref`` is
# this reference. The product ``width * height`` stays constant
# across logos so every brand occupies the same screen area
# regardless of whether it's a wide wordmark (META at 4.95 : 1,
# NVDA at 5.40 : 1) or a near-square mark (CRM at 1.43 : 1).
#
# Why 3.0 even though the portfolio's actual tight-cropped median
# is closer to 4 : 1: the reference doubles as the "neutral" point
# where the factor pair ``(wf, hf)`` lands at ``(1, 1)``. Setting it
# **below** the typical wordmark aspect makes the IMG box stretch
# horizontally for the median logo (a 4 : 1 logo at ``R_ref = 3``
# renders in a ~4.6 : 1 IMG box) so the box adapts to each logo's
# natural shape rather than letterboxing the median wordmark inside
# a fixed-aspect rectangle. The CSS base box itself is ~3 : 1
# (``clamp(60px, 11.5cqi, 96px)`` wide x ``clamp(22px, 3.8cqi, 28px)``
# tall), so ``R_ref = 3`` keeps the math centred on the base box's
# intrinsic shape while letting wider logos earn extra horizontal
# real estate.
_LOGO_REFERENCE_ASPECT = 3.0

# Reference ink density for the equal-VISUAL-area logo sizing pass
# layered on top of the aspect-ratio normalisation above. ``density``
# is the fraction of the rasterised bounding box that survives the
# treemap's SVG knockout filter -- i.e. the white silhouette the
# eye actually reads as "the logo" on a coloured tile (see
# :func:`investing.logos._measure_svg_density` for the measurement
# pass). Across the current portfolio (post tight-cropping), density
# ranges from ~0.054 for thin-stroke wide wordmarks (Qualcomm,
# UnitedHealth, FreshWorks) up to ~0.35 for solid-mass icons
# (Vanguard), so the same bbox area renders as very different
# visible marks. 0.13 sits comfortably above the measured median
# (~0.087): the sparsest wordmarks (UNH at 0.055, FRSH at 0.057,
# QCOM at 0.054) want a raw bbox scale of ``sqrt(0.13 / 0.055) ~= 1.54``
# and float just below the MAX clamp; the mid-pack wordmarks grow
# gently; the moderately-dense and dense logos all land on the MIN
# clamp. This avoids the "every wordmark grew, the chart looks
# overcorrected" feel of a higher reference while still letting
# the visibly-sparsest logos read as larger than their dense
# counterparts.
#
# Important caveat about what ``density`` actually measures: the
# rasteriser in :func:`investing.logos._measure_svg_density`
# preserves the SVG's intrinsic aspect ratio and letterboxes it
# into a fixed 128x128 frame, so the measured value is the
# letterboxed-canvas density rather than the bbox-intrinsic
# density. For a logo with intrinsic ink density ``d_i`` and
# aspect ``R``, the measured value follows ``d_letterbox ~ d_i / max(R, 1/R)``.
# That means the density correction here is implicitly **also**
# an aspect correction (wide logos get an extra ``sqrt(R)`` size
# bump on top of the explicit aspect normalisation above). The
# constants below (``MIN``, ``MAX``, ``D_REF``) are tuned against
# the letterboxed scale, so the system as a whole is internally
# consistent -- but a future migration to an aspect-correct
# density probe would require recalibrating all three.
_LOGO_REFERENCE_DENSITY = 0.13

# Min / max clamps on the density scale factor (= the multiplier
# applied to BOTH width and height to scale the bounding-box area
# inversely to the source's ink density). The clamps cap the
# scale's effect on extreme outliers in either direction:
#
#   * ``_LOGO_DENSITY_MAX_SCALE`` is the upper guardrail that
#     prevents a very-sparse logo from blowing up. With the
#     reference density at 0.13, the sparsest wordmarks in the
#     post-tight-cropping portfolio (Qualcomm at 0.054, UnitedHealth
#     at 0.055, FreshWorks at 0.057, BABA at 0.057) want a raw
#     scale of ``sqrt(0.13 / 0.055) ~= 1.54``, comfortably below
#     ``MAX = 1.60``. The clamp's job is therefore to absorb any
#     future logo whose letterboxed density measures lower than
#     the current floor (a thin-stroke wordmark at ~7:1+ aspect
#     would land there) rather than to bind the current cluster --
#     in the current portfolio no logo hits MAX.
#   * ``_LOGO_DENSITY_MIN_SCALE`` is intentionally *above 1.0* --
#     i.e. dense logos (Salesforce, Adobe, TSM, Vanguard) don't
#     just stop shrinking, they grow by a uniform 15 %. The
#     intent is a "combination of overall size and density" sizing
#     pass rather than a strict equal-visible-ink one: dense logos
#     visibly carry their own mass on the tile and don't need
#     their bbox squeezed by the same amount the formula's
#     equal-ink math wants to take from them. With ``MIN = 1.15``,
#     Salesforce reads as a comparably-sized neighbour of the
#     mid-pack wordmarks (LRCX at bbox-scale 1.15) rather than a
#     miniature variant. About half of the post-tight portfolio
#     lands on this clamp.
_LOGO_DENSITY_MIN_SCALE = 1.15
_LOGO_DENSITY_MAX_SCALE = 1.60

# SVG ``<filter>`` definitions referenced from the per-tile logo's
# CSS ``filter: url(#treemap-logo-knockout-light|dark)``. The
# filters recolour every visible source pixel to a solid ink
# (uniform white for ``-light``, uniform dark near-black for
# ``-dark``) and knock out brand-authored near-pure-white pixels
# so the tile colour shows through wherever the brand intended a
# white reveal. CSS picks the variant per tile based on the
# sector's background contrast against white -- see the
# ``.treemap__tile-logo`` rules in ``page.css``.
#
# Why two filters rather than one parametrised on ``currentColor``:
# Chromium has a long-standing bug where ``flood-color="currentColor"``
# inside an SVG filter resolves to **black** regardless of the
# host element's CSS ``color`` -- the colour value isn't propagated
# from the HTML element the filter is applied to into the
# filter-graph rendering context. Shipping two filters and
# selecting between them in CSS sidesteps the bug entirely while
# costing only a few hundred extra bytes of inline SVG markup.
#
# Earlier iterations of these filters used a single ``feColorMatrix``
# that mapped ``alpha = 1 - (R+G+B)/3`` on every pixel. That was
# elegant but turned every coloured pixel into a partially-
# transparent white (blue at ~67% opacity, red at ~67%, etc.),
# which read as grey rather than the crisp uniform silhouette the
# rest of the chart calls for. The current four-stage pipeline
# below keeps the silhouette uniformly opaque and only ramps the
# alpha down on pixels whose source whiteness is above ~0.8 --
# i.e. brand-authored white reveals and a thin band of
# anti-aliased edges around them.
#
# Pipeline (all stages run in premultiplied sRGB, the SVG-filter
# default):
#
#   1. ``silhouette`` -- ``feColorMatrix`` rewrites every channel
#      to a hardcoded ink. The ``-light`` filter uses
#      ``(1,1,1,A)`` (opaque white); the ``-dark`` filter uses
#      ``(0.059,0.141,0.188,A)`` (#0F2430, the page's primary
#      text colour, the same near-black the dark-ink CSS rules
#      use for tile labels).
#
#   2. ``whiteness`` -- ``feColorMatrix`` zeroes out colour and
#      writes ``(R+G+B)/3`` of the SOURCE into alpha, so pure-
#      white source pixels yield alpha=1 and pure-black/coloured
#      pixels yield alpha=0.
#
#   3. ``knockoutMask`` -- ``feComponentTransfer`` thresholds the
#      whiteness alpha with a steep linear ramp
#      (``slope=5, intercept=-4``): alpha <= 0.8 clamps to 0 (no
#      knockout at all on brand colours), alpha = 1.0 produces a
#      full knockout, and the band in between fades smoothly so
#      anti-aliased edges between white and adjacent fills don't
#      pop as hard cutouts.
#
#   4. ``feComposite operator="out"`` keeps the silhouette where
#      the knockout mask is empty -- which is the "subtract"
#      operation on the alpha channel. The output pixel becomes
#      ``silhouette * (1 - knockoutMask.alpha)``.
#
# Sanity checks on representative source pixels (``-light`` filter):
#   * Transparent       (0,0,0,0):   -> (0,0,0,0)            -> stays transparent.
#   * Opaque black      (0,0,0,1):   -> (1,1,1,1)            -> opaque white.
#   * Opaque brand blue (0,0,1,1):   -> (1,1,1,1)            -> opaque white (whiteness 0.33 < 0.8 threshold).
#   * Opaque brand red  (1,0,0,1):   -> (1,1,1,1)            -> opaque white.
#   * Opaque white      (1,1,1,1):   -> (0,0,0,0)            -> drops out to transparent.
#   * Near-white        (0.9,0.9,0.9,1) -> (0.5,0.5,0.5,0.5) -> half knockout (smooth fade).
# The ``-dark`` filter substitutes the ink in stage 1 and otherwise
# behaves identically: opaque black source becomes opaque #0F2430,
# opaque white still drops out, and near-white still fades smoothly.
_LOGO_KNOCKOUT_FILTER_ID_LIGHT = "treemap-logo-knockout-light"
_LOGO_KNOCKOUT_FILTER_ID_DARK = "treemap-logo-knockout-dark"


def _build_logo_knockout_filter(filter_id: str, rgb: tuple[float, float, float]) -> str:
    """Emit one variant of the treemap logo knockout filter.

    ``rgb`` is the (R, G, B) ink colour in 0..1 floats. The four
    stages are identical between variants; only the silhouette's
    ink colour differs. Splitting the filter construction into a
    helper keeps the two variants byte-for-byte consistent at the
    cost of one small allocation per render.
    """
    r, g, b = rgb
    return (
        f'<filter id="{filter_id}" color-interpolation-filters="sRGB">'
        # Stage 1: uniform opaque ink silhouette with source alpha.
        '<feColorMatrix in="SourceGraphic" type="matrix" '
        'result="silhouette" values="'
        f"0 0 0 {r:.4f} 0 "
        f"0 0 0 {g:.4f} 0 "
        f"0 0 0 {b:.4f} 0 "
        "0 0 0 1 0"
        '"/>'
        # Stage 2: whiteness map -- average of R/G/B routed into alpha.
        '<feColorMatrix in="SourceGraphic" type="matrix" '
        'result="whiteness" values="'
        "0 0 0 0 0 "
        "0 0 0 0 0 "
        "0 0 0 0 0 "
        "0.3333 0.3333 0.3333 0 0"
        '"/>'
        # Stage 3: steep alpha threshold so only near-pure-white contributes.
        '<feComponentTransfer in="whiteness" result="knockoutMask">'
        '<feFuncA type="linear" slope="5" intercept="-4"/>'
        "</feComponentTransfer>"
        # Stage 4: subtract the knockout mask from the silhouette.
        '<feComposite in="silhouette" in2="knockoutMask" operator="out"/>'
        "</filter>"
    )


# Dark ink colour (#0F2430) kept in sync with the
# ``--treemap-tile-ink: #0f2430`` overrides in ``page.css``. The
# value is the page's primary text colour in light mode -- using
# the same hex keeps the filtered logo and the surrounding tile
# label visually identical on every "dark ink" tile.
_LOGO_KNOCKOUT_DARK_INK = (0x0F / 255.0, 0x24 / 255.0, 0x30 / 255.0)
_LOGO_KNOCKOUT_SVG = (
    '<svg class="treemap__defs" width="0" height="0" '
    'aria-hidden="true" focusable="false">'
    + _build_logo_knockout_filter(_LOGO_KNOCKOUT_FILTER_ID_LIGHT, (1.0, 1.0, 1.0))
    + _build_logo_knockout_filter(_LOGO_KNOCKOUT_FILTER_ID_DARK, _LOGO_KNOCKOUT_DARK_INK)
    + "</svg>"
)

# Sentinel sector label for two related buckets that both deserve a
# neutral grey swatch:
#
#   1. Real holdings whose upstream ``info["sector"]`` is empty or
#      missing -- yfinance returns this on a handful of exotic
#      instruments (some ADRs, recently listed names whose profile
#      is not yet populated). Bucketing them together keeps the
#      treemap legend stable across runs rather than producing
#      one-off "Unknown" / "" tiles whose colour might shift
#      between renders.
#
#   2. The aggregated pseudo-row the renderer synthesises when the
#      merge loop folds tiny-weight holdings together so their
#      combined tile clears the readability threshold (see
#      :func:`_merge_small_into_other`). The pseudo-row is
#      identifiable by ``_Row.is_aggregated`` (its ``folded_tickers``
#      tuple is non-empty); both buckets share the ``_OTHER_SECTOR``
#      label and the same colour swatch so the legend stays a clean
#      one-line summary of the chart.
#
# Kept in lower-case ``"Other"`` rather than the user-facing
# ``"Other equities"`` for two reasons: the value also serves as the
# CSS ``data-sector="Other"`` hook the colour / ink overrides key
# off (so renaming the sentinel would force a cascade of CSS-
# selector edits), and the same constant is referenced from tests
# / docs that pin the internal contract. The display label is
# produced from this sentinel via :data:`_OTHER_DISPLAY_LABEL` and
# the responsive long / short markup the aggregated tile emits.
_OTHER_SECTOR = "Other"

# Human-readable label for the **aggregated pseudo-row** -- i.e.
# the synthesised bucket the merge loop creates when several
# small-weight holdings get folded together so their combined tile
# clears the readability threshold (see :func:`_merge_small_into_other`).
# The label disambiguates the bucket from the ``Other`` *sector*
# (real holdings whose upstream ``info["sector"]`` was empty), so:
#
#   * The legend chip keeps saying ``"Other"`` -- it labels the
#     sector swatch, which may legitimately contain real "no
#     sector" holdings as well as the folded ones.
#   * The aggregated tile's body / tooltip says ``"Other
#     equities"`` -- it labels the synthesised bucket, which is
#     specifically the folded-small group.
#
# The short fallback below is used in the tile body whenever the
# tile is too narrow for ``"Other equities"`` to fit -- so a
# deeply folded tail still reads as a clear identifier on the
# chart instead of clipping to an ellipsis. The responsive long /
# short swap is implemented in CSS (see the
# ``treemap__tile-ticker-long`` / ``--short`` rules in
# ``page.css``) using the per-tile size container query already
# used for the logo / ticker swap.
_OTHER_DISPLAY_LABEL = "Other equities"
_OTHER_DISPLAY_LABEL_SHORT = "Other"

# Stable palette of sector swatches, keyed off the canonical sector
# name yfinance reports. The values are CSS custom-property names
# defined alongside the rest of the page palette in
# ``assets/src/css/page.css`` -- keeping the palette out of the
# Python source lets dark-mode adjustments live next to the rest of
# the colour overrides instead of being threaded through render
# kwargs.
_SECTOR_VARS: tuple[tuple[str, str], ...] = (
    ("Technology", "--treemap-color-tech"),
    ("Communication Services", "--treemap-color-comm"),
    ("Consumer Cyclical", "--treemap-color-cyclical"),
    ("Consumer Defensive", "--treemap-color-defensive"),
    ("Healthcare", "--treemap-color-healthcare"),
    ("Financial Services", "--treemap-color-financial"),
    ("Industrials", "--treemap-color-industrials"),
    ("Energy", "--treemap-color-energy"),
    ("Utilities", "--treemap-color-utilities"),
    ("Basic Materials", "--treemap-color-materials"),
    ("Real Estate", "--treemap-color-realestate"),
    (_OTHER_SECTOR, "--treemap-color-other"),
)
_SECTOR_COLORS: dict[str, str] = dict(_SECTOR_VARS)


def _sector_color(sector: str) -> str:
    """Return the CSS variable holding the swatch for ``sector``.

    Unknown sectors (anything yfinance reports outside the canonical
    palette above) fall back to the ``Other`` swatch so the chart's
    colour vocabulary stays bounded -- otherwise a stray non-GICS
    label would render against a default browser colour and break
    the legend's promise that "same hue means same sector".
    """
    return _SECTOR_COLORS.get(sector, _SECTOR_COLORS[_OTHER_SECTOR])


# Type alias the public entrypoint accepts: ``(ticker) -> logo_url``
# resolver. Matches :class:`investing.logos.LogoResolver` so the
# renderer can pass its ``_get_logo_url`` straight through without
# adapting the signature.
LogoResolver = Callable[[str], str]

# Companion resolver that returns a logo's intrinsic aspect ratio
# (width / height) for the equal-area sizing math. The Webpage
# callsite wires this up to :meth:`investing.logos.LogoCache.aspect_ratio`
# in production; tests and preview scripts can pass a synthetic
# function over a local SVG fixture.
LogoAspectResolver = Callable[[str], float]

# Same shape, for the rasterised ink-density probe that powers the
# equal-VISUAL-area sizing layered on top of the aspect ratio. The
# Webpage callsite wires this to
# :meth:`investing.logos.LogoCache.coverage_ratio`; tests fall back
# to a constant default when the resolver isn't injected.
LogoCoverageResolver = Callable[[str], float]


@dataclass(frozen=True)
class _Tile:
    """A single positioned rectangle ready to splat into HTML.

    Coordinates are stored in percentages of the parent container's
    width / height (0..100) so the rendered tiles inherit the
    container's intrinsic dimensions without any client-side
    measurement.
    """

    x: float
    y: float
    w: float
    h: float


def _squarify(values: Sequence[float], rect: _Tile) -> list[_Tile]:
    """Place ``values`` into ``rect`` using the squarified algorithm.

    ``values`` is the per-item weight; the returned list aligns
    positionally with the input so the caller can zip the rectangles
    back against its payload list. Items are *not* re-sorted: the
    caller is responsible for passing values in descending order
    (the squarified algorithm produces its best aspect ratios on a
    descending input, but the public callsites pass already-sorted
    data so re-sorting here would just throw away the input order
    the renderer relies on).

    The algorithm walks the items greedily, extending the current
    "row" as long as appending the next item improves (or doesn't
    worsen) the row's worst aspect ratio. When the next item would
    degrade the row, the row is laid out along the shorter side of
    the remaining rectangle and we recurse into the leftover area.
    """
    placed: list[_Tile | None] = [None] * len(values)
    if not values:
        return []
    total = sum(values)
    if total <= 0 or rect.w <= 0 or rect.h <= 0:
        empty = _Tile(rect.x, rect.y, 0.0, 0.0)
        return [empty for _ in values]

    # Scale every value into area units that sum to the rect's area.
    # Working in area units (rather than the original percentages)
    # keeps the row-layout math a clean ``area = side * thickness``
    # without re-introducing the total denominator on every step.
    area = rect.w * rect.h
    scaled = [v * area / total for v in values]

    # Indices into ``values`` that have not yet been placed. The
    # iterative walk mutates ``remaining_rect`` as each row is
    # consumed.
    remaining_idx = list(range(len(values)))
    remaining_rect = rect

    while remaining_idx:
        row_idx: list[int] = [remaining_idx[0]]
        if remaining_rect.w <= 0 or remaining_rect.h <= 0:
            for idx in remaining_idx:
                placed[idx] = _Tile(remaining_rect.x, remaining_rect.y, 0.0, 0.0)
            break
        short = min(remaining_rect.w, remaining_rect.h)
        cur_worst = _row_worst([scaled[i] for i in row_idx], short)
        i = 1
        while i < len(remaining_idx):
            candidate = row_idx + [remaining_idx[i]]
            cand_worst = _row_worst([scaled[j] for j in candidate], short)
            if cand_worst > cur_worst:
                break
            row_idx = candidate
            cur_worst = cand_worst
            i += 1

        row_values = [scaled[j] for j in row_idx]
        row_sum = sum(row_values)
        if remaining_rect.w >= remaining_rect.h:
            row_w = row_sum / remaining_rect.h
            ry = remaining_rect.y
            for j, v in zip(row_idx, row_values, strict=False):
                rh = v / row_w if row_w > 0 else 0.0
                placed[j] = _Tile(remaining_rect.x, ry, row_w, rh)
                ry += rh
            remaining_rect = _Tile(
                remaining_rect.x + row_w,
                remaining_rect.y,
                remaining_rect.w - row_w,
                remaining_rect.h,
            )
        else:
            row_h = row_sum / remaining_rect.w
            rx = remaining_rect.x
            for j, v in zip(row_idx, row_values, strict=False):
                rw = v / row_h if row_h > 0 else 0.0
                placed[j] = _Tile(rx, remaining_rect.y, rw, row_h)
                rx += rw
            remaining_rect = _Tile(
                remaining_rect.x,
                remaining_rect.y + row_h,
                remaining_rect.w,
                remaining_rect.h - row_h,
            )

        remaining_idx = remaining_idx[i:]

    return [tile if tile is not None else _Tile(0.0, 0.0, 0.0, 0.0) for tile in placed]


def _row_worst(values: Sequence[float], length: float) -> float:
    """Compute the worst aspect ratio of a row laid along ``length``.

    Returns ``+inf`` when the row contains a zero value (the
    resulting tile would be infinitely thin); the greedy walk above
    interprets ``+inf`` as "this row can't accept the new item",
    which closes the row and forces the next item into a new strip.
    """
    if not values:
        return float("inf")
    row_sum = sum(values)
    if row_sum <= 0:
        return float("inf")
    r_max = max(values)
    r_min = min(values)
    if r_min <= 0:
        return float("inf")
    s2 = row_sum * row_sum
    l2 = length * length
    return max(l2 * r_max / s2, s2 / (l2 * r_min))


@dataclass(frozen=True)
class _Row:
    """One row in the layout pipeline.

    A row is either a *real holding* (``folded_tickers`` is empty) or
    the *aggregated ``Other`` pseudo-row* the merge loop synthesises
    when one or more small holdings have been folded together (see
    :func:`_merge_small_into_other`). The pseudo-row has an empty
    ticker, an empty logo URL, and lives in the ``_OTHER_SECTOR``
    bucket; the source tickers it represents are kept in
    ``folded_tickers`` so callers can surface them in tooltips /
    aria-labels.
    """

    ticker: str
    name: str
    sector: str
    weight: float
    logo_url: str
    logo_aspect: float = _DEFAULT_LOGO_ASPECT
    logo_density: float = _LOGO_REFERENCE_DENSITY
    folded_tickers: tuple[str, ...] = ()

    @property
    def is_aggregated(self) -> bool:
        return bool(self.folded_tickers)


def _layout_rows(rows: Sequence[_Row]) -> list[tuple[_Row, _Tile]]:
    """Run the two-level squarified layout and pair rows with tiles.

    The outer layer partitions the 100x100 canvas between sectors by
    their total weight; the inner layer partitions each sector
    rectangle between the tickers inside it. Sectors and tickers are
    both ordered by weight descending -- both for squarify's aspect-
    ratio quality (largest-first is the algorithm's preferred input)
    and to match the reading affordance of "largest tile in the
    top-left corner".

    Between the two passes each sector rect is shrunk inward by
    :data:`_SECTOR_INSET_PCT` on every side so the gap between two
    *sectors* reads as visibly wider than the gap between two
    tickers in the **same** sector. The intra-sector seam stays at
    the 2 px the abutting tile outlines paint; the inter-sector seam
    adds the inset gutter on top, encoding the two-level hierarchy as
    two-level gap thickness without needing any extra markup or
    colour. See :data:`_SECTOR_INSET_PCT` for the trade-off discussion.
    """
    if not rows:
        return []
    sectors: dict[str, list[_Row]] = {}
    for r in rows:
        sectors.setdefault(r.sector, []).append(r)
    sector_totals = [(name, sum(x.weight for x in items)) for name, items in sectors.items()]
    sector_totals.sort(key=lambda st: st[1], reverse=True)
    for items in sectors.values():
        items.sort(key=lambda row: row.weight, reverse=True)

    canvas = _Tile(0.0, 0.0, 100.0, 100.0)
    sector_rects = _squarify([total for _, total in sector_totals], canvas)

    layout: list[tuple[_Row, _Tile]] = []
    for (sname, _), srect in zip(sector_totals, sector_rects, strict=False):
        items = sectors[sname]
        # Every sector pays the same inset on every side, regardless
        # of how many tiles it contains. The two-width-gap promise
        # only holds if every sector boundary contributes the same
        # ``2 * _SECTOR_INSET_PCT`` gutter -- exempting single-tile
        # sectors (an earlier attempt at "the lone tile doesn't need
        # an inner gap, just float it") produces a *third* gap width
        # at the join between a single-tile sector and a multi-tile
        # neighbour, which is exactly the visual bug this constant
        # is supposed to remove.
        padded = _inset_rect(srect, _SECTOR_INSET_PCT)
        ticker_rects = _squarify([row.weight for row in items], padded)
        for row, tile in zip(items, ticker_rects, strict=False):
            layout.append((row, tile))
    return layout


def _tile_shows_identifier(px_w: float, px_h: float) -> bool:
    """Return whether CSS would show a logo or ticker on a tile of
    ``(px_w, px_h)`` pixels.

    Mirrors the ``@container tile`` rules in ``50-treemap.css``: the
    logo appears from 80 x 50 px; otherwise the ticker / weight stack
    appears from 60 x 46 px. Either counts as a non-empty tile.
    """
    return (px_w >= _LOGO_MIN_TILE_W_PX and px_h >= _LOGO_MIN_TILE_H_PX) or (
        px_w >= _TEXT_MIN_TILE_W_PX and px_h >= _TEXT_MIN_TILE_H_PX
    )


def _tile_would_fold_at_canvas(tile: _Tile, canvas_w_px: float, canvas_h_px: float) -> bool:
    """Return whether a real holding tile should fold at ``(canvas_w_px, canvas_h_px)``.

    Uses :data:`_FOLD_SAFETY_PX` so borderline tiles that would lose their
    label after padding / sub-pixel rounding still merge into Other.
    """
    px_w = tile.w * canvas_w_px / 100.0
    px_h = tile.h * canvas_h_px / 100.0
    return not _tile_shows_identifier(
        px_w - _FOLD_SAFETY_PX,
        px_h - _FOLD_SAFETY_PX,
    )


def _tile_would_be_empty_on_canvas(tile: _Tile, canvas_w_px: float, canvas_h_px: float) -> bool:
    """Return whether ``tile`` would render as a colour-only swatch on a
    canvas of ``(canvas_w_px, canvas_h_px)`` pixels."""
    px_w = tile.w * canvas_w_px / 100.0
    px_h = tile.h * canvas_h_px / 100.0
    return not _tile_shows_identifier(px_w, px_h)


def _tile_must_fold_into_other(tile: _Tile) -> bool:
    """Return whether ``tile`` would be unlabeled at any sampled width.

    Individual equity tiles must show a logo or ticker on every
    supported viewport; holdings that would be a colour-only swatch at
    any reference canvas size fold into ``Other equities``.

    The aggregated ``Other`` pseudo-row is exempt from this probe
    (see :func:`_merge_small_into_other`).
    """
    return any(
        _tile_would_fold_at_canvas(tile, canvas_w, canvas_h)
        for canvas_w, canvas_h in _REF_CANVAS_SPECS
    )


def _inset_rect(rect: _Tile, pad: float) -> _Tile:
    """Return a copy of ``rect`` shrunk inward by ``pad`` on each side.

    Falls back to a zero-area rect at the original ``rect`` origin when
    the requested padding would consume more than the rect's width or
    height (the squarified algorithm already handles zero-area input,
    so the caller does not need a defensive branch around this).
    """
    if rect.w <= 2 * pad or rect.h <= 2 * pad:
        return _Tile(rect.x, rect.y, 0.0, 0.0)
    return _Tile(rect.x + pad, rect.y + pad, rect.w - 2 * pad, rect.h - 2 * pad)


def _merge_small_into_other_at_canvas(
    rows: Sequence[_Row],
    canvas_w_px: float,
    canvas_h_px: float,
) -> list[_Row]:
    """Fold holdings whose tile would be unlabeled at ``(canvas_w_px, canvas_h_px)``.

    Mirrors the client-side merge in ``treemap_layout.js``, which
    re-layouts against the canvas's rendered pixel dimensions on
    load and resize.
    """
    rows_list = list(rows)
    for _ in range(len(rows_list) + 1):
        layout = _layout_rows(rows_list)
        to_fold = [
            row
            for row, tile in layout
            if not row.is_aggregated and _tile_would_fold_at_canvas(tile, canvas_w_px, canvas_h_px)
        ]
        if not to_fold:
            return rows_list
        remaining_real = [row for row in rows_list if not row.is_aggregated and row not in to_fold]
        if not remaining_real:
            return rows_list
        rows_list = [row for row in rows_list if row not in to_fold]
        existing_other = next((row for row in rows_list if row.is_aggregated), None)
        batch_weight = sum(row.weight for row in to_fold)
        batch_tickers = tuple(row.ticker for row in to_fold)
        if existing_other is None:
            rows_list.append(
                _Row(
                    ticker="",
                    name="Other",
                    sector=_OTHER_SECTOR,
                    weight=batch_weight,
                    logo_url="",
                    folded_tickers=batch_tickers,
                )
            )
        else:
            rows_list = [
                _Row(
                    ticker=existing_other.ticker,
                    name=existing_other.name,
                    sector=existing_other.sector,
                    weight=existing_other.weight + batch_weight,
                    logo_url=existing_other.logo_url,
                    folded_tickers=existing_other.folded_tickers + batch_tickers,
                )
                if row.is_aggregated
                else row
                for row in rows_list
            ]
    return rows_list


def _merge_small_into_other(rows: Sequence[_Row]) -> list[_Row]:
    """Iteratively fold holdings whose tile would be unlabeled into Other.

    Each pass re-runs the squarified layout, collects every *real*
    holding whose tile would be a colour-only swatch at any sampled
    reference width (see :func:`_tile_must_fold_into_other`), and
    folds **all** of them into the aggregated ``Other`` pseudo-row
    in one batch. Batching avoids the layout thrash that one-at-a-time
    tail folding causes -- removing a single small holding can reshape
    neighbouring tiles enough to falsely trip the probe on holdings
    that were legible in the original layout.

    The loop repeats until no real holding fails the probe. The
    aggregated ``Other`` pseudo-row is exempt from the probe itself.
    """
    rows_list = list(rows)
    for _ in range(len(rows_list) + 1):
        layout = _layout_rows(rows_list)
        to_fold = [
            row
            for row, tile in layout
            if not row.is_aggregated and _tile_must_fold_into_other(tile)
        ]
        if not to_fold:
            return rows_list
        remaining_real = [row for row in rows_list if not row.is_aggregated and row not in to_fold]
        if not remaining_real:
            # Every real holding failed the probe -- keep the chart's
            # last identifiable tile rather than folding into a single
            # colour block.
            return rows_list
        rows_list = [row for row in rows_list if row not in to_fold]
        existing_other = next((row for row in rows_list if row.is_aggregated), None)
        batch_weight = sum(row.weight for row in to_fold)
        batch_tickers = tuple(row.ticker for row in to_fold)
        if existing_other is None:
            rows_list.append(
                _Row(
                    ticker="",
                    name="Other",
                    sector=_OTHER_SECTOR,
                    weight=batch_weight,
                    logo_url="",
                    folded_tickers=batch_tickers,
                )
            )
        else:
            rows_list = [row for row in rows_list if row is not existing_other]
            rows_list.append(
                _Row(
                    ticker="",
                    name="Other",
                    sector=_OTHER_SECTOR,
                    weight=existing_other.weight + batch_weight,
                    logo_url="",
                    folded_tickers=existing_other.folded_tickers + batch_tickers,
                )
            )
    return rows_list


def _rows_from_holdings(
    holdings: Iterable[dict],
    *,
    logo_url_for: LogoResolver,
    logo_aspect_for: LogoAspectResolver,
    logo_coverage_for: LogoCoverageResolver,
) -> list[_Row]:
    rows: list[_Row] = []
    for holding in holdings:
        weight = holding.get("current_weight%")
        if weight is None or weight <= 0:
            continue
        ticker = holding["ticker"]
        name = holding.get("name") or ticker
        sector = (holding.get("sector") or "").strip() or _OTHER_SECTOR
        rows.append(
            _Row(
                ticker=ticker,
                name=name,
                sector=sector,
                weight=float(weight),
                logo_url=logo_url_for(ticker),
                logo_aspect=logo_aspect_for(ticker),
                logo_density=logo_coverage_for(ticker),
            )
        )
    rows.sort(key=lambda row: row.weight, reverse=True)
    return rows


def build_payload_json(
    holdings: Iterable[dict],
    *,
    logo_url_for: LogoResolver,
    logo_aspect_for: LogoAspectResolver | None = None,
    logo_coverage_for: LogoCoverageResolver | None = None,
) -> str:
    """Serialize current-equity holdings for client-side treemap layout.

    Returns JSON text safe to embed in a ``<script type="application/json">``
    island (``</`` escaped). The treemap script hashes this payload in CSP
    when it is non-empty.
    """
    if logo_aspect_for is None:
        logo_aspect_for = _default_logo_aspect_for
    if logo_coverage_for is None:
        logo_coverage_for = _default_logo_coverage_for
    rows = _rows_from_holdings(
        holdings,
        logo_url_for=logo_url_for,
        logo_aspect_for=logo_aspect_for,
        logo_coverage_for=logo_coverage_for,
    )
    if not rows:
        return ""
    holdings_payload: list[dict[str, object]] = []
    for row in rows:
        w_factor, h_factor = _equal_area_factors(row.logo_aspect, row.logo_density)
        holdings_payload.append(
            {
                "ticker": row.ticker,
                "name": row.name,
                "sector": row.sector,
                "weight": row.weight,
                "logoUrl": row.logo_url,
                "logoWFactor": round(w_factor, 3),
                "logoHFactor": round(h_factor, 3),
                "anchor": holding_anchor(row.ticker),
                "shortTicker": strip_exchange(row.ticker),
            }
        )
    payload = {
        "holdings": holdings_payload,
        "sectorColors": dict(_SECTOR_COLORS),
        "otherSector": _OTHER_SECTOR,
        "otherDisplayLabel": _OTHER_DISPLAY_LABEL,
        "otherDisplayLabelShort": _OTHER_DISPLAY_LABEL_SHORT,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def layout_at_canvas_block(
    holdings: Iterable[dict],
    canvas_w_px: float,
    canvas_h_px: float,
    *,
    logo_url_for: LogoResolver,
    logo_aspect_for: LogoAspectResolver | None = None,
    logo_coverage_for: LogoCoverageResolver | None = None,
) -> str:
    """Render tile + legend HTML for a canvas size (test / parity helper).

    Mirrors ``treemap_layout.js`` at ``(canvas_w_px, canvas_h_px)`` so
    assertions can target the client layout without a browser.
    """
    if logo_aspect_for is None:
        logo_aspect_for = _default_logo_aspect_for
    if logo_coverage_for is None:
        logo_coverage_for = _default_logo_coverage_for
    rows = _rows_from_holdings(
        holdings,
        logo_url_for=logo_url_for,
        logo_aspect_for=logo_aspect_for,
        logo_coverage_for=logo_coverage_for,
    )
    if not rows:
        return ""
    rows = _merge_small_into_other_at_canvas(rows, canvas_w_px, canvas_h_px)
    layout = _layout_rows(rows)
    sector_totals: dict[str, float] = {}
    sector_order: list[str] = []
    for row in rows:
        if row.sector not in sector_totals:
            sector_order.append(row.sector)
            sector_totals[row.sector] = 0.0
        sector_totals[row.sector] += row.weight
    sector_order.sort(key=lambda s: -sector_totals[s])
    tile_html = [_ticker_tile(row=row, tile=tile) for row, tile in layout]
    legend_html = [_legend_chip(s, sector_totals[s]) for s in sector_order]
    return "".join(tile_html) + "".join(legend_html)


def render(
    holdings: Iterable[dict],
    *,
    logo_url_for: LogoResolver,
    logo_aspect_for: LogoAspectResolver | None = None,
    logo_coverage_for: LogoCoverageResolver | None = None,
) -> str:
    """Render the ``<figure class="treemap">`` block.

    ``holdings`` is the iterable of current-equity dicts. Cash and
    other non-equity assets never reach this renderer -- the
    ``Webpage`` callsite filters to current equity holdings before
    handing the iterable over -- so the chart is by construction a
    pure equity view.

    Layout and fold-into-Other decisions run client-side in
    ``treemap_layout.js`` against the canvas's rendered pixel size.
    The build emits a JSON payload plus an empty canvas shell; the
    script squarifies, merges unlabeled tiles into ``Other equities``,
    and paints tiles and legend chips. Per-tile logo-vs-ticker
    visibility still follows the ``@container tile`` rules in
    ``50-treemap.css``.

    ``logo_aspect_for`` resolves a logo's intrinsic aspect ratio
    (width / height) for the equal-area sizing factors emitted on
    each ``<img>``; defaults to ``_DEFAULT_LOGO_ASPECT`` when the
    resolver is omitted (every logo renders at the CSS base size
    with no factor adjustment, the same as before the equal-area
    pass was introduced).

    ``logo_coverage_for`` resolves a logo's ink-density (= the
    fraction of the rasterised bbox that survives the SVG knockout
    filter) for the equal-VISUAL-area sizing layered on top of the
    aspect-ratio normalisation. Defaults to :data:`_LOGO_REFERENCE_DENSITY`
    when the resolver is omitted, which yields a density scale of
    exactly 1.0 and preserves the pre-density-correction behaviour;
    the test stubs rely on this fall-back so they don't have to
    expose ``coverage_ratio``.

    Returns an empty string when there are no current equity
    holdings to plot. Callers should treat that signal the same way
    they do for the top-N bar chart: omit the surrounding heading /
    caption rather than render an empty container.
    """
    payload_json = build_payload_json(
        holdings,
        logo_url_for=logo_url_for,
        logo_aspect_for=logo_aspect_for,
        logo_coverage_for=logo_coverage_for,
    )
    if not payload_json:
        return ""

    return (
        '<figure class="treemap" '
        'aria-label="Equity holdings grouped by sector">'
        f"{_LOGO_KNOCKOUT_SVG}"
        '<script type="application/json" class="treemap__payload">'
        f"{payload_json}"
        "</script>"
        '<div class="treemap__canvas"></div>'
        '<figcaption class="treemap__legend" aria-hidden="true"></figcaption>'
        "</figure>"
    )


def _default_logo_aspect_for(_ticker: str) -> float:
    """Fallback aspect resolver used when ``render`` is called without one.

    Returns :data:`_DEFAULT_LOGO_ASPECT` for every ticker so the
    rendered logos all sit at the CSS base size with factor ``1`` --
    matching the behaviour before the equal-area pass landed.
    """
    return _DEFAULT_LOGO_ASPECT


def _default_logo_coverage_for(_ticker: str) -> float:
    """Fallback density resolver used when ``render`` is called without one.

    Returns ``0.0`` as a "no measurement available" sentinel. The
    consumer (:func:`_equal_area_factors`) treats any non-positive
    density as "skip the density correction entirely" and emits
    aspect-only factors -- which is the right semantic for test
    stubs and any other callsite that hasn't wired up a real
    rasterisation pipeline: in the absence of data, fall back to
    the pre-density-correction sizing rather than apply the
    correction to a synthesised default density.

    Returning ``_LOGO_REFERENCE_DENSITY`` here looks tempting (the
    formula would collapse to ``sqrt(D_ref / D_ref) = 1.0``), but
    that breaks down once ``_LOGO_DENSITY_MIN_SCALE > 1.0`` -- the
    "no-op" then becomes "clamp every logo up to MIN", which is
    surprising behaviour for a callsite that didn't ask for the
    density pass at all.
    """
    return 0.0


def _equal_area_factors(aspect: float, density: float) -> tuple[float, float]:
    """Compute ``(w_factor, h_factor)`` for an equal-VISUAL-area logo.

    The factors combine two adjustments on top of the CSS base box:

    1. **Aspect-ratio normalisation** -- keeps the bounding-box area
       ``width * height`` constant across logos with different
       intrinsic aspect ratios:

           w_aspect = sqrt(R / R_ref)
           h_aspect = sqrt(R_ref / R)

       A wide wordmark (R > R_ref) gets a wider-than-base width and
       a shorter-than-base height; a near-square icon gets the
       inverse. The product is 1 by construction.

    2. **Ink-density normalisation** -- scales the bounding-box area
       *inversely* to the source's coverage ratio so the white
       silhouette (= the part of the logo the eye actually reads on
       a coloured tile) covers approximately the same pixel area
       across brands:

           density_scale = sqrt(D_ref / D)
           w = w_aspect * clamp(density_scale, MIN, MAX)
           h = h_aspect * clamp(density_scale, MIN, MAX)

       where ``D`` is the logo's measured ink density and ``D_ref``
       is :data:`_LOGO_REFERENCE_DENSITY`. A sparse wordmark grows;
       a solid icon shrinks; the symmetric MIN / MAX clamps keep
       extreme outliers from blowing up or vanishing.

    Falls back gracefully on degenerate inputs: a non-finite or
    non-positive aspect returns ``(1.0, 1.0)`` outright (treat as
    "use the CSS base size unchanged"); a non-finite or non-positive
    density skips the density adjustment but still applies the
    aspect-ratio one.
    """
    if aspect <= 0 or not math.isfinite(aspect):
        return (1.0, 1.0)
    ratio = aspect / _LOGO_REFERENCE_ASPECT
    aspect_w = math.sqrt(ratio)
    aspect_h = math.sqrt(1.0 / ratio)
    if density <= 0 or not math.isfinite(density):
        return (aspect_w, aspect_h)
    raw_density_scale = math.sqrt(_LOGO_REFERENCE_DENSITY / density)
    density_scale = max(
        _LOGO_DENSITY_MIN_SCALE,
        min(_LOGO_DENSITY_MAX_SCALE, raw_density_scale),
    )
    return (aspect_w * density_scale, aspect_h * density_scale)


def _ticker_tile(*, row: _Row, tile: _Tile) -> str:
    """Render a single rectangle inside the treemap canvas.

    Real holdings render as ``<a>`` so clicking the rectangle scrolls
    to the matching holding capsule (same click-to-scroll affordance
    the top-N bar chart and the marquee already expose). The
    aggregated ``Other`` pseudo-row renders as a non-interactive
    ``<div>`` because there is no single holding card to anchor to.

    Identifier strategy: non-aggregated tiles emit **both** the
    logo ``<img>`` and the ticker ``<span>``; the per-tile CSS
    container size query decides which one is actually visible on
    the current viewport (see the ``@container tile`` rules in
    ``page.css``). Wide-canvas viewports show the logo; narrow ones
    swap to the ticker symbol; both share the weight-percent line
    below. The fold-into-Other decision runs client-side in
    ``treemap_layout.js`` (and in :func:`layout_at_canvas_block` for
    tests) using the ``_TEXT_MIN_TILE_*_PX`` thresholds mirrored from
    ``50-treemap.css``. Full ticker + company name +
    sector context lives on the ``aria-label`` / ``title`` for
    screen-readers and hover users.
    """
    sector_var = _sector_color(row.sector)
    label_pct = _fmt_pct(row.weight)

    style_parts = [
        f"left: {tile.x:.4f}%",
        f"top: {tile.y:.4f}%",
        f"width: {tile.w:.4f}%",
        f"height: {tile.h:.4f}%",
        f"background: var({sector_var})",
    ]
    style = "; ".join(style_parts) + ";"

    if row.is_aggregated:
        count = len(row.folded_tickers)
        # Compact tooltip for hover; aria-label gets the same string
        # so screen-readers announce the same context. The tooltip
        # always uses the long ``"Other equities"`` form because
        # hover / SR contexts aren't space-constrained, and the
        # extra word disambiguates the bucket from a real sector
        # named "Other" that yfinance might one day surface.
        tickers_blurb = ", ".join(strip_exchange(t) for t in row.folded_tickers)
        tooltip = (
            f"{_OTHER_DISPLAY_LABEL} ({count} smaller holding"
            f"{'' if count == 1 else 's'}): "
            f"{label_pct}% - {tickers_blurb}"
        )
        # Body text emits BOTH the long and short labels; CSS picks
        # which one shows based on the tile's own container width
        # (see ``.treemap__tile-ticker-long`` / ``--short`` rules in
        # ``page.css``). Emitting both rather than committing to one
        # at render time lets the same HTML reflow gracefully across
        # viewports: a tall narrow Other tile on mobile reads
        # "Other"; a wide one on desktop reads "Other equities";
        # no JS / build-time tile-width measurement needed.
        #
        # On the smallest tiles -- e.g. the worst-case mobile canvas
        # where the aggregated tile lands near the 12 %-canvas merge
        # floor (~43 x 32 px) -- CSS hides the entire
        # ``.treemap__tile-text`` span so neither label nor weight
        # try to render in a box too small to hold them without
        # overflowing the tile boundary. The legend chip below the
        # canvas still identifies the swatch, keeping the chart
        # self-documenting. The drop threshold lives in
        # ``page.css`` (the per-figure-tier ``@container treemap`` /
        # ``@container tile`` rules next to the long / short swap
        # block) rather than here because the decision needs the
        # tile's rendered px dimensions on the current viewport,
        # which the build-time renderer doesn't have access to.
        return (
            '<div class="treemap__tile treemap__tile--aggregated" '
            'role="img" '
            f'data-sector="{html.escape(row.sector)}" '
            f'style="{html.escape(style, quote=True)}" '
            f'title="{html.escape(tooltip)}" '
            f'aria-label="{html.escape(tooltip)}">'
            '<span class="treemap__tile-inner">'
            '<span class="treemap__tile-text">'
            '<span class="treemap__tile-ticker">'
            '<span class="treemap__tile-ticker-long">'
            f"{html.escape(_OTHER_DISPLAY_LABEL)}"
            "</span>"
            '<span class="treemap__tile-ticker-short">'
            f"{html.escape(_OTHER_DISPLAY_LABEL_SHORT)}"
            "</span>"
            "</span>"
            f'<span class="treemap__tile-weight">{html.escape(label_pct)}%</span>'
            "</span>"
            "</span>"
            "</div>"
        )

    short_ticker = strip_exchange(row.ticker)
    tooltip = f"{row.ticker} - {row.name} ({row.sector}): {label_pct}%"
    href = f"#{holding_anchor(row.ticker)}"

    if row.logo_url:
        # Per-logo equal-area scaling factors. Exposed to CSS as
        # custom properties on the ``<img>``'s inline style; the
        # rules in ``page.css`` multiply them onto the base
        # width / height clamps. The 3-decimal format is enough
        # precision for sub-pixel accuracy on any realistic
        # container size and keeps the rendered HTML diff-stable.
        w_factor, h_factor = _equal_area_factors(row.logo_aspect, row.logo_density)
        img_style = f"--logo-w-factor: {w_factor:.3f}; --logo-h-factor: {h_factor:.3f};"
        img_html = (
            '<img class="treemap__tile-logo" '
            f'src="{html.escape(row.logo_url)}" '
            f'alt="{html.escape(short_ticker)}" '
            'loading="lazy" decoding="async" '
            f'style="{html.escape(img_style, quote=True)}" '
            'width="48" height="24">'
        )
        tile_modifier = ""
    else:
        # No logo URL: skip the ``<img>`` and flag the tile so the
        # responsive swap rule keeps the ticker text visible even on
        # tiles that clear the logo-swap size threshold (otherwise
        # the big tile would be left with just the weight number).
        # In production this branch is only reached when the
        # resolver short-circuits to ``COURAGE_LOGO`` and the
        # caller happens to pass an empty string instead; the
        # defensive class makes that an unambiguous "render the
        # ticker as the primary identifier" signal.
        img_html = ""
        tile_modifier = " treemap__tile--no-logo"

    return (
        f'<a class="treemap__tile{tile_modifier}" '
        f'data-sector="{html.escape(row.sector)}" '
        f'href="{html.escape(href)}" '
        f'style="{html.escape(style, quote=True)}" '
        f'title="{html.escape(tooltip)}" '
        f'aria-label="{html.escape(tooltip)}">'
        '<span class="treemap__tile-inner">'
        f"{img_html}"
        '<span class="treemap__tile-text">'
        f'<span class="treemap__tile-ticker">{html.escape(short_ticker)}</span>'
        f'<span class="treemap__tile-weight">{html.escape(label_pct)}%</span>'
        "</span>"
        "</span>"
        "</a>"
    )


def _legend_chip(sector: str, weight: float) -> str:
    """Render a single ``swatch + sector name + weight`` legend chip.

    The legend names a **sector**, not the fold-up bucket: the
    ``Other`` swatch can hold both real holdings whose upstream
    sector was empty (a genuine "Other" sector entry) and the
    aggregated pseudo-row's folded-small holdings, but in either
    case the legend chip's job is to identify the sector swatch
    -- so the chip stays ``"Other"`` here. The disambiguated
    ``"Other equities"`` label is reserved for the aggregated
    tile's body / tooltip, where it names the synthesised bucket
    rather than the sector.
    """
    sector_var = _sector_color(sector)
    return (
        '<span class="treemap__legend-chip">'
        f'<span class="treemap__legend-swatch" style="background: var({sector_var});"></span>'
        f'<span class="treemap__legend-label">{html.escape(sector)}</span>'
        f'<span class="treemap__legend-weight">{html.escape(_fmt_pct(weight))}%</span>'
        "</span>"
    )
