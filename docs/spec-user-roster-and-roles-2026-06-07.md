# User roles and capability gating — design spec

**Status:** Draft. Pre-implementation. **Extends — does not replace —** [spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md), whose Phases 1, 1.1, and 2 already shipped (Users page, OC-native allowlist surface, auto-approval, name enrichment). This spec adds the role and capability layers above the existing identity admission gate.

**Date:** 2026-06-07.

**Origin:** Conversation about preparing Atlas for a real Telegram group. Three concerns surfaced: (1) anyone in the group can @mention Atlas and burn LLM cycles, (2) admitted users have no granularity below "can use the bot" — a participant should not be able to make Atlas install apps or send email, (3) existing guard mechanisms (POD_CONDUCT injection, `bot_guidance`, app-side `audience_scoping` like Atlas's `atlas_lib/guard.py`) are bot-honor rules a jailbreak defeats.

Mid-design, exploration of the codebase surfaced [spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md) — already shipped. That spec solves concern (1) fully: a per-bot allowlist keyed on platform stable_id, pairing flow with `/start`, auto-approval for pod admins, rejection cool-off, admin UI surface. So this spec drops the identity-admission layer and focuses entirely on what's *above* the admission gate: a typed role for each admitted identity, a capability bundle per role, iron-clad enforcement at the MCP-tool boundary, and the messaging-based admin paths primary users need given they don't have admin-UI access.

**Adjacent:**

- [spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md) — **the prerequisite layer.** This spec builds on top of its admitted-users surface. Whenever this spec says "the admitted identity," it means an entry in `<bot_home>/.openclaw/credentials/<provider>-default-allowFrom.json`.
- [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) — establishes the admin-daemon-is-the-privileged-actor pattern and the unix-socket API surface. This spec extends that surface with new endpoints for role/engagement mutations.
- [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) — establishes the OS-user-is-the-policy-boundary principle. This spec adds a per-requester capability layer on top of the existing per-bot OS boundary.
- [spec-agent-freelance-bypass-2026-06-05.md](spec-agent-freelance-bypass-2026-06-05.md) — documents the class gap where an agent freelances on general tools when a script fails. The per-role MCP tool allowlist in this spec is the durable defense.
- [project_manifest_schema_v7_recommendation](../memory/project_manifest_schema_v7_recommendation.md) — this spec drives the `provided_capabilities` and `requires_capability` parts of v7.
- [project_user_types_and_approval](../memory/project_user_types_and_approval.md) — the three-user-type framing (pod sysadmin / personal-bot user / team-bot member). This spec realizes those types as `admin` / `primary_user` / `participant` roles, on top of the existing identity layer.

---

## Problem (revised)

The identity-admission layer is **done**. The 2026-05-29 spec admits or denies users by matching their platform stable_id against an OpenClaw-native allowlist; pod admins auto-approve via a periodic sweep; rejections cool off for an hour; everything is administered through the Users page.

What's still missing, and what this spec addresses:

1. **No granularity below "admitted."** Once a user is on the allowlist, the bot's full MCP tool surface and full app behavior is available to them on demand. A participant in Atlas's group can ask Atlas to install an app, modify code, or send email — and the gate is the LLM honoring conduct rules, not a tool boundary the LLM can't cross.

2. **No way to grant a non-admin trusted user administrative reach over a single bot.** The user-types framing names this person `primary_user`: admin-equivalent for one bot, no Evolve admin UI access, manages via messaging only. The 2026-05-29 spec has a `primary owner` label that surfaces in the UI, but it's identity metadata, not a permission claim. A primary owner today has the same effective permissions as any other admitted user.

3. **No first-class block.** The 2026-05-29 spec has a 1-hour rejection cool-off to prevent accidental auto-re-approval, but no sticky deny for a person who has lost trust. Today, "removing" a user means revoke (drop from allowFrom.json) — if they re-pair via `/start`, they're admitted again (and if they match a pod-admin claim, auto-approved).

4. **No engagement-surface distinction.** Group chat and DM are different threat profiles — DMs are unwitnessed and uncosted-by-social-pressure — but admitted users are admitted on every surface their identity reaches.

5. **No "newcomer mode" choice.** The existing pairing flow is effectively always `require_approval` (admin must approve every `/start`). For trusted private groups where membership = approval, the operator wants an `auto_admit` mode; for sensitive bots, a `closed` mode that ignores `/start` entirely.

The principle these point at: **enforcement must live outside the LLM wherever the worst-case bypass causes external effects** (sent message, exec, file write, money spent). The LLM is allowed to be the gate only where the worst case is "the bot said something it shouldn't have."

---

## Principle

**Every admitted identity carries a role; the role binds a capability set; the gateway loads only the MCP tools the capability set names.** Neither the LLM nor a freelancing agent can use a tool that was never loaded.

Three corollaries that follow from this and from the existing 2026-05-29 work:

- **Identity admission is already iron-clad.** The 2026-05-29 layer verifies the platform stable_id against OC's allowlist at the channel boundary, below the LLM. Roles attach to admitted identities; the role does not weaken or change the admission decision.

- **Capability enforcement is the per-session MCP tool allowlist.** Each role binds a capability set; each capability names the MCP tools it requires (via app manifest declaration or built-in mapping). The gateway loads the union into the session. A jailbreak cannot invoke a tool that was never loaded.

- **Role and engagement mutations are admin-daemon API calls, not file writes.** Even when an admin or primary_user requests a role change via natural-language chat, the LLM's only path to mutation is a typed API call against the admin daemon's roster endpoint. The daemon checks the *requesting identity's* role on the *target bot* before applying. The LLM cannot promote itself.

This is the same architectural pattern as evo-account-separation, generalized.

---

## Architecture

### 1. Relationship to the existing identity gate

The 2026-05-29 spec owns two pieces of state per bot:

- `<bot_home>/.openclaw/credentials/<provider>-default-allowFrom.json` — OC-native allowlist of channel-native IDs. **Source of truth for *DM* admission** (`dmPolicy: pairing`). *Note (R1a, 2026-06-17): this gates direct messages only — group/channel admission is a separate gate against `openclaw.json::channels.<provider>.allowFrom` (`groupPolicy: allowlist`). See Layer 1 below.*
- `<bot_home>/.openclaw/credentials/<provider>-pairing.json` — OC-native pending pairing requests.

Plus a third pod-wide piece of state:

- `network.json` identity claims — pod-admin claims and per-bot primary owners.

This spec adds **one overlay file per bot:**

- `{shared_dir}/rosters/{bot_id}.json` — evolve-owned, atomic temp-file + rename writes.

The overlay carries everything the existing files cannot: per-identity role + engagement_surfaces + notes, per-channel newcomer_mode, the sticky block index. It does not replicate the allowlist (OC's file remains source-of-truth for "is this stable_id admitted?"); it layers metadata on the same stable_id keys.

When the existing GET endpoint resolves a user, the overlay is joined in. When a mutation needs to add to the allowlist, the existing 2026-05-29 path is used. When a mutation needs to set a role or engagement set, the overlay is updated.

### 2. Identity model

Identities continue to be tuples of `(platform, stable_id)`, matching the 2026-05-29 spec exactly. No changes here.

### 3. Roles

Four roles, named consistently across bots. They **derive** from a combination of existing state and the overlay:

| Role | Source | Evolve UI access | Where managed | Scope |
|---|---|---|---|---|
| `admin` | network.json pod-admin claim (existing) | Yes | UI primary; messaging additionally | All bots (pod-level) |
| `primary_user` | overlay role field, defaults from network.json primary-owner claim | No | Messaging only (Path B / Path C) | One bot |
| `participant` | overlay role field (default for an admitted identity with no explicit role) | No | UI or messaging | One bot |
| `blocked` | overlay role field + block index (sticky) | No | UI or messaging | One bot |

`admin` is not a new role — it's the existing pod-admin claim, named consistently here. `primary_user` is new and is what this spec adds for the "admin-equivalent on this one bot, manages via messaging" case. It defaults from the existing primary-owner claim in network.json: the bot's primary owner is automatically primary_user unless the admin explicitly demotes them in the overlay.

`participant` is the default for an admitted identity with no other role assigned. `blocked` is new — sticky deny that survives re-pairing.

### Role resolution

Per admitted identity (in priority order):

1. If the overlay has the identity in the **block index** → `blocked`
2. Else if the identity matches a pod-admin claim in `network.json` → `admin`
3. Else if the overlay specifies an explicit role → that role
4. Else if the identity matches the bot's `primary_owner` claim in `network.json` → `primary_user`
5. Else → `participant`

This is computed at admission time (Layer 1) and the resolved role travels with the request through Layers 2-4.

### 4. Capabilities

A capability is a string identifier that names a permission. Capabilities come from two sources:

**Built-in:**

| Capability | What it gates |
|---|---|
| `bot.roster.read` | Read the roster (overlay + computed roles) |
| `bot.roster.mutate` | Add/modify/block identities, set roles, set engagement surfaces |
| `bot.roles.bind` | Modify role → capability bindings |
| `bot.channel.config` | Modify per-channel newcomer_mode and surface defaults |
| `bot.config.modify` | Edit bot configuration (openclaw.json, etc.) |
| `bot.app.install` | Install / remove apps |
| `bot.code.modify` | Modify bot scripts / code |
| `bot.send_external` | Reach outside the bot's primary channel (email, alternate channel post, webhook) |

**App-declared:** in the app manifest (schema v7):

```yaml
provided_capabilities:
  - name: app.archive.add
    description: Add an article URL to the archive
    requires_mcp_tools: [archive.add]
    default_role_binding: participant
  - name: app.archive.delete
    description: Remove an article from the archive
    requires_mcp_tools: [archive.delete]
    default_role_binding: primary_user
```

`requires_mcp_tools` is the link between capability and gateway tool allowlist (Layer 2). `default_role_binding` is the suggested initial binding when the app installs; admin can override.

### 5. Role → capability binding (per bot)

Per-bot, in the overlay file. Default bindings:

```json
"role_bindings": {
  "admin": ["*"],
  "primary_user": ["*"],
  "participant": ["app.archive.add", "app.notes.add"],
  "blocked": []
}
```

`"*"` = all currently declared capabilities on this bot. When a new app installs and declares new capabilities, each binds to its `default_role_binding`; admin sees a one-line confirmation in alerts ("Atlas: new capability `app.calendar.create_event` available, bound to participant by default").

### 6. Engagement surfaces

Per-identity, in the overlay. A set drawn from `{"group", "dm"}`:

- `["group"]` — bot responds when @-mentioned in a shared group, ignores DMs
- `["dm"]` — bot responds to DMs, does not honor group mentions (rare; private-user pattern)
- `["group", "dm"]` — both

Defaults: an identity admitted via the existing pairing flow with no overlay entry inherits the channel's `default_engagement_surfaces` (see overlay schema below). Channel default for groups is `["group"]`; for DM-only channels, `["dm"]`. Admin and primary_user override to `["group", "dm"]` by default.

### 7. Overlay file schema

`{shared_dir}/rosters/{bot_id}.json`. Evolve-user-owned. Atomic writes.

```json
{
  "bot_id": "atlas",
  "version": 1,
  "channels": {
    "telegram:-100123456789": {
      "newcomer_mode": "auto_admit",
      "default_engagement_surfaces": ["group"]
    }
  },
  "identities": {
    "telegram:98765432": {
      "role": "primary_user",
      "engagement_surfaces": ["group", "dm"],
      "notes": "",
      "added_at": "2026-06-07T12:00:00Z",
      "added_by": "admin:pod-admin"
    }
  },
  "blocked": {
    "telegram:55555555": {
      "blocked_at": "2026-06-07T13:00:00Z",
      "blocked_by": "primary_user:bob",
      "reason": "spam"
    }
  },
  "role_bindings": {
    "admin": ["*"],
    "primary_user": ["*"],
    "participant": ["app.archive.add", "app.notes.add"],
    "blocked": []
  }
}
```

Identity keys are `"<platform>:<stable_id>"` strings (Slack uses `"slack:<workspace>:<user_id>"`). This shape merges cleanly with the 2026-05-29 spec's GET response — for each entry in `by_channel.<channel>.approved[]`, look up `<platform>:<id>` in the overlay's `identities` and `blocked` maps.

Audit log appends to `{shared_dir}/rosters/log/{YYYY-MM-DD}.jsonl` with `{ts, requester, action, target_bot, target_identity, before, after}` per mutation. One-year retention.

### 8. Enforcement layers

Four layers, earliest-to-latest. Earlier layers are cheaper and iron-clad; later ones are partly bot-honor.

#### Layer 1: Channel ingress identity gate (largely exists)

The 2026-05-29 spec ships this. OC's gateway admits or denies incoming senders fully outside the LLM. **Crucially, this is *two* independent gates, against two different files** (corrected R1a, 2026-06-17 — the original wording named only the first and implied it gated all senders):

- **DM access** (`dmPolicy: pairing` / `allowlist`) consults the credentials pairing store `<provider>-default-allowFrom.json`. This is what the Evolve Users page reads, renders as "Approved · DM", and manages via approve/reject/disconnect.
- **Group/channel access** (`groupPolicy: allowlist`) consults the *config* allowlist `openclaw.json::channels.<provider>.allowFrom` (or `channels.<provider>.groupAllowFrom` when set; OpenClaw falls back to `allowFrom` by default). OC's `group-access` module keys this **per-sender** and is fail-closed (`empty_allowlist` / `sender_not_allowlisted` → denied). These two lists routinely diverge: a sender can be group-authorized but not DM-paired, and vice versa.

A user "not admitted" on the DM list may therefore still be processed in channels because they are on the *group* list — not a bypass (the group gate fired correctly), but a governance/visibility gap, since the group list was historically unmanaged in the admin UI. The full R1a diagnosis (two-allowlist coherence) is in `docs/spec-users-meta-2026-06-15.md` § "R1a diagnosis (2026-06-17)"; surfacing the group list on the Users page is its read-side fix.

This spec adds three thin behaviors on top of the DM gate:

- **Block check** — before OC's standard admission, the gateway consults the overlay's `blocked` map. A blocked identity is silent-ignored regardless of whether they're in the OC allowlist. Block takes precedence.
- **Engagement-surface check** — after OC admits, check whether the current surface (group vs. DM) is in the identity's resolved engagement_surfaces. If not, silent ignore.
- **Newcomer mode handling** — when OC's pairing flow receives a `/start` from an unknown sender, the overlay's per-channel `newcomer_mode` controls the response:
  - `auto_admit` — admit immediately as participant with channel default engagement surfaces (bypasses pairing-code flow)
  - `require_approval` — existing pairing-code flow (unchanged from 2026-05-29)
  - `closed` — silent ignore, no pairing code issued

These extensions live in the same Evolve-side handler that the 2026-05-29 spec already calls into for auto-approval (`pairing_auto_approver`). The OC gateway logic does not change; the Evolve-side glue does.

#### Layer 2: Per-role MCP tool allowlist (new — the iron-clad capability layer)

Where: gateway session initialization, when the LLM session starts for this turn.

Logic: from the requester's resolved role, compute the bound capability set. From each capability, compute the union of `requires_mcp_tools` (app-declared) plus built-in tool mappings (e.g. `bot.roster.mutate` → `[roster.mutate_tool]`). Load only those tools into the session.

This is the iron-clad layer for tool-mediated actions. A jailbroken LLM cannot invoke a tool that was never loaded into its session. Specifically: a participant turn does **not** have the `send_email` tool, the `roster.mutate` tool, the `app.install` tool, or any other tool not derivable from participant-bound capabilities. Whatever the prompt says, the tool isn't there to call.

This layer also neutralizes the agent-freelance-bypass class gap. When an app script fails and the agent freelances, it freelances within only the tools the requesting role permits.

Implementation site: the OC plugin's `before_agent_run` hook in `packages/plugin/src/observer/TurnObserver.ts` (around line 2872 — where cost breakers already run). Resolve role from the verified identity; consult the role binding; compute and apply the per-session tool subset.

#### Layer 3: App-script capability check (iron-clad if used)

Inside an app's script, before performing the action: `evolve.capabilities.check(context.requester, "app.archive.add")` and refuse if absent.

Necessary because some app actions don't reduce to a single MCP tool call. An app might dispatch to multiple internal helpers based on argument shape, or have a "delete" path that branches off a "read" path. The script-internal check guards those.

Known weakness: if the agent freelances around the script entirely, this check is skipped (the script never runs). Layer 2 catches this — the tools the freelancing agent reaches for are already restricted.

#### Layer 4: POD_CONDUCT injection (bot-honor)

Per-turn system context: inject verified identity, resolved role, current capability summary, plus role-aware conduct rules ("you are talking to a participant; do not promise to take admin actions; explain limits gracefully").

For behavior shape, not enforcement. A jailbroken LLM can ignore these rules; the iron-clad layers ensure the worst it can do is misspeak, not act unauthorized.

### 9. Mutation API

Extends `routes_bot_users.py`. Existing endpoints stay; new endpoints add:

```
PATCH  /api/admin/bots/{bot_id}/users/{channel}/{stable_id}
       body: { role?, engagement_surfaces?, notes? }
       auth: requester has bot.roster.mutate on {bot_id}
       effect: updates overlay identity record; if role is admin or primary_owner
               (network.json) requires elevated auth (admin only)

POST   /api/admin/bots/{bot_id}/users/{channel}/{stable_id}/block
       body: { reason? }
       effect: (a) revoke from OC allowFrom.json via existing path,
               (b) add to overlay block index,
               (c) reject any pending pairing requests for this id
       auth: requester has bot.roster.mutate on {bot_id}

POST   /api/admin/bots/{bot_id}/users/{channel}/{stable_id}/unblock
       effect: remove from overlay block index. Does NOT re-add to allowFrom —
               re-admission is a separate explicit step.
       auth: requester has bot.roster.mutate on {bot_id}

PUT    /api/admin/bots/{bot_id}/channels/{channel}/newcomer_mode
       body: { mode: "auto_admit" | "require_approval" | "closed",
               default_engagement_surfaces?: ["group"] | ["dm"] | ["group", "dm"] }
       auth: requester has bot.channel.config on {bot_id}

PUT    /api/admin/bots/{bot_id}/role_bindings/{role_name}
       body: { capabilities: ["..."] }
       auth: requester has bot.roles.bind on {bot_id}
```

The existing GET endpoint extends additively: each admitted identity gains `role` and `engagement_surfaces` fields; each channel gains `newcomer_mode` and `default_engagement_surfaces`. A new top-level `blocked[]` array surfaces blocked identities for the UI's "Blocked users" section.

All endpoints continue to gate on `@require_trusted_peer()` (the existing peer-uid auth from the unix-socket transport). The new auth wrinkle: requester role must be checked. Today the daemon trusts any "trusted peer" equally. The new endpoints add a header `X-Requester-Identity` (gateway-attested) carrying `{platform, stable_id, source_bot_id}` and the daemon resolves the role on the *target bot* and checks the required capability.

For Path A (admin UI), the admin UI sends `X-Requester-Identity: {role: "admin", source: "ui"}` — the UI is trusted via the existing peer-uid check to assert this.

### 10. Admin paths

Three paths, all calling the same daemon API:

**Path A: Evolve admin UI** (admin only). Extends the existing Users page with role chips, engagement-surface chips, inline role/engagement edit affordances, block button, per-channel newcomer_mode selector. No new tab; new affordances within `_usersRenderBotPanel`.

**Path B: bot-LLM-direct** (admin or primary_user on this bot). New in this spec. The user says in chat: "block alice", "set bob to primary_user", "set channel to auto-admit". Because their role binds `bot.roster.mutate` and `bot.channel.config`, the gateway loaded the corresponding MCP tools into their session at Layer 2. The bot extracts intent, calls the tool, the tool hits the daemon API. Confirms in chat.

Both natural-language ("please block alice, she was being disruptive") and keyword forms ("/roster block @alice", "/channel auto_admit") supported. Keyword is recommended for high-friction operations because it parses deterministically.

**Path C: evo cross-bot** (admin, or primary_user on the target bot). New in this spec. User talks to evo: "evo, roster atlas block alice" or "evo, set atlas to auto-admit on telegram". Evo's gateway has the relevant MCP tools available (it always does, because evo's purpose includes cross-bot administration). The tool calls the daemon API with `target_bot=atlas`. Daemon checks the requester's role *on atlas* — not on evo. For primary_users, this means they can manage any bot they're primary on; for admins, all bots.

Both B and C reach the same daemon API surface, with the same auth check. The choice between them is ergonomic, not security-driven — the per-role MCP allowlist equalizes them.

### 11. Newcomer modes per channel

Set per-channel in the overlay file. Three modes:

- **`auto_admit`** — `/start` from an unknown sender admits immediately (writes to OC allowFrom.json) with the channel's default engagement surfaces. No pairing code. Best for trusted private groups (your Atlas case).

- **`require_approval`** — the existing 2026-05-29 pairing flow (unchanged). `/start` returns a code; admin approves via UI or messaging. Auto-approval still applies for pod-admin matches.

- **`closed`** — `/start` is silent-ignored. No pairing code, no signal. Roster is operator-managed only. Best for sensitive bots.

Membership-change events: for `auto_admit` channels, when a member leaves the group (per Telegram `chat_member`, Slack `member_left_channel`, etc.), the channel handler removes them from OC's allowFrom (no overlay change — they were never explicitly elevated). For `require_approval` and `closed`, departures file a `roster_member_left` Signal for operator awareness, no state change.

Re-add to the group does **not** auto-restore an admitted user who was removed. Re-admission requires explicit pairing (or, for `auto_admit`, a fresh `/start`).

---

## Manifest schema v7 dependency

This spec drives two additions to manifest schema v7:

```yaml
provided_capabilities:
  - name: app.archive.add
    description: Add an article URL to the archive
    requires_mcp_tools: [archive.add]
    default_role_binding: participant

requires_capability:
  scripts:
    archive_add.py: app.archive.add
    archive_delete.py: app.archive.delete
```

`provided_capabilities` flows into the bot's capability registry at install time. `requires_capability` lets the script dispatcher consult capability before invoking the script (Layer 3) and lets the agent's tool descriptions filter by what the current requester can actually invoke (so the participant's session doesn't even see a tool listing for `archive_delete`).

Apps that don't declare `provided_capabilities` default to "all exposed tools bind to `participant`" — backward-compatible; admin can tighten per-app.

---

## Rollout phases

### Phase A — Overlay + extended UI (start here)

- Overlay file schema and reader/writer module
- Atlas overlay seeded from existing per-bot user record (every admitted identity → role: participant, default surfaces from channel type)
- Extend existing GET `/api/admin/bots/{bot_id}/users` with `role`, `engagement_surfaces`, `blocked[]`, per-channel `newcomer_mode`
- Add PATCH role/engagement endpoint, POST block/unblock endpoint, PUT newcomer_mode endpoint
- Extend Users page UI: role chip, engagement_surface chips, role/engagement edit affordance, block button, newcomer_mode selector per channel
- Block enforcement at Layer 1 (overlay block index consulted before existing admission)
- Engagement-surface enforcement at Layer 1
- Newcomer-mode `auto_admit` and `closed` modes added (`require_approval` continues to be the existing pairing flow)
- `primary_user` role exists in data model; gets auto-assigned from primary-owner claim

**Effective behavior at end of Phase A:** admin can assign roles, engagement surfaces, sticky blocks via UI; per-channel newcomer mode is operator-controlled; admitted users carry meaningful role metadata; blocks survive re-pairing. No capability enforcement yet — that's Phase B.

### Phase B — Capabilities and MCP allowlist

- Manifest schema v7 `provided_capabilities` and `requires_capability` fields
- Role → capability binding stored in overlay file
- Per-role gateway MCP tool allowlist applied in `before_agent_run` hook (Layer 2)
- App-script capability helper `evolve.capabilities.check` (Layer 3)
- Atlas's archive + notes apps migrated to declare capabilities
- "New capability available" admin alert on app install
- PUT role_bindings endpoint

**Effective behavior:** per-role tool restriction enforced; freelance bypass class gap closes; participants literally cannot invoke privileged tools regardless of jailbreak.

### Phase C — Messaging admin paths

- `roster.mutate` and `channel.config` MCP tools
- Path B (bot-LLM-direct) — Atlas first, then any bot with a primary_user
- Path C (evo cross-bot) — extend admin-daemon with cross-bot scope, register evo's roster commands
- Keyword form parser ("/roster ...", "/channel ...")
- POD_CONDUCT injection of role + capability summary (Layer 4)

**Effective behavior:** primary_user role is fully functional without admin UI access; admin can manage rosters by chatting with the affected bot or with evo.

### Phase D — Other platforms

Identity admission for non-Telegram channels already works via the 2026-05-29 spec. Phase D extends the overlay/role/engagement layer to the same channels (Slack, Discord, WhatsApp, Signal, iMessage). Mostly per-platform configuration, no architectural change.

### Phase E — Email (separate spec)

DMARC-required inbound auth, forwarded-mail edge cases, mailing-list interactions. Out of scope here.

---

## Open questions

1. **Capability versioning on app update.** If an app updates and renames or removes a capability, do existing role bindings break? Proposal: on install/upgrade, diff the capability set; surface `added` / `removed` / `renamed` in a one-shot admin prompt. New capabilities default to declared `default_role_binding`. Resolve before Phase B.

2. **Primary_user bootstrap.** Who assigns the first `primary_user` on a bot? Proposal: defaults from the existing `primary_owner` network.json claim. If no primary_owner, the admin assigns via UI. Once a primary_user exists, they can promote others via Path B/C.

3. **roster_member_left Signal lifetime.** Auto-archive after some window? Proposal: 30 days, matching proposal-store archived retention.

4. **Cross-platform identity unification.** If Alice is `@alice` on Telegram and `alice@slack` on Slack, are those the same Alice? Proposal: distinct identities by default. Phase D may surface "looks like the same person, link?" as a hint, but the link is administrative metadata, not an authentication shortcut. **→ RESOLVED (2026-06-23) — designed in [spec-user-identity-merge-2026-06-23.md](spec-user-identity-merge-2026-06-23.md):** distinct-by-default holds; an operator-driven, reversible *merge* links them under one stable `person_id`. The merge is **administrative identity metadata, never an authentication/authority shortcut** — roles/capabilities stay strictly per-platform-identity (merge unifies identity, never authority), exactly as this hint anticipated.

5. **Overlay file ownership and ACLs.** Per the existing pod-perms invariant from evo-account-separation, files in `{shared_dir}/proposals/` and `{shared_dir}/signals/` carry an inherited ACL for the `evo` user. Should `{shared_dir}/rosters/` follow the same pattern? Required if evo's admin path (Path C) writes the overlay directly; not required if Path C goes through admin-daemon HTTP (current design). Proposal: keep Path C through the daemon — no special ACL needed. The roster file is evolve-owned, daemon-mediated.

6. **Block index vs. overlay block field.** The schema above shows blocks as a top-level `blocked` map. Alternative: an `identities.<key>.role = "blocked"` entry with the existing identities map. Either works; the top-level map makes "list blocked users" trivial and avoids hiding the sticky-deny in normal identity iteration. Recommend top-level.

---

## Risks

**Social engineering of bot-LLM-direct admin (Path B).** A clever participant could ask a primary_user in the same group to "tell atlas to promote me to primary_user." The primary_user might do it without checking. Mitigation: every roster mutation appears in the existing audit log; admins can review after the fact. We don't ship UI-side approval for primary_user actions because the value of Path B is friction-free management.

**Freelance bypass on Layer 3.** Documented; Layer 2 is the durable defense. Layer 3 is belt-and-suspenders for in-script branches Layer 2 can't see.

**Overlay file corruption.** Atomic writes plus the daily JSONL audit log give a recovery path. A `evolve-admin roster-restore --from-log <date>` helper should ship with Phase A.

**Auto-admit drift.** With `auto_admit`, anyone added to the group by another member becomes a participant automatically. If group membership policy drifts (less curated over time), the bot's allowlist drifts with it. Mitigation: surface a weekly "X new participants added via auto_admit on Atlas, review?" Signal.

**Platform membership-event reliability.** Telegram delivers `chat_member`; Slack delivers `member_left_channel`. WhatsApp Business API is more limited. Without an event, the overlay can't auto-remove on departure. Mitigation: a periodic reconciliation pass per channel (Phase D).

**`auto_admit` interacts with existing pairing flow.** Existing OC behavior: `/start` always returns a pairing code. If we set `newcomer_mode: auto_admit`, our handler must intercept *before* OC issues the code (otherwise users see a code they don't need to use). Likely requires a small OC plugin hook; investigation needed in Phase A.

---

## Dependencies

- **[spec-per-bot-users-management-2026-05-29.md](spec-per-bot-users-management-2026-05-29.md)** — Phases 1, 1.1, 2 must be in place (they are). This spec extends, not replaces.
- **Existing `routes_bot_users.py`** — extended additively; existing endpoints unchanged.
- **Existing Users page (`_usersRenderBotPanel`)** — extended with new fields/affordances.
- **Manifest schema v7** — `provided_capabilities`, `requires_capability` (this spec drives that schema change).
- **Admin-daemon unix-socket API** — extended with new endpoints; auth shape gains gateway-attested requester identity.
- **Per-bot gateway MCP tool scoping** — new infrastructure in the OC plugin's `before_agent_run` hook.
- **POD_CONDUCT injection channel** — exists; extends with role-aware blocks (Phase C).
- **Signal store** — adds `roster_member_left`, `roster_capability_added`, `roster_auto_admit_drift` types; mechanically identical to existing types.
- **Telegram `chat_member` webhook** — already integrated for the 2026-05-29 pairing flow; reused.

---

## Naming notes

- **Role** — operator-facing label (admin, primary_user, participant, blocked). What the operator picks.
- **Capability** — code-level permission string (`app.archive.add`, `bot.roster.mutate`). What the code checks.

The mapping role → capabilities is per-bot. The mapping capability → MCP tools is per-app-declaration. Identities are assigned a role, not capabilities directly. Custom roles are out of scope for v1 but the data model accommodates them.

"Permission" is avoided because it's overloaded with macOS file permissions throughout the codebase. Capabilities are the higher-level Evolve concept.

**"Primary user" vs. "primary owner."** The existing `primary_owner` claim in `network.json` is *identity* metadata — "who is the natural owner of this bot?" The new `primary_user` role is *permission* metadata — "who has admin-equivalent permissions on this bot?" They default to the same person but can desync (an admin might set someone as primary_user without making them the primary owner, e.g. a temporary delegate). The Users page should surface both — "Owner" chip from network.json, "Primary user" chip from the overlay role — so the distinction is visible.

---

# Status as of 2026-06-08

Everything in the original spec above is shipped, plus three architectural deviations and several operator-driven additions worth capturing for the next reader. The spec's phases (A / B / C / D / E) completed; the deviations are where the implementation diverged from the design and why.

## Phase status

| Phase | Spec section | What | Status |
|---|---|---|---|
| A | "Phase A — Overlay + extended UI" | Roster overlay file, GET extension, PATCH/block/unblock/newcomer-mode endpoints, Users-page extensions | Shipped (#2383) |
| B.1 | "Phase B — Capabilities and MCP allowlist" (subset) | Manifest v7 `provided_capabilities` / `requires_capability` fields, validator, audit migration, plugin interceptor for at-risk apps | Shipped pre-conversation (#2246, #2248, #2251, #2253) — discovered mid-conversation when planning Phase B |
| C.1 | "Phase C — Messaging admin paths" | Capability registry + admin-daemon `X-Requester-Identity` header + evo cross-bot `action.roster.*` / `action.channel.*` tools | Shipped (#2387) |
| C.2 | "Phase C — Messaging admin paths" (Path B) | Plugin-side `roster_*` / `channel_*` tools registered on every bot; DM-only initial cut | Shipped (#2392) |
| C.3 | (not in original spec — Path-B-for-groups follow-up) | Group sender extraction via OC's `BeforeAgentRunEvent.senderId` captured in a per-runId registry | Shipped (#2393) |
| C.4 | "Layer 4 — POD_CONDUCT injection" | TS port of role resolution + per-turn speaker-context block injected via `before_prompt_build` | Shipped (#2397) |
| C.5 | (not in original spec — operator UI feedback) | Tight single-row table layout, segmented Primary/Participant toggle, email-load fix, one-button "Set primary user" with confirm-on-replace | Shipped (#2401) + workspace-token fix (#2404) |
| D.1 | (not in original spec — D.2 prerequisite) | TurnObserver enriches turn records with `user_id` from senderRegistry; replaces the legacy sessionKey-only path | Shipped (#2402) |
| D.2 | (not in original spec — operator request) | Per-user activity aggregation + "Last seen" column on the Users page | Shipped (#2403) |
| E.1 | (not in original spec — operator UI feedback) | "Pod user" label renamed to "Person ID" across the admin UI; wire field stays `pod_user` | Shipped (this commit) |

## Implementation deviations from the original spec

### 1. Sender extraction: senderRegistry, not sessionKey parsing

**Spec said** (§9): "the daemon checks the *requesting identity's* role" — implying plugin tools resolve identity from `ctx`. The original plan assumed parsing the sessionKey's trailing int.

**Reality**: For Telegram DMs that works (`chat_id == user_id`). For groups, the trailing int is the negative group `chat_id`, not the sender's `user_id`. C.2 shipped DM-only with that limitation. C.3 discovered that OC's `PluginHookBeforeAgentRunEvent` carries a `senderId` field populated by the channel layer — the right source for groups too.

The fix is the `senderRegistry` module (`packages/plugin/src/util/senderRegistry.ts`): the `before_agent_run` hook captures `event.senderId` keyed on `ctx.runId`, and tools read it back via `getSender(runId)` when they execute. Module-level Map, 1024-entry bound, 5-minute TTL. Both Path B tools (C.2) and the role injector (C.4) use it.

**Why this matters**: future guard — if a new tool needs the sender, use `getSender(runId)`. Don't re-parse sessionKey for group surfaces.

### 2. Overlay file permissions: explicit chmod 644

**Spec said** (§7): "evolve-user-owned, atomic temp-file + rename writes." Implicit assumption: the file is read by evolve-side processes only.

**Reality**: C.4's TS role resolver reads `{shared_dir}/rosters/{bot_id}.json` directly from the bot's gateway process (atlas user, etc.). `tempfile.mkstemp` creates the file with mode 600 — bot users can't read it. Caught at C.4 deploy time when the speaker-context block would have silently resolved every speaker as `participant`.

The fix is an explicit `os.chmod(p, 0o644)` after `os.replace` in `roster_overlay.save_overlay`. No secret in the file; mode 644 matches the existing OC `allowFrom.json` which bot processes already read.

### 3. Channel token routing: workspace-scoped (per-bot), not pod-wide

**Spec said**: nothing — the original design didn't anticipate cross-workspace bots.

**Reality**: Slack tokens are workspace-scoped. `name_resolver._channel_token` originally returned the FIRST bot's Slack token in iteration order. For a pod with bots in different workspaces, the wrong token would be used and the call would return `user_not_found`. One bot's roster showed every user as `[unknown]` until the fix.

The fix threads `prefer_bot_id` through `resolve()` → `_channel_token()` so each bot's roster uses its own workspace's token. Telegram and Discord tokens are also workspace-scoped but the deployed pod had only one bot per channel for those — fix protects future expansion silently.

**Why this matters**: future guard — any per-bot channel API call needs to use that bot's token, not whichever one comes first.

## Operator-driven additions

### Phase D.1 — turn record user_id enrichment

Before D.1, `{shared_dir}/<bot>/turns/turns-<date>.jsonl` records had `user_id: null` for most turns (the legacy TurnObserver resolver only filled Telegram DMs). D.1 adds the `senderRegistry.getSender(runId).senderId` fallback so the same hook that powers C.3's group routing also enriches the turn log for cost-attribution / activity / audit. Prerequisite for D.2.

### Phase D.2 — per-user activity column

Operator asked for "last active" visibility on the Users page. `evolve_admin/user_activity.py` walks turn-rollup JSONLs (30-day window, 7-day rolling count) and joins per-`(channel, user_id)` into each approved-entry row of the GET response. UI renders compact relative time with optional turn count badge.

### Phase E.1 — "Pod user" → "Person ID"

Operator feedback after C.5 deploy: the "Pod user (optional)" field on the per-bot card was overloaded with adjacent vocabulary (Pod admins, pod-admin-user account, etc.) and its purpose wasn't conveyed by the name. Renamed to "Person ID" across the admin UI; wire field stays `pod_user` for backward compat. Glossary surface kept the wire-name mapping for debugging.

## Open follow-ups

- **Phase D.3 — per-user activity deep-dive view**: D.2 ships the column; D.3 would extend `aggregate()` with `turns_30d` / `cost_30d` / `sessions_30d` / daily buckets and either enrich the tooltip with a sparkline (drop-dead minimal) or add a slide-in drawer (mirror of the existing #turndetail-drawer pattern).
- **Per-app capability declarations**: Phase B's "apps declare `provided_capabilities` at install time, role bindings tighten/loosen per app" is half-done: schema fields exist (B.1) but no app declares them. Migrating atlas's `archive` / `notes` apps is the next concrete step.
- **`list_admitted_users` plugin tool**: Path B works end-to-end via senderRegistry, but the operator still has to know the target user's numeric stable_id to use it from chat ("block 987654321"). A small read-only tool would let the LLM resolve `"alice"` → `123456789` from the bot's own roster.
- **Setup-wizard + console-side prose sweep**: the `pod_user` field is renamed to "Person ID" in the admin UI (E.1) but the same term may appear in `setup_wizard.py` prompts and signal-store message templates. A sweep would close the gap.
