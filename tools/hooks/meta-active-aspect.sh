#!/bin/bash
# Shared helper for the META "active-aspect" marker (cwd-keyed) — the small piece of
# operator-local runtime state that lets the prepend-meta-prefix.sh PreToolUse hook
# know which META aspect "owns" the current session, so chip/subagent titles spawned
# from a coordinator get the [META:<id>] prefix deterministically.
#
#   WRITER  : the /meta and /design skills, at bootstrap, once the aspect id resolves.
#   READER  : prepend-meta-prefix.sh (the hook), keyed off the PreToolUse `cwd` field.
#   CLEANUP : the /close skill (best-effort; non-blocking).
#
# Marker path : ~/.claude/meta-state/active-aspect/<key>
#   <key>     = sha256 hex of the CANONICAL (symlink-resolved) absolute working dir
#   content   = the aspect id on one line (strict kebab: ^[a-z0-9][a-z0-9-]{0,30}$)
#
# This is operator-machine-local transient state (like ~/.claude/hooks/ itself), NOT
# the durable per-aspect ledger (memory/meta-state/<id>.json) and NOT version-control.
#
# KEY-ALGORITHM CONTRACT: maa_key() below is mirrored byte-for-byte inside
# prepend-meta-prefix.sh (the hook is intentionally self-contained — it does NOT source
# this file at runtime, so a missing/renamed helper can never break the interceptor).
# The contract test (test_prepend_meta_prefix.sh) writes a marker via THIS helper and
# then feeds the hook a payload with the same cwd, asserting the two agree end-to-end —
# that is the guard against the two copies drifting apart.
#
# Usage:
#   meta-active-aspect.sh write <id> [<dir>]   # write marker for <dir> (default: $PWD)
#   meta-active-aspect.sh read        [<dir>]  # print the valid id for <dir>, else nothing
#   meta-active-aspect.sh clear       [<dir>]  # remove marker for <dir> (best-effort)
#   meta-active-aspect.sh key         [<dir>]  # print the key for <dir>   (debug / test)
#   meta-active-aspect.sh path        [<dir>]  # print the marker path for <dir> (debug)
#
# Fail-safe: every path exits 0 except (a) `write` with an invalid id and (b) an
# unknown subcommand, which exit 2 so a caller notices misuse. The helper NEVER blocks
# its caller and NEVER errors a skill bootstrap on a transient FS hiccup.
#
# Maintained by META:substrate.
set -uo pipefail

MARKER_DIR="${HOME:-}/.claude/meta-state/active-aspect"   # ${HOME:-} so set -u can't abort

# Resolve <dir> to its canonical (symlink-resolved) absolute form. Falls back to the
# raw input if it can't be entered (deleted/racing dir) — still deterministic. This
# matters because /tmp -> /private/tmp (macOS) and worktree symlinks would otherwise
# make the writer's $PWD and the hook's reported cwd hash differently.
maa_canon() {
  local raw="$1" canon
  canon="$( cd "$raw" 2>/dev/null && pwd -P )" || canon=""
  [ -n "$canon" ] || canon="$raw"
  printf '%s' "$canon"
}

# sha256 hex of the canonical dir. Prefer `shasum` (macOS), fall back to `sha256sum`
# (Linux); both emit the same SHA-256 digest for byte-identical input. Empty on neither.
maa_key() {
  local canon
  canon="$(maa_canon "$1")"
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$canon" | shasum -a 256 2>/dev/null | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$canon" | sha256sum 2>/dev/null | awk '{print $1}'
  fi
}

# Strict lowercase-kebab aspect id (anti-junk + anti-injection). Bound is {0,30} (1-31
# chars) — comfortably covers every real aspect id, incl. the grandfathered 11-char
# `model-tiers` that exceeds the carve protocol's nominal <=10. Kept IDENTICAL to the
# validation inlined in prepend-meta-prefix.sh.
maa_valid_id() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]]; }

cmd="${1:-}"

case "$cmd" in
  write)
    id="${2:-}"
    dir="${3:-$PWD}"
    if ! maa_valid_id "$id"; then
      printf 'meta-active-aspect: invalid aspect id %q (expected ^[a-z0-9][a-z0-9-]{0,30}$)\n' "$id" >&2
      exit 2
    fi
    key="$(maa_key "$dir")"; [ -n "$key" ] || exit 0
    mkdir -p "$MARKER_DIR" 2>/dev/null || exit 0
    tmp="${MARKER_DIR}/.${key}.tmp.$$"
    printf '%s\n' "$id" > "$tmp" 2>/dev/null || exit 0
    mv -f "$tmp" "${MARKER_DIR}/${key}" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }
    ;;
  read)
    dir="${2:-$PWD}"
    key="$(maa_key "$dir")"; [ -n "$key" ] || exit 0
    m="${MARKER_DIR}/${key}"
    [ -f "$m" ] || exit 0
    id="$(head -n1 "$m" 2>/dev/null | tr -d '\r\n')" || exit 0   # trailing CR/LF only
    maa_valid_id "$id" || exit 0
    printf '%s\n' "$id"
    ;;
  clear)
    dir="${2:-$PWD}"
    key="$(maa_key "$dir")"; [ -n "$key" ] || exit 0
    rm -f "${MARKER_DIR}/${key}" 2>/dev/null || true
    ;;
  key)
    dir="${2:-$PWD}"
    maa_key "$dir"
    ;;
  path)
    dir="${2:-$PWD}"
    key="$(maa_key "$dir")"; [ -n "$key" ] || exit 0
    printf '%s\n' "${MARKER_DIR}/${key}"
    ;;
  *)
    echo "usage: meta-active-aspect.sh {write <id> [dir]|read [dir]|clear [dir]|key [dir]|path [dir]}" >&2
    exit 2
    ;;
esac
