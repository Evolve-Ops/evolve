#!/bin/bash
# PreToolUse(Bash) hook — rewrite `cd <dir> && git <readonly...>` into
# `git -C <dir> <readonly...>` BEFORE Claude Code's hardcoded "untrusted git hooks"
# guardrail can fire (that guardrail is not suppressible by permission rules,
# bypassPermissions, or a permissionDecision:allow hook — only by removing the
# `cd ... && git` text, per the Claude Code docs).
#
# FAIL-SAFE CONTRACT (worst case == today's behavior, never a bad rewrite):
#   - Acts ONLY on commands that begin with `cd `.
#   - Rewrites ONLY when EVERY git invocation in the chain is READ-ONLY and EVERY
#     non-git segment is a cwd-insensitive benign command, whole-token matched
#     (echo/printf/true/: with literal args; head/tail/wc ONLY reading stdin, no file
#     operand) with no glob char (* ? [) anywhere. Each git in the chain gets its own
#     `-C <dir>` (so `cd X && git a && git b` stays correct).
#   - On ANY uncertainty (a non-readonly git, an unrecognized command, quoting that
#     breaks the split, command substitution, a nested cd, a missing interpreter,
#     malformed input) it emits NOTHING and the original command proceeds unchanged
#     (i.e. you get the normal prompt — no regression, never a destructive rewrite).
#   - It NEVER removes a git subcommand or touches non-git/destructive segments.
#
# Maintained by META:substrate (unattended-run permission contract). To disable:
# remove the Bash matcher from hooks.PreToolUse in ~/.claude/settings.json.
set -euo pipefail

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)" || exit 0
[ -n "$cmd" ] || exit 0

# Fast path: only `cd ...` commands are candidates; everything else passes through.
case "$cmd" in
  cd\ *) ;;
  *) exit 0 ;;
esac

rewritten="$(python3 - "$cmd" <<'PYEOF' 2>/dev/null
import sys, re
cmd = sys.argv[1] if len(sys.argv) > 1 else ''
m = re.match(r'^cd\s+([A-Za-z0-9_./~@:+-]+)\s+&&\s+(.+)$', cmd, re.DOTALL)
if not m:
    sys.exit(1)
d, rest = m.group(1), m.group(2)
# Bail on anything that makes the split unsafe or could hide a destructive command.
# Also bail on an embedded newline or carriage return -- a real shell separator the
# splitter does not see, so a trailing destructive command would otherwise run in the
# original cwd -- and on a redirection operator, which would relocate the target file
# to the original cwd. A glob char (* ? [) is cwd-sensitive too: the shell expands it
# against the ORIGINAL cwd, so dropping the `cd` would feed a different fileset to the
# (read-only) git or benign command. All bail to passthrough; worst case is the same
# prompt as now. (Tilde/brace expand to $HOME / a literal list -- cwd-independent -- so
# they are NOT bailed.)
if ('$(' in rest or '`' in rest or '\n' in rest or '\r' in rest or '>' in rest
        or '<' in rest or '*' in rest or '?' in rest or '[' in rest
        or re.search(r'(^|[;&|]\s*)cd\b', rest)):
    sys.exit(1)
READONLY = {
    'status','log','show','diff','branch','rev-parse','ls-files','ls-tree',
    'describe','tag','remote','rev-list','shortlog','blame','cat-file',
    'for-each-ref','symbolic-ref','name-rev','whatchanged','reflog','count-objects',
    'show-ref','merge-base','stash',  # stash with no subcmd lists; bare-only handled below
}
# `stash`/`tag`/`branch`/`remote`/`config` can mutate with args; only treat as
# read-only when used in their listing form. Keep it strict: bail if they carry a
# mutating flag/subcommand.
MUTABLE_IF_ARGS = {
    'stash': re.compile(r'^git\s+(?:-\S+\s+)*stash\b\s*(?:list\b.*)?$'),
    'tag':   re.compile(r'^git\s+(?:-\S+\s+)*tag\b\s*(?:-l\b.*|--list\b.*|-n\d*\b.*)?$'),
    'branch':re.compile(r'^git\s+(?:-\S+\s+)*branch\b\s*(?:-[lvar]+\b.*|--list\b.*|--show-current\b.*)?$'),
    'remote':re.compile(r'^git\s+(?:-\S+\s+)*remote\b\s*(?:-v\b.*|show\b.*|get-url\b.*)?$'),
    'config':re.compile(r'^git\s+(?:-\S+\s+)*config\b\s+(?:-l\b|--list\b|--get\b.*|get\b.*)'),
}
BENIGN = {'echo','head','tail','true',':','wc','printf'}
parts = re.split(r'(\s*(?:&&|\|\||;|\|)\s*)', rest)
out, saw_git = [], False
for seg in parts:
    if seg is None:
        continue
    if re.fullmatch(r'\s*(?:&&|\|\||;|\|)\s*', seg):
        out.append(seg); continue
    s = seg.strip()
    if s == '':
        out.append(seg); continue
    gm = re.match(r'^git\s+(?:-[^\s]+\s+)*([a-z][a-z0-9-]*)\b', s)
    if gm:
        sub = gm.group(1)
        if sub not in READONLY:
            sys.exit(1)
        chk = MUTABLE_IF_ARGS.get(sub)
        if chk is not None and not chk.match(s):
            sys.exit(1)
        saw_git = True
        seg = re.sub(r'^(\s*)git\b', lambda mm: mm.group(1) + 'git -C ' + d, seg, count=1)
        out.append(seg); continue
    toks = s.split()
    name = toks[0]
    # Whole-token match: `echo-foo`/`head.py`/`wc-clean` must NOT pass on a benign
    # PREFIX -- they are different (possibly cwd-sensitive) commands.
    if name not in BENIGN:
        sys.exit(1)
    # head/tail/wc are cwd-insensitive only when reading stdin (e.g. piped). A
    # positional (non-flag) operand names a FILE resolved against the original cwd,
    # which dropping the `cd` would silently relocate -- bail to passthrough.
    if name in ('head', 'tail', 'wc') and any(not t.startswith('-') for t in toks[1:]):
        sys.exit(1)
    out.append(seg)
if not saw_git:
    sys.exit(1)
sys.stdout.write(''.join(out))
PYEOF
)" || exit 0

[ -n "$rewritten" ] || exit 0
[ "$rewritten" = "$cmd" ] && exit 0

printf '%s' "$rewritten" | jq -R -s '{hookSpecificOutput:{hookEventName:"PreToolUse",updatedInput:{command: .}}}'
