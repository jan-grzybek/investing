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

    Weight joined the right-aligned figures when its wide-frame bar
    retired, so the open table asserts three columns here, not two.
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
                    if (key !== 'tsr' && key !== 'cagr' && key !== 'weight') return;
                    const btn = th.querySelector('.holdings__sort');
                    const cell = row.children[i];
                    out.push({key, header: R(btn), value: R(cell),
                              align: getComputedStyle(cell).textAlign,
                              weight: getComputedStyle(cell).fontWeight});
                });
                return out;
            }"""
        )
        expected = 3 if scope == "open" else 2
        assert len(edges) == expected, f"{scope}: expected {expected} right-aligned columns"
        for col in edges:
            assert col["align"] == "right", f"{scope}/{col['key']} is {col['align']}"
            assert abs(col["header"] - col["value"]) <= 1, (
                f"{scope}/{col['key']}: header right edge {col['header']} vs value {col['value']}"
            )
        # Return carries the emphasis, IRR is the secondary reading.
        by_key = {c["key"]: c["weight"] for c in edges}
        assert by_key["tsr"] == "700", by_key
        assert by_key["cagr"] == "500", by_key


def test_mobile_cards_place_every_item_on_the_right_grid_line(page: Page, preview_index: Path):
    """The phone layout is a card, and each part has one place in it.

        [logo] [name              ] [return ]
               [listing     since ] [IRR    ]
               [======= bar ======] [ weight]

    This is the geometry, not the CSS, because the layout depends on
    the name cell dissolving via ``display: contents`` -- and a
    blanket ``.holdings__row th`` reset at (0,1,1) sits in front of
    that (0,1,0) declaration and will silently win if anyone reorders
    or re-adds it. When that happened the cell stayed intact and every
    sibling auto-placed around it: the listing, the date and the IRR
    each ended up on their own line.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())

    placement = page.evaluate(
        """() => {
            const out = {};
            for (const scope of ['open', 'closed']) {
                const row = document.querySelector(
                    `table[data-holdings-table="${scope}"] .holdings__row`);
                const items = [];
                const walk = el => {
                    for (const c of el.children) {
                        const cs = getComputedStyle(c);
                        if (cs.display === 'contents') { walk(c); continue; }
                        const b = c.getBoundingClientRect();
                        items.push({cls: c.className, row: cs.gridRowStart,
                                    col: cs.gridColumnStart,
                                    left: Math.round(b.left), right: Math.round(b.right)});
                    }
                };
                walk(row);
                out[scope] = items;
            }
            return out;
        }"""
    )

    def find(items, needle):
        hit = [i for i in items if needle in i["cls"]]
        assert hit, f"no cell matching {needle}"
        return hit[0]

    # Four columns: logo, listing, date, figures. The date has a cell
    # of its own so it can sit immediately after the listing -- with
    # three columns there was nowhere to put it but the far end of the
    # row, where it read as a caption on the IRR instead.
    for scope, expected in (
        (
            "open",
            {
                "holdings__name": ("1", "2"),
                "holdings__ticker": ("2", "2"),
                "holdings__since": ("2", "3"),
                "holdings__weight": ("3", "2"),
            },
        ),
        (
            "closed",
            {
                "holdings__name": ("1", "2"),
                "holdings__ticker": ("2", "2"),
                "holdings__periods": ("3", "2"),
            },
        ),
    ):
        items = placement[scope]
        for cls, (row, col) in expected.items():
            cell = find(items, cls)
            assert (cell["row"], cell["col"]) == (row, col), f"{scope}/{cls}: {cell}"
        # The two metrics stack in the last column.
        nums = [i for i in items if "holdings__num" in i["cls"]]
        assert len(nums) == 2, nums
        assert {n["col"] for n in nums} == {"4"}, nums
        assert {n["row"] for n in nums} == {"1", "2"}, nums

    # The date follows its listing directly, and is nearer to it than
    # to the IRR at the other end of the line -- which is the whole
    # point of giving it a column instead of pinning it right.
    open_items = placement["open"]
    ticker = find(open_items, "holdings__ticker")
    since = find(open_items, "holdings__since")
    irr = next(i for i in open_items if "holdings__num--soft" in i["cls"])
    assert ticker["right"] <= since["left"], (ticker, since)
    assert since["left"] - ticker["right"] < irr["left"] - since["right"], (ticker, since, irr)


