# Per-install Configuration Sandbox — Schema Survey

**Status:** schema pitch. No code yet. Approve / amend the schema before mechanism work begins.

**Date:** 2026-05-09

## 1. Mental model

Configuration is **cascading** (CSS-style). Stock defaults live in code; an install's
override file contains only the keys that diverge.

```
resolved(key) = override[key] if present else shipped_default[key]
```

The override file is the answer to *"what has this install customized?"* — by
construction, since it contains only divergences. New shipped defaults flow through
to non-overridden keys on upgrade. Overridden keys surface as a diff so the
operator can decide whether to keep the override or adopt the new default.

## 2. Override-strength taxonomy

Every tunable key carries a strength label. The label tells the operator what
overriding *costs* — it does not gate the override.

| Strength | Internal int | Meaning |
|---|---|---|
| **free** | 0 | Local economics, identity, escape hatches, feature flags. No globally optimal value exists. Override freely; new shipped defaults still flow through unrelated keys. |
| **advisory** | 1 | We have a default that's reasonable but not telemetry-informed. Override is fine; on upgrade you'll see if our default moved. |
| **shipped-policy** | 2 | Defaults we are actively tuning across installs from telemetry. Overriding here means you stop benefiting from that learning. RSI proposals targeting a shipped-policy key should be raised as upstream PRs by default; only sticky local overrides if the install has a concrete reason. |

Stored as a small int so RSI ranking is cheap; rendered to operators with the label.

## 3. Three categories of state, only one needs the cascade

| Category | Cascade applies? | Reason |
|---|---|---|
| **Identity** — network.json structure, bot identity, integration credentials, `bot_guides/`, `SOUL.md`, `manifests/` | No | No shipped default to propagate. Whatever the install authored *is* the value. |
| **Policy** — generator thresholds, behavior knobs, evolve-tiers routing, plugin tunables | **Yes** | Shipped defaults exist. Installs may diverge. Upgrade-propagation matters. |
| **Invariant** — generator charters, signal-store layout, state machines, wire formats, ACL/sudoers | No | Not overrideable. Locked in code. |

The sandbox's contract is: **the override file may only contain keys from category Policy.** Identity lives in its native files (network.json, SOUL.md, etc.) and is read through the sandbox API but not stored as overrides. Invariants are not addressable through the sandbox at all.

## 4. Out-of-sandbox decisions (locked from prior discussion)

- **Tier → model mapping.** Driven by the primary bot's `evolve-tiers.json`. The "evolve install" inherits its model assignments from its primary bot. The sandbox *reads* this; it does not store it. The shipped opinion is "if you have anthropic, sonnet-4-6 is our pick for tier2" — surfaced through `model_registry.RECOMMENDED` advisory, not as a default the install inherits.
- **Generator charters.** Fingerprint-locked at runtime. Not addressable.
- **API keys / auth profiles.** Always free; live in their existing stores. Sandbox surfaces them as "configured / not configured" but does not store secret values.

## 5. The schema, key by key

Backing-store paths use the existing `ConfigPatch` syntax: `{file}::{dotted.key}`.
Strength labels are the proposed value; flagged keys (⚠) need your decision.

### 5.1 `{shared_dir}/network.json` (pod-wide)

| Key | Strength | Stock default | Notes |
|---|---|---|---|
| `evolveVersion` | identity | — | Set by repo puller; not operator-tunable. |
| `networkId` | identity | — | |
| `primary` | identity | — | |
| `members` | identity | `[]` | |
| `sharedDir` | identity | `/Users/Shared/evolve` | |
| `timezone` | identity | host TZ | |
| `thresholds.dailySpendAlertUsd` | free | 5.0 | Local economics. |
| `thresholds.weeklySpendAlertUsd` | free | 20.0 | Same. |
| `thresholds.spendCapAction` ⚠ | **advisory** | `"downgrade-tier"` | Alternative is `"alert-only"`. Could argue shipped-policy. |
| `thresholds.maxSessionContextTokens` | advisory | 100000 | Provider-dependent ceiling. |
| `classifiers.tier.model` | free | provider-dep | |
| `classifiers.tier.fallback` | advisory | `"keyword"` | Only one option today. |
| `classifiers.judge.model` | free | provider-dep | |
| `alerts.channel` | identity | `"telegram"` | |
| `alerts.chatId` | identity | — | |
| `alerts.spendThresholdUSD` | free | 5.0 | **Duplicates `thresholds.dailySpendAlertUsd` — flag for cleanup.** |
| `alerts.spendAlertHour` | free | 12 | |
| `alerts.enabled` | free | true | Escape hatch. |
| `alerts.cronSilenceThresholdDays` | advisory | 2 | |
| `alerts.watchedCrons` | advisory | `["ai.evolve.*.measure", "ai.evolve.*.heal"]` | Install may extend. |
| `security.mode` | advisory | `"primary"` | Architecture choice. |
| `security.botId` | identity | null | |
| `security.autoRejectRisk` ⚠ | **shipped-policy** | `["high","critical"]` | Telemetry-tunable floor. |
| `security.rulesFile` | identity | path | |
| `models.tiers.*` | OUT OF SANDBOX | — | Driven by primary bot's `evolve-tiers.json`. |
| `models.routing.maintenanceTier` | advisory | `"tier3"` | |
| `models.routing.backgroundTier` | advisory | `"tier3"` | |
| `models.routing.ambiguousTier` | advisory | null | |
| `accounts.tiers.*` | free | — | Account structure varies per install. |
| `accounts.routing.enabled` | free | false | |
| `bots.<id>.role` | identity | — | |
| `bots.<id>.port` | identity | — | |
| `bots.<id>.securityScanning` | free | true | Per-bot opt-out. |

