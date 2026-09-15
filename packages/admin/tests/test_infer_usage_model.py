"""tests/test_infer_usage_model.py — usage.model inference rules.

`app_registry.infer_usage_model` is called from two places: the
INSTALLED_APPS.md renderer (display fallback when usage.model is unset)
and forge Phase 5a.0 (persist the inferred value into the manifest before
the discoverability check fires the no_invocation_model finding).

Rule precedence:
  1. scheduled_actions present → "scheduled"
  2. event_triggers present → "event-driven"
  3. interface_contract.cli has a non-empty command → "user-initiated"
  4. fallback → "user-initiated" (permissive)

These tests pin the precedence + each cue.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))

from evolve_admin.applications.app_registry import infer_usage_model  # noqa: E402


# ── Single-cue tests ───────────────────────────────────────────────────────


def test_scheduled_actions_with_launchd_yields_scheduled() -> None:
    manifest = {"scheduled_actions": [{"id": "x", "mechanism": "launchd"}]}
    assert infer_usage_model(manifest) == "scheduled"


def test_heartbeat_instruction_action_doesnt_count_as_scheduled() -> None:
    """Heartbeat instructions are passive nudges (remind the bot to consider
    this app's outputs), not real timers. An app whose ONLY scheduled action
    is a heartbeat instruction is fundamentally user-routed — the user adds
    the things; the heartbeat just nags the bot to surface them. Catches
    real gallery shape (commitment-tracker, task-manager)."""
    manifest = {
        "scheduled_actions": [
            {"id": "x", "mechanism": "oc_heartbeat_instruction"},
            {"id": "y", "mechanism": "oc_heartbeat_instruction"},
        ],
        "interface_contract": {"cli": [{"command": "add"}]},
    }
    assert infer_usage_model(manifest) == "user-initiated"


def test_mixed_heartbeat_plus_launchd_yields_scheduled() -> None:
    """A real launchd timer alongside heartbeat instructions is still
    scheduled — the timer is the primary surface."""
    manifest = {
        "scheduled_actions": [
            {"id": "x", "mechanism": "oc_heartbeat_instruction"},
            {"id": "y", "mechanism": "launchd"},
        ],
    }
    assert infer_usage_model(manifest) == "scheduled"


def test_event_triggers_present_yields_event_driven() -> None:
    manifest = {"event_triggers": [{"id": "url_in_msg"}]}
    assert infer_usage_model(manifest) == "event-driven"


def test_cli_present_yields_user_initiated() -> None:
    manifest = {
        "interface_contract": {
            "cli": [{"command": "journal-add"}],
        },
    }
    assert infer_usage_model(manifest) == "user-initiated"


def test_empty_manifest_defaults_to_user_initiated() -> None:
    """Permissive fallback — if nothing structural is set, assume user-routed
    so the audit's routing-floor checks still apply."""
    assert infer_usage_model({}) == "user-initiated"


# ── Precedence tests (multiple cues present) ───────────────────────────────


def test_scheduled_wins_over_cli() -> None:
    """A scheduled app with an off-cycle CLI for previews / status is still
    primarily scheduled — the cron is where the value lands."""
    manifest = {
        "scheduled_actions": [{"id": "x", "mechanism": "launchd"}],
        "interface_contract": {"cli": [{"command": "morning-briefing preview"}]},
    }
    assert infer_usage_model(manifest) == "scheduled"


def test_event_driven_wins_over_cli() -> None:
    """Event-driven apps usually expose a CLI for handler dispatch; the
    primary trigger is still the event."""
    manifest = {
        "event_triggers": [{"id": "url_in_msg"}],
        "interface_contract": {"cli": [{"command": "atlas_capture process"}]},
    }
    assert infer_usage_model(manifest) == "event-driven"


def test_scheduled_wins_over_event_driven() -> None:
    """If somehow both are set, scheduled takes precedence — closer match
    for the "primary value source" intent."""
    manifest = {
        "scheduled_actions": [{"id": "x", "mechanism": "launchd"}],
        "event_triggers": [{"id": "y"}],
    }
    assert infer_usage_model(manifest) == "scheduled"


# ── Edge cases on cue detection ─────────────────────────────────────────────


def test_empty_list_doesnt_count_as_cue() -> None:
    """A scheduled_actions=[] field shouldn't trip the scheduled branch —
    that's "field exists but unused", not "is scheduled"."""
    manifest = {
        "scheduled_actions": [],
        "event_triggers": [],
        "interface_contract": {"cli": [{"command": "foo"}]},
    }
    assert infer_usage_model(manifest) == "user-initiated"


def test_cli_entry_without_command_doesnt_count() -> None:
    """An interface_contract.cli entry missing its command is hollow —
    no actual invocation surface."""
    manifest = {
        "interface_contract": {"cli": [{"key_flags": ["--text"]}]},  # no command
    }
    assert infer_usage_model(manifest) == "user-initiated"


def test_cli_entry_with_whitespace_command_doesnt_count() -> None:
    """Likewise a whitespace-only command — strip().truthiness gates."""
    manifest = {
        "interface_contract": {"cli": [{"command": "   "}]},
    }
    assert infer_usage_model(manifest) == "user-initiated"


def test_non_dict_cli_entry_is_skipped() -> None:
    """Defensive: a stray string in cli[] shouldn't crash the inference."""
    manifest = {
        "interface_contract": {"cli": ["not-a-dict", {"command": "real-cmd"}]},
    }
    assert infer_usage_model(manifest) == "user-initiated"


# ── Round-trip against the gallery packages ────────────────────────────────


def test_inference_matches_authored_gallery_models() -> None:
    """Sanity check the rules against the 13 hand-authored gallery packages.
    Each manifest's authored usage.model should equal what the rules infer —
    if not, either the rules are wrong or the gallery backfill misclassified
    something."""
    import glob
    import json
    import os

    # Use the worktree's gallery dir; the test is portable across worktrees
    # via the relative path from this file (packages/admin/tests/ → ../../gallery).
    repo_root = Path(__file__).parent.parent.parent.parent
    gallery_dir = repo_root / "gallery"
    if not gallery_dir.exists():
        # Repo layout sometimes nests differently in CI; skip rather than fail.
        import pytest
        pytest.skip(f"gallery dir not found at {gallery_dir}")

    pkgs = sorted(glob.glob(str(gallery_dir / "*" / "p-*.json")))
    assert pkgs, "expected gallery packages on disk"

    mismatches = []
    for p in pkgs:
        data = json.loads(Path(p).read_text())
        usage = data.get("usage") or {}
        authored = str(usage.get("model") or "").strip()
        if not authored:
            # If the backfill PR hasn't reached this checkout yet, skip.
            continue
        inferred = infer_usage_model(data)
        # ambient is a judgment call with no structural cue; skip those.
        if authored == "ambient":
            continue
        if authored != inferred:
            mismatches.append({
                "pkg": os.path.basename(os.path.dirname(p)),
                "authored": authored,
                "inferred": inferred,
            })
    assert not mismatches, (
        "rules-vs-authored mismatch on gallery packages — either the rules "
        f"are wrong or the gallery backfill misclassified: {mismatches}"
    )