def test_ownership_windows_each_hold_a_single_line(page: Page, preview_index: Path):
    """A date range is one line, always -- on the wide frame the date
    column grows to the widest range a re-entered row brings (auto
    table layout treats the 128px header width as a floor), rather
    than breaking the range after the dash. Asserted as geometry on
    every window on the page, open and closed tables, both frames."""
    for width, height in ((1280, 900), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        page.goto(preview_index.as_uri())
        spreads = page.evaluate(
            """() => [...document.querySelectorAll('.holdings__periods li')].map(li => {
                const tops = [...li.querySelectorAll('time, span')]
                    .map(e => Math.round(e.getBoundingClientRect().top));
                return Math.max(...tops) - Math.min(...tops);
            })"""
        )
        assert spreads, "no windows list rendered in the preview"
        assert all(s <= 1 for s in spreads), (width, spreads)


def test_mobile_activity_rows_are_two_lines(page: Page, preview_index: Path):
    """The design's mobile trade row, asserted as geometry.

        TICKER  Company name          921.40 USD
        BOUGHT  +32%                     Q2 2026

    Two properties are easy to lose and invisible in the CSS. The
    ticker and its company name have to sit together -- a shared grid
    column sized itself to the BOUGHT pill and left every name 50px
    adrift of its own ticker. And a long company name must not break
    the line: flex breaks on an item's *hypothetical* size, so a
    ``flex-basis: auto`` name went to a line of its own before
    shrinking was considered, taking the price with it to a third.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())
    # Expand so the long-name rows behind the collapse are checked too.
    page.locator(".trades__toggle").click()

    result = page.evaluate(
        """() => {
            const bad = [];
            let checked = 0;
            document.querySelectorAll('.trades__row').forEach((r, i) => {
                if (r.getBoundingClientRect().height === 0) return;
                checked++;
                const box = c => r.querySelector('.trades__cell--' + c).getBoundingClientRect();
                const t = box('ticker'), n = box('name'), p = box('price');
                const a = box('action'), d = box('detail'), dt = box('date');
                const gap = Math.round(n.left - t.right);
                const sameLine = (x, y) => Math.abs(x.top - y.top) < 6;
                if (!sameLine(t, n) || !sameLine(t, p)) bad.push({i, why: 'line 1 broke', gap});
                else if (!sameLine(a, d) || !sameLine(a, dt)) bad.push({i, why: 'line 2 broke'});
                else if (gap < 4 || gap > 12) bad.push({i, why: 'ticker/name adrift', gap});
                else if (Math.abs(p.right - dt.right) > 2) bad.push({i, why: 'right edge ragged'});
                else if (a.top <= t.top) bad.push({i, why: 'not two lines'});
            });
            return {checked, bad};
        }"""
    )
    assert result["checked"] >= 10, result
    assert not result["bad"], result["bad"]


def test_mobile_sort_chips_follow_the_designs_order(page: Page, preview_index: Path):
    """Chips lead with what a reader sorts each table by, not with
    column order: Weight for the open book, Dates for the closed one,
    Date for the log."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())
    order = page.evaluate(
        """(sel) => [...document.querySelectorAll(sel)]
            .filter(t => getComputedStyle(t).display !== 'none')
            .map(t => ({t: t.innerText.trim(), x: t.getBoundingClientRect().left}))
            .sort((a, b) => a.x - b.x)
            .map(o => o.t)""",
        "#holdings thead th",
    )
    # The chips run in the order the card under them reads, so a reader
    # scanning the strip is looking at the same row of cards either
    # way: name, the line under it, the two figures on the right, then
    # the bar across the bottom.
    assert order == ["Name", "Dates", "Return", "IRR", "Weight"], order
    order = page.evaluate(
        """(sel) => [...document.querySelectorAll(sel)]
            .filter(t => getComputedStyle(t).display !== 'none')
            .map(t => ({t: t.innerText.trim(), x: t.getBoundingClientRect().left}))
            .sort((a, b) => a.x - b.x)
            .map(o => o.t)""",
        "#closed thead th",
    )
    assert order == ["Name", "Dates", "Return", "IRR"], order
    order = page.evaluate(
        """(sel) => [...document.querySelectorAll(sel)]
            .filter(t => getComputedStyle(t).display !== 'none')
            .map(t => ({t: t.innerText.trim(), x: t.getBoundingClientRect().left}))
            .sort((a, b) => a.x - b.x)
            .map(o => o.t)""",
        "#activity thead th",
    )
    # Ticker and name lead, as they do on the card's first line; the
    # date closes, as it does on the second. "Name", not "Company":
    # half these rows are funds.
    assert order == ["Ticker", "Name", "Action", "Detail", "Price", "Date"], order


# The baseline probe: an empty inline-block's baseline is its bottom
# margin edge, so a 0x0 span appended to a line reports that line's
# baseline exactly. Comparing ``getBoundingClientRect()`` instead
# compares border boxes against text boxes, which differ by the
# descender even when the baselines agree -- so it cannot tell a real
# misalignment from two different font sizes sitting correctly.
_BASELINE_PROBE = """
    const baseline = el => {
        const s = document.createElement('span');
        s.style.cssText = 'display:inline-block;width:0;height:0;';
        el.appendChild(s);
        const y = s.getBoundingClientRect().bottom;
        s.remove();
        return y;
    };
"""


def test_action_badge_label_shares_the_row_baseline(page: Page, preview_index: Path):
    """The badge's label must sit on the same baseline as the Detail
    and Date beside it.

    This is a trap the swatch walks straight into. A flex container
    takes its baseline from its *first* flex item, and the first item
    in the badge is the swatch -- an empty box, whose baseline is its
    own bottom edge. Made ``inline-flex``, the badge therefore hands
    the line a baseline taken from a 7px square, and its label floats
    3px above everything else on the row. ``inline-block`` takes the
    baseline from the last line box, which is the label itself.

    The probe goes *inside* the badge, not inside its cell: the cell's
    baseline is the one the flex row aligned, and it agrees either
    way. What moves is the label within it.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())
    page.locator(".trades__toggle").click()

    result = page.evaluate(
        """() => {"""
        + _BASELINE_PROBE
        + """
            const bad = [];
            let checked = 0;
            document.querySelectorAll('.trades__row').forEach((r, i) => {
                if (r.getBoundingClientRect().height === 0) return;
                checked++;
                const line = [
                    baseline(r.querySelector('.trade__badge')),
                    baseline(r.querySelector('.trades__cell--detail')),
                    baseline(r.querySelector('.trades__cell--date')),
                ];
                const spread = Math.max(...line) - Math.min(...line);
                if (spread > 0.6) bad.push({i, spread: +spread.toFixed(2)});
            });
            return {checked, bad};
        }"""
    )
    assert result["checked"] >= 10, result
    assert not result["bad"], result["bad"]


def test_phone_frame_runs_the_designs_smaller_type_scale(page: Page, preview_index: Path):
    """The design ships two frames, 880px and 390px, and they do not
    run the same type: headings, captions and uppercase labels all
    step down on the narrow one.

    Shipping the desktop scale into a 390px frame is what made the
    phone layout read as a squeezed desktop page -- the same words at
    the same size in half the width, so the hierarchy between a
    heading and the note under it collapsed. Each pair below is a
    measured value from the design's own two frames.
    """
    wanted = {
        ".section__title": (17, 15),
        ".hero__eyebrow": (11, 9.5),
        ".hero__stat-label": (11, 9.5),
        ".yearly__caption": (13, 10.5),
        ".yearly__summary": (13, 11.5),
        ".allocation__title": (11, 9.5),
        ".method__title": (15, 13),
    }
    measured = {}
    for width, key in ((880, 0), (390, 1)):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(preview_index.as_uri())
        for selector in wanted:
            size = page.evaluate(
                """(sel) => {
                    const e = document.querySelector(sel);
                    return e ? parseFloat(getComputedStyle(e).fontSize) : null;
                }""",
                selector,
            )
            measured.setdefault(selector, [None, None])[key] = size

    for selector, (desktop, phone) in wanted.items():
        got_desktop, got_phone = measured[selector]
        assert got_desktop == pytest.approx(desktop, abs=0.26), (selector, measured)
        assert got_phone == pytest.approx(phone, abs=0.26), (selector, measured)

    # The acronym that names the metric is dropped on the phone, so the
    # stat label reads "Portfolio" rather than "Portfolio TWR".
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(preview_index.as_uri())
    labels = page.evaluate(
        """() => [...document.querySelectorAll('.hero__stat-label')]
            .map(e => e.innerText.trim())"""
    )
    assert not any("TWR" in x or "TSR" in x for x in labels), labels


def test_phone_chart_drops_the_end_labels_and_reclaims_their_gutter(
    page: Page, preview_index: Path
):
    """The design's phone chart has no end labels, and its curve runs
    to the right edge.

    Hiding the labels is half the job. The wide frame reserves viewBox
    for them, so with the labels gone that gutter would sit there as
    dead margin and the curve would stop a ninth short of the edge.
    ``preserveAspectRatio="xMinYMid slice"`` plus the plot-only aspect
    ratio scales the drawing to fill the narrower box and clips the
    gutter away -- same geometry, no distortion.
    """
    measured = {}
    for width, key in ((880, "wide"), (390, "phone")):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(preview_index.as_uri())
        measured[key] = page.evaluate(
            """() => {
                const fig = document.querySelector('.return-chart');
                const svg = fig.querySelector('svg');
                const d = JSON.parse(fig.dataset.chart);
                // Where the last plotted x actually lands on screen.
                const pt = svg.createSVGPoint();
                pt.x = d.plot.x1; pt.y = 0;
                const at = pt.matrixTransform(svg.getScreenCTM());
                const box = svg.getBoundingClientRect();
                return {
                    fill: (at.x - box.left) / box.width,
                    ends: getComputedStyle(
                        fig.querySelector('.return-chart__end-value')).display,
                    sign: getComputedStyle(
                        fig.querySelector('.return-chart__tick-sign')).display,
                };
            }"""
        )
    assert measured["wide"]["ends"] != "none"
    assert measured["phone"]["ends"] == "none"
    # "+0%" wide, "0%" on the phone -- the design's phone axis.
    assert measured["wide"]["sign"] != "none"
    assert measured["phone"]["sign"] == "none"
    # The wide frame keeps a gutter for the labels; the phone does not.
    assert measured["wide"]["fill"] < 0.92, measured
    assert measured["phone"]["fill"] > 0.95, measured


def test_chart_hover_marker_tracks_the_curve_in_both_frames(page: Page, preview_index: Path):
    """The hover marker has to sit on the curve it reads.

    The scale factor is derived from the plot's *height*, not its
    width, precisely because the phone frame clips the SVG
    horizontally: there the rendered drawing is wider than its box, so
    a width-derived scale under-reports and every marker drifts left
    of its curve -- by about 12% of the chart at the right-hand end.
    The vertical axis is never clipped, which is what makes height the
    honest reference.
    """
    for width in (880, 390):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(preview_index.as_uri())
        plot = page.locator(".return-chart__plot")
        plot.scroll_into_view_if_needed()
        box = plot.bounding_box()
        assert box is not None
        for fraction in (0.15, 0.5, 0.9):
            page.mouse.move(box["x"] + box["width"] * fraction, box["y"] + box["height"] / 2)
            offset = page.evaluate(
                """() => {
                    const fig = document.querySelector('.return-chart');
                    const svg = fig.querySelector('svg');
                    const ctm = svg.getScreenCTM();
                    let worst = 0;
                    ['jg', 'bench'].forEach(kind => {
                        const mk = fig.querySelector('.return-chart__marker--' + kind);
                        const line = svg.querySelector('.return-chart__line--' + kind);
                        if (!mk || !line) return;
                        const m = mk.getBoundingClientRect();
                        const mx = m.left + m.width / 2, my = m.top + m.height / 2;
                        let best = Infinity, bestY = 0;
                        line.getAttribute('points').trim().split(' ').forEach(pair => {
                            const [px, py] = pair.split(',').map(Number);
                            const q = svg.createSVGPoint(); q.x = px; q.y = py;
                            const s = q.matrixTransform(ctm);
                            const d = Math.abs(s.x - mx);
                            if (d < best) { best = d; bestY = s.y; }
                        });
                        worst = Math.max(worst, Math.abs(bestY - my));
                    });
                    return worst;
                }"""
            )
            # Vertical distance from the marker's centre to the curve.
            # Compared against the nearest sampled vertex, so a couple
            # of pixels of polyline granularity is expected; a broken
            # scale puts it tens of pixels out.
            assert offset < 4, (width, fraction, offset)


def test_allocation_labels_never_overflow_their_segment(page: Page, preview_index: Path):
    """A drawn percentage has to fit inside the slice it belongs to,
    and every legend chip carries its share whether the bar drew one
    or not.

    The old rule dropped a label below a fixed 6% share, which decided
    in percent a question that is really about pixels: on a narrow
    page two neighbours both cleared 6% and neither could fit, so the
    bar rendered "10.7%10.6%" -- two numbers, no gap, no way to tell
    which belonged to which colour. A container query on the segment
    asks the question of the box that knows the answer.
    """
    for width in (1440, 880, 620, 390, 320):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(preview_index.as_uri())
        result = page.evaluate(
            """() => {
                const bad = [];
                let drawn = 0;
                document.querySelectorAll('.allocation__segment').forEach(seg => {
                    const v = seg.querySelector('.allocation__segment-value');
                    if (!v || getComputedStyle(v).display === 'none') return;
                    drawn++;
                    const range = document.createRange();
                    range.selectNodeContents(v);
                    const text = range.getBoundingClientRect();
                    const slice = seg.getBoundingClientRect();
                    if (text.width > slice.width + 0.5) {
                        bad.push(v.textContent + ' in ' + slice.width.toFixed(0) + 'px');
                    }
                });
                const legends = [];
                document.querySelectorAll('.allocation__key').forEach(k => {
                    const items = [...k.querySelectorAll('.allocation__key-item')];
                    legends.push([
                        items.length,
                        items.filter(i => i.querySelector('.allocation__key-value')).length,
                    ]);
                });
                return {bad, drawn, legends};
            }"""
        )
        assert not result["bad"], (width, result["bad"])
        # Non-vacuous: the widest slice always keeps its label.
        assert result["drawn"] >= 1, (width, result)
        for total, labelled in result["legends"]:
            assert total == labelled, (width, result["legends"])


def test_chart_legend_sits_under_the_caption_on_a_phone(page: Page, preview_index: Path):
    """Wide, the legend shares the heading's line; narrow, it drops to
    its DOM position under the caption and directly above the chart.
    One copy of the markup, two grid placements -- so the curves are
    named exactly once for a screen reader."""
    placements = {}
    for width, key in ((880, "wide"), (390, "phone")):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(preview_index.as_uri())
        placements[key] = page.evaluate(
            """() => {
                const top = s => document.querySelector(s).getBoundingClientRect().top;
                return {
                    heading: top('.section--chart > .section__head'),
                    caption: top('.section--chart > .section__intro'),
                    legend: top('.section--chart > .legend'),
                    copies: document.querySelectorAll('.legend').length,
                };
            }"""
        )
    wide, phone = placements["wide"], placements["phone"]
    assert wide["legend"] < wide["caption"], wide
    assert abs(wide["legend"] - wide["heading"]) < 24, wide
    assert phone["legend"] > phone["caption"], phone
    assert wide["copies"] == 1 and phone["copies"] == 1, placements


def test_sort_chips_are_evenly_spaced_and_hint_when_they_scroll(page: Page, preview_index: Path):
    """Every chip's cell must be exactly as wide as the chip in it,
    and a strip that scrolls must say so.

    The desktop column widths live on selectors like
    ``.holdings__col--num:last-child`` -- (0,2,0) -- and the card-mode
    reset was ``.holdings thead th``, only (0,1,2). The reset lost, so
    the IRR chip's cell kept its 92px table width while the button
    inside measured 53px: 39px of dead cell masquerading as a gap
    between two chips whose ``gap`` was an even 6px throughout. The
    logo header lost the same way and stayed in the strip as a
    zero-width item, charging a further gap before the first chip.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())

    result = page.evaluate(
        """() => {
            const out = {};
            [['open', 'table[data-holdings-table="open"] thead tr'],
             ['closed', 'table[data-holdings-table="closed"] thead tr'],
             ['activity', '.trades thead tr']].forEach(([id, sel]) => {
                const tr = document.querySelector(sel);
                const cells = [...tr.children]
                    .filter(th => getComputedStyle(th).display !== 'none')
                    .sort((a, b) => a.getBoundingClientRect().left
                                  - b.getBoundingClientRect().left);
                const gaps = [];
                for (let i = 1; i < cells.length; i++) {
                    gaps.push(Math.round(cells[i].getBoundingClientRect().left
                                       - cells[i - 1].getBoundingClientRect().right));
                }
                out[id] = {
                    labels: cells.map(c => c.innerText.trim()),
                    gaps,
                    // Cell wider than the chip inside it == dead space.
                    dead: cells.map(c => {
                        const btn = c.querySelector('button');
                        return Math.round(c.getBoundingClientRect().width
                            - (btn ? btn.getBoundingClientRect().width : 0));
                    }),
                    frozen: cells.map(c => c.style.width || ''),
                    heights: cells.map(c => {
                        const b = c.querySelector('.holdings__sort');
                        const r = (b || c).getBoundingClientRect();
                        // The gap between chips is part of the target's
                        // separation, not of the target itself; the row
                        // gap adds to the reachable band.
                        return Math.round(r.height + 8);
                    }),
                    scrolls: tr.scrollWidth > tr.clientWidth + 1,
                    hint: tr.getAttribute('data-scroll'),
                    masked: getComputedStyle(tr).maskImage !== 'none',
                };
            });
            return out;
        }"""
    )

    for scope, data in result.items():
        assert data["labels"], scope
        # Every chip carries a caption -- the logo header is gone, not
        # lingering as an empty item.
        assert all(data["labels"]), (scope, data["labels"])
        # Every chip clears the 44px touch target both platform
        # guidelines ask for. They used to compute to 26.5px -- and
        # they shrank at exactly the width where they stop being
        # reachable with a mouse and become the only sort UI.
        assert min(data["heights"]) >= 44, (scope, data["heights"])
        assert set(data["gaps"]) == {8}, (scope, data["gaps"])
        assert max(data["dead"]) <= 1, (scope, data["dead"])
        # And no chip may be *narrower* than the button inside it.
        #
        # The Activity strip regressed here: its sort script freezes
        # column widths, and once those became percentages the freeze
        # started running in card mode too -- where a percentage is a
        # share of the *strip*, and the strip is narrower than the chips
        # it scrolls. Every chip was squeezed to about 70% of its label
        # and the buttons overlapped their neighbours, which is exactly
        # what the horizontal scroll exists to avoid.
        assert min(data["dead"]) >= -1, (scope, data["dead"])
        # The hint is a fade over the strip's own rendering, so a chip
        # travelling toward the edge dissolves instead of being sliced
        # flat by it. It appears only on the side that still has chips
        # to reach, and not at all on a strip that fits -- which is why
        # it is asserted against ``scrolls`` rather than unconditionally.
        # No frozen column width survives into the card layout: a width
        # meant for a table column has no meaning on a chip.
        assert not any(data["frozen"]), (scope, data["frozen"])
        if data["scrolls"]:
            assert data["hint"] in {"start", "end", "both"}, (scope, data)
            assert data["masked"], (scope, data)
        else:
            assert data["hint"] is None, (scope, data)
            assert not data["masked"], (scope, data)


