"""Reconciliation pass — provenance-aware manifest-vs-disk delta walker.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §7.4
(PR 4 "Scanner reconciliation pass").

This is the first module where the spec's central doctrine — *silent
for observational fields, chip for authored fields* — becomes
operationally visible. PR 1 added the schema, PR 2 wired the write
paths to stamp provenance, PR 3 classified layers. Now we walk the
delta and apply the provenance gate.

## Gate semantics (spec §7.2 + §4.4)

For every disagreement between manifest claims and disk reality, the
gate consults ``provenance.field_origins.<field>.source``:

  observational  → silently update manifest to match reality. No
                   chip, no Signal, no Proposal. Trail entry only.

  authored (any) → stage entry in reconciliation.<list>[]. Chip
                   surfaces on Apps page (PR 7+); operator decides
                   Approve / Repair / Defer (PR 8+).

A field with no field_origins entry defaults to observational — the
safe choice that prevents day-1 alert flood.

## What this module checks

PR 4 ships the four highest-value checks:

  **missing_files**     — manifest.files[*].path not on disk
  **missing_crons**     — manifest.crons[*] not in live cron source
  **missing_actions**   — scheduled_action evidence_locator not present
  **extra_files**       — workspace files not in any manifest's files[]

Other checks (volatile_growth_anomalies, sha drift, etc.) are layered
in by Tier 2's existing app_audit_structural and PR 5 — this module
focuses on the manifest-write side that runs during scan.

## Scanner integration: Phase 6

Wired in scanner.py after Phase 5.5 (layer classification). Walks every
manifest in caps_dir and applies the reconciliation pass, writing
``manifest.reconciliation`` and updating files[]/crons[] for
observational fields. Best-effort; per-manifest failures don't block
the rest.

## Idempotency

The pass is idempotent for unchanged state. Re-running on a manifest
where reality matches claims produces no changes. When reality drifts,
the pass either applies the change (observational) or stages it
(authored) — both are stable across repeat invocations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..secret_config_perms import exists_or_unreachable
from .path_keys import ws_rel_key


# ── Provenance gate helper ──────────────────────────────────────────────────

# The four "authored" sources. ``observational`` is the safe default.
# Constants are also imported from manifest.py but redeclared here for
# zero-cycle imports.
_AUTHORED_SOURCES = frozenset({
    "forge_built", "user_authored", "bot_authored", "confirmed",
})


def is_field_authored(manifest: dict, field_name: str) -> bool:
    """Look up ``provenance.field_origins.<field>.source`` and return
    True iff it's an authored source.

    Defaults to False (observational) when the field has no entry,
    matching spec §4.2: 'A field absent from the map defaults to
    observational — safe default that errs on the side of "no chip"
    until the operator actively promotes.'
    """
    prov = manifest.get("provenance") or {}
    field_origins = prov.get("field_origins") or {}
    entry = field_origins.get(field_name)
    if not isinstance(entry, dict):
        return False
    source = (entry.get("source") or "").strip().lower()
    return source in _AUTHORED_SOURCES


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class ReconciliationSummary:
    """Per-manifest summary of what changed. Returned by ``apply_reconciliation``
    so the scanner Phase 6 can log a single line per manifest.

    The summary distinguishes silent updates (observational fields
    rewritten to match reality) from staged entries (authored fields
    flagged for operator review). Both are useful signals — the silent
    counts surface in the changelog; the staged counts are what the
    operator sees on the Apps page chips.
    """
    bot_id: str = ""
    app_id: str = ""
    missing_files_silent_dropped: int = 0
    missing_files_staged: int = 0
    missing_crons_silent_dropped: int = 0
    missing_crons_staged: int = 0
    missing_actions_silent_dropped: int = 0
    missing_actions_staged: int = 0
    extra_files_silent_added: int = 0
    extra_files_staged: int = 0
    # Bite 3 (spec §9.3): normalized drift events for the drift-significance
    # classifier to narrate. One entry per real delta (silent OR staged),
    # shape: {kind: add|remove|modify, target_type: file|cron|action,
    # target: <path|label|id>, detail?: <str>}. Purely additive — the
    # reconcile gate behavior is unchanged; this is the feed drift_classifier
    # consumes in scanner Phase 6.
    drift_events: list = field(default_factory=list)

    @property
    def silent_total(self) -> int:
        return (
            self.missing_files_silent_dropped
            + self.missing_crons_silent_dropped
            + self.missing_actions_silent_dropped
            + self.extra_files_silent_added
        )

    @property
    def staged_total(self) -> int:
        return (
            self.missing_files_staged
            + self.missing_crons_staged
            + self.missing_actions_staged
            + self.extra_files_staged
        )

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "app_id": self.app_id,
            "missing_files_silent_dropped": self.missing_files_silent_dropped,
            "missing_files_staged": self.missing_files_staged,
            "missing_crons_silent_dropped": self.missing_crons_silent_dropped,
            "missing_crons_staged": self.missing_crons_staged,
            "missing_actions_silent_dropped": self.missing_actions_silent_dropped,
            "missing_actions_staged": self.missing_actions_staged,
            "extra_files_silent_added": self.extra_files_silent_added,
            "extra_files_staged": self.extra_files_staged,
            "silent_total": self.silent_total,
            "staged_total": self.staged_total,
            "drift_events": list(self.drift_events),
        }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _file_exists(workspace_path: Path, rel_path: str) -> bool:
    """True if ``workspace_path / rel_path`` exists. Tolerates leading
    slashes and ``./`` prefixes in rel_path.

    exists_or_unreachable: a bare .exists() RAISES under a 0700 ACL-mask clamp on
    .openclaw (Py3.12). Treat an unreachable path as PRESENT so a clamp can't make
    reconciliation falsely flag a still-existing file as 'missing' (which would
    silent-drop or stage a spurious removal)."""
    if not rel_path:
        return False
    clean = rel_path.lstrip("/").lstrip("./")
    return exists_or_unreachable(workspace_path / clean)


def _cron_label(cron: Any) -> str:
    if isinstance(cron, dict):
        return cron.get("label") or cron.get("name") or ""
    if isinstance(cron, str):
        return cron
    return ""


def _cron_in_live_list(cron: Any, live_cron_labels: set[str]) -> bool:
    label = _cron_label(cron)
    if not label:
        return False
    # The scanner may give us labels with or without timing info; match
    # by exact name AND by substring (cron-alert-style labels are
    # canonical names).
    if label in live_cron_labels:
        return True
    return any(label in live for live in live_cron_labels)


def _section_anchor_present(text: str, locator: str) -> bool:
    """Check whether a scheduled_action evidence_locator anchor is
    still present in the cited file's text.

    The locator format varies — sometimes a section heading, sometimes
    a unique phrase, sometimes a line range. Simplest robust approach:
    substring match.
    """
    if not text or not locator:
        return False
    # Strip leading hashes/whitespace that section headings may include.
    needle = locator.strip().lstrip("# ").strip()
    if not needle:
        return False
    return needle.lower() in text.lower()


# ── Missing-files check (the most common path) ──────────────────────────────


def check_missing_files(
    manifest: dict,
    workspace_path: Path,
) -> tuple[list[dict], list[dict]]:
    """Walk files[*] and return (silent_drops, staged_entries).

    Silent_drops: file entries to remove from manifest.files[] because
    the field provenance is observational. The caller (apply_reconciliation)
    actually does the removal.

    Staged_entries: full entries to stage in
    reconciliation.missing_files[] because files[] is on contract.
    """
    files_authored = is_field_authored(manifest, "files")
    silent_drops: list[dict] = []
    staged: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        # F-C1: canonicalize to the workspace-relative key so an absolute path
        # stored by extend_application reduces to its in-workspace form before
        # the existence check — a bare ``.lstrip("/")`` mangles an absolute into
        # a non-existent nested path, firing a false `missing_files`.
        path = ws_rel_key(entry.get("path") or "", workspace_path)
        if not path:
            continue
        if _file_exists(workspace_path, path):
            continue
        if files_authored:
            staged.append({
                "path": path,
                "layer": entry.get("layer", ""),
                "expected_sha": entry.get("sha256", ""),
                "since": now,
                "last_seen": entry.get("modified_at") or entry.get("created_at") or "",
            })
        else:
            silent_drops.append(entry)
    return silent_drops, staged


# ── Missing-crons check ─────────────────────────────────────────────────────


def check_missing_crons(
    manifest: dict,
    *,
    live_cron_labels: set[str] | None,
) -> tuple[list[Any], list[dict]]:
    """Walk crons[*] and return (silent_drops, staged_entries).

    When ``live_cron_labels`` is None (cron source unavailable — transient
    CLI failure), the check is skipped entirely and both lists are
    empty. Avoids firing false positives when we can't see live state.
    """
    if live_cron_labels is None:
        return [], []
    crons_authored = is_field_authored(manifest, "crons")
    silent_drops: list[Any] = []
    staged: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for cron in manifest.get("crons") or []:
        if _cron_in_live_list(cron, live_cron_labels):
            continue
        if crons_authored:
            label = _cron_label(cron)
            if isinstance(cron, dict):
                script = cron.get("script") or ""
                schedule = cron.get("schedule") or ""
            else:
                script, schedule = "", ""
            staged.append({
                "cron_id": label,
                "expected_schedule": schedule,
                "expected_script": script,
                "since": now,
            })
        else:
            silent_drops.append(cron)
    return silent_drops, staged


# ── Missing-actions check ───────────────────────────────────────────────────


def check_missing_actions(
    manifest: dict,
    workspace_path: Path,
) -> tuple[list[dict], list[dict]]:
    """Walk scheduled_actions[*] and check each evidence_locator anchor.
    Returns (silent_drops, staged_entries).

    Skips entries with state != 'active' (intentional pause) and
    quality == 'suspect' (legacy noise). Matches Pass A's filter for
    consistency.
    """
    actions_authored = is_field_authored(manifest, "scheduled_actions")
    silent_drops: list[dict] = []
    staged: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for entry in manifest.get("scheduled_actions") or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("state") or "active") != "active":
            continue
        if (entry.get("quality") or "") == "suspect":
            continue
        trigger = entry.get("trigger") or {}
        if not isinstance(trigger, dict):
            continue
        ev_path = (trigger.get("evidence_path") or "").lstrip("/")
        locator = trigger.get("evidence_locator") or ""
        if not ev_path or not locator:
            continue
        full = workspace_path / ev_path
        # exists_or_unreachable: a bare .exists() RAISES under a 0700 ACL-mask
        # clamp (Py3.12). Treat unreachable as present → fall through to the
        # guarded read below (conservative skip), never falsely "missing".
        if not exists_or_unreachable(full):
            # Evidence file gone entirely — flag as missing.
            if actions_authored:
                staged.append({
                    "action_id": entry.get("id"),
                    "evidence_path": ev_path,
                    "evidence_locator": locator,
                    "reason": "evidence_file_missing",
                    "since": now,
                })
            else:
                silent_drops.append(entry)
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            # Can't verify; conservative skip.
            continue
        if _section_anchor_present(text, locator):
            continue
        if actions_authored:
            staged.append({
                "action_id": entry.get("id"),
                "evidence_path": ev_path,
                "evidence_locator": locator,
                "reason": "anchor_not_in_file",
                "since": now,
            })
        else:
            silent_drops.append(entry)
    return silent_drops, staged


# ── Extra-files check (workspace walk, attribution) ─────────────────────────


def check_extra_files(
    manifest: dict,
    workspace_path: Path,
    *,
    workspace_files: set[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Walk un-claimed workspace files and attribute to this app.

    Returns (silent_adds, staged_entries).

    ``workspace_files`` is the set of paths in the workspace not claimed
    by ANY manifest. The scanner pre-computes this (one walk per scan)
    and passes it in. None means "scanner didn't compute it" — skip.

    Attribution heuristics:
      - File path is in a directory containing this app's claimed files
        → likely belongs here.
      - File matches a volatile_paths[*].glob → belongs here, by the
        operator's declared rule.

    Confidence:
      - High (volatile_paths match or owned-directory match): auto-add
        to files[] when files[] is observational; stage when authored.
      - Low (no clear attribution): never auto-add; stage only when
        authored; ignore when observational (those will get classified
        on next discovery scan).

    For PR 4, we ship the volatile_paths match path only. Directory-
    based attribution is a refinement that PR 6+ adds with the change
    detector's import-graph scan.
    """
    if workspace_files is None:
        return [], []
    files_authored = is_field_authored(manifest, "files")
    volatile_paths = manifest.get("volatile_paths") or []
    claimed_paths = {
        (e.get("path") or "").lstrip("/")
        for e in manifest.get("files") or []
        if isinstance(e, dict)
    }
    silent_adds: list[dict] = []
    staged: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    import fnmatch
    for path in sorted(workspace_files):
        if path in claimed_paths:
            continue
        # Check volatile_paths first — operator-declared globs are
        # authoritative.
        matched_glob = None
        matched_layer = None
        for vp in volatile_paths:
            if not isinstance(vp, dict):
                continue
            glob = vp.get("glob") or ""
            if glob and fnmatch.fnmatch(path, glob):
                matched_glob = glob
                matched_layer = vp.get("layer", "")
                break
        if matched_glob is None:
            # No high-confidence attribution. Skip — fresh scan will
            # discover via Phase 5 stamping if it belongs here.
            continue
        if files_authored:
            staged.append({
                "path": path,
                "inferred_layer": matched_layer,
                "confidence": "high",
                "attribution_evidence": f"volatile_path_match:{matched_glob}",
                "detected_at": now,
                "authored_field_affected": "files",
            })
        else:
            silent_adds.append({
                "path": path,
                "layer": matched_layer or "data",
                "classification_method": "reconciliation_extra_files_volatile_match",
                "created_at": now,
                "modified_at": now,
            })
    return silent_adds, staged


