"""Logo URL resolution against the deployed ``logos/`` directory.

Each ``Holding`` is rendered alongside the issuer's logo, served as a
sibling of ``index.html`` under :data:`investing.paths.LOGOS_ADDRESS`.
Files there are committed by hand in one of the
:data:`investing.paths.LOGO_EXTENSIONS` formats. The build runs in
the same repo that commits those files, so the resolver checks the
local mirror at :data:`investing.paths._REPO_LOGOS_DIR` first and
only falls back to an HTTP probe when no local file is present --
that path is needed when a logo is added to the repo but its asset
hasn't been deployed yet, or when the resolver runs from a working
copy whose ``logos/`` directory is intentionally empty (e.g. a
preview script in a temp dir). A miss across both paths falls back
to :data:`investing.paths.COURAGE_LOGO`.

This module exists separately from the page renderer so the HTTP
plumbing (session reuse, retries, timeouts) lives in one place rather
than scattered through whatever module happens to need a logo URL.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .paths import _REPO_LOGOS_DIR, COURAGE_LOGO, LOGO_EXTENSIONS, LOGOS_ADDRESS

# Per-request budget for a single HEAD probe (connect, read). Pages
# responds in milliseconds when the file exists; on a stalled run we
# would rather fall back to the placeholder than hang the entire CI
# build.
_HEAD_TIMEOUT_S: tuple[float, float] = (3.0, 3.0)


# Default aspect ratio (width / height) used when a logo's intrinsic
# proportions can't be parsed. 3 : 1 is the median for the portfolio's
# wordmark distribution (most company wordmarks land between 2.5 : 1
# and 4 : 1), so a missing data point degrades gracefully to "looks
# like a typical wordmark" rather than forcing the renderer into a
# square cell that would penalise the common case.
_DEFAULT_LOGO_ASPECT: float = 3.0

# Default ink-density (= fraction of the rasterised bounding box
# whose pixels survive the treemap's white-knockout filter; see
# :func:`_measure_svg_density`). Used when a logo's coverage can't
# be measured -- missing file, non-SVG format, cairosvg failure.
# 0.10 is a touch above the measured median of the current portfolio
# (~0.09) so a logo without measurement defaults to "the typical
# wordmark mass" and the density-scale knob in the treemap renderer
# (``_LOGO_REFERENCE_DENSITY`` in ``sector_treemap.py``) sees a
# neutral input rather than a value that systematically over- or
# under-sizes the missing-data case.
_DEFAULT_LOGO_DENSITY: float = 0.10

# Pixel-level thresholds the rasterised density measurement uses to
# decide whether a source pixel will *survive* the SVG knockout
# filter in ``page.css``. The filter (see ``_LOGO_KNOCKOUT_FILTER_ID``
# in ``sector_treemap.py``) turns every visible pixel opaque-white
# and then subtracts a knockout mask whose alpha = whiteness ramped
# steeply from 0 at whiteness=0.8 to 1 at whiteness=1.0. The two
# constants below pin the same cut-offs in the measurement pass so
# the density we compute matches the silhouette the eye actually
# sees on the treemap tile:
#
#   * ``_INK_OPACITY_THRESHOLD`` (0..255) -- source pixels below
#     this alpha are treated as fully transparent (they wouldn't
#     contribute to the silhouette).
#   * ``_KNOCKOUT_WHITENESS_THRESHOLD`` (0..1) -- source pixels at
#     or above this average-RGB whiteness are treated as fully
#     knocked-out (they would survive the silhouette pass but get
#     removed by the knockout mask before reaching the screen).
_INK_OPACITY_THRESHOLD: int = 128
_KNOCKOUT_WHITENESS_THRESHOLD: float = 0.8

# Pixel grid used when rasterising the SVG for the density probe.
# 128 x 128 is plenty for a 0..1 coverage estimate (the count is the
# same up to a few permille at any reasonable resolution), keeps the
# memory footprint trivial (~64 KiB per RGBA frame), and renders in
# single-digit milliseconds via cairosvg -- so the production build
# absorbs the whole portfolio's density probe in well under a
# second, cached for the rest of the page render.
_DENSITY_PROBE_SIZE: int = 128


def _measure_svg_density(svg_path: str) -> float | None:
    """Return the fraction of the SVG's bounding box that survives the knockout filter.

    Rasterises the SVG at :data:`_DENSITY_PROBE_SIZE` with a
    transparent background, then counts the fraction of pixels that
    are *both* opaque enough (alpha >= ``_INK_OPACITY_THRESHOLD``)
    and *not* near-pure-white (whiteness < ``_KNOCKOUT_WHITENESS_THRESHOLD``).
    The remaining pixels mirror the white silhouette the treemap's
    SVG knockout filter produces, so dividing by the canvas area
    yields the visible "ink coverage" of the rendered logo.

    Returns ``None`` when cairosvg can't parse the file or any other
    runtime error blocks the measurement -- callers can substitute
    :data:`_DEFAULT_LOGO_DENSITY` so the renderer always has a
    usable value.
    """
    try:
        import io

        import cairosvg
        import numpy as np
        from PIL import Image

        png_bytes = cairosvg.svg2png(
            url=svg_path,
            output_width=_DENSITY_PROBE_SIZE,
            output_height=_DENSITY_PROBE_SIZE,
        )
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception:
        return None
    arr = np.asarray(img)
    if arr.size == 0:
        return None
    alpha = arr[..., 3]
    rgb = arr[..., :3].astype(np.float32)
    whiteness = rgb.mean(axis=-1) / 255.0
    opaque = alpha >= _INK_OPACITY_THRESHOLD
    not_white = whiteness < _KNOCKOUT_WHITENESS_THRESHOLD
    ink_mask = opaque & not_white
    return float(ink_mask.sum()) / float(ink_mask.size)


def _parse_svg_aspect_ratio(svg_text: str) -> float | None:
    """Extract width / height aspect from an SVG document.

    Tries ``viewBox`` first (the canonical sizing source -- the four
    space-separated numbers are ``min-x min-y width height`` per the
    SVG spec) and falls back to a top-level ``width="..." height=
    "..."`` attribute pair. Returns ``None`` when neither shape can
    be parsed cleanly so callers can substitute their own default.

    The parser is intentionally regex-based rather than running a
    full XML parse: every logo in the repo is a hand-curated SVG
    whose root element starts with a single ``<svg ...>`` tag, so
    the cost of pulling in ``xml.etree.ElementTree`` per logo is
    not worth it. ``re.search`` finds the first match anywhere in
    the document, which handles SVGs that prefix their root with
    XML declarations / DOCTYPEs / comments without special-casing.
    """
    m = re.search(r"viewBox\s*=\s*[\"']([^\"']+)[\"']", svg_text)
    if m:
        parts = m.group(1).split()
        if len(parts) >= 4:
            try:
                w = float(parts[2])
                h = float(parts[3])
            except ValueError:
                w = h = 0.0
            if w > 0 and h > 0:
                return w / h
    w_m = re.search(r"\bwidth\s*=\s*[\"']?([\d.]+)", svg_text)
    h_m = re.search(r"\bheight\s*=\s*[\"']?([\d.]+)", svg_text)
    if w_m and h_m:
        try:
            w = float(w_m.group(1))
            h = float(h_m.group(1))
        except ValueError:
            return None
        if w > 0 and h > 0:
            return w / h
    return None


# Retry policy: three attempts with exponential back-off on transient
# server errors. The ``LOGOS_ADDRESS`` endpoint is GitHub Pages, which
# very occasionally 5xxs during deploy churn -- a fixed-budget retry
# absorbs that without escalating to the placeholder.
_RETRY_POLICY = Retry(
    total=2,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset({"HEAD"}),
    raise_on_status=False,
)


def _build_session() -> requests.Session:
    """Create a session pre-wired with the retry policy above."""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_RETRY_POLICY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Type alias for "anything callable as ``resolver(ticker) -> str``".
# A plain function, a :class:`LogoCache` instance and a test stub all
# satisfy it; using a ``Callable`` alias rather than a ``Protocol``
# means mypy accepts every shape of callable (lambda, function,
# ``LogoCache``, partial, etc.) without each one having to name its
# parameter ``ticker`` to match a protocol's positional-or-keyword
# spelling.
type LogoResolver = Callable[[str], str]


class LogoCache:
    """Caches resolved (or missing) logo URLs for the lifetime of one page render.

    Resolution strategy:

    1. **Local filesystem probe** against :data:`_REPO_LOGOS_DIR`.
       The build runs from the repo that commits the logo files, so
       a hit here yields a sub-millisecond resolution that doesn't
       depend on the previous deploy being reachable. This is the
       normal production path.
    2. **HTTP HEAD fallback** against :data:`LOGOS_ADDRESS` when no
       local file matches any of the configured extensions. Needed
       when the resolver runs against a working copy whose
       ``logos/`` directory is empty (preview scripts in temp dirs)
       or when a freshly-added logo lives only on the previous
       deploy.

    Caches BOTH hits and misses: an outage that returns 404 for every
    extension previously triggered ``len(LOGO_EXTENSIONS)`` HEADs per
    ticker per render call. Caching the fallback eliminates the fan-out
    on the second lookup of the same ticker (which happens in
    ``add_holding`` for the marquee + the card body).

    A session is held open across all lookups so connection reuse is
    automatic; ``requests.head`` per-call would re-establish TLS for
    every probe.

    ``local_dir`` defaults to the repo's mirror but can be overridden
    (or set to ``None``) to disable the filesystem probe entirely --
    the existing test suite plants a mock session that asserts on
    HEAD calls, and pointing ``local_dir`` at an empty path is the
    cleanest way to make those expectations still hold under the new
    local-first contract.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        local_dir: str | None = _REPO_LOGOS_DIR,
    ):
        self._cache: dict[str, str] = {}
        # Separate cache for parsed aspect ratios so a hit on the URL
        # lookup doesn't force a second filesystem read for the
        # aspect probe (and vice versa). Both caches share the same
        # ticker keyspace.
        self._aspect_cache: dict[str, float] = {}
        # Same pattern for the rasterised ink-density probe used by
        # the treemap's equal-visual-area logo sizing pass; see
        # :meth:`coverage_ratio`.
        self._density_cache: dict[str, float] = {}
        self._session = session or _build_session()
        self._local_dir = local_dir

    def aspect_ratio(self, ticker: str) -> float:
        """Return the logo's intrinsic aspect ratio (width / height).

        Parses the local SVG when one is present (the canonical
        production path -- every committed logo lives in the repo's
        ``logos/`` mirror and ``LogoCache.__call__`` will have already
        resolved it). For non-SVG formats, missing files, or
        parse failures, returns :data:`_DEFAULT_LOGO_ASPECT` so
        the renderer always has a usable value.

        The aspect ratio is what drives the treemap's *equal-area*
        logo sizing: each logo's rendered width / height is scaled
        by ``sqrt(R / R_ref)`` / ``sqrt(R_ref / R)`` against a
        reference aspect, which keeps ``width * height`` constant
        across logos at any given container size. See
        :mod:`investing.webpage.sector_treemap` for the consumer.
        """
        cached = self._aspect_cache.get(ticker)
        if cached is not None:
            return cached
        aspect = _DEFAULT_LOGO_ASPECT
        if self._local_dir is not None:
            path = os.path.join(self._local_dir, f"{ticker}.svg")
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    text = ""
                parsed = _parse_svg_aspect_ratio(text)
                if parsed is not None:
                    aspect = parsed
        self._aspect_cache[ticker] = aspect
        return aspect

    def coverage_ratio(self, ticker: str) -> float:
        """Return the logo's visible-ink coverage (0..1).

        Rasterises the local SVG (when one exists) and counts the
        fraction of pixels that survive the treemap's SVG knockout
        filter -- i.e. the area the eye actually reads as "the logo"
        on a coloured tile. The metric feeds the treemap's
        equal-VISUAL-area sizing pass: each logo's bounding box is
        scaled so the rendered white silhouette covers approximately
        the same pixel area across brands, which makes a thin-stroke
        wordmark (NVIDIA, Alphabet) and a solid-mass icon
        (Salesforce, TSM) read as comparably-sized marks rather than
        the icon overwhelming the wordmark at the same bbox size.

        For non-SVG formats, missing files, or rasterisation
        failures returns :data:`_DEFAULT_LOGO_DENSITY` so the
        renderer always has a usable value. See
        :mod:`investing.webpage.sector_treemap` for the consumer
        side -- the reference density and the min / max scale
        clamps that turn this raw coverage into an actual size
        multiplier live there.
        """
        cached = self._density_cache.get(ticker)
        if cached is not None:
            return cached
        density = _DEFAULT_LOGO_DENSITY
        if self._local_dir is not None:
            path = os.path.join(self._local_dir, f"{ticker}.svg")
            if os.path.exists(path):
                measured = _measure_svg_density(path)
                if measured is not None:
                    density = measured
        self._density_cache[ticker] = density
        return density

    def __call__(self, ticker: str) -> str:
        cached = self._cache.get(ticker)
        if cached is not None:
            return cached
        encoded = ticker.replace(":", "%3A")
        # Local-first probe. The legacy implementation reached for
        # the previous deploy of GitHub Pages on every fresh ticker
        # (a network round-trip per extension, with a configured
        # retry policy on top), even though the build itself runs
        # in the repo that ships the logo files. ``os.path.exists``
        # in the same working tree is two-to-three orders of
        # magnitude faster and removes the deploy dependency, so
        # an "added a logo, deploying for the first time" workflow
        # no longer falls through to the courage fallback.
        if self._local_dir is not None:
            for extension in LOGO_EXTENSIONS:
                if os.path.exists(os.path.join(self._local_dir, f"{ticker}{extension}")):
                    url = LOGOS_ADDRESS + encoded + extension
                    self._cache[ticker] = url
                    return url
        for extension in LOGO_EXTENSIONS:
            url = LOGOS_ADDRESS + encoded + extension
            try:
                response = self._session.head(url, timeout=_HEAD_TIMEOUT_S)
            except requests.RequestException:
                # Network-level failure (DNS, refused, timeout that
                # exhausted retries). Drop to the next extension; if
                # they all fail we'll cache the placeholder.
                continue
            if response.status_code == 200:
                self._cache[ticker] = url
                return url
        self._cache[ticker] = COURAGE_LOGO
        return COURAGE_LOGO
