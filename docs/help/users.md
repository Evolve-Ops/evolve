---
title: "Help: Users Page"
slug: users
audience: public
last_reviewed: 2026-06-08
concepts:
  - users
  - pod-admins
  - paired-users
  - pairing-approval
  - self-claim-passphrase
  - per-bot-owner
  - roles
  - capabilities
  - primary-user
  - blocked
  - engagement-surfaces
  - newcomer-mode
  - auto-admit
ui_surface: admin.users
related_specs:
  - docs/spec-per-bot-users-management-2026-05-29.md
  - docs/spec-user-roster-and-roles-2026-06-07.md
---

# Help: Users Page

The Users page (in the **Settings** bucket) is the single place for every "who can use this pod, and what can they do" question — pod admins, self-claim passphrases, per-bot owners, and paired-user management per channel.

It replaces the old Settings → Identity sub-tab and absorbs the pairing-approval flow that previously required SSHing into the bot's account to run `openclaw pairing approve …`. Spec: [docs/spec-per-bot-users-management-2026-05-29.md](../spec-per-bot-users-management-2026-05-29.md).

**Ask evo from chat.** Most user-management actions are reachable through chat: *"approve Sam's pairing request for team-bot-a on Slack"*, *"disconnect U0PLKKXV0 from atlas"*, *"who is the primary user on personal-bot?"*, *"switch team-bot-a to multi-user mode"*. Evo routes through the same Users API the page uses, so the audit trail matches.

---

## Page shape

Top of the page: **pod-wide identity** (pod admins, self-claim passphrases) in cards. Below that: a **bot-tile rail** matching the Cost Optimization / Capabilities pattern — pick a bot to expand its panel below. Each bot's panel shows owner, passphrase override, and Users by channel.

A page header chip counts pending pairing requests across the whole pod. Each bot tile shows a `●N` chip if that bot has any pending requests. The sidebar's Users badge mirrors the page header so demand is visible without opening the page.

---

## Pod-wide identity

### Pod Admins (messaging)

Anyone listed here gets admin privileges on every multi-user bot's chat surface. Pod admins are independent of the Unix admin account in Settings → Network — this is the messaging-side admin, the person who can run admin-tier `evo` keywords from any bot.

To add: pick channel (Telegram / Slack / Discord / WhatsApp), enter the external ID, optionally label with a pod user and display name. The `×` next to an ID chip removes that channel's claim; the `×` next to a pod user drops every claim for that person.

### Self-claim passphrases

Out-of-band claim words a user can message to a bot to record themselves as admin or primary, without you having to enter their channel ID by hand.

- **Admin passphrase** (default `charles`) — claims pod-wide admin status.
- **Primary passphrase** (default `darwin`) — claims per-bot primary on whichever bot the user messaged.

A user types `evo claim darwin` and they become primary on that bot. `evo claim charles darwin` claims both at once.

Per-bot overrides live in each bot's panel below — useful when you want different words per bot so one bot's owner doesn't accidentally claim primary on a sibling bot.

---

## Per-bot panel

The bot-tile rail picks a bot; the panel below shows everything user-related for it.

### Owner / primary user

The bot's primary user. Two ways to set:

- **Enter their ID here** — pick channel + external ID + optional display name + pod user.
- **Have them self-claim** — they message the bot once with the primary passphrase. The system records them.
- **Discover from history** — a button under the owner block scans recent transcripts and proposes a primary based on conversation patterns; one-click "Use" commits the candidate.

### Self-claim passphrase

Shows the active passphrase for this bot. If unset, the row reads "inherits pod default ({primary passphrase})". Override with a custom word per bot.

### Single-user ↔ multi-user toggle

Each bot's panel header has a mode segmented control. Single-user bots respond only to their primary owner. Multi-user bots accept paired users from the allowlist below.

Flipping the toggle rewrites `openclaw.json` (`multi_user_mode`) and kickstarts the gateway so the change takes effect immediately.

### Users by channel

For each messaging channel the bot is configured on, two sections:

