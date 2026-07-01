#!/bin/bash
# PreToolUse hook (matchers: Agent, mcp__ccd_session__spawn_task) — on EVERY chip /
# subagent spawn from a known active META aspect, record a dispatch CLAIM to
# ~/.claude/meta-state/claims/<claim-id>.json = {aspect, title, time, ttl_seconds, ...}.
# This closes the "invisible mid-flight window": `tools/meta-inflight` scans ledgers +
# open PRs, but a just-dispatched chip has NEITHER (no PR yet; nothing in a ledger until
# a coordinator writes it), so a 2nd dispatch of the same work can't see the first. A
# claim is written the instant the spawn happens, so meta-inflight's claims signal makes
# that no-PR-yet work visible immediately (META:substrate spec §17 / Initiative 12, B4).
#
# SELF-PROPAGATION DOWN THE SPAWN TREE (why this closes the "dies one level down" gap):
# this hook fires whenever an active-aspect marker exists for the spawning session's cwd.
# The prepend-meta-prefix.sh hook titles every spawned chip "[META:<id>] …", and the
# meta-marker-register-on-start.sh SessionStart hook makes any [META:<id>]-titled session
# self-register its OWN marker — so a chip, and its grandchildren, all carry a marker and
# all record claims. No prose the model can forget; the mechanism propagates itself.
#
# TWO SPAWN SURFACES (the display/title field differs by tool — identical to the prefix hook):
#   Agent                        -> tool_input.description   (short subagent task line)
#   mcp__ccd_session__spawn_task -> tool_input.title         (background-task chip label)
#
# FAIL-SAFE CONTRACT (worst case == today's behavior: NO claim recorded — never a blocked,
# altered, delayed, or auto-approved spawn):
#   - This hook performs a SIDE-EFFECT ONLY (write the claim). It emits NOTHING on stdout —
#     no hookSpecificOutput, no updatedInput, no permissionDecision — so the spawn proceeds
#     through the NORMAL permission/approval flow, completely unchanged. Unlike
#     prepend-meta-prefix.sh (which rewrites the title via updatedInput), this hook NEVER
#     touches the spawn's input or its approval behavior.
#   - It NEVER blocks. Operator decision is record-always + WARN (fuzzy title/scope overlap
#     must not block legit parallel work); the WARN is surfaced later by `tools/meta-inflight`
#     reading the claims registry, NOT by this hook.
#   - `trap 'exit 0' EXIT` + `set -euo pipefail`: ANY error (no marker / not a spawn tool /
#     bad json / write failure / missing dep / invalid id) lands on the trap and exits 0
#     with no output => passthrough. The claim is best-effort; its absence just reverts to
#     today's invisibility, never harms the spawn.
#   - A claim is recorded ONLY when an active-aspect marker exists for this cwd (a real META
#     session). Non-META sessions record nothing (passthrough), exactly like the prefix hook.
#
# MARKER CONTRACT (cwd-keyed; no session-id dependency — the spawning session and this hook
# see the same cwd): ~/.claude/meta-state/active-aspect/<key>, key = sha256 of the canonical
# cwd, content = aspect id. Written by /meta & /design (and self-registered by
# meta-marker-register-on-start.sh), cleared by /close. The key algorithm below is mirrored
# BYTE-FOR-BYTE from maa_key() in tools/hooks/meta-active-aspect.sh and the inline copy in
# prepend-meta-prefix.sh (the hook is self-contained — it does NOT source the helper, so a
# missing helper can never break it). Guarded by test_record_dispatch_claim.sh.
#
# TTL default = 4h (14400s), override with $META_CLAIM_TTL_SECONDS. Rationale: a claim only
# needs to bridge the window from dispatch until the work becomes visible another way (its
# PR appears in `gh pr list`, or a coordinator records it in a ledger). Because the policy
# is warn-only, an over-long TTL costs at most a spurious advisory (a dead chip's claim
# lingering), while an over-short TTL reintroduces the very invisibility gap this closes —
# so we err generous. `tools/meta-inflight` ignores (and prunes) expired claims, so a dead
# chip's claim self-expires and never warns forever.
#
# Maintained by META:substrate. To disable: remove the Agent and
# mcp__ccd_session__spawn_task matcher blocks that point at this script from
# hooks.PreToolUse in ~/.claude/settings.json.

