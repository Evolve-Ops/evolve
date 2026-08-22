"""Tests for the evo app-changes / app-coherence / app-scan handlers.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.4.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
# schema + arbiter live under packages/analyzer/ — the flag handler
# imports them lazily; tests need them on the path so the lazy import
# resolves.
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))


_NETWORK = {"pod": {"shared_dir": "/tmp/test-shared"}}


def _handler_module():
    """Lazy import — keeps evo modules out of the collection-phase sys.modules."""
    from evolve_admin.evo.handlers import app_changes
    return app_changes


def _write_manifest(workspace_root: Path, bot_id: str,
                     manifest: dict) -> Path:
    """Write a manifest in the per-bot workspace layout.

    applications_dir(shared_dir, bot_id) returns
    /Users/<bot>/.openclaw/workspace/manifests — per-bot, not under
    shared_dir. Tests stub get_bot_workspace to return workspace_root,
    so manifests live at workspace_root / "manifests" / "<app>.json".
    """
    mdir = workspace_root / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    app_id = manifest.get("id") or "app1"
    p = mdir / f"{app_id}.json"
    p.write_text(json.dumps(manifest))
    return p


@pytest.fixture()
def shared_dir(tmp_path, monkeypatch):
    """Stub get_bot_workspace so manifests live under tmp_path."""
    workspace_root = tmp_path / "bot-workspace"
    workspace_root.mkdir()

    def _stub_workspace(bot_id: str):
        return workspace_root

    # Patch the helper that applications_dir uses.
    import evolve_admin.config as _config
    monkeypatch.setattr(_config, "get_bot_workspace", _stub_workspace)
    # Also patch in the manifest module's namespace if it re-imported.
    try:
        from evolve_admin.applications import manifest as _mf
        monkeypatch.setattr(
            "evolve_admin.applications.manifest.get_bot_workspace",
            _stub_workspace,
            raising=False,
        )
    except Exception:
        pass

    # Yield the WORKSPACE root (not tmp_path) so _write_manifest uses
    # the same path applications_dir resolves to.
    yield workspace_root


def _network_for(shared_dir: Path) -> dict:
    return {"pod": {"shared_dir": str(shared_dir)}}


# ── app-changes: list ──────────────────────────────────────────────

def test_list_changes_empty_state(shared_dir):
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x", args="",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "No applications" in body or "no changes" in body.lower()


def test_list_changes_shows_authored_drift(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "myapp",
        "reconciliation": {
            "drifted_fields": [
                {"field": "description", "authored": True,
                 "before": "old", "after": "new"},
            ],
        },
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x", args="",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "myapp" in body
    assert "drifted" in body


def test_list_changes_shows_quiet_failure(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "myapp",
        "coherence": {"findings": [{
            "id": "C-A1", "severity": "minor",
            "assertion": "recurring_behavior_only_suspect_actions",
            "description": "X", "evidence": [],
        }]},
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x", args="",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "myapp" in body
    assert "quiet failure" in body


# ── app-changes: show ─────────────────────────────────────────────

def test_show_unknown_app_reports_not_found(shared_dir):
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x",
                             args="missing-app",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "missing-app" in body
    assert "No app" in body or "not found" in body.lower()


def test_show_app_with_quiet_failure(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "coherence": {"findings": [{
            "id": "C-A1", "severity": "critical",
            "assertion": "recurring_behavior_without_trigger",
            "description": "no triggers configured", "evidence": [],
        }]},
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x", args="j",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "j:" in body
    assert "no triggers configured" in body


def test_show_app_with_additions(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "reconciliation": {
            "added_files": [
                {"path": "scripts/new.py", "layer": "code"},
            ],
        },
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x", args="j",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "scripts/new.py" in body


# ── app-changes: auth gate on mutating forms ────────────────────────

def test_team_member_cannot_approve(shared_dir):
    """Mutating forms gated to primary + admin (spec §10.9.7)."""
    _write_manifest(shared_dir, "bot-x", {"id": "j"})
    handler = _handler_module()
    result = handler.render(role="secondary", bot_id="bot-x",
                             args="j approve",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "primary user" in body or "operator" in body


def test_team_member_can_show(shared_dir):
    """Read-only forms are open to team members."""
    _write_manifest(shared_dir, "bot-x", {"id": "j"})
    handler = _handler_module()
    result = handler.render(role="secondary", bot_id="bot-x",
                             args="j",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    # No "not for you" — proceeded to render.
    assert "primary user" not in body or "j" in body


# ── app-changes: approve / promote / flag ──────────────────────────

def test_approve_succeeds_with_count(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "reconciliation": {
            "added_files": [{"path": "a"}],
            "drifted_fields": [{"field": "x"}],
        },
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x",
                             args="j approve",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "approved 2 change" in body


def test_approve_actually_clears_reconciliation(shared_dir):
    """Spec §10.4: approve mutates manifest.reconciliation to empty."""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "reconciliation": {
            "added_files": [{"path": "a"}],
            "removed_files": [{"path": "old.py"}],
            "drifted_fields": [{"field": "x"}],
        },
    })
    handler = _handler_module()
    handler.render(role="primary", bot_id="bot-x",
                    args="j approve",
                    network=_network_for(shared_dir))
    # Re-read the manifest from disk.
    on_disk = json.loads(
        (shared_dir / "manifests" / "j.json").read_text()
    )
    rec = on_disk["reconciliation"]
    assert rec["added_files"] == []
    assert rec["removed_files"] == []
    assert rec["drifted_fields"] == []
    assert rec.get("status") == "ok"
    assert "last_approved_at" in rec


def test_approve_preserves_orphan_status(shared_dir):
    """If an app is confirmed orphan, approving drift doesn't override
    that — orphan is a separate concept."""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "reconciliation": {
            "status": "orphan",
            "drifted_fields": [{"field": "x"}],
        },
    })
    handler = _handler_module()
    handler.render(role="primary", bot_id="bot-x",
                    args="j approve",
                    network=_network_for(shared_dir))
    on_disk = json.loads(
        (shared_dir / "manifests" / "j.json").read_text()
    )
    assert on_disk["reconciliation"]["status"] == "orphan"


def test_approve_zero_changes_no_op(shared_dir):
    _write_manifest(shared_dir, "bot-x", {"id": "j"})
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x",
                             args="j approve",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "nothing to approve" in body.lower()


def test_promote_lists_observational_fields(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "provenance": {"field_origins": {
            "description": {"source": "observational"},
            "files": {"source": "user_authored"},
        }},
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x",
                             args="j promote",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "promoted 1 field" in body


def test_promote_actually_flips_field_origins(shared_dir):
    """Spec §10.4: promote mutates provenance.field_origins.<X>.source."""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "provenance": {"field_origins": {
            "description": {"source": "observational"},
            "tags": {"source": "observational"},
            "files": {"source": "user_authored"},
        }},
    })
    handler = _handler_module()
    handler.render(role="primary", bot_id="bot-x",
                    args="j promote",
                    network=_network_for(shared_dir))
    on_disk = json.loads(
        (shared_dir / "manifests" / "j.json").read_text()
    )
    origins = on_disk["provenance"]["field_origins"]
    # Both observational fields flipped to bot_authored.
    assert origins["description"]["source"] == "bot_authored"
    assert origins["tags"]["source"] == "bot_authored"
    # The user_authored field untouched.
    assert origins["files"]["source"] == "user_authored"
    # by/via stamped.
    assert "evo" in origins["description"].get("by", "")


def test_promote_with_no_observational_fields_no_op(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "provenance": {"field_origins": {
            "description": {"source": "user_authored"},
        }},
    })
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x",
                             args="j promote",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "nothing to promote" in body.lower()


def test_flag_records_description(shared_dir):
    _write_manifest(shared_dir, "bot-x", {"id": "j"})
    handler = _handler_module()
    result = handler.render(role="primary", bot_id="bot-x",
                             args="j flag the 6pm cron is missing",
                             network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "6pm cron is missing" in body
    assert "operator" in body


def test_flag_writes_proposal_to_arbiter_store(shared_dir):
    """`evo app-changes <app> flag <desc>` lands a Proposal under
    {shared_dir}/proposals/pending/<id>.json so the operator queue
    surfaces the user's concern."""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "coherence": {"findings": [{
            "id": "C-A1", "severity": "minor",
            "assertion": "recurring_behavior_only_suspect_actions",
            "description": "no triggers", "evidence": [],
        }]},
    })
    handler = _handler_module()
    handler.render(
        role="primary", bot_id="bot-x",
        args="j flag the 6pm cron is missing",
        network=_network_for(shared_dir),
    )
    pending_dir = shared_dir / "proposals" / "pending"
    files = list(pending_dir.glob("*.json")) if pending_dir.exists() else []
    assert len(files) == 1, f"expected 1 pending proposal, found {len(files)}"
    proposal = json.loads(files[0].read_text())
    assert proposal["bot_id"] == "bot-x"
    assert proposal["generator_id"] == "evo_user_flag"
    assert proposal["status"] == "pending"
    assert proposal["approval_audience"] == "pod_operator"
    assert proposal["action"]["kind"] == "Investigation"
    # User's description verbatim in the action context.
    assert "the 6pm cron is missing" in proposal["action"]["context"]
    # app_id surfaced in provenance.signals so the queue can group by app.
    assert proposal["provenance"]["signals"]["app_id"] == "j"
    # Manifest context (findings) reflected in the body.
    assert "coherence finding" in proposal["action"]["context"]


