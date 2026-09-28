"""pod_baseline schema: round-trip, validation, classification precedence, store.

Spec: internal/spec-pod-plane-2026-08-15.md §B1.
"""
import json

import pytest

from pod_baseline.schema import (
    DRIFT_STATES,
    NO_INTENT_SENTINELS,
    POD_BASELINE_SCHEMA_VERSION,
    STATE_DISPLAY_ORDER,
    SURFACES,
    STATE_CONFORM,
    STATE_DIVERGENT,
    STATE_EXCEPTION,
    STATE_LOOSENED,
    STATE_TIGHTENED,
    STATE_UNDECLARED,
    STATE_UNREADABLE,
    BaselineException,
    PodBaseline,
    classify_surface,
    is_drift,
)
from pod_baseline.store import baseline_path, load_baseline, save_baseline


def _baseline(**overrides):
    surfaces = {
        "exec_policy": "full",
        "tool_profile": "coding",
        "browser": "on",
        "context_profile": "unset",
        "model_policy": "pod-defaults",
    }
    surfaces.update(overrides.pop("surfaces", {}))
    return PodBaseline(
        updated_at="2026-08-15T00:00:00+00:00",
        surfaces=surfaces,
        undeclared=overrides.pop("undeclared", []),
        exceptions=overrides.pop("exceptions", []),
    )


def _exception(bot_id="security-bot", surface="exec_policy", value="deny"):
    return BaselineException(
        bot_id=bot_id,
        surface=surface,
        value=value,
        reason="locked down by design",
        declared_at="2026-08-15T00:00:00+00:00",
    )


# ── Round-trip ────────────────────────────────────────────────────────────────

def test_round_trip_preserves_everything():
    baseline = _baseline(exceptions=[_exception()])
    again = PodBaseline.from_dict(baseline.to_dict())
    assert again == baseline
    assert again.schema_version == POD_BASELINE_SCHEMA_VERSION


def test_from_dict_tolerates_missing_optional_fields():
    baseline = PodBaseline.from_dict({"surfaces": {"exec_policy": "full"}})
    assert baseline.schema_version == POD_BASELINE_SCHEMA_VERSION
    assert baseline.updated_at == ""
    assert baseline.exceptions == []


def test_from_dict_rejects_bad_shapes():
    with pytest.raises(ValueError):
        PodBaseline.from_dict({"surfaces": ["exec_policy"]})
    with pytest.raises(ValueError):
        PodBaseline.from_dict({"surfaces": {}, "exceptions": {"not": "a list"}})


def test_from_dict_rejects_non_string_surface_values():
    # The file is operator-editable: a natural-JSON hand edit like
    # `"browser": true` must error loudly, not coerce to the string "True"
    # and census everything as nonsensical DRIFT.
    with pytest.raises(ValueError, match="browser"):
        PodBaseline.from_dict({"surfaces": {"browser": True}})


def test_from_dict_rejects_an_empty_surface_value():
    # "" IS a string, so the isinstance guard above lets it through — and it
    # matches no observed reading, silently flipping the whole fleet to
    # `divergent` against a value that is not even listed in `undeclared`.
    # That is precisely the silent coercion from_dict promises to prevent.
    for bad in ("", "   ", "\t"):
        with pytest.raises(ValueError, match="empty value"):
            PodBaseline.from_dict({"surfaces": {"exec_policy": bad}})


def test_from_dict_rejects_malformed_exception_entries():
    # A stringified exception must not silently vanish from the list.
    with pytest.raises(ValueError, match="exception"):
        PodBaseline.from_dict({
            "surfaces": {},
            "exceptions": ["security-bot exec_policy deny"],
        })


def test_from_dict_refuses_future_schema_version():
    data = _baseline().to_dict()
    data["schema_version"] = POD_BASELINE_SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="newer"):
        PodBaseline.from_dict(data)


def test_exception_round_trip_and_fields():
    exc = _exception()
    again = BaselineException.from_dict(exc.to_dict())
    assert again == exc
    # The exception object is first-class: all four descriptive fields survive.
    assert set(exc.to_dict()) == {"bot_id", "surface", "value", "reason", "declared_at"}


# ── Validation ────────────────────────────────────────────────────────────────

def test_validate_ok_on_complete_baseline():
    assert _baseline(exceptions=[_exception()]).validate() == []


def test_validate_flags_missing_and_unknown_surfaces():
    baseline = _baseline()
    del baseline.surfaces["browser"]
    baseline.surfaces["plugin_set"] = "big"  # v2 surface — not in v1
    problems = baseline.validate()
    assert any("missing surface value: browser" in p for p in problems)
    assert any("unknown surface: plugin_set" in p for p in problems)


