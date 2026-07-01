# Capability watchdog — design spec

**Status:** Draft. Pre-implementation. **Replaces — does not extend —** the `config_drift_unexplained` Signal pipeline currently emitted by `heal.py::detect_backup_drift`. Migration path in §11.

**Date:** 2026-06-08.

**Origin:** Conversation on whether bot-initiated file writes should count as "config drift." The observation that broke the existing model: a critical and *normal* method of using a bot is for the user to ask the bot to build things — apps, Python scripts, JSON files, heartbeats, crons. The current drift detector flags any change to `openclaw.json` outside the proposal pipeline as `security.config_drift` (CRITICAL), which is wrong for the happy path: it would page on the product's most important use case. The pivot this spec encodes: stop watching *files*, start watching *capabilities*. Most writes confer zero new capability and should be invisible; some writes extend the bot's reach into the world (new credential, new outbound destination, new persistence trigger, new privilege) and *those* are the watchdog's surface.

**Adjacent:**

- [packages/analyzer/heal.py](../packages/analyzer/heal.py) `detect_backup_drift_keys` — the current file-diff detector this spec retires for the security path. Heal's drift detection becomes a *baseline-staleness* check only (§11).
- [packages/analyzer/backup.py](../packages/analyzer/backup.py) — nightly git snapshot of `openclaw.json` + `evolve-tiers.json` into the bot's workspace. Survives this spec as forensic trail; no longer the trigger surface.
- [spec-prompt-injection-scanner-2026-05-10.md](spec-prompt-injection-scanner-2026-05-10.md) — content-pattern scanner over AGENTS.md / SOUL.md / POD_CONDUCT.md. **Sibling spec, not subsumed.** The injection scanner watches *natural-language instruction surface*; this spec watches *structured capability surface*. Both feed Signals; they classify different threats.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — the Signal store this spec writes into. New Signal `type`s defined in §8.
- [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) — the chokepoint pattern this spec inherits. Capability-extending writes get the same observability treatment privileged ops got from evo separation.
- [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md) — the role/capability enforcement layer for user-initiated tool use. This spec is the *audit* counterpart: roles gate who can ask, this watchdog records what got built.
- [docs/principle-each-bot-applies-its-own-changes.md](principle-each-bot-applies-its-own-changes.md) — establishes the proposal pipeline as the canonical self-modification path. This spec relaxes the corollary that "non-pipeline = unauthorized" to "non-pipeline = needs a grounding receipt and capability classification."
- Memory: [feedback_prelaunch_architect_properly](../../.claude/.../feedback_prelaunch_architect_properly.md), [feedback_rsi_low_cost_preference](../../.claude/.../feedback_rsi_low_cost_preference.md), [feedback_dont_reimplement_upstream](../../.claude/.../feedback_dont_reimplement_upstream.md).

---

## 1. Problem

The current drift detector asks a binary question — *did `openclaw.json` change in a way that's not explained by the proposal pipeline or admin UI?* — and emits a CRITICAL Signal when the answer is yes. Five things break under load:

1. **It alerts on the happy path.** "Build me a daily Hacker News briefer" expands the bot's persistence (new cron), outbound destinations (news.ycombinator.com), and possibly its skill set. Today, any of these landing via a skill that writes `openclaw.json` directly fires `config_drift_unexplained`. The detector cannot distinguish *user-requested capability extension* from *unauthorized config tamper.*

2. **It misses the real threat surface.** Skills, Forge apps, AGENTS.md, SOUL.md, `auth-profiles.json`, `exec-approvals.json`, crontabs, and LaunchAgents are not in the drift check. The capability surface that an attacker would actually target lives mostly outside the watched file.

3. **It conflates configuration hygiene with security tamper.** "Live diverged from the nightly backup" (a backup-staleness fact) and "an unauthorized actor changed our config" (a security fact) ride the same CRITICAL Signal. Operators learn to dismiss the channel because most fires are the former.

4. **Self-declaration is fragile.** Admin-UI writers credit themselves by writing `oc_keys: [...]` to `audit-log.jsonl`. Miss one → false-positive CRITICAL. Over-declare → real drift hides under a legitimate write. Same shape as the silent-monitor-allowlist-drift class.

