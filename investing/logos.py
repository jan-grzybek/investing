"""Logo URL resolution against the deployed ``logos/`` directory.

Each ``Holding`` is rendered alongside the issuer's logo, served as a
sibling of ``index.html`` under :data:`investing.paths.LOGOS_ADDRESS`.
Files there are committed by hand in one of the
:data:`investing.paths.LOGO_EXTENSIONS` formats, so we probe each
extension in turn and use the first one that responds 200. A miss
falls back to :data:`investing.paths.COURAGE_LOGO`.

This module exists separately from the page renderer so the HTTP
plumbing (session reuse, retries, timeouts) lives in one place rather
than scattered through whatever module happens to need a logo URL.
"""
from __future__ import annotations

from typing import Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .paths import COURAGE_LOGO, LOGO_EXTENSIONS, LOGOS_ADDRESS

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


class LogoResolver(Protocol):
    def __call__(self, ticker: str) -> str: ...


class LogoCache:
    """Caches resolved (or missing) logo URLs for the lifetime of one page render.

    Caches BOTH hits and misses: an outage that returns 404 for every
    extension previously triggered ``len(LOGO_EXTENSIONS)`` HEADs per
    ticker per render call. Caching the fallback eliminates the fan-out
    on the second lookup of the same ticker (which happens in
    ``add_holding`` for the marquee + the card body).

    A session is held open across all lookups so connection reuse is
    automatic; ``requests.head`` per-call would re-establish TLS for
    every probe.
    """

    def __init__(self, *, session: requests.Session | None = None):
        self._cache: dict[str, str] = {}
        self._session = session or _build_session()

    def __call__(self, ticker: str) -> str:
        cached = self._cache.get(ticker)
        if cached is not None:
            return cached
        encoded = ticker.replace(":", "%3A")
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
