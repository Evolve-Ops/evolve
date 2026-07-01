#!/bin/bash
# Regression guard for tools/meta-skills-sync — the auto-sync that refreshes the
# operator-global ~/.claude/skills/<name>/SKILL.md mirror from origin/main, driven by the
# SessionStart hook tools/hooks/meta-skills-sync-on-start.sh.
#
#   bash tools/hooks/test_meta_skills_sync.sh
#
# Exits 0 only if every assertion passes; non-zero (and prints which cases failed) otherwise.
# Requires `git` (the tool's only hard dep).
#
# ISOLATION: every invocation runs with HOME pointed at a throwaway temp dir, so the test
# NEVER reads or writes the operator's real ~/.claude/skills mirror or ref cache. The fake
# "origin/main" is a local refs/remotes/origin/main ref (git update-ref) — no network.
#
# The tool overwrites the cross-branch / cross-account skill mirror, so the hard safety
# properties are asserted explicitly: it NEVER deletes a mirror-only skill, NEVER rewrites a
# byte-identical file, NEVER mutates anything under --dry-run, and no-ops cleanly (exit 0)
# when there is no usable repo.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$(cd "$SCRIPT_DIR/.." && pwd)/meta-skills-sync"
HOOK="$SCRIPT_DIR/meta-skills-sync-on-start.sh"

if [ ! -x "$TOOL" ]; then
  echo "FATAL: sync tool not found or not executable: $TOOL" >&2
  exit 2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "FATAL: required dependency 'git' not on PATH" >&2
  exit 2
fi

# Deterministic git identity / no signing for the fake repos.
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=t@t
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=t@t

TEST_HOME="$(mktemp -d)"
SCRATCH="$(mktemp -d)"          # where fake repos live
cleanup() { rm -rf "$TEST_HOME" "$SCRATCH"; }
trap cleanup EXIT

PASS=0
FAIL=0
FAILED_LABELS=()

fail() { FAIL=$((FAIL + 1)); FAILED_LABELS+=("$1"); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }
ok()   { PASS=$((PASS + 1)); printf 'ok    %s\n' "$1"; }

# ---- fake-repo helpers ------------------------------------------------------------------
new_repo() {                    # $1 = repo dir
  local r="$1"
  mkdir -p "$r"
  git -C "$r" init -q
  git -C "$r" config user.email t@t
  git -C "$r" config user.name test
  git -C "$r" config commit.gpgsign false
}
add_skill_origin() {            # $1=repo $2=name $3=content  (stage only; publish() commits)
  local r="$1" n="$2" c="$3"
  mkdir -p "$r/.claude/skills/$n"
  printf '%s\n' "$c" > "$r/.claude/skills/$n/SKILL.md"
}
publish() {                     # $1=repo : commit all + move origin/main to the new HEAD
  local r="$1"
  git -C "$r" add -A
  git -C "$r" commit -q -m "snapshot" >/dev/null 2>&1 || true
  git -C "$r" update-ref refs/remotes/origin/main HEAD
}

# ---- mirror helpers (all under the isolated TEST_HOME) ----------------------------------
MIRROR="$TEST_HOME/.claude/skills"
REFCACHE="$TEST_HOME/.claude/meta-state/skills-sync-last-ref"
reset_mirror() { rm -rf "$TEST_HOME/.claude"; }
put_mirror() {                  # $1=name $2=content
  mkdir -p "$MIRROR/$1"
  printf '%s\n' "$2" > "$MIRROR/$1/SKILL.md"
}
mfile() { printf '%s' "$MIRROR/$1/SKILL.md"; }
mcontent() { cat "$MIRROR/$1/SKILL.md" 2>/dev/null; }
inode() { ls -i "$1" 2>/dev/null | awk '{print $1}'; }

run_tool() { HOME="$TEST_HOME" EVOLVE_REPO="" "$TOOL" "$@"; }

