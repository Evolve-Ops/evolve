"""tests/test_evo_lineage_in_pitches.py — Lineage in chat pitches.

Step 5 of the evo unify plan. When a proposal has same-fingerprint
history (prior dismissals, prior approvals, prior verify failures),
the bot's pitch should mention it naturally — closes the recursive
loop visible in the admin UI's lineage chip.

Three concerns:

1. **Bridge: proposal_lineage_summary**
   Walks the archive, returns a chat-friendly summary dict with counts
   per status and the most recent timestamp per status. Returns None
   when there's no history (the common case — most proposals are
   first-shot).

2. **Engine: enrichment**
   ``_enrich_with_lineage`` attaches the summary to the rec dict
   before it's stashed on state. Wired into start_rec_pending,
   chain-to-next, and refine-refresh paths.

3. **Prompts: lineage clause**
   ``_format_lineage_clause`` renders the summary into a one-line cue
   the LLM weaves into its pitch. Tells the bot to acknowledge prior
   history naturally rather than as a footnote.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures — persist a proposal + same-fingerprint archived predecessors
# ──────────────────────────────────────────────────────────────────────────


_FIXED_TRIGGER = "obs-shared-test-fingerprint"


def _persist_pending_investigation(tmp_path: Path) -> str:
    """Persist an Investigation with a FIXED trigger_observations entry
    so predecessors built with the same trigger share its fingerprint.
    Default ``make_investigation_proposal`` randomizes triggers."""
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(bot_id="team_bot_a")
    p.trigger_observations = [_FIXED_TRIGGER]  # pin for fingerprint stability
    _sm.transition(p, "pending", actor="test")
    _store.write_proposal(p, tmp_path, subdir="pending")
    return p.id


def _archive_predecessor(
    tmp_path: Path, *, original_id: str, terminal_status: str
) -> str:
    """Create a same-fingerprint predecessor in archived/ with the
    given terminal status. Pins trigger_observations to match the
    original — Investigation has no other fingerprint inputs that vary
    by construction (no target_path / metric)."""
    from arbiter import state_machine as _sm
    from arbiter import store as _store
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(bot_id="team_bot_a")
    p.trigger_observations = [_FIXED_TRIGGER]
    _sm.transition(p, "pending", actor="test")

    # Walk to the requested terminal status via legal transitions.
    if terminal_status in (
        "succeeded",
        "failed_reverted",
        "failed_flagged",
        "failed_revert_failed",
    ):
        _sm.transition(p, "approved_human", actor="user", reason="t")
        _sm.transition(p, "applied", actor="user")
        _sm.transition(p, terminal_status, actor="test")
    elif terminal_status in ("dismissed", "rejected"):
        _sm.transition(p, terminal_status, actor="user")
    elif terminal_status == "superseded":
        _sm.transition(p, "superseded", actor="test")
    elif terminal_status == "resolved_externally":
        _sm.transition(p, "resolved_externally", actor="test")

    _store.write_proposal(p, tmp_path, subdir="archived")
    return p.id


# ──────────────────────────────────────────────────────────────────────────
# Bridge: proposal_lineage_summary
# ──────────────────────────────────────────────────────────────────────────


def test_lineage_summary_returns_none_when_no_history(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import proposal_lineage_summary

    pid = _persist_pending_investigation(tmp_path)
    assert proposal_lineage_summary(tmp_path, pid) is None


def test_lineage_summary_returns_none_for_unknown_proposal(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import proposal_lineage_summary

    assert proposal_lineage_summary(tmp_path, "no-such-id") is None


def test_lineage_summary_groups_by_status(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import proposal_lineage_summary

    pid = _persist_pending_investigation(tmp_path)
    _archive_predecessor(tmp_path, original_id=pid, terminal_status="dismissed")
    _archive_predecessor(tmp_path, original_id=pid, terminal_status="dismissed")
    _archive_predecessor(tmp_path, original_id=pid, terminal_status="succeeded")

    summary = proposal_lineage_summary(tmp_path, pid)
    assert summary is not None
    assert summary["total"] == 3
    assert summary["by_status"]["dismissed"] == 2
    assert summary["by_status"]["succeeded"] == 1
    # Latest timestamps captured per status
    assert "dismissed" in summary["latest"]
    assert "succeeded" in summary["latest"]


def test_lineage_summary_caps_at_max_entries(tmp_path: Path):
    from evolve_admin.evo.arbiter_bridge import proposal_lineage_summary

    pid = _persist_pending_investigation(tmp_path)
    for _ in range(7):
        _archive_predecessor(
            tmp_path, original_id=pid, terminal_status="dismissed"
        )

    summary = proposal_lineage_summary(tmp_path, pid, max_entries=3)
    assert summary is not None
    # At most max_entries entries returned, even though all 7 are same-fp.
    assert summary["total"] == 3


# ──────────────────────────────────────────────────────────────────────────
# Prompts: _format_lineage_clause
# ──────────────────────────────────────────────────────────────────────────


def test_lineage_clause_empty_when_no_summary():
    from evolve_admin.evo.wizard.prompts import _format_lineage_clause

    assert _format_lineage_clause({}) == ""
    assert _format_lineage_clause({"_lineage_summary": None}) == ""
    assert _format_lineage_clause({"_lineage_summary": {}}) == ""


def test_lineage_clause_renders_singular_and_plural():
    from evolve_admin.evo.wizard.prompts import _format_lineage_clause

    rec = {
        "_lineage_summary": {
            "total": 3,
            "by_status": {"dismissed": 2, "succeeded": 1},
            "latest": {},
        }
    }
    clause = _format_lineage_clause(rec)
    assert clause  # non-empty
    assert "dismissed twice" in clause
    assert "approved and verified once" in clause


def test_lineage_clause_includes_recency():
    from datetime import datetime, timedelta, timezone

    from evolve_admin.evo.wizard.prompts import _format_lineage_clause

    twelve_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=12)
    ).isoformat(timespec="seconds")
    rec = {
        "_lineage_summary": {
            "total": 1,
            "by_status": {"dismissed": 1},
            "latest": {"dismissed": twelve_days_ago},
        }
    }
    clause = _format_lineage_clause(rec)
    assert "12 days ago" in clause


def test_lineage_clause_renders_as_user_facing_footnote():
    """Under the 2026-05-17 verbatim-render conversion the clause is no
    longer an LLM directive ("acknowledge naturally") — it's a one-line
    user-facing footnote prefixed with 'Prior history:' that lands
    directly in the pitch the user sees."""
    from evolve_admin.evo.wizard.prompts import _format_lineage_clause

    rec = {
        "_lineage_summary": {
            "total": 2,
            "by_status": {"dismissed": 2},
            "latest": {},
        }
    }
    clause = _format_lineage_clause(rec)
    assert clause.startswith("\n\nPrior history:")
    assert "dismissed twice" in clause
    # No leftover LLM-directive framing.
    assert "Acknowledge" not in clause
    assert "naturally" not in clause


def test_lineage_clause_warns_on_prior_failure():
    from evolve_admin.evo.wizard.prompts import _format_lineage_clause

    rec = {
        "_lineage_summary": {
            "total": 1,
            "by_status": {"failed_flagged": 1},
            "latest": {},
        }
    }
    clause = _format_lineage_clause(rec)
    assert "warn" in clause.lower() or "verify failed" in clause.lower()


# ──────────────────────────────────────────────────────────────────────────
# Engine: enrichment wires through to the pitch
# ──────────────────────────────────────────────────────────────────────────


def test_engine_enrich_attaches_lineage_for_proposal_recs(tmp_path: Path):
    from evolve_admin.evo.wizard.engine import _enrich_with_lineage

    rec = {
        "id": "rec_p1",
        "source": "generator:budget_hawk",
        "source_ref": {"proposal_id": "p_test_001", "bot_id": "team_bot_a"},
    }
    fake_summary = {
        "total": 2,
        "by_status": {"dismissed": 2},
        "latest": {},
    }
    with patch(
        "evolve_admin.evo.arbiter_bridge.proposal_lineage_summary",
        return_value=fake_summary,
    ):
        enriched = _enrich_with_lineage(tmp_path, dict(rec))

    assert enriched.get("_lineage_summary") == fake_summary


def test_engine_enrich_skips_non_proposal_recs(tmp_path: Path):
    from evolve_admin.evo.wizard.engine import _enrich_with_lineage

    # Non-proposal rec (no source_ref → no proposal_id)
    rec = {
        "id": "rec_onb",
        "source": "onboarding",
    }
    with patch(
        "evolve_admin.evo.arbiter_bridge.proposal_lineage_summary"
    ) as mock_summary:
        enriched = _enrich_with_lineage(tmp_path, dict(rec))

    assert "_lineage_summary" not in enriched
    mock_summary.assert_not_called()


def test_engine_enrich_handles_summary_failure_silently(tmp_path: Path):
    """Bridge errors don't crash the wizard. Rec is returned untouched."""
    from evolve_admin.evo.wizard.engine import _enrich_with_lineage

    rec = {
        "id": "rec_p1",
        "source_ref": {"proposal_id": "p_test", "bot_id": "team_bot_a"},
    }
    with patch(
        "evolve_admin.evo.arbiter_bridge.proposal_lineage_summary",
        side_effect=RuntimeError("simulated"),
    ):
        enriched = _enrich_with_lineage(tmp_path, dict(rec))

    assert "_lineage_summary" not in enriched


def test_pitch_includes_lineage_when_summary_present():
    """End-to-end on the prompt builder: rec carries
    _lineage_summary → pitch text mentions prior history."""
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = {
        "id": "rec_test",
        "title": "Watch team_bot_c spend",
        "detail": "spend has crossed cap",
        "context": "",
        "tags": [],
        "action_kind": "Investigation",
        "_lineage_summary": {
            "total": 2,
            "by_status": {"dismissed": 2},
            "latest": {},
        },
    }
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")
    assert "Prior history" in text
    assert "dismissed twice" in text


def test_pitch_omits_lineage_when_summary_absent():
    """Backward-compat: recs without lineage info still pitch cleanly,
    no dangling headers."""
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = {
        "id": "rec_test",
        "title": "Fresh proposal",
        "detail": "no history",
        "context": "",
        "tags": [],
        "action_kind": "Investigation",
    }
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")
    assert "Prior history" not in text
