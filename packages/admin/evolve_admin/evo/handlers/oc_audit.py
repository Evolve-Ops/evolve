"""``evo security`` / ``evo, what can my bot do?`` handler.

Runs ``openclaw security audit --json`` for the invoking bot and returns a
plain-language summary of the audit score and any active findings.

This is the handler that answers "what can my bot do?" — formerly consulted
the Safety summary card (cut in v2.2 because it produced unverifiable claims).
The audit output replaces it: every finding here is a *measured* result from
OpenClaw's own security scanner, not an architectural assertion.

V2.4-3 rationale (see memory/feedback_safety_summary_less_useful_than_audit.md):
  - Safety summary bullets ("This bot can…", "This bot can't…") were cut because
    they either lied (cost cap as hard guarantee) or were tautological.
  - The audit score + active findings IS the right answer to "what can my bot do?"
    because it reflects measurable restrictions and deviations from baseline.
  - Plex-test friendly: every finding gets a plain-language explanation, not
    raw check IDs or technical jargon.

Response shape:
  - Audit score: XX/100 (label)
  - Active findings count (if any)
  - Plain-language rendering of each finding (≤8, severity-ordered)
  - Pointer to Skills and Security pages

Typical wall time: 20–60s (OpenClaw CLI subprocess). Tolerant of failures.
"""

from __future__ import annotations

import logging
from typing import Any

from ..identity import Role
from ._shared import speak


# ─────────────────────────────────────────────────────────────────────────────
# Score labels (Plex-test friendly: no jargon)
# ─────────────────────────────────────────────────────────────────────────────

def _score_label(score: int) -> str:
    """Plain-English label for an audit score (0–100)."""
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 55:
        return "fair"
    if score >= 35:
        return "needs attention"
    return "has issues"


# ─────────────────────────────────────────────────────────────────────────────
# Severity rendering
# ─────────────────────────────────────────────────────────────────────────────

_SEVERITY_ICON = {
    "critical": "🔴",
    "warning": "🟡",
    "info": "🔵",
}


def _sev_label(sev: str) -> str:
    """Short severity label for a finding."""
    if sev == "critical":
        return "urgent"
    if sev == "warning":
        return "heads-up"
    return "note"


# ─────────────────────────────────────────────────────────────────────────────
# Category plain-language labels (Plex-test friendly)
# ─────────────────────────────────────────────────────────────────────────────

_CAT_LABEL = {
    "channel": "channel setup",
    "exec": "command permissions",
    "config": "configuration",
    "auth": "authentication",
    "session": "session settings",
}


def _cat_label(cat: str) -> str:
    return _CAT_LABEL.get(cat, cat)


# ─────────────────────────────────────────────────────────────────────────────
# oc_cli import (same pattern as pod_summary.py's cross_bot_summary import)
# ─────────────────────────────────────────────────────────────────────────────

def _get_runtime():
    """Resolve the AgentRuntime seam from the evolve-analyzer package.

    Returns the runtime adapter or None if unavailable (graceful degradation).
    """
    try:
        from runtime.agent_runtime import get_runtime  # type: ignore[import-not-found]
        return get_runtime()
    except ImportError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Severity normalization — mirrors _audit_run_one in server.py
# ─────────────────────────────────────────────────────────────────────────────

_CAT_MAP = {
    "gateway": "channel", "tools": "exec", "fs": "config",
    "models": "config", "security": "auth", "session": "session",
    "auth": "auth", "summary": "config",
}


def _normalize_findings(raw_findings: list[dict]) -> list[dict]:
    """Normalize raw OC findings into the same shape _audit_run_one produces.

    Routes every finding through the analyzer audit's single-source helper
    (analyzer/audit.py::normalize_oc_finding) so this third forwarder applies
    EXACTLY the drop/demote rules the Security page (_audit_run_one) and the
    daemon use. That kills the "keep the lists in sync" hazard that caused the
    mask-FP drift (memory:feedback_three_oc_audit_forwarders_must_share_suppression):
    the Linux ACL-mask false-positive drop, the member-bot "full" exec drop, and
    the generic multi-user prose demote all live in one place now.

    Surface-local context: the tray has no gateway_bind / routing / primary-
    model config to pass, so it calls the helper WITHOUT context. The two
    context-dependent demotions (proxy-header on a loopback gateway; model
    below-recommended) then never fire here — exactly the tray's historical
    behavior (it kept those findings as-is; the Security page demotes them with
    its config context).

    Score recomputes downstream from the kept findings, so a drop/demote needs
    no counter bookkeeping here.
    """
    try:
        from evolve_admin.web.server import _import_analyzer
        _normalize = _import_analyzer("audit").normalize_oc_finding
    except Exception as exc:
        # Fail-open: if the analyzer helper can't be imported/run, surface
        # findings un-normalized (warn→warning) rather than hide them — but log
        # rather than silently swallow, so a broken seam is diagnosable instead
        # of invisibly un-gating the whole normalization pod-wide.
        logging.getLogger(__name__).debug(
            "OC-audit normalize seam unavailable, surfacing raw findings: %s", exc
        )
        _normalize = None

    out = []
    for f in raw_findings:
        check_id = f.get("checkId", "")
        cat_key = check_id.split(".")[0] if check_id else "config"

        if _normalize is None:
            sev = "warning" if f.get("severity") == "warn" else f.get("severity", "info")
        else:
            decision = _normalize(f)  # no surface context — see docstring
            if decision.drop:
                continue
            sev = decision.severity

        out.append({
            "severity": sev,
            "category": _CAT_MAP.get(cat_key, "config"),
            "message": f.get("title", ""),
            "recommendation": f.get("remediation", ""),
        })
    return out


