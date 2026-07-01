# Post-mortem: every cost breaker missed the 2026-06-03 team-bot-c forge-install spend bomb

**Incident:** `team-bot-c` forge install at 07:36:11 UTC billed one round-trip to Sonnet 4.6 with 8.82M cache-write tokens for **$33.65**.
**Receipt:** `cost_events-2026-06-03.jsonl` (session `8226b8d4…`, model `claude-sonnet-4-6`, trigger_kind `forge`, cache_state `invalidated`).
**Question:** PR #1483 (safety-net sprint), PR #1639 (tier-cascade Phase 1), and the cost-watchdog catalog all shipped specifically to prevent runaway single-turn cost. Why was none of them able to stop $33.65 on a single API call?

---

## Timeline of breakers (UTC)

| Time | Event | Result |
|---|---|---|
| 02:02:21 | First forge turn — warm cache, $2.78 | Spend within tolerance |
| 07:19:33 | `budget_hawk:warn_cap_crossed` fires | Observational only — no enforcement |
| **07:36:11.324** | **The $33.65 turn lands** | **Already billed** |
| 07:39:02 | `spend_alert:cost_burst` fires | Post-bill signal |
| 08:09:57 | `cascade_audit:runaway_rate_tripped` Signal | In-session ModelRouter cap tripped *during* the session — but post-API-response, so no in-flight effect |
| 08:27:56 | `cost_watchdog:daily_spend_high` fires (hourly cron) | Post-bill signal |
| ~09:01 | Operator manually sets `daily_cap_usd: 5` | First time team-bot-c had a cap |
| 09:03:06 | `spend_alert` auto-trips L1 cost breaker | 1h 27m after the bill |

Net: **every safety net was reactive.** The expensive turn was unconditionally dispatched.

---

## Per-breaker findings

### 1. PR #1483 per-bot `daily_cap_usd` auto-trip — never armed

- `network.json::bots.team-bot-c.daily_cap_usd` was **unset** at 07:36 UTC. The cap was added by the operator only after the bill arrived (~09:01 UTC).
- Even if `daily_cap_usd: 10` had been set in advance, the trip path goes: `spend_alert` (cron) reads `metrics/<date>/team-bot-c.json` → calls `spend_caps.write_enforcement_flag` → `ModelRouter.isSpendCapForced()` returns true on the **next** turn's `before_model_resolve`. There is **no pre-API-call cost projection** anywhere on this path. A single >$10 turn slips through cleanly because metrics roll up post-cost_event.
- **What would have helped:** nothing about this breaker's design can stop a single $33 turn. It is a "stop the next turn" cap, not a "stop this turn" cap.

### 2. ModelRouter runaway-rate cap — fired in-session but post-bill

- Code at [packages/plugin/src/observer/ModelRouter.ts:1283-1342](packages/plugin/src/observer/ModelRouter.ts:1283). The cap accumulates per-session cost via `recordTurnCost(sessionKey, costUsd, ts)`. That call lives in [TurnObserver.ts:1870](packages/plugin/src/observer/TurnObserver.ts:1870) under the `llm_output` hook — which fires *after* the API response is back.
- `checkRunawayRate` runs on the *next* turn's `before_model_resolve`. Once tripped, `isRunawayTripped` returns true and `_safetyNetDowngradeModel` swaps to tier3 for the rest of the session.
- The 08:09 `runaway_rate_tripped` Signal references session `8226b8d4…` — the same session as the $33 turn — which means the cap *did* trip during the session. But the trip records *after* the cost event the trip is reacting to. For a forge install session that completes after one expensive turn, this is exactly equivalent to no cap at all.
- **What would have helped:** the runaway-rate cap protects N+1, never N. To catch team-bot-c-shape, the same threshold logic would need to run pre-API-call from a *projected* cost (prompt length × per-token rate + expected output ceiling). The infrastructure to do that doesn't exist.

