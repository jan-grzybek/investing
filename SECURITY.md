# Security Policy

## Threat model

This repository builds a public web page from a *private* data source:

1. **Secrets** held by the production GitHub Actions workflow
   (`.github/workflows/main.yml`) -- the Google service-account JSON
   passed in as `GSHEET_CREDS` and the sheet identifier
   `GSHEET_ID`. Both come from repository secrets and are scoped to
   the deploy job.
2. **Nominal portfolio values** read from that Google Sheet -- share
   counts, per-trade prices, cash balances, dividend payouts, FX
   rates. None of these are intended to be published. The page only
   ever exposes *derived percentages* (weights, returns, allocations).

Because the build runs in a public repository, **job logs are
world-readable** and act as a side channel for both classes of data.

## Mitigations in place

* `investing.safe_run._run_main_safely` wraps `main()` so that
  `sys.stderr` and the underlying file descriptor are both fully
  redirected to `/dev/null` for the duration of the build. This
  silences not only Python output but also any output from C
  extensions that bypass the Python `stderr` wrapper (NumPy / Pandas
  warnings, `gspread` HTTP error bodies, `yfinance` rate-limit
  notices, etc.) that could echo offending values back into the logs.
* On failure, the wrapper restores `stderr` and prints a
  *hand-formatted* traceback that is deliberately built from public
  identifiers only: exception class, per-frame `filename:lineno`,
  function name, source line. It drops `str(exc)`, exception
  `__notes__` and local variables -- the usual channels through
  which runtime values surface.
* All progress / diagnostic output in the pipeline goes through
  `investing.log.logger`; the package contains no `print()` calls in
  non-test code. Do not reintroduce them.
* `_gspread_client` accepts the service-account JSON inline via the
  `GSHEET_CREDS` environment variable so the secret never lands on
  the runner's filesystem.
* The deploy workflow uses the least-privilege token scope (`contents:
  read`, `pages: write`, `id-token: write`).

## Reporting a vulnerability

Please open a private security advisory via GitHub's "Security" tab
rather than a public issue or PR. If you believe a leak has already
occurred in published job logs or in the served page, include the
specific URL and the line range so we can rotate the affected secrets
and rewrite history if needed.

## When changing the build pipeline

If you touch `investing/safe_run.py`, the workflow files, or anything
that runs around `main()`, please verify:

* No new `print()` calls (use `investing.log.logger`).
* No new redirections that swap stderr back to a tty path.
* No code paths that re-raise with the offending value embedded in
  the message (e.g. `raise ValueError(f"bad row: {row!r}")`). Use the
  `SheetParseError` pattern in `investing/sheets.py` instead -- it
  builds the message from row coordinates only.
* No `--no-redact` style escape hatch in CI; if a build fails and the
  sanitized summary isn't enough, reproduce locally with
  `python -m investing` instead.
