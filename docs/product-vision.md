# Evolve — Product Vision (internal)

*Last updated: 2026-05-30*

This is the internal, deeper-detail companion to the public vision at
[gitpages/product-vision.md](gitpages/product-vision.md). The framing is the
same — what differs is depth: pod architecture, on-disk layouts, the security
audit categories, the proposal/signal stores, the sandbox, the MCP bridge. If
anything here contradicts the live code, the code wins. File an issue or open
a PR.

---

## What Evolve is

OpenClaw is powerful but unfriendly. Evolve packages it for **households,
professional services businesses, and small operators** — people who can install
software and care about their data, but don't want to spend weekends managing
AI infrastructure.

The analogy that holds best: **Evolve is to OpenClaw what Ubuntu is to Linux,
or what Plex is to media files.** The underlying stack is open, capable, and
intimidating. The packaging layer is what turns it into something a person can
pick up and run. We don't replace OpenClaw — we assemble it into a friendly
product.

**At the center is evo: an OpenClaw bot that knows your pod end-to-end and
resolves things in conversation.** You say *"snooze every team-bot-a alert
until tomorrow"* or *"fix the cron caps issue"* — evo finds the matching
change, applies it, verifies it worked, reports done. Underneath sits a full
dashboard with real depth — usage graphs, credentials, applications, security
audits, self-improvement suggestions, paired-user management, backup — for
when you want to dig in. Chat handles the common path; the dashboard is there
when you want detail.

The whole experience is delivered through OpenClaw itself. The bot at the heart
of Evolve isn't a wrapper around someone else's API — it's a real OpenClaw bot,
configured through the same files, gateway, and tool-use mechanisms that govern
every other bot on your pod. We use the product to deliver the product.

---

## The two surfaces

### Evo — the usage layer

**Chat is the home page.** When you open the admin UI, the first thing you see
is evo's short report on the state of things — pod-wide spend, what's firing,
what's been added — plus a conversation thread to address any of it. The report
is written on a heartbeat (no slow LLM call on page load), and a "refresh"
button forces an immediate update.

**A panel on every page.** Wherever you are in the dashboard, evo is on the
right with the context of that page. Open Alerts and ask *"why is this
firing?"* — evo sees the same signals you see. Open the Usage page and ask
*"who's spending most this week?"* — evo sees the same numbers. Switch pages
and evo's context switches with you; each page keeps its own conversation
thread.

**Same brain everywhere.** Chat in the dashboard, DM evo on Telegram, or use
the `evo` keyword from any bot's thread on Signal / iMessage / Slack /
Discord. One bot, one set of tools, one long-term memory. Improvements to
evo's instructions land everywhere at once.

**It actually resolves things.** The old workflow was: an engine generates
suggestions, they queue up on a Recommendations page, you click through them.
The new workflow is: you describe the problem to evo, evo finds the matching
suggestion (or stages one), applies it end-to-end, verifies, reports. The
Recommendations page is still there — useful for audit, for triage, for
changes you want to review carefully — but most of the time you don't need it.

### The dashboard — the depth

Evo is the usage layer; under it is a real admin dashboard with substance to
dig into. Today's sidebar:

- **Operate**: Usage, Reports, Plugins, Security, Maintenance, Backup, Terminal.
- **Improve**: Skills, Apps, Recommendations, AI Optimization, Cost Optimization.
- **Settings**: Getting Started, Settings, Users, Help.
- **Developer**: Inbox, Errors, Feedback.

Floating above all of it: Chat and Dashboard.

The dashboard isn't decoration. It's a full management surface — chat is just
the friendly front door.

### When to use which

- **Chat for the common path.** *"What needs attention?"* *"Fix Team-Bot-A."*
  *"Snooze every alert until tomorrow."* *"Why did spend spike yesterday?"*
- **Dashboard for inspection and depth.** Charts you want to study.
  Configuration you want to edit precisely. Audit trails you want to walk
  through. Anything where you want to *see* rather than ask.

You don't have to choose. The two surfaces share the same data, the same
tools, and the same memory.

---

## Capabilities today

What's actually installed and running. Counts are auditable against the repo.

### Build, keep alive, keep safe, make useful, make better, make easy, make fun

The website organizes capabilities as a seven-layer hierarchy. The internal
view of the same layers, with deeper hooks into specs and on-disk state.

**1. Build them.** `evolve-admin setup --fresh` takes a bare Mac mini to a
running pod in one pass — service user, OpenClaw install, plugin deploy,
launchd jobs, first bot. `evolve-admin deploy <bot>` adds a bot in five
minutes (its own macOS user, workspace, credentials). The safe-upgrade
preflight (`internal/spec-safe-upgrade-2026-05-02.md`)
gates OC version bumps so the pod doesn't skid on a breaking release.

