# Discord skill — pod admin setup

This is a one-time setup per Evolve pod. Once you've done it, any bot on the pod
can send messages to Discord servers from the Skills page.

---

## What you'll need

- A Discord account (and access to the servers you want the bot to join)
- Access to your pod's terminal (SSH to mini, or run commands locally)

---

## Step 1 — Create a Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
   and click **New Application**.
2. Give it a name (e.g. "Evolve" or "YourPodName Bot") and click **Create**.

---

## Step 2 — Create a bot user

1. In your new application's left sidebar, click **Bot**.
2. Click **Add Bot** → **Yes, do it!**
3. Under **Privileged Gateway Intents**, leave all intents **off** by default.
   You can enable them later if your use case requires reading message content —
   but they require Discord verification for large bots, so leave them off to start.
4. Click **Save Changes**.

---

## Step 3 — Get your credentials

You need three values:

**Client ID:**
1. In the left sidebar, click **OAuth2 → General**.
2. Copy the **Client ID** shown at the top.

**Client Secret:**
1. On the same page, click **Reset Secret** to reveal the Client Secret.
2. Copy it immediately — Discord won't show it again after you leave the page.

**Bot Token:**
1. Go back to **Bot** in the left sidebar.
2. Click **Reset Token** → confirm.
3. Copy the token immediately — same warning applies.

---

## Step 4 — Add credentials to the Evolve keystore

On your pod (SSH to mini, or locally if you're running the admin server on your laptop):

```bash
sudo evolve-admin keystore set discord-client-id <your-client-id>
sudo evolve-admin keystore set discord-client-secret <your-client-secret>
sudo evolve-admin keystore set discord-bot-token <your-bot-token>
```

These are stored at:
- `/Users/Shared/evolve/keystore/discord-client-id`
- `/Users/Shared/evolve/keystore/discord-client-secret`
- `/Users/Shared/evolve/keystore/discord-bot-token`

The files are owned by the `evolve` user with restricted permissions (read-only).
The `evolve-admin keystore set` command handles ownership and permissions automatically.

---

## Step 5 — Install Discord for a bot

1. Open the Evolve dashboard, go to any bot's **Skills** page, and click **Install**
   next to the Discord skill.
2. You'll see an **Invite the bot to your Discord server** step with an invite link.
3. Click the link (or copy it to your browser). Discord will ask you to select a server.
4. Select the server you want the bot to join and click **Authorize**.
5. Back in the Evolve dashboard, click **Confirm** to verify the bot token is valid.

The bot is now connected to Discord for that bot. Repeat the invite step for
each additional Discord server you want the bot to join.

---

## Adding the bot to more servers

You can add the bot to additional Discord servers at any time:

1. Go to the bot's Skills page in the Evolve dashboard.
2. If the skill is already installed (status: Connected), you can still generate
   a new invite URL from the start-oauth route:
   ```
   https://<your-pod-address>/api/skills/install/discord/start-oauth
   ```
3. Or construct the invite URL manually using your Client ID:
   ```
   https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&permissions=2149550256&scope=bot+applications.commands
   ```

---

## Revoking access

- **Per-bot revoke**: Dashboard → bot → Skills → Discord → Revoke. This removes the
  bot's activation record from its local files. The pod-level bot token is unaffected
  — other bots on the pod that use Discord can continue to work.
- **Application-level revoke**: Go to
  [discord.com/developers/applications](https://discord.com/developers/applications),
  select your app, go to **Bot**, and click **Reset Token**. This generates a new
  token, which means all bots on the pod will need the new token added to the keystore
  before they can send to Discord again.
- **Remove from a specific server**: Go to your Discord server's settings →
  Integrations → find your bot → Remove. This removes the bot from that server only.

---

## Permissions granted

When the bot is invited using Evolve's default invite URL, it gets these permissions:

| Permission | What it allows |
|------------|---------------|
| View Channels | See channels in servers it has joined |
| Send Messages | Post messages in those channels |
| Read Message History | See earlier messages in channels (for context) |
| Use Application Commands | Use slash commands |

No admin-level permissions are requested. If your use case needs additional
permissions, you can construct a custom invite URL with a different permissions
bitmask from the Discord Permissions Calculator.

---

## Privileged intents

Evolve's Discord skill does **not** enable privileged gateway intents by default:

- **Server Members Intent** — required to enumerate all guild members. Off by default.
- **Message Content Intent** — required to read the full content of messages in
  channels where the bot isn't mentioned. Off by default.

These intents require Discord verification for bots in more than 100 servers. For
a pod-scale bot (a small number of personal or team servers), they can be enabled
in the Discord Developer Portal under **Bot → Privileged Gateway Intents**, but
you should only enable them if you need them.

---

## Security notes

- The bot token is stored in the shared keystore at
  `/Users/Shared/evolve/keystore/discord-bot-token`, owned by the `evolve` user.
  It is never logged or sent to any external service beyond Discord's own API.
- Per-bot activation records (which bots have been confirmed) are stored at
  `~/<bot>/.openclaw/skills/discord.json`, owned by the bot user. The `evolve`
  user can read them via ACL; writes go through `/tmp` staging + `sudo /bin/cp`
  (per Evolve's CLAUDE.md file-access pattern).
- The bot token is pod-level, not per-bot. All bots on the pod that use the
  Discord skill share the same application token. This is Discord's design —
  one bot application per pod, invited to multiple servers as needed.
