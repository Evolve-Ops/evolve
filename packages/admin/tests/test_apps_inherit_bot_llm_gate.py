"""Tests for the apps-inherit-bot-llm save-time gate in
``save_manifest_with_provenance``.

Covers the source-aware gating: writes from new-authoring sources
(forge_built, user_authored, bot_authored, confirmed) are refused;
observational writes (scanner re-discovery) are allowed through so
existing pre-rearchitect manifests can be re-stamped during migration.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import manifest as manifest_mod  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    ManifestPrincipleViolation,
    PROVENANCE_BOT_AUTHORED,
    PROVENANCE_CONFIRMED,
    PROVENANCE_FORGE_BUILT,
    PROVENANCE_OBSERVATIONAL,
    PROVENANCE_USER_AUTHORED,
    save_manifest_with_provenance,
)


def _violating_manifest() -> ApplicationManifest:
    """Build an Atlas-pre-rearchitect-shaped manifest that violates
    the apps-inherit-bot-llm principle."""
    m = ApplicationManifest(id="p-atlas-daily-digest", name="Atlas Daily Digest", bot_id="atlas")
    # Both anti-patterns: api_key_source declared + credential template in files[].
    m.raw = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "model": "haiku"}],
            "api_key_source": "atlas/llm-config.json",
            "fallback_required": True,
        },
        "files": [
            {"path": "atlas/llm-config.json", "layer": "data", "data_kind": "template"},
        ],
    }
    return m


def _clean_manifest() -> ApplicationManifest:
    """Atlas post-rearchitect shape: passes the validator."""
    m = ApplicationManifest(id="p-atlas-daily-digest", name="Atlas Daily Digest", bot_id="atlas")
    m.raw = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "intent": "classify into 5 buckets"}],
            "transport": "openclaw_headless",
            "fallback_required": True,
        },
        "files": [
            {"path": "atlas/sources.json", "layer": "data", "data_kind": "template"},
        ],
    }
    return m


def _stub_save(monkeypatch: pytest.MonkeyPatch) -> list[ApplicationManifest]:
    """Replace save_manifest with a recorder so we can assert it ran (or
    didn't) without touching the real applications/<bot>/ directory."""
    calls: list[ApplicationManifest] = []

    def _fake_save(manifest, shared_dir):
        calls.append(manifest)
        return Path("/tmp/fake-manifest.json")

    monkeypatch.setattr(manifest_mod, "save_manifest", _fake_save)
    return calls


# ── Gate fires on new-authoring sources ──────────────────────────────────────


def test_forge_built_save_refuses_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_save(monkeypatch)
    m = _violating_manifest()
    with pytest.raises(ManifestPrincipleViolation) as exc:
        save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_FORGE_BUILT)
    assert exc.value.principle == "apps-inherit-bot-llm"
    assert exc.value.manifest_id == "p-atlas-daily-digest"
    assert exc.value.bot_id == "atlas"
    assert any("api_key_source" in e for e in exc.value.errors)
    assert calls == []  # gate blocked the save


def test_user_authored_save_refuses_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_save(monkeypatch)
    m = _violating_manifest()
    with pytest.raises(ManifestPrincipleViolation):
        save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_USER_AUTHORED)
    assert calls == []


def test_bot_authored_save_refuses_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_save(monkeypatch)
    m = _violating_manifest()
    with pytest.raises(ManifestPrincipleViolation):
        save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_BOT_AUTHORED)
    assert calls == []


def test_confirmed_save_refuses_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-confirmed writes are still gated — confirming a violation
    isn't a valid way to bypass the principle. The operator must fix the
    manifest first."""
    calls = _stub_save(monkeypatch)
    m = _violating_manifest()
    with pytest.raises(ManifestPrincipleViolation):
        save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_CONFIRMED)
    assert calls == []


# ── Observational source passes through unchanged ────────────────────────────


def test_observational_save_allows_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanner re-discovery must NOT block on pre-rearchitect manifests.
    Atlas's existing manifests still have api_key_source until the live-pod
    migration completes; blocking observational writes would break the
    scanner mid-migration.

    The exception path: observational writes pass through; only authoring
    paths are gated."""
    calls = _stub_save(monkeypatch)
    m = _violating_manifest()
    save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_OBSERVATIONAL)
    # Save proceeded.
    assert len(calls) == 1


# ── Clean manifest passes regardless of source ───────────────────────────────


@pytest.mark.parametrize("source", [
    PROVENANCE_FORGE_BUILT,
    PROVENANCE_USER_AUTHORED,
    PROVENANCE_BOT_AUTHORED,
    PROVENANCE_CONFIRMED,
    PROVENANCE_OBSERVATIONAL,
])
def test_clean_manifest_passes_gate_from_any_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    calls = _stub_save(monkeypatch)
    m = _clean_manifest()
    save_manifest_with_provenance(m, tmp_path, source=source)
    assert len(calls) == 1


# ── Exception payload carries enough context to render an actionable error ───


def test_violation_exception_includes_human_readable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_save(monkeypatch)
    m = _violating_manifest()
    with pytest.raises(ManifestPrincipleViolation) as exc:
        save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_FORGE_BUILT)
    msg = str(exc.value)
    assert "spec-apps-inherit-bot-llm-2026-06-06" in msg
    assert "principle-apps-inherit-bot-llm" in msg


def test_violation_exception_includes_source_for_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_save(monkeypatch)
    m = _violating_manifest()
    with pytest.raises(ManifestPrincipleViolation) as exc:
        save_manifest_with_provenance(m, tmp_path, source=PROVENANCE_BOT_AUTHORED)
    # forge/UI handlers want to log which write path was refused.
    assert exc.value.source == PROVENANCE_BOT_AUTHORED
