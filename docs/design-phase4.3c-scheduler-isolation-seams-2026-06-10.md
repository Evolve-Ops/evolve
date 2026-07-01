# Design: Phase 4.3 C — the Scheduler + IsolationProvider seams

**Status:** ✅ executed — Track S (S0–S3) and Track I shipped; see per-step PR notes below · **Date:** 2026-06-10 · **Roadmap:** Phase 4.3, step C

This scopes the **Scheduler** (launchd) and **IsolationProvider** (dscl/macOS user)
adapter seams — the second and third of the three runtime chokepoints named in
[design-phase4.3-runtime-adapter-seam-2026-06-09.md](design-phase4.3-runtime-adapter-seam-2026-06-09.md).
Phase A (the `AgentRuntime` interface) and Phase B (migrating all 18 `oc_cli`
importers onto it) are **done** — PRs #2560 (seam) + #2562 (migration). `oc_cli`
is now imported nowhere outside `oc_cli.py` and the seam; the agent runtime is
genuinely swappable. The seam itself is `packages/analyzer/runtime/agent_runtime.py`.

## The correction that makes C different from B

The Phase A doc's load-bearing claim was *"the coupling is funneled, not diffuse"* —
all three concerns run through one chokepoint each. **That held for the agent
runtime and does NOT hold for scheduling or isolation.** Measured on `main`
(2026-06-10, non-test files):

| Concern | Phase A's claimed chokepoint | Reality |
|---------|------------------------------|---------|
| Agent runtime | `oc_cli.py` — one module, 18 importers | ✅ True. Funnel existed; B was a mechanical migration. |
| **Scheduler** | `service.py::generate_plist` + "a handful of deploy fns" | ❌ **55 files** subprocess `launchctl`; **6 independent plist-XML generators**; `service.py` only wraps the *admin-ui daemon itself*. |
| **Isolation** | "`dscl`/`sysadminctl` in cli.py + provisioning, a handful of call sites" | ⚠️ Partly. **11 files** touch `dscl`/`sysadminctl`, but create/delete is semi-funnelled in `provisioning.py`. Closer to B than the scheduler is. |

**Consequence:** Phase B could skip straight to "migrate the importers" because
`oc_cli` *was already the funnel*. Phase C cannot — there is no funnel to migrate
onto. **The funnel must be built first.** Launching C as "migrate like B" would
mis-scope it and produce a half-abstraction with 50 call sites still calling
`launchctl` directly. This doc's central recommendation is therefore: **consolidate
→ then seam → then migrate**, scheduler and isolation tracked separately because
their funnel-debt is very different.

---

## Surface inventory (the real map)

### Scheduler — launchd

**launchctl verbs in use** (≈ the Scheduler interface, by frequency):
`kickstart` (125) · `bootstrap` (65) · `bootout` (56) · `list` (52) · `print`
(28) · `load`/`unload` (10) · `kill`/`enable`/`disable`/`print-disabled` (few).
These collapse to **6 operations**: install, remove, restart, list/exists,
status, kill.

**Six independent plist-XML emitters** (today each hand-builds launchd XML):
- `service.py::generate_plist` — the admin-ui daemon (host/port).
- `deploy.py::_plist_content` + `install_bot_gateway_plist` — per-bot gateway + infra jobs.
- `applications/install_helpers.py::_build_plist_xml` / `_build_command_plist_xml` — app/skill jobs.
- `alerts/digest_dispatcher.py::render_plist` and `applications/audit_scheduler.py::render_plist` — scheduled monitors.

**Top launchctl call-site files** (the funnel targets, by ref count): `deploy.py`
(41) · `setup_wizard.py` (35) · `applications/infra_audit.py` (30) ·
`mcp_service.py` (24) · `health.py` (22) · `bot_templates/cli_integration.py`
(20) · `service.py` (19) · `retire.py` (16) · `recovery.py` (16) · `cli.py` (15).

**Existing partial helper to build on:** `service.py::_launchctl(*args)` — a thin
`(rc, output)` wrapper, but scoped to the admin-ui service only. It's the shape
the generic adapter generalizes.

**Key entrypoints that must keep working unchanged:** `evolve-admin
install-infra-jobs` (cli.py::install_infra_jobs → deploy.py::install_evolve_infra_jobs,
referenced by ~10 files), per-bot deploy (`install_bot_gateway_plist`), and the
orphan sweep (`find_orphaned_plists`/`remove_orphaned_plists`).

### Isolation — dscl / macOS users

**Operations in use:** create user (`sysadminctl -addUser` / `dscl . create`),
`createhomedir`, delete user (`dscl . -delete /Users/<u>` + `rm -rf`), read UID
(`dscl . -read /Users/<u> UniqueID`), and the pervasive **run-as** primitive
(`sudo -H -u <user> …`).

