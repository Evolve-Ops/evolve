# Slack Setup Guide for Evolve Bots

This is the operator's reference for wiring a Slack-integrated Evolve bot. It covers OAuth scopes (what permissions to grant), streaming behavior (how the bot displays its work in channels), and the verbose-bot anti-pattern (why your bot should be quieter in team channels than in DMs).

After install, run `evolve-admin slack-doctor <bot>` — the status header surfaces every concern documented here against your bot's actual config, so you can see at a glance what's enabled, what's missing, and what's about to embarrass you in front of your team.

---

## Part 1: OAuth Scopes — Feature Setup

When you create a Slack app for an Evolve bot, you pick which OAuth scopes the app gets. Slack's app dashboard lists ~80 scopes; most operators don't know which they need for which features, so the setup pattern becomes "ship → find a feature doesn't work → add scope → reinstall → repeat." This section collapses that loop into a single decision: "what should the bot do, and what are the scopes that enable each thing?"

Each block below is a coherent feature with the minimum scopes Slack requires. Add the scopes in the bot's Slack app dashboard under **OAuth & Permissions → Bot Token Scopes**, then reinstall the app to apply.

### Respond to @-mentions (most common starting point)
`app_mentions:read`, `chat:write`

The bot sees `@team-bot-a` in any channel it's a member of and can reply.

### Send and receive direct messages
`im:history`, `im:read`, `im:write`, `chat:write`

One-on-one DM conversations with users. Required for any "DM the bot to ask a question" flow.

### Participate in ad-hoc group DMs
`mpim:history`, `mpim:read`, `mpim:write`, `chat:write`

Multi-person DMs — the "create a group chat with three people" Slack flow without a channel. Surprisingly easy to forget; bots that work in DMs and channels can be silently broken in MPDMs.

### Read + reply in public channels
`channels:history`, `channels:read`, `chat:write`

Bot processes every message in channels it's a member of. Combined with `requireMention: true` in the channel's openclaw.json entry, it only acts on @-mentions.

### Read + reply in private channels
`groups:history`, `groups:read`, `chat:write`

Same as above, for private (invite-only) channels.

### Show "thinking" via emoji reaction
`reactions:write`

Bot adds a ⌛ or 💭 emoji to your message while it works. This is the single highest-value scope for user trust — without it, the bot looks broken between "you sent a message" and "the bot replied 8 seconds later."

### See reactions other people add
`reactions:read`

Useful for "react with 👍 to confirm" UX patterns.

### Handle file attachments
`files:read`, `files:write`

Bot can read uploaded files (images, PDFs) and attach files to its replies.

### Look up users + workspace identity directory
`users:read` — **recommended**. Add `users:read.email` for the richer directory.

Lets the bot resolve `@dave` or `dave@example.com` to a Slack user ID, and powers the **Evolve workspace identity directory** (see Part 4 below). Without `users:read`, the bot has no way to map Slack IDs to real names — leads to "pod-admin vs Pod-Admin vs U0PLKKXV0" confusion (the team-bot-a-2026-05-15 incident).

`users:read.email` is the extra scope that lets the directory include each user's email address. Email is one of the strongest disambiguation anchors — recommended for any team-bot install.

### Read user groups (`@-team` mentions)
`usergroups:read`

Lets the bot understand `@-engineering` and expand it to current members.

### Read workspace name + branding
`team:read`

Lets the bot include the workspace name in messages and logs.

---

## Elevated scopes (review carefully)

These widen the blast radius if the bot token leaks. Add only if the feature is needed.

### Post in any public channel without being a member
`chat:write.public`

Convenient for cross-channel announcements; means a leaked token can post anywhere.

### Customize sender name + avatar per message
`chat:write.customize`

Required for the "status message" pattern where the bot posts as a different name/icon to communicate state.

### Search workspace history
`search:read.files`, `search:read.im`, `search:read.mpim`, `search:read.private`, `search:read.public`, `search:read.users`

Bot can answer "where did we discuss X last week?". Read access spans every message the bot is authorized to see; a leaked token = a workspace-wide indexable mirror.

### Create / archive / rename channels
`channels:manage`, `groups:write`

Operator-level access — the bot can structurally change the workspace. Most bots **don't** need this.

### Create / modify user groups
`usergroups:write`

Bot can change who's in `@-engineering`. Operator-level; rarely needed.

### Auto-join public channels
`channels:join`

Bot can add itself to public channels without being invited. Useful for "watcher" bots, surprising for assistant bots.

### Slack AI Assistant surface
`assistant:write`

Bot integrates with Slack's native AI Assistant UI (sidebar, in-thread suggestions). Required for the new "Slack-native AI" experience.

---

## Recommended starter set (Plex-test minimum)

For most assistant bots, start here:

