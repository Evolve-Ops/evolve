# Google Integration Architecture — Bot-Agent Tool Layer

**Status:** decisions RESOLVED 2026-06-21 (D-google-arch); building **P1**. See §8 for the
locked decisions; §5 engine resolved.
**Aspect:** `skills` (META:skills)
**Companion specs (auth mechanics, unchanged by this doc):**
[spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) (Paths A/B/C),
[spec-google-path-a-2026-06-01.md](spec-google-path-a-2026-06-01.md),
[spec-google-workspace-suite-2026-06-04.md](spec-google-workspace-suite-2026-06-04.md),
[runbook-path-c-google-integration.md](runbook-path-c-google-integration.md),
[runbook-google-oauth-personal.md](runbook-google-oauth-personal.md)

This spec is about **one question the existing specs never answered**: *how does a bot's own
agent actually get Google tools, and which credentials back them?* The path specs designed
the auth backends; this designs the layer the bot calls.

---

## 0. Why this spec exists (the precipitating incident, 2026-06-20)

A live Gate-2 test set up two Workspace bots (both on the same `@example-corp.com` tenant) for
Google via the Path-C (service-account + domain-wide-delegation) wizard. The wizard reported
"✓ Verified" for both. Then:

- Asked to check its email, **the first bot worked** — and we (wrongly) concluded "Path C
  validated end-to-end."
- Asked the same, **the second bot failed**, reporting a dead OAuth token and telling the
  operator to run `npx @googleworkspace/cli auth login`.

Investigation (four grounding agents, verified on the live pod) found the real story:

> **The bots' agents do not use the Evolve Path-C integration at all.** They use
> `@googleworkspace/cli` (**gws**), an OAuth tool each bot authed by hand. The first bot's gws
> token had been refreshed that day (so it "worked"); the second's was **6 weeks stale** (so it
> failed). The Path-C wizard configured a *separate* service-account layer (the Evolve
> `mcp-bridge`) that the bot agents are not wired to.

So the thing we built and tested (durable SA+DwD) is not the thing the bots run (per-bot
OAuth, unmanaged, silently rotting). This spec closes that gap.

---

## 1. Current state (verified 2026-06-20)

### 1.1 How a bot agent gets tools
Four stacked mechanisms (live-confirmed):

| Mechanism | Scope | Notes |
|---|---|---|
| **Evolve OC plugin** (`/Users/Shared/evolve-plugin`, `api.registerTool()`) | **every bot** | loads at agent start; tier-gated; already does loopback calls to the admin server (`list_signals`, `pod_status`, …). Knows its own `config.botId`. |
| `evo_tools` stdio MCP (`python -m evolve_admin.evo.tools`) | **primary bot only** | 30+ admin tools, `CallerIdentity`-gated |
| Skills `extraDirs` | primary only | Evolve-built skills |
| OC built-ins + bundled plugins | per allow-list | incl. an OC-native `google` plugin (OAuth, **not** Evolve's Google tools — do not conflate) |

**Member (non-primary) bots have `mcp.servers: []`.** The only universal, already-loaded
insertion point for a new tool on every bot is the **Evolve OC plugin → admin-server** path.

### 1.2 Evolve's first-party Google tool layer
- `mcp_bridge/google_tools.py` — **15 tools**: Gmail ×9, Calendar ×2, Drive ×4. **No Docs /
  Sheets / Slides / Contacts** (scoped in the wizard catalog, zero implementations).
- `google_auth.load_credentials(bot_id, scopes, network)` — **single unified interface**,
  dispatches on `google_integration.mode`: `service_account_dwd` (Path C) / `free_gmail_oauth`
  (Path A) / `workspace_user_oauth` (Path B → `NotImplementedError`). Tools carry **no
  per-path branching**. ✅ The tool surface is already auth-backend-agnostic.
- Creds: SA keys at `{shared}/secrets/google_service_accounts/<ref>.json`; OAuth tokens at
  `{shared}/secrets/google_oauth_tokens/<bot>.json` — **all `evolve:wheel 0600`**.

### 1.3 Two divergent tracks already in the tree
- **Bridge-tools track** (Gmail/Cal/Drive) → `google_tools.py`, served as `evolve-pod` over
  SSE for **Claude Desktop / evo / admin**. Not wired to member bots.
- **Per-bot embedded-MCP track** (Docs/Sheets/Slides) → `spec-google-workspace-suite` chose
  option α: install the third-party `taylorwilsdon/google_workspace_mcp` per bot with a
  `google_workspace_token_shim.py`. A *different* architecture (bot holds its own tokens).

These two have never been reconciled. This spec picks one.

### 1.4 What the bots actually run today (gws)
- Only **two bots** use gws today — one with a fresh token, one **6 weeks stale** (its crons
  silently skipping).
- gws is invoked by the bot's own scripts/heartbeat (`workspace/scripts/gws.sh`,
  `npx @googleworkspace/cli`) and one bot's Python (`google-auth` reading gws's
  `credentials.json`).
