"""A bot's evolve-tiers.json must never be BORN on the deprecated tier shape.

#3566 follow-up to #3567. ``oc_model.save_model_config``'s handling of a
legacy-shaped ``{"tiers": {...}}`` update splits three ways:

  - file already carries ``rungs``  → fold into the rungs (unchanged);
  - file already carries ``tiers``  → keep writing ``tiers`` (unchanged — we
    refuse to half-migrate; ``evolve-admin migrate-model-roles`` is the
    whole-file conversion and it is the operator's call);
  - **nothing on disk**             → CREATE on the rungs/roles shape.

The third case is what this file pins. Before the fix it fell through to the
legacy branch, so an operator editing one tier on a never-seeded bot minted a
brand-new deprecated config — the shape the ModelRouter fallback exists to
absorb, and the reason that fallback could not be deleted.

Writing ``rungs`` is what makes a bot Custom, and a Custom bot does NOT inherit
the pod's auto-upgrade ``enabled`` — so the create path also has to carry the
pod's ``autoUpgrade`` block forward (lifecycle rule 1). That regression is what
#3567's adversarial review caught in the seed; the same shape of bug is pinned
here for the writer, including the underlying ``bot_policy`` mechanism so a
change in that mechanism fails loudly instead of silently making the
compensating write wrong.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_auto_upgrade  # noqa: E402
import oc_model  # noqa: E402

LADDER = {
    "tier1": {"models": ["anthropic/claude-opus-4-8"]},
    "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
    "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
    "tier0": {"models": ["openai/gpt-5-mini"]},
}
#: What the apply-recommendation / bulk-apply routes send: exactly one tier.
ONE_TIER = {"tier2": {"models": ["openai/gpt-5-mini"]}}

POD_AUTO_UPGRADE = {"enabled": True, "applyDay": "tuesday", "minVisibleDays": 7}


@pytest.fixture
def fake_bot_env(tmp_path, monkeypatch):
    """A bot home with openclaw.json but deliberately NO evolve-tiers.json."""
    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {
            "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
        }}},
    }))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        oc_model, "_preserve_write", lambda data, p: p.write_text(json.dumps(data))
    )
    return {"oc_json": oc_json, "tiers_path": home / ".openclaw" / "evolve-tiers.json"}


def _write(env, updates: dict) -> dict:
    oc_model.json_full_config_set(
        bot="b", updates=updates, oc_json_path=env["oc_json"],
    )
    return json.loads(env["tiers_path"].read_text())


def _no_legacy_tier_keys(blob) -> bool:
    """True when no ``tierN`` key appears anywhere in ``blob``, at any depth."""
    if isinstance(blob, dict):
        if any(isinstance(k, str) and k.startswith("tier") and k[4:].isdigit()
               for k in blob):
            return False
        return all(_no_legacy_tier_keys(v) for v in blob.values())
    if isinstance(blob, list):
        return all(_no_legacy_tier_keys(v) for v in blob)
    return True


# ── create: never born legacy ─────────────────────────────────────────────────

@pytest.mark.parametrize("tiers,expect_rungs", [
    (LADDER, {"haiku-class", "sonnet-class", "opus-class", "judge-class"}),
    (ONE_TIER, {"sonnet-class"}),
])
def test_absent_file_is_created_on_rungs_not_legacy(fake_bot_env, tiers, expect_rungs):
    assert not fake_bot_env["tiers_path"].exists()
    written = _write(fake_bot_env, {"tiers": tiers})

    assert "tiers" not in written, "a fresh file must never carry the legacy key"
    assert _no_legacy_tier_keys(written), f"legacy tierN key survived: {written}"
    assert {r["id"] for r in written["rungs"]} == expect_rungs
    assert all(r.get("costClass") for r in written["rungs"]), "costClass backfilled"
    assert written["roles"], "roles map populated"


def test_created_file_reads_as_new_shape_to_the_gateway(fake_bot_env):
    """The whole point: the plugin's legacy synthesize-at-load never fires."""
    written = _write(fake_bot_env, {"tiers": LADDER})
    assert oc_model._file_is_new_shape(written)
    # And the legacy read projection still renders every tier the operator set,
    # so nothing on the read side notices the shape change.
    assert oc_model.synthesize_legacy_tiers(written) == LADDER


