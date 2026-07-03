"""Tests for the Apps-page repair-chat backend.

Covers:

* proposal parsing extracts all five tool calls and drops malformed
  blocks without raising
* apply paths mutate the manifest via save_manifest_with_provenance with
  source=user_authored + via='repair_chat'
* per-(bot, app) daily rate limit blocks the 11th turn
* per-(bot, app) lock blocks a second operator from posting / applying
* dispatcher errors render a "bot is offline" message in the chat log
  without losing the operator's turn
* an applied "done" proposal releases the lock automatically
* the apply endpoint refuses payloads not previously logged (no
  arbitrary-write surface)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import repair_chat as rc  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    PROVENANCE_USER_AUTHORED,
    save_manifest,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    """A clean pod-shared dir for each test."""
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def fake_manifest(monkeypatch, tmp_path: Path):
    """Install a minimal manifest in a per-bot manifests dir + return
    (bot_id, app_id, manifest).

    The real ``applications_dir`` returns ``/Users/<bot>/.openclaw/...`` —
    we monkeypatch it to a tmp dir so save_manifest can actually write.
    """
    bot_id = "team-bot-c"
    app_id = "atlas-knowledge"
    workspace = tmp_path / "bot-workspace"
    manifests_dir = workspace / "manifests"
    manifests_dir.mkdir(parents=True)

    # Stub applications_dir so save_manifest writes under tmp_path.
    from evolve_admin.applications import manifest as _m

    def _fake_apps_dir(_shared_dir, _bot_id):
        return manifests_dir

    monkeypatch.setattr(_m, "applications_dir", _fake_apps_dir)
    # The C3 charter-change dispatch fires on user_authored saves —
    # neutralise it so tests don't try to spawn openclaw.
    monkeypatch.setattr(_m, "_maybe_dispatch_c3_after_editor_save",
                        lambda *a, **kw: None)
    # apps-inherit-bot-llm validator requires bot fields we don't bother
    # populating in the tiny fixture — bypass it.
    monkeypatch.setattr(
        "evolve_admin.applications.apps_inherit_bot_llm_validator.validate_apps_inherit_bot_llm",
        lambda _m: {"ok": True, "errors": [], "message": ""},
    )
    # Pre-deploy coherence gate would refuse partial fixture manifests —
    # repair_chat applies are operator-driven so the gate is the right
    # production behaviour, but for unit tests we skip it.
    monkeypatch.setattr(
        "evolve_admin.applications.manifest.validate_coherence_gate",
        lambda *a, **kw: None,
    )

    manifest = ApplicationManifest(
        id=app_id, name="Atlas Knowledge", bot_id=bot_id,
        description="Summarizes captured articles.",
        example_triggers=[],
        success_criteria={"observable_outcomes": []},
        known_issues=[],
    )
    save_manifest(manifest, tmp_path / "shared")
    return bot_id, app_id, manifest


# ── Proposal parsing ────────────────────────────────────────────────────────


def test_extract_proposals_handles_all_five_actions() -> None:
    text = """Hi! Here are a few proposals.

<<<repair_proposal action="propose_field_edit">>>
{"field": "example_triggers", "before": [], "after": ["each morning"], "rationale": "scheduled"}
<<<end>>>

Some chatter between.

<<<repair_proposal action="propose_file_edit">>>
{"path": "scripts/x.py", "summary": "add error handling", "rationale": "crashes"}
<<<end>>>

<<<repair_proposal action="propose_test_exemption">>>
{"reason": "no test harness for cron-driven apps yet"}
<<<end>>>

<<<repair_proposal action="mark_resolved">>>
{"signature": "abc123", "rationale": "operator confirmed this is intentional"}
<<<end>>>

<<<repair_proposal action="done">>>
{}
<<<end>>>
"""
    cleaned, proposals = rc.extract_proposals(text)
    actions = [p.action for p in proposals]
    assert actions == [
        "propose_field_edit", "propose_file_edit",
        "propose_test_exemption", "mark_resolved", "done",
    ]
    # Each proposal gets a server-assigned id.
    assert all(len(p.id) == 8 for p in proposals)
    # The tag blocks are stripped from the user-facing text.
    assert "<<<repair_proposal" not in cleaned
    assert "<<<end>>>" not in cleaned
    assert "Some chatter between." in cleaned
    # Payloads are parsed as dicts.
    field_payload = proposals[0].payload
    assert field_payload["field"] == "example_triggers"
    assert field_payload["after"] == ["each morning"]


def test_extract_proposals_drops_malformed_blocks() -> None:
    text = """First a good one.

