# App Audit — Architecture (2026-05-16)

Status: **proposed**. Companion to [spec-app-testing-2026-05-07.md](spec-app-testing-2026-05-07.md) (which owns test execution and cadence) and [spec-behavioral-runs-2026-05-07.md](spec-behavioral-runs-2026-05-07.md) (which owns the LLM judge for `test_cases[]`). Concretizes a gap surfaced during the manifest-coverage investigation: tests verify a narrow defined slice; they don't catch broken cron entries, drifted file paths, code that has rotted into semantic divergence from its manifest, or apps that quietly stopped doing what the user installed them for.

**What this is.** A third QA tier above tests, split into two layers. **Tier 2 (structural verifier)** is pure-Python reality checking — does the wiring still hold? File paths exist, crons parse, integrations are still configured, shas match. **Tier 3 (semantic audit)** is a two-stage LLM pass — does the code still do what `usage.how_to_use` and `description` claim? Tier 2 is cheap and runs weekly; Tier 3 is expensive and runs monthly or on-demand. Both produce structured findings that flow into the existing Signal store / Proposal store rather than creating new surfaces.

**Ownership: bot, not Evolve.** Each bot runs its own audits — scheduling, execution, conflict detection, trail-writing — all happen on the bot account using the bot's own LLM credentials. Evolve's role is narrower: install the runner during deploy, poll bot outboxes for completed audits, ingest findings into pod-wide Signal/Proposal stores, surface in the admin UI. This matches the per-bot inference principle ("LLM inference over user data runs inside each bot") and removes a central scheduling daemon that would otherwise be a single point of failure for every bot's audits.

The two layers ship as separate PRs because their failure modes are different (mechanical vs probabilistic) and their calibration disciplines are different (Tier 2 is right-or-wrong; Tier 3 needs a months-long noise-tuning phase before we trust it with code changes).

**Relationship to other specs.**
- [spec-app-testing-2026-05-07.md](spec-app-testing-2026-05-07.md) — owns Tier 1 (`test_command` + `test_cases[]`). The testing scheduler stays Evolve-owned (admin dispatches test jobs to the bot). Audits diverge from this model — they're bot-scheduled.
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — audit findings emit Signals; Tier 3 triage may emit Proposals via the existing arbiter. No new wiring beyond what RSI already contemplates.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — every audit finding lands as a Signal (or auto-resolves a prior one via `sweep_resolve`). Audit is just another monitor from the Signal store's perspective.
- [manifest-spec.md](manifest-spec.md) — schema v11 added `last_structural_verify` + `audit_trail_path` (Tier 2, PR 1); schema v12 adds `audit_cadence`, `audit_eligible`, `audit_accepted`, `last_audit` (Tier 3, this PR).

---

## 1. Goals and non-goals

**Goals.**

1. Catch the structural failure modes tests don't notice: broken cron entries, missing files referenced by the manifest, drifted shas, unconfigured integrations, dangling cross-app dependencies.
2. Catch the semantic failure modes neither tests nor structural verification notice: code that runs and passes tests but no longer matches the manifest's stated behavior.
3. Keep the cost story honest. Tier 2 is free (pure Python). Tier 3 is metered and capped, runs monthly by default, and can be turned off entirely per-app.
4. Triage before surfacing. Stage 3b filters noise so the operator sees only audit findings worth their attention. Most observations get logged, fewer get raised, fewer still ever auto-fix.
5. Calibration before automation. Tier 3 ships with `auto_fix` disabled. Operators see proposed changes for at least one full cadence cycle before the system is allowed to act on its own.

**Non-goals.**

1. Replacing tests. Tier 1 stays the load-bearing per-commit regression check. Audits complement it; they don't substitute for it.
2. Generic code review. Tier 3 is grounded in the manifest's claims about the app, not in abstract code-quality opinions. "This function could be cleaner" is not a finding worth surfacing.
3. Cross-bot or cross-pod audits. Each app is audited in the context of its owning bot. Cross-app interactions are followed via `manifest.dependencies[*]` but the audit unit is one app on one bot.
4. Live audit-run streaming in the UI. Wrap-and-notify, same as forge and tests.
5. Automated rollback. If an `auto_fix` makes things worse, the user notices via the next signal/audit; we don't try to detect-and-rollback inside the audit pipeline.

---

## 2. The three-tier model

| Tier | What it asserts | Mechanism | Default cadence | LLM? |
|---|---|---|---|---|
| **1. Tests** | "The narrow slice I defined still works" | `test_command` exit code; `test_cases[]` via behavioral judge | Weekly (`light`) | Judge only |
| **2. Structural verify** | "The wiring still holds" | Pure Python — file stat, cron parse, import probe, ACL lookup | Weekly | No |
| **3. Semantic audit** | "The code still does what the manifest claims" | LLM, two-stage | Monthly | Yes |

Each tier is independently configurable per-app. A trivial app may run with `test_cadence=off`, `audit_cadence=off` and just live on its forge-time verification. A load-bearing app may run `test_cadence=strict` (daily), structural verify weekly (always on), and `audit_cadence=monthly`. The structural tier has no `off` mode — it's always on — because it's mechanically cheap and there's no rational reason to disable it.

---

## 3. Tier 2 — structural verifier

We already have `verify_manifest_reality` in `packages/admin/evolve_admin/applications/bot_forge.py` but it only fires at forge-approval time (Phase C). This tier promotes the same idea to a periodic check with broader coverage and integrates the output with the Signal store rather than just stamping `manifest.last_verification`.

### 3.1 Assertions

Pure-Python checks, all mechanical Yes/No:

1. Every `manifest.files[*].path` exists on disk. Severity: `critical` if missing.
2. Every `manifest.files[*]` sha256 matches the stored value. Severity: `major` if drifted. (Some drift is benign — `data/` files are expected to change; the layer field on the file entry tells us which to skip.)
3. Every `manifest.crons[*].script` path exists and is executable. Severity: `critical` if missing.
4. Every `manifest.crons[*].schedule` parses as a valid crontab schedule. Severity: `major` if unparseable.
5. Every cron entry appears in the bot user's actual `crontab -l` output. Severity: `major` if missing (manifest claims a cron that isn't installed).
6. `manifest.test_command`, when set, parses to an executable command and the first token exists on `$PATH` (or is an absolute path that exists). Severity: `minor` if broken.
7. `manifest.requirements.python_packages[*]` import in the bot's Python env (via subprocess `python3 -c "import X"`). Severity: `major` if any required import fails.
8. `manifest.requirements.integrations[*]` appear in the bot's `auth-profiles.json` for the listed channel. Severity: `major` if required and missing.
9. `manifest.dependencies[*]` (cross-app file deps) — file exists AND its provenance marker still names the same `pkg_id`. Severity: `major` if the file was deleted or got reclaimed by another app.
10. `manifest.interface_contract.cli[*].command` resolves to one of `manifest.files[*]` or an installed shell command. Severity: `minor` if broken.