**Already semi-funnelled:** `provisioning.py::create_macos_user` (the dscl +
createhomedir ritual) and `provisioning.py::_dscl_delete_user(user, *,
remove_home=True)`. There's even an injection precedent for testability —
`metrics/resolvers/users.py::set_dscl_runner`. Lifecycle create/delete lives in 5
files (cli, provisioning, setup_wizard, web/unix_socket_server, wizard).

**Run-as / home-dir resolution is split** across `oc_cli.py`
(`_resolve_user`/`_resolve_home_dir`/`_should_sudo`), `deploy.py`
(`get_bot_user`/`bot_home`), and `config.py`. This is the most-called isolation
primitive and the one the `AgentRuntime` adapter already leans on implicitly.

---

## The two interfaces

```python
# packages/analyzer/runtime/scheduler.py   (shipped home — beside agent_runtime; evolve_admin.runtime re-exports as a compat shim)
@dataclass
class JobSpec:
    """Platform-neutral description of a recurring/long-running job.
    The adapter renders it (launchd plist XML today; systemd unit / cron / a
    container later). Replaces the 6 hand-built plist emitters."""
    label: str
    program_args: list[str]
    user: str | None = None             # run-as identity (None = current)
    keep_alive: bool | dict = False     # bool, or launchd-style {SuccessfulExit:…}
    run_at_load: bool = False
    start_interval: int | None = None   # seconds (cron-like cadence)
    start_calendar: list[dict] | None = None
    working_dir: str | None = None
    env: dict[str, str] | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    # escape hatch for launchd-only keys during migration; audited out later
    extra: dict | None = None

class Scheduler(Protocol):
    # install returns InstallResult(ok, message, skipped) as shipped (S2b0) —
    # the idempotent skip is a field, never a message substring callers parse.
    def install(self, spec: JobSpec) -> InstallResult: ...        # render + bootstrap/load
    def remove(self, label: str) -> tuple[bool, str]: ...       # bootout/unload (+ rm plist)
    def restart(self, label: str) -> tuple[bool, str]: ...      # kickstart -k
    def list(self, *, prefix: str | None = None) -> list[str]: ...
    def status(self, label: str) -> dict: ...                   # print → {running, pid, last_exit, …}
    def running(self, label: str) -> bool: ...
    def kill(self, label: str) -> tuple[bool, str]: ...

# packages/analyzer/runtime/isolation.py   (shipped home — evolve_admin.runtime re-exports as a compat shim)
@dataclass
class Identity:
    bot_id: str
    user: str
    uid: int | None
    home: Path

class IsolationProvider(Protocol):
    def create(self, bot_id: str, *, user: str | None = None) -> Identity: ...  # dscl + createhomedir + sysadminctl
    def delete(self, bot_id: str, *, remove_home: bool = True) -> bool: ...      # dscl delete + rm home
    def resolve(self, bot_id: str) -> Identity | None: ...                       # network.json → user/uid/home
    def home_dir(self, bot_id: str) -> Path: ...
    def run_as(self, bot_id: str, argv: list[str], **kw) -> subprocess.CompletedProcess: ...  # sudo -H -u
```

`get_scheduler()` / `get_isolation()` mirror `get_runtime()` — process-wide
singleton + `set_*()` for test injection. `FakeScheduler` / `FakeIsolation` plus
a `SystemdScheduler` **stub** (compiles, raises `NotImplementedError`) are the
swappability proof (Phase D), the same way `FakeRuntime` was for the agent runtime.

---

## Migration plan — consolidate, then seam, then migrate

Run the **Scheduler** and **Isolation** tracks independently; isolation is smaller
and can go first as a confidence-builder, or in parallel.

