"""outcome.process_reply — general-path 👍/👎 → detector-confidence loop.

Regression / feature: an operator's thumbs-up/down on an ordinary
detector-triggered proposal used to be recorded into ``outcomes.jsonl`` and
then dropped on the floor — nothing ever adjusted the detector's confidence.
The forge path (``forge_jobs.approve_job``/``reject_job``) already nudged
``confidence_multiplier`` on approve/reject; ``process_reply`` now mirrors that
shape so the general path closes the same loop.

The end-to-end proof is ``cal.effective_confidence(detector)`` moving in the
right direction after a reply.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import outcome  # noqa: E402
from calibration import CalibrationLoader  # noqa: E402


_DETECTOR = "high_maintenance_ratio"


def _seed_pending(shared: Path, outcome_id: str, detector_name: str) -> None:
    outcome.write_pending_outcome(
        {
            "schema_version": 1,
            "outcome_id": outcome_id,
            "detector_name": detector_name,
            "applied_date": "2026-07-01",
            "checkin_sent": True,
            "checkin_sent_date": "2026-07-08",
        },
        str(shared),
    )


def test_thumbs_down_lowers_effective_confidence(tmp_path):
    # Seed enough prior judgements that one more thumbs_down crosses the
    # 3-judgement floor with a <0.40 positive rate → multiplier drops.
    cal = CalibrationLoader(str(tmp_path))
    cal.set_detector_outcome_stat(_DETECTOR, "thumbs_up", 0)
    cal.set_detector_outcome_stat(_DETECTOR, "thumbs_down", 2)
    before = cal.effective_confidence(_DETECTOR)

    _seed_pending(tmp_path, "dddddddd-0000", _DETECTOR)
    assert outcome.process_reply("dddddddd", "thumbs_down", str(tmp_path)) is True

    after = CalibrationLoader(str(tmp_path)).effective_confidence(_DETECTOR)
    assert after < before


def test_thumbs_up_raises_effective_confidence(tmp_path):
    # Seed 2 prior thumbs_up; one more → 3/3 positive (rate 1.0 > 0.70) →
    # multiplier climbs → effective_confidence rises.
    cal = CalibrationLoader(str(tmp_path))
    cal.set_detector_outcome_stat(_DETECTOR, "thumbs_up", 2)
    cal.set_detector_outcome_stat(_DETECTOR, "thumbs_down", 0)
    before = cal.effective_confidence(_DETECTOR)

    _seed_pending(tmp_path, "uuuuuuuu-0000", _DETECTOR)
    assert outcome.process_reply("uuuuuuuu", "thumbs_up", str(tmp_path)) is True

    after = CalibrationLoader(str(tmp_path)).effective_confidence(_DETECTOR)
    assert after > before


def test_thumbs_up_increments_stat_counter(tmp_path):
    cal = CalibrationLoader(str(tmp_path))
    cal.set_detector_outcome_stat(_DETECTOR, "thumbs_up", 0)

    _seed_pending(tmp_path, "cccccccc-0000", _DETECTOR)
    assert outcome.process_reply("cccccccc", "thumbs_up", str(tmp_path)) is True

    stats = CalibrationLoader(str(tmp_path)).detector(_DETECTOR).get("outcome_stats", {})
    assert stats.get("thumbs_up") == 1


def test_empty_detector_name_is_noop(tmp_path):
    # No detector_name → reply still records, but no calibration file is
    # touched (no local detectors.json written, no crash).
    _seed_pending(tmp_path, "eeeeeeee-0000", "")
    assert outcome.process_reply("eeeeeeee", "thumbs_down", str(tmp_path)) is True

    # The result was recorded to outcomes.jsonl (loop still ran to completion).
    assert (tmp_path / "feedback" / "outcomes.jsonl").exists()
    # No detector calibration was written.
    assert not (tmp_path / "calibration" / "detectors.json").exists()