### 5.2 `{shared_dir}/better-engine-config.json` (pod + per-bot via existing cascade)

| Key | Strength | Stock default | Notes |
|---|---|---|---|
| `better_engine.enabled` | free | true | Escape hatch. |
| `rsi.enabled` | free | true | Per-bot opt-out. |
| `budget.monthly_cap_usd` | free | 50.00 | Local economics. |
| `budget.per_bot_daily_warn_usd` | free | 2.00 | |
| `budget.per_bot_daily_hard_usd` | free | 5.00 | |
| `budget.per_bot_monthly_cap_usd` | free | null | |
| `conversational_approval.enabled` | free | true | Operator escape hatch. |
| `conversational_approval.llm_intent_parse_enabled` | free | true | Cost-sensitive opt-out. |
| `conversational_approval.confidence_threshold` ⚠ | **shipped-policy** | 0.80 | Quality knob. |
| `conversational_approval.default_snooze_days` | shipped-policy | 3 | UX. |
| `conversational_approval.pending_expiry_minutes` | shipped-policy | 60 | UX. |
| `conversational_approval.push_preamble_enabled` | free | false | Off-by-default flag. |

### 5.3 `{shared_dir}/generators/<id>.json` — `GeneratorRecord.config`

**Generator detector tunables are not in the sandbox schema.** They are owned
by `_GENERATOR_TUNABLE_PARAMS` in [`evolve_admin.web.server`](../packages/admin/evolve_admin/web/server.py) — a
live system that already covers `budget_hawk`, `efficiency_hawk`, and
`gateway_diagnostician` with UI metadata (label/help/unit/step/decimals)
and matching POST endpoints (`/api/arbiter/bot-setup/<bot_id>` and
`/api/arbiter/generators/<id>/config`). It also handles the per-bot
generator-override merge path (`config.per_bot.<bot_id>.<nested_under>.<api_field>`)
that the sandbox does not.

The sandbox's `customizations()` deliberately does not cover generator
detector keys. The boundary:

- Sandbox owns install-config-at-large: network, better-engine, openclaw,
  evolve-tiers, identity docs (~50 keys).
- `_GENERATOR_TUNABLE_PARAMS` owns generator detector tunables (~13 keys).

A future PR can unify the two — add UI metadata fields to `TunableKey`,
project `_GENERATOR_TUNABLE_PARAMS` from the schema, route the existing
endpoints through the sandbox's write API. That is meaningful refactor
of live UI wiring; out of scope for this turn.

### 5.4 `{shared_dir}/bot_guides/<id>.md`

Whole-document. Identity. Authored content; no shipped default.

### 5.5 Bot-side: `~<bot>/.openclaw/openclaw.json` (evolve-relevant keys only)

