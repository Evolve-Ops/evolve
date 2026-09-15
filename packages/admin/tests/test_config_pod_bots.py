"""Pod-bot existence helpers (`pod_bot_ids` / `is_pod_bot`).

The primary is recorded in `network.primary`, a SIBLING of `members` —
on current-schema pods it never appears in the members list. Any
"does this bot exist?" gate that scans only `members` therefore can
never match the primary; these helpers are the shared predicate that
consults both keys (the members-only scan in `evo.identity` made the
primary unclaimable through the whole identity surface).
"""

from evolve_admin.config import is_pod_bot, pod_bot_ids


def test_pod_bot_ids_sibling_schema_prepends_primary():
    network = {"primary": "evo", "members": ["schoolassistant"], "bots": {}}
    assert pod_bot_ids(network) == ["evo", "schoolassistant"]


def test_pod_bot_ids_legacy_primary_listed_in_members_no_dup():
    network = {"primary": "evolve", "members": ["evolve", "atlas"], "bots": {}}
    assert pod_bot_ids(network) == ["evolve", "atlas"]


def test_pod_bot_ids_resolves_role_flag_primary_without_top_level_field():
    # No top-level `primary` key: only the real primary_bot.primary_bot_id
    # resolver finds the role-flagged bot. This exercises the lazy import
    # for real — if the import silently degraded to network.get("primary"),
    # this would return just the members list.
    network = {"members": ["atlas"], "bots": {"evo": {"role": "primary"}}}
    assert pod_bot_ids(network) == ["evo", "atlas"]


def test_pod_bot_ids_no_primary_no_members():
    assert pod_bot_ids({}) == []


def test_is_pod_bot_accepts_primary_and_members():
    network = {"primary": "evo", "members": ["schoolassistant"], "bots": {}}
    assert is_pod_bot("evo", network)
    assert is_pod_bot("schoolassistant", network)
    assert not is_pod_bot("ghost", network)
    assert not is_pod_bot("", network)