**2. Keep them alive.** Per-gateway liveness, per-channel health, per-key /
per-token freshness. The heal daemon restarts what's down. Maintenance is
the reactive surface; Reports lets you subscribe to digests (daily cost,
weekly review, integration health).

**3. Keep them safe and affordable.** Security audits run every 15 minutes
across eight categories (see "Security" below). Usage tracking gives daily /
monthly spend per bot, per model, per session class with forecasts and
anomaly detection. Cost Optimization carries per-bot tile rows with
configurable daily caps that **auto-trip an L1 cost breaker** when crossed
(disables heartbeat sessions; gateway calls go to a refuse-turn sentinel).
AI Optimization holds the tier-routing rules.

**4. Make them useful.** Skills (capability primitives — Gmail, Calendar,
Slack, Discord, Telegram, iMessage, Obsidian, Notion, Linear, Home
Assistant, AutoCAD, Runway, the upstream OC plugins). Applications
(goal-shaped recipes — Morning Briefing, Email Triage, Note-taker, EA Pack,
Workspace Backup, GitHub Integration). Plugins (per-bot view of what's
installed, separating platform plugins from user-facing skills). Pod Conduct
(the universal behavioral floor injected into every bot).

**5. Make it better.** The Better Engine: a portfolio of specialized
generators (guardians, optimizers, a meta-guardian), each emitting falsifiable
proposals graded by a verify daemon. Continuity Engine for cross-session
follow-through (bot-scheduled defers). App Gallery for one-click installs; Forge for spec-driven
generation. Approval pipeline gates every production change.

**6. Make it easy.** Evo at the center — chat-as-home, panel on every page,
reachable as a Telegram DM or the `evo` keyword from any bot's thread. The
Claude Desktop MCP bridge brings pod context into deep-work sessions.

**7. Make it fun.** Whimsy in voice, status messages, and a few surprises
along the way. Personality without a mascot. The brand cross-checks are
Tailscale, Notion, and Plex.

### Users — paired-user management

Multi-user bots in OpenClaw require a `/start`-then-`pairing approve` dance
that previously meant SSH-ing into the bot's account and running the OC CLI.
The Users page absorbs that into the admin UI:

- **Pod-wide identity at top** — pod admins (messaging), self-claim
  passphrases, per-bot owners. (Migrated from the old Settings → Identity
  sub-tab, which no longer exists.)
- **Per-bot tile rail** — pick a bot, see its panel below. Matches the
  established Cost Optimization / Capabilities pattern.
- **Users by channel** — approved users (the OC `allowFrom` list) and
  pending pairing requests, per channel.
- **One-click approve / reject / disconnect.** Pod-admin-claimed IDs
  **auto-approve on sight** (no code round-trip needed for your own
  `/start`); auto-approvals emit an audit Signal.
- **Single-user ↔ multi-user toggle** per bot, from the panel header.
- **Name enrichment** — channel API lookups via `name_resolver` cover
  Telegram (`getChat`), Slack (`users.info`), and Discord (`/users/<id>`),
  with a 7-day cache TTL. Slack also surfaces `profile.email` when the
  `users:read.email` scope is present, gated behind a per-page
  "Show emails" toggle.
- **Pending badges** on bot tiles and in the sidebar so demand is visible
  without clicking through.

Spec: `internal/spec-per-bot-users-management-2026-05-29.md`.

### Backup — its own top-level page

Backup used to live under Maintenance → Recovery. It's now a top-level page
with five subtabs:

- **Status** — roll-up of cloud + local backup state across the pod.
- **Cloud** — private GitHub repo per bot. Pre-flight size estimate so a
  push doesn't surprise you. Post-push classification audit that flags
  paths that shouldn't have shipped. Auto-prune of reclassified paths.
  All endpoints unified under `/api/backup/cloud/*`.
- **Local** — Time Machine status, exclusion sync for ephemeral paths
  (cache dirs, scratch, log spool).
- **Data** — per-bot **default tier** for what's eligible to back up, with
  per-app overrides and bulk apply. Three-tier classification per app
  (cloud / local / none). Forge stamps a bot's `backup_default_tier` onto
  new manifests on install.
- **Recovery** — `git checkout` from the latest backup, restore-to-machine
  flow, recovery instructions for drive loss or host swap.

Cloud is the durability story; Local is the "undo recent accidents fast"
story. Most pods want both.

### Tier cascade — per-bot and per-user