**Approved (N).** The OpenClaw `allowFrom` list — every channel-native ID that can talk to the bot. Each row shows the display name (when known), labels (`[Owner]`, `[Pod admin]`, `[You]`), the raw ID, and a `[Disconnect]` button. Labels come from cross-referencing `network.json` (pod admins, primary owners). The "You" label appears on rows that match the currently-viewing admin's claim for that channel.

**Pending (N).** Pairing requests that the user sent via `/start` and that haven't been approved or rejected yet. Each row shows the user's display name (or username), their ID, the pairing code, when the request was created, and `[Approve]` / `[Reject]` buttons.

Channels with no configuration show a "Channel not configured" stub and link to Plugins → Messaging.

### Disconnect / reject confirmations

`[Disconnect]` prompts a confirm dialog before removing the ID from the allowlist. If the target is the primary user or the only pod admin on that channel, the dialog adds a warning bar.

`[Reject]` on a pending request prompts a dialog with an optional **"Don't auto-approve this ID for the next hour"** checkbox (default on). This populates the `rejected.json` cool-off index so the background sweep doesn't re-approve an ID that coincidentally matches a pod-admin claim.

---

## Auto-approval

When a pending request's channel ID matches a recorded pod admin's `external_id` for the same channel, the system auto-approves it. This is **inline on GET** (the listing endpoint approves on read so a freshly-paired admin sees their own `/start` already approved) and **ambient on a 60-second sweep** (`pairing_auto_approver` launchd job) for the case where no admin is watching the page.

Every auto-approval emits a Signal to the Alerts page. Signature: `pairing_auto_approved:<bot>:<channel>:<id>`. The Signal auto-resolves immediately (informational). One-line summary: *"Auto-approved pairing — atlas / telegram / Pod-Admin Alden (pod admin)."*

If you manually reject a request, the cool-off index blocks re-approval for an hour even if the ID matches a pod-admin claim.

---

## Name enrichment

Approved IDs without a name from any local source (the resolved-names cache, the pod-admin claims map, the primary-owner record, or the identity_cache) get an asynchronous lookup against the channel API using the bot's own provider token.

Coverage:

- **Telegram** — `getChat(user_id)` returns `first_name`, `last_name`, `username`.
- **Slack** — `users.info(user)` returns `real_name`, `display_name`, and (when the bot has the `users:read.email` scope) `profile.email`.
- **Discord** — `GET /users/<id>` returns `username` and `global_name`. The display preference is `global_name` → `username`.

Cache TTL is 7 days. The GET endpoint never blocks the page on a channel API call; lookups beyond a 5-second wall-clock complete in the background and seed the cache for the next render.

### Show emails toggle

