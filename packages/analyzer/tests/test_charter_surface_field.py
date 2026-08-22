"""tests/test_charter_surface_field.py — pins the new ``surface`` field on
the Charter schema and the Phase 1 routing assignments.

Spec: docs/spec-recommendations-rework-2026-06-02.md §"Generator routing".

Phase 1 (this rework) ships the schema field and populates only the
worst-offender generators called out explicitly in the spec table. Other
charters stay ``surface: None`` and route via the UI's fallback heuristic
until the Phase 2 sweep closes them out.

The tests fall into three groups:
  1. Schema shape — the field exists, defaults to None, round-trips through
     to_dict / from_dict / YAML loading.
  2. Routing assignments — the 8 spec-listed charters carry the expected
     surface value.
  3. Fallback behavior — unclassified charters expose ``surface=None`` so
     the UI knows to apply its fallback heuristic.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYZER = REPO_ROOT / "packages" / "analyzer"
GENERATORS = ANALYZER / "generators"


# ─────────────────────────────────────────────────────────────────────────────
# Schema shape
# ─────────────────────────────────────────────────────────────────────────────


def test_surface_literal_declared():
    """The ``Surface`` Literal alias exists in schema.generator alongside
    Bucket / Cadence / GeneratorType. Without this, charters using
    ``surface:`` get loaded as plain strings with no type pinning."""
    import sys
    sys.path.insert(0, str(ANALYZER))
    from schema.generator import Surface  # noqa: E402

    # Literal types don't have a clean introspection API across Python
    # versions; the import succeeding + the type being usable as an
    # annotation is the contract.
    assert Surface is not None


def test_charter_has_surface_field_defaulting_to_none():
    """The Charter dataclass carries the new ``surface`` field, with a
    None default so charters that haven't been classified yet still
    construct."""
    import sys
    sys.path.insert(0, str(ANALYZER))
    from schema.generator import Charter  # noqa: E402

    c = Charter(
        id="x", type="optimizer", dimension="cost",
        purpose="x", cadence="daily",
    )
    assert hasattr(c, "surface"), "Charter must have a ``surface`` attribute"
    assert c.surface is None, (
        "Default ``surface`` must be None — charters that haven't been "
        "classified yet should not be coerced into a specific bucket"
    )


def test_charter_surface_round_trips_through_dict():
    """to_dict / from_dict must carry the ``surface`` field through so
    the registry's load-time fingerprint check + record round-trips
    don't silently drop it."""
    import sys
    sys.path.insert(0, str(ANALYZER))
    from schema.generator import Charter  # noqa: E402

    c = Charter(
        id="x", type="optimizer", dimension="cost",
        purpose="x", cadence="daily", surface="firing",
    )
    d = c.to_dict()
    assert d["surface"] == "firing", "to_dict must carry surface"
    c2 = Charter.from_dict(d)
    assert c2.surface == "firing", "from_dict must restore surface"


