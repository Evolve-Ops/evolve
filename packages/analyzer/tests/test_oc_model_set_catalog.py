"""tests/test_oc_model_set_catalog.py — oc_model.set_catalog shape tolerance.

set_catalog is the boundary where the in-process catalog representations
(list[str] of model IDs OR list[dict] of {"id", "provider"} entries)
meet OC's storage shape (``agents.defaults.models: {<id>: {<meta>}}``).

Historically, set_catalog accepted only list[str]. Real callers passed
list[dict] without realizing it — provisioning.seed_model_config_if_empty
and the /api/admin/config/<bot>/tiers reconcile endpoint both did. Using
a dict as a dict key trips ``TypeError: unhashable type: 'dict'`` inside
the oc_model.py subprocess, which surfaces to operators as the opaque
"write failed — check server logs" message. Two production bugs in one
month (PR #1725 was the first, this is the second). After this PR,
set_catalog accepts either shape — any future caller is immune.

Coverage:
- list[str] input writes correctly (the canonical form)
- list[dict] with "id" key writes correctly (the team_bot_a/atlas caller form)
- list[dict] with "model" key tolerated (alternate caller convention)
- Mixed list[str|dict] tolerated
- Per-model metadata preserved when ID already present
- Duplicate IDs in input list don't crash; first wins
- Non-string, non-dict entries silently dropped (defensive)
- Empty list clears the catalog
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from oc_model import set_catalog  # noqa: E402


def _empty_data() -> dict:
    return {"agents": {"defaults": {}}}


def _models_block(data: dict) -> dict:
    return data["agents"]["defaults"]["models"]


# ── list[str] — canonical form ─────────────────────────────────────────────


def test_accepts_list_of_string_ids():
    """Canonical form: list of model ID strings."""
    data = _empty_data()
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "google/gemini-2.5-pro",
    ])
    models = _models_block(data)
    assert list(models.keys()) == [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "google/gemini-2.5-pro",
    ]
    # Each value is an (empty) metadata dict
    assert all(v == {} for v in models.values())


# ── list[dict] — caller-convenience form ────────────────────────────────────


def test_accepts_list_of_dict_with_id_key():
    """REGRESSION: provisioning + reconcile_catalog both produce
    list[dict] with "id" key. Before tolerance was added, this tripped
    "unhashable type: 'dict'" inside set_catalog and surfaced to
    operators as the opaque 'write failed — check server logs' message."""
    data = _empty_data()
    set_catalog(data, [
        {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
        {"id": "openai/gpt-4o-mini", "provider": "openai"},
    ])
    models = _models_block(data)
    assert set(models.keys()) == {
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o-mini",
    }


def test_accepts_list_of_dict_with_model_key():
    """Some callers use 'model' instead of 'id' (older convention)."""
    data = _empty_data()
    set_catalog(data, [
        {"model": "anthropic/claude-sonnet-4-6"},
    ])
    models = _models_block(data)
    assert "anthropic/claude-sonnet-4-6" in models


def test_accepts_mixed_string_and_dict_entries():
    """Defensive: heterogeneous input shouldn't crash."""
    data = _empty_data()
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        {"id": "openai/gpt-4o", "provider": "openai"},
        {"model": "google/gemini-2.5-pro"},
    ])
    models = _models_block(data)
    assert set(models.keys()) == {
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "google/gemini-2.5-pro",
    }


# ── Metadata preservation ───────────────────────────────────────────────────


def test_preserves_existing_per_model_metadata():
    """When a model ID already has metadata (cost overrides, custom
    routing, etc.), set_catalog should NOT clobber it. The catalog
    write is a whitelist update, not a metadata reset."""
    data = {
        "agents": {"defaults": {"models": {
            "anthropic/claude-sonnet-4-6": {"cost_override_usd": 0.01},
            "openai/gpt-4o": {},
        }}}
    }
    # Rewrite the catalog with the same set — metadata should survive
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
    ])
    models = _models_block(data)
    assert models["anthropic/claude-sonnet-4-6"] == {"cost_override_usd": 0.01}


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_deduplicates_repeated_ids_in_input():
    """Operator might accidentally pass duplicates — don't crash, first wins."""
    data = _empty_data()
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        {"id": "anthropic/claude-sonnet-4-6", "provider": "anthropic"},
        "anthropic/claude-sonnet-4-6",
    ])
    models = _models_block(data)
    assert list(models.keys()) == ["anthropic/claude-sonnet-4-6"]


def test_silently_drops_non_string_non_dict_entries():
    """Defensive: integer / None / list entries shouldn't crash."""
    data = _empty_data()
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        42,
        None,
        ["nested"],
        {"id": "openai/gpt-4o"},
    ])
    models = _models_block(data)
    assert set(models.keys()) == {
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
    }


def test_dict_without_id_or_model_field_silently_dropped():
    """A dict that has neither "id" nor "model" can't be represented in
    OC's storage shape — drop it rather than crash."""
    data = _empty_data()
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        {"provider": "openai", "released": "2025-05-13"},  # no id/model
        {"id": "openai/gpt-4o"},
    ])
    models = _models_block(data)
    assert set(models.keys()) == {
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
    }


def test_empty_list_clears_catalog():
    """Operator may want to reset the catalog — empty list should produce empty models dict."""
    data = {
        "agents": {"defaults": {"models": {
            "anthropic/claude-sonnet-4-6": {},
            "openai/gpt-4o": {},
        }}}
    }
    set_catalog(data, [])
    models = _models_block(data)
    assert models == {}
