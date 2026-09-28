# Setting Up Google Calendar

Calendar access is part of the Google Workspace skill (`gog`). This doc covers what the Calendar scope covers and which applications use it. For the full install flow, see [gog-setup.md](gog-setup.md).

---

## What Calendar access covers

The `calendar_readonly` scope lets the bot:
- List events on your Google Calendar — titles, times, attendees, descriptions
- Check what's on your schedule for a given day or week
- Surface upcoming events in Morning Briefing and Calendar Watch apps

The bot will **not** add, move, or cancel calendar events under the default `calendar_readonly` scope.

*Source: `packages/admin/evolve_admin/skills/gog_install.py` — `GOG_DEFAULT_SERVICES = ("gmail_readonly", "calendar_readonly")` (line 86); `GOG_ACCESS_PANEL` wont list: "Won't add, move, or cancel calendar events" (line 118)*

---

## Setup

Calendar is installed as part of the Google Workspace (`gog`) skill. See [gog-setup.md](gog-setup.md) for the full install flow. The default GOG install requests `calendar_readonly` automatically alongside `gmail_readonly`.

---

## Why Calendar was split from GOG

In v2.1, Calendar was extracted from the combined GOG skill into its own documentation entry (`V2.1-3` sprint). This lets apps declare a Calendar dependency independently from Gmail — for example, a scheduling-only bot can declare it needs `calendar_readonly` without implying it needs email access.

The underlying OAuth token is still requested together (one Google sign-in covers both), but the skill catalog treats them as logically separate capabilities.

---

## Applications that use Calendar

- **Morning Briefing** — shows today's schedule in the daily summary
- **Calendar Watch** — watches for new events and alerts you
- **Note-taker** — attaches meeting notes to calendar events
- **EA Pack** — full personal assistant, includes calendar as one capability

---

## Related

- [gog-setup.md](gog-setup.md) — full Google Workspace install (Gmail + Calendar)
- [gmail-setup.md](gmail-setup.md) — Gmail-specific documentation
