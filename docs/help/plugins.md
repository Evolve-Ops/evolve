---
title: "Help: Plugins Page"
slug: plugins
audience: public
last_reviewed: 2026-06-23
concepts:
  - plugins
  - channels
  - credentials
  - embeddings
  - mcp-servers
  - hooks
  - install-provenance
ui_surface: admin.integrations-keys
related_specs: []
---

# Help: Plugins Page

The Plugins page (in the **Operate** bucket) is one place per bot to see and manage how every capability the bot has is *wired up* — the messaging surfaces it's reachable on, the API keys and connected accounts it uses, its embedding providers, the connection details for its MCP servers, and its hook policies. The Channels tab was retired and folded into the Plugins tab as a Messaging section; Plugins is now the source of truth for every OpenClaw plugin entry.

**Add a capability on the Skills page; manage its keys here.** This page is the *keys and connections* surface — the credentials, channels, and per-bot configuration behind your bots' abilities. To browse, install, remove, or scope the abilities themselves, use the [Skills](skills.md) page. Rule of thumb: go to Skills to turn a capability on or off; come here to manage the key or connection it runs on.

**Ask evo from any sub-tab.** The chat widget's suggested prompts shift with
your sub-tab:

- **Plugins** — *"are any plugins missing required modules?"*, *"what's
  drifted from the plugin baseline?"* Evo reads the per-bot plugin inventory
  and can apply baseline-restoring proposals via `proposal_action(action="apply")`.
- **Credentials** — *"which integrations have unhealthy keys?"*, *"are any
  credentials about to expire?"* Evo summarizes the per-bot key inventory.
  *(Direct in-chat key rotation is on the roadmap as `RotateApiKey`; for now
  evo can stage a rotation as a proposal.)*
- **Embeddings** — *"what's the embedding spend look like?"*, *"is the
  embeddings index healthy?"*
- **MCP Servers** — *"is the github MCP server healthy?"*, *"what MCP servers
  are configured?"*
- **Hooks** — *"what hooks are enabled on team-bot-a?"*, *"review the active hook
  policy."*
- **Activity** — *"what changed on plugins this week?"*, *"show recent
  permission changes."*

---

## Bot header chip strip

At the top of the page, just below the bot tabs, a chip strip answers the at-a-glance question "can people reach this bot?":

- **Runtime** — `● OC Gateway · ● Evolve Plugin` chips show whether each is live (green) or down (red). Pulled from the heal-scan probes that run every cycle.
- **Reachable via** — one chip per messaging surface the bot has configured (slack / telegram / discord / whatsapp / …). Same data as the Messaging section of the Plugins tab, summarized.

Both lines pull from `/api/integrations`. Click **⟳ Scan** to refresh now; the chip strip updates without leaving the page.

---

## Sub-tabs

### Plugins

The source-of-truth view for every OpenClaw plugin entry on the bot, grouped into four sections by purpose:

**LLM Providers** — adapter plugins for chat models: `anthropic`, `openai`, `google`, `xai`, `voyage`, `mistral`, `copilot`, `perplexity`. Each row carries a `Credentials →` button that jumps to the Credentials tab and highlights the matching key row — there's exactly one canonical place to rotate keys.

**Messaging** — the surfaces humans use to reach the bot. Slack, Telegram, WhatsApp, Discord, Signal, Matrix, Email, SMS. Each row shows both the plugin's config state ("Enabled") *and* its runtime probe state ("Connected") inline, so you can spot the case where the plugin is enabled but the integration can't reach the service.

Some messaging surfaces live in `openclaw.json → channels.<id>` without a corresponding `plugins.entries` record — that's a valid OpenClaw shape (the channel-block config implicitly engages the channel-handler plugin). Those rows show up with a `channel-block` role badge and the source column reads `channels.<id>` instead of `path`.

**Tools & Capabilities** — functional plugins: `brave` (web search), `memory-core`, `unity`, `dropbox`, and anything else.

**Infrastructure** — platform-required plugins managed by `deploy.py`: `evolve`, `oc_plugin`. Always required; not user-installable.

