"""arbiter.appliers.adopt_model — Adopt a discovered model into the pod catalog.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum (A).

The ``model_discovery`` generator emits an :class:`AdoptModel` proposal when a
provider's live listing surfaces a model in no rung. On approval the operator's
choices (role mapping + cap seed) are patched onto the action by the ``/act``
endpoint; this applier writes them to ``network.json::models``:

  - **rungs** — creates the suggested rung at the suggested cost-rank position,
    or extends an existing same-slug rung's cluster (dedup, idempotent).
  - **roles** — when ``role_mapping`` is a real role (not ``"none"``), re-points
    that role at the new rung. ``judge`` writes the structured
    ``{rung, provider:"not-standard"}`` form and is validated for provider
    diversity against the ``standard`` role before the write.
  - **roleCaps** — when mapping ``max``/``power`` with a ``cap_per_day``, seeds
    ``roleCaps.<role>.maxPerDayPerBot``.

All writes go through the atomic temp+rename network.json save helper
(``evolve_config._patch_network_json``). Idempotent: a re-apply that finds the
model already in the target rung and the role/cap already pointed is a no-op.
The applied config delta is recorded on the proposal via ``result.details``
(arbiter.apply stores it under ``provenance.signals._apply_details``).
"""

from __future__ import annotations

from typing import Any, Callable

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import AdoptModel


# Valid role ids a discovered rung may be mapped to. ``none`` means
# "adopt as a dormant catalog entry" (no role re-point).
_VALID_ROLES = {"fast", "standard", "power", "judge", "max"}

# Roles that carry a per-day cap seed when mapped.
_CAP_ROLES = {"max", "power"}

# Default cap seed for the ``max`` role when the operator maps it without
# supplying an explicit cap (spec §A.2: default roleCaps.max.maxPerDayPerBot=5).
_DEFAULT_MAX_CAP = 5

# Valid costClass values for a newly-created rung.
_VALID_COST_CLASSES = {"low", "medium", "high", "premium"}

# Placeholder rung slugs that must NEVER persist as a real rung id (Addendum 13:
# the ``new-rung`` footgun). These are config-placeholder tokens, not provider
# names. The canonical set lives in ``model_discovery.PLACEHOLDER_RUNG_SLUGS``;
# this is a defensive fallback if that import is unavailable at apply time.
_PLACEHOLDER_RUNG_FALLBACK = frozenset(
    {"", "new-rung", "new_rung", "newrung", "none", "null", "tbd", "unplaced"}
)


def _is_placeholder_rung_slug(slug: str | None) -> bool:
    """True when ``slug`` is empty / a placeholder that must not mint a rung.

    A placeholder slug means the model has no real tier yet (an unplaceable
    discovery), so adopting it would create a dead rung no role points at — the
    cruft-by-construction bug Addendum 13 removes. Reuses the discovery contract
    set when importable, with a local fallback so the guard holds even if the
    import fails."""
    norm = (slug or "").strip().lower()
    try:
        from model_discovery import PLACEHOLDER_RUNG_SLUGS  # type: ignore

        return norm in PLACEHOLDER_RUNG_SLUGS
    except Exception:
        return norm in _PLACEHOLDER_RUNG_FALLBACK


# ── Injectable IO (tests stub these to avoid touching the real network.json) ──
# Reader returns the parsed network dict; writer persists it. Production wires
# to evolve_config helpers lazily.
_read_network_fn: Callable[[], dict] | None = None
_write_network_fn: Callable[[dict], None] | None = None


def set_network_io(
    read_fn: Callable[[], dict] | None,
    write_fn: Callable[[dict], None] | None,
) -> None:
    """Override the network.json read/write IO — for tests."""
    global _read_network_fn, _write_network_fn
    _read_network_fn = read_fn
    _write_network_fn = write_fn


def _read_network() -> dict:
    global _read_network_fn
    if _read_network_fn is None:
        from evolve_config import load_config

        _read_network_fn = load_config
    data = _read_network_fn() or {}
    return data if isinstance(data, dict) else {}


