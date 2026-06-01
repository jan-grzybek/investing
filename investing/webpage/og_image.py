"""OG image rendering -- the 1200x630 PNG social cards link
to whenever the portfolio URL is pasted into a feed.

The composition is tuned for a single-glance share preview: a
prominent ``Jan Grzybek`` byline, the headline outperformance vs
the S&P 500 on CAGR (the metric that earns its way once the
track record is long enough), and a strip of the top-10 equity
holdings' logos so the preview hints at *what* sits in the
portfolio without needing a click.

Extracted from :mod:`investing.webpage._page` so the renderer
class can focus on per-section HTML and the OG-specific Pillow
plumbing (font search, logo rasterisation, halo composition)
lives on its own. The ``render`` entrypoint is the only public
function; everything else here is implementation detail.
"""
from __future__ import annotations

import io
import os
from collections.abc import Iterable
from datetime import datetime

from dateutil.relativedelta import relativedelta

from ..formatting import _fmt_date, _fmt_pct, _format_duration
from ..paths import _REPO_LOGOS_DIR, LOGO_EXTENSIONS

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

    candidates = [
        os.path.join(_REPO_LOGOS_DIR, f"{ticker}{ext}")
        for ext in LOGO_EXTENSIONS
    ]
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
                    output_height=max_h * 2,
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

    Each logo is fitted into a same-width cell with consistent
    gaps so the strip reads as a uniform "what's inside" row
    regardless of the underlying logos' aspect ratios. A
    semi-transparent white pill sits behind the row so dark logo
    wordmarks stay legible when the OG image is composited on a
    dark surface; the pill is wrapped in a tight Gaussian halo
    so it visually lifts off the background. The strip is a
    no-op when ``tickers`` is empty.
    """
    from PIL import Image, ImageDraw, ImageFilter

    tickers = list(tickers)
    if not tickers:
        return

    slots = max(len(tickers), 1)
    # Tight gap on small counts, looser gap once the row fills up,
    # so a 3-ticker row doesn't look unintentionally airy.
    gap = 20 if slots >= 6 else 28
    cell_w = (w - gap * (slots - 1)) // slots
    cell_h = h
    # Center the strip horizontally within the requested width
    # when there are fewer than 10 slots so a short row still
    # feels balanced under the hero number.
    used_w = slots * cell_w + (slots - 1) * gap
    offset_x = x + (w - used_w) // 2

    # Card backdrop. Semi-transparent white pill (alpha 225 gives
    # a slight "frosted glass" softness on dark backgrounds while
    # keeping dark logo wordmarks high contrast) wrapped in a
    # tight Gaussian-blurred outer halo of the same shape,
    # lifting the card visually off dark backgrounds.
    pad_x = 24
    pad_y = 18
    card_rect = (
        offset_x - pad_x,
        y - pad_y,
        offset_x + used_w + pad_x,
        y + cell_h + pad_y,
    )
    # White-RGB transparent canvas (not (0,0,0,0)) so the
    # GaussianBlur pass below doesn't average dark transparent
    # pixels into the halo's RGB and fringe the pill's outer
    # glow with grey on white backgrounds.
    card_layer = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
    ImageDraw.Draw(card_layer).rounded_rectangle(
        card_rect, radius=24, fill=(255, 255, 255, 225),
    )
    glow_layer = card_layer.filter(ImageFilter.GaussianBlur(radius=6))
    canvas.alpha_composite(glow_layer)
    canvas.alpha_composite(card_layer)

    for idx, ticker in enumerate(tickers):
        cell_x = offset_x + idx * (cell_w + gap)
        logo = load_logo_for_og(ticker, cell_w, cell_h)
        if logo is None:
            continue
        # Center the logo within its cell -- horizontally because
        # narrow logos otherwise hug the left edge, and
        # vertically so wide logos line up on a consistent
        # midline with square ones.
        paste_x = cell_x + (cell_w - logo.width) // 2
        paste_y = y + (cell_h - logo.height) // 2
        canvas.paste(logo, (paste_x, paste_y), logo)


def _benchmark_label(benchmark: dict | None, display_names: dict[str, str]) -> str | None:
    """Friendly display name for a benchmark, falling back gracefully."""
    if benchmark is None:
        return None
    ticker = benchmark.get("ticker", "")
    return (
        display_names.get(ticker)
        or benchmark.get("name")
        or ticker
        or "Benchmark"
    )


def render(
    *,
    total_return: dict,
    benchmarks: list[dict],
    top_10: dict | None,
    benchmark_display_names: dict[str, str],
    now: datetime,
) -> None:
    """Render a 1200x630 PNG with the headline numbers for sharing.

    The image is what platforms like LinkedIn, Slack, Discord, X
    and Facebook display when the URL is pasted into a chat or
    feed. Failures (Pillow missing, unwritable disk, fonts
    unavailable, ...) are swallowed -- the page still renders
    fine without a regenerated OG image; the static fallback
    referenced by the page's ``SOCIAL_IMAGE`` constant keeps
    working until the next successful regeneration.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: F401  (used below)
    except ImportError:
        return
    try:
        _render_unsafe(
            total_return=total_return,
            benchmarks=benchmarks,
            top_10=top_10,
            benchmark_display_names=benchmark_display_names,
            now=now,
        )
    except Exception:
        # Best-effort: never fail the whole page build because the
        # OG image couldn't be drawn (e.g. on a system with no
        # truetype fonts at all).
        return


