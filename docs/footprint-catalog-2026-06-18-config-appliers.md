# Footprint catalog — F-1d: config-mutation / appliers

**Date:** 2026-06-18 · **Aspect:** `META:footprint` · **Slice:** F-1d (the "alters how OpenClaw works" footprint)
**Frame:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md) · sibling slices: F-1a deploy/privilege · F-1b runtime/gateway · F-1c cost/monitors · F-1e Settings audit

This slice exhaustively catalogs every way Evolve **WRITES** to the config/state a bot
loads — the **Mutation** dimension of the four-dimensional footprint frame. Each row is
grounded in code (`file:line`); nothing is asserted from assumption. Where a write also
carries Runtime / Cost / Privilege weight, the Dimensions column tags it.

**Dimension legend:** **M**=Mutation · **R**=Runtime/hot-path · **C**=Cost · **P**=Privilege/surface.

**The one write gate.** Almost every openclaw.json mutation below funnels through a single
critical-safety function, `safe_write_bot_config()` ([deploy.py:3527](../packages/admin/evolve_admin/deploy.py)):
it stages JSON to `/tmp` via `_secure_stage()` ([deploy.py:243](../packages/admin/evolve_admin/deploy.py)),
runs `openclaw config validate --json` as the bot user against the staged file, backs up the
live config to `openclaw.json.bak`, `sudo /bin/cp`s the staged file in, chowns to `bot:staff`,
and enforces `chmod 600` via `chmod_secret_config()`
([secret_config_perms.py:55](../packages/admin/evolve_admin/secret_config_perms.py)). An
unrecognized key fails the deploy rather than crash-looping the bot. Repair paths
(`strip_agents_main`, `_clear_stale_plugin_install`) use the same `/tmp`+`sudo cp` mechanism
but skip OC-schema validation (they only delete known-bad keys).

---

## A. openclaw.json field writes (the core deploy mutation surface)

