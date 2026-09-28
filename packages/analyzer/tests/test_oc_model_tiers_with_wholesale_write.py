"""A ``tiers`` edit sent WITH a wholesale ``rungs``/``roles`` write must merge.

#3566 follow-up to #3569, flagged by that PR's adversarial review.

``json_full_config_set`` handles two very different tier writes:

  - ``tiers``          — a legacy-shaped ``{tierN: {models}}`` PER-TIER edit,
    folded into (or converted onto) the file's rung clusters;
  - ``rungs``/``roles`` — a WHOLESALE REPLACE of the bot's whole rung set, the
    per-bot Use-defaults↔Custom toggle (spec §Addendum 5).

They used to run in that order, so a payload carrying BOTH had its per-tier
edit written first and then thrown away by the replace. The one file state that
survived did so by accident: the legacy branch left a ``tiers`` key behind that
``normalize_tiers_file_shape`` folded into the replacement rungs at save time.

The fix runs the wholesale replace FIRST, so the per-tier edit lands ON TOP of
the replacement set — which is what an operator sending both means, and what
the accidental legacy recovery effectively did.

**No in-repo caller sends both keys today** (verified in #3569), so this is a
latent defect in the single write path every tier change goes through, not a
live outage. The tests below pin all three file states (new-shape, legacy,
absent) crossed with a non-empty wholesale write and with the empty/reset one.
Two of the six are deliberate REGRESSION guards — the legacy file already
behaved correctly and must keep doing so.

Also pinned here, because the reorder moves them:
  - the collision guard now sees the REPLACEMENT rungs (more correct, and it
    must not start rejecting the tiers-only writes that used to succeed);
  - the lifecycle-rule-1 auto-upgrade carry, which used to be create-branch-only
    and is now keyed on the on-disk not-Custom → Custom transition, so the
    payload that reaches the fold branch via a same-payload ``rungs`` write
    still carries the pod's block;
  - the #3569 create/preserve/fold split, unchanged for a tiers-only write.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_auto_upgrade  # noqa: E402
import oc_model  # noqa: E402

#: An already-migrated bot: one rung, one role.
NEW_SHAPE_FILE = {
    "rungs": [{"id": "sonnet-class", "models": ["old/s"], "costClass": "medium"}],
    "roles": {"standard": "sonnet-class"},
}
#: A bot still on the deprecated shape (four of them across both pods, #3566).
LEGACY_FILE = {"tiers": {"tier2": {"models": ["old/s"]}}}

#: "Customize this bot" — a full replacement rung set.
WHOLESALE = {
    "rungs": [{"id": "haiku-class", "models": ["z/z"], "costClass": "low"}],
    "roles": {"fast": "haiku-class"},
}
#: "Reset to pod defaults" — exactly what routes_admin_config sends.
RESET = {"rungs": [], "roles": {}, "roleCaps": {}, "autoUpgrade": {}}
#: The per-tier edit riding along. tier2 → role ``standard`` → ``sonnet-class``.
TIER_EDIT = {"tier2": {"models": ["a/b"]}}

POD_AUTO_UPGRADE = {"enabled": True, "applyDay": "tuesday"}


@pytest.fixture
def env(tmp_path, monkeypatch):
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


def _seed(env, state: dict | None) -> None:
    if state is not None:
        env["tiers_path"].write_text(json.dumps(state))


def _write(env, updates: dict) -> dict:
    oc_model.json_full_config_set(bot="b", updates=updates, oc_json_path=env["oc_json"])
    return json.loads(env["tiers_path"].read_text())


def _primary(env) -> str | None:
    blob = json.loads(env["oc_json"].read_text())
    return blob["agents"]["defaults"]["model"].get("primary")


def _models_for(tiers_file: dict, tier_key: str) -> list[str]:
    """The tier's models as the gateway would resolve them, in EITHER shape."""
    return (oc_model.synthesize_legacy_tiers(tiers_file).get(tier_key) or {}).get(
        "models", []
    )


# ── the merge: 3 file states × 2 wholesale write kinds ───────────────────────

