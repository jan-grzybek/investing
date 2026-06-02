"""Manual sector overrides for tickers without a yfinance sector.

The equities treemap (see :mod:`investing.webpage.sector_treemap`)
groups holdings by ``info["sector"]`` -- the GICS-style label
yfinance returns for most listed equities. A handful of exotic
instruments (some ADRs, recently listed names whose Yahoo profile
hasn't been populated yet, certain ETFs / closed-end funds) come
back with a blank string, which lands them in the renderer's
neutral "Other" bucket.

This module exposes a small fallback so a maintainer can pin those
tickers to a real sector without patching code:

  * :data:`KNOWN_SECTORS` -- canonical sectors the treemap palette
    recognises. Kept in sync with the swatch table in
    :mod:`investing.webpage.sector_treemap`; an override using any
    other value is rejected (with a maintenance hint logged) and the
    ticker falls back to "Other".
  * :func:`resolve_sector` -- ``(ticker, yfinance_sector) -> str``.
    Returns the yfinance value when it's a real sector, otherwise
    the override from ``sector_overrides.toml``, otherwise the empty
    string. Records a maintenance hint whenever the empty-string
    case fires so the build summary can prompt the maintainer to add
    a manual entry.

The hint registry is process-scoped: :func:`reset_hints` clears it
(called at the start of every ``build_page`` run), :func:`record_*`
helpers populate it, and :func:`consume_hints` drains and returns
the accumulated entries for emission alongside the curated build
summary. Ticker symbols are public (they appear in the rendered
page), so emitting them on the real stdout is safe even under the
leak-safe wrapper.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from .log import logger
from .paths import _SECTOR_OVERRIDES_PATH

# Canonical yfinance / GICS-style sector labels recognised by the
# treemap palette. Kept as a ``frozenset`` so callers can do
# ``sector in KNOWN_SECTORS`` cheaply without an inadvertent mutation
# changing the validation surface for the rest of the build. The
# values intentionally duplicate the keys in
# :data:`investing.webpage.sector_treemap._SECTOR_VARS` (minus the
# ``"Other"`` sentinel which is the fallback, not a real sector);
# importing from there would create a cycle (the treemap renderer
# imports from this module via the holdings pipeline) so the list is
# repeated here. A new sector would need to be added in both places
# at once, which is fine -- the alternative pulls a renderer-side
# detail back into the data layer.
KNOWN_SECTORS: frozenset[str] = frozenset(
    {
        "Basic Materials",
        "Communication Services",
        "Consumer Cyclical",
        "Consumer Defensive",
        "Energy",
        "Financial Services",
        "Healthcare",
        "Industrials",
        "Real Estate",
        "Technology",
        "Utilities",
    }
)


@dataclass(frozen=True)
class MaintenanceHints:
    """Accumulated maintenance hints from a single build.

    ``missing_sector`` -- tickers whose yfinance sector was empty AND
    have no entry in ``sector_overrides.toml``. The maintainer should
    add an entry mapping each ticker to one of :data:`KNOWN_SECTORS`.

    ``invalid_overrides`` -- ``ticker -> value`` pairs from the TOML
    file whose value isn't a recognised sector (typo, casing,
    removed-from-GICS, etc.). The bad entry is ignored and the
    ticker falls back to the empty-sector path.

    ``missing_logos`` -- tickers whose logo resolution fell all the
    way through to the courage placeholder. Populated by
    :class:`investing.logos.LogoCache` via
    :func:`record_missing_logo`. Lives here (rather than in
    ``logos.py``) so the build summary has a single place to drain
    every maintenance hint from.
    """

    missing_sector: list[str] = field(default_factory=list)
    invalid_overrides: dict[str, str] = field(default_factory=dict)
    missing_logos: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.missing_sector or self.invalid_overrides or self.missing_logos)


# Process-scoped registries. Reset at the start of every
# :func:`investing.cli.build_page` run so a long-lived process (test
# suite, preview script) doesn't accumulate stale entries across
# unrelated builds. Sets keep the ordering insensitive to the order
# tickers are processed by upstream code (which depends on the
# Google Sheets row order in production).
_missing_sectors: set[str] = set()
_invalid_overrides: dict[str, str] = {}
_missing_logos: set[str] = set()


def reset_hints() -> None:
    """Clear every accumulated maintenance hint.

    The CLI calls this at the start of :func:`build_page` so a fresh
    run starts with an empty slate. Tests that exercise the recorders
    directly should also call this in ``setup`` / ``teardown`` so
    module-level state does not bleed across test cases.
    """
    _missing_sectors.clear()
    _invalid_overrides.clear()
    _missing_logos.clear()


def record_missing_logo(ticker: str) -> None:
    """Flag ``ticker`` as missing a hand-curated logo file.

    Called by :meth:`investing.logos.LogoCache.__call__` the first
    time a ticker's resolution falls through to the courage
    placeholder. The call site already short-circuits on the cache,
    so this function is invoked at most once per ticker per build --
    but :func:`set.add` is idempotent so a defensive double-call is
    harmless. Emits a single ``logger.warning`` per ticker (visible
    in local dev runs; CI redacts stderr but the hint is also
    surfaced via the build summary).
    """
    if ticker in _missing_logos:
        return
    _missing_logos.add(ticker)
    logger.warning(
        "no logo file for ticker %s; add a hand-curated SVG / PNG / JPG "
        "to ``logos/`` (build will pick it up via the tighten-logos "
        "pre-commit hook and ``LogoCache``'s local-first probe)",
        ticker,
    )


def consume_hints() -> MaintenanceHints:
    """Return + clear every accumulated maintenance hint.

    Used by :func:`investing.cli._print_summary` to roll the hints
    into the curated build summary line. ``consume`` rather than a
    plain read so a second call within the same build doesn't
    surface the same hints twice (the CLI's summary is emitted
    exactly once per ``build_page`` invocation, but the explicit
    "drain" contract keeps test setups easy to reason about: each
    test asserts on the hints from its own action and the next test
    starts clean).
    """
    hints = MaintenanceHints(
        missing_sector=sorted(_missing_sectors),
        invalid_overrides=dict(_invalid_overrides),
        missing_logos=sorted(_missing_logos),
    )
    reset_hints()
    return hints


# ---------------------------------------------------------------------------
# Overrides loader
# ---------------------------------------------------------------------------


# Cache for the parsed TOML payload. Held as the sole attribute of
# a tiny module-level container so reads and writes go through
# attribute access -- this avoids a ``global`` statement (whose
# write-side CodeQL fails to link back to the same-function read,
# producing a spurious ``py/unused-global-variable`` note for each
# assignment) and keeps the cache contract obvious at a glance.
# ``value is None`` is the unset sentinel (an empty file legitimately
# parses to an empty dict and we don't want that to trigger a re-read
# on every call). The cache is in-process; tests that rewrite the
# file under a temp path should call :func:`_clear_overrides_cache`
# so the next read picks up the new bytes.
class _OverridesCache:
    value: dict[str, str] | None = None


def _clear_overrides_cache() -> None:
    """Drop the parsed TOML cache so the next read re-loads from disk.

    Production code never needs this -- the overrides file is read
    once per process lifetime. Tests that exercise the loader
    against a temp file call it between runs so a fresh fixture
    doesn't see a stale parse from a previous test case.
    """
    _OverridesCache.value = None


def _load_overrides(path: str | None = None) -> dict[str, str]:
    """Read and validate the overrides TOML file.

    Returns a ``{ticker: sector}`` dict containing only entries
    whose sector is in :data:`KNOWN_SECTORS`. Invalid entries are
    dropped (the renderer would otherwise emit a tile against the
    "Other" swatch anyway, since :func:`_sector_color` falls back
    there for unknown sectors) and recorded as
    :class:`MaintenanceHints.invalid_overrides` so the build
    summary surfaces the typo.

    ``path`` defaults to :data:`_SECTOR_OVERRIDES_PATH` so the
    production callsite stays argument-free; tests pass an explicit
    temp file path. A missing file is treated as an empty override
    set (a fresh fork without the TOML present should still build
    cleanly); a malformed file logs a warning and falls back to the
    same empty-set behaviour rather than crashing the entire render.
    """
    cached = _OverridesCache.value
    if cached is not None and path is None:
        return cached

    effective_path = path if path is not None else _SECTOR_OVERRIDES_PATH
    parsed: dict[str, str] = {}

    if not os.path.exists(effective_path):
        if path is None:
            _OverridesCache.value = parsed
        return parsed

    try:
        with open(effective_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "failed to read sector overrides at %s (%s); falling back to "
            "empty override set",
            effective_path,
            type(exc).__name__,
        )
        if path is None:
            _OverridesCache.value = parsed
        return parsed

    raw = data.get("sectors")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning(
                "sector overrides file %s has a non-table ``[sectors]`` "
                "entry; ignoring overrides",
                effective_path,
            )
        if path is None:
            _OverridesCache.value = parsed
        return parsed

    for ticker, sector in raw.items():
        if not isinstance(ticker, str) or not isinstance(sector, str):
            # TOML's type system can carry through ints / arrays /
            # tables that wouldn't satisfy the renderer; reject
            # silently rather than crashing the build on a typo'd
            # value, the recorder picks up the discarded entry below
            # via the same code path as a misspelled sector name.
            _invalid_overrides[str(ticker)] = repr(sector)
            continue
        if sector not in KNOWN_SECTORS:
            _invalid_overrides[ticker] = sector
            continue
        parsed[ticker] = sector

    if path is None:
        _OverridesCache.value = parsed
    return parsed


def resolve_sector(
    ticker: str,
    yfinance_sector: str,
    *,
    overrides_path: str | None = None,
) -> str:
    """Return the effective sector for ``ticker``.

    Priority order:

    1. ``yfinance_sector`` when it's a non-empty string. yfinance
       stays the source of truth whenever it has data, so a ticker
       that grows a sector upstream automatically falls off the
       override file's effective surface even if a stale entry
       lingers.
    2. The matching entry from ``sector_overrides.toml`` when the
       yfinance value is blank. The override has already been
       validated against :data:`KNOWN_SECTORS` at load time, so a
       hit here is guaranteed to be a canonical sector.
    3. The empty string ``""``, plus a maintenance hint recorded via
       :func:`record_missing_sector` so the build summary prompts
       the maintainer to either add an override or wait for yfinance
       to fill the gap. The downstream renderer maps an empty
       string into the ``"Other"`` bucket sentinel.

    ``overrides_path`` is the test injection hook; production
    callsites omit it and pick up :data:`_SECTOR_OVERRIDES_PATH`
    from the module-level loader.
    """
    cleaned = yfinance_sector.strip()
    if cleaned:
        return cleaned
    overrides = _load_overrides(overrides_path)
    pinned = overrides.get(ticker)
    if pinned is not None:
        return pinned
    record_missing_sector(ticker)
    return ""


def record_missing_sector(ticker: str) -> None:
    """Flag ``ticker`` as missing both an upstream sector and an override.

    Kept as a public helper (rather than inlined into
    :func:`resolve_sector`) so :func:`consume_hints`'s contract --
    "exactly the tickers that need a manual entry" -- doesn't depend
    on the resolver being the only entry point. Idempotent: the
    set-based registry naturally collapses repeat calls. The
    accompanying ``logger.warning`` fires once per ticker per build
    (the second call short-circuits via the membership check) so
    local-dev terminal output stays scannable on portfolios with
    many missing-sector tickers.
    """
    if ticker in _missing_sectors:
        return
    _missing_sectors.add(ticker)
    logger.warning(
        "no sector for ticker %s; yfinance returned a blank value and "
        "no override is present in ``sector_overrides.toml``. The "
        "treemap will group this ticker under the neutral ``Other`` "
        "tile -- add an entry under ``[sectors]`` to pin a real sector.",
        ticker,
    )


# ---------------------------------------------------------------------------
# Auto-populate hook
# ---------------------------------------------------------------------------


def append_missing_sector_stubs(
    tickers: list[str],
    *,
    path: str | None = None,
) -> list[str]:
    """Append commented-out override stubs for ``tickers`` to the TOML
    file at ``path``.

    For each ticker not already mentioned anywhere in the file (open
    or commented), the function appends a small block of the shape::

        # Auto-detected: missing sector for "NMS:FISV". Uncomment the
        # next line and replace "" with one of the canonical sectors
        # documented at the top of this file (e.g. "Technology").
        # "NMS:FISV" = ""

    The maintainer's editing flow is then two keystrokes per ticker:
    delete the leading ``# `` from the data line and type a sector
    name inside the empty string.

    Why "commented-out" rather than "active": an empty string fails
    the :data:`KNOWN_SECTORS` validation in :func:`_load_overrides`
    and would itself record an ``invalid_overrides`` hint on the very
    next build, defeating the point of writing the stub. Leaving the
    line commented keeps the file parseable until the maintainer
    explicitly opts each ticker in.

    The function is a strict no-op when ``path`` doesn't exist (a
    fresh fork without the TOML file should still build cleanly) or
    when ``tickers`` is empty. Tickers already mentioned in the file
    -- by exact ``"TICKER"`` substring match, which catches both
    active entries and the auto-appended commented stubs -- are
    skipped so re-running the build is idempotent.

    ``path`` defaults to :data:`_SECTOR_OVERRIDES_PATH` so production
    callsites stay argument-free; tests pass an explicit temp file.

    Returns the list of tickers actually appended (in input order),
    primarily so the build summary can mention them on the curated
    stdout stream. The ``[]`` return on a no-op makes the caller's
    "did we mutate the file" predicate trivial.
    """
    if not tickers:
        return []
    effective_path = path if path is not None else _SECTOR_OVERRIDES_PATH
    if not os.path.exists(effective_path):
        # Don't conjure a TOML file out of thin air -- the maintainer
        # may have deliberately removed it (a fork with no overrides
        # needed). Logging would be noisy here so we stay silent;
        # the file's absence already short-circuits ``_load_overrides``
        # in the same module.
        return []

    try:
        with open(effective_path, encoding="utf-8") as f:
            existing = f.read()
    except OSError as exc:
        logger.warning(
            "sector overrides auto-populate: failed to read %s (%s); "
            "skipping stub append",
            effective_path,
            type(exc).__name__,
        )
        return []

    appended: list[str] = []
    blocks: list[str] = []
    for ticker in tickers:
        # Substring match against the quoted form catches both an
        # active entry (``"NMS:FISV" = "Technology"``) and a
        # previously-appended commented stub (``# "NMS:FISV" = ""``).
        # The quotes anchor the match so a ticker that's a substring
        # of another (``"NMS:A"`` vs ``"NMS:AAA"``) doesn't false-
        # positive.
        needle = f'"{ticker}"'
        if needle in existing:
            continue
        blocks.append(_format_sector_stub(ticker))
        appended.append(ticker)

    if not appended:
        return []

    # Always lead with a blank line so the appended block visually
    # separates from whatever the file currently ends with (which
    # might be the header's example line, an earlier active entry,
    # or another auto-appended stub). ``rstrip`` + ``\n\n`` is
    # idempotent: re-running with the same input is a no-op (the
    # needle check above bails before we reach the write).
    new_tail = "\n\n" + "\n\n".join(blocks) + "\n"
    try:
        with open(effective_path, "a", encoding="utf-8") as f:
            f.write(new_tail)
    except OSError as exc:
        logger.warning(
            "sector overrides auto-populate: failed to append to %s (%s); "
            "the stub for %s was NOT written",
            effective_path,
            type(exc).__name__,
            ", ".join(appended),
        )
        return []
    return appended


def _format_sector_stub(ticker: str) -> str:
    """Render the per-ticker commented stub block.

    Kept as a tiny helper so the exact wording lives in one place
    -- the maintainer's editing workflow depends on the "delete
    ``# `` then fill in the sector" gesture being uniform across
    every auto-appended block, and tests can pin the wording
    without duplicating the format string.
    """
    return (
        f"# Auto-detected: missing sector for {ticker!r}. Uncomment the\n"
        f"# next line and replace \"\" with one of the canonical sectors\n"
        f"# documented at the top of this file (e.g. \"Technology\").\n"
        f'# "{ticker}" = ""'
    )