| Key | Strength | Stock default | Notes |
|---|---|---|---|
| `plugins.evolve.tier` | free | `"full"` | Per-bot integration tier. |
| `plugins.evolve.summarizerMinTurns` | shipped-policy | 2 | |
| `plugins.evolve.classifierKeywordConfidenceFloor` | shipped-policy | 0.80 | Comment notes change from 0.75 — this is exactly the kind of thing telemetry tunes. |
| `plugins.evolve.costLedgerEnabled` | free | true | IO escape hatch. |
| `plugins.evolve.classifierModel` | free | `"anthropic/claude-haiku-4-5"` | Provider-dep. |
| `plugins.evolve.defaultModel` | free | unset | |
| `plugins.evolve.tierClassification` | advisory | `"session"` | |
| `plugins.evolve.reportingEnabled` | free | true | |
| `plugins.evolve.dashboardEnabled` | free | true | |
| `plugins.evolve.enableLLMSummarization` | free | true | |
| `plugins.evolve.enableLLMExtraction` | free | true | |
| `plugins.evolve.enableTaskExtraction` | free | true | |
| `plugins.evolve.classifierHints` | identity | — | Deployment-specific. |
| `plugins.evolve.applicationPatterns` | identity | — | Deployment-specific. |
| `session.dmScope` | identity | — | Per-deploy chat plumbing. |
| `channels.*` | identity | — | |
| `hooks.allowConversationAccess` | free | false | Per-bot privacy. |

(Other openclaw.json keys are OC's domain, not Evolve's — out of sandbox.)

### 5.6 Bot-side: `~<bot>/.openclaw/evolve-tiers.json`

| Key | Strength | Stock default | Notes |
|---|---|---|---|
| `routing.maintenanceTier` | free | per-bot | RSI proposes via TierAdjustment. |
| `routing.backgroundTier` | free | per-bot | |
| `routing.ambiguousTier` | free | per-bot | |
| `routing.enabled` | free | true | |
| `routing.confidenceThreshold` ⚠ | **shipped-policy** | per-bot | Quality knob. |
| `tier0..tier3.models` | free | provider-dep | Install-local; advised by `model_registry.RECOMMENDED`. |

### 5.7 Bot-side: `SOUL.md`, `AGENTS.md`, `manifests/`

Whole-document. Identity. Authored content.

## 6. Things this surfaces

1. **`thresholds.dailySpendAlertUsd` and `alerts.spendThresholdUSD` are the same number with two names.** Worth consolidating before the sandbox locks in the duplication.
2. **`generators/<id>.json` already cascades** through `_resolve_gen_config`. The sandbox formalizes this as the pattern, not a new mechanism.
3. **`better-engine-config.json` already cascades** (pod_defaults → bots[bot]). Same observation.
4. **The vast majority of "tunable" keys are `free`.** The `shipped-policy` set is small and mostly lives inside generator detectors. That's a useful concentration: most of the operator-facing config story is "go wild, your overrides survive upgrades cleanly," and only a small set of detector thresholds carries the "you're overriding learning" warning.
5. **Identity is huge.** A lot of what looks like configuration is really per-install identity (bot list, channel wiring, deployment-specific keyword hints). These don't need cascade machinery — they need a clean read API and survive upgrades trivially because there's no shipped default to conflict with.

## 7. Decisions to confirm before mechanism work

1. **Strength labels: free / advisory / shipped-policy.** Adopt these three? Or split / merge?
2. **The five ⚠ keys above:** strength label correct?
   - `thresholds.spendCapAction` (advisory or shipped-policy?)
   - `security.autoRejectRisk` (shipped-policy?)
   - `conversational_approval.confidence_threshold` (shipped-policy?)
   - `routing.confidenceThreshold` in evolve-tiers (shipped-policy?)
   - The duplication between `thresholds.dailySpendAlertUsd` and `alerts.spendThresholdUSD` (consolidate?)
3. **Override-storage location.** One file (`{shared_dir}/sandbox-overrides.json`) or keep overrides in their native backing stores (current state) and let the sandbox project a unified view? My lean: **keep native** — backwards-compatible with everything written today, and the sandbox is a *projection* layer, not a *storage* layer. The override file then becomes a generated artifact, not the source of truth. But this means the diff API has to walk seven stores at read time (cheap; they're all small JSON).
4. **Bot-side scope.** Survey includes bot-side. Confirm including bot-side in v1, or start `shared_dir`-only?

## 8. What comes next (after sign-off)

In order, smallest commits first:

1. Encode strength labels into a Python schema module: `evolve_admin/config_sandbox/schema.py`. One entry per key, with backing-store path, strength, stock default, type, and one-line description. No code that reads or writes — just the schema.
2. `resolve(key_path) -> value` and `customizations() -> list[OverrideEntry]` over the existing backing stores. Read-only.
3. UI page: "What this install has customized." Renders `customizations()` grouped by strength, with the rationale text.
4. Upgrade-diff tool: on deploy, compare the running schema's stock defaults against the previous version's; surface keys whose default moved as advisories on the customizations page.

Steps 1–3 are Option A. Step 4 is the upgrade-propagation feature that makes the cascade actually pay off. Write paths (Option B) come after.
