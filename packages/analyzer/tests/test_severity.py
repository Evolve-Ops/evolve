"""tests/test_severity.py — severity framework module tests.

Spec: docs/spec-severity-framework-2026-05-18.md
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import severity as sev  # noqa: E402


# ── Anchor table integrity ───────────────────────────────────────────────────


def test_anchors_present_for_every_vector_magnitude():
    """Every (vector, 0..4) cell must have a non-empty anchor string."""
    for vector in sev.ALL_VECTORS:
        for magnitude in range(5):
            text = sev.anchor_text(vector, magnitude)
            assert isinstance(text, str) and text.strip(), (
                f"missing anchor for ({vector}, {magnitude})"
            )


def test_anchor_text_returns_empty_for_out_of_range():
    assert sev.anchor_text("security", -1) == ""
    assert sev.anchor_text("security", 5) == ""


# ── clamp_magnitude / infer_magnitude ────────────────────────────────────────


def test_clamp_magnitude_bounds_to_zero_four():
    assert sev.clamp_magnitude(-3) == 0
    assert sev.clamp_magnitude(0) == 0
    assert sev.clamp_magnitude(2) == 2
    assert sev.clamp_magnitude(4) == 4
    assert sev.clamp_magnitude(99) == 4


def test_infer_magnitude_from_legacy_severity():
    assert sev.infer_magnitude("info") == 1
    assert sev.infer_magnitude("warn") == 2
    assert sev.infer_magnitude("alert") == 4


def test_infer_magnitude_case_insensitive():
    assert sev.infer_magnitude("WARN") == 2
    assert sev.infer_magnitude("Alert") == 4


def test_infer_magnitude_fallback_for_missing_or_unknown():
    """Unknown / None defaults to 2 (matches Signal store default)."""
    assert sev.infer_magnitude(None) == 2
    assert sev.infer_magnitude("") == 2
    assert sev.infer_magnitude("critical") == 2  # not in legacy enum


# ── default_vector_for_producer ──────────────────────────────────────────────


def test_default_vector_for_known_producers():
    assert sev.default_vector_for_producer("security_warden") == "security"
    assert sev.default_vector_for_producer("audit") == "security"
    assert sev.default_vector_for_producer("cost_watchdog") == "cost"
    assert sev.default_vector_for_producer("sysadmin_watchdog") == "operations"
    assert sev.default_vector_for_producer("alerts_loop_monitor") == "operations"


def test_default_vector_falls_back_to_operations_for_unknown():
    assert sev.default_vector_for_producer("brand_new_producer") == "operations"
    assert sev.default_vector_for_producer("") == "operations"


# ── resolve_severity ─────────────────────────────────────────────────────────


def test_resolve_explicit_vector_and_magnitude_wins():
    """Producer tag in details takes precedence over inference."""
    sig = {
        "producer": "cost_watchdog",   # default vector would be 'cost'
        "severity": "warn",            # would infer magnitude 2
        "details": {"vector": "security", "magnitude": 3},
    }
    rating = sev.resolve_severity(sig)
    assert rating.vector == "security"
    assert rating.magnitude == 3


def test_resolve_falls_back_to_producer_default_vector():
    sig = {
        "producer": "audit",
        "severity": "alert",
        "details": {},
    }
    rating = sev.resolve_severity(sig)
    assert rating.vector == "security"   # default for `audit`
    assert rating.magnitude == 4          # inferred from `alert`


def test_resolve_falls_back_to_inferred_magnitude_from_legacy():
    sig = {
        "producer": "cost_watchdog",
        "severity": "warn",
        "details": {},
    }
    rating = sev.resolve_severity(sig)
    assert rating.vector == "cost"
    assert rating.magnitude == 2


def test_resolve_invalid_explicit_vector_falls_back_to_default():
    sig = {
        "producer": "audit",
        "severity": "warn",
        "details": {"vector": "made_up", "magnitude": 3},
    }
    rating = sev.resolve_severity(sig)
    # Made-up vector ignored → producer default kicks in
    assert rating.vector == "security"
    # Magnitude still respected
    assert rating.magnitude == 3


def test_resolve_clamps_out_of_range_explicit_magnitude():
    sig = {
        "producer": "audit",
        "severity": "warn",
        "details": {"vector": "security", "magnitude": 99},
    }
    rating = sev.resolve_severity(sig)
    assert rating.magnitude == 4


def test_resolve_accepts_float_magnitude_and_rounds():
    sig = {
        "producer": "audit",
        "severity": "warn",
        "details": {"vector": "security", "magnitude": 2.6},
    }
    rating = sev.resolve_severity(sig)
    assert rating.magnitude == 3


def test_resolve_picks_up_optional_note():
    sig = {
        "producer": "audit",
        "severity": "warn",
        "details": {
            "vector": "security",
            "magnitude": 3,
            "severity_note": "Promoted from default because token scope is broad.",
        },
    }
    rating = sev.resolve_severity(sig)
    assert "broad" in rating.note


def test_resolve_handles_missing_details_gracefully():
    sig = {"producer": "audit", "severity": "warn"}
    rating = sev.resolve_severity(sig)
    assert rating.vector == "security"
    assert rating.magnitude == 2


def test_resolve_handles_signal_dataclass_shape():
    """Resolver also accepts an object with attribute access, mirroring
    the Signal dataclass."""

    class FakeSignal:
        producer = "audit"
        severity = "warn"
        details = {"vector": "security", "magnitude": 3}

    rating = sev.resolve_severity(FakeSignal())
    assert rating.vector == "security"
    assert rating.magnitude == 3


# ── resolve_pod_weights ──────────────────────────────────────────────────────


def test_resolve_pod_weights_defaults_when_missing():
    out = sev.resolve_pod_weights({})
    for v in sev.ALL_VECTORS:
        assert out[v] == 1.0


def test_resolve_pod_weights_reads_network_overrides():
    net = {"severity_weights": {"security": 1.5, "cost": 0.8}}
    out = sev.resolve_pod_weights(net)
    assert out["security"] == 1.5
    assert out["cost"] == 0.8
    assert out["operations"] == 1.0
    assert out["quality"] == 1.0


def test_resolve_pod_weights_ignores_invalid_values():
    net = {
        "severity_weights": {
            "security": "not a number",
            "cost": -1.0,        # non-positive
            "operations": True,  # bool — explicitly excluded
            "quality": 0,        # zero — excluded
        }
    }
    out = sev.resolve_pod_weights(net)
    for v in sev.ALL_VECTORS:
        assert out[v] == 1.0  # all defaults


def test_resolve_pod_weights_handles_non_dict_input():
    assert sev.resolve_pod_weights(None) == sev.DEFAULT_POD_WEIGHTS
    assert sev.resolve_pod_weights({"severity_weights": "garbage"}) == sev.DEFAULT_POD_WEIGHTS


# ── compose_priority + priority_bucket ───────────────────────────────────────


def test_priority_zero_when_magnitude_zero():
    rating = sev.SeverityRating(vector="security", magnitude=0)
    assert sev.compose_priority(rating) == 0.0


def test_priority_scales_with_magnitude():
    a = sev.compose_priority(sev.SeverityRating("cost", 1))
    b = sev.compose_priority(sev.SeverityRating("cost", 3))
    assert b == 3 * a


def test_priority_pod_scope_higher_than_bot():
    rating = sev.SeverityRating("operations", 3)
    pod = sev.compose_priority(rating, scope="pod")
    bot = sev.compose_priority(rating, scope="bot")
    assert pod > bot


def test_priority_active_outage_boosts_score():
    rating = sev.SeverityRating("operations", 3)
    plain = sev.compose_priority(rating, scope="bot")
    boosted = sev.compose_priority(rating, scope="bot", is_active_outage=True)
    assert boosted > plain


def test_priority_self_resolving_discounts_score():
    rating = sev.SeverityRating("operations", 3)
    plain = sev.compose_priority(rating, scope="bot")
    discounted = sev.compose_priority(rating, scope="bot", is_self_resolving=True)
    assert discounted < plain


def test_priority_pod_weight_applied():
    rating = sev.SeverityRating("security", 2)
    plain = sev.compose_priority(rating)
    boosted = sev.compose_priority(
        rating, pod_weights={"security": 1.5, "cost": 1.0, "operations": 1.0, "quality": 1.0}
    )
    assert boosted == plain * 1.5


def test_priority_max_value_in_expected_range():
    """Max magnitude × max scope × max urgency × max reasonable weight
    should land near 15.6 — well below 16. Guards against future
    multiplier changes silently inflating the score range."""
    rating = sev.SeverityRating("security", 4)
    weights = {"security": 2.0, "cost": 2.0, "operations": 2.0, "quality": 2.0}
    p = sev.compose_priority(
        rating, scope="pod", is_active_outage=True, pod_weights=weights
    )
    assert 15.0 < p < 16.0


def test_priority_bucket_thresholds():
    assert sev.priority_bucket(0.0) == "small"
    assert sev.priority_bucket(2.9) == "small"
    assert sev.priority_bucket(3.0) == "in_narrative"
    assert sev.priority_bucket(6.9) == "in_narrative"
    assert sev.priority_bucket(7.0) == "lead"
    assert sev.priority_bucket(15.6) == "lead"


# ── End-to-end shape: real-looking signals → expected bucket ────────────────


def test_gateway_down_pod_wide_leads_narrative():
    """A pod-wide gateway outage should clear the 'lead' threshold even
    without explicit producer tagging — the legacy `severity=alert` +
    operations vector + pod scope composes well past 8.0."""
    sig = {
        "producer": "sysadmin_watchdog",
        "severity": "alert",
        "scope": "pod",
        "details": {},
    }
    rating = sev.resolve_severity(sig)
    score = sev.compose_priority(rating, scope="pod", is_active_outage=True)
    assert sev.priority_bucket(score) == "lead"


def test_audit_advisory_small_bucket():
    """An info-tier audit advisory should NOT crowd the narrative."""
    sig = {
        "producer": "audit",
        "severity": "info",
        "scope": "bot",
        "details": {},
    }
    rating = sev.resolve_severity(sig)
    score = sev.compose_priority(rating, scope="bot")
    assert sev.priority_bucket(score) == "small"


def test_cost_warn_with_explicit_tag_lands_in_narrative():
    """A retrofitted producer emitting `cost: 2` on a single bot should
    sort into the in-narrative bucket."""
    sig = {
        "producer": "cost_watchdog",
        "severity": "warn",
        "scope": "bot",
        "details": {"vector": "cost", "magnitude": 2},
    }
    rating = sev.resolve_severity(sig)
    score = sev.compose_priority(rating, scope="bot")
    assert sev.priority_bucket(score) == "small"  # 2 * 1.0 * 1.0 = 2.0


def test_cost_warn_pod_wide_clears_to_narrative():
    sig = {
        "producer": "cost_watchdog",
        "severity": "warn",
        "scope": "pod",
        "details": {"vector": "cost", "magnitude": 2},
    }
    rating = sev.resolve_severity(sig)
    score = sev.compose_priority(rating, scope="pod")
    # 2 * 1.5 = 3.0 — clears the in_narrative threshold
    assert sev.priority_bucket(score) == "in_narrative"


# ── Fix-risk helpers ────────────────────────────────────────────────────────


def test_normalize_fix_risk_passes_through_known_values():
    for risk in sev.ALL_FIX_RISKS:
        assert sev.normalize_fix_risk(risk) == risk


def test_normalize_fix_risk_defaults_unknown_to_medium():
    """Conservative default — unclassified actions never auto-fire."""
    assert sev.normalize_fix_risk(None) == "medium"
    assert sev.normalize_fix_risk("") == "medium"
    assert sev.normalize_fix_risk("catastrophic") == "medium"
