#!/bin/bash
# PreToolUse(Bash) hook — BLOCK a high-confidence MUTATING git/shell operation that
# targets a git checkout OTHER THAN this session's own worktree. It is the deterministic
# COMPLEMENT to tools/hooks/rewrite-cd-git.sh: that hook clears read-only `cd && git`
# for unattended runs; THIS hook stops the dangerous inverse — a chip/sibling/reconcile
# session reaching OUT of its worktree to mutate the ONE shared dev checkout (branch
# switch / reset / commit / stash pop / clean → silent WIP loss for a concurrent
# session). The defense used to be prose ("cd to your worktree") that drifts; this binds
# it in code (≥6 documented incidents: sibling-chip wipe, dispatch-from-clean-base,
# worktree-cd-failure-runs-in-main, git-stash-shared-across-worktrees, …).
#
# "OWN worktree" is GENERIC, never a hardcoded path: it is derived from the PreToolUse
# payload's `cwd` (the git toplevel containing it). A command that `cd`s to — or
# `git -C`s — a DIFFERENT checkout toplevel and then runs a mutating verb is the block
# target. This works identically in any repo (CalGraph included); nothing Evolve-specific.
#
# FAIL-SAFE CONTRACT (worst case == today's behavior — NO block — NEVER a false block of
# a chip working in its own tree):
#   - BLOCK only when BOTH are positively identified: (a) a mutating verb, AND (b) a
#     target checkout TOPLEVEL that differs from this session's own worktree toplevel
#     (both resolved through the SAME `git rev-parse --show-toplevel`, so the compare is
#     canonical/apples-to-apples and symlink-safe).
#   - Mutating GIT verbs guarded: checkout/switch, reset, commit, merge, rebase,
#     cherry-pick/revert/am, clean, restore, mv, rm, apply; stash (except list/show);
#     branch (only delete/move/force flags or a create form). Read-only git (log/status/
#     diff/show/branch --list/…) is NOT guarded — rewrite-cd-git.sh already covers it.
#   - Mutating SHELL ops guarded ONLY under a leading `cd <foreign>` AND only when every
#     path operand is RELATIVE (so it provably resolves under the foreign checkout):
#     rm/mv/cp/install/truncate/tee/dd/rmdir/ln/touch/`sed -i`, and `>`/`>>` redirects to
#     a relative target. An ABSOLUTE operand/target (could point anywhere) → passthrough.
#   - On ANY uncertainty — cwd/own-toplevel unresolvable, target toplevel unresolvable
#     (junk path, non-repo, `cd /nonexistent`), no explicit foreign path, an ambiguous/
#     read-only verb, a path token carrying $()/backtick/glob/quote, malformed JSON, or a
#     missing jq/python3/git — it emits NOTHING (passthrough, exit 0) and the command
#     proceeds to the normal permission flow. It NEVER blocks an op confined to the
#     session's own worktree.
#   - KNOWN LIMITATION (by design, fail-safe): a BARE mutation (`git reset`, no `cd`/`-C`)
#     that runs foreign only because a PRIOR, separate Bash call left the persisted cwd in
#     another checkout is NOT caught — the payload cwd then already reads as that checkout,
#     so own==target and nothing is foreign. This hook binds the EXPLICIT in-command
#     reach-out (`cd <foreign> && …`, `git -C <foreign> …`), which is the documented
#     incident shape.
#
# Maintained by META:substrate. Deps: jq + git + python3 (same minimal set as the sibling
# hooks). To disable: remove the Bash matcher pointing here from hooks.PreToolUse in
# ~/.claude/settings.json.
set -euo pipefail

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)" || exit 0
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)" || exit 0
[ -n "$cmd" ] || exit 0
[ -n "$cwd" ] || exit 0

reason="$(python3 - "$cmd" "$cwd" <<'PYEOF' 2>/dev/null
import sys, os, re, shlex, shutil, subprocess

