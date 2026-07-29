"""Tests for evolve_admin.migrate_model_roles — tier→role config migration.

Covers the pure transforms (idempotency + correctness across all three
surfaces) and the filesystem driver against tmp paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from evolve_admin import migrate_model_roles as mmr


# ── models block ──────────────────────────────────────────────────────────────


def test_migrate_models_block_basic():
    models = {
        "tiers": {
            "tier0": {"name": "Judge", "models": ["openai/gpt-4o"], "costClass": "medium"},
            "tier1": {"name": "Power", "models": ["anthropic/claude-opus-4-6"], "maxPerDayPerBot": 10, "costClass": "high"},
            "tier2": {"name": "Workhorse", "models": ["anthropic/claude-sonnet-4-6"], "fallbacks": ["openai/gpt-4o"], "costClass": "medium"},
            "tier3": {"name": "Grunt", "models": ["anthropic/claude-haiku-4-5"], "fallbacks": ["openai/gpt-4o-mini"], "costClass": "low"},
        },
        "routing": {"enabled": True, "maintenanceTier": "tier3", "backgroundTier": "tier3", "ambiguousTier": None},
    }
    out, changed = mmr.migrate_models_block(models)
    assert changed is True
    assert "tiers" not in out

    # Rungs are cost-ordered cheapest-first; judge owns its own judge-class rung
    # (spec §Addendum 16), cost-ordered next to sonnet-class (both medium).
    rung_ids = [r["id"] for r in out["rungs"]]
    assert rung_ids == ["haiku-class", "sonnet-class", "judge-class", "opus-class"]
    haiku = next(r for r in out["rungs"] if r["id"] == "haiku-class")
    assert haiku["models"] == ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"]
    assert haiku["costClass"] == "low"

    # Roles map tier keys → role IDs; judge is structured and migrates tier0 into
    # its OWN judge-class rung, not folded into standard's sonnet-class.
    assert out["roles"]["fast"] == "haiku-class"
    assert out["roles"]["standard"] == "sonnet-class"
    assert out["roles"]["power"] == "opus-class"
    assert out["roles"]["judge"] == {"rung": "judge-class", "provider": "not-standard"}
    judge = next(r for r in out["rungs"] if r["id"] == "judge-class")
    assert judge["models"] == ["openai/gpt-4o"]
    sonnet = next(r for r in out["rungs"] if r["id"] == "sonnet-class")
    assert "openai/gpt-4o" in sonnet["models"]

    # tier1 cap → roleCaps.power.
    assert out["roleCaps"]["power"]["maxPerDayPerBot"] == 10

    # routing.*Tier → *Role with value mapping; null ambiguous preserved.
    assert out["routing"]["maintenanceRole"] == "fast"
    assert out["routing"]["backgroundRole"] == "fast"
    assert out["routing"]["ambiguousRole"] is None
    assert "maintenanceTier" not in out["routing"]


def test_migrate_models_block_idempotent():
    already = {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
        ],
        "roles": {"fast": "haiku-class", "standard": "sonnet-class"},
        "routing": {"enabled": True, "maintenanceRole": "fast"},
    }
    out, changed = mmr.migrate_models_block(already)
    assert changed is False
    assert out == already


def test_migrate_models_block_routing_only():
    # No tiers/rungs, just a routing block with legacy keys.
    models = {"routing": {"backgroundTier": "tier2"}}
    out, changed = mmr.migrate_models_block(models)
    assert changed is True
    assert out["routing"]["backgroundRole"] == "standard"


# ── userTierOverride ──────────────────────────────────────────────────────────


def test_migrate_user_tier_override():
    override = {"enabled": True, "dailyCap": 7, "allowBotInitiated": True, "defaultTier": "tier1"}
    out, changed = mmr.migrate_user_tier_override(override)
    assert changed is True
    # dailyCap preserved (loader folds it); allowBotInitiated bool → per-role
    # with max NEVER inheriting the blanket-true.
    assert out["dailyCap"] == 7
    assert out["allowBotInitiated"] == {"power": True, "max": False}
    assert out["defaultRole"] == "power"
    assert "defaultTier" not in out


def test_migrate_user_tier_override_false_bot_initiated():
    out, changed = mmr.migrate_user_tier_override({"allowBotInitiated": False})
    assert changed is True
    assert out["allowBotInitiated"] == {"power": False, "max": False}


def test_migrate_user_tier_override_idempotent():
    override = {"allowBotInitiated": {"power": True, "max": False}, "defaultRole": "standard"}
    out, changed = mmr.migrate_user_tier_override(override)
    assert changed is False
    assert out == override


# ── network ───────────────────────────────────────────────────────────────────


def test_migrate_network_full():
    network = {
        "models": {
            "tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}, "tier3": {"models": ["anthropic/claude-haiku-4-5"]}},
            "routing": {"maintenanceTier": "tier3"},
        },
        "userTierOverride": {"defaultTier": "tier2", "allowBotInitiated": True},
    }
    out, changed = mmr.migrate_network(network)
    assert changed is True
    assert "rungs" in out["models"]
    assert out["models"]["routing"]["maintenanceRole"] == "fast"
    assert out["userTierOverride"]["defaultRole"] == "standard"


def test_migrate_network_idempotent():
    # A file already on canonical rung ids WITH costClass set is a true no-op —
    # the canonicalization step (rename + costClass backfill, spec Addendum 8 §D)
    # finds nothing to change. costClass must be present, else the backfill is a
    # legitimate change (that path is covered by the *-default migration tests).
    network = {
        "models": {
            "rungs": [{"id": "sonnet-class", "models": ["x"], "costClass": "medium"}],
            "roles": {"standard": "sonnet-class"},
        },
    }
    out, changed = mmr.migrate_network(network)
    assert changed is False


# ── evolve-tiers.json ─────────────────────────────────────────────────────────


def test_migrate_evolve_tiers():
    tiers_file = {
        "tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}, "tier1": {"models": ["anthropic/claude-opus-4-6"]}},
        "routing": {"enabled": True, "maintenanceTier": "tier3"},
        "userTierOverride": {"dailyCap": 5, "allowBotInitiated": True, "defaultTier": "tier1"},
        "cascade": {"enabled": False},
    }
    out, changed = mmr.migrate_evolve_tiers(tiers_file)
    assert changed is True
    assert "tiers" not in out
    assert out["roles"]["standard"] == "sonnet-class"
    assert out["roles"]["power"] == "opus-class"
    assert out["routing"]["maintenanceRole"] == "fast"
    assert out["userTierOverride"]["allowBotInitiated"] == {"power": True, "max": False}
    assert out["userTierOverride"]["defaultRole"] == "power"
    # cascade block untouched.
    assert out["cascade"] == {"enabled": False}


def test_migrate_evolve_tiers_idempotent():
    tiers_file = {
        "rungs": [{"id": "sonnet-class", "models": ["x"], "costClass": "medium"}],
        "roles": {"standard": "sonnet-class"},
        "routing": {"maintenanceRole": "fast"},
        "userTierOverride": {"allowBotInitiated": {"power": True, "max": False}},
    }
    out, changed = mmr.migrate_evolve_tiers(tiers_file)
    assert changed is False


# ── mixed-shape reconcile (freshness-apply pollution recovery) ────────────────


def test_migrate_evolve_tiers_mixed_shape_folds_and_strips():
    # The pollution shape: rungs (correct) + a stale legacy tiers key written
    # by an old-code freshness "apply". migrate must fold the legacy content
    # into the rung the mapped role points at AND drop the stale tiers key.
    mixed = {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
        ],
        "roles": {"fast": "haiku-class", "power": "opus-class"},
        # Operator's polluted write: tier1 (power) pointed at an old opus.
        "tiers": {"tier1": {"models": ["anthropic/claude-opus-4-6"]}},
    }
    out, changed = mmr.migrate_evolve_tiers(mixed)
    assert changed is True
    assert "tiers" not in out
    opus = next(r for r in out["rungs"] if r["id"] == "opus-class")
    assert opus["models"] == ["anthropic/claude-opus-4-6"]


def test_migrate_evolve_tiers_mixed_shape_idempotent():
    mixed = {
        "rungs": [{"id": "opus-class", "models": ["anthropic/claude-opus-4-6"], "costClass": "high"}],
        "roles": {"power": "opus-class"},
        "tiers": {"tier1": {"models": ["anthropic/claude-opus-4-6"]}},
    }
    once, c1 = mmr.migrate_evolve_tiers(mixed)
    assert c1 is True
    assert "tiers" not in once
    # Re-running on the cleaned file changes nothing.
    twice, c2 = mmr.migrate_evolve_tiers(once)
    assert c2 is False
    assert twice == once


def test_migrate_evolve_tiers_mixed_creates_missing_role_rung():
    # Stale tiers names a tier the rungs/roles don't yet carry.
    mixed = {
        "rungs": [{"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"}],
        "roles": {"fast": "haiku-class"},
        "tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}},
    }
    out, changed = mmr.migrate_evolve_tiers(mixed)
    assert changed is True
    assert "tiers" not in out
    assert out["roles"]["standard"] == "sonnet-class"
    sonnet = next(r for r in out["rungs"] if r["id"] == "sonnet-class")
    assert sonnet["models"] == ["anthropic/claude-sonnet-4-6"]


# ── user-tier-prefs.json ──────────────────────────────────────────────────────


def test_migrate_user_tier_prefs():
    prefs = {
        "users": {
            "ext:slack:U1": {"defaultTier": "tier1"},
            "ext:tg:42": {"defaultTier": "tier0"},
            "ext:tg:99": {"defaultRole": "max"},  # already role-shaped, max kept
        }
    }
    out, changed = mmr.migrate_user_tier_prefs(prefs)
    assert changed is True
    assert out["users"]["ext:slack:U1"]["defaultRole"] == "power"
    assert "defaultTier" not in out["users"]["ext:slack:U1"]
    assert out["users"]["ext:tg:42"]["defaultRole"] == "judge"
    assert out["users"]["ext:tg:99"]["defaultRole"] == "max"


def test_migrate_user_tier_prefs_idempotent():
    prefs = {"users": {"ext:slack:U1": {"defaultRole": "power"}}}
    out, changed = mmr.migrate_user_tier_prefs(prefs)
    assert changed is False
    assert out == prefs


def test_migrate_user_tier_prefs_empty():
    out, changed = mmr.migrate_user_tier_prefs({"users": {}})
    assert changed is False


# ── driver (filesystem) ───────────────────────────────────────────────────────


def test_run_migration_dry_run_no_writes(tmp_path: Path):
    network = {
        "sharedDir": str(tmp_path / "shared"),
        "models": {"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}},
        "bots": {"botA": {}},
    }
    network_path = tmp_path / "network.json"
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}}}))

    saved: list = []
    changes = mmr.run_migration(
        network=network,
        network_path=network_path,
        shared_dir=tmp_path / "shared",
        bot_homes={"botA": home},
        apply_changes=False,
        save_network_fn=lambda d, p: saved.append((d, p)),
    )
    assert len(changes) == 2  # network + bot tiers
    assert saved == []  # dry-run wrote nothing
    # The on-disk tiers file is untouched.
    assert "tiers" in json.loads(tiers_path.read_text())


def test_run_migration_apply_writes(tmp_path: Path):
    shared = tmp_path / "shared"
    network = {
        "sharedDir": str(shared),
        "models": {"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}},
        "bots": {"botA": {}},
    }
    network_path = tmp_path / "network.json"
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}}, "userTierOverride": {"allowBotInitiated": True}}))

    prefs_dir = shared / "botA"
    prefs_dir.mkdir(parents=True)
    prefs_path = prefs_dir / "user-tier-prefs.json"
    prefs_path.write_text(json.dumps({"users": {"ext:slack:U1": {"defaultTier": "tier1"}}}))

    saved: list = []
    changes = mmr.run_migration(
        network=network,
        network_path=network_path,
        shared_dir=shared,
        bot_homes={"botA": home},
        apply_changes=True,
        save_network_fn=lambda d, p: saved.append((d, p)),
    )
    assert len(changes) == 3  # network + tiers + prefs

    # network saved via the injected fn (not via real save_network).
    assert len(saved) == 1
    assert "rungs" in saved[0][0]["models"]

    # bot tiers rewritten in place.
    migrated_tiers = json.loads(tiers_path.read_text())
    assert "tiers" not in migrated_tiers
    assert migrated_tiers["roles"]["fast"] == "haiku-class"
    assert migrated_tiers["userTierOverride"]["allowBotInitiated"] == {"power": True, "max": False}

    # prefs rewritten.
    migrated_prefs = json.loads(prefs_path.read_text())
    assert migrated_prefs["users"]["ext:slack:U1"]["defaultRole"] == "power"


def test_run_migration_idempotent_second_pass(tmp_path: Path):
    shared = tmp_path / "shared"
    network = {
        "sharedDir": str(shared),
        "models": {"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}},
        "bots": {"botA": {}},
    }
    network_path = tmp_path / "network.json"
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}}}))

    # First pass applies.
    mmr.run_migration(
        network=network, network_path=network_path, shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    # Re-read migrated network + run again — nothing left to change.
    migrated_network, _ = mmr.migrate_network(network)
    changes = mmr.run_migration(
        network=migrated_network, network_path=network_path, shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    assert changes == []


# ── *-default rung-id canonicalization (spec Addendum 8 §D) ────────────────────
#
# The pod-default editor / easy-setup wrote synthetic ``*-default`` rung ids
# with no costClass; the keyed merge deduped by id so they accumulated on top of
# the code default instead of overlaying. The migration renames → canonical ids,
# backfills costClass, dedupes, re-derives never-edited buggy-default seeds, and
# preserves models.embedding.


def _live_pod_buggy_models() -> dict:
    """The live pod's mis-keyed models block (synthetic ids, stale seed)."""
    return {
        "embedding": {"per_bot": {"a_bot": {"chain": ["gemini", "local"]}}},
        "rungs": [
            {"id": "fast-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["haiku-class"])},
            {"id": "standard-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["sonnet-class"])},
            {"id": "power-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["opus-class"])},
            {"id": "max-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["fable-class"])},
            # judge carries the same SET as the old sonnet-class, reordered.
            {"id": "judge-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["sonnet-class"])},
        ],
        "roles": {
            "fast": "fast-default",
            "standard": "standard-default",
            "power": "power-default",
            "max": "max-default",
            "judge": {"rung": "judge-default", "provider": "not-standard"},
        },
    }


def test_default_canonicalization_renames_backfills_rederives_preserves_embedding():
    out, changed = mmr.migrate_models_block(_live_pod_buggy_models())
    assert changed is True

    # Canonical ids, cost-ordered; judge owns its own judge-class rung (spec
    # §Addendum 16), cost-ordered next to sonnet-class (5 rungs).
    ids = [r["id"] for r in out["rungs"]]
    assert ids == [
        "haiku-class", "sonnet-class", "judge-class", "opus-class", "fable-class",
    ], ids
    # costClass backfilled on every rung.
    assert all(r.get("costClass") for r in out["rungs"]), out["rungs"]
    # Roles point at canonical ids; judge keeps {rung, provider}.
    assert out["roles"]["fast"] == "haiku-class"
    assert out["roles"]["max"] == "fable-class"
    assert out["roles"]["judge"] == {"rung": "judge-class", "provider": "not-standard"}

    # Stale buggy-seed ids re-derived from the CORRECTED code default.
    flat = " ".join(m for r in out["rungs"] for m in r["models"])
    assert "gpt-5.5" not in flat and "gpt-5.4-mini" not in flat, flat
    from primary_bot import DEFAULT_MODEL_CATALOG  # type: ignore
    default_flat = {
        m for r in DEFAULT_MODEL_CATALOG["rungs"] for m in r["models"]
    }
    haiku = next(r for r in out["rungs"] if r["id"] == "haiku-class")
    assert set(haiku["models"]) == set(
        next(r for r in DEFAULT_MODEL_CATALOG["rungs"] if r["id"] == "haiku-class")["models"]
    )
    assert default_flat  # sanity: catalog importable

    # models.embedding survives untouched.
    assert out["embedding"] == {"per_bot": {"a_bot": {"chain": ["gemini", "local"]}}}


def test_default_canonicalization_idempotent_second_pass():
    out1, changed1 = mmr.migrate_models_block(_live_pod_buggy_models())
    assert changed1 is True
    out2, changed2 = mmr.migrate_models_block(out1)
    assert changed2 is False
    assert out2["rungs"] == out1["rungs"]
    assert out2["roles"] == out1["roles"]


def test_default_canonicalization_preserves_operator_edits():
    # An operator-edited cluster (differs from the buggy seed) is NOT re-derived,
    # only canonicalized (id rename + costClass backfill).
    models = _live_pod_buggy_models()
    for r in models["rungs"]:
        if r["id"] == "power-default":
            r["models"] = ["anthropic/claude-opus-4-8"]  # operator trimmed
    out, changed = mmr.migrate_models_block(models)
    assert changed is True
    opus = next(r for r in out["rungs"] if r["id"] == "opus-class")
    assert opus["models"] == ["anthropic/claude-opus-4-8"]  # preserved
    assert opus["costClass"] == "high"  # still backfilled


def test_default_canonicalization_noop_on_clean_canonical_file():
    # A file already on canonical ids with costClass and non-stale clusters is
    # left ENTIRELY untouched (the canonicalizer drops unpointed rungs, so a
    # no-op guard prevents discarding a legitimate orphan/fallback rung).
    from primary_bot import DEFAULT_MODEL_CATALOG  # type: ignore
    clean = {
        "embedding": {"x": 1},
        "rungs": json.loads(json.dumps(DEFAULT_MODEL_CATALOG["rungs"])),
        "roles": json.loads(json.dumps(DEFAULT_MODEL_CATALOG["roles"])),
    }
    out, changed = mmr.migrate_models_block(clean)
    assert changed is False


def test_default_canonicalization_via_evolve_tiers():
    # Bot evolve-tiers.json goes through migrate_evolve_tiers (top-level rungs).
    bot_file = {
        "rungs": [
            {"id": "fast-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["haiku-class"])},
            {"id": "standard-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["sonnet-class"])},
            {"id": "power-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["opus-class"])},
            {"id": "max-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["fable-class"])},
        ],
        "roles": {
            "fast": "fast-default", "standard": "standard-default",
            "power": "power-default", "max": "max-default",
        },
    }
    out, changed = mmr.migrate_evolve_tiers(bot_file)
    assert changed is True
    assert [r["id"] for r in out["rungs"]] == [
        "haiku-class", "sonnet-class", "opus-class", "fable-class",
    ]
    assert all(r.get("costClass") for r in out["rungs"])
