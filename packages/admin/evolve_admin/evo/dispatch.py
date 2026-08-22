"""Dispatch an ``evo`` command to its handler.

The plugin posts ``{ bot_id, channel, sender_external_id, raw_text }`` to
``/api/evo/dispatch``; the route forwards to :func:`dispatch` here.

Returned envelope (``DispatchResult.to_dict``):

.. code-block:: python

    {
      "subcommand": "help",       # canonical name after parsing
      "role": "primary",          # resolved role of the caller
      "mode": "speak",            # "speak" | "send_direct"
      "system_append": "...",     # for mode=speak; LLM echoes verbatim
      "direct_message": null,     # for mode=send_direct (unused in v1)
      "wizard_session_id": null   # placeholder for the wizard turn loop
    }
"""

from __future__ import annotations

import importlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import audit as _audit
from . import identity as _identity
from .. import external_ids as _ext_ids
from .identity import Role, derive_user_key, resolve_role
from . import _delimiters as _evo_delim
from . import subcommands as _sc


Mode = Literal["speak", "send_direct"]


@dataclass
class DispatchResult:
    subcommand: str
    role: Role
    mode: Mode
    system_append: str | None = None
    direct_message: str | None = None
    wizard_session_id: str | None = None
    # Bare user-facing body (no "respond verbatim" preamble) for the
    # plugin to deliver directly via the channel API on surfaces where
    # that's reliable (Telegram). When set, the plugin sends this string
    # to the user and tells the LLM to stay silent rather than relying
    # on it to echo ``system_append`` verbatim. ``None`` for wizard /
    # conversational paths where the LLM is supposed to engage.
    #
    # Auto-wrapped with visual delimiters in ``__post_init__`` (idempotent)
    # so the user can distinguish plugin-direct-sent content from any
    # LLM hallucinations that arrive alongside. See ``evo._delimiters``.
    direct_send_message: str | None = None
    # Optional override for the title rendered inside the open
    # delimiter (default: ``evo`` → ``═══ evo ═══``). Handlers that
    # would otherwise prefix their body with a bolded ``**evo X**``
    # title pass the title here instead, so the wrap renders as
    # ``═══ evo X ═══`` and the body drops the now-redundant heading
    # row. Ignored when ``direct_send_message`` is None or already
    # wrapped (idempotency guard in ``__post_init__``).
    direct_send_title: str | None = None
    # Short human-readable description of what this subcommand does
    # (the registry's ``short_help``). Threaded through to the plugin
    # so its ``before_prompt_build`` directive can brief the LLM with
    # the actual command name + meaning instead of generic "the user
    # typed an evo keyword". Eliminates speculation work.
    subcommand_brief: str | None = None
    # Diagnostic: why the dispatcher chose this path. Surfaced in logs only.
    notes: list[str] = field(default_factory=list)
    # When True, the route handler must persist the mutated ``network`` dict
    # (e.g. via ``save_network``) before returning. Set by handlers that
    # write into network.json — currently only ``claim``.
    network_dirty: bool = False
    # Session-scoped tier override the plugin must apply to ModelRouter's
    # in-memory state for the current session (audit #69 Phase B —
    # ``evo tier {auto|fast|standard|power|max}``). Shape:
    #
    #   {"choice": "auto"|"fast"|"standard"|"power"|"max",
    #    "consent_source": "evo_keyword"}
    #
    # When present, the plugin calls ``modelRouter.setUserTier(sessionKey,
    # choice, consent_source)`` after delivering the acknowledgment text.
    # Takes effect from the NEXT user turn (this turn just acknowledges
    # the change; routing on this turn already ran). The session-scope
    # only lives in plugin memory — a gateway restart clears it, the
    # operator's persistent ``evo tier-default`` writes to
    # userTierOverride.defaultTier instead (which the plugin reads from
    # evolve-tiers.json on every turn, no in-memory state to lose).
    #
    # Slots ABOVE the operator default (precedence level 2 vs 4a) so a
    # session-scoped ``evo tier power`` beats whatever the operator set
    # on the AI Optimization page. Cleared by ``evo tier auto``.
    session_tier_override: dict[str, str] | None = None

    def __post_init__(self) -> None:
        # Frame the user-visible body with evo delimiters. Idempotent;
        # safe to apply at construction time even when a downstream
        # caller has already wrapped (e.g. wizard TurnResult that
        # wrapped before being threaded through dispatch).
        if self.direct_send_message and not _evo_delim.is_wrapped(self.direct_send_message):
            self.direct_send_message = _evo_delim.wrap(
                self.direct_send_message,
                title=self.direct_send_title,
            )
        # Auto-fill subcommand_brief from the registry if the caller
        # didn't set it explicitly. Centralizes the lookup so handlers
        # don't have to remember to plumb it through every DispatchResult
        # they construct.
        if self.subcommand_brief is None and self.subcommand:
            try:
                cmd = _sc.lookup(self.subcommand)
                if cmd is not None:
                    self.subcommand_brief = cmd.short_help
            except Exception:
                pass

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("notes", None)
        d.pop("network_dirty", None)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def dispatch(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
    raw_text: str,
) -> DispatchResult:
    """Resolve role, parse subcommand, run handler. Pure-ish: handlers may
    read the filesystem but never write in v1.

    Wraps :func:`_dispatch_inner` with an abandon-leftover-wizard pass:
    the wizard is optional, so any new ``evo`` command that doesn't
    itself produce a wizard session must mark the user's prior wizard
    state (if any) abandoned. Otherwise the plugin's recovery probe
    would re-attach to a stale session after a restart and trap the
    user in a wizard they tried to escape.
    """
    result = _dispatch_inner(
        network,
        bot_id=bot_id,
        channel=channel,
        sender_external_id=sender_external_id,
        raw_text=raw_text,
    )
    if not result.wizard_session_id:
        try:
            from .wizard import state as _wizard_state
            shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
            user_key = derive_user_key(
                network,
                bot_id=bot_id,
                channel=channel,
                sender_external_id=sender_external_id,
            )
            _wizard_state.mark_abandoned(shared_dir, bot_id, user_key)
        except Exception:
            # Bookkeeping must not block the user's command.
            pass
    return result


