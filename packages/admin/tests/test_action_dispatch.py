"""tests/test_action_dispatch.py — evo MCP tools for dispatch polling.

Phase 3.3b of the Slice 3 spec
(docs/spec-take-this-on-evo-dispatch-2026-06-04.md).

Two tools:
  pod_state.dispatches         — read pending evo-bound envelopes
  action.dispatch.acknowledge  — report outcome + transition the
                                 proposal + unlink the envelope

These tests exercise the tools directly (no MCP harness) using
synthetic dispatch envelopes + a real arbiter store on a temp
shared_dir. The wired-up registry registration is also pinned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_DIR = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


# Importing the module registers the tools.
from evolve_admin.evo.tools import action_dispatch, lookup  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import find_proposal, write_proposal  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _seed_dispatched_proposal(shared_dir: Path, *, proposal_id: str = "test-1"):
    """Create a proposal in 'dispatched' state with a matching envelope
    on disk. Mirrors what the admin server's /dispatch endpoint does."""
    from schema.proposal import DispatchState

    p = make_investigation_proposal(
        audience="pod_operator", bot_id="team_bot_a", problem="x",
    )
    p.id = proposal_id  # for test predictability
    p.dispatch_target = "evo"
    p.dispatch_message = "fix it"
    p.dispatch_state = DispatchState(
        target="evo",
        dispatched_at="2026-06-04T12:00:00+00:00",
        message="fix it",
        session_id=str(
            shared_dir / "dispatches" / "evo" / f"{p.id}.json"
        ),
    )
    transition(p, "pending", actor="test", reason="seed")
    transition(p, "dispatched", actor="test", reason="seed")
    write_proposal(p, shared_dir, subdir="pending")

    # Drop a matching envelope.
    env_dir = shared_dir / "dispatches" / "evo"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_path = env_dir / f"{p.id}.json"
    env_path.write_text(json.dumps({
        "proposal_id": p.id,
        "target": "evo",
        "dispatched_at": "2026-06-04T12:00:00+00:00",
        "message": "fix it",
        "proposal": p.to_dict(),
    }))
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Registry pin
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_state_dispatches_tool_registered():
    """Read-tier tool that returns pending dispatches addressed to evo.
    Must be a Tool with RiskTier.READ and no validate (read-tier
    contract from __init__.py's Tool.__post_init__)."""
    tool = lookup("pod_state.dispatches")
    assert tool is not None
    assert tool.risk_tier.value == "read"
    assert tool.validate is None
    # Tool description must mention the dispatches/{target} path so
    # the model understands what it's reading.
    assert "dispatches/evo" in tool.description.lower()


def test_action_dispatch_acknowledge_tool_registered():
    """Write-tier tool with required validate. Inputs include
    proposal_id + outcome at minimum; outcome is an enum-constrained
    string."""
    tool = lookup("action.dispatch.acknowledge")
    assert tool is not None
    assert tool.risk_tier.value == "write_risky"
    assert tool.validate is not None
    schema = tool.input_schema
    assert "proposal_id" in schema["properties"]
    assert "outcome" in schema["properties"]
    assert set(schema["required"]) == {"proposal_id", "outcome"}
    assert schema["properties"]["outcome"]["enum"] == ["applied", "failed", "in_process"]


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.dispatches — read path
# ─────────────────────────────────────────────────────────────────────────────


def test_list_returns_empty_when_no_envelopes(tmp_path):
    """Empty dispatches dir → ok=True, count=0, dispatches=[]."""
    out = action_dispatch._list_handler(tmp_path)
    assert out == {"ok": True, "count": 0, "dispatches": []}


def test_list_returns_envelope_fields(tmp_path):
    """A real envelope's key fields (proposal_id, message, bot_id,
    generator_id, action_kind) surface in the response."""
    _seed_dispatched_proposal(tmp_path)
    out = action_dispatch._list_handler(tmp_path)
    assert out["ok"] is True
    assert out["count"] == 1
    item = out["dispatches"][0]
    assert item["proposal_id"] == "test-1"
    assert item["message"] == "fix it"
    assert item["bot_id"] == "team_bot_a"
    assert item["action_kind"] == "Investigation"
    assert "x" in (item["problem"] or "")


