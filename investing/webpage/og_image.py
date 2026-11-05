"""OG image rendering -- the 1200x630 PNG social cards link
to whenever the portfolio URL is pasted into a feed.

For most readers this image *is* the portfolio: pasted into Slack
or LinkedIn it is the only surface they will ever see. So it makes
the same claim the page hero makes. The headline is the total
time-weighted gap to the benchmark over the whole history,
captioned "ahead of the / S&P 500" so the ``pp`` unit never has to
carry the meaning on its own, with the length of that history under
it. A band beneath carries both annualised rates, for the reader who
looks closer and wants the yearly edge rather than the accumulated
one. The top-10 equity logos fill a strip along the bottom edge.

Nothing in the composition is drawn below 20px, which is roughly
9px once a feed scales the card to the ~552px width it actually
renders at.

Extracted from :mod:`investing.webpage._page` so the renderer
class can focus on per-section HTML and the OG-specific Pillow
plumbing (font search, logo rasterisation) lives on its own. The
``render`` entrypoint is the only public function; everything else
here is implementation detail.
"""

from __future__ import annotations

import io
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from dateutil.relativedelta import relativedelta

from ..formatting import _fmt_pct, _format_duration
from ..log import logger
from ..logos import _DEFAULT_LOGO_ASPECT, _parse_svg_aspect_ratio
from ..paths import _REPO_LOGOS_DIR, LOGO_EXTENSIONS
from ..types import BenchmarkSummary, TotalReturn

if TYPE_CHECKING:
    # Pillow is imported lazily at every call site so a host without
    # it still renders the page (``render`` degrades to a no-op).
    # Importing the types under ``TYPE_CHECKING`` keeps that runtime
    # contract while still letting mypy check the drawing helpers.
    from PIL import Image as _Image
    from PIL import ImageDraw as _ImageDraw
    from PIL import ImageFont as _ImageFont

    Canvas = _Image.Image
    Draw = _ImageDraw.ImageDraw
    Font = _ImageFont.FreeTypeFont | _ImageFont.ImageFont
    # Pillow accepts either a colour-spec string or an RGB(A) tuple
    # wherever it takes a ``fill``; the card uses both.
    Fill = str | tuple[int, int, int] | tuple[int, int, int, int]

# Tickers in ``top_10`` keys that are not real holdings (e.g. the
# synthetic "Other equities" bucket added when there are >11 current
# positions). Skipped when picking logos for the strip.
NON_TICKER_TOP10_KEYS: frozenset[str] = frozenset({"Other equities"})


# Committed typeface. The card used to probe the host for a face --
# DejaVu, then Arial, then Helvetica, then Pillow's bitmap default --
# which meant its type was whatever the runner happened to have
# installed. A local render came out in Helvetica and the CI render of
# the identical inputs came out in DejaVu: different metrics,
# different line breaks, a different card. For an asset whose whole
# job is to be one recognisable image wherever it is pasted, that is
# not a detail.
#
# Roboto is the choice for three reasons: it is already named in the
# page's own ``font-family`` stack, so the card and the page agree on
# any machine that falls through to it; it is Apache-2.0, so
# redistributing it in a public repo is unambiguous; and it is a UI
# face designed to hold up at the small sizes the band label lands at
# once a feed scales the card down.
#
# Vendored at ``fonts/`` from the official Google release (v2.138,
# ``googlefonts/roboto-2``), alongside the upstream LICENSE. The
# directory is deliberately *not* in ``stage_site._SITE_DIRS``: these
# are build-time inputs for Pillow, not bytes the browser ever asks
# for, so publishing them to Pages would be a megabyte of dead weight.
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "fonts")
_FONT_FILES: dict[str, str] = {
    "regular": "Roboto-Regular.ttf",
    "bold": "Roboto-Bold.ttf",
}


def load_font(weight: str, size: int) -> Font:
    """Load the committed Roboto face for the requested weight/size.

    Falls back to Pillow's bitmap default only if the vendored file is
    missing or unreadable -- an installation so broken that a wrong
    typeface is the least of its problems, but the card still draws
    rather than taking the page build down with it.
    """
    from PIL import ImageFont

    name = _FONT_FILES.get(weight)
    if name is not None:
        path = os.path.join(_FONT_DIR, name)
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            logger.warning("og-image: vendored font %s unreadable, using default", name)
    return ImageFont.load_default()