# Belt-and-suspenders: force a clean exit (passthrough) on EVERY code path before set -e bites.
trap 'exit 0' EXIT
set -euo pipefail

input="$(cat 2>/dev/null || true)"
[ -n "$input" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

# --- parse the fields we need; ANY jq failure => passthrough ---------------------------
tool="$( { printf '%s' "$input" | jq -r '.tool_name // empty'; } 2>/dev/null || true)"
cwd="$(  { printf '%s' "$input" | jq -r '.cwd // empty';       } 2>/dev/null || true)"
[ -n "$tool" ] || exit 0
[ -n "$cwd" ]  || exit 0

# Which tool_input field carries the human-facing title? (Confirmed surfaces ONLY; any
# other tool that somehow reaches this hook is not ours and records nothing.)
case "$tool" in
  Agent)                        field="description" ;;
  mcp__ccd_session__spawn_task) field="title" ;;
  *) exit 0 ;;
esac

# --- compute the marker key from the canonical cwd (mirror of maa_key) -----------------
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

[ -n "${HOME:-}" ] || exit 0                                   # no HOME => nowhere to key/write
marker="${HOME}/.claude/meta-state/active-aspect/${key}"
[ -f "$marker" ] || exit 0          # no marker => non-META session => record nothing

# --- read + validate the aspect id (strict kebab; anti-junk + anti-injection) ---------
# Strip ONLY the trailing line terminator(s), never internal whitespace — a corrupted
# marker like "bad id" must FAIL validation and passthrough, never be coerced or spliced.
# The charset guarantees a valid id can carry no shell/jq metacharacter. Byte-identical to
# prepend-meta-prefix.sh so a claim can only ever be attributed to a real, validated aspect.
aspect="$(head -n1 "$marker" 2>/dev/null | tr -d '\r\n')" || exit 0
[ -n "$aspect" ] || exit 0
[[ "$aspect" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || exit 0        # bad id => never record junk

# --- read the display title; passthrough if absent or non-string ----------------------
# select(type=="string") makes the fail-safe explicit: a non-string title (number/array/
# object/null) yields no value => passthrough (nothing to record).
title="$(printf '%s' "$input" | jq -r --arg f "$field" '.tool_input[$f] // empty | select(type=="string")' 2>/dev/null)" || exit 0
[ -n "$title" ] || exit 0

# --- session id (best-effort; omitted from the claim if absent) -----------------------
session="$( { printf '%s' "$input" | jq -r '.session_id // empty'; } 2>/dev/null || true)"

# --- TTL (default 4h; env override must be a positive integer, else fall back) ---------
ttl="${META_CLAIM_TTL_SECONDS:-14400}"
[[ "$ttl" =~ ^[1-9][0-9]*$ ]] || ttl="14400"

now="$(date +%s 2>/dev/null)" || exit 0
[[ "$now" =~ ^[0-9]+$ ]] || exit 0

# --- write the claim atomically (temp in the SAME dir + mv); best-effort ---------------
# claim-id = <epoch>-<pid>-<random>: unique per spawn even for rapid same-second dispatches.
CLAIMS_DIR="${HOME}/.claude/meta-state/claims"
mkdir -p "$CLAIMS_DIR" 2>/dev/null || exit 0
cid="${now}-$$-${RANDOM}"
tmp="${CLAIMS_DIR}/.${cid}.tmp.$$"

# Build the JSON with jq --arg/--argjson so the title (which can carry quotes, braces, and
# shell/jq metacharacters) is safely encoded — never string-concatenated. `scope` is left
# empty on purpose: the spawn input carries no structured scope field, and inventing globs
# from the title would produce false file-level collisions. meta-inflight matches a claim
# on its aspect + title keywords (the design; see spec §17 / docs/meta-ledger-schema.md).
jq -cn \
  --arg aspect "$aspect" \
  --arg title "$title" \
  --arg tool "$tool" \
  --arg session "$session" \
  --argjson time "$now" \
  --argjson ttl "$ttl" '
    {aspect: $aspect, title: $title, time: $time, ttl_seconds: $ttl, tool: $tool, scope: []}
    + (if $session != "" then {session: $session} else {} end)
  ' > "$tmp" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }

mv -f "$tmp" "${CLAIMS_DIR}/${cid}.json" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }

# Side-effect done. Emit NOTHING so the spawn proceeds through the normal flow, unchanged.
exit 0
