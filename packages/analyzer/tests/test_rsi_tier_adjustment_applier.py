"""tests/test_rsi_tier_adjustment_applier.py — TierAdjustment applier."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.appliers import get_applier  # noqa: E402
from arbiter.appliers.tier_adjustment import set_config_io  # noqa: E402
from schema.proposal import TierAdjustment  # noqa: E402


def _stub_config_io():
    """Build (get_fn, set_fn, store) where store is a dict the fns mutate."""
    store: dict[str, dict] = {}

    def get_fn(bot_id: str) -> dict | None:
        return dict(store.get(bot_id) or {})

    def set_fn(bot_id: str, updates: dict) -> dict | None:
        cur = dict(store.get(bot_id) or {})
        # Mirror oc_full_config_set semantics: replace whole sections.
        for k, v in updates.items():
            cur[k] = v
        store[bot_id] = cur
        return cur

    return get_fn, set_fn, store


def test_apply_writes_maintenance_tier_with_alias_resolution():
    get_fn, set_fn, store = _stub_config_io()
    store["admin_bot"] = {
        "routing": {
            "enabled": True,
            "maintenanceTier": "tier2",
            "backgroundTier": "tier3",
            "ambiguousTier": None,
            "confidenceThreshold": 0.65,
        }
    }
    set_config_io(get_fn, set_fn)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="admin_bot", target_class="maintenance", new_tier="haiku"
        )
        snap = applier.capture_snapshot(action, "admin_bot")
        result = applier.apply(action, "admin_bot")
        assert result.ok, result.message
        assert store["admin_bot"]["routing"]["maintenanceTier"] == "tier3"
        # Other routing fields preserved.
        assert store["admin_bot"]["routing"]["backgroundTier"] == "tier3"
        assert store["admin_bot"]["routing"]["confidenceThreshold"] == 0.65
        # Snapshot captured the prior value for revert.
        assert snap["prior_routing"]["maintenanceTier"] == "tier2"

        revert = applier.revert(snap, "admin_bot")
        assert revert.ok, revert.message
        assert store["admin_bot"]["routing"]["maintenanceTier"] == "tier2"
    finally:
        set_config_io(None, None)


def test_apply_accepts_canonical_tier_id_passthrough():
    get_fn, set_fn, store = _stub_config_io()
    set_config_io(get_fn, set_fn)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="team_bot_b", target_class="background", new_tier="tier3"
        )
        result = applier.apply(action, "team_bot_b")
        assert result.ok
        assert store["team_bot_b"]["routing"]["backgroundTier"] == "tier3"
    finally:
        set_config_io(None, None)


def test_apply_rejects_unknown_target_class():
    get_fn, set_fn, _ = _stub_config_io()
    set_config_io(get_fn, set_fn)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="team_bot_a", target_class="nonsense", new_tier="haiku"
        )
        result = applier.apply(action, "team_bot_a")
        assert not result.ok
        assert "target_class" in result.message
    finally:
        set_config_io(None, None)


def test_apply_rejects_unknown_tier_alias():
    get_fn, set_fn, _ = _stub_config_io()
    set_config_io(get_fn, set_fn)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="team_bot_a", target_class="maintenance", new_tier="banana"
        )
        result = applier.apply(action, "team_bot_a")
        assert not result.ok
        assert "new_tier" in result.message
    finally:
        set_config_io(None, None)


def test_apply_returns_not_ok_on_write_failure():
    def get_fn(_bot):
        return {"routing": {}}

    def set_fn(_bot, _updates):
        return None  # simulate sudo/oc_model failure

    set_config_io(get_fn, set_fn)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="team_bot_a", target_class="maintenance", new_tier="haiku"
        )
        result = applier.apply(action, "team_bot_a")
        assert not result.ok
        assert "failed to write" in result.message
    finally:
        set_config_io(None, None)


# ── revert against the REAL writer (its whitelist can refuse a snapshot) ─────
#
# These drive oc_model itself rather than _stub_config_io, because the defect
# they cover lives in the writer's routing whitelist: it DROPS keys it rejects
# and still returns a success-shaped result. A stub that stores whatever it is
# handed cannot reproduce that, so a stub-only suite reported ok=True on a
# revert that changed nothing on disk (#3566 audit E-3 follow-up).


def _wire_real_config_io(tmp_path, monkeypatch, bot_id: str, routing: dict) -> Path:
    """Point the applier at real oc_model reads/writes over a temp HOME.

    Returns the seeded evolve-tiers.json path. Caller must
    ``set_config_io(None, None)`` in a finally.
    """
    import json

    import oc_model  # noqa: F401 — imported for its side-effect-free helpers

    home = tmp_path / bot_id
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"routing": routing}))
    monkeypatch.setenv("HOME", str(home))

    set_config_io(
        lambda b: oc_model.json_full_config(b, oc_json),
        lambda b, u: oc_model.json_full_config_set(b, u, oc_json_path=oc_json),
    )
    return tiers_path


def _stored_routing(tiers_path: Path) -> dict:
    import json

    return json.loads(tiers_path.read_text())["routing"]


def test_revert_reports_failure_when_a_non_canonical_role_cannot_be_restored(
    tmp_path, monkeypatch
):
    """A hand-edited role outside the canonical set (``turbo``) is refused by
    the writer's whitelist, so the snapshot cannot go back — the revert must
    say so instead of reporting success over the still-applied tier."""
    tiers_path = _wire_real_config_io(
        tmp_path, monkeypatch, "maintbot",
        {"enabled": True, "maintenanceRole": "turbo"},
    )
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="evolve", target_class="maintenance", new_tier="sonnet"
        )
        snap = applier.capture_snapshot(action, "evolve")
        # The unprojectable role is what the snapshot holds — there is no
        # maintenanceTier to restore.
        assert snap["prior_routing"]["maintenanceRole"] == "turbo"
        assert "maintenanceTier" not in snap["prior_routing"]

        # apply is correct here: the stale role is evicted, and the tier the
        # applier writes lands as its ROLE (the boundary translates *Tier keys
        # since #3662 — the runtime refuses a persisted *Tier on sight).
        assert applier.apply(action, "evolve").ok
        assert _stored_routing(tiers_path)["maintenanceRole"] == "standard"
        assert "maintenanceTier" not in _stored_routing(tiers_path)

        revert = applier.revert(snap, "evolve")
        # The reported result must match the on-disk state: nothing went back.
        assert not revert.ok, revert.message
        assert "maintenanceRole" in revert.message
        assert revert.details["unrestored"] == ["maintenanceRole"]
        stored = _stored_routing(tiers_path)
        assert stored["maintenanceRole"] == "standard"
        assert "maintenanceTier" not in stored
    finally:
        set_config_io(None, None)


def test_revert_restores_a_projectable_role_and_reports_ok(tmp_path, monkeypatch):
    """Control for the above: on an ordinary migrated bot the snapshot holds
    the PROJECTED tier, the writer accepts it, and revert is a real revert."""
    tiers_path = _wire_real_config_io(
        tmp_path, monkeypatch, "powerbot",
        {"enabled": True, "maintenanceRole": "power"},
    )
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="evolve", target_class="maintenance", new_tier="haiku"
        )
        snap = applier.capture_snapshot(action, "evolve")
        assert snap["prior_routing"]["maintenanceTier"] == "tier1"

        assert applier.apply(action, "evolve").ok
        # The applier writes the projected tier; the boundary persists the ROLE
        # (#3662 — a stored *Tier key would poison the plugin's router).
        assert _stored_routing(tiers_path)["maintenanceRole"] == "fast"
        assert "maintenanceTier" not in _stored_routing(tiers_path)

        revert = applier.revert(snap, "evolve")
        assert revert.ok, revert.message
        assert _stored_routing(tiers_path)["maintenanceRole"] == "power"
        assert "maintenanceTier" not in _stored_routing(tiers_path)
    finally:
        set_config_io(None, None)


def test_revert_restores_an_unprojectable_but_canonical_role(tmp_path, monkeypatch):
    """``max`` has no legacy tier, so the snapshot carries the ROLE — but the
    whitelist accepts it, so this one really does go back. The difference from
    ``turbo`` is the whitelist, not the projection."""
    tiers_path = _wire_real_config_io(
        tmp_path, monkeypatch, "maxbot",
        {"enabled": True, "maintenanceRole": "max"},
    )
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="evolve", target_class="maintenance", new_tier="haiku"
        )
        snap = applier.capture_snapshot(action, "evolve")
        assert snap["prior_routing"]["maintenanceRole"] == "max"

        assert applier.apply(action, "evolve").ok
        applied = _stored_routing(tiers_path)
        assert applied["maintenanceRole"] == "fast"  # tier3, boundary-translated

        revert = applier.revert(snap, "evolve")
        assert revert.ok, revert.message
        restored = _stored_routing(tiers_path)
        assert restored["maintenanceRole"] == "max"
        assert restored.get("maintenanceTier") is None
    finally:
        set_config_io(None, None)


# ── target_class="primary" (writes openclaw.json's defaults.model.primary) ───


def _stub_primary_io():
    """Build (get_oc_full_fn, get_tier_models_fn, write_primary_fn, primary_store)."""
    # primary_store["<bot_id>"] = "<current primary model string>"
    primary_store: dict[str, str] = {}
    # tiers_by_bot["<bot_id>"]["tierN"] = ["model1", ...]
    tiers_by_bot: dict[str, dict[str, list[str]]] = {}
    writes: list[tuple[str, str]] = []

    def get_oc_full_fn(bot_id: str) -> dict | None:
        return {
            "agents": {"defaults": {"model": {
                "primary": primary_store.get(bot_id, ""),
            }}}
        }

    def get_tier_models_fn(bot_id: str, tier_id: str) -> list[str]:
        return list((tiers_by_bot.get(bot_id) or {}).get(tier_id) or [])

    def write_primary_fn(bot_id: str, model: str) -> tuple[bool, str]:
        primary_store[bot_id] = model
        writes.append((bot_id, model))
        return True, f"wrote primary={model!r} for {bot_id}"

    return (
        get_oc_full_fn, get_tier_models_fn, write_primary_fn,
        primary_store, tiers_by_bot, writes,
    )


def test_apply_primary_writes_first_tier_model():
    """target_class="primary", new_tier="haiku" → resolves to the first
    model in the haiku tier and writes openclaw.json's primary field."""
    from arbiter.appliers.tier_adjustment import set_primary_io
    get_oc, get_tiers, write_pr, store, tiers, writes = _stub_primary_io()
    store["security_bot"] = "anthropic/claude-sonnet-4-6"
    tiers["security_bot"] = {
        "tier3": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"],
    }
    set_primary_io(get_oc, get_tiers, write_pr)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="security_bot", target_class="primary", new_tier="haiku",
        )
        snap = applier.capture_snapshot(action, "security_bot")
        result = applier.apply(action, "security_bot")
        assert result.ok, result.message
        assert store["security_bot"] == "anthropic/claude-haiku-4-5"
        # Snapshot carried the prior model for revert.
        assert snap["prior_primary_model"] == "anthropic/claude-sonnet-4-6"
        assert writes == [("security_bot", "anthropic/claude-haiku-4-5")]

        revert = applier.revert(snap, "security_bot")
        assert revert.ok, revert.message
        assert store["security_bot"] == "anthropic/claude-sonnet-4-6"
    finally:
        set_primary_io(None, None, None)


