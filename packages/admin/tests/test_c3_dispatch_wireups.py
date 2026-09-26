"""Integration tests for the C3 LLM dispatch wire-ups.

Covers the two entry points that call ``coherence_c3_dispatcher.dispatch_c3``:

  * ``manifest.save_manifest_with_provenance`` — editor / evo saves
    (user_authored, bot_authored) trigger C3 when a charter field
    changes and Pass A allows it.

  * ``forge_engine.approve_forge_job`` — pre-approval coherence gate
    dispatches C3 (via the ``_dispatch_c3_for_approval`` helper) when
    the manifest is structurally ok/warnings.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    workspace_root = tmp_path / "bot-workspace"
    workspace_root.mkdir()
    import evolve_admin.config as _config
    monkeypatch.setattr(_config, "get_bot_workspace", lambda _b: workspace_root)
    yield workspace_root


def _write_manifest(workspace_root: Path, bot_id: str, manifest: dict) -> Path:
    mdir = workspace_root / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    app_id = manifest.get("id") or "app1"
    p = mdir / f"{app_id}.json"
    manifest.setdefault("bot_id", bot_id)
    manifest.setdefault("name", manifest.get("id", "app1"))
    p.write_text(json.dumps(manifest))
    return p


def _read_manifest(workspace_root: Path, app_id: str) -> dict:
    return json.loads(
        (workspace_root / "manifests" / f"{app_id}.json").read_text()
    )


# ── save_manifest_with_provenance integration ─────────────────────────


def test_editor_save_dispatches_c3_on_charter_field_change(
    workspace, monkeypatch,
):
    """A user_authored save that changes ``description`` triggers a C3
    dispatch tagged ``charter_change`` against the prior on-disk
    snapshot."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest, save_manifest_with_provenance,
        PROVENANCE_USER_AUTHORED,
    )

    # Prior version on disk — same id, OLD description.
    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "description": "OLD description",
    })

    calls: list[dict] = []

    def _stub_dispatch(**kwargs):
        calls.append(kwargs)
        from evolve_admin.applications.coherence_c3_dispatcher import DispatchResult
        from evolve_admin.applications.coherence_pass_c3 import CapabilityCheck
        return DispatchResult(
            ok=True, skipped=False,
            check=CapabilityCheck(
                severity="feasible", rationale="ok",
                checked_at="2026-06-07T00:00:00Z",
                triggered_by="charter_change",
            ),
            model="anthropic/claude-haiku-4-5",
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _stub_dispatch,
    )

    m = ApplicationManifest(
        id="j", name="J", bot_id="bot-x",
        description="NEW description",
    )
    save_manifest_with_provenance(
        m, workspace, source=PROVENANCE_USER_AUTHORED,
        by="user:operator", via="ui",
    )

    assert len(calls) == 1, "C3 dispatch should fire once on charter edit"
    assert calls[0]["trigger"] == "charter_change"
    assert calls[0]["bot_id"] == "bot-x"
    assert calls[0]["app_id"] == "j"
    # before_manifest must carry the OLD on-disk description so the
    # charter-change detector inside should_run_c3 sees the diff.
    assert calls[0]["before_manifest"]["description"] == "OLD description"


def test_editor_save_skips_dispatch_when_no_charter_field_changed(
    workspace, monkeypatch,
):
    """Edit that doesn't touch description / usage.how_to_use /
    success_criteria.observable_outcomes → no LLM call."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest, save_manifest_with_provenance,
        PROVENANCE_USER_AUTHORED,
    )

    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "description": "stays the same",
    })

    calls: list[dict] = []

    def _stub_dispatch(**kwargs):
        calls.append(kwargs)
        from evolve_admin.applications.coherence_c3_dispatcher import DispatchResult
        # Real dispatcher returns skipped on no-charter-change; mimic
        # that here so the test catches the call but verifies semantics.
        return DispatchResult(
            ok=False, skipped=True,
            reason="no charter fields changed",
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _stub_dispatch,
    )

    m = ApplicationManifest(
        id="j", name="J", bot_id="bot-x",
        description="stays the same",
        tags=["new-tag"],  # non-charter-field edit
    )
    save_manifest_with_provenance(
        m, workspace, source=PROVENANCE_USER_AUTHORED,
        by="user:operator", via="ui",
    )

    # The dispatch helper still gets called — the dispatcher itself
    # decides skip — but it must report "no charter fields changed".
    assert len(calls) == 1
    assert calls[0]["before_manifest"]["description"] == "stays the same"


def test_editor_save_with_observational_source_does_not_dispatch(
    workspace, monkeypatch,
):
    """Scanner re-stamps (observational) must not burn C3 budget — those
    aren't operator-driven, and Pass C3 doesn't apply during
    discovery."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest, save_manifest_with_provenance,
        PROVENANCE_OBSERVATIONAL,
    )

    _write_manifest(workspace, "bot-x", {
        "id": "j", "description": "old",
    })

    def _boom(**kwargs):
        raise AssertionError(
            "dispatch_c3 must not be called for observational saves"
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _boom,
    )

    m = ApplicationManifest(
        id="j", name="J", bot_id="bot-x", description="new",
    )
    # No exception → no dispatch attempted.
    save_manifest_with_provenance(
        m, workspace, source=PROVENANCE_OBSERVATIONAL,
        by="scanner", via="scan",
    )


