# Evolve — Model Rungs & Roles

> **Design spec:** `internal/spec-model-rungs-and-roles-2026-06-09.md`.
> This is the operator-facing guide; the spec is the rationale of record.

Evolve routes by **role**, never by model name. A role (`fast`, `standard`,
`power`, `max`) is a stable job description; it resolves to a **rung**
(a cross-provider cluster of interchangeable models) at runtime. Code and
config reference roles and rungs; the concrete model string lives in exactly
one place — the rung's cluster. Configured in `network.json::models` and
managed via `evolve-admin models`.

> **Formerly "tiers."** This system replaced the four-tier (`tier0`–`tier3`)
> naming on 2026-06-09. See the [formerly-tierN mapping](#formerly-tiern-mapping)
> at the bottom and run `evolve-admin migrate-model-roles` once to convert an
> old config in place.

---

## Rungs and roles

### Rungs — the model catalog

A rung is a named cross-provider cluster. Rungs live as an **ordered array** in
`network.json::models.rungs`; array position is rank, cheapest first, most
powerful last. Each rung has:

- **`id`** — a stable slug named for its Anthropic anchor class
  (`haiku-class`, `sonnet-class`, `opus-class`, `fable-class`). The slug is an
  identifier, not a promise: if the anchor model retires, the slug can stay.
- **`models`** — the cluster: primary first, fallbacks after. This is where the
  old per-tier fallback chains live now, unchanged.
- **`costClass`** — `low | medium | high | premium`. `premium` is new (Fable-class)
  and feeds cost reporting exactly as the other classes do.

Rungs are added **when the pod adopts a model**, not speculatively. A rung earns
its row when a role points at it or it sits in a fallback chain. New rungs
arrive through discovery proposals you accept (see
[Model freshness](#model-freshness--discovery-not-list-matching)), not by
hand-editing a recommendation table.

### Roles — the job descriptions

A role is a map entry in `network.json::models.roles`: role ID → rung slug,
uniformly. One namespace, used everywhere — tool schemas, routing config,
annotations, admin-UI values.

| Role | Default rung | Purpose |
|---|---|---|
| `fast` | `haiku-class` | Background, maintenance, internal analysis. Users never see its output directly. |
| `standard` | `sonnet-class` | Default for productive, user-facing sessions. Most turns. |
| `power` | `opus-class` | High-complexity work; user-requested or cascade-escalated. Daily-capped. |
| `max` | `fable-class` | The frontier model. **Pull-only** (see below). Daily-capped, lower default cap. |

"Grunt", "Workhorse", "Power", and "Max" survive only as **display labels** in
the admin UI. The old user-choice enum (`fast | standard | power`) and the old
internal tier names collapse into this single role namespace — there is no
more tierN↔name↔choice triple-translation.

### The former `judge` role — collapsed into a derivation (2026-08)

There used to be a fifth, structured role — `judge`, constrained to a provider
other than `standard`'s — for cross-model evaluation. It was removed on
2026-08-31: no call site resolved it, and the property it existed to buy is
now **derived at the call site** from config the pod already has. See
[cross-vendor checking](#cross-vendor-checking-goodharts-law) below. Old
configs that still carry `roles.judge` or a `judge-class` rung keep loading
(the entries are inert); `sudo evolve-admin migrate-model-roles` folds the
judge rung's models into the standard chain so nothing you curated is lost.

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
    "max":      "fable-class"
  },
  "routing": {
    "enabled": true,
    "maintenanceRole": "fast",
    "backgroundRole": "fast",
    "ambiguousRole": null
  }
}
```

Fable note for code that constructs requests: Fable's API surface matches Opus
4.7/4.8, **except** an explicit `thinking: {type: "disabled"}` returns 400 —
omit the param entirely instead of disabling it.

---

## The `max` role — pull-only

The defining property of `max`: **no automatic path routes to it.** A model at
roughly 2× Opus pricing must never be reachable by a silent escalation — that is
the lesson of the 2026-05-20 cost blackout, encoded as a rule.

1. **Reachable only by explicit choice** — the admin-UI tier picker, the
   `session_set_tier` tool forwarding an explicit user request, or a per-user
   default set via `evo tier-default max`.
2. **Excluded from the cascade.** The cascade controller may escalate up to
   `power`, never to `max`.
3. **Excluded from classifier routing.** `maintenanceRole` / `backgroundRole` /
   `ambiguousRole` and per-bot `defaultRole` validate against
   `{fast, standard, power}`. Configuring `max` as any default is a **config
   error** the loader rejects.
4. **Bot-initiated escalation to `max` is blocked by default.**
   `userTierOverride.allowBotInitiated` is per-role:
   `allowBotInitiated: { power: <legacy value>, max: false }`. A bot may forward
   a user's explicit ask but may not unilaterally pin Fable.
5. **Safety nets still win.** Spend-cap and runaway-rate downgrades force `fast`
   regardless of any choice (precedence ladder unchanged, below). The per-bot
   daily cost cap (PR #1483 auto-trip) is the blast-radius backstop for a user
   who pins `max` and walks away.

### Per-role daily caps

The old tier1-limit machinery generalized to per-role caps:

```json
"roleCaps": {
  "power": { "maxPerDayPerBot": 10 },
  "max":   { "maxPerDayPerBot": 5 }
}
```

On a cap hit, `max` degrades to `power` (and `power`'s own cap may then degrade
to `standard`), surfaced honestly to the user as **"Max capped today — used
Power"** (the same visible degradation the power cap already had).

**Caps are disk-backed and survive gateway restarts.** The plugin seeds today's
power/max counters from the on-disk tier-usage log at boot and appends a record
on every transition into a capped role:

```
{sharedDir}/cost/tier-usage/{botId}/{YYYY-MM-DD}.jsonl
```

Each line carries the resolved `tier`/role (e.g. `"tier":"max"`), so a restart
mid-day doesn't reset the count and let a capped role slip through. The admin-UI
chip gate reads the same counter. If the plugin can't write this file it logs a
loud tier-usage warning — absence of the file under live load is a real fault,
not a quiet no-op.

### Routing precedence (highest wins)

Only the names changed from the tier era; `max` slots into the user-choice rungs:

1. Runaway-rate cap tripped → force `fast`
2. Spend-cap downgrade active → force `fast`
3. `/model` command (`ctx.userModelOverride`) → respect, no override
4. User role choice (chip / `session_set_tier` / per-user default):
   `fast | standard | power | max`
5. Cascade verdict (when enabled): may select `fast | standard | power`
6. Classifier: maintenance/background → `maintenanceRole` / `backgroundRole`
7. Operator per-bot default → bot default

---

## Enforcement

Role routing is enforced via OC's `before_model_resolve` hook, which fires
before every model call and can return `{ modelOverride }` to swap the model OC
would otherwise use. Installed on every bot via `evolve-admin deploy`.

### How it works

```
User sends message
  → before_model_resolve hook fires (TurnObserver.ts)
  → ModelRouter reads session classification from in-memory state
  → maintenance/background → return { modelOverride: <fast rung primary> }
  → productive/ambiguous   → return {} (use OC default = standard)
  → OC uses the resolved model for this run
```

`ModelRouter.ts` holds a `Map<sessionId, sessionType>` populated by the
classifier on each turn. The hook is registered in `TurnObserver.register()`:

```typescript
api.hooks.register("before_model_resolve", async (ctx) => {
  const sessionKey = ctx.sessionKey ?? ctx.session?.id;
  if (ctx.userModelOverride) return {};          // respect /model command
  const override = modelRouter.resolveModelOverride(sessionKey);
  return override ? { modelOverride: override } : {};
});
```

The hook **always fails open**: any error returns `{}` and lets OC use its
default.

### What gets overridden

| Session type | Action |
|---|---|
| `productive` | No override — use bot's configured default (`standard`) |
| `ambiguous` | No override — use bot's configured default (`standard`) |
| `maintenance` | Override to the `fast` rung's primary (Haiku) |
| `background` | Override to the `fast` rung's primary (Haiku) |
| Unknown / first turn | No override — classification not yet available |

### Respecting /model

If the user has explicitly set a model via `/model`, OC sets
`ctx.userModelOverride`. The hook checks this first and returns `{}` immediately,
leaving the user's choice intact.

---

## Session classification

Session labels (`productive` / `maintenance` / `ambiguous`) are distinct from
roles. The classifier labels a session by *what kind of work it is*; the router
maps that label to a role.

| Concept | Values | Meaning |
|---------|--------|---------|
| Session class | `productive`, `maintenance`, `ambiguous` | What kind of work this session is doing |
| Role | `fast`, `standard`, `power`, `max` | Which compute/cost class the model call uses |

**First pass — keyword matching (free, always).** Productive signals (research,
novel, design, plan, roadmap, calendar, travel…) vs. maintenance signals
(openclaw.json, gateway, restart, config error, permission denied, plist,
traceback, watchdog…). If one type clears 0.75 confidence, classification is
immediate.

**Second pass — `fast`-role LLM call (ambiguous cases only).** When keyword
confidence is ≤0.75, a single `fast`-role call classifies the first user message:
one word, PRODUCTIVE / MAINTENANCE / AMBIGUOUS.

**Background → fast.** Background and maintenance sessions resolve to the `fast`
rung (Haiku) deliberately. They are short, diagnostic, never user-facing, so
routing them to Haiku cuts cost and fallback risk without degrading the work.

**Annotation schema (schema_version 4):**

```json
{
  "session_class": "productive",
  "class_signals": ["novel", "inversion"],
  "class_confidence": 0.80,
  "model_role": "standard",
  "model_tier": "tier2",
  "model_selected": "anthropic/claude-sonnet-4-6"
}
```

`model_role` is the field of record. `model_tier` is retained as a transitional
alias so old readers and historical records keep working; readers accept both.

---

## Changing model assignments

The canonical workflow is **discovery proposes, you accept.** You no longer
hand-edit a model table to keep up with the market — the freshness check
enumerates each provider's live catalog and emits a Proposal when something new
appears (see below). Accepting that proposal is the operator action that edits
`models.rungs` / `models.roles`. The CLI below is the direct path when you
already know the change you want.

### Via CLI (recommended)

```bash
# View current role → rung assignments
evolve-admin models list

# Point a role at a different rung
evolve-admin models set standard sonnet-class --yes

# Point power at a different rung
evolve-admin models set power opus-class --yes

# Inspect one role (resolved model, fallbacks, cap)
evolve-admin models show power

# Today's usage grouped by role
evolve-admin models usage
evolve-admin models usage --bot <bot-id>
```

`models set <role> <rung>` warns if a re-point changes `costClass` by more
than one step. `--yes` skips the confirmation prompt for scripted/deploy use.

To change which **models** are in a rung (add a fallback, bump the primary),
edit the rung's `models` array in `network.json` (or accept a discovery
proposal). Roles point at rungs; rungs name the models.

### Via network.json (direct edit)

```json
"models": {
  "rungs": [
    { "id": "sonnet-class", "models": ["anthropic/claude-sonnet-5", "openai/gpt-4o"], "costClass": "medium" }
  ],
  "roles": { "standard": "sonnet-class" }
}
```

Changes take effect on the next script run for Python-side callers. The plugin
(TypeScript) reads `network.json` once at startup, so a **gateway restart**
(`evolve-admin deploy <bot>` or a kickstart) is required for the plugin-side
`ModelRouter` to pick up rung/role changes.

**Note:** `network.json` rung/role assignments drive Evolve's internal calls
(analyze, audit, forge, etc.) and the user-facing routing layer. The bot's own
OC default model in `openclaw.json` is separate — Evolve does not change it
autonomously.

### Legacy config — automatic synthesis + one-shot migration

A pod whose `network.json` still carries the old `models.tiers.tierN` shape keeps
working. The loader **fails open**: it synthesizes rungs and roles from the old
tiers (`tier3`→`fast`, `tier2`→`standard`, `tier1`→`power`; `tier0` folds into
the standard chain — the judge role is gone) and
logs a **deprecation warning** on every load. This is a transitional shim, slated
for removal one release cycle out — a synthesis warning in the gateway log is
expected on an un-migrated pod and is not an error.

Convert in place with the one-shot migration:

```bash
sudo evolve-admin migrate-model-roles
```

It rewrites `network.json`, each bot's `~/.openclaw/evolve-tiers.json`, and each
bot's `{sharedDir}/{botId}/user-tier-prefs.json` from tier keys to role IDs. It
is **idempotent** — safe to run repeatedly, and run as part of a deploy.

---

## Model freshness — discovery, not list-matching

The freshness check is **discovery-based**, built on the rungs/roles catalog.
This replaces the old advice to hand-edit a `RECOMMENDED` table to "keep models
current" — a circular check that reported "all current" the day Fable 5 shipped,
because "current" meant "matches our hand-edited list."

How it works now:

1. **Enumerate.** For each credentialed provider, fetch the live model listing
   (Anthropic `GET /v1/models`, OpenAI `GET /v1/models`, Google equivalent).
   Pure HTTP against keys the pod already holds — no LLM, no web search. Cheap
   enough for the daily generator sweep.
2. **Diff** the listing against the pod's rungs:
   - **Known** — model is in some rung's cluster: check *within-family*
     staleness (a newer version in the same family, per the listing).
   - **Unknown** — a chat-capable model in no rung and not on the ignore list:
     a **discovery finding**. This is the class that catches a whole new line
     like Fable.
   - **Out of scope** — embeddings / audio / specialty models and dated
     snapshot aliases of known models: auto-ignored.
3. **Propose, never auto-categorize.** Rung placement is a judgment call and
   provider listings carry no pricing, so discovery emits a Signal
   (`type: model_discovery`, signature per (provider, model-id)) and the
   generator turns it into a **Proposal** — e.g. *"claude-fable-5 discovered
   from Anthropic; in no rung. Suggested placement: new rung above opus-class."*
   Accepting the proposal is the operator action that edits `models.rungs` /
   `models.roles`. Discovery suggests adoption; it never performs it. Every
   claim on the proposal cites its evidence (the listing fields it read).
4. **No silent "all current."** A provider whose listing call fails shows as
   **degraded**, logged — never folded silently into "current." Silence and
   "all current" must never be indistinguishable. The old `RECOMMENDED` dict
   survives only as an offline fallback for a provider with no listing endpoint.
5. **No re-fire for known models.** A model already placed in a rung does **not**
   raise a `model_discovery` signal; the signal store dedups per
   (provider, model-id), so `fable-class` being in the catalog means Fable is
   reported KNOWN, not rediscovered every sweep.

Ownership sits under the `bot_config_integrity` guardian (check id
`model_discovery`). The notification path — Signal → Alerts page → operator chat
— runs through the `signal_notifier` producer allowlist like every other Signal.

---

## Cross-vendor checking (Goodhart's Law)

Cross-model self-checking exists because of Goodhart's Law: *"When a measure
becomes a target, it ceases to be a good measure."* If Evolve used Claude to
evaluate Claude's own work, it would systematically bias toward
Claude-flavored solutions. A model from a different provider buys independent
evaluation.

This property used to be carried by a dedicated `judge` role with a
`provider: "not-standard"` constraint. Since 2026-08 it is a **derivation at
the call site** instead (`resolve_cross_vendor` / `resolveCrossVendor`): *the
first credentialed model in the standard chain whose provider differs from
the one that produced the work being judged — or nothing, if the pod holds
only one provider's keys.* Practically:

- `standard` resolves to Anthropic → the cross-vendor check runs on the first
  credentialed OpenAI/Google/xAI model in the standard chain
- only one provider credentialed → **no cross-vendor check** (the caller
  falls back honestly and flags the result as self-judged)

There is nothing to configure: adding a second provider's key (and letting
easy-setup put its models on the tier chains) is what enables cross-vendor
checking — the same act that buys you failover when the primary provider is
rate-limited or down. That is why the AI Optimization page and the setup
checklist recommend a second provider.

---

## Account routing

Account routing is a layer on top of model routing. Where role routing selects
*which model* runs a session, account routing selects *which auth profile* the
session is pinned to.

OC natively supports multiple auth profiles per provider and rotates between them
reactively (on rate limit / cooldown). Account routing makes that **intentional**:
specific session types are pre-assigned to specific profiles before OC picks one.

| Session type | Default mapping | Reason |
|---|---|---|
| `productive`, `ambiguous` | primary account | Best experience for real work |
| `maintenance`, `background` | secondary account | Preserve primary capacity |
| `cron`, `heartbeat` | API key account | Metered, lowest cost |

Configure via an `accounts` section in `network.json`:

```json
"accounts": {
  "tiers": {
    "primary":   { "profiles": ["anthropic:user@example.com"],    "for_session_types": ["productive", "ambiguous"] },
    "secondary": { "profiles": ["anthropic:account2@example.com"], "for_session_types": ["maintenance", "background"] },
    "metered":   { "profiles": ["anthropic:api"],                  "for_session_types": ["cron", "heartbeat"] }
  },
  "routing": { "enabled": true }
}
```

Profile IDs must match those in
`~/.openclaw/agents/<agentId>/agent/auth-profiles.json` (`provider:identifier` —
OAuth is `provider:email`, API key is `provider:api`). `accounts.routing.enabled`
defaults to `false`; flip it to `true` after adding a second account. The hook
fails open: a first-turn or unclassified session always uses OC's default
rotation. Model override and auth-profile override are independent and can both
fire on the same session.

---

## Per-bot role overrides

Some bots run a different default role. Configure in `network.json`:

```json
"models": {
  "perBot": {
    "forge":        { "defaultRole": "fast" },
    "security-bot": { "defaultRole": "fast" },
    "team-bot-a":   { "defaultRole": "standard" }
  }
}
```

This sets the default role for that bot's background work. It does not affect
user-facing routing (productive sessions still resolve to `standard`).
`defaultRole` validates against `{fast, standard, power}` — `max` is not a
legal per-bot default (it is pull-only; see above).

---

## Model drift detection

`cost.py` checks whether each bot's running model matches its expected model:

```json
"bots": {
  "admin-bot": { "expectedModel": "anthropic/claude-sonnet-4-6" }
}
```

This check speaks concrete model IDs, unchanged by the rungs/roles rename. If a
bot is running a different model (e.g. it fell back to an API key with a
different model), `cost.py` raises an alert — catching silent model changes from
OC updates or auth failures. Update `expectedModel` whenever you intentionally
change a bot's model.

---

## Formerly-tierN mapping

For anyone reading old records, logs, or docs:

| Old tier | Old label | Role | Default rung |
|---|---|---|---|
| `tier0` | Judge | *(role retired 2026-08 — models fold into the standard chain)* | — |
| `tier1` | Power | `power` | `opus-class` |
| `tier2` | Workhorse | `standard` | `sonnet-class` |
| `tier3` | Grunt | `fast` | `haiku-class` |
| — | — | `max` (new) | `fable-class` |

Config-shape changes that rode along with the rename:

| Old config key | New config key |
|---|---|
| `models.tiers.tierN` | `models.rungs[]` + `models.roles{}` |
| `models.routing.maintenance_tier` | `models.routing.maintenanceRole` |
| `models.tiers.tierN.fallbacks` | the rung's `models[]` (after the primary) |
| `models.perBot.<bot>.defaultTier` | `models.perBot.<bot>.defaultRole` |
| `tier1.maxPerDayPerBot` | `roleCaps.power.maxPerDayPerBot` (+ new `roleCaps.max`) |
| annotation `model_tier` | annotation `model_role` (`model_tier` kept as alias) |

Historical annotations and cascade-log records that still carry tier keys stay
valid — the synthesis map above doubles as the read-side translation. Run
`sudo evolve-admin migrate-model-roles` once to convert live config files.
