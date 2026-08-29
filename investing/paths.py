"""Repo paths, site URL configuration, and the small ``_read_asset``
helper used to load inline CSS / JS payloads from ``assets/``.

The site URL is env-configurable (``INVESTING_SITE_URL``) so a fork or
staging deployment can be rendered without patching the source. Every
URL the page emits (canonical, OG image, sitemap pointer, logos
mirror) derives from this single value, so a single ``export
INVESTING_SITE_URL=https://staging.example.com/`` repoints the entire
build.
"""

from __future__ import annotations

import os

# Public surface of this module. The leading-underscore entries are
# imported by sibling modules (``investing.webpage.og_image`` /
# ``investing.logos`` reach into ``_REPO_LOGOS_DIR``); ``__all__`` is
# the canonical opt-in that tells CodeQL's
# ``py/unused-global-variable`` query they're cross-module exports
# rather than module-local bindings the leading underscore would
# otherwise imply.
__all__ = [
    "COURAGE_LOGO",
    "LOGOS_ADDRESS",
    "LOGOS_PROBE_BASE",
    "LOGO_EXTENSIONS",
    "SITE_DISPLAY",
    "SITE_URL",
    "SOCIAL_IMAGE",
    "_MARKET_DATA_DIR",
    "_POSITION_GROUPS_PATH",
    "_REPO_LOGOS_DIR",
    "_REPO_LOGOS_SOURCE_DIR",
    "_SECTOR_OVERRIDES_PATH",
    "_read_asset",
]

# ---------------------------------------------------------------------------
# Site URL configuration
# ---------------------------------------------------------------------------

# Env var the operator sets to override the canonical site URL. ``None``
# (or empty) falls back to :data:`_DEFAULT_SITE_URL`, which matches the
# production deployment so existing forks see no behavioural change.
_SITE_URL_ENV = "INVESTING_SITE_URL"
_DEFAULT_SITE_URL = "https://jan-grzybek.github.io/investing/"


def _resolve_site_url() -> str:
    """Return the active site URL, always trailing with a slash.

    Read at import time so the rest of the module can declare derived
    constants in the natural ``CONSTANT = expression`` shape. The env
    contract is process-scoped: an operator who wants to point the
    build at a staging URL sets ``INVESTING_SITE_URL`` before invoking
    ``python -m investing``.
    """
    raw = os.environ.get(_SITE_URL_ENV) or _DEFAULT_SITE_URL
    return raw if raw.endswith("/") else raw + "/"


# Canonical site URL surfaced as ``<link rel="canonical">``, ``og:url``,
# the sitemap loc entries, etc. Trailing slash is part of the contract:
# downstream string concatenation appends path segments (``logos/`` /
# ``og-image.png`` / ``sitemap.xml``) without a separator.
SITE_URL = _resolve_site_url()


# Logos are referenced *relatively*. They ship in the same Pages
# artifact as ``index.html`` (``scripts/stage_site.py`` copies
# ``logos/tight`` verbatim), so a root-relative-free path resolves
# against whatever origin is serving the page.
#
# They used to be absolute, built from :data:`SITE_URL`, which made
# every rendered page permanently bound to the production host. The
# concrete cost was the local preview: ``scripts/preview.py`` exists to
# "inspect the rendered HTML in a browser without touching production"
# and then emitted ``<img src="https://jan-grzybek.github.io/...">``
# for every logo, so a newly added logo rendered as a broken image
# until after it had been deployed -- the one workflow the preview is
# meant to support. Relative paths also let the CSP tighten ``img-src``
# from ``https:`` (any host on the internet) to ``'self'``.
#
# ``tight/`` rather than the raw source dump: every served logo is a
# viewBox-cropped variant produced by ``scripts/tighten_logos.py``,
# while the hand-curated originals stay under ``logos/`` as the design
# source of truth. Cropping removes the SVG-author-introduced padding
# around each mark, which is the single biggest driver of perceived
# size disparity in the holdings logo cell (a centred icon in a square
# viewBox was reading much smaller than an edge-to-edge wordmark at the
# same bounding box). See the ``regenerate-logos`` workflow and the
# matching pre-commit hook for the contract that keeps the tight mirror
# in sync with its sources.
LOGOS_ADDRESS = "logos/tight/"
COURAGE_LOGO = LOGOS_ADDRESS + "courage.png"

