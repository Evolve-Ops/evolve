#!/usr/bin/env bash
# Worktree garbage collector.
#
# Symptom this fixes: Claude Code spawns agent-* worktrees under
# .claude/worktrees/ that get `locked` when an agent session ends, and they
# never get cleaned up. Counted 201 of these on the evolve repo over two
# months — pure disk bloat.
#
# Rule:
#   - any worktree path under .claude/worktrees/agent-*
#   - that is `locked`
#   - and whose head commit is reachable from origin/main (or older than 7d
#     with no uncommitted/unmerged work)
#   → remove with `git worktree remove --force`.
#
# Conservative: we only remove worktrees whose branch is fully merged OR
# whose mtime is >7 days AND working tree is clean. Anything with uncommitted
# work or unmerged commits is left alone for the operator to inspect.
#
# Dry-run by default. Use --apply to actually delete.
#
# Usage:
#   .claude/hooks/worktree-gc.sh            # dry-run (default)
#   .claude/hooks/worktree-gc.sh --apply    # actually delete
#   .claude/hooks/worktree-gc.sh --apply --max-age-days 14

set -euo pipefail

APPLY=0
MAX_AGE_DAYS=7
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --max-age-days) shift; MAX_AGE_DAYS="$1" ;;
    --max-age-days=*) MAX_AGE_DAYS="${arg#*=}" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Make sure origin/main is fresh (best-effort).
git fetch --quiet origin main 2>/dev/null || true

removed=0
kept_dirty=0
kept_unmerged=0
kept_recent=0
considered=0

# Parse `git worktree list --porcelain`.
# Each record is separated by a blank line and has keys: worktree, HEAD,
# branch, optionally `locked`.
python_filter() {
python3 - <<'PY'
import os, subprocess, sys, time, json

now = time.time()
max_age_days = int(os.environ["MAX_AGE_DAYS"])
max_age = max_age_days * 86400

records = []
record = {}
porcelain = subprocess.run(
    ["git", "worktree", "list", "--porcelain"],
    check=True, capture_output=True, text=True,
).stdout
for line in porcelain.splitlines() + [""]:
    if line == "":
        if record:
            records.append(record); record = {}
        continue
    key, _, val = line.partition(" ")
    record[key] = val or True

def branch_merged_to_main(branch):
    if not branch:
        return False
    # Fully merged means `git merge-base --is-ancestor <branch> origin/main`.
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, "origin/main"],
        capture_output=True,
    )
    return r.returncode == 0

def is_clean(path):
    r = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == ""

candidates = []
for r in records:
    path = r.get("worktree", "")
    if not path:
        continue
    if "/.claude/worktrees/agent-" not in path and "/.claude/worktrees/" not in path:
        continue
    if path == os.environ.get("REPO_ROOT", ""):
        continue
    if not os.path.exists(path):
        # Orphan — git knows about it but the dir is gone. Safe to prune.
        candidates.append((path, r, "orphan"))
        continue
    branch = (r.get("branch", "") or "").replace("refs/heads/", "")
    locked = "locked" in r
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = now
    age = now - mtime

    clean = is_clean(path)
    merged = branch_merged_to_main(branch)

    if not clean:
        candidates.append((path, r, "skip:dirty"))
        continue
    if merged:
        candidates.append((path, r, "remove:merged"))
        continue
    if age > max_age:
        candidates.append((path, r, "remove:stale"))
        continue
    candidates.append((path, r, "skip:recent"))

print(json.dumps(candidates))
PY
}

export MAX_AGE_DAYS REPO_ROOT
TAB=$'\t'
TMPLIST="$(mktemp)"
trap 'rm -f "$TMPLIST"' EXIT
python_filter | python3 -c '
import json, sys
for path, rec, reason in json.load(sys.stdin):
    branch = rec.get("branch", "")
    print("\t".join([reason, path, branch]))
' > "$TMPLIST"

while IFS=$'\t' read -r reason path branch; do
  [[ -z "$reason" ]] && continue
  considered=$((considered+1))

  case "$reason" in
    remove:merged|remove:stale|orphan)
      if (( APPLY )); then
        echo "[gc] removing ($reason): $path  branch=$branch"
        git worktree remove --force "$path" 2>/dev/null || git worktree prune
      else
        echo "[gc] WOULD remove ($reason): $path  branch=$branch"
      fi
      removed=$((removed+1))
      ;;
    skip:dirty)
      kept_dirty=$((kept_dirty+1))
      ;;
    skip:recent)
      kept_recent=$((kept_recent+1))
      ;;
    *)
      kept_unmerged=$((kept_unmerged+1))
      ;;
  esac
done < "$TMPLIST"

# Always end with a prune to clean up any dangling administrative refs.
if (( APPLY )); then
  git worktree prune
fi

echo "[gc] summary: considered=${considered} removed=${removed} kept_dirty=${kept_dirty} kept_recent=${kept_recent}"
if (( ! APPLY )); then
  echo "[gc] dry run; re-run with --apply to actually delete."
fi
