#!/usr/bin/env bash
# CI-parity verification after each agent file edit (ruff, mypy, asset
# drift, full pytest with coverage). Wired to Cursor's ``afterFileEdit``
# hook via ``.cursor/hooks.json``.

set -uo pipefail

input=$(cat)
file_path=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("file_path",""))')

# Only react to source edits the CI contract covers.
if [[ -z "$file_path" ]]; then
  echo '{}'
  exit 0
fi
case "$file_path" in
  *.py|assets/src/*|assets/page.css|assets/*.js|sector_overrides.toml) ;;
  *) echo '{}'; exit 0 ;;
esac

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
  echo "post-edit-verify: ${label}..." >&2
  if ! "$@"; then
    echo "post-edit-verify: ${label} FAILED" >&2
    failures=1
  fi
}

if [[ "$file_path" == *.py ]]; then
  run_step "ruff check ${file_path}" ruff check --fix "$file_path"
  run_step "ruff check ${file_path} (post-fix)" ruff check "$file_path"
  run_step "ruff format ${file_path}" ruff format "$file_path"
fi

if [[ "$file_path" == assets/src/* ]]; then
  run_step "build_assets sync" "$PYTHON" scripts/build_assets.py
fi

run_step "ruff check" ruff check .
run_step "ruff format --check" ruff format --check .
run_step "mypy" mypy investing scripts/preview.py
run_step "build_assets --check" "$PYTHON" scripts/build_assets.py --check
run_step "pytest" "$PYTHON" -m pytest -q --tb=line --cov=investing --cov-report=term-missing:skip-covered

if (( failures )); then
  echo "post-edit-verify: finished with failures" >&2
  echo '{"additional_context":"post-edit-verify: CI-parity checks failed — see Hooks output channel."}'
  exit 0
fi

echo "post-edit-verify: all checks passed" >&2
echo '{}'
exit 0
