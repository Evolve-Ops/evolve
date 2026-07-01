# MCP Server Administration — Architecture (2026-05-10)

Status: **draft** (design lock + Phase A implementation start pending operator review).

**What this is.** The architecture for administering MCP (Model Context Protocol) servers across the pod. MCP servers are external processes/endpoints each OpenClaw bot can connect to in order to gain new tool capabilities (GitHub, Slack, Linear, Postgres, browser automation, etc.). They are configured per-bot in `openclaw.json → mcp.servers`. Today Evolve has zero visibility into this surface; this spec defines the inventory, catalog, install path, credential binding, health probing, usage tracking, and security posture for MCP across the pod's lifetime.

**Naming note.** "MCP" in this spec always refers to MCP servers the *bots* connect to. The pre-existing **Evolve MCP Bridge** (`packages/admin/evolve_admin/mcp_bridge/`, `/api/mcp/*` routes) is the opposite direction — Evolve serving its pod context as an MCP server to a Claude Desktop client. The bridge is out of scope for this spec except as something the inventory view may optionally surface for completeness.

**Relationship to other specs.**
- [roadmap-openclaw-admin-coverage-2026-05-10.md](roadmap-openclaw-admin-coverage-2026-05-10.md) — this spec implements Tier 1, item 1.1, and parts of 1.4 (plugin inventory, where plugins ship MCP servers).
- [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) — the UI placement for MCP (per-bot Integrations sub-tab + cross-bot Security posture) lives there; §7 of this spec defers the details.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — the MCP monitor writes Signals; the curator generator writes Proposals linked via `motivating_signals[]`.
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — the install/remove/update flow rides the existing Proposal → review → apply → verify pipeline.
- [spec-rsi-layer-1-foundation-2026-04-18.md](archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md) §5.4 — applier dispatch pattern that the three new MCP action kinds plug into.
- Schema reference: [docs/schemas/oc-config-schema.txt:40235](schemas/oc-config-schema.txt) — `mcp.servers` field shape.

---

## 1. The problem

**Capability expansion is a blast-radius expansion.** A bot with no MCP servers can only do what OpenClaw built-ins (Bash, Edit, Read) and first-party plugins (Brave, Slack, Telegram, the LLM providers) allow. Add a Postgres MCP server with write credentials and that bot can now alter a database. Add a GitHub MCP server and it can open issues, write to repos, leak source code in tool output. The set of MCP servers configured on a bot is a more accurate description of "what this bot can actually do" than its name or persona — and Evolve currently has no view of it.

**Credentials live in the same file as config.** Each entry in `mcp.servers` may have an `env` map for the spawned process, where API keys and tokens go today. This is the same anti-pattern Evolve's keystore was created to avoid for the first-party plugin keys, but extended to a surface Evolve doesn't yet manage.

**Trust transitivity.** When the model invokes an MCP-server tool, anything that tool returns becomes part of the model's context. A compromised or malicious server can inject prompts that change subsequent model behavior. CVE-2025-6514 (`mcp-remote` RCE), the Mitiga MCP-OAuth MitM, and the TrustFall ("Comment and Control") attack are the canonical recent examples. The pod has no defense if it doesn't even know which servers are installed.

**No lifecycle.** Today, installing an MCP server requires SSH to the mini, editing `openclaw.json` by hand, restarting the gateway. Removing or rotating credentials is the same. There is no catalog, no approval gate, no usage tracking, no retirement workflow, no CVE awareness, no test before production. Every install is bespoke.

**Live-pod baseline (2026-05-10).** 6 of 7 bots have `mcp.servers = {}`. team-bot-b has no `openclaw.json`. `commands.mcp` and `commands.plugins` are unset (default false) on every bot, so bots can't currently add MCP servers themselves via the `/mcp` slash command. The pod is in a clean baseline state — which is the right time to design the policy plane.

---

## 2. Core reframe: Evolve as the MCP policy + lifecycle plane

Generic MCP gateways (Microsoft mcp-gateway, IBM mcp-context-forge, Portkey, Kong) sit in the network and are blind to who the bots are, who their users are, what credentials already exist, and what the pod has decided is acceptable. Evolve already knows all of that — through `network.json`, the keystore, the signal store, the proposal pipeline, and POD_CONDUCT. Rather than introducing a gateway, this spec extends Evolve's existing surfaces so it becomes the **policy + lifecycle plane** for MCP. Three new concepts:

