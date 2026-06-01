"""The ``Webpage`` renderer plus the ``generate_webpage``
entrypoint that wires per-section content into a single
rendered ``index.html`` + companion artefacts.

This module hosts the main :class:`Webpage` class. The renderer is
intentionally kept together because most of its sections share
internal state through the instance; the self-contained helpers
(anchors, sitemap/robots, logo cache) live next to it in the
``investing.webpage`` package.
"""
from __future__ import annotations

import html
import io
import json
import os
from datetime import datetime

import numpy as np
from dateutil.relativedelta import relativedelta

from ..clock import NowFn
from ..errors import InvariantError
from ..formatting import (
    _fmt_date,
    _fmt_date_long,
    _fmt_pct,
    _fmt_quarter_range,
    _format_duration,
    _format_sort_number,
    _value_class,
)
from ..holdings import CAGR_TBA_THRESHOLD
from ..logos import LogoCache
from ..paths import _REPO_LOGOS_DIR, COURAGE_LOGO, LOGO_EXTENSIONS
from ..pchip import Pchip
from ..performance import _BENCHMARK_DISPLAY_NAMES
from ..trades import _BUY_CATEGORIES, _TRADE_ACTION_DISPLAY, _TRADE_DETAIL_LABELS
from .anchors import holding_anchor, strip_exchange
from .head import SiteMeta, build_head, build_jsonld
from .sitemap import write_robots_txt, write_sitemap