def test_first_click_on_the_pre_sorted_column_reverses_it(page: Page, preview_index: Path):
    """Clicking the column a table is *already* sorted by must reverse
    it on the first click, not the second.

    The script used to boot blind: ``state.key`` was null and every
    ``aria-sort`` said "none", even though the open book arrives
    ordered by weight. The first click on Weight therefore took the
    "new column" branch, picked ``descending`` -- the direction a
    number column opens in -- and re-applied the order the table was
    already in. Nothing moved, and it took a second click to reach
    ascending. The table also showed no sort indicator at all until
    something was clicked.

    Both tables now declare the order they are in and the script
    adopts it, so the indicator is right from first paint and the
    first click is a reversal.
    """
    for width in (1000, 390):
        page.set_viewport_size({"width": width, "height": 900})
        for scope, column in (("open", "weight"), ("closed", "held")):
            page.goto(preview_index.as_uri())
            table = f'table[data-holdings-table="{scope}"]'

            def snapshot(sel: str = table):
                return page.evaluate(
                    """(sel) => {
                        const t = document.querySelector(sel);
                        return {
                            names: [...t.querySelectorAll('.holdings__row .holdings__name')]
                                .map(e => e.textContent.trim()),
                            active: [...t.querySelectorAll('thead th[data-sort-key]')]
                                .filter(th => th.getAttribute('aria-sort') !== 'none')
                                .map(th => th.dataset.sortKey + '=' + th.getAttribute('aria-sort')),
                        };
                    }""",
                    sel,
                )

            start = snapshot()
            # The indicator is present before any interaction.
            assert start["active"] == [f"{column}=descending"], (scope, width, start)
            assert len(start["names"]) >= 2, (scope, start)

            chip = f'{table} .holdings__col[data-sort-key="{column}"] .holdings__sort'
            page.click(chip)
            page.wait_for_timeout(80)
            first = snapshot()
            assert first["active"] == [f"{column}=ascending"], (scope, width, first)
            # The visible order actually moved -- the whole complaint.
            assert first["names"] != start["names"], (scope, width, first["names"])

            page.click(chip)
            page.wait_for_timeout(80)
            second = snapshot()
            assert second["active"] == [f"{column}=descending"], (scope, width, second)
            assert second["names"] == start["names"], (scope, width, second["names"])


