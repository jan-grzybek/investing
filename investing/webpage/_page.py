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
from datetime import datetime

from dateutil.relativedelta import relativedelta

from ..clock import NowFn
from ..formatting import (
    _fmt_date_long,
    _fmt_pct,
    _format_duration,
    _value_class,
)
from ..logos import LogoCache
from ..paths import COURAGE_LOGO
from ..performance import _BENCHMARK_DISPLAY_NAMES
from ..trades import _TRADE_DETAIL_LABELS
from . import bars as _bars
from . import holdings_view as _holdings_view
from . import og_image as _og_image
from . import return_chart as _return_chart
from . import trades_view as _trades_view
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

    # The OG image renderer (~300 lines of Pillow plumbing: font
    # candidate search, SVG rasterisation, halo composition,
    # top-10 logo strip) lives in :mod:`investing.webpage.og_image`.
    # The methods below are thin delegators so the historical
    # ``Webpage._render_og_image`` / ``Webpage._load_font`` /
    # ``Webpage._load_logo_for_og`` / ``Webpage._top_holdings_for_og`` /
    # ``Webpage._draw_top_holdings_strip`` call surface still works
    # for any test or external caller that reached for it.
    _FONT_CANDIDATES = _og_image._FONT_CANDIDATES
    _NON_TICKER_TOP10_KEYS = _og_image.NON_TICKER_TOP10_KEYS

    @staticmethod
    def _load_font(weight: str, size: int):
        return _og_image.load_font(weight, size)

    @staticmethod
    def _load_logo_for_og(ticker: str, max_w: int, max_h: int):
        return _og_image.load_logo_for_og(ticker, max_w, max_h)

    def _top_holdings_for_og(self, limit: int = 10) -> list[str]:
        return _og_image.top_holdings_for_og(self.top_10, limit=limit)

    def _draw_top_holdings_strip(
        self, canvas, *, x: int, y: int, w: int, h: int,
    ) -> None:
        _og_image.draw_top_holdings_strip(
            canvas,
            _og_image.top_holdings_for_og(self.top_10, limit=10),
            x=x, y=y, w=w, h=h,
        )

    def _render_og_image(self) -> None:
        if self._total_return is None:
            return
        _og_image.render(
            total_return=self._total_return,
            benchmarks=self._benchmarks or [],
            top_10=self.top_10,
            benchmark_display_names=_BENCHMARK_DISPLAY_NAMES,
            now=self._now(),
        )

    def _render_og_image_unsafe(self, total_return, benchmarks) -> None:
        """Backwards-compatible thin wrapper around :func:`og_image.render`.

        The historical signature took ``(total_return, benchmarks)``;
        external callers (and earlier test snapshots) bind to that
        method directly, so we keep it as a delegator and forward
        the renderer's other dependencies through ``self``.
        """
        _og_image._render_unsafe(
            total_return=total_return,
            benchmarks=benchmarks,
            top_10=self.top_10,
            benchmark_display_names=_BENCHMARK_DISPLAY_NAMES,
            now=self._now(),
        )

    # ``holding_anchor`` and ``strip_exchange`` are imported from
    # :mod:`investing.webpage.anchors`; the static-method wrappers
    # below preserve the historical ``Webpage._holding_anchor`` /
    # ``Webpage._strip_exchange`` callsites used by the renderer and
    # the test suite.
    _holding_anchor = staticmethod(holding_anchor)

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
            '<li>All performance metrics on this page were calculated using '
            'USD as the base currency.</li>\n'
            '<li>Per-holding <strong>Return</strong> and '
            '<strong>IRR</strong> are money-weighted figures: they '
            'reflect the actual journey of capital in the position, so '
            'the size and timing of every purchase and sale shape the '
            'result \u2014 the more dollars committed when the position '
            'moved, the more weight that move carries. '
            '<strong>Return</strong> is the cumulative profit per dollar '
            'invested over the holding period; <strong>IRR</strong> is '
            'its annualised equivalent. Dividends are treated as cash '
            '(not reinvested) and reduced by an assumed 15% withholding '
            'tax; the impact of capital gains taxes is not '
            'modelled.</li>\n'
            '<li>The portfolio-level time-weighted return (TWR) chains '
            'sub-period returns across portfolio valuation snapshots, '
            'neutralising the effect of contributions and withdrawals so it '
            'reads apples-to-apples against the comparison benchmark. It '
            'was calculated excluding the impact of capital gains taxes, '
            'but including the effects of withholding taxes and transaction '
            'costs.</li>\n'
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

    # The trades-table renderer (row builder, headers, sort indices,
    # "Show all" toggle) lives in :mod:`investing.webpage.trades_view`.
    # The class-level attributes below preserve the historical
    # ``Webpage._build_trade_row`` / ``Webpage._build_trades_table`` /
    # ``Webpage._TRADES_VISIBLE_DEFAULT`` call surface used by the
    # test suite.
    _strip_exchange = staticmethod(strip_exchange)
    _build_trade_row = staticmethod(_trades_view.build_row)
    _build_trades_table = staticmethod(_trades_view.build_table)
    _TRADES_VISIBLE_DEFAULT = _trades_view.VISIBLE_DEFAULT
    _TRADES_SORTABLE_COLUMNS = _trades_view.SORTABLE_COLUMNS
    _TRADE_DETAIL_SORT_INDEX = _trades_view.TRADE_DETAIL_SORT_INDEX
    _TRADE_ACTION_SORT_INDEX = _trades_view.TRADE_ACTION_SORT_INDEX
    _TRADE_DETAIL_LABELS_REF = _TRADE_DETAIL_LABELS

    @staticmethod
    def _trade_detail_text(event) -> str:
        return _trades_view._detail_text(event)

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
    # Holdings-card + per-section "Sort by" toolbar live in
    # :mod:`investing.webpage.holdings_view`. The class-level
    # attributes below preserve the historical
    # ``Webpage._build_holding_card`` / ``Webpage._build_card`` /
    # ``Webpage._build_holdings_sort_control`` /
    # ``Webpage._HOLDINGS_SORT_OPTIONS`` call surface.
    _HOLDINGS_SORT_OPTIONS = _holdings_view.SORT_OPTIONS
    _build_card = staticmethod(_holdings_view.build_card)
    _build_holdings_sort_control = staticmethod(_holdings_view.build_sort_control)

    def _build_holding_card(self, holding) -> str:
        return _holdings_view.build_holding_card(
            holding, logo_url_for=self._get_logo_url,
        )

    # ---- chart / bar primitives (also covered directly by tests) -------

    # Bar-chart renderer lives in :mod:`investing.webpage.bars`.
    # The classmethod here preserves the historical
    # ``Webpage._render_bars`` call surface used by the test suite.
    _render_bars = staticmethod(_bars.render)

    @classmethod
    def _render_return_chart(cls, total_return, benchmarks) -> str:
        """Delegate to :func:`investing.webpage.return_chart.render`.

        The chart's NumPy math (Pchip interpolation, delta-bracket
        geometry, vectorised SVG projection) lives in the
        ``return_chart`` module; the classmethod here preserves the
        historical ``Webpage._render_return_chart`` call surface
        used by the test suite.
        """
        return _return_chart.render(
            total_return, benchmarks, benchmark_label=cls._benchmark_label,
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
