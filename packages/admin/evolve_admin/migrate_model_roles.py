#!/usr/bin/env python3
"""migrate_model_roles — one-shot tier→role config migration.

Rewrites the persisted model-tier identifiers (``tier0``-``tier3``) to the
role IDs (``fast``/``standard``/``power``/``judge``) introduced by
spec-model-rungs-and-roles-2026-06-09. Three storage surfaces carry tier
keys and get rewritten:

  - ``network.json``                  — ``models.tiers``       → ``models.rungs`` + ``models.roles``
                                        ``routing.*Tier``      → ``routing.*Role``
                                        ``userTierOverride.*``  → role-shaped
  - ``~/.openclaw/evolve-tiers.json``  — same shape, per bot
  - ``{sharedDir}/{botId}/user-tier-prefs.json`` — ``users.<k>.defaultTier`` → ``defaultRole``

Mapping (spec §Legacy-shape fallback):

  tier3 → fast      (haiku-class)
  tier2 → standard  (sonnet-class)
  tier1 → power     (opus-class)
  tier0 → judge     (judge-class + provider:"not-standard")

  userTierOverride.dailyCap        → roleCaps.power.maxPerDayPerBot
  userTierOverride.allowBotInitiated (bool) → {power: <bool>, max: false}
  userTierOverride.defaultTier     → userTierOverride.defaultRole
  routing.{maintenance,background,ambiguous}Tier → *Role

The transform is a pure function (``migrate_models_block`` /
``migrate_evolve_tiers`` / ``migrate_user_tier_prefs``) so it is unit-
testable without touching the filesystem, and **idempotent**: a config
already in the rungs/roles shape is returned unchanged (``changed=False``),
so the command is safe to re-run at any time.

**This command has no automatic caller** (#3566). It is operator-invoked
only — deploy does NOT run it. An earlier version of this docstring said it
was "safe to run on every deploy", which readers took to mean it *did* run
on every deploy; it never has. Consequence: a bot that is legacy-shaped when
the fleet migration runs stays legacy-shaped forever, because oc_model's
writer is deliberately shape-preserving. If you are adding a new legacy-shape
producer, fix the producer — do not rely on this command to clean up after it.

Writing ``rungs`` IS the Custom flip (``primary_bot.bot_has_custom_tiers``),
and a Custom bot does not inherit the pod's auto-upgrade ``enabled``. So the
driver also carries the pod's ``models.autoUpgrade`` block onto any bot the
migration itself flips from legacy-shape to Custom — see
``_carry_pod_auto_upgrade_on_custom_flip``. The pure transforms stay pure; the
carry lives in ``run_migration``.

File access follows CLAUDE.md: network.json is written via
``config.save_network`` (its own /tmp-stage + sudo cp + chown). The
per-bot ``evolve-tiers.json`` is owned by the bot user, so it is staged in
/tmp and copied with ``sudo /bin/cp``; ``user-tier-prefs.json`` lives under
``{sharedDir}/{botId}/`` (evolve-owned, write ACL) and is written directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from evolve_util import assert_safe_sudo_dest, atomic_write_json

# ── Canonical legacy-tier ↔ role mapping ──────────────────────────────────────
# Single source of truth for the rename. The TS runtime (ModelRouter.ts) no
# longer carries this mapping at all: its legacy-shape fallback was removed
# 2026-08-15 (both pods verified migrated) and a tier0-tier3-shaped config now
# fails loudly there (LegacyTierShapeError → refused turns) instead of being
# translated. This migrator — plus the admin read side's TIER_TO_ROLE in
# primary_bot.py — is the sole owner of the mapping; keep those two in sync.

TIER_TO_ROLE: dict[str, str] = {
    "tier3": "fast",
    "tier2": "standard",
    "tier1": "power",
    "tier0": "judge",
}

TIER_TO_RUNG: dict[str, str] = {
    "tier3": "haiku-class",
    "tier2": "sonnet-class",
    "tier1": "opus-class",
    "tier0": "judge-class",  # judge owns its own rung (spec §Addendum 16)
}

# Cost-ordered (cheapest first) so the synthesized rungs[] array position
# stays a valid cost rank.
_RUNG_COST_ORDER: list[tuple[str, str]] = [
    ("tier3", "low"),
    ("tier2", "medium"),
    ("tier1", "high"),
]


# ── *-default rung-id canonicalization (spec Addendum 8 §D) ────────────────────
#
# The pod-default editor + easy-setup wizard used to write rungs with synthetic
# ids (``fast-default`` / ``standard-default`` / ``power-default`` /
# ``max-default`` / ``judge-default``) and no ``costClass``. The keyed merge
# dedupes rungs by ``id``, so these never overlaid the code-default rungs
# (``haiku-class`` / ``sonnet-class`` / ``opus-class`` / ``fable-class``) — they
# ACCUMULATED. This migration renames them to canonical ids, backfills
# ``costClass``, dedupes against the code default, and — for rungs that still
# carry the now-stale OLD buggy default model ids (the pod was default-seeded
# and never operator-edited) — re-derives the cluster from the CORRECTED code
# default. Operator edits (a cluster that differs from the old buggy seed) are
# left intact. Idempotent: a file already on canonical ids whose clusters match
# the corrected default is unchanged. ``models.embedding`` is a sibling key and
# is never touched (only ``rungs``/``roles``/``roleCaps`` are rewritten).

# Frozen snapshot of the OLD BUGGY default clusters — the exact model lists the
# pod-default editor / easy-setup seeded as ``*-default`` rungs before #2719
# corrected the catalog (openai/gpt-5.4-mini, openai/gpt-5.5 were non-existent
# ids). A rung whose canonicalized cluster SET equals the snapshot's set for its
# rung is a never-edited default seed → re-derive from the corrected catalog.
# Compared as SETS so the judge rung's secondary-first reorder still matches.
# Catalog DATA (three-homes rule, home #1) — a migration-only frozen constant.
# provider-literal-allow-begin: migration-only frozen catalog snapshot (home #1)
_OLD_BUGGY_DEFAULT_CLUSTERS: dict[str, frozenset[str]] = {
    "haiku-class": frozenset({
        "anthropic/claude-haiku-4-5",
        "openai/gpt-5.4-mini",
        "google/gemini-2.0-flash",
        "xai/grok-4-mini",
    }),
    "sonnet-class": frozenset({
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
        "xai/grok-4",
    }),
    # judge shared the sonnet-class rung pre-decouple (spec §Addendum 16), so a
    # never-edited buggy judge seed carries the same stale set. A live pod whose
    # canonicalized judge-class cluster equals this set is re-derived from the
    # corrected code default (drops the ghost gpt-5.5 etc.).
    "judge-class": frozenset({
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
        "xai/grok-4",
    }),
    "opus-class": frozenset({
        "anthropic/claude-opus-4-8",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
        "xai/grok-4",
    }),
    "fable-class": frozenset({
        "anthropic/claude-fable-5",
    }),
}
# provider-literal-allow-end


def _current_default_clusters() -> dict[str, list[str]]:
    """The CORRECTED code-default model clusters, keyed by canonical rung id.

    Pulled live from ``primary_bot.DEFAULT_MODEL_CATALOG`` (the single source of
    truth) so this migration tracks the catalog without a second copy. Returns
    ``{rung_id: [models...]}``. Empty on an import error (the re-derive step then
    no-ops — the rename/costClass-backfill still runs).
    """
    try:
        from primary_bot import DEFAULT_MODEL_CATALOG  # type: ignore
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for r in DEFAULT_MODEL_CATALOG.get("rungs", []):
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            models = r.get("models")
            if isinstance(models, list):
                out[r["id"]] = [m for m in models if isinstance(m, str) and m]
    return out


def migrate_default_catalog_shape(models: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Rename ``*-default`` rungs → canonical ids, backfill costClass, re-derive
    stale-default clusters, dedupe against the code default.

    Returns ``(new_models, changed)``. A no-op (``changed=False``) on a block
    without rungs, or already on canonical ids with correct costClass and
    non-stale clusters. ``models.embedding`` and any other sibling key survive
    untouched — only ``rungs``/``roles``/``roleCaps`` are rewritten.
    """
    if not isinstance(models, dict):
        return models, False
    rungs = models.get("rungs")
    if not isinstance(rungs, list) or not rungs:
        return models, False

    # Only act when there is something to fix — a non-canonical (``*-default``)
    # rung id, a canonical rung missing ``costClass``, or a rung still carrying
    # the OLD buggy default seed (stale ids to re-derive). A file that is already
    # canonical, costClass-complete, and non-stale is left ENTIRELY untouched so
    # the canonicalizer (which drops rungs no role points at) can't silently
    # discard a legitimate orphan/fallback rung an operator hand-authored.
    # Canonical rung ids = the old-buggy-seed keys PLUS ``judge-class`` (spec
    # §Addendum 16), which has no pre-#2719 buggy seed of its own but is a
    # first-class canonical rung. Without it a clean judge-class rung reads as a
    # non-canonical ``*-default`` id and churns needs_fix on every pass.
    _CANON_IDS = set(_OLD_BUGGY_DEFAULT_CLUSTERS.keys()) | {"judge-class"}
    needs_fix = False
    for r in rungs:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, str) or rid not in _CANON_IDS:
            needs_fix = True  # non-canonical id (e.g. ``fast-default``)
            break
        if not r.get("costClass"):
            needs_fix = True  # canonical id but blank costClass
            break
        cur_set = frozenset(m for m in r.get("models", []) if isinstance(m, str))
        if cur_set and cur_set == _OLD_BUGGY_DEFAULT_CLUSTERS.get(rid):
            needs_fix = True  # never-edited buggy seed → re-derive
            break
    if not needs_fix:
        return models, False

    try:
        from primary_bot import canonicalize_catalog_rung_ids  # type: ignore
    except Exception:
        # Canonicalizer unavailable (analyzer not importable) — skip safely.
        return models, False

    before = {
        "rungs": json.loads(json.dumps(rungs)),
        "roles": json.loads(json.dumps(models.get("roles") or {})),
    }
    canon = canonicalize_catalog_rung_ids({
        "rungs": rungs,
        "roles": models.get("roles") or {},
    })

    # Re-derive any rung whose canonicalized cluster equals the OLD buggy default
    # seed (never operator-edited) from the CORRECTED code default.
    current = _current_default_clusters()
    for rung in canon.get("rungs", []):
        rid = rung.get("id")
        old_seed = _OLD_BUGGY_DEFAULT_CLUSTERS.get(rid)
        new_models = current.get(rid)
        if old_seed is None or not new_models:
            continue
        cur_set = frozenset(m for m in rung.get("models", []) if isinstance(m, str))
        if cur_set == old_seed:
            rung["models"] = list(new_models)

    after = {"rungs": canon.get("rungs", []), "roles": canon.get("roles", {})}
    changed = after != before
    if not changed:
        return models, False

    out = dict(models)
    out["rungs"] = canon.get("rungs", [])
    out["roles"] = canon.get("roles", {})
    return out, True


