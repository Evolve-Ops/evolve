"""evolve_admin.config_intent — Per-bot config-intent sidecar I/O.

Spec: docs/spec-config-intent-system-2026-05-21.md.

Records *why* a non-baseline permission-config value is set, so generators
like ``auth_drift_filler`` can distinguish deliberate operator deviations
from actual drift and stop proposing the same revert every sweep.

Sidecar canonical: ``{shared_dir}/config_intents/<bot_id>.json`` (one file
per bot, owned by the ``evolve`` user, atomic temp+rename writes).
Inline mirror (visibility-only summary): ``network.json::bots.<id>.config_intents``.
Sidecar is authoritative; the mirror is best-effort and self-heals.

Phase 1 ships the data + helpers without plugin-coupled validity logic
(Phase 5 adds that). ``intent_still_valid`` therefore returns True for
non-coupled intents, which is every intent v1 actually records.

Module placement (open question §10.5): kept at the flat
``evolve_admin.config_intent`` path rather than promoting ``config.py``
to a package, to keep Phase 1's blast radius small. The spec named
``evolve_admin/config/intent.py``; the open question explicitly leaves
placement to PR-time discretion.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso

DEFAULT_SHARED_DIR = Path("/Users/Shared/evolve")
SCHEMA_VERSION = 1

# Field paths this module accepts. Mirrors the permission-posture set the
# L2 applier writes (UpdatePermissionConfigApplier in
# packages/analyzer/arbiter/appliers/permissions.py) plus the plugin-install
# entries written by bot_action(action="pin_plugin_version"). Kept narrow because
# v1's only consumer is auth_drift_filler — accepting arbitrary dotpaths
# would invite intent records the generator can't use. Extends naturally
# in Phase 5 when cron_caps_filler / cache_ttl_tuner come online.
#
# ``agents.`` added 2026-05-31 for the cacheRetention L2 applier
# (UpdateAgentDefaults — PR B). The matching consumer is PR F's
# ``cache_ttl_tuner`` generator: with the intent record in place, it
# won't re-propose the inverse flip after an L2 apply lands.
_ALLOWED_FIELD_PREFIXES: tuple[str, ...] = (
    "tools.", "commands.", "plugins.", "agents.",
)


def _new_intent_id() -> str:
    return f"intent-{secrets.token_hex(3)}"


def _resolve_shared(shared_dir: Path | None) -> Path:
    return Path(shared_dir) if shared_dir is not None else DEFAULT_SHARED_DIR


def _sidecar_path(bot_id: str, shared_dir: Path | None) -> Path:
    return _resolve_shared(shared_dir) / "config_intents" / f"{bot_id}.json"


def _validate_field_path(field_path: str) -> None:
    if not isinstance(field_path, str) or not field_path:
        raise ValueError("field_path must be a non-empty string")
    if not any(field_path.startswith(p) for p in _ALLOWED_FIELD_PREFIXES):
        raise ValueError(
            f"field_path {field_path!r} outside accepted prefixes "
            f"{_ALLOWED_FIELD_PREFIXES}; extend _ALLOWED_FIELD_PREFIXES "
            f"in evolve_admin.config_intent if you need a new surface."
        )


def _load_sidecar(bot_id: str, shared_dir: Path | None) -> dict[str, Any]:
    """Return the sidecar dict, initializing if missing.

    Raises ``RuntimeError`` on schema-version mismatch so a forward-migrated
    sidecar isn't silently truncated by an older reader. Treats malformed
    JSON the same way for the same reason — fail loud rather than discard.
    """
    path = _sidecar_path(bot_id, shared_dir)
    if not path.exists():
        return {"bot_id": bot_id, "schema_version": SCHEMA_VERSION,
                "intents": [], "intents_archive": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Cannot read config-intent sidecar at {path}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Config-intent sidecar at {path} is not a JSON object")
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise RuntimeError(
            f"Config-intent sidecar at {path} has schema_version={sv!r}; "
            f"this reader supports {SCHEMA_VERSION}."
        )
    data.setdefault("intents", [])
    data.setdefault("intents_archive", [])
    return data


def _atomic_write_sidecar(bot_id: str, data: dict[str, Any],
                          shared_dir: Path | None) -> None:
    """Write the sidecar via temp+rename inside the same directory.

    ``{shared_dir}/config_intents/`` is owned by the evolve user (see
    CLAUDE.md "Arbiter on-disk layout" precedent), so no /tmp staging or
    sudo is needed. ``os.replace`` is atomic on POSIX when source and
    destination are on the same filesystem — staging in the same dir
    guarantees that.
    """
    dest = _sidecar_path(bot_id, shared_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{bot_id}-",
                                suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _find_intent_index(data: dict[str, Any], field_path: str) -> int:
    for i, entry in enumerate(data.get("intents") or []):
        if isinstance(entry, dict) and entry.get("field_path") == field_path:
            return i
    return -1


def _sync_inline_mirror(bot_id: str, data: dict[str, Any],
                        network_path: Path | None = None) -> None:
    """Update ``network.json::bots.<bot_id>.config_intents`` to mirror the sidecar.

    Best-effort: the sidecar is authoritative, so a mirror-write failure
    must not roll back the sidecar update. The inline annotation is just
    a visibility helper — operators reading network.json see "intent
    recorded" at a glance without consulting the sidecar.
    """
    try:
        from evolve_admin.config import load_network, save_network, DEFAULT_NETWORK_CONFIG
    except ImportError:
        return
    path = network_path or DEFAULT_NETWORK_CONFIG
    try:
        network = load_network(path)
    except Exception:
        return
    bots = network.get("bots")
    if not isinstance(bots, dict) or bot_id not in bots:
        # The mirror is intentionally per-known-bot. We don't materialize
        # bot entries from intent records — that would invert the source
        # of truth (network.json defines bots; intents annotate them).
        return
    bot_entry = bots[bot_id]
    if not isinstance(bot_entry, dict):
        return
    inline = [
        {"field": e["field_path"], "value": e["value"], "reason_id": e["id"]}
        for e in data.get("intents") or []
        if isinstance(e, dict) and "field_path" in e and "id" in e
    ]
    if inline:
        bot_entry["config_intents"] = inline
    else:
        bot_entry.pop("config_intents", None)
    try:
        save_network(network, path)
    except Exception:
        # Self-heals on next set_intent call. Discrepancies are silent in
        # v1 per spec §8.6 / open question §10.4.
        return


# ── Public API ───────────────────────────────────────────────────────────────


def set_intent(
    bot_id: str,
    field_path: str,
    value: Any,
    *,
    reason: str = "",
    set_by: str,
    set_by_detail: str | None = None,
    depends_on: dict | None = None,
    actor: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
    network: dict | None = None,
) -> str:
    """Record (or update) an intent for ``(bot_id, field_path)``.

    Last-write-wins for the same ``(bot_id, field_path)`` (spec §8.2): the
    existing record's ``id`` is preserved and an ``updated`` audit entry
    is appended capturing the prior value and reason.

    Phase 3 inferred-auto path (spec §3.1 step 2): callers that don't
    have an explicit reason pass ``set_by="inferred:auto"`` and
    optionally omit ``reason``. The function dispatches to
    ``analyzer.permissions.intent_inference.infer()`` which deduces a
    reason + confidence-grade ``set_by``. Low-confidence inferences
    write the intent with ``queued=True`` so the UI can prompt the
    operator. Any inference failure (model unreachable, malformed
    output, contradictions) collapses to ``inferred:low`` with a
    fallback reason rather than blocking the write.

    Returns the intent ``id``.
    """
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("bot_id must be a non-empty string")
    _validate_field_path(field_path)
    if not isinstance(set_by, str) or not set_by.strip():
        raise ValueError("set_by must be a non-empty string")
    if depends_on is not None and not isinstance(depends_on, dict):
        raise ValueError("depends_on must be a dict or None")

    queued = False

    if set_by == "inferred:auto":
        # Resolve the old value for the inference diff. ``existing`` may
        # be None on first write for this (bot, field) — that's a valid
        # input to inference; the model just gets old=None to reason about.
        existing = get_intent(bot_id, field_path, shared_dir=shared_dir)
        old_value = existing.get("value") if isinstance(existing, dict) else None

        result = _infer_intent(
            bot_id=bot_id, field_path=field_path,
            old_value=old_value, new_value=value,
            shared_dir=shared_dir, network=network,
        )
        # Inference layer is the source of truth for these fields when
        # the caller asked for auto. The caller's ``reason`` /
        # ``depends_on`` arguments — if supplied alongside
        # ``inferred:auto`` — are intentionally ignored; the contract
        # is "ask the inference layer to figure it out."
        reason = result.reason
        set_by = result.set_by
        depends_on = result.depends_on
        queued = result.queued

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")

    data = _load_sidecar(bot_id, shared_dir)
    intents = data.setdefault("intents", [])
    now = _utc_now_iso()
    idx = _find_intent_index(data, field_path)
    if idx < 0:
        intent_id = _new_intent_id()
        intent: dict[str, Any] = {
            "id": intent_id,
            "field_path": field_path,
            "value": value,
            "reason": reason,
            "depends_on": depends_on,
            "set_at": now,
            "set_by": set_by,
            "set_by_detail": set_by_detail,
            "audit_history": [
                {"event": "set", "at": now, "actor": actor or set_by,
                 "set_by": set_by, "reason": reason}
            ],
        }
        if queued:
            intent["queued"] = True
        intents.append(intent)
    else:
        intent = intents[idx]
        intent_id = intent["id"]
        prior_value = intent.get("value")
        prior_reason = intent.get("reason")
        intent.update({
            "value": value,
            "reason": reason,
            "depends_on": depends_on,
            "set_by": set_by,
            "set_by_detail": set_by_detail,
        })
        # ``queued`` is a transient state — a successful re-inference
        # that lands on high/medium confidence should clear it; a low-
        # confidence one keeps it set. Either way reflect the new state.
        if queued:
            intent["queued"] = True
        else:
            intent.pop("queued", None)
        history = intent.setdefault("audit_history", [])
        history.append({
            "event": "updated", "at": now,
            "actor": actor or set_by, "set_by": set_by,
            "from_value": prior_value, "to_value": value,
            "from_reason": prior_reason, "to_reason": reason,
        })

    _atomic_write_sidecar(bot_id, data, shared_dir)
    _sync_inline_mirror(bot_id, data, network_path)
    return intent_id


def _infer_intent(
    *,
    bot_id: str,
    field_path: str,
    old_value: Any,
    new_value: Any,
    shared_dir: Path | None,
    network: dict | None,
):
    """Dispatch to the analyzer-side inference module.

    Lazy import (evolve-analyzer is an installed dependency). The
    import itself never raises — we fall back to a plain "inference
    unavailable" InferenceResult on any failure so the calling
    ``set_intent`` continues to write the intent rather than
    blocking the operator's Save.
    """
    try:
        from permissions.intent_inference import infer, InferenceResult
    except Exception:  # noqa: BLE001 — fallback if analyzer unreachable
        return _InferenceResultStub()
    try:
        return infer(
            bot_id=bot_id, field_path=field_path,
            old_value=old_value, new_value=new_value,
            shared_dir=shared_dir, network=network,
        )
    except Exception:  # noqa: BLE001 — never block the write on inference
        return _InferenceResultStub()


class _InferenceResultStub:
    """Stable fallback when the analyzer-side inference module isn't
    reachable. Mirrors the public surface of
    ``permissions.intent_inference.InferenceResult`` so set_intent
    doesn't have to branch on the type."""
    reason = (
        "Inference unavailable for this write. Click 'Edit reason' on "
        "the Intentional Deviations row to record the operator's actual "
        "intent."
    )
    confidence = "low"
    set_by = "inferred:low"
    depends_on = None
    queued = True
    contradictions = ["inference module unreachable from config_intent"]


