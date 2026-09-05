"""Change detector — decides whether a scan is warranted.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §7.6.

Time-based scan cadence wastes LLM tokens when nothing's changed and is
too slow when something has. The change detector — pure Python, no LLM
— diffs the current workspace state against a stored snapshot and
decides:

  scan_warranted: false  → no scan; skip the next quiet-window tick
  scope: "targeted"      → only the named apps need re-evaluation
  scope: "full_discovery" → unattributable files suggest a new app cluster

The detector runs every Tier 2 tick (every 6h). Its decision lands at
``{shared_dir}/applications/{bot_id}/.detector_decision.json`` so the
UI / CLI can render the current state without re-running the detector.

## What warrants a scan (spec §7.6.2)

Triggers:
  - New file not in any manifest's files[] and not matching any
    volatile_paths[*].glob, where inferred layer is code/config/
    behavior_doc
  - ≥3 unattributable files in any layer (suggests new app cluster)
  - Sha drift on a code/config/contract layer file claimed in a manifest
  - New cron entry not claimed by any manifest
  - Heartbeat section added or removed
  - Claimed code/config/contract file removed from disk

Non-triggers:
  - New files inside a volatile_paths[*].glob (expected by definition)
  - New files in data/log/state/content layers in app-convention dirs
    (handled by reconciliation silent-update path)
  - Sha drift on data/log/state/content layer files
  - File mtime changes without content change

## Defaults err on the side of NO SCAN

When the detector can't decide (snapshot corrupt, classifier import
fails, etc.), the safe default is ``scan_warranted: false`` — a scan
costs LLM tokens, a skipped scan just delays catching a change by 6h.
The exception: when the snapshot file is *missing entirely* (first run
after enable), the detector returns ``scan_warranted: true`` with
scope=full_discovery so the baseline gets established.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ── Decision shape ──────────────────────────────────────────────────────────


@dataclass
class ScanDecision:
    """The detector's verdict for a single bot.

    Shape mirrors spec §7.6.1's example output verbatim so the on-disk
    ``.detector_decision.json`` is the spec's exact contract.
    """
    scan_warranted: bool = False
    scope: str = "none"   # "full_discovery" | "targeted" | "none"
    reasons: list[str] = field(default_factory=list)
    targeted_apps: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    decided_at: str = ""
    error_status: str | None = None

    def to_dict(self) -> dict:
        return {
            "scan_warranted": self.scan_warranted,
            "scope": self.scope,
            "reasons": list(self.reasons),
            "targeted_apps": list(self.targeted_apps),
            "evidence": dict(self.evidence),
            "decided_at": self.decided_at,
            "error_status": self.error_status,
        }


# ── Snapshot capture and storage ────────────────────────────────────────────


def take_snapshot(
    workspace_path: Path,
    *,
    cron_labels: Iterable[str] | None = None,
    heartbeat_text: str | None = None,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    max_files: int = 5000,
) -> dict:
    """Capture a content-addressable snapshot of the workspace.

    The snapshot is the input the detector diffs against next tick.
    It records:

      - File paths + sha256 of every file (capped at ``max_files`` to
        avoid pathological cases on accidental data dumps)
      - The provided ``cron_labels`` set
      - The provided ``heartbeat_text`` (sha256, not full content —
        the full text is reconstructible from the workspace and we
        don't want the snapshot to balloon)

    Caller is responsible for invoking ``take_snapshot`` with the
    correct cron source (openclaw cron list + crontab + launchd) — the
    snapshot module is content-agnostic.

    Args:
        workspace_path: Bot's workspace root.
        cron_labels: Iterable of cron name strings to record.
        heartbeat_text: HEARTBEAT.md content (or whatever heartbeat
            file the bot uses).
        include_globs: Optional whitelist of path globs (relative to
            workspace_path). Default None = include all.
        exclude_globs: Optional blacklist. Always applied AFTER
            include_globs.
        max_files: Hard ceiling on snapshot size.

    Returns:
        A dict suitable for ``json.dump`` and later ``load_snapshot``.
    """
    files: dict[str, str] = {}
    if workspace_path.exists():
        include = list(include_globs or [])
        exclude = list(exclude_globs or [])
        for sub in sorted(workspace_path.rglob("*")):
            if not sub.is_file():
                continue
            try:
                rel = sub.relative_to(workspace_path).as_posix()
            except ValueError:
                continue
            if include and not any(fnmatch.fnmatch(rel, g) for g in include):
                continue
            if any(fnmatch.fnmatch(rel, g) for g in exclude):
                continue
            try:
                data = sub.read_bytes()
            except (PermissionError, OSError):
                continue
            files[rel] = hashlib.sha256(data).hexdigest()
            if len(files) >= max_files:
                break
    hb_sha = ""
    if heartbeat_text is not None:
        hb_sha = hashlib.sha256(heartbeat_text.encode("utf-8")).hexdigest()
    return {
        "version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "cron_labels": sorted(set(cron_labels or [])),
        "heartbeat_sha": hb_sha,
    }


def save_snapshot(snapshot: dict, path: Path) -> None:
    """Write the snapshot to a JSON file via atomic rename.

    The snapshot file lives at
    ``{shared_dir}/applications/{bot_id}/.last_scan_snapshot.json`` per
    spec §7.6.1. Atomic rename ensures partial writes never corrupt
    the file the next detector read consumes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_snapshot(path: Path) -> dict | None:
    """Load a snapshot from disk. Returns None on any read error —
    callers treat None as 'first run, no baseline'."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ── Manifest aggregation ────────────────────────────────────────────────────


def _all_claimed_paths(manifests: list[dict]) -> set[str]:
    """Set of every file path claimed by any manifest's files[]."""
    out: set[str] = set()
    for m in manifests:
        if not isinstance(m, dict):
            continue
        for entry in m.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path = (entry.get("path") or "").lstrip("/")
            if path:
                out.add(path)
    return out


def _all_volatile_globs(manifests: list[dict]) -> list[tuple[str, str]]:
    """Every (glob, owning_app_id) tuple across all manifests'
    volatile_paths."""
    out: list[tuple[str, str]] = []
    for m in manifests:
        if not isinstance(m, dict):
            continue
        app_id = m.get("id") or ""
        for vp in m.get("volatile_paths") or []:
            if not isinstance(vp, dict):
                continue
            glob = vp.get("glob") or ""
            if glob:
                out.append((glob, app_id))
    return out


def _all_claimed_cron_labels(manifests: list[dict]) -> set[str]:
    """Set of every cron label claimed by any manifest's crons[]."""
    out: set[str] = set()
    for m in manifests:
        if not isinstance(m, dict):
            continue
        for cron in m.get("crons") or []:
            if isinstance(cron, dict):
                label = cron.get("label") or cron.get("name") or ""
            elif isinstance(cron, str):
                label = cron
            else:
                continue
            if label:
                out.add(label)
    return out


def _manifest_owning(
    path: str,
    manifests: list[dict],
    claimed_paths_by_app: dict[str, set[str]] | None = None,
) -> str | None:
    """Return the app_id of the manifest that claims ``path`` via either
    its files[] or one of its volatile_paths[*].glob entries. None when
    no manifest claims it."""
    for m in manifests:
        if not isinstance(m, dict):
            continue
        app_id = m.get("id") or ""
        if not app_id:
            continue
        # files[]
        for entry in m.get("files") or []:
            if not isinstance(entry, dict):
                continue
            ep = (entry.get("path") or "").lstrip("/")
            if ep == path:
                return app_id
        # volatile_paths[]
        for vp in m.get("volatile_paths") or []:
            if not isinstance(vp, dict):
                continue
            glob = vp.get("glob") or ""
            if glob and fnmatch.fnmatch(path, glob):
                return app_id
    return None


# ── Decision logic ──────────────────────────────────────────────────────────

# Layers that warrant a scan when a new unattributable file appears with
# this layer (spec §7.6.2 rule 1).
_TRIGGER_LAYERS = frozenset({"code", "config", "behavior_doc"})

# Layers that warrant a scan when sha drift is detected on a claimed file
# (spec §7.6.2 rule 3).
_SHA_DRIFT_TRIGGER_LAYERS = frozenset({"code", "config", "contract"})

# Default threshold for "≥N unattributable files = likely new app".
# Spec §7.6.2: ≥3.
DEFAULT_DISCOVERY_THRESHOLD = 3


def _classify_path_layer(path: str) -> str | None:
    """Cheap layer guess for an unattributed file. Uses the rules-first
    classifier when available — falls back to None when import fails
    (the detector then treats the file as 'unknown layer', which means
    it counts toward the ≥3 discovery threshold but doesn't trigger
    the rule-1 single-file-of-trigger-layer warrant)."""
    try:
        from .layer_classifier import classify
    except Exception:
        return None
    result = classify(path)
    return result.layer


def detect(
    *,
    workspace_path: Path,
    current_snapshot: dict,
    prior_snapshot: dict | None,
    manifests: list[dict],
    discovery_threshold: int = DEFAULT_DISCOVERY_THRESHOLD,
    now_iso: str | None = None,
) -> ScanDecision:
    """The core decision function.

    Args:
        workspace_path: bot workspace root (used only for context — the
            actual file state comes from ``current_snapshot``).
        current_snapshot: snapshot taken at decision time.
        prior_snapshot: snapshot from the last scan. None = first run
            (forces full_discovery).
        manifests: list of manifest dicts as currently on disk.
        discovery_threshold: how many unattributable files trigger
            full_discovery (default 3 per spec §7.6.2).
        now_iso: timestamp override for testing.

    Returns:
        ScanDecision. ``scan_warranted=False, scope='none'`` means
        nothing changed; the caller should skip the scan.
    """
    decision = ScanDecision(decided_at=now_iso or datetime.now(timezone.utc).isoformat())

    # First-run case: no prior snapshot → establish baseline via full
    # discovery. Spec §7.6.7 first failure mode: "Snapshot file missing
    # or corrupted → detector treats as first run; recommends full
    # discovery."
    if not prior_snapshot:
        decision.scan_warranted = True
        decision.scope = "full_discovery"
        decision.reasons.append("first_run_no_snapshot")
        decision.evidence = {"file_count": len(current_snapshot.get("files") or {})}
        return decision

    prior_files: dict[str, str] = prior_snapshot.get("files") or {}
    current_files: dict[str, str] = current_snapshot.get("files") or {}
    prior_cron_labels = set(prior_snapshot.get("cron_labels") or [])
    current_cron_labels = set(current_snapshot.get("cron_labels") or [])
    prior_hb = prior_snapshot.get("heartbeat_sha") or ""
    current_hb = current_snapshot.get("heartbeat_sha") or ""

    claimed_paths = _all_claimed_paths(manifests)
    volatile_globs = _all_volatile_globs(manifests)
    claimed_cron_labels = _all_claimed_cron_labels(manifests)

    new_paths = sorted(set(current_files) - set(prior_files))
    removed_paths = sorted(set(prior_files) - set(current_files))
    drifted_paths = sorted(
        p for p in (set(prior_files) & set(current_files))
        if prior_files[p] != current_files[p]
    )

    # ── Rule 1: new unattributable file in trigger layer ───────────────────
    # AND
    # ── Rule 2: ≥N unattributable files in any layer ───────────────────────
    unattributable_paths: list[str] = []
    unattributable_in_trigger_layer: list[tuple[str, str]] = []
    for p in new_paths:
        # In any manifest's files[]?
        if p in claimed_paths:
            continue
        # In any volatile_paths glob?
        if any(fnmatch.fnmatch(p, g) for g, _ in volatile_globs):
            continue
        unattributable_paths.append(p)
        layer = _classify_path_layer(p)
        if layer in _TRIGGER_LAYERS:
            unattributable_in_trigger_layer.append((p, layer))

    if unattributable_in_trigger_layer:
        decision.scan_warranted = True
        decision.scope = "full_discovery"
        for p, layer in unattributable_in_trigger_layer[:3]:
            decision.reasons.append(
                f"new_unattributable_{layer}_file:{p}"
            )
        if len(unattributable_in_trigger_layer) > 3:
            decision.reasons.append(
                f"... and {len(unattributable_in_trigger_layer) - 3} more"
            )
    elif len(unattributable_paths) >= discovery_threshold:
        decision.scan_warranted = True
        decision.scope = "full_discovery"
        decision.reasons.append(
            f"{len(unattributable_paths)} unattributable file(s) "
            f"(≥{discovery_threshold} threshold)"
        )

    # ── Rule 3: sha drift on claimed code/config/contract file ─────────────
    targeted_apps: set[str] = set()
    for p in drifted_paths:
        owner = _manifest_owning(p, manifests)
        if owner is None:
            continue
        # Look up the file's recorded layer on the owning manifest.
        layer = None
        for m in manifests:
            if not isinstance(m, dict) or m.get("id") != owner:
                continue
            for entry in m.get("files") or []:
                if not isinstance(entry, dict):
                    continue
                if (entry.get("path") or "").lstrip("/") == p:
                    layer = entry.get("layer")
                    break
            break
        if layer in _SHA_DRIFT_TRIGGER_LAYERS:
            targeted_apps.add(owner)
            decision.reasons.append(
                f"sha_drift_on_{layer}_file:{p}@{owner}"
            )

    # ── Rule 4: new cron entry not claimed by any manifest ─────────────────
    new_cron_labels = current_cron_labels - prior_cron_labels
    unattributable_crons = new_cron_labels - claimed_cron_labels
    if unattributable_crons:
        for label in sorted(unattributable_crons)[:5]:
            decision.reasons.append(f"new_unattributable_cron:{label}")
        # Unattributable cron suggests a new app (or operator added a
        # cron outside the app framework) — full discovery is the
        # right scope.
        decision.scan_warranted = True
        if decision.scope != "full_discovery":
            decision.scope = "full_discovery"

    # ── Rule 5: heartbeat content changed ──────────────────────────────────
    if prior_hb and current_hb and prior_hb != current_hb:
        decision.reasons.append("heartbeat_content_changed")
        decision.scan_warranted = True
        # Heartbeat changes could affect any app — targeted scope can't
        # fit, so promote to full_discovery if not already.
        if decision.scope == "none":
            decision.scope = "full_discovery"

    # ── Rule 6: claimed code/config/contract file removed from disk ────────
    for p in removed_paths:
        owner = _manifest_owning(p, manifests)
        if owner is None:
            continue
        layer = None
        for m in manifests:
            if not isinstance(m, dict) or m.get("id") != owner:
                continue
            for entry in m.get("files") or []:
                if not isinstance(entry, dict):
                    continue
                if (entry.get("path") or "").lstrip("/") == p:
                    layer = entry.get("layer")
                    break
            break
        if layer in _SHA_DRIFT_TRIGGER_LAYERS:
            targeted_apps.add(owner)
            decision.reasons.append(
                f"claimed_{layer}_file_missing:{p}@{owner}"
            )

    # ── Resolve scope when only targeted reasons fired ─────────────────────
    if not decision.scan_warranted and targeted_apps:
        decision.scan_warranted = True
        decision.scope = "targeted"
        decision.targeted_apps = sorted(targeted_apps)
    elif decision.scope == "full_discovery" and targeted_apps:
        # Discovery already promoted — still record targeted apps as
        # additional info for the scan_router (so it can dispatch
        # tier-1 in parallel with the discovery LLM call).
        decision.targeted_apps = sorted(targeted_apps)

    # ── Evidence ────────────────────────────────────────────────────────────
    decision.evidence = {
        "new_files": len(new_paths),
        "removed_files": len(removed_paths),
        "drifted_files": len(drifted_paths),
        "unattributable_files": len(unattributable_paths),
        "unattributable_in_trigger_layer": len(unattributable_in_trigger_layer),
        "new_cron_labels": sorted(new_cron_labels),
        "removed_cron_labels": sorted(prior_cron_labels - current_cron_labels),
        "heartbeat_changed": prior_hb != current_hb,
        "targeted_apps": decision.targeted_apps,
    }

    return decision


# ── Decision file write ─────────────────────────────────────────────────────


def write_decision(
    decision: ScanDecision,
    bot_id: str,
    shared_dir: Path,
) -> Path:
    """Write the decision to its canonical location.

    Spec §7.6.1: ``{shared_dir}/applications/{bot_id}/.detector_decision.json``.
    Atomic via rename.
    """
    target = (shared_dir / "applications" / bot_id /
              ".detector_decision.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(decision.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_decision(bot_id: str, shared_dir: Path) -> ScanDecision | None:
    """Read the most recent decision file, or None if absent."""
    target = (shared_dir / "applications" / bot_id /
              ".detector_decision.json")
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ScanDecision(
        scan_warranted=bool(data.get("scan_warranted")),
        scope=data.get("scope") or "none",
        reasons=list(data.get("reasons") or []),
        targeted_apps=list(data.get("targeted_apps") or []),
        evidence=dict(data.get("evidence") or {}),
        decided_at=data.get("decided_at") or "",
        error_status=data.get("error_status"),
    )