### 3.2 Output

Each finding is written by the bot as a structured record into its **audit outbox** at `/Users/<bot>/.openclaw/workspace/evolve/audit_outbox/<audit_run_id>.json`. The record carries:
- `producer`: `app_structural_verifier`
- `signature`: `(bot_id, app_id, assertion_id, evidence_key)` — so the admin's poller dedupes across reruns
- `subject`: `{bot_id, app_id, manifest_pkg_id}`
- `severity`: as listed above
- `summary`: e.g. `"app journal: missing file scripts/journal.py"`
- `evidence`: the specific path / sha / cron line / etc.

The admin server runs a thin **audit poller** (folded into the existing admin server tick, not a new daemon) that watches each bot's `audit_outbox/`. New records get ingested via `signals.store.observe()` on the admin side — i.e. Signals stay pod-wide and centralized, but their creation is triggered by bot-produced records. The poller also calls `signals.store.sweep_resolve(producer="app_structural_verifier", kept_signatures=<current_firing_set>)` per bot so cleared findings auto-archive.

### 3.3 Bot-side runner

A small script `audit_runner.py` lives in the bot's workspace under `/Users/<bot>/.openclaw/workspace/evolve/`. The script is planted at deploy time (mirrors how the forge inbox/outbox is set up). Its responsibilities:

1. Read all manifests under `/Users/<bot>/.openclaw/workspace/manifests/`.
2. For each app, run the Tier 2 assertions (§3.1).
3. Write findings to `audit_outbox/<audit_run_id>.json`.
4. Stamp `manifest.last_structural_verify = {verified_at, status, findings_count}` directly (the manifest is bot-owned, so no sudo dance).

The runner is invoked by a bot-side cron entry installed during deploy:
```
0 */6 * * * cd $HOME && python3 .openclaw/workspace/evolve/audit_runner.py --tier=2 >> .openclaw/workspace/evolve/audit_runner.log 2>&1
```

Every 6 hours is the default; structural verification is mechanically cheap, and 6h gives Signals a tight feedback loop. The interval can be tuned via the synced pod config (§7.1).

`manifest.last_verification` continues to hold the forge-time verification block (it's already documented for that purpose). The new `manifest.last_structural_verify` field captures the most recent periodic run from the bot-side runner.

### 3.4 Critical-finding kick to Tier 3

When Tier 2 emits a `critical` finding, the same runner enqueues a Tier 3 audit for that app on its next invocation — gated on the synced pod-config flag `audit_on_critical_structural` (default `true`). Rationale: a missing file is a structural break, but it's also often the surface symptom of a deeper rot — Tier 3 can read the surrounding code to determine whether the manifest should be rewritten, the file restored, or the app retired. The "enqueue" is just a hint file the runner writes to itself; the next tick picks it up. No admin involvement.

---

## 4. Tier 3 — semantic audit (two-stage LLM)

Tier 3 runs entirely on the bot. The same `audit_runner.py` that does Tier 2 also orchestrates Tier 3 when an app is due: it assembles inputs from local state, makes the LLM call via the bot's OpenClaw agent (same credentials path as forge), runs Stage 3a then Stage 3b, applies safe auto_fixes within its own workspace, and writes results to the audit outbox.

The admin's only role is on-demand triggering (writing an "audit this app now" hint into the bot's audit inbox) and post-run ingestion (poller picks up outbox records, writes Proposals and Signals into shared stores).

