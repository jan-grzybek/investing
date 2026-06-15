"""Render the page locally with synthetic data, for visual debugging.

The production pipeline (``python -m investing``) needs real Google
Sheets credentials and live market data to run. This helper
short-circuits both and feeds ``Webpage`` plausible-looking synthetic
data so you can inspect the rendered HTML / OG image / sitemap /
robots in a browser without touching production secrets.

Usage (run from the repo root)::

    python scripts/preview.py                  # writes to ./preview/
    python scripts/preview.py --out /tmp/jg    # custom output directory
    python scripts/preview.py --open           # also open index.html
                                               # in the default browser

All artifacts (``index.html``, ``og-image.png``, ``sitemap.xml``,
``robots.txt``) are gitignored so they will not pollute the repo
even if you point ``--out`` at the current directory.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

# When invoked as ``python scripts/preview.py`` Python prepends
# ``scripts/`` (not the repo root) to ``sys.path``, so ``import
# investing`` would otherwise fail. Bootstrap the repo root onto
# ``sys.path`` before the package import below so the script can be
# launched directly from the repo root without an editable install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from investing.paths import COURAGE_LOGO, LOGOS_ADDRESS  # noqa: E402
from investing.webpage import Webpage  # noqa: E402

# Local mirror of what GitHub Pages serves under ``LOGOS_ADDRESS``.
# The repo keeps hand-curated originals under ``logos/`` and a
# parallel ``logos/tight/`` mirror that ``scripts/tighten_logos.py``
# regenerates whenever a source changes -- the served bytes are the
# tight crop, so the preview must read from the same place to match
# production. See ``investing.paths._REPO_LOGOS_DIR`` for the prod
# counterpart and the ``regenerate-logos`` workflow / pre-commit hook
# for the maintenance contract.
_REPO_LOGOS_DIR = _REPO_ROOT / "logos" / "tight"


def _build_logo_extension_map() -> dict[str, str]:
    """Index ``logos/`` by ticker stem so the stub can pick the right
    extension (``.svg`` / ``.png`` / ``.jpg``) per ticker."""
    if not _REPO_LOGOS_DIR.is_dir():
        return {}
    mapping: dict[str, str] = {}
    for entry in _REPO_LOGOS_DIR.iterdir():
        if not entry.is_file():
            continue
        stem, _, ext = entry.name.rpartition(".")
        if stem and ext:
            mapping[stem] = "." + ext
    return mapping


class _StubLogoCache:
    """Offline stand-in for :class:`investing.logos.LogoCache`.

    The real cache probes ``LOGOS_ADDRESS`` over HTTP for each
    candidate extension and falls back to ``courage.png`` when none
    match. We do the same shape of lookup against the repo's local
    ``logos/`` directory (which is what GitHub Pages serves anyway)
    so the preview render never has to leave the workstation. The
    class shape mirrors ``LogoCache``'s public surface --
    ``__call__`` for the URL, ``aspect_ratio`` for the equal-area
    sizing math, and ``coverage_ratio`` for the equal-VISUAL-area
    density correction the sector treemap layers on top -- so the
    ``Webpage`` callsite's ``getattr(..., "aspect_ratio", None)`` /
    ``getattr(..., "coverage_ratio", None)`` probes both find the
    methods and the preview's treemap renders with per-logo factors
    that match production.
    """

    def __init__(self, extension_map: dict[str, str], logos_dir: Path):
        self._extensions = extension_map
        self._logos_dir = logos_dir
        self._aspect_cache: dict[str, float] = {}
        self._density_cache: dict[str, float] = {}

    def __call__(self, ticker: str) -> str:
        ext = self._extensions.get(ticker)
        if ext is None:
            return COURAGE_LOGO
        encoded = ticker.replace(":", "%3A")
        return f"{LOGOS_ADDRESS}{encoded}{ext}"

    def aspect_ratio(self, ticker: str) -> float:
        from investing.logos import _DEFAULT_LOGO_ASPECT, _parse_svg_aspect_ratio

        cached = self._aspect_cache.get(ticker)
        if cached is not None:
            return cached
        aspect = _DEFAULT_LOGO_ASPECT
        ext = self._extensions.get(ticker)
        if ext == ".svg":
            path = self._logos_dir / f"{ticker}.svg"
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                parsed = _parse_svg_aspect_ratio(text)
                if parsed is not None:
                    aspect = parsed
        self._aspect_cache[ticker] = aspect
        return aspect

    def coverage_ratio(self, ticker: str) -> float:
        from investing.logos import _DEFAULT_LOGO_DENSITY, _measure_svg_density

        cached = self._density_cache.get(ticker)
        if cached is not None:
            return cached
        density = _DEFAULT_LOGO_DENSITY
        ext = self._extensions.get(ticker)
        if ext == ".svg":
            path = self._logos_dir / f"{ticker}.svg"
            if path.is_file():
                measured = _measure_svg_density(str(path))
                if measured is not None:
                    density = measured
        self._density_cache[ticker] = density
        return density


def _ease_history(
    start: datetime, end: datetime, end_value: float, *, step_days: int = 14
) -> list[tuple[datetime, float]]:
    """Generate a smooth (cubic ease-in-out) cumulative-return curve.

    Mirrors the shape of a calm uptrend over the requested period so
    the line chart and the OG sparkline both look realistic."""
    days = (end - start).days or 1
    points: list[tuple[datetime, float]] = []
    cur = start
    while cur <= end:
        t = (cur - start).days / days
        eased = t * t * (3 - 2 * t)
        points.append((cur, 1.0 + (end_value - 1.0) * eased))
        cur += timedelta(days=step_days)
    if points[-1][0] != end:
        points.append((end, end_value))
    return points


def _holding(
    ticker: str,
    name: str,
    tsr: float,
    cagr: float,
    weight: float,
    period_start: datetime,
    *,
    website: str | None = None,
    sector: str = "",
    asset_class: str = "equity",
) -> dict:
    return {
        "ticker": ticker,
        "name": name,
        "tsr%": tsr,
        "cagr%": cagr,
        "is_current": True,
        "current_weight%": weight,
        "current_value_usd": weight * 1000,
        "periods": [{"start": period_start, "end": None}],
        # Click target for the capsule logo. The production path
        # fills this from yfinance's ``website`` / ``irWebsite`` fields
        # (with a Google-search fallback in ``resolve_company_url``);
        # the preview hard-codes plausible issuer URLs so a developer
        # can click through and verify the wrapper navigates as
        # expected.
        "website": website,
        # Sector tag mirrors what yfinance's ``info["sector"]`` would
        # return for the production summary. Drives the equities
        # treemap below the top-N bar chart; empty / missing values
        # get bucketed into the renderer's "Other" sentinel so the
        # preview exercises that branch by leaving at least one row
        # unset.
        "sector": sector,
        # Asset class tag the renderer's bucketing reads. Defaults to
        # ``"equity"`` so the rest of the synthetic dataset keeps the
        # historical equity-side rendering; pass
        # ``asset_class="fixed_income"`` to feed the dedicated Fixed
        # Income sub-section so the preview exercises that path.
        "asset_class": asset_class,
    }


def _build_dataset() -> dict:
    """Synthesise a portfolio + benchmark + holdings bundle.

    Tickers are picked from the repo's ``logos/`` directory so every
    holding's logo resolves to a real file already deployed alongside
    ``index.html``. Numbers are made up but plausibly shaped."""
    start = datetime(2022, 6, 1)
    end = datetime.today()

    total_return = {
        "start_date": start,
        "history": _ease_history(start, end, 1.484),
        "twr%": 48.4,
        "cagr%": 10.5,
    }
    benchmarks = [
        {
            "ticker": "LSE:VUAA.L",
            "name": "Vanguard S&P 500 UCITS ETF",
            "tsr%": 41.7,
            "cagr%": 9.2,
            "periods": [{"start": start, "end": None}],
            "history": _ease_history(start, end, 1.417),
        }
    ]
    # ``period_start`` dates on the current-holdings rows mirror the
    # OPEN trade dates in ``_build_trade_events`` wherever the two
    # cross-reference each other, so a developer eyeballing the
    # preview sees a coherent timeline across the "Current holdings"
    # and "Trades" sections rather than two unrelated
    # synthetic data sets that happen to share tickers. Tickers that
    # have no corresponding OPEN trade in the log (ADBE, AMAT, SPGI,
    # META, UNH) keep their own plausible "owned since" dates.
    current = [
        _holding(
            "NMS:NVDA", "NVIDIA Corporation", 217.4, 64.2, 21.4,
            datetime(2024, 8, 14), website="https://www.nvidia.com",
            sector="Technology",
        ),
        _holding(
            "NMS:GOOGL", "Alphabet Inc.", 41.2, 18.6, 13.7,
            datetime(2025, 9, 1), website="https://www.abc.xyz",
            sector="Communication Services",
        ),
        _holding(
            "NMS:META", "Meta Platforms, Inc.", 156.8, 47.2, 11.5,
            datetime(2023, 1, 12), website="https://investor.atmeta.com",
            sector="Communication Services",
        ),
        _holding(
            "NMS:ADBE", "Adobe Inc.", 28.4, 12.7, 9.1,
            datetime(2023, 4, 5), website="https://www.adobe.com",
            sector="Technology",
        ),
        _holding(
            "NMS:AMAT", "Applied Materials, Inc.", 62.3, 22.4, 7.9,
            datetime(2023, 11, 9), website="https://www.appliedmaterials.com",
            sector="Technology",
        ),
        _holding(
            "NMS:LRCX", "Lam Research Corporation", 74.6, 26.8, 6.4,
            datetime(2025, 5, 15), website="https://www.lamresearch.com",
            sector="Technology",
        ),
        _holding(
            "NYQ:SPGI", "S&P Global Inc.", 34.1, 14.2, 6.0,
            datetime(2023, 9, 18), website="https://www.spglobal.com",
            sector="Financial Services",
        ),
        _holding(
            "NYQ:UNH", "UnitedHealth Group Inc.", -11.8, -5.1, 4.7,
            datetime(2024, 3, 17), website="https://www.unitedhealthgroup.com",
            sector="Healthcare",
        ),
        _holding(
            "NYQ:CRM", "Salesforce, Inc.", 18.7, 9.4, 4.1,
            datetime(2026, 1, 15), website="https://www.salesforce.com",
            sector="Technology",
        ),
        # SAP is left without an explicit ``website`` to exercise the
        # renderer-side Google-search fallback path; the production
        # ``Holding.summary`` would have filled this in already, but
        # the preview keeps one row null so a developer can verify
        # the safety-net branch by inspection. Sector is also left
        # blank so the treemap's "Other" fallback bucket is
        # exercised end-to-end in the preview render.
        _holding("DUS:SSU.DU", "SAP SE", 46.9, 19.1, 3.5, datetime(2026, 4, 1)),
    ]
    # Two current fixed-income holdings exercise the dedicated
    # sub-section: header + sort toolbar (>1 row gates the toolbar)
    # + capsule list. The treemap is intentionally absent for fixed
    # income so the preview confirms the renderer keeps it equity-only.
    current_fixed_income = [
        _holding(
            "NMS:TLT", "iShares 20+ Year Treasury Bond ETF", 4.8, 1.6, 6.2,
            datetime(2024, 2, 1), website="https://www.ishares.com",
            sector="Government", asset_class="fixed_income",
        ),
        _holding(
            "NMS:LQD", "iShares iBoxx $ Investment Grade Corporate Bond ETF",
            7.4, 2.4, 4.5, datetime(2024, 5, 10),
            website="https://www.ishares.com",
            sector="Corporate", asset_class="fixed_income",
        ),
    ]
    historical = [
        {
            "ticker": "NMS:BIDU",
            "name": "Baidu, Inc.",
            "tsr%": -22.4,
            "cagr%": -14.6,
            "is_current": False,
            "current_weight%": None,
            "current_value_usd": 0.0,
            "periods": [{"start": datetime(2022, 11, 4), "end": datetime(2025, 11, 20)}],
            "website": "https://ir.baidu.com",
        },
        {
            "ticker": "NMS:FRSH",
            "name": "Freshworks Inc.",
            "tsr%": 31.8,
            "cagr%": 13.7,
            "is_current": False,
            "current_weight%": None,
            "current_value_usd": 0.0,
            # Listed in chronological order on purpose -- the renderer
            # in ``Webpage._build_card`` re-sorts to newest-first so
            # whichever order we hand it over in, the most recent
            # ownership window ends up on top of the stack.
            "periods": [
                {"start": datetime(2022, 8, 5), "end": datetime(2023, 6, 9)},
                {"start": datetime(2025, 7, 22), "end": datetime(2025, 12, 30)},
            ],
            "website": "https://www.freshworks.com",
        },
    ]
    # One historical fixed-income row so the dedicated sub-heading
    # appears under "Historical holdings" (the renderer hides the
    # sub-section when the bucket is empty). The single row also
    # gates the sort toolbar off, so the preview demonstrates the
    # "no toolbar for one row" branch alongside the multi-row
    # fixed-income sort toolbar in "Current holdings" above.
    historical_fixed_income = [
        {
            "ticker": "NMS:SHY",
            "name": "iShares 1-3 Year Treasury Bond ETF",
            "tsr%": 1.7,
            "cagr%": 0.9,
            "is_current": False,
            "current_weight%": None,
            "current_value_usd": 0.0,
            "periods": [
                {"start": datetime(2023, 6, 1), "end": datetime(2024, 1, 31)},
            ],
            "website": "https://www.ishares.com",
            "asset_class": "fixed_income",
        },
    ]
    allocation = {
        "Equities": 78.7,
        "Fixed Income": 10.7,
        "Cash & Cash Equivalents": 10.6,
    }
    top_10 = {h["ticker"]: h["current_weight%"] for h in current}
    trades = _build_trade_events(end)
    return {
        "total_return": total_return,
        "benchmarks": benchmarks,
        "current": current,
        "current_fixed_income": current_fixed_income,
        "historical": historical,
        "historical_fixed_income": historical_fixed_income,
        "allocation": allocation,
        "top_10": top_10,
        "trades": trades,
    }


def _trade(
    ticker: str,
    name: str,
    currency: str,
    category: str,
    price: float,
    start: datetime,
    end: datetime | None = None,
    delta_pct: float | None = None,
) -> dict:
    """Shape matches what ``Holding.trade_events`` produces in production.

    ``delta_pct`` is the magnitude of the position change as a
    percentage of the pre-burst holding (set on INCREASE / DECREASE
    rows, ``None`` on OPEN / CLOSE). In production it's derived by
    ``_combine_trade_events`` from per-event ``pre_quantity``; the
    preview short-circuits that computation and just sets the value
    directly so we don't have to thread synthetic quantities all the
    way through.
    """
    return {
        "ticker": ticker,
        "name": name,
        "currency": currency,
        "category": category,
        "price": price,
        "start_date": start,
        "end_date": end or start,
        "delta_pct": delta_pct,
    }


def _build_trade_events(today: datetime) -> list[dict]:
    """Synthesise a believable trades log for the preview.

    Each entry mimics one of the four real categories
    (``OPEN`` / ``INCREASE`` / ``DECREASE`` / ``CLOSE``) and includes
    a couple of multi-day bursts so the "rolling-quarter combined"
    rendering is exercised. The section is now a complete activity
    log (production no longer trims to a trailing year either) so we
    leave the older bursts in so the rendered table demonstrates the
    sortable date column with enough rows to be meaningful. The list
    is intentionally a mix of tickers that also appear in
    ``current``/``historical`` so the logos resolve and the
    cross-section linking feels coherent. The ``delta_pct`` values on
    INCREASE / DECREASE rows are made up but plausible so the badge
    text demonstrates the magnitude readout in the rendered preview.
    The ``today`` parameter is accepted for callers that want the
    log anchored at a specific moment; this helper itself doesn't
    filter on it any more.
    """
    del today  # kept for backward compatibility with callers
    events = [
        _trade(
            "NMS:NVDA",
            "NVIDIA Corporation",
            "USD",
            "INCREASE",
            921.40,
            datetime(2026, 5, 14),
            delta_pct=32.0,
        ),
        # Fixed-income fills sit alongside equity trades in the same
        # log -- the section reads as a chronological activity feed
        # rather than a per-asset-class report. The LQD entries here
        # demonstrate that intermix in the preview render.
        _trade(
            "NMS:LQD",
            "iShares iBoxx $ Investment Grade Corporate Bond ETF",
            "USD",
            "INCREASE",
            108.85,
            datetime(2026, 4, 22),
            delta_pct=18.0,
        ),
        _trade("DUS:SSU.DU", "SAP SE", "EUR", "OPEN", 181.25, datetime(2026, 4, 1)),
        _trade(
            "NMS:META",
            "Meta Platforms, Inc.",
            "USD",
            "DECREASE",
            504.60,
            datetime(2026, 3, 8),
            delta_pct=25.0,
        ),
        _trade(
            "NYQ:CRM",
            "Salesforce, Inc.",
            "USD",
            "OPEN",
            247.85,
            datetime(2026, 1, 15),
            datetime(2026, 2, 12),
        ),
        _trade("NMS:FRSH", "Freshworks Inc.", "USD", "CLOSE", 15.85, datetime(2025, 12, 30)),
        _trade("NMS:BIDU", "Baidu, Inc.", "USD", "CLOSE", 98.30, datetime(2025, 11, 20)),
        _trade(
            "NYQ:UNH",
            "UnitedHealth Group Inc.",
            "USD",
            "INCREASE",
            472.10,
            datetime(2025, 10, 17),
            datetime(2025, 11, 9),
            delta_pct=100.0,
        ),
        _trade("NMS:GOOGL", "Alphabet Inc.", "USD", "OPEN", 142.65, datetime(2025, 9, 1)),
        _trade("NMS:FRSH", "Freshworks Inc.", "USD", "OPEN", 13.40, datetime(2025, 7, 22)),
        _trade(
            "NMS:LQD",
            "iShares iBoxx $ Investment Grade Corporate Bond ETF",
            "USD",
            "OPEN",
            104.30,
            datetime(2024, 5, 10),
        ),
        _trade(
            "NMS:TLT",
            "iShares 20+ Year Treasury Bond ETF",
            "USD",
            "OPEN",
            93.40,
            datetime(2024, 2, 1),
        ),
        _trade(
            "NMS:SHY",
            "iShares 1-3 Year Treasury Bond ETF",
            "USD",
            "CLOSE",
            81.55,
            datetime(2024, 1, 31),
        ),
        _trade(
            "NMS:SHY",
            "iShares 1-3 Year Treasury Bond ETF",
            "USD",
            "OPEN",
            80.20,
            datetime(2023, 6, 1),
        ),
        _trade(
            "NMS:LRCX",
            "Lam Research Corporation",
            "USD",
            "OPEN",
            742.30,
            datetime(2025, 5, 15),
            datetime(2025, 6, 10),
        ),
        # Older entries are deliberately kept in: the production
        # section no longer trims on age so the preview should show
        # the full multi-year ownership history.
        _trade(
            "NMS:NVDA",
            "NVIDIA Corporation",
            "USD",
            "OPEN",
            458.20,
            datetime(2024, 8, 14),
            datetime(2024, 9, 5),
        ),
        _trade("NMS:META", "Meta Platforms, Inc.", "USD", "OPEN", 185.00, datetime(2023, 1, 12)),
        _trade("NMS:BIDU", "Baidu, Inc.", "USD", "OPEN", 128.50, datetime(2022, 11, 4)),
        _trade("NMS:FRSH", "Freshworks Inc.", "USD", "OPEN", 18.20, datetime(2022, 8, 5)),
        _trade("NMS:FRSH", "Freshworks Inc.", "USD", "CLOSE", 12.60, datetime(2023, 6, 9)),
    ]
    return sorted(events, key=lambda e: (e["end_date"], e["start_date"]), reverse=True)


def render(out_dir: Path) -> Path:
    """Render the page + companion artifacts into ``out_dir``.

    Returns the path to the generated ``index.html`` so callers can
    print or open it. ``Webpage.save`` now accepts an explicit
    ``output_dir`` so we no longer need to ``chdir`` for the duration
    of the render; the previous CWD juggling left a brief window
    where exceptions in the renderer could leak the wrong CWD into
    sibling tests / subsequent commands."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = _build_dataset()

    # Bypass HTTP HEAD probes: instead of hitting Pages, we resolve
    # each ticker against the repo's local ``logos/`` directory and
    # build the URL with the matching extension. Same fallback to
    # ``courage.png`` as production when no logo is on file. The stub
    # is now injected via the constructor's ``logo_cache=`` keyword
    # rather than mutated onto the class, so re-rendering the preview
    # twice in the same interpreter no longer leaves the renderer in
    # a half-stubbed state for any sibling import.
    extension_map = _build_logo_extension_map()
    stub_resolver = _StubLogoCache(extension_map, _REPO_LOGOS_DIR)

    page = Webpage(logo_cache=stub_resolver)
    page.add_return(data["total_return"], data["benchmarks"])
    page.add_allocations(data["allocation"], data["top_10"])
    for h in data["current"]:
        page.add_holding(h)
    for h in data["current_fixed_income"]:
        page.add_holding(h)
    for h in data["historical"]:
        page.add_holding(h)
    for h in data["historical_fixed_income"]:
        page.add_holding(h)
    page.add_trades(data["trades"])
    page.save(out_dir)
    return out_dir / "index.html"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the JG Investing page locally with synthetic data."
    )
    parser.add_argument(
        "--out",
        default="preview",
        help=(
            "Directory to write artifacts into "
            "(index.html, og-image.png, sitemap.xml, robots.txt). "
            "Default: ./preview/"
        ),
    )
    parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="Open the rendered index.html in the default browser.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe the output directory before rendering.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)

    index_path = render(out_dir)
    print(f"Wrote {index_path}")
    for name in ("og-image.png", "sitemap.xml", "robots.txt"):
        sibling = out_dir / name
        if sibling.exists():
            print(f"      {sibling}")

    if args.open_browser:
        webbrowser.open(index_path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
