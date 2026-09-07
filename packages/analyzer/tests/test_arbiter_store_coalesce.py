"""tests/test_arbiter_store_coalesce.py — write-time coalescing.

Spec: internal/spec-recommendations-rework-2026-06-02.md (Phase 1 coalesce
+ humanize + reclassify). When multiple Proposals share a non-empty
``coalesce_key``, ``arbiter.store.write_proposal`` folds the 2nd..Nth
into the first as ``sub_findings`` entries instead of writing N
separate files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import (  # noqa: E402
    iter_proposals,
    load_proposal_file,
    proposal_path,
    write_proposal,
)
from testing.harness import make_investigation_proposal  # noqa: E402


def _seed(*, coalesce_key: str | None, trigger: str, problem: str):
    p = make_investigation_proposal(bot_id="team-bot-a", problem=problem)
    transition(p, "pending", actor="test", reason="seed")
    p.trigger_observations = [trigger]
    p.coalesce_key = coalesce_key
    p.human_title = "app-evolve-framework: undeclared egress hosts"
    return p


def test_three_proposals_same_key_become_one_file(tmp_path):
    """Three Proposals sharing a coalesce_key produce 1 file with 2
    sub_findings (the parent's own finding is implicit, not duplicated
    into its own sub_findings)."""
    a = _seed(coalesce_key="k1", trigger="t-a", problem="missing host A")
    b = _seed(coalesce_key="k1", trigger="t-b", problem="missing host B")
    c = _seed(coalesce_key="k1", trigger="t-c", problem="missing host C")

    write_proposal(a, tmp_path)
    write_proposal(b, tmp_path)
    write_proposal(c, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(pending) == 1, "all three should fold into one parent"
    parent = pending[0]
    assert parent.id == a.id, "first writer becomes the parent"
    assert len(parent.sub_findings) == 2
    triggers = {sf["trigger_observation"] for sf in parent.sub_findings}
    assert triggers == {"t-b", "t-c"}

    # Sibling files MUST NOT exist on disk — coalescing replaces the
    # per-finding write, it doesn't shadow it.
    assert not proposal_path(tmp_path, b.id, subdir="pending").exists()
    assert not proposal_path(tmp_path, c.id, subdir="pending").exists()


def test_no_coalesce_when_key_unset(tmp_path):
    """Without a coalesce_key, every proposal writes as its own file —
    today's behavior for every pre-2026-06-09 proposal."""
    a = _seed(coalesce_key=None, trigger="t-a", problem="finding A")
    b = _seed(coalesce_key=None, trigger="t-b", problem="finding B")
    write_proposal(a, tmp_path)
    write_proposal(b, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert {p.id for p in pending} == {a.id, b.id}


def test_different_keys_dont_merge(tmp_path):
    """Proposals with different coalesce_keys stay independent — the
    key is what scopes the merge."""
    a = _seed(coalesce_key="k-egress", trigger="t-a", problem="A")
    b = _seed(coalesce_key="k-execs",  trigger="t-b", problem="B")
    write_proposal(a, tmp_path)
    write_proposal(b, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert {p.id for p in pending} == {a.id, b.id}
    assert all(not p.sub_findings for p in pending)


def test_idempotent_on_reemit(tmp_path):
    """Re-emitting a finding that's already a sub_finding is a no-op —
    the second cycle of a generator that landed N findings round-1
    shouldn't grow the parent's sub_findings on round-2."""
    a = _seed(coalesce_key="k1", trigger="t-a", problem="A")
    b = _seed(coalesce_key="k1", trigger="t-b", problem="B")
    write_proposal(a, tmp_path)
    write_proposal(b, tmp_path)
    # Re-emit B with a NEW id but same trigger — what happens on a
    # generator's second observation cycle.
    b2 = _seed(coalesce_key="k1", trigger="t-b", problem="B again")
    write_proposal(b2, tmp_path)
    # And re-emit A — parent's own trigger; also a no-op.
    a2 = _seed(coalesce_key="k1", trigger="t-a", problem="A again")
    write_proposal(a2, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(pending) == 1
    parent = pending[0]
    assert parent.id == a.id
    assert len(parent.sub_findings) == 1
    assert parent.sub_findings[0]["trigger_observation"] == "t-b"


def test_round_trip_preserves_coalesce_fields(tmp_path):
    """The new fields survive the to_dict / from_dict roundtrip — old
    proposals on disk without them load with sensible defaults."""
    p = _seed(coalesce_key="k1", trigger="t-a", problem="A")
    p.sub_findings.append({
        "id": "sf-1",
        "trigger_observation": "t-b",
        "problem": "B",
        "admin_surface_summary": "B",
        "summary": None,
        "provenance_signals": {"k": "v"},
        "dismiss_signature": None,
        "created_at": "2026-06-09T00:00:00+00:00",
    })
    write_proposal(p, tmp_path)
    path = proposal_path(tmp_path, p.id, subdir="pending")
    loaded = load_proposal_file(path)
    assert loaded is not None
    assert loaded.coalesce_key == "k1"
    assert loaded.human_title == "app-evolve-framework: undeclared egress hosts"
    assert len(loaded.sub_findings) == 1
    assert loaded.sub_findings[0]["trigger_observation"] == "t-b"

    # Backward-compat: drop the new keys from disk and reload.
    raw = json.loads(path.read_text())
    raw.pop("coalesce_key")
    raw.pop("human_title")
    raw.pop("sub_findings")
    path.write_text(json.dumps(raw))
    loaded_old = load_proposal_file(path)
    assert loaded_old is not None
    assert loaded_old.coalesce_key is None
    assert loaded_old.human_title is None
    assert loaded_old.sub_findings == []
