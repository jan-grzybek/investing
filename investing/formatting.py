"""Format helpers used by both the data pipeline and the
page renderer (dates, percentages, durations, hashes).
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime

from dateutil.relativedelta import relativedelta


def _ts_to_datetime(ts) -> datetime:
    """Convert a pandas Timestamp (or any ISO-stringifiable date) to a
    naive ``datetime`` at midnight.

    Hot path: ``pandas.Timestamp`` (and ``datetime``) expose
    ``year``/``month``/``day`` directly, so we can construct the result
    without round-tripping through string formatting + ``strptime`` --
    that round trip used to dominate per-dividend / per-FX-bar parsing
    on tickers with long histories. Strings (used by the test fixtures
    that mimic ``pandas.Series.items()`` with a plain dict) take the
    fallback path through ``fromisoformat``, which is materially
    faster than the legacy ``strptime("%Y-%m-%d")`` call.
    """
    if not isinstance(ts, str):
        return datetime(ts.year, ts.month, ts.day)
    iso_date = ts.split(" ", 1)[0] if " " in ts else ts
    return datetime.fromisoformat(iso_date)


# ---------------------------------------------------------------------------
# Date / formatting helpers (used by the renderer)
# ---------------------------------------------------------------------------


def _fmt_date(dt) -> str:
    # ``DD/MM/YYYY`` is the canonical human-readable format across
    # the whole page (holding capsules, trade rows, footer "Updated
    # on" line). The zero-padded day / month gives every date the
    # exact same character width, which keeps columns of dates
    # (the trades table, the holding capsules' period lists)
    # vertically aligned without monospaced glyphs. The ISO
    # ``<time datetime="...">`` attributes wrapping each rendered
    # date stay in W3C ``YYYY-MM-DD`` form -- machine-format is a
    # separate concern from the human-facing label.
    return dt.strftime("%d/%m/%Y")


def _fmt_date_long(dt) -> str:
    # Long-form ``Mon D, YYYY`` (e.g. "Mar 7, 2026") used for the
    # one-off "Since ..." caption that sits under the return chart
    # (and its chart-less twin in the returns-compare block). Those
    # captions read as full prose ("Since Jan 1, 2024 . 2 years, 1
    # month"), and the slashes of the table-friendly DD/MM/YYYY
    # format break the sentence rhythm there even though they read
    # naturally in the tabular columns. ``%-d`` (GNU/BSD) drops the
    # leading zero on the day number so the label reads as proper
    # English rather than as date-stamp metadata.
    return dt.strftime("%b %-d, %Y")


def _quarter_of(dt) -> tuple[int, int]:
    """``(year, quarter_index)`` for a date.

    Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec, mapped
    via integer-divide-by-3 of the (1-indexed) month. Used by the
    trades-table renderer to translate a burst's start / end dates
    into a calendar-quarter label.
    """
    return (dt.year, (dt.month - 1) // 3 + 1)


def _fmt_quarter_range(start, end) -> str:
    """Render a burst's date span as a calendar-quarter label.

    The trades table commits to publishing trade timing at quarter
    granularity rather than to-the-day. That matches the long-term-
    investor framing the rest of the page already uses (fund-letter
    cadence, quarterly disclosure, etc.) and removes a layer of
    incidental precision the reader wouldn't act on. Three layouts:

    * **Single quarter** (typical, since bursts are aggregated in a
      90-day rolling window): ``Q3 2026``.
    * **Two quarters in the same year** (a burst straddling a
      quarter boundary -- the only multi-quarter case the rolling
      window can produce inside one calendar year): ``Q3/Q4 2026``.
    * **Cross-year span** (a burst that ends in the next calendar
      year, typically Q4 -> Q1): ``Q4 2026 - Q1 2027``. Wrapped
      in two ``<time>`` elements separated by the same
      ``.trades__date-sep`` span the equity capsules use for
      multi-period dates, so the column reads with one mental
      model across both surfaces.

    Each ``<time datetime="...">`` carries the first month of the
    referenced quarter (W3C "valid month string" form,
    ``YYYY-MM``) so the machine layer still gets a real anchor
    point even though the visible label is qualitative. The sort
    key on the surrounding ``<tr>`` stays anchored on the burst's
    ``end_date`` (set in ``_build_trade_row``), so sorting by date
    still works at sub-quarter granularity -- two bursts in the
    same Q3 sort by how recent each one is.
    """
    start_y, start_q = _quarter_of(start)
    end_y, end_q = _quarter_of(end)
    start_month_iso = f"{start_y}-{(start_q - 1) * 3 + 1:02d}"
    if (start_y, start_q) == (end_y, end_q):
        return f'<time datetime="{start_month_iso}">Q{start_q} {start_y}</time>'
    if start_y == end_y:
        # Single-element ``<time>`` for same-year multi-quarter
        # spans: the slash-joined label ("Q3/Q4 2026") reads as a
        # single name for the span, not as two separate dates, so
        # splitting it across two ``<time>`` elements would over-
        # commit to a machine-readable structure the page doesn't
        # need.
        return f'<time datetime="{start_month_iso}">Q{start_q}/Q{end_q} {start_y}</time>'
    end_month_iso = f"{end_y}-{(end_q - 1) * 3 + 1:02d}"
    return (
        f'<time datetime="{start_month_iso}">'
        f"Q{start_q} {start_y}</time>"
        '<span class="trades__date-sep"> - </span>'
        f'<time datetime="{end_month_iso}">'
        f"Q{end_q} {end_y}</time>"
    )


def _pluralize(count: int, singular: str) -> str:
    return f"1 {singular}" if count == 1 else f"{count} {singular}s"


def _format_duration(delta: relativedelta) -> str:
    """Format a ``relativedelta`` as 'N years, M months' (decade-capped)."""
    if delta.years >= 10:
        return _pluralize(delta.years, "year")
    parts = []
    if delta.years > 0:
        parts.append(_pluralize(delta.years, "year"))
    if delta.months > 0:
        parts.append(_pluralize(delta.months, "month"))
    if not parts:
        return "less than a month"
    return ", ".join(parts)


def _value_class(value: float) -> str:
    """CSS modifier reflecting the sign of a TSR/CAGR/TWR percentage."""
    return "value--negative" if value < 0 else "value--positive"


def _format_sort_number(value: float) -> str:
    """Stringify a numeric sort key for a ``data-sort-*`` attribute.

    Holding cards expose TSR / CAGR / weight as raw numbers via
    ``data-sort-*`` attributes that the inline holdings-sort
    script reads back with ``parseFloat``. Padding to a fixed
    decimal count keeps the markup tidy and ensures values like
    ``-12`` and ``-12.0`` serialise identically across calls so
    the rendered HTML stays diff-stable regardless of whether
    the upstream computation emitted an int or a float."""
    return format(float(value), ".4f")


def _fmt_pct(value: float, *, signed: bool = False) -> str:
    """Format a percentage with one decimal up to 99.9 and as a whole
    number once the displayed magnitude reaches 100.

    A trailing ``.x`` next to a 3-digit integer part is visually
    noisy and adds no real precision to the reader -- ``100.3%``
    reads tidier as ``100%`` and ``672.9%`` as ``673%``. We apply
    the same rule to ``pp`` deltas (capsule + chart overlay + OG
    image) so the page is uniform: any quantity expressed in
    percent or percentage points drops its decimal once it hits
    triple digits.

    Boundary handling uses ``round(abs(value), 1) >= 100`` rather
    than the raw magnitude so values that round UP to the 100
    threshold (e.g. ``99.95`` -> ``100.0``) also shed the now-
    redundant decimal instead of rendering as ``100.0%``.
    ``signed=True`` prefixes a leading ``+`` for non-negative
    values, matching the existing ``:+.1f`` behaviour at delta
    sites.
    """
    sign_spec = "+" if signed else ""
    if round(abs(value), 1) >= 100:
        return format(value, f"{sign_spec}.0f")
    return format(value, f"{sign_spec}.1f")


def _sha256_b64(payload: str) -> str:
    """Base64 SHA-256 digest in the form CSP expects for hash sources.

    Browsers compute the digest of the inline script/style content
    (verbatim, without surrounding ``<script>``/``<style>`` tags) and
    require it to match a ``'sha256-<b64>'`` entry in the matching
    directive of the page's Content-Security-Policy."""
    return base64.b64encode(hashlib.sha256(payload.encode("utf-8")).digest()).decode("ascii")
