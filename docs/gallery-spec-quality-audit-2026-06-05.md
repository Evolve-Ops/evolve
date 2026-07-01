# Gallery spec quality audit — 2026-06-05

> **Post-backfill addendum — 2026-06-06.** The cause of the empty fields catalogued below turned out to be a two-bug stack in the gallery publisher (`migrate_v7.py` / `migrate_v7_backfill.py`), not under-specified sources. The in-repo schema-5 sources at `gallery/<name>/p-*.json` were already populated; the early-2026-05-20 migration ran with a translator that dropped most fields, and the backfill that fixed it forward only covered `description` + `identity`. Fixed across PR #2230 (translator union of `interface_contract.cli` + `data_files` + `## FILE:` blocks + `requirements.integrations`) and PR #2233 (broaden backfill to all PR #1471 passthroughs + new translated fields + add a repo-gallery walk). After running the apply on 2026-06-06: **the 8 "Skeletal" specs in the prioritization below all graduated to "Good" with no per-spec rewrite work.** Email Integration spot-check: 3 `blueprint.files`, 1 `dependencies.integrations`, 1 `scheduled_actions`, full `identity`/`description`/`constraints`/`example_triggers`. The 2 "Hollow" specs (Morning Briefing + EA Pack) still need the product calls described below — they cannot be mechanically lifted. Three slug-style pkg_ids (`p-calsummary01` / `p-emailtriage01` / `p-note01taker`) were renamed to conformant `^p-[a-f0-9]{8}$` form in the cleanup sweep PR: pkg_id table values are updated accordingly.

