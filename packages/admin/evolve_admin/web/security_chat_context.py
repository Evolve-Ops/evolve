"""evolve_admin.web.security_chat_context — server-side Security-page pack
for evo's chat drawer.

The client-side pack at index.html `_EVO_CONTEXT_PACKS.security` ships
per-bot summaries with title-only finding lists. That shape forces evo
to fan out into `pod_state(query="audit", bot_id=...)` for every affected bot
when the operator asks "which bots have <X>?" or "what's the detail
behind <Y>?" — eight tool calls × per-bot round-trip latency is the
primary contributor to the 270s timeout pattern observed on this page.

This module builds a denser snapshot from canonical sources
(audit_state.snapshot, signals.store) so evo can answer common
questions from context alone. The two key reshapes:

* **Findings coalesced across bots** by message text (1:1 with OC's
  underlying `checkId`). One entry per distinct advisory with the
  affected-bots list inline, instead of the same advisory repeated in
  every bot's section.
* **Firing signals inlined with detail** (signature, producer,
  occurrence count, affected bot_id) instead of just titles. Same
  data the Active Signals chips show on screen.

The wire format is JSON-encodable and stays well under the 1500-char
page-state cap in :func:`home_chat_routes._prepend_page_context`.

Wiring: :func:`home_chat_routes._maybe_reshape_page_context` calls
:func:`build_security_state` when ``page_context.page_id == "security"``
and replaces the client-provided ``state`` blob.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


# Tunables. The downstream cap is the proxy's rendered-body cap
# (`proxy._SUMMARY_MAX_CHARS` = 4000). The renderer adds headers,
# bullet-list prefixes, and section labels on top of our raw payload,
# so these per-section caps must leave ~10% margin for that overhead.
# Adjusted empirically against the worst-case test (10 bots × 8 long-
# remediation findings + 12 signals + 6 drift entries).
_MAX_FINDINGS = 8
_MAX_SIGNALS = 5
_MAX_PER_BOT = 10
_MAX_DRIFT = 5
# Per-string truncation. Audit titles are short; remediations can be
# 1-2 sentences. Trim to keep the pack readable inline.
_MAX_TITLE_CHARS = 90
_MAX_REMEDIATION_CHARS = 110
# When a coalesced finding affects many bots, listing every bot_id
# eats the budget without adding much signal — the operator's next
# question is usually "which one is worst", not "give me all 12".
_MAX_BOTS_PER_FINDING = 6


def _truncate(s: str, limit: int) -> str:
    """Shorten ``s`` to ``limit`` chars with an ellipsis when cut."""
    if not s:
        return ""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


# Severity ordering for stable sort across findings and signals.
# Higher number = more urgent.
_SEV_RANK = {
    "critical": 3,
    "warning": 2,
    "warn": 2,        # alias used by some producers
    "info": 1,
    "advisory": 1,
}


def _sev_rank(sev: str | None) -> int:
    return _SEV_RANK.get((sev or "info").lower(), 0)


def _normalize_severity(sev: str | None) -> str:
    """Map producer-side aliases ("warn") onto the canonical token
    ("warning") so the chat pack reads consistently regardless of
    which source emitted the finding."""
    s = (sev or "info").lower()
    if s == "warn":
        return "warning"
    return s


def _coalesce_findings(audit_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Group findings across bots by message text.

    The audit cache shape per ``_audit_run_one`` drops ``checkId`` in
    favor of the human title (``message``); same checkId always
    produces the same title, so message-keying is stable for v1. If a
    future schema change reintroduces ``check_id`` in the cached
    finding dict we can switch the key without changing the output
    shape.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for bot_id, result in (audit_data or {}).items():
        if not isinstance(result, dict):
            continue
        if result.get("unavailable"):
            continue
        for f in result.get("findings") or []:
            if not isinstance(f, dict):
                continue
            title = (f.get("message") or "").strip()
            if not title:
                continue
            sev = _normalize_severity(f.get("severity"))
            key = f"{sev}::{title}"
            entry = by_key.get(key)
            if entry is None:
                entry = {
                    "severity": sev,
                    "category": f.get("category") or "config",
                    "title": _truncate(title, _MAX_TITLE_CHARS),
                    "remediation": _truncate(
                        (f.get("recommendation") or "").strip(),
                        _MAX_REMEDIATION_CHARS,
                    ),
                    "bots": [],
                }
                by_key[key] = entry
            entry["bots"].append(bot_id)
    out: list[dict[str, Any]] = []
    for entry in by_key.values():
        all_bots = sorted(set(entry["bots"]))
        entry["count"] = len(all_bots)
        # Cap the bot list rendered inline so a finding affecting many
        # bots doesn't dominate the budget. The count is preserved in
        # ``count`` so the model still knows the true blast radius.
        if len(all_bots) > _MAX_BOTS_PER_FINDING:
            shown = all_bots[:_MAX_BOTS_PER_FINDING]
            entry["bots"] = shown + [f"+{len(all_bots) - _MAX_BOTS_PER_FINDING} more"]
        else:
            entry["bots"] = all_bots
        out.append(entry)
    out.sort(key=lambda e: (-_sev_rank(e["severity"]), -e["count"], e["title"]))
    return out


def _shape_signals(firing: Iterable[Any]) -> list[dict[str, Any]]:
    """Render firing-signal dicts in a chat-friendly inline form.

    Accepts either Signal objects (with ``.to_dict()``) or pre-shaped
    dicts so callers can hand in fixtures directly. Severity is read
    from the framework dict and surfaced inline so evo can prioritize
    without re-querying.
    """
    out: list[dict[str, Any]] = []
    for s in firing or []:
        if hasattr(s, "to_dict"):
            d = s.to_dict()
        elif isinstance(s, dict):
            d = s
        else:
            continue
        sev_framework = d.get("severity_framework") or {}
        if not isinstance(sev_framework, dict):
            sev_framework = {}
        vec = (sev_framework.get("vector") or "").strip()
        mag = sev_framework.get("magnitude")
        try:
            mag_int = int(mag) if mag is not None else None
        except (TypeError, ValueError):
            mag_int = None
        title = (d.get("title") or "").strip()
        entry: dict[str, Any] = {
            "signature": (d.get("signature") or "").strip() or None,
            "title": _truncate(title, _MAX_TITLE_CHARS),
            "producer": (d.get("producer") or "").strip() or None,
            "bot_id": (d.get("bot_id") or "").strip() or None,
            "occurrence_count": int(d.get("occurrence_count") or 0),
            "type": (d.get("type") or "").strip() or None,
        }
        if vec:
            entry["severity_vector"] = vec
        if mag_int is not None:
            entry["severity_magnitude"] = mag_int
        out.append(entry)
    # Sort by (magnitude desc, occurrence_count desc, title asc) so the
    # urgent recurring stuff floats to the top of the inline list.
    out.sort(key=lambda e: (
        -(e.get("severity_magnitude") or 0),
        -(e.get("occurrence_count") or 0),
        e.get("title") or "",
    ))
    return out


def _shape_per_bot(audit_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact per-bot row: bot_id + score + advisory counts.

    Findings detail lives on the coalesced list, so this row is just
    the scoreboard. Bots whose audit is unavailable get an inline
    error_type so evo can explain *why* a bot is missing instead of
    silently dropping it.
    """
    rows: list[dict[str, Any]] = []
    for bot_id, result in (audit_data or {}).items():
        if not isinstance(result, dict):
            continue
        if result.get("unavailable"):
            rows.append({
                "bot_id": bot_id,
                "unavailable": True,
                "error_type": (result.get("error_type") or "unknown"),
            })
            continue
        rows.append({
            "bot_id": bot_id,
            "score": result.get("score"),
            "critical": int(result.get("critical") or 0),
            "warn": int(result.get("warned") or 0),
            "info": int(result.get("info") or 0),
        })
    # Bots with critical findings first, then by score asc (lowest = most pressing).
    def _key(row: dict[str, Any]) -> tuple:
        if row.get("unavailable"):
            return (-1, 0, row.get("bot_id") or "")
        return (
            -int(row.get("critical") or 0),
            int(row.get("score") or 100),
            row.get("bot_id") or "",
        )
    rows.sort(key=_key)
    return rows


