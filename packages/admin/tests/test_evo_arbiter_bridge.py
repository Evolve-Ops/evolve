"""tests/test_evo_arbiter_bridge.py — Asymmetric chat→arbiter routing.

Step 1 of the evo unify plan. Two assertion families:

1. **Bridge primitives**: ``accept_proposal`` / ``dismiss_proposal`` /
   ``snooze_proposal`` actually mutate the proposal store the same way
   the corresponding HTTP endpoints do. We exercise them against a
   real on-disk proposal in tmp_path to verify state transitions land
   in the right subdir.

2. **Asymmetric routing in the wizard**: ``_record_rec_action`` calls
   the bridge for proposal-derived recs (``source_ref.proposal_id``
   set) and skips it for non-proposal recs (onboarding / scoreboard /
   compliance / whimsy). In both cases ``BetterEngine.record_feedback``
   still fires for the learning signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ──────────────────────────────────────────────────────────────────────────
# Bridge primitive tests — real proposals on disk
# ──────────────────────────────────────────────────────────────────────────


def _persist_pending_investigation(tmp_path: Path) -> str:
    """Create a pending Investigation proposal on disk and return its id.
    Investigation is convenient because its applier is a no-op — we
    don't need to mock the apply layer."""
    from arbiter import store as _store
    from arbiter import state_machine as _sm
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal()
    _sm.transition(p, "pending", actor="test")
    _store.write_proposal(p, tmp_path, subdir="pending")
    return p.id


def test_accept_proposal_drives_through_apply_lifecycle(tmp_path: Path):
    """Investigation has no applier work and no claim, so accept should
    end with the proposal in ``applied`` (manual-completion kind: stays
    in In Process awaiting Mark complete)."""
    from evolve_admin.evo.arbiter_bridge import accept_proposal

    pid = _persist_pending_investigation(tmp_path)
    result = accept_proposal(tmp_path, pid)

    assert result.ok
    assert result.new_status == "applied"
    # Investigation lands in applied/ (the In Process queue's home subdir).
    assert (tmp_path / "proposals" / "applied" / f"{pid}.json").exists()
    assert not (tmp_path / "proposals" / "pending" / f"{pid}.json").exists()