| Concept | Role | Output | Lives in |
|---|---|---|---|
| **MCP Catalog** | Pod-curated set of vetted server entries, each with capabilities, required credentials, vetting status, CVE cross-references | `CatalogEntry` | `{shared_dir}/mcp/catalog/` |
| **MCP Allowlist** | Per-bot binding of catalog entries to credentials, approved via the existing proposal pipeline | `BotAllowlist` | `{shared_dir}/mcp/allowlist/<bot_id>.json` |
| **MCP Inventory** | Observed `mcp.servers` state per bot, refreshed every audit cycle, compared against the allowlist | `Inventory` | `{shared_dir}/mcp/inventory/<bot_id>.json` |

The monitor watches inventory vs. allowlist and fires Signals. The curator generator proposes installs/removals/updates that ride the proposal pipeline. Three new appliers (`InstallMcpServer`, `RemoveMcpServer`, `UpdateMcpServerConfig`) write the changes. Health and usage are separate concerns layered on top.

---

## 3. Data model

### 3.1 CatalogEntry

A pod-approved MCP server. Operator-curated. Static-ish — versions bump occasionally, vetting status changes when CVEs land.

```json
{
  "id": "github",
  "name": "GitHub MCP",
  "description": "Search repositories, read files, create issues, manage PRs.",
  "transport": "stdio",
  "package": {
    "kind": "npm",
    "name": "@modelcontextprotocol/server-github",
    "version_constraint": "^1.0",
    "current_version": "1.2.3",
    "source_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/github"
  },
  "launch": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "cwd": null
  },
  "required_env": [
    {
      "name": "GITHUB_TOKEN",
      "purpose": "Personal access token with repo:read scope",
      "scope_recommendation": "repo:read (avoid repo:write unless bot needs to push)",
      "keystore_hint": "github-*"
    }
  ],
  "advertised_tools": [
    "search_repositories",
    "get_file_contents",
    "create_issue",
    "create_pull_request",
    "list_commits"
  ],
  "vetting": {
    "status": "approved",
    "vetted_by": "operator@2026-05-10",
    "vetted_notes": "Source-reviewed; first-party from modelcontextprotocol org; npm package signed.",
    "cve_check": {
      "last_run_at": "2026-05-10T12:00:00Z",
      "advisories": []
    }
  },
  "default_scope": {
    "pod_wide": false,
    "suggested_bots": ["evolve", "admin-bot"]
  },
  "fingerprint": "<sha256 of canonical entry>"
}
```

### 3.2 BotAllowlist

Per-bot record of approved server bindings. Mutated only via approved Proposals. Audit history lives in the proposal log.

```json
{
  "bot_id": "team-bot-a",
  "version": 3,
  "servers": {
    "github": {
      "catalog_id": "github",
      "catalog_fingerprint": "<sha256>",
      "version_pin": "^1.0",
      "env_bindings": {
        "GITHUB_TOKEN": "keystore:github-team-bot-a"
      },
      "approved_at": "2026-05-12T14:30:00Z",
      "approved_by": "operator",
      "approved_via_proposal": "P-2026-05-12-007",
      "config_signature": "<sha256 of resolved launch config, excluding env values>"
    }
  }
}
```

### 3.3 Inventory

Observed state, refreshed every audit cycle. Cached on disk so the monitor can compute deltas cheaply.

```json
{
  "bot_id": "team-bot-a",
  "observed_at": "2026-05-10T14:15:00Z",
  "openclaw_config_path": "/Users/team-bot-a/.openclaw/openclaw.json",
  "openclaw_config_present": true,
  "servers": [
    {
      "name": "github",
      "kind": "stdio",
      "command": "/Users/Shared/evolve-plugin/mcp-launcher/github",
      "args": [],
      "url": null,
      "env_keys": ["GITHUB_TOKEN"],
      "config_signature": "<sha256>"
    }
  ],
  "self_mutation": {
    "commands_mcp": false,
    "commands_plugins": false
  }
}
```

The `config_signature` excludes env values — only key *names* contribute. That keeps secrets out of the inventory file and the signal index.

### 3.4 Signals emitted by the MCP monitor

