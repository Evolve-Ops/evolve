"""First-audit deferral in the Tier-3 audit runner (decision D).

A just-built app's FIRST Tier-3 audit is provisioning cost — a same-day
adversarial re-audit of fresh, to-spec forge output that found "no dominant
pattern" (finding internal/finding-new-bot-activation-cost-2026-06-12.md). D
defers that first audit out of the activation window: a never-audited app
younger than the 7-day grace window has its first audit DEFERRED, not skipped.

The deferral must never become a silent coverage gap, so these tests prove
both halves of the contract:

  1. A brand-new app's first audit is NOT run on build-day (deferred), and
  2. the first audit DOES fire once the app crosses the +7d boundary — the
     gate is time-bounded and fail-open, so the recurring hourly cadence
     tick runs it. The "first audit never runs" failure case is impossible.

D changes WHEN the first audit runs; C (decision C, #2817) changed WHICH
model it runs on. They compose: deferred apps `continue` before C's model
resolution, and once the audit fires C's standard-role routing still applies.

internal/finding-new-bot-activation-cost-2026-06-12.md
internal/roadmap-user-value-2026-06-10.md  (Cost-defaults decision)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_audit_runner  # noqa: E402


# ── _defer_first_audit (the predicate) ───────────────────────────────────────


_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_defer_fresh_never_audited_app() -> None:
    """A never-audited app built today is deferred, with a reason that names
    the boundary it will fire at."""
    m = {"id": "a", "created_at": _iso(_NOW - timedelta(hours=2))}
    defer, reason = app_audit_runner._defer_first_audit(m, now=_NOW)
    assert defer is True
    assert "deferred" in reason
    assert "first real usage" in reason  # follow-up boundary documented


def test_no_defer_at_grace_boundary() -> None:
    """At exactly the grace boundary the audit runs — this is the proof the
    deferred audit FIRES once age ≥ 7d. The gate is time-bounded; it cannot
    defer forever."""
    created = _NOW - timedelta(days=app_audit_runner._FIRST_AUDIT_GRACE_DAYS)
    m = {"id": "a", "created_at": _iso(created)}
    defer, reason = app_audit_runner._defer_first_audit(m, now=_NOW)
    assert defer is False
    assert "grace elapsed" in reason


def test_no_defer_past_grace() -> None:
    """A never-audited app older than the grace window audits normally —
    deferral only targets the activation window, never legacy/old apps."""
    m = {"id": "a", "created_at": _iso(_NOW - timedelta(days=30))}
    defer, _ = app_audit_runner._defer_first_audit(m, now=_NOW)
    assert defer is False


def test_no_defer_when_already_audited() -> None:
    """Steady-state audits are never deferred — only the FIRST one."""
    m = {
        "id": "a",
        "created_at": _iso(_NOW),  # fresh, but...
        "last_audit": {"verified_at": _iso(_NOW - timedelta(hours=1))},
    }
    defer, reason = app_audit_runner._defer_first_audit(m, now=_NOW)
    assert defer is False
    assert reason == "not first audit"


def test_no_defer_missing_created_at_failopen() -> None:
    """No created_at ⇒ can't prove the app is fresh ⇒ audit now. Fail-open is
    the invariant: a deferral must never become a silent coverage gap."""
    m = {"id": "a"}  # never audited, no created_at
    defer, reason = app_audit_runner._defer_first_audit(m, now=_NOW)
    assert defer is False
    assert "fail-open" in reason


def test_no_defer_unparseable_created_at_failopen() -> None:
    """A garbage created_at also fails open to auditing now."""
    m = {"id": "a", "created_at": "not-a-timestamp"}
    defer, reason = app_audit_runner._defer_first_audit(m, now=_NOW)
    assert defer is False
    assert "fail-open" in reason


# ── run_tier3 integration ────────────────────────────────────────────────────


class _FakeAuditOutput:
    """Minimal stand-in for AuditOutput — enough for run_tier3's post-dispatch
    bookkeeping (no decisions/observations ⇒ the per-finding loop is skipped)."""

    def __init__(self) -> None:
        self.tokens_used = 0
        self.decisions: list = []
        self.observations: list = []
        self.status = "ok"
        self.error = ""

    def to_dict(self) -> dict:
        return {}


def _write_manifest(workspace: Path, app_id: str, manifest: dict) -> None:
    d = workspace / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    manifest.setdefault("id", app_id)
    manifest.setdefault("status", "active")
    (d / f"{app_id}.json").write_text(json.dumps(manifest))


def _install_dispatch_recorder(monkeypatch) -> list[str]:
    """Patch run_tier3_for_app to record dispatched app_ids without doing work.
    Returns the (mutable) list that accumulates dispatched ids."""
    dispatched: list[str] = []

    def fake_dispatch(*, manifest, **kwargs):
        dispatched.append(manifest.get("id"))
        return _FakeAuditOutput()

    monkeypatch.setattr(app_audit_runner, "run_tier3_for_app", fake_dispatch)
    return dispatched


def test_cadence_sweep_defers_fresh_runs_aged(tmp_path, monkeypatch) -> None:
    """THE proof artifact. In a normal cadence sweep:

      * a brand-new never-audited app is NOT audited on build-day (deferred), and
      * a never-audited app past the +7d boundary IS audited (the boundary fires).

    Both apps are never-audited (both cadence-"due"); only age decides.
    """
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    _write_manifest(workspace, "fresh-app", {
        "created_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    _write_manifest(workspace, "aged-app", {
        "created_at": (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    dispatched = _install_dispatch_recorder(monkeypatch)

    result = app_audit_runner.run_tier3(
        workspace,
        bot_id="ledger",
        shared_dir=tmp_path / "shared",
    )

    # Build-day app deferred; aged app audited.
    assert "fresh-app" not in dispatched
    assert "aged-app" in dispatched
    assert result["apps_first_audit_deferred"] == 1
    assert result["apps_audited"] == 1

    # Observable: a per-app audit_deferred trail entry was written for the
    # deferred app (so it shows in the UI, not just the log).
    trail = workspace / "evolve" / "audits" / "fresh-app" / "trail.jsonl"
    assert trail.exists()
    entries = [json.loads(ln) for ln in trail.read_text().splitlines() if ln.strip()]
    deferred_entries = [e for e in entries if e.get("kind") == "audit_deferred"]
    assert len(deferred_entries) == 1
    assert deferred_entries[0]["tier"] == 3
    assert deferred_entries[0]["grace_days"] == app_audit_runner._FIRST_AUDIT_GRACE_DAYS
    assert "deferred" in deferred_entries[0]["reason"]


def test_force_due_bypasses_deferral(tmp_path, monkeypatch) -> None:
    """The Tier-2 critical-structural → Tier-3 escalation (force_due) must
    audit a fresh app immediately — a real critical finding is never deferred,
    so the deferral can't hide a genuine problem."""
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    _write_manifest(workspace, "fresh-app", {
        "created_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    dispatched = _install_dispatch_recorder(monkeypatch)

    result = app_audit_runner.run_tier3(
        workspace, bot_id="ledger", shared_dir=tmp_path / "shared",
        force_due=True,
    )
    assert "fresh-app" in dispatched
    assert result["apps_first_audit_deferred"] == 0


def test_only_apps_bypasses_deferral(tmp_path, monkeypatch) -> None:
    """An explicit per-app request (operator / evo) audits a fresh app now —
    manual requests bypass cadence and deferral alike."""
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    _write_manifest(workspace, "fresh-app", {
        "created_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    dispatched = _install_dispatch_recorder(monkeypatch)

    result = app_audit_runner.run_tier3(
        workspace, bot_id="ledger", shared_dir=tmp_path / "shared",
        only_apps=["fresh-app"],
    )
    assert "fresh-app" in dispatched
    assert result["apps_first_audit_deferred"] == 0