def test_flag_proposal_id_in_response(shared_dir):
    """The acknowledgement message must include the Proposal id so the
    user sees confirmation and the operator can correlate."""
    _write_manifest(shared_dir, "bot-x", {"id": "j"})
    handler = _handler_module()
    result = handler.render(
        role="primary", bot_id="bot-x",
        args="j flag something is off",
        network=_network_for(shared_dir),
    )
    body = result.direct_send_message
    pending_dir = shared_dir / "proposals" / "pending"
    files = list(pending_dir.glob("*.json"))
    assert len(files) == 1
    proposal_id = json.loads(files[0].read_text())["id"]
    assert proposal_id in body
    assert "Proposal" in body


# ── app-coherence ─────────────────────────────────────────────────

def test_coherence_no_app_errors(shared_dir):
    handler = _handler_module()
    result = handler.render_coherence(role="primary", bot_id="bot-x",
                                        args="",
                                        network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "Usage" in body


def test_coherence_dispatches_c3_when_no_cache(shared_dir, monkeypatch):
    """When no C3 verdict is cached, the handler runs the C3 dispatcher
    in-process so the next render carries a fresh verdict. (Until
    2026-06-10 this went through the daemon's own peer-authed unix-socket
    route — a self-call that stopped passing once the socket no longer
    trusted the daemon's own uid; roadmap 2.11.)"""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "coherence": {"findings": [], "status": "ok"},
    })

    dispatch_calls: list[dict] = []

    class _Result:
        @staticmethod
        def to_dict():
            return {"ok": True}

    def _stub_dispatch_c3(*, bot_id, app_id, trigger, shared_dir, network):
        dispatch_calls.append({
            "bot_id": bot_id, "app_id": app_id, "trigger": trigger,
        })
        # Simulate the dispatcher writing the verdict to the manifest
        # before returning. The handler reloads the manifest after.
        from evolve_admin.evo.handlers import app_changes as _h
        m = _h._load_manifest_dict(shared_dir, "bot-x", "j")
        coherence = m.setdefault("coherence", {})
        coherence["last_capability_check"] = {
            "severity": "feasible",
            "rationale": "synthetic — looks good",
            "checked_at": "2026-06-07T00:00:00Z",
        }
        _write_manifest(shared_dir, "bot-x", m)
        return _Result()

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _stub_dispatch_c3,
    )
    handler = _handler_module()
    result = handler.render_coherence(
        role="primary", bot_id="bot-x", args="j",
        network=_network_for(shared_dir),
    )
    body = result.direct_send_message
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["trigger"] == "on_demand"
    assert dispatch_calls[0]["app_id"] == "j"
    assert dispatch_calls[0]["bot_id"] == "bot-x"
    # The freshly-written rationale is in the rendered body.
    assert "looks good" in body