### 3. Heartbeat-session-bloat detector — not applicable

- `detect_heartbeat_session_bloat` in [cost_watchdog.py:2282](packages/analyzer/cost_watchdog.py:2282) filters on `trigger_kind == "heartbeat"`. The team-bot-c event was `trigger_kind: "forge"`. Detector skipped the session entirely.
- **What would have helped:** generalizing the bloat detector to non-heartbeat sessions (forge installs, exec-approval cascades, cron-app sessions) — but it's still post-hoc; it would log a Signal, not prevent the bill.

### 4. 30-min exec-approval timeout — not applicable

- Forge installs are not exec-approval flows. The timeout protects sessions where OC's `tools.exec` approval queue stalls. Team-bot-c's session ran cleanly to completion.

### 5. cost_watchdog catalog dispatch — fired late

- `details.catalog_event` is the channel for cost_watchdog to feed `cost_watchdog:daily_spend_high` into the per-bot daily cap breaker.
- The hourly cron at 08:27 produced a `daily_spend_high` Signal — 51 minutes post-bill. The Signal carries `catalog_event: cost.daily_spend_high` and feeds the cap-tripper, but only if a cap is configured. None was.

### 6. Anomaly + dangerous-combo detector — both miss this shape

- `DangerousComboDetector` ([packages/plugin/src/observer/DangerousComboDetector.ts](packages/plugin/src/observer/DangerousComboDetector.ts)) requires **all four** of: `trigger_kind ∈ {heartbeat, cron_app}`, `tier_used == tier1`, `tier_chosen_by == cascade`, `context_tokens > 100K`.
- Team-bot-c's turn: `trigger_kind=forge` (mismatch), `model=sonnet-4-6` ≈ tier2 (mismatch), forge picks Sonnet by config not cascade (mismatch). Three of four features mismatched; detector returned `matched=false`. Also: even when matched, it only emits a Signal — it has no enforcement path.
- Anomaly detector (per `project_tier_cascade_phase1_2026_05_27`) is **observe-only Phase 1**; no enforcement is wired.

### 7. Operator confirmation gates on forge install — none

- Trace: `evo/handlers/install.py::_run` → `create_install_job` → `_dispatch_forge_job_async` → silent background execution. No cost estimate, no "are you sure," no preview of expected token budget. Same shape from the admin UI path.
- The "Preflight" surface checks for build_blocker dependencies (storage, integration). **No cost preflight.**

---

## Fired / didn't fire / fired too late

| Breaker | Active for team-bot-c at 07:36? | Could it prevent the turn? | Outcome |
|---|---|---|---|
| `daily_cap_usd` auto-trip | No (unset) | No (next-turn only) | Eventually tripped at 09:03, 1h 27m late |
| ModelRouter runaway-rate cap | Yes (default config) | No (post-`llm_output`) | Tripped during session, billed anyway |
| Heartbeat bloat detector | N/A | No (trigger_kind mismatch) | Skipped |
| Exec-approval timeout | N/A | N/A | N/A |
| cost_watchdog hourly sweep | Yes | No (post-cost_event) | Fired 51 min late |
| DangerousComboDetector | Yes | No (Signal-only; also no match) | Did not fire |
| Anomaly detector | Phase 1 observe-only | No | N/A |
| Forge install preflight | Yes | Could have | No cost gate exists |

**All eight breakers were either reactive, inapplicable, or unarmed.** The only *preventive* control available — operator confirmation at install time — does not exist.

---

## Structural gaps

