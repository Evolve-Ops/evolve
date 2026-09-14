#!/bin/bash
# PreToolUse(Bash) hook — WARN (never block) when a session is about to start SELF-WORK
# that overlaps work already in flight.
#
# THE READ SIDE. `record-dispatch-claim.sh` closed the WRITE side: self-work now records a
# claim at branch-create, so dispatchers that run `meta-inflight` can see it. But a
# self-working session still checks NOTHING of its own — `internal/meta-reconcile-procedure.md`
# calls `meta-inflight` zero times, and a coordinator told "just fix it here" never reaches the
# dispatch lane's step that does. On 2026-08-27 that asymmetry let two sessions build the same
# fleet-blocking tile_metrics fix; it surfaced only because one happened to message the other.
#
# Fixing this with procedure prose would repeat the mistake the claim layer was built to
# correct — spec §17 names "the 'run meta-inflight before you spawn' instruction is advisory
# prose the model forgets" as gap (2). So the check is bound to the same deterministic moment
# the claim is: branch creation.
#
# WARN, NEVER BLOCK — this is the operator's recorded policy for the claim layer
# (record-always + WARN; blocking/ask on overlap was explicitly DEFERRED, spec §17.8). The hook
# therefore emits `hookSpecificOutput.additionalContext` and NOTHING else: no
# `permissionDecision`, no `updatedInput`. The command proceeds through the normal
# permission/approval flow, completely unchanged; the overlap is surfaced as context for the
# model to weigh. Same shape as the background-agent guardrail already in settings.json.
#
# FAIL-SAFE CONTRACT — worst case is today's behavior (no warning), NEVER a stopped command:
# `trap 'exit 0' EXIT` + `set -euo pipefail`, so ANY error exits 0 silently. It emits nothing
# whenever: the command is not a branch-create, no active-aspect marker exists for this cwd,
# the marker is not a valid kebab id, the repo root or `tools/meta-inflight` cannot be located,
# meta-inflight errors / times out / returns unparseable JSON, or no overlap survives filtering.
#
# COST. Runs only on branch-create (rare), not on ordinary commands — the branch-create test is
# the FIRST thing it does, before any other work. `--no-prs` deliberately: the open-PR scan is
# the slow, networked signal, and the signals this path is actually blind to are the mid-flight
# ones (active claims, non-terminal ledger chips, backlog). A `gh` call inside a 10s hook
# timeout on a flaky network would spend the budget and emit nothing.
#
# SELF-EXCLUSION. `record-dispatch-claim.sh` claims on the SAME branch-create, so this hook
# would otherwise report the session's own claim as an overlap. Hook execution order within a
# matcher is not something to rely on, so the exclusion is by VALUE, not by ordering: this hook
# computes the exact title the claim hook writes and drops that claim from the results.
# ⚠ The branch-create regex and the title format below are MIRRORED from
# record-dispatch-claim.sh. Change one, change both (and both test suites).
#
# Maintained by META:substrate. To disable: remove the "matcher": "Bash" block pointing at this
# script from hooks.PreToolUse in ~/.claude/settings.json.

trap 'exit 0' EXIT
set -euo pipefail

