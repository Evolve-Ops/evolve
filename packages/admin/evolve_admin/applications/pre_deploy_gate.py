"""Pre-deploy coherence gate — runs Pass A before manifests ship.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.6.

Pass A (the cheap manifest-internal graph walk shipped in PR 4) runs
during scan + on every Tier 2 audit cycle. PR 8 wires it into two more
points where catching incoherence early avoids shipping a broken
contract:

  Forge approval     — must be ``ok`` or ``warnings`` to proceed;
                       ``incoherent`` requires operator override
  Manifest editor    — save button is disabled if Pass A's status
                       would be ``incoherent``

Pass A is a **warning** (non-blocking) for:
  Scanner-discovered manifests on first write
  Re-scans of existing manifests (incoherence may pre-date the gate)

## Why a gate matters

Without it, an operator can approve a forge build whose description
claims "daily briefing" but has zero scheduled_actions — the spec's
C-A1 finding. The build ships, runs, and silently does nothing. The
gate catches that at approval time, when the operator can still decide
to override or send back for revision.

## Gate modes (spec §6.6 + §13.6)

The gate has three modes per the migration plan:

  ``blocker``  — incoherent manifests refuse to save / approve.
                 Operator override is the only path forward.
                 Default for forge approval.
  ``warning``  — incoherent manifests save / approve, but the
                 operator sees a clear warning. Default for scanner
                 writes and re-scans (spec §13.6 first 30 days).
  ``off``      — gate is skipped entirely. Reserved for emergencies.

Mode is per-call; callers decide.

## Return shape

``check_pre_deploy`` returns a ``GateVerdict`` with:

  allowed       — True if the operation should proceed (warning mode
                  always returns True; blocker mode returns False on
                  incoherent)
  status        — Pass A's coherence status (ok / warnings / incoherent)
  reason        — human-readable explanation when blocked
  findings      — Pass A finding list (callers can render in UI)
  override_key  — short hash an operator's override request must
                  reference. Prevents stale override approvals
                  ("operator clicked override on stale finding list")
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


# Gate modes.
MODE_BLOCKER = "blocker"
MODE_WARNING = "warning"
MODE_OFF     = "off"

VALID_MODES = frozenset({MODE_BLOCKER, MODE_WARNING, MODE_OFF})

# Status values from Pass A.
STATUS_OK         = "ok"
STATUS_WARNINGS   = "warnings"
STATUS_INCOHERENT = "incoherent"


@dataclass
class GateVerdict:
    """The pre-deploy gate's decision for a single check."""
    allowed: bool = True
    status: str = STATUS_OK
    reason: str = ""
    findings: list[dict] = field(default_factory=list)
    override_key: str = ""
    mode: str = MODE_WARNING

    def to_dict(self) -> dict:
        return {
            "allowed":      self.allowed,
            "status":       self.status,
            "reason":       self.reason,
            "findings":     list(self.findings),
            "override_key": self.override_key,
            "mode":         self.mode,
        }


def _c3_finding_from_cache(manifest: dict) -> dict | None:
    """Read manifest.coherence.last_capability_check and return a
    finding-shaped dict iff C3 said incoherent or unclear.

    Spec §6.5: C3 writes to ``manifest.coherence.last_capability_check``
    (NOT findings[], which is the recurring-pass log). The gate reads
    that slot and surfaces the verdict as an additional finding.

    Returns:
        - dict (synthetic finding) when C3 ran AND severity is
          incoherent or unclear (worth flagging in the gate response)
        - None when C3 hasn't run, the result is feasible, or the
          shape is malformed

    The synthetic finding's severity maps:
        c3 incoherent → critical (gate-elevating)
        c3 unclear    → minor (surfaced but not blocking)
        c3 feasible   → None (no finding emitted)
    """
    coherence = manifest.get("coherence") or {}
    last = coherence.get("last_capability_check")
    if not isinstance(last, dict):
        return None
    severity = (last.get("severity") or "").strip().lower()
    rationale = (last.get("rationale") or "").strip()
    if severity == "feasible":
        return None
    if severity not in {"incoherent", "unclear"}:
        return None
    if not rationale:
        return None
    # Map C3 severity → coherence finding severity.
    mapped_severity = "critical" if severity == "incoherent" else "minor"
    return {
        "id":          "C3",
        "check_id":    "C3",
        "severity":    mapped_severity,
        "assertion":   "capability_check",
        "description": (
            f"Pass C3 verdict: {severity!r}. {rationale[:280]}"
        ),
        "message":     rationale,
        "signature":   f"C3:{severity}:{rationale[:40]}",
        "evidence":    [{"checked_at": last.get("checked_at", "")}],
        # Helps the chip surface know this came from cache (no fresh LLM call).
        "source":      "c3_cached",
    }


