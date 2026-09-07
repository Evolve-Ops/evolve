"""Phase 11a — catalog-merge correctness (spec Addendum 8 §D).

Two server-side fixes, tested here:

  - ``ladder_rank_view`` / ``resolve_roles_with_provenance`` rank: derived from
    the cost-ordered LADDER (the 4 user-selectable roles), NOT the role's index
    in the raw merged ``rungs`` array. A mis-keyed pod merge can leave ~9 rungs
    (4 dead code + 5 accumulated pod ``*-default``); the meter must still show a
    monotonic 4-step Fast(1)<Standard(2)<Power(3)<Max(4), cost chips populated.

  - ``canonicalize_catalog_rung_ids``: the write-path normalization that renames
    a one-rung-per-role catalog onto canonical ids + backfilled costClass so a
    saved pod/bot default OVERLAYS the code default (matching ids → replace)
    instead of accumulating synthetic ``*-default`` rungs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import primary_bot as pb  # noqa: E402


# ── rank-from-ladder ──────────────────────────────────────────────────────────


def _junk_9_rung_catalog() -> dict:
    """The live-pod accumulated catalog: 4 code rungs + 5 pod ``*-default``.

    The roles point at the ``*-default`` rungs (the pod layer wins by key); the
    canonical code rungs are dead (no role points at them). The OLD broken code
    counted all 9 rungs and indexed roles into the raw array, producing ~9
    non-monotonic steps (Standard could outrank Power).
    """
    return {
        "rungs": [
            {"id": "haiku-class", "costClass": "low", "models": ["anthropic/claude-haiku-4-5"]},
            {"id": "sonnet-class", "costClass": "medium", "models": ["anthropic/claude-sonnet-4-6"]},
            {"id": "opus-class", "costClass": "high", "models": ["anthropic/claude-opus-4-8"]},
            {"id": "fable-class", "costClass": "premium", "models": ["anthropic/claude-fable-5"]},
            # Pod-layer ``*-default`` rungs, no costClass — the accumulated junk.
            {"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]},
            {"id": "standard-default", "models": ["anthropic/claude-sonnet-4-6"]},
            {"id": "power-default", "models": ["anthropic/claude-opus-4-8"]},
            {"id": "max-default", "models": ["anthropic/claude-fable-5"]},
            {"id": "judge-default", "models": ["openai/gpt-4.1", "anthropic/claude-sonnet-4-6"]},
        ],
        "roles": {
            "fast": "fast-default",
            "standard": "standard-default",
            "power": "power-default",
            "max": "max-default",
            "judge": {"rung": "judge-default", "provider": "not-standard"},
        },
    }


def test_ladder_rank_9_rung_junk_still_four_steps_monotonic():
    view = pb.ladder_rank_view(_junk_9_rung_catalog())
    ranks = {r: view[r]["rank"] for r in ("fast", "standard", "power", "max")}
    assert ranks == {"fast": 1, "standard": 2, "power": 3, "max": 4}, ranks
    # Exactly 4 steps — stray rungs do NOT inflate the count.
    assert {view[r]["rungCount"] for r in view} == {4}
    # Monotonic ascending.
    seq = [ranks["fast"], ranks["standard"], ranks["power"], ranks["max"]]
    assert seq == sorted(seq) == [1, 2, 3, 4]
    # Judge is off-ladder — no rank entry.
    assert "judge" not in view


def test_ladder_rank_clean_four_rung_catalog():
    view = pb.ladder_rank_view(pb.default_model_catalog())
    assert {r: view[r]["rank"] for r in ("fast", "standard", "power", "max")} == {
        "fast": 1, "standard": 2, "power": 3, "max": 4,
    }
    assert all(v["rungCount"] == 4 for v in view.values())


def test_ladder_rank_costclass_orders_over_array_position():
    # Rungs deliberately out of cost order in the array — cost class must win.
    catalog = {
        "rungs": [
            {"id": "r_premium", "costClass": "premium", "models": ["p/x"]},
            {"id": "r_low", "costClass": "low", "models": ["p/y"]},
            {"id": "r_high", "costClass": "high", "models": ["p/z"]},
            {"id": "r_medium", "costClass": "medium", "models": ["p/w"]},
        ],
        "roles": {
            "fast": "r_low", "standard": "r_medium",
            "power": "r_high", "max": "r_premium",
        },
    }
    view = pb.ladder_rank_view(catalog)
    assert view["fast"]["rank"] == 1
    assert view["standard"]["rank"] == 2
    assert view["power"]["rank"] == 3
    assert view["max"]["rank"] == 4


def test_ladder_rank_shared_rung_shares_rank():
    # Two ladder roles pointing at the SAME rung share a rank; rungCount shrinks.
    catalog = {
        "rungs": [
            {"id": "haiku-class", "costClass": "low", "models": ["p/a"]},
            {"id": "sonnet-class", "costClass": "medium", "models": ["p/b"]},
        ],
        "roles": {
            "fast": "haiku-class",
            "standard": "sonnet-class",
            "power": "sonnet-class",  # power shares standard's rung
        },
    }
    view = pb.ladder_rank_view(catalog)
    assert view["fast"]["rank"] == 1
    assert view["standard"]["rank"] == 2
    assert view["power"]["rank"] == 2  # same rung → same rank
    assert {v["rungCount"] for v in view.values()} == {2}
    # max omitted (no role) → not in the ladder view.
    assert "max" not in view


def test_resolve_roles_with_provenance_rank_on_junk_pod_merge():
    # End-to-end: a pod whose network.json::models carries the 9-rung junk
    # resolves through resolve_roles_with_provenance to a 4-step monotonic meter
    # with cost chips populated (even though the on-disk config has stray rungs).
    network = {"models": _junk_9_rung_catalog()}
    out = pb.resolve_roles_with_provenance(network)
    ranks = {r: out[r]["rank"] for r in ("fast", "standard", "power", "max")}
    assert ranks == {"fast": 1, "standard": 2, "power": 3, "max": 4}, ranks
    assert {out[r]["rungCount"] for r in out} == {4}
    # Cost chips populated for every ladder role (no blank costClass).
    for role in ("fast", "standard", "power", "max"):
        assert out[role]["costClass"], (role, out[role])
    # No judge row — the role is gone (judge-role collapse §5.4).
    assert "judge" not in out


# ── defaultModels: the per-tier RECOMMENDED set for the picker (Addendum 6) ───
#
# The bot Custom tier editor's picker stars the "recommended for this tier" set.
# That set is the resolved DEFAULT cluster (defaults ← pod) for the role —
# exactly what "Reset to pod defaults" would put there — credential-matched to
# the bot. resolve_roles_with_provenance surfaces it as ``defaultModels``,
# independent of the bot's CURRENT (possibly-customized) ``models``.


def test_default_models_is_pod_merged_cluster_independent_of_bot_override(monkeypatch):
    # The pod default cluster for `power` is the code default (opus-class). A bot
    # that has OVERRIDDEN power to a cheaper model still gets the pod cluster in
    # defaultModels (recommended), while `models` reflects its override.
    network = {"models": {}}  # pod = code defaults
    bot_doc = {
        "rungs": [
            {"id": "haiku-class", "costClass": "low", "models": ["anthropic/claude-haiku-4-5"]},
        ],
        "roles": {"power": "haiku-class"},  # power overridden to cheap model
    }
    # Stub the bot doc read so we exercise the bot-override path without disk.
    monkeypatch.setattr(pb, "_bot_evolve_tiers_path", lambda net, bid: "/nonexistent")
    monkeypatch.setattr(pb, "_read_bot_owned_json", lambda path: (bot_doc, True))

    out = pb.resolve_roles_with_provenance(network, bot_id="somebot")
    # Current models reflect the bot override ...
    assert out["power"]["models"] == ["anthropic/claude-haiku-4-5"]
    assert out["power"]["layer"] == "bot"
    # ... but the RECOMMENDED set is the pod-merged default cluster for power.
    default_power = pb._rung_models(pb.merge_model_catalog({}, {}), "opus-class")
    assert out["power"]["defaultModels"] == default_power
    assert out["power"]["defaultModels"] != out["power"]["models"]


def test_default_models_credential_matched(monkeypatch):
    # A pod cluster that mixes two providers is filtered to the bot's
    # credentialed providers, so the picker never recommends an unrunnable model.
    network = {
        "models": {
            "rungs": [
                {"id": "standard-default", "costClass": "medium",
                 "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4.1"]},
            ],
            "roles": {"standard": "standard-default"},
        }
    }
    monkeypatch.setattr(pb, "_bot_evolve_tiers_path", lambda net, bid: "/nonexistent")
    monkeypatch.setattr(pb, "_read_bot_owned_json", lambda path: ({}, False))

    # Bot only has an Anthropic key — the openai recommendation must drop out.
    out = pb.resolve_roles_with_provenance(
        network, bot_id="somebot", credentialed_providers={"anthropic"},
    )
    rec = out["standard"]["defaultModels"]
    assert "anthropic/claude-sonnet-4-6" in rec
    assert all(pb.provider_of(m) == "anthropic" for m in rec)
    assert "openai/gpt-4.1" not in rec


def test_default_models_present_for_every_role():
    # Every role carries a defaultModels list (the picker reads it per tier).
    out = pb.resolve_roles_with_provenance({"models": {}})
    for role in ("fast", "standard", "power", "max"):
        assert isinstance(out[role]["defaultModels"], list), role


# ── canonicalize write path ───────────────────────────────────────────────────


def test_canonicalize_renames_and_backfills_costclass():
    pod = {
        "rungs": [
            {"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]},
            {"id": "standard-default", "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4.1"]},
            {"id": "power-default", "models": ["anthropic/claude-opus-4-8"]},
            {"id": "max-default", "models": ["anthropic/claude-fable-5"]},
            {"id": "judge-default", "models": ["openai/gpt-4.1"]},
        ],
        "roles": {
            "fast": "fast-default", "standard": "standard-default",
            "power": "power-default", "max": "max-default",
            "judge": {"rung": "judge-default", "provider": "not-standard"},
        },
    }
    out = pb.canonicalize_catalog_rung_ids(pod)
    # The stale judge role + its rung are dropped by the canonicalizer (the
    # role is gone — judge-role collapse §5.4; the sanctioned model-preserving
    # path is migrate-model-roles, which merges the cluster into standard).
    assert [r["id"] for r in out["rungs"]] == [
        "haiku-class", "sonnet-class", "opus-class", "fable-class",
    ]
    assert [r["costClass"] for r in out["rungs"]] == ["low", "medium", "high", "premium"]
    assert out["roles"]["max"] == "fable-class"
    assert "judge" not in out["roles"]
    sonnet = next(r for r in out["rungs"] if r["id"] == "sonnet-class")
    assert sonnet["models"] == ["anthropic/claude-sonnet-4-6", "openai/gpt-4.1"]


def test_canonicalized_catalog_overlays_code_default_in_merge():
    pod = pb.canonicalize_catalog_rung_ids({
        "rungs": [
            {"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]},
            {"id": "standard-default", "models": ["anthropic/claude-sonnet-4-6"]},
            {"id": "power-default", "models": ["anthropic/claude-opus-4-8"]},
            {"id": "max-default", "models": ["anthropic/claude-fable-5"]},
            {"id": "judge-default", "models": ["openai/gpt-4.1", "anthropic/claude-sonnet-4-6"]},
        ],
        "roles": {
            "fast": "fast-default", "standard": "standard-default",
            "power": "power-default", "max": "max-default",
            "judge": {"rung": "judge-default", "provider": "not-standard"},
        },
    })
    merged = pb.merge_model_catalog(pod, {})
    # OVERLAY, not accumulate — 4 canonical rungs, not 9.
    assert [r["id"] for r in merged["rungs"]] == [
        "haiku-class", "sonnet-class", "opus-class", "fable-class",
    ]


def test_canonicalize_idempotent():
    pod = {
        "rungs": [
            {"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]},
            {"id": "standard-default", "models": ["anthropic/claude-sonnet-4-6"]},
            {"id": "power-default", "models": ["anthropic/claude-opus-4-8"]},
            {"id": "max-default", "models": ["anthropic/claude-fable-5"]},
        ],
        "roles": {
            "fast": "fast-default", "standard": "standard-default",
            "power": "power-default", "max": "max-default",
        },
    }
    once = pb.canonicalize_catalog_rung_ids(pod)
    twice = pb.canonicalize_catalog_rung_ids(once)
    assert once == twice


def test_canonicalize_partial_set_not_fabricated():
    # A 3-tier partial Custom set (no max/judge) keeps its rung count — an empty
    # canonical rung must NOT be minted (it would brick the merge for that role).
    partial = {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
        ],
        "roles": {"fast": "haiku-class", "standard": "sonnet-class", "power": "opus-class"},
    }
    out = pb.canonicalize_catalog_rung_ids(partial)
    assert [r["id"] for r in out["rungs"]] == ["haiku-class", "sonnet-class", "opus-class"]
    assert "max" not in out["roles"]
    assert "judge" not in out["roles"]


def test_canonicalize_no_roles_map_left_untouched():
    # A malformed doc (rungs present, NO roles map) must NOT be wiped — without
    # roles there is nothing to map rungs onto. Leave it exactly as-is.
    malformed = {
        "rungs": [{"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]}],
        "roles": {},
    }
    out = pb.canonicalize_catalog_rung_ids(malformed)
    assert out == malformed  # untouched, rungs not wiped


def test_canonicalize_does_not_mutate_input():
    pod = {
        "rungs": [{"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]}],
        "roles": {"fast": "fast-default"},
    }
    snapshot = json.loads(json.dumps(pod))
    pb.canonicalize_catalog_rung_ids(pod)
    assert pod == snapshot  # caller's dict untouched
