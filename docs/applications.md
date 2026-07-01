# Evolve — Application Manifests

The application system is how Evolve knows what a bot is supposed to do, whether it's doing it, and whether it's getting better over time.

---

## What is an application manifest?

An application manifest is a structured definition of one named area of bot functionality. It answers three questions:

1. **What should this bot do?** (goals)
2. **How do we know it's working?** (success metrics + the audit/coherence framework)
3. **What are its constraints?** (privacy, known issues)

Manifests are per-bot state. They live with the bot at
`~{bot}/.openclaw/workspace/manifests/{application_id}.json` (the `evolve` user
has a write ACL on that directory). They are versioned — every save increments
the version, and old versions are archived in `_history/`.

> **Historical note:** manifests used to carry their own test surface
> (`tests[]`, later `test_command` / `test_cases[]`, a test scheduler, and a
> forge-time test gate). That surface was removed on 2026-06-08 after a usage
> audit (2% adoption across 77 manifests, two parallel dead implementations) —
> see [docs/decision-app-tests-2026-06-08.md](decision-app-tests-2026-06-08.md)
> for the rationale and the full coverage mapping. Health coverage moved to
> the audit + coherence framework described below. The deprecated `test_*`
> fields may still appear in older on-disk manifests; they are inert.

---

## Application priorities

Every manifest has a `priority` field: `core`, `feature`, or `optional`. It is
an operator-facing triage label — the Apps page highlights `core` applications,
and the recommended review cadence (below) keys off it.

- **`core`** — fundamental to the bot's purpose. If this application is broken, the bot isn't doing its job.
  Examples (personal assistant bot): calendar integration, messaging, health tracking.
  Examples (team/project bot): Slack communication, task management, project tracking.
- **`feature`** — significant application worth monitoring. The bot is less useful without it, but it's not mission-critical.
  Examples: document generation, CAD workflow, web search, email sending.
- **`optional`** — nice-to-have. A failure doesn't block core bot function.
  Examples: entertainment recommendations, travel research, expansion suggestions.

> **2026-06-08:** priorities previously drove application-test scheduling and
> escalation (regression cadence, budget-based skips, failure severity); that
> went away with the app-test surface (see the historical note above).

---

## The manifest schema

> **Note:** The trimmed example below shows the long-lived descriptive fields
> only. The current schema is v20 (`MANIFEST_SCHEMA_VERSION` in
> `applications/manifest.py`) and carries many more operational fields —
> `files[]` provenance records, `crons[]`, `skills[]`, `requirements`,
> `interface_contract`, `usage`, gallery/forge provenance, and more. See
> [docs/manifest-spec.md](manifest-spec.md) for the field reference.

```json
{
  "id": "slack-comms",
  "name": "Slack Communication",
  "bot_id": "team-bot-a",
  "version": 2,
  "schema_version": 20,
  "status": "active",
  "priority": "core",

  "description": "Team-bot-a communicates with the Example Corp team via Slack. Sends weekly reports, responds to questions, posts project updates to #project-x.",

  "goals": [
    "Send weekly PROJECT-X project updates to #project-x every Monday",
    "Respond to team questions about project status within 1 hour during work hours",
    "Post task completions and blockers to the appropriate channel"
  ],

  "success_metrics": [
    {
      "name": "weekly_report_sent",
      "description": "Weekly report was sent on Monday",
      "measurement": "Check Slack for message from Team-bot-a in #project-x with date in past 7 days",
      "target": "At least 3 of 4 recent Mondays",
      "metric_type": "quantitative"
    },
    {
      "name": "response_quality",
      "description": "Team reactions to Team-bot-a's messages are positive",
      "measurement": "Average reaction score from slack_signals.py",
      "target": "> 0.50 average quality score",
      "metric_type": "quantitative"
    }
  ],

  "privacy_constraints": [
    "Never share team members' personal information in Slack",
    "Slack message content stays in Slack — don't copy to workspace files"
  ],

  "known_issues": [
    "Slack rate limits can delay messages by up to 60s during busy periods"
  ],

  "desired_improvements": [
    "Thread replies instead of new messages for follow-up questions"
  ],

  "satisfaction_score": 4,
  "satisfaction_notes": "Works well, occasional timing issues with weekly report",

  "created_at": "2026-04-05T00:00:00Z",
  "approved_at": "2026-04-05T00:00:00Z",
  "last_reviewed_at": "2026-04-05T00:00:00Z"
}
```

---

## Creating a manifest

### Option 1: Auto-scan (recommended for first-time)

```bash
evolve-admin application scan admin-bot
```

The scanner is a Python-orchestrated pipeline (LLM answers questions, never
drives — see `applications/scanner.py`):

1. **Inventory** — filesystem walk, crontab, memory files, scheduled tasks. No LLM.
2. **LLM discovery** — clusters the workspace evidence into candidate applications (tier3).
3. **Merge** — deduplicates results against each other and existing manifests.
4. **Manifests** — generates a full manifest per discovered application (tier3, parallel).
5. **Post-passes** — file registration, layer classification, reconciliation
   against reality, and a coherence Pass A check on each manifest.

