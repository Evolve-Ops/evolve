# Admin UI — Integrations restructure (2026-05-10)

Status: **draft** (design lock pending operator review; implementation lands in phases alongside the specs that fill each new sub-tab).

**What this is.** The admin UI today has a single "Integrations & Keys" page that stacks three vertical sections (Channels & Runtime, API Keys, Embedding Providers) under a per-bot tab bar. The OpenClaw admin-coverage roadmap ([roadmap-openclaw-admin-coverage-2026-05-10.md](roadmap-openclaw-admin-coverage-2026-05-10.md)) adds MCP servers, hook governance, plugin inventory, and sandbox config as new admin-relevant capability surfaces. Bolting each one on as another stacked section makes the page unscannable. This spec defines a single restructure that absorbs all of them, plus the corresponding split with the Security tab so configuration and posture views stay distinct.

**Naming.** "Integrations & Keys" → "Integrations" (just one word). The "& Keys" qualifier was load-bearing only because the page also held credentials; the new structure puts credentials in their own sub-tab, so the broader noun is enough.

**Relationship to other specs.**
- [roadmap-openclaw-admin-coverage-2026-05-10.md](roadmap-openclaw-admin-coverage-2026-05-10.md) — items 1.1–1.5 + 2.3 are the new surfaces this restructure absorbs.
- [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) — first concrete consumer of the new structure; §7 of that spec defers to this one.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — the principle of separating "where I see what's wrong" (Alerts/Security) from "where I configure things" (Integrations) is the same one driving that consolidation.

---

## 1. The problem

**Three sections on Integrations & Keys today, ~7 a year from now.** The current page ([packages/admin/evolve_admin/web/index.html:1128](../packages/admin/evolve_admin/web/index.html)) has:

