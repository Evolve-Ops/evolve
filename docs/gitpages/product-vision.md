# Evolve — Product Vision

*Last updated: 2026-06-06*

---

## What Evolve Is

OpenClaw is powerful but unfriendly. Evolve packages it for **households, professional services businesses, and small operators** — people who can install software and care about their data, but don't want to spend weekends managing AI infrastructure.

The analogy that holds best: **Evolve is to OpenClaw what Ubuntu is to Linux, or what Plex is to media files.** The underlying stack is open, capable, and intimidating. The packaging layer is what turns it into something a person can pick up and run. We don't replace OpenClaw — we assemble it into a friendly product.

**At the center is evo: an OpenClaw bot that knows your pod end-to-end and resolves things in conversation.** You say *"snooze every team-bot-a alert until tomorrow"* or *"fix the cron caps issue"* — evo finds the matching change, applies it, verifies it worked, reports done. Underneath sits a full dashboard with real depth — usage graphs, credentials, applications, security audits, self-improvement suggestions — for when you want to dig in. Chat handles the common path; the dashboard is there when you want detail.

The whole experience is delivered through OpenClaw itself. The bot at the heart of Evolve isn't a wrapper around someone else's API — it's a real OpenClaw bot, configured through the same files, gateway, and tool-use mechanisms that govern every other bot on your pod. We use the product to deliver the product.

---

## The two surfaces

Evolve gives you two ways into the same pod, each with its own strengths.

### Evo — the usage layer

**Chat is the home page.** When you open the admin UI, the first thing you see is evo's short report on the state of things — pod-wide spend, what's firing, what's been added — plus a conversation thread to address any of it. The report is written on a heartbeat (no slow LLM call on page load), and a "refresh" button forces an immediate update.

**A panel on every page.** Wherever you are in the dashboard, evo is on the right with the context of that page. Open Alerts and ask *"why is this firing?"* — evo sees the same signals you see. Open the Cost page and ask *"who's spending most this week?"* — evo sees the same numbers. Switch pages and evo's context switches with you; each page keeps its own conversation thread.

**Same brain everywhere.** Chat in the dashboard, DM evo on Telegram, or use the `evo` keyword from any bot's thread on Signal / iMessage / Slack / Discord. One bot, one set of tools, one long-term memory. Improvements to evo's instructions land everywhere at once.

**It actually resolves things.** The old workflow was: an engine generates suggestions, they queue up on a Recommendations page, you click through them. The new workflow is: you describe the problem to evo, evo finds the matching suggestion (or stages one), applies it end-to-end, verifies, reports. The Recommendations page still exists — useful for audit, for triage, for changes you want to review carefully — but most of the time you don't need it.

### The dashboard — the depth

Evo is the usage layer; under it is a real admin dashboard with substance to dig into.