def _write_models_block(models: dict) -> None:
    """Persist ``network.json::models`` atomically via the canonical helper."""
    global _write_network_fn
    if _write_network_fn is not None:
        # Tests inject a writer that takes the full network dict; mirror that
        # contract by reading + splicing in-process.
        net = _read_network()
        net["models"] = models
        _write_network_fn(net)
        return
    from evolve_config import _patch_network_json, resolve_network_path

    _patch_network_json(resolve_network_path(), ["models"], models)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _provider_of(model: str) -> str | None:
    """Provider prefix of a qualified model string ("anthropic/x" -> "anthropic").

    A bare id (no slash) returns None — the same convention as
    model_registry._provider_of and the runtime resolver
    (ModelRouter._providerOf), so this validation accepts exactly what
    the router would resolve. No provider is ever presumed for bare ids.
    """
    return model.split("/", 1)[0].lower() if "/" in model else None


def _qualified(action: AdoptModel) -> str:
    """``"{provider}/{model_id}"`` — re-derived, never trusted from input."""
    mid = action.model_id
    if "/" in mid:
        return mid
    return f"{action.provider}/{mid}"


def _validate_judge_diversity(models_block: dict, candidate_rung_id: str) -> str | None:
    """Return an error string if mapping ``judge`` to ``candidate_rung_id``
    would violate provider diversity, else None.

    The judge role must resolve to at least one model whose provider differs
    from the ``standard`` role's primary provider (spec §Roles, judge row).
    Mirrors the CLI check in cli.py::map-role.
    """
    rungs = models_block.get("rungs") or []
    rung_by_id = {
        r.get("id"): r for r in rungs if isinstance(r, dict) and r.get("id")
    }
    roles = models_block.get("roles") or {}
    std_rung_id = roles.get("standard") or "sonnet-class"
    if isinstance(std_rung_id, dict):
        std_rung_id = std_rung_id.get("rung", "sonnet-class")
    std_models = (rung_by_id.get(std_rung_id) or {}).get("models") or []
    std_primary = std_models[0] if std_models else ""
    std_provider = _provider_of(std_primary)

    target_models = (rung_by_id.get(candidate_rung_id) or {}).get("models") or []
    diverse = [m for m in target_models if _provider_of(m) != std_provider]
    if not diverse:
        return (
            f"judge role requires at least one model in rung "
            f"{candidate_rung_id!r} from a provider other than standard's "
            f"({(std_provider or 'unknown')!r}); rung has only same-provider models. "
            f"Add a cross-provider model to the rung before mapping judge."
        )
    return None


def _splice_rung(
    models_block: dict,
    rung_slug: str,
    model: str,
    position: int,
    cost_class: str,
    insert_before: str | None = None,
) -> tuple[bool, str]:
    """Create the rung at ``position`` (or extend an existing same-slug rung's
    cluster). Returns ``(changed, note)``. Idempotent: a model already in the
    target rung at the intended cluster position yields ``(False, ...)``.

    ``insert_before`` controls cluster placement when extending an existing
    rung. ``None`` (default) appends — a discovered alternative joins as the
    lowest-priority fallback and never preempts the operator's chosen primary.
    A model id splices ``model`` immediately AHEAD of that predecessor (the
    version-freshness path passes the model being upgraded), so the resolver —
    which routes to the FIRST credentialed member of ``models[]``, not the
    newest — actually moves to the newer version. If ``model`` is already in the
    cluster but sits behind ``insert_before``, it is promoted ahead of it (an
    upgrade left half-applied by a prior append is repaired on re-apply).
    """
    rungs = models_block.setdefault("rungs", [])
    if not isinstance(rungs, list):
        rungs = []
        models_block["rungs"] = rungs

    existing = next(
        (r for r in rungs if isinstance(r, dict) and r.get("id") == rung_slug), None
    )
    if existing is not None:
        cluster = existing.setdefault("models", [])
        if not isinstance(cluster, list):
            cluster = []
            existing["models"] = cluster
        # Where should the model land? Ahead of its predecessor when an upgrade
        # names one and it is present; otherwise at the tail (append).
        target = (
            cluster.index(insert_before)
            if insert_before and insert_before in cluster
            else None
        )
        if model in cluster:
            cur = cluster.index(model)
            if target is None or cur < target:
                # Already at/ahead of the intended slot → routing already prefers
                # it → genuine no-op.
                return False, f"{model} already in rung {rung_slug!r}"
            cluster.pop(cur)
            # Recompute the predecessor index after the removal shifted it.
            tgt = cluster.index(insert_before) if insert_before in cluster else 0
            cluster.insert(tgt, model)
            return True, f"promoted {model} ahead of {insert_before!r} in rung {rung_slug!r}"
        if target is not None:
            cluster.insert(target, model)
            return True, f"inserted {model} ahead of {insert_before!r} in rung {rung_slug!r}"
        cluster.append(model)
        return True, f"extended rung {rung_slug!r} with {model}"

    # New rung. Clamp the insertion index into [0, len].
    idx = max(0, min(int(position), len(rungs)))
    rungs.insert(
        idx,
        {"id": rung_slug, "models": [model], "costClass": cost_class},
    )
    return True, f"created rung {rung_slug!r} at position {idx} with {model}"


