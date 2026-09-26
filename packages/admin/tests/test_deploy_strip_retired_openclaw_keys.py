"""Tests for the writer-side fix in
breaker-notify-survives-openclaw-config-validation item 2.

OpenClaw 2026.9.2 retired ``agents.defaults.contextPruning.keepLastAssistants``
(a strict-schema ``Unrecognized key``, not a rename — see the module comment
on ``deploy._BALANCED_COST_DEFAULTS``). Evolve was the writer: ``deploy.py``'s
gap-fill and ``cost_profiles.py``'s built-in profiles both used to inject it,
which is how it ended up baked into the ``evolve`` service account's own
openclaw.json and blocked every ``openclaw`` CLI invocation that resolves
config off that account (breaker notify included).

Two things are pinned here:
  1. Golden JSON — neither writer emits the key any more.
  2. The matching subtraction — ``strip_retired_openclaw_keys`` removes an
     already-on-disk instance (gap-fill only ever ADDS, so it has nothing to
     say about a key that's already present).
"""

from __future__ import annotations

from evolve_admin.deploy import _BALANCED_COST_DEFAULTS, gap_fill_cost_settings
from evolve_admin.oc_retired_keys import strip_retired_openclaw_keys


def test_balanced_cost_defaults_never_emit_keep_last_assistants() -> None:
    """Golden JSON: the deploy-time default dict itself carries no
    retired key, under any nesting OpenClaw might have used it at."""
    assert "keepLastAssistants" not in _BALANCED_COST_DEFAULTS["contextPruning"]


def test_fresh_install_gap_fill_never_writes_keep_last_assistants() -> None:
    cfg: dict = {}
    changed = gap_fill_cost_settings(cfg, snapshot=None)
    assert changed is True
    assert "keepLastAssistants" not in cfg["agents"]["defaults"]["contextPruning"]


def test_snapshot_carrying_the_retired_key_is_cleaned_by_the_full_pipeline() -> None:
    """gap_fill_cost_settings alone copies the operator's snapshot
    verbatim (by design — it fills gaps, it does not scrub snapshot
    content). A stale pre-retirement snapshot can therefore still
    reintroduce the key at that step; it's the FULL deploy pipeline —
    gap-fill followed by strip_retired_openclaw_keys, exactly as
    ``deploy.ensure_plugin_config`` runs them — that must leave it out."""
    cfg: dict = {}
    snapshot = {
        "contextPruning": {"mode": "cache-ttl", "ttl": "5m", "keepLastAssistants": 5},
    }
    gap_fill_cost_settings(cfg, snapshot=snapshot)
    strip_retired_openclaw_keys(cfg)
    assert "keepLastAssistants" not in cfg["agents"]["defaults"]["contextPruning"]


def test_strip_removes_an_already_deployed_instance() -> None:
    """The drift-apply migration: a bot deployed before this fix keeps the
    key forever unless something actively removes it — gap-fill can't,
    since the key is already present."""
    cfg = {
        "agents": {
            "defaults": {
                "contextPruning": {
                    "mode": "cache-ttl", "ttl": "5m", "keepLastAssistants": 5,
                },
            },
        },
    }
    changed = strip_retired_openclaw_keys(cfg)
    assert changed is True
    assert "keepLastAssistants" not in cfg["agents"]["defaults"]["contextPruning"]
    # Sibling keys are untouched.
    assert cfg["agents"]["defaults"]["contextPruning"]["mode"] == "cache-ttl"
    assert cfg["agents"]["defaults"]["contextPruning"]["ttl"] == "5m"


def test_strip_is_a_noop_on_an_already_clean_config() -> None:
    cfg = {
        "agents": {
            "defaults": {"contextPruning": {"mode": "cache-ttl", "ttl": "5m"}},
        },
    }
    before = dict(cfg["agents"]["defaults"]["contextPruning"])
    changed = strip_retired_openclaw_keys(cfg)
    assert changed is False
    assert cfg["agents"]["defaults"]["contextPruning"] == before


def test_strip_tolerates_missing_sections() -> None:
    assert strip_retired_openclaw_keys({}) is False
    assert strip_retired_openclaw_keys({"agents": {}}) is False
    assert strip_retired_openclaw_keys({"agents": {"defaults": {}}}) is False
