# Principle: Tri-State Status — `null` ≠ `0`

**Status:** load-bearing design principle (not a soft guideline).
**Adopted:** 2026-05-31, consolidating the rule already enforced in `spec-tier-cascade-2026-05-26.md` and the broader "distinguish tooling failure from substantive findings" pattern.

---

## The principle, in two clauses

1. **Detectors return a sentinel — typically `null` — when they cannot measure, distinct from a real zero/false/empty result.** A struggle detector that couldn't parse the OC payload returns `score: null`, not `score: 0`. A monitor whose `sudo` grant is missing returns `status: "unmeasurable"`, not `status: "ok"`. A finder that errored returns an error record, not an empty findings list. Silent degradation to "looks fine" is a bug.

2. **Downstream consumers must handle the three states explicitly — "measured signal," "measured no signal," "couldn't measure."** Aggregators ignore `null` rather than averaging it as zero. Surfaces show "% measurable" alongside the headline number so operators see when the contract is degrading. Decision logic that branches on a score must treat `null` as its own case, never coerce to false/zero.

## What this implies in code

Practical translation across the codebase:

### Detectors emit sentinels, not coerced defaults

A function whose job is to classify, score, or measure something must distinguish "ran successfully and found nothing" from "couldn't run." The first returns `0` / `False` / `[]`; the second returns `None` / a sentinel object / raises an explicit "unmeasurable" exception. Catching and silently returning `0` from inside the detector is the violation.

Reference impl: `StruggleDetector` in the tier-cascade work returns `score: null` when `event.messages` is malformed, and emits a `cascade_payload_unexpected` Signal so the contract drift is observable ([spec-tier-cascade-2026-05-26.md](spec-tier-cascade-2026-05-26.md) §"OC payload contract drift").

### Monitors emit tri-state status, not boolean health

Audit-style monitors that gate on whether their own probe ran (sudo grant, file readable, command exited 0) must publish three states: `ok`, `finding`, `unmeasurable`. A monitor that loses sudo and silently reports `ok` trains operators to trust a green checkmark that means nothing.

Reference impl: `infra_audit` visudo handling (PR #1579) is the worked example — distinguishes "check ran and found X" from "check never ran" via tri-state status + `sudo -n` + escalation-stderr detection.

### Aggregators ignore `null`, don't average it as zero

Mean / sum / percentile calculations over per-bot or per-session metrics filter out null rows before computing. A 7-day average score that includes "couldn't measure" days as zeros lies to the operator about the bot's actual state.

### Surfaces show measurability alongside the metric

Tile metrics that depend on a measurable signal carry a coverage indicator — "% measurable this week" or a coverage chip. When measurability drops, the operator sees it as a signal rather than seeing the headline number quietly drift.

## Anti-patterns to grep for

These are violations:

- `score = result or 0` (coerces None to 0 — loses the distinction)
- `except Exception: return False` in a detector (silent unmeasurable → measured-false)
- Boolean `is_healthy` fields from monitors that probe via sudo / external command (should be tri-state)
- Averaging over a list that may contain Nones without filtering first
- `if score < threshold` without a prior `if score is None` branch
- Charts that plot `null` as zero rather than as a gap or distinct color

## What this principle is NOT

- **Not a demand to return `null` for every error.** Programming errors (NPEs, type mismatches inside our own code) should still raise. The principle covers measurement-impossible cases — missing payload, missing grant, external contract drift.
- **Not a ban on defaults.** A config field that's missing can still default to a sensible value. The principle covers detectors and monitors, not config readers.
- **Not retroactive at every detector simultaneously.** Existing detectors that silently coerce can be migrated incrementally — but new detectors must ship with the three-state contract from day one.

## Why this matters

Silent degradation is the most expensive failure mode the pod can suffer because it looks identical to "everything is fine." A monitor that lost its sudo grant six months ago and has been reporting `ok` ever since is worse than no monitor at all — it claims coverage it doesn't have. The tri-state contract is the cheapest insurance: it costs one extra branch in the consumer and saves the operator from confidently-wrong assertions.

The May 2026 cost-alerting blackout (see canonical incident doc) had this pattern as a root cause — multiple detectors degraded silently and the headline dashboards looked normal while spend ran 4× over budget for a day.

## References

- [spec-tier-cascade-2026-05-26.md](spec-tier-cascade-2026-05-26.md) §"OC payload contract drift" — the canonical `score: null` worked example
- [principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md) — the surfacing rule (a tri-state result must be explainable to the operator)
- `docs/incident-cost-alerting-blackout-2026-05-20.md` — the cautionary tale of silent degradation
- PR #1579 — `infra_audit` visudo handling, the tri-state reference impl