5. **Actor attribution is binary and inferred.** "Explained" vs. "unexplained" is the only axis. We can't say *who* did this (bot-self via skill, operator via SSH, pipeline) or *why* (which user turn motivated it).

The reframe is to shift the watchdog's question from **"did a watched file change?"** to **"did the bot's capability footprint extend, and if so, can we trace it to an intent?"**

---

## 2. Principle

**A change is interesting if and only if it extends what the bot can do in the world.** Every other change is content. Capability extension always carries a *grounding receipt* — a record of what intent (user turn, approved proposal, operator action) caused it. Receipts are captured, not gated: the watchdog does not block any change; it makes every change attributable.

Three corollaries:

- **Capability classification beats file-level diffing.** A new entry in `openclaw.json::mcpServers` and an `app install weather-tool` that registers the same MCP server are the *same security event* and should appear as one capability extension. The watchdog classifies events by what they confer, not by which file they touched.

- **Grounding is observability, not enforcement.** The bot may self-initiate a capability extension (e.g., autonomous proposal apply, RSI experiment) and stamp `{cause_type: "self_initiated", reason: "..."}` on the receipt. The watchdog logs this; it does not refuse the write. The operator decides at digest/review time whether the pattern is acceptable.

- **The severity of an event is a property of the capability class and the grounding, not the file path.** A grounded persistence change ("user asked for a daily cron") is `digest`. An ungrounded conduct relaxation ("`POD_CONDUCT.md` rule deleted, no receipt") is `alert`. The classifier picks the level; the file the change landed in is incidental.

---

## 3. Capability ontology

Six classes constitute the watchdog's surface. Every classified event belongs to exactly one (the most specific applicable). Class **H** (content) is the catch-all for everything outside these — silent, never surfaced.

| Class | Confers | Examples |
|---|---|---|
| **A. Identity** | New or modified credential / external identity | New auth token, new login profile, modified `mcpServers[i].url`, new provider in `auth-profiles.json` |
| **B. Privilege** | Wider exec or sudo surface; relaxed deny rule | New entry in `exec-approvals.json`, sudoers grant, OC `execPolicy` widened, scope expansion |
| **C. Persistence** | New autonomous trigger that runs without user prompt | New cron, new LaunchAgent, new `hooks.*` entry, new `heartbeat.*` task, new signal subscription |
| **D. Conduct** | Relaxation of instruction / policy surface | Deleted rule in `POD_CONDUCT.md`, removed paragraph in `SOUL.md`, weakened `AGENTS.md` directive, system-prompt override |
| **E. Outbound** | New destination the bot can reach | New webhook URL, new email recipient, new chat channel id, new network host in MCP/HTTP allowlist |
| **F. Tools** | New tool/skill/app available to the session | Skill mounted under `.openclaw/skills/`, MCP server attached, Forge app installed, plugin enabled |
| **H. Content** | (none) | Workspace scripts, JSON data, app body code, README, prompt template tweaks |

**Diff-direction sensitivity.** The classifier acts on *capability delta*, not raw equality:
- Removing an outbound destination, tightening exec policy, deleting a cron, adding a conduct rule = **not a capability extension**, silent.
- Adding an outbound, loosening exec, deleting a conduct rule, adding a cron = **extension**, classified.
- A rename or cosmetic edit (a comment in JSON, a typo fix in a conduct file, key re-ordering) = **none**, silent.

**Subclasses inside each class** carry differential severity (see §8). Examples:
- **A.token_rotation** (same destination, new value) vs **A.new_credential** (new destination)
- **B.exec_widening_new_command** vs **B.exec_widening_existing_command_new_path**
- **C.cron_external_outbound** vs **C.cron_local_only**
- **D.rule_removed** vs **D.tone_edit_no_rule_change**
- **E.first_seen_destination** vs **E.known_destination_new_route**
- **F.skill_with_mcp** vs **F.skill_no_capabilities_declared**

The subclass set is operator-curated in `{shared_dir}/policy/capability-classes.json`. New classes are added as new threat shapes surface; the catalog ships with sensible defaults and the existing OC config schema as input.

