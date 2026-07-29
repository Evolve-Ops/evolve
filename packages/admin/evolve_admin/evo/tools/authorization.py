"""Authorization framework for evo MCP tools (Phase 2).

Three surfaces can invoke evo's MCP tools today, and the tool dispatcher
needs to know which one a given tool_use call came from so it can apply
the right gate:

1. **admin_ui** — the operator types in the admin-UI chat drawer; the
   request lands in ``/api/home/chat`` → ``send_to_evo`` → OC gateway →
   MCP tool call. The operator is the pod admin.

2. **telegram_operator** — the operator DMs the evolve bot on Telegram;
   same OC gateway, same MCP server, same operator behind the bot.

3. **cross_bot_member** — a non-admin user typing ``evo X`` into a
   member bot's channel (Slack, Telegram, Discord). The plugin's
   keyword path routes the subcommand through ``/api/evo/dispatch``,
   but **conversational** follow-ups in the same surface can land at
   evo's MCP tools via the cross-bot relay (eg the future name-resolver
   handoff or any chat that ends up in evo's agent loop with the member
   bot as the originating context). This caller is NOT the pod admin.

Each tool declares its ``authorization_scope`` (``admin`` / ``user`` /
``anyone``). The dispatcher's gate (``check_authorization`` here)
returns a refusal envelope for callers below the tool's scope.

The conservative default is ``admin`` — every existing tool stays
gated until explicitly opened in a follow-up PR. This matches the
"admin UI is the auth layer" rule (memory note
``feedback_ui_authorization_presumed``) while making the gate explicit
for the cross-bot surface where the UI's implicit authority doesn't
apply.

See ``docs/audit-evo-tool-coverage-2026-06-02.md`` §Authorization-tier
classification for the full audit that motivated this framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from . import AuthorizationScope, Tool


# The three surfaces evo's MCP tool dispatcher distinguishes between.
# Adding a new surface (mobile PWA, voice, etc.) means adding it here
# AND deciding which side of the admin/non-admin boundary it lands on
# in :meth:`CallerIdentity.is_admin`. Both choices belong together so a
# new surface can't silently inherit the wrong default.
CallerSurface = Literal["admin_ui", "telegram_operator", "cross_bot_member"]


@dataclass(frozen=True)
class CallerIdentity:
    """Who is invoking this tool, and from which surface?

    Threaded through the MCP dispatcher so the authorization gate can
    decide whether to execute the tool or return a refusal envelope.

    Surfaces map to admin/non-admin as follows:

    * ``admin_ui`` → admin. The admin UI is the pod operator's web
      surface; UI access is the authorization layer (see memory note
      ``feedback_ui_authorization_presumed``).
    * ``telegram_operator`` → admin. The operator's own DM with the
      evolve bot — same human, different transport.
    * ``cross_bot_member`` → non-admin. A team member typing an evo
      keyword into a member bot's channel. They get user/anyone tier
      tools only; admin-tier requests are refused with a clear "ask
      your pod admin" message.

    Optional context fields (``bot_id``, ``operator_handle``) help the
    refusal message tell the caller *what* they tried and *where* —
    operators report a bare "denied" is harder to debug than "you
    tried action.bot.redeploy from team-bot-b — that's admin-only."
    """

    surface: CallerSurface
    bot_id: str | None = None
    operator_handle: str | None = None

    @property
    def is_admin(self) -> bool:
        """True when this caller is the pod admin in some form.

        The two admin surfaces (admin UI, Telegram DM with evolve)
        both resolve to admin because the operator's identity behind
        each is the same human — the pod owner. The cross-bot member
        surface is the only non-admin case today.
        """
        return self.surface in ("admin_ui", "telegram_operator")


# Default identity used when an entry point hasn't been updated to
# thread :class:`CallerIdentity` through yet. Defensive: assume the
# most-conservative interpretation (cross-bot, non-admin) so a new
# call site can't accidentally bypass the gate while the wiring lands.
# Every call site that *does* know the identity must pass it explicitly.
DEFAULT_CONSERVATIVE_IDENTITY = CallerIdentity(surface="cross_bot_member")


def scope_allows(scope: AuthorizationScope, caller: CallerIdentity) -> bool:
    """Does this scope permit the given caller?

    * ``anyone``  → always allow.
    * ``user``    → allow (per-operator preferences; both admin and
                    non-admin callers have their own preferences to
                    manage).
    * ``admin``   → allow only when ``caller.is_admin``.

    The order matters for the cascade: admin > user > anyone. An admin
    caller may invoke any scope; a user-scope tool isn't restricted
    to non-admins.
    """
    if scope == "anyone":
        return True
    if scope == "user":
        return True
    if scope == "admin":
        return caller.is_admin
    # Defensive — an unknown scope value (shouldn't reach here because
    # ``Tool.__post_init__`` rejects unknowns) is treated as admin-only.
    return caller.is_admin


def check_authorization(
    tool: Tool, caller: CallerIdentity,
) -> dict[str, Any] | None:
    """Gate one tool invocation. Returns None when allowed, or a
    refusal envelope (dict) when denied.

    The refusal envelope's shape is stable so the MCP bridge can
    return it as the tool result without further translation; the
    model then surfaces ``message`` to the operator and the
    structured ``error`` / ``scope`` fields are available for
    programmatic callers.

    The refusal message names the tool so the cross-bot caller knows
    *which* command they hit the gate on — operators report bare
    "denied" responses are harder to debug than "you tried
    action.bot.redeploy".
    """
    if scope_allows(tool.authorization_scope, caller):
        return None

    return {
        "error": "authorization_required",
        "scope": tool.authorization_scope,
        "tool": tool.name,
        "surface": caller.surface,
        "message": (
            f"`{tool.name}` is an {tool.authorization_scope}-only "
            "command. Ask your pod admin to run it via Telegram or "
            "the admin UI."
        ),
    }


def visible_tools(
    tools: tuple[Tool, ...], caller: CallerIdentity,
) -> tuple[Tool, ...]:
    """Filter a tool list to the ones this caller is authorized to invoke.

    Used by ``evo help`` (the keyword surface) and ``meta.tools`` (the
    MCP introspection surface) so non-admin callers don't see commands
    they couldn't run anyway. Admin callers see everything.
    """
    return tuple(t for t in tools if scope_allows(t.authorization_scope, caller))


NON_ADMIN_HELP_FOOTER = (
    "Your pod admin has access to additional commands (deploy, "
    "security, etc.). Ask them via Telegram if you need one of those."
)
