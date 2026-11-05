#!/usr/bin/env bash
# Commit files a build job produced and push them to main with the
# BOT_PUSH_KEY deploy key.
#
#   bot_push.sh SRC PATH MESSAGE [PATH MESSAGE ...]
#
# SRC is an artifact uploaded by a job that ran the third-party
# dependency set, so it is untrusted. Each PATH that SRC contains
# replaces its counterpart in the checkout wholesale and is committed
# with its MESSAGE; every commit then goes out in one push. Before
# anything is applied, every entry in SRC must be a plain file or
# directory, and every file must sit at or under one of the PATHs.
# Anything else fails the run before the key is read.
#
# This only isolates the key if it runs in a job of its own that
# never executes the build's dependencies. Steps of one job share
# the runner, the checkout, $GITHUB_PATH and any process left
# running, so a compromised dependency earlier in the same job could
# plant a git hook or shadow git/ssh and take the key.
set -euo pipefail

if (( $# < 3 || $# % 2 == 0 )); then
  echo "usage: bot_push.sh SRC PATH MESSAGE [PATH MESSAGE ...]" >&2
  exit 2
fi
src=$1
shift
paths=()
messages=()
while (( $# )); do
  paths+=("$1")
  messages+=("$2")
  shift 2
done

allowed() {
  local rel=$1 p
  for p in "${paths[@]}"; do
    [[ "$rel" == "$p" || "$rel" == "$p/"* ]] && return 0
  done
  return 1
}

while IFS= read -r -d '' entry; do
  rel=${entry#"$src"/}
  if [[ -L "$entry" ]] || { [[ ! -f "$entry" ]] && [[ ! -d "$entry" ]]; }; then
    echo "::error::bot changes contain a non-regular entry: $rel" >&2
    exit 1
  fi
  if [[ -f "$entry" ]] && ! allowed "$rel"; then
    echo "::error::bot changes contain an unexpected path: $rel" >&2
    exit 1
  fi
done < <(find "$src" -mindepth 1 -print0)

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'

committed=0
for i in "${!paths[@]}"; do
  p=${paths[$i]}
  [[ -e "$src/$p" ]] || continue
  rm -rf -- "$p"
  mkdir -p -- "$(dirname -- "$p")"
  cp -R -- "$src/$p" "$p"
  git add -A -- "$p"
  if git diff --cached --quiet -- "$p"; then
    echo "$p unchanged; nothing to commit."
    continue
  fi
  git commit -q -m "${messages[$i]}"
  committed=1
done

if (( ! committed )); then
  exit 0
fi

# Host key pinned from GitHub's published list, fetched over TLS. The
# job token only lifts the rate limit on that lookup.
key="$(mktemp)"
hosts="$(mktemp)"
trap 'rm -f "$key" "$hosts"' EXIT
printf '%s\n' "$BOT_PUSH_KEY" > "$key"
curl -fsS -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/meta \
  | jq -r '.ssh_keys[] | "github.com " + .' > "$hosts"
GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o UserKnownHostsFile=$hosts -o StrictHostKeyChecking=yes" \
  git push "git@github.com:${GITHUB_REPOSITORY}.git" HEAD:main
