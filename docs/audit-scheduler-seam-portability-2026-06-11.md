# Audit: scheduler-seam portability gap (Phase 8.3 Linux)

> **Headline.** **14 silently-broken `LaunchdScheduler(...)` constructions across 12 modules**
> bypass the process-wide `get_scheduler()`/`set_scheduler(SystemdScheduler())` injection and
> invoke `launchctl` directly on a Linux pod. The daemons that hit them every cycle: **repo-puller**
> (post-pull kickstart — `repo_puller.py:807`, the confirmed example), **audit-scheduler / infra-audit**
> (`cron_exit_monitor.py:112`, `infra_audit.py:394`), **RSI metric resolution**
> (`metrics/resolvers/launchd.py:81` — fires a false `launchd_not_loaded` Signal for *every* bot,
> no systemd sibling resolver exists), the **admin web server** (`routes_maintenance.py:48`,
> `routes_trust.py:34`, `tile_metrics.py:1836`), and the **retire / ocadmin / mcp-bridge** operator
> paths (`retire.py:1222`/`:1278`, `ocadmin.py:140`/`:147`, `mcp_service.py:155`/`:162`). Two
> low-severity sites self-empty to `[]` (`scanner.py:539`, `app_audit_runner.py:512`). **The crux
> design verdict: `timeout` is CONSTRUCTOR-ONLY on both adapters — no `Scheduler` Protocol verb
> takes a per-call timeout** — so a naive swap to the `get_scheduler()` singleton silently changes
> the load-bearing 5/10/15 s timeouts to 30 s. **Bite 1 (everything depends on it): add an optional
> per-call `timeout` to the Protocol mutating/probe verbs on both adapters.** Proposed **6 PR-sized
> bites** (incl. the ratchet) below. Ground truth vs the pre-scan: the gap is real but the
> `infra_audit` "0 constructions" note was wrong (there IS one at `:394`), and `recovery`+`service`
> are correctly GRACEFUL (not broken).

**Status:** Complete — read-only audit, no code changes; worklist scoped for the META coordinator.
**Scope:** non-test, non-`runtime/` call sites that construct `LaunchdScheduler(...)`
directly (bypassing the process-wide `get_scheduler()` injection point) or call
`get_scheduler()` / `get_launchd_scheduler()`.
**Anchor:** `get_scheduler()` (`packages/analyzer/runtime/scheduler.py:1230`) defaults to
`LaunchdScheduler` and is NOT profile-dispatching; Linux selection works only because the
platform gate injects `set_scheduler(SystemdScheduler())` process-wide at startup. Any module
that constructs `LaunchdScheduler(...)` in a module-global bypasses that injection and invokes
`launchctl` on Linux.

## The timeout-Protocol verdict (the crux)

**Verdict: timeout is CONSTRUCTOR-ONLY. No `Scheduler` Protocol verb accepts a per-call
`timeout`.** Confirmed at `scheduler.py`:

- `Scheduler` Protocol verbs (`scheduler.py:220-226`): `install(spec)`, `remove(label)`,
  `restart(label)`, `list(*, prefix=None)`, `status(label)`, `running(label)`, `kill(label)` —
  none take `timeout`.
- `LaunchdScheduler.__init__` (`scheduler.py:261-271`): `timeout: float = 30.0` is a
  **constructor** kwarg; the runner closes over it (`scheduler.py:281`).
- `SystemdScheduler.__init__` (`scheduler.py:947-961`): identical shape — `timeout: float = 30.0`
  constructor kwarg, runner closes over it (`scheduler.py:961`).
- `get_scheduler()` (`scheduler.py:1230-1235`) returns the **process-wide singleton** built with
  **default** construction (`LaunchdScheduler()` — 30s timeout, sudo-by-default).

Therefore the broken sites that pass `timeout=15.0` / `timeout=5.0` / `use_sudo=False` /
`sudo_non_interactive=True` carry **per-call-site config the `get_scheduler()` singleton does not
carry**. A naive swap to `get_scheduler()` would silently change timeout (15s→30s), sudo posture,
and non-interactive behavior — all load-bearing (e.g. repo_puller restarts ~7 daemons
sequentially and must stay bounded: `repo_puller.py:787-789`).

