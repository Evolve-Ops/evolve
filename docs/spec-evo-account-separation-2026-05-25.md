# Evo / admin-daemon account separation — design spec

**Status:** Draft. Pre-implementation. No code changes in scope for this spec yet — it captures the architectural target that supersedes the primary-bot exec carve-out.

**Date:** 2026-05-25.

**Origin:** Out of the conversation that produced the Phase A landing of [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md). The primary-bot carve-out in `_infer_exec_policy` (evo gets `tools.exec.security="deny"` while every other member bot gets `"full"`) is an *inconsistency relative to the spec's principle*, not a feature. The carve-out exists because evo currently shares its macOS user account with the privileged admin daemon, which makes any exec leak on evo a path to the admin daemon's reach. Splitting the user accounts removes the inconsistency.

**Adjacent:**

- [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) — establishes the OS-user-is-the-policy-boundary principle. This spec extends that principle to evo itself.
- The original `/approve` slash-command leak diagnosis (internal-only) — documents the channel that motivated the deny-on-evo carve-out. This spec eliminates the leak by making evo non-privileged at the OS layer.
- [project_evo_oc_native_architecture](../memory/project_evo_oc_native_architecture.md) — established that evo is an OC bot supercharged by Evolve scaffolding; the admin-UI chat, Telegram, and cross-bot "evo" keyword all route through evo's gateway.
- [project_evolve_bot_role](../memory/project_evolve_bot_role.md) — bot-name-vs-account-name distinction is first-class.

---

## Problem

The `evolve` macOS user runs two distinct processes:

1. **The admin daemon** (`ai.evolve.evolve.admin-ui` LaunchDaemon) — Flask server, repo puller, reconciler, signal store writer, etc. Needs sudoers grants and ACL access across every bot to do its job (read/write bot configs, kickstart gateways, install plugins, manage LaunchDaemons, etc.).

2. **Evo's OC gateway** (the bot named "evo," the primary conversational interface) — runs evo's LLM, handles channels, evaluates tool calls.

Because they share a user account, they share OS-level permissions. Evo's gateway inherits the admin daemon's full reach. This creates two problems:

