"""Tests for evolve_admin.migrate_model_roles — tier→role config migration.

Covers the pure transforms (idempotency + correctness across all three
surfaces) and the filesystem driver against tmp paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

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


# ── write-helper permissions (#3566 post-hoc audit, finding B-4) ─────────────
#
# Both writers on the `migrate-model-roles --apply` path used a bare
# write_text + replace, so the resulting mode came from the umask instead of
# being pinned. `--apply` is run once, as root, across every bot on a pod, so a
# tightened umask silently produced files neither the bot nor its gateway
# plugin could read — the migration breaking the very config it exists to
# preserve. 0644 is the documented contract: `evo.user_tier_prefs.set_user_pref`
# (the OTHER writer of user-tier-prefs.json) pins it because the bot's
# ModelRouter reads that file on every routing decision.


def _modes_under_umask(tmp_path, umask):
    import os
    prev = os.umask(umask)
    try:
        shared = tmp_path / f"shared-{umask:04o}"
        shared.mkdir()
        prefs = shared / "user-tier-prefs.json"
        tiers = shared / "evolve-tiers.json"
        mmr._write_shared_json(prefs, {"users": {}})
        mmr._write_bot_owned_json(tiers, {"rungs": []})
        return (prefs.stat().st_mode & 0o777, tiers.stat().st_mode & 0o777)
    finally:
        os.umask(prev)


def test_shared_and_bot_writers_pin_0644_regardless_of_umask(tmp_path):
    """A tightened umask must not make the migrated files unreadable.

    0o077 is the case that regressed: pre-fix both writers landed 0600.
    """
    for umask in (0o022, 0o077):
        prefs_mode, tiers_mode = _modes_under_umask(tmp_path, umask)
        assert prefs_mode == 0o644, f"user-tier-prefs.json {oct(prefs_mode)} at umask {umask:04o}"
        assert tiers_mode == 0o644, f"evolve-tiers.json {oct(tiers_mode)} at umask {umask:04o}"


def test_shared_write_repairs_an_already_tightened_destination(tmp_path):
    """CONTRACT PIN (not a regression test) — pin the mode, never preserve it.

    Passes against the pre-fix code too, because a same-dir rename always
    carries the TEMP file's mode rather than the destination's. It exists so a
    future "preserve the destination mode" change — the shape `cp` without
    `-p` really does have, and which CLAUDE.md warns about for the sudo leg —
    fails loudly here instead of silently perpetuating a tightened file.
    """
    import os
    prefs = tmp_path / "user-tier-prefs.json"
    prefs.write_text("{}")
    os.chmod(prefs, 0o600)
    mmr._write_shared_json(prefs, {"users": {"u": {"defaultRole": "fast"}}})
    assert prefs.stat().st_mode & 0o777 == 0o644
    assert json.loads(prefs.read_text())["users"]["u"]["defaultRole"] == "fast"


def test_bot_owned_writer_still_falls_through_to_sudo_when_parent_unwritable(
    tmp_path, monkeypatch,
):
    """The direct/sudo control flow is unchanged — only the mode is pinned.

    Asserts the sudo argv was actually ISSUED, not merely that "something
    raised": `pytest.raises(Exception)` would also pass if the direct branch
    threw a non-PermissionError that bypasses the fallback entirely, which is
    the precise regression this test exists to catch. `subprocess.run` is
    stubbed so no real `sudo` is invoked (a test that shells out to sudo passes
    for the wrong reason on a box that prompts, and fails under root).
    """
    calls = []

    class _R:
        returncode = 0
        stderr = ""

    def _fake_run(argv, *a, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(mmr.subprocess, "run", _fake_run)
    # chown_chmod_bot_config is the post-cp repair; stub it out so this test
    # pins the fallback ENTRY, not the repair helper's own behaviour.
    monkeypatch.setattr(
        "evolve_admin.secret_config_perms.chown_chmod_bot_config",
        lambda *a, **kw: None,
    )

    ro = tmp_path / "readonly"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        mmr._write_bot_owned_json(ro / "evolve-tiers.json", {"rungs": []})
    finally:
        os.chmod(ro, 0o700)

    assert calls, "direct branch did not fall through to the sudo path"
    argv = calls[0]
    assert argv[:3] == ["sudo", "/bin/cp"] or argv[0] == "sudo", argv
    assert str(ro / "evolve-tiers.json") in argv, argv


# ── symlink gate on the root-cp destination (#3566 audit D-2) ─────────────────
#
# `sudo /bin/cp tmp dest` FOLLOWS a symlink at dest — there is no cp flag that
# refuses to — and so do the `chown`/`chmod` that repair the mode afterwards.
# All three run as ROOT here, so an unchecked destination is an arbitrary
# root-write primitive; the sudoers path pin cannot help, because sudo matches
# the literal argv and the argv is the legitimate-looking link path. The gate
# is `evolve_util.assert_safe_sudo_dest`, shared with the writer of this SAME
# file on the analyzer side (`oc_model._save_tiers_file`).
#
# Every test below stubs `subprocess.run`: a test that really shells out to
# sudo passes for the wrong reason on a box that prompts, and would perform a
# genuine root write under root.


class _FakeRun:
    """Records argv instead of executing it; optional per-call side effect."""

    def __init__(self, on_call=None):
        self.calls: list[list[str]] = []
        self._on_call = on_call

    def __call__(self, argv, *a, **kw):
        self.calls.append(argv)
        if self._on_call is not None:
            self._on_call(argv)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()


def _force_sudo_branch(tmp_path):
    """A directory whose contents can be lstat'd but not created in, so the
    direct `atomic_write_json` raises PermissionError and the writer falls
    through to the /tmp + sudo path this gate guards."""
    d = tmp_path / "botdir"
    d.mkdir()
    return d


def _repair_spy(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        "evolve_admin.secret_config_perms.chown_chmod_bot_config",
        lambda p, *a, **kw: seen.append(p),
    )
    return seen


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
def test_bot_owned_writer_refuses_symlinked_destination(tmp_path, monkeypatch):
    """THE hazard. With a symlink planted at the dest, no root command may be
    issued at all — not the cp, not the ownership repair — and the victim must
    be byte-for-byte untouched."""
    d = _force_sudo_branch(tmp_path)
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    dest = d / "evolve-tiers.json"
    dest.symlink_to(victim)

    run = _FakeRun()
    monkeypatch.setattr(mmr.subprocess, "run", run)
    repaired = _repair_spy(monkeypatch)

    os.chmod(d, 0o500)
    try:
        with pytest.raises(PermissionError) as exc:
            mmr._write_bot_owned_json(dest, {"rungs": []})
    finally:
        os.chmod(d, 0o700)

    assert "SYMLINK" in str(exc.value)
    assert run.calls == [], f"a root command was issued anyway: {run.calls}"
    assert repaired == [], "the chown/chmod repair ran through the link"
    assert victim.read_text() == "secret"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
def test_bot_owned_writer_refuses_symlinked_parent(tmp_path, monkeypatch):
    """`<link>/evolve-tiers.json` redirects the write out of tree even with
    nothing planted at the leaf name."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "botdir"
    link.symlink_to(real)

    run = _FakeRun()
    monkeypatch.setattr(mmr.subprocess, "run", run)
    monkeypatch.setattr(
        "evolve_admin.secret_config_perms.chown_chmod_bot_config",
        lambda *a, **kw: None,
    )

    os.chmod(real, 0o500)
    try:
        with pytest.raises(PermissionError, match="symlink or not a directory"):
            mmr._write_bot_owned_json(link / "evolve-tiers.json", {"rungs": []})
    finally:
        os.chmod(real, 0o700)
    assert run.calls == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
