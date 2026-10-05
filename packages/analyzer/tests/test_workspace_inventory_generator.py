"""tests/test_workspace_inventory_generator.py — workspace_inventory factory + observe tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.workspace_inventory.observe import (  # noqa: E402
    WorkspaceInventoryContext,
    observe,
)
from generators.workspace_inventory.signal_proposals import (  # noqa: E402
    SIGNAL_TYPE_TO_FACTORY,
    make_unregistered_script_proposal,
    make_unregistered_cron_proposal,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Factory shape ────────────────────────────────────────────────────────────


def _rollup_script_signal(items: list[dict], sig_id: str = "sig-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id="admin_bot",
        type="unregistered_script",
        details={"items": items, "item_count": len(items)},
    )


def _rollup_cron_signal(items: list[dict], sig_id: str = "sig-2") -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id="admin_bot",
        type="unregistered_cron",
        details={"items": items, "item_count": len(items)},
    )


def test_unregistered_script_factory():
    sig = _rollup_script_signal([
        {"path": "ops/run.py", "message": "Script has no registered manifest"},
    ])
    proposals = make_unregistered_script_proposal(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.generator_id == "workspace_inventory"
    assert p.dimension == "app_quality"
    assert p.action.kind == "Investigation"
    assert p.urgency == "hygiene"
    assert p.motivating_signals == ["sig-1"]
    assert "ops/run.py" in p.problem
    assert "ops/run.py" in p.admin_surface_summary
    assert len(p.admin_surface_summary) <= 120


def test_unregistered_script_factory_fans_out():
    sig = _rollup_script_signal([
        {"path": "a.py", "message": "msg"},
        {"path": "b.py", "message": "msg"},
        {"path": "c.py", "message": "msg"},
    ])
    proposals = make_unregistered_script_proposal(sig)
    assert len(proposals) == 3
    paths = {p.provenance.signals["path"] for p in proposals}
    assert paths == {"a.py", "b.py", "c.py"}


def test_unregistered_cron_factory():
    sig = _rollup_cron_signal([
        {"cron": "0 * * * * /path/script.py", "message": "Cron unmanaged"},
    ])
    proposals = make_unregistered_cron_proposal(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "Investigation"
    assert "cron" in p.problem.lower()
    assert "0 * * * *" in p.action.context


def test_unregistered_cron_long_entry_truncates_in_problem_field():
    long_cron = "0 * * * * " + ("python3 /very/long/path/" * 20) + "script.py"
    sig = _rollup_cron_signal(
        [{"cron": long_cron, "message": "x"}], sig_id="sig-3",
    )
    proposals = make_unregistered_cron_proposal(sig)
    p = proposals[0]
    # The Problem line is one-line shorthand; long crons are truncated.
    assert len(p.problem) < 200
    # Full cron text still reaches the operator via the Investigation context.
    assert long_cron in p.action.context


def test_legacy_per_item_signal_still_works():
    """Legacy pre-rollup per-item signal still in the store produces a Proposal."""
    sig = SimpleNamespace(
        id="legacy-1",
        bot_id="admin_bot",
        type="unregistered_script",
        details={"path": "ops/run.py", "message": "legacy msg"},
    )
    proposals = make_unregistered_script_proposal(sig)
    assert len(proposals) == 1
    assert "ops/run.py" in proposals[0].problem


# ── Dispatch table ───────────────────────────────────────────────────────────


def test_dispatch_table():
    assert set(SIGNAL_TYPE_TO_FACTORY.keys()) == {
        "unregistered_script", "unregistered_cron",
    }


# ── observe() end-to-end ─────────────────────────────────────────────────────


def _write_signal(
    shared_dir: Path,
    *,
    bot_id: str,
    sig_type: str,
    path: str | None = None,
    cron: str | None = None,
) -> str:
    """Write a rollup-shape signal (one item)."""
    item: dict = {"message": f"{sig_type} message"}
    if path:
        item["path"] = path
    if cron:
        item["cron"] = cron
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
        details={"items": [item], "item_count": 1},
    )
    return sig.id


def test_observe_consumes_both_inventory_types(tmp_path):
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="unregistered_script",
                  path="ops/run.py")
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="unregistered_cron",
                  cron="0 * * * * /path/script.py")
    proposals = observe(WorkspaceInventoryContext(bot_id="admin_bot", shared_dir=tmp_path))
    types = sorted(p.trigger_observations[0].split(":")[0] for p in proposals)
    assert types == ["unregistered_cron", "unregistered_script"]


def test_observe_filters_by_bot_id(tmp_path):
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="unregistered_script",
                  path="x.py")
    _write_signal(tmp_path, bot_id="team_bot_c", sig_type="unregistered_script",
                  path="y.py")
    proposals = observe(WorkspaceInventoryContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].bot_id == "admin_bot"


def test_observe_ignores_manifest_quality_signal_types(tmp_path):
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="stale", path="x")
    _write_signal(tmp_path, bot_id="admin_bot", sig_type="misplaced_secret", path="x")
    proposals = observe(WorkspaceInventoryContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []
