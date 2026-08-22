---
title: "Help: Apps Page"
slug: apps
audience: public
last_reviewed: 2026-08-20
concepts:
  - applications
  - forge
  - app-install
  - app-audit
  - app-gallery
  - app-reliability
  - app-coherence
  - app-reconciliation
  - app-provenance
ui_surface: admin.apps
related_specs:
  - docs/spec-app-derived-permissions-2026-05-24.md
  - docs/spec-app-coherence-and-reconciliation-2026-06-05.md
---

# Help: Apps Page

The Apps page (in the **Improve** bucket) is where you manage the applications your bots
run and the forge jobs that build them. It is **pod-first**: the page shows one row per app
across the whole pod, and the bots that have it are a column and a filter rather than a tab
bar.

Four tabs, following an app's life:

| Tab | What it holds |
|---|---|
| **Apps** | Every *defined* app on the pod, once — name, purpose, bots it is installed on, how it runs, when it last ran, cost over 7 days, status. Click a row for **App detail**: the same facts per bot, plus the signals raised about that app. |
| **Discovered** | Drafts — things a bot appears to be doing that nobody has vouched for yet. Promote one to make it a real app, or set it aside. *Sync all bots* (discovery + manifest check) lives here. |
| **Gallery** | Ready-made app packages you can install, import, and publish. |
| **Activity** | What has happened to your apps — builds, installs, promotions, publishes — newest first, with outcomes. The forge's own in-flight table and its approval panel are on this tab. |

**+ New app** (top right) opens the wizard, where you describe an app in plain language.

**What the page will not tell you.** Where the pod cannot measure something, the cell says so
— *not measured* (no per-app usage has been recorded for that bot yet) and *can't measure*
(nothing records whether the app ran; per-run history is still being built). Neither means
zero, and the page never guesses a verdict from a missing number. Two columns on
**Discovered** — readiness and the offer a bot has made to its user — read *not yet scored*
and *not yet offered* because those mechanisms are still being built.

