---
title: "Help: Security Page"
slug: security
audience: public
last_reviewed: 2026-06-06
concepts:
  - security
  - audit
  - signals
  - exec-policy
  - sudoers
  - drift-detection
  - posture
  - auto-memory
  - intentional-deviations
  - config-intent
  - install-provenance
ui_surface: admin.security
related_specs:
  - docs/spec-config-intent-system-2026-05-21.md
---

# Help: Security Page

The Security page is the single home for every security-class signal Evolve emits. It runs a 15-minute audit (active threats and integrity violations), config-health checks (configuration that creates risk without being broken yet), and posture views over the configuration surfaces OpenClaw exposes — MCP servers, plugins, hooks, content scanned at session start, exec/sudoers permissions, and auto-memory hygiene.

**Backup / drift detection moved.** The git-backup status and recovery flow now live on the [Backup](backup.md) page. The Security audit still uses the backup's last known-good commit as its baseline for SOUL/AGENTS/HEARTBEAT/openclaw.json drift checks — the data is the same, only the configuration surface moved.

**Ask evo to investigate.** Open the chat widget from any security view and
describe what you're looking at — *"investigate the firing security signal on
team-bot-a"*, *"audit team-bot-a's morning-brief app"*, *"which bot has the worst
posture?"*. Evo can read the audit findings via `pod_state.audit`, audit
specific apps via `action.app.audit`, snooze / dismiss / resolve signals, and
apply any auth-drift proposals already in the queue. Faster than navigating
between sub-tabs for triage.

---

## Sub-tabs

### Audit

Every 15 minutes, `audit.py` runs across the pod and emits Signals for every finding. Categories:

**Identity integrity:**
- SOUL.md, AGENTS.md, HEARTBEAT.md are hash-monitored against the last git-backup commit. Mismatch fires immediately.
- Config changes to `openclaw.json` not traceable to an approved proposal are flagged.

**Gateway security:**
- **Bind address** — must be `127.0.0.1`. Anything else (especially `0.0.0.0`) is CRITICAL.
- **Exec allowlist** — unexpected new entries are flagged. Most common vector for unintended capability expansion.
- **Sudoers grant integrity** — Evolve's sudoers rules shouldn't change outside the setup wizard.

**Machine-level checks:**
- Firewall enabled · SSH `PasswordAuthentication` and `PermitRootLogin` disabled · listening ports match baseline · user accounts unchanged · OC binary mtime tracked.

**Cost anomalies:**
- Spend spikes tracked alongside security events. Compromised keys and runaway prompts surface here.

**Run Audit** triggers an immediate run; results take 15–30 seconds. The **Last run** timestamp shows the most recent completion.

### Config Health

Configuration that isn't broken but creates risk or cost: auth order, primary-model reachability, plugin version, gateway settings. Address within the week.

### MCP Posture

