# Model rungs & roles — design spec

**Status:** Draft — approved direction (design session 2026-06-09).

**Date:** 2026-06-09.

**Supersedes:** the numeric tier scheme (`tier0`–`tier3`), now rewritten as
[docs/model-roles.md](model-roles.md) (formerly `docs/model-tiers.md`).

**Related:**
- [docs/spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md) —
  the user-facing tier picker this spec extends (adds `max`, renames nothing
  user-visible).
- the cost-alerting-blackout postmortem
  ([docs/incident-cost-alerting-blackout-2026-05-20.md](incident-cost-alerting-blackout-2026-05-20.md))
  — why the new top rung must be pull-only.

---

## Motivation

Anthropic shipped Claude Fable 5 (`claude-fable-5`): a tier *above* Opus at 2×
Opus pricing ($10/$50 per MTok vs $5/$25). Our ladder has no rung for it, and
adding one exposed three structural problems in the current scheme:

1. **The three-rung assumption is hard-coded.** `_USER_TIER_TO_KEY` in
   [ModelRouter.ts:318](../packages/plugin/src/observer/ModelRouter.ts) and the
   `fast | standard | power | auto` enum in
   [SetTierTool.ts:47](../packages/plugin/src/tools/SetTierTool.ts) bake in
   exactly three user-selectable gradations.
2. **Numeric keys encode ordering implicitly.** There is no natural key for a
   rung above `tier1`, and inserting a rung between `tier2` and `tier3` would
   mean renumbering — but tier identifiers are *persisted* (annotations record
   `model_tier`, cascade logs, signal payloads, per-bot `evolve-tiers.json`,
   `user-tier-prefs.json`). A renumbering identifier must never be stored.
3. **`tier0` already breaks the ordering.** Judge sits numerically below Power
   but is not "more powerful" — it's a *provider-diversity* selection at
   Workhorse strength. The numbering conflates capability order with role.

The fix is to split the two concepts the numbers were conflating:

- **Rungs** — the catalog: capability clusters in cost order.
- **Roles** — the interface: stable semantic names that code, users, and
  telemetry speak.

## Design principles

1. **Numbers are derived, never stored.** Rung order is the position in a
   config array. If the admin UI wants to display "1–N", it computes that at
   render time. Nothing persisted ever references a rung by number.
2. **Code and users speak roles only.** Every routing decision, tool schema,
   log line, annotation, and UI label uses a role ID or a resolved model ID.
   Rung slugs appear only in config and the catalog UI.
3. **The hard ordering dependency is cost, not capability.** Every consumer of
   rank in the codebase today means "force cheaper" (spend cap, runaway cap,
   background classification) or "spend more/less" (cascade). Cost is scalar
   and stays scalar. If the model market stops being a single
   price/performance line (workload-specialized clusters: code, creative,
   spatial…), rung order quietly becomes a pure *cost* order — which the
   downgrade paths still need — and capability-fit moves into how roles
   resolve. Only the role-resolution function changes; no consumer of
   `standard`/`power`/`max` moves. This is the named escape hatch; see
   §Non-goals.

---

## The model

### Rungs

An **ordered array** in `network.json::models.rungs`. Array position = rank
(cheapest first, most powerful last). Each rung:

- `id` — stable slug, named for its Anthropic anchor class
  (`haiku-class`, `sonnet-class`, `opus-class`, `fable-class`). The slug is an
  identifier, not a promise — if the anchor model retires, the slug may stay.
- `models` — the cross-provider cluster: primary first, fallbacks after.
  This absorbs the existing per-tier fallback chains unchanged.
- `costClass` — `low | medium | high | premium` (adds `premium` for
  Fable-class; feeds cost reporting as today).

Rungs are added **when the pod adopts a model**, not speculatively. A rung
earns its row when a role points at it or it's in a fallback chain.

### Roles

A **map** in `network.json::models.roles` from role ID → rung slug (or an
object for constrained roles). Canonical role IDs — one namespace, used
everywhere (tool schemas, routing config, annotations, UI values):

| Role | Replaces | Default rung | Purpose |
|---|---|---|---|
| `fast` | tier3 / "Grunt" | `haiku-class` | Background, maintenance, internal analysis. Users never see its output directly. |
| `standard` | tier2 / "Workhorse" | `sonnet-class` | Default for productive user-facing sessions. |
| `power` | tier1 / "Power" | `opus-class` | High-complexity work; user-requested or cascade-escalated. Daily-capped. |
| `max` | — (new) | `fable-class` | The frontier model. **Pull-only** (see below). Daily-capped, lower default cap. |
| `judge` | tier0 / "Judge" | `sonnet-class` + provider constraint | Cross-model evaluation. Selected by provider diversity, not strength. |

"Grunt" / "Workhorse" / "Power" / "Max" / "Judge" survive as **display labels
only** (admin UI). The old user-choice enum (`fast | standard | power`) and
the old internal names collapse into this single namespace — there is no more
tierN↔name↔choice triple translation.

`judge` is the one structured role:

```json
"judge": { "rung": "sonnet-class", "provider": "not-standard" }
```

