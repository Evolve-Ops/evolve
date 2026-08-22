"""Action tool — set a bot's per-integration autonomy level from chat.

Spec: docs/spec-autonomy-ladder-2026-06-10.md §3.1 (the second front
door): "let my assistant send email after asking me" works in evo chat.
Same API, same validation, same history record as the Permissions-tab
UI — the tool calls the admin daemon's existing
``POST /api/autonomy/<bot>/<integration>`` route with
``actor=primary_bot`` so the audit timeline shows the chat path.

Confirm-before-call mirrors ``action.proposal.apply``:

  - PROMOTIONS (widening — including any move whose direction can't be
    proven) return ``requires_confirmation: True`` from validate, so
    the proxy stages a confirmation button regardless of authority
    tier. The confirmation context carries the same operator-language
    consequence copy the UI dialog uses (spec §3.1).
  - DEMOTIONS follow plain authority-tier semantics — the way back
    down must always be cheaper than the way up.

Daemon-first plumbing (Phase E.3.4 pattern): post-separation, evo's
gateway user cannot write the posture file or run the render's sudo
path; the admin daemon route does both. The in-process fallback exists
for the migration window only, same as action.proposal.apply.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _catalog():
    try:
        from autonomy import catalog as acatalog
        return acatalog
    except ImportError as exc:
        log.warning("action.autonomy.set: autonomy package unavailable: %s", exc)
        return None


def _current_posture_row(
    shared_dir: Path, bot_id: str, integration_id: str,
) -> "tuple[dict[str, Any] | None, str | None]":
    """The bot's current posture row, daemon-first.

    Returns ``(row, error)``. ``row`` is the inventory row dict (or a
    minimal {rung} dict from the in-process fallback); ``error`` is a
    human-readable failure when neither path could answer.
    """
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "GET", "/api/autonomy/inventory", timeout=15.0,
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        rows = ((body.get("bots") or {}).get(bot_id) or {}).get("integrations") or []
        for row in rows:
            if isinstance(row, dict) and row.get("integration_id") == integration_id:
                return row, None
        return None, (
            f"'{integration_id}' is not a ladder-managed integration on "
            f"bot '{bot_id}' (no autonomy row)"
        )
    # Fallback: direct read (pre-separation runtimes).
    try:
        from autonomy import store as astore
        doc = astore.load(shared_dir, bot_id)
    except ImportError as exc:
        return None, f"autonomy store unavailable: {exc}"
    except ValueError as exc:
        return None, f"stored autonomy settings are unreadable: {exc}"
    except OSError as exc:
        return None, f"cannot read autonomy settings: {exc}"
    posture = doc.integrations.get(integration_id) if doc else None
    if posture is None:
        return None, (
            f"'{integration_id}' has no autonomy entry on bot '{bot_id}'"
        )
    return {"rung": posture.rung, "rules": dict(posture.rules)}, None


# ─── handler ─────────────────────────────────────────────────────────────────


def _set_handler(
    shared_dir: Path,
    bot_id: str,
    integration_id: str,
    rung: str,
    rules: "dict[str, Any] | None" = None,
    expected_current_rung: "str | None" = None,
    reason: "str | None" = None,
) -> dict[str, Any]:
    """Set the level via the admin daemon route (actor=primary_bot)."""
    if not bot_id or not integration_id or not rung:
        return {"ok": False, "error": "bot_id, integration_id, and rung are required"}

    if expected_current_rung is None:
        # REQUIRED, never auto-resolved here: resolving it at execution
        # time would only guard this handler's own read→write window,
        # not the staged-confirmation→execute window the CAS exists for
        # (the operator confirmed a SPECIFIC transition; a posture that
        # moved since must fail loudly, not silently apply a different
        # one). validate() reports the current rung key so the caller
        # can pass it.
        return {
            "ok": False,
            "error": (
                "expected_current_rung is required — call validate "
                "first; its context.current_rung is the value to pass"
            ),
        }

    payload: dict[str, Any] = {
        "rung": rung,
        "rules": dict(rules or {}),
        "expected_current_rung": expected_current_rung,
        "actor": "primary_bot",
        "note": reason or "set via evo chat",
    }

    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", f"/api/autonomy/{bot_id}/{integration_id}",
        body=payload,
        timeout=30.0,  # render + gateway kickstart can take a moment
    )
    if used_daemon:
        out = dict(body) if isinstance(body, dict) else {}
        out.setdefault("ok", status == 200)
        out.setdefault("bot_id", bot_id)
        out.setdefault("integration_id", integration_id)
        out["via"] = "admin_daemon"
        if status == 409:
            out.setdefault(
                "error",
                "the level changed underneath you — re-check and retry",
            )
        elif status not in (200, None) and not out.get("ok"):
            out.setdefault("error", f"admin daemon returned status {status}")
        return out

    # Fallback: in-process write (migration window only).
    try:
        from autonomy import renderer as arenderer
        from autonomy import store as astore
    except ImportError as exc:
        return {"ok": False, "error": f"autonomy modules unavailable: {exc}"}
    try:
        posture = astore.set_posture(
            shared_dir, bot_id, integration_id,
            rung=rung, rules=dict(rules or {}),
            actor=astore.ACTOR_PRIMARY_BOT,
            note=reason or "set via evo chat",
            expected_current_rung=expected_current_rung,
        )
    except astore.StalePostureError as exc:
        return {"ok": False, "error": f"stale: {exc}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"cannot write autonomy settings: {exc}"}
    render = arenderer.render_bot(bot_id, shared_dir)
    return {
        "ok": True,
        "bot_id": bot_id,
        "integration_id": integration_id,
        "rung": posture.rung,
        "render": {
            "changed": render.changed,
            "written": render.written,
            "error": render.write_error,
        },
        "via": "in_process_fallback",
    }


# ─── validate ────────────────────────────────────────────────────────────────


def _set_validate(
    shared_dir: Path,
    bot_id: str,
    integration_id: str,
    rung: str,
    rules: "dict[str, Any] | None" = None,
    expected_current_rung: "str | None" = None,
    reason: "str | None" = None,
) -> dict[str, Any]:
    """Dry-run gate + the §3.1 confirm-before-call tier override.

    ``requires_confirmation=True`` for every promotion (and for any
    move whose direction can't be proven — fail closed). The context
    carries the rung labels + consequence copy so the staged button
    reads exactly like the UI's confirmation dialog.
    """
    if not bot_id or not integration_id or not rung:
        return {"ok": False, "reason": "bot_id, integration_id, and rung are required"}

    acatalog = _catalog()
    if acatalog is None:
        return {"ok": False, "reason": "autonomy package unavailable in this runtime"}
    if rung not in acatalog.RUNGS:
        return {
            "ok": False,
            "reason": f"unknown level {rung!r} — one of {list(acatalog.RUNGS)}",
        }
    if rules is not None and not isinstance(rules, dict):
        return {"ok": False, "reason": "rules must be an object"}

    row, err = _current_posture_row(shared_dir, bot_id, integration_id)
    if row is None:
        return {"ok": False, "reason": err or "current level unavailable"}
    current_rung = str(row.get("rung") or "")
    if expected_current_rung is not None and expected_current_rung != current_rung:
        return {
            "ok": False,
            "reason": (
                f"expected current level {expected_current_rung!r} but it is "
                f"{current_rung!r} — re-check before retrying"
            ),
        }
    if rung == current_rung:
        return {
            "ok": False,
            "reason": f"already at {acatalog.RUNG_LABELS.get(rung, rung)!r}",
        }

    binding = acatalog.binding_for(integration_id)
    spec = acatalog.kind_spec(binding.kind) if binding else None
    if spec is not None:
        errors = acatalog.validate_rules(spec, rung, rules or {})
        if errors:
            return {"ok": False, "reason": "; ".join(errors)}

    promotion = acatalog.is_promotion(current_rung, rung)
    context: dict[str, Any] = {
        "bot_id": bot_id,
        "integration_id": integration_id,
        # The CAS witness the handler requires — pass this back as
        # expected_current_rung so the confirmed transition is exactly
        # the one that applies.
        "current_rung": current_rung,
        "current_level": acatalog.RUNG_LABELS.get(current_rung, current_rung),
        "target_level": acatalog.RUNG_LABELS.get(rung, rung),
        "direction": "more" if promotion else "less",
    }
    if spec is not None and promotion:
        context["consequence"] = spec.promotion_consequences.get(rung, "")
    return {
        "ok": True,
        "context": context,
        # Promotions always stage a confirmation, regardless of the
        # operator's authority tier — the same permanent carve-out as
        # every other auto-approve lane (spec §3.2). Demotions follow
        # plain tier semantics: the way down stays cheap.
        "requires_confirmation": promotion,
    }


# ─── Tool registration ───────────────────────────────────────────────────────


SET_TOOL = Tool(
    name="action.autonomy.set",
    description=(
        "Set how much a bot may do on one integration without a person "
        "— the same control as Security → Permissions → Autonomy, "
        "driven from chat. Levels (rung keys): 'draft_only' (Drafts "
        "only — reads and prepares, a person acts), 'act_with_approval' "
        "(Asks first — acts only after an explicit OK per action), "
        "'autonomous_within_rules' (Acts within limits — requires a "
        "rules object, e.g. {\"actions_per_day\": 10}).\n"
        "\n"
        "Allowing MORE always stages a confirmation with the operator "
        "first (validate.requires_confirmation=True regardless of "
        "authority tier — widening autonomy is permanently a human "
        "decision). Stepping DOWN follows normal tier semantics: the "
        "way back is always cheap. Takes effect within seconds (config "
        "re-render + gateway restart)."
    ),
    wire_description=(
        "Set how much a bot may do on one integration without a "
        "person — same control as Security → Permissions → Autonomy. "
        "Rungs: 'draft_only', 'act_with_approval', "
        "'autonomous_within_rules' (requires a rules object). "
        "Allowing MORE always stages an operator confirmation "
        "regardless of authority tier; stepping down follows normal "
        "tier semantics. Takes effect within seconds (gateway "
        "restart)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json (see pod_state.bots).",
            },
            "integration_id": {
                "type": "string",
                "description": (
                    "The bot's mcp.servers key, e.g. "
                    "'google_workspace'. Must already have an autonomy "
                    "row (Permissions page / /api/autonomy/inventory)."
                ),
            },
            "rung": {
                "type": "string",
                "enum": [
                    "draft_only",
                    "act_with_approval",
                    "autonomous_within_rules",
                ],
                "description": "Target level (see tool description).",
            },
            "rules": {
                "type": "object",
                "description": (
                    "Required (non-empty) when rung is "
                    "'autonomous_within_rules'; must be empty otherwise. "
                    "Keys: reach_allow, scope_allow, actions_per_day, "
                    "never."
                ),
            },
            "expected_current_rung": {
                "type": "string",
                "description": (
                    "REQUIRED to execute: the current level key, from "
                    "validate's context.current_rung. Pins the "
                    "confirmed transition — fails loudly if the level "
                    "changed in between."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line reason recorded in the integration's "
                    "history timeline."
                ),
            },
        },
        "required": ["bot_id", "integration_id", "rung"],
        "additionalProperties": False,
    },
    handler=_set_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_set_validate,
    tags=("action", "autonomy"),
    # Operator-only: widening a bot's reach is a pod-security decision.
    authorization_scope="admin",
)

register(SET_TOOL)