cmd = sys.argv[1] if len(sys.argv) > 1 else ''
cwd = sys.argv[2] if len(sys.argv) > 2 else ''
if not cmd or not cwd or not shutil.which('git'):
    sys.exit(0)

# --- toplevel resolution (the ONLY notion of checkout identity; symlink-canonical) -------
_top_cache = {}
def toplevel(d):
    if not d:
        return None
    if d in _top_cache:
        return _top_cache[d]
    try:
        r = subprocess.run(['git', '-C', d, 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True, timeout=5)
        t = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        t = None
    t = t or None
    _top_cache[d] = t
    return t

# The session's own worktree. If we can't resolve it, we cannot compare -> passthrough.
own = toplevel(cwd)
if not own:
    sys.exit(0)

_UNSAFE = ('$', '`', '*', '?', '[', '"', "'")
def resolve(tok):
    """Resolve a LITERAL path token to an absolute path, or None if not literal-safe."""
    if tok is None or tok == '':
        return None
    if any(c in tok for c in _UNSAFE):
        return None
    p = os.path.expanduser(tok)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return p

# --- tokenize (bail on unbalanced quotes) + split into shell segments --------------------
try:
    toks = shlex.split(cmd, posix=True)
except ValueError:
    sys.exit(0)
if not toks:
    sys.exit(0)

_SEP = {'&&', '||', ';', '&', '|'}
segments, cur = [], []
for t in toks:
    if t in _SEP:
        segments.append(cur); cur = []
    else:
        cur.append(t)
segments.append(cur)
segments = [s for s in segments if s]
if not segments:
    sys.exit(0)

# --- mutating-verb classification (git) --------------------------------------------------
MUT_ALWAYS = {'checkout', 'switch', 'reset', 'commit', 'merge', 'rebase',
              'cherry-pick', 'revert', 'am', 'clean', 'restore', 'mv', 'rm', 'apply'}
_VALUE_OPTS = {'-C', '-c', '--git-dir', '--work-tree', '--namespace',
               '--exec-path', '--config-env', '--super-prefix'}
_BRANCH_MUT_FLAGS = {'-d', '-D', '--delete', '-m', '-M', '--move', '--copy',
                     '-f', '--force'}

def git_parts(git_tokens):
    """Return (dash_C_dir_or_None, subcommand_or_None, sub_args, dash_C_count) for the
    tokens after 'git'. dash_C_count is the number of top-level `-C` options seen: git
    CHAINS them (`git -C a -C b` = chdir a, then `-C b` relative to a), so a single value
    resolved against cwd is only correct when the count is exactly 1. Callers bail (treat
    the target as unresolvable → passthrough) on a chained `-C` rather than mis-resolve it."""
    dashC, sub, sub_args, dashC_count = None, None, [], 0
    i, n = 0, len(git_tokens)
    while i < n:
        t = git_tokens[i]
        if sub is None:
            if t.startswith('-'):
                if t == '-C' and i + 1 < n:
                    dashC = git_tokens[i + 1]; dashC_count += 1; i += 2; continue
                if t.startswith('--git-dir=') or t.startswith('--work-tree='):
                    i += 1; continue
                if '=' in t:
                    i += 1; continue
                if t in _VALUE_OPTS and i + 1 < n:
                    i += 2; continue
                i += 1; continue
            sub = t; i += 1; continue
        sub_args.append(t); i += 1
    return dashC, sub, sub_args, dashC_count

def git_is_mutating(sub, sub_args):
    if sub is None:
        return False
    if sub in MUT_ALWAYS:
        return True
    if sub == 'stash':
        first = next((a for a in sub_args if not a.startswith('-')), None)
        return first not in ('list', 'show')   # bare/push/pop/apply/drop/clear -> mutating
    if sub == 'branch':
        flags = [a for a in sub_args if a.startswith('-')]
        pos = [a for a in sub_args if not a.startswith('-')]
        if any(f in _BRANCH_MUT_FLAGS for f in flags):
            return True
        return bool(pos)   # create/update form
    return False

# --- mutating-verb classification (shell, relative-operand-only) -------------------------
SHELL_MUT = {'rm', 'mv', 'cp', 'install', 'truncate', 'tee', 'dd', 'rmdir', 'ln', 'touch'}
_ABS_LIKE = ('/', '~', '$')
def _is_relative(tok):
    return not (tok.startswith(_ABS_LIKE) or any(c in tok for c in ('*', '?', '[', '`')))

def relative_redirect(seg):
    """True iff seg writes via `>`/`>>`/`2>`/`&>` to a RELATIVE target."""
    for i, t in enumerate(seg):
        if t in ('>', '>>', '2>', '&>', '>|'):
            if i + 1 < len(seg) and _is_relative(seg[i + 1]):
                return True
        elif t.startswith('>') and len(t) > 1:
            if _is_relative(t.lstrip('>')):
                return True
    return False

def shell_mutates_relative(seg):
    """True iff seg is a mutating shell command whose operands are ALL relative."""
    if not seg:
        return False
    c0 = seg[0]
    is_mut = c0 in SHELL_MUT or (c0 == 'sed' and any(a.startswith('-i') for a in seg[1:]))
    if is_mut:
        operands = [a for a in seg[1:] if not a.startswith('-')]
        if operands and all(_is_relative(a) for a in operands):
            return True
    return relative_redirect(seg)

# --- Detector A: `git -C <foreign> <mutating>` (explicit target, cd-independent) ----------
for seg in segments:
    if seg[0] != 'git':
        continue
    dashC, sub, sub_args, dashC_count = git_parts(seg[1:])
    if not dashC or dashC_count != 1:
        continue   # no -C, or a chained -C not resolvable against cwd alone -> passthrough
    tgt = resolve(dashC)
    top = toplevel(tgt) if tgt else None
    if top and top != own and git_is_mutating(sub, sub_args):
        print("Blocked cross-checkout mutation: `git -C %s %s ...` mutates a DIFFERENT "
              "git checkout (%s) than this session's own worktree (%s). That can switch "
              "its branch or destroy a concurrent session's uncommitted work. Run this "
              "from a session whose cwd IS that checkout instead. "
              "(guard: tools/hooks/guard-cross-checkout-mutation.sh; passthrough on any "
              "uncertainty.)" % (dashC, sub, top, own))
        sys.exit(0)

# --- Detector B: leading `cd <foreign>` then a mutating op (git-without-C or shell) -------
cd_dir = None
if len(segments[0]) == 2 and segments[0][0] == 'cd':
    cd_dir = segments[0][1]
if cd_dir:
    cd_tgt = resolve(cd_dir)
    cd_top = toplevel(cd_tgt) if cd_tgt else None
    if cd_top and cd_top != own:
        for seg in segments[1:]:
            verb = None
            if seg[0] == 'git':
                dashC, sub, sub_args, dashC_count = git_parts(seg[1:])
                if dashC_count >= 1:
                    continue   # any -C: its own target decides; Detector A owns the single-C
                               # case, and a chained -C is a passthrough bail
                if git_is_mutating(sub, sub_args):
                    verb = 'git %s' % sub
            elif shell_mutates_relative(seg):
                verb = seg[0]
            if verb:
                print("Blocked cross-checkout mutation: `cd %s && ... %s ...` runs a "
                      "mutating operation inside a DIFFERENT git checkout (%s) than this "
                      "session's own worktree (%s). That can switch its branch or destroy "
                      "a concurrent session's uncommitted work. Work from a session whose "
                      "cwd IS that checkout instead. "
                      "(guard: tools/hooks/guard-cross-checkout-mutation.sh; passthrough "
                      "on any uncertainty.)" % (cd_dir, verb, cd_top, own))
                sys.exit(0)

# Nothing positively identified as a foreign mutation -> passthrough.
sys.exit(0)
PYEOF
)" || exit 0

[ -n "$reason" ] || exit 0

printf '%s' "$reason" | jq -R -s '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:.}}'