**Status column** uses plain colored text with an emoji prefix (matches the Credentials table styling):
- `✅ Enabled` — running per the plugin entry
- `❌ Enabled (denied!)` — running but the baseline says it shouldn't be
- `⚠️ Disabled (expected on)` — required or expected by the baseline but turned off
- `⊘ Disabled (intentional)` — explicitly disabled by per-bot override
- `— Disabled` — not enabled, not expected

**Role column** classifies each plugin against the pod baseline (`{shared_dir}/policy/plugin-baseline.json`):
- `required` — pod-wide invariant (every bot must have it). Today: `evolve`, `brave`.
- `expected` — listed in this bot's `per_bot_overrides.additional_plugins`. Bot-specific add.
- `permitted` — in the pod's `common_optional_plugins` allowlist. Allowed but not required.
- `denied` — explicitly forbidden.
- `unexpected` — enabled but not declared anywhere. Curator generator surfaces these as proposals.

Hover the column header for the inline legend.

**Install provenance.** Each row also carries an install-source pill — `built-in`, `signed gallery`, `unsigned (allowed)`, or `unknown` (PR 2284 / PR 2288). Trust is keyed off where the plugin came from, not whether its hash drifted from a snapshot — the old baseline-snapshot drift signals were retired because they fired on every harmless version bump. Today an `unknown` source surfaces a `plugin_unknown_install_source` Signal; everything else stays quiet.

**Buttons:**
- **Adopt allow list…** — creates an `UpdatePluginAllowDeny` proposal that writes this bot's `plugins.allow` to the baseline-expected set. Use this when the bot doesn't yet have a curated allow list.
- **↻ Re-scan** — re-reads the bot's `openclaw.json` and refreshes the inventory cache.
- Per-row: **Disable…** / **Enable…** / **Config…** create proposals through the standard pipeline.

To *install* a new capability (rather than tune one already present), head to the [Skills](skills.md) page — that's where the browsable catalog and per-bot install/uninstall live. This tab is for the inventory and posture of what's already wired.

### Credentials

One row per provider. Key values masked (first 8 + last 4 characters). Writes go to `auth-profiles.json` via the standard staging + `sudo cp` pattern.

**+ Add Key** opens a modal to add or replace a provider key. **↺ Sync** re-reads the file; **↺ Rotate** on a row opens the rotate modal for that provider; **↩ Rollback** restores the previous value where the storage layer supports it (auth-profiles, openclaw-channels).

Cross-links: messaging plugin rows on the Plugins tab and embedding rows on the Embeddings tab carry `Credentials →` buttons that jump here and highlight the matching `<tr>`.

### Embeddings

Embedding providers (used for `memory_search` semantic recall) — distinct from chat models. Resolves to a fallback chain so OpenClaw fails over without Evolve in the loop. Active chain shown at the top; per-provider table below.

- Providers that take an API key (`openai`, `voyage`, `mistral`, `gemini`) show `+ Add key in Credentials` when missing or `↻ Rotate in Credentials` when present — both jump to the Credentials tab.
- Providers without a per-provider key show an inline hint instead: `AWS credential chain`, `Copilot subscription`, `local — no key`, `offline GGUF`.
- **Set as primary** writes a per-bot embedding-chain override. **Configure chain →** jumps to AI Optimization for fuller chain editing.

### MCP Servers

The per-bot inventory of MCP (Model Context Protocol) servers a bot is connected to, read from `openclaw.json → mcp.servers`. Each row is a configured connection: which server it is and how its credentials are bound. This is the connection-and-config view — use it to confirm a bot is wired to the MCP servers you expect and that each one's credential slot is filled. **↻ Re-scan** re-reads the inventory across all bots.

**Browsing, installing, and removing MCP servers lives on the [Skills](skills.md) page**, alongside the rest of the capability catalog — including the capability scoping for suites like Google Workspace and the security-advisory review that runs before an install. This tab is where you see and manage a *connection* once it's in place; Skills is where you add or remove the capability itself. When a server is removed there, its linked credential slot is released if no other bot is still using it, so connections don't leave dangling keys behind.

