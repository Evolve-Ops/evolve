"""App changelog — structural-change trail entries.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §11.5.

The changelog is the silent surface for observational manifests
(operator opens it to see what's evolved) and the historical record
for authored manifests (alongside the chip-driven decision flow).

This module extends the existing per-app audit trail
``/Users/<bot>/.openclaw/workspace/evolve/audits/<app_id>/trail.jsonl``
(introduced by the audit framework) with new entry kinds for
manifest-evolution events.

## What gets logged

Per spec §11.5.1, the filter is:

  log when something that COULD become a contract appears,
  disappears, or changes; do NOT log data flow.

Concretely:

  YES — new/removed file in code, config, contract, behavior_doc
  YES — sha drift on code/config/contract
  YES — new/removed cron entry
  YES — new/removed scheduled_actions[*]
  YES — new/removed requirements.integrations[*]
  YES — new/removed volatile_paths[]
  YES — provenance change (any field)
  YES — operator decision (Approve / Repair / Defer / Promote)
  YES — repair session result (applied / proposed / failed)
  YES — manifest field edit via UI editor

  NO  — new file in data, log, state, content layers
  NO  — file count growth/shrinkage in a volatile_paths glob
        (unless Pass B anomaly threshold crossed)
  NO  — cron firing successfully (that's audit run telemetry)

## Entry shape

All entries share a common header:

  {
    "ts":   "<iso>",
    "kind": "<entry_kind>",
    ...kind-specific fields
  }

Kind-specific fields:

  manifest_change         — {field, change, before, after}
  structural_addition     — {kind: file|cron|action|integration|
                                   volatile_path, id, layer?}
  structural_removal      — same shape as addition
  provenance_change       — {field, from, to, by}
  decision_recorded       — {finding_id, decision, by, rationale?}
  repair_applied          — {request_id, transformations[], proposals[]}
  repair_failed           — {request_id, error}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evolve_util import now_iso_micro as _now_iso


# ── Trail entry kinds ─────────────────────────────────────────────────────

KIND_MANIFEST_CHANGE     = "manifest_change"
KIND_STRUCTURAL_ADDITION = "structural_addition"
KIND_STRUCTURAL_REMOVAL  = "structural_removal"
KIND_PROVENANCE_CHANGE   = "provenance_change"
KIND_DECISION_RECORDED   = "decision_recorded"
KIND_REPAIR_APPLIED      = "repair_applied"
KIND_REPAIR_FAILED       = "repair_failed"

VALID_KINDS = frozenset({
    KIND_MANIFEST_CHANGE,
    KIND_STRUCTURAL_ADDITION,
    KIND_STRUCTURAL_REMOVAL,
    KIND_PROVENANCE_CHANGE,
    KIND_DECISION_RECORDED,
    KIND_REPAIR_APPLIED,
    KIND_REPAIR_FAILED,
})


# Layers whose file additions / removals are LOGGED (the others —
# data/log/state — are noise per spec §11.5.1).
_LOGGED_FILE_LAYERS = frozenset({
    "code", "config", "contract", "behavior_doc",
})


# ── Entry builders ────────────────────────────────────────────────────────


def build_manifest_change_entry(
    field: str, *, before: Any = None, after: Any = None,
    change: str = "modified", at: str | None = None,
) -> dict:
    """Generic catch-all for a single field change."""
    return {
        "ts":     at or _now_iso(),
        "kind":   KIND_MANIFEST_CHANGE,
        "field":  field,
        "change": change,
        "before": before,
        "after":  after,
    }


def build_structural_addition_entry(
    *, kind: str, id: str, layer: str | None = None,
    at: str | None = None, extra: dict | None = None,
) -> dict | None:
    """A structural element was added.

    Returns ``None`` when the addition is in a layer the spec filters
    out (data / log / state / content). Callers should check for None
    before writing so they don't fill the trail with noise.

    Args:
        kind: one of "file" | "cron" | "action" | "integration" |
            "volatile_path"
        id: the element identifier (path / label / id / glob).
        layer: for kind="file", the v20 layer. Filters per spec §11.5.1.
        at: timestamp override.
        extra: additional fields merged into the entry.
    """
    if kind == "file" and layer not in _LOGGED_FILE_LAYERS:
        return None
    entry: dict[str, Any] = {
        "ts":   at or _now_iso(),
        "kind": KIND_STRUCTURAL_ADDITION,
        "element_kind": kind,
        "id":   id,
    }
    if layer is not None:
        entry["layer"] = layer
    if extra:
        entry.update(extra)
    return entry


def build_structural_removal_entry(
    *, kind: str, id: str, layer: str | None = None,
    at: str | None = None, extra: dict | None = None,
) -> dict | None:
    """Symmetric to structural_addition — same filter."""
    if kind == "file" and layer not in _LOGGED_FILE_LAYERS:
        return None
    entry: dict[str, Any] = {
        "ts":   at or _now_iso(),
        "kind": KIND_STRUCTURAL_REMOVAL,
        "element_kind": kind,
        "id":   id,
    }
    if layer is not None:
        entry["layer"] = layer
    if extra:
        entry.update(extra)
    return entry


def build_provenance_change_entry(
    *, field: str, from_source: str | None, to_source: str,
    by: str | None = None, via: str | None = None,
    at: str | None = None,
) -> dict:
    """One field's provenance changed (observational → user_authored,
    etc.). The provenance dance is operator-visible context for why a
    chip started firing."""
    return {
        "ts":   at or _now_iso(),
        "kind": KIND_PROVENANCE_CHANGE,
        "field": field,
        "from":  from_source,
        "to":    to_source,
        "by":    by,
        "via":   via,
    }


def build_decision_recorded_entry(
    *, finding_id: str, decision: str,
    by: str | None = None, rationale: str | None = None,
    at: str | None = None,
) -> dict:
    """Operator clicked Approve / Repair / Defer / Promote on a chip."""
    return {
        "ts":         at or _now_iso(),
        "kind":       KIND_DECISION_RECORDED,
        "finding_id": finding_id,
        "decision":   decision,
        "by":         by,
        "rationale":  rationale,
    }


def build_repair_applied_entry(
    *, request_id: str, transformations: list[dict] | None = None,
    proposals: list[dict] | None = None, at: str | None = None,
) -> dict:
    """A repair session ran and applied 0+ transformations and emitted
    0+ proposals."""
    return {
        "ts":             at or _now_iso(),
        "kind":           KIND_REPAIR_APPLIED,
        "request_id":     request_id,
        "transformations": list(transformations or []),
        "proposals":      list(proposals or []),
    }


def build_repair_failed_entry(
    *, request_id: str, error: str, at: str | None = None,
) -> dict:
    return {
        "ts":         at or _now_iso(),
        "kind":       KIND_REPAIR_FAILED,
        "request_id": request_id,
        "error":      error,
    }


# ── Trail append ──────────────────────────────────────────────────────────


def append_to_trail(
    audits_dir: Path,
    app_id: str,
    entry: dict | None,
) -> bool:
    """Append a single trail entry to
    ``{audits_dir}/{app_id}/trail.jsonl``.

    Idempotent: per-line atomic via O_APPEND. The caller is expected to
    pass entries built via the ``build_*_entry`` functions; ``None``
    is tolerated (when the entry was filtered out by a noise rule)
    and returns False without writing.

    Returns True on success, False on noise-skip or write error.
    """
    if entry is None:
        return False
    if not isinstance(entry, dict):
        return False
    kind = entry.get("kind")
    if kind not in VALID_KINDS:
        return False
    target_dir = audits_dir / app_id
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        return False
    target = target_dir / "trail.jsonl"
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except (PermissionError, OSError):
        return False


# ── Trail read / filter ───────────────────────────────────────────────────


def read_trail(
    audits_dir: Path,
    app_id: str,
    *,
    limit: int | None = None,
    kinds: set[str] | None = None,
) -> list[dict]:
    """Read trail entries from ``{audits_dir}/{app_id}/trail.jsonl``.

    Args:
        limit: max entries to return (most-recent first). None = all.
        kinds: optional kind filter — set of one or more VALID_KINDS.

    Returns:
        List of entry dicts, newest first. Empty when the file doesn't
        exist or is unreadable.
    """
    target = audits_dir / app_id / "trail.jsonl"
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (PermissionError, OSError):
        return []
    entries: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if kinds and entry.get("kind") not in kinds:
            continue
        entries.append(entry)
        if limit and len(entries) >= limit:
            break
    return entries


def filter_structural_only(entries: list[dict]) -> list[dict]:
    """Return only the entries that count as 'changelog' (structural
    + decision + repair); excludes audit_run telemetry from older trail
    kinds the existing audit framework writes."""
    keep = VALID_KINDS
    return [e for e in entries if e.get("kind") in keep]


# ── Diff summary (Compare to N days ago) ─────────────────────────────────


def summarize_diff(
    audits_dir: Path,
    app_id: str,
    *,
    since_iso: str,
) -> dict:
    """Summarize structural changes in the trail since ``since_iso``.

    Returns a dict with counts per kind, useful for the "Compare to N
    days ago" view (spec §11.5.3).
    """
    summary: dict[str, Any] = {
        "since": since_iso,
        "by_kind": {},
        "total":   0,
    }
    entries = read_trail(audits_dir, app_id)
    for e in entries:
        if e.get("ts", "") < since_iso:
            continue
        if e.get("kind") not in VALID_KINDS:
            continue
        k = e.get("kind", "")
        summary["by_kind"][k] = summary["by_kind"].get(k, 0) + 1
        summary["total"] += 1
    return summary
