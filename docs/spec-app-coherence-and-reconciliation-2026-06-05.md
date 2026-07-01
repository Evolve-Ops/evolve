# App Coherence + Reconciliation (2026-06-05)

Status: **proposed**. Extends [spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md) (3-tier audit framework) and [spec-audit-extensions-2026-05-17.md](spec-audit-extensions-2026-05-17.md) (scheduled-action extraction + substrate audit). Concretizes three problems the existing audit framework partially overlaps but never names cleanly: **reconciliation** (manifest vs disk disagreement), **provenance** (whether a manifest field is contract or just observation), and **coherence** (manifest claims a behavior with no mechanism to produce it).

**The seam this closes.** Today the scanner builds truth from disk and refuses to touch existing manifests; the auditor checks disk against the manifest and emits Signals; nothing writes back to the manifest; nothing checks whether the manifest's claims even hang together internally; and crucially, **nothing distinguishes a scanner-discovered manifest (a snapshot of reality) from an operator-authored manifest (a contract).** Treating both the same way creates two opposite failures: forcing operators to "approve" every observational change the scanner found (double work), and silently overwriting operator-authored contracts when the scanner re-runs (loss of intent). This spec separates the two.

---

## 1. Goals and non-goals

**Goals.**

1. Make the manifest the source of truth for what an app *should* do, while distinguishing fields the operator authored (contract) from fields the scanner observed (snapshot).
2. When a scanner re-run finds new files / new triggers / new behavior on an app the operator has not put on contract, silently update the manifest — no chip, no Proposal, no approval. The manifest is just tracking reality.
3. When reality drifts from a field the operator authored, surface the drift and let the operator decide: repair the implementation, update the contract, or accept the drift.
4. Type files by purpose (`code`, `config`, `data`, `log`, etc.) so reconciliation rules can fire by purpose rather than treating every disagreement as equally severe.
5. Verify that the manifest's *claims hang together* — declared recurring behaviors must have declared mechanisms; declared inputs must be enumerated; declared outputs must have a code path. This check runs regardless of provenance.
6. Stay cheap. Almost all of this runs as pure-Python graph walks on the manifest. LLM is tiered (cheap-handles-routine, expensive-handles-complex), reserved for the work that needs it.
7. Never delete manifests. Never silently rewrite authored fields. Operator approves every expansion or removal of contract.
8. **Recognize that most apps stay observational forever, and that's a complete posture.** Promotion is an opt-in tool the admin uses when they specifically want something on contract; it is not a workflow milestone. The Apps page must not nag operators to promote.
9. **Make findings visible to the bot itself**, so the bot can mention issues conversationally, alert the user when something is broken, and accept "fix it" instructions through chat. The bot's conversation context is the primary surface for bot users; the admin UI is the diagnostic surface for admins.

**Non-goals.**

1. Replacing Tier 1/2/3 audits. This spec composes on top of the existing tiers; it does not redefine them.
2. Auto-restructuring legacy apps to fit canonical layouts. A separate generator may *propose* layout changes; this spec only proposes layer classification, not file movement.
3. Cross-app coherence (e.g., "app A claims to write data app B reads, but B's reads don't match A's writes"). Single-app coherence first; cross-app is a follow-up.
4. Per-entry provenance within list fields (each `files[*]` entry tracking its own author). v1 uses per-top-level-field granularity; per-entry is a v2 evolution noted in §15.
5. Making the existing scanner's discovery LLM cheaper or smarter. Out of scope.

---

## 2. The three concerns, cleanly separated

| Concern | Question | Detection | Surface |
|---|---|---|---|
| **Reconciliation** | Does the manifest match what's on disk? | Pure Python (file walk + sha + crontab/launchctl) | `reconciliation` block on manifest + chip on Apps page (authored fields only) |
| **Provenance** | Was this field authored or observed? | Stamped at write time | `provenance` block on manifest — gates whether reconciliation surfaces |
| **Coherence** | Does the manifest describe something that could work? | Pure Python (graph walk) for Pass A/B; static analysis for Pass C1; LLM for C2/C3 | `coherence` block on manifest + chip on Apps page (provenance-independent) |
| **Audit (existing)** | Does the code do what the manifest claims? | LLM (Tier 3) | Proposals + trail (existing) |

Reconciliation and coherence are sibling concepts. Provenance is the modulator that decides which reconciliation events deserve operator attention. Audit (existing) continues to operate orthogonally.

---

## 3. File-layer taxonomy (schema v20)

