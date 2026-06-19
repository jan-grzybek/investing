#!/usr/bin/env bash
# Run ruff, mypy, and the full pytest suite after each agent file edit.
# Wired to Cursor's ``afterFileEdit`` hook via ``.cursor/hooks.json``.

set -uo pipefail

input=$(cat)
file_path=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("file_path",""))')

# Only react to source edits the CI contract covers.
if [[ -z "$file_path" ]]; then
  echo '{}'
  exit 0
fi
case "$file_path" in
  *.py|assets/src/*|sector_overrides.toml) ;;
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

if [[ "$file_path" == *.py ]]; then
  echo "post-edit-verify: ruff on ${file_path}..." >&2
  if ! ruff check --fix "$file_path" && ! ruff check "$file_path"; then
    echo "post-edit-verify: ruff check FAILED on ${file_path}" >&2
    failures=1
  fi
  if ! ruff format "$file_path"; then
    echo "post-edit-verify: ruff format FAILED on ${file_path}" >&2
    failures=1
  fi
fi

if [[ "$file_path" == assets/src/* ]]; then
  echo "post-edit-verify: rebuilding minified assets..." >&2
  if ! "$PYTHON" scripts/build_assets.py; then
    echo "post-edit-verify: build_assets FAILED" >&2
    failures=1
  fi
fi

echo "post-edit-verify: mypy..." >&2
if ! mypy investing scripts/preview.py; then
  echo "post-edit-verify: mypy FAILED" >&2
  failures=1
fi

echo "post-edit-verify: pytest..." >&2
if ! "$PYTHON" -m pytest -q --tb=line; then
  echo "post-edit-verify: pytest FAILED" >&2
  failures=1
fi

if (( failures )); then
  echo "post-edit-verify: finished with failures" >&2
  echo '{"additional_context":"post-edit-verify: ruff/mypy/pytest reported failures — see Hooks output channel."}'
  exit 0
fi

echo "post-edit-verify: all checks passed" >&2
echo '{}'
exit 0
