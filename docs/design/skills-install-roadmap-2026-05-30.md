# Skills install roadmap — 2026-05-30

**What this is.** Standing roadmap for the Skills install surface. Two
buckets — "skills to revive (or extend)" and "platform improvements" —
each scored for effort + priority, with acceptance criteria. Updated
as items ship (see [Shipped](#shipped) at the bottom).

**Context.** Born out of the three-PR sweep on 2026-05-30:
- [#1812](https://github.com/pod_admin/evolve/pull/1812) — audit + lying-hint fixes (merged)
- [#1814](https://github.com/pod_admin/evolve/pull/1814) — withdraw 5 paste-token dead-ends (merged)
- [#1817](https://github.com/pod_admin/evolve/pull/1817) — Obsidian rewire as reference impl for MCP-server-backed installs (merged)

Backing docs:
- [paste-token-skills-future-2026-05-30.md](paste-token-skills-future-2026-05-30.md)
- [skills-install-audit-2026-05-30.md](skills-install-audit-2026-05-30.md) — **end-of-roadmap audit**: pattern correctness per shipped skill + cross-cutting findings (F1–F6) + prioritized follow-up tasks
- [skills-deep-audit-2026-05-30.md](../skills-deep-audit-2026-05-30.md) — **deep audit**: 7-point end-to-end check per skill via multi-agent workflow; surfaced 4 pre-launch blockers (P0-1 through P0-4) shipped as PRs #1836 / #1837 / #1839 / #1844

---

## Skills to revive

Following the Obsidian template established in #1817:
**catalog wrapper → MCP server install with `extra_args` → wrapper route
with whatever toggle that skill needs**. Memory: `project_obsidian_mcp_install_pattern`.

| # | Skill | MCP candidate | Status | Effort | Confidence | Notes |
|---|---|---|---|---|---|---|
| 1 | **Dropbox** | `@modelcontextprotocol/server-filesystem` scoped to `~/Dropbox` | ✅ Shipped 2026-05-30 | S | High | Line-for-line port of Obsidian — same filesystem MCP, same read/read+write toggle, swap `vault_path` for `dropbox_path`. Auto-detects path via `~/.dropbox/info.json`. Removed the §6 inventory false-positive (3 bots that showed "configured" from desktop-client detection alone). |
| 2 | **Notion** | `@notionhq/notion-mcp-server` (first-party) | ✅ Shipped 2026-05-30 | M | High | First MCP rewire that's NOT filesystem-backed — exercised the `env_bindings` + keystore + JSON-encoded `OPENAPI_MCP_HEADERS` shape that Linear / HA will reuse. Per-bot keystore slot (`notion-<bot>`), `verify_token` runs before the keystore write, rollback on proposal failure. Access panel includes a `post_install_callout` reminding users about Notion's per-page-sharing model. |
| 3 | **GitHub-via-MCP** (purpose 2) | `@modelcontextprotocol/server-github` (in catalog already) | ✅ Shipped 2026-05-30 | S | High | New module `github_install.py` (purpose 2; purpose 1 stays in upstream_plugin_skills) + wrapper route `/api/skills/install/github/install-mcp-server` follows the Notion pattern: validate PAT against `/user`, save to keystore `github-<bot>`, install MCP with `catalog_id="github"` + `env_bindings={GITHUB_TOKEN: keystore:github-<bot>}`. Purpose independence preserved — MCP install does NOT touch `.git/config` backup PAT. **Known follow-up**: catalog entry uses deprecated `@modelcontextprotocol/server-github` (Anthropic deprecated in favor of GitHub-owned packages); swap is a separate decision. |
| 4 | **Linear** | `linear-mcp` by dvcrn (community, MIT) | ✅ Shipped 2026-05-30 | M | Medium | Second API-key MCP install (after Notion). Differences from Notion: verbatim PAT in keystore (no JSON-encoded headers blob — `linear-mcp` reads a plain `LINEAR_API_KEY` env var), and post_install_callout warns about identity (the bot acts as the API key holder). Catalog vetting_status is **candidate**, not approved — community package by dvcrn, source-reviewed but no real-bot runtime hours yet. Bump to approved after ~2 weeks of clean use; pin a specific version on production-critical bots until then. Audited 2026-05-30 — Linear is NOT in OC's `dist/extensions/`, so MCP is the right pattern (vs bundled-plugin used for Runway). |
| 5 | **Home Assistant** | `hass-mcp` (hannoeru/MIT) likely best; alternatives below | Vetting done; scope-toggle design BLOCKING | L | Medium | Vetting (2026-05-30): NOT bundled in OC (confirmed via 95-extension audit). Candidates surveyed: **hass-mcp** by hannoeru (MIT, 0.1.7, 37KB, official @modelcontextprotocol/sdk + home-assistant-js-websocket, 3-month-old, cleanest); ha-mcp by jgracey (Aug 2025, older); home-mcp by shenjingnan (Feb 2026, fresh but newer/less proven); @coolver/home-assistant-mcp (broad IDE-focused tool, too wide for our use); @rezti/homeassistant-mcp-plus (Aug 2025, extended query). **Blocking issue**: HA controls physical devices → default must be read-only. HA's long-lived tokens are user-scoped, NOT per-permission. Three design paths: (1) **token-level**: require user to mint token from a read-only HA user account (admin friction); (2) **MCP-server-level**: pick/fork a server that takes a tools-allowlist env var, install with allowlist for read mode + full toolset for control mode (need to verify hass-mcp supports this); (3) **wrapper proxy**: Evolve-side MCP middleware that filters tool calls — most flexible, most code. Use Obsidian's mode-marker shape for the UI regardless of which path wins. Implementation deferred until the scope-toggle design is settled — don't ship a write-by-default HA install. |
| 6 | **Runway** | **N/A — bundled OC plugin** | ✅ Shipped 2026-05-30 | M | High | **Pattern correction**: Runway doesn't need an MCP server. OC ships `@openclaw/runway-provider` bundled internally as a `videoGenerationProviders` contract. Established a NEW install pattern alongside MCP: write `auth-profiles.json` (mirrors how Google OAuth profiles are stored) + write `agents.defaults.videoGenerationModel.primary` in openclaw.json + kickstart. No keystore, no MCP server. Same pattern will serve any future bundled OC provider (Google Veo, Synthesia if shipped, etc). TeamBotA demonstrated Runway works pre-Evolve via the same plugin. |

### Acceptance criteria (shared across revivals)

A skill is "back" when **all** of:

1. MCP server is in [catalog.py::default_entries()](../../packages/analyzer/mcp_admin/catalog.py) with `vetting_status="approved"` and real `vetting_notes` (who reviewed, when, scope recommendation).
2. Wrapper route at `/api/skills/install/<id>/set-<field>` validates input, grants whatever permissions are needed (filesystem ACL for fs skills; keystore slot for API skills), calls `InstallMcpServer` with the right `catalog_id` + `extra_args` + `env_bindings`, persists a mode marker.
3. Unit tests for the helpers (validate, grant/revoke, mode marker, status resolver, access panel per mode).
4. Route integration tests: bad input rejected, happy path produces the right proposal shape, permission failure does NOT create the proposal.
5. Re-added to `/api/skills/catalog` + `/api/skills/catalog/<id>` + `/api/skills/install/<id>/status` + `/api/skills/install/<id>` POST.
6. Per the existing pattern: the install module's docstring carries a `.. note::` block describing the install path; the install module itself stays even if the wrapper route grows (keeps verify_token et al. reusable).
7. Manual verification on the mini: ssh in, run the install, confirm the bot can actually call a tool from the MCP server.

---

## Platform improvements

Ranked by effort vs payoff. Most are independent of the revivals and can
land in parallel.

| # | Improvement | Effort | Payoff | Notes |
|---|---|---|---|---|
| P1 | **Kickstart the gateway on GOG OAuth callback** | XS | Removes "install succeeded but bot can't see it until next deploy" reports. | Slack and Discord kickstart; GOG doesn't. ~5-line fix at [server.py:16671](../../packages/admin/evolve_admin/web/server.py:16671). |
| P2 | **Apple Local / AutoCAD inventory detection** | S | Skills tab can answer "is apple_local installed on bot X?" — today the matrix is silent. | Add §7/§8 branches to inventory.py's `get_bot_skills`. Apple Local probes via AppleScript already exist in `apple_local_install.py::probe_*`. |
| P3 | **Obsidian/Dropbox mode-change endpoint** | S | Switching read↔read+write currently requires uninstall + reinstall. Reusable for HA's read/control toggle. | New `/api/skills/install/obsidian/set-mode` (and equivalent for dropbox) that just re-runs `grant_vault_acl` with the new mode and updates the marker. |
| P4 | **`gallery/note-taker` manifest integration_id fix** | S | App declares `obsidian` as required integration but the rewired install registers as `obsidian_vault`. Need to verify preflight handles the alias OR rename the manifest reference. | Audit + fix in `gallery/note-taker/p-f14e9562.json`. |
| P5 | **Remove dead github `install_hint` code path** | XS | Cleanliness — github skill now short-circuits to wizard before reaching `install_hint`. The text is misleading but harmless. | Either delete the field for github or convert it to a fallback note in code comments only. |
| P6 | **Inventory detection unification** (from yesterday's audit) | M | Closes the false-positive class: one team-bot-Telegram (§4), 3 dropbox (§6 — drops automatically with Dropbox revival), 7 github (§5). | (a) §3 should require loader wiring — Obsidian PR established the pattern; apply to telegram §4 too. (b) §4 should honor `channels.<x>.enabled`. (c) §5 should require an actual github plugin/MCP entry. (d) §6 should require an actual dropbox plugin/MCP entry. |
| P7 | **Per-bot skill health pings** | M | Catches silent breakage (revoked token, deleted vault, etc.) before user notices. | Cron-style routine that, for each (bot, installed skill), makes a benign capability check (Telegram getMe, Obsidian `list_directory(vault_path)`, Slack auth.test, etc.). Surfaces failures as Signals in the existing alerts pipeline. |
| P8 | **Skill install canonical pattern doc** | S | Onboards future contributors; documents the 5-piece pattern from the Obsidian impl. | Extract from [paste-token-skills-future-2026-05-30.md](paste-token-skills-future-2026-05-30.md) and memory `project_obsidian_mcp_install_pattern` into `docs/contributing-skills.md`. |
| P9 | **Withdrawn-marker-file cleanup** | S | Bots that ran pre-withdrawal paste-token installs have dormant credential files at `~/.openclaw/skills/*.json`. No surface lists them, no code reads them, but the credential is still in plaintext on disk. | Sweep on next deploy: move them to `~/.openclaw/skills/withdrawn/`. Open Q #3 in design doc. |
| P10 | **File OC upstream issue for tool-denylist** | XS to file, blocking on upstream | The OS-permission-layer mode toggle works for Obsidian / Dropbox (filesystem MCPs) but isn't general. HA in particular needs per-tool denial (`exec_service` vs `read_state`). | File `mcp.servers[name].toolDenylist` issue against openclaw/openclaw. Until then, HA's mode toggle has to wrap the MCP server with a tool-filtering proxy. |

---

## Suggested ship order

Each row independent; pick by what you have time for.

**Status as of 2026-05-30 evening (post-Linear)**:
- Skill revivals: 6 of 6 shipped (Obsidian / Dropbox / Notion / GitHub-MCP / Runway / Linear). HA alone remains — blocked on the tools-allowlist scope-toggle design (see end-of-roadmap audit).
- Platform improvements: 7 of 10 shipped. P6 (inventory unification), P7 (health pings), P9 (marker cleanup) remain.

Updated suggested order for what's left:

1. **Pre-launch fix sprint (Phase 1)** — see [skills-deep-audit-2026-05-30.md](skills-deep-audit-2026-05-30.md). Add keystore CLI subcommand, sudoers grants for /Users/*/**, withdraw 4 broken skills, fix Discord field name. Each is its own PR (#1836 / #1844 / #1839 / #1837).
2. **Phase 2 cleanup** — symmetric revoke for channel skills, brave/github status tightening, slack appToken step, GitHub-MCP status dispatch.
3. **Inventory detection unification (P6)** — touches §3/§4/§5 cleanup; benefits from one careful pass.
4. **Home Assistant** — needs OC tools-allowlist support upstream OR a wrapper-proxy design; not unblock-able with current OC.
5. **Per-bot skill health pings (P7)** + **withdrawn marker cleanup (P9)**.

---

## Shipped

_Add a row here when an item lands. Include the date, PR number, what
shipped, what's left for the item (if anything)._

| Date | Item | PR | Notes |
|---|---|---|---|
| 2026-05-30 | Audit + lying-hint fixes (github / unity / dropbox install_hint) | [#1812](https://github.com/pod_admin/evolve/pull/1812) | Companion to the withdrawal — closed out github/unity/dropbox install_hint lies. github went further into a Backup wizard handoff via PR #1811 (merged before #1812). |
| 2026-05-30 | Withdraw 5 paste-token dead-ends | [#1814](https://github.com/pod_admin/evolve/pull/1814) | notion / linear / runway / home_assistant / obsidian_vault removed from catalog + inventory; install modules kept for verify_token reuse. |
| 2026-05-30 | Obsidian rewire as MCP install with read/read+write toggle | [#1817](https://github.com/pod_admin/evolve/pull/1817) | Reference impl. Added `InstallMcpServer.extra_args` + `/api/mcp-admin/install` accepts `extra_args` + wrapper route pattern + OS-permission-layer mode toggle + mode marker + `access_panel_for(mode)`. |
| 2026-05-30 | Dropbox rewire (2nd MCP install — first non-Obsidian) | [#1819](https://github.com/pod_admin/evolve/pull/1819) | Roadmap doc + dropbox_install module (mirrors obsidian_install) + wrapper routes + auto-detect from `~/.dropbox/info.json` + removal of §6 inventory false-positive + removal of dropbox entry from upstream_plugin_skills. Proves the Obsidian pattern is genuinely reusable. |
| 2026-05-30 | apple_local + autocad surface in matrix (P2) | [#1823](https://github.com/pod_admin/evolve/pull/1823) | Pod-wide resolution (one probe per matrix refresh applied to all bots) since TCC grants are user-scoped not bot-scoped. Includes critical regression-guard test asserting the apple probe runs exactly once no matter the bot count. |
| 2026-05-30 | Notion rewire (3rd MCP install — first non-filesystem) | [#1831](https://github.com/pod_admin/evolve/pull/1831) | First MCP rewire that's not filesystem-backed. Added `@notionhq/notion-mcp-server` to catalog, built `notion_install.build_headers_json` / `resolve_status_mcp` / `keystore_slot_for` helpers, wrapper route at `/api/skills/install/notion/set-token` hides the JSON-headers encoding from operators. Per-bot keystore slot pattern. Includes Notion-specific UX: post-install callout about per-page sharing. Establishes the env_bindings + token-paste shape for the remaining four vetting-blocked skills. |
| 2026-05-30 | GitHub-via-MCP (purpose 2 — 4th MCP install) | [#1832](https://github.com/pod_admin/evolve/pull/1832) | New `github_install.py` for purpose 2 (LLM access); purpose 1 (backup wizard) untouched. Wrapper at `/api/skills/install/github/install-mcp-server`: validate PAT against `/user`, save to keystore `github-<bot>` verbatim (no JSON encoding — GitHub MCP wants plain `GITHUB_TOKEN`), install with `catalog_id="github"`. Purpose independence enforced — MCP install does NOT touch the `.git/config` backup PAT, revoke does NOT clear it. Catalog entry uses deprecated `@modelcontextprotocol/server-github`; swap to GitHub-owned package is a follow-up decision. |
| 2026-05-30 | Runway rewire (first BUNDLED-PLUGIN install) | [#1833](https://github.com/pod_admin/evolve/pull/1833) | **Pattern correction triggered by PodAdmin's note that TeamBotA had Runway working pre-Evolve**: Runway doesn't need an MCP server. OC ships `@openclaw/runway-provider` bundled internally as a `videoGenerationProviders` contract — the install is auth-profiles.json (`profiles["runway:default"]`) + openclaw.json (`agents.defaults.videoGenerationModel.primary`) + kickstart. Established the second install pattern alongside MCP; will serve any future bundled OC provider. Also surfaces the "audit OC bundles before assuming an MCP server is needed" lesson for Linear / HA. |
| 2026-05-30 | Linear rewire (6th MCP install — 2nd API-key one) | [#1834](https://github.com/pod_admin/evolve/pull/1834) | Added `linear-mcp` (dvcrn/MIT) to catalog as **candidate** vetting status, mirrored Notion's helpers (keystore_slot_for, resolve_status_mcp) in `linear_install.py`. Crucially differs from Notion: linear-mcp reads a plain `LINEAR_API_KEY` env var, so the keystore stores the verbatim PAT (mirrors GitHub-MCP's shape). Post-install callout warns about identity (the bot acts as the API key holder). Audited OC bundled extensions first to confirm MCP path was correct (Linear NOT bundled). Sixth near-identical `_create_<skill>_mcp_proposal` closure in server.py — flagged for refactor in the end-of-roadmap audit pass. |
