"""Tests for the F-3 posture-dial component dependency graph + load-validator.

Spec: docs/spec-footprint-2026-06-18.md § "FD-5 engine design" (part 1). Edge
list: docs/footprint-dependency-graph-2026-06-18.md (the verified F-2.5 graph).

These pin the structural invariants the (future) cascade engine relies on:

  * the live graph is internally consistent (`graph_consistency_errors() == []`);
  * the #3322 output-declaration entries still load and still pass the output
    contract unchanged (the two registries share one dict, do not interfere);
  * required_by is reciprocal with requires (derived → never drifts);
  * the cascade DAG is acyclic;
  * FD-7 — only cascade + safe-leaf are dial-scoped; the infra floor is excluded;
  * FD-3 — safety_floor items are dial-scoped, never infra-floor;
  * fail-safe — an `unverified` (graph §6 soft) edge is honored as a REAL edge
    (still produces a required_by link / cascade dependency, never skipped);
  * the validator actually BLOCKS each malformed shape (mutated copies fail).
"""

from __future__ import annotations

import dataclasses
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

from footprint import components as reg

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "footprint-graph-lint"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("footprint_graph_lint", str(_TOOL))
    spec = importlib.util.spec_from_loader("footprint_graph_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["footprint_graph_lint"] = mod
    loader.exec_module(mod)
    return mod


lint = _load_tool()


# ── The live graph is sound ──────────────────────────────────────────────────
def test_live_graph_is_consistent():
    assert reg.graph_consistency_errors() == []


def test_graph_lint_exits_zero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["footprint-graph-lint"])
    assert lint.main() == 0


def test_graph_has_substantive_node_set():
    nodes = reg.graph_nodes()
    # The superset: DEFAULT_MODULES (~12) + tier capabilities (~7) + infra floor
    # (~7) + monitors/appliers/leaves. Floor for "the backfill happened".
    assert len(nodes) >= 35
    kinds = {c.kind for c in nodes.values()}
    assert kinds <= reg.VALID_KINDS
    # Every documented kind is exercised by ≥1 node.
    assert {
        reg.KIND_MODULE, reg.KIND_TIER_CAPABILITY, reg.KIND_INFRA,
        reg.KIND_DAEMON, reg.KIND_MONITOR, reg.KIND_APPLIER, reg.KIND_RUNTIME,
    } <= kinds


# ── #3322 output contract is untouched by the graph extension ────────────────
def test_output_entries_still_load_and_pass():
    # The output-only entries (kind == "") keep passing the output contract — the
    # graph nodes (which carry no outputs) do not trip the "≥1 output" rule.
    assert reg.consistency_errors() == []
    output_only = [c for c in reg.FOOTPRINT_COMPONENTS.values() if not c.kind]
    assert len(output_only) >= 20
    assert all(c.outputs for c in output_only)


def test_graph_and_output_entries_are_disjoint_axes():
    # A graph node carries kind + classification + no outputs; an output entry is
    # the inverse. They never collide on id (one shared registry).
    for c in reg.FOOTPRINT_COMPONENTS.values():
        if c.kind:
            assert c.classification in reg.VALID_CLASSIFICATIONS
            assert not c.outputs, f"{c.id}: graph node unexpectedly carries outputs"
        else:
            assert c.outputs, f"{c.id}: output entry must declare ≥1 output"


# ── Edge invariants ──────────────────────────────────────────────────────────
def test_required_by_is_reciprocal_with_requires():
    nodes = reg.graph_nodes()
    for cid, comp in nodes.items():
        for dep in comp.requires:
            assert cid in nodes[dep].required_by, (
                f"{cid} requires {dep} but {dep}.required_by omits it"
            )
        for rb in comp.required_by:
            assert cid in nodes[rb].requires, (
                f"{cid}.required_by lists {rb} but {rb}.requires omits it"
            )


def test_every_requires_target_is_a_graph_node():
    nodes = reg.graph_nodes()
    for cid, comp in nodes.items():
        for dep in comp.requires:
            assert dep in nodes, f"{cid} requires {dep!r} which is not a graph node"


def test_cascade_dag_is_acyclic():
    assert reg._requires_cycle(reg.graph_nodes()) == []


# ── FD-7 / FD-3 classification invariants ────────────────────────────────────
def test_infra_floor_is_never_dial_scoped():
    for c in reg.graph_nodes().values():
        if c.classification == reg.CLASS_INFRA_FLOOR:
            assert not reg.is_dial_scoped(c), f"{c.id}: floor must be out of the dial"
    # And the dial domain is exactly the non-floor nodes.
    dial = reg.dial_components()
    assert all(reg.is_dial_scoped(c) for c in dial.values())
    assert all(c.classification != reg.CLASS_INFRA_FLOOR for c in dial.values())


def test_safety_floor_items_are_dial_scoped():
    # FD-3: the cost breakers + security_warden are dialable-in-principle but kept
    # ON — they are NOT infra-floor (graph §4 note).
    safety = [c for c in reg.graph_nodes().values() if c.safety_floor]
    assert safety, "expected the FD-3 safety-floor breakers to be present"
    for c in safety:
        assert c.classification != reg.CLASS_INFRA_FLOOR
        assert reg.is_dial_scoped(c)