def top_holdings_for_og(top_10: dict | None, *, limit: int = 10) -> list[str]:
    """Return up to ``limit`` ticker symbols for the OG logo strip.

    ``top_10`` is already sorted by weight (descending) and may
    contain a synthetic "Other equities" key when there are more
    than 11 current positions; we filter that out so only real
    tickers reach the logo loader.
    """
    if not top_10:
        return []
    tickers: list[str] = []
    for ticker in top_10:
        if ticker in NON_TICKER_TOP10_KEYS:
            continue
        tickers.append(ticker)
        if len(tickers) >= limit:
            break
    return tickers


_SVG_RASTER_MIN_H = 400


def load_logo_for_og(ticker: str, max_w: int, max_h: int) -> Canvas | None:
    """Load a ticker's logo as an RGBA ``PIL.Image`` fitted to a
    ``max_w x max_h`` box (preserving aspect ratio).

    Reads from the local ``logos/`` directory rather than going
    over HTTP, so the OG image is reproducible without a network
    round-trip and works the first time the site is deployed
    (before any logo is live behind ``LOGOS_ADDRESS``). SVG logos
    are rasterised with ``cairosvg`` at 2x the target dimensions
    for crispness; raster logos (PNG/JPG) are loaded directly.
    Falls back to ``courage.png`` when no per-ticker logo is on
    file, and returns ``None`` when even that fails so the caller
    can leave a gap rather than crash the whole image.
    """
    from PIL import Image

    candidates = [os.path.join(_REPO_LOGOS_DIR, f"{ticker}{ext}") for ext in LOGO_EXTENSIONS]
    candidates.append(os.path.join(_REPO_LOGOS_DIR, "courage.png"))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            if path.lower().endswith(".svg"):
                import cairosvg

                # Pass ``output_height`` only -- cairosvg would
                # *stretch* the SVG to a non-native aspect ratio
                # if both dimensions were pinned, which squashes
                # wide logos (Salesforce, NVIDIA, etc.). Pinning
                # the height alone keeps the natural aspect
                # ratio; the LANCZOS resize below caps the width
                # at ``max_w``. 2x supersample for crispness.
                # Rasterised at no less than 400px and scaled down
                # below: cairosvg sizes ``<mask>`` regions wrongly at
                # small outputs, which clipped the Salesforce mark to
                # its top-left corner at strip height. The LANCZOS
                # downscale doubles as the supersample.
                png_bytes = cairosvg.svg2png(
                    url=path,
                    output_height=max(_SVG_RASTER_MIN_H, max_h * 2),
                )
                src = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            else:
                src = Image.open(path).convert("RGBA")
        except Exception:
            continue

        scale = min(max_w / src.width, max_h / src.height)
        new_w = max(1, round(src.width * scale))
        new_h = max(1, round(src.height * scale))
        return src.resize((new_w, new_h), Image.LANCZOS)  # type: ignore[attr-defined]

    return None


def _og_logo_aspect(ticker: str) -> float:
    """Return the intrinsic aspect ratio of ``ticker``'s logo file.

    Mirrors :meth:`investing.logos.LogoCache.aspect_ratio` but
    reads directly off disk so the OG renderer can size each
    cell *before* the rasteriser runs. Returns
    :data:`_DEFAULT_LOGO_ASPECT` whenever the logo can't be parsed
    -- missing file, non-SVG without a parseable raster, or any
    other read failure -- so the equal-area math degrades to the
    "typical wordmark" assumption instead of crashing on a single
    bad row.
    """
    from PIL import Image

    candidates = [os.path.join(_REPO_LOGOS_DIR, f"{ticker}{ext}") for ext in LOGO_EXTENSIONS]
    candidates.append(os.path.join(_REPO_LOGOS_DIR, "courage.png"))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            if path.lower().endswith(".svg"):
                with open(path, encoding="utf-8") as f:
                    parsed = _parse_svg_aspect_ratio(f.read())
                if parsed and parsed > 0:
                    return parsed
            else:
                # Pillow refuses to open an image with a zero side
                # (``UnidentifiedImageError``, an ``OSError``), so the
                # ratio is always defined here.
                with Image.open(path) as im:
                    return im.width / im.height
        except (OSError, ValueError):
            continue
    return _DEFAULT_LOGO_ASPECT


# Reference aspect for the OG strip's equal-area logo sizing -- same
# 3 : 1 reference the treemap uses (see
# :data:`investing.webpage.sector_treemap._LOGO_REFERENCE_ASPECT`).
# Picking the same reference keeps the two compositions visually
# coherent: a logo that reads "wide" in the treemap reads "wide" in
# the OG strip too, and the same JG-portfolio wordmark distribution
# clusters around factor 1.0 in both passes.
_OG_REFERENCE_ASPECT = 3.0