### Track S — Scheduler (the big one)
- **S0. Consolidate plist emission.** *(✅ shipped — PR #2600)* Collapse the 6 plist generators into one
  `JobSpec`-driven renderer. Each call site builds a `JobSpec` instead of XML;
  golden-file tests assert the rendered plist is byte-identical to today's for
  every existing job (admin-ui, per-bot gateway, each infra job, digest, audit).
  *This is the load-bearing step and most of the work.* No behavior change.
- **S1. Define `Scheduler` + `LaunchdScheduler`.** *(✅ shipped — PR #2627)* Wrap `_launchctl` semantics
  (bootstrap/bootout/kickstart/print) behind the interface; `LaunchdScheduler`
  is the only adapter. Generalize `service.py::_launchctl`.
- **S2. Migrate the 55 launchctl call sites** *(✅ shipped — PRs #2628/#2629/#2630/#2631/#2632/#2645/#2647)* onto `get_scheduler()`, deploy.py +
  setup_wizard.py + infra_audit.py first (≈60% of refs). Behavior-preserving;
  per-file, green-tests, pyflakes-baseline discipline from Phase B.
  ⚠️ **`kickstart -k` is destructive** (restarts live gateways) — canary on one
  bot before fanning out; see the canary-on-an-affected-bot rule (auto-memory: feedback_canary_for_one_file_edits).
- **S3. Prove.** *(✅ shipped — PR #2649: FakeScheduler + SystemdScheduler stub +
  zero-subprocess proof + seam gates incl. the shrink-only raw()-debt census)*
  `FakeScheduler` + `SystemdScheduler` stub; a test that installs/
  restarts/removes a job against `FakeScheduler` with **no launchctl spawned**.

### Track I — Isolation (smaller; partly done)

*✅ shipped — I0–I3 landed in PR #2626 (`runtime/isolation.py`: IsolationProvider +
MacOSIsolation + FakeIsolation, lifecycle/run-as migration, zero-dscl proof).*
- **I0. Funnel run-as + resolution.** Unify `_resolve_user`/`get_bot_user`/
  `bot_home`/`_should_sudo` into one resolver the interface owns (today the
  `AgentRuntime` adapter and deploy.py each reimplement slices of this).
- **I1. Define `IsolationProvider` + `MacOSIsolation`.** Fold the existing
  `create_macos_user` / `_dscl_delete_user` (+ `set_dscl_runner` precedent) in.
- **I2. Migrate** the 5 lifecycle files + run-as call sites onto `get_isolation()`.
- **I3. Prove.** `FakeIsolation` (in-memory user table) — provisioning/retire flows
  run with no `dscl`/`sysadminctl` spawned.

### Estimate (vs Phase B's ~1 session)
- **Isolation:** ~1 session (funnel mostly exists).
- **Scheduler:** **2–3 sessions** — S0 (plist consolidation + golden tests) is its
  own session; S1+S2 another; S3 a short one. Do **not** try to land it as one PR.

---

## Risks & notes
- **No silent half-abstraction.** The failure mode is shipping `Scheduler` while
  30 files still call `launchctl` directly — worse than not starting, because it
  reads as "done." S2 must be tracked to **0 remaining** non-test `launchctl`
  subprocess sites (the same `git grep` gate Phase B used for `oc_cli`).
- **`kickstart -k` and `bootout` are live-traffic destructive.** Golden-file +
  canary before fan-out; these restart/stop running bot gateways.
- **Plist parity is the whole game in S0.** A one-key drift in rendered XML can
  silently change launchd behavior (KeepAlive semantics, run-as uid). Byte-diff
  every generated plist against the pre-refactor output.
- **Placement (RESOLVED, S2b0 2026-06-10).** The seam modules live in
  `packages/analyzer/runtime/` beside `agent_runtime.py`. S1 shipped them under
  `packages/admin/evolve_admin/runtime/`, but S2's remaining migration covers
  analyzer-package files (heal.py, oc_cli.py, …) and analyzer importing
  evolve_admin is the reverse-coupling direction Phase 6.1 forbids — so S2b0
  moved them and left `evolve_admin.runtime` as a pure re-export shim (state
  singletons live only in the analyzer modules).
- **Diligence value is front-loaded.** The agent runtime was *the* headline
  lock-in finding and it's done. Scheduler/isolation harden the story but the
  marginal de-risking is smaller — which is the argument for doing the
  **AgentRuntime swappability proof (Phase D-partial) first**, then Track I, then
  Track S, rather than blocking on the diffuse scheduler work.

## Open questions to resolve before S1/I1
1. **One package or two seams' home?** `analyzer/runtime/` (beside `agent_runtime`)
   vs `admin/evolve_admin/runtime/`. The scheduler/isolation callers are almost
   all admin-side → lean admin. *(Resolved the other way in S2b0: analyzer-side
   home, admin shim — see the Placement note above.)*
2. **`JobSpec.extra` escape hatch** — keep permanently, or treat as migration-only
   debt with a lint that drives it to empty (mirrors the `command()` escape-hatch
   decision in the AgentRuntime seam)?
3. **Does Track S subsume `service.py`'s admin-ui install/uninstall/restart**, or
   stay separate (it's the one job that manages the admin server itself, a
   bootstrap-order special case)?
4. **Sequencing:** Phase D-partial (prove AgentRuntime swappability) before C, or
   fold the AgentRuntime proof into S3/I3's "no-Mac stack run"? Recommend the
   former — it's shippable now and independently valuable.
