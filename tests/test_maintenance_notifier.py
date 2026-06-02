"""Tests for :mod:`investing.maintenance_notifier`.

The notifier touches the GitHub Issues REST API via a ``requests``
session, so every test substitutes a ``MagicMock`` for the session
and asserts on the requested URLs / payloads. Coverage focus:

* Env-var gating: missing opt-in, missing token, or missing repo
  slug all short-circuit to a strict no-op (no API calls at all).
* Empty hints short-circuit before even reading the env (cheapest
  possible no-op path).
* Dedup: a matching issue (open OR closed) suppresses the create
  call. ``invalid_overrides`` adds an exact title match on top.
* Per-category routing: missing-sector, missing-logo and
  invalid-override hints each produce their own label / title /
  body shape.
* Defensive failure modes: network errors / non-200 lookups treat
  the issue as already present so a transient outage cannot spam
  duplicates.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from investing import maintenance_notifier as notifier
from investing.sector_overrides import MaintenanceHints

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(payload, status=200):
    """Build a ``requests.Response``-shaped MagicMock with JSON payload."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _enable_notifier(monkeypatch, *, repo="acme/widgets", token="ghs_xxx"):
    """Set the env vars that ``_read_context`` consults.

    Centralised so individual tests don't repeat the three-line
    setup and so a future env-var rename only has to update this
    helper.
    """
    monkeypatch.setenv("INVESTING_NOTIFY_GITHUB", "1")
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)


def _install_session(monkeypatch):
    """Patch ``_build_session`` to return a MagicMock and surface it.

    The returned MagicMock is what the test asserts on -- its
    ``get`` / ``post`` mock attributes record every API call the
    notifier made. ``_build_session`` is patched (rather than the
    module-level ``requests.Session``) so the test doesn't have to
    care about session header configuration; the auth path is
    exercised separately in :class:`TestBuildSession`.
    """
    session = MagicMock()
    monkeypatch.setattr(
        notifier,
        "_build_session",
        lambda token: session,  # noqa: ARG005
    )
    return session


# ---------------------------------------------------------------------------
# Env gating
# ---------------------------------------------------------------------------


class TestEnvGating:
    def test_no_opt_in_is_strict_noop(self, monkeypatch):
        # Without the opt-in env var the notifier must never touch
        # the API even when hints are populated. Confirms forks
        # default to silent.
        monkeypatch.delenv("INVESTING_NOTIFY_GITHUB", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_xxx")
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        session = _install_session(monkeypatch)
        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))
        assert session.method_calls == []

    def test_opt_in_without_token_is_noop(self, monkeypatch):
        monkeypatch.setenv("INVESTING_NOTIFY_GITHUB", "1")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        session = _install_session(monkeypatch)
        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))
        assert session.method_calls == []

    def test_opt_in_without_repo_is_noop(self, monkeypatch):
        monkeypatch.setenv("INVESTING_NOTIFY_GITHUB", "1")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_xxx")
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        session = _install_session(monkeypatch)
        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))
        assert session.method_calls == []

    def test_empty_hints_short_circuits_before_env_read(self, monkeypatch):
        # Empty hints means there's nothing to notify about; the
        # notifier should bail before even resolving the runtime
        # context. The session must therefore stay untouched.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        notifier.notify_github(MaintenanceHints())
        assert session.method_calls == []


# ---------------------------------------------------------------------------
# API URL resolution
# ---------------------------------------------------------------------------


class TestApiRoot:
    def test_defaults_to_public_github(self):
        ctx = notifier._GitHubContext(
            token="t", repo="acme/widgets", api_url=None
        )
        assert notifier._api_root(ctx) == "https://api.github.com/repos/acme/widgets"

    def test_honours_github_api_url(self):
        # GitHub Enterprise / staging deployments set ``GITHUB_API_URL``
        # to point at their own host. The notifier must respect that
        # so a fork on a private GitHub doesn't try to POST against
        # the public API host.
        ctx = notifier._GitHubContext(
            token="t",
            repo="acme/widgets",
            api_url="https://github.example.com/api/v3",
        )
        assert (
            notifier._api_root(ctx)
            == "https://github.example.com/api/v3/repos/acme/widgets"
        )

    def test_strips_trailing_slash(self):
        # Defensive: a stray trailing slash on the env var should
        # not produce a double-slash in the URL.
        ctx = notifier._GitHubContext(
            token="t",
            repo="acme/widgets",
            api_url="https://api.github.com/",
        )
        assert (
            notifier._api_root(ctx)
            == "https://api.github.com/repos/acme/widgets"
        )