- **Auth is manual, per-bot, OAuth, unmanaged** — nothing provisions/refreshes/monitors it.
  The stale bot's reconciliation log shows a prior 2-day outage; it *recommended SA auth and it
  was never done.*

### 1.5 The hard security constraint
Bot processes run as their **own macOS user** and **cannot read the `evolve:wheel 0600`
secrets**. More importantly, a **DwD service-account key is domain-wide-impersonation
material** — putting it within a bot process's reach means that bot could impersonate *any*
Workspace user. **A bot must never hold the DwD key.** (Per-bot *consumer* OAuth tokens are
acceptable — their blast radius is that one consumer account.)

---

## 2. Goals & constraints

1. **One tool surface the bot agent actually calls** — no second hidden integration.
2. **Durable where possible:** Workspace bots get SA+DwD → no token expiry, no per-bot browser
   reauth. (Consumer `@gmail` is inherently OAuth — manage it well, can't make it eternal.)
3. **Both account types** first-class: Workspace (SA+DwD) and consumer `@gmail` (user OAuth).
4. **Bot never holds the DwD key** (§1.5). Credentials stay with the `evolve` user.
5. **Reuse upstream, don't reimplement** ([[feedback_dont_reimplement_upstream]]) — especially
   for the Docs/Sheets/Slides long tail.
6. **Evolve owns the credential lifecycle** — provision, refresh, health-monitor, alert. No
   silent rot (the stale-token failure mode above must be impossible).
7. **Per-bot identity is enforced below the LLM** — a bot can only ever act as *itself*
   (today the bridge trusts a client-supplied `bot` arg; that's a cross-bot data-leak hole).

---

## 3. The central tension: credential custody

Everything reduces to **where the credentials live and who executes the Google call**:

- **Bot-side** (the bot process runs the Google client / gws directly): simplest, already how
  gws works — but the bot must hold creds. Fine for consumer OAuth; **violates §4** for
  Workspace DwD. Also re-creates the per-bot unmanaged-token problem.
- **Evolve-intermediary** (an `evolve`-user process holds creds and executes; the bot calls a
  proxied tool): the bot never sees creds (satisfies §4/§5-sec), identity can be bound to the
  caller — but needs the tool wired to the bot agent and the call proxied.

The existing `google_tools.py` bridge is already the *evolve-intermediary* shape; the gws
status quo is the *bot-side* shape. **We recommend the evolve-intermediary shape** because the
DwD-key constraint (§1.5) is non-negotiable and it's the only shape that also fixes identity
and lifecycle.

---

## 4. Candidate architectures

| | **A. Evolve-intermediary tool layer** (recommended) | **B. Per-bot upstream MCP** (`taylorwilsdon`) | **C. Managed gws, bot-side** |
|---|---|---|---|
| Bot reaches tools via | Evolve OC plugin → admin-server proxy (already universal) | a per-bot MCP server in `openclaw.json` | bot's own `gws` calls |
| Creds held by | `evolve` user (admin server) | the bot's MCP process | the bot |
| Workspace (DwD) | ✅ key stays with evolve | ⚠️ bot process can reach the SA key | ❌ bot holds DwD key |
| Consumer OAuth | ✅ evolve provisions+refreshes | ✅ but per-bot, unmanaged | ✅ but unmanaged (status quo) |
| Identity binding | ✅ plugin's authenticated `botId` | ⚠️ per-server, needs care | n/a (bot is itself) |
| Coverage incl Docs/Sheets | via engine choice (§5) | ✅ 12 services built-in | ✅ gws full API |
| Reuse-upstream | engine can be gws (§5) | ✅ adopt a 2.7k★ MIT server (single-maintainer risk) | ✅ gws (Google-official) |
| Lifecycle mgmt | ✅ central | ✗ per-bot | ✗ per-bot (the rot we hit) |

**Recommendation: A.** It is the only option that satisfies the DwD-key constraint, binds
per-bot identity, and centralizes the credential lifecycle — while still reusing upstream for
execution (§5). B is attractive for its built-in Docs/Sheets but puts credentials (incl. the
DwD key for Workspace bots) in the bot's reach and scatters lifecycle. C is the status quo's
failure mode with a coat of paint.

### 4.1 Recommended shape (Option A in detail)
```
bot agent
  │  calls tool  google.gmail_list / google.docs_read / …   (registered by the Evolve OC plugin)
  ▼
Evolve OC plugin   (runs in the bot; knows its OWN config.botId; holds NO creds)
  │  loopback HTTP →  POST /api/google/<verb>   (bot identity = the plugin's botId, server-trusted)
  ▼
admin server  (runs as `evolve`; resolves bots.<botId>.google_integration.mode)
  │  loads creds via google_auth.load_credentials(botId, scopes)   ── creds stay here, never leave
  ▼
execution engine  (§5)  →  Google APIs
```
- **Identity is bound at the plugin layer** — the bot can't ask for another bot's data because
  the `botId` is the plugin's own config, not a client-supplied argument. (Fixes the bridge's
  untrusted-`bot`-arg hole, §1.2/§7.)
- **Workspace** bots resolve to SA+DwD impersonation of their `subject`; **consumer** bots to
  their managed OAuth token — both behind the same `load_credentials` call (§1.2), so the tool
  layer stays auth-agnostic.

---

## 5. Execution engine — RESOLVED: one curated Python tool layer

*What runs the Google call inside the evolve process?* **Decision (2026-06-21): a single
curated first-party tool layer over the official Google Python SDK** — extend the existing
`google_tools.py` (the 15 Gmail/Calendar/Drive tools) with Docs/Sheets/Slides via the same
`load_credentials` abstraction. **Not** gws-as-runtime; **not** a hybrid.

Rationale:
- The asset an LLM agent needs is a **curated, least-privilege, well-described, auditable**
  tool set — not the entire Workspace API. gws's strength (dynamically exposing *every*
  endpoint) is a **liability** for an agent surface.
