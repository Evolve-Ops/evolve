# App Manifest Specification

**Version:** 4  
**Location:** `/docs/manifest-spec.md`  
**Authoritative store:** `/Users/Shared/evolve/applications/{bot_id}/{app_id}.json`  
**Bot runtime copy:** `~{bot}/.openclaw/workspace/manifests/{app_id}.json`

> **v7-arc note (2026-06-11, manifest-v7 Slice 3a).** New apps no longer
> mint this single-file shape. Forge builds and scanner detections write
> the split v7-arc artifacts natively — App Spec
> (`{shared_dir}/gallery/<tier>/<spec_id>/<spec_version>.json`) + App
> Instance (`manifests/<app_id>.json` with `manifest_shape: "v7-arc"`) +
> embedded Provenance — per
> [spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) and
> [spec-manifest-v7-slicing-2026-06-10.md](spec-manifest-v7-slicing-2026-06-10.md) §5.1.
> This document still describes the legacy single-file shape, which
> remains valid: existing legacy manifests keep working (consumers
> branch on `manifest_shape`) and migrate opportunistically via
> `migrate_v7`. The full docs cutover is Slice 3 proper.

An app manifest is the contract for one application or application running on an OpenClaw bot. It is the primary input to the RSI (recursive self-improvement) loop, and the registration record that tells Evolve what is running, how to test it, and how to grade its health over time.

---

## File Format

JSON. YAML is accepted but JSON is preferred. One file per app. Filename = the app's `id` field + `.json`.

---

## Complete Field Reference

### Identity & Registry

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | **yes** | Slug identifier, e.g. `gmail-fetcher`. Used as the filename. |
| `name` | string | **yes** | Human-readable name, e.g. `Gmail Fetcher`. |
| `bot_id` | string | **yes** | The bot account that owns this app, e.g. `admin-bot`. |
| `description` | string | **yes** | One-paragraph description of what this app does. |
| `status` | string | **yes** | `active` \| `paused` \| `draft` \| `deprecated` |
| `app_version` | string | no | Semver of the app itself, e.g. `0.1.0`. |
| `objective` | string | no | One-sentence goal statement. Complements `purpose`. |
| `owner` | string | no | Bot that owns/runs this app (defaults to `bot_id`). |
| `maintainers` | list[string] | no | Human operators responsible, e.g. `["Pod-admin Alden"]`. |
| `source` | string | no | How this manifest was created: `detected` \| `user-defined` \| `imported`. |
| `confidence` | float | no | Discovery confidence score (0.0–1.0). Set by scanner. |
| `tags` | list[string] | no | Free-form labels for filtering, e.g. `["email", "automation"]`. |
| `last_reviewed` | string | no | ISO date of last human review, e.g. `2026-04-11`. |
| `created_at` | string | auto | ISO datetime set on creation. |
| `updated_at` | string | auto | ISO datetime updated on any change. |

---

### RSI Core — The Four Sections

These four sections are the primary input to Evolve's RSI loop. Fill them out to enable automated improvement proposals.

#### `identity` (object)

Answers: *What is this application and why does it exist?*

```json
"identity": {
  "purpose": "This application exists to fetch and parse Gmail messages — including MIME and attachments — so Admin-bot can triage email without manual intervention.",
  "scope_includes": ["Fetching by message ID", "Parsing MIME multipart", "Extracting attachments"],
  "scope_excludes": ["Sending email", "Managing labels", "Calendar integration"],
  "user": "Pod-admin Alden"
}
```

#### `success_criteria` (object)

Answers: *How do we know it's working?*

```json
"success_criteria": {
  "observable_outcomes": [
    "When given a message ID, returns parsed body within 5 seconds",
    "Attachments > 0 bytes are extracted and saved to workspace"
  ],
  "failure_signals": [
    "OAuth token error not surfaced to operator",
    "Attachment silently dropped with no error"
  ],
  "quality_bar": "Minimum: body text returned. Excellent: all attachments extracted with correct MIME types."
}
```

#### `constraints` (object)

Answers: *What must always be true?*

```json
"constraints": {
  "privacy": ["Email content must not be logged to shared dirs", "OAuth token stored in keystore only"],
  "safety": ["Never delete messages", "Never send or forward without explicit approval"],
  "dependencies": ["Gmail OAuth token in keystore", "scripts/gmail_fetch.py"],
  "boundaries": ["Does not handle calendar", "Does not manage labels"]
}
```

#### `improvement_history` (list of objects)

Answers: *What has been tried and learned?*

Each entry is added automatically by the RSI loop after a change, or manually by the operator.

