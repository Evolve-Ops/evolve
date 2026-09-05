"""tests/test_bot_tier_mode_helpers.py — per-bot Use-defaults/Custom helpers.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 5. The per-bot
toggle decides Custom-vs-default from the bot's RAW evolve-tiers.json (presence
of a non-empty ``rungs`` array), and Customize seeds a per-bot override from the
fully-merged catalog. These pin the three helpers in primary_bot:

  - read_bot_tiers_doc — returns the raw per-bot doc ({} when absent).
  - bot_has_custom_tiers — True iff the raw doc carries non-empty rungs.
  - materialize_bot_tier_override — the merged rungs/roles seed (defaults folded).

No real bot/user names appear; placeholders only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import primary_bot  # noqa: E402


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    """Point the bot tiers-path resolver at a tmp file so no pwd lookup runs."""
    tiers_path = tmp_path / "evolve-tiers.json"
    monkeypatch.setattr(
        primary_bot, "_bot_evolve_tiers_path",
        lambda network, bot_id: tiers_path,
    )
    return {"tiers_path": tiers_path, "network": {"bots": {"a_bot": {}}}}


# ── read_bot_tiers_doc / bot_has_custom_tiers ─────────────────────────────────


def test_absent_file_reads_use_defaults(bot_env):
    """A bot with no tiers file is Use-defaults (empty raw doc, no rungs)."""
    assert not bot_env["tiers_path"].exists()
    assert primary_bot.read_bot_tiers_doc(bot_env["network"], "a_bot") == {}
    assert primary_bot.bot_has_custom_tiers(bot_env["network"], "a_bot") is False


def test_file_without_rungs_is_use_defaults(bot_env):
    """A tiers file that carries sibling config (cascade) but no rungs still
    reads as Use-defaults — the toggle keys on rungs presence, not file
    presence."""
    bot_env["tiers_path"].write_text(json.dumps({"cascade": {"enabled": True}}))
    assert primary_bot.bot_has_custom_tiers(bot_env["network"], "a_bot") is False


def test_empty_rungs_array_is_use_defaults(bot_env):
    """An explicit empty rungs array (the Reset shape) is Use-defaults."""
    bot_env["tiers_path"].write_text(json.dumps({"rungs": [], "roles": {}}))
    assert primary_bot.bot_has_custom_tiers(bot_env["network"], "a_bot") is False


def test_nonempty_rungs_is_custom(bot_env):
    """A non-empty rungs array means the bot defines its own tiers — Custom."""
    bot_env["tiers_path"].write_text(json.dumps({
        "rungs": [{"id": "low-class", "models": ["prov_a/m-small"]}],
        "roles": {"fast": "low-class"},
    }))
    assert primary_bot.bot_has_custom_tiers(bot_env["network"], "a_bot") is True


# ── materialize_bot_tier_override ─────────────────────────────────────────────


def test_materialize_seeds_from_defaults_for_inheriting_bot(bot_env):
    """An inheriting bot (no per-bot doc) materializes from the code/pod merge —
    so the seed carries the full default ladder (every role resolves)."""
    seed = primary_bot.materialize_bot_tier_override(bot_env["network"], "a_bot")
    assert isinstance(seed.get("rungs"), list) and seed["rungs"], (
        "seed must carry the merged rung ladder"
    )
    assert isinstance(seed.get("roles"), dict) and seed["roles"], (
        "seed must carry the role map"
    )
    # All five roles resolve from the default catalog.
    for role in ("fast", "standard", "power", "max"):
        assert role in seed["roles"], f"seed roles missing {role}"


def test_materialize_folds_pod_layer(bot_env):
    """A pod ``models`` layer that adds a rung is folded into the seed (the
    merge is DEFAULT ← pod ← bot)."""
    network = {
        "bots": {"a_bot": {}},
        "models": {
            "rungs": [{"id": "fast-class", "costClass": "low", "models": ["prov_z/extra"]}],
            "roles": {"fast": "fast-class"},
        },
    }
    seed = primary_bot.materialize_bot_tier_override(network, "a_bot")
    # The pod's role override wins for fast.
    assert seed["roles"]["fast"] == "fast-class"
    rung_ids = {r.get("id") for r in seed["rungs"] if isinstance(r, dict)}
    assert "fast-class" in rung_ids


# ── pod_default_catalog_view (POD editor read) ────────────────────────────────


def test_pod_view_no_pod_layer_all_default():
    """With no pod ``models``, every role's layer is ``default`` and
    podConfigured is False (nothing to reset)."""
    view = primary_bot.pod_default_catalog_view({"bots": {}})
    assert view["podConfigured"] is False
    for role in ("fast", "standard", "power", "max"):
        assert view["roleLayers"][role] == "default", role
    assert isinstance(view["rungs"], list) and view["rungs"]
    assert isinstance(view["roles"], dict) and view["roles"]


def test_pod_view_pod_override_flags_pod_layer():
    """A pod ``models`` layer that changes a role's cluster flips that role's
    layer to ``pod`` and sets podConfigured True; untouched roles stay
    ``default``."""
    network = {
        "bots": {},
        "models": {
            "rungs": [{"id": "std-class", "costClass": "medium",
                       "models": ["prov_a/std", "prov_b/std"]}],
            "roles": {"standard": "std-class"},
        },
    }
    view = primary_bot.pod_default_catalog_view(network)
    assert view["podConfigured"] is True
    assert view["roleLayers"]["standard"] == "pod"
    # Roles the pod layer didn't touch keep falling through to the code default.
    assert view["roleLayers"]["fast"] == "default"


# ── roleDefaultModels: the POD picker's RECOMMENDED set (Addendum 6) ──────────
#
# The POD editor edits the pod's tier definitions, so its "recommended" set is
# the layer ABOVE the pod — the code DEFAULT_MODEL_CATALOG cluster (what "Reset
# to Evolve defaults" would put there), NOT the defaults ← pod cluster (that is
# what's already in the editor — recommending it would be circular). The picker
# stars these and greys the pod's current models. Credential-matched when a
# credentialed set is given.


def test_role_default_models_is_code_default_not_pod_merged():
    # The pod OVERRIDES standard to a cheaper, single-provider cluster. The
    # editor's recommended set for standard must still be the CODE default
    # cluster (the "Reset to Evolve defaults" target), independent of the pod
    # override that is already in the editor.
    network = {
        "bots": {},
        "models": {
            "rungs": [{"id": "std-class", "costClass": "medium",
                       "models": ["prov_a/std"]}],
            "roles": {"standard": "std-class"},
        },
    }
    view = primary_bot.pod_default_catalog_view(network)
    cat_default = primary_bot.merge_model_catalog({}, {})
    std_rung = primary_bot._resolve_role_to_rung(cat_default, "standard")
    code_default_std = primary_bot._rung_models(cat_default, std_rung)
    # The recommended set is the CODE default cluster ...
    assert view["roleDefaultModels"]["standard"] == code_default_std
    # ... NOT the pod-merged cluster (which is what's already in the editor).
    pod_std_rung = view["roles"]["standard"]
    pod_std_models = next(
        (r["models"] for r in view["rungs"] if r["id"] == pod_std_rung), [],
    )
    assert view["roleDefaultModels"]["standard"] != pod_std_models


def test_role_default_models_present_for_every_role():
    # Every role carries a roleDefaultModels list (the picker reads it per tier).
    view = primary_bot.pod_default_catalog_view({"bots": {}})
    for role in ("fast", "standard", "power", "max"):
        assert isinstance(view["roleDefaultModels"][role], list), role
        # With no credential filter (None) the full code-default cluster shows.
        assert view["roleDefaultModels"][role], role


def test_role_default_models_credential_matched():
    # The recommended (code-default) cluster is filtered to the pod's
    # credentialed providers, so the picker never stars an unrunnable model.
    # Pick a role whose code-default cluster spans >1 provider, derive that
    # cluster's providers from the data (no provider literals), then keep only
    # one of them and assert the recommended set narrows to it.
    cat_default = primary_bot.merge_model_catalog({}, {})
    target_role, target_provider = None, None
    for role in ("fast", "standard", "power", "max"):
        rung = primary_bot._resolve_role_to_rung(cat_default, role)
        models = primary_bot._rung_models(cat_default, rung) if rung else []
        provs = {primary_bot.provider_of(m) for m in models}
        provs.discard(None)
        if len(provs) >= 2:
            target_role = role
            target_provider = sorted(provs)[0]
            break
    assert target_role, "expected a code-default role cluster spanning >1 provider"

    filtered = primary_bot.pod_default_catalog_view(
        {"bots": {}}, credentialed_providers={target_provider},
    )
    rec = filtered["roleDefaultModels"][target_role]
    assert rec, "credentialed provider should keep at least one recommendation"
    assert all(primary_bot.provider_of(m) == target_provider for m in rec)


# ── pod_default_catalog_view: DISPLAYED rung credential-matching ──────────────
#
# The default card must be credential-honest like the picker: each tier shows
# only models from providers the pod holds a key for. DEFAULT_MODEL_CATALOG and
# the gateway resolution are unchanged — this is a display filter. Every rung id
# stays present (an empty models[] is the JS zero-credential signal); a None
# credentialed set is fail-open (full cluster). No provider-name literals —
# providers are derived from the data.


def _view_role_models(view, role):
    """Resolve a role to its displayed rung's models in a catalog view."""
    entry = (view.get("roles") or {}).get(role)
    rung_id = entry if isinstance(entry, str) else (entry or {}).get("rung")
    for r in (view.get("rungs") or []):
        if r.get("id") == rung_id:
            return r.get("models") or []
    return []


