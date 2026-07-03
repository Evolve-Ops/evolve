# OpenClaw install health — fleet audit + mini repair (2026-06-30)

**Author:** META:deploy chip (routed from META:apps).
**Trigger:** META:apps is building an execution-integrity harness as an OpenClaw
plugin *agent tool-result middleware* (`registerAgentToolResultMiddleware`),
which OpenClaw ships in **v2026.6.6** (PR #90004). The harness only runs if every
pod runs OC **≥ v2026.6.6**. Confirming this turned up a broken/in-flux OpenClaw
install on the mini.

---

## TL;DR — the apps-harness gate

**STATUS: RESOLVED 2026-06-30.** Both pods now run OC **2026.6.10**; the mini was
repaired live (canary-verified) and the rogue updater disabled. The fleet is
**uniformly ≥ v2026.6.6** — the apps harness gate **passes**. See *Resolution*
at the bottom for what was executed.

| Pod | Platform | OC version (on-disk) | OC version (running) | ≥ v2026.6.6? |
|-----|----------|----------------------|----------------------|--------------|
| **VPS** (`evolve-vps-pod`, 64.23.177.242) | Linux/Ubuntu | **2026.6.10** ✅ resolvable | 2026.6.10 | **YES ✅** |
| **mini** (`<pod-admin-user>@mini`) | macOS/Darwin | **2026.6.10** ✅ (after repair) | **2026.6.10** ✅ (after repair) | **YES ✅** |

> *State at discovery (before repair):* mini on-disk was **BROKEN** (non-resolvable)
> and its gateways were down / crash-looping; only one stale in-memory 2026.6.1
> help-process survived. The repair below restored the fleet.

> Names in this memo use role placeholders per `docs/PLACEHOLDER_NAMING.md`
> (`<pod-admin-user>` = the mini admin account; `team-bot-a/-b/-c` = the three
> gateways the operator updater manages). Map back to the live deployment when
> running the runbook.

**Gate verdict for META:apps (post-repair):** the fleet is now **uniformly
≥ v2026.6.6** (both pods on 2026.6.10) — the harness may deploy fleet-wide. (At
discovery the mini failed the gate; the *Resolution* section records the live
repair that closed it.)

---

## Part 1 — per-pod version evidence

### VPS (Linux) — healthy, 2026.6.10
```
$ ssh root@64.23.177.242
/usr/bin/openclaw -> ../lib/node_modules/openclaw/openclaw.mjs   # resolves
$ openclaw --version
OpenClaw 2026.6.10 (aa69b12)
/usr/lib/node_modules/openclaw/package.json: "version": "2026.6.10"
```
Clean install (root:root, Jun 24 19:15). No stale `.openclaw-*` staging dirs.
**Passes the gate.**

### mini (macOS) — broken on disk, 2026.6.1 in memory
- `/opt/homebrew/bin/openclaw` → `../lib/node_modules/openclaw/openclaw.mjs`
  — **dangling**, target does not exist.
- `/opt/homebrew/lib/node_modules/openclaw/` (the live install) contains only
  `dist/`, `pnpm-workspace.yaml`, `skills/` — **no `package.json`, no
  `openclaw.mjs`**. Owned `<pod-admin-user>:admin`, re-created **Jun 30 09:13**. This is a
  partial husk, not a working install.
- `which openclaw` → **not found** (the daemon also runs with a stripped PATH).
- A **complete openclaw 2026.6.10** install sits orphaned at
  `/opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q/` (root:admin, Jun 25 00:11):
  `package.json` (`"version": "2026.6.10"`), `openclaw.mjs`, `dist/` (5507
  entries), `node_modules/`, `npm-shrinkwrap.json` — a full, valid tree.
- **Running gateways** (team-bot-a, team-bot-b, team-bot-c) are alive but loaded the **2026.6.1**
  code into memory before the breakage (per `openclaw-updater-state.json`:
  `last_applied_version: 2026.6.1`, applied 2026-06-03). They keep working only
  because Node holds the code in memory; the on-disk install they'd reload from
  is gone.

---

## Part 2 — mini root cause

The breakage is **not** Evolve's deploy machinery. It is an **operator-authored
autonomous updater** running *outside* Evolve:

`/Users/<pod-admin-user>/bin/openclaw-updater.py` — a `<pod-admin-user>` LaunchAgent ("Autonomous
OpenClaw version manager … checks for updates hourly. Applies security fixes
immediately."). It manages the team-bot-a, team-bot-b, team-bot-c gateways and logs to
`/Users/Shared/openclaw-updater.log`.

Its `upgrade()` does:
```python
run(["npm", "install", "-g", "openclaw@latest"])   # NOTE: use_sudo defaults False
```

**Failure mechanism (half-finished npm atomic swap):** npm installs by extracting
the new tree into a `.openclaw-XXX` sibling, then renaming the live `openclaw`
dir out of the way and renaming the staging dir into place. Run **non-sudo
against a root-owned global prefix** (`/opt/homebrew/lib/node_modules`), npm
cannot complete the rename of root-owned dirs — the swap dies mid-flight,
leaving:
- the freshly-extracted **2026.6.10** stranded in `.openclaw-2N5mgx4q` (root:admin), and
- a dangling `bin/openclaw` symlink + a partial `<pod-admin-user>:admin` husk where the
  live install used to be.

The updater never verifies the install is resolvable afterward and never cleans
up the failed staging dir, so it has logged **"Cannot read installed version …
package.json"** hourly ever since and applied nothing.

This is a **chronic** failure on the mini: the same `.openclaw-2N5mgx4q` hash
appears in a real ENOTEMPTY stderr captured **2026-05-22** (now the fixture in
`packages/admin/tests/test_oc_upgrade_stale_temp_dir.py`).

**Systemic finding:** the rogue updater *competes* with Evolve's own OC update
machinery (`ocadmin` / `safe_upgrade` / `update_watcher`), which already has
stale-temp-dir handling, canary gating, and version monitoring. Two updaters
fighting over one global install is the underlying hazard. **Operator decision
required** (see below).

---

## ⚠️ Critical caveat — do NOT run the existing stale-temp cleanup as-was

Evolve's `ocadmin._check_and_clean_stale_npm_temp_dirs()` (pre-this-PR) treats
**every** `.openclaw-*` dir as disposable residue and offers to `sudo rm -rf` it.
On the mini that is **inverted**: the live `openclaw` dir is the broken husk and
`.openclaw-2N5mgx4q` is the **only complete install**. Blindly deleting it would
**destroy the only resolvable OpenClaw on the box.**

This PR hardens that path (see below) so it detects the inverted swap and
*promotes* the staging dir instead of deleting it.

---

## What this PR changes (code)

`packages/admin/evolve_admin/ocadmin.py` — two predicates with deliberately
asymmetric strictness:
- `_openclaw_install_has_manifest(dir)` — `package.json` parses with a version.
  Gates the **destructive** direction: recovery only ever deletes the live
  `openclaw` dir when it has **no valid manifest** (an unambiguous husk), so an
  upstream entrypoint-layout change can never make us destroy a real install.
- `_openclaw_install_is_healthy(dir)` — manifest **and both** runtime entrypoints
  (`openclaw.mjs` + `dist/index.js`). Gates the **promote** direction: a
  `.openclaw-XXX` sibling must be this complete to be promoted (rejects a
  truncated extraction).
- `_check_and_clean_stale_npm_temp_dirs()` — if the live install has no manifest
  **and** a *real-directory* (not file/symlink) `.openclaw-XXX` sibling is
  complete, it is the **recovery source** → `_recover_from_staged_install`. After
  a successful promote it falls through to clear any remaining incomplete
  siblings (which would otherwise ENOTEMPTY the next `npm install`).
- `_recover_from_staged_install(live, complete)` — confirm-gated promote: warm up
  sudo with a visible `sudo -v` (the captured `rm`/`mv` would otherwise hide a
  password prompt and hang), `sudo /bin/rm -rf` the husk, `sudo /bin/mv` the
  staging dir into place, verify resolvable. Refuses when >1 complete staging dir
  exists. Reached only when the live install is already a husk, so it can only
  improve a non-working state. No new sudoers grant (ocadmin is the operator CLI;
  uses interactive sudo, same `/bin/rm`/`/bin/mv` as the delete path).
- Tests in `test_oc_upgrade_stale_temp_dir.py`: health classifier (5 cases) +
  cleanup decision matrix (promote when live broken; delete-residue when live
  healthy; refuse on ambiguous multi-staging).

This is **distinct from #3358** (single-source CLI *resolver*). #3358 fixes which
binary the resolvers point at; even after it merges, the mini stays broken because
the **install itself** is broken. This PR fixes the install-*state* recovery.

---

## Repair runbook (mini) — operator-gated, privileged, fleet-wide

The on-disk repair activates 2026.6.10 globally and needs gateway restarts to
load it — inherently fleet-wide (a shared global node_modules cannot be canaried
per-bot), so it is **operator-gated** per deploy DoD.

1. **Repair the install** (promote the staged 2026.6.10 — what this PR automates):
   ```
   sudo /bin/rm -rf /opt/homebrew/lib/node_modules/openclaw            # broken husk
   sudo /bin/mv /opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q \
                /opt/homebrew/lib/node_modules/openclaw                # promote
   /opt/homebrew/bin/openclaw --version    # expect: OpenClaw 2026.6.10
   ```
   (Once this PR is deployed, the same is reachable via the ocadmin upgrade
   preflight, which will detect the inverted swap and offer the promote.)
2. **Restart gateways** to load 2026.6.10 into the running bots (fleet-wide —
   operator gate). Canary one bot first; verify health; then the rest.
3. **Address the rogue updater** — operator decision (its script, not in this
   repo):
   - **Recommended:** disable `openclaw-updater.py`
     (`launchctl bootout gui/$(id -u <pod-admin-user>)/<its-label>` + remove the
     LaunchAgent plist) and let Evolve's `ocadmin`/`safe_upgrade` own OC updates
     (canary-gated, stale-temp-aware, monitored). One updater, not two.
   - **Or** fix it in place: run npm with `sudo`, force `--prefix=/opt/homebrew`,
     verify resolvability post-install, and clean up failed `.openclaw-*` staging.

---

## Resolution — executed 2026-06-30 (operator-approved, canary-verified)

Operator approved (1) running the live repair now, canary-first, and (2) disabling
the rogue updater so Evolve's `ocadmin`/`safe_upgrade` is the single OC updater.

1. **Disabled the rogue updater (both instances).** There were *two* loaded copies
   of `openclaw-updater.py` — a user LaunchAgent (`ai.openclaw.updater`, gui domain)
   and a system LaunchDaemon (same label, `UserName=<pod-admin-user>`); a PID-lock
   let only one work at a time. Both were `bootout`'d and their plists renamed to
   `.DISABLED` (the box's existing convention — reversible). No updater process
   remains; neither reloads at boot/login.
2. **Promoted the staged install.** `sudo /bin/rm -rf …/openclaw` (the broken
   husk) → `sudo /bin/mv …/.openclaw-2N5mgx4q …/openclaw`. `openclaw --version`
   now reports **`OpenClaw 2026.6.10 (aa69b12)`** (matches the VPS); ownership
   restored to `root:admin`; no `.openclaw-*` staging dirs remain.
3. **Fleet self-restarted onto 2026.6.10 (canary observation).** Before the
   promote, *no* `dist/index.js` gateway processes were running — the launchd
   `*-gateway` KeepAlive services had been crash-looping on the broken install.
   The instant the install was repaired, launchd's next relaunch succeeded: **all
   9 gateways came up at 16:39:57** and were verified **healthy**
   (`{"ok":true,"status":"live"}` on every port 18789/18790/18800/19000–19050),
   stable with no flapping. (The repair *was* the canary: launchd restarted the
   fleet atomically the moment the entrypoint resolved, and every gateway came up
   green — no separate phased restart was needed.)

**Net:** mini OC = 2026.6.10, all gateways healthy; rogue updater off; fleet now
uniformly ≥ v2026.6.6.

### Residual / follow-ups
- **META:apps** — gate **CLEARED**: VPS 2026.6.10 ✅ and mini 2026.6.10 ✅. The
  execution-integrity harness may deploy fleet-wide.
- **Stale processes (cosmetic):** a hung `sudo -H -u <team-bot-a> openclaw --help`
  from 2026-06-22 (+ its child `openclaw` and `zsh` wrapper) still lingers; inert,
  not a gateway. Left in place (minimal-touch); the operator can `kill` it.
- **Durable updater story:** with the rogue updater off, OC updates should flow
  through Evolve's canary-gated `ocadmin`/`safe_upgrade`. This PR's inverted-swap
  recovery makes that path self-heal the exact failure that broke the mini.
- **Monitoring gap (deposited to META:deploy backlog):** Evolve had no Signal for
  "global OC install non-resolvable while gateways crash-loop." The existing
  `install_integrity_monitor` covers per-bot `.openclaw/` *config* integrity
  (ownership, `openclaw config validate`, agent dry-run) — not the global npm
  install being runnable. `update_watcher` only reads the version for *update*
  notifications (it would read `None` here, not fire an outage Signal). Consider a
  `host_health` check that fires when the `bin/openclaw` symlink resolves but its
  target (`openclaw.mjs` / `dist/index.js`) is missing.
