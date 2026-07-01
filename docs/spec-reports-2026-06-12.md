# Reports surface — quality & trustworthiness (META:reports)

**Status:** seeded 2026-06-12. Design source of truth for the `reports` aspect.
**Owner:** `META:reports`.

---

## Mission

Own the **operator-facing quality and trustworthiness of everything surfaced under
the admin "Reports" tab** — Alerts (the Signal store), Proposals/Recommendations
(the Arbiter store), and Subscriptions (alert-delivery config). The product
promise of this surface is: *every item an operator sees is genuine, accurate,
clear, succinct-yet-thorough, and actionable.* When the surface drifts from that —
noise, duplicates, illegible titles, stale-firing ghosts, severity inflation — it
becomes wallpaper, and the operator stops trusting the whole engine.

This META's distinctive job (vs. the generation-focused aspects) is the **periodic
operator's-eye review**: read what's actually live in the local test pod, judge it
as an operator would, and drive the fixes — both in the surfacing layer and in the
producers upstream.

### Three-fold charter (from the operator kickoff)

1. **Periodic review** — regularly read the live alerts and proposals in the local
   test pod (mini) and assess: are they genuine? accurate? clear? succinct yet
   adequately thorough? actionable? Improve the surfacing functionality itself.
2. **Codebase learning** — for each noisy/wrong item, ask: is the root cause an
   *Evolve codebase bug* (a producer misfiring, junk fields, broken dedup) rather
   than a genuine environment condition? Surface the bigger code issue.
3. **Environment fix** — what steps clean up the local pod's stores (stale-firing
   signals, duplicate proposals) so the surface reflects reality.

---

## Scope boundary (this aspect vs. neighbors)

The Reports surface overlaps three existing concerns. To avoid double-ownership:

| Concern | Owner | `reports` role |
|---|---|---|
| **Signal/alert producer quality** (misfiring monitors, sweep_resolve gaps, signature fan-out, title/flavor calibration) | **`reports`** — no other aspect owns it | **Own + drive fixes** |
| **Cross-surface operator experience** (is the Reports tab as a whole legible/useful; Subscriptions/delivery) | **`reports`** | **Own** |
| **Proposal *generation* quality** (altitude ideation, Fit Reviewer, value_line, the legibility *contract* itself) | **`rsi`** (in flight: legibility B-series, Fit Reviewer Bites) | **Review & hand off**; don't rebuild rsi's work |
| **Signal store mechanics / state machine / spec** | `alerts-signal-store` (spec `docs/spec-alerts-signal-store-2026-05-07.md`) | Consume; propose producer-side fixes |

Rule of thumb: **`reports` reviews the *output*; where a fix is in proposal
generation/legibility, dispatch it as `rsi` work (or an `[META:reports]` chip that
explicitly cites the rsi overlap). Where a fix is in a *monitor/signal producer*
or the *surfacing layer*, `reports` owns the build.**

---

## First periodic review — 2026-06-12 (baseline)

Live counts on the mini: **203 firing Signals, 147 pending Proposals.** Volume
itself is the headline finding — neither store is anywhere near "every item is
genuine and actionable." Two producers dominate and are the worst offenders.

### Alerts / Signal store (203 firing)

- **`app_structural_verifier` = 125/203 (62%).** Four verifier types
  (`scheduled_action_orphan_install`, `app_no_producer_surface`,
  `app_discoverability_no_cli`, `app_discoverability_no_example_triggers`) =
  114/203 (56%).
- **90 stale-firing** (last_observed before today); 56 `app_discoverability_*`
  from 06-08 stayed firing through two later verifier runs — **sweep_resolve is
  not clearing pod-wide** (stuck-firing leak).
- **Signature fan-out**: one host-level orphan plist becomes 18/13 signals because
  the signature keys on `app_id`, not the plist/label. Includes **false positives
  on third-party plists** (Dropbox updater flagged as an app orphan).
- **Illegible titles**: 100+ titles are `<id>: <type>` (the signature echoed back),
  not a human one-liner.