@pytest.mark.parametrize("state,label", [
    (NEW_SHAPE_FILE, "new-shape"),
    (LEGACY_FILE, "legacy"),
    (None, "absent"),
])
def test_tier_edit_survives_a_same_payload_wholesale_write(env, state, label):
    """The per-tier edit lands ON TOP of the replacement rung set."""
    _seed(env, state)
    after = _write(env, {"tiers": TIER_EDIT, **WHOLESALE})

    # The edit landed …
    assert _models_for(after, "tier2") == ["a/b"], f"tier2 edit lost on {label} file"
    # … and the wholesale replacement is still the file's rung set: the rung the
    # payload named is present, and the pre-write rung it replaced is gone.
    assert any(r["id"] == "haiku-class" for r in after["rungs"])
    assert after["roles"]["fast"] == "haiku-class"
    assert "old/s" not in json.dumps(after), "replaced rung's models survived"
    # openclaw.json's flat chain is recomputed from the merged result.
    assert _primary(env) == "a/b"


@pytest.mark.parametrize("state,label", [
    (NEW_SHAPE_FILE, "new-shape"),
    (LEGACY_FILE, "legacy"),
    (None, "absent"),
])
def test_tier_edit_survives_a_same_payload_reset_write(env, state, label):
    """Reset clears the bot's rung set; the tier edit still lands on the result.

    Pre-fix, the new-shape and absent cases ended with an EMPTY file — the
    create/fold wrote rungs and the reset's ``rungs: []`` then popped them, so
    the operator's edit and the bot's whole tier config vanished together.
    """
    _seed(env, state)
    after = _write(env, {"tiers": TIER_EDIT, **RESET})

    assert _models_for(after, "tier2") == ["a/b"], f"tier2 edit lost on {label} file"
    assert _primary(env) == "a/b"
    # Nothing of the pre-write rung set survives the reset.
    assert "old/s" not in json.dumps(after)


def test_legacy_file_plus_wholesale_is_unchanged_by_the_reorder(env):
    """REGRESSION GUARD — the one case that already worked, by accident.

    The legacy branch left a ``tiers`` key that ``normalize_tiers_file_shape``
    folded into the replacement rungs at save time. The reorder drops that key
    explicitly instead (it describes the PRE-replacement world), then folds the
    update on top — same result, and now on purpose. This assertion is the
    byte-for-byte output from before the change.
    """
    _seed(env, LEGACY_FILE)
    after = _write(env, {"tiers": TIER_EDIT, **WHOLESALE})
    assert after == {
        "rungs": [
            {"id": "haiku-class", "models": ["z/z"], "costClass": "low"},
            {"id": "sonnet-class", "models": ["a/b"], "costClass": "medium"},
        ],
        "roles": {"fast": "haiku-class", "standard": "sonnet-class"},
    }


def test_legacy_file_plus_reset_still_preserves_the_legacy_shape(env):
    """REGRESSION GUARD — a reset carries no rungs, so the file stays legacy.

    Preserve-not-half-migrate (#3569) still governs: an empty wholesale write
    leaves the file without rungs, so the tiers edit takes the PRESERVE branch
    and re-affirms the deprecated shape rather than converting behind the
    operator's back. ``migrate-model-roles`` remains the whole-file conversion.
    """
    _seed(env, LEGACY_FILE)
    after = _write(env, {"tiers": TIER_EDIT, **RESET})
    assert after == {"tiers": {"tier2": {"models": ["a/b"]}}}


def test_stale_on_disk_legacy_tiers_do_not_clobber_the_edit(env):
    """The file's OWN legacy allocations must not win over the payload's.

    Specific to the reorder: a legacy file gains ``rungs`` from the wholesale
    write, which makes it mixed-shape — and ``normalize_tiers_file_shape`` folds
    a legacy sibling into the rungs at SAVE time, i.e. after the update was
    folded. Without dropping the stale key first, the on-disk ``old/s`` would be
    written over the operator's ``a/b``.
    """
    _seed(env, {"tiers": {"tier2": {"models": ["old/s"]}, "tier3": {"models": ["old/h"]}}})
    after = _write(env, {"tiers": TIER_EDIT, **WHOLESALE})
    assert _models_for(after, "tier2") == ["a/b"]
    assert "tiers" not in after
    # The replaced set is authoritative: tier3's stale allocation does not come
    # back either — the operator replaced the whole rung set in this payload.
    assert "old/h" not in json.dumps(after)


