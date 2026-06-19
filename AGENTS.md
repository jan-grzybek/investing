# AI agent instructions

This repository is a **personal project** maintained solely by Jan Grzybek for his own investment portfolio website. The repo is public on GitHub but is **not** intended for forks, pull requests, or use by other people.

When working here, do **not**:

- Add onboarding docs, contributor guides, Makefile shortcuts, or other tooling aimed at hypothetical external developers
- Assume the repo will be forked or that anyone else will contribute
- Add fork checklists, generic README development sections, or open-source boilerplate for strangers
- Optimize for outsiders discovering the project on GitHub

Treat all changes as for a single maintainer who already knows the repo. Prefer minimal, direct changes over ecosystem polish for external audiences.

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
- Do not add `--no-redact` escape hatches for CI; reproduce failures locally with `python -m investing`.

Internal workflow notes: [CONTRIBUTING.md](CONTRIBUTING.md). Security policy: [SECURITY.md](SECURITY.md).
