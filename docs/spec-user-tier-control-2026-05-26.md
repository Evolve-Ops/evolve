# User-facing tier control — design spec

> **Superseded naming (2026-06-09):** the four-tier `tier0`–`tier3` model
> this spec assumes has been replaced by **rungs & roles**. See
> [docs/spec-model-rungs-and-roles-2026-06-09.md](spec-model-rungs-and-roles-2026-06-09.md)
> for the current model and [docs/model-roles.md](model-roles.md) for the
> operator guide. The user-control mechanism described below still holds;
> read `tierN` as the corresponding role (`tier3`→`fast`, `tier2`→`standard`,
> `tier1`→`power`, `tier0`→`judge`, plus the new pull-only `max`).

**Status:** Draft.

**Date:** 2026-05-26.

**Parent doc:** [docs/model-roles.md](model-roles.md) — the rungs/roles system and
classifier-driven routing (formerly `docs/model-tiers.md`, the four-tier system).

**Related:**
- [docs/spec-evo-oc-native-2026-05-19.md](spec-evo-oc-native-2026-05-19.md) §3.7 — the
  authority mechanism this spec mirrors.
- the internal cost-alerting-blackout postmortem
  — the spend-cap safety net that must keep winning over user choice.

---

## Goal

Give the operator a per-conversation knob to pick how much compute evo applies to
their turn — analogous to authority, which already lets the operator say "ask /
auto-small / auto" per send.

Today tier selection is automatic (classifier on turn 1 → routing on every turn).
The classifier is right most of the time, but it has no signal for "this question
is harder than it looks" or "this is throwaway, don't burn Sonnet." The user can
say "use your best model" in plain English; the docs claim that triggers tier1,
but the trigger is dead code ([packages/analyzer/models.py:389](packages/analyzer/models.py:389) has no live caller).

Concretely:
1. Wire the documented tier1 path so explicit power requests actually route.
2. Add a UI control next to authority: `Auto / Fast / Standard / Power`.
3. Surface the tier choice in evo's session-context so the model can acknowledge it.
4. Handle the tier1 daily cap gracefully and visibly.

Non-goal: changing how the classifier works. Auto stays exactly as-is.

---

## Scope decisions

1. **Per-turn, not persisted.** Mirrors authority precisely
   ([packages/admin/evolve_admin/web/home_chat_routes.py:126](packages/admin/evolve_admin/web/home_chat_routes.py:126)):
   each `/api/home/chat` send includes the operator's chosen tier; the server validates
   and forwards. The *frontend* may render it as sticky-for-the-session for UX, but
   that's a client-side concern. The server has no memory of "what tier this session
   prefers" — every turn is authoritative.