# OG-card palette mirrors the CSS ``:root`` block in ``page.css``. Every
# colour the OG renderer paints derives from a webpage design-system
# token so the share preview reads as a continuation of the page rather
# than a separately-themed asset:
#
#   * ``_OG_BG`` = ``--bg`` (faint slate page surface). The OG canvas
#     is **opaque** in this colour so the same image renders identically
#     on light, dark and branded social-platform surfaces -- a
#     transparent canvas would let dark ``--fg`` text disappear into a
#     dark platform background, the readability problem the previous
#     stroke-halo workaround tried to paper over.
#   * ``_OG_CARD`` = ``--card-bg`` (pure white). The logo strip's pill
#     sits on this colour to read as a lifted card above ``--bg``, the
#     same surface layering the webpage uses for its content cards.
#   * ``_OG_FG`` = ``--fg`` (body slate). Byline + hero caption.
#   * ``_OG_MUTED`` = ``--muted``. Foot metadata; lands at WCAG-AA
#     contrast against ``_OG_BG`` (4.3 : 1) so it reads as supporting
#     context without a stroke halo.
#   * ``_OG_ACCENT`` = ``--accent`` (Tiger Orange). The JG brand mark
#     -- identical to the chapter rules under every section title and
#     the chart's JG curve.
#   * ``_OG_POS`` / ``_OG_NEG`` = ``--positive`` / ``--negative`` (Sea
#     Green / Rose Red). The same pair the BOUGHT / SOLD pills and
#     every up / down TSR readout on the page resolve to.
#   * ``_OG_BENCH`` = ``--accent-bench`` (Deep Space Blue). The
#     benchmark's swatch, matching the benchmark curve on the chart.
#   * ``_OG_RULE`` = ``--line``. The divider between the claim and
#     the two totals beside it.
_OG_BG: tuple[int, int, int] = (248, 250, 252)
_OG_CARD: tuple[int, int, int] = (255, 255, 255)
_OG_FG: tuple[int, int, int] = (15, 36, 48)
_OG_MUTED: tuple[int, int, int] = (107, 130, 145)
_OG_ACCENT: tuple[int, int, int] = (251, 133, 0)
_OG_BENCH: tuple[int, int, int] = (2, 48, 71)
_OG_RULE: tuple[int, int, int] = (221, 228, 234)
_OG_POS: tuple[int, int, int] = (42, 157, 143)
_OG_NEG: tuple[int, int, int] = (230, 57, 112)

# Logo-strip pill geometry. ``draw_top_holdings_strip`` receives the
# inner logo box and inflates it by these paddings, so a 38px-tall
# logo row renders inside a 74px pill -- enough air that a tall
# near-square mark and a thin wordmark both sit comfortably on the
# same midline.
_STRIP_PAD_X = 18
_STRIP_PAD_Y = 18
_STRIP_RADIUS = 18


