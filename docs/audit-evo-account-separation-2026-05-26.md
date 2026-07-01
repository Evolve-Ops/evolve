# Phase E.1 audit — evo's current direct-fs reach

**Date:** 2026-05-26.
**Spec:** [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) Phase E.1.
**Scope:** read-only audit; no code changes.
**Goal:** identify every direct-fs read or privileged subprocess call that runs inside evo's OC gateway process today, so Phase E.3 knows what to re-plumb when evo moves off the `evolve` macOS user.

---

## Runtime-context split (read this first)

The directory `packages/admin/evolve_admin/evo/` mixes two runtime contexts:

| Subtree | Runtime | Affected by Phase E.2? |
|---|---|---|
| `evo/tools/` (registered MCP tools + `mcp_server.py`) | The MCP-server subprocess spawned by evo's OC gateway via `python3 -m evolve_admin.evo.tools`. Runs as **gateway user** — currently `evolve`, future `evo`. | **Yes** — every direct-fs call here happens as the new unprivileged `evo` user post-E.2. |
| `evo/{dispatch,proxy,handlers,wizard,audit,notifications,guide,…}` | Flask routes / handlers inside the admin daemon (the `ai.evolve.evolve.admin-ui` LaunchDaemon). Reached over HTTP by the OC plugin, by browser sessions, and by `proxy.py` itself (which subprocesses the `openclaw` CLI). Runs as **`evolve` user** regardless of E.2. | **No** — admin daemon stays on `evolve` user; its fs reach is unchanged. |

The audit's primary subject is `evo/tools/`. Other-subtree findings are noted at the end for completeness but are out of scope for E.2/E.3/E.4.

Also out of scope: the JS plugin at `packages/plugin/` (loaded into every bot's gateway including evo's). It runs as the gateway user already — same boundary all member bots already live with. No fs read it does today succeeds outside its own user's reach.

---

## Verification on the mini (2026-05-26)

Run before classifying to ground the C/D distinction:

```
$ ssh mini 'id evolve'
uid=507(evolve) gid=20(staff) groups=20(staff),0(wheel),12(everyone),...

$ ssh mini 'id team-bot-a; id admin-bot'
uid=502(team-bot-a) gid=20(staff) groups=20(staff),12(everyone),...
uid=505(admin-bot) gid=20(staff) groups=20(staff),12(everyone),...

$ ssh mini 'stat -f "%Sp %Su:%Sg %N" \
    /Users/Shared/evolve /Users/Shared/evolve/signals \
    /Users/Shared/evolve/network.json /Users/Shared/evolve-repo \
    /Library/LaunchDaemons/ai.evolve.evolve.heal.plist'
drwxrwxrwt evolve:wheel /Users/Shared/evolve
drwxr-xr-x evolve:wheel /Users/Shared/evolve/signals
-rw-r--r-- evolve:wheel /Users/Shared/evolve/network.json
drwxrwxr-x evolve:staff /Users/Shared/evolve-repo
-rw-r--r-- root:wheel  /Library/LaunchDaemons/ai.evolve.evolve.heal.plist

$ ssh mini 'stat -f "%Sp %Su:%Sg %N" /Users/team-bot-a/.openclaw \
    /Users/team-bot-a/.openclaw/openclaw.json /Users/team-bot-a/.openclaw/workspace'
drwx------+ team-bot-a:staff /Users/team-bot-a/.openclaw
-rw-------+ team-bot-a:staff /Users/team-bot-a/.openclaw/openclaw.json
drwx------+ team-bot-a:staff /Users/team-bot-a/.openclaw/workspace
```

What this tells us:

1. **`/Users/Shared/evolve/`** is world-readable / world-writable-with-sticky. Type-B reads survive the account split unchanged. ✅
2. **`/Users/Shared/evolve-repo/`** is mode `drwxrwxr-x` with files at `rw-rw-r--` / `rwxrwxr-x`. Staff group has rw; world has r. Bots are created in `staff` group, so the new `evo` user reads it without any extra grant. Type-D verified. ✅
3. **`/Library/LaunchDaemons/ai.evolve.*.plist`** is mode `-rw-r--r-- root:wheel`. World-readable. Read-only access (e.g. `plist.exists()`, `plistlib.load`) survives the split. The *write* path (`sudo /bin/launchctl kickstart`) does **not** — sudoers grants belong to `evolve`, not to a freshly-created `evo` user. Classify these as type-E (system path) for reads and type-C-equivalent (privileged-op) for kicks.
4. **`/Users/<other_bot>/.openclaw/`** is mode `drwx------+` — bot-owner-only, ACL-augmented for `evolve`. The `+` indicates the ACL grant `set_evolve_read_acl` installs. A non-ACL'd `evo` user gets EACCES on the parent directory and cannot descend even though some inner files (e.g. `workspace/manifests/*.json`) are mode `0644`. Every direct read into another bot's `.openclaw/` is therefore type-C and breaks post-E.2 unless the new `evo` user gets an equivalent ACL grant, which the spec explicitly rejects (§ Principle).

---

## Per-tool findings (`evo/tools/` registered tools)

Tools listed in registration order from [evo/tools/__init__.py:235](packages/admin/evolve_admin/evo/tools/__init__.py:235). Each row gives the dominant fs reach — for tools that delegate to imported helpers (`evolve_admin.deploy`, `evolve_admin.recovery`, `evolve_admin.breakers_enforce`, `arbiter.appliers.*`, `permissions.writer`, `tile_metrics.compute_tile_data`, etc.) the deepest path the helper itself reaches is what's classified.

### pod_state read tools

