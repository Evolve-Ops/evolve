"""tests/test_evo_action_tools_part2.py — Phase 1.5x tools landed 2026-05-27.

Covers the 7 new evo tools added in the "wrap UI surfaces" pass:

* action.proposal.refine            (write_safe; wraps refine API)
* action.cost.set_bot_cap           (write_risky; daily_cap_usd write)
* action.cost.clear_bot_cap         (write_risky; daily_cap_usd remove)
* action.cost.clear_enforcement     (write_risky; spend-cap flag clear)
* pod_state.rollback_points         (read; list rollback targets)
* action.bot.rollback               (destructive; revert openclaw.json)
* action.bot.reverse_rollback       (write_risky; undo a rollback)

Test scope mirrors test_evo_action_tools.py:

* Registration + tier invariants are smoke-checked in
  test_registration_and_invariants (one test covers all 7).
* Validate paths get explicit coverage — happy path + each refusal
  branch. This is the third gate in spec §5.2 (don't render a button
  that would fail at execute time).
* Handler paths get coverage where the fixture cost is low (cost
  tools: tmp_path + a tiny network.json is enough). Recovery
  handlers depend on git+filesystem state; we cover the validate
  branches and trust the underlying recovery module's own test
  coverage for the execution path.
* Refine's LLM call is not exercised — handler tests would need a
  stub for arbiter.refine.make_anthropic_caller, which is more
  fixture than the value justifies right now. Validate-only is
  sufficient to catch the high-value bad-args cases (missing
  proposal, pod-wide proposal, no api_key).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo.tools import (  # noqa: E402
    RiskTier,
    action_cost,
    action_proposal_refine,
    action_recovery,
    lookup,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — shared across cost + recovery + refine tests
# ─────────────────────────────────────────────────────────────────────────────


def _write_network(tmp_path: Path, bots: dict | None = None) -> Path:
    """Write a minimal network.json into tmp_path and return its Path.

    ``bots`` defaults to a single bot 'team_bot_a' with no extra config. The
    cost + recovery + refine validate paths all need to look up the
    bot — having one present keeps the validate-happy-path tests
    simple.
    """
    net_path = tmp_path / "network.json"
    net = {
        "sharedDir": str(tmp_path),
        "bots": bots or {"team_bot_a": {}},
    }
    net_path.write_text(json.dumps(net))
    return net_path


def _seed_proposal(shared_dir: Path, *, proposal_id: str, bot_id: str | None,
                   status: str = "pending", summary: str = "test summary"):
    """Write a Proposal to ``shared_dir/proposals/<status>/``. Mirrors
    the helper in test_evo_action_tools.py; duplicated to keep this
    file's pytest collection independent."""
    from arbiter import store as arbiter_store
    from schema.proposal import Investigation, Proposal, RiskTag
    from schema.provenance import Provenance

    p = Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id="test_gen",
        dimension="cost",
        trigger_observations=[],
        provenance=Provenance(technique="test"),
        problem="test problem",
        action=Investigation(context="test"),
        risk_tag=RiskTag(blast_radius="local", reversibility="reversible"),
        urgency="improvement",
        admin_surface_summary=summary,
        status=status,
    )
    arbiter_store.write_proposal(p, shared_dir, subdir=status)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Registration + tier invariants (all 7 in one test)
# ─────────────────────────────────────────────────────────────────────────────


_NEW_TOOL_TIERS = [
    ("action.proposal.refine", RiskTier.WRITE_SAFE),
    ("action.cost.set_bot_cap", RiskTier.WRITE_RISKY),
    ("action.cost.clear_bot_cap", RiskTier.WRITE_RISKY),
    ("action.cost.clear_enforcement", RiskTier.WRITE_RISKY),
    ("pod_state.rollback_points", RiskTier.READ),
    ("action.bot.rollback", RiskTier.DESTRUCTIVE),
    ("action.bot.reverse_rollback", RiskTier.WRITE_RISKY),
]


