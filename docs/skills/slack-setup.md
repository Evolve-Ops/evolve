# Slack skill — pod admin setup

This is a one-time setup per Evolve pod. Once you've done it, any bot on the pod
can connect to your Slack workspace from the Skills page.

---

## What you'll need

- A Slack workspace you own or administer
- Access to your pod's terminal (SSH to mini, or run commands locally)

---

## Step 1 — Create a Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**.
3. Give it a name (e.g. "Evolve" or "YourPodName Bot") and select your workspace.
4. Click **Create App**.

---

## Step 2 — Configure OAuth & Permissions

1. In your new app's left sidebar, click **OAuth & Permissions**.
2. Under **Redirect URLs**, click **Add New Redirect URL** and enter:
   ```
   https://<your-pod-address>/api/skills/install/slack/oauth-callback
   ```
   Replace `<your-pod-address>` with your pod's hostname or IP (e.g. `mini.local:5050`).
   If you access the dashboard over HTTP locally, use `http://` instead of `https://`.
3. Click **Save URLs**.
4. Under **Bot Token Scopes**, click **Add an OAuth Scope** and add each of these:
   - `chat:write` — lets the bot send messages
   - `channels:read` — lets the bot see your channels
   - `im:read` — lets the bot see direct message conversations
   - `im:write` — lets the bot open direct messages
5. Click **Save Changes** (if prompted).

---

## Step 3 — Get your credentials

1. Still in your Slack app, click **Basic Information** in the left sidebar.
2. Scroll to **App Credentials**.
3. Copy your **Client ID** and **Client Secret**.

---

## Step 4 — Add credentials to the Evolve keystore

On your pod (SSH to mini, or locally if you're running the admin server on your laptop):

```bash
sudo evolve-admin keystore set slack-client-id <your-client-id>
sudo evolve-admin keystore set slack-client-secret <your-client-secret>
```

These are stored at:
- `/Users/Shared/evolve/keystore/slack-client-id`
- `/Users/Shared/evolve/keystore/slack-client-secret`

The files are owned by the `evolve` user with restricted permissions (read-only).
The `evolve-admin keystore set` command handles ownership and permissions automatically.

---

## Step 5 — Verify

Open the Evolve dashboard, go to any bot's Skills page, and click **Install** next
to the Slack skill. You should see a "Connect to your Slack workspace" step appear
(not an error about missing credentials).

If you see "Pod admin needs to register a Slack app," the credentials are not in
the keystore yet — double-check Step 4.

---

## Revoking access

- **Per-bot revoke**: Dashboard → bot → Skills → Slack → Revoke. This removes the
  token from the bot's files and the bot can no longer send to Slack.
- **Workspace-level revoke**: Go to [api.slack.com/apps](https://api.slack.com/apps),
  select your app, and click **Revoke all tokens** under OAuth & Permissions. This
  revokes all bots at once. Each bot that needs Slack will need to re-authorize.

---

## Security notes

- Bot tokens (`xoxb-...`) are stored per-bot at `~/<bot>/.openclaw/skills/slack.json`,
  owned by the bot user. The `evolve` user can read them via ACL; writes go through
  `/tmp` staging + `sudo /bin/cp` (per Evolve's CLAUDE.md file-access pattern).
- Pod credentials (Client ID + Client Secret) are stored in the shared keystore,
  owned by the `evolve` user. The Client Secret is never sent to bots or logged.
- Evolve requests bot-token scopes only (`chat:write`, `channels:read`, `im:read`,
  `im:write`). No admin-level scopes are requested by default.
