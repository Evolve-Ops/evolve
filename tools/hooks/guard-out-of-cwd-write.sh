#!/bin/bash
# PreToolUse(Write|Edit|MultiEdit) hook — DENY a file write that targets one of the three
# scheduled sweeps' own state/log dirs (~/.claude/meta-{dispatch,reconcile,coherence}/**)
# from a META session, and name the granted verb to use instead.
#
# WHY THIS EXISTS (a stall, not a data loss). Every scheduled tick ended by writing
# `~/.claude/meta-<lane>/last-seen.json` with the `Write` tool. The harness classes
# `~/.claude/**` as a SENSITIVE path and raises its own approval prompt —
#   "Allow Claude to write last-seen.json? … Claude requested permissions to edit
#    ~/.claude/meta-dispatch/last-seen.json which is a sensitive file."
# — which neither a `Write(~/.claude/**)` allow rule nor `permissions.additionalDirectories`
# pre-answers, and which `bypassPermissions` does not suppress. An unattended run has
# nobody to click it, so the tick did its real work (bound PRs, read the lane, applied the
# cap) and then hung on the bookkeeping, holding the scheduler's slot until the operator
# happened to be at the laptop. Measured 2026-09-01 over 825 transcripts / 55,152 paired
# tool calls: `~/.claude/**` writes blocked >30 min in 49 of 267 `default`-mode cases
# (18.4%) against 2 of 323 in-cwd (0.6%). The granted-Bash path does not prompt —
# `meta-dispatch-move heartbeat` wrote under the same directory 161 times with 1 stall.
#
# The fix is the procedures (they now call the granted verbs). This hook is the FAIL-SAFE:
# procedure text drifts, a mirrored SKILL.md can be stale by a tick, and a model under a
# `<system-reminder>` telling it to prefer file tools can reach for `Write` anyway. The
# cost of getting it wrong is an unattended session that hangs for hours, so the rule is
# bound in code as well as in prose.
#
# SCOPE — deliberately narrow, and the narrowness is the design:
#   DENIED  : a path under $HOME/.claude/meta-dispatch/, meta-reconcile/, meta-coherence/
#             or pm-landing/ — exactly the dirs for which a granted writer EXISTS
#             (`meta-dispatch-move state` for last-seen.json, `… heartbeat` for log/).
#             A deny is only honest when there is something to do instead. `pm-landing` is
#             the odd name out: its dir carries no `meta-` prefix, so the list is spelled
#             out rather than derived.
#   PASSED  : everything else under $HOME/.claude/, INCLUDING the aspect ledgers at
#             ~/.claude/projects/*/memory/meta-state/*.json. Those are the same stall
#             class and they have NO granted writer yet, so blocking them would replace a
#             probabilistic stall with a certain failure. They are the known residual;
#             see internal/meta-dispatch-procedure.md TOOL DISCIPLINE.
#
# FAIL-SAFE CONTRACT (worst case == today's behavior — the write proceeds to the normal
# permission flow; NEVER a false deny of a chip writing in its own tree):
#   - Emits NOTHING (exit 0, passthrough) whenever anything is uncertain: no META
#     active-aspect marker for this session's cwd (non-META sessions are never touched),
#     an unparseable payload, a missing/empty/non-string file_path, a missing cwd, an
#     unset HOME, a tool that is not one of the three write surfaces, a path that
#     resolves INSIDE the session's checkout, or any jq/shasum failure.
#   - It never rewrites, redirects, or auto-approves anything. Its only two outputs are
#     "nothing" and one `permissionDecision: deny` carrying the remedy.
#
# Marker contract (cwd-keyed, sha256 of the canonical cwd) is mirrored from
# tools/hooks/meta-active-aspect.sh — the same self-contained copy prepend-meta-prefix.sh
# keeps, and for the same reason: a missing helper must never break an interceptor.
# Pinned end-to-end by tools/hooks/test_guard_out_of_cwd_write.sh.
#
# Maintained by META:substrate. To disable: remove the Write/Edit/MultiEdit matcher block
# pointing here from hooks.PreToolUse in ~/.claude/settings.json.
set -euo pipefail

input="$(cat)"

tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)" || exit 0
cwd="$(printf '%s'  "$input" | jq -r '.cwd // empty'       2>/dev/null)" || exit 0
[ -n "$tool" ] || exit 0
[ -n "$cwd" ]  || exit 0