```json
"improvement_history": [
  {
    "date": "2026-04-10",
    "change": "Added retry logic for 429 rate limit responses",
    "why": "fetch_success_rate dropped to 72% during high-volume days",
    "outcome": "Success rate recovered to 98% within 24 hours"
  }
]
```

#### `satisfaction` (object)

```json
"satisfaction": {
  "score": 8,
  "notes": "Works well, but attachment extraction fails on nested MIME occasionally",
  "rated_at": "2026-04-11"
}
```

---

### Operational / Registry Fields

These fields describe the app's implementation and enable Evolve to test and monitor it.

| Field | Type | Description |
|---|---|---|
| `purpose` | string | 2–3 sentence why-this-matters statement (v3, complements `objective`). |
| `goals` | list[string] | High-level goals for this app. |
| `skills` | list[object] | Convenience index of all files with `layer: "skill"`. Each entry: `name`, `description` (routing signal from front-matter), `file_id`, `path`. Populated and maintained by forge — do not edit manually. |
| `files` | list[object] | All scripts, code files, and assets belonging to this app. Each entry includes `file_id`, `path`, `purpose`, `layer`, `owned_by`, `shared_with`. See gallery-forge spec §4.4–4.5. |
| `crons` | list[string\|object] | Cron entries. String shorthand (crontab format) is accepted; object format is preferred and required for crons with `session_target` or `delivery` intent. See gallery-forge spec §4.7. |
| `inputs` | list[string] | What the app consumes (data, tokens, params). |
| `outputs` | list[string] | What the app produces (files, messages, return values). |
| `exported_hooks` | list[string] | Named functions or entry points other apps/bots can call. |
| `evidence_files` | list[string] | Files that were used to discover this app (set by scanner). |
| `example_triggers` | list[string] | Example user messages that invoke this app. |
| `docs` | list[string] | Paths to documentation files, relative to workspace root. |

---

### Testing

| Field | Type | Description |
|---|---|---|
| `test_command` | string | Shell command to run the app's test suite, executed from the workspace root as the bot user. E.g. `python3 tests/test_gmail_fetch.py`. |
| `test_cases` | list[object] | Structured test case definitions (see below). |
| `test_cadence` | string \| null | Per-app override of `app_testing.default_cadence` in `network.json`. One of `off`, `on_change`, `light`, `strict`. `null` means inherit pod default. See [spec-app-testing-2026-05-07.md](spec-app-testing-2026-05-07.md) §3. |
| `test_exemption_reason` | string | When set, forge enforcement accepts a manifest with no `test_command` and empty `test_cases`. Operator-supplied reason; surfaced as audit trail on the Applications card. Empty string means tests are required. |
| `last_tested` | string | ISO datetime of last test run. |
| `last_test_result` | string | `pass` \| `fail` \| `partial` |
| `last_test_run` | string | ISO datetime of last `test_command` execution. |
| `last_test_output` | string | Truncated stdout/stderr from last `test_command` run. |
| `last_test_exit_code` | int\|null | Exit code from last `test_command` run. |

**Test case object:**

```json
{
  "id": "tc-001",
  "description": "Fetch email by message ID",
  "trigger": "python3 -c \"from gmail_fetch import fetch_email; fetch_email('abc123')\"",
  "expected": "Returns dict with 'body' key, non-empty string",
  "last_run": "2026-04-11T10:00:00Z",
  "last_result": "pass",
  "last_notes": "Output dict had non-empty 'body' field as expected.",
  "last_judge_model": "claude-haiku-4-5-20251001",
  "last_judge_tokens": 412
}
```

| Per-case field | Type | Description |
|---|---|---|
| `id` | string | Stable case id, e.g. `tc-001`. |
| `description` / `name` | string | Human-readable label. |
| `trigger` | string | Shell command that exercises the behaviour. Run as the bot user from the workspace root. |
| `expected` / `expected_behavior` | string | What the captured output should demonstrate. |
| `pass_criteria` | string | Optional sharper rubric the judge uses (falls back to `expected_behavior` when empty). |
| `last_run` | string \| null | ISO datetime of the most recent run. |
| `last_result` | string \| null | `pass` \| `fail` \| `partial` \| `error`. `error` is reserved for trigger/judge failure (separated from `fail` for telemetry). See [spec-behavioral-runs-2026-05-07.md](spec-behavioral-runs-2026-05-07.md) §2. |
| `last_notes` | string \| null | Judge's one- or two-sentence explanation. |
| `last_judge_model` | string | Resolved model id used for the most recent judge call. Empty string when the case has not run. |
| `last_judge_tokens` | int \| null | Combined input + output tokens for the most recent judge call. Powers the daily cost rollup. |