def _shape_backup_drift(
    drift_hint: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Pass through client-provided backup-drift entries with a small
    shape clean-up. Server-side drift detection runs git per bot
    (heal.detect_backup_drift_keys); calling it per chat turn would
    add seconds of latency for an off-subtab read. The client already
    surfaces drift when the operator is on the Backups subtab, so the
    hint is the right channel for v1. If the operator asks about
    drift from a different subtab without a hint, evo can call
    ``pod_state(query="config_drift")`` — same tool pointer the client
    pack advertises today.
    """
    out: list[dict[str, Any]] = []
    for entry in drift_hint or []:
        if not isinstance(entry, dict):
            continue
        bot_id = (entry.get("bot_id") or "").strip()
        if not bot_id:
            continue
        raw_keys = entry.get("drifted_keys") or []
        # The client packer joins drifted_keys into a comma-separated
        # string before sending; the snapshot cache holds them as a
        # list. Accept either so this helper composes cleanly with
        # both shapes (and with hand-rolled test fixtures).
        if isinstance(raw_keys, str):
            keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        elif isinstance(raw_keys, list):
            keys = [str(k) for k in raw_keys if k]
        else:
            keys = []
        out.append({
            "bot_id": bot_id,
            "drifted_keys": keys[:10],
            "stale_backup": bool(entry.get("stale_backup")),
            "last_backup_at": entry.get("last_backup_at"),
        })
    return out


def build_security_state(
    shared_dir: Path,
    *,
    active_subtab: str | None = None,
    backup_drift_hint: list[dict[str, Any]] | None = None,
    audit_data_override: dict[str, Any] | None = None,
    firing_signals_override: list[Any] | None = None,
    cached_at_override: float | None = None,
) -> dict[str, Any]:
    """Build the server-side Security page-state pack.

    The override params let tests pass fixtures directly without
    standing up disk state. In production all three default to None
    and the real ``audit_state`` / ``signals.store`` modules are read.
    """
    # Audit cache — disk-mirrored snapshot.
    if audit_data_override is not None:
        audit_data = audit_data_override
        cached_at = cached_at_override
    else:
        try:
            from .. import audit_state  # type: ignore
            snap = audit_state.snapshot(shared_dir)
            audit_data = snap.get("data") or {}
            cached_at = (
                cached_at_override
                if cached_at_override is not None
                else snap.get("cached_at")
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("security_chat_context: audit snapshot failed: %s", exc)
            audit_data = {}
            cached_at = cached_at_override

    # Firing signals — canonical store.
    if firing_signals_override is not None:
        firing_raw: Iterable[Any] = firing_signals_override
    else:
        firing_raw = []
        try:
            from signals import store as signals_store  # type: ignore
            firing_raw = list(
                signals_store.iter_active(shared_dir, state="firing")
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning(
                "security_chat_context: signal iter failed: %s", exc,
            )
            firing_raw = []

    findings_all = _coalesce_findings(audit_data)
    signals_all = _shape_signals(firing_raw)
    per_bot_all = _shape_per_bot(audit_data)
    drift = _shape_backup_drift(backup_drift_hint)[:_MAX_DRIFT]

    findings = findings_all[:_MAX_FINDINGS]
    signals = signals_all[:_MAX_SIGNALS]
    per_bot = per_bot_all[:_MAX_PER_BOT]
    elided_findings = max(0, len(findings_all) - len(findings))
    elided_signals = max(0, len(signals_all) - len(signals))

    counts = {
        "critical": sum(int(r.get("critical") or 0) for r in per_bot_all),
        "warn": sum(int(r.get("warn") or 0) for r in per_bot_all),
        "info": sum(int(r.get("info") or 0) for r in per_bot_all),
        "drifted_bots": len(drift),
        "firing_signals": len(signals_all),
    }

    headline = _build_headline(
        active_subtab=active_subtab,
        per_bot_count=len(per_bot_all),
        counts=counts,
        cached_at=cached_at,
        elided_findings=elided_findings,
        elided_signals=elided_signals,
    )

    return {
        "headline": headline,
        "active_subtab": active_subtab,
        "cached_at": cached_at,
        "counts": counts,
        "per_bot": per_bot,
        # Coalesced across bots — answers "which bots have <X>?" without
        # a per-bot fan-out.
        "findings": findings,
        "firing_signals": signals,
        "backup_drift": drift,
        "elided_findings_count": elided_findings,
        "elided_signals_count": elided_signals,
        # Surface a tool pointer for the long tail so evo knows where to
        # go if the operator needs more than the inline summary.
        "tool_pointers": [
            {
                "tool": 'pod_state(query="audit", bot_id=...)',
                "for": (
                    "the FULL per-bot audit detail. Only call when the "
                    "operator asks about a finding NOT in the inline "
                    "`findings` list — the inline list already covers "
                    "the top advisories with affected-bots inline."
                ),
            },
            {
                "tool": 'pod_state(query="config_drift")',
                "for": (
                    "enumerating backup-baseline drift when the inline "
                    "`backup_drift` list is empty (the client only "
                    "populates it on the Backups subtab)."
                ),
            },
            {
                "tool": "action.security.accept_drift(bot_id=...)",
                "for": (
                    "committing one bot's live openclaw.json as the new "
                    "baseline; iterate when the operator says 'accept all'."
                ),
            },
        ],
    }


def _build_headline(
    *,
    active_subtab: str | None,
    per_bot_count: int,
    counts: dict[str, int],
    cached_at: float | None,
    elided_findings: int,
    elided_signals: int,
) -> str:
    """One-line situational headline so evo gets the gist before
    walking the structured fields."""
    if active_subtab == "backups" and counts.get("drifted_bots"):
        return (
            f"Backups subtab — {counts['drifted_bots']} bot(s) have "
            "config drift; each has an 'Accept as baseline' button."
        )
    if active_subtab == "backups":
        return "Backups subtab — no config drift detected."
    if not per_bot_count:
        return "Security audit data not yet loaded (or no bots audited)."
    parts = [f"{per_bot_count} bot(s) audited"]
    if counts.get("critical"):
        parts.append(f"{counts['critical']} critical")
    if counts.get("warn"):
        parts.append(f"{counts['warn']} warn")
    if counts.get("firing_signals"):
        parts.append(f"{counts['firing_signals']} firing signal(s)")
    extra = ""
    if elided_findings or elided_signals:
        extra = (
            f" (+{elided_findings} more findings, "
            f"+{elided_signals} more signals truncated)"
        )
    return ", ".join(parts) + extra