- In-process typed SDK calls are more robust, observable, and auditable than a gws subprocess
  + CLI-output parsing.
- Both gws and the Python SDK are official Google clients; the thin curated wrappers are
  **product curation, not "reimplementing upstream."**
- One engine, forever — no second runtime to version, parse, or reconcile.

gws remains the **operator / power CLI** (not the bot runtime).
`taylorwilsdon/google_workspace_mcp` is a **reference** for DwD per-request impersonation, not
adopted as runtime.

---

## 6. Phased plan

- **P0 — unbreak the stale bot (optional band-aid, no arch change):** its gws OAuth token is
  dead. To restore it *now*, re-auth that bot's **own OAuth** (browser consent as the bot's
  Workspace address) — bot-scoped, low blast radius, but it's the unmanaged/expiry path.
  **Do NOT** point the bot's gws at the SA key — §1.5: that hands a bot the
  domain-wide-impersonation key. The SA path is durable *only* behind the evolve intermediary
  (P1). Since that bot's gws has been broken ~6 weeks already, deferring straight to P1 instead
  of band-aiding is equally reasonable.
- **P1 — wire the core tool layer to bots:** register the existing 15 `google_tools` on every
  eligible bot via the Evolve plugin → a new `/api/google/*` admin route, with `botId`-bound
  identity. Verify a configured bot checks mail through *this* path (not gws).
