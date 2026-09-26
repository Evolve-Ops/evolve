"""Regression tests for the pre-merge review findings on U4.1 Phase A.

Each test pins one reviewed failure mode:
  1. unknown/empty tool surface must fail CLOSED (never strip denies)
  2. non-dict "tools" in a hand-edited openclaw.json must not TypeError
  3. monitor tooling failure must not sweep-resolve autonomy Signals
  4. backfill review findings survive an unreadable openclaw.json
  5. dry-run (create_missing=False) must not persist inferred entries
  6. delete-reachable can never be inferred as "Drafts only"
  7. the intent file is world-readable (the bot-user guidance surface)
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from autonomy import backfill as _backfill
from autonomy import catalog as _catalog
from autonomy import renderer as _renderer
from autonomy import store as _store
from permissions import monitor as _mon


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _make_home(tmp_path: Path, *, deny: list[str] | None = None) -> Path:
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    cfg: dict = {
        "mcp": {"servers": {
            "google_workspace": {"command": "uvx", "args": ["workspace-mcp"]},
        }},
    }
    if deny is not None:
        cfg["tools"] = {"deny": deny}
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps(cfg))
    return h


def test_empty_tool_surface_fails_closed(shared_dir: Path, tmp_path: Path, monkeypatch):
    """If the known tool surface vanishes (catalog unimportable / entry
    renamed), the renderer must leave the owned prefix UNTOUCHED — an
    empty expected slice would strip the send deny and silently widen."""
    home = _make_home(tmp_path, deny=["mcp__google_workspace__send_gmail_message"])
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    monkeypatch.setattr(_catalog, "known_server_tools", lambda iid: [])

    posture = _store.load(shared_dir, "alpha").integrations["google_workspace"]
    assert _renderer.expected_deny_entries(posture) is None

    result = _renderer.render_bot("alpha", shared_dir, home_override=home)
    assert not result.changed
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert "mcp__google_workspace__send_gmail_message" in cfg["tools"]["deny"]


def test_merge_survives_non_dict_tools():
    merged, changed = _renderer.merge_deny_slice(
        {"tools": "corrupted"},
        {"google_workspace": ["mcp__google_workspace__send_gmail_message"]},
    )
    assert changed
    assert merged["tools"]["deny"] == ["mcp__google_workspace__send_gmail_message"]


class _SweepRecorder:
    """Stub for the monitor's optional signals-store dependency."""

    def __init__(self):
        self.observed = []
        self.sweeps = []

    def observe(self, shared_dir, **kwargs):
        self.observed.append(kwargs)

    def sweep_resolve(self, shared_dir, **kwargs):
        self.sweeps.append(kwargs)
        return []


def test_tooling_failure_excludes_bot_from_autonomy_sweep(
    shared_dir: Path, monkeypatch,
):
    """A crashed autonomy check is 'never ran', not 'condition cleared':
    the autonomy-type sweep must skip that bot so its Signals persist."""
    recorder = _SweepRecorder()
    monkeypatch.setattr(_mon, "_signals_store", recorder)
    monkeypatch.setattr(_mon, "_make_signature", lambda p, t, s: f"{p}:{t}:{s}")

    from autonomy import coherence as _coherence
    def _boom(bot_id, sd, config=None, **kwargs):
        raise RuntimeError("transient read failure")
    monkeypatch.setattr(_coherence, "check_bot", _boom)

    # Bot homes don't exist on the dev box → perm findings are the
    # missing-config kind; that's fine, we only care about the sweeps.
    _mon.run(shared_dir, ["alpha"], config={"bots": {"alpha": {}}})

    # First sweep: non-autonomy types only.
    assert recorder.sweeps, "expected at least the base sweep"
    base_sweep = recorder.sweeps[0]
    assert not (set(base_sweep["types"]) & _mon.AUTONOMY_SIGNAL_TYPES)
    # No autonomy sweep at all — the only bot's check failed.
    autonomy_sweeps = [
        s for s in recorder.sweeps if set(s["types"]) & _mon.AUTONOMY_SIGNAL_TYPES
    ]
    assert autonomy_sweeps == []


def test_autonomy_sweep_scoped_to_succeeding_bots(shared_dir: Path, monkeypatch):
    recorder = _SweepRecorder()
    monkeypatch.setattr(_mon, "_signals_store", recorder)
    monkeypatch.setattr(_mon, "_make_signature", lambda p, t, s: f"{p}:{t}:{s}")

    _mon.run(shared_dir, ["alpha"], config={"bots": {"alpha": {}}})
    autonomy_sweeps = [
        s for s in recorder.sweeps if set(s["types"]) & _mon.AUTONOMY_SIGNAL_TYPES
    ]
    assert len(autonomy_sweeps) == 1
    assert autonomy_sweeps[0]["bot_ids"] == {"alpha"}


def test_pending_reviews_survive_unreadable_openclaw(
    shared_dir: Path, tmp_path: Path,
):
    """The review condition lives in the posture file; a transient
    openclaw read failure must not drop it (the monitor sweep would
    auto-resolve a still-true Signal)."""
    home = _make_home(tmp_path)
    _backfill.ensure_backfilled(shared_dir, "alpha", home_override=home)
    # Now make openclaw unreadable (missing home).
    findings = _backfill.ensure_backfilled(
        shared_dir, "alpha", home_override=tmp_path / "gone",
    )
    assert len(findings) == 1
    assert findings[0]["type"] == "autonomy_backfill_review"


def test_dry_run_does_not_create_entries(shared_dir: Path, tmp_path: Path):
    home = _make_home(tmp_path)
    findings = _backfill.ensure_backfilled(
        shared_dir, "alpha", home_override=home, create_missing=False,
    )
    assert findings == []
    assert _store.load(shared_dir, "alpha") is None


def test_reachable_delete_never_infers_draft_only():
    """Every rung's operator copy promises 'never deletes' — inferring
    'Drafts only' over a reachable delete tool would be a sign
    pretending to be a wall."""
    spec = _catalog.KIND_SPECS["email"]
    binding = _catalog.INTEGRATION_BINDINGS["google_workspace"]
    tools = ["send_gmail_message", "delete_gmail_message", "search_gmail_messages"]
    rung = _backfill.infer_rung(
        "google_workspace", spec, binding, tools,
        live_deny={"mcp__google_workspace__send_gmail_message"},  # send blocked, delete reachable
    )
    assert rung == "act_with_approval"


def test_intent_file_is_world_readable(shared_dir: Path):
    """session_surface.load_autonomy_block runs as the bot user; an
    0600 intent file would silently kill the guidance surface."""
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    mode = stat.S_IMODE(_store.autonomy_path(shared_dir, "alpha").stat().st_mode)
    assert mode & stat.S_IROTH, f"autonomy.json mode {oct(mode)} not world-readable"
