# App Manifest v7 — Architecture (2026-05-20)

Status: **draft** (design developed in conversation 2026-05-20; supersedes the scope of the prior v7 recommendation by folding in the sharing/lifecycle work).

**What this is.** A structural redesign of the application manifest from a single per-bot record into three artifacts — **App Spec**, **App Instance**, **Provenance** — plus a fourth shareable artifact (**Lessons**). Adds first-class fields for the four high-leverage gaps from the Atlas pressure-test (`event_triggers`, `bot_guidance`, `privacy`, `audience_scoping`). Specifies how apps share between bots within a pod and between pods. Adds two phases to Forge (Reflect, Adopt) and one new tool primitive (`extend_application`).

**Naming note.** The code's `MANIFEST_SCHEMA_VERSION` is at 13, incrementally bumped each field addition. "v7" in this doc refers to the architectural-arc label from the prior Atlas memo, not an incremental counter. On landing, `MANIFEST_SCHEMA_VERSION` jumps to 14 with a `manifest_shape: "v7-arc"` discriminator to reflect the structural split, not a renaming back to 7.

**Relationship to other specs.**
- [docs/manifest-spec.md](manifest-spec.md) — current spec (v4 in doc-versioning / v13 in code). v7 supersedes this once migration completes.
- [docs/manifest-schema.json](manifest-schema.json) — JSON Schema for the current single-file shape. Replaced with separate schemas per artifact (spec, instance, lessons).
- [docs/spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md) — audit framework; extended here by Reflect phase semantics.
- `docs/atlas-app-manifests/GAPS.md` (on `elegant-bohr-f5356c` worktree) — full Atlas pressure-test gap analysis; this spec consumes its four high-leverage gaps.
- [packages/admin/evolve_admin/applications/provenance.py](../packages/admin/evolve_admin/applications/provenance.py) — file-marker system; continues unchanged, reframed in Section 7.

---

## 1. Why the structural shift

Two driving forces:

**Atlas pressure-test (May 2026).** Drafting four full Atlas manifests against schema v6 surfaced 11 gaps; four are load-bearing for the app-framework-as-contracts story: event-triggered apps, bot guidance, privacy block, and audience scoping. Without first-class fields, every event-driven or audience-scoped app smuggles structure into `build_spec` prose; the RSI loop can't inspect it; compliance can't verify it; the guard layer reinvented per app (~220 lines for Atlas alone) compounds across the gallery.

**Sharing/lifecycle conversation (May 2026).** Three downstream features — bot-to-bot app sharing, pod-to-pod app sharing, and Lesson-driven cross-bot learning — all require the manifest to separate cleanly into "what travels" vs. "what stays." Today's schema conflates objective + blueprint (canonical, shareable) with realized files + schedules + usage (local, never travels). Sharing forces the split.

The two pressures point at the same redesign. Doing them together produces a coherent v7; doing them separately produces a v7 from one pressure that the other has to re-cut.

---

## 2. Architectural backbone — three artifacts plus one

Today's manifest does four jobs. v7 separates three of them and creates a fourth shareable object:

```
        ┌──────────────────────┐
        │  App Spec            │  Canonical · Shareable · Objective-first
        │  (the recipe)        │  Travels between bots and pods
        └──────────┬───────────┘
                   │
            spec_id + spec_version
                   │
        ┌──────────▼───────────┐
        │  App Instance        │  Local · Never travels
        │  (the realization)   │  Realized files, schedules, learned config,
        │                      │  usage metadata, change log
        └──────────┬───────────┘
                   │
            periodic distill
                   │
        ┌──────────▼───────────┐
        │  Lessons             │  Compressed · Optionally shareable
        │  (what we learned)   │  Must cite evidence; redacted on share
        └──────────────────────┘

        Provenance binds them: which Spec/version birthed this Instance,
        where the Spec came from, when the install happened.
```

| Artifact | Mutability | Authored by | Shareable | Storage |
|---|---|---|---|---|
| **App Spec** | Versioned (immutable per version, append-only versions) | This bot's LLM at Instance creation; subsequent versions via Adopt phase | Yes | `{shared_dir}/gallery/<tier>/<spec_id>/<spec_version>.json` where `<tier>` ∈ {`builtin`, `local`, `imported/<source_pod_id>`} (one file per version) |
| **App Instance** | Mutable (continuous via change log) | This bot's LLM + scanner | **No** (bot-specific paths, user data, learned config) | `/Users/{bot}/.openclaw/workspace/manifests/<instance_id>.json` |
| **Provenance** | Immutable (one record per Instance) | Created at install time | Travels embedded in Instance | Embedded |
| **Lessons** | Append-only, periodically compressed | Bot's LLM at Reflect time | Yes (with redaction) | `{shared_dir}/lessons/<bot_id>/<spec_id>.json` (pod-shared; matches signal-store layout — the bot's `.openclaw/` dir is bot-owned with only ACL read for evolve, so per-bot lessons storage isn't writable by the migration daemon) |

**Why separate Instance from Spec at all.** Per the RSI design approach, every install is expected to diverge from the Spec from birth — different LLM choices, different file layouts, different user-specific configurations. The Spec captures intent, the Instance captures realization, and divergence between them is *the design*, not drift. Keeping them together forces every share-time operation to re-litigate what counts as "the app" — separating them up front makes the answer structural.

**Why Lessons isn't just compressed change-log.** The change log is a private audit trail. Lessons are evidence-cited claims about what worked or didn't, with redaction at share-time. Different objects, different evidence requirements, different privacy posture.

---

## 3. App Spec schema

The canonical, shareable artifact. Carries everything required to *rebuild the app on a new bot* — and nothing bot-specific.