**Problem 1 — the `/approve` exfiltration channel.** The admin-UI chat box is a passthrough surface (text the operator types is fed verbatim into evo's gateway). If evo has any exec capability, OC exposes `/approve <id>` as a slash command. A prompt-injected evo could queue a malicious exec call; the operator could see `/approve abc123` in chat and approve it thinking it's routine; the malicious exec then runs as the admin-daemon-privileged `evolve` user.

**Problem 2 — principle inconsistency.** The [app-derived-permissions spec](spec-app-derived-permissions-2026-05-24.md) establishes that the OS user account is the policy boundary, and the bot gets `tools.exec.security="full"` by default because its OS user account's natural reach is the right scope. That principle holds for team-bot-a/admin-bot/team-bot-c/etc. — their OS user accounts have no sudo, no cross-bot ACLs, and roughly the reach of a regular human user. But it doesn't hold for evo *only* because evo runs on the privileged admin-daemon account. The carve-out (`if bot_id == "evolve" or role == "primary": return "deny"`) is a workaround, not a coherent application of the principle.

The current Phase A deployment is correct given the constraint, but the constraint itself is removable.

---

## Principle

**The admin daemon is the privileged actor. Bots — including evo — are non-privileged actors that reach privileged capability only through a typed API the admin daemon exposes.**

Two corollaries:

- Every bot runs on its own non-privileged macOS user account. No bot user has sudoers grants. The bot's reach is its OS account's natural scope (own home, world-readable system paths, `/Users/Shared/evolve/`).
- All privileged capability — reading other bots' state, restarting gateways, applying config patches, installing plugins, etc. — is exposed by the admin daemon as a typed HTTP API. Bots invoke this API via registered MCP/plugin tools.

This makes the admin daemon's API surface evo's *de facto* capability allowlist. A prompt-injected evo can only do what the admin daemon's tools let it do. Adding capability = adding a tool = reviewable code change. The bot can't reach privileged operations by writing arbitrary shell.

The same principle obviously applies to member bots already — team-bot-a doesn't read admin-bot's openclaw.json by directly opening the file, it asks the admin daemon. Extending it to evo finishes the principle.

---

## Architecture

### 1. Two macOS user accounts

| macOS user | What runs there | Privilege |
|---|---|---|
| `evolve` | The admin daemon (Flask server, repo puller, reconciler, signal store writer, etc.) | All sudoers grants in `/etc/sudoers.d/evolve`. ACL read on every bot's `.openclaw/`. ACL read+write on `.openclaw/workspace/evolve/` per bot. Owns `/Users/Shared/evolve/`. |
| `evo` (new) | Evo's OC gateway and tool runtime | Default 700 home. No sudoers grants. No special ACLs. Same OS reach as team-bot-a/admin-bot. |

The naming follows the existing bot-name-vs-account convention: the bot named "evo" runs on the macOS user named "evo." (Same shape as the team-bot-b bot on the personal-bot-user account, just with the bot_id and account agreeing in this case.)

### 2. Communication: evo → admin daemon via registered tools

Evo's privileged capabilities — *all of them* — route through the admin daemon's HTTP API. This is the same path member bots already use for cross-bot data (admin-bot's signals page already talks to `/api/signals/...`; team-bot-a's prompts already invoke the registered tool surface).

For evo specifically, the API surface needs to cover at minimum:

- Read any bot's openclaw.json, exec-approvals.json, cron jobs (existing endpoints)
- Read any bot's manifests + scan status (existing endpoints)
- Read signals across the pod (existing — pod-wide `/Users/Shared/evolve/signals/` is readable to any bot anyway)
- Read any bot's gateway logs (existing endpoint with sudo grant)
- File / read / mutate proposals (existing endpoints)
- Restart a bot's gateway (existing endpoint)
- Trigger an app scan on a specific bot (existing endpoint)
- Read the codebase (`/Users/Shared/evolve-repo/` is world-readable post-pull; evo reads directly)

Most of this **already exists** — it's what the admin UI uses. The admin UI is one client of these endpoints; evo becomes another. The work is registering the corresponding MCP/plugin tools on evo's side and removing whatever direct-fs paths evo currently relies on.

### 3. Auth between evo's gateway and the admin daemon

Today, the trust model between evo's gateway and the admin daemon is implicit — "same user account, loopback." After separation, the admin daemon needs to authenticate evo's tool calls explicitly. Options, in order of preference:

1. **Unix-socket peer-credential check.** The admin daemon listens on a unix socket; the kernel reports the calling process's uid. The daemon trusts callers whose uid matches the `evo` macOS user. Simple, no token leak surface, no token rotation problem.
2. **Token-based auth over HTTP loopback.** A shared secret in a file readable only by `evo` (or via Keychain). Admin daemon validates `Authorization: Bearer <token>`. Already the pattern OC uses for its own gateway auth.
3. **mTLS over HTTPS loopback.** Overkill on a single-machine pod; mentioned only for completeness.

Option 1 is the cleanest and matches the unix-y principle elsewhere in the codebase. We'd add a second unix-socket binding to the admin daemon's Flask app alongside the existing HTTP binding.

### 4. Surface: what evo CAN'T do after separation

The point of the separation is that some things move from "evo could do directly" to "evo can only ask the admin daemon to do." Examples:

- **Direct file reads outside evo's home and `/Users/Shared/evolve/`.** Routed through admin daemon endpoints.
- **Shelling out as the privileged user.** Evo's `exec=full` runs as the `evo` user, which has the same blast radius as team-bot-a doing the same thing — bounded by its own user account.
- **Direct launchctl manipulation.** Routed through the existing `/api/admin/gateway/<bot>/restart` endpoint.
- **Direct writes to bot config files.** Routed through the proposal + applier system (which is what we want anyway — see the existing arbiter spec).

What evo CAN still do:

- **Read its own workspace.** Unchanged.
- **Read `/Users/Shared/evolve/`.** Unchanged — pod-wide shared dir, world-readable.
- **Run scripts in its own workspace.** With `exec=full` (the new default after this spec), evo runs scripts as `evo` user. Same as any other member bot.
- **Hit any host on the internet via web.fetch.** Unchanged.
- **Call any registered tool the admin daemon exposes.** Unchanged.

The net effect: evo's everyday operation is identical from the LLM's perspective (it calls tools); the underlying OS reach is dramatically narrower; the `/approve` leak stops being a privilege-escalation path.

### 5. The primary-bot exec carve-out is reset

After this spec lands, `_infer_exec_policy` no longer needs the primary-bot deny carve-out:

```python
# Current Phase A
if bot_id == "evolve" or (bot_role or "").lower() == "primary":
    return "deny"

# After this spec
# (block removed — evo gets the same "full" default as other member bots)
```

The DEFAULT_BASELINE and ensure_plugin_config flow remain unchanged otherwise — evo is just another member bot with `exec=full + ask=on-miss` and a fresh reconciler-produced `exec-approvals.preview.json`.

The compliance chip's "deny + member bot with installed apps = red" state machine continues to work; evo as a primary bot would never have apps installed, so its state remains green by virtue of "no apps to declare."

---

## Migration plan

Five phases, none of which block each other in unexpected ways. Each is a separable PR.

### Phase E.1: Audit evo's current direct-fs reach

**Goal:** understand which evo tools/skills currently rely on direct file reads (vs. registered tool calls) so we know what to re-plumb.

- Grep evo's tool implementations (`packages/admin/evolve_admin/evo/`) for direct `Path.read_text` / `open()` calls that target paths outside `/Users/evolve/` (the bot's own current home) and outside `/Users/Shared/evolve/` (pod-wide shared).
- For each hit, classify:
  - **A: paths inside evo's home/workspace** — no change needed, still works on the new account
  - **B: paths inside `/Users/Shared/evolve/`** — no change needed, world-readable
  - **C: paths inside other bots' `.openclaw/`** — needs an admin daemon endpoint (most already exist)
  - **D: paths inside `/Users/Shared/evolve-repo/`** — should already work (deploy checkout is world-readable post-pull) but verify
