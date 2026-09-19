"""autonomy.backfill — observe-first inference for existing pods.

Spec: internal/spec-autonomy-ladder-2026-06-10.md §5.2.

Existing pods have bots already acting on integrations. Snapping them
to kind defaults would break working workflows — forbidden. Instead:

  - infer the effective rung from current enforcement (the live
    ``tools.deny`` list vs the integration's known outward tools),
  - record it with ``set_by: backfill_inferred`` (find-or-create only;
    a deliberate posture is never overwritten),
  - where the inferred rung is WIDER than the kind default, return a
    suggestion-grade finding ("this bot can send email without asking
    — want to keep that?") for the permission monitor to emit.

Backfilled entries are observe-only: the renderer skips them (no deny
merge, no guidance injection) until the operator's first deliberate
action rewrites ``set_by`` — see ``renderer.posture_is_render_eligible``.
The finding keeps firing while the posture stays inferred-and-wide;
confirming or restricting it clears the condition and the monitor's
sweep auto-resolves the Signal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import catalog as _catalog
from . import renderer as _renderer
from . import store as _store


BACKFILL_SIGNAL_TYPE = "autonomy_backfill_review"


def infer_rung(
    integration_id: str,
    spec: _catalog.KindSpec,
    binding: _catalog.IntegrationBinding,
    server_tools: list[str],
    live_deny: set[str],
) -> str:
    """Infer the effective rung from live enforcement.

    Every outward-class tool (send/forward/delete) denied ⇒ the bot
    mechanically cannot act outward: ``draft_only``. ANY outward tool
    reachable ⇒ the bot can act — including delete, which every rung's
    operator copy promises never happens, so claiming "Drafts only"
    over a reachable delete tool would be a sign pretending to be a
    wall. With no per-tool ask gate we cannot distinguish "asks first
    by instruction" from "acts freely", and rung 3 is invalid without
    a rules block — so reachable-outward lands on ``act_with_approval``
    and the suggestion Signal asks the operator to confirm or restrict
    (spec §5.2).
    """
    outward = [
        t for t in _catalog.kind_tools(binding, server_tools)
        if _catalog.classify_tool(spec, t) in spec.outward_verbs
    ]
    reachable = [
        t for t in outward
        if _catalog.oc_deny_entry(integration_id, t) not in live_deny
    ]
    if reachable:
        return _catalog.RUNG_ACT_WITH_APPROVAL
    return _catalog.RUNG_DRAFT_ONLY


def ensure_backfilled(
    shared_dir: Path,
    bot_id: str,
    config: dict | None = None,
    *,
    home_override: Path | None = None,
    collect_findings: bool = True,
    create_missing: bool = True,
) -> list[dict[str, Any]]:
    """Create missing posture entries for the bot's ladder-eligible
    integrations; return suggestion-grade findings for every entry that
    is still ``backfill_inferred`` and wider than the kind default.

    Idempotent: existing entries (deliberate or inferred) are never
    rewritten. Safe to call from the monitor sweep, the deploy path,
    and the inventory API (which passes ``collect_findings=False`` —
    it only needs the entries to exist). ``create_missing=False`` makes
    the call read-only (the monitor's dry-run path: previewing findings
    must not lock in an inferred rung from a possibly-transient state).

    An unreadable openclaw.json skips the creation pass (the monitor's
    missing-config finding covers that) but still reports the pending
    reviews — those depend only on the posture file, and dropping them
    would let the monitor's sweep auto-resolve a still-true Signal.
    """
    if not create_missing:
        return pending_review_findings(shared_dir, bot_id) if collect_findings else []
    cfg = _renderer.read_live_openclaw(bot_id, config, home_override=home_override)
    if cfg is None:
        return pending_review_findings(shared_dir, bot_id) if collect_findings else []
    deny_raw = (cfg.get("tools") or {}).get("deny")
    live_deny = {e for e in deny_raw if isinstance(e, str)} if isinstance(deny_raw, list) else set()

    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError:
        # Malformed posture file — the coherence check owns surfacing
        # that; backfill must not write next to garbage.
        return []
    existing = set(doc.integrations.keys()) if doc else set()

    # Both discovery sources: mcp.servers entries AND plugin-provided
    # tool surfaces (e.g. plugin Gmail — gmail_* in tools.alsoAllow with
    # no mcp.servers entry). catalog.discover_ladder_integrations supplies
    # the per-bot tool surface for each.
    for integration_id, binding, tools in _catalog.discover_ladder_integrations(cfg):
        spec = _catalog.kind_spec(binding.kind)
        if spec is None:
            continue
        if not _catalog.is_ladder_eligible(spec, binding, tools):
            continue
        if integration_id in existing:
            continue
        rung = infer_rung(integration_id, spec, binding, tools, live_deny)
        _store.ensure_entry(
            shared_dir, bot_id, integration_id,
            kind=spec.kind, rung=rung,
            actor=_store.ACTOR_BACKFILL,
            note="inferred from live enforcement at backfill (spec §5.2)",
        )

    return pending_review_findings(shared_dir, bot_id) if collect_findings else []


def pending_review_findings(shared_dir: Path, bot_id: str) -> list[dict[str, Any]]:
    """Findings for entries still inferred-and-wider-than-default.

    The condition persists until the operator confirms or restricts
    (either action rewrites ``set_by`` and the finding clears).
    """
    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError:
        return []
    if doc is None:
        return []
    findings: list[dict[str, Any]] = []
    for iid, posture in sorted(doc.integrations.items()):
        if (posture.set_by or {}).get("actor") != _store.ACTOR_BACKFILL:
            continue
        spec = _catalog.kind_spec(posture.kind)
        if spec is None:
            continue
        if not _catalog.is_promotion(spec.default_rung, posture.rung):
            continue
        binding = _catalog.binding_for(iid)
        display = binding.display_name if binding else iid
        rung_label = _catalog.RUNG_LABELS.get(posture.rung, posture.rung)
        default_label = _catalog.RUNG_LABELS.get(spec.default_rung, spec.default_rung)
        findings.append({
            "type": BACKFILL_SIGNAL_TYPE,
            "severity": "info",
            "signature_scope": f"{bot_id}:{iid}",
            "title": (
                f"{bot_id}: {spec.operator_noun} ({display}) can act "
                "without a recorded decision"
            ),
            "body": (
                f"{bot_id} can currently use {spec.operator_noun} on "
                f"{display} at \"{rung_label}\" — wider than the shipped "
                f"default \"{default_label}\". This was inferred from the "
                "live configuration, not set by anyone. If that's "
                "intentional, confirm it on Security → Permissions → "
                "Autonomy (one click); if not, restrict it there. "
                "Nothing was changed — the bot keeps working as-is "
                "either way."
            ),
            "details": {
                "bot_id": bot_id,
                "integration_id": iid,
                "integration_label": f"{spec.operator_noun} ({display})",
                "kind": posture.kind,
                "inferred_rung": posture.rung,
                "rung_label": rung_label,
                "default_rung": spec.default_rung,
                "set_at": posture.set_at,
            },
        })
    return findings


__all__ = [
    "BACKFILL_SIGNAL_TYPE",
    "ensure_backfilled",
    "infer_rung",
    "pending_review_findings",
]
