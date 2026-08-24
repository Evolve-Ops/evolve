"""Tests for the producer-severity default map.

Pins two contracts:

  1. ``default_severity`` returns the mapped value for known producers
     and the global default for unknown ones. The map placement is the
     policy decision the team is making — these tests are guard-rails,
     not validation of WHICH tier is "right" for each producer.

  2. ``signals.store.observe`` resolves ``severity=None`` via the map,
     and explicit ``severity=`` still overrides. This is the integration
     contract that lets producers concentrate severity policy in one
     file instead of sprinkling it across observe() calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from signals.producer_severity import (  # noqa: E402
    DEFAULT_SEVERITY,
    PRODUCER_SEVERITY,
    default_severity,
)
from signals.store import observe, find_active_by_signature  # noqa: E402


# ── Map placement ──────────────────────────────────────────────────────────


def test_security_warden_is_alert():
    """Phase 4 policy: multi-user exec=full + no admin is a real privilege
    issue — alert tier, not warn. This test pins the placement so a
    casual edit doesn't silently downgrade the severity."""
    assert default_severity("security_warden") == "alert"


def test_heal_and_sysadmin_watchdog_are_alert():
    """Gateway down / restart-failed loops are user-affecting."""
    assert default_severity("heal") == "alert"
    assert default_severity("sysadmin_watchdog") == "alert"


def test_audit_and_cost_watchdog_are_warn():
    """Default tier for the steady-state monitors. Producers escalate
    per-emit when needed (audit critical findings, cost ≥ 2× threshold)."""
    assert default_severity("audit") == "warn"
    assert default_severity("cost_watchdog") == "warn"
    assert default_severity("embedding_monitor") == "warn"


def test_proposal_synthesizer_is_info():
    """The actionable layer is the proposals themselves; the signals are
    advisory."""
    assert default_severity("proposal_synthesizer") == "info"


def test_unknown_producer_falls_through_to_default():
    assert default_severity("brand-new-producer") == DEFAULT_SEVERITY


def test_default_is_warn():
    """The fall-through default — biases toward visible. New producers
    that forget to register show up in the alerts page rather than going
    silently to info."""
    assert DEFAULT_SEVERITY == "warn"


def test_map_only_uses_valid_severities():
    """Every entry must be one of the three values the signal schema
    allows. Guard-rail against typos like 'critical' (we use 'alert')."""
    for producer, sev in PRODUCER_SEVERITY.items():
        assert sev in ("info", "warn", "alert"), (
            f"PRODUCER_SEVERITY[{producer!r}] = {sev!r} is not a valid Severity"
        )


# ── observe() integration ──────────────────────────────────────────────────


def _observe_args(**over):
    base = dict(
        signature="security_warden:exec_full:team_bot_a",
        producer="security_warden",
        type="exec_full",
        flavor="maintenance",
        scope="bot",
        bot_id="team_bot_a",
        title="team_bot_a: exec=full on multi-user bot",
    )
    base.update(over)
    return base


def test_observe_without_severity_resolves_from_map(tmp_path):
    """severity=None → falls back to PRODUCER_SEVERITY[producer]."""
    sig = observe(tmp_path, **_observe_args())
    assert sig.severity == "alert"  # security_warden default


def test_observe_with_explicit_severity_overrides_map(tmp_path):
    """Producers can still pass severity= to escalate/de-escalate."""
    sig = observe(tmp_path, **_observe_args(severity="warn"))
    assert sig.severity == "warn"


def test_observe_unknown_producer_uses_default(tmp_path):
    sig = observe(
        tmp_path,
        signature="brand_new:type:scope",
        producer="brand_new",
        type="something",
        flavor="maintenance",
        scope="pod",
    )
    assert sig.severity == DEFAULT_SEVERITY


def test_observe_severity_resolution_is_per_call(tmp_path):
    """Resolution happens at each observe() call; the second observation
    of the same signature re-applies the producer's current default
    unless an explicit severity is passed."""
    args = _observe_args()
    s1 = observe(tmp_path, **args)
    assert s1.severity == "alert"
    s2 = observe(tmp_path, **args)
    assert s2.id == s1.id  # same signature → same signal
    assert s2.severity == "alert"  # still the map default


# ── Recalibration placements (the demotions are the noise-reduction lever) ──


def test_advisory_producers_default_to_info():
    """These producers emit advisory content that's mostly noise on the
    Alerts page if defaulted to warn. Demoting to info hides them from
    the default view but keeps them queryable via the Show Info toggle."""
    for producer in (
        "backup_audit_signal",
        "local_backup_signal",
        "manifest_reflex_runner",
        "manifest_reflex_scanner",
        "code_quality_monitor",
        "monitor_coverage",
        "cascade_audit",
        "openclaw_posture",
        "bot_recovery_monitor",
        "proposal_synthesizer",
        "upstream_issues_watcher",
    ):
        assert default_severity(producer) == "info", (
            f"{producer} should default to info (advisory) but is "
            f"{default_severity(producer)}"
        )


def test_session_economics_defaults_to_info_2026_06_04():
    """session_economics emits observations that feed cache_ttl_tuner
    (the action layer). The 2026-06-04 quality-control pass demoted the
    producer from warn → info after the operator surfaced the framing
    "either there's a proposed action or there's nothing concrete to do
    and you are wasting time and attention." Cache_*_elevated /
    cache_hit_rate_low are pure telemetry; the actionable surface is
    the cache_ttl_tuner Proposal on the Proposals page, not the raw
    Signal on the Alerts page. Pin the demotion so a regression-prone
    revert ("but cache invalidation costs money!") trips this test."""
    assert default_severity("session_economics") == "info", (
        "session_economics is observation-only — the operator-actionable "
        "surface is the cache_ttl_tuner Proposal that consumes it. "
        "Re-promoting to warn re-introduces the noise the 2026-06-04 "
        "review caught: snoozed observation signals crowding the Alerts "
        "page with no inline action affordance."
    )


def test_on_fire_producers_default_to_alert():
    """Producers whose emit-rate baseline IS the bad case (recurrent
    auto-trip, host resource exhaustion). They shouldn't ever land at
    warn — that would mix them in with steady-state monitors."""
    for producer in (
        "security_warden",
        "heal",
        "sysadmin_watchdog",
        "openclaw_config_validator",
        "breakers_runner",
        "host_health",
    ):
        assert default_severity(producer) == "alert", (
            f"{producer} should default to alert but is "
            f"{default_severity(producer)}"
        )