```
app_mentions:read              # respond to @-mentions
chat:write                     # send messages
im:history, im:read, im:write          # DMs
mpim:history, mpim:read, mpim:write    # group DMs
channels:history, channels:read        # public channels
groups:history, groups:read            # private channels
reactions:write                # show "thinking" via emoji
users:read                     # look up users
users:read.email               # workspace directory email column
```

That's 14 scopes. Reinstall the app once with this set, and you have a working messaging bot with a full identity directory. Add file handling, search, or assistant API as separate steps when you need them.

---

## Part 2: Streaming and Reply Behavior

The most embarrassing failure mode of a Slack bot isn't the bot being silent — it's the bot being too loud. OpenClaw's streaming feature exists so a user chatting with the bot in a DM can see "team-bot-a is working…" while it runs tool calls. In a team channel where the bot processes every message, that same feature broadcasts every internal step to everyone watching the channel.

### Streaming modes

Per [OC docs](https://docs.openclaw.ai/channels/slack), `channels.slack.streaming.mode` accepts three values:

- **`partial`** — Live updates as the response is generated. Replaces the draft message with the latest partial output as the response generates. The most aggressive streaming.
- **`block`** — Appends chunked preview updates. Users see accumulating text blocks added sequentially to the message — **NOT** a quiet "thinking…" indicator. The intermediate tool-call text is still rendered into the message body. Verified against a live team-bot-a configuration on 2026-05-15.
- **`off`** — No live preview. The bot posts once when it has a final answer. The only mode that's truly silent in a team channel.

There's also `channels.slack.streaming.nativeTransport` — when `true`, OC uses Slack's native streaming API which exposes tool-call boundaries explicitly.

And one more knob most operators miss: **`channels.slack.streaming.progress.commandText`** — default is `"raw"`, which renders the literal `command run python3 ...` text into the streamed message. Set it to `"status"` to keep compact tool-progress lines while hiding the raw command text.

### When to use each

| Bot type | Recommended `streaming.mode` | Why |
|---|---|---|
| Personal-bot DM only (one user) | `partial` | The user wants live feedback; nobody else sees it. |
| Team bot (multi-user channels) | **`off`** | The only mode that doesn't leak tool calls into the channel. `block` is NOT a quieter alternative — it still appends tool-call output to the streamed message. |
| Watcher bot (reads but rarely replies) | `off` | Same as above. |
| Mixed (DMs + channels) | `off` | The channel surface is the higher-blast-radius case. Bot loses live feedback in DMs but gains silence in channels. |

> **Note (historical):** an earlier version of this guide recommended `block` for team channels. That was wrong. OC's `block` mode "appends chunked preview updates" per the docs — verified empirically against a live team-bot-a configuration that was producing walls of tool-call output in a team channel while on `block` mode. Only `off` is silent. If you have a bot still on `block`, switch to `off`.

### Hazard: any streaming mode != "off" in a channel with `requireMention: false`

The team-bot-a-2026-05-14 / 2026-05-15 incident shape:

- `streaming.mode: "partial"` or `"block"` → intermediate state visible in the channel
- `requireMention: false` → bot processes every message in the channel
- `progress.commandText: "raw"` (the default) → literal command preview leaks into the streamed message body
- Result: every conversation in that channel produces a wall of `command run python3 …`, `tool: exec`, exec output, etc.

The slack-doctor flags this as **SLK015 FAIL** (mode-level) and/or **SLK016 WARN** (commandText-level).

The fix:

```
"channels": {
  "slack": {
    "streaming": {
      "mode": "off",              // any of partial/block leaks in team channels
      "nativeTransport": false
    }
  }
}
```

Apply via:
1. **Admin UI** — Settings → Pod Config → Bot Config → Slack Policy card → Apply (once the policy supports the streaming field). *(In flight.)*
2. **Upstream OC CLI** — `openclaw config patch --file ./streaming-fix.json5` per [docs.openclaw.ai/channels/slack](https://docs.openclaw.ai/channels/slack).
3. **Hand-edit** — `sudo /bin/cat /Users/<bot>/.openclaw/openclaw.json | jq '.channels.slack.streaming.mode = "off"'` + `sudo /bin/cp` per the CLAUDE.md pattern.

---

## Part 3: The Verbose-Bot Anti-Pattern

Even after streaming is set correctly, a bot can still be unbearably verbose if its prompt encourages narrating internal steps. Two distinct sources of noise:

1. **OpenClaw streaming** — covered above (`streaming.mode`). Set it to `block` or `off` for team-channel bots.
2. **The bot's own writing voice** — covered here. Live in the bot's prompt / `POD_CONDUCT.md`.

A bot trained to "explain your work" or "narrate your reasoning" will, in a team channel, post things like:

> "Let me check the logs."
> *(30 seconds of silence)*
> "Found it. The selfheal log shows…"
> "Let me also check the config:"
> *(more silence)*
> "Good — the streaming change is in. Now let me fix the selfheal grep:"

In a DM that's pleasant; the user feels accompanied. In a team channel it's three separate messages where one would do. The right pattern in a team channel is: **work silently, post once when done**.

If you control the bot's `POD_CONDUCT.md` (per the `project_pod_conduct_mechanism` memory), add language like:

> When you're in a team channel — a channel listed under `channels.slack.channels` with `requireMention: false`, or any group conversation with more than two participants — respond once and concisely. Don't narrate intermediate steps ("Let me check…", "Working…"). Don't post tool output or exec results raw. If you need to share command results, summarize them. When you'd be tempted to post "let me do X, then Y, then Z," just do X+Y+Z silently and post the final answer.

The slack-doctor doesn't check `POD_CONDUCT.md` (it's content, not code). But the SLK015 status header is your hint to also review the bot's writing voice when you change streaming mode — they're paired concerns.

---

## Part 4: Workspace Identity Directory

Slack exposes multiple names for the same user — and bots routinely confuse them. The team-bot-a-2026-05-15 incident: team-bot-a treated `"pod-admin"` (Slack legacy username) and `"Pod-Admin"` (real name) and `U0PLKKXV0` (Slack user ID) as three different people, even though they're all the same operator.

The fix isn't bot-side memory (team-bot-a tried that — said "I'll lock this in" — and the same confusion recurred the next time a new identifier appeared). The fix is structural: Evolve maintains a curated workspace directory that maps every active user's full identity tuple, and injects it into the bot's session prompt at session_start.

### What the directory contains

Pulled from Slack's `users.list`:

| Field | What it is |
|---|---|
| `id` | Immutable Slack user ID (`U0...` / `W0...`) |
| `name` | Legacy username (workspace-unique, kebab-case — e.g. `pod-admin`) |
| `real_name` | Full name as the user provided it (e.g. `Pod-Admin Alden`) |
| `display_name` | Operator-chosen workspace display (often empty → Slack falls back to `real_name`) |
| `email` | Email address (**requires `users:read.email` scope**) |
| `title` | Job title |
| `role` | `owner` / `admin` / `member` / `bot` / `deactivated` |

### Workflow

1. **Refresh:** `evolve-admin slack-directory <bot> --refresh` — pulls `users.list`, filters out bots, sorts admins to the top, writes `{shared_dir}/bots/<bot>/slack-directory.json`.
2. **Inspect:** `evolve-admin slack-directory <bot>` — prints the directory as a table.
3. **Auto-refresh:** runs as the last step of every `evolve-admin deploy <bot>` for any Slack-enabled bot. Catches the "operator just turned Slack on, now needs the directory" case.
4. **Staleness:** the slack-doctor's **SLK017** flags missing or >24h-stale directories. Continuous probe re-runs the check on schedule.
5. **Injection:** at every Slack session_start, the directory's contents are appended to the bot's system prompt (alongside POD_CONDUCT). The bot sees the full identity tuple for every workspace user and the rule: "Every row is one person; if you see any of (Slack ID, name, display_name, real_name, email) in a message, all of those identify the same individual."

### Length cap

The injection caps at ~3KB (matches `app_posture`'s cap). Workspaces over ~40 users will see the table truncated to the most-recently-active users, with a tail note pointing to the full file on disk. If your workspace is bigger, file an issue — we'll add per-channel scoping (only inject users who are members of channels the bot listens in).

### When the directory is wrong

Two failure modes:

1. **Missing `users:read.email`** — directory builds, but the email column is empty. The slack-doctor **SLK018** surfaces this as INFO with a pointer to add the scope.
2. **Operator hasn't refreshed yet** — `slack-doctor` shows **SLK017 WARN**. Either run `slack-directory <bot> --refresh` manually, or re-deploy the bot (auto-refresh runs as the last deploy step).

---

## After install — verify everything

```bash
evolve-admin slack-doctor <bot>
```

The status header will show:

```
team-bot-a · workspace Acme · token openclaw_channels
  Provider: enabled · mode=socket · groupPolicy=allowlist · dmPolicy=pairing · streaming=block
  Listening (10): ...
  Features enabled (8):
    ✓ Respond to @-mentions
    ✓ Send and receive direct messages
    ✓ Read + reply in public channels
    ✓ Show "thinking" via emoji reaction
    ...
  Features partially configured (missing scopes):
    ✗ Handle file attachments (missing: files:write)
```

If `streaming=partial` shows up in red, that's SLK015 telling you to switch to `block` or `off` for team-channel use.

The "partially configured" lane is the scope-checklist win — you see what's almost-but-not-quite working and the exact scope to add. No more "ship → break → add scope → reinstall" loop.