| Tool | File:line | Path | Bucket | Notes |
|---|---|---|---|---|
| pod_state.signals.{firing,history} | [pod_state_signals.py:89](packages/admin/evolve_admin/evo/tools/pod_state_signals.py:89) → `signals.store` | `{shared_dir}/signals/{firing,archived}/*.json` | **B** | All under shared dir; survives split. |
| pod_state.proposals.{pending,snoozed} | [pod_state_proposals.py](packages/admin/evolve_admin/evo/tools/pod_state_proposals.py) → `arbiter.store` | `{shared_dir}/proposals/{pending,snoozed}/*.json` | **B** | Survives split. |
| pod_state.bots | [pod_state_bots.py:184](packages/admin/evolve_admin/evo/tools/pod_state_bots.py:184) — reads `network_path` | `/Users/Shared/evolve/network.json` | **B** | Survives. |
| pod_state.bots | [pod_state_bots.py:128](packages/admin/evolve_admin/evo/tools/pod_state_bots.py:128) → `tile_metrics.compute_tile_data` → [tile_metrics.py:321](packages/analyzer/tile_metrics.py:321) | `/Users/<bot_user>/.openclaw/workspace/manifests/.scan-status.json` | **C** | Cross-bot read used for the `scan_needed` chip computation. |
| pod_state.bots | …same path → [tile_metrics.py:1201](packages/analyzer/tile_metrics.py:1201) | `/Users/<bot_user>/.openclaw/openclaw.json` | **C** | Cross-bot read used for the per-bot security chip. |
| pod_state.bots | …same path → [tile_metrics.py:1087](packages/analyzer/tile_metrics.py:1087) | `/Users/Shared/evolve-repo/.git/FETCH_HEAD` | **D** | Verified world-readable. |
| pod_state.bots | [status.py:99-111](packages/admin/evolve_admin/status.py:99) (via `evolve_admin.status.network_status`) | `{shared_dir}/metrics/<date>/<bot>.json` | **B** | Survives. |
| pod_state.host | [pod_state_host.py:70](packages/admin/evolve_admin/evo/tools/pod_state_host.py:70) → `host_health.collect_host_health` (psutil only) | n/a (psutil); reads `/Users` via `disk_usage` | A/D | Reads via syscall, not Python fs. Works for any user. |
| pod_state.audit | [pod_state_audit.py:185](packages/admin/evolve_admin/evo/tools/pod_state_audit.py:185) | HTTP `GET /api/security/audit` | n/a | Already HTTP. Auth layer needed (Phase E.3 task), but no fs change. |
| pod_state.usage | [pod_state_usage.py:71](packages/admin/evolve_admin/evo/tools/pod_state_usage.py:71) → `pod_rollup.compute_spend_rollup_live` → [pod_rollup.py:207](packages/admin/evolve_admin/pod_rollup.py:207) | `{shared_dir}/<bot_id>/turns/turns-<date>.jsonl` | **B** | Survives. |
| pod_state.errors | [pod_state_errors.py:41](packages/admin/evolve_admin/evo/tools/pod_state_errors.py:41) | `{shared_dir}/status/<bot>.json` | **B** | Survives. |
| pod_state.rollbacks | [pod_state_rollbacks.py:60](packages/admin/evolve_admin/evo/tools/pod_state_rollbacks.py:60) → `evolve_admin.recovery` rollback dir | `{shared_dir}/recovery/rollbacks/` | **B** | Survives. |
| pod_state.app_scan_status | [pod_state_app_scan_status.py:154-208](packages/admin/evolve_admin/evo/tools/pod_state_app_scan_status.py:154) | `{shared_dir}/applications/<bot>/.scan-status.json`, `…/*.json` | **B** | Survives. |
| pod_state.backup_status | [pod_state_backup.py:49](packages/admin/evolve_admin/evo/tools/pod_state_backup.py:49) | `/Library/LaunchDaemons/ai.evolve.<bot>.backup.plist` | **E** | World-readable system path — verified. Read survives. |
| pod_state.backup_status | [pod_state_backup.py:55,86,112](packages/admin/evolve_admin/evo/tools/pod_state_backup.py:55) — `_git("log …")` / `_git("remote get-url origin")` in `/Users/<bot_user>/.openclaw/workspace/` | git subprocess inside another bot's workspace | **C** | Workspace parent is mode 700 + ACL; cross-bot read fails without ACL grant. |
| pod_state.config_drift | [pod_state_config_drift.py:98](packages/admin/evolve_admin/evo/tools/pod_state_config_drift.py:98) | HTTP `GET /api/security/backup-status` (admin daemon) | n/a | Already HTTP. |
| pod_state.breakers | [action_breakers.py:766-769](packages/admin/evolve_admin/evo/tools/action_breakers.py:766) → `breakers.store.list_active` | `{shared_dir}/breakers/` | **B** | Survives. |
| pod_state.pause_state | [action_pod.py](packages/admin/evolve_admin/evo/tools/action_pod.py) → `evolve_admin.recovery._read_pause_state` | `{shared_dir}/recovery/pause-state.json` | **B** | Survives. |
| pod_state.forge_job | [action_app.py](packages/admin/evolve_admin/evo/tools/action_app.py) → `evolve_admin.applications.forge_jobs` | `{shared_dir}/forge/jobs/<id>.json` | **B** | Survives. |
| pod_state.tool_gaps | [evo_telemetry.py](packages/admin/evolve_admin/evo/tools/evo_telemetry.py) → `evo.tool_gaps` | `{shared_dir}/evo/tool-gaps.jsonl` (admin-daemon-owned) | **B** | Survives — same shared dir. |

### config read tools

| Tool | File:line | Path | Bucket | Notes |
|---|---|---|---|---|
| config.bot | [config_bot.py:129](packages/admin/evolve_admin/evo/tools/config_bot.py:129) | `/Users/<bot_user>/.openclaw/openclaw.json` direct read | **C** | EACCES post-E.2; needs daemon endpoint. |
| config.bot | [config_bot.py:138](packages/admin/evolve_admin/evo/tools/config_bot.py:138) | `sudo /bin/cat /Users/<bot_user>/.openclaw/openclaw.json` fallback | **C** (privileged-op) | Sudo grant only exists for `evolve` user; fails post-E.2. |
| config.network | [config_network.py](packages/admin/evolve_admin/evo/tools/config_network.py) | `network_path` (= `/Users/Shared/evolve/network.json`) | **B** | Survives. |

