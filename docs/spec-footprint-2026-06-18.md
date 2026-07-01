# Spec: META:footprint — Evolve's install footprint & invasiveness posture (coordinator charter)

**Date:** 2026-06-18 · **Status:** F-1 audit + F-2.5 graph done; **FD-5 engine design settled** (see below) — F-3 build is next. F-4 slice 1 (auto-gen output declaration contract + lint) landed — see § "Auto-generated output declaration contract".
**Aspect id:** `footprint` · **Session name:** `META footprint` · **Chip prefix:** `[META:footprint]`

This is the **coordinator charter** for the `footprint` META aspect — the durable
concern of *how much Evolve mutates / costs / intercepts an OpenClaw install, vs.
merely observes it, and whether that invasiveness is dialable down*. It is a
**cross-cutting / coordinating** aspect: it owns the platform-wide footprint
**catalog** and the **posture-dial contract**, and routes each implementation slice
to the subsystem owner (`deploy`, `edr`/security, `skills`, `rsi`, `reports`,
`model-tiers`). It does not rebuild those subsystems — it gives them a shared
"declare your footprint + honor the posture" contract.

---

## Mission

Make Evolve's footprint on an OpenClaw install **legible and configurable**. An
operator should be able to get the benefits of Evolve (legibility, safety,
updates) without an unconditionally heavy, invasive layer — and should be able to
*see* exactly what Evolve changes vs. what it only watches. The north star is the
positioning already in memory: Evolve as a **thin packaging layer** over OpenClaw
(Plex/Ubuntu analogy), *don't reimplement upstream*, substrate-first — invasiveness
is opt-in, not the default.

## Why this is its own aspect (carve rationale, 2026-06-18)

The concern spans ≥5 existing aspects and has no single owner:

| Footprint surface | Subsystem owner |
|---|---|
| Daemons, sudoers, ACLs, config writes, repo-puller, OC safe-upgrade | `deploy` (+ `platform`) |
| Gateway hooks / turn interception, routing rewrites, enforcement-that-blocks | `edr`/security + `model-tiers` |
| Plugins/skills/MCP injected into the bot, exec policy | `skills` |
| Generators + appliers that mutate `openclaw.json` | `rsi` |
| Monitors / signal producers (some read-only, some token-spending) | `reports` |

`deploy` is the closest single home but its charter explicitly hands off
runtime/monitor/per-app/cost concerns. The **unifying** question — total
invasiveness budget, mutate-vs-observe classification, and a posture dial — needs a
coordinating owner. Carved confirm-first with the operator 2026-06-18.

## The four footprint dimensions

"Invasive" is not one axis. A monitor can be observe-only yet expensive; a hook can
be cheap yet behavior-altering. Every subsystem is classified on **all four**:

1. **Mutation** — changes config/state the bot loads, or how OC behaves
   (openclaw.json / auth-profiles writes, plugin injection, exec-policy flips,
   model-routing rewrites, L1/L2 appliers). The literal "alters how OpenClaw works."
2. **Runtime / hot-path** — runs inside or intercepts the bot's turn loop (gateway
   plugin code, `agent_end`/`llm_output` hooks, `hooks.allowConversationAccess`).
   The performance/latency tax.
3. **Cost** — spends tokens (LLM-driven scanner, tier-3 audits, generators,
   escalation monitors). The surprise-bill tax.
4. **Privilege / surface** — daemons, sudoers grants, ACLs, the managed git
   checkout, the `evo` account. The security-and-bugs footprint.

## The existing `modules` system IS the substrate (operator constraint, 2026-06-18)

**Do not build a parallel toggle system.** Evolve already has a module registry —
the posture dial extends it; the deliverable is **consolidated, accurate UI over
it**, not a second surface beside it.

- **Store:** `network.json["modules"]` — per-module `{enabled, ...tunable settings}`,
  with per-bot overrides (`evolve_config.set_bot_module_enabled` /
  `is_bot_module_enabled`).
