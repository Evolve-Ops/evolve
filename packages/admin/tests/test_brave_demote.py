"""tests/test_brave_demote.py — Brave demoted from pod-invariant to optional.

Operator decision (2026-06-24, LOCKED): demote Brave Search to an optional
integration, keep GitHub pod-invariant. Brave search is optional, especially
for the evo bot.

Covers the four requirements of the change:
  (a) the default pod-invariant set EXCLUDES brave (default-set test also lives
      in test_onboarding.py::test_keys_api_includes_pod_invariants_default);
  (b) the idempotent network.json migration strips brave ONLY from the exact
      old default ["github","brave"] — operator-customized lists are preserved;
  (c) the wizard's solicited-integration set derives from
      podInvariantIntegrations, so demoting brave silences the prompt;
  (d) a Brave key in openclaw.json (canonical OR legacy path) reads as active.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evolve_admin import config as cfg  # noqa: E402
from evolve_admin.web.credentials_oc import brave_key_from_oc_config  # noqa: E402


# ── (a) default set excludes brave ───────────────────────────────────────────


def test_default_pod_invariants_exclude_brave():
    assert "brave" not in cfg.DEFAULT_POD_INVARIANT_INTEGRATIONS
    assert tuple(cfg.DEFAULT_POD_INVARIANT_INTEGRATIONS) == ("github",)
    # _default_network() (used for brand-new pods with no network.json) reflects
    # the demoted default.
    assert cfg._default_network()["podInvariantIntegrations"] == ["github"]


# ── (b) idempotent migration ──────────────────────────────────────────────────


def _load(tmp_path: Path, invariants) -> list:
    net = {"networkId": "p", "bots": {}, "podInvariantIntegrations": invariants}
    path = tmp_path / "network.json"
    path.write_text(json.dumps(net))
    return cfg.load_network(path)["podInvariantIntegrations"]


def test_migration_strips_brave_from_exact_old_default(tmp_path):
    assert _load(tmp_path, ["github", "brave"]) == ["github"]


def test_migration_strips_brave_order_insensitive(tmp_path):
    # The old default could have been written in either order; both narrow.
    assert _load(tmp_path, ["brave", "github"]) == ["github"]


def test_migration_preserves_brave_only_list(tmp_path):
    # A deliberate brave-only list is operator intent — not the old default.
    assert _load(tmp_path, ["brave"]) == ["brave"]


def test_migration_preserves_customized_superset(tmp_path):
    # ["brave","github","tavily"] is a superset of the old default; preserve it.
    out = _load(tmp_path, ["brave", "github", "tavily"])
    assert out == ["brave", "github", "tavily"]


def test_migration_preserves_unrelated_list(tmp_path):
    assert _load(tmp_path, ["github", "tavily"]) == ["github", "tavily"]


def test_migration_leaves_already_demoted_list_untouched(tmp_path):
    # Already-narrowed list: no match against the old default → no-op.
    assert _load(tmp_path, ["github"]) == ["github"]


def test_migration_is_idempotent(tmp_path):
    # Run the in-memory migration twice; a second pass must be a no-op.
    net = {"podInvariantIntegrations": ["github", "brave"]}
    cfg._apply_pod_defaults(net)
    assert net["podInvariantIntegrations"] == ["github"]
    cfg._apply_pod_defaults(net)
    assert net["podInvariantIntegrations"] == ["github"]


def test_migration_ignores_non_list_value(tmp_path):
    net = {"podInvariantIntegrations": "github,brave"}
    cfg._apply_pod_defaults(net)
    # Non-list left untouched (no crash, no coercion).
    assert net["podInvariantIntegrations"] == "github,brave"


# ── (c) wizard prompt derives from podInvariantIntegrations ───────────────────


def test_wizard_solicit_set_excludes_brave_by_default(tmp_path):
    from evolve_admin import wizard

    # No network.json present → loader default (already demoted) is used.
    solicited = wizard._pod_invariant_integrations(tmp_path / "absent.json")
    assert "brave" not in solicited
    assert "github" in solicited


def test_wizard_solicit_set_includes_brave_when_invariant(tmp_path):
    from evolve_admin import wizard

    path = tmp_path / "network.json"
    # A customized list that keeps brave invariant (superset of old default, so
    # the migration does NOT strip it) re-enables the wizard prompt.
    path.write_text(json.dumps({
        "podInvariantIntegrations": ["github", "brave", "tavily"],
    }))
    solicited = wizard._pod_invariant_integrations(path)
    assert "brave" in solicited


def test_wizard_solicit_set_demoted_when_old_default_migrated(tmp_path):
    from evolve_admin import wizard

    path = tmp_path / "network.json"
    # Existing pod with the exact OLD default: the migration narrows it on
    # load, so the wizard no longer solicits brave — the mismatch can't recur.
    path.write_text(json.dumps({"podInvariantIntegrations": ["github", "brave"]}))
    solicited = wizard._pod_invariant_integrations(path)
    assert "brave" not in solicited


# ── (d) brave key in openclaw.json reads as active ───────────────────────────


def test_detect_brave_canonical_oc_path():
    oc = {
        "plugins": {
            "entries": {
                "brave": {"config": {"webSearch": {"apiKey": "BSA-canonical"}}}
            }
        }
    }
    assert brave_key_from_oc_config(oc) == "BSA-canonical"


def test_detect_brave_legacy_oc_path():
    # Hand-edited / older config: key at the legacy tools.web.search location.
    oc = {"tools": {"web": {"search": {"apiKey": "BSA-legacy"}}}}
    assert brave_key_from_oc_config(oc) == "BSA-legacy"


def test_detect_brave_canonical_wins_over_legacy():
    oc = {
        "plugins": {"entries": {"brave": {"config": {"webSearch": {"apiKey": "BSA-new"}}}}},
        "tools": {"web": {"search": {"apiKey": "BSA-old"}}},
    }
    assert brave_key_from_oc_config(oc) == "BSA-new"


def test_detect_brave_none_when_unconfigured():
    assert brave_key_from_oc_config({}) is None
    assert brave_key_from_oc_config({"plugins": {"entries": {}}}) is None
    # Whitespace-only key is treated as unset.
    oc = {"plugins": {"entries": {"brave": {"config": {"webSearch": {"apiKey": "  "}}}}}}
    assert brave_key_from_oc_config(oc) is None


def test_detect_brave_handles_non_dict():
    assert brave_key_from_oc_config(None) is None  # type: ignore[arg-type]
    assert brave_key_from_oc_config([]) is None  # type: ignore[arg-type]
