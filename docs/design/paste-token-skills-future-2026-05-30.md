# Paste-token skills — how they should work next time

**Status:** Design note. Five paste-token skills were withdrawn from the
Skills catalog on 2026-05-30 (Notion, Linear, Runway, Home Assistant,
Obsidian Vault). This note captures *why* they were dead-ends, *what the
right shape is*, and *the per-skill plan* for bringing each one back as
a working install — so the next person picking this up doesn't reinvent
the design conversation.

---

## What went wrong

The shape that broke:

1. Skills page renders a catalog tile for *Notion* (or Linear / Runway /
   HA / Obsidian).
2. User clicks "Install", pastes a credential.
3. Server POST `/api/skills/install/notion/set-token`:
    * Calls `notion_install.verify_token(token)` → validates against the
      real Notion API. ✅
    * Calls `notion_install.write_config(bot_id, {access_token, ...})` →
      writes `~/<bot>/.openclaw/skills/notion.json`. ✅
    * Returns `{ok: true, status: "active"}`.
4. `evolve_admin.skills.inventory.get_bot_skills` §3 detects the file
   and reports the bot as `configured` for `notion`.
5. Skills tab renders `✓ <bot>` chip. UI looks complete.
6. User asks the bot to do something with Notion. **The bot has no way
   to.** Nothing in `packages/plugin/`, `packages/analyzer/`,
   `packages/gallery/`, or the OpenClaw plugin set reads
   `skills/notion.json`. The credential sits inert on disk.

Every layer reports success. The user blames the bot for being broken
when the install was broken.

`grep -rn "skills/<name>.json" packages/ | grep -v _install.py | grep -v
inventory.py` returned **zero results** for notion, linear, runway,
home_assistant, and obsidian_vault — the proof that no consumer exists.

iMessage was the only "filesystem skill" with a real consumer
(`analyzer.imessage_plugin.poller`), which is why it stayed.

## The shape that's right

The right pattern for skills that need to ride on a third-party API is
an **MCP server install** that wires through `mcp.servers.<id>` in
`openclaw.json`, with credentials living in the pod keystore and
referenced by env-binding:

```
                        ┌─────────────────────────────────────────┐
  Skills tab            │  packages/analyzer/mcp_admin/catalog.py │
  "+ Add to <bot>" ───► │  CatalogEntry(id="notion", ...)         │
                        │   command="npx",                        │
                        │   args=["-y", "@notionhq/notion-mcp-server"],
                        │   required_env=[                        │
                        │     RequiredEnv(name="NOTION_TOKEN",    │
                        │       keystore_hint="notion-*")],       │
                        │   advertised_tools=["search_pages",     │
                        │     "create_page", "read_page", ...]),  │
                        └─────────────────────────────────────────┘
                                       │
                                       ▼
  POST /api/mcp-admin/install
    body: { bot_id, server_id, catalog_id: "notion",
            env_bindings: {NOTION_TOKEN: "keystore:notion-<bot>"},
            token_values: {"notion-<bot>": "secret_..."} }
                                       │
                                       ▼
  Keystore.register("notion-<bot>", value=...)         ← real secret store
  InstallMcpServer proposal (auto-applied)             ← single proposal path
                                       │
                                       ▼
  bot's openclaw.json::mcp.servers.notion              ← gateway loads it
  Gateway kickstart                                    ← takes effect immediately
```

This is the **same pipeline the MCP Servers tab uses today** (the
GitHub MCP server install at
[server.py:21202](../../packages/admin/evolve_admin/web/server.py:21202)
is the working reference). It already handles:

- **Credential storage** in the keystore, not in `openclaw.json` as
  cleartext.
- **Atomic install** via the InstallMcpServer applier.
- **Gateway kickstart** so the server is live on next turn.
- **Detection** via the §2 MCP-server inventory branch (which reads
  `mcp.servers.<id>` — a real loader-side artefact, not a §3
  consumer-side marker).
- **Revocation** via UninstallMcpServer.

