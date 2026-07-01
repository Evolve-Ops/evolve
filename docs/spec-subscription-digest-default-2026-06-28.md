# Spec — Digest-by-default delivery + flap-leak closure (workstream D)

**Aspect:** `reports` · **Created:** 2026-06-28 · **Status:** DESIGN (operator-approved direction)
**Extends:** [spec-subscription-completeness-2026-06-24.md](spec-subscription-completeness-2026-06-24.md)
(this review unblocks its held **A** + **C**) ·
**Companion:** [spec-drift-alert-taxonomy-2026-06-26.md](spec-drift-alert-taxonomy-2026-06-26.md)
(config_drift reclassification, edr co-owned) ·
[spec-transient-signal-suppression-2026-06-23.md](spec-transient-signal-suppression-2026-06-23.md) ·
[spec-delta-transient-delivery-grace-2026-06-26.md](spec-delta-transient-delivery-grace-2026-06-26.md)

## Motivation — the 2026-06-28 evo-vps flood

A single day of messages to the Evo bot: **~40 pushes, of which exactly 1 was
actionable** (the 🔴 evo gateway down). Breakdown:

| Subscription | Count | Verdict |
|---|---|---|
| `security.audit_finding` (darwin `auth-profiles.json mode=640` fire↔clear all day; evo sqlite/sessions triple) | ~22 | benign flap |
| `security.config_drift` ("pod perm contract drifted" fire/clear) | ~6 | auto-heals at deploy |
| `system.daemon_error_spike` (`set_evolve_read_acl` fire/clear) | ~3 | self-clears |
| `meta.unclassified` (version skew, fix:feat ratio, cache-invalidation, plugin telemetry) | ~7 | info junk drawer |
| `system.gateway_autorestart_failed` | 1 | **actionable** |
| `cost.weekly_summary` | 1 | useful summary |

The volume isn't the disease — it's that the volume **buries the one RED**.

## Grounding — the machinery already exists (and is deployed)

Per the pipeline map (2026-06-28):

- **Per-event digest frequency** (`immediate` / `daily_digest` / `weekly_digest`) —
  `dispatcher.send()` routes non-immediate to the digest queue. ✅
- **Per-severity delivery grace** (`grace.py`): `alert`=0s, `warn`/`info`=900s.
  Fire younger than grace is held; fire+clear within grace both dropped
  (`digest_dispatcher._cancel_transient_pairs`). ✅
- **Clear-message gating**: a "🟢 Cleared" pushes **only if the fire was pushed**
  (`signal_notifier`, `alerted_for_signal_id`). ✅
- **Producer flap hysteresis** (`flap_gate.py`, N≥2 cycles) — wired into the
  sysadmin watchdog ACL drift + pod-perms drift monitors. ✅

So the flood is not "no digest." It is **three leak-holes past the deployed gates**:

1. **Catalog defaults page benign noise.** `security.audit_finding` =
   `IMMEDIATE`/ERROR; `security.config_drift` = `IMMEDIATE`/ERROR. Severity is
   conflated with must-act.
2. **The audit producer never calls `flap_gate`.** `security.audit_finding`
   (the single biggest contributor) gets no hysteresis — every cycle pushes,
   even though its perm/mode/acl types are already `flap_gate`-eligible.