```jsonc
{
  "spec_id": "p-a3f91c8b",                  // Stable across versions
  "spec_version": "2026.05.20-1.0",         // Date-prefixed semver
  "name": "Protein Tracker",
  "app_version": "1.0.0",                    // App's own semver, distinct from spec_version
  "schema_version": 14,
  "manifest_shape": "v7-arc",

  "source": {
    "pod_id": "pod-example-mini",            // Optional — present for shared specs
    "bot_id": "evo",                          // Optional
    "shared_at": "2026-05-20T14:23:00Z"      // Set on share, not on install
  },

  "objective": {
    "primary": "Track the user's daily protein intake and surface trends.",
    "sub_objectives": [
      "Capture intake events via Telegram message",
      "Aggregate weekly and monthly trends",
      "Notify when 3+ days below target"
    ]
  },

  "success_criteria": {
    "behavioral": [
      "User can log protein intake in under 10 seconds",
      "Weekly summary delivers within 60s of cron"
    ],
    "observable": [
      "intake_log entries > 4 per week",
      "no failed cron runs in 30 days"
    ]
  },

  "blueprint": {
    "files": [
      {
        "logical_name": "ingest_script",
        "role": "vital_to_blueprint",         // vital_to_blueprint | instance_specific | reference_only
        "intent": "Parse Telegram protein-log messages and append to data store.",
        "code_snippet": "# Reference Python — actual implementation may differ\nimport re\ndef parse(msg): ...",
        "language": "python",
        "expected_location": "scripts/ingest.py"
      },
      {
        "logical_name": "summary_script",
        "role": "vital_to_blueprint",
        "intent": "Generate weekly intake summary and send via Telegram.",
        "language": "python",
        "expected_location": "scripts/summary.py"
      },
      {
        "logical_name": "data_store",
        "role": "instance_specific",          // Recreate per install, do not copy data
        "intent": "Persist daily intake events as append-only JSONL.",
        "language": "jsonl"
      }
    ]
  },

  "dependencies": {                            // See Section 3.1 for full schema and resolution
    "apps": [],
    "python_packages": [
      { "name": "pyyaml", "version": ">=6.0", "required": true }
    ],
    "system_packages": [],
    "oc_plugins": [
      { "plugin_id": "filesystem", "required": true, "purpose": "Read/write the intake log" }
    ],
    "oc_skills": [],
    "integrations": [
      {
        "integration_id": "telegram",
        "scopes": ["receive_messages", "send_messages"],
        "required": true,
        "purpose": "Receive /protein commands and deliver weekly summary"
      }
    ],
    "credentials": []
  },

  "event_triggers": [                          // Atlas Gap 1
    {
      "id": "incoming_protein_log",
      "source": "telegram",
      "match": { "text_regex": "(?i)^/?protein\\s+\\d+" },
      "audience": "operator_only",            // References audience_scoping.role_capabilities
      "invokes": "ingest_script"
    }
  ],

  "schedules": [
    {
      "id": "weekly_summary",
      "cron_intent": "Sunday 9am local time",
      "cron_default": "0 9 * * 0",
      "invokes": "summary_script"
    }
  ],

  "bot_guidance": [                            // Atlas Gap 2
    {
      "section": "## Protein Tracker",
      "content": "When the user sends /protein N, invoke scripts/ingest.py with the number..."
    }
  ],

  "privacy": {                                 // Atlas Gap 3
    "user_data_collected": ["intake_log_entries", "timestamps"],
    "opt_out_command": "/protein opt-out",
    "consent_notice": "I log your daily protein intake to help track trends. Reply /protein opt-out to disable.",
    "retention_days": 365,
    "shareable_in_lessons": false              // Gates whether this app's observations appear in shareable Lessons
  },

  "audience_scoping": {                        // Atlas Gap 4
    "operator": "operator_only",              // operator_only | named_users | open
    "approved_surfaces": ["telegram_dm"],
    "role_capabilities": {
      "operator_only": ["read", "write", "configure"]
    },
    "operator_bypasses": ["admin_override"]
  },

  "scope_excludes": [
    "Tracking other macronutrients",
    "Multi-user shared logs"
  ],

  "approval_audience": "pod_operator",
  "tags": ["health", "tracking"]
}
```

**Key design decisions:**

- **`spec_id` is stable across versions.** A new Spec version (e.g., 1.0 → 1.1) keeps the same `spec_id`; only `spec_version` changes. This enables Lesson back-flow to target a Spec without ambiguity. (The `p-` prefix is a legacy artifact from "package"; functional meaning is now "spec," but the prefix stays for marker-migration simplicity.)
- **`spec_id` is minted at Instance creation, not at first share.** Every Instance has a `spec_id` from day 1 in its Provenance. Simultaneously, a corresponding Spec file is written to `{shared_dir}/gallery/local/<spec_id>/<spec_version>.json` — so locally-original apps have a Spec from the start; sharing later copies that file to the destination pod's `gallery/imported/<source_pod_id>/<spec_id>/<spec_version>.json` (see §9 for cross-pod namespacing). This eliminates ambiguity about when the Spec exists.
- **`spec_version` format is canonical: `YYYY.MM.DD-major.minor`** (e.g., `2026.05.20-1.0`). Markers carry this verbatim (`spec=p-...@2026.05.20-1.0`). File `file_id` carries its own version stamp in the same format (`f-...@2026.05.20-1.0`). Don't mix dot- and hyphen-separated variants.
- **Per-file role tag, with sharpened semantics:**
  - `vital_to_blueprint` — Forge **must** recreate equivalent functionality on install. Code snippet is illustrative; LLM may implement differently but must satisfy the intent.
  - `instance_specific` — Forge **must** create an empty placeholder at the expected location. Path/format matters; content is per-instance and does not propagate.
  - `reference_only` — Forge **may** ignore. Pure documentation that doesn't need to land on the receiving bot.