def test_validate_flags_bad_exceptions():
    baseline = _baseline(exceptions=[
        _exception(),
        _exception(),  # duplicate (bot, surface)
        _exception(bot_id="", surface="exec_policy"),
        _exception(surface="plugin_set"),
    ])
    problems = baseline.validate()
    assert any("duplicate exception" in p for p in problems)
    assert any("empty bot_id" in p for p in problems)
    assert any("unknown surface" in p for p in problems)


def test_exception_for_lookup():
    exc = _exception()
    baseline = _baseline(exceptions=[exc])
    assert baseline.exception_for("security-bot", "exec_policy") is exc
    assert baseline.exception_for("security-bot", "browser") is None
    assert baseline.exception_for("bot-a", "exec_policy") is None


# ── Classification (exception vs drift precedence is pinned here) ────────────

def test_classify_conform_and_drift_without_exception():
    assert classify_surface("full", "full", None, surface="exec_policy") == (
        STATE_CONFORM, "full")
    assert classify_surface("deny", "full", None, surface="exec_policy") == (
        STATE_TIGHTENED, "full")


def test_classify_exception_match():
    exc = _exception(value="deny")
    assert classify_surface("deny", "full", exc, surface="exec_policy") == (
        STATE_EXCEPTION, "deny")


def test_classify_exception_mismatch_drifts_even_when_matching_baseline():
    # Precedence: a declared exception REPLACES the baseline as the bot's
    # desired value. Matching the pod baseline under a live exception means
    # the exception is stale — that is drift, never silent conformance. The
    # DIRECTION is measured against the exception, not the pod baseline.
    exc = _exception(value="deny")
    assert classify_surface("full", "full", exc, surface="exec_policy") == (
        STATE_LOOSENED, "deny")
    assert classify_surface("allowlist", "full", exc, surface="exec_policy") == (
        STATE_LOOSENED, "deny")


def test_classify_unreadable_never_conforms_or_drifts():
    assert classify_surface(None, "full", None, surface="exec_policy") == (
        STATE_UNREADABLE, "full")
    exc = _exception(value="deny")
    assert classify_surface(None, "full", exc, surface="exec_policy") == (
        STATE_UNREADABLE, "deny")


def test_unset_is_a_real_comparable_value():
    assert classify_surface("unset", "unset", None, surface="exec_policy") == (
        STATE_CONFORM, "unset")
    # ...but it sits on no safety chain, so a difference against it is
    # DIVERGENT, never a direction: "the knob is absent" means upstream's
    # default governs, and that can move under the fleet.
    assert classify_surface("unset", "full", None, surface="exec_policy") == (
        STATE_DIVERGENT, "full")


# ── Q7(a): drift gains a direction ───────────────────────────────────────────

def test_direction_split_on_an_ordered_surface():
    assert classify_surface("allowlist", "full", None, surface="exec_policy")[0] \
        == STATE_TIGHTENED
    assert classify_surface("full", "allowlist", None, surface="exec_policy")[0] \
        == STATE_LOOSENED
    assert classify_surface("off", "on", None, surface="browser")[0] == STATE_TIGHTENED
    assert classify_surface("on", "off", None, surface="browser")[0] == STATE_LOOSENED


def test_surfaces_without_a_safety_ordering_can_only_diverge():
    # context_profile is a COST axis and model_policy a binary provenance
    # flag (Q7(a)); neither carries a safe direction, so no reading on
    # either may ever classify tightened or loosened.
    for surface, observed, expected in (
        ("context_profile", "lean", "balanced"),
        ("context_profile", "balanced", "lean"),
        ("model_policy", "custom", "pod-defaults"),
        ("model_policy", "pod-defaults", "custom"),
    ):
        assert classify_surface(observed, expected, None, surface=surface)[0] \
            == STATE_DIVERGENT


def test_unordered_tool_profile_pair_is_divergent_not_a_direction():
    # coding and messaging each expose tools the other does not (see
    # pod_baseline.ordering) — neither is the tighter of the pair.
    assert classify_surface("coding", "messaging", None, surface="tool_profile")[0] \
        == STATE_DIVERGENT
    assert classify_surface("messaging", "coding", None, surface="tool_profile")[0] \
        == STATE_DIVERGENT
    # ...but both ARE comparable against minimal and full.
    assert classify_surface("minimal", "messaging", None, surface="tool_profile")[0] \
        == STATE_TIGHTENED
    assert classify_surface("full", "coding", None, surface="tool_profile")[0] \
        == STATE_LOOSENED


def test_omitted_surface_name_degrades_to_divergent_never_a_direction():
    # surface= is keyword-only and defaults to "": a caller that forgets it
    # must get "we don't know", never a fabricated direction.
    assert classify_surface("allowlist", "full", None)[0] == STATE_DIVERGENT