def draw_top_holdings_strip(
    canvas: Canvas,
    tickers: Iterable[str],
    *,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Render up to 10 logos in a single horizontal row inside ``(x, y, w, h)``.

    Each logo is sized for **equal visual area** rather than for
    equal-cell-width fit: a wide wordmark (BABA, SPGI, NVDA) gets
    a proportionally wider but shorter bbox; a near-square mark
    (CRM, TSM) gets a narrower but taller bbox; every logo's
    ``width * height`` lands at the same value. This matches the
    treemap's equal-area logo sizing pass (see
    :func:`investing.webpage.sector_treemap._equal_area_factors`)
    and replaces the prior uniform-cell layout, where wide
    wordmarks letterboxed inside a square cell and read as ~3x
    smaller than the icon-style logos next to them.

    Layout pipeline:

    1. Parse each ticker's intrinsic aspect ratio off the local
       tight-cropped SVG (or raster fallback). Tickers whose
       aspect can't be parsed degrade to the default 3 : 1.

    2. Derive a single ``base_w`` so the sum of equal-area widths
       plus the inter-logo gaps fills the available strip width
       exactly. ``base_h`` is then derived so the tallest
       resulting cell (the squarest logo) caps at the strip
       height -- shorter cells stay centred on the strip's
       horizontal midline.

    3. Each logo's per-cell dimensions are
       ``base_w * sqrt(R/R_ref)`` wide by
       ``base_h * sqrt(R_ref/R)`` tall; the product is constant
       across rows so the visible "logo area" is uniform
       regardless of intrinsic aspect.

    A pure-white ``--card-bg`` pill sits behind the row -- the
    same lifted card surface the webpage uses to separate
    content from the faint slate page background. The pill is
    rendered sharp (no Gaussian halo) and fully opaque; the
    surrounding ``--bg`` canvas provides the contrast that lets
    the card read as a distinct surface, without the previous
    soft outer fringe that competed visually with the captions
    above/below it. The strip is a no-op when ``tickers`` is
    empty.

    ``(x, y, w, h)`` describes the *logo* box; the pill is
    :data:`_STRIP_PAD_X` / :data:`_STRIP_PAD_Y` larger on each
    axis. The strip now owns a full-width line of the card
    rather than sharing a row with the hero, so the ten marks
    divide the whole 1088px content width between them instead
    of the ~640px column the old composition left them.
    """
    from PIL import ImageDraw

    tickers = list(tickers)
    if not tickers:
        return

    aspects = [_og_logo_aspect(t) for t in tickers]
    n = len(tickers)
    # Tight gap on small counts, looser gap once the row fills up,
    # so a 3-ticker row doesn't look unintentionally airy.
    gap = 20 if n >= 6 else 28
    available_w = max(1, w - gap * (n - 1))

    # Equal-area math: every logo renders at
    # ``(base_w * sqrt(R/R_ref), base_h * sqrt(R_ref/R))``. The
    # ``w_factor`` sum determines how ``base_w`` packs into the
    # available strip width; the ``h_factor`` max determines how
    # tall the squarest logo wants to be relative to ``base_h``.
    # Both fall out cleanly from the per-aspect factors.
    w_factors = [math.sqrt(r / _OG_REFERENCE_ASPECT) for r in aspects]
    h_factors = [math.sqrt(_OG_REFERENCE_ASPECT / r) for r in aspects]
    sum_wf = sum(w_factors) or 1.0
    max_hf = max(h_factors) or 1.0
    base_w = available_w / sum_wf
    base_h = h / max_hf

    # ``--card-bg`` pill backdrop -- opaque, sharp edges. Sits on the
    # opaque ``--bg`` page surface the way card surfaces lift above
    # ``--bg`` on the webpage itself.
    card_rect = (
        x - _STRIP_PAD_X,
        y - _STRIP_PAD_Y,
        x + w + _STRIP_PAD_X,
        y + h + _STRIP_PAD_Y,
    )
    ImageDraw.Draw(canvas).rounded_rectangle(card_rect, radius=_STRIP_RADIUS, fill=_OG_CARD)

    cur_x = float(x)
    for ticker, wf, hf in zip(tickers, w_factors, h_factors, strict=True):
        target_w = max(1, round(base_w * wf))
        target_h = max(1, round(base_h * hf))
        logo = load_logo_for_og(ticker, target_w, target_h)
        if logo is not None:
            # Centre vertically on the strip's midline so a tall
            # near-square mark and a thin wide wordmark share a
            # consistent baseline rather than top-aligning.
            paste_x = round(cur_x)
            paste_y = y + (h - logo.height) // 2
            canvas.paste(logo, (paste_x, paste_y), logo)
        cur_x += base_w * wf + gap


def _benchmark_label(
    benchmark: BenchmarkSummary | None, display_names: dict[str, str]
) -> str | None:
    """Friendly display name for a benchmark, falling back gracefully."""
    if benchmark is None:
        return None
    ticker = benchmark.get("ticker", "")
    return display_names.get(ticker) or benchmark.get("name") or ticker or "Benchmark"


@dataclass(frozen=True)
class HeroCopy:
    """The headline lockup's strings, plus its sign.

    Split out of :func:`_card_copy` so the directional wording is
    unit-testable on its own.
    """

    eyebrow: str  # Names the metric being claimed, above the number.
    number: str  # The hero figure itself, already signed and rounded.
    unit: str  # "pp" or "%", set beside ``number`` at a smaller size.
    claim: tuple[str, str]  # Two lines beside the number giving ``unit`` its meaning.
    positive: bool  # Drives the hero colour (Sea Green / Rose Red).


def _hero_copy(
    twr: float,
    twr_delta: float | None,
    bench_label: str | None,
) -> HeroCopy:
    """Return the headline for a given total return and gap.

    The headline is the **total** gap: the portfolio's time-weighted
    return minus the benchmark's total return over the whole history,
    the figure the page hero leads with. A card seen cold in a feed
    and the page it links to have to make one claim, and the per-year
    rates drawn under it keep a long history from passing for a large
    yearly edge.

    * with a benchmark -> the gap in percentage points, captioned
      "ahead of the / {bench}" or "behind the / {bench}" by its sign.
      In a feed the reader gets no scale for "pp", so the words do the
      work the abbreviation cannot.
    * without one -> the portfolio's own total return, captioned
      "total return / since inception", a standalone claim rather than
      a comparison with nothing on the other side.

    ``bench_label`` falls back to "S&P 500" when present-but-empty so
    the claim never collapses to "ahead of the ".
    """
    if twr_delta is None:
        return HeroCopy(
            eyebrow="Portfolio return",
            number=_fmt_pct(twr),
            unit="%",
            claim=("total return", "since inception"),
            positive=twr >= 0,
        )
    bench = bench_label or "S&P 500"
    return HeroCopy(
        eyebrow=f"Total return vs {bench}",
        number=_fmt_pct(twr_delta, signed=True),
        unit="pp",
        claim=("ahead of the" if twr_delta >= 0 else "behind the", bench),
        positive=twr_delta >= 0,
    )


@dataclass(frozen=True)
class Rate:
    """One entry in the annualised band under the headline."""

    swatch: tuple[int, int, int]  # The chart curve's colour for this series.
    label: str
    value: str
    fill: tuple[int, int, int]


@dataclass(frozen=True)
class CardCopy:
    """Every string the card draws, worked out before anything is drawn.

    The rendered text is not round-trippable out of the raster, so the
    only way to hold the card to a contract is to test this first.
    """

    eyebrow: str
    number: str
    unit: str
    claim: tuple[str, str]
    period: str
    positive: bool
    rates: tuple[Rate, ...]


def _card_copy(
    *,
    total_return: TotalReturn,
    benchmarks: list[BenchmarkSummary],
    benchmark_display_names: dict[str, str],
    now: datetime,
) -> CardCopy:
    """Derive the card's copy from the same inputs as the page hero.

    The gap is computed exactly as :func:`investing.webpage.hero.render`
    computes it -- ``twr%`` against the first benchmark's ``tsr%`` -- so
    the card and the page cannot lead with different numbers.
    """
    bench = benchmarks[0] if benchmarks else None
    twr = float(total_return.get("twr%", 0.0))
    bench_label = _benchmark_label(bench, benchmark_display_names)
    delta = twr - float(bench["tsr%"]) if bench is not None else None
    hero = _hero_copy(twr, delta, bench_label)
    history = list(total_return.get("history") or [])
    start_date = total_return.get("start_date") or (history[0][0] if history else now)

    rates = [
        Rate(
            _OG_ACCENT,
            "Portfolio",
            f"{_fmt_pct(float(total_return.get('cagr%', 0.0)))}% / yr",
            _OG_FG,
        )
    ]
    if bench is not None and bench.get("cagr%") is not None:
        rates.append(
            Rate(
                _OG_BENCH,
                bench_label or "S&P 500",
                f"{_fmt_pct(float(bench['cagr%']))}% / yr",
                _OG_MUTED,
            )
        )
    return CardCopy(
        eyebrow=f"{hero.eyebrow} \u00b7 since {start_date.year}",
        number=hero.number,
        unit=hero.unit,
        claim=hero.claim,
        period=f"over {_format_duration(relativedelta(now, start_date))}",
        positive=hero.positive,
        rates=tuple(rates),
    )


OUTPUT_FILENAME = "og-image.png"
# Historical module-level path constant kept as an alias for any
# external code (and test snapshots) that imported it by name; the
# resolved write path now flows through ``_resolve_output_dir`` from
# the call-site ``output_dir`` argument.
OUTPUT_PATH = OUTPUT_FILENAME


def _resolve_output_dir(output_dir: Path | None) -> Path:
    """Resolve ``output_dir`` against ``Path.cwd()`` when unspecified."""
    return output_dir if output_dir is not None else Path.cwd()


def render(
    *,
    total_return: TotalReturn,
    benchmarks: list[BenchmarkSummary],
    top_10: dict[str, float] | None,
    benchmark_display_names: dict[str, str],
    now: datetime,
    output_dir: Path | None = None,
) -> None:
    """Render a 1200x630 PNG with the headline numbers for sharing.

    The image is what platforms like LinkedIn, Slack, Discord, X
    and Facebook display when the URL is pasted into a chat or
    feed. Failures (Pillow missing, unwritable disk, fonts
    unavailable, ...) are swallowed -- the page still renders
    fine without a regenerated OG image; the static fallback
    referenced by the page's ``SOCIAL_IMAGE`` constant keeps
    working until the next successful regeneration.

    The render is unconditional. It previously short-circuited on a
    content-addressable digest written to an ``og-image.png.sha256``
    sidecar, which could never fire in the deployment it was built
    for: both the PNG and the sidecar are gitignored, and the deploy
    workflow checks out a fresh tree every run, so the cache was
    always cold. The whole composition costs well under a second
    against a job that spends roughly a minute installing
    dependencies -- there was nothing there worth caching.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: F401  (used below)
    except ImportError:
        return
    out_dir = _resolve_output_dir(output_dir)
    try:
        _render_unsafe(
            total_return=total_return,
            benchmarks=benchmarks,
            top_10=top_10,
            benchmark_display_names=benchmark_display_names,
            now=now,
            output_dir=out_dir,
        )
    except Exception:
        # Best-effort: never fail the whole page build because the
        # OG image couldn't be drawn (e.g. on a system with no
        # truetype fonts at all).
        return