> **Final-state addendum — 2026-06-06 (later).** Both Hollow rewrites landed and chose Path A. **Morning Briefing** (`p-a9a74bf7`, #2252) was rewritten as a scope-down composer that reads data files produced by Calendar Sync + Email Integration and asks the bot's LLM to compose one tight message — the broad weather/news/multi-integration scope was dropped. Post-rewrite: 11 `scope_includes`, 5 `blueprint.files`, 1 launchd `scheduled_actions`, full `success_criteria`, 5 `example_triggers`, 4 `test_cases`. **EA Pack** (`p-aab5e569`, #2247) was rewritten as a thin meta-spec that depends on three new single-purpose behavior specs: **Evening Sweep** (`p-1d3e8f47`), **Pre-Meeting Brief** (`p-2c7a9b6e`), and **Commitment Tracker** (`p-3b4f5d29`). Each new behavior spec meets the Atlas reference bar independently (e.g. Evening Sweep ships 8 `scope_includes`, 2 `blueprint.files`, 1 launchd `scheduled_actions`, full `success_criteria`, 5 `example_triggers`, 3 `test_cases`). EA Pack itself now correctly carries 0 blueprint files / 0 scheduled_actions / 0 integrations because it doesn't run anything of its own — that's the meta-spec shape. After the operator re-ran `migrate_v7 --gallery-only --apply` + `migrate_v7_backfill --apply` on the mini, all 13 specs (the audit's original 10 plus 3 new single-purpose behaviors from the EA Pack split) sit cleanly at the Atlas reference bar. PR #2260 surfaces this richness in the admin UI's Details modal — purpose, scope, schedule, files, success criteria, safety, boundaries, test cases all visible per spec instead of a one-paragraph repeat of the tile description. **Audit thread closed.**

**Scope.** The 10 builtin gallery specs at `/Users/Shared/evolve/gallery/builtin/p-*/2026.05.20-1.0.json` after excluding `p-f9bce546` (Workspace Backup) and `p-f047a60f` (GitHub Integration), which are being removed because Evolve provides those capabilities natively.

**Reference bar.** The community-imported `p-7b26ba5e` (Atlas — Daily Digest) at `/Users/Shared/evolve/gallery/imported/`. Atlas declares 3 integrations with `check_path` / `setup_doc` / `reason`, 10 `scope_includes`, 6 `scope_excludes`, 18 top-level `files[]`, 1 `scheduled_actions[]` with a fully-rendered launchd install block, 5 `observable_outcomes`, explicit `bot_interaction_pattern`, signals, and a 30k-character `build_spec`.

**Framework yardstick.** Coherence Pass A from [docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.1](spec-app-coherence-and-reconciliation-2026-06-05.md) — an internal-consistency graph walk over the manifest. The relevant assertion for this audit is #1: *"if `description` contains a recurring-behavior phrase, then at least one of `scheduled_actions[]`, `crons[]`, `oc_heartbeat_instruction` must be non-empty — severity critical."*

**Shape disclaimer.** The builtin specs use a different schema shape than Atlas: `objective.primary` / `success_criteria.{behavioral,observable}` / `blueprint.files[]` / `dependencies.{apps,integrations,credentials,…}` / `bot_guidance[]` as section-tagged prose. Atlas uses top-level `files[]` / `requirements.integrations[]` / `success_criteria.observable_outcomes[]` / `identity` / `scheduled_actions[]` / a `build_spec` markdown string. Both target schema_version 13–14 / `manifest_shape: v7-arc(-pre)`. The audit treats the analogous fields as equivalent.

---

## Table

| App | pkg_id | deps | blueprint.files | scheduled_actions | scope_includes | integrations declared | Pass A | Gap |
|---|---|---|---|---|---|---|---|---|
| Email Integration | p-341576fa | 0 | 0 | 0 | 0 (no `identity`) | No (implied: gmail) | **Fail (A1)** | Skeletal |
| Contacts | p-4136a932 | 0 | 0 | 0 | 0 | No | Pass | Skeletal |
| Task Manager | p-9bfa1c84 | 0 | 0 | 0 | 0 | No | **Fail (A1)** | Skeletal |
| Morning Briefing | p-a9a74bf7 | 0 | 0 | 0 | 0 | No (implied: ≥4) | **Fail (A1)** | **Hollow** |
| EA Pack | p-aab5e569 | 1 app | 0 | 0 | 0 | No (implied: ≥2) | **Fail (A1)** | **Hollow** |
| Calendar Daily Summary | p-738f057c | 0 | 0 | 0 | 0 | No (implied: calendar) | **Fail (A1)** | Skeletal |
| Email Triage | p-41e4c5f4 | 0 | 0 | 0 | 0 | No (implied: gmail + gateway) | **Fail (A1)** | Skeletal |
| Journal | p-fb9141b4 | 0 | 0 | 0 | 0 | No | **Fail (A1)** | Skeletal |
| Calendar Sync | p-fe9acef3 | 0 | 0 | 0 | 0 | No (implied: calendar) | **Fail (A1)** | Skeletal |
| Meeting Note-taker | p-f14e9562 | 0 | 0 | 0 | 0 | No (implied: calendar + obsidian) | Pass | Skeletal |

**Universal pattern.** Every builtin spec has `blueprint.files: 0`, `scheduled_actions: 0`, `dependencies.integrations: 0`, `success_criteria.{behavioral,observable}: 0`, no `identity` block, `audience_scoping.approved_surfaces: 0`. The bot_guidance prose ranges from 4.3k to 22.9k characters and carries the actual build content. Forge would have to interpret bot_guidance to produce files, plist XML, integration wiring, scheduled actions, and observable outcomes at install time.

**Real-world install evidence.** Only 2 of the 10 ever shipped to a production bot: p-9bfa1c84 (Task Manager) on atlas, p-aab5e569 (EA Pack) on a personal-bot. The scanner-rebuilt manifests show that forge invented everything: Task Manager ended up with 5 files + 1 heartbeat-driven `scheduled_actions[]`; EA Pack ended up with 6 files + 3 launchd-driven `scheduled_actions[]`. Neither installed manifest's structured fields trace back to the source spec; they come from the post-install scan re-observing what forge produced.

---

## Per-app summaries

### Email Integration (p-341576fa) — Skeletal
Objective: *"Syncs Gmail into structured local files. … Updates every 30 minutes."* Pass A fails on A1: the recurring phrase is paired with empty `scheduled_actions[]` / `crons[]` / `oc_heartbeat_instruction`. bot_guidance (5.1k chars, 8 sections) includes a full `## LaunchDaemon Plist` XML block, CLI subcommands, and a test suite — forge has prose to work from. Forge must still invent: top-level `files[]`, rendered plist with `{bot_id}` substituted, `requirements.integrations[]` for Gmail with `check_path` / `setup_doc` / `reason`, and `success_criteria.observable_outcomes[]`. Tight single-integration scope.

### Contacts (p-4136a932) — Skeletal
Objective: *"Per-person relationship memory."* No recurring-behavior phrase → Pass A passes. bot_guidance (4.7k, 11 sections) covers file format, CLI, append-only invariants. Local-only CLI, no integrations. Forge must invent: file roster, `interface_contract.cli[*]`, `volatile_paths[]` for per-contact files, and observable outcomes. Closest to shippable as-is.

### Task Manager (p-9bfa1c84) — Skeletal
Objective mentions *"periodic cron reporting"* → Pass A fails on A1. Largest bot_guidance among the 10 (15.8k, 18 sections) covering storage, schema, tag normalization, and a `## task-check.sh — Cron Trigger` section. The atlas install emerged with 1 heartbeat-driven scheduled action, not launchd — a substantive choice the spec did not pin down. Forge must invent: heartbeat vs launchd vs openclaw_cron choice, plist rendering, archive rotation cadence, observable outcomes.

### Morning Briefing (p-a9a74bf7) — **Hollow**
Objective: *"Deliver a daily morning message to the operator covering schedule, email highlights, weather, and news."* The user-cited canonical Hollow example. It earns the label not because bot_guidance is short — it's 20.9k chars / 16 sections — but because the *scope* is unspecified. Forge must invent: which calendar integration, which email integration, a weather API choice (and whether a key is needed), a news source (RSS? API? aggregator?), a delivery channel (Telegram? Slack? email-back-to-self?), the cron mechanism, and email-highlights relevance scoring. Four-plus undeclared integrations and a delivery channel means every install renders differently. Needs full rewrite — likely split or scope down to a primary-bot-only pattern.

### EA Pack (p-aab5e569) — **Hollow**
Objective: *"Executive assistant capabilities: daily morning brief, evening task sweep, pre-meeting briefings, and per-person commitment tracking. Installs four coordinated scripts and two cron jobs."* Largest bot_guidance (22.9k, 15 sections) and the only spec with a non-empty structured field (`dependencies.apps: 1`). The bundle is the problem: four behaviors + two crons + cross-app dependencies on Contacts / Task Manager / Calendar Sync, only one declared. Pass A fails on A1. The personal-bot install emerged with 3 launchd actions + 6 files — forge had to choose which behaviors to wire up. Split into four single-purpose specs, or declare the full dependency graph + per-behavior `scheduled_actions[]`.

### Calendar Daily Summary (p-738f057c) — Skeletal
Objective: *"Compose the Calendar skill into a daily schedule summary delivered at 7 AM."* Pass A fails on A1. bot_guidance (10k, 14 sections) includes plist, calendar API access, message format, and template variables — clear blueprint. Missing: declared `requirements.integrations[]` for Calendar, rendered `scheduled_actions[]` for the 7 AM trigger, and observable outcomes for delivery. Delivery-channel ambiguity is the biggest unspecified piece.

### Email Triage (p-41e4c5f4) — Skeletal
Objective: *"Surface the emails worth your time — twice a day, automatically."* Pass A fails on A1. bot_guidance (9.4k, 14 sections) covers config, gmail token, classification, gateway delivery, run-result file. Single primary integration (Gmail) plus an unspecified gateway. Forge must invent: gateway choice (Telegram? bot inbox?), two scheduled-action triggers, classification thresholds, declared integrations.

### Journal (p-fb9141b4) — Skeletal
Objective: *"Daily log with morning intentions, end-of-day notes, and freeform entries."* "Daily" trips Pass A's recurring-phrase heuristic, but morning/EOD entries are typed via CLI, not cron-driven. bot_guidance (4.3k, 11 sections) covers file format, entry format, append-only rule, timezone. Forge must invent: `interface_contract.cli[*]`, `volatile_paths[]` for `journal/YYYY-MM-DD.md`, and clarification that recurrence is human-driven. Drop "Daily" from the objective, or declare `scheduled_actions[]` of `kind: user_command` per spec §3.4.

### Calendar Sync (p-fe9acef3) — Skeletal
Objective: *"Syncs Google Calendar events into structured local JSON files. Updates every 15 minutes."* Pass A fails on A1. bot_guidance (5k, 9 sections) is tight — auth, event construction, atomic write, CLI, plist, tests. Mirror of Email Integration's shape; same gaps. Single-integration scope, fully buildable from the prose.

### Meeting Note-taker (p-f14e9562) — Skeletal
Objective: *"Compose Calendar and Obsidian skills into a novel capability: meetings get a vault note automatically."* No recurring-behavior phrase — meetings drive the trigger. Pass A passes. bot_guidance (10.4k, 21 sections — most of any spec) covers CLI, active-meeting state, `## AGENTS.md guidance for the bot` (interactive flow), note format. Implied integrations: Calendar + Obsidian (a skill, not a typical OC plugin). Forge must invent: two declared integrations, an event-trigger for meeting-start, an `invocation_mode` decision for mid-meeting note instructions, and observable outcomes. Closest to a Good-class spec if rewritten.

---

## Prioritization

**Full rewrite required (Hollow — 2 specs).** Cannot ship to a public-launch gallery without rebuild:
- **Morning Briefing (p-a9a74bf7)** — split or scope down. Four-plus undeclared integrations and a delivery channel means every install renders differently.
- **EA Pack (p-aab5e569)** — split into four single-purpose specs, or declare the full app-dependency graph + per-behavior `scheduled_actions[]`.

**Structured-field population (Skeletal — 8 specs).** bot_guidance already encodes the build; the gap is contract surfaces. For each: top-level `files[]` with `layer` enum, `requirements.integrations[]` with `check_path` / `setup_doc` / `reason`, `scheduled_actions[]` (or `kind: user_command` for CLI-only), `identity` block (`purpose`, `user`, `bot_interaction_pattern`, `scope_includes`, `scope_excludes`), `success_criteria.observable_outcomes[]`, and signals where the app produces them.
- Tier A (single declared integration, plist already in bot_guidance — fastest to lift): **Email Integration**, **Calendar Sync**, **Calendar Daily Summary**.
- Tier B (single behavior, integration choices to make): **Email Triage** (gateway channel), **Task Manager** (heartbeat vs launchd reporter), **Meeting Note-taker** (Obsidian skill wiring + event trigger).
- Tier C (local-only CLI, no integrations to declare): **Contacts**, **Journal**.

**Close to shippable.** None of the 10 meet the Atlas reference bar. Tier A is the closest cluster — each maps a single OpenClaw skill to a single launchd-scheduled script; the bot_guidance for those already contains an XML plist block. A spec-rewrite pass that simply lifts existing bot_guidance content into the structured fields would move Tier A to Good without inventing new behavior.

**Sequencing recommendation.** Do the Hollow rewrites first (they're product decisions: what should Morning Briefing actually be? should EA Pack be a meta-spec that depends on the others?). Then sweep Tier A → Tier C through structured-field population; the work is mechanical once the rewrite shape is decided. Use Atlas as the template — same shape, drop the multi-app shared-modules block, keep everything else.
