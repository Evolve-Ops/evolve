# OpenClaw Admin Coverage — Investigation Roadmap (2026-05-10)

Status: **research roadmap, not committed scope.** Each candidate is a starting point for spec work, not a task ready to execute.

## Why this doc exists

Evolve administers a pod of OpenClaw instances. The original framing emphasized cost, integrations, security audit (file-hash level), conduct injection, signal store, and the RSI proposal pipeline. OpenClaw itself has grown a lot of admin-relevant configuration surfaces in the last 6–9 months — plugins, hooks, MCP servers, sub-agents, skills, sandbox config, telemetry export, managed-policy gates — and the Claude Code community has surfaced concrete pain points around several of them (CVEs, runaway-cost incidents, supply-chain attacks, approval fatigue).

This document maps the gap and proposes candidates for further investigation, in rough priority order. It does **not** lock scope, and each Tier 1 item should get its own spec before implementation.

**Cross-cutting:** The admin UI absorbs each of these new surfaces via [spec-admin-ui-integrations-restructure-2026-05-10.md](spec-admin-ui-integrations-restructure-2026-05-10.md) — per-bot configuration lives under a renamed "Integrations" tab with sub-tabs (Channels / Credentials / Embeddings / MCP Servers / Plugins / Hooks / Sandbox); cross-bot posture lives under Security. Each new surface plugs into a sub-tab and a posture section as its spec lands.

## What Evolve already covers

