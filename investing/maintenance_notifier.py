"""Open GitHub issues for accumulated maintenance hints.

Companion to :mod:`investing.sector_overrides` -- where that module
records *which* tickers need attention (missing sector, missing logo,
typo'd override), this module turns those records into actionable
notifications for the repository's maintainer.

Mechanism: a per-build sync against the repo's Issues API. For each
hint we look up "is there already an issue for this ticker /
category?" and only file a new one when nothing matches. The lookup
is intentionally inclusive of CLOSED issues so a hint the maintainer
deliberately ignored stays ignored -- once filed, an issue is the
single source of truth for that ticker / category pair regardless of
whether it ends up resolved, won't-fix or never read. The build
therefore notifies *at most once per ticker per category*, satisfying
the "no nagging" contract.

Email delivery is delegated to GitHub: the maintainer's notification
preferences turn a freshly opened issue (carrying the maintainer's
own login or a watch on the repository) into an email / push /
in-app notification with no per-project SMTP plumbing on this side.

The notifier is opt-in via the ``INVESTING_NOTIFY_GITHUB`` env var so
local runs (``python -m investing`` on a developer machine) and the
preview script never accidentally file an issue, and a fork's CI
without the env var defaults to a silent no-op rather than spamming
issues against a repository the fork's owner may not control.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

from .log import logger
from .sector_overrides import KNOWN_SECTORS, MaintenanceHints

# Per-request timeout for every Issues API call. Generous enough to
# absorb a slow handshake from GitHub's API edge but not so loose
# that a stalled connection would extend the production build by
# meaningful seconds. The notifier's failure mode is *always*
# silent-skip-and-log, so a too-tight timeout just means the next
# build re-attempts the issue creation -- the dedupe pass keeps that
# safe.
_REQUEST_TIMEOUT_S: tuple[float, float] = (5.0, 10.0)

# Labels applied to every issue this module opens. The top-level
# ``maintenance`` label lets the maintainer filter the repo's issue
# list down to just the auto-filed ones; the category label
# (``sector`` / ``logo``) lets the issues UI bulk-triage by gap
# type; the ``ticker:`` label is what powers the dedupe lookup and
# keeps the open-once invariant intact across rebuilds.
_LABEL_ROOT = "maintenance"
_LABEL_SECTOR = "sector"
_LABEL_LOGO = "logo"
_LABEL_INVALID_OVERRIDE = "invalid-override"


@dataclass(frozen=True)
class NotifierOutcome:
    """Per-build counters describing what :func:`notify_github` did.

    The CLI surfaces these counts on the curated build-summary
    stream (via :func:`investing.cli.emit_summary`) so the
    operator sees, in the public job log, whether the notifier
    actually managed to file issues -- not just "the env vars were
    set". Three failure modes hide behind the previous "log a
    warning then move on" behaviour: a fork forgetting to enable
    ``issues: write``, a repository with Issues turned off (the
    case that surfaced this dataclass), and a flaky API connection.
    All three show up here as a non-zero ``failed`` count instead
    of the previous total opacity.

    ``enabled`` distinguishes "notifier deliberately skipped because
    the env gate was unmet" (e.g. local ``python -m investing``)
    from "notifier ran". Callers that suppress the summary line on
    ``enabled=False`` keep local runs quiet without losing the CI
    diagnostic.
    """

    enabled: bool = False
    opened: list[str] = field(default_factory=list)
    already_tracked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.opened or self.already_tracked or self.failed)


def _ticker_label(ticker: str) -> str:
    """Per-ticker dedupe label.

    Kept as a helper so the label format lives in exactly one place
    -- a future migration (e.g. URL-safe encoding of ``:`` for
    repositories whose label-naming policy rejects colons) only has
    to edit this one return statement. GitHub accepts colons in
    label names today so the raw ``ticker:NMS:XYZ`` shape is the
    most direct mapping back to the on-page identifier.
    """
    return f"ticker:{ticker}"


@dataclass(frozen=True)
class _GitHubContext:
    """Subset of the GitHub Actions runtime the notifier needs.

    ``token`` is the ``GITHUB_TOKEN`` injected by the workflow (with
    ``permissions: issues: write`` opted in). ``repo`` is the
    ``owner/name`` slug from ``GITHUB_REPOSITORY``. ``api_url`` lets
    a GitHub Enterprise deployment repoint the notifier at its own
    Issues API endpoint without code changes (``GITHUB_API_URL`` is
    the canonical workflow-runtime env var for this); ``None`` falls
    back to the public API host inside :func:`_api_root`.
    """

    token: str
    repo: str
    api_url: str | None


def _read_context() -> _GitHubContext | None:
    """Resolve the runtime context, or return ``None`` if the notifier
    is disabled.

    The notifier is a strict no-op unless every prerequisite is
    present: the opt-in flag, an auth token, and a repository slug.
    Local invocations (no opt-in set) and forks (no token by
    default) both fall into the no-op branch silently. The
    :func:`logger.info` lines below double as a hint to anybody
    reading their local terminal: "the notifier saw the opt-in flag
    but couldn't find a token", which is the exact failure mode of a
    misconfigured workflow.
    """
    if not os.environ.get("INVESTING_NOTIFY_GITHUB"):
        return None
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        logger.info(
            "maintenance notifier: opt-in set but GITHUB_TOKEN / "
            "GITHUB_REPOSITORY missing; skipping",
        )
        return None
    return _GitHubContext(
        token=token,
        repo=repo,
        api_url=os.environ.get("GITHUB_API_URL"),
    )


def _api_root(ctx: _GitHubContext) -> str:
    """Base URL for the repo's Issues API.

    Honours ``GITHUB_API_URL`` (set automatically by GitHub Actions;
    differs on GitHub Enterprise Server) so the notifier doesn't
    need to hard-code ``api.github.com``. Strips any trailing slash
    so the caller can concatenate ``/repos/...`` without worrying
    about double-slashes.
    """
    base = ctx.api_url.rstrip("/") if ctx.api_url else "https://api.github.com"
    return f"{base}/repos/{ctx.repo}"


def _build_session(token: str) -> requests.Session:
    """Construct an authenticated session with the canonical headers.

    Uses the modern ``Authorization: Bearer`` form (GitHub still
    accepts the older ``token`` scheme but the docs explicitly
    recommend bearer for new code). The ``X-GitHub-Api-Version``
    pin keeps us on a known schema even after GitHub rolls forward
    the default -- the response shapes the notifier depends on
    (issue ``state`` / ``labels`` / ``number``) have been stable
    since the API was versioned, but pinning makes the dependency
    explicit and lets us audit upgrades.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "investing-maintenance-notifier",
        }
    )
    return session