- Produce a per-tool table of what needs re-plumbing. The migration is bounded by this list.

Estimated effort: a half-day audit. The output drives the scope of subsequent phases.

**Status:** Done 2026-05-26 — report at [docs/audit-evo-account-separation-2026-05-26.md](audit-evo-account-separation-2026-05-26.md). Key takeaways folded into the phases below:

- **Runtime-context split confirmed.** `packages/admin/evolve_admin/evo/` mixes two runtimes — `evo/tools/` runs inside the gateway subprocess (affected by E.2), everything else (`evo/dispatch/`, `evo/handlers/`, `evo/wizard/`, etc.) runs inside the admin daemon (unaffected). E.2/E.3 scope is *only* the `evo/tools/` subtree.
- **Bucket totals across 30 registered MCP tools:** 0 type-A, 14 type-B (survive unchanged), 3 type-C reads + ~10 type-C privileged-ops (re-plumb work), 1 type-D (verified world-readable), 1 type-E (system path, world-readable; sudo write counted under C).
- **About half of evo's tools survive the split with no work** (the 14 shared-dir reads + the already-HTTP set). The other half need either a new admin daemon endpoint or an auth-wrap.
- **Six load-bearing tools to re-plumb first** (audit §"Load-bearing tools"): `pod_state.bots`, `config.bot`, `pod_state.backup_status`, `action.bot.restart`, `action.proposal.apply`, `action.bot.backup_workspace + action.infra.daemon_restart`. After those, the rest can move in parallel.

### Phase E.2 / E.3 ordering (revised 2026-05-26)

**Important sequencing decision surfaced by the audit:** if Phase E.2 (account cutover) lands before Phase E.3 (endpoints + re-plumb), every C-bucket tool breaks during the gap. To avoid that, the phases were originally numbered sequentially but should run **interleaved**:

1. **E.2.a — Provision the `evo` account** (account exists, but no gateway runs there yet).
2. **E.3 — Add endpoints, write auth layer, re-plumb evo's tools through HTTP** (still running as the `evolve` user; nothing functionally changes for the operator because the loopback HTTP calls return the same data the in-process calls did).
3. **E.2.b — Cutover** (flip the gateway plist's `UserName` from `evolve` to `evo`). Low-risk now — every privileged path already routes through the admin daemon.
4. **E.4 — Remove the carve-out** (evo lands at `exec=full`).
5. **E.5 (optional) — Decommission `evolve` as a bot account.**

Subsequent sections describe each step in detail. E.2.a + E.2.b combined are the "Phase E.2" the original spec described; this revision splits them around E.3 to eliminate the breakage window.

### Phase E.2.a: Provision the `evo` macOS account

**Goal:** create the new account so it exists and the deploy code can target it, but don't start running evo there yet.

- Setup wizard learns to create the `evo` user (same `dscl` pattern as team-bot-a/admin-bot/team-bot-c creation).
- Build the per-bot tree under `/Users/evo/.openclaw/` empty (no openclaw.json yet; nothing runs as this user).
- `set_evolve_read_acl(bot_id="evo")` to grant the admin daemon read access to whatever lands there in E.2.b.
- No plist changes yet; evo still runs as `evolve`. This step is purely "the account exists when E.2.b needs it."

After this phase: a new macOS user `evo` exists; nothing runs there; existing operation unchanged.

### Phase E.3: Wire admin-daemon auth + re-plumb the audit-identified tools

**Goal:** make admin-daemon API calls from evo authenticate explicitly and re-plumb every direct-fs path identified in Phase E.1. Done while evo is still running as the `evolve` user, so no operator-visible disruption — only internal plumbing changes.

Following the audit's recommended order (audit §"Recommendation for E.3 ordering"):

1. **Unix-socket peer-cred auth** added to the admin daemon's Flask app (spec §3, option 1). Listening on `/Users/Shared/evolve/admin-daemon.sock`. Auth is "uid match" — the admin daemon trusts callers whose peer-cred uid matches the configured evo uid.

2. **New GET endpoints for type-C reads** (audit §"Type-C findings — candidate admin daemon endpoints"):
   - `GET /api/admin/bot/<bot_id>/openclaw-config` — replace `config.bot`'s direct fs read.
   - `GET /api/admin/bot/<bot_id>/backup-status` — replace `pod_state.backup_status`'s in-workspace `git` shellouts.
   - `GET /api/status` — already exists; re-point `pod_state.bots` away from its in-process `tile_metrics` calls.

3. **Gateway/launchd lifecycle endpoints** (audit C5): verify existence of `POST /api/admin/gateway/<bot>/restart`, `POST /api/admin/bot/<bot>/redeploy`, `POST /api/admin/bot/<bot>/remove`, `POST /api/admin/pause-all`, `POST /api/admin/resume-all`. Add any missing. Re-plumb the corresponding `action.*` tools.

4. **Breakers HTTP endpoints** (audit C4): add `POST /api/admin/breakers/trip` and `POST /api/admin/breakers/reset`. Re-plumb `action.{bot,pod}.{trip,reset}_breaker`.

5. **Infra-daemon + per-bot backup-kickstart endpoints**: add `POST /api/admin/infra/<daemon_id>/restart` and `POST /api/admin/bot/<bot>/backup-kickstart`. Re-plumb `action.infra.daemon_restart`, `action.bot.backup_workspace`.

6. **Auth-wrap the already-HTTP tools** (`action.signal/proposal/security/plugin`, `pod_state.audit`, `pod_state.config_drift`) so they use the new auth header.

After step 6, every privileged capability evo uses goes through the admin daemon's authenticated API. The MCP tool subprocess still runs as `evolve` for now — that's E.2.b's job.

### Phase E.2.b: Cutover — flip evo's gateway to the `evo` user

**Goal:** swap evo's runtime user account now that everything routes through the admin daemon.

- Update evo's gateway plist: `UserName` from `evolve` to `evo`; working dir + env vars accordingly.
- Move evo's workspace state from `/Users/evolve/.openclaw/` to `/Users/evo/.openclaw/` (only the per-bot state that evo's gateway and its OC plugin need at runtime; admin-daemon state stays under the `evolve` user).
- Restart evo's gateway.
- Operator-visible disruption: minutes-scale, gateway restart only.

After this phase: evo runs as the unprivileged `evo` user. The privileged work continues to be done by the admin daemon as the `evolve` user, reached only through the authenticated HTTP API.