def test_list_skips_malformed_envelopes(tmp_path):
    """A garbage envelope file shouldn't block reading the others."""
    env_dir = tmp_path / "dispatches" / "evo"
    env_dir.mkdir(parents=True)
    # Bad file.
    (env_dir / "bad.json").write_text("{this is not json")
    # Good envelope inline (no need to seed a full proposal).
    (env_dir / "good.json").write_text(json.dumps({
        "proposal_id": "good",
        "target": "evo",
        "dispatched_at": "2026-06-04T12:00:00Z",
        "message": "do it",
        "proposal": {
            "bot_id": "team_bot_a",
            "generator_id": "audit_poller",
            "admin_surface_summary": "x: y",
            "action": {"kind": "Investigation"},
        },
    }))

    out = action_dispatch._list_handler(tmp_path)
    assert out["count"] == 1
    assert out["dispatches"][0]["proposal_id"] == "good"


def test_list_sorts_by_dispatched_at_ascending(tmp_path):
    """FIFO so evo handles older dispatches first."""
    env_dir = tmp_path / "dispatches" / "evo"
    env_dir.mkdir(parents=True)
    for pid, ts in [
        ("c", "2026-06-04T13:00:00Z"),
        ("a", "2026-06-04T11:00:00Z"),
        ("b", "2026-06-04T12:00:00Z"),
    ]:
        (env_dir / f"{pid}.json").write_text(json.dumps({
            "proposal_id": pid,
            "dispatched_at": ts,
            "message": "...",
            "proposal": {"action": {"kind": "Investigation"}},
        }))
    out = action_dispatch._list_handler(tmp_path)
    assert [d["proposal_id"] for d in out["dispatches"]] == ["a", "b", "c"]


