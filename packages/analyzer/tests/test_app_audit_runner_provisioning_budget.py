"""Tier-3 audit runner honors the provisioning budget (decision B).

The audit pipeline is the second provisioning spender (the first being forge).
A first (provisioning) audit must respect the same backstop: it pauses when
the bot's daily cost breaker is tripped or the one-time provisioning ceiling
is reached, instead of bleeding past the trip the way the `ledger` audit wave
did.

This composes with D (which defers the first audit by TIME): an audit only
reaches this gate if D let it through (grace elapsed, or D fail-open), and
this refuses by BUDGET. The gate is fail-open — any failure resolving the
budget audits now, so it can never become a silent coverage gap (same
invariant as D).

  - ``_provisioning_budget_skip`` — the predicate (refuse / allow / fail-open).
  - ``run_tier3`` integration — a first audit D let through is skipped when the
    gate refuses, and the pause is observable in the per-app trail.

docs/finding-new-bot-activation-cost-2026-06-12.md
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_audit_runner  # noqa: E402
import provisioning_budget as pb  # noqa: E402

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _refused(check="daily_breaker", reason="daily cost breaker is tripped"):
    return pb.ProvisioningBudgetDecision(
        allowed=False, failed_check=check, reason=reason,
        ceiling_usd=12.0, spent_usd=1.0,
        signal_payload={"type": "provisioning_budget_exceeded", "bot_id": "ledger"},
    )


# ── _provisioning_budget_skip predicate ──────────────────────────────────────


def test_skip_when_budget_refuses(monkeypatch):
    monkeypatch.setattr(pb, "evaluate", lambda *a, **k: _refused())
    monkeypatch.setattr(pb, "emit_signal", lambda *a, **k: True)
    skip, reason = app_audit_runner._provisioning_budget_skip(
        "ledger", Path("/tmp/x"), app_id="journal", now=_NOW,
    )
    assert skip is True
    assert "breaker" in reason


def test_no_skip_when_allowed(monkeypatch):
    monkeypatch.setattr(
        pb, "evaluate",
        lambda *a, **k: pb.ProvisioningBudgetDecision(allowed=True, ceiling_usd=12.0),
    )
    skip, _ = app_audit_runner._provisioning_budget_skip(
        "ledger", Path("/tmp/x"), app_id="journal", now=_NOW,
    )
    assert skip is False


def test_fail_open_when_evaluate_raises(monkeypatch):
    """A backstop read failure must audit, not silently skip."""
    def _boom(*a, **k):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(pb, "evaluate", _boom)
    skip, reason = app_audit_runner._provisioning_budget_skip(
        "ledger", Path("/tmp/x"), app_id="journal", now=_NOW,
    )
    assert skip is False
    assert "fail-open" in reason


# ── run_tier3 integration ────────────────────────────────────────────────────


class _FakeAuditOutput:
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


def test_run_tier3_skips_first_audit_when_budget_refuses(tmp_path, monkeypatch):
    """THE call-site proof. An aged, never-audited app (D does NOT defer it —
    grace elapsed) is still skipped when the provisioning budget refuses, and
    the pause is recorded in the per-app trail."""
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    # Aged 8d ⇒ D's time-deferral does not apply ⇒ reaches the budget gate.
    _write_manifest(workspace, "aged-app", {
        "created_at": (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    dispatched: list[str] = []

    def fake_dispatch(*, manifest, **kwargs):
        dispatched.append(manifest.get("id"))
        return _FakeAuditOutput()

    monkeypatch.setattr(app_audit_runner, "run_tier3_for_app", fake_dispatch)
    monkeypatch.setattr(pb, "evaluate", lambda *a, **k: _refused())
    monkeypatch.setattr(pb, "emit_signal", lambda *a, **k: True)

    result = app_audit_runner.run_tier3(
        workspace, bot_id="ledger", shared_dir=tmp_path / "shared",
    )

    assert "aged-app" not in dispatched          # paused — not audited
    assert result["apps_audited"] == 0
    assert result["apps_first_audit_deferred"] == 1

    # Observable in the trail (the bot-side-reliable record; the Signal write
    # may be denied bot-side, the trail is not).
    trail = workspace / "evolve" / "audits" / "aged-app" / "trail.jsonl"
    assert trail.exists()
    entries = [json.loads(ln) for ln in trail.read_text().splitlines() if ln.strip()]
    paused = [e for e in entries if e.get("kind") == "audit_budget_paused"]
    assert len(paused) == 1
    assert paused[0]["tier"] == 3


def test_run_tier3_audits_when_budget_allows(tmp_path, monkeypatch):
    """No regression: an aged, never-audited app audits normally when the
    budget allows."""
    now = datetime.now(timezone.utc)
    workspace = tmp_path / "ws"
    _write_manifest(workspace, "aged-app", {
        "created_at": (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    dispatched: list[str] = []

    def fake_dispatch(*, manifest, **kwargs):
        dispatched.append(manifest.get("id"))
        return _FakeAuditOutput()

    monkeypatch.setattr(app_audit_runner, "run_tier3_for_app", fake_dispatch)
    monkeypatch.setattr(
        pb, "evaluate",
        lambda *a, **k: pb.ProvisioningBudgetDecision(allowed=True, ceiling_usd=12.0),
    )

    result = app_audit_runner.run_tier3(
        workspace, bot_id="ledger", shared_dir=tmp_path / "shared",
    )
    assert "aged-app" in dispatched
    assert result["apps_audited"] == 1
    assert result["apps_first_audit_deferred"] == 0
