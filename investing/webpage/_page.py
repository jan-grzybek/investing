"""The ``Webpage`` renderer plus the ``generate_webpage``
entrypoint that wires per-section content into a single
rendered ``index.html`` + companion artefacts.

The page leads with the alpha and then proves it: hero, chart,
year-by-year, allocation, holdings, closed positions, activity,
method. Every section below the hero is evidence for the claim the
hero makes, in roughly the order a sceptical reader would ask for it.

This module hosts the main :class:`Webpage` class. The renderer is
intentionally kept together because most of its sections share
internal state through the instance; the self-contained pieces (the
hero, the chart, the two holdings tables, the allocation bars, the
year table, anchors, sitemap/robots) live next to it in the
``investing.webpage`` package.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from ..clock import NowFn
from ..formatting import _fmt_date_long
from ..log import logger
from ..logos import LogoCache, LogoResolver
from ..paths import COURAGE_LOGO
from ..paths import SITE_URL as _SITE_URL
from ..paths import SOCIAL_IMAGE as _SOCIAL_IMAGE
from ..performance import _BENCHMARK_DISPLAY_NAMES
from ..trades import _TRADE_DETAIL_LABELS
from ..types import (
    BenchmarkSummary,
    HoldingsRollup,
    HoldingSummary,
    TotalReturn,
    TradeEvent,
    YearlyReturn,
)
from . import allocation as _allocation
from . import hero as _hero
from . import holdings_view as _holdings_view
from . import og_image as _og_image
from . import return_chart as _return_chart
from . import trades_view as _trades_view
from . import yearly_view as _yearly_view
from .anchors import holding_anchor, strip_exchange
from .head import SiteMeta, build_analytics_tag, build_head, build_jsonld
from .sitemap import write_robots_txt, write_sitemap


def _write_if_changed(path: Path, body: str) -> bool:
    """Write ``body`` to ``path`` only when it differs from what's on disk.

    Returns ``True`` if a write happened, ``False`` if the existing file
    already matched. The skip path avoids bumping mtime on no-op runs:
    the bi-hourly CI schedule regenerates the page even when nothing
    moved (markets closed, rounded display values steady), and the
    deploy step downstream re-uploads artefacts whose mtimes changed.
    Keeping the mtime stable on a no-op render therefore avoids a
    visible "deployed at <new timestamp>" entry on the Pages dashboard
    for runs that didn't change a single rendered byte.
    """
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing == body:
        logger.info("%s: content unchanged, skipping write", path.name)
        return False
    path.write_text(body, encoding="utf-8")
    return True


class Webpage:
    """Builds the JG Investing index page as a single responsive document."""

    def __init__(
        self,
        *,
        now: NowFn | None = None,
        logo_cache: LogoResolver | None = None,
    ):
        self.return_html: str = ""
        # Pre-rendered ``<tr>`` fragments, one per position, bucketed
        # by the group band they render under. The equity / fixed
        # income split is read off each summary's ``asset_class`` tag
        # in ``add_holding``; the renderer skips an empty group
        # silently -- "no title for an empty section" is the
        # asymmetric-portfolio contract.
        self.current: list[str] = []
        self.historical: list[str] = []
        self.current_fixed_income: list[str] = []
        self.historical_fixed_income: list[str] = []
        self.allocation_pct: dict[str, float] | None = None
        self.top_10: dict[str, float] | None = None
        # Pre-rendered HTML for each row in the "Activity" section, in
        # newest-first order. An empty list omits the whole section
        # (and its nav link) cleanly.
        self.trades: list[str] = []
        # Logo URL resolver. In production this is a
        # :class:`investing.logos.LogoCache` instance that probes the
        # local repo first and falls back to an HTTP HEAD against
        # GitHub Pages -- the wrapping session has retry / timeout /
        # negative-cache behaviour so an outage cannot hang the build.
        # The constructor accepts anything satisfying
        # :class:`investing.logos.LogoResolver` (typed callable shape
        # ``(ticker: str) -> str``) so the local-preview script and
        # tests can pass a plain function that resolves against a
        # synthetic source without monkey-patching the class.
        self._logo_resolver: LogoResolver = logo_cache if logo_cache is not None else LogoCache()
        # Minimal payload for the sector allocation bar: one entry per
        # current equity holding, carrying the two fields the bar
        # needs. Populated as a side-effect of ``add_holding`` so the
        # public API stays small -- cash and historical positions
        # never reach this list in the first place, which is the
        # contract the "share of equities" denominator depends on.
        self._current_equity_sectors: list[dict] = []
        # Largest current weight seen, which normalises every weight
        # bar in the holdings table (see
        # :func:`investing.webpage.holdings_view._weight_bar`).
        self._max_weight: float = 0.0
        self._open_count = 0
        self._closed_count = 0
        # Stashed for the hero, the chart and OG image generation.
        self._total_return: TotalReturn | None = None
        self._benchmarks: list[BenchmarkSummary] | None = None
        self._yearly_returns: list[YearlyReturn] = []
        # Wall-clock plug used in the hero / sitemap / "Since X"
        # captions. ``None`` falls through to ``datetime.today`` so
        # the legacy ``freeze_today`` fixture (which monkeypatches
        # this module's bound ``datetime``) keeps working; new code
        # can inject a fixed closure directly.
        self._now: NowFn = now if now is not None else datetime.today

    # ------------------------------------------------------------------ API

    def add_return(
        self,
        total_return: TotalReturn,
        benchmarks: list[BenchmarkSummary],
        *,
        yearly_returns: list[YearlyReturn] | None = None,
    ) -> None:
        self._total_return = total_return
        self._benchmarks = benchmarks
        self._yearly_returns = list(yearly_returns or [])
        self.return_html = self._build_return_section(
            total_return,
            benchmarks,
            yearly_returns=self._yearly_returns,
        )

    def add_holding(self, holding: HoldingSummary) -> None:
        # ``asset_class`` defaults to ``"equity"`` so historical
        # callers that hand-build summary dicts without the new key
        # still bucket exactly the same as before -- only summaries
        # tagged ``"fixed_income"`` route to the dedicated FI lists.
        asset_class = holding.get("asset_class") or "equity"
        is_fixed_income = asset_class == "fixed_income"
        if holding["is_current"]:
            self._open_count += 1
            weight = holding.get("current_weight%")
            if weight is not None:
                self._max_weight = max(self._max_weight, float(weight))
            if not is_fixed_income:
                # The sector bar is an equity-only surface: bond and
                # treasury tickers carry no upstream GICS sector and
                # would either land in "Other" or break the bar's
                # "share of equities" denominator outright.
                self._current_equity_sectors.append(
                    {
                        "sector": holding.get("sector") or "",
                        "current_weight%": weight,
                    }
                )
        else:
            self._closed_count += 1
        row = self._build_holding_card(holding)
        if is_fixed_income:
            bucket = (
                self.current_fixed_income if holding["is_current"] else self.historical_fixed_income
            )
        else:
            bucket = self.current if holding["is_current"] else self.historical
        bucket.append(row)

    def add_allocations(
        self,
        allocation_pct: dict[str, float] | None,
        top_10: dict[str, float] | None,
    ) -> None:
        self.allocation_pct = allocation_pct
        self.top_10 = top_10

    def add_trades(self, trade_events: list[TradeEvent]) -> None:
        """Render each burst-aggregated trade event into a table row.

        ``trade_events`` is the newest-first list produced by
        ``get_holdings`` (or by ``Holding.trade_events`` directly in
        the preview/test paths). Rows are stored pre-rendered as
        ``<tr>`` fragments so the page assembly in ``save()`` stays
        linear; ``_build_trades_table`` wraps them with the matching
        ``<thead>`` and sortable column headers."""
        self.trades = [self._build_trade_row(event) for event in trade_events]

    def save(self, output_dir: Path | None = None):
        """Render the page and companion artefacts into ``output_dir``.

        ``output_dir`` defaults to the current working directory so the
        legacy ``chdir_tmp``-based test paths keep working unchanged;
        new callers (production pipeline, preview script) pass an
        explicit ``Path`` so the artefact write doesn't depend on
        process-level state. The four artefacts produced are
        ``index.html``, ``og-image.png`` (+ its sidecar),
        ``sitemap.xml`` and ``robots.txt``."""
        out_dir = output_dir if output_dir is not None else Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        now = self._now()
        update_date = _fmt_date_long(now)
        update_iso = now.strftime("%Y-%m-%d")
        # Best-effort: generate the OG image first so its filename can
        # be referenced from <head>. If Pillow / fonts aren't available
        # the page still renders, just without a fresh social preview.
        self._render_og_image(out_dir)

        parts: list[str] = []
        parts.append("<!DOCTYPE html>")
        parts.append('<html lang="en">')
        parts.append(self._head())
        parts.append("<body>")
        # Skip link: visually hidden until focused, lets keyboard users
        # bypass the sticky nav and jump straight to <main>.
        parts.append('<a class="skip-link" href="#main-content">Skip to content</a>')
        parts.append(self._build_site_header())
        parts.append('<main id="main-content" tabindex="-1">')

        if self._total_return is not None:
            parts.append(self._build_hero(update_date, update_iso))

        if self.return_html:
            parts.append('<section id="performance" class="section">')
            parts.append(self.return_html)
            parts.append("</section>")

        allocation_html = self._render_allocation()
        if allocation_html:
            parts.append('<section id="allocation" class="section">')
            parts.append('<h2 class="section__title">Allocation</h2>')
            parts.append(allocation_html)
            parts.append("</section>")

        holdings_table = self._build_open_holdings_table()
        if holdings_table:
            parts.append('<section id="holdings" class="section">')
            parts.append(
                self._section_head(
                    "Holdings",
                    f"All {self._open_count} shown &middot; click a column to sort",
                )
            )
            parts.append(self._metrics_note())
            parts.append(holdings_table)
            parts.append("</section>")

        closed_table = self._build_closed_holdings_table()
        if closed_table:
            parts.append('<section id="closed" class="section">')
            parts.append(
                self._section_head(
                    "Closed positions",
                    "The losses stay on the page &middot; click a column to sort",
                )
            )
            parts.append(closed_table)
            parts.append("</section>")

        if self.trades:
            parts.append('<section id="activity" class="section">')
            parts.append(
                self._section_head(
                    "Activity",
                    f"{len(self.trades)} entries since inception",
                )
            )
            # Pins the one methodology detail the reader would
            # otherwise have to infer from the data: what "combined"
            # rows represent. The "rolling quarter" wording matches
            # the long-term-investor framing of the page (a
            # fund-letter cadence rather than a high-frequency trade
            # log) and is the natural human reading of the 90-day
            # numerical ``TRADE_WINDOW_DAYS`` constant. The second
            # sentence is the privacy contract, stated where a reader
            # would otherwise wonder why no sizes appear.
            parts.append(
                '<p class="section__intro">'
                "Every executed trade since inception. Fills within a "
                "rolling quarter are combined into a single entry at "
                "their volume-weighted average per-share price. Sizes are "
                "never published &mdash; only relative changes and "
                "per-share prices."
                "</p>"
            )
            parts.append(self._build_trades_table(self.trades))
            parts.append("</section>")

        parts.append("</main>")
        parts.append(self._footer(update_date, update_iso))
        # Analytics beacon lives next to its CSP whitelist entry in
        # :mod:`investing.webpage.head`; the renderer just splices in
        # the pre-built fragment so adding / removing third-party
        # scripts is a single-edit change.
        parts.append(build_analytics_tag())
        parts.append("</body>")
        parts.append("</html>")

        # Content-addressable short-circuit: on the bi-hourly schedule
        # most regenerations produce byte-identical HTML (markets are
        # closed, the page already shows today's data, or rounded
        # display values haven't moved). Comparing the new bytes to
        # what's on disk before writing avoids bumping ``index.html``'s
        # mtime, which in turn lets ``actions/deploy-pages`` skip a
        # no-op redeploy and lets the OG image's content cache stay
        # valid alongside it. ``robots.txt`` is deterministic from the
        # site URL so the same comparison short-circuits there; the
        # sitemap intentionally embeds the daily ``<lastmod>`` so it
        # still rewrites once a day.
        _write_if_changed(out_dir / "index.html", "\n".join(parts))
        write_sitemap(self.SITE_URL, out_dir, now=self._now)
        write_robots_txt(self.SITE_URL, out_dir)

    # ----------------------------------------------------------- internals

    SITE_TITLE = "Jan Grzybek Investment Portfolio"
    # Used in <title>, OG/Twitter title, and JSON-LD. Keep it short so
    # search engines render it without truncation in SERPs (~60 chars).
    SEO_TITLE = "Jan Grzybek - Investment Portfolio"
    # Sourced from :mod:`investing.paths`, where the canonical value
    # is env-overridable (``INVESTING_SITE_URL``) so a fork or staging
    # build can repoint the canonical / sitemap / OG URLs in one place
    # without patching the source.
    SITE_URL = _SITE_URL
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
    SOCIAL_IMAGE = _SOCIAL_IMAGE
    # Each entry maps an anchor to a label and a list of attribute
    # names: the link is emitted iff at least one of the named
    # attributes is truthy. "Method" is unconditional -- the
    # disclaimer renders on every page, whatever the portfolio holds.
    _NAV_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("performance", "Performance", ("return_html",)),
        ("holdings", "Holdings", ("current", "current_fixed_income")),
        ("activity", "Activity", ("trades",)),
        ("method", "Method", ()),
    )

    def _build_site_header(self) -> str:
        """Brand lockup plus the section nav.

        The name is set at the same size as the section label beside
        it rather than at 30px / 800: on the old header the reader's
        name outranked every piece of evidence on the page, which is
        the same inversion the OG card had. Identify, then get out of
        the way.
        """
        links = []
        for anchor, label, attrs in self._NAV_ITEMS:
            if attrs and not any(getattr(self, attr) for attr in attrs):
                continue
            links.append(f'<a href="#{anchor}">{html.escape(label)}</a>')
        nav_html = (
            f'<nav class="site-nav" aria-label="Page sections">{"".join(links)}</nav>'
            if len(links) > 1
            else ""
        )
        return (
            '<header class="site-header">'
            '<p class="site-brand">'
            f'<img class="site-brand__mark" src="{html.escape(COURAGE_LOGO)}" alt="" '
            'width="26" height="26" decoding="async">'
            '<span class="site-brand__name">Jan Grzybek</span>'
            '<span class="site-brand__sep" aria-hidden="true">/</span>'
            '<span class="site-brand__section">Investment Portfolio</span>'
            "</p>"
            f"{nav_html}"
            "</header>"
        )

    @staticmethod
    def _section_head(title: str, note: str) -> str:
        """A section title with its right-aligned note on one line."""
        return (
            '<div class="section__head">'
            f'<h2 class="section__title">{html.escape(title)}</h2>'
            f'<p class="section__note">{note}</p>'
            "</div>"
        )

    def _build_hero(self, update_date: str, update_iso: str) -> str:
        assert self._total_return is not None
        benchmarks = self._benchmarks or []
        return _hero.render(
            total_return=self._total_return,
            benchmarks=benchmarks,
            yearly_returns=self._yearly_returns,
            benchmark_label=(
                self._benchmark_label(benchmarks[0]) if benchmarks else "the benchmark"
            ),
            position_counts=(self._open_count, self._closed_count),
            now=self._now(),
            update_date=update_date,
            update_iso=update_iso,
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

    # ----------------------------------------------------- OG image

    # The OG image renderer (font candidate search, SVG
    # rasterisation, the two-column composition, top-10 logo strip)
    # lives in :mod:`investing.webpage.og_image`. The methods below
    # are thin delegators so the historical
    # ``Webpage._render_og_image`` / ``Webpage._load_font`` /
    # ``Webpage._load_logo_for_og`` / ``Webpage._top_holdings_for_og`` /
    # ``Webpage._draw_top_holdings_strip`` call surface still works
    # for any test or external caller that reached for it.
    _FONT_FILES = _og_image._FONT_FILES
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
        self,
        canvas,
        *,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        _og_image.draw_top_holdings_strip(
            canvas,
            _og_image.top_holdings_for_og(self.top_10, limit=10),
            x=x,
            y=y,
            w=w,
            h=h,
        )

    def _render_og_image(self, output_dir: Path | None = None) -> None:
        if self._total_return is None:
            return
        _og_image.render(
            total_return=self._total_return,
            benchmarks=self._benchmarks or [],
            top_10=self.top_10,
            benchmark_display_names=_BENCHMARK_DISPLAY_NAMES,
            now=self._now(),
            output_dir=output_dir,
        )

    def _render_og_image_unsafe(self, total_return, benchmarks, output_dir=None) -> None:
        """Backwards-compatible thin wrapper around :func:`og_image.render`.

        The historical signature took ``(total_return, benchmarks)``;
        external callers (and earlier test snapshots) bind to that
        method directly, so we keep it as a delegator and forward
        the renderer's other dependencies through ``self``. ``output_dir``
        defaults to ``None`` so the legacy CWD-based path keeps working.
        """
        _og_image._render_unsafe(
            total_return=total_return,
            benchmarks=benchmarks,
            top_10=self.top_10,
            benchmark_display_names=_BENCHMARK_DISPLAY_NAMES,
            now=self._now(),
            output_dir=output_dir,
        )

    # ``holding_anchor`` and ``strip_exchange`` are imported from
    # :mod:`investing.webpage.anchors`; the static-method wrappers
    # below preserve the historical ``Webpage._holding_anchor`` /
    # ``Webpage._strip_exchange`` callsites used by the renderer and
    # the test suite.
    _holding_anchor = staticmethod(holding_anchor)

    @staticmethod
    def _footer(update_date: str, update_iso: str) -> str:
        """The Method & disclaimer block.

        Two columns rather than a bullet list, because the two things
        a reader needs from it are a *pair*: what the portfolio-level
        number means, and what the per-holding numbers mean. Setting
        them side by side is the point -- the distinction between
        time-weighted and money-weighted is the single most
        misreadable thing on the page, and burying it as bullet three
        of four made it look like boilerplate.

        The heading is not in the in-page nav's section list by
        accident: "Method" is the fourth nav link, because a reader
        who wants to know how a number was computed should not have
        to scroll to find out.
        """
        return (
            '<footer id="method" class="method">\n'
            '<h2 class="method__title">Method &amp; disclaimer</h2>\n'
            '<div class="method__grid">\n'
            '<p class="method__note"><strong>Portfolio TWR</strong> chains '
            "sub-period returns across valuation snapshots, so contributions "
            "and withdrawals don't flatter or penalise the number. That is "
            "what makes it comparable to the benchmark. Calculated in "
            "<strong>USD</strong>, excluding capital-gains tax and including "
            "withholding tax and transaction costs.</p>\n"
            '<p class="method__note"><strong>Per-holding Return and IRR</strong> '
            "are money-weighted: the size and timing of every fill shape the "
            "result, so they do not sum to the time-weighted figure above. "
            "Return is the cumulative profit per dollar invested; IRR is its "
            "annualised equivalent. Dividends are treated as cash, reduced by "
            "an assumed 15% withholding tax. Prices and dividends from "
            '<a href="https://finance.yahoo.com/markets/stocks/trending/" '
            'title="Yahoo Finance" rel="noopener noreferrer">Yahoo Finance</a>.'
            "</p>\n"
            "</div>\n"
            '<p class="method__legal">For <strong>informational purposes '
            "only</strong> &mdash; nothing here is a recommendation to buy, "
            "sell or hold any security. Logos are trademarks of their "
            "respective owners, used for identification only. Cloudflare Web "
            "Analytics measures anonymous traffic; <strong>no cookies or "
            "tracking identifiers are used.</strong> Updated on "
            f'<time datetime="{update_iso}">{update_date}</time>.</p>\n'
            "</footer>"
        )

    def _get_logo_url(self, ticker: str) -> str:
        """Resolve a holding logo URL via the injected resolver.

        Delegates to the :class:`investing.logos.LogoResolver` wired in
        ``__init__`` so the HTTP plumbing (session reuse, retry,
        timeout, negative cache) lives in one place and tests /
        preview scripts can swap in a stub via the ``logo_cache=``
        constructor parameter without monkey-patching the class.
        """
        return self._logo_resolver(ticker)

    # ---- per-section builders ------------------------------------------

    def _build_return_section(
        self,
        total_return,
        benchmarks,
        *,
        yearly_returns: list[YearlyReturn] | None = None,
    ) -> str:
        lines: list[str] = []
        lines.append(
            self._section_head(
                "Cumulative return",
                self._chart_legend(benchmarks),
            )
        )
        lines.append(
            '<p class="section__intro">'
            "Hover the chart to read the return and alpha on any date. "
            "The shaded band is the running gap between the two."
            "</p>"
            if benchmarks
            else '<p class="section__intro">Hover the chart to read the return on any date.</p>'
        )
        chart = self._render_return_chart(total_return, benchmarks)
        if chart:
            lines.append(chart)
        yearly_html = _yearly_view.render(
            yearly_returns or [],
            benchmarks,
            benchmark_label=(self._benchmark_label(benchmarks[0]) if benchmarks else "Benchmark"),
        )
        if yearly_html:
            lines.append(yearly_html)
        return "\n".join(lines)

    def _chart_legend(self, benchmarks) -> str:
        """The chart's key, rendered in the section head's note slot."""
        chips = [
            '<span class="legend__item">'
            '<span class="legend__swatch legend__swatch--jg"></span>Portfolio</span>'
        ]
        if benchmarks:
            label = html.escape(self._benchmark_label(benchmarks[0]))
            chips.append(
                '<span class="legend__item">'
                f'<span class="legend__swatch legend__swatch--bench"></span>{label}</span>'
            )
            chips.append(
                '<span class="legend__item">'
                '<span class="legend__swatch legend__swatch--band"></span>Alpha</span>'
            )
        return f'<span class="legend">{"".join(chips)}</span>'

    @staticmethod
    def _metrics_note() -> str:
        """The money-weighted caveat, explained where it is used.

        The difference between the time-weighted figure in the hero
        and the money-weighted figures in this table is real,
        important, and was previously explained only in a footnote
        four screens down. It now sits directly above the columns it
        applies to, with the full explanation one click away rather
        than one scroll.
        """
        return (
            '<p class="section__intro">'
            "<strong>Return</strong> and <strong>IRR</strong> here are "
            "money-weighted &mdash; they follow the actual dollars, so they "
            "don't add up to the time-weighted number above. "
            '<button type="button" class="metrics-note__toggle" '
            'aria-expanded="false" aria-controls="metrics-note" '
            'data-label-open="Why?" data-label-close="Hide">Why?</button>'
            "</p>"
            '<div class="metrics-note" id="metrics-note" hidden>'
            '<div class="metrics-note__grid">'
            "<div>"
            '<h3 class="metrics-note__title">'
            '<span class="metrics-note__swatch metrics-note__swatch--jg"></span>'
            "Time-weighted &mdash; the number up top</h3>"
            "<p>Chains the return of each sub-period between valuation "
            "snapshots, then multiplies them together. Adding or withdrawing "
            "cash changes the size of the portfolio but not the chain, so the "
            "result measures <strong>the decisions</strong>, not the funding. "
            "That is why it is the only figure fair to set against an index.</p>"
            "</div>"
            "<div>"
            '<h3 class="metrics-note__title">'
            '<span class="metrics-note__swatch metrics-note__swatch--bench"></span>'
            "Money-weighted &mdash; the numbers per holding</h3>"
            "<p>Solves for the rate that makes every actual cash flow "
            "balance, so buying more before a run-up counts for more than "
            "buying after it. It measures <strong>the dollars</strong>. Two "
            "holdings with the same price chart can post different IRRs "
            "purely on timing.</p>"
            "</div>"
            '<p class="metrics-note__foot">Both exclude capital-gains tax and '
            "both net out an assumed 15% dividend withholding. Neither is "
            "&ldquo;the real one&rdquo; &mdash; they answer different "
            "questions, which is why the page shows both.</p>"
            "</div>"
            "</div>"
        )

    def _group_label(self, name: str, rows: list[str]) -> str:
        """Band text for a holdings group, with its share of the book.

        The share comes from the allocation rollup rather than from
        summing the rows: the rollup is the denominator the allocation
        bar uses, and two numbers on one page that disagree about what
        100% means is exactly the defect this redesign set out to fix.
        """
        share = (self.allocation_pct or {}).get(name)
        if share is None:
            return f"{name} · {len(rows)} position{'s' if len(rows) != 1 else ''}"
        return f"{name} · {share:.1f}% of portfolio"

    def _build_open_holdings_table(self) -> str:
        groups = [
            _holdings_view.build_group(
                label=self._group_label("Equities", self.current),
                rows=self.current,
                columns=len(_holdings_view.OPEN_COLUMNS) + 1,
            ),
            _holdings_view.build_group(
                label=self._fixed_income_label(),
                rows=self.current_fixed_income,
                columns=len(_holdings_view.OPEN_COLUMNS) + 1,
            ),
        ]
        return _holdings_view.build_table(
            scope="open",
            groups=groups,
            columns=_holdings_view.OPEN_COLUMNS,
            caption="Current holdings",
            weight_scale=self._max_weight,
        )

    def _fixed_income_label(self) -> str:
        """Band text for fixed income, carrying the cash line with it.

        Cash has no rows of its own -- it is a residual, not a
        position -- so it would otherwise be the one slice of the
        allocation bar with nowhere in the table to land. Naming it
        beside fixed income keeps the two denominators reconcilable
        by eye.
        """
        label = self._group_label("Fixed Income", self.current_fixed_income)
        cash = (self.allocation_pct or {}).get(_allocation.CASH_LABEL)
        if cash is None:
            return label
        return f"{label} · Cash {cash:.1f}%"

    def _build_closed_holdings_table(self) -> str:
        columns = len(_holdings_view.CLOSED_COLUMNS) + 1
        groups = [
            _holdings_view.build_group(
                label=f"Equities · {len(self.historical)} closed",
                rows=self.historical,
                columns=columns,
            ),
            _holdings_view.build_group(
                label=f"Fixed income · {len(self.historical_fixed_income)} closed",
                rows=self.historical_fixed_income,
                columns=columns,
            ),
        ]
        return _holdings_view.build_table(
            scope="closed",
            groups=groups,
            columns=_holdings_view.CLOSED_COLUMNS,
            caption="Closed positions",
        )

    def _render_allocation(self) -> str:
        return _allocation.render(
            self.allocation_pct,
            _allocation.sector_totals(self._current_equity_sectors),
        )

    @staticmethod
    def _benchmark_label(benchmark) -> str:
        """Friendly display name for a benchmark, falling back gracefully."""
        ticker = benchmark.get("ticker", "")
        return (
            _BENCHMARK_DISPLAY_NAMES.get(ticker) or benchmark.get("name") or ticker or "Benchmark"
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

    # Holdings rows + table assembly live in
    # :mod:`investing.webpage.holdings_view`. The class-level
    # attributes below preserve the historical call surface.
    _OPEN_COLUMNS = _holdings_view.OPEN_COLUMNS
    _CLOSED_COLUMNS = _holdings_view.CLOSED_COLUMNS
    _build_holdings_group = staticmethod(_holdings_view.build_group)
    _build_holdings_table = staticmethod(_holdings_view.build_table)

    def _build_holding_card(self, holding) -> str:
        return _holdings_view.build_row(holding, logo_url_for=self._get_logo_url)

    # ---- chart primitive (also covered directly by tests) --------------

    @classmethod
    def _render_return_chart(cls, total_return, benchmarks) -> str:
        """Delegate to :func:`investing.webpage.return_chart.render`.

        The chart's NumPy math (Pchip interpolation, axis tick
        selection, band splitting, vectorised SVG projection) lives in
        the ``return_chart`` module; the classmethod here preserves
        the historical ``Webpage._render_return_chart`` call surface
        used by the test suite.
        """
        return _return_chart.render(
            total_return,
            benchmarks,
            benchmark_label=cls._benchmark_label,
        )


def generate_webpage(
    total_return: TotalReturn,
    benchmarks: list[BenchmarkSummary],
    holdings: HoldingsRollup,
    *,
    yearly_returns: list[YearlyReturn] | None = None,
    output_dir: Path | None = None,
    now: NowFn | None = None,
) -> None:
    """Render ``Webpage`` from a pre-computed pipeline output bundle.

    ``output_dir`` is forwarded through ``Webpage.save`` so production /
    preview / test callers can pin an explicit destination directory;
    ``None`` falls back to the current working directory to preserve
    the historical ``chdir_tmp``-style fixture path.

    Fixed-income buckets ride alongside the equity buckets so a
    portfolio that grows a fixed-income sleeve surfaces it on the
    rendered page without any further changes to the orchestrator;
    ``Webpage.add_holding`` reads the per-summary ``asset_class``
    tag and routes each row into the matching group band.
    """
    webpage = Webpage(now=now)
    webpage.add_allocations(holdings.get("allocation%"), holdings.get("top_10"))
    for holding in holdings["current"]:
        webpage.add_holding(holding)
    for holding in holdings.get("current_fixed_income") or []:
        webpage.add_holding(holding)
    for holding in holdings["historical"]:
        webpage.add_holding(holding)
    for holding in holdings.get("historical_fixed_income") or []:
        webpage.add_holding(holding)
    # After the holdings, so the hero can state how many positions
    # are open and how many have been closed.
    webpage.add_return(
        total_return,
        benchmarks,
        yearly_returns=yearly_returns,
    )
    webpage.add_trades(holdings.get("trades") or [])
    webpage.save(output_dir)
