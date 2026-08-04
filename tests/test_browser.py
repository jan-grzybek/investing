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


def test_every_year_renders_without_a_toggle(preview_page: Page):
    # Three of seven years used to start collapsed, which hid most of
    # the evidence for the page's central claim behind a click.
    expect(preview_page.locator(".returns-yearly__toggle")).to_have_count(0)
    rows = preview_page.locator(".yearly__row")
    assert rows.count() >= 5
    for index in range(rows.count()):
        expect(rows.nth(index)).to_be_visible()


def test_holdings_sort_reorders_rows_within_their_group(preview_page: Page):
    # Sorting is per-``<tbody>``: rows reorder inside their own group,
    # so a sort can never shuffle a bond into the equity sleeve.
    table = preview_page.locator('table[data-holdings-table="open"]')
    expect(table).to_be_visible()
    equities = table.locator("tbody.holdings__section").first

    def names():
        return equities.locator(".holdings__row").evaluate_all(
            "els => els.map(el => el.getAttribute('data-sort-name'))"
        )

    groups_before = table.locator("tbody.holdings__section").evaluate_all(
        "els => els.map(el => el.querySelectorAll('.holdings__row').length)"
    )
    before = names()
    assert len(before) >= 2

    header = table.locator('th[data-sort-key="name"]')
    header.locator(".holdings__sort").click()
    after = names()
    assert after != before
    assert after == sorted(before)
    # aria-sort is the single source of truth for the sorted state.
    expect(header).to_have_attribute("aria-sort", "ascending")
    # No row crossed a group boundary.
    assert (
        table.locator("tbody.holdings__section").evaluate_all(
            "els => els.map(el => el.querySelectorAll('.holdings__row').length)"
        )
        == groups_before
    )

    header.locator(".holdings__sort").click()
    expect(header).to_have_attribute("aria-sort", "descending")
    assert names() == sorted(before, reverse=True)


def test_holdings_numeric_sort_starts_high_to_low(preview_page: Page):
    table = preview_page.locator('table[data-holdings-table="open"]')
    header = table.locator('th[data-sort-key="tsr"]')
    header.locator(".holdings__sort").click()
    expect(header).to_have_attribute("aria-sort", "descending")
    equities = table.locator("tbody.holdings__section").first
    values = equities.locator(".holdings__row").evaluate_all(
        "els => els.map(el => parseFloat(el.getAttribute('data-sort-tsr')))"
    )
    assert values == sorted(values, reverse=True)


def test_numeric_columns_align_with_their_headers(preview_page: Page):
    """Right-aligned columns have to right-align with their headers.

    A blanket ``.holdings__row td { text-align: left }`` reset once sat
    at specificity (0,1,1) in front of ``.holdings__num`` at (0,1,0),
    which silently discarded both the alignment and the 700/500 weight
    pair -- the figures rendered left-aligned at 400, out of line with
    the headers above them. Nothing about that is visible in the
    markup, and it survived several rounds of looking at screenshots,
    so it is asserted geometrically instead.
    """
    for scope in ("open", "closed"):
        table = preview_page.locator(f'table[data-holdings-table="{scope}"]')
        expect(table).to_be_visible()
        edges = table.evaluate(
            """t => {
                const R = el => {
                    const r = document.createRange();
                    r.selectNodeContents(el);
                    return Math.round(r.getBoundingClientRect().right);
                };
                const row = t.querySelector('.holdings__row');
                const out = [];
                [...t.querySelectorAll('thead th')].forEach((th, i) => {
                    const key = th.getAttribute('data-sort-key');
                    if (key !== 'tsr' && key !== 'cagr') return;
                    const btn = th.querySelector('.holdings__sort');
                    const cell = row.children[i];
                    out.push({key, header: R(btn), value: R(cell),
                              align: getComputedStyle(cell).textAlign,
                              weight: getComputedStyle(cell).fontWeight});
                });
                return out;
            }"""
        )
        assert len(edges) == 2, f"{scope}: expected a Return and an IRR column"
        for col in edges:
            assert col["align"] == "right", f"{scope}/{col['key']} is {col['align']}"
            assert abs(col["header"] - col["value"]) <= 1, (
                f"{scope}/{col['key']}: header right edge {col['header']} vs value {col['value']}"
            )
        # Return carries the emphasis, IRR is the secondary reading.
        by_key = {c["key"]: c["weight"] for c in edges}
        assert by_key["tsr"] == "700", by_key
        assert by_key["cagr"] == "500", by_key


