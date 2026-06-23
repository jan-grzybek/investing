# AI agent instructions

This repository is a **personal project** maintained solely by Jan Grzybek for his own investment portfolio website. The repo is public on GitHub but is **not** intended for forks, pull requests, or use by other people.

When working here, do **not**:

- Add onboarding docs, contributor guides, Makefile shortcuts, or other tooling aimed at hypothetical external developers
- Assume the repo will be forked or that anyone else will contribute
- Add fork checklists, generic README development sections, or open-source boilerplate for strangers
- Optimize for outsiders discovering the project on GitHub

Treat all changes as for a single maintainer who already knows the repo. Prefer minimal, direct changes over ecosystem polish for external audiences.

## Python environment

Dev dependencies live in **`.venv/`** (`pip install -e '.[dev]'`). The venv is gitignored; bare `python` / `pytest` / `ruff` on PATH use system Python and will hit `ModuleNotFoundError`.

**Shell commands from the agent do not auto-activate the venv** — only the IDE's integrated terminal does (via `.vscode/settings.json`). Always prefix with `.venv/bin/`:

- `.venv/bin/python -m pytest …`
- `.venv/bin/python -m investing`
- `.venv/bin/ruff check .` / `.venv/bin/mypy investing scripts/preview.py`

If `.venv/` is missing: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. Do not install project deps into system Python.

## CI parity hooks

Cursor runs [`.cursor/hooks/post-edit-verify.sh`](.cursor/hooks/post-edit-verify.sh) after each agent file edit and [`.cursor/hooks/post-agent-verify.sh`](.cursor/hooks/post-agent-verify.sh) when a session completes. Both mirror the **Tests** workflow (`ruff`, `mypy`, `scripts/build_assets.py --check`, full `pytest` with coverage). If a hook reports failures, fix them before pushing — the same gates run on GitHub (`test.yml`).

## Secrets and private data

Pay **special attention** to not leaking secrets or private portfolio data — especially **raw spreadsheet source data**. The public site shows derived percentages only; nominal values from the Google Sheet must never appear in the repo, logs, tests, or agent output.

The build runs in a **public** repository, so **GitHub Actions job logs are world-readable** and act as a side channel for both secrets and nominal sheet values. See [SECURITY.md](SECURITY.md) for the full threat model, mitigations in [`investing/safe_run.py`](investing/safe_run.py), and third-party script policy.

**Credentials and identifiers** — never commit, echo in logs, paste into comments, or print in error messages:

- `GSHEET_CREDS` (service-account JSON)
- `GSHEET_ID` (spreadsheet identifier)
- Any other tokens, API keys, or workflow secrets

**Spreadsheet source data** — treat as confidential even when credentials are not involved:

- Share counts, cash balances, per-trade prices and sizes
- Dividend payouts, FX rates, and other nominal amounts used to derive published percentages
- Raw sheet rows, cell values, or dumps of ingested data

**Safe practices:**

- Use [`scripts/preview.py`](scripts/preview.py) with **synthetic** fixtures for local HTML/visual checks — not the live sheet.
- Keep test fixtures fictional; do not copy real holdings or trades into committed files.
- Avoid `print()` / logging that could surface runtime values from the pipeline; follow existing redaction patterns in [`investing/safe_run.py`](investing/safe_run.py).
- Before committing, scan diffs for sheet IDs, credential-shaped JSON, and numeric portfolio data that looks like production values.
- Do not suggest committing `preview/`, CI smoke output, or debug artifacts that were built from real data.

**When touching the build pipeline** (`investing/safe_run.py`, workflow files, or code around `main()`):

- Use [`investing.log`](investing/log.py) for diagnostics; route the curated build signal through `safe_run.emit_summary` only.
- Do not re-raise with runtime values in the message (e.g. `raise ValueError(f"bad row: {row!r}")`). Follow the `SheetParseError` pattern in [`investing/sheets.py`](investing/sheets.py) — coordinates only, never cell contents.
- Do not add `--no-redact` escape hatches for CI; reproduce failures locally with `.venv/bin/python -m investing`.

Internal workflow notes: [CONTRIBUTING.md](CONTRIBUTING.md). Security policy: [SECURITY.md](SECURITY.md).
