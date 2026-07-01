# Footprint Catalog (consolidated) — F-1 + DEFAULT_MODULES reconciliation (2026-06-18)

**Aspect:** `META:footprint` · **Slice:** F-stitch (consolidation of the five F-1 section audits)
**Spec:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md)

This is the consolidated, top-level footprint catalog. It stitches the five F-1
section audits into one master table and **reconciles every cataloged subsystem
against the existing module registry** (`DEFAULT_MODULES` in
[evolve_config.py:204](../packages/analyzer/evolve_config.py#L204)). The reconciliation
is the load-bearing deliverable: the spec's hard constraint is *"the existing `modules`
system IS the substrate — do not build a parallel toggle system"*, so the F-3 posture
dial can only be designed once we know **which cataloged footprint already maps to a
module key (already dialable) and which is a GAP (no toggle today)**.

---

## 1. The four footprint dimensions

"Invasive" is not one axis (spec §"four footprint dimensions"). A monitor can be
observe-only yet expensive; a hook can be cheap yet behavior-altering. Every item is
classified on all four:

- **M — Mutation:** changes config/state the bot loads, or how OC behaves
  (openclaw.json / auth-profiles writes, plugin injection, exec-policy flips,
  model-routing rewrites, L1/L2 appliers).
- **R — Runtime / hot-path:** runs inside or intercepts the bot's turn loop (gateway
  plugin code, `agent_end`/`llm_output` hooks, `allowConversationAccess`). The
  latency/perf tax.
- **C — Cost:** spends tokens (LLM scanner, tier-3 audits, generators, escalation
  monitors). The surprise-bill tax.
- **P — Privilege / surface:** daemons, sudoers grants, ACLs, the managed git checkout,
  the `evo` account. The security-and-bugs footprint.

### The five section catalogs (full detail lives here)

| Slice | Dimension focus | Doc |
|---|---|---|
| F-1a | Privilege/surface — daemons, sudoers, ACLs, managed checkout, `evo` account, OC safe-upgrade | [footprint-catalog-2026-06-18-deploy-privilege.md](footprint-catalog-2026-06-18-deploy-privilege.md) |
| F-1b | Runtime/hot-path — the gateway plugin (`packages/plugin`) turn-loop interception | [footprint-catalog-2026-06-18-runtime-gateway.md](footprint-catalog-2026-06-18-runtime-gateway.md) |
| F-1c | Cost — every monitor/signal/scanner/audit/generator/breaker, $0 vs token-spending | [footprint-catalog-2026-06-18-cost-monitors.md](footprint-catalog-2026-06-18-cost-monitors.md) |
| F-1d | Mutation — every write to the config/state a bot loads (openclaw.json, appliers) | [footprint-catalog-2026-06-18-config-appliers.md](footprint-catalog-2026-06-18-config-appliers.md) |
| F-1e | Settings audit — what footprint control actually exists in the UI today + gaps | [footprint-catalog-2026-06-18-settings-surface.md](footprint-catalog-2026-06-18-settings-surface.md) |

### The substrate being reconciled against

`DEFAULT_MODULES` ([evolve_config.py:204](../packages/analyzer/evolve_config.py#L204)),
stored at `network.json["modules"]` with per-bot overrides
(`set_bot_module_enabled` / `is_bot_module_enabled`), surfaced on Settings → Modules
(`renderModules` [settings.js:37](../packages/admin/evolve_admin/web/static/js/pages/settings.js#L37)).
The keys and **what each one actually gates** (grep-verified consumer, not the label):

| Module key | Default | Code consumer (the real gate) | What flipping it off does |
|---|---|---|---|
| `rsi` (master) | on | [analyze.py:1071](../packages/analyzer/analyze.py#L1071), [apply.py:114](../packages/analyzer/apply.py#L114), [outcome.py:316](../packages/analyzer/outcome.py#L316) | Short-circuits analyze/apply/outcome **only** (see accuracy flag §4) |
| `analysis` (+11 detectors) | on | [analyze.py:1075](../packages/analyzer/analyze.py#L1075) | Stops behavior-analysis detectors |
| `apply` | on | [apply.py:117](../packages/analyzer/apply.py#L117) | Stops the applier daemon realizing approved proposals |
| `outcomes` | on | [outcome.py:322](../packages/analyzer/outcome.py#L322) | Stops outcome tallying |
| `expansion` | on | [expansion.py:729](../packages/analyzer/expansion.py#L729) | Stops monthly app-expansion Haiku (~5 calls/mo) |
| `healing` | on | [heal.py:439](../packages/analyzer/heal.py#L439) | Stops 5-min gateway self-heal + auto-restart |
| `metrics` | on | [measure.py:475](../packages/analyzer/measure.py#L475) | Stops daily metric aggregation |
| `cost` | on | [spend_alert.py:1553](../packages/analyzer/spend_alert.py#L1553) | Stops spend_alert burst/cap detection **and the L1-breaker auto-trip** (safety; see §4) |
| `continuity_engine` | on (per-bot) | [defer_runner.py:210](../packages/analyzer/defer_runner.py#L210) | Stops one bot's time-deferred promises |
| `community_intel` | **off** | [community_intel.py:373](../packages/analyzer/community_intel.py#L373) | (already off; weekly external scan) |
| `slack_signals` | **off** | (daemon installed; ingestion token-gated) | (already off) |
| `observer` | on | **NO consumer found** — see accuracy flag §4 | Nothing — the runtime observer is gated by the plugin `tier` ladder, not this key |

---

## 2. Master reconciliation table

One row per significant footprint item across all five sections.
`Module key` = the `DEFAULT_MODULES` key that already gates it, or **GAP** (no module
toggle today). `Toggle-state` = how it is gated *right now* (independent of whether a
module key exists). Dimensions: M/R/C/P.

### Privilege / install surface (F-1a)

| Subsystem / touch | Dims | Module key | Current toggle-state | Section |
|---|---|---|---|---|
| `admin-ui` daemon (control plane) | P,R | **GAP** | Unconditional | F-1a §1 |
| `repo-puller` (15-min `git pull` + post-pull plugin restage / gateway kickstart) | P,M,R | **GAP** | Unconditional (risk de-risked by `pod.release.mode`, no off-switch) | F-1a §1,§5 |
| `mcp-bridge` (laptop tunnel, port 5051) | P | **GAP** (has own toggle) | Gated `network.json::mcp_bridge` (default on) | F-1a §1 |
| `signal-subscriber` (1 Hz firing-dir watch → generator dispatch) | P,R,C | **GAP** | Unconditional (dispatches only $0 generators today) | F-1a §1 |
| ~30 pure-Python pod-wide monitors (pod_report, audit, host_health, drift/recovery/coverage…) | P | **GAP** | Unconditional, per-daemon | F-1a §2,§4 · F-1c §1 |
| `heal` (5-min gateway self-heal) | P,R | **`healing`** | Gated (default on) | F-1a §4 |
| `measure` (daily metrics) | P | **`metrics`** | Gated (default on) | F-1a §4 |
| `spend-alert` + `cost_watchdog` daemons | P | **`cost`** (spend_alert only) / GAP (cost_watchdog) | Gated default on / Unconditional | F-1a §4 · F-1c §1,§4 |
| per-bot `apply.{bot}` applier daemon | P,M | **`apply`** + `rsi` | Gated (default on) | F-1a §3 · F-1d §D |
| per-bot `doctor-pass` (nightly `openclaw doctor --fix`) | P,M | **GAP** | Unconditional | F-1a §3 |
| per-bot `audit-runner` T2 (6h structural, $0) / T3 (semantic, costed) | P,C | **GAP** | Unconditional daemon; T3 cadence-gated (default monthly) | F-1a §3 · F-1c §2 |
| `expansion.{bot}` (monthly app-expansion generator) | P,C | **`expansion`** | Gated (default on) | F-1a §4 · F-1c §3 |
| `analyze` / `outcome` / `tuples` periodic generators | P,C | `analyze`→**`analysis`**/`rsi`; `outcome`→**`outcomes`**/`rsi`; **tuples GAP** | Gated / Gated / Unconditional | F-1a §4 · F-1c §3 |
| `proposal_synthesizer` (6h LLM synthesis) | P,C | **GAP** (rsi-adjacent, no explicit gate) | Unconditional | F-1a §4 |
| `community_intel` weekly scan | P,C | **`community_intel`** | Gated (default **off**) | F-1a §4 |
| `slack-signals` daemon | P | **`slack_signals`** | Gated (default **off**) | F-1a §4 |
| `*-issues-watcher` (upstream/inbound GitHub) | P,C | **GAP** (own feature flag) | Gated `install.json::features.*` (default off; plist absent) | F-1a §4 |
| sudoers grant set (`/etc/sudoers.d/evolve` — account create/delete, kill -9, cp/chown) | P | **GAP** | Unconditional (manual `refresh-sudoers` to apply) | F-1a §6 |
| macOS read/write ACLs on `.openclaw/` + workspace | P | **GAP** | Unconditional | F-1a §7 |
| `evo` second macOS account + shared-store ACL | P | **GAP** (good precedent) | Gated — fresh-install opt-in, non-fatal | F-1a §8 |
| Managed deploy checkout `/Users/Shared/evolve-repo` | P | **GAP** | Unconditional | F-1a §5 |
| OC safe-upgrade preflight (read-only; never auto-upgrades) | P | n/a | On-demand only | F-1a §9 |

### Runtime / hot-path — gateway plugin (F-1b)

The gateway plugin's whole interception layer is gated by the per-bot **`tier` ladder**
(`off`/`monitor`/`manage`/`full`) in `openclaw.json` — a **real, code-level dial that is
NOT in `DEFAULT_MODULES`** ([config.ts:40-152](../packages/plugin/src/config.ts#L40)).
This is the single most important reconciliation finding: a posture mechanism already
exists at the gateway, parallel to the modules registry.

| Subsystem / touch | Dims | Module key | Current toggle-state | Section |
|---|---|---|---|---|
| Master **tier ladder** (off/monitor/manage/full) | R,M,C | **GAP** (real toggle, not a module) | Per-bot `openclaw.json::tier`; **default `full`** (fail-toward-invasive) | F-1b §0 |
| `allowConversationAccess` (the capture opt-in for all content hooks) | M,R | **GAP** | **Forced `true` at deploy, no toggle anywhere** | F-1b §1 · F-1d §B · F-1e §2 |
| `llm_output` / `agent_end` / `session_end` hooks (cost monitor, annotation, summarizer) | R,C | **GAP** | Gated by `observer` capability (`monitor`+ tier) | F-1b §1 |
| `before_model_resolve` model-routing rewrite | M,R,C | **GAP** | Gated `modelRouting` (`manage`+) + `routing.enabled` kill-switch (default true) | F-1b §1,§2 |
| `session_start` pod-conduct subprocess (persona injection) | M,R,C | **GAP** | Gated `injectPodConduct` (`manage`+) | F-1b §1 |
| `before_prompt_build` per-turn system-prompt injection (narrative + speaker) | M,R,C | **GAP** | Gated `injectKeywords` (`full`) | F-1b §1 |
| Pre-flight intent router (per-turn **haiku** call on critical path) | R,C,M | **GAP** (own toggle) | Gated `cascade.preflight.enabled` / `_isHaikuEnabled()` (default on) | F-1b §2 |
| Manifest-trigger interception (synchronous 25 s subprocess) | M,R,C | **GAP** | Data-driven (only with `plugin_intercept` manifests) | F-1b §2 |
| Cascade telemetry span (per-turn file append) | R | **GAP** (own toggle) | Gated `observability.cascade_telemetry.enabled` (default true) | F-1b §3 |
| Cost ledger (per-`llm_output` JSONL) | R | **GAP** (own toggle) | Gated `costLedgerEnabled` (default true) | F-1b §3 |
| Outward-action ledger (per-MCP-call JSONL, backs autonomy caps) | R | **GAP** | **Unconditional, un-gateable by design** | F-1b §3 |
| Registered tools (`defer`, `session.set_tier`, `record_application`, roster tools) | C,M | **GAP** | Tiered; roster tools **unconditional on every bot** | F-1b §4 |

### Cost — token-spending surfaces (F-1c)

| Subsystem / touch | Dims | Module key | Current toggle-state | Section |
|---|---|---|---|---|
| **Tier-3 semantic app audit** (2 `openclaw agent` LLM dispatches/due app, `power` model) | C,R | **GAP** | Cadence-gated (default monthly); calibration-on | F-1c §2 |
| App scanner discovery + purpose classifier (Haiku/Sonnet) | C | **GAP** | On-demand only (no recurring schedule) | F-1c §2 |
| Observation-tuple extraction (daily Haiku, ≤50 sessions/run) | C,P | **GAP** | Unconditional daily (`tuples` daemon) | F-1c §3 |
| `security_warden` Haiku injection-verifier | C (gated) | **GAP** | Regex-gated, fails open to regex-only | F-1c §1,§3 |
| `model_discovery` fit classifier (Haiku, only on new model) | C (gated) | **GAP** | Per-discovery; fail-open | F-1c §3 |
| `user_profile_inferrer` (per-session Haiku, bot's own creds) | C,P | **GAP** | Per-session; DNT-gated | F-1c §3 |
| ~24 other RSI generators (budget_hawk, efficiency_hawk, fillers…) | R | **`rsi`** (loop) / GAP per-gen | Pure-Python $0; under the `better` runner | F-1c §3 · F-1d §D |
| L1 cost breaker (daily-cap → strips heartbeat, narrows exec) | M,R,P | **`cost`** (auto-trip feed) | Enforcement on when a cap is set (cap None by default) | F-1c §4 |
| Runaway-rate hard cap (ModelRouter, forced downgrade) | M,R | **GAP** (`tiers.json::runawayRateCap`) | **ON by default** | F-1c §4 · F-1b §2 |
| Spend-cap safety net (`isSpendCapActive` → forces `fast`/pause/suspend) | M,R | **GAP** | Gated by file flag; action selectable | F-1c §4 |

### Mutation — config writes (F-1d)

| Subsystem / touch | Dims | Module key | Current toggle-state | Section |
|---|---|---|---|---|
| **Evolve TS gateway plugin install** (the headline injection) | M,R,P | **GAP** | **Unconditional on every deploy** | F-1d §B |
| `ensure_plugin_config()` openclaw.json field writes (~20 fields) | M,R,P | **GAP** | Unconditional (gap-fill / overwrite per field) | F-1d §A |
| `tools.exec.security` exec-policy flip | M,R,P | **GAP** (operator-tunable via `pod.execPolicy`) | Unconditional write; value via 3-tier ladder (default `full`) | F-1d §A,§C |
| Arbiter appliers (ConfigPatch, permissions, cron, tier, MCP, soul…) | M,R,C,P | **`apply`** + `rsi` | Per-applier; auto vs `approved_human` via `is_autonomous_eligible` | F-1d §D |
| `evo_tools` MCP server (primary bot) | M,R,P | **GAP** | Unconditional (primary only) | F-1d §B |
| Operator MCP install / channel skills | M,R,P | **GAP** | Gated — proposal/operator-UI action | F-1d §B |
| Doc/workspace re-assertion (SOUL/AGENTS/POD_CONDUCT/manifests) | M | **GAP** | Unconditional (idempotent; ≥1500-byte hand-edit guard) | F-1d §A,§E |

---

## 3. Gaps — invasive subsystems with NO module toggle today

These are what the posture dial must add a control for. Grouped by dimension. (Items
that already have *some* toggle but no `DEFAULT_MODULES` key are flagged
"non-module toggle" — the dial should aggregate them, not re-invent them.)

### Mutation gaps

- **The Evolve TS gateway-plugin install itself** ([deploy.py:3494](../packages/admin/evolve_admin/deploy.py)) — unconditional on every deploy; *everything runtime hangs off it*. No module key. A true "dashboard mode" (deploy without injecting the plugin) is the floor of Passive and the single biggest mutation cut. **No toggle exists.**
- **`ensure_plugin_config()` openclaw.json writes** (~20 fields incl. `thinkingDefault`, `contextPruning`, `heartbeat.*`) — unconditional; no module key. A subset is a non-reducible safety floor (see §4 / F-1d "always-on floor").
- **`allowConversationAccess` forced `true`** — non-module, **and has no toggle at all** (deploy-forced). Its passive form (don't intercept conversation content) is currently unreachable. The biggest mutation+runtime gap with zero existing control.
- **`tools.exec.security` exec-policy flip** — non-module toggle: tunable via `pod.execPolicy` in network.json, but only as a code/JSON override, not framed as a footprint choice.
- **Doc/workspace re-assertion** — unconditional overwrite-by-default (hand-edit guarded); low-stakes, no toggle.

### Runtime / hot-path gaps

- **The per-bot `tier` ladder is a real dial that is *not* a module key** — `off`/`monitor`/`manage`/`full` already gates almost the entire hot path, but it lives in `openclaw.json` per bot, defaults to `full` (fail-toward-invasive), and is invisible to the modules registry and the Settings UI. **The single highest-leverage reconciliation item: the posture dial should drive the tier ladder, not duplicate it.**
- **`routing.enabled`, `cascade.preflight.enabled`, `cascade_telemetry.enabled`, `costLedgerEnabled`** — four named gateway kill-switches, each a real toggle, none a module key, none surfaced in the UI.
- **Outward-action ledger** — unconditional, deliberately un-gateable (backs operator autonomy caps). The contract must decide whether autonomy caps exist at all rather than half-disable them (spec: "a toggle that leaves a subsystem half-on is worse than no toggle").
- **Roster tools registered on every bot unconditionally** — tool-surface token cost even at `monitor`/`manage`.

### Cost gaps

- **Tier-3 semantic app audit** — the heaviest token spender, cadence-gated but **no module key** and **not under the `rsi` master** (see §4 accuracy flag). Owner: `apps`.
- **App scanner** (discovery + classifier), **observation-tuple extraction** (daily Haiku), **`security_warden` Haiku verifier**, **`model_discovery` fit classifier**, **`user_profile_inferrer`** — all costed, all gated only by their own cadence/condition, none mapped to a module key, none under the `rsi` master.
- **Cost breakers** (L1 daily-cap, runaway-rate, spend-cap) — $0 to *run* but **mutate bot behavior when they fire** (strip heartbeat, force downgrade, pause crons, suspend). They straddle observe/active; the L1 auto-trip feed is gated by `cost`, but the runaway cap (`tiers.json`, **on by default**) and spend-cap are non-module. **Open question for F-3 (see §5).**

### Privilege / surface gaps

- **sudoers grant set** (account create/delete via `dscl`/`useradd`, `kill -9 -<pgid>`, recursive chown) — unconditional; highest privilege surface; no toggle.
- **macOS ACLs** on every `.openclaw/` tree — unconditional.
- **The managed deploy checkout + repo-puller** — unconditional 15-min auto-pull with plugin-restage + fleet kickstart; `pod.release.mode` de-risks rollout but there is no "manual updates only" off-switch.
- **`signal-subscriber`, `proposal_synthesizer`, ~30 pure-Python monitors, per-bot `doctor-pass`** — all unconditional daemons, no module key. Individually cheap, but ~64 host daemons is itself a privilege/surface footprint.
- **`evo` second macOS account** — already gated (fresh-install opt-in, non-fatal). Cited as the **model precedent** for the footprint contract, not a cut candidate.

---

## 4. Already-dialable (maps to a module key)

These cataloged items already have a `DEFAULT_MODULES` toggle. The posture dial should
**surface and aggregate** them, not re-implement (Reconciliation invariant).

| Footprint item | Module key | Default | Maps cleanly? |
|---|---|---|---|
| RSI improvement loop (analyze/apply/outcome) | `rsi` (master) | on | ✅ collapses several daemons |
| Behavior-analysis detectors (×11) | `analysis` | on | ✅ per-detector granularity |
| Applier daemon (realize approved proposals) | `apply` | on | ✅ |
| Outcome tallying | `outcomes` | on | ✅ |
| App-expansion generator (monthly Haiku) | `expansion` | on | ✅ |
| Gateway self-heal / auto-restart | `healing` | on | ✅ |
| Daily metric aggregation | `metrics` | on | ✅ |
| Spend-alert + L1-breaker auto-trip feed | `cost` | on | ⚠️ load-bearing safety — see flags |
| Per-bot time-deferred promises | `continuity_engine` | on (per-bot) | ✅ |
| Weekly external Kaizen scan | `community_intel` | off | ✅ already off |
| Slack reaction signals | `slack_signals` | off | ✅ already off |

### Accuracy flags — module copy/scope vs. what the code actually gates

Accuracy is a gate (spec Reconciliation invariant: *"the UI must reflect what each
module actually gates, not a hand-maintained label that drifts"*). The audit surfaced
four discrepancies the F-3/F-4 work must fix:

1. **`observer` module key gates nothing.** No Python consumer reads
   `is_module_enabled(…, "observer")` (grep-confirmed). The runtime observer is gated by
   the plugin **`tier` ladder** (`observer` capability at `monitor`+), entirely separate
   from `network.json::modules`. The module key is served by `GET /api/modules` but is
   **dead** — toggling it does nothing. Either wire it to the tier ladder or remove it;
   leaving it is exactly the drift the invariant forbids.

2. **The RSI master "no model tokens" copy is misleading.** The card says off ⇒ *"No
   model tokens spent on improvement work"* ([settings.js:118](../packages/admin/evolve_admin/web/static/js/pages/settings.js#L118)).
   True for analyze/apply/outcome — but **tier-3 app audit, the app scanner, and
   observation-tuple extraction are NOT under the `rsi` master** (grep-confirmed: no
   `is_rsi_enabled` gate in `app_audit_runner.py`, `scanner.py`, `extract_tuples.py`).
   The heaviest token spender (tier-3 audit) keeps running with RSI off. An operator
   reading "RSI off = no spend" is misled; the copy must scope to "improvement work" and
   the costed audit/scanner/tuples surfaces need their own controls (currently GAP, §3).

3. **`cost` module is a load-bearing safety gate with no UI card.** It gates
   `spend_alert.py` — including the **L1-breaker auto-trip** — but `renderModules` does
   not render a `cost` card (nor `apply`/`outcomes`/`metrics`/`observer`). So a
   safety-critical breaker can be silently disabled via `network.json` with no UI
   surface and no warning. F-3 must treat `cost` (and the breakers it feeds) as a
   safety-floor decision, not a hidden toggle.

4. **`cost_watchdog` daemon is not gated by `cost`.** Only `spend_alert.py` reads the
   `cost` module; the `cost_watchdog` cost-antipattern daemon runs unconditionally
   (it's $0, so low stakes, but the "the `cost` module turns off cost monitoring"
   mental model is wrong — `cost_watchdog` survives).

---

## 5. Posture-preset seed (first cut for F-3 — NOT a final decision)

A starting point for the F-3 design discussion: which items/modules each posture level
turns off. **Open questions flagged** — especially where a passive cut drops a safety
floor (co-own with `edr`/security per the spec boundary). This is a seed, not settled.

### Passive / "dashboard mode" — observe-only, minimal footprint

Evolve = legibility + inventory + managed updater. The good news from F-1c: **almost
the entire monitoring surface is already $0**, so Passive is cheap to reach.

- **Gateway tier → `monitor`** (capture-only; no injection, no routing, no hot-path
  mutation). This one change subsumes most runtime cuts.
- **Module keys off:** `rsi` (master), `apply`, `expansion`, `analysis`, `outcomes`.
- **Costed surfaces off (GAP — needs new controls):** tier-3 app audit, app-scanner LLM
  passes (structural-only scan retained), observation tuples, `user_profile_inferrer`,
  pre-flight haiku.
- **Mutation off:** auto-appliers forced `approved_human`; `doctor-pass` off;
  doc-re-assertion → seed-once.
- **Kept (the free Passive floor):** all $0 signal producers / monitors, host/pod
  health, Tier-2 structural audit, cost-antipattern detection, `healing`, `metrics`,
  managed updater (repo-puller).
- **⚠️ Open questions:**
  - **Plugin install:** does Passive still install the gateway plugin (tier `monitor`)
    or deploy *without* it (pure external observer)? The latter is a bigger cut but loses
    the dashboard/cost-ledger entirely. Likely keep the plugin at `monitor`.
  - **Cost breakers:** are the *interrupting* breakers (L1 daily-cap, runaway downgrade,
    spend-cap) "observe-only safety" that stays on in Passive, or "active mutation" that
    turns off? They are $0 to run but mutate when they fire. **Passive must not silently
    drop the cost safety floor** — recommend keeping breakers ON even in Passive and
    framing them as safety, not feature. Co-own with `edr` + `model-tiers`.
  - **`allowConversationAccess`:** turning it off (its passive form) guts the monitor
    layer that populates dashboards — so "dashboard mode" probably *keeps* it on at
    `monitor` tier. The dimension's "passive form" and the posture's goal conflict here.
  - **`security_warden` verifier off** drops to regex-only injection detection (not zero)
    — security-floor decision, co-own with `edr`.

### Standard — costed monitors + proposals (suggest, don't auto-apply)

- **Gateway tier → `manage`** (observer + pod-conduct + routing; no `full`-tier keyword
  injection / per-turn prompt rewrites).
- **Module keys on:** `rsi`, `analysis`, `outcomes`, `expansion`, `metrics`, `healing`,
  `cost`.
- **`apply` module on but auto-appliers gated to `approved_human`** (the propose/apply
  split already exists — flip the default, don't rebuild).
- **Costed surfaces:** tier-3 audit monthly + calibration-on (current default);
  observation tuples on (cheap, dedup-gated); pre-flight haiku optional.
- **⚠️ Open question:** Standard is closest to *today's* default — confirm whether
  "today's defaults" should be relabeled Standard, and whether the headline default for a
  fresh install should be Standard or Passive (spec: "default toward minimal" argues
  Passive; the advertised "managed" value argues Standard).

### Managed / active — full autonomy

- **Gateway tier → `full`** (all injection, routing, keyword, defer/record tools).
- **All module keys on**; auto-appliers auto-eligible (`is_autonomous_eligible` honored);
  tier cascade + autonomy ladder active; faster audit cadence + auto-fix.
- This is the current `full`-tier behavior — Managed = today's maximal posture made
  explicit and *chosen*, rather than the silent default.

### Cross-cutting open questions for F-3

1. **Where does the posture key live?** Spec says `network.json` alongside `modules`
   (e.g. `modules._posture: "passive|standard|managed"`), with per-module overrides
   layering on top. Confirm the precedence (posture preset → per-module override →
   per-bot override).
2. **The gateway tier ladder is the elephant.** It is a second, code-level posture
   system not in `DEFAULT_MODULES`. F-3 must decide: does the posture preset *write* the
   per-bot `openclaw.json::tier`, or does `tier` become a derived view of the posture?
   Either way the two must be reconciled into one operator concept (the whole point of
   this aspect).
3. **Default tier flip.** `full` is today's fail-open default ([config.ts:113](../packages/plugin/src/config.ts#L113)),
   contradicting "default toward minimal." Changing it is high-leverage but a behavior
   change for existing installs — needs the deploy/edr owners and a migration story.
4. **Non-reducible floor.** F-4's footprint-declaration contract must mark the items that
   *cannot* be dialed off without breaking/insecuring the bot (`gateway.auth.token`,
   `agents.main`→`defaults` migration, `logging.file`, npm re-pin, arguably the cost
   breakers) so a posture preset can never produce a non-booting or insecure config.

---

*F-stitch complete. The reconciliation is the F-3 input: the modules registry already
covers the RSI/cost/healing/metrics layer (§4); the gaps (§3) — gateway tier ladder,
`allowConversationAccess`, the costed audit/scanner/tuples surfaces, the install-time
privilege surface — are what the posture dial must add or absorb. Routing per spec:
gateway/exec → `edr`+`deploy`; appliers/generators → `rsi`; app audit/scanner → `apps`;
cost breakers → `model-tiers`+`edr`; UI presentation → `ui`. This aspect owns the
catalog + contract + dial.*