All of the following are written by `ensure_plugin_config()`
([deploy.py:2219](../packages/admin/evolve_admin/deploy.py)), invoked on **every** `deploy_bot()`
after the plugin install (deploy.py:3508). "Gap-fill" = only written if absent, operator
values preserved. "Overwrite" = re-asserted on every deploy regardless of operator value.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `agents.defaults.model.*` | Seeds primary model from auth-profile provider / RECOMMENDED if missing; normalizes string→object | M | **Unconditional** (gap-fill — once set, never re-detected) | [deploy.py:2302-2332](../packages/admin/evolve_admin/deploy.py) | Honors tier config first; operator override survives redeploy |
| `agents.defaults.thinkingDefault` | Forced to `"off"` on every deploy | M, R | **Unconditional overwrite** (always re-asserted) | [deploy.py:2334-2341](../packages/admin/evolve_admin/deploy.py) | Sonnet-4.6 adaptive thinking breaks context pruning; one of the few always-clobbered fields |
| `agents.defaults.contextPruning` / `compaction` / `bootstrap*Chars` / `heartbeat.*` | Seeds Evolve's context-management defaults if absent | M, R | **Unconditional** (block gap-fill, subfields preserved) | [deploy.py:2023-2063](../packages/admin/evolve_admin/deploy.py) | `cache-ttl 4h`, safeguard compaction, 100k/40k bootstrap caps |
| `plugins.installs.evolve` (registry entry) | Created by `openclaw plugins install` — registers the Evolve TS plugin | M, R, P | **Unconditional on deploy** | [deploy.py:3397,3494](../packages/admin/evolve_admin/deploy.py) | See §B — this is the headline injection |
| `plugins.entries.evolve.*` | Materializes the Evolve plugin config block (model routing, tier, cost ledger, dashboard) | M, R | **Unconditional** (materialize from declared inputs) | [deploy.py:2544-2658](../packages/admin/evolve_admin/deploy.py); `materialize_evolve_plugin_config()` ([openclaw_materializer.py](../packages/admin/evolve_admin/openclaw_materializer.py)) | `_PLUGIN_CONFIG_DEFAULTS` at openclaw_materializer ~94-111 |
| `plugins.entries.evolve.hooks.allowConversationAccess` | Forced `true` — lets Evolve's hooks receive `llm_output`/`agent_end` content | M, R | **Unconditional overwrite** | [deploy.py:2622-2628](../packages/admin/evolve_admin/deploy.py) | Required by OC ≥2026.4.29; this is the hot-path opt-in (cross-ref F-1b) |
| `plugins.entries.evolve.subagent.allowModelOverride` | Forced `true` | M, R | **Unconditional overwrite** | [deploy.py:2617-2619](../packages/admin/evolve_admin/deploy.py) | Required for tier override in `subagent.run()` |
| `plugins.entries.evolve.config.dashboardEnabled` | `true` for primary role, `false` for member | M, R | **Unconditional** (role-conditional) | [deploy.py:2630-2637](../packages/admin/evolve_admin/deploy.py) | |
| `plugins.entries.<stale>` | Prunes plugin-config keys dropped from the manifest's configSchema | M | **Unconditional** (reconcile) | [deploy.py:2639-2658](../packages/admin/evolve_admin/deploy.py) | Printed to deploy log |
| `plugins.load.paths` | Normalized to the single root-owned `PLUGIN_INSTALL_DIR`; strips admin-owned relay paths | M, P | **Unconditional** (repair, only if `load.paths` already present) | [deploy.py:2348-2376](../packages/admin/evolve_admin/deploy.py) | |
| `tools.exec.security` | Sets exec policy (`deny`/`allowlist`/`full`) computed by `_infer_exec_policy()` | M, R, P | **Unconditional write, value configurable** (see §C) | [deploy.py:2446-2452](../packages/admin/evolve_admin/deploy.py); `_infer_exec_policy` [deploy.py:1637](../packages/admin/evolve_admin/deploy.py) | The literal "how OC executes" knob; see §C for the override ladder |
| `tools.exec.ask` | `"on-miss"` when security≠deny; key deleted when security=deny | M, R | **Unconditional** (derived from exec.security) | [deploy.py:2453-2462](../packages/admin/evolve_admin/deploy.py) | Applier guards refuse `ask="off"` (open posture) |
| `tools.web.search.enabled` / `tools.web.fetch.enabled` | `true` if absent | M, C | **Unconditional** (gap-fill) | [deploy.py:1947-1952](../packages/admin/evolve_admin/deploy.py) | Web fetch/search is a token-cost surface |
| `commands.native` / `commands.nativeSkills` | `"auto"` if absent | M | **Unconditional** (gap-fill) | [deploy.py:1954-1959](../packages/admin/evolve_admin/deploy.py) | |
| `gateway.mode` / `gateway.bind` / `gateway.trustedProxies` | Defaults `local`/`loopback`/`[]` if absent | M, P | **Unconditional** (gap-fill; operator list preserved) | [deploy.py:2378-2402](../packages/admin/evolve_admin/deploy.py) | |
| `gateway.auth.token` | Generates a fresh `secrets.token_hex(32)` if missing/empty | M, P | **Unconditional** (only if absent — existing token survives) | [deploy.py:2387-2393](../packages/admin/evolve_admin/deploy.py) | Closes the `gateway.loopback_no_auth` OC security finding |
| `channels.telegram.*` | Repairs old wizard field names; fills `enabled/dmPolicy/groupPolicy/streaming` defaults | M | **Unconditional** (repair + gap-fill) | [deploy.py:2508-2521](../packages/admin/evolve_admin/deploy.py) | `token`→`botToken`, strips stale `chatId` |
| `session.dmScope` / `session.reset` | `per-channel-peer` / `{idleMinutes:120}` if absent | M, R | **Unconditional** (gap-fill) | [deploy.py:2859-2865](../packages/admin/evolve_admin/deploy.py) | dmScope required for Telegram direct-send |
| `logging.file` | Path to `{bot_home}/.openclaw/logs/openclaw.log` | M | **Unconditional overwrite** (bot home may move) | [deploy.py:2877-2881](../packages/admin/evolve_admin/deploy.py) | Prevents unbounded default log |
| `logging.maxFileBytes` | 25 MiB if absent | M | **Unconditional** (gap-fill) | [deploy.py:2882-2884](../packages/admin/evolve_admin/deploy.py) | |
| `models.*` (provider registry) | `sync_provider_models_from_catalog(cfg)` syncs Evolve's model catalog into OC's provider index | M | **Unconditional** (reconcile) | [deploy.py:2941-2948](../packages/admin/evolve_admin/deploy.py); oc_model.py | Cross-ref model-tiers; provider-registry incident memory |
| `agents.main` (legacy) | Deleted/migrated to `agents.defaults` | M | **Unconditional** (repair) | [deploy.py:2288-2300](../packages/admin/evolve_admin/deploy.py); `strip_agents_main` [deploy.py:3603](../packages/admin/evolve_admin/deploy.py) | OC schema rejects `agents.main` |
| Unpinned npm plugin specs | `_repin_unpinned_via_audit()` re-installs `@openclaw/*` at explicit versions | M, P | **Unconditional** (driven by OC audit finding) | [deploy.py:2703-2730](../packages/admin/evolve_admin/deploy.py) | Keyed on `plugins.installs_unpinned_npm_specs` |
| **Initial seed (setup wizard)** | Writes a minimal openclaw.json incl. `tools.exec={security:"full",ask:"on-miss"}` | M, P | **Unconditional at bot creation** | [setup_wizard.py:504](../packages/admin/evolve_admin/setup_wizard.py) (primary), [setup_wizard.py:3516](../packages/admin/evolve_admin/setup_wizard.py) (member) | First-write before the materializer takes over |