For reference (so we don't relitigate it):

- **Cost / spend.** Per-bot daily/monthly tracking, anomaly detection, spend caps, cost profiles, efficiency scoring (`packages/analyzer/cost.py`, `cost_watchdog.py`, `spend_alert.py`).
- **Identity & config audit.** SHA256 of SOUL/AGENTS/HEARTBEAT every 15 min, gateway/port/exec/sudoers/launchd integrity, machine-level posture (`packages/analyzer/audit.py`).
- **API keys & credentials.** Encrypted vault + per-integration health (`packages/admin/evolve_admin/keystore.py`, `packages/analyzer/oc_keys.py`).
- **Bot lifecycle.** Setup wizard, deploy, upgrade, removal, manifest scanning.
- **Integrations.** Channel status (Slack/Telegram/Discord), OAuth freshness, GitHub/Brave/Google probes.
- **Conduct injection.** Pod-wide POD_CONDUCT.md → session_start.
- **Signal store.** Unified observation layer with state machine; ~10 monitors writing.
- **RSI / proposals.** 11 generators competing on track record, verify daemon grading.
- **Embedding providers.** Registration, health, per-bot selection.
- **Pod health.** Composite score, host metrics, gateway liveness, heal daemon.
- **Application manifests + tests.** Behavioural / file / http / script regression tests.

## Tier 1 — high-impact, well-fitted to Evolve's pod model

The three at the top of this tier are the ones with the strongest near-term case (concrete CVEs and well-documented community pain) and the cleanest fit into the existing audit / signal-store / proposal pipeline.

### 1.1 MCP server inventory + vetting

**Status: spec drafted** → [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md). The spec covers inventory, catalog, install/remove/update appliers, credential binding, health probing, usage tracking, scope-drift detection, and retirement. The summary below is preserved for context.

**Gap.** Evolve has an "MCP Bridge" route under `/api/mcp/*` but no per-bot inventory of which MCP servers each bot has configured, no allow/deny enforcement, no vetting workflow on new servers, and no signal when an unknown server appears.

**Why now.**
- CVE-2025-6514 — RCE in `mcp-remote` (437K downloads). First real-world full RCE against an MCP client.
- Mitiga (April 2026) — MCP OAuth-token MitM via `~/.claude.json`, survives token rotation, invisible in endpoint UI.
- Docker's "MCP horror stories" series + agentic-community/mcp-gateway-registry / Microsoft mcp-gateway / IBM mcp-context-forge / Portkey MCP Gateway all targeting "shadow MCP" in the enterprise.
- TrustFall ("Comment and Control") — malicious MCP shipped in a repo abuses auto-approve.

**OpenClaw primitives to leverage.**
- `allowedMcpServers` / `deniedMcpServers` / `allowManagedMcpServersOnly` — managed-policy gates.
- MCP server config locations per bot (`.openclaw/...` — needs investigation; see MCP investigation spec).

**Slot-in shape.**
- New monitor (e.g. `mcp_inventory`) writing Signals on new/changed/unknown MCP servers.
- Pod-wide allowlist as a new piece of shared state under `{shared_dir}`.
- `security_warden` extension to propose deny-rule additions.
- New admin-UI panel showing the per-bot MCP matrix.

**Open questions.** Resolved or carried into the spec — see [spec-mcp-administration-2026-05-10.md §10](spec-mcp-administration-2026-05-10.md). Notable resolutions:
- Config lives at `openclaw.json → mcp.servers` (confirmed against schema).
- Vetting in v1 is operator-attestation; sandbox/static-analysis deferred to v2.
- First-party OpenClaw plugins are separate from MCP servers and don't currently ship MCP. Plugin inventory (Tier 1.4) merges into this spec only if/when that changes.

---

### 1.2 Hook governance + content audit

**Status: spec drafted** → [spec-hook-governance-2026-05-10.md](spec-hook-governance-2026-05-10.md). Covers both OpenClaw hook surfaces (webhook ingress and plugin typed hooks), drift monitor extending `audit.py`, baseline file replacing the current deploy-time enforcement, four proposal action kinds, three-phase rollout. Summary below preserved for context.

**Gap.** Evolve hashes SOUL/AGENTS/HEARTBEAT for drift detection but doesn't enumerate or audit hook configuration per bot, doesn't validate hooks against a baseline, and doesn't surface "what fires across the fleet."

**Why now.**
- CVE-2025-59536 — RCE specifically via pre-trust hook execution.
- The per-bot hook opt-in incident (memory: `project_oc_per_bot_hook_optin.md`) is exactly the kind of silent-misconfig the audit doesn't catch today.
- Hooks are the primary policy lever (PreToolUse can block tool calls, SessionStart can inject context, PermissionRequest can auto-approve/deny). They're also the primary attack surface if poisoned.

**OpenClaw primitives.**
- `allowManagedHooksOnly` — fleet enforcement gate.
- 13+ hook event types (PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop, PermissionRequest, PreCompact, ConfigChange, InstructionsLoaded, SubagentStart/Stop, Notification, Setup).

**Slot-in shape.**
- Extend `audit.py` to enumerate per-bot hooks and write a Signal on diff.
- Pod-wide "expected hooks" baseline (a la POD_CONDUCT but for hooks).
- Admin-UI panel: hook matrix per bot per event type.

**Open questions.**
- Does Evolve already write hooks during deploy? If yes, baseline is whatever deploy installs; if no, we need a separately-managed baseline.
- Should baseline live in code (deploy-time) or in `{shared_dir}` (operator-editable)?

---

### 1.3 Permission posture + bypass-mode detection

**Status: spec drafted** → [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md). Covers three OpenClaw surfaces (permission config in openclaw.json, exec-approvals.json runtime store, cron/jobs.json scheduled invocations), a composite posture classifier, denylist enforcement for approvals and cron payloads, cron `agentTurn` cap audit (absorbing Tier 2.5), and sandbox config inventory (absorbing Tier 2.3). Five new proposal action kinds.

**Gap.** Evolve writes `openclaw.json` and tracks `exec-approvals.json` but doesn't surface a unified view of permissions (allow/deny/ask), `defaultMode`, `disableBypassPermissionsMode`, or sandbox config across bots. Whether any bot is running in `bypassPermissions` mode (or invoked with `--dangerously-skip-permissions`) is invisible.

**Why now.**
- Approval fatigue is the most-cited Claude Code UX complaint. ~100 prompts/hour drives users to YOLO-mode.
- `--dangerously-skip-permissions` on host machines is widely flagged as the highest-risk single behavior.
- Phoenix Security shipped three command-injection CVEs specifically against the non-interactive / headless mode.

**OpenClaw primitives.**
- 6 permission modes: default, plan, auto, dontAsk, acceptEdits, bypassPermissions.
- `permissions.disableBypassPermissionsMode: "disable"` — managed-policy lockout.
- `sandbox.enabled` + `sandbox.network.allowedDomains`.

**Slot-in shape.**
- Add to existing audit: report each bot's `defaultMode` and any bypass-mode usage.
- Signal severity scaled by mode (bypassPermissions = critical).
- Cron-job scan for headless `claude -p` invocations missing `--max-turns` / `--max-budget-usd` (catches the CI-runaway class).

**Open questions.**
- Are any pod bots intentionally running headless / non-interactive (forge, healers)? Need to enumerate before flagging.

---

### 1.4 Plugin inventory

**Status: spec drafted** → [spec-plugin-inventory-2026-05-10.md](spec-plugin-inventory-2026-05-10.md). Covers four sub-surfaces (entries, allow/deny, install provenance, load paths), bootstrap-from-memory of the per-bot integration mapping, six signal types, six action kinds. v1 work item: 5-of-6 bots currently lack a `plugins.allow` list — Phase A's curator generates one proposal per bot to close the gap. Marketplace handling deferred to Phase C.

**Gap.** OpenClaw shipped a plugin marketplace this spring; Evolve doesn't track plugins per bot, version drift, or unknown sources.

**Why now.**
- Plugins ship hooks + MCP servers + skills + sub-agents — multiplier on every other risk surface.
- Plugin marketplace is brand new; supply-chain vetting is community-organized at best.
- OpenAI adopted the Skills format Dec 2025 — cross-vendor standard, same supply chain.

**OpenClaw primitives.**
- `enabledPlugins`, `allowedMarketplaces`, `blockedMarketplaces`, `strictKnownMarketplaces`, `pluginTrustMessage`.

**Slot-in shape.**
- New monitor (`plugin_inventory`) writing Signals on installs / version drift.
- Per-bot plugin manifest in admin UI.

**Open questions.**
- Likely overlaps heavily with 1.1 (MCP) — plugins ship MCP servers. Consider unifying as a single "extension surface" monitor.

---

### 1.5 Indirect prompt-injection scanner for AGENTS.md / READMEs

**Status: Phase A landed (2026-05-11)** → [spec-prompt-injection-scanner-2026-05-10.md](spec-prompt-injection-scanner-2026-05-10.md). Extends `audit.py` with a regex/heuristic pattern catalog (HTML-comment payloads, zero-width Unicode, authority-impersonation framings, instruction-negation, encoded blocks, structural emptying). Critical FP guard: allowlist of Evolve's own HTML markers (`evolve-handoff:*`, `evolve-managed:*`). Operator-curated catalog file. `UpdateContentScanCatalog` proposal + applier ships for catalog edits; bot file remediation rides existing SoulEdit rails. Phase B (catalog editor UI, suppression graduation) pending.

**Gap.** Evolve hashes AGENTS.md but doesn't scan content for known injection patterns. The April team-bot-a AGENTS.md truncation was an availability incident; the parallel concern is *content* injection.

**Why now.**
- HiddenLayer / NVIDIA / SecurityWeek writeups on AGENTS.md prompt injection.
- Injection payloads in HTML comments are invisible on rendered GitHub but present in LLM context.
- Subcommand-chain injection (SC Media) bypasses deny rules.

**OpenClaw primitives.** None directly — this is content-side defense.

**Slot-in shape.**
- Extend identity audit to do content scanning: HTML comments, suspicious instruction markers, unusually long blocks of text relative to the file's typical baseline.
- New Signal type: `agents_md_injection_suspected`.

**Open questions.**
- False-positive rate. Heuristics need calibration; might want LLM judge (but `feedback_rsi_low_cost_preference.md` says default to pure Python).

## Tier 2 — natural extensions

### 2.1 OpenTelemetry as the pod's collector

OpenClaw exports tokens / costs / tool-use / errors via OTEL when `CLAUDE_CODE_ENABLE_TELEMETRY=1`. Community has standardized on OTEL → Prometheus → Grafana (5+ OSS dashboards). Evolve currently reconstructs cost from `turns-{date}.jsonl`. Switching/augmenting via OTEL would give per-tool attribution and cleaner cost lineage — partial answer to the deferred Budget Hawk v2 forensics in `pending-ideas.md`.

**Slot-in.** Set env vars during `evolve-admin deploy`; run a local OTEL collector under `{shared_dir}`; consume in cost monitors.

### 2.2 Anthropic Analytics API + Compliance API ingestion

Both shipped recently as official admin surfaces. Analytics gives sessions / LOC / PRs / accept-rates per model; Compliance gives the audit-event feed. Both partial (no Cowork, no conversation content) but useful as cross-checks against locally-derived metrics.

### 2.3 Sandbox config governance

`sandbox.enabled` + network allowlist. Pairs naturally with bypass-mode detection (1.3) — they're the two halves of "is this bot actually contained?"

### 2.4 Auto-memory inventory

OpenClaw writes auto-memory under `~/.openclaw/workspace/memory/` (the `MEMORY.md` index plus dated entries and topical subdirs) with a sibling embedding + FTS index at `~/.openclaw/memory/main.sqlite` — grows passively, can contain sensitive content, isn't backed up by Evolve's git-backup pipeline. Inventory + size trend + optional backup. Distinct from the user-profile passive-growth pattern (memory: `feedback_profile_passive_growth.md`); this is the bot's own auto-memory, not the user's profile. (Pre-rename Claude Code used `~/.claude/projects/<project>/memory/`; that path is no longer populated.)

### 2.5 Headless / CI guard

Scan `.openclaw/cron/jobs.json` for unbounded `claude -p` invocations missing `--max-turns` / `--max-budget-usd`. Catches the Phoenix-Security CVE class.

## Tier 3 — drift detection, lower urgency

- **Sub-agent / skill / slash-command drift** across bots.
- **Output styles / status line / keybindings** inventory.
- **`minimumVersion` enforcement.**
- **Conversation transcript content audit** (PII / secret scanning) — Compliance API explicitly excludes content; Evolve has local access. Memory `project_security_warden_capture_policy.md` covers a related-but-narrower capture for security_warden; this would be broader.

## Recommended start order

**Now (this thread):** 1.1 MCP server inventory. Strongest CVE case, cleanest fit into existing pipeline, and a useful template for 1.2/1.3/1.4 — they share the "enumerate per-bot config → diff against baseline → signal → propose" shape.

**After 1.1 lands:** 1.2 hook governance and 1.3 permission posture in parallel (they share the audit-extension shape). 1.4 plugin inventory likely merges into 1.1 once we see the actual config layout.

**Then:** revisit 1.5 and Tier 2 with whatever we learn about the per-bot config shape from 1.1–1.4.

## Cross-cutting open questions

- **Discovery vs. enforcement.** First-version monitors should *observe* and *signal*, not enforce. Enforcement (pushing managed-policy gates fleet-wide) comes after we trust the signal quality.
- **Baseline storage.** New shared state under `{shared_dir}` for "expected MCP / expected hooks / expected plugins" — needs a consistent layout. Probably `{shared_dir}/policy/` or similar. Worth a small-spec pass before the first monitor lands.
- **Pod-vs-bot scoping.** Some policy is genuinely per-bot (team-bot-a's MCPs differ from admin-bot's), some is pod-wide (no bot should run in bypassPermissions). Need a way to express both in the baseline.
