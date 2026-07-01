# Evolve — Configuration Reference

Full reference for `network.json`. The canonical copy lives at `/Users/Shared/evolve/network.json`.

---

## Full example

```json
{
  "networkId": "my-network",
  "primary": "bot1",
  "forge": "forge",
  "members": ["bot1", "bot2", "bot3"],
  "sharedDir": "/Users/Shared/evolve",

  "thresholds": {
    "dailySpendAlertUsd": 5.0,
    "weeklySpendAlertUsd": 20.0,
    "spendCapAction": "downgrade-tier",
    "maxSessionContextTokens": 100000
  },

  "models": {
    "tiers": {
      "tier0": {
        "name": "Judge",
        "models": ["openai/gpt-4o"],
        "fallbacks": [],
        "policy": "Cross-model evaluation only. Must differ from tier2 provider.",
        "costClass": "medium"
      },
      "tier1": {
        "name": "Power",
        "models": ["anthropic/claude-opus-4-6"],
        "fallbacks": [],
        "policy": "Explicit user request only. Never background tasks.",
        "maxPerDayPerBot": 10,
        "costClass": "high"
      },
      "tier2": {
        "name": "Workhorse",
        "models": ["anthropic/claude-sonnet-4-6"],
        "fallbacks": ["openai/gpt-4o"],
        "policy": "Default for user-facing conversations.",
        "costClass": "medium"
      },
      "tier3": {
        "name": "Grunt",
        "models": ["anthropic/claude-haiku-4-5"],
        "fallbacks": ["openai/gpt-4o-mini", "google/gemini-2.0-flash"],
        "policy": "Background tasks: analysis, testing, judging, summarization.",
        "costClass": "low"
      }
    },
    "perBot": {
      "forge": { "defaultTier": "tier3" },
      "security-bot": { "defaultTier": "tier3" }
    }
  },

  "security": {
    "mode": "primary",
    "botId": null,
    "autoRejectRisk": ["high", "critical"],
    "rulesFile": "/Users/Shared/evolve/security_rules.json"
  },

  "heal": {
    "failuresBeforeProposal": 3,
    "windowHours": 24,
    "slowThresholdMs": 3000,
    "restartCooldownMin": 10,
    "checkTimeoutSec": 5
  },

  "alerts": {
    "channel": "telegram",
    "chatId": "YOUR_TELEGRAM_CHAT_ID"
  },

  "bots": {
    "bot1": { "role": "member",  "port": 19001, "user": "bot1", "expectedModel": "anthropic/claude-sonnet-4-6" },
    "bot2": { "role": "member",  "port": 19002, "user": "bot2" },
    "bot3": { "role": "member",  "port": 19003, "user": "bot3", "service_domain": "gui/503" },
    "evo":  { "role": "primary", "port": 19030, "user": "evolve" }
  }
}
```

---

## Top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `networkId` | string | Unique name for this Evolve network. Used in reports and UI. |
| `primary` | string | Bot ID of the primary analyzer. Runs analyze.py, cost.py, and the other primary-side analyzer jobs. |
| `forge` | string | Bot ID of the Forge validation instance. |
| `members` | string[] | All bot IDs in the network (including primary and forge). |
| `sharedDir` | string | Absolute path to the shared data directory. Default: `/Users/Shared/evolve`. |

---

## `thresholds`

Spend alerting and session-size guardrails. Read by `spend_alert.py`,
`spend_caps.py`, and the admin server's health endpoints.

| Field | Default | Description |
|-------|---------|-------------|
| `dailySpendAlertUsd` | `5.0` | Telegram alert when any bot's daily spend exceeds this. |
| `weeklySpendAlertUsd` | `20.0` | Telegram alert when pod-wide weekly spend exceeds this. |
| `dailySpendCapUsd` | `null` | Hard daily cap (per bot). `null` = no cap. When hit, `spendCapAction` fires. |
| `weeklySpendCapUsd` | `null` | Hard weekly cap (pod-wide). `null` = no cap. |
| `spendCapAction` | `"downgrade-tier"` | What to do when a cap is hit: `alert-only` \| `downgrade-tier` \| `pause-crons` \| `suspend-bot`. |
| `maxSessionContextTokens` | `100000` | Sessions above this input-token total are flagged in the health report. |