### Phase E.4: Reset the primary-bot exec carve-out ✅ shipped 2026-05-25

**Goal:** complete the principle by removing the deny-on-evo branch.

What landed:
- Removed the `bot_id == "evolve" or role == "primary" → "deny"` branch from `_infer_exec_policy` (deploy.py) AND its mirror in `permissions/reconciler.py::_resolve_target_mode`. Evo's default exec is now `"full"`, same as every other member bot.
- Removed the parallel guards in `analyzer/generators/app_permission_drift/signal_proposals.py` so allowlist-mutation proposals now emit against primary bots like any other.
- Compliance chip (`upstream_version.evaluate_exec_policy_compliance`) used to return GREEN for primary+deny ("intended posture"). Post-E.4 returns UNKNOWN — there's no intended deny posture; operator-pinned deny is surfaced as an override that may or may not be intentional.
- Updated tests pin the new behavior (carve-out removal regression check).
- Updated `analyzer/permissions/baseline.py` docstring — pod-default `"full"` now applies uniformly.

After this phase: evo is a fully consistent application of the principle. The carve-out disappears from the code; the spec section §"Resolved 2" of the app-derived spec stops needing the "primary bot exception" branch.

### Phase E.5: Decommission `evolve` as a bot account (optional)

**Goal:** make `evolve` strictly an admin-daemon service account, no bot lives there. Cleanup, not load-bearing.

- Remove any lingering bot-state files from `/Users/evolve/.openclaw/`.
- Document that `evolve` is the admin daemon's user, not a bot's user. Wizard/CLI surfaces should reject "create a bot called evolve" if the operator tries.

### Phase E.6: Primary `bot_id` is `"evo"`, not `"evolve"` (PARTIALLY SHIPPED → completed 2026-06-23)

**Reclassified 2026-06-23 from "deferred — cosmetic" to a real bug.** The original
framing below ("purely ergonomic", "cosmetic on a solid foundation") was wrong:
the rename was *half-shipped*, not deferred, and the split-brain it left caused a
**production gateway crash-loop and monitor blindness** on the live evo-primary
Linux pod. Two gateway units — the stale `ai.openclaw.evolve-gateway` and the real
`ai.openclaw.evo-gateway` — fought over port 19030 (mutual fratricide, thousands of
restart attempts), and the label-keyed health monitor watched the wrong label so no
UI alert fired. Completion was **required**, not optional.

**What was actually true (not the stale claim "the internal bot_id is still evolve"):**
the setup wizard's fresh-install path flipped the dedicated-primary default to `"evo"`
*early* (`setup_wizard.py` `_provision_evo_oc` defaults `bot_id="evo"`), so **fresh
installs already produce `network.primary = "evo"` with no `evolve` bot** — only the
`evolve` service *user* (which runs the admin daemon and owns no bot). The design is:
**primary bot_id = `"evo"`; there is NO `evolve` bot.** What was NOT done was the
~25 code sites that still assumed the primary is named `evolve`, plus reaping of stale
`evolve`-named units — which is what produced the live incident.

**Intended design (the invariant going forward):**
- The primary bot's id is whatever `network.primary` says (`"evo"` on every fresh
  pod; legacy macOS pods may still have a bot *literally* named `"evolve"`, which is
  valid and honored — `bot_id == macOS account` is not required).
- `evolve` is a **service user**, never a bot. No code may assume the primary bot is
  named `"evolve"`; the primary is resolved through
  `packages/analyzer/primary_bot.py::primary_bot_id(network)` (honors
  `network.primary` → `role: "primary"` → legacy `"evolve"` fallback).

**What completed this phase (2026-06-23, this PR):**
- **Purged silent `primary_bot_id(...) or "evolve"` substitution** at the
  resolve-or-misconfig sites — they now read the resolver and treat `None` as a real
  misconfig instead of papering it with `"evolve"`: `deploy.py`
  (`_install_launchd_slack_signals` skips the install rather than create a dead
  `--bot evolve` unit), `health.py` (gateway-probe sweep + signal scope),
  `web/routes_cost_measures.py`, `web/home_chat_routes.py` (assistant self-identity),
  `analyzer/audit.py` (primary-only procedure checks).