**auth-profiles.json** — Evolve treats this as effectively read-only; the **only** write is
`normalize_auth_profile_keys()` ([provisioning.py:1630](../packages/admin/evolve_admin/provisioning.py)),
which canonicalizes profile keys to `<provider>:<id>` shape via `/tmp`+`sudo cp`+`chmod 600`
(idempotent no-op if already canonical, no OC-schema validation). **M, P; Unconditional repair.**

---

## B. Plugin / skill / MCP injection

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Perf/cost/bug notes |
|---|---|---|---|---|---|
| **Evolve TS gateway plugin** | `build_plugin()` compiles `packages/plugin` TS→JS, `sudo cp -R`s `dist/` to root-owned `PLUGIN_INSTALL_DIR`; `install_oc_plugin()` runs `openclaw plugins install --dangerously-force-unsafe-install -l <dir>` as the bot user → creates `~/.openclaw/extensions/evolve/` + `plugins.installs.evolve` | M, R, P | **Unconditional on every deploy** | build [deploy.py:946](../packages/admin/evolve_admin/deploy.py); install [deploy.py:3397,3494](../packages/admin/evolve_admin/deploy.py) | `--dangerously-force-unsafe-install` (OC 2026.6.1+) bypasses the dangerous-code scanner (plugin spawns a TurnObserver); guarded by `verify_plugin_signature()` distDigest check ([deploy.py:3467](../packages/admin/evolve_admin/deploy.py)). This is THE headline mutation — everything else hangs off the plugin being present. |
| `_clear_stale_plugin_install()` | Before install, removes `~/.openclaw/extensions/evolve/` + `plugins.installs.evolve` if the installed manifest schema drifted | M | **Unconditional** (conditional on drift) | [deploy.py:3056-3123](../packages/admin/evolve_admin/deploy.py) | `/tmp`+`sudo cp`, no validation |
| **Evo tools MCP server** | `ensure_evo_tools_mcp_server()` writes `mcp.servers.evo_tools` (a `python3 -m evolve_admin.evo.tools` stdio server) into the **primary bot's** openclaw.json | M, R, P | **Unconditional on deploy (primary bot only)** | [deploy_integration.py:197,86,246](../packages/admin/evolve_admin/evo/tools/deploy_integration.py) | Idempotent gap-fill; member bots never get it. Cross-ref evo-asst aspect. |
| **Operator MCP install** (member bots) | `InstallMcpServerApplier` writes a `{shared_dir}/mcp/launchers/` wrapper, `mcp.servers.<id>` block in openclaw.json, a `{shared_dir}/policy/mcp-allowlist.json` audit entry, then kickstarts the gateway | M, R, P | **Gated — proposal-driven** (arbiter applier; auto-vs-gated per §D risk gates) | `InstallMcpServerApplier` [mcp_server.py:185](../packages/analyzer/arbiter/appliers/mcp_server.py); allowlist [mcp_server.py:117](../packages/analyzer/arbiter/appliers/mcp_server.py) | Reads bot config via `sudo /bin/cat`, writes via `safe_write_bot_config()` |
| **Messaging-channel skills** (Slack/Telegram/Discord) | Admin-UI "Connect" routes write `channels.<id>` (token, dmPolicy, groupPolicy, streaming) + `plugins.entries.<id>.enabled=true`, then restart gateway | M, R | **Gated — operator UI action** (token entry required) | `_oc_install_common.py:56-120` ([skills/](../packages/admin/evolve_admin/skills/)); routes `routes_skills_workspace.py` | Not proposal-driven; user-initiated per channel |
| `installed_plugin_index` (openclaw.sqlite) | Evolve **reads** it (read-only/immutable) during safe-upgrade preflight; OC itself writes it on `plugins install` | (read only) | **n/a — Evolve does not write** | `_read_installs_from_sqlite()` [safe_upgrade.py:674](../packages/admin/evolve_admin/safe_upgrade.py) | Legacy fallback: `plugins/installs.json` |

