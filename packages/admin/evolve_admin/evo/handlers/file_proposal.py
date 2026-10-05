"""`evo file-proposal` handler — Channel B bot-to-operator routing.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

When a non-evo bot's repair conversation produces "user said yes, file
this for review", the bot routes through the cross-bot ``evo`` keyword
mechanism. The handler writes a Proposal to the arbiter store; the
operator approves it in the Self-Improvement queue (or via
``evo app-changes``) before any manifest mutation happens.

Mirrors ``app_changes._handle_flag`` (the ``evo app-changes <app> flag
<description>`` path from PR #2332) but carries STRUCTURED repair
content rather than free-form user text. The structured content is
the same proposal shape Channel A's admin-UI chat emits — so the
admin-side apply infrastructure can be shared across channels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role
from ._shared import speak
from ...applications.file_proposal_grammar import (
    KIND_FILE_PROPOSAL,
    parse_file_proposal,
)


_SUBCOMMAND = "file-proposal"


def render(*, role: Role, bot_id: str, args: str,
           network: dict[str, Any]) -> DispatchResult:
    """Top-level dispatcher for ``evo file-proposal …``.

    Auth model: any role can FILE a proposal — that's the whole point
    of routing through the operator's approval queue. The audience
    field on the Proposal is fixed to ``pod_operator`` so secondaries
    can't smuggle bot_primary_user-audience proposals through this
    channel.
    """
    cmd = parse_file_proposal(args)
    if cmd.is_error():
        return speak(_SUBCOMMAND, cmd.usage_error, role)

    if cmd.kind != KIND_FILE_PROPOSAL:
        # Defensive — grammar only emits FILE_PROPOSAL or USAGE_ERROR.
        return speak(_SUBCOMMAND,
                      f"Unhandled command kind: {cmd.kind}", role)

    shared_dir = _shared_dir_for(network)
    proposal_id = _write_file_proposal(
        on_behalf_of=cmd.on_behalf_of,
        app_id=cmd.app_id,
        action=cmd.action,
        content=cmd.content,
        shared_dir=shared_dir,
        sender_bot_id=bot_id,
    )
    return speak(_SUBCOMMAND, _render_result(
        on_behalf_of=cmd.on_behalf_of, app_id=cmd.app_id,
        action=cmd.action, proposal_id=proposal_id,
    ), role)


def _write_file_proposal(*, on_behalf_of: str, app_id: str,
                         action: str, content: dict,
                         shared_dir: Path,
                         sender_bot_id: str) -> str:
    """Construct + persist the Proposal. Returns the proposal id, or ""
    on write failure (caller surfaces a polite acknowledgement either
    way — losing the write to a transient I/O issue shouldn't read as
    success in the chat).

    The Proposal's ``bot_id`` is ``on_behalf_of`` — the bot the repair
    conversation is about — NOT ``sender_bot_id``, which is whichever
    bot's gateway hosted the cross-bot ``evo`` call (often
    ``evolve``/the primary). The provenance signals capture both so
    the operator can see who routed it.
    """
    # Local import — schema / arbiter aren't on the evo gateway's hot
    # import path; keeping them local avoids paying the import cost on
    # every non-file-proposal command (same pattern as _handle_flag).
    from schema.proposal import (
        Investigation, Proposal, RiskTag, new_proposal_id,
    )
    from schema.provenance import Provenance
    from arbiter import store as arbiter_store

    proposal_id = new_proposal_id()
    summary = f"{on_behalf_of}/{app_id}: bot-routed repair proposal ({action})"
    context_lines = [
        f"## Bot-routed repair proposal — `{app_id}` on `{on_behalf_of}`",
        "",
        f"Action: `{action}`",
        "",
        "Content:",
        "```json",
        _format_content_for_context(content),
        "```",
        "",
        "_Filed via `evo file-proposal` from a bot-side repair "
        "conversation (Channel B). The bot and its primary user agreed "
        "on this change; the operator approves before the manifest "
        "mutation lands._",
    ]
    context = "\n".join(context_lines)

    try:
        proposal = Proposal(
            id=proposal_id,
            bot_id=on_behalf_of,
            generator_id="evo_file_proposal",
            dimension="app_quality",
            trigger_observations=[
                f"evo_file_proposal:{on_behalf_of}/{app_id}:{action}",
            ],
            provenance=Provenance(
                technique="evo_file_proposal.v1",
                signals={
                    "app_id": app_id,
                    "on_behalf_of": on_behalf_of,
                    "sender_bot_id": sender_bot_id,
                    "action": action,
                    "source": "evo_file_proposal",
                },
                confidence=1.0,
            ),
            problem=summary,
            action=Investigation(context=context),
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="manual",
                touches=["app_manifest"],
            ),
            claim=None,
            approval_audience="pod_operator",
            urgency="improvement",
            admin_surface_summary=summary[:120],
            status="pending",
        )
        arbiter_store.write_proposal(proposal, shared_dir)
    except Exception:
        return ""
    return proposal_id


def _format_content_for_context(content: dict) -> str:
    """Render the action's content payload as pretty JSON for the
    operator-facing context. Falls back to ``repr`` on serialization
    failure so the proposal still surfaces SOMETHING actionable."""
    import json as _json
    try:
        return _json.dumps(content, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return repr(content)


def _render_result(*, on_behalf_of: str, app_id: str,
                    action: str, proposal_id: str) -> str:
    base = (
        f"Filed `{action}` proposal for `{app_id}` on `{on_behalf_of}` — "
        f"the operator will review."
    )
    if proposal_id:
        base += f" (Proposal {proposal_id})"
    else:
        base += (
            " (Proposal write failed — operator can re-run via the "
            "admin UI if this doesn't surface.)"
        )
    return base


def _shared_dir_for(network: dict[str, Any]) -> Path:
    """Resolve shared_dir from network config. Defaults to the
    canonical pod path when network doesn't carry an override."""
    shared = (network.get("pod") or {}).get("shared_dir")
    if shared:
        return Path(shared)
    return Path("/Users/Shared/evolve")