2. **Four choices, including Auto.** `auto | fast | standard | power`. Default `auto`
   (preserves today's behavior). Invalid values fall back to `auto`, not to a tier —
   the operator's intent on a malformed value is unknown, so use the classifier.
3. **User choice beats classification, loses to safety nets.** Ordered precedence
   (highest wins):
   1. Spend-cap downgrade flag active → force tier3 (already in
      [ModelRouter.ts:186](packages/plugin/src/observer/ModelRouter.ts:186); unchanged)
   2. `/model` command set in OC → respect, no override
      ([packages/plugin/src/observer/ModelRouter.ts:181](packages/plugin/src/observer/ModelRouter.ts:181) checks `ctx.userModelOverride`)
   3. Operator tier choice from this spec (when not `auto`)
   4. Classifier-driven routing (today's behavior)
   5. Bot default
4. **tier1 has a daily cap with graceful fallback.** Per
   [docs/model-tiers.md:343](model-tiers.md:343), tier1 defaults to 10 uses/bot/day.
   On cap hit, route to tier2 *and* surface a visible "Power capped today, used Standard"
   string in the reply payload so the frontend can render it. Never silent.
5. **Admin UI chat first; Telegram + cross-bot deferred.** Telegram has no obvious
   per-turn affordance; a `/tier power` text command can land in a follow-on. Cross-bot
   dispatches via the `evo` keyword are not user-driven, so the toggle doesn't apply.
6. **Inline keyword detector is *not* in v1.** Phrases like "use your best model"
   could auto-bump to power, but that conflicts with an explicit toggle (which wins?
   does the toggle override the keyword?). Skip the keyword path until the toggle
   ships and we see usage. Cleaner mental model: power is something you *click for*,
   not something the system tries to read your mind about.
7. **Per-bot opt-out, default on.** `tiers.json::userTierOverride.enabled = true`.
   Bots with `defaultTier: tier3` (e.g. forge per [docs/model-tiers.md:331](model-tiers.md:331))
   should probably opt out — surfacing a Power control on a background-class bot is
   confusing.

---

## Architecture

### Where the choice enters the system

```
Frontend chat composer (sticky toggle, client-side memory)
  ↓ POST /api/home/chat   { ..., tier: "auto"|"fast"|"standard"|"power" }
home_chat_routes.api_home_chat()                     ← validates, defaults on bad input
  ↓ session_ctx.tier_preference
evo.proxy.send_to_evo()                              ← passes through to plugin
  ↓ POST /api/evo/dispatch  body.session_context.tier_preference
EvoDispatchClient → plugin gateway turn start
  ↓ TurnObserver.setUserTier(sessionKey, tier)       ← NEW
ModelRouter.sessionUserTier[sessionKey] = tier       ← NEW per-session map
  ↓
before_model_resolve hook fires
  ↓ ModelRouter.resolveModelOverride(sessionKey)
     → consults userTier FIRST, then classification (existing logic)
  ↓ { modelOverride: <tierN-model> | none }
OC runs the turn with the chosen model
```

### ModelRouter changes

[packages/plugin/src/observer/ModelRouter.ts](packages/plugin/src/observer/ModelRouter.ts) — add a parallel map and a setter that's
written by `TurnObserver` on every turn start (not just first-turn classification):

```typescript
private sessionUserTiers: Map<string, "auto"|"fast"|"standard"|"power">;

setUserTier(sessionKey: string, choice: string): void {
  if (!["auto", "fast", "standard", "power"].includes(choice)) return;
  if (choice === "auto") { this.sessionUserTiers.delete(sessionKey); return; }
  this.sessionUserTiers.set(sessionKey, choice as any);
}

clearSession(sessionKey: string): void {
  this.sessionTypes.delete(sessionKey);
  this.sessionUserTiers.delete(sessionKey);    // NEW
}
```

`resolveModelOverride` precedence becomes:

```typescript
resolveModelOverride(sessionKey: string): string | null {
  if (this.config.routing?.enabled === false) return null;

  // 1. Spend-cap safety net (unchanged, wins over everything)
  if (isSpendCapActive(this.sharedDir, this.botId)) {
    return this.config.tiers["tier3"]?.models?.[0] ?? null;
  }

  // 2. Operator tier choice (NEW) — beats classification
  const userTier = this.sessionUserTiers.get(sessionKey);
  if (userTier) {
    const tierKey = userTier === "fast" ? "tier3"
                  : userTier === "standard" ? "tier2"
                  : "tier1";  // power
    return this.config.tiers[tierKey]?.models?.[0] ?? null;
  }

  // 3. Classification-driven routing (today's behavior, unchanged)
  const sessionType = this.sessionTypes.get(sessionKey);
  if (!sessionType || sessionType === "productive" || sessionType === "ambiguous") {
    return null;
  }
  const tierKey = sessionType === "background"
    ? (this.config.routing?.backgroundTier ?? "tier3")
    : (this.config.routing?.maintenanceTier ?? "tier3");
  return this.config.tiers[tierKey]?.models?.[0] ?? null;
}
```

Key invariant: **if the operator picks Standard, we explicitly route to tier2 even
when the classifier says maintenance**. The current code returns `null` for
productive/ambiguous (uses the bot default, which happens to be tier2). With user
tier set, we must return the tier2 model string explicitly — otherwise a bot whose
default is tier3 (forge) would silently ignore "Standard."

### tier1 daily-cap handling

The cap is in [packages/analyzer/models.py](packages/analyzer/models.py) (Python), but the hot path is TypeScript.
Two options:

- **A.** Mirror the cap counter in the plugin (read/write `{sharedDir}/calibration/tier1_usage.json`).
- **B.** Check the cap server-side in `api_home_chat` and downgrade the tier preference
  to "standard" before forwarding to the plugin, returning a `tier_capped: true`
  field in the response.

**Choose B.** Server-side check keeps the plugin simple, gives the frontend a clean
signal to render ("Power capped today — used Standard"), and the cap accounting stays
in one place. The plugin trusts what it's told. The frontend reads `tier_capped` and
renders a one-line note under the reply.

Cap counter increment happens *after* a successful tier1 turn (in the proxy response
path), not pre-emptively at request time — same shape as how OC's cost ledger works.

**"Today" window: pod-local midnight.** The cap resets at midnight in the pod's
timezone (`/etc/localtime` on the mini). This matches operator intuition — "I get
10 today, and 'today' rolls over when I go to sleep." Both `check_daily_limit()` in
[models.py](packages/analyzer/models.py) and the spend-cap flag path
([ModelRouter.ts:29](packages/plugin/src/observer/ModelRouter.ts:29) `spendCapFlagPath` uses `toISOString().slice(0,10)` —
**UTC**) need to align on pod-local. P1 fixes both; the spend-cap path being on UTC
today is a latent bug that this work surfaces (Costa Rica is UTC-6, so the cap
currently rolls over at 6pm local — wrong). File a follow-up issue when P1 lands.

### Operator-adjustable daily cap

The cap is operator-tunable per-bot via the admin UI, not a fixed `network.json`
edit. Lives on the same admin-UI surface as the spend-cap controls (`Settings → AI
Optimization → Model Catalog → Tier1 daily cap`). Range 0–100/day per bot; default
10. Setting 0 disables Power for that bot (chip hides; explicit Power sends fall
back to Standard with `tier_capped: true`). CLI parity: `evolve-admin models cap
<bot> <n>`.

Storage: same `tiers.json::userTierOverride` block introduced for the opt-out —
add a `dailyCap: <int>` field. The cap check in `api_home_chat` reads from there
with `network.json::tier1.maxPerDayPerBot` as the legacy fallback (preserving the
current default).

### session-context block (informational only)

The model already sees `Operator: pod_admin (authority tier: ask)` per
[packages/admin/evolve_admin/evo/proxy.py:298](packages/admin/evolve_admin/evo/proxy.py:298). Add a parallel line:

```
Tier preference: power
```

Only emit when non-default (skip for `auto` to avoid prompt bloat). This is
informational — routing is enforced by the hook, not by the model. But evo can
acknowledge it in reply when relevant ("Using Power for this — let me think harder.").

---

## UI

### Composer chip, next to authority

Existing authority control already lives in the chat composer. The tier chip sits
beside it, same visual weight:

```
[Auto ▾]  [Authority: Ask ▾]    [Send]
```

Dropdown options:

| Label | Tier | Cost hint | Sub-line |
|---|---|---|---|
| **Auto** | classifier-driven | — | "Auto → Standard (productive session)" |
| **Fast** | tier3 (Haiku) | $ | "Cheap and quick — good for simple lookups" |
| **Standard** | tier2 (Sonnet) | $$ | "Default workhorse" |
| **Power** | tier1 (Opus) | $$$ | "Best model — use sparingly (capped at N/day)" |

**Auto exposes what the classifier picked.** The Auto label's sub-line dynamically
reads "Auto → \<tier label\> (\<session class\>)" — so the operator can see the
classifier's verdict without having to dig into telemetry. Requires the response
to include the tier the classifier landed on for *this* turn (new field
`tier_classified` in the response payload — distinct from `tier_resolved`, which
includes user override and safety nets). The label updates after each turn lands.
First-turn UX before classification: "Auto → picking based on your message…"

Cost-class glyphs derive from `network.json::models.tiers[N].costClass`
(low/medium/high → $/$$/$$$).

### Client-side stickiness

The toggle persists in the browser per-session (sessionStorage keyed by
`session_id`). Reload the page → it remembers. Server has no memory; every send
carries the explicit value.

### "Power capped" surface

When the response includes `tier_capped: true`, render a one-line note under the
reply: *"Power capped today (10/day) — used Standard."* No banner, no dialog. The
operator can keep picking Power for the rest of the day; it'll just keep
downgrading visibly.

### Per-bot opt-out rendering

When `tiers.json::userTierOverride.enabled === false`, the composer hides the chip
entirely. We don't render a disabled control — that invites the question "why is
it disabled?" Better to not show it.

---

## Phases / PRs

**P1 — Backend plumbing.** No UI yet. ~150 LOC, mostly plumbing.
- `api_home_chat` accepts `tier` field, validates, plumbs to session_ctx
- `send_to_evo` forwards `tier_preference` to plugin via session_context
- `evo_routes /api/evo/dispatch` accepts `tier_preference` in session_context block
- Plugin gateway pulls it from session_context, calls `ModelRouter.setUserTier`
- `ModelRouter.resolveModelOverride` precedence updated per spec
- `tier1` daily-cap check in `api_home_chat` (server-side, downgrades + returns `tier_capped`)
- Unit tests on the precedence stack: spend-cap > user-tier > classification > default

**P2 — Admin UI composer toggle.** Frontend only.
- Composer chip beside authority
- sessionStorage stickiness
- `tier_capped` notice rendering
- Cost-class glyphs read from a small public endpoint that serves the tier config
  (so the frontend doesn't have to ship tier names)

**P3 — Session-context line + AGENTS.md update.**
- `format_session_context` emits `Tier preference: <X>` when non-default
- evo's AGENTS.md gains a one-paragraph section on what the line means and when to
  reference it ("you can mention it if the user asks why you took an approach;
  don't volunteer it otherwise")

**P4 — Per-bot opt-out + operator-adjustable cap.**
- `tiers.json` schema gains `userTierOverride.enabled` (default true) and
  `userTierOverride.dailyCap: <int>` (default 10)
- Admin UI tier config page exposes both: toggle + cap slider/input
- Cap=0 hides the Power chip (treated as opt-out for Power specifically; Fast and
  Standard remain available)
- CLI parity: `evolve-admin models cap <bot> <n>`
- Composer hides the whole chip when `enabled === false`
- For forge/security/etc., explicit opt-out in the seed config

**P5 — Telegram + keyword detector (deferred).**
- Slash-command `/tier power|standard|fast|auto` in the Telegram surface
- Keyword detector ("use your best model" → bump to power for this turn) —
  only if usage shows it's missed and operators ask for it

P1+P2 are the v1 ship. P3-P4 are polish. P5 lands when there's signal.

---

## Edge cases / failure modes

- **Operator picks Power on a session the classifier flagged maintenance.** User
  choice wins. They asked for it; route to tier1. This is the whole point of the
  control.
- **Spend cap trips mid-session.** Next turn routes to tier3 regardless of user
  choice. `tier_capped` style indicator should also fire here, with a different
  message ("Cost cap active — tier locked to Fast.").
- **`/model` set by user via OC slash command.** Hook returns `{}` immediately; user
  tier choice ignored. The OC override is more specific.
- **tier1 cap exhausted, user keeps picking Power.** Each request downgrades to
  Standard and returns `tier_capped: true`. No throttle / blocklist needed.
- **Network.json has no tier1 configured.** `resolveModelOverride` returns null
  for the tier1 lookup; OC uses default. Frontend's Power chip should be hidden
  in that case (covered by the same public tier-config endpoint P2 introduces).
- **Per-bot `defaultTier: tier3` + operator picks Standard.** The router explicitly
  routes to tier2 — see the "must return tier2 explicitly" invariant under
  `resolveModelOverride`. Without this, the bot default wins and Standard is a no-op.
- **Bot opt-out + frontend still sends `tier: "power"`.** Server validates against
  the bot's `userTierOverride.enabled` and silently ignores (treats as `auto`). No
  error — frontends may be cached / out of date.
- **classification is "background" (cron / heartbeat).** These don't go through
  /api/home/chat, so `tier_preference` is never set. No-op.

---

## Account routing interaction

Model routing and account routing are independent layers per
[docs/model-tiers.md:253](model-tiers.md:253), and that stays true here. The
operator's tier choice picks *which model* runs the turn; account routing picks
*which auth profile* it runs under. They don't collapse:

- An operator picking Fast on a productive session still gets primary-account
  routing (productive → primary per the default mapping). The fact that they're
  running tier3 doesn't demote the account.
- An operator picking Power on a maintenance-classified session gets tier1 + the
  account that the session classification maps to (maintenance → secondary), not
  a "Power account."
- If `accounts.routing.enabled === false` (the default — most pods only have one
  profile), no account override fires. OC uses whatever auth-profiles.json says,
  same as today.
- **MAX phase-out:** MAX no longer covers OC turns
  ([memory: project_max_auth_obsolete](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_max_auth_obsolete.md));
  every turn bills at API rates. This doesn't change the routing logic — accounts
  are still pinned by session class — but it means the "preserve primary capacity"
  argument for routing maintenance to secondary is mostly historical. Don't
  remove account routing in this spec; just don't treat MAX-preservation as
  load-bearing.

The evo bot's own account-routing config (whatever's in its `network.json::accounts`
section) is the source of truth. This spec doesn't introduce a per-tier account
override.

---

## Telemetry

Per-turn annotation gains:
```json
{
  "tier_preference": "power" | "standard" | "fast" | null,    // null when auto
  "tier_classified": "tier2",   // what the classifier picked (for the Auto sub-line)
  "tier_resolved": "tier1",     // the tier actually used
  "tier_capped": false,         // true when downgraded due to tier1 cap
  "tier_override_source": "user" | "classifier" | "spend_cap" | "oc_user_override"
}
```

This lets calibration see how often operators reach for the control, which
direction they go (more often up to Power, or down to Fast?), and how often the
cap fires. After 2-4 weeks of data, decide whether the keyword detector (P5) is
worth building.

---

## Resolved questions (settled 2026-05-26)

1. **Auto exposes the classifier's choice.** Sub-line on the Auto option reads
   "Auto → \<tier\> (\<session class\>)" — see UI section. New telemetry field
   `tier_classified` carries the value back to the frontend.
2. **Pod-local midnight for the cap reset.** Also surfaces a latent UTC-vs-local
   bug in `spendCapFlagPath` ([ModelRouter.ts:29](packages/plugin/src/observer/ModelRouter.ts:29)) — P1 aligns both.
3. **Operator-adjustable cap is in scope.** Per-bot, admin UI + CLI, range 0–100,
   default 10. Lives in `tiers.json::userTierOverride.dailyCap`. Cap=0 hides the
   Power chip. Folded into P4.
4. **Account routing operates independently and unchanged.** Tier choice picks
   *which model*, account routing picks *which auth profile* — they don't
   collapse. MAX phase-out doesn't change the mechanism, just makes
   "preserve-primary-MAX-capacity" a less load-bearing rationale. See "Account
   routing interaction" section above.