def test_file_with_only_siblings_is_created_on_rungs(fake_bot_env):
    """A file that exists but defines no tiers has nothing to preserve."""
    fake_bot_env["tiers_path"].write_text(json.dumps({"cascade": {"enabled": True}}))
    written = _write(fake_bot_env, {"tiers": ONE_TIER})
    assert "tiers" not in written
    assert {r["id"] for r in written["rungs"]} == {"sonnet-class"}
    assert written["cascade"] == {"enabled": True}, "sibling keys survive"


def test_file_with_an_empty_legacy_tiers_dict_is_converted_not_topped_up(fake_bot_env):
    """``{"tiers": {}}`` defines no allocations, so there is nothing to
    preserve — and the now-meaningless key must not survive alongside rungs
    (the gateway loader ignores ``tiers`` whenever ``rungs`` is present, so a
    leftover would be exactly the mixed-shape pollution normalize exists for)."""
    fake_bot_env["tiers_path"].write_text(json.dumps({"tiers": {}}))
    written = _write(fake_bot_env, {"tiers": ONE_TIER})
    assert "tiers" not in written
    assert {r["id"] for r in written["rungs"]} == {"sonnet-class"}


def test_created_shape_equals_what_the_migrator_would_produce(fake_bot_env):
    """Behaviour-preservation, pinned rather than asserted in prose.

    The create path uses oc_model's own ``apply_tiers_update_new_shape`` rather
    than importing ``migrate_model_roles`` across the package boundary (this
    module runs as the bot user under the analyzer's sys.path). This test is
    what keeps that local transform honest: the file a bot is born with must
    equal what ``evolve-admin migrate-model-roles --apply`` would have written
    for the same allocations.
    """
    from evolve_admin.migrate_model_roles import migrate_evolve_tiers

    written = _write(fake_bot_env, {"tiers": LADDER})
    expected, _ = migrate_evolve_tiers({"tiers": dict(LADDER)})
    expected.pop("tiers", None)

    assert written["rungs"] == expected["rungs"]
    assert written["roles"] == expected["roles"]


@pytest.mark.parametrize("order", list(itertools.permutations(LADDER)))
def test_created_shape_does_not_depend_on_tier_key_order(fake_bot_env, order):
    """The equality above pins ONE dict ordering, and a single ordering is not
    the property we want: array position in ``rungs[]`` IS the cost rank that
    ``primary_bot.rung_ranks`` reads. Folding in payload order put judge-class
    ahead of sonnet-class for 12 of these 24 permutations (inert today only
    because judge is excluded from the ladder roles). The create path folds in
    a canonical order instead, so every permutation lands the same file — and
    the same file the migrator produces."""
    from evolve_admin.migrate_model_roles import migrate_evolve_tiers

    payload = {k: LADDER[k] for k in order}
    written = _write(fake_bot_env, {"tiers": payload})
    expected, _ = migrate_evolve_tiers({"tiers": dict(payload)})

    assert written["rungs"] == expected["rungs"]
    assert written["roles"] == expected["roles"]


def test_per_tier_fallbacks_are_folded_into_the_cluster_not_dropped(fake_bot_env):
    """The rungs shape has no separate fallback slot. Before this was handled,
    the create path wrote only ``models`` — so models an operator sent through
    the (unvalidated) tiers PUT vanished, and the post-write "did it land?"
    guard only checks ``models``, so the loss was invisible."""
    written = _write(fake_bot_env, {"tiers": {
        "tier2": {"models": ["a/b"], "fallbacks": ["c/d", "a/b"]},
    }})
    sonnet = next(r for r in written["rungs"] if r["id"] == "sonnet-class")
    assert sonnet["models"] == ["a/b", "c/d"], "fallbacks fold in, dedup-preserving"