def _override_key_for(findings: list[dict]) -> str:
    """Stable hash of the finding-signature set. Override approvals
    must reference this key — if the findings change between gate-check
    and override-submit, the key changes and the override is rejected.

    Prevents "operator approved an old override; new findings landed in
    between; ship anyway" failure mode.
    """
    sigs = sorted(
        (f.get("signature") or f.get("id") or "")
        for f in findings
    )
    blob = "|".join(sigs)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def check_pre_deploy(
    manifest: dict,
    *,
    mode: str = MODE_BLOCKER,
    override_key: str | None = None,
) -> GateVerdict:
    """Run Pass A against ``manifest`` and decide whether to allow the
    operation.

    Args:
        manifest: the manifest dict that's about to be saved / approved
            / shipped. Pass A is run against a COPY-LIKE view (we don't
            mutate the manifest's existing coherence block; the gate's
            check is transient).
        mode: one of MODE_BLOCKER / MODE_WARNING / MODE_OFF. Callers
            decide the policy — forge approval is typically blocker;
            scanner writes are typically warning.
        override_key: if the operator has already overridden the
            findings, the key they referenced. The gate compares it
            against the current override_key for the manifest's
            findings; if they match, ``allowed`` is forced True even
            in blocker mode.

    Returns:
        GateVerdict. Callers consult ``verdict.allowed`` to decide
        whether to proceed.
    """
    if mode == MODE_OFF:
        return GateVerdict(allowed=True, status=STATUS_OK, mode=mode)
    if mode not in VALID_MODES:
        # Unknown mode → most conservative path: block.
        return GateVerdict(
            allowed=False, status=STATUS_INCOHERENT,
            reason=f"unknown gate mode {mode!r}",
            mode=MODE_BLOCKER,
        )

    # Run Pass A against a shallow copy so we don't write to the
    # caller's coherence block (some callers haven't decided to persist
    # yet — they're just checking what would happen).
    try:
        from .coherence_pass_a import run_pass_a, status_for_findings
    except Exception as exc:
        # Pass A can't be loaded — fail open in warning mode (let the
        # operation through), closed in blocker mode (refuse).
        if mode == MODE_BLOCKER:
            return GateVerdict(
                allowed=False, status=STATUS_INCOHERENT,
                reason=f"coherence_pass_a import failed: {exc}",
                mode=mode,
            )
        return GateVerdict(
            allowed=True, status=STATUS_WARNINGS,
            reason=f"gate skipped (Pass A unavailable: {exc})",
            mode=mode,
        )

    findings = run_pass_a(manifest)
    finding_dicts = [f.to_dict() for f in findings]
    status = status_for_findings(findings)

    # ── Pass C3 capability check (spec §6.5) ──────────────────────────
    # When a recent C3 result is cached on the manifest, honor it:
    #   incoherent → escalates to an incoherent gate verdict + appended
    #                finding
    #   unclear / feasible → no effect
    # The actual LLM dispatch happens elsewhere (admin daemon socket /
    # bot gateway) — this gate just READS the cached result. Decouples
    # the cost-bearing LLM call from the deterministic gate check.
    c3_finding = _c3_finding_from_cache(manifest)
    if c3_finding is not None:
        finding_dicts.append(c3_finding)
        # Incoherent C3 elevates the status; feasible/unclear don't.
        if c3_finding.get("severity") == "critical":
            status = STATUS_INCOHERENT

    key = _override_key_for(finding_dicts)

    verdict = GateVerdict(
        status=status, findings=finding_dicts,
        override_key=key, mode=mode,
    )

    # No findings → always allowed.
    if status == STATUS_OK:
        verdict.allowed = True
        return verdict

    # Warnings only → always allowed (operators see the warnings on
    # the response but the operation proceeds).
    if status == STATUS_WARNINGS:
        verdict.allowed = True
        verdict.reason = (
            f"Pass A reported {len(finding_dicts)} minor coherence "
            f"warning(s); operation proceeds"
        )
        return verdict

    # Incoherent (critical or major findings):
    if mode == MODE_WARNING:
        verdict.allowed = True
        verdict.reason = (
            f"Pass A reported {len(finding_dicts)} coherence finding(s) "
            f"including critical/major; operation proceeds because gate "
            f"mode is 'warning'"
        )
        return verdict

    # Blocker mode — check for an override.
    if override_key and override_key == key:
        verdict.allowed = True
        verdict.reason = (
            f"Pass A reported {len(finding_dicts)} coherence finding(s); "
            f"operation proceeds under operator override"
        )
        return verdict

    verdict.allowed = False
    verdict.reason = (
        f"Pass A reported {len(finding_dicts)} coherence finding(s) "
        f"(status={STATUS_INCOHERENT}); operation blocked. "
        f"Operator may override by submitting with override_key={key}."
    )
    return verdict


# ── Caller-side helpers ────────────────────────────────────────────────────


def forge_approval_gate(manifest: dict, *,
                        override_key: str | None = None) -> GateVerdict:
    """Pre-set blocker mode — forge approval cares enough to refuse."""
    return check_pre_deploy(manifest, mode=MODE_BLOCKER,
                            override_key=override_key)


def manifest_editor_gate(manifest: dict, *,
                         override_key: str | None = None) -> GateVerdict:
    """Pre-set blocker mode for manual editor saves. The UI surfaces
    the override path explicitly."""
    return check_pre_deploy(manifest, mode=MODE_BLOCKER,
                            override_key=override_key)


def scanner_write_gate(manifest: dict) -> GateVerdict:
    """Pre-set warning mode — scanner writes never block.

    Spec §6.6: 'Pass A is a warning (non-blocking) for: Scanner-
    discovered manifests on first write...'
    """
    return check_pre_deploy(manifest, mode=MODE_WARNING)
