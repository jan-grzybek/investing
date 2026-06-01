"""Sibling files emitted alongside ``index.html``: ``sitemap.xml`` and ``robots.txt``."""

from __future__ import annotations

from ..clock import NowFn
from ..safehtml import SafeHtml, render_template


def write_sitemap(site_url: str, *, now: NowFn) -> None:
    """Emit a single-URL ``sitemap.xml`` next to ``index.html``.

    Search engines use ``<lastmod>`` as a hint to recrawl pages whose
    content has changed; bumping it on every regeneration means new
    holdings/returns surface in indexes faster than they otherwise
    would on a static GitHub Pages site.
    """
    last_mod = now().strftime("%Y-%m-%d")
    sitemap: SafeHtml = render_template(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        "    <loc>{url}</loc>\n"
        "    <lastmod>{last_mod}</lastmod>\n"
        "    <changefreq>daily</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "</urlset>\n",
        url=site_url,
        last_mod=last_mod,
    )
    with open("sitemap.xml", "w") as f:
        f.write(sitemap)


def write_robots_txt(site_url: str) -> None:
    """Emit ``robots.txt`` alongside ``index.html``.

    Generating this at build time (rather than committing a static
    file) keeps the canonical URL and sitemap location in sync with
    ``Webpage.SITE_URL`` -- a single source of truth -- and lines up
    with how ``index.html``, ``sitemap.xml`` and ``og-image.png`` are
    also produced.
    """
    sitemap_url = f"{site_url.rstrip('/')}/sitemap.xml"
    body = (
        "# Allow all well-behaved crawlers to index everything.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {sitemap_url}\n"
    )
    with open("robots.txt", "w") as f:
        f.write(body)