def test_apply_primary_rejects_when_tier_has_no_models():
    """Defensive: if the requested tier isn't registered in the bot's
    tiers config, refuse to write rather than blank the primary field."""
    from arbiter.appliers.tier_adjustment import set_primary_io
    get_oc, get_tiers, write_pr, store, tiers, writes = _stub_primary_io()
    store["security_bot"] = "anthropic/claude-sonnet-4-6"
    # No tier3 registered.
    tiers["security_bot"] = {}
    set_primary_io(get_oc, get_tiers, write_pr)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="security_bot", target_class="primary", new_tier="haiku",
        )
        result = applier.apply(action, "security_bot")
        assert not result.ok
        assert "no registered models" in result.message
        # Nothing written.
        assert store["security_bot"] == "anthropic/claude-sonnet-4-6"
        assert writes == []
    finally:
        set_primary_io(None, None, None)


def test_apply_primary_no_op_when_already_at_target():
    from arbiter.appliers.tier_adjustment import set_primary_io
    get_oc, get_tiers, write_pr, store, tiers, writes = _stub_primary_io()
    store["security_bot"] = "anthropic/claude-haiku-4-5"
    tiers["security_bot"] = {"tier3": ["anthropic/claude-haiku-4-5"]}
    set_primary_io(get_oc, get_tiers, write_pr)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="security_bot", target_class="primary", new_tier="haiku",
        )
        result = applier.apply(action, "security_bot")
        assert not result.ok
        assert "no-op" in result.message
        assert writes == []
    finally:
        set_primary_io(None, None, None)