def test_per_tier_daily_cap_is_lifted_to_role_caps(fake_bot_env):
    written = _write(fake_bot_env, {"tiers": {
        "tier1": {"models": ["a/b"], "maxPerDayPerBot": 5},
    }})
    assert written["roleCaps"] == {"power": {"maxPerDayPerBot": 5}}


def test_an_explicit_role_caps_update_wins_over_the_lifted_cap(fake_bot_env):
    written = _write(fake_bot_env, {
        "tiers": {"tier1": {"models": ["a/b"], "maxPerDayPerBot": 5}},
        "roleCaps": {"power": {"maxPerDayPerBot": 99}},
    })
    assert written["roleCaps"] == {"power": {"maxPerDayPerBot": 99}}


def test_a_tier_with_no_models_does_not_mint_an_empty_rung(fake_bot_env):
    """On a CREATE there is nothing to clear, so an empty list is not an edit.
    Materializing a rung for it would flip the bot to Custom — and orphan it
    from the pod's auto-upgrade toggle — over a no-op. The migrator drops it."""
    written = _write(fake_bot_env, {"tiers": {"tier2": {"models": []}}})
    assert "rungs" not in written and "tiers" not in written


def test_non_dict_legacy_key_is_dropped_rather_than_left_beside_rungs(fake_bot_env):
    """A junk ``tiers`` value carries no allocations to fold, but it is still
    the deprecated key sitting next to ``rungs`` — and the gateway loader
    ignores ``tiers`` whenever ``rungs`` is present. It has to go, or the file
    stays permanently mixed: ``migrate-model-roles`` reports no change for a
    non-dict ``tiers``, so nothing else would ever clean it up."""
    for junk in ("junk", None, [1, 2]):
        fake_bot_env["tiers_path"].write_text(json.dumps({"tiers": junk}))
        written = _write(fake_bot_env, {"tiers": ONE_TIER})
        assert "tiers" not in written, f"junk={junk!r} survived beside rungs"
        assert {r["id"] for r in written["rungs"]} == {"sonnet-class"}


# ── preserve: an existing legacy file is still left alone ─────────────────────

def test_existing_legacy_file_is_still_preserved(fake_bot_env):
    """Preserve-vs-create is the whole distinction — the preserve half must not
    have moved. Half-migrating on a partial write is what this branch refuses."""
    fake_bot_env["tiers_path"].write_text(json.dumps({
        "tiers": {"tier3": {"models": ["anthropic/claude-haiku-4-5"]}},
    }))
    written = _write(fake_bot_env, {"tiers": {"tier3": {"models": ["x/y"]}}})
    assert written["tiers"]["tier3"]["models"] == ["x/y"]
    assert "rungs" not in written


def test_empty_tiers_payload_does_not_create_a_file_with_rungs(fake_bot_env):
    written = _write(fake_bot_env, {"tiers": {}})
    assert "tiers" not in written and "rungs" not in written


def test_uninterpretable_payload_does_not_mint_legacy(fake_bot_env, capsys):
    """A payload naming no known tier writes nothing — it must NOT fall back to
    the legacy key, which is the shape this branch exists to stop minting."""
    written = _write(fake_bot_env, {"tiers": {"tier9": {"models": ["x/y"]}}})
    assert "tiers" not in written and "rungs" not in written
    assert "no known tier" in capsys.readouterr().err


# ── lifecycle rule 1: the pod's auto-upgrade posture rides along ──────────────

def test_pod_auto_upgrade_carried_when_the_write_creates_rungs(fake_bot_env):
    written = _write(
        fake_bot_env, {"tiers": ONE_TIER, "podAutoUpgrade": POD_AUTO_UPGRADE},
    )
    assert written["autoUpgrade"] == POD_AUTO_UPGRADE