def test_every_text_step_and_segment_label_clears_wcag_aa(page: Page, preview_index: Path):
    """The neutral ramp and the allocation labels both have to be
    legible against the surface they are drawn on, in both themes.

    The palette's own comment claimed luminance was rotated so
    "contrast lands within WCAG AA". That was true on the dark surface
    and false on the light one, where every step came in under 4.5:1 --
    with the 11.5px ticker and the 10px group band sitting at the
    bottom of it. The segment labels had the same problem from the
    other direction: white on Tiger Orange is 2.48:1, propped up by a
    text-shadow, which is what you reach for when a colour is wrong.
    """

    def luminance(css_colour: str) -> float:
        parts = [float(x) for x in re.findall(r"[\d.]+", css_colour)][:3]
        if css_colour.startswith("color("):
            parts = [x * 255 for x in parts]
        channels = []
        for raw in parts:
            c = raw / 255
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def contrast(a: str, b: str) -> float:
        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.set_viewport_size({"width": 880, "height": 1000})
        page.goto(preview_index.as_uri())
        samples = page.evaluate(
            """() => {
                const surface = el => {
                    let n = el;
                    while (n) {
                        const c = getComputedStyle(n).backgroundColor;
                        if (c && c !== 'rgba(0, 0, 0, 0)') return c;
                        n = n.parentElement;
                    }
                    return null;
                };
                const out = [];
                document.querySelectorAll(
                    '.holdings__ticker, .holdings__band td, .section__note, '
                    + '.holdings__since'
                ).forEach(e => {
                    if (!e.textContent.trim()) return;
                    out.push({what: e.className.split(' ')[0],
                              fg: getComputedStyle(e).color, bg: surface(e)});
                });
                document.querySelectorAll('.allocation__segment').forEach(seg => {
                    const v = seg.querySelector('.allocation__segment-value');
                    const cs = getComputedStyle(v);
                    if (cs.display === 'none') return;
                    out.push({what: 'segment-label', fg: cs.color,
                              bg: getComputedStyle(seg).backgroundColor,
                              shadow: cs.textShadow});
                });
                return out;
            }"""
        )
        assert samples, scheme
        for sample in samples:
            got = contrast(sample["fg"], sample["bg"])
            assert got >= 4.5, (scheme, sample, round(got, 2))
            # The shadow existed only to prop up an ink that could not
            # carry the fill; with a per-fill ink it has no job.
            assert sample.get("shadow", "none") == "none", (scheme, sample)


