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
