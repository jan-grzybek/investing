"""End-to-end Content-Security-Policy hash contract tests."""

from __future__ import annotations

import re
from pathlib import Path

from investing import assets
from investing.formatting import _sha256_b64
from investing.webpage.head import SiteMeta, build_csp, build_head, build_jsonld

_REPO_ROOT = Path(__file__).resolve().parents[1]

_INLINE_PAYLOADS = (
    ("jsonld", None),  # filled from build_jsonld below
    ("hash_clear", assets._HASH_CLEAR_SCRIPT),
    ("nav_scroll", assets._NAV_SCROLL_SCRIPT),
    ("return_chart", assets._RETURN_CHART_SCRIPT),
    ("ticker_marquee", assets._TICKER_MARQUEE_SCRIPT),
    ("trades_sort", assets._TRADES_SORT_SCRIPT),
    ("yearly_returns", assets._YEARLY_RETURNS_SCRIPT),
    ("holdings_sort", assets._HOLDINGS_SORT_SCRIPT),
)

_STYLE_PAYLOADS = (("page.css", assets._PAGE_STYLES),)


def _meta() -> SiteMeta:
    return SiteMeta(
        title="JG Investing",
        seo_title="Jan Grzybek - Investment Portfolio",
        description="Synthetic preview description for CSP contract tests.",
        url="https://jan-grzybek.github.io/investing/",
        social_image="https://jan-grzybek.github.io/investing/og-image.png",
    )


def test_served_asset_bytes_match_repo_files():
    assets_dir = _REPO_ROOT / "assets"
    pairs = (
        ("hash_clear.js", assets._HASH_CLEAR_SCRIPT),
        ("nav_scroll.js", assets._NAV_SCROLL_SCRIPT),
        ("return_chart.js", assets._RETURN_CHART_SCRIPT),
        ("ticker_marquee.js", assets._TICKER_MARQUEE_SCRIPT),
        ("trades_sort.js", assets._TRADES_SORT_SCRIPT),
        ("yearly_returns.js", assets._YEARLY_RETURNS_SCRIPT),
        ("holdings_sort.js", assets._HOLDINGS_SORT_SCRIPT),
        ("page.css", assets._PAGE_STYLES),
    )
    for name, loaded in pairs:
        path = assets_dir / name
        assert path.is_file(), f"missing served asset {name}"
        on_disk = path.read_text(encoding="utf-8")
        if name.endswith(".css"):
            assert on_disk.strip() == loaded
        else:
            assert on_disk == loaded


def test_build_csp_pins_every_inline_payload():
    meta = _meta()
    jsonld = build_jsonld(meta)
    csp = str(build_csp(jsonld))

    for label, body in _INLINE_PAYLOADS:
        payload = jsonld if label == "jsonld" else body
        assert payload is not None
        digest = _sha256_b64(payload)
        assert f"'sha256-{digest}'" in csp, f"missing CSP hash for {label}"

    for label, body in _STYLE_PAYLOADS:
        digest = _sha256_b64(body)
        assert f"'sha256-{digest}'" in csp, f"missing CSP hash for {label}"


def test_build_head_inlines_match_asset_module_and_csp():
    meta = _meta()
    head = str(build_head(meta))
    for _, body in _INLINE_PAYLOADS:
        if body is None:
            continue
        assert body in head
    assert assets._PAGE_STYLES in head

    hashes = re.findall(r"'sha256-([A-Za-z0-9+/=]+)'", head)
    assert len(hashes) >= 8
    assert len(hashes) == len(set(hashes)), "duplicate CSP hashes in head"
