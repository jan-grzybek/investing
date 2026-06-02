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
    "LOGO_EXTENSIONS",
    "SITE_DISPLAY",
    "SITE_URL",
    "SOCIAL_IMAGE",
    "_REPO_LOGOS_DIR",
    "_REPO_LOGOS_SOURCE_DIR",
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


# Sibling artefacts deployed alongside ``index.html`` derive their
# absolute URLs from :data:`SITE_URL` so a repoint is single-source.
# ``LOGOS_ADDRESS`` points at the ``tight/`` subdirectory rather than
# at the raw source dump: every served logo is a viewBox-cropped
# variant produced by ``scripts/tighten_logos.py``, while the
# hand-curated originals stay under ``logos/`` as the design source
# of truth. Cropping removes the SVG-author-introduced padding around
# each mark, which is the single biggest driver of perceived size
# disparity in the sector treemap (a centred icon in a square viewBox
# was reading much smaller than an edge-to-edge wordmark at the same
# bounding box). See the ``regenerate-logos`` workflow and the
# matching pre-commit hook for the contract that keeps the tight
# mirror in sync with its sources.
LOGOS_ADDRESS = SITE_URL + "logos/tight/"
SOCIAL_IMAGE = SITE_URL + "og-image.png"
COURAGE_LOGO = LOGOS_ADDRESS + "courage.png"

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


# Source-of-truth directory for the inline CSS / JS payloads embedded in
# the rendered page. Each constant below loads its content from a file
# under ``assets/`` so editors can lint / format the real CSS and JS
# rather than the equivalent Python string literal.
_ASSETS_DIR = os.path.join(_REPO_DIR, "assets")


def _read_asset(name: str) -> str:
    """Read an ``assets/<name>`` file as UTF-8 text."""
    with open(os.path.join(_ASSETS_DIR, name), encoding="utf-8") as f:
        return f.read()
