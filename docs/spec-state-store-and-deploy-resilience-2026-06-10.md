# Spec: State-store concurrency & deploy resilience (Phase 7)

**Date:** 2026-06-10 (rev 2, post design-review)
**Roadmap:** [roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md) Phase 7, rows 7.1–7.2
**Status:** decided — 7.1 Phase A (locking) + 7.2 (release pipeline) implement now; 7.1 Phases B–D are sequenced follow-ups
**Review:** adversarial design review 2026-06-10 (16 findings) folded in; the
material ones are called out inline as *(review #N)*.

This doc covers the two structural reliability gaps the file-based design
papers over with repair daemons, plus the deploy model's missing undo:

- **7.1** — the Signal and Proposal stores have zero cross-process
  coordination. Two daemons observing the same signature concurrently both
  miss in `find_active_by_signature` and both create; read-modify-write
  bumps lose updates; cross-user renames need a deliberately
  sticky-bit-free `0o777` dir contract backed by an hourly perms-repair
  daemon.
- **7.2** — the repo-puller fast-forwards every daemon's code to
  `origin/main` within 15 minutes of any merge, with no gate, no version
  pointer, and no undo.

---

## Part 1 — Store concurrency (7.1)

### 1.1 Audited current state (2026-06-10, corrected per review)

- `packages/analyzer/signals/store.py` and `packages/analyzer/arbiter/store.py`
  have **no locking of any kind**. Atomic temp-file+rename writes are
  correct per-file; the gap is coordination across find-or-create and
  read-modify-write sequences.
- `signals.store.observe()` races: `find_active_by_signature` linearly
  scans `firing/` + `snoozed/` (store.py:368-375), then creates on miss.
  Two concurrent observers of one signature → duplicate Signals → the
  notifier pages twice and sweep-resolve leaves an orphan.
- The bump path (existing-signal branch of `observe()`) is read-modify-write
  with no guard: concurrent bumps lose `observation_count` increments and
  `details` merges. The same find→mutate→write shape exists in **every
  transition endpoint** (routes_signals, routes_arbiter, evo
  `action_signal`/`action_proposal` MCP tools) — the read half happens in
  the caller, outside any store function *(review #6)*.
- `move_proposal` has a **resurrection bug** under racing movers: it
  stages the updated content at the *source* path before the rename, so
  a second mover whose source was already moved away re-creates the
  source file and then renames it to a *different* destination — the
  proposal ends up in two subdirs at once. Observed shape: dismiss vs
  approve racing on the same proposal.
- `signature_index.json` appears in the spec layout
  (spec-alerts-signal-store-2026-05-07.md §7) **but was never
  implemented** — nothing reads or writes it. (`retention.py:19-20`
  claims it is "rebuilt from active dirs on startup"; that comment is
  stale and gets fixed in Phase A *(review #16)*.)
- Proposal dirs are deliberately `0o777` without sticky bit
  (`POD_PROPOSALS_MODE`, deploy.py) so cross-user `os.replace` works
  (evolve / bot users / evo all legitimately rewrite each other's files),
  with the hourly `pod_perms_drift_monitor` daemon repairing the
  resulting ownership races.
- **The "store helpers are the only sanctioned access path" claim is
  aspirational, not actual.** A census (2026-06-10) found ~25 direct
  directory consumers outside the store modules: `pod_rollup.py`,
  `evo/handlers/{alerts,audit,health}.py`, `web/routes_cascade.py`,
  `cost_opt_tiles.py`, `cross_bot_summary.py`, `session_surface.py`,
  `substrate_audit_state.py`, `app_audit_investigation.py`,
  `signal_subscriber_runner.py` (1 Hz directory watch), `retire.py`,
  `status.py`, `audit_pod_acls.py`. Census corrections from review
  *(review #8)*: `web/server.py`'s `_move_proposal` (~:4926) is **dead
  code** (zero callers) — Phase B deletes it rather than migrating it;
  and the census *missed* a live bot-user writer: the legacy L1 apply
  pipeline (`apply.py` reading `proposals/approved/` with
  rename/unlink/quarantine moves, sudoers grants in setup_wizard.py,
  and the hourly `stuck_proposal_monitor` scanning it). Its subdirs are
  disjoint from the store's four, so Phase A is unaffected, but Phases
  C–D must subsume or retire that pipeline before the `0o777` contract
  can be torn down.

### 1.2 Writer/reader matrix (drives the whole decision)

| Store | Writers (OS users) | Readers |
|---|---|---|
| `signals/` | `evolve` (admin-ui, heal, audit, verify, puller, subscriber — many *processes*, one user), `evo` (MCP `action.signal.*`, via inherited ACL), **bot users transitively** (`write_proposal` → `_maintain_signal_backrefs` → `attach_proposal`) | same + ~12 direct dir readers |
| `proposals/` | `evolve` (generators, arbiter, verify), **bot users** (per-bot analyzer daemons / appliers + the legacy L1 `apply.py` pipeline), `evo` (MCP `action.proposal.*`) | same + admin UI, evo pod-state tools |

The races are real in both dimensions: many evolve-user *processes*
(same-user concurrency), and cross-user writes (the perms mess). Note
the bot-user row for `signals/`: bot users take the **signals lock**
too, via the backref path — this drives the lock-file pre-creation
requirement in §1.4 *(review #7)*.

### 1.3 Decision

**SQLite (WAL, single-writer-user) is the end-state. It is not shippable
in one step, because two premises of "the swap is contained" are false
today.** We phase it:

| Phase | What | Risk | Ships |
|---|---|---|---|
| **A** | Cross-process `flock` serialization inside both stores + CAS-style transition semantics + concurrent-`observe()` race test | low (no format change, no consumer change) | **now (this PR series)** |
| **B** | Sanction the access path: migrate the ~25 direct dir consumers onto store APIs (and delete the dead `server.py::_move_proposal`); add a CI guard forbidding store-dir literals outside the store modules | low, wide | next |
| **C** | Funnel non-evolve writers through the admin-daemon socket (evo MCP actions, bot-user proposal writers, the legacy L1 apply pipeline) | medium | after B |
| **D** | SQLite backend behind `EVOLVE_STORE_BACKEND` flag (default `files`); flip default once 7.2 canary is live; delete files backend, drop the `0o777`/no-sticky/evo-ACL store contracts | highest | after 7.2 is deployed |

#### Why not straight to SQLite (the roadmap's "right" option)?

1. **The swap is not contained.** ~25 consumers read the directories
   directly. Swapping the backend first would silently break them
   (they'd read an empty/stale dir while writers go to the db). Phase B
   is a precondition, not a nicety.
2. **Cross-user SQLite trades one perms mess for a worse one.** With
   bot users + evo + evolve all writing, the db (and its `-wal`/`-shm`
   sidecars) must be world-writable in a world-writable dir. That
   *concentrates* blast radius: today a corrupt/hostile write damages one
   JSON record (`load_proposal_file` → `None`, skipped); a world-writable
   SQLite file makes any bot process able to corrupt the entire store.
   The right shape is **single-writer-user SQLite** (evolve-owned,
   `0o644`): evo and bot-user writes route through the admin-daemon
   socket that already exists post-evo-account-separation. That's Phase
   C, and it's what finally lets the evo write ACL and the `0o777`
   contract die — see §1.6.
3. **Deploy-safety ordering.** A storage-backend swap on the
   highest-blast-radius daemons should ride the canary/rollback pipeline,
   which doesn't exist until 7.2 ships. Doing D before 7.2 means the
   riskiest change of the year deploys through the least-protected
   pipeline. (Exit criterion stays honest: the *race* is fixed in Phase
   A; the *perms contract* dies in Phase D.)

#### Why flock now is not a throwaway quick-win

Phase A is not undone by Phase D — it is *subsumed*. The files backend
remains the default through Phases B–D (flag rollout), so it must be
correct for that entire window; the lock is what makes it correct. When
the files backend is deleted, the lock goes with it. No Phase-D work is
re-done because Phase A existed.

#### Deviation from the roadmap row: no `signature_index.json`

The roadmap's cheap option is "flock + signature index". We take the
flock and **skip the index**: at observed volumes (low hundreds of
active signals, 90-day archive retention) the linear scan costs ~1 ms,
while an index file is one more invariant to hold under the same lock —
and Phase D gets real indexing for free. If active-signal volume ever
grows 100×, the answer is to accelerate Phase D, not to build a JSON
index.

### 1.4 Phase A design (implement now)

**Lock primitive.** One advisory lock file per store:

```
{shared_dir}/signals/.store.lock
{shared_dir}/proposals/.store.lock
```

- Acquire: `os.open(path, O_RDONLY | O_CREAT, 0o666)` then
  `fcntl.flock(fd, LOCK_EX)`. BSD `flock` permits `LOCK_EX` on a
  read-only fd (verified empirically on macOS during review, including
  cross-process exclusion and a 0444 file) — so locking an *existing*
  lock file needs only read access.
- **Pre-creation by `ensure_pod_perms` is load-bearing, not
  belt-and-suspenders** *(review #7)*: bot users take the signals lock
  via the backref path but cannot create files in evolve-owned `0o755`
  `signals/`. `ensure_pod_perms` pre-creates both lock files
  evolve-owned `0o666` (same pattern as `APPLY_LOCK_TEMPLATE`); lazy
  `O_CREAT` is a fallback only for writers with dir-write (evolve, evo,
  and anyone in `proposals/`). Best-effort `chmod 0o666` after a lazy
  create (umask may clamp); failure non-fatal since existing-file
  locking only needs read.
- **The lock file must never be repaired by replace/rename** — a
  perms-drift fix must `chmod`/`chown` in place, or mutual exclusion
  silently splits across two inodes. Stated in the `_PermCheck` for it.
- Failure behavior *(review #7)*: `StoreLockTimeout` subclasses
  `OSError`. Direct store mutations **fail closed** (raise — better a
  loud failed observe than silent corruption); the best-effort backref
  path (`_maintain_signal_backrefs`) already swallows exceptions and
  therefore fails open by existing design.
- No-timeout `flock` is unacceptable in daemons → acquire with
  `LOCK_NB` in a retry loop (short sleep), deadline 10 s, then raise
  `StoreLockTimeout`.
- **Reentrant per thread**: a `threading.local` depth counter keyed by
  lock path, so `sweep_resolve` → `apply_transition` (both locked) nests
  instead of self-deadlocking.
- **Lock ordering invariant:** proposals → signals only (verified: the
  only cross-store calls are `arbiter/store.py` → signals backrefs,
  `auto_resolve.py`, `still_motivated.py`; zero signals → arbiter
  imports). `signals.store` must never call into `arbiter.store`.
  Documented in both modules.
- Crash safety: `flock` evaporates with the fd on process death — no
  stale-lock recovery needed (the reason to prefer it over
  lockfile-existence schemes).

**Closing the whole RMW window, not just the write half** *(review #6)*:

- `observe()` holds the lock across the entire find-or-create (including
  the bump branch).
- `apply_transition` **re-loads the Signal by id inside the lock** and
  applies the transition to the freshly-loaded record (the mutation
  inputs — `to_state`, `actor`, `reason`, `snoozed_until` — are all
  arguments, so the caller's possibly-stale object is only identity).
  If the re-loaded state can't legally make the transition, the existing
  `IllegalTransitionError` fires — a lost race is detected, not
  silently clobbered. The caller's object is refreshed in place.
- `move_proposal` verifies **inside the lock** that the source file
  still exists before staging its write; a missing source raises
  `OSError` instead of resurrecting the file (fixes the two-subdirs
  corruption above).
- Both stores export a public `locked(shared_dir)` context manager for
  callers whose check-then-write spans store calls. Phase A migrates
  the live racy writers: the admin-UI transition endpoints
  (routes_signals / routes_arbiter find→transition→move sequences), the
  evo MCP action tools, and the runner's `find_open_duplicate` →
  refresh-or-write sequence.

**What takes the lock**: `observe()`, `apply_transition`,
`sweep_resolve`, `wake_due_snoozes`, `record_delivery`,
`attach_proposal`/`detach_proposal`; `write_proposal` (the coalesce
branch is find-then-merge), `move_proposal`, `sweep_resolve_proposals`,
`find_open_duplicate`. Plain readers stay lock-free — per-file atomic
writes already guarantee they never see torn JSON. `retention.py` and
`backfill.py` deliberately do **not** take the lock *(review #16)*:
retention's mtime-based pruning cannot overlap the 1 h reopen window,
backfill is a one-shot manual tool, and the worst interleaving
(resurrecting a being-pruned dismissed bump) is benign and self-heals
on the next retention pass.

**Contention bound** *(review #9)*: the archived-dir scans inside
`observe()` (dismissed lookup; resolved-reopen lookup) are the only
non-trivial work under the lock. Phase A bounds them with a
stat-mtime prefilter (skip files older than the retention window — for
dismissed entries every live one gets mtime-bumped on each observe) and
newest-first early-exit on match. If burst contention ever approaches
the 10 s deadline in practice, that is a signal to accelerate Phase D,
not to grow the files backend.

**Proof artifact** — `test_signals_store_concurrency.py`:

- N=8 **processes** via an explicit `multiprocessing.get_context("spawn")`
  (so Linux CI exercises the same thing macOS does *(review #10)*),
  module-level worker fn, start-`Barrier` passed through `Process` args,
  generous join timeouts for 2-core runners.
- All call `observe()` with the same signature against a tmp shared_dir.
  Assert **exactly one** Signal file across `firing/` + `snoozed/`, and
  — the stronger claim — `observation_count == 8` (schema default 1 + 7
  bumps): that proves the RMW bump path serialized, not merely that
  creation deduped.
- Proposals variant: concurrent `write_proposal` with one
  `coalesce_key` and **distinct `trigger_observations` per writer**
  (identical/empty triggers no-op in `_coalesce_into_parent` by design
  *(review #10)*) → exactly one parent with N−1 `sub_findings`.
- Transition-race test: two processes racing `apply_transition` /
  `move_proposal` on one record → exactly one file in exactly one
  subdir afterward.

### 1.5 Phases B–D sketch (follow-up sessions; recorded here so they're scoped, not re-derived)

- **B**: each direct consumer moves to `iter_active` / `iter_signals` /
  `iter_proposals` / `find_*` / `move_proposal`; delete dead
  `server.py::_move_proposal`. The subscriber's 1 Hz directory watch
  becomes a store-API poll (`iter_active(state="firing")` with an
  mtime/rowid cursor — API designed so the Phase D backend can answer
  it cheaply). CI guard: a test greps for `signals" / "firing`-class
  literals and proposal-subdir paths outside `signals/store.py`,
  `arbiter/store.py`, `signals/retention.py`, `signals/backfill.py`
  (same pattern as the provider-literals guard).
- **C**: census bot-user proposal writers (including the legacy L1
  `apply.py` / `proposals/approved/` pipeline — subsume into L2 or
  retire); add admin-daemon socket endpoints mirroring
  `action.proposal.*` / `action.signal.*`; switch evo MCP tools from
  in-process store calls to socket calls (reverses the E.2.b direct-ACL
  exception — the socket didn't exist when that exception was cut; it
  does now).
- **D**: `EVOLVE_STORE_BACKEND=files|sqlite` resolved inside the store
  modules; one db per store (`signals/signals.sqlite`,
  `proposals/proposals.sqlite`), WAL, `busy_timeout=5000`, evolve-owned
  `0o644`; one-time migration importing legacy JSON under the Phase A
  lock + a stray-file sweep for the cutover window; `log/` JSONL and
  `feedback.jsonl` stay as files (append-only audit streams, atomic at
  `PIPE_BUF`). Flip default after the 7.2 canary has soaked the
  flag-off code; then delete the files backend.

### 1.6 The evo cross-user write ACL (constraint from spec-evo-account-separation-2026-05-25.md)

This constraint **must survive every phase until C completes**:

- **Phase A** preserves it untouched — files, dirs, modes, ACLs are all
  unchanged; the lock files are the only new objects, pre-created
  evolve-owned `0o666` and readable by every writer class (§1.4).
- **Phase B** is reader-side only — no change.
- **Phase C** is what *retires the need*: once evo's mutations go over
  the admin-daemon socket, the inherited ACL on `proposals/` and
  `signals/` (and the no-sticky `0o777` contract) stop being
  load-bearing.
- **Phase D** removes them: `ensure_pod_perms` drops
  `_check_evo_write_acl` for these two dirs and `POD_PROPOSALS_MODE`
  reverts to evolve-owned `0o755`.

**Honest correction to the roadmap's success criterion:** the
perms-repair daemon does **not** become *deletable* — it checks
contracts beyond these two stores (bot workspace ACLs, keystore, alerts
dir, evolve-owned dirs). What Phase D makes true is that the daemon
stops being **load-bearing for store correctness**: the
proposals/signals checks are deleted from `ensure_pod_perms`, and
ownership drift in those dirs can no longer wedge state transitions.
The daemon shrinks to a janitor for the remaining contracts.

---

## Part 2 — Deploy rollback + canary (7.2)

### 2.1 Audited current state (corrected per review)

- `repo_puller.py` `tick()` → `pull()` runs `git pull --ff-only origin
  main` every 15 min (LaunchDaemon, `evolve` user, `StartInterval=900`),
  then post-pull hooks: plugin rebuild + gateway restarts, infra-jobs
  reinstall, charter fingerprint bumps, sudoers refresh, pip install,
  path-mapped daemon kickstarts, openclaw config validation,
  lagging-bot redeploy. The validation + lagging-bot hooks **also run
  on no-op ticks** — that per-tick healing must survive this redesign.
- The puller has no rollback (its own comment ~:994: reverting is "more
  disruptive than logging a broken stage"). **But a pod-state rollback
  does already exist** *(review #3)*: `recovery.py::run_pod_rollback`
  (web-exposed) does a confirmation-gated `git reset --hard <sha>` on
  the deploy checkout + admin-ui kickstart. Today the puller un-does it
  within 15 minutes — presumably why row 7.2 exists. This spec
  **rewires it**: `run_pod_rollback` becomes a thin wrapper over the
  release manager's rollback (same gates, release.json-aware) instead
  of a bare git reset that desyncs the pointer.
- Exactly one migration script exists
  (`evolve_admin/migrations/cost_caps_normalize.py`) — schema
  migrations are rare and ad-hoc, so v1 rollback is a *code* rollback,
  not a data down-migration (non-goal, §2.5).
- `evolve-admin rollback` **name is taken** (per-bot config rollback);
  the release surface gets its own command group.
- Existing tooling assumes the fleet checkout sits on branch `main`
  tracking origin *(review #4)*: `evolve-admin refresh-sudoers` pulls
  `origin/main` by default and hard-fails off-branch (cli.py ~:4511,
  ~:4571); the puller's wedge heuristics probe `HEAD..origin/<branch>`;
  `tile_metrics.py::repo_puller_stale` keys on `.git/FETCH_HEAD` mtime.
  §2.10 addresses each.

### 2.2 Design at a glance

```
origin/main ──fetch──▶ CANDIDATE ──Gate 1──▶ CANDIDATE (soaking) ──Gate 2──▶ STABLE ──▶ fleet
              (every tick)   static checks      canary bot runs        promote: fleet
                             in per-candidate   staging code for       checkout moves +
                             staging worktree   soak window            all post-pull hooks
                                  │                  │
                                  ▼ fail             ▼ fail
                          Signal + skip sha   Signal + canary restored to stable + skip sha

evolve-admin release rollback  ──▶  fleet checkout → previous stable, full hooks, one command
```

The fleet checkout (`/Users/Shared/evolve-repo`) only ever sits at a
**promoted release pointer** — never raw `origin/main`. Candidates are
evaluated in per-candidate staging worktrees.

### 2.3 Release pointer

Machine truth: `{shared_dir}/release.json` (evolve-owned, atomic writes):

```json
{
  "stable":    {"sha": "…", "version": "2026.0610.2572", "promoted_at": "…"},
  "previous":  {"sha": "…", "version": "…", "promoted_at": "…"},
  "candidate": {"sha": "…", "first_seen_at": "…", "quiet_since": "…",
                "soak_started_at": "…", "state": "checking|soaking|failed",
                "failure": ""},
  "skip":      ["<sha>", "…"],
  "pin":       null,
  "mode":      "canary"
}
```

- **Fleet ref mechanics** *(review #4)*: the fleet checkout stays on
  branch `main`, moved by `git reset --hard <sha>` at promote/rollback
  (never detached — existing tooling asserts the branch name). All
  release-manager git ops run **as evolve** with explicit
  `safe.directory` covering both the fleet repo and the staging
  worktree paths; `sudo evolve-admin release …` commands re-exec their
  git work via the same `sudo -u evolve` pattern `cli.py` already uses
  for the deploy checkout *(review #15)*.
- Operator-visible mirror: local lightweight tags `evolve-stable` /
  `evolve-previous` moved at promote/rollback — `git log evolve-stable`
  answers "what is the fleet running" without knowing about
  release.json. This is the roadmap's "release tag": the fleet follows
  the promoted pointer; the tag is its git-native face. Tags are local
  to the mini (no push) — release state is per-pod, not per-repo.
- **Corrupt/missing release.json** *(review #11)*: corrupt → **freeze
  promotion + alert Signal** (`release_state_corrupt`); never silently
  reinitialize (a pin set by a rollback must not evaporate). Reinit
  only via explicit `evolve-admin release init`. Genuinely-missing
  (first run after upgrade) → initialize `stable = current fleet HEAD`,
  which makes the first canary-mode tick a no-op.
- **Fleet HEAD ≠ stable.sha** (operator did manual git surgery): alert
  Signal + repair the checkout back to the pointer on the next tick —
  the pointer is authoritative, exactly so that out-of-band resets
  can't silently fork the fleet *(review #3)*.

**No manual release-cutting step.** Candidates come from `origin/main`
automatically; promotion is automatic on canary pass. This deliberately
preserves the merge → live workflow with no new ceremony — but the
latency changes: a busy merge day batches into one promoted release per
quiet-window+soak (§2.4), so "merged" no longer means "live in 15 min."
`release promote` is the operator escape hatch when a fix needs to land
now *(review #13)*.

### 2.4 Candidate pipeline (state machine driven by the existing 15-min tick)

The tick is stateless; `release.json` carries the state across ticks.
**The per-tick healing hooks (openclaw config validation, lagging-bot
redeploy) continue to run every tick in canary mode**, against the
fleet checkout — promotion gating changes when *code* moves, not the
tick-level pod maintenance *(review #1)*.

1. **Fetch** `origin/main` in the fleet repo (objects shared with
   staging worktrees; per-tick fetch also keeps the
   `repo_puller_stale` FETCH_HEAD heuristic valid *(review #4)*). New
   sha ≠ `stable.sha` and ∉ `skip` → candidate. If a candidate was
   already in flight and origin/main moved again, the new sha replaces
   it in a **new** worktree (`quiet_since` resets) — soak starts when
   main goes quiet, so a burst of merges batches into one release.
2. **Per-candidate staging worktree** *(review #5)* —
   `/Users/Shared/evolve-staging/<short-sha>/`, a `git worktree` of the
   fleet repo, detached at the candidate sha, `clean -fdx` after
   checkout (`-x`: stale ignored debris like `node_modules`/`dist`
   must not leak into gates). A worktree is **immutable for its
   candidate's lifetime** — a replacement candidate gets a fresh
   worktree; resolved (promoted/failed/superseded) worktrees are
   pruned (`git worktree remove --force` + `worktree prune`). This is
   what keeps a soaking canary's plists (which bake staging paths into
   `PYTHONPATH`/script paths) pointing at the exact code that was
   gated.
3. **Gate 1 — static checks** (fast, every candidate, runs even with
   no canary bot configured):
   - `python3 -m compileall -q` over `packages/` in staging (syntax
     class),
   - import-smoke: importing the daemon entry modules
     (`evolve_admin.repo_puller`, `evolve_admin.deploy`,
     `evolve_admin.web.server`, `signals.store`, `arbiter.store`,
     `generator_runner`, …) with staging `PYTHONPATH` — catches
     import-time crashes before any daemon restarts onto them.
     (PYTHONPATH precedence over the PEP-660 editable install was
     verified empirically during review.)
   - **Dependency-bump handling** *(review #2 — was a pipeline
     deadlock)*: when the candidate diff touches
     `packages/admin/pyproject.toml`, Gate 1 first builds/refreshes a
     **staging venv** (`/Users/Shared/evolve-venv-staging`, `pip
     install -e <staging>/packages/admin` into it — never into the
     live venv, which would repoint the fleet's editable install at
     staging) and runs import-smoke + the canary deploy with the
     staging venv's python. Without this, any dep-adding PR (the
     google-auth class, PR #1862) would fail import-smoke forever and
     freeze the fleet. The live venv still gets its pip hook at
     promote, as today.
   - plugin build check when the candidate diff touches
     `packages/plugin/`: `tsc` build in staging to a throwaway dir
     (build-validation only; scope note §2.7).
4. **Gate 2 — canary soak** (requires `pod.release.canary_bot` in
   network.json; explicit pod membership, no auto-pick):
   - deploy the canary bot *running staging code*: `deploy <bot>`
     executed with staging `PYTHONPATH` (staging venv python when Gate
     1 built one). **Pod-side-effect suppression** *(review #5)*:
     canary deploys run with pod-wide mutations damped —
     `ensure_pod_perms` in check-only mode, no network.json/install.json
     pod-level rewrites — so candidate code cannot rewrite pod-wide
     state before it's been promoted. The canary's per-bot plists point
     into the candidate's immutable worktree.
   - **Release-aware lagging-bot sweep** *(review #1 — was a soak
     killer)*: `_find_lagging_bots` compares each bot to its *expected*
     version derived from `release.json` — the canary is expected at
     the **candidate** version while `state=soaking`; every other bot
     at stable. Without this, the per-tick sweep sees the canary
     "lagging" and silently redeploys it back to stable within 15 min,
     and the soak passes on fabricated evidence. `deploy_drift_monitor`
     gets the same carve-out (no pod drift Signal for the canary
     during an active soak).
   - soak for `pod.release.soak_minutes` (default 60; checked each
     tick): canary gateway not crash-looping (launchctl probe), no new
     firing Signals with `bot_id == canary` since `soak_started_at`,
     canary deploy reported success.
   - **No canary bot configured → degraded mode:** Gate 1 +
     `soak_minutes` of pure time-delay (operator can set 0 for
     promote-on-pass). Degraded mode is visible in `release status`
     and the puller log. Fresh installs work zero-config and are
     strictly better-gated than today.
5. **Promote**: fleet checkout `reset --hard` to the candidate sha —
   ancestry enforced (candidate must descend from stable; anything else
   requires `release pin`/`rollback`, never automatic) — then the
   **existing** post-pull hook suite runs unchanged (plugin rebuild,
   gateway restarts, infra jobs, sudoers, pip, daemon kickstarts,
   lagging-bot redeploy). Promote hooks run **in a fresh subprocess
   from the new fleet checkout** so `EVOLVE_VERSION` (computed at
   import time from `git log -1`) stamps the post-promote version, not
   the version of the still-running tick process *(review #14)*. The
   promote-time untracked-conflict sweep is keyed against the target
   sha (reusing `_handle_untracked_conflict`'s delete-identical /
   quarantine logic) *(review #15)*. `release.json` rotates stable →
   previous; tags move; the candidate's worktree is pruned; the canary
   is redeployed from the fleet checkout (back onto fleet paths).
   Info-Signal `release_promoted`.
6. **Fail** (either gate): `candidate.state=failed`, sha appended to
   `skip`, alert-Signal `release_canary_failed` with the gate
   transcript, **canary restored**: redeployed from the fleet checkout
   (stable code, fleet paths) and any `ai.openclaw.<canary>*` /
   per-bot plists not present in the stable plist set are
   garbage-collected (a plist *added* by candidate deploy code must not
   linger pointing at a doomed worktree *(review #5)*). The worktree is
   pruned after restore. Fleet never saw the sha. Operator paths:
   fix-forward on main, or `release retry <sha>` after investigation.

Skip-list hygiene: entries older than the current stable's ancestry are
pruned at promote (a skipped sha that is now an ancestor of stable is
moot) — the list stays O(recent failures), not O(history).

### 2.5 Rollback

```
sudo evolve-admin release rollback            # → previous stable
sudo evolve-admin release rollback --to <sha|version>
```

One command *(review #12 folded in)*: cancels any in-flight candidate
(worktree pruned, canary restored to the rollback target), fleet
checkout `reset --hard` to target (ancestry check waived under
rollback), full post-move hook suite in a fresh subprocess (rebuild,
restarts, redeploys), `release.json` updated — `stable` ↔ target,
**the rolled-back-from sha appended to `skip`** (tripwire: `unpin`
alone must not re-promote the sha we just fled), `pin` set to target —
tags moved, alert-Signal `release_rolled_back`. Candidate machinery
keeps *evaluating* new candidates while pinned but **cannot promote**;
`release unpin` resumes auto-promotion.

`recovery.py::run_pod_rollback` (the existing web-exposed pod-state
rollback) becomes a wrapper over this path — same confirmation-token
gates, no more bare `git reset` that the puller fights *(review #3)*.

Non-goal (v1): data down-migrations. Rollback restores code, not store
state — with exactly one migration script in existence, store formats
are stable across adjacent releases. When 7.1 Phase D's SQLite
migration lands later, its release notes the constraint explicitly
(forward-only migration + documented manual restore path).

### 2.6 Command surface

```
evolve-admin release status      # pointer, candidate state, soak countdown, degraded?, last failure
evolve-admin release rollback [--to REF] [--dry-run]      # force-move BACKWARD (skip-lists fled)
evolve-admin release bootstrap REF [--dry-run] [--yes]    # force-move FORWARD, bypassing the soak gate (§2.11)
evolve-admin release pin [REF] / unpin                    # FREEZE at the current stable — never a move
evolve-admin release promote     # operator override: run Gate 1 now, skip remaining soak
evolve-admin release retry SHA   # remove from skip list
evolve-admin release init        # explicit (re)initialization — never automatic on corrupt state
```

(Distinct from the existing per-bot `evolve-admin rollback` — that name
stays with bot config recovery.)

**`pin` is a freeze, not a move** *(footgun fixed 2026-06-13)*. `pin`
holds the fleet at the **current stable** so a bad auto-promotion can't
land mid-investigation. The invariant `state.pin["sha"] == state.stable["sha"]`
holds at every write site (`release_pin`, `release_rollback`,
`release_bootstrap`). A `REF` argument is only an *assertion* that stable
is where the operator thinks it is — `pin <ref>` where `ref` ≠ stable is
**refused** and redirects to `bootstrap`/`rollback`. (The old behavior
silently recorded a `pin.sha` that disagreed with stable and moved
nothing — the deploy checkout and pointer-repair both follow
`state.stable`, never `state.pin`, so the recorded sha was dead data.)

### 2.7 Scope cuts (recorded, deliberate)

- **Plugin canarying**: the canary bot's gateway loads the shared
  staged plugin dist, so true per-bot plugin canarying needs a per-bot
  plugin load path. v1 gives plugin changes Gate 1 build-validation
  only; fleet-wide plugin restage still happens at promote (strictly
  later and better-gated than today). Follow-up: point the canary
  gateway at a canary-staged dist.
- **Pod-wide daemons can't be canaried per-bot** (admin-ui, heal, …):
  they follow the fleet checkout and move only at promote. Gate 1's
  import-smoke is their pre-restart gate (it directly targets their
  historical failure class: import-time crashes, missing deps,
  undefined names at module scope).
- **Soak telemetry granularity is the 15-min tick** — good enough for
  the failure classes observed; not a real-time watchdog.

> **Proposed follow-up (2026-06-12, `META:rsi` → `META:diligence`):** real usage
> showed the flat 60-min soak is uniform-cost / variable-benefit — docs & UI changes
> pay the full hour for ~zero canary coverage, and §2.7's own daemon/plugin cuts mean
> the soak is pure time-delay for those. See
> [`spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md`](spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md):
> risk-tier Gate 2 by blast-radius × reversibility (skip / short / full), add an
> *active* canary probe so a short window gives strong evidence, and reserve the full
> soak for the irreversible-consequence minority (rollback already covers the rest).

### 2.8 Rollout & kill-switches

- `pod.release.mode`: `"direct"` (legacy `pull()` path, **initial code
  default**) | `"canary"`. Env `EVOLVE_RELEASE_MODE` overrides for
  30-second disable (same pattern as `EVOLVE_PULLER_AUTO_RESTART`).
  *(review #13)*: shipping canary as the immediate code default would
  enable it everywhere, unwatched, the moment the commit lands via the
  old puller. Sequence instead: ship default-`direct` → enable
  `mode=canary` on the mini → watch one full
  candidate→soak→promote cycle (the project's canary rule, applied to
  the canary feature itself) → follow-up PR flips the code default so
  fresh installs get the gated pipeline.
- First canary-mode tick initializes `release.json` with
  `stable = fleet HEAD` (the running sha) → a no-op, not a surprise.
- Retreat: `mode=direct` (or the env var) restores today's exact
  behavior; `release.json` is left in place and resumes when re-enabled.

### 2.9 Proof artifact (7.2)

`test_release_manager.py` against throwaway git fixtures (bare origin +
fleet clone + staging worktrees, injected deploy/kickstart/notify fns —
the injection pattern `test_repo_puller.py` already uses):

- a deliberately broken candidate (syntax error / failing import) fails
  Gate 1 → fleet sha unchanged, Signal emitted, sha in `skip`;
- a healthy candidate soaks → promotes → fleet sha advances + hooks ran
  + candidate worktree pruned;
- a candidate that breaks the canary during soak (injected firing
  Signal) → fails, canary restored, fleet unchanged;
- the lagging-bot sweep during an active soak does **not** redeploy the
  canary back to stable (the review-#1 regression test);
- a dep-bump candidate (pyproject change) routes through the staging
  venv and is promotable (the review-#2 regression test);
- `release rollback` moves the fleet to `previous` in one command,
  pins, and skips the fled sha.

### 2.10 Integration with existing tooling (from review #3/#4)

| Tool | Today | Under canary mode |
|---|---|---|
| `refresh-sudoers` default pull | `merge --ff-only origin/main`, asserts branch `main` | syncs to the **release pointer** (no-op if fleet already there); never to origin tip — otherwise it would bypass every gate |
| `recovery.run_pod_rollback` | bare `git reset --hard` the puller un-does | wrapper over `release rollback` (§2.5) |
| puller wedge heuristics (`HEAD..origin/<branch>` probes, "diverged" hint) | assume fleet tracks origin/main | scoped to `mode=direct`; canary mode has its own failure Signals (`release_canary_failed`, `release_state_corrupt`) |
| `tile_metrics.repo_puller_stale` (FETCH_HEAD mtime) | fetch every tick | unchanged — canary mode fetches every tick too (§2.4.1) |
| `deploy_drift_monitor` | any stamp ≠ EVOLVE_VERSION fires | canary carve-out during active soak (§2.4.4) |

### 2.11 Deploying a fix to the release pipeline ITSELF — the bootstrap deadlock *(added 2026-06-13)*

**Invariant: you cannot soak-gate a fix to the soak gate.** The release
daemon evaluates Gate 2 (and all pipeline logic) by importing
`release_manager` **from the deploy checkout** — i.e. from the *current
stable* — not from the candidate worktree. So a candidate whose only
change *is* a fix to the gate is still judged by the **old, broken** gate.
If the bug fails every candidate (e.g. the D4 ambient-debt fleet-jam:
stable #2816's `_default_soak_health` failed the soak on standing app-debt
that re-fires every scan), the fix for it can never promote through the
broken gate — a classic bootstrap deadlock. `release promote` does
not escape it either: it only fast-forwards the soak clock and then runs a
normal tick, which still runs the broken health check.

This is intrinsic to the design (the daemon must run *some* committed
version of itself; the candidate is unproven by definition), so the
pipeline ships a sanctioned escape hatch rather than pretending the
deadlock can't happen.

**`release bootstrap <ref>`** is that hatch — the one verb that
force-promotes past the gate while still moving the fleet *correctly*:

- Force-moves `state.stable` **forward** to `<ref>` via the shared
  `_force_move_fleet` path (the same pointer-before-hooks ordering,
  auto-pin, and canary-restore machinery as `release rollback`), bypassing
  Gate 1/Gate 2 and the ancestry check.
- Prints a **loud confirmation banner** naming exactly the un-soaked
  `stable..<ref>` commits it promotes, and (interactively) requires
  confirmation; `--dry-run` shows the range and changes nothing; `--yes`
  skips the prompt for scripted use.
- Emits a **distinct `release_bootstrapped` Signal** (not
  `release_rolled_back`) so the Alerts page reads "bootstrapped" for a
  forward move — the previously-only lever was `rollback --to <tip>`,
  whose `release_rolled_back` Signal mislabels a forward bootstrap as a
  regression.
- Does **not** skip-list the fled sha. The fled sha is an *ancestor* of
  the target (forward move); skip-listing it would needlessly block ever
  re-promoting it. By contrast `rollback` *does* skip-list the fled sha,
  because it flees a sha believed bad.
- **Auto-pins** (`pin.sha == target == new stable`), exactly like
  rollback. The operator then verifies the fleet (gate fixed? daemons
  healthy?) and runs `release unpin` to resume auto-promotion — at which
  point anything on origin/main *past* the bootstrap target soaks normally
  through the now-fixed gate.

`bootstrap` is CLI-only by design — it bypasses the project's central
safety gate, so it is gated behind an explicit operator command + a
confirmation banner rather than surfaced as a one-click web button (unlike
`release promote`, which still respects Gate 1).

Worked example (the D4 gate-fix bootstrap, 2026-06-13): stable #2816 had
the broken soak-health check; the fix (#2832) and everything between were
ancestors of origin tip. `sudo evolve-admin release bootstrap <tip>`
(after `--dry-run` to confirm the `#2816..#2832` range) moved the fleet to
the fixed code and pinned; `release unpin` then resumed auto-promotion,
and the next candidate (#2834) soaked normally through the repaired gate.

---

## Sequencing (both rows)

1. **PR 1** — this doc.
2. **PR 2** — 7.1 Phase A: store locks + CAS transitions + concurrency
   tests + lock-file pre-creation in `ensure_pod_perms` (safe under the
   legacy puller).
3. **PR 3** — 7.2: release manager + CLI + tests, default `mode=direct`;
   enabled on the mini after a watched cycle; follow-up PR flips the
   default.
4. Follow-ups (separate sessions): 7.1 Phase B (reader sanctioning + CI
   guard), Phase C (writer funneling), Phase D (SQLite swap riding the
   canary), plugin canarying, canary-mode default flip.

Exit criteria mapping: "two daemons can't corrupt or duplicate shared
state" → PR 2 (race test is the artifact). "A bad merge can no longer
reach every daemon within 15 minutes" → PR 3 (broken-release test is
the artifact). "Perms-repair daemon deletable" → corrected in §1.6: the
store-correctness checks become deletable at Phase D; the daemon
shrinks to a janitor.
