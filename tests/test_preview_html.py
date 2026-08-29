"""Structural validation of the synthetic preview page."""

from __future__ import annotations

import re
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


class TestLogoStaging:
    """``_stage_logos`` copies the served mirror next to the render.

    Logo ``src`` values are relative, so the preview has to provide the
    same sibling directory GitHub Pages serves. The dangerous case is
    ``--out .``, which the module docstring documents as supported
    because every artifact is gitignored: there the destination *is*
    the repo's own ``logos/tight``, and a naive wipe-then-copy deletes
    the served mirror and then fails to copy it back from the directory
    it just removed.
    """

    @staticmethod
    def _preview_module():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_preview_under_test", REPO_ROOT / "scripts" / "preview.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_staging_into_the_repo_root_is_a_no_op(self):
        preview = self._preview_module()
        source = preview._REPO_LOGOS_DIR
        before = sorted(p.name for p in source.iterdir())
        assert before, "repo logo mirror should not be empty"

        # ``out_dir`` is the repo root, so ``dest`` resolves onto the
        # source directory itself.
        preview._stage_logos(REPO_ROOT)

        after = sorted(p.name for p in source.iterdir())
        assert after == before, "staging must never delete the repo's own logo mirror"

    def test_staging_into_a_fresh_directory_copies_the_mirror(self, tmp_path):
        preview = self._preview_module()
        preview._stage_logos(tmp_path)

        staged = tmp_path / "logos" / "tight"
        assert staged.is_dir()
        assert sorted(p.name for p in staged.iterdir()) == sorted(
            p.name for p in preview._REPO_LOGOS_DIR.iterdir()
        )

    def test_restaging_replaces_a_stale_directory(self, tmp_path):
        preview = self._preview_module()
        staged = tmp_path / "logos" / "tight"
        staged.mkdir(parents=True)
        (staged / "GONE:OLD.svg").write_text("<svg/>", encoding="utf-8")

        preview._stage_logos(tmp_path)

        assert not (staged / "GONE:OLD.svg").exists()
        assert (staged / "courage.png").is_file()


class TestRenderedPercentagesAddUp:
    """Whole-page guard: every stacked bar on the real render totals 100.

    The unit tests pin the helper and the bar builder. This one walks
    the finished document, so a *new* bar added later without going
    through the apportionment is caught here even though nothing about
    it was named in a test.

    What it does not do is prove the rounding fix by itself: the
    preview's synthetic weights happen to round cleanly, so these
    assertions still pass on the unrounded code. The drift case is
    pinned in ``TestAllocationBarsAlwaysAddUp`` with figures chosen to
    break. Treat this class as a structural sweep -- does every bar on
    the page satisfy the invariant -- not as the regression test.
    """

    @staticmethod
    def _bars(html_out: str):
        chunks = html_out.split('<div class="allocation__block">')[1:]
        for chunk in chunks:
            title_match = re.search(r'allocation__title">([^<]*)<', chunk)
            title = title_match.group(1) if title_match else "(untitled)"
            legend = [
                float(v) for v in re.findall(r'allocation__key-value">([\d.]+)%</span>', chunk)
            ]
            segments = [
                float(v) for v in re.findall(r'allocation__segment-value">([\d.]+)%<', chunk)
            ]
            widths = [float(w) for w in re.findall(r"width: ([\d.]+)%", chunk)]
            yield title, legend, segments, widths

    def test_the_page_renders_at_least_two_bars(self, preview_html):
        assert len(list(self._bars(preview_html))) >= 2

    def test_every_bar_legend_totals_a_hundred(self, preview_html):
        for title, legend, _segments, _widths in self._bars(preview_html):
            assert legend, f"{title}: no legend values found"
            assert sum(legend) == pytest.approx(100.0), f"{title}: legend sums to {sum(legend)}"

    def test_every_bar_segment_set_matches_its_legend(self, preview_html):
        for title, legend, segments, _widths in self._bars(preview_html):
            assert segments == legend, f"{title}: bar and key disagree"

    def test_every_bar_fills_its_track(self, preview_html):
        for title, _legend, _segments, widths in self._bars(preview_html):
            assert sum(widths) == pytest.approx(100.0), f"{title}: widths sum to {sum(widths)}"