# Sentinels for the three possible outcomes of an issue lookup.
# Plain strings (rather than an Enum) so the values render
# self-explanatorily in stack traces and ``repr`` output of test
# failures without requiring an import to interpret.
_LOOKUP_FOUND = "found"
_LOOKUP_NOT_FOUND = "not_found"
_LOOKUP_FAILED = "failed"


def _issue_exists(
    session: requests.Session,
    api_root: str,
    *,
    labels: list[str],
    title: str | None = None,
) -> str:
    """Return one of :data:`_LOOKUP_FOUND` / :data:`_LOOKUP_NOT_FOUND`
    / :data:`_LOOKUP_FAILED` for the lookup against ``api_root``.

    The lookup is intentionally **inclusive of closed issues**: the
    "once if ignored" contract relies on a maintainer-closed issue
    suppressing future notifications for the same ticker / category
    pair. The Issues list endpoint's ``state=all`` query parameter
    + label intersection gives us exactly that.

    ``title`` is an optional secondary match used only by the
    invalid-override category, where the dedup key is
    ``(ticker, bad_value)`` rather than just ``ticker``: a typo'd
    override that gets fixed and then re-introduced as a different
    typo should produce a fresh issue, which the title-equality
    fallback achieves on top of the label intersection.

    The :data:`_LOOKUP_FAILED` sentinel preserves the historical
    "treat unknown as exists" safety net (the caller still skips
    ``_create_issue`` on a failed lookup so a flaky API connection
    can't spam duplicates) while letting the outcome counter
    distinguish a real existing-issue dedupe from a transient
    fault -- the operator needs that signal to spot a misconfigured
    workflow (e.g. ``issues: write`` not granted, or Issues turned
    off on the repository entirely).
    """
    encoded_labels = urllib.parse.quote(",".join(labels), safe=",:-")
    url = (
        f"{api_root}/issues?state=all&per_page=100"
        f"&labels={encoded_labels}"
    )
    try:
        response = session.get(url, timeout=_REQUEST_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.warning(
            "maintenance notifier: GET %s failed (%s); skipping create",
            url,
            type(exc).__name__,
        )
        return _LOOKUP_FAILED
    if response.status_code != 200:
        logger.warning(
            "maintenance notifier: GET %s returned %d; skipping create",
            url,
            response.status_code,
        )
        return _LOOKUP_FAILED
    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "maintenance notifier: non-JSON response from %s; skipping create",
            url,
        )
        return _LOOKUP_FAILED
    if not isinstance(payload, list):
        return _LOOKUP_FAILED
    if title is None:
        # Pure label-intersection match -- any returned issue means
        # "we've notified about this ticker / category before".
        return _LOOKUP_FOUND if payload else _LOOKUP_NOT_FOUND
    # Title-equality narrowing for the invalid-override category.
    # GitHub returns the ``title`` field verbatim so a string
    # comparison is the right primitive here; no canonicalisation
    # needed.
    return (
        _LOOKUP_FOUND
        if any(item.get("title") == title for item in payload)
        else _LOOKUP_NOT_FOUND
    )