def test_pod_auto_upgrade_key_is_never_persisted(fake_bot_env):
    """It is advisory transport, not a config field."""
    written = _write(
        fake_bot_env, {"tiers": ONE_TIER, "podAutoUpgrade": POD_AUTO_UPGRADE},
    )
    assert oc_model.POD_AUTO_UPGRADE_KEY not in written


def test_pod_auto_upgrade_not_carried_onto_a_preserved_legacy_file(fake_bot_env):
    """That bot stays Use-pod-defaults (no rungs), so it still inherits the pod
    toggle directly — writing a per-bot block would be noise at best."""
    fake_bot_env["tiers_path"].write_text(json.dumps({"tiers": {"tier3": {"models": ["a/b"]}}}))
    written = _write(
        fake_bot_env,
        {"tiers": {"tier3": {"models": ["x/y"]}}, "podAutoUpgrade": POD_AUTO_UPGRADE},
    )
    assert "autoUpgrade" not in written


def test_pod_auto_upgrade_does_not_overwrite_a_custom_bots_own_block(fake_bot_env):
    """A new-shape file is already Custom and owns its toggle — the fold path
    must not reach in and reset it to the pod's."""
    fake_bot_env["tiers_path"].write_text(json.dumps({
        "rungs": [{"id": "sonnet-class", "models": ["a/b"], "costClass": "medium"}],
        "roles": {"standard": "sonnet-class"},
        "autoUpgrade": {"enabled": False},
    }))
    written = _write(
        fake_bot_env, {"tiers": ONE_TIER, "podAutoUpgrade": POD_AUTO_UPGRADE},
    )
    assert written["autoUpgrade"] == {"enabled": False}


def test_explicit_auto_upgrade_update_still_wins_over_the_carry(fake_bot_env):
    written = _write(fake_bot_env, {
        "tiers": ONE_TIER,
        "podAutoUpgrade": POD_AUTO_UPGRADE,
        "autoUpgrade": {"enabled": False},
    })
    assert written["autoUpgrade"]["enabled"] is False
    assert written["autoUpgrade"]["applyDay"] == "tuesday", "pod knobs still merged"


# ── the mechanism the carry compensates for ───────────────────────────────────

def test_custom_bot_does_not_inherit_the_pod_enabled_flag():
    """Pinned independently of the writer.

    This is the mechanism that makes the carry load-bearing: if ``bot_policy``
    ever starts inheriting the pod's ``enabled`` for a Custom bot, the
    compensating write becomes unnecessary and can be dropped DELIBERATELY —
    this test failing is the signal. Without it the coupling is invisible.
    """
    pod = model_auto_upgrade.pod_policy({"models": {"autoUpgrade": {"enabled": True}}})
    assert pod.enabled is True

    inherits = model_auto_upgrade.bot_policy(pod, {}, custom=False)
    assert inherits.enabled is True and inherits.enabled_source == "pod"

    orphaned = model_auto_upgrade.bot_policy(pod, {}, custom=True)
    assert orphaned.enabled is False
    assert orphaned.enabled_source == "code-default"


def test_created_file_keeps_the_pod_auto_upgrade_posture_end_to_end(fake_bot_env):
    """The regression this whole carry exists to prevent, measured the way an
    operator would feel it: pod says auto-upgrade on, bot keeps riding it."""
    written = _write(
        fake_bot_env, {"tiers": ONE_TIER, "podAutoUpgrade": {"enabled": True}},
    )
    # The write made the bot Custom — that is the trigger for the hazard.
    # ``primary_bot.bot_has_custom_tiers`` decides Custom purely on this.
    assert isinstance(written.get("rungs"), list) and written["rungs"]

    pod = model_auto_upgrade.pod_policy({"models": {"autoUpgrade": {"enabled": True}}})
    resolved = model_auto_upgrade.bot_policy(pod, written, custom=True)
    assert resolved.enabled is True, (
        "a freshly-created Custom bot silently stopped riding the latest "
        "model version — lifecycle rule 1 regression"
    )
