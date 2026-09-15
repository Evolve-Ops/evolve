---
title: "Help: AI Optimization Page"
slug: ai-optimization
audience: public
last_reviewed: 2026-06-06
concepts:
  - model-tiers
  - providers
  - routing
  - model-catalog
ui_surface: admin.ai-optimization
related_specs: []
---

# Help: AI Optimization Page

The AI Optimization page controls how each bot selects and routes models — which models are in the catalog, which tier each maps to, and how routing decisions are made. This is where you configure the model tier system that automatically routes sessions to the right model.

---

## The Model Tier System

Evolve routes every session to the appropriate model automatically. Code never references a model by name — only by tier. This means swapping a model is a one-line change here, not a code change.

| Tier | Name | Role | Default |
|------|------|------|---------|
| tier0 | Judge | Cross-model evaluation (must differ from tier2's provider) | openai/gpt-4o |
| tier1 | Power | High-complexity tasks explicitly requested by the user | anthropic/claude-opus-4-6 |
| tier2 | Workhorse | Default for productive user-facing sessions | anthropic/claude-sonnet-4-6 |
| tier3 | Grunt | Background tasks, maintenance sessions, analysis | anthropic/claude-haiku-4-5 |

**How routing works:**
- Every session is anchored on `trigger_kind` (see "Tier cascade" below) and classified as `productive`, `maintenance`, or `ambiguous`
- Maintenance and background → routed to tier3 automatically (saves significant cost)
- Productive and ambiguous → tier2 by default, unless the operator or user has set a different default
- User says "use your best model" → tier1
- The tier0 constraint requires a different provider than tier2 (Goodhart's Law: a model can't fairly evaluate its own outputs)

The mapping between tier3/tier2/tier1 and the user-facing labels:

| Tier | User-facing label |
|------|-------------------|
| tier3 | `fast` |
| tier2 | `standard` |
| tier1 | `power` |

The labels are what appears on the per-bot default tier picker, in the `evo tier` keyword, and in the per-user-per-bot persistence layer. The numeric tiers are how the plugin's ModelRouter reasons internally.

---

## Tier cascade — per bot, per user, per turn

The routing decision is hierarchical (audit #69, three phases shipped May 2026):

1. **Trigger-kind anchor.** The session class is fixed on `trigger_kind` before model selection runs. Heartbeat sessions can't accidentally drift into tier1.
2. **Per-user-per-bot default.** If the user has set a default via `evo tier-default …` or the future per-user UI, that wins.
3. **Operator's bot-wide default.** The per-bot picker on this page.
4. **Bot-level fallback.** The original tier-routing rules below.

The per-user layer lives at `{sharedDir}/{botId}/user-tier-prefs.json`, keyed by the same `derive_user_key(channel, ext_id)` used for wizard state and profiles. Setting a user's pref to `auto` deletes their entry rather than persisting a tombstone — the file stays small.

### Per-bot default tier picker

The headline control on this page. Pick `fast` / `standard` / `power` per bot. Single source of truth for the bot's ordinary turns when no per-user override is set.

The picker writes through `evolve-tiers.json` (via the audited L2 applier path that goes through `/tmp` + `sudo /bin/cp` since the file is bot-owned). A post-deploy gate (`verify_tier_chain.sh`) fails CI if the chain is broken end-to-end.

### Evo keyword tier control

Reachable from any bot's chat:

- `evo tier <fast|standard|power|auto>` — set your own default on this bot. Persists per-user.
- `evo tier-default <fast|standard|power|auto>` — set the bot's default (admin / primary only). Same write path as the picker on this page.

Use `auto` to clear your override and fall back to the operator default.

### Tier-routing disagreement audit

A passive live-pod audit (`tier_routing_disagreement` detector) watches for divergence between classifier intent and dispatched tier and surfaces the rate as a Signal. Useful for catching regressions where a routing change silently misroutes a class of sessions.

---

## Sections

### Model Freshness

Compares each bot's tier assignments to the latest recommended models for the providers you have keys configured for. If you're still pinned to a model that's been superseded, this card shows it. Click **Check Now** to refresh.

### Model Catalog

The full list of models available to this bot, with tier assignment and enabled/disabled status. You can:
- **Add a model** — click the + button and enter the model ID in `provider/model-name` format (e.g., `anthropic/claude-sonnet-4-6`, `google/gemini-2.0-flash`)
- **Edit tier assignment** — drag a model to a different tier, or click edit to change it
- **Enable/disable** — disabled models are excluded from routing even if they're in a tier's fallback list
- **↻ Refresh** — reloads the catalog from the bot's `openclaw.json`

### Tier Definitions

Shows which models are assigned to each tier, in priority order. The first model in the list is primary; subsequent models are fallbacks used when the primary is unavailable or rate-limited.

Drag to reorder within a tier. Fallbacks are tried in order when the primary fails.

### Routing Rules

Controls when each tier is selected:
- **maintenance_tier** — which tier handles maintenance sessions (default: tier3)
- **background_tier** — which tier handles cron/background jobs (default: tier3)
- **max_tier1_per_day_per_bot** — daily limit for tier1 (Power) calls (default: 10)

### Fallback Configuration

What happens when a model fails:
- **fallbackMode** — `sequential` (try next in list), `random` (pick any available), or `off` (fail hard)
- **tierCascade** — if all models in tier2 fail, should Evolve cascade to tier3 rather than fail the session?

### Cascade toggle

Per-bot toggle on each row in Tier Definitions (PR 2291) — when on, the model router walks down the tier ladder on tier-2 errors or on bot-side "I need help" markers (e.g., tier1 → tier2 on a long-running tier1 turn). When off, a tier-2 failure fails the session. Off by default; this is a treatment-group experiment as of June 2026. The per-bot cost tile on Cost Optimization carries a `cascade-live` chip on bots where the toggle is on.

### Live Session Routing Status

Shows the current routing state for any active sessions — which tier is being used, which model was selected, and the session classification. Useful for verifying that routing changes took effect after a gateway restart. *(Marked Phase 2 — placeholder pending real-time event stream wiring.)*

### How Sessions Are Routed

A static walkthrough of the routing decision tree (keyword classifier → tier resolver → fallback cascade). This is the read-only counterpart to Routing Rules — useful when you want to understand why a session ended up on the tier it did.

---

## Common Questions

**A gateway restart notification appeared at the top — why?**
When you save routing changes, the plugin (TypeScript code inside the OC gateway) needs to restart to pick up the new configuration. The banner reminds you to restart; click **Restart** in the banner or go to Maintenance → Status to restart the gateway for the affected bot.

**How do I add a new model provider?**
1. Add the API key in Plugins → Credentials
2. Come back to AI Optimization and add the model to the catalog with the correct `provider/model-name` string
3. Assign it to a tier
4. Restart the gateway

**How do I know if tier routing is actually working?**
Check the Usage page → "By Model" breakdown. You should see your tier 3 model being used for a significant fraction of turns if your bots do any maintenance or background work. If everything is running on Sonnet, routing may not be active. Verify the plugin is installed (`evolve-admin deploy <bot>`) and the gateway has been restarted since the last config change.

**Why does tier0 need to be a different provider than tier2?**
Goodhart's Law: if you use the same model to evaluate its own proposals, it will systematically favor its own style of answers. By requiring tier0 to be from a different provider (Anthropic → use OpenAI or Google; OpenAI → use Anthropic or Google), Evolve gets independent judgment for proposal evaluation and behavioral tests.

**What model string format does Evolve expect?**
`provider/model-name` — for example:
- `anthropic/claude-sonnet-4-6`
- `openai/gpt-4o`
- `google/gemini-2.0-flash`
- `ollama/llama3.2` (for local Ollama)

These must match the format OpenClaw expects. Check your OC documentation if a model isn't being recognized.

**What's account routing?**
Account routing (if you have multiple auth profiles for one provider — e.g., two Anthropic accounts) lets you pin session types to specific accounts: productive sessions to your primary account, maintenance/background to a secondary account. This is optional and only useful with multiple profiles. Configure it in `network.json` under the `accounts` section.
