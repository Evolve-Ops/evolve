"""
spec_lineage.py — Spec-id supersession lineage for v7-arc Instances.

Background
──────────
A v7-arc Instance pins itself to a Spec via ``provenance.spec_id`` (``p-…``)
and tracks *version* bumps in ``spec_version_history[]`` (see ``adopt.py``).
But when an app is rebuilt under a **fresh** ``spec_id`` — a forge re-create,
or scanner re-discovery minting a new ``p-`` id — the OLD ``spec_id`` is
dropped with no link to the new one. Files on disk still carry the old
``spec_id`` in their ``_evolve`` marker (see ``provenance.py``), so
reconciliation (``reflect.py``) reports them as orphans even though the app
is still installed under a new id.

This module adds the missing link: an append-only **supersession chain** at
``provenance.prior_spec_ids[]`` plus the two pure functions that maintain and
consult it.

  - ``record_spec_supersession(instance, old_spec_id)`` — append a retired
    ``spec_id`` to the chain (write path, called at the re-spec site).
  - ``resolve_spec(spec_id, instances)`` — return the live Instance whose
    *current* ``spec_id`` OR whose ``prior_spec_ids[]`` contains ``spec_id``
    (read path, called by reconciliation so a ``p-old`` marker resolves to
    the live app instead of orphaning).

``provenance.prior_spec_ids`` schema
────────────────────────────────────
``list[str]`` — the chain of ``spec_id`` values this Instance has previously
been bound to, **oldest → newest**, NOT including the current
``provenance.spec_id``. Optional and back-compat: an absent key reads as the
empty chain. Append-only; entries are deduped and order-preserved.

See docs/spec-manifest-v7-2026-05-20.md §5 (Provenance) and the app-identity
ledger sub-spec of docs/spec-apps-meta-2026-06-13.md.

Pure module — no disk I/O. Callers own reads/writes (manifest atomic writes
live in ``manifest.py`` / ``native_write.py``); these functions only mutate /
inspect in-memory Instance dicts.
"""

from __future__ import annotations

from typing import Iterable, Optional


# ── Chain accessors ───────────────────────────────────────────────────────────

def _provenance(instance: dict) -> dict:
    """Return ``instance['provenance']`` as a dict, creating it if absent.

    Mutates ``instance`` so the returned dict is the live one (callers append
    to it in place).
    """
    prov = instance.get("provenance")
    if not isinstance(prov, dict):
        prov = {}
        instance["provenance"] = prov
    return prov


def prior_spec_ids(instance: dict) -> list[str]:
    """Return the Instance's supersession chain (oldest→newest), or ``[]``.

    Read-only — never creates the key. Tolerates a missing/garbled
    ``provenance`` or ``prior_spec_ids`` (returns ``[]``) and filters out
    non-string / empty entries so downstream lookups stay clean.
    """
    prov = instance.get("provenance")
    if not isinstance(prov, dict):
        return []
    chain = prov.get("prior_spec_ids")
    if not isinstance(chain, list):
        return []
    return [s for s in chain if isinstance(s, str) and s]


def current_spec_id(instance: dict) -> Optional[str]:
    """Return ``provenance.spec_id`` (the live binding), or ``None``."""
    prov = instance.get("provenance")
    if not isinstance(prov, dict):
        return None
    sid = prov.get("spec_id")
    return sid if isinstance(sid, str) and sid else None


# ── Write path ────────────────────────────────────────────────────────────────

