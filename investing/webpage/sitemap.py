"""Sibling files emitted alongside ``index.html``: ``sitemap.xml`` and ``robots.txt``."""

from __future__ import annotations

from pathlib import Path

from ..clock import NowFn
from ..log import logger
from ..safehtml import SafeHtml, render_template


def _write_if_changed(path: Path, body: str) -> bool:
    """Skip the write when ``body`` already matches the on-disk file.

    Companion to the ``_write_if_changed`` in :mod:`investing.webpage._page`
    (kept local to avoid an import cycle through ``_page``). The
    rationale matches: ``robots.txt`` is fully deterministic from
    ``SITE_URL`` so the comparison short-circuits on every run; the
    sitemap embeds a daily ``<lastmod>`` so it still rewrites once a
    calendar day. Returns ``True`` when a write occurred.
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


def _resolve_output_dir(output_dir: Path | None) -> Path:
    """Resolve ``output_dir`` against ``Path.cwd()`` when unspecified.

    Keeps the historical CWD-based behaviour for tests that use the
    ``chdir_tmp`` fixture while letting fresh callers (production
    pipeline, preview script) pass an explicit destination so the
    artefact write doesn't depend on process-level state.
    """
    return output_dir if output_dir is not None else Path.cwd()


def write_sitemap(site_url: str, output_dir: Path | None = None, *, now: NowFn) -> None:
    """Emit a single-URL ``sitemap.xml`` into ``output_dir``.

    Search engines use ``<lastmod>`` as a hint to recrawl pages whose
    content has changed; bumping it on every regeneration means new
    holdings/returns surface in indexes faster than they otherwise
    would on a static GitHub Pages site. ``output_dir`` defaults to
    the current working directory so the legacy ``chdir_tmp``-based
    test paths keep working unchanged.
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
    _write_if_changed(_resolve_output_dir(output_dir) / "sitemap.xml", sitemap)


def write_robots_txt(site_url: str, output_dir: Path | None = None) -> None:
    """Emit ``robots.txt`` into ``output_dir``.

    Generating this at build time (rather than committing a static
    file) keeps the canonical URL and sitemap location in sync with
    ``Webpage.SITE_URL`` -- a single source of truth -- and lines up
    with how ``index.html``, ``sitemap.xml`` and ``og-image.png`` are
    also produced. ``output_dir`` defaults to the current working
    directory to preserve the historical CWD-based contract.
    """
    sitemap_url = f"{site_url.rstrip('/')}/sitemap.xml"
    body = (
        "# Allow all well-behaved crawlers to index everything.\n"
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {sitemap_url}\n"
    )
    _write_if_changed(_resolve_output_dir(output_dir) / "robots.txt", body)