def _a_multiprovider_ladder_role():
    """A ladder role whose code-default cluster spans >1 provider + its providers
    (derived from the data, no literals). Used to exercise the display filter."""
    cat_default = primary_bot.merge_model_catalog({}, {})
    for role in ("fast", "standard", "power", "max"):
        rung = primary_bot._resolve_role_to_rung(cat_default, role)
        models = primary_bot._rung_models(cat_default, rung) if rung else []
        provs = sorted({p for m in models if (p := primary_bot.provider_of(m))})
        if len(provs) >= 2:
            return role, provs
    raise AssertionError("expected a code-default ladder cluster spanning >1 provider")


def test_pod_view_rungs_credential_matched_to_single_provider():
    # Credential the pod to ONE provider → the displayed tier narrows to only
    # that provider's models (the bug: it used to show all four providers raw).
    role, provs = _a_multiprovider_ladder_role()
    target = provs[0]
    view = primary_bot.pod_default_catalog_view(
        {"bots": {}}, credentialed_providers={target},
    )
    shown = _view_role_models(view, role)
    assert shown, "the credentialed provider should keep at least one model"
    assert all(primary_bot.provider_of(m) == target for m in shown)


def test_pod_view_rungs_widen_when_a_provider_is_added():
    # Adding a second credentialed provider makes its model(s) appear in the tier
    # (the catalog is unchanged — the filter just un-narrows).
    role, provs = _a_multiprovider_ladder_role()
    p0, p1 = provs[0], provs[1]
    one = _view_role_models(
        primary_bot.pod_default_catalog_view({"bots": {}}, credentialed_providers={p0}),
        role,
    )
    two = _view_role_models(
        primary_bot.pod_default_catalog_view(
            {"bots": {}}, credentialed_providers={p0, p1},
        ),
        role,
    )
    assert {primary_bot.provider_of(m) for m in one} == {p0}
    assert {primary_bot.provider_of(m) for m in two} == {p0, p1}
    assert len(two) > len(one)


