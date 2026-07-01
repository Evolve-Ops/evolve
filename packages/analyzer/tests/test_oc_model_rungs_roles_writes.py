"""Write-path tests for the rungs/roles shape bridge in oc_model.

Guards the freshness-advisory "apply cosmetic write" half of the 2026-06-09
incident: a freshness apply wrote a sibling legacy ``tiers`` key into a
new-shape file, which the gateway loader ignores whenever ``rungs`` is
present — the operator's change never reached routing.

Pins:
  - config set on a new-shape file folds the legacy ``tiers`` update into the
    rung the mapped role points at; no stale ``tiers`` key remains;
  - synthesize_legacy_tiers projects a new-shape file back to the legacy view
    for read-path consumers;
  - a write that lands a mixed-shape file (stale tiers + rungs) is normalized
    on save (legacy folded, key dropped);
  - legacy-only files keep writing the legacy shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


NEW_SHAPE = {
    "rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
        {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ],
    "roles": {
        "fast": "haiku-class",
        "standard": "sonnet-class",
        "power": "opus-class",
        "judge": {"rung": "sonnet-class", "provider": "not-standard"},
    },
}


@pytest.fixture
def fake_bot_env(tmp_path, monkeypatch):
    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-haiku-4-5", "fallbacks": []}}},
    }))
    monkeypatch.setenv("HOME", str(home))
    # Stub the openclaw.json schema-validating write so tests run anywhere.
    monkeypatch.setattr(oc_model, "_preserve_write", lambda data, p: p.write_text(json.dumps(data)))
    return {"home": home, "oc_json": oc_json,
            "tiers_path": home / ".openclaw" / "evolve-tiers.json"}


def _seed_new_shape(env):
    env["tiers_path"].write_text(json.dumps(NEW_SHAPE))


# ── synthesize (read projection) ──────────────────────────────────────────────

def test_synthesize_projects_new_shape_to_legacy_view():
    legacy = oc_model.synthesize_legacy_tiers(NEW_SHAPE)
    assert legacy["tier3"]["models"] == ["anthropic/claude-haiku-4-5"]
    assert legacy["tier2"]["models"] == ["anthropic/claude-sonnet-4-6"]
    assert legacy["tier1"]["models"] == ["anthropic/claude-opus-4-8"]
    # tier0 → judge → sonnet-class rung.
    assert legacy["tier0"]["models"] == ["anthropic/claude-sonnet-4-6"]


def test_synthesize_legacy_only_returns_its_tiers():
    legacy_file = {"tiers": {"tier2": {"models": ["x"]}}}
    assert oc_model.synthesize_legacy_tiers(legacy_file) == {"tier2": {"models": ["x"]}}


# ── write: fold into new shape ────────────────────────────────────────────────

def test_config_set_folds_tier_update_into_rung(fake_bot_env):
    _seed_new_shape(fake_bot_env)
    # Freshness apply: bump tier1 (power) to a new opus.
    oc_model.json_full_config_set(
        bot="b",
        updates={"tiers": {"tier1": {"models": ["anthropic/claude-opus-4-9"]}}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    # The opus-class rung carries the new model...
    opus = next(r for r in written["rungs"] if r["id"] == "opus-class")
    assert opus["models"] == ["anthropic/claude-opus-4-9"]
    # ...and NO stale legacy tiers key was written (loader would ignore it).
    assert "tiers" not in written


def test_config_set_creates_role_and_rung_when_absent(fake_bot_env):
    # New-shape file without the standard role/rung adopted yet.
    sparse = {
        "rungs": [{"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"}],
        "roles": {"fast": "haiku-class"},
    }
    fake_bot_env["tiers_path"].write_text(json.dumps(sparse))
    oc_model.json_full_config_set(
        bot="b",
        updates={"tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert written["roles"]["standard"] == "sonnet-class"
    sonnet = next(r for r in written["rungs"] if r["id"] == "sonnet-class")
    assert sonnet["models"] == ["anthropic/claude-sonnet-4-6"]
    assert sonnet["costClass"] == "medium"
    assert "tiers" not in written


# ── write: normalize a mixed-shape file ───────────────────────────────────────

def test_write_normalizes_mixed_shape(fake_bot_env):
    # Simulate prior pollution: rungs + a stale legacy tiers key written by
    # old code. A subsequent (unrelated) write must fold + strip.
    mixed = dict(NEW_SHAPE)
    mixed["tiers"] = {"tier1": {"models": ["anthropic/claude-opus-4-6"]}}  # stale
    fake_bot_env["tiers_path"].write_text(json.dumps(mixed))
    # Any write triggers normalization — flip routing (evolve-tiers-only).
    oc_model.json_full_config_set(
        bot="b",
        updates={"routing": {"enabled": True, "maintenanceTier": "tier3"}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert "tiers" not in written
    opus = next(r for r in written["rungs"] if r["id"] == "opus-class")
    # The stale legacy content folded into the rung (operator's polluted
    # write is what we're recovering, so it wins on the named rung).
    assert opus["models"] == ["anthropic/claude-opus-4-6"]


# ── write: legacy-only files keep legacy shape ────────────────────────────────

def test_config_set_legacy_file_stays_legacy(fake_bot_env):
    fake_bot_env["tiers_path"].write_text(json.dumps({"tiers": {"tier3": {"models": ["x"]}}}))
    oc_model.json_full_config_set(
        bot="b",
        updates={"tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert written["tiers"]["tier3"]["models"] == ["anthropic/claude-haiku-4-5"]
    assert "rungs" not in written


# ── wholesale rungs/roles writes — the per-bot Use-defaults/Custom toggle ──────
# (spec-model-rungs-and-roles §Addendum 5). Distinct from the ``tiers`` fold
# above: these REPLACE the whole rung/role set wholesale, and an empty value
# CLEARS the key so the merge inherits the pod/code default again.


def test_wholesale_rungs_roles_write_materializes_custom(fake_bot_env):
    """Writing rungs+roles wholesale (the Customize seed) lands a Custom file.

    An inheriting bot (no per-bot tiers file) gets its first Custom config: the
    full merged rungs/roles arrive verbatim, flipping it to Custom."""
    # No tiers file yet → bot was "use pod defaults".
    assert not fake_bot_env["tiers_path"].exists()
    seed = json.loads(json.dumps(NEW_SHAPE))
    oc_model.json_full_config_set(
        bot="b",
        updates={"rungs": seed["rungs"], "roles": seed["roles"]},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert written["rungs"] == seed["rungs"]
    assert written["roles"] == seed["roles"]


def test_wholesale_synthetic_ids_canonicalized_on_write(fake_bot_env):
    """A bot Customize write with synthetic ``*-default`` rung ids is
    canonicalized on save (spec Addendum 8 §D) so the bot catalog OVERLAYS the
    pod/code default by matching ids instead of accumulating."""
    synthetic = {
        "rungs": [
            {"id": "fast-default", "models": ["anthropic/claude-haiku-4-5"]},
            {"id": "standard-default", "models": ["anthropic/claude-sonnet-4-6"]},
            {"id": "power-default", "models": ["anthropic/claude-opus-4-8"]},
            {"id": "max-default", "models": ["anthropic/claude-fable-5"]},
            {"id": "judge-default", "models": ["openai/gpt-4.1", "anthropic/claude-sonnet-4-6"]},
        ],
        "roles": {
            "fast": "fast-default", "standard": "standard-default",
            "power": "power-default", "max": "max-default",
            "judge": {"rung": "judge-default", "provider": "not-standard"},
        },
    }
    oc_model.json_full_config_set(
        bot="b",
        updates={"rungs": synthetic["rungs"], "roles": synthetic["roles"]},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert [r["id"] for r in written["rungs"]] == [
        "haiku-class", "sonnet-class", "opus-class", "fable-class",
    ]
    assert all(r.get("costClass") for r in written["rungs"])
    assert written["roles"]["max"] == "fable-class"
    assert written["roles"]["judge"] == {"rung": "sonnet-class", "provider": "not-standard"}


def test_wholesale_roleCaps_written_and_empty_clears(fake_bot_env):
    """roleCaps is carried wholesale on Customize; an empty dict clears it."""
    oc_model.json_full_config_set(
        bot="b",
        updates={
            "rungs": NEW_SHAPE["rungs"],
            "roles": NEW_SHAPE["roles"],
            "roleCaps": {"power": {"maxPerDayPerBot": 7}},
        },
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert written["roleCaps"] == {"power": {"maxPerDayPerBot": 7}}
    # Empty roleCaps clears the key.
    oc_model.json_full_config_set(
        bot="b", updates={"roleCaps": {}}, oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert "roleCaps" not in written


def test_reset_clears_rungs_roles_back_to_inherit(fake_bot_env):
    """The Reset path (empty rungs/roles) drops both keys so the file carries
    NO per-bot tier definitions — the merge inherits the pod/code default."""
    _seed_new_shape(fake_bot_env)  # Custom bot.
    assert "rungs" in json.loads(fake_bot_env["tiers_path"].read_text())
    oc_model.json_full_config_set(
        bot="b",
        updates={"rungs": [], "roles": {}, "roleCaps": {}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert "rungs" not in written
    assert "roles" not in written
    assert "roleCaps" not in written


def test_reset_preserves_unrelated_keys(fake_bot_env):
    """Reset clears only rungs/roles/roleCaps — sibling config (cascade,
    userTierOverride) on the same file survives the all-or-nothing reset."""
    seeded = json.loads(json.dumps(NEW_SHAPE))
    seeded["cascade"] = {"enabled": True}
    seeded["userTierOverride"] = {"defaultTier": "fast"}
    fake_bot_env["tiers_path"].write_text(json.dumps(seeded))
    oc_model.json_full_config_set(
        bot="b",
        updates={"rungs": [], "roles": {}},
        oc_json_path=fake_bot_env["oc_json"],
    )
    written = json.loads(fake_bot_env["tiers_path"].read_text())
    assert "rungs" not in written and "roles" not in written
    assert written["cascade"] == {"enabled": True}
    assert written["userTierOverride"] == {"defaultTier": "fast"}
