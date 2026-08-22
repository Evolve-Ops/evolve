"""Shared helpers for the model-tier freshness "Apply"/"Apply All" write path.

Extracted from ``routes_admin_config`` so the single (`api_models_update_tier`)
and bulk (`api_models_update_tier_bulk`) endpoints share one implementation of
the rung-collision-safe write + post-write verification.

The bug these guard against (model-tiers false-success, 2026-06-27): on the
rungs/roles tier shape several legacy tier keys can collapse onto one rung —
historically ``standard``(tier2) and ``judge``(tier0) both mapped to
``sonnet-class`` (judge is now its own ``judge-class``, spec §Addendum 16; a
hand-authored roles map can still collide). oc_model
folds a legacy ``tiers`` update into the rung store one key at a time,
last-writer-wins, so sending the FULL synthesized tiers dict lets an UNCHANGED
sibling tier clobber a just-applied edit. The write "succeeds" (rc=0) but the
model never lands and the freshness advisory never clears. Two rules keep the
endpoints honest:

  1. Send ONLY the tiers actually changed (so a sibling sharing a rung can't
     clobber) — callers stage edits via :func:`stage_tier_model`.
  2. A truthy setter result is NOT proof of persistence — verify the model is
     present in the post-write, disk-synthesized tiers via :func:`model_landed`
     before reporting success.
"""

from __future__ import annotations


def stage_tier_model(tier_entry: "dict | None", provider, new_model) -> dict:
    """Return a NEW tier entry with ``new_model`` set for ``provider``.

    Replaces the first model from the same provider in ``models[]``; appends
    when the tier has no same-provider entry. Never mutates the input — the
    bulk caller accumulates several updates against the same staged entry.
    (``provider``/``new_model`` are request-derived strings, left unannotated
    so callers can pass the already-validated ``body.get(...)`` values.)
    """
    models = list((tier_entry or {}).get("models") or [])
    for i, m in enumerate(models):
        if m and "/" in m and m.split("/", 1)[0].lower() == provider:
            models[i] = new_model
            break
    else:
        models.append(new_model)
    out = dict(tier_entry or {})
    out["models"] = models
    return out


def tier_models(result: "dict | None", tier) -> list:
    """Return the models[] for ``tier`` in a post-write config result (or [])."""
    tiers = (result or {}).get("tiers") or {}
    return ((tiers.get(tier) or {}).get("models")) or []


def model_landed(result: "dict | None", tier, model) -> bool:
    """True iff ``model`` is present in ``tier`` of the post-write result.

    ``result`` is what the setter returns — the post-write, disk-synthesized
    config. A truthy result with the model ABSENT means a silent non-persist
    (the rung clobber, a perms error, or a schema reject that still exits 0);
    callers MUST treat ``False`` as a failed write, not a success.
    """
    return model in tier_models(result, tier)


# ── Model-swap ledger (canonical impl: analyzer/model_swap_ledger.py) ────────
# Why this exists (2026-08-14 group-chat silence incident; design doc
# docs/design-model-swap-behavior-guard-2026-08-19.md): the write path above
# verifies that the model STRING landed, and stops there. Nothing recorded
# what the rung held BEFORE the write, so when six bots started behaving
# differently the operator had neither a one-command undo nor an artifact
# tying the behavior change to the swap that caused it — POD_CONDUCT rule 2
# ("applying a change is not the same as the change taking effect"), which
# Evolve enforces on its bots, failing in Evolve's own control plane.
#
# The ledger itself lives in the analyzer package because its heavier consumer
# is ``analyzer/model_swap_watch.py`` (and the analyzer must not import the
# admin web layer). This module re-exports the writer so the tier-write call
# sites keep importing their whole contract — stage / verify / record — from
# one place.


def record_swap(network_path, bot_id, tier, provider, previous_models, new_models,
                *, source) -> bool:
    """Append one model-swap record for the pod ``network_path`` describes.

    Thin adapter over :func:`model_swap_ledger.record_swap`: it resolves the
    shared dir from network.json (honoring a pod that overrides ``sharedDir``)
    so the endpoint call sites don't each have to. Import is deferred to call
    time — this module must stay importable in unit tests that never load the
    analyzer package.

    Never raises: a swap that happened must still be reported as applied even
    if its ledger line could not be written.
    """
    from pathlib import Path as _Path

    try:
        from model_swap_ledger import record_swap as _record  # type: ignore

        from ..config import CANONICAL_SHARED_DIR, load_network

        shared_dir = _Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))
        return _record(bot_id, tier, provider, previous_models, new_models,
                       source=source, shared_dir=shared_dir)
    except Exception:  # noqa: BLE001 — see docstring
        return False