def _score_from_findings(findings: list[dict]) -> int:
    """Compute a 0–100 score from normalized findings (same formula as server.py)."""
    crit = sum(1 for f in findings if f["severity"] == "critical")
    warn = sum(1 for f in findings if f["severity"] == "warning")
    return max(0, 100 - crit * 20 - warn * 5)


# ─────────────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────────────

def _render_finding(f: dict) -> str:
    """Format one finding as a bullet line (plain-language, no check IDs)."""
    sev = f.get("severity", "info")
    icon = _SEVERITY_ICON.get(sev, "•")
    msg = str(f.get("message") or "(unnamed finding)")
    rec = str(f.get("recommendation") or "").strip()
    cat = _cat_label(str(f.get("category") or "config"))
    label = _sev_label(sev)
    line = f"{icon} {label} ({cat}): {msg}"
    if rec and len(rec) < 120:
        line += f"\n  → {rec}"
    return line


def _render_response(bot_id: str, findings: list[dict], score: int,
                     generated_at: str | None) -> str:
    """Build the full plain-language audit response."""
    label = _score_label(score)
    header = f"**Security snapshot — {bot_id}**\nAudit score: {score}/100 ({label})"

    # Filter out info-only findings for the summary view — they're advisory.
    surface = [f for f in findings if f.get("severity") in ("critical", "warning")]
    surface_sorted = sorted(surface, key=lambda f: (
        0 if f.get("severity") == "critical" else 1,
        str(f.get("message") or ""),
    ))

    if not surface_sorted:
        body = (
            f"{header}\n\n"
            "No active issues.\n\n"
            "To see what's installed: use the Skills page.\n"
            "Full security details: Security page."
        )
        return body

    count = len(surface_sorted)
    plural = "things" if count != 1 else "thing"
    body_lines = [
        header,
        "",
        f"Currently {count} {plural} to look at:",
        "",
    ]

    for f in surface_sorted[:8]:
        body_lines.append(_render_finding(f))
        body_lines.append("")

    if len(surface_sorted) > 8:
        body_lines.append(f"…and {len(surface_sorted) - 8} more on the Security page.")
        body_lines.append("")

    if generated_at:
        # Format "2026-05-13T10:30:00Z" → "2026-05-13 10:30 UTC"
        ts = generated_at.replace("T", " ").replace("Z", " UTC").split(".")[0] + " UTC" \
            if "T" in generated_at else generated_at
        body_lines.append(f"(Audit run: {ts})")
        body_lines.append("")

    body_lines.append(
        "To see what's installed: use the Skills page.\n"
        "Full security details: Security page."
    )

    return "\n".join(body_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    """Handle ``evo security`` / ``evo, what can my bot do?``.

    Runs ``openclaw security audit --json`` for ``bot_id``, normalizes
    the findings, and renders a plain-language score + findings summary.

    Tolerant of OC CLI failures: if the audit can't run (bot offline,
    OC not installed, timeout), surfaces a graceful fallback that points
    the user at the admin UI Security page.
    """
    runtime = _get_runtime()
    if runtime is None:
        body = (
            f"**Security snapshot — {bot_id}**\n\n"
            "The OpenClaw CLI isn't available right now — can't run the audit.\n\n"
            "Check the Security page on the admin dashboard for the latest results."
        )
        return speak("security", body, role)

    try:
        raw = runtime.security_audit(bot_id)
    except Exception as exc:
        body = (
            f"**Security snapshot — {bot_id}**\n\n"
            f"The audit ran into a problem: {exc}\n\n"
            "Try again in a moment, or check the Security page on the admin dashboard."
        )
        return speak("security", body, role)

    if not isinstance(raw, dict):
        body = (
            f"**Security snapshot — {bot_id}**\n\n"
            "The audit didn't return a result — the bot may be offline, "
            "or OpenClaw may not be installed.\n\n"
            "Check the Security page on the admin dashboard."
        )
        return speak("security", body, role)

    raw_findings = raw.get("findings") or []
    findings = _normalize_findings(raw_findings)
    score = _score_from_findings(findings)

    ts_ms = raw.get("ts", 0)
    generated_at: str | None = None
    if ts_ms:
        try:
            import datetime as _dt
            generated_at = (
                _dt.datetime.utcfromtimestamp(ts_ms / 1000)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except Exception:
            pass

    body = _render_response(bot_id, findings, score, generated_at)
    return speak("security", body, role)
