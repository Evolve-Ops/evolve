"""Built-in capability registry + role → capability resolution.

Phase C of docs/spec-user-roster-and-roles-2026-06-07.md. The capability
layer above the role layer. Roles are operator-facing ("admin",
"primary_user", "participant", "blocked"); capabilities are
code-checkable permission strings ("bot.roster.mutate",
"bot.send_external").

This module only ships **built-in** capabilities — the ones the platform
defines for bot-system operations. App-declared capabilities
(``provided_capabilities`` from manifest schema v7/v21+) layer on top via
the per-bot ``role_bindings`` block in the overlay file. Phase C.1
delivers the built-in side first; app-declared capability resolution
hooks in when the role_bindings UI ships.

Spec: docs/spec-user-roster-and-roles-2026-06-07.md §4 (Capabilities).
"""

from __future__ import annotations

from typing import Iterable


# ── Built-in capabilities ────────────────────────────────────────────────


BUILTIN_CAPABILITIES: dict[str, str] = {
    "bot.roster.read":      "Read the roster (identities, roles, engagement, blocked, channel modes)",
    "bot.roster.mutate":    "Add, modify, or block identities; set roles; set engagement surfaces",
    "bot.roles.bind":       "Modify role → capability bindings",
    "bot.channel.config":   "Set per-channel newcomer_mode + default_engagement_surfaces; manage the channel/group allowlist",
    "bot.config.modify":    "Edit bot configuration (openclaw.json, network.json bot block)",
    "bot.app.install":      "Install or remove apps on the bot",
    "bot.code.modify":      "Modify bot scripts or code under the bot's workspace",
    "bot.send_external":    "Reach outside the bot's primary channel (alt channel post, webhook, email)",
}
"""Maps capability id → one-line description.

Description is used by the admin UI + roster_bindings response so the
operator can hover/expand and see what the capability gates without
reading the spec.
"""


# ── Default role bindings ────────────────────────────────────────────────


DEFAULT_ROLE_CAPABILITIES: dict[str, list[str]] = {
    # Pod admin gets everything — they're the human operator with UI
    # access and full sudo on the deploy machine; capability gating
    # would be a fiction.
    "admin": ["*"],

    # Primary user gets the bot-scoped admin set: manage the bot's
    # roster + channel modes + reach + bot config, but NOT install
    # apps or modify code (those touch the pod's deploy surface) and
    # NOT bind capabilities to roles (that's the pod admin's design
    # decision per-bot, not a per-bot user's).
    "primary_user": [
        "bot.roster.read",
        "bot.roster.mutate",
        "bot.channel.config",
        "bot.config.modify",
        "bot.send_external",
    ],

    # Participant gets none of the bot.* built-ins. They get
    # app-declared capabilities via the per-bot role_bindings overlay,
    # which is admin-tunable per bot at install time.
    "participant": [],

    # Sticky deny.
    "blocked": [],
}
"""Default role → capability set for built-ins.

These bind when the overlay's ``role_bindings`` block is absent or
empty. Once the role-binding UI ships, these become the *starting*
state for a new bot but the operator can tighten or loosen per role
per bot.
"""


# ── Resolution ────────────────────────────────────────────────────────────


def resolve_role_capabilities(
    role: str,
    *,
    overlay_role_bindings: "dict | None" = None,
    app_capabilities: "Iterable[str] | None" = None,
) -> "set[str]":
    """Compute the capability set granted by a role.

    Resolution order:

      1. If ``overlay_role_bindings`` has an explicit list for the role,
         use it. ``"*"`` in the list expands to all known capabilities
         (built-in + ``app_capabilities``).
      2. Otherwise, fall back to ``DEFAULT_ROLE_CAPABILITIES[role]``.

    ``app_capabilities`` is the set of app-declared capability ids
    currently registered on this bot (sourced from each installed
    manifest's ``provided_capabilities[].name``). Pass them in so
    ``"*"`` can expand correctly; pass None or empty when only
    built-ins are in scope.
    """
    if role == "blocked":
        # Sticky-deny — overlay can't override.
        return set()

    all_known = set(BUILTIN_CAPABILITIES.keys())
    if app_capabilities:
        all_known.update(app_capabilities)

    bindings = (overlay_role_bindings or {})
    if role in bindings:
        explicit = bindings[role]
        if isinstance(explicit, list):
            if "*" in explicit:
                return set(all_known)
            return {c for c in explicit if isinstance(c, str) and c}

    # Fall back to defaults.
    default = DEFAULT_ROLE_CAPABILITIES.get(role, [])
    if "*" in default:
        return set(all_known)
    return set(default)


def has_capability(
    role: str,
    capability: str,
    *,
    overlay_role_bindings: "dict | None" = None,
    app_capabilities: "Iterable[str] | None" = None,
) -> bool:
    """Convenience: does this role grant this capability?"""
    return capability in resolve_role_capabilities(
        role,
        overlay_role_bindings=overlay_role_bindings,
        app_capabilities=app_capabilities,
    )


# ── Capability → endpoint mapping ────────────────────────────────────────


# The admin-daemon roster endpoints use these capabilities to gate
# inbound requests. The header-based auth path in routes_bot_users
# consults this map after resolving the requester's role.
ENDPOINT_CAPABILITIES: dict[str, str] = {
    # routes_bot_users PATCH/POST/PUT
    "roster.mutate.role":           "bot.roster.mutate",
    "roster.mutate.engagement":     "bot.roster.mutate",
    "roster.mutate.block":          "bot.roster.mutate",
    "roster.mutate.unblock":        "bot.roster.mutate",
    "channel.config.newcomer_mode": "bot.channel.config",
    "roles.bind":                   "bot.roles.bind",
    "roster.read":                  "bot.roster.read",
}
"""Endpoint id → required capability.

Endpoint ids are stable strings the admin-daemon route uses to look up
the gating capability. Keeping the mapping centralized here makes it
trivial to audit "which capability protects this endpoint" and to add
new endpoints without scattering the choice across routes.
"""