def test_coherence_skips_dispatch_when_cached(shared_dir, monkeypatch):
    """Cached verdict already on the manifest → no dispatch attempted."""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "coherence": {
            "findings": [],
            "status": "ok",
            "last_capability_check": {
                "severity": "feasible",
                "rationale": "cached verdict",
                "checked_at": "2026-06-06T12:00:00Z",
            },
        },
    })

    def _boom(*args, **kwargs):
        raise AssertionError("C3 must not be dispatched when cache is fresh")

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3", _boom
    )
    handler = _handler_module()
    result = handler.render_coherence(
        role="primary", bot_id="bot-x", args="j",
        network=_network_for(shared_dir),
    )
    assert "cached verdict" in result.direct_send_message


def test_coherence_secondary_role_does_not_dispatch(shared_dir, monkeypatch):
    """Team members get a read-only render — no LLM dispatch on their
    behalf (avoids unauthenticated cost-bearing calls)."""
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "coherence": {"findings": [], "status": "ok"},
    })

    def _boom(*args, **kwargs):
        raise AssertionError("daemon must not be called for secondary role")

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", _boom
    )
    handler = _handler_module()
    # No exception → daemon stayed quiet.
    handler.render_coherence(
        role="secondary", bot_id="bot-x", args="j",
        network=_network_for(shared_dir),
    )