`provider: "not-standard"` is the Goodhart's-law constraint
([docs/model-roles.md §judge constraint](model-roles.md#the-judge-constraint-goodharts-law)) promoted from documentation to
a checkable invariant: role resolution picks the first model in the rung's
cluster whose provider differs from `standard`'s resolved provider, and
config validation (loader + `evolve-admin models set`) errors if no such
model exists.

### Config shape

```json
"models": {
  "rungs": [
    { "id": "haiku-class",  "models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini", "google/gemini-2.0-flash"], "costClass": "low" },
    { "id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"], "costClass": "medium" },
    { "id": "opus-class",   "models": ["anthropic/claude-opus-4-8"], "costClass": "high" },
    { "id": "fable-class",  "models": ["anthropic/claude-fable-5"], "costClass": "premium" }
  ],
  "roles": {
    "fast":     "haiku-class",
    "standard": "sonnet-class",
    "power":    "opus-class",
    "max":      "fable-class",
    "judge":    { "rung": "sonnet-class", "provider": "not-standard" }
  },
  "routing": {
    "enabled": true,
    "maintenanceRole": "fast",
    "backgroundRole": "fast",
    "ambiguousRole": null
  }
}
```

Note the model bumps riding along: `power` moves to `claude-opus-4-8` (the
example config's `claude-opus-4-6` is two versions stale), and `max` lands on
`claude-fable-5`. Fable API note for any code that constructs requests: same
surface as Opus 4.7/4.8, except an explicit `thinking: {type: "disabled"}`
returns 400 — omit the param instead.

---

## `max` semantics — pull-only

The defining property of `max`: **no automatic path routes to it.**

1. **Reachable only by explicit choice** — the admin-UI tier picker, the
   `session_set_tier` tool forwarding an explicit user request, or a per-user
   default set via `evo tier-default max`.
2. **Excluded from the cascade.** The cascade controller's escalation verdict
   may reach `power` (as today) but never `max`. Rationale: nearly all routing
   machinery routes *down* for cost control; the only upward paths are
   deliberate. A model at 2× Opus pricing must not be reachable by a silent
   escalation — the 2026-05-20 cost-blackout lesson. Revisit only after
   pull-only usage data shows where auto-escalation would have earned its
   cost (optimize-for-learning).
3. **Excluded from classifier routing.** `maintenanceRole` / `backgroundRole`
   / `ambiguousRole` and per-bot `defaultRole` validate against
   `{fast, standard, power}` — configuring `max` as a default is a config
   error.
4. **`bot_initiated` escalation to `max` is blocked by default.**
   `userTierOverride.allowBotInitiated` becomes per-role:
   `allowBotInitiated: { power: <legacy value>, max: false }`. A bot may
   forward a user's explicit ask (`ask_hint_agreed` / explicit phrasing) but
   may not unilaterally pin Fable.
5. **Safety nets still win.** Spend-cap and runaway-rate downgrades force
   `fast` regardless of any choice, exactly as today
   (precedence ladder unchanged, see below). The per-bot daily cost cap
   (PR #1483 auto-trip) is the blast-radius backstop for a user who pins
   `max` and walks away.
6. **Own daily cap.** The tier1 cap machinery (`canEscalateToTier1`,
   `maxPerDayPerBot`) generalizes to per-role caps:

   ```json
   "roleCaps": {
     "power": { "maxPerDayPerBot": 10 },
     "max":   { "maxPerDayPerBot": 5 }
   }
   ```

   On cap hit, `max` degrades to `power` (then `power`'s own cap may degrade
   to `standard`), with the same visible "capped today, used X" surfacing the
   power cap has.

### Routing precedence (unchanged shape, renamed values)

Highest wins — only the names change, plus `max` slotting into the
user-choice rungs:

1. Runaway-rate cap tripped → force `fast`
2. Spend-cap downgrade active → force `fast`
3. `/model` command (`ctx.userModelOverride`) → respect, no override
4. User role choice (chip / `session_set_tier` / per-user default):
   `fast | standard | power | max`
5. Cascade verdict (when enabled): may select `fast | standard | power`
6. Classifier: maintenance/background → `maintenanceRole`/`backgroundRole`
7. Operator per-bot default → bot default

---

## Migration inventory

The identifier rename touches a known, bounded set. Everything below ships in
one deploy (git pull + kickstart is atomic enough for the single pod).

### Config & loaders

- **`network.json` / `network.example.json`** — `models.tiers` → `rungs` +
  `roles`; `routing.maintenanceTier` etc. → `*Role`.
- **`ModelRouter.ts`** — `ModelRouterConfig.tiers` → `rungs`/`roles`;
  `_USER_TIER_TO_KEY` deleted (choices *are* roles now);
  `_tierPreferenceRank` → role preference rank for reverse model→role lookup
  (`standard` 0, `power` 1, `max` 2, `fast` 3, `judge` 4); `canEscalateToTier1`
  → `canEscalateToRole(role)`; safety-net sentinel renames
  (`evolve/safety-net-blocked-fast-unconfigured`).
- **Legacy-shape fallback (fail open):** the loader accepts the old
  `models.tiers.tierN` shape and synthesizes rungs/roles from it
  (tier3→fast, tier2→standard, tier1→power, tier0→judge), logging a
  deprecation warning. This protects a pod whose config wasn't migrated;
  remove after one release cycle.
- **One-shot migration:** `evolve-admin migrate-model-roles` rewrites
  `network.json`, each bot's `~/.openclaw/evolve-tiers.json`, and
  `{sharedDir}/{botId}/user-tier-prefs.json` values from tier keys to role
  IDs. Idempotent; run during deploy.

### Surfaces

- **`SetTierTool.ts`** — choice enum becomes
  `fast | standard | power | max | auto`; description gains max guidance
  ("the frontier model at ~2× power cost; only on explicit user request");
  cost gate generalizes to per-role caps; `bot_initiated` + `max` blocked by
  default (§max semantics #4).
- **Admin UI** ([ai-optimization.js](../packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js),
  home chat tier chip in home_chat_routes.py) — picker gains **Max** with a
  cost hint ("2× Power"); Tier Definitions / Model Catalog panels render the
  rung array (derived numbering allowed here, display-only); role→rung
  mapping editable. **All UI work follows
  [docs/style-guide.md](style-guide.md)** — both themes verified.
- **`evolve-admin models` CLI** — `set`/`show`/`list`/`usage` speak roles;
  `set` validates the judge provider-diversity invariant and warns on
  role→rung re-points that change costClass by more than one step.
- **Per-user prefs (`evo tier-default`)** — accepts `max`; values already
  role-shaped, no schema change beyond the new value.

### Telemetry & records

- **Annotations** — `model_tier: "tier2"` → `model_role: "standard"`; bump
  annotation `schema_version`; readers accept both fields for the transition.
- **Cascade log / signals** — record role IDs; historical records with tier
  keys are read-only and stay valid (the legacy synthesis map above doubles
  as the read-side translation for old records).
- **Cost reporting** — keyed by `costClass`, picks up `premium`
  automatically; usage breakdowns group by role.
- **Model drift detection (`cost.py expectedModel`)** — unchanged (speaks
  concrete model IDs).

### Docs

- Rewrite [docs/model-tiers.md](model-tiers.md) → `docs/model-roles.md`
  around rungs/roles (keep a "formerly tierN" mapping table).
- Update [docs/spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md)
  references via a banner note, not a rewrite.

---

## Freshness check rework — discovery, not list-matching

**Added 2026-06-09 (same design session).** The pod's model freshness check
ran the day Fable 5 was public and reported "all models current."

### Root cause

`check_bot_freshness()`
([packages/analyzer/model_registry.py:119](../packages/analyzer/model_registry.py))
iterates only over the hardcoded `RECOMMENDED` dict (model_registry.py:38–60)
— a hand-maintained provider→tier→model table whose latest Anthropic entries
predate Fable. The check never queries any provider; "current" means "matches
our hand-edited list," which is circular: the check is exactly as fresh as
the last human edit to `model_registry.py`. A new *version in a known line*
gets caught only after someone updates the dict; a new *line* (Fable) is
structurally invisible. The companion `find_catalog_drift()` checks are
consistency-only (tier↔catalog agreement), not discovery.

### Design: enumerate, diff, propose

The freshness check becomes **discovery-based**, built on the rungs/roles
catalog:

1. **Enumerate** — for each credentialed provider, fetch the live model
   listing (Anthropic `GET /v1/models`, OpenAI `GET /v1/models`, Google
   equivalent). Pure HTTP against keys the pod already holds; no LLM, no web
   search. Cheap enough for the daily generator sweep.
2. **Diff** — partition listed models against the pod's rungs:
   - **Known** — in some rung's cluster: check *within-family* staleness
     (newer version in the same family per the provider listing). The
     "latest in family" fact now comes from the listing, not the dict.
   - **Unknown** — chat-capable models in no rung and not on the ignore
     list: a **discovery finding**. This is the class that catches Fable.
   - **Out of scope** — embeddings/audio/specialty models (filtered by the
     listing's capability metadata) and dated snapshot aliases of known
     models: auto-ignored.
3. **Propose, never auto-categorize.** Rung placement is a judgment call and
   provider listings carry no pricing, so discovery emits a Signal
   (`type: model_discovery`, signature per (provider, model-id) — the signal
   store dedups re-fires) and the generator turns it into a Proposal:
   *"claude-fable-5 discovered from Anthropic; in no rung. Suggested
   placement: new rung above opus-class."* The suggestion is heuristic
   (context window, max output, capability flags, family naming) with an
   optional single `fast`-role LLM call for the rationale — and every claim
   on the proposal cites its evidence (listing fields), per the
   cite-or-don't-recommend rule. Accepting the proposal is the operator
   action that edits `models.rungs`/`roles` (consistent with §Non-goals #2:
   rungs are added on adoption — discovery *suggests* adoption, it doesn't
   perform it).
4. **`RECOMMENDED` shrinks to a fallback.** The dict stops being the source
   of truth about the market. Keep it only as an offline fallback for
   providers whose listing call fails (logged as degraded), or delete it
   outright if all credentialed providers have listing endpoints. No code
   path may report "current" from the dict alone without flagging that
   discovery was skipped — silence and "all current" must not be
   indistinguishable (the silent-monitor-drift lesson).
5. **Ownership** — stays under the `bot_config_integrity` guardian (new
   check id `model_discovery` in its charter inventory, fingerprint bumped)
   or a sibling generator if its charter scope reads cleaner at build time.
   Notification path must be verified end-to-end: Signal → Alerts page →
   operator chat (check `signal_notifier` producer allowlist — both sources
   of truth).

---

## Non-goals (and the assumption ledger)

1. **Max+/Max− relative addressing.** Resolving "one rung above/below a role"
   falls out of the ordered list for free, but exposing it reintroduces the
   instability this spec removes ("Max−" silently changes meaning when a rung
   is inserted). If a user wants Opus specifically, the honest interface is
   `power`. Revisit only on a demonstrated workflow.
2. **Speculative rung cataloging.** No rungs for models the pod doesn't use.
3. **Multi-axis capability clusters.** Named assumption: *rung order is
   currently both the cost order and the capability order because the market
   happens to put models on one line. The system's hard dependency is only on
   the cost order. If those diverge — workload-specialized models (code,
   creative, spatial, visual) — capability-fit moves into role resolution
   (roles consult a workload dimension declared by manifest/charter/
   classifier) and rungs keep cost ordering. Only the resolution function and
   its config change; every consumer of role IDs is untouched.* This spec
   deliberately does not build that.
4. **Auto-escalation to `max`.** See §max semantics #2 — pull-only in v1,
   reconsidered against usage data.
5. **`classifiers.*.model` role references.** `network.json::classifiers`
   still names concrete models; allowing `"role:fast"` there is a nice
   follow-on cleanup, out of scope here.

---

## Phasing (for build sessions)

**Phase 1 — core (plugin + config).** ModelRouter rungs/roles refactor,
legacy-shape synthesis, per-role caps (`canEscalateToRole`), precedence
renames, SetTierTool `max` choice + gates, annotation field + schema bump,
migration command. Tests: routing precedence matrix incl. max-pull-only and
cap-degradation chains (`max`→`power`→`standard`); legacy-config synthesis;
judge provider-diversity validation. ⚠️ Worktree note: run tests with the
conftest rebind (editable-install shadow).

**Phase 2 — surfaces.** Admin UI picker + catalog panels, home-chat chip,
`evolve-admin models` CLI, `evo tier-default max`. Style-guide compliance +
both themes.

**Phase 3 — docs + deploy + verify.** model-roles.md rewrite, deploy to the
mini, run `migrate-model-roles`, canary: one low-traffic bot first
(per canary-for-one-file-edits practice — config + kickstart is the
destructive step), verify routing log shows role IDs, verify a `max` pull
resolves to `claude-fable-5` and a background session still lands on Haiku,
confirm Usage tile/summary agree. Also verify the Phase 4 discovery check:
run it against live provider listings and confirm it would have flagged
Fable (temporarily remove `fable-class` from a test config and assert a
`model_discovery` Signal fires).

**Phase 4 — freshness discovery (parallel with Phase 2).** §Freshness check
rework: provider-listing enumeration, rung diff, `model_discovery` Signal +
Proposal with cited evidence, ignore-list, `RECOMMENDED` demotion, charter
update + fingerprint bump, notifier allowlist check. Python/analyzer-side;
independent of Phase 2's UI surface.

Each phase is a PR; Phase 1 must land before 2/3/4 start. Phase 3 deploys
last, after 2 and 4 merge.

---

## Addendum (2026-06-10): adoption applier + design envelope

Two additions after the pod-wide ship. First: adopting a discovered model is
manual today — the shipped `model_discovery` proposal is an *Investigation*,
so accepting it edits nothing, and there is no UI to create a rung or map the
`max` role (operator hit this gap on day one). That deviates from this spec's
own intent ("accepting the proposal is the operator action that edits
`models.rungs`/`roles`"). Second: market research (2026-06-10, primary-source
verified) sharpens the design envelope.

### A. Adoption applier — accepting a discovery edits the catalog

1. **`AdoptModel` action** replaces Investigation on `model_discovery`
   proposals. The proposal carries: provider, model id, suggested rung slug
   (family heuristic, e.g. `fable-class`), suggested ladder position
   (cost-rank insertion point), and the cited listing evidence.
2. **Operator choices at approval** (proposal card): role mapping —
   `none` (default; the rung is adopted as a dormant catalog entry) or an
   existing role (e.g. `max`); when mapping `max`/`power`, a cap seed
   (default `roleCaps.max.maxPerDayPerBot: 5`). Mapping `max` is never
   pre-selected — adopting a frontier model and arming it are separate
   deliberate acts in one card.
3. **Applier** (arbiter applier, modeled on `tier_adjustment`): creates or
   extends the rung, splices it at the suggested position, applies the
   optional role re-point + cap seed, validates the judge provider-diversity
   invariant, writes through the normalized write path (PR #2551), idempotent,
   and records the applied config delta on the proposal for audit.
4. **Catalog locus — pod-wide-first.** Adoption writes the rung to
   `network.json::models.rungs` (the pod catalog); per-bot
   `evolve-tiers.json` remains an *override layer*. This requires the loader
   (and the Python `resolve_tier_chain` helper) to move from block-precedence
   to **keyed merge**: rungs merge by `id` (a per-bot rung with the same id
   wins; pod-only rungs are appended in pod order), roles merge by key
   (per-bot wins). Today every bot carries per-bot rungs, so block-precedence
   would make a pod-wide adoption silently invisible — the exact ambiguity
   behind the known-set incident. No config-file migration needed; the change
   is read-side merge semantics only.
5. **End-to-end acceptance test** (the lifecycle that closes the loop):
   discovery proposes fable → operator approves with `max` + cap → catalog
   gains rung/role/cap → ModelRouter resolves `max` → next discovery run
   treats fable as known → the `model_discovery` Signal auto-resolves.

### B. Design envelope (market research, 2026-06-10)

Findings (primary-source verified except where noted), and what each changes:

1. **Rung-count envelope: design for 1–6 per provider.** Anthropic is already
   at *five* public gradations — Haiku/Sonnet/Opus/Fable **plus Mythos 5**
   (limited availability, Fable-priced) — while xAI has flattened to a single
   price band split by workload. Nothing may assume four rungs; roles stay
   five regardless (unmapped rungs are dormant catalog entries).
2. **Cross-provider clustering is best-effort, not an invariant.** The middle
   bands cluster cleanly (Sonnet ≈ GPT-5.4 ≈ Gemini Pro); the ends diverge —
   the frontier rung is Anthropic-only today, and the fast band spans a 10×
   price spread. A rung MAY be single-provider; never force-fit a cluster.
3. **Workload-split variants at one price** (xAI reasoning/coding/
   multi-agent): a rung's cluster may legitimately contain same-price
   workload variants. Role resolution keeps picking the primary; workload-
   aware resolution stays in the §Non-goals ledger.
4. **The effort axis is confirmed, industry-wide** — every major provider now
   ships a per-request compute knob that changes effective cost on the SAME
   model (OpenAI `reasoning.effort` none→xhigh; Google `thinking_level`;
   Anthropic Opus Fast mode at 2× price; xAI reasoning/non-reasoning
   variants). **Anticipated, not built:** rungs remain a price ladder of
   models; when Evolve first needs per-request effort control, it lands as an
   optional qualifier on rung model entries plus a cost-class multiplier in
   cost reporting — those are the two reserved landing spots. The §Non-goals
   assumption ledger is hereby updated: the "multi-axis future" partially
   arrived as effort-per-request; the exit path is unchanged.
5. **Mythos 5 expectation:** discovery will surface `mythos` as its own
   family when it appears in the listing — correct behavior for a
   parallel-frontier SKU; the operator decides (likely ignore-list until
   generally available).

### Phasing

**Phase 5 — adoption applier (one PR + independent review).** `AdoptModel`
action + applier, keyed loader/helper merge, proposal-card role/cap picker
(style-guide compliant, both themes), the end-to-end lifecycle test above.
Deploy via the standard pull + admin-ui kickstart; verify by adopting Fable
for real on the live pod as the acceptance run.

---

## Addendum 2 (2026-06-10): the default catalog ships in code

Operator review of the shipped adoption flow surfaced a layering error:
making `max` exist only via per-instance proposal acceptance turns a
**product capability** into **instance state**. Every pod would drift on
whether Max exists; a fresh install would render a Max button that resolves
to nothing. The rule this codifies: **product capabilities ship as code
defaults; proposals and config carry instance-specific state.** AdoptModel
remains the right machinery for models Evolve's defaults don't know yet
(the next Mythos, a new provider's frontier) — it is not the delivery
vehicle for Evolve's own blessed ladder.

### Design — a third layer in the existing keyed merge

```
code defaults (ships with Evolve) ← network.json (pod) ← evolve-tiers.json (bot)
```

1. **`DEFAULT_MODEL_CATALOG`** — canonical rungs/roles/roleCaps shipped in
   code: all five roles mapped, including `fable-class →
   anthropic/claude-fable-5` with `roleCaps.max.maxPerDayPerBot: 5`. One
   canonical definition per side (Python analyzer + TS plugin), each marked
   `KEEP IN SYNC` alongside the existing mirrored maps (`TIER_TO_ROLE`),
   plus a `MODEL LAUNCH:` comment marker so release-time updates are
   greppable. The loaders merge it as the base layer using the **same keyed
   rules** (rungs by `id`, roles/roleCaps by key; pod overrides defaults,
   bot overrides pod).
2. **Max ships armed, not dormant.** Cost safety never depended on
   dormancy: pull-only routing, the per-role daily cap, and the per-bot
   cost breaker all hold. A Max affordance that errors until configured is
   the exact "inherent in the product" failure this addendum fixes.
3. **AI Optimization page renders all five roles unconditionally** from the
   merged view, each editable, each labeled with its winning layer
   (default / pod / bot) — extending the existing source-provenance label.
4. **Discovery knows the defaults.** `known_model_set` unions the default
   catalog, so default-ladder models are never "discoveries." The pending
   Fable AdoptModel proposal auto-resolves through the existing lifecycle
   (model becomes known → signature not kept → signal resolves → proposal
   sweep-resolves).
5. **Defaults vs freshness truth (not a RECOMMENDED regression).** The
   demoted RECOMMENDED dict failed as a *market truth* source — hardcoded
   knowledge masquerading as "current." The default catalog is *product
   defaults*: allowed to lag the market, updated each release,
   per-pod overridable — and discovery exists precisely to surface when it
   lags.
6. **No migration.** This is a read-side base layer; existing pod/bot
   config wins wherever it speaks. `evolve-admin models show` displays
   layer provenance.

### Phasing

**Phase 6 — default catalog (one PR + independent review + deploy).**
DEFAULT_MODEL_CATALOG both sides + loader base-layer merge, AI-Optimization
all-roles rendering with provenance labels, known-set union, AdoptModel
copy narrowed to "models outside Evolve defaults." Acceptance on the live
pod: a bot with no fable config anywhere resolves `max` →
`claude-fable-5`; the pending Fable card auto-resolves; a chip Max pull
works end-to-end.

---

## Addendum 3 (2026-06-10): availability-aware resolution + rank presentation (Phase 6b)

Two operator-review threads, one surfaces phase.

### A. Availability-aware resolution (no dead Max on any pod)

A pod without a credentialed provider for a role's rung must not render a
working-looking control that fails at the provider. Three layers:

1. **Credential-aware resolution.** A role resolves to the first model in
   its rung whose provider is credentialed (provider-key discovery already
   exists from Phase 4). Multi-provider rungs degrade gracefully inside the
   rung; a rung with no credentialed provider resolves to nothing,
   explicitly.
2. **Downward degradation with the honesty string.** An unresolvable role
   degrades down the ladder through the SAME chain and visible-message
   machinery as cap hits. Degradation reasons unify into one concept:
   `cap_exhausted | uncredentialed | unconfigured` — one chain, one message
   pattern ("Max isn't available on this pod — used Power for this turn"),
   never silent, never upward.
3. **Disabled-with-reason UI, never hidden.** The chip and the
   AI-Optimization page keep rendering every role; an unavailable role is
   grayed with a computed reason. Precedent: judge's
   unsatisfiable-diversity behavior (boot warn + fall through).

### B. No provider/model literals in logic (the three-homes rule)

Provider and model names may live in exactly three places: (1) catalog/index
DATA (DEFAULT_MODEL_CATALOG, network.json, evolve-tiers.json); (2) provider
ADAPTERS (per-provider listing endpoints, auth/key formats); (3) tests.
**Never in logic** — no provider conditionals, no literal fallbacks, no
names in UI templates. Availability is a set derivation:
`providers(rung cluster) ∩ credentialed_providers`. Tooltip reasons list the
computed provider set. The litmus: a new frontier provider must be a
one-line catalog-data edit with zero code changes.

Enforcement: a **provider-literal CI guard** (ratchet-style) over the
routing/availability/catalog-logic modules, with the three legitimate homes
allowlisted. Known seed violation: `adopt_model.py::_provider_of` bare-id
fallback to a hardcoded provider. A repo-wide audit runs separately
(spawned task); the guard's coverage grows as audited modules come clean.

### C. Rank presentation — ordered, metered, never numbered

The legacy `Tier 0–3` numerals (lower = stronger, judge wedged at 0) still
render in the AI-Optimization page (`_AI_TIER_LABELS`, tierNames at
ai-optimization.js:96/661). Retire them:

1. **Role rank is derived**: rank(role) = array index of the rung it points
   at. Computed at render; never stored; reorders itself when the catalog
   changes.
2. **Canonical ascending order everywhere** — Fast → Standard → Power → Max
   in every picker, panel, and usage table.
3. **Visual meter, not numerals** — a filled-steps indicator per role
   (steps = rung count, filled = rung rank) plus the costClass chip and a
   relational subtitle ("above Power, premium cost"). A future rung adds a
   step; nothing renumbers because nothing is numbered.
4. **Judge is off-ladder by design and by layout** — separate section under
   a divider, NO meter, copy: "Off the power ladder — chosen for provider
   diversity (cross-checks the pod's own work), not for strength."
5. Rung catalog panel may show derived ascending positions (1..N, cheapest
   first) — the one surface where a number reflects the array order it is
   derived from.

### D. Default-aware catalog coverage (no role resolves to a model OC drops)

A role can resolve to a code-DEFAULT rung model (the `max → claude-fable-5`
case) that no `tier`/role entry names and that the per-bot catalog — OpenClaw's
runtime allowlist (`agents.defaults.models`) — therefore does not contain. OC
silently drops any model absent from that allowlist, so the pull dies at the
gateway with no advisory. The fix:

1. **Default-aware coverage check.** The freshness/drift pass resolves every
   role through the merged `defaults ← pod ← bot` catalog and verifies the
   resolved model is in the bot's runtime catalog. A miss emits a new drift
   kind, `role_resolves_outside_catalog`, carrying the role id and the
   resolved model.
2. **One advisory surface, one fix.** The new kind renders in the same red
   catalog-drift banner as `tier_member_missing` (a model named in a role's
   `models[]` that the catalog lacks) and offers the same one-click
   Reconcile-catalog add. The two kinds are the same correctness failure
   (a model OC will silently drop); only the phrasing differs ("the resolved
   model for <Role>" vs "is in <Role>"). The stale "older model than the
   registry recommends" freshness copy is retired in favor of
   discovery + defaults + catalog-coverage language.
3. **Editable Max rung.** The per-bot Tier Definitions panel renders Max as a
   first-class editable role (its own `models[]`, stored under the `max` key
   the save path folds), so an operator can override the Evolve-default
   frontier model per bot without touching code or the pod layer.

Acceptance: a fresh pod whose primary bot has the default Max rung but no
catalog entry for `claude-fable-5` shows a drift row naming Max + the resolved
model, with a working one-click add; no role resolves to a model the catalog
silently drops once reconciled.

### Phasing

**Phase 6b — one PR + independent review + deploy** (after Phase 6 / PR
#2561 merges): credential-aware resolution + unified degradation reasons,
disabled-with-reason surfaces, provider-literal guard + seed-violation
fixes in owned modules, legacy-numeral retirement + rank meter + judge
separation. Acceptance: a pod stripped of a provider credential shows Max
grayed-with-reason and degrades a forced pull honestly; UI shows zero
"Tier N" strings; guard fails a planted literal.

---

## Addendum 4 (2026-06-10): engine config vs bot defaults — un-overloading the panel (Phase 7)

Operator review found the AI-Optimization "ENGINE TIER DEFAULTS" panel
conflates two unrelated questions under one label, and that a default-covered
tier renders as "(empty)" on the per-bot Tier Definitions page — making a
working default (max → claude-fable-5 via the code catalog) look broken.

### The two concepts, named

| Concept | Question it answers | Mechanism today |
|---|---|---|
| **Engine model config** | What models does Evolve's *background code* (analyzers, scanners, classifiers, generators, freshness) use for its non-bot LLM calls? | Mirrors the primary bot: `select_model_for_session` → `resolve_roles_with_provenance(config, primary_bot_id)` ([models.py:729](../packages/analyzer/models.py)). Borrows primary's credentials too. |
| **Bot tier defaults** | What does an *unconfigured bot* inherit per tier? | `DEFAULT_MODEL_CATALOG` (code) ← `network.json` (pod) ← `evolve-tiers.json` (bot) keyed merge. |

The panel title "Engine tier **defaults** — what background Evolve uses"
splices both readings ("defaults" reads as bot-fallback; "what the engine
uses" is engine-config). They are separate mechanisms; the UI just rendered
them as one.

### v1 scope (Phase 7)

**1. Reframe the engine panel — informational, mirrors primary, no knob.**
Rename "ENGINE TIER DEFAULTS" → "Engine models (background work)". Copy:
*"Evolve's background engine (analyzers, scanners, generators) runs on the
primary bot's tier config and credentials. Configure it by configuring the
primary bot."* Drop or recontextualize the per-row DEFAULT/BOT source badges
so they read as the engine's *resolved* models, not as a bot-defaults editor.
**No engine-override config** — deferred (operator decision 2026-06-10:
mirroring is fine for now).

**2. Inherited-default provenance on the Tier Definitions page (the core fix).**
Each of the five role rows renders its *resolved* model(s) with a provenance
badge — `Evolve default` / `pod` / `this bot` — from
`resolve_roles_with_provenance`. The current "(empty — add models below)"
disappears: a role with no per-bot override shows the inherited value
(faded/italic) plus:
- **Customize** — materializes a per-bot override seeded from the inherited
  value (writes via the safe bot-file path, PR #2596).
- **Revert to default** — drops the per-bot override, falling back to the
  pod/code layer.

So `max` on atlas reads *"claude-fable-5 · Evolve default · Customize"* instead
of "(empty)". Visibility without forcing per-bot population — consistent with
[[product-defaults-in-code]] (defaults work out of the box; customization is
opt-in, never a ritual).

### Non-goals (deferred, operator decision 2026-06-10)

1. **Engine independent override.** The seam (`resolve_roles_with_provenance`
   takes any bot/config) makes this a later one-surface addition; not v1.
2. **Preferred-provider tie-break** for mixed-credential pods. Provider-specific
   defaults are *already emergent* — the §6b availability resolver picks the
   first credentialed provider in each cross-provider rung cluster, so an
   OpenAI-only pod already resolves its defaults to OpenAI models. The only
   missing knob is a tie-break preference for pods credentialing *multiple*
   providers; deferred as nice-to-have.
3. **Per-provider cluster completeness (data, separable).** For emergent
   provider-defaults to be *good* on non-Anthropic pods, the default rungs need
   a credentialed model per major provider. Today `opus-class` and `fable-class`
   are Anthropic-only — so on an OpenAI-only pod `power`/`max` resolve to
   nothing (correctly, but with no fallback). Enriching the clusters
   (gpt-5.x / gemini-3.x, per the 2026-06-10 research) is a catalog-data task
   that can ship independently of Phase 7; this pod (Anthropic+Google) doesn't
   need it yet.

### Phasing

**Phase 7 — one PR + independent review + deploy.** Engine-panel reframe +
Tier-Definitions provenance UI (inherited badge, Customize, Revert). Pure
presentation + the existing safe-write path; no resolution-logic change (the
merged view already exists via `resolve_roles_with_provenance`). Acceptance:
atlas's Max row shows "claude-fable-5 · Evolve default" with a working
Customize/Revert; no tier renders "(empty)" when a default covers it.

---

## Addendum 5 (2026-06-10): pod-default editor + per-bot toggle (Phase 8)

Operator review of the shipped Phase 7 UI found the per-tier `BOT`/`DEFAULT`
provenance confusing, and surfaced two gaps. Root cause of the confusion: the
mixed per-tier state (Fast/Standard/Power/Judge = `BOT`, Max = `DEFAULT`) is a
**migration artifact** — migration wrote explicit per-bot configs for the four
old tiers and left the new Max to inherit — not a deliberate design. This
addendum replaces per-tier provenance *control* with a per-bot toggle and adds
the missing pod-default editor.

### What the operator observed (all correct)

1. **`BOT` vs `DEFAULT` is opaque.** They are *merge-layer* badges: `BOT` = the
   bot's `evolve-tiers.json` decided it; `DEFAULT` = no per-bot entry, so it
   fell through to the code `DEFAULT_MODEL_CATALOG`. Not "all from evo."
2. **No pod-default editor.** The pod `network.json::models` block holds only
   `embedding` — the rungs/roles layer is empty, so "the default" is
   *code-only* and uneditable. The operator expected to edit it on the POD tab.
3. **The default should be a provider-matched multi-model cluster** (like bot
   tiers). The mechanism already exists — §6b availability resolution picks the
   first *credentialed* provider in each cross-provider rung cluster, so an
   OpenAI-only bot already resolves the default to its OpenAI model — but the
   default is shown as a single resolved line, not the editable cluster.

### The model — same toggle pattern at two levels

Resolution stays the keyed merge `code ← pod ← bot` with per-bot
credentialed-provider selection (unchanged). The *control surface* collapses to
one consistent toggle at each layer:

**POD tab — "Default tier definitions" (new editor).** Multi-model clusters per
tier (Fast/Standard/Power/Max/Judge), edited exactly like the bot tier editor.
Seeded from the code `DEFAULT_MODEL_CATALOG`; edits write
`network.json::models.rungs/roles` (the pod layer). A **"Reset to Evolve
defaults"** clears the pod layer back to code. The engine panel stays a
separate, informational, read-only surface (mirrors the primary bot).

**Bot tabs — one per-bot toggle, replacing per-tier badges + Customize/Revert.**
A bot is either:
- **Use pod defaults** — the bot's `evolve-tiers.json` carries *no* rungs/roles;
  it inherits the merged default, provider-matched to that bot's credentials.
  The tab shows a read-only preview of the resolved clusters (reusing the
  Phase 7 `resolve_roles_with_provenance` view).
- **Custom** — the bot defines its own full tier set (`evolve-tiers.json` rungs/
  roles), editable as today. Materialized seeded-from-default on first
  customize.

The toggle *is* the control: **Customize this bot** (use-defaults → custom,
seed from resolved default) and **Reset to pod defaults** (custom → use-defaults,
clear per-bot rungs/roles). No per-tier provenance badges; a bot is default or
custom, full stop.

### Strict all-or-nothing (v1 decision)

Per operator preference, the per-bot toggle is **strict all-or-nothing** — a
bot does not mix inherited and overridden tiers. This is the simplification
that removes the confusing per-tier middle ground. Per-tier inheritance under
Custom is **deferred** (re-introduce only if a real need appears); the Phase 7
per-tier resolver view survives as the use-defaults *preview*, not a control.

### Migration / cleanup

Every bot currently reads "Custom" (migration gave them explicit per-tier
configs), much of it duplicating what the now-richer default provides. Phase 8
ships:
- The **Reset to pod defaults** path (clears a bot's rungs/roles → inherit).
- An optional advisory: a bot whose explicit config is equal-after-merge to the
  pod default is flagged as "redundant — reset to defaults?" (cheap, derived;
  no auto-action — operator-owned per [[product-defaults-in-code]]).

### Non-goals

1. **Per-tier override under Custom** — deferred (strict all-or-nothing v1).
2. **Pod preferred-provider tie-break** — still deferred (Addendum 3 §B); the
   cluster ordering decides multi-credential pods until then.
3. **Engine override** — still deferred (Addendum 4); engine mirrors primary.

### Phasing

**Phase 8 — one PR + independent review + deploy.** POD "Default tier
definitions" editor (writes `network.json::models`, safe-write path); per-bot
Use-defaults/Custom toggle replacing the per-tier control; Reset-to-defaults +
redundant-config advisory; retire the per-tier Customize/Revert. Pure
control-surface + the existing keyed-merge resolution (no resolution-logic
change). Acceptance: edit a Standard default cluster on POD → an OpenAI-only
bot in "use defaults" resolves Standard to the cluster's OpenAI model; flip a
bot to Custom and back; no per-tier `BOT`/`DEFAULT` badges remain.

---

## Addendum 6 (2026-06-10): model-entry UX — validated picker + easy-setup (Phase 9)

Operator feedback: typing model IDs into tier editors is error-prone (version
numbers, dashes/dots/underscores), and the "copy from the catalog above" dance
is clumsy. The fix is the same on every tier editor (POD default + bot tabs):
**pick from validated candidates, don't type.** A drag-and-drop vision was
explored; this addendum captures the elegant-and-sound subset to build, and
explicitly defers the fragile part.

### The candidate source already exists (one small add)

The freshness **discovery** (Addendum-era §Freshness rework) enumerates each
*credentialed* provider's live `/v1/models` per run — the authoritative,
correctly-spelled, currently-existing model list. It is **fetched but not
persisted** (used ephemerally for the diff). Phase 9's only infra dependency:
discovery writes its enumerated listings to a cache (e.g.
`{shared_dir}/model-listings.json`) on each run; the UI reads it. Cheap; also
makes the listings inspectable. Provenance stays data-sourced (no provider
literals in logic — the listing IS data).

### v1 scope — build these

1. **Validated model picker (the accessible baseline; replaces free-text
   entry).** Each tier row's "Add model" becomes a picker grouped by
   credentialed provider, listing real current models (canonical IDs) from the
   cached listings. **Tier-appropriateness is a SOFT signal** — models in that
   tier's default cluster are highlighted "suggested," but *all* credentialed
   models are pickable (no hard gate: an operator may want a cheap model in a
   higher tier for cost). A free-text "add anyway" path **validates against the
   listing**: exists → add canonically-spelled; not found → reject with a
   nearest-match suggestion ("not in <provider>'s current models — did you mean
   `<x>`?"). Universal across the POD default editor and bot tier editors.

2. **Easy-setup wizard (the 90% path, the star).** One button → asks for a
   **provider-order preference** → auto-populates every tier with appropriate
   models in fallback order matching the preference. Most operators never touch
   the manual editor after this. **This retires the deferred preferred-provider
   tie-break** (Addenda 3 §B / 4): it becomes an interactive question asked at
   the moment it matters, not a buried config knob. Server-side populate action
   (testable). On both POD default and bot tabs.

3. **Within-tier drag-to-reorder fallback (the one DnD piece worth it).** Drag
   chips left-to-right to set fallback order within a tier; the resolver already
   treats `models[]` order as the fallback chain. **Reorder buttons (↑↓) stay as
   the accessible fallback** — DnD is an enhancement over them, never the only
   path.

4. **Color-by-provider on chips (permanent — provider is a fact).** Honest,
   cheap legibility; instantly shows provider diversity in a tier. Tier-fit is a
   *contextual* "suggested here" highlight, NOT a permanent chip pattern.

### Non-goals — explicitly NOT built (and why)

1. **Full pool→tier drag-and-drop.** The validated picker already solves "add a
   model" cleanly and accessibly; full DnD-from-a-pool adds the most build cost
   and fragility for the least marginal value. Three concrete reasons it's
   deferred to a future progressive-enhancement, not v1: (a) this admin SPA has
   no JS test harness — UI is gated by Python assertions on JS *source strings*,
   so DnD interaction logic would ship essentially untested on a config surface;
   (b) native HTML5 DnD is keyboard/screen-reader-hostile with no repo precedent
   to lean on, and the "mildly-tech-capable / Plex-test" constraint requires a
   non-drag path to always exist (the picker is it); (c) drop-zones/drag-ghosts
   need theme-safe styling from scratch in both modes. **A future agent must not
   add pool→tier DnD as a "nice extra" — it is a deliberate deferral.**
2. **Pattern-coding chips by tier-appropriateness** (softened to the contextual
   "suggested here" highlight — appropriateness is a judgment, not a chip
   property).

### Constraints

Vanilla-JS SPA; Python-source-string test pattern (the picker + easy-setup
populate are server-testable; the DnD-reorder's testable path is its
reorder-button equivalent). Both themes; style-guide (width classes, token
vars, ai-layer-chip reuse). No provider/model literals in logic — the picker
sources from cached listings (data). Resolution logic unchanged.

### Phasing (build splits to respect the ~one-bite agent envelope)

- **Phase 9a** — listings-persistence (discovery writes the cache) + the
  validated picker (POD + bot editors) with listing-validated free-text.
- **Phase 9b** — easy-setup wizard (provider-preference → populate + fallback
  order) + within-tier drag-to-reorder (with button fallback) + color-by-
  provider.

Acceptance: a tier editor offers a grouped credentialed-provider picker (no
free typing needed); a typed nonexistent model is rejected with a suggestion;
one easy-setup click with a provider preference populates all tiers sensibly;
chips are provider-colored; fallback reorders by drag and by buttons.

---

## Addendum 7 (2026-06-11): Phase 9 UX refinement (Phase 10)

Operator review of the shipped Phase 9 surfaces. 14 items in five workstreams.
Two are bigger than they look (flagged ⚠).

### A. Visual consistency & color

1. **Catalog tiles must match tier-definition tiles.** The Model Catalog chips
   and the tier-row chips currently differ in color/style — unify them (same
   chip component, provider-colored).
2. **Provider colors are too subtle** (the 9c hash approach makes Claude/OpenAI
   near-identical). Replace the hash with an **explicit provider→color map**,
   bold and distinct: **Anthropic = orange, OpenAI = green, Google = yellow,
   xAI = purple**, others from a distinct palette (pink/red/blue/gray for
   unknown). Lint note: an explicit provider→color map is *presentation data*
   (same category as `_aiProviderLabel`) — implement as a presentation map or
   `ai-provider-<name>` CSS classes so provider-literal-lint treats it like the
   label map, NOT logic. Colors are token vars (dark/light pairs in base.css).

### B. Tier-row controls

3. **The right-column ↑↓ and × "don't work" — remove them.** (Investigate the
   9c wiring bug, but the resolution is removal, not repair.) Reorder stays
   **drag-only**; move a **× onto each model chip** (inline, like the catalog
   chips). a11y note: drag-only loses keyboard reorder — acceptable for a
   single-operator admin tool per operator direction; revisit if needed.

### C. Catalog-as-pool restructure (⚠ the structural one)

4. **The exhaustive model list belongs in the CATALOG, not the tier editor.**
   - **Bot Model Catalog**: the "add model" control offers the full credentialed-
     provider listing (the pool). The tier editor (bot Custom) then offers
     **only models already in that bot's catalog**, organized by recommended
     tier. The catalog *is* the pool the tiers draw from.
   - **POD default editor has no OC catalog** (the catalog is a per-bot runtime
     allowlist; the pod default isn't gated by one). So the POD tier editor
     draws from the **full credentialed listings** directly. This asymmetry is
     correct: bot-custom tiers pick from the bot's allowlist (it will run them);
     pod defaults may name any model (a bot inheriting it gets the §6 reconcile
     drift advisory + one-click catalog-add).
5. **In the catalog's exhaustive add-model dropdown, bold the latest model per
   family** (reduce version confusion). Reuse discovery's family-latest
   computation.
6. **Free-text "or type a model ID" lives in the CATALOG only** (validated, as
   today), NOT in the tier editor.

### D. Layout & copy (POD tab)

7. **Make the Model Freshness box collapsible** (`.expand-icon`; collapsed by
   default — it's large).
8. **Update "How Evolve Models Work"** to explain the defaults/inheritance model
   (code default ← pod default ← per-bot; Use-defaults vs Custom).
9. **Engine Models panel: drop the BOT/DEFAULT source chips.** The engine
   resolves entirely from the primary bot (evo) — the per-row provenance is
   confusing noise on this panel (provenance still belongs on evo's own tab).
10. **Move the Engine Models panel AND the Engine Override below the "Default
    tier definitions" section** on the POD tab — defaults lead, engine info
    follows.

### E. Easy-setup correctness (⚠ the compute one)

11. **Easy-setup button: prominent, at the TOP of the tier-definitions block**
    (POD/default view), not a small secondary button.
12. **LLM-providers only in the wizard.** Brave and Runway are not LLM providers
    — exclude them (derive the LLM-provider set from the catalog clusters, not
    raw credentialed providers). DeepSeek belongs if credentialed.
13. **⚠ Easy-setup must populate every tier with every credentialed provider's
    appropriate model**, ordered by preference — not just reorder a sparse
    cluster. The shipped preview showed only Claude across tiers + OpenAI/Google
    partial because the **default clusters are thin**. Fix requires folding in
    the deferred **per-provider cluster completeness**: `DEFAULT_MODEL_CATALOG`
    must carry, for each tier, the appropriate (latest) model from each major
    provider. Then "reorder by preference" yields claude/google/openai/xai in
    each tier where that provider has a tier-appropriate model (max stays
    Anthropic-only until a peer frontier ships). The per-provider tier mapping
    stays current via discovery (a newer model → discovery proposes the cluster
    bump). "Appropriate = latest in that provider's line for the tier."
14. **Judge inverts the preference.** Judge's purpose is provider *diversity*,
    so easy-setup must order judge's models leading with the highest-preference
    provider that is **NOT** standard's resolved provider (secondary-first,
    primary-second) — keep the `{rung, provider:"not-standard"}` form valid.

### Phasing (build in bites)

- **10a** — color system (explicit provider→token-var map, dark/light pairs) +
  catalog/tier tile unification + inline-× on chips + remove the broken ↑↓/×
  column (workstreams A, B).
- **10b** — catalog-as-pool restructure: catalog gets the exhaustive picker +
  free-text + latest-in-family bolding; bot tier editor constrained to catalog;
  pod tier editor from full listings; free-text removed from tier editors
  (workstream C).
- **10c** — POD layout/copy: collapsible freshness, engine-panel chip removal +
  reorder below defaults, "How Evolve Models Work" rewrite, easy-setup button
  prominence (workstream D).
- **10d** — easy-setup compute: DEFAULT_MODEL_CATALOG per-provider enrichment,
  every-provider-per-tier populate, LLM-provider filter, judge secondary-first
  (workstream E — the substantive one).

Acceptance: chips are vividly provider-colored and identical in catalog & tiers;
tier rows have an inline × and drag-reorder, no broken side controls; the
exhaustive list + free-text are catalog-only (latest-in-family bold), tier
editors pick from catalog (bot) / listings (pod); freshness collapses; engine
panel has no source chips and sits below the editable default; easy-setup is
top + prominent, lists only LLM providers, and populates every tier with each
provider's latest tier-appropriate model ordered by preference (judge
secondary-first).

---

## Addendum 8 (2026-06-11): authoritative catalog — identity from listings, tier from pricing (Phase 11)

Phase 10d shipped a default catalog containing a **non-existent** OpenAI model
(`openai/gpt-5.5` / `gpt-5.4-mini`) because the enrichment was hand-typed from a
stale `model_registry.RECOMMENDED` dict — the exact "only as fresh as the last
human edit" rot the §Freshness-check rework was written to kill. Live verification
also surfaced two structural defects: (a) the easy-setup / pod-default **write
path mints synthetic rung ids** (`fast-default`…) instead of reusing the code rung
ids (`haiku-class`…), so the keyed merge *accumulates* instead of *overlays* — the
per-bot merged ladder bloats to ~9 rungs, the rank meter (§Addendum3.C) goes
non-monotonic, and `costClass` is dropped; and (b) the bogus IDs were copied into
the **pod `network.json`** (seeded from the buggy default), so a code-only fix does
not change live resolution. This addendum makes the catalog derive from authoritative
data instead of a hand table, and repairs the write path.

### The two facts, two sources

A model has two facts with different best sources:

| Fact | Authoritative source | Have it? |
|---|---|---|
| **Identity** — exact ID, exists, latest-in-family | the provider's own `/v1/models` listing (already enumerated by discovery into `{shared_dir}/model-listings.json`) | ✅ |
| **Tier** — which cost band | **per-token pricing** (the listing carries none — verified: records hold id/caps/sometimes context, no price) | ❌ add |

**Principle:** identity is *self-updating* (always the latest-in-family from the live
listing — never hand-typed into code, never free-typed); tier is the *only* curated
thing, and it is curated at the **family** level (families and their bands are stable;
versions churn every release) and **computed from pricing** wherever a price exists.

### A. Identity — from the listing, never hand-typed

Default catalog, pod-default editor, and easy-setup all populate model IDs by picking
the latest-in-family from the enumerated listing. Phase 9 already built this validated
picker for the UI; the code default + easy-setup compute bypassed it — close that gap.
A CI guard asserts every default-catalog model for a *credentialed* provider exists in
that provider's listing (the regression guard already added by the 10d hotfix).

### B. Tier — computed from a normalized pricing catalog (research-backed 2026-06-11)

Providers expose **no structured pricing** (HTML docs + incurred-cost usage APIs only),
so a third-party normalized catalog is required. Decision:

1. **Primary: LiteLLM `model_prices_and_context_window.json`** — MIT, single static
   mirrorable JSON, broadest coverage, strongest freshness posture (day-0 intent +
   6-hour auto-sync precedent). Dedup by `litellm_provider` (it keys the same logical
   model per serving surface). Pricing in $/token.
2. **Cross-check / fallback: models.dev `api.json`** — MIT, cleaner normalization,
   uniquely carries `family` + `release_date` (feeds the fallback map). Pricing in $/M.
3. **OpenRouter `/api/v1/models`** — fastest to *list* new models; use ONLY as an
   internal **freshness tripwire** ("OpenRouter lists X our pricing source lacks"). Its
   ToS grants no redistribution license — never republish its data. (Raw API emits
   $/token as strings — easy 1e6× error; normalize on ingest.)
4. Mirror the chosen catalog to `{shared_dir}/model-pricing.json` on the discovery
   sweep (same pattern as `model-listings.json`); the source URL is config, not a
   literal in logic. **Compute** the cost band from `input_cost_per_token` (and/or a
   blended in+out figure) — never hand-assign.

**Fallback for the freshness gap (honest):** every catalog is community-PR-maintained,
so a brand-new frontier model can appear in the provider's own listing *before* any
pricing catalog has it. Cover the window with a tiny hand-curated **`family → cost-band`
map** (~one row per family × ~6 providers), keyed on the family parsed from the
*correctly-spelled ID the provider listing already gives us*. Resolution: exact catalog
row → computed band; miss → family-band; family unknown → emit a `model_discovery`
Signal (do **not** invent a tier — that is the rot this addendum removes). When the
catalog PR lands, the exact row supersedes the fallback automatically.

### C. Catalog derivation + freshness rework

The freshness check becomes: enumerate listings (identity) → join pricing (band) →
the default catalog's per-(provider,tier) model = latest-in-family for the family that
maps to that band. A model in a *new* family → `model_discovery` proposal with **cited
pricing evidence** (per cite-or-don't-recommend). **`RECOMMENDED`'s identity role is
deleted** (the spec's own demotion, regressed by 10d); it survives only as a last-ditch
offline fallback if both catalogs and the family map miss, and even then must flag that
discovery was degraded.

### D. Catalog-write correctness (the `*-default` / meter repair)

1. The pod-default editor + easy-setup **write the canonical code rung ids**
   (`haiku-class`…), so the keyed merge *overlays* the pod onto code instead of
   accumulating; they also write `costClass`.
2. **Rank derives from the role→rung ladder order** (`fast<standard<power<max`, judge
   off-ladder), not the raw merged-rung-array index — robust to stray/duplicate rungs.
3. A **migration** reconciles existing pod `*-default` rungs (rename to canonical ids,
   backfill `costClass`); after the identity fix lands, a **pod-config remediation**
   (admin) re-seeds `network.json::models` so the bogus IDs drop from live resolution.

### Phasing

- **Hotfix (in flight, 10d fast-follow):** correct the OpenAI IDs in code
  `DEFAULT_MODEL_CATALOG` + `RECOMMENDED` (both sides) + parity fixtures + the
  listing-existence CI guard.
- **Phase 11a — catalog-write correctness (one PR + review + deploy).** §D: rung-id
  reuse + `costClass` in the write path, rank-from-ladder-order, the migration, and the
  live pod-config remediation. Dispatch *after* the hotfix merges (both edit
  `primary_bot.py`). Acceptance: a use-defaults bot shows a monotonic 4-step meter
  (Fast<Standard<Power<Max), cost chips populated, and no `*-default` rungs remain.
- **Phase 11b — authoritative pricing + freshness rework (one PR + review + deploy).**
  §B/§C: pricing-catalog ingestion + mirror, band computation, family-band fallback,
  the freshness rework, `RECOMMENDED` identity-role removal. Acceptance: discovery
  prices known models from the catalog, proposes a new-family model with cited pricing,
  and a model absent from every source emits a Signal rather than a fabricated tier.

---

## Addendum 9 — as-built deltas after Addendum 8 (2026-06-12)

Phase 10 + 11a/11b shipped as specified. This addendum records the design choices made
**during** the build that extend Addendum 8 — they are live and verified on-pod; capture
them here so the spec matches the code. (Memory: `[[model-rungs-and-roles]]`.)

### §A — Provider-adapter set + self-surfacing gap

The discovery listing adapters (`model_discovery._LISTING_PROVIDERS`) cover six providers:
`anthropic, openai, google, xai, deepseek, mistral`. Each `_fetch_<provider>` hits that
provider's `/v1/models` (Bearer-auth) so model **identity** is self-updating per Addendum 8 §A.

When a provider is **credentialed on the pod but has no listing adapter**, discovery emits a
gap Signal rather than silently omitting it:

```
uncovered = sorted((credentialed & _LLM_PROVIDERS) - set(_LISTING_PROVIDERS))
```

`_LLM_PROVIDERS` is a **data mirror of the admin "llm" provider category** — *not* the catalog
clusters (which would miss an uncatalogued LLM) and *not* every `api_key` provider (which would
false-fire on non-LLM creds like `brave`/`runway`, the bug the independent review caught). This
keeps the "add a new frontier provider = one data edit" property (`[[no-provider-literals-in-logic]]`).

### §B — Observed cost as a third band source

`model_cost_bands.resolve_band()` resolves a model's cost band from, in order:
**(1)** external pricing catalog (LiteLLM primary + models.dev cross-check),
**(2)** `observed_band()` — the pod's own effective cost telemetry, **(3)** family→band map,
**(4)** Signal (never a fabricated tier). Observed cost slots **between** authoritative pricing
and the family fallback: real-but-unlisted is better evidence than a family guess, weaker than a
published rate.

Observed cost comes from `usage_analytics._observed_per_1k()`:
`usd_per_1k_{input,output,blended} = total_$ / (tokens / 1000)`. This is an **effective, all-in
cost that includes cache read/write spend** — *not* a clean per-token list rate. Cache tokens are
deliberately **excluded from the denominator** so the figure reflects what the model actually costs
the pod to run; dividing all-in spend by all-in tokens would make heavily-cached models look
artificially cheap. The UI labels say so (cost.js "By Model" `$/1k in|out (incl. cache cost)`;
the picker chip tooltip: "Effective $/1k … not a clean per-token list rate"). The label was made
honest by **relabeling, not re-mathing** — the number is a correct effective cost.

### §C — Rung suggestion derived from band (no provider literals)

`model_discovery.suggest_rung_placement` / `suggest_rung_structured` place a newly-discovered
model into a rung by **inverting the rung cost-ordering** (`_band_to_rung_map()` maps a resolved
cost band → the rung whose `costClass` it matches). The earlier per-provider `== "<provider>"`
branches are gone — placement follows cost, so a new provider needs no code edit.

### §D — Tier-picker safety (recommendations + per-bot scoping)

Two admin-UI guards protect operators from mis-assigning models (`ai-optimization.js`):

- **Recommended-model surfacing.** Each "Add a model…" dropdown stars the models recommended for
  *that* tier (`★ (suggested)`) and still lists an already-added recommended model as a disabled
  `★ (already added)` row, so the recommended choice is always visible. Suggestions come from the
  code-default cluster (`defaultModels` for a bot view; `roleDefaultModels[roleId]` for the pod
  view) — the same product-default source, never a hand-maintained UI list (`[[product-defaults-in-code]]`).
- **Per-bot credential scoping.** `model_catalog.scope_credentialed_to_bot()` filters the picker to
  the models a given bot can actually authenticate against, **failing open to the pod union** if the
  per-bot credential set can't be resolved (never shows an empty picker).

### §E — Easy-setup preview renders judge's effective non-standard chain

The easy-setup preview ("Resulting tiers — preview before writing") renders the **Judge** row as
its *effective* non-standard fallback chain — mirroring the runtime not-standard resolution
(`_resolve_judge_availability` / `ModelRouter._resolveJudgeModel`: skip standard's resolved
provider, lead with the first non-standard model) — even though judge **shares standard's rung**
in the written config (`{rung, provider:"not-standard"}`). Without this the row printed
byte-identical to Standard and implied judge would use standard's provider. The transform is
**display-only** (`_aiEasyPreviewRows`): standard's provider is derived from standard's own rung
chain, the standard-provider model(s) are de-emphasized/trailed under a "skipped" marker, and a
"Provider diversity — Judge avoids Standard's provider" caption is shown. The written rung is
unchanged (reordering the shared rung would corrupt Standard, which must keep leading with the
top-preferred provider) — this supersedes the original Addendum 6 item #14 intent of independently
reordering judge's models, which is impossible while judge shares standard's rung.

### Deploy note

These changes are config-shape-compatible with Addendum 8; the only migration is the Phase 11a
`migrate-model-roles --apply` (canonical rung ids + `*-default`→canonical + buggy-seed
`gpt-5.5`→`gpt-4.1`), already run on-pod. Deploy is **canary-gated** — see the registry deploy
column.

---

## Addendum 10 — credential-awareness in the default→bot flow (Phase 12, 2026-06-12)

The default catalog ships every supported provider per tier (Addendum 9 §A: anthropic,
openai, google, xai, deepseek, mistral). A given bot is credentialed for only some of
them. So a default that flows to a bot carries fallback entries the bot cannot
authenticate. **Most of the system already handles this correctly** (audit 2026-06-12) —
this addendum closes the three surfaces that don't, and writes down the governing
principle so it isn't re-litigated.

### The principle (settled)

1. **The pod default stays full — never credential-filter the template.** Different bots
   have different keys; filtering is a *per-bot boundary* operation, applied where a
   bot's effective config is computed, never to the pod-level default.
2. **Inherit vs Custom decides whether to strip.**
   - A **"Use pod defaults"** bot inherits the full chain and relies on **runtime
     degradation** (`resolve_role_with_availability` / `ModelRouter.resolveRoleAvailability`
     already skip uncredentialed providers). This is a *feature*: add a provider key later
     and the model activates with **zero config change**. Do **not** fork a filtered copy.
   - A **Custom** bot owns its tier list, so its fork/edit/easy-setup writes **strip to the
     bot's credentialed providers** (already true for the tier editor #2784 and
     `materialize_bot_tier_override`).
3. **Never silently drop.** A bot's existing inert non-cred fallback is surfaced as a
   *visible, reversible* row ("dormant — add creds to activate / remove"), not deleted out
   from under the operator. (Operator decision 2026-06-12.)
4. **Routing is already safe.** A non-cred entry in a chain is inert (never selected); a
   chain with *no* credentialed model resolves to `None` with reason `uncredentialed` — no
   silent bad pull. Credential-awareness is about **legible advisories + clean writes**,
   not a routing fix.
5. **The copy-creds affordance is the single shared resolution.** Every surface that hits
   a non-cred provider routes to `POST /api/admin/config/<bot>/credentials/borrow/<provider>`
   ("Copy \<Provider\> from \<bot\>") rather than inventing its own remedy.

Already credential-aware (do **not** rebuild): runtime resolvers; the per-bot tier
editor/picker; the Custom-fork seed; the **reconcile-catalog WRITE** (it skips
uncredentialed providers); the freshness, catalog-coverage, provider-diversity, and
model_discovery generators; the copy-creds route.

### §A — Gap 1: bot-scoped easy-setup bypasses the per-bot filter

`compute_easy_setup_catalog` / `easy_setup_catalog_for` accept `credentialed_providers`
and honor it, and the route passes `_pod_credentialed_providers(net)` for `scope="pod"` —
but for `scope="<bot_id>"` it computes **without** the target bot's creds, so the wizard
can populate a Custom bot with models it has no key for. (This is the most likely origin
of stale entries like `xai/grok-4` in a non-xAI bot's tier.) **Fix:** when `scope` is a
bot id, resolve that bot's credentialed providers (`_bot_providers_with_keys`) and pass
them through. Pod scope stays full.

### §B — Gap 2: catalog-drift (Type-1) is not credential-aware

The "models referenced but not registered" detector (`find_catalog_drift`, Type-1)
flags *every* tier model missing from the catalog regardless of credential status, so a
bot with no xAI key shows `xai/grok-4 in Judge — OC drops it` with a **"Reconcile
catalog" button that correctly refuses to add it** → an un-actionable row that never
clears. **Fix:** split Type-1 findings by whether the bot is credentialed for the model's
provider:
- **credentialed but un-cataloged** → keep the current "Reconcile catalog" path (the
  provider IS credentialed, the model just not yet whitelisted).
- **uncredentialed provider** → reframe the row to a **missing-credentials** advisory:
  "\<bot\> isn't credentialed for \<provider\> — [Copy \<provider\> from \<bot\>] or
  [remove from tier]", routing to the copy-creds affordance (§5). The reconcile WRITE is
  already safe; this corrects the misleading *display* and gives the row a real remedy.

### §C — Gap 3: hard-break vs inert-fallback severity

Today an inert deep-chain fallback and a genuinely broken tier read identically. **Fix:**
add a severity distinction to the drift/coverage advisories:
- **Hard break** — a tier whose entire chain is uncredentialed (no working model; e.g.
  judge needs a non-Standard provider but the bot only has Standard's). Prominent; this is
  a real routing failure (`reason: uncredentialed`).
- **Soft / dormant** — a tier that resolves fine but carries non-cred fallbacks. Quiet;
  rendered as the reversible "dormant — add creds to activate / remove" row from §3, never
  auto-stripped. Keeps attention on real breaks and preserves auto-activation.

`migrate-model-roles` stays **lossless** (a structural rename must not silently delete
operator intent); any non-cred residue it preserves is caught by §B/§C, so no migration
change is needed.

### Phasing (build in bites)

- **Phase 12a — bot-scoped easy-setup filter (§A).** Small: pass the target bot's
  credentialed providers on `scope="<bot_id>"`. Acceptance: bot-scoped easy-setup never
  writes a model from a provider the bot lacks a key for; pod scope unchanged.
- **Phase 12b — credential-aware catalog drift (§B).** Type-1 drift split into
  cataloged-gap vs missing-creds; the missing-creds row routes to copy-creds/remove.
  Acceptance: a non-cred tier member shows the copy-or-remove remedy, not a dead
  "Reconcile" button; credentialed-but-uncataloged rows keep reconcile.
- **Phase 12c — severity split + dormant surfacing (§C). SHIPPED PR #2836.** Hard-break vs soft-dormant on
  the advisories; dormant fallbacks shown as reversible rows. Acceptance: a tier with a
  working model + inert non-cred fallback is quiet (dormant row); a fully-uncredentialed
  tier is a prominent hard-break. As-built: `resolve_roles_with_provenance` adds
  `inertProviders: list[str]` per role (non-credentialed providers in the rung; `[]` when
  credentials unknown — fail-open). JS `_aiRoleCredSeverityRows(rv)` helper reads it:
  hard-break path fires on `!available && uncredentialed` (amber card, prominent); dormant
  path fires on `available && inert.length > 0` (muted bg3/text3 card, `_aiProviderLabel`
  for names, auto-activates message). Both modes (`_aiRenderTiersUseDefaults` +
  `_aiRenderTiersCustom`) call the shared helper; generic `unavailRow` excludes
  `'uncredentialed'` to avoid double-rendering.

Deploy is **canary-gated** like the rest of the aspect (admin/analyzer-side; no routing
change → no gateway kickstart required).

---

## Addendum 11 — Model Economics page (Phase 13, 2026-06-13)

Pointer addendum: Phase 13 has its own design doc,
[`docs/spec-model-economics-page-2026-06-13.md`](spec-model-economics-page-2026-06-13.md),
because it is a **synthesis/presentation** phase off this arc rather than a change to the
rungs/roles core. A model-CENTRIC cost lens ("what does each *model* cost across the whole
pod, and how do models compare across tiers/providers") transposing the bot-centric Usage/
Cost page; pooled pod-wide, with volume + confidence.

**v1 (SHIPPED — PR #2868, live stable `9d84412a3` / version 2026.0613.2868):** a sortable
model-economics leaderboard off AI Optimization — **$/turn** headline (effective $/1k
incl-cache secondary per the #2788/#2797 contract), list $/1k, eff-vs-list delta, spend,
share, turns, confidence badge, human%; group/filter by tier + provider; configured-but-
**unused** models shown distinctly. New `packages/analyzer/model_economics.py`
(`assemble_model_economics`) + additive `bot_count`/`last_used_ts` on
`usage_analytics.by_model` (Cost page unbroken) + `/api/analytics/model-economics` +
SPA `static/js/pages/model-economics.js`. **Identity is the gateway-reported model that
ran** (sidesteps [[bot-cannot-observe-own-routing]]); almost all data pre-existed — only
new compute is per-model bot-count + recency.

**Deferred:** v2 = perf-from-spans (latency/success/error/struggle per model, behind an
EACCES coverage gate + a new cascade-span reader); v1.1(opt) = `(session×model)` $/session.
Deploy is admin-only, canary-gated; **no `migrate-model-roles`** (read-only new page, no
config-shape change).

---

## Addendum 12 (2026-06-15): model discovery surfaces on the AI Optimization page, not as a proposal

Operator-confirmed (2026-06-15). Today `model_discovery` **double-surfaces** every newly-
discovered model: it emits a `model_discovery` **Signal** AND an `AdoptModel` **Proposal**
into the Recommendations queue. The proposal is a literal duplicate of data that already
renders (now, with adopt controls) on the AI Optimization page's **Model Freshness** card,
and it taxes the operator's attention on a cadence set by *providers'* release schedules.
This addendum removes the proposal. It **removes surface area** and is reversible.

### A. Altitude rationale — discovery does not earn a proposal slot

The proposal apparatus (card → expand to a busy page → approve / reject / snooze / dismiss
→ 7-day check-in) is for *"I changed your config and claim it helped"* — a deliberate RSI
edit whose per-event decision value justifies the ceremony. A newly-discovered model is a
different altitude: **tactical (L0), recurring, near-zero per-event decision value** — "a
model exists; consider adopting it," nothing the bot is actively running off of broke.
xAI/etc. ship models constantly; proposalizing each one is a standing tax on attention with
no commensurate per-event judgment to make. Discovery is an **advisory**, and an advisory's
home is a page you visit, not a queue that interrupts you. (This is the presentation×altitude
contract from [[project_rsi_proposal_altitude_2026_06_12]]: a flat, high-ceremony surface for
a low-altitude recurring event is wasted apparatus.)

### B. The new contract — Signal only, adoption from the card

1. **Kill the proposal.** `model_discovery.observe()` returns `[]` for ALL discoveries (a
   tactical new model and a hypothetical new frontier line alike). `observe_signals()` is
   unchanged — the `model_discovery` Signal (recency/modality-gated at emission, deduped by
   signature) is the **single output** and the data source for everything below.
2. **Surface on the AI Optimization Model Freshness card.** The card reads the **gated
   firing Signals** (`GET /api/models/discoveries`) — no trigger, no wait — and renders each
   discovered model with **per-model one-click adopt** (role-map dropdown defaulting to
   `none`/dormant + cap input for `max`/`power`), **ignore**, and **"Adopt all as dormant."**
   "Check Now" stays as the manual staleness refresh, not the primary path.
3. **No autonomous catalog mutation** (preserves §Non-goals #2 + the charter's "Discovery
   NEVER edits the catalog or rungs itself" invariant). The card is display-only until the
   operator clicks; nothing is auto-adopted. The thing the operator disliked was the
   *scan-and-wait* — and the scan already ran on the daily schedule, so pre-display (a cache/
   Signal read) solves it with zero auto-mutation.
4. **Neutral nav badge.** A neutral (accent, NOT red — a new model is opportunity, not
   breakage) count badge on the AI Optimization nav item shows un-acknowledged discoveries;
   it clears on page visit and individual models leave the count as they're adopted/ignored.

### C. What this supersedes (and what is retained)

- Supersedes the **"Propose, never auto-categorize"** step (§"Freshness check rework" #3) and
  **Addendum A.1's "`AdoptModel` action replaces Investigation on `model_discovery`
  proposals"** *as the surfacing mechanism*: discovery no longer emits a queued proposal.
- **Retained:** the `AdoptModel` **applier** (rung create/extend, role re-point, judge
  provider-diversity validation, cap seed, normalized `network.json` write — Addendum A.3)
  and the canonical `AdoptModel` builder. **Only the front door moved** — from the proposal
  queue to the page. The card's adopt route (`POST /api/models/adopt-discovery`, helper
  `evolve_admin.web.model_discovery_adopt`) builds the `AdoptModel` *action* from the firing
  Signal's details and drives the SAME applier; adopting/ignoring resolves/dismisses the
  Signal so the card + badge clear immediately. The `AdoptModel` action kind stays in the
  charter `action_kind_allowed` allowlist (the builder still constructs it).
- **Migration of in-flight proposals:** automatic. With `observe()` returning `[]` and the
  charter's `resolves_when_silent: true`, the generator-runner's `sweep_resolve_proposals`
  archives every pending `AdoptModel` proposal as `resolved_externally` on the next run
  (pod-wide bucket, no fingerprint re-emitted) — no one-shot script. Proven by
  `test_runner_signal_only_sweeps_preexisting_adopt_proposal`.

Deploy is **canary-gated** (admin + analyzer side; no routing change → no gateway kickstart).
The charter prose change requires the standard post-promote fingerprint sync
(`sudo evolve-admin migrate-generator-records --apply`); no config-shape / rung-id change, so
**no `migrate-model-roles`.**

## Addendum 13 (2026-06-16): capability-aware fit engine — recommend a role or name the off-ladder kind

Operator-confirmed (2026-06-16). Addendum 12 moved adoption to the AI Optimization card; this
addendum makes what the card *recommends* trustworthy. Discovery today recommends adopting a
model into a tier (rung) that maps to **no role**. Routing only ever reads roles, so a no-role
("dormant") adoption is a catalog entry no bot can ever call — **cruft by construction.** Three
concrete failures motivated this:

1. **The `new-rung` literal is a near-bug.** When the price-band engine couldn't place a model,
   `suggest_rung_structured` returned `("new-rung", "medium")`, and the `AdoptModel` applier's
   `_splice_rung` then minted a **permanent rung whose id is literally `new-rung`** that no role
   points at. A second unplaceable model appended into that same dead cluster. A placeholder
   slug must never become a persisted rung id.
2. The engine **discarded its own good analysis** — a priced model that *does* band-place (e.g.
   $2/MTok → medium → sonnet-class) was still surfaced for a no-role adoption.
3. Analysis was **price-band only**, so it gave up on most models that lack a public price (3 of
   4 live xAI grok models).

**The core idea (why this isn't just "better analysis").** Rungs back a SINGLE cost-ordered
general-purpose ladder (the roles fast/standard/power/max) — the only model abstraction routing
consumes. So a model that "fits no rung" usually isn't an analysis failure; it answers a
question routing doesn't pose. There are **five kinds of discovered model and only one is rung
material**, and the engine's job is to say WHICH KIND each discovery is — in operator language —
never to force an off-ladder model onto the cost axis (that's the `new-rung` junk-drawer bug).
The 2026-06-16 build replaced the initial three-verdict framing with the full five-kind
taxonomy (`mode_variant` + `specialist` added) so the card can *name the off-ladder kind*
instead of mislabeling it a tier candidate.

### A. Every firing Signal carries a placement verdict

Computed at observe time (the daily sweep already runs the analysis; the card reads the Signal
with zero re-run — the same no-scan-wait contract as Addendum 12). Threaded through the
`DiscoveryFinding` dataclass + `to_dict` and written into the Signal `details`.

**The taxonomy — five kinds, one is rung material:**

| Verdict | Kind | Rung material? | `recommended_role` / `recommended_rung_slug` |
|---|---|---|---|
| `fits_existing` | A general-purpose model whose cost/capability matches a role the pod already runs | **Yes** | the role / its existing rung |
| `new_tier` | A general-purpose model genuinely pricier/larger than anything in the catalog (the Fable-catch) — extends the ladder | **Yes** (explicit op action) | `null` / a band-derived PROPOSED slug |
| `mode_variant` | A reasoning / non-reasoning / thinking MODE of a model that already maps to a tier — same model, a compute knob turned | No — named, not a tier | `null` / `null` |
| `specialist` | A domain / workload specialist (coding, multi-agent, creative, math) — off the general ladder | No — named + tracked, not routed | `null` / `null` |
| `cannot_place` | A genuine gap: no price, unknown family, LLM abstained — the only true "can't tell" | No | `null` / `null` |

The `specialist` bucket's accumulation is the **future demand signal for the deferred second
capability axis** (§Non-goals #3 "Multi-axis capability clusters") — this addendum deliberately
does NOT build that axis; it only *names* the specialists so the need can be observed.

The Signal-detail fields:

- `placement_verdict` — one of `fits_existing` | `new_tier` | `mode_variant` | `specialist` |
  `cannot_place`.
- `recommended_role` — `fast`|`standard`|`power`|`max`|`judge` ONLY for `fits_existing`; `null`
  for the other four.
- `recommended_rung_slug` — the existing rung for `fits_existing`; a meaningfully-derived
  PROPOSED slug for `new_tier`; `null` for `mode_variant` / `specialist` / `cannot_place`.
  **Never the literal `new-rung`.**
- `fit_reason` — one plain-language, operator-facing sentence (tiers + roles in plain words;
  never `rung`/`band`/`dormant`/`new-rung`/position numbers — Bite 2 owns card copy, this owns
  the reason string the Signal carries).
- `fit_confidence` — float 0..1.
- `fit_evidence` — the cited signals behind the verdict (band, price, context window, max
  output, capability flags, and the generic mode/workload name tokens); keeps the existing
  `cost_band_source` / `cost_band_evidence`.
- `fit_source` — `llm` | `deterministic` (which layer produced the verdict).

### B. Engine layering (degrades cleanly)

1. **Deterministic first (no LLM)** — `compute_placement_verdict` combines the existing price
   band (`model_cost_bands.resolve_band` → `rung_for_band`) with listing metadata previously
   collected but ignored (`context_window`, `max_output_tokens`, capability flags) and
   provider-NEUTRAL name tokens of two sorts, both generic English (the same three-homes posture
   as the `_band_from_size_naming` size tokens): **MODE tokens** (`reasoning` / `non-reasoning` /
   `thinking`) and **WORKLOAD tokens** (`code` / `coder` / `build` / `multi-agent` / `creative` /
   `math`). The decision order says WHICH KIND before the cost axis is consulted:
   a **mode token PLUS a base family the pod already runs** (the id with the mode token removed,
   matched against `known_family_stems`) → `mode_variant`; a **workload token** → `specialist`
   (a SOFT hint — the LLM may pull a genuinely general model back to `fits_existing`); a **known
   band that maps to a role-bearing tier** → `fits_existing` (authoritative when price-derived;
   softer for the family-map / naming fallbacks); a **known band with no role-bearing tier** →
   `new_tier` with a band-derived proposed slug; a **size-name hint** → soft `fits_existing`;
   nothing → `cannot_place`. The tokens are soft signal, refined by the LLM; they never decide a
   cost tier.
2. **One cheap LLM fit call** per newly-discovered model (`generators.model_discovery.fit_llm`)
   takes the deterministic signals + model id and returns `{verdict, recommended_role,
   confidence, reason}` as JSON — `verdict` is the **five-value enum**. The model to call is
   resolved from the pod's **`fast` (else `judge`) role** via the catalog roles map — **never a
   hardcoded model id** (the no-provider-literals rule). The prompt makes the
   **mode-vs-specialist-vs-gap distinction explicit** so the LLM separates "a reasoning MODE of a
   general model" (`mode_variant`) from "a domain SPECIALIST" (`specialist`) from "genuinely
   can't tell" (`cannot_place`). It SHARPENS the verdict for the cases the deterministic layer is
   unsure about (the unpriced grok models) and authors the operator-facing reason.
3. **Fail-open, always.** No `fast`/`judge` role resolves, no key, SDK missing, call error, or
   low-confidence / invalid output → fall back to the deterministic verdict (`fit_source =
   "deterministic"`): band-placeable → `fits_existing`, recognized mode/workload token →
   `mode_variant` / `specialist`, else `cannot_place` — never a fabricated tier. The LLM is never
   a hard dependency and **never mutates the catalog** — adoption stays a deterministic,
   operator-clicked applier write. Injection-safe: the model id is untrusted data, the response
   is JSON-only with a strict fallback, `recommended_role` is re-validated against the known role
   set, and the **slug is always computed deterministically** (role→rung via the catalog, or a
   band-derived proposed slug; `mode_variant`/`specialist`/`cannot_place` carry no slug) so a
   prompt-injected string can never become a persisted rung id. A real price-derived
   `fits_existing` is authoritative — the LLM cannot override ground-truth cost.

### C. Kill the `new-rung` literal at the source

- `suggest_rung_structured` (and the `DiscoveryFinding.suggested_rung_slug` default) stop using
  `"new-rung"` as a dunno-bucket. Unplaceable → an **empty** slug (no rung suggested);
  genuinely-new-cluster → `new_tier` with a meaningfully-derived slug + a cited reason. The
  canonical placeholder set lives in `model_discovery.PLACEHOLDER_RUNG_SLUGS`.
- The applier footgun is closed: `AdoptModelApplier.apply` **rejects** a placeholder / empty /
  `new-rung` slug cleanly (flag, no write) rather than minting a dead rung. The applier is
  otherwise intact — Bite 2 / the operator drives real role mappings through it.

### D. What this amends

This consciously amends the **"Propose, never auto-categorize"** step (§"Freshness check
rework" #3) and Addendum 12's dormant-by-default framing: discovery still *proposes* (it never
auto-edits the catalog), but it no longer proposes a **placement that routes to nothing**. A
recommendation that maps to no role is not a neutral default — it is guaranteed cruft, so the
engine now recommends a role with a reason, or honestly says it `cannot_place` the model and
asks the operator to choose. Adoption remains 100% operator-gated.

### Build order

- **Bite 1 — backend.** The five-verdict fit engine, the verdict on every Signal, the LLM seam,
  the `new-rung` kill + applier guard. No SPA change. (Landed in two passes: an initial
  three-verdict cut, then the `mode_variant` + `specialist` extension that completes the
  five-kind taxonomy — both are "Bite 1, backend".)
- **Bite 2 (separate) — card UI + vocabulary.** The AI Optimization Model Freshness card reads
  the verdict fields and renders the recommended role + reason — or, for `mode_variant` /
  `specialist` / `cannot_place`, *names the off-ladder kind* with no Adopt-into-a-tier control;
  the operator-facing copy lives there.

Deploy is **canary-gated** (admin + analyzer side; no routing change → no gateway kickstart).
**No charter / fingerprint change** (the generator's prose and `subscribes_to` are untouched —
only the Signal `details` payload grew), so **no `migrate-generator-records`** and **no
`migrate-model-roles`** (no config-shape / rung-id change).

## Addendum 14 (2026-06-25): Model Freshness adopt-card — best-per-rung, role labels, blue Google

Operator-confirmed rework of the AI Optimization "Model Freshness" adopt card, building on
Addendum 13's verdict engine. The card had been dumping **every** unadopted model a provider
lists (22 rows in the reported screenshot), because it treated "model the provider lists" as the
unit. The unit is now **"best available model for a rung I actually use."** Four changes — all
read from the verdict fields Addendum 13 already puts on the Signal; **no new generator/charter
change**:

1. **Surface only cleanly-placeable models.** The adopt list is filtered to
   `placement_verdict == fits_existing`. `mode_variant` / `specialist` / `cannot_place` are
   unroutable, so they stay signal-only (no adopt-into-a-tier control) — this completes
   Addendum 13's "names the off-ladder kind" intent by simply *not surfacing* the off-ladder
   kinds on the adopt list. `new_tier` is split into its own **"create a rung?"** section
   (`/api/models/discoveries` now returns `discoveries` + `new_tiers`), where the action is
   creating the rung from the model's proposed slug (role assigned afterward from Settings →
   Models), never slotting it into an existing one.
2. **Best-per-(provider, role) collapse.** Among `fits_existing` findings, group by
   `(provider, recommended_role)` and keep only the single best — **newest generation**
   (lexicographic version-tuple, then dated snapshot), then capability (context window, max
   output). The three `gemini-*-flash-lite → fast` rows collapse to one. The ranking is a
   documented, **provider-neutral** helper (`model_discovery.model_generation_rank` /
   `select_best_per_rung`) — it reads only generic version/date tokens + listing capability,
   never branches on provider/model identity (the `no-provider-literals-in-logic` invariant) —
   with unit tests covering the collapse. Done server-side so the nav-badge count matches the
   card.
3. **Role labels, never slugs.** The card renders the canonical role label (Fast / Standard /
   Power / Max [/ Judge]) via `TIER_DISPLAY`, never `suggested_rung_slug` ("sonnet-class").
4. **Segmented role picker, not a dropdown.** The "Map role" `<select>` is replaced by a
   segmented button group reusing the chat-bar model-tier primitive
   (`.home-tier-buttons` / `.home-tier-btn` / `.active`), pre-selected to `recommended_role`;
   the per-row Adopt + Ignore and the max/power cap input stay. Judge is offered only when the
   pod catalog defines a judge rung (`picker_roles`). The "Adopt all as dormant" button and the
   dormant-default framing are removed (a dormant rung no role points at is the cruft
   Addendum 13 already rejects).

Presentation: `--ai-provider-google` is repointed from yellow to brand **blue** (dark
`#4285F4`, light `#1A73E8`) — yellow was indistinguishable from Anthropic orange at chip size;
the cyan overflow hue (`--ai-provider-alt-3`) is nudged to stay distinct from the new blue.
Reuses the existing `--ai-provider-*` token system (no new primitive).

Deploy is **canary-gated** (admin + analyzer; no routing change → no gateway kickstart). **No
charter / fingerprint change** (Signal `details` already carried the verdict fields), so no
`migrate-generator-records` / `migrate-model-roles`.

---

## Addendum 15 (2026-06-30): version freshness is the PRIMARY function — "update to latest"

**Operator-stated reframe.** The entire point of the "Model Freshness" / "Check Now" surface is:
an operator chooses a model **class** (opus / sonnet / haiku) and then wants to ride the **latest
version** of that class. "Fresh" = the latest version number of the models you already chose.
The button's #1 job is to bring every model the pod runs up to its latest version, one click —
**the everyday case**. Switching between model *classes* (adopting a new line) is the occasional
case. Until this addendum the priorities were inverted: the card surfaced only brand-new model
*lines* (the Fable-class discovery path, Addenda 12–14) and **never** recommended Sonnet 4-5 →
Sonnet 5, Opus 4-7 → 4-8, Haiku 4-5 → 5 — its primary job did nothing.

### Root cause (the dead-ended staleness channel)

`model_discovery.diff_listing` sorted each listed model into exactly ONE channel: **discovery**
(a model whose family the pod has no member of → `model_discovery` Signal → card → adopt; LIVE)
or **staleness** (a newer member of an already-adopted family — the version upgrade — recorded as
a `StalenessFinding` and then **dead-ended: zero consumers repo-wide**). The frontier filter
dropped a newer same-family member before it could surface, and the generator's `observe_signals`
looped `discoveries` only. The version upgrade was designed (Addendum-era "staleness, soft
upgrade") and never wired to a surface.

### The design (version freshness, first-class)

1. **Core deterministic pass.** `model_discovery.compute_version_upgrades(listing_by_provider,
   known_locations)` — for each located model find the **numerically-newest** same-(provider,
   family) chat-capable listed member (`model_generation_rank` / `_version_tuple` — never a
   lexicographic compare) and emit a `VersionUpgrade {provider, family, current_model,
   latest_model, rung_slug, roles, evidence}` when it strictly beats what the pod runs. This is
   the `StalenessFinding` promoted to a consumed, location-bearing result. Deduped to one upgrade
   per (provider, family, rung). The location map comes from `known_model_locations` (walks the
   merged catalog `DEFAULT ← pod ← bot` rung clusters directly, so the rung/role of every model —
   **including dormant entries** in a rung no role points at — is known); the upgrade pass runs
   against `pod_sourced_model_locations` — that map **restricted to models a pod source actually
   configured** (`network.json` / per-bot `evolve-tiers.json` / bot_configs), so a pure
   code-default seed (e.g. an `openai` rung-member on an Anthropic-only pod) never nags. The
   merged catalog still supplies the location; we just don't surface upgrades for the
   default-only rows.
2. **Surface — version upgrades LEAD the card.** The Model Freshness card's PRIMARY section is
   "*N* models have a newer version available", one Update row per stale class ("Sonnet 5 is
   available — you're on `claude-sonnet-4-5` · Standard role"), with a prominent **"Update all to
   latest"** bulk action. New-line discovery (`discoveries` / `new_tiers`) demotes below.
   `/api/models/discoveries` now returns `upgrades` (computed off the listings cache — no
   enumeration on page load); `/api/models/apply-upgrade` and `/api/models/apply-all-upgrades`
   drive the apply.
3. **Apply — reuse the swap.** An upgrade splices `latest_model` into the rung its predecessor
   occupies via the existing `AdoptModelApplier` (idempotent), passing
   `insert_before=current_model` so the new version lands **ahead of** the predecessor in the
   `models[]` cluster. This placement is load-bearing: the resolver routes to the **first
   credentialed** member of `models[]`, *not* the newest (`primary_bot.resolve_role_with_availability`
   / `ModelRouter.resolveRoleAvailability` both pick `models[0]`), so a plain append would leave the
   newer version as a fallback and routing would never move — a silent no-op. With the predecessor
   demoted to the immediate fallback slot, the role re-point is a genuine **no-op**
   (`role_mapping="none"`) and the predecessor stays as a harmless graceful fallback; retiring it is
   optional later cleanup, out of scope. In a multi-provider rung the splice preserves the other
   providers' relative order (the new version only displaces its own predecessor). The apply path
   re-derives `rung_slug` from the recomputed upgrade, never trusting it from the client.
4. **Latent bug folded in.** All "newest in family" selection (`diff_listing`'s `family_latest`
   index, `build_listings_cache`'s `is_family_latest` flag) switched from `model_id.lower() > …`
   (lexicographic — sorts `claude-sonnet-10` BELOW `claude-sonnet-4-5`) to the numeric
   `model_generation_rank`.

**Scope.** The button operates on the LIVE/runtime config (`network.json` rungs/tiers + per-bot
`evolve-tiers.json`) — what bots actually route on. The code-shipped `DEFAULT_MODEL_CATALOG`
version numbers are a separate seed; if they lag that is a maintenance code-PR, not this button's
job (this surface never runtime-writes code). Provider-literal-free throughout
(`no-provider-literals-in-logic`): family → rung/role from catalog DATA, version compare from
generic numeric tokens.

**Deploy is canary-gated** (admin + analyzer; no routing change → no gateway kickstart beyond the
usual admin-ui / evo-gateway kickstart on promote). **No rung-id migration** → no
`migrate-model-roles`. The charter description was updated (version freshness is now first-class,
"staleness signals unchanged" was false), which **does** change the charter fingerprint — run
`sudo -u evolve python3 tools/bump_charter_fingerprints.py --shared-dir <dir> --apply` on each pod
after the code lands so the registry keeps loading `model_discovery` (no `migrate-generator-records`
needed; the bump preserves track_record/config/state). No new signal type and no new producer.
