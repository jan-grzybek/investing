"""Whitespace-tolerant probes for asserting on the served ``page.css``.

The served stylesheet at ``assets/page.css`` is now minified at build
time (:mod:`scripts.build_assets` runs ``csscompressor`` over the
readable sources under ``assets/src/css``). The substring assertions
in the webpage test suite ("does this property survive in the
``.foo`` block?", "is this composite selector still present?")
predated that, so they all assumed formatted CSS (``selector {\n  prop:
value;\n}``).

Rather than re-author every assertion against a fragile minified form
-- ``.foo{prop:value;...}`` packs everything onto one line and the
exact byte layout would drift on every csscompressor bump -- we route
lookups through this module. Every helper normalises whitespace
*before* matching, so a test reads as "the .foo block declares
``width: 7em``" regardless of whether the served bytes carry the
formatted source, the minified output, or anything in between.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
# Punctuation around which whitespace carries no semantic information in
# the CSS grammar. ``csscompressor`` strips spaces around exactly these
# characters, so normalising against the same set keeps the canonical
# form aligned with the served bytes.
#
# ``>`` is included because it only ever appears as a child combinator
# in selectors, where ``csscompressor`` always collapses surrounding
# whitespace. Sibling combinators (``+``, ``~``) are intentionally
# omitted: ``+`` and ``-`` also appear inside ``calc()`` expressions
# where the surrounding space is meaningful (``calc(a - b)`` is a
# subtraction, ``calc(a -b)`` is a parse error), and the test surface
# does not currently care about sibling-combinator whitespace.
_AROUND_PUNCT = re.compile(r"\s*([{}:;,()>])\s*")


def normalize(css: str) -> str:
    """Collapse whitespace and remove spaces around CSS punctuation.

    Idempotent: running it on already-minified CSS is a no-op. Used as
    the canonical form for every helper below so a test never has to
    care which side of minification produced the bytes it's looking at.
    """
    css = _WS.sub(" ", css)
    css = _AROUND_PUNCT.sub(r"\1", css)
    return css.strip()


def blocks_for(css: str, selector: str) -> list[str]:
    """Return declaration bodies for every block whose selector list
    ends in ``selector``.

    Matches both ``.foo`` blocks and grouped selectors like
    ``.bar, .foo`` (where ``.foo`` is the last entry). The returned
    bodies do NOT include the surrounding braces; they're already in
    :func:`normalize`'d form so callers can do simple substring checks
    against canonical ``prop:value`` pairs.

    The implementation walks brace nesting so nested at-rules
    (``@media (...) { .foo { ... } }``) don't confuse the splitter --
    the inner ``.foo`` block is reported exactly once with its true
    body, instead of the splitter dropping the closing ``}`` of the
    enclosing ``@media`` into the result.
    """
    body = normalize(css)
    needle_a = selector + "{"
    needle_b = "," + selector + "{"
    out: list[str] = []
    i = 0
    while i < len(body):
        idx_a = body.find(needle_a, i)
        idx_b = body.find(needle_b, i)
        candidates = [idx for idx in (idx_a, idx_b) if idx != -1]
        if not candidates:
            break
        idx = min(candidates)
        if idx == idx_b:
            idx += 1  # skip the comma in `,selector{`
        start = idx + len(selector) + 1  # past the trailing `{`
        depth = 1
        j = start
        while j < len(body) and depth:
            ch = body[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        # ``j`` is one past the closing ``}`` of the matching block;
        # ``j - 1`` is the closing brace itself.
        out.append(body[start : j - 1])
        i = j
    return out


def has_declaration(body: str, prop: str, value: str) -> bool:
    """Return True if a (normalized) declaration body contains ``prop:value``.

    Accepts a freshly-extracted body from :func:`blocks_for` or a raw
    CSS fragment -- either is normalised before matching. The match is
    anchored on declaration boundaries (``;`` or block start) so a
    bogus partial match (``border-width: 7em`` satisfying a check for
    ``width: 7em``) cannot slip through.
    """
    normalised = normalize(body)
    pattern = re.compile(
        rf"(^|;|\{{){re.escape(prop)}:{re.escape(value)}(;|$|\}})",
    )
    return bool(pattern.search(normalised))


def contains_selector(css: str, selector: str) -> bool:
    """Return True if a selector appears verbatim in the (normalised) CSS.

    Useful for "the ``:nth-of-type(n+11)`` rule survives" assertions
    where the test cares about a specific composite selector, not the
    declarations attached to it. The selector argument is itself
    normalised before matching so callers can write it in whichever
    whitespace style they prefer.
    """
    return normalize(selector) in normalize(css)


def at_rule_bodies(css: str, prelude: str) -> list[str]:
    """Return the bodies of every at-rule block with the given prelude.

    ``prelude`` is the ``@media (...)`` / ``@supports (...)`` /
    ``@keyframes name`` prefix; whitespace inside it is normalised
    before matching. A single stylesheet typically declares the same
    ``@media`` query several times (one block per topic), so returning
    all matches lets callers union the contents without having to know
    which physical block carries the rule they care about.

    Each body comes back without its surrounding braces and is itself
    in normalised form, ready for substring / selector probing.
    """
    body = normalize(css)
    needle = normalize(prelude) + "{"
    out: list[str] = []
    i = 0
    while i < len(body):
        idx = body.find(needle, i)
        if idx == -1:
            break
        start = idx + len(needle)
        depth = 1
        j = start
        while j < len(body) and depth:
            ch = body[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        out.append(body[start : j - 1])
        i = j
    return out


def at_rule_body(css: str, prelude: str) -> str | None:
    """Return the (normalised) body of the first matching at-rule.

    Convenience wrapper around :func:`at_rule_bodies` for the common
    "is this rule under that media query?" probe. Use the plural form
    when the stylesheet may declare the same query in several blocks.
    """
    bodies = at_rule_bodies(css, prelude)
    return bodies[0] if bodies else None


def contains_at_rule(css: str, prelude: str) -> bool:
    """Return True if an at-rule with the given prelude is declared.

    A thin wrapper over :func:`at_rule_bodies` for tests that only
    need a presence check.
    """
    return bool(at_rule_bodies(css, prelude))