3. **Grace covers single transients, not recurring multi-hour flaps.** A
   condition that oscillates every ~couple hours with ~15–20min lifetimes
   evades both the 900s grace and (un-gated) hysteresis. (Pipeline-map gap #4.)

Plus the unbound catch-all: `meta.unclassified` is loud-by-default.

## Design — workstream D

### D1 — Digest-by-default catalog flip (keystone)

**`IMMEDIATE` is reserved for must-act, not for "loud severity."** The default
delivery class is derived from a single rule applied to every `CatalogEvent`:

```
default_frequency = IMMEDIATE  iff  severity == CRITICAL
                                  or is_safety_critical
                                  or key in MUST_PAGE_ALLOWLIST
                  = (its natural summary cadence) for SUMMARIES events
                  = DAILY_DIGEST  otherwise
```

`MUST_PAGE_ALLOWLIST` (curated, small): `system.gateway_state_change`,
`system.gateway_autorestart_failed`, `cost.hard_cap_hit`, `cost.breaker_tripped`,
`cost.gateway_stopped`, plus the genuine-unauthorized-tamper class from
drift-taxonomy L2 (the `heal` git-diff detector) once it lands.

Net effect: `security.audit_finding` (ERROR) and `security.config_drift` flip to
`DAILY_DIGEST`; the real CRITICAL findings (world-readable creds) keep paging.
Operator override per-event on the Subscriptions page is unchanged — default
aggressive, tune up individually.

**Guardrail:** a parity test pins the derived default for every catalog key, so a
new event can't silently re-introduce a loud default. The audit case study
(2026-06-28 set) is encoded as a fixture: the flood collapses to one digest.

### D2 — Wire the audit producer through `flap_gate`

`analyzer/audit.py` routes its perm/mode/acl-family `security.audit_finding`
signals through `flap_gate.note_observed(..., dwell_cycles=2)` /
`note_cleared(...)` — the same contract the sysadmin watchdog already honors.
The eligible-type substrings (`perm`, `file_mode`, `secret_mode`, `acl`, …)
already match these findings; this is wiring, not new policy. Kills the darwin
`auth-profiles.json` oscillation before it ever reaches the dispatcher.

**edr coordination:** these are `category: security` findings. The must-always-
page floor (genuine world-readable creds, CRITICAL) is **never** flap-gated — it
pages on cycle 1. Only the benign mask-artifact / group-readable family dwells.
Co-sign with edr that the floor is intact (shares the drift-taxonomy must-page
floor).

### D3 — Recurring-flapper demotion (delivery layer)

Close pipeline-map gap #4. A new delivery-side detector: track per
`(source, coalesce_key)` oscillation count in a rolling 24h window. When a
signature has flapped ≥K times (default K=4) — i.e. fired and cleared repeatedly
despite each lifetime exceeding the per-severity grace — **auto-demote it to
digest** and stop pushing its standalone clears until it has been stable for a
cooldown. The demotion is recorded (visible on the Reports page) and self-lifts
when the flap stops. `alert`-severity signatures are exempt (never demoted).

Lives next to `grace.py` / `rate_breaker.py` (same dispatch chokepoint).

### D4 — Bind-complete (resume workstream A) + classify `meta.unclassified`

Unblocks subscription-completeness **A**. Every dispatched message resolves to a
real `catalog_event`; the `catalog_event=None → skip gating` escape hatch is
replaced by `is_safety_critical` as the gating modifier. The recurring
`meta.unclassified` contributors get real classes:

- bot-version skew (newer/older than admin) → `updates.version_skew`
  (DAILY_DIGEST; self-clears, never page);
- prompt-cache invalidation stat → a `cost.*` or `meta.*` info class
  (DAILY_DIGEST);
- fix:feat dev-health ratio → a `meta.*` dev-health class (DAILY_DIGEST);
- plugin cascade-telemetry silence → keep as a real `system.*` health class.

Residual unclassified defaults to `DAILY_DIGEST`, never loud-immediate.

## Deposited out of aspect

- **→ plainlang** (`plainlang.json`): plain-language + de-truncation pass on
  alert titles/bodies. Today's messages truncate mid-word ("readable by o…") and
  carry dev jargon ("evolve access contract NOT satisfied for bot_user=evo after
  the ACL grants", "fix:feat ratio 2.2x — verification-weak surface"). Apply via
  the voice-card mechanism (no per-message LLM cost) + a title-truncation fix
  (truncate on a word/clause boundary with the full text in the body).
- **→ deploy/edr** (`deploy.json` / `edr.json`): the **underlying re-clamp flap
  source** — darwin `auth-profiles.json` physically re-drifts to `mode=640`
  (OC re-harden re-clamps the ACL), and pod-perms drift recurs between deploys.
  Fix at source so the condition stops oscillating. Distinct from D2/D3, which
  quiet the *alert*; this removes the *cause*. See
  [[feedback_3198_clamp_missed_self_heal_call_site]] /
  [[feedback_linux_default_acl_mints_group_world_readable]].

## Sequencing

D1 first (highest leverage, lowest risk — a catalog edit + parity test; collapses
the flood immediately). D2 + D3 close the residual flap. D4 + the deposits land in
parallel. The drift-taxonomy companion (config_drift reclassification) proceeds on
its own edr-co-sign track.
