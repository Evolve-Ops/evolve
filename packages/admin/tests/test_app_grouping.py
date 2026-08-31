"""Tests for the same-app CLAIM (ALPHA-3a; audit B3; operator decision D-I).

``app_grouping`` is pure — no disk, no clock, no pod — so these are the
tests that pin the claim's SHAPE, and ``test_apps_pod_grouping.py`` pins
what the routes do with it.

Each clause of the claim gets a test that fails if the clause is dropped:

  * normalized name equality (and that near-names do NOT merge)
  * evidence overlap at / below / above the threshold
  * both-empty evidence merges on the name; one-empty never does
  * two ids on the SAME bot never merge, whatever they look like
  * the lead, and therefore the row's key, is deterministic

ALPHA-3b added one more: the claim is a FUNCTION (``equivalent``), and the
clustering here and promotion's cross-bot ADOPT both call it. The last test in
this file is what stops the clustering quietly growing a second copy.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_grouping  # noqa: E402
from evolve_admin.applications.app_grouping import (  # noqa: E402
    equivalent,
    EVIDENCE_SIMILARITY_THRESHOLD,
    GROUP_BASIS_NAME,
    GROUP_BASIS_NAME_AND_FILES,
    cluster_app_ids,
    evidence_signature,
    normalize_app_name,
    path_signature,
    similarity,
)


def _fact(name: str, paths, bots, spec_version: int = 1) -> dict:
    return {
        "name": name,
        "evidence": evidence_signature(paths),
        "bots": set(bots),
        "spec_version": spec_version,
    }


def _clusters(facts: dict) -> "list[list[str]]":
    return [sorted(g.members) for g in cluster_app_ids(facts)]


# ── Normalization ───────────────────────────────────────────────────────────


def test_name_normalization_ignores_case_and_separators():
    for variant in ("Morning Brief", "morning-brief", "MORNING_BRIEF",
                    "  Morning   Brief!  "):
        assert normalize_app_name(variant) == "morning brief", variant


def test_an_absent_name_normalizes_to_nothing_and_never_matches():
    # "both of these have no name" is not evidence that they are one app.
    assert normalize_app_name(None) == ""
    assert normalize_app_name("") == ""
    assert normalize_app_name("!!!") == ""
    facts = {"a": _fact("", ["x/y.py"], ["bot-a"]),
             "b": _fact("", ["x/y.py"], ["bot-b"])}
    assert _clusters(facts) == [["a"], ["b"]]


def test_path_signature_survives_three_different_roots():
    """The same file, as the three carriers on the pod actually record it."""
    for path in (
        "/Users/personal-bot/.openclaw/workspace/apps/morning-brief/run.py",
        "apps/morning-brief/run.py",
        "morning-brief/run.py",
    ):
        assert path_signature(path) == "morning-brief/run.py", path


def test_path_signature_keeps_the_folder_so_a_bare_filename_cannot_carry_a_match():
    assert path_signature("run.py") == "run.py"
    assert path_signature("a/b/c/run.py") == "c/run.py"


def test_two_empty_signatures_do_not_score_as_a_match():
    assert similarity(frozenset(), frozenset()) == 0.0
    assert similarity(frozenset({"a/b"}), frozenset()) == 0.0


# ── The claim ───────────────────────────────────────────────────────────────


def test_same_name_and_files_on_two_bots_is_one_group():
    """B3's exact case: two bots discovered it, so there are two ids."""
    facts = {
        "p-befb87e0": _fact("Morning Brief", [
            "/Users/personal-bot/.openclaw/workspace/apps/morning-brief/run.py",
            "/Users/personal-bot/.openclaw/workspace/apps/morning-brief/config.json",
        ], ["personal-bot"]),
        "p-049bf7ab": _fact("Morning Brief", [
            "apps/morning-brief/run.py",
            "apps/morning-brief/config.json",
        ], ["team-bot-a"]),
    }
    groups = cluster_app_ids(facts)
    assert len(groups) == 1
    assert sorted(groups[0].members) == ["p-049bf7ab", "p-befb87e0"]
    assert groups[0].basis == GROUP_BASIS_NAME_AND_FILES


