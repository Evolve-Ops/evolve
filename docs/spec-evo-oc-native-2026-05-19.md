# Evo as a Supercharged OpenClaw Bot — Architecture (2026-05-19)

Status: **resolved**. Original draft landed 2026-05-19 (#1262). Amendment closing the six open questions landed via this PR. See §12 (revision history) for what changed.

**What this is.** Today the admin UI's "evo" chat is an impostor: `/api/home/chat` in the admin server makes a direct Anthropic call with a hand-rolled system prompt, bypassing the real evo OC bot entirely. The evo bot exists at `/Users/evolve/` with its own SOUL.md, AGENTS.md, etc., but the admin UI doesn't talk to it. This spec replaces the impostor stack with a thin proxy and migrates evo's behavior, knowledge, and tools to live where they belong: in evo's OpenClaw workspace, addressed through OC's session pipeline, configured via OC's standard mechanisms.

**Relationship to other specs.**
- [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md) — defines the `evo <subcommand>` keyword surface. This spec extends evo from "a dispatcher with keyword shortcuts" to "an OC bot whose direct-access surface includes a conversational session, while indirect-access (cross-bot keyword) keeps the existing plugin-dispatched path."
- [spec-evo-llm-compliance-2026-05-18.md](spec-evo-llm-compliance-2026-05-18.md) — captures the empirical reliability matrix and transport approaches for the existing plugin-mediated `evo X` keyword surface. Load-bearing for §1.3 (security boundaries) and §5.3 (cross-bot keyword routing) below.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — defines the signal store that evo reads from. This spec adds OC-tool wrappers around the store so evo can query it through tool-use.
- [POD_CONDUCT.md](system/POD_CONDUCT.md) — existing Evolve mechanism for cross-bot rules. §6 of this spec aligns evo's per-bot file-lifecycle conventions with the POD_CONDUCT injection pattern that already works.

**Memories that drive this spec.**
- `feedback_prelaunch_architect_properly` — Evolve is pre-launch; build the right architecture once, no quick-win placeholders.
- `project_evo_oc_native_architecture` — the architectural commitment this spec implements.
- `project_pod_conduct_mechanism` — existing pod-wide injection pattern; §6 below extends it.
- `feedback_per_bot_inference` — evo's LLM calls bill against evo's auth-profiles, not a centralized service.

---

## 1. Goals and non-goals

**Goals.**

1. The admin UI's chat surface is a thin proxy to evo's gateway. No system prompt in admin Python. No parallel Anthropic call. No per-route cap counter. No hand-maintained capability list.
2. Evo's behavior lives in evo's workspace files (SOUL/AGENTS/TOOLS/MEMORY/USER) and openclaw.json — the same surfaces that govern every other OC bot.
3. Evo can read pod state and take action via OC tool-use, calling tools backed by Evolve code (signal store, proposal queue, audit, deploy, etc.). The "broken-promise" pattern (offer an action, can't deliver) is structurally prevented by tying offers to real tool definitions.
4. Two access surfaces with DIFFERENT capability sets (see §1.3):
   - **Direct access** (admin UI + Telegram-to-evolve-bot) carries the full read + action tool surface.
   - **Indirect access** (member bots' existing `evo X` keyword path) keeps the legacy plugin-mediated dispatcher with role-gated subcommands; does NOT get OC tool-use access.
5. Evolve's templates for evo's workspace files are themselves managed: shipped with Evolve code, versioned, regenerated where appropriate, and updated on existing installs without clobbering operator customizations.

**Non-goals.**

1. Reimplementing OC's session pipeline, memory engine, or tool-use protocol. Evolve hosts the bot; OC runs it.
2. A new admin-UI page or chat redesign. This spec changes what's *behind* the existing Chat page, not the UI itself (#1252's drawer + Chat page stay).
3. Per-operator profiles beyond what `user_profile_inferrer` already produces. USER.md remains driven by that pipeline.
4. Multi-tenant evo (one evo bot per Evolve install). A pod has one evo; this spec doesn't change that.
5. Replacing POD_CONDUCT.md. §6 explains how the per-bot file-lifecycle conventions in this spec compose with the existing pod-wide POD_CONDUCT injection.
6. **Multiple native messaging channels on evo's gateway simultaneously.** Evo today serves Telegram + an HTTP API. The HTTP API is what the admin UI proxy uses; it isn't a messaging channel in OC's plugin sense, so no concurrency conflict with the Telegram plugin. Adding a *second* native messaging channel to evo (e.g. native Slack alongside Telegram) is out of scope and historically problematic — see §5.5.

---

### 1.3 Security boundaries — two capability tiers

This subsection was added to close Q1 from the original spec's §9. Bug we're avoiding: making evo's full tool surface reachable from any prompt-injection on any member bot in the pod.

**The problem.** Evo's tool surface (§2) includes powerful read AND action tools — query the entire signal store, redeploy bots, accept proposals, etc. The admin operator should have all of those. **A user typing on a team bot (e.g. another member of Star Springs Ranch chatting on Team-Bot-C, or a household member on Personal-Bot) should not.** If we expose `crossbot.ask` or any equivalent that lets a member bot's LLM invoke evo tools, every member bot becomes an attack surface for evo — a crafted prompt on Personal-Bot could potentially get evo to act.

**The boundary.** Two distinct surfaces, with intentionally different capability sets:

| Surface | Who reaches it | How | Capability surface |
|---|---|---|---|
| **Direct evo** | Pod operator (admin) | Admin UI chat (HTTP proxy to evo's gateway) **or** Telegram DM to the evolve bot | Full Layer 1 tool surface (§2): all reads, all actions, gated by the authority tier the operator sets |
| **Indirect evo** | Any member-bot user (primary or secondary on any team bot) | The existing `evo X` keyword path: plugin intercepts, dispatcher resolves a subcommand, response relayed | The dispatcher's subcommand registry, filtered by the subcommand's `available_to` field, the requesting user's role on the originating bot, and bot scope. NO OC tool-use exposed. |

**Why this is secure.** The indirect path is the architecture documented in [spec-evo-llm-compliance-2026-05-18.md](spec-evo-llm-compliance-2026-05-18.md) — the originating bot's LLM is *suppressed* during `evo X` turns and never sees an "evo" tool. The plugin pattern-matches the keyword, calls `/api/evo/dispatch` with explicit bot_id and sender identity, the dispatcher applies role-aware filtering, and only the resolved subcommand response comes back. Even a fully-compromised member-bot LLM can only invoke subcommands its own user is authorized for.

**`available_to` is the gate.** The subcommand registry's `available_to` field (`PRIMARY` / `SECONDARY` / `BOTH`) determines which subcommands a given user can reach via the indirect path. Operator-only subcommands (`summary`, full-pod queries) are admin-tier; per-bot helpers (`cost`, `help`, `fail`) are primary-tier; informational only (`fun`) are BOTH. Auditing this field with the security boundary in mind is a Phase 0 task (see §7).

**What this rules out.**
- No `crossbot.ask(bot_id='evolve', question='...')` tool registered on member bots.
- No path that lets a member bot's LLM resolve to evo's full tool surface.
- No "indirect tool-use forwarding" where a member bot proxies its user's tool requests to evo.

**What's still allowed.**
- A user on Team-Bot-C types `evo cost` and gets the cost summary — works today via plugin dispatch.
- A user on any bot types `evo fail <description>` and triggers a diagnostic — same path.
- Phase 5 (originally "cross-bot keyword routing") becomes "audit + document the existing indirect path"; no new mechanism.

---

## 2. Layer 1 — Evo's tool surface

Evo is a model with hands. Hands = OC tool-use bindings. This section defines the catalog.

### 2.1 Catalog organization

This catalog is the **direct surface only** (admin UI proxy + Telegram-to-evolve-bot). It is NOT available to the indirect cross-bot keyword path — see §1.3.

Tools group into five families. Each tool is a normal OC tool registered in evo's gateway config (`agents.defaults.tools` or per-channel scope; details in §7) so the model invokes them via standard tool-use, not via string parsing or backticked-command extraction.

**Read — pod state**
- `pod_state.signals.firing()` — return currently firing signals. Args: optional `bot_id`, `severity` filter, `producer` filter. Returns array of signal records.
- `pod_state.signals.history(window)` — historical signal state transitions over a window. Args: `window` (e.g. `"24h"`, `"7d"`).
- `pod_state.proposals.pending()` — pending arbiter proposals. Args: optional `bot_id`, `urgency` filter.
- `pod_state.proposals.snoozed()` — snoozed proposals waiting on a wake condition.
- `pod_state.bots()` — every bot's status (live/active/offline, version, role, port).
- `pod_state.host()` — host CPU/memory/disk/load/uptime.
- `pod_state.audit()` — most recent audit findings.
- `pod_state.content_scan(bot_id?)` — content-scan findings.

**Read — configuration**
- `config.bot(bot_id)` — read a bot's openclaw.json (summarized — same projection the admin UI's "config" command shows).
- `config.network()` — read pod-level network.json (claude-relevant fields only — no secrets).
- `config.integrations(bot_id?)` — integration status, key health, scope summaries.

**Action — bot management**
- `action.bot.redeploy(bot_id, reason)` — trigger a redeploy through the same admin endpoint the UI uses. Returns a job_id; evo can poll status via `action.job.status(job_id)`.
- `action.bot.restart(bot_id, reason)` — restart gateway.
- `action.bot.remove(bot_id, reason)` — delete a bot (requires `confirm: true` keyword).

**Action — signals & proposals**
- `action.signal.snooze(signal_id, duration, reason)`
- `action.signal.dismiss(signal_id, verdict, reason)`
- `action.proposal.accept(proposal_id)` — runs the arbiter applier.
- `action.proposal.reject(proposal_id, reason)`
- `action.proposal.snooze(proposal_id, duration, reason)`

**Action — operator-side coordination**
- `action.fail.log(description)` — same as the current `evo fail` subcommand; spawns an investigation.
- `action.app.audit(app_id, mode?)` — kick an app audit.
- `action.app.install(pkg_id, bot_id)` — install a gallery app.
- `action.continuity.defer(due_at, message_or_action)` — schedule a future-turn commitment per the POD_CONDUCT.md "honesty about application" rule.

**Cross-bot**
- `crossbot.ask(bot_id, question)` — route a question to another bot's gateway, return its reply. Backs the "evo, X" keyword path described in §5.3.

Each tool definition includes risk-tier metadata (`read | write_safe | write_risky | destructive`) so the gateway's safety machinery can enforce confirmation gates before destructive calls — same machinery any other OC bot uses, just driven by tool annotations rather than hand-coded guards in admin Python.

### 2.2 Authority tier as a tool gate

The operator's authority tier ("ask" / "auto-small" / "auto") in the Home page UI today shapes evo's *offers* via system-prompt language. Under the new architecture it shapes evo's *tool access*:

- `ask` — only read-tier tools are auto-allowed. Write tools require explicit confirmation per call (OC's ask-on-miss pattern).
- `auto-small` — read + write_safe (snooze, dismiss, log) auto-allowed; write_risky + destructive ask.
- `auto` — read + write_safe + write_risky auto-allowed; destructive always asks.

This is a standard OC exec-policy preset, parameterized by the authority field. No new mechanism — exactly the substrate the OC v2026.4.12 `exec-policy {show, preset, set}` CLI added.

### 2.3 The current `evo.dispatch` registry's relationship to tools

The existing `evolve_admin.evo.subcommands._REGISTRY` (`help`, `alerts`, `cost`, `usage`, `health`, `apps`, `app-audit`, `fail`, `gallery`, `fun`, `mute`, `unmute`, `audit`, `skills`, `connect`, `install`, `continuity`, `summary`, `security`, `bug`, `feature`, `intake`) does NOT disappear. It keeps serving:

- The Telegram surface — operators typing `evo alerts` on Telegram still dispatch through this path.
- The cross-bot keyword path (§5.3) — when a user on another bot types "evo alerts", the originating bot relays through this dispatcher.
- The admin UI's command-bar autocomplete (out of scope for this spec but anticipated).

What changes: each subcommand in the registry **also** registers as an OC tool. The tool definition is generated from the registry entry (name, short_help, long_help, args). So `evo alerts` is reachable two ways — as a typed command (Telegram, keyword) and as a tool the model calls when reasoning about a problem. Same underlying handler. One source of truth.

Stub subcommands (`wizard`, `guide`, `better`, `setup-google`, `app`, `profile`, `default`, `claim` — per #1258's filter list) are flagged in the registry as `chat_visible=False` so they don't register as tools. They keep working on Telegram (where the stub message "coming soon" is appropriate prose), but evo's tool surface doesn't surface them.

### 2.4 Where tool implementations live

Each tool is a thin adapter — Python function in `packages/admin/evolve_admin/evo/tools/` — that wraps an existing Evolve module:

```
packages/admin/evolve_admin/evo/tools/
    __init__.py             # registry: list of (name, handler, schema, risk_tier)
    pod_state_signals.py    # wraps signals.store
    pod_state_proposals.py  # wraps arbiter.store
    pod_state_bots.py       # wraps config.network_status
    pod_state_host.py       # wraps host_health.snapshot
    config_bot.py           # wraps the existing config-reader path
    action_bot_redeploy.py  # wraps the existing redeploy endpoint
    action_signal.py        # wraps signals.state_machine.transition
    action_proposal.py      # wraps arbiter applier
    action_fail.py          # wraps evo.handlers.fail
    crossbot_ask.py         # wraps a new bot-to-bot gateway client (§5.3)
```

The registry exposes a single `build_tool_manifest()` function that emits the OC tool definitions in the JSON shape evo's openclaw.json expects under `agents.defaults.tools.custom`. This is the function TOOLS.md is regenerated from (§3.4).

---

## 3. Layer 2 — Evo's context model

"What evo sees when it wakes up to a new turn."

### 3.1 Two complementary patterns

OC's bootstrap-file mechanism is one half of the context picture (workspace files injected verbatim every turn — see §3.2). The other half is **live queries via tools** for state that's too volatile to bake into the prompt. Evo uses both:

- **Bootstrap files** (slow-changing): identity, voice, action-tier discipline, the tool catalog itself, where data lives, what kinds of things to expect.
- **Live tools** (fast-changing): firing signals, pending proposals, host metrics, deploy drift. These get fetched per turn when evo asks for them.

This split solves the staleness problem we kept hitting with the hand-rolled system prompt: we were baking pod state into the prompt at session start and the state was stale within seconds. The bootstrap files now carry only the slow stuff; live state is on tap when evo needs it.

### 3.2 What the bootstrap files contain

Each file is described in detail in §4 (content) and §6 (lifecycle). Brief summary here:

- **SOUL.md** — evo's identity, voice, tone, what evo is and is not.
- **AGENTS.md** — operating rules: the action-tier discipline, the don't-embellish-titles rule, how to refer to tools (button affordance phrasing), what evo does on each kind of operator request. Includes the *generated* sections that derive from the tool registry (tool catalog, action-risk tiers).
- **TOOLS.md** — the tool catalog. **Fully auto-generated** from `build_tool_manifest()` (§2.4) every deploy.
- **USER.md** — who the operator is, their preferences, prior decisions. Driven by `user_profile_inferrer`.
- **MEMORY.md** — durable facts the operator told evo to remember (or evo inferred). Co-authored: agent writes during sessions, operator can edit, Evolve doesn't touch.
- **IDENTITY.md** — name + emoji. Seeded once.
- **HEARTBEAT.md** — heartbeat behavior. Seeded once.

### 3.3 Bootstrap budget

OC's `agents.defaults.bootstrapTotalMaxChars` defaults to 60000 (per OC docs); evo's openclaw.json today has it set to 100000. OC's per-file `bootstrapMaxChars` defaults to 12000 — files larger than that get truncated at injection time. Estimated current sizes:

- SOUL.md: 2KB (today: 1884 bytes)
- AGENTS.md: **30KB target** (today: 28KB after reliability-content additions — see below)
- TOOLS.md: 4KB target (today: 860 bytes; will grow with generated tool definitions in tabular form)
- USER.md: 1-3KB (driven by inferrer)
- MEMORY.md: variable, capped by OC at `bootstrapMaxChars` per file
- IDENTITY.md, HEARTBEAT.md: <1KB each

Aggregate: ~40KB typical, well below the 100KB ceiling.

**Per-file cap.** Evo's AGENTS.md is unavoidably large — it carries (a) operating rules, (b) hard anti-fabrication rules, (c) the per-page tool map, (d) the pod glossary (chips, signal producers, proposal generators), and (e) the legacy command reference. Each section earns its keep; trimming the glossary trades injection cost for hallucination risk on every operator question that touches those terms. We accept the cost.

Evo's openclaw.json sets `agents.defaults.bootstrapMaxChars: 40000` (set by deploy.py's `ensure_plugin_config` for primary bots) to give AGENTS.md headroom. The pod's `bootstrapTotalMaxChars` remains 100000; with the new per-file cap we still have ~60KB of room for MEMORY.md and other files.

Cost note: AGENTS.md sits in the system prompt and benefits from Anthropic's prompt caching — the per-session cache-write happens once and subsequent turns read from cache (≈10× cheaper than uncached). The cost increment from 12KB → 28KB is small in steady-state.

### 3.4 Per-turn page context — injection mechanism

The admin UI client knows which page the operator is currently looking at and what's rendered on it. Evo needs that context to answer "what should I do about this alert?" — and to make it feel like evo can SEE the page the operator sees.

**The injection mechanism.** Per [spec-evo-llm-compliance-2026-05-18.md](spec-evo-llm-compliance-2026-05-18.md), there are two viable per-turn injection points in OC:

| Hook | Reliability | Use |
|---|---|---|
| `handleSessionStart` → `systemAppend` | ✅ works (POD_CONDUCT uses this) | Per-session content |
| `before_prompt_build.appendSystemContext` | ✅ works | Per-turn directives |
| `before_model_resolve` → `systemAppend` | ❌ silently dropped (known bug) | (don't use) |

For per-turn page context we need `before_prompt_build.appendSystemContext`. **But the admin UI is an HTTP client of evo's gateway** — it doesn't have plugin-hook access. Two paths to inject:

- **Path A — proxy wraps page context into the message body.** The admin UI proxy (§5.2) builds a structured prefix on the wire and sends it as part of the user message:

  ```
  <page-context channel="admin-ui" page="alerts">
  {compact JSON snapshot of the page's state — see §3.5}
  </page-context>

  {user's actual message}
  ```

  Evo's session pipeline ingests the message as-is; the page-context block is visible to evo as a structured prefix in the user turn. Simple, no plugin required.

- **Path B — small Evolve plugin on evo's gateway.** Reads a custom HTTP header (`X-Evolve-Page-Context: {json}`) on incoming chat requests, captures per-turn, injects via `before_prompt_build.appendSystemContext`. Cleaner separation (page context never appears in the session transcript). More work — requires writing a gateway-side plugin specifically for the admin-channel.

**Phase 4 ships Path A.** Path B is the cleaner end-state; revisit only if Path A's noise (page-context blocks appearing in the transcript) becomes a problem in practice.

### 3.5 Per-page sessions, shared MEMORY.md

This subsection closes Q4 from the original §9.

The admin UI runs **per-page sessions** with evo. Each page (Chat, Dashboard, Alerts/Reports, Cost Optimization, Maintenance/Status, etc.) gets its own OC session ID, persisted in the operator's browser as:

```
localStorage["evo_session_<page_id>"] = "sess_<uuid>"
```

Switching pages opens (or resumes) that page's session. Closing and reopening a page resumes where the operator left off. Per-session memory (OC's `memory/YYYY-MM-DD.md` daily-note layer) scopes per-session — the cost-page session's daily notes don't leak into the security-page session.

**MEMORY.md is shared.** It's evo's long-term brain. If the operator tells evo "remember I prefer Sonnet over Opus" while on the cost page, MEMORY.md gets the fact; every other session (security, alerts, the dedicated Chat page) reads the same MEMORY.md and benefits. Standard OC pattern — daily notes are short-term-scoped, MEMORY.md is long-term-shared. See [docs.openclaw.ai/concepts/memory](https://docs.openclaw.ai/concepts/memory.md).

**The standalone Chat page** has its own session (`evo_session_home`), with no page_context — it's the pod-wide chat. Operator can have arbitrary topical conversations there independent of which page they're navigating.

**"Clear conversation"** wipes the active page's session (the localStorage key + OC session-state on that session_id). MEMORY.md is never touched by this action.

**Per-page page-context snapshot — Pattern 1 (verbose injection, capped).**

Each page exposes a `pageSnapshot()` builder via the existing `_EVO_CONTEXT_PACKS` registry from #1252. The proxy serializes it on each chat turn into the `<page-context>` block. Token budgets per page:

| Page | Snapshot shape | Token cap |
|---|---|---|
| Alerts / Reports | Top 20 firing signals + total count + "X more in archive" pointer | 2000 |
| Cost Optimization | Per-bot spend table (7d) + outliers | 1500 |
| Maintenance / Status | Health summary + active check failures + last-run timestamps | 1500 |
| Dashboard / Overview | Bot tile data (status + activity headline per bot) | 2000 |
| Plugins / Skills / Apps | Active counts + recent additions | 800 |
| (any page) | Always includes: `{page_id, page_label, page_url}` | 50 floor |

A `page.read(page_id, scope?)` tool (Layer 1) exists for when evo needs to drill past the cap — e.g. "show me all 71 firing signals, not just the top 20." Tool returns the full snapshot bypassing the per-turn injection cap.

Why caps: a busy alerts page can have 71 signals; serializing all of them on every turn is 5–10k tokens of prompt overhead just for the page snapshot. Top-20 + "tool to read the rest" balances "evo sees what I see" magic against per-turn cost. Caps live in `evolve_admin.evo.proxy.PAGE_SNAPSHOT_CAPS` for tuning.

### 3.6 Narrative as a heartbeat-written file

This subsection closes Q5 from the original §9.

Today's "Evo's report" banner on the admin UI is generated on demand: page loads `/api/home/narrative` → server checks 5-min cache → cache miss → LLM call → cache → return. That's expensive (an LLM call per cold operator page-load) and stale (5 minutes is enough for several alerts to fire).

The OC-native shape: evo writes the narrative to a workspace file on a heartbeat. The admin UI report banner reads the file. No on-demand LLM call.

**Mechanism.**
- Evo's heartbeat (configured in evo's openclaw.json) runs every N minutes (default proposal: 15 min, tuneable). Each heartbeat is a short OC session that reads current pod state (via the Layer 1 read tools), composes the narrative, and writes it to `/Users/evolve/.openclaw/workspace/REPORT.md` (or per OC's pattern, to `memory/YYYY-MM-DD.md` with the latest entry being authoritative).
- The admin UI's report banner fetches REPORT.md (cheap file read, no LLM) and renders. Always under one heartbeat-interval stale; instant page load.
- A "refresh now" button on the banner forces an out-of-band heartbeat trigger that recomputes the file. Same as today's manual refresh, just file-backed.
- Tier routing: heartbeat composition uses tier3 (Haiku) — narrative summarization is exactly the workload Haiku is good at. Operator-facing chat uses tier0 (Sonnet 4.6) for reasoning. Both are evo's openclaw.json tier defaults; nothing new.

**Benefits.**
- Page loads are instant.
- Narrative refresh smarter: signals-changed-recently triggers an early heartbeat; quiet pod skips heartbeats.
- Operator can read the report when offline (it's a static file the admin UI serves).
- `/api/home/narrative`, its 5-min cache, the cache invalidation, and the cap counter all delete.

**Phase placement.** This is Phase 3 work (content + heartbeat config), not Phase 4 (the proxy bridge). Reason: it doesn't depend on the admin UI proxy. Even before the proxy lands, evo can be writing REPORT.md and the existing admin UI can read it.

### 3.7 Reliability model — failure taxonomy and mitigations

Added 2026-05-19 after three independent failure modes surfaced live (Alerts/Google_Workspace, Security/team-bot-a, Dashboard/scan-needed) and a fourth shortly after on the Recommendations page. Each was a sample from a much wider distribution. This subsection makes the distribution explicit so future contributors mitigate by category, not by anecdote.

**Failure taxonomy.** Every reliability concern for an LLM-operating-a-pod falls into one of eight categories:

| # | Category | Examples |
|---|---|---|
| 1 | **Context gaps** | Page state, temporal staleness, cross-thread memory, identity/authority, recent-action audit trail, domain semantics |
| 2 | **Confabulation** | UI paths, tool/mechanism, data, capability, causality, document |
| 3 | **Wrong-action mistakes** | Misidentified target, stale-data action, cascading effect, wrong tool |
| 4 | **Pragmatic misunderstanding** | Reference resolution, wrong scope, urgency calibration, implicit-action assumption |
| 5 | **Reliability / availability** | Tool failures, race conditions, token exhaustion, deploy/version drift, cross-bot coordination |
| 6 | **Trust / safety** | Destructive auto-action, false reassurance, confidence miscalibration, recovery-loop magnification, privilege escalation suggestion |
| 7 | **Discoverability / introspection** | "What can you do?", "What did you just do?", "What will this do?", "Did it actually work?" |
| 8 | **Failure recovery** | Tool-error spiral, mid-action interruption, operator-reversal blindness |

**Mitigation toolkit.** A small set of architectural mechanisms covers most of the taxonomy. We track each as either ✅ (in place), ⏳ (partial), or ❌ (gap):

| Mechanism | Status as of 2026-05-19 | Closes |
|---|---|---|
| Per-page `<page-context>` packs | ⏳ 2 of ~10 pages | 1 (page state) |
| Fetch-on-demand tools | ✅ | 1 (most) |
| `<session-context>` block (identity, authority, time, recent actions) | ❌ | 1 (identity, recent action), 4 (reference, scope) |
| AGENTS.md anti-hallucination rules | ⏳ (UI paths, mechanisms) | 2 |
| Cite-the-tool rule in AGENTS.md | ❌ | 2 (data fabrication) |
| Domain-knowledge teaching (SOUL/AGENTS) | ❌ (chips, signals, generators semantics not taught) | 1, 4, 6 |
| Post-action verify pattern | ❌ | 3 (stale action), 5 (race), 7 (did-it-work) |
| Tool result `fetched_at` timestamps | ❌ | 3 (stale), 5 |
| Authority-tier → tool gates | ⏳ (registry has tiers; gating is Phase 4.2) | 6 |
| `meta.tools` introspection | ❌ | 2 (fabrication), 7 (discoverability) |
| Cross-session MEMORY.md | ❌ (Phase 4.3) | 1 (cross-thread) |
| `<intent>` distillation block | ❌ | 4 (drift) |
| Recovery / escalation pattern in AGENTS.md | ❌ | 8 |
| Transparency UI (tool-call trace shown to operator) | ❌ | 6 (trust), 7 (verifiability) |

**Order of work.** When picking what to ship next, prefer levers that close the most failure-mode classes per unit of work. The 2026-05-19 priority ordering:

1. `<session-context>` block (identity, authority, local time, recent-action ring) — covers categories 1, 4, partial 6, partial 8.
2. Cite-the-tool rule + AGENTS.md domain knowledge — covers 2, partial 4, partial 6.
3. Per-page packs for the remaining ~8 pages — covers 1 as a class.
4. Post-action verify pattern — covers 3, partial 5, partial 7.
5. `meta.tools` introspection tool — covers partial 2, partial 7.

After those: tool result `fetched_at` (3, 5), `<intent>` distillation (4 at scale), authority gates (6), transparency UI (7).

**Why this matters.** The three pre-2026-05-19 failure modes were each in category 1 (page-state context gap) AND category 2 (UI-path fabrication). Fixing just those two left categories 3–8 unfixed. The work above is staged so each PR closes a category cleanly rather than patching anecdotes.

### 3.8 Brittleness mitigations

The reliability levers above add taught content (AGENTS.md sections, per-page packs, glossary). Hand-curated content has its own brittleness profile — sources of drift between what's in code and what evo "knows". This subsection makes the brittleness mitigations explicit so future contributors stay on the right side of the source-of-truth-singular principle.

**Drift surfaces, with status as of 2026-05-19:**

| Surface | Brittleness without mitigation | Mitigation in place |
|---|---|---|
| Pod glossary content | HIGH — hand-curated; new chips/producers/generators silently make evo's taught semantics stale | ✅ `packages/analyzer/evolve_bot/glossary.yaml` is single source of truth; `test_evo_glossary_drift.py` cross-checks every chip in `tile_metrics.py`, every `producer="X"` / `PRODUCER = "X"` call site, every `analyzer/generators/<id>/` subdir. CI fails on any divergence. |
| Per-page packs | MEDIUM — pages renamed in HTML, snapshot writers split, packs orphaned | ✅ `test_evo_pack_coverage.py` asserts every `data-page="X"` in `index.html` has a pack OR is in an explicit `_PACK_OPT_OUT_PAGES` set; phantom pack keys also caught |
| Pack button labels | HIGH — "Take this on" gets renamed in HTML; pack still says old label; evo cites phantom button | ✅ `test_evo_button_label_drift.py` asserts every `available_actions[*].label` appears verbatim in `index.html`; descriptive non-button labels go in an explicit exemption set |
| Tool registry vs TOOLS.md | LOW — registry is the single source of truth; `build_tool_manifest()` is the canonical reader | ✅ structurally clean by design |
| Action tool `verify_via` | LOW — declared per-tool in the success response; CI tests pin each one | ✅ in place |
| OC session jsonl shape (for recent-actions ring) | LOW — central to OC, breaks are loud | None needed |

**Operator override mechanism.** The glossary is the only piece of taught content the operator might reasonably want to customize per pod (act-vs-defer judgments vary by deployment). The override surface lives at `network.json::evo_glossary_overrides`:

```json
{
  "evo_glossary_overrides": {
    "chips": {
      "cost_spike": {
        "evo_urgency_default": "act",
        "evo_advice": "Our budget is tight; treat every spike as urgent."
      }
    },
    "producers": {
      "bot_log_monitor": {
        "evo_advice": "We rotated to Sonnet — max_auth_failure is expected."
      }
    },
    "generators": { ... }
  }
}
```

The repo's `glossary.yaml` defaults stay generic. At deploy time, `install_bot_docs` regenerates the glossary with overrides applied, concatenates it onto `AGENTS.md`, and writes the result into evo's workspace. CI's drift check runs without overrides (the committed `GLOSSARY.md` is the pre-override baseline).

**Principle to apply when shipping new taught content.** Before adding any hand-curated content (a new glossary section, a new pack, a new AGENTS.md table), ask:

1. Where does the source-of-truth live? If it's hand-curated in two places, expect drift.
2. Can the content be generated from source? If so, generate; commit the generated artifact; CI-check.
3. What's the fallback when the drift detection misses something? If it's "evo hallucinates", the surface is too high-stakes for hand-curated content. If it's "evo says 'I don't know, let me check'" (cite-the-tool rule + fetch-on-demand), the brittleness is bounded.

---

## 4. Layer 3 — Evo's content (what the files say)

The bootstrap files are content, not code. This section sketches what each should contain, with full templates landed alongside the implementation. The current files on disk (audited as part of writing this spec) are partly serviceable but lean Telegram-flavored; the rewrite balances three channels (admin UI, Telegram, cross-bot) and drops anything Telegram-specific from the shared base.

### 4.1 SOUL.md (seeded; ~2KB)

Defines:
- Who evo IS — name, role (the conversational interface for the Evolve pod admin), relationship to the operator.
- Voice — terse, factual, friendly, Team-Bot-A-style (per `feedback_message_style_team-bot-a_like`).
- What evo is NOT — not a personal assistant, not autonomous (without operator input or defer()), not a risk-taker.
- Channel-aware behavior — channel hints come via systemAppend per turn, not baked in. SOUL.md describes evo's behavior in *all* channels; channel-specific formatting rules live in AGENTS.md.

The current evo SOUL.md is already close; rewrite removes Telegram references and adds the multi-channel framing.

### 4.2 AGENTS.md (hybrid — generated + seeded; ~12KB target)

Structure:
- **`<!-- evolve-agents-tools:begin -->` ... `:end`** — generated section listing every tool, its description, its risk tier, when to use it. Source: `build_tool_manifest()`. Regenerated every deploy.
- **`<!-- evolve-agents-rules:begin -->` ... `:end`** — generated section with the action-tier discipline, the don't-embellish-signal-titles rule, the no-redeploy-from-here boundary, the use-the-digest rule. Source: Python template + tier policies. Regenerated every deploy.
- **`<!-- evolve-agents-pod-conduct:begin -->` ... `:end`** — generated section listing the active POD_CONDUCT summary (or reference to it). Auto-synced with POD_CONDUCT.md. See §6.
- **`## Make It Yours`** — operator-customizable tail. Evolve never touches anything below the last `:end` marker.

The marker pattern matches POD_CONDUCT.md's existing `<!-- evolve-pod-conduct:begin -->` / `:end` convention. Existing `<!-- BEGIN EVOLVE-INSTALLED-APPS -->` / `<!-- END ... -->` markers in AGENTS.md are migrated to the same lowercase-colon syntax (`<!-- evolve-installed-apps:begin -->` / `:end`) to standardize the convention.

### 4.3 TOOLS.md (fully generated; ~4KB target)

Header: `<!-- generated by evolve.evo.tools_md_generator — do not edit; changes will be overwritten on next deploy -->`.

Body: one section per tool, with name, description, args (type-annotated), risk tier, examples. Same content as the OC tool definitions but in markdown form for the prompt. Regenerated every deploy from the same `build_tool_manifest()` that drives the OC registration.

We are breaking OC's "TOOLS.md is user-written prose" convention here, deliberately and explicitly — Evo is a managed bot, not a generic OC bot, and code drift between TOOLS.md and the actual tool surface is a bug.

### 4.4 MEMORY.md (instance-owned; shared across sessions; variable size)

Driven by:
- Operator: "remember that I prefer …" said on any session — evo writes the fact to MEMORY.md and confirms.
- Inference: `user_profile_inferrer` extracts patterns from sessions and updates relevant entries.
- Self: evo curates over time (memory flush at compaction, per OC's pattern).

**Shared across all per-page sessions** (per §3.5). A fact written on the cost page is visible to the security session next time it opens. Per-session conversation history (`memory/YYYY-MM-DD.md`) is scoped per-session; long-term curated knowledge in MEMORY.md is global to evo.

Evolve never touches MEMORY.md after the initial seed. Seed-only on bot creation (a single line: `## Initialized YYYY-MM-DD on <hostname>`). This is the file that's currently missing on both evo and Personal-Bot — fixed by the bot-creation spinoff in #1260.

### 4.5 USER.md (seeded; grown by inferrer)

Initial seed: the operator's name (from network.json setup), their bot role (sysadmin), the install hostname, the install date. After that, `user_profile_inferrer` (existing) writes facts derived from sessions. Evolve's template engine doesn't touch USER.md after the seed.

### 4.6 IDENTITY.md, HEARTBEAT.md (seeded; small)

IDENTITY: name = "evo", emoji = "✦" (matches the admin UI FAB). Seeded once on bot creation.

HEARTBEAT: short checklist matching POD_CONDUCT's "honesty about application" rule — call defer() before claiming to do anything later. Seeded once.

### 4.7 INSTALLED_APPS.md (generated; root-owned; existing)

Already managed via deploy code and the `<!-- BEGIN EVOLVE-INSTALLED-APPS -->` marker pair. Stays. Marker syntax migrates to the unified `<!-- evolve-installed-apps:begin -->` / `:end` convention as part of this spec.

### 4.8 What does NOT live in evo's workspace

The action-button extraction registry (`_known_subcommand_names`), the live signal-store reader, the cost-cap counter, the pod-state digest builder — all of these are Evolve code that backs evo's tools. They stay in `evolve_admin` Python. None of them belong in workspace files.

---

## 5. Layer 4 — Channel routing

Three surfaces, one bot.

### 5.1 The shape

```
                  ┌──────────────────────────────┐
   admin UI ────► │                              │
                  │       evo bot gateway        │ ──► Anthropic
   Telegram ────► │   (port 19030, /Users/evolve)│      (tier-routed)
                  │                              │
   other bots ──► │                              │
                  └──────────────────────────────┘
                              │
                              ▼
                       evo's session pipeline
                       (SOUL/AGENTS/TOOLS/MEMORY/USER)
                              │
                              ▼
                       OC tools (signal-store reads,
                       proposals, actions, crossbot)
                              │
                              ▼
                       Evolve code (analyzer, arbiter,
                       signals, deploy, etc.)
```

Every surface hits the same gateway. Same SOUL, same AGENTS, same TOOLS, same MEMORY, same tier routing, same authority enforcement.

### 5.2 Admin UI as a channel adapter

The admin server gets a new module: `packages/admin/evolve_admin/web/evo_proxy.py`. It's a thin proxy from admin-UI chat requests to evo's gateway.

**Request shape from the admin UI frontend** (mostly unchanged from today's `/api/home/chat`):
```
POST /api/home/chat
{
  message,           // user's text
  page_context,      // {page_id, page_label, page_url, state}
                     // state is the per-page snapshot from _EVO_CONTEXT_PACKS, capped per §3.5
  authority,         // "ask" | "auto-small" | "auto" — operator's current tier
  session_id?        // sess_<uuid> from localStorage["evo_session_<page_id>"];
                     // proxy generates + returns if missing (first turn on this page)
}
```

**Proxy behavior — outbound to evo's gateway** (Path A from §3.4):

The proxy POSTs to evo's gateway HTTP API with a message body that wraps the page context as a structured prefix on the user turn:

```
POST http://localhost:19030/api/<oc-chat-endpoint>
{
  session_id,        // from above
  message:
    "<page-context channel=\"admin-ui\" page=\"alerts\" url=\"/admin#alerts\">\n"
    + "{page_context.state as compact JSON, capped per §3.5}\n"
    + "</page-context>\n\n"
    + "{original user message}"
}
```

Authority tier is communicated to OC's exec-policy via session metadata or header (`X-Evolve-Authority: ask|auto-small|auto`) and maps to the OC exec-policy preset on the session.

**Session continuity.** Per §3.5: one session per page, persisted via `localStorage["evo_session_<page_id>"]`. The frontend sends `session_id` with every chat call; the proxy threads it through. Browser refresh, tab close + reopen, navigation away + back — all resume the same session.

**Response shape — tool-call interception (closes Q3 / button reliability).**

When evo's gateway emits a tool call in response to a user turn, the proxy intercepts it before execution and runs three validation gates:

1. **Schema gate**: tool name + arg types valid against the registered tool definition. (OC enforces this natively; the proxy just confirms the gateway didn't error.)
2. **Authority gate**: the tool's risk tier (`read | write_safe | write_risky | destructive`) vs. the operator's current authority tier. `read` always executes silently; `write_safe` executes silently in `auto-small`+; `write_risky` executes silently in `auto`; `destructive` always renders a button.
3. **Dry-run validate**: every action tool exposes a `validate()` method that confirms the action is currently possible (target bot exists, signal still firing, proposal still pending, etc.). If validate fails, the button doesn't render; evo's session sees the validate failure inline and incorporates it into its reply ("I tried to snooze that signal but it was already resolved 2 minutes ago").

When all three gates pass and the tool is `read`-tier OR the authority allows silent execution: tool fires, result returns to evo's session, evo composes its reply with the data. When a button is needed: the proxy returns the reply envelope with a `pending_actions: [{tool, args, label, risk_tier}, …]` array that the frontend renders as one-click buttons. Click → POST `/api/home/chat/confirm-action` → proxy releases the tool call → result flows back through evo's session → evo's next turn incorporates the result.

**Net guarantee: hallucinated buttons are structurally impossible.** The model can only request tool calls that exist in evo's tool registry (registered at session start). Each tool the model requests passes the three gates before any button renders. The user never sees a button that would fail.

**Response envelope to the admin UI frontend:**
```
{
  reply,               // the prose evo generated
  session_id,          // echoed (frontend caches in localStorage)
  pending_actions: [   // optional — only present when a write/destructive tool needs confirmation
    { tool, args, label, risk_tier }
  ],
  source: "evo",       // always "evo" (no more "dispatch" / "llm" / "cache" distinction)
  model,               // evo's tier resolution for this turn (Sonnet 4.6 typical)
  cost_usd, input_tokens, output_tokens   // from evo's OC session metrics
}
```

**What gets deleted.**

The entire impostor stack:
- `packages/admin/evolve_admin/web/home_chat.py` — direct Anthropic call, system prompt template, cap counter, narrative generator
- `packages/admin/evolve_admin/web/home_chat_routes.py` — `/api/home/chat` and `/api/home/narrative` routes
- `_known_subcommand_names`, `_looks_like_command`, `_prepend_page_context`, `extract_suggested_actions`, the catalog filter — all replaced by OC tool-use
- `home-chat-usage.json` cap counter — OC tracks per-bot usage in its own metrics pipeline; we read from there for the operator-facing spend display
- The narrative cache + `/api/home/narrative` route — replaced by REPORT.md file read (§3.6)

The proxy itself (`evo_proxy.py`) is estimated at ~150 lines (Path A wrapper, tool-call interception logic, button extraction, session_id management). Substantially smaller than what it replaces.

### 5.3 Cross-bot "evo" keyword — keep the existing plugin-mediated path

This subsection resolves Q1 from the original §9, in light of [spec-evo-llm-compliance-2026-05-18.md](spec-evo-llm-compliance-2026-05-18.md).

**Decision: no architectural change.** The existing plugin-mediated `evo X` keyword path on member bots stays. It is the *correct* security boundary for indirect access (§1.3). Specifically:

- The plugin (`packages/plugin/src/observer/TurnObserver.ts`) intercepts `evo X` before the originating bot's LLM resolves, calls `/api/evo/dispatch` on the admin server with explicit bot_id + sender_external_id, suppresses the originating LLM via stay-silent or LLM-echo-verbatim (per the two approaches documented in the compliance spec), and surfaces the dispatcher's response.
- The originating bot's LLM **never gets `crossbot.ask` or any equivalent tool**. There is no "ask evo arbitrary questions and get OC tool-use back" capability via member bots.
- The subcommand registry's `available_to` field (`PRIMARY` / `SECONDARY` / `BOTH`) gates which subcommands the indirect path can reach. The dispatcher's role-aware filter (`dispatch.role_can_run`) enforces this at every call.

**Phase 5 of §7 becomes:** audit the `available_to` field across the subcommand registry with the security boundary in mind. Document the indirect-path security model in the dispatcher's docstring. Add a smoke test that verifies a secondary user on a team bot cannot reach admin-only subcommands. No new mechanism.

**What this rules out (architectural commitments).**
- No `crossbot.ask` tool on member bots.
- No path that exposes OC tool-use from evo to any indirect caller.
- No "team bot forwards arbitrary user prompts to evo for handling."

**What's preserved.**
- The current plugin-mediated `evo cost`, `evo help`, `evo fail <description>`, etc. on member bots — works today, continues working.
- The compliance-spec reliability matrix (§5 of that spec) is the source of truth for which transport approach lands which subcommands on which channels.
- The agenda-phase blind spot from §4 of the compliance spec stays open as a follow-on; this spec doesn't address it (it's about indirect-path wizard reliability, not architecture).

### 5.4 Telegram (direct surface)

Existing path. Works unchanged. The evolve bot's Telegram DM channel is direct-access — same capability surface as the admin UI proxy (full Layer 1 tool surface), because the operator is the one talking to evo through Telegram. Other bots' Telegram channels are indirect-access (subject to §5.3's plugin-mediated dispatcher).

### 5.5 One messaging channel per gateway — known OC constraint

This subsection captures pod-admin's flag and clarifies its scope.

**The historical issue.** Pod-Admin's early Evolve experimentation hit a reliability bug when trying to attach a single OpenClaw bot to both Telegram and Slack concurrently — OC's gateway appeared to "listen" on one channel and become unresponsive on the other, even when one used the native plugin and the other a custom webhook. Whether this is a current-OC bug or was specific to older versions is undetermined; the empirical conclusion was "one native messaging channel per bot at a time."

**How this spec relates.** Evo today attaches to one native messaging channel: Telegram. The new architecture adds:

- The **admin UI proxy** (§5.2) — this hits evo's HTTP API, NOT a messaging channel. The admin UI is a programmatic client; it doesn't compete with the Telegram plugin for inbound-message handling. The OC docs separate the gateway's HTTP-API path from messaging-channel plugins; they run on different layers.
- The **indirect cross-bot path** (§5.3) — this doesn't touch evo's gateway at all. Member bots' plugins call `/api/evo/dispatch` on the admin server, which talks to the dispatcher (Python, in-process), not to evo's gateway. Evo's session pipeline is uninvolved.

**Net effect on evo's gateway:** one messaging plugin (Telegram) + the HTTP API (used by admin UI proxy). No multi-messaging-channel concurrency. We stay within the operating range Pod-Admin's earlier experimentation validated.

**If a future requirement adds a second native messaging channel to evo** (e.g. evo on both Telegram + Slack for direct admin access from two different transports), that hits the OC constraint and needs upstream verification before committing. Out of scope here. As of OC 2026.4.29, the doc and release notes are silent on whether the original Telegram+Slack concurrency issue has been resolved.

---

## 6. Layer 5 — File-lifecycle management

The most novel part of this spec, and the part that mediates between OC's "workspace files are user-owned" convention and Evolve's need to ship updates to those files.

### 6.1 Three policies

| Policy | Files | Behavior |
|---|---|---|
| **Generated** | TOOLS.md (fully); AGENTS.md (marker-bounded sections); INSTALLED_APPS.md (existing) | Regenerated every deploy. Operator edits inside generated sections are overwritten; outside-marker content is preserved. |
| **Seeded** | SOUL.md, USER.md (initial), IDENTITY.md, HEARTBEAT.md, MEMORY.md (initial) | Evolve writes once at install if absent. Never touched on upgrade. |
| **Instance-owned** | MEMORY.md (after seed), USER.md (after inferrer takes over), AGENTS.md "Make It Yours" tail | Evolve never touches. |

For files in multiple buckets (e.g. AGENTS.md has both generated sections AND a "Make It Yours" tail), the marker convention decides per-section: inside markers = Generated, outside = Instance-owned.

### 6.2 Marker convention

Unified across all Evolve-managed sections in OC workspace files:

```markdown
<!-- evolve-{section-name}:begin -->
... content Evolve owns and regenerates ...
<!-- evolve-{section-name}:end -->
```

Existing usages migrate:
- `<!-- evolve-pod-conduct:begin -->` / `:end` — already in this format; no change.
- `<!-- BEGIN EVOLVE-INSTALLED-APPS -->` / `<!-- END ... -->` — rename to `<!-- evolve-installed-apps:begin -->` / `:end`. Backward-compat: deploy code reads both styles for one release, writes only the new style.

The content-scan allow-list (per the spinoff task already filed) accepts the regex `^evolve-[a-z][a-z0-9-]*:(begin|end)$` inside HTML comment bodies — covers all current and future marker uses.

### 6.3 Manifest

`/Users/Shared/evolve/evo/template-state.json`:

```json
{
  "schema_version": 1,
  "evolve_version": "2026.0519.1530",
  "template_version": "v3",
  "files": {
    "SOUL.md": {
      "policy": "seeded",
      "seeded_at": "2026-05-19T15:30:00Z",
      "seeded_template_hash": "sha256:abc123…",
      "live_hash_at_seed": "sha256:abc123…"
    },
    "AGENTS.md": {
      "policy": "hybrid",
      "generated_sections": [
        "evolve-agents-tools",
        "evolve-agents-rules",
        "evolve-agents-pod-conduct",
        "evolve-installed-apps"
      ],
      "last_regenerated_at": "2026-05-19T15:30:00Z",
      "last_regenerated_evolve_version": "2026.0519.1530"
    },
    "TOOLS.md": {
      "policy": "generated",
      "generator": "evolve_admin.evo.tools_md_generator",
      "last_regenerated_at": "2026-05-19T15:30:00Z",
      "last_regenerated_evolve_version": "2026.0519.1530"
    },
    "MEMORY.md": {
      "policy": "instance-owned",
      "seeded_at": "2026-05-19T15:30:00Z"
    }
  }
}
```

Lives in `/Users/Shared/evolve/`, NOT in evo's workspace — keeps workspace files clean (OC-compatible) and centralizes Evolve's state.

### 6.4 Upgrade engine

On every Evolve deploy (running as part of `evolve-admin deploy evolve`), the upgrade engine:

1. Reads the manifest.
2. For each **Generated** file: regenerate via the registered generator. Write to a temp path, atomic-rename onto the live path. Update manifest's `last_regenerated_*` fields.
3. For each **Hybrid** file (AGENTS.md): for each generated section, locate the markers, replace content between them. Preserve content outside any marker block. Update manifest.
4. For each **Seeded** file: check if it exists. If absent → write the template, update manifest with `seeded_at` + `seeded_template_hash`. If present → no-op (OC's "non-destructive" rule).
5. For each **Instance-owned** file: never touch.
6. If template structure changes (new generated sections added to AGENTS.md, new generator for a file, etc.) → run migrations between manifest's `template_version` and current. See §6.5.
7. Update manifest's `evolve_version` and `template_version`.

The whole engine runs in <500ms (file I/O bound, no LLM). It's idempotent — running it twice in a row is a no-op on the second run.

### 6.5 Migrations

When a new Evolve release changes template structure (not just content), a migration runs once between the old and new `template_version`. Migrations live in:

```
packages/admin/evolve_admin/evo/template_migrations/
    v1_to_v2.py    # added the evolve-agents-tools section to AGENTS.md
    v2_to_v3.py    # split evolve-agents-rules out of evolve-agents-tools
    ...
```

Each migration is a function that takes the workspace path, applies the structural change idempotently, and updates the manifest. Migrations run in order from manifest's recorded version up to the current shipped version. Standard prior art: Django migrations, Flyway, Helm chart upgrades. Migrations are STRUCTURAL only — content updates are handled by the per-deploy regeneration in §6.4.

### 6.6 Conflict resolution via Proposals

When does Evolve need operator input?

- Seeded files: never — Evolve doesn't update them.
- Generated files (full file): never — operator edits inside are overwritten by design (header says so).
- Hybrid files: operator may edit the "Make It Yours" tail; those edits are preserved automatically; no conflict possible.
- Schema changes (a generated section is removed in a new template version, but the operator's customizations referenced it): rare; handled per-migration.

**There is no diff-and-prompt flow.** OC's "non-destructive on upgrade" discipline + Evolve's "generated content goes in markers" approach + "instance-owned files are off-limits" means there's no design space for conflicts. This is the major payoff: simpler than dpkg conffiles because the structure prevents conflicts rather than negotiating them.

### 6.7 How this aligns with POD_CONDUCT.md

POD_CONDUCT.md is **pod-wide**: rules every bot in the pod follows. The file:
- Lives in each bot's workspace at `/Users/<bot>/.openclaw/workspace/POD_CONDUCT.md` (copied by deploy.py).
- Has a marker block `<!-- evolve-pod-conduct:begin -->` / `:end` containing the summary that actually gets injected.
- The full file is reference material; only the summary is injected per session via `session_surface.py` → TurnObserver → systemAppend.
- `heal.py` ensures each bot's AGENTS.md has a `## Pod Conduct` section pointing to POD_CONDUCT.md.

This spec extends the same pattern from pod-wide rules to per-bot evo specifics:
- POD_CONDUCT.md remains pod-wide, untouched, doing its job.
- Evo's AGENTS.md gets a generated `<!-- evolve-agents-pod-conduct:begin -->` / `:end` section that mirrors the POD_CONDUCT summary (regenerated when POD_CONDUCT.md changes). This means evo's bootstrap context includes the POD_CONDUCT rules without relying on the runtime systemAppend (which runs per session_start) — useful because evo's tool definitions need to be coherent with the conduct rules.
- All marker conventions, the regeneration mechanism, the `heal.py`-ensures-section pattern → the same playbook. POD_CONDUCT is the reference implementation; this spec generalizes it.

Concretely: if POD_CONDUCT.md changes, `heal.py` (or its successor in this spec) regenerates the `<!-- evolve-agents-pod-conduct:begin -->` section in every bot's AGENTS.md, including evo's. Same path, same marker convention, no new mechanism.

**Should POD_CONDUCT.md's mechanism be folded into the manifest?** Probably yes, eventually — POD_CONDUCT becomes one more entry in `/Users/Shared/evolve/evo/template-state.json` (and analogous per-bot manifests) with `policy: "pod-wide-injected"`. But that's a consolidation pass, not a precondition for this spec to ship. v1 of this spec leaves POD_CONDUCT.md's existing pipeline alone and just aligns marker conventions.

---

## 7. Build sequence

Phases run in order. Each phase is independently shippable.

### Phase 0 — Prereqs (spinoffs, shipped)
- Bot-creation bugs fixed (#1260, shipped 2026-05-19).
- Content-scan allow-list for `<!-- evolve-X:(begin|end) -->` marker pattern (#1261, shipped 2026-05-19).

### Phase 1 — Tool surface (§2)
- Build `evolve_admin/evo/tools/` adapters wrapping existing read/action code.
- Build `build_tool_manifest()` generator.
- Register tools in evo's openclaw.json under `agents.defaults.tools.custom`.
- Add `validate()` method on every action tool (per §5.2 — required for the dry-run button gate).
- Annotate every tool with risk tier (`read | write_safe | write_risky | destructive`).
- Unit-test each tool standalone.
- **Deliverable**: evo can call `pod_state.signals.firing()` and friends via OC tool-use on Telegram, returning correct data. No admin-UI changes yet.

### Phase 2 — Lifecycle engine (§6)
- Implement the manifest reader/writer at `/Users/Shared/evolve/evo/template-state.json`.
- Implement the upgrade engine.
- Add the generators (TOOLS.md, the AGENTS.md generated sections).
- Implement marker-aware section replacement.
- Hook into `deploy.py` so deploys run the engine.
- Test against both fresh-install and update paths.
- **Deliverable**: deploying Evolve regenerates evo's TOOLS.md + AGENTS.md generated sections; seeded files land on fresh installs without overwriting existing ones.

### Phase 3 — Content + narrative-as-file (§4, §3.6)
- Write the templates: SOUL.md, AGENTS.md (with generated section placeholders), IDENTITY.md, HEARTBEAT.md, USER.md initial seed.
- Configure evo's heartbeat to write REPORT.md every N minutes (default 15).
- Update the admin UI report banner to read REPORT.md instead of calling `/api/home/narrative`.
- Validate prompt token budget under load.
- **Deliverable**: evo's workspace files reflect the new architecture. Telegram chat with evo demonstrates correct behavior (uses tools, respects boundaries, references real subcommand catalog). Report banner is file-backed (instant page loads, narrative refresh on heartbeat).

### Phase 4 — Admin UI proxy + per-page sessions + tool-call buttons (§5.2, §3.5)
- Build `evo_proxy.py` with Path A wire format (page context as structured prefix in the user message).
- Implement per-page `session_id` plumbing — frontend reads `localStorage["evo_session_<page_id>"]`, sends with each chat call; proxy threads through to evo's gateway.
- Implement tool-call interception — schema gate (OC-native), authority gate, dry-run validate. Return `pending_actions[]` to frontend for write/destructive tools needing operator confirmation.
- Add the `/api/home/chat/confirm-action` route for one-click button confirmations.
- Delete `home_chat.py`, `home_chat_routes.py`, the system-prompt template, the cap counter, `_known_subcommand_names`, `_looks_like_command`, `_prepend_page_context`, `extract_suggested_actions`, `home-chat-usage.json`.
- Migrate frontend chat rendering: continues to render bubbles; renders `pending_actions[]` as one-click buttons (replaces the old backticked-`evo X` extraction).
- **Deliverable**: admin UI chat is identical to Telegram chat in behavior (modulo per-page page-context injection that's admin-UI-specific), both routing through evo's gateway. Hallucinated buttons are structurally impossible. Per-page sessions persist across browser refresh.

### Phase 5 — Cross-bot indirect surface audit (§5.3)
- Audit `available_to` field across every subcommand in the registry. Confirm admin-only commands (`summary`, `intake`) are gated to PRIMARY only, etc.
- Add smoke tests verifying a secondary user on a team bot cannot reach admin-tier subcommands.
- Document the indirect-path security model in the dispatcher's docstring + the compliance spec.
- **Deliverable**: a confirmed security boundary between direct and indirect evo. No new code routing capabilities. NO `crossbot.ask` tool added to member bots.

### Phase 6 — Consolidation
- Migrate INSTALLED_APPS marker to unified syntax (`<!-- evolve-installed-apps:begin -->` / `:end`).
- Consider folding POD_CONDUCT.md into the manifest.
- Audit other Telegram-flavored prose in evo's content for multi-channel correctness.

### Phase 1.4 — The resolver pattern (§13)

Inserted into the build sequence between Phase 1.3 (write_safe action tools, shipped) and Phase 1.5 (destructive lifecycle tools, future). Implements §13's tool catalog: `action.proposal.apply` (the linchpin), `action.proposal.reject`, `action.bot.restart`, `action.bot.redeploy`, `action.app.install`. Plus the AGENTS.md "resolve in chat" teaching + the first fresh generator (`cron_caps_filler`) demonstrating how the catalog grows. See §13.8 for the ordered build steps.

**Deliverable**: when the operator describes a problem in chat, evo finds (or stages) the matching proposal and applies it end-to-end without routing the operator to the Recommendations page. The Recommendations page becomes an audit / triage view, not the primary workflow.

---

## 8. Out of scope / future

- **Multi-pod evo synchronization.** Each Evolve install has its own evo with its own MEMORY/USER. No cross-pod sync.
- **Operator-authored evo skills.** OC's skills directory could host operator-written skills for evo, but this spec doesn't define a curation/install flow for them.
- **Voice / audio / multimodal**.
- **Evo as a generator-portfolio member.** The RSI architecture has generators that propose pod changes; this spec doesn't make evo one of them.
- **Replacing OC's session pipeline.** We use what OC provides. Where OC doesn't fit, we layer on top; we don't reimplement OC concepts.
- **Cross-thread awareness in the admin UI.** Per §3.5, per-page sessions are isolated by design. If a future need surfaces a "show me what I asked evo on the security page" from the cost page, that's a UI feature on top of the per-session model, not an architectural change.
- **Second native messaging channel on evo's gateway.** Per §5.5, evo today serves Telegram + HTTP API only. Adding e.g. native Slack to evo's gateway hits a historical OC constraint and would need upstream verification first.

---

## 9. Resolved questions

The original spec had six open questions in §9. All resolved during the 2026-05-19 sync; resolutions are now in the spec body (§1.3, §3.4, §3.5, §3.6, §5.2, §5.3). Original questions and their resolutions:

| # | Question | Resolution | Section |
|---|---|---|---|
| Q1 | Cross-bot keyword routing — Choice A (handler in originating bot) vs Choice B (gateway interceptor)? | NEITHER. Existing plugin-mediated path stays. Architectural security boundary (§1.3) makes `crossbot.ask` and equivalents the wrong shape. | §1.3, §5.3 |
| Q2 | Per-turn `systemAppend` for admin-UI page context — right mechanism? | `before_prompt_build.appendSystemContext` is the right hook, but admin UI is an HTTP client without plugin access. Use Path A (wrap page context as structured prefix in user message body). | §3.4, §5.2 |
| Q3 | Suggested-action buttons — lose them or rebuild? | Rebuild via tool-call interception with three validation gates (schema, authority, dry-run validate). Hallucinated buttons structurally impossible. | §5.2 |
| Q4 | Session continuity — one operator-wide session or per-page? | PER-PAGE. Each page has its own OC session_id; MEMORY.md shared across all sessions. | §3.5 |
| Q5 | Tier routing for narrative vs chat — per-call hint or restructure? | RESTRUCTURE. Narrative becomes a heartbeat-written REPORT.md; admin UI banner reads file. Chat uses tier0 (Sonnet 4.6). No on-demand narrative LLM call. | §3.6 |
| Q6 | OC version baseline | Confirmed 2026.4.29 sufficient for all phases. exec-policy presets shipped 2026.4.12. | (verified on mini) |

---

## 10. Risk and rollback

- **Phases 1-3 are zero-risk** for the admin UI: changes only affect evo's gateway behavior and workspace files. Telegram path is exercised continuously; regressions surface immediately. Admin UI keeps its impostor stack until Phase 4.
- **Phase 4 is the bridge.** Rollback path: revert the proxy module, restore the deleted `home_chat.py` from the prior PR. Quick.
- **Phase 5 is audit + documentation.** Effectively zero functional risk — no new code routing capabilities.
- **Phase 6 is cosmetic** — marker rename, manifest folding. No functional risk.

---

## 11. Acceptance

This spec is "done" when:

- Admin UI chat with evo and Telegram chat with evo produce equivalent responses to equivalent prompts (modulo per-page context injection on the admin UI side).
- Asking evo "fix Personal-Bot" produces a response that names the operator-side action (redeploy from Dashboard → Personal-Bot → Redeploy), offers tool-call-based subcommand options (`evo fail`, `evo app-audit`), and never hallucinates a tool that doesn't exist.
- Editing evo's SOUL.md on disk changes evo's voice in the next session (Telegram or admin UI), no redeploy needed.
- `evolve-admin deploy evolve` regenerates TOOLS.md and the generated sections of AGENTS.md, leaving operator-owned content untouched.
- A fresh Evolve install creates an evo bot whose workspace passes content-scan, permission_monitor, and plugin_monitor on first observation (the Personal-Bot-shaped problem is gone — verified by #1260 + #1261).
- Asking evo something on the Alerts page produces an answer grounded in the actual firing signals visible on that page; switching to the Security page opens a separate session with the security page's state in context.
- Buttons offered under evo's replies invoke real tools that pass schema + authority + dry-run validation. No "Run evo wizard" type hallucinations possible.
- The admin UI report banner reads from `/Users/evolve/.openclaw/workspace/REPORT.md` and never triggers an LLM call on page load.
- A user on a member bot (Personal-Bot, Team-Bot-C, Team-Bot-A, etc.) typing `evo X` continues to work via the existing plugin-mediated path, and CANNOT reach evo's tool surface or any admin-tier subcommand.
- `home_chat.py`, `home_chat_routes.py`, the system-prompt template, `extract_suggested_actions`, the cap counter, and the narrative-generation route are all deleted.

---

## 12. Revision history

| Date | Change | PR |
|---|---|---|
| 2026-05-19 | Initial draft. 6 open questions in §9. | #1262 |
| 2026-05-19 | Amendment closing all six open questions. New §1.3 (security boundaries). New §3.5 (per-page sessions). New §3.6 (narrative-as-file). Rewritten §3.4 (Path A injection). Rewritten §5.2 (tool-call interception, button gates). Rewritten §5.3 (indirect path stays plugin-mediated). New §5.5 (multi-channel concurrency note). §7 phases updated. §8 expanded. §9 retitled "Resolved questions" with a resolution table. Status: draft → resolved. | (this PR) |
| 2026-05-19 | New §13 (the resolver pattern). Documents the architectural shift from "evo routes operator to the proposal queue" to "evo resolves proposals end-to-end in chat." Phase 1.4 now defined as the work that makes this real. | (this PR) |
| 2026-05-19 | New §14 (extending evo locally). Three-level extension hierarchy: one-off Proposals (§13.4 Q4), local Skills riding on OC's native 6-source skill loader, opt-in tool-gap telemetry. Codifies the "no local Python tool code" boundary and the merge/redundancy story for pod customizations vs upstream tool shipments. Phase 1.5b. | (this PR) |
| 2026-05-19 | Phase 1.4–1.5 builds shipped. §13.8 steps 1–6 (resolver pattern) all merged: `action.proposal.apply` (#1308), `action.proposal.reject` + `action.bot.{restart,redeploy}` + `action.app.install` + `pod_state.forge_job` (#1311), AGENTS.md teaching (#1310), `cron_caps_filler` generator (#1312). §14.2 first slices: Evolve-shipped skills loader hookup + first skill `investigate-firing-signal` (#1318), retirement detector (#1320). §14.3: tool-gap storage + tools + opt-out config (#1317), wipe-telemetry CLI (#1321). Plus Phase 1.5a destructive lifecycle (#1313), 1.5d minor reads (#1314), 1.5e signal/proposal lifecycle (#1315), and second resolver-pattern generator `auth_drift_filler` (#1316). Registry now at 31 tools. Open: upload daemon, list/retire-local-skill CLIs, Phase 1.5c new action_kinds. See `docs/admin-coverage-backlog.md` for the running picture. | (multi-PR) |

---

## 13. The resolver pattern — evo as proposal-resolver, not proposal-router

Added 2026-05-19 after a live operator session surfaced the gap. The reliability framework in §3.7 closed evo's CONTEXT gaps; the resolver pattern closes the AGENCY gap. The operator's words: *"In the Better better engine, evo is acting and resolving proposals — based on the user discussions and prompts."*

### 13.1 The architectural shift

**Old Better Engine.** Generators detect drift → emit proposals → proposals queue in `/proposals/pending/` → operator navigates to the Recommendations page → reads each proposal → clicks **Take this on** → applier runs → state updates. The proposal queue is the WORKFLOW; evo's chat is read-only commentary about that workflow.

**BETTER better Engine.** Generators still detect drift → still emit proposals → BUT evo resolves them on the operator's word from chat. Operator describes a problem; evo finds (or stages) the matching proposal; evo applies it end-to-end; evo verifies; evo reports done — all in conversation. The proposal queue becomes **inventory** (the system's memory of "things that could be fixed"); evo's chat becomes the **destination** for actually fixing them.

**What changes:**

- The Recommendations page is no longer the primary workflow. It's an audit / debugging view — every proposal is visible there, but resolution happens in chat. Operators who prefer the click-through flow still have it; operators who'd rather just say *"fix the cron caps"* get the same effect with less friction.
- Evo's tool surface adds `action.proposal.apply` as the LINCHPIN. Not a status flip — a synchronous end-to-end runner of the existing applier infrastructure (`arbiter/appliers/`, `safe_write_bot_config`, gateway restart, post-action verify).
- AGENTS.md gets a hard rule: when the operator describes a problem, *check the proposal queue first*, then offer to apply. Don't route them away from chat.

**What stays:**

- The proposal queue itself, the proposal schema, the applier infrastructure, the audit log. The pipeline is correct; we're adding evo as a new client of it.
- The Recommendations page UI (still useful for triage, for proposals operators want to review carefully, for the audit history).
- Generator-authored proposals as the primary source. Generators are deterministic Python, scheduled, cheap. LLM-authored proposals are the escape hatch (§13.4 Q4), not the default.

### 13.2 The behavioral arc

When the operator describes a problem in chat, evo's reasoning chain becomes:

1. **Look for a matching proposal.** Call `pod_state.proposals.pending` (or `.snoozed`) filtered by the affected bot / dimension / generator. Cite what's there.
2. **If a proposal exists** — describe it succinctly (one or two lines), cite the proposed change, and offer to apply. Don't ask the operator to navigate.
3. **If no proposal but a direct action covers it** — name the action, offer to call it. Example: *"snooze all team-bot-a alerts until tomorrow"* maps to a loop over `action.signal.snooze`; no proposal needed.
4. **If neither** — recognize the gap. Either:
   - a) Identify which generator class *should* handle this (eg "cron jobs missing `caps`" → `cron_caps_filler`) and tell the operator the generator doesn't exist yet, OR
   - b) Stage a one-off proposal authored by evo from the chat (§13.4 Q4) for operator approval. The applier handles it; evo doesn't bypass the proposal infrastructure.
5. **Apply on confirm.** Under `ask` authority, evo describes and waits. Under `auto-small`, write_safe applies auto-run. Under `auto`, write_risky applies auto-run. Destructive always asks regardless. (§13.4 Q2)
6. **Verify.** Every applied proposal has a `verify_via` equivalent — read the post-state and confirm. (§13.4 Q3 covers partial-failure handling.)
7. **Report.** Tell the operator what was done, cite the proposal id + the verify result. End of arc.

This is the same pattern reliability levers 1–5 enabled: cite tools, verify writes, respect authority. Resolver is what it looks like end-to-end.

### 13.3 Tool catalog this requires

Phase 1.4 of the build sequence becomes the work that delivers the resolver pattern. The tools:

| Tool | Tier | What it does |
|---|---|---|
| `action.proposal.apply(proposal_id)` | write_risky | Synchronously runs the full applier chain for one proposal: validate → write config (or whatever the action kind dictates) → restart gateway (if applicable) → verify. Returns `{ok, applied_changes, restart_result, verify_result}`. Honors authority tier per §5.2. |
| `action.proposal.reject(proposal_id, verdict?)` | destructive | Terminal close. Moves the proposal to `archived/` with rejection status + optional verdict for producer feedback. Destructive because it forecloses the suggestion (no auto-reopen via observe). Always asks regardless of authority tier. |
| `action.bot.restart(bot_id)` | write_risky | `launchctl kickstart -k` on the bot's gateway. Quick, reversible (just a restart). |
| `action.bot.redeploy(bot_id)` | write_risky | Runs `deploy_bot(bot_id)` end-to-end. Heavier — pulls config, restarts daemons, refreshes plugin install. |
| `action.app.install(bot_id, gallery_app)` | write_risky | Install a gallery app onto a bot. Synchronous; long-running (~30s). |
| `action.bot.remove(bot_id)` | destructive | Deferred to a future PR (Phase 1.5). |

All five carry the `verify_via` contract from spec §3.7 lever #4. Each has its own validate() per spec §5.2.

### 13.4 Design choices, resolved

**Q1. Synchronous or async apply?** Synchronous. The whole point is end-to-end resolution within the conversation turn; an async apply that returns immediately and surfaces the result on the next operator message breaks the "just fix it" UX. The cost is response latency (5–30s on apply chains that include a gateway restart). Worth it.

**Q2. Authority tier semantics for `action.proposal.apply`?**

- `ask` — evo describes the apply, names the proposal id + proposed change, waits for explicit confirmation.
- `auto-small` — write_safe proposals (eg snooze) auto-apply. Write_risky proposals (eg ConfigPatch, InstallMcpServer) still ask.
- `auto` — write_risky proposals auto-apply. Destructive proposals (RejectProposal, RemoveBot, etc.) still ask.

**Tier override for policy-weighted classes.** Some proposal action kinds carry judgment that warrants operator review even under `auto`: `SoulEdit`, `ThrottleGenerator`, `PauseGenerator`, `UpdatePermissionBaseline`. These force-`ask` regardless of the operator's authority setting. The proposal's `action.kind` carries an `evo_apply_tier_override` field declaring this.

**Q3. Partial-fail recovery.** The applier chain has multiple steps (validate, write config, restart, verify). If step N succeeds and step N+1 fails, the system is half-applied. The existing applier infrastructure handles this via `apply-results/` records and the rollback pattern. `action.proposal.apply` surfaces the partial-failure honestly:

```
{
  "ok": false,
  "applied_changes": {"openclaw.json": "..."},
  "restart_result": {"error": "gateway port collision"},
  "rolled_back": true,
  "rollback_result": {"openclaw.json": "restored from .bak"},
  "verify_result": null
}
```

Evo's reply prose mirrors the structure: *"Applied the config edit but the gateway restart failed (port 19030 in use). I've reverted the config from .bak. Want me to retry or look at what's holding the port?"*

**Q4. Ad-hoc operator requests with no matching proposal or direct tool.** Two paths:

  a. **Direct action available.** The operator's request maps cleanly to one or more `action.*` tools. Just call them. No proposal needed. Example: *"snooze every alert until tomorrow"* → loop `action.signal.snooze`.
  
  b. **Neither proposal nor direct tool covers it.** Evo's right answer is: *"There's no `action.bot.set_model` tool yet and no generator targets model-class changes. I can stage a ConfigPatch proposal for the operator queue with the model change you described — would you like that?"* The model AUTHORS a one-off Proposal object (using the existing Proposal schema), writes it to `proposals/pending/`, and applies it via `action.proposal.apply` on the next turn. Operator sees the proposal id + diff before any change lands.

   This honors `feedback_rsi_low_cost_preference` (deterministic Python generators handle the background-monitoring case) AND `feedback_per_bot_inference` (the LLM authoring is a chat-time decision, not a centralized background service). Evo authors proposals only in response to explicit operator request; the proposal flow then handles the rest. This is the only path where LLM-authored proposals enter the system.

### 13.5 Generator catalog growth

The resolver pattern works only when the proposal queue has the right contents. Every "evo identifies → fix is well-defined" loop wants its own generator:

- `cron_caps_filler` — uncapped agent-turn cron jobs (the case that surfaced the gap)
- `auth_drift_filler` — auth config drift on a bot (mirrors `permission_monitor`'s signals)
- `mcp_drift_filler` — MCP server config drift (surfaces `mcp_monitor` signals)
- `version_drift_filler` — bots running older Evolve than admin
- `plugin_allowlist_filler` — missing `plugins.allow` on a bot
- `acl_drift_filler` — `evolve` user ACL gaps
- `gateway_unhealthy_filler` — pairs with `gateway_diagnostician` for ongoing instability
- (more)

Each new generator: ~150–250 lines of Python under `analyzer/generators/<id>/`, plus a `charter.yaml`, plus a glossary entry (which the §3.8 drift test forces). Each is small + cheap to maintain. Coverage of operator pain points grows over time.

### 13.6 AGENTS.md teaching

Phase 1.4 adds a new section to AGENTS.md, taught to evo at session start:

> **Resolving operator-described issues in chat.** When the operator describes a problem, your FIRST move is to look for an existing proposal (`pod_state.proposals.pending`) matching the issue. If you find one, describe it and offer to apply via `action.proposal.apply`. Don't route the operator to the Recommendations page — act in chat.
> 
> If no proposal exists:
> - **Direct action available?** Use the corresponding `action.*` tool.
> - **Neither?** Either point at the missing generator (so the operator knows what to file as a structural gap) or offer to stage a one-off proposal for their approval.
> 
> **Hard rule — never tell the operator to navigate.** *"Go to the Recommendations page and click Take this on"* is the failure pattern. The right answer is *"I'll apply that for you — confirm?"* Authority tier dictates whether you need the confirm.

### 13.7 What this doesn't replace

- **The proposal queue UI stays.** Operators who want to review proposals one-by-one still have the Recommendations page. The inline buttons (Take this on / Snooze 1w / Dismiss) still work.
- **The applier infrastructure stays.** `action.proposal.apply` is a NEW CLIENT of the existing appliers, not a parallel implementation. Same code paths; same audit trail; same rollback story.
- **Generator-authored proposals stay primary.** Operator-authored ad-hoc proposals (§13.4 Q4) are the escape hatch, not the default. We don't want every fix authored by the LLM at chat time — the cost and the audit story both prefer deterministic generators.

### 13.8 Phase placement

This spec amendment is small + standalone. It lands first.

**Phase 1.4 then implements the resolver pattern** in this order:

1. `action.proposal.apply` (write_risky, synchronous, honors authority tier override field).
2. `action.proposal.reject` (destructive, always asks).
3. `action.bot.restart`, `action.bot.redeploy`, `action.app.install` (write_risky direct actions for cases that aren't proposal-shaped).
4. AGENTS.md "Resolving operator-described issues in chat" section.
5. First fresh generator: `cron_caps_filler` (the case that surfaced the gap, demonstrates the pattern).
6. Glossary entries for all of the above (drift tests force this).

**Phase 1.5+** (post-resolver pattern):
- Expand the generator catalog (one PR per new generator class, mechanical at that point).
- `action.bot.remove` (destructive lifecycle).
- Operator-authored proposal staging (the §13.4 Q4 escape hatch — a new tool `action.proposal.stage` that wraps Proposal construction + write).

---

## 14. Extending evo locally — the gap-filling architecture

Added 2026-05-19. Companion to §13. The resolver pattern works only when the proposal catalog is sufficient. The first time it isn't — and there will always be a first time — what should happen?

Three principles, each codified below as one extension level:

1. **No local code generation.** Code stays upstream. LLM-authored data + prose are allowed; LLM-authored Python tool code is not.
2. **Pod customization survives upgrade.** When upstream ships a capability that overlaps local customization, the result is a clean migration prompt — not a merge conflict.
3. **Gaps feed back upstream.** Operators don't have to file feature requests; the system reports anonymized gap patterns so the dev team's roadmap is driven by real operator behavior.

### 14.1 The extension hierarchy

Three levels of pod customization, ordered by abstraction:

| Level | Mechanism | What it's for |
|---|---|---|
| 1 | **One-off Proposals** (§13.4 Q4) | A single ad-hoc resolution. Evo authors a `Proposal` object; operator confirms; applier handles. |
| 2 | **Local Skills** (§14.2) | A repeatable recipe the operator wants reused. Markdown teaching evo how to combine existing tools. |
| 3 | **Tool-gap telemetry** (§14.3) | Anonymized signal to the Evolve dev team about which capabilities need first-class tools. |

**Explicit non-goal: no local Python tool code authored by the LLM.** Reasons:

- **Blast radius.** A tool is code with the agent's privileges. An LLM-authored tool missing a `validate()` check or with a subtle error-handling bug runs against pod state. The §13.4 Q4 escape hatch keeps the LLM in the data layer (Proposal schema) so the deterministic applier infrastructure does the actual write — same trust boundary the rest of the system uses.
- **Tier-system invariants.** Tools carry `risk_tier`, `validate()`, `verify_via`, authority gates. The construction-time guard in `evo/tools/__init__.py` rejects malformed tools at registration. LLM-authored tools either satisfy all of that (in which case the deterministic generator path was already an option) or fail to register (the construction guard refuses them).
- **Cost profile.** Memory `feedback_rsi_low_cost_preference` argues for deterministic Python over LLM for monitoring; same logic for tools. Reserve LLM cost for the *escalation* layer (the chat turn), not the substrate.

### 14.2 Local Skills — pod-authored recipes

OC has a native skill-loading system; we use it as-is. The loader scans **six sources** and resolves conflicts by name with later sources winning:

| # | Source | Path | Precedence |
|---|---|---|---|
| 1 | `openclaw-extra` | `agents.defaults.skills.load.extraDirs[]` (config) | LOWEST |
| 2 | `openclaw-bundled` | OC core install | low |
| 3 | `openclaw-managed` | OC-managed skills | mid |
| 4 | `agents-skills-personal` | `~/.agents/skills/` | mid |
| 5 | `agents-skills-project` | `{workspaceDir}/.agents/skills/` | high |
| 6 | `openclaw-workspace` | `{workspaceDir}/skills/` | **HIGHEST** |

The loader merges by `skill.name` — later sources overwrite earlier ones. So an Evolve-shipped skill in source 1 is automatically overridden by a same-named workspace skill in source 6. **No invented precedence layer needed**; OC's existing model handles workspace-overrides-Evolve correctly.

**Two storage locations:**

- **Evolve-shipped skills** live at `packages/analyzer/evolve_bot/skills/<name>/SKILL.md`. The deploy hook adds the resolved path to `agents.defaults.skills.load.extraDirs` in evo's openclaw.json (alongside `bootstrapMaxChars` and the existing config defaults). Source 1, lowest precedence — workspace customization always wins.

- **Pod-local skills** live at `{workspaceDir}/skills/local/<name>/SKILL.md`. Source 6, highest precedence. The `local/` subdirectory is namespacing convention for *this* spec (OC's loader doesn't care about subdir structure — it recurses for any `SKILL.md`). The path-namespace gives the operator a clear visual: anything under `local/` was authored here, anything outside is Evolve-shipped or OC-bundled.

**Skill frontmatter — required fields:**

```yaml
---
name: set-bot-model                    # unique; this is what OC's loader keys on
description: Recipe for changing a bot's primary model and verifying.
metadata:
  evolve:
    authored_by: evo                   # or "operator"
    authored_at: "2026-05-19T14:32:00Z"
    obviated_by: action.bot.set_model  # optional — tool name or prefix that
                                       # would make this skill redundant.
                                       # Drives the retirement detector below.
---
```

The `obviated_by` field is Evolve-specific (OC ignores unknown metadata). It declares the tool (exact name or prefix) whose existence would obviate this skill. Skills authored without it skip the retirement check; recommended for evo-authored skills, optional for operator-authored.

**Skill content** — markdown teaching the model a recipe. Cites real tools from the registry; never describes capabilities that don't exist. Same cite-the-tool rule from §3.7 lever #2 applies.

**Lifecycle:**

1. **Author.** Evo recognizes a repeatable pattern during chat (operator asks "set team-bot-a's model to X" twice; second time evo notices it just did this) and offers to author a skill: *"This is the second time you've asked me to change a bot's model. Want me to save this as a local skill so future requests use the same recipe?"* Or the operator authors one directly.
2. **Reuse.** Next time the same intent surfaces, OC's skill loader has the skill in context. Evo follows the recipe.
3. **Retire.** On every deploy, an Evolve step scans `{workspaceDir}/skills/local/` and reads each skill's `obviated_by`. For each match against the current tool registry, the deploy log emits a notice: *"local skill `set-bot-model` is obviated by the now-available `action.bot.set_model` tool. Run `evolve-admin retire-local-skill set-bot-model` to delete it, or leave it as belt-and-suspenders."* **Never automatic deletion.** Operator decides.

**New CLI subcommand** (Phase 1.5b work):

```
evolve-admin list-local-skills              # show local/ skills + retirement candidates
evolve-admin retire-local-skill <name>      # operator-confirmed delete
```

**Why we don't auto-delete:** the operator may have intentionally customized the recipe beyond what the upstream tool offers, OR may want the skill to act as documentation alongside the tool. Default = preserve; flag for operator review.

### 14.3 Tool-gap telemetry

Every time evo enters the §13.4 Q4 escape hatch (no proposal, no matching tool, must author one-off), it ALSO writes a structured `tool_gap` record:

```json
{
  "kind": "config.bot.set_model",
  "scope": "single_bot",
  "frequency_local": 3,
  "first_observed": "2026-05-19T14:32:00Z",
  "last_observed": "2026-05-22T09:11:00Z",
  "fingerprint": "config-edit:agents.defaults.model"
}
```

No chat content. No proposal body. No bot id. Just the shape of the gap — what kind of action evo wanted to take, the scope (single bot, pod-wide, etc.), a fingerprint that aggregates similar gaps, and frequency.

**Local storage:** `{shared_dir}/observations/tool_gaps.jsonl` — same convention as the existing observations infrastructure.

**Upload — opt-in.** Per `feedback_user_observation_optout`, observation features ship with a user-flippable DNT switch + wipe path. Operator's `network.json::evo_telemetry.tool_gaps` is one of:

- `"off"` (default) — never upload. Records stay local, useful for the operator's own debugging.
- `"aggregated"` — periodic anonymized rollup ships to Evolve's telemetry endpoint. No bot ids, no chat content, no proposal text. Just `{kind, scope, frequency, fingerprint}` per gap.

**Wipe path:** `evolve-admin wipe-telemetry` clears local `tool_gaps.jsonl` and the upload buffer. Same UX as other observation wipes.

**Upstream surface (Evolve dev team's view):** an aggregated dashboard showing "12 pods hit `action.bot.set_model` gaps in the last week, total frequency 47." Prioritization signal for which tools to ship first. Anonymized; no pod identity needed for the dev team's decisions.

### 14.4 Merge / redundancy during upgrade

What happens when upstream ships a tool that overlaps with a pod's local customization?

**Case A — Local skill, upstream ships covering tool.** Handled by §14.2 retirement detector. Deploy logs the notice; operator decides.

**Case B — One-off Proposal in `proposals/pending/`, upstream ships covering tool.** The proposal still has the action kind it was authored with. If the kind exists in the new version, the applier handles it normally — the proposal applies the same way. If the kind was renamed or split, the existing arbiter schema-migration tooling handles it (this is the same problem the arbiter has always had; the proposal/action-kind versioning model is unchanged).

**Case C — One-off Proposal in `proposals/archived/`.** Already applied; immutable history. No reconciliation needed.

**Case D — Tool-gap records reference an action_kind that's now a real tool.** Records stay (they're history). The telemetry signal helped prioritize the tool's existence; that's the success case. Operator can wipe via `evolve-admin wipe-telemetry` if they want a clean slate.

No code merge. No version-pinning. The pod's "customizations" are all DATA (Proposals) or PROSE (Skills) or TELEMETRY (gap records). Upstream upgrades produce notices, not conflicts.

### 14.5 What this doesn't replace

- **Operator-authored upstream PRs.** Operators who want a tool to exist upstream don't have to wait for telemetry — they can file a PR like anyone else. Telemetry is a prioritization signal, not a request channel.
- **Generator-authored proposals.** Local Skills and one-off Proposals are for *gaps* in the generator catalog. The generator catalog itself should still grow (§13.5).
- **OC's bundled skills.** Skills like `notion`, `python-debugpy`, etc. that OC ships are unaffected. They're general-purpose, not Evolve-specific. Evolve-shipped skills live alongside them in source 1 (extraDirs).

### 14.6 Phase placement

This is **Phase 1.5b work** — lands after Phase 1.4 (the resolver pattern is the prerequisite).

Build order:

1. **Phase 1.4** ships first (per §13.8): action.proposal.apply + the rest of the new action tools + AGENTS.md teaching + first fresh generator.
2. **Phase 1.5b** then ships §14:
   - Evolve-shipped skill loader hookup: deploy adds `agents.defaults.skills.load.extraDirs` entry pointing at `packages/analyzer/evolve_bot/skills/`.
   - First Evolve-shipped skill (probably a recipe for "investigate a firing alert end-to-end" — composes pod_state.signals.firing + pod_state.audit + a write tool).
   - Retirement detector in `evolve-admin deploy`.
   - `evolve-admin list-local-skills` + `retire-local-skill` CLI.
   - Tool-gap telemetry schema + local writer (in the proxy / send_to_evo path when §13.4 Q4 fires).
   - `network.json::evo_telemetry.tool_gaps` config field + opt-in upload daemon (separate launchd job, daily cadence).
3. **Phase 1.5c+**: Phase 2 of telemetry (upstream dashboard / aggregated view) — separate concern, doesn't gate the pod-side work.

The pod-side work (skills + local gap recording) is shippable independently of the upstream telemetry infrastructure. Phase 1.5b can land before there's any dev-team-facing dashboard.
