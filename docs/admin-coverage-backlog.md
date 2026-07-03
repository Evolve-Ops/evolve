# Admin-UI operation coverage backlog

**Purpose.** Tracking doc for "what evo can do on the operator's behalf, vs.
what still requires the admin UI / terminal." Drives the priority ordering for
new tools, new proposal action_kinds, and new generators.

**Not part of the formal spec** — this is a backlog, not an architectural
commitment. The proposal-vs-direct decision rule lives in
[`packages/analyzer/evolve_bot/AGENTS.md`](../packages/analyzer/evolve_bot/AGENTS.md)
under "Resolving operator-described issues in chat"; the tool catalog itself is
in [`packages/admin/evolve_admin/evo/tools/`](../packages/admin/evolve_admin/evo/tools/);
the action_kind catalog is in
[`packages/analyzer/schema/proposal.py`](../packages/analyzer/schema/proposal.py).

**How to read this file.** Each row is an operator-facing UI action.
"Coverage" classifies how evo handles it today (or will after the listed PR
ships).

  - ✅ **Covered** — evo can handle this end-to-end in chat.
  - 🛠️ **Covered after Phase 1.4** — listed action_kind / direct tool exists
    in code; evo can use it once `action.proposal.apply` (#1308) ships and
    AGENTS.md teaching lands (this file).
  - 📐 **Needs new action_kind** — schema entry + applier to be added in
    Phase 1.5+.
  - 🔨 **Needs new direct tool** — to be added.
  - 📊 **Read-only** — model can fetch via existing read tools; nothing to
    build.

**Telemetry feedback.** Per spec §14.3, evo records tool_gap events whenever
it enters the §13.4 Q4 escape hatch. The aggregated rollup (opt-in upload)
reveals which uncovered operations are actually used. This file should be
re-ordered periodically by telemetry frequency.

---

## Dashboard / Overview page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Add a bot | A | 📐 needs `AddBot` action_kind | Phase 1.5c |
| Restart bot | B | ✅ `action.bot.restart` | #1311 |
| Redeploy bot | B | ✅ `action.bot.redeploy` | #1311 |
| Remove bot | B (destructive) | ✅ `action.bot.remove` (`confirm:true` required) | #1313 |
| Pause all bots | B (destructive) | ✅ `action.pod.pause_all` (`confirm:true` + `reason` required) | #1313 |
| Resume all bots | B | ✅ `action.pod.resume_all` | #1313 |
| View per-bot tile (chips, cost, apps) | — | 📊 `pod_state.bots` | ✅ |
| Check pause state | — | 📊 `pod_state.pause_state` | #1313 |

## Reports / Alerts page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Snooze a signal | B | ✅ `action.signal.snooze` | shipped |
| Dismiss a signal | B | ✅ `action.signal.dismiss` | shipped |
| Mark a signal resolved (external fix) | B | ✅ `action.signal.resolve` | #1315 |
| Show info-tier signals (UI toggle) | — | 📊 read tool covers it | ✅ |
| View firing alerts | — | 📊 `pod_state.signals.firing` | ✅ |
| View alert history | — | 📊 `pod_state.signals.history` | ✅ |
| Configure alert subscriptions | A | 📐 needs `UpdateAlertSubscriptions` | Phase 1.5c |
| Manage proposal watchlist | B | 🔨 `action.watchlist.add/remove` | Phase 1.5+ |

## Recommendations page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Take this on (apply proposal) | A | ✅ `action.proposal.apply` | #1308 |
| Snooze a proposal | A | ✅ `action.proposal.snooze` | shipped |
| Dismiss / reject a proposal | A (destructive) | ✅ `action.proposal.reject` | #1311 |
| Mark complete (deferred-completion kinds) | B | ✅ `action.proposal.mark_complete` | #1315 |
| View pending / snoozed / in-process / history | — | 📊 `pod_state.proposals.*` | ✅ |
| Refresh recommendations | B | 🔨 `action.recommendations.refresh` | minor |

## Security page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Run audit (per-app) | B | ✅ `action.app.audit(bot_id, app_id)` | #1313 |
| Run audit (all apps on a bot) | B | ✅ `action.app.audit(bot_id, all_apps=true)` | #1313 |
| Run pod-wide infra audit | B | 🔨 `action.audit.run` (infra scope) | Phase 1.5c |
| Mute a finding | A | 📐 needs `MuteAuditFinding` | Phase 1.5c |
| Unmute a finding | A | 📐 same kind, reversed | Phase 1.5c |
| Update permission baseline | A (force-ask) | ✅ `UpdatePermissionBaseline` (apply via `action.proposal.apply`) | #1308 |
| Rotate an API key | A | 📐 needs `RotateApiKey` action_kind | Phase 1.5c |
| View audit findings | — | 📊 `pod_state.audit` | ✅ |
| Apply Slack policy | A | 📐 may already be covered by `UpdatePluginConfig` | verify |

## Apps / Capabilities page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Run app scan | B | 🔨 `action.apps.scan(bot_id=...)` | Phase 1.5c |
| Install from gallery | A/B | ✅ direct: `action.app.install`; also via proposal apply | #1311 + #1308 |
| Deprecate an app | A (destructive) | ✅ `DeprecateApp` (apply via `action.proposal.apply`) | #1308 |
| Build a new app | A | ✅ `BuildApp` (apply via `action.proposal.apply`) | #1308 |
| Dispatch a forge job | B | 🔨 `action.forge.dispatch` | Phase 1.5c |
| Cancel a forge job | B | 🔨 `action.forge.cancel` | Phase 1.5c |
| Check forge job status | — | 📊 `pod_state.forge_job(job_id)` | #1311 |
| View app inventory | — | 📊 capabilities-tool needed (not yet) | minor read |

## Cost / Usage / Cost Optimization pages

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Change tier assignments | A | ✅ `TierAdjustment` (apply via `action.proposal.apply`) | #1308 |
| Change model preferences / fallback list | A | ✅ generic `ConfigPatch` covers this | #1308 |
| Change context-pruning config (TTL, etc.) | A | ✅ `ConfigPatch` | #1308 |
| Change compaction settings | A | ✅ `ConfigPatch` | #1308 |
| Set / change spend cap | A | 📐 needs `UpdateSpendCap` action_kind | Phase 1.5c |
| Enable / disable spend-cap enforcement | A | 📐 part of `UpdateSpendCap` | same |
| View usage (per-bot + pod) | — | 📊 `pod_state.usage` | #1314 |
| Refresh Anthropic Admin cost report | B | 🔨 `action.anthropic_admin.refresh` | Phase 1.5c |

## Plugins page

All Pattern-A items below apply via `action.proposal.apply` (#1308). Listed
✅ when the action_kind + applier already existed before this work; the
resolver pattern is what made them addressable from chat.

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Enable / disable plugin entry | A | ✅ `EnablePluginEntry` / `DisablePluginEntry` | #1308 |
| Update plugin config | A | ✅ `UpdatePluginConfig` | #1308 |
| Update plugin allow/deny | A | ✅ `UpdatePluginAllowDeny` | #1308 |
| Update plugin baseline | A (force-ask) | ✅ `UpdatePluginBaseline` | #1308 |
| Update plugin load paths | A | ✅ `UpdatePluginLoadPaths` | #1308 |
| Install MCP server | A | ✅ `InstallMcpServer` | #1308 |
| Remove MCP server | A | ✅ `RemoveMcpServer` | #1308 |
| Update MCP server config | A | ✅ `UpdateMcpServerConfig` | #1308 |
| Enable / disable webhook ingress | A | ✅ `EnableWebhookIngress` / `DisableWebhookIngress` | #1308 |
| Update webhook mapping | A | ✅ `UpdateWebhookMapping` | #1308 |
| View plugins | — | 📊 covered by `config.bot` projection | ✅ |

## Skills page (§14.2)

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| OC loader picks up Evolve-shipped skills | — | ✅ deploy hook adds `agents.defaults.skills.load.extraDirs` | #1318 |
| Retirement detection (obviated_by) | — | ✅ scanned on deploy; logs `[notice]` per candidate | #1320 |
| List local skills | — | 📊 needs `pod_state.skills.local` read tool | Phase 1.5b+ |
| Retire a local skill | B (destructive) | 🔨 `evolve-admin retire-local-skill <name>` CLI | Phase 1.5b+ |
| (Author a local skill — chat-time, not UI) | — | (evo authors at chat time per §14.2) | Phase 1.5b+ |

## AI Optimization page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Adjust classifier confidence floor | A | ✅ `ConfigPatch` (apply via `action.proposal.apply`) | #1308 |
| Adjust tier classification config | A | ✅ `ConfigPatch` | #1308 |

## Maintenance / Infra Jobs / System page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Upgrade OC version | A (force-ask) | 📐 needs `UpgradeOC` action_kind | Phase 1.5c |
| Upgrade Evolve | B (it's a deploy) | ✅ `action.bot.redeploy(bot_id="evolve")` covers this | #1311 |
| Restart a gateway | B | ✅ `action.bot.restart` | #1311 |
| Trigger repo-puller | B | 🔨 `action.infra.repo_pull` | Phase 1.5c |
| Pause all bots / resume all | B | ✅ `action.pod.pause_all` / `.resume_all` | #1313 |
| Rollback a bot's config | B | 🔨 `action.bot.rollback` | Phase 1.5c |
| List rollback points | — | 📊 `pod_state.rollbacks` | #1314 |
| Restore ACLs | A | ✅ `ConfigPatch` from `sysadmin_watchdog` generator | #1308 |
| Install / re-install infra LaunchDaemons | B | 🔨 `action.infra.install_jobs` | Phase 1.5c |
| Migrate jobs | B | 🔨 `action.infra.migrate_jobs` | minor |

## Settings page

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| Edit pod-level network.json | A | ✅ generic `ConfigPatch` at pod scope | #1308 |
| Change primary bot | A | 📐 needs `SetPrimaryBot` action_kind | Phase 1.5c |
| Change pod-level alert chat target | A | ✅ `ConfigPatch` | #1308 |
| Change pod-level evo_telemetry settings | A | ✅ `ConfigPatch` (default `tool_gaps: off`) | #1308 + #1317 |
| Wipe local evo telemetry | B (destructive) | ✅ `evolve-admin wipe-telemetry [-y]` CLI | #1321 |
| Enable HTTPS | B | 🔨 `action.pod.enable_https` (wraps `evolve-admin enable-https`) | Phase 1.5c |
| Disable HTTPS | B (destructive) | 🔨 `action.pod.disable_https` | Phase 1.5c |

## Errors / Feedback / Help pages

| Operator action | Pattern | Coverage | Path |
|---|---|---|---|
| View error log per bot | — | 📊 `pod_state.errors` (raw heal recent_errors) | #1314 |
| Submit feedback | B | 🔨 `action.feedback.submit` (low priority) | minor |
| (Help is informational; no actions) | — | — | — |

## Telemetry / Resolver-pattern infrastructure (§14.3)

Not operator-facing UI per se, but tracked here so the picture stays complete.

| Capability | Coverage | Path |
|---|---|---|
| Tool-gap recording (LLM-side) | ✅ `action.evo.log_tool_gap` | #1317 |
| Tool-gap inspection | ✅ `pod_state.tool_gaps` | #1317 |
| `network.json::evo_telemetry.tool_gaps` opt-out | ✅ default `"off"` | #1317 |
| Local wipe path | ✅ `evolve-admin wipe-telemetry` | #1321 |
| Upload daemon (aggregated rollups) | 🔨 launchd job — needs endpoint URL + auth design | Phase 1.5b+ |
| Upstream dashboard | 🔨 separate concern (Phase 2 of telemetry) | Phase 1.5c+ |

## Resolver-pattern generators (§13.5)

| Generator | Status | Path |
|---|---|---|
| `cron_caps_filler` (uncapped agentTurn → `UpsertCronJob`) | ✅ shipped | #1312 |
| `auth_drift_filler` (`perm_config_drift` → per-field `ConfigPatch`) | ✅ shipped | #1316 |
| `mcp_drift_filler` | 🔨 needs new appliers + signal producer | Phase 1.5+ |
| `version_drift_filler` | 🔨 needs new signal producer | Phase 1.5+ |
| `plugin_allowlist_filler` | ❎ already covered by existing `plugin_curator` | — |
| `acl_drift_filler` | ❎ already covered by `sysadmin_watchdog` | — |
| `gateway_unhealthy_filler` | ❎ already covered by `gateway_diagnostician` | — |

---

## Phase 1.4–1.5 progress (as of 2026-05-19)

Status of the originally-proposed phase ordering:

1. ✅ **Phase 1.4 step 2** (shipped): `action.proposal.reject`,
   `action.bot.restart`, `action.bot.redeploy`, `action.app.install`,
   `pod_state.forge_job`. — PR #1311
2. ✅ **Phase 1.4 step 4** (shipped): `cron_caps_filler` generator
   (the case that surfaced §13). — PR #1312
3. ✅ **Phase 1.5a — high-value direct tools** (shipped):
   `action.bot.remove`, `action.pod.pause_all`, `action.pod.resume_all`,
   `action.app.audit`, `pod_state.pause_state`. — PR #1313
   (Pod-wide `action.audit.run` for infra audits is still
   open — see Security page row.)
4. 🟡 **Phase 1.5b — §14.2 local skills + §14.3 telemetry** (partial):
   shipped: skills loader hookup (#1318), tool-gap telemetry storage +
   tools (#1317), wipe-telemetry CLI (#1321), retirement detector
   (#1320). Open: list-local-skills + retire-local-skill CLIs,
   telemetry upload daemon, upstream dashboard.
5. 🔨 **Phase 1.5c — new action_kinds (Pattern A)**: still open. Order
   telemetry-driven once enough §14.3 records accumulate.
   - `AddBot`, `SetPrimaryBot`
   - `RotateApiKey`
   - `UpdateSpendCap`
   - `UpgradeOC`
   - `MuteAuditFinding`
   - `UpdateAlertSubscriptions`
   Plus the open `action.audit.run`,
   `action.forge.dispatch` / `.cancel`, `action.pod.enable_https` /
   `.disable_https`, `action.bot.rollback`, `action.infra.*`.
6. ✅ **Phase 1.5d — minor read tools** (shipped): `pod_state.usage`,
   `pod_state.errors`, `pod_state.rollbacks`. — PR #1314
   `pod_state.skills.local` deferred to a Phase 1.5b+ slice alongside
   `retire-local-skill` (both consume the same skills-scan logic).
7. ✅ **Phase 1.5e — minor lifecycle tools** (shipped):
   `action.signal.resolve`, `action.proposal.mark_complete`. — PR #1315
   Forge dispatch/cancel + alert subscription management deferred to
   Phase 1.5c (need new action_kinds).
8. ✅ **Generator catalog companion**: `auth_drift_filler` (per-field
   `ConfigPatch` for `perm_config_drift`) — PR #1316. Other §13.5
   catalog entries (`mcp_drift_filler`, `version_drift_filler`) need
   new appliers or signal producers first; the rest are already
   covered by existing generators.

The order isn't rigid — operator-described gaps that don't fit any
of these come up via the §14.3 telemetry loop and reorder the list.

---

## How to maintain this doc

- Add a row when you encounter an operator-facing UI action that's
  not covered.
- Update the Coverage column when a PR ships that closes a row.
- Move rows between sections when the admin UI restructures.
- Treat this as a "living backlog" — it should drift as the system
  evolves; the §3.8 brittleness framework doesn't watch this file
  because it's intentionally informal.