def test_accept_proposal_returns_clean_result_for_missing_id(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import accept_proposal

    result = accept_proposal(tmp_path, "no-such-proposal")
    assert not result.ok
    assert "not found" in result.message


def test_accept_proposal_rejects_wrong_status(tmp_path: Path):
    """Only ``pending`` proposals can be accepted via the bridge — same
    rule the /act endpoint enforces."""
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from evolve_admin.evo.arbiter_bridge import accept_proposal
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal()
    _sm.transition(p, "pending", actor="test")
    _sm.transition(p, "dismissed", actor="test")  # already dismissed
    _store.write_proposal(p, tmp_path, subdir="archived")

    result = accept_proposal(tmp_path, p.id)
    assert not result.ok
    assert "expected 'pending'" in result.message


def test_dismiss_proposal_archives_and_logs_rejection(tmp_path: Path):
    """Dismiss writes the rejection log so the runner-level cooldown
    filter suppresses re-emission. Verifies BOTH effects."""
    from evolve_admin.evo.arbiter_bridge import dismiss_proposal

    pid = _persist_pending_investigation(tmp_path)
    result = dismiss_proposal(tmp_path, pid, reason="not actionable")

    assert result.ok
    assert result.new_status == "dismissed"
    # File moved to archived/
    assert (tmp_path / "proposals" / "archived" / f"{pid}.json").exists()
    # Rejection log entry written
    rej_log = tmp_path / "feedback" / "rejections.jsonl"
    assert rej_log.exists()
    entries = [
        json.loads(line) for line in rej_log.read_text().strip().splitlines()
    ]
    assert any(e["proposal_id"] == pid for e in entries)


def test_snooze_proposal_sets_snoozed_until(tmp_path: Path):
    from arbiter import store as _store
    from evolve_admin.evo.arbiter_bridge import snooze_proposal

    pid = _persist_pending_investigation(tmp_path)
    result = snooze_proposal(tmp_path, pid, days=3)

    assert result.ok
    assert result.new_status == "snoozed"
    # Re-read the proposal from disk and verify snoozed_until
    located = _store.find_proposal(tmp_path, pid)
    assert located is not None
    proposal, _path, subdir = located
    assert subdir == "snoozed"
    assert proposal.snoozed_until  # ISO timestamp


def test_snooze_proposal_default_is_seven_days(tmp_path: Path):
    from datetime import datetime, timezone
    from arbiter import store as _store
    from evolve_admin.evo.arbiter_bridge import snooze_proposal

    pid = _persist_pending_investigation(tmp_path)
    result = snooze_proposal(tmp_path, pid)  # no days kwarg
    assert result.ok

    located = _store.find_proposal(tmp_path, pid)
    proposal = located[0]
    until = datetime.fromisoformat(proposal.snoozed_until)
    delta = until - datetime.now(timezone.utc)
    # Allow a few seconds of slop for the clock between now and the
    # snooze write.
    assert 6.9 < delta.total_seconds() / 86400 < 7.1


# ──────────────────────────────────────────────────────────────────────────
# proposal_id_for_rec helper
# ──────────────────────────────────────────────────────────────────────────


def test_proposal_id_for_rec_returns_id_when_source_ref_present():
    from evolve_admin.evo.arbiter_bridge import proposal_id_for_rec

    rec = {
        "id": "rec_abc",
        "source": "generator:budget_hawk",
        "source_ref": {"proposal_id": "p_xyz", "bot_id": "team_bot_a"},
    }
    assert proposal_id_for_rec(rec) == "p_xyz"


def test_proposal_id_for_rec_returns_none_for_non_proposal_recs():
    from evolve_admin.evo.arbiter_bridge import proposal_id_for_rec

    # Non-proposal adapters don't set source_ref.
    onboarding_rec = {"id": "rec_onb", "source": "onboarding"}
    whimsy_rec = {"id": "rec_w", "source": "whimsy", "source_ref": {}}
    bad = {"id": "rec_bad", "source_ref": "not-a-dict"}

    assert proposal_id_for_rec(onboarding_rec) is None
    assert proposal_id_for_rec(whimsy_rec) is None
    assert proposal_id_for_rec(bad) is None
    assert proposal_id_for_rec(None) is None  # type: ignore[arg-type]
    assert proposal_id_for_rec("string") is None  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────
# Asymmetric routing in _record_rec_action
# ──────────────────────────────────────────────────────────────────────────


def _proposal_rec(proposal_id: str, *, rec_id: str = "rec_p1") -> dict:
    return {
        "id": rec_id,
        "source": "generator:budget_hawk",
        "source_ref": {"proposal_id": proposal_id, "bot_id": "team_bot_a"},
    }


def _onboarding_rec(rec_id: str = "rec_onb") -> dict:
    return {"id": rec_id, "source": "onboarding"}


def test_record_rec_action_dispatches_proposal_recs_through_bridge(
    tmp_path: Path,
):
    """When the rec is proposal-derived, accept must invoke the bridge.
    We don't need a real BetterEngine — just verify the bridge call."""
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _proposal_rec("p_test_001")

    with patch(
        "evolve_admin.evo.arbiter_bridge.accept_proposal"
    ) as mock_accept, patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        engine_instance = MagicMock()
        MockEngine.return_value = engine_instance

        _record_rec_action(
            tmp_path,
            rec=rec,
            action="accept",
            snooze_days_hint=None,
            network={},
        )

        mock_accept.assert_called_once_with(tmp_path, "p_test_001")
        # Dual-write: BetterEngine.record_feedback also called for learning
        engine_instance.record_feedback.assert_called_once_with(
            "rec_p1", "accepted"
        )


def test_record_rec_action_skips_bridge_for_non_proposal_recs(tmp_path: Path):
    """Onboarding / scoreboard / compliance / whimsy recs have no
    proposal_id. The bridge must NOT be called for them — only
    BetterEngine.record_feedback fires."""
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _onboarding_rec()

    with patch(
        "evolve_admin.evo.arbiter_bridge.accept_proposal"
    ) as mock_accept, patch(
        "evolve_admin.evo.arbiter_bridge.dismiss_proposal"
    ) as mock_dismiss, patch(
        "evolve_admin.evo.arbiter_bridge.snooze_proposal"
    ) as mock_snooze, patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        engine_instance = MagicMock()
        MockEngine.return_value = engine_instance

        _record_rec_action(
            tmp_path,
            rec=rec,
            action="accept",
            snooze_days_hint=None,
            network={},
        )

        # No bridge calls for onboarding-source recs
        mock_accept.assert_not_called()
        mock_dismiss.assert_not_called()
        mock_snooze.assert_not_called()
        # But BetterEngine learning still fires
        engine_instance.record_feedback.assert_called_once_with(
            "rec_onb", "accepted"
        )


def test_record_rec_action_routes_reject_to_dismiss(tmp_path: Path):
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _proposal_rec("p_test_002")

    with patch(
        "evolve_admin.evo.arbiter_bridge.dismiss_proposal"
    ) as mock_dismiss, patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        MockEngine.return_value = MagicMock()
        _record_rec_action(
            tmp_path,
            rec=rec,
            action="reject",
            snooze_days_hint=None,
            network={},
        )
        mock_dismiss.assert_called_once_with(tmp_path, "p_test_002", reason="")


def test_record_rec_action_routes_next_to_dismiss_with_ignored_reason(
    tmp_path: Path,
):
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _proposal_rec("p_test_003")

    with patch(
        "evolve_admin.evo.arbiter_bridge.dismiss_proposal"
    ) as mock_dismiss, patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        MockEngine.return_value = MagicMock()
        _record_rec_action(
            tmp_path,
            rec=rec,
            action="next",
            snooze_days_hint=None,
            network={},
        )
        mock_dismiss.assert_called_once_with(
            tmp_path, "p_test_003", reason="ignored"
        )


def test_record_rec_action_routes_snooze_with_days_hint(tmp_path: Path):
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _proposal_rec("p_test_004")

    with patch(
        "evolve_admin.evo.arbiter_bridge.snooze_proposal"
    ) as mock_snooze, patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        MockEngine.return_value = MagicMock()
        _record_rec_action(
            tmp_path,
            rec=rec,
            action="snooze",
            snooze_days_hint=14,
            network={},
        )
        mock_snooze.assert_called_once_with(tmp_path, "p_test_004", days=14)


def test_record_rec_action_continues_on_bridge_failure(tmp_path: Path):
    """If the arbiter bridge raises, the wizard should still fall through
    to BetterEngine.record_feedback so the learning signal isn't lost.
    Bookkeeping must never wedge the chat session."""
    from evolve_admin.evo.wizard.engine import _record_rec_action

    rec = _proposal_rec("p_test_005")

    with patch(
        "evolve_admin.evo.arbiter_bridge.accept_proposal",
        side_effect=RuntimeError("simulated arbiter outage"),
    ), patch(
        "evolve_admin.better_engine.engine.BetterEngine"
    ) as MockEngine:
        engine_instance = MagicMock()
        MockEngine.return_value = engine_instance

        _record_rec_action(
            tmp_path,
            rec=rec,
            action="accept",
            snooze_days_hint=None,
            network={},
        )
        # Learning side still fires — the user's signal isn't lost
        engine_instance.record_feedback.assert_called_once_with(
            "rec_p1", "accepted"
        )
