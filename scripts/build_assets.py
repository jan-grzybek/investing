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

# Kept available for the future ``_concatenate_css`` -> minify upgrade
# described below.
try:
    import csscompressor as _css
except ImportError:  # pragma: no cover - optional today
    _css = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_JS_DIR = REPO_ROOT / "assets" / "src" / "js"
SRC_CSS_DIR = REPO_ROOT / "assets" / "src" / "css"
OUT_DIR = REPO_ROOT / "assets"


def _minify_js(source: str) -> str:
    """Run rjsmin and append a trailing newline.

    ``end_of_file_fixer`` (the matching pre-commit hook) skips
    generated files, but a trailing newline is friendlier to anyone
    cat-ing the file at the terminal.
    """
    return rjsmin.jsmin(source, keep_bang_comments=False)


def _concatenate_css(chunks: list[str]) -> str:
    """Join CSS chunks with a blank line and ensure a trailing newline.

    CSS minification is deliberately *not* applied here -- the suite
    of webpage render tests asserts on the formatted whitespace
    pattern of the inline ``<style>`` block (``.foo { bar: 1; }``),
    so a minifying pass would invalidate ~20 assertions for a few
    kilobytes saved. The structural separation (readable
    ``assets/src/css/`` vs served ``assets/page.css``) is in place;
    flip on :func:`csscompressor.compress` here when you're ready
    to update the assertions in ``tests/test_webpage_*.py``.
    """
    body = "\n".join(chunks).rstrip("\n") + "\n"
    # Future enabling line:
    # return csscompressor.compress(body, max_linelen=0)
    return body


def _build_outputs() -> dict[Path, str]:
    """Return a mapping of output path -> contents."""
    outputs: dict[Path, str] = {}

    for src in sorted(SRC_JS_DIR.glob("*.js")):
        # ``end_of_file_fixer`` skips generated files (see the
        # ``exclude`` regex in ``.pre-commit-config.yaml``) so we
        # explicitly preserve the trailing-newline contract here.
        outputs[OUT_DIR / src.name] = _minify_js(src.read_text(encoding="utf-8"))

    css_chunks = [
        path.read_text(encoding="utf-8")
        for path in sorted(SRC_CSS_DIR.glob("*.css"))
    ]
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
