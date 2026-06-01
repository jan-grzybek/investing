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
import os
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from dateutil.relativedelta import relativedelta

from ..formatting import _fmt_date, _fmt_pct, _format_duration
from ..log import logger
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
        card_rect,
        radius=24,
        fill=(255, 255, 255, 225),
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


def _benchmark_label(
    benchmark: BenchmarkSummary | None, display_names: dict[str, str]
) -> str | None:
    """Friendly display name for a benchmark, falling back gracefully."""
    if benchmark is None:
        return None
    ticker = benchmark.get("ticker", "")
    return display_names.get(ticker) or benchmark.get("name") or ticker or "Benchmark"


OUTPUT_FILENAME = "og-image.png"
_HASH_SIDECAR_FILENAME = OUTPUT_FILENAME + ".sha256"
# Historical module-level path constants used to be the source of truth
# when the renderer always wrote to CWD. They're kept as aliases here so
# any external code (and the test snapshots) that imported them by name
# still resolves; the resolved write path now flows through
# ``_resolve_output_dir`` from the call-site ``output_dir`` argument.
OUTPUT_PATH = OUTPUT_FILENAME
_HASH_SIDECAR_PATH = _HASH_SIDECAR_FILENAME


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
    start_date = total_return.get("start_date") or (history[0][0] if history else now)
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
        (pad_l, 36),
        "Jan Grzybek",
        font=f_name,
        fill=FG,
        stroke_width=STROKE_BIG,
        stroke_fill=HALO,
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
        (pad_l + label_w, cap_y),
        label_emph,
        font=f_caption_b,
        fill=MUTED,
    )
    emph_w = int(draw.textlength(label_emph, font=f_caption_b))
    draw.text(
        (pad_l + label_w + emph_w, cap_y),
        label_tail,
        font=f_caption,
        fill=MUTED,
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

    foot = f"Since {_fmt_date(start_date)}  \u00b7  {duration}  \u00b7  {SITE_DISPLAY}"
    draw.text((pad_l, H - 40), foot, font=f_foot, fill=MUTED)

    img.save(_resolve_output_dir(output_dir) / OUTPUT_FILENAME, optimize=True)
