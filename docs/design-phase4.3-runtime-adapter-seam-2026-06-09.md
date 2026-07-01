# Design: Phase 4.3 — the runtime/scheduler/isolation adapter seam

**Status:** design / awaiting go-ahead · **Date:** 2026-06-09 · **Roadmap:** Phase 4.3

This addresses the **last unanswered finding** from the 2026-06-09 diligence review:
single-platform / single-runtime / single-host lock-in (Evolve is welded to
OpenClaw + macOS/launchd + one Mac mini, none of which the company controls). The
review called it *"the only large strategic item — schedule it deliberately while
the coupling surface is still small."* This is that schedule.

## The good news: the coupling is funneled, not diffuse

"535 files reference openclaw" overstates it. The real **code** coupling runs
through three existing chokepoints:

| Concern | Chokepoint today | Coupling magnitude |
|---------|------------------|--------------------|
| **Agent runtime** (talk to a bot's agent) | `packages/analyzer/oc_cli.py` — *documented* single entry: "Never call openclaw directly from other modules" | **34 importers** |
| **Scheduler** (run recurring jobs) | `service.py::generate_plist` + `deploy.py` per-bot plist fns | launchd plist emission, a handful of fns |
| **Isolation** (one identity per bot) | `dscl`/`sysadminctl` in `cli.py` + provisioning | a handful of call sites |

So the seam is **formalizing existing boundaries** into interfaces, not untangling
the codebase. That's why this is tractable now and gets more expensive as the
chokepoints erode.

## The three interfaces

```python
# packages/analyzer/runtime/agent_runtime.py  (proposed)
class AgentRuntime(Protocol):
    """Talk to one bot's agent. The macOS/OpenClaw adapter is today's oc_cli."""
    def status(self, bot_id: str) -> dict | None: ...
    def health(self, bot_id: str, *, timeout: int | None = None) -> dict | None: ...
    def models(self, bot_id: str) -> list | None: ...
    def channels(self, bot_id: str) -> list | None: ...
    def config_get(self, bot_id: str, key: str) -> dict | None: ...
    def config_set(self, bot_id: str, key: str, value: str) -> bool: ...
    def full_config_get(self, bot_id: str) -> dict | None: ...
    def full_config_set(self, bot_id: str, cfg: dict) -> bool: ...
    def model_get(self, bot_id: str) -> dict | None: ...
    def model_set(self, bot_id: str, model: str) -> bool: ...
    def memory_get(self, bot_id: str) -> dict | None: ...
    def memory_set(self, bot_id: str, value: str) -> bool: ...
    def gateway_restart(self, bot_id: str) -> bool: ...
    def security_audit(self, bot_id: str) -> dict | None: ...
    def cron_list(self, bot_id: str) -> list | None: ...
    # …the ~25 oc_* functions, 1:1.

class Scheduler(Protocol):
    """Install/remove recurring + long-running jobs. macOS adapter = launchd."""
    def install_job(self, label: str, spec: JobSpec) -> bool: ...
    def remove_job(self, label: str) -> bool: ...
    def restart_job(self, label: str) -> bool: ...
    def list_jobs(self, *, prefix: str | None = None) -> list[str]: ...
    def job_running(self, label: str) -> bool: ...

class IsolationProvider(Protocol):
    """Create/destroy an isolated identity per bot. macOS adapter = dscl user."""
    def create_identity(self, bot_id: str) -> Identity: ...
    def delete_identity(self, bot_id: str) -> bool: ...
    def home_dir(self, bot_id: str) -> Path: ...
    def run_as(self, bot_id: str, argv: list[str], **kw) -> subprocess.CompletedProcess: ...
```

`JobSpec` / `Identity` are small dataclasses describing the job/identity in
platform-neutral terms; each adapter renders them (launchd plist XML, systemd
unit, a container, …).

## Migration plan — behavior-preserving, phased

- **A. Define `AgentRuntime` + the `OpenClawRuntime` adapter** that simply
  delegates to today's `oc_cli` functions. **Zero behavior change** — it's the
  same calls behind an interface. Add `runtime.get_runtime()` returning the
  OpenClaw adapter by default. *(~2–3 days, fully testable with a fake adapter.)*
- **B. Migrate the 34 `oc_cli` importers** to depend on `AgentRuntime` (via
  `get_runtime()` or injection). Mechanical, one importer at a time, each a
  green-tests no-op. *(~3–4 days, dispatchable once A lands.)*
- **C. Same for `Scheduler` (launchd) and `IsolationProvider` (dscl)** — fewer
  call sites, same pattern. *(~1 week.)*
- **D. Prove swappability:** a second adapter set — even a `FakeRuntime` (already
  needed for tests) + a `SystemdScheduler` **stub** — that compiles against the
  interfaces. The proof artifact: the test suite runs the whole stack against the
  fake adapters with **no OpenClaw/launchd/macOS present**. *(days.)*

## What this buys — and doesn't

- **Buys:** converts *"rewrite the integration, scheduling, and isolation layers
  to ever leave the Mac"* into *"implement one adapter set."* It de-risks the
  review's lock-in finding and makes the runtime/OS swappable interfaces rather
  than load-bearing assumptions. It also makes the whole system **testable without
  a Mac/OpenClaw** (the fake adapters), which is independently valuable.
- **Does NOT** deliver Linux or a non-OpenClaw runtime. It's the seam, not the
  port. Building a real second adapter (systemd + Linux isolation + a different
  agent runtime) is a separate, larger effort — but one that becomes *additive*
  instead of a rewrite.

## The decision
**Do we invest in the seam now?** Recommendation: **yes, at least phases A+B**
(`AgentRuntime`), because it's the highest-value/most-funneled chokepoint and the
test-without-a-Mac payoff is immediate. C/D can follow. This is the one move that
turns the diligence "no defensible platform, capped to Mac owners" into "a runtime
abstraction designed for substrate optionality" — which is already the stated
substrate strategy; 4.3 is where it stops being a slogan and becomes an interface.