### action tools — HTTP-already (auth layer only)

These already route through the admin daemon HTTP API and just need Phase E.3's auth layer wrapped around them. Zero fs work to re-plumb.

| Tool | Endpoint |
|---|---|
| action.signal.snooze | already calls `signals.store` directly on shared dir — **B** survives. (See note below.) |
| action.signal.dismiss | same — **B**. |
| action.proposal.snooze | `arbiter.store.move_proposal` within shared dir — **B**. |
| action.proposal.reject | same — **B**. |
| action.proposal.apply | `arbiter.appliers.*` — **mixed**; see Privileged-op section below. |
| action.bot.rescan_apps | HTTP `POST /api/applications/scan?bot=<id>` ([action_bot.py:634](packages/admin/evolve_admin/evo/tools/action_bot.py:634)) — n/a. |
| action.plugin.{enable,disable} | HTTP `POST /api/plugins-admin/propose-{enable,disable}` ([action_plugin.py:178,307](packages/admin/evolve_admin/evo/tools/action_plugin.py:178)) — n/a. |
| action.security.accept_drift | HTTP `POST /api/security/accept-drift` ([action_security.py:130](packages/admin/evolve_admin/evo/tools/action_security.py:130)) — n/a. |

Note on signals/proposals stores: these write JSON files under `{shared_dir}/signals/` and `{shared_dir}/proposals/`. The shared dir is mode `drwxrwxrwt` (world-writable sticky) so a non-`evolve` user can write/move files there with no sudo. **However** Phase E.4's success criterion #3 ("every privileged capability evo uses goes through the admin daemon API") arguably implies these should be routed too, even though they technically work as-is. Recommend leaving them direct in E.3 and revisiting only if a concrete security argument shows up — they don't expand evo's reach beyond what the principle already permits (shared dir is the pod-wide ABI).

### action tools — privileged-op, **break post-E.2**

These call into helpers that depend on the `evolve` user's sudoers grants and/or ACL access to other bots' homes. Without re-plumbing, every one of them will error after Phase E.2.