---

## `models`

### `models.routing`

Controls session-aware model routing. Optional — defaults are sensible.

| Field | Default | Description |
|-------|---------|-------------|
| `maintenance_tier` | `"tier3"` | Tier used for `maintenance`-classified sessions. Override to `"tier2"` if you want full Sonnet for debugging. |

```json
"models": {
  "routing": {
    "maintenance_tier": "tier2"
  }
}
```

### `models.tiers`

Defines the four model tiers. Each tier:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable name (Judge, Power, Workhorse, Grunt). |
| `models` | string[] | Ordered list of primary models. First = preferred. |
| `fallbacks` | string[] | Fallback models if primary is unavailable. |
| `policy` | string | Usage policy description (informational). |
| `costClass` | string | `"low"` / `"medium"` / `"high"` — used in cost reporting. |
| `maxPerDayPerBot` | int | (tier1 only) Max uses per bot per day. Enforced by `check_daily_limit()`. |

**Important:** `tier0` (Judge) **must** use a different provider than `tier2` (Workhorse). This prevents self-evaluation bias. If you switch tier2 to Google, update tier0 to Anthropic or OpenAI.

### `models.perBot`

Per-bot overrides. Currently supports `defaultTier` (the tier used for background tasks on that bot).

```json
"perBot": {
  "forge": { "defaultTier": "tier3" }
}
```

---

## `security`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | string | `"primary"` | `"primary"` or `"dedicated"`. Dedicated runs review.py on `botId`. |
| `botId` | string\|null | `null` | Security bot ID (only used when mode=dedicated, e.g. `"security-bot"`). |
| `autoRejectRisk` | string[] | `["high","critical"]` | Risk levels that are auto-rejected without human review. |
| `rulesFile` | string | `"/Users/Shared/evolve/security_rules.json"` | Path to static security rules. Never modify via proposal. |

---

## `heal`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `failuresBeforeProposal` | int | `3` | Gateway failures in window before generating investigation proposal. |
| `windowHours` | int | `24` | Rolling window for failure counting. |
| `slowThresholdMs` | int | `3000` | Response time above this = "slow" incident (not restart, just logged). |
| `restartCooldownMin` | int | `10` | Minimum minutes between restart attempts on the same bot. |
| `checkTimeoutSec` | int | `5` | HTTP health check timeout in seconds. |

---

## `alerts`

| Field | Type | Description |
|-------|------|-------------|
| `channel` | string | Messaging channel (`telegram`, `slack`, etc.). |
| `chatId` | string | Chat ID or username for alert delivery. |

---

## `bots`

Per-bot metadata. Keys are bot IDs.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | yes | `"primary"`, `"member"`, `"forge"`, `"security"`. |
| `port` | int | yes | Gateway port. Used by heal.py for health checks. |
| `user` | string | no | macOS username for this bot. Defaults to bot ID if omitted. |
| `service` | string | no | launchd service label. Defaults to `ai.openclaw.{bot_id}-gateway`. |
| `service_domain` | string | no | launchd domain override for gateway restart. See below. |
| `expectedModel` | string | no | Expected tier2 model. cost.py alerts on drift. |
| `securityScanning` | bool | no | Default `true`. Set `false` to opt this bot out of `security_warden` raw-transcript capture. See "Security scanning" below. |

### Security scanning

Each bot's plugin captures the user side of recent turns into a rolling buffer at `{sharedDir}/metrics/{botId}/recent-transcripts.json` (200 entries OR 48h, whichever caps first). The `security_warden` generator scans this buffer for credential exposures (API keys, tokens, SSH private keys, password literals) and proposes a critical-severity Investigation when one is found, so the user is prompted to rotate.