---

## C. exec-policy flips (`tools.exec.security`)

Evolve **writes** `tools.exec.security` on every deploy — but the *value* is computed by a
3-tier override ladder, so this is the single most operator-tunable mutation in the catalog.

| Priority | Condition | Result | Configurable surface | Code refs |
|---|---|---|---|---|
| 1 | `pod.execPolicy` set in network.json | Honored unconditionally | **Operator override** (network.json) | [deploy.py:1700-1702](../packages/admin/evolve_admin/deploy.py) |
| 2 | `exec-approvals.json` has allowlist entries | `"allowlist"` inferred | Implicit (operator-authored approvals) | [deploy.py:1707-1718](../packages/admin/evolve_admin/deploy.py) |
| 3 | neither | `"full"` (default since 2026-05-25 pivot; was `"deny"`) | Code default | [deploy.py:1731](../packages/admin/evolve_admin/deploy.py) |

Every divergence from the `full`+`on-miss` baseline is recorded via `_record_exec_policy_intent()`
([deploy.py:2464-2490](../packages/admin/evolve_admin/deploy.py)) for the audit trail. The
OC v2026.5.26 **preflight** that blocks `python`/`node` with pipes/`&&`/`-c` is upstream OC, not
an Evolve write — it runs *before* exec policy is consulted (RUNTIME_NOTES.md:31-50), so even
`security:"full"` doesn't override it. History: the v5.18 deny-migration once caused Evolve to
clobber operator fixes on every redeploy (default was `deny` with empty approvals); the 05-25
flip to `full` + the Priority-1 `execPolicy` escape hatch fixed the reversion loop (memory
[[project_oc_2026_5_18_exec_deny_migration]]).

---

## D. Arbiter appliers + generators (proposal-driven mutation)

