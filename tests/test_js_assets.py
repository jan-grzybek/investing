"""Smoke tests for minified client-side scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "assets"

EXPECTED_JS = (
    "hash_clear.js",
    "holdings_sort.js",
    "nav_scroll.js",
    "return_chart.js",
    "ticker_marquee.js",
    "trades_sort.js",
    "yearly_returns.js",
)


@pytest.mark.parametrize("name", EXPECTED_JS)
def test_minified_js_exists_and_is_wrapped(name: str):
    path = ASSETS / name
    assert path.is_file(), f"missing minified asset {name}"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("(function")
    assert body.rstrip().endswith("})();")


def test_yearly_returns_source_matches_served_contract():
    src = (REPO_ROOT / "assets/src/js/yearly_returns.js").read_text(encoding="utf-8")
    assert "returns-yearly__toggle" in src
    assert "aria-expanded" in src
    served = (ASSETS / "yearly_returns.js").read_text(encoding="utf-8")
    assert "returns-yearly__toggle" in served
