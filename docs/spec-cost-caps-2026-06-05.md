# Cost cap normalization — full spec (2026-06-05, revised 2026-06-06)

Status: **DRAFT** — awaiting review before Phase 4b+ implementation.

**2026-06-06 revisions** (this PR):
- Monthly tolerance is now an **alert-only** threshold; the derivation
  rules that previously translated `monthly_budget_usd` into
  `daily_warn_usd` / `tier_downgrade_usd` / `l1_breaker_usd` /
  `l2_breaker_usd` are removed. Every threshold is set explicitly.
- UI mounts collapse from three to **two**: the canonical caps panel
  lives on Cost Optimization → per-bot tab (per-bot scope, above the
  Context & Session matrix) and Cost Optimization → POD tab
  (pod-default scope). The Settings page Cost & Caps card is replaced
  by a deep link to Cost Optimization.
- New **Day boundary** section: caps roll at midnight in the pod's
  local timezone (not UTC).

## Problem

As of 2026-06-05, per-bot cost caps live in four places with three
storage layers, three editor shapes, and one inconsistent action menu:

| Surface | Storage | Editor | Owner |
|---|---|---|---|
| Settings → Bots → "Cost & caps" card | `better-engine-config.json::bots.<bot>.budget.*` | text inputs | Phase 3 |
| Settings → Bots → "Customizations" card (per-session + TTL pickers) | `sandbox/overrides/<bot>.json` | bespoke pickers | pre-Phase 3, never cleaned up |
| Cost Optimization → "POD-WIDE BUDGET" | `network.json::thresholds.dailySpendCapUsd` | preset chips | pre-Phase 1 |
| Cost Optimization → "SPENDING CAPS & ENFORCEMENT" | same `network.json::thresholds.*` | text inputs + action dropdown | pre-Phase 1 |

Symptoms operator sees:
- Setting "Daily hard cap" in one surface and a different value in
  another — they're different fields with the same label.
- "Per-session cap" and "Prompt-cache TTL" each have two editors on the
  same Settings page (Cost & caps + Customizations).
- "When cap is hit: downgrade-tier / pause-crons / suspend-bot" implies
  three remediation tiers but the storage carries one threshold.
