# Phase 4.3 C — Track S, Step S0: consolidate launchd plist emission behind `JobSpec`

**Status:** ready to execute · **Date:** 2026-06-10 · **Roadmap:** Phase 4.3 C, Track S, Step S0

Tactical execution spec for the first step of the Scheduler track. Parent design:
[design-phase4.3c-scheduler-isolation-seams-2026-06-10.md](design-phase4.3c-scheduler-isolation-seams-2026-06-10.md).
This step is deliberately the most **independent** slice of Phase C — it shares
zero files with the in-flight Phase D session and invokes no `launchctl`, so it
is safe to run in parallel.

## Goal

Collapse the **6 hand-rolled launchd plist-XML generators** into one
`JobSpec`-driven renderer, with **no behavior change**. This builds the funnel
that Steps S1 (the `Scheduler` interface) and S2 (migrating the 55 `launchctl`
call sites) depend on.

## Scope — do exactly this, nothing more

Running in parallel with a Phase D session; staying in-scope is what keeps it
conflict-free.

**IN:**
- Create `packages/admin/evolve_admin/runtime/__init__.py` +
  `packages/admin/evolve_admin/runtime/scheduler.py` holding the `JobSpec`
  dataclass and a pure `render_launchd_plist(spec: JobSpec) -> str`.
  *(Historical: S2b0 later moved the module to
  `packages/analyzer/runtime/scheduler.py`; `evolve_admin.runtime` remains
  as a re-export shim.)*
- Rewrite each of the 6 emitters to build a `JobSpec` and call the renderer
  instead of formatting XML by hand.

**OUT (do NOT touch — these are S1/S2, and would collide with Phase D):**
- The `Scheduler` Protocol / adapter (that's S1).
- Any `launchctl` subprocess call site (that's S2).
- The agent-runtime seam, anything under `dscl`/`sysadminctl`, and
  `audit.py` / `routes_oc.py` / `oc_audit.py`.
- For `install_bot_gateway_plist`: refactor **only** the XML-string production;
  leave its `launchctl bootstrap` call exactly as-is.

## The 6 emitters to migrate

All under `packages/admin/evolve_admin/`:

| # | Location | Signature / note |
|---|----------|------------------|
| 1 | `service.py::generate_plist(host, port)` | admin-ui daemon; uses `_PLIST_TEMPLATE` |
| 2 | `deploy.py::_plist_content(label, user, script_path, schedule, extra_args, run_at_load, jitter_seconds)` | generic infra job; **`jitter_seconds` bash-sleep wrapper** → add a `jitter_seconds` field to `JobSpec` |
| 3 | `deploy.py::install_bot_gateway_plist(...)` | extract inline gateway XML into a `JobSpec`; keep the bootstrap call |
| 4 | `applications/install_helpers.py::_build_plist_xml(...)` | launchd_python_signal wrapper |
| 5 | `applications/install_helpers.py::_build_command_plist_xml(...)` | generic command |
| 6 | `alerts/digest_dispatcher.py::render_plist(...)` and `applications/audit_scheduler.py::render_plist(...)` | scheduled-monitor daemons |

## `JobSpec` shape

From the parent design doc; extend only if an emitter needs a field:

```python
@dataclass
class JobSpec:
    label: str
    program_args: list[str]
    user: str | None = None
    keep_alive: bool | dict = False      # bool or launchd {SuccessfulExit: …}
    run_at_load: bool = False
    start_interval: int | None = None    # seconds
    start_calendar: list[dict] | None = None
    working_dir: str | None = None
    env: dict[str, str] | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    jitter_seconds: int = 0              # emitter #2's bash-sleep wrapper
    extra: dict | None = None            # migration-only escape hatch
```

`extra` is a migration-only escape hatch for any launchd key not yet modelled —
emit a `log()`/warning when it is non-empty so leftover debt is visible. If a
`JobSpec` round-trip can't reproduce a key, **model it as a real field** rather
than hiding it in `extra`.

## Acceptance gate — parity is the whole game

For every one of the 6 emitters, add a golden test that builds the equivalent
`JobSpec`, renders it, and asserts:

```python
import plistlib
assert plistlib.loads(new_xml) == plistlib.loads(old_xml)
```

where `old_xml` is captured from the pre-refactor function (snapshot the current
output into the test as a fixture **before** you refactor).

**Use parsed-dict equality, not raw byte-diff.** launchd only sees the parsed
keys/values; a single renderer cannot reproduce 6 different whitespace/key-order
styles, and forcing it to would defeat the consolidation. Parsed equality still
catches every behavior-relevant difference: missing keys, wrong value types
(e.g. `StartInterval` as int vs string), `KeepAlive` shape, run-as `UserName`.

## Discipline

- `git log origin/main..` at the start **and** again right before `gh pr create`
  — a Phase D session is in flight; rebase if `main` moved.
- Branch off `main`. (The Phase C design docs land via PR #2567; if not yet on
  `main`, read them from the `phase4.3c-design` branch.)
- pyflakes every changed file, baseline-diffed against `origin/main` with
  **line:col normalised** — a docstring shift renumbers downstream warnings and
  naive `comm` mis-flags them as new (Phase B retro).
- Two-pass: build-then-self-review for silent failure. A dropped plist key reads
  as "fine" but changes launchd `KeepAlive` / run-as behavior at runtime.
- One PR: `refactor(4.3): consolidate launchd plist emission behind JobSpec (Phase C S0)`.
  **No `launchctl` is invoked by this change** — it's non-destructive; say so in
  the PR body.

## Done when

- All 6 emitters route through `render_launchd_plist`.
- Every golden test passes (parsed-dict parity).
- pyflakes clean (baseline-diffed).
- `git grep -nE "_PLIST_TEMPLATE|def .*plist.*-> str|render_plist"` shows the new
  renderer is the single source of plist XML.

## Why this is safe to run parallel to Phase D

The 6 emitters live in `service.py`, `deploy.py`, `install_helpers.py`,
`digest_dispatcher.py`, `audit_scheduler.py`. Phase D's files are
`agent_runtime.py`, `audit.py`, `oc_audit.py`, `routes_oc.py`,
`routes_cost_measures.py`, `routes_bot_config.py`. **The two sets are disjoint.**
The launchctl call-site collisions noted in the parent doc (audit.py, routes_oc.py)
belong to Step **S2**, which is explicitly out of scope here.
