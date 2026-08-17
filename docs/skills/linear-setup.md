# Setting Up Linear

The Linear skill gives your bot access to your Linear workspace — issues, projects, teams, and cycles. The bot can query and update tickets, surface blockers, and help manage project work through natural conversation.

**Install via:** Skills page → Linear → Install

*Linear appears in the skill catalog (`packages/admin/evolve_admin/skills/inventory.py` — `_PLUGIN_DISPLAY["linear"]`, line 66). Its credential is managed as an API key through the standard Plugins → Credentials flow.*

---

## What it does

After setup, the bot can:
- Read issues — title, status, assignee, priority, comments, cycle
- Create new issues and update existing ones
- List issues by project, team, or cycle
- Surface blockers and overdue items in briefings

---

## Prerequisites

- A Linear account (Personal, Team, or Organization plan)
- A Linear Personal API key

---

## How to get a Linear API key

1. In Linear, open your profile settings (bottom-left) → **API** → **Personal API keys**
2. Click **+ New key**, give it a description (e.g., "Evolve bot"), set expiry if desired
3. Copy the key — it starts with `lin_api_` and is shown only once

---

## How the install flow works

1. Go to **Plugins → Credentials** for the target bot
2. Find the **Linear** row (or add a new credential entry for `linear`)
3. Paste your API key (`lin_api_...`)
4. Click **Save** — the key is stored in the bot's `auth-profiles.json`
5. Go to **Plugins** → verify the Linear plugin entry is enabled

Alternatively, the Skills page walks through the same steps in a guided flow:
1. **Skills → Linear → Install**
2. Paste the API key
3. The install validates it against `https://api.linear.app/graphql` with an `{ viewer { id } }` query
4. On success, the key is stored and the skill is marked active

---

## Status values

| Status | What it means |
|--------|--------------|
| `missing_config` | No API key configured |
| `auth_failed` | Key rejected by Linear's API |
| `active` | Key verified — bot can read and update issues |

---

## Revoking access

1. In Linear: Profile → API → delete the personal API key
2. In Evolve: Plugins → Credentials → remove the Linear key (or Skills → Linear → Remove)

---

## Related

- [notion-setup.md](notion-setup.md) — for document-based project management
- [upstream-plugin-skills-setup.md](upstream-plugin-skills-setup.md) — if Linear is installed as an upstream OpenClaw community skill
