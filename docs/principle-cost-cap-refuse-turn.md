# Principle: Cost Cap Trips to a Refuse-Turn Sentinel, Not Silent Overage

**Status:** load-bearing safety principle (not a soft guideline).
**Adopted:** 2026-05-31, consolidating the rule already shipped in the #1483 safety-net sprint and documented in [product-vision.md](product-vision.md).

> **Implementation status: partial — blocked on upstream OpenClaw (2026-06-11).**
> The *recurring*-spend half of this principle is shipped and enforcing: the
> per-bot `daily_cap_usd` breaker (PR #1483) disables heartbeat and routes
> subsequent turns to the refuse-turn path, and the forge dispatch path has a
> pre-dispatch worst-case cost projection that refuses a dispatch before any
> tokens are billed (`forge_cost_guard.evaluate_pre_dispatch`, Guard A).
>
> The **single-turn spend veto is not implemented**, and cannot be from the
> plugin layer as it stands. OpenClaw's `before_model_resolve` hook lets a
> plugin *choose a model* (so the plugin can **downgrade** a turn to a cheaper
> tier) but provides no way to **refuse / abort a turn pre-spend** — there is no
> `cancel_turn` / refuse verdict the hook can return, and every cost detector
> reads the ledger only *after* the API response is recorded. So a single
> expensive turn (the 2026-06-03 `$33.65` forge bomb) is billed before any
> Evolve control can intervene. A full refuse-turn sentinel requires an upstream
> OpenClaw capability (a pre-spend turn-abort hook, e.g. a `cancel_turn` return
> type on `before_model_resolve`, or a locally-resolving "cap reached" stub
> model). See the post-mortem
> `incident-post-mortem-2026-06-03-cost-breaker-gaps.md`
> (Structural gap #1, recommendation #3) for the analysis, and roadmap row 3.2
> in `roadmap-80-to-100-2026-06-09.md` for
> tracking.
>
> **Upstream dependency:** OpenClaw lacks a pre-spend turn-abort hook (plugins
> can only downgrade, not refuse). Tracking issue:
> [openclaw/openclaw#92296](https://github.com/openclaw/openclaw/issues/92296)
> ("before_model_resolve can downgrade a turn but cannot refuse it — no
> pre-dispatch cost/cancel hook") — **filed and tracked as of 2026-06-11.** The
> feature stays blocked-on-upstream until OpenClaw ships the hook.

---

## The principle, in two clauses

1. **When a bot's configured daily cost cap is exceeded, the pod actively refuses further LLM calls — it does not log a warning and keep spending.** The cost breaker disables heartbeat sessions and routes new gateway calls to a `refuse-turn` sentinel that returns a fixed "spending cap reached" response instead of dispatching to the upstream provider. The breaker is automatic; no operator intervention required to trip it.

2. **The breaker is a hard ceiling, not a soft suggestion.** Once tripped, the bot stops billing additional tokens. The operator is notified (via the alert channel) with the trip event, the configured cap, the day's spend, and the path to lift or raise the cap. The bot resumes normal operation on the next day boundary or on operator override — never silently because spend looked OK in a later sampling window.

## What this implies in code

Practical translation across the codebase:

### The breaker disables heartbeat, not just user-facing chat

Heartbeat sessions are the largest non-interactive consumer in the pod. When the cap trips, heartbeats are the first thing to stop — otherwise a misconfigured bot can blow through a cap repeatedly via background work. The breaker disables both heartbeat AND user-facing calls; the cap is about the bot's total spend, not just the visible part.

Reference impl: PR #1483 (safety-net sprint) — the per-bot `daily_cap_usd` field auto-trips an L1 cost breaker that actually disables heartbeat (an earlier iteration only logged, which is the bug this principle codifies).

### Refuse-turn returns a real response, not a network error

When a turn is refused, the gateway returns a structured "spending cap reached" response that the bot's client sees as a normal turn outcome — not a 5xx, not a timeout, not silence. This lets bot-side code log the refusal and surface it to the user (if the channel allows) rather than appearing as a runtime failure.

### Caps are per-bot, configured, and visible

Every bot has a `daily_cap_usd` setting visible in the admin UI (Cost Optimization tile row). The cap is set explicitly per bot, not inherited from a pod default that operators forget exists. Tiles show current spend vs cap, so the operator can see how close any bot is to tripping.

### Tripping a breaker is an Alert-grade event

The cost-breaker trip uses `⚡` per [operator-message-style.md](operator-message-style.md) — system-state change, distinct from a generic critical alert. The message names the bot, the cap, the day's spend, and the path to raise or override the cap.

## Anti-patterns to grep for

These are violations:

- Cost monitors that log "over budget" and continue dispatching turns
- Daily caps that warn but never enforce
- Breaker code that disables only user-facing channels and lets heartbeat keep running
- Refuse-turn paths that return a 5xx / network error instead of a structured "cap reached" response
- Pod-level cost caps with no per-bot visibility (the operator can't tell which bot is the problem)
- "Trip on next sampling window" delays that allow continued spend during the lag

## What this principle is NOT

- **Not a ban on warnings before tripping.** Soft alerts at 80% / 90% / 100% of cap are good operator hygiene. The principle is that 100% means stop, not "warn harder."
- **Not a demand for a single global cap.** Per-bot caps are the right granularity; pod-wide caps are a different control and can coexist.
- **Not retroactive to all breakers.** Other breakers (security, exec, audit) have their own trip semantics. The principle is specifically about cost — the failure mode it prevents is silent financial overage.

## Why this matters

Cost is the failure mode where "almost stopping" is worse than "definitely stopping." A bot running 50% over its cap because the breaker is soft burns real money every minute it's not enforced. The May 2026 cost-alerting blackout (4× spend day, pod-wide, unalerted) is the canonical cautionary case — multiple soft signals all degraded together and the operator only noticed via the bank statement. The hard refuse-turn sentinel is the architectural answer: no clever reasoning, no graceful degradation, just stop billing.

This is also the operator's contract with Evolve. Every product description that says "Evolve protects against runaway spend" is backstopped by this principle. A breaker that doesn't actually stop spend turns that contract into a lie.

## References

- [product-vision.md](product-vision.md) §"Make them safe and affordable" — the operator-facing promise
- [operator-message-style.md](operator-message-style.md) §"Headers" — `⚡` emoji is reserved for breaker-trip events
- PR #1483 — the safety-net sprint that made the breaker actually enforce
- `docs/incident-cost-alerting-blackout-2026-05-20.md` — the cautionary incident
- `project_safety_nets_shipped_2026_05_23` — operational summary of the breaker landing