def test_state_display_order_covers_every_state_constant():
    """Stated as a SHAPE, derived from the module — not a second roster.

    A hand-written set here would be the same omission twice: adding
    STATE_QUARANTINED and forgetting STATE_DISPLAY_ORDER would pass. Reading
    the constants off the module is what makes this catch it.
    """
    import pod_baseline.schema as schema_mod

    declared = {
        value for name, value in vars(schema_mod).items()
        if name.startswith("STATE_") and isinstance(value, str)
    }
    assert set(STATE_DISPLAY_ORDER) == declared
    assert len(STATE_DISPLAY_ORDER) == len(set(STATE_DISPLAY_ORDER))
    assert DRIFT_STATES <= declared
    # Sanity: the derivation actually found the states, not an empty set.
    assert len(declared) == 7


def test_state_vocabulary_is_complete_and_consistent():
    assert DRIFT_STATES == {STATE_TIGHTENED, STATE_LOOSENED, STATE_DIVERGENT}
    assert all(is_drift(s) for s in DRIFT_STATES)
    assert not any(is_drift(s) for s in
                   (STATE_CONFORM, STATE_EXCEPTION, STATE_UNREADABLE, STATE_UNDECLARED))
    # "drift" is gone as a row state — it named two opposite facts.
    assert "drift" not in STATE_DISPLAY_ORDER


def test_classification_families_are_behaviour_preserving():
    """Q7(a) split drift; it must have moved NOTHING else.

    The oracle below is B1's classifier verbatim (pre-Q7). For every
    (observed, baseline, exception) triple over a value space that spans
    ordered, unordered and sentinel values, the new classifier must agree
    with it on FAMILY: conform stays conform, exception stays exception,
    unreadable stays unreadable, and every old drift is one of exactly the
    three new drift states — nothing crosses a family boundary. This test
    fails if the refactor changed any of the four original outcomes.
    """
    def b1_classify(observed, baseline_value, exception):
        expected = exception.value if exception is not None else baseline_value
        if observed is None:
            return "unreadable", expected
        if exception is not None:
            return ("exception" if observed == exception.value else "drift"), expected
        return ("conform" if observed == baseline_value else "drift"), expected

    values = [None, "deny", "allowlist", "full", "unset", "custom", "on"]
    checked = 0
    for surface in SURFACES:
        for observed in values:
            for baseline_value in values[1:]:
                for exc_value in [None] + values[1:]:
                    exc = (None if exc_value is None
                           else _exception(surface=surface, value=exc_value))
                    want_state, want_expected = b1_classify(observed, baseline_value, exc)
                    got_state, got_expected = classify_surface(
                        observed, baseline_value, exc, surface=surface,
                    )
                    assert got_expected == want_expected, (surface, observed, exc_value)
                    if want_state == "drift":
                        assert got_state in DRIFT_STATES, (surface, observed, exc_value)
                    else:
                        assert got_state == want_state, (surface, observed, exc_value)
                    checked += 1
    assert checked == len(SURFACES) * 7 * 6 * 7  # the whole cross-product ran


# ── Q7(b): undeclared surfaces ───────────────────────────────────────────────

def test_no_intent_sentinels_are_exactly_custom_and_unset():
    assert NO_INTENT_SENTINELS == {"custom", "unset"}
    # "custom-allow" is NOT one: an exclusive tools.allow list is somebody's
    # deliberate list, merely one that sits on no safety ladder.
    assert "custom-allow" not in NO_INTENT_SENTINELS


def test_undeclared_row_is_never_conform_and_never_drift():
    for observed in ("custom", "unset", "full", "coding"):
        state, expected = classify_surface(
            observed, "", None, surface="tool_profile", undeclared=True,
        )
        assert state == STATE_UNDECLARED
        assert expected == ""  # nothing was declared to compare against


def test_unreadable_beats_undeclared():
    # "we could not look" and "we looked and no intent exists" are different
    # facts; a permission wall must not hide inside a pod-level silence.
    state, expected = classify_surface(
        None, "", None, surface="tool_profile", undeclared=True,
    )
    assert state == STATE_UNREADABLE
    assert expected == ""


def test_exception_beats_undeclared():
    # An exception IS a declared intent for that bot. Reporting it as
    # "nobody declared this" would discard a real declaration.
    exc = _exception(bot_id="security-bot", surface="exec_policy", value="deny")
    assert classify_surface("deny", "", exc, surface="exec_policy", undeclared=True) \
        == (STATE_EXCEPTION, "deny")
    assert classify_surface("full", "", exc, surface="exec_policy", undeclared=True) \
        == (STATE_LOOSENED, "deny")


def test_baseline_tracks_undeclared_surfaces():
    baseline = _baseline(undeclared=["context_profile"])
    del baseline.surfaces["context_profile"]
    assert baseline.validate() == []
    assert baseline.is_undeclared("context_profile") is True
    assert baseline.is_undeclared("exec_policy") is False