Spec: `docs/spec-mcp-administration-2026-05-10.md`.

### Hooks

Two hook surfaces per bot, both read from `openclaw.json`:

**Webhook ingress** (`hooks{}` block) — external HTTP endpoints that can trigger agent turns. Token, allowed agent IDs, allowed session-key prefixes, mappings, and an optional `transformsDir` for prebuilt request transforms. Disabled by default; enabling requires explicit operator approval through `UpdateHookBaseline`. The monitor hashes the `transformsDir` contents and flags any drift from the recorded baseline — that directory is a supply-chain surface.

**Plugin typed hooks** (`plugins.entries.<id>.hooks{}`) — per-plugin `allowConversationAccess` and `allowPromptInjection` flags. The evolve plugin needs `allowConversationAccess=true` to receive `agent_end` / `llm_output` events; that's set automatically by `deploy.py`. `allowPromptInjection=true` lets a plugin rewrite the bot's system prompt and is auto-rejected unless the plugin is in the baseline's `trusted_prompt_mutators` allowlist (default: empty).

**Edit baseline…** opens the pod-wide hook policy editor. Per-row actions create proposals through the standard pipeline.

Spec: `docs/spec-hook-governance-2026-05-10.md`.

---

## Common Questions

**The chip strip says "reachable via telegram" but the Plugins → Messaging section is empty — what's wrong?**
That used to be a real bug. Now the Messaging section also surfaces plugins configured via `channels.<id>` even when there's no `plugins.entries.<id>` record — they appear with a `channel-block` role badge and `channels.telegram` in the source column. If you still see an empty Messaging section while the chip strip says reachable, click **↻ Re-scan** on Plugins; the inventory cache is per-bot and may be stale.

**A row says `unexpected` in the Role column — should I worry?**
"Unexpected" just means the plugin isn't declared in the pod baseline or this bot's overrides. It's enabled but the baseline doesn't expect it. Two valid paths: (a) issue an `UpdatePluginBaseline` proposal to add it to the bot's expected set, or (b) disable it if it shouldn't be there. The Plugin Curator generator fires a proposal for any unexpected enable that persists across cycles.

**How do I add a new key?**
Click **+ Add Key** on the Credentials tab, pick the provider, paste the key. Evolve stages the new `auth-profiles.json` to `/tmp` then `sudo cp`s it into the bot's home — the staging dance keeps writes atomic.

**How do I rotate an expiring key?**
On the Credentials tab, click **↻ Rotate** on the row. Or from any other tab, click the row-level **Credentials →** / **Rotate in Credentials** button to jump to Credentials with that row highlighted, then click Rotate.

**How do I install an MCP server (or any new capability)?**
Installing capabilities moved to the [Skills](skills.md) page — browse the catalog, pick a bot, and follow the install flow (which handles capability scoping and a security-advisory check). Once a server is installed there, its connection and credential binding show up on this page's MCP Servers tab for you to manage.

**Why are keys masked?**
Key values are masked (first 8 + last 4) for security — verify you're looking at the right key without exposing the full value. Full value never leaves the bot's home directory.

**I added a key but the bot isn't using it — why?**
The gateway needs to pick up the new value. Restart it from **Maintenance → Status** for the affected bot. For most keys the plugin reads the value on each request; some channel tokens require a gateway restart.

**What's the difference between "Scan" and "Sync"?**
**⟳ Scan** (top of page) re-runs the heal probes — actively pings each integration. **↺ Sync** (Credentials tab) re-reads the local `auth-profiles.json` file without probing external services. Use Scan when something looks stale; use Sync after editing auth-profiles by hand.

**What about Channels — where did that tab go?**
Retired 2026-05-11. Every "channel" in OpenClaw is implemented by a plugin (slack, telegram, etc.), so a separate Channels tab was just a different view of the same data. Messaging is now the top section of the Plugins tab, and the "is the bot reachable?" question is answered by the bot header chip strip at the top of the page.
