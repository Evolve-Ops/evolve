# Setting Up Apple Local (iMessage, Reminders, Notes)

The Apple Local skill gives your bot access to macOS-native apps — iMessage, Reminders, and Notes — using local automation rather than a cloud API. Everything stays on the machine; no external service is involved.

**Install via:** Skills page → Apple Local → Install

---

## What it does

After setup, the bot can interact with:
- **iMessage** — receive and send messages via the Messages app (macOS only)
- **Reminders** — read and create reminders in the macOS Reminders app
- **Notes** — read and append to notes in the macOS Notes app

All three use AppleScript / Shortcuts automation through the bot's user account on the Mac mini. No third-party API, no OAuth.

---

## Prerequisites

- macOS 13+ (Ventura or later recommended)
- The bot is running as a real macOS user account on the Mac mini
- The bot's user account must be granted TCC (Transparency, Consent, and Control) permissions for Messages, Reminders, and Notes

---

## TCC permissions — the critical step

macOS requires explicit user consent (via TCC) before any process can access Messages, Reminders, or Notes. The admin UI will guide you through the required grants, but the key principle is:

**TCC grants must be made while logged in as the bot's macOS user.** You cannot grant them remotely or as another user.

To grant permissions:
1. Log into the Mac mini as the bot user (or use `su - <bot-user>` in a terminal session with a display)
2. Open **System Settings → Privacy & Security**
3. Grant access for each relevant item:
   - **Automation** → allow your terminal / OpenClaw gateway to control Messages, Reminders, Notes
   - **Full Disk Access** (optional, for Notes databases) — only if the bot reads note attachments

Without these grants, the bot will receive `PermissionError` when trying to invoke the local apps.

---

## How the install flow works

The Apple Local install is a `kind=local` skill — similar to the Obsidian filesystem skill, it stores a config at `~/.openclaw/skills/apple_local.json`. The install walk is:

1. **Check TCC status** — the install flow attempts a minimal AppleScript call to each service to verify permissions are in place
2. **Enable capabilities** — you select which services to enable (iMessage, Reminders, Notes — individually toggleable)
3. **Confirm** — the flow verifies each selected service responds successfully

If a TCC grant is missing, the step shows a "permission needed" error with instructions to grant it.

---

## iMessage specifics

iMessage works by sending AppleScript commands to the Messages app (`osascript`). The bot receives messages by monitoring the Messages SQLite database at `~/Library/Messages/chat.db`.

Important constraints:
- The Messages app must be running on the Mac mini for real-time receive
- Messages are delivered to the bot's iCloud account, not a separate bot number — the "bot" IS you on this device
- Zero cloud roundtrip: messages go directly to the macOS Messages app, not through any server

---

## Reminders and Notes

Both use the native macOS EventKit (Reminders) and Notes APIs via AppleScript. The bot reads the local iCloud-synced databases. Writing creates new items that sync to iCloud automatically (if iCloud is set up for that service).

---

## Status values

| Status | What it means |
|--------|--------------|
| `tcc_missing` | Required TCC grants not yet made — follow the TCC permissions steps above |
| `services_not_selected` | No services enabled — at least one must be chosen |
| `active` | All selected services verified working |
| `partial` | Some services active, some have permission errors |

---

## Revoking access

Go to Skills → Apple Local → the bot's card → **Remove**. This removes `~/.openclaw/skills/apple_local.json`. TCC permissions are not automatically revoked — revoke them manually in System Settings → Privacy & Security if needed.

---

## Related

- [obsidian-setup.md](obsidian-setup.md) — for file-based note taking (cross-platform)