What's missing is a per-skill **wrapper route** that converts the
Skills-page "Install Notion" click into the right MCP install call —
picking the catalog entry, naming the keystore slot, and presenting the
plain-language access panel.

## Per-skill plans

Each row is what needs to ship before the corresponding Skills-tab tile
can come back. The MCP server has to be vetted and added to
[catalog.py::default_entries()](../../packages/analyzer/mcp_admin/catalog.py)
first; only then does the per-skill wrapper become safe to wire.

| Skill          | Vetted MCP candidate                          | Status | Notes |
|----------------|-----------------------------------------------|--------|-------|
| **Notion**     | `@notionhq/notion-mcp-server`                 | Not vetted | First-party from Notion. Self-hostable, open source. Permissions: list_pages, retrieve_page, create_page, query_database, append_block. Vetting needs license check + scope-recommendation note. |
| **Linear**     | `mcp-server-linear` (community) or wait for first-party | Not vetted | Several community implementations exist; none are first-party as of 2026-05-30. Watch for a Linear-team-published server before picking a community one — bad MCP server = arbitrary code with bot's API key. |
| **Runway**     | None known                                    | Blocked | No MCP server for Runway exists yet. Skill stays withdrawn until one ships. The Python module's verify_token still proves the key works, but there's no install destination. |
| **Home Assistant** | `homeassistant-mcp` (community) or `hass-mcp` | Not vetted | Multiple community implementations. Vetting needs to check whether they're scope-limited (read-only vs. control-everything) — HA is high-risk because the bot could turn devices on/off. Default scope must be read-only with an explicit opt-in for control. |
| **Obsidian**   | `@modelcontextprotocol/server-filesystem` scoped to the vault | ✅ **Shipped 2026-05-30** | Reference impl. `POST /api/skills/install/obsidian/set-vault-path` validates the vault path, ACL-grants the bot user read or read+write on the vault, then installs `mcp.servers.obsidian` via the existing `InstallMcpServer` applier with `catalog_id="filesystem"` and `extra_args=[vault_path]`. The read/read_write toggle is enforced at the OS-permission layer (filesystem MCP still advertises `write_file`; the kernel returns EACCES in read mode). See `obsidian_install.grant_vault_acl` + `obsidian_install.resolve_status_mcp`. |

Obsidian shipped 2026-05-30 as the worked example. The other four still
need either an MCP-server vetting pass or, in Runway's case, for the
ecosystem to produce a server at all.

### What the Obsidian impl proves out for the next four

The Obsidian PR establishes the load-bearing pieces all four future
revivals will reuse:

1. **`InstallMcpServer.extra_args`** ([packages/analyzer/schema/proposal.py:344](../../packages/analyzer/schema/proposal.py:344))
   — per-install positional args appended to the catalog entry's `args`,
   flowing through to the real binary via the wrapper script's
   trailing `"$@"`. Notion/Linear/HA's per-bot config (workspace id,
   instance URL, etc.) can land here too.

2. **`/api/mcp-admin/install` accepts `extra_args`**
   ([packages/admin/evolve_admin/web/server.py:20695](../../packages/admin/evolve_admin/web/server.py:20695))
   — the public route gained an `extra_args: [str, ...]` field with
   type validation. Any future per-skill wrapper can just include it
   in the body.

3. **The wrapper-route pattern** — `/api/skills/install/obsidian/set-vault-path`
   is ~150 lines: validate input → grant filesystem-side permissions →
   call into `_create_obsidian_mcp_proposal` with the right
   `catalog_id` + `extra_args` → persist a mode marker → re-resolve
   status. The same shape (validate → grant → install → persist)
   should serve Notion / Linear / HA wrappers.

4. **Mode marker pattern** — when the install needs to record state
   that the MCP applier doesn't (e.g. "is this install read-only?"),
   a sidecar `~/.openclaw/skills/<id>.json` is the storage. **Critical:
   the marker must NOT trigger `inventory.py` §3 detection** — that's
   the dead-end pattern. The marker is consulted only by the status
   resolver via an injected `read_marker` callable; inventory looks at
   `mcp.servers.<id>` (§2) for the "configured" signal.

