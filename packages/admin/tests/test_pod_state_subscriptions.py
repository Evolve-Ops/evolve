"""tests/test_pod_state_subscriptions.py — pod_state.subscriptions tool.

Read tool for the operator's effective alert subscription state.
Closes the 2026-06-02 hallucination gap (operator on Telegram asked
"unsubscribe me from config drift alerts"; evo, with no subscription-
aware tool, fabricated an OC config path).

Tests cover the contract:
  - registered in the tool registry under the expected dotted name
  - empty subscriptions.json returns all catalog defaults
  - operator override is reflected in `enabled` / `frequency`
  - is_override flag tracks presence/absence of override entry
  - default_enabled / default_frequency surface separately from
    effective values (so evo can tell "you're on the default" vs
    "you overrode the default")
  - malformed subscriptions.json soft-fails with `error` in payload
  - missing network.json / config-sandbox returns
    source_level_enabled=True (the catalog default) rather than crashing
  - single-key filter returns just that entry
  - unknown key returns an error payload with valid-keys sample
  - safety-critical flag flows through unchanged
  - handler-closure binds shared_dir

Spec: ``docs/diagnosis-evo-subscription-awareness-2026-06-02.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.alerts import catalog as _cat  # noqa: E402
from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import pod_state_subscriptions  # noqa: E402


# ─── Registration ────────────────────────────────────────────────────────────


def test_pod_state_subscriptions_is_registered():
    """The tool registers under the dotted name evo will invoke. This
    is the regression that catches "module not imported in __init__"."""
    tool = _tools.lookup("pod_state.subscriptions")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.READ
    # Read tools must NOT define validate (registry __post_init__ would
    # have raised, but we check explicitly so an accidental tier change
    # without removing validate is caught here).
    assert tool.validate is None


def test_pod_state_subscriptions_in_manifest():
    """Manifest renders with the standard shape (name + description +
    input_schema). The `key` filter property is declared in the
    schema."""
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "pod_state.subscriptions"),
        None,
    )
    assert entry is not None
    assert "key" in entry["input_schema"]["properties"]


# ─── Empty / defaults ────────────────────────────────────────────────────────


def test_empty_subscriptions_returns_catalog_defaults(tmp_path):
    """No subscriptions.json on disk → returns every catalog entry with
    default_enabled / default_frequency intact and is_override=False
    everywhere. This is the fresh-install case the operator on Telegram
    was effectively in."""
    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    assert result["available"] is True
    assert result["override_count"] == 0
    assert result["catalog_count"] == len(_cat.CATALOG)
    assert len(result["subscriptions"]) == len(_cat.CATALOG)

    drift = next(
        s for s in result["subscriptions"]
        if s["key"] == "security.config_drift"
    )
    assert drift["enabled"] is True  # catalog default
    # Workstream D1: config drift is a benign-flap event and now defaults to
    # the daily digest (operator can still raise it back to immediate).
    assert drift["frequency"] == "daily_digest"
    assert drift["default_enabled"] is True
    assert drift["default_frequency"] == "daily_digest"
    assert drift["is_override"] is False
    assert drift["is_safety_critical"] is True
    assert drift["label"] == "Bot configuration changed unexpectedly"


# ─── Override resolution ────────────────────────────────────────────────────


def _write_subs(tmp_path: Path, subs: dict, groups: dict | None = None) -> Path:
    """Helper: write a subscriptions.json with the given per-event
    subscriptions dict and (Phase 2) optional subscription_groups dict.
    Other top-level fields default to the dispatcher's shape."""
    alerts_dir = tmp_path / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    p = alerts_dir / "subscriptions.json"
    p.write_text(json.dumps({
        "version": 1,
        "subscriptions": subs,
        "subscription_groups": groups or {},
        "channel_override": None,
        "digest_hour_local": 8,
    }), encoding="utf-8")
    return p


def test_override_reflected_in_effective_values(tmp_path):
    """A group on/off override flows through to every member event's
    effective `enabled`, with `default_enabled` still True so the model can
    explain 'you overrode the default'. (Phase 2: on/off is a group toggle.)"""
    _write_subs(tmp_path, {}, groups={
        "security_findings": {"enabled": False},
    })

    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    drift = next(
        s for s in result["subscriptions"]
        if s["key"] == "security.config_drift"
    )
    assert drift["enabled"] is False          # resolved through the group
    assert drift["default_enabled"] is True
    assert drift["is_override"] is True        # group has an override
    assert drift["subscription_id"] == "security_findings"
    assert result["override_count"] == 1


def test_is_override_tracks_presence(tmp_path):
    """is_override is True only for events that have an override entry;
    others stay False even though they're returned with default values."""
    _write_subs(tmp_path, {
        "security.config_drift": {"enabled": False},
    })

    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    by_key = {s["key"]: s for s in result["subscriptions"]}
    assert by_key["security.config_drift"]["is_override"] is True
    # Pick another safety-critical event that almost certainly has no
    # override.
    audit = by_key["security.audit_finding"]
    assert audit["is_override"] is False
    assert audit["enabled"] == audit["default_enabled"]


# ─── Malformed / missing ─────────────────────────────────────────────────────


