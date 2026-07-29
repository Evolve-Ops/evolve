---
title: "Help: Skills Page"
slug: skills
audience: public
last_reviewed: 2026-06-23
concepts:
  - skills
  - skill-install
  - plugin-install
  - mcp-install
  - capability-maintenance
ui_surface: admin.skills
related_specs: []
---

# Help: Skills Page

The Skills page (in the **Improve** bucket) is where you give your bots new
abilities. A **skill** is a single thing a bot *can do* — send a Slack message,
read your Gmail, search the web, create a calendar event. This is the page to
browse what's available and turn capabilities on (or off) per bot.

**Skills vs. apps.** A skill is one capability; an [application](apps.md)
orchestrates skills toward a goal. A "Morning Brief" app might use a web-search
skill, a calendar-read skill, and a messaging skill together. You add the raw
capabilities here; apps put them to work.

**Two kinds of skill, one page.** Under the hood a skill is delivered either as
an **OpenClaw plugin** or as an **MCP server** (a portable, open-standard
connector). You don't have to care which when you're installing — both show up
in the same catalog and install the same way. The difference only matters when
you're reading the fine print on a given entry.

**This is the install surface, not the keys surface.** Browsing, installing,
removing, and scoping capabilities happens here. Managing the API keys, channel
connections, and per-bot configuration *behind* those capabilities lives on the
[Plugins](plugins.md) page. Rule of thumb: come here to add or remove an ability;
go to Plugins to manage the credentials and connections an ability uses.

---

## Browse — add a skill

The **Browse** view is the catalog: every skill available to install, with a
short description of what it does. This is the default view and the action-
oriented one — it's where you see capabilities a bot doesn't have yet.

To add a skill:

1. Find it in the catalog (use **↻ Refresh** if you just expect something new).
2. Choose which bot to install it on.
3. If the skill needs more than a flip — a set of permissions to grant, an
   account to connect — a short install flow walks you through it before
   anything is enabled.

### Scoping what a skill can touch

Some skills cover a family of capabilities, and you don't have to grant all of
them. The clearest example is **Google Workspace** — Gmail, Calendar, Drive,
Docs, and Sheets behind one entry. The install flow lets you pick exactly which
surfaces a bot gets: "read Calendar and read Gmail" without handing over Drive
or write access. You connect the account once, approve the scopes you chose, and
the bot gets only those.

If you grant a second bot a subset of capabilities you already approved on an
earlier bot, the install is smart enough to skip the parts you've already done.

### When an install needs a key or an account

Installing a capability and supplying its credentials are two steps. Many skills
need an API key or an OAuth connection to actually run; the install flow prompts
for what's required, and the key itself is stored and managed over on the
[Plugins](plugins.md) page (under Credentials). If a skill shows as installed but
isn't working, a missing or unhealthy key is the usual reason — check Plugins.

---

## Installed — the pod-wide matrix

The **Installed** view is a grid: every skill down one side, every bot across the
top, with each cell showing whether that bot has that skill. It's the fastest way
to answer "which bots can do X?" and to spot a bot that's missing a capability
its peers have.

Click a cell to install or uninstall that skill on that bot. Click into a cell's
history to see its **audit trail** — when the skill was installed or removed on
that bot, so a capability never silently appears or disappears without a record.

---

## Maintaining capabilities over time

Skills aren't fire-and-forget. A few things to keep an eye on:

- **Uninstalling** — removing a skill from a bot is the same one-click action as
  installing, from either view. When a connected skill is removed and no other
  bot is using its connection, the linked credential is cleaned up too rather
  than left dangling.
- **Security advisories** — for skills delivered as MCP servers, Evolve tracks
  published advisories for the underlying package. If an entry has an open
  advisory, the install step surfaces it for review rather than letting you add
  it blind. The pod-wide security view of the same information lives on the
  [Security](security.md) page (MCP Posture).
- **The audit trail** — every install and uninstall is recorded per bot, so you
  can always reconstruct how a bot's capabilities got to where they are.

---

## Common Questions

**What's the difference between the Skills page and the Plugins page?**
Skills is where you *add and remove* abilities; Plugins is where you *manage the
keys and connections* those abilities rely on. They describe the same underlying
plumbing from two angles — capability on one side, credentials and connection
health on the other. Add a skill here; rotate its key there.

**Is a "skill" the same as an OpenClaw plugin or an MCP server?**
Both are skills. OpenClaw plugins and MCP servers are two delivery formats for
the same idea — a capability you give a bot. The catalog mixes them and installs
them the same way; the format only matters when you're reading an entry's
details.

**I installed a skill but the bot can't use it.**
Almost always a credential gap. Installing the skill turns the capability on;
the bot still needs the key or connected account behind it. Open
[Plugins → Credentials](plugins.md) and confirm the key is present and healthy
(and, for channel-style connections, that the gateway picked it up).

**How do I give one bot a capability another bot already has?**
Open the **Installed** matrix, find the skill's row, and click the empty cell
under the bot that's missing it. If it's a connected skill you already authorized
elsewhere, the install reuses what you've approved and skips the redundant steps.

**Why does an MCP-style skill show a security warning before install?**
Evolve checks the skill's underlying package against published advisories. A
warning means there's an open advisory to read first — review it, then decide
whether to install or wait for an upstream fix. The same posture, pod-wide, is on
the [Security](security.md) page.
