"""Phase 3.4: dispatch_target / dispatch_message on Investigation proposals.

Tests that the audit_poller populates dispatch_target + dispatch_message on
tier3_finding records according to the spec mapping table, so the Phase 3.2
UI "Take this on" → evo dispatch flow has real targets to send to.

Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md
      §"audit_poller mapping (motivating example)"
      §"Generator-side conventions"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_outbox(tmp_root: Path, bot_user: str) -> Path:
    outbox = tmp_root / "Users" / bot_user / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _write_tier3(
    outbox: Path,
    *,
    bot_id: str,
    app_id: str,
    category: str,
    description: str,
    record_id: str = "rec-1",
    severity: str = "major",
) -> Path:
    rec = {
        "record_id": record_id,
        "audit_run_id": "run-1",
        "kind": "tier3_finding",
        "ts": "2026-06-03T00:00:00Z",
        "runner_version": "1.1.0",
        "producer": "app_audit_tier3",
        "bot_id": bot_id,
        "app_id": app_id,
        "signature": f"app_audit_tier3:{category}:{bot_id}:{app_id}:{record_id}",
        "obs_id": "obs-1",
        "category": category,
        "severity": severity,
        "description": description,
        "evidence": ["scripts/x.py:42"],
        "outcome": "propose",
        "rationale": "operator should review",
        "transformation_summary": "",
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def _load_emitted_proposal(shared: Path) -> dict:
    pending = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1, (
        f"expected exactly one proposal, got {len(pending)}: "
        f"{[p.name for p in pending]}"
    )
    return json.loads(pending[0].read_text())


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch) -> Path:
    """Re-point audit_poller's /Users paths into tmp_path."""

    def _audit_outbox(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user
            / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
        )

    def _audit_ingested(bot_user: str) -> Path:
        return _audit_outbox(bot_user) / "_ingested"

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(audit_poller, "_audit_outbox_ingested", _audit_ingested)
    return tmp_path


# ── Bot-dispatchable categories ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "category",
    ["broken_path", "missing_functionality", "behavior_mismatch",
     "dead_code", "manifest_drift"],
)
def test_bot_dispatchable_categories_set_dispatch_target_to_bot_id(
    tmp_root: Path, tmp_path: Path, category: str,
) -> None:
    """Categories whose fix lives in one bot's workspace get
    ``dispatch_target = bot_id`` plus a copy-paste-quality message."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    _write_tier3(
        outbox,
        bot_id="team-bot-a",
        app_id="journal",
        category=category,
        description="manifest claims feature X but the script is missing it",
    )

    audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    proposal = _load_emitted_proposal(shared)
    assert proposal["dispatch_target"] == "team-bot-a"
    message = proposal["dispatch_message"]
    assert isinstance(message, str) and message
    assert "journal" in message
    assert category in message
    assert "manifest claims feature X but the script is missing it" in message
    # Spec: trailing reference to the proposal id so the target can echo it.
    assert message.rstrip().endswith(f"Reference: proposal id {proposal['id']}.")


def test_broken_path_finding_dispatches_to_bot(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Spec table row 1: broken_path → dispatch_target = bot_id; message
    quotes the app name and the audit description."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    _write_tier3(
        outbox,
        bot_id="team-bot-a",
        app_id="ea-pack",
        category="broken_path",
        description=(
            "manifest references scripts/evening_sweep.py but the file is missing"
        ),
    )

    audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    proposal = _load_emitted_proposal(shared)
    assert proposal["dispatch_target"] == "team-bot-a"
    message = proposal["dispatch_message"]
    assert "ea-pack" in message
    assert "evening_sweep.py" in message
    assert "broken_path" in message
    assert f"Reference: proposal id {proposal['id']}." in message


# ── Operator-only categories ────────────────────────────────────────────────


def test_drift_finding_has_no_dispatch_target(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Spec table: drift (TAG_ALIASES, persona-style) → operator judgment,
    dispatch_target stays None."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    _write_tier3(
        outbox,
        bot_id="team-bot-a",
        app_id="persona-pack",
        category="drift",
        description="persona tone has shifted away from the manifest description",
    )

    audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    proposal = _load_emitted_proposal(shared)
    assert proposal["dispatch_target"] is None
    assert proposal["dispatch_message"] is None


def test_unknown_category_defaults_to_no_dispatch_target(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Anything not in the spec table → defensive default of None."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    _write_tier3(
        outbox,
        bot_id="team-bot-a",
        app_id="journal",
        category="unrecognized_future_category",
        description="future audit category not yet in the mapping table",
    )

    audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    proposal = _load_emitted_proposal(shared)
    assert proposal["dispatch_target"] is None
    assert proposal["dispatch_message"] is None


# ── Conflict notices stay operator-only ─────────────────────────────────────


def test_conflict_notice_has_no_dispatch_target(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Cross-app conflict notices need operator judgment (coordinate, drop,
    or accept) — they should NOT be dispatched to a single bot."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    rec = {
        "record_id": "rec-conflict-1",
        "audit_run_id": "run-1",
        "kind": "conflict_notice",
        "ts": "2026-06-03T00:00:00Z",
        "runner_version": "1.1.0",
        "producer": "app_audit_tier3",
        "bot_id": "team-bot-a",
        "app_id": "journal",
        "signature": "app_audit_tier3:cross_app_conflict:team-bot-a:journal:zzz",
        "obs_id": "obs-1",
        "category": "drift",
        "severity": "major",
        "description": "manifest_path_update would touch shared file",
        "evidence": ["scripts/shared.py"],
        "rationale": "",
        "summary": "cross-app conflict on scripts/shared.py",
        "file_path": "scripts/shared.py",
        "affected_apps": [
            {"pkg_id": "p-b", "app_id": "b", "display_name": "B", "role": "owner"},
        ],
    }
    (outbox / "rec-conflict-1.json").write_text(json.dumps(rec))

    audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    proposal = _load_emitted_proposal(shared)
    assert proposal["dispatch_target"] is None
    assert proposal["dispatch_message"] is None


# ── Helper-level coverage ───────────────────────────────────────────────────


def test_dispatch_helper_returns_none_when_bot_id_missing() -> None:
    """The resolver is defensive: a category from the mapping table without
    a bot_id still returns (None, None) rather than emitting "" as target."""
    target, message = audit_poller._dispatch_for_tier3_finding(
        {"category": "broken_path", "bot_id": "", "app_id": "journal",
         "description": "x"},
        proposal_id="p-test",
    )
    assert target is None
    assert message is None