#: A file polluted with BOTH shapes — what an old freshness "apply" wrote.
MIXED_FILE = {
    "rungs": [{"id": "sonnet-class", "models": ["r/s"], "costClass": "medium"}],
    "roles": {"standard": "sonnet-class"},
    "tiers": {"tier1": {"models": ["keep/o"]}, "tier3": {"models": ["keep/h"]}},
}


@pytest.mark.parametrize("wholesale", [
    {"roles": {}}, {"roles": None}, {"roleCaps": {}},
], ids=["empty-roles", "null-roles", "empty-rolecaps"])
def test_a_wholesale_key_that_replaces_NOTHING_does_not_drop_the_mixed_legacy(
    env, wholesale,
):
    """The stale-``tiers`` drop keys off a landed replacement, not key presence.

    An empty or junk ``rungs``/``roles`` clears (or is ignored) — it replaces
    nothing, so the file's rungs stand and its own legacy allocations are still
    the operator's current config, to be folded as they always were. Gating the
    drop on mere key presence destroyed them.
    """
    _seed(env, MIXED_FILE)
    after = _write(env, {"tiers": TIER_EDIT, **wholesale})
    assert _models_for(after, "tier2") == ["a/b"]
    assert _models_for(after, "tier1") == ["keep/o"]
    assert _models_for(after, "tier3") == ["keep/h"]


@pytest.mark.parametrize("rungs", [[], "junk", None], ids=["empty", "junk", "null"])
def test_clearing_rungs_on_a_mixed_file_falls_back_to_legacy_write_semantics(
    env, rungs,
):
    """A cleared ``rungs`` leaves the legacy key as the config, replaced wholesale.

    Not the stale-drop path: any non-list-or-empty value POPS the rungs, so the
    file is legacy-only by the time the tiers block runs and the PRESERVE branch
    handles it — and a legacy write has always replaced the whole ``tiers`` dict
    rather than merging into it (every real caller sends the full dict). The
    edit landing at all is the fix; pre-change it went into the rungs that the
    same payload had just popped, and vanished.
    """
    _seed(env, MIXED_FILE)
    after = _write(env, {"tiers": TIER_EDIT, "rungs": rungs})
    assert after["tiers"] == TIER_EDIT
    assert "rungs" not in after


def test_the_caller_updates_dict_is_never_mutated(env):
    """``json_full_config_set`` must not write back through ``updates``.

    The fold appends rungs and rewrites their ``models[]`` in place, so aliasing
    the payload's list/dict into the file would mutate the caller's own object.
    Only reachable since the reorder put the fold after the wholesale write, and
    only for PARTIAL payloads — a full ``rungs``+``roles`` write is replaced by
    ``canonicalize_catalog_rung_ids``'s fresh objects, which is the worst shape
    for a landmine (it hides in the uncommon case).
    """
    updates = {
        "tiers": TIER_EDIT,
        "rungs": [{"id": "haiku-class", "models": ["z/z"], "costClass": "low"}],
    }
    snapshot = json.dumps(updates, sort_keys=True)
    _write(env, updates)
    assert json.dumps(updates, sort_keys=True) == snapshot


@pytest.mark.parametrize("junk", [None, "junk", [], 7], ids=["null", "str", "list", "int"])
def test_a_junk_tiers_payload_does_not_raise(env, junk):
    """A non-dict ``tiers`` is ignored, not a 500.

    The per-bot tiers PUT does no body validation, so junk reaches the writer.
    The fold path has always tolerated it; the create path must too, and the
    reorder routes more payloads through create.
    """
    _seed(env, NEW_SHAPE_FILE)
    after = _write(env, {"tiers": junk, "rungs": [], "roles": {}})
    assert "tiers" not in after


# ── the collision guard now sees the replacement rungs ───────────────────────