**Minimal-change recommendation: add an OPTIONAL per-call `timeout` to the Protocol mutating
verbs that the broken sites actually call** (`restart`, `remove`, `install`, plus `status`/`list`
for probes), defaulting to the constructor value when omitted, implemented on BOTH adapters. This
is strictly smaller than option (b) (a config-overridden `get_scheduler()` view) and keeps the
singleton model intact. Sites that also need `use_sudo=False` / `sudo_non_interactive=True` are a
SEPARATE concern (sudo posture), addressed below per-site — most of those are `raw()`-based probes
that are launchd-verbatim regardless and must migrate to Protocol verbs first (S2 debt).

This timeout-Protocol change is **bite 1 — everything else depends on it.**

## Per-site classification

Legend — class ∈ {PORTABLE, GRACEFUL, FAIL-FAST, SILENTLY-BROKEN}. "Runs on Linux?" =
does the process/daemon that reaches this construction execute on a Linux pod.

| file:line (construction) | class | runs on Linux? | verb(s) | notes |
|---|---|---|---|---|
| `repo_puller.py:807` | **SILENTLY-BROKEN** | YES — repo-puller daemon (every pod, every 15 min) | `restart` | `_kickstart_daemon`: `LaunchdScheduler(timeout=15.0).restart(label)` (`:808`). Post-pull kickstart of ~7 daemons. **Headline site.** Needs the 15s timeout (`:787-789`). |
| `retire.py:1278` | **SILENTLY-BROKEN** | YES — CLI / web / evo-tool bot retirement | `raw("bootout")` | `_stop_plist`: `LaunchdScheduler(timeout=15.0).raw("bootout", …)` (`:1279`). Constructs directly → bypasses `get_launchd_scheduler()` fail-fast. |
| `retire.py:1222` | **SILENTLY-BROKEN** | YES — same path (post-bootout verify) | `raw("print")` | `_launchctl_service_loaded`: `LaunchdScheduler(use_sudo=False, timeout=10.0).raw("print", …)` (`:1223`). Read-only probe; on Linux returns ambiguous→"still loaded" (fails retire loudly, not silent data corruption). |
| `mcp_service.py:155` (`_sched_sudo`) | **SILENTLY-BROKEN** | YES — CLI + web routes_mcp + repo_puller/deploy reload | `restart`, `kill`, `status`, `raw("bootout")` | `LaunchdScheduler(sudo_non_interactive=True, timeout=15.0)`. Verbs at `:351,397,416,467`. Module is framed "macOS launchd service management" — **see coordinator Q1** (is mcp-bridge installed at all on Linux?). |
| `mcp_service.py:162` (`_sched_nosudo`) | **SILENTLY-BROKEN** | YES — same | `status`, `raw("bootout")` | `LaunchdScheduler(use_sudo=False, timeout=15.0)`. Verbs at `:205,465`. |
| `cron_exit_monitor.py:112` | **SILENTLY-BROKEN** | YES — audit-scheduler daemon + maintenance run-now | `status` | `_status_probe`: `LaunchdScheduler(sudo_non_interactive=True, timeout=5.0).status(label)` (`:113`). Also parses launchd `wait(2)` exit semantics (deeper port concern, out of seam scope). |
| `ocadmin.py:140` (`_root_sched`) | **SILENTLY-BROKEN** | YES — deploy / setup_wizard / safe_upgrade / CLI | `restart` | `LaunchdScheduler(use_sudo=False)`. `_restart_gateway` → `.restart(svc)` at `:1182, :2493`. |
| `ocadmin.py:147` (`_sudo_sched`) | **SILENTLY-BROKEN** | YES — same | `raw("asuser list"/"bootout gui/…")` | `LaunchdScheduler()`. `.raw()` at `:261, :281` (gui-domain ops). |
| `applications/scanner.py:539` | **SILENTLY-BROKEN (low-sev)** | YES — per-bot app scanner | `list` | `LaunchdScheduler(use_sudo=False, runner=_as_bot_runner).list()`. Read-only label snapshot for LLM context; self-empties (`return []`) on Linux (launchctl absent → runner exception). Cosmetic data gap, not a crash. |
| `tunnel.py:40` | **NOT-ON-LINUX-POD (exempt)** | NO — `evolve-admin tunnel` runs on the operator's Mac laptop | `raw("list"/"load"/"unload")` | `_agent_launchctl = LaunchdScheduler(use_sudo=False)`. Documented sanctioned seam exemption (`:67-75`): client-side LaunchAgent surface, macOS by definition, not imported by any pod daemon. **Exclude from migration.** |
| `recovery.py:347` | **GRACEFUL** | YES — admin self-heal (pause/resume) | `raw` (guarded) | `_launchctl_n`: guarded by `isinstance(get_scheduler(), LaunchdScheduler)` at `:345` → returns no-op `(0,"","")` on non-launchd. Derives from the injected seam runner. Same correct pattern as `service._user_scheduler`. **No migration.** |
| `service.py:71` | **GRACEFUL** | YES — admin-server restart-self | `restart`/`status` (guarded) | `_user_scheduler()` (`:68-71`): `get_scheduler()` + `isinstance` check, returns `None` on non-launchd; call sites fall back. The reference legitimate pattern. **No migration.** |
| `applications/infra_audit.py:394` | **SILENTLY-BROKEN** | YES — infra-audit / audit-scheduler daemon (every pod) | `raw("list"/"print gui|user")` | `_launchctl_probe`: `LaunchdScheduler(sudo_non_interactive=True, timeout=5.0).raw(…)` (`:395`). Verbs at `:418,440,488`. **Pre-scan said "0 constructions, comment only" — that was wrong: there is a real construction at `:394`.** No platform gate; the `print gui/<uid>`/`user/<uid>` domain probes have no systemd analogue (deeper port). |
| `web/routes_maintenance.py:48` | **SILENTLY-BROKEN** | YES — admin web server (`GET /api/launchd/jobs`) | `raw("list"/"print-disabled")` | `_launchctl_probe`: `LaunchdScheduler(use_sudo=False, timeout=10.0).raw(…)` (`:49`). Read-only; feeds the Infra-Jobs admin page. Verbs at `:67,90`. |
| `web/routes_trust.py:34` | **SILENTLY-BROKEN** | YES — admin web server (`GET /api/trust`) | `status` | `_get_probe_scheduler`: `LaunchdScheduler(use_sudo=False, timeout=5.0)`; `.status("ai.openclaw.evolve.defer-runner")["managed"]` at `:338`. Read-only; `managed` would be false-wrong on Linux. |
| `tile_metrics.py:1836` | **SILENTLY-BROKEN** | YES — admin-UI tile rendering | `raw("list", label)` | `_check_infra_daemons`: `LaunchdScheduler(timeout=5.0).raw("list", label)` (`:1846`). On Linux: no rc=113 → skips → silently drops the "infra daemon down" chip (false-clean). Read-only. |
| `app_audit_runner.py:512` | **SILENTLY-BROKEN (low-sev)** | YES — per-bot app-audit runner | `list` | `_launchctl_labels`: `LaunchdScheduler(use_sudo=False, timeout=5.0).list()` in try/except→`[]` (`:512-514`). Self-empties on Linux; cron-label assertion treats empty as "nothing to compare." Cosmetic. |
| `metrics/resolvers/launchd.py:81` | **SILENTLY-BROKEN (noisy)** | YES — RSI metric resolution (every bot, every cycle) | `raw("print", target)` | `resolve_launchd_service_loaded`: `LaunchdScheduler(use_sudo=False, runner=_runner).raw("print", target)` (`:81-83`). **No platform switch and NO systemd sibling resolver/detector** — on Linux the `launchctl print` raises → `MetricValue(0.0)` → `sysadmin_watchdog`'s `launchd.service_loaded` detector (`generators/sysadmin_watchdog/detectors/platform/launchd.py:11`) fires `launchd_not_loaded` for EVERY bot. **Worst false-positive on the list.** |

