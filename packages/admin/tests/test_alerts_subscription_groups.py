"""Phase 2 totality ratchet — event → Subscription grouping layer.

Extends the Phase-1 invariant (every signal → catalog_event) with:

  * every non-internal CatalogEvent maps to exactly one of the 13
    operator Subscriptions,
  * every subscription id referenced by an event exists in the registry
    (no orphans / typos),
  * the 13 registry ids are exactly the spec's operator-approved set,
  * the internal/removed events (subscription=None) are exactly the
    spec's list, and
  * the safety-critical flag is set on exactly the spec's five groups.

This is the ratchet that keeps "every operator-facing message belongs to
one of 13 Subscriptions" true as the catalog evolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.alerts import catalog as _cat  # noqa: E402
from evolve_admin.alerts import subscriptions_catalog as _subcat  # noqa: E402


# The operator-approved taxonomy (spec-subscription-completeness-2026-06-24,
# Phase 2 §"The 13"). Hard-coded here so a registry edit that drifts from the
# spec fails loud — the spec is the authority, this is the pin.
SPEC_SUBSCRIPTION_IDS = frozenset({
    "needs_decision",
    "security_findings",
    "autonomy",
    "cost_warnings",
    "cost_enforcement",
    "pod_health",
    "app_delivery",
    "bot_config",
    "updates",
    "daily_report",
    "weekly_summaries",
    "alerting_system",
    "upstream_tracking",
})

SPEC_SAFETY_CRITICAL_IDS = frozenset({
    "security_findings",
    "cost_enforcement",
    "pod_health",
    "app_delivery",
    "alerting_system",
})

# The internalized / removed events (subscription=None — hidden from the
# Configure groups, not group-gated). The original 8 (spec §"Removed/
# internalized (8)") plus meta.dev_health, added by the digest-default
# classification (D4, spec-subscription-digest-default-2026-06-28): the
# repo dev-health KPI note is a real class (DAILY_DIGEST, out of the loud
# catch-all) but isn't a pod condition the operator tunes, so it stays
# internal rather than joining one of the 13 operator groups.
SPEC_INTERNAL_EVENT_KEYS = frozenset({
    "decisions.proposal_rejected",
    "decisions.proposal_outcome_checkin",
    "decisions.briefing_activated",
    "decisions.proposal_applied",
    "meta.digest",
    "meta.send_probe",
    "meta.alert_repeat_loop",
    "meta.unclassified",
    "meta.dev_health",
})


def test_registry_ids_match_spec():
    assert _subcat.SUBSCRIPTION_IDS == SPEC_SUBSCRIPTION_IDS


def test_registry_has_exactly_thirteen():
    assert len(_subcat.SUBSCRIPTIONS) == 13
    # ids unique
    ids = [s.id for s in _subcat.SUBSCRIPTIONS]
    assert len(ids) == len(set(ids))


def test_safety_critical_set_matches_spec():
    got = {s.id for s in _subcat.SUBSCRIPTIONS if s.is_safety_critical}
    assert got == SPEC_SAFETY_CRITICAL_IDS


def test_only_upstream_tracking_defaults_off():
    off = {s.id for s in _subcat.SUBSCRIPTIONS if not s.default_enabled}
    assert off == {"upstream_tracking"}


def test_every_event_subscription_is_a_known_id_or_none():
    for e in _cat.CATALOG:
        if e.subscription is not None:
            assert e.subscription in _subcat.SUBSCRIPTION_IDS, (
                f"event {e.key} maps to unknown subscription {e.subscription!r}"
            )


def test_every_non_internal_event_maps_to_exactly_one_subscription():
    for e in _cat.subscribed_events():
        assert isinstance(e.subscription, str) and e.subscription
        # belongs to exactly one group: events_for_subscription is the
        # inverse map; the event must appear under its own id and no other.
        groups = [
            sid for sid in _subcat.SUBSCRIPTION_IDS
            if e in _cat.events_for_subscription(sid)
        ]
        assert groups == [e.subscription], (
            f"event {e.key} membership {groups} != [{e.subscription}]"
        )


def test_no_orphan_subscriptions():
    """Every registry id is referenced by at least one event — no dead
    groups that would render empty in Configure."""
    used = {e.subscription for e in _cat.subscribed_events()}
    assert used == _subcat.SUBSCRIPTION_IDS, (
        f"registry ids with no events: {_subcat.SUBSCRIPTION_IDS - used}"
    )


def test_internal_events_match_spec():
    got = {e.key for e in _cat.internal_events()}
    assert got == SPEC_INTERNAL_EVENT_KEYS


def test_subscription_for_resolves_legacy_keys():
    # A retired key resolves to its survivor's group rather than None.
    survivor = _cat.LEGACY_KEY_MIGRATION.get("summaries.daily_bot_digest")
    if survivor:  # guard in case the legacy map changes
        assert _cat.subscription_for("summaries.daily_bot_digest") == \
            _cat.subscription_for(survivor)


def test_subscription_for_unknown_key_is_none():
    assert _cat.subscription_for("not.a.real.event") is None
