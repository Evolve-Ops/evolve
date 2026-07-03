# Skills Deep Audit — 2026-05-30

**Status**: completed via multi-agent workflow (16 skills × 7-point check + adversarial verify).
**Trigger**: PodAdmin's request after the May launch where many shipped skills turned out to be silently broken; goal is to catch every broken/half-broken skill before the next public launch.
**Method**: pipeline workflow — per-skill auditor → adversarial verifier (refute-by-default) for any non-passing finding → synthesis. Auditors had SSH access to the mini for read-only live verification.

---

## Executive summary

**Of 16 skills audited, only 2 fully work end-to-end** (dropbox by architecture but blocked by infra bug, linear by architecture but blocked by same bug). The remaining 14 break down:

| Bucket | Count | Skills |
|---|---|---|
| ✅ **Works fully end-to-end** | 2 | autocad (honest stub), runway (correctly withdrawn) |
| 🔵 **Architecturally correct, blocked by cross-cutting infra bug** | 5 | dropbox, linear, notion, obsidian_vault, github-MCP — **all silently broken in production right now** |
| 🟡 **Fix in place before launch** | 4 | telegram, discord, slack, brave, github (backup status) |
| 🔴 **Pull from catalog now** | 5 | gog, imessage, gdrive, apple_local, unity |

**Cross-cutting bugs dominate per-skill issues.** Two infrastructure-level gaps silently break 5 MCP-backed skills at runtime even though their individual install code is correct; one shared bug pattern affects every channel skill's revoke; status resolvers across 5 different skills lie in the same way. Fixing the cross-cutting bugs unblocks most of the catalog.

---

## Pre-launch blockers (must fix before any public skill is real)

