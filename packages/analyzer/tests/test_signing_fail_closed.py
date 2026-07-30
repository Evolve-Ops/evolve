"""Fail-closed proposal-signature verification (roadmap item 0.3).

Before this change all three verifiers (proposal / review-stamp / forge-result)
returned True when the signing key was absent — a fail-OPEN default: deleting one
file in the keystore silently disabled proposal authentication pod-wide.

Now verification fails CLOSED once signing has been enabled on the pod (a marker
that self-heals from any successful key load), while a genuinely fresh,
never-keyed pod still bootstraps open.
"""

from __future__ import annotations

import importlib

import pytest

evolve_config = importlib.import_module("evolve_config")


@pytest.fixture
def keystore(tmp_path, monkeypatch):
    """Point the key + enforcement-marker paths at an isolated tmp dir."""
    key_path = tmp_path / "keystore" / "evolve-signing.key"
    marker_path = tmp_path / ".proposal-signing-enabled"
    monkeypatch.setattr(evolve_config, "SIGNING_KEY_PATH", key_path)
    monkeypatch.setattr(evolve_config, "SIGNING_ENFORCED_MARKER", marker_path)
    return key_path, marker_path


def _signed_proposal() -> dict:
    p = {
        "id": "p-123",
        "type": "config_change",
        "target_bot": "team_bot_a",
        "pattern_key": "cost.high_tier",
        "proposed_change": {"field": "model", "to": "tier3"},
        "generated": "2026-06-09T00:00:00Z",
    }
    p["evolve_sig"] = evolve_config.sign_proposal(p)
    return p


# ── happy path ────────────────────────────────────────────────────────────────


def test_valid_signature_passes_and_latches_enforcement(keystore):
    _key, marker = keystore
    evolve_config.generate_signing_key()
    assert marker.exists(), "generating a key must enable enforcement"

    proposal = _signed_proposal()
    assert evolve_config.verify_proposal_sig(proposal) is True


def test_tampered_payload_fails_when_key_present(keystore):
    evolve_config.generate_signing_key()
    proposal = _signed_proposal()
    proposal["proposed_change"] = {"field": "model", "to": "tier1"}  # post-sign edit
    assert evolve_config.verify_proposal_sig(proposal) is False


def test_missing_sig_field_fails_when_key_present(keystore):
    evolve_config.generate_signing_key()
    proposal = _signed_proposal()
    del proposal["evolve_sig"]
    assert evolve_config.verify_proposal_sig(proposal) is False


# ── the fix: fail-open → fail-closed ───────────────────────────────────────────


def test_fresh_pod_bootstraps_open(keystore):
    """No key has ever existed and no marker: allow (pre-setup bootstrap)."""
    _key, marker = keystore
    assert not marker.exists()
    assert evolve_config.verify_proposal_sig({"id": "p-1"}) is True


def test_enforced_pod_with_missing_key_fails_closed(keystore):
    """Signing was enabled, then the key went missing: refuse, do not fail open."""
    _key, marker = keystore
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n")  # enforcement latched, key absent
    assert evolve_config.verify_proposal_sig({"id": "p-1", "evolve_sig": "deadbeef"}) is False


def test_review_stamp_and_forge_result_also_fail_closed(keystore):
    _key, marker = keystore
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n")
    assert evolve_config.verify_review_stamp({"id": "p-1", "review_stamp": {"sig": "x"}}) is False
    assert evolve_config.verify_forge_result_sig({"proposal_id": "p-1", "forge_sig": "x"}) is False


def test_self_heal_latches_enforcement_from_a_successful_load(keystore):
    """A pod that already has a working key (but predates the marker) latches
    enforcement the first time the key is loaded — no deploy/migration needed."""
    key_path, marker = keystore
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("ab" * 32)  # valid hex key, no marker yet (legacy pod)
    assert not marker.exists()

    assert evolve_config._load_signing_key() is not None
    assert marker.exists(), "loading a usable key must latch enforcement on"
