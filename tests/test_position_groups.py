"""Tests for :mod:`investing.position_groups`.

Coverage focus:

* TOML loader -- happy path, missing file, malformed file, and each
  shape of malformed group entry.
* The one hard error: a ticker claimed by two groups.
* :func:`resolve_groups` narrowing against the portfolio's held set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from investing.errors import InvariantError
from investing.position_groups import (
    PositionGroup,
    _clear_groups_cache,
    load_groups,
    resolve_groups,
)

SAMSUNG = """
[samsung]
primary = "DUS:SSU.DU"
members = ["IOB:SMSN.IL"]
name = "Samsung Electronics"
label = "Samsung"
"""


def _write_groups(tmp_path: Path, body: str) -> Path:
    """Drop a groups TOML under ``tmp_path`` and return its path.

    Every test passes the path explicitly so the maintainer's real
    ``position_groups.toml`` never enters the test surface.
    """
    path = tmp_path / "position_groups.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoadGroups:
    def test_parses_a_full_group(self, tmp_path):
        path = _write_groups(tmp_path, SAMSUNG)
        (group,) = load_groups(str(path))
        assert group.key == "samsung"
        assert group.primary == "DUS:SSU.DU"
        assert group.name == "Samsung Electronics"
        assert group.label == "Samsung"
        # ``tickers`` always leads with the primary so the tooltip
        # names the identity listing first.
        assert group.tickers == ("DUS:SSU.DU", "IOB:SMSN.IL")

    def test_optional_fields_default_to_none(self, tmp_path):
        path = _write_groups(
            tmp_path,
            """
            [alphabet]
            primary = "NMS:GOOGL"
            members = ["NMS:GOOG"]
            """,
        )
        (group,) = load_groups(str(path))
        assert group.name is None
        # No label: the primary's bare symbol is already the
        # recognisable form for a share-class pairing.
        assert group.label is None

    def test_missing_file_yields_no_groups(self, tmp_path):
        assert load_groups(str(tmp_path / "absent.toml")) == ()

    def test_malformed_toml_degrades_to_no_groups(self, tmp_path):
        path = _write_groups(tmp_path, "[unclosed\n")
        assert load_groups(str(path)) == ()

    @pytest.mark.parametrize(
        "body",
        [
            # Not a table.
            'samsung = "DUS:SSU.DU"',
            # No primary.
            '[samsung]\nmembers = ["IOB:SMSN.IL"]',
            # Blank primary.
            '[samsung]\nprimary = "  "\nmembers = ["IOB:SMSN.IL"]',
            # Primary of the wrong type.
            '[samsung]\nprimary = 42\nmembers = ["IOB:SMSN.IL"]',
            # Members not a list.
            '[samsung]\nprimary = "DUS:SSU.DU"\nmembers = "IOB:SMSN.IL"',
            # Members carrying a non-string.
            '[samsung]\nprimary = "DUS:SSU.DU"\nmembers = [7]',
            # A "group" of one leg is not a combination.
            '[samsung]\nprimary = "DUS:SSU.DU"\nmembers = []',
            # Same, spelled with the primary repeated.
            '[samsung]\nprimary = "DUS:SSU.DU"\nmembers = ["DUS:SSU.DU"]',
        ],
    )
    def test_malformed_entries_are_dropped(self, tmp_path, body):
        # A group that cannot be parsed leaves its legs independent,
        # which is the pre-existing rendering -- never wrong numbers,
        # so the build degrades rather than aborting.
        path = _write_groups(tmp_path, body)
        assert load_groups(str(path)) == ()

    def test_primary_repeated_in_members_is_deduplicated(self, tmp_path):
        path = _write_groups(
            tmp_path,
            """
            [samsung]
            primary = "DUS:SSU.DU"
            members = ["DUS:SSU.DU", "IOB:SMSN.IL", "IOB:SMSN.IL"]
            """,
        )
        (group,) = load_groups(str(path))
        assert group.tickers == ("DUS:SSU.DU", "IOB:SMSN.IL")

    def test_ticker_claimed_by_two_groups_raises(self, tmp_path):
        # The one unresolvable case. Silently picking a winner would
        # drop the loser's cashflows out of the portfolio entirely, so
        # this is the single hard failure in the loader.
        path = _write_groups(
            tmp_path,
            """
            [one]
            primary = "DUS:SSU.DU"
            members = ["IOB:SMSN.IL"]

            [two]
            primary = "NMS:AAA"
            members = ["IOB:SMSN.IL"]
            """,
        )
        with pytest.raises(InvariantError, match=re.escape("IOB:SMSN.IL")):
            load_groups(str(path))

    def test_repeated_reads_are_cached_only_for_the_default_path(self, tmp_path):
        # An explicit path always re-reads, so a test rewriting its
        # fixture mid-run sees the new bytes.
        path = _write_groups(tmp_path, SAMSUNG)
        assert load_groups(str(path))[0].label == "Samsung"
        _write_groups(tmp_path, SAMSUNG.replace('label = "Samsung"', 'label = "SSNLF"'))
        assert load_groups(str(path))[0].label == "SSNLF"
        _clear_groups_cache()


class TestResolveGroups:
    def test_applies_when_every_leg_is_held(self, tmp_path):
        path = _write_groups(tmp_path, SAMSUNG)
        (group,) = resolve_groups({"DUS:SSU.DU", "IOB:SMSN.IL"}, path=str(path))
        assert group.tickers == ("DUS:SSU.DU", "IOB:SMSN.IL")

    def test_skipped_when_only_one_leg_is_held(self, tmp_path):
        # Declaring a pairing before actually buying the second
        # listing is supported: the entry sits inert and the held leg
        # renders as an ordinary standalone position.
        path = _write_groups(tmp_path, SAMSUNG)
        assert resolve_groups({"DUS:SSU.DU"}, path=str(path)) == ()

    def test_skipped_when_the_primary_is_not_held(self, tmp_path):
        # Without the primary there is no leg to inherit the logo,
        # anchor and sector from, so the remaining leg stays standalone
        # rather than silently being promoted.
        path = _write_groups(tmp_path, SAMSUNG)
        assert resolve_groups({"IOB:SMSN.IL"}, path=str(path)) == ()

    def test_narrows_members_to_the_held_legs(self, tmp_path):
        path = _write_groups(
            tmp_path,
            """
            [samsung]
            primary = "DUS:SSU.DU"
            members = ["IOB:SMSN.IL", "KSC:005930.KS"]
            """,
        )
        (group,) = resolve_groups(
            {"DUS:SSU.DU", "IOB:SMSN.IL"},
            path=str(path),
        )
        # The Seoul line is described by the config but not held, so
        # it drops out rather than producing a phantom leg.
        assert group.tickers == ("DUS:SSU.DU", "IOB:SMSN.IL")

    def test_no_config_means_no_grouping(self, tmp_path):
        assert resolve_groups({"NMS:AAA"}, path=str(tmp_path / "absent.toml")) == ()


class TestPositionGroupShape:
    def test_tickers_puts_primary_first_regardless_of_member_order(self):
        group = PositionGroup(
            key="k",
            primary="DUS:SSU.DU",
            members=("IOB:SMSN.IL", "DUS:SSU.DU"),
        )
        assert group.tickers == ("DUS:SSU.DU", "IOB:SMSN.IL")