def test_apply_primary_returns_not_ok_on_write_failure():
    from arbiter.appliers.tier_adjustment import set_primary_io
    get_oc, get_tiers, _, store, tiers, _ = _stub_primary_io()
    store["security_bot"] = "anthropic/claude-sonnet-4-6"
    tiers["security_bot"] = {"tier3": ["anthropic/claude-haiku-4-5"]}
    # Substitute a failing writer.
    def fail_write(bot_id: str, model: str) -> tuple[bool, str]:
        return False, "sudo cp failed: rc=1"
    set_primary_io(get_oc, get_tiers, fail_write)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="security_bot", target_class="primary", new_tier="haiku",
        )
        result = applier.apply(action, "security_bot")
        assert not result.ok
        assert "sudo cp failed" in result.message
    finally:
        set_primary_io(None, None, None)


def test_capture_snapshot_when_no_prior_routing():
    """Bots without an evolve-tiers.json yet snapshot an empty routing dict."""
    get_fn, set_fn, store = _stub_config_io()
    set_config_io(get_fn, set_fn)
    try:
        applier = get_applier("TierAdjustment")
        action = TierAdjustment(
            bot_id="personal_bot", target_class="maintenance", new_tier="haiku"
        )
        snap = applier.capture_snapshot(action, "personal_bot")
        assert snap["prior_routing"] == {}
        # Apply still succeeds; routing dict is created.
        result = applier.apply(action, "personal_bot")
        assert result.ok
        assert store["personal_bot"]["routing"]["maintenanceTier"] == "tier3"
        # Revert restores empty routing.
        applier.revert(snap, "personal_bot")
        assert store["personal_bot"]["routing"] == {}
    finally:
        set_config_io(None, None)
