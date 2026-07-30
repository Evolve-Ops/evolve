"""Schema tests for the Phase 4 PR-1 additions to Signal.

Pins three contracts:

  1. Signals roundtrip through to_dict/from_dict with the new optional
     fields (remediation, incident_key, caused_by_signal_id) preserved.

  2. Old-format Signal JSON (written before PR-1) deserializes cleanly
     with the new fields defaulting to None. This is the back-compat
     contract for any existing signal already on disk in {shared_dir}/
     signals/firing|snoozed|archived/ — they pre-date these fields and
     must not break when read after the schema bump.

  3. Remediation itself roundtrips through its own to_dict/from_dict
     including the params blob.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from schema.signal import (  # noqa: E402
    Remediation,
    Signal,
    make_signature,
    new_signal_id,
)


def _base_signal(**overrides) -> Signal:
    defaults = dict(
        id=new_signal_id(),
        signature=make_signature("audit", "drift", "security_bot"),
        producer="audit",
        type="drift",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="security_bot",
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ── Remediation roundtrip ────────────────────────────────────────────────────


def test_remediation_roundtrips_through_dict():
    r = Remediation(
        kind="reset_baseline",
        params={"bot_id": "security_bot", "kind": "scripts"},
        label="Reset baseline",
        confirm="Reseeds security_bot's scripts baseline from observed state.",
    )
    r2 = Remediation.from_dict(r.to_dict())
    assert r2.kind == r.kind
    assert r2.params == r.params
    assert r2.label == r.label
    assert r2.confirm == r.confirm


def test_remediation_default_label_and_confirm_are_empty():
    """Producers may attach a remediation with just kind + params."""
    r = Remediation(kind="install_infra_jobs")
    assert r.label == ""
    assert r.confirm == ""
    assert r.params == {}


# ── Signal with new fields roundtrips ───────────────────────────────────────


def test_signal_with_remediation_roundtrips():
    rem = Remediation(
        kind="reset_baseline",
        params={"bot_id": "security_bot", "kind": "scripts"},
        label="Reset baseline",
        confirm="Reseeds.",
    )
    sig = _base_signal(remediation=rem)
    sig2 = Signal.from_dict(sig.to_dict())
    assert sig2.remediation is not None
    assert sig2.remediation.kind == "reset_baseline"
    assert sig2.remediation.params == {"bot_id": "security_bot", "kind": "scripts"}


def test_signal_with_incident_key_roundtrips():
    sig = _base_signal(incident_key="security_bot-embedding-storm")
    sig2 = Signal.from_dict(sig.to_dict())
    assert sig2.incident_key == "security_bot-embedding-storm"


def test_signal_with_caused_by_roundtrips():
    sig = _base_signal(caused_by_signal_id="abc-123")
    sig2 = Signal.from_dict(sig.to_dict())
    assert sig2.caused_by_signal_id == "abc-123"


def test_signal_without_new_fields_serializes_them_as_none():
    """to_dict surfaces the new fields even when None — so the wire format
    is consistent and the UI doesn't have to handle absence vs. None."""
    sig = _base_signal()
    d = sig.to_dict()
    assert d["remediation"] is None
    assert d["incident_key"] is None
    assert d["caused_by_signal_id"] is None


# ── Back-compat: old JSON deserializes ──────────────────────────────────────


def test_old_format_signal_json_deserializes():
    """A Signal written before PR-1 has no remediation/incident_key/
    caused_by_signal_id keys. from_dict must accept it and leave the new
    fields as None.
    """
    old_json = {
        "id": "old-id",
        "schema_version": 1,
        "signature": "audit:drift:security_bot",
        "producer": "audit",
        "type": "drift",
        "flavor": "maintenance",
        "severity": "warn",
        "scope": "bot",
        "bot_id": "security_bot",
        "title": "drift",
        "body": "",
        "details": {},
        "state": "firing",
        "created_at": "2026-05-01T00:00:00+00:00",
        "last_observed_at": "2026-05-01T00:00:00+00:00",
        "observation_count": 1,
        "state_history": [],
        "motivated_proposals": [],
        "deliveries": [],
        "config_hint": None,
        # no remediation / incident_key / caused_by_signal_id
    }
    sig = Signal.from_dict(old_json)
    assert sig.remediation is None
    assert sig.incident_key is None
    assert sig.caused_by_signal_id is None


# ── Category field (2026-06-03 Alerts review) ──────────────────────────────


