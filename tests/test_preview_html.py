"""Structural validation of the synthetic preview page."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import html5lib
import pytest

from tests._html_helpers import assert_single_element, parse_html

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def preview_html(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("preview_html") / "index.html"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/preview.py"), "--out", str(out.parent)],
        check=True,
        cwd=REPO_ROOT,
    )
    return out.read_text(encoding="utf-8")


def test_preview_parses_under_strict_html5lib(preview_html: str):
    parser = html5lib.HTMLParser(strict=True)
    parser.parse(preview_html)


def test_preview_has_required_document_skeleton(preview_html: str):
    assert preview_html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in preview_html
    soup = parse_html(preview_html)
    assert_single_element(soup, "html")
    assert_single_element(soup, "head")
    assert_single_element(soup, "body")
    assert soup.find("main", id="main-content") is not None


def test_preview_sections_are_structurally_wired(preview_html: str):
    soup = parse_html(preview_html)
    for section_id in ("performance", "current", "trades"):
        assert soup.find(id=section_id) is not None, f"missing #{section_id}"
    nav = soup.find("nav", class_="site-nav")
    assert nav is not None
    hrefs = {a.get("href") for a in nav.find_all("a")}
    assert "#performance" in hrefs
    assert "#current" in hrefs