- **When role tags are assigned.** Either at share-time (source LLM classifies during Reflect-distill) or at Reflect-time when an orphan file is folded into a Spec (the Reflect proposal includes role assignment).
- **Code snippets carry intent, never as authoritative source.** Every `code_snippet` is paired with `intent` describing what it satisfies. The receiving LLM may implement it identically, similarly, or differently — divergence is allowed and expected.
- **`event_triggers[]` is first-class.** Today event-driven apps smuggle this into `build_spec` prose. With a structured field, Forge can wire event handling at install, RSI can inspect coverage, and compliance can verify which surfaces an app listens to.
- **`bot_guidance[]` replaces AGENTS.md splicing.** The provisioner today scrapes a heading out of `build_spec` and splices it into AGENTS.md. v7 makes the binding explicit; uninstall removes it cleanly.
- **`privacy{}` is machine-checkable.** What was prose in `constraints.privacy` becomes inspectable fields. `shareable_in_lessons` gates whether observations from this app can appear in shareable Lessons at all — connects to the user-observation-opt-out posture (v1 requirement).
- **`audience_scoping{}` declares the trust boundary.** Atlas built 220 lines of guard code; v7 collapses it into a declared field that the platform's gateway layer enforces.

---

## 3.1 Dependencies — seven kinds

Apps depend on more than other apps. The full taxonomy:

| Kind | What | How it's checked at install |
|---|---|---|
| `apps` | Other Specs (by `spec_id` + version) this app needs to function | Required Spec installed locally; version satisfied; otherwise prompt to install it first |
| `python_packages` | pip-installable Python packages | Install via the bot's venv |
| `system_packages` | OS-level binaries (ffmpeg, imagemagick, Unity Editor, etc.) | Run `host_check` command; if exit ≠ 0, surface to operator — cannot auto-install on locked-down hosts |
| `oc_plugins` | OpenClaw plugins providing tools to the LLM (filesystem, github, brave, unity, etc.) | Query the bot's `openclaw.json` for enabled plugins; prompt operator to enable if missing (per-bot opt-in) |
| `oc_skills` | OpenClaw skills (substrate-shared capabilities; `agentskills.io/...`) | Query skill registry; install or surface if missing |
| `integrations` | Bot-level integration channels (telegram, gmail, slack, discord) with required scopes | Query bot integration config; verify channel is active and grants the requested scopes |
| `credentials` | External API keys / secrets the app needs at runtime | Query env vars / secret store; if missing, prompt operator |

Each entry carries:

- **`purpose`** — natural-language reason the app needs this dep. Shown to the operator when prompting for missing deps. (e.g., *"Read/write the intake log"*, *"Receive /protein commands"*.)
- **`required: true | false`** — required deps fail the install; optional deps degrade gracefully (the app may skip features that need them).
- **`version` constraint** where applicable. **Syntax: PEP 440** across all dependency kinds, regardless of substrate (`">=6.0"`, `">=1.0,<2.0"`, `"==2022.3"`, `"*"`). The dependency resolver translates to substrate-native check syntax (e.g., `pip` version specs are native PEP 440; `host_check` commands for system packages are operator-authored shell commands that must verify both presence AND version themselves; `oc_plugin` version checks query the bot's plugin registry).

### Example: a Unity app

```jsonc
"dependencies": {
  "apps": [],
  "python_packages": [
    { "name": "PyYAML", "version": ">=6.0", "required": true }
  ],
  "system_packages": [
    {
      "name": "Unity Editor",
      "version": ">=2022.3",
      "host_check": "ls /Applications/Unity",
      "required": true,
      "purpose": "Open and edit Unity projects"
    }
  ],
  "oc_plugins": [
    {
      "plugin_id": "unity",
      "version": "*",
      "required": true,
      "purpose": "Project file access and scene manipulation tools for the LLM"
    },
    {
      "plugin_id": "filesystem",
      "required": true,
      "purpose": "Read project assets"
    }
  ],
  "oc_skills": [],
  "integrations": [],
  "credentials": []
}
```

### Install-time dependency resolution

Forge reads `Spec.dependencies`, dispatches the appropriate check per kind:

1. **All checks run before any file is created.** No half-installed apps.
2. **Required deps missing → install fails.** Operator sees: *"Spec requires Unity plugin. Enable it on this bot first (Admin UI → Plugins), then re-install."* Each missing dep cites its `purpose`.
3. **Optional deps missing → install proceeds with warning.** Logged on the Instance; the app's runtime path must handle the absence gracefully.
4. **All resolved → record on the Instance** as `dependency_check_at_install` (see Section 4 schema).

Cross-pod implication: a Spec shared from pod A may require dependencies pod B doesn't have. The mandatory re-review on cross-pod import (Section 9.2) surfaces all dependency gaps before the operator decides to install. Dependencies are a major reason cross-pod re-review can't be skipped.

### Inferring vs. declaring

Today's scanner partially infers deps by reading `import` statements. v7 makes declaration explicit in the Spec. Reflect can flag mismatches: **declared-but-unused** (suggest removal) and **used-but-undeclared** (suggest adding to the Spec). Inference doesn't replace declaration — it audits it.

---

## 4. App Instance schema

Local realization of a Spec on one specific bot. Never travels.

```jsonc
{
  "instance_id": "i-d4e8f901",
  "bot_id": "team-bot-a",
  "schema_version": 14,
  "manifest_shape": "v7-arc",

  "provenance": {
    "spec_id": "p-a3f91c8b",
    "spec_version": "2026.05.20-1.0",
    "source_pod_id": "pod-example-mini",     // null if locally-original
    "source_bot_id": "evo",
    "installed_at": "2026-05-20T15:00:00Z",
    "installed_by": "forge_engine",
    "forked_from": null,                      // instance_id of source if installed from a sibling
    "prior_spec_ids": []                      // append-only supersession chain (oldest→newest), excl. current spec_id; see §5
  },

  "spec_version_history": [                    // Updated by Adopt phase
    { "version": "2026.05.20-1.0", "adopted_at": "2026-05-20T15:00:00Z", "reason": "initial_install" }
  ],

  "dependency_check_at_install": {              // Audit record from install-time resolution
    "checked_at": "2026-05-20T15:00:00Z",
    "all_required_satisfied": true,
    "details": [
      { "kind": "oc_plugin", "id": "filesystem", "required": true, "satisfied": true, "version_seen": "*" },
      { "kind": "integration", "id": "telegram", "required": true, "satisfied": true, "scopes_granted": ["receive_messages", "send_messages"] },
      { "kind": "python_package", "id": "pyyaml", "required": true, "satisfied": true, "version_seen": "6.0.1" }
    ],
    "optional_missing": []                       // Optional deps not satisfied; runtime may degrade
  },

  "realized_files": [
    {
      "logical_name": "ingest_script",        // Matches Spec.blueprint.files[].logical_name
      "path": "/Users/team-bot-a/.openclaw/workspace/scripts/ingest_v2.py",
      "file_id": "f-d4e8f901@2026.05.20-1.0", // Marker file_id (YYYY.MM.DD-major.minor format)
      "marker_state": "OWNED",
      "created_in_session": "sess-abc123"
    }
  ],

  "configured_schedules": [
    {
      "spec_schedule_id": "weekly_summary",
      "resolved_cron": "0 9 * * 0",
      "configured_at": "2026-05-20T15:00:00Z",
      "user_adjustments": []                   // Record of operator-driven changes
    }
  ],

  "learned_config": {                          // Free-form, LLM-managed
    "user_protein_target": 120,
    "preferred_unit": "grams"
  },

  "usage_metadata": {
    "invocation_count": 247,
    "last_run": "2026-05-19T22:14:00Z",
    "error_count": 3,
    "last_error_at": "2026-05-15T11:00:00Z"
  },

  "change_log": [
    // See section 4.1 — append-only entries
  ],

  "status": "active",                          // active | paused | draft | deprecated
  "last_reflect_at": "2026-05-19T03:00:00Z"
}
```

### 4.1 Change log entries

Append-only. Captures intent, change, and evidence at the moment of change.

```jsonc
{
  "entry_id": "log-2026-05-19-001",
  "timestamp": "2026-05-19T14:23:00Z",
  "kind": "capability_added",                  // See kind tags below
  "who": "bot",                                 // bot | user | scanner | forge
  "session_id": "sess-xyz789",
  "description": "Added fiber tracking via /fiber command",
  "user_intent_quote": "you know what? I also want to track my fiber",
  "file_changes": [
    { "action": "created", "path": "scripts/fiber_ingest.py", "file_id": "f-..." }
  ],
  "reason_signals": [                          // For audit; consumed by Lessons compression
    { "signal_type": "user_request", "ref": "telegram_msg_abc" }
  ]
}
```

**Kind tags** (canonical vocabulary):

| Tag | Meaning |
|---|---|
| `capability_added` | New file, cron, or trigger that extends the app's behavior |
| `blueprint_correction` | Fix to a previously-built file (bug fix, refactor) |
| `config_tuning` | Adjustment to scheduled times, thresholds, or learned config |
| `data_refresh` | Non-structural data update (e.g., reseed) |
| `user_redirect` | Change driven explicitly by user request |
| `scanner_inferred` | Change discovered by Reflect, not by direct action |
| `forge_rebuild` | Forge re-ran on this Instance (e.g., Adopt phase applied) |

Kind tags are set at write-time, not derived later. Compression to Lessons uses kind as the primary axis.

**`who` field semantics.** Identifies the *actor* that made the change — `bot` (LLM mid-session), `user` (direct operator action via UI), `scanner` (Reflect-discovered), or `forge` (Build/Adopt phase). When the bot acts in response to user instruction, `who: "bot"` with `user_intent_quote` set — both fields appear; they describe different things.

**Retention.** Change-log entries are append-only and retained for the lifetime of the Instance (audit trail). Periodic compression rolls qualifying entries into Lessons (Section 6), but the raw log is never deleted. This is more conservative than the signal-store log retention (1 year per CLAUDE.md) because the change-log scales with structural change events, which are infrequent.

**Bot deletion.** When a bot is deleted from the pod, its Instances are not auto-deleted — they're moved to `{shared_dir}/archived_instances/<deleted_bot_id>/<instance_id>.json` and their `status` flips to `deprecated`. Lessons compiled from those Instances stay shareable (subject to the Spec's `shareable_in_lessons` flag). The corresponding Specs in `gallery/local/` are *not* removed — they may be installed on other bots. Manual purge of archived Instances is a separate operator action; v1 keeps the archive indefinitely so historical audit data isn't lost.