Appliers are the RSI path by which an **approved proposal** mutates config. The auto-vs-gated
decision is `is_autonomous_eligible()` ([routing.py:97](../packages/analyzer/arbiter/routing.py)):
a proposal auto-applies (`approved_auto`) only if **all** hold — `reversibility=="auto"`,
`blast_radius ∈ {local,bot}`, no touch of `IRREVERSIBILITY_SURFACES`, and a verifiable
`claim` + `revert_on_failure` snapshot are present. Otherwise it is **operator-gated**
(`approved_human`). After apply, the **verify daemon** ([verify/daemon.py](../packages/analyzer/verify/daemon.py))
scans `applied/` and transitions to `succeeded` / `failed_reverted` / `failed_flagged`,
calling `applier.revert()` on a failed claim.

| Applier | What it mutates | Dimensions | Auto vs operator-gated | Code refs (file:line) |
|---|---|---|---|---|
| `ConfigPatch` (L1) | Generic JSON files via `<file>::<dotted.key>`; **explicitly refuses** bot openclaw.json paths | M | Auto-eligible if risk gates pass | guard [config_patch.py:50,167-169](../packages/analyzer/arbiter/appliers/config_patch.py) |
| `UpdatePermissionConfig` (L2) | `openclaw.json::tools.*` / `commands.*` (schema-derived whitelist); refuses `exec.ask="off"` and the `security:"full"+ask:"off"` open posture; writes durable overrides to `{shared_dir}/sandbox/overrides/<bot>.json`, re-materializes, `safe_write_bot_config()`, kickstart | M, R, P | Auto-eligible if risk gates pass | [permissions.py:282-622](../packages/analyzer/arbiter/appliers/permissions.py); whitelist 51-86; guards 128-155 |
| `UpdateExecApproval` (L2) | `exec-approvals.json` (approved command patterns); rejects denylist patterns | M, R, P | Auto-eligible if risk gates pass | [permissions.py:687-775](../packages/analyzer/arbiter/appliers/permissions.py) |
| `UpsertCronJob` / `RemoveCronJob` (L2) | `cron/jobs.json`; rejects uncapped `agentTurn` payloads + denylisted cron | M, R, C | Auto-eligible if risk gates pass | [permissions.py:869-928](../packages/analyzer/arbiter/appliers/permissions.py) |
| `UpdatePermissionBaseline` | `{shared_dir}/policy/permission-baseline.json` (pod-wide); refuses denylist removals (v1 add-only) | M, P | Auto-eligible if risk gates pass | [permissions.py:1082-1087](../packages/analyzer/arbiter/appliers/permissions.py) |
| `UpdateAgentDefaults` | `openclaw.json::agents.defaults.<dotpath>` (whitelist, currently `cacheRetention`) | M, R, C | Auto-eligible if risk gates pass | [agent_defaults.py](../packages/analyzer/arbiter/appliers/agent_defaults.py) |
| `TierAdjustment` | `openclaw.json::agents.defaults.model.<role>` (tier downgrade) | M, C | Auto-eligible (reversible downgrade) | [appliers/](../packages/analyzer/arbiter/appliers/); gens `budget_hawk`, `efficiency_hawk` |
| `ReconcileModelCatalog` | `network.json::agents.defaults.models` (catalog tier whitelist) | M | Auto-eligible if risk gates pass | gen `bot_config_integrity` |
| `AdoptModel` | `network.json::models` + role mappings + `roleCaps` | M, C | **Operator-gated** (needs role/cap choices) | [adopt_model.py](../packages/analyzer/arbiter/appliers/adopt_model.py); gen `model_discovery` |
| `UpdateAutonomyPosture` | `{shared_dir}/autonomy/posture/<bot>.json` (autonomy rung) | M, R | **Operator-gated for promotions** (permanent carve-out); demotions auto-eligible | [routing.py:69-94,112](../packages/analyzer/arbiter/routing.py); gen `autonomy_promoter` |
| `SoulEdit` | Bot persona/soul narrative | M, R | **Operator-gated** (spec §4.4 human-only) | [soul_edit.py](../packages/analyzer/arbiter/appliers/soul_edit.py); gen `persona_tuner` |
| `InstallMcpServer` / `RemoveMcpServer` / `UpdateMcpServerConfig` | `mcp.servers.*` (see §B) | M, R, P | Auto-eligible if risk gates pass | [mcp_server.py:185](../packages/analyzer/arbiter/appliers/mcp_server.py) |
| Enable/Disable `PluginEntry`, `UpdateAllowDeny`, `UpdatePluginHookPolicy`, `UpdateHookBaseline` | `plugins.entries.*` enable/allow-deny/hook policy | M, R | Auto-eligible if risk gates pass | appliers/ (Phase B) |
| `BuildApp` / `ManifestUpdate` / `DeprecateApp` / `RetireOrphan` | App manifests + forge builds + registry | M, C | **Operator-gated** (external completion / review) | appliers/; gens `app_birth_detector`, `efficiency_hawk` |
| `ContentScan` / `Investigation` / `WorkflowInstruction` / `AddSignalCollection` | **No config mutation** (informational/FYI) | — | n/a (claim-less, operator marks complete) | apply.py:82-90 |