input="$(cat 2>/dev/null || true)"
[ -n "$input" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

tool="$( { printf '%s' "$input" | jq -r '.tool_name // empty'; } 2>/dev/null || true)"
[ "$tool" = "Bash" ] || exit 0

# --- cheapest disqualifier first: is this even a branch-create? -----------------------
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty | select(type=="string")' 2>/dev/null)" || exit 0
[ -n "$cmd" ] || exit 0
_bc_re='^[[:space:]]*git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(checkout[[:space:]]+-[bB]|switch[[:space:]]+-[cC])[[:space:]]+([^[:space:]-][^[:space:]]*)'
[[ "$cmd" =~ $_bc_re ]] || exit 0
branch="${BASH_REMATCH[3]}"
[ -n "$branch" ] || exit 0

cwd="$( { printf '%s' "$input" | jq -r '.cwd // empty'; } 2>/dev/null || true)"
[ -n "$cwd" ] || exit 0

# --- marker gate (mirror of maa_key; a non-META session is nobody's business) ----------
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
[ -n "${HOME:-}" ] || exit 0
marker="${HOME}/.claude/meta-state/active-aspect/${key}"
[ -f "$marker" ] || exit 0
aspect="$(head -n1 "$marker" 2>/dev/null | tr -d '\r\n')" || exit 0
[ -n "$aspect" ] || exit 0
[[ "$aspect" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || exit 0

# --- locate the checkout's meta-inflight (generic: never a hardcoded repo path) --------
root="$( cd "$canon" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null )" || root=""
[ -n "$root" ] || exit 0
tool_path="${root}/tools/meta-inflight"
[ -f "$tool_path" ] || exit 0

# --- keywords from the branch name ----------------------------------------------------
# Separators become spaces; drop the aspect's own words and generic branch noise, which
# would match everything in the aspect and drown the real signal. Keep <=5.
words="$(printf '%s' "$branch" | tr '/_-' '   ')"
title="branch ${branch} (${words})"
kw="$(ASPECT="$aspect" WORDS="$words" python3 -c '
import os, re
noise = {"meta","claude","fix","fixes","feat","feature","chore","wip","tmp","temp",
         "branch","the","and","for","with","from","into","test","tests"}
noise |= set(re.split(r"[-_/ ]+", os.environ["ASPECT"].lower()))
seen, out = set(), []
for w in os.environ["WORDS"].lower().split():
    w = re.sub(r"[^a-z0-9]", "", w)
    if len(w) < 4 or w in noise or w in seen:
        continue
    seen.add(w); out.append(w)
print(",".join(out[:5]))
' 2>/dev/null)" || exit 0
[ -n "$kw" ] || exit 0

# --- ask meta-inflight, drop our own claim, render ------------------------------------
raw="$(python3 "$tool_path" --aspect "$aspect" --keywords "$kw" --no-prs --json 2>/dev/null)" || exit 0
[ -n "$raw" ] || exit 0

msg="$(SELF_TITLE="$title" BRANCH="$branch" python3 -c '
import json, os, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
self_title = os.environ["SELF_TITLE"]
# IN-FLIGHT ONLY. The gap being closed is two sessions doing the same thing AT THE SAME
# TIME, so the warning must trigger on work that is actually running: an active claim, a
# non-terminal chip, an open PR, a live session. A `backlog` entry is work WRITTEN DOWN,
# not work under way — including it drowned the real signal (a live query returned 19
# overlaps, 18 of them backlog, two already marked `shipped:`), and a warning that cries
# wolf is one that gets ignored. The dispatch lane still checks backlog; this hook does not.
INFLIGHT_KINDS = {"claim", "chip", "pr", "session"}
# Defensive: a chip whose bucket still reads as finished is not in flight. meta-inflight
# already excludes terminal buckets, but `workflow_done` reached these results, so do not
# rely on that alone here.
DONE_MARKERS = ("done", "merged", "closed", "shipped", "archived", "abandoned")


def _is_done(o):
    b = (o.get("bucket") or "").lower()
    return any(m in b for m in DONE_MARKERS)


kept = []
for o in data.get("overlaps") or []:
    # our own just-written claim is not an overlap with anything
    if o.get("kind") == "claim" and (o.get("title") or "") == self_title:
        continue
    if o.get("kind") not in INFLIGHT_KINDS:
        continue
    if _is_done(o):
        continue
    kept.append(o)
if not kept:
    sys.exit(0)
lines = []
for o in kept[:3]:
    t = (o.get("title") or "").strip().replace("\n", " ")
    if len(t) > 110:
        t = t[:107] + "..."
    bits = [b for b in (o.get("bucket"), ("PR #%s" % o["pr"]) if o.get("pr") else None) if b]
    lines.append("  - [%s] %s%s" % (o.get("kind") or "?", t, (" (%s)" % ", ".join(bits)) if bits else ""))
more = "" if len(kept) <= 3 else "\n  ... and %d more" % (len(kept) - 3)
n = len(kept)
subject = "1 piece of work already overlaps" if n == 1 else "%d pieces of work already overlap" % n
print(
    "⚠ IN-FLIGHT OVERLAP — you are creating branch %s, and %s it:\n%s%s\n"
    "  ADVISORY, not a stop: this is the pre-dispatch check the self-work path never ran "
    "(META:substrate spec §17.8). Confirm you are not duplicating a sibling before you "
    "continue. Full detail: tools/meta-inflight --aspect <id> --keywords <kw>."
    % (os.environ["BRANCH"], subject, "\n".join(lines), more)
)
' <<< "$raw" 2>/dev/null)" || exit 0
[ -n "$msg" ] || exit 0

# additionalContext ONLY — no permissionDecision, no updatedInput. The command proceeds
# through the normal flow; this is a warning, not a gate.
jq -cn --arg m "$msg" \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:$m}}' 2>/dev/null || exit 0
exit 0