echo "== T1: differing skill is REFRESHED from origin/main =="
R="$SCRATCH/r1"; new_repo "$R"
add_skill_origin "$R" alpha "ORIGIN-ALPHA-v2"; publish "$R"
reset_mirror; put_mirror alpha "STALE-ALPHA-v1"
out="$(run_tool --repo "$R")"; rc=$?
if [ $rc -ne 0 ]; then fail "T1 exit" "expected 0, got $rc"; fi
if [ "$(mcontent alpha)" = "ORIGIN-ALPHA-v2" ]; then ok "T1 mirror refreshed to origin content"
else fail "T1 content" "mirror alpha='$(mcontent alpha)' expected 'ORIGIN-ALPHA-v2'"; fi
case "$out" in *"1 refreshed"*) ok "T1 summary says '1 refreshed'";; *) fail "T1 summary" "got: $out";; esac

echo
echo "== T2: byte-identical skill is left UNTOUCHED (no rewrite — inode unchanged) =="
R="$SCRATCH/r2"; new_repo "$R"
add_skill_origin "$R" beta "SAME-BETA"; publish "$R"
reset_mirror; put_mirror beta "SAME-BETA"
ino_before="$(inode "$(mfile beta)")"
out="$(run_tool --repo "$R")"
ino_after="$(inode "$(mfile beta)")"
if [ -n "$ino_before" ] && [ "$ino_before" = "$ino_after" ]; then ok "T2 identical file not rewritten (inode stable)"
else fail "T2 inode" "inode changed $ino_before -> $ino_after (file was rewritten)"; fi
case "$out" in *"0 refreshed, 1 unchanged"*) ok "T2 summary '0 refreshed, 1 unchanged'";; *) fail "T2 summary" "got: $out";; esac

echo
echo "== T3: skill missing from the mirror is CREATED =="
R="$SCRATCH/r3"; new_repo "$R"
add_skill_origin "$R" gamma "NEW-GAMMA"; publish "$R"
reset_mirror
run_tool --repo "$R" >/dev/null
if [ "$(mcontent gamma)" = "NEW-GAMMA" ]; then ok "T3 absent skill created in mirror"
else fail "T3 create" "mirror gamma='$(mcontent gamma)' expected 'NEW-GAMMA'"; fi

echo
echo "== T4: mirror-only skill (absent on origin/main) is NEVER deleted =="
R="$SCRATCH/r4"; new_repo "$R"
add_skill_origin "$R" alpha "ORIGIN-ALPHA"; publish "$R"
reset_mirror
put_mirror alpha "STALE-ALPHA"
put_mirror localonly "DO-NOT-DELETE-ME"     # exists only in the mirror
run_tool --repo "$R" >/dev/null
if [ "$(mcontent localonly)" = "DO-NOT-DELETE-ME" ]; then ok "T4 mirror-only skill preserved"
else fail "T4 delete" "localonly was modified/deleted: '$(mcontent localonly)'"; fi
if [ "$(mcontent alpha)" = "ORIGIN-ALPHA" ]; then ok "T4 (and the origin skill still refreshed alongside)"
else fail "T4 alpha" "alpha='$(mcontent alpha)' expected 'ORIGIN-ALPHA'"; fi

echo
echo "== T5: no usable repo => clean exit 0, mirror untouched =="
reset_mirror; put_mirror alpha "UNTOUCHED"
out="$(run_tool --repo "$SCRATCH/does-not-exist")"; rc=$?
if [ $rc -eq 0 ]; then ok "T5a missing --repo path exits 0"; else fail "T5a exit" "got $rc"; fi
if [ -z "$out" ]; then ok "T5a emits nothing"; else fail "T5a output" "expected silence, got: $out"; fi
# A real git repo that simply has no origin/main ref is also a clean no-op.
NOREF="$SCRATCH/noref"; new_repo "$NOREF"; add_skill_origin "$NOREF" alpha "X"
git -C "$NOREF" add -A; git -C "$NOREF" commit -q -m x >/dev/null 2>&1   # commit but DON'T set origin/main
out="$(run_tool --repo "$NOREF")"; rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then ok "T5b repo without origin/main exits 0, silent"
else fail "T5b" "rc=$rc out='$out'"; fi
if [ "$(mcontent alpha)" = "UNTOUCHED" ]; then ok "T5 mirror untouched by no-op runs"
else fail "T5 mirror" "alpha changed to '$(mcontent alpha)'"; fi

