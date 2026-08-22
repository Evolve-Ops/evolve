"""pod_baseline schema: round-trip, validation, classification precedence, store.

Spec: docs/spec-pod-plane-2026-08-15.md §B1.
"""
import json

import pytest

from pod_baseline.schema import (
    POD_BASELINE_SCHEMA_VERSION,
    SURFACES,
    STATE_CONFORM,
    STATE_DRIFT,
    STATE_EXCEPTION,
    STATE_UNREADABLE,
    BaselineException,
    PodBaseline,
    classify_surface,
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
    assert classify_surface("full", "full", None) == (STATE_CONFORM, "full")
    assert classify_surface("deny", "full", None) == (STATE_DRIFT, "full")


def test_classify_exception_match():
    exc = _exception(value="deny")
    assert classify_surface("deny", "full", exc) == (STATE_EXCEPTION, "deny")


def test_classify_exception_mismatch_is_drift_even_when_matching_baseline():
    # Precedence: a declared exception REPLACES the baseline as the bot's
    # desired value. Matching the pod baseline under a live exception means
    # the exception is stale — that is DRIFT, never silent conformance.
    exc = _exception(value="deny")
    assert classify_surface("full", "full", exc) == (STATE_DRIFT, "deny")
    assert classify_surface("allowlist", "full", exc) == (STATE_DRIFT, "deny")


def test_classify_unreadable_never_conforms_or_drifts():
    assert classify_surface(None, "full", None) == (STATE_UNREADABLE, "full")
    exc = _exception(value="deny")
    assert classify_surface(None, "full", exc) == (STATE_UNREADABLE, "deny")


def test_unset_is_a_real_comparable_value():
    assert classify_surface("unset", "unset", None) == (STATE_CONFORM, "unset")
    assert classify_surface("unset", "full", None) == (STATE_DRIFT, "full")


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