def test_the_nav_reaches_every_anchored_section_and_says_where_you_are(
    page: Page, preview_index: Path
):
    """Six blocks carry an id; the bar listed four, so Allocation was
    a full section with no way to reach it. And a sticky bar over one
    long document that never marks the section under the reader is a
    map with no marker.

    Both edges matter: above the first heading the bar defaults to the
    first pill rather than to nothing, and at the foot of the document
    the last section wins outright -- it is shorter than the viewport,
    so its top never crosses the offset line on its own.
    """
    page.set_viewport_size({"width": 1000, "height": 900})
    page.goto(preview_index.as_uri())

    labels = page.evaluate(
        """() => [...document.querySelectorAll('.site-nav a')].map(a => a.textContent.trim())"""
    )
    assert labels == ["Performance", "Allocation", "Holdings", "Activity", "Method"], labels
    # Five pills and the brand still hold one line -- a wrapped nav is
    # what put every anchor under the sticky header.
    lines = page.evaluate(
        """() => new Set([...document.querySelectorAll('.site-nav a')]
            .map(a => Math.round(a.getBoundingClientRect().top))).size"""
    )
    assert lines == 1, lines

    def current():
        return page.evaluate(
            """() => {
                const a = document.querySelector('.site-nav a[aria-current="true"]');
                return a ? a.textContent.trim() : null;
            }"""
        )

    assert current() == "Performance"
    for label in labels:
        page.evaluate("(a) => document.querySelector(a).scrollIntoView()", f"#{label.lower()}")
        page.mouse.wheel(0, 1)
        page.wait_for_timeout(80)
        assert current() == label, (label, current())
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(120)
    assert current() == "Method"
    assert page.evaluate("""() => document.querySelectorAll('[aria-current="true"]').length""") == 1


