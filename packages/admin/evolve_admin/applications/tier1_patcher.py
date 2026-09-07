"""Tier 1 manifest patcher — applies LLM-emitted patches respecting provenance.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §7.7.3.

When a Tier 1 quick scan returns ``verdict: "handled"`` with a list of
``manifest_patches``, this module applies them. The patcher honors the
spec's central provenance gate:

  observational field  → apply directly to the manifest in place
                         (no chip, no Signal, no Proposal — the silent
                         update path)
  authored field       → stage in reconciliation.<list>[] for chip
                         review (operator decides via Approve/Repair/
                         Defer in the UI)

This module is a pure-Python function on dicts — no I/O, no LLM calls.
The dispatcher reads/writes the manifest file; this module just
transforms the manifest dict.

## Patch ops

  add        — append the value to a list-typed field
                (files / crons / scheduled_actions / volatile_paths /
                 requirements.integrations)
  append     — append text to a string-typed field (description)
  update_sha — update a specific files[*] entry's sha256
  remove     — remove an entry from a list-typed field (rare; Tier 1
                proposes; observational removal applies, authored
                removal stages)

## Safety bounds

The patcher is opinionated about what Tier 1 can change. Even a
``forge_built`` field MAY NOT be deleted via Tier 1 — Tier 1's job is
small structural amendments, not rewrites. Anything more is the
escalate path's job (Tier 3 follow-up).

## Output

A ``PatchResult`` with three counts:

  applied  — patches that landed (observational fields)
  staged   — patches deferred to chip (authored fields)
  rejected — patches that failed validation
             (op/field combination not supported, target not found,
              etc.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Result type ────────────────────────────────────────────────────────────


@dataclass
class PatchResult:
    """Summary of a Tier 1 patch application.

    Each list carries the *patch dicts* (with reasons) so the caller
    can render trail entries or chip rows.
    """
    applied:  list[dict] = field(default_factory=list)
    staged:   list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    @property
    def staged_count(self) -> int:
        return len(self.staged)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def to_dict(self) -> dict:
        return {
            "applied_count":  self.applied_count,
            "staged_count":   self.staged_count,
            "rejected_count": self.rejected_count,
            "applied":  list(self.applied),
            "staged":   list(self.staged),
            "rejected": list(self.rejected),
        }


# ── Provenance gate helper (mirrors reconciliation.is_field_authored) ─────

_AUTHORED_SOURCES = frozenset({
    "forge_built", "user_authored", "bot_authored", "confirmed",
})


def _is_field_authored(manifest: dict, field_name: str) -> bool:
    prov = manifest.get("provenance") or {}
    field_origins = prov.get("field_origins") or {}
    entry = field_origins.get(field_name)
    if not isinstance(entry, dict):
        return False
    return (entry.get("source") or "").strip().lower() in _AUTHORED_SOURCES


# ── Op handlers ────────────────────────────────────────────────────────────


def _apply_add(manifest: dict, field_name: str, value: Any) -> bool:
    """Append ``value`` to a list-typed field. Returns True on success."""
    if field_name == "requirements":
        # Special: requirements is a dict; Tier 1 adds to its integrations[]
        # subkey. The patch's value shape is the integration dict.
        reqs = manifest.setdefault("requirements", {})
        ints = reqs.setdefault("integrations", [])
        if not isinstance(ints, list):
            return False
        ints.append(value)
        return True
    existing = manifest.get(field_name)
    if existing is None:
        manifest[field_name] = [value]
        return True
    if not isinstance(existing, list):
        return False
    existing.append(value)
    return True


def _apply_append(manifest: dict, field_name: str, value: Any) -> bool:
    """Append ``value`` (string) to a string-typed field."""
    if not isinstance(value, str):
        return False
    existing = manifest.get(field_name)
    if existing is None:
        manifest[field_name] = value
        return True
    if not isinstance(existing, str):
        return False
    # Be tolerant of trailing whitespace.
    if existing and not existing.endswith(" "):
        manifest[field_name] = existing + " " + value.lstrip()
    else:
        manifest[field_name] = existing + value
    return True


def _apply_update_sha(manifest: dict, field_name: str, value: Any) -> bool:
    """Update a files[*] entry's sha256. ``value`` must be a dict with
    ``path`` and ``sha256``."""
    if field_name != "files":
        return False
    if not isinstance(value, dict):
        return False
    path = (value.get("path") or "").lstrip("/")
    new_sha = (value.get("sha256") or "").strip()
    if not path or not new_sha:
        return False
    files = manifest.get("files") or []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        if (entry.get("path") or "").lstrip("/") == path:
            entry["sha256"] = new_sha
            return True
    return False


def _apply_remove(manifest: dict, field_name: str, value: Any) -> bool:
    """Remove an entry from a list-typed field. ``value`` shape varies:
        - for files[]: dict with ``path``
        - for crons[]: dict with ``label`` or the label string
        - for scheduled_actions[]: dict with ``id``
    """
    if field_name not in ("files", "crons", "scheduled_actions",
                          "volatile_paths"):
        return False
    existing = manifest.get(field_name) or []
    if not isinstance(existing, list):
        return False

    def matches(entry: Any) -> bool:
        if field_name == "files":
            if not (isinstance(entry, dict) and isinstance(value, dict)):
                return False
            return (entry.get("path") or "") == (value.get("path") or "")
        if field_name == "crons":
            if isinstance(value, dict):
                target_label = value.get("label") or value.get("name") or ""
            elif isinstance(value, str):
                target_label = value
            else:
                return False
            entry_label = ""
            if isinstance(entry, dict):
                entry_label = entry.get("label") or entry.get("name") or ""
            elif isinstance(entry, str):
                entry_label = entry
            return target_label and target_label == entry_label
        if field_name == "scheduled_actions":
            if not (isinstance(entry, dict) and isinstance(value, dict)):
                return False
            return (entry.get("id") or "") == (value.get("id") or "")
        if field_name == "volatile_paths":
            if not (isinstance(entry, dict) and isinstance(value, dict)):
                return False
            return (entry.get("glob") or "") == (value.get("glob") or "")
        return False

    before = len(existing)
    manifest[field_name] = [e for e in existing if not matches(e)]
    return len(manifest[field_name]) != before


# ── Staging (authored fields) ──────────────────────────────────────────────


def _stage_in_reconciliation(
    manifest: dict, patch: dict, *, now_iso: str,
) -> None:
    """Stage an authored-field patch in the reconciliation block.

    Different patch ops map to different reconciliation lists:
      add files       → extra_files[]
      add crons       → (logged in patch trail; no specific list — the
                         operator will see crons that exist on disk but
                         not in manifest via the cron-status check)
      append desc     → operator_decisions[] (the operator needs to
                         decide whether to accept the prose change)
      others          → operator_decisions[] generic

    The staged entry is the patch dict itself plus a ``staged_at``
    timestamp.
    """
    rec = manifest.setdefault("reconciliation", {})
    field_name = patch.get("field")
    op = patch.get("op")

    if field_name == "files" and op == "add":
        extras = rec.setdefault("extra_files", [])
        value = patch.get("value") or {}
        extras.append({
            "path": (value.get("path") or "").lstrip("/"),
            "inferred_layer": value.get("layer", ""),
            "confidence": "medium",   # Tier 1 attribution
            "attribution_evidence": "tier1_patch",
            "detected_at": now_iso,
            "authored_field_affected": "files",
            "tier1_patch": patch,
        })
        return

    decisions = rec.setdefault("operator_decisions", [])
    decisions.append({
        "kind": "tier1_pending",
        "patch": patch,
        "staged_at": now_iso,
        "rationale": (
            f"Tier 1 proposed {op} on authored field {field_name!r}; "
            f"awaiting operator approve / repair / defer"
        ),
    })


# ── Main entry point ───────────────────────────────────────────────────────


_OP_HANDLERS = {
    "add":        _apply_add,
    "append":     _apply_append,
    "update_sha": _apply_update_sha,
    "remove":     _apply_remove,
}


def apply_tier1_patches(
    manifest: dict,
    patches: list[dict],
    *,
    now_iso: str | None = None,
) -> PatchResult:
    """Apply a list of Tier 1 patches to ``manifest`` in place.

    Each patch is routed through the provenance gate. Returns a
    PatchResult so the caller can log trail entries and report
    per-patch outcomes.

    Args:
        manifest: the manifest dict (will be mutated).
        patches: list of Tier 1 manifest_patches from the LLM output.
        now_iso: timestamp override for testing.

    Returns:
        PatchResult with applied / staged / rejected lists.
    """
    result = PatchResult()
    now = now_iso or datetime.now(timezone.utc).isoformat()

    for patch in patches or []:
        if not isinstance(patch, dict):
            result.rejected.append({
                "patch": patch,
                "reason": "not a dict",
            })
            continue

        field_name = patch.get("field")
        op = patch.get("op")

        # Validate op + field upfront.
        if op not in _OP_HANDLERS:
            result.rejected.append({
                "patch": patch,
                "reason": f"unknown op {op!r}",
            })
            continue

        # Provenance gate.
        if _is_field_authored(manifest, field_name):
            _stage_in_reconciliation(manifest, patch, now_iso=now)
            result.staged.append({"patch": patch, "staged_at": now})
            continue

        # Observational field — apply directly.
        handler = _OP_HANDLERS[op]
        value = patch.get("value")
        ok = handler(manifest, field_name, value)
        if ok:
            result.applied.append({"patch": patch, "applied_at": now})
        else:
            result.rejected.append({
                "patch": patch,
                "reason": (
                    f"op={op!r} on field={field_name!r} with "
                    f"value={value!r} did not apply"
                ),
            })

    return result
