# Spec delta: risk-tier the soak + active canary

**Status:** accepted (shipped) — B1/B2/B3 (D1 risk-tier, D2 active probe, D3 default tuning) + D4 baseline-diff all landed and verified against code (#2812/#2820/#2831/#2832). Originally proposed by `META:rsi` for the `META:diligence` (deploy-resilience / Phase 7) coordinator to review + sequence.
**Date:** 2026-06-12
**Author:** `META:rsi` (analysis + draft), handed to `META:diligence`
**Amends:** [`docs/spec-state-store-and-deploy-resilience-2026-06-10.md`](spec-state-store-and-deploy-resilience-2026-06-10.md) §2.4 (Gate 2), §2.7 (scope cuts), §2.8 (defaults)
**Keeps unchanged:** Gate 1 (static checks), the release pointer (§2.3), rollback (§2.5), canary side-effect suppression (§2.4)

---

## Why (real-usage friction that surfaced this)

The canary pipeline shipped default-OFF, was enabled on the mini to watch one full cycle (the project's own canary rule applied to the canary feature), and **real usage has now surfaced the next round of work** — exactly the intended rhythm.

The friction: **every candidate pays a flat 60-min soak regardless of risk.** The operator hit it on a docs PR (#2799) and a generator-copy change (#2796) — a full hour each, for changes the soak structurally *cannot* evaluate. "I'm routinely tempted to override it with `release promote`" is the tell that the default is miscalibrated.

Grounded critique (against the as-built pipeline):

1. **The two gates have opposite cost/benefit, but the policy bundles them.** Gate 1 (static: `compileall` + import-smoke + staging-venv-on-dep-bump) carries nearly all the demonstrated value at near-zero cost — it targets Evolve's documented failure class (import-time crashes, missing deps, module-scope `NameError`). Gate 2 (the 60-min soak) is the friction, and its *marginal* coverage over Gate 1 is far thinner than it feels.
2. **The soak is passive.** Nothing exercises the candidate during the window — it watches whether the canary bot emits *new firing Signals*. So it only catches failures that manifest **on the canary specifically, within the window, as a firing Signal, at 15-min telemetry granularity** (§2.7 already concedes "not a real-time watchdog"). The canary (a low-traffic personal bot) never runs most changed paths, so "quiet for 60 min" is weak evidence.
3. **Uniform cost, variable (often zero) benefit.** Docs, admin-UI, pod-wide daemons, and plugin changes pay the full hour — but §2.7 *already* excludes daemons and plugins from per-bot canarying, so for those the soak is pure time-delay with no canary coverage at all.
4. **Rollback (already built, §2.5) reverts code, not consequences.** A one-command rollback exists, so the soak only earns its latency tax where a bad release reaching the fleet does damage rollback *can't* undo: messages already sent to users, state/config already mutated, an auth/security hole already exposed. For side-effect-free / reversible changes, rollback alone is sufficient and far cheaper.

**The dividing line:** prevention (soak) vs. recovery (rollback). With good rollback in place, soak should be reserved for the **irreversible-consequence minority**, not applied uniformly.

---

## The delta (three changes)

### D1 — Risk-tier Gate 2 by blast-radius × reversibility (path policy, v1)

Classify each candidate from its diff (the diff is **already computed** in §2.4 for dep-bump / plugin detection — no new machinery to obtain it) into a soak tier:

| Tier | Soak behavior | Example touched paths |
|---|---|---|
| **skip** | Gate 1 + promote immediately; rollback is the net | `docs/`, `*.md`, tests, admin-UI-only static assets (`web/static/**` with no server/runtime change) |
| **short** | one active-probe pass (D2) or ~10–15 min | generators, analyzers, admin-server routes, ordinary runtime code |
| **full** | current 60-min window (or staged rollout, future) | delivery / message-send, store/state writes, auth / sudoers / perms, the deploy/release pipeline itself, plugin (TS), migrations |

- **Fail-safe default:** an unmatched / ambiguous path falls to **full**. The policy is a small, testable path→tier table (mirrors the existing CI path-filter pattern), never a model judgment.
- **Vocabulary reuse:** the two axes are exactly the proposal system's `risk_tag.blast_radius` × `reversibility`. v1 derives the tier from paths (a raw diff has no `risk_tag`); the table encodes the mapping so it stays auditable.
- **Operator controls unchanged + extended:** `release promote` still forces promotion; add `release soak <tier|minutes> [SHA]` so the operator can bump a specific candidate *up* a tier when they want more caution than the policy assigned.

### D2 — Active canary probe (make a short window strong)

During soak, **exercise** the candidate on the canary instead of only watching it, with the probe selected from the same diff classification:

- diff touches generators → run the changed generator(s) once against the canary;
- diff touches admin-server routes → hit the changed route(s);
- diff touches delivery / message-send → a canary send round-trip (the **post-upgrade canary send** precedent from the send-surface probe already exists — reuse it).

A probe failure (non-zero exit / new error Signal / send regression) **fails the soak immediately** — minutes, not an hour. The passive quiet-window stays as a backstop, but because evidence is now *active*, the default window can shrink and no longer depends on the canary's ambient traffic level.

> **Amendment (2026-06-23, [META:deploy] — send-probe goes SILENT).** The `send` kind's **live `dispatcher.send` to the operator alert channel was dropped**. That message ("🟢 Release soak probe …") was vestigial in the soak context — the verdict is read programmatically, so the green text added nothing — yet it delivered to the operator on **every** delivery/message-send soak with no dedup (the operator got several a day). Operator decision: **silent success** — a healthy soak shows the operator NOTHING; the broken case still surfaces through the existing soak-failure path. The `send` kind now derives its verdict from the **send-surface CONTRACT probe** (`safe_upgrade.probe_send_surface` — `message send --help` + required-flags check, side-effect-free) and rests the **live end-to-end delivery proof on the always-on D5 `gateway` liveness probe** (which round-trips the actual canary process: CLI → gateway → `/evolve/status`). Tri-state unchanged: a removed/renamed send surface → REGRESSION; "couldn't verify" → tooling ERROR (fail OPEN). A self/loopback re-target (send to the canary itself via `recipient_override` so the path runs but the operator's inbox stays quiet) was **investigated and is not currently supported** — Telegram (the dominant channel) does not let a bot deliver to itself (`sendMessage` to the bot's own id → "chat not found"), the dispatcher's Telegram path is hardcoded to the *primary* bot's token, and the canary's only real chat is the operator's; a re-target would therefore either spuriously fail every healthy soak or reintroduce an operator-visible / real-user send. The live re-target can be added later through the reserved, **default-OFF `soak_send_probe`** dispatcher source if OpenClaw grows a self-deliverable destination. This makes the send probe strictly **side-effect-free** — well inside the §2.4 canary side-effect-suppression carve-out (it removes the one deliberately-allowed side effect rather than adding one). Decoupled from the **post-OC-upgrade** proof in `ocadmin.py`, which keeps its one operator-visible delivery-proof send per upgrade (`send_surface_probe`, default-ON) — that is genuine product value after a potentially-bricking upgrade and is **unchanged**.

### D3 — Tune defaults

- `short` tier window → ~10–15 min (the 15-min telemetry tick is the practical floor); `skip` → 0; `full` → keep 60 (until staged rollout exists).
- `DEFAULT_SOAK_MINUTES = 60` becomes the **full-tier** default, not the universal one.

---

## Non-goals / deferred (recorded, deliberate)

- **Staged percentage rollout** (one-canary → N% → fleet) — the natural extension of the `full` tier; sketch only, not v1.
- **Data down-migration rollback** — already a Phase 7 non-goal (§2.5).
- **Per-bot plugin canarying** — already a §2.7 scope cut; D2's plugin handling stays Gate-1 build-validation unless that cut is separately lifted.

## Proof artifacts

Extend `test_release_manager.py` (the §2.9 fixture pattern):

- docs-only candidate → tier `skip` → promotes with **no** soak window; fleet advances; rollback still available.
- generator candidate → tier `short` + active generator-run probe → an injected runtime error fails the soak in **< one tick**; canary restored; fleet unchanged.
- delivery-path candidate → tier `full` + active send probe → an injected send regression fails the soak; canary restored; fleet unchanged.
- unknown/ambiguous path → **defaults to `full`** (the fail-safe regression test).
- live re-proof: a #2796-class generator-copy change under the new policy is tier `short` and promotes in ~one tick instead of an hour.

## Sequencing (for `META:diligence`)

Suggested bites (each ~30 min, own proof artifact):
- **B1** — path→tier policy + skip/short/full wiring + tests (no probe yet). Delivers the headline friction win.
- **B2** — active canary probe (D2), reusing the post-upgrade-send precedent.
- **B3** — default tuning (D3) + (optional) staged-rollout design note.

Invariants to preserve: **fail-safe default** (soak when unsure); **Gate 1 always-on**; rollback path untouched; canary side-effect suppression (§2.4) unchanged; this is a privileged path → **auditor-grade two-pass review** on the release-pipeline changes ([[two-pass-review-workflow]]).

---

## D4 — Soak-health baseline-diff by signature (fleet-jam fix)

**Status:** built — privileged release-pipeline change, auditor-grade two-pass review required before merge
**Surfaced by:** `META:model-tiers` (observed every candidate failing the soak on the same four standing signals)
**Owned by:** `META:diligence`
**Amends:** the passive soak-health check (`_default_soak_health`) only. The active probe (D2, `soak_probe.py`) — *modulo the 2026-06-23 silent-send amendment in §D2* — the risk tiers (D1), the release pointer, rollback, and canary side-effect suppression are all unchanged. (D4 itself does not touch the active probe.)

### The bug (was jamming the whole fleet)

Under `pod.release.mode=canary`, Gate 2 promotion is gated on soak HEALTH = "no NEW firing signals on the canary." `_default_soak_health` decided "new" by reading each canary-scoped firing Signal's **last `to_state="firing"` transition timestamp** and failing the soak if it was at/after `soak_started_at`.

That predicate keys on a transition **timestamp**, not on signal **identity**. The canary (a low-traffic personal bot) carries standing app-quality debt on its own apps — e.g.:

```
app_structural_verifier:app_discoverability_thin_hint_words
app_structural_verifier:app_discoverability_no_example_triggers
app_structural_verifier:app_discoverability_no_cli
app_manifest_monitor:app_permission_drift
```

The canary's scheduled app scan re-asserts these standing conditions inside every soak window. A resolve→reopen (or a fresh observe outside the reopen window) stamps a NEW to-firing transition `>= soak_started_at`, so the standing debt is (wrongly) counted as a candidate-caused regression **on every candidate** — including admin / release / docs candidates that never touch the canary's apps. Result: stable stuck at #2816; every candidate since #2807 failing identically; the gate yielding zero candidate-discriminating signal; the only bypass `release pin` (force, freezing auto-promotion).

### The fix — baseline-diff by signature

Capture a **baseline** of the canary's active-signal `signature` set on STABLE code, at the `checking→soaking` edge **before** the candidate is deployed to the canary. During the soak, compare the canary's firing-signal set **under the candidate** against that baseline and fail only on signal **`signature` values present under the candidate but NOT in the baseline**.

- **Why `signature`:** `Signal.signature` (producer:type:scope_key, see `schema.signal.make_signature`) is the stable, producer-computed dedup identity. It is deterministic per condition and is **reused** by both the reopen path and the fresh-observe path — so it survives the resolve→reopen cycle that re-stamps the firing transition. Diffing by signature is therefore immune to the re-fire mechanism that defeated the timestamp predicate.
- **Why baseline (not category-exclusion):** excluding the `app_*` producer categories would also blind the gate to a candidate that *genuinely* breaks one of the canary's apps. Baseline-diff is strictly more correct: it is **category-agnostic** — it catches a genuinely-new regression from any producer whose `signature` keys on the offending entity (heal, watchdog, and the crash/send/mutate class, all keyed on `{bot_id}`) while excluding standing debt from any producer. **One honest exception — rollup-signature producers** (added 2026-06-14, post-hoc auditor review of D4 on #2832): a producer whose `signature` rolls findings up by *kind* rather than by individual finding — e.g. `app_manifest_monitor`'s `app_permission_drift`, signature `{bot_id}:{kind}` — will **not** surface a candidate-introduced finding of an **already-firing kind** on a *different* app, because that new finding folds into the existing rollup signal's signature and never appears as a new signature in the diff. This blind spot is confined to the **info/warn app-config-debt class D4 deliberately stops jamming on**; the **crash/send/mutate class keys on `{bot_id}`** (one signal per affected bot) and **is** caught. So the precise claim is: baseline-diff catches a genuinely-new regression from any producer in the crash/send/mutate class, and from any non-rollup producer generally — not literally *every* signature shape.
- **Capture scope:** firing **and** snoozed (`iter_active` with no `state=` filter). Snoozed ambient signals are included so that an ambient signal which un-snoozes during the soak is not mistaken for a candidate-caused regression.
- **Residual / future hardening:** the rollup-signature blind spot above would close with a per-finding `kept_signatures`-style diff for rollup producers (compare the *set of underlying findings* a rollup signal represents, not just its rolled-up signature). Deliberately deferred as tolerable: the gap is confined to the info/warn app-config-debt class, and even there the trade is favorable — pre-D4, that same standing app-debt was failing the soak on **every** candidate (jam-the-whole-fleet false-positive); post-D4 the worst case is a single missed info/warn finding of an already-firing kind on the canary's own apps, recoverable post-promote by the same heal/watchdog Signals + one-command `release rollback` the slow-failure class already relies on.

### Mechanism

1. **Capture** — `tick()`, at the `checking→soaking` transition, just before the canary deploy (the canary still runs stable code there), calls `_capture_canary_signal_baseline(shared_dir, canary_bot, deps)` and persists a **sorted, JSON-serializable list** in `state.candidate["soak_baseline_signatures"]`. The capture is **persisted before the canary deploy** (its own `save_release_state`) and **guarded against re-capture** (a `not in state.candidate` key-presence check, *not* a truthiness check). Why both: the soaking-flip's save lands only **after** the canary deploy **and** the ≤900s active probe; if the daemon dies anywhere in that window, on-disk state is still `checking` with the canary already on **candidate** code, and the next tick re-enters this block. Re-capturing there would baseline-in any candidate-introduced firing signal — silently passing a passive-only regression (e.g. a heal/watchdog crash-loop) the active probe can miss to the whole fleet. Persisting before the deploy + the key-presence guard keep the baseline **stable-code-derived** across that crash-restart. Defensive: a capture fault stores `[]`; the key is then present, so the guard does not re-capture against candidate code, and the empty baseline takes the fail-safe below. The `skip` tier promotes before any canary deploy, so it captures no baseline (correct — no soak runs).
2. **Diff** — `_default_soak_health(shared_dir, canary_bot, since_iso, baseline_signatures=None)`: an offender is a **firing** signal whose `signature ∉ baseline`. Offenders are reported as `producer:type` (capped at 5; message shape "new firing signals on canary: …" preserved).
3. **Fail-safe fallback** — when `baseline_signatures` is None/empty (a pre-D4 candidate already mid-soak across the upgrade, or a capture fault), the check **falls back to the legacy `since_iso` timestamp predicate** so the gate NEVER silently passes everything. New candidates always capture a baseline, so the diff path is the norm. The existing try/except → "soak check errored … not failing soak on tooling" (fail-open on tooling fault) is preserved.

### Invariants preserved

- **Never fail a release on our own tooling fault** (capture wrapped in try/except → `[]`; health check fail-open on exception).
- **Never silently pass everything** (no baseline ⇒ legacy predicate, not a blanket pass).
- Gate 1 always-on; active probe (D2) untouched; release pointer / rollback / canary side-effect suppression untouched.

### Proof artifacts (`test_release_manager.py`, `-k d4`)

- `test_d4_ambient_signals_refiring_passes_soak` — the exact bug scenario: baseline `{sigA,sigB}`, both re-fire after soak start → `(True, "no new canary signals")`. **The jam is gone.**
- `test_d4_genuinely_new_signature_fails_soak` — baseline `{sigA}`, canary fires sigA + a NEW sigZ → `(False, …)` naming sigZ and NOT sigA.
- `test_d4_baseline_captured_before_canary_deploy` (+ `test_d4_skip_tier_captures_no_baseline`) — through `tick()`: a short-tier candidate captures the pre-deploy active (firing+snoozed) set into `soak_baseline_signatures` and threads it to the check; a skip-tier candidate captures none and never consults the check.
- `test_d4_no_baseline_falls_back_to_since_predicate` — `None`/`[]` baseline → legacy since-predicate (fires-after ⇒ unhealthy, fires-before ⇒ healthy). No pass-all.
- `test_d4_crash_during_soak_entry_reuses_stable_baseline` (A1) — first `checking` tick persists the stable baseline `{sigA}` then the canary deploy crashes (daemon dies before the soaking-flip); the canary then emits a new candidate signal `sigZ`; the re-entry tick does NOT re-capture (capture invoked exactly once, baseline still `{sigA}`) and the diff FAILS on `sigZ` — the candidate regression is still caught across the crash-restart.
