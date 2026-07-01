# Spec — Fresh-pod alert protection (settle window + flap hysteresis + maturity gate)

Status: **active** · Created 2026-06-23 · META:platform / META:reports

Umbrella contract for the complementary mechanisms that stop Evolve monitors
and summaries from paging the operator on **non-durable** or **not-yet-real**
conditions — most acutely on a freshly-provisioned pod, which must NOT spam its
operator. They are independent and composable; a producer may adopt any subset. A fourth
piece — a **coverage registry + CI lint** (§ "Protection by construction") —
turns the opt-in mechanisms into a *contract*: no operator-facing producer can
ship without declaring how it behaves on a fresh/immature pod.

| # | Mechanism | Quiets | Module | Status |
|---|-----------|--------|--------|--------|
| 1 | Bring-up **settle window** | the *one-time* fresh-pod flurry while the deploy is still cycling `harden → ACL-reassert` | `signals.settle_gate` | shipped (#3186; spec [spec-pod-bringup-settle-2026-06-23](spec-pod-bringup-settle-2026-06-23.md)) |
| 2 | Self-healing **flap hysteresis** | *recurring* fire→clear→fire flaps (e.g. the evolve-read ACL re-clamped hourly and auto-restored) | `signals.flap_gate` | shipped (#3197) |
| 3 | Pod-maturity **"no-data-yet" gate** | a history-dependent signal/summary that pages a scary state on a brand-new pod purely *because no history has accumulated yet* (RSI Weekly Review → `🔴 At Risk` on a 1-day-old pod) | `signals.maturity_gate` | this PR |

Mechanisms 1 & 2 sit **in front of** `signals.store.observe` at the producer's
emission point. Neither hand-edits Signal JSON; once a condition is allowed to
page, the producer's normal `store.observe(**spec)` runs and find-or-create /
reopen / `sweep_resolve` semantics are unchanged. Mechanism 3 is the same shape
— a predicate consulted at the producer's emission point — but applies to
**summary producers** too (the RSI Weekly Review is a dispatched Markdown
report, not a Signal); it reframes the producer's *own* output rather than
gating a `store.observe` call.

---

## Motivation

A fresh Linux pod (bots `evo`/`darwin`) emitted a large alert flurry. Two
non-durable patterns dominated:

1. **Bring-up transients.** Steady-state monitors (OpenClaw security audit,
   Sysadmin Watchdog, infra audit) fire immediately on a newly deployed pod
   while perms/ACLs are still being re-asserted — most prominently "evolve
   cannot read `.openclaw`". Mechanism 1 owns this.
2. **Self-healing flaps.** The evolve-read ACL was re-clamped hourly and
   auto-restored, so the same `acl_drift` / `pod_perms_drift` condition fired,
   cleared, and fired again every cycle. The system even *detected* the
   pattern — the meta-alert `signal_notifier is on repeat: same alert sent 5×
   in 24h` — but only after paging 5×. **A condition that clears within one
   observation cycle should never have paged.** Mechanism 2 owns this.

---

## Mechanism 2 — flap hysteresis (`signals.flap_gate`)

### Rule

A **transient-prone** condition must be observed on **N ≥ 2 consecutive
monitor cycles** before it pages (`DEFAULT_DWELL_CYCLES = 2`). A condition
that clears before the dwell elapses is logged but never reaches the Signal
store. Once a condition *has* paged (its Signal is firing), re-observation
pages immediately — a genuinely persistent condition is delayed at most by the
first dwell, and a firing Signal is never starved into a sweep-resolve.

### Transient-prone types

Flap-eligible by default (substring match on Signal `type`, case-insensitive):
`acl`, `perm`, `workspace_unreadable`, `unreadable`, `file_mode`, `filemode`,
`mode_drift`, `secret_mode`. Producers override per-condition via the
`transient=` flag (both wired producers pass `transient=True` explicitly).
Non-transient types page immediately — genuinely critical, persistent
conditions (gateway down, daemon not loaded, sudoers invalid) are **never**
dwell-delayed.

### State

One tiny JSON file per dwelling signature at
`{shared_dir}/signals/pending/<sha256(signature)[:24]>.json`:

```json
{"signature": "...", "type": "acl_drift", "count": 1, "dwell_cycles": 2,
 "first_observed_at": "...Z", "last_observed_at": "...Z"}
```

The dwell key **is** the Signal store's dedup `signature` — "reuse the
signature machinery" — so promotion hands straight off to `store.observe`.
Per-signature files keep the read-modify-write lock-free (one single-instance
monitor writes a given file). Entries are removed on promotion, on
`note_cleared` (condition absent), and by `sweep_stale` (last seen >
`PENDING_STALE_SECONDS = 3h` ago — a crash/disabled-detector backstop).

### Producer contract (per cycle, per transient-prone signature)

* condition ABSENT  → `flap_gate.note_cleared(shared_dir, signature)`
* condition PRESENT → `verdict = flap_gate.note_observed(...)`; emit the Signal
  iff `verdict.page`.
* paired Proposal (the ACL-restore fix) → gate on
  `flap_gate.signal_is_active(shared_dir, signature)` so it fires in lock-step
  with its Signal. The generator runner always runs `observe_signals()` before
  `observe()`, so within a cycle the Signal is already written (or withheld)
  when the proposal detector checks.

### Fail-open

Matching `settle_gate`: any ledger read/write or store-lookup error **pages**
rather than risk swallowing a persistent condition.

### Wired producers (this PR)

| Producer | File | Signal type |
|----------|------|-------------|
| Sysadmin Watchdog — ACL drift (Signal **and** autonomous Proposal) | `generators/sysadmin_watchdog/detectors/platform/acl.py` | `acl_drift` |
| Pod perms drift monitor (also gains the settle gate) | `pod_perms_drift_monitor.py` | `pod_perms_drift` |

`pod_perms_drift_monitor` applies both gates in order: settle (withhold the
warn-level drift while unsettled), then flap (dwell once settled).

---

## Mechanism 3 — pod-maturity "no-data-yet" gate (`signals.maturity_gate`)

### The failure mode

Mechanisms 1 & 2 quiet *transient* conditions. A third class slips past both: a
producer whose output is computed from **accumulated history** reports a scary
state on a brand-new pod purely *because there is no history yet*. The canonical
live case is the **RSI Weekly Review** (`summaries.weekly_rsi_review`) on a
1-day-old pod:

```
RSI Cycle Health  🔴 27/100 — At Risk
Approval Rate (35%) 0/100   Measurement Rate (25%) 0/100
This week: 2 proposals · 4-week avg: 0.0/week
```

The `0/100` scores are *warming up*, not *at risk* — there has not yet been a
single prior weekly cycle to approve, apply, or measure. The settle window has
long since closed (it caps at 30 min) and nothing is flapping, so neither
existing gate applies.

### Rule

A producer declares a **maturity requirement** along two optional dimensions:

* **pod age** (`min_age_days`) — time since first bring-up, read from the *same*
  marker the settle gate established. `settle_gate.pod_age()` /
  `pod_first_seen()` resolve `pod-bringup.json::started_at` (write-once ⇒
  durable pod birthday), falling back to `pod-settled.json::settled_at`. Reusing
  that anchor means "pod age" has **one definition** across all three
  mechanisms.
* **data-point count** (`min_data_points` vs a producer-supplied `data_points`)
  — the count of accumulated records the output needs (for the RSI review:
  proposals that entered the 30-day funnel window, the denominator the
  approval/measurement rates require). The gate never gathers this — the
  producer passes the number it already computed.

A producer is **mature** only when **every declared dimension is satisfied**
(old-enough **AND** data-rich-enough). Equivalently it is "warming up" when
*either* is short — the conservative direction, since a low score born of no
history is exactly what must not page.

When immature, the producer reframes its **own** output to an informational
"warming up / insufficient data" state — **never** warn/red — or withholds it.
The gate is a pure predicate (`maturity_gate.evaluate(...) -> MaturityVerdict`);
it does not touch Signal/summary JSON.

### Fail-open

Any uncertainty resolves to **mature** (show the real content): undeterminable
pod age (no markers ⇒ an established/upgraded pod) satisfies the age dimension;
a declared-but-unsupplied `data_points` satisfies the data dimension; an import
or evaluation error in the producer falls back to scoring normally. The gate
must never silently hide a fully-grounded finding.

This is the *opposite* fail-direction from mechanisms 1 & 2 (which fail-open to
*paging*), and correctly so: there the risk is swallowing a real alert; here the
risk is hiding a real score, so "when in doubt, show it" lands on mature.

### Thresholds (RSI Weekly Review)

`RSI_REVIEW_MIN_AGE_DAYS = 14` (two full ISO weeks — enough for a real
week-over-week comparison and a non-trivial funnel denominator, without delaying
the first genuine review by a month) **AND** `RSI_REVIEW_MIN_DATA_POINTS = 1`
(at least one proposal in the funnel window, so approval/measurement rates have
a real denominator). A 14-day-old pod that has produced literally zero proposals
still reads "insufficient data", not "at risk" — the genuine "RSI went silent"
concern is a separate producer-liveness signal, not this summary's job.

### Wired producer (this PR) — `weekly_review.py`

* Consults `maturity_gate.evaluate(...)` with the RSI thresholds; on an immature
  pod `build_report` renders **`🌱 Warming up — insufficient history to score`**
  in place of the `🔴 … At Risk` header, and LLM synthesis is skipped (there is
  nothing to reason over).
* **Empty-proposal filter** (`_is_empty_proposal`): pending proposals with no
  usable description **and** zero confidence (the `[] No description (0%
  confidence)` rows) are dropped from the **display** (Decisions Needed + the
  LLM context) — but **not** from the rsi-owned health scoring, which still sees
  the raw pending list (boundary: reports owns presentation, rsi owns scoring).

### Double-fire fix (idempotency)

The review fired **twice ~15 min apart** on the fresh pod (a launchd make-up /
re-bootstrap double-fire). The dispatcher `dedup_key` is `weekly_review/<ISO
week>` but the source was `PER_EVENT_UNIQUE` (0s cooldown) and the report body
embeds a live `Generated:` timestamp, so the identical-content floor never
matched a same-week re-fire. Two changes, mirroring the sibling
`weekly_bot_trends`:

1. **Producer-level ISO-week sentinel** — `weekly_review._already_sent_this_week`
   / `_mark_sent_this_week` records the last-sent ISO week at
   `{shared_dir}/state/weekly_review/last_sent_week`. `main()` skips the whole
   run (before any LLM cost) when the current week is already stamped; the stamp
   is written only on a real `SENT`. `--always` bypasses it for debug.
2. **Dispatcher backstop** — `weekly_review` flips to `STATE_PERSISTS` (24h
   cooldown on the per-ISO-week dedup_key) as a belt-and-suspenders guard for
   the rare case the sentinel write fails. The schema `stock_default` moves to
   match (`config_sandbox/schema.py`).

---

## Protection by construction — the coverage registry + lint

The three mechanisms above are powerful but **opt-in per producer**: each gate is
a seam a producer chooses to call in front of its `store.observe`. Nothing forces
a producer to adopt the right one, so the *next* new monitor can reintroduce
exactly the fresh-pod flurry these mechanisms were built to stop. This section is
the enforcement layer that makes fresh-pod protection a property of the system,
not a habit each author must remember.

### The coverage registry

`packages/analyzer/signals/protection_registry.py` enumerates **every
operator-facing Signal / summary producer** and the fresh-pod protection
**posture** it declares — one of four:

| Posture | Meaning | Mechanism |
|---------|---------|-----------|
| `settle`   | gated by the bring-up settle window | 1 (`settle_gate`) |
| `flap`     | dwell hysteresis for transient-prone conditions | 2 (`flap_gate`) |
| `maturity` | no-data / pod-maturity gate | 3 (`maturity_gate`) |
| `none`     | **deliberately always pages** — requires a `justification` | — |

A producer may declare **multiple** non-`none` postures (e.g. `pod_perms_drift`
is `settle` + `flap`). `none` is **exclusive** — it means "no gate", so it cannot
be combined with one.

**The `none` escape hatch is correct and expected**, not a failure. A
genuinely-critical, must-page-immediately condition — gateway down, daemon not
loaded, sudoers invalid, the security floor (`security_warden`), host resource
exhaustion (`host_health`), a cost-breaker trip — *should* page on a fresh pod,
and declares `none` **with a required `justification` string** explaining why.
The registry enforces a **declared** posture, never a non-`none` one: **do not
over-gate a critical alert to satisfy the contract.** Delaying a real
privilege-exposure or outage alert is a worse failure than a fresh-pod nuisance.

### The `gap` flag — honest backfill, not silent re-wiring

Backfilling the registry against the live wiring surfaced producers that *can*
false-fire on a fresh or immature pod but are **not yet gated** (e.g. the
config/posture drift monitors `deploy_drift_monitor`, `permission_monitor`,
`integration_probe`, …; the history-dependent `weekly_bot_trends`,
`pod_report`'s no-history buckets). The boundary of this work (reports owns the
*contract*; each subsystem owns its producer's *behavior*) means these are
registered at their **current** posture (`none`) and flagged `gap=True` with an
owner note — **not** re-wired here. `tools/signal-protection-lint --gaps` lists
them; each is a tracked follow-up for its owning aspect. `gap` is metadata on a
`none` entry; it does not change what the lint requires (a justification either
way).

### The lint — `tools/signal-protection-lint`

Mirrors the established lint pattern (`tools/sudo-grant-lint`,
`tools/platform-path-lint`, `tools/scheduler-factory-lint`). AST-based, so
comments and docstrings that mention `store.observe` are ignored. It enforces
three rules:

1. **File coverage (BLOCK).** It statically finds every Signal-store
   `observe(...)` call site in `packages/{admin,analyzer}` — identified by the
   call *shape* (a `**kwargs` splat, a Signal kwarg like `producer=`/`type=`, or
   ≥2 positional args; this deliberately excludes the generator `mod.observe(ctx)`
   proposal call) — and asserts the file is claimed by some registry entry's
   `emits_from`. A new `observe()` site in an unregistered file is a new producer
   with no declared posture → **fail**, pointing at the registry.
2. **Registry consistency (BLOCK).** A `none` producer must carry a
   justification; `none` is exclusive; `gap` is `none`-only. (Also asserted at
   import time, so the registry can never be in a state the lint would reject.)
3. **Name reconciliation (WARN; BLOCK under `--strict`).** Every producer in
   `signals.producer_severity.PRODUCER_SEVERITY` and every dispatcher source in
   `alerts.dispatcher._DEFAULT_SOURCE_CATEGORY` must have a registry entry — so a
   producer added to those maps but not the registry is caught. Warned locally
   (the call-site→producer mapping can be momentarily ambiguous mid-edit), blocked
   in CI.

Hybrid severity mirrors `ui-style-lint`: the high-confidence rules (1, 2) block
unconditionally; the heuristic reconciliation (3) warns by default and blocks
under `--strict`. Wired into the CI gate set (`.github/workflows/ci.yml`,
alongside the other ratchet/lint gates, run as `--all --strict`) and the
pre-commit hook (`.githooks/pre-commit`, `--staged`).

---

## Proof artifacts

`packages/analyzer/tests/test_signal_maturity_gate.py` (Mechanism 3 gate):

* **Young pod → warming up.** A 1-day-old pod against a 14-day requirement is
  not mature; the reason names the age shortfall.
* **Mature pod → real content.** A 40-day pod with history is mature.
* **Data dimension.** An old pod with zero accumulated data points is still
  "insufficient data"; both-short names both shortfalls; the age boundary is
  inclusive (`>=`).
* **Fail-open.** Unknown age (no markers) satisfies the age dimension; an
  unsupplied `data_points` satisfies the data dimension; no thresholds ⇒ always
  mature. `settle_gate.pod_age` reads the bring-up marker and falls back to the
  settled marker.

`packages/analyzer/tests/test_weekly_review_maturity.py` (reports wiring):

* **Warming-up render.** A zero-/low-history pod renders `🌱 Warming up` and the
  scary `🔴` / `At Risk` / `27/100` verdict is absent; a mature pod renders the
  real `🟢 84/100 — Healthy` unchanged; an absent `maturity` key (old callers)
  renders normally.
* **Empty-proposal filter.** `_is_empty_proposal` keeps real-description /
  real-confidence proposals and drops blank 0%-confidence placeholders; they
  never reach Decisions Needed.
* **Idempotency.** Two `main()` runs in the same ISO-week emit exactly once; a
  stale sentinel (new week) emits again; `--always` re-emits.

`packages/analyzer/tests/test_signal_protection_registry.py` (registry + lint):

* **Unregistered site → fail.** A new file with a `signals_store.observe(...)`
  call for an unregistered producer is flagged by `find_unregistered`; the
  generator `mod.observe(ctx)` proposal call and docstring/comment mentions are
  not (no false trips).
* **All current producers pass.** `find_unregistered` over all production `.py`
  is empty; `reconcile_missing` is empty (every `PRODUCER_SEVERITY` producer and
  dispatcher source is registered); `--all --strict` exits 0.
* **`none` escape hatch.** A `none`+justification entry passes `entry_errors`; a
  `none` WITHOUT justification fails; `none` combined with a gate fails; a gated
  entry needs no justification.
* **Backfill integrity.** Every `emits_from` path exists on disk; every `gap`
  entry is a `none` posture with a justification; `--gaps` lists the tracked
  follow-ups.

`packages/analyzer/tests/test_signal_flap_hysteresis.py` (Mechanism 2):

* **Single-cycle flap → no page.** `note_observed` once (count 1 → dwelling),
  then `note_cleared`; no Signal is ever written.
* **Persist ≥ N → page.** Two consecutive `note_observed` calls promote; the
  producer then writes the Signal via `store.observe`.
* **Dedup/reopen preserved.** After promotion, re-observe bumps the firing
  Signal's `observation_count` (already-firing short-circuit); resolve →
  re-observe re-opens through `store.observe` exactly as before.
* **Proposal lock-step.** The ACL proposal is withheld while the Signal dwells
  and fires once the Signal is active.
* **Settle proof** stays in `test_pod_bringup_settle.py` (mechanism 1).

---

## Reversibility

All three gates are additive seams. Removing a producer's `flap_gate` /
`settle_gate` calls restores the prior fire-immediately behavior; the pending
ledger is self-pruning and ignored by everything else.

The maturity gate is likewise additive: removing the `maturity_gate.evaluate`
call from `weekly_review` restores the always-score behavior. The ISO-week
sentinel is a single self-overwriting file under `{shared_dir}/state/` and is
ignored by everything else; the `STATE_PERSISTS` flip is a one-line dispatcher
+ schema change reversible on its own. The only store-adjacent change is the
`weekly_review` cooldown default (0 → 86400), which an operator can override
per-pod via `alerts.weekly_review.cooldown_seconds`.
