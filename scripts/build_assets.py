"""Minify CSS / JS sources and write the served outputs.

The page generator reads inline CSS / JS payloads at import time and
hashes each one for the Content-Security-Policy in
:mod:`investing.webpage.head`. The hashes are computed over the
*served* bytes, so the build step is part of the contract: any change
to a source file under ``assets/src/`` must be propagated to the
matching generated artefact under ``assets/`` before the CSP hash
stays in sync.

This script is the single source of truth for that translation. It
runs from the pre-commit ``build-assets`` hook on changes under
``assets/src/`` and from CI to verify that committed outputs match
their freshly-built equivalents. Contributors can also run it
manually:

    python scripts/build_assets.py            # write outputs
    python scripts/build_assets.py --check    # exit non-zero if drift

Layout:

    assets/src/js/<name>.js   -- readable JS source
    assets/src/css/page.css   -- readable CSS source
    assets/<name>.js          -- minified, served by Pages
    assets/page.css           -- minified, served by Pages

CSS sources are concatenated in lexicographic filename order before
minification, which lets the styles eventually grow into a per-
section split (``00-base.css``, ``10-ticker.css``, ...) without
touching this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import rjsmin
except ImportError as exc:  # pragma: no cover - exercised only on a misconfigured env
    print(
        f"build_assets requires rjsmin: {exc}\n"
        "Install via ``pip install -r requirements-dev.txt`` or, in "
        "pre-commit, let the hook environment manage it.",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    import csscompressor
except ImportError as exc:  # pragma: no cover - exercised only on a misconfigured env
    print(
        f"build_assets requires csscompressor: {exc}\n"
        "Install via ``pip install -r requirements-dev.txt`` or, in "
        "pre-commit, let the hook environment manage it.",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_JS_DIR = REPO_ROOT / "assets" / "src" / "js"
SRC_CSS_DIR = REPO_ROOT / "assets" / "src" / "css"
OUT_DIR = REPO_ROOT / "assets"


def _minify_js(source: str) -> str:
    """Run rjsmin over ``source`` and return the minified output.

    ``rjsmin`` is idempotent, so source files that are already
    hand-minified pass through unchanged. ``keep_bang_comments=False``
    drops every comment from the served bytes.
    """
    return rjsmin.jsmin(source, keep_bang_comments=False)


def _concatenate_css(chunks: list[str]) -> str:
    """Concatenate readable CSS sources and minify the result.

    Test assertions against the served stylesheet now go through
    ``tests/_css_helpers.py``, which normalises whitespace before
    matching, so the minifier's output (``.foo{prop:val;...}`` on a
    single line) is now safe to ship without rewriting every
    assertion. The structural separation between readable
    ``assets/src/css/`` and served ``assets/page.css`` is the
    source-of-truth boundary the rest of the build relies on (the CSP
    hash in :mod:`investing.webpage.head` is computed over the served
    bytes).
    """
    body = "\n".join(chunks).rstrip("\n")
    # ``max_linelen=0`` disables the per-line wrap so the served
    # ``page.css`` ships as a single line -- smaller payload, and the
    # SHA-256 over it stays stable across csscompressor versions.
    return csscompressor.compress(body, max_linelen=0) + "\n"


def _build_outputs() -> dict[Path, str]:
    """Return a mapping of output path -> contents."""
    outputs: dict[Path, str] = {}

    for src in sorted(SRC_JS_DIR.glob("*.js")):
        # ``end_of_file_fixer`` skips generated files (see the
        # ``exclude`` regex in ``.pre-commit-config.yaml``) so we
        # explicitly preserve the trailing-newline contract here.
        outputs[OUT_DIR / src.name] = _minify_js(src.read_text(encoding="utf-8"))

    css_chunks = [path.read_text(encoding="utf-8") for path in sorted(SRC_CSS_DIR.glob("*.css"))]
    if css_chunks:
        outputs[OUT_DIR / "page.css"] = _concatenate_css(css_chunks)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Compare the freshly-built outputs against the committed "
            "files. Exit 1 (without writing) if any differ."
        ),
    )
    args = parser.parse_args()

    outputs = _build_outputs()
    drift: list[Path] = []
    for path, body in outputs.items():
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == body:
            continue
        drift.append(path)
        if not args.check:
            path.write_text(body, encoding="utf-8")

    if args.check:
        if drift:
            print(
                "Asset outputs are stale -- run ``python scripts/build_assets.py``:",
                file=sys.stderr,
            )
            for path in drift:
                print(f"  {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            return 1
        print("Assets are up to date.")
    else:
        if drift:
            print(f"Wrote {len(drift)} asset file(s):")
            for path in drift:
                print(f"  {path.relative_to(REPO_ROOT)}")
        else:
            print("Assets already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
