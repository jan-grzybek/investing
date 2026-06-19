"""Unit tests for ``scripts/tighten_logos.py`` helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tighten_logos",
    _REPO_ROOT / "scripts" / "tighten_logos.py",
)
assert _spec and _spec.loader
tighten_logos = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tighten_logos)


def test_format_viewbox_trims_trailing_zeros():
    assert tighten_logos._format_viewbox((0.0, 0.0, 10.5, 20.0)) == "0 0 10.5 20"


def test_parse_viewbox_reads_four_floats():
    tag = '<svg viewBox="1 2 3.5 4" xmlns="http://www.w3.org/2000/svg">'
    assert tighten_logos._parse_viewbox(tag) == (1.0, 2.0, 3.5, 4.0)


def test_parse_width_height_reads_numeric_root_attrs():
    tag = '<svg width="100" height="50" xmlns="http://www.w3.org/2000/svg">'
    assert tighten_logos._parse_width_height(tag) == (100.0, 50.0)


def test_crop_svg_tightens_viewbox(tmp_path):
    svg = tmp_path / "box.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect x="40" y="40" width="20" height="20" fill="black"/>'
        "</svg>",
        encoding="utf-8",
    )
    cropped = tighten_logos._crop_svg(svg)
    assert cropped is not None
    text = cropped.decode("utf-8")
    assert 'viewBox="' in text
    assert "width=" not in text.split(">", 1)[0]
    assert "height=" not in text.split(">", 1)[0]


def test_committed_tight_logos_match_fresh_build():
    outputs = tighten_logos._expected_outputs()
    drift = [
        path for path, body in outputs.items() if not path.exists() or path.read_bytes() != body
    ]
    assert not drift, f"stale tight logos: {[p.relative_to(_REPO_ROOT) for p in drift]}"