Cross-bot inventory of MCP servers configured in `openclaw.json → mcp.servers`. Each cell shows the server's vetting state:
- **Green** — approved (in the pod's MCP catalog with no advisories)
- **Yellow** — config drift (server present but config differs from the catalog entry)
- **Red** — unknown server (not in the allowlist) or open CVE advisory

Click **↻ Re-scan** to re-fetch advisories from the GHSA feed and re-check the inventory.

Producer: `mcp_monitor`. Signal types: `mcp_unknown_server`, `mcp_config_drift`, `mcp_server_cve_match`, `mcp_credential_binding_missing`.

Spec: `docs/spec-mcp-administration-2026-05-10.md`.

### Plugin Posture

Cross-bot view of every plugin entry. Each cell shows its enable state, role-vs-baseline classification, and **install provenance** — where the plugin came from (built-in / signed gallery / unsigned-but-allowed / unknown). Red = required-missing / denied-present / unknown install source. Yellow = unexpected-enabled, allow-list missing.

The posture reshape shipped 2026-06-04 ([PR 2279](https://github.com/evolve-ops/evolve/pull/2279) / [PR 2284](https://github.com/evolve-ops/evolve/pull/2284)) replaces the old baseline-snapshot drift signals — which fired on every plugin version bump and produced noise — with provenance-based trust. The signal now fires on "the plugin was loaded from somewhere we can't vouch for," not on "the plugin's hash changed since last week."

Producer: `plugin_monitor`. Spec: `docs/spec-plugin-posture-rework-2026-06-06.md` (current); `docs/spec-plugin-inventory-2026-05-10.md` covers the inventory layer.

### Hook Posture

Two hook surfaces per bot watched in one view:

**Webhook ingress** — the `hooks{}` block in `openclaw.json`. The baseline expects every bot to have ingress disabled by default. Enabling requires explicit operator approval through `UpdateHookBaseline`. The monitor also hashes the `transformsDir` contents and signals on any change to the supply-chain surface.

**Plugin typed hooks** — per-plugin `allowConversationAccess` and `allowPromptInjection` flags. The April 2026 incident — Team-Bot-A's evolve-plugin `allowConversationAccess` flag silently dropped to false on an OC upgrade, severing the TurnObserver pipeline — is what motivated this view. The dedicated `hook_plugin_policy_silent_disable` signal specifically catches that class.

`allowPromptInjection=true` is auto-rejected for any plugin not in the baseline's `trusted_prompt_mutators` allowlist (default: empty). `UpdateHookBaseline.set_plugin_policy` also refuses to add an expectation that requires injection unless the plugin is already trusted — closing the "add yourself and flip" loop.

Producer: `hook_monitor`. Signal types: `hook_webhook_unexpected_enabled`, `hook_webhook_mapping_changed`, `hook_webhook_transforms_dir_drift`, `hook_plugin_policy_silent_disable`, `hook_plugin_policy_unexpected`, `hook_command_gate_enabled`.

Spec: `docs/spec-hook-governance-2026-05-10.md`.

### Content Scan

Scans the markdown files each bot reads at session start (`AGENTS.md`, `SOUL.md`, `HEARTBEAT.md`, `IDENTITY.md`, `TOOLS.md`, `USER.md`, `MEMORY.md`, `README.md`) and the pod-wide `POD_CONDUCT.md` for indirect-prompt-injection patterns. Today's identity audit catches *that* a file changed; this catches *what* suspicious thing got added.

Three sub-views:

**Summary** — per-bot cards showing files scanned, files with matches, highest severity, last scan. Click a card to drill into a bot, click a file to see the match list with excerpts and ±1 line of context. Each match has a **Mark Reviewed** button that adds a 30-day suppression (per `(bot, file, pattern_id, line_range)`) and a **View raw file** modal that pulls the current file content through `sudo /bin/cat`.

**Pattern Catalog** — read-only view of the operator-curated pattern catalog at `{shared_dir}/policy/content-scan-patterns.json`. The v1 catalog ships 10 patterns:
- `html_comment_unknown` — HTML comments whose marker isn't in the Evolve-marker allowlist (handles `<!-- evolve-handoff:* -->` regions correctly, including wildcards)
- `zero_width_invisible` — zero-width / tag-style Unicode characters
- `authority_impersonation` — `System: ignore previous instructions` framings
- `instruction_negation` — classic prompt-injection negation phrasing
- `long_base64_block` / `long_hex_block` — encoded payload staging (160+ base64 chars / 64+ hex bytes outside fenced code blocks)
- `subcommand_chain_long` — SC Media's chain-bypass shape (8+ chained subcommands)
- `credential_exfil_url` — `curl`/`wget` invocations that interpolate `SECRET`/`TOKEN`/`PASSWORD`/`KEY`/`API_KEY`
- `single_line_oversize` — single lines past 2000 chars
- `structural_emptiness` — file unexpectedly short (the April 2026 team-bot-a-`AGENTS.md` 14.9KB→583B truncation class). Per-file via `applies_to`.

Edits to the catalog flow through `UpdateContentScanCatalog` proposals (`add_pattern` / `remove_pattern` / `set_evolve_markers_allowlist`).

**Suppressions** — every active Mark-Reviewed suppression across the pod, with expiry dates and the reviewer's note. **Remove** revokes a suppression; the underlying match re-fires on the next scan if still present.

Producer: `content_scan`. Signal types:
- `content_scan_match` — per-pattern severity inherited (info/warn/alert)
- `content_scan_structural_anomaly` — alert; the April truncation class
- `content_scan_file_disappeared` — alert; catalog says present but absent

The scanner is observation-only — remediation rides existing `SoulEdit`/`AgentsAppend` rails.

Spec: `docs/spec-prompt-injection-scanner-2026-05-10.md`.

### Intentional Deviations

Every active **config intent** across the pod — the operator-acknowledged "this field is deliberately set to X because Y" records that suppress drift signals from `permission_monitor` and `auth_drift_filler`.

The problem this solves: context-free drift generators ("if the field doesn't match the baseline, propose a revert") are wrong as soon as you have a deliberate per-bot deviation. Without an intent record, the same drift gets re-proposed every cycle and you reject the same proposal forever. With an intent recorded, the monitor matches the field + value + reason and suppresses the proposal.

Each row shows the bot, the field path, the recorded value, the reason you gave when you set it, and how long ago it was recorded. Per-row actions:

- **Edit reason** — update the recorded rationale if it no longer reflects reality. The intent stays in force; only the explanation changes.
- **Revoke** — drops the intent record. The next monitor sweep will see the deviation as fresh drift and emit a proposal. Useful when the deviation is no longer deliberate (e.g., you took on a per-bot tweak as an experiment, decided it was wrong, and want the standard fix back).

Revoking an intent does NOT change the underlying config field — that's a separate action via the Permissions tab.

**How intents get recorded.** Three paths:

1. **Implicit at write time** — when you flip an exec policy, edit a sudoers grant, or change any field the intent system tracks, the writer auto-stamps an intent with a short reason (e.g., "operator set exec policy = full via admin UI 2026-05-25"). This is the default — most intents land here without you doing anything special.
2. **Explicit via chat** — *"record an intent: team-bot-a exec policy stays full because the run-bash app needs it"* logs a more detailed reason than the auto-stamp.
3. **Phase 3 LLM inference** — for drift the system observes but doesn't have a paper trail for, an LLM pass infers the likely reason from session history and asks you to confirm. This shows up as a small banner on the queue ([PR 2326](https://github.com/evolve-ops/evolve/pull/2326), Phase 4.1 queued banner) — clicking through walks you through a one-question confirm.

The system errs on the side of asking. Phase 3.1 added activity context so the inference has more to work with; Phase 4.1 added the queued banner so the work doesn't pile up invisibly.

Spec: [docs/spec-config-intent-system-2026-05-21.md](../spec-config-intent-system-2026-05-21.md).

---

## Severity Levels

| Icon | Level | Meaning |
|------|-------|---------|
| ❌ | CRITICAL | Immediate action required. Gateway exposed, identity compromised, new unknown user account. |
| ⚠️ | HIGH | Significant risk. Should be addressed within 24 hours. |
| ⚠️ | MEDIUM | Moderate risk. Address within the week. |
| ℹ️ | LOW | Minor issue or informational. |

---

## The Eight Hard Security Rules

These rules apply to all improvement proposals. Proposals violating any of these are auto-rejected — they never reach human review:

1. No gateway binding to `0.0.0.0`
2. No disabling auth
3. No modifying Evolve's own scripts
4. No writing credential files
5. No `sudo` in proposed scripts
6. No outbound network calls in proposed scripts
7. No writes outside the target bot's workspace
8. No modifying launchd plist files

---

## Common Questions

**The audit shows "SOUL.md hash changed" — is this serious?**
Yes. SOUL.md contains the bot's behavioral constraints. If it changed outside the proposal pipeline, something modified it directly. Compare the current file against the last git backup to see what changed. If Content Scan also fires on the same file, both signals appear as a unified incident on the file's detail page — that combination is the strongest evidence that the change was suspicious rather than benign.

**Gateway is bound to 0.0.0.0 — how do I fix it?**
Open the bot's `openclaw.json` and set `gateway.bind` to `"127.0.0.1"`. Restart the gateway from Maintenance → Status. CRITICAL — a gateway on 0.0.0.0 is reachable from anything on your local network.

**Content scan fired on a benign HTML comment — what do I do?**
Click the match's **Mark Reviewed** button. A modal asks for an optional note; the suppression sticks for 30 days. The match won't re-fire on subsequent scans unless the file changes significantly above the suppressed line range. If you find yourself marking the same false-positive class repeatedly, consider proposing a catalog tweak through `UpdateContentScanCatalog` — operator-curated patterns are how you tune precision.

**A messaging plugin's `allowConversationAccess` silently went to false — what fired?**
That's the `hook_plugin_policy_silent_disable` signal. It specifically catches the April 2026 incident class: a required `allowConversationAccess=true` flag dropping to false on an enabled plugin (typical cause: OC upgrade resetting the flag). Re-set via `openclaw.json` and redeploy, or apply the auto-proposal the hook monitor emits.

**An MCP server I haven't seen before is configured on a bot — alert or expected?**
The `mcp_unknown_server` signal fires for any `mcp.servers` entry not in the pod's MCP catalog. Two valid resolutions: (a) add the server to the catalog via `UpdateMcpCatalog` if it's legitimate (and the package is clean of advisories), or (b) issue an `UninstallMcpServer` proposal to remove it. The Posture cell stays red until reconciled.

**A new macOS user account appeared — what should I do?**
Investigate immediately. If you didn't create it, this is a potential persistence mechanism. Check System Settings → Users & Groups. If the account matches a bot you just added via the setup wizard, it's expected and you can dismiss the finding.

**How do I see the full git backup diff?**
Backup configuration and history moved to the [Backup](backup.md) page (Cloud subtab → per-bot commit history). You can also compare directly via `git diff HEAD~1 HEAD` in the backup repo or browse commits on GitHub.