def test_different_names_never_group_however_alike_the_files():
    facts = {
        "a": _fact("Morning Brief", ["apps/x/run.py"], ["bot-a"]),
        "b": _fact("Evening Brief", ["apps/x/run.py"], ["bot-b"]),
    }
    assert _clusters(facts) == [["a"], ["b"]]


def test_a_near_name_is_a_different_name():
    """Deliberately exact: "Weekly Report" and "Weekly Reports" are two apps."""
    facts = {
        "a": _fact("Weekly Report", ["apps/wr/run.py"], ["bot-a"]),
        "b": _fact("Weekly Reports", ["apps/wr/run.py"], ["bot-b"]),
    }
    assert _clusters(facts) == [["a"], ["b"]]


def test_same_name_with_disjoint_files_does_not_group():
    facts = {
        "a": _fact("Report", ["apps/report/alpha.py"], ["bot-a"]),
        "b": _fact("Report", ["apps/report/beta.py"], ["bot-b"]),
    }
    assert _clusters(facts) == [["a"], ["b"]]


def test_overlap_at_the_threshold_groups_and_below_it_does_not():
    """The knob is a judgement, so pin both sides of it."""
    assert EVIDENCE_SIMILARITY_THRESHOLD == 0.5
    # 2 shared of 4 union = 0.5 → grouped.
    at = {
        "a": _fact("Report", ["r/one.py", "r/two.py", "r/a.py"], ["bot-a"]),
        "b": _fact("Report", ["r/one.py", "r/two.py", "r/b.py"], ["bot-b"]),
    }
    assert _clusters(at) == [["a", "b"]]
    # 1 shared of 3 union ≈ 0.33 → not grouped.
    below = {
        "a": _fact("Report", ["r/one.py", "r/a.py"], ["bot-a"]),
        "b": _fact("Report", ["r/one.py", "r/b.py"], ["bot-b"]),
    }
    assert similarity(below["a"]["evidence"], below["b"]["evidence"]) < 0.5
    assert _clusters(below) == [["a"], ["b"]]


def test_two_apps_with_no_files_at_all_group_on_the_name():
    """A standing-instruction app has nothing on disk to compare."""
    facts = {
        "a": _fact("Daily Check-in", [], ["bot-a"]),
        "b": _fact("Daily Check-in", [], ["bot-b"]),
    }
    groups = cluster_app_ids(facts)
    assert [sorted(g.members) for g in groups] == [["a", "b"]]
    assert groups[0].basis == GROUP_BASIS_NAME


def test_one_side_with_files_and_one_without_never_groups():
    """"I have nothing to compare" is not "the evidence matches"."""
    facts = {
        "a": _fact("Daily Check-in", ["apps/dc/run.py"], ["bot-a"]),
        "b": _fact("Daily Check-in", [], ["bot-b"]),
    }
    assert _clusters(facts) == [["a"], ["b"]]


def test_two_records_on_the_same_bot_are_never_one_app_on_two_bots():
    """Merging these would list one bot twice in the bots column."""
    facts = {
        "a": _fact("Morning Brief", ["apps/mb/run.py"], ["bot-a"]),
        "b": _fact("Morning Brief", ["apps/mb/run.py"], ["bot-a"]),
    }
    assert _clusters(facts) == [["a"], ["b"]]


def test_one_bot_can_never_appear_twice_in_a_group():
    """The transitive case a pairwise guard alone gets wrong.

    ``a`` is on bot-b; ``b`` and ``c`` are both on bot-a. Every PAIR passes
    a pairwise disjoint-bots check (a-b and a-c share nothing), so a guard
    applied per pair would union all three and put bot-a in the row twice.
    """
    facts = {
        "a": _fact("Morning Brief", ["apps/mb/run.py"], ["bot-b"]),
        "b": _fact("Morning Brief", ["apps/mb/run.py"], ["bot-a"]),
        "c": _fact("Morning Brief", ["apps/mb/run.py"], ["bot-a"]),
    }
    groups = cluster_app_ids(facts)
    assert [sorted(g.members) for g in groups] == [["a", "b"], ["c"]]
    for group in groups:
        bots = [bot for m in group.members for bot in facts[m]["bots"]]
        assert len(bots) == len(set(bots)), (
            f"{group.members} puts one bot in the row twice"
        )


