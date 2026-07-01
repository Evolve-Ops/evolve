# Spec delta: active-canary validation — make the soak window earn its time

**Status:** accepted (shipped). D5 shipped (#2858) · D6-live DROPPED, re-scoped to a future deterministic dry-run (OQ2) · D7 shipped (#2863). See the Build-time findings section for the as-built record. **Owner:** `META:diligence` (release pipeline, roadmap Phase 7).
**Builds on:** [`docs/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md`](spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md) (§D1 risk-tier, §D2 active probe, §D3 tune defaults, §D4 baseline-diff). Continues the section numbering at **D5–D7**.
**Pipeline spec of record:** [`docs/spec-state-store-and-deploy-resilience-2026-06-10.md`](spec-state-store-and-deploy-resilience-2026-06-10.md) (7.2).

---

## Why (the soak time is mostly dead wall-clock)

A trace of `release_tick` shows a soak is three parts:

1. an **active probe that runs once, at soak start** (`soak_probe.py`, invoked at `release_manager.py:1754`) — imports/runs the *changed* generators, import-smokes the *changed* route modules, and does a send round-trip for send-surface changes;
2. a **per-tick signal-diff** (`_default_soak_health`, §D4) — fails the release only if a *new-signature* firing Signal appears on the canary versus a stable-code baseline;
3. a **timer** (`release_manager.py:1828`) — `now >= soak_started + minutes` → promote.

Part 3 does nothing by itself. Part 2 only catches a regression **if some monitor, or the bot's own cron/heartbeat, actually executes the candidate code during the window and emits a Signal.** For a canary bot with nothing scheduled inside the window, the elapsed time catches nothing the start-probe didn't — **5, 15, 20, 60 minutes are equivalent to 0.** The window's "value" is purely the probability that the slowest surface you care about happens to fire within it: uniform-cost / variable-benefit (already the #2802 driver). This delta removes the variance by making the window's checks **active** instead of ambient.

Two surfaces the timer is implicitly waiting on, and that the start-probe does **not** cover today:

- the **canary's gateway coming up and serving on the candidate code** (import-smoke ≠ runtime init — a bot can import clean and crash on boot);
- the **bot's heartbeat / cron loop** running the candidate code without breaking.

Force those two, check them, and the timer becomes vestigial.

---

## The delta (three changes)

### D5 — Gateway liveness probe (always-on; runtime, not import)

After the candidate is deployed to the canary (canary on candidate code, side-effects suppressed per §2.4), actively assert the canary's **gateway is up and serving** on the new code: a bounded liveness round-trip (the gateway healthcheck / a no-op request), retried over a short ceiling (≤ ~60s).

- **Always-on**, not gated on changed paths — gateway health is universal and is the single highest-value runtime check (operator-named: "checking that a release doesn't instantly break the gateway is useful").
- **Fail CLOSED** on no-response after retries (the candidate broke the bot's runtime). **Fail OPEN** + `release_soak_check_degraded` Signal on a *tooling* fault (we couldn't reach it for our own reasons) — never fail a release on our own fault (§D2/§D4 invariant).
- Distinct from the §D2 import-smoke probe: §D2 proves the changed modules import/run in a subprocess pinned to staging; D5 proves the **actual canary bot process** serves on the new code.
- New probe `kind: "gateway"` in `classify_soak_probe` output, emitted unconditionally (not derived from the diff). Implemented in `soak_probe.py` with the same tri-state exit-code contract (`0` ok / `2` regression / `3` tooling).

### D6 — Forced heartbeat / cron exercise

Instead of passively waiting for the bot's heartbeat to fire inside the window, **force one heartbeat tick (and one cycle of any *due* scheduled actions) on the canary under candidate code**, and assert it completes without error and without emitting a new-signature firing Signal (reuse the §D4 baseline diff for the verdict).

- **Side-effect suppression is load-bearing:** the forced tick runs under the same `pod_side_effects=False` contract as the canary deploy (§2.4) — it exercises the code path but routes any unavoidable send to the **operator channel only** (the §2.4-sanctioned exception, exactly as the §D2 send-probe already does). A forced heartbeat that sent to real recipients would itself be a regression.
- This is the "does it break the bot's actual runtime loop" check — done in **seconds by forcing it**, vs. up to the heartbeat interval by waiting.
- **Scope:** the universal heartbeat + the *diff-affected* scheduled surfaces (reuse the §D1 path→probe classification). Do **not** attempt to force every monitor.

### D7 — Promote on active-pass; demote the timer to a residual ceiling

> **As-built (2026-06-13):** shipped on **D5 + D2 + D4** (D6-live dropped — see Build-time findings). The proposing text below names D6; the shipped predicate is `active_validation_passed (D5 gateway + D2 probes, no REGRESSION) AND tier_residual_elapsed AND soak_health_clean (D4)`. Residual: skip/short (active-validated) → 0; full → `DEFAULT_SOAK_MINUTES` (tuned 60 → 15). The authoritative as-built record is in the Build-time findings section.

Once D5 + D6 + the §D2 changed-surface probe + the §D4 baseline-diff are all clean, the candidate has been **actively validated** — promotion gates on *that*, not on elapsed time. The wall-clock window becomes a short **per-tier residual ceiling** for the slow-failure classes that cannot be cheaply forced, not the primary gate:

- **skip / short tiers:** promote as soon as active validation passes (timer → 0 / minimal).
- **full tier:** keep a short residual dwell (recommend **~10–15 min**, configurable via the existing `pod.release.soak_minutes`) as the only concession to accumulating / slow failures on the irreversible-consequence minority.
- Net: the common case collapses from "wait T and hope a monitor runs" to "force the three things that matter, verify, promote in seconds" — **deterministically**.

The promote predicate becomes: `active_validation_passed AND (tier_residual_elapsed) AND soak_health_clean` — where `tier_residual` is 0 for skip/short and the short dwell for full. A failed active check fails the candidate **immediately**, not at timer-elapse.

---

## Non-goals / deferred (deliberate)

- **Slow / accumulating failures** (memory leak, a crash loop that needs several restarts, an hourly cron that breaks) that cannot be forced cheaply: accepted as caught **post-promote** by heal + watchdog Signals + one-command `release rollback`. Pre-release, no external users → cheap and instructive. The full-tier residual ceiling (D7) is the only pre-promote concession.
- **Forcing every monitor:** out of scope — D6 forces the universal heartbeat + the diff-affected surfaces only.
- **Plugin / gateway canarying gap** (the canary gateway still loads the shared dist) — pre-existing Phase 7 follow-up, unchanged here.

---

## Proof artifacts (falsifiable — the diligence completion bar)

`packages/admin/tests/test_release_manager.py` (+ `test_soak_probe.py`), `-k "d5 or d6 or d7"`:

- **D5:** a candidate whose gateway init raises → liveness probe returns REGRESSION → soak **FAILS CLOSED**, candidate left rollback-able; a healthy candidate → OK. A liveness *tooling* fault (probe can't reach) → **fail OPEN** + `release_soak_check_degraded` Signal, release **not** failed.
- **D6:** a candidate that breaks the heartbeat path → forced-heartbeat probe FAILS (new-signature diff); a healthy one passes; **assert no real send escapes** — side-effect suppression honored, only the operator channel, verified through the send-surface fake.
- **D7:** a candidate passing all active checks **promotes at active-pass time, not timer-elapse** (assert promotion *before* the residual ceiling on skip/short); a candidate failing an active check is failed **immediately**, not after the timer; the full-tier residual ceiling still applies when active checks pass but the tier mandates dwell.
- **Live proof:** stage a deliberately gateway-breaking candidate; confirm it is caught at promote-time **in seconds** (not after the window) and rolled back; a clean candidate promotes in ~seconds on skip/short.

---

## Invariants preserved (carry from D1–D4 / Phase 7)

- Gate 1 always-on; release **pointer persisted before the post-move hook suite**; canary **side-effect suppression** (D6 forced tick honors it); **never fail on tooling fault**; **never silently pass** (no-baseline → legacy predicate); **lagging-bot sweep + `deploy_drift_monitor` exempt the canary during soak**; rollback / pin / bootstrap semantics unchanged.
- **Privileged path** (the release pipeline mutates the live fleet): **auditor-grade two-pass review**, and construct the actual failure string (a candidate that imports clean but crashes the gateway on boot), don't just eyeball.

---

## Sequencing (for `META:diligence`)

1. **D5** gateway liveness probe (+ tests) — smallest, highest value; ship first.
2. **D6** forced heartbeat exercise (+ the side-effect-suppression guard + tests).
3. **D7** event-driven promote: timer → per-tier residual ceiling (+ tests), then tune the `soak_minutes` defaults down (skip/short → promote-on-active-pass; full → ~10–15 min residual).

Each bite: immediate empty-commit push + incremental pushes; **two-pass review inside the build chip** (the reviewer's verdict gates the auto-merge — not a separate post-build chip); auditor-grade review on the promote-decision change; a falsifiable proof artifact verified against code before close.

---

## Build-time findings (2026-06-13)

### D5 — SHIPPED (PR #2858)

Always-on gateway liveness probe, merged with auditor-grade review (PASS). Two as-built notes that supersede the proposing text above:

- **Signal name:** a gateway *tooling* fault emits `release_soak_probe_degraded` (the existing **active-probe** degraded Signal), **not** the `release_soak_check_degraded` named in §D5 above (which is the **passive-health-check's** Signal). The gateway probe is an active probe, so reusing the active-probe degraded path is correct — and keeping the two signatures distinct (active-probe-couldn't-run vs. passive-health-check-erroring) is better operator ergonomics. Treat §D5's `release_soak_check_degraded` as a drafting slip; the active-probe name is the contract.
- **Scope honesty:** D5 proves the canary's gateway **re-mounts and serves `/evolve/status` after the candidate's deploy + restart** — it catches a candidate that corrupts the bot's config or crashes the deploy-driven restart. It does **not** exercise a candidate change to the **Node plugin bundle** (which loads from the shared dist — the pre-existing plugin-canarying gap, already a Non-goal). The marginal value over the deploy's own kickstart-wait is real: that wait gates only on the OC gateway *version* (`openclaw gateway status`), never on whether `/evolve/status` re-mounts.

### D6 — live-heartbeat exercise DROPPED; re-scoped to a future deterministic dry-run (design decision 2026-06-13)

**The §D6 mechanism (force a live heartbeat tick on the canary) does not exist in the platform as written. After the design session the operator DROPPED the live-heartbeat half and approved D7 to ship on D5+D2+D4.** The mechanism finding that drove the decision:

- **A heartbeat tick *is* a live LLM agent turn** (`delivery_monitor.py:63` — "the LLM decides per heartbeat"; the plugin's `TurnObserver` treats heartbeat sessions as full agent sessions). Running a *due scheduled action* is likewise an agent turn. There is no pure-Python "run the heartbeat/scheduled-action" path to force cheaply.
- **No force-run interface exists.** `delivery_monitor.py:817` documents it explicitly: the deployed `AgentRuntime` seam "exposes `cron_list`/`cron_runs` only — no 'run job now' interface … Report-only until upstream grows one (§15 OQ2)." The evolve plugin serves only `/evolve/status|/metrics|/` over loopback — **no trigger endpoint**. The closest mechanism, `sudo -u <bot> openclaw agent --local --agent main --message` (the forge dispatcher pattern), runs a **user** turn (won't fire `scheduled_actions[trigger=heartbeat]`), **costs a real LLM call per soak**, and is non-deterministic.
- **The forced turn's own sends cannot be suppressed without a plugin change.** `pod_side_effects=False` suppresses *deploy* pod-writes; the dispatcher's `recipient_override` governs the **evolve** dispatcher — neither governs the **bot's own openclaw sends** during an agent turn. So an unsuppressed forced turn could leak a real send to a user, violating D6's load-bearing side-effect-suppression invariant.

**Decision (operator-approved 2026-06-13):**

- **D6-live is DROPPED.** Gating promotion on a forced heartbeat would mean gating on a **non-deterministic LLM turn** with no force-run seam, scored via the §D4 signal-diff — which reintroduces exactly the false-positive class (a flaky/ambient signal failing a good release) this whole D-series exists to kill. The cost (a real LLM call per soak) and the required TypeScript send-suppression change to the **un-canaried shared-dist plugin** make it negative-value as a promotion gate.
- **D6 is re-scoped to a future *deterministic dry-run*** — a pure-Python "exercise the heartbeat/scheduled-action code path without an LLM turn and without any send" — **gated on an upstream `openclaw` force-run / dry-run seam (§15 OQ2)**. Until that seam exists, there is no D6 to build. The upstream dependency is now **filed and tracked** as [openclaw/openclaw#92783](https://github.com/openclaw/openclaw/issues/92783) (a force-run / dry-run seam for `scheduled_actions`); petitioning upstream for it is the clean path, with a pod-local plugin trigger+suppression endpoint as the fallback. Tracked as deferred, not blocking.
- **D7 ships on D5 + D2 + D4** (this PR). The active-validation set is already strong without D6: **D5** (the canary's gateway re-mounts and serves `/evolve/status` on the new code — catches a candidate that imports clean but crashes the bot's runtime on boot) + **D2** (the changed-surface probes import/run the changed generators, routes, send paths) + **D4** (the baseline-diff health fails only on a new-signature firing Signal on the canary). The slow / accumulating class that only a live loop would surface is covered exactly as the spec's own Non-goals already accept: **post-promote by heal + watchdog Signals + one-command `release rollback`**, plus D7's full-tier residual ceiling for the irreversible minority.

### D7 — event-driven promote — AS-BUILT (this PR)

Status: **shipped.** `release_manager.py` (`release_tick` + `tier_residual_minutes` + `_candidate_soak_minutes` + the `release soak` override), tests in `test_release_manager.py` (`-k d7`).

The promote predicate is now **`active_validation_passed AND tier_residual_elapsed AND soak_health_clean`**, evaluated in one place (the soaking branch) every tick:

- **`active_validation_passed`** — the D5 gateway-liveness probe + the D2 changed-surface probe(s) returned **no REGRESSION**. Enforced structurally: a REGRESSION fails the candidate at the tick it is observed (soak entry), so a candidate only *reaches* the soaking branch having passed. A *tooling* fault (degraded/ERROR) fails OPEN — it does **not** fail the candidate — but it does **not** count as validated either: the candidate is stamped `soak_active_validated=False` and falls back to the legacy passive window (never silently fast-tracked on validation that never ran). The flag is persisted on the candidate (`soak_active_validated`).
- **`soak_health_clean`** — the §D4 baseline-diff shows no new-signature firing Signal on the canary.
- **`tier_residual_elapsed`** — the per-tier residual ceiling (below) replaces B3's passive windows.

**Timer → residual ceiling.** `DEFAULT_SOAK_MINUTES` dropped **60 → 15** (it is now the *full-tier residual ceiling*, configurable via `pod.release.soak_minutes`, not a universal passive window). `tier_residual_minutes(tier, configured, *, active_validated)`:

| tier | active-validated (canary, probe OK) | not validated (degraded / no canary) |
|------|--------------------------------------|--------------------------------------|
| skip | 0 (promotes after Gate 1, never soaks) | 0 |
| short | **0 — promote at active-pass time** | `min(SHORT_SOAK_MINUTES=15, full)` (legacy B3 fallback) |
| full | configured (`DEFAULT_SOAK_MINUTES`, default 15) | configured |

B3's `short = min(15, cfg)` is **superseded**: short's window is now *active-gated* — residual 0 once the canary has actively validated it, the legacy passive window **only** as the no-canary fallback.

**Same-tick promote (the real win).** The release pipeline ticks on the 15-min repo-puller cadence (`cli.py` → `release_tick`). So "promote on the next tick" would mean *up to 15 minutes later* — D7 would be a no-op for `short`. Instead, soak entry now **falls through** into the soaking-branch predicate in the **same tick**: a residual-0 candidate (skip/short, active-validated) promotes *now*, at active-pass time, deterministically in seconds; a full-tier (or degraded short) logs "soaking" and waits the residual dwell exactly as before. One predicate, one code path — the privileged `_promote` (ancestry check, mid-tick pin re-check, pointer-before-hooks, canary restore, prune) is unchanged.

**A failed active check fails the candidate IMMEDIATELY** at the tick it is observed (soak entry), not at timer-elapse.

Invariants preserved: Gate 1 always-on; release pointer persisted before the post-move hooks; canary side-effect suppression unchanged; never fail on tooling fault (fail OPEN + degraded Signal); never silently pass (no canary / degraded probe → legacy window, not residual 0; no baseline → legacy since-predicate); lagging-bot sweep + `deploy_drift_monitor` exempt the canary during soak; rollback / pin / bootstrap semantics unchanged.