---

### Performance Tracking

| Field | Type | Description |
|---|---|---|
| `rsigrade_signals` | list[string] | Named operational metrics tracked during normal operation. These are labels — the RSI loop watches for them in logs and app output to grade health over time. |

**About `rsigrade_signals`:** There are two ways Evolve tracks app performance:

1. **Operational metrics** (`rsigrade_signals`) — ongoing signals emitted during normal operation. Name the metrics your app naturally produces so the RSI loop knows what to watch for. Examples: `fetch_success_rate`, `attachment_extraction_rate`, `response_latency_p50`. The actual measurement comes from app logs or structured output; `rsigrade_signals` tells the RSI loop what to look for.

2. **Unit tests** (`test_command`, `test_cases`) — periodic QA runs that verify the plumbing works. Cadence is set per-app via `test_cadence` (or pod-wide via `app_testing.default_cadence` in `network.json`); see [spec-app-testing-2026-05-07.md](spec-app-testing-2026-05-07.md) for the cadence model and the smoke-vs-behavioral split. The smoke half (`test_command`) runs at every due tick; the behavioral half (`test_cases`) runs only on `first_run` / `content_changed` and is LLM-judged — see [spec-behavioral-runs-2026-05-07.md](spec-behavioral-runs-2026-05-07.md). Failures generate RSI improvement proposals.

---

### Compliance

| Field | Type | Description |
|---|---|---|
| `compliance_suppressed` | bool | If `true`, this app is intentionally exempt from unregistered-asset flagging. |
| `compliance_suppressed_reason` | string | Why this app is suppressed from compliance checks. |

---

### Legacy Fields

These fields are preserved for backward compatibility. New manifests should prefer the v3/v4 equivalents above.

| Field | Notes |
|---|---|
| `version` | Integer manifest version (not app version). Use `app_version` for semver. |
| `schema_version` | Set automatically. Do not edit. |
| `priority` | `core` \| `feature` \| `optional`. Deprecated — all apps are equally optional. |
| `success_metrics` | Replaced by `success_criteria.observable_outcomes`. |
| `tests` | Replaced by `test_cases`. |
| `desired_improvements` | Replaced by `open_questions`. |
| `evidence` | Replaced by `evidence_files`. |
| `known_issues` | Still valid. List of known bugs or limitations. |
| `open_questions` | List of unresolved design or implementation questions. |
| `privacy_constraints` | Replaced by `constraints.privacy`. |

---

## Complete Sample Manifest

