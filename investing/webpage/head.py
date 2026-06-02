"""``<head>`` builder + Content-Security-Policy assembler.

Keeping the head's hash-based CSP in its own file makes it auditable
without scrolling past 1900 lines of section renderers; any change
to an inline script / style payload must update a hash here, so the
"what is allowed to execute on the page" surface lives in one
self-contained module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..assets import (
    _HASH_CLEAR_SCRIPT,
    _HOLDINGS_SORT_SCRIPT,
    _NAV_SCROLL_SCRIPT,
    _PAGE_STYLES,
    _RETURN_CHART_SCRIPT,
    _TRADES_SORT_SCRIPT,
)
from ..formatting import _sha256_b64
from ..safehtml import SafeHtml, escape


@dataclass(frozen=True)
class SiteMeta:
    """The handful of constants the ``<head>`` builder needs.

    Bundled into a frozen dataclass so the two consumers
    (:func:`build_head` and :func:`build_jsonld`) share a single
    obvious contract rather than each taking the same five
    positional arguments.
    """

    title: str  # Long-form site title (used in <h1>, og:site_name, JSON-LD ``name``).
    seo_title: str  # SERP-friendly short title (used in <title> + Twitter / OG title meta).
    description: str  # ~155-char meta description (description meta + Twitter / OG).
    url: str  # Canonical site URL.
    social_image: str  # Absolute URL of the rendered OG image.


# Cloudflare Web Analytics beacon. The token is a *write-only* identifier
# (it grants the bearer permission to push pageviews into the dashboard
# but reveals nothing about the dataset on its own), so committing it
# in source is intentional. The matching CSP allowance lives in
# :func:`build_csp` below; both must move together if a third-party
# analytics provider is ever swapped.
_CLOUDFLARE_ANALYTICS_TAG: SafeHtml = SafeHtml(
    "<!-- Cloudflare Web Analytics -->"
    "<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
    'data-cf-beacon=\'{"token": "8f450af27c86439fb0e9ab0031c76d6e"}\'></script>'
    "<!-- End Cloudflare Web Analytics -->"
)


def build_analytics_tag() -> SafeHtml:
    """Return the Cloudflare Web Analytics beacon ``<script>`` tag.

    Centralised so the only edit point for "what analytics does the
    page run" is here, alongside the CSP that whitelists the same
    domain. Returning :class:`SafeHtml` lets the renderer append the
    fragment without an extra escape pass.
    """
    return _CLOUDFLARE_ANALYTICS_TAG


def build_jsonld(meta: SiteMeta) -> SafeHtml:
    """Schema.org structured data identifying the site and its author.

    Emits a ``WebSite`` graph with a nested ``Person`` so search
    engines can attribute the portfolio to Jan Grzybek and use the
    description/title in knowledge-graph cards. The output is
    JSON-encoded with ``ensure_ascii=False`` so unicode (e.g. en-
    dashes) round-trips cleanly, and ``</`` is escaped so the payload
    can't accidentally close the surrounding ``<script>``.
    """
    payload = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": meta.title,
        "alternateName": "JG Investing",
        "url": meta.url,
        "description": meta.description,
        "inLanguage": "en",
        "author": {
            "@type": "Person",
            "name": "Jan Grzybek",
            "url": meta.url,
        },
    }
    return SafeHtml(json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))


def build_csp(jsonld: SafeHtml) -> SafeHtml:
    """Construct the page's Content-Security-Policy.

    Inline ``<script>`` / ``<style>`` payloads are pinned by their
    SHA-256 hashes so ``unsafe-inline`` never needs to appear on the
    ``script-src`` directive. CSP3 splits inline styles into element
    vs attribute scopes (``style-src-elem`` / ``style-src-attr``)
    because the renderer emits both: one inline ``<style>`` block
    (hashable) plus per-element ``style="..."`` attributes for bar
    widths / delta positions (programmatic and not hashable).
    ``style-src`` stays as the CSP2 fallback for browsers that
    don't understand the CSP3 directives.
    """
    style_hash = _sha256_b64(_PAGE_STYLES)
    jsonld_hash = _sha256_b64(jsonld)
    hash_clear_hash = _sha256_b64(_HASH_CLEAR_SCRIPT)
    nav_scroll_hash = _sha256_b64(_NAV_SCROLL_SCRIPT)
    return_chart_hash = _sha256_b64(_RETURN_CHART_SCRIPT)
    trades_sort_hash = _sha256_b64(_TRADES_SORT_SCRIPT)
    holdings_sort_hash = _sha256_b64(_HOLDINGS_SORT_SCRIPT)
    return SafeHtml(
        "default-src 'self'; "
        f"script-src 'self' 'sha256-{jsonld_hash}' "
        f"'sha256-{hash_clear_hash}' "
        f"'sha256-{nav_scroll_hash}' "
        f"'sha256-{return_chart_hash}' "
        f"'sha256-{trades_sort_hash}' "
        f"'sha256-{holdings_sort_hash}' "
        "https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline'; "
        f"style-src-elem 'self' 'sha256-{style_hash}'; "
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' https: data:; "
        "connect-src 'self' https://cloudflareinsights.com; "
        "font-src 'self'; "
        "base-uri 'self'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


def build_head(meta: SiteMeta) -> SafeHtml:
    """Render the page's ``<head>`` block.

    Pulls together the SEO / OG / Twitter / canonical / theme-color
    meta tags, the CSP, the inline script payloads (each pinned in
    CSP via its SHA-256), the JSON-LD island, and the stylesheet.
    Returns a :class:`SafeHtml` so the caller can append it to the
    document without an extra escape pass.
    """
    title = escape(meta.seo_title)
    desc = escape(meta.description)
    site = escape(meta.title)
    url = escape(meta.url)
    image = escape(meta.social_image)
    jsonld_str = build_jsonld(meta)
    csp = build_csp(jsonld_str)
    return SafeHtml(
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{title}</title>\n"
        f'<meta name="description" content="{desc}">\n'
        '<meta name="author" content="Jan Grzybek">\n'
        '<meta name="robots" content="index,follow,max-image-preview:large">\n'
        f'<link rel="canonical" href="{url}">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="referrer" content="strict-origin-when-cross-origin">\n'
        '<meta name="theme-color" content="#f8fafc" media="(prefers-color-scheme: light)">\n'
        '<meta name="theme-color" content="#071923" media="(prefers-color-scheme: dark)">\n'
        '<link rel="icon" type="image/svg+xml" href="favicon.svg">\n'
        '<link rel="icon" type="image/png" href="favicon.png">\n'
        '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
        '<link rel="icon" href="favicon.ico">\n'
        f'<meta property="og:title" content="{title}">\n'
        f'<meta property="og:description" content="{desc}">\n'
        f'<meta property="og:image" content="{image}">\n'
        '<meta property="og:image:type" content="image/png">\n'
        '<meta property="og:image:width" content="1200">\n'
        '<meta property="og:image:height" content="630">\n'
        f'<meta property="og:image:alt" content="{title}">\n'
        f'<meta property="og:url" content="{url}">\n'
        '<meta property="og:type" content="website">\n'
        '<meta property="og:locale" content="en_US">\n'
        f'<meta property="og:site_name" content="{site}">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{title}">\n'
        f'<meta name="twitter:description" content="{desc}">\n'
        f'<meta name="twitter:image" content="{image}">\n'
        f'<meta name="twitter:image:alt" content="{title}">\n'
        f'<script type="application/ld+json">{jsonld_str}</script>\n'
        f"<script>{_HASH_CLEAR_SCRIPT}</script>\n"
        f"<script>{_NAV_SCROLL_SCRIPT}</script>\n"
        f"<script>{_RETURN_CHART_SCRIPT}</script>\n"
        f"<script>{_TRADES_SORT_SCRIPT}</script>\n"
        f"<script>{_HOLDINGS_SORT_SCRIPT}</script>\n"
        f"<style>{_PAGE_STYLES}</style>\n"
        "</head>"
    )