def _render_unsafe(
    *,
    total_return: dict,
    benchmarks: list[dict],
    top_10: dict | None,
    benchmark_display_names: dict[str, str],
    now: datetime,
) -> None:
    from PIL import Image, ImageDraw

    W, H = 1200, 630
    # Transparent canvas: we draw on RGBA with a fully-clear
    # background so the same OG image looks correct whether a
    # social platform places it on a light, dark, or branded
    # surface.
    TRANSPARENT = (255, 255, 255, 0)
    HALO = (255, 255, 255)
    FG = (17, 17, 17)
    MUTED = (95, 99, 106)
    ACCENT = (230, 125, 34)
    POS = (31, 122, 61)
    NEG = (179, 38, 30)

    # Tiny stroke width (in px) for the dark-mode readability
    # outline around the byline. PIL renders ``stroke_width`` as
    # opaque pixels, so the stroke disappears completely on white
    # backgrounds regardless of width.
    STROKE_BIG = 2

    bench = benchmarks[0] if benchmarks else None
    cagr = float(total_return.get("cagr%", 0.0))
    bench_cagr = float(bench["cagr%"]) if bench else None
    cagr_delta = (cagr - bench_cagr) if bench_cagr is not None else None
    bench_label = _benchmark_label(bench, benchmark_display_names)
    history = list(total_return.get("history") or [])
    start_date = (
        total_return.get("start_date")
        or (history[0][0] if history else now)
    )
    duration = _format_duration(relativedelta(now, start_date))

    f_name = load_font("bold", 96)
    f_hero = load_font("bold", 140)
    f_caption = load_font("regular", 32)
    f_caption_b = load_font("bold", 32)
    f_foot = load_font("regular", 22)

    img = Image.new("RGBA", (W, H), TRANSPARENT)
    draw = ImageDraw.Draw(img)

    pad_l = 60

    # ``Jan Grzybek`` is the byline header -- promoted from a
    # small eyebrow to the dominant identity element so the
    # share preview is recognisable from the name first.
    draw.text(
        (pad_l, 36), "Jan Grzybek", font=f_name, fill=FG,
        stroke_width=STROKE_BIG, stroke_fill=HALO,
    )
    # Accent rule under the name doubles as a visual anchor for
    # the rest of the layout.
    draw.rectangle((pad_l, 168, pad_l + 96, 176), fill=ACCENT)

    # Hero: outperformance vs the benchmark on CAGR.
    if cagr_delta is not None:
        hero_text = f"{_fmt_pct(cagr_delta, signed=True)} pp"
        hero_color = POS if cagr_delta >= 0 else NEG
        label = "Outperformance of "
        label_emph = bench_label or "S&P 500"
        label_tail = " on CAGR"
    else:
        hero_text = f"{_fmt_pct(cagr, signed=True)}%"
        hero_color = POS if cagr >= 0 else NEG
        label = "Annualized return ("
        label_emph = "CAGR"
        label_tail = ")"

    draw.text((pad_l, 210), hero_text, font=f_hero, fill=hero_color)

    cap_y = 388
    draw.text((pad_l, cap_y), label, font=f_caption, fill=MUTED)
    label_w = int(draw.textlength(label, font=f_caption))
    draw.text(
        (pad_l + label_w, cap_y), label_emph,
        font=f_caption_b, fill=MUTED,
    )
    emph_w = int(draw.textlength(label_emph, font=f_caption_b))
    draw.text(
        (pad_l + label_w + emph_w, cap_y), label_tail,
        font=f_caption, fill=MUTED,
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

    foot = (
        f"Since {_fmt_date(start_date)}  \u00b7  {duration}  \u00b7  "
        "jan-grzybek.github.io/investing"
    )
    draw.text((pad_l, H - 40), foot, font=f_foot, fill=MUTED)

    img.save("og-image.png", optimize=True)
