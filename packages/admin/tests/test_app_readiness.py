"""AL-1.6b — the readiness score (design §7.1, brief §7).

Three things these tests are for, beyond the arithmetic:

1. **Purity.** ``app_readiness`` must import nothing that does I/O and must
   read no clock, so a score is reproducible off a dict alone. That is asserted
   against the module's own AST rather than left as a docstring promise,
   because the point of the brief's §7.3 purity rule is that it survives
   someone else's edit.

2. **The unmeasured/zero distinction.** Two of design §7.1's three dimensions
   have no producer on the pod. If a later change quietly starts scoring an
   absent producer as 0, every score drops and the only remedy on offer is a
   lower threshold — the vacuity trap the brief names. These tests pin the
   distinction so that change is loud.

3. **The top evidence rung means a SCRIPT, not a file.** On a discovered
   manifest ``files[]`` is stamped by the scanner's own workspace walk, so
   "has files" is true of nearly every manifest and discriminates nothing. The
   first revision of this module scored the top rung off exactly that and
   produced ``{100: 71, ...}`` across 79 manifests — a near-boolean it then
   blamed on the pod. ``files[].layer`` is the discriminator, it was already in
   the data, and an independent review caught the miss. The layer tests below
   are what keep it caught.
"""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_readiness as ar  # noqa: E402


def _dim(readiness: ar.Readiness, key: str) -> ar.Dimension:
    return next(d for d in readiness.dimensions if d.key == key)


def _code_file() -> dict:
    return {"files": [{"path": "run.py", "layer": "code"}]}


# ── Purity (brief §7.3) ──────────────────────────────────────────────────