def test_bot_owned_writer_rechecks_before_the_ownership_repair(tmp_path, monkeypatch):
    """The SECOND gate, and why it is not redundant.

    The check is lstat-then-subprocess, i.e. TOCTOU. Simulate a plant that wins
    the window by having the stubbed `cp` create the symlink: `cp` through a
    link leaves the LINK in place, so the post-cp re-check still sees it. The
    content overwrite is already lost at that point — what the re-check saves
    is the second half of the escalation, `chmod 644` relabelling the victim.

    The cp must still be reported as having happened (no raise): raising here
    would tell the operator a completed write failed.
    """
    d = _force_sudo_branch(tmp_path)
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    dest = d / "evolve-tiers.json"

    def plant(argv):
        os.chmod(d, 0o700)
        dest.symlink_to(victim)
        os.chmod(d, 0o500)

    run = _FakeRun(on_call=plant)
    monkeypatch.setattr(mmr.subprocess, "run", run)
    repaired = _repair_spy(monkeypatch)

    os.chmod(d, 0o500)
    try:
        mmr._write_bot_owned_json(dest, {"rungs": []})  # must NOT raise
    finally:
        os.chmod(d, 0o700)

    assert len(run.calls) == 1, run.calls  # the cp ran
    assert repaired == [], "chown/chmod ran on a destination that became a symlink"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
def test_bot_owned_writer_still_repairs_ownership_on_a_clean_destination(
    tmp_path, monkeypatch,
):
    """Counterpart to the two refusals: the gate must not have turned the
    ownership repair off for the ordinary case. Without this, a gate that
    always refused would look 'safe' and still be a silent outage — the bot
    could no longer read its own evolve-tiers.json."""
    d = _force_sudo_branch(tmp_path)
    dest = d / "evolve-tiers.json"
    dest.write_text("{}")

    run = _FakeRun()
    monkeypatch.setattr(mmr.subprocess, "run", run)
    repaired = _repair_spy(monkeypatch)

    os.chmod(d, 0o500)
    try:
        mmr._write_bot_owned_json(dest, {"rungs": []})
    finally:
        os.chmod(d, 0o700)

    assert len(run.calls) == 1, run.calls
    assert repaired == [dest]


