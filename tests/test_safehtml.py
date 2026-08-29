"""Tests for the SafeHtml escaping helpers."""

from __future__ import annotations

import pytest

from investing.safehtml import (
    EMPTY,
    SafeHtml,
    attr,
    escape,
    join,
    render_template,
)


def test_escape_idempotent_on_safe_html():
    raw = SafeHtml("<b>ok</b>")
    assert escape(raw) is raw


def test_escape_plain_string():
    assert str(escape("<script>")) == "&lt;script&gt;"


def test_escape_none_becomes_empty():
    assert str(escape(None)) == ""


def test_escape_numbers_and_bools():
    assert str(escape(42)) == "42"
    assert str(escape(True)) == "True"


def test_safe_html_add_with_safe_operand():
    left = SafeHtml("<b>")
    right = SafeHtml("</b>")
    assert isinstance(left + right, SafeHtml)
    assert str(left + right) == "<b></b>"


def test_safe_html_add_with_plain_str_returns_str():
    assert isinstance(SafeHtml("a") + "b", str)
    assert SafeHtml("a") + "b" == "ab"


def test_safe_html_radd_with_plain_str_returns_str():
    assert isinstance("a" + SafeHtml("b"), str)


def test_attr_delegates_to_escape():
    assert str(attr("<x>")) == "&lt;x&gt;"


def test_join_escapes_each_part():
    out = join(", ", ["<a>", SafeHtml("<b>")])
    assert isinstance(out, SafeHtml)
    assert str(out) == "&lt;a&gt;, <b>"


def test_render_template_auto_escapes_fields():
    out = render_template("<p>{name}</p>", name="<evil>")
    assert str(out) == "<p>&lt;evil&gt;</p>"


def test_render_template_preserves_safe_fragments():
    out = render_template("{inner}", inner=SafeHtml("<em>ok</em>"))
    assert str(out) == "<em>ok</em>"


def test_empty_singleton():
    assert str(EMPTY) == ""


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (SafeHtml("x"), None),
        (None, SafeHtml("x")),
    ],
)
def test_safe_html_add_with_none_coerces_to_empty(left, right):
    if isinstance(left, SafeHtml):
        assert str(left + right) == "x"
    else:
        assert str(left + right) == "x"


class TestSafeUrl:
    """Scheme validation for URLs that reach an ``href``.

    ``html.escape`` escapes delimiters; it does not disarm a scheme.
    ``javascript:alert(1)`` survives escaping unchanged and stays live
    as an ``href``, so the two obligations are separate and this is
    where the second one is discharged. The values matter because
    ``resolve_company_url`` forwards ``info["website"]`` straight out
    of the yfinance payload.
    """

    def test_http_and_https_pass_through(self):
        from investing.safehtml import safe_url

        assert safe_url("https://example.com/x?y=1") == "https://example.com/x?y=1"
        assert safe_url("http://example.com") == "http://example.com"

    def test_scheme_match_is_case_insensitive(self):
        from investing.safehtml import safe_url

        assert safe_url("HTTPS://example.com") == "HTTPS://example.com"

    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
        ],
    )
    def test_dangerous_schemes_are_replaced(self, hostile):
        from investing.safehtml import safe_url

        assert safe_url(hostile) == "https://www.google.com/"

    def test_relative_paths_pass_through(self):
        # Logo ``src`` values are relative and carry no scheme; they can
        # only resolve against the page's own origin.
        from investing.safehtml import safe_url

        assert safe_url("logos/tight/NMS%3AAAA.svg") == "logos/tight/NMS%3AAAA.svg"

    def test_a_colon_after_a_slash_is_not_a_scheme(self):
        # ``logos/a:b.svg`` is a relative path whose *filename* contains
        # a colon -- exactly the shape every ticker logo uses
        # ("NMS:AAA.svg"). Treating that as a scheme would reject every
        # logo on the page.
        from investing.safehtml import safe_url

        assert safe_url("logos/tight/NMS:AAA.svg") == "logos/tight/NMS:AAA.svg"

    def test_empty_and_none_take_the_fallback(self):
        from investing.safehtml import safe_url

        assert safe_url(None) == "https://www.google.com/"
        assert safe_url("") == "https://www.google.com/"
        assert safe_url("   ") == "https://www.google.com/"

    def test_custom_fallback_is_honoured(self):
        from investing.safehtml import safe_url

        assert safe_url("javascript:x", fallback="https://q/") == "https://q/"


class TestSchemeRelativeUrls:
    def test_scheme_relative_urls_are_rejected(self):
        """``//host/path`` is absolute wearing a relative costume.

        It inherits the page's scheme but *not* its origin, so the
        "no colon means same-origin" shortcut does not hold for it --
        the leading segment before the first slash is empty and
        contains no colon, so it would otherwise sail through.
        """
        from investing.safehtml import safe_url

        assert safe_url("//evil.example/x") == "https://www.google.com/"
        assert safe_url("//evil.example") == "https://www.google.com/"

    def test_query_and_fragment_only_urls_stay_relative(self):
        from investing.safehtml import safe_url

        assert safe_url("?q=1") == "?q=1"
        assert safe_url("#current") == "#current"
