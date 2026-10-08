"""tests/test_operator_apply_inline.py — operator-UI inline apply path.

Operator-clicked config changes (Enable plugin, Install MCP, etc.) go through
the shared Proposal pipeline (security_warden gates → applier → verify) but
auto-approve at write time rather than sitting in the Self-Improvement review
queue. These tests exercise the helper that wires that — ``_operator_create_apply``
— against a stubbed applier so we can cover happy path, applier-refusal, and
deferred-kind guard without standing up a real bot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from arbiter.appliers.base import (  # noqa: E402
    ApplyResult,
    RevertResult,
    _APPLIER_REGISTRY,
    register_applier,
)
from schema.proposal import RiskTag  # noqa: E402


class _FakeApplier:
    """Drop-in applier double — returns whatever result the test wires up."""

    def __init__(self, result: ApplyResult):
        self.result = result
        self.snapshot_called = False
        self.apply_called = False

    def capture_snapshot(self, action, bot_id):
        self.snapshot_called = True
        return {"before": {"bot_id": bot_id}}

    def apply(self, action, bot_id):
        self.apply_called = True
        return self.result

    def revert(self, snapshot, bot_id):
        return RevertResult(ok=True)


@pytest.fixture
def fake_plugin_applier():
    """Swap in a stub for EnablePluginEntry, restore the real one after."""
    real = _APPLIER_REGISTRY.get("EnablePluginEntry")
    fakes: list[_FakeApplier] = []

    def install(result: ApplyResult) -> _FakeApplier:
        applier = _FakeApplier(result)
        register_applier("EnablePluginEntry", applier)
        fakes.append(applier)
        return applier

    yield install

    if real is not None:
        register_applier("EnablePluginEntry", real)
    else:
        _APPLIER_REGISTRY.pop("EnablePluginEntry", None)


@pytest.fixture
def shared_dir(tmp_path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    return d


def _call_helper(*, action_kind: str, action_payload: dict, bot_id: str,
                  shared_dir: Path):
    from evolve_admin.web.server import _operator_create_apply

    return _operator_create_apply(
        action_kind=action_kind,
        action_payload=action_payload,
        bot_id=bot_id,
        summary=f"test:{action_kind}:{bot_id}",
        technique="test_operator_ui",
        dimension="operational_health",
        risk=RiskTag(blast_radius="bot", reversibility="auto", touches=["bot_config"]),
        shared_dir=shared_dir,
    )


def test_happy_path_succeeds_inline(fake_plugin_applier, shared_dir):
    """When the applier returns ok=True on a claim-less proposal, the helper
    should drive it through approved_auto → applied → succeeded and land
    the file in archived/."""
    applier = fake_plugin_applier(ApplyResult(ok=True, message="enabled"))

    proposal, err = _call_helper(
        action_kind="EnablePluginEntry",
        action_payload={"bot_id": "team_bot_a", "plugin_name": "telegram"},
        bot_id="team_bot_a",
        shared_dir=shared_dir,
    )

    assert err is None
    assert proposal is not None
    assert proposal["status"] == "succeeded"
    assert proposal["generator_id"] == "operator_ui"
    assert applier.apply_called is True

    pid = proposal["id"]
    assert (shared_dir / "proposals" / "archived" / f"{pid}.json").exists()
    assert not (shared_dir / "proposals" / "pending" / f"{pid}.json").exists()

    # The audit trail records both the auto-approval and the success.
    history_statuses = [(h["from_status"], h["to_status"]) for h in proposal["history"]]
    assert ("pending", "approved_auto") in history_statuses
    assert ("approved_auto", "applied") in history_statuses
    assert ("applied", "succeeded") in history_statuses


def test_applier_flag_refusal_lands_in_failed_flagged(fake_plugin_applier, shared_dir):
    """When the applier returns ok=False with fail_action='flag' (security_warden-
    style refusal — e.g. denied_plugin, load-path-not-whitelisted), apply()
    transitions to failed_flagged internally. The helper just lets it land in
    archived/ with the rejection reason in history."""
    applier = fake_plugin_applier(ApplyResult(
        ok=False,
        details={"fail_action": "flag"},
        message="refused: plugin is in denied_plugins",
    ))

    proposal, err = _call_helper(
        action_kind="EnablePluginEntry",
        action_payload={"bot_id": "team_bot_a", "plugin_name": "telegram"},
        bot_id="team_bot_a",
        shared_dir=shared_dir,
    )

    assert err is None  # no creation-class error; apply outcome is on the proposal
    assert proposal is not None
    assert proposal["status"] == "failed_flagged"
    assert applier.apply_called is True

    pid = proposal["id"]
    assert (shared_dir / "proposals" / "archived" / f"{pid}.json").exists()

    # The refusal reason is recorded in the history entry.
    last = proposal["history"][-1]
    assert last["to_status"] == "failed_flagged"
    assert "denied_plugins" in last["reason"]


def test_applier_unflagged_failure_is_explicitly_flagged(fake_plugin_applier, shared_dir):
    """When the applier returns ok=False without fail_action='flag' (e.g. IO
    error mid-apply), apply() leaves the proposal at approved_auto. The
    helper must explicitly transition to failed_flagged so the proposal
    doesn't sit there forever."""
    applier = fake_plugin_applier(ApplyResult(
        ok=False,
        details={},
        message="openclaw.json read failed: permission denied",
    ))

    proposal, err = _call_helper(
        action_kind="EnablePluginEntry",
        action_payload={"bot_id": "team_bot_a", "plugin_name": "telegram"},
        bot_id="team_bot_a",
        shared_dir=shared_dir,
    )

    assert err is None
    assert proposal is not None
    assert proposal["status"] == "failed_flagged"
    assert applier.apply_called is True

    pid = proposal["id"]
    assert (shared_dir / "proposals" / "archived" / f"{pid}.json").exists()
    assert not (shared_dir / "proposals" / "pending" / f"{pid}.json").exists()

    last = proposal["history"][-1]
    assert last["to_status"] == "failed_flagged"
    assert "permission denied" in last["reason"]


