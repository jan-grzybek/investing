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
  **both** `sys.stderr` / fd 2 **and** `sys.stdout` / fd 1 are fully
  redirected for the duration of the build. The stderr path silences
  output from C extensions that bypass the Python wrappers (NumPy /
  Pandas warnings, `gspread` HTTP error bodies, `yfinance`
  rate-limit notices, etc.). The stdout path defends against a
  transitive dependency emitting nominal values via `print()` (debug
  modes in `httpx`, `tqdm`-style progress lines, etc.) -- those land
  in a discarded in-memory buffer rather than the public job log.
* The curated `Build OK: ...` summary line routes through
  `investing.safe_run.emit_summary`, which writes to the stashed
  real stdout while the redaction is active. The line is composed
  exclusively of quantities the rendered page also publishes (TWR /
  CAGR percentages, holding counts) so the job log gets a positive
  build signal without surfacing privacy-sensitive values.
* On failure, the wrapper restores `stderr` and prints a
  *hand-formatted* traceback that is deliberately built from public
  identifiers only: exception class, per-frame `filename:lineno`,
  function name, source line. It drops `str(exc)`, exception
  `__notes__` and local variables -- the usual channels through
  which runtime values surface.
* All progress / diagnostic output in the pipeline goes through
  `investing.log.logger`; the package contains no `print()` calls in
  non-test code (other than the curated summary above). Do not
  reintroduce them.
* `_gspread_client` accepts the service-account JSON inline via the
  `GSHEET_CREDS` environment variable so the secret never lands on
  the runner's filesystem.
* The deploy workflow uses the least-privilege token scope (`contents:
  read`, `pages: write`, `id-token: write`).
* `.github/workflows/security.yml` runs `pip-audit` against both
  lockfiles (OSV.dev advisory feed, `--strict` so known CVEs fail
  the build) and a CodeQL Python scan (`security-and-quality` query
  set) on every push, PR, and weekly cron sweep.

## Third-party scripts on the deployed page

The rendered page loads exactly one third-party asset: the Cloudflare
Web Analytics beacon
(`https://static.cloudflareinsights.com/beacon.min.js`). It is
allow-listed in the page's `script-src` Content-Security-Policy
directive (`investing/webpage/head.py`) and the matching `<script>`
tag is composed by `investing.webpage.head.build_analytics_tag`.
The embedded `data-cf-beacon` token is a write-only identifier
(grants push access to the analytics dashboard but reveals nothing
about the dataset on its own), so its presence in source is
intentional. Removing or swapping the provider means editing both
the CSP and the tag together; the matching `head.py` constants are
the single edit surface.

## Reporting a vulnerability

Please open a private security advisory via GitHub's "Security" tab
rather than a public issue or PR. If you believe a leak has already
occurred in published job logs or in the served page, include the
specific URL and the line range so we can rotate the affected secrets
and rewrite history if needed.

## When changing the build pipeline

If you touch `investing/safe_run.py`, the workflow files, or anything
that runs around `main()`, please verify:

* No new `print()` calls reaching `sys.stdout` directly (use
  `investing.log.logger` for diagnostics; route the curated build
  signal through `safe_run.emit_summary`). The leak-safe wrapper
  now redirects stdout too, but a future redaction that misses one
  of the streams would silently re-open the leak.
* No new redirections that swap stderr / stdout back to a tty path.
* No code paths that re-raise with the offending value embedded in
  the message (e.g. `raise ValueError(f"bad row: {row!r}")`). Use the
  `SheetParseError` pattern in `investing/sheets.py` instead -- it
  builds the message from row coordinates only.
* No `--no-redact` style escape hatch in CI; if a build fails and the
  sanitized summary isn't enough, reproduce locally with
  `python -m investing` instead.
