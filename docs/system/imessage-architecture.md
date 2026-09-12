# iMessage Integration — Architecture Notes

Operator-facing documentation for the iMessage skill + primary-channel plugin (V2.1-6).

---

## Overview

The iMessage integration has two layers:

**Layer 1 — Skill (send-only as tool):** Any bot can send iMessages to specific contacts
using an OpenClaw tool. Reads conversation history from the local `chat.db` SQLite file.

**Layer 2 — Primary-channel plugin:** A bot configured with iMessage as its primary channel
receives incoming iMessages and responds to them. Implemented as a polling daemon
(`imessage_plugin/poller.py`) that checks `chat.db` every 15 seconds.

---

## Local-Only Architecture

iMessage is Mac-local by architecture. No data leaves the machine:

- **Read path:** SQLite query against `~/Library/Messages/chat.db` (local file).
- **Write path:** AppleScript via `osascript` → Messages.app (local process).
- **Gateway path:** HTTP POST to the bot's gateway on `127.0.0.1` (loopback only).

There is no cloud proxy, no relay service, and no third-party dependency.

---

## TCC Permissions Required

macOS protects both the read and write paths via Transparency, Consent, and Control (TCC):

| Permission | Needed For | System Settings Location |
|------------|------------|--------------------------|
| Full Disk Access | Reading `chat.db` | Privacy & Security → Full Disk Access |
| Automation → Messages | Sending via AppleScript | Privacy & Security → Automation |

Both grants must be applied to the **evolve** service user (the user that runs the
admin server LaunchDaemon). The setup wizard walks through this.

The TCC state is checked via:
1. Attempting to stat/open `~/Library/Messages/chat.db` (FDA check).
2. Running a benign AppleScript against Messages.app (Automation check).

These checks are non-destructive and idempotent.

---

## Per-Bot vs Shared Messages.app Account

This is the most significant operational decision for the iMessage integration.

### Option A — Per-Bot Apple ID (best isolation, operationally expensive)

Each bot that uses iMessage as a primary channel has its own Apple ID signed in to
Messages.app on its own macOS user account. Incoming messages land in a per-bot inbox
with zero cross-contamination.

**Requirements:**
- Each Apple ID requires an email address + phone number for creation.
- Messages.app must be signed in on the bot's macOS account.
- 2FA codes land on the Apple ID's linked phone; re-authentication is periodic.

**Suitable for:** pods where bots serve different audiences and message isolation is required.

### Option B — Shared Account + Per-Bot Allowlist (v1 choice)

A single Messages.app instance runs under the admin account (or a dedicated iMessage
account). All bots share one inbox. The poller filters by `allowed_senders`:

- Each bot has an `allowed_senders` list in its `imessage.json` config.
- The poller only forwards messages from senders on the bot's allowlist to that bot.
- Senders not in any bot's allowlist are silently ignored.