def test_deferred_completion_kinds_refused_at_creation(shared_dir):
    """Manual/external completion kinds (Investigation, WorkflowInstruction,
    AddSignalCollection, BuildApp) require operator or sweep follow-through
    after apply — they should never be created via the operator-UI inline
    path. The helper rejects them before any side effects."""
    from evolve_admin.web.server import _operator_create_apply

    for kind in ("Investigation", "WorkflowInstruction", "BuildApp"):
        proposal, err = _operator_create_apply(
            action_kind=kind,
            action_payload={"bot_id": "team_bot_a"},
            bot_id="team_bot_a",
            summary=f"should-not-be-created:{kind}",
            technique="test_operator_ui",
            dimension="operational_health",
            risk=RiskTag(blast_radius="bot", reversibility="auto", touches=["bot_config"]),
            shared_dir=shared_dir,
        )
        assert proposal is None, f"{kind} should be refused before creation"
        assert err is not None
        assert "manual or external completion" in err

    # And nothing was written to disk along the way.
    proposals_dir = shared_dir / "proposals"
    assert not proposals_dir.exists() or all(
        not list((proposals_dir / sub).glob("*.json"))
        for sub in ("pending", "applied", "archived")
        if (proposals_dir / sub).exists()
    )


def test_bad_action_payload_returns_creation_error(shared_dir):
    """If the action payload is missing required fields, schema validation
    fails before any proposal is written. The caller should 400 — no
    half-state on disk."""
    from evolve_admin.web.server import _operator_create_apply

    proposal, err = _operator_create_apply(
        action_kind="EnablePluginEntry",
        action_payload={"bot_id": "team_bot_a"},  # missing plugin_name
        bot_id="team_bot_a",
        summary="bad payload",
        technique="test_operator_ui",
        dimension="operational_health",
        risk=RiskTag(blast_radius="bot", reversibility="auto", touches=["bot_config"]),
        shared_dir=shared_dir,
    )

    assert proposal is None
    assert err is not None
    assert "action decode failed" in err
    assert not (shared_dir / "proposals").exists() or not list(
        (shared_dir / "proposals" / "pending").glob("*.json")
    )


def test_response_helper_shapes_success(fake_plugin_applier, shared_dir):
    """The Flask response helper renders {ok, proposal_id, status, applied,
    message} for the UI to show inline rather than redirecting to
    Self-Improvement."""
    from flask import Flask

    from evolve_admin.web.server import _operator_proposal_response

    fake_plugin_applier(ApplyResult(ok=True, message="enabled"))
    proposal, err = _call_helper(
        action_kind="EnablePluginEntry",
        action_payload={"bot_id": "team_bot_a", "plugin_name": "telegram"},
        bot_id="team_bot_a",
        shared_dir=shared_dir,
    )
    assert proposal is not None

    app = Flask(__name__)
    with app.app_context():
        resp = _operator_proposal_response(proposal, err)

    body = resp.get_json()
    assert body["ok"] is True
    assert body["proposal_id"] == proposal["id"]
    assert body["status"] == "succeeded"
    assert body["applied"] is True
    assert "message" in body


def test_response_helper_shapes_creation_failure():
    """When the helper returned (None, err), the response is 400 with the
    error string surfaced — same shape as before this change."""
    from flask import Flask

    from evolve_admin.web.server import _operator_proposal_response

    app = Flask(__name__)
    with app.app_context():
        result = _operator_proposal_response(None, "action decode failed: bad shape")

    # Flask returns (response, status_code) for non-200 paths.
    resp, status = result
    assert status == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "bad shape" in body["error"]
