# Principle: Apps Minimize Per-Turn Context Cost

**Status:** load-bearing architectural principle (not a soft guideline).
**Adopted:** 2026-06-07, after Atlas's heartbeat session billed $1.39 for 8 no-op ticks on Haiku (173K tokens per tick, 0% cache reads — accumulated session history loaded with `heartbeat.isolatedSession: false` and `lightContext: false`).

---

## The principle, in three clauses

1. **Prefer cron over heartbeat when the task does not need an LLM.** Most "scan / classify / write" tasks are pure Python — they read sources, normalize, append to disk, emit a Signal. They do not need to enter a bot session at all. Adding an LLM to a cron-shaped task adds three things the cron didn't have: a system prompt that grows with every other installed app, a prompt-cache miss on every tick at typical heartbeat intervals, and a billable spend the operator did not budget for. If the manifest's `recursive_llm` block is empty AND no app script imports `bot_tool` / invokes `subagent` / shells to `openclaw_headless`, the right hook is a cron job, not heartbeat anchoring.

2. **When an LLM IS needed: prefer subagent invocation over riding the bot's main session.** The bot's main session accumulates conversation history, observation payloads, and per-app `bot_guidance` injections that have nothing to do with this specific app's narrow LLM step. A subagent (`invocation_mode: "subagent"`) runs against a clean, narrow context the app controls — typically 2–5K tokens of focused instructions and inputs versus 50K+ of inherited session state. Riding the main session is correct only when the app must actually see what the user has been saying to the bot (rare; usually triage and follow-up cases). Riding heartbeat to "save a roundtrip" while the main session keeps growing is the antipattern this principle exists to name.

3. **When heartbeat IS the right hook: budget the per-turn footprint.** Every byte an app adds to `bot_guidance`, INSTALLED_APPS.md, or AGENTS.md is paid by every other turn the bot runs — user prompts, scheduled actions, heartbeats — forever. Footprint targets:

   - `bot_guidance` ≤ 1024 bytes per app
   - INSTALLED_APPS.md entry ≤ 500 chars per app
   - Aggregate per-bot bootstrap injection (sum across all installed apps) ≤ 10 KB before warning, ≤ 25 KB before alert.

   The verifier emits Signals at info severity today, calibrating against real data; thresholds become enforced after one calibration window.

## What this implies in code

### Manifest authoring

The [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) cron-vs-heartbeat section explains the decision tree:

- No LLM call anywhere in the app's scheduled work → `crons:` entry, no `heartbeat_evidence` block.
- LLM call needed for a narrow step → `invocation_mode: "subagent"` (composes with [principle-apps-inherit-bot-llm](principle-apps-inherit-bot-llm.md) — the subagent still routes through the bot's LLM stack).
- LLM call must observe live bot state or user conversation → declare `heartbeat_evidence`, write `bot_guidance` to under 1 KB, justify the inheritance in the manifest's `objective`.

### Verifier checks (info-only, calibration phase)

[packages/analyzer/app_audit_structural.py](../packages/analyzer/app_audit_structural.py) emits four Signals when a manifest crosses a threshold:

| Signal type | Condition |
|---|---|
| `app_cron_eligible_used_heartbeat` | App declares `heartbeat_evidence` but no script imports a bot transport. |
| `app_no_invocation_mode_subagent` | App has CLI scripts with `recursive_llm` purposes but `invocation_mode != "subagent"`. |
| `app_bot_guidance_oversized` | `len(bot_guidance) > 1024` bytes for a single app. |
| `app_heartbeat_baseline_inflation` | Per-bot aggregate (`sum(bot_guidance) + sum(INSTALLED_APPS entries)`) > 10 KB. |

These are info-severity in the first calibration window — they exist to populate the measurement, not gate installs. The "App bootstrap footprint" chip on the bot detail page surfaces the numbers per-bot so the operator sees footprint growth before it becomes spend.

### Cost-profile honesty

[packages/analyzer/cost_profiles.py](../packages/analyzer/cost_profiles.py) no longer offers a "Performance" profile that sets `heartbeat.isolatedSession: false + lightContext: false`. That combination has no valid use case at heartbeat intervals ≥ 5 minutes (Anthropic prompt cache TTL), and labeling it "Performance" misleads. The profile is renamed `unrestricted-debug` with an explicit "negative savings at typical interval" expected-savings field. The `write_openclaw_cost_settings` preflight gate rejects the combination at the API boundary unless explicitly overridden.

### Per-bot measurement, surfaced

A new endpoint `GET /api/bot/app-bootstrap-footprint?bot=<id>` returns the same data the chip displays. The chip lives alongside the existing cost score on the bot detail page. Three numbers operators see:

- Bytes injected per turn from all installed apps
- Estimated tokens at the bot's current bootstrap model
- Estimated $/turn at a cache miss (the worst case the heartbeat hits today)

## Anti-patterns to grep for

- `"invocation_mode": "main"` in a manifest whose scripts have CLI scaffolding — the app is using the bot's main session when a subagent would be tighter.
- `heartbeat_evidence: {...}` in a manifest whose scripts do not import `bot_tool` / call `subagent` / shell to `openclaw` — the app could be a cron.
- `bot_guidance` blocks over 1024 bytes — the bot pays this every turn, including turns that have nothing to do with this app.
- INSTALLED_APPS.md entries over 500 chars — the per-app `description` + `objective` + `what_app_does` fields are templated into INSTALLED_APPS.md verbatim; trimming the manifest trims every bot's per-turn bootstrap.
- Multiple apps in one bot each with 800-byte `bot_guidance` blocks — composes to a hidden 4–8 KB always-on injection.

## What this principle is NOT

- **Not a ban on LLM-using apps.** Apps that genuinely need an LLM call should make it; the principle directs them toward `subagent` transport with narrow context, not away from LLMs entirely.
- **Not enforcement at install time (yet).** Verifier Signals are info-severity in the calibration window. After a few weeks of measurement, the largest-leverage check graduates to "warn" with a corresponding fix proposal; install-time rejection follows only if the data shows it's warranted.
- **Not about heartbeat as a feature.** Heartbeat is the right hook when the bot must observe live conversation state, surface time-sensitive nudges from accumulated context, or perform tasks that genuinely require knowing what the user has been doing in this bot session. The principle is that those cases are rarer than they look in current app design, not that the hook is wrong.
- **Not a substitute for the cost cap.** Per-bot `daily_cap_usd` ([principle-cost-cap-refuse-turn](principle-cost-cap-refuse-turn.md)) remains the backstop. This principle is upstream of the backstop — measure and shape the input cost so the cap doesn't have to fire.
- **Not retroactive across all installed apps simultaneously.** Existing apps move on their next substantive touch; new apps must be principle-aligned from day one.

## Why this matters

The Atlas 2026-06-07 incident exposed two layered costs, only one of which the cost watchdog caught:

- **Foreground (caught):** Atlas was in the "Performance" cost profile, so each heartbeat tick paid for full session-history rehydration with zero cache hit. The watchdog's `heartbeat_session_bloat` Signal fired at 8 ticks and $1.39. This is what the operator saw.
- **Background (uncaught):** Every Atlas app contributed `bot_guidance` (1.2 KB each) and an INSTALLED_APPS.md entry (~1.4 KB each) to *every* turn the bot runs, not just heartbeats. With four apps installed, that is ~10 KB / 2.5K tokens of always-on injection — never measured, never displayed, never bounded. A future Atlas with 12 apps at the same per-app footprint pays 30 KB / 7.5K tokens per turn, every turn, before doing any work.

The cost cap catches the *outcome* of unbounded bootstrap growth. This principle catches the *cause*. App developers slap a heartbeat anchor on something that "works" without thinking about who else is sharing the session or about the per-turn injection their manifest adds. The forge can't catch this from the manifest alone — a small `bot_guidance` block is fine; the accumulated total across N apps is what matters — so the gate has to be at the bot level, looking at the aggregate.

This is the same shape as [principle-instrument-outcomes-before-optimization](principle-instrument-outcomes-before-optimization.md): measure the actual production footprint before deciding what to enforce. The chip and the info-severity Signals are the instrumentation; thresholds get tuned against real numbers, not guesses.

## References

- Atlas incident 2026-06-07: 8 ticks × 173 K tokens × Haiku input = $1.39, `cache_read_tokens: 0` on every tick.
- [docs/principle-apps-inherit-bot-llm.md](principle-apps-inherit-bot-llm.md) — the LLM-stack inheritance principle that established the `subagent` transport this principle leans on.
- [docs/principle-cost-cap-refuse-turn.md](principle-cost-cap-refuse-turn.md) — the per-bot spend backstop downstream of this principle.
- [docs/principle-instrument-outcomes-before-optimization.md](principle-instrument-outcomes-before-optimization.md) — the methodological principle behind shipping measurement first and thresholds second.
- [docs/principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md) — the bar the new Signals' `human_title` / `details` payloads have to meet.
- [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) — the cron-vs-heartbeat decision tree this principle codifies.
- [packages/analyzer/cost_profiles.py](../packages/analyzer/cost_profiles.py) — the cost-profile rename and preflight gate.
- [packages/analyzer/app_audit_structural.py](../packages/analyzer/app_audit_structural.py) — the four new verifier checks.