def test_editor_save_skips_dispatch_when_pass_a_incoherent(
    workspace, monkeypatch,
):
    """When Pass A says incoherent, don't burn LLM tokens — the
    structural gate (PR #2325) raises ForgeCoherenceGateError before
    the C3 dispatch helper has a chance to run. Use ``bot_authored``
    here because PR #2325's gate exempts that source (it doesn't author
    intent), which still routes through the C3 dispatch block where
    the Pass A pre-check should skip the LLM call.
    """
    from evolve_admin.applications.manifest import (
        ApplicationManifest, save_manifest_with_provenance,
        PROVENANCE_BOT_AUTHORED,
    )

    _write_manifest(workspace, "bot-x", {
        "id": "j", "description": "old",
    })

    def _boom(**kwargs):
        raise AssertionError(
            "dispatch_c3 must not be called when Pass A is incoherent"
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _boom,
    )
    # Force Pass A to report incoherent.
    monkeypatch.setattr(
        "evolve_admin.applications.coherence_pass_a.run_pass_a",
        lambda _m: [],
    )
    monkeypatch.setattr(
        "evolve_admin.applications.coherence_pass_a.status_for_findings",
        lambda _f: "incoherent",
    )

    m = ApplicationManifest(
        id="j", name="J", bot_id="bot-x",
        description="new",
    )
    save_manifest_with_provenance(
        m, workspace, source=PROVENANCE_BOT_AUTHORED,
        by="evo:app-changes", via="evo",
    )


def test_editor_save_skips_dispatch_when_already_cached(
    workspace, monkeypatch,
):
    """is_rate_limited short-circuits the dispatch helper before the LLM
    call. Saves with a recent cached verdict skip silently."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest, save_manifest_with_provenance,
        PROVENANCE_USER_AUTHORED,
    )
    from datetime import datetime, timezone

    _write_manifest(workspace, "bot-x", {
        "id": "j", "description": "old",
    })

    def _boom(**kwargs):
        raise AssertionError(
            "dispatch_c3 must not be called when verdict is fresh"
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _boom,
    )

    fresh_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = ApplicationManifest(
        id="j", name="J", bot_id="bot-x",
        description="new",
        coherence={
            "status": "ok", "findings": [],
            "last_capability_check": {
                "severity": "feasible",
                "rationale": "fresh",
                "checked_at": fresh_iso,
            },
        },
    )
    save_manifest_with_provenance(
        m, workspace, source=PROVENANCE_USER_AUTHORED,
        by="user:operator", via="ui",
    )


# ── forge_engine.approve_forge_job dispatch helper ────────────────────


def test_forge_helper_skips_when_rate_limited(workspace, monkeypatch, tmp_path):
    """``_dispatch_c3_for_approval`` honors the 24h rate limit."""
    from datetime import datetime, timezone
    from evolve_admin.applications.forge_engine import (
        _dispatch_c3_for_approval,
    )

    def _boom(**kwargs):
        raise AssertionError(
            "dispatch_c3 must not be called when verdict is fresh"
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _boom,
    )

    fresh_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _dispatch_c3_for_approval(
        job_id="job-1", shared_dir=tmp_path,
        bot_id="bot-x", app_id="j",
        manifest_dict={
            "id": "j", "description": "x",
            "coherence": {
                "last_capability_check": {
                    "severity": "feasible",
                    "rationale": "fresh",
                    "checked_at": fresh_iso,
                },
            },
        },
    )


def test_forge_helper_skips_when_pass_a_incoherent(
    workspace, monkeypatch, tmp_path,
):
    """No point burning C3 budget when Pass A would already block."""
    from evolve_admin.applications.forge_engine import (
        _dispatch_c3_for_approval,
    )

    def _boom(**kwargs):
        raise AssertionError(
            "dispatch_c3 must not be called when Pass A is incoherent"
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _boom,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.coherence_pass_a.run_pass_a",
        lambda _m: [],
    )
    monkeypatch.setattr(
        "evolve_admin.applications.coherence_pass_a.status_for_findings",
        lambda _f: "incoherent",
    )

    _dispatch_c3_for_approval(
        job_id="job-1", shared_dir=tmp_path,
        bot_id="bot-x", app_id="j",
        manifest_dict={"id": "j", "description": "x"},
    )


def test_forge_helper_dispatches_and_stamps_manifest_dict(
    workspace, monkeypatch, tmp_path,
):
    """When the gate is about to run and no cache exists, the helper
    dispatches C3 and stamps the verdict on the in-memory manifest_dict
    so validate_coherence_gate (which runs immediately after) reads it."""
    from evolve_admin.applications.forge_engine import (
        _dispatch_c3_for_approval,
    )
    from evolve_admin.applications.coherence_c3_dispatcher import DispatchResult
    from evolve_admin.applications.coherence_pass_c3 import CapabilityCheck

    def _stub_dispatch(**kwargs):
        return DispatchResult(
            ok=True, skipped=False,
            check=CapabilityCheck(
                severity="incoherent",
                rationale="missing telegram inputs",
                checked_at="2026-06-07T00:00:00Z",
                triggered_by="forge_approval",
            ),
            model="anthropic/claude-haiku-4-5",
            cost_estimate_usd=0.004,
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _stub_dispatch,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.coherence_pass_a.run_pass_a",
        lambda _m: [],
    )
    monkeypatch.setattr(
        "evolve_admin.applications.coherence_pass_a.status_for_findings",
        lambda _f: "ok",
    )

    manifest_dict = {"id": "j", "description": "x"}
    _dispatch_c3_for_approval(
        job_id="job-1", shared_dir=tmp_path,
        bot_id="bot-x", app_id="j",
        manifest_dict=manifest_dict,
    )
    # In-memory dict carries the verdict for the gate that runs next.
    cap = manifest_dict.get("coherence", {}).get("last_capability_check")
    assert cap is not None
    assert cap["severity"] == "incoherent"
    assert "missing telegram inputs" in cap["rationale"]