---

## 5. Provenance — the link

Provenance lives inside the Instance (see Section 4 example). Three roles:

1. **Identity binding.** `spec_id` + `spec_version` answer "what Spec birthed this Instance, and at what point in the Spec's evolution?"
2. **Source attribution.** `source_pod_id` + `source_bot_id` answer "where did this Spec come from?" Null `source_pod_id` = locally-original; non-null = imported from another pod. Drives "external source" badging in the operator UI.
3. **Fork lineage.** `forked_from` answers "did this Instance start from another sibling Instance on this pod?" — set when one bot's working install is used as the seed for another (rather than a fresh Spec install). Mostly diagnostic; not used for runtime behavior.
4. **Spec-id lineage.** `prior_spec_ids` answers "what `spec_id`s did this Instance carry before its current one?" It is an append-only chain (`list[str]`, oldest→newest, **excluding** the current `spec_id`). Distinct from `spec_version_history`, which tracks *version* bumps **within one stable `spec_id`** (the Adopt phase): `prior_spec_ids` tracks the rarer event where the whole `spec_id` is **replaced** because the app was rebuilt under a fresh identity — a forge re-create, or scanner re-discovery minting a new `p-` id. Without it, the old `spec_id` would be dropped and the workspace files still stamped with it (their `_evolve` marker, §7) would reconcile (§reflect) as orphans even though the app is still installed under the new id. The resolver consults current `spec_id` ∪ `prior_spec_ids` so a retired-id marker resolves to the live Instance. Optional / back-compat: an absent key reads as `[]`. Entries are deduped and order-preserved; a `spec_id` never supersedes itself.

Provenance's *attribution* fields (`source_pod_id`, `source_bot_id`, `forked_from`, `installed_at`, `installed_by`) are immutable after install. Subsequent Spec **version** changes (Adopt phase) bump `spec_version_history` in the Instance body, not the original Provenance record; a Spec **identity** replacement (re-create / re-discovery) appends to `prior_spec_ids` — the one Provenance field that grows over the Instance's life.

---