echo
echo "== T6: --dry-run mutates NOTHING (no file write, no ref cache) =="
R="$SCRATCH/r6"; new_repo "$R"
add_skill_origin "$R" alpha "ORIGIN-DRY"; publish "$R"
reset_mirror; put_mirror alpha "STALE-DRY"
ino_before="$(inode "$(mfile alpha)")"
out="$(run_tool --repo "$R" --dry-run)"; rc=$?
ino_after="$(inode "$(mfile alpha)")"
if [ $rc -eq 0 ]; then ok "T6 dry-run exit 0"; else fail "T6 exit" "got $rc"; fi
case "$out" in *"would refresh"*) ok "T6 summary uses 'would refresh' phrasing";; *) fail "T6 summary" "got: $out";; esac
if [ "$(mcontent alpha)" = "STALE-DRY" ] && [ "$ino_before" = "$ino_after" ]; then ok "T6 mirror file unchanged (content + inode)"
else fail "T6 mutated" "content='$(mcontent alpha)' inode $ino_before->$ino_after"; fi
if [ ! -f "$REFCACHE" ]; then ok "T6 ref cache NOT created by dry-run"; else fail "T6 cache" "ref cache was written: $(cat "$REFCACHE")"; fi

echo
echo "== T7: HEAD-unchanged fast path — second run no-ops and does NOT repair a stale file =="
R="$SCRATCH/r7"; new_repo "$R"
add_skill_origin "$R" alpha "ORIGIN-FP"; publish "$R"
reset_mirror; put_mirror alpha "STALE-FP"
run_tool --repo "$R" >/dev/null                      # apply: writes alpha + ref cache
if [ -f "$REFCACHE" ] && [ "$(cat "$REFCACHE")" = "$(git -C "$R" rev-parse origin/main)" ]; then
  ok "T7 ref cache written with the synced SHA"
else fail "T7 cache" "ref cache missing/mismatched: '$(cat "$REFCACHE" 2>/dev/null)'"; fi
put_mirror alpha "RE-STALED-FP"                      # stale it again WITHOUT moving origin/main
out="$(run_tool --repo "$R")"; rc=$?
case "$out" in *"nothing to do"*) ok "T7 second run takes the fast path ('nothing to do')";; *) fail "T7 fastpath" "got: $out";; esac
if [ "$(mcontent alpha)" = "RE-STALED-FP" ]; then ok "T7 fast path correctly skips per-skill work (file left as-is by design)"
else fail "T7 unexpected-repair" "alpha='$(mcontent alpha)'"; fi

echo
echo "== T8: --force bypasses the fast path and repairs the stale file =="
out="$(run_tool --repo "$R" --force)"; rc=$?
if [ "$(mcontent alpha)" = "ORIGIN-FP" ]; then ok "T8 --force re-synced the stale file"
else fail "T8 force" "alpha='$(mcontent alpha)' expected 'ORIGIN-FP'"; fi
case "$out" in *"1 refreshed"*) ok "T8 summary '1 refreshed'";; *) fail "T8 summary" "got: $out";; esac

echo
echo "== T9: idempotent — applying twice in a row refreshes 0 the second time =="
R="$SCRATCH/r9"; new_repo "$R"
add_skill_origin "$R" alpha "I9A"; add_skill_origin "$R" beta "I9B"; publish "$R"
reset_mirror
run_tool --repo "$R" >/dev/null
out="$(run_tool --repo "$R" --force)"      # --force so the fast path doesn't mask the comparison
case "$out" in *"0 refreshed, 2 unchanged"*) ok "T9 second apply '0 refreshed, 2 unchanged'";; *) fail "T9" "got: $out";; esac