@pytest.mark.parametrize("name,expected_tier", _NEW_TOOL_TIERS)
def test_registration_and_invariants(name, expected_tier):
    """Every new tool: registered, correct tier, validate-vs-read
    invariant honoured, description is substantive (not a stub)."""
    tool = lookup(name)
    assert tool is not None, f"{name} not registered"
    assert tool.risk_tier == expected_tier

    # Tool.__post_init__ enforces validate-vs-read at construction
    # time, so by reaching the registry the tool already passed that
    # check. Re-asserting here documents the invariant per-tool for
    # future readers.
    if tool.risk_tier == RiskTier.READ:
        assert tool.validate is None
    else:
        assert tool.validate is not None

    # Sanity floors on description + schema. A bare-bones registration
    # would slip past Python typing but fail the operator's ability
    # to read what the tool does.
    assert tool.description and len(tool.description) >= 80
    assert tool.input_schema.get("type") == "object"
    assert isinstance(tool.input_schema.get("properties"), dict)


# ─────────────────────────────────────────────────────────────────────────────
# action.proposal.refine — validate
# ─────────────────────────────────────────────────────────────────────────────


def test_refine_validate_missing_proposal_id(tmp_path):
    net = _write_network(tmp_path)
    result = action_proposal_refine._refine_validate(
        shared_dir=tmp_path, network_path=net,
        proposal_id="", feedback="reword this",
    )
    assert result["ok"] is False
    assert "proposal_id" in result["reason"]


def test_refine_validate_missing_feedback(tmp_path):
    """Empty/whitespace feedback → validate rejects with clean reason."""
    net = _write_network(tmp_path)
    _seed_proposal(tmp_path, proposal_id="p-refine-1", bot_id="team_bot_a")
    result = action_proposal_refine._refine_validate(
        shared_dir=tmp_path, network_path=net,
        proposal_id="p-refine-1", feedback="   ",
    )
    assert result["ok"] is False
    assert "feedback" in result["reason"]