def test_pod_view_rungs_all_empty_when_no_credentialed_provider():
    # An empty credentialed set → every rung id is STILL present (resolution +
    # the editor depend on it) but every models[] is empty (the zero-credential
    # signal the JS empty-state reads).
    view = primary_bot.pod_default_catalog_view(
        {"bots": {}}, credentialed_providers=set(),
    )
    assert view["rungs"], "rung ids stay present even with no credentialed provider"
    for r in view["rungs"]:
        assert r.get("models") == [], r.get("id")
    for role in ("fast", "standard", "power", "max"):
        assert _view_role_models(view, role) == [], role


def test_pod_view_rungs_full_cluster_when_credentials_unknown():
    # credentialed_providers=None → fail-open: the full code-default cluster
    # shows (unchanged behavior for a reader that can't see credentials).
    view = primary_bot.pod_default_catalog_view({"bots": {}})
    cat_default = primary_bot.merge_model_catalog({}, {})
    for role in ("fast", "standard", "power", "max"):
        rung = primary_bot._resolve_role_to_rung(cat_default, role)
        full = primary_bot._rung_models(cat_default, rung)
        assert _view_role_models(view, role) == full, role


def test_pod_view_filter_does_not_change_role_layer_provenance():
    # Provenance is computed on the UNFILTERED merge, so credential-filtering the
    # displayed rungs must not move a role's layer. A pod override flips
    # standard→pod; the layer stays pod even when filtered to one provider.
    network = {
        "bots": {},
        "models": {
            "rungs": [{"id": "std-class", "costClass": "medium",
                       "models": ["prov_a/std", "prov_b/std"]}],
            "roles": {"standard": "std-class"},
        },
    }
    unfiltered = primary_bot.pod_default_catalog_view(network)
    filtered = primary_bot.pod_default_catalog_view(
        network, credentialed_providers={"prov_a"},
    )
    assert unfiltered["roleLayers"] == filtered["roleLayers"]
    assert filtered["roleLayers"]["standard"] == "pod"
    assert filtered["roleLayers"]["fast"] == "default"