def test_the_activity_table_never_escapes_its_card(page: Page, preview_index: Path):
    """The sort script used to freeze six column widths in absolute
    pixels, and unfreeze them on every resize -- which cleared
    ``table-layout`` and handed the table back to auto layout for
    150ms. On auto its min-content is wider than the card: the columns
    jumped, a scrollbar appeared inside the card and Price clipped its
    currency code.

    Percentages hold the same proportions through any width, so the
    freeze survives a resize and the unfreeze cycle is gone. The CSS
    declares the algorithm and a baseline split too, so the no-JS path
    cannot overflow either.
    """
    page.goto(preview_index.as_uri())
    page.wait_for_timeout(250)
    for width in (1000, 940, 860, 780, 720, 690, 1000):
        page.set_viewport_size({"width": width, "height": 900})
        for pause in (0, 40, 160, 320):
            page.wait_for_timeout(pause or 15)
            state = page.evaluate(
                """() => {
                    const t = document.querySelector('.trades');
                    if (getComputedStyle(t).display !== 'table') return null;
                    const wrap = document.querySelector('.trades__wrap');
                    const clipped = [];
                    t.querySelectorAll('td,th').forEach(c => {
                        if (c.scrollWidth - c.clientWidth > 1) {
                            clipped.push(c.className);
                        }
                    });
                    return {
                        layout: getComputedStyle(t).tableLayout,
                        overflow: wrap.scrollWidth - wrap.clientWidth,
                        clipped: [...new Set(clipped)],
                        units: [...t.querySelectorAll('thead th')]
                            .map(th => th.style.width).join('|'),
                    };
                }"""
            )
            if state is None:
                continue
            assert state["layout"] == "fixed", (width, pause, state)
            assert state["overflow"] == 0, (width, pause, state)
            assert not state["clipped"], (width, pause, state)
            if state["units"].strip("|"):
                assert "px" not in state["units"], (width, pause, state["units"])


