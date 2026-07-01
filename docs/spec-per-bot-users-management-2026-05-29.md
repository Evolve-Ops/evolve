# Users Page — Spec

**Author:** Pod-Admin (via Claude session 2026-05-29)
**Status:** Draft — awaiting approval
**Tracking issue:** TBD

## Motivation

Two problems, one page solves both.

**Problem A — pairing requires a terminal.** OpenClaw's per-bot user-pairing flow today:

1. A user messages the bot from Telegram/Slack/Discord with `/start`.
2. The bot replies with a pairing code (`SY5FYGMT`) and tells them to ask the bot owner to run `openclaw pairing approve telegram SY5FYGMT`.
3. The bot owner SSHes into the pod, `sudo -u <bot>` to the bot account, runs the OC CLI.

This is a **Plex test** failure (see [feedback_design_constraint_mildly_tech_capable.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_design_constraint_mildly_tech_capable.md)) — the admin has to drop into a terminal to do something every chat-channel admin product does in-app. The data and operations belong in the admin UI. Worse, the admin UI **already knows** who the pod admins are. When an admin's own `/start` produces a pairing request, it should auto-approve.

**Problem B — identity content is mis-shelved.** Settings → Identity currently hosts pod admins, self-claim passphrases, and per-bot owners. None of these are "Settings" in the configuration-knob sense — they're answers to "who can use this pod, and what can they do." That's a Users page, not an Identity tab. The Identity content is also currently the only per-bot interface that *isn't* a tile-rail pattern, so it's IA-inconsistent with Cost Optimization, Capabilities, etc.

Combined fix: a new **Users** page in the Settings section that absorbs Settings → Identity and adds the paired-user management. Pod-wide identity sits at the top; a bot-tile rail (matching the established pattern from Cost Optimization / Capabilities) selects a bot, and the panel below shows owner, passphrase override, approved users by channel, and pending requests — all on one screen per bot.

## Goals

1. **Create a Users page** in the Settings sidebar section, with pod-wide identity at top and a bot-tile rail below.
2. **Migrate Settings → Identity content** (Pod admins, Self-claim passphrases, Per-bot owners) into the Users page; Settings tab loses the Identity sub-tab.
3. **Surface paired users per bot per channel** within each bot's panel — approved users (the OC allowlist) and pending pairing requests.
4. **One-click approve/reject** for pending requests, **one-click disconnect** for already-approved users.
5. **Auto-approve known pod admins** so claimed identities don't require a code round-trip.
6. **Enrich with display names** where the channel API can give them to us (defer to phase 2).

## Non-goals (v1)

