"""meta_rig_preflight — D-TP1: the tick EXERCISES every precondition it assumes, instead
of asserting one and hoping the rest hold.

WHY THIS EXISTS (`internal/decision-rig-throughput-2026-09-15.md` §2 class A,
`internal/assessment-silent-controls-2026-09-14.md` D-CS7). Two failures in one week, both
the same shape: a control that reported "fine" without actually looking. `check_prereqs`
ran a `claude -p` echo and printed `ok` while four background sessions sat at "login
required" for 21 hours (`internal/finding-headless-launch-is-0-for-4-2026-09-13.md`,
corrected); separately, the dispatcher's checkout sat on a feature branch for ~30 hours
while every tick read `stale-checkout` and simply stopped, because nothing besides a human
noticing was watching for THAT precondition either. A precondition that is asserted rather
than exercised is a silent control (D-CS7) sitting at the rig's front door, and the fix in
both cases is the same: read the REAL thing, not a proxy for it.

`rig_preflight()` is the aggregate: five checks, each one exercising — not assuming — its
precondition, returning `{ok, lines, may_reconcile, may_launch, login_note,
login_cache_update}` a tick prints before doing anything else. FAIL TOWARD RED. Every
check that could not be made — an unreadable `gh api` call, a `git` call that errored, a
`claude agents --json` poll that timed out — reads as `unknown`, and `unknown` counts
exactly as red for the purpose of "may the tick launch anything this turn": the opposite
direction from `tools/meta_dispatch_headless.classify_liveness`, which fails an ambiguous
STALL toward `alive` because the cost there is a duplicate session. Here the cost of a
false green is a tick that launches into a rig that cannot actually run it, so ambiguity
blocks.

TWO VERDICTS, NOT ONE (D-TP1 hold-fix, `internal/dispatch/reviews/pr-4351.md`, held and
repaired on the PR's own branch). `may_reconcile` and `may_launch` — see `rig_preflight`'s
own docstring for the split and why the login probe is opt-in per call.

NO NETWORK IN THE READ-ONLY DECIDER. `tools/meta-dispatch-eligible` documents, and a test
pins, that it never invokes `gh` — so the two checks that need it (main's CI state, the
token's access) cannot live there. This module is loaded by `tools/meta-dispatch-move`
instead, the tool that already calls `gh` for the lane-PR census and `land`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meta_dispatch_headless as mdh  # noqa: E402  (path insert must precede the import)
import meta_dispatch_integrity as mdi  # noqa: E402  (same reason)

RED = "rig: "

DEFAULT_REMOTE = "origin"
DEFAULT_BASE = "main"

LANE_STATES = ("queued", "inflight", "done")
# The dispatcher's own working set; mirrors `meta-dispatch-move.SYNC_ALLOWED_DIRTY_PREFIX`.
LANE_DIRTY_PREFIX = "internal/dispatch/"
STALE_LOCK_S = 10 * 60

# D-TP1 hold-fix (`internal/dispatch/reviews/pr-4351.md` finding 3): a login verdict is
# reusable across ticks — a login does not expire between hourly probes — so a fresh
# `LOGIN_OK` is cached this long before the next headless launch pays for another
# throwaway `claude --bg` session. Never applied to `LOGIN_EXPIRED`/`LOGIN_UNKNOWN`: only
# a POSITIVE verdict is cheap to keep believing, and re-probing after that is what makes a
# stale login visible again inside one tick.
LOGIN_CACHE_TTL_S = 6 * 60 * 60

# Mirrors `tools/meta-dispatch-move`'s own constants. Inlined rather than imported — the
# same choice `tools/meta-dispatch-eligible`'s `_stall_corroboration` already makes for the
# identical string (`"claude/meta-%s" % ident`) — because importing the WRITER module here
# would pull a 3900-line, write-capable sibling into the read-mostly checks this module
# also serves, for three literals that are a stable on-disk protocol, not an implementation
# detail either side is free to change alone.
HEADLESS_WORKTREE_SUBDIR = ".worktrees"
HEADLESS_WORKTREE_PREFIX = "meta-chip-"
HEADLESS_BRANCH_PREFIX = "claude/meta-"
WORKTREE_CHECKOUT_SLACK_S = 120
_WALK_SKIP = {"node_modules", ".git"}


class RigResult(NamedTuple):
    ok: bool                    # == may_launch (kept for callers that only care "may
                                 # anything happen"): true iff `lines` is empty.
    lines: tuple
    may_reconcile: bool         # checkout + lane-integrity only.
    may_launch: bool            # every check evaluated this call — login only counts
                                 # when `probe_login=True`.
    login_note: str             # ALWAYS present, informational — e.g. "login: ok
                                 # (cached 2h13m)" / "login: probed" / "login: not
                                 # probed this tick (…)".
    login_cache_update: dict | None  # `{"state": LOGIN_OK, "at": <iso>}` when a probe
                                      # this call just confirmed OK, for the caller to
                                      # write through `state --lane dispatch --merge`.
                                      # `None` otherwise — a bad probe is never cached.


def _default_runner(argv, cwd=None):
    """The one seam every check below calls through. Tests replace this and nothing else —
    the same discipline `tools/meta-dispatch-move._run_claude` and
    `meta_dispatch_headless._default_probe_runner` already use."""
    return subprocess.run(argv, cwd=(str(cwd) if cwd else None), capture_output=True,
                          text=True, timeout=60)


def _short(text, n=220):
    return (text or "").strip()[:n]


# ── check 1: login (delegates to the real-path probe) ────────────────────────


def _login_line(state, detail):
    if state == mdh.LOGIN_EXPIRED:
        return RED + "login expired — run /login on the laptop (%s)" % detail
    return RED + "login unknown — %s" % detail


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_desc_s(seconds: float) -> str:
    """`<N>m` / `<N>h<N>m` — the same width `tools/meta-dispatch-move._age_desc` prints
    for a claim's age, duplicated rather than imported (that module has no `.py` suffix
    and is a 3900-line write-capable sibling — the same reason the headless-worktree
    literals just above are inlined instead of imported)."""
    seconds = int(seconds)
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh%dm" % (seconds // 3600, (seconds % 3600) // 60)


def _cached_login_age_s(cached_login, now_ts):
    """Seconds since a cached `LOGIN_OK` verdict, or `None` when there is nothing usable
    to age — no cache, a non-`LOGIN_OK` state (never cached in the first place — see
    `LOGIN_CACHE_TTL_S`), an unparseable `at`, or a negative age (a clock that moved
    backward is not evidence the login is still fresh)."""
    if not isinstance(cached_login, dict) or cached_login.get("state") != mdh.LOGIN_OK:
        return None
    at = mdh._epoch(cached_login.get("at"))
    if at is None:
        return None
    age = now_ts - at
    return age if age >= 0 else None


# ── check 2: checkout ──────────────────────────────────────────────────────


def checkout_state(repo, *, remote=DEFAULT_REMOTE, branch=DEFAULT_BASE, runner=None):
    """(ok, line_or_None). On `branch`, clean, and not AHEAD of `<remote>/<branch>`.

    BEHIND is not red. A checkout behind `<remote>/<branch>` is exactly what step 0b —
    `meta-dispatch-move sync`, the fast-forward — fixes in the SAME tick, and `sync`
    carries its own preconditions and refuses safely when they fail; downstream, the
    eligibility helper's `stale-checkout` block still dispatches nothing if the
    fast-forward did not happen. Reading "behind" as red here put the fix behind the
    gate that needed it: measured 2026-09-24, 18 consecutive runner ticks (20:01 PDT
    09-23 → 14:01 PDT 09-24) stopped at step 0a with "3 commit(s) behind origin/main —
    run sync" while the runner's own next step WAS sync. Same class as the two-dirs
    rule in `rig_preflight`: what a later step fixes must not be what forbids that step.
    AHEAD (local commits the remote does not have) is the human's half-made work this
    check exists for — a fast-forward cannot run over it — and stays red.

    Deliberately NOT `tools/meta-dispatch-eligible`'s `base_distance`/`stale-checkout`:
    that check answers "is the QUEUE ORDER current" (a commit-count distance only, read
    from whatever branch the checkout happens to be on) and stays exactly as it is. This
    answers a broader front-door question — is this checkout even ON the branch the lane
    expects, and clean — which is what let a checkout sit on a feature branch for ~30
    hours while `stale-checkout` (which never checks the branch NAME) kept reporting a
    distance instead of the actual problem.
    """
    run = runner or _default_runner
    if repo is None:
        return False, RED + "checkout unknown — no git checkout to inspect"
    cur = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    if cur.returncode != 0:
        return False, RED + ("checkout unknown — could not read the current branch: %s"
                             % _short(cur.stderr or cur.stdout))
    current = cur.stdout.strip()
    remedy = "run `python3 tools/meta-dispatch-move sync`"
    if not current or current != branch:
        return False, RED + ("checkout is on %r, not %r — %s"
                             % (current or "<detached>", branch, remedy))
    dirty = run(["git", "status", "--porcelain"], repo)
    if dirty.returncode != 0:
        return False, RED + ("checkout unknown — could not read working-tree status: %s"
                             % _short(dirty.stderr or dirty.stdout))
    # Scoped to paths OUTSIDE the lane dir, for the same reason `meta-dispatch-move
    # sync` scopes its own precondition (`_staged_paths_outside_lane`): the dispatcher's
    # `queued/ -> inflight/ -> done/` moves are STAGED in this checkout from the moment
    # `launch` runs until step 6b lands them, so an unscoped "porcelain must be empty"
    # reads red on every tick that did its job — and, because a red here clears
    # `may_reconcile`, the tick that would land those moves never runs. Measured
    # 2026-09-23: 13 consecutive ticks stopped at step 0a on the lane's own staged
    # moves (`reviews/pr-4351.md` finding 2 named the shape before the merge). A human's
    # half-made commit lives outside `internal/dispatch/` by definition and still reds.
    # Untracked (`??`) files are not counted either — `sync`'s `_dirty_paths_outside_lane`
    # says why ("untracked files are deliberately not counted"): a fast-forward carries
    # them across untouched, and a stray marker (2026-09-23: a `.landed-by-pm-land` file
    # from 09-11) must not be what stops the lane. TRACKED modifications outside the
    # lane dir are the human's half-made commit this check exists for, and still red.
    outside = [ln[3:] for ln in dirty.stdout.splitlines()
               if ln.strip() and not ln.startswith("??")
               and not ln[3:].startswith(LANE_DIRTY_PREFIX)]
    if outside:
        return False, RED + ("checkout on %r is dirty outside the lane dir (%s) — %s"
                             % (branch, _short(", ".join(outside[:3])), remedy))
    ref = "%s/%s" % (remote, branch)
    count = run(["git", "rev-list", "--count", "%s..HEAD" % ref], repo)
    if count.returncode != 0:
        return False, RED + ("checkout unknown — could not count %s..HEAD: %s"
                             % (ref, _short(count.stderr or count.stdout)))
    n_txt = count.stdout.strip()
    if not n_txt.isdigit():
        return False, RED + ("checkout unknown — git returned a non-numeric distance from "
                             "%s" % ref)
    ahead = int(n_txt)
    if ahead > 0:
        return False, RED + ("checkout on %r is %d commit(s) AHEAD of %s (unpushed local "
                             "commits) — a fast-forward cannot run over them; push them "
                             "or move them to a branch" % (branch, ahead, ref))
    # Behind is not checked here on purpose — see the docstring: `sync` (step 0b) is the
    # remedy and runs next; `stale-checkout` downstream holds dispatch if it did not.
    return True, None


# ── check 3: main's last completed required-check run ────────────────────────

# The `ci` workflow's own `name:` (`.github/workflows/ci.yml` line 1). Measured live
# 2026-09-21 against this repo: THREE workflows (`ci`, `publish-public`,
# `secret-history-scan`) all complete on the same push to `main`, and `gh run list
# --branch main` (no event/workflow filter) returned only `ci` runs from 2026-09-07 —
# two weeks stale — while `gh api .../actions/runs?event=push&branch=main` returned the
# same day's runs. Both facts are why this reads `event=push&branch=<branch>` off the
# Actions API directly rather than `gh run list --branch`, and filters to this one name.
CI_WORKFLOW_NAME = "ci"


def _load_quarantine_entries(quarantine_path):
    """(entries, note). `entries` is `{check_name: QuarantineEntry}` from
    `tools/required_check_health.load_quarantine`; `note` is `""` normally, or a
    parenthetical to append to a red line when the quarantine could not be consulted at
    all (the module is absent on this checkout, or the file does not parse) — the
    quarantine then reads as EMPTY rather than crashing the check, and the red line says
    so rather than silently under- or over-blocking on a state nobody could see."""
    try:
        import required_check_health as rch
    except ImportError:
        return {}, (" (tools/required_check_health is not on this checkout — quarantine "
                    "treated as empty)")
    try:
        return rch.load_quarantine(Path(quarantine_path)), ""
    except rch.QuarantineFormatError as e:
        return {}, (" (tools/required-check-quarantine.txt is malformed: %s — quarantine "
                    "treated as empty)" % e)


def main_ci_state(repo, *, branch=DEFAULT_BASE, workflow_name=CI_WORKFLOW_NAME, runner=None,
                  quarantine_path=None, today=None):
    """(ok, line_or_None). Judges the newest completed PUSH-to-`branch` run of
    `workflow_name` by its JOBS, never by the run's own top-level `conclusion` and never
    by any other workflow (`internal/dispatch/reviews/pr-4351.md` finding 2). A job that
    failed but carries an unexpired row in `tools/required-check-quarantine.txt` (D-CS9)
    does not redden the rig — this is what stops the known-flaky Linux e2e job (43/100
    push-to-main failures, `tools/required-check-health-report.json`) from stopping the
    lane on ~4 pushes in 10. `gh` unreadable, no JSON, or no completed run yet all read
    `unknown` rather than either color — a run that has not finished, or that this check
    could not read, is not evidence of red.
    """
    run = runner or _default_runner
    if repo is None:
        return False, RED + "main CI unknown — no git checkout to run `gh` from"
    r = run(["gh", "api",
             "repos/{owner}/{repo}/actions/runs?event=push&branch=%s&status=completed"
             "&per_page=20" % branch], repo)
    if r.returncode != 0:
        return False, RED + ("main CI unknown — could not list %s's runs: %s"
                             % (branch, _short(r.stderr or r.stdout)))
    try:
        payload = json.loads(r.stdout or "{}")
    except ValueError as e:
        return False, RED + ("main CI unknown — run list did not return JSON: %s" % e)
    rows = [x for x in (payload.get("workflow_runs") or []) if isinstance(x, dict)]
    rows = [x for x in rows if str(x.get("name") or "") == workflow_name]
    if not rows:
        return False, RED + ("main CI unknown — no completed %r run found for %s"
                             % (workflow_name, branch))
    rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    latest = rows[0]
    sha = str(latest.get("head_sha") or "?")[:12]
    conclusion = str(latest.get("conclusion") or "").lower()
    if conclusion in ("success", "skipped", "neutral"):
        return True, None

    run_id = latest.get("id")
    if not run_id:
        return False, RED + ("main is red since %s — %s run had no id to inspect its jobs"
                             % (sha, workflow_name))
    jr = run(["gh", "api",
             "repos/{owner}/{repo}/actions/runs/%s/jobs?per_page=100" % run_id], repo)
    if jr.returncode != 0:
        return False, RED + ("main CI unknown — could not list run %s's jobs: %s"
                             % (run_id, _short(jr.stderr or jr.stdout)))
    try:
        jobs = json.loads(jr.stdout or "{}").get("jobs") or []
    except ValueError as e:
        return False, RED + ("main CI unknown — job list did not return JSON: %s" % e)

    quarantine, note = _load_quarantine_entries(
        quarantine_path or (Path(repo) / "tools" / "required-check-quarantine.txt"))
    today = today or date.today()
    failed = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("conclusion") or "").lower() != "failure":
            continue
        name = job.get("name") or "?"
        entry = quarantine.get(name)
        if entry is not None and entry.expiry >= today:
            continue  # quarantined and not expired — not a red
        failed.append(name)
    if not failed:
        return True, None
    return False, RED + ("main is red since %s — required check(s) failed: %s%s"
                         % (sha, ", ".join(sorted(set(failed))), note))


# ── check 4: the PM token's access ─────────────────────────────────────────────

# EXERCISE, DO NOT ASSERT (`internal/dispatch/reviews/pr-4351.md` finding 1). The old
# check grepped `gh auth status` for `checks:read`/`actions:read` on its `Token scopes:`
# line — those are fine-grained-PAT *permission* names, not OAuth scopes, and `gh auth
# status` never prints them for a fine-grained token (confirmed live 2026-09-21 against
# this operator's own `gh` login: `Token scopes: 'gist', 'read:org', 'repo', 'workflow'`,
# the classic OAuth set, no `checks:read`/`actions:read` anywhere) — so that check was red
# or `unknown` on every tick, forever, and it inspected the wrong credential besides (the
# operator's `gh` login, not the PM token D-TP6 granted). The repair: two real reads with
# the credential the dispatcher actually calls `gh` with, judged by HTTP status. RULINGS
# 2026-09-16: `/actions/*` is readable, `/check-runs` is not — neither read here touches
# `/check-runs`.
TOKEN_READS = (
    ("repos/{owner}/{repo}/actions/runs?per_page=1",
     "read the lane's own CI run history"),
    ("repos/{owner}/{repo}/actions/workflows",
     "read workflow definitions (needs `workflow` visibility)"),
)

_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})")


def _gh_api_status(run, path, repo):
    """(status:int|None, detail). `gh api -i <path>` prints the HTTP status line to
    stdout REGARDLESS of `gh`'s own exit code — a 403/404 is a complete, normal response
    that `gh` reports as a CLI failure, not a transport failure, and parsing the status
    off stdout (rather than trusting `returncode`) is what lets a real permission problem
    read as a NAMED status rather than `unknown`. Captured live 2026-09-21 against this
    repo: a 200 prints `HTTP/2.0 200 OK` on the first line; a 404 prints `HTTP/2.0 404
    Not Found` on stdout AND exits 1. No status line at all — no `gh` binary, no network,
    not authenticated — is the genuine `unknown` case."""
    r = run(["gh", "api", "-i", path], repo)
    m = _HTTP_STATUS_RE.search(r.stdout or "")
    if m:
        return int(m.group(1)), _short(r.stderr or r.stdout)
    return None, _short(r.stderr or r.stdout or ("gh api exited %d" % r.returncode))


def token_access(*, runner=None, repo=None):
    """(ok, lines[]). One named line per failed read, with the status code — never a
    bundled "the token is bad", because the remedy differs per read and the operator
    should not have to re-derive which."""
    run = runner or _default_runner
    lines = []
    for path, purpose in TOKEN_READS:
        status, detail = _gh_api_status(run, path, repo)
        if status is None:
            lines.append(RED + ("token access unknown — could not read %s (%s): %s"
                                % (path, purpose, detail)))
        elif status != 200:
            lines.append(RED + ("token cannot %s — GET %s returned %d"
                                % (purpose, path, status)))
    return (not lines), lines


# ── check 5: the lane itself ──────────────────────────────────────────────────


_FM_ID_RE = re.compile(r"^id\s*:\s*(.+?)\s*$")


def _entry_id(fm_lines):
    for line in fm_lines:
        m = _FM_ID_RE.match(line.strip())
        if m:
            return m.group(1).strip().strip("\"'")
    return None


def lane_state(root, *, now_ts=None, stale_lock_s=STALE_LOCK_S):
    """(ok, lines[]). No id in two lane dirs, no INVALID entry, no `*.lock` file at the
    lane root older than `stale_lock_s`.

    Deliberately its OWN small scan rather than a borrow of `tools/meta-dispatch-
    eligible.lane_conflicts` — that function answers "can THIS tick's dispatch decision be
    trusted", computed alongside the cap, back-pressure and headless-liveness machinery a
    front-door health check has no business paying for; this answers the narrower "is the
    lane's on-disk shape sane at all", cheaply, with no `gh` and no dependency on the rest
    of `evaluate()`'s pipeline.
    """
    lines = []
    root = Path(root)
    now_ts = now_ts if now_ts is not None else _now_ts()
    seen: dict = {}
    for state in LANE_STATES:
        d = root / state
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                lines.append(RED + ("lane entry %s is INVALID — unreadable: %s" % (path, e)))
                continue
            try:
                split = mdi.split_front_matter(text)
            except mdi.FrontMatterError as e:
                lines.append(RED + ("lane entry %s is INVALID — %s" % (path, e)))
                continue
            try:
                integ = mdi.check(text)
            except mdi.FrontMatterError as e:
                lines.append(RED + ("lane entry %s is INVALID — %s" % (path, e)))
                continue
            if not integ.ok:
                lines.append(RED + ("lane entry %s is INVALID — %s" % (path, integ.reason)))
                continue
            ident = _entry_id(split.fm_lines)
            if ident is None:
                lines.append(RED + ("lane entry %s is INVALID — no front-matter id:" % path))
                continue
            seen.setdefault(ident, []).append(path)
    for ident in sorted(seen):
        paths = seen[ident]
        if len(paths) > 1:
            lines.append(RED + ("lane holds id %r in %d dir(s): %s"
                                % (ident, len(paths), ", ".join(str(p) for p in paths))))
    if root.is_dir():
        for lock in sorted(root.glob("*.lock")):
            try:
                age = now_ts - lock.stat().st_mtime
            except OSError:
                continue
            if age > stale_lock_s:
                lines.append(RED + ("lane lock %s is stale (%ds old) — remove it by hand"
                                    % (lock, int(age))))
    return (not lines), lines


def _now_ts():
    return time.time()


# ── the aggregate ──────────────────────────────────────────────────────────


def rig_preflight(repo, root, *, remote=DEFAULT_REMOTE, branch=DEFAULT_BASE,
                  login_runner=None, git_runner=None, gh_runner=None,
                  probe_login=False, cached_login=None, now_ts=None) -> RigResult:
    """The aggregate — every red line, plus the two verdicts and the login note,
    computed ONCE. Never more than one call per tick pays for a login probe.

    **`probe_login` (D-TP1 hold-fix, finding 3).** Default `False`: login is NOT probed
    and NOT evaluated as a red/unknown line — a throwaway `claude --bg` session every
    tick (24/day) was the finding. `login_note` instead reports whatever a fresh CACHED
    `LOGIN_OK` (`cached_login`, read by the caller) says, purely informational, and never
    blocks `may_launch`: if a stale/absent cache blocked launch here, at step 0a, no tick
    could ever reach the one place (`probe_login=True`, step 4, once, before the first
    headless launch) that refreshes it — a probe gated on its own absence never fires. A
    fresh `LOGIN_OK` from THAT call is cached for `LOGIN_CACHE_TTL_S` so later ticks can
    skip probing too.

    **`may_reconcile` / `may_launch` (finding 2).** Checkout and lane-integrity clear
    BOTH — the lane's own on-disk state cannot be trusted, so nothing past step 0a may
    run. Login (only when `probe_login=True`), the PM token, and main's CI clear
    `may_launch` ONLY — stopping the fast-forward/repair-queue/reconcile on a red main
    (finding 2b) is how a red main additionally turns into a stale lane.

    `unknown` counts exactly as red for both verdicts — a check that could not be made is
    not evidence anything is fine.
    """
    now_ts = now_ts if now_ts is not None else _now_ts()
    lines = []
    reconcile_bad = False
    launch_bad = False
    login_cache_update = None

    if probe_login:
        lstate, ldetail = mdh.probe_background_login(runner=login_runner)
        if lstate == mdh.LOGIN_OK:
            login_note = "login: probed"
            login_cache_update = {"state": mdh.LOGIN_OK, "at": _iso_utc(now_ts)}
        else:
            lines.append(_login_line(lstate, ldetail))
            launch_bad = True
            login_note = "login: %s" % lstate
    else:
        age_s = _cached_login_age_s(cached_login, now_ts)
        if age_s is not None and age_s <= LOGIN_CACHE_TTL_S:
            login_note = "login: ok (cached %s)" % _age_desc_s(age_s)
        else:
            login_note = ("login: not probed this tick (%s)"
                          % ("cache expired" if cached_login else "no cached verdict"))

    ok, line = checkout_state(repo, remote=remote, branch=branch, runner=git_runner)
    if not ok and line:
        lines.append(line)
        reconcile_bad = True
        launch_bad = True

    ok, line = main_ci_state(repo, branch=branch, runner=gh_runner)
    if not ok and line:
        lines.append(line)
        launch_bad = True

    ok, tok_lines = token_access(runner=gh_runner, repo=repo)
    if tok_lines:
        lines.extend(tok_lines)
        launch_bad = True

    ok, lane_lines = lane_state(root, now_ts=now_ts)
    if lane_lines:
        lines.extend(lane_lines)
        launch_bad = True
        # An id in two dirs is the shape a merged chip leaves behind until the tick's own
        # step 1 runs `done <id>` — it is what reconciling FIXES, so it must not also be
        # what forbids reconciling (the 2026-09-23 deadlock: five merged chips held the
        # cap for 13 ticks). INVALID entries and stale locks are not self-clearing and
        # still block both.
        if any(not ln.startswith(RED + "lane holds id") for ln in lane_lines):
            reconcile_bad = True

    return RigResult(ok=(not lines), lines=tuple(lines),
                     may_reconcile=(not reconcile_bad), may_launch=(not launch_bad),
                     login_note=login_note, login_cache_update=login_cache_update)


# ── corroborated-stall recovery (D-TP1 step 4) ────────────────────────────────


def _newest_mtime_under(path):
    """Newest mtime under `path`, or None when it cannot be walked at all. `node_modules`
    and `.git` are skipped for the same reason `tools/meta-dispatch-eligible`'s copy is:
    a chip's worktree carries the repo's whole dependency tree and walking it costs real
    seconds for a directory no chip writes into."""
    try:
        if not Path(path).is_dir():
            return None
    except OSError:
        return None
    newest = 0.0
    seen = False
    for dirpath, dirnames, filenames in os.walk(str(path)):
        dirnames[:] = [d for d in dirnames if d not in _WALK_SKIP]
        for name in filenames:
            try:
                m = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            seen = True
            if m > newest:
                newest = m
    return newest if seen else None


LANE_OWN_HEADS = ("lane/state",)
LANE_OWN_HEAD_PREFIXES = ("pm/",)


def _is_lane_own_head(head_ref: str) -> bool:
    """True for a PR head the lane itself produces — never evidence a chip worked."""
    return head_ref in LANE_OWN_HEADS or any(head_ref.startswith(p)
                                             for p in LANE_OWN_HEAD_PREFIXES)


def four_way_corroboration(entry: dict, repo, *, git_runner=None, gh_runner=None,
                           worktree_subdir=HEADLESS_WORKTREE_SUBDIR,
                           worktree_prefix=HEADLESS_WORKTREE_PREFIX,
                           branch_prefix=HEADLESS_BRANCH_PREFIX,
                           checkout_slack_s=WORKTREE_CHECKOUT_SLACK_S,
                           mtime_fn=None):
    """(all_absent, findings, detail) for one inflight headless entry past `BRANCH_GRACE_S`.

    Four independent, all-free sources — `entry.get("branch")`, a PUSHED
    `claude/meta-<id>` ref (remote-tracking, never the local head the launcher itself
    creates), an OPEN PR from a non-lane head whose body names the id, a worktree written
    after its own checkout. `findings[k] is True` means EVIDENCE OF WORK was found for source
    `k` — the chip is not actually dead — and `all_absent` (the sole license to auto-
    abandon, per the guardrail "abandon only on four-way corroboration") is true only when
    every one of the four reads False.

    FAILS TOWARD "EVIDENCE FOUND", not toward abandoning: `repo is None`, a `git`/`gh` call
    that errors, or an unparseable `started` stamp all set their source's finding to
    `True`. A corroboration that could not run must not license the harsher verdict — the
    same rule `tools/meta-dispatch-eligible._stall_corroboration` already enforces on
    itself, applied here to the action that actually deletes the entry.
    """
    grun = git_runner or _default_runner
    ghrun = gh_runner or _default_runner
    ident = entry.get("id") or ""
    findings = {"branch": bool(entry.get("branch"))}

    if repo is None:
        findings["ref"] = True
        findings["pr"] = True
        findings["worktree"] = True
        return False, findings, "no repo to corroborate from"

    # `ref` — a PUSHED branch, read from the REMOTE-TRACKING ref. Not `refs/heads/`:
    # the headless launcher itself creates the local `claude/meta-<id>` branch when it
    # cuts the worktree, so a local head exists from the moment of launch and proves
    # nothing about work. Measured 2026-09-24: `recover-stalls` answered "UNKNOWN, no
    # action (corroborated by: ref, pr)" for a chip that had died before its first push
    # 24 h earlier — both "sources" were the rig's own footprints (this one, and the
    # lane/state PR below), so the sweep could never act on anything this rig launched.
    ref = "%s%s" % (branch_prefix, ident)
    r = grun(["git", "for-each-ref", "--format=%(objectname)",
              "refs/remotes/origin/%s" % ref], repo)
    findings["ref"] = True if r.returncode != 0 else bool(r.stdout.strip())

    # `pr` — an open PR naming the id, EXCLUDING the lane's own PRs: `lane/state` (its
    # body and diff name every in-flight id by construction) and `pm/*` (PM bookkeeping
    # PRs discuss chips by id). Only a PR from some other head is evidence the chip did
    # something.
    r = ghrun(["gh", "pr", "list", "--state", "open", "--limit", "100",
              "--json", "number,body,headRefName"], repo)
    if r.returncode != 0:
        findings["pr"] = True
    else:
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError:
            findings["pr"] = True
        else:
            findings["pr"] = bool(ident) and any(
                isinstance(x, dict) and ident in str(x.get("body") or "")
                and not _is_lane_own_head(str(x.get("headRefName") or ""))
                for x in (rows if isinstance(rows, list) else []))

    wt = Path(repo) / worktree_subdir / ("%s%s" % (worktree_prefix, ident))
    newest = (mtime_fn or _newest_mtime_under)(wt)
    if newest is None:
        findings["worktree"] = False
    else:
        started = mdh._epoch(entry.get("started"))
        findings["worktree"] = True if started is None else (
            newest > started + checkout_slack_s)

    all_absent = not any(findings.values())
    if all_absent:
        detail = "no branch, no %s ref, no open PR naming %r, no worktree write" % (
            ref, ident)
    else:
        detail = "corroborated by: %s" % ", ".join(k for k, v in findings.items() if v)
    return all_absent, findings, detail


_SUPERSEDES_RE_TMPL = r"^supersedes\s*:\s*%s\s*$"


def find_existing_successor(root, original_id):
    """A lane entry (any dir) that already carries `supersedes: <original_id>`, if one
    exists — the idempotency check: a `recover-stalls` run that finds the original already
    superseded takes no further action rather than minting a second successor."""
    pat = re.compile(_SUPERSEDES_RE_TMPL % re.escape(original_id), re.MULTILINE)
    root = Path(root)
    for state in LANE_STATES:
        d = root / state
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if pat.search(text):
                return path
    return None
