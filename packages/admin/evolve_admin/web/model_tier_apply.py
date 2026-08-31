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
# internal/design-model-swap-behavior-guard-2026-08-19.md): the write path above
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


def _ledger_shared_dir(network_path):
    """Shared dir for the pod ``network_path`` describes (honoring a pod that
    overrides ``sharedDir``)."""
    from pathlib import Path as _Path

    from ..config import CANONICAL_SHARED_DIR, load_network

    return _Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))


def record_swap(network_path, bot_id, tier, provider, previous_models, new_models,
                *, source) -> bool:
    """Append one model-swap record for the pod ``network_path`` describes.

    Thin adapter over :func:`model_swap_ledger.record_swap`: it resolves the
    shared dir from network.json so the endpoint call sites don't each have
    to. Import is deferred to call time — this module must stay importable in
    unit tests that never load the analyzer package.

    Never raises: a swap that happened must still be reported as applied even
    if its ledger line could not be written.
    """
    try:
        from model_swap_ledger import record_swap as _record  # type: ignore

        return _record(bot_id, tier, provider, previous_models, new_models,
                       source=source, shared_dir=_ledger_shared_dir(network_path))
    except Exception:  # noqa: BLE001 — see docstring
        return False


# ── Behavior pins (sticky rollback; canonical impl: model_swap_ledger) ───────
# Why (2026-08-21 recurrence): ``models rollback`` restored the pre-incident
# model, then a Model Freshness "Apply All" through the bulk endpoint below
# re-swapped the very model the rollback had rejected, and the deliberation
# leaks resumed. The pin ledger records the rejection; the two tier-write
# endpoints consult it via :func:`behavior_pins` and refuse to reintroduce a
# pinned (bot, tier, model) unless the request carries an explicit
# ``override_pin`` — in which case :func:`record_unpin` makes the operator's
# decision durable so pin state matches what is actually applied.


def behavior_pins(network_path) -> "tuple[dict, str | None]":
    """Active behavior pins for this pod: ``({(bot, tier, model_key): rec}, err)``.

    ``err`` is None in the two healthy states (no pin ledger yet, or a
    readable one). It carries a message when pin state is UNKNOWN — the
    ledger exists but is unreadable, or the check itself crashed. Callers
    enforcing pins must treat ``err`` as "refuse the write, override still
    available": an unknown pin state silently reading as unpinned is the
    exact non-sticky-rollback failure this gate exists to close. The one
    fail-open case is the analyzer package being absent entirely (unit-test
    environments) — pins are written by analyzer-side code, so there is
    nothing to read.
    """
    try:
        from model_swap_ledger import PinLedgerUnreadable, active_pins  # type: ignore
    except ImportError:
        return {}, None
    try:
        return active_pins(_ledger_shared_dir(network_path)), None
    except PinLedgerUnreadable as exc:
        return {}, f"behavior-pin ledger unreadable: {exc}"
    except Exception as exc:  # noqa: BLE001 — unknown state must not fail open
        return {}, f"behavior-pin check failed: {exc}"


def pin_lookup(pins: dict, bot_id, tier, model) -> "dict | None":
    """The active pin for (bot, tier, model) in a :func:`behavior_pins` result."""
    try:
        from model_swap_ledger import model_key  # type: ignore
    except ImportError:
        return None
    return pins.get((bot_id, tier, model_key(model)))


def record_unpin(network_path, bot_id, tier, model, *, source) -> bool:
    """Append an ``unpin`` event after an explicit operator override applied a
    pinned model — the pin has been consciously revoked, and leaving it active
    would refuse the *next* routine apply of a model the operator just chose.

    Never raises; a False return means the unpin did not persist (the next
    apply of this model will demand the override again — annoying, safe).
    """
    try:
        from model_swap_ledger import record_pin_event  # type: ignore

        return record_pin_event(
            bot_id, tier, model, action="unpin",
            reason="operator override via the admin tier-write endpoint",
            source=source, shared_dir=_ledger_shared_dir(network_path),
        )
    except Exception:  # noqa: BLE001 — see docstring
        return False
