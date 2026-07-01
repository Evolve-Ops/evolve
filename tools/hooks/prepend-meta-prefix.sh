#!/bin/bash
# PreToolUse hook (matchers: Agent, mcp__ccd_session__spawn_task) — when a META
# coordinator session spawns a chip / subagent, deterministically prepend "[META:<id>] "
# to the chip's display title IF it lacks a [META: prefix, so the propagation rule can't
# be silently forgotten. This is the mechanism layer complementing the prose rule in the
# /meta + /design skills (META:substrate; spec-substrate-2026-06-15 §9).
#
# TWO SPAWN SURFACES (the display field differs by tool):
#   Agent                        -> tool_input.description   (short subagent task line)
#   mcp__ccd_session__spawn_task -> tool_input.title         (background-task chip label)
#
# FAIL-SAFE CONTRACT (worst case == today's behavior: an UN-prefixed title; NEVER a
# blocked, dropped, auto-approved, or otherwise-altered spawn):
#   - Emits hookSpecificOutput.updatedInput ALONE — NO permissionDecision. Per the
#     Claude Code hook docs, omitting permissionDecision is equivalent to "defer": the
#     (re-titled) spawn proceeds through the NORMAL permission/approval flow. This hook
#     ONLY edits the title field; it must NEVER change the spawn's approval behavior.
#   - updatedInput REPLACES the whole tool_input, so we echo the ORIGINAL tool_input
#     back verbatim with ONLY the one title field changed (prompt/subagent_type/model/
#     tldr/cwd/... preserved value-for-value).
#   - Passthrough (exit 0, emit NOTHING) WHENEVER:
#       * no active-aspect marker exists for this session's cwd (non-META sessions are
#         NEVER touched),
#       * the display field already starts with "[META:" (idempotent; never double-prefix),
#       * the marker content is not a valid kebab aspect id (^[a-z0-9][a-z0-9-]{0,30}$ —
#         strict lowercase kebab; rejects junk, whitespace, and shell/jq-injection chars),
#       * the display field is missing/empty, the cwd is missing, the tool is not one of
#         the two known surfaces, or ANY jq / shasum / parse error occurs.
#
# Marker contract (cwd-keyed; no session-id dependency — both the spawning session and
# this hook see the same cwd): ~/.claude/meta-state/active-aspect/<key>, key = sha256 of
# the canonical cwd, content = aspect id. Written by /meta & /design, cleared by /close.
# The key algorithm below is mirrored from maa_key() in tools/hooks/meta-active-aspect.sh
# (kept identical; guarded end-to-end by test_prepend_meta_prefix.sh). The hook is
# deliberately self-contained — it does NOT source the helper, so a missing helper can
# never break this interceptor.
#
# Maintained by META:substrate. To disable: remove the Agent and
# mcp__ccd_session__spawn_task matcher blocks from hooks.PreToolUse in ~/.claude/settings.json.
set -euo pipefail

input="$(cat)"

# --- parse the fields we need; ANY jq failure => passthrough --------------------------
tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)" || exit 0
cwd="$(printf '%s' "$input"  | jq -r '.cwd // empty'       2>/dev/null)" || exit 0
[ -n "$tool" ] || exit 0
[ -n "$cwd" ]  || exit 0

# Which tool_input field carries the human-facing title? (Confirmed surfaces ONLY; any
# other tool that somehow reaches this hook is not ours and passes through untouched.)
case "$tool" in
  Agent)                        field="description" ;;
  mcp__ccd_session__spawn_task) field="title" ;;
  *) exit 0 ;;
esac

# --- compute the marker key from the canonical cwd (mirror of maa_key) ----------------
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

marker="${HOME:-}/.claude/meta-state/active-aspect/${key}"   # ${HOME:-} so set -u can't abort
[ -f "$marker" ] || exit 0          # no marker (or unset HOME) => passthrough

# --- read + validate the aspect id ----------------------------------------------------
# Strip ONLY the trailing line terminator(s) — NOT internal whitespace. A corrupted /
# tampered marker like "bad id" must FAIL validation and passthrough, never be silently
# coerced into a plausible-but-wrong id ("badid"). The id charset is strict kebab, so a
# valid id can carry no whitespace, no shell metacharacter, and nothing that could break
# out of the jq string concatenation below. Bound is generous enough for every real
# aspect id (the existing `model-tiers` is 11 chars — over the carve-protocol's nominal
# <=10 — so a literal {0,9} would silently never prefix that whole aspect).
aspect="$(head -n1 "$marker" 2>/dev/null | tr -d '\r\n')" || exit 0
[ -n "$aspect" ] || exit 0
[[ "$aspect" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || exit 0   # bad id => never inject junk

# --- read the current title; passthrough if absent, non-string, or already prefixed ---
# select(type=="string") makes the fail-safe EXPLICIT: a non-string title (number, array,
# object, null, false) yields no value => passthrough here, rather than relying on the
# final jq erroring on a string+non-string concatenation.
cur="$(printf '%s' "$input" | jq -r --arg f "$field" '.tool_input[$f] // empty | select(type=="string")' 2>/dev/null)" || exit 0
[ -n "$cur" ] || exit 0
case "$cur" in
  "[META:"*) exit 0 ;;              # already prefixed (any aspect) => idempotent passthrough
esac

# --- emit updatedInput: the FULL original tool_input, ONLY the title field changed ----
# permissionDecision is intentionally OMITTED so the spawn keeps its normal approval flow.
printf '%s' "$input" | jq -c \
  --arg f "$field" --arg id "$aspect" '
    .tool_input as $ti
    | ($ti + {($f): ("[META:" + $id + "] " + ($ti[$f]))})
    | {hookSpecificOutput: {hookEventName: "PreToolUse", updatedInput: .}}
  ' 2>/dev/null || exit 0
