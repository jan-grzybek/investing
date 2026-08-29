"""Unit tests for ``scripts/tighten_logos.py`` helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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


class TestSvgAspectParsing:
    """Aspect ratio drives the OG card's equal-area logo strip.

    A logo whose proportions cannot be read falls back to a 3:1
    wordmark default rather than being forced into a square cell, so
    the parser has to fail cleanly rather than raise.
    """

    def test_viewbox_is_preferred(self):
        from investing.logos import _parse_svg_aspect_ratio

        assert _parse_svg_aspect_ratio('<svg viewBox="0 0 300 100"/>') == pytest.approx(3.0)

    def test_width_height_attributes_are_the_fallback(self):
        from investing.logos import _parse_svg_aspect_ratio

        assert _parse_svg_aspect_ratio('<svg width="200" height="50"/>') == pytest.approx(4.0)

    def test_a_short_viewbox_falls_through_to_width_height(self):
        from investing.logos import _parse_svg_aspect_ratio

        svg = '<svg viewBox="0 0 300" width="200" height="50"/>'
        assert _parse_svg_aspect_ratio(svg) == pytest.approx(4.0)

    def test_a_zero_dimension_is_not_a_ratio(self):
        from investing.logos import _parse_svg_aspect_ratio

        assert _parse_svg_aspect_ratio('<svg width="0" height="50"/>') is None
        assert _parse_svg_aspect_ratio('<svg viewBox="0 0 0 100"/>') is None

    def test_a_non_numeric_viewbox_falls_through(self):
        from investing.logos import _parse_svg_aspect_ratio

        assert _parse_svg_aspect_ratio('<svg viewBox="a b c d"/>') is None

    def test_no_dimensions_at_all_returns_none(self):
        from investing.logos import _parse_svg_aspect_ratio

        assert _parse_svg_aspect_ratio("<svg/>") is None


def test_a_malformed_numeric_dimension_is_not_a_ratio():
    """``[\\d.]+`` matches "1.2.3", which ``float`` then rejects."""
    from investing.logos import _parse_svg_aspect_ratio

    assert _parse_svg_aspect_ratio('<svg width="1.2.3" height="4"/>') is None