case "$tool" in
  Write|Edit|MultiEdit) ;;
  *) exit 0 ;;
esac

# select(type=="string") makes the fail-safe explicit: a non-string file_path yields no
# value here rather than a string that happens to look like a path.
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty | select(type=="string")' 2>/dev/null)" || exit 0
[ -n "$fp" ] || exit 0

home="${HOME:-}"
[ -n "$home" ] || exit 0

# --- META marker for this cwd? (mirror of maa_key) -------------------------------------
canon="$( cd "$cwd" 2>/dev/null && pwd -P )" || canon=""
[ -n "$canon" ] || canon="$cwd"
if command -v shasum >/dev/null 2>&1; then
  key="$(printf '%s' "$canon" | shasum -a 256 2>/dev/null | awk '{print $1}')" || exit 0
elif command -v sha256sum >/dev/null 2>&1; then
  key="$(printf '%s' "$canon" | sha256sum 2>/dev/null | awk '{print $1}')" || exit 0
else
  exit 0
fi
[ -n "$key" ] || exit 0
[ -f "${home}/.claude/meta-state/active-aspect/${key}" ] || exit 0   # non-META => passthrough

# --- resolve the target to an absolute path -------------------------------------------
# Only a LITERAL path is classified. A token carrying $()/backtick/glob/quote could
# resolve anywhere, so it is uncertainty and passes through.
case "$fp" in
  *'$'*|*'`'*|*'*'*|*'?'*|*'['*|*'"'*|*"'"*) exit 0 ;;
esac
case "$fp" in
  '~/'*) target="${home}/${fp#\~/}" ;;
  '/'*)  target="$fp" ;;
  *)     target="${canon}/${fp}" ;;
esac
# Collapse `.` / `..` / duplicate slashes WITHOUT touching the filesystem (the file need
# not exist yet — a Write creates it), so `~/.claude/meta-dispatch/../meta-dispatch/x`
# cannot walk around the prefix test below.
target="$(printf '%s' "$target" | awk -F/ '{
  n=0
  for (i=1; i<=NF; i++) {
    if ($i == "" || $i == ".") continue
    if ($i == "..") { if (n > 0) n--; continue }
    parts[++n] = $i
  }
  out = ""
  for (i=1; i<=n; i++) out = out "/" parts[i]
  print (out == "" ? "/" : out)
}')" || exit 0
[ -n "$target" ] || exit 0

# A path inside this session's own checkout is never ours, whatever it looks like.
case "$target" in
  "$canon"/*) exit 0 ;;
esac

# --- the three guarded dirs ------------------------------------------------------------
lane=""
for d in "meta-dispatch:dispatch" "meta-reconcile:reconcile" "meta-coherence:coherence" \
         "pm-landing:pm-landing"; do
  case "$target" in
    "${home}/.claude/${d%%:*}/"*) lane="${d##*:}"; break ;;
  esac
done
[ -n "$lane" ] || exit 0                      # everything else => passthrough

dir="$(printf '%s' "$target" | sed -e "s|^${home}/.claude/||" -e 's|/.*||')"
case "$target" in
  */log/*|*/log)  remedy="python3 tools/meta-dispatch-move heartbeat --log-dir '~/.claude/${dir}/log' … (it appends in one O_APPEND write and stamps UTC itself; a whole-file Write here destroyed three heartbeats on 2026-08-27)" ;;
  *last-seen.json) remedy="python3 tools/meta-dispatch-move state --lane ${lane} --json '<whole object>' (or --merge '<partial>' to add one key without re-sending the rest)" ;;
  *)               remedy="python3 tools/meta-dispatch-move state --lane ${lane} … for last-seen.json, or … heartbeat --log-dir '~/.claude/${dir}/log' … for the run log" ;;
esac

printf '%s' "Blocked: \`${tool}\` targets ${target}, under ~/.claude/${dir}/. The harness treats ~/.claude/** as a sensitive path and raises its own approval prompt there regardless of any Write grant — in an unattended scheduled run there is nobody to answer it, so the tick hangs after doing its real work. Use the granted command instead, which cannot stall: ${remedy}. (guard: tools/hooks/guard-out-of-cwd-write.sh; passthrough on any uncertainty, and the aspect ledgers under ~/.claude/projects/ are deliberately NOT guarded.)" \
  | jq -R -s '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:.}}'