def record_spec_supersession(instance: dict, old_spec_id: str) -> dict:
    """Append ``old_spec_id`` to ``instance['provenance']['prior_spec_ids']``.

    Mutates ``instance`` in place and returns it. The chain is append-only,
    oldest→newest, deduped, and order-preserving.

    No-op (the chain is left exactly as it was) when:
      - ``old_spec_id`` is empty / falsy,
      - ``old_spec_id`` equals the Instance's current ``provenance.spec_id``
        (a Spec never supersedes itself), or
      - ``old_spec_id`` is already in the chain.

    Back-compat: an Instance with no ``prior_spec_ids`` key (or no
    ``provenance`` block at all) gets one created on first append.
    """
    if not old_spec_id or not isinstance(old_spec_id, str):
        return instance

    prov = _provenance(instance)
    if old_spec_id == prov.get("spec_id"):
        return instance

    # Normalize to a clean list of non-empty strings, preserving order.
    raw = prov.get("prior_spec_ids")
    chain: list[str] = (
        [s for s in raw if isinstance(s, str) and s]
        if isinstance(raw, list)
        else []
    )
    if old_spec_id not in chain:
        chain.append(old_spec_id)
    prov["prior_spec_ids"] = chain
    return instance


def record_spec_supersessions(instance: dict, old_spec_ids: Iterable[str]) -> dict:
    """Append every id in *old_spec_ids* to the Instance's supersession chain.

    A thin convenience over :func:`record_spec_supersession` for the 1→N split
    case, where one successor Instance inherits a *set* of retired spec_ids at
    once (e.g. a marker that carried two retired ids, or several files whose
    markers each carry a different retired id all binding to the same
    successor). Order-preserving and idempotent: each id goes through
    ``record_spec_supersession``, so empties / self-references / duplicates are
    no-ops. Mutates *instance* in place and returns it.
    """
    for sid in old_spec_ids:
        record_spec_supersession(instance, sid)
    return instance


# ── Read path ─────────────────────────────────────────────────────────────────

def resolve_spec(spec_id: str, instances: Iterable[dict]) -> Optional[dict]:
    """Return the live Instance that ``spec_id`` resolves to, or ``None``.

    An Instance "resolves" ``spec_id`` when its *current*
    ``provenance.spec_id == spec_id`` OR its ``provenance.prior_spec_ids[]``
    contains ``spec_id`` (a retired binding that was superseded). This is what
    reconciliation calls so a workspace marker carrying ``p-old`` still maps to
    the app that is live under ``p-new``.

    A current-binding match wins over a prior-chain match: the scan checks the
    current ``spec_id`` of every Instance before falling back to prior chains,
    so an id that is simultaneously some app's live id and another app's
    retired id resolves to the live one.

    O(len(instances)). For repeated lookups against the same Instance set,
    build the index once with :func:`build_spec_index` and dict-lookup instead.
    """
    if not spec_id:
        return None

    fallback: Optional[dict] = None
    for inst in instances:
        prov = inst.get("provenance")
        if not isinstance(prov, dict):
            continue
        if prov.get("spec_id") == spec_id:
            return inst
        if fallback is None:
            chain = prov.get("prior_spec_ids")
            if isinstance(chain, list) and spec_id in chain:
                fallback = inst
    return fallback


def build_spec_index(instances: Iterable[dict]) -> dict[str, dict]:
    """Map every resolvable ``spec_id`` → Instance for O(1) repeated lookups.

    Includes each Instance's current ``spec_id`` and every entry in its
    ``prior_spec_ids[]``. A current binding wins over a retired one on
    collision (mirrors :func:`resolve_spec`): prior chains are indexed first,
    then current ids overwrite, so a live id always beats a superseded id.

    Within a single pass, the first Instance seen owns a contested *prior* id
    (later ones do not clobber it) — duplicate prior chains across Instances
    are a degenerate state ``record_spec_supersession`` cannot produce on a
    coherent pod, so the tie-break is arbitrary-but-stable rather than
    load-bearing.
    """
    index: dict[str, dict] = {}
    materialized = list(instances)
    # Pass 1: retired ids (first writer wins).
    for inst in materialized:
        for sid in prior_spec_ids(inst):
            index.setdefault(sid, inst)
    # Pass 2: current ids overwrite — a live binding beats a retired one.
    for inst in materialized:
        sid = current_spec_id(inst)
        if sid:
            index[sid] = inst
    return index