### FAIL-FAST sites (excluded — tracked as S2 `raw()` debt, not migrated here)

These go through `get_launchd_scheduler()` (`scheduler.py:1245`), which **raises** `RuntimeError`
on non-launchd rather than misbehaving — they are loud, not silent, and are the separately-tracked
`raw()` migration debt. Enumerated for completeness, not for this worklist:
`setup_wizard.py:828,1115,1627` · `bot_templates/cli_integration.py:385` · `health.py:1801` ·
`cli.py:8881,8890,8948` · `web/admin_bot_routes.py:243` · `web/routes_admin.py:7485` ·
`applications/install_helpers.py:961,1097` · `spend_caps.py:415` · `oc_cli.py:846`.

### PORTABLE sites (no action — already on the injected seam)

All bare `get_scheduler().<verb>(…)` call sites are PORTABLE by construction (they resolve the
injected `SystemdScheduler` on Linux). Examples: `deploy.py` (many: `:3265,3631,5741,5967,5992,
8134,8307`), `health.py:185,1787`, `cli.py:8406,8607`, `web/server.py:2441`, `web/routes_oc.py:1777`,
`evo/tools/action_*.py`, `analyzer/heal.py:919`, `analyzer/permissions/writer.py:163,216`,
`analyzer/arbiter/appliers/{permissions,agent_defaults}.py`, `analyzer/autonomy/renderer.py:373`,
`applications/{install_helpers,audit_scheduler}.py`, `alerts/digest_dispatcher.py:508`,
`pairing/auto_approver.py:304`, `skills/_oc_install_common.py:222`, `deploy_verify.py:170`,
`oc_cli_device.py:631`. **These pass per-call config they do NOT need** (default sudo + 30s timeout
is correct for them), which is exactly why they were left on `get_scheduler()` and the broken sites
were not.