def test_phone_cards_show_the_whole_holding_name(page: Page, preview_index: Path):
    """The name is what identifies the row. Clamped to one line the two
    bond ETFs and the dual-listed Samsung all cut off -- while the card
    beneath them had a whole empty line. Two lines are allowed now; the
    listing keeps its ellipsis, since that one is a code."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())
    truncated = page.evaluate(
        """() => [...document.querySelectorAll('.holdings__name')]
            .filter(n => n.scrollHeight > n.clientHeight + 1)
            .map(n => n.textContent.trim())"""
    )
    assert not truncated, truncated


def test_the_scroll_hint_fades_the_chip_itself(page: Page, preview_index: Path):
    """The hint has to act on the chips, not just on the gaps between
    them.

    It began as four background layers -- two riding the content, two
    pinned -- which is elegant and self-managing, and wrong in one
    way: a background paints *behind* content. The chips are opaque
    pills, so they slid over the shadow instead of under it. The
    shadow darkened the gaps and left every chip crisp, sliced flat at
    the container edge, which is the opposite of what a shadow claims.

    Measured here as pixels: sample a column inside the fade and the
    same column's worth of chip ink well clear of it, and the edge
    sample has to be the paler of the two.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())
    strip = page.locator('table[data-holdings-table="open"] thead tr')
    strip.scroll_into_view_if_needed()
    page.evaluate(
        """() => {
            const tr = document.querySelector(
                'table[data-holdings-table="open"] thead tr');
            tr.scrollLeft = (tr.scrollWidth - tr.clientWidth) / 2;
        }"""
    )
    page.wait_for_timeout(150)
    assert strip.get_attribute("data-scroll") == "both"

    shot = strip.screenshot()
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(shot)).convert("L")
    width, height = image.size

    def darkest(x0: int, x1: int) -> int:
        """Darkest pixel in a vertical band -- i.e. the strongest ink."""
        band = image.crop((x0, 0, x1, height))
        return min(band.getdata())

    # The mask ramps over the outermost 26 CSS px; the screenshot is at
    # the page's own scale, so work in fractions of the strip width.
    edge = darkest(0, max(2, int(width * 0.02)))
    middle = darkest(int(width * 0.35), int(width * 0.65))
    # Ink at the very edge must be lighter than ink in the clear.
    assert edge > middle + 40, (edge, middle)
    # And the clear region must still carry real ink, or the assertion
    # above would pass on an empty strip.
    assert middle < 120, middle


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
    # Price is a number, so the first click opens it high-to-low --
    # the same direction the holdings tables open a number in. This
    # table used to open every column but Date ascending, so the first
    # click on Price offered the cheapest fill in the log while the
    # identical click two sections up offered the largest position.
    header.locator(".trades__sort").click()
    expect(header).to_have_attribute("aria-sort", "descending")

    def pairs():
        return preview_page.locator(".trades__row").evaluate_all(
            """els => els.map(el => [
                el.getAttribute('data-sort-currency'),
                parseFloat(el.getAttribute('data-sort-price')),
            ])"""
        )

    assert pairs() == sorted(pairs(), key=lambda p: (p[0], p[1]), reverse=True)
    header.locator(".trades__sort").click()
    expect(header).to_have_attribute("aria-sort", "ascending")
    assert pairs() == sorted(pairs(), key=lambda p: (p[0], p[1]))


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


