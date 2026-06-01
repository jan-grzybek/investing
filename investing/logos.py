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
        self._session = session or _build_session()
        self._local_dir = local_dir

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