| Tool | Privileged op | File:line |
|---|---|---|
| action.bot.trip_breaker (cost) | `permissions.writer.write_openclaw_fields` → `/tmp` staging + `sudo /bin/cp` + `sudo chown` + `sudo chmod` + `sudo /bin/launchctl kickstart` on another bot's `openclaw.json` | [breakers_enforce.py:586](packages/admin/evolve_admin/breakers_enforce.py:586), [permissions/writer.py](packages/analyzer/permissions/writer.py) |
| action.bot.trip_breaker (full) | same shape, also `sudo launchctl bootout` to take gateway down | same |
| action.bot.reset_breaker | `permissions.writer.write_openclaw_fields` + kickstart | [breakers_enforce.py:606+](packages/admin/evolve_admin/breakers_enforce.py:606) |
| action.pod.trip_breaker | fan-out of the above across every bot | [action_breakers.py:472+](packages/admin/evolve_admin/evo/tools/action_breakers.py:472) |
| action.pod.reset_breaker | same fan-out | [action_breakers.py:637+](packages/admin/evolve_admin/evo/tools/action_breakers.py:637) |
| action.pod.pause_all | `evolve_admin.recovery.pause_all` → `sudo launchctl bootout` per bot | [recovery.py:380](packages/admin/evolve_admin/recovery.py:380) |
| action.pod.resume_all | `evolve_admin.recovery.resume_all` → `sudo launchctl bootstrap` per bot | [recovery.py:418](packages/admin/evolve_admin/recovery.py:418) |
| action.bot.restart | `evolve_admin.deploy.restart_gateway` → `sudo /bin/launchctl kickstart` | [action_bot.py:88](packages/admin/evolve_admin/evo/tools/action_bot.py:88) |
| action.bot.redeploy | `evolve_admin.deploy.deploy_bot` → full sudo-heavy deploy sequence | [action_bot.py](packages/admin/evolve_admin/evo/tools/action_bot.py) |
| action.bot.remove | `evolve_admin.retire.retire_bot` via `/api/lifecycle/retire` → `sudo launchctl bootout` per per-bot Evolve plist + sudoers-protected openclaw.json edit (re-plumbed by PR #1903; `deploy.remove_bot` removed in follow-up) | [action_bot.py:375+](packages/admin/evolve_admin/evo/tools/action_bot.py:375) |
| action.bot.backup_workspace | `subprocess(["sudo","/bin/launchctl","kickstart",...])` directly | [action_bot.py:831](packages/admin/evolve_admin/evo/tools/action_bot.py:831) |
| action.infra.daemon_restart | `subprocess(["sudo","/bin/launchctl","kickstart",...])` directly | [action_infra.py:169](packages/admin/evolve_admin/evo/tools/action_infra.py:169) |
| action.app.install | `evolve_admin.applications.forge_jobs.create_install_job` writes shared-dir state (B, OK) but the forge sweep daemon that consumes it is a separate process — should still work. **Verify:** does any code path inside `action.app.install` itself shell out as `evolve`? Likely not — the heavy lifting is the daemon. Mark as **B-likely** pending follow-up. | [action_app.py:80+](packages/admin/evolve_admin/evo/tools/action_app.py:80) |
| action.app.audit | calls `evolve_admin.app_audit_tier3` — verify it doesn't sudo. Mark **TBD**. | [action_app.py](packages/admin/evolve_admin/evo/tools/action_app.py) |
| action.proposal.apply | dispatches to `arbiter.appliers.<kind>`. Some appliers are shared-dir-only (B); others (UpdatePermissionConfig per memory `project_l1_l2_applier_architecture`) use `/tmp` + sudo + kickstart on a bot's `openclaw.json`. **Partial C** depending on applier kind. | [action_proposal_apply.py:84](packages/admin/evolve_admin/evo/tools/action_proposal_apply.py:84) |
| action.evo.log_tool_gap | shared-dir write — **B**. Survives. | [evo_telemetry.py](packages/admin/evolve_admin/evo/tools/evo_telemetry.py) |

---

## Bucket totals

Counting registered MCP tools (the units that need re-plumbing decisions). For tools with multiple paths, the dominant / hardest-to-fix bucket counts.

| Bucket | Count | What it means |
|---|---|---|
| A — evo's home/workspace | **0** | No registered tool reads paths inside `/Users/evolve/` directly. (After E.2 the new home is `/Users/evo/`; same architecture, no change needed.) |
| B — shared dir | **14** | `pod_state.{signals.firing,signals.history,proposals.pending,proposals.snoozed,bots*,usage,errors,rollbacks,app_scan_status,breakers,pause_state,forge_job,tool_gaps}`, `config.network`, action.signal/proposal snooze/dismiss/reject, action.evo.log_tool_gap. (\* `pod_state.bots` is B-and-C — see note below.) |
| C — other bots' `.openclaw/` | **3 read, ~10 privileged-op** | Reads: `config.bot`, `pod_state.backup_status` (via git in workspace), `pod_state.bots` (via tile_metrics chip computation). Privileged-ops touching other bots: ~10 action tools (trip/reset breaker × 4, pause/resume × 2, restart/redeploy/remove × 3, backup_workspace, infra.daemon_restart, proposal.apply when applier is L2). |
| D — `/Users/Shared/evolve-repo/` | **1** | Indirect read only, via `tile_metrics`. Verified world-readable on the mini; survives the split. No re-plumb. |
| E — unexpected | **1** | `/Library/LaunchDaemons/ai.evolve.*.plist` (read by `pod_state.backup_status` and `action.infra.daemon_restart` validate). World-readable; survives. The companion sudo-launchctl write is what breaks, captured under C-privileged. |

Total registered MCP tools: 30 (counting trip/reset breaker as 4, action.bot.* as 5, etc.). About **half** survive the split with no work (the shared-dir B set + the already-HTTP set). The other half need either an admin daemon endpoint or an auth-layer wrap.

---

## Type-C findings — candidate admin daemon endpoints

For each cross-bot-fs or privileged-op tool, the suggested HTTP shape evo's MCP tool would call instead. Existing endpoints noted; where one already exists, the work is just adding auth and switching the tool's internals from in-process call to HTTP. Where none exists yet, the shape is a new design Phase E.3 takes as a starting point.

### C1. Read another bot's openclaw.json

- **Current path:** `config.bot` direct read + sudo /bin/cat fallback ([config_bot.py:129](packages/admin/evolve_admin/evo/tools/config_bot.py:129)).
- **Existing endpoint:** none specifically for this — but the admin-UI's Bots page already loads it server-side; pull that into a registered route.
- **Proposed:** `GET /api/admin/bot/<bot_id>/openclaw-config` → returns the redacted projection that `_project_bot_config` already produces ([config_bot.py:149](packages/admin/evolve_admin/evo/tools/config_bot.py:149)). Keep secret redaction server-side — evo's tool then doesn't need to know which fields are secret. Auth scope: any tool caller authenticated as the `evo` user.
- **Body:** none (GET).
- **Returns:** same dict as `config.bot` produces today.

### C2. Read another bot's backup state (git in workspace)

- **Current path:** `pod_state.backup_status` shells `git log` / `git remote` inside `/Users/<bot>/.openclaw/workspace/` ([pod_state_backup.py:79,110](packages/admin/evolve_admin/evo/tools/pod_state_backup.py:79)).
- **Existing endpoint:** none — the admin-UI's Security → Backups subtab renders the same data but I didn't trace whether it goes through an HTTP endpoint or the in-process status helper.
- **Proposed:** `GET /api/admin/bot/<bot_id>/backup-status` → identical projection. Server-side runs the existing `_last_backup_commit` / `_git_remote` / `_read_plist_schedule` helpers (which today live in the tool) — extract them to `evolve_admin/web/backup_routes.py`.
- **Returns:** the same flat dict `_backup_status_handler` returns today.

### C3. Compute per-bot tile chips (cross-bot reads inside tile_metrics)

- **Current path:** `pod_state.bots` → `tile_metrics.compute_tile_data` → reads `/Users/<bot>/.openclaw/workspace/manifests/.scan-status.json` and `/Users/<bot>/.openclaw/openclaw.json` directly.
- **Existing endpoint:** **yes** — `GET /api/status` already returns the same data the admin UI dashboard renders, with tile chips computed server-side.
- **Proposed:** point `pod_state.bots` at `GET /api/status` instead of importing `status.network_status` + `tile_metrics.compute_tile_data` in-process. Pure projection swap; the heavy work stays in the admin daemon where it already runs successfully today (the daemon is the `evolve` user).
- **Returns:** the same shape `pod_state.bots` returns now, derived from /api/status.

### C4. Apply a config patch to another bot (gateway-kickstart-required)

- **Current path:** `action.bot.{trip,reset}_breaker`, `action.pod.{trip,reset}_breaker`, `action.proposal.apply` (when applier is L2) → `permissions.writer.write_openclaw_fields` → /tmp + sudo cp/chown/chmod/launchctl.
- **Existing endpoint:** **partial** — proposal apply has `POST /api/arbiter/proposals/<id>/apply`. Breakers do not — the breakers store has its own write path with no HTTP entry.
- **Proposed (breakers):** `POST /api/admin/breakers/trip` and `POST /api/admin/breakers/reset` taking `{scope, breaker_type, duration, reason}`. Server runs `store.trip` + `enforce.enforce_trip` (which today already happens inside the admin daemon for some flows — the heal daemon resets breakers on its own).
- **Proposed (proposal apply):** route `action.proposal.apply` through the existing `POST /api/arbiter/proposals/<id>/apply` endpoint. Removes the L2-applier privilege from the MCP subprocess entirely.

### C5. Gateway / launchd lifecycle

- **Current paths:** `action.bot.restart`, `action.bot.redeploy`, `action.bot.remove`, `action.bot.backup_workspace`, `action.infra.daemon_restart`, `action.pod.pause_all`, `action.pod.resume_all`.
- **Existing endpoints:**
  - `POST /api/admin/gateway/<bot>/restart` — likely exists for the admin UI's Restart button. Verify.
  - `POST /api/admin/bot/<bot>/redeploy` — likely exists for the Redeploy button. Verify.
  - `POST /api/admin/bot/<bot>/remove` — likely exists. Verify.
  - `POST /api/admin/pause-all` / `POST /api/admin/resume-all` — `recovery.pause_all` / `resume_all` are wired to admin UI buttons; verify the HTTP shim is there.
  - `POST /api/admin/bot/<bot>/backup` (kickstart) — uncertain; may need new.
  - `POST /api/admin/infra/<daemon_id>/restart` — uncertain; may need new.
- **Proposed:** for each tool, replace the in-process subprocess call with an HTTP POST to the corresponding admin daemon endpoint. Where no endpoint exists yet, add one — the server-side code already has all the helpers (`recovery._bootout_one_bot`, `deploy.restart_gateway`, etc.); the endpoint is a thin wrapper.

### C6. App install / audit

- **Current path:** `action.app.install` calls `applications.forge_jobs.create_install_job` — writes shared-dir state, no privileged ops in the tool itself. `action.app.audit` calls into `app_audit_tier3` — verify (separate task).
- **Proposed:** keep direct if confirmed shared-dir-only; route through HTTP otherwise.

---

## Load-bearing tools (re-plumb in this order in Phase E.3)

The tools below are the ones evo calls most often in normal operation. If any of them breaks during E.2 → E.3, evo's everyday usefulness drops sharply. Re-plumb these FIRST, in this order:

1. **pod_state.bots** — every operator session that opens the dashboard chat starts with the model wanting to know bot state. Type-C indirect (tile chips). Easy fix: route through `GET /api/status`.
2. **config.bot** — second-most-asked-about query ("what model is X running"). Type-C direct. Needs the new `GET /api/admin/bot/<bot>/openclaw-config` endpoint.
3. **pod_state.backup_status** — quoted in the operator's own troubleshooting workflows ("when did team-bot-a last back up?"). Type-C. Needs `GET /api/admin/bot/<bot>/backup-status`.
4. **action.bot.restart** — most-invoked write action. Privileged. Likely already has the endpoint.
5. **action.proposal.apply** — the core RSI loop. Privileged-mixed. Endpoint exists.
6. **action.bot.backup_workspace + action.infra.daemon_restart** — used during incidents. Privileged. New endpoints needed.

After those six, the breaker action tools and pause/resume can re-plumb in parallel without blocking everyday use.

The remaining ~14 type-B tools and the already-HTTP set can move in any order with the auth-layer wrap.

---

## Findings outside `evo/tools/` (informational; not in E.1 scope)

Recorded here so they're not lost. None of these run inside evo's gateway today; all stay on the admin daemon side post-E.2 and are unaffected. Listed for the writer of E.5 (decommission `evolve` as a bot account) since some of these references will need updating when the bot/account distinction is finalized.

- `evo/proxy.py:153,434,538,699` — admin daemon proxy code; reads jsonl logs from shared dir. No change.
- `evo/dispatch.py`, `evo/handlers/*.py`, `evo/wizard/*.py`, `evo/audit.py`, `evo/notifications.py`, `evo/guide.py`, `evo/inspector.py`, `evo/glossary.py`, `evo/skill_retirement.py`, `evo/name_resolver.py`, `evo/identity_discovery.py`, `evo/tool_gaps.py` — all admin daemon Flask code. Some do direct cross-bot reads (e.g. `wizard/user_profile_writer.py` reads other bots' `.openclaw/` for profile harvesting); those continue to work because the admin daemon stays on the `evolve` user.
- Skills at `packages/admin/evolve_admin/skills/*_install.py` — admin daemon CLI helpers, not gateway-runtime code.
- Bot soul/agents files at `packages/analyzer/evolve_bot/` — content shipped to evo's deployed home as part of `evolve-admin deploy evolve`. After E.2 these get deployed to `/Users/evo/.openclaw/` instead of `/Users/evolve/.openclaw/`. No code change; just a deploy-time path update (E.2 work).

---

## Open questions surfaced by the audit

1. **`pod_state.host` calls `psutil.disk_usage("/Users")`** from inside the MCP subprocess. Post-E.2 this still works (psutil uses statfs, no Python fs perm gate), but the *number* will be the same regardless of which user calls it. No work.
2. **`action.app.install` / `action.app.audit`** — confirm the only writes are to shared-dir state, not direct bot-home writes. If they do reach into bot homes, add to the C5 group.
3. **`action.proposal.apply` per-applier dispatch** — appliers vary in their reach. Recommend enumerating each applier's privileged-op needs separately during E.3 (this audit didn't go applier-by-applier).
4. **`pod_state.bots` via `network_status` opens a urllib request to `http://localhost:<port>/evolve/status`** for every bot. That's a loopback HTTP probe of each bot's gateway. Works for any local user. No work — listed for completeness.
5. **Sudoers grants for evo** — the spec presumes evo gets *no* sudoers grants. If during E.3 some narrow grant turns out cheaper than a round-trip endpoint (e.g. `sudo /bin/launchctl bootout system/ai.openclaw.evo*` so evo can restart its own gateway), that's a decision point worth surfacing — but the default per the spec is no grants. This audit assumes the default.

---

## Recommendation for E.3 ordering

1. Add unix-socket peer-cred auth to the admin daemon's Flask app (spec § 3, option 1). Listening on `/Users/Shared/evolve/admin-daemon.sock`.
2. Add the three new GET endpoints (C1: openclaw-config, C2: backup-status, C3 already exists). Re-plumb the load-bearing reads first: `pod_state.bots`, `config.bot`, `pod_state.backup_status`.
3. Verify the gateway/launchd lifecycle endpoints exist; add the few that don't (C5 group). Re-plumb `action.bot.restart`, `action.proposal.apply`.
4. Add the breakers HTTP endpoints (C4). Re-plumb `action.{bot,pod}.{trip,reset}_breaker`.
5. Add the infra-daemon and per-bot backup-kickstart endpoints. Re-plumb `action.bot.backup_workspace`, `action.infra.daemon_restart`.
6. Wrap the already-HTTP tools (action.signal/proposal/security/plugin, pod_state.audit, pod_state.config_drift) with the new auth header.

After step 6, every privileged capability evo uses goes through the admin daemon's authenticated API. Phase E.4 then removes the primary-bot deny carve-out from `_infer_exec_policy` and evo lands at `exec=full` like every other bot.

---

## Sign-off scope

This audit covers only `packages/admin/evolve_admin/evo/tools/` (the MCP-server subprocess that runs in evo's gateway). Code outside this subtree is admin daemon code and stays unaffected by the account split. Nothing here should be acted on as a fix in this PR — the deliverable is this report. E.2 (provision the `evo` account) and E.3 (re-plumb + auth) are separate sessions whose scope this report defines.