def record_migration_intent(
    bot_id: str,
    migration_id: str,
    fields: "list[tuple[str, Any, str]] | dict[str, tuple[Any, str]]",
    *,
    depends_on: dict | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> list[str]:
    """Record one or more intents from a migration script.

    Spec: docs/spec-config-intent-system-2026-05-21.md §3 (Phase 1
    deliverable). Convenience wrapper that uniformly stamps
    ``set_by=f"migration:{migration_id}"`` so every migration script
    looks the same in the audit history regardless of who wrote it.

    Args:
      bot_id: The bot whose sidecar gets the entries.
      migration_id: Short identifier the spec calls out for set_by —
        e.g. ``"oc_exec_deny_phase_a"`` or ``"openclaw_derived_2026_05_24"``.
        Used to construct ``set_by=f"migration:{migration_id}"``.
      fields: Either a list of ``(field_path, value, reason)`` tuples
        or a dict mapping ``field_path -> (value, reason)``. Both
        shapes accepted because the calling sites differ (some have
        an ordered list, some a dict keyed by field).
      depends_on: Optional shared ``depends_on`` block applied to
        every recorded intent. When intents have different
        ``depends_on`` blocks, call ``set_intent`` directly per field
        instead.
      shared_dir / network_path: forwarded to ``set_intent``.

    Returns the list of intent ids in the same order as ``fields``.

    Migration scripts that don't have a ``depends_on`` block (most)
    can pass it as None — the recorded intent will be ``set_by`` +
    ``reason`` only, no plugin coupling.

    No current migration script wires this yet — the helper exists as
    a stable seam future migrations (Evolve-side ones that mutate
    ``openclaw.json`` permission fields, or OC-side migrations whose
    operator-facing wrapper records the intent post-hoc) can call
    without re-litigating the call shape.
    """
    if not isinstance(migration_id, str) or not migration_id.strip():
        raise ValueError("migration_id must be a non-empty string")

    if isinstance(fields, dict):
        items = [
            (path, value, reason)
            for path, (value, reason) in fields.items()
        ]
    else:
        items = list(fields)

    set_by = f"migration:{migration_id}"
    ids: list[str] = []
    for field_path, value, reason in items:
        ids.append(set_intent(
            bot_id, field_path, value,
            reason=reason, set_by=set_by,
            depends_on=depends_on,
            actor=set_by,
            shared_dir=shared_dir,
            network_path=network_path,
        ))
    return ids


def get_intent(bot_id: str, field_path: str, *,
               shared_dir: Path | None = None) -> dict | None:
    """Return the active intent record for ``(bot_id, field_path)``, or None.

    A "valid for the observed value" check is the caller's responsibility
    (`intent_still_valid` plus its own value-equality test). Returning the
    raw record lets the generator inspect ``depends_on`` and ``audit_history``
    without a second read.
    """
    try:
        data = _load_sidecar(bot_id, shared_dir)
    except (RuntimeError, OSError):
        # Fail-open: treat malformed/missing sidecar as "no intent recorded"
        # so a broken file can't suppress a legitimate proposal. Caller's
        # generator emits the revert proposal as before. The corruption
        # surfaces on the next set_intent call which will raise.
        return None
    idx = _find_intent_index(data, field_path)
    if idx < 0:
        return None
    return data["intents"][idx]


def list_intents(bot_id: str, *,
                 shared_dir: Path | None = None) -> list[dict]:
    """Return all active intents for ``bot_id`` (empty list if none)."""
    try:
        data = _load_sidecar(bot_id, shared_dir)
    except (RuntimeError, OSError):
        return []
    return list(data.get("intents") or [])


def revoke_intent(bot_id: str, intent_id: str, *,
                  actor: str,
                  shared_dir: Path | None = None,
                  network_path: Path | None = None) -> bool:
    """Move an intent from ``intents`` to ``intents_archive`` (audit-preserving).

    Returns True if the intent existed and was archived, False otherwise.
    Does NOT mutate the underlying config field — revocation is a metadata
    operation (spec §7.3). The next sweep that finds the deviation will
    legitimately emit a revert proposal which the operator can accept.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    data = _load_sidecar(bot_id, shared_dir)
    intents = data.get("intents") or []
    for i, entry in enumerate(intents):
        if isinstance(entry, dict) and entry.get("id") == intent_id:
            now = _utc_now_iso()
            entry.setdefault("audit_history", []).append({
                "event": "revoked", "at": now, "actor": actor,
            })
            data.setdefault("intents_archive", []).append(entry)
            del intents[i]
            _atomic_write_sidecar(bot_id, data, shared_dir)
            _sync_inline_mirror(bot_id, data, network_path)
            return True
    return False


def edit_intent_reason(
    bot_id: str,
    intent_id: str,
    *,
    new_reason: str,
    actor: str,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> bool:
    """Update an intent's ``reason`` field in place.

    Spec: docs/spec-config-intent-system-2026-05-21.md §8.5 — the
    operator-facing edit path when the recorded reason no longer
    matches reality (e.g., inferred reason was wrong, or the
    underlying context shifted). Updates ``reason`` only; the recorded
    ``value`` and ``set_by`` stay intact so the audit chain still
    explains who originally set the field.

    Appends an ``audit_history`` entry of shape::

        {"event": "reason_edited", "at": ..., "actor": <actor>,
         "from_reason": <prior>, "to_reason": <new>}

    Returns ``True`` if the intent existed and the reason was updated;
    ``False`` if no matching intent was found (caller responds 404).

    Raises ``ValueError`` on empty ``actor`` or ``new_reason`` so the
    HTTP layer can map them to 400. The inline annotation is re-synced
    via the same path ``set_intent`` uses, keeping the sidecar +
    network.json mirror coherent per §8.6.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    if not isinstance(new_reason, str) or not new_reason.strip():
        raise ValueError("new_reason must be a non-empty string")

    data = _load_sidecar(bot_id, shared_dir)
    intents = data.get("intents") or []
    for entry in intents:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") != intent_id:
            continue
        prior_reason = entry.get("reason")
        entry["reason"] = new_reason
        history = entry.setdefault("audit_history", [])
        history.append({
            "event": "reason_edited",
            "at": _utc_now_iso(),
            "actor": actor,
            "from_reason": prior_reason,
            "to_reason": new_reason,
        })
        _atomic_write_sidecar(bot_id, data, shared_dir)
        _sync_inline_mirror(bot_id, data, network_path)
        return True
    return False


def confirm_queued_intent(
    bot_id: str,
    intent_id: str,
    *,
    actor: str,
    new_reason: str | None = None,
    shared_dir: Path | None = None,
    network_path: Path | None = None,
) -> bool:
    """Operator-acknowledges a low-confidence inferred intent.

    Spec: Phase 4.1 of docs/spec-config-intent-system-2026-05-21.md
    (deferred from §7.3 — surface C popover for queued=True intents).

    The Phase 3 inference layer marks low-confidence verdicts with
    ``queued=true`` so the UI can prompt the operator to confirm or
    replace the inferred reason. This helper handles the "confirm"
    path:

      - Clears ``queued`` on the active intent.
      - Optionally replaces ``reason`` if the operator wrote one
        (otherwise the inferred reason stays).
      - Appends an ``audit_history`` entry of shape
        ``{event: 'confirmed_queued', at, actor, from_reason?,
        to_reason?}`` so the audit trail shows the operator's
        acknowledgment.

    Returns ``True`` on success, ``False`` if no matching intent was
    found. Raises ``ValueError`` on empty ``actor`` so the HTTP layer
    can map to 400.

    Does NOT change the recorded ``value`` or ``set_by`` — the original
    inference tag stays so future audits can see this was an
    operator-confirmed inferred intent, not an operator-originated one.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")

    data = _load_sidecar(bot_id, shared_dir)
    intents = data.get("intents") or []
    for entry in intents:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") != intent_id:
            continue
        history_event: dict[str, Any] = {
            "event": "confirmed_queued",
            "at": _utc_now_iso(),
            "actor": actor,
        }
        if isinstance(new_reason, str) and new_reason.strip():
            history_event["from_reason"] = entry.get("reason")
            history_event["to_reason"] = new_reason
            entry["reason"] = new_reason
        # Whether the operator supplied a new reason or accepted the
        # inferred one, the queued flag clears — the explicit
        # acknowledgment IS what the queue was waiting for.
        entry.pop("queued", None)
        entry.setdefault("audit_history", []).append(history_event)
        _atomic_write_sidecar(bot_id, data, shared_dir)
        _sync_inline_mirror(bot_id, data, network_path)
        return True
    return False


def list_all_intents(*,
                     shared_dir: Path | None = None) -> dict[str, list[dict]]:
    """Return ``{bot_id: [intent, ...]}`` for every bot with a sidecar.

    Used by the GET /api/intents (no bot_id) route to render the pod-
    wide Surface C view. The map's value lists are the same shape
    ``list_intents`` returns for one bot; an empty intents list IS
    included for bots whose sidecar exists but has no active intents
    (so the UI can render an "all intents revoked" empty state
    distinguishably from "never had any").

    Fails open: unreadable sidecars are skipped silently — same shape
    as ``list_intents`` and ``get_intent`` to keep the operator-visible
    UI from blank-screening on a single bad file.
    """
    sd = _resolve_shared(shared_dir)
    intents_dir = sd / "config_intents"
    if not intents_dir.is_dir():
        return {}
    out: dict[str, list[dict]] = {}
    for path in sorted(intents_dir.glob("*.json")):
        bot_id = path.stem
        try:
            data = _load_sidecar(bot_id, shared_dir)
        except (RuntimeError, OSError):
            continue
        out[bot_id] = list(data.get("intents") or [])
    return out


def intent_still_valid(intent: dict, *,
                       shared_dir: Path | None = None) -> bool:
    """Whether ``intent``'s preconditions still hold (spec §4.2).

    Phase 1 ships the non-coupled branch: intents without a ``depends_on``
    block are always valid. Plugin-coupled validation (Phase 5) replaces
    the default-True return for ``depends_on.plugin`` intents with an
    actual plugin-presence lookup.
    """
    if not isinstance(intent, dict):
        return False
    deps = intent.get("depends_on") or {}
    if isinstance(deps, dict) and "plugin" in deps:
        # Phase 5 will implement _plugin_is_enabled_for_bot here.
        # Until then, treat plugin-coupled intents as valid — same
        # noise-suppression behavior as non-coupled; no spurious
        # stale_flagged emissions.
        return True
    return True