| Signal | Severity | Fired when |
|---|---|---|
| `mcp_unknown_server` | high | Server name in inventory not in the bot's allowlist |
| `mcp_server_changed` | medium | Allowlist server present with a config signature differing from the approved fingerprint |
| `mcp_self_mutation_enabled` | medium | `commands.mcp = true` or `commands.plugins = true` on any bot |
| `mcp_openclaw_config_missing` | low | Bot in `network.json` but `openclaw.json` not found (today's team-bot-b case) |
| `mcp_server_unhealthy` | medium | Probe failures cross threshold (Phase C) |
| `mcp_server_credential_invalid` | medium | Auth-error pattern in probe output (Phase C) |
| `mcp_server_unused` | low | Zero usage for ≥30 days (Phase C, requires usage tracking) |
| `mcp_server_cve_match` | high | Cached advisory intersects an installed catalog entry's version (Phase D) |
| `mcp_scope_drift` | high | Observed tool calls reference tools not in the catalog entry's advertised set (Phase D, requires usage tracking) |

### 3.5 Proposal action kinds (new)

| Kind | Effect | Applier |
|---|---|---|
| `InstallMcpServer` | Add a server to `openclaw.json → mcp.servers`, update allowlist, trigger restart | `packages/analyzer/arbiter/appliers/install_mcp_server.py` |
| `RemoveMcpServer` | Remove from both `openclaw.json` and allowlist, trigger restart | `.../remove_mcp_server.py` |
| `UpdateMcpServerConfig` | Mutate config or env bindings, trigger restart | `.../update_mcp_server_config.py` |

All three use the existing `/tmp` staging + `sudo /bin/cp` pattern documented in [CLAUDE.md](../CLAUDE.md). All three include a snapshot of the pre-state in the proposal's `RevertPlan`, so the verify daemon can roll back if the claim fails.

---

## 4. On-disk layout

All MCP state lives under `{shared_dir}/mcp/`. Owned by `evolve` user; atomic writes via temp-file + rename, per the [signal-store pattern](spec-alerts-signal-store-2026-05-07.md).

```
{shared_dir}/mcp/
├── catalog/
│   ├── pod-catalog.json              # operator-curated, the source of truth
│   ├── upstream-anthropic.json       # cached upstream catalog (read-only mirror)
│   └── pending/<entry_id>.json       # candidate entries from the curator generator
├── allowlist/
│   └── <bot_id>.json                 # per-bot approved server bindings
├── inventory/
│   └── <bot_id>.json                 # latest observed config (replaces previous each cycle)
├── health/
│   └── <bot_id>/<server>.jsonl       # probe history, ~1 line per probe, 90-day retention
├── usage/
│   └── <bot_id>/<YYYY-MM-DD>.json    # daily aggregates (Phase C)
├── advisories/
│   └── <advisory_id>.json            # cached CVE/security advisories (Phase D)
└── launchers/                        # generated wrapper scripts (see §6)
    └── <bot_id>/<server>             # exec'd by openclaw instead of the raw command
```

Retention: `inventory/` is current-state only; `health/` and `usage/` follow the signal-store's 90-day/1-year split.

---

## 5. Lifecycle activities

The ten activities below are organized roughly in the order an operator encounters them. Each is described in design terms; phasing (which ones land first) is in §9.

### 5.1 Discovery / catalog

The pod-catalog is operator-curated. Entries arrive three ways:

1. **Manual.** Operator adds an entry directly via admin UI ("Add server to catalog"). Required fields: id, transport, launch (command/args or url), required_env declarations. Operator marks vetting status.
2. **Upstream ingest.** A daily cron pulls Anthropic's published MCP server registry (when one stabilizes) into `upstream-anthropic.json`. Operator promotes entries from upstream into the pod catalog one at a time, after review.
3. **Curator generator.** The new `mcp_curator` generator (under `packages/analyzer/generators/mcp_curator/`) watches for inventory entries that aren't in the catalog (the `mcp_unknown_server` signal) and proposes either adding them to the catalog or removing them from the bot. This is the "we found something on a bot — is it supposed to be there?" workflow.

**Vetting depth in v1** is operator-attestation, not automated. Future v2 work could add static analysis (read the server's `tools/list` advertisement, scan source for suspicious patterns, run in a sandboxed bot).

### 5.2 Install

Operator picks an entry from the catalog, picks a bot, picks credentials. The UI builds an `InstallMcpServer` proposal containing the catalog entry id, bot id, env bindings (keystore references). Proposal flows through:

1. **Security review** — `security_warden` evaluates: is the catalog entry vetted? Do the env bindings match the catalog's `required_env` declarations? Is this bot allowed to install this server (per the catalog's `default_scope` and any per-bot policy)?
2. **Approval gate** — human approval for v1 (no auto-approve, even for `default_scope.pod_wide=true` entries; we revisit after the first dozen installs).
3. **Apply** — the `InstallMcpServer` applier:
   - Reads the catalog entry.
   - Writes a wrapper script to `{shared_dir}/mcp/launchers/<bot_id>/<server_id>` (see §6.2 on credential binding).
   - Reads the bot's current `openclaw.json`, merges in the new `mcp.servers` entry pointing at the wrapper.
   - Stages via `/tmp` + `sudo /bin/cp` per [CLAUDE.md](../CLAUDE.md).
   - Updates `{shared_dir}/mcp/allowlist/<bot_id>.json`.
   - Triggers a gateway restart via the existing heal/launchctl pathway.
4. **Verify** — verify daemon checks at next cycle that the server appears in inventory with the expected signature and that the probe (Phase C) is green.

### 5.3 Configure / credentials

The credential-binding story is the design's hinge. Two options were considered:

- **Option A: write resolved credentials directly into `mcp.servers[name].env`.** Pro: zero changes elsewhere; OpenClaw reads them as-is. Con: cleartext credentials in `openclaw.json` (the very anti-pattern we're trying to avoid); rotation means rewriting every bot's config.
- **Option B (chosen): wrapper-script credential injection.** The `mcp.servers[name].command` points at a generated wrapper under `{shared_dir}/mcp/launchers/<bot_id>/<server_id>`. The wrapper reads the keystore (it has the right ACL since `{shared_dir}` is evolve-owned and the keystore decrypts inside the evolve process), sets env vars, then exec's the real server command.

Wrapper sketch:
```bash
#!/bin/bash
# Auto-generated by Evolve mcp installer; do not edit.
# bot=team-bot-a server=github catalog_fingerprint=<sha256>
set -euo pipefail
export GITHUB_TOKEN="$(/usr/local/bin/evolve-admin keystore get github-team-bot-a)"
exec npx -y @modelcontextprotocol/server-github "$@"
```

This puts credential resolution inside Evolve's existing keystore + sudoers grants, keeps `openclaw.json` credential-free, and makes rotation a one-place edit. The wrapper is executable by the bot user (mode 0755, owned by the bot user, written through `/tmp` staging + `sudo /bin/cp` per [CLAUDE.md](../CLAUDE.md)).

**Trade-offs to accept.** Wrapper scripts add a small process layer between OpenClaw and the real server. Probe / debug instructions need to know to peek inside the wrapper. We accept this for the credential-hygiene win.

**Open question.** Whether OpenClaw natively supports `${VAR}` interpolation in `mcp.servers[name].env` from the gateway's process env. If yes, a future v2 could let Evolve inject the env via the launchd plist's `EnvironmentVariables` block instead of a wrapper script. Worth confirming before lock; doesn't block v1.

### 5.4 Per-bot scoping

Two scoping primitives:

- **Catalog entry `default_scope`** — `pod_wide` (any bot can be allowlisted to install this) or a `suggested_bots` whitelist. Advisory only; the actual gate is the allowlist.
- **Per-bot allowlist** — the operative gate. An install proposal must produce an allowlist entry for the (bot, server) pair, and the monitor's `mcp_unknown_server` signal fires on any inventory entry without a matching allowlist row.

Per-bot policy (e.g. "personal-bot shall never have any non-search MCP server") lives as operator policy in the admin UI; not modeled in v1, sketched as a future field on `BotAllowlist`.

### 5.5 Health monitoring (Phase C)

Probe each `(bot, server)` cell every 5–15 min depending on criticality.

- **stdio servers** — spawn the wrapper script with stdin closed after the MCP `initialize` handshake message; expect a `serverInfo` response within a timeout; kill. Records: probe_ts, latency_ms, ok, error_class.
- **HTTP servers** — POST `initialize` to the configured URL; same success criteria.

Results append to `{shared_dir}/mcp/health/<bot>/<server>.jsonl`. The monitor fires `mcp_server_unhealthy` when failure rate over a rolling window crosses threshold, and `mcp_server_credential_invalid` when error class matches an auth-failure pattern.

The existing integration-probe infrastructure ([packages/analyzer/generators/security_warden/scanners/](../packages/analyzer/generators/security_warden/scanners/)) is the closest pattern; the MCP probe runner is a peer module under `packages/analyzer/mcp_admin/health.py`.

### 5.6 Usage tracking (Phase C)

**Status note: source TBD.** Investigation during this spec showed that `{bot}/.openclaw/workspace/memory/turns-<date>.jsonl` contains per-turn token/cost aggregates but no `tool_use` records. `message_intake.jsonl` is inbound-message-only. Tool-call records are not currently emitted to a known location at the per-bot level. Two paths:

- **Hook-based capture (preferred).** A pod-wide PostToolUse hook injected by `deploy.py` that appends `(bot_id, session_id, ts, tool_name, ok, latency_ms)` to `{shared_dir}/mcp/usage/<bot>/<YYYY-MM-DD>.jsonl`. Tool names follow OpenClaw's `mcp__<server>__<tool>` convention so the source server is recoverable from the tool name. This is the same injection mechanism POD_CONDUCT uses today.
- **Session-transcript parsing.** If OpenClaw transcripts persist per-session in a parseable form (location not yet confirmed; needs investigation), Evolve could batch-parse them daily. Less timely; doesn't require hook deployment.

Either way, downstream consumers operate on a JSONL file of tool-call events keyed by `(bot, server, tool, day)`.

**Open question.** Whether the hook approach interacts well with the per-bot opt-in flag (`hooks.allowConversationAccess`, memory `project_oc_per_bot_hook_optin.md`). The plugin config in §1 shows it's `true` on every deployed bot, so this is probably fine, but the deploy path should set it explicitly rather than rely on existing state.

### 5.7 Token / cost attribution (Phase D)

MCP tool descriptions are sent in the model's system prompt for every session that has the server enabled; their token cost amortizes across the session. Tool results are part of conversation tokens. Attributing this to a server requires:

- A per-session inventory of which servers were enabled (derivable from `openclaw.json` at session start).
- A per-tool-description token count for each catalog entry (derivable once per catalog version).
- The existing per-turn token cost from `turns-<date>.jsonl`.

The math is straightforward division-of-shares; the value is being able to answer "is the Linear MCP paying for itself or just bloating the system prompt?" This connects directly to the deferred Budget Hawk v2 forensics in [docs/pending-ideas.md](pending-ideas.md).

### 5.8 Error tracking (Phase C)

Errors arise from probe failures (§5.5) and from observed tool-call errors (§5.6). Both flow into the existing error-reporter pipeline, dedup'd by `(bot, server, error_class)`, surfaced on the admin UI's Alerts page via the standard `mcp_server_unhealthy` Signal.

### 5.9 Security posture (Phase D)

Three streams:

- **Scope drift.** Compare observed tool-call patterns (§5.6) against the catalog entry's `advertised_tools`. A call to a tool not in the advertised set fires `mcp_scope_drift`. This catches both "server was updated with new tools the pod hasn't reviewed" and "server is misbehaving and exposing things its catalog entry hides."
- **CVE / advisory feed.** Daily cron pulls NIST NVD, GitHub Security Advisories, npm advisory DB filtered to packages matching catalog entries' `package.name`. Cache under `{shared_dir}/mcp/advisories/`. Cross-reference daily; fire `mcp_server_cve_match` on intersect with version constraints.
- **Auto-reject extensions.** Extend the eight existing proposal auto-reject rules (CLAUDE.md, §security_warden) with MCP-specific rules: no `InstallMcpServer` for a non-vetted catalog entry, no `UpdateMcpServerConfig` that adds a credential not declared in the catalog's `required_env`, no install of a server marked `vetting.status = "withdrawn"`.

### 5.10 Retirement (Phase D)

When usage for a `(bot, server)` pair drops to zero for ≥30 days, the `mcp_curator` generator proposes `RemoveMcpServer` with the rationale "unused for N days; remove to shrink attack surface." Operator approves or dismisses; dismissal triggers an annotation on the allowlist entry to suppress re-proposal for 90 days.

CVE-driven retirement: a fired `mcp_server_cve_match` signal automatically generates a `RemoveMcpServer` proposal with severity matched to the advisory's CVSS score.

---

## 6. Slot-in points (codebase locations)

| Concern | New file / location |
|---|---|
| Catalog reader/writer | `packages/analyzer/mcp_admin/catalog.py` |
| Allowlist reader/writer | `packages/analyzer/mcp_admin/allowlist.py` |
| Inventory reader (parses `openclaw.json`) | `packages/analyzer/mcp_admin/inventory.py` |
| Monitor (writes Signals) | `packages/analyzer/mcp_admin/monitor.py` |
| Health probe runner | `packages/analyzer/mcp_admin/health.py` |
| Usage aggregator | `packages/analyzer/mcp_admin/usage.py` |
| CVE advisory ingest | `packages/analyzer/mcp_admin/advisories.py` |
| Curator generator | `packages/analyzer/generators/mcp_curator/` (charter.yaml + observe.py + evaluate.py) |
| Install applier | `packages/analyzer/arbiter/appliers/install_mcp_server.py` |
| Remove applier | `packages/analyzer/arbiter/appliers/remove_mcp_server.py` |
| Update applier | `packages/analyzer/arbiter/appliers/update_mcp_server_config.py` |
| Wrapper script generator | `packages/analyzer/mcp_admin/launcher.py` |
| Admin UI routes | New `/api/mcp-admin/*` namespace (distinct from the existing `/api/mcp/*` bridge routes) in `packages/admin/evolve_admin/web/server.py` |
| Admin UI surfaces | Integrations → MCP Servers sub-tab (per-bot config); Security → MCP Posture sub-section (cross-bot matrix); see [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) §3 + §4 |
| Deploy-time integration | `packages/admin/evolve_admin/deploy.py` — ensure `{shared_dir}/mcp/` tree exists, inject the PostToolUse usage hook if Phase C is enabled |

The `/api/mcp/*` namespace stays with the bridge. Bot-MCP-administration routes use `/api/mcp-admin/*` to avoid confusion.

---

## 7. Admin UI surface

UI is split across two existing tabs per the restructure spec, [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md): **Integrations** owns per-bot configuration; **Security** owns cross-bot posture. The two are cross-linked. The Self-Improvement tab is the home for the install/remove/update proposal queue via the existing pipeline.

### 7.1 Integrations → MCP Servers sub-tab (per-bot configuration)

Lives in the new sub-tab structure under Integrations. Bot-tab axis selects which bot; the sub-tab content shows that bot's MCP picture.

- **Phase A (with this spec's Phase A).** Read-only inventory list pulled from `{shared_dir}/mcp/inventory/<bot>.json`. Each row: server name, transport (stdio/http), target, env key names, allowlist status (approved / unknown / changed). Empty state: "No MCP servers configured. Install from catalog to add one."
- **Phase B (with this spec's Phase B).** Adds the "Install from Catalog" action button (top-right, consistent with other sub-tabs) opening a modal catalog browser. Adds per-row actions (remove / update config / test connection) gated by the proposal pipeline. Adds a "Pending proposals" inline panel showing in-flight installs/removals for this bot.
- **Phase C+.** Adds health badges per row (last probe status, response time), recent usage summary per row (calls last 7d), credential-rotation affordance.

### 7.2 Security → MCP Posture sub-section (cross-bot posture)

Lives under the existing Security tab as a new posture sub-section.

- **Phase A.** Bot×server matrix: rows are bots from `network.json`, columns are servers observed across the pod. Cells: green (approved + signature match), red (unknown server), yellow (changed signature). Self-mutation flag (`commands.mcp = true`) badged on bot row. Click a cell to jump to that bot's Integrations → MCP Servers sub-tab with the relevant row highlighted.
- **Phase C.** Adds health status to each cell (unhealthy = yellow). Adds an unused-servers list ("30+ days no activity").
- **Phase D.** Adds CVE-match panel cross-referencing installed catalog entries against the advisory cache. Adds scope-drift panel listing tools called outside their catalog entry's advertised set.

### 7.3 Catalog management (where it lives)

The catalog is pod-wide, not per-bot, so it doesn't fit under the per-bot Integrations axis. Three placements considered:
- **Chosen: a "Catalog" link inside the install modal** (no dedicated page). Operators reach the catalog when they need to install; promoting candidate entries happens via the curator generator's proposals (Self-Improvement). Manual catalog edits use a hidden admin page reachable from the install modal's "Manage catalog" link.
- Alternative (rejected for v1): a top-level "MCP Catalog" page. Adds a nav item for a surface most operators visit ~quarterly.
- Alternative (rejected): under Modules. Catalog is operationally cold and doesn't fit Modules' "running code" framing.

Revisit if catalog management becomes a more frequent activity.

### 7.4 Activity / signals

Per-server activity (recent installs, signal history, probe failures) renders in two places:
- The Alerts page is the home for active signals — `mcp_unknown_server`, `mcp_server_unhealthy`, etc. surface there via the standard alerts route.
- The bot's Integrations → MCP Servers sub-tab shows a compact "Recent activity" footer per row for the last ~10 events on that (bot, server) pair.

No dedicated Activity page; the two existing surfaces are enough.

---

## 8. Cross-cutting decisions

### 8.1 Credential redaction

Inventory records and signal payloads never include env *values* — only key *names*. Wrapper scripts use `keystore get` which has the existing keystore audit log. The admin UI displays "configured" / "not configured" for each env var, never the value, and offers a "rotate via keystore" action that links to the keystore page.

### 8.2 Signature stability

`config_signature` is computed over a canonical JSON serialization of `(command, args sorted, url, env keys sorted, cwd)` — values excluded. This means:
- Adding a new env key to the bot's config → signature changes → `mcp_server_changed` fires (intended).
- Rotating a credential (same key, new value) → signature unchanged → no false-positive signal (intended).

### 8.3 Worktree-safe testing

Per [feedback memory `feedback_worktree_editable_install_shadow.md`](https://memory), tests for `packages/analyzer/mcp_admin/` need `conftest.py` rebinding to ensure the worktree's code is loaded, not the main repo's. Test fixture pattern: temp `{shared_dir}` under `tmp_path`, synthesized `openclaw.json` files for fake bots, no real subprocess calls except in dedicated integration tests.

### 8.4 Generator infrastructure cost

Per memory `feedback_rsi_low_cost_preference.md`, the monitor and curator default to pure Python:
- Inventory parsing — pure JSON read.
- Diff against allowlist — pure equality on signatures.
- Vetting status check — pure metadata lookup.
- Catalog-promotion heuristic in the curator — pure Python; an LLM-judge upgrade is a future escalation, not v1.

### 8.5 First-version conservatism

For v1 the monitor only **observes and signals** — it does not enforce. Enforcement (refusing to start a gateway whose `openclaw.json` has un-allowlisted servers, or pushing managed-policy gates that block self-mutation fleet-wide) comes after we trust the signal quality. This mirrors how `security_warden` matured.

---

## 9. Phasing

Each phase is a clean stopping point — could ship a PR, demo to the operator, gather feedback before continuing.

**Phase A — Read-only inventory monitor. ✅ shipped.** Inventory reader, empty pod-wide allowlist as the default baseline, four baseline signal types (`mcp_unknown_server`, `mcp_server_changed`, `mcp_self_mutation_enabled`, `mcp_openclaw_config_missing`), read-only matrix view in the admin UI. Validates the data model with zero risk to the pod.

**Phase B — Catalog + install path. ✅ shipped.** Catalog data model with two seed entries (GitHub MCP + Filesystem MCP), three appliers (`InstallMcpServer` / `RemoveMcpServer` / `UpdateMcpServerConfig`), proposal flow via the existing arbiter pipeline, wrapper-script credential binding for stdio, catalog-browser + install modal admin UI.

**Phase C — Health probes + new signal types. ✅ shipped (stdio only).** `probe_stdio()` runs an MCP `initialize` handshake against each installed stdio server, classifies into eight outcome classes including `credential_invalid`. Per-(bot, server) JSONL health log. `mcp_server_unhealthy` + `mcp_server_credential_invalid` signals fire on N consecutive failures. Health badges on the inventory table; probe-history modal. Usage tracking deferred — see §10 open question 2 below.

**Phase D — CVE feed + signal. ✅ shipped (advisory feed only).** Daily refresh of GitHub Security Advisories per catalog package (24h rate-limited), cross-reference against installed servers, `mcp_server_cve_match` signal per (bot, server, advisory). CVE badges + advisory modal in UI. Catalog entries with open advisories have their Install button disabled. Scope drift, cost attribution, and auto-retirement curator deferred — see §10 below.

**Phase E — Completion: http transport + auto-restart. ✅ shipped.** `InstallMcpServerApplier` handles http catalog entries (resolves keystore references at install time, writes literal headers into openclaw.json). `probe_http()` mirrors the stdio classification for url-based servers including auth-failure → `credential_invalid` mapping. All three appliers auto-trigger `deploy.restart_gateway(bot_id)` after a successful config write so the operator doesn't have to restart manually. Spec doc + open-question list updated to reflect what remains genuinely deferred.

**Total shipped: A–E, six commits.** Remaining work below (§10) is genuinely blocked on out-of-MCP-scope dependencies and tracked as follow-ups, not in-scope completion.

---

## 10. Open questions and follow-ups

Status reflects the end of Phase E.

### Resolved during implementation

1. **OpenClaw env-var interpolation in `mcp.servers[name].env`.** ✅ Resolved by the wrapper-script pattern (§5.3) — the wrapper resolves keystore references at exec time, so OpenClaw doesn't need to do env interpolation.

3. **`team-bot-b` bot deploy state.** ✅ Inventory monitor fires `mcp_openclaw_config_missing` for team-bot-b — the right posture finding. The signal is the operator's surface to address it; no silencing needed.

4. **Allowlist scope: pod-wide vs per-bot.** ✅ Shipped per-bot with a `bot_id="*"` wildcard syntax reserved for future pod-wide entries. No real demand for pod-wide rules emerged through Phases A–C.

5. **Auto-vs-human approval for low-risk installs.** ✅ Phase B ships always-human; no demand to relax.

6. **CVE-feed source selection.** ✅ Phase D uses GitHub Security Advisories only. NVD + npm audit deferred until a gap shows up.

### Genuinely deferred (out-of-MCP-scope blockers)

2. **Tool-call telemetry source — blocks usage tracking and scope drift.** OpenClaw's plugin SDK exposes `before_agent_run` / `before_model_resolve` / `agent_end` / `llm_output` — none are per-tool-call. The Evolve plugin's `TurnObserver` already hooks all four and writes structured annotations but doesn't surface tool_use blocks. The spec's preferred path (PostToolUse hook injected by deploy.py) doesn't have a hook event to attach to.
    - **Unblocks:** the `mcp_server_unused` retirement signal and `mcp_scope_drift` security signal (both require per-tool-call observation).
    - **Required work:** either (a) extend the Evolve plugin's `TurnObserver.handleAgentEnd()` to extract tool_use records from the turn payload and write them to `{shared_dir}/mcp/usage/<bot>/<YYYY-MM-DD>.jsonl`, or (b) wait for OpenClaw to expose a PreToolUse/PostToolUse hook in its plugin SDK.
    - **Effort:** Path (a) is a few hours of TypeScript work in the Evolve plugin and a one-version coordination on the deploy side. Path (b) is OpenClaw-upstream work.
    - **Triggers re-evaluation:** when the Evolve plugin sees a maintenance window, or when OpenClaw ships a plugin SDK update that includes tool-call hooks.

7. **Cost attribution — depends on Budget Hawk v2.** Per-tool-call token attribution requires (a) usage telemetry (above) and (b) Budget Hawk v2's lineage primitives. Tracked separately under [docs/pending-ideas.md](pending-ideas.md) §4.

8. **Auto-retirement via curator generator.** When `mcp_server_cve_match` fires, automatically produce a `RemoveMcpServer` proposal so the operator has one-click revocation. The existing UI (CVE badge + advisory modal + per-row Remove button) covers the operator workflow without a curator, so this is sugar rather than essential.
    - **Required work:** a new generator under `packages/analyzer/generators/mcp_curator/` (charter + observe + evaluate, matching the existing security_warden pattern).
    - **Effort:** ~2–3 days.
    - **Triggers:** when CVE matches become frequent enough that the manual-remove flow is friction.

---

## 11. Non-goals

- **Not a generic MCP gateway.** Evolve doesn't sit on the wire between bots and MCP servers. The wrapper script is purely for credential injection; the real connection is OpenClaw ↔ server, unmediated.
- **Not a replacement for OpenClaw's `/mcp` slash command.** Bots that need ad-hoc, ephemeral MCP servers for a single session still can — if `commands.mcp` is explicitly set true (which the monitor will signal on). For long-lived production MCP, this spec is the path.
- **Not a tool-call firewall.** Stopping individual tool calls mid-session is a hook-side concern, not an MCP-config-administration concern. The Lasso-style PostToolUse prompt-injection scanner is complementary, not this spec.
- **Not exhaustive vetting.** v1 vetting is operator-attestation. Source-code review, runtime sandbox testing, and behavioral profiling are deferred to a possible v2 of catalog vetting.

---

## 12. Test strategy summary

| Layer | Tests |
|---|---|
| `mcp/inventory.py` | Unit tests with synthesized `openclaw.json` files covering: empty, single stdio, single http, multiple servers, missing-file, malformed-JSON |
| `mcp/catalog.py` + `mcp/allowlist.py` | Unit tests: round-trip read/write, signature stability across reorders, fingerprint mismatch detection |
| `mcp/monitor.py` | Integration: synthesized state directories, run the monitor, assert correct signals fire to a temp signal store |
| Appliers | Integration: full proposal → apply → verify cycle in a temp tree, assert `openclaw.json` written correctly and allowlist updated. Bot-config files faked; no real subprocess |
| Wrapper script generator | Unit: snapshot test of generated wrapper for representative catalog entries |
| Probe runner | Integration: against a fixture stdio MCP server (echoes initialize) and an http server (Flask test app) |
| Web routes | Integration: API contract tests for the matrix/catalog/install endpoints |
| End-to-end | One per phase: a single happy-path scenario exercising the full pipeline against a sandboxed `{shared_dir}` |

The test conftest must rebind `evolve_admin` and `analyzer` packages to the worktree's source per `feedback_worktree_editable_install_shadow.md`.