Per-bot panels have a **Show emails** checkbox in the header (Slack only, since other channels don't expose email via the bot API path). Off by default so the page stays compact. The preference persists to `localStorage`. Email values come from `profile.email` on Slack `users.info` and require the `users:read.email` scope.

---

## Pod context (the row that used to live here)

The deploy machine / Unix admin / admin URL block that used to be on Settings → Identity has moved to Settings → Network. It's system configuration, not a "who" question.

---

## API surface

All endpoints under `/api/admin/bots/<bot_id>/users`:

- `GET /` — per-channel paired-user state for the bot, with `approved[]`, `pending[]`, `auto_approve_eligible` flags, and `labels` chips.
- `POST /approve` — body `{channel, id, code}`. Adds `id` to the allowlist and removes the pending entry. `code` acts as a nonce so a stale UI can't approve an unrelated request.
- `POST /revoke` — body `{channel, id}`. Removes `id` from the allowlist.
- `POST /reject` — body `{channel, id, code}`. Removes the pending entry without adding to the allowlist; optionally populates the cool-off index.

Routes live in [`packages/admin/evolve_admin/web/routes_bot_users.py`](../../packages/admin/evolve_admin/web/routes_bot_users.py).

---

## Common questions

**An ID is on the allowlist but shows no name — is something broken?**
No. It just means no local source (pod admins, primary owner, resolved-names cache) had a label and the channel API hasn't been queried yet, or the channel doesn't expose names easily (WhatsApp). The async lookup will populate the cache for the next render.

**I rejected a pending request and they re-sent `/start` — will they auto-approve now?**
Not for an hour, even if their ID matches a pod-admin claim. The rejected.json cool-off index prevents that. After an hour, normal auto-approval rules resume.

**Why does my own `/start` auto-approve but a teammate's doesn't?**
Your claim is recorded in the pod admins list; theirs isn't. Add them as a pod admin first (or have them message `evo claim charles` with the admin passphrase you set) and their next `/start` will auto-approve.

**I switched a bot from single-user to multi-user — what happens to the existing primary?**
They remain the primary owner. The new mode means the bot now also accepts other users on the allowlist. Flip back and only the primary can talk to the bot again.

**Does OpenClaw watch allowFrom.json for changes, or do I need to restart the gateway after approving?**
OC's gateway reads the file fresh on each incoming message, so approve / disconnect take effect immediately — no restart needed. The mode-toggle flow is different (it edits `openclaw.json`) and does kickstart the gateway.

**Where does the auto-approval audit show up?**
On the Alerts page (Reports → Alerts), as a self-resolving informational Signal. Signature `pairing_auto_approved:<bot>:<channel>:<id>`.

---

## Roles and capabilities (2026-06-08)

The Users page also surfaces a per-identity **role** chip alongside the existing `pod_admin` / `owner` labels. Role determines what mutations the user is allowed to make ON THIS BOT — block other users, change channel modes, set someone else's role. It's distinct from "is paired at all" (the allowlist) and from "is the bot's primary owner" (the network.json claim).

Spec: [docs/spec-user-roster-and-roles-2026-06-07.md](../spec-user-roster-and-roles-2026-06-07.md).

### The four roles

| Role | What they can do | How they got it |
|---|---|---|
| `admin` | Everything — manage every bot's roster, install apps, modify config, edit code | Pod-admin claim in `network.json` (set on the Pod Admins card) |
| `primary_user` | Manage THIS bot's roster + channel modes + bot config; cannot install apps or edit code | Set on the per-bot panel (role dropdown) or auto-inherited from the bot's Primary Owner claim |
| `participant` | Talk to the bot. No mutations. | Default for anyone admitted to the allowlist |
| `blocked` | Sticky deny. Bot ignores them entirely. | Operator clicks **Block** on a row, or evo cross-bot action |

Admin is computed from pod-admin claims, so changing it requires the Pod Admins card. Primary_user, participant, and blocked are controlled per-bot from the Users by channel rows: the role dropdown for set/promote/demote, the **Block** button for sticky deny.

### Engagement surfaces — group vs. DM

Per-identity field saying whether the bot honors group mentions, DMs, or both. Chips render alongside the role:

- `group` — bot responds when @-mentioned in a shared group
- `dm` — bot responds to private 1:1 messages
- both — most common; admin and primary_user default to both

The distinction matters because DMs are unwitnessed and less socially costed. A participant defaulting to group-only means new members of a Telegram group can talk to the bot in the group context but can't quietly DM it for things they shouldn't be asking for.

### Newcomer mode (per channel)

Each channel has a `newcomer_mode` dropdown in its header that controls what happens when an unknown sender messages the bot:

- **Auto-admit** — group membership is approval. Anyone who joins the Telegram group can talk to the bot; their `/start` (or first message) admits them as a participant. Removing them from the group quietly drops them from the allowlist. Best for trusted private groups.
- **Require approval** — existing pairing flow. Their `/start` returns a pairing code; you approve in the Users page (or evo handles it for pod admins automatically). The default.
- **Closed** — operator-only. Even pairing requests are advisory; nothing auto-admits.

The Users page Newcomers dropdown writes the mode to the bot's overlay file at `{shared_dir}/rosters/{bot_id}.json`. Changes take effect on the next message.

### Block vs. Disconnect

| Action | What happens | When to use |
|---|---|---|
| **Disconnect** | Removes from OC allowFrom. They can re-pair via `/start` and you can re-approve. | Normal removal — they left the team, their access is no longer current. |
| **Block** | Sticky deny — removes from allowFrom AND writes a block-index entry that survives re-pairing. The pairing auto-approver will never re-admit them, even if they match a pod-admin claim. | They lost trust. You don't want them sneaking back via `/start`. |

Unblock removes from the block index but does NOT re-admit them — they'd have to send `/start` again and you'd approve the new pairing.

### Three ways to manage a bot's roster

You don't have to use the admin UI. Three equivalent paths:

| Path | Who | How |
|---|---|---|
| **A — Admin UI** (this page) | Pod admin | Click role chips, toggle Newcomer dropdown, hit Block / Unblock |
| **B — DM or @-mention the bot** | Admin or primary_user | Tell the bot what to do in natural language: *"block 987654321 on telegram"*, *"set 555 to primary_user"*, *"set this channel to auto-admit"* |
| **C — Evo cross-bot** | Admin | Tell evo, naming the target bot: *"evo, block 987654321 on atlas telegram"*, *"evo, set atlas's telegram to auto-admit"* |

All three reach the same admin daemon endpoints, with the same per-endpoint capability check. The audit log shows who actually triggered each change: `by=telegram:<id> via evolve` for Path C, `by=telegram:<id>` for Path B, `by=ui:admin` for Path A.

**Path B and C are guarded by capability, not by surface.** The daemon resolves the requester's role on the target bot and refuses with 403 if they don't have `bot.roster.mutate` (or `bot.channel.config` for newcomer mode changes). A participant typing `"block alice"` in Atlas's group gets a polite "I can't do that for you" instead of a mutation.

### Worked example — onboarding Atlas to a household group

1. **Add yourself as pod admin** if you aren't already (Pod Admins card). Add your Telegram numeric user_id.
2. **Set Atlas's Telegram channel to `auto_admit`** via the Newcomers dropdown on Atlas's panel. Now anyone added to the group will be admitted as a participant.
3. **Add a primary_user** (optional). Pick a household member, get their Telegram user_id (or have them `/start` Atlas in a DM so it appears in pending), approve, then set their role to `primary_user`. They can now block / unblock / promote other users by DMing or @-mentioning Atlas.
4. **Block when needed.** If someone misbehaves in the group, click Block on their row — or just say "block them" to Atlas in the group, the bot calls the tool, the daemon writes the block.

### What the LLM knows

Every turn, the bot's plugin captures the sender's id and resolves their role + capabilities. A short "Speaker context" block goes into the LLM's system prompt:

```
SPEAKER (this turn):
  telegram:1260193629
  role=admin · can_mutate_roster=yes · can_config_channel=yes
```

For participants/blocked, the block tells the LLM to decline politely WITHOUT calling the tools (saving a refusal round-trip). The DAEMON is still the authoritative gate — if a jailbroken LLM ignores the hint and calls the tool anyway, the 403 catches it.

### Capabilities

The full set of built-in capabilities:

| Capability | Gates |
|---|---|
| `bot.roster.read` | Reading the per-bot user list |
| `bot.roster.mutate` | Add/modify/block identities, set roles |
| `bot.roles.bind` | Change which capabilities each role has (admin only — bot-scoping rule) |
| `bot.channel.config` | Set `newcomer_mode` / `default_engagement_surfaces` per channel |
| `bot.config.modify` | Edit bot configuration (openclaw.json, network.json bot block) |
| `bot.app.install` | Install / remove apps (admin only — deploy-surface rule) |
| `bot.code.modify` | Modify bot scripts or code (admin only — deploy-surface rule) |
| `bot.send_external` | Reach outside the bot's primary channel (admin and primary_user) |

`admin` has all of them. `primary_user` has the bot-scoped subset (roster.mutate, channel.config, config.modify, send_external) but NOT the deploy-surface ones. `participant` and `blocked` have none.

App-declared capabilities (manifest v7 `provided_capabilities`) layer on top once apps migrate. Not active yet.