# ---------------------------------------------------------------------------
# Issue dedup
# ---------------------------------------------------------------------------


class TestMissingSectorRouting:
    def test_creates_issue_when_no_match(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.return_value = _ok_response({"number": 42}, status=201)

        notifier.notify_github(
            MaintenanceHints(missing_sector=["NMS:AAA"])
        )

        assert session.post.call_count == 1
        call = session.post.call_args
        # POST to the repo's /issues endpoint with the expected
        # title / labels / body shape.
        url = call.args[0] if call.args else call.kwargs.get("url")
        assert url.endswith("/repos/acme/widgets/issues")
        body = call.kwargs["json"]
        assert body["title"] == "Missing sector for NMS:AAA"
        assert set(body["labels"]) == {
            "maintenance",
            "sector",
            "ticker:NMS:AAA",
        }
        assert "NMS:AAA" in body["body"]
        assert "Technology" in body["body"]  # canonical list is rendered

    def test_skips_create_when_open_issue_matches(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response(
            [{"number": 7, "state": "open", "title": "Missing sector for NMS:AAA"}]
        )

        notifier.notify_github(
            MaintenanceHints(missing_sector=["NMS:AAA"])
        )

        # Lookup happened, but the create call MUST NOT fire.
        assert session.get.call_count == 1
        assert session.post.call_count == 0

    def test_skips_create_when_closed_issue_matches(self, monkeypatch):
        # The "once if ignored" guarantee depends on closed issues
        # also suppressing future notifications -- a maintainer who
        # closes-without-fix should not get re-notified on the next
        # scheduled build.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response(
            [{"number": 7, "state": "closed", "title": "Missing sector for NMS:AAA"}]
        )

        notifier.notify_github(
            MaintenanceHints(missing_sector=["NMS:AAA"])
        )

        assert session.post.call_count == 0

    def test_lookup_uses_state_all(self, monkeypatch):
        # Regression guard for the "once if ignored" semantic: the
        # query string must include ``state=all`` so closed issues
        # are part of the dedupe pool.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.return_value = _ok_response({"number": 1}, status=201)

        notifier.notify_github(
            MaintenanceHints(missing_sector=["NMS:AAA"])
        )

        url = session.get.call_args.args[0]
        assert "state=all" in url
        assert "labels=" in url
        # All three labels appear in the labels query parameter.
        # GitHub accepts colons in label names unescaped (they're
        # not URL-reserved in the query value), and the dedupe
        # query intentionally leaves them as-is so the label-name
        # spelling round-trips identically to what the create
        # endpoint stores.
        assert "labels=maintenance,sector,ticker:NMS:AAA" in url


class TestMissingLogoRouting:
    def test_creates_logo_issue(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.return_value = _ok_response({"number": 11}, status=201)

        notifier.notify_github(MaintenanceHints(missing_logos=["NMS:XYZ"]))

        body = session.post.call_args.kwargs["json"]
        assert body["title"] == "Missing logo for NMS:XYZ"
        assert set(body["labels"]) == {
            "maintenance",
            "logo",
            "ticker:NMS:XYZ",
        }
        # Body should mention the on-disk destination so the
        # maintainer can act without reading the source.
        assert "logos/NMS:XYZ" in body["body"]

    def test_dedupes_logo_issue_by_label_only(self, monkeypatch):
        # Logo issues key purely on the ticker / category pair, no
        # value component (unlike invalid-overrides). Any matching
        # issue suppresses creation.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response(
            [{"number": 7, "state": "open", "title": "anything"}]
        )

        notifier.notify_github(MaintenanceHints(missing_logos=["NMS:XYZ"]))

        assert session.post.call_count == 0


class TestInvalidOverrideRouting:
    def test_creates_issue_with_value_in_title(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.return_value = _ok_response({"number": 99}, status=201)

        notifier.notify_github(
            MaintenanceHints(invalid_overrides={"NMS:AAA": "Tech"})
        )

        body = session.post.call_args.kwargs["json"]
        # Title carries the bad value so the maintainer reads it
        # straight off the email subject line.
        assert body["title"] == "Invalid sector override 'Tech' for NMS:AAA"
        assert "'Tech'" in body["body"]
        assert set(body["labels"]) == {
            "maintenance",
            "invalid-override",
            "ticker:NMS:AAA",
        }

    def test_dedupes_by_title_when_value_matches(self, monkeypatch):
        # The same bad value on the same ticker should NOT re-file
        # -- the existing issue's title matches verbatim.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response(
            [
                {
                    "number": 7,
                    "state": "open",
                    "title": "Invalid sector override 'Tech' for NMS:AAA",
                }
            ]
        )

        notifier.notify_github(
            MaintenanceHints(invalid_overrides={"NMS:AAA": "Tech"})
        )

        assert session.post.call_count == 0

    def test_refiles_when_value_changes(self, monkeypatch):
        # A *different* bad value on the same ticker should produce
        # a fresh issue -- the title narrowing is the "once per
        # value" guarantee that lets the maintainer triage a new
        # typo independently from an older one.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response(
            [
                {
                    "number": 7,
                    "state": "closed",
                    "title": "Invalid sector override 'OLDBAD' for NMS:AAA",
                }
            ]
        )
        session.post.return_value = _ok_response({"number": 99}, status=201)

        notifier.notify_github(
            MaintenanceHints(invalid_overrides={"NMS:AAA": "Tech"})
        )

        assert session.post.call_count == 1
        body = session.post.call_args.kwargs["json"]
        assert body["title"] == "Invalid sector override 'Tech' for NMS:AAA"