- **Fixed `bot_id == "evolve"` primary special-cases** to derive the primary:
  `upstream_version.py::evaluate_exec_policy_compliance` now takes the resolved
  `primary_bot_id` (threaded from `safety_summary.py`); `web/routes_analytics.py`
  `_bot_exists` resolves the primary.
- **Fixed the member-exclusion filters** (`[m for m in members if m != "evolve"]`)
  in the five `evo/handlers/*` pod-report handlers — they hardcoded `"evolve"` and so
  excluded *nothing* on an evo-primary pod, leaking `"evo"` into member-only lists.
  Now routed through a shared `pod_member_bots(network)` helper that excludes the
  *resolved* primary.
- **Added a stale-gateway reaper** (`deploy.find_orphaned_gateway_labels` /
  `reap_orphaned_gateways`, wired into the `install upgrade` flow): the general orphan
  sweep (`find_orphaned_plists`) only matches `ai.openclaw.evolve.*` (infra) and
  `ai.evolve.*` (per-bot apps), **not** the gateway shape `ai.openclaw.<bot>-gateway`,
  and was macOS-launchd-only with no Linux systemd equivalent — which is exactly why
  the stale `evolve-gateway` survived a fresh install. The reaper is platform-aware
  (scans `/Library/LaunchDaemons` on macOS, `/etc/systemd/system` on Linux), reaps via
  the scheduler seam, and is conservative (no primary resolved ⇒ reap nothing).
- The three gateway-**label** literals (`expected_plist_labels` keep-set, post-deploy
  restart, gateway health check) are de-hardcoded under the separate **[META:platform]**
  primary-gateway-label work (`primary_bot.primary_bot_gateway_label`); not duplicated here.

**Deliberately KEPT (audited, not bugs — the "distinguish carefully" cases):**
- `analyzer/apply.py` (`current_user == "evolve"`) and `tile_metrics.py`
  (`user == "evolve"`) check the **service USER / OS account**, not the bot id — legit.
- `evo/handlers/_shared.py::is_pod_wide_caller` keeps its no-network `"evolve"`
  legacy fallback (it already resolves via `primary_bot_id` when `network` is present;
  the fallback is tested and mirrors the resolver's own legacy fallback).
- `lifecycle/inventory.py` and `retire.py` key off `plugins.entries.evolve` — the
  OpenClaw **plugin product name** is the constant `"evolve"`, written on *every*
  bot's openclaw.json (`deploy.py` `ensure_plugin_config`), so this is correct. (The
  original scope line "rename `plugins.entries.evolve` key" was mistaken — only the
  `.config.botId` *value* is bot-specific, and that is already written from the bot id.)
- `deploy.py::_resolve_evolve_app_target` keeps `or "evolve"` — it is the
  byte-identity contract for macOS (where the primary's account legitimately *is*
  `evolve`) and is explicitly tested (`test_degenerate_network_falls_back_to_evolve`).

**Migration-CLI decision (was: `evolve-admin rename-bot --from evolve --to evo`):**
**Not built — moot.** All current pods are already fresh-`evo` (the wizard has
defaulted to `evo` since well before this phase), and legacy macOS pods that still run
a bot *literally* named `evolve` are valid and resolve correctly byte-for-byte — they
do not need renaming. The only residue a half-transition can leave is a stale
`evolve`-named **gateway unit**, which the reaper above now removes on the next
`install upgrade`. A bulk record/dir rename of the `network.json` key + 191 stored
JSON records + shared-dir subdirs is therefore unnecessary churn; if a future pod ever
needs it, build it then. The reaper closes the operationally-dangerous gap; the rest
was cosmetic and is intentionally not pursued.

**Status:** completed 2026-06-23 (this PR). The split-brain is resolved: the primary is
resolved through `primary_bot_id` everywhere it matters, stale gateways are reaped, and
the spec no longer claims the bot_id is `"evolve"` or that this is cosmetic.

<details>
<summary>Original (2026-05-26) framing — superseded, kept for history</summary>

