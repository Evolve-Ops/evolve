"""Pre-deploy coherence gate tests — PR 8.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.6.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.pre_deploy_gate import (  # noqa: E402
    MODE_BLOCKER,
    MODE_OFF,
    MODE_WARNING,
    STATUS_INCOHERENT,
    STATUS_OK,
    STATUS_WARNINGS,
    VALID_MODES,
    GateVerdict,
    check_pre_deploy,
    forge_approval_gate,
    manifest_editor_gate,
    scanner_write_gate,
)


# ── Mode constants ────────────────────────────────────────────────────────

def test_mode_constants_in_valid_set() -> None:
    assert MODE_BLOCKER in VALID_MODES
    assert MODE_WARNING in VALID_MODES
    assert MODE_OFF in VALID_MODES
    assert len(VALID_MODES) == 3


# ── Off mode ──────────────────────────────────────────────────────────────

def test_off_mode_always_allows() -> None:
    """MODE_OFF skips Pass A entirely — even a clearly incoherent
    manifest is allowed."""
    manifest = {
        "description": "daily briefing",   # claims recurring behavior
        "scheduled_actions": [], "crons": [],   # no triggers
    }
    verdict = check_pre_deploy(manifest, mode=MODE_OFF)
    assert verdict.allowed is True
    assert verdict.status == STATUS_OK
    assert verdict.findings == []


# ── Healthy manifest ──────────────────────────────────────────────────────

def test_healthy_manifest_allows_in_blocker_mode() -> None:
    manifest = {
        "id": "x", "description": "Simple data manager.",
        "files": [], "scheduled_actions": [], "crons": [],
        "requirements": {"integrations": []},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert verdict.allowed is True
    assert verdict.status == STATUS_OK


def test_healthy_manifest_allows_in_warning_mode() -> None:
    manifest = {"id": "x", "description": "ok",
                "scheduled_actions": [], "crons": []}
    verdict = check_pre_deploy(manifest, mode=MODE_WARNING)
    assert verdict.allowed is True


# ── Warnings (only minor findings) ────────────────────────────────────────

def test_warnings_only_findings_allow_in_blocker_mode() -> None:
    """Status=warnings (only minor findings, e.g. orphan code) doesn't
    block — these are nudges, not failures."""
    manifest = {
        "id": "x", "description": "ok",
        # An orphan code file fires C-A6 at minor severity.
        "files": [{"path": "scripts/orphan.py", "layer": "code"}],
        "scheduled_actions": [], "crons": [],
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert verdict.allowed is True
    assert verdict.status == STATUS_WARNINGS


# ── Incoherent in blocker mode ────────────────────────────────────────────

def test_incoherent_blocks_in_blocker_mode() -> None:
    """Description claims daily behavior but no triggers → C-A1
    critical → status=incoherent → blocker refuses."""
    manifest = {
        "id": "x", "description": "Sends a daily briefing at 7am.",
        "scheduled_actions": [], "crons": [],
        "requirements": {"integrations": []},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert verdict.allowed is False
    assert verdict.status == STATUS_INCOHERENT
    assert "blocked" in verdict.reason.lower()
    assert verdict.override_key   # an override is offered


def test_incoherent_passes_in_warning_mode() -> None:
    """Warning mode lets incoherent manifests through with a clear
    reason — useful for scanner-discovered manifests on first write."""
    manifest = {
        "id": "x", "description": "Sends daily briefing.",
        "scheduled_actions": [], "crons": [],
    }
    verdict = check_pre_deploy(manifest, mode=MODE_WARNING)
    assert verdict.allowed is True
    assert verdict.status == STATUS_INCOHERENT
    assert "warning" in verdict.reason.lower()


# ── Override ─────────────────────────────────────────────────────────────

def test_override_key_unblocks_incoherent_in_blocker_mode() -> None:
    """Operator provides the correct override_key → allowed."""
    manifest = {
        "id": "x", "description": "daily briefing",
        "scheduled_actions": [], "crons": [],
    }
    # First call returns the override key.
    initial = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert initial.allowed is False
    key = initial.override_key
    assert key

    # Caller submits with the key → allowed.
    final = check_pre_deploy(manifest, mode=MODE_BLOCKER,
                             override_key=key)
    assert final.allowed is True
    assert "override" in final.reason.lower()


def test_stale_override_key_rejected() -> None:
    """If the finding set changes between gate-check and override-submit,
    the key changes and the override is rejected. Prevents the operator
    from approving a stale override."""
    initial = check_pre_deploy(
        {"id": "x", "description": "daily briefing",
         "scheduled_actions": [], "crons": []},
        mode=MODE_BLOCKER,
    )
    assert not initial.allowed
    stale_key = initial.override_key

    # The manifest now has a DIFFERENT incoherence — C-A4 instead of C-A1.
    new_manifest = {
        "id": "x", "description": "an app",
        "requirements": {"integrations": []},
        "scheduled_actions": [
            {"id": "y", "state": "active", "trigger": {},
             "outputs": [{"kind": "messaging_channel"}]},
        ],
    }
    # Submitting the OLD override key → still blocked.
    result = check_pre_deploy(new_manifest, mode=MODE_BLOCKER,
                              override_key=stale_key)
    assert result.allowed is False


def test_override_key_stable_for_same_findings() -> None:
    """Same set of findings → same key. Idempotent."""
    manifest = {"id": "x", "description": "daily x",
                "scheduled_actions": [], "crons": []}
    a = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    b = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert a.override_key == b.override_key


# ── Caller-side helpers ──────────────────────────────────────────────────

def test_forge_approval_gate_uses_blocker_mode() -> None:
    manifest = {"id": "x", "description": "daily briefing",
                "scheduled_actions": [], "crons": []}
    verdict = forge_approval_gate(manifest)
    assert verdict.allowed is False
    assert verdict.mode == MODE_BLOCKER


def test_manifest_editor_gate_uses_blocker_mode() -> None:
    verdict = manifest_editor_gate({"id": "x", "scheduled_actions": []})
    assert verdict.mode == MODE_BLOCKER


def test_scanner_write_gate_uses_warning_mode() -> None:
    """Scanner-discovered manifests pass through even when incoherent —
    we capture truth-as-found and let the operator decide."""
    manifest = {"id": "x", "description": "daily briefing",
                "scheduled_actions": [], "crons": []}
    verdict = scanner_write_gate(manifest)
    assert verdict.mode == MODE_WARNING
    assert verdict.allowed is True
    assert verdict.status == STATUS_INCOHERENT


# ── Defensive cases ─────────────────────────────────────────────────────

def test_unknown_mode_blocks() -> None:
    """An unknown mode falls back to blocker — fail closed."""
    verdict = check_pre_deploy({"id": "x"}, mode="bogus")
    assert verdict.allowed is False


def test_verdict_to_dict_includes_all_fields() -> None:
    """The GateVerdict.to_dict surface matches what the UI consumes."""
    verdict = forge_approval_gate(
        {"id": "x", "description": "daily x",
         "scheduled_actions": [], "crons": []},
    )
    d = verdict.to_dict()
    for k in ("allowed", "status", "reason", "findings",
              "override_key", "mode"):
        assert k in d, f"missing key {k}"


# ── C3 capability check integration (spec §6.5) ─────────────────────

def test_c3_feasible_does_not_emit_finding() -> None:
    """C3 said feasible → no extra finding in the gate response."""
    manifest = {
        "id": "x",
        "coherence": {"last_capability_check": {
            "severity": "feasible", "rationale": "looks good",
        }},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert verdict.allowed
    # No C3 finding present (only Pass A would emit; manifest has nothing
    # for Pass A to flag).
    assert not any(f.get("id") == "C3" for f in verdict.findings)


def test_c3_unclear_emits_minor_finding_but_does_not_block() -> None:
    """C3 said unclear → minor finding surfaced, but operation allowed."""
    manifest = {
        "id": "x",
        "coherence": {"last_capability_check": {
            "severity": "unclear",
            "rationale": "manifest is ambiguous about telegram integration",
            "checked_at": "2026-06-06T00:00:00Z",
        }},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert verdict.allowed   # minor doesn't block
    c3_findings = [f for f in verdict.findings if f.get("id") == "C3"]
    assert len(c3_findings) == 1
    assert c3_findings[0]["severity"] == "minor"


def test_c3_incoherent_blocks_in_blocker_mode() -> None:
    """Spec §6.5: C3 incoherent in blocker mode → gate refuses."""
    manifest = {
        "id": "x",
        "coherence": {"last_capability_check": {
            "severity": "incoherent",
            "rationale": "manifest claims to summarize Telegram messages but declares no Telegram input integration",
            "checked_at": "2026-06-06T00:00:00Z",
        }},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert not verdict.allowed
    assert verdict.status == "incoherent"
    c3_findings = [f for f in verdict.findings if f.get("id") == "C3"]
    assert len(c3_findings) == 1
    assert c3_findings[0]["severity"] == "critical"


def test_c3_incoherent_warning_mode_allows() -> None:
    """Spec §6.6: warnings mode never blocks, even on C3 incoherent.
    Used for scanner re-scans of pre-existing manifests."""
    manifest = {
        "id": "x",
        "coherence": {"last_capability_check": {
            "severity": "incoherent", "rationale": "contradicts itself",
        }},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_WARNING)
    assert verdict.allowed


def test_c3_incoherent_blocker_mode_accepts_override() -> None:
    """Operator override flow works for C3-driven incoherence too."""
    manifest = {
        "id": "x",
        "coherence": {"last_capability_check": {
            "severity": "incoherent", "rationale": "contradicts itself",
        }},
    }
    verdict_blocked = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert not verdict_blocked.allowed
    key = verdict_blocked.override_key

    verdict_allowed = check_pre_deploy(
        manifest, mode=MODE_BLOCKER, override_key=key,
    )
    assert verdict_allowed.allowed


def test_c3_missing_rationale_skipped() -> None:
    """A malformed cache entry (no rationale) is skipped — no finding."""
    manifest = {
        "id": "x",
        "coherence": {"last_capability_check": {
            "severity": "incoherent",
            # rationale missing
        }},
    }
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert verdict.allowed   # no usable C3 finding


def test_no_c3_cache_no_finding() -> None:
    """When C3 hasn't run, no C3 finding appears in the gate response."""
    manifest = {"id": "x"}
    verdict = check_pre_deploy(manifest, mode=MODE_BLOCKER)
    assert not any(f.get("id") == "C3" for f in verdict.findings)