def test_charter_surface_yaml_round_trip():
    """A charter YAML file with ``surface:`` declared must load through
    the registry charter loader with the value preserved. This is the
    actual production path — to_dict / from_dict alone don't exercise
    the YAML reader.

    Uses ``bloat_investigator`` as the round-trip witness since its
    charter ships with ``surface: firing`` declared. (Was previously
    ``primary_model_floor_advisor``; that generator was retired
    2026-06-04 — see runner registry note.)
    """
    import sys
    sys.path.insert(0, str(ANALYZER))
    from registry.charter_loader import load_charter_from_yaml  # noqa: E402

    path = GENERATORS / "bloat_investigator" / "charter.yaml"
    charter, _fingerprint = load_charter_from_yaml(path)
    assert charter.surface == "firing", (
        f"bloat_investigator charter.yaml should load with "
        f"surface='firing'; got {charter.surface!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routing assignments — the 8 spec-listed charters
# ─────────────────────────────────────────────────────────────────────────────
#
# Map from generator_id → expected surface. Each entry is pinned by the
# spec's "Generator routing" table; changing an assignment requires
# updating both the spec doc and this test in the same PR.

EXPECTED_ROUTING = {
    # Firing — sensor-blocking misconfigs, active cost incidents,
    # top-of-list operator findings. The 2026-06-04 recalibration moved
    # bloat_investigator + cost_root_cause_correlator into this bucket
    # because their proposals name actively-burning cost root causes,
    # not slow drift the operator can defer. The 2026-06-04 retirement
    # of primary_model_floor_advisor removed its entry here (premise
    # obsoleted by the trigger anchor + Phase A defaultTier picker).
    "workspace_security": "firing",
    "security_warden": "firing",
    "bot_config_integrity": "firing",
    "budget_hawk": "firing",
    "gateway_diagnostician": "firing",
    "sysadmin_watchdog": "firing",
    "bloat_investigator": "firing",            # recalibrated 2026-06-04
    "cost_root_cause_correlator": "firing",    # recalibrated 2026-06-04
    # Drift — slow drift / install hygiene, operator should look. The
    # 2026-06-04 recalibration added cache_ttl_tuner here (was
    # improvement; cache invalidation elevated IS a drifted config) and
    # removed bloat_investigator + cost_root_cause_correlator (now
    # firing) + workspace_inventory (now cleanup).
    "cost_spike": "drift",
    "app_permission_drift": "drift",
    "auth_drift_filler": "drift",
    "evolve_watchdog": "drift",
    "exec_outcome_investigator": "drift",
    "manifest_quality": "drift",
    "session_quality": "drift",
    # test_failure_responder retired 2026-06-08 with the app-test surface
    "cache_ttl_tuner": "drift",                # recalibrated 2026-06-04
    # Cleanup — low-stakes hygiene, accept-or-ignore in batch. The
    # 2026-06-04 recalibration added workspace_inventory here — orphan
    # files/crons are the canonical cleanup case under the rubric.
    "cron_caps_filler": "cleanup",
    "app_permission_review": "cleanup",
    # test_gate_backfill retired 2026-06-08 with the app-test surface
    "workspace_inventory": "cleanup",          # recalibrated 2026-06-04
    # Improvement — product-shaped suggestions routed to the
    # Recommendations page instead of Alerts. These are "consider doing
    # X" optimizations where the current state is fine. The Phase 2 RSI
    # work (spec-rsi-proposal-eligibility-2026-06-05.md) added
    # engagement_amplifier + pod_capability_lift here — capability
    # amplification and pod-wide capability synthesis, same posture as
    # app_suggester / persona_tuner.
    "app_birth_detector": "improvement",
    "app_suggester": "improvement",
    "efficiency_hawk": "improvement",
    "persona_tuner": "improvement",
    "engagement_amplifier": "improvement",     # Phase 2 RSI 2026-06-05
    "pod_capability_lift": "improvement",      # Phase 2 RSI 2026-06-05
    # Discovery surfaces "a newer/unknown model exists; consider adopting it"
    # — an opportunity to improve the model substrate, not a structural bug.
    # Phase 4 of spec-model-rungs-and-roles-2026-06-09.
    "model_discovery": "improvement",
    # Autonomy ladder Phase B (spec-autonomy-ladder-2026-06-10 §3.2):
    # the promotion proposal is an offer to widen, not a firing problem
    # — the streak Signal itself is the Alerts-page evidence trail.
    "autonomy_promoter": "improvement",
    # AL-1.7 (2026-08-21): the promotion offer's scheduler. Same posture as
    # app_birth_detector — "this draft could be a real app" is an opportunity,
    # not a firing problem — and the Proposal is answered by the bot's own user
    # in their channel, with the pod admin seeing it in Recommendations.
    "app_promotion": "improvement",
}


def _load_charter(gen_id: str):
    import sys
    sys.path.insert(0, str(ANALYZER))
    from registry.charter_loader import load_charter_from_yaml  # noqa: E402

    path = GENERATORS / gen_id / "charter.yaml"
    if not path.exists():
        path = GENERATORS / gen_id / "charter.yml"
    charter, _ = load_charter_from_yaml(path)
    return charter


def test_phase1_routing_assignments_are_correct():
    """All 8 charters called out in the spec table carry their expected
    surface value. This is the heart of the test — if the spec drifts
    from the charters, CI catches it here."""
    mismatches = []
    for gen_id, expected in EXPECTED_ROUTING.items():
        charter = _load_charter(gen_id)
        actual = charter.surface
        if actual != expected:
            mismatches.append(f"  {gen_id}: expected {expected!r}, got {actual!r}")
    assert not mismatches, (
        "Charter surface assignments disagree with the spec routing "
        "table. Either update the charter YAML or update both the spec "
        "and this test:\n" + "\n".join(mismatches)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fallback behavior — unclassified charters expose surface=None
# ─────────────────────────────────────────────────────────────────────────────


def test_unclassified_charters_have_surface_none():
    """Charters that the spec didn't call out for Phase 1 (the other
    ~20 generators) must stay ``surface: None`` so the UI's fallback
    heuristic kicks in, rather than being silently assigned by hand."""
    classified = set(EXPECTED_ROUTING)
    all_charters = sorted(
        d for d in GENERATORS.iterdir()
        if d.is_dir() and (d / "charter.yaml").exists()
    )
    leaks = []
    for d in all_charters:
        gen_id = d.name
        if gen_id in classified:
            continue
        charter = _load_charter(gen_id)
        if charter.surface is not None:
            leaks.append(f"  {gen_id}: surface={charter.surface!r}")
    assert not leaks, (
        "Charters classified outside the Phase 1 spec table — either add "
        "them to EXPECTED_ROUTING above (and update the spec) or remove "
        "the surface from the charter YAML:\n" + "\n".join(leaks)
    )
