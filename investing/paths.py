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
LOGOS_ADDRESS = SITE_URL + "logos/"
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
_REPO_LOGOS_DIR = os.path.join(_REPO_DIR, "logos")


# Source-of-truth directory for the inline CSS / JS payloads embedded in
# the rendered page. Each constant below loads its content from a file
# under ``assets/`` so editors can lint / format the real CSS and JS
# rather than the equivalent Python string literal.
_ASSETS_DIR = os.path.join(_REPO_DIR, "assets")


def _read_asset(name: str) -> str:
    """Read an ``assets/<name>`` file as UTF-8 text."""
    with open(os.path.join(_ASSETS_DIR, name), encoding="utf-8") as f:
        return f.read()
