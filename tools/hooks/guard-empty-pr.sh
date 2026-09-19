#!/bin/bash
# PreToolUse(Bash) hook — BLOCK `gh pr create` when the head branch has an EMPTY
# diff vs its base (no file changes). This is the "empty-scaffold merge" root
# cause (spec §17, Initiative 12 / bite B2): a chip opens a `--draft` PR from a
# branch with no real changes, and with no barrier the auto-lane merges an empty
# scaffold (#2846, #3122). A deterministic hook cannot be drifted away from by
# prose, so it is the enforcement complement to the merge-gate rules.
#
# FAIL-SAFE CONTRACT (worst case == today's behavior — the normal prompt/allow,
# NEVER a false block):
#   - BLOCKS only when it can POSITIVELY compute (a) a base ref that exists and is
#     non-empty AND (b) an empty `git diff <base>...HEAD` (git exited 0 = no
#     changes) in the session's own working directory.
#   - On ANY uncertainty it emits NOTHING and the command proceeds unchanged:
#     not a `gh pr create`; a leading/embedded `cd` (would change the cwd our git
#     runs in); `--repo`/`-R` (a different repo — local git state may not match);
#     `--head`/`-H` (an explicit head ref — HEAD is not the compared branch); a
#     cross-fork `--base owner:branch`; HEAD detached; the base branch resolves
#     nowhere (neither `origin/<base>` nor local `<base>`); a NON-empty diff (real
#     changes — the whole point is to never block these); or any git / parse /
#     jq / python error or timeout.
#   - It NEVER blocks a create that carries real changes, and never touches any
#     command that is not a `gh pr create`.
#
# The head is HEAD (the current branch), matching how `gh pr create` (no --head)
# picks the head; the base is `--base` if given, else the remote default branch
# (`origin/HEAD`). The diff is three-dot (`base...HEAD` = changes on HEAD since the
# merge-base) — exactly the fileset the PR would show.
#
# Maintained by META:substrate (fact-gate merges, spec §17). Regression guard:
# tools/hooks/test_guard_empty_pr.sh. To disable: remove this hook's Bash matcher
# from hooks.PreToolUse in ~/.claude/settings.json.
set -euo pipefail

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)" || exit 0
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)" || exit 0
[ -n "$cmd" ] || exit 0

# Cheap gate: only `gh … pr … create` commands are candidates. The python below
# does the precise `\bgh\s+pr\s+create\b` match; this just avoids paying python
# startup on every unrelated Bash command.
case "$cmd" in
  *gh*pr*create*) ;;
  *) exit 0 ;;
esac

out="$(python3 - "$cmd" "$cwd" <<'PYEOF' 2>/dev/null
import sys, re, os, json, shlex, subprocess

try:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    cwd = sys.argv[2] if len(sys.argv) > 2 else ''

    # Must be a real `gh pr create` invocation (contiguous subcommand).
    if not re.search(r'(?:^|[;&|(]\s*)gh\s+pr\s+create\b', cmd):
        sys.exit(0)
    # Need a real directory to run git in (the cwd `gh pr create` would run in).
    if not cwd or not os.path.isdir(cwd):
        sys.exit(0)
    # A `cd` anywhere would change the cwd the create runs in — our `git -C <cwd>`
    # would then read the wrong repo/branch. Bail to passthrough (fail-safe).
    if re.search(r'(?:^|[;&|]\s*)cd\b', cmd):
        sys.exit(0)

    # Isolate the `gh pr create` segment from any && / || / ; / | chain so we parse
    # only ITS flags, not a neighbour's.
    seg = None
    for s in re.split(r'(?:&&|\|\||;|\|)', cmd):
        if re.search(r'\bgh\s+pr\s+create\b', s):
            seg = s
            break
    if seg is None:
        sys.exit(0)
    try:
        toks = shlex.split(seg)
    except ValueError:
        sys.exit(0)
    try:
        ci = toks.index('create')
    except ValueError:
        sys.exit(0)
    args = toks[ci + 1:]

    base = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('--repo', '-R') or a.startswith('--repo=') or a.startswith('-R='):
            sys.exit(0)   # different repo — local git state may not correspond
        if a in ('--head', '-H') or a.startswith('--head=') or a.startswith('-H='):
            sys.exit(0)   # explicit head ref — HEAD is not the compared branch
        if a in ('--base', '-B'):
            base = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith('--base='):
            base = a.split('=', 1)[1]
            i += 1
            continue
        if a.startswith('-B='):
            base = a.split('=', 1)[1]
            i += 1
            continue
        i += 1

    def git(*a):
        try:
            return subprocess.run(['git', '-C', cwd, *a],
                                  capture_output=True, text=True, timeout=5)
        except Exception:
            return None

    # HEAD must be a real branch (a detached HEAD is not the head `gh` would push).
    r = git('symbolic-ref', '--quiet', 'HEAD')
    if r is None or r.returncode != 0:
        sys.exit(0)

    # Resolve the base branch NAME.
    if base is not None:
        if base == '' or ':' in base:   # empty or cross-fork owner:branch → bail
            sys.exit(0)
        base_name = base
    else:
        r = git('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')
        if r is None or r.returncode != 0 or not r.stdout.strip():
            sys.exit(0)   # can't positively resolve a default base → passthrough
        base_name = r.stdout.strip().rsplit('/', 1)[-1]

    # Resolve a base REF that actually exists (the PR compares vs the remote base,
    # so prefer origin/<base>; fall back to a local branch of that name).
    base_ref = None
    for cand in ('origin/' + base_name, base_name):
        rr = git('rev-parse', '--verify', '--quiet', cand + '^{commit}')
        if rr is not None and rr.returncode == 0 and rr.stdout.strip():
            base_ref = cand
            break
    if base_ref is None:
        sys.exit(0)   # base resolves nowhere → uncertainty → passthrough

    # Three-dot diff = the fileset the PR would carry. `--quiet` → exit 0 (NO
    # changes), 1 (changes), >1 (error). ONLY a clean exit-0 (positively empty)
    # blocks; a non-empty diff or any git error falls through to passthrough.
    rr = git('diff', '--quiet', base_ref + '...HEAD')
    if rr is None or rr.returncode != 0:
        sys.exit(0)

    reason = (
        "Empty-diff PR blocked (META:substrate B2): the head branch has NO file "
        "changes vs %s (git diff %s...HEAD is empty). `gh pr create` would open an "
        "empty scaffold that the auto-merge lane could merge unreviewed. Commit the "
        "real changes first, then create the PR. (Override intentionally: pass "
        "--head/--base to a ref this guard can't resolve, or open the PR from the "
        "GitHub UI.)" % (base_ref, base_ref)
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
except Exception:
    sys.exit(0)
PYEOF
)" || exit 0

[ -n "$out" ] || exit 0
printf '%s' "$out"
