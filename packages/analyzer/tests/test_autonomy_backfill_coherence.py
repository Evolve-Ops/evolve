"""Tests for autonomy.backfill (observe-first, §5.2) + autonomy.coherence (§4.2.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autonomy import backfill as _backfill
from autonomy import catalog as _catalog
from autonomy import coherence as _coherence
from autonomy import renderer as _renderer
from autonomy import store as _store
from permissions import monitor as _mon


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _make_home(tmp_path: Path, *, deny: list[str] | None = None) -> Path:
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    cfg: dict = {
        "mcp": {"servers": {
            "google_workspace": {"command": "uvx", "args": ["workspace-mcp"]},
        }},
    }
    if deny is not None:
        cfg["tools"] = {"deny": deny}
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps(cfg))
    return h


# ── Backfill ─────────────────────────────────────────────────────────────────

def test_backfill_infers_wider_and_fires_suggestion(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)  # send reachable
    findings = _backfill.ensure_backfilled(
        shared_dir, "alpha", home_override=home,
    )
    doc = _store.load(shared_dir, "alpha")
    p = doc.integrations["google_workspace"]
    assert p.rung == "act_with_approval"
    assert p.set_by["actor"] == _store.ACTOR_BACKFILL

    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "autonomy_backfill_review"
    assert f["severity"] == "info"
    assert f["signature_scope"] == "alpha:google_workspace"
    assert f["details"]["inferred_rung"] == "act_with_approval"
    assert f["details"]["default_rung"] == "draft_only"
    # Never demote: the live config was not touched.
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert "tools" not in cfg or not (cfg.get("tools") or {}).get("deny")


def test_backfill_infers_draft_only_when_send_denied(
    shared_dir: Path, tmp_path: Path,
):
    home = _make_home(
        tmp_path, deny=["mcp__google_workspace__send_gmail_message"],
    )
    findings = _backfill.ensure_backfilled(
        shared_dir, "alpha", home_override=home,
    )
    doc = _store.load(shared_dir, "alpha")
    assert doc.integrations["google_workspace"].rung == "draft_only"
    assert findings == []  # not wider than default — nothing to review


def test_backfill_idempotent_and_respects_deliberate(
    shared_dir: Path, tmp_path: Path,
):
    home = _make_home(tmp_path)
    _backfill.ensure_backfilled(shared_dir, "alpha", home_override=home)
    first = _store.load(shared_dir, "alpha").integrations["google_workspace"]
    _backfill.ensure_backfilled(shared_dir, "alpha", home_override=home)
    second = _store.load(shared_dir, "alpha").integrations["google_workspace"]
    assert first.set_at == second.set_at
    assert len(second.history) == 1

    # Operator confirms → finding clears (set_by no longer backfill).
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="act_with_approval", actor="operator_ui",
    )
    assert _backfill.ensure_backfilled(
        shared_dir, "alpha", home_override=home,
    ) == []


# ── Plugin-provided Gmail (Atlas shape: gmail_* in alsoAllow, no mcp.servers) ─

def _make_plugin_home(tmp_path: Path, *, deny: list[str] | None = None) -> Path:
    """A bot whose Gmail comes from the plugin tool surface, not an MCP
    server — mirrors the live Atlas openclaw.json (mcp.servers empty)."""
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    cfg: dict = {
        "mcp": {"servers": {}},
        "tools": {"alsoAllow": [
            "gmail_list_messages", "gmail_get_message", "gmail_send",
            "gmail_label_message", "gmail_archive_message", "gmail_mark_read",
            "gmail_delete_message",
        ]},
    }
    if deny is not None:
        cfg["tools"]["deny"] = deny
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps(cfg))
    return h


def test_plugin_backfill_mints_email_posture_and_fires_review(
    shared_dir: Path, tmp_path: Path,
):
    home = _make_plugin_home(tmp_path)  # gmail_send reachable, no deny
    findings = _backfill.ensure_backfilled(shared_dir, "atlas", home_override=home)

    doc = _store.load(shared_dir, "atlas")
    iid = _catalog.PLUGIN_GMAIL_INTEGRATION_ID
    p = doc.integrations[iid]
    assert p.kind == "email"
    assert p.rung == "act_with_approval"   # gmail_send reachable ⇒ inferred wider
    assert p.set_by["actor"] == _store.ACTOR_BACKFILL

    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "autonomy_backfill_review"
    assert f["severity"] == "info"
    assert f["signature_scope"] == f"atlas:{iid}"
    assert f["details"]["inferred_rung"] == "act_with_approval"
    assert f["details"]["default_rung"] == "draft_only"
    # Label is identical to the MCP Google Workspace surface.
    assert "Google Workspace" in f["details"]["integration_label"]
    # Never demote: live config untouched.
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert not (cfg.get("tools") or {}).get("deny")


def test_plugin_backfill_draft_only_when_send_denied(
    shared_dir: Path, tmp_path: Path,
):
    home = _make_plugin_home(tmp_path, deny=["gmail_send", "gmail_delete_message"])
    findings = _backfill.ensure_backfilled(shared_dir, "atlas", home_override=home)
    iid = _catalog.PLUGIN_GMAIL_INTEGRATION_ID
    assert _store.load(shared_dir, "atlas").integrations[iid].rung == "draft_only"
    assert findings == []  # not wider than default


def test_plugin_render_denies_send_delete_then_releases_send(
    shared_dir: Path, tmp_path: Path,
):
    home = _make_plugin_home(tmp_path)
    iid = _catalog.PLUGIN_GMAIL_INTEGRATION_ID

    # draft_only: send AND delete denied by bare name; reads reachable.
    _store.set_posture(
        shared_dir, "atlas", iid,
        rung="draft_only", actor="operator_ui", kind="email",
    )
    _renderer.render_bot("atlas", shared_dir, home_override=home)
    deny = json.loads((home / ".openclaw" / "openclaw.json").read_text())["tools"]["deny"]
    assert "gmail_send" in deny
    assert "gmail_delete_message" in deny
    assert "gmail_list_messages" not in deny
    assert "gmail_get_message" not in deny
    # alsoAllow wiring untouched — only deny changed.
    allow = json.loads((home / ".openclaw" / "openclaw.json").read_text())["tools"]["alsoAllow"]
    assert "gmail_send" in allow

    # Promote to "Asks first": send reachable, delete still denied.
    _store.set_posture(
        shared_dir, "atlas", iid,
        rung="act_with_approval", actor="operator_ui", kind="email",
    )
    _renderer.render_bot("atlas", shared_dir, home_override=home)
    deny = json.loads((home / ".openclaw" / "openclaw.json").read_text())["tools"]["deny"]
    assert "gmail_send" not in deny
    assert "gmail_delete_message" in deny

    # Coherence agrees with the render.
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert _coherence.check_bot("atlas", shared_dir, home_override=home) == []


def test_plugin_backfilled_is_observe_only(shared_dir: Path, tmp_path: Path):
    home = _make_plugin_home(tmp_path)
    _backfill.ensure_backfilled(shared_dir, "atlas", home_override=home)
    # Unrendered by design (observe-first) — no deny written, no drift.
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert not (cfg.get("tools") or {}).get("deny")
    assert _coherence.check_bot("atlas", shared_dir, home_override=home) == []


def test_backfill_ignores_unknown_servers(shared_dir: Path, tmp_path: Path):
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "mcp": {"servers": {"github": {"command": "npx"}}},
    }))
    assert _backfill.ensure_backfilled(shared_dir, "alpha", home_override=h) == []
    assert _store.load(shared_dir, "alpha") is None


# ── Coherence ────────────────────────────────────────────────────────────────

def test_coherent_after_render(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    _renderer.render_bot("alpha", shared_dir, home_override=home)
    assert _coherence.check_bot("alpha", shared_dir, home_override=home) == []


def test_drift_on_missing_deny_entry(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    _renderer.render_bot("alpha", shared_dir, home_override=home)

    # Out-of-band edit: someone removed the send deny.
    oc = home / ".openclaw" / "openclaw.json"
    cfg = json.loads(oc.read_text())
    cfg["tools"]["deny"] = [
        e for e in cfg["tools"]["deny"] if "send" not in e
    ]
    oc.write_text(json.dumps(cfg))

    findings = _coherence.check_bot("alpha", shared_dir, home_override=home)
    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "autonomy_posture_drift"
    assert f["severity"] == "warn"
    assert f["signature_scope"] == "alpha:google_workspace"
    assert "mcp__google_workspace__send_gmail_message" in (
        f["details"]["missing_deny_entries"]
    )


def test_drift_on_unexpected_owned_entry(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="act_with_approval", actor="operator_ui",
    )
    _renderer.render_bot("alpha", shared_dir, home_override=home)

    oc = home / ".openclaw" / "openclaw.json"
    cfg = json.loads(oc.read_text())
    cfg.setdefault("tools", {}).setdefault("deny", []).append(
        "mcp__google_workspace__search_gmail_messages"
    )
    oc.write_text(json.dumps(cfg))

    findings = _coherence.check_bot("alpha", shared_dir, home_override=home)
    assert len(findings) == 1
    assert findings[0]["details"]["unexpected_deny_entries"] == [
        "mcp__google_workspace__search_gmail_messages"
    ]


def test_no_drift_for_backfilled_observe_only(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)
    _backfill.ensure_backfilled(shared_dir, "alpha", home_override=home)
    # Unrendered by design — must NOT read as drift.
    assert _coherence.check_bot("alpha", shared_dir, home_override=home) == []


def test_malformed_posture_file_is_a_finding(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)
    path = _store.autonomy_path(shared_dir, "alpha")
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    findings = _coherence.check_bot("alpha", shared_dir, home_override=home)
    assert len(findings) == 1
    assert findings[0]["type"] == "autonomy_posture_drift"
    assert findings[0]["signature_scope"] == "alpha:posture_file"


# ── Monitor registration ─────────────────────────────────────────────────────

def test_monitor_owns_autonomy_signal_types():
    assert "autonomy_posture_drift" in _mon.OWNED_SIGNAL_TYPES
    assert "autonomy_backfill_review" in _mon.OWNED_SIGNAL_TYPES
    assert "autonomy_posture_drift" in _mon._OPERATOR_DOCS
    assert "autonomy_backfill_review" in _mon._OPERATOR_DOCS
