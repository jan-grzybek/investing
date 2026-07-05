"""Tests for the public-site staging helper."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from investing.webpage.head import SiteMeta, build_head

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "stage_site",
    _REPO_ROOT / "scripts" / "stage_site.py",
)
assert _spec and _spec.loader
stage_site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stage_site)


def test_collect_skips_assets_src(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "page.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "assets" / "src" / "css").mkdir(parents=True)
    (tmp_path / "assets" / "src" / "css" / "00-base.css").write_text(":root{}", encoding="utf-8")
    mapping = stage_site._collect_source_paths(tmp_path)
    assert Path("index.html") in mapping
    assert Path("assets/page.css") in mapping
    assert Path("assets/src/css/00-base.css") not in mapping


def test_write_staging_includes_nojekyll(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    out = tmp_path / "site"
    stage_site._write_staging(tmp_path, out)
    assert (out / ".nojekyll").is_file()
    assert (out / "index.html").read_text(encoding="utf-8") == "<html></html>"


def test_every_head_icon_reference_is_staged():
    """Regression guard for the "no favicon in search results" bug.

    ``build_head`` emits a root-relative ``<link>`` href for each
    favicon variant; every one must be published by the stager or it
    404s on GitHub Pages, leaving Google with no crawlable icon and no
    logo beside the result. Cross-check the hrefs the head actually
    emits against what ``stage_site`` ships so a new icon link can't be
    added to the head without also teaching the stager to publish it.
    """
    head = str(
        build_head(
            SiteMeta(
                title="Site",
                seo_title="Site",
                description="Desc",
                url="https://example.test/",
                social_image="https://example.test/og-image.png",
            )
        )
    )
    href_values = set(re.findall(r'<link[^>]+href="([^"]+)"', head))
    # Absolute URLs (e.g. rel="canonical") are not our files to ship.
    local_icons = {h for h in href_values if not h.startswith(("http://", "https://", "//"))}
    assert local_icons, "expected build_head to emit at least one local <link> icon"

    shipped = set(stage_site._ROOT_ARTIFACTS) | set(stage_site._STATIC_ROOT_FILES)
    missing = local_icons - shipped
    assert not missing, f"icons referenced in <head> but never staged: {sorted(missing)}"

    # And the committed source files must actually exist to be copied.
    for name in stage_site._STATIC_ROOT_FILES:
        assert (_REPO_ROOT / name).is_file(), f"missing committed asset: {name}"
