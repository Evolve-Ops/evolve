"""tests/test_oc_model_sync_providers.py — provider-registry sync.

``sync_provider_models_from_catalog`` keeps ``models.providers[<provider>]
.models[]`` in sync with the catalog at ``agents.defaults.models``.
Required by OpenClaw v2026.6.1+, whose runtime registry-gate
(``buildMissingProviderModelRegistrationHint``) rejects models that are
present in the catalog but missing from the provider registry, even when
the model is in OC's bundled static catalog.

The function is also called transitively by ``set_catalog`` so every
catalog write maintains both layers in sync.

Coverage:
- Fresh catalog produces correctly-shaped {id, name} entries per provider
- Re-run on the same config is idempotent (no churn)
- Existing richer entries are preserved untouched (no overwrites)
- Malformed catalog keys (no slash, empty provider, empty model) are skipped
- Non-bundled providers (e.g. runway) are skipped — OC needs baseUrl for them
- Operator-set non-dict provider blocks are left alone
- set_catalog calls the sync helper transitively
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from oc_model import (  # noqa: E402
    _OC_BUNDLED_PROVIDERS,
    set_catalog,
    sync_provider_models_from_catalog,
)


def _catalog_data(*model_keys: str) -> dict:
    """Build a config with a populated agents.defaults.models catalog."""
    return {
        "agents": {
            "defaults": {
                "models": {k: {} for k in model_keys},
            },
        },
    }


def _provider_models(data: dict, provider: str) -> list:
    return (
        data.get("models", {})
        .get("providers", {})
        .get(provider, {})
        .get("models", [])
    )


# ── happy path ─────────────────────────────────────────────────────────────


def test_fresh_catalog_writes_correctly_shaped_entries():
    data = _catalog_data(
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-6",
        "google/gemini-2.0-flash",
    )
    sync_provider_models_from_catalog(data)

    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
        {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6"},
    ]
    assert _provider_models(data, "google") == [
        {"id": "gemini-2.0-flash", "name": "gemini-2.0-flash"},
    ]


def test_idempotent_on_rerun():
    """Re-running on a config that's already in sync produces no change."""
    data = _catalog_data(
        "anthropic/claude-haiku-4-5",
        "openai/gpt-4o",
    )
    sync_provider_models_from_catalog(data)
    snapshot = sorted(
        (p, m["id"])
        for p, block in data["models"]["providers"].items()
        for m in block["models"]
    )
    sync_provider_models_from_catalog(data)
    sync_provider_models_from_catalog(data)
    snapshot_after = sorted(
        (p, m["id"])
        for p, block in data["models"]["providers"].items()
        for m in block["models"]
    )
    assert snapshot == snapshot_after


# ── preserve operator-set richer entries ────────────────────────────────────


def test_preserves_richer_existing_entries():
    """An operator who set {id, name, contextWindow, cost} should not have
    their extra fields stomped on. Idempotency must be lossless."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [
                        {
                            "id": "claude-haiku-4-5",
                            "name": "Claude Haiku 4.5",
                            "contextWindow": 200000,
                            "cost": {"input": 1.0, "output": 5.0},
                        },
                    ],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {
            "id": "claude-haiku-4-5",
            "name": "Claude Haiku 4.5",
            "contextWindow": 200000,
            "cost": {"input": 1.0, "output": 5.0},
        },
    ]


def test_preserves_operator_non_dict_provider_block():
    """If an operator wrote a non-dict (e.g. string scalar) under a provider
    key, we must not clobber it. This is a defensive guarantee even though
    no caller is expected to produce this shape."""
    data = _catalog_data("anthropic/claude-haiku-4-5")
    data.setdefault("models", {})["providers"] = {"anthropic": "operator-wrote-this"}
    sync_provider_models_from_catalog(data)
    assert data["models"]["providers"]["anthropic"] == "operator-wrote-this"


# ── malformed input ────────────────────────────────────────────────────────


def test_malformed_catalog_keys_skipped():
    """Keys without a clean ``<provider>/<model>`` shape are silently
    skipped. They can't be represented in the provider-registry."""
    data = _catalog_data(
        "anthropic/claude-haiku-4-5",
        "no-slash-here",
        "/empty-provider",
        "empty-id/",
        "/",
    )
    sync_provider_models_from_catalog(data)
    # Only the valid entry is registered.
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
    ]
    # No phantom providers from the malformed keys.
    assert set(data["models"]["providers"].keys()) == {"anthropic"}


def test_empty_catalog_is_noop():
    data = {"agents": {"defaults": {"models": {}}}}
    sync_provider_models_from_catalog(data)
    assert "models" not in data or data["models"] == {}


