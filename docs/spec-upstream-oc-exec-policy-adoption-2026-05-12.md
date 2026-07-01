# Upstream OpenClaw `exec-policy` adoption — migration plan

**Status:** Phase 1 (deploy model change) shipped 2026-05-17. Cutover step (CLI migration) is a supervised step Pod-Admin will run after confirming v2026.4.12+ on the mini fleet.

**Pillar:** V1.5-3 (Evolve v1.5 sprint). Origin: [feedback_dont_reimplement_upstream](../memory/feedback_dont_reimplement_upstream.md).

**Date:** 2026-05-12. Updated: 2026-05-17.

---

## Why this exists

Evolve's team-bot-c and security-bot bots are currently hardened via direct edits to their `openclaw.json` and `exec-approvals.json` files on the mini — outside git, outside `deploy.py`'s force-set behavior, survives future deploys. That history is documented in [project_team-bot-c_safeguards](../memory/project_team-bot-c_safeguards.md).

OpenClaw upstream `v2026.4.12` (April 2026) shipped a real CLI for the same job: `openclaw exec-policy` with `show`, `preset`, and `set` subcommands that synchronize requested `tools.exec.*` config with the local exec approvals file. Per PR [#64050](https://github.com/openclaw/openclaw/pull/64050), the CLI includes:

- node-host rejection (refuse to launch a node-managed exec when the host process is itself node — closes the recursion vector)
- rollback safety (atomic write, restore-on-failure)
- sync conflict detection (warn when openclaw.json and exec-approvals.json have drifted apart)

Per [feedback_dont_reimplement_upstream](../memory/feedback_dont_reimplement_upstream.md): when upstream OC ships a capability, adopt — don't maintain duplicate. The team-bot-c/security-bot direct-edit pattern is exactly such a duplicate.

## What we are **not** doing

The original V1.5-3 brief referenced a "signed manifest + eBPF runtime enforcement" upstream feature. That feature does not exist in any actual OpenClaw release through `v2026.5.12-beta.3`. Verified against:

- `gh release view v2026.4.12 --repo openclaw/openclaw` — no mention of manifest signing, `clawmanifest.json`, eBPF, SHA256, kernel-level enforcement
- `https://github.com/openclaw/openclaw/releases/tag/v2026.4.27` — same
- `https://docs.openclaw.ai/plugins/manifest` — documents `openclaw.plugin.json` as metadata, no signing system
- `https://patchbot.io/ai/openclaw` — patch notes 2026.5.3 through 2026.5.7, no signing

Blog posts (clawbot.blog, jitendrazaa.com) that describe a signed-manifest + eBPF system appear to be AI-generated content not corroborated by primary sources. The escalation note at `.claude/specs/escalations/v1-5-3-20260512-1430.md` documents the discovery. The memory entry [reference_openclaw_releases_page](../memory/reference_openclaw_releases_page.md) should be corrected.

## What we **are** doing

Three pieces shipped in the original V1.5-3 PR:

1. **Per-bot OpenClaw version detection** (`evolve_admin.upstream_version`). Pulls `meta.lastTouchedVersion` from each bot's `openclaw.json` with `openclaw --version` as fallback. Returns `BotVersionReading` per bot and `FleetStatus` rollup with `bots_at_or_above` / `bots_below` / `bots_unknown`.

2. **Exec-policy compliance indicator** on each bot's Safety card. Determined structurally: a bot is **scoped** if `tools.exec.security` is `allowlist`/`deny`/`off`, or `full` with a non-empty allowlist anywhere (inline or in `exec-approvals.json`). Otherwise **permissive**.

3. **`security_warden.posture.check_openclaw_version_floor`** — emits a `warn`-severity Signal when a bot's `meta.lastTouchedVersion` parses below the configured floor (default `2026.4.12`). The Signal automatically sweep-resolves once the bot's `lastTouchedVersion` advances.

**Phase 1 shipped 2026-05-17** — deploy model change (see below). The cutover below is the operator action that completes the migration.

## Phase 1: deploy model change (shipped 2026-05-17)

`ensure_plugin_config` in `deploy.py` previously forced `tools.exec.security="full"` on every bot to suppress OpenClaw's "ask the user" mode in chat channels (Telegram, Slack, Discord have no in-channel approval UI). This was the correct workaround but the wrong architecture — it caused the OpenClaw advisory "Extension plugin tools may be reachable under permissive tool policy" on every bot.

The fix: infer the exec policy from the bot's actual exec needs:

| Condition | Inferred policy |
|---|---|
| Explicit `execPolicy` in `network.json` bot config | That value |
| `exec-approvals.json` has allowlist entries | `allowlist` |
| No exec-approvals / empty | `deny` |

**`deny`** is correct for plugin-only bots (brave, github, slack, etc. make HTTP calls via MCP — no exec needed). Hard-blocking exec removes the advisory and the permissive surface entirely.

**`allowlist`** is correct for bots with real exec needs (e.g. security-bot's monitoring scripts). They already have entries in `exec-approvals.json`; the inferred policy matches.

**`execPolicy` override** in `network.json` is the escape hatch for edge cases (e.g. `"full"` for a bot that needs dynamic exec approval in an interactive Claude Code session).

The `ask` key is removed for `deny`-mode bots (irrelevant when exec is blocked) and set to `"on-miss"` for `allowlist`/`full`-mode bots.

The `exec security=full` severity-demotion rules in `_audit_run_one` (server.py) and `_normalize_findings` (oc_audit.py) are removed — `full` is no longer the baseline, so OC audit warnings about it should surface to the operator.

**On deploy to the mini:** security-bot gets `allowlist` (it has ~30 exec-approvals entries). All other bots (evo, team-bot-a, admin-bot, team-bot-b, team-bot-c, personal-bot) get `deny`. The deploy corrects any bot currently on `full` with no allowlist.

## Cutover plan (supervised, not executed by this PR)

### Pre-flight

Run `GET /api/security/upstream-version` and confirm `floor_met: true` and `bots_below: []`. As of 2026-05-12 the mini fleet is on `OpenClaw 2026.4.29 (a448042)` (verified by `ssh pod-admin-user@mini ... openclaw --version` for team-bot-a, admin-bot, team-bot-c, personal-bot, security-bot, evolve), so the floor should already be met. `team-bot-b` does not exist as a Unix user on the mini — confirmed `sudo: unknown user team-bot-b`.

### Step 1 — Inventory the existing manual edits

Read [project_team-bot-c_safeguards](../memory/project_team-bot-c_safeguards.md) for the canonical list. Today's state on the mini:

**team-bot-c** (`/Users/team-bot-c/.openclaw/openclaw.json`):
- `channels.slack.groupPolicy: "allowlist"` (was `"open"`)
- `tools.fs.workspaceOnly: true`
- `agents.defaults.sandbox.mode: "all"`
- `tools.exec.security: "deny"` (post-Phase-1 deploy; team-bot-c has no exec-approvals entries)

**security-bot** (`/Users/security-bot/.openclaw/openclaw.json` + `exec-approvals.json`):
- `tools.exec.strictInlineEval: true` (in openclaw.json `tools.exec`)
- `agents.main.autoAllowSkills: false` (in exec-approvals.json)
- ~30+ entries in `agents.main.allowlist[]` covering /usr/bin/curl, grep, shasum, /bin/ls, /bin/cat, /usr/bin/stat, /usr/bin/find, /usr/bin/git, /usr/bin/ps, etc. (read live on 2026-05-12, partial; pattern-style entries with `lastUsedAt`/`lastUsedCommand` fields)

### Step 2 — Generate the upstream CLI invocations

Per the actual upstream CLI shape (`openclaw exec-policy show / preset / set`):

For **team-bot-c** (keeps `security: "full"`, adds an explicit allowlist preset):
```bash
# Inspect current state
sudo -u team-bot-c openclaw exec-policy show
# Apply a hardened preset (subject to verifying preset names in --help on the mini)
sudo -u team-bot-c openclaw exec-policy preset hardened
# Or set granularly:
sudo -u team-bot-c openclaw exec-policy set tools.exec.security=full
sudo -u team-bot-c openclaw exec-policy set tools.exec.ask=on-miss
```

For **security-bot** (keeps the ~30 entry allowlist; needs no migration beyond confirming exec-policy sees it):
```bash
sudo -u security-bot openclaw exec-policy show       # should list the current 30+ entries
```

The `strictInlineEval`, `workspaceOnly`, `sandbox.mode`, and `slack.groupPolicy` settings live outside `tools.exec.*` and are **not** in scope for `openclaw exec-policy`. They remain direct-edit until OpenClaw upstream surfaces them via a comparable CLI (none announced as of `2026.5.12-beta.3`).

### Step 3 — Validate

After cutover:
```bash
sudo -u team-bot-c openclaw exec-policy show > /tmp/team-bot-c-policy-after.txt
diff /tmp/team-bot-c-policy-before.txt /tmp/team-bot-c-policy-after.txt
# Expect: no change in the effective policy; the file was the source of truth all along.
```

Confirm the bot still runs (`launchctl print system/ai.openclaw.team-bot-c-gateway`). Confirm 24-48h of normal traffic via the audit log.

### Step 4 — Update deploy.py gap-fill

`packages/admin/evolve_admin/deploy.py` currently force-sets `tools.exec.security` and `tools.exec.ask` on every deploy. Once cutover is verified, switch that path to delegate to `openclaw exec-policy set` via subprocess (run as the bot user, like the rest of deploy.py). The direct-write path stays as fallback if `openclaw exec-policy` is unavailable.

### Step 5 — Update memory

Update [project_team-bot-c_safeguards](../memory/project_team-bot-c_safeguards.md):
- Mark the `tools.exec.*` edits as superseded by `openclaw exec-policy` (with a pointer to this doc)
- Keep the `workspaceOnly`, `sandbox.mode`, `slack.groupPolicy`, `strictInlineEval` notes as-is (not in scope for the CLI)

Update [reference_openclaw_releases_page](../memory/reference_openclaw_releases_page.md):
- Remove the "v2026412 — manifest-driven plugin security with eBPF runtime enforcement" bullet (unsupported by primary sources)
- Replace with the actual v2026.4.12 contents: "openclaw exec-policy CLI (#64050)" and "narrow plugin loading to manifest-declared needs (#65120, #65259, #65298, #65429, #65459)"

## Open questions

- Does `openclaw exec-policy preset` ship with a `hardened` preset, and if so what does it set? Need to run `--help` on the mini before relying on a specific preset name.
- Does `openclaw exec-policy set` accept dotted paths like `tools.exec.ask=on-miss` or does it require a different syntax? Read the v2026.4.12 changelog entry for #64050 again before scripting.
- When `deploy.py` re-deploys a bot, should it invoke `openclaw exec-policy set ...` for each desired key, or write the file directly and let the bot pick it up on next launch? The CLI is safer (atomic, conflict-detected), but adds a subprocess + bot-user sudo per setting per deploy.

## Why the proposal type is `warn`, not `alert`

The version-floor finding (`openclaw_version_below_floor`) ships at `warn` severity, not `alert`. Two reasons:

1. **Reversibility**: a bot running an old OC version is functional — it just doesn't have the new CLI. Operator can upgrade on their schedule; no security incident is in flight.
2. **Plex test**: "OpenClaw version is behind — upgrade recommended" reads as advisory, not panic. An `alert` chip on the Safety card for a routine version drift would erode trust in alerts that need urgent attention.

The companion finding `multi_user_exec_full_unscoped` stays at `alert` because it's the actual security gap; the version finding is the substrate that lets us *fix* the gap upstream-style.
