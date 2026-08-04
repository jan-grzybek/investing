"""Maintainer-curated grouping of several tickers into one position.

A single company can reach a portfolio through more than one
instrument: a primary listing plus a depositary receipt, a dual
listing on a second exchange, or two share classes. Samsung
Electronics is the worked example -- it can be held as ``DUS:SSU.DU``
(Düsseldorf) and as ``LSE:SMSN.IL`` (the London GDR) at the same time.

Left alone, the pipeline reports those as two independent holdings:
two holdings rows, two rows in the top-10 weights. The
reader sees two half-sized stakes in the same company rather than one
whole one. This module lets a maintainer declare that the legs are one
position, without touching code:

    [samsung]
    primary = "DUS:SSU.DU"
    members = ["LSE:SMSN.IL"]
    name    = "Samsung Electronics"
    label   = "Samsung"

``primary`` is the leg whose identity the combined position inherits
-- logo file, anchor id, issuer website, sector, asset class. It is
also the key the portfolio weights map is written under, so it must be
a ticker the portfolio actually holds.

``members`` lists the remaining legs. ``primary`` may be repeated
there harmlessly; it is deduplicated.

``name`` overrides the display name. Without it the primary's
yfinance ``longName`` is used, which is often the *listing's* name
rather than the company's ("... GDR", "... SPONSORED ADR").

``label`` is the compact form used where space is tight -- currently
compact surfaces such as the OG card. It defaults to the primary's symbol
with the exchange stripped, which is the right answer whenever one leg
is the recognisable one (holding ``NMS:GOOGL`` alongside ``NMS:GOOG``
should just read ``GOOGL``). Samsung is the case that needs the
override: neither ``SSU.DU`` nor ``SMSN.IL`` reads as Samsung to
anyone scanning the chart.

Validation is deliberately lenient about *absence* and strict about
*contradiction*. A missing file, or a group naming tickers the
portfolio doesn't hold, degrades to "no grouping" so a fork or a
partially-sold portfolio still builds; a ticker claimed by two groups
is a genuine authoring mistake that would silently drop a position, so
it raises.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from .errors import InvariantError
from .log import logger
from .paths import _POSITION_GROUPS_PATH


@dataclass(frozen=True)
class PositionGroup:
    """One declared multi-ticker position."""

    key: str
    primary: str
    members: tuple[str, ...]
    name: str | None = None
    label: str | None = None

    @property
    def tickers(self) -> tuple[str, ...]:
        """Every leg, primary first.

        Order matters downstream: the primary leads the combined
        position's tooltip listing, so the recognisable listing is
        read first.
        """
        rest = tuple(t for t in self.members if t != self.primary)
        return (self.primary, *rest)


# Cache for the parsed TOML payload, held as the sole attribute of a
# module-level container so reads and writes go through attribute
# access rather than a ``global`` statement -- the same shape (and the
# same CodeQL rationale) as ``sector_overrides._OverridesCache``.
# ``value is None`` is the unset sentinel so an empty file doesn't
# trigger a re-read on every call.
class _GroupsCache:
    value: tuple[PositionGroup, ...] | None = None


def _clear_groups_cache() -> None:
    """Drop the parsed TOML cache so the next read re-loads from disk.

    Production reads the file once per process. Tests that point the
    loader at a temp file call this between cases so a fresh fixture
    doesn't see a stale parse.
    """
    _GroupsCache.value = None


def _coerce_group(key: str, raw: object, source: str) -> PositionGroup | None:
    """Validate one ``[key]`` table into a :class:`PositionGroup`.

    Returns ``None`` (with a warning) for anything malformed. A group
    that can't be parsed means its legs stay independent, which is the
    pre-existing behaviour -- strictly worse presentation, never wrong
    numbers -- so degrading beats aborting the build.
    """
    if not isinstance(raw, dict):
        logger.warning(
            "position group %r in %s is not a table; ignoring",
            key,
            source,
        )
        return None
    primary = raw.get("primary")
    if not isinstance(primary, str) or not primary.strip():
        logger.warning(
            "position group %r in %s has no usable ``primary`` ticker; ignoring",
            key,
            source,
        )
        return None
    raw_members = raw.get("members", [])
    if not isinstance(raw_members, list) or not all(isinstance(m, str) for m in raw_members):
        logger.warning(
            "position group %r in %s has a malformed ``members`` list; ignoring",
            key,
            source,
        )
        return None
    members = tuple(dict.fromkeys(m.strip() for m in raw_members if m.strip()))
    if not [m for m in members if m != primary.strip()]:
        logger.warning(
            "position group %r in %s names no leg beyond its primary; ignoring",
            key,
            source,
        )
        return None
    name = raw.get("name")
    label = raw.get("label")
    return PositionGroup(
        key=key,
        primary=primary.strip(),
        members=members,
        name=name.strip() if isinstance(name, str) and name.strip() else None,
        label=label.strip() if isinstance(label, str) and label.strip() else None,
    )


def load_groups(path: str | None = None) -> tuple[PositionGroup, ...]:
    """Read and validate ``position_groups.toml``.

    ``path`` defaults to :data:`_POSITION_GROUPS_PATH` so the
    production callsite stays argument-free; tests pass a temp file.
    A missing or malformed file yields an empty tuple -- a fork
    without the file present still builds, matching how
    :mod:`investing.sector_overrides` treats its own config.

    Raises :class:`InvariantError` when one ticker is claimed by two
    groups: that is unresolvable rather than merely incomplete, and
    silently picking a winner would drop the loser's cashflows out of
    the portfolio entirely.
    """
    cached = _GroupsCache.value
    if cached is not None and path is None:
        return cached

    effective_path = path if path is not None else _POSITION_GROUPS_PATH
    groups: tuple[PositionGroup, ...] = ()

    def _memo(value: tuple[PositionGroup, ...]) -> tuple[PositionGroup, ...]:
        if path is None:
            _GroupsCache.value = value
        return value

    if not os.path.exists(effective_path):
        return _memo(groups)

    try:
        with open(effective_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "failed to read position groups at %s (%s); falling back to no grouping",
            effective_path,
            type(exc).__name__,
        )
        return _memo(groups)

    parsed: list[PositionGroup] = []
    for key, raw in data.items():
        group = _coerce_group(key, raw, effective_path)
        if group is not None:
            parsed.append(group)

    claimed: dict[str, str] = {}
    for group in parsed:
        for ticker in group.tickers:
            owner = claimed.get(ticker)
            if owner is not None:
                raise InvariantError(
                    f"ticker {ticker!r} is claimed by position groups "
                    f"{owner!r} and {group.key!r} -- a ticker may belong to at most one group",
                )
            claimed[ticker] = group.key

    return _memo(tuple(parsed))


def resolve_groups(
    held_tickers: set[str],
    *,
    path: str | None = None,
) -> tuple[PositionGroup, ...]:
    """Return the groups that are actually usable for this portfolio.

    A group survives only when its primary and at least one other leg
    are both currently in ``held_tickers``. Everything else is dropped
    with a debug note:

    * A group whose primary was sold off entirely has no identity leg
      left to inherit a logo / anchor / sector from.
    * A group down to a single held leg is not a combination any more
      -- that leg renders as an ordinary standalone position, which is
      exactly right.

    Legs named in the config but not held are simply ignored, so the
    file can keep describing a pairing the portfolio has since exited
    without breaking the build.
    """
    resolved: list[PositionGroup] = []
    for group in load_groups(path):
        if group.primary not in held_tickers:
            logger.debug(
                "position group %r skipped: primary %s is not held",
                group.key,
                group.primary,
            )
            continue
        legs = tuple(t for t in group.tickers if t in held_tickers)
        if len(legs) < 2:
            logger.debug(
                "position group %r skipped: only %d held leg(s)",
                group.key,
                len(legs),
            )
            continue
        resolved.append(
            PositionGroup(
                key=group.key,
                primary=group.primary,
                members=legs,
                name=group.name,
                label=group.label,
            )
        )
    return tuple(resolved)