def test_the_module_imports_nothing_that_touches_the_world():
    """No ``os``/``pathlib``/``json``/``subprocess``/``time``/``datetime``.

    A pure scorer is testable without a live pod AND immune to §2's outcome;
    both properties die the moment an import here can reach the filesystem.
    """
    tree = ast.parse(Path(ar.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import can reach anything in-package
                imported.add("." * node.level + (node.module or ""))
            elif node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "typing"}, imported


def test_scoring_the_same_manifest_twice_gives_the_same_answer():
    manifest = dict(_code_file(), usage_metadata={"invocation_count": 4})
    assert ar.score_readiness(manifest).to_dict() == ar.score_readiness(manifest).to_dict()


def test_the_scorer_never_reads_identity():
    """§2 is an open operator question about what carries a ``draft_id``.

    A scorer that read one would have to be rewritten whichever way §2 lands.
    """
    tree = ast.parse(Path(ar.__file__).read_text())
    # The module docstring discusses both fields by name, which is fine; what
    # must not exist is a *lookup* of either.
    looked_up = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    subscripted = {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    assert not (looked_up | subscripted) & {"draft_id", "app_id"}


# ── The top rung means a script, not a file ─────────────────────────────


def test_a_file_whose_layer_is_not_implementation_does_not_reach_the_top_rung():
    """The fix an independent review caught, and the highest-value test here.

    A manifest of markdown notes must not outrank one with a traced launchd job.
    """
    notes = ar.score_readiness({"files": [{"path": "notes.md", "layer": "reference"}]})
    traced = ar.score_readiness(
        {"scheduled_actions": [{"mechanism": "launchd", "trigger": {"kind": "launchd"}}]}
    )
    code = ar.score_readiness(_code_file())
    assert notes.score < traced.score < code.score
    assert code.score == 100


def test_the_implementation_layers_are_real_layer_values():
    """The set is a hardcoded literal — importing ``layer_classifier`` would
    break the module's purity — so nothing otherwise stops it drifting to a
    plausible-but-wrong vocabulary that simply never matches, silently
    returning the top rung to unreachable. That is not hypothetical: the first
    review of this module proposed ``{"script", "state", "data", "reference"}``
    from a different function's vocabulary entirely.

    The TEST may import freely; only the module under test must stay pure.
    """
    from evolve_admin.applications.layer_classifier import VALID_LAYERS

    assert ar.IMPLEMENTATION_LAYERS
    assert ar.IMPLEMENTATION_LAYERS <= VALID_LAYERS, (
        ar.IMPLEMENTATION_LAYERS - VALID_LAYERS
    )


def test_a_runbook_counts_as_implementation():
    """``procedure`` is what implementation looks like for an app with no .py —
    an LLM-readable runbook the heartbeat loads."""
    assert ar.score_readiness({"files": [{"path": "r.md", "layer": "procedure"}]}).score == 100


def test_a_file_entry_with_no_layer_is_not_claimed_as_implementation():
    """76 of 280 file entries on the measured pod carry no ``layer``. Unknown
    must fall DOWN a rung, never up."""
    assert ar.score_readiness({"files": [{"path": "a.py"}]}).score == 60


def test_one_implementation_file_among_many_is_enough():
    r = ar.score_readiness(
        {"files": [{"path": "n.md", "layer": "reference"}, {"path": "a.py", "layer": "code"}]}
    )
    assert r.score == 100


def test_realized_files_are_files_but_not_evidence_of_implementation():
    """They carry no ``layer`` on any manifest measured, so claiming them for
    design's "bot-authored script" rung would assert what is not there.

    NOTE the asymmetry this creates: ``layer`` is read only off ``files[]``, so
    ``realized_files`` cannot reach the top rung even if a producer started
    stamping it. That is deliberate for now — no producer does — and is the
    thing to change first if one ever does.
    """
    r = ar.score_readiness({"realized_files": [{"path": "a.py"}]})
    assert r.score == 60
    assert "none of them implementation" in r.drivers[0]


def test_the_pre_v28_evidence_dict_shape_still_counts():
    """``routes_analytics`` reads ``evidence.files`` for legacy manifests and
    renders it on the tile. Scoring 0 while the same card lists that evidence
    would be a card contradicting itself."""
    legacy = ar.score_readiness({"evidence": {"files": ["notes/x.md", "scripts/x.sh"]}})
    current = ar.score_readiness({"evidence_files": ["notes/x.md", "scripts/x.sh"]})
    assert legacy.score == current.score == 60


# ── Unmeasured is not zero ───────────────────────────────────────────────


def test_an_empty_manifest_scores_zero_rather_than_going_unscored():
    """Absence of evidence IS an observation — the scanner looked and found
    nothing — so evidence stays measurable and ``unscored`` is unreachable with
    today's dimension set. Named for what happens, because a test named for an
    unreachable state is worse than no test at all."""
    r = ar.score_readiness({})
    assert r.dimensions_measured == 1
    assert r.score == 0
    assert r.band == "weak"
    assert r.eligible_to_offer is False


def test_recurrence_is_unmeasured_when_the_counter_is_at_its_migration_default():
    r = ar.score_readiness({"usage_metadata": {"invocation_count": 0, "last_run": ""}})
    rec = _dim(r, "recurrence")
    assert rec.measured is False
    assert rec.to_dict()["score"] is None


def test_a_nonempty_last_run_does_not_by_itself_make_recurrence_measured():
    """23 of the 28 non-empty ``last_run`` values on the pod are the epoch."""
    r = ar.score_readiness(
        {"usage_metadata": {"invocation_count": 0, "last_run": "1970-01-01T00:00:00Z"}}
    )
    assert _dim(r, "recurrence").measured is False


def test_recurrence_becomes_measured_once_a_producer_writes_a_positive_count():
    r = ar.score_readiness({"usage_metadata": {"invocation_count": 5}})
    rec = _dim(r, "recurrence")
    assert rec.measured is True
    assert rec.score == 0.5
    assert r.dimensions_measured == 2


def test_a_bool_is_not_an_invocation_count():
    assert _dim(ar.score_readiness({"usage_metadata": {"invocation_count": True}}),
                "recurrence").measured is False


def test_stability_has_no_producer_today_and_is_unmeasured():
    assert _dim(ar.score_readiness({}), "stability").measured is False


def test_stability_becomes_measured_when_its_carrier_appears():
    r = ar.score_readiness({ar.STABILITY_SCANS_FIELD: 3})
    st = _dim(r, "stability")
    assert st.measured is True
    assert st.score == 1.0
    assert r.dimensions_measured == 2


def test_an_unmeasured_dimension_leaves_the_denominator_alone():
    """Evidence-only at full strength must read 100, not 50.

    Scoring an absent producer as zero would put a full-evidence app at
    ``0.5*1.0 = 50`` and drop every real app under any sane threshold.
    """
    r = ar.score_readiness(_code_file())
    assert r.dimensions_measured == 1
    assert r.score == 100


def test_widening_to_two_dimensions_reweights_rather_than_appends():
    r = ar.score_readiness(dict(_code_file(), usage_metadata={"invocation_count": 5}))
    # (0.5*1.0 + 0.3*0.5) / 0.8 = 0.8125
    assert r.score == 81
    assert r.dimensions_measured == 2


# ── Evidence tiers follow design §7.1's stated order ─────────────────────


def test_the_rungs_descend_in_design_order():
    code = ar.score_readiness(_code_file()).score
    scheduled = ar.score_readiness(
        {"scheduled_actions": [{"mechanism": "launchd", "trigger": {"kind": "launchd"}}]}
    ).score
    observed = ar.score_readiness({"evidence_files": ["x.md"]}).score
    memory = ar.score_readiness(
        {"heartbeat_evidence": {"file_path": "HEARTBEAT.md", "section_anchors": ["a"]}}
    ).score
    conversation = ar.score_readiness({"conversation_only": True}).score
    assert code > scheduled > observed > memory > conversation > 0


def test_an_unknown_mechanism_heartbeat_action_is_memory_structure_not_cron():
    """220 of the pod's 232 scheduled actions are ``mechanism: unknown`` /
    ``quality: suspect`` — prose the scanner extracted, not a traced job.
    Collapsing the two rungs would make nearly every manifest read cron-backed.
    """
    extracted = ar.score_readiness(
        {
            "scheduled_actions": [
                {"mechanism": "unknown", "quality": "suspect", "trigger": {"kind": "heartbeat"}}
            ]
        }
    )
    memory_only = ar.score_readiness({"heartbeat_evidence": {"file_path": "HEARTBEAT.md"}})
    assert extracted.score == memory_only.score


def test_conversation_only_evidence_is_read_but_weighs_least():
    """AL-1.6c's carrier. Read tolerantly now so 1.6c needs no change here."""
    flat = ar.score_readiness({"conversation_only": True})
    listed = ar.score_readiness({"evidence": [{"kind": "conversation_only"}]})
    stringy = ar.score_readiness({"evidence": ["conversation_only"]})
    assert flat.score == listed.score == stringy.score
    assert 0 < flat.score < ar.score_readiness({"heartbeat_evidence": {"file_path": "x"}}).score


def test_corroboration_never_lets_a_lower_tier_outrank_a_higher_one():
    corroborated_low = ar.score_readiness(
        {
            "evidence_files": ["x.md"],
            "heartbeat_evidence": {"file_path": "HEARTBEAT.md"},
            "conversation_only": True,
        }
    )
    assert corroborated_low.score < ar.score_readiness(_code_file()).score


# ── The offer gate (design §11's open threshold; brief §7.4) ─────────────


def test_min_dimensions_suppresses_a_perfect_single_dimension_score():
    """The load-bearing conservative choice, pinned.

    With one producer live, a 100 resting entirely on the evidence dimension is
    not grounds to ask a user to promote an app.
    """
    r = ar.score_readiness(_code_file())
    assert r.score == 100
    assert r.band == "ready"
    assert r.eligible_to_offer is False


def test_two_dimensions_over_threshold_are_eligible():
    r = ar.score_readiness(dict(_code_file(), usage_metadata={"invocation_count": 10}))
    assert r.score == 100
    assert r.eligible_to_offer is True


def test_thresholds_can_be_swept_without_monkeypatching_the_module():
    manifest = dict(_code_file(), usage_metadata={"invocation_count": 1})
    assert ar.score_readiness(manifest, offer_threshold=99).eligible_to_offer is False
    assert ar.score_readiness(manifest, offer_threshold=10).eligible_to_offer is True
    assert ar.score_readiness(manifest, min_dimensions=3).eligible_to_offer is False


def test_both_band_cuts_travel_with_the_score_not_just_one():
    """The tile colours a bar and a badge off the same numbers.

    Comparing the payload to the module constant would pass even if the field
    were hardcoded — which is the bug this guards. So sweep BOTH cuts away from
    the constants and require the payload to follow.
    """
    swept = ar.score_readiness(_code_file(), offer_threshold=25, emerging_threshold=10).to_dict()
    assert swept["offer_threshold"] == 25
    assert swept["emerging_threshold"] == 10
    assert swept["offer_threshold"] != ar.OFFER_THRESHOLD
    assert swept["emerging_threshold"] != ar.EMERGING_THRESHOLD
    assert swept["emerging_threshold"] < swept["offer_threshold"]


def test_neither_cut_sits_inside_a_rungs_reachable_range():
    """What this guards, precisely: no cut falls where an evidence-only score
    can land.

    A cut inside a rung's reachable range separates AMOUNTS of evidence, and is
    decided by whether the corroboration bonus happened to fire — which is what
    60 did (exactly ``observed_files``'s rung value) and what 70 did (that
    rung's ceiling). This does NOT by itself select 75: several other bands are
    also empty, and wider. What selects 75 is design §7.1's top-two-rungs
    boundary, which is prose and not testable here.

    Ranges are recomputed from the tier table, so this fails if a rung, the
    bonus, or either cut moves independently.
    """
    n = len(ar.EVIDENCE_TIERS)
    reachable: list[tuple[int, int]] = []
    for i, (_, value, _) in enumerate(ar.EVIDENCE_TIERS):
        lower_tiers = n - 1 - i  # only strictly-lower tiers can corroborate
        lo = int(round(100 * value))
        hi = int(round(100 * min(1.0, value + ar.CORROBORATION_BONUS * lower_tiers)))
        reachable.append((lo, hi))
    assert not any(lo <= ar.OFFER_THRESHOLD <= hi for lo, hi in reachable), reachable
    assert not any(lo <= ar.EMERGING_THRESHOLD <= hi for lo, hi in reachable), reachable


def test_bands_are_four_and_unscored_is_its_own():
    assert ar.band_for(None) == "unscored"
    assert ar.band_for(0) == "weak"
    assert ar.band_for(ar.EMERGING_THRESHOLD) == "emerging"
    assert ar.band_for(ar.OFFER_THRESHOLD) == "ready"


# ── Payload shape (what the Apps page consumes) ──────────────────────────


def test_payload_is_json_shaped_and_names_the_unmeasured_dimensions():
    p = ar.readiness_payload(_code_file())
    assert p["score"] == 100
    assert p["band"] == "ready"
    assert p["dimensions_measured"] == 1
    assert p["dimensions_total"] == 3
    assert p["eligible_to_offer"] is False
    assert p["version"] == ar.READINESS_VERSION
    unmeasured = [d["key"] for d in p["dimensions"] if not d["measured"]]
    assert set(unmeasured) == {"recurrence", "stability"}
    assert all(d["score"] is None for d in p["dimensions"] if not d["measured"])


def test_the_payload_does_not_duplicate_the_drivers_list():
    """``drivers`` is derivable from ``dimensions[].detail`` and the tile does
    not read it — 79 discovered apps ride this payload."""
    assert "drivers" not in ar.readiness_payload(_code_file())


def test_drivers_are_ordered_by_contribution_and_only_from_measured_dimensions():
    r = ar.score_readiness(dict(_code_file(), usage_metadata={"invocation_count": 1}))
    assert len(r.drivers) == 2
    assert "implementation file" in r.drivers[0]


# ── Robustness: the route wraps this in a broad ``except: pass`` ─────────


def test_a_non_mapping_input_does_not_raise():
    """A raising scorer would not error — it would silently drop the app from
    the grid entirely."""
    for junk in (None, [], "x", 3):
        assert ar.score_readiness(junk).score == 0


def test_junk_field_types_do_not_raise():
    r = ar.score_readiness(
        {
            "files": "not-a-list",
            "scheduled_actions": [None, 3, {"trigger": "not-a-dict"}],
            "usage_metadata": [],
            "heartbeat_evidence": "x",
            "evidence": {"kind": "conversation_only"},
            ar.STABILITY_SCANS_FIELD: "three",
        }
    )
    assert r.score == 0
    assert r.dimensions_measured == 1


def test_a_file_entry_that_is_not_a_dict_does_not_raise():
    assert ar.score_readiness({"files": [None, "a.py", {"layer": "code"}]}).score == 100


def test_the_scorer_does_not_mutate_the_manifest():
    manifest = dict(_code_file(), usage_metadata={"invocation_count": 3})
    before = copy.deepcopy(manifest)
    ar.score_readiness(manifest)
    assert manifest == before
