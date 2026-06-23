"""Playwright smoke tests for client-side interactions and accessibility.

Renders the synthetic preview page (``scripts/preview.py``) and
exercises the inline scripts the production build ships. Requires
Chromium (``playwright install chromium``).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page, expect

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.browser


@pytest.fixture(scope="session")
def preview_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render ``index.html`` once for the whole browser test session."""
    out = tmp_path_factory.mktemp("browser_preview")
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/preview.py"), "--out", str(out)],
        check=True,
        cwd=REPO_ROOT,
    )
    html = out / "index.html"
    assert html.is_file(), "preview render did not produce index.html"
    return html


@pytest.fixture
def preview_page(page: Page, preview_index: Path) -> Page:
    page.goto(preview_index.as_uri())
    return page


def test_yearly_returns_toggle_expands_and_collapses(preview_page: Page):
    toggle = preview_page.locator(".returns-yearly__toggle")
    table = preview_page.locator("table.returns-yearly__table")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")

    toggle.click()
    expect(table).to_have_attribute("data-expanded", "true")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_contain_text("Show fewer years")

    toggle.click()
    expect(table).not_to_have_attribute("data-expanded", "true")
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_holdings_sort_reorders_current_list(preview_page: Page):
    list_el = preview_page.locator('[data-holdings-list="current"]')
    ticker_btn = preview_page.locator(
        '[data-holdings-sort="current"] .holdings__sort-btn[data-holdings-sort-key="ticker"]'
    )
    expect(list_el).to_be_visible()
    before = list_el.locator(".holding").evaluate_all(
        "els => els.map(el => el.getAttribute('data-sort-ticker'))"
    )
    assert len(before) >= 2

    ticker_btn.click()
    after = list_el.locator(".holding").evaluate_all(
        "els => els.map(el => el.getAttribute('data-sort-ticker'))"
    )
    assert after != before
    assert after == sorted(before)


def test_trades_sort_toggles_date_direction(preview_page: Page):
    date_header = preview_page.locator('th[data-sort-key="date"]')
    # Boot applies the default newest-first sort on the date column.
    expect(date_header).to_have_attribute("aria-sort", "descending")

    sort_btn = date_header.locator(".trades__sort")
    sort_btn.click()
    expect(date_header).to_have_attribute("aria-sort", "ascending")

    sort_btn.click()
    expect(date_header).to_have_attribute("aria-sort", "descending")


def test_treemap_link_expands_collapsed_holdings_and_scrolls(preview_page: Page):
    list_el = preview_page.locator('[data-holdings-list="current"]')
    toggle = preview_page.locator('[data-holdings-toggle="current"]')
    expect(toggle).to_be_visible()
    expect(list_el).not_to_have_attribute("data-expanded", "true")

    target = preview_page.evaluate(
        """() => {
            const list = document.querySelector('[data-holdings-list="current"]');
            if (!list) return null;
            const holdings = list.querySelectorAll('.holding');
            for (let i = 0; i < holdings.length; i++) {
                const holding = holdings[i];
                if (getComputedStyle(holding).display === 'none') {
                    const link = document.querySelector(
                        '.treemap a[href="#' + holding.id + '"]'
                    );
                    if (link) return holding.id;
                }
            }
            return null;
        }"""
    )
    assert target, "preview needs a treemap tile for a collapsed holding"

    link = preview_page.locator(f'.treemap a[href="#{target}"]')
    expect(link).to_be_visible()
    link.click()

    expect(list_el).to_have_attribute("data-expanded", "true")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_contain_text("Show fewer holdings")
    preview_page.wait_for_function(
        """(id) => {
            const el = document.getElementById(id);
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.top >= 0 && r.top < window.innerHeight * 0.75;
        }""",
        arg=target,
        timeout=3000,
    )


def test_nav_scroll_sets_hash_on_section_link(preview_page: Page):
    link = preview_page.locator('nav.site-nav a[href="#current"]')
    expect(link).to_be_visible()
    link.click()
    expect(preview_page).to_have_url(re.compile(r"#current$"))
    preview_page.wait_for_function(
        """() => {
            const el = document.getElementById('current');
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.top >= 0 && r.top < window.innerHeight * 0.75;
        }""",
        timeout=3000,
    )


def test_hash_clear_strips_hash_after_user_scroll(preview_page: Page):
    preview_page.goto(f"{preview_page.url}#performance")
    expect(preview_page).to_have_url(re.compile(r"#performance$"))
    preview_page.evaluate(
        "window.dispatchEvent(new WheelEvent('wheel', {bubbles: true, cancelable: true}))"
    )
    expect(preview_page).not_to_have_url(re.compile(r"#"))


def test_return_chart_shows_hover_on_pointer_move(preview_page: Page):
    plot = preview_page.locator(".return-chart__plot").first
    expect(plot).to_be_visible()
    box = plot.bounding_box()
    assert box is not None
    preview_page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    hover = plot.locator(".return-chart__hover")
    expect(hover).to_have_class(re.compile(r"\bis-active\b"))


def test_ticker_marquee_duplicates_logos_and_animates(preview_page: Page):
    track = preview_page.locator(".ticker__track")
    expect(track).to_be_visible()
    logo_count = track.locator(".ticker__logo").count()
    assert logo_count >= 4
    before = track.evaluate("el => getComputedStyle(el).transform")
    preview_page.wait_for_timeout(400)
    after = track.evaluate("el => getComputedStyle(el).transform")
    assert before != after


# Baseline violations tracked as design debt (nav muted-link contrast).
_BASELINE_A11Y_RULES = frozenset({"color-contrast"})


def test_preview_has_no_unexpected_a11y_violations(preview_page: Page):
    axe = Axe()
    results = axe.run(preview_page)
    violations = results.response.get("violations", [])
    serious = [
        v
        for v in violations
        if v.get("impact") in {"serious", "critical"} and v.get("id") not in _BASELINE_A11Y_RULES
    ]
    assert not serious, _format_violations(serious)


def _format_violations(violations: list[dict]) -> str:
    lines: list[str] = []
    for violation in violations:
        rule = violation.get("id", "?")
        impact = violation.get("impact", "?")
        help_text = violation.get("help", "")
        nodes = violation.get("nodes", [])
        targets = ", ".join(str(node.get("target", "?")) for node in nodes[:3])
        lines.append(f"- [{impact}] {rule}: {help_text} ({targets})")
    return "Accessibility violations:\n" + "\n".join(lines)