The two stages exist to separate **observation** (cheap to generate, noisy, fine-grained) from **triage** (decides what's worth surfacing, what's safe to fix, what's noise). Putting both in one prompt produces over-eager auto-fixes; splitting them gives us a cheap calibration knob — Stage 3a can stay constant while Stage 3b's prompt and gating logic tighten over time.

### 4.1 Stage 3a — Discovery

**Inputs assembled by the bot runner from local state:**
- The full manifest (including `usage`, `description`, `identity`, `success_criteria`, `constraints`, `interface_contract`).
- The actual code files at their current sha (read from disk; not from manifest claims, so a drift between manifest and reality is detectable).
- Last 30 days of test runs (`last_test_run`, `last_test_result`, behavioral case history).
- Last 30 days of Tier 2 verification results.
- Recent app usage stats from the user-profile inferrer / app-session correlator (how often the app actually got invoked).
- The prior audit's findings, if any, so the LLM can note what's changed since.
- `manifest.audit_accepted[]` — the list of finding signatures the operator has already accepted. Stage 3a is instructed to drop matching observations before emitting. The flag `--ignore-accepted` on a "Full audit" suppresses this input so the LLM re-evaluates from scratch (see §5.5).

**Prompt frame:** "This app's manifest claims X. Read the code as it exists right now. Produce structured observations where reality diverges from claim, or where the code has rotted in ways tests wouldn't catch."

**Output (one JSON file per audit run):**
```jsonc
{
  "audit_id": "audit-<8hex>",
  "app_id": "journal",
  "bot_id": "team-bot-a",
  "stage": "3a-discovery",
  "started_at": "2026-05-16T10:00:00Z",
  "completed_at": "2026-05-16T10:02:14Z",
  "model": "claude-sonnet-4-5",
  "tokens": {"input": 18432, "output": 1204},
  "observations": [
    {
      "id": "obs-01",
      "category": "drift | missing_functionality | broken_path | code_smell | behavior_mismatch | dead_code | manifest_mismatch",
      "severity": "info | minor | major | critical",
      "description": "Manifest usage.how_to_use claims auto_capture detects mood from messages, but scripts/journal.py only logs when invoked explicitly with --mood.",
      "evidence": [
        {"path": "scripts/journal.py", "lines": [42, 58]},
        {"path": "manifest", "field": "usage.auto_capture.enabled"}
      ],
      "suggested_action": "auto_fix | raise | dismiss"
    }
  ]
}
```

Persisted at `/Users/<bot>/.openclaw/workspace/evolve/audits/<app_id>/<audit_id>.json` — bot-side, just like the trail. The admin reads via the existing ACL when surfacing in UI; nothing about the audit lives in `{shared_dir}` except the Signals and Proposals the admin's poller ingests. This is the **noisy** stage — every observation lands here, unfiltered. Stage 3b is where the noise gets squeezed out.

### 4.2 Stage 3b — Triage

**Inputs:**
- Stage 3a's observations from this run.
- Prior audit history for this app (last 12 runs).
- Recent Signal / Proposal activity for this app (so we don't re-raise what's already open).
- The pod-wide rejected-proposal feedback log (`{shared_dir}/signals/feedback.jsonl`) — operators rejecting prior audit proposals teaches the triage stage which classes of finding aren't worth raising.
- The current calibration mode (see §5).

**Prompt frame:** "For each observation, pick exactly one outcome. Justify when picking `auto_fix`. Justify when picking `dismiss` if the observation is `major` or `critical`."

**Three outcomes per observation:**

| Outcome | Behavior |
|---|---|
| `dismiss` | Logged in the audit JSON; no surface. Reason captured for future triage tuning. |
| `auto_fix` | Bot applies the change directly via the existing bot-dispatch path. Allowed transformations only (§5.2). Result captured in the audit JSON. |
| `propose` | Emits a Proposal to the arbiter store. `motivating_signals[]` links the audit observation. Operator handles via existing Proposals queue. |

**Output:** the same audit JSON file gets a `stage_3b_triage` block appended:
```jsonc
{
  "stage_3b_triage": {
    "completed_at": "...",
    "tokens": {...},
    "outcomes": [
      {"obs_id": "obs-01", "outcome": "propose", "proposal_id": "p-...", "rationale": "..."},
      {"obs_id": "obs-02", "outcome": "dismiss", "rationale": "..."},
      {"obs_id": "obs-03", "outcome": "auto_fix", "rationale": "...", "applied": true, "diff": "..."}
    ]
  }
}
```

### 4.3 Model selection

Both stages run on the same model in v1 — the bot's configured `audit_tier` (defaults to `tier2`, same as the builder model used by forge). Rationale:
- Cheaper than escalating; we don't need cross-model adjudication for the kinds of observations Tier 3 produces.
- Easier to reason about consistency — Stage 3a's emit shape and Stage 3b's interpretation of "severity major" share a calibration.
- The cost-tuning lever is cadence and audit_tier, not architecture.

If Stage 3b proves unreliable in calibration mode (false-`propose` rate stays high after months), revisit by escalating Stage 3b to a stronger tier.

---

## 5. Calibration discipline

The biggest risk with Tier 3 isn't that it doesn't work — it's that it works *too eagerly*. An audit system that raises 30 proposals per app per run produces alert fatigue, and the operator's "snooze everything" reflex is worse than no audit at all.

The discipline below is non-negotiable for the first 2–3 months after Tier 3 ships.

### 5.1 Calibration mode (default for v1)

When `app_audit.calibration_mode = true` in network.json (default), Stage 3b's `auto_fix` outcome is **disabled at the orchestration layer**. The triage stage may pick `auto_fix` in its output, but the executor maps `auto_fix → propose` before acting. Operators see what the system *would have* fixed before it can act on its own.

Once the operator has accepted ≥80% of `auto_fix`-flagged proposals across at least one full cadence cycle (∼60 days of monthly audits) for a given finding shape, that shape can be promoted to actual auto-fix. Promotion is a manual config edit per allowed-transformation entry — not automatic.

### 5.2 Allowed transformations (once auto_fix is enabled)

Hard-coded whitelist of safe operations, not "whatever the LLM thinks is fine":

| Transformation | Notes |
|---|---|
| Delete file in `manifest.files[*]` whose path no longer exists, AND no code path references the entry | Pure manifest cleanup; no FS write. |
| Delete dead orphan files (not in any manifest, not modified in N days, no recent invocations) | FS write; data/state layer files excluded permanently. |
| Update `manifest.files[*].path` after a detected rename (file at new path, sha matches old entry) | Pure manifest cleanup. |
| Remove stale crontab entry that matches a removed manifest cron | Touches user's crontab; only when manifest is authoritative. |
| Fix obviously stale `manifest` metadata (`last_reviewed`, dead `docs` links) | Pure manifest cleanup. |
| Update `manifest.usage` fields from comment-block annotations in the app code | Pure manifest cleanup; useful for keeping `usage.how_to_use` synced with header comments. |

Auto-fix **never** touches:
- Application code itself (any `.py`, `.sh`, `.js`, etc. that isn't pure metadata).
- User data files (any file with `layer ∈ {data, state}`).
- Crons referenced by another app's manifest.
- Integration credentials, auth-profiles, OAuth tokens.
- The bot's `openclaw.json` or anything in `~/.openclaw/` outside `workspace/`.
- Anything outside the app's owned file set (cross-app modification requires a Proposal).

### 5.3 Rate ceilings (always on)

Even after calibration ends, ceilings prevent any single audit run from spiraling:

| Ceiling | Default |
|---|---|
| Max `auto_fix` actions per app per audit run | 3 |
| Max Proposals raised per app per audit run | 5 |
| Max audit runs per scheduler tick | 5 (pod-wide) |
| Max LLM tokens per audit (both stages combined) | 100k input |

A run that would exceed any ceiling drops the lowest-severity items first, logs the truncation in the audit JSON, and defers them to the next tick.

### 5.4 The audit trail (per app)

Every app gets a durable rolling log at `/Users/<bot>/.openclaw/workspace/evolve/audits/<app_id>/trail.jsonl` (bot-side, written by the runner). The trail is the **operator-facing** record — distinct from the per-run audit JSON files, which are forensic and verbose. Admin reads via the existing read ACL on `.openclaw/`; the manifest's `audit_trail_path` field carries the full path so the UI can deep-link without recomputing it. Each trail entry is one line:

```jsonc
{"ts": "2026-05-16T10:02:14Z", "kind": "audit_run", "audit_id": "...", "raised": 2, "auto_fixed": 1, "dismissed": 5}
{"ts": "2026-05-16T10:02:14Z", "kind": "auto_fix", "audit_id": "...", "obs_id": "obs-03", "transformation": "manifest_path_update", "diff": "..."}
{"ts": "2026-05-16T11:14:01Z", "kind": "mark_accepted", "signature": "...", "accepted_by": "operator", "rationale": "..."}
{"ts": "2026-05-16T12:30:00Z", "kind": "conflict_notice", "audit_id": "...", "affected_apps": [...]}
{"ts": "2026-05-17T09:00:00Z", "kind": "audit_run", "audit_id": "...", "status": "failed", "error": "..."}
```

The trail is the canonical home for auto-fix actions. Auto-fixes do NOT emit Proposals (would just bloat the inbox) and do NOT emit Signals (would clutter the alerts page). They land in the trail, viewable on demand. The manifest carries a pointer (`manifest.audit_trail_path`) so the Apps page can deep-link directly.

Retention: 365 days, mirroring the rest of the audit data.

### 5.5 Mark-as-accepted

Operators can mark any audit finding as "accepted" — typically from the Proposal it spawned, via a "Mark as accepted" button that writes the finding's signature into `manifest.audit_accepted[]`:

```jsonc
"audit_accepted": [
  {
    "signature": "sha256-of-(category, evidence_paths, description-canonical)",
    "accepted_at": "2026-05-16T11:14:01Z",
    "accepted_by": "operator",
    "rationale": "Manifest usage block intentionally describes future behavior; not drift."
  }
]
```

Stage 3a's prompt receives this list and is instructed: "Observations matching any of these signatures should be dropped before output." Stage 3b is a backstop — if Stage 3a emits one anyway, Stage 3b dismisses it.

**Risk:** "Mark as accepted" can hide a real flaw — operators may accept a finding once and then never re-examine it. Mitigation:
- A **"Full audit" option** is always available manually (CLI flag `--ignore-accepted`, UI button). It runs the audit *without* feeding `audit_accepted` to Stage 3a, so the audit re-evaluates from scratch. Findings that come back are surfaced as Proposals with a `reaffirmed` flag, telling the operator "we re-examined this and still think it's worth looking at."
- The trail logs the original `mark_accepted` event, so when a full audit re-raises a previously-accepted finding the history is clickable.

Full audits are also auto-scheduled at **2× the configured cadence** — i.e. an app on `monthly` cadence gets a full audit every other run. This guarantees the accepted list gets re-checked without operator action.

### 5.6 Cross-app conflict awareness

Before any auto_fix touches a file, the executor checks whether that file appears in another app's `manifest.files[*]`, `manifest.dependencies[*]`, or `manifest.crons[*]`. When it does, the executor **does not apply the fix**. Instead, it writes a `conflict_notice` entry to the audit trail and emits a single `audit_conflict_notice` Proposal:

```
App `journal` audit wants to update scripts/journal.py (manifest path drift),
but this file is also used by:
  - app `morning-briefing` (depends on scripts/journal.py for daily mood data)
  - app `mood-tracker` (shares scripts/journal.py as data writer)

Action deferred. Resolve by either:
  - updating all three apps' manifests together (recommended)
  - removing this file from the dependents' manifests
  - marking this finding as accepted on the `journal` audit
```

This applies to every transformation in §5.2 that would touch a shared resource. We do NOT try to auto-resolve the cross-app conflict; v1 stops at "notice and surface."

This is the load-bearing guardrail against the regression loop the operator named: app A's audit fixes a shared file, app B's tests break, app B's audit re-touches the file, app A breaks. Surfacing as a notice without auto-action keeps the human in the loop until we have evidence of how often these conflicts occur. If volume is low (the expected case), we never need to build a resolution mechanism. If volume is high, a follow-up spec for `audit_coordinator` is the right place to handle it.

---

## 6. The audit_cadence model

Parallel to `test_cadence`, distinct field, distinct cron schedule.

| Mode | Behavior |
|---|---|
| `never` | No auto audit. Only manual / on-demand runs ever execute. |
| `quarterly` | Auto Tier 3 runs every 90 days. Cheap posture for stable apps. |
| `monthly` *(default)* | Auto Tier 3 runs every 30 days. Catches drift from upstream changes (LLM updates, dep shifts) and slow-rot bugs. |
| `weekly` | Auto Tier 3 runs every 7 days. For load-bearing or rapidly-evolving apps. |
| `daily` | Auto Tier 3 runs every 24 hours. Reserved for high-risk apps where semantic drift would have material consequences. Cost-flag the operator when they pick this. |

The auto audit cadence has three layers; later layers override earlier ones, with `null` at any layer meaning "inherit the layer above."

1. **Pod-wide default** — `network.json → app_audit.default_cadence`. Defaults to `monthly`.
2. **Per-bot override** — `network.json → app_audit.bot_cadence: {bot_id: cadence}`. Empty by default.
3. **Per-app override** — `manifest.audit_cadence`. Nullable.

**Why the per-bot tier exists.** A pod may have one load-bearing bot (e.g. the primary admin assistant) whose apps deserve higher scrutiny than the rest, without forcing every app's manifest to be edited individually. Setting `app_audit.bot_cadence: {team-bot-a: "weekly"}` raises team-bot-a's default without touching team-bot-a's manifests.

**`on_demand` is always available** regardless of cadence — every app has a "Run audit now" button, including apps with `audit_eligible=false` (§6.1). Manual runs are how operators audit apps that the auto-scheduler skips.

Tier 2 has no cadence override — it always runs weekly alongside the test scheduler, and there's no off-switch (it's nearly free).

### 6.1 Audit eligibility (which apps the auto-scheduler considers)

Distinct from `test_exemption_reason` (which says "this app is too trivial to test"). Audit eligibility says "this app has no behavior worth auto-auditing — it's a data container, a static reference, or pure-config."

The manifest field is `audit_eligible: bool` (default `true`). When `false`, the auto-scheduler skips the app entirely; only manual runs ever audit it.

**Who sets it.** Forge sets it during build based on the app's shape:
- Apps with no executable code (e.g., a manifest that owns only `.md` reference files, a knowledge-base app) → `audit_eligible: false`.
- Apps with a `test_exemption_reason` set AND no code that could drift → `audit_eligible: false`.
- Everything else → `audit_eligible: true`.

Operators can override either way in the manifest editor. The Apps page shows a small badge for `audit_eligible=false` apps so the absence of audit history is visible (not invisible).

The test-exemption and audit-eligibility decisions are independent: an app may be test-exempt but audit-eligible (a single-line cron that doesn't need a test but whose manifest claims could drift), or test-required but audit-ineligible (rare — an app with deterministic behavior that's easy to test but whose code is too trivial to semantically audit).

---

## 7. Triggers

All audit execution lives on the bot. Triggers fall into two shapes: **timed** (bot's own crontab fires the runner on a schedule) and **on-demand** (something external writes to the bot's `audit_inbox/` and optionally "kicks" the runner so it wakes immediately instead of waiting for the next cron tick).

### 7.1 Who actually runs the cron

The crontab entries belong to the **bot user account** (e.g. `/var/at/tabs/journal` for a bot named `journal`, or `/var/at/tabs/personal-bot-user` for `team-bot-b`). macOS launchd cron fires them as the bot user, not as `evolve`. Evolve has no role in firing them — Evolve only **installs** the entries during `deploy_bot` and refreshes them on each subsequent deploy.

This matters because it means audits keep running even if the Evolve admin server is down — the bot is sovereign. The downside: if the bot's crontab gets clobbered (manual edit, OS issue), audits stop until the next deploy reinstalls them. Mitigation: Tier 2 includes an assertion that the bot's audit cron entries are present in `crontab -l`; a missing entry surfaces as a Signal.

### 7.2 The trigger list

1. **Bot cron — Tier 2.** Bot's crontab runs `audit_runner.py --tier=2` every 6 hours. Always on. No way to disable per-app; mechanically cheap.
2. **Bot cron — Tier 3.** Bot's crontab runs `audit_runner.py --tier=3` once an hour. The runner only audits apps that are *due by cadence* (per-app `audit_cadence`, with bot and pod fallbacks). Hourly wake-up doesn't mean hourly audits — it's a cheap scan that picks up only what's due. Also picks up any pending inbox requests written since the last tick.
3. **CLI on-demand.** `sudo evolve-admin application audit <bot> [<app>] [--all] [--ignore-accepted]` — admin writes `audit_inbox/<request_id>.json`, then **kicks** the bot (see §7.3) so the runner wakes immediately. The CLI follows progress by polling the outbox.
4. **UI on-demand.** "Run audit now" button on the Apps detail modal hits `POST /api/applications/<bot>/<app>/audit` which writes the same inbox file and kicks. The UI polls the outbox for completion (typical wait: 10s–2min depending on tier and app size).
5. **evo keyword on-demand.** Bot user types `evo app-audit <app>` in their messaging channel; the wizard handler writes an inbox file and kicks. The bot replies with a "started" confirmation; the audit result lands as a notification when complete (same mechanism as forge-complete notifications). See §7.4.
6. **Tier 2 critical → Tier 3.** When the Tier-2 run emits a `critical` finding, the runner writes a self-hint into its own audit_inbox so the next Tier-3 tick picks it up — gated on the synced pod-config flag `audit_on_critical_structural` (default `true`). No external involvement.
7. **Post-forge.** Not a default trigger. Forge-time `verify_manifest_reality` already runs; rerunning Tier 3 right after a build is wasteful. Operators who want this can run on-demand from the UI or via `evo app-audit` after a fresh install.

The runner serializes within a single bot — at most one audit run per bot at any time. This is enforced by a lockfile (`/Users/<bot>/.openclaw/workspace/evolve/.audit_runner.lock`). A second invocation while one is running exits cleanly; the inbox request stays queued for the next free tick.

### 7.3 The "kick" — immediate execution on demand

If the manual run only relied on the hourly cron, the operator could wait 59 minutes after clicking "Run audit now." Unacceptable. The fix is a thin wake-up dispatch.

After writing the `audit_inbox/<request_id>.json` file, the admin server runs:

```
sudo -H -u <bot_user> /opt/homebrew/bin/python3 \
    /Users/<bot_user>/.openclaw/workspace/evolve/audit_runner.py \
    --pickup-inbox \
    --request-id <request_id>
```

This is the same shape as the forge dispatch (`sudo -H -u <bot> openclaw agent ...`), with a new sudoers grant for the `audit_runner.py` path. The runner:
1. Checks the lockfile. If held, exits 0 with "already running — request remains queued."
2. Acquires the lock, reads the named request from the inbox.
3. Executes the audit synchronously (Tier 2 or Tier 3 per the request).
4. Writes outbox records.
5. Releases lock and exits.

The dispatching admin process **does not wait** for the runner to finish — it returns immediately to the caller (UI / CLI / evo handler). The caller polls the outbox. This matches the forge wrap-and-notify model.

Kicks are idempotent: if two operators click "Run audit now" within 2 seconds, the second kick sees the lock held and exits; the first request runs, the second one stays in the inbox for the next tick.

### 7.4 evo keyword grammar

The `evo app-audit` wizard intent ships alongside the rest of the audit feature. It runs admin-side (where the rest of the evo wizard lives) and writes to the bot's audit inbox using the same helper the UI and CLI use.

Grammar:

| Form | Effect |
|---|---|
| `evo app-audit` | Lists this bot's apps with their most recent audit summary (`✓ healthy`, `⚠ 2 findings`, `❌ audit failed`, `– never audited`). |
| `evo app-audit <app>` | Queues an audit run for `<app>`. Replies "Started — I'll let you know when it's done." Notification fires on completion with a summary. |
| `evo app-audit <app> full` | Same as above, but with `--ignore-accepted` so previously-accepted findings get re-evaluated. |
| `evo app-audit all` | Queues audit runs for every `audit_eligible=true` app on this bot. Single confirmation reply; one notification per app as each completes. |
| `evo app-audit all full` | Same as `evo app-audit all`, with `--ignore-accepted` applied to each. |
| `evo app-audit accept <finding-id>` | Marks the named finding as accepted. The bot's reply to a Stage-3b proposal includes the finding-id; the user echoes it back via this command. Writes to `manifest.audit_accepted[]`. |
| `evo app-audit history <app>` | Pastes the last 5 trail entries for the named app, with timestamps. |

`evo app-audit all` writes one inbox file per eligible app and fires a single kick; the bot serializes them via the lockfile and processes the queue. Notifications batch into a single summary message when more than 3 audits run in a 5-minute window, so the user doesn't get flooded.

**Auth model.** Same as other evo commands: the primary user of the bot (`network.json → bots.<bot_id>.primary_user`) and pod admins (`network.json → pod.admins`) can run all forms. Other users on team bots see a "this command isn't available to you" reply. The auth check lives in the evo handler, not in the audit runner — the runner trusts the inbox file because only admin-writable inbox writes can land there.

**Reply shape.** Like other evo commands, replies are short. A successful queue: `"Started auditing journal. I'll let you know when it's done."` A completion notification: `"Audit of journal done · 2 findings · 1 raised, 1 dismissed · view: <link to trail>"`. A no-findings reply: `"Audit of journal done · all clear."`

**Why this matters.** End users notice problems with their apps before operators do. Without an evo route, a user who suspects their journal app has stopped working has to ping the operator, who has to log into the dashboard, who has to click around. With `evo app-audit journal`, the user can self-diagnose in 30 seconds.

### 7.5 Synced pod config

The runner reads pod-wide settings from `/Users/<bot>/.openclaw/workspace/evolve/pod_config.json`, written by Evolve whenever `network.json` changes. The synced file is a small subset of network.json with just the keys the runner needs:

```jsonc
{
  "audit": {
    "default_cadence": "monthly",
    "bot_cadence": "weekly",                   // resolved for THIS bot from network.json's bot_cadence map
    "calibration_mode": true,
    "audit_on_critical_structural": true,
    "tier3_tier": "tier2",                     // model tier for Stage 3a/3b
    "ceilings": {
      "max_auto_fix_per_run": 3,
      "max_proposals_per_run": 5,
      "max_tokens_per_audit": 100000
    }
  }
}
```

The runner re-reads this file every tick — operator changes propagate within the hourly cycle, no bot restart required. Evolve writes the synced file using the same `/tmp` staging + sudo-cp pattern the admin uses for other bot-side writes; the bot has read ACL.

### 7.6 What Evolve actually does (the narrow role)

For absolute clarity on what stays on Evolve's side:

1. **Plant during deploy.** Drop `audit_runner.py` into the bot's workspace, install the two crontab entries (Tier 2 every 6h, Tier 3 hourly scan), set the workspace ACLs, install the sudoers grant for the `--pickup-inbox` kick.
2. **Sync pod config.** Whenever `network.json` changes, regenerate `pod_config.json` for each bot.
3. **On-demand dispatch.** When the operator (UI / CLI / evo handler) requests an audit: write `audit_inbox/<request_id>.json`, then kick the bot via the sudo dispatch (§7.3). Return immediately; don't wait for the runner.
4. **Run the evo handler.** The `evo app-audit ...` wizard intent runs admin-side, authenticates the requesting user, then calls the same on-demand dispatch helper as the UI button.
5. **Poll outboxes.** Admin server's existing tick walks each bot's `audit_outbox/`, ingests records into pod-wide Signal store / Proposal store, then archives the outbox file (`audit_outbox/_ingested/<ts>/`).
6. **Surface in UI.** Render trails (read from bot side via ACL), Signals, and Proposals.
7. **Emit notifications.** When an outbox ingest completes a request that originated from the evo handler, emit a `forge_complete`-style notification to the requesting user (existing notification queue, same mechanism as forge-complete).

That's it. No audit-scheduling daemon, no audit-orchestration logic on the admin side, no cross-bot coordination. The bot is sovereign over when and how its own apps get audited; Evolve's job is plumbing + UX surface.

---

## 8. Output integration

The bot's runner produces outputs in two places: bot-side files (audit trail, per-run JSONs, manifest stamps) and the bot's audit_outbox/ (records the admin's poller ingests into pod-wide stores).

| Layer | Bot writes | Admin poller does |
|---|---|---|
| Tier 2 finding | Outbox record, severity-tagged. | Calls `signals.store.observe()` → Signal in `{shared_dir}/signals/firing/`. `sweep_resolve` cleared findings. |
| Tier 3 `dismiss` outcome | Per-run audit JSON only; not added to trail. | Nothing. |
| Tier 3 `auto_fix` outcome (calibration mode) | Promoted to `propose` (see §5.1). Falls through to the row below. | — |
| Tier 3 `auto_fix` outcome (post-calibration, allowed transformation, no conflict) | Applied directly to bot-owned manifest / files. Trail entry written. No Proposal, no Signal — the trail is the canonical record. | Nothing. |
| Tier 3 `auto_fix` outcome with cross-app conflict | Action deferred. Trail entry written. Outbox record with kind=`conflict_notice`. | Raises one `audit_conflict_notice` Proposal per record (see §5.6). |
| Tier 3 `propose` outcome | Outbox record with full observation + rationale. | Writes Proposal to arbiter store with `motivating_signals[]` linking the audit observation. |
| Audit run failure (LLM dispatch errored, lockfile timeout, etc.) | Trail entry with `kind: "audit_run", status: "failed"`. Outbox record with kind=`run_failed`. | Surfaces "Last audit failed" badge on the Apps page tile. |

Per-app data on the **bot** lives at `/Users/<bot>/.openclaw/workspace/evolve/audits/<app_id>/`:
- `trail.jsonl` — durable rolling log (see §5.4). The operator-facing surface.
- `<audit_id>.json` — full per-run output. Retained 365 days; the bot's runner prunes on its next tick.

Pod-wide records that the admin's poller wrote live in their existing homes:
- Signals: `{shared_dir}/signals/firing/`
- Proposals: `{shared_dir}/proposals/pending/`

The manifest carries `manifest.audit_trail_path` (absolute path on the bot) so the Apps page UI can deep-link via the admin's ACL-backed file reader.

### 8.1 Idempotency of poller ingestion

The audit poller must be safe to run repeatedly against the same outbox files — a single network blip or admin restart shouldn't double-emit Signals or Proposals. Each outbox record carries a `record_id` (UUID per finding). Before emitting, the poller checks whether a Signal or Proposal already exists with that `record_id` in its source-attribution field; if so, the ingestion is a no-op. After successful ingestion, the poller moves the outbox file to `audit_outbox/_ingested/<ts>/<filename>` (kept 7 days for forensics, then pruned by the bot).

---

## 9. Applications page changes

**Per-app tile** (the existing card on the Apps grid):
- Audit-failed badge on the tile when `trail.jsonl`'s most recent `audit_run` entry has `status=failed`. Hover shows the timestamp; click opens the trail.
- `audit_eligible=false` apps get a small "manual audit only" pill, so the absence of audit activity is intentional and visible.

**Per-app detail modal** (the manifest view), four additions:

1. **"Last audit" line.** `Last audit: 2026-04-12 · 2 raised, 1 auto-fixed, 5 dismissed`. Click → opens the audit trail viewer (the trail.jsonl rendered as a chronological log; each entry expandable).
2. **Audit cadence dropdown.** `never / quarterly / monthly / weekly / daily / inherit (pod or bot default)`. Same pattern as the test-cadence dropdown.
3. **"Run audit now" button.** Next to "Run test now". Fires the on-demand trigger from §7. Drops down with a "Full audit (ignore accepted)" sub-option per §5.5.
4. **Audit-accepted findings list.** A small expandable section listing `manifest.audit_accepted[]` entries with rationale + timestamp + an "Un-accept" link that removes the signature (next audit can re-raise).

**Pod-wide header strip** additions:
- `12 apps healthy · 2 with audit findings · 1 audit failed · last audit sweep 14d ago`. Click "with audit findings" or "audit failed" → filters.

**Pod settings page,** new "App auditing" section:
- Pod-wide default cadence (one dropdown).
- Calibration mode toggle (`calibration_mode`, default on).
- Per-bot overrides table — one row per bot with a cadence dropdown.

No new top-level page. Audits live inside the Applications page UX, same way tests do.

---

## 10. Cost & telemetry

Daily aggregate at `{shared_dir}/observations/app_audit/<YYYY-MM-DD>.jsonl`, written by the admin's audit poller from ingested outbox records (and a separate admin job that scans bot-side trail files daily for trail-only events like auto_fix):
- Audits executed (count, by tier, by bot)
- Tokens spent per stage (Stage 3a + Stage 3b separately, so we can see whether triage is amortizing the discovery cost)
- Findings emitted (by severity, by outcome)
- Auto-fix actions applied
- Proposals raised; rate of operator approval / rejection / dismissal (joins the existing proposal-outcome stream)

The signal-store feedback loop (`feedback.jsonl`) becomes the load-bearing tuning signal for Stage 3b: when an audit-emitted proposal gets rejected, the rejection reason flows back as triage input for future audits of the same app.

Cost ballpark for a mid-sized pod (10 bots × 15 active apps × monthly):
- Tier 2: $0 (pure Python).
- Tier 3: ~150 audits/month × ~20k input tokens × tier2 rates ≈ $30–80/month for the whole pod.

Cheap, but the per-tick token cap in §5.3 still matters — without it, a single tick could chew through the monthly budget.

---

## 11. RSI loop integration

Audit observations feed the existing generator pipeline same way watchdog events do. Generators run pod-wide on the admin side, reading from the same Signal store the audit poller ingests into. Two specific hooks:

1. **`audit_findings_generator`** (new). Reads from the pod-wide Signal store (producer `app_structural_verifier`) and the per-bot trail.jsonl files (via ACL); emits Investigation proposals for apps where successive audits raise the same finding shape across multiple runs.
2. **`audit_drift_generator`** (new). Watches for the pattern of `auto_fix → reverted by operator within 7 days` (joined from trail entries + git history of the bot workspace). Signals that the auto-fix whitelist (§5.2) is mis-calibrated and the transformation needs to come off.

Both generators are post-PR-2 follow-ups. The audit itself ships fine without them; they're the leverage layer.

---

## 12. Migration

**PR 1 — Tier 2 structural verifier (small, low-risk):**
1. Build `audit_runner.py` (the bot-side script). Tier-2 path only in PR 1: read manifests, run §3.1 assertions, write outbox records + trail entries + manifest stamps.
2. Deploy plumbing: drop `audit_runner.py` into `/Users/<bot>/.openclaw/workspace/evolve/` during `deploy_bot`; install the Tier-2 crontab entry; set ACLs on `audits/`, `audit_inbox/`, `audit_outbox/`.
3. Sudoers grant for the admin's pod_config writes (small `/tmp/evolve-pod-config-*.json → /Users/*/.openclaw/workspace/evolve/pod_config.json`).
4. Admin-side audit poller: walk each bot's `audit_outbox/`, ingest Tier-2 records via `signals.store.observe()`, sweep-resolve, archive outbox files. Folded into the existing admin server tick.
5. Manifest fields via `migrate_manifest` (schema v11): `last_structural_verify`, `audit_trail_path`.
6. Surface Tier-2 findings on the Applications page via the existing Signal-driven status badge — no new UI in PR 1.

**PR 2 — Tier 3 semantic audit (larger, calibration-gated):**
1. Extend `audit_runner.py` with Tier-3 logic: assembles Stage 3a inputs (manifest, code files, test history, last-30 Tier-2 results, `audit_accepted[]`), dispatches to the bot's local LLM via the OpenClaw agent, persists discovery output to bot-side `audits/<app>/<audit_id>.json`.
2. Stage 3b orchestrator inside the runner: dismiss/auto_fix/propose triage. Calibration-mode gate (auto_fix → propose). Drops observations matching `audit_accepted[]` signatures.
3. Allowed-transformation executor inside the runner (§5.2). Each transformation is its own function so the whitelist is grep-able. Cross-app conflict check (§5.6) reads the bot's own manifest set.
4. Outbox shapes: `tier3_finding`, `conflict_notice`, `run_failed`. Admin poller extended to ingest these into Proposals (and Signals for `run_failed`).
5. New manifest fields: `audit_cadence`, `audit_eligible`, `audit_accepted`, `last_audit`. All nullable / safe defaults via `migrate_manifest`.
6. Pod config: `network.json → app_audit` block (`default_cadence`, `bot_cadence`, `calibration_mode`, `audit_on_critical_structural`, `tier3_tier`, rate ceilings). Sync script writes per-bot `pod_config.json` whenever `network.json` saves.
7. Install Tier-3 crontab entry (hourly scan) during deploy.
8. On-demand dispatch helper (admin-side): writes inbox file + kicks bot via the `sudo -H -u <bot> python3 audit_runner.py --pickup-inbox` path. Single function reused by CLI, UI, evo handler.
9. CLI: `evolve-admin application audit <bot> [<app>] [--all] [--ignore-accepted]` — uses the dispatch helper, polls outbox for completion.
10. API endpoint: `POST /api/applications/<bot>/<app>/audit` (with `?ignore_accepted=true`). Same dispatch helper.
11. Mark-as-accepted API: `POST /api/applications/<bot>/<app>/audit/accept` writes the signature into `manifest.audit_accepted[]` via the existing `_write_manifest_bytes` helper.
12. **evo wizard handler** (`packages/admin/evolve_admin/evo/wizard/audit_handlers.py`): implements the `evo app-audit ...` grammar (§7.4). Auth-gates by primary_user / pod admin, calls dispatch helper, emits notifications on completion.
13. Sudoers grant for the kick: `evolve ALL=(<bot_user>) NOPASSWD: /opt/homebrew/bin/python3 /Users/<bot_user>/.openclaw/workspace/evolve/audit_runner.py --pickup-inbox *` (rendered per-bot in `setup_wizard.py`).
14. Forge hook: set `audit_eligible` based on the app's shape during build (§6.1).
15. UI surfaces: tile-level audit-failed badge, manifest-view "Last audit" line, audit trail viewer (reads bot-side trail.jsonl via the existing ACL'd file reader), cadence dropdown, accepted-findings list with un-accept, "Full audit" sub-option, pod-settings "App auditing" section with per-bot table.
16. Audit-aggregate daily telemetry write (admin-side, summarizing across bots).

PR 1 is independent of PR 2 and ships first; the Signal store integration in PR 1 is the foundation Tier 3 reuses for its findings. PR 2 can be split further at the orchestrator/scheduler line if it's getting too big — the orchestrator runs synchronously via the on-demand CLI even before the scheduler exists.

---

## 13. Decisions resolved + remaining open questions

### Decisions made
1. **Ownership: bot, not Evolve.** Each bot runs its own audits via a bot-side `audit_runner.py` plus two cron entries (Tier-2 every 6h, Tier-3 hourly scan). Evolve's role is narrow: plant the runner during deploy, sync pod config, write on-demand inbox files, poll outboxes into pod-wide stores, surface in UI. No central audit daemon. (§7)
2. **Auto_fix output:** lands in the per-app audit trail (§5.4), not the Proposal inbox, not the Signals page. Manifest links to the trail; one click from the manifest view opens it.
3. **Mark-as-accepted:** writes to `manifest.audit_accepted[]`; future auto-audits drop matching observations. "Full audit" CLI flag / UI sub-option (`--ignore-accepted`) re-evaluates from scratch. Full audits auto-run at 2× the configured cadence so the accepted list gets re-checked without operator action (§5.5).
4. **Sequencing:** PR 1 (Tier 2 structural verifier inside the bot runner) ships first; PR 2 (Tier 3 semantic audit) extends the same runner with LLM logic and calibration gating.
5. **Test-exemption vs audit-eligibility:** independent fields. `test_exemption_reason` says "too trivial to test"; `audit_eligible` says "too static to auto-audit." Forge sets `audit_eligible` based on the app's shape; operators can override; manual audits run regardless (§6.1).
6. **Cadence vocabulary:** `never / quarterly / monthly / weekly / daily` (dropped `off` in favor of `never`, added `daily` for high-risk apps). Three layers: pod-wide → per-bot → per-app. Each layer's `null` value means "inherit upstream." (§6)
7. **Per-bot cadence:** first-class. `network.json → app_audit.bot_cadence: {bot_id: cadence}`. Allows raising scrutiny on a load-bearing bot without editing every manifest. Resolved into the bot's `pod_config.json` so the runner only ever sees its own bot's effective cadence. (§6, §7.1)
8. **Audit-failed surfacing:** badge on the Apps grid tile + click-through to the trail from the manifest view (§9). Doesn't sit in a separate alerts surface.
9. **Cross-app conflict awareness:** before any auto_fix touches a file used by another app, the runner stops and writes a `conflict_notice` outbox record that the admin's poller converts to an `audit_conflict_notice` Proposal (§5.6). No auto-resolution. We measure conflict volume in production before deciding whether to build a coordinator.

### Remaining open questions

1. **Concurrency.** Bot-side lockfile (`/Users/<bot>/.openclaw/workspace/evolve/.audit_runner.lock`) ensures at most one audit runs per bot at any time. Open: how should the admin UI's "Run audit now" behave when the lock is held? *Lean: surface "Queued — audit already in progress; will run on next tick" and poll the outbox for completion. CLI exits 0 with the same message.*
2. **Post-forge audit trigger.** Not a default. Whether to add it depends on whether operators find themselves manually running audit-after-forge regularly. *Lean: wait for the signal.*
3. **Audit prompts: pod-wide or per-bot?** Stage 3a / Stage 3b prompts could in theory be tuned per-bot (team-bot-a emphasizes Slack-flow correctness; team-bot-c emphasizes data-integrity). The prompts ship inside `audit_runner.py` so they're naturally per-bot if we want. *Lean: pod-wide template loaded from the synced pod_config.json for v1; per-bot override only when we have evidence the global prompt is failing for a specific bot.*
4. **Conflict-notice handling.** §5.6 says the notice is a Proposal with three suggested resolutions ("update all manifests together", "remove from dependents", "mark as accepted"). The first option implies an audit-coordinator workflow we haven't designed. *Lean: ship the notice; manually walk the operator through resolution for the first N occurrences; design the coordinator only if volume justifies it.*
5. **Calibration mode exit criteria.** §5.1 sets ≥80% acceptance rate over one full cadence cycle as the bar for promoting an auto_fix shape. Open: per-shape, per-bot, or pod-wide? *Lean: per-shape pod-wide. Shape-level evidence accumulates faster than per-bot, and the transformations themselves don't depend on which bot owns the app.*
6. **Bot-side cost visibility.** The runner uses the bot's LLM credentials, so audit costs are mixed in with the bot's other spend. Operators can't easily ask "how much did audits cost on this bot last month?" without log filtering. *Lean: tag the bot's LLM call with a metadata field `purpose=app_audit` and have the cost rollup recognize it. Defer until cost rollups land per-purpose.*
7. **Runner upgrades.** The runner script lives on the bot. When we change it (bug fix, prompt tweak), the next deploy_bot replaces it — but bots that don't get deployed will keep running the old script. *Lean: include the runner version in every outbox record; the admin poller surfaces stale-runner warnings on the Apps page so operators know which bots need redeploy.*
