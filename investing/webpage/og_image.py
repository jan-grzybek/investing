"""OG image rendering -- the 1200x630 PNG social cards link
to whenever the portfolio URL is pasted into a feed.

The composition is tuned for a single-glance share preview: a
prominent ``Jan Grzybek`` byline, the headline out/underperformance
vs the S&P 500 on CAGR (the metric that earns its way once the
track record is long enough -- the caption flips between
"Outperformance" and "Underperformance" with the sign so the
share preview never claims a lead it doesn't have), and a strip
of the top-10 equity holdings' logos so the preview hints at
*what* sits in the portfolio without needing a click.

Extracted from :mod:`investing.webpage._page` so the renderer
class can focus on per-section HTML and the OG-specific Pillow
plumbing (font search, logo rasterisation, halo composition)
lives on its own. The ``render`` entrypoint is the only public
function; everything else here is implementation detail.

The rendered PNG is also content-addressable: every successful
:func:`render` writes an ``og-image.png.sha256`` sidecar with a
SHA-256 of the inputs that produced it. A subsequent call with the
same inputs short-circuits without re-running Pillow, which keeps
the hourly schedule cheap when only the live market tape moved (the
chart / holding numbers are inline HTML, not pixel input to the OG
composition).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from dateutil.relativedelta import relativedelta

from ..formatting import _fmt_date, _fmt_pct, _format_duration
from ..log import logger
from ..logos import _DEFAULT_LOGO_ASPECT, _parse_svg_aspect_ratio
from ..paths import _REPO_LOGOS_DIR, LOGO_EXTENSIONS, SITE_DISPLAY
from ..types import BenchmarkSummary, TotalReturn

# Tickers in ``top_10`` keys that are not real holdings (e.g. the
# synthetic "Other equities" bucket added when there are >11 current
# positions). Skipped when picking logos for the strip.
NON_TICKER_TOP10_KEYS: frozenset[str] = frozenset({"Other equities"})


# Search order for sans-serif fonts. Picks the first installed
# candidate; falls back to Pillow's bitmap default if none exist
# (still readable, just less crisp).
_FONT_CANDIDATES: dict[str, tuple[tuple[str, int], ...]] = {
    "regular": (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf", 0),
        ("/Library/Fonts/Arial.ttf", 0),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 0),
        ("C:/Windows/Fonts/arial.ttf", 0),
    ),
    "bold": (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", 0),
        ("/Library/Fonts/Arial Bold.ttf", 0),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1),
        ("C:/Windows/Fonts/arialbd.ttf", 0),
    ),
}


def load_font(weight: str, size: int):
    """Pick the first installed candidate for the requested weight/size."""
    from PIL import ImageFont

    for path, idx in _FONT_CANDIDATES.get(weight, ()):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                continue
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


def load_logo_for_og(ticker: str, max_w: int, max_h: int):
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
                png_bytes = cairosvg.svg2png(
                    url=path,
                    output_height=max(2, max_h * 2),
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
                with Image.open(path) as im:
                    if im.width > 0 and im.height > 0:
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
_OG_BG: tuple[int, int, int] = (248, 250, 252)
_OG_CARD: tuple[int, int, int] = (255, 255, 255)
_OG_FG: tuple[int, int, int] = (15, 36, 48)
_OG_MUTED: tuple[int, int, int] = (107, 130, 145)
_OG_ACCENT: tuple[int, int, int] = (251, 133, 0)
_OG_POS: tuple[int, int, int] = (42, 157, 143)
_OG_NEG: tuple[int, int, int] = (230, 57, 112)


def draw_top_holdings_strip(
    canvas,
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
    pad_x = 24
    pad_y = 18
    card_rect = (x - pad_x, y - pad_y, x + w + pad_x, y + h + pad_y)
    ImageDraw.Draw(canvas).rounded_rectangle(card_rect, radius=24, fill=_OG_CARD)

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


def _hero_caption(cagr_delta: float | None, bench_label: str | None) -> tuple[str, str, str]:
    """Return the three caption pieces (``prefix``, ``emph``, ``tail``)
    rendered under the hero number.

    Split out of :func:`_render_unsafe` so the directional copy is
    unit-testable without rasterising a PNG and OCR'ing it back. The
    contract:

    * with a benchmark and a non-negative delta -> "Outperformance of
      {bench} on CAGR" (the canonical share-preview claim);
    * with a benchmark and a negative delta -> "Underperformance of
      {bench} on CAGR" (mirrors the symmetric "outperformance (or
      underperformance)" framing the in-page returns-compare block
      uses, so the OG card never claims a lead the page itself
      doesn't show);
    * without a benchmark -> "Annualized return (CAGR)" so the hero
      reads as a standalone metric rather than a comparison.

    ``bench_label`` falls back to "S&P 500" when present-but-empty so
    the caption never collapses to "Outperformance of  on CAGR".
    """
    if cagr_delta is None:
        return ("Annualized return (", "CAGR", ")")
    word = "Outperformance" if cagr_delta >= 0 else "Underperformance"
    return (f"{word} of ", bench_label or "S&P 500", " on CAGR")


OUTPUT_FILENAME = "og-image.png"
_HASH_SIDECAR_FILENAME = OUTPUT_FILENAME + ".sha256"
# Historical module-level path constant kept as an alias for any
# external code (and test snapshots) that imported it by name; the
# resolved write path now flows through ``_resolve_output_dir`` from
# the call-site ``output_dir`` argument. The matching
# ``_HASH_SIDECAR_PATH`` alias was removed because nothing imported
# it -- the live sidecar path is built from ``_HASH_SIDECAR_FILENAME``
# and the resolved output directory wherever it's needed.
OUTPUT_PATH = OUTPUT_FILENAME


def _resolve_output_dir(output_dir: Path | None) -> Path:
    """Resolve ``output_dir`` against ``Path.cwd()`` when unspecified."""
    return output_dir if output_dir is not None else Path.cwd()


def _input_digest(
    *,
    total_return: TotalReturn,
    benchmarks: list[BenchmarkSummary],
    top_10: dict[str, float] | None,
    benchmark_display_names: dict[str, str],
    now: datetime,
) -> str:
    """Return a stable SHA-256 over the OG image's pixel inputs.

    Only the quantities that the composition actually reads should
    feed the hash: the headline CAGR (JG + benchmark), the benchmark
    display label, the top-10 ticker list (drives the logo strip),
    and the ``start_date`` / ``now`` pair (drives the "Since X" foot
    caption + duration). The full ``history`` list is deliberately
    excluded -- it doesn't reach the canvas, so a daily TWR re-fix
    that doesn't change anything in the headline shouldn't force a
    rerender.

    ``now`` is rounded to the calendar day: the foot caption renders
    a date-precision duration ("3 years, 4 months"), so two runs on
    the same day with identical numerical inputs would otherwise hash
    differently and re-render needlessly.
    """
    bench = benchmarks[0] if benchmarks else None
    history = total_return.get("history") or []
    start_from_history = history[0][0] if history else None
    payload = {
        "cagr": _round(total_return.get("cagr%")),
        "bench_cagr": _round(bench.get("cagr%") if bench else None),
        "bench_label": _benchmark_label(bench, benchmark_display_names),
        "tickers": top_holdings_for_og(top_10, limit=10),
        "start_date": _iso_day(total_return.get("start_date") or start_from_history),
        "today": _iso_day(now),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _round(value: float | None) -> float | None:
    """Quantise a percentage to one decimal place so micro-jitter in
    the source numbers (e.g. a ``regularMarketPrice`` tick that nudges
    CAGR by 0.001 pp) doesn't invalidate the rendered cache.

    The composition itself rounds to one decimal at format time
    (see ``_fmt_pct``); aligning the cache key with what's actually
    drawn avoids cache misses that would not change any drawn pixel.
    """
    if value is None:
        return None
    return round(float(value), 1)


def _iso_day(value) -> str | None:
    """Render a ``datetime`` / ``date`` as ISO ``YYYY-MM-DD`` text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _read_sidecar(output_dir: Path) -> str | None:
    """Read the digest of the OG image on disk, if any.

    Missing file / unreadable / wrong size all return ``None`` so the
    caller treats the cache as cold and re-renders.
    """
    try:
        text = (output_dir / _HASH_SIDECAR_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if len(text) == 64 else None


def _write_sidecar(output_dir: Path, digest: str) -> None:
    """Persist ``digest`` next to the rendered PNG.

    Best-effort: a write failure is logged but never propagated, so
    the page build doesn't abort because the cache key couldn't be
    saved. The worst case is one extra rerender next run.
    """
    try:
        (output_dir / _HASH_SIDECAR_FILENAME).write_text(digest + "\n", encoding="utf-8")
    except OSError as exc:
        # ``str(exc)`` would land on the safe-run-redacted stderr in
        # CI; logger.debug routes the same content through the
        # logger which is silenced under leak-safe wrapping. Local
        # runs surface it at DEBUG.
        logger.debug("og-image sidecar write failed: %s", exc)


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

    Short-circuits on content-addressable caching: if the PNG on
    disk was produced from the same set of headline numbers (see
    :func:`_input_digest`), the redraw is skipped entirely. The
    hourly CI cadence runs even when only intraday quotes moved,
    and the OG composition doesn't surface intraday moves -- the
    skip path keeps that wasted Pillow work out of the schedule.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: F401  (used below)
    except ImportError:
        return
    out_dir = _resolve_output_dir(output_dir)
    digest = _input_digest(
        total_return=total_return,
        benchmarks=benchmarks,
        top_10=top_10,
        benchmark_display_names=benchmark_display_names,
        now=now,
    )
    output_path = out_dir / OUTPUT_FILENAME
    if output_path.is_file() and _read_sidecar(out_dir) == digest:
        logger.info("og-image: cache hit, skipping render")
        return
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
    _write_sidecar(out_dir, digest)


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

    W, H = 1200, 630
    # The palette tokens live at module scope so the strip renderer
    # below can paint its card-coloured pill from the same deck.
    BG = _OG_BG
    FG = _OG_FG
    MUTED = _OG_MUTED
    ACCENT = _OG_ACCENT
    POS = _OG_POS
    NEG = _OG_NEG

    bench = benchmarks[0] if benchmarks else None
    cagr = float(total_return.get("cagr%", 0.0))
    bench_cagr = float(bench["cagr%"]) if bench else None
    cagr_delta = (cagr - bench_cagr) if bench_cagr is not None else None
    bench_label = _benchmark_label(bench, benchmark_display_names)
    history = list(total_return.get("history") or [])
    start_date = total_return.get("start_date") or (history[0][0] if history else now)
    duration = _format_duration(relativedelta(now, start_date))

    f_name = load_font("bold", 96)
    f_hero = load_font("bold", 140)
    f_caption = load_font("regular", 32)
    f_caption_b = load_font("bold", 32)
    f_foot = load_font("regular", 22)

    img = Image.new("RGBA", (W, H), (*BG, 255))
    draw = ImageDraw.Draw(img)

    pad_l = 60

    # ``Jan Grzybek`` is the byline header -- promoted from a
    # small eyebrow to the dominant identity element so the
    # share preview is recognisable from the name first. With
    # the opaque ``BG`` surface the dark ``FG`` slate reads at
    # full contrast everywhere; no stroke halo is needed.
    draw.text((pad_l, 36), "Jan Grzybek", font=f_name, fill=FG)
    # Accent rule under the name doubles as a visual anchor for
    # the rest of the layout.
    draw.rectangle((pad_l, 168, pad_l + 96, 176), fill=ACCENT)

    # Hero: out/underperformance vs the benchmark on CAGR. The
    # leading caption word flips with the sign of ``cagr_delta``
    # (see :func:`_hero_caption`) so the share preview tells the
    # truth even when JG trails the benchmark -- the prior static
    # "Outperformance of ..." copy contradicted the red hero
    # number on underperforming windows.
    if cagr_delta is not None:
        hero_text = f"{_fmt_pct(cagr_delta, signed=True)} pp"
        hero_color = POS if cagr_delta >= 0 else NEG
    else:
        hero_text = f"{_fmt_pct(cagr, signed=True)}%"
        hero_color = POS if cagr >= 0 else NEG
    label, label_emph, label_tail = _hero_caption(cagr_delta, bench_label)

    draw.text((pad_l, 210), hero_text, font=f_hero, fill=hero_color)

    # Caption sits above the logo card. ``FG`` body slate on the
    # opaque ``BG`` page surface gives full WCAG-AAA contrast
    # (15.4 : 1) so the label reads cleanly without a halo
    # outline. The bold middle word ("S&P 500" / "CAGR") carries
    # the hierarchical weight via font weight, not colour.
    cap_y = 388
    draw.text((pad_l, cap_y), label, font=f_caption, fill=FG)
    label_w = int(draw.textlength(label, font=f_caption))
    draw.text((pad_l + label_w, cap_y), label_emph, font=f_caption_b, fill=FG)
    emph_w = int(draw.textlength(label_emph, font=f_caption_b))
    draw.text(
        (pad_l + label_w + emph_w, cap_y),
        label_tail,
        font=f_caption,
        fill=FG,
    )

    # Logo strip: top-10 current holdings by weight.
    draw_top_holdings_strip(
        img,
        top_holdings_for_og(top_10, limit=10),
        x=pad_l,
        y=470,
        w=W - 2 * pad_l,
        h=90,
    )

    # Foot metadata gets the ``MUTED`` slate so it reads as
    # supporting context rather than competing with the hero
    # caption. ``MUTED`` on the opaque ``BG`` page surface
    # lands at WCAG-AA contrast (4.3 : 1) -- readable without
    # any stroke outline.
    foot = f"Since {_fmt_date(start_date)}  \u00b7  {duration}  \u00b7  {SITE_DISPLAY}"
    draw.text((pad_l, H - 40), foot, font=f_foot, fill=MUTED)

    img.save(_resolve_output_dir(output_dir) / OUTPUT_FILENAME, optimize=True)