def test_missing_catalog_is_noop():
    data = {}
    sync_provider_models_from_catalog(data)
    assert data == {}


# ── non-bundled providers ──────────────────────────────────────────────────


def test_non_bundled_provider_skipped():
    """For providers not in ``_OC_BUNDLED_PROVIDERS`` (e.g. runway), OC's
    schema requires baseUrl + transport config we cannot synthesize. We
    skip them entirely rather than synthesize a half-registration that
    would fail OC's schema validator on startup (the failure mode that
    crash-looped six gateways during a prior incident)."""
    data = _catalog_data(
        "anthropic/claude-haiku-4-5",
        "runway/gen4.5",  # not bundled — must be skipped
    )
    sync_provider_models_from_catalog(data)
    # Anthropic registered, runway untouched (no synthetic provider block).
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
    ]
    assert "runway" not in data["models"]["providers"]


def test_bundled_providers_set_includes_known_providers():
    """Smoke-test: the bundled set must include the providers our pod
    actually uses, so they get registered. If this fails, fix the set
    after confirming OC actually bundles a catalog for the missing
    provider."""
    expected = {"anthropic", "openai", "google", "xai"}
    assert expected.issubset(_OC_BUNDLED_PROVIDERS)


# ── repair pass: heal entries with missing/invalid name ────────────────────


def test_repair_backfills_missing_name():
    """An entry that has ``id`` but no ``name`` (the schema-fatal shape that
    crash-loops gateways with ``models.providers.<P>.models.<N>.name:
    Invalid input``) must be repaired by setting ``name = id``. Without
    this, the existing entry survives the add-missing pass — the dedupe
    key is ``entry.get("id") == model_id``, so a malformed
    ``{"id": ...}`` reads as 'already registered'."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [{"id": "claude-haiku-4-5"}],  # no name
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
    ]


def test_repair_backfills_empty_string_name():
    """``name: ""`` fails the schema's ``minLength: 1`` constraint. The
    repair must overwrite empty strings with ``id``."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [{"id": "claude-haiku-4-5", "name": ""}],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
    ]


def test_repair_backfills_non_string_name():
    """``name: false`` / ``name: null`` / ``name: 0`` all fail the schema's
    ``type: "string"`` constraint. Repair overwrites with ``id``."""
    for bad_name in (False, None, 0, ["x"], {"x": 1}):
        data = {
            "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
            "models": {
                "providers": {
                    "anthropic": {
                        "models": [
                            {"id": "claude-haiku-4-5", "name": bad_name},
                        ],
                    },
                },
            },
        }
        sync_provider_models_from_catalog(data)
        assert _provider_models(data, "anthropic") == [
            {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
        ], f"bad_name={bad_name!r}"


def test_repair_runs_on_entries_outside_current_catalog():
    """Orphan entries — present in ``models.providers`` but no longer
    referenced by ``agents.defaults.models`` — must still be repaired.
    OC's startup validator rejects ANY malformed entry, not just ones
    tied to the current catalog."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [
                        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
                        {"id": "claude-orphan-from-old-catalog"},  # no name
                    ],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
        {
            "id": "claude-orphan-from-old-catalog",
            "name": "claude-orphan-from-old-catalog",
        },
    ]


def test_repair_preserves_valid_name():
    """An entry with a valid ``name`` (any non-empty string, including a
    display-formatted one like 'Claude Haiku 4.5') must not be overwritten.
    Operators can curate names without losing them on the next deploy."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [
                        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
                    ],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
    ]


def test_repair_skips_entries_without_id():
    """An entry with no ``id`` can't be repaired by mirroring — there's
    nothing to mirror from. Leave it for OC's validator to reject so the
    operator sees the real shape error instead of a synthesized
    ``name: ""`` masking it."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [
                        {"name": "orphan"},  # no id
                        {"id": "claude-haiku-4-5"},
                    ],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {"name": "orphan"},  # untouched
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},  # repaired
    ]


# ── set_catalog calls sync transitively ────────────────────────────────────


def test_set_catalog_calls_sync():
    """Every catalog write should refresh models.providers in lockstep.
    Centralizing the sync at the set_catalog boundary means every
    callsite (provisioning, reconcile, deploy gap-fill) stays consistent
    without per-caller awareness."""
    data = {}
    set_catalog(data, [
        "anthropic/claude-sonnet-4-6",
        "google/gemini-2.5-pro",
    ])
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6"},
    ]
    assert _provider_models(data, "google") == [
        {"id": "gemini-2.5-pro", "name": "gemini-2.5-pro"},
    ]