```json
{
  "id": "gmail-fetcher",
  "name": "Gmail Fetcher",
  "bot_id": "admin-bot",
  "description": "Fetches and parses complete Gmail messages, handling MIME multipart and attachments.",
  "status": "active",
  "app_version": "0.1.0",
  "objective": "Enable robust, automatable retrieval of raw emails for triage and forwarding.",
  "owner": "admin-bot",
  "maintainers": ["Pod-admin Alden"],
  "source": "user-defined",
  "confidence": 1.0,
  "tags": ["email", "automation", "gmail"],
  "last_reviewed": "2026-04-11",

  "identity": {
    "purpose": "This application exists to fetch and parse Gmail messages — including MIME and attachments — so Admin-bot can triage email without manual intervention.",
    "scope_includes": ["Fetching by message ID", "Parsing MIME multipart", "Extracting attachments"],
    "scope_excludes": ["Sending email", "Managing labels", "Calendar integration"],
    "user": "Pod-admin Alden"
  },

  "success_criteria": {
    "observable_outcomes": [
      "When given a message ID, returns parsed body within 5 seconds",
      "Attachments > 0 bytes are extracted and saved to workspace"
    ],
    "failure_signals": [
      "OAuth token error not surfaced to operator",
      "Attachment silently dropped with no error"
    ],
    "quality_bar": "Minimum: body text returned. Excellent: all attachments extracted with correct MIME types."
  },

  "constraints": {
    "privacy": ["Email content must not be logged to shared dirs", "OAuth token stored in keystore only"],
    "safety": ["Never delete messages", "Never send or forward without explicit approval"],
    "dependencies": ["Gmail OAuth token in keystore", "scripts/gmail_fetch.py"],
    "boundaries": ["Does not handle calendar", "Does not manage labels"]
  },

  "satisfaction": {
    "score": 8,
    "notes": "Works well. Nested MIME occasionally drops attachments.",
    "rated_at": "2026-04-11"
  },

  "improvement_history": [
    {
      "date": "2026-04-10",
      "change": "Added retry logic for 429 rate limit responses",
      "why": "fetch_success_rate dropped to 72% during high-volume days",
      "outcome": "Success rate recovered to 98% within 24 hours"
    }
  ],

  "purpose": "Provides Gmail retrieval and parsing as a reusable application for Admin-bot.",
  "goals": ["Reliable email retrieval", "Full attachment extraction"],

  "skills": [
    {
      "name": "fetch-email",
      "description": "Use this skill to retrieve a Gmail message by ID and return the parsed body and attachments.",
      "file_id": "f-a1b2c3d4",
      "path": "skills/fetch-email.md"
    }
  ],
  "files": [
    {
      "file_id": "f-a1b2c3d4",
      "file_version": "2026.04.11.1",
      "path": "skills/fetch-email.md",
      "purpose": "Skill: retrieve and parse a Gmail message",
      "layer": "skill",
      "owned_by": "p-gmail-fetcher",
      "shared_with": []
    },
    {
      "file_id": "f-b2c3d4e5",
      "file_version": "2026.04.11.1",
      "path": "scripts/gmail_fetch.py",
      "purpose": "Core Gmail API fetch and MIME parsing logic",
      "layer": "script",
      "owned_by": "p-gmail-fetcher",
      "shared_with": []
    },
    {
      "file_id": "f-c3d4e5f6",
      "file_version": "2026.04.11.1",
      "path": "tests/test_gmail_fetch.py",
      "purpose": "Unit tests for gmail_fetch.py",
      "layer": "test",
      "owned_by": "p-gmail-fetcher",
      "shared_with": []
    }
  ],
  "dependencies": [],
  "crons": [],
  "inputs": ["Gmail OAuth token", "search params (date/from/subject)", "message ID"],
  "outputs": ["Parsed email body (string)", "Decoded attachments (saved to workspace)"],
  "exported_hooks": ["fetch_email", "list_recent_emails"],
  "docs": ["docs/gmail_fetcher.md"],

  "test_command": "python3 tests/test_gmail_fetch.py",
  "test_cases": [
    {
      "id": "tc-001",
      "description": "Fetch email by message ID",
      "trigger": "python3 -c \"from gmail_fetch import fetch_email; print(fetch_email('test-id'))\"",
      "expected": "Returns dict with non-empty 'body' key",
      "last_run": null,
      "last_result": null
    }
  ],
  "last_tested": null,
  "last_test_result": null,
  "last_test_run": null,
  "last_test_output": "",
  "last_test_exit_code": null,

  "rsigrade_signals": ["fetch_success_rate", "attachment_extraction_rate"],

  "evidence_files": ["scripts/gmail_fetch.py"],
  "example_triggers": ["fetch email from Pod-admin", "get message abc123", "list recent emails from noreply@github.com"],
  "known_issues": ["Nested MIME occasionally drops innermost attachment"],
  "open_questions": ["Should attachments be saved to workspace or returned inline?"],
  "privacy_constraints": ["Email content must not be logged to shared dirs"],

  "compliance_suppressed": false,
  "compliance_suppressed_reason": "",

  "created_at": "2026-04-11T10:00:00Z",
  "updated_at": "2026-04-13T10:00:00Z",
  "schema_version": 6,
  "version": 1,
  "priority": "feature"
}
```

---

## Manifest Lifecycle

1. **Auto-generated** — scanner creates a rough draft from detected evidence (crons, scripts, named dirs, memory files)
2. **LLM-enriched** — Haiku fills in `identity`, `success_criteria`, `constraints` from workspace context
3. **User-refined** — operator adds test cases, rates satisfaction, notes issues, sets `last_reviewed`
4. **RSI-maintained** — improvement loop appends to `improvement_history` automatically after each change

Minimum viable manifest: `id`, `name`, `bot_id`, `description`, `status`, plus at least one `success_criteria.observable_outcomes` entry and one `constraints` entry.

---

## Compliance Rules

A manifest is **compliant** if it:
- Exists in `/Users/Shared/evolve/applications/{bot_id}/`
- Has `status` of `active` or `draft` (not `deprecated`)
- Has `schema_version` = 4 (or has been migrated)
- Has at least one `success_criteria.observable_outcomes` entry
- Has `last_reviewed` within the past 90 days (or is newly created)
- If `test_command` is set, `last_test_exit_code` is 0 (or has never been run)

An asset (script, cron, named directory) is **unregistered** if no manifest's `files` or `crons` list references it and `compliance_suppressed` is not set.