class Webpage:
    """Builds the JG Investing index page as a single responsive document."""

    def __init__(self, *, now: NowFn | None = None, logo_cache: LogoCache | None = None):
        self.return_html: str = ""
        self.current: list[str] = []
        self.historical: list[str] = []
        self.allocation_pct: dict[str, float] | None = None
        self.top_10: dict[str, float] | None = None
        # Pre-rendered HTML for each row in the "Trades"
        # section, in newest-first order. Populated by
        # ``add_trades``; an empty list omits the whole section
        # (and its nav link) cleanly.
        self.trades: list[str] = []
        # Logo URLs are looked up via HTTP HEAD; the resolver wraps
        # a ``requests`` session with retry / timeout / negative-cache
        # behaviour so an outage cannot hang the build. Tests inject
        # a stub callable here instead.
        self._logo_resolver: LogoCache = logo_cache if logo_cache is not None else LogoCache()
        # ``(ticker, name, logo_url)`` tuples for current holdings, in
        # the order they were added. Drives the marquee ticker.
        self._current_logos: list[tuple[str, str, str]] = []
        # Stashed for OG image generation in ``save()``.
        self._total_return: dict | None = None
        self._benchmarks: list | None = None
        # Wall-clock plug used in the footer / sitemap / "Since X"
        # captions. ``None`` falls through to ``datetime.today`` so
        # the legacy ``freeze_today`` fixture (which monkeypatches
        # this module's bound ``datetime``) keeps working; new code
        # can inject a fixed closure directly.
        self._now: NowFn = now if now is not None else datetime.today

    # ------------------------------------------------------------------ API

    def add_return(self, total_return, benchmarks):
        self._total_return = total_return
        self._benchmarks = benchmarks
        self.return_html = self._build_return_section(total_return, benchmarks)

    def add_holding(self, holding):
        if holding["is_current"]:
            self._current_logos.append((
                holding["ticker"],
                holding["name"],
                self._get_logo_url(holding["ticker"]),
            ))
        card = self._build_holding_card(holding)
        bucket = self.current if holding["is_current"] else self.historical
        bucket.append(card)

    def add_allocations(self, allocation_pct, top_10):
        self.allocation_pct = allocation_pct
        self.top_10 = top_10

    def add_trades(self, trade_events):
        """Render each burst-aggregated trade event into a table row.

        ``trade_events`` is the newest-first list produced by
        ``get_holdings`` (or by ``Holding.trade_events`` directly in
        the preview/test paths). Rows are stored pre-rendered as
        ``<tr>`` fragments so the page assembly in ``save()`` stays
        linear; ``_build_trades_table`` wraps them with the matching
        ``<thead>`` and sortable column headers."""
        self.trades = [
            self._build_trade_row(event) for event in trade_events
        ]

    def save(self):
        now = self._now()
        # Long-form date here ("Updated on May 31, 2026") rather than
        # the page-wide DD/MM/YYYY -- the footer line reads as prose,
        # not as tabular data, so slashes break the sentence the same
        # way they did under the chart's "Since X" caption above
        # ``_render_return_chart``. The ISO ``<time datetime="...">``
        # attribute stays in W3C YYYY-MM-DD form regardless.
        update_date = _fmt_date_long(now)
        update_iso = now.strftime("%Y-%m-%d")
        # Best-effort: generate the OG image first so its filename can
        # be referenced from <head>. If Pillow / fonts aren't available
        # the page still renders, just without a fresh social preview.
        self._render_og_image()
        parts: list[str] = []
        parts.append('<!DOCTYPE html>')
        parts.append('<html lang="en">')
        parts.append(self._head())
        parts.append('<body>')
        # Skip link: visually hidden until focused, lets keyboard users
        # bypass the sticky nav and jump straight to <main>.
        parts.append(
            '<a class="skip-link" href="#main-content">Skip to content</a>'
        )
        parts.append(self._build_site_header())
        parts.append('<main id="main-content" tabindex="-1">')

        ticker = self._build_ticker()
        if ticker:
            parts.append(ticker)

        parts.append('<section id="performance" class="section section--return">')
        parts.append('<h2 class="section__title">All-time performance</h2>')
        parts.append(self.return_html or '<p>No data yet.</p>')
        parts.append('</section>')

        if self.current:
            parts.append('<section id="current" class="section section--current">')
            parts.append('<h2 class="section__title">Current holdings</h2>')
            if self.allocation_pct:
                parts.append('<h3 class="section__subtitle">Asset allocation</h3>')
                # The "Equities" allocation row is clickable: it
                # jumps to the equities sub-section directly below
                # (where the per-ticker breakdown + individual
                # capsules live). The cash row has no dedicated
                # section to point at and stays a plain bar.
                parts.append(self._render_bars(
                    list(self.allocation_pct.items()),
                    "allocation",
                    anchors={"Equities": "equities"},
                ))
            parts.append(
                '<h3 id="equities" class="section__subtitle">Equities</h3>'
            )
            if self.top_10:
                # Each ticker bar in the top-10 chart jumps to the
                # matching holding capsule. The synthetic "Other
                # equities" bucket is absent from the anchor map
                # so it stays a plain (non-linked) bar.
                equity_anchors = {
                    ticker: self._holding_anchor(ticker)
                    for ticker in self.top_10
                    if ticker not in self._NON_TICKER_TOP10_KEYS
                }
                parts.append(self._render_bars(
                    list(self.top_10.items()),
                    "equities",
                    scale_to_max=True,
                    anchors=equity_anchors,
                ))
            parts.append(self._build_holdings_sort_control(
                scope="current",
                include_weight=True,
            ))
            parts.append(
                '<div class="holdings__list" data-holdings-list="current">'
            )
            parts.append('\n'.join(self.current))
            parts.append('</div>')
            parts.append('</section>')

        if self.historical:
            parts.append('<section id="historical" class="section section--historical">')
            parts.append('<h2 class="section__title">Historical holdings</h2>')
            parts.append(self._build_holdings_sort_control(
                scope="historical",
                include_weight=False,
            ))
            parts.append(
                '<div class="holdings__list" data-holdings-list="historical">'
            )
            parts.append('\n'.join(self.historical))
            parts.append('</div>')
            parts.append('</section>')

        if self.trades:
            parts.append('<section id="trades" class="section section--trades">')
            parts.append('<h2 class="section__title">Trades</h2>')
            # Subtitle pins the one methodology detail the reader
            # would otherwise have to infer from the data: what
            # "combined" rows represent. The section now spans the
            # full ownership history (the year-back horizon is gone)
            # so the subtitle no longer mentions a retention window;
            # the sortable date column lets the reader find recent
            # activity on their own terms. The "rolling quarter"
            # wording matches the long-term-investor framing of the
            # page (a fund-letter cadence rather than a high-frequency
            # trade log) and is the natural human reading of the
            # 90-day numerical ``TRADE_WINDOW_DAYS`` constant.
            parts.append(
                '<p class="section__intro">'
                'Every executed trade since inception. Fills within a '
                'rolling quarter are combined into a single entry at '
                'their volume-weighted average per-share price.'
                '</p>'
            )
            parts.append(self._build_trades_table(self.trades))
            parts.append('</section>')

        parts.append('</main>')
        parts.append(self._footer(update_date, update_iso))
        parts.append(
            "<!-- Cloudflare Web Analytics -->"
            "<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
            "data-cf-beacon='{\"token\": \"8f450af27c86439fb0e9ab0031c76d6e\"}'></script>"
            "<!-- End Cloudflare Web Analytics -->"
        )
        parts.append('</body>')
        parts.append('</html>')

        with open("index.html", "w") as f:
            f.write("\n".join(parts))
        write_sitemap(self.SITE_URL, now=self._now)
        write_robots_txt(self.SITE_URL)

    # ----------------------------------------------------------- internals

    # Page title + nav links rendered above ``<main>``. Nav is built
    # dynamically so we never produce dead anchors when a section is
    # absent (e.g. an account with no historical positions yet).
    SITE_TITLE = "Jan Grzybek Investment Portfolio"
    # Used in <title>, OG/Twitter title, and JSON-LD. Keep it short so
    # search engines render it without truncation in SERPs (~60 chars).
    SEO_TITLE = "Jan Grzybek - Investment Portfolio"
    SITE_URL = "https://jan-grzybek.github.io/investing/"
    # ~155 chars: long enough to surface keywords, short enough that
    # search engines won't truncate the snippet on result pages.
    SITE_DESCRIPTION = (
        "Personal investment portfolio of Jan Grzybek: time-weighted "
        "return (TWR) vs the S&P 500, current asset allocation, equity "
        "holdings, and historical positions with TSR/CAGR."
    )
    # The OG image is regenerated on every ``save()`` with the latest
    # numbers baked in. Cache-busting on the social-platform side
    # happens via the ``og:updated_time`` header below.
    SOCIAL_IMAGE = "https://jan-grzybek.github.io/investing/og-image.png"
    _NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
        ("performance", "Performance", "return_html"),
        ("current", "Current", "current"),
        ("historical", "Historical", "historical"),
        ("trades", "Trades", "trades"),
    )

    def _build_site_header(self) -> str:
        links = []
        for anchor, label, attr in self._NAV_ITEMS:
            value = getattr(self, attr)
            if value:
                links.append(f'<a href="#{anchor}">{html.escape(label)}</a>')
        nav_html = (
            f'<nav class="site-nav" aria-label="Page sections">{"".join(links)}</nav>'
            if len(links) > 1 else ""
        )
        return (
            '<header class="site-header">'
            f'<h1 class="site-title">{html.escape(self.SITE_TITLE)}</h1>'
            f'{nav_html}'
            '</header>'
        )

    def _build_ticker(self) -> str:
        """Render a slow horizontal marquee of current-holdings logos.

        Each logo carries the ticker + name in its ``title`` attribute
        for sighted users who hover and is wrapped in an in-page
        anchor that scrolls down to the matching holding capsule when
        clicked. The track contains two copies of the logo set so the
        keyframe can translate by exactly -50% and the loop is
        seamless. The strip itself is decorative (``aria-hidden=
        "true"``) and each link carries ``tabindex="-1"`` so the
        invisible marquee never traps keyboard focus -- but a
        sighted user pointer-clicking a logo still gets navigated to
        the capsule. The actual holding details live in the cards
        below."""
        if not self._current_logos:
            return ""
        items = "".join(
            # Ticker is above the fold so we don't lazy-load, but we
            # still set ``decoding="async"`` so the marquee paints as
            # soon as the first logo is ready. Both ``width`` and
            # ``height`` are pinned at the desktop cell dimensions
            # (56x28 -- the landscape 2:1 cell that normalizes wide
            # and square wordmarks to similar visual prominence; see
            # the ``.ticker__logo`` CSS for the rationale) so the
            # browser reserves the exact box up-front and the
            # marquee paints with zero layout shift even before
            # individual SVGs decode. CSS ``object-fit: contain``
            # fits each logo inside that box without distortion;
            # smaller viewports override the dimensions further down
            # in ``_PAGE_STYLES`` so the cell scales gracefully on
            # mobile.
            f'<a class="ticker__link" '
            f'href="#{html.escape(self._holding_anchor(ticker))}" '
            f'tabindex="-1" aria-hidden="true">'
            f'<img class="ticker__logo" src="{html.escape(url)}" alt="" '
            f'title="{html.escape(f"{ticker} - {name}")}" '
            f'decoding="async" width="56" height="28">'
            f'</a>'
            for ticker, name, url in self._current_logos
        )
        return (
            '<div class="ticker" aria-hidden="true">'
            f'<div class="ticker__track">{items}{items}</div>'
            '</div>'
        )

    @classmethod
    def _site_meta(cls) -> SiteMeta:
        """The class-attribute bundle :func:`build_head` consumes.

        Subclasses (e.g. a hypothetical staging build pointing at a
        different domain) can override the constants individually
        and keep the head builder honest -- the assembly happens off
        a single ``SiteMeta`` instance rather than five separate
        ``cls.SITE_X`` reads scattered through the head module.
        """
        return SiteMeta(
            title=cls.SITE_TITLE,
            seo_title=cls.SEO_TITLE,
            description=cls.SITE_DESCRIPTION,
            url=cls.SITE_URL,
            social_image=cls.SOCIAL_IMAGE,
        )

    @classmethod
    def _head(cls) -> str:
        """Delegate to :func:`investing.webpage.head.build_head`."""
        return build_head(cls._site_meta())

    @classmethod
    def _jsonld(cls) -> str:
        """Delegate to :func:`investing.webpage.head.build_jsonld`."""
        return build_jsonld(cls._site_meta())

    # The original in-class ``_head`` / ``_jsonld`` implementations
    # (~130 lines of head meta + CSP assembly + JSON-LD payload)
    # moved to :mod:`investing.webpage.head`. The classmethods
    # above are thin delegators so the historical
    # ``Webpage._head()`` / ``Webpage._jsonld()`` call surface used
    # by the test suite still works.

    # ----------------------------------------------------- OG image

    # Search order for sans-serif fonts. Picks the first installed
    # candidate; falls back to Pillow's bitmap default if none exist
    # (still readable, just less crisp).
    _FONT_CANDIDATES = {
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

    @classmethod
    def _load_font(cls, weight: str, size: int):
        """Pick the first installed candidate font for the requested
        weight/size and fall back to Pillow's bitmap default."""
        from PIL import ImageFont
        for path, idx in cls._FONT_CANDIDATES.get(weight, ()):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size, index=idx)
                except Exception:
                    continue
        return ImageFont.load_default()

    def _render_og_image(self) -> None:
        """Render a 1200x630 PNG with the headline numbers for sharing.

        The image is what platforms like LinkedIn, Slack, Discord, X,
        and Facebook display when the URL is pasted into a chat or
        feed. The composition is tuned for that single-glance context:
        a prominent ``Jan Grzybek`` byline, the headline outperformance
        vs the S&P 500 on CAGR (the metric that matters once a long
        enough track record exists), and a strip of the top-10 equity
        holdings' logos so the preview hints at *what* is in the
        portfolio without needing a click. Failures (Pillow missing,
        unwritable disk, etc.) are swallowed - the page still renders
        fine without a regenerated OG image."""
        if self._total_return is None:
            return
        try:
            from PIL import Image, ImageDraw  # noqa: F401  (used below)
        except ImportError:
            return
        try:
            self._render_og_image_unsafe(self._total_return, self._benchmarks or [])
        except Exception:
            # Best-effort: never fail the whole page build because the
            # OG image couldn't be drawn (e.g. on a system with no
            # truetype fonts at all). The static fallback referenced
            # by ``SOCIAL_IMAGE`` will keep working until the next
            # successful regeneration.
            return

    def _render_og_image_unsafe(self, total_return, benchmarks) -> None:
        from PIL import Image, ImageDraw

        W, H = 1200, 630
        # Transparent canvas: we draw on RGBA with a fully-clear
        # background so the same OG image looks correct whether a
        # social platform places it on a light, dark, or branded
        # surface. Readability on both extremes is preserved by:
        #   - a tiny crisp white stroke around dark text (invisible
        #     against white -- white-on-white blends away -- and
        #     just enough of a bright edge to lift dark glyphs off
        #     dark backgrounds without the fat outlined-bubble
        #     letter look that wider strokes produce);
        #   - a semi-transparent white pill with its own soft outer
        #     glow behind the holdings logo strip (most logos are
        #     dark wordmarks that would otherwise vanish on dark
        #     backgrounds).
        TRANSPARENT = (255, 255, 255, 0)
        HALO = (255, 255, 255)
        FG = (17, 17, 17)
        MUTED = (95, 99, 106)
        ACCENT = (230, 125, 34)
        POS = (31, 122, 61)
        NEG = (179, 38, 30)

        # Tiny stroke width (in px) for the dark-mode readability
        # outline around the byline. Sized down hard from the
        # original 5px so the stroke reads as a crisp edge, not a
        # chunky outlined glyph; PIL renders ``stroke_width`` as
        # opaque pixels, so the stroke disappears completely on
        # white backgrounds regardless of width. The caption,
        # footer, and hero number are drawn without a stroke
        # because:
        #   - the caption (32pt) and footer (22pt) are small
        #     enough that PIL's minimum 1px stroke is
        #     proportionally chunky and renders the glyphs as
        #     puffy outlined letters rather than crisp text;
        #     dropping the stroke keeps them razor-sharp on white
        #     and trades a little dark-mode contrast for the
        #     sharpness;
        #   - the hero number's saturated accent green / red
        #     already carries enough contrast on both modes, and
        #     a stroke around a vivid 140pt numeral muddies the
        #     colour rather than helping read it.
        STROKE_BIG = 2    # 96pt display type ("Jan Grzybek")

        bench = benchmarks[0] if benchmarks else None
        cagr = float(total_return.get("cagr%", 0.0))
        bench_cagr = float(bench["cagr%"]) if bench else None
        cagr_delta = (cagr - bench_cagr) if bench_cagr is not None else None
        bench_label = self._benchmark_label(bench) if bench else None
        history = list(total_return.get("history") or [])
        start_date = (
            total_return.get("start_date")
            or (history[0][0] if history else self._now())
        )
        duration = _format_duration(relativedelta(self._now(), start_date))

        f_name = self._load_font("bold", 96)
        f_hero = self._load_font("bold", 140)
        f_caption = self._load_font("regular", 32)
        f_caption_b = self._load_font("bold", 32)
        f_foot = self._load_font("regular", 22)

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
        # the rest of the layout. Mid-luminance orange is legible on
        # both light and dark backgrounds without a halo.
        draw.rectangle((pad_l, 168, pad_l + 96, 176), fill=ACCENT)

        # Hero: outperformance vs the benchmark on CAGR. CAGR is the
        # metric that compares portfolios fairly across periods, so
        # it earns the headline slot. When no benchmark is available
        # yet we fall back to the portfolio's own CAGR so the image
        # still has a meaningful headline.
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

        # Caption below the hero: "Outperformance of S&P 500 on CAGR"
        # with the benchmark name bolded so the reader's eye lands on
        # the comparison subject. Rendered without a stroke so the
        # 32pt glyphs stay sharp on both modes; the bold benchmark
        # name carries the same MUTED fill as the surrounding text
        # so the emphasis lives in the weight alone -- a darker
        # fill on the emphasis would look great on white but vanish
        # on a dark background where MUTED is already at the dim
        # end of legible.
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

        # Logo strip: top-10 current holdings by weight. The strip
        # is the visual proof that the headline number is backed by
        # a real portfolio, and it's the only place on the image
        # that hints at *what* is held.
        self._draw_top_holdings_strip(
            img,
            x=pad_l,
            y=470,
            w=W - 2 * pad_l,
            h=90,
        )

        # Footer line: anchor period + URL for visual grounding.
        foot = (
            f"Since {_fmt_date(start_date)}  \u00b7  {duration}  \u00b7  "
            "jan-grzybek.github.io/investing"
        )
        draw.text((pad_l, H - 40), foot, font=f_foot, fill=MUTED)

        img.save("og-image.png", optimize=True)

    # Tickers in ``top_10`` keys that are not real holdings (e.g. the
    # synthetic "Other equities" bucket added when there are >11
    # current positions). Skipped when picking logos for the strip.
    _NON_TICKER_TOP10_KEYS = frozenset({"Other equities"})

    # ``holding_anchor`` and ``strip_exchange`` are imported from
    # :mod:`investing.webpage.anchors`; the static-method wrappers
    # below preserve the historical ``Webpage._holding_anchor`` /
    # ``Webpage._strip_exchange`` callsites used by the renderer and
    # the test suite.
    _holding_anchor = staticmethod(holding_anchor)

    def _top_holdings_for_og(self, limit: int = 10) -> list[str]:
        """Return up to ``limit`` ticker symbols for the OG logo strip.

        ``self.top_10`` is already sorted by weight (descending) and
        may contain a synthetic "Other equities" key when there are
        more than 11 current positions; we filter that out so only
        real tickers reach the logo loader."""
        if not self.top_10:
            return []
        tickers: list[str] = []
        for ticker in self.top_10:
            if ticker in self._NON_TICKER_TOP10_KEYS:
                continue
            tickers.append(ticker)
            if len(tickers) >= limit:
                break
        return tickers

    @staticmethod
    def _load_logo_for_og(ticker: str, max_w: int, max_h: int):
        """Load a ticker's logo as an RGBA ``PIL.Image`` fitted to a
        ``max_w x max_h`` box (preserving aspect ratio).

        Reads from the local ``logos/`` directory next to ``update.py``
        rather than going over HTTP, so the OG image is reproducible
        without a network round-trip and works the first time the
        site is deployed (before any logo is live behind
        ``LOGOS_ADDRESS``). SVG logos are rasterised with ``cairosvg``
        at 2x the target dimensions for crispness; raster logos
        (PNG/JPG) are loaded directly. Falls back to ``courage.png``
        when no per-ticker logo is on file, and returns ``None`` when
        even that fails so the caller can leave a gap rather than
        crash the whole image."""
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

            # Fit to the target box while preserving aspect ratio.
            scale = min(max_w / src.width, max_h / src.height)
            new_w = max(1, round(src.width * scale))
            new_h = max(1, round(src.height * scale))
            return src.resize((new_w, new_h), Image.LANCZOS)  # type: ignore[attr-defined]

        return None

    def _draw_top_holdings_strip(
        self, canvas, *, x: int, y: int, w: int, h: int,
    ) -> None:
        """Render up to 10 logos of the largest current holdings in a
        single horizontal row inside the ``(x, y, w, h)`` rectangle.

        Each logo is fitted into a same-width cell with consistent
        gaps so the strip reads as a uniform "what's inside" row
        regardless of the underlying logos' aspect ratios. A
        semi-transparent white pill sits behind the row so that the
        predominantly dark logo wordmarks (Adobe, Lam Research,
        Samsung, ...) stay legible when the OG image is composited
        on a dark surface. The pill itself is rendered with a soft
        outer halo (a Gaussian-blurred copy of the same shape)
        composited underneath so the strip feels lifted off the
        background rather than stamped on top of it; on white
        backgrounds the halo blends in and is invisible. The strip
        is left untouched when there are no current holdings yet
        (e.g. on the very first build) so the rest of the layout
        still reads cleanly."""
        from PIL import Image, ImageDraw, ImageFilter

        tickers = self._top_holdings_for_og(limit=10)
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

        # Card backdrop. Semi-transparent white pill (alpha 225
        # gives a slight "frosted glass" softness on dark
        # backgrounds while keeping dark logo wordmarks high
        # contrast) wrapped in a tight Gaussian-blurred outer halo
        # of the same shape, lifting the card visually off dark
        # backgrounds; on white pages both layers blend into the
        # page so the strip reads as plain logos. The blur radius
        # is intentionally tight (~6px) so the halo's bottom edge
        # stays well clear of the footer text below the card --
        # a wider glow looks pretty in isolation but its faint
        # white wash crosses into the footer and dims the contrast
        # of the small muted glyphs there. The padding values are
        # tuned so the pill hugs the row of logos with a bit of
        # breathing room on all sides.
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
        # glow with grey on white backgrounds. Only the alpha
        # channel needs to spread; RGB stays pure white throughout.
        card_layer = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
        ImageDraw.Draw(card_layer).rounded_rectangle(
            card_rect, radius=24, fill=(255, 255, 255, 225),
        )
        glow_layer = card_layer.filter(ImageFilter.GaussianBlur(radius=6))
        canvas.alpha_composite(glow_layer)
        canvas.alpha_composite(card_layer)

        for idx, ticker in enumerate(tickers):
            cell_x = offset_x + idx * (cell_w + gap)
            logo = self._load_logo_for_og(ticker, cell_w, cell_h)
            if logo is None:
                continue
            # Center the logo within its cell -- horizontally because
            # narrow logos otherwise hug the left edge, and
            # vertically so wide logos line up on a consistent
            # midline with square ones.
            paste_x = cell_x + (cell_w - logo.width) // 2
            paste_y = y + (cell_h - logo.height) // 2
            canvas.paste(logo, (paste_x, paste_y), logo)

    @staticmethod
    def _footer(update_date: str, update_iso: str) -> str:
        # Two ``<h2>`` headings break the footer into a "Methodology"
        # block (the bullets covering base currency, tax/cost
        # assumptions, and the data source) and a "Disclaimer" block
        # (informational-purposes-only notice plus the
        # logos/analytics legal note). The headings are intentionally
        # *not* added to the in-page nav: the nav lists portfolio
        # sections, and the footer remains a tail-of-page reference
        # that doesn't need a nav target. Heading level matches
        # ``.section__title`` (h2) inside ``<main>`` so the document
        # outline stays linear -- ``<footer>`` is its own landmark
        # at the same depth as a top-level section.
        return (
            '<footer>\n'
            '<h2 class="footer__title">Methodology</h2>\n'
            '<ul class="footer__notes">\n'
            '<li>All performance metrics on this page (TSR, TWR, CAGR) were '
            'calculated using USD as the base currency.</li>\n'
            '<li>TSR figures were calculated using the modified Dietz method, '
            'with dividends assumed to be subject to a 15% withholding tax '
            'and cashed out.</li>\n'
            '<li>The portfolio-level time-weighted return (TWR) was calculated '
            'excluding the impact of capital gains taxes, but including the '
            'effects of withholding taxes and transaction costs.</li>\n'
            '<li>The latest stock prices and dividend data used in the '
            'calculations were obtained from '
            '<a href="https://finance.yahoo.com/markets/stocks/trending/" '
            'title="Yahoo Finance" rel="noopener noreferrer">'
            'Yahoo Finance</a>.</li>\n'
            '</ul>\n'
            '<h2 class="footer__title">Disclaimer</h2>\n'
            '<p class="footer__disclaimer">For informational purposes only. '
            'Nothing contained herein should be construed as a recommendation '
            'to buy, sell or hold any security or pursue any investment '
            'strategy.</p>\n'
            '<p class="footer__legal">Logos are trademarks of their respective '
            'owners and are used for identification purposes only. This webpage '
            'uses Cloudflare Web Analytics to measure anonymous traffic '
            'statistics. No cookies or tracking identifiers are used.</p>\n'
            f'<p class="footer__updated">Updated on '
            f'<time datetime="{update_iso}">{update_date}</time></p>\n'
            '</footer>'
        )

    def _get_logo_url(self, ticker):
        """Resolve a holding logo URL via :class:`investing.logos.LogoCache`.

        Delegates to the resolver wired in ``__init__`` so the HTTP
        plumbing (session reuse, retry, timeout, negative cache)
        lives in one place and tests can swap in a stub via the
        ``logo_cache=`` constructor parameter.
        """
        return self._logo_resolver(ticker)

    # ---- per-section builders ------------------------------------------

    def _build_return_section(self, total_return, benchmarks) -> str:
        lines: list[str] = []
        # Short orientation paragraph so a first-time reader knows
        # what the chart + comparison capsules below it represent
        # before they get to the numbers. Phrased so the wording
        # still reads naturally when the chart is omitted (single-
        # point histories fall back to the comparison block alone)
        # and when no benchmark is configured (capsules render the
        # portfolio column on its own). Acronym expansions live in
        # the prose itself rather than in a separate legend so the
        # explanation degrades to plain text when CSS is stripped.
        lines.append(self._build_return_intro(benchmarks))
        # Chart leads the section as the headline visual; its caption
        # carries the start date so the comparison block below can stay
        # focused on the head-to-head numbers without restating the
        # period. When there's no chart (single-point history) we move
        # the "Since {start}" header into the comparison block instead.
        chart = self._render_return_chart(total_return, benchmarks)
        if chart:
            lines.append(chart)
        lines.append(self._build_returns_comparison(
            total_return, benchmarks, include_period=not chart,
        ))
        return "\n".join(lines)

    def _build_return_intro(self, benchmarks) -> str:
        """Render the section__intro paragraph above the chart.

        Adapts to whether a benchmark is configured: the comparison
        block only renders a benchmark column when one is present, so
        the intro phrasing follows suit (no dangling "vs the S&P 500"
        reference when there's nothing to compare against). The
        per-acronym legend that used to follow is intentionally
        omitted -- the capsule labels (TWR / TSR / CAGR) are next to
        their values and the footer "Methodology" block carries the
        deeper definitions, so a one-liner is enough to orient a
        first-time reader without restating what the layout already
        shows."""
        if benchmarks:
            bench = html.escape(self._benchmark_label(benchmarks[0]))
            body = (
                f"Cumulative return of the portfolio tracked against "
                f"the {bench}."
            )
        else:
            body = "Cumulative return of the portfolio."
        return f'<p class="section__intro">{body}</p>'

    def _build_returns_comparison(
        self, total_return, benchmarks, *, include_period: bool,
    ) -> str:
        """Render JG vs benchmark side-by-side with shared metrics.

        When ``include_period`` is true a "Since {start}" header is
        prepended to the block; otherwise the chart caption already
        carries that information so we omit it here to avoid repeating
        the period (and its length) twice.

        A delta line at the bottom summarises the outperformance (or
        underperformance) in percentage points so the comparison reads
        head-to-head over the same measurement window."""
        period_html = ""
        if include_period:
            start_date = total_return["start_date"]
            duration = _format_duration(relativedelta(self._now(), start_date))
            # Long-form date here -- the caption reads as prose
            # ("Since Jan 1, 2024 . 2 years, 1 month"), not as a
            # tabular slot, so the slash-separated DD/MM/YYYY
            # format used everywhere else on the page would break
            # the sentence rhythm.
            period_html = (
                '<p class="returns-compare__period">'
                f'Since <time datetime="{start_date.strftime("%Y-%m-%d")}">'
                f'{_fmt_date_long(start_date)}</time> &middot; '
                f'{html.escape(duration)}'
                '</p>'
            )

        cols: list[str] = [self._render_compare_col(
            name="JG",
            subtitle="Jan Grzybek",
            logo_url=COURAGE_LOGO,
            rows=[
                ("TWR", total_return["twr%"]),
                ("CAGR", total_return["cagr%"]),
            ],
        )]
        for benchmark in benchmarks or []:
            cols.append(self._render_compare_col(
                name=self._benchmark_label(benchmark),
                subtitle=benchmark.get("ticker") or "",
                logo_url=self._get_logo_url(benchmark["ticker"]),
                rows=[
                    ("TSR", benchmark["tsr%"]),
                    ("CAGR", benchmark["cagr%"]),
                ],
            ))

        delta_html = ""
        if benchmarks:
            b = benchmarks[0]
            twr_delta = total_return["twr%"] - b["tsr%"]
            cagr_delta = total_return["cagr%"] - b["cagr%"]
            # Each piece is its own span with ``white-space: nowrap``
            # so a narrow viewport never breaks "+6.7 pp Total Return"
            # or "+1.3 pp CAGR" mid-phrase. The container is a flex
            # row that wraps under pressure; at <=540px each piece
            # gets ``flex: 1 0 100%`` and stacks vertically.
            # The TWR vs benchmark TSR delta is labelled "Total
            # Return" here -- the capsule columns above already give
            # the precise per-side metric ("TWR" for JG, "TSR" for
            # the benchmark), so this summary line just states what's
            # being compared. Title-cased to sit visually parallel
            # with the ``CAGR`` token next to it, both reading as
            # data labels rather than prose.
            delta_html = (
                '<p class="returns-compare__delta">'
                '<span class="returns-compare__delta-prefix">JG vs '
                f'{html.escape(self._benchmark_label(b))}:</span>'
                f'<span class="returns-compare__delta-metric '
                f'{_value_class(twr_delta)}">'
                f'{_fmt_pct(twr_delta, signed=True)} pp Total Return</span>'
                '<span class="returns-compare__delta-sep" '
                'aria-hidden="true">&middot;</span>'
                f'<span class="returns-compare__delta-metric '
                f'{_value_class(cagr_delta)}">'
                f'{_fmt_pct(cagr_delta, signed=True)} pp CAGR</span>'
                '</p>'
            )

        return (
            '<section class="returns-compare">'
            f'{period_html}'
            f'<div class="returns-compare__grid">{"".join(cols)}</div>'
            f'{delta_html}'
            '</section>'
        )

    @staticmethod
    def _benchmark_label(benchmark) -> str:
        """Friendly display name for a benchmark, falling back gracefully."""
        ticker = benchmark.get("ticker", "")
        return (
            _BENCHMARK_DISPLAY_NAMES.get(ticker)
            or benchmark.get("name")
            or ticker
            or "Benchmark"
        )

    @staticmethod
    def _render_compare_col(*, name, subtitle, logo_url, rows) -> str:
        stat_html = []
        for label, value in rows:
            # ``value`` is the unrounded percentage straight off
            # ``total_return`` / benchmark dicts; ``_fmt_pct`` decides
            # at format time whether to render one decimal (<100%)
            # or whole-number (>=100%, where the decimal is just
            # noise next to a 3-digit integer part).
            stat_html.append(
                f'<dt>{html.escape(label)}</dt>'
                f'<dd class="{_value_class(value)}">{_fmt_pct(value)}%</dd>'
            )
        sub_html = ""
        if subtitle:
            sub_html = (
                f'<small class="returns-compare__name-sub">'
                f'{html.escape(subtitle)}</small>'
            )
        # ``h3`` keeps the heading tree contiguous: the parent section
        # is at h2, so jumping to h4 here would skip a level (a WCAG
        # and SEO smell).
        return (
            '<article class="returns-compare__col">'
            '<h3 class="returns-compare__name">'
            f'<img class="returns-compare__logo" src="{html.escape(logo_url)}" '
            'alt="" decoding="async" width="48" height="48">'
            f'<span class="returns-compare__name-text">'
            f'{html.escape(name)}{sub_html}</span>'
            '</h3>'
            f'<dl class="returns-compare__stats">{"".join(stat_html)}</dl>'
            '</article>'
        )

    # Numeric sort indices for the Action and Details columns. The
    # buy-vs-sell axis is binary (action == 0 for BUY, 1 for SELL),
    # so sorting ascending groups Bought rows above Sold rows. The
    # finer-grained "Details" sort uses the dict-order index --
    # OPEN -> INCREASE -> DECREASE -> CLOSE, ascending -- so an
    # ascending sweep flows through the position's lifecycle:
    # initial stake, then top-ups, then trims, then disposal. Both
    # tables are derived rather than written out by hand so
    # ``_TRADE_ACTION_DISPLAY`` / ``_TRADE_DETAIL_LABELS`` stay the
    # single source of truth for the four-category space.
    _TRADE_DETAIL_SORT_INDEX: dict[str, int] = {
        # Canonical category order -- matches the dict literals at
        # the top of this module.
        category: index
        for index, category in enumerate(
            ("OPEN", "INCREASE", "DECREASE", "CLOSE")
        )
    }
    _TRADE_ACTION_SORT_INDEX: dict[str, int] = {
        category: 0 if category in _BUY_CATEGORIES else 1
        for category in _TRADE_ACTION_DISPLAY
    }

    # ``_strip_exchange`` trims the ``EXCHANGE:`` prefix off a
    # ticker -- the "Trades" table renders tickers without the prefix
    # because the page already groups by security and the exchange
    # is redundant noise for a reader scanning for a familiar symbol
    # like ``AAPL``. The implementation lives in
    # :mod:`investing.webpage.anchors`; the static-method wrapper
    # below preserves the historical ``Webpage._strip_exchange``
    # callsites used by the renderer and the test suite.
    _strip_exchange = staticmethod(strip_exchange)

    @classmethod
    def _trade_detail_text(cls, event) -> str:
        """Human-facing text for the "Details" column.

        OPEN / CLOSE return the static lifecycle labels (the
        position came into existence / was disposed of, respectively).
        INCREASE / DECREASE return a signed whole-number percentage
        of the burst's magnitude relative to the prior position --
        ``+30%`` reads as "this BUY grew the existing stake by 30%",
        ``-25%`` as "this SELL trimmed it by 25%". The minus glyph
        is the typographically correct ``\u2212`` (U+2212), not the
        ASCII hyphen-minus: it lines up to the same width as ``+``
        in tabular-numbers fonts so the column edges stay flush.
        A defensive fallback returns the bare ``Bought`` / ``Sold``
        label if ``delta_pct`` is missing for an INCREASE / DECREASE
        burst (which the production data path never produces, but
        guards us against malformed test fixtures).
        """
        category = event["category"]
        if category in cls._TRADE_DETAIL_LABELS_REF:
            return cls._TRADE_DETAIL_LABELS_REF[category]
        delta_pct = event.get("delta_pct")
        if delta_pct is None:
            return _TRADE_ACTION_DISPLAY[category][0]
        sign = "+" if category == "INCREASE" else "\u2212"
        return f"{sign}{delta_pct:.0f}%"

    # Module-level table is referenced through a class attribute so
    # the renderer code reads top-down with no ``global`` jumps;
    # this also gives subclasses a single place to override the
    # mapping if a future variation of the page wants different
    # boundary labels.
    _TRADE_DETAIL_LABELS_REF: dict[str, str] = _TRADE_DETAIL_LABELS

    def _build_trade_row(self, event) -> str:
        """Render one burst-aggregated trade as a ``<tr>`` for the
        sortable trades table.

        Five columns: ticker (without exchange prefix), company
        name, action badge (Bought / Sold), details (initial stake
        / signed percentage / disposal), date / range, per-share
        price. ``data-sort-*`` attributes carry the sort key for
        each sortable column so the inline ``_TRADES_SORT_SCRIPT``
        can re-order rows without re-parsing cell text. The
        per-share price stays in the security's native currency
        (e.g. ``EUR 76.32``); we use the ISO code rather than the
        symbol because a leading ``$`` would silently misrepresent
        a EUR / GBp trade as USD in a multi-market portfolio.
        Nominal share counts are deliberately absent: the page
        commits to publishing only relative percentages and
        per-share prices, never sizes.
        """
        category = event["category"]
        action_label, action_modifier = _TRADE_ACTION_DISPLAY[category]
        detail_label = self._trade_detail_text(event)
        # The two "boundary" labels (Initial stake / Disposal) are
        # qualitative; the magnitude rows (+30% / -25%) are
        # quantitative and benefit from a tabular-numbers treatment
        # plus a sign-driven colour cue. The CSS hooks both off this
        # modifier so the cell either renders muted-grey text or
        # green / red value-style text depending on context.
        if category in ("INCREASE", "DECREASE"):
            detail_modifier = "pct"
            detail_class = (
                "trades__detail trades__detail--pct "
                + ("value--positive" if category == "INCREASE"
                   else "value--negative")
            )
        else:
            detail_modifier = "label"
            detail_class = "trades__detail trades__detail--label"
        start = event["start_date"]
        end = event["end_date"]
        # Quarter-granularity timing -- see ``_fmt_quarter_range``
        # for the layout rules. The trades table commits to
        # publishing trade timing at quarter precision rather than
        # to-the-day; the page already speaks the long-term-
        # investor / fund-letter idiom, where the quarter is the
        # natural cadence and a to-the-day stamp would be
        # incidental precision the reader can't act on. The
        # row-level ``data-sort-date`` still carries the burst's
        # ISO end date below, so sorting by date stays fine-
        # grained even though the visible label is coarse.
        period_html = _fmt_quarter_range(start, end)
        # Thousands separator + 2 decimals reads well across the full
        # range of equity prices we ingest (sub-dollar US tickers up
        # through GBp pence quotes in the thousands). The ISO
        # currency code follows the value (``921.40 USD`` rather
        # than ``USD 921.40``) so the eye lands on the magnitude
        # first and the currency is read as a unit, the way every
        # other quantity on the page does it. Using the ISO code
        # rather than the symbol is still important: a leading
        # ``$`` would silently misrepresent a EUR or GBp trade as
        # USD in a multi-market portfolio.
        price_html = html.escape(
            f"{event['price']:,.2f} {event['currency']}"
        )
        symbol = self._strip_exchange(event["ticker"])
        name = event["name"]
        # Sort keys: dates are ISO so lexical compare = chronological;
        # the ``end_date`` is the most-recent activity in the burst
        # so it's the natural "when did this trade happen?" anchor
        # for the date sort. Ticker / name keys are lower-cased so
        # the sort is case-insensitive (avoids the "Z before a"
        # surprise that ASCII compare would otherwise produce).
        # Action / detail use dict-order indices so an ascending
        # sweep clusters BUYs before SELLs / opens before closes.
        sort_date = end.strftime("%Y-%m-%d")
        sort_ticker = symbol.lower()
        sort_name = name.lower()
        sort_action = self._TRADE_ACTION_SORT_INDEX[category]
        sort_detail = self._TRADE_DETAIL_SORT_INDEX[category]
        return (
            '<tr class="trades__row"'
            f' data-sort-date="{sort_date}"'
            f' data-sort-ticker="{html.escape(sort_ticker)}"'
            f' data-sort-name="{html.escape(sort_name)}"'
            f' data-sort-action="{sort_action}"'
            f' data-sort-detail="{sort_detail}">'
            f'<td class="trades__cell trades__cell--ticker">{html.escape(symbol)}</td>'
            f'<td class="trades__cell trades__cell--name">{html.escape(name)}</td>'
            '<td class="trades__cell trades__cell--action">'
            f'<span class="trade__badge trade__badge--{action_modifier}">'
            f'{html.escape(action_label)}</span>'
            '</td>'
            '<td class="trades__cell trades__cell--detail">'
            f'<span class="{detail_class}" '
            f'data-detail-kind="{detail_modifier}">'
            f'{html.escape(detail_label)}</span>'
            '</td>'
            f'<td class="trades__cell trades__cell--date">{period_html}</td>'
            f'<td class="trades__cell trades__cell--price">{price_html}</td>'
            '</tr>'
        )

    # Headers for the sortable columns of the trades table. ``key`` is
    # the ``data-sort-key`` consumed by ``_TRADES_SORT_SCRIPT`` and
    # matched against the ``data-sort-*`` attributes on each row;
    # ``label`` is the displayed text. The non-sortable price column
    # is emitted separately so this tuple captures exactly the keys
    # the JS module knows how to handle.
    _TRADES_SORTABLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("ticker", "Ticker",  "trades__col--ticker"),
        ("name",   "Company", "trades__col--name"),
        ("action", "Action",  "trades__col--action"),
        ("detail", "Details", "trades__col--detail"),
        ("date",   "Date",    "trades__col--date"),
    )

    # How many rows the trades table shows by default before the
    # rest are tucked behind the "Show all" toggle. Kept conservative
    # so a glance at the section reads as "recent activity" rather
    # than "every trade ever"; the full log is one click away. The
    # same constant lives in the CSS rule that hides overflow rows
    # (``:nth-of-type(n+11)``); the two must stay in sync.
    _TRADES_VISIBLE_DEFAULT: int = 10

    @classmethod
    def _build_trades_table(cls, rows: list[str]) -> str:
        """Wrap pre-rendered ``<tr>`` fragments in the sortable
        ``<table>`` and add the "Show all" toggle when the log is
        longer than the default visible window.

        The header row exposes click-to-sort buttons on the ticker /
        company / action / details / date columns. The default sort
        (the order the rows are emitted in) is by date descending so
        the most recent activity sits at the top before the user
        touches anything; ``_TRADES_SORT_SCRIPT`` reads
        ``data-sort-default`` / ``data-sort-default-dir`` and marks
        the matching header with ``aria-sort`` + the indicator
        triangle on load. ``data-sort-key`` lives on the ``<th>``
        (not the inner button) so the script can update ``aria-sort``
        and the indicator without walking back up the DOM.

        Once ``len(rows) > _TRADES_VISIBLE_DEFAULT`` the renderer
        also emits a ``.trades__toggle`` button after the table.
        CSS hides every ``<tr>`` past the threshold (``:nth-of-type``
        in DOM order, so the cutoff naturally follows whatever sort
        the user has applied); the inline sort script also wires the
        button up so a click toggles the table's ``data-expanded``
        attribute and updates the button's ``aria-expanded`` /
        text label.
        """
        headers: list[str] = []
        for key, label, modifier in cls._TRADES_SORTABLE_COLUMNS:
            headers.append(
                f'<th class="trades__col {modifier}" scope="col" '
                f'data-sort-key="{key}" aria-sort="none">'
                # The ``<button>`` is what receives focus / clicks --
                # a real button gets keyboard activation (Enter / Space)
                # and focus styles for free. The trailing
                # ``aria-hidden`` indicator span carries no semantic
                # value; the live ``aria-sort`` on the ``<th>`` is
                # what assistive tech announces.
                f'<button type="button" class="trades__sort">'
                f'{html.escape(label)}'
                '<span class="trades__sort-indicator" aria-hidden="true"></span>'
                '</button></th>'
            )
        # Price column is not sortable -- mixing currencies in a
        # numeric sort would imply a meaningful ordering across
        # USD / EUR / GBp etc. that doesn't exist without an FX
        # conversion, and surfacing that machinery in a personal
        # trade log would obscure the simpler intent of the column.
        headers.append(
            '<th class="trades__col trades__col--price" scope="col">Price</th>'
        )
        thead = f'<thead><tr>{"".join(headers)}</tr></thead>'
        tbody = f'<tbody>{"".join(rows)}</tbody>'
        # ``.trades__wrap`` is a horizontal-scroll fallback for very
        # narrow viewports where the six columns still don't fit
        # even after the mobile-specific column-hiding rules below;
        # the visible table itself sits inside it.
        table_html = (
            '<div class="trades__wrap">'
            '<table class="trades" '
            'data-sort-default="date" '
            'data-sort-default-dir="desc">'
            f'{thead}{tbody}'
            '</table>'
            '</div>'
        )
        toggle_html = ""
        total = len(rows)
        if total > cls._TRADES_VISIBLE_DEFAULT:
            # ``data-total`` is read by the inline script to compose
            # the "Show all N trades" label after each toggle. Keeping
            # the count in markup means the script never has to count
            # rows itself, which would otherwise have to filter the
            # currently-hidden ones out of ``querySelectorAll``.
            toggle_html = (
                '<button type="button" class="trades__toggle" '
                f'data-total="{total}" aria-expanded="false">'
                f'Show all {total} trades</button>'
            )
        return table_html + toggle_html

    # Sort options surfaced above each holdings list. ``key`` is the
    # ``data-holdings-sort-key`` consumed by ``_HOLDINGS_SORT_SCRIPT``
    # and matched against the ``data-sort-<key>`` attribute on each
    # ``<article class="holding">``; ``label`` is the displayed text;
    # ``kind`` controls the default direction the JS picks the first
    # time the user activates a column ("text" -> ascending, "number"
    # -> descending) so "Ticker" / "Name" jump straight to A->Z while
    # "TSR" / "CAGR" / "Weight" jump straight to high->low (the same
    # pattern ``_TRADES_SORT_SCRIPT`` already implements for the
    # trades table). The "default" key is special-cased: re-pressing
    # it restores the original DOM order without consuming a sort
    # direction at all -- that's the most-recent-trade-first
    # ordering that ``get_holdings`` produces upstream. The Weight
    # column is current-only (historical rows have no
    # ``current_weight%``); the historical button group filters
    # ``"weight"`` out before rendering.
    _HOLDINGS_SORT_OPTIONS: tuple[tuple[str, str, str], ...] = (
        ("default", "Default", "default"),
        ("ticker",  "Ticker",  "text"),
        ("name",    "Name",    "text"),
        ("tsr",     "TSR",     "number"),
        ("cagr",    "CAGR",    "number"),
        ("weight",  "Weight",  "number"),
    )

    @classmethod
    def _build_holdings_sort_control(
        cls, *, scope: str, include_weight: bool,
    ) -> str:
        """Render the per-section "Sort by" toolbar above a
        holdings list.

        ``scope`` is the value the wrapping
        ``data-holdings-list="..."`` element carries on its inner
        list, used by ``_HOLDINGS_SORT_SCRIPT`` to wire each
        toolbar to its own list independently. ``include_weight``
        controls whether the "Weight" button is rendered -- it is
        meaningless for historical holdings (no current weight)
        so the historical toolbar omits it.

        The "Default" button is rendered as the active option on
        first paint to mirror the order ``get_holdings`` already
        emits (most recent buy / most recent sell first); the
        inline script honours that initial state via the
        ``aria-pressed="true"`` attribute and only overwrites it
        once the user clicks a different button.
        """
        buttons: list[str] = []
        for key, label, kind in cls._HOLDINGS_SORT_OPTIONS:
            if key == "weight" and not include_weight:
                continue
            is_default = key == "default"
            indicator_html = (
                ""
                if is_default
                else '<span class="holdings__sort-indicator" '
                     'aria-hidden="true"></span>'
            )
            buttons.append(
                f'<button type="button" class="holdings__sort-btn" '
                f'data-holdings-sort-key="{key}" '
                f'data-holdings-sort-kind="{kind}" '
                f'aria-pressed="{"true" if is_default else "false"}" '
                f'aria-sort="none">'
                f'{html.escape(label)}{indicator_html}'
                '</button>'
            )
        scope_label = "current" if scope == "current" else "historical"
        return (
            f'<div class="holdings__sort" role="group" '
            f'aria-label="Sort {scope_label} holdings" '
            f'data-holdings-sort="{html.escape(scope)}">'
            '<span class="holdings__sort-label" aria-hidden="true">'
            'Sort by'
            '</span>'
            f'{"".join(buttons)}'
            '</div>'
        )

    def _build_holding_card(self, holding) -> str:
        stats: list[tuple[str, str, float | None]] = [
            # ``tsr%``/``cagr%``/``current_weight%`` are unrounded
            # floats on the data dict; ``_fmt_pct`` chooses one
            # decimal under 100 and whole-number from 100 up so a
            # 3-digit TSR (e.g. NVDA at +217%) doesn't carry a
            # noisy ``.4`` next to it. The raw float still flows
            # to ``_value_class`` for sign-based colouring.
            ("TSR:", f"{_fmt_pct(holding['tsr%'])}%", holding["tsr%"]),
        ]
        if holding["cagr%"] > CAGR_TBA_THRESHOLD:
            stats.append(("CAGR:", "TBA", None))
        else:
            stats.append(("CAGR:", f"{_fmt_pct(holding['cagr%'])}%", holding["cagr%"]))
        if holding["is_current"]:
            weight = holding["current_weight%"]
            if weight is None:
                raise InvariantError(
                    f"current holding {holding['ticker']!r} reached the "
                    "renderer with no weight -- summarize() did not run",
                )
            stats.append(("Weight:", f"{_fmt_pct(weight)}%", None))

        periods = [(p["start"], p["end"]) for p in holding["periods"]]

        # ``data-sort-*`` attributes feed the inline sort control
        # above each holdings list. The ticker key drops the
        # exchange prefix so ordering by "Ticker" reads as an
        # alphabetical run of company symbols (NVDA before SPGI),
        # which is the natural mental model for a reader who
        # thinks of "AAPL" / "GOOGL" as the canonical ticker --
        # the displayed title still carries the ``EXCHANGE:SYMBOL``
        # form so the row is unambiguous. Tickers and names are
        # case-folded so the sort stays stable across mixed-case
        # spellings. CAGR rows that overflow the TBA threshold
        # still emit the raw numeric value -- they sink to one
        # extreme of the ordering, but the row stays present
        # under either direction. ``data-sort-weight`` is omitted
        # on historical rows whose weight is ``None`` so the
        # button group can hide the Weight option for the
        # historical list while still rendering it for current
        # holdings.
        ticker_key = holding["ticker"].rsplit(":", 1)[-1].casefold()
        sort_attrs: dict[str, str] = {
            "sort-ticker": ticker_key,
            "sort-name": holding["name"].casefold(),
            "sort-tsr": _format_sort_number(holding["tsr%"]),
            "sort-cagr": _format_sort_number(holding["cagr%"]),
        }
        if holding["is_current"]:
            sort_attrs["sort-weight"] = _format_sort_number(
                holding["current_weight%"]
            )

        return self._build_card(
            logo_url=self._get_logo_url(holding["ticker"]),
            title=f'{holding["ticker"]} - {holding["name"]}',
            stats=stats,
            periods=periods,
            card_id=self._holding_anchor(holding["ticker"]),
            data_attrs=sort_attrs,
        )

    @staticmethod
    def _build_card(
        *,
        logo_url,
        title,
        stats,
        periods=None,
        note: str | None = None,
        card_id: str | None = None,
        data_attrs: dict[str, str] | None = None,
    ) -> str:
        """Render a capsule with logo, title/period(s)/note, and right-aligned stats.

        ``data_attrs`` is an optional mapping of ``data-*`` attribute
        names (without the ``data-`` prefix) to string values that
        will be emitted on the outer ``<article>``. Used by the
        holdings sort control to read per-card sort keys (ticker /
        name / TSR / CAGR / weight) without having to re-parse the
        rendered card body."""
        body_parts = [f'<h3 class="holding__title">{html.escape(title)}</h3>']
        if periods:
            # Always render the most-recent period first so it sits at
            # the top of the visual stack -- that's what readers scan
            # first. We sort here defensively rather than trusting the
            # caller: ``Holding.summary`` already returns newest-first
            # in production, but the preview/synthetic data and any
            # future call sites might not, and the visual order is a
            # UX guarantee, not an upstream invariant. Sorting by
            # ``start`` descending puts the period with the latest
            # opening date on top; periods don't overlap, so this also
            # implicitly sorts by ``end`` descending.
            ordered = sorted(periods, key=lambda p: p[0], reverse=True)
            items = []
            for start, end in ordered:
                # Each <li> emits three children -- start <time>, the
                # dash separator, and the end (either a <time> or a
                # plain <span> for "Present"). Combined with the
                # ``display: contents`` rule on .holding__periods li,
                # those three pieces become grid items in the parent
                # <ul>'s 3-column grid, so the dash and end-date
                # column line up vertically across multiple periods
                # even when day numbers have different digit counts.
                start_html = (
                    f'<time datetime="{start.strftime("%Y-%m-%d")}">'
                    f'{_fmt_date(start)}</time>'
                )
                if end is None:
                    end_html = '<span>Present</span>'
                else:
                    end_html = (
                        f'<time datetime="{end.strftime("%Y-%m-%d")}">'
                        f'{_fmt_date(end)}</time>'
                    )
                items.append(
                    f'<li>{start_html}<span>-</span>{end_html}</li>'
                )
            body_parts.append(
                f'<ul class="holding__periods">{"".join(items)}</ul>'
            )
        if note:
            body_parts.append(f'<p class="holding__note">{html.escape(note)}</p>')

        stat_parts = []
        for label, value, sign in stats:
            attr = ""
            if sign is not None:
                attr = f' class="{_value_class(sign)}"'
            # Each label-value pair gets its own ``<div>`` wrapper so
            # mobile CSS can treat the pair as a single flex item and
            # spread TSR/CAGR/Weight across the full row width with
            # ``justify-content: space-between`` (instead of clumping
            # them on the left and leaving an awkward gap on the
            # right). Desktop neutralises the wrapper with
            # ``display: contents`` so dt/dd still feed the parent's
            # 2-column grid as before. ``<div>`` is a valid grouping
            # element inside ``<dl>`` per HTML5.
            stat_parts.append(
                '<div class="holding__stat">'
                f'<dt>{html.escape(label)}</dt>'
                f'<dd{attr}>{html.escape(value)}</dd>'
                '</div>'
            )

        # Anchor ``id`` is what makes the marquee logo and equities
        # bar rows scroll to the right capsule -- both compute their
        # ``href`` from the same slug via ``_holding_anchor``.
        id_attr = f' id="{html.escape(card_id)}"' if card_id else ""
        data_attr_html = ""
        if data_attrs:
            # Emit attributes in a stable order so the rendered markup
            # is deterministic across calls; ``dict`` preserves insertion
            # order in modern Python but a ``sorted`` pass keeps the
            # output reproducible regardless of how the caller built
            # the mapping.
            for key in sorted(data_attrs):
                value = data_attrs[key]
                data_attr_html += f' data-{key}="{html.escape(value)}"'
        return (
            f'<article class="holding"{id_attr}{data_attr_html}>'
            # Below-the-fold logos load lazily; explicit dimensions
            # reserve space and keep CLS at zero.
            f'<img class="holding__logo" src="{html.escape(logo_url)}" '
            'alt="" loading="lazy" decoding="async" '
            'width="64" height="64">'
            f'<div class="holding__body">{"".join(body_parts)}</div>'
            f'<dl class="holding__stats">{"".join(stat_parts)}</dl>'
            '</article>'
        )

    # ---- chart / bar primitives (also covered directly by tests) -------

    @staticmethod
    def _render_bars(
        rows,
        variant: str,
        *,
        scale_to_max: bool = False,
        anchors: dict[str, str] | None = None,
    ) -> str:
        """Render a horizontal CSS bar chart.

        ``rows`` is an iterable of ``(label, value)`` pairs where ``value``
        is a percentage (0..100). Each row renders as
        ``label | value | bar`` so the percentages sit between the title
        and the bar. ``variant`` is the BEM modifier controlling the fill
        colour (e.g. ``"allocation"`` or ``"equities"``).

        With ``scale_to_max=True`` the widest bar fills its track and the
        rest are sized proportionally to the largest value. Useful when
        the rows do not sum to 100% (e.g. the top-N equities) and the
        viewer cares about relative weight rather than absolute share.

        ``anchors`` is an optional ``{label: anchor-id}`` map (anchor
        without the leading ``#``). When present for a row, that row
        is emitted as an ``<a>`` instead of a plain ``<div>`` so
        clicking it scrolls to the targeted section / capsule. Rows
        without an anchor entry (e.g. the synthetic "Other equities"
        bucket) keep their non-linked form.
        """
        if not rows:
            return ""
        rows = list(rows)
        denom = max((value for _, value in rows), default=0.0) if scale_to_max else 100.0
        if not denom:
            denom = 100.0

        row_html = []
        for label, value in rows:
            # ``value`` arrives unrounded (allocation% / weight%);
            # the bar's CSS width gets two decimals for sub-pixel
            # precision while the visible label uses ``_fmt_pct`` --
            # one decimal under 100, whole-number from 100 up.
            width = value / denom * 100 if scale_to_max else value
            inner = (
                f'<div class="bars__label">{html.escape(str(label))}</div>'
                f'<div class="bars__value">{_fmt_pct(value)}%</div>'
                f'<div class="bars__track"><div class="bars__fill" '
                f'style="width: {width:.2f}%"></div></div>'
            )
            anchor = anchors.get(label) if anchors else None
            if anchor:
                # ``bars__row--link`` opts the row into the underlined-
                # free, pointer-cursor styling and keeps the grid
                # layout (``<a>`` is treated as a grid container the
                # same way ``<div>`` is).
                row_html.append(
                    '<a class="bars__row bars__row--link" '
                    f'href="#{html.escape(anchor)}">{inner}</a>'
                )
            else:
                row_html.append(f'<div class="bars__row">{inner}</div>')
        return f'<div class="bars bars--{variant}">{"".join(row_html)}</div>'

    @classmethod
    def _render_return_chart(cls, total_return, benchmarks) -> str:
        """Render an inline SVG of the portfolio return curve.

        When a benchmark is present we reserve a slice on the right edge
        of the chart for an outperformance annotation: a vertical line
        connecting the JG and benchmark endpoints with a "+X.X pp" label
        showing the cumulative-return delta in percentage points.

        Returns an empty string when the history has fewer than two
        samples (since there is nothing to draw)."""
        history = total_return.get("history", [])
        if len(history) < 2:
            return ""

        # Collect series (JG + each benchmark) and the global y-range.
        start_date = history[0][0]
        time_x = np.array([int((d - start_date).days) for d, _ in history], dtype=float)
        jg_y = np.array([v for _, v in history], dtype=float)

        series: list[tuple[str, str, np.ndarray]] = [("jg", "JG", jg_y)]
        for benchmark in benchmarks or []:
            bh = benchmark.get("history", [])
            if len(bh) < 2:
                continue
            label = cls._benchmark_label(benchmark)
            series.append(("bench", label, np.array([v for _, v in bh], dtype=float)))

        min_y = min(float(s[2].min()) for s in series)
        max_y = max(float(s[2].max()) for s in series)
        # Add a little headroom so the curves don't sit on the frame.
        pad_y = max((max_y - min_y) * 0.05, 0.01)
        view_max = max_y + pad_y
        view_min = min_y - pad_y

        # Viewport (unitless; the CSS picks the rendered size).
        width = 1000.0
        height = 400.0
        # Reserve 12% on the right when we'll be drawing a delta
        # annotation so its bar+label don't overlap the curves.
        has_delta = (
            len(series) >= 2 and series[0][0] == "jg" and series[1][0] == "bench"
        )
        right_margin_pct = 12.0 if has_delta else 0.0
        chart_x_end = width * (1 - right_margin_pct / 100.0)

        def map_x(x_days: float) -> float:
            span = float(time_x.max() - time_x.min()) or 1.0
            return (x_days - float(time_x.min())) / span * chart_x_end

        def map_y(value: float) -> float:
            span = view_max - view_min or 1.0
            return height - (value - view_min) / span * height

        # Smooth interpolation when there are three or more points,
        # straight segments for two.
        if len(time_x) >= 3:
            dense = np.linspace(time_x.min(), time_x.max(), 200)
            interp_x = dense
            interp_targets = {id(s[2]): np.exp(Pchip(time_x, np.log(s[2]))(dense)) for s in series}
        else:
            interp_x = time_x
            interp_targets = {id(s[2]): s[2] for s in series}

        def to_points(ys: np.ndarray) -> str:
            return " ".join(
                f"{map_x(x):.2f},{map_y(y):.2f}"
                for x, y in zip(interp_x, ys, strict=False)
            )

        ref_y = map_y(1.0)
        svg_lines = [
            f'<svg viewBox="0 0 {int(width)} {int(height)}" xmlns="http://www.w3.org/2000/svg" '
            'preserveAspectRatio="none" role="img" aria-label="Portfolio return curve">',
            f'<line class="return-chart__ref" x1="0" y1="{ref_y:.2f}" x2="{chart_x_end:.2f}" y2="{ref_y:.2f}"/>',
        ]
        for kind, _label, ys in series:
            svg_lines.append(
                f'<polyline class="return-chart__line return-chart__line--{kind}" '
                f'points="{to_points(interp_targets[id(ys)])}"/>'
            )
        svg_lines.append('</svg>')

        # Outperformance overlay: a vertical bar between the two curve
        # endpoints with a percentage-point delta label. Built as
        # absolutely-positioned HTML (rather than SVG text or a single
        # bordered box) so the bar can stay glued to the chart-end
        # x-coordinate at every viewport while the label flows around
        # it - on wide screens to its right, on phones to its left
        # with a translucent backdrop. SVG text would also be
        # unreadably small on phones because of viewBox scaling.
        delta_html = ""
        if has_delta:
            jg_final = float(series[0][2][-1])
            bench_final = float(series[1][2][-1])
            # Prefer the canonical TWR (JG) - TSR (benchmark) delta
            # straight off ``total_return`` / ``benchmarks``: the JG
            # vs S&P 500 capsule directly below the chart shows the
            # exact same delta as ``+X.X pp Total Return``, and we
            # don't want the two numbers to drift apart. Modified
            # Dietz TWR/TSR are computed cashflow-aware over the
            # exact period, while the chart's curves are sampled at
            # discrete dates, so naively differencing the last-point
            # values can disagree with the canonical metric by
            # several tenths of a percentage point. Falling back to
            # the curve endpoints when those metrics aren't supplied
            # keeps the renderer usable from unit tests and any
            # future caller that only has a history.
            twr_pct = total_return.get("twr%")
            tsr_pct = benchmarks[0].get("tsr%") if benchmarks else None
            if twr_pct is not None and tsr_pct is not None:
                delta_pp = float(twr_pct) - float(tsr_pct)
            else:
                delta_pp = (jg_final - bench_final) * 100.0
            jg_y_pct = map_y(jg_final) / height * 100.0
            bench_y_pct = map_y(bench_final) / height * 100.0
            top_pct = min(jg_y_pct, bench_y_pct)
            height_pct = abs(jg_y_pct - bench_y_pct)
            # ``--delta-color`` is consumed by the caliper bracket
            # (vertical spine + top/bottom jaws) in ``_PAGE_STYLES``.
            # Encoding the sign here keeps the colour logic in one
            # place: the same green/red mapping that drives the label
            # also tints the bracket so its visual meaning is
            # self-evident.
            delta_color = (
                "var(--positive)" if delta_pp >= 0 else "var(--negative)"
            )
            delta_html = (
                '<div class="return-chart__delta" '
                f'style="--top: {top_pct:.2f}%; --height: {height_pct:.2f}%; '
                f'--delta-color: {delta_color};">'
                '<span class="return-chart__delta-bar"></span>'
                f'<span class="return-chart__delta-label {_value_class(delta_pp)}">'
                f'{_fmt_pct(delta_pp, signed=True)} pp</span>'
                '</div>'
            )

        # Legend (only when there is more than one series).
        legend_html = ""
        if len(series) > 1:
            chips = []
            for kind, label, _ in series:
                chips.append(
                    f'<span><span class="return-chart__swatch return-chart__swatch--{kind}" '
                    f'style="background: var(--{"accent" if kind == "jg" else "accent-bench"});"></span>'
                    f'{html.escape(label)}</span>'
                )
            legend_html = f'<div class="return-chart__legend">{"".join(chips)}</div>'

        # Caption: when this chart sits above the comparison block we
        # rely on it to anchor the period (the comparison block omits
        # its own period header in that case to avoid repetition).
        # The duration follows the start date so the reader sees both
        # the anchor point ("since when?") and the elapsed window
        # ("how long?") in one glance.
        duration = _format_duration(relativedelta(history[-1][0], start_date))
        # Long-form date here -- the caption reads as prose
        # ("Since Jan 1, 2024 . 2 years, 1 month"), not as a tabular
        # slot, so the slash-separated DD/MM/YYYY format used
        # everywhere else on the page would break the sentence
        # rhythm. The chart-less variant in ``returns-compare``
        # makes the same choice (see ``_render_returns_compare``).
        caption = (
            f'<div class="return-chart__caption">'
            f'Since <time datetime="{start_date.strftime("%Y-%m-%d")}">'
            f'{_fmt_date_long(start_date)}</time> &middot; '
            f'{html.escape(duration)}</div>'
        )

        # Hover overlay: empty containers the scrubber script fills
        # in on the fly. The guide line and tooltip stay invisible
        # until a pointer enters the plot (CSS toggles
        # ``.is-active``). Markers/rows are injected by the script
        # so the markup is identical for one- and two-series charts.
        # The hover-delta bar + tooltip-delta row only render when a
        # benchmark is present -- they're the moving counterpart of
        # the static right-edge caliper, showing the local
        # outperformance at the cursor's x-coordinate.
        hover_delta_bar_html = (
            '<div class="return-chart__hover-delta-bar"></div>'
            if has_delta else ''
        )
        tooltip_delta_html = (
            '<div class="return-chart__tooltip-delta"></div>'
            if has_delta else ''
        )
        # ``.return-chart__hover`` sits BEFORE ``.return-chart__delta``
        # in source order so a CSS sibling selector can dim the
        # static end-of-period delta while the scrubber is active
        # (``.return-chart__hover.is-active ~ .return-chart__delta``).
        # The hover overlay still paints on top thanks to its
        # explicit ``z-index`` in ``_PAGE_STYLES``.
        hover_html = (
            '<div class="return-chart__hover" aria-hidden="true">'
            '<div class="return-chart__guide"></div>'
            f'{hover_delta_bar_html}'
            '<div class="return-chart__tooltip">'
            '<div class="return-chart__tooltip-date"></div>'
            '<div class="return-chart__tooltip-rows"></div>'
            f'{tooltip_delta_html}'
            '</div>'
            '</div>'
        )

        # Pack the scrubber data into a JSON blob on the <figure>.
        # We embed the SAME densely-sampled curve the SVG polyline
        # draws (Pchip in log-space when there are >= 3 history
        # points, raw segments for two) so the marker dots track the
        # rendered line exactly -- linear-interpolating between
        # adjacent dense samples is visually indistinguishable from
        # the curve at that resolution. Values are rounded to six
        # decimals -- well past the chart's visual precision -- so
        # the inline payload stays compact.
        dense_x = [round(float(x), 2) for x in interp_x.tolist()]
        chart_data = {
            "start": start_date.strftime("%Y-%m-%d"),
            "totalDays": int(time_x[-1] - time_x[0]),
            "rightPct": right_margin_pct,
            "yMin": round(float(view_min), 6),
            "yMax": round(float(view_max), 6),
            "series": [
                {
                    "kind": kind,
                    "label": label,
                    "x": dense_x,
                    "y": [round(float(v), 6) for v in interp_targets[id(ys)].tolist()],
                }
                for kind, label, ys in series
            ],
        }
        chart_data_attr = html.escape(
            json.dumps(chart_data, separators=(",", ":")), quote=True
        )

        plot_html = (
            f'<div class="return-chart__plot">'
            f'{"".join(svg_lines)}{hover_html}{delta_html}'
            f'</div>'
        )
        return (
            f'<figure class="return-chart" data-chart="{chart_data_attr}">'
            f'{plot_html}{legend_html}{caption}</figure>'
        )




def generate_webpage(total_return, benchmarks, holdings):
    webpage = Webpage()
    webpage.add_return(total_return, benchmarks)
    webpage.add_allocations(holdings.get("allocation%"), holdings.get("top_10"))
    for holding in holdings["current"]:
        webpage.add_holding(holding)
    for holding in holdings["historical"]:
        webpage.add_holding(holding)
    webpage.add_trades(holdings.get("trades") or [])
    webpage.save()