def test_nav_anchors_clear_the_sticky_header_on_a_phone(page: Page, preview_index: Path):
    """Every shortcut has to land the section *below* the sticky bar.

    The offset used to be a literal 68px, which was right on desktop
    and wrong on a phone: there the four nav links wrapped to a second
    line, the header grew to 86px, and every section landed 18px
    underneath it. The offset is now derived from the header's
    measured height, and the nav collapses behind a disclosure so the
    header stays one line -- both of which this pins.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(preview_index.as_uri())

    header = page.locator(".site-header")
    toggle = page.locator(".site-nav__toggle")
    expect(toggle).to_be_visible()
    # One line: the wrap is what made the header tall enough to swallow
    # the anchor offset.
    assert header.bounding_box()["height"] < 60

    targets = page.locator("nav.site-nav a").evaluate_all(
        "els => els.map(a => a.getAttribute('href').slice(1))"
    )
    assert targets, "expected a collapsed nav with links"
    for target in targets:
        page.evaluate("window.scrollTo(0, 0)")
        toggle.click()
        page.locator(f'nav.site-nav a[href="#{target}"]').click()
        page.wait_for_timeout(900)
        landed = page.evaluate(
            """(id) => {
                const top = document.getElementById(id).getBoundingClientRect().top;
                const bottom = document.querySelector('.site-header').getBoundingClientRect().bottom;
                const max = document.documentElement.scrollHeight - window.innerHeight;
                return {gap: Math.round(top - bottom), atEnd: Math.ceil(window.scrollY) >= max - 1};
            }""",
            target,
        )
        # The last section on the page cannot reach the top -- the
        # document runs out of scroll first -- so "as far as it goes"
        # is the correct landing there. Everywhere else the section
        # has to clear the bar without hiding behind it.
        assert landed["atEnd"] or 0 <= landed["gap"] <= 28, (
            f"#{target} landed {landed['gap']}px relative to the header"
        )
        # Choosing a section closes the drawer; leaving it open would
        # cover the thing the reader just asked to see.
        expect(page.locator("nav.site-nav")).to_be_hidden()


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