- **P2 — credential lifecycle:** the `gmail_integration_health` monitor already exists for the
  bridge — extend it to fire on the **bot-facing** path and to cover OAuth-token refresh/expiry
  for consumer bots, with operator alerts (no silent rot).
- **P3 — coverage parity:** add Docs/Sheets/Slides as curated tools over the Google Python SDK
  (same `load_credentials` abstraction) behind the shared tool layer; reconcile/retire the
  divergent `taylorwilsdon` per-bot track (§1.3).
- **P4 — retire bot-side gws:** migrate the bot Python scripts to the Evolve credential path /
  SA creds; remove `scripts/gws.sh`, the per-bot npm dep, and the manual-reauth docs. Update
  bot memory/AGENTS to call the Evolve tools.
- **P5 — consumer path:** finish Path-A so a consumer `@gmail` bot flows through the *same*
  bot-facing tool layer (managed OAuth), validating the consumer half end-to-end.

Each phase ships independently and is reversible; P0/P1 unblock the live test immediately.

---

## 7. Security notes
- DwD key never leaves the `evolve` user (§1.5). Bots get **results**, not credentials.
- Caller identity is the plugin's own `botId`, server-trusted — not a client argument. Audit
  every `/api/google/*` call with `(botId, verb, scope)`.
- Token-bearing files stay `0600` (the 06-20 self-heal, PR #3054). Consumer OAuth tokens are
  per-bot, low blast radius; the SA key is shared + high blast radius — treat differently.

## 8. Resolved decisions (2026-06-21)
1. **Engine (§5):** ONE curated Python tool layer over the official Google SDK; extend with
   Docs/Sheets/Slides. gws = operator CLI only, never the bot runtime. No hybrid.
2. **Consumer durability:** pursue Google **app verification** — the only durable consumer
   path (periodic re-consent is the rejected band-aid). But **Workspace SA+DwD is the strategic
   default**; consumer `@gmail` is a fully-supported second tier, sequenced *behind* the
   Workspace path (verification is a multi-week long pole and must not block the core).
3. **Eligibility:** Google tools register **per-bot, gated on a configured `google_integration`**
   (the wizard is the opt-in). Not fleet-wide, not tier-gated. Least-privilege by construction —
   no Google config ⇒ no Google tools.
4. **Bridge:** **one shared Google service module**; the bot-facing `/api/google/*` route, the
   `evolve-pod` bridge (kept as a surface for Claude-Desktop/evo), and evo all delegate to it —
   no forked logic. **Fix the bridge's identity model:** kill the client-supplied `bot`-arg
   trust; every surface supplies an *authenticated* caller identity (bot plugin → its own
   trusted `botId`; operator surfaces → operator-authenticated bot context).

## 9b. Remaining open questions (non-blocking)
- App-verification ownership/timeline (who drives the multi-week Google process, P2 spec §2).
- Whether evo's own Google access (admin-side) routes through the same shared module from day one
  or follows in P3.

## 9. References
- Grounding (2026-06-20, live-verified): bot tool-delivery mechanisms; `google_tools.py` /
  `google_auth.py` internals; upstream MCP/gws survey; gws fleet entrenchment.
- Upstream: `@googleworkspace/cli` (github.com/googleworkspace/cli, Apache-2.0, SA+DwD native);
  `taylorwilsdon/google_workspace_mcp` (MIT, SA+DwD first-class, 12 services).
- [[feedback_dont_reimplement_upstream]], [[feedback_bot_cannot_observe_own_routing]],
  [[feedback_bot_secret_config_0600_and_cp_mode_semantics]],
  [[project_evolve_substrate_strategy]].