# ── validate_pod_default_catalog (structure + resolvability) ──────────────────


def _diverse_catalog():
    """A minimal valid pod default with a two-provider standard chain.
    No real model names — placeholders."""
    return {
        "rungs": [
            {"id": "fast-class", "models": ["prov_a/fast"]},
            {"id": "std-class", "models": ["prov_a/std", "prov_b/std"]},
            {"id": "power-class", "models": ["prov_a/power"]},
            {"id": "max-class", "models": ["prov_a/max"]},
        ],
        "roles": {
            "fast": "fast-class",
            "standard": "std-class",
            "power": "power-class",
            "max": "max-class",
        },
    }


def test_validate_accepts_diverse_catalog():
    assert primary_bot.validate_pod_default_catalog(_diverse_catalog()) is None


def test_validate_rejects_empty_rungs():
    err = primary_bot.validate_pod_default_catalog({"rungs": [], "roles": {}})
    assert err and "rungs" in err


def test_validate_accepts_single_provider_candidate():
    """A single-provider candidate (every rung from one provider, e.g. a
    credential-matched anthropic-only pod) saves — the former judge
    provider-diversity clause is gone with the judge role (judge-role
    collapse §5.4); only the non-empty ladder checks apply."""
    cat = _diverse_catalog()
    for rung in cat["rungs"]:
        rung["models"] = [f"prov_a/{rung['id']}"]
    assert primary_bot.validate_pod_default_catalog(cat) is None


def test_validate_ignores_stale_judge_role_entry():
    """Read-compat: a candidate still carrying the old structured judge
    entry is not an error — the key is inert."""
    cat = _diverse_catalog()
    cat["roles"]["judge"] = {"rung": "std-class", "provider": "not-standard"}
    assert primary_bot.validate_pod_default_catalog(cat) is None


def test_validate_rejects_empty_ladder_role():
    """A ladder role resolving to an empty cluster is rejected."""
    cat = _diverse_catalog()
    for rung in cat["rungs"]:
        if rung["id"] == "power-class":
            rung["models"] = []
    err = primary_bot.validate_pod_default_catalog(cat)
    assert err and "power" in err


# ── bot_redundant_vs_default (redundant-config advisory) ──────────────────────


def test_redundant_flags_custom_bot_equal_to_default(bot_env):
    """A Custom bot whose tiers resolve equal-after-merge to the code/pod
    default is flagged redundant."""
    # Seed the bot with the materialized default — by construction this
    # resolves identically to the no-override merge.
    seed = primary_bot.materialize_bot_tier_override(bot_env["network"], "a_bot")
    bot_env["tiers_path"].write_text(json.dumps(seed))
    assert primary_bot.bot_redundant_vs_default(bot_env["network"], "a_bot") is True


def test_redundant_not_flagged_for_genuinely_custom_bot(bot_env):
    """A bot whose tiers differ from the default is NOT flagged."""
    bot_env["tiers_path"].write_text(json.dumps({
        "rungs": [{"id": "fast-class", "models": ["prov_custom/only"]}],
        "roles": {
            "fast": "fast-class", "standard": "fast-class",
            "power": "fast-class", "max": "fast-class",
        },
    }))
    assert primary_bot.bot_redundant_vs_default(bot_env["network"], "a_bot") is False


def test_redundant_not_flagged_for_use_defaults_bot(bot_env):
    """A Use-defaults bot (no rungs) is never flagged — nothing to reset."""
    assert not bot_env["tiers_path"].exists()
    assert primary_bot.bot_redundant_vs_default(bot_env["network"], "a_bot") is False