5. **Mode-aware access panel** —
   `access_panel_for("read") / access_panel_for("read_write")` produce
   distinct will/wont lists. HA in particular will need this for the
   "control my devices" vs "just read state" toggle.

## Open design questions

These need answers before the wrapper route ships:

1. **Per-bot vs. per-pod credentials.** Notion / Linear / Runway / HA
   tokens are user-scoped — they belong to a specific human. Should the
   keystore slot be `notion-<bot>` (per-bot, lets each bot have a
   different Notion workspace) or `notion-shared` (one credential, all
   bots see the same workspace)? Telegram is per-bot, Slack is per-bot
   (workspace), Discord is pod-wide. No single right answer; per-skill
   decision.

2. **Default scope for high-risk skills.** Home Assistant can control
   physical devices. The default install should be read-only ("list
   devices, see state") with control as an explicit toggle. Where does
   that toggle live in the install flow? Probably the access-panel
   step's confirmation screen.

3. **What happens to existing `~/.openclaw/skills/<x>.json` files?**
   On bots that had a paste-token install before the 2026-05-30
   withdrawal, the credential file is still on disk (the withdrawal
   only removed the routes, not the files). Options:
   - Leave them — dormant, no surface lists them, but credential is
     still in plaintext on the bot's home.
   - Sweep them on next deploy with a `safe_upgrade` pass that moves
     them to a `withdrawn/` subdirectory.
   - Migrate them automatically into the keystore when the MCP wrapper
     ships, so the user doesn't have to re-paste.

   Recommend: leave for now, document in the migration plan when the
   MCP wrapper ships.

4. **Gallery manifests that reference withdrawn skills.** Any
   `gallery/*/<pkg>.json` whose `requirements.integrations[].id` is one
   of the five withdrawn ids will now permanently fail preflight —
   the orchestrator returns `awaiting_oauth` with `missing` populated,
   but the user has no install path. Need to grep gallery manifests and
   either:
   - Mark the requirement `required: false` for now.
   - Remove the requirement and document that the app degrades
     gracefully.
   - Hold the app out of the gallery until the corresponding MCP
     wrapper ships.

## Criteria for putting a skill back

A withdrawn skill comes back when **all** of:

1. The corresponding MCP server is added to
   [catalog.py::default_entries()](../../packages/analyzer/mcp_admin/catalog.py)
   with `vetting_status="approved"` and a real `vetting_notes` entry
   (who reviewed it, when, what scope is safe).
2. A wrapper route at `/api/skills/install/<id>` exists that:
   - Renders the existing access panel from the install module.
   - Collects the credential (using the install module's
     `verify_token`).
   - Calls `/api/mcp-admin/install` internally with the right
     `catalog_id`, `env_bindings`, and `token_values`.
   - Surfaces the keystore-slot decision (per-bot vs per-pod).
3. A test asserts the install flow produces a working
   `mcp.servers.<id>` entry in `openclaw.json` and that the gateway
   kickstart fires.
4. Manual verification on the mini: ssh in, run the install, confirm
   the bot can actually call an MCP tool from the server.

The withdrawal note in each install module's docstring (e.g.
[notion_install.py](../../packages/admin/evolve_admin/skills/notion_install.py))
points back here so anyone tempted to re-route the legacy paste-token
endpoint sees the design before they ship the bug again.

## What's NOT in this design

- Re-implementing the paste-token write-to-filesystem path with a
  different runtime consumer (an Evolve-side daemon that polls
  `skills/<id>.json` and proxies API calls). That would work but
  duplicates what MCP already does, and Evolve's substrate strategy
  is "build for MCP, design abstractions around it" — adding a
  parallel runtime would be a cul-de-sac.
- Hiding the catalog tiles in the UI but keeping the install endpoints
  alive as feature-flagged. The endpoints lied to users when they
  succeeded; keeping them latent invites silent revival.
- A general "paste-token skill" framework. Each MCP server has its own
  shape (env vars, install args, scopes); the wrapper-route pattern is
  thin enough that a framework adds more friction than it removes.
