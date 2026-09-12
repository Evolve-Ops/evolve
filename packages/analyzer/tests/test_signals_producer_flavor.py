"""Tests for the producer-flavor default map (2026-06-12 reports pass).

The 2026-06-12 live review of the mini found 197/203 firing Signals framed
as ``maintenance`` ("something is broken / a task in the inbox") even for
purely informational items — a new-model-available discovery, a
``security_warden`` note literally titled "(informational)". ``maintenance``
is supposed to be reserved for genuine breakage; advisory / telemetry /
discovery observations belong in the ``activity`` triage lane.

Pins three contracts:

  1. ``default_flavor`` returns ``activity`` for advisory producers and the
     global default (``maintenance``) for breakage-shaped / unknown ones.
  2. ``signals.store.observe`` resolves ``flavor=None`` via the map, and an
     explicit ``flavor=`` still overrides (the per-emit divergence path).
  3. Guard: every info-severity producer also defaults to ``activity`` flavor
     (the info ⟹ activity correlation) so a future advisory producer can't
     silently default into the maintenance task inbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from signals.producer_severity import (  # noqa: E402
    DEFAULT_FLAVOR,
    PRODUCER_FLAVOR,
    PRODUCER_SEVERITY,
    default_flavor,
)
from signals.store import observe  # noqa: E402


# ── Map placement ──────────────────────────────────────────────────────────


def test_model_discovery_defaults_to_activity():
    """A "new model available" discovery is advisory — nothing is broken,
    the model just isn't in a rung yet. It belongs in the activity lane,
    NOT the maintenance task inbox. This was the headline miscalibration in
    the 2026-06-12 review."""
    assert default_flavor("model_discovery") == "activity"


def test_advisory_producers_default_to_activity():
    """Telemetry / discovery / "nothing is broken" producers default to the
    advisory activity lane. Mirrors test_advisory_producers_default_to_info
    in test_signals_producer_severity — the same producers that are
    info-severity are also activity-flavor."""
    for producer in (
        "model_discovery",
        "value_baseline",
        "session_economics",
        "monitor_coverage",
        "cascade_audit",
        "proposal_synthesizer",
        "upstream_issues_watcher",
        "bot_recovery_monitor",
        "backup_audit_signal",
        "local_backup_signal",
        "manifest_reflex_runner",
        "manifest_reflex_scanner",
        "code_quality_monitor",
        "openclaw_posture",
    ):
        assert default_flavor(producer) == "activity", (
            f"{producer} should default to activity (advisory) but is "
            f"{default_flavor(producer)}"
        )


def test_breakage_producers_default_to_maintenance():
    """Producers whose normal output IS a fix-it task stay in the
    maintenance inbox — reserve maintenance for genuine breakage, but don't
    overcorrect and drain the inbox of real work."""
    for producer in (
        "heal",
        "audit",
        "host_health",
        "security_warden",  # most warden checks are real posture problems
        "openclaw_config_validator",
        "app_structural_verifier",
    ):
        assert default_flavor(producer) == "maintenance", (
            f"{producer} should default to maintenance (breakage) but is "
            f"{default_flavor(producer)}"
        )


def test_unknown_producer_falls_through_to_default():
    assert default_flavor("brand-new-producer") == DEFAULT_FLAVOR


def test_default_is_maintenance():
    """The fall-through default is the conservative loud/task-inbox value —
    a new breakage producer that forgets to register lands in maintenance
    (visible work), and a new advisory producer is caught by the
    info ⟹ activity guard below."""
    assert DEFAULT_FLAVOR == "maintenance"


def test_map_only_uses_valid_flavors():
    """Every entry must be one of the two values the signal schema allows."""
    for producer, fl in PRODUCER_FLAVOR.items():
        assert fl in ("activity", "maintenance"), (
            f"PRODUCER_FLAVOR[{producer!r}] = {fl!r} is not a valid Flavor"
        )


# ── Guard: info-severity ⟹ activity flavor ─────────────────────────────────


# Exemptions: info-severity producers that are nonetheless a queued task
# rather than pure FYI. Empty today — every info-severity producer is
# advisory. A future addition must justify itself by landing here (with a
# comment) instead of silently slipping a task into the activity lane.
_INFO_BUT_MAINTENANCE_EXEMPT: frozenset[str] = frozenset()


def test_default_flavor_matches_info_severity():
    """If a producer's default severity is ``info`` it should also default to
    the ``activity`` flavor — an info-severity signal is advisory by
    construction, so framing it as a maintenance task is the exact
    miscalibration the 2026-06-12 review flagged. This guard forces the
    flavor decision to be visible whenever an info producer is added."""
    for producer, sev in PRODUCER_SEVERITY.items():
        if sev != "info" or producer in _INFO_BUT_MAINTENANCE_EXEMPT:
            continue
        assert default_flavor(producer) == "activity", (
            f"{producer} is info-severity but defaults to "
            f"{default_flavor(producer)!r} flavor — info signals are "
            f"advisory, so they belong in the activity lane. Either add it "
            f"to PRODUCER_FLAVOR as 'activity' or, if it really is a queued "
            f"task, list it in _INFO_BUT_MAINTENANCE_EXEMPT with a reason."
        )


# ── observe() integration ──────────────────────────────────────────────────


def _observe_args(**over):
    base = dict(
        signature="model_discovery:new_model:anthropic/claude-x",
        producer="model_discovery",
        type="model_discovery_new",
        scope="pod",
        title="New model available: anthropic/claude-x",
    )
    base.update(over)
    return base


def test_observe_without_flavor_resolves_from_map(tmp_path):
    """flavor=None → falls back to PRODUCER_FLAVOR[producer]. This is the
    path a model_discovery *discovery* signal takes now that its spec omits
    the flavor key."""
    sig = observe(tmp_path, **_observe_args())
    assert sig.flavor == "activity"  # model_discovery default


def test_observe_with_explicit_flavor_overrides_map(tmp_path):
    """Producers still pass flavor= for a divergent signal — e.g.
    model_discovery's degraded-listing signal overrides to maintenance."""
    sig = observe(
        tmp_path,
        **_observe_args(
            signature="model_discovery:degraded",
            flavor="maintenance",
        ),
    )
    assert sig.flavor == "maintenance"


def test_observe_unknown_producer_uses_default_flavor(tmp_path):
    sig = observe(
        tmp_path,
        signature="brand_new:type:scope",
        producer="brand_new",
        type="something",
        scope="pod",
    )
    assert sig.flavor == DEFAULT_FLAVOR  # maintenance


def test_observe_severity_and_flavor_resolve_independently(tmp_path):
    """A producer can inherit one default and override the other —
    model_discovery's discovery signal passes severity=info explicitly while
    inheriting flavor=activity from the map."""
    sig = observe(tmp_path, **_observe_args(severity="info"))
    assert sig.severity == "info"
    assert sig.flavor == "activity"
