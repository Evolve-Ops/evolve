# Setting Up iMessage for Your Bot

This guide walks you through giving your bot the ability to send and receive iMessages.
The whole setup takes about five minutes and happens entirely on your Mac — no cloud service,
no extra account, no third-party app.

---

## What You're Setting Up

There are two things the bot can do with iMessage:

**Sending (any bot):** The bot can send iMessages to specific contacts on request.
Example: "Text Alice that I'm running 10 minutes late."

**Receiving (primary channel):** The bot checks for incoming iMessages and responds to them.
This is for bots where you want to text them directly, the same way you text a person.

Both require the same permissions below. You only need to do this once per bot.

---

## Step 1 — Allow Access to Message History

The bot needs permission to read your message history so it can look up conversations and contacts.

1. Open **System Settings** (click the  menu → System Settings).
2. Click **Privacy & Security** in the sidebar.
3. Click **Full Disk Access**.
4. Find **Evolve** in the list and flip the toggle to **on**.

> If Evolve doesn't appear in the list, click the **+** button at the bottom of the list,
> navigate to your Applications folder, and add Evolve.

Once the toggle is on, come back here and click **I've Done This** in the setup wizard.

---

## Step 2 — Allow Sending Messages

The bot needs permission to drive the Messages app when it sends a message.

1. Still in **System Settings → Privacy & Security**, scroll down to **Automation**.
2. Find **Evolve** and click the arrow next to it to expand it.
3. Find **Messages** in the expanded list and flip its toggle to **on**.

Click **I've Done This** when the toggle is on.

---

## Step 3 — Keep the Messages App Open

The bot sends messages through the Messages app. The app needs to be running.

- Open **Messages** from your Dock or Applications folder.
- Leave it running in the background. (It doesn't need to be the frontmost window.)

On most Macs this is already the case. If you use Messages regularly, you're good to go.

---

## Step 4 — Sign In to iMessage

The Messages app needs to be signed in to an iCloud account for sending to work.

1. In the Messages app, go to **Messages → Settings** (or **Messages → Preferences** on older macOS).
2. Click the **iMessage** tab.
3. Make sure you're signed in with your Apple ID and that iMessage is turned on.

If you want the bot to have its own iMessage address (so people can text it directly as a
primary channel), sign in with a separate Apple ID specifically for the bot.

> **Note on separate Apple IDs:** Creating a new Apple ID requires an email address and
> a phone number for verification. You can use any email address you control. Apple IDs
> are free.

---

## Step 5 — Enter the Bot's iMessage Address

The last step is to tell the system which iMessage address this bot uses. This is the
address that people will text to reach the bot.

Enter it in the field in the setup wizard — it looks like an email address
(`bot@icloud.com`) or a phone number (`+15550001234`).

---

## You're Done

Once all four green checks appear, the bot is ready to send iMessages. If you also
configured it as a primary channel, it will start checking for new messages automatically.

---

## Troubleshooting

**"Full Disk Access not granted" after I turned on the toggle**

Try clicking "I've Done This" again. macOS takes a moment to propagate the permission.
If it still shows as missing, try quitting Evolve and reopening it.

**"Messages app is not running" even though I opened it**

Make sure the Messages app is actually open (you should see its icon in the Dock with a
dot beneath it). Closing it to the Dock is fine — quitting it (Cmd+Q) is not.

**The bot can look up contacts but can't send messages**

This usually means the Automation permission (Step 2) wasn't granted. Double-check that
the toggle for **Evolve → Messages** is on in Privacy & Security → Automation.

**I want to give this bot its own iMessage number**

Each Apple ID has a unique iMessage address (the email you sign in with) and can have
phone numbers linked to it. To give a bot its own address, sign in to Messages with
a dedicated Apple ID. You can have multiple Apple IDs signed in to different user accounts
on the same Mac.

---

## Privacy Notes

- Your message history stays on your Mac. Nothing is sent to any external service.
- The bot only reads messages from contacts you configure in the allowed-senders list.
  If you leave the list empty, the bot reads messages from anyone (open mode).
- You can revoke access at any time by turning off the toggles in System Settings.
- Removing the iMessage skill from the bot's configuration stops the bot from accessing
  Messages immediately.