The buffer holds **raw user text** so the credential scanner can detect secrets verbatim. Assistant replies are NOT captured. The file is owned by the bot's user and readable only by the `evolve` admin user via macOS ACL.

To opt a bot out (e.g., a personal bot serving a single user who doesn't want any raw text persisted):

```json
"bots": {
  "personal-bot": { "role": "member", "port": 19010, "securityScanning": false }
}
```

When `securityScanning` is `false`, no buffer file is written and `security_warden` runs against an empty input — it stays inert for that bot.

### Gateway restart and `service_domain`

When restarting a gateway, Evolve uses the following priority order to find the right launchd domain:

1. **`service_domain` in network.json** — explicit override, always wins
2. **Plist file discovery** — Evolve looks for the plist in standard locations and infers the domain from where it's found:
   - `/Library/LaunchDaemons/{service}.plist` → `system`
   - `/Library/LaunchAgents/{service}.plist` → `gui/{uid}`
   - `~/Library/LaunchAgents/{service}.plist` → `gui/{uid}`
3. **Brute-force fallback** — tries `gui/{uid}` then `system` if the plist isn't found

You only need to set `service_domain` for non-standard installs where plist discovery fails. Valid values:

```json
"service_domain": "system"       // LaunchDaemon (sudo launchctl kickstart -k system/…)
"service_domain": "gui/502"      // LaunchAgent in user session (UID 502)
```

Example with explicit domain:
```json
"bots": {
  "admin-bot": { "role": "primary", "port": 19000 },
  "team-bot-a":   { "role": "member",  "port": 19001, "service_domain": "gui/502" }
}
```

---

## `integrations`

Optional. Controls how the admin dashboard discovers integration credentials.

```json
"integrations": {
  "discovery": {
    "v2": true
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `discovery.v2` | bool | `true` | Phase 2 integration discovery probes. Kill switch — set to `false` to disable. See below. |

### `discovery.v2` — Phase 2 integration discovery

Enabled by default. Set to `false` explicitly to fall back to the Phase 1.5
discovery surface (wizard-managed credentials only) — useful as an escape
hatch if a future probe misbehaves on a particular instance.

When enabled, the dashboard probes:

- **Plugin-managed credentials** under `~/<bot>/.openclaw/workspace/credentials/`
  — OAuth client secrets, OAuth token caches, and service-account JSONs that
  custom plugins (e.g. team-bot-c's ranch plugin) bundle alongside the bot.
- **Openclaw channels-block tokens** in `~/<bot>/.openclaw/openclaw.json`
  under `channels.<provider>.<botToken|appToken|userToken>` (telegram,
  slack) — for bots configured outside the wizard. Rotation rewrites the
  JSON path directly (storage="openclaw_channels").
- **Dotenv-stored tokens** in `~/<bot>/.openclaw/workspace/.env` for Slack,
  Telegram, and Discord (a curated list of provider-specific env-var names;
  values never leave the helper). Rotation rewrites the matching
  `<NAME>=<value>` line, preserving every adjacent line including
  unrelated secrets like database passwords (storage="dotenv").
- **System-level GitHub auth** evidence from `~/.ssh/id_*` and
  `~/.config/gh/hosts.yml`, surfaced as evidence chips alongside the existing
  PAT-from-`.git/config` row.

Each row's `actions` array — driven by the winning probe's declared
affordances (Phase 3 routing) — controls which buttons render. Plugin-managed
rows get `view_config` only (writing wizard tokens elsewhere would silently
leave the plugin reading stale credentials). Openclaw-channels-block and
dotenv rows get `rotate` + `view_config` so the operator can update the
token in place without disturbing where the runtime reads from. The
frontend reads `row.actions[]` for the action buttons; each entry carries
an `id` matching the probe's declared `Affordance`, plus `label` / `style`
/ `modal` / `endpoint` records.

If you've upgraded an existing pod, run `sudo evolve-admin refresh-sudoers`
so the new grants for the workspace credential / dotenv read+write / ssh /
gh paths take effect on the live mini.

---

## Proposal schema

Every file in `proposals/*/` follows this schema:

```json
{
  "id": "prop-2026-04-04-abc12345",
  "generated": "2026-04-04T09:00:00Z",
  "status": "pending",
  "schema_version": 1,
  "pattern_key": "bot1:high_maintenance_ratio",
  "target_bot": "bot1",
  "type": "config_change",
  "problem": "Tier 2 ratio is 0.42 (target: 0.20).",
  "root_cause": "High maintenance ratio over 14 days.",
  "alternatives_considered": ["workflow_change"],
  "confidence": 0.82,
  "proposed_change": {
    "path": "agents.defaults.contextSize",
    "from": 8000,
    "to": 16000,
    "description": "Increase context window to reduce re-orientation turns."
  },
  "minimum_test": "Tier 2 ratio drops below 0.30 over next 7 days",
  "risk": "low",
  "forge_required": true,
  "source": "analyze.py",
  "security_review": { ... }
}
```

### Proposal types

| Type | Description | Forge required | apply.py action |
|------|-------------|----------------|-----------------|
| `config_change` | Patch to `openclaw.json` | Yes | Backup → patch → restart → health check → rollback on failure |
| `script_change` | Write/patch a workspace file | Yes | Backup → write → rollback on error |
| `workflow_change` | Instruction to bot (can't be mechanically applied) | No | Write note to `memory/evolve-workflow-instructions.md` |
| `investigation` | Alert and note only, no change | No | Acknowledge, no action |

### `pattern_key` format

`"{bot_id}:{detector_name}"` — e.g. `"bot1:high_maintenance_ratio"`.

Proposals are deduplicated by `pattern_key` across the `pending/`, `reviewed/`, and `approved/` stages. A new proposal for the same pattern won't be created while an existing one is live.

---

## Application manifest schema

Files in `shared_dir/applications/{bot_id}/{application_id}.json`:

```json
{
  "id": "health-tracking",
  "name": "Health Tracking",
  "bot_id": "bot1",
  "schema_version": 1,
  "status": "approved",
  "priority": "optional",
  "description": "Tracks health metrics...",
  "goals": ["Track daily protein intake", "..."],
  "success_metrics": [
    {
      "name": "protein_log_updated",
      "description": "Daily protein log is updated",
      "measurement": "file_exists check",
      "target": "Pass"
    }
  ],
  "tests": [
    {
      "name": "test_1",
      "description": "Protein log exists and is non-empty",
      "test_type": "file_exists",
      "trigger": "memory/health/protein-log.md",
      "expect": "not-empty"
    }
  ],
  "privacy_constraints": ["Health data never shared outside workspace"],
  "satisfaction_score": 4,
  "known_issues": [],
  "desired_improvements": [],
  "created_at": "2026-04-04T00:00:00Z",
  "approved_at": "2026-04-04T00:00:00Z"
}
```

---

## Validation result schema

Files in `proposals/validation-results/{proposal_id}.json`:

```json
{
  "proposal_id": "prop-2026-04-04-abc12345",
  "validated_at": "2026-04-04T10:00:00Z",
  "validator_version": "0.1.0",
  "result": "pass",
  "recommendation": "promote",
  "confidence": 0.95,
  "tests_run": ["boot_test", "health_check"],
  "evidence": "Gateway responded 200 OK after config patch",
  "validation_notes": ""
}
```

Pre-rename pods may still have files at `proposals/forge-results/{proposal_id}.json`
with a `forge_notes` field. `apply.py` reads from both locations during the
transition; run `scripts/migrate-validation-results.py` to rename the directory
and field in place.

---

## Apply result schema

Files in `proposals/apply-results/{proposal_id}.json`:

```json
{
  "proposal_id": "prop-2026-04-04-abc12345",
  "bot_id": "bot1",
  "success": true,
  "action_taken": "config_patched",
  "details": "Set agents.defaults.contextSize = 16000. Gateway healthy.",
  "rollback_triggered": false,
  "applied_at": "2026-04-04T11:00:00Z",
  "applier_version": "0.1.0"
}
```

---

## `modules`

Controls which Evolve components are active and their tuning parameters. All modules default to enabled except `continuity_engine` and `slack_signals`.

```json
"modules": {
  "observer":          { "enabled": true },
  "metrics":           { "enabled": true, "retentionDays": 90 },
  "healing":           { "enabled": true, "checkIntervalMin": 5, "failuresBeforeAlert": 3, "slowThresholdMs": 3000, "restartCooldownMin": 10 },
  "analysis":          { "enabled": true, "days": 7,
    "detectors": {
      "high_maintenance_ratio":  { "enabled": true,  "threshold": 0.30 },
      "api_key_fallback":        { "enabled": true },
      "declining_resolution":    { "enabled": true,  "threshold": 0.70 },
      "zero_activity":           { "enabled": true,  "daysMissing": 3 },
      "low_satisfaction":        { "enabled": true,  "minScore": 3 },
      "promise_breach":          { "enabled": true,  "threshold": 0.40 },
      "efficiency_problems":     { "enabled": true,  "threshold": 0.25 },
      "application_abandonment":  { "enabled": true },
      "promise_resolution":      { "enabled": true },
      "slack_quality_drop":      { "enabled": false },
      "detector_staleness":      { "enabled": true }
    }
  },
  "forge":             { "enabled": true },
  "apply":             { "enabled": true },
  "continuity_engine": { "enabled": false, "idleThresholdMin": 15, "maxAgentTasksPerRun": 3, "budgetFloor": 0.10 },
  "expansion":         { "enabled": true, "minSessionsForTheme": 3 },
  "slack_signals":     { "enabled": false },
  "outcomes":          { "enabled": true }
}
```

### Module reference

| Module | Default | Key settings |
|--------|---------|-------------|
| `observer` | enabled | — (plugin-side, no script tuning) |
| `metrics` | enabled | `retentionDays` |
| `healing` | enabled | `checkIntervalMin`, `failuresBeforeAlert`, `slowThresholdMs`, `restartCooldownMin` |
| `analysis` | enabled | `days`, per-detector `enabled`/`threshold` |
| `forge` | enabled | — |
| `apply` | enabled | — |
| `continuity_engine` | **disabled** | `idleThresholdMin`, `maxAgentTasksPerRun`, `budgetFloor` |
| `expansion` | enabled | `minSessionsForTheme` |
| `slack_signals` | **disabled** | — (configure channels via `slackChannelApplications`) |
| `outcomes` | enabled | — |

### Managing via CLI

```bash
evolve-admin modules list                         # show all modules
evolve-admin modules enable continuity_engine     # enable a module
evolve-admin modules disable slack_signals        # disable a module
evolve-admin modules tune healing failuresBeforeAlert 5
evolve-admin modules detector disable slack_quality_drop
evolve-admin modules detector enable promise_resolution
```

### Staged deployment

Use modules to roll out Evolve incrementally:

**Stage 1 (immediate):** observer, metrics, healing, apply
```json
"modules": {
  "analysis":          { "enabled": false },
  "forge":             { "enabled": false },
  "continuity_engine": { "enabled": false },
  "expansion":         { "enabled": false },
  "outcomes":          { "enabled": false }
}
```

**Stage 2 (after 2 weeks):** enable analysis with core detectors only
**Stage 3 (after 30 days):** enable all detectors, outcomes
**Stage 4 (optional):** enable continuity_engine, expansion, slack_signals