## Count summary (two ways to count)

**8 truly silently-broken pod-host sites** (excluding the `tunnel.py` Mac-client exemption and the
`recovery.py`/`service.py` graceful pair): `repo_puller.py:807`, `retire.py:1222`+`:1278`,
`mcp_service.py:155`+`:162`, `cron_exit_monitor.py:112`, `ocadmin.py:140`+`:147`,
`infra_audit.py:394`, `routes_maintenance.py:48`, `routes_trust.py:34`, `tile_metrics.py:1836`,
plus 3 low-severity self-emptying/`[]` sites (`scanner.py:539`, `app_audit_runner.py:512`) and 1
noisy-false-positive site (`metrics/resolvers/launchd.py:81`). Counting by **construction site**:
**14 silently-broken constructions** across **12 modules**; the daemons that hit them on every
Linux pod are **repo-puller** (kickstart), **audit-scheduler/infra-audit** (`cron_exit_monitor`,
`infra_audit`), **RSI metric resolution** (`metrics/resolvers/launchd`, fires a false signal per
bot), the **admin web server** (3 routes/tile probes), and the **retire / ocadmin** operator paths.
The **timeout-Protocol verdict is constructor-only**, so the migration's first dependency is adding
an optional per-call `timeout` to the Protocol verbs.

## Migration worklist (PR-sized bites, ordered by dependency × test-blast-radius)

Sizing rule (flow-rule: size by test-blast-radius). Bite 1 is the shared dependency; bites 2-5 are
grouped by module-cluster so each lands an isolated test surface; bite 6 (ratchet) is last so it
doesn't fail while debt still exists.