# Canvas + frame. 1200x630 is the Open Graph contract; the padding
# is asymmetric because the logo strip's card needs less air beneath
# it than the byline needs above.
_W, _H = 1200, 630
_PAD_X = 56
_PAD_T = 40
_PAD_B = 34


def _tracked_width(draw: Draw, text: str, font: Font, tracking: float) -> float:
    """Width of ``text`` drawn with ``tracking`` px between glyphs."""
    if not text:
        return 0.0
    return draw.textlength(text, font=font) + tracking * (len(text) - 1)


def _draw_tracked(
    draw: Draw,
    xy: tuple[float, float],
    text: str,
    font: Font,
    fill: Fill,
    tracking: float,
) -> None:
    """Draw ``text`` glyph-by-glyph with extra letter-spacing.

    Pillow has no letter-spacing knob, and the card's two uppercase
    labels (the byline and the metric eyebrow) are set in caps at
    small sizes, where tracking is what keeps them from reading as a
    solid block. Drawing per-glyph costs a handful of extra calls on
    a path that runs at most a few times a day.
    """
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking


def _fit_font(
    draw: Draw,
    text: str,
    weight: str,
    size: int,
    max_w: float,
    *,
    tracking: float = 0.0,
) -> Font:
    """Return the largest font <= ``size`` at which ``text`` fits ``max_w``.

    The card's copy grows with the benchmark's display name, and the
    fonts themselves are whatever the runner happens to have
    installed, so a fixed size cannot promise a fit. Stepping down
    2px at a time keeps a long label on one line rather than letting
    it run off the frame; the floor is 22px, which is the card's
    "nothing smaller than this survives feed scaling" rule.
    """
    for candidate in range(size, 20, -2):
        font = load_font(weight, candidate)
        if _tracked_width(draw, text, font, tracking) <= max_w:
            return font
    return load_font(weight, 22)