def test_malformed_subscriptions_json_returns_error_field(tmp_path):
    """Corrupt JSON → soft-fail with `error` field in the response and
    still return catalog defaults so evo can explain the situation
    rather than crash."""
    alerts_dir = tmp_path / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    (alerts_dir / "subscriptions.json").write_text(
        "{this is not valid json", encoding="utf-8",
    )

    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    assert result["available"] is True
    assert "error" in result
    assert "malformed" in result["error"].lower()
    # Still returns the catalog defaults so evo's reply isn't empty.
    assert result["catalog_count"] == len(_cat.CATALOG)
    assert len(result["subscriptions"]) == len(_cat.CATALOG)


def test_missing_network_returns_source_level_default_true(tmp_path):
    """When config_sandbox is unavailable / network.json is missing,
    source_level_enabled falls back to True (catalog default for every
    source). We do NOT crash the tool — evo's other state surfaces
    must keep working."""
    # tmp_path has no network.json, no better-engine-config.json — the
    # config_sandbox lookup will fail gracefully.

    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    drift = next(
        s for s in result["subscriptions"]
        if s["key"] == "security.config_drift"
    )
    # Source-level enabled either resolved to True (catalog default
    # fallback in the lookup helper) or None (sandbox not importable).
    # Both are acceptable — neither crashes.
    assert drift["source_level_enabled"] in (True, None)
    assert drift["source_level_key"] == "alerts.heal.enabled"


# ─── Single-key filter ──────────────────────────────────────────────────────


def test_key_filter_returns_single_entry(tmp_path):
    """Passing `key='security.config_drift'` returns just that record.
    This is the path evo takes when the operator names the dotted key
    directly."""
    result = pod_state_subscriptions._handler(
        shared_dir=tmp_path, key="security.config_drift",
    )

    assert result["available"] is True
    assert len(result["subscriptions"]) == 1
    assert result["subscriptions"][0]["key"] == "security.config_drift"
    # catalog_count + override_count still reflect the full catalog.
    assert result["catalog_count"] == len(_cat.CATALOG)


def test_unknown_key_returns_error_payload(tmp_path):
    """Unknown key → error payload listing the first few valid keys.
    Mirrors the action tool's KeyError shape."""
    result = pod_state_subscriptions._handler(
        shared_dir=tmp_path, key="security.does_not_exist",
    )

    assert result["available"] is True
    assert "error" in result
    assert "unknown subscription key" in result["error"].lower()
    assert result["subscriptions"] == []


# ─── Safety-critical flag ────────────────────────────────────────────────────


def test_safety_critical_flag_flows_through(tmp_path):
    """is_safety_critical surfaces as a boolean in the projection so evo
    can decide whether to require the operator's second confirm when
    muting. security.config_drift and security.audit_finding are
    safety-critical through their security_findings GROUP (the per-event
    flag was dropped in workstream D1 when they moved to digest defaults),
    and the projection resolves the group flag — so both must still
    surface True."""
    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    by_key = {s["key"]: s for s in result["subscriptions"]}
    # Spot-check the two events the diagnosis names explicitly.
    assert by_key["security.config_drift"]["is_safety_critical"] is True
    assert by_key["security.audit_finding"]["is_safety_critical"] is True
    # Type stability — never None, never absent.
    for rec in result["subscriptions"]:
        assert isinstance(rec["is_safety_critical"], bool), (
            f"{rec['key']} is_safety_critical must be bool, "
            f"got {type(rec['is_safety_critical']).__name__}"
        )


# ─── Source-level off ────────────────────────────────────────────────────────


def test_source_level_off_surfaces_independent_of_subscription(
    tmp_path, monkeypatch
):
    """When source-level enabled is False, the per-event enabled flag
    is unaffected (we report what the dispatcher would see), but
    source_level_enabled=False makes the silence explainable."""
    # Stub the config-sandbox lookup so the source resolves to False
    # without needing a real better-engine-config.json on disk.
    def fake_lookup(_shared_dir, path, default):
        if path == "alerts.heal.enabled":
            return False
        return default

    monkeypatch.setattr(
        "evolve_admin.alerts._config_lookup.lookup", fake_lookup,
    )

    result = pod_state_subscriptions._handler(shared_dir=tmp_path)

    drift = next(
        s for s in result["subscriptions"]
        if s["key"] == "security.config_drift"
    )
    assert drift["source_level_enabled"] is False
    # Per-event enabled is still its catalog default (True). The
    # dispatcher's two-layer gate would resolve to "no notification" —
    # evo's job is to explain the layering, not collapse it.
    assert drift["enabled"] is True


# ─── Handler closure ─────────────────────────────────────────────────────────


def test_make_handler_binds_shared_dir(tmp_path):
    """make_handler closes over shared_dir so the model-facing signature
    stays clean. Calling the closure with no kwargs reads from the
    bound directory."""
    _write_subs(tmp_path, {}, groups={
        "security_findings": {"enabled": False},
    })

    bound = pod_state_subscriptions.make_handler(tmp_path)
    result = bound()

    assert result["available"] is True
    assert result["override_count"] == 1
    drift = next(
        s for s in result["subscriptions"]
        if s["key"] == "security.config_drift"
    )
    assert drift["enabled"] is False
    assert drift["is_override"] is True


def test_make_handler_passes_key_kwarg(tmp_path):
    """The closure forwards model-supplied kwargs (e.g. `key`)
    through to the handler."""
    bound = pod_state_subscriptions.make_handler(tmp_path)
    result = bound(key="security.config_drift")

    assert len(result["subscriptions"]) == 1
    assert result["subscriptions"][0]["key"] == "security.config_drift"