1. Channels & Runtime — messaging channels (slack/telegram/discord/email), the OC gateway, the Evolve plugin runtime
2. API Keys — provider credentials, one row per provider
3. Embedding Providers — recently added (PR #913)

The roadmap adds:

4. MCP Servers — Tier 1.1 (spec drafted)
5. Plugins — Tier 1.4 (first-party OpenClaw plugins inventory)
6. Hooks — Tier 1.2
7. Sandbox — Tier 2.3

At seven stacked vertical sections on one page, operators stop scanning and start hunting. The bot-tab bar at the top stays useful (the per-bot mental model is right) but the inside of each bot's view needs structure.

**Configuration and posture are getting muddled.** The current page mixes "configure this bot" with "what's wrong" (channel status badges, missing-key warnings). For three sections this is bearable. For seven it's noise. The Security tab has the same problem incoming — MCP CVE matches, hook drift signals, permission-mode drift would each add posture sub-sections.

The structural principle that resolved this for the Alerts/Signal-Store consolidation applies here: separate "where I configure things" from "where I see what's wrong." This spec extends that principle to the capability surfaces.

---

## 2. Core reframe: Integrations is config; Security is posture

Two tabs, two operator questions:

| Tab | Question it answers | Shape |
|---|---|---|
| **Integrations** | "How is this *one bot* set up?" | Per-bot view with sub-tabs across capability surfaces |
| **Security** | "Where is the *whole pod* exposed?" | Cross-bot matrices, drift signals, CVE matches, audit findings |

The two are cross-linked. Clicking a red cell in the Security matrix jumps to the corresponding Integrations sub-tab on that bot, with the relevant row highlighted. Adding a new MCP server in Integrations runs through the Proposal pipeline whose history surfaces in Security.

---

## 3. The new Integrations tab

### 3.1 Structure

The bot-tab bar stays at the top — that's the right axis and operators already know it. Each bot's content area gains a sub-tab bar:

```
[ team-bot-a | team-bot-c | personal-bot | admin-bot | security-bot | team-bot-b | evolve ]      ← bot tabs (existing)

[ Channels | Credentials | Embeddings | MCP Servers | Plugins | Hooks | Execution & Cron ]   ← sub-tabs (new)

(content area, one capability surface at a time)
```

Sub-tab pattern is the same one already used in Self-Improvement, Analytics, and Maintenance. No new component model.

### 3.2 Sub-tab inventory

Each row notes what lands today, what's deferred to a later spec, and the data source. "Per-bot" means the view scopes to the active bot tab; cross-bot views live in Security.

| Sub-tab | Today | Future | Source |
|---|---|---|---|
| **Channels** | Slack / Telegram / Discord / Email channel status. OC gateway and Evolve plugin runtime status. | Channel-specific config (auto-reply, rate limits) as those features land. | Existing `/api/integrations` + `/api/kaizen` routes |
| **Credentials** | API keys per provider (today's "API Keys" section). Masked values, "Add Key" / "Sync" actions. Pulls keystore behind the scenes. | Cross-references showing which surface uses each key (e.g. "GITHUB_TOKEN: used by the github-mcp install on team-bot-a and the github plugin on admin-bot"). | Existing keystore + auth-profiles.json |
| **Embeddings** | Embedding-provider selection per bot, health, model identity. | Could fold into Credentials in a future cleanup. Keep separate for now since the model-tier UX is distinct. | Existing `/api/embedding-providers` |
| **MCP Servers** | (Phase A) Read-only list of any configured `mcp.servers` entries on the bot with allowlist status. | (Phase B) Catalog browser + install workflow + per-server config + per-server "test connection" button. | [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) |
| **Plugins** | Plugin entries table with baseline match indicators, allow/deny lists panel, load paths panel. Bulk-propose-allow-list action for bots missing the list. | (Phase B) per-row enable/disable proposals, allow-list editor, load-path editor. (Phase C) marketplace install workflow. | [spec-plugin-inventory-2026-05-10.md](spec-plugin-inventory-2026-05-10.md) |
| **Hooks** | Read-only enumeration of hooks configured per event type (PreToolUse, SessionStart, etc.) with content fingerprint vs. baseline. | Edit hooks via proposal; pod-wide baseline management. | Tier 1.2 of the roadmap; spec TBD |
| **Execution & Cron** *(renamed from "Sandbox" by [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) §7.1)* | Composite posture badge (tight/moderate/wide/open). Permission-config field table vs baseline. Exec-approvals panel with canonical patterns. Cron jobs table with cap and denylist badges. | Per-row "Propose Change" actions, per-pattern approve/revoke, cron add/edit/remove. | Tier 1.3 + 2.3 + 2.5 of the roadmap; spec drafted |

The phasing column is intentional: each new sub-tab can land empty/read-only when the corresponding spec drafts its inventory monitor, then gain actions when the spec's install/config flow lands. No new sub-tab requires the full feature stack on first appearance.

### 3.3 What's NOT in Integrations

Three things move *out* of the current Integrations & Keys page or never enter the new one:

- **Cross-bot security matrices** (which bot has which MCP server, which bots have which hooks). Those are pod-wide questions; they live in Security.
- **Signals / alerts** (channel down, credential expired, drift detected). Those route through the Alerts page; Integrations links *to* the relevant alert but doesn't host the alert state itself.
- **Self-Improvement proposals** (install MCP server, edit hook, rotate key). Those flow through the Self-Improvement tab as part of the existing pipeline. Integrations is where the install *button* lives, not where the proposal queue lives.

### 3.4 The Add affordance per sub-tab

Each sub-tab has a primary action button in the top-right:

- Channels: "Add Channel" (existing)
- Credentials: "Add Key" (existing)
- Embeddings: "Configure Provider" (existing)
- MCP Servers: "Install from Catalog" → opens the catalog browser in a modal
- Plugins: "Enable Plugin" (when Tier 1.4 lands)
- Hooks: "Add Hook" (when Tier 1.2 lands)
- Sandbox: "Edit Allowlist" (when Tier 2.3 lands)

Consistent placement; operators don't relearn the affordance per sub-tab.

---

## 4. The Security tab posture additions

Security today owns: identity audit (file hashes), config audit (gateway/exec/sudoers), machine audit (firewall/SSH/ports), proposal volume audit. Posture findings already render here.

The roadmap adds posture surfaces:

| Posture sub-section | What it shows | From |
|---|---|---|
| **MCP Posture** | Bot×server matrix. Red = unknown server; yellow = config drift / unhealthy / CVE match; green = approved + green probe. Scope-drift findings list. CVE-match list. | [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) §3.4 |
| **Hook Posture** | Bot×event-type matrix. Red = unexpected hook; yellow = content mismatch vs. baseline. | Roadmap Tier 1.2 |
| **Permission Posture** | Bot × posture-axis matrix (Execution / Filesystem / Web / Sandbox / Scheduled), per-bot composite score badge, denylist matches panel, uncapped-cron panel. Cross-link to Integrations → Execution & Cron. | [spec-permission-posture-2026-05-10.md](spec-permission-posture-2026-05-10.md) (absorbs Roadmap Tier 1.3 + 2.3 + 2.5) |
| **Plugin Posture** | Bot×plugin matrix. Red = required-missing / denied-present / unexpected-enabled. Allow-list-missing badge, command-gate badge, load-path-unexpected badge. Bulk-propose action for missing allow lists. | [spec-plugin-inventory-2026-05-10.md](spec-plugin-inventory-2026-05-10.md) |
| **Skill / Subagent Drift** | Inventory of skills and subagent definitions per bot; flags divergence from pod baseline. | Roadmap Tier 3 |
| **Content Scan** | Per-bot summary cards (files scanned, matches, highest severity); per-file detail with match excerpts; suppression list; pattern catalog browser. Renders alongside the existing Identity audit findings with cross-links. | [spec-prompt-injection-scanner-2026-05-10.md](spec-prompt-injection-scanner-2026-05-10.md) |

These sub-sections render only when the corresponding spec's monitor is live. Sub-sections with no signals render compact ("MCP Posture — all green, 0 servers configured"). The cross-link from a red cell to the Integrations sub-tab on that bot is the primary navigation between the two views.

The existing Security sub-sections (Identity / Config / Machine audit) stay where they are.

---

## 5. Migration plan

The restructure ships in three phases. Each phase is a clean stopping point.

### 5.1 Phase 1 — Rename + sub-tab the existing content (no new functionality)

- Rename nav item: "Integrations & Keys" → "Integrations".
- Wrap the existing three sections in sub-tabs: Channels, Credentials, Embeddings.
- Move provider-adapter plugins (anthropic, openai, google, xai) out of the "Channels & Runtime" mixed section: messaging-only stays in Channels; provider adapters move to a placeholder Plugins sub-tab (read-only listing for now).
- Keep all existing routes / data sources / handlers identical. The change is purely DOM reorganization.
- Add a redirect / banner on the old page name for ~2 weeks so muscle memory adapts.

Size: ~1 day. Zero data-model change.

### 5.2 Phase 2 — Add MCP Servers sub-tab and Security MCP Posture (lands with MCP spec Phase A)

- Add empty MCP Servers sub-tab on Integrations: read-only inventory list pulled from `{shared_dir}/mcp/inventory/<bot>.json`.
- Add MCP Posture section on Security: bot×server matrix + "no servers configured" baseline.
- Wire the cross-link from a red Security cell to the corresponding bot's MCP Servers sub-tab.

Lands as part of [spec-mcp-administration-2026-05-10.md](spec-mcp-administration-2026-05-10.md) Phase A; no work charged to this spec beyond ensuring the UI scaffolding is in place.

### 5.3 Phase 3 — Subsequent surfaces (lands with each roadmap spec)

- Plugins sub-tab gains actions when Tier 1.4 spec lands.
- Hooks sub-tab + Security Hook Posture lands with Tier 1.2 spec.
- Sandbox sub-tab + Security Permission Posture lands with Tier 1.3 + 2.3 specs.

Each subsequent sub-tab follows the same pattern: read-only inventory in the matching spec's Phase A, edit actions in its Phase B.

---

## 6. Cross-cutting decisions

### 6.1 Bot-tab consistency

The bot-tab order, styling, and selection state stay identical across all sub-tabs. Switching sub-tabs preserves the active bot. Switching bots preserves the active sub-tab. This is the principle that lets operators build muscle memory.

### 6.2 Empty-state phrasing

Each sub-tab needs a sensible empty state for bots that don't use it. Examples:

- MCP Servers on a bot with no servers: "No MCP servers configured. Install from catalog to add one." (with the catalog action)
- Hooks on a bot with no custom hooks: "Default hooks only. Override via Self-Improvement → Propose a hook change."
- Sandbox on a bot with no explicit sandbox: "Default sandbox config. View pod baseline."

Empty states are themselves a posture signal — silent isn't the same as nothing-to-show.

### 6.3 Read-only baseline

Phase 1 ships entirely read-only for existing sub-tabs because the underlying handlers already are. New sub-tabs (MCP, Plugins, Hooks, Sandbox) also ship read-only first and gain write actions only when the spec for that surface has its applier landed and tested. This prevents operators from acting through a UI whose Apply pipeline isn't ready.

### 6.4 Routing and URL hygiene

Sub-tabs gain URL fragments: `/admin/#/integrations/<bot>/<surface>`. Today's `/admin/#/integrations-keys` redirects to `/admin/#/integrations/<active-bot>/channels`. Operators can deep-link to a specific bot+surface; the alerting code that links from a Signal to its origin gets a stable target.

### 6.5 Per-bot scoping vs. pod-wide views

A few capability surfaces have a meaningful "pod default" view alongside the per-bot view (notably Hooks and Sandbox, where pod-wide baselines matter). Those sub-tabs render a "Pod baseline" entry alongside the bot tabs — clicking it switches the content area to the pod-wide view. Mirrors the way the existing POD_CONDUCT view works.

---

## 7. Non-goals

- **Not changing the Alerts page.** Alerts is the home for "what's wrong right now" by user-facing severity; Security is the home for "what's the posture across the pod" by audit category; Integrations is the home for "configure one bot." The three coexist.
- **Not modeling sub-agents / skills / slash commands under Integrations.** Those are bot-internal behaviors, not external capabilities. They live under the Modules tab (existing) or the per-bot config view when their inventory monitor lands.
- **Not refactoring the existing route handlers.** Phase 1 is DOM-only. The `/api/integrations` routes keep their shape. Future cleanup is opportunistic.

---

## 8. Open questions

1. **Embeddings sub-tab vs. Credentials sub-tab.** Embeddings could plausibly live under Credentials (it *is* a credentialed external service). Today's separate placement reflects how recently it was added. Decide whether to keep separate or fold. Doesn't block Phase 1; revisit before Phase 3.

2. **Provider plugins (anthropic, openai, brave, google, xai) — sub-tab placement.** These appear in the current page's "Channels & Runtime" section mixed with messaging channels. Phase 1 moves them to a Plugins sub-tab. The catch: a "GitHub MCP server" and a "GitHub plugin" could both exist on the same bot, and operators might want them adjacent. Two options: (a) keep them in separate sub-tabs as proposed; (b) introduce a per-provider grouping view that surfaces both. Probably (a) for now; revisit after we see real usage.

3. **Mobile / narrow viewport.** Seven sub-tabs is a lot horizontally. At narrow widths the sub-tab bar should collapse to a dropdown. Standard responsive treatment; called out so it isn't forgotten.

4. **What to do with the "Reports" placeholder.** The current Reports tab is hidden (`style="display:none"`) per the alerts/signal-store consolidation. Phase 1 is a clean moment to remove the dead nav item entirely.

---

## 9. Test strategy summary

- Phase 1 is primarily a snapshot test: existing page visible content stays accessible from the new sub-tab structure. No behavioral change.
- Each subsequent phase tests the sub-tab → handler wiring with a synthetic fixture for that surface's inventory file.
- Cross-link tests: clicking a red Security cell lands on the expected Integrations sub-tab with the expected bot active.
- URL deep-link tests: each `/admin/#/integrations/<bot>/<surface>` lands correctly and roundtrips through nav.

No new fixture infrastructure required beyond what the per-surface specs already need.