# ── Drift-event collection (Bite 3 feed) ─────────────────────────────────────


def _collect_drift_events(
    *,
    missing_files_silent: list,
    missing_files_staged: list,
    crons_silent: list,
    crons_staged: list,
    actions_silent: list,
    actions_staged: list,
    extra_silent: list,
    extra_staged: list,
) -> list[dict]:
    """Normalize the reconcile deltas (silent + staged, all four checks) into
    drift events the drift_classifier consumes.

    Captures BOTH the silently-absorbed (observational) and the staged
    (authored) deltas — the drift narrative is independent of the
    provenance gate. For a ``defined`` app most fields are observational, so
    the silent path is the primary source; the staged path adds the rare
    authored-field drift.

    Shape: {kind: add|remove|modify, target_type: file|cron|action,
            target: <path|cron-label|action-id>, detail?: <reason>}.

    Action kind: a staged entry carries its ``reason`` — ``anchor_not_in_file``
    is a content *modify* of the action's evidence surface; ``evidence_file_missing``
    is a *remove*. A silently-dropped action lost its reason in the
    observational path, so it is narrated as a *remove* (it left the manifest).
    """
    events: list[dict] = []

    # ── missing files → remove ──────────────────────────────────────────────
    for entry in missing_files_silent:
        path = (entry.get("path") or "").lstrip("/") if isinstance(entry, dict) else ""
        if path:
            events.append({"kind": "remove", "target_type": "file", "target": path})
    for entry in missing_files_staged:
        path = (entry.get("path") or "").lstrip("/") if isinstance(entry, dict) else ""
        if path:
            events.append({"kind": "remove", "target_type": "file", "target": path})

    # ── missing crons → remove ──────────────────────────────────────────────
    # Targets are coerced to str at this data boundary: a hand-edited manifest
    # may carry a non-string cron label / action id, and the downstream
    # narrator treats every target as a path string.
    for cron in crons_silent:
        label = _cron_label(cron)
        if label:
            events.append({"kind": "remove", "target_type": "cron", "target": str(label)})
    for entry in crons_staged:
        label = entry.get("cron_id") if isinstance(entry, dict) else ""
        if label:
            events.append({"kind": "remove", "target_type": "cron", "target": str(label)})

    # ── missing actions → remove (file gone) | modify (anchor moved) ────────
    for entry in actions_silent:
        action_id = entry.get("id") if isinstance(entry, dict) else ""
        if action_id:
            events.append({"kind": "remove", "target_type": "action", "target": str(action_id)})
    for entry in actions_staged:
        if not isinstance(entry, dict):
            continue
        action_id = entry.get("action_id")
        if not action_id:
            continue
        reason = entry.get("reason") or ""
        kind = "modify" if reason == "anchor_not_in_file" else "remove"
        events.append({
            "kind": kind, "target_type": "action",
            "target": str(action_id), "detail": reason,
        })

    # ── extra files → add ───────────────────────────────────────────────────
    for entry in (extra_silent + extra_staged):
        path = (entry.get("path") or "").lstrip("/") if isinstance(entry, dict) else ""
        if path:
            events.append({"kind": "add", "target_type": "file", "target": path})

    return events