### 🔴 P0-1 — Missing `evolve-admin keystore get` CLI command
**Affects**: notion, linear, obsidian_vault, dropbox, github-MCP (every MCP-backed install we've shipped)

`packages/analyzer/mcp_admin/launcher.py:106` generates MCP wrapper scripts that shell out to `/usr/local/bin/evolve-admin keystore get <slot>` at MCP-server exec time. **That CLI command doesn't exist.** Verified live on the mini: `Error: No such command 'keystore'`. The wrapper's `[ -z "${VAR}" ]` guard trips on the empty output, exits 64 with `evolve-admin-launcher: missing keystore key '<slot>' for <VAR>`, and `npx` never starts the actual MCP server.

`resolve_status_mcp` in each install module reports `valid` because it only checks that the keystore slot has a value, not that the launcher can actually resolve the slot at shell time. So the dashboard shows green while every MCP exec silently fails.

Why this wasn't caught earlier: Obsidian and Dropbox have `required_env=[]` in their filesystem catalog entries, so their launcher wrappers skip the keystore shell-out entirely. Notion was the **first** MCP install with `required_env`, and Linear / GitHub-MCP shipped right after copying the Notion pattern — none of them surfaced the gap because nobody manually ran one through end-to-end on the mini.

**Fix**: ~10 LOC adding `@main.group() def keystore()` + `keystore_get(name)` to `evolve_admin/cli.py`, plus a subprocess-based integration test that exercises the CLI against a real `KeystoreManager`. Deploy + restart admin-ui daemon.

### 🔴 P0-2 — Sudoers missing `chmod +a` grants for `/Users/*` paths
**Affects**: obsidian_vault, dropbox

`obsidian_install.grant_vault_acl()` runs `sudo /bin/chmod +a "<bot_user> allow ..." <vault_path>` against user-chosen paths like `/Users/pod_admin/Documents/Obsidian`. Current `/etc/sudoers.d/evolve` only grants `chmod +a` for `/Users/Shared/evolve/proposals|signals`. The call is rejected, the install route returns 500 `acl_grant_failed` before creating the InstallMcpServer proposal — install fails before anything lands.

Verified live: `ssh mini 'sudo cat /etc/sudoers.d/evolve | grep chmod.*+a'` returns only the two `/Users/Shared/...` lines.

**Fix**: extend `setup_wizard.py` §17 to grant `chmod +a/-a/-R +a/-R -a` on `/Users/*/*` glob paths; re-roll via `sudo evolve-admin install-infra-jobs`.

### 🟠 P0-3 — Discord install would DELETE team_bot_b's working credential
**Affects**: discord (and the only working Discord bot, team_bot_b)

`discord_install.py:557-558` writes `dc['botToken'] = bot_token; dc.pop('token', None)`. But OC reads `channels.discord.token`, not `botToken` — verified by counting references in OC's bundled `dist/discord-DU7KIiYG.js`: `.token` appears 31×, `.botToken` 0×. team_bot_b's live working config on the mini uses `token`. Running the install on team_bot_b today would delete team_bot_b's only working Discord credential and replace it with a key OC ignores, killing the bot's Discord integration.

**Fix**: 1-line flip (`botToken` → `token`) + 3 test assertions in `test_skills_discord_install.py`. Ship as a hotfix before anyone touches the install.

### 🟠 P0-4 — Withdraw 5 skills before launch
**Affects**: gog, imessage, gdrive, apple_local, unity — see "Pull from catalog" section.

Each promises capabilities the bot cannot deliver. Several silently report `status="active"` on bots that cannot use the feature. **Better to under-promise than over-deliver-then-fail** — same lesson as the May incident.

### 🟠 P0-5 — Brave status lies on 7-of-8 pod bots
`resolve_status` for brave returns `"active"` from `plugins.entries.brave.enabled` alone, never checking that `apiKey` is set. Verified live: 7 of 8 pod bots (every bot except atlas) show green for Brave Search but have no working API key.

This is the textbook "status said active but capability is broken" pattern from May. Don't ship the launch with this state intact.

### 🟠 P0-6 — GitHub backup status lies the OTHER way on 6-of-7 bots
`_resolve_github_status` regex matches only HTTPS+PAT URLs; SSH-form `git@github.com:` returns `missing`. 6 of 7 production bots show "missing" on the Skills page **despite nightly backups actually succeeding**. Trust-eroding in the inverse direction.

---

## Per-skill verdicts

### ✅ Keep as-is

#### dropbox — works (blocked by P0-1 + P0-2)
- All 7 checks pass architecturally; ACL mode toggle + marker + InstallMcpServer applier + gateway kickstart are correct
- Reference shape for filesystem-MCP installs
- **Will fail in production today** because of P0-1 (missing keystore CLI) and P0-2 (missing sudoers grants); fix those and dropbox is good.

#### linear — works (blocked by P0-1)
- Validate-before-keystore-write, per-bot keystore slot, MCP applier kickstarts gateway. 33/33 tests pass.
- Same launcher dependency as Notion; broken by P0-1 today.
- vetting_status="candidate" (community package) is correctly flagged.

#### autocad — honest stub
- Module docstring openly declares v1 catalog stub; `resolve_status` hard-returns `needs_app`; access panel directs revoke to Autodesk's dashboard.
- **Status cannot lie** — only return value is `needs_app`. Cannot trigger the failure mode of the May incident.
- Optional polish: tighten access-panel `will` list from present-tense to future-tense; replace the `confirm` step's no-op endpoint with terminal copy ("we'll email you when the OAuth installer ships").

#### runway — already correctly withdrawn
- PR #1814 pulled it from catalog; the disk state on main is consistent (no routes, no inventory branch).
- Open PR #1833 (mine) contains the bundled-plugin rewire; when merged, runway returns properly.

### 🟡 Fix in place before launch

#### telegram — partial (asymmetric revoke)
- ✅ Install + capability + status all work (`getMe` is called against real API on every status poll)
- ❌ Revoke only deletes the marker file; doesn't clear `channels.telegram.botToken` from `openclaw.json` or kickstart the gateway
- **Fix (S)**: add `disable_channel_in_oc_config` helper mirroring `enable_channel_in_oc_config`; call from revoke route alongside `delete_token_config` + kickstart

#### discord — broken (P0-3 plus asymmetric revoke + status drift)
- See P0-3 above for the critical field-name fix
- Same revoke asymmetry as Telegram
- `resolve_status` doesn't check that `channels.discord.token` matches the keystore token; would report `valid` while a drifted config sends nowhere
- **Fix (S)**: 3 changes in one PR — flip `botToken` → `token` (P0-3); add inverse of `enable_channel_in_oc_config` to revoke; tighten `resolve_status` to confirm openclaw.json wiring matches keystore

#### slack — partial (3 gaps)
- Install never auto-installs the upstream `@openclaw/slack` npm package — works for team_bot_a/team_bot_c because they were manually pre-installed, fails for fresh bots, AND `deploy.py`'s strip-stale-entries pass would actively REMOVE `plugins.entries.slack` on next deploy
- Slack socket mode requires `appToken` (xapp-) that OAuth doesn't deliver — docstring at `slack_install.py:433-446` admits this
- `resolve_status` returns `valid` on `auth.test` pass alone, so fresh bot shows green while no Slack traffic flows
- Same revoke asymmetry as Telegram/Discord
- **Fix (M)**: 3 changes in one PR — auto-install npm package in OAuth callback (mirror brave gap-fill at `deploy.py:1937-1962`); add `appToken` paste step + `needs_app_token` status; mirror `disable_channel_in_oc_config` in revoke

#### brave — partial (P0-5 plus install plan + revoke gaps)
- See P0-5 for the status fix
- Skill install button leads to a passive "run sudo evolve-admin deploy" hint that captures no credential; the working capture lives at `/api/admin/onboard/brave` on the Credentials page (different surface)
- No `/api/skills/install/brave/revoke` endpoint
- **Fix (S)**: tighten `resolve_status` to require `apiKey`; deep-link `build_install_plan` to `/api/admin/onboard/brave` (mirror github→`open_github_backup_wizard` pattern); add revoke endpoint that clears `apiKey` + auth profile + kickstarts

#### github — partial (P0-6 plus MCP status orphan)
- Purpose 1 (backup): both wiring and runtime consumer work; status regex is wrong (see P0-6)
- Purpose 2 (MCP): correct end-to-end **except** the orphaned `_github_mcp_resolve_status` is not wired to `/api/skills/install/github/status` (the dispatcher always returns purpose-1 backup status)
- The deprecated `@modelcontextprotocol/server-github` package is on borrowed time (officially deprecated 2025.4.8 last published)
- **Fix (S)**: source backup status truth from `network.json::bots.<bot>.backupRepoUrl` instead of regex on `.git/config`; wire dual-purpose dispatch (`?purpose=mcp`); separately, schedule swap of deprecated MCP package

### 🔴 Pull from catalog now

#### gog — broken (no runtime consumer)
- **Smoking gun**: `ssh mini grep -rln google_workspace_ /opt/homebrew/lib/node_modules/openclaw/dist/` returns **ZERO matches**. OC never reads the `google_workspace_<bot>` profile that the install writes.
- The OC `google` plugin is the Gemini LLM provider only — its OAuth scopes are `cloud-platform` + `userinfo.email`, NOT gmail/calendar
- The only OC Gmail consumer is `gmail-watcher-*.js`, which spawns a separate `gog` CLI binary that **isn't installed** (`which gog` → "not found")
- Access panel falsely promises "Read incoming emails", "List events on your calendar"
- This is the most-downloaded ClawHub skill per memory notes — high-stakes false advertising
- **Withdraw immediately**; re-add via real Gmail-MCP server (e.g., `@gongrzhe/gmail-mcp-server`) through the InstallMcpServer pipeline, same shape as Notion/Linear

#### imessage — broken (3 load-bearing failures)
1. `set-handle` endpoint never calls `install_imessage_poller` — wizard finishes but **no LaunchDaemon is installed until next full `deploy_bot`**
2. SEND has zero runtime consumer — `imessage_helpers.send_message` is admin-side Python with no tool/MCP/plugin exposure
3. Poller is **architecturally one-way**: discards gateway response at `poller.py:290-292`, no reply-back code
- Verified live on mini: 0 bots have `imessage.json`, 0 LaunchDaemons exist
- Access panel promises both send AND receive — both false
- **Withdraw**; re-add requires wiring `@openclaw/imessage` channel OR exposing `send_message` as a tool, plus making the poller bidirectional, plus running `install_imessage_poller` from the wizard, plus a liveness probe, plus a revoke route

#### gdrive — broken (rides on broken gog)
- No OC runtime Drive consumer exists (bundled `google` extension is Gemini LLM only; no Drive MCP)
- `GOG_DEFAULT_SERVICES = ('gmail_readonly', 'calendar_readonly')` — Drive scopes are never even granted
- `resolve_status` returns `active` on any GOG-active bot regardless of Drive scopes (guaranteed false positive)
- Slipped through the May paste-token withdrawals because it piggybacks on GOG instead of writing its own marker
- **Withdraw**; re-add via real Drive MCP server (e.g., `google-drive-mcp` by isaacphi) through InstallMcpServer pipeline

#### apple_local — broken (probe-is-the-only-consumer)
- **NO bot has any plugin/mcp.servers/channels entry** for Contacts/Calendar/Reminders/Notes (verified live on all 7 bots)
- OC bundle has no apple-* extension; `packages/plugin/src` has zero AppleScript
- `inventory.py:657-658` admits in a comment: "neither skill writes a plugin / mcp.servers / channels entry"
- TCC grants would land for `evolve` user but bots run as team_bot_a/team_bot_c/personal_bot_user/etc — wrong user
- Access panel promises 5 capabilities — all false post-install
- **Withdraw**; re-add via either apple-mcp-server (with per-bot-user TCC grants via `sudo -u <bot> osascript`) or in-pod Apple tool surfaces in `packages/plugin/src`

#### unity — broken (no upstream plugin exists)
- `openclaw plugins inspect unity` returns "Plugin not found"; `openclaw plugins search unity` returns "No ClawHub plugins found"
- Install hint instructs `openclaw plugins install unity` — the CLI rejects this
- Compounding: `resolve_status` has a nested-shape bug (`{config: {enabled: false}}` falls through to `active`) that would falsely flip team_bot_a's unity entry to active
- Access panel promises scene listing, GameObject inspection, scripted Editor actions — none implemented anywhere
- **Withdraw**; revisit if/when OpenClaw ships a unity extension or a Unity MCP gets vetted (none known)
- **Independent fix regardless**: fix the `{config: {enabled: false}}` nested-shape resolver bug — it affects any genuinely-disabled upstream plugin

---

## Cross-cutting findings

### F1 — Missing keystore CLI silently breaks every MCP install (CRITICAL)
The launcher contract assumes a CLI command that doesn't exist. Every MCP install with `required_env` fails at exec time while `resolve_status_mcp` reports `valid`. Filesystem MCPs (Obsidian/Dropbox) didn't surface the bug because `required_env=[]` skips the keystore shell-out. Notion was the first MCP install with `required_env`; Linear/GitHub-MCP shipped right after copying the pattern.

**Recommendation**: Add the CLI subcommand (P0-1). Additionally, harden `resolve_status_mcp` implementations to actually subprocess `evolve-admin keystore get <slot>` so future launcher-pipeline breakages flip status to broken instead of silently lying.

### F2 — Asymmetric install/revoke across channel skills (HIGH)
Install writes marker + `openclaw.json::channels.<x>` + `plugins.entries.<x>` + kickstarts. Revoke only deletes the marker. Same bug shape in telegram, slack, discord.

**Recommendation**: Hoist a common `disable_channel_in_oc_config` helper into `_oc_install_common` (or per-module mirrors); call from each revoke route alongside `delete_token_config`. Add a test that asserts install-then-revoke leaves `openclaw.json` structurally identical to pre-install.

### F3 — Status resolvers report active without probing capability (HIGH)
Affects brave (apiKey-not-checked, 7-of-8 bots green-but-broken), gog (LLM-plugin enabled + OAuth profile present → "active"), gdrive (any GOG-active bot regardless of Drive scopes → "active"), apple_local (active from osascript working for evolve user, not bot user), unity (nested `{config: {enabled: false}}` falls through to active). Inverse failure on github backup: status="missing" for every bot using SSH-form remotes (6 of 7) because regex only matches HTTPS-PAT form.

**Recommendation**: Each `resolve_status` must encode the actual runtime-consumer contract — for plugin-based skills, check the config key the plugin manifest declares it reads, not just `enabled` flag. For OAuth skills, check the consumer's expected profile shape. For probe-based skills, run the probe as the user whose grant matters. Where possible, run a cheap actual capability call on status poll.

### F4 — Several skills claim runtime consumers that don't exist (HIGH)
- gog assumes OC `google` plugin reads `google_workspace` OAuth profile (it doesn't — Gemini LLM only)
- gdrive assumes OC ships a Drive consumer (no extension, no MCP)
- apple_local assumes a Contacts/Calendar/Reminders/Notes tool surface exists (none anywhere)
- unity assumes upstream `openclaw plugins install unity` works (CLI rejects)
- imessage assumes the `@openclaw/imessage` channel plugin is wired (install never touches it)

Same fingerprint as the 5 paste-token skills withdrawn 2026-05-30 (PR #1814).

**Recommendation**: Before listing a skill in the catalog, a smoke check should verify the named runtime consumer exists on the live OC bundle (or in the documented MCP catalog). If the consumer is intentionally future, mark the catalog entry honestly (autocad-style stub with hard-coded `needs_app` status + future-tense access-panel copy).

### F5 — Access-panel `will` lists are present-tense capability promises some skills can't keep (MEDIUM)
gog ("Read incoming emails", "List events on your calendar"), gdrive ("List files and folders in your Drive", "Search Drive by name or contents"), apple_local (5 bullets), unity ("List scenes, prefabs, and assets"), imessage ("Send iMessages to contacts you specify"), and autocad ("Lets this bot read AutoCAD drawings"). These are the Plex-test trust contract — if false post-install, the user's first interaction is a betrayal.

**Recommendation**: Audit every `access_panel.will` list against the actual runtime consumer. Where the consumer is missing or wrong-shape, either withdraw the skill or restructure the copy to future-tense ("will, when wired") with explicit "not yet available" callout. autocad shows the honest-stub pattern done right; gog/gdrive/apple_local/imessage are the dishonest-stub anti-pattern.

---

## Phased fix plan

### Phase 1 — Stop the bleeding (today, ~1 day)
The things that would betray users if launched. Each ships as its own PR:

1. **Add `evolve-admin keystore get` CLI** — unblocks notion/linear/obsidian_vault/dropbox/github-MCP (P0-1)
2. **Sudoers grants for `/Users/*` chmod +a** — unblocks dropbox + obsidian (P0-2)
3. **Withdraw 5 broken skills** — gog, imessage, gdrive, apple_local, unity (mirror PR #1814 pattern)
4. **Fix Discord field name bug** — 1-line hotfix before anyone runs the install (P0-3)

### Phase 2 — Make remaining skills honest (this week, ~2-3 days)
5. **Symmetric revoke for telegram/slack/discord** — F2 fix; hoist `disable_channel_in_oc_config` into `_oc_install_common`
6. **Brave status resolver** — check `apiKey`, not just `enabled`; deep-link install plan to `/api/admin/onboard/brave`; add revoke route (P0-5)
7. **GitHub backup status** — accept SSH-form remotes; source from `network.json` (P0-6)
8. **GitHub-MCP status dispatch** — wire the orphaned `_github_mcp_resolve_status`
9. **Slack — auto-install npm package + appToken paste step + symmetric revoke**
10. **Unity nested-shape resolver bug** — independent of unity outcome, affects any disabled upstream plugin

### Phase 3 — Roadmap (post-launch)
11. **Gmail/Calendar/Drive** via real MCP packages through InstallMcpServer (replaces withdrawn gog + gdrive)
12. **iMessage** with bidirectional poller + tool-surface exposure
13. **Apple Contacts/Calendar/Reminders/Notes** via apple-mcp-server OR osascript tool surface
14. **Swap deprecated `@modelcontextprotocol/server-github`** for the GitHub-owned successor
15. **Home Assistant** with scope-toggle design (read vs control) — also blocked on OC tools-allowlist (see end-of-roadmap audit doc)
16. **Unity** parked indefinitely (no community MCP, no upstream OC plugin)

---

## Method (for reproducibility)

The audit ran as a multi-agent workflow:

```
SKILLS (16) ─pipeline─→ Stage 1: per-skill auditor (general-purpose agent)
                          ↓ returns structured verdict (7-point check + recommendation + smoking_gun + confidence)
                        Stage 2: adversarial verifier (only for non-'works' or low-confidence findings)
                          ↓ tries to REFUTE the auditor's verdict
                        [barrier: collect all results]
                          ↓
                        Synthesis agent ─→ keep / fix_in_place / pull_from_catalog buckets + cross-cutting findings + pre-launch blockers
```

**The 7-point end-to-end check** every auditor applied:

1. **Discoverability** — appears in `/api/skills/catalog`, access panel renders, status resolver doesn't 500
2. **Install plan** — POST `/api/skills/install/<id>` returns steps; wizard renders + accepts input
3. **Credential lands somewhere real** — to a specific target file/keystore slot/env binding/TCC grant
4. **Runtime consumer exists** — OC actually loads a plugin OR MCP server OR cron that reads that credential (the load-bearing question)
5. **Actual capability** — bot can call a tool that uses the credential against the real API and succeed
6. **Status correctness** — `resolve_status` returns `valid` only when 1–5 are all true
7. **Revoke path** — uninstall removes credential AND plugin/MCP entry AND kickstarts gateway

**Critical instruction to auditors**: "credential lands somewhere" is NOT the same as "credential is consumed by a runtime that uses it for the advertised capability". The May incident was caused by accepting check #3 as proof of #4+#5. The full audit prompt is at `/Users/pod_admin/.claude/projects/-Users-pod_admin-GitHub-evolve--claude-worktrees-priceless-yonath-eb4661/5be1f7f1-faa3-481c-a70d-6736abe49730/workflows/scripts/skill-audit-deep-2026-05-30-wf_66893b54-b44.js`.

**Verifier discipline**: only ran on non-`works` or low-confidence findings to save tokens. Default-to-skeptical: try to refute the auditor's verdict; if you find evidence the skill works that the auditor missed, set `refuted=true`. Two findings had the verifier add evidence (gog: caught a SECOND phantom-consumer path the auditor missed — `openclaw.json::integrations.gmail` is also told to users but has no OC reader; brave: team_bot_b also affected, making it 7-of-8 not "all 7"). Zero verdicts were refuted.

**Live-mini verification**: auditors had SSH access to the mini for read-only checks. Most verdicts include `ssh mini grep ...` smoking guns confirming the bug exists in production state, not just static analysis.

**Skills not audited**: home_assistant (already withdrawn, design-pending). The catalog list reads 16 entries today; runway is also withdrawn but audited for completeness because of the open rewire PR #1833.

Full per-skill JSON (~1053 lines of structured verdict + adversarial verifier reasoning) is preserved at `/private/tmp/claude-501/-Users-pod_admin-GitHub-evolve--claude-worktrees-priceless-yonath-eb4661/5be1f7f1-faa3-481c-a70d-6736abe49730/tasks/wlz3p4x2v.output`.

---

## §Method update — enumerate both OC channel sources (added 2026-06-04)

**Framework bug**: the per-skill audit (Stage 1 above) treated `dist/channel-catalog.json` as the complete OC channel surface. It is not. OC also bundles channel-shaped plugins inside `dist/extensions/<id>/` whose `package.json` carries an `openclaw.channel.id` block. The catalog file does not list them.

**Consequence in this audit**: the iMessage skill was placed in the 🔴 "Pull from catalog now" bucket (see "imessage — broken" above) on the reasoning that "the OC `@openclaw/imessage` plugin doesn't exist." It does — bundled at `dist/extensions/imessage/`. The withdrawal was operationally correct (our home-rolled install was broken), but the diagnostic was wrong, and the right fix was rewire-to-bundled, not withdraw.

**Channels missed by this audit's enumeration step** (verified live against OC v2026.6.1, 2026-06-04):

| Bundled channel | Package | Notes |
|---|---|---|
| imessage  | `dist/extensions/imessage/`  | Withdrawn 2026-05-30; should be re-added via bundled-plugin rewire |
| signal    | `dist/extensions/signal/`    | Vetted ([project_signal_cli_vetting_2026_05_14](memory)) but never wrapped |
| mattermost| `dist/extensions/mattermost/`| Carla/Diana persona fit; never wrapped |
| sms       | `dist/extensions/sms/`       | Twilio; never wrapped |
| irc       | `dist/extensions/irc/`       | Niche; explicitly out-of-scope per June 4 Bucket C |
| clickclack| `dist/extensions/clickclack/`| Self-hosted chat; not in the June 4 audit either — discovered by `tools/list_oc_channels.py` |
| telegram  | `dist/extensions/telegram/`  | Already wrapped; included for completeness |

That's seven channels the catalog-only enumeration would never have surfaced. `telegram` is wrapped already (so the silent miss didn't bite us), but the other six are silent gaps.

**Mandatory framework change going forward**:

1. **Canonical channel-list source is `tools/list_oc_channels.py`.** Any future skills audit MUST call it with `--source=both` before writing the per-skill verdict table. Never read `channel-catalog.json` directly without also enumerating `dist/extensions/*/package.json`.

2. **Drift gate**: a committed snapshot lives at `docs/skills/oc-channel-coverage.json`. Every OC version bump on the mini must re-run `tools/list_oc_channels.py --diff-against=docs/skills/oc-channel-coverage.json`; a non-empty diff blocks the upgrade until the snapshot is updated AND each new channel is either wrapped (new install module) or explicitly skipped (Bucket C in `docs/openclaw-coverage-audit-2026-06-04.md`).

3. **Discovery is dispatched, not deduced.** The June 4 audit (`docs/openclaw-coverage-audit-2026-06-04.md`) caught this gap (its F1 cross-cutting finding). That document is the authority on "what OC ships that Evolve doesn't wrap" — cross-reference it before withdrawing any channel skill.

The original May 30 audit body above is preserved as historical record. The iMessage verdict in particular should be read against this update: the load-bearing failures it documents in the home-rolled install are real, but the right fix is a bundled-plugin rewire (see June 4 Bucket B), not permanent withdrawal.