# Absolute counterpart, used only for the HTTP HEAD existence probe in
# :mod:`investing.logos`. That probe asks the *deployed* site whether a
# logo is live, which needs a real URL; what it returns for the page to
# embed is the relative form above.
LOGOS_PROBE_BASE = SITE_URL + "logos/tight/"

# Stays absolute: ``og:image`` / ``twitter:image`` are consumed by
# crawlers that never resolve against the page's own base URL.
SOCIAL_IMAGE = SITE_URL + "og-image.png"

# Host + path tail shown in human-readable contexts (OG image foot
# caption). Strips the scheme so the rendered text reads as a
# domain rather than a URL.
SITE_DISPLAY = SITE_URL.removeprefix("https://").removeprefix("http://").rstrip("/")


LOGO_EXTENSIONS = (".svg", ".png", ".jpg")


# Repo root: the directory holding the ``investing/`` package, the
# ``assets/`` source directory, and the ``logos/`` mirror. Resolved
# relative to this file so the CI build and the local preview both
# work regardless of the caller's CWD.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Local mirror of ``LOGOS_ADDRESS`` -- the same files served at the URL
# above live at the repo root and ship as part of the Pages artifact.
# The OG image renderer rasterises logos for the top-10 strip and
# reads them straight from disk so it doesn't depend on the previous
# deploy being reachable.
#
# Two separate directories so the served crop and the design source
# never drift:
#
#   * ``_REPO_LOGOS_SOURCE_DIR`` (``logos/``) holds the hand-curated
#     source SVGs (and the small handful of raster fallbacks). This
#     is what designers / contributors edit; only
#     ``scripts/tighten_logos.py`` and ``regenerate-logos.yml`` read
#     it.
#   * ``_REPO_LOGOS_DIR`` (``logos/tight/``) is the served mirror:
#     every SVG has been viewBox-cropped to the visible silhouette
#     and every non-SVG copied through verbatim. Both the renderer
#     and the OG image pipeline read from here.
_REPO_LOGOS_SOURCE_DIR = os.path.join(_REPO_DIR, "logos")
_REPO_LOGOS_DIR = os.path.join(_REPO_LOGOS_SOURCE_DIR, "tight")


# Path to the maintainer-curated TOML mapping that overrides
# yfinance's ``info["sector"]`` field for tickers where the upstream
# value is empty or missing. See
# :mod:`investing.sector_overrides` for the loader / schema / fallback
# semantics; the file itself is hand-edited and ships as part of the
# repo (the production build reads it from the same checkout it
# renders against). Kept here rather than inlined into
# ``sector_overrides.py`` so the repo-root path lives next to the
# other repo-relative constants and a fork that wants to repoint the
# data only has to edit one module.
_SECTOR_OVERRIDES_PATH = os.path.join(_REPO_DIR, "sector_overrides.toml")

# Maintainer-curated map of several tickers onto one economic position
# (a primary listing plus its depositary receipts / secondary
# listings). Consumed by ``investing.position_groups``; see that
# module for the file's schema.
_POSITION_GROUPS_PATH = os.path.join(_REPO_DIR, "position_groups.toml")

# Committed yfinance snapshot tree (splits / dividends / FX / history).
# Override with ``INVESTING_MARKET_DATA_DIR``; disable persistence with
# ``INVESTING_MARKET_DATA_DISABLE=1``; skip disk writes with
# ``INVESTING_MARKET_DATA_PERSIST=0`` (read/merge only).
_MARKET_DATA_DIR = os.path.join(_REPO_DIR, "market_data")


# Source-of-truth directory for the inline CSS / JS payloads embedded in
# the rendered page. Each constant below loads its content from a file
# under ``assets/`` so editors can lint / format the real CSS and JS
# rather than the equivalent Python string literal.
_ASSETS_DIR = os.path.join(_REPO_DIR, "assets")


def _read_asset(name: str) -> str:
    """Read an ``assets/<name>`` file as UTF-8 text."""
    with open(os.path.join(_ASSETS_DIR, name), encoding="utf-8") as f:
        return f.read()
