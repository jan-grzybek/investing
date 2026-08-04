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


def test_preview_headline_figures_survive_the_readers_arithmetic(preview_html: str):
    """The demo's own numbers have to add up.

    They used to be written down by hand and disagreed: "48.4% total,
    10.5% annualised" over seven and a half years is arithmetically
    impossible -- 48.4% compounds to 5.3% a year, and 10.5% a year
    compounds to 113%. The yearly table below it was right, because it
    is derived from ``history``, so the page contradicted itself in the
    one place a reader is most likely to check. A demo whose figures do
    not survive that check is worse than no demo.

    The fixture now derives both from the growth multiplier with the
    same formula ``performance.calc_total_return`` uses. This asserts
    the relationship on the *rendered* page, so neither the fixture nor
    the renderer can quietly drift from it.
    """
    import re

    from investing.holdings import DAYS_YEAR

    def figure(label: str) -> float:
        # Each stat is a <dt> label followed by its <dd> value.
        pattern = (
            r'hero__stat-label"[^>]*>(?:(?!</dt>).)*?'
            + re.escape(label)
            + r"(?:(?!</dt>).)*?</dt>\s*<dd[^>]*>\s*(?:<[^>]+>)?\s*(-?[\d.]+)%"
        )
        match = re.search(pattern, preview_html, re.S)
        assert match, f"no hero stat labelled {label!r}"
        return float(match.group(1))

    total = figure("Portfolio")
    annualised = figure("Annualised")

    # The inception date the page states, against the date it was built.
    since = re.search(
        r'class="hero__period"[^>]*>[^<]*<time datetime="(\d{4}-\d{2}-\d{2})"',
        preview_html,
    )
    assert since, "no inception date in the hero"
    from datetime import date, datetime

    start = datetime.strptime(since.group(1), "%Y-%m-%d").date()
    days = max((date.today() - start).days, 1)

    implied = ((1.0 + total / 100.0) ** (DAYS_YEAR / days) - 1.0) * 100.0
    # Both figures are rendered to one decimal, so allow the rounding.
    assert abs(implied - annualised) < 0.12, (
        f"{total}% over {days / DAYS_YEAR:.2f} years annualises to "
        f"{implied:.2f}%, but the page says {annualised}%"
    )


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
    for section_id in ("performance", "allocation", "holdings", "closed", "activity", "method"):
        assert soup.find(id=section_id) is not None, f"missing #{section_id}"
    nav = soup.find("nav", class_="site-nav")
    assert nav is not None
    hrefs = {a.get("href") for a in nav.find_all("a")}
    assert {"#performance", "#holdings", "#activity", "#method"} <= hrefs
