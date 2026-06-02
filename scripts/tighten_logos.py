"""Generate cropped variants of logo SVGs in ``logos/tight/``.

For each SVG under ``logos/`` (excluding the ``tight/`` subdirectory),
this script rasterises the artwork, finds the tight bounding box of
the visible content (= the pixels that would survive the treemap's
white-knockout filter, defined in ``investing/logos.py``), and
rewrites the SVG's ``viewBox`` to that crop. The width / height
root attributes are stripped so the SVG's intrinsic aspect ratio is
derived from the cropped viewBox alone -- otherwise a mismatch
between the original ``width:height`` and the new viewBox aspect
would make the rendered SVG letterbox inside its ``<img>`` box
under the default ``preserveAspectRatio="xMidYMid meet"``.

Non-SVG files in ``logos/`` (e.g. ``courage.png``, raster fallbacks
for vendors who only publish bitmap marks) are copied verbatim so
``logos/tight/`` is a complete mirror of every served logo. The
renderer reads from ``logos/tight/`` exclusively (see
:data:`investing.paths._REPO_LOGOS_DIR` / :data:`investing.paths.LOGOS_ADDRESS`),
which keeps the source originals in ``logos/`` untouched -- they are
the design artefact, never the served bytes.

The script is idempotent: running it twice in a row writes no files
on the second invocation. CI invokes it in ``--check`` mode to fail
when ``logos/tight/`` drifts from its sources; the
``regenerate-logos`` workflow runs it without ``--check`` on every
push that touches ``logos/<name>.{svg,png,jpg}`` and commits the
regenerated tight directory back to ``main`` so contributors who
forget to run the pre-commit hook don't ship a stale crop.

Usage::

    python scripts/tighten_logos.py            # regenerate logos/tight/
    python scripts/tighten_logos.py --check    # exit 1 on drift, write nothing
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

try:
    import cairosvg
except ImportError as exc:  # pragma: no cover - exercised only on a misconfigured env
    print(
        f"tighten_logos requires cairosvg: {exc}\n"
        "Install via ``pip install -r requirements.txt`` (cairosvg also "
        "needs the system-level ``libcairo2`` shared library).",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised only on a misconfigured env
    print(
        f"tighten_logos requires numpy: {exc}\nInstall via ``pip install -r requirements.txt``.",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - exercised only on a misconfigured env
    print(
        f"tighten_logos requires Pillow: {exc}\nInstall via ``pip install -r requirements.txt``.",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "logos"
TARGET_DIR = SOURCE_DIR / "tight"

# Extensions the renderer is willing to serve (see
# ``investing.paths.LOGO_EXTENSIONS``). Non-SVGs round-trip as a
# byte-for-byte copy so the tight directory is a self-sufficient
# mirror of every served logo, not just the croppable subset.
_SERVABLE_EXTENSIONS = {".svg", ".png", ".jpg", ".jpeg"}

# Rasterise at a resolution generous enough that the tight bbox lands
# on the exact pixel edge of the visible silhouette rather than the
# anti-aliased halo around it. 1024 squared is overkill for a 0..1
# bbox estimate (the count is stable up to a few permille at any
# resolution >= 256) but keeps memory + cairosvg time well under a
# second per logo while staying comfortably above the silhouette's
# stroke width on every committed logo.
_PROBE_SIZE = 1024

# Same alpha + whiteness cut-offs the treemap's density probe uses
# (see ``investing.logos._INK_OPACITY_THRESHOLD`` /
# ``_KNOCKOUT_WHITENESS_THRESHOLD``). The cropped bbox is then exactly
# the silhouette the treemap renders, so a logo's served tight crop
# and its measured ink density refer to the same visible mark.
_ALPHA_THRESHOLD = 128
_WHITENESS_THRESHOLD = 0.8

# Decimal places retained in the rewritten viewBox attribute. Four
# places is ~0.0001 viewBox units, which is well under a pixel at
# every container size we render, and keeps the diff readable.
_VIEWBOX_DECIMALS = 4

_VIEWBOX_RE = re.compile(r"viewBox\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_SVG_TAG_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_WIDTH_ATTR_RE = re.compile(r"\swidth\s*=\s*[\"'][^\"']*[\"']", re.IGNORECASE)
_HEIGHT_ATTR_RE = re.compile(r"\sheight\s*=\s*[\"'][^\"']*[\"']", re.IGNORECASE)
_NUMERIC_VALUE_RE = re.compile(r"[\d.]+")


def _format_viewbox(values: tuple[float, float, float, float]) -> str:
    """Format four floats as a viewBox value with trimmed trailing zeros."""

    def fmt(v: float) -> str:
        s = f"{v:.{_VIEWBOX_DECIMALS}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    return " ".join(fmt(v) for v in values)


def _parse_viewbox(svg_tag: str) -> tuple[float, float, float, float] | None:
    """Return the four viewBox floats, or None when absent / malformed."""
    m = _VIEWBOX_RE.search(svg_tag)
    if not m:
        return None
    parts = m.group(1).split()
    if len(parts) < 4:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return None


def _parse_width_height(svg_tag: str) -> tuple[float, float] | None:
    """Return the SVG root's numeric width/height, or None when missing."""
    w_m = _WIDTH_ATTR_RE.search(svg_tag)
    h_m = _HEIGHT_ATTR_RE.search(svg_tag)
    if not w_m or not h_m:
        return None
    w_val = _NUMERIC_VALUE_RE.search(w_m.group(0))
    h_val = _NUMERIC_VALUE_RE.search(h_m.group(0))
    if not w_val or not h_val:
        return None
    try:
        return float(w_val.group(0)), float(h_val.group(0))
    except ValueError:
        return None


