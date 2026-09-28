"""tests/test_heal_drift_coalesce.py — repeated config_drift incidents coalesce.

heal.py re-detects config_drift on every tick (default 5 min) until an
operator accepts baseline — the condition never self-resolves. The old
writer minted one timestamped file per detection, so a single persistent
drift bursted hundreds of identical files per day under
``{shared_dir}/incidents/<day>/`` (425 files on 2026-06-21 on the mini pod).
That production RATE is the cruft — "Evolve not being judicious".

The fix coalesces by signature = the drifted key-set: N identical drifts in
a window collapse onto ONE incident file carrying ``count`` +
``first_detected_at`` / ``last_detected_at``; a DIFFERENT key-set is a
genuinely-new drift and gets its own file (never lost).

Coverage:
- N identical config_drift detections in a window → 1 incident, count=N
- Distinct drifted key-sets → separate incidents (not collapsed)
- Coalesced record preserves first detection + bumps last_detected_at
- Default (no-signature) path is unchanged: one file per detection
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402


def _incident_files(shared_dir: Path, bot_id: str) -> list[Path]:
    out: list[Path] = []
    inc_dir = shared_dir / "incidents"
    if not inc_dir.exists():
        return out
    for day_dir in inc_dir.iterdir():
        if day_dir.is_dir():
            out.extend(day_dir.glob(f"{bot_id}-*-config_drift.json"))
    return out


def _record_drift(shared_dir: Path, bot_id: str, drifts: list[str]) -> None:
    heal._record_incident(
        shared_dir,
        heal.Incident(
            bot_id=bot_id,
            detected_at=heal.now_iso(),
            type="config_drift",
            detail="; ".join(drifts),
        ),
        signature=heal._config_drift_signature(drifts),
        coalesce_window_hours=24,
    )


def test_identical_drifts_coalesce_to_one_incident(tmp_path: Path) -> None:
    drifts = ["Unexplained config drift in alice: key 'agents' changed outside proposal pipeline"]

    for _ in range(5):
        _record_drift(tmp_path, "alice", drifts)

    files = _incident_files(tmp_path, "alice")
    assert len(files) == 1, f"expected one coalesced file, got {[f.name for f in files]}"

    data = json.loads(files[0].read_text())
    assert data["count"] == 5
    assert data["type"] == "config_drift"
    assert data["detail"] == drifts[0]
    assert data["first_detected_at"]
    assert data["last_detected_at"]


def test_distinct_keys_get_separate_incidents(tmp_path: Path) -> None:
    agents_drift = ["Unexplained config drift in alice: key 'agents' changed outside proposal pipeline"]
    plugins_drift = ["Unexplained config drift in alice: key 'plugins' changed outside proposal pipeline"]

    # Three of each, interleaved — the two key-sets must NOT collapse together.
    for _ in range(3):
        _record_drift(tmp_path, "alice", agents_drift)
        _record_drift(tmp_path, "alice", plugins_drift)

    files = _incident_files(tmp_path, "alice")
    assert len(files) == 2, f"expected two distinct incidents, got {[f.name for f in files]}"

    records = [json.loads(f.read_text()) for f in files]
    assert sorted(r["count"] for r in records) == [3, 3]

    sigs = {r["signature"] for r in records}
    assert len(sigs) == 2, "distinct key-sets must produce distinct signatures"


def test_coalesce_preserves_first_and_bumps_last(tmp_path: Path) -> None:
    drifts = ["Unexplained config drift in alice: key 'agents' changed outside proposal pipeline"]

    _record_drift(tmp_path, "alice", drifts)
    first = json.loads(_incident_files(tmp_path, "alice")[0].read_text())

    _record_drift(tmp_path, "alice", drifts)
    after = json.loads(_incident_files(tmp_path, "alice")[0].read_text())

    assert after["count"] == 2
    assert after["first_detected_at"] == first["first_detected_at"]
    # last_detected_at advances (or at minimum never goes backwards).
    assert after["last_detected_at"] >= first["last_detected_at"]


def test_distinct_bots_do_not_coalesce(tmp_path: Path) -> None:
    drifts = ["Unexplained config drift: key 'agents' changed outside proposal pipeline"]
    _record_drift(tmp_path, "alice", drifts)
    _record_drift(tmp_path, "bob", drifts)

    assert len(_incident_files(tmp_path, "alice")) == 1
    assert len(_incident_files(tmp_path, "bob")) == 1


def test_default_path_unchanged_one_file_per_detection(tmp_path: Path) -> None:
    # Without a signature the writer keeps minting one file per detection.
    for i in range(3):
        heal._record_incident(
            tmp_path,
            heal.Incident(
                bot_id="alice",
                detected_at=heal.now_iso(),
                type="gateway_down",
                port=19000,
                detail=f"down {i}",
            ),
        )
    inc_dir = tmp_path / "incidents"
    files = [
        f
        for day_dir in inc_dir.iterdir()
        if day_dir.is_dir()
        for f in day_dir.glob("alice-*-gateway_down.json")
    ]
    assert len(files) == 3
    # Legacy shape: no coalescing fields leaked in.
    for f in files:
        data = json.loads(f.read_text())
        assert "count" not in data
        assert "signature" not in data