def test_validate_rejects_surface_both_declared_and_undeclared():
    baseline = _baseline(undeclared=["exec_policy"])
    assert any("both declared and undeclared" in p for p in baseline.validate())


def test_validate_rejects_surface_in_neither_list():
    baseline = _baseline()
    del baseline.surfaces["browser"]
    assert any("missing surface value: browser" in p for p in baseline.validate())


def test_validate_flags_unknown_and_duplicate_undeclared_entries():
    baseline = _baseline(undeclared=["plugin_set", "plugin_set"])
    problems = baseline.validate()
    assert any("unknown undeclared surface: plugin_set" in p for p in problems)
    assert any("duplicate undeclared surface: plugin_set" in p for p in problems)


def test_undeclared_round_trips_and_keeps_canonical_order():
    baseline = _baseline()
    for surface in ("model_policy", "context_profile", "browser"):
        del baseline.surfaces[surface]
    baseline.undeclared = ["model_policy", "browser", "context_profile"]
    data = baseline.to_dict()
    # SURFACES order, not insertion or sort order — stable diffs across re-seeds.
    assert data["undeclared"] == ["browser", "context_profile", "model_policy"]
    assert PodBaseline.from_dict(data).undeclared == data["undeclared"]


def test_to_dict_does_not_launder_a_duplicate_validate_would_name():
    # A save/load round-trip must not silently repair a hand-edit that
    # validate() exists to report.
    baseline = _baseline(undeclared=["browser", "browser"])
    del baseline.surfaces["browser"]
    assert any("duplicate undeclared surface" in p for p in baseline.validate())
    again = PodBaseline.from_dict(baseline.to_dict())
    assert again.undeclared == ["browser", "browser"]
    assert any("duplicate undeclared surface" in p for p in again.validate())


def test_from_dict_rejects_bad_undeclared_shapes():
    with pytest.raises(ValueError, match="undeclared"):
        PodBaseline.from_dict({"surfaces": {}, "undeclared": "exec_policy"})
    with pytest.raises(ValueError, match="undeclared"):
        PodBaseline.from_dict({"surfaces": {}, "undeclared": [{"s": "exec_policy"}]})


def test_pre_q7_baseline_file_still_loads_and_validates():
    # Both live pods hold a pre-Q7 file: all five surfaces declared, no
    # "undeclared" key. It must keep working untouched — no migration.
    data = {
        "schema_version": POD_BASELINE_SCHEMA_VERSION,
        "updated_at": "2026-08-17T07:51:00+00:00",
        "surfaces": {
            "exec_policy": "full", "tool_profile": "unset", "browser": "unset",
            "context_profile": "custom", "model_policy": "custom",
        },
        "exceptions": [],
    }
    baseline = PodBaseline.from_dict(data)
    assert baseline.undeclared == []
    assert baseline.validate() == []
    assert not any(baseline.is_undeclared(s) for s in SURFACES)


def test_schema_version_is_not_bumped_by_q7():
    # Deliberate (Q7(b)): NOT bumping is what keeps a pre-Q7 reader able to
    # open a post-Q7 file at all — from_dict hard-refuses a newer version.
    # That reader then complains "missing surface value: X" per undeclared
    # surface, which is the accepted trade: a legible complaint beats a
    # refusal. Bumping this constant breaks that, so it is pinned here.
    assert POD_BASELINE_SCHEMA_VERSION == 1


# ── Store ─────────────────────────────────────────────────────────────────────

def test_store_round_trip(tmp_path):
    baseline = _baseline(exceptions=[_exception()])
    out = save_baseline(tmp_path, baseline)
    assert out == baseline_path(tmp_path)
    assert load_baseline(tmp_path) == baseline
    # evolve-owned but cross-user readable; schema_version present on disk.
    assert (out.stat().st_mode & 0o777) == 0o644
    raw = json.loads(out.read_text())
    assert raw["schema_version"] == POD_BASELINE_SCHEMA_VERSION


def test_store_absent_returns_none(tmp_path):
    assert load_baseline(tmp_path) is None


def test_store_corrupt_raises(tmp_path):
    baseline_path(tmp_path).write_text("{not json")
    with pytest.raises(ValueError):
        load_baseline(tmp_path)
    baseline_path(tmp_path).write_text('["top level is a list"]')
    with pytest.raises(ValueError):
        load_baseline(tmp_path)


def test_surfaces_constant_is_the_decided_v1_set():
    # Q2 (decided 2026-08-15): exactly five knobs; plugin/skill sets are v2.
    assert SURFACES == (
        "exec_policy", "tool_profile", "browser", "context_profile", "model_policy",
    )
