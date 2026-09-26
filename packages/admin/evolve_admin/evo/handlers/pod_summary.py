"""``evo status`` / ``evo summary`` / ``evo, what's on this week?`` handler.

Gathers a plain-language pod-wide summary — active alerts, pending proposals,
activity snapshot — and returns it as a DispatchResult the evo bot speaks
verbatim.

Data is read from existing on-disk stores via the cross_bot_summary module.
No LLM calls; no per-bot network round-trips. Typical wall time < 1 second.

Subcommands that route here (registered in subcommands.py):
  evo status
  evo summary
  evo week           (alias for "what's on this week?")

Parsing aliases ("what's on", "on this week", etc.) are handled in
subcommands.parse() before reaching this handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role


def _import_cross_bot_summary():
    """Import cross_bot_summary from the installed evolve-analyzer package.

    Returns the module or None if unavailable (graceful degradation).
    """
    try:
        import cross_bot_summary as _cbs  # type: ignore[import-not-found]
        return _cbs
    except ImportError:
        return None


def render(
    *,
    role: Role,
    bot_id: str,
    args: str,
    network: dict[str, Any],
) -> DispatchResult:
    """Gather and render a pod-wide summary."""
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    cbs = _import_cross_bot_summary()
    if cbs is None:
        msg = (
            "Pod summary isn't available right now — the cross-bot "
            "data module couldn't be loaded. Check the admin logs."
        )
        return DispatchResult(
            subcommand="summary",
            role=role,
            mode="speak",
            system_append=_speak_block(msg),
            direct_send_message=msg,
        )

    try:
        summary = cbs.gather(shared_dir, network)
    except Exception as e:
        msg = (
            f"Pod summary ran into a problem reading data: {e}. "
            "The admin dashboard has full details."
        )
        return DispatchResult(
            subcommand="summary",
            role=role,
            mode="speak",
            system_append=_speak_block(msg),
            direct_send_message=msg,
        )

    # Build a display-name map from the network bots config if available.
    bots_cfg = network.get("bots") or {}
    bot_names: dict[str, str] = {}
    for bid, cfg in bots_cfg.items():
        # Use "name" if set, otherwise fall back to the bot_id.
        name = cfg.get("name") or bid
        bot_names[bid] = name

    body = cbs.render_summary(summary, bot_names=bot_names)

    # Log errors to stderr for diagnostics; don't surface in the message.
    if summary.errors:
        import sys as _sys
        for err in summary.errors:
            print(f"[pod_summary] {err}", file=_sys.stderr)

    return DispatchResult(
        subcommand="summary",
        role=role,
        mode="speak",
        system_append=_speak_block(body),
        direct_send_message=body,
    )


def _speak_block(message: str) -> str:
    return (
        "IMPORTANT: The user has requested a pod summary. "
        "Respond ONLY with the following message, verbatim. "
        "Do not add commentary, framing, or any additional text:\n\n"
        + message
    )
