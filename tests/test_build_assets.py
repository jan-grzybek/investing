"""Unit tests for ``scripts/build_assets.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_assets",
    _REPO_ROOT / "scripts" / "build_assets.py",
)
assert _spec and _spec.loader
build_assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_assets)


def test_minify_js_wraps_iife_sources():
    source = "(function(){ var x = 1 ; })();"
    out = build_assets._minify_js(source)
    assert out.startswith("(function")
    assert "x=1" in out.replace(" ", "")


def test_concatenate_css_preserves_container_query_whitespace():
    chunks = ["@container ticker (min-width: 1px) { .x { color: red; } }"]
    out = build_assets._concatenate_css(chunks)
    assert "@container ticker (" in out


def test_build_outputs_includes_all_js_and_css():
    outputs = build_assets._build_outputs()
    js_names = {p.name for p in outputs if p.suffix == ".js"}
    assert "holdings_sort.js" in js_names
    assert "yearly_returns.js" in js_names
    assert _REPO_ROOT / "assets" / "page.css" in outputs


def test_committed_assets_match_fresh_build():
    outputs = build_assets._build_outputs()
    drift = [
        path
        for path, body in outputs.items()
        if not path.exists() or path.read_text(encoding="utf-8") != body
    ]
    assert not drift, f"stale assets: {[p.relative_to(_REPO_ROOT) for p in drift]}"


def test_check_mode_reports_drift(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    src_js = root / "assets" / "src" / "js"
    out_dir = root / "assets"
    src_js.mkdir(parents=True)
    (src_js / "probe.js").write_text("(function(){})();\n", encoding="utf-8")
    (out_dir / "probe.js").write_text("stale\n", encoding="utf-8")

    monkeypatch.setattr(build_assets, "REPO_ROOT", root)
    monkeypatch.setattr(build_assets, "SRC_JS_DIR", src_js)
    monkeypatch.setattr(build_assets, "SRC_CSS_DIR", root / "assets" / "src" / "css")
    monkeypatch.setattr(build_assets, "OUT_DIR", out_dir)
    monkeypatch.setattr(build_assets.sys, "argv", ["build_assets.py", "--check"])

    assert build_assets.main() == 1