- **Flavor miscalibration**: 197/203 framed as `maintenance` ("something is
  broken"); info/activity items (model_discovery, "informational" warden notes)
  defaulting to maintenance.
- **Alert-as-wallpaper**: observation_count 1623× / 837× / 289× on items that have
  re-fired every ~15 min for 17 days and never been actioned or cleared.

### Proposals / Arbiter store (147 pending)

- **`app_audit_tier3` = 118/147 (80%).** This generator *is* the queue.
- **Empty operator-facing fields**: all 118 have empty `claim`, `summary`,
  `human_title`, `explanation`, `signature`; `problem` == a one-word label. The
  rich detail exists in `action.context` but is never projected into the card.
- **Dedup defeated**: empty `signature` ⇒ find-or-create can't dedup; 70
  proposals are exact duplicates; 118 collapse to ~30 distinct (bot, app) pairs.
- **No `altitude` / `value_line` field on disk** — Fit Reviewer Bite 2 / value_line
  schema not deployed to this pod (these are **rsi** lanes).
- **Severity inflation**: 85 marked `operational_urgent` with empty bodies for what
  is manifest-drift hygiene.
- **`model_discovery` phantoms**: surfaced `gpt-4o` (>1yr old) as a "new model" and
  a Grok **video** model into an LLM rung — no recency/modality filter.
- **`surface` is None on all 147** — the Improvements/Health routing field unset.

### Codebase bugs revealed (charter goal #2)

1. `app_structural_verifier` — orphan-install **false positives** on non-Evolve
   plists + **signature fan-out** (key on app_id, should be plist label) +
   **sweep_resolve not run pod-wide** + **`<id>: <type>` titles**. Home:
   `packages/analyzer/generators/.../app_structural_verifier` (runner_version 1.3.0)
   and its `signals.store.observe`/`sweep_resolve` calls. *(reports owns)*
2. `app_audit_tier3` — **doesn't project `action.context` into legibility fields**,
   **empty signature defeats dedup**, **severity inflation**. Home:
   `packages/analyzer/generators/app_audit_tier3` (+ audit_poller / proposal
   builder). *(generation bug — coordinate with rsi; reports can own the
   field-projection + signature fix as it's the same family as the verifier bug)*
3. `model_discovery` — **no recency/modality gate**. *(rsi-adjacent; small)*
4. **Flavor/severity defaults** misassigned (maintenance vs activity; urgent vs
   hygiene) across producers. *(reports owns — surfacing calibration)*
5. The **legibility contract** (coalesce_key + count-agnostic human_title) works
   where wired (`app_permission_review`, `model_discovery`) but the two highest-
   volume producers never adopted it. The fix pattern is **pilot-then-ratchet the
   contract onto the remaining producers** — exactly rsi's stated approach. *(rsi
   for proposals; reports applies the same pattern to signal producers)*

### Environment-fix steps (charter goal #3)

- **After** the producer fixes land + deploy, the stale-firing signals and dup
  proposals should clear on the next run via sweep_resolve / dedup. Until then the
  pod shows ghosts.
- A one-time cleanup of the 90 stale-firing signals + 70 dup proposals may be
  warranted to get a clean baseline — via the proper `signals.store` /
  `arbiter.store` resolve paths, **not** by hand-deleting JSON (CLAUDE.md rule).
  Decide cleanup-before-fix vs. let-the-fix-sweep after the producer PRs.

---

## Backlog (proposed, unsequenced)

- **R1 — verifier noise fix (reports)**: third-party-plist exclusion + signature on
  plist label + reliable pod-wide sweep_resolve. Biggest single noise win (~31+
  false signals, unblocks 90-stale cleanup).
- **R2 — tier3 legibility + signature (reports/rsi)**: project `action.context`
  into human_title/summary/claim; set a real signature so dedup fires. 80% of the
  proposal queue.
- **R3 — flavor/severity calibration (reports)**: default discoverability/model/
  info to `activity`; reserve `maintenance`/`operational_urgent` for true breakage;
  ban `<id>: <type>` titles (a producer-side lint).
- **R4 — coalesce ratchet (rsi, reports tracks)**: per-(bot,app) rollup for tier3;
  per-bot digest for verifier discoverability findings.
- **R5 — model_discovery recency/modality gate (rsi)**.
- **R6 — env cleanup pass (reports)**: clear stale-firing + dup baseline.
- **R7 — Subscriptions review (reports)**: not yet reviewed this bout — the
  alert-subscription toggles + dispatcher health (`alerts-extended.js`,
  `routes_alerts.py`) are the third leg of the surface; assess delivery accuracy.

---

## R8 — Alert-delivery resilience: no flood on recovery (designed 2026-06-23)

**Trigger (live, evo VPS pod):** a notification channel was down for hours
(Telegram `chat not found` — new bot the operator hadn't `/start`ed), and when
delivery recovered the operator was **flooded**: two backlogs dumped at once —
(a) OpenClaw's `delivery_queue_entries` replayed ~800 wedged entries, and
(b) `signal_notifier` re-pushed every firing signal at once. Issue
[#3152](https://github.com/evolve-ops/evolve/issues/3152) (bug report + flood
follow-up comment). This is a generic hazard for **any** pod where a channel is
down for a stretch then recovers — fresh-pod bring-up is just the most common
trigger. See [[feedback_fresh_pod_bot_needs_dm_start_telegram]].

**Root cause (code-level):** `signal_notifier` only suppresses a signal when the
dispatcher returns `is_permanent_failure=True`, set by
`dispatcher._is_permanent_failure(error)` which matches only `"http 4"` in the
error string. The live pod delivers via the **openclaw CLI path**
(`_dispatch_via_openclaw`), whose error is unstructured
(`Telegram send failed: chat not found (chat_id=…)`, **no `http 4`**) — the
docstring concedes the CLI path "doesn't expose structured error codes today, so
its failures are treated as transient." So a permanent 400 is misclassified
transient → notifier leaves it `unmarked, retried next tick` → per-minute retry
for hours → mass announce on recovery. (The direct `_dispatch_via_telegram_http`
path *would* catch it — it prefixes `telegram http 400` — but the gateway path is
what's live.)

**Design — levers (operator chose full resilience, 2026-06-23):**

1. **Classify openclaw-path permanent failures** *(stop the loop).* Normalize
   `_dispatch_via_openclaw`'s error into a structured permanent-failure verdict
   (the `OutboundDeliveryError` text already names "chat not found" / "bot was
   blocked" / "wrong bot token"). `chat not found` then trips the existing
   `permanent_failure_signal_id` suppression. **Stay conservative** — over-eager
   "permanent" suppresses a legit alert; match a small explicit allowlist of
   permanent shapes, default unknown CLI errors to transient as today.
2. **Quiet re-sync on recovery, not a flood** *(resilience policy).* When a target
   transitions down→up, cold-start-style re-sync the current firing set (mark
   known) and emit **one digest** ("N alerts accumulated while delivery was down;
   see Alerts page") instead of replaying each. → **Chip A** (with lever 1; same
   subsystem `evolve_admin/alerts/{dispatcher,signal_notifier}.py`).
3. **Delivery-queue dead-letter** *(boundary — OC-upstream).* The
   `delivery_queue_entries` replay is OpenClaw's queue, not Evolve's.
   `chat not found` should dead-letter there, not blind-replay. Per
   [[feedback_dont_reimplement_upstream]] this is an OC-upstream fix or a thin
   Evolve-side reconciler — **flag, don't rebuild**. Tracked on #3152 (queue half).
4. **First-class "alert delivery down" Signal card** *(reports core).* Upgrade
   `alerts_loop_monitor`'s `dispatcher_failures` Signal to be actionable +
   remediation-bearing ("delivery to `<target>` is DOWN (chat not found) — N
   undelivered since `<t>`; remediation: `/start @<bot>`"). This is the one alert
   that must surface in the **admin UI**, since the channel itself is the broken
   path. → **Chip B** (`packages/analyzer/alerts_loop_monitor.py` — distinct files
   from Chip A, parallel-safe).

---

## Deploy mechanism

Admin-only. Signal producers / generators live in the packaged `evolve-analyzer`
and run via scheduled daemons / `generator_runner`; UI lives in the admin SPA.
Merge → repo-puller ≤15 min on the mini. **UI changes** → admin-ui kickstart.
**Producer/monitor changes** → kickstart the relevant daemon (the monitors run as
scheduled jobs; the analyzer is a packaged compat-editable install, so a stale
module cache after pull may need a daemon kickstart — see
`[[pull-deploy-stale-module-cache]]`). Pod runs `pod.release.mode=canary`, so a
producer change is canary-gated like any other code change.

## Key invariants / guardrails

- **Review the output as an operator would** — judge live pod state, not code in
  the abstract; the periodic review is the heartbeat of this aspect.
- **Don't double-own rsi's proposal-generation work** — review & hand off.
- **No hand-editing the signal/proposal JSON on the mini** — use `signals.store` /
  `arbiter.store` resolve paths (CLAUDE.md).
- **A finding on every bot of a producer = an Evolve bug, not per-bot drift**
  (`[[pod-wide-fingerprint-is-evolve-bug]]`).
- **Producer-side legibility is a contract, ratcheted** (coalesce_key +
  count-agnostic human_title + non-empty signature + human title) — pilot then
  ratchet onto remaining producers; no LLM editor brain.
- Style-guide + both-theme check on any SPA surface touched.