def _text_height(draw: Draw, text: str, font: Font) -> float:
    """Ink height of ``text`` in ``font`` (0 for an empty string)."""
    if not text:
        return 0.0
    top, bottom = draw.textbbox((0, 0), text, font=font)[1::2]
    return bottom - top


# Headline lockup sizes. The number starts at 176px and steps down
# until number + unit fit their column, so a gap that reaches three
# digits shrinks rather than running into the claim. 96px is the
# floor: below it the lockup stops reading as the headline. The unit
# keeps the design's 74 : 176 ratio at every size.
_HERO_MAX_SIZE = 176
_HERO_MIN_SIZE = 96
_HERO_UNIT_RATIO = 74 / 176


def _lockup_gap(num_size: int) -> float:
    """Space between the number and its unit, scaled with the number."""
    return 14 * num_size / _HERO_MAX_SIZE


def _lockup_width(draw: Draw, number: str, unit: str, num_font: Font, unit_font: Font) -> float:
    """Width of the "+6.7 pp" lockup at the given fonts."""
    size = getattr(num_font, "size", _HERO_MAX_SIZE)
    return (
        draw.textlength(number, font=num_font)
        + _lockup_gap(size)
        + draw.textlength(unit, font=unit_font)
    )


def _fit_lockup(draw: Draw, number: str, unit: str, max_w: float) -> tuple[Font, Font]:
    """Return the largest (number, unit) fonts whose lockup fits ``max_w``."""
    for size in range(_HERO_MAX_SIZE, _HERO_MIN_SIZE, -4):
        num_font = load_font("bold", size)
        unit_font = load_font("bold", round(size * _HERO_UNIT_RATIO))
        if _lockup_width(draw, number, unit, num_font, unit_font) <= max_w:
            return num_font, unit_font
    return (
        load_font("bold", _HERO_MIN_SIZE),
        load_font("bold", round(_HERO_MIN_SIZE * _HERO_UNIT_RATIO)),
    )


# Letter-spacing for the card's uppercase labels, in px at their
# rendered sizes (~0.1em each).
_OG_BYLINE_TRACKING = 2.7
_OG_EYEBROW_TRACKING = 2.4
_OG_BAND_LABEL_TRACKING = 2.0

