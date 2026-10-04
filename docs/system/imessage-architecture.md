# iMessage Integration — Architecture Notes

Operator-facing notes for iMessage as a first-class channel. Design and rulings:
`internal/design-imessage-channel-2026-09-29.md` (D-IM1..8).

---

## Overview

iMessage is a **connection** in the connections registry — service `imessage`, scope `bot`,
one row per bot — and it runs on OpenClaw's own bundled channel (`extensions/imessage`,
"iMessage (imsg)"). Evolve does not run a poller or a bridge of its own; it does three things
around the OpenClaw channel:

1. **Holds the row** (`connections.json`): the bot's address (read back from Messages, never
   typed), who may text it (`allow_from`), Apple-ID mode (`own` | `shared`), the macOS user
   whose Messages it uses, groups (off), and health (`signed_in`, `channel_up`,
   `last_inbound`, `last_outbound`).
2. **Derives the channel config** from the row (`imessage_channel.derive_channel_block`) and
   writes it to the bot's `openclaw.json` through the shared config writer, then restarts the
   gateway. The row is the source of truth; the config is derived.
3. **Probes** (`imessage_channel.probe`) — verified by doing, not by config presence.

The capability map lists `imessage.receive`, `imessage.send` and `imessage.read-history` (the
bot's own conversations only).

---

## The DM policy fails closed

The block Evolve writes always carries `dmPolicy: "allowlist"` and an explicit `allowFrom`
list (E.164 phone numbers or Apple-ID emails). An empty list means the channel receives
nothing. Evolve never writes `open`, never writes a wildcard, and keeps `groupPolicy:
"disabled"` (a group joins only when every member is allowlisted — a later brief).

OpenClaw's own default when `dmPolicy` is absent is `pairing`, so a config with no explicit
policy is **drift**, not a pass. `handle` is not an OpenClaw key (the channel block is a closed
schema); the address lives on the row only.

The probe reds — and the wizard refuses to continue — when it finds an open policy on disk
(someone edited the config by hand): `dmPolicy: open`, `groupPolicy: open`, or a `*` in an
allowlist, at the top level or under any account.

Inbound iMessage is an inbound message like any other: it passes the same taint and send
gates, and nothing here changes which model answers.

---

## Local-Only Architecture

No data leaves the machine, and there is no cloud proxy, relay service or third-party bridge:

- **Read path:** OpenClaw's `imsg` extension reads `~/Library/Messages/chat.db` (the bot's
  own macOS user's Messages database) — local file.
- **Write path:** the extension sends through Messages.app in that user's session.
- **Gateway path:** in-process inside the bot's gateway.

---

## One Apple ID per bot, signed in once by hand

Messages.app signs in one Apple ID per macOS user, and the pod already gives each bot its own
user. The bot's Apple ID is signed in **once, by hand, in that user's session** (screen
sharing to the Mac; Messages → sign in; accept the two-factor prompt). That is the only
by-hand step. Everything after is the wizard on the bot's Settings page.

A per-user LaunchAgent (`ai.openclaw.imessage-keeper.<macos_user>`, installed by the wizard)
keeps Messages.app open in that session; it writes nothing. The signed-in handle the wizard and
the probe use is read only from the pod — OpenClaw's own `channels status` — never from a file
in the bot's home, which the bot can write. When that read names no account, the probe is red
and says why. The probe also reds when Messages is not running in that session. The keeper's
install grant is rendered per bot user (`sudo evolve-admin refresh-sudoers` after adding a bot).

**Shared mode (per bot, chosen in the wizard):** a bot may bind to the pod's shared Apple ID
instead — one Messages instance on the admin user. Trade-off: no extra Apple ID, phone number
or two-factor re-authentication to babysit, versus its own inbox and isolation. Allowlists
across shared-mode bots **must be disjoint**; the wizard refuses an overlap and names the
number and the other bot. The row records `apple_id_mode: own|shared`.

---

## TCC permissions

macOS protects both paths (Transparency, Consent, and Control):

| Permission | Needed For | System Settings Location |
|------------|------------|--------------------------|
| Full Disk Access | Reading `chat.db` | Privacy & Security → Full Disk Access |
| Automation → Messages | Sending through Messages.app | Privacy & Security → Automation |

Both grants apply to the process that runs the bot's channel (the bot's gateway under its
macOS user). A missing grant shows up as a red `channel_up` in the probe.

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

---

## Known Limitations

1. **Messages.app must stay open in the bot's session.** The keeper agent reopens it; the
   probe reds while it is not running.
2. **TCC grants may need re-granting after a major macOS upgrade.**
3. **Apple ID re-authentication.** If the Apple ID signs out of iMessage (password change,
   long inactivity) the probe reds `signed_in`; signing in again restores it.
4. **The Linux pod does not run this channel.** Messages runs only on macOS, and Evolve builds
   no relay (design D-IM6). A Linux-pod bot that needs iMessage is hosted on the Mac pod.
5. **Groups are off.**
