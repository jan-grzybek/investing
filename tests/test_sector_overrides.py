"""Tests for :mod:`investing.sector_overrides`.

Coverage focus:

* :func:`resolve_sector` priority order -- yfinance value wins, then
  override, then empty + hint.
* TOML loader -- happy path, missing file, malformed file, invalid
  sector value, non-table ``[sectors]`` entry.
* Maintenance hint registry -- :func:`record_missing_sector`,
  :func:`record_missing_logo`, :func:`consume_hints`,
  :func:`reset_hints`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from investing.sector_overrides import (
    KNOWN_SECTORS,
    MaintenanceHints,
    _clear_overrides_cache,
    _load_overrides,
    append_missing_sector_stubs,
    consume_hints,
    record_missing_logo,
    record_missing_sector,
    reset_hints,
    resolve_sector,
)


def _write_overrides(tmp_path: Path, body: str) -> Path:
    """Drop a TOML overrides file under ``tmp_path`` and return its path.

    Tests pass the path to :func:`resolve_sector` /
    :func:`_load_overrides` via the ``overrides_path`` keyword so the
    real ``sector_overrides.toml`` at the repo root never enters the
    test surface.
    """
    path = tmp_path / "sector_overrides.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestResolveSector:
    def test_yfinance_value_wins_over_override(self, tmp_path):
        # yfinance sector ALWAYS wins when present, even when an
        # override exists -- the file is a fallback, not a forced
        # mapping. A stale override therefore degrades gracefully
        # the moment yfinance starts returning a real sector.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Healthcare"\n',
        )
        result = resolve_sector(
            "NMS:AAA", "Technology", overrides_path=str(path)
        )
        assert result == "Technology"

    def test_override_used_when_yfinance_blank(self, tmp_path):
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        assert resolve_sector("NMS:AAA", "", overrides_path=str(path)) == "Technology"

    def test_override_used_when_yfinance_whitespace_only(self, tmp_path):
        # An upstream value that strips to empty is treated the same
        # as a genuinely-blank one. yfinance occasionally returns a
        # single-space string in lieu of a missing field.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Energy"\n',
        )
        assert resolve_sector("NMS:AAA", "   ", overrides_path=str(path)) == "Energy"

    def test_falls_back_to_empty_when_both_blank(self, tmp_path):
        # No yfinance value, no override entry -> empty string + a
        # maintenance hint recorded for the build summary. The
        # renderer maps "" to the ``Other`` bucket downstream.
        path = _write_overrides(tmp_path, "[sectors]\n")
        result = resolve_sector("NMS:NONE", "", overrides_path=str(path))
        assert result == ""
        hints = consume_hints()
        assert hints.missing_sector == ["NMS:NONE"]

    def test_no_hint_when_yfinance_provides_value(self, tmp_path):
        path = _write_overrides(tmp_path, "[sectors]\n")
        resolve_sector("NMS:AAA", "Technology", overrides_path=str(path))
        assert consume_hints().is_empty

    def test_no_hint_when_override_fills_gap(self, tmp_path):
        # An override that covers the missing yfinance value should
        # NOT produce a hint -- the maintainer has already addressed
        # the gap and the build summary should stay clean.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        resolve_sector("NMS:AAA", "", overrides_path=str(path))
        assert consume_hints().is_empty


class TestLoadOverrides:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        # A fresh fork that hasn't added the TOML yet should still
        # build cleanly -- a missing file is equivalent to "no
        # overrides", not a hard error.
        nonexistent = tmp_path / "absent.toml"
        assert _load_overrides(str(nonexistent)) == {}

    def test_empty_sectors_table_returns_empty_dict(self, tmp_path):
        path = _write_overrides(tmp_path, "[sectors]\n")
        assert _load_overrides(str(path)) == {}

    def test_valid_entries_are_returned(self, tmp_path):
        path = _write_overrides(
            tmp_path,
            (
                "[sectors]\n"
                '"NMS:AAA" = "Technology"\n'
                '"NYQ:BBB" = "Healthcare"\n'
            ),
        )
        result = _load_overrides(str(path))
        assert result == {
            "NMS:AAA": "Technology",
            "NYQ:BBB": "Healthcare",
        }

    def test_invalid_sector_is_recorded_as_hint(self, tmp_path):
        # ``"Tech"`` isn't a canonical sector (the real label is
        # ``"Technology"``); the entry is dropped from the loader's
        # output and the bad value is surfaced via the hints so the
        # typo is easy to spot in the build summary.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Tech"\n',
        )
        assert _load_overrides(str(path)) == {}
        hints = consume_hints()
        assert hints.invalid_overrides == {"NMS:AAA": "Tech"}

    def test_non_string_value_is_recorded_as_hint(self, tmp_path):
        # TOML allows ints / arrays / tables on the RHS of an
        # assignment; reject anything that isn't a string outright
        # so the renderer never sees a non-sector payload.
        path = _write_overrides(
            tmp_path,
            "[sectors]\n\"NMS:AAA\" = 42\n",
        )
        assert _load_overrides(str(path)) == {}
        hints = consume_hints()
        assert "NMS:AAA" in hints.invalid_overrides

    def test_non_table_sectors_entry_is_warned_about(self, tmp_path):
        # A maintainer who accidentally writes ``sectors = "..."``
        # instead of the table form should get the empty-overrides
        # fallback rather than a crash. The loader logs a warning
        # but no hint is recorded -- there's no specific ticker /
        # value pair to surface, just the file-level shape error.
        path = _write_overrides(tmp_path, 'sectors = "broken"\n')
        assert _load_overrides(str(path)) == {}

    def test_malformed_toml_returns_empty_dict(self, tmp_path):
        path = tmp_path / "broken.toml"
        path.write_text("this is = = not toml", encoding="utf-8")
        assert _load_overrides(str(path)) == {}


class TestMaintenanceHints:
    def test_record_missing_sector_is_idempotent(self):
        record_missing_sector("NMS:AAA")
        record_missing_sector("NMS:AAA")
        record_missing_sector("NMS:BBB")
        hints = consume_hints()
        assert hints.missing_sector == ["NMS:AAA", "NMS:BBB"]

    def test_record_missing_logo_is_idempotent(self):
        record_missing_logo("NMS:AAA")
        record_missing_logo("NMS:AAA")
        record_missing_logo("NMS:BBB")
        hints = consume_hints()
        assert hints.missing_logos == ["NMS:AAA", "NMS:BBB"]

    def test_consume_drains_registry(self):
        record_missing_sector("NMS:AAA")
        record_missing_logo("NMS:AAA")
        first = consume_hints()
        assert first.missing_sector == ["NMS:AAA"]
        assert first.missing_logos == ["NMS:AAA"]
        second = consume_hints()
        assert second.is_empty

    def test_reset_hints_clears_everything(self):
        record_missing_sector("NMS:AAA")
        record_missing_logo("NMS:BBB")
        reset_hints()
        assert consume_hints().is_empty

    def test_is_empty_predicate(self):
        empty = MaintenanceHints()
        assert empty.is_empty
        non_empty = MaintenanceHints(missing_sector=["X"])
        assert not non_empty.is_empty


class TestKnownSectors:
    def test_known_sectors_includes_canonical_set(self):
        # Sanity check: the loader's validation pivots on this
        # frozenset, so a sector that has visibly worked in the
        # treemap palette for a long time should obviously remain
        # acceptable.
        for sector in ("Technology", "Healthcare", "Financial Services"):
            assert sector in KNOWN_SECTORS

    def test_known_sectors_excludes_other_sentinel(self):
        # ``"Other"`` is the renderer's fallback bucket, not a real
        # sector value -- accepting it as an override would defeat
        # the point of pinning a missing-sector ticker.
        assert "Other" not in KNOWN_SECTORS


class TestModuleCacheBehaviour:
    def test_default_path_uses_module_cache(self, monkeypatch, tmp_path):
        # Two back-to-back calls with the default path should hit
        # the cache; an explicit ``overrides_path`` argument should
        # bypass it (test injection contract).
        from investing import sector_overrides as so

        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        monkeypatch.setattr(so, "_SECTOR_OVERRIDES_PATH", str(path))
        _clear_overrides_cache()

        first = _load_overrides()
        second = _load_overrides()
        assert first is second  # same dict instance -> cache hit

    def test_explicit_path_bypasses_cache(self, tmp_path):
        # Two explicit-path reads against different files must each
        # parse from disk; an explicit path never populates or
        # consults the default-path cache.
        first_file = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        second_file = tmp_path / "second.toml"
        second_file.write_text(
            '[sectors]\n"NMS:AAA" = "Healthcare"\n',
            encoding="utf-8",
        )

        assert _load_overrides(str(first_file))["NMS:AAA"] == "Technology"
        assert _load_overrides(str(second_file))["NMS:AAA"] == "Healthcare"


class TestAppendMissingSectorStubs:
    """The auto-populate hook gives the maintainer a head-start when
    editing ``sector_overrides.toml`` after a missing-sector hint:
    each ticker gets a commented-out block with two-keystroke
    activation (delete ``# `` from the data line, type a sector).
    Idempotent across rebuilds: a ticker that already has any
    mention in the file (active entry or auto-appended stub) is
    skipped.
    """

    def test_noop_on_empty_input(self, tmp_path):
        path = _write_overrides(tmp_path, "[sectors]\n")
        original = path.read_text(encoding="utf-8")
        appended = append_missing_sector_stubs([], path=str(path))
        assert appended == []
        # File must be byte-identical -- a no-op input must not
        # touch the file at all (a trailing-whitespace tidy would
        # still count as mutation under git-diff).
        assert path.read_text(encoding="utf-8") == original

    def test_noop_when_file_missing(self, tmp_path):
        # A fresh fork without the TOML must not see one conjured
        # into existence by the hook. The function returns an
        # empty list and the file stays absent.
        absent = tmp_path / "absent.toml"
        appended = append_missing_sector_stubs(["NMS:AAA"], path=str(absent))
        assert appended == []
        assert not absent.exists()

    def test_appends_commented_stub_for_each_ticker(self, tmp_path):
        path = _write_overrides(tmp_path, "[sectors]\n")
        appended = append_missing_sector_stubs(
            ["NMS:FISV", "NYQ:WIDGET"], path=str(path),
        )
        assert appended == ["NMS:FISV", "NYQ:WIDGET"]
        text = path.read_text(encoding="utf-8")
        # Each ticker shows up in a commented data line ready for
        # the maintainer to uncomment + fill in.
        assert '# "NMS:FISV" = ""' in text
        assert '# "NYQ:WIDGET" = ""' in text
        # And the explanatory header is present so the maintainer
        # doesn't have to remember the canonical sector list.
        assert "Auto-detected: missing sector" in text
        assert "canonical sectors" in text

    def test_appended_stub_does_not_parse_as_active_entry(self, tmp_path):
        # Commented stubs must stay commented -- if the empty
        # ``""`` value were active it would itself record an
        # invalid-override hint on the next build (failing the
        # ``KNOWN_SECTORS`` membership check), creating a churning
        # loop. Re-parse the file and confirm no new override
        # surfaces.
        path = _write_overrides(tmp_path, "[sectors]\n")
        append_missing_sector_stubs(["NMS:AAA"], path=str(path))
        _clear_overrides_cache()
        assert _load_overrides(str(path)) == {}
        # And no invalid-override hint either: the line is purely
        # a comment so the loader never sees it.
        assert consume_hints().is_empty

    def test_skips_ticker_already_in_active_entry(self, tmp_path):
        # An override already pinned to a real sector must not get
        # a duplicate commented stub on top of it. The substring
        # match against ``"TICKER"`` (with quotes) catches the
        # active entry.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        original = path.read_text(encoding="utf-8")
        appended = append_missing_sector_stubs(["NMS:AAA"], path=str(path))
        assert appended == []
        assert path.read_text(encoding="utf-8") == original

    def test_skips_ticker_already_in_commented_stub(self, tmp_path):
        # The dedupe predicate must also catch a previously
        # auto-appended stub so repeated builds don't pile up
        # identical comment blocks. Run twice and confirm the
        # second pass is a no-op.
        path = _write_overrides(tmp_path, "[sectors]\n")
        first = append_missing_sector_stubs(["NMS:AAA"], path=str(path))
        text_after_first = path.read_text(encoding="utf-8")
        second = append_missing_sector_stubs(["NMS:AAA"], path=str(path))
        assert first == ["NMS:AAA"]
        assert second == []
        assert path.read_text(encoding="utf-8") == text_after_first

    def test_partial_dedupe_only_appends_new_tickers(self, tmp_path):
        # Mixed input: one ticker already has an entry, one is new.
        # Only the new one should be appended; the active entry
        # for the existing ticker must not be touched.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        appended = append_missing_sector_stubs(
            ["NMS:AAA", "NMS:BBB"], path=str(path),
        )
        assert appended == ["NMS:BBB"]
        text = path.read_text(encoding="utf-8")
        # Active entry preserved verbatim.
        assert '"NMS:AAA" = "Technology"' in text
        # New ticker got a stub.
        assert '# "NMS:BBB" = ""' in text

    def test_substring_anchoring_does_not_false_positive(self, tmp_path):
        # ``"NMS:A"`` is a substring of ``"NMS:AAA"`` but the
        # quote-anchored needle (``"NMS:A"``) must NOT match the
        # quoted form of the longer ticker (``"NMS:AAA"``). A
        # naive ``in`` against the unquoted ticker would have
        # mis-deduped here.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"NMS:AAA" = "Technology"\n',
        )
        appended = append_missing_sector_stubs(["NMS:A"], path=str(path))
        assert appended == ["NMS:A"]
        text = path.read_text(encoding="utf-8")
        assert '# "NMS:A" = ""' in text

    def test_prose_mention_in_header_is_not_a_skip(self, tmp_path):
        # Regression: the shipped ``sector_overrides.toml`` mentions
        # ``"DUS:SSU.DU"`` inside the header's worked-example prose
        # (``# (e.g. "NMS:AAPL", "NYQ:UNH", "DUS:SSU.DU")``) without
        # ever assigning it. The earlier substring-only predicate
        # treated that as "already present" and silently skipped
        # the auto-populate stub on the first build that hit the
        # ticker. The line-anchored regex must NOT match the prose
        # form -- only true ``"TICKER" = ...`` assignment shapes
        # (active or commented) should count as already-present.
        path = _write_overrides(
            tmp_path,
            (
                "# Worked example referencing "
                '"DUS:SSU.DU" in prose without assigning it.\n'
                "[sectors]\n"
            ),
        )
        appended = append_missing_sector_stubs(
            ["DUS:SSU.DU"], path=str(path),
        )
        assert appended == ["DUS:SSU.DU"]
        text = path.read_text(encoding="utf-8")
        assert '# "DUS:SSU.DU" = ""' in text

    def test_ticker_with_regex_metachars_is_handled(self, tmp_path):
        # The ticker key contains a ``.`` which is a regex
        # metacharacter; ``re.escape`` must be applied so the
        # predicate matches the literal quoted form rather than
        # treating the dot as "any character". A ticker like
        # ``DUS:SSUaDU`` (hypothetical) should still NOT collide
        # with ``DUS:SSU.DU``.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n"DUS:SSU.DU" = "Industrials"\n',
        )
        # Replace the assigned ticker so the dot escape matters:
        # without escape, ``"DUS:SSUaDU"`` would match
        # ``"DUS:SSU.DU"`` via the ``.`` wildcard and erroneously
        # skip.
        appended = append_missing_sector_stubs(
            ["DUS:SSUaDU"], path=str(path),
        )
        assert appended == ["DUS:SSUaDU"]

    def test_double_comment_marker_is_recognised_as_present(self, tmp_path):
        # ``## "NMS:FISV" = "..."`` is still a comment in TOML
        # (TOML uses ``#`` for the rest of the line; a second
        # ``#`` is just part of the comment body) but the
        # predicate should treat it as "already mentioned" because
        # it visibly carries the ticker's assignment shape -- a
        # human reader would intuit the intent.
        path = _write_overrides(
            tmp_path,
            '[sectors]\n## "NMS:FISV" = "Technology"\n',
        )
        original = path.read_text(encoding="utf-8")
        appended = append_missing_sector_stubs(["NMS:FISV"], path=str(path))
        assert appended == []
        assert path.read_text(encoding="utf-8") == original

    def test_uses_default_path_when_argument_omitted(
        self, tmp_path, monkeypatch,
    ):
        # Production callsites omit ``path``; the function must
        # then fall through to :data:`_SECTOR_OVERRIDES_PATH`. The
        # test redirects the default at a tmp file to confirm the
        # plumbing without mutating the shipped TOML.
        from investing import sector_overrides as so

        default_path = tmp_path / "default.toml"
        default_path.write_text("[sectors]\n", encoding="utf-8")
        monkeypatch.setattr(so, "_SECTOR_OVERRIDES_PATH", str(default_path))

        appended = append_missing_sector_stubs(["NMS:ZZZ"])
        assert appended == ["NMS:ZZZ"]
        assert '"NMS:ZZZ"' in default_path.read_text(encoding="utf-8")

    def test_oserror_on_read_is_swallowed(self, tmp_path, monkeypatch):
        # A file we can't open for reading (permissions revoked,
        # remote filesystem hiccup) must not crash the build. The
        # function returns ``[]`` so the caller's summary line
        # stays silent rather than reporting phantom stubs.
        import builtins

        path = _write_overrides(tmp_path, "[sectors]\n")
        real_open = builtins.open

        def _failing_open(p, *args, **kwargs):
            if str(p) == str(path) and (not args or "r" in args[0]):
                raise OSError("simulated read failure")
            return real_open(p, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _failing_open)
        appended = append_missing_sector_stubs(["NMS:AAA"], path=str(path))
        assert appended == []

    def test_oserror_on_write_is_swallowed(self, tmp_path, monkeypatch):
        # The same defence on the write side. A POSIX append that
        # fails mid-build must not abort the deploy -- the next
        # build's dedupe pass will retry against whatever the file
        # actually ended up containing.
        import builtins

        path = _write_overrides(tmp_path, "[sectors]\n")
        original = path.read_text(encoding="utf-8")
        real_open = builtins.open

        def _failing_open(p, *args, **kwargs):
            if str(p) == str(path) and args and "a" in args[0]:
                raise OSError("simulated write failure")
            return real_open(p, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _failing_open)
        appended = append_missing_sector_stubs(["NMS:AAA"], path=str(path))
        assert appended == []
        # File contents must be untouched (we got past the open
        # but the write itself failed -- partial writes are
        # technically possible but the simulated ``OSError``
        # fires before any bytes hit disk).
        assert path.read_text(encoding="utf-8") == original


class TestRepoOverridesFile:
    def test_repo_file_parses_cleanly(self):
        # The shipped ``sector_overrides.toml`` at the repo root
        # should always parse without errors and validate every
        # entry against :data:`KNOWN_SECTORS`. A new entry whose
        # sector value is mistyped would otherwise fail silently in
        # production (the renderer falls back to the ``Other``
        # bucket); this pins the file as a load-time tripwire.
        _clear_overrides_cache()
        overrides = _load_overrides()
        for ticker, sector in overrides.items():
            assert sector in KNOWN_SECTORS, (
                f"{ticker} overrides to invalid sector {sector!r}"
            )
        # No invalid-override hint should have been recorded as a
        # side effect of parsing the production file -- if it had,
        # the same parse loop above would have skipped the bad
        # entry and the assertion would never have caught it.
        assert not consume_hints().invalid_overrides


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Belt-and-braces hint reset for this file.

    The repo-wide ``_reset_sector_override_state`` autouse fixture in
    ``conftest.py`` already clears hints around each test; this
    file's tests assert directly on the hint registry so we keep a
    local equivalent that runs as a sibling fixture rather than
    relying on the global one. Both fixtures end up calling the same
    teardown so an accidental removal of either still leaves at
    least one cleaning the slate.
    """
    reset_hints()
    _clear_overrides_cache()
    yield
    reset_hints()
    _clear_overrides_cache()
