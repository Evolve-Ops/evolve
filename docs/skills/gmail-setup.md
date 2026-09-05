# Setting Up Gmail

Gmail access is part of the Google Workspace skill (`gog`). If you only want Gmail (not Calendar), or want to understand what the Gmail scope covers specifically, this doc is for you. If you want both Gmail and Calendar together (the common case), see [gog-setup.md](gog-setup.md) instead.

---

## What Gmail access covers

The `gmail_readonly` scope lets the bot:
- Read incoming emails (subject, sender, body, attachments)
- Search your inbox
- Surface emails in Morning Briefing, Email Manager, and Email Triage apps

The bot will **not** send, delete, or modify any emails under the default `gmail_readonly` scope.

*Source: `packages/admin/evolve_admin/skills/gog_install.py` — `GOG_DEFAULT_SERVICES = ("gmail_readonly", "calendar_readonly")` (line 86); `GOG_ACCESS_PANEL` will/wont lists (lines 113–119)*

---

## Setup

Gmail is installed as part of the Google Workspace (`gog`) skill. See [gog-setup.md](gog-setup.md) for the full install flow:

1. Configure the GCP OAuth client (one-time pod setup)
2. Enable the Google plugin for the bot
3. Sign in with Google — request `gmail_readonly` scope
4. Confirm

The default GOG install requests both `gmail_readonly` and `calendar_readonly` together. You cannot install Gmail-only through the standard flow; both scopes are requested at once.

---

## Applications that use Gmail

- **Email Manager** — structured triage and response drafting
- **Email Triage** — Gmail classifier; labels and prioritizes incoming mail
- **Morning Briefing** — surfaces high-priority emails in your daily summary
- **EA Pack** — full personal assistant, includes email as one capability

---

## Related

- [gog-setup.md](gog-setup.md) — full Google Workspace install (Gmail + Calendar)
- [calendar-setup.md](calendar-setup.md) — calendar-specific documentation
