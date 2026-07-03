# Footprint Dependency Graph + Classification (F-2.5) — 2026-06-18

**Aspect:** `META:footprint` · **Slice:** F-2.5 (the dependency audit that gates the
posture-engine design) · **Spec:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md)
("Safe-disable / dependency model" — FD-5/FD-6/FD-7) ·
**Input catalog:** [docs/footprint-catalog-2026-06-18.md](footprint-catalog-2026-06-18.md)

## Purpose

FD-5 (settled, operator 2026-06-18): the posture dial is a power-user **opt-OUT** —
default stays `full`, but disabling a component must be **dependency-aware** so the
disable *process* never cascades an install into a broken or half-on state. That
requires a declared dependency graph mapped **from code, not assumed**. This doc is
that graph: every component's `requires[]` / `required_by[]` / classification, each
edge cited to `file:line`, plus the half-state hazards the engine's preflight must
catch, the infra floor excluded from the v1 dial, and the cascade groups the engine
must disable-together-with-consent.

**Method.** Edges were traced by reading the consumer code (grep the gate, open the
reader, confirm it actually consumes the producer's output). Each row cites the line
that proves the edge. Edges that could not be confirmed in code are flagged
**UNVERIFIED** in §6 so the engine design knows what is still soft.

**Classification (exactly one per component):**
- **infra-floor** — disabling breaks the control plane / locks out recovery / the
  apply mechanism itself depends on it. **Excluded from the v1 dial** (spec FD-5 #2).
- **cascade** — other components consume its output; disabling must
  cascade-with-consent (show blast radius, disable the chain together).
- **safe-leaf** — nothing depends on it; safe to flip alone.

---

## 1. Component dependency table

`requires[]` = what must be ACTIVE for this component to function (it consumes their
output / needs that daemon, capability, or file). `required_by[]` = what no-ops or
breaks if this is disabled. Citations are the line that *proves* the edge.

### 1a. Infra floor (control plane / apply mechanism)

| Component | Classification | requires[] | required_by[] | Code evidence (file:line) | Notes |
|---|---|---|---|---|---|
| **Gateway plugin install** (`evolve` plugin entry in `openclaw.json`) | infra-floor | sudoers (write config), ACLs/`sudo cat` (read config), admin daemon (deploy) | **Everything runtime**: all tier capabilities, all content hooks, all ledgers, all registered tools | [deploy.py:2545-2550](../packages/admin/evolve_admin/deploy.py#L2545) (`plugin_entry.setdefault("enabled", True)`, unconditional); [provisioning.py:1227](../packages/admin/evolve_admin/provisioning.py#L1227) (provision fails if absent) | **The runtime root.** No plugin ⇒ bot reverts to vanilla OC — no hooks/routing/tools/ledgers. Installed unconditionally on every deploy. |
| **`allowConversationAccess` = true** (`openclaw.json::plugins.entries.evolve.hooks`) | infra-floor (for the content layer) | gateway plugin install | content hooks `llm_output` / `agent_end` / `before_agent_run` (the event payloads carry conversation content) | [deploy.py:2622-2627](../packages/admin/evolve_admin/deploy.py#L2622) (deploy-forced `true`; comment cites OC ≥2026.4.29 requirement) | Upstream-OC gate, NOT our plugin code (grep of `packages/plugin/src` = 0 hits). If false, OC withholds content-bearing events ⇒ observer captures nothing ⇒ annotations/summaries/cost data never produced. **No toggle exists today.** Floor for the *content/observe layer*, not for plugin presence. |
| **sudoers grant set** (`/etc/sudoers.d/evolve`) | infra-floor | — | apply mechanism (cp/chown/chmod), gateway kickstart, deploy, heal, provision, daemon install | [setup_wizard.py:2272-2281](../packages/admin/evolve_admin/setup_wizard.py#L2272) (cp+chmod grants); [setup_wizard.py:2436-2463](../packages/admin/evolve_admin/setup_wizard.py#L2436) (kickstart/bootstrap/bootout); [recovery.py:975-1008](../packages/admin/evolve_admin/recovery.py#L975) (the `sudo -n /bin/cp` + `chown` + `launchctl kickstart` calls) | **Lockout hazard.** Remove the write grants ⇒ admin can stage `/tmp/evolve-*.json` but cannot copy it live, cannot 0600 it (secrets stay world-readable), cannot kickstart. The apply mechanism that would *re-enable* a component depends on these. |
| **macOS `.openclaw/` ACLs** (`set_evolve_read_acl`) | infra-floor | — | config reads in deploy/apply/heal (with `sudo /bin/cat` as the only fallback) | [deploy.py:1261-1306](../packages/admin/evolve_admin/deploy.py#L1261) (`set_evolve_read_acl`); [deploy.py:2244-2272](../packages/admin/evolve_admin/deploy.py#L2244) (direct read → `sudo /bin/cat` fallback) | If BOTH the ACL and the `sudo cat` grant are absent, reads fail silently (EACCES under 0700 parent → treated as not-found). Reads are on the critical path for every config mutation. |
| **admin daemon** (`ai.evolve.evolve.admin-ui`) | infra-floor | sudoers, ACLs | Settings UI, proposal-approval surface, the module enable/disable endpoints (the UI recovery path) | [routes_analytics.py:2853-2862](../packages/admin/evolve_admin/web/routes_analytics.py#L2853) (`/api/modules/<m>/enable|disable`) | The control plane itself. If off, the only re-enable path is the CLI (`evolve-admin modules enable`, [cli.py:7877-7895](../packages/admin/evolve_admin/cli.py#L7877)) or hand-editing `network.json` off-box. |
| **repo-puller — security-update path** | infra-floor (security-update role only) | sudoers, deploy checkout | delivery of fixes to the fleet | [repo_puller.py:1-35](../packages/analyzer/repo_puller.py#L1) (15-min `git pull --ff-only` + restage/kickstart) | **Split classification.** The *convenience auto-pull* is a feature (manual `git pull` is a recovery path → cascade-ish). But spec FD-5 #2 names the **security-update path** as infra-floor: without any delivery path the pod cannot receive fixes. Treat the daemon's existence as floor; its *cadence* could be a dial knob later. |

### 1b. Runtime / hot-path (gateway plugin, gated by the `tier` ladder)

The `tier` ladder (`off`/`monitor`/`manage`/`full`, default **`full`**) is the real
runtime dial — [config.ts:40-45](../packages/plugin/src/config.ts#L40) (capability
map), [config.ts:114](../packages/plugin/src/config.ts#L114) (`?? "full"` default). It
is NOT a `DEFAULT_MODULES` key; FD-1 makes posture the authoritative concept that
*writes* this tier.

| Component | Classification | requires[] | required_by[] | Code evidence (file:line) | Notes |
|---|---|---|---|---|---|
| **`tier` ladder** (master runtime gate) | cascade | gateway plugin install | every capability below; observer→annotations/metrics/tuples chain | [config.ts:40-45](../packages/plugin/src/config.ts#L40) | `off` ⇒ all capabilities false (plugin loaded but inert). Dropping to `monitor` subsumes most hot-path cuts. |
| **`observer` capability** (content hooks: `llm_output`, `agent_end`, `session_end`) | cascade | tier ≥ `monitor`, `allowConversationAccess`, gateway plugin | annotations JSONL, session-summary JSONL, cost data, outward-action ledger → (downstream) `metrics`, `analysis`, `tuples` | [TurnObserver.ts:1402](../packages/plugin/src/observer/TurnObserver.ts#L1402) (`llm_output`), [:1479](../packages/plugin/src/observer/TurnObserver.ts#L1479) (`agent_end` → annotations), [:1499](../packages/plugin/src/observer/TurnObserver.ts#L1499) (`session_end` → summary) | **The producer of the whole observe→analyze data spine.** NB the `observer` *module key* in `DEFAULT_MODULES` gates nothing (accuracy bug, §6 / catalog flag #1) — the real gate is this tier capability. |
| **`modelRouting`** (`before_model_resolve` rewrite + spend-cap/runaway safety nets) | cascade | tier ≥ `manage`, `routing.enabled` kill-switch | model-override decisions; the spend-cap downgrade & runaway-rate cap fire here | [TurnObserver.ts:1688-1740](../packages/plugin/src/observer/TurnObserver.ts#L1688) (gate); [ModelRouter.ts:2726-2728](../packages/plugin/src/observer/ModelRouter.ts#L2726) (`isSpendCapActive` → downgrade); [ModelRouter.ts:193-196](../packages/plugin/src/observer/ModelRouter.ts#L193) (`runawayRateCap`) | Safety nets (spend-cap, runaway) live *inside* the routing hook — see §3 hazard: disabling routing also drops those interrupting safety breakers. |
| **`injectPodConduct`** (`session_start` persona injection) | safe-leaf | tier ≥ `manage` | nothing downstream consumes it | [TurnObserver.ts:1374](../packages/plugin/src/observer/TurnObserver.ts#L1374) | Pure mutation of the bot's system prompt; no reader depends on it. |
| **`injectKeywords`** (`before_prompt_build` / `before_agent_run` per-turn injection) | safe-leaf | tier = `full` | nothing downstream | [TurnObserver.ts:1527-1575](../packages/plugin/src/observer/TurnObserver.ts#L1527), [:1882-1884](../packages/plugin/src/observer/TurnObserver.ts#L1882) | Top of the ladder; flipping it off is the smallest, safest cut. |
| **`preflight` intent router** (per-turn haiku) | safe-leaf | `cascade.preflight.enabled` (default on), `_isHaikuEnabled` | nothing downstream (Phase 1 returns ABSTAIN) | [TurnObserver.ts:906-935](../packages/plugin/src/observer/TurnObserver.ts#L906) (`_isPreflightEnabled`, default `true`) | Own kill-switch; safe leaf. |
| **outward-action ledger** (per-MCP-call JSONL) | cascade | tier ≥ `monitor` (written in `agent_end`), gateway plugin | autonomy caps (rung-3 `record_application` limits read it to count actions) | [OutwardActionLedger.ts:154-219](../packages/plugin/src/observer/OutwardActionLedger.ts#L154) (write); instantiated unconditionally [TurnObserver.ts:1121-1126](../packages/plugin/src/observer/TurnObserver.ts#L1121) | **Deliberately un-gateable** — the docstring states a kill-switch here would silently disable an operator-set limit. The contract must decide caps exist-or-not, never half-disable (spec). |
| **registered tools** (`defer`, `session.set_tier`, `record_application`) | cascade (tied to their tier) | tier (`manage`/`full` per tool) | autonomy ladder (`record_application` ↔ outward ledger / caps) | [index.ts:111-142](../packages/plugin/src/index.ts#L111) | `record_application` at `manage`+; gating it below `manage` removes the autonomy-cap write tool. |
| **roster tools** (`set_role`/`block`/`unblock`/`newcomer_mode`) | safe-leaf | gateway plugin (registered unconditionally on every bot) | nothing in the footprint dial | [index.ts:188-201](../packages/plugin/src/index.ts#L188) (unconditional) | Tool-surface token cost only; per-call auth at the daemon. Not a dial target. |

### 1c. Analysis / RSI data chain (Python analyzer)

| Component | Classification | requires[] | required_by[] | Code evidence (file:line) | Notes |
|---|---|---|---|---|---|
| **`metrics`** (`measure.py`, daily aggregation) | cascade | annotations (from observer `session_end`/`agent_end`) | metrics-dependent detectors in `analysis` | [measure.py:165](../packages/analyzer/measure.py#L165), [:193-199](../packages/analyzer/measure.py#L193) (reads `annotations/<bot>/<date>.jsonl`, filters `type==session_summary`); gate [measure.py:475](../packages/analyzer/measure.py#L475) (`metrics` module) | **Hard dep on the observe layer.** No annotations (observer off / `allowConversationAccess` false) ⇒ empty metrics. Runs daily regardless of RSI. |
| **observation `tuples`** (`extract_tuples.py`, daily haiku) | cascade | annotations / session summaries (observer output) | generators / profile inference (NOT `analyze.py`) | [extract_tuples.py:207](../packages/analyzer/extract_tuples.py#L207), [:234](../packages/analyzer/extract_tuples.py#L234) (reads session summaries → writes `observations/<bot>/<date>.jsonl`) | Producer→consumer confirmed; consumers are generators, not the detector runner. **No `rsi`/module gate** (§6). |
| **`analysis`** (`analyze.py`, 11 detectors) | cascade | `metrics` (metric detectors) AND/OR annotations (annotation detectors); `rsi` master | proposals (writes `proposals/pending/`) | gate [analyze.py:1071](../packages/analyzer/analyze.py#L1071) (`is_rsi_enabled`) + [:1075](../packages/analyzer/analyze.py#L1075) (`analysis`); reads [analyze.py:44-56](../packages/analyzer/analyze.py#L44) (metrics), [:331-352](../packages/analyzer/analyze.py#L331) (annotations) | **Two parallel input streams.** Metric detectors no-op without metrics; annotation detectors no-op without annotations. Each detector degrades independently. |
| **`apply`** (applier daemon) | cascade | approved proposals (from `analysis`); sudoers/ACLs (to write bot config); `rsi` master | `outcomes` (tallies what apply produced) | gate [apply.py:114](../packages/analyzer/apply.py#L114) (`is_rsi_enabled`) + [:117](../packages/analyzer/apply.py#L117) (`apply`); reads approved dir [apply.py:214-219](../packages/analyzer/apply.py#L214) | No proposals ⇒ "No new proposals" no-op. The L2 write path is what couples it to the infra floor (sudoers/ACLs). |
| **`outcomes`** (`outcome.py`, tally) | cascade | applied proposals (from `apply`); `rsi` master | calibration / generator track records (consume outcomes) | gate [outcome.py:316](../packages/analyzer/outcome.py#L316) (`is_rsi_enabled`) + [:322](../packages/analyzer/outcome.py#L322) (`outcomes`); reads pending-outcomes [outcome.py:70-79](../packages/analyzer/outcome.py#L70) | Nothing applied ⇒ empty pending ⇒ no-op. Terminal of the RSI chain. |
| **`rsi`** (master switch) | cascade | — | `analysis`, `apply`, `outcomes` (short-circuits all three) | [analyze.py:1071](../packages/analyzer/analyze.py#L1071), [apply.py:114](../packages/analyzer/apply.py#L114), [outcome.py:316](../packages/analyzer/outcome.py#L316) | **Scope caveat (catalog flag #2):** gates ONLY analyze/apply/outcome. Tier-3 audit, scanner, tuples are NOT under it — the "no model tokens" copy is misleading. |

### 1d. Cost / safety breakers

| Component | Classification | requires[] | required_by[] | Code evidence (file:line) | Notes |
|---|---|---|---|---|---|
| **`cost`** module → `spend_alert.py` | cascade (safety-floor — keep ON in Passive per FD-3) | usage/turn data (from observer cost data) | **L1 daily-cap breaker auto-trip** | gate [spend_alert.py:1553](../packages/analyzer/spend_alert.py#L1553); trip [spend_alert.py:991-1056](../packages/analyzer/spend_alert.py#L991) (writes `breakers/<bot>/cost.json`, runs enforce) | Disabling `cost` disables the L1 auto-trip — a **safety regression**, not just a feature cut. Surface as safety (catalog flag #3: no UI card today). |
| **L1 cost breaker** (strip heartbeat / narrow exec on trip) | cascade (safety-floor) | `cost` module (the auto-trip feed); a cap being set | bot cost ceiling enforcement | [spend_alert.py:991-1056](../packages/analyzer/spend_alert.py#L991) | $0 to run; mutates bot behavior when it fires. FD-3: stays ON even in Passive. |
| **runaway-rate cap** (`ModelRouter`, forced downgrade) | cascade (safety-floor) | `modelRouting` capability (lives in the routing hook) | runaway-spend protection | [ModelRouter.ts:193-196](../packages/plugin/src/observer/ModelRouter.ts#L193) (config; **on by default**) | **Coupled to routing** — turning off `modelRouting` (tier < `manage`) also drops this. §3 hazard. |
| **spend-cap safety net** (`isSpendCapActive` → force `fast`/pause) | cascade (safety-floor) | `modelRouting` capability | spend-cap enforcement | [ModelRouter.ts:2726-2728](../packages/plugin/src/observer/ModelRouter.ts#L2726) | Same coupling as runaway: lives inside the routing hook. FD-3. |
| **`cost_watchdog`** daemon | safe-leaf | — | nothing | (runs unconditionally; NOT gated by `cost` — catalog flag #4) | $0 antipattern detector; independent of the `cost` module. Mental-model copy bug, not a dependency. |

### 1e. Healing / monitors / generators / leaf features

| Component | Classification | requires[] | required_by[] | Code evidence (file:line) | Notes |
|---|---|---|---|---|---|
| **`healing`** (`heal.py`, gateway self-heal) | safe-leaf | gateway process/launchd state + config (NOT observations) | nothing downstream | gate [heal.py:439](../packages/analyzer/heal.py#L439); reads live HTTP health + `ps`, NOT `observations/`/`metrics/`/`annotations/` (grep-confirmed absent) | **Spine edge REFUTED:** healing is independent of the observer/analysis layer. It would work identically with the observe layer off. Safe to reason about alone — but it *restores* the plugin, so it is the natural floor self-heal (spec FD-5 #5). |
| **signal-subscriber** + **~30 pod monitors** (pod_report, audit, host_health, watchdog…) | cascade | — (monitors write Signals; subscriber dispatches generators) | Alerts page, event-driven proposal generation | [spec-signal-subscriber-2026-05-31.md](spec-signal-subscriber-2026-05-31.md) (daily sweep is the safety-net fallback; arbiter dedup makes it non-critical) | Observability + proposal-latency, NOT control plane. Disabling stops alerts/proposals but not bot operation or apply. Subscriber is a *latency* reduction; the daily sweep backstops it. |
| **`expansion`** (monthly app-expansion haiku) | safe-leaf | — | nothing | gate [expansion.py:729](../packages/analyzer/expansion.py#L729) | ~5 calls/mo; isolated. |
| **`continuity_engine`** (per-bot deferred promises) | safe-leaf | — | nothing pod-wide | gate [defer_runner.py:210](../packages/analyzer/defer_runner.py#L210) | Per-bot; default on per bot. |
| **`community_intel`** (weekly external scan) | safe-leaf | — | nothing | gate [community_intel.py:373](../packages/analyzer/community_intel.py#L373) | Default **off** already. |
| **`slack_signals`** | safe-leaf | — | nothing | gate [slack_signals.py:503](../packages/analyzer/slack_signals.py#L503) | Default **off** already. |
| **tier-3 app audit** / **app scanner** / **`user_profile_inferrer`** / **`model_discovery`** / **`security_warden`** | safe-leaf (`security_warden` = safety-floor per FD-3) | — (cadence/condition-gated, own creds) | nothing downstream consumes their output as a hard dep | grep-confirmed NO `is_rsi_enabled`/`is_module_enabled` in `app_audit_runner.py`, `scanner.py`, `extract_tuples.py` | Costed leaves: each gates only on its own cadence/condition, none under `rsi`. `security_warden` off ⇒ regex-only injection detection (not zero) — security-floor, co-own `edr`. |
| **11 behavior detectors** (individual) | safe-leaf | their data source (metric or annotation) | nothing | per-detector toggles under `analysis` | Individually flippable; safe leaves under the `analysis` cascade parent. |

---

## 2. The dependency spine (ASCII)

```
                          ┌─────────────────────────────────────────────┐
   INFRA FLOOR (not in    │  admin daemon ── sudoers grants ── .openclaw │
   the v1 dial; §4)       │       │              │            ACLs       │
                          │       └──────┬───────┘             │         │
                          │              ▼                     ▼         │
                          │     APPLY MECHANISM  ◄── reads config ───────┤
                          │   (cp+chown+chmod+kickstart)                 │
                          │              │                               │
                          │   repo-puller (security-update delivery)     │
                          └──────────────┼───────────────────────────────┘
                                         ▼  installs + can re-enable
                          ╔══════════════════════════════════╗
                          ║   GATEWAY PLUGIN INSTALL (root)   ║  no plugin ⇒ vanilla OC
                          ╚══════════════════════════════════╝
                                         │
                  ┌──────────────────────┼───────────────────────────┐
                  ▼                      ▼                            ▼
        tier ladder (full)     allowConversationAccess=true   roster tools (uncond.)
                  │                      │  (upstream-OC gate)
   ┌──────────────┼──────────────┐       ▼
   ▼              ▼              ▼   ┌─────────────────────────────────────────┐
injectKeywords injectPodConduct modelRouting   observer capability (tier≥monitor)│
  (full)         (manage)        (manage)       = CONTENT HOOKS (PRODUCERS)      │
  safe-leaf      safe-leaf          │           llm_output / agent_end / session_end
                                    │           └───────────────┬─────────────────┘
                          ┌─────────┴─────────┐                 │ writes
                          ▼                   ▼      ┌──────────┼───────────┬─────────────┐
                  runaway-rate cap      spend-cap    ▼          ▼           ▼             ▼
                  (safety-floor)        (safety)  annotations  cost data  outward-action  session
                          coupled to modelRouting     │          │         ledger          summary
                                                      ▼          ▼            │
                                              ┌── metrics ──┐  `cost` module  ▼
                                              │ (measure.py)│      │       autonomy caps
                                              ▼             ▼      ▼       (rung-3, un-gateable)
                                         tuples         analysis  L1 breaker (safety-floor)
                                     (extract_tuples)  (analyze.py)
                                              │             │
                                         generators     proposals/pending
                                                            │
                                                          apply ──► outcomes
                                                       (needs sudoers/ACLs to write)

   healing (heal.py) ── INDEPENDENT of the observe→analyze spine; watches live
                        gateway/launchd health; is the natural floor self-heal.
```

**Read the spine as three layers:** (1) an **infra floor** that the apply mechanism —
the very thing that re-enables a disabled component — itself depends on; (2) a
**producer layer** (plugin → tier → observer + `allowConversationAccess`) that
generates the data; (3) a **consumer chain** (metrics/tuples → analysis → apply →
outcomes) that no-ops top-down when a producer goes dark.

---

## 3. Half-state hazards (ranked by severity)

These are the `(enabled X + disabled Y)` combinations the engine's **preflight cascade
simulation** must catch (spec FD-5 #4). A "half-state" = an enabled component whose
required input is disabled — it keeps running but produces nothing / breaks.

| # | Severity | Hazard combo | What breaks | Engine action |
|---|---|---|---|---|
| H1 | 🔴 critical (lockout) | Remove **sudoers** or **ACLs** while expecting to re-enable later | Apply mechanism can't write config / kickstart; admin can't read config. The disable becomes **irreversible from the UI**. | **Block** — infra floor, excluded from dial entirely (§4). |
| H2 | 🔴 critical (lockout) | Disable **admin daemon** | No UI recovery path; only CLI / off-box `network.json` edit re-enables anything. | **Block** — infra floor. |
| H3 | 🔴 critical | Remove **gateway plugin** while any tier capability / hook / tool / ledger is "enabled" | All of those silently no-op (bot is vanilla OC). Maximal half-on state. | **Block** — infra floor; plugin presence is the precondition for the whole dial. |
| H4 | 🟠 high (safety) | Disable **`cost`** module (or drop **`modelRouting`**) while operator believes spend protection is on | L1 daily-cap auto-trip stops ([spend_alert.py:1553](../packages/analyzer/spend_alert.py#L1553)); runaway-rate cap + spend-cap downgrade stop (they live in the `modelRouting` hook, [ModelRouter.ts:2726](../packages/plugin/src/observer/ModelRouter.ts#L2726)). **Silent loss of the cost safety floor.** | **Keep ON / warn-and-confirm** — FD-3: breakers stay on even in Passive, framed as safety. Co-own `edr`/`model-tiers`. |
| H5 | 🟠 high | Disable **observer** (tier→`off`/below `monitor`) OR set **`allowConversationAccess` false**, while `metrics`/`analysis`/`tuples` stay enabled | Producers go dark: no annotations ⇒ empty metrics ([measure.py:165](../packages/analyzer/measure.py#L165)); no annotations/metrics ⇒ detectors no-op ([analyze.py:331](../packages/analyzer/analyze.py#L331)); no summaries ⇒ empty tuples. **Dashboards go empty; RSI silently produces nothing.** | **Cascade-with-consent** — show "also stops metrics, analysis, tuples; dashboards empty." |
| H6 | 🟠 high (safety) | Disable **`security_warden`** while believing injection detection is full-strength | Drops to regex-only injection detection (not zero, but reduced). | **Warn** — security-floor, co-own `edr`. FD-3 keeps it on in Passive. |
| H7 | 🟡 medium | Disable **observer/outward-ledger** while **autonomy caps** (rung-3) are set | Caps read the outward-action ledger to count actions; no ledger ⇒ caps silently stop enforcing. | **Cascade/decide** — ledger is un-gateable by design; the contract must decide caps exist-or-not, never half-disable (spec). |
| H8 | 🟡 medium | Disable **`analysis`** while **`apply`**/**`outcomes`** stay on | apply: "No new proposals" no-op; outcomes: empty. Harmless no-op but wasted daemons + confusing "RSI on, nothing happens." | **Cascade-with-consent** down the chain (analysis→apply→outcomes). |
| H9 | 🟢 low | Disable **`apply`** while **`outcomes`** on | Nothing applied ⇒ outcomes empty no-op. | Inform only. |
| H10 | 🟢 low | Toggle the **`observer` *module key*** expecting it to change runtime | No effect — the key gates nothing ([catalog flag #1](footprint-catalog-2026-06-18.md)); the real gate is the tier capability. | **Fix the bug** (FD-4): remove/redirect the dead key; don't let the dial expose a no-op toggle. |

---

## 4. Infra floor (excluded from the v1 dial)

Per spec FD-5 #2 and the operator's "v1 = cascade + safe-leaf only" call, these are
**out of scope for the v1 dial — not even an expert gate.** One-line reason each:

| Infra-floor component | Why it is excluded (the hazard) |
|---|---|
| **Gateway plugin install** | The runtime root — removing it makes every other dial item a silent no-op (H3) and there is no "half" plugin. Posture writes *tier*, never plugin presence. |
| **sudoers grant set** | The apply mechanism (cp/chown/chmod/kickstart) that re-enables anything depends on these — removing them is a one-way lockout (H1). |
| **macOS `.openclaw/` ACLs** | Config reads on the critical path fall back only to `sudo cat`; lose both and reads fail silently (H1). |
| **admin daemon** | The control plane and the UI recovery path; off ⇒ re-enable only via CLI/off-box edit (H2). |
| **`allowConversationAccess`** | Upstream-OC precondition for the entire content/observe layer; today it has no toggle at all and its "passive form" is unreachable. Listed as floor for the *content layer*; a future Passive cut would need a deliberate, OC-honoring mechanism, not a casual dial. |
| **repo-puller (security-update path)** | The fleet's fix-delivery path; the *cadence* could become a knob later, but the existence of a delivery path is floor (no auto-pull AND no manual pull ⇒ pod can't be patched). |

Note FD-3 safety-floor items (**L1 / runaway / spend-cap breakers, `security_warden`**)
are *not* infra-floor — they are dialable in principle but **kept ON in every posture
including Passive** by policy, framed as safety. The engine treats them as "warn +
confirm, default keep," not "exclude."

---

## 5. Cascade groups (disable-together-with-consent)

When the operator disables the group's head, the engine shows the blast radius and
disables the chain together (spec: orphaned-dependent handling = cascade-disable-with-
consent).

- **CG-1 — Observe→Analyze data spine (head: `observer` / tier→below `monitor` /
  `allowConversationAccess`):** `observer` → `metrics`, `tuples`, `analysis` →
  `apply` → `outcomes`. Disabling the producer empties everything downstream (H5/H8).
  Cascade order: drop consumers first (outcomes→apply→analysis→metrics/tuples) so no
  enabled consumer is ever left starving mid-apply.
- **CG-2 — RSI chain (head: `rsi` master):** `analysis` + `apply` + `outcomes`
  short-circuit together via `is_rsi_enabled` ([analyze.py:1071](../packages/analyzer/analyze.py#L1071)
  / [apply.py:114](../packages/analyzer/apply.py#L114) / [outcome.py:316](../packages/analyzer/outcome.py#L316)).
  This one is *already* a clean cascade in code — the master switch does the right thing.
- **CG-3 — Cost safety bundle (head: `cost` / `modelRouting`):** `cost` → L1 breaker;
  `modelRouting` → runaway-rate cap + spend-cap net. Because the routing-side breakers
  live inside the `modelRouting` hook, dropping routing silently removes them (H4).
  **Per FD-3 this group is kept ON in all postures** — listed here so the engine knows
  the coupling, not because it should cascade them off.
- **CG-4 — Autonomy caps (head: `observer`/outward-ledger):** rung-3 caps depend on the
  outward-action ledger, which is written in `agent_end` and is un-gateable by design.
  The contract decides caps-exist-or-not as a unit; never a half-disable (H7).
- **CG-5 — Tools↔autonomy (head: tier < `manage`):** `record_application` tool
  ([index.ts:138](../packages/plugin/src/index.ts#L138)) and the autonomy ladder are
  tied to `manage`+; dropping below `manage` removes the cap-write tool alongside the
  ledger consumer — keep CG-4 and CG-5 reasoned together.

Everything not in a cascade group is a **safe-leaf** (§1b/§1e: `injectKeywords`,
`injectPodConduct`, `preflight`, roster tools, `expansion`, `continuity_engine`,
`community_intel`, `slack_signals`, the costed audit/scanner/inferrer/discovery leaves,
`healing`, `cost_watchdog`, individual detectors) — flip alone, no cascade.

---

## 6. Edges that could NOT be confirmed from code (still soft)

Flagged so the engine design knows what is assumed vs. proven:

1. **`allowConversationAccess` → which exact hooks starve — assumed from the deploy
   comment + OC version note, not traced into OC.** Our plugin code never reads the
   flag ([deploy.py:2622-2627](../packages/admin/evolve_admin/deploy.py#L2622) sets it;
   grep of `packages/plugin/src` = 0). The enforcement is **upstream OpenClaw**
   (≥2026.4.29) — we can't cite the OC line that withholds events. Edge direction is
   firm (deploy forces it *because* hooks need it); the precise per-hook degradation
   when false is **inferred from the deploy comment**, not observed.

2. **outward-action ledger → autonomy caps — write side proven, read side asserted by
   docstring.** The ledger write is cited ([OutwardActionLedger.ts:154-219](../packages/plugin/src/observer/OutwardActionLedger.ts#L154)) and the
   unconditional instantiation is cited; the claim that rung-3 caps *read* it to count
   actions rests on the in-code docstring ("the ledger is the data source the rung-3
   caps depend on"), **not** a traced reader. The caps-reader path should be confirmed
   before the contract finalizes CG-4.

3. **tuples consumers — producer proven, consumer set partial.** `extract_tuples.py`
   writing `observations/<bot>/<date>.jsonl` is cited; the specific generators that read
   tuples (via `observations.access.window`) were not individually enumerated. We
   confirmed the *non*-consumer (analyze.py does NOT read tuples) but the exact consumer
   list is **"assumed from catalog."**

4. **signal-subscriber criticality — classified `cascade`/observability from the spec's
   "daily sweep is the safety net," not from a per-generator dependency trace.** If some
   load-bearing generator subscribes *and* lacks a daily-cadence fallback, the subscriber
   could be more load-bearing than classified. The spec asserts the daily sweep backstops
   all generators; this was **not verified per-generator**.

5. **repo-puller security-update split — the infra-floor portion is a policy call, not a
   code edge.** Code shows the puller is a convenience auto-pull with a manual-pull
   recovery path ([repo_puller.py:1-35](../packages/analyzer/repo_puller.py#L1)); the
   "security-update path is floor" classification comes from spec FD-5 #2, not from a
   code branch that distinguishes security from feature pulls.

---

## 7. Hand-off to the engine design (F-3)

The graph above gates the engine design:
- **v1 dial domain = the `cascade` + `safe-leaf` rows of §1b/§1c/§1d/§1e.** The §4
  infra floor is excluded entirely (FD-5 #2).
- **Preflight must implement the §3 hazard table** as the cascade simulation (FD-5 #4),
  with H1-H3 as hard blocks (they touch the floor) and H4-H10 as cascade/warn/inform.
- **Cascade engine consumes §5 groups**, dropping consumers before producers so no
  enabled component is ever left in a starving half-state mid-apply (the spine direction
  in §2).
- **FD-3 safety items are "keep-on-warn," not dial-off** — the engine must special-case
  CG-3 + `security_warden`.
- **§6 soft edges** are the verification backlog before the contract (F-4) hardens the
  `requires[]` declarations: confirm the OC-side `allowConversationAccess` enforcement,
  the autonomy-cap reader, the tuples consumer set, and per-generator subscriber
  fallback.

Routing per spec boundary: gateway/tier/exec edges → `edr`+`deploy`; appliers/generators
→ `rsi`; app-audit/scanner → `apps`; cost breakers → `model-tiers`+`edr`; UI of the dial
→ `ui`. This aspect owns the graph + the contract.
