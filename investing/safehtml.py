"""Light Markup-style wrapper for "this string is already safe HTML".

The page renderer historically built HTML by string concatenation,
sprinkling :func:`html.escape` calls at the points the author
remembered to. That left every untyped ``str`` flowing into a template
indistinguishable from one that was already escaped, and forced a
manual audit at every change to keep XSS-able fields covered.

:class:`SafeHtml` makes the distinction explicit. Anything wrapped in
``SafeHtml`` is treated as pre-escaped HTML; everything else gets
``html.escape`` applied automatically when it reaches a template via
:func:`render_template` or when it's concatenated through the helpers
in this module. The wrapper is *not* a security boundary on its own
-- a malicious caller can still call ``SafeHtml(user_input)`` -- but
it converts "did I remember to escape this?" into a typed obligation
the lint pass can spot at the construction site.

The API intentionally mirrors Jinja2's ``Markup`` so the mental model
transfers, but the implementation is ~30 lines and adds no third-
party dependency.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Union

# Type alias for "anything render_template knows how to escape into HTML".
SafeHtmlConvertible = Union["SafeHtml", str, int, float, bool, None]


class SafeHtml(str):
    """A string that has already been HTML-escaped.

    Subclasses ``str`` so any function that takes a ``str`` keeps
    working unchanged; the distinguishing behaviour lives in
    :func:`escape`, :func:`render_template` and :func:`join` below,
    all of which check the type rather than the value.

    Concatenation with another ``SafeHtml`` (via ``+`` or
    :func:`join`) yields a ``SafeHtml``. Concatenation with a plain
    ``str`` yields a plain ``str`` so a stray un-escaped fragment
    cannot silently inherit the "safe" mark; convert the other side
    with :func:`escape` first if that's the intent.
    """

    __slots__ = ()

    def __add__(self, other: object) -> SafeHtml | str:
        if isinstance(other, SafeHtml):
            return SafeHtml(str.__add__(self, other))
        return str.__add__(self, str(other) if other is not None else "")

    def __radd__(self, other: object) -> SafeHtml | str:
        if isinstance(other, SafeHtml):
            return SafeHtml(str.__add__(other, self))
        return str.__add__(str(other) if other is not None else "", self)


# A convenience singleton for "empty safe payload", to make optional
# section renderers tidy (``return SafeHtml("")`` vs ``return EMPTY``).
EMPTY = SafeHtml("")


def escape(value: SafeHtmlConvertible) -> SafeHtml:
    """Return ``value`` as :class:`SafeHtml`, escaping plain strings.

    Idempotent on ``SafeHtml`` (no double-escape), passes numbers /
    ``bool`` / ``None`` through ``str`` so format strings stay tidy.
    """
    if isinstance(value, SafeHtml):
        return value
    if value is None:
        return SafeHtml("")
    return SafeHtml(html.escape(str(value)))


def attr(value: SafeHtmlConvertible) -> SafeHtml:
    """Escape ``value`` for use as an HTML attribute value.

    Differs from :func:`escape` only conceptually -- ``html.escape``
    handles both contexts -- but a named helper makes the call site
    self-documenting and gives a single edit surface should the
    attribute-vs-text distinction ever need to diverge (e.g. URL
    attribute escaping).
    """
    return escape(value)


def join(separator: SafeHtmlConvertible, parts: Iterable[SafeHtmlConvertible]) -> SafeHtml:
    """Join ``parts`` with ``separator``, escaping anything not already safe."""
    sep = escape(separator)
    return SafeHtml(str.join(sep, (escape(p) for p in parts)))


def render_template(template: str, /, **fields: SafeHtmlConvertible) -> SafeHtml:
    """Format ``template`` with auto-escaped ``fields``.

    Substitution uses ``str.format``, so placeholders are ``{name}``.
    Each field is routed through :func:`escape` before substitution
    so a plain string can never reach the output un-escaped; if you
    have a fragment that's already safe, wrap it in :class:`SafeHtml`
    before passing it in.

    The function returns a :class:`SafeHtml` instance: nested template
    invocations therefore compose without ever stripping the safety
    mark.
    """
    return SafeHtml(template.format(**{k: escape(v) for k, v in fields.items()}))