### Bite 1 — per-call `timeout` on the Protocol verbs *(blocks everything)*
**Files:** `runtime/scheduler.py` only (+ its tests).
**Change:** add an optional keyword `timeout: float | None = None` to the verbs the broken sites
call — `restart`, `remove`, `install`, `status`, `list` — on the `Scheduler` Protocol (`:220-226`)
and on BOTH adapters (`LaunchdScheduler` `:248`, `SystemdScheduler` `:921`). When `None`, fall back
to the constructor's `timeout` (the runner's closed-over value). Implementation: thread the override
into the per-call `_subprocess_runner(argv, timeout=…)` instead of the closure default. **Do NOT add
`timeout` to `raw()`** — `raw()` stays launchd-only debt (bites that use `raw()` migrate to verbs or
stay FAIL-FAST). Blast radius: scheduler unit tests + every fake-runner test (large but contained to
one module's test file). This bite ships green with NO call-site changes.

### Bite 2 — repo-puller kickstart (the headline, smallest blast radius)
**Files:** `repo_puller.py` (+ `tests/test_repo_puller*.py`).
**Change:** `_kickstart_daemon` (`:796-811`) → drop the module-global `_kickstart_scheduler`
(`:793,807`); call `get_scheduler().restart(label, timeout=15.0)`. Tests already inject via
`set_scheduler(LaunchdScheduler(runner=fake))` — flip the monkeypatch target from the module global
to `runtime.set_scheduler`. Highest value (runs on every pod every 15 min), tightest test surface.

### Bite 3 — admin web-server probes (3 read-only routes, one cluster)
**Files:** `web/routes_maintenance.py`, `web/routes_trust.py`, `tile_metrics.py`
(+ their route/tile tests).
**Change:** these are read-only probes (`list` / `print` / `status` / `print-disabled`).
- `routes_trust.py:34` calls only `status()` — swap to `get_scheduler().status(label)` (sudo posture
  difference is benign for a read; if not, pass an explicit no-sudo override — see note). Clean.
- `routes_maintenance.py:48` + `tile_metrics.py:1836` + `infra_audit.py:394` use `raw("list"/"print"/
  "print-disabled")` for the **tri-state** (rc=113 / "Could not find service") that `status()` folds
  away. Two options: (a) extend `status()`'s dict with a `not_found: bool` field so the tri-state is
  Protocol-expressible on both adapters (preferred — kills the `raw()` dependency), or (b) leave
  these as FAIL-FAST by routing through `get_launchd_scheduler()` (loud, not silent) until the port
  ships a systemd probe. **Recommend (a)** for `routes_maintenance`/`tile_metrics` (admin-UI surfaces
  an operator sees), and grouping `infra_audit.py:394` here too since it shares the tri-state need.

### Bite 4 — retire + ocadmin (operator destructive paths, `raw()`-heavy)
**Files:** `retire.py`, `ocadmin.py` (+ their tests).
**Change:** the destructive verbs (`bootout`, `kickstart`) have Protocol equivalents (`remove` minus
the plist-delete; `restart`). `ocadmin._restart_gateway` (`:1182,2493`) → `get_scheduler().restart(svc)`
(no special timeout). The `raw("bootout")` sites (`retire.py:1279`, `ocadmin.py:261,281`) need either
the `remove()`-without-plist-delete semantics (retire deletes the plist separately — `:1180-1184`) or
stay FAIL-FAST via `get_launchd_scheduler()`. **Recommend: route the bare-bootout `raw()` calls
through `get_launchd_scheduler()` (FAIL-FAST) for now** — they are launchd-domain-specific (gui/uid
bootout, separate-plist-rm) and a correct systemd port is a larger design (`systemctl stop` vs
`bootout` + unit-file rm). This bite *converts silent breakage to loud breakage* and migrates the
clean `restart` paths.

### Bite 5 — RSI metric resolver + watchdog detector (the false-signal fix)
**Files:** `metrics/resolvers/launchd.py`, `generators/sysadmin_watchdog/detectors/platform/launchd.py`,
`generators/sysadmin_watchdog/` registration (+ tests).
**Change:** this one needs a **platform branch**, not just a seam swap, because there is no systemd
sibling. Minimum: in `resolve_launchd_service_loaded` (`:74-99`) route through `get_scheduler()` and,
when the active adapter is `SystemdScheduler`, use `status(label)["managed"]/["running"]` instead of
`raw("print")`; register the metric key under a platform-neutral name (or add a `systemd.service_loaded`
resolver + a sibling detector). **Until done, every bot on every Linux pod emits a false
`launchd_not_loaded` Signal each cycle** — so this bite is high-priority despite "low" blast radius.
The two `list()`-only self-emptying sites (`scanner.py:539`, `app_audit_runner.py:512`) ride along
here: swap to `get_scheduler().list()` so on Linux they return the real systemd label set instead of
`[]` (improves the app-scan LLM context; pure upside, no destructive verb).

### Bite 6 — `mcp_service` (gated on coordinator Q1)
**Files:** `mcp_service.py` (+ tests). **Blocked on Q1** (is the mcp-bridge daemon installed on a
Linux pod at all?). If NO → mark the module macOS-only and gate its install/CLI surface behind the
platform check (no seam work needed). If YES → migrate `_scheduler_sudo`/`_scheduler_nosudo`
(`:152-163`) to `get_scheduler()` with per-call `timeout=15.0` for the Protocol verbs (`restart`,
`kill`, `status`) and FAIL-FAST `get_launchd_scheduler()` for the `raw("bootout")` legacy-agent path
(`:205,351`).

**Excluded from the worklist:** `tunnel.py:40` (Mac-client surface, documented exemption);
`recovery.py:347` + `service.py:71` (already GRACEFUL); all `get_launchd_scheduler()` FAIL-FAST sites
(S2 `raw()` debt, tracked separately).

## Bite — factory-usage ratchet (separate PR, lands after the worklist)

A lint/AST ratchet that **bans `LaunchdScheduler(` / `SystemdScheduler(` construction outside an
explicit allowlist**, so a new silently-broken site can't be introduced. Mirrors the existing
argv=0 launchctl ban (which catches raw *argv* but NOT adapter *construction* — that blind spot is
exactly this gap).

**Allowlist (the ONLY files permitted to construct an adapter directly):**
- `packages/analyzer/runtime/scheduler.py` — defines the adapters + `get_scheduler`/`get_launchd_scheduler`.
- `packages/admin/evolve_admin/setup_wizard.py::_activate_linux_platform` — the Linux platform gate,
  the one production caller of `set_scheduler(SystemdScheduler())` (`:3507`). Allowlist by
  `file:function` so only this function may construct `SystemdScheduler`.
- Test trees: `packages/*/tests/**` (fakes inject `LaunchdScheduler(runner=fake)`).
- `service.py::_user_scheduler` and `recovery.py::_launchctl_n` — the two GRACEFUL accessors
  (they construct a derived adapter *after* an `isinstance` guard). **Allowlist by `file:function`,
  not whole-file**, so a NEW unguarded construction in those files still trips.

**Mechanism:** an AST check (not regex — must distinguish `LaunchdScheduler(` construction from the
class definition and from `isinstance(x, LaunchdScheduler)`). Severity **block** in CI (`--strict`),
**warn** locally, matching the `tools/ui-style-lint` hybrid model. Wire into the existing
`tools/<gate>` Phase-6 toolchain. The ratchet ships **after** the worklist (it would fail while the
debt exists) — or ships first in **warn-only** mode and flips to **block** as the final worklist PR.

## Extending `linux-e2e` to cover the broken paths

`packages/admin/tests/e2e_linux/test_ubuntu_e2e.py` already injects `set_scheduler(SystemdScheduler())`
module-scope (`:188`) and step5 (`:600`) installs/runs/`status`/`running` a **real** systemd stub
unit via `get_scheduler()`. The gap: it exercises only seam-routed paths, never the bypass sites.
Add steps that drive the **highest-value broken paths through their real entrypoints** (so a
module-global `LaunchdScheduler()` that bypasses the injection is caught by an actual `systemctl`
failure / `launchctl: command not found`):

1. **Kickstart (bite 2):** after step5's unit is live, call `repo_puller._kickstart_daemon(GATEWAY_LABEL)`
   and assert `(ok, "ok")` — pre-migration this constructs `LaunchdScheduler` and fails on Ubuntu;
   post-migration it routes to the injected `SystemdScheduler` and restarts the unit. Verify the PID
   changed (`status()["pid"]`).
2. **Retire (bite 4):** call `retire._stop_plist(GATEWAY_LABEL, dry_run=False, RetireResult())` (or
   the verb-migrated equivalent) and assert the unit is gone (`not sched.running(...)`). Catches the
   `raw("bootout")` bypass.
3. **Metric resolver / false-signal (bite 5):** with `SystemdScheduler` active and the stub unit
   running, assert `resolve_launchd_service_loaded(BOT, now).value == 1.0` (post-migration) — today
   it raises→`0.0` and would fire a false watchdog Signal. This is the cheapest highest-signal assert
   (no new host state — reuses step5's unit).
4. **Recovery (GRACEFUL guard regression):** assert `recovery._launchctl_n("print", …)` returns the
   no-op `(0, "", "")` under `SystemdScheduler` — locks in that the graceful sites STAY graceful.

These reuse step5's stub unit and the existing `_host_cleanup` teardown (`:196`, which already
`remove()`s the labels), so the marginal host-state cost is near zero.

## Coordinator open questions

- **Q1 — mcp-bridge on Linux?** `mcp_service.py` is framed "macOS launchd service management." Is the
  `ai.evolve.evolve.mcp-bridge` daemon installed on a Linux pod at all? If not, bite 6 is a platform
  gate, not a seam migration. *(Blocks bite 6's shape.)*
- **Q2 — `raw()`/bootout systemd semantics.** The `bootout` + separate-plist-`rm` pattern
  (`retire.py`, `ocadmin.py`) maps to `systemctl stop` + unit-file removal, which `SystemdScheduler.remove()`
  already does atomically (`scheduler.py:1105`). Is the separate-diagnosis-of-plist-rm requirement
  (`retire.py:1180-1184`) still needed on systemd, or can retire adopt `remove()` wholesale on Linux?
- **Q3 — tri-state `status()`.** Adding `not_found: bool` to the `status()` dict (bite 3 option a)
  touches the Protocol return shape. Acceptable, or keep the `raw()`/FAIL-FAST split?
- **Q4 — `infra_audit` gui/user-domain probes.** `print gui/<uid>` / `print user/<uid>`
  (`:440,488`) have no systemd analogue. Are those audit checks meaningful on Linux, or should they
  be platform-gated out (vs. ported)?