def test_coherence_reads_cached_results(shared_dir):
    _write_manifest(shared_dir, "bot-x", {
        "id": "j",
        "coherence": {
            "findings": [{"id": "C-A1", "severity": "critical",
                          "description": "no triggers", "evidence": []}],
            "status": "incoherent",
            "last_capability_check": {
                "severity": "incoherent",
                "rationale": "manifest contradicts itself",
            },
        },
    })
    handler = _handler_module()
    result = handler.render_coherence(role="primary", bot_id="bot-x",
                                        args="j",
                                        network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "Pass A" in body
    assert "incoherent" in body
    assert "manifest contradicts itself" in body


# ── app-scan ─────────────────────────────────────────────────────

def test_scan_acknowledges_start(shared_dir, monkeypatch):
    """When the daemon dispatches successfully, the user sees the
    started-acknowledge message."""
    def _fake_daemon_call(method, path, body=None):
        return True, 200, {"status": "ok"}
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        _fake_daemon_call,
    )
    handler = _handler_module()
    result = handler.render_scan(role="primary", bot_id="bot-x",
                                  args="", network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "bot-x" in body
    assert "Scan" in body


def test_scan_already_running_reported(shared_dir, monkeypatch):
    """When a scan is already in flight, the user is told (rather than
    getting a misleading 'started' message)."""
    def _fake_daemon_call(method, path, body=None):
        return True, 200, {"status": "already_running"}
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        _fake_daemon_call,
    )
    handler = _handler_module()
    result = handler.render_scan(role="primary", bot_id="bot-x",
                                  args="", network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "already running" in body.lower()


def test_scan_daemon_rejects_surfaces_error(shared_dir, monkeypatch):
    """When the daemon returns non-200, the operator sees the failure
    rather than a misleading success."""
    def _fake_daemon_call(method, path, body=None):
        return True, 400, {"error": "bot required"}
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        _fake_daemon_call,
    )
    handler = _handler_module()
    result = handler.render_scan(role="primary", bot_id="bot-x",
                                  args="", network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "400" in body or "failed" in body.lower()


def test_scan_daemon_unreachable_gives_hint(shared_dir, monkeypatch):
    """Fallback path — daemon unreachable returns acknowledgement
    plus a hint about the admin UI."""
    def _fake_daemon_call(method, path, body=None):
        return False, None, None
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        _fake_daemon_call,
    )
    handler = _handler_module()
    result = handler.render_scan(role="primary", bot_id="bot-x",
                                  args="", network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "admin UI" in body or "may not have actually started" in body


def test_scan_secondary_blocked(shared_dir):
    handler = _handler_module()
    result = handler.render_scan(role="secondary", bot_id="bot-x",
                                  args="", network=_network_for(shared_dir))
    body = result.direct_send_message
    assert "primary user" in body or "operator" in body
