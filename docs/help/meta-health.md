---
title: "Help: Meta-health (rolled into Recommendations + Alerts)"
slug: meta-health
audience: public
status: deprecated
last_reviewed: 2026-06-05
concepts:
  - meta-health
  - watchdog-events
  - alerts
  - calibration-snapshots
ui_surface: null
related_specs: []
---

# Help: Meta-health (rolled into Recommendations + Alerts)

The standalone Meta-health page was retired during the Better Engine pipeline unification (May 2026). Its three surfaces now live elsewhere:

- **Events** → the Alerts page (Reports → Alerts). Watchdog events are emitted as Signals with the `meta_health:*` signature prefix; severity, scope, and event-type filters all work the same way.
- **Snapshots** → still reachable via `/api/calibration/snapshots`, surfaced inside the Recommendations page's "Coaches" subtab as a per-generator history block.
- **Observations** → the Observations subtab on Recommendations.

The rest of this doc describes the original page's contents — useful as a glossary of event types and snapshot kinds, which still exist under their new homes.

---

## Events

Every event is something the Evolve Watchdog noticed about the system itself, not about your bots. Severity-colored cards, newest first.

### The event types

| Event type | What it means |
|-----------|---------------|
| `proposal_volume_deviation` | A generator is emitting significantly more (or fewer) proposals than its baseline. |
| `auto_revert_rate_spike` | Auto-revert rate has jumped — generators are producing changes that fail verification. |
| `rejection_rate_spike` | Users are rejecting proposals from a generator at an above-baseline rate. |
| `verification_reliability_drop` | A generator's verified-success rate has dropped below baseline. |
| `generator_dominance` | One generator is producing an outsized share of pod-wide proposals. |
| `calibration_drift` | Calibration parameters have drifted from their baseline values. |
| `observation_extraction_drift` | The observation-extraction step is producing materially different outputs than before. |
| `meta_layer_cost_spike` | The arbiter + verify + watchdog layer is costing more in resources than its baseline. |
| `gateway_instability` | A bot's gateway is restarting more often than baseline (raised by `sysadmin_watchdog`). |
| `config_drift_unexplained` | A bot's `openclaw.json` or identity files differ from the last applied state without an approved proposal trail. |
| `test_failure_pattern` | An app's test suite has failed repeatedly enough to warrant a system-level look. |

Each card shows severity (info/warn/alert), scope (pod-wide or a specific bot), a one-line description, and the event's raw details (generator_id, ratio, current vs. baseline values). Hover the badge for the tooltip.

### When an event becomes a proposal

Watchdog events are observations, not actions. The Evolve Watchdog generator reads its own event log and *emits proposals* in response — typically ThrottleGenerator or PauseGenerator. Those land in the Proposals queue for you to act on. So the typical flow is:

1. An alert appears here.
2. A few minutes later, a proposal shows up on the Proposals page ("pause generator X — its auto-revert rate tripled").
3. You act on the proposal there.

This page is the raw signal; the Proposals page is where you respond.

### Filters

- **Type** — one of the eight event types.
- **Severity** — info, warn, or alert.
- **Since** — days back to look. Default 14.

The nav badge counts alerts; if any alerts exist, a number appears next to "Meta-health" in the sidebar.

---

## Snapshots

Calibration snapshots are point-in-time copies of a tunable layer. They're taken:

- Weekly, for every layer (scheduled snapshot).
- On-demand, when a calibration change is made.

Three kinds, all listed newest first:

- **user** — a bot's dimension weights at a moment. Restore rewrites `{bot}.md`'s frontmatter weights and appends an audit-log entry.
- **generator** — a generator's config at a moment. Restore overwrites the record's `config` dict via the registry.
- **signal** — signal-threshold calibration. Restore not yet implemented (501); snapshots are preserved for manual review until the storage layer wires in.

### How to restore

Click **Restore**. Confirm the dialog. For user targets you'll be prompted for the bot_id; for generator targets, the generator_id. (They're also stored in the snapshot's `data` if it was taken that way — those get used as defaults.)

The restore is idempotent: reapplying the same snapshot produces the same state. For user restores, the profile's audit-log section records the snapshot_id and timestamp.

### When to restore

- You changed a dimension weight, regretted it, and want to go back to what you had.
- A generator's config tuning has been counterproductive — restore the previous config.
- Before an experiment, take a snapshot (future work — not yet a UI action) and restore when done.

A restore never removes evidence of the intermediate state — the old record isn't deleted, just overwritten, and the audit log preserves what happened.

---

## Observations

The observation browser lets you inspect the raw (noun × verb × mood × engagement) tuples that the extraction layer produces from session turns. This is the evidence pool that every generator reads from; browsing it tells you what the system is actually seeing.

### Filters

- **Bot** — required.
- **Since** — days back (default 7).
- **Noun** — partial match against the noun field (`fitness`, `email`, `code`, …).
- **Verb** — one of the fixed 20-word verb vocabulary (drafting, planning, troubleshooting, …).
- **Mood** — one of neutral / urgent / enthusiastic / frustrated / reflective / uncertain, or unspecified.

### What the summary shows

- Window range, total tuples in window, matched count, engagement total.
- Mood distribution across matched tuples.
- Top 10 verbs and top 10 nouns, by count.
- Sample table of up to 30 matched tuples: when, noun, verb, mood, engagement, session id prefix.

### When to use this

- Debugging a generator that surfaced a surprising proposal: filter by the noun or verb the proposal cites and see how many tuples back it up.
- Understanding which verbs dominate a bot's usage (if it's overwhelmingly "drafting" and "reviewing", you probably don't need an "exploring"-targeted proposal).
- Sanity-checking the extraction layer after a model change — if top nouns or mood distribution shift sharply, the extractor may be drifting.

If the tuple stream isn't available (pre-L3 annotation data, or a fresh bot with no observations), you'll see an error with a pointer to `{shared_dir}/observations/<bot>/`.

---

## Common questions

**A watchdog alert fired but no proposal appeared on the Proposals page. Why?**
The Watchdog generator runs on a cadence (typically daily or weekly depending on its charter). Alerts in the event log are immediate; the proposals are produced on the next Watchdog pass. Check the Generators page → evolve_watchdog → recent_proposals.

**I restored a user snapshot — why didn't the weights on the Profile page change instantly?**
They did, at the API layer. The Profile page caches the last-loaded state; refresh the page or reselect the bot.

**Can I take a snapshot manually?**
Not from this UI yet — snapshots are taken on a cadence and when calibration changes apply. Spec work for user-initiated snapshots is tracked in the Phase 3 follow-up list.

**Observations show 0 tuples but my bot has had sessions.**
Tuple extraction is an L3 feature; it runs alongside the session annotator. If your bot predates L3 or the extractor hasn't run yet, there won't be tuples. The session-quality page still works — it reads the annotation JSONL directly rather than tuples.
