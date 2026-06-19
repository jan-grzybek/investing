"""Assemble a GitHub Pages artifact containing only public site files.

The production build writes ``index.html``, ``og-image.png``,
``sitemap.xml``, and ``robots.txt`` into the repo checkout alongside
the source tree. Uploading the entire checkout would also publish
``investing/``, ``tests/``, ``market_data/``, etc. This script copies
just the served artefacts into a staging directory (default ``site/``)
that ``upload-pages-artifact`` can point at.

Usage::

    python scripts/stage_site.py              # write ./site/
    python scripts/stage_site.py --out /tmp/site
    python scripts/stage_site.py --check      # verify ./site/ is current
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Generated at build time in the repo root (or ``--source``).
_ROOT_ARTIFACTS = (
    "index.html",
    "og-image.png",
    "sitemap.xml",
    "robots.txt",
)

# Static files committed at the repo root.
_STATIC_ROOT_FILES = ("favicon.svg",)

# Directories whose contents ship as-is (no ``src/`` subtrees).
_SITE_DIRS = (
    "assets",
    "logos/tight",
)


def _collect_source_paths(source: Path) -> dict[Path, Path]:
    """Return ``relative_dest -> absolute_source`` for every public file."""
    mapping: dict[Path, Path] = {}

    for name in _ROOT_ARTIFACTS + _STATIC_ROOT_FILES:
        src = source / name
        if src.is_file():
            mapping[Path(name)] = src

    for rel_dir in _SITE_DIRS:
        src_dir = source / rel_dir
        if not src_dir.is_dir():
            continue
        for src in src_dir.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(source)
            if rel.parts[0] == "assets" and "src" in rel.parts:
                continue
            mapping[rel] = src

    return mapping


def _write_staging(source: Path, out: Path) -> list[Path]:
    """Copy public files into ``out``; return paths written or updated."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    (out / ".nojekyll").write_text("", encoding="utf-8")

    changed: list[Path] = []
    for rel, src in sorted(_collect_source_paths(source).items()):
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        changed.append(rel)
    return changed


def _staging_matches(source: Path, out: Path) -> bool:
    """Return True when ``out`` mirrors a fresh staging from ``source``."""
    expected = _collect_source_paths(source)
    if not out.is_dir():
        return False
    if not (out / ".nojekyll").is_file():
        return False

    for rel, src in expected.items():
        dest = out / rel
        if not dest.is_file():
            return False
        if not filecmp.cmp(src, dest, shallow=False):
            return False

    staged = {p.relative_to(out) for p in out.rglob("*") if p.is_file()}
    expected_keys = set(expected.keys()) | {Path(".nojekyll")}
    extra = staged - expected_keys
    return not extra


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "site",
        help="Staging directory (default: ./site/)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT,
        help="Directory containing build outputs (default: repo root)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when ``--out`` is missing or stale (no writes).",
    )
    args = parser.parse_args()
    source = args.source.resolve()
    out = args.out.resolve()

    if args.check:
        if _staging_matches(source, out):
            print(f"Site staging at {out} is up to date.")
            return 0
        print(
            f"Site staging at {out} is stale -- run ``python scripts/stage_site.py --out {out}``",
            file=sys.stderr,
        )
        return 1

    written = _write_staging(source, out)
    print(f"Staged {len(written)} file(s) under {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