## 6. Lessons artifact

The shareable distillation of an Instance's change log + usage. Separate file, separate share semantics, separate privacy posture.

```jsonc
{
  "lessons_id": "l-e5f0aa12",
  "spec_id": "p-a3f91c8b",
  "spec_version_observed": "2026.05.20-1.0",
  "source_pod_id": "pod-example-mini",
  "source_bot_id": "team-bot-a",
  "observation_window": {
    "start": "2026-05-20T00:00:00Z",
    "end": "2026-08-20T00:00:00Z",
    "instance_runs": 247
  },

  "lessons": [
    {
      "lesson_id": "les-001",
      "kind": "blueprint_correction",         // worked_well | failed | new_capability | blueprint_correction
      "summary": "Original ingest regex missed decimal values like '23.5g'",
      "evidence": [
        { "type": "error_count", "ref": "usage.error_count", "value": 14 },
        { "type": "user_complaint", "ref": "redacted", "summary": "User reported numbers not registering" }
      ],
      "proposed_spec_change": {
        "target": "the ingest script's number-capture regex",
        "kind": "regex_extension",
        "description": "Allow decimal values in protein number capture (e.g., 23.5g)"
      }
    },
    {
      "lesson_id": "les-002",
      "kind": "new_capability",
      "summary": "Users wanted to also track fiber; we extended the script to handle multiple nutrients",
      "evidence": [
        { "type": "user_request", "ref": "redacted", "count": 3 }
      ],
      "proposed_spec_change": {
        "target": "the app's objective and sub-objectives",
        "kind": "scope_expansion",
        "description": "Generalize from single-nutrient to multi-nutrient tracking; objective rename suggested"
      }
    }
  ],

  "current_summary": "After 3 months, protein-tracker has stabilized at ~80 logs/week per user. Two blueprint corrections (decimal handling, message-typo tolerance) and one scope expansion (multi-nutrient) emerged. Privacy opt-outs: 0.",

  "redaction_applied": true,
  "redaction_kind": ["user_quotes_paraphrased", "timestamps_quantized_to_week"]
}
```

**Hard rules:**

1. **Every Lesson must cite evidence.** No evidence = no Lesson. Permitted evidence types: signal-store references, error counts, user-complaint references, completion-rate stats, scheduled-action success rates. LLM narration ("we learned X") without one of these is rejected at compression time. *Note: `current_summary` is a derived view of the cited `lessons[]`, not a Lesson itself — it carries no separate evidence requirement, but it must not introduce claims absent from the underlying Lessons.*
2. **Redaction is mandatory at share-time.** User-attributable patterns (specific times, identifying numbers, verbatim quotes) must be paraphrased or quantized. The `redaction_applied: true` flag is verified by the share gateway before publishing.
3. **`shareable_in_lessons: false` in the Spec is honored.** If the Spec's privacy block opts out, Lessons compression produces a local record but it is never published to the share gateway.
4. **`proposed_spec_change` is advisory and free-text.** A Lesson can propose Spec changes; whether they get adopted goes through Forge Adopt (Section 8) with the receiving operator's gate. The `target` field is a **natural-language description** of what the change affects (e.g., *"the ingest script's regex"*, *"the weekly summary schedule"*), not a parseable selector. Adopt shows it to the operator and re-runs the full Forge pipeline against the proposed change description; nothing depends on machine-parsing the target string.

