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

    def __radd__(self, other: object) -> str:
        # ``other`` is never a ``SafeHtml`` here. Python only reaches
        # ``b.__radd__(a)`` when ``a.__add__(b)`` declines, and
        # ``SafeHtml.__add__`` handles a ``SafeHtml`` right-hand side
        # itself, so a same-type sum never gets this far. The case that
        # does arrive is ``str + SafeHtml``, which the subclass-priority
        # rule routes here with a plain ``str`` on the left. It yields a
        # plain ``str``, which is the point: an unescaped fragment must
        # not inherit the safe mark.
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
    self-documenting. For ``href`` / ``src`` specifically, reach for
    :func:`safe_url` instead: escaping alone is not sufficient there.
    """
    return escape(value)


# Schemes allowed to appear in a rendered ``href``. Anything else --
# ``javascript:``, ``data:``, ``vbscript:``, or a scheme invented
# later -- is replaced by :data:`_UNSAFE_URL_FALLBACK`.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})

# Where a rejected URL points instead. The page wraps logos in
# ``<a href>``; an empty href would make the wrapper swallow clicks
# rather than route them anywhere, so the fallback is a real
# destination.
_UNSAFE_URL_FALLBACK = "https://www.google.com/"


def safe_url(value: str | None, *, fallback: str = _UNSAFE_URL_FALLBACK) -> str:
    """Return ``value`` if it carries an allowed scheme, else ``fallback``.

    ``html.escape`` does not neutralise a dangerous URL: it escapes the
    delimiters, so ``javascript:alert(1)`` survives the escape intact
    and stays live as an ``href``. Scheme validation is a separate
    obligation from escaping, and this is where the page discharges it.

    The URLs that reach the page's ``href`` attributes are not all
    ours. :func:`investing.holdings.resolve_company_url` forwards
    ``info["website"]`` straight out of the yfinance payload -- a
    third-party feed the build does not control -- so the value is
    untrusted input by the time it is rendered.

    Path-relative URLs (the logo ``src`` values) carry no scheme and
    pass through: they can only resolve against the page's own origin,
    which is the point of using them. A *scheme-relative* ``//host/...``
    URL is rejected -- it looks relative but names another origin, and
    nothing this page emits uses that form.
    """
    if not value:
        return fallback
    candidate = value.strip()
    if not candidate:
        return fallback
    # ``//host/path`` inherits the page's scheme but not its origin, so
    # it is an absolute URL wearing a relative costume. Rejected before
    # the scheme test below, which would otherwise see no colon in the
    # leading (empty) segment and wave it through.
    if candidate.startswith("//"):
        return fallback
    # A colon before any slash / query / fragment marks a scheme. No
    # colon in that leading segment means a path-relative URL, which is
    # same-origin by construction.
    head = candidate.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if ":" not in head:
        return candidate
    scheme = head.split(":", 1)[0].strip().lower()
    if scheme in _ALLOWED_URL_SCHEMES:
        return candidate
    return fallback


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
