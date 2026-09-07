# Setting Up Upstream Plugin Skills

Upstream plugin skills are OpenClaw community skills installed from ClawHub — the OpenClaw skill registry. Unlike Evolve's built-in skills (Gmail, Obsidian, Slack, etc.), upstream skills are maintained by the OpenClaw community and installed via `openclaw plugins install`.

**Install via:** Skills page → (skill name) → Install  
**Or via CLI:** `sudo evolve-admin deploy <bot>` after adding the skill to the bot's config

---

## What upstream plugin skills are

OpenClaw ships with a community skill registry (ClawHub). Any skill published there — CRM integrations, specialized APIs, language tools, domain-specific workflows — can be installed on a bot as a plugin entry under `plugins.entries` in `openclaw.json`.

Evolve surfaces these skills in the catalog under the label "upstream plugin skill." The install flow in the admin UI wraps the standard `openclaw plugins install` command.

*Skills are tracked in `packages/admin/evolve_admin/skills/inventory.py` — plugins discovered in `plugins.entries` of `openclaw.json` appear in the skill inventory automatically (lines 298–326). MCP servers discovered in `mcp.servers` appear as `format_compliance="standard"` entries (lines 328–348).*

---

## Installing an upstream plugin skill

### Via the admin UI

1. Go to **Skills** page
2. Find the skill in the catalog (search by name)
3. Click **Install**
4. The UI shows what the skill does and what credentials (if any) it needs
5. Click **Install** — the admin server runs `openclaw plugins install <skill-id>` for the target bot via `evolve-admin`
6. After install, the skill appears in `plugins.entries` in the bot's `openclaw.json`
7. If the skill requires an API key or token, you'll be prompted to add it in Plugins → Credentials

### Via the CLI

```bash
# As pod-admin user, on the mini
sudo -u <bot-user> openclaw plugins install <skill-id>

# Then redeploy to sync Evolve's view
sudo evolve-admin deploy <bot-id>
```

---

## Providing credentials for upstream skills

Most upstream skills need credentials — an API key, OAuth token, or endpoint URL. After install:

1. Go to **Plugins → Credentials** for the bot
2. Find the new skill's row (it appears automatically if the skill declares its credential requirements)
3. Paste the required key/token
4. Click **Save**

If the skill uses the standard OpenClaw credential key format (`provider:key_type`), it shows up in the Credentials tab automatically. If it uses a custom format, check the skill's `SKILL.md` for instructions.

---

## Format compliance

Upstream OpenClaw plugins have `format_compliance = "proprietary"` in the skill inventory — they use the OpenClaw SKILL.md format, which is proprietary to the OpenClaw runtime. MCP servers installed as upstream plugins have `format_compliance = "standard"` — they're portable across runtimes that support the Model Context Protocol.

*Source: `inventory.py` — `format_compliance` field logic, lines 316–317 and 344–347*

---

## Keeping upstream skills updated

Upstream skills update independently from Evolve. To update a skill:

```bash
sudo -u <bot-user> openclaw plugins update <skill-id>
```

Or use **Plugins → (skill name) → Update** in the admin UI if the update action is available.

Evolve's Recommendations page may surface update proposals when a newer version of an installed skill is available.

---

## Removing an upstream skill

1. **Skills → (skill name) → Remove** in the admin UI, **or**
2. CLI: `sudo -u <bot-user> openclaw plugins remove <skill-id>`, then redeploy

The skill entry is removed from `plugins.entries` in `openclaw.json`. Credentials stored in `auth-profiles.json` are removed separately via Plugins → Credentials → Remove.

---

## Related

- [gog-setup.md](gog-setup.md) — GOG (Gmail + Calendar), the most common upstream skill
- [linear-setup.md](linear-setup.md) — Linear, may be available as an upstream skill
- [notion-setup.md](notion-setup.md) — Notion, may be available as an upstream skill
- `docs/applications-vs-skills.md` — the distinction between skills (primitives) and applications (goal contracts)
