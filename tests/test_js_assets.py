"""Smoke tests for minified client-side scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "assets"

EXPECTED_JS = (
    "hash_clear.js",
    "holdings_sort.js",
    "metrics_note.js",
    "nav_scroll.js",
    "return_chart.js",
    "trades_sort.js",
)


@pytest.mark.parametrize("name", EXPECTED_JS)
def test_minified_js_exists_and_is_wrapped(name: str):
    path = ASSETS / name
    assert path.is_file(), f"missing minified asset {name}"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("(function")
    assert body.rstrip().endswith("})();")


def test_holdings_sort_source_matches_served_contract():
    src = (REPO_ROOT / "assets/src/js/holdings_sort.js").read_text(encoding="utf-8")
    # The three attributes the renderer and the script have to agree
    # on: the table root, the per-column key, and the state the header
    # publishes back to assistive tech.
    for token in ("data-holdings-table", "data-sort-key", "aria-sort"):
        assert token in src
    served = (ASSETS / "holdings_sort.js").read_text(encoding="utf-8")
    assert "data-holdings-table" in served


def test_metrics_note_source_matches_served_contract():
    src = (REPO_ROOT / "assets/src/js/metrics_note.js").read_text(encoding="utf-8")
    assert "metrics-note__toggle" in src
    assert "aria-expanded" in src
    served = (ASSETS / "metrics_note.js").read_text(encoding="utf-8")
    assert "metrics-note__toggle" in served