Generators producing config-mutating proposals (charters at
`packages/analyzer/generators/<id>/charter.yaml`): `auth_drift_filler`, `app_permission_drift`,
`bot_config_integrity`, `budget_hawk`, `cache_ttl_tuner`, `cost_root_cause_correlator`,
`cron_caps_filler`, `efficiency_hawk`, `exec_outcome_investigator`, `model_discovery`,
`autonomy_promoter`, `persona_tuner`, `security_warden`, `sysadmin_watchdog`. Each declares its
`action_type`; whether the resulting mutation auto-applies is decided by `is_autonomous_eligible`,
not the generator.

---

## E. workspace/evolve/ + manifest writes (lower-stakes — bot-loaded but not config)

These land under the bot's `workspace/evolve/` (write-ACL granted by `set_evolve_read_acl()`,
so direct write, no sudo) or `workspace/manifests/`. They shape what the bot *sees* but are not
openclaw.json config.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Notes |
|---|---|---|---|---|---|
| Bot docs (SOUL/AGENTS/MEMORY/README) | Seeded + re-asserted on deploy | M | **Unconditional** (idempotent; preserves operator edits ≥1500 bytes) | `install_bot_docs` [deploy.py:7378-7467](../packages/admin/evolve_admin/deploy.py); [bot_doc_seeding.py:151](../packages/admin/evolve_admin/bot_doc_seeding.py) | Primary's AGENTS.md gets an auto-glossary append |
| Gap-fill doc stubs | SOUL/AGENTS/HEARTBEAT/IDENTITY/TOOLS/USER stubs if OC onboard skipped | M | **Unconditional** (once-only, creation path) | [bot_doc_seeding.py:109-114](../packages/admin/evolve_admin/bot_doc_seeding.py) | |
| `pod_config.json` | Pod-config audit slice mirrored into the bot | M | **Unconditional** (idempotent on content) | [audit_pod_config.py:232-298](../packages/admin/evolve_admin/applications/audit_pod_config.py) | |
| `rec-hints.json` | Recommendation hints written at end of each Engine refresh | M, C | **Unconditional** (per RSI cycle) | [better_engine/hints.py:131-151](../packages/admin/evolve_admin/better_engine/hints.py) | |
| `pending-admin-tasks.json` | Task queue the bot reads | M | **Unconditional** (per task mutation) | [better_engine/pending_tasks.py:95-114](../packages/admin/evolve_admin/better_engine/pending_tasks.py) | |
| App Instance manifests (`{app_id}.json`, v7-arc) | Minted on forge install / scanner detection | M | **Unconditional** (per-app) | `native_write.mint_v7_arc_app()` [native_write.py:279-451](../packages/admin/evolve_admin/applications/native_write.py) | atomic temp+rename, /tmp+sudo fallback |
| `.scan-status.json` | Scanner progress (5 writes per run) | M, C | **Unconditional** (per scan) | [scanner.py:2948-2990](../packages/admin/evolve_admin/applications/scanner.py) | |
| `POD_CONDUCT.md` | Copied into bot workspace | M | **Unconditional** | `inject_pod_conduct` [deploy.py:3647-3699](../packages/admin/evolve_admin/deploy.py) | |
| OC memory (`workspace/memory/*`) | **Evolve reads/inventories only — never writes** | (read only) | n/a | `auto_memory.py:46-115` | OC/bot writes these |