- Per-user audit log of approvals/revocations. (Phase 4.)
- Cross-bot user matrix ("show me every bot Stephanie has access to"). (Phase 3.)
- Editing channel-side credentials (the bot's own provider token) — out of scope; that's `auth-profiles.json` and belongs to the Plugins → Messaging flow.
- Multi-account-per-provider support. OC schemas show `accountId: "default"` everywhere on this pod — defer multi-account dimension until OC exposes it as a first-class concept.
- New nav section. The Users page lives **inside** the existing Settings sidebar group ("Getting Started / Settings / Users / Help"); no new section.

## Data model

All pairing state lives in `<bot_home>/.openclaw/credentials/`. The admin server (`evolve` user) has macOS ACL read on `/Users/<bot>/.openclaw/` via `set_evolve_read_acl(bot_id)` in `deploy.py` per [CLAUDE.md](CLAUDE.md). Direct `Path.read_text()` works for reads; writes go through `/tmp` staging + `sudo /bin/cp` because these files are bot-owned.

### Pending pairing requests
**File:** `<bot_home>/.openclaw/credentials/<provider>-pairing.json`
**Providers seen on the mini:** `telegram`, `slack`, `whatsapp` (presumed `discord` similar).

```json
{
  "version": 1,
  "requests": [
    {
      "id": "123456789",
      "code": "SY5FYGMT",
      "createdAt": "2026-05-30T00:33:01.539Z",
      "lastSeenAt": "2026-05-30T00:33:01.539Z",
      "meta": {
        "username": "example_user",
        "firstName": "Pod-Admin",
        "lastName": "Example",
        "accountId": "default"
      }
    }
  ]
}
```

`meta` shape is provider-specific. Slack pairing requests carry `{name, accountId}`; Telegram carries `{username, firstName, lastName, accountId}`. Treat `meta` as an opaque dict; render whatever fields exist.

### Approved users (the allowlist)
**File:** `<bot_home>/.openclaw/credentials/<provider>-default-allowFrom.json`

```json
{
  "version": 1,
  "allowFrom": ["U0PLKKXV0", "U9ZL3JYR3", "U4T907NV6"]
}
```

Just a list of channel-native IDs. No metadata — OC stores the names elsewhere (or queries the channel API on demand). For v1 we render the bare IDs; enrichment is phase 2.

### Sources of truth that already exist
- **Pod admins:** `network.json` identity claims (rendered at Settings → Identity → "Pod admins (messaging)"). Schema: `{channel, external_id, pod_user, display_name}` per claim.
- **Per-bot primary owner:** also in `network.json`, rendered at Settings → Identity → "Per-bot owners".

The Users page reads both for cross-referencing — an approved ID that matches a pod-admin claim gets labeled "Pod admin"; an ID that matches the bot's primary owner gets labeled "Owner".

## IA placement

**New Users page in the Settings sidebar group.** Sidebar after this change:

```
SETTINGS
  ▸ Getting Started
  ▸ Settings        ← Network + Bot only (Identity sub-tab removed)
  ▸ Users           ← NEW
  ▸ Help
```

The Users page is a single scroll, organized as:

```
USERS                                          [pending: 2 across all bots]
─────────────────────────────────────────────────────────────────────────────

POD-WIDE                                       (migrated from Settings → Identity)
  • Pod admins (messaging)                     — add/remove pod-wide admin claims
  • Self-claim passphrases                     — admin + primary defaults

PER BOT
  ┌─────────┬─────────┬─────────┬─────────┬─────────┬─────────┐
  │ team-bot-a  ●1 │ admin-bot   │ team-bot-c   │ evolve  │ atlas ●1│ personal-bot   │   ← bot-tile rail
  └─────────┴─────────┴─────────┴─────────┴─────────┴─────────┘   (mirrors cm-bot-tiles)

  ▼ ATLAS (multi-user)
  ─────────────────────────
  Owner / primary user                         — moved from "Per-bot owners"
  Self-claim passphrase override               — moved from Identity
  Users by channel                             — NEW
    Telegram
      Approved (2)
        Pod-Admin  [Owner] [Pod admin]   123456789         [Disconnect]
        [unknown]                          9876543210         [Disconnect]
      Pending (1)
        Sam (@sam_t)                       5555555555  AB12CD34
          Created 2 min ago                            [Approve] [Reject]
    Slack
      ...
```

The `●1` chip on a bot tile counts pending requests; the page-header chip aggregates across all bots so the admin sees pairing demand without clicking through.

**Settings tab after the migration:** Network + Bot. The Identity sub-tab disappears. Any deep-links to `#identity` redirect to `/users` (with anchor `#pod-admins`, `#passphrases`, or `#bot-<id>` where appropriate).

**Why this shape:**
- The bot-tile rail matches Cost Optimization (`cost-bot-tiles`) and Capabilities/Cost Measures (`cm-bot-tiles`) — established convention, no new pattern to learn.
- Pod-wide identity stays visible at top so the admin lands on the page and immediately sees the global view; selecting a bot scrolls/swaps the lower panel.
- All identity-and-users questions answer themselves on one page: who are the pod admins, who owns team-bot-a, who's paired to atlas-Telegram, what's pending on team-bot-c-Slack.

**Not separating into a new sidebar section.** Users belongs alongside Settings/Help — it's configuration of "who", not a daily operational page. Per [project_evolve_three_bucket_ia.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_evolve_three_bucket_ia.md), the current IA is Operate / Improve / Settings; Users fits the third bucket.

## API endpoints

All under `/api/admin/bots/<bot_id>/users`. Routes live in `packages/admin/evolve_admin/web/routes_bot_users.py` (new module).

### `GET /api/admin/bots/<bot_id>/users`
Returns per-channel paired-user state for the bot.

```json
{
  "bot_id": "atlas",
  "by_channel": {
    "telegram": {
      "supported": true,
      "approved": [
        {
          "id": "123456789",
          "display_name": "Pod-Admin",
          "labels": ["owner", "pod_admin"],
          "source": "network_json"
        },
        {
          "id": "9876543210",
          "display_name": null,
          "labels": [],
          "source": "allowlist_only"
        }
      ],
      "pending": [
        {
          "id": "5555555555",
          "code": "AB12CD34",
          "createdAt": "2026-05-29T17:33:01Z",
          "lastSeenAt": "2026-05-29T17:33:01Z",
          "meta": {"firstName": "Sam", "username": "sam_t"},
          "auto_approve_eligible": false,
          "auto_approve_reason": null
        }
      ]
    },
    "slack": { ... },
    "discord": { ... }
  }
}
```

`labels` is a free-form list rendered as chips; current values: `owner`, `pod_admin`, `you` (when the ID matches the current viewing admin). Open-ended so future labels (e.g. `auto_approved`) can be added without an API break.

`source` is the provenance of the display_name — `network_json` (resolved from a pod admin / primary owner claim), `channel_api` (phase 2 — Slack `users.info` etc.), or `allowlist_only` (no name available, just the ID).

`auto_approve_eligible` is `true` when the pending request's `id` matches a pod-admin claim for the same channel. `auto_approve_reason` carries a short human string ("known pod admin").

`supported` indicates whether OC supports pairing for this provider on this bot (presence of either pairing.json or allowFrom.json file). Single-user bots will have `supported: false` for channels they're not configured on.

### `POST /api/admin/bots/<bot_id>/users/approve`
Body: `{channel, id, code}` (code is the pending request's code — required as a CSRF-ish nonce so a stale UI can't approve an unrelated request).
Action: add `id` to `<provider>-default-allowFrom.json`, remove the matching `requests[]` entry from `<provider>-pairing.json`.
Returns the updated channel block (same shape as the GET response's `by_channel.<channel>`).

### `POST /api/admin/bots/<bot_id>/users/revoke`
Body: `{channel, id}`.
Action: remove `id` from `<provider>-default-allowFrom.json`.

### `POST /api/admin/bots/<bot_id>/users/reject`
Body: `{channel, id, code}`.
Action: remove the matching `requests[]` entry from `<provider>-pairing.json` without adding to allowFrom.

### Write semantics
Both pairing.json and allowFrom.json are bot-owned. Write via the **/tmp + sudo /bin/cp** pattern documented in CLAUDE.md:

```python
fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
with os.fdopen(fd, "w") as f:
    json.dump(updated, f, indent=2)
subprocess.run(["sudo", "/bin/cp", tmp, dest_path], check=True)
subprocess.run(["sudo", "/bin/chmod", "600", dest_path], check=True)  # match OC's mode
os.unlink(tmp)
```

**No gateway kickstart needed** — OC's gateway watches `allowFrom.json` for changes (this is how the existing `openclaw pairing approve` CLI works without restart). To confirm: file the OC upstream issue (below) asking them to document the file-watching contract.

## Auto-approval rule

When the admin server scans pairing.json files and finds a `requests[]` entry whose `id` matches a known pod admin's `external_id` for that channel, auto-approve it.

**Implementation:** add a `pairing_auto_approver` periodic task to the admin server (similar to existing periodic tasks like the signals retention sweep). Polls every 30s:

```python
for bot in network.bots:
    for provider in PROVIDERS:
        pairing_path = bot_home(bot) / ".openclaw/credentials/" / f"{provider}-pairing.json"
        if not pairing_path.exists(): continue
        for req in load(pairing_path)["requests"]:
            if req["id"] in pod_admin_ids_for(provider):
                approve(bot, provider, req["id"], req["code"], reason="known_pod_admin")
                emit_signal(...)  # "auto-approved Pod-Admin's atlas-telegram pairing"
```

30s is a good middle ground — feels instant from the user's perspective ("I sent /start, it just worked") without burning cycles. Could go to file-watching (`fsevents`) later if needed.

**Audit:** every auto-approval emits a Signal to the [signal store](docs/spec-alerts-signal-store-2026-05-07.md) so the admin sees it on the Alerts page. Signature: `pairing_auto_approved:<bot>:<channel>:<id>`. Auto-resolves immediately (informational). One-line: "Auto-approved pairing — atlas / telegram / Pod-Admin (pod admin)".

**Manual rejection wins:** if the admin explicitly rejects a pending request and the same ID re-pairs immediately, the auto-approver should NOT re-approve for some window (e.g. 1 hour). Store a `<shared_dir>/pairing/rejected.json` index — `{provider:id → rejected_until_ms}` — that the auto-approver checks before acting.

## Display-name enrichment (phase 2)

For approved IDs that don't match any pod-admin / primary-owner claim, fetch display names from the channel API using the bot's own provider token:

- **Telegram:** `bot.getChat(user_id)` → `{first_name, last_name, username}`. No email available.
- **Slack:** `users.info(user)` → `{real_name, display_name, email, profile.image_72}`. Email requires `users:read.email` scope — Evolve already provisions this for team-bot-a but not all bots; spec needs a fall-back path.
- **Discord:** `GET /users/<id>` → `{username, global_name, avatar}`. Requires bot to share a guild with the user.

Cache to `<shared_dir>/identity_cache/<channel>/<id>.json` with a 7-day TTL. Refresh on cache-miss only — never block the UI on a channel API call. The GET endpoint returns whatever's cached and kicks an async refresh.

Privacy note (per [feedback_user_observation_optout.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_user_observation_optout.md)): name enrichment is unambiguously needed for the admin UI ("who is U0PLKKXV0?" is unanswerable without it). No opt-out is incoherent here — the admin is asking. Email is more sensitive — defer that until we have a clear need; show name + ID by default, gate email behind a "show email" expander.

## Migration from Settings → Identity

The current Settings → Identity tab (rendered around [index.html:4596](packages/admin/evolve_admin/web/index.html:4596) under `page-settings`) has four sections. Disposition:

| Identity section today | Lands as | Notes |
|---|---|---|
| Pod context (deploy machine, unix admin, admin URL) | Settings → Network tab (or merged into Settings → Bot) | Pod context is configuration, not "who"; this isn't user content. Move to Settings, don't bring it to Users. |
| Pod admins (messaging) | Users → POD-WIDE section | Same component, same data, same API (`GET /api/admin/identity`). Rendered at the top of the new page. |
| Self-claim passphrases | Users → POD-WIDE section | Same. |
| Per-bot owners | Users → per-bot panel, top of the bot view | Each bot's "Owner / primary user" block is what used to be that row. The "Discover from history" affordance comes along. |

The existing `routes_identity.py` API (`GET /api/admin/identity`, `POST /api/admin/identity/*`) stays as-is in v1 — the new Users page is a re-shell, not a re-plumbing. The new paired-user endpoints (`/api/admin/bots/<id>/users`) are additive. A follow-on PR can rename `routes_identity.py` → `routes_users.py` once the old Identity HTML is fully removed.

**Backward-compat redirects:** the old `#identity` hash (and `data-page="settings"` + Identity tab activation) redirect to `/users` with an anchor for the relevant sub-section.

## Wireframes

### 1. Page on first load — multi-user bot selected

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│  Users                                                          ● 2 pending across all bots │
│  Pod admins, passphrases, and per-bot user management.                                       │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                              │
│  POD-WIDE                                                                                    │
│  ─────────                                                                                   │
│                                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────────────────┐  │
│  │  POD ADMINS (MESSAGING)                                                              │  │
│  │  Anyone listed here gets admin privileges on every multi-user bot's chat surface.    │  │
│  │  Independent from the Unix admin in Settings → Network.                              │  │
│  │                                                                                       │  │
│  │  telegram:  Pod-Admin  @example_user  123456789                          [Remove]       │  │
│  │  slack:     Stephanie    @steph    U04R26D2HJ6                        [Remove]       │  │
│  │                                                                                       │  │
│  │  Add admin:                                                                          │  │
│  │  ┌────────┐  ┌─────────────────┐  ┌──────────┐  ┌──────────┐                       │  │
│  │  │Telegram│  │ External ID     │  │Pod user  │  │Display   │       [Add admin]      │  │
│  │  └────────┘  └─────────────────┘  └──────────┘  └──────────┘                       │  │
│  └──────────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────────────────┐  │
│  │  SELF-CLAIM PASSPHRASES                                                              │  │
│  │  Out-of-band claim words. Admin passphrase → pod-wide admin; primary passphrase →    │  │
│  │  per-bot owner on whichever bot the user sends it to.                                │  │
│  │                                                                                       │  │
│  │  Admin passphrase:    charles   [Edit]                                               │  │
│  │  Primary passphrase:  darwin    [Edit]                                               │  │
│  │                                                                                       │  │
│  │  Defaults: charles (admin), darwin (primary). Per-bot overrides below.               │  │
│  └──────────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                              │
│  PER BOT                                                                                     │
│  ────────                                                                                    │
│                                                                                              │
│  ┌─────────┬─────────┬─────────┬─────────┬─────────┬─────────┬─────────┐                  │
│  │  team-bot-a    │ admin-bot   │  team-bot-c  │ evolve  │ atlas ●1│ personal-bot   │  team-bot-b   │                  │
│  │ ●1      │         │         │         │         │         │         │                  │
│  │ multi   │ single  │ multi   │ single  │ multi   │ single  │ single  │                  │
│  └─────────┴─────────┴─────────┴─────────┴─────────┴─────────┴─────────┘                  │
│            ▲                                                                                 │
│         active                                                                               │
│                                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────────────────┐  │
│  │  ADMIN-BOT  (single-user bot)                                                            │  │
│  │  ─────                                                                                │  │
│  │                                                                                       │  │
│  │  Owner / primary user                                                                │  │
│  │  ──────────────────                                                                  │  │
│  │  telegram:  Pod-Admin  123456789                              [Discover from hist] │  │
│  │  ┌────────┐ ┌─────────────────┐ ┌──────────┐ ┌──────────┐                          │  │
│  │  │Telegram│ │ External ID     │ │Pod user  │ │Display   │   [☐ overwrite]  [Set]   │  │
│  │  └────────┘ └─────────────────┘ └──────────┘ └──────────┘                          │  │
│  │                                                                                       │  │
│  │  Self-claim passphrase:  inherits pod default (darwin)  [Edit]                       │  │
│  │                                                                                       │  │
│  │  Users by channel                                                                    │  │
│  │  ────────────────                                                                    │  │
│  │  Telegram                                                                            │  │
│  │    Approved (1)                                                                      │  │
│  │      Pod-Admin  [Owner] [Pod admin] [You]   123456789      [Disconnect]           │  │
│  │    Pending — none                                                                    │  │
│  │                                                                                       │  │
│  │  Slack — channel not configured                              [Configure in Plugins]  │  │
│  │  Discord — channel not configured                            [Configure in Plugins]  │  │
│  └──────────────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2. Multi-user bot with pending requests

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  TEAM-BOT-A  (multi-user bot)                                                               │
│  ───                                                                                  │
│                                                                                       │
│  Owner / primary user                                                                │
│  ──────────────────                                                                  │
│  slack:  Stephanie  U0PLKKXV0                                                        │
│  [Edit owner ▾]                                                                      │
│                                                                                       │
│  Self-claim passphrase:  override = "team-bot-a-primary"  [Edit]                            │
│                                                                                       │
│  Users by channel                                                                    │
│  ────────────────                                                                    │
│                                                                                       │
│  ┌─ Slack ─────────────────────────────────────────────────────────────────────┐    │
│  │                                                                              │    │
│  │  Approved (10)                                       [Hide IDs] [Sort: A–Z] │    │
│  │  ──────────────                                                              │    │
│  │  Stephanie       [Owner]            U0PLKKXV0          [Disconnect]         │    │
│  │  Marcus                              U9ZL3JYR3          [Disconnect]         │    │
│  │  Pod-Admin     [Pod admin] [You]   U04R26D2HJ6        [Disconnect]         │    │
│  │  [unknown]                           U4T907NV6          [Disconnect]         │    │
│  │  [unknown]                           U4VBB85PY          [Disconnect]         │    │
│  │  …5 more                             [Show all]                              │    │
│  │                                                                              │    │
│  │  Pending (1)                                                                 │    │
│  │  ───────────                                                                 │    │
│  │  Sam (@sam_t)                        U99XX1234   code AB12CD34              │    │
│  │    Created 2 min ago                            [Approve]  [Reject]         │    │
│  │                                                                              │    │
│  └──────────────────────────────────────────────────────────────────────────────┘    │
│                                                                                       │
│  ┌─ Telegram ──────────────────────────────────────────────────────────────────┐    │
│  │                                                                              │    │
│  │  Approved (3)                                                                │    │
│  │  ──────────────                                                              │    │
│  │  Pod-Admin     [Pod admin] [You]   123456789          [Disconnect]         │    │
│  │  [unknown]                           987654321          [Disconnect]         │    │
│  │  [unknown]                           555555555          [Disconnect]         │    │
│  │                                                                              │    │
│  │  Pending — none                                                              │    │
│  │                                                                              │    │
│  └──────────────────────────────────────────────────────────────────────────────┘    │
│                                                                                       │
│  WhatsApp — no users paired                                  [Collapsed ▸]           │
│                                                                                       │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 3. Pending request that's about to auto-approve (admin's own /start)

```
┌─ Telegram ──────────────────────────────────────────────────────────────────┐
│                                                                              │
│  Approved (0)                                                                │
│  ─────────────                                                               │
│  No users approved yet.                                                      │
│                                                                              │
│  Pending (1)                                                                 │
│  ───────────                                                                 │
│  Pod-Admin  [Pod admin]            123456789   code SY5FYGMT              │
│    Created 4 sec ago                                                         │
│    ⟳ Auto-approving (known pod admin)…                                       │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

After the next sweep (~30s, often less because of live refresh), the row moves to Approved. An informational Signal lands on the Alerts page: "Auto-approved pairing — atlas / telegram / Pod-Admin (pod admin)".

### 4. Empty bot — no channels configured

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  PERSONAL-BOT  (single-user bot)                                                            │
│  ─────                                                                                │
│                                                                                       │
│  Owner / primary user — not set                                  [Set owner ▾]       │
│  Self-claim passphrase: inherits pod default                                          │
│                                                                                       │
│  Users by channel                                                                    │
│  ────────────────                                                                    │
│                                                                                       │
│  No channels configured for this bot.                                                │
│  Configure a channel in Plugins → Messaging first; users will appear here once       │
│  they pair via /start.                                                               │
│                                                                                       │
│                                              [Configure messaging in Plugins →]      │
│                                                                                       │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 5. Confirm dialog on Disconnect

```
┌─────────────────────────────────────────────────────────────────┐
│  Disconnect Stephanie from team-bot-a?                                  │
│  ─────────────────────────────────────                           │
│                                                                  │
│  Stephanie (Slack U0PLKKXV0) will lose access to team-bot-a on Slack.   │
│  She can rejoin any time by sending /start to the bot — you'll   │
│  see the request on this page.                                   │
│                                                                  │
│  ⚠ Stephanie is the bot's Owner / primary user. Removing her     │
│    access will leave team-bot-a without a primary user until you set    │
│    a new one.                                                    │
│                                                                  │
│                                          [Cancel]   [Disconnect] │
└─────────────────────────────────────────────────────────────────┘
```

The warning bar only renders if the target is the primary user or the only pod admin on that channel — otherwise it's a vanilla two-line confirm.

### 6. Confirm dialog on Reject (pending request)

```
┌─────────────────────────────────────────────────────────────────┐
│  Reject Sam's pairing request?                                   │
│  ──────────────────────────────                                  │
│                                                                  │
│  Sam (Slack U99XX1234) won't be paired with team-bot-a. Their pairing   │
│  code AB12CD34 will be invalidated.                              │
│                                                                  │
│  ☐ Don't auto-approve this ID for the next hour                  │
│    (default: on — prevents accidental re-approval if they retry  │
│     and they happen to match a pod-admin claim)                  │
│                                                                  │
│                                            [Cancel]   [Reject]   │
└─────────────────────────────────────────────────────────────────┘
```

The cool-off checkbox surfaces the "rejected.json" rule from the auto-approver section so the admin sees it's there.

## UI details

**Single-user vs multi-user bots.** Multi-user bots show every supported channel in the per-bot panel. Single-user bots show only channels with users or pending requests; "Channel X — no users paired" is collapsed by default.

**Auto-approval display.** When a pending request is auto-approved between page loads, the row simply isn't there on next render — but the Alerts page surfaces the Signal. On the live page, if the GET response indicates `auto_approve_eligible: true` for a request, render a green "Auto-approving…" indicator in place of Approve/Reject buttons (the user shouldn't have to click).

**Pending counts.** Bot tiles show a `●N` chip when any provider has pending requests for that bot. The page header shows the global sum across all bots. Both come straight from the per-bot GET responses; no separate count endpoint.

**Empty states.**
- No pod admins claimed → POD-WIDE section shows the "Add admin" form with the same explanatory copy that's on Settings → Identity today.
- Bot with no paired users at all → per-bot panel says "No users paired yet. To pair the first user, have them send `/start` to the bot." with a link to the Channels flow if the bot has no channels configured.
- Bot with no channels configured → "Pair a channel first" with link to Plugins → Messaging.

## Edge cases

| Case | Behavior |
|---|---|
| Same ID in pending and approved | Render as approved; auto-prune the stale pending request. |
| Multiple pending requests for the same ID (stale codes) | Dedupe by ID showing the most recent `lastSeenAt`. Approving cleans up all matches. |
| Provider's pairing.json missing entirely | Treat as `{requests: []}` — channel not yet active for this bot. |
| allowFrom.json missing | Treat as `{allowFrom: []}` — no approved users. Don't fail; render empty. |
| Write race with OC processing an approve at the same time | last-writer-wins is fine: `allowFrom` is a set, duplicate writes are idempotent. |
| Admin disconnects themselves | Allow with a confirm dialog ("You'll lose access to <bot> in this channel"). They can re-pair via /start. |
| Bot not yet deployed (no `.openclaw/credentials/` dir) | `supported: false` for every channel; the Users section says "Pair a channel first" with a link to the Channels flow. |

## Failure modes / schema drift

This spec is reading and writing OC's runtime files directly. If OC v2026.6 renames `allowFrom` or moves files around, we break.

**Mitigations:**
- Defensive reads: if the schema doesn't match expected shape, fall back to `supported: false` rather than crash.
- **File an OC upstream issue** requesting either:
  - A stable CLI surface: `openclaw pairing list --json <provider>` / `openclaw pairing approve <provider> <code>` / `openclaw pairing revoke <provider> <id>` — we shell out instead of touching files.
  - Or, even better, an MCP tool that does the same.
- Pin to a known-good OC version in `security/upstream-version` floor; bump deliberately and re-test this surface every OC upgrade.

## Phased delivery

**Phase 1 — Page + migration + paired-user surface (SHIPPED).** Single PR landing: the Users page in the Settings sidebar group with bot-tile rail, migration of Pod admins / Self-claim passphrases / Per-bot owners from Settings → Identity, hash-redirect from `#identity`, the per-bot "Users by channel" block, approve/reject/disconnect, and **inline** auto-approval on GET (any pending request whose ID matches a pod-admin claim is approved during the listing fetch so a freshly-paired admin sees their own `/start` already-approved). Bare channel IDs where no claim resolves them. Pending chips on tiles + page header.

**Phase 1.1 — Ambient auto-approval sweep.** Small follow-up: a 60s launchd job (`pairing_auto_approver`) running the same sweep across all bots when no admin is actively watching the Users page. Plus the `rejected.json` cool-off index so the background sweep doesn't re-approve IDs the admin just rejected. (Inline sweep already covers the admin-is-watching case.) Signal emitted to the alert store for each auto-approval.

**Phase 2 — Name enrichment (SHIPPED).** Channel-API lookups for IDs that lack a name from any local source (resolved_names cache, admins.names map, primary owner record, identity_cache).

Implemented in `routes_bot_users._enrich_unknown_names`: identifies approved entries with `display_name=None` on channels `name_resolver.SUPPORTED_CHANNELS` supports (`telegram`, `slack`, `discord`), fires concurrent `name_resolver.resolve(use_cache=False)` calls via a `ThreadPoolExecutor(max_workers=8)`, capped at 5s wall-clock so the page-load budget is bounded; stragglers complete in the background and seed the cache for the next GET. Successful resolutions mutate the response entry in place (`display_name` + `source: "channel_api"`) and persist into `pod.admins.resolved_names` via `name_resolver.write_cache_entry`.

Coverage per channel:
- **Telegram** — `getChat` via token-in-URL-path auth.
- **Slack** — `users.info` via `Authorization: Bearer <token>` header (the legacy `?token=` form is rejected by modern apps as `invalid_auth`).
- **Discord** — `GET /users/<id>` via `Authorization: Bot <token>` header. Prefers `global_name` (Discord display name) over `username` (handle).

**Email surface (Slack only).** `_resolve_slack` extracts `profile.email` when the bot's app has `users:read.email` scope; the value is persisted into `pod.admins.resolved_names[<channel>:<id>].email` and surfaced in the GET response as an optional `email` field. The Users page renders a page-level `Show emails` checkbox in the per-bot panel header that toggles the email column on/off; preference persists to `localStorage`. Off by default so the page stays compact. Telegram and Discord don't expose email via the bot API path, so they have no email field.

**TTL (7 days).** `name_resolver.cached_name(network, channel, ext_id, max_age_days=7)` treats entries with `cached_at` older than 7 days as a miss → next `resolve()` re-fetches. Unparseable / future-dated `cached_at` is treated as fresh (defensive). `max_age_days=0` disables the check for callers that explicitly want whatever's cached.

Phase 2 also closes the Phase-1 cosmetic gap from the 2026-05-29 mini test: when an ID matches a pod-admin claim but `pod.admins.names[<ext_id>]` isn't populated, `_resolve_display_name` falls through to `resolved_names` (Phase 1's chain) and the enrichment step backfills that cache for next time.