> **Goal:** finish the conceptual rename. Today (post-Phase-E.2.b) the bot is called
> "evo" everywhere user-facing but the internal `bot_id` in `network.json` is still
> `"evolve"`. **Motivation:** purely ergonomic. **Status:** deferred 2026-05-26 — the
> rename is cosmetic on a solid foundation.
>
> *(This was incorrect on two counts: the wizard already defaulted fresh installs to
> `evo`, so the bot_id was NOT still `evolve` on fresh pods; and the incompleteness was
> load-bearing, not cosmetic — it crash-looped a production gateway. See above.)*

</details>

---

## What we lose

Honest accounting of costs:

1. **Ad-hoc code execution by evo loses the privileged path.** Today, evo could (in principle) write a Python script and run it as the privileged user. After separation, evo can run scripts as the `evo` user, which has no sudo / no cross-bot ACL / no admin-daemon reach. For diagnostics evo wants to script up on the fly, the answer is either (a) build it as a registered tool ahead of time, or (b) ask the operator to run it manually. This pushes more capability through the typed API, which is the desired direction.

2. **Some current evo internals need rewiring.** Anywhere evo's code today shortcuts to direct file reads (relying on shared-user-account convenience), the migration replaces those reads with admin-daemon tool calls. The Phase E.1 audit is what determines how much work this is. We don't know yet; the audit is the first step.

3. **The auth layer adds complexity.** Loopback HTTP between two processes on the same user was implicitly trusted; with two users, we need explicit auth. Unix-socket peer-cred is cheap to add but it's another thing to maintain. Token rotation, if we go that route, is more.

4. **Setup wizard work + migration downtime.** The wizard learns a new step; the production cutover requires a brief window where evo is unavailable while its account is provisioned and state moved. Minutes-scale, not hours-scale.

5. **Documentation churn.** Memory notes that say "evo runs on the evolve account" need updating; this spec is one of those updates.

## What we gain

1. **The `/approve` exfiltration channel closes.** Even with `exec=full`, evo's worst-case exec runs as the `evo` user, which can't reach the admin daemon's data or sudo to anything. The slash-command leak loses its target.

2. **The principle becomes consistent.** No more carve-out. Every bot's OS account is the policy boundary; the admin daemon is the privileged actor; tools are the cross-bot ABI. The spec-app-derived-permissions principle holds without exception.

3. **Evo can have `exec=full`.** Your original instinct from the design discussion ("evo needs full exec to actually do its job") becomes achievable without the security tradeoff that motivated the current deny carve-out.

4. **Blast radius for evo prompt injection drops dramatically.** Today: a prompt injection on evo that gets through the operator-approval flow could exfiltrate the admin daemon's reach (every API key, every config, every secret the daemon can read). After separation: same prompt injection at worst exfiltrates `/Users/evo/`'s contents + `/Users/Shared/evolve/`'s contents — same surface as a prompt injection on team-bot-a.

5. **The admin daemon API surface becomes an auditable, typed capability allowlist.** No more pattern-matching exec strings (`/usr/bin/python3*`-style) — instead, typed JSON over HTTP / unix socket, per-endpoint authorization, per-operation audit logging. Cleaner than exec-approvals.json for the privileged-side capabilities.

6. **Less concentrated privilege.** The pod's most-privileged identity (the `evolve` user) is reachable only via Flask routes the admin daemon code defines. Anything that needs that privilege has to be written as code, not LLM-driven shell construction.

---

## Open questions