# ── Main entry point ────────────────────────────────────────────────────────


def apply_reconciliation(
    manifest: dict,
    workspace_path: Path,
    *,
    live_cron_labels: set[str] | None = None,
    workspace_files: set[str] | None = None,
    bot_id: str = "",
) -> ReconciliationSummary:
    """Run the full reconciliation pass on a manifest dict.

    Mutates ``manifest`` in place. Observational drops are removed
    directly from files[] / crons[] / scheduled_actions[]. Authored
    drops are staged in reconciliation.<list>[].

    The ``reconciliation`` block is rewritten with current state.
    Operator-decision entries are preserved (operator_decisions[]
    survives the rewrite).

    Returns a ReconciliationSummary the caller logs.
    """
    summary = ReconciliationSummary(
        bot_id=bot_id, app_id=manifest.get("id", ""),
    )
    now = datetime.now(timezone.utc).isoformat()
    rec = manifest.setdefault("reconciliation", {})

    # Preserve operator decisions across the rewrite.
    operator_decisions = rec.get("operator_decisions") or []

    # ── Missing files ───────────────────────────────────────────────────────
    missing_silent, missing_staged = check_missing_files(manifest, workspace_path)
    if missing_silent:
        # Silently drop from files[].
        keep = [
            e for e in manifest.get("files") or []
            if e not in missing_silent
        ]
        manifest["files"] = keep
        summary.missing_files_silent_dropped = len(missing_silent)
    summary.missing_files_staged = len(missing_staged)

    # ── Missing crons ───────────────────────────────────────────────────────
    crons_silent, crons_staged = check_missing_crons(
        manifest, live_cron_labels=live_cron_labels,
    )
    if crons_silent:
        keep = [
            c for c in manifest.get("crons") or []
            if c not in crons_silent
        ]
        manifest["crons"] = keep
        summary.missing_crons_silent_dropped = len(crons_silent)
    summary.missing_crons_staged = len(crons_staged)

    # ── Missing scheduled_actions evidence anchors ─────────────────────────
    actions_silent, actions_staged = check_missing_actions(manifest, workspace_path)
    if actions_silent:
        keep = [
            a for a in manifest.get("scheduled_actions") or []
            if a not in actions_silent
        ]
        manifest["scheduled_actions"] = keep
        summary.missing_actions_silent_dropped = len(actions_silent)
    summary.missing_actions_staged = len(actions_staged)

    # ── Extra files (un-claimed workspace files this app should own) ───────
    extra_silent, extra_staged = check_extra_files(
        manifest, workspace_path, workspace_files=workspace_files,
    )
    if extra_silent:
        manifest.setdefault("files", [])
        manifest["files"].extend(extra_silent)
        summary.extra_files_silent_added = len(extra_silent)
    summary.extra_files_staged = len(extra_staged)

    # ── Write reconciliation block ─────────────────────────────────────────
    rec["last_reconciled_at"] = now
    rec["missing_files"] = missing_staged
    rec["missing_crons"] = crons_staged
    rec["missing_actions"] = actions_staged
    rec["extra_files"] = extra_staged
    # Volatile growth anomalies are a Tier 2 / Pass B concern — we
    # preserve any existing entries the auditor wrote rather than
    # clobbering them during scan.
    if "volatile_growth_anomalies" not in rec:
        rec["volatile_growth_anomalies"] = []
    rec["operator_decisions"] = operator_decisions

    # Status reflects staged entries (the silent updates don't surface).
    if summary.staged_total == 0:
        rec["status"] = "ok"
    elif summary.missing_actions_staged > 0 or summary.missing_crons_staged > 0:
        rec["status"] = "quiet_failure"
    elif summary.missing_files_staged > 0:
        rec["status"] = "drift_repair_needed"
    elif summary.extra_files_staged > 0:
        rec["status"] = "proposed_expansion"
    else:
        rec["status"] = "ok"

    # ── Bite 3 (spec §9.3): normalized drift events for the classifier ──────
    # Purely additive — the gate behavior above is unchanged. The scanner's
    # Phase-6 reconcile narrates the MAJOR ones into manifest.drift_log for
    # ``defined`` apps (drift_classifier.narrate_drift).
    summary.drift_events = _collect_drift_events(
        missing_files_silent=missing_silent,
        missing_files_staged=missing_staged,
        crons_silent=crons_silent,
        crons_staged=crons_staged,
        actions_silent=actions_silent,
        actions_staged=actions_staged,
        extra_silent=extra_silent,
        extra_staged=extra_staged,
    )

    return summary
