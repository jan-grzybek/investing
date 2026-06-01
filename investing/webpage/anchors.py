"""Pure string helpers used by the renderer for in-page anchors / links."""

from __future__ import annotations

import re

# Characters to keep in a holding anchor; everything else folds to a
# single dash so the produced ``id`` / ``href`` round-trips cleanly
# through ``location.hash``.
_ANCHOR_KEEP = re.compile(r"[A-Za-z0-9]+")


def holding_anchor(ticker: str) -> str:
    """Slug for a single holding: ``NMS:AAPL`` -> ``holding-NMS-AAPL``.

    Tickers carry exchange prefixes and dotted suffixes
    (``NMS:AAPL``, ``LSE:VUAA.L``) that aren't URL-fragment friendly.
    Every run of alphanumerics is preserved as-is and joined with
    single dashes so the marquee, the bar chart and the capsule
    renderer can independently call this and agree on a stable id.
    """
    return "holding-" + "-".join(_ANCHOR_KEEP.findall(ticker))


def strip_exchange(ticker: str) -> str:
    """``NMS:AAPL`` -> ``AAPL`` (drop everything up to the first colon)."""
    _, _, symbol = ticker.partition(":")
    return symbol or ticker