1. **No hook between "OC turn prep complete" and "API request sent."** Confirmed by reading `before_model_resolve` (used for model selection) and `llm_output` (fires after the response). The `before_model_resolve` event carries `event.prompt` but the resolver returns a model name, not a "refuse this turn" verdict. There is no `refuse_turn` sentinel path in the plugin code, despite the principle document. The closest thing is `_safetyNetDowngradeModel` — which *swaps* a model, it doesn't *refuse*.
2. **All cost detectors are post-`llm_output`.** `recordTurnCost`, `spend_alert`, `cost_watchdog`, every Signal producer reads cost from the ledger after the API response is recorded. The cost ledger only knows about turns that completed.
3. **Forge installs are exempt from heartbeat-class protections.** Bloat detection, automation-dominance, override-violation — all key on `trigger_kind=heartbeat`. Forge installs are background-pure but get treated like user-initiated turns by every detector that would care.
4. **`refuse-turn` principle is documented, not implemented.** `docs/principle-cost-cap-refuse-turn.md` describes a "refuse-turn sentinel" that returns "spending cap reached" instead of dispatching. The plugin has no such sentinel — only the tier3 downgrade. A tier-3 downgrade is meaningless against a single expensive turn that's already been queued.

---

## Recommendations

Each recommendation marked **[plugin]** is blocked by OC's hook surface (limited to what `before_model_resolve` can return) and needs either upstream change or a Python gateway shim. **[admin]** is tractable inside Evolve.

1. **[admin] Forge install cost projection + operator confirm.** Before `_dispatch_forge_job_async` runs, project the install's likely cost from the manifest spec size + expected tool-use roundtrips. Render to the operator in the admin UI with a confirm button. Target: $33 → $0 on uninspected installs. Blocking work for v1.1.
2. **[admin] Mandatory `daily_cap_usd` default on every bot.** Today the cap is opt-in per bot; `network.json::bots.team-bot-c` had no cap. Make the cap **non-optional** at deploy time (`ensure_pod_perms` or `deploy.py::_reconcile_caps`) with a conservative default ($10/day) the operator must explicitly raise. The cap still won't stop the *current* turn, but it bounds the worst-case while pre-API-call enforcement is missing.
3. **[plugin] Project pre-API-call cost in `before_model_resolve`.** OC's `before_model_resolve` event already carries `event.prompt` and the session's accumulated context. Add a projected-cost computation (input_tokens × per-tier-rate + expected_output_ceiling). When projected_cost + session_spend > runaway cap OR + day_spend > daily cap, return the *refuse-turn sentinel model* — which short-circuits to a structured response without dispatching to the upstream API. This is the only mechanism that can prevent a single-turn $33 bomb. Upstream OC needs a `cancel_turn` return type on `before_model_resolve`, or we implement a stub model in the plugin that resolves locally to "cap reached." Filed upstream: [openclaw/openclaw#92296](https://github.com/openclaw/openclaw/issues/92296).
4. **[admin] Forge install runs in a tier-3-by-default session.** The session that performs the install should not inherit the bot's primary model. Default to tier-3 (Haiku) with an explicit operator override required to escalate. For most install workloads this is sufficient.
5. **[plugin] Extend DangerousComboDetector to forge + cron_app + exec_approval origins** and lower the tier1-only restriction to "tier ≥ tier2 with cache_write_tokens > 5M." The pattern (background-pure + large-context + autonomous-tier-pick) generalizes; team-bot-c's turn matched the pattern's spirit and the detector's strict feature list excluded it. Still Signal-only, but adds an alert at minimum.

**Order of work:** (1) and (2) are tractable today and would have prevented this incident on their own. (4) is a small config change with high ROI. (3) is the architecturally correct fix and probably needs an OC upstream issue first. (5) is cleanup that closes a near-miss class.

---

## What this incident teaches

The 2026-05-21 circuit-breaker spec calls out the right invariant — *act first, notify second.* The shipped implementation honored that for *recurring* spend patterns (heartbeat cadence, hourly aggregates), but the **single-turn spend bomb** was outside its threat model. Every detector relies on the cost ledger; the cost ledger is only written post-API-response. The only place left to intercept is the plugin, and the plugin has no veto path.

The principle says "refuse-turn sentinel." The code has tier-downgrade. Those are not the same thing.