5. **Minimum evidence by Lesson kind.** The "must cite evidence" rule is per-kind:
   - `blueprint_correction` — at least one quantitative metric (error count, completion rate, failure rate, etc.)
   - `failed` — at least one quantitative metric
   - `worked_well` — at least one quantitative metric (otherwise it's narration)
   - `new_capability` — at least one `user_request` or `user_complaint` reference (the user asked for it)

   Lessons that don't meet the per-kind minimum are rejected at compression time. Easy-to-game cases (e.g., a `new_capability` Lesson with one redacted user_request) still pass the schema; the Adopt phase is the secondary gate.

---

## 7. Marker system — what changes and what doesn't

The provenance.py marker system (`file_id`, `pkg_id`, multi-pkg sharing, lifecycle taxonomy at [provenance.py:62](../packages/admin/evolve_admin/applications/provenance.py:62)) remains. v7 adds two things:

**Marker payload extends to include `spec_id`.** Today: `# evolve: pkg=p-a3f91c8b@... file=f-d4e8f901@...`. v7: `# evolve: spec=p-a3f91c8b@2026.05.20-1.0 file=f-d4e8f901@2026.05.20-1.0`. Both stamps use the canonical `YYYY.MM.DD-major.minor` format. The marker now references the *Spec* rather than the *gallery package* (which becomes redundant — every Spec serves the gallery-package role for installs).

**Reframe the manifest/marker relationship explicitly:**

> The manifest is the authoritative **index** of declared ownership; markers are the file's **self-identity**; the scanner is the reconciler. Disagreements surface via the existing OWNED / SHARED / ORPHANED / UNOWNED taxonomy.

No code change required for this reframe — it's documentation. But it lands in this spec so future contributors don't propose flattening to one layer. The two layers are complementary, not redundant.

---

## 8. Forge phase additions

Current Forge phases ([forge_engine.py](../packages/admin/evolve_admin/applications/forge_engine.py)): **Build → Critique → Test → Gate → Apply**. v7 adds two:

### 8.1 Reflect — manifest hygiene

Runs periodically (post-session, daily, on-demand). Scope:

1. **Orphan capability detection.** Scan workspace for files / crons / schedules not claimed by any Instance. For each orphan, use the change log to propose ownership — extension of nearby app, new app, or one-off. Surfaces as a proposal to the operator.
2. **Marker coverage repair.** Files in an Instance's `realized_files[]` but missing markers → stamp them. Files with markers but not in any `realized_files[]` → propose reconciliation.
3. **Spec drift detection.** If the Instance's `realized_files[].role` no longer matches what the Spec says (e.g., a file marked `instance_specific` has become structurally vital), propose a Spec-version bump with the corrected role.
4. **Change-log → Lessons compression.** Roll recent change log entries into Lessons updates. Only entries with evidence get promoted; LLM narration without evidence is rejected.

Reflect doesn't apply changes unilaterally — it produces proposals routed through the existing arbiter Proposal store, with `approval_audience: pod_operator`.

### 8.2 Adopt — Lesson-driven update

Triggered when the operator views a shared Lesson and chooses to adopt it. Flow:

1. **Parse Lesson's `proposed_spec_change[]`.** Categorize each as Spec-affecting or Instance-affecting.
2. **Present to operator as separate decisions.** Spec changes alter what the app *is*; Instance changes alter how *this bot* runs it. The dialogue-gate question: *"Do you want this to change what your app does, or just how it works?"*
3. **For accepted Spec changes:** bump Spec version, re-run Build → Critique → Test → Gate → Apply against the new Spec.
4. **For accepted Instance changes:** apply directly to Instance, log as `forge_rebuild` change-log entry, no Spec change.

Adopt re-runs full Critique/Test/Gate on imported Lessons regardless of source — see Section 9 on cross-source trust.

### 8.3 New tool primitive: `extend_application`

```python
extend_application(
    instance_id: str,
    file: Optional[FileDescriptor] = None,    # path, role, intent
    cron: Optional[CronDescriptor] = None,
    event_trigger: Optional[EventTriggerDescriptor] = None,
    capability_summary: str,
    user_intent_quote: Optional[str] = None,
)
```

Effect:

- Stamps the new file with markers (no need for the bot to remember marker format)
- Appends a `change_log` entry with `kind: capability_added` and the user intent quote if provided
- Updates `realized_files[]` and `configured_schedules[]` in the Instance
- Optionally proposes a Spec-version bump if the addition feels Spec-shaped (heuristic; otherwise the change is Instance-only)

Without this tool, in-session extension requires raw JSON editing of the Instance manifest — exactly the kind of structural work LLMs are flaky about. Making it a first-class tool is **the marker-coverage hook AND the change-log hook AND the Spec-vs-Instance discriminator**, all in one primitive.

**Implementation location.** `extend_application` is an admin-server-backed tool exposed to the bot via the existing forge-runner boundary ([forge_runner.py](../packages/analyzer/forge_runner.py)). Bots don't need a per-bot plugin opt-in for it because they invoke it through the same path they use for `record_application` today. This avoids the per-bot hook opt-in friction noted in the OC 2026.4.29 hook constraints.

**Write-order contract.** The tool performs its work in a fixed sequence so failures roll back cleanly:

1. Stage the file in `/tmp/evolve-extend-<uuid>/` (operator-readable for debugging).
2. Stamp the marker on the staged file. If marker-stamping fails here, abort: nothing has been written to the workspace yet.
3. Atomic rename `/tmp/evolve-extend-<uuid>/<file>` → final workspace path. After this point the file exists at its real location.
4. Atomic write the updated Instance JSON (temp-file + rename to `<instance_id>.json`).
5. Append the `change_log` entry (atomic append to the Instance JSON or to a sidecar log file, depending on size).

If any step fails, the staging directory remains and an error proposal is surfaced to the operator (so the partial state is visible and resolvable). Reflect runs treat a leftover `/tmp/evolve-extend-*` directory as a signal that an extend call failed mid-flight.

**Failure modes.**

- **Marker stamp fails** (binary format, permission error): step 2 aborts. The file is *not* in the workspace, the Instance JSON is *not* updated, the change-log entry is *not* appended. The bot must surface the failure to the user and either retry with a different format or skip the addition.
- **Concurrency with Reflect** (Reflect scans the workspace while `extend_application` is mid-write): Reflect doesn't write the Instance directly — it routes through the arbiter Proposal store. The race window is between step 3 (file appears on disk) and step 4 (Instance JSON updated), during which Reflect may classify the file as UNOWNED. This is acceptable: the next Reflect pass after step 4 reclassifies it correctly. Reflect must not auto-propose orphan integration for files less than 60 seconds old.
- **Concurrent `extend_application` calls on the same Instance**: atomic rename in step 4 ensures one wins; the loser retries by re-reading the Instance JSON and re-applying its change.
- **Dependency missing for the new capability**: the tool checks `Spec.dependencies` against `Instance.dependency_check_at_install`; if the new capability requires a dep that isn't satisfied, the tool refuses (before step 1) and the operator is prompted to resolve before the capability is added.

### 8.4 Uninstall / deprecation

v1 deprecation semantics (no destructive delete):

1. **Instance status flips to `deprecated`.** No further `extend_application` calls accepted; no scheduled actions run.
2. **`bot_guidance[]` is unspliced from the bot's AGENTS.md.** The provisioner's reverse pass removes the bound sections; AGENTS.md is rewritten atomically.
3. **`event_triggers[]` are unwired from the gateway layer.** Incoming events that previously routed to this app are now ignored (or fall through to other apps, per gateway routing rules).
4. **`schedules[]` are stopped.** Any cron jobs registered by this Instance are unregistered from the bot's launchd / scheduler.
5. **`realized_files[]` transition to marker_state `"deprecated"`** but are NOT deleted. The marker payload gains a `deprecated_at` timestamp. Operator can manually delete the files later if desired; Reflect surfaces them as "deprecated files belonging to deprecated app" with a one-click cleanup affordance.
6. **Dependencies are not released.** Other apps may share oc_plugins, integrations, etc. — releasing them is not the uninstall's job. (A separate `cleanup_orphan_dependencies` Reflect pass can identify deps with no remaining live consumer.)
7. **The Spec stays in the gallery.** Deprecation of an Instance doesn't touch the Spec — the Spec may still be installed on another bot or another pod.
8. **Lessons remain.** Existing Lessons for this Spec stay shareable (subject to the Spec's `shareable_in_lessons` flag) — the historical learnings are still valid even if this Instance is no longer running.

A **purge** operation (destructive) is deferred to a v2 spec. v1 is intentionally conservative: deprecate is reversible (status flip back to `active` re-wires everything); purge is not.

**Re-activation.** Flipping status from `deprecated` back to `active` re-runs steps 2/3/4 in reverse: re-splice AGENTS.md, re-wire event_triggers, re-register schedules. Markers transition back to `OWNED`. If a dependency that was satisfied at install is no longer satisfied, re-activation fails the same way a fresh install would.

---

## 9. Sharing protocol

Both within-pod and cross-pod sharing route through the existing gallery infrastructure ([gallery.py](../packages/admin/evolve_admin/applications/gallery.py)). Unified flow:

### 9.1 Within-pod (bot A → bot B)

1. **Operator initiates share** from bot A's Instance.
2. **Distill into Spec.** Reflect phase runs to refresh the Instance manifest against current state, then:
   - Extract Spec-shape fields (identity, objective, blueprint with per-file roles, dependencies, event_triggers, bot_guidance, privacy, audience_scoping).
   - Stamp `source.pod_id`, `source.bot_id`, `source.shared_at`.
   - `spec_id` is preserved from the Instance's Provenance — every Instance has a `spec_id` from creation, sharing elevates the existing Spec into the shared gallery rather than minting a new ID.
3. **Write to `{shared_dir}/gallery/<destination>/<source_pod_id>/<spec_id>/<spec_version>.json`** where `<destination>` is `imported` for cross-pod and `local` for within-pod (within-pod shares may omit `<source_pod_id>` since `(local, spec_id)` is already unique). Per-pod namespacing of imports prevents collision when two pods independently mint the same random `spec_id`.
4. **Bot B installs from gallery.** Existing install flow: Forge Build with new `instance_id`, new realized files, fresh Provenance binding to the shared Spec. The install **pins `spec_version` at start** and reads only the pinned version's JSON for the duration of the install; concurrent Adopt-phase bumps on the source Spec don't affect in-flight installs.

### 9.2 Cross-pod (pod A → pod B)

1. **Export from pod A.** Same distill as 9.1, but produces a downloadable file. Operator transmits to pod B operator (any channel — email, USB, message).
2. **Import into pod B.** Drop file into `{shared_dir}/gallery/imported/<source_pod_id>/<spec_id>/<spec_version>.json` on target pod, or upload via admin server affordance which derives the path from the Spec's `source.pod_id` and `spec_id` fields.
3. **`spec_id` collision check.** If pod B already has a Spec with the same `spec_id` from a different `source_pod_id` (or as `local`), the import surfaces the collision to the operator. The collision is structural, not a conflict: per-pod namespacing means both Specs coexist at distinct paths, but the operator should know two Specs with the same ID exist and decide which to install on which bot.
4. **Mandatory re-review.** Regardless of source, full Forge Critique/Test/Gate runs on the imported Spec before any install. Cross-pod imports carry `source.pod_id != local_pod_id` and the operator UI flags this as *"external source — review carefully."*
5. **Install as in 9.1**, with `spec_version` pinned at install start.

### 9.3 Lessons sharing

Same flow, separate object. Lessons travel independently of Specs. A bot may share a Lesson about Spec `p-a3f91c8b` without sharing the Spec itself (if the receiving pod already has it). Lessons-sharing respects `shareable_in_lessons` in the source Spec's privacy block.

### 9.4 What does NOT travel

| Artifact | Travels? | Why not |
|---|---|---|
| App Spec | Yes | The recipe |
| Realized files | No | Receiver's Forge recreates them per intent |
| Configured schedules | No | Spec carries `cron_default`; receiver may adjust |
| Learned config | No | Bot-local state |
| Usage metadata | No | Bot-local |
| Raw change log | No | Local audit; only Lessons distillation is shareable |
| Lessons | Yes (with redaction) | Evidence-cited claims |
| Provenance | Yes (embedded in Instance at install) | Identity binding |

Every install is expected to diverge from birth. The Spec carries enough for a new bot to build *its own* realization; carrying realized files would defeat the design.

---

## 10. Migration plan

v13 (today) → v7-arc shape. Structural shift, not a field rename. One-shot migration during a maintenance window.

### 10.1 Per-Instance migration

For each existing manifest at `/Users/{bot}/.openclaw/workspace/manifests/<app_id>.json`:

1. **Extract Spec-shape fields** into a new Spec at `{shared_dir}/gallery/local/<spec_id>/<spec_version>.json`:
   - `id`, `name`, `description`, `objective`, `success_criteria`, `build_spec → blueprint`, `dependencies`, `tags`
   - Atlas-gap fields default to inferred/empty:
     - `event_triggers`: empty for cron-only apps; apps with prose hints in `build_spec` flagged for manual review
     - `bot_guidance`: extracted from `build_spec` headings via the existing provisioner regex
     - `privacy`: defaults to `shareable_in_lessons: false`, `retention_days: 365`; personal-bot apps flagged for manual review
     - `audience_scoping`: defaults to `operator_only` for personal bots, `named_users` for team bots; manual review for ambiguous cases
   - Generate `spec_id` (new) and `spec_version` (set to `2026.05.20-1.0`)
2. **Extract Instance-shape fields** into a new Instance file:
   - `realized_files` from current `files[]` (existing marker file_ids carry over)
   - `configured_schedules` from current cron definitions
   - `learned_config` from any free-form state fields
   - `usage_metadata` from `last_run`, `last_error`, etc.
   - `change_log[]` starts empty (pre-migration history is lost; migration is a watermark)
   - Generate `instance_id` (new); Provenance.spec_id binds to new Spec
3. **Markers in files migrate.** `pkg=p-... file=f-...` → `spec=p-... file=f-...`. One-pass via the scanner's existing marker code, idempotent.

### 10.2 Gallery migration

For each existing gallery package at `gallery/index.json + gallery/<name>/<pkg_id>.json`:

- Promote to Spec at `{shared_dir}/gallery/builtin/<spec_id>/<spec_version>.json` (initial version `2026.05.20-1.0`)
- `pkg_id` → `spec_id` (prefix `p-` retained; the prefix is a legacy artifact)
- Add empty Atlas-gap fields with defaults

### 10.3 Atlas-specific

Atlas's four v6 manifests (`docs/atlas-app-manifests/` on the Atlas worktree) map almost directly into v7 Specs since they already structure their Atlas-gap data, just in informal locations:

- Lift `audience_scoping` from `atlas/operator.json`
- Lift `bot_guidance` from `build_spec` prose
- Lift `privacy` from `constraints.privacy` prose

Estimated 2-3 hours per app.

### 10.4 Sequencing

| Session | Work |
|---|---|
| 1 | Migration code + per-artifact JSON Schema files + bump `MANIFEST_SCHEMA_VERSION` to 14 with `manifest_shape: "v7-arc"` discriminator |
| 2 | Run migration on test pod, validate all existing apps still install and run, fix issues |
| 3 | Update Forge to write v7-shape natively; add Reflect + Adopt + `extend_application` |
| 4 | Lessons compression + share flow + cross-pod export/import UI |
| 5 | Gallery app migration + rewrite of [docs/manifest-spec.md](manifest-spec.md) to reflect v7 (this design doc becomes historical) |

Full v7 lands in ~5 focused sessions. Atlas shipping against v6 with workarounds in the meantime remains acceptable — the v6→v7 migration for Atlas is mechanical and doesn't block validation work.

---

## 11. Open questions & deferred work

### 11.1 Decided in this revision (2026-05-20)

- **Back-flow merge semantics → one-way for v1.** Lessons consumed on the receiving pod do not propagate back to the source Spec on the origin pod. Each pod's Spec evolves on its own track. The `spec_id` + `spec_version` machinery in v7 does not preclude richer back-flow later (see 11.3).
- **Sub-objective hierarchy → flat for v1.** `objective.sub_objectives[]` stays a flat list. No real app in the current gallery + Atlas + planned bots forces hierarchy; ordering and grouping ride in prose within each sub-objective string. If a hierarchical case emerges, add an `objective_hierarchy[]` sibling field later — don't restructure `sub_objectives[]`.
- **Cross-source Lessons trust → trust `redaction_applied` + mandatory re-review.** The receiver does not cryptographically verify the source's evidence claims; safety is enforced because any Spec change derived from an external Lesson goes through the full Forge Build/Critique/Test/Gate pipeline regardless of source. Signed Lessons + attestations are a v2+ ambition.
- **`audience_scoping` structure → as documented in §3 with open vocabulary.** Four required keys: `operator` (enum: `operator_only` | `named_users` | `open`), `approved_surfaces` (array of free-form strings), `role_capabilities` (object: role name → array of capability strings), `operator_bypasses` (array of bypass identifier strings). The structural keys are pinned by the JSON Schema; the *vocabulary* of role names, surfaces, and bypass identifiers remains open in v1 and will be tightened when Atlas's `guard.py` is consolidated into the platform (cross-worktree merge).
- **Code-snippet granularity → file-level for v1 (Option A).** Each `blueprint.files[]` entry has one optional `code_snippet` (string) paired with one required `intent` (string). Multi-snippet/multi-intent files convey ordering in prose within the single snippet string. When a real file forces it, add `code_snippets[]` as a sibling field — don't restructure the existing field.

### 11.2 Future evolution (v2+)

- **Two-way back-flow notification** — when an inter-pod notification channel exists, adopting a Lesson on B notifies A's operator that someone else found a useful pattern. Layered onto v7's identity machinery without schema change.
- **Federated Specs** — PR-style mergeable Specs with cross-pod version history. Multi-version work; presupposes operator-side merge tooling that doesn't exist.
- **Signed Lessons with evidence attestations** — cryptographic verification of evidence sources for high-stakes adoption scenarios.
- **Hierarchical objectives** — only if a real app forces the structure.
- **Multi-snippet code blueprints** — `code_snippets[]` sibling field on `blueprint.files[]` for files where multiple distinct snippets/intents must be conveyed.
- **Atlas guard.py consolidation** — pin `audience_scoping` vocabulary (role names, surface IDs, bypass identifiers) once Atlas's working implementation is merged into the platform.

### 11.3 Deferred Atlas gaps (do not block v7)

From the 7 lower-leverage Atlas gaps (see `GAPS.md` on `elegant-bohr-f5356c` worktree):

- **Multi-cron-per-app** — today's `schedules[]` array allows this; revisit if a real app needs more structure.
- **Shared-state-across-apps** — a real cross-app data layer (apps reading each other's state). Defer until a use case surfaces.
- **Taxonomy vocabularies** — pinned vocabulary for `tags`, `objective.primary`, etc. Cosmetic.
- **Governance primitives (rate limits / budgets)** — cost/rate caps per app. Tied to upstream OpenClaw cost telemetry.
- **Cost telemetry per-app** — same dependency.
- **Uninstall symmetry** — clean removal of all Instance state + `bot_guidance` bindings. Worth its own short spec.
- **Surface/channel addressing** — partly subsumed by `audience_scoping.approved_surfaces`.

---

## 12. Summary

v7 splits the app manifest into **App Spec** (canonical, shareable), **App Instance** (local realization, never travels), and **Provenance** (the link). A fourth **Lessons** artifact captures evidence-cited learnings, shareable separately. The marker system continues unchanged but is reframed: manifest as declared-ownership index, markers as file self-identity, scanner as reconciler. Dependencies are first-class with seven kinds (apps, python_packages, system_packages, oc_plugins, oc_skills, integrations, credentials), checked at install time against the receiving bot's capabilities using PEP 440 version constraints. Two new Forge phases (**Reflect**, **Adopt**) close the gaps on in-session LLM file writes and Lesson-driven updates, with conservative non-destructive uninstall semantics (deprecate, don't purge). One new tool primitive (`extend_application`) makes in-session extension viable without raw JSON edits, with a fixed write-order contract for safe failure rollback.

The structural shift is the prior that makes the four Atlas-pressure-test gaps (`event_triggers`, `bot_guidance`, `privacy`, `audience_scoping`) fit cleanly — they're all Spec-side fields and inherit the shareable trajectory.

Land in 5 focused sessions; Atlas ships against v6 in the interim.