**Note on version.** Existing manifests today are at `schema_version: 19` (e.g., personal-bot's manifests). This spec moves to v20 — purely additive over v19, preserving all existing fields. The migration in §13.1 maps the legacy `layer` strings to the new enum.

### 3.1 The `layer` enum

Required field on every `files[*]` entry. The existing v19 schema uses an ad-hoc `layer` field (values seen in practice: `"script"`, `"state"`, sometimes empty); v20 fixes the values and adds semantic meaning.

```
code          — scripts the app runs (.py, .sh, .js, .ts, .rb). Legacy "script" maps here.
config        — declared structured config the code reads (.json, .yaml, .toml)
contract      — the manifest itself, schemas, interface contracts
behavior_doc  — AGENTS.md sections, HEARTBEAT.md anchors, SOUL.md, POD_CONDUCT.md
reference     — README, runbooks, design docs the code does not load
content       — articles, prompts, templates the app reads as payload (legacy mis-classified as "state")
data          — journals, observations, generated outputs
log           — append-only operational logs
state         — locks, checkpoints, indexes, caches (the legitimate state layer)
```

**Migration of legacy `layer` values.** PR 3 re-classifies every existing `files[*]` entry rather than trusting the v19 stamper. Reason: spot-check against personal-bot's manifests showed `marie/*.md` content files stamped `"state"` (should be `content`) and `HEARTBEAT.md` stamped `"state"` (should be `behavior_doc`). Trusting legacy layers would propagate the classification errors into reconciliation severity. PR 3's classifier runs fresh on every file regardless of current layer value; the legacy value is logged in the trail but not used.

### 3.2 Reconciliation severity by layer (applies only to authored fields — see §4)

Severity is **purpose-driven**, not uniform. Tier 2's existing "missing file → critical" rule becomes:

| Layer | Extra file | Missing file | Sha drift |
|---|---|---|---|
| `code` | Proposal (approval-required) | **Critical** | Major (LLM verify) |
| `config` | Proposal | **Critical** | **Critical** |
| `contract` | **Critical** (should never appear unannounced) | **Critical** | **Critical** |
| `behavior_doc` | Proposal | **Critical** | Major (anchor drift) |
| `reference` | Auto-merge | Warning | Ignored |
| `content` | Auto-merge if in declared `volatile_paths[]`; Proposal otherwise | Major if individually claimed; ignored if in volatile path | Ignored |
| `data` | Auto-merge if in declared `volatile_paths[]`; Proposal otherwise | Info | Ignored |
| `log` | Auto-merge | Info | Ignored |
| `state` | Auto-merge | Ignored | Ignored |

**Critical clarification:** this table only applies to fields whose provenance is `user_authored`, `forge_built`, `bot_authored`, or `confirmed`. For `observational` fields, every disagreement results in silent update — no chip, no Signal, no severity. See §4.

### 3.3 `volatile_paths[]`

Apps with content/data/log/state that comes and goes by design declare directories at the manifest level rather than enumerating instances:

```jsonc
"volatile_paths": [
  {"glob": "data/journal/*.md",  "layer": "data",    "expected_growth": "1-5/day",  "max_age_days": null},
  {"glob": "data/articles/*.md", "layer": "content", "expected_growth": "bursty",   "max_age_days": 30},
  {"glob": "log/*.log",          "layer": "log",     "rotation": "weekly",          "max_count": 12},
  {"glob": "state/.cache/**",    "layer": "state",   "rotation": "any"}
]
```

`files[]` then carries only the infrastructure of the app. Files under a `volatile_paths[*].glob` are not enumerated; Tier 2 skips them; the reconciliation rules above apply via the glob's declared layer.

`expected_growth` is verified as a coherence signal (§6.2): a `data` glob declared `1-5/day` with zero new files in 14 days is the quiet-failure signal independent of any specific file.

### 3.4 Scheduled-actions shape extensions

**Foundational vocabulary — the scheduling mechanisms are NOT equivalent.** Before extending `scheduled_actions[*]`, the four trigger mechanisms need to be clearly distinguished. They differ in *what they are*, *what they cost*, and *what they are appropriate for*:

| Mechanism | What it actually IS | Cost per fire | Appropriate for |
|---|---|---|---|
| **`heartbeat`** | A scheduled **LLM session** that loads `HEARTBEAT.md` as instructions and executes them | LLM tokens (~$0.01–$0.10 per fire depending on model and instruction depth) | LLM-judgment work, composite checks needing reasoning, context-aware decisions |
| **`openclaw_cron`** | OpenClaw-managed scheduled invocation — can be a pure command **or** an agent invocation | $0 for pure commands; LLM tokens if it spawns an agent | Work that needs OpenClaw delivery integration (announce to a channel, route to user) |
| **`launchd`** | OS-level scheduled command execution via macOS launchd | $0 (just process execution) | Deterministic Python / shell work; daemon-style background jobs |
| **`crontab`** | OS-level scheduled command execution via user crontab | $0 | Same as launchd; older mechanism; barely used by Evolve in practice |

**Heartbeat is a scheduled LLM session.** Mechanically: every N hours (per `openclaw.json → agents.defaults.heartbeat.every`) OpenClaw spawns an isolated LLM session, loads `HEARTBEAT.md` as the session's instructions, and the model executes them. This is fundamentally different from cron, which just runs a command at a scheduled time. **Implication: defaulting to heartbeat means defaulting to LLM cost on every fire.** Most scheduled checks should NOT be heartbeat-driven.

**OpenClaw optimizes for empty heartbeats.** A heartbeat tick with a comment-only `HEARTBEAT.md` skips the LLM call entirely — security-bot's retirement pattern uses this to keep the file present (for git tracking) while paying $0 per tick (see §17.13).

**Pure-Python scheduled work should be `launchd` or `openclaw_cron` with a script command, not heartbeat.** security-bot's reference pattern: 6 launchd-scheduled Python scripts writing Signals directly to the store — no LLM cost, observable via launchd state, failures visible. This is what most monitoring/health-check work looks like.

**Authoring guidance** (lives in `manifest-authoring-guide.md`, not enforced by spec):
- LLM-judgment work compounding several checks → `heartbeat`
- Deterministic Python work → `launchd` (preferred for reliability) or `openclaw_cron` (preferred for delivery integration)
- Critical work → both, with `safety_net_for` declaring the relationship (§3.4 below)
- Pure shell work → `launchd` (or `crontab` for legacy)

Validating against personal-bot's existing manifests revealed three gaps in `scheduled_actions[*]` as defined in [spec-audit-extensions-2026-05-17.md §3.1](spec-audit-extensions-2026-05-17.md). v20 adds:

**Extended `trigger.kind` enum.** Adds two values to the existing `heartbeat | cron | launchd | session_start` set:

- `openclaw_cron` — scheduled via OpenClaw's internal cron system (`openclaw cron list`). This is the **dominant scheduling mechanism for Evolve bots** — personal-bot has zero user crontab entries but 5 active openclaw crons. Before this spec, the auditor's "missing cron" check couldn't see them.
- `user_command` — triggered by a recognized user phrase (e.g., personal-bot's `MAGIC_COMMANDS.md` single-word triggers like `status`, `blockers`, `tasks`). These are app triggers but not on a schedule; the trigger fires on user intent.

**`state` field on each entry.** New required field with values `active | disabled | paused`:

- `active` — scheduled action is intended to run on its schedule. Default for new entries.
- `disabled` — intentionally turned off; the absence of a mechanism is correct and coherence should not flag it. Example: personal-bot's HEARTBEAT.md documents `protein-daily-checkin (DISABLED — re-enable when the operator returns from travel)`. Coherence Pass A skips disabled actions.
- `paused` — temporarily off with an expected resume time. Stored as `state: "paused", paused_until: "<iso-date>"`. Auditor surfaces a reminder on `paused_until`.

Without these states, every intentionally-paused scheduled action would fire a coherence false positive on every Tier 2 run.

**`quality` field marking legacy noise.** Validating against personal-bot showed the existing scanner over-extracts — Marie campaign manager has 10 `scheduled_actions[]`, most of them first-line snippets of AGENTS.md sections like `summary: "1. Read SOUL.md — who you are"` with `mechanism: "unknown"`. New field:

```jsonc
"quality": "verified | extracted | suspect"
```

- `verified` — operator-confirmed or forge-built; trust completely.
- `extracted` — scanner produced it; trust for structure, verify for substance.
- `suspect` — `mechanism == "unknown"` OR `summary` looks like a section excerpt; do NOT fire coherence findings on these until they've been re-extracted with the tightened prompt or operator-verified.

Migration: every existing entry whose `mechanism == "unknown"` is auto-marked `quality: "suspect"` during the v19→v20 migration. Coherence Pass A respects this flag.

**`safety_net_for` field.** New optional field linking one scheduled_action to another it monitors. Captures the doctrine that critical scheduled work should have two mechanisms — one that does the work, one that verifies the work happened. Shape:

```jsonc
{
  "id": "team-bot-a-task-worker-watcher",
  "trigger": {"kind": "heartbeat", ...},
  "state": "active",
  "safety_net_for": ["team-bot-a-task-worker"],
  "summary": "During heartbeat, check that team-bot-a-task-worker cron has Status != error in the last 25 hours. If error, surface to the operator."
}
```

When `safety_net_for[]` is populated:
- Pass A verifies the referenced action(s) exist on this manifest.
- Pass B checks the safety net itself fires reliably (separate from the work it's watching).
- The relationship appears in the manifest editor and changelog as "watches X."

The safety-net pattern is **recommended but not required.** Coherence Pass A emits a `minor` finding when a `critical`-claimed scheduled action has no safety net AND its only trigger is heartbeat. Heartbeat clobber is the dominant failure mode for Evolve bots — see project memory entries for the Apr 22 AGENTS.md truncation incident and the protein-reminder failure. The operator can accept the finding to suppress, or add the safety net to silence it properly.

**Authoring guidance** — *when to use cron vs heartbeat vs the safety-net pattern* — lives in `manifest-authoring-guide.md` (separate doc), not here. The principle this spec encodes is narrow: critical work should have two mechanisms watching each other; coherence surfaces the absence as a soft warning.

### 3.5 The layer classifier

Pure Python, rules-first, LLM-fallback. Runs once per file per scan; idempotent.

Classification signals in priority order:
1. **Path glob match** against `volatile_paths[]` — wins immediately if matched.
2. **Filename pattern** (`README.md → reference`, `*.lock → state`, `.cache/* → state`, `*.log → log`, `manifest.json/schema.json → contract`, `AGENTS.md/HEARTBEAT.md/SOUL.md → behavior_doc`).
3. **Parent directory** (`scripts/ → code`, `config/ → config`, `data/ → data`, `log/ → log`, `state/ → state`, `content/ → content`, `docs/ → reference`).
4. **Extension** (`.py/.sh/.js/.ts/.rb → code`, `.json/.yaml/.toml → config` (default; classifier may demote to `data` if no code reads it)).
5. **Reference graph**: if any claimed `code`-layer file `read_text()`s the path → `config` or `content` (content if reads happen at runtime; config if at startup).
6. **LLM fallback** for files that survived all rules with no classification — a thin tier-1 call (~1k tokens) that gets the residual.

The classifier is invoked by the scanner once per file per scan and by Tier 2 when a new file appears. The classification is stored on the `files[*]` entry; subsequent runs use the cached layer unless the file's parent directory or filename pattern changes.

---

## 4. Manifest provenance

The reconciliation rules in §3.2 implicitly assume the manifest is a contract — every disagreement deserves attention. **That assumption is wrong for manifests the scanner produced from observation.** A scanner-discovered manifest is a snapshot of reality on the day it was scanned; the next day's reality is the new truth, and the manifest should silently follow it. Forcing the operator to reconcile every observed change is double work — they didn't author the original, so there's nothing to reconcile against.

The fix: **track provenance per top-level field**, and gate reconciliation chip/Signal emission on whether the field was authored.

### 4.1 Provenance categories

| Category | Meaning | Reconciliation behavior |
|---|---|---|
| `observational` | Scanner produced this; no human ever vouched for it | Silently overwrite on re-scan; never emit chip or Signal |
| `forge_built` | Forge built this from an operator-approved spec | Treat as authored — operator vouched for the spec |
| `user_authored` | Operator hand-edited via the manifest editor | Treat as authored — chip on disagreement |
| `bot_authored` | Bot updated via an evo command on user instruction | Treat as authored — user vouched via the conversation |
| `confirmed` | Operator clicked "promote" on a previously observational entry | Treat as authored — explicit promotion |

The four "authored" categories all trigger chips on disagreement. The distinction matters for trail and audit purposes (so we know who authored what when investigating a regression), but the reconciliation rules treat them identically.

### 4.2 Where provenance is stored

A new top-level `provenance` block on the manifest:

```jsonc
"provenance": {
  "manifest_origin":   "observational | forge_built | mixed",
  "created_at":        "2026-05-12T...",
  "created_by":        "scanner | forge | operator | bot",
  "last_promoted_at":  "2026-05-25T...",   // null if never promoted
  "field_origins": {
    "description":          {"source": "user_authored", "by": "operator", "at": "..."},
    "identity":             {"source": "user_authored", "by": "operator", "at": "..."},
    "files":                {"source": "observational"},
    "volatile_paths":       {"source": "confirmed",     "by": "operator", "at": "..."},
    "scheduled_actions":    {"source": "forge_built",   "from_spec": "...", "at": "..."},
    "crons":                {"source": "observational"},
    "requirements":         {"source": "user_authored", "by": "operator", "at": "..."},
    "interface_contract":   {"source": "forge_built", "from_spec": "...", "at": "..."}
  }
}
```

`field_origins` is keyed by top-level manifest field. **A field absent from the map defaults to `observational`** — safe default that errs on the side of "no chip" until the operator actively promotes.

For v1, granularity is **per top-level field**. A future evolution could move to per-entry within list fields (each `files[*]` carries its own provenance) — useful when operator hand-adds 2 files to a list the scanner originally populated. v2 evolution noted in §15.

### 4.3 Promotion paths

How a field becomes authored:

1. **Operator edits the field in the manifest editor.** Save updates `field_origins.<field>` to `user_authored`. Explicit promotion via edit.
2. **Operator clicks "Promote" on a reconciliation chip** or "Mark app as ready" on the manifest view. Promotes touched fields to `confirmed`.
3. **Forge runs against an operator-approved spec.** Forge stamps `forge_built` on every field it materializes from the spec.
4. **Bot user issues an evo command** ("evo, add a weekly summary to journal"). The handler updates the manifest and stamps `bot_authored`.
5. **evo `app-changes <app> promote`** — bot user promotes the current state via chat.

How a field stays observational:

1. **Scanner-discovered manifests are entirely observational by default.** Every field on a fresh-scanned manifest starts as `observational`.
2. **Re-scans of observational fields silently update reality.** No chip, no Signal, no log entry beyond the trail.
3. **Mixed manifests have some authored, some observational fields.** Typical lifecycle: scanner produces fully observational; operator edits description → that field becomes authored; rest stays observational and continues to track reality.

### 4.4 Provenance-aware reconciliation rules

The severity rules from §3.2 apply only to authored fields. The full matrix:

| Field provenance | Disagreement type | Behavior |
|---|---|---|
| Observational | Any (extra files, missing files, missing cron, sha drift, ...) | Silently update manifest to match reality. No chip. No Signal. Trail entry only. |
| Authored (any of the four authored categories) | Per §3.2 severity table | Stage in `reconciliation.*`; emit chip per §5.4; emit Signal at the per-layer severity |
| Authored — extra file with high-confidence provenance marker | Layer ∈ {data, log, state, content, reference} | Auto-merge into `files[]`; trail entry only (the layer makes the change safe) |
| Authored — extra file, layer ∈ {code, config, contract, behavior_doc} | Any | Always require approval, even with provenance marker — these layers are too consequential to auto-add |

This is the key behavior shift: **scanner-discovered apps quietly track reality** until the operator decides to put something on contract. Apps the operator has put on contract surface every meaningful drift.

### 4.5 Coherence is provenance-independent

Coherence (§6) does NOT depend on provenance. Even a fully observational manifest gets coherence-checked: if the description claims daily briefing and `scheduled_actions[]` is empty, that's a coherence failure regardless of who wrote it.

The reason: coherence asks "could this work?" — meaningful for observational manifests too. If a scanner-discovered manifest is incoherent, either the scanner's LLM hallucinated something the app doesn't actually do (description should be corrected), or the app's state is actually broken (the description happens to be right and reality is wrong). Either way, the operator should see it.

Coherence findings on observational manifests still create chips. The operator's typical resolution: edit the description (which auto-promotes it to user_authored), or examine the app and decide whether reality is broken.

### 4.6 Promotion as an opt-in tool

Promotion is the admin's opt-in tool for putting an app's manifest on contract. It is **not** a workflow milestone — most apps will live in observational mode forever, and that is a complete posture. A pod can be healthy with 90% of its apps unpromoted. The Apps page must not surface "you should promote this" prompts; promotion is the admin's deliberate choice, not the system's nudge.

When an admin does choose to promote, the available actions on the manifest view:

- **"Mark this app as ready"** — promotes every observational field to `confirmed` in one click. The common shape: admin looked at a scanner-discovered manifest, decided it's accurate, wants future drift to surface.
- **"Promote this field"** — promotes a single top-level field. Useful when the admin wrote a real description but is fine letting `files[]` continue to track reality.
- **"Freeze this field"** — promotes AND marks `frozen: true`, which blocks observational overwrites entirely until un-frozen. Reserved for cases where the admin wants the manifest's claim to be authoritative even if reality drifts (e.g., during a refactor).

Without explicit promotion, every scanner re-run silently updates observational fields. That's the expected steady state for most apps. The admin only promotes when they have a specific reason to make the manifest authoritative — usually because the app is load-bearing and unintended drift would be a real problem.

### 4.7 Provenance on the audit trail

Every provenance change writes a trail entry:

```jsonc
{"ts": "2026-06-05T...", "kind": "provenance_promotion", "field": "scheduled_actions", "from": "observational", "to": "confirmed", "by": "operator"}
{"ts": "2026-06-05T...", "kind": "provenance_authored",  "field": "description", "by": "operator", "via": "manifest_editor"}
{"ts": "2026-06-05T...", "kind": "provenance_authored",  "field": "scheduled_actions", "by": "bot", "via": "evo_app_modify", "user": "primary"}
```

The trail is the forensic record for "who authored what, when, via which channel." Useful when a regression points back to a specific authored change.

---

## 5. The reconciliation block (schema v20 part 2)

Both scanner and auditor write to `manifest.reconciliation`. This is the **shared vocabulary** between the two passes, and the staging area for changes that need operator decision.

```jsonc
"reconciliation": {
  "last_reconciled_at":  "2026-06-05T18:23:14Z",
  "status":              "ok | proposed_expansion | drift_repair_needed | quiet_failure | orphan",
  "extra_files":         [{path, inferred_layer, confidence, attribution_evidence, detected_at, authored_field_affected}],
  "missing_files":       [{path, expected_sha, since, last_seen, layer, authored_field_affected}],
  "missing_crons":       [{cron_id, expected_schedule, expected_script, since}],
  "missing_actions":     [{action_id, evidence_path, evidence_locator, since}],
  "volatile_growth_anomalies": [{glob, expected, observed_last_window, since}],
  "operator_decisions":  [{kind, target, decision, decided_at, decided_by, rationale}]
}
```

Every entry carries `authored_field_affected` — the top-level field whose provenance triggered the staging. Observational-field disagreements never stage here (they update directly); only authored-field disagreements appear.

### 5.1 The three approval lanes

| Lane | When | Action |
|---|---|---|
| **Auto-merge** (no UI prompt; trail log only) | Field provenance is observational, OR (authored AND change is additive AND layer ∈ {data, log, state, content, reference} AND no cross-app shared resource) | Update `files[]` / `crons[]` / etc. directly; trail entry |
| **Approval-required** (Proposal in inbox; chip on Apps page) | Authored field AND change is non-trivial per §3.2 | Stage in `reconciliation.*`; emit Proposal |
| **Notice-only** (Signal + chip, no automated fix) | Cross-app shared resource OR coherence failure operator must resolve | Emit Signal; chip on Apps page; no Proposal |

### 5.2 Chip vocabulary on the Apps page

Chips only appear when there's something for the operator to decide. Observational-only changes do not surface as chips.

| Chip | When | Click |
|---|---|---|
| "Grew" | `reconciliation.extra_files[]` has entries affecting authored fields | Modal listing extras with rationale; per-file accept/promote/reject |
| "Drift" | `reconciliation.missing_files[]` non-empty (code/config/contract layers) on authored fields | Modal with repair/remove/accept per file |
| "Quiet failure" | `missing_crons[]` or `missing_actions[]` or `volatile_growth_anomalies[]` non-empty | Modal with "Reinstall cron" / "Re-embed heartbeat section" / "Investigate" |
| "Retired" | `reconciliation.status == "orphan"` | Modal with Reinstall/Archive/Convert-to-template |
| "Incoherent" | `manifest.coherence.status != "ok"` (provenance-independent) | Modal explaining which claims have no mechanism |
| "Observational" (badge, not chip) | `provenance.manifest_origin == "observational"` and no field has been promoted | Indicates this app is just being watched, not under contract — informational |

The "Observational" badge is informational only — it's how the operator knows at a glance which apps are tracking reality silently vs which are under contract.

---

## 6. Coherence checks

Coherence is the new axis. It asks whether the manifest, taken on its own, describes something that could plausibly work. Five passes, cost-stratified. **All passes run regardless of provenance** — coherence is a property of the manifest's internal consistency, not of who authored it.

### 6.1 Pass A — Manifest-internal coherence (pure Python, every scan + every Tier 2 run)

Graph walk over the manifest itself. No filesystem, no subprocess, no LLM. Targets ~10ms per app.

**Assertions:**

1. If `description` or `usage.how_to_use` contains a recurring-behavior phrase ("daily", "every morning", "weekly", "every N hours"), then at least one of `scheduled_actions[]`, `crons[]`, `oc_heartbeat_instruction` must be non-empty. Severity: **critical** if all are empty (the briefing-with-no-trigger case).
2. Every `scheduled_actions[*].inputs[*].path` (where `kind != external`) must appear in `files[]` OR match a `volatile_paths[*].glob`. Severity: major.
3. Every `scheduled_actions[*].outputs[*]` must have at least one of: a `code`-layer file in `files[]` that could plausibly produce it (name match heuristic), a declared messaging integration in `requirements.integrations[]`, or a `volatile_paths[]` entry it could be written to. Severity: major.
4. If any `scheduled_actions[*]` declares a messaging output, `requirements.integrations[]` must include a messaging-capable integration. Severity: critical.
5. Every `crons[*].script` must point to a path that is also in `files[]` (with `layer == code`). Severity: major.
6. Every `code`-layer file in `files[]` must be referenced by at least one of: `scheduled_actions[*]`, `crons[*]`, `test_command`, `interface_contract.cli[*]`, or another `code`-layer file (import graph). Files referenced by nothing are "orphan code" — severity minor (might be dead code, might be a library).
7. Every `requirements.integrations[*]` must be referenced by at least one `scheduled_actions[*]`, `crons[*]`, or `code`-layer file (heuristic: import or string match). Severity: minor.
8. `interface_contract.cli[*]` commands resolve to `files[*]` entries. Severity: major.

**Output.** A `coherence` block on the manifest, parallel to `reconciliation`:

```jsonc
"coherence": {
  "last_checked_at": "...",
  "status":          "ok | incoherent | warnings",
  "findings": [
    {"id": "C-A1", "pass": "A", "severity": "critical",
     "assertion": "recurring_behavior_without_trigger",
     "description": "Manifest description claims 'sends a daily briefing' but scheduled_actions[], crons[], and heartbeat instruction are all empty.",
     "evidence": [{"field": "description", "snippet": "..."}, {"field": "scheduled_actions", "value": "[]"}],
     "suggested_remediation": "add_scheduled_action | clarify_description"}
  ]
}
```

### 6.2 Pass B — Substrate coherence (pure Python + subprocess, every Tier 2 run)

Overlaps with the existing Tier 2 assertions in [spec-app-audit-2026-05-16.md §3.1](spec-app-audit-2026-05-16.md). Folded under the coherence framing for symmetry; no new code beyond what Tier 2 already does.

Adds two new assertions:
1. **Volatile growth anomaly.** For each `volatile_paths[]` entry with `expected_growth`, compare actual file-count-delta in the last cadence window against the expected range. Severity: major if zero growth when ≥1 was expected and ≥7 days have elapsed. This is the quiet-failure signal that catches "the cron is running but produces nothing."
2. **Integration credential coherence.** For each `requirements.integrations[*]`, verify the bot's `auth-profiles.json` carries a non-expired credential for that channel. Severity: critical if required and missing/expired.

### 6.3 Pass C1 — Code-shape coherence (static analysis, weekly)

Pure Python, AST + import-graph. No LLM. Targets ~500ms per app.

For each claimed behavior in `scheduled_actions[*]`, walk the implementing code and verify the **shape** is plausible:
1. If output declares a messaging integration, the implementing script must import or reference that integration's module/CLI. (E.g., a Telegram-output action must `import` something Telegram-shaped or `subprocess` a telegram CLI.)
2. If inputs declare file reads, the implementing script must contain a `read_text()` / `open()` / equivalent on a path matching the declared input.
3. If the action declares "summarizes" or "analyzes" in its summary, the implementing script must invoke an LLM (recognized by import patterns: `anthropic`, `openai`, `openclaw`, subprocess to `openclaw agent`).
4. If `crons[*]` declares a script, the script must be syntactically valid (parse with `ast.parse` for Python; `bash -n` for shell).

These are crude heuristics that catch egregious shape violations without LLM. They miss subtle cases — that's what Pass C2 is for.

### 6.4 Pass C2 — Implementation coherence (LLM, monthly via Tier 3)

This folds into Tier 3's existing Stage 3a discovery prompt — no new audit run, no new cost beyond the existing monthly cadence. The prompt gains a coherence section:

> *"For each `scheduled_actions[*]` entry, read the implementing code path and verify the implementation is consistent with the declared inputs, outputs, summary, and trigger. Flag cases where the code exists but doesn't actually accomplish the claim (e.g., 'sends daily briefing' but the script only logs to stdout)."*

Findings emit as the existing `behavior_mismatch` or `manifest_mismatch` observation categories — they flow through Stage 3b triage and into Proposals just like every other Tier 3 finding. No new outbox shape.

### 6.5 Pass C3 — Capability check (LLM, on-demand or on-charter-change)

The most expensive pass. Asks: *"given only this manifest (no code), could a competent developer build something that accomplishes the stated goal?"* This is the only pass that runs without the code as input — it's checking whether the *design* makes sense.

**When it fires:**
1. On charter change — when `description`, `usage.how_to_use`, or `success_criteria.observable_outcomes` is edited, Pass C3 runs against the new manifest before the change is persisted. This is the **pre-deploy coherence gate** for manifest edits.
2. On forge approval — same gate, before forge builds an app from the manifest.
3. On-demand — operator clicks "Check coherence" on the manifest view; runs Pass A + C3 together.

Cost: ~5k tokens per run. Capped at 1 run per app per day to prevent thrashing.

**Output:** a one-shot finding with severity `incoherent | feasible | unclear` plus a one-paragraph rationale. Does not write to `manifest.coherence.findings[]` (which is the recurring-pass log); writes to a separate `manifest.coherence.last_capability_check`.

### 6.6 Pre-deploy coherence gate

Pass A is a **blocker** for:
- Forge approval (must be `ok` or `warnings` to proceed; `incoherent` requires operator override)
- Manual manifest edits via the admin UI (the save button is disabled if Pass A's status would be `incoherent`)

Pass A is a **warning** (non-blocking) for:
- Scanner-discovered manifests on first write (because the scanner may legitimately not have all info yet)
- Re-scans of existing manifests (incoherence may have predated the gate)

This keeps the "you can't ship something broken" doctrine without locking the operator out of fixing legacy manifests that pre-date the spec.

---

## 7. Scanner changes

### 7.1 First-pass layer classification

After Phase 5 (file stamping), a new **Phase 5.5: Layer classification** runs the classifier (§3.5) over every entry in `files[]` across all manifests touched in this scan. Pure Python rules first; LLM fallback only for residual unclassified files. Idempotent.

### 7.2 Provenance handling on re-scan

When the scanner finds a difference between the manifest and reality on an existing manifest:

1. **Look up the field's provenance** in `manifest.provenance.field_origins`.
2. **If observational:** silently update the field to reflect reality. Write a trail entry; no chip, no Signal, no Proposal.
3. **If authored (any of the four authored categories):** stage the difference in `reconciliation.*` and emit a chip per §5.2.
4. **If absent from `field_origins`:** treat as observational (safe default).

This is the single most important behavior change in this spec: **scanner re-runs do not bother the operator about changes to unauthored fields.**

### 7.3 `volatile_paths[]` inference

For each manifest, the scanner proposes `volatile_paths[]` entries by:
1. Looking for directories where the file count grew between scans without manifest amendments (suggests volatile).
2. Looking for filename patterns suggesting volatility (`YYYY-MM-DD.md`, `*.log`, `*.cache`, `*.lock`).
3. Looking for parent directories where >80% of children share a layer (data/, log/, etc.).

For **observational** manifests, inferred `volatile_paths[]` entries are written directly to the manifest. No approval needed.

For **manifests with authored `files[]`**, inferred entries stage in `reconciliation.extra_files[]` with an `inferred_volatile_path` attribution. Operator approves via the standard chip flow; once approved, the entries graduate to `volatile_paths[]` and future scans no longer stage them.

### 7.4 Reconciliation pass

After Phase 5.5, a new **Phase 6: Reconciliation** runs per existing manifest:
1. Walk every `files[*]` entry; mark missing files. If field provenance is observational → silently drop the entry. If authored → stage in `reconciliation.missing_files[]`.
2. Walk every `crons[*]`; mark missing crons. Same provenance gate.
3. Walk every `scheduled_actions[*].trigger.evidence_locator`; mark missing anchors. Same provenance gate.
4. Walk the workspace for un-claimed files. If `files[]` is observational → add directly. If authored → high-confidence provenance markers (and safe layers) auto-merge; low-confidence stages into `extra_files[]`.
5. Update `reconciliation.status` and `last_reconciled_at`.

### 7.5 Coherence Pass A invocation

Phase 7 runs Pass A on every manifest touched and writes the `coherence` block. Provenance-independent.

### 7.6 Change-driven scan scheduling

A timer-based scan cadence wastes LLM tokens when nothing's changed and is too slow when something has. A **change detector** — pure Python, runs cheaply every Tier 2 tick — decides whether a scan is warranted and at what scope. Targeted scans refresh only apps that actually changed; discovery scans only fire when unattributable files appear.

This replaces the time-based scan cadence considered earlier (see commit history). Time-based remains available as a backstop (§7.6.5) but is off by default.

#### 7.6.1 The detector

A new module `applications/change_detector.py`. Pure Python, no LLM. Inputs:

- Current workspace state (file tree with sha256s, heartbeat content)
- **All three cron sources:** `crontab -l`, `launchctl list`, and `openclaw cron list` (the dominant scheduling mechanism for Evolve bots — personal-bot has zero crontab entries but 5 active openclaw crons)
- Last-scan snapshot at `{shared_dir}/applications/{bot_id}/.last_scan_snapshot.json`
- Current manifest set (so the detector knows what's claimed by which app)

The three cron sources are normalized into a single list keyed by `(source, id, schedule, command)`. Reconciliation and coherence checks treat all three equally. Code at `applications/cron_sources.py` (new module) handles the union, including parsing OpenClaw's `cron list` output.

Decision output:

```jsonc
{
  "scan_warranted": true,
  "scope":          "full_discovery | targeted | none",
  "reasons":        ["5 unattributable files in scripts/",
                     "sha drift on code-layer file at scripts/journal.py"],
  "targeted_apps":  ["journal"],          // populated when scope = targeted
  "evidence":       {...}                 // file lists, drift details, for audit trail
}
```

The detector runs every Tier 2 tick (every 6h). Decision is written to `{shared_dir}/applications/{bot_id}/.detector_decision.json` so the UI can render the current state without re-running the detector.

#### 7.6.2 What warrants a scan

- New file not in any manifest's `files[]` and not matching any `volatile_paths[*].glob`, where inferred layer is `code` | `config` | `behavior_doc`
- ≥3 unattributable files in any layer (suggests a new app cluster)
- Sha drift on a `code` | `config` | `contract` layer file claimed in a manifest
- New cron entry not claimed by any manifest
- Heartbeat section added or removed
- Claimed code/config/contract file removed from disk

What does NOT warrant a scan:
- New files inside a `volatile_paths[*].glob` (expected by definition)
- New files in `data` | `log` | `state` | `content` layers in directories matching app conventions (handled by reconciliation)
- Sha drift on `data` | `log` | `state` | `content` layer files
- File mtime changes without content change

The detector's classification of new files uses the rules-first cascade from §3.4 (no LLM) so the warrant decision itself stays free.

#### 7.6.3 Attribution for targeted scans

When the detector finds changes, it attributes each to an existing app using pure-Python signals:

1. File is in a directory containing claimed files of app X → app X
2. File matches a `volatile_paths[*].glob` of app X → app X
3. File has a `<!-- evolve-managed: pkg=X -->` provenance marker → app X
4. File is imported by code in app X (import-graph scan) → app X
5. None of the above → unattributable (suggests new app)

When all changes attribute to existing apps:
- **Targeted scan** runs Phase 4 (manifest regeneration) only for attributed apps; skips Phase 2 LLM discovery. Cost: ~10k tokens for one app vs ~40k for a full discovery scan.

When ≥3 changes are unattributable:
- **Discovery scan** runs full Phase 1–5 pipeline. The Phase 2 LLM clustering is what gives the new-app boundary.

When both: discovery scan first (to identify the new cluster), then targeted scan for the existing apps with changes. Run sequentially within the same scan window.

#### 7.6.4 The quiet window

When the detector flags a scan, it queues at the next configured **quiet window** (default 2 AM local time), not immediately. Rationale:
- Avoids competing with user-facing inference during active hours
- Batches up changes if the user is actively iterating (the operator's "evening" scan from your spec ask)
- Predictable scheduling makes operator observability easier ("scans run nightly when warranted")

Per-bot override via `network.json → app_scan.quiet_window_hour` (24h clock). Operators who prefer immediate can set `app_scan.run_immediately: true`.

For **urgent** changes — heartbeat clobber, missing claimed crons on authored manifests — reconciliation handles them in the current Tier 2 cycle without waiting. The scan is for refreshing LLM-derived fields (identity, success_criteria, description) and for discovering new app clusters; neither is time-critical.

#### 7.6.5 Time-based backstop

Operators can optionally enable a backstop: "if no change-driven scan has run in N days, force a discovery scan." Default off; default N is 30 if enabled. The backstop catches:
- Changes the detector might have missed (e.g., snapshot file corruption)
- Pure-LLM-field drift (description rephrasing that doesn't change files but might change classification)
- Compliance-minded operators who want a known maximum interval

This is the only timer-based path; everything else is event-driven.

#### 7.6.6 The manual scan path

The "Scan now" button in the UI bypasses the detector and runs a full discovery scan immediately. The button shows the detector's current state as context:

> *"Last scan: 4 days ago. Detector found 2 unattributable files (`scripts/analyze.py`, `scripts/summarize.py`); targeted scan queued for tonight at 2 AM. [Scan now] [Wait for scheduled]"*

evo command `evo app-scan <bot>` follows the same path — bypasses detector, runs immediately.

CLI `evolve-admin application scan <bot> [--targeted <app>] [--discovery]` exposes the same modes for scripted operator workflows.

#### 7.6.7 Detector failure modes

- **Snapshot file missing or corrupted** → detector treats as first run; recommends full discovery
- **Workspace not readable** → detector logs error, returns `scan_warranted: false` with `error_status` populated; falls back to time-based backstop if configured
- **crontab / launchctl not accessible** → detector continues with file-state only and notes the gap

Detector failures never block the rest of Tier 2's work — reconciliation, coherence Pass A/B, and audits continue independently.

#### 7.6.8 UI surface

In Pod settings → App audit & scanning:

```
SCAN TRIGGERS
[•] Change-driven (recommended)
    Quiet window: [2:00 AM] local time
[ ] Time-based cadence (monthly / weekly)

[✓] Backstop: if no change-driven scan in [30] days, force discovery
[✓] Always scan after bot deploy

PER-BOT OVERRIDES
  team-bot-a   change-driven   quiet 2 AM
  team-bot-b   change-driven   quiet 3 AM   (active dev)
  team-bot-c   manual only

TOKEN BUDGET
  Per-bot daily scan cap: [50k]   (scans fail fast if over budget)
```

Per-app cadence in the manifest editor collapses to two options for change-driven mode: `inherit` and `manual only`. Per-app time-cadence loses meaning when scanning is event-driven.

The "Scan now" button (§7.6.6) is on every Apps page tile, showing the detector state inline.

### 7.7 Tiered scan execution

When the change detector (§7.6) flags that a scan is warranted, it specifies a scope (targeted vs discovery) but not the LLM tier. **The LLM tier is decided by the work itself.** Routine cases run on a cheap model (Tier 1, haiku-class). Complex cases escalate to the expensive default (Tier 3, sonnet/opus-class as configured per bot). Most scans are handled by the cheap path; escalation is the safety net.

The principle: don't burn expensive tokens on menial work. Stamping a new file into `files[]` with the right layer doesn't need the same model that wrote the manifest in the first place.

#### 7.7.1 Three execution paths

| Layer | Cost | Decides | Does |
|---|---|---|---|
| **Python filter** | $0 | Pre-routes based on change shape | Routes pure structural updates to reconciliation; nothing else |
| **Tier 1 quick scan** | ~$0.005 (~2k tokens) | Handles routine changes; escalates ambiguous ones | Patches manifest with new files, layers, sha updates, small description additions, cron entries |
| **Tier 3 deep scan** | ~$0.10–$0.40 (~10–40k tokens) | Final stop for complex work | Discovery clustering; full manifest regeneration; substantive field rewrites |

Independent of the change detector's scope decision: Tier 1 can handle a "targeted" scope; Tier 3 fires for "discovery" or when Tier 1 escalates from a "targeted" scope.

#### 7.7.2 Decision matrix

Implemented in a new `applications/scan_router.py` — thin Python layer between the change detector and LLM dispatch.

| Change shape | Path |
|---|---|
| File in `data`/`log`/`state`/`content` matching a `volatile_paths[*].glob` | Filter (reconciliation) |
| File added in `data`/`log`/`state`/`content` outside any glob | Filter (proposes `volatile_paths[]` entry) |
| Sha drift on `data`/`log`/`state` | Filter (silent update on observational) |
| 1–2 files added in `code`/`config`, confident attribution | Tier 1 quick scan |
| Cron added/removed, attributable to existing app | Tier 1 quick scan |
| New heartbeat section, attributable | Tier 1 quick scan |
| Sha drift on `code`/`config` | Tier 1 quick scan |
| Tier 1 escalates | Tier 3 follow-up per Tier 1's escalation target |
| ≥3 unattributable files (cluster suggests new app) | Tier 3 discovery |
| Manual "Scan now" | Tier 3 full discovery (operator explicitly requested) |

#### 7.7.3 Tier 1 quick scan

**Inputs** (small, fits in cheap-model context):
- Change list from the detector (which files, what change)
- Current manifest(s) of affected app(s)
- Content of changed files (truncated if large)
- ~30 lines of recent trail for context

**Prompt frame:**

> *"Review these recent changes to the app's workspace in the context of its current manifest. Update the manifest if the changes are routine — file additions matching existing patterns, content updates, sha drift on already-described files, new helper scripts that fit the app's stated purpose. Apply your updates as manifest patches in the structured form below.*
>
> *Escalate if any of these hold:*
> *- New integration imports (slack, gmail, telegram, etc.) not declared in `requirements.integrations[]`*
> *- A new scheduled behavior pattern (new cron, new heartbeat section) without a clear matching scheduled_action*
> *- Description-level shift — the app appears to do something materially different from its current description*
> *- You cannot confidently attribute the change to this app's scope*
>
> *When in doubt, escalate. Tier 3 follow-up is the safety net; over-handling at Tier 1 is the failure mode."*

**Output schema:**

```jsonc
{
  "scan_id": "scan-<8hex>",
  "tier":    "tier1",
  "tokens":  {"input": 1842, "output": 412},
  "verdict": "handled | escalate",
  "manifest_patches": [
    {"app_id": "journal", "field": "files", "op": "add",
     "value": {"path": "scripts/helper.py", "layer": "code", "purpose": "..."}},
    {"app_id": "journal", "field": "description", "op": "append",
     "value": " Helper utilities for date formatting."}
  ],
  "escalation_target":      "manifest_regen | discovery | coherence_check",
  "escalation_target_apps": ["journal"],
  "escalation_reason":      "..."
}
```

When `verdict = handled`, patches apply directly to the manifest (respecting provenance: observational fields update silently; authored fields stage chips per the existing rules). A `manifest_change` trail entry records the change.

When `verdict = escalate`, the Tier 1 patches are **discarded** (the follow-up will produce a more thorough update). Tier 1's role is just routing.

#### 7.7.4 Tier 3 follow-up triggered by Tier 1 escalation

Three escalation targets, three follow-up shapes:

- **`manifest_regen`** → Tier 3 Phase 4 for the named app(s). Refreshes identity, success_criteria, description, and other LLM-derived fields.
- **`discovery`** → Tier 3 Phase 2 + Phase 4. New-app clustering + full manifest generation. Same as a `scope: discovery` detector decision.
- **`coherence_check`** → Coherence Pass C3 (§6.5) on the named app. Validates that the new claims hang together.

Follow-up runs in the same scan window (don't make the operator wait for the next 2 AM) but as a separate runner invocation so each has its own token budget and timeout. Trail entry records the escalation chain (`tier1 → tier3:manifest_regen`).

#### 7.7.5 Coherence check gating

Following the principle that "a new file may not need a new coherence check":

- **Pass A** (free, mechanical graph walk) runs after every manifest change regardless of tier. Always on.
- **Pass C1** (free, static analysis) runs weekly regardless. Always on.
- **Pass C2** (folded into monthly Tier 3 audit) runs on the existing audit cadence. Always on.
- **Pass C3** (expensive LLM coherence) ONLY fires when:
  1. Tier 1 explicitly escalates `coherence_check`
  2. Operator clicks "Check coherence" or charter changes (§6.5)

This means a routine new-file addition doesn't trigger any LLM coherence — Pass A's graph walk catches anything structurally broken for free.

#### 7.7.6 Tier configuration

Per-bot config in `network.json`:

```jsonc
"app_scan": {
  "tier1_model":     "tier1",   // bot's haiku-class for quick scans
  "tier3_model":     "tier3",   // bot's sonnet/opus-class for deep scans
  "quiet_window_hour": 2,
  "daily_token_cap":   50000
}
```

Defaults pull from the bot's existing tier resolver. Operators almost never need to touch these; they exist for cost-conscious deployments that want to cap quick-scan cost per bot.

The Tier 1 model can be disabled entirely via `tier1_model: null` — every flagged scan then goes straight to Tier 3. Useful for testing the Tier 3 path or when a bot's haiku-class budget is exhausted.

#### 7.7.7 Cost story (revised, this supersedes §7.6.5's targeted/discovery numbers)

For a bot with daily user activity (~10 file change events per day):

| Path | Daily | Monthly |
|---|---|---|
| Filter-only (free) | ~6/day | $0 |
| Tier 1 handles | ~3/day × $0.005 | $0.45 |
| Tier 1 escalates → Tier 3 | ~1/day × $0.10 | $3.00 |
| **Total per bot per month** | | **~$3.45** |

Compare to "always Tier 3 when scan is warranted" (pre-tier design): ~$30/bot/month for the same activity. Tiered scanning saves **~90%** on active bots.

For a stable bot (no changes most days), tiered scanning costs $0 — same as event-driven baseline, but without ever burning Tier 3 tokens on the routine changes that do occasionally happen.

#### 7.7.8 Failure modes

- **Tier 1 returns malformed output** → log error, escalate to Tier 3 manifest_regen for the targeted apps (fail-safe to expensive path)
- **Tier 1 over-handles** (applies patches that should have escalated) → caught by Pass A on next Tier 2 tick; Pass A surfaces the resulting incoherence; operator sees the chip and can request repair
- **Tier 1 escalates everything** → cost rises toward the Tier-3-only baseline; if this happens systematically, the Tier 1 prompt needs tightening (open question Q17)

---

## 8. Audit runner changes (bot-side)

Folded into the existing `audit_runner.py` from [spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md). No new runner, no new daemon.

### 8.1 Tier 2 extensions

- Layer-aware severity (§3.2 table) replaces uniform severity in existing assertions.
- **Provenance-aware Signal emission:** Tier 2 findings on observational fields are recorded in the trail but NOT emitted as Signals. The principle is the same as for the scanner — observational changes don't bother the operator.
- Volatile-path skip: files under `volatile_paths[*].glob` are skipped in the per-file assertions.
- Coherence Pass A runs at the start of every Tier 2 tick; findings land as Signals regardless of provenance (because coherence is provenance-independent).
- Coherence Pass B (volatile growth + integration credential) runs as new Tier 2 assertions.
- Reconciliation deltas write back to `manifest.reconciliation` directly when the affected field is authored; for observational fields the runner updates the manifest in place (e.g., drops a missing file from `files[]`).

### 8.2 Tier 2 cadence for coherence Pass C1

Pass C1 (static analysis) is heavier than Pass A but still mechanical. Runs weekly inside the same Tier 2 runner — every Sunday's 6h tick instead of every tick. Configurable via the synced `pod_config.json`.

### 8.3 Tier 3 extensions

Stage 3a's prompt gains the coherence-Pass-C2 section (§6.4). Same monthly cadence, same cost envelope. Stage 3b's allowed-transformation list does *not* gain coherence fixes — Pass C2 findings always emit as Proposals (operator approves; no auto-fix).

### 8.4 On-demand Pass C3

A new audit-runner mode `--coherence-check` runs Pass A + Pass C3 against a manifest passed via inbox file. Triggered by:
- Forge dispatching pre-deploy gate
- Admin UI "Check coherence" button
- evo `evo app-coherence <app>` command (added to the existing wizard)

Token cap of 5k per run; admin-side rate limit of 1 run per app per day.

---

## 9. Orphan handling

When Tier 2 detects `len(missing_files) == len([f for f in files if f.layer in {'code','config','contract'}])` (every infrastructure file is gone), and no `volatile_paths[]` glob has files in it, the app is considered orphan.

**Provenance modulates the response:**

- **Observational orphan:** rare (scanner wouldn't have discovered the app without files). When it happens, the manifest's whole files[] silently drops, then the manifest is marked `reconciliation.status = "orphan"` and moved to the "Retired" section. No notification — the manifest was never authored.
- **Authored orphan:** when an authored manifest goes orphan, this is a loud signal. The operator vouched for this app; its infrastructure disappearing is meaningful. Chip on Apps page (visible), notification to operator, optionally to bot user.

Hysteresis: orphan status requires two consecutive Tier 2 runs (12h apart) confirming the condition. Prevents false positives during deploys.

Manifest stays on disk in both cases. Apps page surfaces:
- Reinstall (forge the spec back to disk; only meaningful for authored manifests with `scheduled_actions[]` / `interface_contract` that forge can rebuild from)
- Archive permanently (UI hidden; file stays for forensic recall)
- Convert to template (becomes a forge seed for a new install elsewhere)

---

## 10. The reconciliation interface

The interface deserves real thought because the experience here is what makes this whole system feel light vs heavy. A system that buzzes every day for trivial changes will be turned off; a system that surfaces nothing will lose trust.

**Two distinct audiences, two distinct primary surfaces:**

- **Bot users** — the people whose bots these are. They live in chat, rarely visit the admin UI. Their primary surface is the **bot's own conversation context** (§10.9), augmented by proactive messages when something critical fires. The bot is aware of its own findings, mentions them when relevant, and accepts "fix it" through natural conversation.
- **Pod admins** — the technical operator of the pod. They visit the admin UI when something specific needs investigation. Their primary surface is the **Apps page modal** (§10.3) and the changelog (§11.5).

The same underlying findings power both surfaces. Most findings on most pods will be seen first (and often resolved entirely) through the bot conversation; the admin UI is the diagnostic surface for cases the conversational flow can't close.

Design target: **silent for observational drift, conversational for typical authored drift, modal-driven only for bulk operations or admin investigation.**

### 10.1 Design principles

1. **Default to silent.** Observational manifests update without surface — no chip, no notification, no inbox item. The audit trail records the change for forensic recall, but nothing demands operator attention.
2. **One surface per audience.** Operators (pod admin) see chips on Apps page + reconciliation modal in admin UI. Bot users (whose bot it is) see evo notifications + can resolve via `evo app-changes` commands. No one is forced to use the surface that doesn't fit their workflow.
3. **Conversational before modal.** When the bot user asked the bot to add a feature, the user already knows about the change. evo can confirm it ("I noted 2 new scripts for the journal app") rather than forcing a reconciliation modal.
4. **Bulk operations are first-class.** A scan that finds 47 new files in `data/journal/` should propose one `volatile_paths[]` entry, not 47 chips. The operator interface emphasizes patterns over instances.
5. **Promotion is a deliberate action, not a side effect.** "Approve" buttons don't accidentally promote observational fields to authored; promotion is its own explicit action with clear language.
6. **Repair, reconcile, and accept are distinct verbs.** Each maps to a different operation shape (§10.7).

### 10.2 Surface inventory

| Audience | Surface | When it fires |
|---|---|---|
| Pod operator | Chip on Apps page tile | Authored field disagrees with reality, OR coherence finding |
| Pod operator | Manifest editor "Coherence" indicator | Pass A finding regardless of provenance |
| Pod operator | "Manifest changes" weekly digest | Summary of observational updates worth knowing about (opt-in; default off) |
| Bot user | evo notification | Authored field disagrees with reality on their bot, OR quiet failure |
| Bot user | `evo app-changes <app>` (pull) | User initiates a review |
| Forensic | `provenance.field_origins[*].at` trail | Always, no surface — for investigation |

The weekly digest is an opt-in low-touch surface for operators who want a sense of observational activity. Default off because most operators don't want it; available because some will.

### 10.3 The reconciliation modal (per-app)

Four tabs, ordered by frequency of use:

**Tab 1 — Changes since last review.**

The default tab. Shows what's changed (observational + authored, with provenance badges). Each row has:
- Field / file / cron affected
- Provenance badge (`observational` in muted gray; `authored` in primary color)
- What changed
- Per-row actions: *Approve* (accept the change in place, leaves provenance unchanged), *Promote* (mark as authored going forward — sticky), *Revert* (restore prior state, only if backup available)
- Bulk actions at the top: *Approve all observational changes*, *Promote all current state to authored*

The bulk-promote action is the "mark this app as ready" affordance — one click takes a freshly-scanned app and commits its current state to contract.

**Tab 2 — Quiet failures.**

`reconciliation.missing_crons[]`, `missing_actions[]`, `volatile_growth_anomalies[]`. Each row has a "Reinstall" or "Investigate" action. Bulk action: *Reinstall all from manifest*. Visually separate from Tab 1 because the resolution shape is different (repair vs accept) and the urgency is higher (quiet failures are user-impacting).

**Tab 3 — Drift on contract.**

`reconciliation.missing_files[]` and `extra_files[]` for authored fields only. Each row has Repair / Remove / Accept actions. Observational extras don't appear here — they were silently merged.

**Tab 4 — Coherence.**

`coherence.findings[]`. Each finding shows the involved manifest fields with a "Fix the manifest" deep-link. Provenance-independent — even observational manifests can fail coherence.

### 10.4 Conversational reconciliation via evo

For the bot user (not the operator), the natural channel is conversation. New evo grammar:

| Form | Effect |
|---|---|
| `evo app-changes` | Lists this bot's apps with recent observational + authored changes |
| `evo app-changes <app>` | Walks the user through changes to the app since last review |
| `evo app-changes <app> approve` | Approve all current changes (leaves provenance) |
| `evo app-changes <app> promote` | Promote all current state to authored — equivalent to "mark this app as ready" |
| `evo app-changes <app> flag <description>` | Add a freeform note about a concern; lands as a Proposal for the operator |
| `evo app-coherence <app>` | Run Pass A + C3 capability check; reply with the result |

Reply example:

> *"The journal app's manifest updated three things since you last looked. New file `scripts/analyze.py` was added (you asked me to add summary analysis last week — this is the script for that). Two new entries in `data/journal/` are normal data growth. The 6pm trigger is **missing from crontab** — looks like a quiet failure. Want me to reinstall it from the manifest? Reply `yes` to reinstall, `flag` to ask the operator to look, or `all good` to accept and move on."*

The bot speaks the conversational version of what would otherwise be a modal. The user can resolve by replying *"yes"* (reinstall the cron) or *"all good"* (approves all observational changes) without leaving the chat.

### 10.5 The bot-user vs operator split

For personal bots, the bot user IS the operator (same person). For team bots, they diverge: bot user is the team's primary user; operator is the pod admin. Notification routing respects this:

- **Authored-field drift** notifies the operator (always) and the bot user (if `bot_user_notifications.app_drift = true` on the bot's network config; default true for personal bots, false for team bots since team bot users may not care about manifest contracts).
- **Observational changes** notify nobody by default; appear in the opt-in weekly digest.
- **Quiet failures** notify both — the user needs to know if the app stopped doing the thing they asked for, the operator needs to know to repair.
- **Coherence failures** notify the operator. Bot user sees them only on explicit `evo app-coherence` invocation.

### 10.6 Promotion as a primary action

The modal makes promotion a clear, separate action distinct from approval. Concrete language:

- "Approve change" — accept that the change happened, leave field as observational (continues to track reality)
- "Promote to authored" — explicitly mark this field as contract going forward (future drift will surface)
- "Mark app as ready" — promote the whole manifest in one click

This matters because operators will often want to "watch a scanner-discovered app for a while" before they're ready to put it on contract. The promotion step is when they commit. Until then, the manifest just tracks.

### 10.7 Repair vs reconcile vs accept

Three different verbs, kept visually and linguistically distinct:

- **Repair** — restore reality to match an authored claim. Reinstall a missing cron from manifest spec. Restore a missing file from git/backup. Requires the manifest claim to be authoritative; only meaningful for authored fields.
- **Reconcile** — update the manifest to match new reality. Add an extra file to `files[]`. Strike a removed cron from `crons[]`. Requires operator to confirm reality is correct. For observational fields this happens silently; for authored fields it requires per-row Approve.
- **Accept** — note that reality and manifest disagree, but the disagreement is intentional and should not flag again. Writes to `coherence_accepted[]` / `audit_accepted[]` / `reconciliation_accepted[]`. The field's provenance is unchanged.

The modal's button language reflects this distinction. No "Approve" buttons that silently do different things in different rows.

### 10.8 Sticky state and re-flash prevention

When the operator dismisses a chip or resolves a row in the modal, the resolution is sticky:
- Approved extra files stay in `files[]`; the chip doesn't reappear next scan.
- Accepted disagreements write to `reconciliation_accepted[]` so the same finding doesn't re-stage.
- Promoted fields stay promoted; subsequent scanner runs respect the new provenance.

Re-flash only happens when the underlying signature changes (different file, different cron). Same finding re-occurring after acceptance is logged but suppressed unless the operator explicitly runs a "full reconcile" that ignores accepted entries (parallel to the existing "full audit" flag in [spec-app-audit-2026-05-16.md §5.5](spec-app-audit-2026-05-16.md)).

### 10.9 Bot-side awareness — the primary surface for bot users

The Apps page + reconciliation modal above is the **admin's** surface. Most bot users (the people whose bots these are) never open it. For findings to be useful, they have to reach the user through the bot itself.

The principle: **the bot should know what's wrong with its own apps, mention it when asked, alert the user when something's broken, and accept "fix it" via chat.** This is the natural mode for a household or small-team pod where the bot user lives in chat and rarely visits a dashboard.

Three mechanisms working together:

#### 10.9.1 Session-start surface

The bot's session_start mechanism (which already handles POD_CONDUCT, per memory) gains an "Apps" block — a short summary of current findings on this bot's apps:

> *Your apps have 1 finding worth noting:*
> *- `journal` (quiet failure): the 6pm cron is missing from crontab. If the user mentions the journal app not working, offer to repair via `evo app-repair journal`.*

The block is **short** — just enough that the bot has context to mention findings naturally if the user asks "anything wrong?" or invokes a broken behavior ("send me the briefing" when the briefing's trigger is missing). It is NOT a script the bot reads aloud unprompted.

When no findings exist, the block is absent — no session-start pollution on healthy bots.

The block is refreshed by the admin's tick when the Signal/Proposal stores update. Implementation rides on the existing session_surface infrastructure (per memory: "Pod conduct injection mechanism").

#### 10.9.2 Heartbeat injection (for time-sensitive findings)

For findings that should affect the bot's behavior immediately (not just on next session start), a heartbeat injection delivers them during ongoing sessions. Used sparingly — most findings are fine in the session-start surface. Reserved for:
- Critical quiet failures the bot might otherwise attempt to invoke ("send the briefing" with a missing cron)
- Findings that fire mid-session due to repair completion or external state change

Injection content follows the same short-summary shape as the session-start block.

#### 10.9.3 Proactive user notifications

When a critical finding lands on a bot's app, the existing `signal_notifier` (per memory) dispatches a user-facing message via the bot's normal messaging channel. New signature patterns to add to `signal_notifier._DEFAULT_PRODUCERS`:

- `app_reconciliation.quiet_failure` — critical, notify always
- `app_coherence.incoherent` — major, notify if user opted into broader app alerts
- `app_audit.critical_finding` — critical, notify always
- `app_reconciliation.drift_repair_needed` — major (authored only), notify on opt-in
- `app_orphan.confirmed` — critical (authored only), notify always

Message shape: short, plain, ends with an offer to repair.

> *"FYI — the 6pm trigger for your journal app has stopped firing. The cron is missing from your bot's crontab. Want me to look into it? Reply `yes` to repair, `ignore` to skip, or `what's wrong` for details."*

User replies are handled by the bot's normal session context (which now has the finding in its session_start surface). The bot interprets the reply and either dispatches repair (`evo app-repair journal`) or snoozes the finding (Defer semantics).

#### 10.9.4 Conversational repair via natural language

Beyond the explicit `evo app-repair` and `evo app-changes` commands (§10.4), the bot should recognize **natural-language equivalents** as repair intents:

| User says | Bot does |
|---|---|
| "fix the journal app" | Dispatch repair on highest-severity journal finding |
| "what's wrong with journal" | Read findings from session context; explain in plain language |
| "the briefing isn't working" | Look for findings matching "briefing" — likely quiet failure on the briefing app's cron — confirm and offer repair |
| "ignore that for now" | Defer the most-recently-discussed finding |
| "anything broken?" | Summarize current findings across all this bot's apps |

These don't require new evo grammar — they ride on the bot's natural conversational handling now that findings are in its session context. The grammar (§10.4) is the explicit-command path for users who prefer that.

#### 10.9.5 Routing — admin vs bot user

| Audience | Surface | When |
|---|---|---|
| Admin | Apps page chips + modal | Reviewing pod, investigating specific app, deliberate workflow |
| Admin | Existing Signal/Alerts page | Cross-pod monitoring |
| Bot user | Session-start surface (always) | Every session — natural conversational awareness |
| Bot user | Heartbeat injection (rarely) | Mid-session for time-sensitive findings |
| Bot user | Proactive message (per opt-in) | Critical findings — immediate awareness |

For team bots, audience-scoping (per memory: "Three user types and approval routing") determines who gets the user-facing message — typically `team_member` for member-relevant findings, `pod_operator` for admin-relevant ones. Mixed-audience findings notify both via different channels.

#### 10.9.6 Session-start block content rules + examples

The block follows precise content rules so the bot has reliable structure to interpret — not free-form prose.

**Personal bot with findings:**

```
[Apps] 2 findings to be aware of.

CRITICAL:
- journal: the 6 PM cron is missing from crontab. The user may notice
  the daily summary not arriving. If they mention this, offer to repair
  via `evo app-repair journal`. The user can also say "fix journal" or
  similar — treat as a repair request.

OTHER (1 more):
- briefing: gmail credentials are within 30 days of expiry. Not blocking
  yet; user can renew via Settings → Integrations or by saying
  "renew gmail credentials".
```

**Team bot (audience-aware):**

```
[Apps] 1 finding visible to the primary user (Diana).

CRITICAL:
- shared-calendar: description claims "syncs with Google Calendar daily"
  but no scheduled_action declares the sync. Diana can request repair.
  Other team members reporting this: acknowledge, say "I'll let Diana
  know," and don't dispatch.
```

**Healthy bot:** block is **absent**. Don't pollute session start when everything's fine.

**Overflow (>5 findings):**

```
[Apps] 7 findings to be aware of.

CRITICAL:
- journal: missing 6pm cron
- briefing: gmail credentials expired
- mood: missing scripts/track.py

4 more (lower severity) — say "evo app-changes" for full list.
```

**Content rules:**

1. Header: `[Apps] N finding(s) to be aware of.` Or `[Apps] N findings visible to <audience>.` for team bots.
2. Group by severity (Critical first, Other second).
3. Each line: `<app>: <one-line description>. <repair-hint>.`
4. Cap at 5 visible findings; overflow line for the rest.
5. **Snoozed findings excluded.** They were explicitly deferred; surfacing them defeats the purpose. Reappear when the snooze elapses.
6. Block absent when no eligible findings.

The block is regenerated whenever the underlying Signals/Proposals change — refreshed on the admin's tick, written to the existing session_surface file the bot reads at session start (per memory: "Pod conduct injection mechanism").

#### 10.9.7 Team-bot authorization for natural-language repair

The pattern: **reads are open, writes escalate.** A team member observing a problem is valuable information; the authorization for fixing it routes to the appropriate authority.

Authorization tiers:

| User | Read findings | Defer | Repair | Promote |
|---|---|---|---|---|
| Pod admin | Yes | Yes | Yes | Yes |
| Bot primary user | Yes | Yes | Yes | No (contract decision is admin-only) |
| Bot team member | Yes (limited) | No | No | No |
| Other | No | No | No | No |

When a team member says "fix the calendar app":

1. Bot recognizes intent.
2. Audience check fails (no repair authority).
3. Bot acknowledges: *"Thanks for letting me know — I'll flag this for Diana."*
4. Bot composes a message to the primary user via the same dispatch path used for proactive notifications (§10.9.3): *"Family member <X> reported the shared calendar app isn't working. The current finding: <summary>. Want me to look into it?"*
5. Primary user replies; if yes, repair dispatches.
6. Trail records the chain (`reported_by:X → escalated_to:Diana → approved → repair_applied`).

Same flow for "anything broken?" from a member — bot answers freely (read is open); if they then say "fix it," same escalation.

For personal bots there's no split — the user is both primary and admin; all four operations are available.

#### 10.9.8 Commands vs natural language — when each is right

The `evo app-*` command grammar (§10.4) and the bot's natural-language repair-intent recognition (§10.9.4) are not redundant. They serve different needs:

| Situation | Surface | Why |
|---|---|---|
| User reports something conversationally | Natural language | Lowest friction; users don't know commands exist |
| User wants precision | Explicit command | Unambiguous target; no LLM interpretation step |
| Script or routine | Explicit command | Stable contract; won't drift with the bot's prompt |
| Admin investigation | Either; CLI for batch | Power-user precision |
| Multiple findings, "fix it" ambiguous | Natural language → bot asks clarification | Conversation handles ambiguity well |
| Documentation ("to fix X, do Y") | Explicit command | Documentable; doesn't change as bot updates |
| Member reporting issue | Natural language | They don't know the grammar |

The natural-language recognition (PR 12) makes conversation the default for users. The commands (PR 13) remain useful for the cases above. A power user mixes freely: *"Fix journal"* and *"evo app-repair journal"* produce the same outcome.

**Important:** the bot does NOT push users toward commands. If a user says "fix it" conversationally, the bot just does it. Commands are an option, not a destination. The session-start block's repair-hint language (§10.9.6) mentions both — *"can also say 'fix journal' or use `evo app-repair journal`"* — so the bot has context for either path but doesn't pressure either.

---

## 11. Decisions, repair, and the changelog

The reconciliation framework above produces findings — what's drifted, what's incoherent, what's grown. This section covers what the operator actually *does* with those findings, what happens after each decision, and how the system fixes things when the answer is "this is wrong, fix it." Without a repair mechanism, the audit framework is informational; with one, it's actionable.

### 11.1 The three-decision model

Three operator actions on any finding, deliberately distinct in semantics and language:

| Decision | What the operator means | What the system does |
|---|---|---|
| **Approve** | Current state is correct | Commit state change appropriate to finding (§11.2); dismiss flag; log in trail |
| **Repair** | Current state is wrong, fix it | Dispatch repair session (§11.3); log in trail |
| **Defer** | Not now, ask later | Snooze chip for chosen interval; log in trail |

"Reject" is not a separate verb because rejecting a finding without action means it stays broken. The cleaner pattern: if the operator thinks something is wrong, they hit Repair; the system either fixes it directly or surfaces a Proposal for a non-mechanical change.

A fourth action, **Promote**, exists for changing provenance — not part of the decision flow on findings, but available on the manifest view as a separate operation (§4.6).

### 11.2 What Approve actually does (per-finding-type)

Approve is not cosmetic for most finding types — it commits a real state change to the manifest:

| Finding | Approve semantics |
|---|---|
| Extra file (authored field) | Add file to `files[]` with classifier-inferred layer; promote field provenance to `confirmed`; log |
| Missing file (authored field) | Remove entry from `files[]`; log in `operator_decisions[]` and trail |
| Sha drift (any layer) | Update `manifest.files[*].sha256` to new value; log new baseline |
| Missing cron / heartbeat | Approve is disabled — Repair is the natural action. To remove the trigger from contract, edit the manifest via the editor. |
| Volatile growth anomaly | Add signature to `reconciliation_accepted[]`; chip suppressed |
| Coherence finding | Add signature to `coherence_accepted[]`; chip suppressed |

The only finding types where Approve is genuinely cosmetic are the `*_accepted[]` dismissals. For everything else, Approve commits state change that future scans respect.

### 11.3 The repair session

The load-bearing primitive. An LLM agent, dispatched on the bot, that takes the manifest + findings + operator intent and either applies mechanical fixes or emits Proposals for non-mechanical ones.

#### 11.3.1 Dispatch

Operator clicks Repair on a chip or modal row. Admin server:
1. Assembles the input bundle (§11.3.2)
2. Writes `/Users/<bot>/.openclaw/workspace/evolve/audit_inbox/repair-<id>.json`
3. Kicks `audit_runner.py --repair` via the existing sudo dispatch path

Same dispatch shape as the audit kick from [spec-app-audit-2026-05-16.md §7.3](spec-app-audit-2026-05-16.md). No new daemon, no new sudoers grant beyond the existing pickup-inbox rule.

#### 11.3.2 Input bundle

```jsonc
{
  "request_id": "repair-<8hex>",
  "app_id":     "journal",
  "findings":   [
    {"finding_id": "...", "source": "reconciliation | coherence",
     "kind": "missing_cron", "evidence": {...}}
  ],
  "operator_rationale": "the 6pm trigger stopped firing last week",
  "operator_intent":    "restore",   // or "remove" or "investigate"
  "context": {
    "manifest_snapshot": {...},   // full manifest including provenance + reconciliation + coherence
    "recent_trail":      [...],   // last 30 days, structural events only
    "last_test_run":     {...},
    "last_audit":        {...}
  }
}
```

`operator_intent` is captured at click time when the modal has direction options (e.g., coherence repair, §11.4). For straightforward findings the modal omits the picker and intent is implied.

#### 11.3.3 Prompt frame

The repair session runs in the bot's audit-runner, dispatched to the bot's configured `audit_tier` model. Prompt sketch:

> *"You are repairing an app whose manifest and implementation have drifted. The operator has reviewed the findings below and asked for repair. For each finding:*
>
> *1. If the fix is mechanical — reinstall a cron from manifest spec, re-embed a heartbeat section from canonical text, restore a file from git history, update a sha after operator-approved code change — apply it directly. Allowed transformations are listed below.*
> *2. If the fix is design-level — manifest claim has no implementation, code rotted into mismatch with claim, an integration needs reconfiguring — emit a Proposal explaining what would change and why.*
> *3. Stay within the manifest's scope. Don't redesign the app — close the specific gap the operator flagged.*
> *4. Honor the operator's stated intent."*

#### 11.3.4 Allowed mechanical transformations

Clicking Repair is the operator's pre-approval for these — no further confirmation needed:

- Reinstall cron from `manifest.crons[*]` spec (deterministic — the manifest is the spec)
- Re-embed heartbeat section from `manifest.scheduled_actions[*].trigger` canonical text
- Restore file from git history at the manifest's stored sha (when backup available)
- Update `manifest.files[*].sha256` to new value when sha drift was approved
- Remove crontab entries the manifest doesn't claim (only when `crons[]` provenance is authored)
- Update `manifest.files[*].path` after a rename (file at new path, sha matches old entry)

Anything outside this list emits a Proposal. The list intentionally overlaps with [spec-app-audit-2026-05-16.md §5.2](spec-app-audit-2026-05-16.md) — same safe-transformation discipline.

#### 11.3.5 Always-Propose, never-auto-apply

- Writing new code files (scaffolding for an `add_mechanism` coherence repair is a Proposal, not auto-apply)
- Modifying existing code beyond `interface_contract`
- Changing integration credentials
- Removing user data (anything in `data` or `state` layer)
- Cross-app changes (existing §5.6 doctrine)
- Manifest description / identity / claim text changes (operator approves through manifest editor, not via repair session)

#### 11.3.6 Output

Same shape as Tier 3 — outbox record with `applied_transformations[]` and `proposals[]`. Admin's poller ingests both: applied transformations write `repair_applied` trail entries; proposals land in the arbiter with `motivating_signals[]` linking the finding.

UI: Repair button enters a "repairing…" state with a spinner; admin's poller updates the row when the session completes. Typical wait: 15–60s for a single finding, up to 3 min for "repair all on this app."

#### 11.3.7 Rate limits and cost

- 3 repair sessions per app per day (configurable per-app via `repair_cadence_limit`)
- ~5k tokens single-finding session, ~15k for "repair all on this app"
- Uses the bot's configured `audit_tier` (defaults to tier2)
- Counts against the bot's daily token budget — repair fails fast with a clear message if the bot is over budget

### 11.4 Coherence repair — two-step direction

Coherence findings have multiple valid repair directions that change what the app *means*. Example: description claims "daily briefing" with no trigger. Three valid fixes, each meaning something different:

1. **Add the missing mechanism** — create a scheduled_action and supporting code
2. **Modify the claim** — rewrite description so it no longer claims a daily briefing
3. **Accept** — "we know this is aspirational; not flagging again"

A one-step Repair would force the LLM to pick a direction, possibly wrong. Coherence repair is **two-step**:

1. Operator clicks Repair on the coherence chip → modal opens with the three direction options + a one-line explanation of each
2. Operator picks a direction (stored as `operator_intent` in the bundle)
3. Branches:
   - **Add mechanism** → repair session dispatches with intent = `add_mechanism`; LLM proposes the addition. Scaffolding stubs (empty script + scheduled_action entry) may auto-apply if the mechanism is fully derivable; real code changes always Propose.
   - **Modify claim** → no LLM session; manifest editor opens scrolled to the offending field for the operator to rewrite. Save promotes the field to `user_authored`.
   - **Accept** → write signature to `coherence_accepted[]`; chip dismisses.

This preserves operator agency — the LLM isn't picking the direction, just executing the chosen one.

### 11.5 The app changelog

The changelog is the silent surface for observational manifests and the historical record for authored ones. Logs **structural and substantive** changes; suppresses data churn.

#### 11.5.1 What gets logged

| Event | Logged? |
|---|---|
| New file in `code`, `config`, `contract`, `behavior_doc` layer | Yes |
| Removed file in same layers | Yes |
| Sha drift on `code`/`config`/`contract` | Yes |
| New / removed cron entry | Yes |
| New / removed `scheduled_actions[*]` | Yes |
| New / removed `requirements.integrations[*]` | Yes |
| New / removed `volatile_paths[]` entry | Yes (contract addition) |
| Provenance change (any field) | Yes |
| Operator decision (Approve / Repair / Defer / Promote) | Yes |
| Repair session result (applied / proposed / failed) | Yes |
| Manifest field edit via UI editor | Yes |
| New file in `data`, `log`, `state`, `content` layer | **No** (this is the noise) |
| File count growth/shrinkage in a `volatile_paths[]` glob | Only if it crosses Pass B's anomaly threshold |
| Cron firing successfully | No (that's audit-run telemetry, not contract evolution) |
| Audit run summary | Yes (existing trail kind) |

The filter principle: **log when something that COULD become a contract appears, disappears, or changes.** Don't log data flow.

#### 11.5.2 Where it lives

Extend the existing per-app trail at `/Users/<bot>/.openclaw/workspace/evolve/audits/<app_id>/trail.jsonl`. The trail already records `audit_run`, `mark_accepted`, `auto_fix`, `conflict_notice`, `run_failed`. New trail entry kinds:

- `manifest_change` — generic catch-all with `{field, change, before, after}`
- `structural_addition` — `{kind: "file|cron|action|integration|volatile_path", id, layer?}`
- `structural_removal` — same shape
- `provenance_change` — `{field, from, to, by}` (already in §4.7)
- `decision_recorded` — `{finding_id, decision, by, rationale?}`
- `repair_applied` — `{request_id, transformations[], proposals[]}`
- `repair_failed` — `{request_id, error}`

Same retention as existing trail (365 days).

#### 11.5.3 Changelog view

Same trail viewer modal extended with a "Changelog" filter mode that hides `audit_run` and other operational telemetry, leaving the structural + decision events. Default sort: newest first. Operator can scroll history, filter by kind or date range, deep-link to a specific event.

A "Compare to N days ago" button summarizes the structural diff between two manifest snapshots — useful for "what's evolved on this app this month?"

#### 11.5.4 The changelog as observational-approval substitute

For observational manifests, the changelog is the operator's primary surface for understanding what's changed. No chip flow because there's nothing to approve. The operator opens the changelog when they want to know "what's the bot been up to with the journal app?"

For authored manifests, the changelog is the historical record alongside the chip-driven decision flow. Chip resolves a current divergence; trail records what happened.

### 11.6 Defer / snooze semantics

Defer is the "not now" path. Snooze options:

- 1 day
- 1 week
- Until next scan
- Until I ask (indefinite — equivalent to dismiss without resolution)

Snoozed chips don't appear on the Apps page until they re-surface. The operator can view snoozed findings via a "Snoozed (N)" link on the modal.

When snoozed time elapses, the chip re-flashes only if the underlying signature still exists. If the divergence resolved itself in the meantime (e.g., cron restored by deploy, file reappeared), the chip silently disappears with a `decision_recorded` trail entry noting `auto_resolved_during_snooze`.

A finding snoozed three times consecutively auto-promotes to `reconciliation_accepted[]` (or `coherence_accepted[]`) with a `snooze_count` annotation. The operator's repeated deferral is a strong signal that the finding isn't worth their attention.

### 11.7 Notification rules for repair completions

When a repair session finishes:

- **Bot user gets notified** (via the bot's normal messaging channel) when the user originated the repair via evo. Reply shape: *"Repair done on journal. Reinstalled the 6pm cron. View the changelog: [link]."*
- **Operator gets notified** when repair surfaced new Proposals that need their decision (admin notification queue, same channel as forge-complete).
- **Both get notified** on a `repair_failed` outcome with the error summary.

The notification carries the trail link and the proposal links so the recipient can drill in without hunting.

---

## 12. Build plan

Thirteen PRs, sized to ship one at a time. Each PR is independently revertable; later PRs depend on earlier schema work but not on earlier UI work.

**PR 1 — Schema v18 (foundation).** Migrate manifest schema: add `layer` to `files[*]` (required, default `"unknown"` for legacy entries), add `volatile_paths[]`, add `reconciliation` block, add `coherence` block, add `provenance` block with `field_origins{}`. Update `migrate_manifest` in `applications/manifest.py`. Migration default: every existing field gets `field_origins.<field>.source = "observational"` (safe default). No behavior change — fields are populated but nothing reads them yet. ~400 LOC + tests.

**PR 2 — Provenance write paths.** Stamp `field_origins` from every existing write path: manifest editor save (→ `user_authored`), forge build (→ `forge_built`), scanner (→ `observational` for new fields, leave existing fields alone), evo handlers that modify manifests (→ `bot_authored`). Trail entries on every provenance change. ~300 LOC + tests. Independent of PR 3+ — provenance is recorded even if no reader uses it yet.

**PR 3 — Layer classifier.** `applications/layer_classifier.py` with the rules-first cascade (§3.5). Pure Python. Scanner Phase 5.5 invokes it on every `files[*]` entry without a layer. LLM fallback gated behind a flag (off by default in PR 3; turned on in PR 4 once we trust the rules). ~400 LOC + classifier unit tests.

**PR 4 — Scanner reconciliation pass (provenance-aware).** New Phase 6 (§7.4) and Phase 7 (Pass A invocation). Writes `reconciliation` and `coherence` blocks. Crucially: **observational fields silently update; authored fields stage**. Reads existing blocks on re-scan to avoid re-flashing. Enables LLM fallback in the classifier. ~600 LOC + integration tests covering the provenance behavior.

**PR 5 — Tier 2 layer-aware severity + provenance-aware emission + Pass A + Pass B.** Existing `audit_runner.py` extended with layer-aware severity table (§3.2), provenance-gated Signal emission (§8.1), Coherence Pass A (§6.1), Pass B volatile-growth + integration-credential (§6.2). Reconciliation writes back to manifest, respecting provenance. ~700 LOC + audit runner tests.

**PR 6 — Change detector + scan scheduling.** New `applications/change_detector.py` (§7.6.1) runs every Tier 2 tick; writes `.detector_decision.json`. Scanner gains `--targeted <app>` and `--quiet-window-only` modes. Snapshot file at `{shared_dir}/applications/{bot_id}/.last_scan_snapshot.json` written at the end of every scan. Quiet-window scheduler folded into the existing admin tick. Pod settings UI for scan triggers (§7.6.8). Per-bot config in `network.json → app_scan`. ~700 LOC + detector unit tests + e2e scan-scoping tests.

**PR 7 — Tier 1 quick scan + escalation routing.** New `applications/scan_router.py` (§7.7.2 decision matrix). New scanner mode `--tier1-quick` (§7.7.3 prompt + output schema). Manifest-patch applier honors provenance (observational silently; authored stages chips). Escalation handler dispatches Tier 3 follow-up (§7.7.4) for `manifest_regen` / `discovery` / `coherence_check` targets. Tier 1 model resolution from `network.json → app_scan.tier1_model`. ~700 LOC + tier-1 prompt regression tests + escalation routing tests.

**PR 8 — Pre-deploy coherence gate.** Forge dispatch runs Pass A before approval; rejects (or warns, per §6.6) if `incoherent`. Manifest editor save button respects Pass A. ~300 LOC.

**PR 9 — Apps page UI: reconciliation modal + chip vocabulary.** New chips (§5.2), four-tab modal (§10.3). Per-file/per-trigger Approve / Defer / Promote actions write through the existing manifest-write helpers. **Repair button is wired but shows "Repair coming in PR 10"** placeholder until repair sessions land. "Observational" badge on apps with no authored fields. ~900 LOC + UI tests.

**PR 10 — Repair sessions (backend + UI activation).** New `audit_runner.py --repair` mode (§11.3.3 prompt, §11.3.4 allowed transformations). Admin-side dispatch helper (`applications/repair_dispatch.py`). Outbox poller extended for `repair_applied` / `repair_failed` records. Activates Repair button in the modal from PR 9. Two-step direction picker for coherence repair (§11.4). New `evo app-repair <app> [<finding>]` command. Per-app rate limit (§11.3.7). ~900 LOC + repair runner tests + e2e tests with synthetic findings.

**PR 11 — App changelog.** New trail entry kinds (§11.5.2). Audit runner and scanner write `manifest_change` / `structural_addition` / `structural_removal` / `decision_recorded` entries on appropriate events. Trail viewer modal gains a "Changelog" filter mode (§11.5.3). "Compare to N days ago" diff view. "Changelog" link on Apps page tile. ~500 LOC + viewer tests.

**PR 12 — Bot-side awareness.** Session-start surface gets an "Apps" block (§10.9.1) with current findings on this bot's apps; refresher rides on the existing session_surface mechanism. `signal_notifier._DEFAULT_PRODUCERS` gains app-finding signature patterns (§10.9.3). Proactive notification dispatch composes user-facing messages from finding `human_title`. Heartbeat injection for time-sensitive findings (§10.9.2). Per-bot opt-in config: `network.json → bots.<id>.app_finding_notifications`. ~600 LOC + e2e test with synthetic finding → bot message → user reply → repair dispatch.

**PR 13 — evo conversational reconciliation.** `evo app-changes <app>`, `evo app-coherence <app>`, `evo app-scan <bot>` grammar (§10.4 + §7.6.6). Handler in `evo/handlers/app_changes.py`. Natural-language repair intents (§10.9.4) recognized via prompt-side wiring. Notification routing per §10.5 + §11.7. ~600 LOC + handler tests.

**PR 14 — Orphan handling.** Hysteresis detection (§9), Retired section on Apps page, provenance-aware notification (loud for authored, silent for observational). Reinstall/Archive/Convert actions. ~400 LOC.

**PR 15 — Coherence Pass C1 (static analysis).** Weekly invocation inside Tier 2 runner. AST parse + import graph + heuristic check. ~600 LOC + fixture tests.

**PR 16 — Coherence Pass C2 + C3.** Tier 3 prompt gains coherence section (Pass C2). `--coherence-check` runner mode (Pass C3). Admin-side dispatch helper. "Check coherence" UI button. Rate limit + token cap. ~700 LOC.

**Total surface:** ~9,300 LOC across 16 PRs. Roughly 13–15 weeks of focused work assuming a single agent per PR. PRs 1–7 are the load-bearing minimum for the silent infrastructure; PR 10 (repair) and PR 12 (bot-side awareness) are the two PRs that make the system *useful to bot users* rather than just admins. If the goal is "bot users see and fix their own app issues conversationally," ship through PR 13; chips and modals (PRs 8–9) and orphan/coherence-LLM (PRs 14–16) can come later or never depending on admin needs.

### 12.1 Sequencing rationale

- PR 1 (schema) and PR 2 (provenance writes) ship first so every subsequent change can rely on provenance being recorded. Without these, all later PRs would need to guess at field origin.
- PR 4 (scanner reconciliation) requires PR 1+2+3 — it needs the schema, the provenance read, and the classifier.
- PR 5 (Tier 2 changes) requires PR 1+2 only — independent of the classifier in PR 3 because Tier 2 reads layers but doesn't classify.
- PR 6 (change detector) requires PR 1–4. It uses provenance to decide which fields are observational vs authored, the classifier to layer new files, and reconciliation infrastructure to know what's claimed. Without PR 6, scanner cadence falls back to manual-only.
- PR 7 (tier 1 quick scan) requires PR 6. The router can ship before tier 1 is wired (everything routes to tier 3 by default), but tier 1 is what makes event-driven cost actually low. Without PR 7, every flagged scan goes Tier 3 — functionally fine, ~10× more expensive.
- PR 8 (pre-deploy gate) is small and independent of PR 6+7; could ship in any order.
- PR 9 (UI) can ship anytime after PR 5+6+7; UI is the primary user-visible surface but the system is functional without it. Ships with Repair placeholders so the modal is usable for Approve/Defer immediately.
- PR 10 (repair sessions) is the load-bearing behavior addition. Independent of PR 9's UI rendering — once PR 9 ships the placeholder buttons, PR 10 activates them.
- PR 11 (changelog) is independent of PR 9+10 — backend trail writes happen even if no viewer is shipped (forensic logs accumulate). The viewer ships in this PR.
- PR 12 (bot-side awareness) is the load-bearing addition for actually making the system useful to bot users. Depends on PR 5 (findings exist) and PR 10 (repair dispatchable). The Apps page UI (PR 9) is the admin's surface; PR 12 is the user's. Both should ship.
- PR 13 (evo) is independent of PR 9+10 — parallel command-grammar surface for users who prefer explicit commands over natural language.
- PR 14 (orphan) can ship anytime after PR 5.
- PRs 15–16 (C1/C2/C3 LLM coherence) are pure additive; defer until cheap passes prove their value.

---

## 13. Migration plan

### 13.1 Schema migration (PR 1)

- Schema v18 lands. `migrate_manifest` populates `layer: "unknown"` on every existing `files[*]` entry, empty `volatile_paths`/`reconciliation`/`coherence` blocks, and `provenance.field_origins` with every existing top-level field stamped `observational`.
- **Critical migration decision:** existing manifests are treated as fully observational by default. This is the safe choice — the alternative (treating legacy manifests as authored) would cause every scanner re-run after PR 4 to fire chips on every observational drift. Operators promote what they want on contract via the manifest editor or "Mark app as ready" button (PR 7).
- An exception: manifests with `forge_built_at` already set (existing field on forge-produced manifests) get `field_origins.*.source = "forge_built"` on migration. Forge-built apps are already authored by definition.

### 13.2 Layer classification (PR 3 + PR 4)

- First scan after PR 4 ships runs the classifier on every existing manifest. Apps page shows a "needs re-scan" pill for any bot whose latest scan predates PR 4.
- Classifier confidence is reported per file; entries that survived to LLM fallback are stamped with a `classification_method: "llm"` marker so the operator can review.
- Operators can correct any layer via the manifest editor; corrections are sticky (subsequent classification respects the manual override).

### 13.3 Tier 2 layer-aware severity (PR 5)

- Existing Tier 2 Signals continue to fire; severity is recomputed per the §3.2 table.
- **Crucially:** because all legacy manifests start as fully observational (§13.1), the first few Tier 2 runs after PR 5 will produce ZERO Signal escalations from reconciliation deltas. This is intentional and desirable — operators see no alert flood, they have time to review and promote what they want on contract.
- Coherence-Pass-A findings DO fire from day one (coherence is provenance-independent) but are wrapped in calibration mode for the first 30 days, suppressing escalation to alert tier.

### 13.4 The "Mark app as ready" rollout

PR 7 adds the "Mark app as ready" affordance on the Apps page. Operator workflow for the first 30 days after PR 7:

1. Apps page renders all existing apps with "Observational" badge.
2. Operator reviews each app's manifest, edits anything they want to put on contract (description, scheduled_actions, etc.) — editor save promotes those fields to `user_authored`.
3. When the operator is satisfied with the manifest as a whole, clicks "Mark app as ready" — promotes remaining observational fields to `confirmed`.
4. After this, future scanner runs will produce chips for drift on the now-authored fields.

Operators who never click "Mark app as ready" continue to operate in fully observational mode indefinitely. That's a valid posture — the app is tracked but not under contract. No pressure to migrate.

### 13.5 `volatile_paths[]` adoption

- Scanner proposes `volatile_paths[]` via the reconciliation chip flow for authored manifests. For observational manifests, scanner writes them directly.
- Operators see the inferred entries when they're considering promotion — "before you mark this app as ready, here are the volatile directories the scanner found; review and accept."
- A help-page entry walks the operator through their first `volatile_paths[]` adoption.

### 13.6 Coherence rollout

- PR 5 ships Pass A on every Tier 2 run with calibration mode suppressing the new "Incoherent" chip for 30 days. After 30 days, the chip appears.
- PR 6's pre-deploy gate ships in **warning mode** for the first 30 days (logged but not blocking) so operators see the rejection rate before it bites them. Configurable per `pre_deploy_gate_mode`.
- PRs 10–11 (C1/C2/C3) ship under calibration mode by default — findings go to trail, not Proposals, for the first cadence cycle (~60 days for monthly Tier 3).

### 13.7 Forge updates

PR 1's schema change implies forge needs to emit:
- `layer` on every `files[*]` entry (forge knows what it built, so this is mechanical).
- `volatile_paths[]` for any directory the build spec declares as data/content/log.
- `provenance.field_origins.*.source = "forge_built"` on every materialized field.
- A coherence-clean manifest by construction (PR 6's gate enforces this).

Forge prompt updates ride along with PR 6.

### 13.8 No bulk migration script

Apps clean themselves up organically as operators work through chips and click "Mark app as ready" on the apps they want under contract. No batch migration is required, and operators can take this at their own pace.

### 13.9 First-run sequence on existing pods

The migration above describes each PR's individual impact. This subsection traces the behavioral sequence the first time the new infrastructure runs end-to-end on existing pre-v18 data. Assumes a typical pod: 10 bots, 5–15 apps per bot, manifests at v17, several apps with no recent scan.

**T = PR 1 ships.** `migrate_manifest` runs on every existing manifest at next admin tick.
- Each manifest gains empty `volatile_paths[]`, `reconciliation`, `coherence`, `provenance` blocks.
- Every existing top-level field gets `provenance.field_origins.<field>.source = "observational"` (the safe default per §13.1).
- Forge-built manifests with `forge_built_at` set: fields that forge materialized get `forge_built` provenance.
- Existing `files[*]` entries get `layer: "unknown"`.
- No behavior change — new fields populated but nothing reads them yet.

**T = PR 2.** Write paths active.
- New scanner runs stamp `observational` on new fields.
- New manifest editor saves stamp `user_authored`.
- New forge runs stamp `forge_built`.
- Existing manifests unchanged until touched.

**T = PR 3.** Classifier module loaded but not invoked by anyone yet.

**T = PR 4.** Scanner reconciliation pass ships.
- Next manual scan on any bot runs Phase 5.5 (classifier) on every `files[*]` entry whose `layer == "unknown"`.
- Rules-first cascade handles most files; LLM fallback handles the residual.
- Phase 6 reconciliation pass runs — but **every field is `observational`**, so all deltas silently update. No chips. No Signals.
- Phase 7 coherence Pass A runs on every manifest. **This is where the first findings appear** — pre-existing incoherences (manifests claiming behaviors with no matching mechanism) surface as `coherence.findings[]`.
- The 30-day calibration suppression (existing flag per §13.6) keeps coherence findings out of the alert tier; admins see them on the Apps page (PR 9) but they don't fire notifications.

**T = PR 5.** Tier 2 audit runner extended with Pass A + Pass B + layer-aware severity.
- Tier 2 every 6h now writes `manifest.reconciliation` and `manifest.coherence` for every app.
- Observational changes → silent. Only coherence findings produce visible state.

**T = PR 6.** Change detector ships.
- First Tier 2 tick after PR 6: detector runs on each bot, finds **no snapshot file** → treats as first run → schedules a full discovery scan for next quiet window (default 2 AM).
- That night: every bot gets a full discovery scan, paced one-at-a-time so the LLM dispatch doesn't spike.
- Cost: ~$0.40 × 10 bots = **~$4 one-time**.
- Snapshot files written; detector now has a baseline.

**T = PR 7.** Tier 1 quick scan + scan router.
- Subsequent scans tier appropriately. Routine changes → Tier 1 (~$0.005); only escalations or new-app discovery hit Tier 3.
- Steady-state cost drops to event-driven baseline (§7.7.7).

**T = PR 8 through PR 16.** UI, repair, bot-side awareness, etc.
- Ship in order; each adds capability without disrupting the steady state.

**Steady state, post-migration.**
- Most apps live observationally with `reconciliation.status = "ok"`.
- Some apps have `coherence.findings[]` from legacy descriptions (claims that no longer match reality).
- Admin's Day-1 work: review coherence findings via the Apps page (PR 9). For each: either fix the manifest (description rewrite — promotes that field to `user_authored`), accept the finding (adds to `coherence_accepted[]`), or repair the implementation (dispatches a repair session).
- Admin's ongoing work: minimal. Change detector catches new development; Tier 1 patches routinely; admin only sees chips when promoted apps drift.

**What the operator doesn't have to do:**
- No bulk promotion. Apps stay observational unless the admin specifically promotes.
- No "approve every observational change." That's why provenance exists.
- No retroactive manifest editing for legacy apps unless something's actually wrong.

**What the bot user notices (or doesn't):**
- For healthy apps: nothing. The session-start "Apps" block (§10.9.1) is absent.
- For apps with coherence findings: bot has context but only mentions if relevant.
- For apps that develop critical findings post-migration: proactive notification per §10.9.3 — if the operator has opted in for that bot (§10.9.5).

**The migration is asymptotically silent** — no explicit milestone the operator needs to drive. The system goes from "pre-v20" to "fully operational v20" through normal use, without a migration day, without a bulk-promote workflow, without prompts that force operator attention.

### 13.10 Storage unification

Per §17.1, manifests today live in two places depending on the bot:
- Canonical (admin-readable): `{shared_dir}/applications/{bot_id}/`
- Bot-side working copy: `/Users/<bot>/.openclaw/workspace/manifests/`

Some bots (personal-bot) only have bot-side; others (evolve) only have shared-dir; others (likely a mix going forward) have both. v20 unifies on shared-dir as the canonical location with the workspace as a read-only mirror.

**Migration steps (PR 4 ships these alongside the reconciliation pass):**

1. **First Tier 2 tick on each bot:** the runner enumerates manifests at both paths.
2. **If a manifest exists only at workspace path:** copy to shared-dir (atomic write); trail entry `manifest_migrated_to_shared`. Leaves workspace copy in place untouched.
3. **If a manifest exists at both paths and content differs:** shared-dir wins (it's canonical); workspace copy is overwritten with shared-dir content; trail entry `manifest_resync`. **Open question if conflicts are common** — if real-world divergence is widespread, this needs operator-visible reconciliation chip instead of silent overwrite. PR 4 includes a count metric to surface this.
4. **Going forward:** all manifest writes go to shared-dir; on every shared-dir write, a sync hook mirrors to workspace (so bot-side tools that read workspace manifests still see current state).
5. **Future PR:** once the workspace mirror is unused, remove the sync hook. Not in v20.

Bot-side reads (audit runner, repair runner) continue reading from workspace because the existing ACL is set up for that. Writes converge on shared-dir.

**For personal-bot specifically:** the 23 workspace manifests get copied to `{shared_dir}/applications/personal-bot/` on first Tier 2 tick after PR 4. ~3 KB × 23 = ~70 KB of disk; pure file copy; takes milliseconds.

---

## 14. Cost summary

Per-app per-week steady-state cost in the v18 world (assuming 15 apps on a typical bot, mix of stable and active):

| Pass | Frequency | Cost per app per week | Per pod (15 apps × 10 bots) |
|---|---|---|---|
| Layer classifier | Per-file, per scan | $0 (rules-first; LLM fallback rare) | $0 |
| Provenance writes | On-edit only | $0 | $0 |
| Change detector | Tier 2 (28×/wk @ 6h) | $0 | $0 |
| Reconciliation | Tier 2 (28×/wk) | $0 | $0 |
| Coherence Pass A | Tier 2 (28×/wk) | $0 | $0 |
| Coherence Pass B | Tier 2 (28×/wk) | $0 | $0 |
| Coherence Pass C1 | Weekly | $0 | $0 |
| Tier 1 quick scan | When detector flags + router routes to Tier 1 | ~$0.005 (~2k tokens) | ~$1/wk pod typical |
| Tier 3 targeted scan (Tier 1 escalation or large change) | When Tier 1 escalates `manifest_regen` | ~$0.10 (~10k tokens) | ~$3/wk pod typical |
| Tier 3 discovery scan | When detector OR Tier 1 flags new-app discovery | ~$0.40 (~40k tokens) | ~$1/wk pod typical |
| Repair session | On-demand | ~$0.05–$0.15 per click | usage-driven |
| Coherence Pass C2 | Monthly (via Tier 3) | folds into Tier 3 | already budgeted |
| Coherence Pass C3 | On-demand or charter-change | ~$0.01 per invocation | ≤$0.10/wk |

**Pod-wide LLM cost** roughly: **$8–$25/pod/month** depending on activity (revised down from the pre-tier estimate). A pod with mostly stable apps and infrequent operator edits stays near $8; a pod with 2–3 actively developed bots gets to $25. Compare to the existing Tier 3 audit budget of ~$30–80/pod/month — this spec **adds modestly** to LLM spend on app-related work, traded for: silent reconciliation, intelligent scan scheduling, repair sessions, change detection, tiered execution.

The two disciplines that keep cost manageable:
1. **The change detector is free.** It's the gate that ensures any LLM scan only fires when there's something to look at.
2. **Tier 1 handles the bulk.** Cheap-model quick scans handle routine changes; Tier 3 only fires when Tier 1 escalates or the detector spots a likely new app. Without Tier 1, the same activity pattern would cost ~10× more (~$80/pod/month).

Without either discipline, naive "weekly Tier 3 scan of every bot" would cost ~$120/pod/month with no behavioral gain.

---

## 15. Open questions

1. **Per-entry provenance within list fields.** v1 is per-top-level-field. The common annoyance: operator hand-adds 2 files to `files[]` that the scanner originally discovered; the whole `files[]` field is now `mixed` and we'd want to know that the 2 are authored while the rest are observational. *Lean: ship v1 as per-field; revisit per-entry once we have data on how often mixed authorship comes up. The interim workaround is `volatile_paths[]` — most observational additions belong there anyway.*

2. **Implicit promotion via edit.** When the operator edits one part of a top-level field (e.g., changes one entry in `scheduled_actions[]`), does the whole field get promoted to `user_authored`? *Lean: yes — per-field granularity means an edit promotes the whole field. If this turns out too aggressive, per-entry provenance (above) is the fix.*

3. **"Observational" badge visibility.** When most of an operator's apps are observational, the badge becomes noise. *Lean: show the badge only when the operator hovers a "What's this?" tooltip on the Apps page header, OR when filtering by "Observational" via a header filter chip. Don't show on every tile.*

4. **Bot-user-driven promotion.** `evo app-changes <app> promote` lets the bot user promote fields. Should this also notify the operator? *Lean: yes for team bots (operator should know what's been promoted on team bots), no for personal bots (same person, no notification needed).*

5. **Provenance for legacy migrations.** Existing forge-built manifests don't carry `forge_built_at` consistently. Some pre-spec forge runs may not have stamped that field. *Lean: at migration, if the manifest has `build_spec` populated, assume forge_built; otherwise observational. Operator can correct via "Mark app as ready" anyway.*

6. **Layer classifier LLM fallback model tier.** Closed-form classification with 9 labels. *Lean: tier1 / haiku-class.* Override per-bot if needed.

7. **`volatile_growth_anomalies` detection window.** Pass B's growth check needs a comparison window. *Lean: 14 days for `bursty`, 7 days for everything else.*

8. **Manifest editor save-blocking.** Pass A is fast enough to run inline on every keystroke debounce. *Lean: on save attempt only, to avoid "why is the save button red, what did I just type."*

9. **Re-scan suppression for promoted-but-drifted fields.** Operator promotes a field, scanner re-runs, finds drift, chip fires. Operator dismisses the chip without repair. Next scan: chip re-fires (same signature). At what point does the system stop bothering? *Lean: after 2 dismissals, the signature auto-writes to `reconciliation_accepted[]`. Recoverable via "full reconcile."*

10. **Cross-app coherence — out of scope.** App A's outputs vs app B's inputs as a graph check. Real-world cross-app contract drift has not been observed enough to validate this is a problem worth solving. *Decision: drop. The `app_dependencies[]` field exists in the schema today but won't be validated by this spec. Revisit only if a concrete failure surfaces that this check would have caught.*

11. **Layout normalizer generator (deferred).** Proposing file movement to match canonical layout. Out of scope here. *Lean: separate spec once we see how noisy reconciliation is on real legacy apps.*

12. **The weekly digest format.** §10.2 mentions a weekly digest for observational changes. Format and channel? *Lean: simple text summary delivered via the existing notification queue (same path as forge-complete notifications). Opt-in via the pod settings page.*

13. **Change detector threshold for "new app likely."** §7.6.2 sets ≥3 unattributable files as the discovery trigger. Real-world calibration TBD — too low and we run wasteful discovery scans on transient files; too high and we miss small apps. *Lean: 3 files OR 1 unattributable code-layer file >24h old. Tunable per-bot.*

14. **Detector snapshot file location.** §7.6.1 puts it at `{shared_dir}/applications/{bot_id}/.last_scan_snapshot.json`. This is in shared_dir (admin-owned ACL); should it be bot-side instead so the bot's own runner can update it without admin involvement? *Lean: shared_dir for now since the scanner already writes there; revisit if bot-side updates become needed.*

15. **Quiet-window scheduler ownership.** §7.6.4 says it folds into the admin tick. Should it actually live in the bot's audit_runner so it survives admin downtime? *Lean: admin-side for v1 — scan dispatch already requires admin orchestration (LLM credentials, cost accounting). The bot's reconciliation runs independently in any case.*

16. **Scan-now vs scheduled-scan UX.** §7.6.6 shows the detector's state on the manual-scan button. Is this enough nudge to make operators trust the schedule? *Lean: ship and see — if operators always click "Scan now" anyway, we've signaled the schedule isn't legible enough.*

17. **Tier 1 escalation calibration.** §7.7.3's prompt says "when in doubt, escalate." If the calibration is wrong, Tier 1 either over-escalates (cost rises toward Tier-3-only) or under-escalates (manifest gets stale data Tier 1 patched incorrectly). We need a feedback loop. *Lean: log every Tier-1 verdict to the trail; weekly a tiny offline analysis compares Tier-1 `handled` outcomes against subsequent Tier 3 audit findings on the same files; if Tier 1's `handled` decisions are routinely rejected by later passes, tighten the prompt. Worth a follow-up spec once we have data.*

18. **Tier 2 (sonnet-class) as a middle tier.** §7.7 hops straight from Tier 1 to Tier 3. A middle tier could handle "needs more reasoning than haiku but isn't a full discovery." *Lean: ship without it; revisit if real workloads show a Tier-1-escalate-to-Tier-3 path that's dominated by cases a Tier 2 model could have closed. The complexity of a third tier isn't worth speculating about.*

19. **Manifest-patch schema for Tier 1 output.** §7.7.3's `op: "add" | "append" | "replace"` is a sketch. Should we use JSON Patch (RFC 6902) or a custom verb set? *Lean: custom verb set scoped to manifest fields — JSON Patch is general-purpose and over-permits operations we don't want Tier 1 doing (e.g., replacing whole sub-objects). Verbs we allow: `files.add`, `files.update_sha`, `crons.add`, `description.append`, `volatile_paths.add`, `requirements.integrations.add`. Anything else means escalate.*

20. **What happens when Tier 1 patches an authored field?** §7.7.3 says patches respect provenance — observational fields update silently, authored fields stage chips. But Tier 1 might propose a patch that would touch an authored field. *Lean: Tier 1's prompt explicitly forbids patching authored fields; if it tries, the router silently downgrades the patch to a reconciliation chip. Operator sees the chip; no surprise authored-field updates.*

21. **Session-start surface size cap.** §10.9.1's "Apps" block should be short to avoid context bloat. What's the cap? *Lean: 5 findings max, sorted by severity (critical first); when over, summarize as "5+ findings — say `evo app-changes` for full list." A bot with 10 critical findings is in trouble; the cap is a UX gate, not a forensic limit.*

22. **Bot natural-language repair recognition.** §10.9.4 lists conversational phrases the bot should recognize as repair intents. Should this be hand-tuned per-bot, or pod-wide via a shared prompt template? *Lean: pod-wide template that takes the bot's app list as input — keeps the recognition logic consistent across the pod and lets us improve it centrally.*

23. **Proactive notification frequency limit.** A bot with multiple findings shouldn't fire multiple notifications in a short window. *Lean: 1 notification per bot per hour, queue+coalesce within the window; the queued findings appear in the next message as a list. Use the existing message-batching infrastructure from `signal_notifier`.*

24. **Notification opt-in default.** Should `app_finding_notifications` default to on or off? *Lean: ON for personal bots (the user is the operator and wants to know); OFF for team bots (operator hasn't decided yet whether team members should see infra alerts); operator toggles per-bot in the bot's settings.*

25. **What if the bot says "fix it" when there's no clear repair target?** User says "fix the app" with multiple findings or no clear context. *Lean: bot asks which one ("you have 3 findings — which would you like me to look at?") rather than guessing. Errs toward conversation over action when ambiguous.*

26. **Session-block refresh mid-session.** Findings can change while the bot is in an active session (a Tier 2 tick fires, a repair completes elsewhere). Should the session_surface be refreshed mid-session, or only at next session start? *Lean: only at session start by default; for time-sensitive cases, heartbeat injection (§10.9.2) carries the update. Avoid invalidating the bot's in-flight context.*

27. **Member-reported escalation timeout.** §10.9.7 has team members escalate to the primary user. If the primary user doesn't reply within N hours, what happens? *Lean: 24h wait, then bot follows up to the member ("I haven't heard back from Diana — want me to flag this for the admin instead?"); 48h wait, then auto-escalate to admin as a low-priority Proposal. Avoids findings dying in the primary-user inbox.*

28. **First-run scan paced one-at-a-time or in parallel.** §13.9 says "paced" — what's the actual concurrency? *Lean: 2 bots concurrent max during first-run discovery, with a 30-second gap between bot starts to avoid LLM-dispatch spikes. Tunable via `app_scan.first_run_concurrency`.*

29. **First-run cost cap.** §13.9 estimates ~$4 for a 10-bot pod. Should there be a hard ceiling that defers some scans if the cumulative cost crosses it? *Lean: no hard cap for first run — it's one-time, predictable, and operators expect a baseline cost. A soft warning at $10/pod in the admin UI is enough.*

30. **Day-1 admin workflow surface.** §13.9 says admin's Day-1 work is reviewing coherence findings on the Apps page. Should there be a dedicated "post-migration review" landing page that surfaces just the legacy-incoherence findings, separate from ongoing coherence findings? *Lean: no — same Apps page, with a "first 30 days" badge on findings created during initial scans so the admin can distinguish if they want. Avoids building a one-time UI.*

31. **Manifest storage: unify or accept duality?** §17.1 surfaced that some bots have manifests at `/Users/<bot>/.openclaw/workspace/manifests/` and others at `{shared_dir}/applications/{bot_id}/`. Migration could either unify (always shared_dir, sync from workspace once) or accept both. *Lean: unify to `{shared_dir}/applications/{bot_id}/` as canonical (admin-readable, bot-writable via ACL). Migration in §13.10 reads bot-side workspace manifests once, writes to shared_dir, leaves bot-side as a working mirror (sync-back on every shared-dir write). Future writes go to shared_dir only; the workspace mirror is read-only from PR 4 onward.*

32. **Pass B assertion: openclaw cron Status check** — **the most important new assertion in this spec.** §17.3 (personal-bot) and §17.12 (team-bot-a) surfaced that this catches real production failures happening *right now* — 4 of team-bot-a's 6 crons are in `error` state including team-bot-a-task-worker (Slack delivery broken; the work succeeds but the operator never sees results). For each `manifest.crons[*]` or `manifest.scheduled_actions[*]` with `trigger.kind: "openclaw_cron"`, the assertion checks `openclaw cron runs --id <id> --limit 1` and flags any of: Status `error` (severity **critical** — the work is failing now), Status `skipped` >2 expected intervals (severity **major** — schedule fires but work defers), Status `ok` but `summary` contains failure-language patterns (e.g., "Message failed", "delivery attempted", "auth configuration") (severity **major** — the work nominally succeeded but a side-channel broke). *Decision: ship in PR 5; not optional. This single assertion provides more operator value on day 1 than any other check in the spec.*

33. **Use `confidence` to prioritize reconciliation chips?** §17.8 noted that existing v19 manifests carry a `confidence` field (0.75–0.95 in personal-bot's set). Could feed into chip prioritization — low-confidence apps deserve more operator review. *Lean: defer to a follow-up. Don't conflate "scanner's confidence in app identity" with "operator's confidence in manifest contract"; the former is mostly stable, the latter is what reconciliation is actually trying to track.*

34. **Instance-manifest file attribution.** §17.12 surfaced that team-bot-a's Instance manifests have empty `files[]` despite the heartbeat referencing concrete files like `pending_todos.json` and `maintenance/maintenance_tracker.py`. Should the classifier proactively attribute these files? *Lean: yes — when a manifest has `volatile_paths: []` and `files: []` but heartbeat content (or AGENTS.md, or scheduled_actions evidence) references workspace files in the bot's app domain, the classifier should propose those files as candidates for `files[]`. Surface as reconciliation `extra_files[]` for operator approval rather than auto-merging. The Instance pattern is currently invisible to attribution; this fixes it.*

35. **Channel-ID drift detection.** §17.12 surfaced that team-bot-a's heartbeat names 10 specific Slack DM channel IDs by hand. Team rotation invalidates these silently. Should coherence check that every channel-ID-shaped reference in heartbeat resolves to an active channel? *Lean: out of scope for v20. Would need Slack API access from the audit runner, which crosses an auth boundary. Possibly a follow-up substrate audit per [spec-audit-extensions-2026-05-17.md §4.1](spec-audit-extensions-2026-05-17.md).*

36. **`openclaw.json` heartbeat config as a detector input.** §17.13 (team-bot-c) surfaced that team-bot-c's config says heartbeat runs every hour but the doc says it's disabled — the config is reality, the doc is operator intent. Detector + Pass A must read both. *Lean: extend detector in PR 6 to include `(source="openclaw_config", id="heartbeat", schedule=<cadence>)` in its normalized cron source list; Pass A reconciles config vs HEARTBEAT.md state. Severity major if config and doc disagree.*

37. **Duplicate-mechanism detection (heartbeat + launchd / cron).** §17.13 (security-bot retirement story) surfaced that some work was running BOTH as heartbeat AND as launchd scripts — duplicate billing until retired. Pass A should surface duplicates as `minor` (might be safety_net intentional; might be wasteful). *Lean: ship in PR 5. Operator distinguishes intentional duplication by annotating `safety_net_for`. Without annotation, surface as info-level finding.*

38. **Two-user bot pattern (Evolve / evo).** §17.15 surfaced that Evolve runs under two macOS users — `evolve` owns the workspace, `evo` runs the gateway. The detector and audit runner must know which user owns which resource. *Lean: extend the bot-config schema with an explicit `runtime_user` field separate from the existing macOS-user mapping; default `runtime_user` to the workspace owner; Evolve gets `runtime_user: "evo"`. Detector queries openclaw cron list as the runtime_user (where the bot's API session lives) but reads workspace files as the workspace owner. Captured in `applications/user_resolution.py` (new module) — small, ~100 LOC.*

---

## 16. Summary of the doctrine

Seven rules that capture the spec in one paragraph each:

1. **A manifest's job depends on its provenance.** Observational manifests describe reality; the scanner updates them silently. Authored manifests express intent; drift from intent surfaces as chips. Neither is more correct than the other — they answer different questions.

2. **Most apps stay observational forever, and that's fine.** Promotion is an opt-in tool, not a workflow milestone. A pod can be healthy with 90% of its apps unpromoted. The system never nags operators to promote.

3. **File purpose matters more than file presence.** A missing journal entry isn't the same kind of problem as a missing script. Layer-typed severity makes the reconciliation rules behave correctly for both.

4. **Coherence is checked regardless of provenance.** Even an observational manifest is checked for "could this work?" — the answer matters even when nobody's vouched for the claims.

5. **The bot is the primary surface for bot users.** Findings reach the user through the bot's own conversation: the bot knows what's wrong, mentions it when relevant, alerts proactively on critical issues, accepts "fix it" via chat. The admin UI is the diagnostic surface admins visit when something needs deeper investigation.

6. **Don't burn expensive tokens on menial work.** A change detector decides whether to scan at all (free). A scan router decides whether the cheap model can handle it (Tier 1, ~$0.005) or whether it deserves the expensive model (Tier 3, ~$0.10–$0.40). Most events never reach Tier 3.

7. **The interface gets out of the way.** Silent when nothing requires a decision; conversational when the user already knows; modal when admin investigation is warranted. The system trusts that attention is limited and spends it carefully.

8. **Critical scheduled work gets two mechanisms.** Heartbeat is fragile to clobber; cron is opaque to silent delivery failure. Neither is more reliable than the other in practice (validation against 4 production bots found 13 of 15 scheduled jobs broken or silently skipped). The discipline: critical work has a primary mechanism that does it and a safety-net mechanism that verifies it. The `safety_net_for` field declares the relationship; coherence surfaces the absence as a soft warning. The single most operator-valuable assertion in this spec — Pass B's "openclaw cron Status check" — exists because of this principle.

9. **Match the trigger to the work's shape; never default blindly to either.** LLM-judgment / composite / context-aware work belongs in heartbeat. Deterministic / precise-time / no-LLM work belongs in cron or launchd. The 4-bot validation found neither pattern is more reliable in practice — the two newest design decisions in production (team-bot-c's heartbeat retirement, security-bot's full migration to launchd Python scripts) both moved AWAY from heartbeat. security-bot's lesson is the load-bearing one: *when work doesn't need LLM, moving it out of heartbeat saves real money and improves observability.* The spec encodes this narrowly — it surfaces duplicate-mechanism findings (heartbeat + cron for same work) so the operator can choose between safety-net (intentional) and duplicate-billing (wasteful). Authoring guidance for "which trigger to choose, when to retire from one to another" lives in `manifest-authoring-guide.md`.

---

## 17. Spec validation against personal-bot (2026-06-05)

Walked one of the production pod's bots — personal-bot, a member-role bot used by the operator via Telegram with 23 existing manifests at v19 schema — through the spec end to end. Findings below are organized by which spec section they affected; each finding includes the disposition (already addressed in this revision, deferred, or new open question).

### 17.1 Storage location reality

**Finding.** personal-bot's manifests live at `/Users/personal-bot/.openclaw/workspace/manifests/`, not at `{shared_dir}/applications/personal-bot/` (which doesn't exist). Other bots may use either or both — evolve has manifests at `{shared_dir}/applications/evolve/` but not in its workspace.

**Disposition.** The spec consistently references `{shared_dir}/applications/{bot_id}/` as the canonical location. In practice, both locations are in active use depending on the bot. The spec needs to acknowledge:
- The canonical location going forward IS `{shared_dir}/applications/{bot_id}/` (admin-readable via existing ACL).
- For bots whose manifests live in workspace, the migration in §13.1 must include a sync step: read the bot-side manifests, write to shared-dir, leave bot-side in place as a working copy.
- Repair sessions (§11.3) write to whichever location the bot's runtime expects.

**Action.** Add a §13.10 covering the storage migration. **Open question Q31** below covers whether to fully unify storage or accept the two-location reality permanently.

### 17.2 OpenClaw cron is the dominant scheduling mechanism

**Finding.** personal-bot has zero entries in `crontab -l`, only 2 entries in user-level `launchctl list`, but 5 active scheduled jobs in `openclaw cron list` — `security-bot-liveness-ping`, `gateway-selfheal`, `turn-collector-personal-bot`, `personal-bot-backup`, `macos-update-monday`. Before this validation, my spec's §7.6.2 detector would have flagged personal-bot's heartbeat as "claims scheduled behaviors with no mechanism" because it couldn't see openclaw cron.

**Disposition.** §7.6.1 updated in this revision to read all three cron sources. §3.4 added `openclaw_cron` to the `trigger.kind` enum.

### 17.3 Skipped-status crons are subtle quiet failures

**Finding.** personal-bot's 5 openclaw cron entries all show `Status: skipped` with `Last: 6h ago / 12h ago / etc.` — the schedules are firing but the work is being deferred (delivery `"not requested (not requested)"`). The cron exists, looks healthy, but nothing runs. This is exactly the "quiet failure" the spec wants to catch.

**Disposition.** Pass B (§6.2) already detects this indirectly via `volatile_growth_anomalies` (the cron's expected outputs aren't appearing). **Adding a direct check:** new Pass B assertion **"every claimed cron entry has Status != skipped in the last expected interval."** Captured in **Q32** below.

### 17.4 Legacy `scheduled_actions[]` over-extraction

**Finding.** personal-bot's `app-backup-system.json` has 10 `scheduled_actions[]` entries (should be 1 — the 3am daily backup). `app-marie-campaign-manager.json` has actions with `summary: "1. Read SOUL.md — who you are"` (the first line of an AGENTS.md section captured as if it were an action). All affected entries have `mechanism: "unknown"`.

**Disposition.** §3.4 added the `quality: "verified | extracted | suspect"` field. Migration auto-marks every entry with `mechanism == "unknown"` as `suspect`. Coherence Pass A respects the flag and does not fire findings on suspect entries. Operators can re-extract via fresh scan with the tightened prompt — pushes these from `suspect` to `extracted`.

### 17.5 Intentional disables aren't modeled

**Finding.** personal-bot's HEARTBEAT.md documents `protein-daily-checkin — DISABLED (re-enable when the operator returns from travel)`. The cron has been removed (not in openclaw cron list); the doc reference remains. Without modeling, coherence Pass A would fire "claims behavior, no mechanism" forever.

**Disposition.** §3.4 added `state: "active" | "disabled" | "paused"` to scheduled_actions. Pass A skips non-`active` states. `paused_until` reminders surface when the date elapses.

### 17.6 Layer mis-classification in existing manifests

**Finding.** personal-bot's existing stamper assigns `layer: "state"` to:
- `marie/AI_Policy_Research_Master.md` and 20 other Marie campaign content files (should be `content`)
- `HEARTBEAT.md` (should be `behavior_doc`)

The existing layer enum also uses `"script"` rather than `"code"`.

**Disposition.** §3.1 updated to map legacy `"script"` → `"code"` and to note PR 3's classifier re-classifies every existing entry rather than trusting legacy values. Without this, reconciliation severity would behave incorrectly on every Marie content file (firing chip-level "missing file" instead of "info" when content files come and go).

### 17.7 Magic commands as triggers

**Finding.** personal-bot's `MAGIC_COMMANDS.md` defines 6 single-word user-triggered behaviors (`status`, `blockers`, `tasks`, `team`, `help`, `evo`). These are app triggers but not on a schedule.

**Disposition.** §3.4 added `user_command` to `trigger.kind`. Magic commands now have a structured way to be captured as scheduled_actions[*] with `trigger.kind: "user_command"` and `trigger.command: "status"`. Pass A's recurring-behavior assertion (§6.1) ignores `user_command` triggers — they're event-driven, not scheduled.

### 17.8 Existing v19 fields to preserve

**Finding.** personal-bot's manifests carry fields not addressed in earlier spec drafts: `manifest_type`, `evidence_files[]`, `capability_tags[]`, `session_keywords[]`, `example_triggers[]`, `test_cases[]`, `confidence`. The `evidence_files` content is notably noisy (entries like `"directory: marie/"`, `"json: manifests/..."` mixed with paths).

**Disposition.** The migration is **purely additive**. v20 preserves all existing v19 fields unchanged. The noisy `evidence_files` is not used by any new assertion in this spec; it's advisory only. `example_triggers` and `test_cases` continue to be used by the existing test framework. `confidence` could feed into reconciliation chip prioritization but doesn't in v20. Noted in **Q33** for future evolution.

### 17.9 Cross-bot file sharing (informational)

**Finding.** personal-bot's AGENTS.md documents a `sudo cp <src> <dst> && sudo chown <user> <dst>` pattern for moving files between bots. This is cross-bot, not cross-app — files genuinely move between bot workspaces.

**Disposition.** Out of scope (matches earlier decision to drop cross-app coherence). Noted here for awareness only. If a future spec addresses cross-bot dependencies, it would need to consider how a file leaving bot A's workspace affects bot A's `files[]` (likely as a `data` or `content` layer outflow) and arriving in bot B's workspace creates an inbound observational entry.

### 17.10 What the spec gets right

Validating against personal-bot confirmed several design choices:
- **Provenance-based reconciliation** is exactly right. personal-bot's 23 manifests are all `source: "discovered"` → all observational → reconciliation correctly stays silent. The few authored fields (if any) would surface drift; the rest would update silently. Day-1 alert flood avoided.
- **Layer-aware severity** is necessary. Without it, every Marie content file appearing/disappearing would fire at uniform severity.
- **Tiered scanning** is needed. personal-bot has 23 apps; a monthly full Tier 3 re-scan would cost ~$5/month per bot when Tier 1 + targeted handles ~95% of routine changes for ~$0.30.
- **Bot-side awareness** matches personal-bot's reality. the operator doesn't routinely visit the admin UI; he talks to personal-bot in Telegram. The session-start "Apps" block + proactive notifications via `signal_notifier` is the natural surface.

### 17.11 New open questions surfaced

Added as Q31–Q33 in §15:

- **Q31:** Unify storage to shared_dir or accept bot-side + shared as parallel locations?
- **Q32:** Add a Pass B assertion for "every claimed cron has Status != error/skipped in last expected interval." **Promoted to load-bearing in this revision** after team-bot-a validation.
- **Q33:** Use existing `confidence` field to prioritize reconciliation chips (low confidence → operator review more aggressively).

### 17.12 Validation against team-bot-a (2026-06-05)

Walked team-bot-a — member-role Slack bot used by Palace Express team (10 named members, primary user the operator) with 10 Instance-shape manifests at v19 — through the spec end to end. Key findings:

**Real production failures the spec catches immediately.** team-bot-a's `openclaw cron list` shows 4 of 6 crons in `error` status:
- `team-bot-a-task-worker` (daily 9 AM): Slack API credentials missing in fallback messenger → the cron's work succeeds (2 expired pending_todos auto-pruned) but the report never reaches the operator. *"The Slack API credentials aren't configured in the fallback messenger... The system should use the native OpenClaw Slack integration to deliver this to the operator when it's available."*
- `healthcheck:update-st...` (weekly Sunday 9 AM): error
- `ping-calendar-monitor` (weekly Monday 9 AM): error
- `maintenance-weekly-re...` (weekly Friday noon): error

the operator doesn't know about any of these because the delivery channels themselves are broken — *the failure pattern is asymmetric: the work succeeds but the failure signal fails.* My spec's **Q32 Pass B assertion** (now promoted to load-bearing, §17.11) would surface all 4 within the next 6-hour Tier 2 tick. **This is the single most impactful finding in either validation.**

**Schema heterogeneity is real.** team-bot-a's manifests are Instance-shape (`i-XXXXX.json`) — they bind to Gallery Specs at `{shared_dir}/gallery/<spec_id>/`. personal-bot's are full-app shape (`app-XXX.json`). Both at v19 but with very different field populations: team-bot-a's Instance manifests have many empty arrays (`evidence_files: []`, `goals: []`, `example_triggers: []`) where personal-bot's are populated; team-bot-a's have `source_detail`, `satisfaction_score`, `improvement_history`, `tags`, `app_version`, `objective`, `owner`, `maintainers` that personal-bot's don't. **Disposition:** v20 migration must be **defensively additive** — never assume a field exists; tolerate empty/missing/nullable fields throughout. PR 1's schema migration covers this by using `dict.get(field, default)` patterns, not direct attribute access.

**team-bot-a's heartbeat is overstuffed and fragile.** Reading end to end: 2 embedded Python scripts (pending_todos + kit_follow_up tasks), 10 team-member DM channels checked individually, 3 maintenance Slack channels parsed with severity-tagged regex matching, triage-manager-handoff shift briefing, resolution cross-check across channels, sev-5 escalation rules, project-blocker check. **All in one heartbeat invocation.** If HEARTBEAT.md gets clobbered (which per project memory has happened to team-bot-a before — Apr 22 AGENTS.md truncation), team-bot-a silently stops being useful to the entire Palace Express maintenance operation. **Disposition:** §3.4's `safety_net_for` field + Pass A's minor-finding-on-heartbeat-only-critical-claim captures the doctrine.

**The "10 broken DM channels" risk pattern.** team-bot-a's heartbeat names 10 specific DM channels by ID (`D0AKX41HELU`, `D0AKQ3G5DEH`, ...). When team members rotate, channel IDs change. The heartbeat doesn't have a reliable mechanism to track this — it's a hand-maintained list. This isn't directly addressed by the current spec but **noted as a future gap**: coherence could include a check "every channel ID referenced in heartbeat resolves to an active Slack channel."

**Multi-user reality is the norm here, not a special case.** team-bot-a has 10 named team members (Peter, Elizabeth, Dan, Brent, Robert, Ping, Terran, Q, Justin, Dalila). The §10.9.7 authorization tiers ("reads open, writes escalate") apply to all of them. A team member at Palace Express saying "fix team-bot-a-task-worker" should escalate to the operator, not act directly.

**Instance-pattern manifests have missing file attribution.** team-bot-a's Instance manifests have empty `files[]` and empty `evidence_files[]`, but the heartbeat references many concrete files (`pending_todos.json`, `ops/tasks/unified_tasks/tasks.json`, `maintenance/maintenance_tracker.py`, etc.). The existing scanner's mapping from heartbeat content to file attribution didn't happen for team-bot-a. **Disposition:** PR 3's classifier must handle this case — when a manifest is sparse but heartbeat references files matching the app's domain, the file should be attributable and surfaced as a candidate for `files[]` entry. Coverage gap noted as **Q34** in §15.

**Two cron sources used together.** team-bot-a has openclaw crons (workspace-backup, drift-check, team-bot-a-task-worker, etc.) AND launchd jobs (`ai.openclaw.team-bot-a-selfheal`, `ai.openclaw.liveness-ping`, `ai.openclaw.log-trimmer`). The launchd jobs are Evolve infrastructure (gateway selfheal); the openclaw crons are app-level. **Disposition:** §7.6.1 already covers reading all three sources. Just confirming the union approach is correct.

### 17.13 Validation against team-bot-c and security-bot (2026-06-05)

Two more bots, two more pattern variations, and the most important data point against "default to heartbeat" yet.

**security-bot intentionally retired its heartbeat.** Verbatim from security-bot's HEARTBEAT.md: *"This file intentionally has no executable instructions. OpenClaw's heartbeat path skips the API call when HEARTBEAT.md is empty or comment-only... security-bot's prior LLM-driven audit checklist is now fully covered in pure Python by [6 launchd scripts]. Re-running the same work via an isolated Haiku heartbeat session was duplicate billing."* The work moved to:
- `packages/analyzer/audit.py`
- `packages/analyzer/monitor_coverage.py`
- `packages/analyzer/cost_watchdog.py`
- `packages/analyzer/spend_alert.py`
- `packages/analyzer/backup_signal.py`
- `packages/analyzer/gateway_state_machine.py`

Each runs on its own launchd schedule and writes Signals directly to the Signal store. **This is the opposite direction from "default to heartbeat" — the team deliberately moved away from heartbeat because (1) the work didn't need LLM, (2) it duplicated billable LLM calls, (3) launchd schedules are more observable.**

**team-bot-c carries a manifest-vs-reality incoherence right now.** team-bot-c's HEARTBEAT.md declares *"Heartbeat is DISABLED (too frequent, wasteful)"* and replaces it with a single 3:30am cron. But team-bot-c's `openclaw.json` config still has `heartbeat: {every: "1h", model: "sonnet-4-6"}`. **The config says heartbeat runs every hour; the doc says it's disabled.** This is exactly the kind of incoherence the spec is designed to catch — and it's been sitting unnoticed in production. **Disposition:** detector must read `openclaw.json` heartbeat config; Pass A must reconcile config-claims against doc-claims.

**Two more broken crons.** security-bot's 2 openclaw crons (`security-bot-weekly-self-audit`, `security-bot-memory-archive`) are both in `error` status. team-bot-c's `dropbox-index-update` has been skipped for 4 days. **Running tally across 4 validation bots: 13 of 15 scheduled jobs broken or silently skipped.** Every one of these would surface within 6 hours under Q32.

### 17.14 Cron-vs-heartbeat doctrine — what the data actually shows

Four bots, four different patterns. **None of them default to heartbeat. The two newest design decisions both moved AWAY from heartbeat.**

| Bot | Heartbeat | Other scheduling | Posture |
|---|---|---|---|
| personal-bot | Active composite — 5 LLM checks (cost+blocker+gmail+calendar+backup) | 5 openclaw crons | Heartbeat-heavy; crons are infrastructure |
| team-bot-a | Overstuffed — 10 DM channels + 3 maintenance + Python embeds | 6 openclaw crons + 3 launchd | Heartbeat does most work; 4 crons broken |
| team-bot-c | Retired (HEARTBEAT_OK + minimal cost check) | 2 openclaw crons | Moved heartbeat → daily cron for cost |
| security-bot | Fully retired (comment-only file) | 2 openclaw crons + **6 launchd scripts** | Audit moved entirely to Python scripts |

**The principle the data supports:**

1. **Match the trigger to the work's shape.** LLM-judgment / composite / context-aware → heartbeat. Deterministic / precise-time / no-LLM → cron or launchd.
2. **When work doesn't need LLM, moving OUT of heartbeat saves money and improves observability.** security-bot's retirement is the canonical example.
3. **launchd is a first-class scheduling mechanism for non-LLM Python work.** security-bot's 6 audit scripts are launchd-scheduled. The detector must read launchd thoroughly, not just for gateway selfheal.
4. **Critical work gets a safety net regardless.** Single most-leveraged principle from §3.4.
5. **`openclaw.json` heartbeat config IS reality.** The doc-level "disabled" framing (team-bot-c) can lie. Detector must read both and reconcile.

What this spec encodes (narrow):

- Pass A reconciles `openclaw.json → heartbeat: {enabled, cadence, model}` against scheduled_actions claims about heartbeat triggers. Mismatch → finding.
- Pass A treats "comment-only HEARTBEAT.md" as a positive signal of intentional disable; manifest scheduled_actions claiming heartbeat trigger in this state → finding *unless* `state: disabled` is set.
- §3.4's `safety_net_for` field and the "two-mechanisms-for-critical-work" doctrine stays.
- **New:** Pass A surfaces a `minor` info-finding when the same work appears both in `scheduled_actions[trigger.kind=heartbeat]` and in `crons[]` / launchd entries — might be a safety-net (intentional), might be duplicate billing (security-bot's pattern). Operator distinguishes via `safety_net_for` annotation.

Authoring guidance — *which trigger to choose, when to retire from one to another, the duplicate-billing trap* — lives in `manifest-authoring-guide.md`. This spec just makes the patterns observable.

### 17.15 Validation against Evolve (2026-06-05)

Evolve is the pod's primary bot — the operator's conversational + alerts partner running as the `evo` macOS user (separated from the `evolve` user that owns the workspace, per memory: "Evo account separation SHIPPED 2026-05-30"). The most architecturally distinct bot in the pod.

**The two-user separation is real and complicates detector queries.** `id evolve` → uid 507; `id evo` → uid 508. The workspace at `/Users/evolve/.openclaw/workspace/` is owned by `evolve`; the runtime (gateway) runs as `evo`. The shared-dir applications folder at `/Users/Shared/evolve/applications/evolve/` is owned by `evo` (writable by the running bot, not by the workspace owner). **Disposition:** the detector and audit runner must know which user to query for which resource. Captured as **Q38** in §15.

**Evolve has only 1 manifest** at `{shared_dir}/applications/evolve/security-cve-scan.json`. **And it's the canonical location** — there's no `/Users/evolve/.openclaw/workspace/manifests/` directory at all. This is the *opposite* extreme from personal-bot (21 bot-side manifests, no shared-dir). The spec's §13.10 unification migration handles this correctly: Evolve already lives where the migration would put everyone else; personal-bot gets synced to match.

**Evolve has only 1 openclaw cron and it's broken right now.** `security:cve-scan` daily 9 AM, Status: `error`. The full delivery line reveals the bug: `Delivery: announce -> last (last -> no route, will fail-closed: Deliver...)`. The `last` channel route doesn't resolve — even when the CVE scan runs successfully, there's nowhere to send the result. This is the same pattern as team-bot-a's `team-bot-a-task-worker` — work succeeds, delivery fails, operator never knows.

**Evolve doesn't use heartbeat AT ALL.**
- No `HEARTBEAT.md` file in the workspace (just SOUL.md, AGENTS.md, README.md, MEMORY.md, procedures/)
- `openclaw.json → agents.defaults.heartbeat` is `{isolatedSession: True, lightContext: True}` — **no `every` field, no `model`** — so no schedule
- No launchd LaunchAgents loaded (`ai.openclaw.gateway.plist` is `.DISABLED`)

This is yet another pattern: **Evolve has the minimum-possible scheduling surface.** One openclaw cron for daily CVE scan; everything else is event-driven (the bot responds to messages, no scheduled work needed).

**Yet another validation pattern: minimal-bot is a valid posture.** Evolve, despite being the *primary* bot, has 1 app, 1 cron, 0 heartbeat use. The system should not pressure operators toward more scheduled work; minimal is fine. Coherence and reconciliation are no-ops for Evolve except for the single CVE-scan app.

**Updated running tally across 5 validation bots: 14 of 16 scheduled jobs broken or silently skipped.** (Added Evolve's 1 broken cron.) The pod's scheduled-work failure rate is now confirmed at 87.5%.

### 17.16 The full pod picture (after all 5 validations)

| Bot | Role | Heartbeat usage | Manifest count | Storage | Crons broken/skipped/ok |
|---|---|---|---|---|---|
| personal-bot | member | Active composite (LLM, ~$0.01/fire) | 21 (full-app) | Bot-side only | 5/5 skipped |
| team-bot-a | member | Overstuffed (LLM, expensive) | 10 (Instance) | Bot-side only | 4/6 error |
| team-bot-c | member | Retired (config says enabled, doc says disabled — incoherence!) | 12 (Instance) | Bot-side only | 1/2 skipped |
| security-bot | member | Fully retired (comment-only file; work moved to launchd Python) | 10 (Instance) | Bot-side only | 2/2 error |
| Evolve | **primary** | Not used (no file, no schedule) | 1 (full-app) | **Shared-dir only** | 1/1 error |

**Patterns confirmed:**
1. **Storage location varies by bot** — personal-bot/team-bot-a/team-bot-c/security-bot are bot-side; Evolve is shared-dir. v20 unification (§13.10) is correct.
2. **Heartbeat usage varies from "heavy" (team-bot-a) to "none" (Evolve)** — no architectural default would fit all. Authoring is per-bot.
3. **The newest design decisions retired heartbeat** — team-bot-c, security-bot. The direction of evolution is "less heartbeat, more launchd Python."
4. **Schema shape varies (full-app vs Instance)** — personal-bot/Evolve use `app-XXX.json` full-app shape; team-bot-a/team-bot-c/security-bot use `i-XXXXX.json` Instance shape bound to Gallery Specs. v20 must tolerate both.
5. **Scheduled-work failures are the dominant production issue** — 14 of 16 broken/skipped. Q32's openclaw-cron-status assertion + signal_notifier delivery is the single most operator-valuable check in the entire spec.

### 17.17 Detector + Pass A extensions needed (additions to §7.6.1 and §6.1)

Three small additions surfaced by team-bot-c+security-bot:

1. **Detector reads `openclaw.json` heartbeat config** in addition to file system + cron sources. New tuple entry: `(source="openclaw_config", id="heartbeat", schedule=<cadence>, command="<empty if comment-only HEARTBEAT.md>")`.
2. **launchd enumeration extended** to include user-installed plists in `/Users/<bot>/Library/LaunchAgents/` AND system-installed plists owned by the bot. security-bot's 6 audit scripts likely have plists; need to confirm via the existing `launchd_entries[]` inventory (§7.1 of [spec-audit-extensions-2026-05-17.md](spec-audit-extensions-2026-05-17.md)).
3. **Pass A reconciles config-claims vs doc-claims** for heartbeat specifically — team-bot-c's case where openclaw.json says enabled and HEARTBEAT.md says disabled. New assertion: "if heartbeat is enabled in openclaw.json AND HEARTBEAT.md is comment-only / asserts disabled, emit `major` finding 'heartbeat config and doc disagree.'"

These are small (~50 LOC additions to PR 6 + PR 5). Captured as **Q36** and **Q37** in §15.

---

## 18. Related docs

- [spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md) — the 3-tier audit framework this extends
- [spec-audit-extensions-2026-05-17.md](spec-audit-extensions-2026-05-17.md) — scheduled-action extraction, substrate audit, evo fail
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — Signal store
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — Proposal store
- [application-manifests.md](application-manifests.md) — design principles
- [manifest-spec.md](manifest-spec.md) — full schema reference
