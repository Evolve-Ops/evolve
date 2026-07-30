"""Spec-id supersession lineage for v7-arc Instances.

Pins applications/spec_lineage.py per the app-identity ledger sub-spec of
docs/spec-apps-meta-2026-06-13.md (and docs/spec-manifest-v7-2026-05-20.md §5):

  - record_spec_supersession appends to provenance.prior_spec_ids[], dedups,
    preserves order, refuses self-supersession, and is back-compat on an
    Instance with no prior_spec_ids key (or no provenance block).
  - resolve_spec returns the live Instance whose current spec_id OR whose
    prior_spec_ids[] contains the queried spec_id; None when nothing resolves.
  - build_spec_index gives the same answers as a dict, current binding winning
    over a retired one on collision.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.spec_lineage import (  # noqa: E402
    build_spec_index,
    current_spec_id,
    prior_spec_ids,
    record_spec_supersession,
    resolve_spec,
)


def _instance(spec_id: str, prior: list[str] | None = None, **extra) -> dict:
    prov: dict = {"spec_id": spec_id}
    if prior is not None:
        prov["prior_spec_ids"] = list(prior)
    return {"instance_id": extra.get("instance_id", "journal"),
            "provenance": prov, **extra}


# ── resolve_spec ──────────────────────────────────────────────────────────────

class TestResolveSpec:
    def test_resolves_current_and_prior_ids(self):
        inst = _instance("p-new", prior=["p-old1", "p-old2"])
        instances = [inst]
        assert resolve_spec("p-old1", instances) is inst
        assert resolve_spec("p-old2", instances) is inst
        assert resolve_spec("p-new", instances) is inst

    def test_unknown_returns_none(self):
        inst = _instance("p-new", prior=["p-old1", "p-old2"])
        assert resolve_spec("p-unknown", [inst]) is None

    def test_empty_spec_id_returns_none(self):
        assert resolve_spec("", [_instance("p-new")]) is None

    def test_empty_instances_returns_none(self):
        assert resolve_spec("p-new", []) is None

    def test_picks_right_instance_among_many(self):
        a = _instance("p-a", prior=["p-a0"], instance_id="a")
        b = _instance("p-b", prior=["p-b0", "p-b1"], instance_id="b")
        c = _instance("p-c", instance_id="c")
        instances = [a, b, c]
        assert resolve_spec("p-b1", instances) is b
        assert resolve_spec("p-a0", instances) is a
        assert resolve_spec("p-c", instances) is c

    def test_current_binding_wins_over_retired(self):
        # p-x is live on `live` and a retired id on `stale`.
        live = _instance("p-x", instance_id="live")
        stale = _instance("p-y", prior=["p-x"], instance_id="stale")
        # Current binding wins regardless of iteration order.
        assert resolve_spec("p-x", [stale, live]) is live
        assert resolve_spec("p-x", [live, stale]) is live

    def test_tolerates_missing_or_garbled_provenance(self):
        good = _instance("p-good")
        instances = [
            {"instance_id": "no-prov"},               # no provenance key
            {"provenance": "not-a-dict"},             # garbled provenance
            {"provenance": {"spec_id": 123}},         # non-str spec_id
            good,
        ]
        assert resolve_spec("p-good", instances) is good
        assert resolve_spec("p-missing", instances) is None


# ── record_spec_supersession ──────────────────────────────────────────────────

class TestRecordSpecSupersession:
    def test_appends(self):
        inst = _instance("p-new", prior=["p-old1"])
        record_spec_supersession(inst, "p-old2")
        assert prior_spec_ids(inst) == ["p-old1", "p-old2"]

    def test_back_compat_no_prior_key(self):
        inst = _instance("p-new")  # no prior_spec_ids key at all
        assert "prior_spec_ids" not in inst["provenance"]
        record_spec_supersession(inst, "p-old1")
        assert inst["provenance"]["prior_spec_ids"] == ["p-old1"]

    def test_back_compat_no_provenance_block(self):
        inst = {"instance_id": "journal"}  # no provenance at all
        record_spec_supersession(inst, "p-old1")
        assert inst["provenance"]["prior_spec_ids"] == ["p-old1"]

    def test_dedups(self):
        inst = _instance("p-new", prior=["p-old1"])
        record_spec_supersession(inst, "p-old1")  # already present
        assert prior_spec_ids(inst) == ["p-old1"]

    def test_preserves_order(self):
        inst = _instance("p-new")
        for sid in ["p-a", "p-b", "p-c"]:
            record_spec_supersession(inst, sid)
        record_spec_supersession(inst, "p-b")  # re-add a middle one → no move
        assert prior_spec_ids(inst) == ["p-a", "p-b", "p-c"]

    def test_no_self_supersession(self):
        inst = _instance("p-new", prior=["p-old1"])
        record_spec_supersession(inst, "p-new")  # == current spec_id
        assert prior_spec_ids(inst) == ["p-old1"]

    def test_empty_old_id_is_noop(self):
        inst = _instance("p-new", prior=["p-old1"])
        record_spec_supersession(inst, "")
        assert prior_spec_ids(inst) == ["p-old1"]

    def test_returns_the_instance(self):
        inst = _instance("p-new")
        assert record_spec_supersession(inst, "p-old1") is inst

    def test_chained_respec_is_unbroken(self):
        # p-a → p-b → p-c: each re-spec carries the chain forward.
        inst = _instance("p-b", prior=["p-a"])
        # Now re-spec to p-c: caller records the whole old chain + old current.
        inst["provenance"]["spec_id"] = "p-c"
        for sid in [*["p-a"], "p-b"]:
            record_spec_supersession(inst, sid)
        assert prior_spec_ids(inst) == ["p-a", "p-b"]
        assert current_spec_id(inst) == "p-c"


# ── record_spec_supersessions (bulk helper for the 1→N split) ──────────────────

class TestRecordSpecSupersessions:
    def test_appends_all_in_order(self):
        from evolve_admin.applications.spec_lineage import record_spec_supersessions
        inst = _instance("p-new")
        record_spec_supersessions(inst, ["p-a", "p-b", "p-c"])
        assert prior_spec_ids(inst) == ["p-a", "p-b", "p-c"]

    def test_dedups_and_skips_self_and_empty(self):
        from evolve_admin.applications.spec_lineage import record_spec_supersessions
        inst = _instance("p-new", prior=["p-a"])
        record_spec_supersessions(inst, ["p-a", "", "p-new", "p-b"])
        assert prior_spec_ids(inst) == ["p-a", "p-b"]

    def test_returns_the_instance(self):
        from evolve_admin.applications.spec_lineage import record_spec_supersessions
        inst = _instance("p-new")
        assert record_spec_supersessions(inst, ["p-a"]) is inst


# ── build_spec_index ──────────────────────────────────────────────────────────

class TestBuildSpecIndex:
    def test_indexes_current_and_prior(self):
        a = _instance("p-a", prior=["p-a0"], instance_id="a")
        b = _instance("p-b", prior=["p-b0", "p-b1"], instance_id="b")
        idx = build_spec_index([a, b])
        assert idx["p-a"] is a
        assert idx["p-a0"] is a
        assert idx["p-b"] is b
        assert idx["p-b0"] is b
        assert idx["p-b1"] is b
        assert "p-missing" not in idx

    def test_current_binding_wins_on_collision(self):
        live = _instance("p-x", instance_id="live")
        stale = _instance("p-y", prior=["p-x"], instance_id="stale")
        idx = build_spec_index([stale, live])
        assert idx["p-x"] is live  # live current beats stale's retired entry

    def test_agrees_with_resolve_spec(self):
        insts = [
            _instance("p-a", prior=["p-a0"], instance_id="a"),
            _instance("p-b", prior=["p-b0"], instance_id="b"),
        ]
        idx = build_spec_index(insts)
        for sid in ["p-a", "p-a0", "p-b", "p-b0"]:
            assert idx.get(sid) is resolve_spec(sid, insts)