def _dispatch_inner(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
    raw_text: str,
) -> DispatchResult:
    role = resolve_role(network, bot_id, channel, sender_external_id)
    parsed = _sc.parse(raw_text)
    if parsed is None:
        # Caller (the route) should not have invoked us; respond gracefully.
        msg = _unrecognized_message()
        return DispatchResult(
            subcommand="",
            role=role,
            mode="speak",
            system_append=msg,
            direct_send_message=msg,
            notes=["raw_text did not parse as an evo command"],
        )

    # Bare keyword: short orientation message that names the two
    # unambiguous next steps (`evo wizard` or `evo better`) and points
    # at `evo help`. Bare `evo` used to dual-route — wizard if the user
    # hadn't onboarded, recommendation otherwise — which made the single
    # word do two different things depending on hidden state. The two
    # action surfaces are now explicit: `evo wizard` always runs setup,
    # `evo better` always asks the engine for a rec.
    if parsed.is_bare:
        from .handlers import orient as _orient
        return _orient.render(
            role=role,
            bot_id=bot_id,
            network=network,
            channel=channel,
            sender_external_id=sender_external_id,
        )

    cmd = _sc.lookup(parsed.subcommand)
    if cmd is None:
        msg = _unknown_subcommand_message(parsed.subcommand, role)
        return DispatchResult(
            subcommand=parsed.subcommand,
            role=role,
            mode="speak",
            system_append=msg,
            direct_send_message=msg,
            notes=[f"unknown subcommand '{parsed.subcommand}'"],
        )

    # Admin is a strict superset of primary — bypasses available_to so we
    # don't have to enumerate "admin OR primary" everywhere in the registry.
    # Centralized in ``role_can_run`` so this gate, the help long-form
    # renderer, and ``subcommands_for_role`` can't disagree.
    if not _sc.role_can_run(role, cmd):
        msg = _not_available_message(cmd.name, role)
        return DispatchResult(
            subcommand=cmd.name,
            role=role,
            mode="speak",
            system_append=msg,
            direct_send_message=msg,
            notes=[f"role={role} not in {sorted(cmd.available_to)}"],
        )

    # ``evo better`` is just an explicit alias of the bare keyword: start
    # a conversational-approval session same as ``evo``.
    if cmd.name == "better":
        return _start_rec_pending_dispatch(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
            role=role,
            invoked_as="better",
        )

    # ``claim`` is special-cased because it mutates network.json (admin
    # storage and per-bot primary_user) and writes audit entries — both
    # need shared_dir + caller context that don't fit the generic handler
    # signature. Spec: docs/spec-evo-wizard-2026-05-05.md §3.2.2.
    if cmd.name == "claim":
        return _handle_claim(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
            args=parsed.args,
            role=role,
        )

    # ``profile`` is special-cased because it needs channel +
    # sender_external_id to derive the user_key for the bot-side
    # profile read/write. Spec: docs/spec-user-profile-2026-05-07.md §D5.
    if cmd.name == "profile":
        from .handlers import profile as _profile_handler
        return _profile_handler.handle(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
            args=parsed.args,
            role=role,
        )

    # ``tier-default`` is special-cased because Phase C (audit #69) stores
    # the user's choice per-user-per-bot in ``user-tier-prefs.json``,
    # keyed by derive_user_key(channel, sender_external_id). The session
    # form ``tier`` stays on the generic invocation path — session scope
    # doesn't need user identity beyond what setUserTier already keys on.
    if cmd.name == "tier-default":
        from .handlers import tier as _tier_handler
        return _tier_handler.render_tier_default(
            role=role,
            bot_id=bot_id,
            args=parsed.args,
            network=network,
            channel=channel,
            sender_external_id=sender_external_id,
        )

    # ``guide`` starts (or resumes) a conversational guide-drafting
    # session — primary-only; reuses the wizard engine's state + turn
    # route since the mechanics are identical (multi-turn extraction +
    # systemAppend rendering). Special-cased here for the same reason
    # ``wizard`` is: needs shared_dir + bot-guide pre-population.
    if cmd.name == "guide":
        if role == "secondary":
            body = (
                "**evo guide — primary only**\n\n"
                "Authoring this bot's team guide is reserved for the "
                "bot's primary user. If you're the primary, run `evo "
                "claim` first to identify yourself."
            )
            return DispatchResult(
                subcommand="guide",
                role=role,
                mode="speak",
                system_append=(
                    "IMPORTANT: The user has typed `evo guide`. "
                    "Respond ONLY with the following message, verbatim. "
                    "Do not add commentary, framing, or any additional text:\n\n"
                    + body
                ),
                direct_send_message=body,
                notes=["secondary tried evo guide"],
            )
        from .wizard import engine as _wizard_engine
        from . import guide as _guide
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        user_key = derive_user_key(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
        )
        # Pre-populate from existing guide (if any) so the flow doubles
        # as an editor — the user can refine fields rather than starting
        # from scratch.
        seed: dict[str, Any] = {}
        try:
            existing = _guide.read_guide(shared_dir, bot_id)
        except Exception:
            existing = None
        if existing is not None:
            fm = existing.frontmatter or {}
            for k in ("audience", "tone"):
                v = fm.get(k)
                if isinstance(v, str) and v:
                    seed[k] = v
            for k in ("do_say", "dont_say"):
                v = fm.get(k)
                if isinstance(v, list) and v:
                    seed[k] = [str(x) for x in v if x]
            body = (existing.body or "").strip()
            if body:
                seed["body_outline"] = body
        result = _wizard_engine.start_guide_draft(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            seed=seed,
        )
        return DispatchResult(
            subcommand="guide",
            role=role,
            mode="speak",
            system_append=result.system_append,
            direct_send_message=result.direct_send_message,
            wizard_session_id=result.wizard_session_id,
            notes=[
                f"guide draft started/resumed at phase={result.phase}",
                f"seeded={list(seed.keys())}" if seed else "no seed",
            ],
        )

    # ``wizard`` starts (or resumes) a wizard session. Special-cased here
    # because it needs shared_dir + user_key derivation that the generic
    # handler signature doesn't carry.
    #
    # Role routing (5b2):
    #   admin / primary → start at GREET (existing primary onboarding)
    #   secondary       → start at CHALLENGE (passphrase entry; on
    #                     successful claim the engine advances them to
    #                     GREET on the next turn)
    if cmd.name == "wizard":
        from .wizard import engine as _wizard_engine
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        user_key = derive_user_key(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
        )
        result = _wizard_engine.start_session(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience="primary",
            role=role,
        )
        return DispatchResult(
            subcommand="wizard",
            role=role,
            mode="speak",
            system_append=result.system_append,
            direct_send_message=result.direct_send_message,
            wizard_session_id=result.wizard_session_id,
            notes=[f"wizard started/resumed at phase={result.phase} role={role}"],
        )

    # `evo app create` starts the Wave 3 app-creation wizard. Same
    # plumbing as `evo wizard` / `evo guide` — needs shared_dir +
    # user_key. Only the literal `create` arg is recognized; bare
    # `evo app` (no args) returns usage.
    if cmd.name == "app":
        sub = (parsed.args or "").strip().split(maxsplit=1)
        if not sub or sub[0].lower() != "create":
            body = (
                "**evo app**\n\n"
                "Usage: `evo app create` — drafts a new app from your "
                "description, then asks you to approve / iterate / cancel."
            )
            return DispatchResult(
                subcommand="app",
                role=role,
                mode="speak",
                system_append=_format_speak_block(body),
                direct_send_message=body,
            )
        from .handlers._shared import is_pod_wide_caller
        if is_pod_wide_caller(bot_id, network):
            body = (
                "Run `evo app create` from the bot you want the new app "
                "on, not from the primary sysadmin bot. Chat-drafted "
                "apps target the calling bot."
            )
            return DispatchResult(
                subcommand="app",
                role=role,
                mode="speak",
                system_append=_format_speak_block(body),
                direct_send_message=body,
            )
        from .wizard import engine as _wizard_engine
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        user_key = derive_user_key(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
        )
        result = _wizard_engine.start_app_create(
            shared_dir, bot_id=bot_id, user_key=user_key,
        )
        return DispatchResult(
            subcommand="app",
            role=role,
            mode="speak",
            system_append=result.system_append,
            direct_send_message=result.direct_send_message,
            wizard_session_id=result.wizard_session_id,
            notes=[f"app create started/resumed at phase={result.phase}"],
        )

    # `evo setup-google` is per-bot under the per-bot OAuth client design
    # (PR #1123): each bot has its own OAuth app, typically tied to a
    # different Google Cloud project / org / billing path. The wizard
    # runs from the bot being set up; the client_id + client_secret it
    # collects are stored under that bot's per-bot block. Two bots can
    # opt-in to share by copying each other's `client_secret_ref`.
    if cmd.name == "setup-google":
        from .wizard import engine as _wizard_engine
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        user_key = derive_user_key(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
        )
        result = _wizard_engine.start_google_setup(
            shared_dir, bot_id=bot_id, user_key=user_key,
        )
        return DispatchResult(
            subcommand="setup-google",
            role=role,
            mode="speak",
            system_append=result.system_append,
            direct_send_message=result.direct_send_message,
            wizard_session_id=result.wizard_session_id,
            notes=[f"setup-google started/resumed at phase={result.phase}"],
        )

    # `evo add-bot` starts the conversational bot-creation wizard (M1:
    # need → audience → purpose → name → plan). Admin-only via the
    # registry's available_to gate, which already ran above. Same
    # plumbing as `evo wizard` / `evo guide` — needs shared_dir +
    # user_key. Spec: docs/spec-add-bot-wizard-build-delta-2026-06-10.md §3.1.
    if cmd.name == "add-bot":
        from .wizard import engine as _wizard_engine
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        user_key = derive_user_key(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
        )
        result = _wizard_engine.start_add_bot(
            shared_dir, bot_id=bot_id, user_key=user_key,
        )
        return DispatchResult(
            subcommand="add-bot",
            role=role,
            mode="speak",
            system_append=result.system_append,
            direct_send_message=result.direct_send_message,
            wizard_session_id=result.wizard_session_id,
            notes=[f"add-bot started/resumed at phase={result.phase}"],
        )

    return _invoke_handler(
        cmd.handler,
        role=role,
        bot_id=bot_id,
        args=parsed.args,
        network=network,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Conversational approval dispatch (slice 5b8)
# ─────────────────────────────────────────────────────────────────────────────


def _start_rec_pending_dispatch(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
    role: Role,
    invoked_as: str,
) -> DispatchResult:
    """Fetch the top rec for ``bot_id`` (member_bot surface) and start a
    conversational-approval wizard session. Returns a ``DispatchResult``
    with ``wizard_session_id`` set so the plugin routes the user's next
    reply through ``/api/evo/wizard/turn`` and into
    :func:`evolve_admin.evo.wizard.engine._handle_rec_pending`.

    When the BetterEngine can't be reached (import error, missing data
    dir mid-deploy) we proceed with ``rec=None`` so the wizard renders
    the "all caught up" variant rather than dropping the turn.
    """
    from .wizard import engine as _wizard_engine
    from .identity import derive_user_key

    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    user_key = derive_user_key(
        network, bot_id=bot_id,
        channel=channel, sender_external_id=sender_external_id,
    )

    # `evo better` is unconditional now — bare `evo` does the orientation
    # nudge toward `evo wizard` for un-onboarded users, so this path
    # always tries to surface a rec.

    # In-process surfacing: when the user has manual-completion
    # proposals already in their In Process queue (Investigation /
    # WorkflowInstruction sitting in applied/), check in on those FIRST
    # before pulling from the inbox. Closes the loop on "you said
    # you'd handle that — done?" without the user having to remember
    # to bring it up.
    rec_dict: dict[str, Any] | None = None
    pending_kind = "inbox"
    try:
        from . import arbiter_bridge as _bridge

        in_process = _bridge.list_in_process_for_bot(shared_dir, bot_id)
    except Exception:
        in_process = []

    if in_process:
        # Materialize the oldest in-process proposal as a Recommendation
        # dict so the existing wizard machinery can render it. Same
        # adapter the BetterEngine uses for pending proposals.
        try:
            from ..better_engine.proposal_reader import (
                proposal_to_recommendation,
            )

            in_process_rec = proposal_to_recommendation(in_process[0])
            if in_process_rec is not None:
                rec_dict = in_process_rec.to_dict()
                pending_kind = "inprocess"
        except Exception:
            rec_dict = None

    engine_unavailable = False
    if rec_dict is None:
        # No in-process work — fall through to the inbox.
        try:
            from ..better_engine.engine import BetterEngine
            engine = BetterEngine(shared_dir, network)
            rec = engine.get_top(surface="member_bot", scope_id=bot_id)
            if rec is not None:
                try:
                    rec_dict = rec.to_dict()
                except Exception:
                    rec_dict = None
        except Exception:
            # If we can't reach the engine (import error, missing data
            # dir mid-deploy) treat the inbox as empty — the wizard
            # renders an "all caught up" message rather than dropping
            # the turn.
            engine_unavailable = True

    result = _wizard_engine.start_rec_pending(
        shared_dir,
        bot_id=bot_id,
        user_key=user_key,
        rec=rec_dict,
        surface="member_bot",
        pending_kind=pending_kind,
    )

    notes = [
        f"{invoked_as} → rec_pending "
        f"({'rec=' + str(rec_dict.get('id')) if rec_dict else 'queue empty'}, "
        f"kind={pending_kind}, completed={result.completed})",
    ]
    if engine_unavailable:
        notes.append("BetterEngine unavailable; rendered as empty queue")

    return DispatchResult(
        subcommand=invoked_as,
        role=role,
        mode="speak",
        system_append=result.system_append,
        direct_send_message=result.direct_send_message,
        wizard_session_id=result.wizard_session_id,
        notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Handler invocation
# ─────────────────────────────────────────────────────────────────────────────


def _invoke_handler(
    handler_path: str,
    *,
    role: Role,
    bot_id: str,
    args: str,
    network: dict[str, Any],
) -> DispatchResult:
    module_name, _, func_name = handler_path.partition(":")
    if not module_name or not func_name:
        msg = _internal_error_message()
        return DispatchResult(
            subcommand="",
            role=role,
            mode="speak",
            system_append=msg,
            direct_send_message=msg,
            notes=[f"malformed handler path: {handler_path!r}"],
        )
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
    except (ImportError, AttributeError) as e:
        msg = _internal_error_message()
        return DispatchResult(
            subcommand="",
            role=role,
            mode="speak",
            system_append=msg,
            direct_send_message=msg,
            notes=[f"failed to import {handler_path}: {e}"],
        )

    result = func(role=role, bot_id=bot_id, args=args, network=network)
    if not isinstance(result, DispatchResult):
        msg = _internal_error_message()
        return DispatchResult(
            subcommand="",
            role=role,
            mode="speak",
            system_append=msg,
            direct_send_message=msg,
            notes=[f"handler {handler_path} returned {type(result).__name__}"],
        )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Canned messages used when no handler runs
# ─────────────────────────────────────────────────────────────────────────────


def _unrecognized_message() -> str:
    return (
        "I didn't recognize that as an evo command. "
        "Try `evo help` to see what's available."
    )


def _unknown_subcommand_message(name: str, role: Role) -> str:
    return (
        f"`evo {name}` isn't a command I know. "
        f"Try `evo help` to see the list of commands available to you."
    )


def _not_available_message(name: str, role: Role) -> str:
    return (
        f"`evo {name}` isn't available to you on this bot. "
        f"Try `evo help` for the commands you can run here."
    )


def _internal_error_message() -> str:
    return (
        "Something went wrong dispatching that command. "
        "Try `evo help` or check the admin logs."
    )


# ─────────────────────────────────────────────────────────────────────────────
# `evo claim` handler — passphrase-driven self-claim of admin / primary
# ─────────────────────────────────────────────────────────────────────────────


def _handle_claim(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
    args: str,
    role: Role,
) -> DispatchResult:
    """Resolve a ``evo claim <pp> [<pp>]`` invocation.

    Each whitespace-separated token in ``args`` is checked against the
    pod's admin and primary passphrases (case-insensitive). Multiple
    matches stack: ``charles darwin`` claims both admin and primary in
    one go. Order doesn't matter (``darwin charles`` works the same).

    Side effects:
      * Admin claim → adds (channel, sender_external_id) to
        ``pod.admins.external_ids[channel]``
      * Primary claim → adds the pair to
        ``bots[bot_id].primary_user.external_ids[channel]``, refusing to
        overwrite an existing different value (no in-band ``--force``;
        operator must use the CLI for forced overwrite)

    Persists via :func:`evolve_admin.config.save_network` and writes one
    audit record per successful claim. Returns a spoken summary of what
    was applied (or wasn't, with the reason).
    """
    if not channel or sender_external_id is None:
        return _speak_claim(
            "I can't tell who you are on this channel — `evo claim` needs the "
            "channel + sender ID to be visible. Try again from a direct chat "
            "with the bot, or ask your pod admin to claim you via "
            "`evolve-admin evo claim-primary`.",
            role,
        )

    tokens = [t for t in args.split() if t.strip()]
    if not tokens:
        already_admin = _identity.is_admin(network, channel, sender_external_id)
        primary_block = (
            ((network.get("bots") or {}).get(bot_id) or {}).get("primary_user")
        )
        # Membership over equality (M1-B2 / invariant 6): a person may hold
        # several ids on one channel, and any of them is "already primary".
        already_primary = _ext_ids.has_id(
            primary_block, channel, sender_external_id,
        )
        if already_admin and already_primary:
            return _speak_claim(
                f"You're already recorded as **pod admin** and as the "
                f"**primary user** for `{bot_id}` on this channel — no need "
                "to claim again. Type `evo help` to see what you can do.",
                role,
            )
        if already_admin:
            return _speak_claim(
                "You're already a **pod admin** — admin status applies to "
                f"every bot in the pod. If you'd also like to be recorded "
                f"as the primary user for `{bot_id}`, type "
                "`evo claim <primary-passphrase>` (your pod admin has the "
                "passphrase). Otherwise, type `evo help` to see what's "
                "available to you.",
                role,
            )
        if already_primary:
            return _speak_claim(
                f"You're already recorded as the **primary user** for "
                f"`{bot_id}` on this channel. If you'd also like pod-admin "
                "status (applies pod-wide), type "
                "`evo claim <admin-passphrase>` — ask your pod admin for "
                "it. Otherwise, type `evo help` for what you can do here.",
                role,
            )
        return _speak_claim(
            "Usage: `evo claim <passphrase> [<passphrase>]`. Type the "
            "admin and/or primary passphrase your pod admin shared with "
            "you. Match is case-insensitive; you can type one passphrase "
            "or both in either order.",
            role,
        )
    if len(tokens) > 2:
        return _speak_claim(
            "Too many tokens. `evo claim` takes one or two passphrases — "
            "your admin word, your primary word, or both.",
            role,
        )

    is_admin_match = any(_identity.matches_admin_passphrase(network, t) for t in tokens)
    # Use the bot-aware primary matcher so a per-bot
    # ``bots.<id>.primary_passphrase`` override takes precedence over the
    # pod default. Falls back to the pod default when no override is set.
    is_primary_match = any(
        _identity.matches_primary_passphrase_for_bot(network, bot_id, t)
        for t in tokens
    )

    if not is_admin_match and not is_primary_match:
        return _speak_claim(
            "I don't recognize that passphrase. Double-check the word(s) "
            "with your pod admin, then try `evo claim` again.",
            role,
        )

    # Resolve persistence side once; the route handler will save_network
    # whatever mutations we make.
    from pathlib import Path
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    notes: list[str] = []
    spoken_lines: list[str] = []

    # ── Admin claim ──────────────────────────────────────────────────────────
    if is_admin_match:
        already_admin = _identity.is_admin(network, channel, sender_external_id)
        try:
            _identity.claim_admin(
                network,
                channel=channel,
                external_id=sender_external_id,
            )
        except _identity.ClaimError as e:
            spoken_lines.append(f"admin: not recorded — {e}")
            notes.append(f"admin claim failed: {e}")
        else:
            if already_admin:
                spoken_lines.append("admin: you're already recorded as a pod admin.")
                notes.append("admin claim was a no-op")
            else:
                spoken_lines.append("admin: recorded — you have full pod access from now on.")
                notes.append("admin claim recorded")
                try:
                    _audit.append_event(
                        shared_dir,
                        actor=_audit.api_actor(),
                        action="claim_admin",
                        bot_id="<pod>",
                        channel=channel,
                        from_external_id=None,
                        to_external_id=str(sender_external_id),
                        reason=None,
                    )
                except Exception as e:
                    notes.append(f"admin audit write failed: {e}")

    # ── Primary claim (per current bot) ──────────────────────────────────────
    if is_primary_match:
        # Read prior value before mutation (None if no primary yet).
        # Tolerant reader — see the M1-B2 note on the audit "from" field.
        prior = _ext_ids.first_id_for(
            ((network.get("bots") or {}).get(bot_id) or {}).get("primary_user"),
            channel,
        )
        try:
            _identity.claim_primary(
                network,
                bot_id,
                channel=channel,
                external_id=sender_external_id,
            )
        except _identity.ClaimError as e:
            msg = str(e)
            if "already recorded" in msg:
                # Refused due to conflict — clearer message than the writer's.
                spoken_lines.append(
                    "primary: another user is already recorded as primary on "
                    "this bot. Ask your pod admin to reassign via "
                    "`evolve-admin evo reassign-primary` if this is wrong."
                )
            else:
                spoken_lines.append(f"primary: not recorded — {msg}")
            notes.append(f"primary claim failed: {msg}")
        else:
            new_value = str(sender_external_id)
            if str(prior or "") == new_value:
                spoken_lines.append(
                    f"primary: you're already recorded as primary on {bot_id}."
                )
                notes.append("primary claim was a no-op")
            else:
                spoken_lines.append(
                    f"primary: recorded — you're now the primary user on {bot_id}."
                )
                notes.append("primary claim recorded")
                try:
                    _audit.append_event(
                        shared_dir,
                        actor=_audit.api_actor(),
                        action="claim",
                        bot_id=bot_id,
                        channel=channel,
                        from_external_id=(str(prior) if prior is not None else None),
                        to_external_id=new_value,
                        force=False,
                        reason=None,
                    )
                except Exception as e:
                    notes.append(f"primary audit write failed: {e}")

    # Did anything actually change in network.json? If so, signal the route
    # to persist. We treat any successful claim — even an idempotent
    # re-claim that adds nothing — as not dirty when the writer's no-op
    # path was taken. The notes list discriminates: "recorded" entries
    # mean a real change happened.
    network_dirty = any("recorded" in n and "failed" not in n for n in notes)

    msg = "Identity claim:\n  " + "\n  ".join(spoken_lines)
    return DispatchResult(
        subcommand="claim",
        role=role,
        mode="speak",
        system_append=_format_speak_block(msg),
        direct_send_message=msg,
        notes=notes,
        network_dirty=network_dirty,
    )


def _speak_claim(message: str, role: Role) -> DispatchResult:
    return DispatchResult(
        subcommand="claim",
        role=role,
        mode="speak",
        system_append=_format_speak_block(message),
        direct_send_message=message,
    )


def _format_speak_block(message: str) -> str:
    return (
        "IMPORTANT: The user has typed `evo claim`. "
        "Respond ONLY with the following message, verbatim. "
        "Do not add commentary, framing, or any additional text:\n\n"
        + message
    )


def _secondary_wizard_stub_message() -> str:
    """Deprecated as of slice 5b2 — secondaries now enter the wizard at
    the CHALLENGE phase rather than getting a stub. Kept for any callers
    still importing it (none in tree at the moment); will be removed
    when the secondary-variant arrives in slice 5b4."""
    return (
        "IMPORTANT: The user has typed `evo wizard`. "
        "Respond ONLY with the following message, verbatim. "
        "Do not add commentary, framing, or any additional text:\n\n"
        "**evo wizard — coming soon for non-primary users**\n\n"
        "The full primary onboarding is live; the secondary-user variant "
        "(focused on this specific bot rather than the Evolve platform) "
        "lands in the next slice. For now, ask the bot's primary for an "
        "intro, or run `evo help` to see what you can do today."
    )
