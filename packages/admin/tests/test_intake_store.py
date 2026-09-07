"""tests/test_intake_store.py — Intake store envelope + state machine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_intake(**overrides):
    from evolve_admin.intake.envelope import Intake, IntakeContext

    kwargs = {
        "id": "intake-20260514-aaaa",
        "kind": "bug",
        "body": "thing X is broken",
        "context": IntakeContext(primary_bot="evo", active_bot="team_bot_a"),
    }
    kwargs.update(overrides)
    return Intake(**kwargs)


def test_envelope_round_trip():
    from evolve_admin.intake.envelope import Intake

    ix = _make_intake()
    raw = ix.to_dict()
    ix2 = Intake.from_dict(raw)
    assert ix2.id == ix.id
    assert ix2.kind == ix.kind
    assert ix2.body == ix.body
    assert ix2.context.primary_bot == "evo"
    assert ix2.context.active_bot == "team_bot_a"
    assert ix2.state == "open"
    assert ix2.schema_version == ix.schema_version


def test_envelope_rejects_bad_kind():
    from evolve_admin.intake.envelope import Intake

    with pytest.raises(ValueError):
        Intake(id="x", kind="nope", body="thing")  # type: ignore[arg-type]


def test_envelope_rejects_empty_body():
    from evolve_admin.intake.envelope import Intake

    with pytest.raises(ValueError):
        Intake(id="x", kind="bug", body="   ")


def test_write_and_find(tmp_path):
    from evolve_admin.intake import store

    ix = _make_intake()
    path = store.write_intake(ix, tmp_path)
    assert path.exists()
    # File lives in the open subdir (state="open")
    assert path.parent.name == "open"

    located = store.find_intake(tmp_path, ix.id)
    assert located is not None
    found, found_path, subdir = located
    assert found.id == ix.id
    assert found_path == path
    assert subdir == "open"


def test_find_returns_none_when_absent(tmp_path):
    from evolve_admin.intake import store

    assert store.find_intake(tmp_path, "intake-missing") is None


def test_iter_intakes_filters_by_kind_and_state(tmp_path):
    from evolve_admin.intake import store

    bug = _make_intake(id="intake-20260514-b001", kind="bug", body="bug a")
    feat = _make_intake(id="intake-20260514-f001", kind="feature", body="feat a")
    bug_closed = _make_intake(
        id="intake-20260514-b002", kind="bug", body="bug b", state="closed"
    )
    for ix in (bug, feat, bug_closed):
        store.write_intake(ix, tmp_path)

    all_bug = [ix for ix in store.iter_intakes(tmp_path, kind="bug")]
    assert {ix.id for ix in all_bug} == {bug.id, bug_closed.id}

    open_only = [ix for ix in store.iter_intakes(tmp_path, state="open")]
    assert {ix.id for ix in open_only} == {bug.id, feat.id}


def test_transition_moves_file_and_appends_log(tmp_path):
    from evolve_admin.intake import store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)
    open_path = store.intake_path(tmp_path, ix.id, subdir="open")
    assert open_path.exists()

    store.transition(ix, to="triaged", shared_dir=tmp_path, actor="admin", note="reviewed")

    assert not open_path.exists()
    triaged_path = store.intake_path(tmp_path, ix.id, subdir="triaged")
    assert triaged_path.exists()

    # Log entry exists
    log_dir = tmp_path / "intake" / "log"
    log_files = list(log_dir.glob("*.jsonl"))
    assert log_files, "expected at least one log file"
    entries = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    assert any(
        e["id"] == ix.id and e["from"] == "open" and e["to"] == "triaged"
        for e in entries
    )


def test_transition_rejects_illegal_edge(tmp_path):
    from evolve_admin.intake import store

    ix = _make_intake(state="filed")
    store.write_intake(ix, tmp_path)
    # filed → triaged is not legal
    with pytest.raises(store.IllegalTransitionError):
        store.transition(ix, to="triaged", shared_dir=tmp_path)


def test_transition_preserves_old_file_on_failure(tmp_path, monkeypatch):
    """If writing the new path fails, the old file must remain readable."""
    from evolve_admin.intake import store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)
    open_path = store.intake_path(tmp_path, ix.id, subdir="open")
    assert open_path.exists()

    # Simulate write failure on the new file
    original = store._atomic_write_json

    def boom(data, path):
        if path.parent.name == "triaged":
            raise OSError("disk full")
        original(data, path)

    monkeypatch.setattr(store, "_atomic_write_json", boom)
    with pytest.raises(OSError):
        store.transition(ix, to="triaged", shared_dir=tmp_path)

    # Old file still present
    assert open_path.exists()


def test_new_intake_id_format():
    from evolve_admin.intake import store
    import re

    iid = store.new_intake_id()
    assert re.fullmatch(r"intake-\d{8}-[0-9a-f]{4}", iid), iid


def test_intake_file_mode_is_world_readable(tmp_path):
    """Cross-user read concern — file must be 0o644 not 0o600."""
    from evolve_admin.intake import store

    ix = _make_intake()
    path = store.write_intake(ix, tmp_path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o644, f"got {oct(mode)}"
