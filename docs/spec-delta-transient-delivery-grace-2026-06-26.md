# Spec delta — Transient-condition delivery grace (don't page on blips that self-resolve)

Status: **active** · Created 2026-06-26 · META:reports
Extends: [spec-transient-signal-suppression-2026-06-23.md](spec-transient-signal-suppression-2026-06-23.md)
(the producer-side umbrella) and [spec-reports-2026-06-12.md](spec-reports-2026-06-12.md).

## Problem

Two operator-facing alerts on the Linux VPS pod, both transient, both with
**nothing for the operator to do**:

1. `security.config_drift` — *"pod perm contract drifted: N targets need
   ensure-pod-perms"* fired 02:44, cleared 06:17 (~3.5h). The producer
   ([pod_perms_drift_monitor.py](../packages/analyzer/pod_perms_drift_monitor.py))
   already runs through `flap_gate` (N=2 dwell), and the dwell *was met* — the
   drift was genuinely present for hours, so it legitimately paged. It then
   self-healed when the next deploy ran `ensure_pod_perms`. **Config drift is
   auto-remediating**: the producer's own docstring says *"ensure_pod_perms only
   runs at deploy time; this monitor catches drift between deploys."* There is a
   guaranteed scheduled fix (deploy cadence ~15 min). Paging about a condition a
   scheduled job will silently fix, with zero operator action, is noise.

2. `meta.digest` CPU saturation — *"🔴 CPU saturation … 🟢 Cleared: CPU
   saturation"* both rendered in one digest. The signal opened-and-closed
   between digest runs. [digest_dispatcher.py](../packages/admin/evolve_admin/alerts/digest_dispatcher.py)
   `_dedup_records` collapses *repeats* of a still-firing signal but does **not**
   cancel a fire against its own resolve, so a within-window transient renders
   both lines — a textbook "nothing to do" pair.

The existing real-time notifier grace ([signal_notifier.py](../packages/admin/evolve_admin/alerts/signal_notifier.py):
~240s debounce + 30-min flap window) is notifier-only and far too short to catch
a multi-hour self-healing condition; the digest has no grace at all.

**Operator directive (2026-06-26):** *"We need some time threshold for issues so
we don't waste operators' time on transient issues and blips. If there is nothing
actionable to do, then an alert is not much use and is a distraction. A lot of
our subscription alerts fall into this class and we need to do better."*

## Design — three levers, all delivery-layer (reports-owned)

### L1 — Digest fire+clear cancellation (per-severity scaled)

At digest flush, when a signal's **fire and its matching resolve both land in the
same digest window** AND the signal's lifetime was shorter than its severity
grace (§L3), **drop both lines**. Net effect of a within-window transient =
silence. `alert`-severity pairs are never cancelled (a critical that fired and
cleared still gets surfaced). The Alerts page retains the full transition history
either way — cancellation is a *delivery* decision, not a store edit.

Implementation: extend `digest_dispatcher._dedup_records` / `_collapsed_lines`
to pair fire↔resolve records by coalesce key/signature and elide cancelled pairs.

### L2 — Auto-remediating condition class (notify-side suppress)

A small registry of signal `type`s that have a **known scheduled self-heal**.
First member: `config_drift` (heals at next deploy's `ensure_pod_perms`,
~15-min cadence). For a registered type, the notify-side rule is **"page on the
failure of the auto-fix, not on the condition"**: suppress the page until the
condition has outlived its expected self-heal window (≥ one deploy cycle, a
per-type tunable defaulting to ~30 min) **and is still firing**. A condition that
self-clears inside the window is logged to the Alerts page but never pushed.

This is the notify-side half. The act-side root fix — *proactively run
`ensure_pod_perms` when drift is detected between deploys, rather than waiting for
the next deploy* — is **deposited to `edr`/`deploy`** as a follow-up (it actually
remediates in minutes instead of paging). Out of scope here.

Lives alongside the producer-side gates conceptually but is applied at the
delivery boundary (notifier), so it composes with `flap_gate` rather than
replacing it: `flap_gate` ensures the condition is real (N≥2 dwell); this ensures
it is *durable past its own auto-fix* before the operator is told.

### L3 — First-class per-severity grace window (the substrate)

Generalize the notifier's hardcoded 240s debounce into a **per-severity grace**,
the operator's literal "time threshold for issues":

| Severity | Grace (default) | Behavior |
|---|---|---|
| `alert` (critical) | ~0 (near-immediate) | never delayed |
| `warn` | ~10–15 min (≈ one digest window) | a warn that clears inside grace is never delivered |
| `info` | ~15 min | same; info is already hidden-by-default on the page |

A firing signal younger than its severity grace, that resolves before the grace
elapses, is never pushed (notifier) and is a cancellation candidate (digest, L1).
Operator-tunable via `network.json::alerts.grace_seconds_by_severity`; the
existing `alerts.signal_notifier.flap_window_seconds` stays as the recurring-flap
window (distinct concern). `alert` grace is clamped so a critical can never be
silently delayed by misconfiguration.

## Invariants

- **`alert`/critical is never suppressed, cancelled, or delayed** by any of L1–L3.
- Suppression/cancellation is a **delivery** decision — never hand-edit Signal
  JSON; the Alerts page always shows the true firing/resolve history.
- L2 composes with `flap_gate` (does not replace it): dwell proves *real*, grace
  proves *durable-past-auto-fix*.
- Defaults must quiet the two observed cases without silencing any genuinely
  actionable, persistent, or critical condition.

## Slices

- **Chip A** — L1 + L3: per-severity grace substrate + digest fire/clear
  cancellation (notifier debounce generalized to severity-scaled grace; digest
  collapser elides same-window cancelled pairs).
- **Chip B** — L2: auto-remediating condition registry + notify-side
  suppress-until-self-heal-fails; mark `config_drift` as first member; deposit
  act-side proactive-remediation follow-up to `edr`/`deploy`.