**Not shipped:** WhatsApp (needs `name_resolver` coverage first — Twilio doesn't expose names easily). Email for non-Slack channels (Telegram and Discord don't expose email via bot API path; an OAuth flow is the only way and it's the user's choice, not the bot's). User-side opt-out of name enrichment (the operator is the audience and names are already disclosed to the channel; revisit if a real user complains).

**Phase 3 — Cross-bot user matrix.** A "Who has access to what?" sub-view on the Users page (collapsible, above the bot rail). Rows = users, columns = bots, cells = channels they're paired on.

**Phase 4 — Audit log.** "When was each user approved, by whom, manual vs auto" — shown as a per-user expander in the per-bot panel and as a Signal-store query backing it.

## Open questions

1. **Where does "Pod context" go after Identity is gone?** The deploy-machine / unix-admin / admin-URL block at the top of today's Identity tab is system-level state, not user-level. Tentatively merge into Settings → Bot tab (or keep on Settings → Network). Confirm in 1a's PR.

2. **Does OC's gateway actually watch allowFrom.json for changes?** Assumed yes based on the CLI behavior (`openclaw pairing approve` takes effect immediately). Worth confirming before relying on it — if not, we kickstart `ai.openclaw.gateway.<bot>` after every write, which is fine but slower.

3. **What about per-account allowlists?** Schemas show `accountId: "default"` everywhere. If OC supports `accountId: "secondary"` or similar, we need to scope allowlists per account. Defer until OC surfaces this as a config option.

4. **Should the auto-approver also handle the per-bot primary owner (not just pod admins)?** Probably yes — if `network.json` says "Stephanie is the primary user of team-bot-a" and Stephanie's Slack ID `/start`s team-bot-a, that should auto-approve too. Worth a flag in case the admin wants the friction for non-admin owners.

5. **How does this interact with the conversational bot-creation wizard ([project_conversational_bot_creation_wizard.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_conversational_bot_creation_wizard.md))?** A wizard that creates a bot probably pre-seeds the allowlist with the wizard-runner's IDs across channels. Should be one of the wizard's final steps — call it out in the wizard spec.

## References

- [CLAUDE.md](CLAUDE.md) — read/write patterns for bot-owned files
- [project_alerts_signal_store.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_alerts_signal_store.md) — Signal store for auto-approval audit
- [feedback_design_constraint_mildly_tech_capable.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_design_constraint_mildly_tech_capable.md) — Plex test motivation
- [project_user_types_and_approval.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_user_types_and_approval.md) — three-user-type framing
- [project_pod_bot_integrations.md](/Users/pod-admin/.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_pod_bot_integrations.md) — current channel coverage