**Coherence + Reconciliation surface.** An app's **Coherence** and **Drift** detail is in the
per-bot raw record (*Open raw instance* on App detail) — the operator-facing surface for the
coherence + reconciliation framework shipped 2026-06. The full breakdown is in the
[manifest modal](#coherence--drift-per-app-in-the-manifest-modal) section below.

**Ask evo to install or audit.** Chat shortcuts that work from this page:

- *"install morning-brief on team-bot-a"* — direct gallery install via
  `app_action(action="install")`. Forge runs the install asynchronously; evo polls
  the job status for you.
- *"audit team-bot-a's morning-brief app"* / *"audit all apps on team-bot-a"* — kicks the
  Tier-3 semantic audit; regressed findings surface as new firing signals.
- *"check the status of job j-abc12345"* — `pod_state(query="forge_job")` returns
  the job's current step + completed steps.

---

## Apps tab — app manifests

Behind every app on this tab is an **app manifest** — the structured contract that defines
what a bot is supposed to do, which files implement it, how to verify it's working, and
whether it's improving over time. Manifests are the source of truth for every app running on
a bot; App detail is the readable view of one, and *Open raw instance* shows the record
itself.

> The sections below describe the manifest and the actions that operate on it. They were
> written when this tab was called **Installed** and showed one card per bot; the manifest
> content and the actions are unchanged — only where you click has moved.

### What an App Manifest Is

A manifest is a JSON document that answers four questions about one named application:

1. **Identity** — What does this app exist to do, what's in scope, what's explicitly out of scope?
2. **Success criteria** — What observable outcomes prove it's working? What are the failure signals?
3. **Constraints** — What privacy rules, safety limits, and dependencies apply?
4. **Satisfaction** — How well is it actually working for you right now?

These four sections are called the **RSI core** and feed directly into the Better Engine. The analysis engine reads them to understand what "working" means for each app, runs the app's test suite, and generates proposals when tests fail or satisfaction scores are low.

Beyond the RSI core, manifests also carry:
- A **files registry** listing every file the app owns or shares, with stable IDs
- A **crons list** for scheduled tasks belonging to the app
- **Source provenance** recording how and when the manifest was created
- **Timestamps** for when it was created and last updated

### How Manifests Are Created

Every manifest has a `source` field recording its origin. This matters because it affects trust level, quality expectations, and how the system handles it.

| Source | How it happened |
|--------|----------------|
| `discovered` | The workspace scanner found the app by analyzing cron jobs, workspace directories, scripts, and data files. This is the most common source for pre-existing bots. |
| `user_created` | You built it manually in the admin UI. |
| `bot_created` | The bot was instructed to build an app (via a direct instruction or conversation), and created its own manifest as part of that work. |
| `rsi_proposed` | The Better Engine's RSI loop proposed and auto-generated it based on usage patterns. |
| `file_imported` | You uploaded a manifest JSON directly (import workflow). |
| `gallery_installed` | Installed from the App Gallery via a forge run. This source also sets a stable `pkg_id` linking the manifest to its gallery blueprint. |

The `source_detail` field carries additional context — for discovered manifests it's the scan timestamp, for forge-built manifests it's the forge job ID.

All manifests also record `created_at` (when the manifest was first created) and `updated_at` (when it last changed). These are ISO 8601 UTC timestamps and are always populated.

### Component File Provenance

Every file that belongs to an app — scripts, data files, markdown logs, JSON stores — carries an embedded **provenance marker** that links it back to its manifest. This creates a bidirectional reference:

- **Manifest → files**: the manifest's `files` list records every component file with its path, stable ID, layer, and ownership
- **File → manifest**: each file has an embedded marker in its first lines pointing back to the owning app(s)

#### What markers look like

The marker format varies by file type but is always in the first few lines:

```python
# evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15.1
```

```markdown
<!-- evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15.1 -->
```

```json
{"_evolve": {"pkg": "p-a3f91c8b@2026.04.15-1.3", "file": "f-d4e8f901@2026.04.15.1"}, ...}
```

`pkg` is the app's stable package ID (`p-` prefix). `file` is that specific file's stable ID (`f-` prefix). Both carry version info.

#### Files shared across apps

A file can belong to more than one app. When that happens, all owning app IDs appear in the marker, comma-separated:

```python
# evolve: pkg=p-a3f91c8b@2026.04.15-1.3,p-b2e04d1a@2026.04.10-1.1 file=f-d4e8f901@2026.04.15.1
```

The file entry in each manifest records `owned_by` (the primary app that created it) and `shared_with` (other apps that use it). The global file index at `/Users/Shared/evolve/file_index.json` maps every file ID to its current location, bot, ownership, and lifecycle state.

### File Layers

Every file in a manifest's registry has a `layer` that describes its role. Layer determines removal behavior when an app is deleted.

| Layer | What it is | Safe to remove on app deletion? |
|-------|-----------|--------------------------------|
| `script` | Executable Python or shell logic | Yes — pure logic, no user value without the app |
| `skill` | Routable procedure invoked by the bot | Yes |
| `policy` | Operator-tunable behavioral constraints | Yes |
| `orchestrator` | Thin coordinator/dispatcher | Yes |
| `test` | Test files | Yes |
| `reference` | Documentation files | Ask — may be user notes, not just app docs |
| `data` | Structured data stores (JSON, etc.) | **No** — may contain records the user still needs |
| `state` | Living state maintained by the bot (markdown logs, task lists, etc.) | **No** — user data outlives the app |

When you remove an app, Evolve removes its logic files but **never auto-deletes `data` or `state` layer files**. Those are surfaced to you as unowned files for you to decide what to do with.

### File Lifecycle States

Every registered file has a lifecycle state that reflects its current relationship to the app system:

| State | What it means |
|-------|--------------|
| `owned` | Marker present, owning app is active |
| `shared` | Marker present, owned by one active app and used by at least one other |
| `orphaned` | Marker present but one or more of the app IDs in the marker no longer have a live manifest |
| `unowned` | No marker — file predates the provenance system, was never claimed, or was a `data`/`state` file whose app was removed but whose contents were preserved |

Lifecycle states are visible in the file index and surfaced in the admin UI's orphan scan results.

### How Discovery Works

When you click **↻ Re-scan** on the Installed tab, Evolve runs a 5-phase pipeline:

**Phase 1 — Inventory (~5s)**
Reads the bot's workspace: cron jobs with script content, named directories, recurring memory/log files, JSON data stores, Python and shell scripts, markdown files, and identity files (SOUL.md, AGENTS.md).

**Phase 2 — LLM discovery (~35s)**
An LLM reads the inventory and clusters workspace signals into named applications. Cron jobs are the strongest signal — if a user configured a cron, it almost certainly serves an app. Named directories and recurring data files are also strong. Infrastructure scripts (gateway health, backup, git commits) are filtered out.

**Phase 3 — Merge (~1s)**
Deduplicates LLM results by evidence overlap and name similarity. Manifests that clearly describe the same app are merged.

**Phase 4 — Manifest generation (~20s, parallel)**
For each newly discovered app, generates a full 4-section RSI manifest using the file content as context. Falls back to a minimal stub if the LLM call fails. Already-existing manifests are not overwritten.

**Phase 5 — File stamping**
For each newly generated manifest:
1. Assigns a stable `pkg_id` if the manifest doesn't have one
2. Resolves the evidence files to real on-disk paths
3. Assigns a `file_id` to each found file and infers its layer from the file extension
4. Embeds a provenance marker in each file (merge-aware — existing markers from other apps are preserved)
5. Re-saves the manifest with the complete v6 files registry

After scanning, the global file index is rebuilt to reflect all new ownership records.

**Gallery apps** go through a different path (forge run #0) but end up with the same structure: a manifest with full RSI core, a files registry with `file_id` entries per file, and provenance markers in every generated file.

### The Application Grid

Shows all manifests for the selected bot, filterable by:
- **Status** — Active, Draft, Paused, Dormant, Archived (use **Show archived** to include Archived in any view)
- **Health** — Passing (all tests green), Failing (1+ tests red), No tests (untested)

Each card shows:
- App name and stable ID (e.g., `health-tracking`, `slack-comms`)
- Source badge — where this manifest came from (`discovered`, `gallery`, etc.)
- Test health summary (X/Y tests passing)
- Satisfaction score (1–5 stars, set by you)
- Last scan / created / last reviewed dates

Click a card to expand it and see identity, success criteria, constraints, tests, files registry, and improvement history.

### App Removal and Data Preservation

Removing an app from the Installed tab triggers a careful teardown sequence:

1. The manifest is deleted
2. Each file in the manifest's registry is evaluated by layer:
   - `script`, `skill`, `policy`, `orchestrator`, `test` — offered for deletion
   - `reference` — flagged for your decision
   - `data`, `state` — **preserved unconditionally**, their markers are stripped, they become `unowned`
3. For files shared with other apps: the removed app's `pkg_id` is removed from the marker, but the file stays and retains its other owners
4. The global file index is rebuilt to reflect the new lifecycle states
5. Any files now in `unowned` state are surfaced in the UI for you to assign, archive, or leave

**Example**: you install a medication reminder app that logs to `memory/medications.md`. Later you remove the app because you don't want the reminders. The reminder script is offered for deletion. `medications.md` is `state` layer — it's preserved as an unowned file. A future app can claim it.

### Coherence + Drift (per-app, in the manifest modal)

Every app card on the Installed tab carries two chips along its top edge:

- **Coherence chip** — does the manifest's claims hang together internally? (Does the description claim a recurring behavior with no cron? Does a declared output have no code path producing it?)
- **Drift chip** — does what's on disk match what the manifest says it owns? (Has the bot edited a file? Added one? Removed one?)

Click either chip to open the manifest modal scrolled to the **Coherence + Drift** section. Empty when there's nothing to report; populated when the audit framework's coherence passes (PR 2270 Pass C1 / PR 2271 Pass C2+C3) or the reconciliation pass find something.

The section is the operator-facing surface for `spec-app-coherence-and-reconciliation-2026-06-05.md`. Three concepts cleanly separated:

| Concern | Question | Where it surfaces |
|---------|----------|-------------------|
| **Reconciliation** | Does the manifest match what's on disk? | Drift chip + Reconciliation drift block |
| **Provenance** | Was this field operator-authored, or did the scanner observe it? | Per-field affordance (the "authored" pill on drifted rows); modulates whether a drift becomes operator-facing |
| **Coherence** | Does the manifest describe something that could work? | Coherence chip + Coherence findings block |

**Pass A** walks the manifest graph in pure Python (description claims a behavior → does a mechanism produce it?). **Pass C1** is static analysis of the bot-authored code shape. **Pass C2 + C3** are LLM-tiered checks that run only when A and C1 surface ambiguity — cheap-handles-routine, expensive-handles-complex.

#### Actions on findings

The Coherence + Drift section header has a row of action buttons:

- **Approve drift** — accept the current observational state. Reconciliation drift clears; the manifest's observational fields catch up to disk.
- **Promote to authored** — flip observational provenance entries to `bot_authored`. The fields stop being "snapshots" and start being "contract." Next time disk drifts away from these fields, the operator gets a normal drift proposal rather than a silent re-sync.
- **Flag…** — escalate a concern to the operator. Useful when you're not sure what to do and want to come back to it.
- **Repair (coming soon)** — will route a repair session to the bot's `audit_inbox/` once the audit_runner `--repair` mode lands. The bot fixes the discrepancy itself; you approve the result.
- **Override pre-deploy gate…** *(only when status is `incoherent`)* — bypass the pre-deploy coherence gate (PR 2263 / PR 2325) for the current finding set. Use sparingly; the gate exists to keep visibly-broken manifests off the bot.

Per-finding affordances (**Mute** / **Snooze 7d**) live next to each row. Mute is durable until you re-enable; Snooze is a 7-day suppression for findings you intend to fix.

**Bot-side coherence repair.** When you approve a drift or accept a finding, the bot itself can be tasked to land the change (PR 2336) — the admin daemon dispatches a repair turn into the bot's session and you can watch the result in Forge Jobs. The same grammar is reachable via chat: *"evo app-changes"* lists pending coherence findings across all apps; *"evo app-coherence team-bot-a morning-brief"* drills into one app.

### Orphan Detection

The **Orphan Scan** (available from the Installed tab) runs two passes against a bot's workspace:

**Forward scan** — checks that every file registered in a manifest actually exists on disk. Files listed in the manifest but missing from disk are `missing_from_disk` issues.

**Reverse scan** — walks all workspace files with embedded markers and checks whether every `pkg_id` in those markers corresponds to a live manifest. Files whose markers point at removed apps show up as `orphaned`.

Orphan scan results show each affected file with:
- Its path and file ID
- Which pkg_ids are stale (removed app) vs. which are still live
- Its layer (to indicate whether it's safe to remove)
- A `safe_to_remove` flag — True only for non-data, non-state layer files with no remaining live owners

You decide what to do with each orphaned file. Evolve doesn't auto-delete.

### Common Questions — Installed tab

**How do I create a manifest for a new app?**
Option 1 (recommended): Click **↻ Re-scan** — the scanner detects apps from workspace evidence and generates manifests automatically, including assigning IDs and embedding markers in all found files.

Option 2: Build it in the Gallery tab using Forge — install a gallery blueprint and the forge run creates everything from scratch.

Option 3: Click **+ New App** to open the inline create wizard — collects name, description, a test trigger phrase, and the expected response. The wizard scaffolds a minimal manifest and lands a Forge job to flesh it out.

Option 4: Create the JSON manually and place it at `{shared_dir}/applications/{bot_id}/{app_id}.json`. If you do this, run a re-scan afterward so Phase 5 can assign file IDs and embed markers.

**What does the `pkg_id` on a manifest mean?**
It's the app's stable package identity — a random 8-character hex ID with a `p-` prefix (e.g. `p-a3f91c8b`). It never changes, even if the app is renamed or restructured. For gallery apps, the `pkg_id` is assigned at gallery creation and stays the same across all bots that install it. For discovered apps, it's assigned at scan time. The `pkg_id` is what gets embedded in every component file's marker.

**What's a `file_id` and do I need to worry about it?**
A `file_id` is a stable `f-` prefixed ID assigned to each file the first time it's registered. It's embedded in the file's provenance marker and used by the global file index. You don't need to manage these — they're assigned and maintained automatically. If you see them in manifest JSON, that's normal.

**A file I edited now shows a version mismatch in the orphan scan — is that a problem?**
No. Version drift (where the file's embedded version doesn't match the manifest's recorded version) is a warning, not an error. It means the file was edited outside of a forge run. The Better Engine will detect this and may propose updating the manifest to reflect the current state. The app still works.

**Can a file belong to two apps?**
Yes. When app B needs a file that app A already owns, B's `pkg_id` is added to the file's marker alongside A's. The manifest has a `shared_with` list for these. The file index tracks both. This is how shared utilities and shared data stores work.

**An app shows "Failing" — what does that mean?**
One or more of its tests failed on the last test run. Click the app card to see which tests failed and why. Common causes:
- API token no longer valid
- File the test checks no longer exists (workspace restructure)
- Bot behavior drifted from what the behavioral test expects

The analysis engine will generate a proposal about persistent failures (2+ consecutive for `feature` priority, 1 for `core`).

**How do I update a manifest?**
Edit it in the admin UI (click the card, then Edit) or directly in the JSON at `{shared_dir}/applications/{bot_id}/{app_id}.json`. Changes made outside the UI won't re-embed markers in files — run a re-scan afterward to keep everything in sync.

**What's the satisfaction score?**
Your 1–5 rating of how well this app is working for you. The analysis engine uses it: a score of 3 or below, combined with session evidence of corrections or failures in this domain, triggers a proposal. Rate honestly — a 4 with no issues is better than a 3 with vague notes.

**How often should I review manifests?**
- `core` apps: every 3 months
- `feature` apps: every 6 months
- `optional` apps: annually or when the underlying feature changes

Manifests go stale as bots evolve. Stale tests that always pass (or always fail) are noise that degrades proposal quality.

**The re-scan found an app I don't recognize — what is it?**
The scanner may infer an app from a recurring data file or cron job you forgot about. Click the card to see its evidence files — those are the workspace files the scanner used to identify it. If it's not a real app (e.g. it inferred a "system administration" app from infrastructure scripts), you can delete the manifest. The files it references won't be touched.

---

## Gallery tab — installable blueprints

The Gallery tab is a library of packaged app blueprints — Morning Brief, Email Manager, Home Controller, Travel Assistant, EA Pack, and more. Installing a gallery app is Forge run #0: the bot builds everything in its own environment from a spec, not pre-packaged code.

### How Gallery Apps Work

Installing a gallery app is not "copy files to the bot." It's a **forge run** — the bot receives a detailed spec and builds the application from scratch in its own workspace. This means:

- Two bots installing the same app produce different implementations, each suited to their workspace and context
- The bot owns the code it generates — it can maintain, modify, and improve it
- Every subsequent improvement cycle is another forge run with richer usage data
- All artifacts carry embedded provenance markers linking them to the app (`pkg_id`) and the specific file identity (`file_id`) — see the Installed tab section above for how this works

### App Grid

Browse available apps. Each card shows:
- App name and description
- Category (productivity, communication, home, health, etc.)
- Install status per bot (installed / not installed)
- Skill count badge (e.g. **4 skills**) — apps with multiple routable applications show a count
- Version / last updated

**Search** — filter by app name or description.

**Status filter** — show All, Installed only, or Not installed only.

**↻ Refresh** — reloads the gallery app list.

### App Detail View

Click an app card to open the detail panel. Tabs:

**Description** — objective, tags, author, version history.

**Skills** — each skill the app contributes, with its name and routing description (the signal the bot uses to decide which skill to invoke for a given message). Apps with multiple skills show the full roster here.

**Files** — all files the forge will create, color-coded by layer:
- `skill` (blue) — routable procedure
- `policy` (orange) — operator-tunable config
- `state` (yellow) — live data the bot maintains; **preserved if the app is later removed**
- `data` (green) — structured data stores; **preserved if the app is later removed**
- `orchestrator` (purple) — thin dispatcher
- `script` (gray) — code files

Layer matters for removal: logic files (`script`, `skill`, `policy`, `orchestrator`) can be safely deleted when an app is removed. Data and state files are always preserved — they may contain user records that outlive the app.

**Crons** — scheduled turns, showing session target (`main` or `isolated`) and delivery disposition (silent or announce to channel).

### Installing an App

1. Click an app card to open the detail view
2. Review the Skills, Files, and Crons tabs to understand what the app creates
3. Click **Install**
4. The Install modal appears — select which bot(s) to install on
5. If the app has placeholder values (e.g. a Telegram chat ID for delivery), a form appears to collect them before install
6. Click **Install** — this creates a Forge run

Before the forge builds anything, it runs a **skill discovery scan**: it queries the bot's file index for existing skills that overlap with what the app needs. If a suitable skill already exists, the forge adopts it as a shared dependency rather than building a duplicate. You'll see this step in the Forge Jobs tab.

The Forge run appears in the **Forge Jobs tab**. Installation takes minutes to hours depending on the app's complexity. You can track progress there.

**Note:** Installation requires:
- The target bot's gateway to be running
- Relevant API keys configured (the app description lists what's needed)
- Your approval at the forge review gate (you'll be prompted in Forge Jobs when the bot needs a decision)

### Importing an App

Click **+ Import App** to import an app from a JSON package. This allows sharing custom apps or importing from other Evolve installations.

Paste the app package JSON into the textarea and click **Import**. The app appears in the gallery and can be installed on any bot.

### Common Questions — Gallery tab

**What apps are available?**
The gallery includes:
- **EA Pack** — core personal assistant applications (calendar, email, tasks, health tracking)
- **Morning Brief** — daily summary delivered via your messaging channel
- **Email Manager** — structured email triage and response drafting
- **Home Controller** — Home Assistant integration for smart home control
- **Travel Assistant** — itinerary building, booking research, trip planning
- And more as the gallery grows

**How long does installation take?**
Varies by app complexity. A simple single-application app might complete in 5–10 minutes. A full EA Pack with multiple integrated applications can take 30–60 minutes. Monitor progress in the Forge Jobs tab.

**The install button is greyed out — why?**
The Install button may be disabled if:
- The bot's gateway isn't running (check Maintenance → Alerts)
- The bot doesn't have required API keys (check Plugins)
- A forge run for this app is already in progress on this bot (check Forge Jobs)

**Can I install the same app on multiple bots?**
Yes — in the Install modal, check all the bots you want to install on. Each gets an independent forge run, producing a separate implementation suited to each bot's workspace.

**What happens to the app if I upgrade Evolve?**
Apps installed by forge runs are part of the bot's workspace — they're independent of Evolve's version. Evolve upgrades don't touch installed app code. The forge system can propose improvements to existing apps through the normal Better Engine cycle.

**Can I modify an app after installation?**
The bot owns its installed app code. You or the bot can modify it directly in the workspace. However, Evolve's forge system may later propose improvements based on usage data — those go through the normal proposal/approval flow and will update the relevant files.

---

## Forge Jobs tab

The Forge Jobs tab tracks all active and recent forge runs — app installations from the gallery, improvement cycles for existing apps, and proposal validation runs. Each row is a forge job showing what's being built, which bot is building it, and what step it's on.

### What is a Forge Run?

A forge run is a structured, multi-step process where a bot builds or improves an app in its own workspace. There are three types:

- **Forge run #0 (Installation)** — triggered by installing from the Gallery tab. The bot receives a detailed spec and builds the app from scratch.
- **Improvement cycle** — triggered by the Better Engine when a proposal is approved. The bot refines an existing app based on usage data and proposal context.
- **Validation run** — triggered by the proposal pipeline. The bot validates a proposed change against its test suite in an isolated environment.

### The Jobs Table

| Column | What it shows |
|--------|--------------|
| **App** | The app being built or improved |
| **Bot** | Which bot is running this forge job |
| **Type** | `install` / `improvement` / `validation` |
| **Progress** | Where in the forge process this job is right now |
| **Status** | `running`, `awaiting_approval`, `completed`, `failed`, `rejected` |
| **Started** | When the forge run began |
| **(actions)** | Approve / reject buttons when awaiting your input |

**↻ Refresh** — reloads the jobs list.

### Forge Job Lifecycle

```
Created → Running (bot is building)
       → Awaiting Approval (bot needs your input at a review gate)
       → Running (resumed after approval)
       → Completed (success)
       → Failed (error; see logs)
       → Rejected (you rejected at review gate)
```

Most forge runs have one or more **review gates** — points where the bot has produced something (an implementation plan, initial code, test results) and needs your confirmation before proceeding. These appear as `awaiting_approval` with an **Approve or Reject Job** panel.

### The Approval Panel

When a job is awaiting your approval, clicking it shows the full context:

- **Metrics bar** — what was built, file count, line count, test pass/fail summary
- **Test output** (collapsible) — the full test runner output. The summary line indicates whether tests passed.
- **Generated implementation** (collapsible) — the actual code/manifest the bot produced for this step.
- **Interface contract** (collapsible) — the contract/spec the bot is committing to honor (skills it provides, files it owns, etc.).
- **Notes (optional)** — any feedback you want the bot to incorporate in the next step

**✓ Approve** — the forge run continues to the next step.

**✗ Reject** — the forge run stops. A **Rejection reason** textarea appears; provide a reason so the system can learn what didn't work. The rejection is logged and feeds back into the Better Engine.

### Common Questions — Forge Jobs tab

**A forge job has been "running" for a long time — is it stuck?**
Forge jobs can legitimately take a long time (30–60 minutes for complex installs). However, if a job has been in `running` state for more than 2 hours without any progress, check Gateway Logs in Maintenance for the bot to see if there's an error. If the bot's session appears stuck, you may need to restart the gateway.

**What happens if I reject a forge job?**
The job stops. Your rejection reason is logged. If this was an improvement proposal, the proposal is marked as rejected and this outcome feeds into the Better Engine (the coach that generated the proposal may be recalibrated).

**How many forge jobs can run simultaneously?**
One per bot at a time. If two apps are being installed on the same bot, the second will queue until the first completes.

**A forge job shows "Failed" — what do I do?**
Click the job row to see the error. Common causes:
- Bot ran out of context during a complex build step (try again; the bot may do better with a fresh context)
- Required API key missing or expired (check Plugins)
- File write permission error (check Maintenance for permission issues)

Failed jobs can usually be retried by installing the app again from the gallery, or re-approving the proposal that triggered the job.

**What does "validation run" mean?**
Before applying an approved improvement proposal to a production bot, Evolve validates it on the Forge bot (an isolated OC instance). The forge validation run deploys the change there, runs the app's test suite, and only proceeds to production if tests pass. You may be asked to approve the production application step.

**Can I see what the bot actually built?**
The forge run logs (in Gateway Logs for the target bot during the job) show the full session. After completion, the installed files appear in the bot's workspace at the paths specified in the app spec. Every file the forge creates has an embedded provenance marker in its first lines linking it back to the app's `pkg_id` and a stable `file_id`. These markers are how Evolve tracks ownership, detects orphaned files, and safely handles app removal. See the Installed tab section above for the full provenance model.