def test_signal_category_defaults_from_producer_when_omitted():
    """Producers don't have to pass category — Signal.__post_init__ derives
    it from the per-producer routing map so freshly emitted signals land
    in the right tab without every observe() call threading the arg
    through. Backward compat shim relied on by all 33 existing producers."""
    sig = _base_signal()  # producer=audit
    assert sig.category == "platform"

    cost = _base_signal(producer="cost_watchdog", signature=make_signature(
        "cost_watchdog", "spike", "team_bot_b"))
    assert cost.category == "cost"

    sec = _base_signal(producer="security_warden", signature=make_signature(
        "security_warden", "credential", "team_bot_c"))
    assert sec.category == "security"

    backup = _base_signal(producer="backup_signal", signature=make_signature(
        "backup_signal", "missing_pat", "team_bot_b"))
    assert backup.category == "backup"

    integ = _base_signal(producer="integration_probe", signature=make_signature(
        "integration_probe", "oauth_failed", "team_bot_c"))
    assert integ.category == "integrations"


def test_signal_unknown_producer_defaults_to_platform_category():
    """A freshly-added producer that nobody has mapped yet still lands
    somewhere — 'platform' is the safe catch-all so the operator sees
    the signal instead of it disappearing from the categorized view."""
    sig = _base_signal(producer="brand_new_unknown_producer",
                       signature=make_signature(
                           "brand_new_unknown_producer", "weird", "key"))
    assert sig.category == "platform"


def test_signal_explicit_category_overrides_producer_default():
    """A producer may route a specific finding to a different bucket —
    e.g. an audit finding that's really a security issue (FileVault off)
    can be tagged category='security' to land under the Security tab
    rather than audit's default 'platform' bucket."""
    sig = _base_signal(category="security")  # producer=audit, override
    assert sig.category == "security"
    # Roundtrips through JSON.
    sig2 = Signal.from_dict(sig.to_dict())
    assert sig2.category == "security"


def test_signal_category_serialized_in_to_dict():
    sig = _base_signal()
    d = sig.to_dict()
    assert "category" in d
    assert d["category"] == "platform"


def test_old_format_signal_json_derives_category_from_producer():
    """Signals written before the category field landed get a derived
    category on load — never None, always one of the six buckets."""
    old_json = {
        "id": "old-cost-1",
        "schema_version": 1,
        "signature": "cost_watchdog:spike:team_bot_c",
        "producer": "cost_watchdog",
        "type": "spike",
        "flavor": "maintenance",
        "severity": "warn",
        "scope": "bot",
        "bot_id": "team_bot_c",
        "title": "cost spike",
        "body": "",
        "details": {},
        "state": "firing",
        "created_at": "2026-05-01T00:00:00+00:00",
        "last_observed_at": "2026-05-01T00:00:00+00:00",
        "observation_count": 1,
        "state_history": [],
        "motivated_proposals": [],
        "deliveries": [],
        # no category key — pre-existing stored signal
    }
    sig = Signal.from_dict(old_json)
    assert sig.category == "cost"


def test_category_for_producer_helper_covers_review_producers():
    """The six clusters identified in the 2026-06-03 Alerts review all
    map to non-platform categories — verifying that the routing dict is
    populated rather than relying on the silent 'platform' fallback."""
    from schema.signal import category_for_producer

    assert category_for_producer("security_warden") == "security"
    assert category_for_producer("compliance_scan") == "security"
    assert category_for_producer("cost_watchdog") == "cost"
    assert category_for_producer("session_economics") == "cost"
    assert category_for_producer("cascade_audit") == "cost"
    assert category_for_producer("backup_signal") == "backup"
    assert category_for_producer("integration_probe") == "integrations"


def test_category_for_producer_routes_security_flavor_producers():
    """The 2026-06-06 count-normalization spec adds plugin_monitor,
    content_scan, mcp_monitor, and permission_monitor to the
    PRODUCER_CATEGORY_DEFAULT map. Without these entries, the four
    producers fall through to "platform", which is what caused the
    Security category chip to show "(1)" while 22 security-flavor
    signals were actually firing on the pod (they bucketed into
    Platform). See docs/spec-alerts-count-normalization-2026-06-06.md.
    """
    from schema.signal import category_for_producer

    assert category_for_producer("plugin_monitor") == "security"
    assert category_for_producer("content_scan") == "security"
    assert category_for_producer("mcp_monitor") == "security"
    assert category_for_producer("permission_monitor") == "security"