1. ~~**Does the admin daemon's existing HTTP API cover all the capabilities evo needs?**~~ **Answered by E.1 audit (2026-05-26).** Partial. The audit's C5 enumerates which gateway/launchd-lifecycle endpoints almost-certainly exist (because the admin UI's buttons depend on them) and which need to be added. New endpoints needed: `GET /api/admin/bot/<id>/openclaw-config` (C1), `GET /api/admin/bot/<id>/backup-status` (C2), `POST /api/admin/breakers/{trip,reset}` (C4), and verify-or-add `POST /api/admin/infra/<id>/restart` + `POST /api/admin/bot/<id>/backup-kickstart` (C5). Existing endpoints to leverage: `GET /api/status` for tile chips (C3), `POST /api/arbiter/proposals/<id>/apply` for proposal apply.

2. **What's the right format for evo's tool registration against the admin daemon's API?** MCP server hosted by the admin daemon? Plugin entries that proxy to HTTP? Both work; MCP is the more modern path and matches where OC is heading. Decide before Phase E.3.

3. **How does the admin daemon authenticate evo specifically vs. other bots?** Once we have a unix-socket / token auth layer, other bots could plausibly call admin daemon APIs too (today they go through Flask HTTP). Should the API be uniformly available to all bots, or specifically scoped to evo? Default: evo gets a richer surface (cross-bot reads, gateway restart, etc.); other bots stay on their narrower current API.

4. ~~**Does evo need any cross-bot ACL grants on the new `evo` user account?**~~ **Answered by E.1 audit (2026-05-26).** No. The audit found 3 type-C cross-bot direct reads and ~10 type-C privileged ops; all of them are re-plumbable through admin daemon endpoints. No hot-path read is hot enough to justify granting evo cross-bot ACLs. The `evo` user starts with no extra grants — admin daemon mediates all cross-bot access.

5. **Backward compatibility during Phase E.2.b cutover.** Existing evo state (manifests, conversation history, learned preferences, channel auth tokens) lives in `/Users/evolve/.openclaw/workspace/`. The migration has to preserve this exactly. Stress-test the copy on a test pod first.

6. **Should evo get narrow sudoers grants for self-management?** E.1 audit flagged this. The spec presumes evo gets *no* sudoers grants. If during E.3 some narrow grant turns out cheaper than an HTTP round-trip (e.g., `sudo /bin/launchctl bootout system/ai.openclaw.evo*` so evo can restart its own gateway), that's a decision point. Default: no grants — match the team-bot-a/admin-bot posture. Revisit only if a specific case shows up where the HTTP path is meaningfully worse.

7. **`action.app.install` / `action.app.audit` privilege check.** E.1 audit flagged these as TBD — the install path writes shared-dir state (B, likely fine) but didn't trace whether anything inside the tool itself sudo's. Verify during E.3 before routing.

8. **Per-applier dispatch in `action.proposal.apply`.** Different appliers reach different surfaces (L1 vs L2 patcher families have very different privilege profiles per `project_l1_l2_applier_architecture`). E.3 should enumerate each applier's privileged-op needs separately rather than treating them as a single block.

---

## Out of scope

- **Splitting the admin daemon itself across multiple users.** The admin daemon is monolithic by design (single Flask app); decomposing it is a separate effort and not motivated by this spec.
- **Container/sandbox isolation for member bots.** Per-user isolation is what we have; finer-grained sandboxing (chroot, sandbox-exec, etc.) is out of scope here — see [spec-app-derived-permissions-2026-05-24.md §"Out of scope"](spec-app-derived-permissions-2026-05-24.md).
- **Multi-machine deployments.** This spec assumes a single mini hosting all bots + admin daemon. Multi-machine adds network-policy and identity-federation questions that aren't here.
- **Replacing the operator-facing admin UI's auth model.** The admin UI uses its own auth (session cookies / loopback trust). That's separate from the evo↔admin-daemon path this spec adds.

---

## Success criteria

This spec has worked when:

1. **Evo runs on its own non-privileged macOS user account.** `/Users/evo/.openclaw/openclaw.json` exists; evo's gateway plist runs as `evo`; the `evolve` user runs only the admin daemon.
2. **The primary-bot deny carve-out is removed from the codebase.** `_infer_exec_policy` has no `if bot_id == "evolve"` branch. Evo lands at `tools.exec.security="full"` like every other member bot, and the compliance chip shows green.
3. **Every privileged capability evo uses goes through the admin daemon API.** Phase E.1's audit found the direct-fs paths; Phase E.3 re-plumbed them; no direct-read regressions.
4. **A prompt-injected evo cannot exfiltrate admin-daemon-level data.** Verified by red-team test: someone tries to get evo to `cat /Users/admin-bot/.openclaw/auth-profiles.json` and the kernel returns EACCES because evo is not on the privileged account.
5. **The operator's experience of evo is unchanged.** Same chat surface, same capabilities, same tools. The migration is internally substantive but externally invisible.