def test_collision_is_detected_against_the_replacement_roles_map(env):
    """Two tiers folding to ONE replacement rung with different models → 409.

    Pre-fix the guard ran against the PRE-replacement roles map, where the two
    tiers resolved to different rungs — so the write "succeeded" while one edit
    silently vanished into the file the replace then discarded. It now runs
    against the map the fold will actually target.

    A roles-only replacement, because a full ``rungs``+``roles`` write is passed
    through ``canonicalize_catalog_rung_ids`` first, and that de-collides by
    construction (it splits a shared rung into per-role canonical ids).
    """
    _seed(env, NEW_SHAPE_FILE)
    with pytest.raises(ValueError) as exc:
        _write(env, {
            "tiers": {"tier2": {"models": ["a/b"]}, "tier3": {"models": ["c/d"]}},
            "roles": {"standard": "one-rung", "fast": "one-rung"},
        })
    assert str(exc.value).startswith(oc_model.RUNG_COLLISION_PREFIX)
    assert "one-rung" in str(exc.value)


def test_a_roles_only_replacement_cannot_steer_a_create_into_a_silent_clobber(env):
    """The collision guard must run even when the file has roles but no rungs.

    A roles-only replacement lands ahead of the fold, so a bot with no rungs
    goes down the CREATE path resolving through the payload's roles map —
    ``_ensure_role_and_rung`` follows the map first and creates the rung it
    names, so two tiers pointed at one rung collapse last-writer-wins and one
    edit disappears while the write reports success. The guard used to skip that
    state entirely (it is gated on rungs being present).
    """
    with pytest.raises(ValueError) as exc:
        _write(env, {
            "tiers": {"tier2": {"models": ["a/b"]}, "tier1": {"models": ["c/d"]}},
            "roles": {"standard": "sonnet-class", "power": "sonnet-class"},
        })
    assert str(exc.value).startswith(oc_model.RUNG_COLLISION_PREFIX)


def test_a_rungless_roles_map_on_disk_is_collision_checked_too(env):
    """The one tiers-ONLY payload class whose outcome this change alters.

    A file carrying ``roles`` but no ``rungs`` steers the create path through
    that map, so two tiers pointed at one rung collapse. Pre-change the write
    "succeeded" and the file came back with a single rung holding whichever tier
    folded last — the other edit gone, with no error anywhere. Refusing it is the
    same 409 the fold path has always returned for the same conflict.
    """
    _seed(env, {"roles": {"standard": "sonnet-class", "power": "sonnet-class"}})
    with pytest.raises(ValueError) as exc:
        _write(env, {"tiers": {"tier2": {"models": ["a/b"]},
                               "tier1": {"models": ["c/d"]}}})
    assert str(exc.value).startswith(oc_model.RUNG_COLLISION_PREFIX)


def test_a_wholesale_only_write_does_not_mutate_the_caller_dict_either(env):
    """Non-mutation holds for the payloads real callers DO send.

    A wholesale write on a legacy file leaves a mixed file, and
    ``normalize_tiers_file_shape`` folds the legacy sibling into the rungs at
    save time — through the caller's own list, pre-change. Copying on write
    closes that for every payload shape, not just the both-keys one.
    """
    _seed(env, LEGACY_FILE)
    updates = {"rungs": [{"id": "haiku-class", "models": ["z/z"], "costClass": "low"}]}
    snapshot = json.dumps(updates, sort_keys=True)
    _write(env, updates)
    assert json.dumps(updates, sort_keys=True) == snapshot


def test_canonicalized_wholesale_write_does_not_collide(env):
    """The full Customize payload cannot trip the guard the reorder exposed it to.

    ``canonicalize_catalog_rung_ids`` runs ahead of the replace and gives each
    role its own canonical rung, so pointing two roles at one rung in a
    ``rungs``+``roles`` payload is de-collided before the fold ever sees it.
    This is why moving the collision check onto the replacement set cannot start
    rejecting the "Customize this bot" write.
    """
    _seed(env, NEW_SHAPE_FILE)
    after = _write(env, {
        "tiers": {"tier2": {"models": ["a/b"]}, "tier3": {"models": ["c/d"]}},
        "rungs": [{"id": "one-rung", "models": ["z/z"], "costClass": "medium"}],
        "roles": {"standard": "one-rung", "fast": "one-rung"},
    })
    assert _models_for(after, "tier2") == ["a/b"]
    assert _models_for(after, "tier3") == ["c/d"]


