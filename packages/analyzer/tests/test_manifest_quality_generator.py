"""tests/test_manifest_quality_generator.py — manifest_quality factory + observe tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.manifest_quality.observe import (  # noqa: E402
    ManifestQualityContext,
    observe,
)
from generators.manifest_quality.signal_proposals import (  # noqa: E402
    SIGNAL_TYPE_TO_FACTORY,
    make_stale_proposal,
    make_test_failing_proposal,
    make_validation_error_proposal,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Signal fixtures ──────────────────────────────────────────────────────────


def _rollup_signal(
    *,
    sig_type: str,
    sig_id: str = "sig-1",
    bot_id: str = "admin_bot",
    items: list[dict] | None = None,
    app_id: str = "health-tracker",
    message: str = "issue detected",
) -> SimpleNamespace:
    """Build a rollup-shape Signal fixture."""
    if items is None:
        items = [{"app_id": app_id, "message": message}]
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type=sig_type,
        severity="warn",
        details={
            "bot_id": bot_id,
            "issue_type": sig_type,
            "items": items,
            "item_count": len(items),
        },
    )


def _legacy_signal(
    *,
    sig_type: str,
    sig_id: str = "sig-legacy",
    bot_id: str = "admin_bot",
    app_id: str = "health-tracker",
    message: str = "issue detected",
) -> SimpleNamespace:
    """Pre-rollup per-item Signal shape (still possible in the store
    transiently until sweep_resolve clears the old ones)."""
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type=sig_type,
        severity="warn",
        details={
            "bot_id": bot_id,
            "issue_type": sig_type,
            "app_id": app_id,
            "message": message,
        },
    )


# ── Per-factory shape ────────────────────────────────────────────────────────


def test_stale_factory_single_item():
    proposals = make_stale_proposal(_rollup_signal(sig_type="stale"))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.generator_id == "manifest_quality"
    assert p.dimension == "app_quality"
    assert p.action.kind == "Investigation"
    assert p.urgency == "improvement"
    assert p.approval_audience == "pod_operator"
    assert p.motivating_signals == ["sig-1"]
    assert p.bot_id == "admin_bot"
    assert "health-tracker" in p.admin_surface_summary
    assert "stale" in p.problem.lower()


def test_stale_factory_fans_out_over_items():
    """A rollup signal with 3 items → 3 Proposals."""
    sig = _rollup_signal(
        sig_type="stale",
        items=[
            {"app_id": "app-a", "message": "stale a"},
            {"app_id": "app-b", "message": "stale b"},
            {"app_id": "app-c", "message": "stale c"},
        ],
    )
    proposals = make_stale_proposal(sig)
    assert len(proposals) == 3
    app_ids = {p.provenance.signals["app_id"] for p in proposals}
    assert app_ids == {"app-a", "app-b", "app-c"}


def test_stale_factory_legacy_per_item_signal_still_works():
    """During the rollup migration, legacy per-item signals already in the
    store still produce a Proposal."""
    proposals = make_stale_proposal(_legacy_signal(sig_type="stale"))
    assert len(proposals) == 1
    assert "health-tracker" in proposals[0].admin_surface_summary


def test_test_failing_factory():
    proposals = make_test_failing_proposal(_rollup_signal(sig_type="test_failing"))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.dimension == "app_quality"
    assert p.action.kind == "Investigation"
    assert "test" in p.problem.lower() or "test" in p.admin_surface_summary.lower()


def test_validation_error_factory():
    proposals = make_validation_error_proposal(_rollup_signal(sig_type="validation_error"))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "Investigation"
    assert p.urgency == "hygiene"


# ── Dispatch table ────────────────────────────────────────────────────────────


def test_dispatch_includes_all_consumed_types():
    """missing_required_field is intentionally excluded — see 2026-05-25 triage."""
    assert set(SIGNAL_TYPE_TO_FACTORY.keys()) == {
        "stale", "test_failing", "validation_error",
    }


# ── observe() end-to-end ──────────────────────────────────────────────────────


def _write_signal(
    shared_dir: Path, *, bot_id: str, sig_type: str, app_id: str = "app-a",
) -> str:
    """Write a rollup-shape signal (current production shape)."""
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("compliance_scan", sig_type, bot_id),
        producer="compliance_scan",
        type=sig_type,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: {sig_type}",
        details={
            "bot_id": bot_id,
            "issue_type": sig_type,
            "items": [{"app_id": app_id, "message": f"{sig_type} message"}],
            "item_count": 1,
        },
    )
    return sig.id


def test_observe_consumes_consumed_signal_types(tmp_path):
    for st in ("stale", "test_failing", "validation_error"):
        _write_signal(tmp_path, bot_id="admin_bot", sig_type=st)
    proposals = observe(ManifestQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    types = sorted(p.trigger_observations[0].split(":")[0] for p in proposals)
    assert types == ["stale", "test_failing", "validation_error"]


def test_observe_ignores_missing_required_field_signals(tmp_path):
    """missing_required_field is still emitted by the scanner for audit/alerts,
    but the generator must not promote it to a Proposal (2026-05-25 triage)."""
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="missing_required_field")
    proposals = observe(ManifestQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_filters_by_bot_id(tmp_path):
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="stale")
    _write_signal(tmp_path, bot_id="team_bot_c", sig_type="stale")
    proposals = observe(ManifestQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].bot_id == "admin_bot"


def test_observe_ignores_workspace_inventory_signal_types(tmp_path):
    """unregistered_script / unregistered_cron belong to workspace_inventory."""
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="unregistered_script")
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="unregistered_cron")
    proposals = observe(ManifestQualityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_returns_empty_when_no_signals(tmp_path):
    assert observe(ManifestQualityContext(bot_id="admin_bot", shared_dir=tmp_path)) == []