Model routing is hierarchical: anchor a session class on `trigger_kind` →
resolve the operator's bot-wide default → resolve the per-user override →
dispatch.

- **AI Optimization → per-bot default tier picker.** Pick `fast` /
  `standard` / `power` per bot. Single source of truth for the bot's
  ordinary turns (#1786).
- **`evo tier-default` / `evo tier` keyword.** Operators set the bot's
  default from inside a chat; member users override their own session
  class without operator intervention (#1788).
- **Per-user-per-bot persistence.** A user's choice is stored at
  `{sharedDir}/{botId}/user-tier-prefs.json` keyed by
  `derive_user_key(channel, ext_id)`. ModelRouter applies the per-user
  choice above the operator's bot-wide default; "auto" deletes the entry
  rather than persisting a no-op (#1791).
- **Routing audit.** A `tier_routing_disagreement` detector watches for
  divergence between classifier intent and dispatched tier and surfaces
  the rate as a Signal (#1794, #1782).
- **Post-deploy gate.** `verify_tier_chain.sh` runs after every deploy
  and fails CI if the chain is broken (#1766).

### Cost Optimization — per-bot tiles + auto-trip caps

Per-bot tile row mirrors the Capabilities pattern (#1747, #1750 for primary
first / alpha tiebreak). Each tile shows current spend, the configured daily
cap, and a "Model × Audience" miniature that spots premium models being
chosen by autonomous (non-Human) audiences (#1773). When a bot crosses its
`daily_cap_usd`, the cost breaker trips: heartbeat sessions are disabled
and gateway calls route to a refuse-turn sentinel rather than burning
through tokens (#1483 safety-net sprint).

### The Better Engine — resolves things, doesn't just suggest them

The Better Engine is Evolve's self-improvement layer. It runs as a portfolio
of specialized **generators** — small, deterministic Python jobs that watch
how your pod is doing and propose specific changes when something can be
better.

Three roles in the ensemble:

- **Guardians** — Sysadmin Watchdog (substrate health), Budget Hawk (cost),
  Security Warden (safety). They flag trouble.
- **Optimizers** — propose improvements (efficiency, gap-fillers, deprecation,
  plugin curation, investigators).
- **Meta-guardian** — Evolve Watchdog watches the engine itself.

Every suggestion carries a **falsifiable claim** (e.g. *"this reduces gateway
restarts ≥30% over 7 days"*) and a **revert plan**. A verify check returns
at the claim's horizon and confirms, reverts, flags, or escalates. Generators
that land good suggestions gain authority; generators that miss lose it.

**Pipeline unification (shipped, 2026-05-24).** The old scoreboard /
compliance / suggestions pathways have been consolidated through the
Signals→Proposals flow. The adapter set is now Onboarding + Whimsy +
ProposalReader only; whimsy is guaranteed when the queue is otherwise empty.

**Smarter generators.** Investigate-before-propose: a shared toolkit
(correlated_signals, proposal_history, peer_baseline, etc.) lets generators
gather evidence before they fire. Two reference implementations
(`bloat_investigator`, `exec_outcome_investigator`) and a
`root_cause_attribution` block on `Provenance.signals` to thread cause
through proposals. Spec:
`internal/spec-smarter-generators-2026-05-28.md`.

**The new shape:** the suggestion queue is *inventory* — the system's
running record of "things that could be fixed." Evo is the *resolver* —
when you describe a problem in chat, evo looks for a matching proposal,
offers to apply it, applies it, and verifies. The Recommendations page
is still there for triage, but most of the time the proposal flow runs
through chat. **RSI on applications, not skills:** Evolve doesn't ship
a system that secretly rewrites your bot's prompts. Every change is
reviewable, revertible, and (depending on your authority tier) explicitly
approved before it lands.

### Evo's tool surface

Evo's behavior is governed by the same files OpenClaw uses for any other
bot — SOUL.md (voice), AGENTS.md (operating rules), TOOLS.md (the tool
catalog), MEMORY.md (long-term memory), USER.md (who you are),
HEARTBEAT.md (background behavior). Improvements to evo are edits to those
files, not changes to a hidden prompt template in Python.

Underneath, evo has hands: a growing set of OpenClaw tools that wrap
Evolve's pod state and actions. Read tools query firing signals, pending
proposals, host metrics, bot status, audit findings, costs, paired users,
backup state. Action tools apply or reject proposals, redeploy or restart
bots, snooze or dismiss signals, install gallery apps, kick off audits or
investigations. Each tool carries a risk tier (`read` / `write_safe` /
`write_risky` / `destructive`) that determines whether evo auto-runs it or
asks you to confirm — driven by an "authority tier" setting you control
(`ask` / `auto-small` / `auto`).

The catalog grows as Evolve grows. Adding a new evo capability is a single
new tool file plus a regenerated TOOLS.md — both done as part of a normal
Evolve deploy.

### Evo subcommand registry

Thirty-five subcommands today, registered in
`packages/admin/evolve_admin/evo/subcommands.py::_REGISTRY`. Notable
additions in the latest wave: `tier`, `tier-default`, `intake`, `bug`,
`feature`, `revise`, `improve`, `security`. The bare `evo` returns the top
recommendation; subcommands are role-gated (Anyone / Primary+Admin / Admin).

### Continuity Engine — invisible glue

Bot sessions are stateless by default. When a bot commits to acting later,
it schedules the follow-up itself via its `defer` tool, and a background
runner fires it at the promised time — delivering a stored message or
running the deferred work as a short agent turn. It's the thing that keeps
a bot's promises across conversations without you having to remind it.

### Pod Conduct — the behavioral floor

Every bot in your pod shares a `POD_CONDUCT.md` file that defines the
universal rules — honesty about state, no empty commitments, privacy and
data handling, safety before completion, scope awareness. Bots can have
their own personality (SOUL.md), but they can't override the floor.
Amendments require human approval.

---

## Architecture and principles

### Design principles

Fourteen load-bearing principles govern how Evolve looks, behaves, and is built. Each lives in its own file under `docs/principle-*.md` and is cited by name in code review and spec docs.

**Audience and UX**
- **[Design for the Plex Test](principle-plex-test.md)** — primary surfaces must be usable by Marcus (the persona below) without Stack Overflow or LLM lookup; no internal jargon in user-facing copy.
- **[Alerts Must Explain and Remediate](principle-alerts-explain-and-remediate.md)** — every chip, banner, toast, or notification must explain itself (what / why / impact / severity) and provide a concrete next step (or be explicitly informational). Hallucinated remediation is a bug.

**Privacy and security architecture**
- **[LLM Inference Over User Data Runs Inside Each Bot](principle-per-bot-inference.md)** — every LLM call that sees user data runs inside the bot that owns it, with that bot's own credentials, on that bot's macOS account. Privacy by architecture.
- **[Each Bot Applies Its Own Changes (No Cross-User Writes)](principle-each-bot-applies-its-own-changes.md)** — approved proposals are applied by the target bot in its own user context; `/Users/Shared/evolve/` is the message bus.
- **[`security_rules.json` Is Not Modifiable by the Proposal Pipeline](principle-no-self-modification.md)** — the reviewer's mandate is immutable from inside the pipeline; the auto-reject `no_self_modification` rule enforces it.
- **[LLM-Extracted Inline Code Is Always `needs_approval`](principle-inline-task-needs-approval.md)** — prompt injection cannot manufacture autonomous code execution; the auth-level forcing happens at the extractor.

**Provider and model**
- **[Evolve is LLM-Provider-Agnostic](principle-llm-provider-agnostic.md)** — no code path presumes or compels a provider; provider-aware surfaces light up only on credential presence.
- **[Self-Checks Should Run Cross-Vendor (Anti-Goodhart)](principle-judge-tier-differs-from-workhorse.md)** — recommendation, not a gate: when a self-evaluation call runs on the same provider that produced the work, it over-rates its own family's output. Realized as a call-site derivation (`resolve_cross_vendor`) over the standard chain — a second provider's key is what enables it — and the AI Optimization page nudges operators toward one, but no behavior is blocked without it.
- **[Apps Inherit the Bot's LLM Stack](principle-apps-inherit-bot-llm.md)** — Forge-installed apps must not credential themselves or call provider APIs directly; LLM work routes through the bot's gateway so tier-walk, `daily_cap_usd`, cost monitoring, and prompt caching govern the call.

**Operational invariants**
- **[Signals Precede Proposals (Monitor → Signal Store → Generator → Proposal)](principle-signals-precede-proposals.md)** — every condition worth acting on appears in the Signal store first; generators read Signals, never raw state.
- **[Cost Cap Trips to a Refuse-Turn Sentinel, Not Silent Overage](principle-cost-cap-refuse-turn.md)** — when a bot exceeds `daily_cap_usd`, the cost breaker actually stops billing (heartbeat off, turns refused).
- **[Tri-State Status — `null` ≠ `0`](principle-tri-state-status.md)** — detectors return a sentinel when they cannot measure, distinct from a real zero; silent degradation to "looks fine" is a bug.
- **[Instrument Outcomes Before Optimization Machinery](principle-instrument-outcomes-before-optimization.md)** — an outcome signal must demonstrably fire on real production data before its associated optimization machinery is worth building; if the signal can't tell good outcomes from bad above noise, any optimization grading against it is unfalsifiable. Adopted after the 2026-06-06 cascade-controller arc shipped five PRs of detection improvements on top of an outcome signal that didn't work.
- **[Apps Minimize Per-Turn Context Cost](principle-apps-minimize-bootstrap-cost.md)** — apps prefer cron over heartbeat when no LLM is needed; prefer subagent invocation over the bot's main session when one is; and budget `bot_guidance` + INSTALLED_APPS.md footprint when heartbeat is genuinely the right hook. Adopted after the 2026-06-07 Atlas heartbeat-bloat incident where four apps' invisible per-turn injection compounded with a misapplied cost profile.

New principles should follow the same template: a short principle doc with clauses, code implications, anti-patterns, what-it-is-not, why-it-matters, and references. The [operator-message-style.md](operator-message-style.md) style guide is the operational expression of the audience/UX principles for chat messages, and is CI-enforced. The OC CLI vs ACL-read split is documented inline in [architecture.md](architecture.md) §"Architecture Principle: OC CLI for Live State" rather than as a standalone principle doc, because it belongs with the OC-integration context.

### Evo is a real OpenClaw bot

Evo runs on its own macOS user (currently the `evolve` service account, with
separation onto a dedicated `evo` account specced
as Phase E follow-on), with its own OpenClaw gateway, its own
SOUL/AGENTS/TOOLS/MEMORY files, its own tier-routing (Sonnet for chat
reasoning, Haiku for narrative heartbeats, Opus as fallback). The admin
UI's chat surface is a thin HTTP proxy to evo's gateway — there's no
parallel Anthropic call in admin Python, no hand-rolled system prompt, no
impostor stack. Operators tune evo through standard OpenClaw mechanisms:
edit SOUL.md, propose an AGENTS.md change, add a tool.

### Per-page sessions, shared memory

Each page in the admin UI has its own OpenClaw session with evo — Usage,
Security, Alerts, Apps, Users, Backup, the standalone Chat page, all
separate threads. Switch pages and your conversation context switches with
you. Close and reopen a page and your conversation resumes. **MEMORY.md is
shared across sessions** — durable facts evo learns on one page are
available to every other page from the next turn forward.

### Privacy by architecture

Every bot's LLM inference runs inside that bot, with that bot's own
credentials. There's no centralized inference service inside Evolve that
sees user data. Filesystem-shape skills (iMessage, Obsidian) never make a
network call — your iMessage history is read on the same Mac it's stored
on, by the bot that owns it, and the inference happens through the bot's
own LLM provider. Cross-bot data sharing is opt-in and explicit; bots are
compartmentalized by default.

This is the load-bearing claim for Diana (board-confidential financial
data) and Carla (client-privileged work product). It's also why Evolve
runs locally on a Mac you own, not in our cloud — because we don't have
one.

### Two surfaces, two capability tiers

Direct access to evo (chat in the admin UI or a Telegram DM to the evolve
bot) carries the full tool surface. Indirect access (the `evo X` keyword
used from any other bot's thread) keeps the existing plugin-mediated
dispatcher with role-aware filtering — and *does not* expose evo's tool
catalog to a member bot's LLM. The boundary is intentional: a crafted
prompt on a household bot cannot trigger evo to take an action. The two
surfaces share the same underlying bot; the difference is in what's
reachable from where.

### In-house OAuth substrate

After vetting Nango, ACP/GatewayStack, ContextForge, and a handful of other
candidates against the licensing and self-host bar, we built the OAuth
substrate ourselves. The result is a provider registry where each new SaaS
integration is a single file. Breakeven against adopting an external
substrate was around five or six providers; we passed that before v2.1
closed. The substrate is small, owned, and not paywalled.

### Safety as a flagship feature

The voice we use internally is **"vigilant by default, friendly by design."**
Concretely:

- Every proposed change to a bot's config or behavior travels through a
  signed pipeline, a security review, and a human approval gate. Evo's
  action tools are part of this pipeline, not a bypass of it.
- The security audit runs every fifteen minutes against eight categories
  of pod drift, with findings surfaced in plain language rather than
  jargon.
- Security alerts use a dedicated channel separate from operational ones,
  so a misconfigured Telegram channel doesn't silence them.
- Architectural safety claims that can't be backed by a measurement are
  not surfaced as guarantees.

A safety claim that isn't true is worse than no claim. The Security page
leads with measured audit findings, not aspirational assertions about what
a bot "can't" do.

### Substrate strategy

Evolve is OpenClaw-first today, standards-aligned for tomorrow. The
ecosystem is converging on MCP for tools, agentskills.io for portable
skills, and A2A for inter-agent communication. We design Evolve's
abstractions around those standards so substrate optionality is preserved
without paying the engineering cost of supporting three runtimes today.

The companion substrate we *did* adopt was **Opik** for observability
(Apache-2.0, self-hostable, OpenTelemetry-compatible swap path).
Everything else (Composio, Nango, ACP, ContextForge, ClawTrace, signal-cli)
was cut after vetting against the licensing and use-case bar.

---

## The pod architecture

Every Evolve installation has three roles cleanly separated:

```
Layer 1: Bot users (per bot — one macOS user per OC instance)
         → Do the actual work. Each runs an OC gateway.
         → Cannot read each other's workspaces. Cannot reach the
           management layer.

Layer 2: evolve user (dedicated macOS service account)
         → Runs the admin server, scheduled jobs, the analyzer,
           security audits, the signal/proposal stores.
         → Evo lives here today (its own OC bot, separate gateway).
         → Spec for moving evo onto its own `evo` macOS user is at
           spec-evo-account-separation-2026-05-25.md — the
           privileged service account stops holding a tool surface
           that could be exfiltrated.

Layer 3: Admin user (the human operator, e.g. pod-admin)
         → Has sudo access. Approves proposals, manages keys,
           deploys updates.
         → Does NOT run an OC bot — sysadmin and assistant roles
           stay separate.
         → The only human in the loop.
```

This separation is intentional. Bots cannot influence their own management
layer. The evolve user has no Telegram token of its own beyond evo's; all
other operator interaction goes through the admin UI or the evolve CLI.

### On-disk layouts

- **Shared dir** (`{sharedDir}`, typically `/Users/Shared/evolve/`) is
  owned by the `evolve` user. It holds the signal store, proposal store,
  generator records, profiles, observations, watchdog logs, calibration
  snapshots, and per-bot `user-tier-prefs.json`. The `proposals/` and
  `signals/` subtrees carry an inherited ACL granting the future `evo`
  macOS user `read,write,delete,append` so evo's action tools can move
  proposals and apply signal state transitions directly (see CLAUDE.md
  Post-evo-account-separation exception). The invariant is enforced by
  `ensure_pod_perms()` on every deploy.
- **Per-bot dirs** (`/Users/<bot>/.openclaw/…`) carry the OC config and
  credentials. The admin server gets macOS ACL read on these via
  `set_evolve_read_acl(bot_id)`; writes go through `/tmp` staging + `sudo
  /bin/cp` because the files are bot-owned. See [CLAUDE.md](../CLAUDE.md)
  for the canonical read/write patterns.

### Signal store (alerts / observation layer)

Every monitor (pod_report, audit, watchdog, host_health, error_reporter,
integration_probe, pod_health, security_warden, test_runner) writes Signals
to `{sharedDir}/signals/`; the Alerts page reads them. Distinct from the
Proposal store — generators write Proposals, monitors write Signals, and
`Proposal.motivating_signals[]` links one to the other. Spec:
`internal/spec-alerts-signal-store-2026-05-07.md`.

### Arbiter (RSI proposal store)

The RSI arbiter lives at `{sharedDir}/proposals/` with state-routed subdirs
(`pending`, `snoozed`, `applied`, `archived`). Charters ship in code at
`packages/analyzer/generators/<id>/charter.yaml` and are immutable at
runtime (fingerprint check). Approval audiences are explicit per proposal:
`pod_operator`, `bot_primary_user`, `both`, or `none` (autonomous).
Anything touching auth, tools, channel config, gateway core, app
install/removal, or bot specialization always routes to a human.

---

## Pod Conduct

Every bot in a pod shares a behavioral floor: `POD_CONDUCT.md`.

This is a single file, owned by Evolve, injected into every bot's session
context (via `session_surface.py` → `TurnObserver.systemAppend`; no length
cap, no redeploy). It defines universal rules that no individual bot can
override:

- Honesty about application and state
- No empty commitments
- Privacy and data handling
- Safety before completion
- Scope awareness

Individual bots keep their own SOUL.md for personality and role-specific
rules. POD_CONDUCT.md handles the universal ethical and behavioral
contract. Amendments go through the proposal/approval system — human
approval required.

---

## Security — what runs and what it checks

Four-layer architecture.

**Layer 1 — Pipeline integrity (HMAC signing).** Every proposal is signed
at creation and verified at every gate (review, apply). An unsigned or
tampered proposal is rejected before it can do harm. The signing key lives
in the keystore, owned by the evolve user.

**Layer 2 — Git backup + drift detection.** Each bot's security-relevant
state (`openclaw.json`, SOUL.md, AGENTS.md, HEARTBEAT.md, USER.md,
TOOLS.md, evolve-tiers.json) is committed nightly to a private GitHub
repository through the Backup pipeline. After each backup, the live state
is diffed against the committed state. Any change not accounted for by a
recent approved proposal → 🔴 CRITICAL alert. Recovery: `git checkout
HEAD:openclaw.json` from the Backup → Recovery tab.

**Layer 3 — Continuous audit (`audit.py`, every 15 minutes).** Eight
categories:

1. **Identity** — SHA256 hash of each bot's SOUL.md and AGENTS.md vs.
   last backup.
2. **Content scans** — regex / heuristic pattern catalog over the
   markdown files the bot reads at session start (SOUL.md, AGENTS.md,
   HEARTBEAT.md, USER.md, README.md, POD_CONDUCT.md, etc.). Catches
   indirect prompt-injection payloads (HTML-comment instructions,
   zero-width Unicode, authority-impersonation framings, long base64/hex
   blocks, subcommand-chain abuse, structural emptying).
3. **Config posture** — gateway bind addresses, exec allowlist, sudoers
   grant integrity, hooks governance.
4. **MCP servers, plugins, hooks** — per-bot inventory + baseline drift
   detection across the three OpenClaw extension surfaces (specs:
   `spec-mcp-administration-2026-05-10.md`,
   `spec-plugin-inventory-2026-05-10.md`,
   `spec-hook-governance-2026-05-10.md`).
5. **Permission posture** — exec-allowlist coverage, app-derived
   permissions reconciliation
   (`spec-app-derived-permissions-2026-05-24.md`).
6. **Machine-level** — firewall status, SSH config
   (PasswordAuth / PermitRoot), new user accounts, unexpected listening
   ports.
7. **Cost** — daily spend vs. configured thresholds, burst detection,
   premium-models-on-autonomous spotter.
8. **Tier-routing disagreement** — divergence between classifier intent
   and dispatched tier (#1794, #1782).

**Layer 4 — Alert independence (dedicated security token).** Security
alerts use a separate Telegram bot token, stored separately from the
general notification channel. If Evolve's general alert channel is
misconfigured or broken, security alerts still reach the operator.

**Proposal auto-reject rules** (hard rules in `security_rules.json`, not
modifiable by proposals): no 0.0.0.0 gateway binding, no auth disable, no
modification of Evolve's own scripts, no credential file writes, no `sudo`
in proposed scripts, no outbound network calls in proposed scripts, no
writes outside the bot's workspace, no launchd plist modifications.

---

## The Sandbox

Every Evolve installation includes a Sandbox bot — a dedicated, isolated
OC instance owned by the evolve user for proposal pre-validation.

- Proposals are deployed to Sandbox first.
- Test cases from application manifests are run against Sandbox.
- Results presented to operator before production deployment.
- Sandbox is reset (wiped to baseline) before each test run.
- No real user data ever enters Sandbox.

The Sandbox closes the loop between "proposal generated" and "production
deployed": every change is tested before it reaches the bots users
depend on.

---

## Claude Desktop integration (MCP bridge)

Evolve includes an MCP Bridge that connects Claude Desktop to the live pod
over Tailscale VPN — no SSH tunnels required.

**What it unlocks:**

- Claude Desktop starts a session with full pod context: evo's memory,
  pending tasks, active proposals, recent metrics.
- Notes and context written in Claude Desktop appear in evo's workspace.
- Evolve proposals are reviewable from Claude Desktop.
- The "deep work" tool (Claude Desktop / Max subscription) and the
  "ambient" tool (the always-on OC pod) share the same context layer.

**Network path:**

```
Laptop (Claude Desktop)
  → Tailscale VPN
    → Mac mini: Evolve MCP Bridge (port 5051)
      → Pod workspace and metrics (read from all bots)
      → Primary context bot (write — evo by default)
```

The MCP Bridge is configured via `evolve-admin` and requires Tailscale on
both the Mac mini and the operator's laptop.

---

## Who Evolve is for

The audience is mildly tech-capable individuals and small operators. We
use three illustrative personas as design constraints — they shape what
we ship, but you don't need to fit one to use Evolve.

- **Marcus** — solo professional (lawyer / accountant / designer /
  consultant). One or two bots. The **Plex test** persona: if he can
  install Plex and run Home Assistant, he can run Evolve.
- **Diana** — multi-bot operator (CEO with an EA who handles setup).
  Four or five compartmentalized bots; cross-bot synthesis through evo.
  Lives in Signal or iMessage; may never open the dashboard.
- **Carla** — service business with many concurrent client projects
  (designer, contractor, planner, real estate agent). Studio bot triages
  comms; project bot per active engagement; client-facing access with
  visibility boundaries.

Personas are design constraints, not blueprints we hardcode. We don't ship
"the Marcus template" or "the Carla template" as fixed packages — those
would calcify. We build composable pieces (skills, applications,
escalation rules, visibility boundaries) and let templates emerge once we
have enough Lego blocks.

### Who Evolve isn't for

- **Enterprise platform teams.** If you have a dedicated AI ops function,
  look at Preloop. Evolve doesn't try to be your platform.
- **Developers who want a coding agent.** That's a different category. We
  share OpenClaw as substrate but not audience.
- **People who want a hosted SaaS.** Evolve runs on your hardware, on
  purpose. There's no cloud control plane to sign up for.
- **Anyone who needs SOC 2 / HIPAA enterprise audit logging today.** The
  signal store and security audits are robust for individuals and small
  operators; formal enterprise compliance is not a v1 promise.

---

## Before you install

Full requirements: [docs/pre-install-checklist.md](pre-install-checklist.md).

**Hardware:**

- Mac mini (M4, 16GB RAM minimum; M4 Pro, 24GB recommended)
- macOS 14 (Sonoma) or later
- Wired ethernet for 24/7 reliability

**Required accounts (minimum viable pod):**

- One LLM provider (Anthropic, OpenAI, Ollama, etc.)
- One messaging channel (Telegram, Slack, Discord, iMessage)

**Strongly recommended:**

- Brave Search API key
- Google OAuth credentials JSON (Gmail, Calendar, Drive)
- Tailscale (admin UI access from laptop + MCP Bridge)
- Private GitHub repo (Backup → Cloud destination + drift detection)

**Optional (add after first bot is running):**

- Second Telegram bot token for dedicated security alerts
- Slack / Discord bot tokens
- GitHub personal access token (for coding bots)
- ElevenLabs, Runway, Perplexity, Home Assistant

**Software prerequisites (pre-wizard):**

- Python 3.9+ (`python3 --version`)
- Node.js 20+ (`brew install node`)
- Admin account with sudo access
- Evolve repo cloned to `/Users/Shared/evolve-repo/`

Estimated time with everything ready: ~30 minutes. Estimated time gathering
all accounts cold: 90 minutes.

---

## What we don't claim

Honesty about what isn't here matters as much as enthusiasm about what is.

- **We don't have a public user base yet.** Evolve runs on the author's
  mini and a small set of friend-of-the-project pods.
- **The evo proxy isn't fully shipped.** Phase 4 of the OC-native
  architecture (the production admin-UI-to-evo proxy, per-page sessions,
  tool-call buttons with dry-run validation) is the next big lift. The
  admin UI chat works today, but until Phase 4 lands some surfaces still
  route through the legacy stack.
- **The `evo` account separation hasn't happened yet.** Evo runs on the
  privileged `evolve` user today; the cutover to a dedicated `evo` macOS
  user (spec written) is Phase E follow-on.
- **We don't ship per-persona templates as a current feature.** Marcus,
  Diana, and Carla shape what we build, but you won't find a "Carla
  starter pack" button.
- **We don't have Signal or WhatsApp support today.** Signal was cut
  after vetting (license + maintainer-risk concerns); WhatsApp is on the
  queue but not shipped.
- **Multi-bot handover (the Diana flow) isn't done.** The architecture is
  there; the end-user onboarding flow isn't yet.
- **The Carla activation wave isn't done.** Client-facing project bots
  with visibility boundaries and escalation rules are next on the
  roadmap, not current capabilities.
- **Enterprise audit logging is not in scope for v1.**

---

## What Evolve is not

- Not a replacement for OpenClaw — it's a layer on top.
- Not a conversational assistant on its own — it's infrastructure that
  happens to be reached through a conversational front door.
- Not a security product you rely on alone — the security protocol provides
  strong detection and audit, but physical access to the Mac is still the
  ultimate trust boundary.
- Not autonomous — every change to production bots requires human approval.

---

*This doc reflects state through the v3 wave (May 2026). The companion
[gitpages/product-vision.md](gitpages/product-vision.md),
[gitpages/index.html](gitpages/index.html),
[architecture.md](architecture.md), and
[applications-vs-skills.md](applications-vs-skills.md) align with the same
model. If anything here contradicts the live code, the code wins — file an
issue or open a PR.*