class AdoptModelApplier:
    # NOTE: capture_snapshot/revert below are currently UNREACHABLE in
    # production — AdoptModel proposals carry claim=None, and arbiter.apply
    # only snapshots claimed proposals (reversibility is declared "manual":
    # one atomic validated write, undo = remove the rung/role via models set).
    # Kept for the future claim-wiring; do not assume auto-revert exists.
    def capture_snapshot(self, action: AdoptModel, bot_id: str) -> dict:
        """Snapshot the whole prior ``models`` block so revert restores the
        exact pre-apply catalog regardless of which sub-keys were touched."""
        net = _read_network()
        prior_models = net.get("models")
        return {
            "action_kind": "AdoptModel",
            "bot_id": bot_id,
            "prior_models": prior_models if isinstance(prior_models, dict) else {},
        }

    def apply(self, action: AdoptModel, bot_id: str) -> ApplyResult:
        if not action.provider or not action.model_id or not action.rung_slug:
            return ApplyResult(
                ok=False,
                details={
                    "provider": action.provider,
                    "model_id": action.model_id,
                    "rung_slug": action.rung_slug,
                },
                message="AdoptModel requires provider, model_id, and rung_slug",
            )

        # Guard the ``new-rung`` footgun (Addendum 13): refuse a placeholder /
        # empty slug rather than mint a dead rung no role points at. An
        # unplaceable discovery carries no real tier — the operator must choose
        # one (Bite 2's card) before it can be adopted. Reject cleanly (flag),
        # never write.
        if _is_placeholder_rung_slug(action.rung_slug):
            return ApplyResult(
                ok=False,
                details={"fail_action": "flag", "rung_slug": action.rung_slug},
                message=(
                    f"refusing to adopt {_qualified(action)} into placeholder "
                    f"tier {action.rung_slug!r}: this model has no role-bearing "
                    f"tier yet, so adopting it would create a tier no role can "
                    f"use. Choose a real role/tier before adopting."
                ),
            )

        role = (action.role_mapping or "none").strip().lower()
        if role != "none" and role not in _VALID_ROLES:
            return ApplyResult(
                ok=False,
                details={"role_mapping": action.role_mapping},
                message=(
                    f"unknown role_mapping {action.role_mapping!r}; expected "
                    f"'none' or one of {sorted(_VALID_ROLES)}"
                ),
            )

        cost_class = (action.cost_class or "medium").strip().lower()
        if cost_class not in _VALID_COST_CLASSES:
            cost_class = "medium"

        qualified = _qualified(action)

        net = _read_network()
        models_block = net.get("models")
        models_block = dict(models_block) if isinstance(models_block, dict) else {}

        delta: dict[str, Any] = {
            "qualified_id": qualified,
            "rung_slug": action.rung_slug,
            "role_mapping": role,
        }

        # ── 1. rung create / extend ───────────────────────────────────────────
        rung_changed, rung_note = _splice_rung(
            models_block, action.rung_slug, qualified, action.position, cost_class,
            insert_before=action.insert_before,
        )
        delta["rung"] = rung_note

        # ── 2. judge diversity validation (BEFORE any role write) ─────────────
        role_changed = False
        if role == "judge":
            err = _validate_judge_diversity(models_block, action.rung_slug)
            if err is not None:
                # Reject cleanly — no write. The proposal is flagged for
                # operator review rather than silently mis-applied.
                return ApplyResult(
                    ok=False,
                    details={"fail_action": "flag", "rung_slug": action.rung_slug},
                    message=err,
                )

        # ── 3. role re-point ──────────────────────────────────────────────────
        if role != "none":
            roles = models_block.setdefault("roles", {})
            if not isinstance(roles, dict):
                roles = {}
                models_block["roles"] = roles
            if role == "judge":
                existing = roles.get("judge")
                if isinstance(existing, dict):
                    if existing.get("rung") != action.rung_slug:
                        existing["rung"] = action.rung_slug
                        existing.setdefault("provider", "not-standard")
                        role_changed = True
                    roles["judge"] = existing
                else:
                    roles["judge"] = {
                        "rung": action.rung_slug,
                        "provider": "not-standard",
                    }
                    role_changed = True
            else:
                if roles.get(role) != action.rung_slug:
                    roles[role] = action.rung_slug
                    role_changed = True
            delta["role"] = f"{role} -> {action.rung_slug}"

        # ── 4. cap seed (max/power only) ──────────────────────────────────────
        cap_changed = False
        if role in _CAP_ROLES:
            cap = action.cap_per_day
            if cap is None and role == "max":
                cap = _DEFAULT_MAX_CAP
            if cap is not None:
                caps = models_block.setdefault("roleCaps", {})
                if not isinstance(caps, dict):
                    caps = {}
                    models_block["roleCaps"] = caps
                role_caps = caps.setdefault(role, {})
                if not isinstance(role_caps, dict):
                    role_caps = {}
                    caps[role] = role_caps
                if role_caps.get("maxPerDayPerBot") != int(cap):
                    role_caps["maxPerDayPerBot"] = int(cap)
                    cap_changed = True
                delta["cap"] = {role: int(cap)}

        if not (rung_changed or role_changed or cap_changed):
            # Pure no-op (idempotent re-apply). Report ok=True so the proposal
            # closes cleanly rather than flagging for review — the desired
            # end-state already holds.
            return ApplyResult(
                ok=True,
                details={"noop": True, **delta},
                message=(
                    f"no-op: {qualified} already adopted into rung "
                    f"{action.rung_slug!r}"
                    + (f" and role {role!r} already mapped" if role != "none" else "")
                ),
            )

        try:
            _write_models_block(models_block)
        except Exception as e:  # noqa: BLE001 — surface write failure
            return ApplyResult(
                ok=False,
                details=dict(delta),
                message=f"failed to write network.json models block: {e}",
            )

        parts = [rung_note]
        if role != "none":
            parts.append(f"mapped role {role} -> {action.rung_slug}")
        if cap_changed:
            parts.append(f"seeded {role} cap = {delta.get('cap', {}).get(role)}")
        return ApplyResult(
            ok=True,
            details=dict(delta),
            message="adopted " + qualified + ": " + "; ".join(parts),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        if "prior_models" not in snapshot:
            return RevertResult(ok=False, message="snapshot missing prior_models")
        prior = snapshot.get("prior_models")
        prior = prior if isinstance(prior, dict) else {}
        try:
            _write_models_block(prior)
        except Exception as e:  # noqa: BLE001
            return RevertResult(
                ok=False,
                details={"bot_id": bot_id},
                message=f"failed to restore models block: {e}",
            )
        return RevertResult(
            ok=True,
            details={"bot_id": bot_id},
            message="restored prior network.json models block",
        )


register_applier("AdoptModel", AdoptModelApplier())
