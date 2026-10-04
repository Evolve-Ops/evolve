"""Data model for the gallery install-verify harness.

Pure dataclasses + status constants. No pod imports, so the pipeline and
assertion logic that build these are importable (and unit-testable) on a
dev laptop with no live pod.

The result hierarchy is three levels:

    RunReport            one sweep (many apps)
      └── AppResult      one (pkg_id, bot_id) pipeline run
            └── StepResult   one pipeline step (preflight / install / ...)
                  └── Check      one assertion inside a step

A ``Check`` is the atom of evidence: a named, pass/fail (or skipped)
observation with a human-readable ``detail``. Overall status rolls UP from
checks — a step fails if any blocking check fails; an app fails if any step
fails. Skipped checks are recorded verbatim and counted separately so a
PASS row can never silently hide un-run coverage (the adversarial
"PASS on a skipped assertion" failure mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── App-level outcomes ────────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
BLOCKED_OAUTH = "BLOCKED-OAUTH"
SKIPPED = "SKIPPED"

# ── Step names (stable; used as the ``failed_step`` value + in tests) ─────────
STEP_DEPENDENCY = "dependency"
STEP_PREFLIGHT = "preflight"
STEP_INSTALL = "install"
STEP_ASSERT = "post_install_assertions"
STEP_VERIFY = "verify_slot"
STEP_TEARDOWN = "teardown"

# ── Check severity ────────────────────────────────────────────────────────────
# BLOCK   → a failed check fails its step (and the app).
# WARN    → a failed check is surfaced but does NOT fail the app.
# INFO    → informational only (never "fails").
BLOCK = "block"
WARN = "warn"
INFO = "info"


@dataclass
class Check:
    """One assertion: a named observation with evidence."""

    name: str
    ok: bool
    detail: str = ""
    severity: str = BLOCK
    # skipped=True means the check did not run (e.g. Linux-only artifact on
    # macOS, or a not-yet-reconciled recon ledger). A skipped check is never
    # ``ok`` and never a failure — it is absent coverage, reported as such.
    skipped: bool = False

    @property
    def failed(self) -> bool:
        return (not self.ok) and (not self.skipped) and self.severity == BLOCK

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "severity": self.severity,
            "detail": self.detail,
        }


def ok_check(name: str, detail: str = "", severity: str = BLOCK) -> Check:
    return Check(name=name, ok=True, detail=detail, severity=severity)


def fail_check(name: str, detail: str, severity: str = BLOCK) -> Check:
    return Check(name=name, ok=False, detail=detail, severity=severity)


def skip_check(name: str, detail: str) -> Check:
    return Check(name=name, ok=False, detail=detail, severity=INFO, skipped=True)


@dataclass
class StepResult:
    """The outcome of one pipeline step."""

    step: str
    checks: list[Check] = field(default_factory=list)
    # Set when the step short-circuits with a status that isn't derivable
    # from its checks alone (install → BLOCKED-OAUTH, or a step that never
    # ran). None ⇒ derive from checks.
    forced_status: str | None = None
    detail: str = ""

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def failed(self) -> bool:
        return any(c.failed for c in self.checks)

    @property
    def ran(self) -> bool:
        """A step 'ran' if it produced any non-skipped check."""
        return any(not c.skipped for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "failed": self.failed,
            "detail": self.detail,
            "forced_status": self.forced_status,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class AppResult:
    """One (pkg_id, bot_id) pipeline run."""

    pkg_id: str
    bot_id: str
    display_name: str = ""
    status: str = SKIPPED
    failed_step: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    # Free-form one-liner surfaced in the console row (the headline reason).
    summary: str = ""

    def step(self, name: str) -> StepResult:
        s = StepResult(step=name)
        self.steps.append(s)
        return s

    @property
    def checks_run(self) -> int:
        return sum(1 for st in self.steps for c in st.checks if not c.skipped)

    @property
    def checks_skipped(self) -> int:
        return sum(1 for st in self.steps for c in st.checks if c.skipped)

    @property
    def checks_warned(self) -> int:
        """Non-blocking checks that flagged a concern (failed WARN checks)."""
        return sum(
            1 for st in self.steps for c in st.checks
            if (not c.ok) and (not c.skipped) and c.severity == WARN
        )

    def to_dict(self) -> dict[str, Any]:
        # identity: see applications.app_identity.resolve_app_id — ``pkg_id``
        # here is this harness's own report-schema column (the sweep is keyed
        # by gallery package, not by app manifest), and the JSON it emits is
        # consumed by report tooling. Not a manifest read; the resolver does
        # not apply.
        return {
            "pkg_id": self.pkg_id,
            "bot_id": self.bot_id,
            "display_name": self.display_name,
            "status": self.status,
            "failed_step": self.failed_step,
            "summary": self.summary,
            "checks_run": self.checks_run,
            "checks_skipped": self.checks_skipped,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class RunReport:
    """The whole sweep."""

    mode: str = "install"  # install | dry-run
    started_at: str = ""
    finished_at: str = ""
    pod_platform: str = ""
    teardown: bool = True
    apps: list[AppResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, BLOCKED_OAUTH: 0, SKIPPED: 0}
        for a in self.apps:
            out[a.status] = out.get(a.status, 0) + 1
        return out

    @property
    def any_failed(self) -> bool:
        return any(a.status == FAIL for a in self.apps)

    def exit_code(self) -> int:
        return 1 if self.any_failed else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "gallery-verify-v1",
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pod_platform": self.pod_platform,
            "teardown": self.teardown,
            "counts": self.counts(),
            "apps": [a.to_dict() for a in self.apps],
        }
