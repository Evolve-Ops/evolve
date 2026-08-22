---
title: "Evolve — What It Is and How It Works"
slug: overview
audience: public
last_reviewed: 2026-06-05
concepts:
  - what-is-evolve
  - architecture
  - three-layer
  - evo
  - pods
  - better-engine
ui_surface: null
related_specs: []
---

# Evolve — What It Is and How It Works

Evolve packages OpenClaw for households, professional services, and small operators — the way **Ubuntu packages Linux** or **Plex packages media files**. It runs on the same host as your bots (macOS and Linux/Ubuntu) and adds pod-level visibility, cost control, security monitoring, paired-user management, backup, and a Better Engine adaptation loop on top of OpenClaw. At the center is **evo** — an OpenClaw bot that knows your pod end-to-end and resolves things in conversation.

---

## The Three-Layer Architecture

```
Layer 1: Bot users (admin-bot, team-bot-a, etc.)
         → Run OC gateways. Do the actual work via Telegram/Slack/Discord.

Layer 2: evolve user (dedicated OS user)
         → Manages and monitors the pod. Runs admin server, analysis, security audits.
         → Cannot be influenced by the bots it manages.

Layer 3: Admin user (you — the human operator)
         → Has sudo access. Approves proposals, manages keys, deploys updates.
         → The only human in the loop.
```

## Three Buckets (plus floating Chat / Dashboard and a Developer bucket)

**Operate** — Observe current pod state + handle what's broken.
- Usage, Reports, Plugins, Security, Maintenance, Backup, Terminal

**Improve** — Add new capabilities + tune existing + review recommendations.
- Skills, Apps, Recommendations, AI Optimization, Cost Optimization

**Settings** — Configure who + what + how.
- Getting Started, Settings, Users, Help

**Developer** — For pod owners working on Evolve itself.
- Inbox, Errors, Feedback

Floating above the buckets: **Chat** (evo conversation surface) and **Dashboard** (live pod status).

### OpenClaw admin coverage

Evolve has end-to-end coverage of every config surface OpenClaw exposes — each one inventoried, baselined, and changeable only through approved proposals:

- **MCP servers** — pod-curated catalog of vetted servers, install/remove/update through proposals, advisory feed from GHSA, posture view on the Security tab. (Spec: `docs/spec-mcp-administration-2026-05-10.md`)
- **Plugins** — per-bot inventory of `plugins.entries`, allow/deny lists, install-source allowlist, load paths, plus a curator generator that proposes the right allow list for each bot. (Spec: `docs/spec-plugin-inventory-2026-05-10.md`)
- **Hooks** — webhook ingress block + per-plugin typed-hook policies (`allowConversationAccess` / `allowPromptInjection`), trusted-mutator allowlist, transforms-dir integrity hashing. (Spec: `docs/spec-hook-governance-2026-05-10.md`)
- **Content scan** — pattern catalog over the markdown files each bot reads at session start, catching HTML-comment injection / zero-width Unicode / authority-impersonation / encoded-payload / structural-emptiness shapes. Mark-Reviewed suppression flow with 30-day TTL. (Spec: `docs/spec-prompt-injection-scanner-2026-05-10.md`)
- **Permissions posture** — read-only inventory of the per-bot permission stack. (Spec: `docs/spec-permission-posture-2026-05-10.md`)

---

## Key Concepts

**Bot**: An OpenClaw instance running as a dedicated OS user (e.g., `admin-bot`). Each has its own gateway, API keys, and channels.

**Pod**: The full set of bots managed by a single Evolve installation on one host.

**Gateway**: The OC server process that handles each bot's messaging. Runs as a launchd service.

**Plugin**: TypeScript code that runs in-process inside each bot's OC gateway. Annotates every turn, routes models, exposes metrics. Installed via `evolve-admin deploy <bot>`.

**Shared directory**: `/Users/Shared/evolve/` — single source of truth for pod state. All bots write here; none can delete each other's files.

**Model tiers**: Evolve routes every session to the right model automatically.
- **tier1 / `power`**: Opus-class — explicit user requests for deep work
- **tier2 / `standard`**: Sonnet-class — default for productive sessions
- **tier3 / `fast`**: Haiku-class — background tasks, maintenance sessions, analysis
- **tier0 (Judge)**: Different provider from tier2 — unbiased evaluation

**Session class**: Every session is classified as `productive` (real user work), `maintenance` (debugging, config), or `ambiguous`. The dispatched tier follows a cascade: trigger-kind anchor → per-user-per-bot default → operator's bot-wide default → fallback. Maintenance sessions are automatically routed to tier3 to save cost.