def test_bot_owned_writer_direct_branch_replaces_a_symlink_rather_than_following(
    tmp_path,
):
    """The direct branch needs no gate, and this pins the reason rather than
    asserting it in a docstring: `atomic_write_json` promotes with `os.replace`,
    which unlinks the LINK itself. If that ever became a plain `open(dest,'w')`,
    the victim's content would change and this fails."""
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    dest = tmp_path / "evolve-tiers.json"
    dest.symlink_to(victim)

    mmr._write_bot_owned_json(dest, {"rungs": ["haiku-class"]})

    assert victim.read_text() == "secret"
    assert not dest.is_symlink()
    assert json.loads(dest.read_text()) == {"rungs": ["haiku-class"]}


# ── pod auto-upgrade carry on the Custom flip (#3566 audit) ───────────────────
#
# `primary_bot.bot_has_custom_tiers` decides Custom purely on a non-empty
# `rungs` array, so the migration's own write IS the Custom flip — and
# `model_auto_upgrade.bot_policy` does not give a Custom bot the pod's
# `enabled`. Without the carry, `migrate-model-roles --apply` flipped every
# migrated bot off pod auto-upgrade (latent until the pod turns auto-upgrade
# on). Every assertion goes through `bot_policy`, never key presence: absence
# and "follows the pod" coincide only while the bot is NON-Custom, which is
# exactly the premise this bug breaks.

_POD_AUTO_UPGRADE = {"enabled": True, "applyDay": "tuesday"}


def _is_custom(bot_doc: dict) -> bool:
    """`primary_bot.bot_has_custom_tiers`'s rule, restated on a raw doc.

    Deliberately NOT `mmr._bot_is_custom` — the tests must fail on BEHAVIOUR
    against a build without the carry, not on a missing private helper.
    """
    rungs = bot_doc.get("rungs")
    return isinstance(rungs, list) and bool(rungs)


def _policy(network: dict, bot_doc: dict):
    """Resolve the bot's effective auto-upgrade policy the way production does."""
    import model_auto_upgrade as mau  # type: ignore

    return mau.bot_policy(mau.pod_policy(network), bot_doc, custom=_is_custom(bot_doc))


def _network_with_pod_auto_upgrade(shared: Path, auto_upgrade=_POD_AUTO_UPGRADE) -> dict:
    models: dict = {"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}}
    if auto_upgrade is not None:
        models["autoUpgrade"] = dict(auto_upgrade)
    return {"sharedDir": str(shared), "models": models, "bots": {"botA": {}}}


def _legacy_bot_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(
        json.dumps({"tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}}})
    )
    return home, tiers_path


def test_legacy_bot_follows_pod_before_migration(tmp_path: Path):
    """Baseline: a legacy-shaped bot is NOT Custom, so it rides the pod."""
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    _, tiers_path = _legacy_bot_home(tmp_path)

    before = _policy(network, json.loads(tiers_path.read_text()))
    assert _is_custom(json.loads(tiers_path.read_text())) is False
    assert (before.enabled, before.enabled_source) == (True, "pod")


def test_migration_carries_pod_auto_upgrade_on_custom_flip(tmp_path: Path):
    """The regression: after --apply the bot must still resolve enabled=True."""
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    home, tiers_path = _legacy_bot_home(tmp_path)

    changes = mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )

    after_doc = json.loads(tiers_path.read_text())
    assert _is_custom(after_doc) is True  # the migration flipped it Custom
    after = _policy(network, after_doc)
    assert after.enabled is True
    assert after.enabled_source == "bot"
    # The pod's subordinate knobs come along too.
    assert after.apply_day == "tuesday"
    # And the carry is reported — as a clause on the file's own surface line,
    # so the CLI's "N surface(s)" count stays honest (network + this bot).
    assert any("carry pod models.autoUpgrade" in c for c in changes)
    assert len(changes) == 2
    assert sum("evolve-tiers.json" in c for c in changes) == 1


def test_migration_dry_run_reports_carry_and_writes_nothing(tmp_path: Path):
    """Dry-run parity: the seed is REPORTED, not done silently only on --apply."""
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    home, tiers_path = _legacy_bot_home(tmp_path)

    changes = mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=False,
        save_network_fn=lambda d, p: None,
    )
    assert any("carry pod models.autoUpgrade" in c for c in changes)
    # Nothing written — the file is still legacy-shaped.
    assert "tiers" in json.loads(tiers_path.read_text())


def test_migration_leaves_an_already_custom_bot_alone(tmp_path: Path):
    """An already-Custom bot getting only rung-id renames must NOT be seeded.

    Switching auto-upgrade ON for a Custom bot that deliberately carries no
    block is as wrong as switching it off.
    """
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    # New-shape (already Custom) but on synthetic ``*-default`` rung ids, so the
    # migration still has canonicalization work to do on it.
    tiers_path.write_text(json.dumps({
        "rungs": [
            {"id": "fast-default", "models": list(mmr._OLD_BUGGY_DEFAULT_CLUSTERS["haiku-class"])},
        ],
        "roles": {"fast": "fast-default"},
    }))
    doc_before = json.loads(tiers_path.read_text())
    assert _is_custom(doc_before) is True

    changes = mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    # The rename happened…
    after_doc = json.loads(tiers_path.read_text())
    assert [r["id"] for r in after_doc["rungs"]] == ["haiku-class"]
    # …but the auto-upgrade posture is untouched: still the code default.
    assert not any("carry pod models.autoUpgrade" in c for c in changes)
    before_pol = _policy(network, doc_before)
    after_pol = _policy(network, after_doc)
    assert (before_pol.enabled, before_pol.enabled_source) == (False, "code-default")
    assert (after_pol.enabled, after_pol.enabled_source) == (False, "code-default")


def test_migration_never_overwrites_the_bots_own_auto_upgrade_block(tmp_path: Path):
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({
        "tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}},
        "autoUpgrade": {"enabled": False, "applyDay": "friday"},
    }))

    changes = mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    after_doc = json.loads(tiers_path.read_text())
    assert after_doc["autoUpgrade"] == {"enabled": False, "applyDay": "friday"}
    assert not any("carry pod models.autoUpgrade" in c for c in changes)
    after = _policy(network, after_doc)
    assert (after.enabled, after.enabled_source) == (False, "bot")
    assert after.apply_day == "friday"


