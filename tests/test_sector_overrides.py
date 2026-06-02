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
