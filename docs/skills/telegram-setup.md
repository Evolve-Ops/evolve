# Telegram skill — setup guide

This is a one-time setup per bot. Once you've done it, the bot can send
(and receive) messages on Telegram.

Telegram bots use a key from @BotFather — Telegram's official bot registration
service. There is no OAuth flow; you copy the key and paste it into Evolve.

---

## What you'll need

- A Telegram account (any — personal is fine)
- Access to the Evolve dashboard

---

## Step 1 — Create a bot via @BotFather

1. Open Telegram (desktop or mobile).
2. Search for **@BotFather** and start a conversation.
3. Send `/newbot`.
4. BotFather will ask for a name (e.g. "My Evolve Bot") — type one and send it.
5. BotFather will ask for a username (must end in `bot`, e.g. `myevolvebot`).
6. BotFather will reply with a message containing your **bot key** — a string
   that looks like `1234567890:ABCDEFGhijklmnopQRSTuvwxyz-1234567`.

Copy that key. You'll paste it into Evolve in the next step.

---

## Step 2 — Paste the key into Evolve

1. Open the Evolve dashboard.
2. Go to the bot's **Skills** page.
3. Click **Install** next to the Telegram skill.
4. Paste your BotFather key into the field and click **Verify and save**.

Evolve will call Telegram's `getMe` API to confirm the key is valid before
saving it. If the key is rejected, double-check that you copied the full token
from BotFather's reply.

---

## Step 3 — Add the bot to a chat or group

Telegram bots can only send messages to chats they've been added to.

- **Direct messages**: have the person you want to reach send `/start` to your
  new bot in Telegram. After that, the bot can message them.
- **Groups**: open the group → Add Member → search for your bot's username.
- **Channels**: open the channel → Administrators → Add Administrator → search
  for your bot's username and grant "Post messages" permission.

---

## Optional: adjust bot privacy settings

By default, Telegram bots only see messages that @mention them in groups.
If you want the bot to see all group messages (so it can respond to anything):

1. Open a conversation with @BotFather.
2. Send `/setprivacy`.
3. Select your bot.
4. Choose **Disable** — the bot will now see all messages in groups it's in.

To let the bot join groups directly (rather than requiring an explicit add):

1. Send `/setjoingroups` to @BotFather.
2. Select your bot.
3. Choose **Enable**.

---

## Revoking access

- **Per-bot revoke in Evolve**: Dashboard → bot → Skills → Telegram → Revoke.
  This removes the key from the bot's files; the bot can no longer send to Telegram.
- **Telegram-side revoke**: Open @BotFather → `/revoke` → select your bot.
  This invalidates the key everywhere. You'll need to get a new key from
  BotFather and re-enter it in Evolve if you want to reconnect.

---

## Security notes

- The Telegram bot key is stored per-bot at `~/<bot>/.openclaw/skills/telegram.json`,
  owned by the bot user. The `evolve` service reads it via file-system ACLs; writes
  go through `/tmp` staging + `sudo /bin/cp` (per Evolve's CLAUDE.md access pattern).
- Bot keys grant access only to chats the bot has been added to. Evolve never
  stores or transmits the key off-pod.
- Telegram does not support programmatic token revocation. To fully revoke
  access, use @BotFather as described above.