@pytest.mark.parametrize("pod_block", [None, {}], ids=["absent", "empty"])
def test_migration_carries_nothing_when_the_pod_has_no_block(tmp_path: Path, pod_block):
    """No pod block (or an empty one) → the bot resolves to the code default."""
    shared = tmp_path / "shared"
    home, tiers_path = _legacy_bot_home(tmp_path)
    network = _network_with_pod_auto_upgrade(shared, auto_upgrade=pod_block)

    changes = mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    after_doc = json.loads(tiers_path.read_text())
    assert "autoUpgrade" not in after_doc
    assert not any("carry pod models.autoUpgrade" in c for c in changes)
    pod = _policy(network, after_doc)
    assert (pod.enabled, pod.enabled_source) == (False, "code-default")


@pytest.mark.parametrize("junk", [[], ["a"], "hello", 5, True], ids=str)
def test_migration_skips_an_unparseable_tiers_doc_instead_of_aborting(
    tmp_path: Path, junk,
):
    """A hand-mangled evolve-tiers.json must not abort a fleet-wide run.

    The carry's Custom check is the only new reader of the raw doc, and
    `--apply` writes network.json + earlier-sorted bots BEFORE reaching it, so
    raising here would leave a half-applied migration.
    """
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    home = tmp_path / "homes" / "botA"
    (home / ".openclaw").mkdir(parents=True)
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps(junk))

    changes = mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    assert not any("botA" in c for c in changes)
    assert json.loads(tiers_path.read_text()) == junk  # left exactly as found


def test_migration_tolerates_a_non_dict_network(tmp_path: Path):
    """`migrate_network` already guards this shape; the pod-block read must too."""
    assert mmr.run_migration(
        network=["not-a-dict"],  # type: ignore[arg-type]
        network_path=tmp_path / "network.json", shared_dir=tmp_path / "shared",
        bot_homes={}, apply_changes=True, save_network_fn=lambda d, p: None,
    ) == []


def test_migration_carry_is_idempotent_on_a_second_apply(tmp_path: Path):
    shared = tmp_path / "shared"
    network = _network_with_pod_auto_upgrade(shared)
    home, tiers_path = _legacy_bot_home(tmp_path)

    mmr.run_migration(
        network=network, network_path=tmp_path / "network.json", shared_dir=shared,
        bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    first_pass = tiers_path.read_text()
    mtime = tiers_path.stat().st_mtime_ns

    migrated_network, _ = mmr.migrate_network(network)
    changes = mmr.run_migration(
        network=migrated_network, network_path=tmp_path / "network.json",
        shared_dir=shared, bot_homes={"botA": home}, apply_changes=True,
        save_network_fn=lambda d, p: None,
    )
    assert changes == []
    assert tiers_path.read_text() == first_pass
    assert tiers_path.stat().st_mtime_ns == mtime  # nothing was rewritten
    after = _policy(migrated_network, json.loads(tiers_path.read_text()))
    assert (after.enabled, after.enabled_source) == (True, "bot")