**Per-user tier**: Anyone using a bot can set their own default tier via `evo tier fast|standard|power|auto` — persisted per user per bot. Operators set the bot-wide default on the AI Optimization page or with `evo tier-default`.

**Suggestions**: Changes the analysis engine wants to make to a bot. Always require human approval. Go through security review, forge validation, and auto-rollback on failure.

---

## The Improvements Loop (Better Engine)

```
Session happens → plugin annotates every turn → observation tuples land in {shared_dir}
  ↓ on each coach's cadence (on_demand / hourly / daily / weekly)
A coach reads observations + system state → emits a Suggestion
  ↓ at ingest
The arbiter checks the charter's invariants → routes the suggestion to the queue
  ↓ every refresh cycle
The referee ranks the queue (urgency × track record + tiebreak)
  ↓ you approve / snooze / dismiss / reject in Recommendations
Evolve applies config patches for you; Apps → Forge Jobs handles heavier changes
  ↓ for suggestions carrying a falsifiable claim
Check-in verifies the metric in the claim's window (1d / 7d / 30d)
  → claim confirmed: coach's track record rises
  → claim refuted: revert / flag / escalate per the suggestion's revert plan
```

The legacy v1 pattern-detector pipeline was retired in the Better Engine pipeline unification (May 2026). All scoreboard, compliance, and suggestion paths now flow through the unified Signals → Proposals stream. Whimsy is guaranteed when the queue is otherwise empty.

---

## Common CLI Commands

```bash
# Check pod status
evolve-admin status

# Deploy/update Evolve plugin on a bot
evolve-admin deploy <bot-id>

# Check model assignments
evolve-admin models list
```

Improvement suggestions are reviewed and approved on the **Recommendations**
page in the dashboard. (Continuity Engine follow-ups have no queue page —
bots schedule their own; the Maintenance page shows runner health.)

---

## Admin UI Pages

| Page | Bucket | What it's for |
|------|--------|--------------|
| Chat | (floating) | Conversation surface with evo — chat-as-home, with per-page session context |
| Dashboard | (floating) | Live pod status at a glance, plus the Better Engine recommendations strip |
| Usage | Operate | Spend by model, channel, source — Sessions tab: productive vs maintenance ratios, session browser |
| Reports | Operate | User-configured digest subscriptions and report history |
| Plugins | Operate | Per-bot external capabilities. Sub-tabs: **Plugins** (Messaging / LLM Providers / Tools / Infrastructure sections), **Credentials**, **Embeddings**, **MCP Servers**, **Hooks**. Bot header chip strip shows runtime + reachable-via at a glance. |
| Security | Operate | Per-bot audit (eight categories), config health, drift detection, plus posture views: **MCP Posture**, **Plugin Posture**, **Hook Posture**, **Content Scan** (markdown-file injection-pattern catalog with Mark-Reviewed suppressions) |
| Maintenance | Operate | Gateway status, cron jobs, infra jobs, OC version, logs, admin server, MCP bridge, system upgrade, pod health |
| Backup | Operate | Cloud (private GitHub per bot), Local (Time Machine), Data (per-bot default tier + per-app overrides), Recovery (rollback from latest backup) |
| Terminal | Operate | In-browser shell into bot accounts (for the cases the UI doesn't yet cover) |
| Skills | Improve | Per-bot skill inventory — catalog, installed skills, credentials, activity |
| Apps | Improve | App manifests, files registry, orphan scan, test status — Installed / Gallery / Forge Jobs / Reliability tabs |
| Recommendations | Improve | The arbiter proposal queue. Sub-pages: Generators (charters + track records), Snapshots (calibration), Observations (raw signal stream). |
| AI Optimization | Improve | Model catalog, per-bot default tier picker, tier definitions, routing rules |
| Cost Optimization | Improve | Per-bot tile row, efficiency scoring, context settings, daily caps (auto-trip L1 breaker), Model × Audience table, Spike Explorer |
| Getting Started | Settings | Onboarding tour |
| Settings | Settings | Pod / Network / Bot config and module enable/disable/tune |
| Users | Settings | Pod admins, self-claim passphrases, per-bot owners, paired users per channel, auto-approval |
| Help | Settings | Per-page help docs |
| Inbox | Developer | Issues filed by users via `evo bug` / `evo feature` |
| Errors | Developer | Aggregated error reports across the pod |
| Feedback | Developer | User-submitted feedback collected by evo |