---

## 4. Data model

### 4.1 Capability event

Written atomically to `{shared_dir}/capability_events/<YYYY-MM-DD>/<bot_id>-<ts>-<id>.json`. Append-only; retention 90 days for events, 1 year for the day-rollup index.

```json
{
  "id": "ce_2026-06-08_14-21-07_abc123",
  "bot_id": "atlas",
  "detected_at": "2026-06-08T14:21:07Z",
  "class": "C",
  "subclass": "cron_external_outbound",
  "artifact": "openclaw.json:hooks.morning_briefing",
  "delta": {
    "kind": "added",
    "before": null,
    "after": {
      "schedule": "0 8 * * *",
      "command": "atlas.briefing.run",
      "outbound": ["news.ycombinator.com"]
    }
  },
  "actor": {
    "kind": "bot",
    "id": "atlas",
    "via": "oc_tool:file_edit"
  },
  "receipt": {
    "cause_type": "user_turn",
    "cause_ref": "telegram:atlas:turn-2026-06-08T14-20-44",
    "cause_excerpt": "schedule a hacker news briefing for 8am",
    "confidence": "high"
  },
  "fingerprint": "C/cron_external_outbound/atlas/news.ycombinator.com",
  "severity": "digest",
  "anomaly_score": 0.12,
  "signal_id": "sig_2026-06-08_capability_extended_persistence_atlas_abc"
}
```

Fields:
- **class / subclass** — from the ontology in §3.
- **artifact** — what changed, in a canonical form: `<filename>:<dotted-path>` for structured files; `<filename>` for binaries/markdown; `crontab:<bot>` and `launchd:<plist>` for OS persistence.
- **delta** — the *capability-relevant* change, not the raw byte diff. For markdown conduct files, the delta is `{rules_removed: [...], rules_added: [...]}` (extracted by the conduct classifier), not the raw text.
- **actor** — one of: `{kind: "bot"}`, `{kind: "operator", id: <admin_user>}`, `{kind: "pipeline", id: <proposal_id>}`, `{kind: "external"}` (the fallback when nothing on the system claims the write).
- **receipt** — the grounding. `cause_type` ∈ `{user_turn, approved_proposal, admin_ui_action, scheduled_task, self_initiated, unknown}`. `confidence` ∈ `{high, low, none}` reflects how strong the link is between the write and the cited cause.
- **fingerprint** — stable dedup key. Multiple capture streams (§5) emitting the "same" event collapse to one stored record.
- **severity** — from §8.
- **anomaly_score** — `[0.0, 1.0]`, how out-of-pattern this is for *this bot* (see §7).
- **signal_id** — link to the Signal in the alert store when `severity ∈ {review, alert}`. Null when silent / digest-only.

### 4.2 Policy: capability-classes catalog

Operator-curated under `{shared_dir}/policy/capability-classes.json`, ships with defaults:

```json
{
  "version": 1,
  "classes": {
    "A": {
      "name": "Identity",
      "default_severity": "review",
      "subclasses": [
        {
          "id": "token_rotation",
          "match": "auth_value_changed_destination_same",
          "severity": "trace"
        },
        {
          "id": "new_credential",
          "match": "auth_destination_added",
          "severity": "review"
        }
      ]
    },
    "B": {
      "name": "Privilege",
      "default_severity": "review",
      "subclasses": [
        {
          "id": "exec_widening_new_command",
          "match": "exec_approvals.commands.added",
          "severity": "alert"
        }
      ]
    }
  },
  "field_mapping": {
    "openclaw.json::auth": "A",
    "openclaw.json::mcpServers[*].url": "A",
    "openclaw.json::execPolicy": "B",
    "openclaw.json::hooks": "C",
    "openclaw.json::heartbeat": "C",
    "openclaw.json::channels[*]": "E",
    "openclaw.json::skills": "F",
    "openclaw.json::plugins": "F",
    "auth-profiles.json": "A",
    "exec-approvals.json": "B",
    "POD_CONDUCT.md": "D",
    "SOUL.md": "D",
    "AGENTS.md": "D",
    ".openclaw/skills/<*>/": "F",
    "apps/<*>/manifest.json": "F",
    "crontab": "C",
    "launchd": "C"
  }
}
```

