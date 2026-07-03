"""apply.py::_register_outcome — pending-outcome record actually gets written.

Regression pin: _register_outcome's whole body is wrapped in a broad
``except Exception`` that downgrades any failure to a "Warning: could not
register outcome" print line. A missing ``date`` import (NameError) was
swallowed this way for two months, which meant no pending-outcome record
was ever written and outcome.py's 7-day check-in never fired.

These tests run the function for real (no mocking of the code under
test), so any exception inside the body — import error, NameError,
schema drift — surfaces as a missing record / missing call, not as a
silently-passing warning line.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import apply as apply_mod  # noqa: E402
import outcome  # noqa: E402


def _proposal() -> dict:
    return {
        "id": "prop-123",
        "type": "config_patch",
        "problem": "Bot responses are slow during peak hours",
        "pattern_key": "bot:slow_responses",
    }


def _result() -> apply_mod.ApplyResult:
    return apply_mod.ApplyResult(
        proposal_id="prop-123", bot_id="team_bot_a", success=True,
    )


def test_register_outcome_writes_pending_record(tmp_path, capsys):
    """End-to-end: record lands in feedback/pending-outcomes.jsonl."""
    apply_mod._register_outcome(_proposal(), _result(), "team_bot_a", tmp_path)

    out = capsys.readouterr().out
    assert "could not register outcome" not in out
    assert "Outcome registered" in out

    records = outcome.load_pending_outcomes(str(tmp_path))
    assert len(records) == 1
    rec = records[0]
    assert rec["proposal_id"] == "prop-123"
    assert rec["target_bot"] == "team_bot_a"
    assert rec["applied_date"] == date.today().isoformat()
    assert rec["detector_name"] == "slow_responses"
    assert rec["proposal_type"] == "config_patch"
    assert rec["checkin_sent"] is False
    assert rec["checkin_sent_date"] is None
    assert rec["outcome_id"]


def test_register_outcome_reaches_writer(tmp_path, monkeypatch):
    """The writer is actually invoked — pins the swallowed-exception class.

    If anything in the body raises before write_pending_outcome (the
    historical NameError, a bad import), the fake is never called and
    this fails loudly instead of printing a warning.
    """
    captured: list[tuple[dict, str]] = []
    monkeypatch.setattr(
        outcome, "write_pending_outcome",
        lambda rec, shared: captured.append((rec, shared)),
    )

    apply_mod._register_outcome(_proposal(), _result(), "team_bot_a", tmp_path)

    assert len(captured) == 1
    rec, shared = captured[0]
    assert shared == str(tmp_path)
    assert rec["schema_version"] == 1
    assert rec["proposal_summary"].startswith("Bot responses are slow")