- Pod-wide values look like defaults but it's unclear whether changing
  the pod default will overwrite per-bot overrides (it doesn't — but
  the UI doesn't say so).
- Preset chips vs text-entry boxes for the same logical field across
  surfaces.

## Design

### Storage

**Single canonical store: `better-engine-config.json`.**

```yaml
budget:
  # ── ALERTS (notify only) ────────────────────────────────────────
  # Each window is an independent alert threshold. Crossing one fires
  # a notification to the pod operator (via whatever channels are
  # subscribed in the signal_notifier — Telegram, Slack, etc.); it
  # does NOT cascade to or trip any remediation tier.
  monthly_budget_usd:    float | null   # Alert when month-to-date spend crosses (per bot)
  weekly_warn_usd:       float | null   # Alert when rolling 7-day spend crosses (per bot)
  daily_warn_usd:        float | null   # Alert when today's spend crosses
  pod_weekly_warn_usd:   float | null   # Alert when pod-total rolling 7-day spend crosses (pod_defaults level only)

  # ── REMEDIATION (escalating enforcement) ────────────────────────
  tier_downgrade_usd:    float | null   # Switch primary to tier 3 for rest of day
  l1_breaker_usd:        float | null   # Disable heartbeat + background sessions
  l2_breaker_usd:        float | null   # Stop bot gateway (no chat, no background)

  # ── SENTINEL (in-flight) ─────────────────────────────────────────
  per_session_cap_usd:   float | null   # Reject next turn in session over this

  # ── OPTIMIZATION (no enforcement) ───────────────────────────────
  cache_retention:       "short" | "long" | null   # Anthropic prompt-cache TTL
```

The Context & Session Settings matrix on Cost Optimization owns
`cache_retention` display — it's a setting, not a cap, so the caps
panel does not include it (see "UI" section below for the mount).

Same shape under `pod_defaults.budget.*` for pod-wide defaults, with
the exception that `pod_weekly_warn_usd` lives *only* at the pod level
(it's an aggregate, not a per-bot value).

### Resolution

```
effective(bot, field) =
    bots[bot].budget[field]           if explicitly set (any non-null value)
    else pod_defaults.budget[field]    if explicitly set
    else compiled_default               (only for the optimization knob;
                                         alert/remediation thresholds
                                         have no compiled default — null
                                         means "no enforcement")
```

**Per-bot override is sticky.** Changing the pod default never
overwrites a per-bot override that's already set. The UI surfaces this
explicitly: a bot's panel shows "(overrides pod default $X)" hint text
next to any overridden value.

**Every threshold is independent — there is no derivation.** Earlier
drafts of this spec had `monthly_budget_usd` derive `daily_warn_usd`,
`tier_downgrade_usd`, `l1_breaker_usd`, and `l2_breaker_usd` via
multipliers. That was removed on 2026-06-06: monthly tolerance is now
itself an alert threshold (fires when month-to-date spend crosses it),
and every other threshold is set explicitly by the operator. The
benefit is that "set a daily warn at $5" never reaches across windows
to imply a $150 monthly trigger the operator didn't ask for, and the
UI doesn't have to explain a derivation rule that didn't carry its
own weight.

### Behavior catalog

Alert phrasing is **channel-agnostic**: the threshold writes an Alert
to the signal store; `signal_notifier` then fans the alert out to
whichever channels the operator has subscribed (Telegram, Slack,
Discord, email, …). "Notifies the operator" is the canonical copy on
every UI surface; "Telegram alert" is wrong because Telegram may not
be the configured channel.

| Threshold | Action when crossed | Reversal | Audience |
|---|---|---|---|
| `daily_warn_usd` | Notifies the operator (dedup once per day) | Midnight (pod local TZ) | Pod operator |
| `weekly_warn_usd` | Notifies the operator (dedup once per ISO week) | Monday 00:00 (pod local TZ) | Pod operator |
| `monthly_budget_usd` | Notifies the operator (dedup once per calendar month) | Month-start 00:00 (pod local TZ) | Pod operator |
| `pod_weekly_warn_usd` | Notifies the operator (pod-total aggregate) | Monday 00:00 (pod local TZ) | Pod operator |
| `tier_downgrade_usd` | Write `agents.defaults.model.primary` to tier-3 model for the bot's gateway; gateway reloads on next heartbeat | Midnight (pod local TZ) auto-revert | Pod operator (silent to users) |
| `l1_breaker_usd` | `breakers_enforce.enforce_trip(bot, "cost", "l1")` — disable heartbeat + background sessions; user chat keeps working | 24h auto-reset OR `evolve-admin breaker reset <bot> cost` | Pod operator (silent to users) |
| `l2_breaker_usd` | `breakers_enforce.enforce_trip(bot, "cost", "l2")` — `launchctl bootout` the bot gateway. Pod operator gets an alert. No auto-reset; explicit operator action required (`evolve-admin breaker reset <bot> cost` + manual gateway restart) | **Manual only** | Pod operator + bot users (chat goes silent) |
| `per_session_cap_usd` | OpenClaw plugin writes per-session breaker; next turn rejected with budget-exceeded message | Operator raises cap or deletes session's breaker file | Bot users (in-session) |

**Tier downgrade enforcement is direct and deterministic** when the
explicit threshold is crossed. Budget Hawk continues to propose tier
downgrades via the arbiter when the warn cap is crossed *repeatedly* —
that's a different signal (pattern of waste), not a hard ceiling.

### Day boundary

"Today's spend" rolls at **midnight in the pod's local timezone**.
The pod runs as a single LaunchDaemon on the mini, so the local
timezone of the mini (e.g. `America/Los_Angeles`) is the canonical
day boundary. UTC is the wrong answer — a Pacific-time operator
reading "Daily warn $5" would see the cap roll at 4–5pm local, which
is confusing and breaks the mental model of "today's spend."

Implementation: `cost_watchdog.py` and `spend_alert.py` resolve "today"
via `datetime.now(zoneinfo.ZoneInfo(pod_tz)).date()`. The pod's TZ is
read from `network.json::pod.timezone` if set; otherwise it falls back
to the operator's system TZ via `time.tzname[0]` (which on a single-
mini install gives the mini's configured TZ — almost always what the
operator wants).

The "rolling 7 days" window for `weekly_warn_usd` and the "rolling
month" window for `monthly_budget_usd` also use local-TZ day boundaries
internally. The pod operator is told the rollover time explicitly on
each cap's tooltip — e.g. "Rolls at 00:00 America/Los_Angeles."

DST handling falls out of `zoneinfo` automatically: a 23- or 25-hour
day is fine, since the cap is "spend in this calendar day," not "spend
in any 24-hour window."

### Validation

`l2 > l1 > tier_downgrade > daily_warn` must hold whenever all four
are explicitly set. Save endpoint rejects with HTTP 400 + a clear
error message identifying which pair inverts the ladder. Null values
are skipped in the check (a missing rung doesn't break the ladder for
the others).

Weekly thresholds have no ordering constraint with the daily ones
(they're cumulative-over-window, not instantaneous).

### Pod-wide vs per-bot semantics — rules

1. **Pod-default is the inheritance source.** Bots without an explicit
   override use the pod default. Bots with an override stay sticky.
2. **Changing the pod default does NOT cascade to existing overrides.**
   It DOES change the value for bots that have no override.
3. **"Reset to default" UI action** clears the per-bot override,
   making the bot inherit the pod default again.
4. **Pod-default panel can be "Off"** for any threshold — meaning
   bots that don't override get no enforcement at that tier. The
   editor shows this as a clear "Off" chip, not an empty field.
5. **Compiled defaults** ship with the install for the OPTIMIZATION
   knob only (`cache_retention = "short"`). Every alert/remediation
   threshold ships as `null` (no enforcement); operator must set
   either a pod default or per-bot value to enable that tier.

### UI: one canonical Cost & Caps matrix

Single render function `renderCostCapsMatrix(scope, botId?)` with
`scope ∈ {'per-bot', 'pod-default'}`. Same component, **two mounts**:

1. **Cost Optimization → per-bot tab** (`scope='per-bot'`) — sits
   directly above the Context & Session matrix. Four columns:
   `Setting | Value | Pod default | Description`. The Value column
   uses the same tristate chip pattern as the Context & Session
   matrix: `Default` (inherit pod default), `Off`, `Custom $___`.
   The active chip is highlighted; clicking another chip commits the
   write. When the pod default is itself "Off", the Default chip
   reads `Default (off)` so it's clear what inheritance buys you.
2. **Cost Optimization → POD tab** (`scope='pod-default'`) — three
   columns only: `Setting | Value | Description`. The Pod default
   column collapses because *this is the pod default*. Tristate
   chips reduce to `Off | Custom $___`. Changing values here changes
   inheritance for every non-overridden bot.

The Settings page Cost & Caps card is replaced by a simple deep link
("Configure cost caps on Cost Optimization →") that switches to the
relevant page and selects the right bot tab. The pre-existing
"Pod-wide budget" preset tile and the "Spending Caps & Enforcement"
tile on Cost Optimization are both deleted — replaced by these two
mounts respectively.

The matrix row shape mirrors the Context & Session Settings matrix
shipped in [#2257](https://github.com/evolve-ops/evolve/pull/2257) so
the operator's eye doesn't have to retrain between the two surfaces.
Group rows separate ALERTS / REMEDIATION / SENTINEL sections.

The "Edit cost levers" modal pattern (preset chips that commit
instantly) stays gone — too easy to mis-tap.

### What gets deleted

- "Edit cost levers" modal (already gone — Phase 3)
- Customizations card pickers for cache TTL + per-session cap (Phase 4b)
- Cost Optimization → "POD-WIDE BUDGET" panel (replaced by canonical pod-defaults editor)
- Cost Optimization → "SPENDING CAPS & ENFORCEMENT" panel (replaced; action dropdown deleted)
- `network.json::thresholds.dailySpendCapUsd` + `dailySpendAlertUsd` + `weeklySpendAlertUsd` + `spendCapAction` (migrated to BE config)
- `spend_caps.py` enforcement-action path (replaced by direct threshold-keyed enforcement)
- Sandbox TunableKey schema entries for `sessionBudgetCapUsd` + `cacheRetention`
- Materializer's sandbox-override fallback for those two keys

### What stays

- `network.json::bots.<bot>.daily_cap_usd` — already removed (Phase 4a)
- `breakers_enforce` module — extended with an `l2` enforcement that
  calls `launchctl bootout`; L1 stays as today
- Budget Hawk generator — keeps proposing tier downgrades on repeated
  warn-cap crossings; the deterministic enforcement here is additive
- Per-session cap behavior — unchanged from Phase 1

### Migration

One-shot migration (extends `cost_caps_normalize.py`):
- `network.json::thresholds.dailySpendCapUsd` → `pod_defaults.budget.l1_breaker_usd`
  (the legacy "hard cap" was the L1 trip)
- `network.json::thresholds.dailySpendAlertUsd` → `pod_defaults.budget.daily_warn_usd`
- `network.json::thresholds.weeklySpendAlertUsd` → `pod_defaults.budget.pod_weekly_warn_usd`
- `network.json::thresholds.spendCapAction` →
  - If `"downgrade-tier"`: copy the existing `l1_breaker_usd` value to `tier_downgrade_usd`
    (operator wanted tier downgrade at that threshold; this preserves
    behavior, though new spec lets them split)
  - If `"suspend-bot"`: copy to `l2_breaker_usd`
  - If `"pause-crons"`: drop (covered by L1 in new spec)
  - If `"alert-only"`: keep `l1_breaker_usd` but log a warning that
    alert-only is no longer a distinct action (the alert fires from
    `daily_warn_usd`)

Strip clean after migration (same pattern as Phase 2).

### API

Extend the existing `POST /api/arbiter/bot-setup/<bot>` endpoint to
accept the new fields:

```
{
  "monthly_budget_usd":    float | null,
  "daily_warn_usd":        float | null,
  "weekly_warn_usd":       float | null,
  "tier_downgrade_usd":    float | null,
  "l1_breaker_usd":        float | null,
  "l2_breaker_usd":        float | null,
  "per_session_cap_usd":   float | null,
  "cache_retention":       "short" | "long" | null
}
```

New endpoint for pod defaults: `POST /api/arbiter/pod-defaults` with
the same body shape (minus per-bot-only fields), writing
`pod_defaults.budget.*`. `pod_weekly_warn_usd` is pod-only.

GET endpoints return both the explicit values AND the effective values
(after override → default → null resolution), so the UI can
distinguish "this is overridden" from "this is inherited."

### Enforcement implementation

Replace `_resolve_per_bot_cap` (single-threshold) with
`_resolve_per_bot_caps` returning a dict of all threshold values:

```python
def _resolve_per_bot_caps(bot_id: str, shared_dir: Path) -> dict[str, float | None]:
    return {
        "daily_warn":     ...,
        "weekly_warn":    ...,
        "tier_downgrade": ...,
        "l1_breaker":     ...,
        "l2_breaker":     ...,
        "per_session":    ...,
    }
```

`spend_alert.py` consults each threshold separately and fires the
matching action:
- Cross daily_warn → Telegram alert (dedup per day)
- Cross weekly_warn → Telegram alert (dedup per week)
- Cross tier_downgrade → `apply_tier_downgrade(bot)` (24h)
- Cross l1_breaker → `breakers_enforce.enforce_trip(bot, "cost", "l1")`
- Cross l2_breaker → `breakers_enforce.enforce_trip(bot, "cost", "l2")`

Each remediation tier has its own breaker file so the dedup state is
distinct (a tier-downgraded bot can still trip L1 later if spend
continues climbing).

## Implementation phases (post-spec)

### Evo conversational interface

Evo is the natural-language surface for users who don't have admin UI
access (every member-bot user, most personal-bot users). The cost cap
system must be **visible, explainable, and — within authority bounds
— actionable** through evo for those users.

#### User classes (mirrors the existing three-types memory)

| Class | Examples | What they need to do with caps |
|---|---|---|
| Pod operator | The pod admin (talks to evo on the primary bot) | Read + write anything, any bot |
| Personal-bot user | A family member with their own personal-bot | Read + write **their own bot**, with guardrails |
| Member-bot user | Slack/Telegram users talking to a team-bot | Read-only, **scoped to what's visible in chat with them** (i.e. "this bot's caps") |

#### Authority model per action

| Action | Pod operator | Personal-bot user (own bot) | Member-bot user (this bot) |
|---|---|---|---|
| Read current caps + effective values | ✓ | ✓ | ✓ |
| Read current spend vs caps + remediation state | ✓ | ✓ | ✓ |
| Set / raise warn caps | ✓ | ✓ | ✗ |
| Set / lower remediation thresholds (more conservative) | ✓ | ✓ | ✗ |
| Set / raise remediation thresholds (less conservative) | ✓ | ✓ within `2 × pod default`; above that requires pod-operator approval | ✗ |
| Set / raise per-session cap | ✓ | ✓ | ✓ if currently rejected on per-session limit (self-service unblock) |
| Lower per-session cap | ✓ | ✓ | ✗ |
| Reset L1 breaker | ✓ | ✓ | ✗ |
| Reset L2 breaker (manual-only by spec) | ✓ | ✗ (proposes to pod operator) | ✗ |
| Set / change `cache_retention` | ✓ | ✓ | ✗ |

Member-bot user's "set per-session cap" exception is the only write
they get — it's the **self-service unblock** for the common shape:
"My session got rejected, can I raise the cap and continue?" Capped
at `2 × pod default` per-session value to prevent abuse; further
raises require the pod operator.

Any write a user is NOT authorized for routes through evo as a
**proposal** (not a hard refusal) — the pod operator sees a Telegram
alert with one-tap approve/reject. Standard arbiter flow.

#### Evo tools (new + extended)

**New read tools:**

- `pod_state.cost_caps` — current cap values + effective (after
  inheritance) + pod defaults + which fields are overridden per-bot.
  Defaults to "the bot this conversation is on" when called from a
  non-primary bot's evo; pod operator can pass `bot_id` arg. Every
  threshold reports its own resolved value; there is no derivation
  layer to explain.

- `pod_state.cost_remediation_status` — current state of every
  remediation tier per-bot:
  - tier_downgrade: `{active: bool, since: timestamp, reverts_at: timestamp, original_model: str, current_model: str}`
  - l1_breaker: `{tripped: bool, tripped_at: timestamp, reverts_at: timestamp, reason: str}`
  - l2_breaker: `{tripped: bool, tripped_at: timestamp, reverts_at: null /* manual */, reason: str}`
  - per_session_breakers: `[{session_id, tripped_at, cap, spend_at_trigger}]` — list of currently-rejected sessions
  Same default-to-current-bot rule as above.

- `pod_state.cost_history` — today's + last-7-days spend per bot,
  tagged against each threshold (e.g. "Tuesday: $4.20, 84% of daily
  warn; no remediation"). Surfaces the *trajectory* so the user can
  see whether they're trending toward a breaker.

**New write tools:**

- `action.cost.set_cap` — set a single threshold field for a bot.
  Accepts `bot_id`, `field` (one of `daily_warn_usd`, `weekly_warn_usd`,
  `tier_downgrade_usd`, `l1_breaker_usd`, `l2_breaker_usd`,
  `per_session_cap_usd`, `monthly_budget_usd`, `cache_retention`),
  and `value`. Writes BE config. Authorization-checked per the table
  above; unauthorized writes route to proposal queue.

- `action.cost.clear_cap` — clear a single threshold (revert to pod
  default). Same authorization shape.

- `action.cost.reset_remediation` — manually reset one remediation
  tier for a bot. Args: `bot_id`, `level` (one of `tier_downgrade`,
  `l1_breaker`, `l2_breaker`). Pod-operator-only by default; L2 is
  the most sensitive (clears the manual-only safety lock).

- `action.cost.raise_session_cap_for_unblock` — convenience tool with
  member-bot-user authorization built in. Caller specifies the
  in-flight `session_id`; tool raises the per-session cap if-and-only-if
  the call is from that session's user, the session currently has a
  per-session breaker tripped, and the new value is within
  `2 × pod default`. Otherwise routes to proposal.

**Extended existing tools:**

- `pod_state.bots` — add a `cost_summary` block to each bot's row:
  `{today_spend_usd, monthly_to_date_spend_usd, next_threshold:
  {name, value, fraction_reached}, active_remediations: [...]}`. So
  any tool that walks pod state sees the cost picture without an
  extra fetch.

#### Conversational shapes (canonical examples)

Pod operator on primary bot:
> "Show me personal-bot's caps."
> — evo calls `pod_state.cost_caps(bot_id="personal-bot")`. Renders a
> table with each threshold + current vs effective vs pod default +
> override indicator.

Pod operator on primary bot:
> "Set personal-bot's daily hard cap to $10."
> — evo calls `action.cost.set_cap(bot_id="personal-bot",
> field="l1_breaker_usd", value=10)`. Confirms back the new ladder
> and notes anything that would violate the validation (e.g. "L1
> $10 is below current tier_downgrade $15 — should I lower
> tier_downgrade too?").

Personal-bot user on their own bot (e.g. security-bot):
> "Why was my last message ignored?"
> — evo calls `pod_state.cost_remediation_status` for security-bot.
> Sees the per-session breaker tripped. Replies: "Your session crossed
> security-bot's per-session $2 cost cap. You can keep going if I
> raise it for this session — should I?"
> — user: "yes, $5"
> — evo calls `action.cost.raise_session_cap_for_unblock(...)`. Tool
> verifies caller is the session's user, current breaker is tripped,
> $5 ≤ `2 × pod default $2.50`. Raises, clears the breaker, replies:
> "Done. Next turn will work."

Member-bot user (Slack DM to team-bot-a):
> "Why are you so slow today?"
> — evo (running on team-bot-a, talking to a member) calls
> `pod_state.cost_remediation_status`. Sees `tier_downgrade.active =
> true`. Replies: "team-bot-a is on a tier-3 model today (cheaper,
> slower) because spend hit the tier-downgrade threshold. It'll
> auto-revert at midnight. Sorry about that — let me know if you
> need to escalate."

Personal-bot user trying to escalate L2:
> "Reset security-bot's L2 breaker."
> — evo: "L2 breaker resets require the pod operator's approval — it's
> a manual-only safety lock. I'll send a notification now."
> — Routes to proposal queue with `audience: pod_operator`. The pod
> operator sees a Telegram alert; one-tap approve runs the reset.

#### Member-bot transparency rule

A member-bot user **must always be told** when their bot is in a
degraded mode (tier-downgrade or any breaker). Silent degradation is
the worst possible UX. Evo's default response to any user query
checks `pod_state.cost_remediation_status` and **prepends a one-line
status banner** to the response when any remediation is active:

> "(Heads up: I'm on a tier-3 model today — spend hit the
> tier-downgrade cap. Reverts at midnight. Continuing your request now.)"

> "(Heads up: my heartbeat is paused — I'm only answering live chat
> right now. Background tasks will resume at midnight or when the
> pod operator resets it.)"

> "(I'm in shutdown mode — only the pod operator can restart me. Your
> message won't be processed.)"

The banner is **off** by default in the user's normal flow until a
remediation is active; once any tier is active, every response leads
with the banner until the remediation clears.

#### Remediation push notifications

When a remediation tier trips, evo sends a proactive Telegram (or
chat-channel) notification to the appropriate audience:

| Trip | Audience | Channel |
|---|---|---|
| Tier downgrade | Pod operator | Telegram alert |
| L1 breaker | Pod operator + bot's primary user (if not the same) | Telegram alert + next chat turn banner |
| L2 breaker | Pod operator + bot's primary user + active member-bot users (last 24h) | Telegram alert + next chat turn banner |
| Per-session cap | Session's user (in-session message from OC plugin) | Already handled by OC |

Pod operator notifications are routed through the existing alerts
catalog (new event types: `cost.tier_downgrade_active`,
`cost.breaker_tripped`, `cost.l2_breaker_tripped`). Member-bot
banner is added to the conversational response next turn — no
separate push.

#### POD_CONDUCT injection

Add a section to `POD_CONDUCT.md` (the pod-wide system-prompt block
that injects into every session) explaining the cost cap ladder so
evo answers consistently across bots without needing per-bot
explanation in HEARTBEAT.md or AGENTS.md:

```
## Cost caps (operator-set, per-bot ladder)

This pod uses a graduated cost cap system per bot:
- daily_warn:        alert only
- weekly_warn:       alert only
- tier_downgrade:    auto-switch primary to a tier-3 model for the day
- l1_breaker:        heartbeat off, chat works
- l2_breaker:        gateway stopped, manual reset only
- per_session_cap:   reject next turn in session over this

When any remediation is active, prepend a one-line status banner to
your response so the user knows why behavior is degraded.

If a user runs into a remediation:
- Per-session cap rejection → offer to raise via
  action.cost.raise_session_cap_for_unblock (self-service for the
  session's user)
- L1 / tier-downgrade → explain, point at auto-revert time, offer to
  propose an early reset to the pod operator
- L2 → explain, route to pod operator (proposal-only)

Read current state via pod_state.cost_caps + pod_state.cost_remediation_status
before answering any spend / capacity / "why is the bot slow" question.
```

### Phase 4b (already on the plan)
Removes sandbox TunableKey duplicates + Customizations card pickers +
materializer sandbox-override fallback. No new behavior; clears the
two-editors-for-one-field problem. Standalone PR.

### Phase 5 — Schema + writers (no new UI)
- Add 4 new fields to BE config schema: `weekly_warn_usd`,
  `pod_weekly_warn_usd`, `tier_downgrade_usd`, `l2_breaker_usd`
- Add setters + getters
- Extend `/api/arbiter/bot-setup` to accept the new fields
- Add `POST /api/arbiter/pod-defaults` endpoint
- Validation: `l2 > l1 > tier_downgrade > daily_warn` ladder check
- Tests

### Phase 6 — Enforcement plumbing
- New `breakers_enforce.enforce_trip(bot, "cost", "l1" | "l2")` 2-arg form
- L2 implementation: `launchctl bootout` + alert
- `apply_tier_downgrade(bot)` — writes `agents.defaults.model.primary`
  to tier-3 model with 24h TTL
- `_resolve_per_bot_caps` returning all 6 threshold values
- `spend_alert.py` and `cost_watchdog.py` consult each tier separately
- Tests for each enforcement action

### Phase 7 — Canonical UI panel
- Build `renderCostCapsPanel(scope, values, pod_defaults)` JS function
- Mount on Settings → Bots → Cost & caps (replace current renderBotSetup
  cost section)
- Mount on Cost Optimization → Pod defaults section (replace POD-WIDE
  BUDGET + SPENDING CAPS panels)
- Per-bot tile chips become 3-chip read-only summary (tier/L1/L2)
- Tests

### Phase 8 — Migration + legacy deletion
- One-shot migration: network.json::thresholds.* → BE config
- Delete `spend_caps.py` enforcement-action path
- Delete legacy POST /api/spend-caps endpoint
- Strip `network.json::thresholds.*` after migration
- Tests

### Phase 9 — Evo conversational interface
- New read tools: `pod_state.cost_caps`,
  `pod_state.cost_remediation_status`, `pod_state.cost_history`
- New write tools: `action.cost.set_cap`, `action.cost.clear_cap`,
  `action.cost.reset_remediation`, `action.cost.raise_session_cap_for_unblock`
- Extend `pod_state.bots` with `cost_summary` block
- Authorization checks (pod_operator / personal-bot-user / member-bot-user)
- Unauthorized-write → proposal-queue routing
- Tests for each authorization shape + each new tool

### Phase 10 — Remediation transparency
- Auto-prepend status banner to evo responses when any remediation
  active (POD_CONDUCT.md injection drives the behavior; the actual
  state-read happens in the session_surface preamble)
- New alerts catalog events: `cost.tier_downgrade_active`,
  `cost.breaker_tripped`, `cost.l2_breaker_tripped`
- Routing rules: pod operator always; bot's primary user when their
  bot trips; active member-bot users when L2 trips on their bot
- Tests for catalog rendering + routing

## Open questions (already-answered defaults in brackets)

1. **Tier downgrade is automatic, not arbiter-routed** — Budget Hawk
   keeps proposing it on warn-repeat patterns, but the explicit
   threshold enforces immediately. [Confirmed.]
2. **L2 reverses ONLY by manual operator action.** No auto-reset
   because the spend pattern that hits L2 is a real problem that
   shouldn't silently restart at midnight. [Confirmed.]
3. **Validation rejects inverted ladder** — operator can't set
   tier_downgrade > L1. [Confirmed.]
4. **Pod-default override is sticky** — changing pod default doesn't
   cascade. [Confirmed.]
5. ~~Cost Optimization page per-bot rows = read-only chips with
   "Configure →" link to Settings.~~ **Superseded 2026-06-06**: the
   per-bot tile chips for caps were never built and aren't going to
   be — the canonical caps editor lives directly on Cost Optimization
   now (per-bot tab), making a read-only summary chip redundant.
6. ~~One canonical render function used in three places.~~
   **Superseded 2026-06-06**: two mounts (per-bot tab + POD tab on
   Cost Optimization). Settings page becomes a deep link.
7. **Monthly tolerance is alert-only** — does not derive any other
   threshold. Daily / weekly / L1 / L2 / tier-downgrade are all set
   explicitly. [Confirmed 2026-06-06.]
8. **Day boundary** rolls at midnight in the pod's local TZ, not UTC.
   [Confirmed 2026-06-06.]