**Requirements:**
- One Apple ID for the pod (can be the pod admin's personal account or a dedicated one).
- One Messages.app instance running (no per-bot Apple ID management).

**Limitation:** If two bots share the same sender (Alice texts both team-bot-a and admin-bot),
only the bot whose allowlist includes Alice's handle will receive her messages.
There is no deduplication concern because each bot's allowlist is disjoint.

**v1 ships Option B.** The architecture is documented and Option A is possible if you
configure per-bot macOS accounts with separate Apple IDs.

---

## chat.db Schema

The integration uses only these columns (present since macOS 10.13 High Sierra):

| Table | Columns Used |
|-------|-------------|
| `message` | `ROWID`, `guid`, `text`, `handle_id`, `is_from_me`, `date`, `service`, `cache_roomnames` |
| `handle` | `ROWID`, `id`, `service` |
| `chat` | `ROWID`, `guid`, `chat_identifier`, `service_name` |
| `chat_message_join` | `chat_id`, `message_id` |
| `chat_handle_join` | `chat_id`, `handle_id` |

The `message.date` column stores nanoseconds since 2001-01-01 00:00:00 UTC on macOS 10.15+
(Catalina). Older macOS stored seconds. The helper detects the format by threshold comparison:
values above 10^13 are treated as nanoseconds; values below are treated as seconds.

**Schema drift risk:** Apple has changed the `chat.db` schema twice in five years:
- macOS 11 (Big Sur): Added group message threading columns.
- macOS 13 (Ventura): Added `is_stewie`, `is_kt_verified`, `is_kt_verified_peer_entity`.

Our queries use only the stable core columns above. New columns (whether added or removed)
do not affect the integration unless the core columns are renamed or dropped.

**Mitigation:** The fixture DB at `packages/admin/tests/fixtures/imessage_sample_chat.db`
has the full current schema. Tests run against it. On schema changes:
1. Update the fixture DB to match the new schema.
2. Verify `imessage_helpers.py` queries still return correct results.
3. Update this document.

---

## Poller Daemon

The Layer 2 poller (`imessage_plugin/poller.py`) runs as a LaunchDaemon under the
`evolve` user (same as all other Evolve infra jobs).

**Label:** `ai.evolve.evolve.imessage-poller.<bot_id>`

**Installed by:** `deploy.py:install_imessage_poller()` — called on every `deploy_bot()`
when `imessage.json` is present for the bot.

**State file:** `~/<bot_home>/.openclaw/workspace/evolve/imessage_poller_state.json`
— stores the last-seen message ROWID to avoid replaying history.

**On first run:** The poller reads the current max ROWID and uses it as the initial
watermark. Historical messages are never replayed.

**Watermark behavior on gateway failure:** If the gateway is unreachable (e.g., during
bot restart), the poller still advances the watermark. Messages during the outage window
are not retried. This is a deliberate design choice: retrying would spam the gateway
once it comes back online, and the message is already in `chat.db` for the bot to read
via the Layer 1 skill if needed.

**Poll interval:** 15 seconds (configurable via `--poll-interval` flag).

---

## Config File

Per-bot iMessage config lives at:
```
<bot_home>/.openclaw/skills/imessage.json
```

Schema:
```json
{
  "handle": "me@icloud.com",
  "allowed_senders": ["+15550001234", "alice@example.com"],
  "active_since": "2026-05-13T12:00:00+00:00"
}
```

- `handle`: The bot's iMessage address (the address others text to reach it).
- `allowed_senders`: If non-empty, only messages from these handles are forwarded (Option B).
  If empty, all incoming messages are forwarded (open mode).
- `active_since`: ISO 8601 timestamp set by the install wizard when the bot first becomes active.

---

## Security Notes

**AppleScript injection prevention:** Both `contact_handle` and `message_text` are
escaped via `_escape_as_string()` before interpolation into AppleScript source code.
The escape function translates `\` → `\\` and `"` → `\"`. Direct string concatenation
into AppleScript is never used.

**SQLite injection prevention:** All queries use parameterized statements (sqlite3's
`?` placeholders). No string interpolation is used in any SQL query.

**Path isolation:** The poller reads `chat.db` read-only (URI mode: `file:...?mode=ro`).
No writes are made to the Messages database.

**Network isolation:** The only network call is the HTTP POST to `127.0.0.1:<gateway_port>`.
No outbound calls to external services.

---

## Known Limitations

1. **Messages.app must be open for sends:** The write path (AppleScript) requires
   Messages.app to be running. If it quits (e.g., macOS update reboot), sends fail
   until it's reopened. The install wizard warns about this; the poller logs a warning
   but does not attempt to reopen Messages.app automatically.

2. **TCC permissions survive reboots but may need re-grant after macOS upgrades:**
   Major macOS version upgrades (e.g., 14 → 15) sometimes reset TCC grants. If the
   integration stops working after an upgrade, re-run the TCC permission steps in
   the setup wizard.

3. **chat.db size on large accounts:** Accounts with many years of message history
   may have `chat.db` files in the gigabyte range. SQLite queries on ROWID (an indexed
   integer primary key) are O(log N) regardless of size. The 15-second poll adds negligible
   load.

4. **Option B limitation with multiple bots sharing a sender:** Described above under
   "Shared Account + Per-Bot Allowlist."

5. **Apple ID re-authentication:** If the Apple ID signs out of iMessage (e.g., after
   a password change or long period of inactivity), sends fail and the install status
   drops to `not_signed_in`. Re-signing in Messages.app restores functionality immediately.
