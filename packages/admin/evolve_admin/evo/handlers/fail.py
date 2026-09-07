"""``evo fail ...`` — user-initiated failure-investigation handler.

Workstream C of the audit-extensions sprint. Spec:
internal/spec-audit-extensions-2026-05-17.md §5.

Grammar (spec §5.1):
  evo fail <description>   start an investigation. Description is the
                            rest of the line; rest-of-line capture must
                            preserve case + punctuation verbatim.
  evo fail recent          re-investigate the most-recent failure
  evo fail status          report state of any in-flight investigation
  evo fail flag            escalate prior no-diagnosis to operator

Auth model (spec §5): primary user of this bot OR pod admin. Other users
on team bots see a "not for you" reply.

The handler is intentionally small — all heavy lifting lives in
``audit_dispatch.request_investigation`` / the bot-side runner / the
admin-side poller. The handler's job is grammar + auth + the immediate
"Looking into it" reply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import speak


# Soft-cap on investigations per user per hour (spec §8 Q3). Hit it and
# the 4th attempt gets the "I'm still working on your previous reports"
# reply rather than queuing a new one.
_RATE_LIMIT_PER_HOUR = 3
_RATE_WINDOW_SECONDS = 60 * 60


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    """Top-level dispatcher for ``evo fail ...``.

    args is the rest of the command after "fail " — typically the user's
    failure description, but for the management forms it's a keyword
    (recent / status / flag).
    """
    tokens = (args or "").strip().split(maxsplit=1)
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id

    if not tokens:
        body = (
            "**evo fail** — report a failure\n\n"
            "Usage: `evo fail <description>` — describe what went wrong "
            "and I'll investigate.\n\n"
            "Other forms:\n"
            "  • `evo fail recent` — re-investigate the most-recent report\n"
            "  • `evo fail status` — report progress of any in-flight\n"
            "  • `evo fail flag` — escalate to the operator after a "
            "no-diagnosis reply"
        )
        return speak("fail", body, role)

    first = tokens[0].lower()

    # Management forms — these consume the whole first token, no rest-of-line.
    if first == "status":
        body = _render_status(bot_id, bot_user, network)
        return speak("fail", body, role)
    if first == "recent":
        body = _handle_recent(bot_id, bot_user, network)
        return speak("fail", body, role)
    if first == "flag":
        body = _handle_flag(bot_id, bot_user, network)
        return speak("fail", body, role)

    # Default form: `evo fail <description>` — the whole args string is the
    # description (the user typed natural language; we don't split it
    # into sub-tokens beyond detecting the management keywords above).
    description = (args or "").strip()
    body = _handle_describe(
        bot_id=bot_id,
        bot_user=bot_user,
        description=description,
        network=network,
    )
    return speak("fail", body, role)


# ── Resolve the requesting-user key ─────────────────────────────────────────


def _resolve_requesting_user(bot_id: str, network: dict[str, Any]) -> str:
    """Best-effort user key for trail attribution + notification routing.

    The dispatcher doesn't thread the channel + sender_external_id into
    our handler signature (yet — that's a separate refactor). We default
    to the bot's recorded primary user when present, so the notification
    queue is keyed consistently with the rest of the evo surface.
    """
    try:
        from ..identity import primary_pod_user, primary_external_ids
        pod_user = primary_pod_user(network, bot_id)
        if pod_user:
            return f"pod:{pod_user}"
        # primary_external_ids returns {channel: [id, ...]} (M1-B2); the
        # recipient key carries ONE bare id — interpolating the list would
        # emit "ext:telegram:['111']" and never match the admin check in
        # applications.audit_poller.
        ext = primary_external_ids(network, bot_id)
        for channel, eids in ext.items():
            if eids:
                return f"ext:{channel}:{eids[0]}"
    except Exception:
        pass
    return f"anon:{bot_id}"


# ── evo fail <description> ──────────────────────────────────────────────────


def _handle_describe(
    *,
    bot_id: str,
    bot_user: str,
    description: str,
    network: dict[str, Any],
) -> str:
    """Queue an investigation and return the immediate-reply body."""
    description = (description or "").strip()
    if not description:
        return (
            "Tell me what went wrong, e.g. `evo fail morning briefing "
            "didn't arrive today`."
        )

    requesting_user = _resolve_requesting_user(bot_id, network)

    # Rate-limit check before we hit the inbox.
    try:
        from ...applications.audit_dispatch import count_investigations_in_window
        prior_count = count_investigations_in_window(
            bot_user, requesting_user, window_seconds=_RATE_WINDOW_SECONDS,
        )
    except Exception:
        prior_count = 0
    if prior_count >= _RATE_LIMIT_PER_HOUR:
        return (
            "I'm still working on your previous reports — let me finish "
            f"those first. (You've reported {prior_count} failures in the "
            "last hour; the cap is "
            f"{_RATE_LIMIT_PER_HOUR}.)"
        )

    try:
        from ...applications.audit_dispatch import request_investigation
        result = request_investigation(
            bot_id=bot_id, bot_user=bot_user,
            user_description=description,
            requesting_user=requesting_user,
            kick=True,
        )
    except Exception as exc:
        return f"Couldn't start the investigation: {exc}"

    if not result.ok:
        return f"Couldn't start the investigation: {result.error or 'unknown error'}"

    return (
        "Looking into it — I'll let you know what I find.\n\n"
        f"_Investigation id: `{result.investigation_id}`_"
    )


# ── evo fail recent ─────────────────────────────────────────────────────────


def _handle_recent(
    bot_id: str, bot_user: str, network: dict[str, Any],
) -> str:
    """Re-investigate the user's most recent failure.

    Uses the same description as the last trail entry. Lets the user
    re-trigger an investigation after a no-diagnosis result without
    retyping their complaint.
    """
    requesting_user = _resolve_requesting_user(bot_id, network)
    try:
        from ...applications.audit_dispatch import latest_investigation_for_user
        prior = latest_investigation_for_user(bot_user, requesting_user)
    except Exception as exc:
        return f"Couldn't read the investigation trail: {exc}"

    if prior is None:
        return (
            "I don't have a previous investigation to re-run for you. "
            "Type `evo fail <description>` to start a fresh one."
        )

    description = (prior.get("user_description") or "").strip()
    if not description:
        return (
            "Your previous investigation didn't record a description, "
            "so there's nothing to re-run. Try `evo fail <description>`."
        )

    return _handle_describe(
        bot_id=bot_id, bot_user=bot_user,
        description=description, network=network,
    )


# ── evo fail status ─────────────────────────────────────────────────────────


def _render_status(
    bot_id: str, bot_user: str, network: dict[str, Any],
) -> str:
    """Report the state of the user's most-recent investigation."""
    requesting_user = _resolve_requesting_user(bot_id, network)
    try:
        from ...applications.audit_dispatch import latest_investigation_for_user
        prior = latest_investigation_for_user(bot_user, requesting_user)
    except Exception as exc:
        return f"Couldn't read the investigation trail: {exc}"

    if prior is None:
        return (
            "No recent investigations on file for you. "
            "Type `evo fail <description>` to start one."
        )

    investigation_id = prior.get("investigation_id") or "(unknown)"
    status = prior.get("status") or "unknown"
    description = (prior.get("user_description") or "")[:120]
    diagnosis = (prior.get("diagnosis") or "").strip()
    confidence = prior.get("confidence") or "low"
    ts = prior.get("ts") or "(?)"

    lines = [
        "**Investigation status**",
        "",
        f"• id: `{investigation_id}`",
        f"• reported: `{description}`",
        f"• completed: `{ts}`",
        f"• status: `{status}` (confidence: `{confidence}`)",
    ]
    if diagnosis:
        lines += ["", "Diagnosis:", diagnosis]
    else:
        lines += [
            "",
            "No diagnosis recorded. "
            "Reply `evo fail flag` to escalate to the operator.",
        ]
    return "\n".join(lines)


# ── evo fail flag ───────────────────────────────────────────────────────────


def _handle_flag(
    bot_id: str, bot_user: str, network: dict[str, Any],
) -> str:
    """Escalate the user's most-recent investigation to a Proposal."""
    requesting_user = _resolve_requesting_user(bot_id, network)
    try:
        from ...applications.audit_dispatch import flag_investigation_unresolved
        ok, err, investigation_id = flag_investigation_unresolved(
            bot_id=bot_id, bot_user=bot_user,
            requesting_user=requesting_user,
        )
    except Exception as exc:
        return f"Couldn't flag the investigation: {exc}"

    if not ok:
        if "no recent investigation" in (err or "").lower():
            return (
                "I don't have a recent investigation to flag. "
                "Type `evo fail <description>` to start one — I'll flag "
                "it only after I've tried to diagnose it first."
            )
        return f"Couldn't flag the investigation: {err}"

    return (
        f"Flagged for the operator. They'll see your report — "
        f"investigation id `{investigation_id}` — in their queue and "
        "investigate manually. I'll let you know when there's an update."
    )
