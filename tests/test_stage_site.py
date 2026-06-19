"""Tests for the public-site staging helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