def test_metrics_note_discloses_the_long_explanation(preview_page: Page):
    toggle = preview_page.locator(".metrics-note__toggle")
    panel = preview_page.locator("#metrics-note")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(panel).to_be_hidden()

    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(panel).to_be_visible()
    expect(toggle).to_have_text("Hide")

    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(panel).to_be_hidden()
    expect(toggle).to_have_text("Why?")


def test_trades_sort_toggles_date_direction(preview_page: Page):
    date_header = preview_page.locator('th[data-sort-key="date"]')
    # Boot applies the default newest-first sort on the date column.
    expect(date_header).to_have_attribute("aria-sort", "descending")

    sort_btn = date_header.locator(".trades__sort")
    sort_btn.click()
    expect(date_header).to_have_attribute("aria-sort", "ascending")

    sort_btn.click()
    expect(date_header).to_have_attribute("aria-sort", "descending")


def test_trades_price_sorts_by_currency_first(preview_page: Page):
    # A bare numeric sort across USD / EUR / GBp implies an ordering
    # that does not exist without an FX conversion. Currency first is
    # a real ordering, and the header's tooltip says so.
    header = preview_page.locator('th[data-sort-key="price"]')
    expect(header.locator(".trades__sort")).to_have_attribute(
        "title", re.compile(r"currency first")
    )
    header.locator(".trades__sort").click()
    expect(header).to_have_attribute("aria-sort", "ascending")
    pairs = preview_page.locator(".trades__row").evaluate_all(
        """els => els.map(el => [
            el.getAttribute('data-sort-currency'),
            parseFloat(el.getAttribute('data-sort-price')),
        ])"""
    )
    assert pairs == sorted(pairs, key=lambda p: (p[0], p[1]))


def test_nav_scroll_sets_hash_on_section_link(preview_page: Page):
    link = preview_page.locator('nav.site-nav a[href="#holdings"]')
    expect(link).to_be_visible()
    link.click()
    expect(preview_page).to_have_url(re.compile(r"#holdings$"))
    preview_page.wait_for_function(
        """() => {
            const el = document.getElementById('holdings');
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


def test_return_chart_tooltip_rows_line_up(preview_page: Page):
    # The tooltip's rows are ``display: contents`` over a two-column
    # grid, so every element inside a row is itself a grid item. A
    # third child per series (the swatch appended beside the label
    # rather than inside it) pushes that series' value into the next
    # row's label slot and scrambles the card from line two down.
    #
    # Asserting the geometry rather than the DOM shape: what matters
    # is that each label sits on the same line as its own value, with
    # the value to its right. That holds however the markup is built,
    # and fails for every arrangement that reads as broken.
    plot = preview_page.locator(".return-chart__plot").first
    box = plot.bounding_box()
    assert box is not None
    preview_page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(plot.locator(".return-chart__hover")).to_have_class(re.compile(r"\bis-active\b"))

    rows = plot.locator(".return-chart__tooltip-row")
    assert rows.count() >= 2, "preview should chart a portfolio and a benchmark"
    for index in range(rows.count()):
        row = rows.nth(index)
        label = row.locator(".return-chart__tooltip-label").bounding_box()
        value = row.locator(".return-chart__tooltip-value").bounding_box()
        assert label is not None and value is not None
        label_mid = label["y"] + label["height"] / 2
        value_mid = value["y"] + value["height"] / 2
        assert abs(label_mid - value_mid) <= 4, (
            f"row {index}: label and value are on different lines "
            f"({label_mid:.1f} vs {value_mid:.1f})"
        )
        assert value["x"] >= label["x"] + label["width"], (
            f"row {index}: value does not sit to the right of its label"
        )
        # The swatch belongs to its label, which is what keeps the row
        # at two grid items.
        assert (
            row.locator(".return-chart__tooltip-label .return-chart__tooltip-swatch").count() == 1
        )


def test_marquee_and_treemap_are_gone(preview_page: Page):
    # Both surfaces were removed, along with the scripts that drove
    # them. A stale element would mean a script hash is still pinned
    # in CSP for code nothing runs.
    expect(preview_page.locator(".ticker")).to_have_count(0)
    expect(preview_page.locator(".treemap")).to_have_count(0)


def test_chart_axes_are_readable_without_a_pointer(preview_page: Page):
    # The whole point of the redesigned chart: every value on it can
    # be read without hovering, which is also what makes it readable
    # on a phone and in print.
    plot = preview_page.locator(".return-chart__plot").first
    expect(plot).to_be_visible()
    assert plot.locator(".return-chart__tick").count() >= 4
    expect(plot.locator(".return-chart__base")).to_have_count(1)
    expect(plot.locator(".return-chart__end-value--jg")).to_have_count(1)
    expect(plot.locator(".return-chart__band--pos").first).to_be_visible()


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