def test_tiers_only_write_is_unaffected_by_the_reorder(env):
    """No ``rungs``/``roles`` key → the wholesale block is a no-op.

    This is the whole safety argument for the reorder: every real caller sends
    ``tiers`` alone, and for those payloads the file the fold (and its collision
    guard) sees is bit-for-bit what it saw before.
    """
    _seed(env, NEW_SHAPE_FILE)
    after = _write(env, {"tiers": TIER_EDIT})
    assert after == {
        "rungs": [{"id": "sonnet-class", "models": ["a/b"], "costClass": "medium"}],
        "roles": {"standard": "sonnet-class"},
    }


def test_tiers_only_write_on_a_mixed_file_still_normalizes(env):
    """``normalize_tiers_file_shape`` still folds-and-drops a stale sibling.

    The stale-key drop the reorder adds is gated on a same-payload wholesale
    write, so mixed-file healing on an ordinary tiers write is untouched.
    """
    _seed(env, {**NEW_SHAPE_FILE, "tiers": {"tier3": {"models": ["stale/h"]}}})
    after = _write(env, {"tiers": TIER_EDIT})
    assert "tiers" not in after
    assert _models_for(after, "tier3") == ["stale/h"]


# ── the #3569 create/preserve/fold split, unchanged ──────────────────────────

def test_create_branch_still_never_writes_a_legacy_tiers_key(env):
    """Absent file + tiers-only write → born on rungs/roles (#3569)."""
    after = _write(env, {"tiers": TIER_EDIT})
    assert "tiers" not in after
    assert after["rungs"] == [
        {"id": "sonnet-class", "models": ["a/b"], "costClass": "medium"}
    ]
    assert after["roles"]["standard"] == "sonnet-class"


def test_preserve_branch_still_refuses_to_half_migrate(env):
    """Legacy file + tiers-only write → stays legacy (#3569)."""
    _seed(env, LEGACY_FILE)
    after = _write(env, {"tiers": TIER_EDIT})
    assert after == {"tiers": {"tier2": {"models": ["a/b"]}}}


# ── lifecycle rule 1: the auto-upgrade carry follows the Custom flip ──────────

def test_pod_auto_upgrade_is_carried_when_a_wholesale_write_makes_the_bot_custom(env):
    """The carry must not be lost now that the payload reaches the fold branch.

    Pre-fix, an absent-file payload with both keys hit the CREATE branch and
    carried the pod's block; post-fix the wholesale write establishes the rungs
    first, so the same payload folds. Keying the carry on the on-disk
    not-Custom → Custom transition keeps it firing — otherwise the bot would
    become Custom and silently stop riding the latest model version
    (``bot_policy`` does not inherit the pod's ``enabled`` for a Custom bot).
    """
    after = _write(env, {
        "tiers": TIER_EDIT,
        **WHOLESALE,
        oc_model.POD_AUTO_UPGRADE_KEY: POD_AUTO_UPGRADE,
    })
    assert after["autoUpgrade"] == POD_AUTO_UPGRADE
    assert oc_model.POD_AUTO_UPGRADE_KEY not in after


def test_already_custom_bot_keeps_its_own_auto_upgrade_block(env):
    """No transition → no carry; the bot's own toggle is never reset."""
    _seed(env, {**NEW_SHAPE_FILE, "autoUpgrade": {"enabled": False}})
    after = _write(env, {
        "tiers": TIER_EDIT,
        **WHOLESALE,
        oc_model.POD_AUTO_UPGRADE_KEY: POD_AUTO_UPGRADE,
    })
    assert after["autoUpgrade"] == {"enabled": False}