<<<repair_proposal action="propose_field_edit">>>
{"field": "description", "after": "new desc"}
<<<end>>>

Now a malformed JSON:

<<<repair_proposal action="propose_field_edit">>>
{this isn't json}
<<<end>>>

And an unknown action:

<<<repair_proposal action="invent_anything">>>
{"foo": "bar"}
<<<end>>>
"""
    _, proposals = rc.extract_proposals(text)
    assert len(proposals) == 1
    assert proposals[0].action == "propose_field_edit"
    assert proposals[0].payload["field"] == "description"


def test_extract_proposals_handles_no_proposals() -> None:
    cleaned, proposals = rc.extract_proposals("Just chatting — no proposals here.")
    assert proposals == []
    assert "Just chatting" in cleaned


def test_extract_proposals_unterminated_block_does_not_crash() -> None:
    text = "<<<repair_proposal action=\"propose_field_edit\">>>\n{\"field\":\"x\""
    cleaned, proposals = rc.extract_proposals(text)
    # No close tag → leave the tail in the cleaned output for debugging.
    assert proposals == []
    assert "<<<repair_proposal" in cleaned


# ── Apply ───────────────────────────────────────────────────────────────────


def test_apply_field_edit_writes_with_user_authored_provenance(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    proposal = {
        "action": "propose_field_edit",
        "payload": {
            "field": "example_triggers",
            "before": [],
            "after": ["each morning"],
            "rationale": "scheduled",
        },
    }
    result = rc.apply_proposal(shared_dir, bot_id, app_id, proposal,
                               by="test:operator")
    assert result["ok"] is True
    assert result["top_field"] == "example_triggers"

    from evolve_admin.applications.manifest import load_manifest
    m = load_manifest(app_id, bot_id, shared_dir)
    assert m.example_triggers == ["each morning"]

    # Provenance stamp records the route.
    origins = (m.to_dict().get("provenance") or {}).get("field_origins") or {}
    et = origins.get("example_triggers") or {}
    assert et.get("source") == PROVENANCE_USER_AUTHORED
    assert et.get("via") == "repair_chat"
    assert et.get("by") == "test:operator"


def test_apply_field_edit_nested_dotted_path(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    proposal = {
        "action": "propose_field_edit",
        "payload": {
            "field": "success_criteria.observable_outcomes",
            "after": ["Atlas posts a daily summary"],
            "rationale": "matches usage",
        },
    }
    result = rc.apply_proposal(shared_dir, bot_id, app_id, proposal)
    assert result["ok"]
    # Top-level field (for provenance) is success_criteria, not the
    # dotted leaf.
    assert result["top_field"] == "success_criteria"
    from evolve_admin.applications.manifest import load_manifest
    m = load_manifest(app_id, bot_id, shared_dir)
    assert m.success_criteria == {
        "observable_outcomes": ["Atlas posts a daily summary"],
    }


def test_apply_mark_resolved_adds_to_coherence_accepted(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    proposal = {
        "action": "mark_resolved",
        "payload": {"signature": "deadbeef", "rationale": "intentional"},
    }
    result = rc.apply_proposal(shared_dir, bot_id, app_id, proposal,
                               by="test:operator")
    assert result["ok"]
    from evolve_admin.applications.manifest import load_manifest
    m = load_manifest(app_id, bot_id, shared_dir)
    accepted = (m.to_dict().get("coherence") or {}).get("coherence_accepted") or []
    sigs = [e.get("signature") for e in accepted]
    assert "deadbeef" in sigs
    # mute_finding stamps by= with the repair_chat: prefix so the trail
    # shows where the accept came from.
    entry = next(e for e in accepted if e.get("signature") == "deadbeef")
    assert (entry.get("accepted_by") or "").startswith("repair_chat:")


def test_apply_test_exemption_writes_reason(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    proposal = {
        "action": "propose_test_exemption",
        "payload": {"reason": "cron-driven app — no harness"},
    }
    result = rc.apply_proposal(shared_dir, bot_id, app_id, proposal)
    assert result["ok"]
    from evolve_admin.applications.manifest import load_manifest
    m = load_manifest(app_id, bot_id, shared_dir)
    md = m.to_dict()
    assert md.get("test_exemption_reason") == "cron-driven app — no harness"


def test_apply_file_edit_queues_as_known_issue(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    proposal = {
        "action": "propose_file_edit",
        "payload": {
            "path": "scripts/atlas.py",
            "summary": "wrap network call in try/except",
            "rationale": "crash on flaky DNS",
        },
    }
    result = rc.apply_proposal(shared_dir, bot_id, app_id, proposal)
    assert result["ok"] is True
    assert result["queued"] is True
    from evolve_admin.applications.manifest import load_manifest
    m = load_manifest(app_id, bot_id, shared_dir)
    issues = m.to_dict().get("known_issues") or []
    assert any("scripts/atlas.py" in i and "wrap network call" in i for i in issues)


def test_apply_done_is_noop_but_returns_ok(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    proposal = {"action": "done", "payload": {}}
    result = rc.apply_proposal(shared_dir, bot_id, app_id, proposal)
    assert result == {"ok": True, "action": "done"}


def test_apply_raises_on_unknown_action(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    with pytest.raises(rc.ApplyError):
        rc.apply_proposal(shared_dir, bot_id, app_id,
                          {"action": "drop_database", "payload": {}})


def test_apply_field_edit_requires_field_and_after(
    shared_dir: Path, fake_manifest,
) -> None:
    bot_id, app_id, _ = fake_manifest
    with pytest.raises(rc.ApplyError, match="field required"):
        rc.apply_proposal(shared_dir, bot_id, app_id, {
            "action": "propose_field_edit", "payload": {"after": "x"},
        })
    with pytest.raises(rc.ApplyError, match="after value required"):
        rc.apply_proposal(shared_dir, bot_id, app_id, {
            "action": "propose_field_edit", "payload": {"field": "description"},
        })


# ── Rate limit ──────────────────────────────────────────────────────────────


def test_rate_limit_blocks_after_cap(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    cap = 10
    # Bump up to the cap and verify the 11th attempt is blocked.
    for _ in range(cap):
        rc.bump_usage(shared_dir, bot_id, app_id)
    status = rc.cap_status(shared_dir, bot_id, app_id, daily_cap=cap)
    assert status["allowed"] is False
    assert status["used"] == cap
    assert status["remaining"] == 0


def test_rate_limit_resets_with_new_date(
    shared_dir: Path, monkeypatch,
) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    # Manually plant yesterday's usage to bypass time-travel.
    usage_path = shared_dir / "repair_chat" / bot_id / f"{app_id}.usage.json"
    usage_path.parent.mkdir(parents=True)
    usage_path.write_text(json.dumps({"date": "2020-01-01", "count": 99}))
    status = rc.cap_status(shared_dir, bot_id, app_id, daily_cap=10)
    # Stale usage resets — operator gets a fresh budget today.
    assert status["used"] == 0
    assert status["allowed"] is True


# ── Lock ────────────────────────────────────────────────────────────────────


def test_lock_blocks_second_operator(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    ok1, _ = rc.try_acquire_lock(shared_dir, bot_id, app_id, owner="op1")
    assert ok1 is True
    ok2, current = rc.try_acquire_lock(shared_dir, bot_id, app_id, owner="op2")
    assert ok2 is False
    assert current["owner"] == "op1"
    assert current["locked"] is True


def test_same_operator_can_reacquire(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    ok1, lock1 = rc.try_acquire_lock(shared_dir, bot_id, app_id, owner="op1")
    ok2, lock2 = rc.try_acquire_lock(shared_dir, bot_id, app_id, owner="op1")
    assert ok1 and ok2
    # started_at carries over so the UI can show "in session since X".
    assert lock2["started_at"] == lock1["started_at"]


def test_expired_lock_can_be_taken_over(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    # Plant an expired lock directly.
    lock_path = shared_dir / "repair_chat" / bot_id / f"{app_id}.lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({
        "owner": "op1",
        "session_id": "abc",
        "started_at": "2020-01-01T00:00:00Z",
        "expires_at": "2020-01-01T00:30:00Z",
    }))
    ok, lock = rc.try_acquire_lock(shared_dir, bot_id, app_id, owner="op2")
    assert ok is True
    assert lock["owner"] == "op2"


def test_release_lock_only_by_owner(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    rc.try_acquire_lock(shared_dir, bot_id, app_id, owner="op1")
    assert rc.release_lock(shared_dir, bot_id, app_id, owner="op2") is False
    assert rc.release_lock(shared_dir, bot_id, app_id, owner="op1") is True
    assert rc.lock_status(shared_dir, bot_id, app_id)["locked"] is False


# ── Chat log ────────────────────────────────────────────────────────────────


def test_chat_log_append_and_read(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    rc.append_chat_log(shared_dir, bot_id, app_id,
                       {"role": "user", "text": "hi"})
    rc.append_chat_log(shared_dir, bot_id, app_id,
                       {"role": "assistant", "text": "hello", "proposals": []})
    log = rc.read_chat_log(shared_dir, bot_id, app_id)
    assert [e["role"] for e in log] == ["user", "assistant"]
    assert all("ts" in e for e in log)


def test_find_proposal_by_id_walks_log(shared_dir: Path) -> None:
    bot_id, app_id = "team-bot-c", "atlas-knowledge"
    rc.append_chat_log(shared_dir, bot_id, app_id, {
        "role": "assistant", "text": "see proposal",
        "proposals": [
            {"id": "deadbeef", "action": "propose_field_edit",
             "payload": {"field": "description", "after": "x"}},
        ],
    })
    found = rc.find_proposal_by_id(shared_dir, bot_id, app_id, "deadbeef")
    assert found is not None
    assert found["action"] == "propose_field_edit"
    assert rc.find_proposal_by_id(shared_dir, bot_id, app_id, "ffffffff") is None


# ── Dispatch ────────────────────────────────────────────────────────────────


def _fake_dispatch_success(reply: str):
    """Return a dispatcher whose return value matches the dict branch of
    ChatTurnResult's normaliser.

    We deliberately stay decoupled from ``analyzer.app_audit_tier3`` —
    the real dispatcher is a dataclass with ``text/tokens/cost_usd/
    error/timed_out`` attributes, and ``dispatch_chat_turn`` also
    accepts a dict with the same keys. Tests use the dict form so the
    admin-side test suite doesn't need analyzer on sys.path.
    """
    def _fn(system_prompt, user_message, *, timeout_s, bot_id, shared_dir):
        return {
            "text": reply, "tokens": 1000, "cost_usd": 0.02,
            "error": "", "timed_out": False,
        }
    return _fn


def _fake_dispatch_failure(error: str = "openclaw binary not found"):
    def _fn(system_prompt, user_message, *, timeout_s, bot_id, shared_dir):
        return {
            "text": "", "tokens": 0, "cost_usd": 0.0,
            "error": error, "timed_out": False,
        }
    return _fn


def test_dispatch_chat_turn_parses_reply_and_proposals(
    shared_dir: Path,
) -> None:
    reply = """Hi — I see one finding. Want me to draft observable_outcomes?

<<<repair_proposal action="propose_field_edit">>>
{"field": "success_criteria.observable_outcomes", "after": ["x"], "rationale": "draft"}
<<<end>>>
"""
    result = rc.dispatch_chat_turn(
        shared_dir=shared_dir, bot_id="team-bot-c", app_id="atlas",
        app_name="Atlas", manifest_dict={"id": "atlas", "name": "Atlas"},
        history=[], latest_message="hi",
        dispatcher=_fake_dispatch_success(reply),
    )
    assert result.ok is True
    assert "Want me to draft" in result.reply
    assert "<<<repair_proposal" not in result.reply
    assert len(result.proposals) == 1
    assert result.proposals[0]["action"] == "propose_field_edit"
    assert result.cost_usd == 0.02


def test_dispatch_chat_turn_surfaces_dispatcher_error(
    shared_dir: Path,
) -> None:
    result = rc.dispatch_chat_turn(
        shared_dir=shared_dir, bot_id="team-bot-c", app_id="atlas",
        app_name="Atlas", manifest_dict={"id": "atlas"},
        history=[], latest_message="hi",
        dispatcher=_fake_dispatch_failure("openclaw binary not found"),
    )
    assert result.ok is False
    assert "openclaw binary not found" in result.error
    assert result.proposals == []


def test_dispatch_chat_turn_dispatcher_exception_does_not_propagate(
    shared_dir: Path,
) -> None:
    def _boom(*a, **kw):
        raise RuntimeError("subprocess died")
    result = rc.dispatch_chat_turn(
        shared_dir=shared_dir, bot_id="team-bot-c", app_id="atlas",
        app_name="Atlas", manifest_dict={"id": "atlas"},
        history=[], latest_message="hi",
        dispatcher=_boom,
    )
    assert result.ok is False
    assert "subprocess died" in result.error


# ── Prompt construction ────────────────────────────────────────────────────


def test_build_system_prompt_includes_findings_and_manifest() -> None:
    manifest = {
        "id": "atlas", "name": "Atlas",
        "description": "Summarises stuff.",
        "coherence": {
            "findings": [
                {"id": "C-A-1", "severity": "major",
                 "assertion": "missing_success_criteria",
                 "description": "observable_outcomes is empty",
                 "signature": "abcd1234"},
            ],
            "coherence_accepted": [],
        },
    }
    prompt = rc.build_system_prompt(
        bot_id="team-bot-c", app_id="atlas", app_name="Atlas",
        manifest_dict=manifest,
    )
    assert "team-bot-c" in prompt
    assert "Atlas" in prompt
    assert "observable_outcomes is empty" in prompt
    assert "abcd1234"[:16] in prompt
    assert "propose_field_edit" in prompt
    # Heavy fields would be stripped — confirm a non-heavy field still rides.
    assert "Summarises stuff." in prompt


def test_trim_drops_heavy_fields() -> None:
    manifest = {
        "id": "x", "files": [{"path": f"f{i}.py"} for i in range(50)],
        "volatile_paths": [{"glob": "**/*.log"}],
        "observation_log": ["a", "b"], "improvement_history": ["c"],
        "coherence": {"findings": [], "coherence_accepted": [{"signature": "s"}]},
        "description": "stays",
    }
    trimmed = rc.trim_manifest_for_prompt(manifest)
    assert "files" not in trimmed
    assert "volatile_paths" not in trimmed
    assert "observation_log" not in trimmed
    assert "improvement_history" not in trimmed
    assert trimmed["description"] == "stays"
    assert trimmed["coherence"]["coherence_accepted"] == [{"signature": "s"}]


# ── End-to-end through the Flask routes ────────────────────────────────────


@pytest.fixture
def flask_client(monkeypatch, tmp_path: Path, fake_manifest):
    """Spin up a Flask app with the repair_chat routes wired in.

    We bypass the full ``build_app`` factory — it instantiates dozens of
    routes we don't need — and register only ours against a fresh
    ``Flask`` instance. ``network.json`` points at the test tmp_path so
    ``_shared_dir`` resolves correctly.
    """
    from flask import Flask
    from evolve_admin.web.repair_chat_routes import register_repair_chat_routes

    network = {"sharedDir": str(tmp_path / "shared")}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Stub the dispatcher used by /message — keep tests offline.
    from evolve_admin.applications import repair_chat as _rc

    def _fake_default_dispatcher():
        return _fake_dispatch_success(
            "Sure — want me to set example_triggers?\n\n"
            "<<<repair_proposal action=\"propose_field_edit\">>>\n"
            "{\"field\": \"example_triggers\", \"after\": [\"morning\"]}\n"
            "<<<end>>>\n"
        )

    monkeypatch.setattr(_rc, "_default_dispatcher", _fake_default_dispatcher)

    app = Flask(__name__)
    register_repair_chat_routes(app, network_path)
    app.testing = True
    return app.test_client(), fake_manifest


def test_route_state_returns_lock_usage_log(flask_client) -> None:
    client, (bot_id, app_id, _) = flask_client
    r = client.get(f"/api/applications/{bot_id}/{app_id}/repair-chat/state")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["lock"]["locked"] is False
    assert body["usage"]["used"] == 0
    assert body["log"] == []


def test_route_message_persists_turns_and_proposals(flask_client) -> None:
    client, (bot_id, app_id, _) = flask_client
    r = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "what's wrong?"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert len(body["assistant"]["proposals"]) == 1
    assert body["usage"]["used"] == 1
    # Lock should be held now.
    assert body["lock"]["locked"] is True


def test_route_message_rejects_empty(flask_client) -> None:
    client, (bot_id, app_id, _) = flask_client
    r = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": ""},
    )
    assert r.status_code == 400


def test_route_message_rejects_bad_bot_id(flask_client) -> None:
    client, (_, app_id, _) = flask_client
    r = client.post(
        f"/api/applications/..%2Fetc/{app_id}/repair-chat/message",
        json={"message": "x"},
    )
    # Flask resolves the URL pattern before our validator; either way
    # the response is non-200.
    assert r.status_code != 200


def test_route_apply_blocks_unknown_proposal_id(flask_client) -> None:
    client, (bot_id, app_id, _) = flask_client
    # First, hold the lock by sending a message.
    client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "hi"},
    )
    r = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/apply",
        json={"proposal_id": "ffffffff"},
    )
    assert r.status_code == 404


def test_route_apply_writes_manifest(flask_client) -> None:
    client, (bot_id, app_id, _) = flask_client
    msg = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "fix it"},
    )
    proposal_id = msg.get_json()["assistant"]["proposals"][0]["id"]
    r = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/apply",
        json={"proposal_id": proposal_id},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["result"]["top_field"] == "example_triggers"


def test_route_message_dispatcher_failure_renders_offline_message(
    monkeypatch, flask_client,
) -> None:
    client, (bot_id, app_id, _) = flask_client
    from evolve_admin.applications import repair_chat as _rc
    monkeypatch.setattr(_rc, "_default_dispatcher",
                        lambda: _fake_dispatch_failure("agent timeout"))
    r = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "hi"},
    )
    assert r.status_code == 200  # graceful — operator sees the message
    body = r.get_json()
    assert "unreachable" in body["assistant"]["text"]
    assert "agent timeout" in body["assistant"]["text"]
    assert body["assistant"]["error"]
    # Usage still bumps on failed dispatch (see route docstring).
    assert body["usage"]["used"] == 1


def test_route_apply_done_releases_lock(flask_client, monkeypatch) -> None:
    client, (bot_id, app_id, _) = flask_client
    # Force the dispatcher to emit a done proposal.
    from evolve_admin.applications import repair_chat as _rc
    monkeypatch.setattr(
        _rc, "_default_dispatcher",
        lambda: _fake_dispatch_success(
            "All set.\n\n<<<repair_proposal action=\"done\">>>\n{}\n<<<end>>>"
        ),
    )
    msg = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "ok"},
    )
    pid = msg.get_json()["assistant"]["proposals"][0]["id"]
    assert msg.get_json()["lock"]["locked"] is True
    client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/apply",
        json={"proposal_id": pid},
    )
    state = client.get(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/state"
    ).get_json()
    assert state["lock"]["locked"] is False


def test_route_message_rate_limit_returns_429(flask_client) -> None:
    """Eleventh message in 24h returns 429.

    Sends 10 successful messages to exhaust the cap, then expects the
    next one to be rejected. The fake dispatcher returns instantly so
    this is cheap.
    """
    client, (bot_id, app_id, _) = flask_client
    for _ in range(10):
        rr = client.post(
            f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
            json={"message": "iter"},
        )
        assert rr.status_code == 200, rr.get_json()
    r = client.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "one too many"},
    )
    assert r.status_code == 429
    body = r.get_json()
    assert "daily message cap" in body["error"]


def test_route_apply_blocks_second_operator(flask_client) -> None:
    """Second operator (distinct cookie) gets 409 on apply.

    First operator's cookie is set on the initial /message call. The
    second operator simulates a fresh browser by using a separate test
    client whose cookie jar starts empty.
    """
    client_a, (bot_id, app_id, _) = flask_client
    # Operator A sends a message — sets the lock + cookie.
    msg = client_a.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/message",
        json={"message": "first turn"},
    )
    pid = msg.get_json()["assistant"]["proposals"][0]["id"]

    # Operator B = a fresh test client (no cookie carry-over).
    client_b = client_a.application.test_client()
    r = client_b.post(
        f"/api/applications/{bot_id}/{app_id}/repair-chat/apply",
        json={"proposal_id": pid},
    )
    assert r.status_code == 409
    body = r.get_json()
    assert "another operator" in body["error"] or "lock held" in body["error"]
