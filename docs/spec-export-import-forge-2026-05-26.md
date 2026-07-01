# Export / Import / Forge — Process Spec

**Status:** draft (2026-05-26)
**Supersedes:** parts of [spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) §8.1–§8.2 + §9; extends rather than replaces.
**Companion docs:**
- [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) — the contract a manifest must satisfy
- [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — architectural backbone (3-artifact split)
- [docs/spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md) — the shipped two-phase Forge
- [packages/admin/evolve_admin/applications/forge_engine.py](../packages/admin/evolve_admin/applications/forge_engine.py) — current Forge impl

---

## 1. What this spec covers

Three connected processes that produce and transform application manifests:

| Process | Direction | Input | Output | Status |
|---|---|---|---|---|
| **Forge** | Spec → Instance | App Spec | Running Instance (manifest + realized files) | Shipped, see [spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md). This spec adds Atlas-shape tuning. |
| **Reflect + Sanitize** (Export) | Instance → exportable Spec | Running Instance (manifest + workspace files + bot context) | Sanitized App Spec | New. Extends v7 §8.1 with the Sanitizer. |
| **Adopt** (Import) | Foreign Spec → local Instance | App Spec from another bot or pod | New Instance (via Forge Build) | Sketched in v7 §8.2; this spec fills in detail. |

The unifying principle: **apps travel as manifests, not buildings.** The Spec is the blueprint; the Instance is the building. Sharing means deriving the Spec from a building and constructing a new building elsewhere.

This spec assumes the [manifest-authoring-guide](manifest-authoring-guide.md) is the contract all manifests in motion must satisfy. The guide is the rubric; this spec is the process that ensures inputs and outputs match the rubric.

---

## 2. Architecture — the Instance ↔ Spec ↔ Instance flow

```
                  ┌────────────────────────────────────────┐
                  │                                        │
                  │   Pod A                                │
                  │                                        │
                  │   ┌──────────────────┐                 │
                  │   │  Instance Manifest │  ← lives on bot, personalized
                  │   │  + workspace files │
                  │   │  + bot context    │
                  │   └────────┬─────────┘                 │
                  │            │                           │
                  │            │ Reflect + Sanitize (Export)│
                  │            ▼                           │
                  │   ┌──────────────────┐                 │
                  │   │  App Spec        │  ← canonical, sharable
                  │   │  (generalized)   │                  │
                  │   └────────┬─────────┘                 │
                  └────────────┼───────────────────────────┘
                               │
                               │  carrier: gallery file / direct transmission
                               ▼
                  ┌────────────┼───────────────────────────┐
                  │            │                            │
                  │   Pod B    ▼                            │
                  │   ┌──────────────────┐                  │
                  │   │  App Spec        │  ← arrived from pod A
                  │   └────────┬─────────┘                  │
                  │            │                            │
                  │            │ Adopt (Import) → Forge Build│
                  │            ▼                            │
                  │   ┌──────────────────┐                  │
                  │   │  Instance Manifest │ ← new, personalized to pod B's user
                  │   │  + workspace files │
                  │   └──────────────────┘                  │
                  │                                         │
                  └─────────────────────────────────────────┘
```

The same flow works **within a pod** (bot A → bot B) and **cross-pod** (pod A → pod B). The Sanitizer is the same either way; the only difference is the destination path in the gallery (see v7 §9.1–§9.2).

---

## 3. The Forge process (brief)

The Forge takes a Spec and produces an Instance. Implementation in `forge_engine.py`. Current phases:

```
Build → Critique × 2 → Test → Gate → Apply
```

v7 §8.1–§8.2 adds **Reflect** (post-install hygiene) and **Adopt** (Lesson-driven update) as additional phases that consume the same machinery.

This spec adds Atlas-shape tuning recommendations for the existing phases:

### 3.1 Builder prompt additions

The `BUILTIN_BUILDER_PROMPT` in `forge_engine.py` should gain two sections:

**Multi-app context.** Appended to the prompt when `app_dependencies` or `shared_modules` is non-empty:

> If this app is part of a multi-app suite (shared_modules, cross-app data):
> - Do NOT assume shared modules exist — create them if the spec mentions them, or import them defensively if owned by another app.
> - For shared state (archives, indexes, logs), use atomic writes (temp-file + os.replace).
> - If `shared_modules` lists exports the dependency owns, import them by exact name; do not redefine.

**Recursive LLM concerns.** Appended when `recursive_llm` is non-empty:

> This app calls Claude or another LLM internally. Required:
> - Exponential backoff with max 3 retries for transient errors (5xx, timeouts, 429).
> - A graceful fallback when the LLM is unreachable (declared in the manifest's `recursive_llm.fallback_required`).
> - Error logging with context (timestamp, error code, what was being done).
> - No infinite retry loops; fail gracefully after exhausting retries.
> - Per-call cost telemetry recorded for the audit roll-up.

### 3.2 Critic prompt additions

The Critic should gain a sixth lens for multi-app suites:

> 6. INTEGRATION: For apps that depend on shared modules or sibling apps, are the imports correct? Are the expected exports present? Are atomic writes used for shared state?

### 3.3 Dependency-integration check (new non-blocking phase)

Between Test and Gate, add an integration check:

```python
def _check_dependency_integration(manifest, bot_id, shared_dir):
    """Verify that imported shared modules exist and have expected signatures.
    Non-blocking: failures are logged to the job, surfaced to the operator,
    but do not prevent approval. This is signal, not a gate."""
```

The check:
- Parses imports from generated files referencing `shared_modules` declarations.
- Tries to import them from the bot's workspace.
- Verifies the declared `expected_exports` are present.
- Returns a list of issues that the operator sees alongside the test output.

---

## 4. Reflect + Sanitize — the Export process

The new substance. Takes a running Instance, produces an exportable Spec.

### 4.1 Inputs

1. **Instance manifest** (the bot's local app manifest, possibly with Instance-shape prose).
2. **All workspace files declared in the manifest's `files[]`** — the Sanitizer needs read access to make classification + sanitization decisions.
3. **Bot context:**
   - SOUL.md (voice / persona)
   - AGENTS.md (current guidance, including any sections this app added)
   - The bot's user-profile (per-bot inferred profile — the database of personal facts the Sanitizer scrubs against)
4. **Sibling app manifests** that this app references via `app_dependencies` — so the Sanitizer can identify which references are to shared apps (those generalize) vs. specific personal apps (those drop).

### 4.2 Outputs

1. **Sanitized App Spec** (JSON, conforms to authoring-guide validation rules).
2. **Sanitization report** — what was changed and why, with confidence levels. Operator-reviewable.
3. **Excluded files manifest** — files the Sanitizer recommended NOT to ship, with reasons.

### 4.3 Pipeline stages

```
┌─────────────────────────────────────────────────────────────────┐
│  Stage 1: Refresh                                               │
│    Run v7 Reflect to ensure Instance manifest matches workspace │
│    reality (marker repair, orphan detection, drift detection)   │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 2: Classify files                                        │
│    For each entry in files[], apply the §6-decision tree from   │
│    the authoring guide.                                         │
│    Output: traveling-set (what ships) + excluded-set (what       │
│    doesn't), each with reasons.                                 │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 3: Generalize prose                                      │
│    Sanitizer LLM rewrites Instance-shape prose to Spec-shape    │
│    per authoring-guide §4.                                      │
│    Affects: description, objective, identity.purpose,           │
│    identity.scope_includes, identity.scope_excludes,            │
│    success_criteria.* (any free-form text), constraints.*       │
│    (any free-form text), build_spec.                            │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 4: Scrub traveling data files                            │
│    For each file in traveling-set (data_kind: template or seed):│
│    pass through the Sanitizer's file pass.                      │
│    Template files: confirm placeholders are empty (operator.json│
│      should have user_id=0, not a real id).                     │
│    Seed files: identify any PII; redact or generalize.           │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 5: Validate                                              │
│    Run §10 validation rules from the authoring guide against    │
│    the candidate Spec. Surface any failures.                    │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 6: Operator review                                       │
│    Display the diff (Instance → Spec) + the sanitization        │
│    report + the excluded files manifest.                        │
│    Operator approves, edits, or rejects.                        │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 7: Stamp + write                                         │
│    Add source.pod_id, source.bot_id, source.shared_at.          │
│    Write to {shared_dir}/gallery/<destination>/<source>/        │
│    <spec_id>/<spec_version>.json per v7 §9.1/§9.2.              │
└─────────────────────────────────────────────────────────────────┘
```

### 4.4 The Sanitizer — prose generalization

LLM-driven. Runs on the bot, using the bot's own LLM credentials.

**Inputs to a single Sanitizer call:**
- The Instance prose to generalize (one field at a time, or batched).
- The bot's user-profile (so the Sanitizer knows what names / facts / specifics belong to the user vs. are generic).
- The list of sibling app manifests on this bot (so the Sanitizer can identify references to specific apps vs. to generic patterns).

**Prompt shape (informal):**

> You are sanitizing the description of an app on a personal AI bot. The app's local form references the user by name and other personal context. Your job is to rewrite it so it describes the *shape* of the app for someone else to install — preserving the structure and purpose, removing all personal details.
>
> The user of this bot is: {user_profile_summary} (e.g., "Pod-Admin, who tracks weight and uses a Zepbound prescription, lives in Boston, is a software developer at Anthropic").
>
> Sibling apps on this bot: {sibling_apps_summary}
>
> Instance prose: ```{prose}```
>
> Rewrite as Spec-shape per the rules:
> 1. People → roles ({user_name}, "the user", "the operator")
> 2. Specifics → categories (medication name → "prescribed medication" or drop)
> 3. Idiosyncrasies → configurations ("at 8pm" → "at a configurable deadline (default 20:00)")
> 4. Stories → patterns (drop history)
> 5. Memory references → drop
> 6. Adjacent-app references → generalize ("Pod-Admin's morning briefing" → "a morning-briefing-shaped app")
>
> Return JSON: {"sanitized": "<text>", "changes": [{"original": "X", "replacement": "Y", "category": "people|specifics|idiosyncrasy|story|memory|adjacent-app", "confidence": 0.0-1.0}]}.
>
> Low confidence on any change → flag it for operator review.

**Default posture:** when in doubt, generalize / redact. Conservative bias.

### 4.5 The Sanitizer — file scrubbing

For files in the traveling-set:

**Templates** (`data_kind: "template"`):

- Validate that placeholder fields are empty / zero / null. Atlas's `operator.json` template should have `operator_telegram_user_id: 0`, not a real user_id.
- If a template field is non-empty in the Instance, replace it with the schema default (or zero/empty/null).

**Seeds** (`data_kind: "seed"`):

- Pass through the Sanitizer's prose generalization (same as Stage 3) for any human-readable content.
- For structured data (JSON, YAML, lists), the Sanitizer scans each value for PII patterns:
  - Email addresses, phone numbers (literal regex)
  - User name from the user-profile
  - Location strings from the user-profile
  - Medication names, health conditions, financial amounts (LLM judgment)
- Flagged values get replaced with placeholders or removed.

**Edge cases:**

- A seed file with **mostly-generic content but a few user-specific entries** (e.g., a default RSS list where the user added one personal blog): the Sanitizer drops the personal entry and surfaces it in the sanitization report ("removed RSS entry: <https://pod-admin-personal-blog.com> — appeared to reference the user's personal site").

### 4.6 Operator review (Stage 6) — what's shown

The operator sees four panels:

1. **Diff: Instance → Spec.** Side-by-side prose changes with the Sanitizer's category labels and confidence scores.
2. **File classification table.** For each file in the manifest's `files[]`, the class assigned (build artifact / template / seed / user-data / runtime-generated) and whether it travels.
3. **File sanitization report.** For each traveling data file, what was changed and why.
4. **Excluded files list.** Files NOT shipping, with reasons (mostly user-data + runtime-generated).

The operator can:

- Approve as-is.
- Edit any prose change (with the Sanitizer's reasoning visible).
- Reclassify a file (e.g., "no, that seed file actually has data I don't want to share — exclude it").
- Reject the export entirely.

### 4.7 LLM model + cost

- Sanitizer model: same as the bot's primary research model (e.g., claude-haiku-4-5 by default; Sonnet for more sensitive bots like personal-assistants).
- Cost: ~$0.05–$0.20 per export for a medium-complexity app.
- Per [feedback_per_bot_inference](memory/feedback_per_bot_inference.md): always runs on the bot, using the bot's credentials. Never centralized.

### 4.8 Failure modes

| Failure | Recovery |
|---|---|
| Instance manifest fails Reflect refresh | Surface to operator; export blocked until manifest is valid. |
| Sanitizer LLM unreachable | Retry with backoff; if all retries fail, abort export with clear error. Do NOT fall back to a "non-sanitized" export. |
| Sanitizer returns malformed JSON | Retry once; on second failure, surface to operator for manual review. |
| Validation against authoring-guide fails | Surface specific rule violations to operator with suggested fixes. Block write to gallery. |
| Operator rejects in Stage 6 | Discard all artifacts; no gallery write; no state change. |
| Race: Instance changes during export | Abort. Re-run from Stage 1. Export is read-only on the Instance; safe to abort at any stage. |

---

## 5. Adopt — the Import process

Receiving a foreign Spec and turning it into a local Instance. Extends v7 §8.2.

### 5.1 Inputs

1. **The foreign Spec** (arrived via gallery, file drop, or in-conversation transmission).
2. **The recipient bot's context** (skills available, existing apps, user-profile, SOUL.md).

### 5.2 Pipeline stages

```
┌─────────────────────────────────────────────────────────────────┐
│  Stage 1: Validate                                              │
│    Authoring guide §10 importable rules: spec_id stable,        │
│    source stamped, dependencies resolvable, skills available.   │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 2: Conflict check                                        │
│    Does the recipient already have an Instance from this        │
│    spec_id? From a different source pod with the same spec_id?  │
│    Different version? Surface to operator.                      │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 3: Dependency resolution                                 │
│    For each app_dependency: is the dependent app installed?     │
│    If not, recursively prompt the operator to install first.    │
│    For each skill: is it available + configured?                │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 4: Personalize prose                                     │
│    Inverse of the Sanitizer. The Spec's generalized prose       │
│    gets light personalization for the new user/bot context.     │
│    E.g., {user_name} → the recipient's name; "the configured    │
│    messaging channel" → the bot's actual primary channel name.  │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 5: Operator review (the design gate)                     │
│    Show the Spec + the personalization preview + the planned    │
│    install. Operator approves, customizes, or rejects.          │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 6: Forge Build                                           │
│    Standard Forge pipeline (Build → Critique × 2 → Test →       │
│    Gate → Apply). Per v7 §9.2, critique/test runs regardless    │
│    of source.                                                    │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Stage 7: Provenance binding + Instance stamping                │
│    Create Provenance record: source Spec id + version,          │
│    source pod_id, install timestamp, recipient bot_id.           │
│    Instance gets fresh files, fresh schedules, fresh learned     │
│    config — nothing from the source travels.                     │
└─────────────────────────────────────────────────────────────────┘
```

### 5.3 Conflict resolution (Stage 2)

| Conflict | Behavior |
|---|---|
| Same `spec_id` from same source, same version | Skip — already installed. |
| Same `spec_id` from same source, newer version | Offer upgrade (Adopt phase per v7 §8.2). |
| Same `spec_id` from same source, older version | Refuse — no downgrade. |
| Same `spec_id` from **different** source pod | Per v7 §9.2, both Specs coexist in the gallery at distinct paths. Operator picks which to install. |
| Same `spec_id` but **incompatible** schema versions | Refuse with clear error; require operator to resolve schema first. |

### 5.4 Personalization (Stage 4)

LLM-driven, inverse of the Sanitizer. Light touch — the Spec is supposed to be generic enough to install verbatim. Personalization replaces:

- `{user_name}` placeholder → recipient's name from user-profile.
- `{telegram_chat_id}` placeholder → recipient's actual chat id (collected from operator).
- `{location}` placeholder → recipient's location.
- Other declared `template_vars`.

If a generalized phrase is awkward in the recipient's context, the LLM may slightly refine ("at a configurable evening deadline (default 20:00)" → "at 20:00 by default" if the operator chose to accept defaults). These changes are tracked in the Provenance record so divergence between Spec and Instance is visible later.

### 5.5 Failure modes

| Failure | Recovery |
|---|---|
| Spec fails authoring-guide validation | Refuse import; surface specific rule violations. |
| Required skill not available | Pause import; prompt operator to install/configure the skill first. |
| Required app dependency not installed | Pause import; offer to install dependencies first. |
| Operator rejects at Stage 5 | Discard; no install. |
| Forge Build fails | Standard Forge failure mode — surface diff/errors, allow retry or rollback. |
| Mid-install crash | Per v7 §8.3 write-order contract — atomic stages allow clean rollback. |

---

## 6. The trust boundary flow — how the privacy block guides the Sanitizer

The `privacy` block declared in the manifest (authoring guide §5.13) does triple duty:

1. **At runtime**, it tells the app's audit/observation features what categories of data are collected and at what retention.
2. **On export**, it tells the Sanitizer what categories to look for and how aggressively to redact.
3. **On import**, it tells the recipient operator what data this app will collect from them.

The Sanitizer uses the `data_collected` field as its **scrubbing checklist**. For each category declared:

| `data_collected.kind` | Sanitizer behavior |
|---|---|
| "user message content" | Drop entirely from any traveling file (would be `user-data`). |
| "user telegram_id" (or any identifier) | If found raw anywhere, refuse to ship. If hashed, confirm the salt is not in the export. |
| "user health conditions" | Aggressive scrub: any disease/medication/symptom name → drop. |
| "user location" | Generalize: city → drop, country → drop, "the user's location" → placeholder. |
| "user calendar events" | Drop entirely. |

**The audit relationship:** the manifest declares `privacy.data_collected`; the audit verifies the running app does only what it declared. The Sanitizer trusts the manifest — but the audit ensures the manifest is honest. Together they form the machine-checkable trust boundary that [safety-as-flagship-feature](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_safety_as_flagship_feature.md) rests on.

---

## 7. Worked examples

### 7.1 Atlas exported to a beta tester's pod

**Source:** Pod-Admin's pod, after Atlas has been running for two weeks with 50 captured URLs from the enthusiast group.

**Reflect refresh** picks up minor drift (one new file the bot added for stats).

**Classify files:**

| File | Class | Travels? |
|---|---|---|
| `scripts/atlas_digest.py` | build artifact (`layer: script`) | No |
| `scripts/atlas_lib/classifier.py` | build artifact | No |
| `atlas/sources.json` | template | Yes (with `{rss_urls}` placeholders kept as templates) |
| `atlas/operator.json` | template | Yes (with empty `operator_telegram_user_id`) |
| `atlas/llm-config.json` | template | Yes (empty api_key) |
| `atlas/research-config.json` | seed | Yes (default rate limits + refusal templates — no PII) |
| `atlas/.capture-salt` | runtime-generated | No (recipient generates fresh) |
| `archive/index.json` | runtime-generated | No |
| `archive/{bucket}/*.md` | user-data (the actual captured articles) | No |
| `digest/{YYYY-MM-DD}.md` | user-data (Pod-Admin's actual digests) | No |
| `atlas/capture-log.jsonl` | user-data (hashed but still linkable to use patterns) | No |
| `atlas/optout.json` | user-data | No |

**Generalize prose:**

| Field | Instance | Spec |
|---|---|---|
| `description` | "Daily research crawler for Pod-Admin's OpenClaw-enthusiasts Telegram group. Posts digests at 7am via the configured group; serves market research for Evolve as a side effect." | "Daily research crawler for a configured Telegram community. Posts a 5-bucket digest at a configured time via the group chat; also serves as a long-term archive for member-shared links." |
| `identity.user` | "Pod-Admin (operator) + members of the @evolve-enthusiasts Telegram group" | "the operator + members of the configured Telegram group" |
| `identity.scope_includes` (a bullet) | "Posts digest to Pod-Admin's enthusiast group every morning at 07:00 PT" | "Posts a digest to the configured group at a configurable time (default 07:00 local)" |

**Output:** an Atlas Spec at `{shared_dir}/gallery/local/p-atlas-daily-digest/2026.06.10-1.0.json` (within-pod share) OR a downloadable file (cross-pod share).

A beta tester imports this Spec, the Adopt flow runs, they provide their own Telegram chat_id + operator_user_id + Anthropic API key, the Forge rebuilds atlas on their pod. They get a structurally identical bot with zero of Pod-Admin's data.

### 7.2 Personal health app — the Pod-Admin-specific example

**Source:** Pod-Admin's personal-assistant bot, after a year of use.

**Generalize prose (the core insight):**

| Field | Instance | Spec |
|---|---|---|
| `description` | "Help Pod-Admin manage his weight by tracking protein, fiber, and sugar intake. Knows about his Zepbound dose schedule and integrates with his morning briefing." | "Personal health app that tracks macronutrient intake (protein, fiber, sugar) via short messages. Optionally surfaces summary data to a morning-briefing-shaped app." |
| `identity.purpose` | "Pod-Admin is trying to lose 20lbs while on Zepbound. He needs an effortless way to log macros without spreadsheets, and the bot already knows his routine, his target weights, and his doctor's recommendations." | "The user is tracking macronutrient balance for a personal health goal. The app provides an effortless logging interface and same-day shortfall awareness without requiring spreadsheets or external tools." |
| `identity.scope_includes` | "Logs Pod-Admin's daily macros via Telegram messages he sends after each meal. Reminds him to log dinner if not seen by 8pm. Connects to his morning briefing to include yesterday's totals. Aware of his Zepbound dose schedule (Tuesdays) and notes that protein intake matters more on dose-week mornings." | "Logs user macros via the configured messaging channel after meals. Sends a reminder if no log entry is received by a configurable evening deadline (default 20:00). Optionally exposes yesterday's totals to a morning-briefing-shaped app. (Domain-specific awareness — e.g., medication-aware logging — is an optional refinement the recipient can add.)" |

**Critical files:**

- The bot's stored memories about Pod-Admin's medical history → never exposed to this app, never travel.
- The medication schedule file (if local) → flagged by Sanitizer as `data_kind: user-data` → does not travel.
- The "user prefers grams over ounces" preference → marked as `data_kind: template` with a default → travels as part of the seed config.

The recipient bot installs a generic personal-health-tracker. Their bot personalizes it over time, knows nothing of Pod-Admin.

---

## 8. Test contract

What tests validate this spec's correctness:

| Test class | What it validates |
|---|---|
| Authoring-guide validators | Manifests produced by Forge / Sanitizer / Adopt satisfy the validation rules in `manifest-authoring-guide.md` §10. |
| Sanitizer regression tests | Known Instance prose → expected Spec prose, with the Sanitizer's category labels matching expectations. Sourced from the worked examples here. |
| File classification tests | Files with known shape get the correct `data_kind` assigned. |
| End-to-end Atlas export | Atlas on a test bot, exported, re-imported on a fresh bot, runs successfully. |
| Privacy block enforcement | An app declares `data_collected: ["user telegram_id"]`. The Sanitizer scrubs the actual telegram_id from any traveling file. |
| Conflict resolution | Two Specs with same `spec_id` from different sources coexist correctly in the recipient gallery. |
| Dependency resolution | Importing an app whose dependencies aren't installed correctly pauses the import. |

The integration tests are end-to-end and expensive (real LLM calls); the unit tests cover individual pipeline stages.

---

## 9. Implementation plan

Priority order, from blocking to nice-to-have:

| # | Work | Blocks | LOC est |
|---|---|---|---|
| 1 | Authoring-guide validator (§10 rules as code) | Sanitizer + Adopt validation stages | ~200 |
| 2 | File-classification helper (decision tree from §6) | Sanitizer Stage 2 + Adopt validation | ~150 |
| 3 | Sanitizer LLM client + prompt + parser | The Export process end-to-end | ~400 |
| 4 | Reflect+Sanitize CLI: `evolve-admin manifest export --app <id> --bot <id>` | Operator usability | ~150 |
| 5 | Adopt CLI: `evolve-admin manifest import <spec-file> --bot <id>` | Operator usability | ~200 |
| 6 | Operator review UI (Stage 6 of Export, Stage 5 of Adopt) | First end-to-end ship | ~UI work, hard to estimate |
| 7 | Builder prompt additions (§3.1 / §3.2) | Atlas-shape Forge builds | ~50 (prompts) |
| 8 | Dependency-integration check (§3.3) | Atlas-shape Forge builds | ~100 |
| 9 | End-to-end Atlas export/import test | Confidence in the system | ~500 (test harness + assertions) |
| 10 | Authoring-guide reference machine-checkable form | Wizard / scanner integration | ~300 |

Total ~2050 LOC + UI work. For comparison: Forge itself is ~5000 LOC. This is incremental, not foundational.

**Sequencing recommendation:** items 7 + 8 first (unblocks Atlas load-test), items 1–5 next (the Export/Adopt MVP), then 6 + 9.

---

## 10. Open questions

1. **Sanitizer model choice.** Haiku is the cost-efficient default but may miss nuanced PII (e.g., a user's specific medical condition mentioned by description rather than name). Should the Sanitizer default to Sonnet for personal-assistant-class apps? Decision deferred to first real export.

2. **Sanitizer training data / few-shot examples.** Do we ship a curated set of Instance→Spec examples that the Sanitizer references? Probably yes; defer concrete authoring to v1.

3. **What about Lessons sharing?** v7 §9.3 covers it; this spec doesn't extend. The Sanitizer flow likely applies to Lessons too — same prose generalization rules, same data file scrubbing. Address in v1.

4. **Multi-app suite export.** Atlas is four apps. Do they export as one atomic bundle, or four independent Specs the recipient adopts separately? Bundle is cleaner UX; separate is more flexible. Probably support both; default to bundle.

5. **Trust calibration.** Cross-pod imports get "external source — review carefully" per v7 §9.2. Within-pod imports between bots owned by the same operator could skip Stage 5 review by default. Configure via pod-level policy.

6. **Operator-profile gap.** The Sanitizer leans on the per-bot user-profile to know who to scrub against. What if the profile is sparse or wrong? Bias toward over-scrubbing (false positives — operator sees them in review) rather than under-scrubbing (false negatives — PII leaks).

---

## 11. Related

- [docs/manifest-authoring-guide.md](manifest-authoring-guide.md) — the contract this spec implements
- [docs/spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — architectural backbone
- [docs/spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md) — current Forge surface
- [packages/admin/evolve_admin/applications/forge_engine.py](../packages/admin/evolve_admin/applications/forge_engine.py) — Forge impl
- [packages/admin/evolve_admin/applications/scanner.py](../packages/admin/evolve_admin/applications/scanner.py) — inverse direction (files → manifest); the Sanitizer should learn from its PII-detection patterns where applicable