# Vertical rhythm, as ink-to-ink distances.
#
# A CSS box carries leading above and below its ink, so the gap the
# eye sees is always larger than a declared margin. Pillow has no line
# boxes -- it places ink -- so these are the rendered spacings the
# design shows at 1200x630, not CSS margins.
_OG_BYLINE_INK_TOP = 6  # caps sit this far into a 27px line box
_OG_EYEBROW_TO_HERO = 29
_OG_CLAIM_LINE_GAP = 16  # between the claim's two lines
_OG_CLAIM_TO_PERIOD = 26
_OG_BAND_TO_STRIP = 20

# The annualised band: a quieter surface than the logo strip's white
# card, between the page ``--bg`` and ``--line``, so it reads as a
# caption on the headline rather than as a second card.
_OG_BAND: tuple[int, int, int] = (233, 238, 242)
_BAND_H = 58
_BAND_RADIUS = 14
_BAND_PAD_X = 24


def _render_unsafe(
    *,
    total_return: TotalReturn,
    benchmarks: list[BenchmarkSummary],
    top_10: dict[str, float] | None,
    benchmark_display_names: dict[str, str],
    now: datetime,
    output_dir: Path | None = None,
) -> None:
    from PIL import Image, ImageDraw

    copy = _card_copy(
        total_return=total_return,
        benchmarks=benchmarks,
        benchmark_display_names=benchmark_display_names,
        now=now,
    )
    tickers = top_holdings_for_og(top_10, limit=10)

    img = Image.new("RGBA", (_W, _H), (*_OG_BG, 255))
    draw = ImageDraw.Draw(img)

    content_l = _PAD_X
    content_r = _W - _PAD_X

    # ---- header: byline eyebrow + section label ----------------------
    #
    # The name is an eyebrow now, not the hero. At 96px it competed
    # with the claim; the page has the same inversion in its header
    # and the fix is the same one -- identify, then get out of the
    # way. The accent rule moves inline beside it, where it reads as
    # a brand mark rather than as a divider under a headline.
    f_byline = load_font("bold", 27)
    f_section = load_font("regular", 24)
    byline = "JAN GRZYBEK"
    byline_h = _text_height(draw, byline, f_byline)
    header_top = _PAD_T
    rule_w, rule_h = 26, 8
    rule_mid = header_top + _OG_BYLINE_INK_TOP + byline_h / 2
    draw.rectangle(
        (content_l, rule_mid - rule_h / 2, content_l + rule_w, rule_mid + rule_h / 2),
        fill=_OG_ACCENT,
    )
    _draw_tracked(
        draw,
        (
            content_l + rule_w + 14,
            header_top + _OG_BYLINE_INK_TOP - draw.textbbox((0, 0), byline, font=f_byline)[1],
        ),
        byline,
        f_byline,
        _OG_FG,
        _OG_BYLINE_TRACKING,
    )
    draw.text(
        (content_r, rule_mid),
        "Investment Portfolio",
        font=f_section,
        fill=_OG_MUTED,
        anchor="rm",
    )
    header_bottom = header_top + _OG_BYLINE_INK_TOP + byline_h

    # ---- logo strip, on the bottom edge -------------------------------
    #
    # Decoration, so it takes the bottom of the frame. There is no foot
    # line under it any more: the period is in the eyebrow and under
    # the claim, and feeds print the domain beneath every link card.
    strip_h = 38
    strip_top = _H - _PAD_B - _STRIP_PAD_Y - strip_h
    if tickers:
        draw_top_holdings_strip(
            img,
            tickers,
            x=content_l + _STRIP_PAD_X,
            y=strip_top,
            w=content_r - content_l - 2 * _STRIP_PAD_X,
            h=strip_h,
        )
    above_strip = strip_top - _STRIP_PAD_Y if tickers else _H - _PAD_B

    # ---- the annualised band, under the headline ----------------------
    #
    # What a closer look finds: both per-year rates over the whole
    # history, so a long history cannot pass for a large yearly edge.
    band_bottom = above_strip - _OG_BAND_TO_STRIP
    band_top = band_bottom - _BAND_H
    band_mid = band_top + _BAND_H / 2
    draw.rounded_rectangle(
        (content_l, band_top, content_r, band_bottom), radius=_BAND_RADIUS, fill=_OG_BAND
    )
    f_band_label = load_font("bold", 20)
    band_label = "ANNUALISED"
    band_label_h = _text_height(draw, band_label, f_band_label)
    _draw_tracked(
        draw,
        (
            content_l + _BAND_PAD_X,
            band_mid - band_label_h / 2 - draw.textbbox((0, 0), band_label, font=f_band_label)[1],
        ),
        band_label,
        f_band_label,
        _OG_MUTED,
        _OG_BAND_LABEL_TRACKING,
    )
    entries_x = (
        content_l
        + _BAND_PAD_X
        + _tracked_width(draw, band_label, f_band_label, _OG_BAND_LABEL_TRACKING)
        + 56
    )
    swatch_w, swatch_h, swatch_gap, entry_gap = 20, 7, 12, 64
    entry_texts = [f"{rate.label}  {rate.value}" for rate in copy.rates]
    f_entry = _fit_font(
        draw,
        "".join(entry_texts),
        "bold",
        30,
        content_r
        - _BAND_PAD_X
        - entries_x
        - len(entry_texts) * (swatch_w + swatch_gap)
        - entry_gap * (len(entry_texts) - 1),
    )
    x = entries_x
    for rate, text in zip(copy.rates, entry_texts, strict=True):
        draw.rectangle(
            (x, band_mid - swatch_h / 2, x + swatch_w, band_mid + swatch_h / 2),
            fill=rate.swatch,
        )
        x += swatch_w + swatch_gap
        draw.text((x, band_mid), text, font=f_entry, fill=rate.fill, anchor="lm")
        x += draw.textlength(text, font=f_entry) + entry_gap

    # ---- the headline: eyebrow, then the lockup with its claim --------
    content_w = content_r - content_l
    hero_fill = _OG_POS if copy.positive else _OG_NEG
    eyebrow = copy.eyebrow.upper()
    f_eyebrow = _fit_font(draw, eyebrow, "bold", 24, content_w, tracking=_OG_EYEBROW_TRACKING)
    # The lockup may take a little under half the width; the claim
    # takes what is left beside it.
    f_num, f_unit = _fit_lockup(draw, copy.number, copy.unit, content_w * 0.48)
    lockup_w = _lockup_width(draw, copy.number, copy.unit, f_num, f_unit)
    claim_x = content_l + lockup_w + 72
    claim_w = content_r - claim_x
    f_claim = min(
        (_fit_font(draw, line, "bold", 46, claim_w) for line in copy.claim),
        key=lambda font: getattr(font, "size", 0),
    )
    f_period = _fit_font(draw, copy.period, "regular", 30, claim_w)

    eyebrow_h = _text_height(draw, eyebrow, f_eyebrow)
    num_h = _text_height(draw, copy.number, f_num)
    stack_top = header_bottom + 30
    stack_bottom = band_top - 30
    stack_h = eyebrow_h + _OG_EYEBROW_TO_HERO + num_h
    y = stack_top + (stack_bottom - stack_top - stack_h) / 2

    _draw_tracked(
        draw,
        (content_l, y - draw.textbbox((0, 0), eyebrow, font=f_eyebrow)[1]),
        eyebrow,
        f_eyebrow,
        _OG_MUTED,
        _OG_EYEBROW_TRACKING,
    )
    num_top = y + eyebrow_h + _OG_EYEBROW_TO_HERO
    draw.text(
        (content_l, num_top - draw.textbbox((0, 0), copy.number, font=f_num)[1]),
        copy.number,
        font=f_num,
        fill=hero_fill,
    )
    # The unit sits on the number's baseline rather than on its own
    # box, so "+6.7" and "pp" read as one lockup at two sizes.
    draw.text(
        (
            content_l
            + draw.textlength(copy.number, font=f_num)
            + _lockup_gap(getattr(f_num, "size", _HERO_MAX_SIZE)),
            num_top + num_h,
        ),
        copy.unit,
        font=f_unit,
        fill=hero_fill,
        anchor="ls",
    )

    # The claim block is centred on the numerals, so the two lines and
    # the period read as the caption of the figure beside them.
    line_h = max(_text_height(draw, line, f_claim) for line in copy.claim)
    period_h = _text_height(draw, copy.period, f_period)
    block_h = 2 * line_h + _OG_CLAIM_LINE_GAP + _OG_CLAIM_TO_PERIOD + period_h
    cy = num_top + (num_h - block_h) / 2
    for line in copy.claim:
        # Line boxes, not ink, so "ahead of the" and "S&P 500" share a
        # baseline grid whatever their ascenders.
        draw.text(
            (claim_x, cy + line_h),
            line,
            font=f_claim,
            fill=_OG_FG,
            anchor="ls",
        )
        cy += line_h + _OG_CLAIM_LINE_GAP
    cy += _OG_CLAIM_TO_PERIOD - _OG_CLAIM_LINE_GAP
    draw.text(
        (claim_x, cy - draw.textbbox((0, 0), copy.period, font=f_period)[1]),
        copy.period,
        font=f_period,
        fill=_OG_MUTED,
    )

    img.save(_resolve_output_dir(output_dir) / OUTPUT_FILENAME, optimize=True)