def test_pod_auto_upgrade_is_carried_when_a_legacy_bot_becomes_custom(env):
    """The case the moved carry newly covers, and the reason it moved.

    A legacy-shaped bot is NOT Custom (``bot_has_custom_tiers`` reads ``rungs``),
    so a payload whose wholesale write gives it rungs flips it — and the carry
    used to be unreachable from the preserve/fold branches this payload takes.
    """
    _seed(env, LEGACY_FILE)
    after = _write(env, {
        "tiers": TIER_EDIT,
        **WHOLESALE,
        oc_model.POD_AUTO_UPGRADE_KEY: POD_AUTO_UPGRADE,
    })
    assert after["autoUpgrade"] == POD_AUTO_UPGRADE


def test_reset_plus_tier_edit_does_not_strand_the_bot_off_auto_upgrade(env):
    """The bot must not end Custom with auto-upgrade resolving to OFF.

    Asserted through ``model_auto_upgrade.bot_policy`` rather than on key
    absence, because key absence is exactly the failure: the reset's
    ``autoUpgrade: {}`` clears the block, the tier edit then re-creates
    ``rungs``, and a Custom bot with no block resolves to the CODE default
    (false) — silently the opposite of the pod, which is the whole regression
    class ``_carry_pod_auto_upgrade`` exists to prevent. Lifecycle rule 2 ("back
    to following the pod in full") is honoured by carrying the pod's block, not
    by leaving the key off a bot that is still Custom.
    """
    _seed(env, {**NEW_SHAPE_FILE, "autoUpgrade": {"enabled": True}})
    after = _write(env, {
        "tiers": TIER_EDIT,
        **RESET,
        oc_model.POD_AUTO_UPGRADE_KEY: POD_AUTO_UPGRADE,
    })
    custom = isinstance(after.get("rungs"), list) and bool(after["rungs"])
    assert custom, "the tier edit re-creates rungs, so the bot is Custom"
    pod = model_auto_upgrade.pod_policy({"models": {"autoUpgrade": POD_AUTO_UPGRADE}})
    resolved = model_auto_upgrade.bot_policy(pod, after, custom=custom)
    assert resolved.enabled is True
    assert resolved.apply_day == "tuesday"


def test_bare_reset_still_clears_auto_upgrade(env):
    """REGRESSION GUARD — lifecycle rule 2 for the payload routes actually send.

    ``routes_admin_config`` sends the reset with NO ``tiers`` key, so the file
    ends without ``rungs``; the bot is Use-pod-defaults again and follows the pod
    wholesale. The carry lives inside the tiers block and cannot fire here.
    """
    _seed(env, {**NEW_SHAPE_FILE, "autoUpgrade": {"enabled": True}})
    after = _write(env, dict(RESET))
    assert "autoUpgrade" not in after
    assert "rungs" not in after


def test_caller_supplied_auto_upgrade_still_wins_over_the_pod_carry(env):
    """An explicit ``autoUpgrade`` merges on top of the carried pod block."""
    after = _write(env, {
        "tiers": TIER_EDIT,
        **WHOLESALE,
        "autoUpgrade": {"enabled": False},
        oc_model.POD_AUTO_UPGRADE_KEY: POD_AUTO_UPGRADE,
    })
    assert after["autoUpgrade"]["enabled"] is False
    assert after["autoUpgrade"]["applyDay"] == "tuesday"


# ── roleCaps interaction with the moved block ────────────────────────────────

def test_explicit_role_caps_win_over_the_create_path_derivation(env):
    """The create branch defers to a wholesale ``roleCaps`` in the same payload.

    ``roleCaps`` moved ahead of the tiers block with the rest of the wholesale
    write; the create branch's ``"roleCaps" not in updates`` guard is what keeps
    it from overwriting, so ordering is immaterial — pinned because it now
    depends on that guard rather than on running last.
    """
    after = _write(env, {
        "tiers": {"tier1": {"models": ["a/b"], "maxPerDayPerBot": 5}},
        "roleCaps": {"power": {"maxPerDayPerBot": 99}},
    })
    assert after["roleCaps"] == {"power": {"maxPerDayPerBot": 99}}


def test_create_path_still_lifts_max_per_day_when_no_role_caps_sent(env):
    after = _write(env, {"tiers": {"tier1": {"models": ["a/b"], "maxPerDayPerBot": 5}}})
    assert after["roleCaps"] == {"power": {"maxPerDayPerBot": 5}}
