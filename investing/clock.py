"""Time-injection plumbing shared across the page generator.

Modules that need "now" historically called ``datetime.today()``
directly on their own bound ``datetime`` symbol. The test suite then
froze time with a ``freeze_today`` fixture that walked the package
swapping ``datetime`` on every module it knew about -- and silently
broke any module that joined the package without being added to the
fixture's list.

The replacement is an optional ``now`` parameter on every public
entrypoint that reads the clock. Production code never passes it
(the default falls through to ``datetime.today()`` so the legacy
fixture still works) but tests can plumb an explicit closure rather
than reach for a cross-module monkeypatch.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

# Zero-arg callable returning a naive ``datetime``. Kept as a public
# alias so the renderer / pipeline functions can spell their
# signatures uniformly without each importing typing machinery.
NowFn = Callable[[], datetime]


def default_now() -> datetime:
    """The clock every ``now``-taking entrypoint falls back to.

    ``now if now is not None else datetime.today`` was spelled out at
    seven call sites, which meant this module exported a type alias and
    nothing else -- the abstraction named the concept without owning
    it. Callers now resolve through :func:`resolve_now`, and anything
    reading the clock without a ``now`` parameter (the manifest
    timestamp in :mod:`investing.market_data_store`) calls this
    directly rather than reaching for ``datetime`` itself.
    """
    return datetime.today()


def resolve_now(now: NowFn | None) -> NowFn:
    """Return ``now``, or :func:`default_now` when it is ``None``."""
    return now if now is not None else default_now
