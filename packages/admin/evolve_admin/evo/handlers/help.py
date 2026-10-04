"""``evo help`` and ``evo help <name>`` rendering.

Renders directly from the subcommand registry, so adding a new subcommand
elsewhere in the package surfaces it here automatically.
"""

from __future__ import annotations

from typing import Any

from .. import subcommands as _sc
from ..dispatch import DispatchResult
from ..identity import Role


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    args = args.strip()
    if args:
        return _render_long(role, args)
    return _render_short(role)


def _render_short(role: Role) -> DispatchResult:
    visible = _sc.subcommands_for_role(role)
    # The bolded "**evo commands**" heading folded into the delimiter
    # row via ``direct_send_title`` below; bullets drop the backticks
    # around the command name (Telegram code-spans add visual
    # stutter in dense lists and the literal command form is already
    # legible on its own).
    body_lines = []
    for sc in visible:
        body_lines.append(f"  • evo {sc.name} — {sc.short_help}")
    body_lines.append("")
    body_lines.append("Run `evo help <name>` for details.")

    msg = "\n".join(body_lines)
    return DispatchResult(
        subcommand="help",
        role=role,
        mode="speak",
        system_append=_speak_verbatim(msg),
        direct_send_message=msg,
        direct_send_title="evo commands",
    )


def _render_long(role: Role, name: str) -> DispatchResult:
    cmd = _sc.lookup(name)
    if cmd is None:
        msg = (
            f"I don't know `evo {name}`. Run `evo help` for the list of "
            "commands."
        )
        return DispatchResult(
            subcommand="help",
            role=role,
            mode="speak",
            system_append=_speak_verbatim(msg),
            direct_send_message=msg,
        )
    if not _sc.role_can_run(role, cmd):
        msg = (
            f"`evo {cmd.name}` isn't available to you on this bot. "
            f"Run `evo help` for the commands you can run here."
        )
        return DispatchResult(
            subcommand="help",
            role=role,
            mode="speak",
            system_append=_speak_verbatim(msg),
            direct_send_message=msg,
        )

    msg = (
        f"**evo {cmd.name}**\n"
        f"{cmd.short_help}\n\n"
        f"{cmd.long_help}"
    )
    return DispatchResult(
        subcommand="help",
        role=role,
        mode="speak",
        system_append=_speak_verbatim(msg),
        direct_send_message=msg,
    )


def _speak_verbatim(message: str) -> str:
    """Wrap a static block with the standard "respond verbatim" instruction.

    Mirrors ``KeywordHandler.buildKeywordInjection`` on the plugin side. The
    LLM is asked to emit the message exactly, with no preamble or commentary.
    """
    return (
        "IMPORTANT: The user has typed an `evo` command. "
        "Respond ONLY with the following message, verbatim. "
        "Do not add commentary, framing, or any additional text:\n\n"
        + message
    )
