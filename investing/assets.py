"""Inline CSS / JS payloads loaded verbatim from the
``assets/`` directory at import time so the source-of-truth
lives in real ``.css`` / ``.js`` files that editors can
lint and format.

Each constant is a one-line read and nothing more. This module used
to carry 232 lines of comment against 25 lines of code, describing the
behaviour of JavaScript that lives in a different directory: easing
curves, ``freezeColumns``, container-query breakpoints. Nothing
connected the two, so editing ``assets/src/js/trades_sort.js`` left a
60-line explanation here quietly describing the previous version --
and three of those blocks had already drifted into duplicates of a
header the JS file itself carried.

The prose now sits at the top of the file it describes, where an
editor of that file will see it. ``scripts/build_assets.py`` strips
comments on the way to the served bytes, so it costs the page nothing.
"""

from __future__ import annotations

from .paths import _read_asset

# Names imported by ``investing.webpage.head`` to assemble the inline
# CSS / JS payloads of the rendered page. Declaring ``__all__`` keeps
# the leading-underscore ``private to this module`` convention honest
# (CodeQL's ``py/unused-global-variable`` query treats the underscore
# prefix as a hard hint that the binding is module-local; ``__all__``
# is the canonical opt-in to advertise these as cross-module exports).
__all__ = [
    "_HASH_CLEAR_SCRIPT",
    "_HOLDINGS_SORT_SCRIPT",
    "_METRICS_NOTE_SCRIPT",
    "_NAV_SCROLL_SCRIPT",
    "_PAGE_STYLES",
    "_RETURN_CHART_SCRIPT",
    "_SCROLL_HINT_SCRIPT",
    "_TRADES_SORT_SCRIPT",
]

# ---------------------------------------------------------------------------
# Webpage renderer
# ---------------------------------------------------------------------------


# Embedded styles. Kept verbatim as a single string so ``save()`` stays
# linear and the dark-mode / print rules are easy to audit.
_PAGE_STYLES = _read_asset("page.css").strip()


_HASH_CLEAR_SCRIPT = _read_asset("hash_clear.js")


_NAV_SCROLL_SCRIPT = _read_asset("nav_scroll.js")


_RETURN_CHART_SCRIPT = _read_asset("return_chart.js")
_SCROLL_HINT_SCRIPT = _read_asset("scroll_hint.js")


_TRADES_SORT_SCRIPT = _read_asset("trades_sort.js")


_METRICS_NOTE_SCRIPT = _read_asset("metrics_note.js")


_HOLDINGS_SORT_SCRIPT = _read_asset("holdings_sort.js")
