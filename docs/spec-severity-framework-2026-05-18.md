# Severity Framework — Vectors, Magnitudes, Fix-Risk (2026-05-18)

Status: **draft** (design locked in conversation 2026-05-18; module + reference cards land first, producer retrofit follows).

**What this is.** A shared severity framework for Signals and Proposals. Replaces the ad-hoc per-producer severity policy (`signals/producer_severity.py`) with a structured (vector × magnitude) tagging system plus a separate fix-risk attribute on remediations and proposal actions. Composes into a derived **priority score** that the Home narrative + the Alerts page can use for ordering and the tier-c authority gate can use (alongside decidability) for auto-fix eligibility.

**Why this exists.** Today every producer picks `info | warn | alert` on its own. Two problems:

1. **One producer's "warn" is another's "alert."** Security findings span "audit advisory" to "token leaked at scale" — both can land as `warn` today. Operators can't sort the loud-but-not-urgent away from the urgent-but-quiet.
2. **No comparison across vectors.** A cost spike at $200/day is bigger than a permissive-tool advisory; both currently surface at the same severity bucket. There's no shared scale.

**Design constraint.** Producers stay in their domain. The cost monitor knows what a cost finding's magnitude is; the security warden knows security; the operations watchdog knows operations. Cross-vector calibration happens through anchored examples in this doc, *not* by asking a single rater to learn every domain. The composed priority score is derived, never producer-supplied directly.

**Relationship to other specs.**
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — defines the Signal store. This spec adds two fields to Signal.details and the Proposal action — `vector`, `magnitude` on the finding side, `fix_risk` on the action side. No schema migration required (Signal.details is `dict[str, Any]`).
- The existing `severity: info | warn | alert` field stays as a *derived* render hint — info ≈ magnitude 0-1, warn ≈ magnitude 2-3, alert ≈ magnitude 4 — so non-migrated producers keep working and the UI still has the bucket it knows how to render.

---

## 1. Three axes, not one

| Axis | Question it answers | Lives on |
|---|---|---|
| **Severity** = vector × magnitude | How bad if this isn't addressed? | The finding (Signal, Proposal) |
| **Decidability** | Is there a clear right answer? | The remediation or proposal action |
| **Fix-risk** | How bad if the fix itself goes sideways? | The remediation or proposal action |

These are **independent**. A critical security issue can be highly severe + highly decidable + low fix-risk (rotate token) — and still get confirmed in tier (c) because of the safety-rail rule that security-tagged actions always require explicit approval. A low-severity hygiene issue can be highly decidable + low fix-risk and auto-fire all the way down to tier (b).

The Home narrative uses **severity** for ordering. The tier-c gate uses **decidability + fix-risk**. The safety rails read tags on the finding (security_tag, judgment_required, novel_producer) regardless of either.

---

## 2. Vectors and the 0-4 magnitude scale

Four vectors. Anchored ordinal 0-4 per vector. Producers cite the closest anchor when emitting.

### 2.1 Security

| Magnitude | Anchor | Examples |
|---|---|---|
| **0** | Advisory only, no real risk | Audit notes a plugin tool reachable under permissive policy but the tool itself is read-only |
| **1** | Theoretical exposure, mitigations in place | An exec policy is `scoped` rather than `minimal` for a coding agent. SSH keys present but in a properly-permissioned dir |
| **2** | Real exposure, scoped to one bot or contained surface | A bot's `.zshrc` is unreadable (auth state may have leaked into the user's shell init). A specific MCP server has `allowlist: ["*"]` but only that bot exposes it |
| **3** | Active exploitation possible, OR token leak with limited scope | An OAuth token for a bot's GitHub integration is present in a checked-in file; partial-scope token |
| **4** | Catastrophic — token leaked at scale, auth bypass, data exfil, ongoing intrusion | Anthropic API key leaked in chat logs, exposed for >1h. Multi-bot privilege escalation. Confirmed unauthorized access |

### 2.2 Cost

| Magnitude | Anchor | Examples |
|---|---|---|
| **0** | Negligible (<$1/day impact) | Cache hit rate dipped 2% one day. One session burned $0.30 extra |
| **1** | Trending up but contained | A bot's daily spend ticked up to $3 from a $2 baseline. Heartbeat firing on primary tier occasionally |
| **2** | Real overrun: $5-25/day OR trajectory hits cap in >14d | A bot is consistently at $15/day vs a $5 baseline. Cache invalidation 20-40% over a week |
| **3** | Material overrun: $25-100/day OR cap hit in 7-14d | Bot at $60/day vs $20 baseline. Embedding 429 storm burning fallback API calls |
| **4** | Severe overrun: $100+/day OR cap hit now | Pod-wide spend running 3x expected. Spend cap actively gating turns |

### 2.3 Operations

| Magnitude | Anchor | Examples |
|---|---|---|
| **0** | Cosmetic, no functional impact | An optional plugin is at version N-1. Log file rotation is one cycle behind |
| **1** | Single bot degraded but functioning | One bot's metrics file is 30 min stale. One MCP server's optional capability is unavailable |
| **2** | Single bot down OR pod-wide degraded | A member bot's gateway has been unreachable for 15 min. Pod's `cron_overactive` firing for one cron |
| **3** | Pod-wide degraded OR multi-bot down | Two member bots' gateways down simultaneously. Heal can't reach any bot. Update_watcher repeatedly failing |
| **4** | Pod-wide hard outage | Admin UI itself unreachable. Every bot offline. Shared dir read-only |