def _is_already_migrated_models(models: dict[str, Any]) -> bool:
    """True when the models block already carries the rungs/roles shape."""
    return isinstance(models.get("rungs"), list) and bool(models.get("rungs"))


def migrate_models_block(models: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Rewrite a ``models`` block from tier keys to rungs/roles.

    Returns ``(new_models, changed)``. Idempotent: a block already in the
    rungs/roles shape (``rungs`` present) is returned unchanged with
    ``changed=False``. A block with neither ``tiers`` nor ``rungs`` (e.g.
    just a ``routing`` sub-block) still gets its routing keys renamed.
    """
    if not isinstance(models, dict):
        return models, False

    changed = False
    out: dict[str, Any] = {k: v for k, v in models.items() if k != "tiers"}

    tiers = models.get("tiers")

    # Mixed-shape reconciliation: a file that already carries ``rungs`` but
    # ALSO has a stale ``tiers`` key (the pollution an old-code freshness-
    # advisory "apply" wrote — see spec freshness addendum + the
    # oc_model write-path fix) gets the legacy ``tiers`` folded into the rung
    # clusters and the ``tiers`` key dropped. Without this the migrate command
    # skipped these files entirely (rungs present ⇒ "already migrated"), so the
    # pod stayed polluted. Idempotent: a clean new-shape file (no ``tiers``)
    # hits neither branch and is unchanged.
    if isinstance(tiers, dict) and _is_already_migrated_models(models):
        folded, fold_changed = _fold_legacy_tiers_into_rungs(out, tiers)
        if fold_changed:
            out = folded
            changed = True
        # ``out`` already excludes the ``tiers`` key (dropped at copy time
        # above), so the stale key is gone whether or not a fold occurred.
        if "tiers" in models:
            changed = True

    if isinstance(tiers, dict) and not _is_already_migrated_models(models):
        rungs: list[dict[str, Any]] = []
        roles: dict[str, Any] = {}
        # Emit rungs in cost order (cheapest first).
        for tier_key, cost_class in _RUNG_COST_ORDER:
            t = tiers.get(tier_key)
            tier_models = (t or {}).get("models") if isinstance(t, dict) else None
            if not isinstance(tier_models, list) or not tier_models:
                continue
            # Fold per-tier fallbacks into the rung cluster (primary first).
            fallbacks = t.get("fallbacks") if isinstance(t, dict) else None
            cluster = list(tier_models)
            if isinstance(fallbacks, list):
                for m in fallbacks:
                    if m not in cluster:
                        cluster.append(m)
            rung_id = TIER_TO_RUNG[tier_key]
            rungs.append({"id": rung_id, "models": cluster, "costClass": cost_class})
            roles[TIER_TO_ROLE[tier_key]] = rung_id

        # tier0 (judge): migrates to its OWN judge-class rung (spec §Addendum 16)
        # carrying the provider-diversity constraint, so a legacy judge list
        # keeps its independent order instead of folding into standard's rung.
        tier0 = tiers.get("tier0")
        tier0_models = (tier0 or {}).get("models") if isinstance(tier0, dict) else None
        if isinstance(tier0_models, list) and tier0_models:
            judge_rung = next((r for r in rungs if r["id"] == "judge-class"), None)
            if judge_rung is not None:
                for m in tier0_models:
                    if m not in judge_rung["models"]:
                        judge_rung["models"].append(m)
            else:
                rungs.append({"id": "judge-class", "models": list(tier0_models), "costClass": "medium"})
            roles["judge"] = {"rung": "judge-class", "provider": "not-standard"}

        # Keep rungs[] cost-ascending (cheapest first) so array position stays a
        # valid cost rank — judge-class (medium, appended after the ladder) sorts
        # back next to sonnet-class rather than trailing a higher-cost rung.
        _cost_rank = {"low": 0, "medium": 1, "high": 2, "premium": 3}

        def _rung_cost_rank(r: dict[str, Any]) -> int:
            c = r.get("costClass")
            return _cost_rank.get(c, len(_cost_rank)) if isinstance(c, str) else len(_cost_rank)

        rungs.sort(key=_rung_cost_rank)

        # Carry over any per-tier daily cap into roleCaps (tier1 → power).
        role_caps = _extract_role_caps_from_tiers(tiers)

        if rungs:
            out["rungs"] = rungs
            out["roles"] = roles
            if role_caps and "roleCaps" not in out:
                out["roleCaps"] = role_caps
            changed = True

    # Routing keys: *Tier → *Role (value mapped tierN → role), regardless of
    # whether tiers/rungs were present.
    routing = models.get("routing")
    if isinstance(routing, dict):
        new_routing, routing_changed = _migrate_routing(routing)
        if routing_changed:
            out["routing"] = new_routing
            changed = True

    # Canonicalize ``*-default`` rung ids → canonical ids + backfill costClass +
    # re-derive stale-default clusters (spec Addendum 8 §D). Runs on the post-
    # tier-migration ``out`` so a freshly-synthesized rungs block is also
    # canonical, and on an already-rungs/roles file (the live-pod case).
    canon_out, canon_changed = migrate_default_catalog_shape(out)
    if canon_changed:
        out = canon_out
        changed = True

    return out, changed


def _fold_legacy_tiers_into_rungs(
    models: dict[str, Any], tiers: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Fold a stale legacy ``tiers`` dict into an already-migrated ``models``.

    For each ``tierN`` in ``tiers``, resolve its role (TIER_TO_ROLE) → rung
    slug via the file's ``roles`` map (falling back to TIER_TO_RUNG), and set
    that rung's ``models`` to the legacy tier's models (folding any per-tier
    ``fallbacks`` after, dedup-preserving). Creates the role/rung when the
    tier names one the file hasn't adopted. Returns ``(new_models, changed)``;
    ``changed`` is True only when a rung's cluster actually differs.

    This is the WRITE-side reconcile that mirrors oc_model's
    ``apply_tiers_update_new_shape`` — the operator's polluted ``tiers`` write
    is what we're recovering, so its content wins on the named rungs.
    """
    out = dict(models)
    rungs = [dict(r) for r in out.get("rungs", []) if isinstance(r, dict)]
    roles = dict(out.get("roles") or {})
    changed = False

    def _rung_slug_for(tier_key: str) -> str:
        role = TIER_TO_ROLE.get(tier_key, "")
        entry = roles.get(role)
        if isinstance(entry, str) and entry:
            return entry
        if isinstance(entry, dict) and isinstance(entry.get("rung"), str):
            return entry["rung"]
        return TIER_TO_RUNG.get(tier_key, "")

    for tier_key, t in tiers.items():
        if not isinstance(t, dict):
            continue
        tier_models = t.get("models")
        if not isinstance(tier_models, list) or not tier_models:
            continue
        cluster = [m for m in tier_models if isinstance(m, str) and m]
        for m in (t.get("fallbacks") or []):
            if isinstance(m, str) and m and m not in cluster:
                cluster.append(m)
        if not cluster:
            continue
        rung_id = _rung_slug_for(tier_key)
        if not rung_id:
            continue
        existing = next((r for r in rungs if r.get("id") == rung_id), None)
        if existing is None:
            cost = next((c for k, c in _RUNG_COST_ORDER if k == tier_key), "medium")
            rungs.append({"id": rung_id, "models": cluster, "costClass": cost})
            changed = True
        elif existing.get("models") != cluster:
            existing["models"] = cluster
            changed = True
        # Ensure the role points at this rung.
        role = TIER_TO_ROLE.get(tier_key, "")
        if role and role not in roles:
            roles[role] = (
                {"rung": rung_id, "provider": "not-standard"}
                if role == "judge"
                else rung_id
            )
            changed = True

    if changed:
        out["rungs"] = rungs
        out["roles"] = roles
    return out, changed


def _extract_role_caps_from_tiers(tiers: dict[str, Any]) -> dict[str, Any]:
    """Pull a legacy per-tier maxPerDayPerBot into roleCaps (tier1 → power)."""
    caps: dict[str, Any] = {}
    t1 = tiers.get("tier1")
    if isinstance(t1, dict) and isinstance(t1.get("maxPerDayPerBot"), int):
        caps["power"] = {"maxPerDayPerBot": t1["maxPerDayPerBot"]}
    return caps


def _migrate_routing(routing: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Rename routing.*Tier → *Role and map tierN values to role IDs.

    Idempotent: a routing block already using ``*Role`` keys is unchanged.
    A ``null`` ambiguous value (use-bot-default) is preserved as null.
    """
    pairs = [
        ("maintenanceTier", "maintenanceRole"),
        ("backgroundTier", "backgroundRole"),
        ("ambiguousTier", "ambiguousRole"),
    ]
    changed = False
    out = dict(routing)
    for tier_key, role_key in pairs:
        if tier_key not in out:
            continue
        val = out.pop(tier_key)
        # Only overwrite the role key if it isn't already set explicitly.
        if role_key not in out:
            out[role_key] = TIER_TO_ROLE.get(val, val) if isinstance(val, str) else val
        changed = True
    return out, changed


def migrate_user_tier_override(override: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Rewrite a ``userTierOverride`` block to role-shaped fields.

    - ``dailyCap`` (legacy power cap) is preserved (the loader folds it into
      roleCaps.power); no rename needed, but it stays for back-compat read.
    - ``allowBotInitiated`` boolean → ``{power: <bool>, max: false}``
      (§max semantics #4 — max never inherits a legacy blanket-true).
    - ``defaultTier`` → ``defaultRole`` (value mapped tierN → role).
    """
    if not isinstance(override, dict):
        return override, False
    changed = False
    out = dict(override)

    abi = out.get("allowBotInitiated")
    if isinstance(abi, bool):
        out["allowBotInitiated"] = {"power": abi, "max": False}
        changed = True

    if "defaultTier" in out and "defaultRole" not in out:
        val = out.pop("defaultTier")
        out["defaultRole"] = TIER_TO_ROLE.get(val, val) if isinstance(val, str) else val
        changed = True

    return out, changed


def migrate_network(network: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate the ``models`` + top-level ``userTierOverride`` of network.json.

    Returns ``(new_network, changed)``. Idempotent. Mutates a shallow copy;
    the caller persists only when ``changed`` is True.
    """
    if not isinstance(network, dict):
        return network, False
    changed = False
    out = dict(network)

    models = out.get("models")
    if isinstance(models, dict):
        new_models, models_changed = migrate_models_block(models)
        if models_changed:
            out["models"] = new_models
            changed = True

    override = out.get("userTierOverride")
    if isinstance(override, dict):
        new_override, override_changed = migrate_user_tier_override(override)
        if override_changed:
            out["userTierOverride"] = new_override
            changed = True

    return out, changed


def migrate_evolve_tiers(tiers_file: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate a bot's ``evolve-tiers.json`` from tier keys to rungs/roles.

    Same shape as the network.json ``models`` block but at the file's top
    level (``tiers``/``routing``/``userTierOverride``/``cascade`` are
    top-level keys, not nested under ``models``). Idempotent.
    """
    if not isinstance(tiers_file, dict):
        return tiers_file, False

    # Reuse the models-block transform by treating the file's top level as
    # the models block (it carries tiers/rungs/roles/routing/roleCaps).
    new_file, changed = migrate_models_block(tiers_file)

    override = new_file.get("userTierOverride")
    if isinstance(override, dict):
        new_override, override_changed = migrate_user_tier_override(override)
        if override_changed:
            new_file = dict(new_file)
            new_file["userTierOverride"] = new_override
            changed = True

    return new_file, changed


def migrate_user_tier_prefs(prefs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Rewrite each user's ``defaultTier`` → ``defaultRole`` in a prefs file.

    Shape: ``{users: {<user_key>: {defaultTier?: str, defaultRole?: str}}}``.
    A per-user ``max`` default is valid (an explicit personal pull), so the
    value mapping is the plain tierN → role table with no max gating.
    Idempotent.
    """
    if not isinstance(prefs, dict):
        return prefs, False
    users = prefs.get("users")
    if not isinstance(users, dict):
        return prefs, False

    changed = False
    new_users: dict[str, Any] = {}
    for user_key, entry in users.items():
        if not isinstance(entry, dict):
            new_users[user_key] = entry
            continue
        if "defaultTier" in entry and "defaultRole" not in entry:
            e = dict(entry)
            val = e.pop("defaultTier")
            e["defaultRole"] = TIER_TO_ROLE.get(val, val) if isinstance(val, str) else val
            new_users[user_key] = e
            changed = True
        else:
            new_users[user_key] = entry

    if not changed:
        return prefs, False
    out = dict(prefs)
    out["users"] = new_users
    return out, True


# ── Filesystem drivers ────────────────────────────────────────────────────────


def _write_bot_owned_json(path: Path, data: dict[str, Any]) -> None:
    """Write a bot-owned JSON file via /tmp staging + sudo /bin/cp.

    The evolve user can't write the bot's home directly; stage in /tmp and
    copy with sudo (CLAUDE.md Writes pattern). Falls back to a direct write
    when the path is already writable (e.g. a test tmpdir).

    The DIRECT branch needs no symlink gate: ``atomic_write_json`` stages
    through ``tempfile.mkstemp`` (random name, ``O_CREAT|O_EXCL|O_NOFOLLOW``)
    and promotes with ``os.replace``, which does not follow a symlink at the
    destination — it replaces the link itself. The SUDO branch does need one,
    twice; see the D-2 comments inline.

    A refused destination raises ``PermissionError`` out of the whole
    migration rather than skipping that bot. That is the same contract this
    writer already had for the adjacent failure ("sudo /bin/cp failed"), and
    the right one for a security refusal: the operator runs this command
    interactively, with a dry-run available, so aborting is visible where a
    per-bot skip inside a fleet-wide loop would scroll past.
    """
    content = json.dumps(data, indent=2)
    try:
        # Direct write when permitted (tests / evolve-owned targets — and, on a
        # real pod, ROOT, because `sudo evolve-admin migrate-model-roles
        # --apply` can write any bot's home directly and so never raises
        # PermissionError). That last case is why the mode is pinned here too:
        # the sudo branch below repairs mode+ownership via
        # chown_chmod_bot_config, but a root-run migration never reaches it, and
        # a bare write_text takes the umask (measured: 0644 under 022, **0600**
        # under 077). At 0600 the bot cannot read its own evolve-tiers.json and
        # silently falls back to code-default routing — the exact failure
        # chown_chmod_bot_config exists to prevent, skipped because the direct
        # write succeeded. (#3566 post-hoc audit, finding B-4.)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data, mode=0o644)
        return
    except PermissionError:
        pass
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-tiers-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # D-2 gate — see evolve_util.assert_safe_sudo_dest. `cp` FOLLOWS a
        # symlink at the destination (no cp flag refuses to) and this cp runs
        # as ROOT, so an unchecked dest is an arbitrary root-write primitive:
        # the content lands on the link's target. The sudoers path pin does not
        # help — sudo matches the literal argv, and the argv is the
        # legitimate-looking link path. Refuse rather than write through a link.
        # (#3566 post-hoc audit, finding D-2; same primitive closed in
        # oc_model._save_tiers_file, which writes this same file.)
        assert_safe_sudo_dest(path)
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise PermissionError(f"sudo /bin/cp failed: {r.stderr.strip()}")
        # A bare `cp` to a *fresh* dest lands it root:wheel 0600 — unreadable
        # by the bot that runs oc_model.py to read its own tiers. Restore bot
        # ownership + 0644 so the bot keeps access (no-op when the dest already
        # existed bot-owned). Shared contract / sudoers grants: §4b.
        #
        # Re-assert the gate first, and note this is NOT redundant with the
        # check above: `chown`/`chmod` without `-h` follow a symlink ARGUMENT
        # just as `cp` follows a symlink DEST, and a `cp` that ran through a
        # link leaves the LINK in place — so a plant that landed inside the
        # (narrow, TOCTOU) window between the first check and the cp is still
        # visible here, and stopping now spares the victim the 0644 relabel on
        # top of the content overwrite. Skip loudly: the cp already succeeded,
        # so raising would misreport a completed write as a failed one.
        try:
            assert_safe_sudo_dest(path)
        except PermissionError as e:
            print(
                f"[migrate-model-roles] ownership repair skipped: {e}",
                file=sys.stderr,
            )
            return
        # NOT reached on Linux, and that is a separate open bug, verified here
        # rather than assumed: chown_chmod_bot_config derives the account from
        # the path via `_bot_user_from_path`, which is still hardcoded to
        # `/Users` (measured: `/home/<bot>/.openclaw/evolve-tiers.json` → None →
        # the helper returns False without issuing chown or chmod). So on the
        # VPS this branch leaves a freshly-cp'd tiers file root:wheel 0600 and
        # the bot silently falls back to code-default routing. Fixing it is a
        # one-site change in secret_config_perms, tracked separately; the same
        # gap is called out from oc_model._restore_tiers_bot_ownership.
        from .secret_config_perms import chown_chmod_bot_config
        chown_chmod_bot_config(path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _write_shared_json(path: Path, data: dict[str, Any]) -> None:
    """Write an evolve-owned JSON file under {sharedDir} directly (atomic).

    Mode is PINNED to 0644, not left to the umask. ``user-tier-prefs.json`` is
    read by the BOT's gateway plugin on every routing decision, and
    ``setStandingUserTierDefault`` read-merges it before its own rename — an
    evolve-owned 0600 file is invisible to the bot user, so the per-user pref
    silently never applies and the plugin's writer refuses to write. The other
    writer of this exact file (``evo.user_tier_prefs.set_user_pref``) pins 0644
    for that documented reason; this one did not, and a bare
    ``write_text`` + ``replace`` takes the umask (measured: 0644 under 022,
    **0600** under 077) AND overwrites whatever mode the destination had.

    That mattered here more than in an ordinary writer: this is the
    ``migrate-model-roles --apply`` path, which the operator runs once, as root,
    across every bot on a pod — so a tightened umask would have silently
    disabled every user's standing tier choice, i.e. the migration breaking the
    prefs it exists to preserve. (#3566 post-hoc audit of the config write
    surface, finding B-4.)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data, mode=0o644)


def _bot_is_custom(tiers_doc: dict[str, Any]) -> bool:
    """Mirror of ``primary_bot.bot_has_custom_tiers`` on a RAW tiers doc.

    Custom is decided purely on a non-empty ``rungs`` array — which is exactly
    why writing ``rungs`` is itself the Custom flip (spec §Addendum 5).

    Non-dict input (a hand-mangled ``evolve-tiers.json`` that parsed to a list
    or a scalar) reads as not-Custom rather than raising — every other entry
    point in this module guards the same way, and the migration's job on a file
    it cannot understand is to leave it alone, not to abort a fleet-wide run
    halfway through.
    """
    return isinstance(tiers_doc, dict) and _is_already_migrated_models(tiers_doc)


def _pod_auto_upgrade_block(network: dict[str, Any]) -> dict[str, Any] | None:
    """The pod's ``models.autoUpgrade`` block, or ``None`` when absent/empty."""
    if not isinstance(network, dict):
        return None
    models = network.get("models")
    block = models.get("autoUpgrade") if isinstance(models, dict) else None
    if isinstance(block, dict) and block:
        return dict(block)
    return None


def _carry_pod_auto_upgrade_on_custom_flip(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    pod_block: dict[str, Any] | None,
) -> bool:
    """Seed the pod's auto-upgrade block onto a bot THIS migration made Custom.

    Lifecycle rule 1 (spec-model-auto-upgrade-2026-07-30 §Scope), mirrored from
    ``oc_model._carry_pod_auto_upgrade`` and ``provisioning.
    seed_model_config_if_empty``: a bot whose ``evolve-tiers.json`` carries
    ``rungs`` is Custom, and ``model_auto_upgrade.bot_policy`` does NOT give a
    Custom bot the pod's ``enabled`` — a Custom bot that never set the key
    resolves to the CODE default (``false``). A legacy-shaped file has no
    ``rungs``, so the bot follows the pod; the migration writes ``rungs``, so
    without this carry ``migrate-model-roles --apply`` silently flips every
    migrated bot off pod auto-upgrade.

    This is a FIFTH carry site: the migrator writes ``evolve-tiers.json``
    directly, bypassing the ``oc_cli`` → ``oc_model`` seam where the other four
    live (#3580 widened that seam and explicitly could not reach here). Unlike
    ``oc_model`` this runs as ``evolve-admin`` and can read ``network.json``
    itself, so no ``podAutoUpgrade`` transport hop is needed.

    Three guards, each of which is the point:

    - **Transition only.** A bot that was ALREADY new-shape (already Custom)
      and is only getting canonical rung-id renames is left alone. Seeding it
      would switch auto-upgrade *on* for a Custom bot that deliberately has no
      block — as wrong as switching it off.
    - **Never overwrite the bot's own block** (``oc_model``'s
      ``caller_set=False`` path — there is no caller-supplied block here, so
      any existing block is the bot's own).
    - **No pod block, or an empty one → carry nothing**; the bot then resolves
      to the code default, same as the pod.

    Mutates ``after`` in place. Returns True when a block was seeded.
    """
    if not pod_block:
        return False
    if _bot_is_custom(before) or not _bot_is_custom(after):
        return False
    existing = after.get("autoUpgrade")
    if isinstance(existing, dict) and existing:
        return False
    after["autoUpgrade"] = dict(pod_block)
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read JSON with a sudo /bin/cat fallback (CLAUDE.md Reads pattern)."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except PermissionError:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)], capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout.strip():
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                return None
        return None
    except (OSError, json.JSONDecodeError):
        return None


def run_migration(
    *,
    network: dict[str, Any],
    network_path: Path,
    shared_dir: Path,
    bot_homes: dict[str, Path],
    apply_changes: bool,
    save_network_fn,
) -> list[str]:
    """Drive the migration across all three surfaces.

    ``bot_homes`` maps bot_id → resolved home Path (the caller resolves the
    macOS account). ``save_network_fn`` is injected so the CLI can pass
    ``config.save_network`` and tests can pass a stub. Returns a list of
    human-readable change lines (one per surface that would change / changed).
    """
    changes: list[str] = []

    # 1. network.json
    new_network, net_changed = migrate_network(network)
    if net_changed:
        changes.append("network.json: tier keys → roles + canonical rung ids")
        if apply_changes:
            save_network_fn(new_network, network_path)

    # 2. each bot's ~/.openclaw/evolve-tiers.json
    #
    # The pod block is read from the post-migration network — identical to the
    # input's today (``migrate_models_block`` copies every key but ``tiers``),
    # and it is the value that will actually be on disk, which is what a bot
    # flipped Custom by this run should inherit. ``migrate_evolve_tiers`` stays
    # a pure transform with no pod context — the carry lives here in the driver.
    pod_auto_upgrade = _pod_auto_upgrade_block(new_network)
    for bot_id, home in sorted(bot_homes.items()):
        tiers_path = home / ".openclaw" / "evolve-tiers.json"
        data = _read_json(tiers_path)
        if data is None:
            continue
        new_data, changed = migrate_evolve_tiers(data)
        seeded = _carry_pod_auto_upgrade_on_custom_flip(
            before=data, after=new_data, pod_block=pod_auto_upgrade,
        )
        # One line per SURFACE (the CLI counts these as "surface(s)"), so the
        # carry is reported as a clause on the file's own line rather than as a
        # second phantom surface. Dry-run and --apply build it identically —
        # the seed is never something --apply does silently.
        parts: list[str] = []
        if changed:
            parts.append("tier keys → roles + canonical rung ids")
        if seeded:
            parts.append("carry pod models.autoUpgrade (bot becomes Custom)")
        if parts:
            changes.append(f"{bot_id}: evolve-tiers.json " + "; ".join(parts))
            if apply_changes:
                _write_bot_owned_json(tiers_path, new_data)

    # 3. each bot's {sharedDir}/{botId}/user-tier-prefs.json
    for bot_id in sorted(bot_homes.keys()):
        prefs_path = shared_dir / bot_id / "user-tier-prefs.json"
        data = _read_json(prefs_path)
        if data is None:
            continue
        new_data, changed = migrate_user_tier_prefs(data)
        if changed:
            changes.append(f"{bot_id}: user-tier-prefs.json defaultTier → defaultRole")
            if apply_changes:
                _write_shared_json(prefs_path, new_data)

    return changes


# ── CLI command registration ──────────────────────────────────────────────────
# The ``migrate-model-roles`` click command lives here (not in cli.py) so the
# command body sits beside the migration it drives. cli.py calls
# ``register_cli(main)`` once to attach it to the top-level group.


def register_cli(main) -> None:  # noqa: ANN001 — main is a click.Group
    """Attach the ``migrate-model-roles`` command to the top-level CLI group."""
    import click

    from rich.console import Console

    from .config import (
        DEFAULT_SHARED_DIR,
        bot_home as _bot_home,
        load_network,
        save_network,
    )

    console = Console()

    @main.command("migrate-model-roles")
    @click.option("--apply", "apply_changes", is_flag=True, default=False,
                  help="Actually rewrite the config (dry-run by default).")
    @click.pass_context
    def migrate_model_roles_cmd(ctx: click.Context, apply_changes: bool) -> None:
        """Rewrite persisted model-tier keys to role IDs + canonical rung ids.

        Migrates three surfaces from the numeric-tier scheme to the rungs/roles
        scheme (spec-model-rungs-and-roles-2026-06-09):

          - network.json            models.tiers → rungs/roles; routing.*Tier
                                    → *Role; userTierOverride → role-shaped
          - <bot>/.openclaw/evolve-tiers.json   same, per bot
          - {sharedDir}/{botId}/user-tier-prefs.json  defaultTier → defaultRole

        Mapping: tier3→fast, tier2→standard, tier1→power, tier0→judge;
        userTierOverride.dailyCap→roleCaps.power; allowBotInitiated(bool)→
        {power: <bool>, max: false}.

        ALSO (spec Addendum 8 §D) reconciles synthetic ``*-default`` rung ids
        (``fast-default`` etc., written by the old pod-default editor / easy-setup)
        to canonical ids (``haiku-class`` / ``sonnet-class`` / ``opus-class`` /
        ``fable-class``) so a saved pod/bot catalog OVERLAYS the code default
        instead of accumulating; backfills missing ``costClass``; and re-derives
        rungs still carrying the OLD buggy default seed (pre-#2719 stale model ids)
        from the corrected code default. Operator-edited clusters are preserved.
        ``models.embedding`` is never touched.

        Idempotent — a config already on canonical rungs/roles with costClass set
        is left untouched, so it is safe to run on every deploy. Default is
        dry-run; pass --apply to write.
        """
        network_path: Path = ctx.obj["network_path"]
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir") or DEFAULT_SHARED_DIR)

        bot_ids = sorted((network.get("bots") or {}).keys())
        bot_homes: dict[str, Path] = {}
        for bot_id in bot_ids:
            try:
                bot_homes[bot_id] = _bot_home(bot_id, network)
            except Exception as exc:  # noqa: BLE001 — skip unresolvable bots, log it
                console.print(f"[yellow]skip {bot_id}: cannot resolve home ({exc})[/]")

        changes = run_migration(
            network=network,
            network_path=network_path,
            shared_dir=shared_dir,
            bot_homes=bot_homes,
            apply_changes=apply_changes,
            save_network_fn=save_network,
        )

        if not changes:
            console.print("[green]✓ all model config already on the rungs/roles shape[/]")
            return

        verb = "migrated" if apply_changes else "would migrate"
        console.print(f"[yellow]{verb} {len(changes)} surface(s):[/]")
        for line in changes:
            console.print(f"  - {line}")
        if not apply_changes:
            console.print()
            console.print("[dim]dry-run — re-run with --apply to rewrite[/]")
        else:
            console.print(f"[green]✓ migrated {len(changes)} surface(s)[/]")
