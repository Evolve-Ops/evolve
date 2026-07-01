#!/bin/bash
# SessionStart hook — when a session's TITLE already carries the META aspect tag, make the
# session self-register its active-aspect marker for its own cwd, so the [META:<id>] prefix
# propagates down the WHOLE spawn tree (coordinator -> chip -> grandchild -> ...).
#
# WHY (the live gap, proven 2026-06-22): the prepend-meta-prefix.sh PreToolUse hook (§9 /
# I4) only prefixes spawns from a session that has WRITTEN an active-aspect marker for its
# cwd — and ONLY the /meta + /design skills write one. Chip sessions never run /meta, so
# they never register, so the sub-agents / sub-chips THEY spawn go un-prefixed and lineage
# goes dark one level below the coordinator. This hook closes that: EVERY session whose
# title carries the tag self-registers on startup, so each prefixed child's own spawns then
# get prefixed by the PreToolUse hook in turn — the tag flows all the way down.
#
# This is the WRITER complement (driven by the session TITLE) to the marker the /meta and
# /design skills write at bootstrap (driven by an explicit aspect id). Same marker file,
# same key, same reader (prepend-meta-prefix.sh). It only ADDS a write path; it never reads
# or alters how spawns are titled.
#
# TWO TITLE FORMS recognized (belt-and-suspenders so coordinators also self-register):
#   chip / child : "[META:<id>] <text>"   (anchored ^\[META:(...)\] )
#   coordinator  : "META <id>"            (anchored ^META (...)$ — what /meta forces)
# where <id> is STRICT lowercase kebab (^[a-z0-9][a-z0-9-]{0,30}$ — the EXACT validator from
# prepend-meta-prefix.sh / meta-active-aspect.sh). Anything else (a plain "Fix the widget"
# title, "[META:Bad Id]", "[META:../etc]", "META not a kebab") fails to match / validate and
# the hook no-ops. So the hook only ever writes a clean, valid aspect id — never junk.
#
# FAIL-SAFE CONTRACT (worst case == today's behavior: no marker for chip sessions, so the
# child's spawns stay un-prefixed; NEVER a blocked, delayed, or errored-loud session start):
#   - `trap 'exit 0' EXIT` + `set -euo pipefail`: ANY error / unset var / pipe failure /
#     signal lands on the trap and exits 0. The hook cannot fail loudly.
#   - Emits NOTHING on stdout, ever. Per the Claude Code hook docs, a SessionStart hook's
#     stdout is INJECTED INTO THE MODEL CONTEXT — so this hook prints nothing; it never
#     pollutes the session. (SessionStart also cannot block the session — its exit code is
#     ignored — but the session waits synchronously, so silence + speed are still required.)
#   - No network. Bounded / instant: a couple of jq + shasum calls, then one small write.
#   - Most sessions (no [META:] / "META " title) hit the no-match branch and exit 0 at once.
#
# KEY-ALGORITHM CONTRACT: the cwd->key computation below is mirrored BYTE-FOR-BYTE from
# maa_key() in tools/hooks/meta-active-aspect.sh AND the inline copy in
# prepend-meta-prefix.sh. It MUST byte-match what prepend-meta-prefix.sh computes for the
# same cwd, or the marker this hook writes won't be the one the reader looks for. The hook
# is deliberately self-contained — it does NOT source the helper, so a missing/renamed
# helper can never break this writer. The equivalence is guarded end-to-end by
# test_meta_marker_register.sh (it computes the key via the REAL helper and via
# prepend-meta-prefix.sh's reader and asserts all three agree).
#
# SEMANTICS: this hook fires at session START, so already-open sessions need a restart to
# benefit. CLEANUP is /close's job — this hook never deletes or clears a marker.
#
# Maintained by META:substrate (spec-substrate-2026-06-15 §14, Initiative 9). To disable:
# remove the SessionStart block that points at this script from hooks.SessionStart in
# ~/.claude/settings.json.

# Belt-and-suspenders: force a clean exit on EVERY code path, before set -e can bite.
trap 'exit 0' EXIT
set -euo pipefail

input="$(cat 2>/dev/null || true)"
[ -n "$input" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

# --- extract the title + cwd from the SessionStart payload; any jq failure => exit 0 ------
# Wrap the WHOLE pipeline's stderr (not just jq's): on a large/malformed stdin, jq closes
# the pipe and the upstream `printf` builtin would otherwise emit a broken-pipe diagnostic.
title="$( { printf '%s' "$input" | jq -r '.session_title // empty'; } 2>/dev/null || true)"
cwd="$(   { printf '%s' "$input" | jq -r '.cwd // empty';           } 2>/dev/null || true)"
[ -n "$title" ] || exit 0      # no title (or not a META session) => nothing to register
[ -n "$cwd" ]   || exit 0      # no cwd => can't key a marker

# --- pull the aspect id out of the title, then validate it strictly ----------------------
# Conditions live in if/elif so a non-match does NOT trip `set -e` (we want to try both
# forms before giving up). The capture groups are permissive; the id_re below is the gate.
chip_re='^\[META:([^]]*)\]'    # "[META:<id>] ..."  -> capture up to the first ']'
coord_re='^META (.+)$'         # "META <id>"        -> capture the rest of the line
id_re='^[a-z0-9][a-z0-9-]{0,30}$'   # EXACT validator from prepend-meta-prefix.sh line ~83

aspect=""
if [[ "$title" =~ $chip_re ]]; then
  aspect="${BASH_REMATCH[1]}"
elif [[ "$title" =~ $coord_re ]]; then
  aspect="${BASH_REMATCH[1]}"
fi
[ -n "$aspect" ] || exit 0                 # no [META:] / "META " title => most sessions
[[ "$aspect" =~ $id_re ]] || exit 0        # junk / spaces / traversal / injection => no write

# --- compute the marker key from the canonical cwd (mirror of maa_key) -------------------
canon="$( cd "$cwd" 2>/dev/null && pwd -P )" || canon=""
[ -n "$canon" ] || canon="$cwd"
if command -v shasum >/dev/null 2>&1; then
  key="$(printf '%s' "$canon" | shasum -a 256 2>/dev/null | awk '{print $1}')" || key=""
elif command -v sha256sum >/dev/null 2>&1; then
  key="$(printf '%s' "$canon" | sha256sum 2>/dev/null | awk '{print $1}')" || key=""
else
  exit 0
fi
[ -n "$key" ] || exit 0

# --- write the marker atomically (temp in the SAME dir + mv); idempotent overwrite -------
MARKER_DIR="${HOME:-}/.claude/meta-state/active-aspect"   # ${HOME:-} so set -u can't abort
[ -n "${HOME:-}" ] || exit 0                              # no HOME => nowhere to write
mkdir -p "$MARKER_DIR" 2>/dev/null || exit 0
tmp="${MARKER_DIR}/.${key}.tmp.$$"
# Trailing newline matches meta-active-aspect.sh's writer byte-for-byte (the reader strips
# only the trailing CR/LF, so this is purely for write-path parity).
printf '%s\n' "$aspect" > "$tmp" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }
mv -f "$tmp" "${MARKER_DIR}/${key}" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }

exit 0