### 2.4 Quality

| Magnitude | Anchor | Examples |
|---|---|---|
| **0** | Style nit | A formatting inconsistency. A renaming opportunity |
| **1** | Minor UX | Classifier accuracy dipped 3%. One persona drifted slightly |
| **2** | Noticeable UX or quality regression | Cache invalidation hurting cache hit rate by 20%+. A bot is consistently misclassifying a session category |
| **3** | Major regression, users actively affected | A bot keeps generating broken tool calls. Routing chain falling back every turn |
| **4** | Catastrophic — bot is actively harmful or broken | A bot is sending the wrong response to the wrong user. A bot's persona has flipped to abusive |

---

## 3. Fix-risk: low / medium / high

Attached to **the action**, not the finding. Three levels — keep producers from agonizing over fine gradations.

| Level | Property | Examples |
|---|---|---|
| **low** | Reversible, bounded blast radius, well-trodden path | Snooze a signal. Dismiss a signal. Kickstart a gateway. Rotate an expired token. Apply a hygiene proposal that removes an unused plugin. Auto-snooze duplicate audit signals |
| **medium** | Reversible but harder, OR broader blast radius | Apply a config-patch proposal. Flip an exec policy from `scoped` to `minimal`. Change an MCP server's allowlist. Disable a plugin entry |
| **high** | Hard to undo, OR large blast radius, OR could cascade | Reinstall a bot from scratch. Delete data. Migrate a schema. Apply any security-policy change. Anything `risk_tag: high` from the proposal pipeline |

**Tier (c) never auto-fires medium or high.** A clear-answer fix that's also high-risk still gets a click. This is the safety floor.

---

## 4. Priority composition

The single number the UI orders by. Derived, never producer-supplied:

```
priority = magnitude
         * impact_multiplier      # pod=1.5, host=1.4, integration=1.3, bot=1.0
         * urgency_multiplier     # active_outage × 1.3, self_resolving × 0.7
         * pod_weight[vector]     # operator-tunable; default 1.0
```

Range: ~0.0 to ~15.6 (4 × 1.5 × 1.3 × 2.0 maximum).

**UI thresholds:**

| Priority | Where it renders |
|---|---|
| ≥ 7.0 | Leads the narrative (top of big-bucket) |
| 3.0 – 6.9 | In the narrative (big-bucket, ordered by priority desc) |
| < 3.0 | Smaller stuff (collapsed by default) |

Calibration: the realistic worst case *without* operator weight tuning — magnitude 4 × pod (1.5) × active_outage (1.3) — comes to 7.8. The lead threshold sits just below so a pod-wide critical with active outage qualifies. Finer urgency lives in operator-tunable weights (`severity_weights[security] = 1.5` lets a security-paranoid pod escalate single-bot security:4 findings into the lead bucket).

The score is an *ordering primitive*. Never shown bare. The breakdown — "security:3, pod-wide, active = 5.85" — is available in a tooltip / debug overlay; the operator sees the human-readable title and the vector tag.

---

## 5. Operator-tunable pod weights

Per-pod `network.json::severity_weights` lets the operator boost vectors they care more about:

```json
{
  "severity_weights": {
    "security": 1.5,
    "cost": 0.8,
    "operations": 1.0,
    "quality": 1.0
  }
}
```

A security-conscious pod (or a startup that's paranoid pre-launch) sets `security: 1.5`. A pod tight on budget sets `cost: 1.3`. Defaults are all `1.0` — neutral.

This is the right place for opinion. Anchors (the magnitude tables above) are universal; weights are local.

---

## 6. Feedback teaches calibration over time

`signals/feedback.jsonl` already records dismissals with verdicts:

- `false_positive` — the producer shouldn't have fired
- `bad_inference` — fired correctly but the suggested fix is wrong
- `not_actionable` — fine to know about, nothing to do

After enough `not_actionable` dismissals at magnitude=2 from one producer-type pair, the system can suggest the producer lower its default magnitude for that type. Visible, opt-in, auditable — never silent retraining.

This is a Phase 2 concern. Phase 1 is: ship the framework, retrofit a few producers, see what surfaces.

---

## 7. Default magnitude when a producer doesn't tag

Backward compat for producers that haven't been retrofitted yet:

| Existing `severity` | Inferred magnitude |
|---|---|
| `info` | 1 |
| `warn` | 2 |
| `alert` | 4 |

The inferred vector falls back to a producer-default table (security_warden → security, cost_watchdog → cost, etc.). The migration plan: retrofit producers in priority order, falling through to the inferred values until they're each tagged explicitly.

---

## 8. What lands first (Phase 1)

1. `packages/analyzer/severity.py` — the framework module: vector/magnitude types, anchor cards (importable as data), `compose_priority()`, default-inference helpers.
2. This spec doc (you're reading it).
3. Unit tests for the framework.

Phase 2 (separate work):
- Retrofit audit, cost_watchdog, sysadmin_watchdog with explicit `vector` + `magnitude` tagging.
- Wire the Home narrative + the Alerts page to read priority for ordering.
- Tier-c authority gate using decidability + fix-risk classifiers.
- Operator UI for tuning `severity_weights`.

Producer retrofit is *gradual*. The default-inference table means a pod with no retrofitted producers still gets a working priority ordering — just one that reads existing `severity` as a proxy.