- **Catalog:** `DEFAULT_MODULES` in `packages/analyzer/evolve_config.py` (~L204):
  `rsi` (master switch), `observer`, `metrics`, `healing`, `analysis` (+per-detector
  toggles), `apply`, `continuity_engine`, `expansion`, `slack_signals`, `outcomes`,
  `cost`, `community_intel`. Several already default-off (`continuity_engine`,
  `slack_signals`, `community_intel`).
- **UI:** Settings → **Modules** subtab (`web/static/js/pages/settings.js`
  `renderModules`) — feature cards that **already** render **Benefit / Cost / Note**
  per module + an enable toggle + tunable thresholds; the RSI loop is a master switch.

So the "invasiveness budget" is **half-built**: the RSI/analysis/cost/healing layer
has toggles *and a benefit-vs-cost framing already*. The **gaps** are the dimensions
`modules` doesn't yet cover — the install-time **privilege** footprint
(daemons/sudoers/ACLs), the **gateway hot-path** interception, config
**mutation/appliers** beyond `apply`, and most **monitors**. Reconciling the F-1
catalog against `DEFAULT_MODULES` (which subsystems have a module key vs. which are
unconditional gaps) is a first-class deliverable, and the consolidated UI replaces /
absorbs today's Modules subtab rather than adding a sibling.

## The design direction (operator-settled 2026-06-18)

**A single invasiveness posture dial + per-subsystem overrides, built ON the existing
`modules` registry** — not a wall of checkboxes, and not a parallel system. The dial =
named **presets over module states** (each posture is a set of `modules.*.enabled` +
key tunables); advanced operators still get the per-module cards (today's Modules
subtab, consolidated + gap-filled). Precedent: `pod.release.mode = canary` dials
deploy behavior from one knob. Proposed posture levels (to be validated against the
real catalog + registry):

- **Passive / "dashboard mode"** — observe-only; no hot-path hooks, no routing
  rewrites, no auto-appliers; token-spending monitors off or sampled. Evolve =
  legibility + inventory + managed updater. Minimal footprint.
