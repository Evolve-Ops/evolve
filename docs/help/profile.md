---
title: "Help: Bot Page"
slug: profile
audience: public
last_reviewed: 2026-06-05
concepts:
  - bot-page
  - bot-setup
  - archetype
  - surfacing-cadence
  - per-bot-monthly-cap
  - profile-binary
ui_surface: null   # per-bot detail surface (opened from a bot tile), not a sidebar page
related_specs:
  - internal/spec-user-profile-2026-05-07.md
---

# Help: Bot Page

The Bot page is the admin's view of a single bot's setup. It shows two things:

1. **Bot setup** — concrete admin settings the operator answers explicitly (audience, spend tolerance, cadence, timezone).
2. **User profile** — a binary signal indicating whether the bot has recorded anything about its primary user. Profile *contents* are deliberately not surfaced here. Spec: `internal/spec-user-profile-2026-05-07.md`.

Bot setup lives in `{shared_dir}/profiles/<bot_id>.md` (frontmatter only — `archetype`, `surfacing_cadence`, `timezone`). Per-user profile content lives **bot-side** at `~/<bot_user>/.openclaw/profiles/<user_key>.md`, owned by the bot user, mode 600 — explicitly excluded from the admin's read ACL. The admin only ever sees the binary `any_has_content` flag from `~/<bot_user>/.openclaw/profiles/.status.json`.

The monthly spend tolerance is stored separately in `{shared_dir}/better-engine-config.json` under `bots.<bot_id>.budget.per_bot_monthly_cap_usd`. The page edits both as a single "Save" action.

---

## Bot setup

Four settings, each mapping to a real-world question the operator already has:

### Who uses this bot?

| Choice | What it means |
|---|---|
| Just me (this is my personal bot) | Single primary user — typically the operator. The bot can be more autonomous and capability-growth-oriented. |
| A family member or single user | One non-operator user. Slightly more conservative. |
| A small team or shared bot | Multiple users. Safety posture is tighter; voice tuning is less aggressive. |

This is a label that records who the bot serves. Stored as the profile's `archetype` field.

### Monthly spend tolerance

A single dollar amount per bot per month. The system derives daily warn / hard caps from it (warn ≈ 1.5× of daily average, hard ≈ 2.5×) and the Budget Hawk generator vetoes proposals that would push spend above the hard cap.

Leave blank to use the pod-wide default. Set it when this bot is heavier or lighter than the rest of the pod.

### How often to hear from me?

Filters the proposals queue when you're viewing it scoped to this bot. **Pod-wide listings (no bot filter) are not affected** — the operator always sees everything in that view.

| Choice | Effect on this bot's proposals queue |
|---|---|
| As it arises (default) | No filter — show everything as soon as it's pending. |
| Daily | At most 7 non-urgent proposals shown at once. |
| Weekly | At most 1 non-urgent proposal shown at once. |
| Only urgent items | Only `security_critical` and `operational_urgent` proposals show — everything else is held. |

`security_critical` and `operational_urgent` proposals **always surface** regardless of cadence. The cadence only affects how many low-urgency items pile up.

### Timezone

Per-bot IANA timezone override (e.g. `America/Los_Angeles`). Leave blank to use the pod-wide default from `network.json`.

---

## User profile

The Bot page shows one of two stub messages:

- **"User has a profile."** — at least one user on this bot has facts recorded.
- **"No profile yet."** — empty, or every user has opted out of tracking.

That's all the admin sees. Profile contents are private to the bot — the user reads them via `evo profile` (chat command), not through the admin UI. The data architecture enforces this:

- Profile `.md` files live in the bot user's home, mode 600, ACL-stripped via `chmod -N`.
- `set_evolve_read_acl` in `deploy.py` explicitly excludes profile `.md` files from the evolve admin ACL.
- The admin endpoint `/api/arbiter/profile/<bot_id>` reads only `~/.openclaw/profiles/.status.json` (mode 644), which carries a single `any_has_content` bit.

Updates to the profile are **passive**: the user_profile_inferrer extracts facts from session transcripts at session_end without per-fact approval. The user controls what's recorded via:

- `evo profile` — view what's been recorded.
- `evo profile dnt on` — disable tracking, wipe existing content.
- `evo profile dnt off` — resume tracking, no backfill.

For more on the inferrer, see [profile-inferrer.md](profile-inferrer.md).

---

## Common questions

**I changed the monthly spend tolerance — when does Budget Hawk pick it up?**
Next cycle. Budget Hawk re-reads the config file on each run.

**I set the cadence to "weekly" but I still see lots of proposals.**
Cadence only filters when you're viewing this bot specifically. The pod-wide proposals list (no `bot_id` filter in the URL) shows everything by design.

**The Bot page says "User has a profile" but I want to see what's there.**
You can't, by design. Profile contents are private to the bot. Ask the user to share what they've recorded via `evo profile` if you need to know.

**Where does the actual profile content live?**
On the bot user's host: `~/<bot_user>/.openclaw/profiles/<user_key>.md`. The user can edit it directly. The admin server does not have read access (POSIX mode 600 + no ACL).

**Can I still pause/resume individual generators?**
Yes — that's on the Generators page. The user_profile_inferrer is per-bot (runs at session_end via the OpenClaw hook), so pausing it pod-wide isn't a thing — but the user can disable it for themselves with `evo profile dnt on`.
