# Setting Up Notion

The Notion skill gives your bot access to your Notion workspace — pages, databases, and blocks. The bot can read and write pages, query databases, and help manage notes, projects, and knowledge bases through natural conversation.

**Install via:** Skills page → Notion → Install

*Notion appears in the skill catalog (`packages/admin/evolve_admin/skills/inventory.py` — `_PLUGIN_DISPLAY["notion"]`, line 67). Its credential is managed as an API key through the standard Plugins → Credentials flow.*

---

## What it does

After setup, the bot can:
- Read pages and database entries — title, content, properties
- Create new pages and update existing ones
- Query databases (filter, sort, paginate)
- Search across the workspace

Access is scoped to pages your Notion integration has been explicitly shared with — the bot only sees what you give it access to.

---

## Prerequisites

- A Notion account (Free or paid plan)
- A Notion Internal Integration token

---

## How to get a Notion integration token

1. Go to `www.notion.so/my-integrations`
2. Click **+ New integration**
3. Name it (e.g., "Evolve bot"), select the workspace, set capabilities (Read content + Update content + Insert content for full use)
4. Copy the **Internal Integration Secret** (starts with `secret_`)
5. **Important:** Share each page or database you want the bot to access: open the page → **Share** → invite your integration by name

The bot can only read/write pages that have been explicitly shared with the integration.

---

## How the install flow works

1. Go to **Skills → Notion → Install**
2. Paste your Internal Integration Secret (`secret_...`)
3. The install validates it against the Notion API (`/v1/users/me`) to confirm it's active
4. The key is stored in the bot's `auth-profiles.json`

---

## Status values

| Status | What it means |
|--------|--------------|
| `missing_config` | No integration token configured |
| `auth_failed` | Token rejected by Notion's API |
| `active` | Token verified — bot can access shared pages |

---

## Common issue: pages not accessible

If the bot says it can't find a page that you know exists, it hasn't been shared with the integration yet. Open the page in Notion → Share → add the integration by name.

---

## Revoking access

1. In Notion: `notion.so/my-integrations` → delete the integration
2. In Evolve: Skills → Notion → Remove (or Plugins → Credentials → remove the Notion key)

---

## Related

- [obsidian-setup.md](obsidian-setup.md) — for local Markdown-based notes (no cloud API)
- [linear-setup.md](linear-setup.md) — for project/issue tracking
- [upstream-plugin-skills-setup.md](upstream-plugin-skills-setup.md) — if Notion is installed as an upstream OpenClaw community skill
