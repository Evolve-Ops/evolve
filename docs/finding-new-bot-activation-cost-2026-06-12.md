# Finding: New-bot activation cost — the "cost-to-first-value" gap (2026-06-12)

**Status:** diagnosis for operator design-sync (no code shipped)
**Companion to:** [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §U1 (activation). This is the **cost dimension** of U1: the activation thesis targets *time*-to-first-value < 24h; this finding argues it must be paired with a bounded *cost*-to-first-value.
**Trigger:** a `cost.daily_threshold` alert on the freshly-created bot `ledger` ($5.65 crossing the $5 alert at 01:27 PT, 2026-06-12).

---

## 1. TL;DR

A brand-new bot (`ledger`, created via the conversational wizard on 2026-06-11 with the 6-app project-manager starter pack) spent **$30.26 across its first two calendar days** (2026-06-11 + 06-12) — entirely on **building and then auditing its own starter apps**. It has had **zero user conversations and delivered zero briefings** to date. The $5.65 alert that triggered this finding is a *snapshot at the moment the alert fired*; the true totals are higher (below).

- **Verdict on recurrence: one-time setup tax, NOT a recurring burn.** ~82% of the spend is forge app-builds (one-time, per app create/modify); the rest is the first audit of each new app (tier-3 cadence is *monthly* — confirmed in `ledger`'s live `pod_config.json` — so it amortizes to ~$0.18/day). Steady-state idle cost will converge to the mature-bot range of **~$1–2/day**.
- **The brief's premise is outdated and is corrected here.** `ledger` *does* have a daily hard cap (`$10`), and there *is* a pod default (`$20`) — caps moved from `network.json` to `better-engine-config.json` in the 2026-06 cost-cap normalization. The cap even **tripped** (twice). The real gap is that **the cap cannot govern provisioning spend**: forge builds and the audit pipeline run via admin-side daemons, *outside* the heartbeat/background path the L1 cost breaker pauses.
- **Biggest single lever:** the 8 forge builds ran on **Opus** (~$3/build). Routing provisioning builds + first-audits to **Sonnet** would cut the dominant cost ~4–5× with no quality loss for templated starter-pack generation.

---

## 2. What happened (timeline, live pod)

All figures from the authoritative per-turn cost ledger
(`{shared_dir}/ledger/turns/turns-YYYY-MM-DD.jsonl`, the same source the
Usage page and `spend_alert` read). Daily spend is accounted by **UTC**
date (`ts[:10]`); wall-clock below is pod-local **PT** for readability.

| When (PT) | UTC date | What ran | Sessions | Spend | Model |
|---|---|---|---|---|---|
| 06-11 02:26–02:43 | 06-11 | Wizard provisioning: **6 forge app-builds** (starter pack) | 6 | **$19.82** | Opus |
| 06-11 21:22–21:26 | 06-12 | **Overnight audit wave** — tier-2/tier-3 app-audits + triage of the 6 new apps | 9 | $4.40 | Opus |
| 06-12 01:25–01:46 | 06-12 | Telegram channel connected → **briefing auto-activation**: 2 more forge builds (the $5 alert) | 2 | $4.89 | Opus |
| 06-12 03:07 | 06-12 | Follow-on audit + triage | 2 | $1.14 | Opus |
| **Total** | | | **19** | **$30.26** | |

**Per-UTC-day:** 06-11 = **$19.82**; 06-12 = **$10.44** (still the current day at time of writing).

**Why the alert numbers are lower than the day totals:** the `spend-ledger-*.flag` `amount_usd` is the *running total at the instant the alert crossed $5*, not end-of-day. 06-11 alerted at $14.51 (true day total $19.82); 06-12 alerted at $5.65 (true day total $10.44). Reading the flag alone understates the spend by ~30–40%.

Every one of the 19 sessions is a **single Opus turn**, `source=user channel=unknown` — the signature of headless, admin-orchestrated forge/audit invocations, not interactive chat. There is **no wizard-conversation cost and no briefing-delivery cost on `ledger`** (the wizard runs on the `evo` bot; the 07:00 briefing had not fired as of the snapshot, and the channel was only paired at 09:46 PT on 06-12).

---

## 3. Attribution by category

| Category | Spend | Share | Model tier | Recurrence |
|---|---|---|---|---|
| **Forge app-builds** — 8 builds (6 starter-pack + 2 briefing-activation) | **$24.71** | **82%** | Opus | **One-time** — only on app create / modify |
| **App-audits (tier-2/tier-3 + triage)** — first audit of the new apps | **$5.54** | **18%** | Opus | One-time at provisioning; then **monthly** (≈ $0.18/day amortized) |
| Wizard creation conversation | $0 *(on `ledger`)* | — | — | Billed to the `evo` bot, not the new bot |
| Briefing **delivery** | $0 *(not yet fired)* | — | — | Recurring daily once active — **this is the actual value** |
| **Total** | **$30.26** | | | |

Forge per-build costs ranged **$1.25–$4.96** (avg ~$3.09), driven almost
entirely by cache-write tokens (each build re-primes a large context, then
emits the app files in one Opus turn).

---

## 4. Why the existing cap did not stop it (the mechanism)

The brief stated `ledger` had no cap and no pod default. That was true of
`network.json`, but **caps moved to `better-engine-config.json`** in the
2026-06 cost-cap normalization (Phase 4). Current live state:

```
bots.ledger.budget.per_bot_daily_hard_usd      = 10.0
pod_defaults.budget.per_bot_daily_hard_usd      = 20.0   (fallback)
pod_defaults.budget.per_bot_daily_warn_usd      =  5.0   (the $5 ALERT)
pod_defaults.budget.tier_downgrade_usd          = 15.0
pod_defaults.budget.per_bot_session_cost_cap_usd= 10.0
pod_defaults.budget.l2_breaker_usd              = 50.0
```

The L1 cost breaker **did trip** — `{shared_dir}/breakers/ledger/cost.json`:

> tripped_at `2026-06-12T10:11:20Z`, `$10.44 ≥ $10.00`, initiated_by
> `auto:spend_alert`. Heartbeat was stashed at 06-11T09:39 as well — so the
> cap tripped **on both days**.

**Yet the spend still landed at $19.82 / $10.44.** The reason is structural:

- The L1 cost breaker's enforcement is **heartbeat-disable + gateway kickstart** — it pauses the bot's *own autonomous/background* activity and leaves user chat working (per [spec-cost-caps-2026-06-05.md](spec-cost-caps-2026-06-05.md)).
- But **forge builds and the audit pipeline are not the bot's heartbeat.** They are launched by admin-side daemons (the forge dispatcher and the audit-scheduler) as headless runs *as* the bot. They ran **while an active cost breaker was tripped** (the overnight audit wave and the activation builds both fired before the breaker's 24h TTL expired).

So the cap is real, it fires daily during the setup window, and it is
**unable to govern the spend that dominates a new bot's cost.** A cap value
alone — at any number — does not fix this; the enforcement scope is the gap.

Separately, the forge cost guard ([forge_cost_guard.py](packages/admin/evolve_admin/applications/forge_cost_guard.py))
sets a `per_turn_cap_usd=5.0` default whose stated intent is to "refuse Opus
dispatches by default and force a deliberate opt-in; Sonnet + Haiku flow
through." The wizard-driven builds ran on Opus regardless, because the new
bot's tier is `full` (Opus) and apps inherit the bot's LLM stack. The guard's
own default points at the right answer (§6).

---

## 5. Recurrence verdict: one-time setup tax

**This is a front-loaded, decaying setup tax — not a recurring ~$5/day burn.**

| Cost driver | Recurs? | Steady-state contribution |
|---|---|---|
| Forge app-builds ($24.71) | No — only on create/modify | $0/day once apps are built |
| First tier-3 audit of each app ($5.54) | Monthly cadence (`audit.cadence = "monthly"`; a never-audited app is "due" immediately, then not for 30 days) | ≈ $0.18/day amortized |
| Skill / provider audits | Weekly cadence | small |
| Daily 07:00 briefing | **Yes — daily** (the value) | ~$0.1–1/day depending on tier |
| Idle heartbeat | Yes | minimal |

**Baseline for comparison.** Established single-purpose bots on this pod
run **$0.18–$4.21/day (median ~$1.27, mean ~$1.6)** — and almost entirely
on **Haiku/Sonnet**, not Opus. `ledger`'s two-day setup ($30.26) is roughly
**19 days of mature-bot operation compressed into ~36 hours.**

**Steady-state estimate for an idle 6-app `ledger`:** once the build +
first-audit window closes, **~$0.5–2/day** — squarely in the mature-bot
range, dominated by the daily briefing (the thing the operator actually
wants). The audit cost does **not** re-charge daily.

> Caveat that extends (not breaks) the verdict: if apps churn through
> build → audit → auto-fix → rebuild → re-audit during week 1, the setup
> tax *spreads across the first several days* rather than perpetuating —
> which is exactly the field's "week-1 setup tax" shape (§7). It still
> converges; it is a setup-window phenomenon, not a steady-state one.

---

## 6. Recommendations (for the design-sync — operator's call)

These route to the operator. **No code shipped here.** The cap value and
posture are the operator's decision; a default-cap change is a separate
reviewed PR.

1. **Bound cost-to-first-value, and *show* it at creation.** Mirror U1's
   "time-to-first-value < 24h" with an accepted **cost-to-first-value**.
   The wizard already knows the starter pack; have it project provisioning
   cost ("standing up these 6 apps will cost ≈ $X one-time") and surface it
   in the consent/cost step alongside the daily figures. A spent number the
   operator agreed to is not a "cost surprise" (the field's drop-off cause #2).

2. **Route provisioning builds + first-audits to Sonnet, not Opus
   (highest-leverage, ~4–5× saving).** Templated starter-pack generation
   and structural audits do not need Opus. This single change takes the
   $24.71 build cost toward ~$5 and the audit cost down proportionally. The
   forge cost guard's own `per_turn_cap_usd=$5` default already encodes
   "refuse Opus by default" — apply it on the wizard/provisioning path.

3. **Stagger / defer the day-1 audit wave.** Skip the immediate tier-3
   re-audit of an app the forge **just built clean** — a same-day adversarial
   re-audit of fresh, to-spec output is largely redundant (the overnight wave
   here reported "no dominant pattern… manual review recommended"). Defer the
   first tier-3 audit to +7 days or first real usage. This removes the
   overnight spike entirely and spreads audit cost out of the activation
   window.

4. **A product-default daily cap for new bots — proposed value.** Adopt a
   graduated default in code (product-defaults-in-code): **$10/day for the
   first 7 days, then $5/day**, or a flat **$8/day**. Rationale: mature bots
   run ~$1–2/day, so $5 (the current warn) is already 2.5–5× normal and $8–10
   leaves headroom for a genuinely busy day. **But note (4) is insufficient
   alone** — see (5).

5. **Make the cap able to govern provisioning** (the real fix). Either bring
   forge-dispatch and audit-scheduler runs under the bot's cost-breaker check,
   or give provisioning its own one-time **creation budget ceiling** (e.g.
   $10–15, projected and enforced at dispatch by the forge cost guard). Today
   the daily breaker trips without stopping the bleed because the spenders run
   outside its enforcement scope.

6. **Keep alert and hard-cap distinct, and surface both at creation.** They
   already differ (warn $5 / hard $10–20) but are invisible to the operator
   at creation. Show both in the wizard: "alert at $X/day, hard stop at
   $Y/day, ~$Z one-time to set up."

**Recommended starting point for the sync:** default new-bot cap **$10/day
(first 7d) → $5/day**, a **$12 one-time provisioning ceiling** projected at
creation, **Sonnet** for provisioning builds/first-audits, and **defer the
day-1 tier-3 audit** to +7d.

---

## 7. Framing: this confirms the field "week-1 setup tax" on our own pod

The activation roadmap's field refresh (roadmap §3.2) names week-1 as
"setup tax (~$40–50 burned on kinks)" and lists **cost surprise** as the #2
weeks-1–3 drop-off cause. `ledger` reproduced exactly that pattern on our
own pod: **$30 in under two days before delivering a single unit of value**, caught
only because the spend-alert instrumentation fired.

This is the cost mirror of the U1 delivery-P0 (roadmap §5, W4): there, a
"functioning" pod was silently *not delivering* its #1 value; here, a
just-activated bot is *spending* like an established one for two weeks before
delivering anything. Both are the activation funnel leaking — one on the value
side, one on the cost side. **"Time-to-first-value < 24h" needs a partner
metric: "cost-to-first-value < $N, shown and accepted at creation."**

---

## 8. Credential / attribution note

`ledger` borrowed `atlas`'s Anthropic credential at creation
(`auth: {provider: anthropic, mode: api_key}`; no per-bot Anthropic key in
`ledger`'s credential store). **Per-bot cost attribution is correct
regardless:** every turn is tagged `instance=ledger` and costed from its own
token counts, so the pod's cost ledger, the Usage page, and `spend_alert` all
attribute this spend to `ledger`. The only consolidation is on the *external*
Anthropic invoice, where the usage rolls up under the shared/borrowed key —
a billing-rollup caveat, not an attribution bug. (No keys or tokens are
reproduced in this doc.)

---

## 9. Links

- Roadmap: [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §U1
- Cost caps spec: [spec-cost-caps-2026-06-05.md](spec-cost-caps-2026-06-05.md)
- Spend alerter: [spend_alert.py](packages/analyzer/spend_alert.py)
- Forge cost guard: [forge_cost_guard.py](packages/admin/evolve_admin/applications/forge_cost_guard.py)
- Audit cadence: [app_audit_runner.py](packages/analyzer/app_audit_runner.py) (`_DEFAULT_POD_CONFIG`, `_is_tier3_due`)

---

## Status (as of 2026-06-14): SHIPPED

The cost-defaults decision was settled at the 2026-06-12 design-sync (roadmap §8 "Cost-defaults decision — SETTLED"). All four recommendations + the UI rider have now shipped and been reviewed sound; the slice is **CLOSED** (roadmap §8 "Cost-defaults slice — CLOSED 2026-06-14"). Mapping §6's recommendations to the PRs that landed them:

| §6 recommendation | PR | Verdict |
|---|---|---|
| **(4)** product-default graduated new-bot daily cap ($10/day first 7d → $5/day) | [#2816](https://github.com/evolve-ops/evolve/pull/2816) | reviewed sound |
| **(2)** route provisioning builds + first-audits off `power` → `standard` role | [#2817](https://github.com/evolve-ops/evolve/pull/2817) | reviewed sound |
| **(3)** defer the day-1 tier-3 audit to +7d / first real usage | [#2822](https://github.com/evolve-ops/evolve/pull/2822) | post-merge SOUND (superseded empty #2815) |
| **(5)** $12 one-time provisioning ceiling that governs provisioning ("the real fix") | [#2852](https://github.com/evolve-ops/evolve/pull/2852) | auditor-grade SOUND (recovered empty #2846) |
| **(1)+(6)** cost-to-first-value projected + alert/hard-cap shown & accepted at creation (UI rider) | [#2870](https://github.com/evolve-ops/evolve/pull/2870) | META-verified sound |

**Three by-design residuals of (5), ACCEPTED no-fix:** (a) concurrent-dispatch overshoot — independent read-then-spend checks on overlapping provisioning paths can overshoot by ~one build per concurrent path, still bounded (atomic reservation judged not worth the weight); (b) the audit-side ceiling arm is mostly shadowed by the (3) deferral within the provisioning window (live on the fail-open path; the daily-breaker arm is always live) — defense-in-depth, not dead code; (c) budget-pause and time-defer share the `apps_first_audit_deferred` counter (trail entries distinguish them) — trivial metrics conflation.