# ── Determinism ─────────────────────────────────────────────────────────────


def test_the_lead_is_the_id_covering_the_most_bots():
    facts = {
        "zzz": _fact("Report", ["r/x.py"], ["bot-a", "bot-b"]),
        "aaa": _fact("Report", ["r/x.py"], ["bot-c"]),
    }
    group = cluster_app_ids(facts)[0]
    assert group.lead == "zzz"
    assert group.members == ["zzz", "aaa"], "lead must come first"


def test_the_lead_tie_breaks_on_version_then_on_the_id():
    same_bots = {
        "bbb": _fact("Report", ["r/x.py"], ["bot-a"], spec_version=9),
        "aaa": _fact("Report", ["r/x.py"], ["bot-b"], spec_version=2),
    }
    assert cluster_app_ids(same_bots)[0].lead == "bbb"
    all_equal = {
        "bbb": _fact("Report", ["r/x.py"], ["bot-a"]),
        "aaa": _fact("Report", ["r/x.py"], ["bot-b"]),
    }
    assert cluster_app_ids(all_equal)[0].lead == "aaa"


def test_every_id_lands_in_exactly_one_group_including_the_lonely_ones():
    facts = {
        "solo": _fact("Solo", ["s/one.py"], ["bot-a"]),
        "pair-1": _fact("Pair", ["p/one.py"], ["bot-a"]),
        "pair-2": _fact("Pair", ["p/one.py"], ["bot-b"]),
        "nameless": _fact("", [], ["bot-c"]),
    }
    groups = cluster_app_ids(facts)
    seen = [m for g in groups for m in g.members]
    assert sorted(seen) == ["nameless", "pair-1", "pair-2", "solo"]
    assert len(seen) == len(set(seen))
    lonely = [g for g in groups if not g.grouped]
    assert all(g.basis is None for g in lonely), (
        "a group of one is a claim about nothing and must carry no basis"
    )


# ── One definition, two callers (ALPHA-3b) ──────────────────────────────────


def test_equivalent_is_the_pairwise_claim_including_the_empty_name_rule():
    same = _fact("Morning Brief", ["morning-brief/run.py"], {"a"})
    other = _fact("morning_brief", ["morning-brief/run.py"], {"b"})
    assert equivalent(same, other) == GROUP_BASIS_NAME_AND_FILES
    # No name is not a match, on either side, even against itself.
    assert equivalent(_fact("", [], {"a"}), _fact("", [], {"b"})) is None
    # One side with evidence and one without is "nothing to compare", not a match.
    assert equivalent(same, _fact("Morning Brief", [], {"b"})) is None
    # Garbage in, no claim out.
    assert equivalent(None, other) is None


def test_the_clustering_asks_the_same_function_promotion_asks(monkeypatch):
    """The single-definition invariant, from the page's side.

    ``app_promotion.cross_bot_adoption`` calls ``equivalent`` to decide a
    DURABLE id; this clustering calls it to decide a withdrawable row. If the
    clustering ever grows its own copy of the comparison the two answers can
    drift, and the one that drifts silently is the one already written to a
    manifest.

    MUTATION CHECKED: inlining the name/evidence comparison back into
    ``cluster_app_ids`` makes this go red.
    """
    seen: "list[tuple]" = []
    real = app_grouping.equivalent

    def _spy(left, right):
        seen.append((
            (left or {}).get("name"), (right or {}).get("name")
        ))
        return real(left, right)

    monkeypatch.setattr(app_grouping, "equivalent", _spy)

    facts = {
        "aaa": _fact("Morning Brief", ["morning-brief/run.py"], {"bot-a"}),
        "bbb": _fact("Morning Brief", ["morning-brief/run.py"], {"bot-b"}),
    }
    assert _clusters(facts) == [["aaa", "bbb"]]
    assert seen == [("Morning Brief", "Morning Brief")]