def _create_issue(
    session: requests.Session,
    api_root: str,
    *,
    title: str,
    body: str,
    labels: list[str],
) -> bool:
    """POST a fresh issue, returning ``True`` on success.

    Failures are swallowed and logged: the notifier never blocks
    the build, and the dedup pass on the next run keeps it safe to
    retry. The return value lets the caller bump a "filed" counter
    for the summary line if we ever decide to expose one.
    """
    url = f"{api_root}/issues"
    payload: dict[str, Any] = {"title": title, "body": body, "labels": labels}
    try:
        response = session.post(url, json=payload, timeout=_REQUEST_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.warning(
            "maintenance notifier: POST %s failed (%s)",
            url,
            type(exc).__name__,
        )
        return False
    if response.status_code not in (200, 201):
        logger.warning(
            "maintenance notifier: POST %s returned %d",
            url,
            response.status_code,
        )
        return False
    logger.info("maintenance notifier: opened issue %r", title)
    return True


# ---------------------------------------------------------------------------
# Issue body templates
# ---------------------------------------------------------------------------


def _missing_sector_body(ticker: str) -> str:
    """Email-friendly Markdown body for a missing-sector issue.

    The body is the maintainer's actionable checklist: where to
    edit, what canonical values are valid, and why the gap matters
    (the renderer falls back to the neutral ``Other`` tile, which
    is fine but loses the colour signal). Sectors are listed in
    sorted order so the same body renders identically across builds
    -- a build that re-creates the issue (after the maintainer
    accidentally deletes it, say) won't churn the body shape.
    """
    sector_list = "\n".join(f"- `{s}`" for s in sorted(KNOWN_SECTORS))
    return (
        f"yfinance returned a blank `info[\"sector\"]` for `{ticker}` "
        "on the most recent production build, and no override is "
        "present in `sector_overrides.toml`. The equities treemap "
        "groups this ticker under the neutral `Other` tile until an "
        "override is provided.\n\n"
        "**To resolve:** add an entry to "
        "[`sector_overrides.toml`](../blob/main/sector_overrides.toml) "
        "under `[sectors]`, mapping the ticker to one of:\n\n"
        f"{sector_list}\n\n"
        "Example:\n\n"
        "```toml\n"
        f'"{ticker}" = "Technology"\n'
        "```\n\n"
        "If yfinance is expected to populate the field on its own "
        "shortly (a freshly listed name, for example), close this "
        "issue without action -- the notifier will not re-file it. "
        "The next build that observes a real sector for the ticker "
        "will silently stop emitting the hint."
    )


def _missing_logo_body(ticker: str) -> str:
    """Markdown body for a missing-logo issue.

    Covers the file-naming convention (the directory listing in
    ``logos/`` already follows it, so the body just points the
    maintainer at the existing pattern) and the pre-commit hook
    that regenerates the served ``logos/tight/`` mirror.
    """
    return (
        f"`LogoCache` exhausted every probe for `{ticker}` on the "
        "most recent production build -- neither the repo's "
        "`logos/tight/` mirror nor the deployed `LOGOS_ADDRESS` "
        "served a hand-curated file. The renderer falls back to the "
        "`courage.png` placeholder until a logo is provided.\n\n"
        "**To resolve:** drop a hand-curated SVG (preferred), PNG "
        f"or JPG into `logos/{ticker}.<ext>`. The `tighten-logos` "
        "pre-commit hook will regenerate the served `logos/tight/` "
        "mirror automatically when the commit lands; the same "
        "`LogoCache` local-first probe will pick the new file up on "
        "the next build.\n\n"
        "If this ticker is intentionally placeholder-only (the "
        "courage fallback is acceptable, no brand to source), close "
        "this issue without action -- the notifier will not re-file it."
    )


def _invalid_override_body(ticker: str, value: str) -> str:
    """Markdown body for an invalid-override issue.

    Includes the bad value verbatim so the maintainer can see the
    typo at a glance without diffing the TOML file in another tab.
    The canonical sector list is again sorted for body stability.
    """
    sector_list = "\n".join(f"- `{s}`" for s in sorted(KNOWN_SECTORS))
    return (
        f"`sector_overrides.toml` pins `{ticker}` to "
        f"`{value!r}`, which is not one of the canonical "
        "GICS-style sectors the treemap palette recognises. The "
        "entry was dropped at load time and the ticker fell back "
        "to the empty-sector path (and, if yfinance also has no "
        "value, the neutral `Other` tile).\n\n"
        "**To resolve:** edit "
        "[`sector_overrides.toml`](../blob/main/sector_overrides.toml) "
        "to use one of:\n\n"
        f"{sector_list}\n"
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def notify_github(hints: MaintenanceHints) -> NotifierOutcome:
    """Open / dedupe GitHub issues for every hint in ``hints``.

    The function is a strict no-op when any of the runtime
    prerequisites is missing (see :func:`_read_context` for the
    gating logic), so the caller can invoke it unconditionally
    without polluting non-CI environments. Every API call is
    individually defensive: a network blip or a 5xx from GitHub
    logs a warning and moves on rather than aborting the build,
    and the dedup pass on the next invocation keeps duplicate
    issues from accumulating.

    Categories handled:

    * ``missing_sector`` -- per-ticker dedupe via
      ``(maintenance, sector, ticker:<T>)`` label intersection.
    * ``missing_logos`` -- per-ticker dedupe via
      ``(maintenance, logo, ticker:<T>)`` label intersection.
    * ``invalid_overrides`` -- per-``(ticker, value)`` dedupe via
      label intersection narrowed by exact title match, so a fresh
      typo on the same ticker re-opens a conversation while a
      repeat of the same typo does not.

    Returns a :class:`NotifierOutcome` summarising the outcome (opt-in
    gate, counts of opened / already-tracked / failed per category).
    The caller (typically :func:`investing.cli.build_page`) renders a
    one-line status into the curated build summary so the operator
    sees, in the public job log, whether the notifier actually filed
    issues or hit a permissions / configuration wall.
    """
    if hints.is_empty:
        # No hints means nothing to notify about; outcome is the
        # default "disabled" so the caller can suppress the status
        # line entirely on clean builds.
        return NotifierOutcome()
    ctx = _read_context()
    if ctx is None:
        return NotifierOutcome(enabled=False)
    session = _build_session(ctx.token)
    api_root = _api_root(ctx)

    opened: list[str] = []
    already_tracked: list[str] = []
    failed: list[str] = []

    def _dispatch(
        *,
        ticker_label: str,
        title: str,
        body: str,
        labels: list[str],
        lookup_title: str | None = None,
    ) -> None:
        # Per-ticker dedupe + create, with the three lookup
        # outcomes routed to the right counter. ``ticker_label``
        # is what the operator reads on the status line; for the
        # invalid-override category that's "ticker=value" so the
        # value-changed dedupe key shows up in the summary too.
        outcome = _issue_exists(
            session, api_root, labels=labels, title=lookup_title,
        )
        if outcome == _LOOKUP_FAILED:
            failed.append(ticker_label)
            return
        if outcome == _LOOKUP_FOUND:
            already_tracked.append(ticker_label)
            return
        if _create_issue(
            session, api_root, title=title, body=body, labels=labels,
        ):
            opened.append(ticker_label)
        else:
            failed.append(ticker_label)

    for ticker in hints.missing_sector:
        _dispatch(
            ticker_label=ticker,
            title=f"Missing sector for {ticker}",
            body=_missing_sector_body(ticker),
            labels=[_LABEL_ROOT, _LABEL_SECTOR, _ticker_label(ticker)],
        )

    for ticker in hints.missing_logos:
        _dispatch(
            ticker_label=ticker,
            title=f"Missing logo for {ticker}",
            body=_missing_logo_body(ticker),
            labels=[_LABEL_ROOT, _LABEL_LOGO, _ticker_label(ticker)],
        )

    for ticker, value in sorted(hints.invalid_overrides.items()):
        title = f"Invalid sector override {value!r} for {ticker}"
        _dispatch(
            ticker_label=f"{ticker}={value!r}",
            title=title,
            body=_invalid_override_body(ticker, value),
            labels=[_LABEL_ROOT, _LABEL_INVALID_OVERRIDE, _ticker_label(ticker)],
            lookup_title=title,
        )

    return NotifierOutcome(
        enabled=True,
        opened=opened,
        already_tracked=already_tracked,
        failed=failed,
    )