LLM failures fall back to stub manifests, each manifest is written atomically,
and a re-run skips already-saved manifests. `--no-llm` skips the LLM phases
(deterministic fallbacks only); see `evolve-admin application scan --help` for
the other knobs (`--min-confidence`, `--auto-approve`, `--dedup-existing`, …).

### Option 2: Manual creation

```bash
evolve-admin application new admin-bot
```

Interactive prompts for the descriptive fields. Takes about 5 minutes per application.

### Option 3: Direct file creation

Write the JSON to `~{bot}/.openclaw/workspace/manifests/{application_id}.json`
and set `status: "active"`.

---

## How application health is checked

Manifests are not self-testing — health coverage comes from the audit +
coherence framework, which reads the manifest's claims and verifies them
against reality. Findings land in the Signal store and surface on the
Alerts page.

**Structural audit (Tier 2)** — `packages/analyzer/app_audit_structural.py`,
driven by the hourly `ai.evolve.evolve.audit-scheduler` daemon. Deterministic
checks of manifest claims: files in `files[]` exist and match their recorded
sha, crons are installed with parseable schedules and existing scripts, the
most recent run of each claimed cron was healthy, required Python packages
import in the bot's environment, the app is discoverable to the bot's LLM,
and bootstrap cost is reasonable.

**Coherence passes** — `packages/admin/evolve_admin/applications/coherence_pass_*.py`:

- **Pass A** — structural coherence of the manifest itself: recurring claims
  have triggers, inputs/outputs have producing mechanisms, crons reference
  files the manifest owns, no orphan files, declared integrations are used.
- **Pass C1** — AST-grounds the manifest's claims in the actual code: the
  scheduled-action target parses and the integration shape matches the claim.
- **Pass C2** — LLM judge of "does the code do what the manifest claims",
  folded into the monthly Tier 3 audit.
- **Pass C3** — design plausibility ("could this manifest work at all?"),
  fired on charter change, forge approval, and on demand.

**Forge coherence gate** — `validate_coherence_gate` refuses forge approval
when Pass A finds the manifest incoherent (override key available). This is
the only gate at forge time; the old test gate is gone.

**Integration health** — credential/liveness checking is per-integration, not
per-app: the `gmail_integration_health` monitor probes Google credentials
every 30 minutes, and `integration_probe` checks channel/gateway liveness at
UI time. (Per the decision memo, periodic credential probes for non-Google
providers are a known gap, to be filled as a pod-wide monitor — not as
manifest fields.)

---

## Application ID naming conventions

- `kebab-case` always
- Noun-first: `health-tracking`, `slack-comms`, `calendar-integration`
- Not too specific: `protein-tracking` is too narrow (use `health-tracking`)
- Not too broad: `bot-functionality` is useless

**Standard application IDs (use these for consistency across bots):**

| ID | What it covers |
|----|---------------|
| `slack-comms` | Sending/receiving Slack messages |
| `calendar-integration` | Reading/writing calendars |
| `email-management` | Reading/sending email |
| `task-management` | Task queue, tracking, completion |
| `health-tracking` | Nutrition, supplements, fitness logging |
| `document-generation` | Creating DOCX, PDF, or structured output |
| `cad-workflow` | CadQuery scripts, STEP file generation |
| `web-search` | Brave Search or similar |
| `home-management` | Repair tracking, vendor management |
| `creative-writing` | Novel, writing projects |
| `travel-planning` | Itineraries, bookings, research |
| `slack-signals` | Team-bot-a's Slack quality monitoring (meta-application) |

---

## How manifests drive analysis

Beyond the audit + coherence checks above, the manifest feeds the broader
analysis engine:

**Weekly analysis (analyze.py):**
- `detect_low_satisfaction_application()` — reads `satisfaction_score`, cross-references session summaries for that application's domain; generates proposals when satisfaction is low AND session summaries show evidence of problems

**Expansion engine:**
- Compares session topic clusters against known application IDs to find gaps

**Outcomes tracking:**
- Applied proposals for an application feed `outcome.py`'s 7-day check-in

---

## Updating a manifest

Edit the manifest in the Apps page editor in the admin UI, or edit the JSON
directly. The version field is incremented automatically on save, and the old
version is archived to `manifests/_history/{application_id}-v{n}.json`.

---

## Reviewing manifests periodically

Manifests go stale. The bot's actual behavior drifts from the manifest's expectations over time.

Recommended review cadence:
- `core` applications: review every 3 months
- `feature` applications: review every 6 months
- `optional` applications: review annually or when the underlying feature changes

During review, update:
- `satisfaction_score` — has the experience improved or degraded?
- `known_issues` — are old issues fixed? Are there new ones?
- `desired_improvements` — what should Evolve propose next?
- Open audit/coherence findings — are the manifest's claims still grounded in reality?

```bash
evolve-admin application list admin-bot  # See all manifests and last review date
```