def _tight_bounds_normalised(
    svg_path: Path, viewbox: tuple[float, float, float, float]
) -> tuple[float, float, float, float] | None:
    """Rasterise the SVG and return the tight content bbox in [0,1]^2.

    The raster dimensions are chosen to match the viewBox aspect ratio
    exactly so the rasterised image has *no* letterboxing: every raster
    pixel maps to a single viewBox coordinate via a uniform scale. That
    makes the conversion back to viewBox space the trivial
    ``raster_fraction * viewBox_extent``. Passing a square raster size
    (the obvious first instinct) silently triggers
    ``preserveAspectRatio="xMidYMid meet"`` and offsets the rendered
    content vertically, which would make the returned bbox land in the
    wrong place when it's mapped back.

    Returns ``None`` when cairosvg can't parse the file or no pixel
    survives the knockout filter (e.g. a logo whose visible content
    is pure white -- the knockout would erase it). Callers fall back
    to a verbatim copy in that case so the served set still includes
    the file.
    """
    _, _, vbw, vbh = viewbox
    if vbw <= 0 or vbh <= 0:
        return None
    aspect = vbw / vbh
    if aspect >= 1.0:
        out_w = _PROBE_SIZE
        out_h = max(1, round(_PROBE_SIZE / aspect))
    else:
        out_h = _PROBE_SIZE
        out_w = max(1, round(_PROBE_SIZE * aspect))
    try:
        png_bytes = cairosvg.svg2png(
            url=str(svg_path),
            output_width=out_w,
            output_height=out_h,
        )
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception:
        return None
    arr = np.asarray(img)
    if arr.size == 0:
        return None
    h, w = arr.shape[:2]
    alpha = arr[..., 3]
    rgb = arr[..., :3].astype(np.float32)
    whiteness = rgb.mean(axis=-1) / 255.0
    ink_mask = (alpha >= _ALPHA_THRESHOLD) & (whiteness < _WHITENESS_THRESHOLD)
    if not ink_mask.any():
        return None
    rows = np.where(ink_mask.any(axis=1))[0]
    cols = np.where(ink_mask.any(axis=0))[0]
    return (
        float(cols[0]) / w,
        float(rows[0]) / h,
        float(cols[-1] + 1) / w,
        float(rows[-1] + 1) / h,
    )


