"""Tests for the auto-response policy persistence (Phase 5 of Issue Inbox).

The policy is a small JSON blob at ``{shared_dir}/inbox_policy.json``;
defaults are "everything off" so a fresh install never auto-responds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


def test_default_policy_is_everything_off():
    """A fresh install must NOT auto-respond. Each enable flag stays
    False until the operator explicitly opts in."""
    from evolve_admin.intake.policy import AutoResponsePolicy
    p = AutoResponsePolicy()
    assert p.enabled is False
    assert p.close_duplicate_enabled is False
    assert p.reply_clarifying_enabled is False
    assert p.label_only_enabled is False


def test_default_min_confidence_floors_are_high():
    """Per-kind floors must be conservative — auto-closing someone
    else's issue is high-stakes, so the floor should be above the
    classifier's typical 'pretty sure' range."""
    from evolve_admin.intake.policy import AutoResponsePolicy
    p = AutoResponsePolicy()
    assert p.close_duplicate_min_confidence >= 0.85
    assert p.reply_clarifying_min_confidence >= 0.8


def test_load_missing_file_returns_defaults(tmp_path):
    """Missing policy file is the fresh-install state, not an error."""
    from evolve_admin.intake.policy import load_policy
    p = load_policy(tmp_path)
    assert p.enabled is False


def test_load_malformed_file_returns_defaults(tmp_path):
    """A corrupt policy file must NEVER cause auto-responses to fire
    unexpectedly. Fall back to defaults (everything off)."""
    from evolve_admin.intake.policy import load_policy
    (tmp_path / "inbox_policy.json").write_text("not json {")
    p = load_policy(tmp_path)
    assert p.enabled is False


def test_save_then_load_roundtrip(tmp_path):
    from evolve_admin.intake.policy import (
        AutoResponsePolicy, load_policy, save_policy,
    )
    p = AutoResponsePolicy(
        enabled=True,
        close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.95,
        note="enabled after two weeks of observation",
    )
    save_policy(tmp_path, p)
    loaded = load_policy(tmp_path)
    assert loaded.enabled is True
    assert loaded.close_duplicate_enabled is True
    assert loaded.close_duplicate_min_confidence == 0.95
    assert loaded.note == "enabled after two weeks of observation"


def test_confidence_clamps_to_valid_range(tmp_path):
    """Confidence is a probability — values outside [0,1] must clamp."""
    from evolve_admin.intake.policy import AutoResponsePolicy
    p = AutoResponsePolicy.from_dict({
        "close_duplicate_min_confidence": 1.5,
        "reply_clarifying_min_confidence": -0.3,
    })
    assert p.close_duplicate_min_confidence == 1.0
    assert p.reply_clarifying_min_confidence == 0.0


def test_garbage_confidence_falls_back_to_default():
    from evolve_admin.intake.policy import (
        AutoResponsePolicy,
        DEFAULT_CLOSE_DUPLICATE_MIN_CONFIDENCE,
    )
    p = AutoResponsePolicy.from_dict({
        "close_duplicate_min_confidence": "not a float",
    })
    assert p.close_duplicate_min_confidence == DEFAULT_CLOSE_DUPLICATE_MIN_CONFIDENCE


def test_atomic_save_does_not_leave_temp_files(tmp_path):
    """The save path uses temp-file + rename. There should be no
    leftover *.tmp files after a successful save."""
    from evolve_admin.intake.policy import AutoResponsePolicy, save_policy
    save_policy(tmp_path, AutoResponsePolicy(enabled=True))
    leftovers = list(tmp_path.glob(".inbox-policy-*"))
    assert leftovers == []


def test_save_file_is_valid_json(tmp_path):
    from evolve_admin.intake.policy import AutoResponsePolicy, save_policy
    save_policy(tmp_path, AutoResponsePolicy(enabled=True))
    data = json.loads((tmp_path / "inbox_policy.json").read_text())
    assert data["enabled"] is True


def test_unknown_extra_fields_ignored(tmp_path):
    """Forward-compat: a future-schema field shouldn't break load."""
    from evolve_admin.intake.policy import load_policy
    (tmp_path / "inbox_policy.json").write_text(json.dumps({
        "enabled": True,
        "some_future_field": "ignored",
    }))
    p = load_policy(tmp_path)
    assert p.enabled is True