class TestMultipleCategoriesOneBuild:
    def test_each_hint_gets_its_own_issue(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.return_value = _ok_response({"number": 1}, status=201)

        notifier.notify_github(
            MaintenanceHints(
                missing_sector=["NMS:AAA", "NMS:BBB"],
                missing_logos=["NMS:CCC"],
                invalid_overrides={"NMS:DDD": "Tech"},
            )
        )

        # 2 sector + 1 logo + 1 invalid -> 4 lookups + 4 creates.
        assert session.get.call_count == 4
        assert session.post.call_count == 4
        titles = {c.kwargs["json"]["title"] for c in session.post.call_args_list}
        assert titles == {
            "Missing sector for NMS:AAA",
            "Missing sector for NMS:BBB",
            "Missing logo for NMS:CCC",
            "Invalid sector override 'Tech' for NMS:DDD",
        }


# ---------------------------------------------------------------------------
# Defensive failure paths
# ---------------------------------------------------------------------------


class TestDefensiveLookup:
    def test_network_error_on_lookup_skips_create(self, monkeypatch):
        # A flaky API connection during the lookup phase must NOT
        # fall through to a POST -- the alternative would spam
        # duplicate issues on every retried build.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.side_effect = requests.ConnectionError("simulated")

        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))

        assert session.post.call_count == 0

    def test_non_200_status_on_lookup_skips_create(self, monkeypatch):
        # Same guard for a 5xx / 401 / 403 from the API. Treating
        # the failure as "issue might already exist" is safer than
        # assuming absence.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response(None, status=503)

        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))

        assert session.post.call_count == 0

    def test_non_json_body_on_lookup_skips_create(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not JSON")
        session.get.return_value = resp

        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))

        assert session.post.call_count == 0