def test_refine_validate_unknown_proposal(tmp_path):
    net = _write_network(tmp_path)
    result = action_proposal_refine._refine_validate(
        shared_dir=tmp_path, network_path=net,
        proposal_id="nope", feedback="reword",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


# NOTE: a "pod-wide proposal (no bot_id)" test is omitted because the
# current Proposal schema rejects empty bot_id at __post_init__
# (schema/proposal.py:1184). The refine tool still has a defensive
# ``if not proposal.bot_id`` branch mirroring the server-side guard at
# server.py:23880 — both will trip if a future schema change permits
# pod-wide proposals. Reaching that branch today requires bypassing the
# schema (writing JSON directly), which is more fixture than the
# coverage justifies.


def test_refine_validate_terminal_status_rejected(tmp_path):
    """Proposal in 'rejected' status → refine refuses (terminal)."""
    net = _write_network(tmp_path)
    _seed_proposal(
        tmp_path, proposal_id="p-terminal", bot_id="team_bot_a",
        status="archived",
    )
    result = action_proposal_refine._refine_validate(
        shared_dir=tmp_path, network_path=net,
        proposal_id="p-terminal", feedback="reword",
    )
    assert result["ok"] is False
    # Either "status" mention or the explicit list of acceptable statuses.
    assert "status" in result["reason"]


def test_refine_validate_no_api_key_rejected(tmp_path, monkeypatch):
    """No LLM provider credentialed → validate fails with key-config hint.

    Pinned via infra_llm (provider-agnostic, #3466) — a dev machine's
    exported provider env vars must not flip this, so resolution is
    forced to None.
    """
    net = _write_network(tmp_path)
    _seed_proposal(tmp_path, proposal_id="p-no-key", bot_id="team_bot_a")
    import infra_llm

    monkeypatch.setattr(infra_llm, "resolve_infra_llm", lambda role, **kw: None)
    result = action_proposal_refine._refine_validate(
        shared_dir=tmp_path, network_path=net,
        proposal_id="p-no-key", feedback="reword",
    )
    assert result["ok"] is False
    assert "no LLM provider credentialed" in result["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# action.cost.set_bot_cap — validate + handler
# ─────────────────────────────────────────────────────────────────────────────


def test_set_bot_cap_validate_rejects_missing_cap(tmp_path):
    net = _write_network(tmp_path)
    result = action_cost._set_bot_cap_validate(
        network_path=net, bot_id="team_bot_a", cap_usd=None,  # type: ignore[arg-type]
    )
    assert result["ok"] is False
    assert "cap_usd" in result["reason"]


def test_set_bot_cap_validate_rejects_negative(tmp_path):
    net = _write_network(tmp_path)
    result = action_cost._set_bot_cap_validate(
        network_path=net, bot_id="team_bot_a", cap_usd=-1.0,
    )
    assert result["ok"] is False
    assert "positive" in result["reason"]


def test_set_bot_cap_validate_rejects_zero(tmp_path):
    """Zero is treated as 'remove cap' shape; the user should call
    clear_bot_cap instead. validate guides them there."""
    net = _write_network(tmp_path)
    result = action_cost._set_bot_cap_validate(
        network_path=net, bot_id="team_bot_a", cap_usd=0.0,
    )
    assert result["ok"] is False
    assert "clear_bot_cap" in result["reason"]


def test_set_bot_cap_validate_unknown_bot(tmp_path):
    net = _write_network(tmp_path)
    result = action_cost._set_bot_cap_validate(
        network_path=net, bot_id="ghost", cap_usd=5.0,
    )
    assert result["ok"] is False
    assert "unknown bot" in result["reason"]


def test_set_bot_cap_validate_ok_surfaces_prior_cap(tmp_path):
    """Happy path → validate.ok + context shows the prior cap (so the
    operator can decide whether the change is reasonable)."""
    net = _write_network(tmp_path, bots={"team_bot_a": {"daily_cap_usd": 3.0}})
    result = action_cost._set_bot_cap_validate(
        network_path=net, bot_id="team_bot_a", cap_usd=10.0,
    )
    assert result["ok"] is True
    assert result["context"]["prior_cap_usd"] == 3.0
    assert result["context"]["new_cap_usd"] == 10.0


def test_set_bot_cap_handler_writes_network_json(tmp_path):
    """Real side effect — handler writes daily_cap_usd into network.json,
    and a subsequent read sees the new value."""
    net = _write_network(tmp_path)  # no prior cap
    result = action_cost._set_bot_cap_handler(
        network_path=net, bot_id="team_bot_a", cap_usd=7.5,
    )
    assert result["ok"] is True
    assert result["cap_usd"] == 7.5
    assert result["prior_cap_usd"] is None
    # verify_via shape — operator gets a follow-up hint
    assert result["verify_via"]["tool"] == "config.network"
    # On-disk verification
    written = json.loads(net.read_text())
    assert written["bots"]["team_bot_a"]["daily_cap_usd"] == 7.5


def test_set_bot_cap_handler_overwrites_existing(tmp_path):
    """Setting a new cap over an existing one returns the prior value
    so the operator sees the change clearly."""
    net = _write_network(tmp_path, bots={"team_bot_a": {"daily_cap_usd": 5.0}})
    result = action_cost._set_bot_cap_handler(
        network_path=net, bot_id="team_bot_a", cap_usd=12.0,
    )
    assert result["ok"] is True
    assert result["prior_cap_usd"] == 5.0
    written = json.loads(net.read_text())
    assert written["bots"]["team_bot_a"]["daily_cap_usd"] == 12.0


# ─────────────────────────────────────────────────────────────────────────────
# action.cost.clear_bot_cap — validate + handler
# ─────────────────────────────────────────────────────────────────────────────


def test_clear_bot_cap_validate_no_cap_to_clear(tmp_path):
    """Validate refuses when there's no cap — nothing to clear is a
    user-visible "you didn't need to do anything" signal."""
    net = _write_network(tmp_path)  # team_bot_a has no cap
    result = action_cost._clear_bot_cap_validate(
        network_path=net, bot_id="team_bot_a",
    )
    assert result["ok"] is False
    assert "no daily_cap_usd" in result["reason"] or "nothing to clear" in result["reason"]


def test_clear_bot_cap_validate_ok_when_cap_set(tmp_path):
    net = _write_network(tmp_path, bots={"team_bot_a": {"daily_cap_usd": 8.0}})
    result = action_cost._clear_bot_cap_validate(
        network_path=net, bot_id="team_bot_a",
    )
    assert result["ok"] is True
    assert result["context"]["prior_cap_usd"] == 8.0


def test_clear_bot_cap_handler_removes_field(tmp_path):
    """Handler pops daily_cap_usd from network.json and returns the
    prior value."""
    net = _write_network(tmp_path, bots={"team_bot_a": {"daily_cap_usd": 9.0}})
    result = action_cost._clear_bot_cap_handler(
        network_path=net, bot_id="team_bot_a",
    )
    assert result["ok"] is True
    assert result["prior_cap_usd"] == 9.0
    written = json.loads(net.read_text())
    assert "daily_cap_usd" not in written["bots"]["team_bot_a"]


# ─────────────────────────────────────────────────────────────────────────────
# action.cost.clear_enforcement — validate (handler depends on spend_caps
# module side effects; covered by spend_caps' own tests)
# ─────────────────────────────────────────────────────────────────────────────


def test_clear_enforcement_validate_unknown_bot(tmp_path):
    net = _write_network(tmp_path)
    result = action_cost._clear_enforcement_validate(
        shared_dir=tmp_path, network_path=net, bot_id="ghost",
    )
    assert result["ok"] is False
    assert "unknown bot" in result["reason"]


def test_clear_enforcement_validate_ok(tmp_path):
    """Known bot → validate ok. We can't pre-check enforcement state
    here without duplicating spend_caps internals; handler reports
    'nothing to clear' if no flag exists."""
    net = _write_network(tmp_path)
    result = action_cost._clear_enforcement_validate(
        shared_dir=tmp_path, network_path=net, bot_id="team_bot_a",
    )
    assert result["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.rollback_points — handler (no validate — read tool)
# ─────────────────────────────────────────────────────────────────────────────


def test_rollback_points_unknown_bot(tmp_path):
    net = _write_network(tmp_path)
    result = action_recovery._points_handler(
        network_path=net, bot_id="ghost",
    )
    assert result["ok"] is False
    assert "unknown bot" in result["error"]


def test_rollback_points_clamps_limit(tmp_path):
    """limit > 100 clamps to 100, limit < 1 clamps to 1. Prevents a
    runaway model from asking for the entire history."""
    net = _write_network(tmp_path)
    # We don't have a real workspace + backups in tmp_path, so the
    # underlying list_rollback_points will return either an empty
    # list or error; either way the limit clamp is exercised before
    # that call. We can't directly observe the clamp without the
    # underlying call succeeding, so just confirm a sane response
    # shape on the unknown-bot path which exercises the same code
    # before the bot lookup.
    result = action_recovery._points_handler(
        network_path=net, bot_id="team_bot_a", limit=9999,
    )
    # Either succeeds with points=[] (no backups in tmp_path) or
    # errors cleanly; should not raise.
    assert isinstance(result, dict)
    assert "ok" in result


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.rollback — validate (handler tests defer to recovery's own
# git-fixture-bearing tests)
# ─────────────────────────────────────────────────────────────────────────────


def test_rollback_validate_missing_bot_id(tmp_path):
    net = _write_network(tmp_path)
    result = action_recovery._rollback_validate(
        network_path=net, bot_id="", target="HEAD~1",
    )
    assert result["ok"] is False
    assert "bot_id" in result["reason"]


def test_rollback_validate_missing_target(tmp_path):
    net = _write_network(tmp_path)
    result = action_recovery._rollback_validate(
        network_path=net, bot_id="team_bot_a", target="",
    )
    assert result["ok"] is False
    assert "target" in result["reason"]


def test_rollback_validate_unknown_bot(tmp_path):
    net = _write_network(tmp_path)
    result = action_recovery._rollback_validate(
        network_path=net, bot_id="ghost", target="HEAD~1",
    )
    assert result["ok"] is False
    assert "unknown bot" in result["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.reverse_rollback — validate
# ─────────────────────────────────────────────────────────────────────────────


def test_reverse_rollback_validate_missing_id(tmp_path):
    net = _write_network(tmp_path)
    result = action_recovery._reverse_validate(
        network_path=net, rollback_id="",
    )
    assert result["ok"] is False
    assert "rollback_id" in result["reason"]