- **Standard** — + costed monitors, + proposals (suggest, don't auto-apply).
- **Managed / active** — + auto-appliers, tier cascade, hooks, autonomy ladder.

Advanced operators override individual subsystems. The headline dial sets coherent
defaults so the Plex-test operator never lands in an incoherent partial state.
Surfaced on the **Settings** page (presentation → `ui`; content/contract → here).

## Invariants / guardrails

- **Footprint contract:** every invasive subsystem must *declare* its footprint
  (which of the four dimensions) and *honor the posture* (be reducible to its
  passive form). New daemons/hooks/generators that skip this are a footprint-aspect
  finding, not an accepted default.
- **Default toward minimal, opt-in toward invasive** — consistent with
  [[feedback_product_defaults_in_code]] (a fresh install must not render dead
  invasive affordances) and the thin-layer positioning.
- **Don't reimplement upstream** ([[feedback_dont_reimplement_upstream]]) — reuse
  OC's own opt-in mechanisms (hook opt-in, exec policy, plugin posture) rather than
  inventing parallel toggles.
- **Coordinating, not rebuilding** — implementation slices route to the owning
  aspect via deposit; this aspect owns the catalog, the contract, and the dial.
- **Legibility is first-class** — the operator must be able to *see* the current
  footprint (what's mutating, what it costs) as clearly as toggle it.
- A toggle that silently leaves the subsystem half-on is worse than no toggle —
  posture changes must be verifiable (Gate 2: observe on the live pod).

## F-1 audit findings + F-3 design direction (operator-settled 2026-06-18)

F-1 is **done** — consolidated catalog at
[docs/footprint-catalog-2026-06-18.md](footprint-catalog-2026-06-18.md). The
load-bearing findings and the operator's resolved design forks:

**Finding — two posture systems exist.** Besides `DEFAULT_MODULES`, the gateway plugin
has a per-bot **`tier` ladder** (`off`/`monitor`/`manage`/`full`,
`packages/plugin/src/config.ts` ~L40-152) that gates almost the entire hot path
(routing rewrites, content hooks, prompt injection), defaults to **`full`**
(fail-toward-invasive), and is invisible to the modules registry + Settings UI.

**FD-1 (settled): posture is the single authoritative operator concept; it WRITES the
per-bot `openclaw.json::tier` AND the module states as derived outputs.** The tier
ladder becomes an implementation detail, not a separate dial. Precedence:
posture preset → per-module override → per-bot override.

**FD-2 (REVISED 2026-06-18 — operator): default = `full`. The dial is a power-user
opt-OUT, not the shipped posture.** Disabling components is for operators with specific
performance/footprint reasons; the out-of-box experience stays the full managed
product. This *reverses* the earlier "default Standard / flip the gateway off full"
call. Consequences: **no fleet-wide migration, no gateway-default flip, no behavior
change for existing or new installs** — the posture machinery is purely additive
(full is the default value of the new dial). Lower risk on the default; the safety bar
moves entirely onto the *disable process* (FD-5).

**FD-5 (NEW, settled 2026-06-18 — operator): disabling must be dependency-aware and the
disable PROCESS must not break an install.** This is now the aspect's hardest problem
(see "Safe-disable / dependency model" below). Components depend on other components
being active; a naive disable can cascade an install into a broken or half-on state.
The engine needs a declared dependency graph, preflight cascade simulation, an
infra-floor that is not in the normal dial, validated+atomic apply, and one-command
recovery.

### Safe-disable / dependency model (FD-5 — the core engineering problem)

Turning a component off is not a free toggle — the F-1 catalog shows a real dependency
spine. Design requirements:

1. **Declared dependency graph.** Each component declares `requires: [...]`. The engine
   **refuses or cascade-disables-with-consent** any state where an *enabled* component's
   requirement is *disabled* — never a half-on state (spec: "a toggle that leaves a
   subsystem half-on is worse than no toggle"). The actual edges are mapped from code by
   the F-2.5 dependency audit (below), not assumed.
2. **An infra floor that is NOT in the normal dial.** Some "components" are
   infrastructure, not features: the gateway-plugin presence, sudoers grants, the
   `.openclaw/` ACLs, the admin daemon, and the security-update path of repo-puller.
   Disabling them breaks Evolve's control plane or its safety, so they are excluded from
   the preset flow — at most behind an explicit "expert / may break things" gate with
   stated consequences, never a casual toggle.
3. **Validated, atomic apply.** A posture change is a config mutation (writes
   `openclaw.json::tier` + `network.json::modules`, then kickstarts daemons). Route it
   through the existing validated-apply discipline (L2-applier shape: stage → validate →
   apply → kickstart; **canary an affected bot** for risky combos —
   [[feedback_canary_for_one_file_edits]]), NOT a naive write. A bad combo or a mid-write
   failure must never brick a bot.
4. **Preflight cascade simulation.** Before applying, compute and show the operator the
   full downstream effect from the graph ("disabling Observer also stops Analysis,
   Metrics, Tuples; dashboards go empty — proceed?"). "Think about what turning this off
   impacts" is surfaced *at the toggle*, computed, not left to the operator's memory.
5. **One-command recovery.** Posture state is just config, so every change is reversible
   (rewrite + kickstart), mirroring `release rollback`. A self-heal can also restore a
   floor element if a disable is detected to have broken the control plane.

**Two design calls settled (operator, 2026-06-18):**
- **Orphaned-dependent handling = cascade-disable-with-consent** (show the blast radius,
  disable the chain together), not block-and-require-manual-order. Block stays the
  fallback for anything touching the infra floor.
- **v1 dial scope = cascade + safe-leaf components ONLY; the infra floor is out of scope**
  (not even an expert gate in v1). This keeps the lockout/irreversibility class entirely
  off the table while still giving power users every footprint cut that matters (cost
  surfaces, hooks, routing, generators all live in the cascade/leaf tiers).

The dependency spine observed in F-1 (to be verified/edge-mapped by F-2.5): the gateway
plugin is the runtime root (no plugin ⇒ no hooks/routing/tools/ledgers, bot reverts to
vanilla OC); `allowConversationAccess` gates all content hooks, which feed
observations/metrics/cost-ledger; observations+metrics feed `analysis` → proposals →
`apply` → `outcomes`; `cost` feeds the L1 breaker (safety floor, FD-3); the
outward-action ledger backs the autonomy caps; sudoers/ACLs underpin the admin server's
ability to manage bots at all.

**FD-3 (settled): the interrupting cost breakers (L1 daily-cap, runaway downgrade,
spend-cap) and `security_warden` stay ON even in Passive, framed as SAFETY not
feature.** Passive must not silently drop the safety floor. Co-own with
`edr`/`model-tiers`.

**FD-4 (settled): fix the four Modules-UI accuracy bugs NOW as a small bite** (independent
of the posture redesign): (a) the `observer` module key gates nothing — remove/redirect
it (its real gate is the tier ladder); (b) the RSI "no model tokens" copy is false —
scope it to "improvement work" and note tier-3 audit/scanner/tuples spend separately;
(c) `cost` is a load-bearing safety gate with no UI card — surface it, framed as safety,
warn on disable; (d) `cost_watchdog` isn't gated by `cost` — fix the gating or the
mental-model copy. UI copy is `ui`-co-owned (style-guide). **DONE** ([#3017](https://github.com/evolve-ops/evolve/pull/3017)).

### FD-5 engine design (settled 2026-06-18, against the verified F-2.5 graph)

The disable engine is designed against the **verified** dependency graph
([docs/footprint-dependency-graph-2026-06-18.md](footprint-dependency-graph-2026-06-18.md)):
its §1 component table, §3 hazard table, §4 infra floor, and §5 cascade groups are the
engine's input data. The engine has six parts. **It lives admin-side** (Python, the
control plane) — posture is a `network.json` + per-bot `openclaw.json` mutation
kickstarted by the admin daemon — and requires **exactly one plugin-code change** (the
FD-8 breaker-only mode, below); everything else is admin Python + config.

**1. Component graph (the data model).** A code-resident registry —
`FOOTPRINT_COMPONENTS` — is the single source of truth ([[feedback_product_defaults_in_code]]),
**seeded from the F-2.5 graph**. It is a *superset* of `DEFAULT_MODULES`: it also carries
the tier-ladder capabilities (`observer`, `modelRouting`, `injectKeywords`,
`injectPodConduct`, `preflight`) — which are **not** module keys but real dial targets —
and the infra-floor items (for the guard, even though they're out of the dial). Each node:
`{id, kind: module|tier_capability|infra|daemon, classification:
infra-floor|cascade|safe-leaf, requires[], required_by[] (derived), footprint_dims[],
safety_floor: bool, gate: <where it is actually read>, unverified: bool}`. The §6 soft
edges are marked `unverified: true` and treated **fail-safe** — an unverified `requires`
is honored as real, so the engine cascades conservatively rather than risk a silent
half-state. A **load-time validator** (CI gate, sudo-grant-lint-style) asserts: no cycles,
every `requires` target exists, every `safety_floor` item is reachable — so the graph
can't drift as F-4 lands new components.

**2. State model + resolution (FD-1 precedence).** Posture state lives beside the modules
it extends: `network.json::posture = { preset: "full"|"custom", overrides: { <component>:
<bool> } }`, **default `preset: full`** (FD-2 — purely additive; absent key ⇒ full ⇒ no
migration, no behavior change). The engine **resolves** desired component states (preset
matrix → per-module override → per-bot override, FD-1) and then **derives the concrete
writes**: the per-bot `openclaw.json::tier` value is *computed* as the highest tier
whose capabilities all resolve on (e.g. `injectKeywords` off but `modelRouting` on ⇒
`tier: manage`), and `network.json::modules` states follow directly. The tier ladder
stays an implementation detail the operator never sets by hand.

**3. Preflight cascade simulation (FD-5 #4, consumes graph §3).** Given the current
resolved state + a requested disable, the engine computes the **closure**: walk
`required_by[]` to find every still-enabled component left starving, then classify the
closure against the §3 hazard table → a **preflight report**: `{ requested, cascade_set[],
blocked[] (touches floor — H1-H3), safety_held[] (CG-3 / security_warden — H4/H6),
blast_radius, reversible: true }`. This is the "think about what turning this off
impacts," *computed at the toggle* and shown to the operator ("disabling Observer also
stops Metrics, Analysis, Tuples; dashboards go empty — proceed?"). The dial domain is the
**cascade + safe-leaf** rows only (FD-7); a request that resolves onto a floor item is a
guard-block, not an offered toggle.

**4. Cascade-with-consent + apply order (FD-6, consumes graph §5).** The operator confirms
the `cascade_set`; the engine applies the **whole group atomically** in a
**topologically-sorted order derived from the graph** — **consumers before producers** on
disable (outcomes → apply → analysis → metrics/tuples → observer), producers before
consumers on re-enable — so no enabled component is ever left starving mid-apply (spine
direction, graph §2). The order is computed, never hand-listed.

**5. Validated, atomic apply (L2-applier shape + canary).** A posture change writes
across (potentially) every bot's `openclaw.json::tier` + pod-wide `network.json`. Route
through the validated-apply discipline, **not** a naive write:
**stage** all writes to `/tmp` (CLAUDE.md write pattern) → **validate** by re-running graph
resolution *on the staged result* and asserting **no half-state remains** (every enabled
component's `requires` satisfied), no floor item touched, schema valid — this is the
"never half-on" invariant enforced *mechanically* → **apply** via `sudo cp` + `chown` +
`chmod 0600` for secrets + kickstart only the affected daemons (recovery.py pattern) →
**canary** for risky combos (anything dropping a tier capability or cascading the observe
spine): apply to **one** affected bot first, observe live (Gate 2), then fan out
([[feedback_canary_for_one_file_edits]]); a single safe-leaf flip skips the canary.
**Atomicity:** the `network.json::posture` record is written **last**, after all bot writes
succeed, so it is the recovery anchor — a mid-fan-out failure leaves the previous posture
record intact, and because the target is declarative the engine **heals forward**
(re-applies) rather than rolling back.

**6. One-command recovery.** Posture is just config, so every change reverses by rewriting
the previous posture record + kickstart, mirroring `release rollback`:
`evolve-admin posture rollback` restores the prior record from a kept history, re-derives,
re-applies. Belt-and-suspenders for the floor: `healing` (`heal.py`) is **independent of
the observe spine** (graph §1e, the REFUTED edge) and *restores the plugin* — so it is the
natural floor self-heal; if a disable is ever detected to have broken the control plane,
heal.py restores the floor element regardless of posture. Since the floor is never in the
dial (FD-7), this is defense-in-depth, not the primary guard.

**FD-8 (RESOLVED 2026-06-18 — routing↔breaker coupling, graph §3/H4): breaker-only mode.**
The runaway-rate cap and spend-cap safety breakers live *inside* the `modelRouting`
gateway hook ([ModelRouter.ts:2726](../packages/plugin/src/observer/ModelRouter.ts#L2726),
[:193](../packages/plugin/src/observer/ModelRouter.ts#L193)), so a posture that cuts
routing (tier < `manage` — a legitimate footprint reduction) would silently drop the
breakers (the H4 safety regression). **Resolution:** the engine splits `modelRouting`
into two facets — **tier-optimization** (the cost/tier model-override rewrites = the
actual dialable footprint) and **breaker enforcement** (`isSpendCapActive` → force-`fast`/
pause + `runawayRateCap` = the safety floor, FD-3). When a posture cuts routing **and any
spend/runaway cap is set**, the engine does **not** fully drop the hook: it writes a
**breaker-only routing config** so the hook still loads but evaluates *only* the caps
(no tier-optimization). If no cap is set, routing drops entirely (nothing to protect).
This needs **the one plugin change** FD-5 requires — a `routing.mode: "breaker_only" |
"full"` flag the hook honors (the `before_model_resolve` gate short-circuits past
tier-optimization but still runs the cap checks). Chosen over decoupling the breakers
into a separate always-on daemon because it is the **smaller change** and keeps the
safety logic in one place. **Co-owned with `model-tiers`** (owns `ModelRouter`) **and
`edr`** (owns the safety-floor policy); dispatched as the plugin-side F-3 slice, gated on
this design.

**Net F-3 build shape** (gated by this design): (1) admin-side — `FOOTPRINT_COMPONENTS`
registry + load validator; posture storage/resolution in `evolve_config`; the
preflight/cascade/validated-apply engine; `evolve-admin posture` CLI (set/show/rollback)
+ `/api/posture` endpoints; (2) plugin-side (routes to `model-tiers`+`edr`) — the
`routing.mode` breaker-only flag; (3) UI (routes to `ui`) — the consolidated posture dial
that **absorbs** today's Modules subtab, showing the preflight blast radius at the toggle.

## Backlog (seed)

- **F-1 — The audit / catalog (FIRST).** Exhaustive code sweep: every way Evolve
  touches an OpenClaw install, each tagged on the four dimensions + current
  toggle-state (already-gated vs. unconditional). Deposit to
  `docs/footprint-catalog-2026-06-18.md`. Fan-out: deploy/privilege · runtime+gateway
  · cost/monitors · config-mutation+appliers.
- **F-2 — Settings audit.** Does Settings expose any footprint control coherently
  today? Map existing scattered toggles (plugin posture inventory-only default, OC
  memory kill-switch, hook opt-in, exec policy) → gaps.
- **F-3 — Posture-dial build.** Design **settled** ("FD-5 engine design" above, against
  the verified F-2.5 graph). Build shape: (1) admin-side `FOOTPRINT_COMPONENTS` registry +
  load validator, posture storage/resolution in `evolve_config`, the
  preflight/cascade/validated-apply engine, `evolve-admin posture` CLI + `/api/posture`;
  (2) plugin-side `routing.mode` breaker-only flag (→ `model-tiers`+`edr`); (3) consolidated
  UI absorbing the Modules subtab, blast-radius at the toggle (→ `ui`). Sequence:
  storage/resolution → engine → CLI → plugin flag → UI.
- **F-4 — Footprint-declaration contract.** How a subsystem declares its footprint +
  passive form — likely an extension of the `DEFAULT_MODULES` entry shape (footprint
  dimensions + benefit/cost are already partly there) — so the catalog + UI stay
  current as new subsystems land. **First slice landed:** the *auto-generated output*
  declaration contract + CI lint (see § below) — the author-time gate for disk
  footprint. Remaining: the runtime/cost/privilege declaration facets fold into the
  same `FOOTPRINT_COMPONENTS` node as F-3 builds it out.

## Auto-generated output declaration contract (F-4 slice 1 — author-time gate)

Motivating audit: [docs/footprint-disk-output-audit-2026-06-28.md](footprint-disk-output-audit-2026-06-28.md).
The F-5 disk-output audit found **537 MB** of audit records under
`audit_outbox/_ingested` that *nothing reads* and *nothing prunes*. The root cause
was structural, not a single bug: nothing required a producer that writes to an
auto-generated surface to declare **who reads the output** or **who prunes it**, so
write-only sediment could accumulate unnoticed. This contract closes that gap *by
construction* — the **forward-discipline** half of the F-5 remediation (the source
cuts F-5-A/B/P3 fix the existing leak; this stops new ones).

**The two auto-generated surfaces it governs:** `{shared_dir}/**` (the pod-wide
arbiter/signals/watchdog/incidents/alerts store) and `~/.openclaw/workspace/evolve/**`
(per-bot workspace telemetry).

**Scope — record/telemetry, not bounded state.** The contract governs *accumulating*
output (per-record files, append-only JSONL, dated archive dirs — the sediment
class). It deliberately does **not** police every write under `{shared_dir}/**`: that
tree also holds bounded **state stores** (breaker state, autonomy limits,
config-sandbox overrides, baselines) that are overwritten in place, not accumulated.
The discriminator is **record-shape** (a `.jsonl`, a catalogued record subdir, or a
record-path helper).

**The declaration shape** (`footprint.components.OutputDeclaration`, seeded into the
`FOOTPRINT_COMPONENTS` registry F-3 extends — *not* a parallel registry). Per output:

- `path_glob` + `surface` — where it lands.
- `writer` — `relpath.py:func`, the write site (the lint's coverage unit).
- `cadence` — when/how often a write happens.
- `volume_files` / `volume_bytes` — expected **steady-state** volume after retention
  (the F-5-F4b budget monitor turns these into a runtime ceiling).
- **`retention`** (required) — who prunes it (`module:func` / `overwrite-in-place` /
  operator) + the window. An *unbounded* window REQUIRES a justification.
- **`consumer`** (required) — who reads it + the read site, OR an explicit
  `consumer: none` WITH a justification. **A `consumer: none` with no (finite)
  retention is forbidden** — that is exactly the `_ingested` failure mode.

**The lint** (`tools/footprint-output-lint`, modeled on `signal-protection-lint`):
AST taint-lite finds every record/log write to the two surfaces and BLOCKs when the
writing file is not claimed by a declaration (plus registry-consistency +
stale-declaration guards). Hybrid severity: high-confidence record writes BLOCK
always; a heuristic-ambiguous append tier warns and is promoted to BLOCK under
`--strict`. Wired into `.githooks/pre-commit` (`--staged`), `ci.yml`
(`--all --strict`, the Silent-exception ratchet job), and `tools/preflight`.

**Known limitation:** a write routed through a generic append helper that *receives*
the path (the rooty path is built at the caller) is not caught at the helper — and a
brand-new `.json` per-record store under a *novel* subdir can slip past static
detection. The runtime cardinality-budget monitor (F-5-F4b) is the reactive backstop
for that residual.

**Backfill = the documented inventory.** The registry backfills every current
record/log producer the lint detects (signals/proposals/watchdog/observations/
profiles/calibration stores; the audit pipeline incl. the `_ingested` tombstone;
incidents; alert dispatcher logs; manifests; admin queues; the anthropic ingest log;
roster/directory audit logs; the outcome calibration dataset; the synthesis log; …),
so a green lint doubles as the disk-output inventory.

## Reconciliation invariant (operator, 2026-06-18)

The posture work **reconciles with and extends the existing `modules` system** — same
store (`network.json["modules"]`), same per-bot override path, the consolidated UI
**absorbs** the current Settings→Modules subtab. Building a parallel toggle surface is
the anti-pattern this aspect exists to prevent (consistent with
[[feedback_dont_reimplement_upstream]] applied internally). Accuracy is a gate: the UI
must reflect what each module *actually* gates (verified against code), not a
hand-maintained label that drifts.

## Boundary / hand-offs

- Implementation of any toggle → the **owning subsystem aspect** (deploy/edr/skills/
  rsi/reports/model-tiers) via deposit. This aspect designs the contract + dial.
- Settings page **presentation** → `ui`; the posture-control **content/contract** →
  here.
- Security *enforcement* semantics of "passive mode" (does turning off the warden
  reduce safety below an acceptable floor?) co-owned with `edr`/security.
- Cost accounting of the dimensions → feeds from `model-tiers` (per-bot is the cost
  unit).

## Deploy mechanism

Heterogeneous (mirrors the subsystems it coordinates): admin-only + canary-gated
(`pod.release.mode=canary`) for Settings/posture-storage/admin reads; per-bot gateway
kickstart + `deploy <bot>` for changes that alter the bot's runtime footprint;
`refresh-sudoers` if a posture removes/adds a grant. Verify posture changes on the
live pod (Gate 2) — a toggle that doesn't actually quiet the subsystem is the bug.