class TestDefensiveCreate:
    def test_network_error_on_create_is_swallowed(self, monkeypatch):
        # A failed POST must not abort the build. The next build's
        # lookup will return no match (the previous run failed to
        # create the issue) and the create will be retried.
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.side_effect = requests.ConnectionError("simulated")

        # Must not raise.
        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))

    def test_non_201_status_on_create_is_swallowed(self, monkeypatch):
        _enable_notifier(monkeypatch)
        session = _install_session(monkeypatch)
        session.get.return_value = _ok_response([])
        session.post.return_value = _ok_response(None, status=422)

        # Must not raise.
        notifier.notify_github(MaintenanceHints(missing_sector=["NMS:AAA"]))


# ---------------------------------------------------------------------------
# Session wiring
# ---------------------------------------------------------------------------


class TestBuildSession:
    def test_session_carries_bearer_auth_and_version(self):
        # Pin the exact header shape so a future GitHub API
        # deprecation lands as a single-file edit rather than a
        # mystery 401 in CI.
        session = notifier._build_session("ghs_xxx")
        try:
            assert session.headers["Authorization"] == "Bearer ghs_xxx"
            assert session.headers["Accept"] == "application/vnd.github+json"
            assert session.headers["X-GitHub-Api-Version"] == "2022-11-28"
            assert "investing" in session.headers["User-Agent"]
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class TestBuildPageWiring:
    """The ``cli.build_page`` orchestrator must drain the hint
    registry exactly once per build and share the resulting
    :class:`MaintenanceHints` snapshot between
    :func:`_print_summary` (always-on stdout) and
    :func:`notify_github` (opt-in GitHub Issues sync). Draining
    twice would lose hints to the second consumer, so the test
    pins the contract directly rather than trusting both consumers
    to coincidentally call ``consume_hints`` themselves.
    """

    def test_notifier_receives_hints_recorded_during_build(self, monkeypatch):
        # Plant a hint *before* build_page runs and stub every other
        # pipeline step so the test is exclusively about the hint
        # plumbing. ``build_page`` calls ``reset_hints`` first, so
        # planting the hint via a pull-stub side-effect is the
        # cleanest way to inject one that survives that reset.
        from investing import cli, sector_overrides

        def stub_pull():
            sector_overrides.record_missing_sector("NMS:STUB")
            return ([], [], [])

        monkeypatch.setattr(
            cli, "get_holdings",
            lambda *a, **kw: {"current": [], "historical": []},  # noqa: ARG005
        )

        class _StubRollup:
            total_value_usd = 0.0

        monkeypatch.setattr(
            cli, "compute_rollup", lambda *a, **kw: _StubRollup()  # noqa: ARG005
        )
        monkeypatch.setattr(
            cli, "apply_rollup", lambda *a, **kw: None  # noqa: ARG005
        )
        monkeypatch.setattr(
            cli, "calc_twr",
            lambda *a, **kw: {"history": [], "twr%": 0.0, "cagr%": 0.0},  # noqa: ARG005
        )
        monkeypatch.setattr(
            cli, "get_benchmarks", lambda *a, **kw: []  # noqa: ARG005
        )

        notified: list = []
        monkeypatch.setattr(cli, "notify_github", lambda hints: notified.append(hints))

        def _stub_save(*_args, **_kw):
            return None

        cli.build_page(pull=stub_pull, save=_stub_save)

        # Exactly one notification, carrying the hint planted during
        # the pull stub.
        assert len(notified) == 1
        assert notified[0].missing_sector == ["NMS:STUB"]
        # And the hint registry must be empty afterwards -- the
        # snapshot is supposed to drain the registry exactly once,
        # so a subsequent build starts clean even without an
        # explicit ``reset_hints`` at the boundary.
        from investing.sector_overrides import consume_hints

        assert consume_hints().is_empty


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Strip every notifier-related env var around each test.

    Tests opt back in via :func:`_enable_notifier` -- the autouse
    cleanup means a single test setting the env var cannot leak the
    opt-in state into a sibling test that intentionally wants to
    exercise the no-op path.
    """
    for name in (
        "INVESTING_NOTIFY_GITHUB",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
