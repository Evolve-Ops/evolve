"""Layer-aware severity + provenance-gated emission tests — PR 5.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §3.2, §8.1.

Two narrow load-bearing changes:

1. **Layer-aware severity** in app_audit_structural.check_files_exist /
   check_files_sha. A missing ``code`` / ``config`` / ``contract`` /
   ``behavior_doc`` file fires critical; ``content`` major;
   ``reference`` minor; ``data`` / ``log`` / ``state`` info. Without
   this, every data-file rotation fires alert-tier Signals → chat
   fatigue → muted producer → real failures missed.

2. **Provenance-gated Signal emission** in audit_poller._ingest_finding.
   Tier 2 findings whose assertion targets a field whose provenance
   is explicitly observational get archived to the trail without
   firing a Signal — the spec's central "silent for observational"
   doctrine, now operational on the Tier-2 side too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from app_audit_structural import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    Finding,
    check_files_exist,
    check_files_sha,
    severity_for_missing_file,
    severity_for_sha_drift,
)


# ── Layer-aware severity helpers ──────────────────────────────────────────

class TestSeverityForMissingFile:
    """Spec §3.2: missing-file severity by layer."""

    def test_critical_layers(self):
        for layer in ("code", "config", "contract", "behavior_doc"):
            assert severity_for_missing_file(layer) == SEVERITY_CRITICAL, (
                f"layer {layer!r} should be critical"
            )

    def test_content_is_major(self):
        assert severity_for_missing_file("content") == SEVERITY_MAJOR

    def test_reference_is_minor(self):
        assert severity_for_missing_file("reference") == SEVERITY_MINOR

    def test_data_log_state_are_info(self):
        for layer in ("data", "log", "state"):
            assert severity_for_missing_file(layer) == SEVERITY_INFO, (
                f"layer {layer!r} should be info"
            )

    def test_legacy_script_maps_to_critical(self):
        """Pre-PR-1 manifests still in transit may carry 'script' layer
        (the v19 stamper used that before PR 1's "script"→"code" remap).
        Treat as critical so we don't silently downgrade those."""
        assert severity_for_missing_file("script") == SEVERITY_CRITICAL

    def test_unknown_layer_falls_to_critical(self):
        """Safe default — better to over-alert than silently swallow
        a real failure."""
        assert severity_for_missing_file("xyz") == SEVERITY_CRITICAL
        assert severity_for_missing_file("") == SEVERITY_CRITICAL
        assert severity_for_missing_file(None) == SEVERITY_CRITICAL

    def test_case_insensitive(self):
        assert severity_for_missing_file("CODE") == SEVERITY_CRITICAL
        assert severity_for_missing_file("Data") == SEVERITY_INFO


class TestSeverityForShaDrift:
    """Spec §3.2: sha-drift severity by layer. None means 'ignored'."""

    def test_code_is_major(self):
        assert severity_for_sha_drift("code") == SEVERITY_MAJOR

    def test_config_contract_critical(self):
        """Sha drift on declared config or the manifest itself is a
        silent behavior change — critical."""
        assert severity_for_sha_drift("config") == SEVERITY_CRITICAL
        assert severity_for_sha_drift("contract") == SEVERITY_CRITICAL

    def test_behavior_doc_major(self):
        assert severity_for_sha_drift("behavior_doc") == SEVERITY_MAJOR

    def test_volatile_layers_return_none(self):
        """data / log / state / content / reference all return None —
        sha drift on these is benign / expected."""
        for layer in ("data", "log", "state", "content", "reference"):
            assert severity_for_sha_drift(layer) is None, (
                f"layer {layer!r} should be ignored (None)"
            )

    def test_unknown_layer_falls_to_major(self):
        assert severity_for_sha_drift("xyz") == SEVERITY_MAJOR
        assert severity_for_sha_drift("") == SEVERITY_MAJOR
        assert severity_for_sha_drift(None) == SEVERITY_MAJOR


# ── check_files_exist with layer-aware severity ───────────────────────────

class TestCheckFilesExistLayerAwareSeverity:
    """The integration check — confirm the assertion actually uses the
    helpers (vs hardcoded critical)."""

    def test_missing_code_file_is_critical(self, tmp_path):
        manifest = {"files": [{"path": "scripts/main.py", "layer": "code"}]}
        findings = check_files_exist(manifest, {"workspace": tmp_path})
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_CRITICAL
        assert findings[0].evidence["layer"] == "code"

    def test_missing_data_file_is_info(self, tmp_path):
        """The load-bearing fix: data-layer rotations no longer fire
        alert-tier Signals."""
        manifest = {"files": [{"path": "data/journal.md", "layer": "data"}]}
        findings = check_files_exist(manifest, {"workspace": tmp_path})
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_INFO

    def test_missing_log_file_is_info(self, tmp_path):
        manifest = {"files": [{"path": "log/audit.log", "layer": "log"}]}
        findings = check_files_exist(manifest, {"workspace": tmp_path})
        assert findings[0].severity == SEVERITY_INFO

    def test_missing_content_file_is_major(self, tmp_path):
        manifest = {"files": [{"path": "articles/intro.md", "layer": "content"}]}
        findings = check_files_exist(manifest, {"workspace": tmp_path})
        assert findings[0].severity == SEVERITY_MAJOR

    def test_existing_file_no_finding(self, tmp_path):
        """Existing file → no finding regardless of layer."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "main.py").write_text("# ok")
        manifest = {"files": [{"path": "scripts/main.py", "layer": "code"}]}
        findings = check_files_exist(manifest, {"workspace": tmp_path})
        assert findings == []


# ── check_files_sha respects layer-ignored entries ────────────────────────

class TestCheckFilesShaLayerAwareSeverity:
    """Sha drift on layers that should be ignored produces no finding;
    layers that matter produce a finding at the right severity."""

    def test_sha_drift_on_data_layer_skipped(self, tmp_path):
        """data-layer sha drift was already skipped via _VOLATILE_LAYERS
        before PR 5; this confirms the layer-aware path agrees."""
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "journal.json").write_text("changed")
        manifest = {
            "files": [{
                "path": "data/journal.json", "layer": "data",
                "sha256": "different",
            }],
        }
        findings = check_files_sha(manifest, {"workspace": tmp_path})
        assert findings == []

    def test_sha_drift_on_content_layer_skipped(self, tmp_path):
        """content-layer drift is benign — content evolves by design.
        This case is NEW in PR 5 (content layer didn't exist in v19)."""
        (tmp_path / "articles").mkdir()
        (tmp_path / "articles" / "topic.md").write_text("rewritten")
        manifest = {
            "files": [{
                "path": "articles/topic.md", "layer": "content",
                "sha256": "old_sha",
            }],
        }
        findings = check_files_sha(manifest, {"workspace": tmp_path})
        assert findings == []

    def test_sha_drift_on_code_layer_major(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "main.py").write_text("new content")
        manifest = {
            "files": [{
                "path": "scripts/main.py", "layer": "code",
                "sha256": "old_sha_that_wont_match",
            }],
        }
        findings = check_files_sha(manifest, {"workspace": tmp_path})
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_MAJOR
        assert findings[0].assertion_id == "file_sha_mismatch"

    def test_sha_drift_on_config_layer_critical(self, tmp_path):
        """Sha drift on declared config = silent behavior change. Critical."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "settings.json").write_text("{}")
        manifest = {
            "files": [{
                "path": "config/settings.json", "layer": "config",
                "sha256": "old",
            }],
        }
        findings = check_files_sha(manifest, {"workspace": tmp_path})
        assert findings[0].severity == SEVERITY_CRITICAL


# ── Provenance gate (audit_poller._is_observational_finding) ──────────────


def _make_manifest_with_provenance(
    files_source: str | None = None,
    crons_source: str | None = None,
):
    """Build a SimpleNamespace mimicking ApplicationManifest's
    provenance attribute. The audit_poller reads via getattr."""
    from types import SimpleNamespace
    field_origins = {}
    if files_source is not None:
        field_origins["files"] = {"source": files_source}
    if crons_source is not None:
        field_origins["crons"] = {"source": crons_source}
    return SimpleNamespace(provenance={"field_origins": field_origins})


class TestProvenanceGate:
    """audit_poller._is_observational_finding gates Signal emission."""

    def _import_gate(self):
        from evolve_admin.applications.audit_poller import (
            _is_observational_finding,
        )
        return _is_observational_finding

    def test_observational_files_field_returns_true(self):
        """The doctrine: a finding on an observational field doesn't
        fire a Signal."""
        gate = self._import_gate()
        with patch("evolve_admin.applications.audit_poller.load_manifest"
                   ) if False else patch(
                       "evolve_admin.applications.manifest.load_manifest",
                       return_value=_make_manifest_with_provenance(
                           files_source="observational")):
            assert gate(
                {"assertion_id": "file_missing"}, "bot", "app") is True

    def test_forge_built_files_field_returns_false_emit(self):
        gate = self._import_gate()
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=_make_manifest_with_provenance(
                       files_source="forge_built")):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_user_authored_emits(self):
        gate = self._import_gate()
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=_make_manifest_with_provenance(
                       files_source="user_authored")):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_bot_authored_emits(self):
        gate = self._import_gate()
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=_make_manifest_with_provenance(
                       files_source="bot_authored")):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_confirmed_emits(self):
        gate = self._import_gate()
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=_make_manifest_with_provenance(
                       files_source="confirmed")):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_unknown_assertion_emits(self):
        """Unknown assertion_id → safe default 'emit'. Prevents new
        assertions from being accidentally muted."""
        gate = self._import_gate()
        assert gate({"assertion_id": "new_assertion"}, "bot", "app") is False

    def test_field_missing_from_origins_emits(self):
        """Field never stamped → don't mute. Day-1 protection for
        existing v19 manifests that have empty provenance."""
        gate = self._import_gate()
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=_make_manifest_with_provenance()):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_no_provenance_block_emits(self):
        gate = self._import_gate()
        from types import SimpleNamespace
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=SimpleNamespace(provenance={})):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_manifest_load_failure_emits(self):
        """Can't load the manifest → don't silently mute; emit."""
        gate = self._import_gate()
        with patch("evolve_admin.applications.manifest.load_manifest",
                   side_effect=Exception("boom")):
            assert gate({"assertion_id": "file_missing"}, "bot", "app") is False

    def test_missing_bot_or_app_emits(self):
        """Without bot_id+app_id we can't resolve the manifest. Emit."""
        gate = self._import_gate()
        assert gate({"assertion_id": "file_missing"}, "", "app") is False
        assert gate({"assertion_id": "file_missing"}, "bot", "") is False

    def test_cron_assertions_check_crons_field(self):
        """openclaw_cron_error checks crons, not files."""
        gate = self._import_gate()
        # files=forge_built (would emit), crons=observational (should mute)
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=_make_manifest_with_provenance(
                       files_source="forge_built",
                       crons_source="observational")):
            assert gate({"assertion_id": "openclaw_cron_error"},
                        "bot", "app") is True

    def test_scheduled_action_assertions_check_scheduled_actions_field(self):
        """scheduled_action_anchor checks scheduled_actions."""
        gate = self._import_gate()
        from types import SimpleNamespace
        prov = SimpleNamespace(provenance={
            "field_origins": {
                "scheduled_actions": {"source": "observational"},
            },
        })
        with patch("evolve_admin.applications.manifest.load_manifest",
                   return_value=prov):
            assert gate({"assertion_id": "scheduled_action_anchor"},
                        "bot", "app") is True


# ── Assertion-to-field mapping completeness ───────────────────────────────

def test_assertion_to_field_covers_all_v20_assertion_ids() -> None:
    """Every assertion_id in app_audit_structural.ASSERTION_IDS should
    be mapped (otherwise it falls through to 'emit' and the gate is
    silently ineffective for that assertion). Catches drift within the
    single-source mapping (now ``app_audit_structural.ASSERTION_TO_FIELD``,
    imported by both the runner and the poller)."""
    from app_audit_structural import ASSERTION_IDS, ASSERTION_TO_FIELD
    # Each assertion_id should be present in the mapping. Some IDs
    # (assertion_crashed) intentionally don't map to a field.
    unmapped = []
    _EXCLUDED = {"assertion_crashed"}
    for aid in ASSERTION_IDS:
        if aid in _EXCLUDED:
            continue
        if aid not in ASSERTION_TO_FIELD:
            unmapped.append(aid)
    assert not unmapped, (
        f"assertion_ids missing from ASSERTION_TO_FIELD: {unmapped}. "
        f"Without a mapping, the provenance gate is silently ineffective "
        f"for these — they always emit Signals regardless of provenance."
    )