def test_list_skips_temp_files(tmp_path):
    """Atomic-write tempfiles (.dispatch-*.json) must not surface to
    the model as pending dispatches — they're write-in-flight."""
    env_dir = tmp_path / "dispatches" / "evo"
    env_dir.mkdir(parents=True)
    (env_dir / ".dispatch-xyz.json").write_text("{}")
    out = action_dispatch._list_handler(tmp_path)
    assert out["count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# action.dispatch.acknowledge — validate
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_rejects_missing_proposal_id(tmp_path):
    r = action_dispatch._acknowledge_validate(
        tmp_path, proposal_id="", outcome="applied",
    )
    assert r["ok"] is False
    assert "required" in r["reason"]


def test_validate_rejects_unknown_outcome(tmp_path):
    p = _seed_dispatched_proposal(tmp_path)
    r = action_dispatch._acknowledge_validate(
        tmp_path, proposal_id=p.id, outcome="whatever",
    )
    assert r["ok"] is False
    assert "outcome must be" in r["reason"]


def test_validate_rejects_missing_proposal(tmp_path):
    r = action_dispatch._acknowledge_validate(
        tmp_path, proposal_id="does-not-exist", outcome="applied",
    )
    assert r["ok"] is False
    assert "not found" in r["reason"]


def test_validate_rejects_non_dispatched_proposal(tmp_path):
    """If a proposal isn't currently dispatched, acknowledge can't
    write a result — that's a 409-shaped condition. validate catches
    it before the model wastes a button-click."""
    p = _seed_dispatched_proposal(tmp_path)
    # Move it to applied so the dispatched state no longer holds.
    located = find_proposal(tmp_path, p.id)
    in_mem, _path, _subdir = located
    transition(in_mem, "applied", actor="test", reason="manual")
    from arbiter.store import move_proposal
    move_proposal(in_mem, tmp_path, from_subdir="pending")

    r = action_dispatch._acknowledge_validate(
        tmp_path, proposal_id=p.id, outcome="applied",
    )
    assert r["ok"] is False
    assert "dispatched" in r["reason"]


def test_validate_happy_path_returns_summary(tmp_path):
    """Validate returns the proposal's summary in context so the
    confirmation modal can show it."""
    p = _seed_dispatched_proposal(tmp_path)
    r = action_dispatch._acknowledge_validate(
        tmp_path, proposal_id=p.id, outcome="applied", message="ok",
    )
    assert r["ok"] is True
    assert "summary" in r["context"]
    assert r["context"]["target"] == "evo"


# ─────────────────────────────────────────────────────────────────────────────
# action.dispatch.acknowledge — handler
# ─────────────────────────────────────────────────────────────────────────────


def test_acknowledge_applied_transitions_and_unlinks(tmp_path):
    """outcome='applied' → status=applied, proposal moves to applied/
    subdir, envelope unlinked from dispatches/evo/."""
    p = _seed_dispatched_proposal(tmp_path)
    env_path = tmp_path / "dispatches" / "evo" / f"{p.id}.json"
    assert env_path.exists(), "fixture didn't write envelope"

    out = action_dispatch._acknowledge_handler(
        tmp_path,
        proposal_id=p.id,
        outcome="applied",
        message="rewired the script",
        applied_changes=[{"file": "x.py", "kind": "patch"}],
    )
    assert out["ok"] is True
    assert out["new_status"] == "applied"
    assert out["envelope_unlinked"] is True

    # Proposal really transitioned + moved subdirs.
    located = find_proposal(tmp_path, p.id)
    assert located is not None
    on_disk, _path, subdir = located
    assert on_disk.status == "applied"
    assert subdir == "applied"
    # Result recorded on dispatch_state for audit.
    assert on_disk.dispatch_state.result is not None
    assert on_disk.dispatch_state.result.outcome == "applied"
    assert on_disk.dispatch_state.result.message == "rewired the script"
    assert on_disk.dispatch_state.result.applied_changes == [
        {"file": "x.py", "kind": "patch"}
    ]
    # Envelope gone.
    assert not env_path.exists()


def test_acknowledge_failed_goes_to_failed_flagged(tmp_path):
    p = _seed_dispatched_proposal(tmp_path)
    out = action_dispatch._acknowledge_handler(
        tmp_path,
        proposal_id=p.id,
        outcome="failed",
        message="couldn't reproduce the issue",
    )
    assert out["ok"] is True
    assert out["new_status"] == "failed_flagged"

    located = find_proposal(tmp_path, p.id)
    on_disk, _path, subdir = located
    assert on_disk.status == "failed_flagged"
    assert subdir == "archived"


def test_acknowledge_in_process_goes_to_applied(tmp_path):
    """in_process means evo handed back to the operator — proposal
    moves to applied (same status, but the operator's In Process tab
    surfaces it via the action_kind check)."""
    p = _seed_dispatched_proposal(tmp_path)
    out = action_dispatch._acknowledge_handler(
        tmp_path,
        proposal_id=p.id,
        outcome="in_process",
        message="needs operator judgment on whether to ...",
    )
    assert out["ok"] is True
    assert out["new_status"] == "applied"


def test_acknowledge_rejects_invalid_outcome(tmp_path):
    """Even though validate catches this, the handler is defense in
    depth — if the proxy somehow bypassed validate, the handler
    still refuses."""
    p = _seed_dispatched_proposal(tmp_path)
    out = action_dispatch._acknowledge_handler(
        tmp_path, proposal_id=p.id, outcome="weird",
    )
    assert out["ok"] is False
    assert "outcome must be" in out["error"]


def test_acknowledge_rejects_missing_proposal(tmp_path):
    out = action_dispatch._acknowledge_handler(
        tmp_path, proposal_id="does-not-exist", outcome="applied",
    )
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_acknowledge_rejects_non_dispatched(tmp_path):
    """A proposal that's never been dispatched can't be acknowledged.
    Defends against a stale envelope or evo confusing itself."""
    from arbiter.store import move_proposal
    p = _seed_dispatched_proposal(tmp_path)
    located = find_proposal(tmp_path, p.id)
    in_mem, _path, _subdir = located
    transition(in_mem, "applied", actor="test", reason="manual")
    move_proposal(in_mem, tmp_path, from_subdir="pending")

    out = action_dispatch._acknowledge_handler(
        tmp_path, proposal_id=p.id, outcome="applied",
    )
    assert out["ok"] is False
    assert "dispatched" in out["error"]
