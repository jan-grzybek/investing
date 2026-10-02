"""HTML parsing helpers for structural assertions in tests."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# A calendar day in any of the three forms the page writes one in:
# ISO in attributes, ``DD/MM/YYYY`` in the tables (``_fmt_date``) and
# ``Jan 1, 2019`` in prose (``_fmt_date_long``).
A_DAY = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|[A-Z][a-z]{2} \d{1,2}, \d{4}")


def parse_html(html: str) -> BeautifulSoup:
    """Parse *html* with the html5lib tree builder (strict, spec-aligned)."""
    return BeautifulSoup(html, "html5lib")


def assert_single_element(soup: BeautifulSoup, tag: str) -> None:
    """Assert the document contains exactly one *tag* element."""
    found = soup.find_all(tag)
    assert len(found) == 1, f"expected one <{tag}>, found {len(found)}"
