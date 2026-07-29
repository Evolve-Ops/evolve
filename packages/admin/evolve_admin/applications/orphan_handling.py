"""Orphan app detection + hysteresis — PR 14.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §9.

An app is "orphaned" when every infrastructure file (code / config /
contract layer) the manifest claims is missing from disk AND no
volatile_paths[*].glob is matching any workspace files. The manifest
stays on disk (the never-delete-manifest doctrine) but the app
visibly moves to the Retired section of the Apps page.

## Hysteresis

Spec §9: "orphan status requires two consecutive Tier 2 runs (12h
apart) confirming the condition. Prevents false positives during
deploys."

The state machine reads from manifest.reconciliation block (no new
storage), specifically a small ``orphan_check_history`` list of
recent verdicts. Two consecutive ``orphan`` verdicts → confirmed.

## Provenance modulates response

Spec §9:

  Observational orphan — silent. The manifest's files[] empties
                         then reconciliation.status = "orphan";
                         no notification.
  Authored orphan      — loud. Chip on Apps page, notification to
                         operator (and optionally bot user).

This module decides observational vs authored and exposes the
verdict to the caller (notifier / UI).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Hysteresis: confirm orphan after this many consecutive verdicts.
DEFAULT_CONSECUTIVE_VERDICTS = 2

# Infrastructure layers — when every entry in these layers is missing,
# orphan status applies.
#
# Spec §9 originally said {code, config, contract}; production validation
# 2026-06-06 surfaced that ``procedure`` (LLM-readable runbooks the
# heartbeat loads) is also infra — if procedures/ is empty, the cron
# has no instructions, even though code/config are intact. Added here.
_INFRA_LAYERS = frozenset({"code", "config", "contract", "procedure"})


# Orphan verdicts.
VERDICT_NOT_ORPHAN = "not_orphan"
VERDICT_PENDING    = "pending"
VERDICT_ORPHAN     = "orphan"


@dataclass
class OrphanVerdict:
    """The decision for one Tier 2 tick."""
    verdict: str = VERDICT_NOT_ORPHAN
    consecutive_orphan_verdicts: int = 0
    is_authored: bool = False
    reason: str = ""
    checked_at: str = ""

    @property
    def confirmed(self) -> bool:
        return self.verdict == VERDICT_ORPHAN

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "consecutive_orphan_verdicts": self.consecutive_orphan_verdicts,
            "is_authored": self.is_authored,
            "reason": self.reason,
            "checked_at": self.checked_at,
        }


def _is_field_authored(manifest: dict, field_name: str) -> bool:
    prov = manifest.get("provenance") or {}
    field_origins = prov.get("field_origins") or {}
    entry = field_origins.get(field_name)
    if not isinstance(entry, dict):
        return False
    return (entry.get("source") or "").strip().lower() in {
        "forge_built", "user_authored", "bot_authored", "confirmed",
    }


def _infra_files(manifest: dict) -> list[dict]:
    """Return files[] entries whose layer is in _INFRA_LAYERS."""
    out = []
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("layer") in _INFRA_LAYERS:
            out.append(entry)
    return out


def evaluate_orphan(
    manifest: dict,
    *,
    workspace_files: set[str] | None = None,
    now_iso: str | None = None,
    consecutive_required: int = DEFAULT_CONSECUTIVE_VERDICTS,
) -> OrphanVerdict:
    """Evaluate whether the manifest is an orphan.

    Reads ``manifest.reconciliation.missing_files`` (populated by the
    reconciliation pass in PR 4) — when EVERY infra-layer file is in
    that list AND no volatile_paths glob has files in it,
    this tick verdicts orphan.

    ``workspace_files`` is the set of paths actually on disk —
    used to check whether any volatile_paths glob has files. ``None``
    means "scanner didn't compute", which is treated as "skip the
    glob check" (defensive).

    The verdict's ``consecutive_orphan_verdicts`` field gets read
    from ``manifest.reconciliation.orphan_check_history`` if present;
    callers persist via ``record_orphan_verdict``.

    Returns:
        OrphanVerdict with ``confirmed=True`` only after
        ``consecutive_required`` orphan ticks in a row.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()
    verdict = OrphanVerdict(checked_at=now)

    infra = _infra_files(manifest)
    if not infra:
        # No infra files claimed → either a pure-data app (not orphan)
        # or a manifest that never had files[]. Either way, not orphan.
        verdict.verdict = VERDICT_NOT_ORPHAN
        verdict.reason = "manifest has no infra-layer files claimed"
        return verdict

    # Check reconciliation.missing_files[] for missing infra files.
    rec = manifest.get("reconciliation") or {}
    missing_paths = {
        (m.get("path") or "").lstrip("/")
        for m in rec.get("missing_files") or []
        if isinstance(m, dict)
    }
    infra_paths = {(e.get("path") or "").lstrip("/") for e in infra}
    missing_infra = infra_paths - (infra_paths - missing_paths)
    all_infra_missing = missing_infra == infra_paths

    # Also check volatile_paths gloss against workspace files.
    if workspace_files is not None and all_infra_missing:
        import fnmatch
        for vp in manifest.get("volatile_paths") or []:
            if not isinstance(vp, dict):
                continue
            glob = vp.get("glob") or ""
            if not glob:
                continue
            if any(fnmatch.fnmatch(p, glob) for p in workspace_files):
                all_infra_missing = False
                break

    if not all_infra_missing:
        verdict.verdict = VERDICT_NOT_ORPHAN
        verdict.reason = (
            f"{len(infra) - len(missing_infra)} of {len(infra)} "
            f"infra files still present (or volatile_paths active)"
        )
        return verdict

    # Apply hysteresis. Read prior verdicts from manifest.
    # PENDING + ORPHAN entries both mean "all infra missing on this tick";
    # only NOT_ORPHAN breaks the chain (and record_orphan_verdict clears
    # history on NOT_ORPHAN, so any entries present count toward the
    # chain).
    history = rec.get("orphan_check_history") or []
    consecutive_priors = sum(
        1 for h in history
        if h.get("verdict") in (VERDICT_PENDING, VERDICT_ORPHAN)
    )
    # +1 for THIS tick.
    consecutive = consecutive_priors + 1
    verdict.consecutive_orphan_verdicts = consecutive
    verdict.is_authored = _is_field_authored(manifest, "files")

    if consecutive >= consecutive_required:
        verdict.verdict = VERDICT_ORPHAN
        verdict.reason = (
            f"all {len(infra)} infra files missing for "
            f"{consecutive} consecutive tick(s)"
        )
    else:
        verdict.verdict = VERDICT_PENDING
        verdict.reason = (
            f"all {len(infra)} infra files missing this tick; "
            f"need {consecutive_required - consecutive} more consecutive "
            f"tick(s) to confirm"
        )
    return verdict