---

## Candidates to make dialable

Ranked by footprint reduction vs. capability lost. A **Passive** posture operator wants the
managed-updater + legibility benefits without Evolve rewriting how their bots run.

1. **Auto-appliers → propose-don't-apply (highest leverage, cleanest).** Force every arbiter
   applier to `approved_human` regardless of `is_autonomous_eligible` — a single gate at
   [routing.py:97](../packages/analyzer/arbiter/routing.py). Evolve still surfaces proposals;
   nothing mutates config without an operator click. **Loses:** hands-off self-tuning (budget
   downgrades, cron caps, catalog reconcile). **Keeps:** all visibility. This is the cleanest
   posture lever because the propose/apply split already exists — F-3 just flips the default.

2. **`tools.exec.security` value → operator-pinned.** Default Priority-1 `execPolicy` so Evolve
   never *infers* the exec policy, only honors the operator's. **Loses:** auto-allowlist inference
   from `exec-approvals.json`. **Keeps:** a fixed, predictable exec posture. (Already partly
   dialable via network.json — F-3 would surface it on Settings.)

3. **Plugin hook access → off (`allowConversationAccess`).** Currently force-`true`
   ([deploy.py:2622-2628](../packages/admin/evolve_admin/deploy.py)). A passive operator who wants
   inventory-only could drop the hot-path conversation hooks. **Loses:** `llm_output`/`agent_end`
   observation → most cost/quality monitors go dark (heavy cross-ref to F-1b runtime + F-1c cost).
   **Keeps:** plugin present for tier routing + dashboard. *This is a coordinated dial, not an
   independent one — turning it off guts the monitor layer.*

4. **MCP / channel injection → operator opt-in only.** `evo_tools` MCP and channel skills are
   already operator/proposal-gated; the **Evolve TS plugin install itself** is the one
   unconditional injection ([deploy.py:3494](../packages/admin/evolve_admin/deploy.py)). A true
   "dashboard mode" would deploy *without* installing the gateway plugin — Evolve becomes a pure
   external observer. **Loses:** essentially all runtime features (tier routing, hooks, dashboard,
   cost ledger). **Keeps:** repo-puller updates, host/launchd inventory, config legibility. This is
   the floor of the Passive posture and the biggest single footprint cut.

5. **Doc / workspace re-assertion → seed-once.** Make SOUL/AGENTS/pod_config/POD_CONDUCT
   write-once instead of re-asserted every deploy (the ≥1500-byte guard already protects hand
   edits, but the *default* is overwrite). **Loses:** drift correction. **Keeps:** initial seeding.
   Low-stakes, low-payoff — list it for completeness, not priority.

**Always-on floor (NOT dialable without breaking the bot or security).** These should stay
unconditional even at Passive: `gateway.auth.token` generation (closes a security finding),
`agents.main`→`defaults` migration + stale-key pruning (OC schema rejects the old shape),
`logging.file` (prevents unbounded logs), npm re-pin (supply-chain). The footprint contract
should mark these as **non-reducible** so a posture dial can't silently produce a non-booting or
insecure config (spec invariant: "a toggle that silently leaves the subsystem half-on is worse
than no toggle").

---

*F-1d complete. Routes to F-3 (posture-dial design) — candidates 1–4 are the levers; F-3 owns
the per-subsystem default matrix + storage. Implementation of each toggle deposits to the owning
aspect: appliers→`rsi`, exec/hooks→`edr`, plugin/MCP injection→`skills`/`deploy`.*
