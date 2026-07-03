# Manifest Authoring Guide

**Status:** v0 (draft 2026-05-26)
**Calibrated to:** `forge_engine.py::BUILTIN_BUILDER_PROMPT` @ commit `b51d7dc9`
**Audience:** **Bots and automated processes that produce manifests** — forge, scanner, wizard, Reflect, Adopt, and any hand-authoring agent. Humans may read this; the prose is calibrated for machine consumption.
**Companions:**
- [docs/manifest-spec.md](manifest-spec.md) — schema reference (what fields exist)
- [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — architectural target (the 3-artifact split)
- [docs/spec-export-import-forge-2026-05-26.md](spec-export-import-forge-2026-05-26.md) — process spec for Forge / Reflect+Sanitize / Adopt
- [packages/admin/evolve_admin/applications/forge_engine.py](../packages/admin/evolve_admin/applications/forge_engine.py) — the live forge code

---

## 0. Purpose

This guide tells **any process that produces a manifest** what to put in vs. what to leave for the forge to decide. It is the rubric the forge expects its inputs to be written against.

A manifest is a **blueprint**, not a building. The forge is the contractor. Your job as the author is to describe the building well enough that any qualified contractor produces structurally the same thing. You do not describe how to swing the hammer; you describe what wall goes where and why.

This guide will produce manifests of three quality levels:

- **Skeleton** — enough fields to install but the forge will improvise heavily. Acceptable for stubs.
- **Forge-buildable** — enough specification for a single forge pass to produce a working app. **This is the target for most authoring.**
- **Reference-grade** — additionally includes worked acceptance examples, prior-art references, anti-patterns. Reserved for foundational apps in the gallery.

All three levels follow the same rules; reference-grade just includes more of the optional fields.

---

## 1. What the forge already enforces — do not repeat these

The active builder prompt establishes the following. **Do not duplicate any of this in your manifest.**

| Already enforced by the builder prompt | So your manifest can skip |
|---|---|
| `## FILE: <relative-path>` output format with fenced code blocks | Output format conventions |
| All files included in full — no truncation, no placeholders, no TODOs | "Please write complete files" |
| No provenance comment lines (auto-added by the bot) | Provenance markers |
| Test directive required (`## TEST_COMMAND:` or `## TEST_EXEMPTION_REASON:`) | "Please add a test" |
| Defensive code for first-run with no existing data | "Handle the case where no data exists yet" |
| Persona: "skilled software engineer building an app for an OpenClaw AI assistant bot" | The platform name and persona |
| Critic uses six lenses (completeness, first-run safety, longitudinal trust, edge cases, simplicity, integration) | "Please review for X" |

If you find yourself writing one of the right-column phrases, delete it. The forge already handles it. Use the space to specify something the forge cannot infer.

---

## 2. The five jobs of a Spec

Every Spec does these five jobs. If a job is missing or ambiguous, the forge improvises — usually badly.

1. **Identity** — *What is this and why does it exist?* Purpose, scope_includes, scope_excludes, intended user.
2. **Success criteria** — *How will we know it's working?* Observable outcomes, failure signals, quality bar.
3. **Constraints** — *What must always be true?* Privacy, safety, dependencies, boundaries.
4. **Blueprint (`build_spec`)** — *What is the forge supposed to build?* File structure, key responsibilities per file, integration points, non-obvious algorithmic choices.
5. **Privacy + audience scoping** — *Who can do what with this app?* The trust boundary that the Sanitizer (on export) and the audience guard (at runtime) use.

The first four are universal. The fifth is required for any app that handles user-identifying data OR interacts with multiple human audiences.

---

## 3. The Goldilocks principle

**Specify:** intent, integration points, non-obvious algorithmic choices, file structure with shared modules, acceptance criteria, privacy posture, audience scoping.

**Defer to the forge:** implementation details, naming inside files, stylistic choices, defensive coding patterns, error message wording, code organization within a file, comment density, choice of stdlib vs. dependency, exact log format (unless load-bearing for tests).

### Decision rule

For each piece of content you're about to write, ask:

| Question | If yes → | If no → |
|---|---|---|
| Would two correct implementations differ on this? | **Specify** | Defer |
| Is this load-bearing for a downstream contract (other apps, audit, export)? | **Specify** | Defer |
| Is this a non-obvious choice that affects correctness? | **Specify** | Defer |
| Is this a stylistic preference about the same correct behavior? | Defer | (n/a) |
| Could the forge infer this from the bot's existing patterns? | Defer | Specify |

### Size heuristics (advisory, not absolute)

| App complexity | Target `build_spec` length |
|---|---|
| Tiny (single-shot CLI, one file, no data) | 20–50 lines |
| Medium (cron + external APIs, single domain) | 100–250 lines |
| Complex (multi-file, shared modules, event-driven, recursive LLM) | 250–500 lines |

If your `build_spec` is over 800 lines, you are almost certainly writing code-in-prose. Stop, classify what's actually load-bearing, and remove the rest.

---

## 4. Instance vs Spec — the prose generalization rule

**This is the most important rule in this guide.** A manifest lives in two forms:

- **Instance manifest** — the version on the bot that owns the running app. May reference the user by name, describe specific personal context, tie into the bot's SOUL.md and adjacent apps. *This is correct and desirable locally.*
- **Spec** — the exportable, shareable form derived by Reflect+Sanitize. Generalized; no PII; describes the *shape* of the app, not the *person* it serves.

The same field is written differently in each form.

### Example: `description`

| Form | Text |
|---|---|
| **Instance** | "Help Pod-Admin manage his weight by tracking protein, fiber, and sugar intake. Knows about his Zepbound dose schedule and integrates with his morning briefing." |
| **Spec** | "Personal health app that tracks protein, fiber, and sugar intake. Optionally surfaces summary data to a morning briefing." |

The Instance description is valuable to keep — it lets the bot reason about the app in context with other apps and with the user's memory. The Spec description is what *travels*.

### Example: `identity.scope_includes`

| Form | Text |
|---|---|
| **Instance** | "Logs Pod-Admin's daily macros via Telegram messages he sends after each meal. Reminds him to log dinner if not seen by 8pm." |
| **Spec** | "Logs user macros via the configured messaging channel. Sends a reminder if no log entry is received by a configurable evening deadline." |

### Generalization rules

When writing **Spec-shape** prose:

1. **People → roles.** `Pod-Admin` → `the user` / `the operator` / `{user_name}`. Identified names of family/team/clients → `family member` / `team member` / `client`.
2. **Specifics → categories.** `Zepbound` → `prescribed weight-loss medication` (or, often better, just drop the specific reference — the Spec doesn't need to know which medication). `Boston` → `the user's location` / `{location}`.
3. **Idiosyncrasies → configurations.** `at 8pm` → `at a configurable evening deadline (default 20:00)`. `via Telegram` → `via the configured messaging channel`.
4. **Stories → patterns.** "After Pod-Admin's surgery, this app started reminding him about hydration too" → drop. The Spec describes the shape, not the history.
5. **Memory references → drop.** `Knows about his recent visit to Dr. Patel` → drop. Memory is bot-local.
6. **Adjacent-app references → generalize.** `Integrates with Pod-Admin's morning briefing` → `Optionally exposes summary data to a morning-briefing-shaped app`. Don't assume the recipient pod has the same app suite.

When writing **Instance-shape** prose (in a running bot):

- Reference the user by name if your bot already does.
- Reference adjacent apps by their actual names.
- Reference SOUL.md / AGENTS.md context freely.
- The bot's LLM can use this richness to reason about the app within its broader context.

The Sanitizer's job (see export spec) is to derive Spec-shape from Instance-shape automatically. Your job, when authoring directly, is to know which mode you're in:

- **Hand-authoring a new gallery app?** Write Spec-shape from the start.
- **Hand-authoring on a running bot?** Write Instance-shape; Sanitizer derives the Spec on export.
- **Scanner inferring from existing files?** Write Instance-shape; the bot's context is the inference source.
- **Wizard producing a new app via chat?** Write Instance-shape; the user is sitting right there.
- **Adopt receiving a foreign Spec?** Spec-shape arrives; Instance is built fresh during Build phase.
- **Reflect refreshing an existing Instance?** Instance-shape stays Instance-shape.

---

## 5. Field-by-field anatomy

What to put in every field. Refer to [docs/manifest-spec.md](manifest-spec.md) for the formal schema; this section adds *how to write the content*.

### 5.1 `description` (required)

One-paragraph description. **Spec-shape on export, Instance-shape on bot.**

- Under 80 words.
- Leads with what the app does (verb + object), follows with the user/context, ends with the differentiator if any.
- ✗ Bad (Instance, too local): "Tracks Pod-Admin's protein, fiber, and sugar and tells him when he's behind. Knows about Zepbound."
- ✗ Bad (Spec, too vague): "A health app."
- ✓ Good (Spec): "Personal health app that tracks daily macronutrient intake (protein, fiber, sugar) via short messages and surfaces same-day shortfalls."

### 5.2 `objective` (required)

One sentence. The *outcome* the user gets, not the mechanism.

- ✗ "Lets the user send macros via Telegram."
- ✓ "Make it effortless for the user to keep daily macronutrient balance visible without spreadsheets."

### 5.3 `identity.purpose` (required)

2–3 sentences. Extends `objective` with the *why now*: what problem this solves that the user wouldn't otherwise solve.

### 5.4 `identity.scope_includes` / `scope_excludes` (required)

Bulleted lists. 5–10 bullets each. **The exclusion list is as important as the inclusion list.**

- `scope_includes`: capabilities the user can rely on this app for.
- `scope_excludes`: explicit non-goals. Apps without exclusions tend to grow into mush; exclusions are how you protect the app from scope creep.

### 5.5 `identity.user` (required)

Who the app serves. **Always Spec-shape, even in Instance form** — write "the operator" / "the user" / "a member of the configured group," not the user's name. (This is the one field that has no Instance/Spec distinction.)

### 5.6 `identity.bot_interaction_pattern` (recommended)

How the bot is involved. Cron-driven? Event-driven? Reactive to specific commands? Used during the build to inform integration choices.

### 5.7 `success_criteria.observable_outcomes` (required)

5–8 bullets. Each one must be **verifiable** — you can write a test against it. Vague aspirations ("the user is happier") are not observable outcomes; specific behaviors ("a digest message arrives within 5 minutes of the configured time every day") are.

### 5.8 `success_criteria.failure_signals` (required)

What does broken look like? Specific signals the verify daemon can monitor: missing log entries, signal prefixes from the app, expected files absent, expected reactions absent.

### 5.9 `success_criteria.quality_bar` (required)

`minimum` (acceptable on first run) and `excellent` (target after a few weeks of refinement). Used by the test runner and the Critic to calibrate.

### 5.10 `constraints.privacy` (required)

Free-form rules about what must not be logged, transmitted, or shared. **Distinct from the `privacy` block (§5.13)** which is structured.

### 5.11 `constraints.safety` (required)

Hard rules regardless of user instructions. Default-deny for destructive actions: "never delete," "never send without approval," "never spend over $X without re-confirm."

### 5.12 `constraints.boundaries` (required)

What this app *does not* do — even if a user asks. Different from `scope_excludes` (which is about what's in/out of intended use); boundaries are about what's prohibited regardless of intent.

### 5.13 `privacy` block (v0 expectation; required for any app handling user-identifying data)

Structured, machine-checkable. Drives the Sanitizer on export and the audit/observation features at runtime. **Required for any app that:**
- Captures or stores anything about the user
- Posts messages identifiable to specific people
- Logs interactions
- Calls external APIs with user data

Structure:

```jsonc
"privacy": {
  "data_collected": [
    // Each entry: kind of data + retention + raw-vs-processed
    {"kind": "user message content", "retention": "indefinite (archive)", "processed": "summarized"},
    {"kind": "user telegram_id", "retention": "indefinite (hashed)", "processed": "salted hash"}
  ],
  "retention": {
    // Per data class
    "archive": "indefinite — user removes via /optout",
    "audit_log": "90 days",
    "raw_messages": "never stored — summarized only"
  },
  "opt_out_signals": [
    // How a user opts out
    {"signal": "telegram_command:/optout <url>", "scope": "per_url", "effect": "delete_archive_entry"},
    {"signal": "telegram_command:/optout-all", "scope": "per_user", "effect": "delete_all_entries_for_user"}
  ],
  "consent_notice": {
    "trigger": ["install", "new_group_member"],
    "channel": "primary_group_pinned",
    "content_ref": "AGENTS.md#privacy-notice"
  },
  "identifier_hashing": {
    "salt_path": "atlas/.capture-salt",
    "algorithm": "sha256-prefix-16"
  },
  "shareable_in_lessons": true   // May lessons-derivations from this app be shared?
}
```

The block is **declarative** — the app's code is expected to enforce what's declared. The Critic verifies declarations match enforcement.

### 5.14 `audience_scoping` block (v0 expectation; required for any interactive app)

Structured, machine-checkable. The trust boundary that determines who can do what.

```jsonc
"audience_scoping": {
  "operator_required": true,
  "approved_surfaces": [
    {
      "surface_id": "primary_group",
      "surface_type": "telegram_supergroup",
      "addressing": {"chat_id_var": "{telegram_chat_id}"},
      "default_role_in_surface": "member"
    }
  ],
  "role_capabilities": {
    "operator": ["all"],
    "member":   ["research", "capture", "opt-out", "opt-out-all"],
    "stranger": []   // empty array = silently ignore
  },
  "membership_verification": {
    "method": "telegram_get_chat_member",
    "cache_ttl_seconds": 300
  },
  "operator_bypasses": ["rate_limit", "budget_cap"],
  "config_path": "atlas/operator.json"
}
```

If your app has any interactive surface (group chat, DMs, commands), specify this. If your app is purely cron-driven and writes only to the bot's own workspace, you can omit.

### 5.15 `event_triggers` block (v0 expectation; required for event-driven apps)

```jsonc
"event_triggers": [
  {
    "id": "url_in_group_message",
    "source": "telegram",
    "match": {"kind": "group_message", "filter": "contains_url"},
    "handler_command": "scripts/atlas_capture.py process",
    "argument_mapping": {
      "--url": "$.urls[*]",
      "--message-id": "$.message_id",
      "--member-id": "$.from.id",
      "--chat-id": "$.chat.id",
      "--chat-type": "$.chat.type"
    }
  }
]
```

If your app responds to incoming messages, slash commands, or reactions, specify this. Cron-driven apps don't need it.

### 5.16 `bot_guidance` block (v0 expectation; required for event-driven apps)

Sections to splice into the bot's AGENTS.md so the bot knows when to invoke this app's handlers. The provisioner installs these; uninstall removes them cleanly.

```jsonc
"bot_guidance": [
  {
    "section": "Atlas — Article Capture",
    "content": "When you receive a group message containing one or more URLs: ..."
  }
]
```

The content uses the same `{var}` substitution as the rest of the manifest.

### 5.17 `files[]` (required if app produces files)

Each entry classifies a file the app owns. Schema:

```jsonc
{
  "file_id": "f-a1b2c3d4",       // Stable id (managed by forge/scanner)
  "path": "scripts/foo.py",       // Relative to workspace root
  "purpose": "Main CLI for the daily-digest pipeline",
  "layer": "script",              // script | test | skill | data | doc
  "data_kind": null,              // null unless layer == "data"; otherwise: template | seed | runtime-generated | user-data
  "owned_by": "p-app-id",
  "shared_with": []               // Other apps (by spec_id) that may read/write
}
```

The `data_kind` discriminator is load-bearing for export. See §6.

### 5.18 `skills[]` (required if app uses skills)

```jsonc
"skills": [
  {"id": "telegram", "required": true},
  {"id": "brave", "required": true},
  {"id": "github", "required": false}
]
```

Names must match the resolver's known skill ids (see `bot_templates/resolver.py`).

### 5.19 `app_dependencies` (required if app reads/writes another app's interface)

```jsonc
"app_dependencies": [
  {
    "spec_id": "p-atlas-daily-digest",
    "interface": ["archive/index.json (read)", "archive/{bucket}/*.md (read)"],
    "version_compatibility": ">=1.0,<2.0"
  }
]
```

The forge injects the dependency's interface_contract + the actual source files of declared dependencies into the builder context. Don't repeat their content in your build_spec.

### 5.20 `build_spec` (required)

The blueprint text the forge reads to build the app. Free-form, but expected sections (in order):

1. **Overview** (1 paragraph) — what this app is, in 4–6 sentences.
2. **File layout** — directory tree of files to produce + their roles.
3. **Per-file specs** — for each non-trivial file: purpose, public interface, key responsibilities, non-obvious algorithmic choices. NOT the implementation code (the forge writes that).
4. **Configuration files** — what config files exist, their schemas, their value-vs-template status.
5. **Integration with other apps / shared modules** — what's imported from where, what's expected to exist.
6. **Test surface** — what tests should validate. Specific scenarios, not just "tests pass."
7. **What NOT to include** — explicit anti-scope. (e.g., "no CLI; this is library-only.")

Use `## FILE: <path>` blocks **only for templates** the forge should literally render (e.g., a config JSON template, a shell script template, a plist with substituted vars). For files where the forge generates the content, describe the contract not the code.

### 5.21 `test_command` / `test_cases` / `test_exemption_reason` (one required)

- `test_command`: shell command run by the test gate. Must exit 0 on success.
- `test_cases`: structured cases for LLM-judged behavioral tests (rare; expensive).
- `test_exemption_reason`: a one-line reason this app is too trivial to test. **Use sparingly** — the test gate is the forge's main feedback loop.

### 5.22 `usage` block — discoverability surface (required for user-routed apps)

The bot's LLM reads `INSTALLED_APPS.md` at session start to know what apps it has and when to invoke them. That file is rendered from a fixed set of manifest fields. **If those fields are empty, the app is structurally installed but conversationally invisible** — the bot has no way to recognize a user's intent and call it, so it falls back to general tools and bypasses the app's scope / grounding / privacy controls.

The audit's `app_discoverability_*` assertions enforce this contract at the Tier-2 sweep, and the forge surfaces a warning at apply time when the fields below are thin. Treating discoverability as "we'll fill it in later" loses you both gates.

```jsonc
"usage": {
  "model": "user-initiated",       // user-initiated | scheduled | event-driven | ambient
  "how_to_use": "When the user wants to capture a thought, call journal-add with the text.",
  "trigger_recognition": {
    "hint_words": ["journal", "log", "capture", "remember", "note"],
    "pattern": "user says 'log X' or 'remember X' or 'journal X'",
    "requires_keyword": false       // true = only fire on an explicit hint word
  },
  "bot_voice_examples": [
    "Logged to your journal.",
    "Got it — saved to your morning thoughts."
  ],
  "auto_capture": {
    "enabled": false,               // true = capture matching content without being told to
    "sources": []                   // e.g. ["telegram_dm", "group_mention"]
  }
}
```

Plus, at the top level:

```jsonc
"example_triggers": [
  "log this: had a great meeting with Z",
  "journal — figured out the deploy bug",
  "remember to ask Alex about the budget"
],
"interface_contract": {
  "cli": [
    { "command": "journal-add", "key_flags": ["--text", "--tag", "--date"] }
  ]
}
```

#### What gates fire on what

| Field | Audit assertion | Forge warn | Why it matters |
|---|---|---|---|
| `usage.model` | `app_discoverability_no_invocation_model` (minor) | warn if unset | Tells the LLM *when* the app fires; also gates the routing-only checks (scheduled / event-driven apps skip them) |
| `usage.how_to_use` *(or `description` / `identity.purpose`)* | `app_discoverability_no_how_to_use` (major) | warn if all three empty | The renderer's main prose — LLM has nothing to read otherwise |
| `usage.trigger_recognition.hint_words` *(union with `capability_tags` + `session_keywords`)* | `app_discoverability_thin_hint_words` (major) | warn if `<3` total | Routing words the LLM scans user messages for |
| `example_triggers` | `app_discoverability_no_example_triggers` (major) | warn if empty | Pattern-match examples for intent recognition |
| `interface_contract.cli[].command` | `app_discoverability_no_cli` (major) | warn if empty for user-routed | The bot literally has no command to call |

`bot_voice_examples` and `auto_capture` are not currently gated but shape the rendered entry — fill them when they're load-bearing for the bot's voice or capture behavior.

#### `usage.model` selection

| Value | Bot behavior | Routing fields required? |
|---|---|---|
| `user-initiated` | Invoke when the user asks | Yes |
| `scheduled` | Runs on cron; bot relays results when they arrive | No — bot doesn't recognize intent |
| `event-driven` | Runs on an external event (incoming message, webhook); bot surfaces results | No |
| `ambient` | Bot decides from conversation context | Yes — even more important; no explicit trigger to fall back on |

Pick the one that matches the app's actual lifecycle. **If you're unsure, it's almost certainly `user-initiated`** — the routing checks then apply.

#### Where the renderer reads from

If you want to confirm what your manifest looks like to the bot, the source of truth is `render_installed_apps_md` in [`packages/admin/evolve_admin/applications/app_registry.py`](../packages/admin/evolve_admin/applications/app_registry.py). Browse to your bot's `~/.openclaw/workspace/INSTALLED_APPS.md` after apply to see the rendered entry.

---

## 6. The three file classes + `data_kind` discriminator

Every file in `files[]` is classified by the export's Sanitizer. The `data_kind` field, combined with `layer`, determines whether the file travels with a Spec.

| `layer` | `data_kind` | Class | Travels in Spec? | Example |
|---|---|---|---|---|
| `script` | n/a | Build artifact | **No** — forge rebuilds | `scripts/foo.py` |
| `test` | n/a | Build artifact | **No** — forge rebuilds | `tests/test_foo.py` |
| `doc` | n/a | Build artifact | **No** — forge rebuilds | `AGENTS.md` section |
| `skill` | n/a | Build artifact | **No** — forge rebuilds | A skill .md file |
| `data` | `template` | Critical data, generic | **Yes, with placeholders** | `atlas/sources.json.template` |
| `data` | `seed` | Critical data, generic | **Yes, with possible sanitization** | A starter taxonomy, default RSS list |
| `data` | `runtime-generated` | Forge-managed runtime state | **No** — fresh on install | An empty `optout.json` |
| `data` | `user-data` | User data | **No** — never travels | `archive/` contents, daily logs |

### Decision tree

When you classify a file, ask in order:

1. **Will the forge regenerate this file from the build_spec?** → `layer != "data"`. Don't ship the contents; ship the blueprint.
2. **Is this generic data the app needs to function but does not contain user-specific content?** → `layer: "data", data_kind: "template"` or `"seed"`. Ship it.
3. **Is this data the app produces during operation that's specific to this user?** → `layer: "data", data_kind: "user-data"`. Do not ship.
4. **Is this data the app needs but creates fresh on install (empty index, empty cache)?** → `layer: "data", data_kind: "runtime-generated"`. Don't ship; the install path creates it.

### Templates vs seeds

- `template`: contains placeholder values the recipient must fill in (e.g., `operator.json` with empty `operator_telegram_user_id`).
- `seed`: contains real, generalizable data (e.g., a default RSS feed list, a default 5-bucket taxonomy). May still pass through the Sanitizer to confirm no leakage.

### When in doubt

Mark as `user-data`. The Sanitizer will surface low-confidence classifications to the operator for review on export.

---

## 7. Multi-app and shared-module patterns

Atlas-style suites: multiple apps sharing common code (e.g., `atlas_lib/`). Rules:

### 7.1 Declare the shared owner explicitly

One app owns the shared module. Its `build_spec` lists the shared files as part of its `files[]` with `shared_with` populated:

```jsonc
{
  "path": "scripts/atlas_lib/guard.py",
  "layer": "script",
  "owned_by": "p-atlas-daily-digest",
  "shared_with": ["p-atlas-article-capture", "p-atlas-on-demand-research", "p-atlas-weekly-recap"]
}
```

### 7.2 Dependents declare via `app_dependencies` + `shared_modules`

Each dependent app lists the owner in `app_dependencies` and adds a `shared_modules` field for clarity:

```jsonc
"app_dependencies": [{"spec_id": "p-atlas-daily-digest", "interface": ["atlas_lib/guard.py (read)"]}],
"shared_modules": [
  {"name": "atlas_lib.guard", "owner_spec_id": "p-atlas-daily-digest", "expected_exports": ["classify", "read_operator_config"]}
]
```

### 7.3 Install order

The owner app installs first. The forge resolves this automatically from `app_dependencies` (topological sort).

### 7.4 In the build_spec

The owner's `build_spec` describes the shared module as a normal component. Dependents' `build_specs` state explicitly:

> "This app imports from `atlas_lib.guard` (owned by `p-atlas-daily-digest`, expected exports: `classify`, `read_operator_config`). Do not redefine these; import them. If the import fails, surface a clear error and exit 2."

Without this, the forge may try to redefine the shared module per app — producing inconsistent copies.

---

## 8. LLM access — apps inherit the bot's stack

If your app makes LLM calls (a classifier, a synthesizer, a chat backend), the manifest declares it via `recursive_llm` — but **the call routes through the bot's gateway**, never via a per-app credential. The bot's configured provider, model, tier-walk fallback, `daily_cap_usd` L1 auto-trip, cost monitoring, and prompt caching govern every call the app makes.

### 8.1 The rule

Apps installed via the Forge MUST NOT:

- Carry their own LLM credentials (`api_key`, `ANTHROPIC_API_KEY`, equivalents) in any workspace file.
- Call provider APIs directly (`api.anthropic.com`, OpenAI, etc.) from Python, Node, or shell scripts the app installs.
- Declare `api_key_source` or any equivalent per-app credential pointer in the manifest.

Apps that violate this rule escape every per-bot LLM safeguard: tier-walk fallback, `daily_cap_usd` auto-trip, `cost_watchdog` + heartbeat-bloat detection, LLM-provider-agnostic routing ([docs/principle-llm-provider-agnostic.md](principle-llm-provider-agnostic.md)), prompt caching, credential rotation. They also produce permanent `compliance_scan` false positives on their workspace credential file.

This is the same class of bypass as agent-freelance ([docs/spec-agent-freelance-bypass-2026-06-05.md](spec-agent-freelance-bypass-2026-06-05.md)), different vector. See [docs/spec-apps-inherit-bot-llm-2026-06-06.md](spec-apps-inherit-bot-llm-2026-06-06.md) for the principle and rationale.

### 8.2 The right pattern — three transports

The app declares *intent* in the manifest ("needs a cheap classifier returning JSON"); the bot's stack decides *how*. Three transports:

| Transport | When to pick | How it routes |
|---|---|---|
| `bot_tool` | App is agent-loop driven (event triggers, slash commands, mentions). | App registers a tool the bot's agent calls during its turn. LLM call happens inside the bot's session — full stack inherited automatically. |
| `subagent` | App needs a narrow-scoped sub-conversation (e.g., research synthesis with controlled tool surface). | Trigger or cron invokes a subagent via OC's `subagents.tools.allow/deny` (see [docs/schemas/oc-config-schema.txt](schemas/oc-config-schema.txt)). |
| `openclaw_headless` | App is cron-only and needs structured one-shot LLM output (e.g., daily classification batch). | Cron shells out to `openclaw agent --local --agent main --json --timeout N --message "<prompt>"` (with `cwd="/tmp"` to avoid OC's `uv_cwd()` permission check) and parses the JSON response. Reference impl: `packages/analyzer/app_audit_tier3.py::_dispatch_via_oc_full` (heartbeat / tier-3 audit dispatch). The Atlas suite uses `atlas_lib/oc_dispatch.py` as the per-app wrapper. |

The transport choice is declared in `recursive_llm.transport`. See [docs/system/RUNTIME_NOTES.md](system/RUNTIME_NOTES.md) for the platform-side mechanism details that may change between OC releases.

### 8.3 Declaration

```jsonc
"recursive_llm": {
  "purposes": [
    {"name": "classifier",
     "intent": "classify item into one of 5 buckets or 'skip'; JSON-only output",
     "expected_cost_per_day_usd": 0.30},
    {"name": "synthesizer",
     "intent": "compose digest narrative from classified items",
     "expected_cost_per_day_usd": 1.00}
  ],
  "transport": "openclaw_headless",   // bot_tool | subagent | openclaw_headless
  "fallback_required": true,
  "retry_policy": "exponential_backoff_max_3"
}
```

Note what's absent:

- No `providers` field — the bot's provider config governs.
- No `api_key_source` — apps don't credential themselves. Direct credential pointers in the manifest are rejected by the forge import gate (Phase 3 of the spec above).
- No `model` field — `intent` is what the bot needs to pick a model. The bot's tier ladder + provider routing decides which model serves each intent. (For load-bearing model constraints — e.g., "must be a model with vision" — declare via `intent` text, not by hardcoding a model id.)

### 8.4 What the forge enforces when `recursive_llm` is present

- Retry with backoff: exponential, max 3 retries for transient errors (5xx, timeouts, 429).
- Fallback behavior: if `fallback_required: true`, the code must have a non-LLM fallback path (e.g., the classifier defaults to `bucket: "skip"` if the gateway is unreachable).
- Error logging: every LLM error logged with context (timestamp, error code, what was being done).
- Cost telemetry: per-call cost rolls up to the bot's daily total — the same number `daily_cap_usd` watches.
- No infinite retry loops.
- No direct provider API calls in generated code. The forge produces transport-appropriate scaffolding (tool registration, subagent invocation, or headless prompt) — never `urllib.request` against a provider endpoint.

### 8.5 What you should write in `build_spec`

For each LLM purpose: a short paragraph describing the intent, the expected output shape (JSON contract for parseable outputs), how malformed responses are handled, and the fallback behavior. Do NOT specify the model — the bot's tier ladder decides. Do NOT describe API authentication — there is none from the app's perspective.

### 8.6 Pre-2026-06-06 manifests

The Atlas reference suite ([docs/atlas-app-manifests/](atlas-app-manifests/)) was authored before this rule and declares `api_key_source: "atlas/llm-config.json"` plus a per-app credential file. These are the regression case driving [docs/spec-apps-inherit-bot-llm-2026-06-06.md](spec-apps-inherit-bot-llm-2026-06-06.md) and are scheduled for Phase 2 rearchitect. **Do not use them as forging references** for the LLM-access shape until rearchitected. The rest of those manifests (audience_scoping, privacy block, event_triggers, bot_guidance) remains the working reference.

---

## 9. Worked examples

### 9.1 Tiny — `greeting`

A single-shot app that posts a daily good-morning message.

```jsonc
{
  "spec_id": "p-greeting",
  "name": "Daily Greeting",
  "description": "Sends a daily good-morning message to the operator at a configured time.",
  "objective": "Open the user's day with a warm acknowledgement.",
  "identity": {
    "purpose": "Many users like a small daily 'I'm here, good morning' signal from their bot before the day's busier interactions.",
    "scope_includes": [
      "One message per day at the configured time",
      "Personalization via the configured user name and a small rotation of greeting templates"
    ],
    "scope_excludes": [
      "Holidays, special occasions, or context-aware messages — pure routine greeting",
      "Any kind of mood detection, calendar awareness, or content beyond the greeting itself"
    ],
    "user": "the operator",
    "bot_interaction_pattern": "Cron-driven daemon. No bot session invocation."
  },
  "success_criteria": {
    "observable_outcomes": [
      "A greeting arrives at the configured time, every day, within 2 minutes",
      "The greeting includes the user's configured name"
    ],
    "failure_signals": [
      "GREETING_FAILED: signal in logs — check gateway config"
    ],
    "quality_bar": {
      "minimum": "Right time, right name, no duplicates",
      "excellent": "Varied templates feel natural; one missed day in 365"
    }
  },
  "constraints": {
    "privacy": ["No data captured beyond the configured user name"],
    "safety": ["Read-only — sends one outbound message per day"],
    "boundaries": ["One message per day, ever. Never two."]
  },
  "build_spec": "Single Python file scripts/greeting.py with a 'send' subcommand. Reads the configured user_name and greeting_time from network.json under bots[bot_id].capabilities.greeting. Picks a greeting template from a small built-in rotation (5-8 templates). Sends via the bot's gateway. Emits GREETING_SENT: or GREETING_FAILED: signal. Idempotent — does not send if today's greeting already sent.\n\nFile layout:\n- scripts/greeting.py — CLI: send | preview\n- scripts/greeting-cron.sh — cron trigger\n- /Library/LaunchDaemons/com.{bot_id}.greeting.plist — LaunchDaemon at {greeting_time} daily\n\nNot included: no archive, no logs beyond signal emission, no LLM calls.",
  "test_exemption_reason": "Single-file CLI with one external call. Operator-visible failure signal is the test."
}
```

`build_spec`: ~20 lines. No `recursive_llm`, no `event_triggers`, no `audience_scoping` (it's operator-only). Trivial app, trivial blueprint.

### 9.2 Medium — `morning-briefing`

Pattern already in the gallery. See `gallery/bot-templates/morning-briefing/apps/morning_briefing.json` for the full reference.

Key points illustrated:
- `build_spec` ~150 lines, narrative description with explicit `## FILE:` blocks **only for the cron script and plist** (which are templates with substitutions). The Python file (`briefing.py`) is NOT inlined; the build_spec describes it well enough that the forge writes it.
- `skills[]` declares Gmail + Calendar with required scopes.
- `app_dependencies` is empty (foundational app).
- `recursive_llm` is empty (no LLM calls inside the app — just data assembly).
- `event_triggers` is empty (cron-only).
- No `audience_scoping` (operator-only).

### 9.3 Complex — `atlas-daily-digest` (excerpt)

Full manifest at `docs/atlas-app-manifests/atlas-daily-digest.json` (drafts).

```jsonc
{
  "spec_id": "p-atlas-daily-digest",
  "name": "Atlas Daily Digest",
  "description": "Community research digest. Crawls configured RSS, GitHub releases, and a small set of search queries once per day, classifies into five buckets, posts a Team-Bot-A-style digest to the configured Telegram group.",
  "objective": "Give an enthusiast community a single concise morning digest of what changed in the ecosystem, classified for skimming.",
  "identity": {
    "purpose": "...",
    "scope_includes": ["..."],
    "scope_excludes": [
      "Synthesizing strategic recommendations (the operator's job)",
      "Posting anywhere other than the configured Telegram group"
    ],
    "user": "members of the configured Telegram group + the operator",
    "bot_interaction_pattern": "Cron-driven; no bot session invocation."
  },
  // ... success_criteria, constraints, files, etc.
  "skills": [
    {"id": "telegram", "required": true},
    {"id": "brave", "required": true},
    {"id": "github", "required": false}
  ],
  "app_dependencies": [],
  "shared_modules": [
    {"name": "atlas_lib.classifier", "owner_spec_id": "p-atlas-daily-digest", "expected_exports": ["classify"]},
    {"name": "atlas_lib.fetchers", "owner_spec_id": "p-atlas-daily-digest", "expected_exports": ["fetch_rss", "fetch_github_releases", "brave_search"]}
  ],
  "recursive_llm": {
    "purposes": [
      {"name": "5-bucket classifier",
       "intent": "classify item into one of 5 buckets or 'skip'; JSON-only output",
       "expected_cost_per_day_usd": 0.10}
    ],
    "transport": "openclaw_headless",
    "fallback_required": true,
    "retry_policy": "exponential_backoff_max_3"
  },
  "event_triggers": [],
  "audience_scoping": {
    "operator_required": true,
    "approved_surfaces": [{"surface_id": "primary_group", "surface_type": "telegram_supergroup", "addressing": {"chat_id_var": "{telegram_chat_id}"}, "default_role_in_surface": "member"}],
    "role_capabilities": {"operator": ["all"], "member": ["read-digest"], "stranger": []},
    "config_path": "atlas/operator.json"
  },
  "privacy": {
    "data_collected": [
      {"kind": "URLs crawled from public sources", "retention": "indefinite", "processed": "summarized + classified"}
    ],
    "retention": {"archive": "indefinite", "digest_history": "indefinite"},
    "opt_out_signals": [],
    "shareable_in_lessons": true
  },
  "files": [
    {"path": "scripts/atlas_digest.py", "layer": "script", "data_kind": null, "owned_by": "p-atlas-daily-digest", "shared_with": []},
    {"path": "scripts/atlas_lib/classifier.py", "layer": "script", "data_kind": null, "owned_by": "p-atlas-daily-digest",
     "shared_with": ["p-atlas-article-capture", "p-atlas-on-demand-research", "p-atlas-weekly-recap"]},
    {"path": "atlas/sources.json", "layer": "data", "data_kind": "template", "owned_by": "p-atlas-daily-digest", "shared_with": []},
    {"path": "archive/index.json", "layer": "data", "data_kind": "runtime-generated", "owned_by": "p-atlas-daily-digest",
     "shared_with": ["p-atlas-article-capture", "p-atlas-weekly-recap"]},
    {"path": "archive/{bucket}/*.md", "layer": "data", "data_kind": "user-data", "owned_by": "p-atlas-daily-digest", "shared_with": []}
  ],
  "build_spec": "/* ~400 lines describing: cron schedule via {digest_time}/{time_zone}, source crawler patterns, classifier prompt structure, archive write semantics, digest composition (Team-Bot-A-style), Telegram post via API client (token from skills/telegram.json + chat_id from atlas/operator.json), signal prefixes for verify daemon, shared atlas_lib structure with explicit exported names. */",
  "test_command": "python3 -m py_compile scripts/atlas_digest.py scripts/atlas_lib/*.py && python3 scripts/atlas_digest.py preview --bot-id $BOT_ID --detail concise > /dev/null"
}
```

Key points illustrated:
- `shared_modules` makes atlas_lib explicit.
- `recursive_llm` declared because the classifier calls Haiku.
- `audience_scoping` declared for group-vs-DM differentiation.
- `privacy` declared even though no member data is captured by digest itself (other Atlas apps do; the digest is part of the same trust boundary).
- `files[]` shows the full `data_kind` taxonomy: `template`, `runtime-generated`, `user-data`, plus regular scripts.
- `archive/{bucket}/*.md` is user-data — does not travel.
- `archive/index.json` is runtime-generated — fresh on install.
- `atlas/sources.json` is a template — travels with placeholders.
- No `atlas/llm-config.json` — apps inherit the bot's LLM stack via `recursive_llm.transport` (see §8).

---

## 10. Validation rules

A manifest is **forge-buildable** if:

1. All required fields present (§5.1–§5.9, §5.20, plus one of §5.21).
2. `description` is Spec-shape (no PII references; passes the Sanitizer's spot check).
3. `scope_includes` and `scope_excludes` are both non-empty.
4. `success_criteria.observable_outcomes` has at least 2 entries, each one verifiable.
5. `build_spec` is non-empty, contains a File layout section, and is under 800 lines.
6. If app is interactive (has surfaces beyond cron): `audience_scoping` is populated.
7. If app handles user-identifying data: `privacy` is populated.
8. If app is event-driven: `event_triggers` and `bot_guidance` are populated.
9. If app uses LLM (classifier, synthesizer, chat backend): `recursive_llm` is populated with `transport` set; no `api_key_source` or per-app credential pointer present.
10. If app has shared dependencies: `app_dependencies` is populated AND the dependency's owner declares `shared_with` reciprocally.
11. All `files[]` entries have `layer` and (if `layer == "data"`) `data_kind` populated.
12. Skill ids in `skills[]` are known to the resolver.

A manifest is **exportable** if forge-buildable AND:

13. `description`, `identity.*`, `success_criteria.*`, `constraints.*` are all Spec-shape (no PII).
14. All `files[]` with `layer == "data"` have `data_kind` other than `user-data` (or are excluded by the Sanitizer).
15. `privacy.shareable_in_lessons` is explicitly set (true or false, not absent).
16. `audience_scoping.config_path` references a template file (data_kind: template), not user-data.

A manifest is **importable** if exportable AND:

17. `spec_id` is set and stable.
18. `source.pod_id` and `source.shared_at` are stamped (added by the Export process).
19. The recipient pod can resolve all `skills[]` and `app_dependencies`.

---

## 11. Versioning and sync

This guide is pinned to a specific `BUILTIN_BUILDER_PROMPT` version (currently commit `b51d7dc9`). When the builder prompt changes:

1. Update this guide's `Calibrated to:` header at the top.
2. Update §1 ("forge enforces these") to reflect the new prompt content.
3. If new enforcement is added, remove duplicate guidance from §5 fields.
4. If enforcement is removed, add it back to the §5 fields that now need to specify it.

The forge_engine.py builder prompt should reference this guide explicitly so future authors of the prompt know to keep them in sync:

```python
# When updating this prompt, also update docs/manifest-authoring-guide.md
# (the authoring contract is calibrated against this prompt's enforcement set).
```

---

## 12. Future work

- **§6 file taxonomy:** add a `sensitive` discriminator to `data_kind` (e.g., `template-sensitive`, `seed-sensitive`) that signals to the Sanitizer to apply stricter generalization.
- **Validation as machine-readable rules:** §10 should be expressible as JSON Schema + custom validators, runnable as `evolve-admin manifest validate <path>`. v1 work.
- **Mass-update path:** when the guide changes, a Reflect-style pass over all gallery Specs to surface ones that no longer satisfy the validation rules.
- **Worked example: shared utility manifest.** A pattern for when an app's *only* purpose is to provide shared code to other apps (e.g., `atlas_lib` as its own Spec, with no user-facing functionality). Decide if this pattern is supported.

---

## Related

- [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — architectural backbone (3-artifact split)
- [docs/spec-export-import-forge-2026-05-26.md](spec-export-import-forge-2026-05-26.md) — the three processes that produce and transform manifests
- [docs/manifest-spec.md](manifest-spec.md) — formal schema reference
- [docs/spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md) — the shipped two-phase forge surface
- [packages/admin/evolve_admin/applications/forge_engine.py](../packages/admin/evolve_admin/applications/forge_engine.py) — the live forge code
- `docs/atlas-app-manifests/GAPS.md` (worktree-deleted reference) — the original Atlas pressure-test findings
