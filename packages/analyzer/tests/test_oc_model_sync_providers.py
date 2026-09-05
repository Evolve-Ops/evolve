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
- Fresh catalog produces correctly-shaped {id, name, maxTokens} entries
  per provider
- Known-family entries always carry the catalog ``maxTokens`` (the
  2026-08-31 stopReason=length incident fix); unknown ids are left
  UNSTAMPED — OC's cap resolution is explicit-wins, so a stamped
  "conservative" value would clamp implicitly-cataloged models
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
    _MODEL_MAX_TOKENS,
    _OC_BUNDLED_PROVIDERS,
    _lookup_model_max_tokens,
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
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
        {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6", "maxTokens": 64000},
    ]
    # Unknown id: NO maxTokens key — explicit-wins means a stamp would
    # override OC's implicit catalog cap (gemini carries implicit 65536).
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
    their extra fields stomped on. Idempotency must be lossless. (The
    absent ``maxTokens`` IS backfilled — that's the repair pass doing its
    job, not a stomp; the operator-set fields all survive.)"""
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
            "maxTokens": 64000,
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
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
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
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
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
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
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
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
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
            {
                "id": "claude-haiku-4-5",
                "name": "claude-haiku-4-5",
                "maxTokens": 64000,
            },
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
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
        # Unknown family: name repaired, maxTokens deliberately NOT added.
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
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "maxTokens": 64000},
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
        # repaired: name mirrored AND the output cap stamped
        {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5", "maxTokens": 64000},
    ]


# ── maxTokens: known families stamped, unknown ids left unstamped ──────────
#
# 2026-08-31 incident class: OC's cap resolution is EXPLICIT-WINS
# (verified against the installed OC 2026.7.1-2 dist,
# resolvePreferredTokenLimit): explicit positive maxTokens wins
# unconditionally; absence falls to the bundled implicit catalog; and
# only when neither exists does the 8192 config default apply. A bare
# {id, name} entry for claude-opus-5 (absent from the bundled catalog)
# therefore truncated the PoC bot's turn at 8192 (stopReason=length).
# These tests pin BOTH halves of the fix: known families always carry
# the catalog cap, and unknown ids carry NO cap — stamping one would
# clamp models the implicit catalog knows (gemini implicit 65536).


def test_known_id_entry_always_carries_catalog_max_tokens():
    """Every family in the output-cap catalog, minted fresh, carries its
    catalog value — the contract the 2026-08-31 incident fix rests on."""
    for family, expected in _MODEL_MAX_TOKENS.items():
        data = _catalog_data(f"anthropic/{family}")
        sync_provider_models_from_catalog(data)
        assert _provider_models(data, "anthropic") == [
            {"id": family, "name": family, "maxTokens": expected},
        ], f"family={family}"


def test_dated_snapshot_id_lands_on_its_family_cap():
    """Dated snapshot ids (``<family>-YYYYMMDD``) match their family via
    the ``<family>-`` prefix rule."""
    data = _catalog_data("anthropic/claude-haiku-4-5-20251001")
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {
            "id": "claude-haiku-4-5-20251001",
            "name": "claude-haiku-4-5-20251001",
            "maxTokens": 64000,
        },
    ]


def test_unknown_id_is_left_unstamped():
    """An id the catalog doesn't know gets NO maxTokens field. Under
    explicit-wins, any stamped value — even OC's own 8192 default —
    would override the implicit catalog cap for models OC knows (e.g.
    gemini's implicit 65536), re-creating the truncation incident class
    on those routes. Absence is the correct, non-clamping state."""
    data = _catalog_data("xai/grok-4", "anthropic/claude-nonexistent-9")
    sync_provider_models_from_catalog(data)
    for provider, model_id in (
        ("xai", "grok-4"),
        ("anthropic", "claude-nonexistent-9"),
    ):
        (entry,) = _provider_models(data, provider)
        assert entry["id"] == model_id
        assert "maxTokens" not in entry, (
            f"{provider}/{model_id} must NOT be stamped — explicit-wins "
            "would clamp an implicitly-cataloged model"
        )


def test_lookup_contract_none_for_unknown_and_catalog_values_sane():
    """The lookup helper's contract: the catalog value for known
    families, None (leave unstamped) for everything else, and catalog
    values that are real caps (above OC's 8192 default — a family entry
    at or below the default would be pointless)."""
    assert _lookup_model_max_tokens("completely-unknown") is None
    assert _lookup_model_max_tokens("gemini-2.0-flash") is None
    for family, cap in _MODEL_MAX_TOKENS.items():
        assert isinstance(cap, int) and cap > 8192, f"family={family}"
        assert _lookup_model_max_tokens(family) == cap


def test_repair_backfills_missing_max_tokens_on_existing_entry():
    """The deployed-fleet shape: a pre-catalog ``{id, name}`` entry for a
    KNOWN family (every bot minted before this fix) gains maxTokens on
    its next sync — the self-heal that supersedes the 2026-08-31
    hand-patch fleet-wide."""
    data = {
        "agents": {"defaults": {"models": {"anthropic/claude-opus-5": {}}}},
        "models": {
            "providers": {
                "anthropic": {
                    "models": [{"id": "claude-opus-5", "name": "claude-opus-5"}],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    assert _provider_models(data, "anthropic") == [
        {"id": "claude-opus-5", "name": "claude-opus-5", "maxTokens": 64000},
    ]


def test_repair_preserves_operator_set_max_tokens():
    """An operator-chosen positive cap survives — larger or smaller than
    the catalog value (a deliberate 4096 cost clamp is as valid as a
    deliberate 128000)."""
    for operator_value in (4096, 128000):
        data = {
            "agents": {"defaults": {"models": {"anthropic/claude-opus-5": {}}}},
            "models": {
                "providers": {
                    "anthropic": {
                        "models": [
                            {
                                "id": "claude-opus-5",
                                "name": "claude-opus-5",
                                "maxTokens": operator_value,
                            },
                        ],
                    },
                },
            },
        }
        sync_provider_models_from_catalog(data)
        (entry,) = _provider_models(data, "anthropic")
        assert entry["maxTokens"] == operator_value


def test_repair_replaces_schema_invalid_max_tokens():
    """Non-numeric, non-positive, and boolean values fail the OC schema's
    ``number, exclusiveMinimum 0`` — replace them with the catalog value
    like the name repair replaces invalid names."""
    for bad_value in ("8192", None, 0, -1, True, False, [1], {}):
        data = {
            "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
            "models": {
                "providers": {
                    "anthropic": {
                        "models": [
                            {
                                "id": "claude-haiku-4-5",
                                "name": "claude-haiku-4-5",
                                "maxTokens": bad_value,
                            },
                        ],
                    },
                },
            },
        }
        sync_provider_models_from_catalog(data)
        (entry,) = _provider_models(data, "anthropic")
        assert entry["maxTokens"] == 64000, f"bad_value={bad_value!r}"


def test_repair_removes_schema_invalid_max_tokens_on_unknown_id():
    """A schema-invalid cap on an UNKNOWN-family id cannot be replaced —
    we don't know the right value — so the repair removes the key,
    restoring the absence that lets OC's implicit catalog (or config
    default) resolve it. Leaving the invalid value would fail OC's
    startup validator; guessing a value would clamp (explicit-wins)."""
    for bad_value in ("8192", None, 0, True, [1]):
        data = {
            "agents": {"defaults": {"models": {"google/gemini-2.0-flash": {}}}},
            "models": {
                "providers": {
                    "google": {
                        "models": [
                            {
                                "id": "gemini-2.0-flash",
                                "name": "gemini-2.0-flash",
                                "maxTokens": bad_value,
                            },
                        ],
                    },
                },
            },
        }
        sync_provider_models_from_catalog(data)
        (entry,) = _provider_models(data, "google")
        assert "maxTokens" not in entry, f"bad_value={bad_value!r}"


def test_repair_preserves_operator_cap_on_unknown_id():
    """An operator who set a valid cap on an unknown-family id keeps it —
    the unknown-id rule is 'never GUESS', not 'never allow'."""
    data = {
        "agents": {"defaults": {"models": {"google/gemini-2.0-flash": {}}}},
        "models": {
            "providers": {
                "google": {
                    "models": [
                        {
                            "id": "gemini-2.0-flash",
                            "name": "gemini-2.0-flash",
                            "maxTokens": 32000,
                        },
                    ],
                },
            },
        },
    }
    sync_provider_models_from_catalog(data)
    (entry,) = _provider_models(data, "google")
    assert entry["maxTokens"] == 32000


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
        {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6", "maxTokens": 64000},
    ]
    assert _provider_models(data, "google") == [
        {"id": "gemini-2.5-pro", "name": "gemini-2.5-pro"},
    ]