echo
echo "== T10: discovery via --cwd (no --repo) finds the repo by its git toplevel =="
R="$SCRATCH/r10"; new_repo "$R"
add_skill_origin "$R" alpha "DISCOVER-ME"; publish "$R"
reset_mirror
mkdir -p "$R/some/deep/dir"
out="$(run_tool --cwd "$R/some/deep/dir")"; rc=$?    # cwd a subdir => toplevel resolves to R
if [ $rc -eq 0 ] && [ "$(mcontent alpha)" = "DISCOVER-ME" ]; then ok "T10 repo discovered from --cwd subdir"
else fail "T10 discovery" "rc=$rc alpha='$(mcontent alpha)'"; fi

echo
echo "== T11: unknown flag => usage error exit 2 =="
HOME="$TEST_HOME" "$TOOL" --bogus >/dev/null 2>&1; rc=$?
if [ $rc -eq 2 ]; then ok "T11 unknown flag exits 2"; else fail "T11" "expected exit 2, got $rc"; fi

echo
echo "== T12: SessionStart hook is SILENT on stdout and refreshes via the cwd payload =="
if [ -x "$HOOK" ]; then
  R="$SCRATCH/r12"; new_repo "$R"
  # The hook locates the tool at <toplevel>/tools/meta-skills-sync; give the fake repo a copy.
  mkdir -p "$R/tools"; cp "$TOOL" "$R/tools/meta-skills-sync"; chmod +x "$R/tools/meta-skills-sync"
  add_skill_origin "$R" alpha "HOOK-ALPHA"; publish "$R"
  reset_mirror
  payload="$(printf '{"hook_event_name":"SessionStart","source":"startup","cwd":"%s","session_id":"t"}' "$R")"
  hout="$(printf '%s' "$payload" | HOME="$TEST_HOME" EVOLVE_REPO="" bash "$HOOK")"; rc=$?
  if [ $rc -eq 0 ]; then ok "T12 hook exit 0"; else fail "T12 exit" "got $rc"; fi
  if [ -z "$hout" ]; then ok "T12 hook emits NOTHING on stdout (won't pollute model context)"
  else fail "T12 stdout" "expected silence, hook emitted: $hout"; fi
  if [ "$(mcontent alpha)" = "HOOK-ALPHA" ]; then ok "T12 hook refreshed the mirror via cwd discovery"
  else fail "T12 refresh" "alpha='$(mcontent alpha)'"; fi
  # Off-repo cwd: the hook must stay exit-0 + silent (the safety property). HOME is isolated,
  # so the `$HOME/GitHub/evolve` default can't resolve; the only host-dependent default is the
  # absolute `/Users/Shared/evolve-repo`.
  reset_mirror
  hout="$(printf '{"cwd":"%s","source":"startup"}' "$SCRATCH/empty" | HOME="$TEST_HOME" EVOLVE_REPO="" bash "$HOOK")"; rc=$?
  if [ $rc -eq 0 ] && [ -z "$hout" ]; then ok "T12 hook off-repo: exit 0 + silent"
  else fail "T12 offrepo" "rc=$rc out='$hout'"; fi
  # Mutation-safety: with NO reachable checkout the mirror must stay empty. Guarded so the
  # assertion is meaningful (not vacuous) yet host-portable — if a real default checkout
  # exists, syncing from it is correct, so we only assert emptiness when none is reachable.
  if [ ! -e /Users/Shared/evolve-repo ] && [ ! -e "$TEST_HOME/GitHub/evolve" ]; then
    if [ -z "$(ls -A "$MIRROR" 2>/dev/null)" ]; then ok "T12 hook off-repo mirrored nothing (no checkout reachable)"
    else fail "T12 offrepo-mirror" "mirror unexpectedly populated: $(ls -A "$MIRROR" 2>/dev/null)"; fi
  else
    ok "T12 off-repo mirror-emptiness check skipped (a default checkout is reachable on this host)"
  fi
else
  fail "T12 hook" "hook not executable: $HOOK"
fi

echo
echo "==================================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED: ${FAILED_LABELS[*]}"
  exit 1
fi
echo "ALL ASSERTIONS PASSED"