`field_mapping` is the dotted-path → class lookup that the classifier consults when it has a structured diff. Unmapped fields are class **H** (silent).

### 4.3 Receipt-cause ledger

To verify that a `cause_ref` in a receipt actually exists, the classifier reads from existing stores:

- `cause_type: "user_turn"` → match against the bot's recent OC session log (turn id, timestamp, message excerpt). The OC plugin hook stamps this directly; the FS watcher matches by time window if no stamp.
- `cause_type: "approved_proposal"` → match against `{shared_dir}/proposals/applied/<id>.json` with `status="applied"`. This is the existing pipeline path.
- `cause_type: "admin_ui_action"` → match against `{shared_dir}/audit-log.jsonl` (existing).
- `cause_type: "scheduled_task"` → match against the routine/cron schedule that fired.
- `cause_type: "self_initiated"` → no external verification; the receipt carries a free-text `reason` field and the classifier flags `confidence: "none"`.
- `cause_type: "unknown"` → no receipt was attached. The fallback case, raises severity by one step (§8).

---

## 5. Capture: three input streams

All three streams feed a single classifier (§7) via append to `{shared_dir}/capability_inbox/<stream>-<ts>-<id>.json`. The classifier dedups by `fingerprint`.

### 5.1 OC plugin hook (primary, high-fidelity)

A new OpenClaw plugin hook `pre_write_capability` (or in-process wrapper around `file_edit`, `exec`, MCP-add, app-install tool uses) fires before any bot-initiated write to a watched path. The hook captures:

- The active turn's context (channel, user, message id, excerpt)
- The tool name and arguments
- The candidate write target and intended content

Posts a stream-1 capability inbox file with `actor.via: "oc_tool:<name>"` and `receipt.cause_type: "user_turn"` (with full turn metadata).

**Upstream dependency.** OC v2026.6.x does not yet expose this hook. Coordinate with upstream (see memory `feedback_dont_reimplement_upstream`); file an issue against openclaw/openclaw to add `pre_write_capability` similar to the existing `agent_end` / `llm_output` hooks. If upstream rejects or delays, the FS watcher (5.2) covers the same ground at lower fidelity until the hook lands.

### 5.2 FS watcher (always-on backstop)

A new long-running LaunchDaemon `ai.evolve.evolve.capability-watcher` (KeepAlive, runs as `evolve`, installed by `sudo evolve-admin install-infra-jobs`) uses macOS FSEvents to subscribe to:

- Every `/Users/<bot>/.openclaw/openclaw.json`
- Every `/Users/<bot>/.openclaw/auth-profiles.json`
- Every `/Users/<bot>/.openclaw/exec-approvals.json`
- Every `/Users/<bot>/.openclaw/skills/` (recursive)
- Every `/Users/<bot>/AGENTS.md`, `SOUL.md`
- Every `/Users/<bot>/.openclaw/workspace/apps/*/manifest.json`
- `/Users/Shared/evolve/POD_CONDUCT.md`, `RUNTIME_NOTES.md`
- Each bot's crontab (snapshot poll, 60s cadence — FSEvents doesn't see crontab edits)
- `/Library/LaunchAgents/` and `/Library/LaunchDaemons/`

On an event, the watcher:
1. Reads the new content (via the ACL read pattern already documented in CLAUDE.md — direct read, fall back to `sudo /bin/cat`).
2. Diffs against the last-seen baseline cached at `{shared_dir}/capability_watcher/baseline/<bot>/<artifact_canonical>.json`.
3. Posts a stream-2 capability inbox file with `actor.via: "fs_event"` and `receipt.cause_type: "unknown"`.

The classifier then attempts grounding-receipt matching against streams 1 and 3 within a 30s window (§7); a match upgrades the receipt with full context.

### 5.3 Wrapper library (opt-in enrichment)

`evolve_capabilities.write_capability(class, delta, actor, cause)` — a small Python lib that skill / app authors import to declaratively stamp capability changes:

```python
from evolve_capabilities import write_capability

write_capability(
    cls="C",
    subclass="cron_external_outbound",
    artifact="openclaw.json:hooks.morning_briefing",
    delta={"kind": "added", "after": new_hook},
    actor={"kind": "bot", "id": "atlas", "via": "skill:morning_briefing/install.py"},
    cause={
        "type": "user_turn",
        "ref": current_turn_id(),
        "excerpt": "schedule a hacker news briefing for 8am",
    },
)
# returns; caller proceeds with the actual write
```

The lib does **not** perform the write itself. It posts a stream-3 capability inbox file that the classifier will match against the actual FS / OC events that follow. The lib is the *author's* way to attach rich grounding when they have it (which user turn, which skill, what justification). Skipping the lib is fine — the FS watcher still catches the change, just with `cause_type: "unknown"`.

The wrapper is the place skills accept the "self-initiated, reason X" pattern from §2's grounding-as-logged decision.

---

## 6. Classifier

A pure-Python long-running process inside the capability-watcher daemon. Pulls files from `{shared_dir}/capability_inbox/`, runs the pipeline below, writes to `{shared_dir}/capability_events/`, archives consumed inbox entries.

```
inbox event → canonicalize artifact path → diff against baseline
            → look up class via field_mapping → compute subclass via match rules
            → match grounding receipts within 30s window across streams
            → score anomaly vs per-bot baseline
            → resolve severity from policy + grounding + anomaly
            → emit capability_event
            → if severity ∈ {review, alert}: also write Signal to {shared_dir}/signals/firing/
            → if severity ∈ {digest}: increment digest counter
            → update baseline cache
```

Pure Python, no LLM (per memory `feedback_rsi_low_cost_preference`). The classifier is deterministic; ambiguous cases default to higher severity rather than guessing.

**Dedup window** is 30 seconds: if streams 1 (OC hook) and 2 (FS event) both report the same `fingerprint` within 30s, they collapse into one event with the union of receipt data (richer wins). The fingerprint is `{class}/{subclass}/{bot_id}/{artifact_canonical_summary}` — stable across captures of the same logical change.

**No-receipt fallback.** If only the FS watcher saw the change (no stream-1 or stream-3 match within 30s), the receipt stays `{cause_type: "unknown", confidence: "none"}` and the severity table (§8) raises the baseline severity by one step.

---

## 7. Per-bot anomaly baseline

A change is *anomalous* if this bot doesn't typically make changes in this class.

For each `(bot_id, class)` pair, maintain a rolling counter of events over the last 30 days at `{shared_dir}/capability_watcher/profile/<bot>.json`:

```json
{
  "atlas": {
    "C": { "count_30d": 47, "first_seen": "2026-05-09", "last_seen": "2026-06-07" },
    "F": { "count_30d": 12, "first_seen": "2026-05-12", "last_seen": "2026-06-06" },
    "B": { "count_30d": 0, "first_seen": null, "last_seen": null }
  }
}
```

`anomaly_score` is a coarse function of:
- Time since first-seen of any event in this class (`first_seen == null` → 1.0; else recency)
- Frequency relative to the bot's median for that class
- Whether the artifact target has been touched before

