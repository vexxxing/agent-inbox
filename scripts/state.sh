#!/usr/bin/env bash
# Keep watcher state on its own branch, so state updates never touch your
# main branch and never wake the inbox pull request.
#
#   scripts/state.sh load   copy $STATE_FILE from the state branch to $STATE_DIR
#   scripts/state.sh save   commit $STATE_DIR/$STATE_FILE back if it changed
#
# Defaults: STATE_BRANCH=agent-inbox-state, STATE_FILE=watchers.json.
#
# The branch holds a single commit that is replaced on each save, so it never
# grows. Runs are serialised by the workflow's concurrency group.
set -euo pipefail

BRANCH="${STATE_BRANCH:-agent-inbox-state}"
DIR="${STATE_DIR:-.state}"
FILE="${STATE_FILE:-watchers.json}"
REMOTE="${STATE_REMOTE:-origin}"

case "${1:-}" in
  load)
    mkdir -p "$DIR"
    rm -f "$DIR/$FILE" "$DIR/.loaded"
    if git ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
      git fetch --quiet --depth=1 "$REMOTE" "refs/heads/$BRANCH"
      if git cat-file -e "FETCH_HEAD:$FILE" 2>/dev/null; then
        git show "FETCH_HEAD:$FILE" > "$DIR/$FILE"
        cp "$DIR/$FILE" "$DIR/.loaded"
        echo "loaded state from $BRANCH"
        exit 0
      fi
    fi
    echo "no saved state yet; starting fresh"
    ;;
  save)
    if [ ! -f "$DIR/$FILE" ]; then
      echo "nothing to save"
      exit 0
    fi
    if [ -f "$DIR/.loaded" ] && cmp -s "$DIR/$FILE" "$DIR/.loaded"; then
      echo "state unchanged"
      exit 0
    fi
    export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}"
    export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
    export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
    export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
    blob=$(git hash-object -w "$DIR/$FILE")
    tree=$(printf '100644 blob %s\t%s\n' "$blob" "$FILE" | git mktree)
    commit=$(git commit-tree "$tree" -m "Update watcher state")
    git push --quiet --force "$REMOTE" "$commit:refs/heads/$BRANCH"
    cp "$DIR/$FILE" "$DIR/.loaded"
    echo "saved state to $BRANCH"
    ;;
  *)
    echo "usage: $0 load|save" >&2
    exit 2
    ;;
esac
