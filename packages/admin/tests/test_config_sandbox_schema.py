"""Schema integrity tests for the config sandbox.

The schema is the API contract — these tests guard against silently breaking
it by adding a duplicate path, wrong type_hint, or malformed target_path.
"""

from __future__ import annotations

from evolve_admin.config_sandbox import SCHEMA, Strength, by_path, by_store
from evolve_admin.config_sandbox.schema import Store


_VALID_TYPE_HINTS = {"bool", "int", "float", "string", "enum", "list", "dict", "doc"}
_VALID_STORES = {
    Store.NETWORK,
    Store.BETTER_ENGINE,
    Store.BOT_GUIDE,
    Store.OPENCLAW,
    Store.EVOLVE_TIERS,
    Store.SOUL,
    Store.AGENTS,
    Store.MANIFEST,
}


def test_paths_are_unique():
    paths = [e.path for e in SCHEMA]
    assert len(paths) == len(set(paths)), (
        f"duplicate paths in SCHEMA: "
        f"{[p for p in paths if paths.count(p) > 1]}"
    )


def test_type_hints_are_known():
    bad = [(e.path, e.type_hint) for e in SCHEMA if e.type_hint not in _VALID_TYPE_HINTS]
    assert not bad, f"unknown type_hint values: {bad}"


def test_stores_are_known():
    bad = [(e.path, e.store) for e in SCHEMA if e.store not in _VALID_STORES]
    assert not bad, f"unknown store values: {bad}"


def test_strength_is_enum():
    bad = [e.path for e in SCHEMA if not isinstance(e.strength, Strength)]
    assert not bad, f"non-Strength entries: {bad}"


def test_target_path_format():
    """target_path must be either '<file>::<dotted.key>' or '<filename>' (doc)."""
    for e in SCHEMA:
        if e.type_hint == "doc":
            # whole-document; target_path is just a filename pattern
            assert "::" not in e.target_path, (
                f"doc target_path should not contain '::': {e.path} -> {e.target_path}"
            )
        else:
            assert "::" in e.target_path, (
                f"key-level target_path missing '::': {e.path} -> {e.target_path}"
            )
            file_part, key_part = e.target_path.split("::", 1)
            assert file_part, f"empty file part in {e.path}"
            assert key_part, f"empty key part in {e.path}"


def test_per_bot_flag_consistency():
    """per_bot flag should align with target_path containing <bot_id> when needed."""
    # Bot-side stores are inherently per-bot.
    bot_side = {Store.OPENCLAW, Store.EVOLVE_TIERS, Store.SOUL, Store.AGENTS,
                Store.MANIFEST, Store.BOT_GUIDE}
    for e in SCHEMA:
        if e.store in bot_side:
            assert e.per_bot, f"{e.path}: bot-side stores must have per_bot=True"


def test_lookups():
    # by_path round-trip
    for e in SCHEMA:
        assert by_path(e.path) is e

    # Unknown path returns None
    assert by_path("does.not.exist") is None

    # by_store returns matching entries
    network_keys = by_store(Store.NETWORK)
    assert len(network_keys) > 0
    assert all(e.store == Store.NETWORK for e in network_keys)


def test_strength_label():
    assert Strength.FREE.label == "free"
    assert Strength.ADVISORY.label == "advisory"
    assert Strength.SHIPPED_POLICY.label == "shipped-policy"


def test_schema_covers_known_categories():
    """Sanity: schema has entries from each of the seven backing stores
    that we said it would cover."""
    stores_present = {e.store for e in SCHEMA}
    # The stores we committed to in the spec:
    expected = {
        Store.NETWORK,
        Store.BETTER_ENGINE,
        Store.OPENCLAW,
        Store.EVOLVE_TIERS,
        Store.BOT_GUIDE,
        Store.SOUL,
        Store.AGENTS,
    }
    missing = expected - stores_present
    assert not missing, f"schema missing entries for stores: {missing}"


def test_shipped_policy_keys_have_descriptions():
    """Shipped-policy entries must explain WHY it's centrally tuned."""
    short = [
        (e.path, e.description)
        for e in SCHEMA
        if e.strength == Strength.SHIPPED_POLICY and len(e.description) < 30
    ]
    assert not short, (
        f"shipped-policy entries with thin descriptions: {short}"
    )


def test_cache_retention_entry_was_removed_phase4b():
    """The cacheRetention sandbox TunableKey entry was deleted in Phase 4b
    of the 2026-06 cost-cap normalization (spec:
    internal/spec-cost-caps-2026-06-05.md). The knob now lives in
    better-engine-config and is written via /api/arbiter/bot-setup. Pin
    the deletion so a future refactor doesn't accidentally resurrect
    the duplicate-source-of-truth bug."""
    assert by_path("openclaw.agents.defaults.models.cacheRetention") is None, (
        "cacheRetention TunableKey must stay removed post-Phase-4b — the "
        "knob's canonical home is better-engine-config.per_bot_cache_retention."
    )
    assert by_path("openclaw.agents.defaults.sessionBudgetCapUsd") is None, (
        "sessionBudgetCapUsd TunableKey must stay removed post-Phase-4b — "
        "lives at better-engine-config.per_bot_session_cost_cap_usd now."
    )