def test_known_floor_and_dial_members():
    # Spot-check the load-bearing classifications against the graph doc §4.
    nodes = reg.graph_nodes()
    for floor in ("gateway_plugin_install", "sudoers", "openclaw_acls",
                  "admin_daemon", "allow_conversation_access",
                  "repo_puller_security"):
        assert nodes[floor].classification == reg.CLASS_INFRA_FLOOR
    for safe in ("inject_keywords", "inject_pod_conduct", "preflight",
                 "healing", "expansion"):
        assert nodes[safe].classification == reg.CLASS_SAFE_LEAF
    for casc in ("observer", "tier_ladder", "rsi", "analysis", "apply"):
        assert nodes[casc].classification == reg.CLASS_CASCADE


# ── Fail-safe: unverified soft edges are honored as REAL edges ────────────────
def test_unverified_edges_are_honored_not_skipped():
    nodes = reg.graph_nodes()
    unverified = [c for c in nodes.values() if c.unverified]
    # The graph §6 soft items are present.
    ids = {c.id for c in unverified}
    assert {"autonomy_caps", "tuples", "signal_subscriber",
            "allow_conversation_access", "repo_puller_security"} <= ids
    # Every unverified node documents its provenance (validator enforces this).
    for c in unverified:
        assert c.note, f"{c.id}: unverified node must cite its soft edge"
    # The §6.2 soft REQUIRES edge still produces a cascade link: even though the
    # autonomy_caps→outward_action_ledger read is asserted-not-traced, the engine
    # sees outward_action_ledger.required_by ∋ autonomy_caps, so dialing the ledger
    # off pulls the cap with it (fail-toward-NOT-disabling).
    caps = nodes["autonomy_caps"]
    assert "outward_action_ledger" in caps.requires
    assert "autonomy_caps" in nodes["outward_action_ledger"].required_by
    # honored_requires never drops a soft edge.
    assert reg.honored_requires(caps) == caps.requires


# ── The validator actually blocks each malformed shape ───────────────────────
def _patch(monkeypatch, cid, **changes):
    """Replace one node in a copy of the registry and return the mutated dict."""
    nodes = dict(reg.FOOTPRINT_COMPONENTS)
    nodes[cid] = dataclasses.replace(nodes[cid], **changes)
    monkeypatch.setattr(reg, "FOOTPRINT_COMPONENTS", nodes)
    return nodes


def test_validator_blocks_dangling_requires(monkeypatch):
    _patch(monkeypatch, "metrics", requires=("does_not_exist",))
    errs = reg.graph_consistency_errors()
    assert any("does_not_exist" in e and "not a registered" in e for e in errs)


def test_validator_blocks_requires_onto_output_entry(monkeypatch):
    # Wiring a dependency onto a #3322 output-only component (kind == "") is a bug.
    _patch(monkeypatch, "metrics", requires=("signals_store",))
    errs = reg.graph_consistency_errors()
    assert any("signals_store" in e and "output-only" in e for e in errs)


def test_validator_blocks_non_reciprocal_edge(monkeypatch):
    # Hand-set a required_by that contradicts the (derived) requires inversion.
    _patch(monkeypatch, "rsi", required_by=("metrics",))
    errs = reg.graph_consistency_errors()
    assert any("reciprocity" in e for e in errs)


def test_validator_blocks_a_cycle(monkeypatch):
    # rsi normally requires nothing; make it require outcomes → rsi→…→outcomes→…
    # outcomes already requires apply→analysis→rsi, closing a loop.
    _patch(monkeypatch, "rsi", requires=("outcomes",))
    errs = reg.graph_consistency_errors()
    assert any("cycle" in e for e in errs)


def test_validator_blocks_safety_floor_marked_infra_floor(monkeypatch):
    # FD-3: a safety_floor item (kept-ON-but-dialable) is never infra-floor —
    # marking one infra-floor would wrongly exclude it from the dial.
    _patch(monkeypatch, "security_warden",
           classification=reg.CLASS_INFRA_FLOOR)  # safety_floor + infra-floor = illegal
    errs = reg.graph_consistency_errors()
    assert any("safety_floor item must be dial-scoped" in e for e in errs)


def test_fd7_partition_guard_fires_if_constants_corrupted(monkeypatch):
    # The FD-7 guard is a partition invariant on the CONSTANTS (not a per-node
    # tautology): if infra-floor ever leaks into the dial-scoped set, the engine
    # would offer to dial off the control plane. Corrupt the constant and prove
    # the validator blocks.
    monkeypatch.setattr(
        reg, "DIAL_SCOPED_CLASSIFICATIONS",
        reg.DIAL_SCOPED_CLASSIFICATIONS | {reg.CLASS_INFRA_FLOOR},
    )
    errs = reg.graph_consistency_errors()
    assert any("FD-7" in e for e in errs)


def test_validator_blocks_bad_kind_and_dims(monkeypatch):
    _patch(monkeypatch, "expansion", kind="not_a_kind", footprint_dims=("nope",))
    errs = reg.graph_consistency_errors()
    assert any("unknown kind" in e for e in errs)
    assert any("unknown footprint dim" in e for e in errs)


def test_validator_blocks_unverified_without_note(monkeypatch):
    _patch(monkeypatch, "expansion", unverified=True, note="")
    errs = reg.graph_consistency_errors()
    assert any("unverified" in e and "note" in e for e in errs)


def test_validator_blocks_edgeless_cascade(monkeypatch):
    # A cascade node with no requires AND no required_by is a mis-classified leaf.
    _patch(monkeypatch, "security_warden", classification=reg.CLASS_CASCADE)
    errs = reg.graph_consistency_errors()
    assert any("cascade node has no edges" in e for e in errs)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
