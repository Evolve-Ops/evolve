# Audit Extensions — scanner gap + substrate audit + `evo fail` (2026-05-17)

Status: **proposed**. Companion to [spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md), which shipped the 3-tier app audit framework (#1191 Tier 2, #1194 Tier 3). This spec extends that framework along three axes — closing a scanner-side gap that lets a class of silent failure through, adding substrate coverage for the layers app-audit doesn't reach (skills, OAuth providers, pod infrastructure), and adding a user-initiated on-demand investigation flow (`evo fail`) so failures the auditor can't predict can still be diagnosed in 30 seconds.

**One core principle threads all three workstreams:** *Every recurring behavior must be expressible as a manifest contract — regardless of whether it lives in a script, a cron, or a heartbeat section.* The existing audit framework verifies contracts against reality. It can only catch what's been claimed. The protein-reminder failure (§1.1) revealed that the scanner is too generous about which behaviors get claimed.

---

## 1. Motivating failures

### 1.1 The protein-reminder failure (April 2, 2026)

**What happened.** A user asked the bot admin-bot to track protein intake with a 6 PM daily tally. The implementation: a heartbeat section that read `journal/protein.md` and posted a summary at 6 PM. The journal file lived under a `Health Tracking` app the scanner had grouped. On April 2, the heartbeat got overwritten and the reminders stopped. No signal. No audit finding. No user-visible alert. The user discovered it days later by noticing the absence of reminders.

**Why nothing caught it.** The scanner discovered `journal/protein.md` and bundled it into a `Health Tracking` manifest correctly. But it did not extract the **heartbeat behavior** (6 PM trigger + read protein journal + post summary) as a declared contract. So:

- Tier 2 had no assertion to verify — no manifest claim said "the heartbeat must reference protein journal at 6 PM"
- Tier 3 had no observable claim to audit — `Health Tracking`'s description didn't mention the reminder
- The signal store had no failure signal because the heartbeat doesn't emit `BRIEFING_FAILED:`-style structured signals; it just stops running quietly

**Why this matters generally.** Heartbeat content (`AGENTS.md`, `SOUL.md`, `HEARTBEAT.md`, `POD_CONDUCT.md`) is the bot's standing-routine layer. It's an inherently lossy surface — vulnerable to:

- Silent clobber during agent self-modification (RSI gone awry)
- Deploy-time replacement that drops user-added sections
- Manual edits that accidentally truncate (the team-bot-a AGENTS.md 14.9KB→583B incident, [memory](../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_team-bot-a_agents_md_truncation_2026_04_22.md))
- LLM context-pruning that removes sections during a rewrite

The audit framework can defend against all of those — but only if the behavior is a declared contract. Today, scheduled-action behaviors embedded in heartbeats are often not promoted to contracts. **That's the gap.**

### 1.2 The non-app failure surfaces

Two more classes of failure the existing app-audit doesn't see:

**Skill-level failures.** Gmail token rotates and the refresh path silently returns empty results. Slack workspace removes the bot's OAuth grant and the slack skill starts erroring. Calendar API changes a field name and the calendar skill quietly stops returning conflicts. These are not app failures — they're skill-substrate failures that cascade into app failures the next time an app invokes the skill. The current audit only sees the app contract; it doesn't audit the skill's own contract.

**Pod-infrastructure failures.** Sudoers entry gets corrupted during a fix. Repo-puller daemon stops because LaunchDaemon plist references a renamed script. TCC permissions get revoked when the OS prompts the user and they accidentally click Don't Allow. Network.json schema migration leaves a stale field. All silent until something downstream breaks.

**User-experienced novel failures.** The user notices that something stopped working — but it doesn't map cleanly to one app, one skill, or one piece of infrastructure. Maybe it's a combination. Maybe it's a config that interacted with a recent OS update. Without a fast triage path, the user has to wait for the operator to investigate or for the next audit cycle to maybe catch it.

---

## 2. The three workstreams

This spec covers three workstreams that ship in parallel. They share the existing audit substrate (runner, dispatch, poller, trail, Proposals, `evo` family) and coordinate at the manifest-schema level.

| | Workstream A | Workstream B | Workstream C |
|---|---|---|---|
| **What** | Scanner fix — extract scheduled-action contracts from heartbeats/crons | Substrate audit — skills, OAuth providers, pod infrastructure | `evo fail` investigation — user-reported failure → bot-driven diagnosis |
| **Touches** | `applications/scanner.py`, manifest schema | New `skill_audit.py`, new `infra_audit.py`, new pod-side runner | New `evo/handlers/fail.py`, new investigation runner mode |
| **Reuses** | Existing manifest store | Existing runner + dispatch + poller + trail | Existing runner + dispatch + poller + arbiter |
| **Output** | Richer manifests + new schema fields | Findings in arbiter + per-element trails | Direct reply in messaging thread + signal-log trace |
| **Risk profile** | Low — additive scanner enhancement, no runtime changes | Medium — new audit kinds, new runner code paths | Low — new handler + small runner extension |

Sequencing: ship A in parallel with B+C. A's deliverables are scanner-side and don't require runner changes; B and C are runner extensions. All three can land in the same sprint via parallel agents in separate worktrees.

---

## 3. Workstream A — Scanner gap fix

The scanner must extract every recurring behavior described in the bot's standing-instruction surfaces and represent it as a structured manifest claim that audits can verify.

### 3.1 What changes in the scanner

Current behavior: the LLM-discovery stage reads `AGENTS.md`, `SOUL.md`, `HEARTBEAT.md`, `POD_CONDUCT.md`, plus scripts and data files, groups them into apps, and writes manifests with `description`, `usage`, `files[]`, etc. It does not consistently extract scheduled-action contracts from the standing-instruction surfaces.

New behavior: a dedicated **scheduled-action extraction pass** runs as part of the scanner pipeline. Its job is to find every place the bot is told to do something periodically — heartbeat sections, cron triggers, AGENTS.md "every morning at X" instructions, POD_CONDUCT scheduled directives — and emit them as structured `scheduled_actions[]` entries on the appropriate app's manifest.

Each `scheduled_actions[]` entry declares:

```jsonc
{
  "id": "protein-6pm-tally",
  "trigger": {
    "kind": "heartbeat | cron | launchd | session_start",
    "schedule": "18:00 daily",
    "evidence_path": "HEARTBEAT.md",
    "evidence_locator": "section 'Daily routines', lines 42-48"
  },
  "inputs": [
    {"path": "journal/protein.md", "kind": "data_file"}
  ],
  "outputs": [
    {"kind": "messaging_channel", "channel": "configured"}
  ],
  "summary": "Reads protein journal entries for the day and posts a tally to the user's messaging channel."
}
```

These entries are the audit contracts. Tier 2 verifies the trigger evidence still exists at the cited locator; Tier 3 verifies the LLM observation that the implementation still matches the summary.

### 3.2 Manifest schema v13

Schema v13 adds three top-level keys on each app manifest:

- `scheduled_actions[]` — array of the structures above. Empty if the app has no scheduled behaviors.
- `heartbeat_evidence` — `{file_path, section_anchors[]}` — explicit references to the heartbeat surfaces the app's behaviors live in. Tier 2 verifies these surfaces still contain the anchors.
- `cron_evidence` — `{labels[]}` — labels of LaunchDaemon plists or crontab entries the app relies on, beyond the existing `crons[]` field (which is for app-owned crons). This captures crons declared elsewhere (e.g., heartbeat-fired) that the app depends on.

Migration: `migrate_manifest` adds the new fields as empty lists / null. Existing manifests continue to validate; the new structural assertions just have nothing to check until the scanner re-runs and populates them.

### 3.3 New structural assertions (Tier 2)

Six new assertions on the existing structural verifier:

1. Every `scheduled_actions[*].trigger.evidence_path` exists on disk. Severity: `critical` if missing.
2. Every `scheduled_actions[*].trigger.evidence_locator` resolves to a present section / line range in the evidence file (string match on the cited anchor text). Severity: `major` if the anchor is gone.
3. Every `scheduled_actions[*].inputs[*].path` exists (unless `kind=external`). Severity: `major` if missing.
4. Every `heartbeat_evidence.section_anchors[*]` is present in the cited heartbeat file. Severity: `critical` if missing (this is the protein-reminder catch).
5. Every `cron_evidence.labels[*]` is loaded via `launchctl list` or appears in `crontab -l`. Severity: `major` if missing.
6. Heartbeat-file checksum drift detection: each scheduled-action entry stores the sha256 of the cited evidence section at scan time; if the section's content sha changes by more than a threshold (say 50% of bytes differ) between audit runs, emit a `minor` finding so the operator can confirm the change was intentional.

These run on the existing 6-hour Tier-2 cadence. No new daemon.

### 3.4 LLM prompt changes (scanner discovery)

The scanner's existing LLM-discovery prompt gets a new section instructing the model to extract scheduled-action contracts. The prompt template should emphasize:

- Every recurring behavior described in any of the standing-instruction surfaces must produce a `scheduled_actions[]` entry on the most-relevant app
- Behaviors that don't map to an existing app should create a new app entry (the LLM is permitted to propose new apps for orphan behaviors)
- The `evidence_locator` field must be specific enough that Tier 2 can verify it (e.g., a section heading or a unique phrase, not just a line number, since edits shift lines)

The behavior of "create a new app for an orphan behavior" is the right shape for the protein-reminder case in retrospect: the scanner should have proposed a `Protein Reminder` app entry with `scheduled_actions[]` populated, owned by the heartbeat surface. Whether that app gets merged into `Health Tracking` later is a manifest-organization decision; the contract claim has to exist first.

### 3.5 Re-scan policy

After this lands, every bot needs a one-time scanner re-run to populate the new fields. The existing on-demand scan flow handles this; we surface a "needs re-scan" pill on the Applications page for any bot whose latest scan predates this schema. Auto-scheduled re-scan via the existing cadence will pick the rest up.

### 3.6 Deliverables

1. `applications/scanner.py` — new extraction pass + LLM prompt updates
2. Manifest schema v13 migration (`applications/manifest.py`)
3. New Tier-2 assertions (`app_audit_structural.py`)
4. UI badge on Applications page for bots needing re-scan
5. Tests — extraction-pass unit tests, schema migration, Tier-2 assertions with synthetic heartbeat fixtures, the protein-reminder regression case as a fixture
6. Help page entry: "How Evolve tracks the things your bot does on a schedule"

---

## 4. Workstream B — Substrate audit

The existing audit framework is scoped to applications. Workstream B extends it to three substrate layers: skills (per-bot), OAuth providers (per-bot), and pod infrastructure (admin-side, pod-wide).

### 4.1 Skill audit (per-bot)

**Scope.** Each skill installed on each bot: gmail, calendar, slack, discord, telegram, imessage, obsidian, notion, linear, autocad, home_assistant, runway, apple_local, upstream_plugin_skills (~15 today).

**What gets audited.**

- **Configuration.** Does `openclaw.json` carry the expected entries for this skill? Are required scopes present?
- **Credentials.** OAuth tokens present and not expired? Refresh tokens valid? Scope drift?
- **Permissions.** macOS TCC for skills that need it (iMessage), ACL state for shared resources.
- **Code.** Does the skill's install module still compile, import, and expose the expected functions? Does it match its synthetic manifest (see §4.4)?
- **Recent invocations.** Did the skill emit success / failure signals in the last audit window? If it was last invoked weeks ago, is that expected?

**Ownership.** Bot-side, mirroring app audit. Skills are pod-wide code, but they're installed and credentialed per-bot. The audit runs on the bot, using the bot's own LLM credentials, asking "is THIS bot's gmail skill set up correctly and likely to work the next time an app calls it?"

**Cadence.** Same model as app audit (`never / quarterly / monthly / weekly / daily`), with a per-skill default of `weekly` since skills are higher-criticality than most apps (any app calling a broken skill fails).

### 4.2 OAuth provider audit (per-bot)

**Scope.** Each OAuth provider configured for each bot: ~8 providers today (google, slack, discord, telegram, notion, linear, github, ...).

**What gets audited.**

- Provider config present and well-formed in `auth-profiles.json`
- Tokens not approaching expiry (>30 days remaining)
- Refresh flow tested in the last N days (synthetic refresh, doesn't actually rotate)
- Scopes match what dependent skills claim to use
- Provider's upstream API hasn't broken our integration (light schema probe)

**Ownership.** Bot-side, same runner shape as skills.

**Cadence.** Default `weekly`. Credentials are the most-failure-prone layer (token rotation, scope changes, OAuth-app revocation).

### 4.3 Pod-infrastructure audit (admin-side)

**Scope.** Pod-wide infrastructure that isn't bot-owned: the admin-ui daemon, mcp-bridge, verify daemon, repo-puller, signal-store retention, sudoers entries, ACL state on shared directories, `network.json` schema validity.

**What gets audited.**

- Each managed daemon's LaunchDaemon plist exists, loads, and is currently running
- Sudoers entries are present and uncorrupted (no `visudo -c` errors)
- ACLs on `/Users/Shared/evolve/` and per-bot `.openclaw/` paths match expected state
- `network.json` validates against current schema; no orphan keys; required-bot config consistent across the file
- Repo-puller's last successful pull is recent (< 30 minutes)
- Signal store retention job has run in the last 24 hours

**Ownership.** Admin-side. Pod infrastructure isn't bot-owned, so the existing bot-side runner can't audit it. This is a new admin-side audit module that uses the same outbox/poller pattern (admin writes to its own audit_outbox/, ingests into the same Signal/Proposal stores).

**Cadence.** Default `daily`. Infrastructure failures cascade fast.

### 4.4 Synthetic manifests for non-app elements

Skills and OAuth providers don't have manifests today. The audit framework needs a contract to verify against. Two options:

- **Option A:** Add an explicit `skill_manifest.json` / `provider_manifest.json` alongside each install module.
- **Option B:** Treat the install module's docstring + the module's public interface as the implicit contract. The auditor reads the module, extracts the docstring, and treats stated behaviors as claims.

**Recommendation: Option B for v1, Option A as a follow-up.** Option B avoids a manifest-authoring burden across 15+ skills + 8+ providers, and the docstrings on the existing modules are already detailed enough to audit against. Where docstrings are thin (rare), the auditor surfaces a `manifest_thin` finding suggesting the docstring be expanded — natural feedback loop.

### 4.5 Substrate trail format

Each audited element gets a trail file, parallel to the app trail:

- Skills: `/Users/<bot>/.openclaw/workspace/evolve/skill_audits/<skill>/trail.jsonl`
- Providers: `/Users/<bot>/.openclaw/workspace/evolve/provider_audits/<provider>/trail.jsonl`
- Pod infrastructure: `{shared_dir}/infra_audits/<element>/trail.jsonl`

Same record shapes as app trail (`audit_run`, `mark_accepted`, `auto_fix`, `conflict_notice`, `run_failed`). Same retention (365 days). Same UI viewer (the existing trail-viewer modal extended with element-type awareness).

### 4.6 Deliverables

1. `analyzer/skill_audit.py` — runner extension that audits skills; reuses Tier 2/3 patterns
2. `analyzer/provider_audit.py` — same shape for OAuth providers
3. `admin/applications/infra_audit.py` — admin-side audit module for pod infrastructure
4. Manifest schema additions for substrate audit fields (`skill_audit_cadence`, etc. — keyed by element)
5. Pod config sync extension (existing `pod_config.json` carries new substrate-audit settings)
6. Audit poller extension to ingest new outbox record kinds (`skill_finding`, `provider_finding`, `infra_finding`)
7. UI surfaces:
   - Skills page: per-skill trail viewer + "Run audit" button + cadence dropdown
   - Settings → OAuth providers: same
   - Pod settings → Pod health: infrastructure-audit summary + trail viewer
8. Tests — runner extensions, poller extensions, UI render tests, integration tests with synthetic skill / provider / infra state
9. `evo` keyword extension: `evo audit skill <name>`, `evo audit provider <name>`, `evo audit infra` for on-demand

---

## 5. Workstream C — `evo fail` investigation

User-initiated investigation flow. The user reports a failure in conversation; the bot investigates and replies with a diagnosis. Direct reply in the messaging thread — no UI surface (per design decision, the diagnosis lives where the conversation lives).

### 5.1 Grammar

| Form | Effect |
|---|---|
| `evo fail <description>` | Starts an investigation. Immediate reply: "Looking into it." Diagnosis lands as a follow-up message when complete. |
| `evo fail recent` | Re-investigates the most-recent failure. Useful when the user wants a fresh look or the first diagnosis wasn't actionable. |
| `evo fail status` | Reports status of any in-flight investigation. |

**Auth model.** Same as `evo audit`: primary user + pod admins. Other users on team bots get the "not for you" reply.

### 5.2 Investigation flow

1. User types `evo fail morning briefing didn't arrive today` in the bot's messaging channel.
2. Handler in `evo/handlers/fail.py` validates auth, generates an investigation ID, writes an investigation request to the bot's `audit_inbox/investigation-<id>.json` with the user's description + timestamp.
3. Handler kicks the runner via the existing sudo dispatch (same kick as audit).
4. Bot replies immediately: *"Looking into it — I'll let you know what I find."*
5. Runner picks up the investigation request and runs a **two-stage investigation**:
   - **Stage 1 — Triage**: Read the user's description, last 24h of signals (`*_FAILED`, `*_PARTIAL`, anomalies), recent app+skill+infra trail entries, the affected bot's recent watchdog events. Identify the most-likely culprit element (app, skill, infrastructure, scheduled action). Output: a candidate-list with confidence scores.
   - **Stage 2 — Diagnosis**: Run a focused audit on the top candidate (full Tier-3 prompt + the specific complaint as additional context). Output: a diagnosis with evidence pointing at the root cause + a proposed fix.
6. Runner writes diagnosis to outbox.
7. Admin poller picks up the outbox record; emits the diagnosis directly to the user via the existing forge_complete-style notification queue.
8. Bot replies in-thread with the diagnosis. Example:

> *"I checked. Your morning briefing didn't arrive today because the gmail OAuth token expired 3 days ago and the refresh path is silently returning empty results — the briefing thought it had no emails to summarize and skipped sending. To fix: regenerate the gmail token in Settings → OAuth providers. The audit trail and a step-by-step fix are at: [trail link]. Want me to flag this for the operator?"*

### 5.3 What the diagnosis carries

The reply is plain-language but the underlying record (in the trail) carries structured fields:

- `investigation_id`
- `user_description` (verbatim)
- `triage_candidates[]` — what the runner considered + confidence scores
- `chosen_candidate` — the element the diagnosis focused on
- `evidence[]` — file:line citations, signal IDs, log excerpts
- `diagnosis` — the LLM's plain-language explanation
- `suggested_fix` — concrete remediation step
- `confidence` — `low | medium | high`
- `related_signal_ids[]` — any motivating signals
- `next_actions[]` — operator follow-ups the user can trigger (e.g., "flag for operator", "run full audit on the affected element")

The reply renders a subset (diagnosis + suggested fix + confidence + an inline link to the trail). The full record is available via `evo fail history` or in the trail-viewer UI for operators.

### 5.4 No-diagnosis path

When the runner can't confidently diagnose, the reply is still useful but honest:

> *"I checked but couldn't pinpoint a single cause. Here's what I did look at: recent signals (no failures), the gmail skill (looks OK), the morning briefing app (last audit clean 4 days ago). One thing to try: ask `evo audit morning-briefing full` to force a re-audit and see if that surfaces anything. If you want me to flag this for the operator, reply `evo fail flag`."*

This matches the calibration discipline of the existing audit — don't over-claim. A confident wrong diagnosis is worse than a humble "I don't know."

### 5.5 Falling back to the operator

If the user replies `evo fail flag` after a no-diagnosis or unsatisfying response, the runner emits an `investigation_unresolved` Proposal to the arbiter so an operator can investigate manually. This is the explicit hand-off path — the user has tried self-diagnosis, it didn't resolve, and the operator now has a structured ticket with the full investigation context.

### 5.6 Deliverables

1. `evo/handlers/fail.py` — handler implementing the grammar above
2. `applications/audit_dispatch.py` — new `request_investigation()` helper
3. `analyzer/app_audit_runner.py` — new `--investigate` mode that runs the two-stage investigation
4. Investigation prompt templates (Stage 1 triage + Stage 2 diagnosis) co-located with the runner
5. `applications/audit_poller.py` — new outbox record kinds (`investigation_diagnosis`, `investigation_unresolved`)
6. Notification template for the in-thread reply (plain text + Plex-tested)
7. Tests — handler grammar, runner mode, dispatch, poller, e2e investigation with synthetic failure
8. Help page accordion: "Reporting a failure with `evo fail`"

---

## 6. Sequencing + parallelism

Three workstreams, three agents, one sprint.

**Coordination point:** the manifest schema migration (Workstream A, §3.2). All three workstreams should consume the v13 schema. Workstream A owns the migration; B and C reference the new fields where relevant (B's substrate audits will eventually adopt the `scheduled_actions[]` pattern; C's investigation reads them as evidence).

**Suggested agent assignment:**

| Agent | Workstream | Worktree |
|---|---|---|
| Agent 1 | A — Scanner + schema | `.claude/worktrees/audit-scanner-fix` |
| Agent 2 | B — Substrate audit (skill + provider + infra) | `.claude/worktrees/audit-substrate` |
| Agent 3 | C — `evo fail` investigation | `.claude/worktrees/evo-fail` |

Each agent gets its own scope doc (this spec's relevant sections) + the existing app-audit spec as required reading + the bot-sovereign architecture principles. Cross-workstream coordination happens in code review (the manifest schema PR lands first, the others rebase).

**Timeline estimate.** Each workstream is roughly 4–6 days of focused work for a single agent.

---

## 7. Calibration + risk

The existing app-audit spec invested heavily in calibration discipline (calibration mode, rate ceilings, mark-as-accepted, full-audit re-check). All three new workstreams inherit those defaults unchanged:

- New audit kinds (skill, provider, infra) ship in calibration mode (auto_fix → propose for the first cadence cycle)
- Rate ceilings apply per-element-type, not just per-app
- `evo fail` investigation does NOT have auto-fix at all — its output is always a diagnosis reply or a Proposal, never an automated change

**Specific risks to monitor:**

1. **Scanner over-extraction.** The new extraction pass might create spurious `scheduled_actions[]` entries from prose that mentions "daily" or "every morning" but isn't actually a scheduled action. Mitigation: every extracted action requires an `evidence_locator` that resolves to a specific text section the LLM cites; the Tier-2 anchor-check fails noisily if the locator is bogus, surfacing scanner over-extraction as a finding.
2. **Substrate audit noise.** Skills audited weekly produce a lot of records. Mitigation: the calibration-mode + rate-ceilings story applies; if Stage 3b finds Stage 3a is over-firing, tighten the prompt.
3. **`evo fail` false confidence.** The LLM might confidently diagnose the wrong root cause. Mitigation: §5.4's no-diagnosis path; explicit confidence reporting in the reply; the `evo fail flag` escalation path.
4. **Heartbeat anchor brittleness.** If the operator renames a heartbeat section, the `evidence_locator` may stop resolving and trip a false `critical`. Mitigation: the anchor is a unique phrase, not a line number, so most rewordings still match. When a rewording does shift the anchor, the finding is a single Proposal the operator dismisses or marks-accepted with a one-click.

---

## 8. Open questions

1. **Manifest v13 anchor format.** Section heading? Heading + first 50 chars? sha of a normalized form? *Lean: heading text + sha of the section's content at scan time; Tier 2 matches on heading; sha drift triggers `minor` to nudge re-scan.*
2. **Investigation TTL.** Should investigations expire? Yes — 30 days, then trail entries roll into archive. *No carry-forward, fresh investigations start clean.*
3. **`evo fail` rate limit.** Should we throttle to prevent a panicked user from spamming `evo fail`? *Lean: 3 investigations per user per hour soft cap; 4th gets "I'm still working on your previous reports — let me finish those first."*
4. **Substrate audit on calibration mode.** Default-on like app audit? *Yes — same rationale, no auto-fix until shape-by-shape promotion.*
5. **Infra audit conflict scope.** §4.3's infra audit might want to auto-fix things like a corrupted sudoers entry. *No — same calibration discipline; first cycle always escalates to Proposal even when the fix is "obvious."*

---

## 9. Acceptance criteria

The sprint is done when:

- Scanner re-run on the test pod produces `scheduled_actions[]` entries for every periodic behavior in heartbeats / crons / standing instructions. The protein-reminder failure scenario (heartbeat clobber) reproduces and Tier 2 emits the expected `critical` finding.
- Substrate audit runs cleanly on the test pod for every shipped skill + provider; admin-side infra audit runs cleanly on pod infrastructure. Trails populate; Proposals surface in the arbiter for any genuine findings.
- `evo fail` round-trips end-to-end: user reports failure in messaging channel → bot replies immediately → diagnosis arrives within typical investigation window (target: 2 minutes for most cases). No-diagnosis case behaves per §5.4.
- All existing app-audit tests still pass; ~30 new tests across the three workstreams.
- Help page has accordion entries for each of the three new operator-visible features.
- Pre-existing failure modes from the open-questions list (§8) are handled per the leans.

---

## 10. Related docs

- [spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md) — the existing 3-tier app audit framework that this extends
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — Signal/Proposal store integration
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — generator pipeline that consumes audit findings
- [project_team-bot-a_agents_md_truncation_2026_04_22](../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_team-bot-a_agents_md_truncation_2026_04_22.md) — prior incident of the same class as the protein-reminder failure (silent heartbeat-surface clobber)