def record_orphan_verdict(
    manifest: dict, verdict: OrphanVerdict,
    *, max_history: int = 5,
) -> None:
    """Persist the verdict into manifest.reconciliation.orphan_check_history.

    Trims to ``max_history`` entries so the manifest doesn't grow
    unboundedly.
    """
    rec = manifest.setdefault("reconciliation", {})
    history = rec.setdefault("orphan_check_history", [])
    # Only record orphan verdicts (clears history on not_orphan to
    # reset the hysteresis chain).
    if verdict.verdict == VERDICT_NOT_ORPHAN:
        rec["orphan_check_history"] = []
        # Also clear orphan status if previously set.
        if rec.get("status") == "orphan":
            rec["status"] = "ok"
        return
    history.append({
        "ts":      verdict.checked_at,
        "verdict": verdict.verdict,
        "reason":  verdict.reason,
    })
    rec["orphan_check_history"] = history[-max_history:]
    if verdict.confirmed:
        rec["status"] = "orphan"


def notification_message(
    *, app_id: str, verdict: OrphanVerdict, bot_id: str = "",
) -> str | None:
    """Build the operator notification for an orphan confirmation.

    Returns None when the verdict shouldn't notify:
      - not confirmed
      - confirmed but observational (silent retirement)

    Confirmed + authored → return a message.
    """
    if not verdict.confirmed or not verdict.is_authored:
        return None
    parts = []
    if bot_id:
        parts.append(f"{bot_id}:")
    parts.append(
        f"app {app_id!r} is orphan — every infrastructure file is "
        f"missing from disk. The manifest stays for archival; the app "
        f"has moved to the Retired section. Options: Reinstall (forge "
        f"the spec back), Archive, or Convert to template."
    )
    return " ".join(parts)
