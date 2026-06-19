#!/usr/bin/env bash
# Full CI-parity verification at the end of each completed agent session:
# ruff, mypy, pytest, asset drift check, and a fresh local preview.
# Wired to Cursor's ``stop`` hook via ``.cursor/hooks.json``.

set -uo pipefail

input=$(cat)
status=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))')

if [[ "$status" != "completed" ]]; then
  echo "post-agent-verify: skipped (status=${status:-unknown})" >&2
  echo '{}'
  exit 0
fi

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [[ -f .testvenv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .testvenv/bin/activate
fi

PYTHON="${PYTHON:-python}"
failures=0

run_step() {
  local label=$1
  shift
  echo "post-agent-verify: ${label}..." >&2
  if ! "$@"; then
    echo "post-agent-verify: ${label} FAILED" >&2
    failures=1
  fi
}

run_step "ruff check" ruff check .
run_step "ruff format --check" ruff format --check .
run_step "mypy" mypy investing scripts/preview.py
run_step "build_assets --check" "$PYTHON" scripts/build_assets.py --check
run_step "pytest" "$PYTHON" -m pytest -q --tb=line --cov=investing --cov-report=term-missing:skip-covered
run_step "build_assets sync" "$PYTHON" scripts/build_assets.py
run_step "preview" "$PYTHON" scripts/preview.py --out preview/

if (( failures )); then
  echo "post-agent-verify: finished with failures (see output above)" >&2
else
  echo "post-agent-verify: all checks passed; preview at ${ROOT}/preview/index.html" >&2
fi

echo '{}'
exit 0