Score is bucketed to `low | medium | high`. The classifier uses it to nudge severity (§8): a `digest`-tier event with `anomaly_score: high` becomes `review`. An `alert`-tier event with `anomaly_score: low` stays `alert` (we never down-grade an alert based on familiarity — that's how attackers normalize their footprint).

This is intentionally simple. We do not need a model. The point is to encode "atlas creates crons every day; team-bot-a never has" as a one-step bump, not to predict next week's traffic.

---

## 8. Severity ladder

| Level | Surface | Triggered by |
|---|---|---|
| **silent** | Logged to `capability_events/`; never surfaced | Class H (content); diff-direction-negative changes |
| **trace** | Bot's own activity log (admin UI, per-bot Activity tab); not in alert stream | A.token_rotation, F.skill_no_capabilities_declared, low-stakes grounded changes |
| **digest** | Daily summary card under Bot → Recent Capability Changes; weekly email roll-up | Class A–F grounded changes in default subclasses; "atlas added a cron + outbound to X yesterday" |
| **review** | Improvement Proposal under the Alerts page → Health → Drift section | Anomalous grounded changes; subclasses operator wants visible; cross-bot effects |
| **alert** | Immediate Signal via existing alert pipeline; notification per subscription preferences | Ungrounded class A–F changes; D.rule_removed; B.exec_widening_new_command; anything matching known-bad subclass patterns |

**Grounding modifier.** Receipt `cause_type: "unknown"` raises the resolved severity by one step (digest → review, review → alert). Receipt `confidence: "high"` from `user_turn` or `approved_proposal` may *lower* a digest to trace if the operator has opted into trusted self-extension for that bot.

**Anomaly modifier.** Per §7, `anomaly_score: high` raises by one step; never lowers.

**Cross-bot modifier.** Any class A–F change where the actor is one bot and the target is another bot's `.openclaw/` is automatically `alert`, regardless of grounding. (Bot-to-bot reach is not in the legitimate design surface.)

---

## 9. Signal types

Replace the single `security.config_drift` Signal with class-specific types under the existing signal store (spec-alerts-signal-store-2026-05-07):

- `capability_extended_identity` — class A, severity ≥ review
- `capability_extended_privilege` — class B, severity ≥ review
- `capability_extended_persistence` — class C, severity ≥ review
- `capability_relaxed_conduct` — class D, severity ≥ review
- `capability_extended_outbound` — class E, severity ≥ review
- `capability_extended_tools` — class F, severity ≥ review
- `capability_change_ungrounded` — any class A–F where `cause_type: "unknown"` AND severity = alert (composite signal for "the bot's config grew without anyone claiming it")
- `capability_cross_bot_tamper` — actor and target are different bots; always alert

Each Signal carries the full capability_event in `details`. Signal dedup uses the existing signature index keyed on `fingerprint`. Default subscription state in `signal_notifier._DEFAULT_PRODUCERS` and `schema.py::stock_default` (see memory `feedback_silent_monitor_allowlist_drift` — don't forget both surfaces).

The existing `security.config_drift` catalog event is **retired** after the migration in §11. Subscriptions auto-migrate to the union of the seven new types.

---

## 10. UI surface

Three placements:

### 10.1 Bot → Activity → Capability Changes (per-bot)

A new sub-tab under each bot's detail page. Three streams:

- **Today** — chronological list of all class A–F events for this bot in the last 24h, each as a card: class chip + subclass + artifact + actor + receipt excerpt + delta summary + a "View diff" disclosure.
- **Anomalies** — anything with `anomaly_score: high` from the last 7 days.
- **Ungrounded** — anything with `cause_type: "unknown"` from the last 30 days.

Operator affordance per card: **Acknowledge** (silences future identical events for N hours), **Investigate** (opens diff + receipt detail + linked Signal), **Mark as expected** (adds to per-bot ignore list for this subclass + artifact, with expiry).

### 10.2 Health → Capability Watchdog (pod-wide)

A summary card on the existing Health page:

```
Capability Watchdog
  Today:    3 grounded changes, 0 ungrounded, 0 alerts
  This week: atlas extended persistence (3×), tools (1×); team-bot-a no changes
  Anomalies: team-bot-a added a new outbound destination (first in 30 days) — Review
```

Click-through to a pod-wide event list with bot filter.

### 10.3 Alerts page → existing Signal stream

Class A–F Signals (§9) appear on the existing Alerts page like any other Signal. Subscription preferences (spec-alert-subscriptions-2026-05-10) gain seven new toggles.

**Language style:** Per memory `feedback_message_style_kit_like`, capability-change cards use plain language — "Atlas added a new cron job that runs at 8am and sends to news.ycombinator.com" — not "config drift detected." Per `feedback_design_constraint_mildly_tech_capable` (the Plex test), the operator never needs to understand what "class C subclass cron_external_outbound" means; the UI presents the human-readable form and tucks the taxonomy under a disclosure.

---

## 11. Migration from existing drift detection

The existing `detect_backup_drift_keys` in `heal.py` (lines 1254–1366) does not disappear; it changes role.

**Today's behavior (retired for security):**
- Diffs live `openclaw.json` + `evolve-tiers.json` against git baseline
- Credits "explained drift" from `proposals/apply-results/` + `audit-log.jsonl::oc_keys`
- Emits `security.config_drift` (CRITICAL)
- UI: "Config Drift Detected" card with "Accept as Baseline" button

**Post-migration behavior (baseline-staleness only):**
- Same diff logic, no credit lookup, no Signal emission
- Emits a `baseline_stale` event (info-level, not in alert stream)
- UI: surfaces under Bot → Activity → Backup Baseline as a *housekeeping nudge* — "your git baseline last refreshed 4 days ago; current state has 6 changes. [Refresh baseline]"
- "Accept as Baseline" button stays (it's the right ergonomic for "make the backup match what's running")
- The button silently calls `backup.commit_baseline_local` — no Signal, no incident record

**Why retain it.** The git baseline is a forensic trail. After an incident, the operator wants `git log` on the `evolve-backup/` repo to reconstruct what the config looked like a week ago. Removing the baseline removes that trail. Demoting it to housekeeping preserves the trail without firing on the happy path.

**Migration sequence:**
1. **Phase 1 (this spec, Phase A).** Ship the capability watchdog (streams 2 + 3, no OC hook) in parallel. Both detectors run; both write Signals; operator sees both during overlap.
2. **Phase 2.** After 30 days of overlap data, compare false-positive rates side by side. Expectation: capability watchdog produces ~10× fewer alerts at ~3× higher action-rate per alert.
3. **Phase 3.** Demote `heal.py::config_drift_unexplained` to `baseline_stale` (info). Auto-migrate active subscriptions. Update the catalog.
4. **Phase 4.** When OC upstream lands `pre_write_capability` hook, plumb stream 1.

---

## 12. Phased rollout

**Phase A — capture, classify, log, no alerts.**
- Build the FS watcher (stream 2), wrapper lib (stream 3), classifier.
- Ship the `capability-watcher` LaunchDaemon and `evolve_capabilities` Python lib.
- Default the policy catalog (`capability-classes.json`) with the §3 ontology and §8 severity ladder.
- Write `capability_events/` but do **not** emit Signals yet.
- Surface a passive Bot → Activity → Capability Changes tab so the operator can see what's flowing.
- 30 days of observation, tune the catalog, refine the per-bot baseline curves.

**Phase B — Signals on.**
- Wire `capability_events` with severity ∈ `{review, alert}` to the Signal store.
- Surface the Health → Capability Watchdog card.
- Tune subscription defaults (per memory: don't forget both `_DEFAULT_PRODUCERS` and `schema.py::stock_default`).

**Phase C — retire `config_drift_unexplained`.**
- Demote `heal.py` drift to `baseline_stale` info-level (§11).
- Migrate subscription preferences from `security.config_drift` to the union of capability signals.

**Phase D — OC plugin hook.**
- File upstream issue / PR for `pre_write_capability` on OC.
- Once landed, plumb stream 1, gain rich turn-level grounding for the happy path.

**Phase E — cross-bot reach + conduct delta classifier.**
- Stand up the Forge-app manifest classifier (capability declarations at install time).
- Build the Markdown rule-extractor for D.rule_removed (parses POD_CONDUCT / SOUL / AGENTS into structured rules so the diff is "rule X removed" not "line 47 changed").

**Phase F — operator-side scoring + auto-tune.**
- Allow operator to mark events as "expected" en masse; the classifier learns subclass-level allowlists per bot.

---

## 13. Non-goals

- **No blocking writes.** Per §2, this is observability. The OC plugin hook does not refuse the call; the FS watcher does not roll back. The watchdog *describes* what the bot did, never prevents it. Enforcement at the tool boundary is the role of [spec-user-roster-and-roles-2026-06-07.md](spec-user-roster-and-roles-2026-06-07.md); enforcement at the OS boundary is the role of [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md). This spec is the audit layer on top.
- **No content scanning.** Markdown prompt-injection patterns are out of scope here — see [spec-prompt-injection-scanner-2026-05-10.md](spec-prompt-injection-scanner-2026-05-10.md). The conduct classifier in class D operates on the rule structure, not on lexical injection markers.
- **No new proposal action kinds.** Capability events are observations. Remediation flows through existing proposal types: an operator who wants to undo a capability extension files a SoulEdit / config-change proposal against the bot in the usual way.
- **No LLM in the classifier.** Per memory `feedback_rsi_low_cost_preference`, the classifier is pure Python. LLM is an escalation tool for ambiguous Class D rule extraction at most; not the default path.
- **No per-event "block this in the future" affordance.** The acknowledge / expected / investigate flow is the granularity. We do not build a per-event policy compiler in Phase A–C.

---

## 14. Open questions

1. **Crontab and LaunchAgent coverage on macOS.** FSEvents reliably sees `~/.openclaw/` changes but not `crontab -e` edits (the file isn't user-visible). Phase A polls `crontab -l` per bot every 60s. Acceptable latency? If not, we need a different mechanism (a launchd-side hook is the cleanest but invasive).

2. **Forge app manifest declaration completeness.** This spec assumes Forge app manifests *declare* their capabilities (outbound destinations, persistence schedules, required MCP servers). The [project_manifest_schema_v7_recommendation](../memory/project_manifest_schema_v7_recommendation.md) memory describes the schema work needed to make this true. The watchdog leans on it; the spec ships without it (manifests are opaque to the classifier until v7 lands).

3. **Anomaly baseline cold-start.** A newly deployed bot has zero history. Phase A treats all of its first-week changes as `anomaly_score: high` (every class is "first-seen"), which would mean the operator gets paged for every cron Atlas creates in its first week. Mitigation: a 7-day grace window where the anomaly modifier is suppressed; events are still logged and classified, just without anomaly bumps. Confirm this is the right shape.

4. **Cross-bot reach detection.** §8 says cross-bot writes are always `alert`. The FS watcher can detect this from the path (one bot's daemon wrote into another bot's `.openclaw/`). But the legitimate cross-bot path is the admin daemon's unix-socket API ([spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md)) — writes routed through the admin daemon should be credited to whichever bot originated the request, not flagged as "external." Wire this attribution in Phase E.

5. **Per-bot opt-out for trusted self-extension.** Some bots (Atlas as the reference deployment) will routinely extend themselves at user request. Per memory `feedback_user_observation_optout`, any observation feature ships with a user-flippable DNT. Here, the operator should be able to mark a bot as "trust self-initiated grounded changes in class C/E/F at digest level" without losing alert coverage on class A/B/D and ungrounded changes. Confirm UI placement (probably Bot → Settings → Watchdog Trust Profile).

6. **What about `network.json`?** Pod-level network config (bots, channels, hosts) lives at `/Users/Shared/evolve/network.json`. Changes to this file are operator-only via admin UI today. Should it be in the watcher's path set? Probably yes — but the field mapping needs care (most fields are operator-facing config, not capabilities). Defer to Phase B catalog tuning.

7. **Retention vs. forensic depth.** 90 days of `capability_events/` is enough for routine review but not for incident reconstruction from months ago. Confirm: do we ship a longer cold-archive (compressed yearly rollups), or do we rely on the existing git baseline (which keeps full history) for that timeframe?

---

## 15. Success criteria

Within 30 days of Phase B:
- `security.config_drift` CRITICAL pages drop to zero on the reference pod (replaced by capability signals at correct severity).
- Average operator action-rate per capability-alert ≥ 60% (vs. the current `config_drift` action-rate, which is near zero — operators learn to dismiss).
- Zero unexplained class A or B events on the reference pod over the observation window (or: each one corresponds to a real investigation, not a false positive).
- Atlas's normal app-building flow generates zero `alert`-level events. Daily Atlas activity is a single digest entry: "atlas extended persistence (2×) and outbound (1×) today, all grounded to user turns."
- The Bot → Activity → Capability Changes tab is the operator's first stop when investigating "what has this bot done lately" — replacing today's pattern of reading raw git diffs against the backup repo.