- **Usage** — daily and monthly spend per bot, per model, per session class. Forecasts, anomaly detection, per-bot drill-downs.
- **Plugins** — gateway liveness, channel health, API key and OAuth token freshness, integration probes per provider.
- **Security** — fifteen-minute audit cycles across eight drift categories: config posture, integrity hashes, plugin posture, content scans, machine-level, cost, tier-routing, and more. Signals in plain language.
- **Maintenance** — active alerts, the heal daemon's restart history, daemon status.
- **Backup** — Cloud (private GitHub per bot, size estimate, classification audit, auto-prune), Local (Time Machine), Data (per-bot default tier + per-app overrides for what's eligible to back up), Recovery (one-click rollback from latest backup).
- **Users** — pod admins, self-claim passphrases, per-bot owners, and paired-user management per channel. Approve, reject, or disconnect with a click. Your own `/start` auto-approves so you don't have to round-trip a code.
- **Reports** — user-configured digests (daily cost, weekly review, integration health).
- **Skills, Apps, Recommendations, AI Optimization, Cost Optimization** — everything you'd want from a real management dashboard.

The dashboard isn't decoration. It's a full management surface — chat is just the friendly front door.

### When to use which

- **Chat for the common path.** "What needs attention?" "Fix Personal-Bot." "Snooze every alert until tomorrow." "Why did spend spike yesterday?"
- **Dashboard for inspection and depth.** Charts you want to study. Configuration you want to edit precisely. Audit trails you want to walk through. Anything where you want to *see* rather than ask.

You don't have to choose. The two surfaces share the same data, the same tools, and the same memory.

---

## Capabilities today

What's actually installed and running, as of the v3 wave. Counts are auditable against the repo.

### Skills — capability primitives

A skill is one thing your bot can do well: send a Slack message, read your Gmail inbox, save a file to Obsidian. Skills come from OpenClaw and the wider ecosystem; Evolve handles install, credentials, and per-bot configuration.

Shipped today, grouped by where they live in a bot's daily life:

- **Messaging** — Slack, Discord, Telegram, iMessage (Mac-local, no cloud roundtrip), WhatsApp
- **Productivity** — Gmail, Calendar, Google Workspace suite (Docs / Sheets / Drive, read + write via vetted MCP), Obsidian, Notion, Linear, Dropbox, Zoom
- **Local and home** — Apple-local file access, Home Assistant
- **Creative and CAD** — Runway, AutoCAD
- **Upstream OpenClaw plugins** — surfaced through the same installer

New MCP-backed skills (Notion, Linear, Runway, Zoom, GitHub-MCP, and the Google Workspace suite) land via a **bundled-plugin pattern** — the catalog entry pulls in the upstream MCP server, the OAuth flow, and the per-bot capability picker as one unit. Adding a new hosted service is still a single registry file, but the surface behind it is now substantially larger.

Two skills (iMessage, Obsidian) are filesystem-shape and run entirely on your machine — no API calls, no cloud roundtrip. The same install interface works for OAuth-shape and filesystem-shape skills alike, which means adding a new one is a single file in `packages/admin/evolve_admin/skills/`.

### Applications — goal-shaped recipes

A skill alone is rarely what you want. You don't think "I want to call the calendar API"; you think "I want a morning briefing." An **application** is a contract — a goal, the skills it leans on, tests that prove it still works, and a way for the system to know whether it's actually delivering.

The Gallery includes Morning Briefing, Email Triage, Note-taker, Calendar Summary, EA Pack, Journal, Task Manager, Contacts, and more. Install once. The application handles the orchestration, the scheduling, and the delivery. Evolve tracks whether it's hitting its contract and proposes improvements when it isn't. See [applications-vs-skills.md](applications-vs-skills.md) for the full distinction.

**Apps inherit the bot's LLM stack.** A formal principle as of June 2026: apps must not credential themselves directly — they route LLM calls through their host bot's gateway, so cost, audit, and tier-routing apply to every call the same way. The forge install gate enforces this on every new app.

### App Coherence &amp; Reconciliation — the manifest is the source of truth

A June 2026 addition. Each `files[*]` entry in a bot's manifest is now typed (`code` / `config` / `contract` / `behavior_doc` / `content` / `data` / `log` / `state`) and tagged with provenance — either *authored* (a contract you or the forge wrote) or *observed* (something the scanner found). Three checks run before a manifest deploys:

- **Reconciliation** — does what's on disk match the contract? Authored fields out of sync surface as chips; observational drift updates silently.
- **Coherence** — does the manifest's own claims hang together? Declared recurring behaviors must have declared mechanisms; declared inputs must be enumerated; declared outputs must have a code path. Most of this is pure-Python graph walks; the LLM-tiered passes (C2/C3) handle the genuinely fuzzy work.
- **Audit** (existing, unchanged) — does the code do what the manifest claims?

A pre-deploy coherence gate blocks anything that fails the contract; `evo app-changes`, `evo app-coherence`, and `evo app-scan` drive the whole flow from chat. The Apps page shows findings inline; an event-driven scan reschedules itself when manifests or files change.

### The Better Engine — resolves things, doesn't just suggest them

The Better Engine is Evolve's self-improvement layer. It runs as a portfolio of specialized generators — small, deterministic Python jobs that watch how your pod is doing and propose specific changes when something can be better.

Three roles in the ensemble:

- **Guardians** — Sysadmin Watchdog (substrate health), Budget Hawk (cost), Security Warden (safety). They flag trouble.
- **Optimizers** — suggest improvements (efficiency, gap-fillers, deprecation, plugin curation, and others).
- **Meta-guardian** — Evolve Watchdog watches the engine itself.

Every suggestion carries a falsifiable claim (e.g. *"this reduces gateway restarts ≥30% over 7 days"*) and a revert plan. A verify check returns at the claim's horizon and confirms, reverts, flags, or escalates. Generators that land good suggestions gain authority; generators that miss lose it.

New in June 2026: cross-bot generators (`engagement_amplifier`, `pod_capability_lift`) surface patterns that only show up across the fleet — a behavior one bot has learned worth lifting to another, a contradicted alignment between two bots, a capability one bot exercises that another's manifest claims but never uses. An **anti-domain** layer lets you mark a topic out of scope so generators stop pitching it. And the **Config Intent** ledger records *why* you set a config value the way you did, so intent-aware generators (`cron_caps_filler` and the rest) consult it before drafting a proposal — closing the "if drifted, propose revert" failure mode permanently.

**The new shape:** the suggestion queue is *inventory* — the system's running record of "things that could be fixed." Evo is the *resolver* — when you describe a problem in chat, evo looks for a matching proposal, offers to apply it, applies it, and verifies. The Recommendations page is still there for triage, but most of the time the proposal flow runs through chat. **RSI on applications, not skills:** Evolve doesn't ship a system that secretly rewrites your bot's prompts. Every change is reviewable, revertible, and (depending on your authority tier) explicitly approved before it lands.

### Evo's tool surface

Evo's behavior is governed by the same files OpenClaw uses for any other bot — SOUL.md (voice), AGENTS.md (operating rules), TOOLS.md (the tool catalog), MEMORY.md (long-term memory), USER.md (who you are), HEARTBEAT.md (background behavior). Improvements to evo are edits to those files, not changes to a hidden prompt template in Python.

Underneath, evo has hands: a growing set of OpenClaw tools that wrap Evolve's pod state and actions. Read tools query firing signals, pending proposals, host metrics, bot status, audit findings, costs. Action tools apply or reject proposals, redeploy or restart bots, snooze or dismiss signals, install gallery apps, kick off audits or investigations. Each tool carries a risk tier (`read` / `write_safe` / `write_risky` / `destructive`) that determines whether evo auto-runs it or asks you to confirm — driven by an "authority tier" setting you control (`ask` / `auto-small` / `auto`).

The catalog grows as Evolve grows. Adding a new evo capability is a single new tool file plus a regenerated TOOLS.md — both done as part of a normal Evolve deploy.

### Users — paired-user management in the admin UI

OpenClaw's per-bot user pairing used to require SSHing into the bot's account and running a CLI command (`openclaw pairing approve …`). That's a Plex-test failure for the audience we care about. The **Users** page absorbs it into the admin UI.

Pod-wide identity lives at the top — pod admins (messaging), self-claim passphrases, per-bot owners. A bot-tile rail below picks a bot and reveals its panel: owner, passphrase override, and **Users by channel** (approved users and pending pairing requests). One click approves, rejects, or disconnects. Pod-admin-claimed IDs **auto-approve on sight** — a sweep matches an incoming `/start` against the known admin list and approves it within seconds, so your own pairing never requires a code round-trip. Each auto-approval emits an audit Signal.

Display names come from the channel API where possible — Telegram (`getChat`), Slack (`users.info`), Discord (`/users/<id>`) — cached for seven days. Slack also surfaces email (when the `users:read.email` scope is granted), gated behind a per-page "Show emails" toggle so the page stays compact.

Each bot has a **single-user ↔ multi-user toggle** in the panel header, so you can switch a personal bot to a shared one (or back) without editing config files. Pending pairing requests show as `●N` chips on bot tiles and as a sidebar badge, so demand is visible at a glance.

### Backup — its own page

Backup used to be a Recovery sub-tab tucked under Maintenance. With more pods running, it earned its own top-level page. Five tabs:

- **Status** — roll-up of cloud and local backup state across the pod.
- **Cloud** — private GitHub repo per bot. Pre-flight size estimate before a push, post-push classification audit that flags paths that shouldn't have shipped, auto-prune of reclassified paths. The repo can also serve drift detection: nightly diffs of `openclaw.json`, SOUL.md, AGENTS.md, HEARTBEAT.md, evolve-tiers.json against the last good commit.
- **Local** — Time Machine status, exclusion sync for ephemeral paths.
- **Data** — per-bot **default tier** for what's eligible (cloud / local / none), with per-app overrides and bulk apply. The Forge stamps new manifests with the bot's default tier on install, so freshly-generated apps start in the right place.
- **Recovery** — restore a bot's state from the most recent backup with a click, or run the host-swap flow if the whole machine moved.

Cloud is the durability story (drive loss, theft, host swap). Local is the "undo recent accidents fast" story. Most pods want both.

### Tier cascade — per bot, per user, per turn

Model routing is hierarchical: anchor the session class on `trigger_kind` → resolve the operator's bot-wide default → resolve a per-user override → dispatch. **AI Optimization** holds the per-bot default-tier picker (`fast` / `standard` / `power`). Inside any bot's chat, the `evo tier-default …` keyword sets the bot's default; `evo tier …` sets a per-session override; both write through the same audited path the UI uses.

Per-user-per-bot preferences are persistent — your choice on Slack-team-bot-a survives reboots and is restored on your next turn. A tier-routing-disagreement audit watches for divergence between classifier intent and dispatched tier and flags it as a Signal. A post-deploy gate (`verify_tier_chain.sh`) fails the deploy if the chain is broken.

### Continuity Engine — invisible glue

Bot sessions are stateless by default. The Continuity Engine extracts pending commitments from session transcripts, surfaces them at the next session start, and runs pre-approved recurring tasks (weekly review, cost summary, project digest) on a schedule. It's the thing that keeps a bot's promises across conversations without you having to remind it.

### Pod Conduct — the behavioral floor

Every bot in your pod shares a `POD_CONDUCT.md` file that defines the universal rules — honesty about state, no empty commitments, privacy and data handling, safety before completion, scope awareness. Bots can have their own personality (SOUL.md), but they can't override the floor. Amendments require human approval.

### Issues — the feedback loop

Say `evo improve "this could be better"` from any thread; evo classifies the issue (is it local environment, Evolve code, an upstream OpenClaw issue, or a mixed concern?), gathers evidence (matching upstream issues via `gh search`, recent firing signals, recent commits in the implicated area), drafts a body, and stages it as a Draft on the Issues page. Promote when you're ready — it lands as a real GitHub issue on the repo you pick (multi-repo subscription is first-class). `evo revise` iterates the draft inline; `evo revise --undo` walks back one revision.

Inbound issues filed by *other* people on repos you maintain get LLM-triaged — category, urgency, draft reply, draft labels, confidence — and queued for review. An opt-in **auto-response policy** with a 24-hour undo handles the obvious cases (close-as-duplicate with citation, label-only, reply with the drafted clarification). Every action is reversible; nothing fires without an explicit per-kind enable and a confidence floor you control.

### Cost &amp; caps — graduated remediation

A June 2026 unification. The four pre-existing cost surfaces collapse into one canonical Cost &amp; Caps matrix per bot (and a POD tab for defaults). Caps come in tiers:

- **Alerts** (`monthly_budget_usd`, `weekly_warn_usd`, `daily_warn_usd`) notify without remediating.
- **Tier downgrade** drops the session-class default to a cheaper tier and pauses non-critical crons.
- **Hard cap** trips an L1 cost breaker that refuses gateway calls until the day rolls.

Caps roll at midnight in the pod's local timezone, not UTC. A behavioral **Cost Efficiency Score** grades each bot's routing choices — premium models picked by autonomous workloads cost a bot points; well-matched tier picks gain them.

---

## Who Evolve is for

The audience is mildly tech-capable individuals and small operators. We use three illustrative personas as design constraints — they shape what we ship, but you don't need to fit one to use Evolve.

### Marcus — the solo professional

A solo lawyer, accountant, designer, or consultant. One bot, one or two domains, self-installing on a Mac mini in his closet. Cares deeply that his client data never leaves his machine. Wants the setup wizard to feel like a tour guide rather than a sysadmin gauntlet. Marcus is the **Plex test** persona: if he can install Plex and run Home Assistant, he can run Evolve.

### Diana — the multi-bot operator

A CEO or investor with a human assistant who handles the setup on her behalf. Four or five specialized bots — household, health, giving, ventures — each compartmentalized so one bot can't read another's data. A primary conversational layer (evo) sees across them so she doesn't have to. Diana lives in Signal or iMessage for the first weeks; she may never open the web dashboard.

### Carla — the project-driven service business

An interior designer, contractor, wedding planner, or real-estate agent. Many concurrent client projects, each with its own context, all sharing rhythms. Already uses heavy industry-specific tools (Studio Designer, Buildertrend, QuickBooks) and won't abandon them — wants a layer that sits on top. A studio bot triages comms; a project bot per active engagement; client-facing access with visibility boundaries. Carla activation is the most acute audience pain we've identified, and most of v3-and-beyond is shaped by closing that gap.

Personas are design constraints, not blueprints we hardcode. We don't ship "the Marcus template" or "the Carla template" as fixed packages — those would calcify. We build composable pieces (skills, applications, escalation rules, visibility boundaries) and let templates emerge once we have enough Lego blocks.

### Who Evolve isn't for

- **Enterprise platform teams.** If you have a dedicated AI ops function, look at Preloop. Evolve doesn't try to be your platform.
- **Developers who want a coding agent.** That's a different category. We share OpenClaw as substrate but not audience.
- **People who want a hosted SaaS.** Evolve runs on your hardware, on purpose. There's no cloud control plane to sign up for.
- **Anyone who needs SOC 2 / HIPAA enterprise audit logging today.** The signal store and security audits are robust for individuals and small operators; formal enterprise compliance is not a v1 promise.

---

## Architecture and principles

A few decisions shape everything else.

### Evo is a real OpenClaw bot

Evo runs on its own OS user account, with its own OpenClaw gateway, its own SOUL/AGENTS/TOOLS/MEMORY files, its own tier-routing (Sonnet for chat reasoning, Haiku for narrative heartbeats, Opus as fallback). The admin UI's chat surface is a thin HTTP proxy to evo's gateway — there's no parallel Anthropic call in admin Python, no hand-rolled system prompt, no impostor stack. Operators tune evo through standard OpenClaw mechanisms: edit SOUL.md, propose an AGENTS.md change, add a tool. The bot at the heart of Evolve is a faithful exercise of OpenClaw.

### Per-page sessions, shared memory

Each page in the admin UI has its own OpenClaw session with evo — Cost, Security, Alerts, Apps, the standalone Chat page, all separate threads. Switch pages and your conversation context switches with you. Close and reopen a page and your conversation resumes. **MEMORY.md is shared across sessions** — durable facts evo learns on one page are available to every other page from the next turn forward.

### Privacy by architecture

Every bot's LLM inference runs inside that bot, with that bot's own credentials. There's no centralized inference service inside Evolve that sees user data. Filesystem-shape skills (iMessage, Obsidian) never make a network call — your iMessage history is read on the same Mac it's stored on, by the bot that owns it, and the inference happens through the bot's own LLM provider. Cross-bot data sharing is opt-in and explicit; bots are compartmentalized by default.

This is the load-bearing claim for Diana (board-confidential financial data) and Carla (client-privileged work product). It's also why Evolve runs locally on a Mac you own, not in our cloud — because we don't have one.

### Two surfaces, two capability tiers

Direct access to evo (chat in the admin UI or a Telegram DM to the evolve bot) carries the full tool surface. Indirect access (the `evo X` keyword used from any other bot's thread) keeps the existing plugin-mediated dispatcher with role-aware filtering — and *does not* expose evo's tool catalog to a member bot's LLM. The boundary is intentional: a crafted prompt on a household bot cannot trigger evo to take an action. The two surfaces share the same underlying bot; the difference is in what's reachable from where.

### In-house OAuth substrate

After vetting Nango, ACP/GatewayStack, ContextForge, and a handful of other candidates against the licensing and self-host bar, we built the OAuth substrate ourselves. The result is a provider registry where each new SaaS integration is a single file — eight providers shipped (Calendar, Discord, Gmail, GOG, Slack, Telegram, and the filesystem-shape iMessage and Obsidian sharing the interface). Breakeven against adopting an external substrate was around five or six providers; we passed that before v2.1 closed. The substrate is small, owned, and not paywalled.

### Safety as a flagship feature

The voice we use internally is **"vigilant by default, friendly by design."** Concretely:

- Every proposed change to a bot's config or behavior travels through a signed pipeline, a security review, and a human approval gate. Evo's action tools are part of this pipeline, not a bypass of it.
- The security audit runs every fifteen minutes against eight categories of pod drift, with findings surfaced in plain language rather than jargon.
- Security alerts use a dedicated channel separate from operational ones, so a misconfigured Telegram channel doesn't silence them.
- Architectural safety claims that can't be backed by a measurement are not surfaced as guarantees.

A safety claim that isn't true is worse than no claim. The Security page leads with measured audit findings, not aspirational assertions about what a bot "can't" do.

### Substrate strategy

Evolve is OpenClaw-first today, standards-aligned for tomorrow. The ecosystem is converging on MCP for tools, agentskills.io for portable skills, and A2A for inter-agent communication. We design Evolve's abstractions around those standards so substrate optionality is preserved without paying the engineering cost of supporting three runtimes today.

The companion substrate we *did* adopt was **Opik** for observability (Apache-2.0, self-hostable, OpenTelemetry-compatible swap path). Everything else (Composio, Nango, ACP, ContextForge, ClawTrace, signal-cli) was cut after vetting against the licensing and use-case bar.

### The pod model

Every Evolve installation has three roles cleanly separated:

- **Bot users** do the work. Each runs an OpenClaw gateway, talks to channels, runs sessions.
- **The `evolve` user** runs the admin server, scheduled jobs, the analyzer, security audits — the management layer. Evo lives here too, as its own OpenClaw bot.
- **The human admin** (you) approves changes, manages keys, and is the only human in the loop.

The bots in your pod cannot influence their own management layer. Evo is a bot too — same compartmentalization rules apply.

---

## What we don't claim

Honesty about what isn't here matters as much as enthusiasm about what is.

- **We don't have a public user base yet.** Evolve runs on the author's mini and a small set of friend-of-the-project pods. The product is real; the community isn't yet. If a website tells you "trusted by thousands of households," it isn't Evolve's.
- **The evo proxy isn't fully shipped.** Phase 4 of the OC-native architecture (the production admin-UI-to-evo proxy, per-page sessions, tool-call buttons with dry-run validation) is the next big lift. The admin UI chat works today, but until Phase 4 lands some surfaces still route through the legacy stack.
- **We don't ship per-persona templates as a current feature.** Marcus, Diana, and Carla shape what we build, but you won't find a "Carla starter pack" button. Templates are a later tier of abstraction, after we have enough composable pieces.
- **We don't have Signal or WhatsApp support today.** Signal was cut after vetting (license + maintainer-risk concerns); WhatsApp is on the queue but not shipped.
- **Multi-bot handover (the Diana flow) isn't done.** The architecture is there; the end-user onboarding flow isn't yet.
- **The Carla activation wave isn't done.** Client-facing project bots with visibility boundaries and escalation rules are next on the roadmap, not current capabilities.
- **Enterprise audit logging is not in scope for v1.** The signal store and audit trail are designed for small operators, not for SOC 2 attestation.
- **We don't compete on raw capability.** OpenClaw, Hermes, and the rest of the agent ecosystem ship faster than any single packaging layer can absorb. Evolve's bet is that the product polish (the wizards, the dashboards, the conversational interface, the application framework) is the thing households and small businesses actually need.

---

## Roadmap shape

Light touch. No dates, no thirty-item lists.

The current focus is **the evo-as-OC-native architecture** — phasing out the legacy admin chat stack and replacing it with a thin proxy to evo's gateway, per-page sessions, file-backed report narrative, tool-call buttons with dry-run validation. Five phases:

1. **Tool surface** (largely landed). The catalog of read and action tools evo invokes via OpenClaw tool-use.
2. **Lifecycle engine** (landed). Evolve regenerates evo's TOOLS.md and the generated sections of AGENTS.md on every deploy; instance-owned files (SOUL, USER, MEMORY) are never overwritten.
3. **Content + heartbeat narrative** (landed). The Report banner is now a file written by evo on a heartbeat; admin chat streams via SSE so heavy turns show progress as evo works.
4. **Admin UI proxy + per-page sessions + tool-call buttons** (in progress). Server-side page packs for Security, Cost, and Alerts let evo answer inline with the same data the page shows; tool-call buttons with dry-run validation are next. Hallucinated buttons become structurally impossible.
5. **Cross-bot indirect surface audit**. Documenting and confirming the security boundary between direct evo (full tool surface) and indirect evo (role-gated subcommand registry only).

Beyond that, the next big strategic bets stay where they were:

- **The resolver pattern** — growing the generator catalog so that more operator requests have a matching proposal evo can apply end-to-end. The pattern works only when the queue has the right contents.
- **Multi-bot handover and cross-bot orchestration (the Diana wave).** First-class "add a bot" flow, Signal/Telegram handover links so the end-user onboards themselves, evo's cross-bot synthesis as a flagship demo.
- **Carla activation.** Client-facing project bots with visibility boundaries, escalation rules, and a bot-retirement flow that generates a closure package for the client.

Capability expansion (more gallery apps, more skills, vector memory for long bot histories) keeps happening in parallel.

---

## A note on voice

The voice we aim for is **thoughtful and competent, with a sense of humor** — closer to a good barista or hotel concierge than to a corporate marketing page. Tailscale, Notion, and Plex are the brand analogues we cross-check against. No exclamation-pointed enthusiasm, no mascot, no badges. Whimsy is in rhythm: a status that says "Humming along" instead of "OK", an evo reply that confesses *"I think this might be a deadline — confirm?"* rather than guessing wrong with confidence.

The audience test we run every feature against is the **Plex test**: can someone who installs Plex on their NAS and runs Home Assistant on a Raspberry Pi use this without three Stack Overflow searches? If not, the feature isn't shipped yet — it's a half-built thing.

---

*This doc reflects state through early June 2026 — the App Coherence framework, the Issues feedback loop, the Cost &amp; Caps unification, the bundled-plugin skills wave, and the cross-bot generators. The companion [README](../../README.md), [architecture.md](../architecture.md), [applications-vs-skills.md](applications-vs-skills.md), and [spec-evo-oc-native-2026-05-19.md](../spec-evo-oc-native-2026-05-19.md) align with the same model. If anything here contradicts the live code, the code wins — file an issue or open a PR.*
