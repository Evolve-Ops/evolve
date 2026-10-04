# Setting Up iMessage for Your Bot

Give a bot its own iMessage address so the people you choose can text it from Messages on an
iPhone. It happens on your Mac, takes a few minutes, and involves no cloud service or
third-party app. Only **one** step is done by hand — signing the bot's Apple ID in to
Messages. The rest is one card on the bot's Settings page.

Open **Settings → Bots →** (pick the bot) **→ iMessage**.

---

## 1. Sign in (the one step you do by hand)

The card shows the Mac user that belongs to this bot and exactly what to do:

1. On the Mac, sign in to **Messages** as that user (screen sharing works) with the bot's
   Apple ID.
2. Accept the two-factor prompt.

That's it — there is nothing to type. The card notices the sign-in and shows the address
Messages reports ("Signed in as …"). A small background helper keeps Messages open in that
session; if it ever stops, the connection turns red and says so.

**Which Apple ID?**

- **Its own Apple ID (recommended).** The bot gets its own inbox, kept separate. Creating an
  Apple ID needs an email address and a phone number for verification; it is free.
- **The pod's shared Apple ID.** No extra Apple ID or two-factor sign-in to look after — but
  every bot on it shares one inbox, so each person may text only **one** bot. The card
  refuses a list that overlaps another bot's and tells you which number.

## 2. Who may text it

List the phone numbers (with country code, like `+15550001234`) or Apple-ID emails that may
reach the bot, one per line. The list is pre-filled from the people the pod already knows.
**Anyone not on the list is ignored.** An empty list means the bot receives nothing.

If the bot also has a Telegram connection nobody uses, the card offers to remove it. Leave the
box unchecked to keep it.

## 3. Connect

Press **Connect and say hello**. Evolve saves the connection, sets the bot up to listen to
exactly your list, restarts it, checks that it works, and texts the first person on the list:
"*bot* is here — reply to say hi." The whole thing takes under three minutes.

---

## What "healthy" means

The connection is checked by actually trying it, not by looking at settings:

- Messages is signed in with the address this connection is for.
- Messages is open in that Mac user's session.
- The channel is up in the bot's gateway.
- The bot's settings still match this connection (if someone edited them by hand, the card
  shows what differs).
- Nobody has opened the bot to everyone. If that is found, the card turns red and refuses to
  continue — Evolve never connects an open channel.

The result shows on the card, on the pod health check, and as a line in the pod report.

## Troubleshooting

**"Waiting for the sign-in…" doesn't finish.** Check that Messages is signed in on the Mac
as the user the card names (not your own user), and that the two-factor prompt was accepted.

**"Messages is not running in …'s session."** Open Messages once in that user's session; the
helper keeps it open from then on.

**The channel stays down after connecting.** macOS may be asking for permission. On the Mac,
in **System Settings → Privacy & Security**, allow **Full Disk Access** (to read messages) and
**Automation → Messages** (to send) for the bot's gateway, then press **Check again**.

**Someone can't reach the bot.** Add their number or email under "Change who may text it".

## Privacy

- Messages stay on your Mac. Nothing goes to any outside service.
- Only people on the list can reach the bot; the bot reads only its own conversations.
- **Disconnect** removes the connection and stops the bot from listening. You can also sign
  the Apple ID out of Messages at any time.
