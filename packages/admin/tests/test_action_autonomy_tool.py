"""tests/test_action_autonomy_tool.py — action.autonomy.set.

The §3.1 chat front door (autonomy ladder Phase B). Coverage: registry
shape, the confirm-before-call tier override (promotions always
requires_confirmation, demotions never), validation gates against the
catalog, the daemon-first routing payload (actor=primary_bot + CAS
witness), and the in-process fallback path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

BOT = "alpha"
IID = "google_workspace"


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def offline_daemon(monkeypatch):
    """Force the in-process fallback path (daemon unreachable)."""
    from evolve_admin.evo import tools as _tools_pkg  # noqa: F401
    import evolve_admin.evo.tools.action_autonomy as mod

    calls: list[tuple] = []

    def _fake(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return (False, None, None)

    from evolve_admin.evo import admin_client
    monkeypatch.setattr(admin_client, "try_daemon_call", _fake)
    return calls


def _set_rung2(shared_dir: Path) -> None:
    from autonomy import store as _store
    _store.set_posture(
        shared_dir, BOT, IID, rung="act_with_approval",
        actor=_store.ACTOR_OPERATOR_UI,
    )


# ─── Registry shape ──────────────────────────────────────────────────────────


def test_tool_registered():
    from evolve_admin.evo.tools import lookup

    t = lookup("action.autonomy.set")
    assert t is not None
    assert t.risk_tier.value == "write_risky"
    assert t.validate is not None
    assert t.authorization_scope == "admin"


# ─── Validate: confirm-before-call ───────────────────────────────────────────


def test_validate_promotion_requires_confirmation(shared_dir, offline_daemon):
    from evolve_admin.evo.tools.action_autonomy import _set_validate

    _set_rung2(shared_dir)
    r = _set_validate(
        shared_dir, BOT, IID,
        rung="autonomous_within_rules",
        rules={"actions_per_day": 5},
    )
    assert r["ok"] is True
    assert r["requires_confirmation"] is True
    assert r["context"]["current_level"] == "Asks first"
    assert r["context"]["target_level"] == "Acts within limits"
    # The CAS witness the handler requires travels via context.
    assert r["context"]["current_rung"] == "act_with_approval"
    # The staged button carries the same consequence copy the UI uses.
    assert "without asking" in r["context"]["consequence"]


def test_validate_demotion_no_confirmation(shared_dir, offline_daemon):
    from evolve_admin.evo.tools.action_autonomy import _set_validate

    _set_rung2(shared_dir)
    r = _set_validate(shared_dir, BOT, IID, rung="draft_only")
    assert r["ok"] is True
    assert r["requires_confirmation"] is False


def test_validate_gates(shared_dir, offline_daemon):
    from evolve_admin.evo.tools.action_autonomy import _set_validate

    _set_rung2(shared_dir)
    # Unknown rung.
    assert _set_validate(shared_dir, BOT, IID, rung="yolo")["ok"] is False
    # Rung 3 without rules — catalog validation surfaces the error.
    r = _set_validate(shared_dir, BOT, IID, rung="autonomous_within_rules")
    assert r["ok"] is False and "rules" in r["reason"]
    # No-op (already at the level).
    assert _set_validate(
        shared_dir, BOT, IID, rung="act_with_approval",
    )["ok"] is False
    # Unmanaged integration.
    assert _set_validate(
        shared_dir, BOT, "nope", rung="draft_only",
    )["ok"] is False
    # Stale expected_current_rung.
    r = _set_validate(
        shared_dir, BOT, IID, rung="draft_only",
        expected_current_rung="autonomous_within_rules",
    )
    assert r["ok"] is False and "re-check" in r["reason"]


# ─── Handler: daemon-first payload ───────────────────────────────────────────


def test_handler_posts_primary_bot_actor_and_cas(shared_dir, monkeypatch):
    import evolve_admin.evo.tools.action_autonomy as mod  # noqa: F401
    from evolve_admin.evo import admin_client
    from evolve_admin.evo.tools.action_autonomy import _set_handler

    _set_rung2(shared_dir)
    posts: list[tuple] = []

    def _fake(method, path, body=None, **kwargs):
        if method == "GET":
            return (True, 200, {"bots": {BOT: {"integrations": [
                {"integration_id": IID, "rung": "act_with_approval"},
            ]}}})
        posts.append((method, path, body))
        return (True, 200, {"ok": True, "rung": body["rung"],
                            "render": {"changed": True, "written": True}})

    monkeypatch.setattr(admin_client, "try_daemon_call", _fake)
    out = _set_handler(
        shared_dir, BOT, IID,
        rung="autonomous_within_rules",
        rules={"actions_per_day": 5},
        expected_current_rung="act_with_approval",
        reason="operator confirmed in chat",
    )
    assert out["ok"] is True
    assert out["via"] == "admin_daemon"
    method, path, body = posts[0]
    assert (method, path) == ("POST", f"/api/autonomy/{BOT}/{IID}")
    assert body["actor"] == "primary_bot"
    assert body["expected_current_rung"] == "act_with_approval"
    assert body["note"] == "operator confirmed in chat"


def test_handler_requires_cas_witness(shared_dir, offline_daemon):
    # Resolving the witness at execution time would only guard the
    # handler's own read→write window — the confirmed transition must
    # be pinned from validate (second-pass review finding).
    from evolve_admin.evo.tools.action_autonomy import _set_handler

    _set_rung2(shared_dir)
    out = _set_handler(shared_dir, BOT, IID, rung="draft_only")
    assert out["ok"] is False
    assert "expected_current_rung" in out["error"]


def test_handler_fallback_writes_history_with_primary_bot(
    shared_dir, offline_daemon,
):
    from autonomy import store as _store
    from evolve_admin.evo.tools.action_autonomy import _set_handler

    _set_rung2(shared_dir)
    out = _set_handler(
        shared_dir, BOT, IID, rung="draft_only",
        expected_current_rung="act_with_approval",
    )
    assert out["ok"] is True
    assert out["via"] == "in_process_fallback"
    posture = _store.load(shared_dir, BOT).integrations[IID]
    assert posture.rung == "draft_only"
    assert posture.set_by["actor"] == "primary_bot"
    # Same history record the UI route writes (spec §3.1).
    assert posture.history[-1]["actor"] == "primary_bot"


def test_handler_stale_cas_fails_loudly(shared_dir, offline_daemon):
    from evolve_admin.evo.tools.action_autonomy import _set_handler

    _set_rung2(shared_dir)
    out = _set_handler(
        shared_dir, BOT, IID, rung="draft_only",
        expected_current_rung="autonomous_within_rules",
    )
    assert out["ok"] is False
    assert "stale" in out["error"]
