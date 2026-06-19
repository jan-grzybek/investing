"""HTML parsing helpers for structural assertions in tests."""

from __future__ import annotations

from bs4 import BeautifulSoup


def parse_html(html: str) -> BeautifulSoup:
    """Parse *html* with the html5lib tree builder (strict, spec-aligned)."""
    return BeautifulSoup(html, "html5lib")


def assert_single_element(soup: BeautifulSoup, tag: str) -> None:
    """Assert the document contains exactly one *tag* element."""
    found = soup.find_all(tag)
    assert len(found) == 1, f"expected one <{tag}>, found {len(found)}"
