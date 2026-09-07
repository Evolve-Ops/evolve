"""autonomy.coherence — posture↔enforcement drift findings.

Spec: internal/spec-autonomy-ladder-2026-06-10.md §4.2.1 / §3.4.

Re-derives what the renderer should have produced for every
render-eligible posture and diffs it against the live openclaw.json
deny list. A mismatch (manual config edit, the integration's known
tool surface changed, a deploy raced the render) is an
``autonomy_posture_drift`` finding; the permission monitor emits it
as a Signal and sweep-resolves it when the condition clears.

Backfill-inferred postures are out of scope here by design — they are
deliberately unrendered (observe-first, §5.2), and their gap is
surfaced by the suggestion-grade ``autonomy_backfill_review`` Signal
instead of drift noise.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import catalog as _catalog
from . import renderer as _renderer
from . import store as _store


DRIFT_SIGNAL_TYPE = "autonomy_posture_drift"


def check_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict | None = None,
    *,
    home_override: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return drift findings for one bot (empty list = coherent).

    Unreadable openclaw.json yields no findings here — the permission
    monitor's missing-config signal already owns that condition. A
    malformed posture *file* IS a finding: someone edited intent by
    hand and the renderer can no longer trust it.

    ``now`` pins the daily-cap pause lookup to the evaluation instant so
    the audit reads the same pause set the render did. A monitor pass
    that evaluates limits and then checks coherence with a fixed ``now``
    must use that instant in both, or a same-day pause would read as
    drift the moment the wall clock crosses the UTC day boundary between
    the two sub-passes.
    """
    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError as exc:
        return [{
            "type": DRIFT_SIGNAL_TYPE,
            "severity": "warn",
            "signature_scope": f"{bot_id}:posture_file",
            "title": f"{bot_id}: autonomy settings file is unreadable",
            "body": (
                f"The stored autonomy settings for {bot_id} no longer "
                "parse, so what the operator decided can't be enforced "
                "or displayed. Restore the file from a backup or re-set "
                "the affected integrations on Security → Permissions → "
                "Autonomy."
            ),
            "details": {"bot_id": bot_id, "error": str(exc)},
        }]
    if doc is None or not doc.integrations:
        return []

    # Same pause set the renderer uses — a daily-cap pause widens the
    # expected deny slice, and the audit must agree with the render or
    # every pause would read as drift.
    expected_by_iid, _skipped = _renderer.expected_deny_by_integration(
        doc, paused=_renderer.paused_for_bot(shared_dir, bot_id, now=now),
    )
    if not expected_by_iid:
        return []

    cfg = _renderer.read_live_openclaw(bot_id, config, home_override=home_override)
    if cfg is None:
        return []
    deny_raw = (cfg.get("tools") or {}).get("deny")
    live_deny = [e for e in deny_raw if isinstance(e, str)] if isinstance(deny_raw, list) else []

    findings: list[dict[str, Any]] = []
    for iid, expected in sorted(expected_by_iid.items()):
        # Source-aware ownership: mcp__<id>__ prefix for mcp_server
        # integrations, bare tool names for plugin integrations.
        live_owned = [e for e in live_deny if _catalog.deny_entry_is_owned(iid, e)]
        missing = sorted(set(expected) - set(live_owned))
        unexpected = sorted(set(live_owned) - set(expected))
        if not missing and not unexpected:
            continue
        posture = doc.integrations[iid]
        binding = _catalog.binding_for(iid)
        spec = _catalog.kind_spec(posture.kind)
        display = binding.display_name if binding else iid
        noun = spec.operator_noun if spec else posture.kind
        rung_label = _catalog.RUNG_LABELS.get(posture.rung, posture.rung)
        findings.append({
            "type": DRIFT_SIGNAL_TYPE,
            "severity": "warn",
            "signature_scope": f"{bot_id}:{iid}",
            "title": (
                f"{bot_id}: {noun} ({display}) enforcement out of sync "
                f"with \"{rung_label}\""
            ),
            "body": (
                f"What {bot_id} is actually allowed to do with {noun} on "
                f"{display} no longer matches the recorded setting "
                f"\"{rung_label}\". Either the bot's config was edited "
                "out-of-band or a deploy raced the render. Re-applying "
                "the setting from Security → Permissions → Autonomy "
                "(re-select the current level) repairs it; the next "
                "deploy would too."
            ),
            "details": {
                "bot_id": bot_id,
                "integration_id": iid,
                "integration_label": f"{noun} ({display})",
                "kind": posture.kind,
                "rung": posture.rung,
                "rung_label": rung_label,
                "missing_deny_entries": missing,
                "unexpected_deny_entries": unexpected,
                "expected_deny_entries": expected,
            },
        })
    return findings


__all__ = ["DRIFT_SIGNAL_TYPE", "check_bot"]