def _crop_svg(svg_path: Path) -> bytes | None:
    """Return the SVG text with a tight ``viewBox``, or None on failure."""
    text = svg_path.read_text(encoding="utf-8")
    svg_match = _SVG_TAG_RE.search(text)
    if not svg_match:
        return None
    svg_tag = svg_match.group(0)
    vb = _parse_viewbox(svg_tag)
    if vb is None:
        wh = _parse_width_height(svg_tag)
        if wh is None:
            return None
        vb = (0.0, 0.0, wh[0], wh[1])
    vbx, vby, vbw, vbh = vb
    bounds = _tight_bounds_normalised(svg_path, vb)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    new_x = vbx + x0 * vbw
    new_y = vby + y0 * vbh
    new_w = (x1 - x0) * vbw
    new_h = (y1 - y0) * vbh
    if new_w <= 0 or new_h <= 0:
        return None
    new_viewbox = _format_viewbox((new_x, new_y, new_w, new_h))

    rewritten_tag = svg_tag
    if _VIEWBOX_RE.search(rewritten_tag):
        rewritten_tag = _VIEWBOX_RE.sub(
            f'viewBox="{new_viewbox}"',
            rewritten_tag,
            count=1,
        )
    else:
        # Insert viewBox just before the closing ``>`` of the root tag.
        rewritten_tag = rewritten_tag[:-1].rstrip() + f' viewBox="{new_viewbox}">'

    rewritten_tag = _WIDTH_ATTR_RE.sub("", rewritten_tag, count=1)
    rewritten_tag = _HEIGHT_ATTR_RE.sub("", rewritten_tag, count=1)

    return (text[: svg_match.start()] + rewritten_tag + text[svg_match.end() :]).encode("utf-8")


def _expected_outputs() -> dict[Path, bytes]:
    """Build the (target path -> bytes) map the tight/ directory should hold."""
    outputs: dict[Path, bytes] = {}
    if not SOURCE_DIR.is_dir():
        return outputs
    for entry in sorted(SOURCE_DIR.iterdir()):
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix not in _SERVABLE_EXTENSIONS:
            continue
        target = TARGET_DIR / entry.name
        if suffix == ".svg":
            cropped = _crop_svg(entry)
            outputs[target] = cropped if cropped is not None else entry.read_bytes()
        else:
            outputs[target] = entry.read_bytes()
    return outputs


def _existing_target_files() -> list[Path]:
    """Return the files currently under ``logos/tight/`` (one level deep)."""
    if not TARGET_DIR.is_dir():
        return []
    return [p for p in TARGET_DIR.iterdir() if p.is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Compare the freshly-built tight outputs against the committed "
            "files. Exit 1 (without writing or deleting anything) if any differ."
        ),
    )
    args = parser.parse_args()

    outputs = _expected_outputs()
    valid_names = {p.name for p in outputs}

    drift: list[Path] = []
    for path, body in outputs.items():
        existing = path.read_bytes() if path.exists() else None
        if existing == body:
            continue
        drift.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    obsolete = [p for p in _existing_target_files() if p.name not in valid_names]

    if args.check:
        if drift or obsolete:
            print(
                "Tight logos are stale -- run ``python scripts/tighten_logos.py``:",
                file=sys.stderr,
            )
            for path in drift:
                print(f"  drift: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            for path in obsolete:
                print(f"  obsolete: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            return 1
        print("Tight logos are up to date.")
        return 0

    for path in obsolete:
        path.unlink()
    if drift or obsolete:
        if drift:
            print(f"Wrote {len(drift)} tight logo(s):")
            for path in drift:
                print(f"  {path.relative_to(REPO_ROOT)}")
        if obsolete:
            print(f"Removed {len(obsolete)} obsolete tight logo(s):")
            for path in obsolete:
                print(f"  {path.relative_to(REPO_ROOT)}")
    else:
        print("Tight logos already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
