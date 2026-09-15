"""tests/test_workspace_security_generator.py — workspace_security factory + observe."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.workspace_security.observe import (  # noqa: E402
    WorkspaceSecurityContext,
    observe,
)
from generators.workspace_security.signal_proposals import (  # noqa: E402
    make_misplaced_secret_proposal,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _misplaced_secret_rollup_signal(
    *,
    sig_id: str = "sig-sec-1",
    bot_id: str = "admin_bot",
    items: list[dict] | None = None,
    path: str = "notes/keys.md",
    message: str = "GitHub PAT (classic) found in workspace file",
) -> SimpleNamespace:
    """Build a rollup-shape misplaced_secret Signal."""
    if items is None:
        items = [{"path": path, "message": message}]
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="misplaced_secret",
        details={"items": items, "item_count": len(items)},
    )


def test_factory_produces_security_critical_proposal():
    proposals = make_misplaced_secret_proposal(_misplaced_secret_rollup_signal())
    assert len(proposals) == 1
    p = proposals[0]
    assert p.generator_id == "workspace_security"
    assert p.dimension == "safety"
    assert p.action.kind == "Investigation"
    assert p.urgency == "security_critical"
    assert p.approval_audience == "pod_operator"
    assert p.motivating_signals == ["sig-sec-1"]
    assert "notes/keys.md" in p.problem
    assert "rotate" in p.action.context.lower() or "rotation" in p.action.context.lower()


def test_factory_records_path_and_message_in_provenance():
    sig = _misplaced_secret_rollup_signal(
        path="docs/old.txt", message="Anthropic API key",
    )
    p = make_misplaced_secret_proposal(sig)[0]
    assert p.provenance.signals["path"] == "docs/old.txt"
    assert "Anthropic" in p.provenance.signals["message"]


def test_factory_fans_out_over_items():
    """A rollup with N secrets → N security_critical proposals."""
    sig = _misplaced_secret_rollup_signal(items=[
        {"path": "a.md", "message": "GitHub PAT"},
        {"path": "b.env", "message": "OpenAI key"},
    ])
    proposals = make_misplaced_secret_proposal(sig)
    assert len(proposals) == 2
    paths = {p.provenance.signals["path"] for p in proposals}
    assert paths == {"a.md", "b.env"}
    assert all(p.urgency == "security_critical" for p in proposals)


def test_factory_handles_legacy_per_item_signal():
    """Pre-rollup signal shape still in the store transiently."""
    sig = {
        "id": "sig-dict",
        "bot_id": "team_bot_c",
        "type": "misplaced_secret",
        "details": {"path": "x.env", "message": "OpenAI project key"},
    }
    proposals = make_misplaced_secret_proposal(sig)
    assert len(proposals) == 1
    assert proposals[0].bot_id == "team_bot_c"
    assert proposals[0].urgency == "security_critical"


# ── observe() end-to-end ─────────────────────────────────────────────────────


def _write_misplaced_secret_signal(
    shared_dir: Path, *, bot_id: str, items: list[dict] | None = None,
    path: str = "notes/keys.md",
) -> str:
    """Write a rollup-shape misplaced_secret signal (one item by default)."""
    if items is None:
        items = [{"path": path, "message": "secret found"}]
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("compliance_scan", "misplaced_secret", bot_id),
        producer="compliance_scan",
        type="misplaced_secret",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: misplaced secret",
        details={"items": items, "item_count": len(items)},
    )
    return sig.id


def test_observe_fans_out_rollup_items_into_proposals(tmp_path):
    """One rollup Signal with two items → two security_critical proposals."""
    _write_misplaced_secret_signal(tmp_path, bot_id="admin_bot", items=[
        {"path": "notes/a.md", "message": "GitHub PAT"},
        {"path": "notes/b.md", "message": "OpenAI key"},
    ])
    proposals = observe(WorkspaceSecurityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 2
    for p in proposals:
        assert p.urgency == "security_critical"


def test_observe_filters_by_bot_id(tmp_path):
    _write_misplaced_secret_signal(tmp_path, bot_id="admin_bot")
    _write_misplaced_secret_signal(tmp_path, bot_id="team_bot_c")
    proposals = observe(WorkspaceSecurityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].bot_id == "admin_bot"


def test_observe_ignores_other_signal_types(tmp_path):
    """unregistered_script / stale belong to the other compliance generators."""
    signals_store.observe(
        tmp_path,
        signature=make_signature("compliance_scan", "stale", "admin_bot::app"),
        producer="compliance_scan",
        type="stale",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="stale",
        details={"app_id": "app", "message": "stale"},
    )
    proposals = observe(WorkspaceSecurityContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_returns_empty_when_no_signals(tmp_path):
    assert observe(WorkspaceSecurityContext(bot_id="admin_bot", shared_dir=tmp_path)) == []
