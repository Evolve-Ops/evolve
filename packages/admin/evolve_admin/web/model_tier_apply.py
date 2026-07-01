"""Shared helpers for the model-tier freshness "Apply"/"Apply All" write path.

Extracted from ``routes_admin_config`` so the single (`api_models_update_tier`)
and bulk (`api_models_update_tier_bulk`) endpoints share one implementation of
the rung-collision-safe write + post-write verification.

The bug these guard against (model-tiers false-success, 2026-06-27): on the
rungs/roles tier shape several legacy tier keys collapse onto one rung — e.g.
``standard``(tier2) and ``judge``(tier0) both map to ``sonnet-class``. oc_model
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
