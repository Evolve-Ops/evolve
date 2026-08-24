"""Visual delimiters for evo direct-sent content.

The motivating problem: OC 2026.4.29's pi-embedded runner does not
fire ``before_agent_reply`` for Telegram user-message-triggered runs
(the hook is gated on ``trigger === "cron"`` in ``pi-embedded-…js``).
That means the plugin can't suppress the LLM's reply server-side
when it has already direct-sent a verbatim body via the Bot API.

Result: the user often sees TWO messages — the direct-sent body, and
a hallucinated commentary from the bot's LLM that ignored the
stay-silent instructions in systemAppend (which OC also drops from
the ``before_model_resolve`` return shape, but that's a separate
upstream gap fixed in this PR by switching to ``before_prompt_build``
+ ``appendSystemContext``).

Until upstream OC fires post-LLM hooks reliably, we make the
direct-sent content visually distinguishable. Delimiters frame every
plugin-direct-sent message so the operator can spot which messages
are canonical (from the wizard / dispatcher) vs which are LLM noise.
The plugin's ``before_prompt_build`` hook also references these
delimiters in its stay-silent directive — at least the LLM is told
"the user already sees content framed by these markers; you didn't
generate it, don't respond."

Both halves of the system (Python admin server, TS plugin) need to
agree on the delimiter strings. The plugin re-declares them; keep
them in sync.
"""

from __future__ import annotations


# Visible Unicode box-drawing characters — render legibly in every
# chat client we care about (Telegram, Slack, Discord, web). Distinct
# enough from typical bot text that the operator's eye picks them up.
EVO_DELIMITER_OPEN = "═══ evo ═══"
EVO_DELIMITER_CLOSE = "═══ end evo ═══"


# First-turn heads-up for multi-turn flows. Inserted at the top of
# wizard intro prompts so the operator knows what to expect when the
# bot's LLM also chimes in alongside the wizard.
EVO_HEADS_UP_NOTE = (
    "_Heads-up: the bot may chime in with its own commentary "
    "alongside these messages. Only content framed by_ "
    "`═══ evo ═══` _…_ `═══ end evo ═══` _is from the wizard / "
    "command. Ignore other messages from the bot until upstream "
    "OpenClaw ships the post-LLM hook fix._"
)


def wrap(body: str, title: str | None = None) -> str:
    """Wrap a direct-send body with visual delimiters. Empty / None
    inputs pass through unchanged so the wrap is safe to call from
    auto-routing code paths that may run with no body.

    ``title`` overrides the default ``evo`` label inside the opener —
    e.g. ``title="evo commands"`` yields ``═══ evo commands ═══`` so
    handlers that already prefix their body with a bolded title can
    fold it into the delimiter row and avoid the visual stutter of
    ``═══ evo ═══`` immediately followed by ``**evo commands**``.
    """
    if not body:
        return body
    opener = _build_opener(title)
    return f"{opener}\n\n{body}\n\n{EVO_DELIMITER_CLOSE}"


def _build_opener(title: str | None) -> str:
    """Build the open-delimiter row. Default is the bare ``EVO_DELIMITER_OPEN``;
    a non-empty ``title`` slots in between the box-drawing rules.

    Centralized so ``wrap`` and ``is_wrapped`` agree on the format —
    if either drifts, idempotency in ``DispatchResult.__post_init__``
    starts double-wrapping silently.
    """
    cleaned = (title or "").strip()
    if not cleaned:
        return EVO_DELIMITER_OPEN
    return f"═══ {cleaned} ═══"


def is_wrapped(body: str) -> bool:
    """True if ``body`` already starts with the open delimiter — used
    to make the wrap idempotent. Auto-routing in TurnResult /
    DispatchResult __post_init__ may run multiple times in pathological
    test setups; this guards against double-wrapping.

    Recognizes both the default ``═══ evo ═══`` opener and the custom-
    title form ``═══ <title> ═══`` (introduced when handlers started
    folding their bolded title into the delimiter row).
    """
    if not isinstance(body, str):
        return False
    stripped = body.lstrip()
    if stripped.startswith(EVO_DELIMITER_OPEN):
        return True
    first_line = stripped.split("\n", 1)[0]
    return first_line.startswith("═══ ") and first_line.endswith(" ═══")
