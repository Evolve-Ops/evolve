"""Feature → OAuth scope bundles for Slack bot apps.

Setting up a Slack app's permissions is a Plex-test failure mode: an
operator who wants "the bot should respond in channels" doesn't know
that they need ``app_mentions:read`` + ``channels:read`` + ``channels:history``
+ ``chat:write`` together — and missing any one of those produces a
silent partial-success ("the bot is connected but doesn't respond to
@mentions in #design"). The fix is feature-grouped bundles plus a
checklist the doctor renders against the live scope set.

Source of mapping: Slack API scope docs cross-referenced with the
actual scope set the live `team_bot_a` bot uses on the production mini
(2026-05-13). Bundles are deliberately conservative — they enable the
feature, no more. Operators wanting broader powers add scopes via the
Slack app dashboard; the doctor surfaces "broader than needed" as a
hint, not a finding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeBundle:
    """One coherent feature plus the scopes it needs.

    ``name`` is operator-facing (the Plex-test caption); ``scopes``
    is the set of OAuth scopes Slack requires to enable it. If every
    scope in the bundle is present the feature is enabled; if any are
    missing, the bundle is "partial" or "off" and the doctor names the
    missing scopes so the operator knows exactly what to add in the
    Slack app dashboard.
    """
    key: str                 # stable identifier for cross-references
    name: str                # operator-facing label
    scopes: tuple[str, ...]
    rationale: str           # one-line explanation


# Bundles ordered roughly by setup priority — most operators need 1-5;
# 6-9 are common; 10+ are situational.
BUNDLES: tuple[ScopeBundle, ...] = (
    ScopeBundle(
        key="receive_mentions",
        name="Respond to @-mentions",
        scopes=("app_mentions:read", "chat:write"),
        rationale="Bot hears @team_bot_a in any channel it's added to and can reply.",
    ),
    ScopeBundle(
        key="send_dms",
        name="Send and receive direct messages",
        scopes=("im:history", "im:read", "im:write", "chat:write"),
        rationale="One-on-one DM conversations with users.",
    ),
    ScopeBundle(
        key="multi_person_dms",
        name="Participate in ad-hoc group DMs",
        scopes=("mpim:history", "mpim:read", "mpim:write", "chat:write"),
        rationale="Multi-person DMs (the 'group chat' Slack flow without a channel).",
    ),
    ScopeBundle(
        key="public_channels",
        name="Read + reply in public channels",
        scopes=("channels:history", "channels:read", "chat:write"),
        rationale="Bot processes every message in channels it's a member of (or every @-mention if requireMention is on).",
    ),
    ScopeBundle(
        key="private_channels",
        name="Read + reply in private channels",
        scopes=("groups:history", "groups:read", "chat:write"),
        rationale="Same as above but for private (invite-only) channels.",
    ),
    ScopeBundle(
        key="thinking_reaction",
        name='Show "thinking" via emoji reaction',
        scopes=("reactions:write",),
        rationale="Bot adds an emoji to the message it's working on, the visual feedback that erases 'is it broken or just slow?'.",
    ),
    ScopeBundle(
        key="read_reactions",
        name="See reactions other people add",
        scopes=("reactions:read",),
        rationale="Useful for 'react with 👍 to confirm' UX patterns.",
    ),
    ScopeBundle(
        key="file_attachments",
        name="Handle file attachments",
        scopes=("files:read", "files:write"),
        rationale="Bot reads uploaded files and can attach files to its replies.",
    ),
    ScopeBundle(
        key="user_directory",
        name="Look up users + workspace directory",
        scopes=("users:read",),
        rationale=(
            "Lets the bot resolve '@dave' to a Slack user ID and powers the "
            "Evolve workspace identity directory (the team_bot_a-2026-05-15 fix — "
            "maps each user's Slack ID, legacy name, display_name, and "
            "real_name so the bot doesn't confuse aliases)."
        ),
    ),
    ScopeBundle(
        key="user_directory_email",
        name="Workspace directory: email column",
        scopes=("users:read.email",),
        rationale=(
            "Adds the email column to the workspace identity directory. "
            "Email is one of the strongest disambiguation anchors — "
            "recommended for any team-bot install."
        ),
    ),
    ScopeBundle(
        key="workspace_info",
        name="Read workspace name + branding",
        scopes=("team:read",),
        rationale="Lets the bot include the workspace name in messages and logs.",
    ),
    ScopeBundle(
        key="usergroups",
        name="Read user groups (@-team mentions)",
        scopes=("usergroups:read",),
        rationale="Lets the bot expand @-engineering to its current members.",
    ),
    ScopeBundle(
        key="post_anywhere",
        name="Post in any public channel without being a member",
        scopes=("chat:write.public",),
        rationale="Bot can post in any public channel, not just ones it's been invited to. Useful for cross-channel announcements; widens blast radius.",
    ),
    ScopeBundle(
        key="custom_sender",
        name="Customize sender name + avatar per message",
        scopes=("chat:write.customize",),
        rationale="Bot can post-as a different name/icon (e.g. system-vs-personal voice). Required for the 'thinking' status-message pattern.",
    ),
    ScopeBundle(
        key="search",
        name="Search workspace history",
        scopes=(
            "search:read.files", "search:read.im", "search:read.mpim",
            "search:read.private", "search:read.public", "search:read.users",
        ),
        rationale="Bot can answer 'where did we discuss X last week?'. Wide blast radius — bot can see every message it's authorized to see.",
    ),
    ScopeBundle(
        key="assistant_api",
        name="Use Slack's AI Assistant surface",
        scopes=("assistant:write",),
        rationale="Bot integrates with Slack's native AI Assistant UI (sidebar, in-thread suggestions).",
    ),
    # ── Higher-risk scopes operators rarely need; surfaced as "broader than needed". ──
    ScopeBundle(
        key="manage_channels",
        name="Create / archive / rename channels",
        scopes=("channels:manage", "groups:write"),
        rationale="Operator-level access — bot can structurally change the workspace. Most operators DON'T need this.",
    ),
    ScopeBundle(
        key="manage_usergroups",
        name="Create / modify user groups",
        scopes=("usergroups:write",),
        rationale="Bot can change who's in @-engineering. Operator-level; rarely needed.",
    ),
    ScopeBundle(
        key="auto_join",
        name="Auto-join public channels",
        scopes=("channels:join",),
        rationale="Bot can add itself to public channels without being invited.",
    ),
)


# Scopes the doctor considers "elevated" — surfaced separately as a
# security-posture note even when the bundle they belong to is enabled.
ELEVATED_SCOPES: frozenset[str] = frozenset({
    "channels:manage",
    "groups:write",
    "usergroups:write",
    "search:read.files",
    "search:read.im",
    "search:read.mpim",
    "search:read.private",
    "search:read.public",
    "search:read.users",
    "chat:write.public",
})


@dataclass
class BundleStatus:
    """One feature's status given the bot's actual scope set."""
    bundle: ScopeBundle
    enabled: bool
    missing: tuple[str, ...]    # scopes the bundle needs but the bot doesn't have


def evaluate_bundles(
    scopes: "set[str] | frozenset[str]",
) -> list[BundleStatus]:
    """Return one :class:`BundleStatus` per known bundle.

    ``scopes`` is the set of OAuth scopes the bot's token currently has,
    extracted from the ``x-oauth-scopes`` header on any Slack Web API
    response. We compare bundle-by-bundle so the operator can read off
    the checklist directly.
    """
    out: list[BundleStatus] = []
    for bundle in BUNDLES:
        missing = tuple(s for s in bundle.scopes if s not in scopes)
        out.append(BundleStatus(
            bundle=bundle,
            enabled=not missing,
            missing=missing,
        ))
    return out


def elevated_scopes_present(
    scopes: "set[str] | frozenset[str]",
) -> tuple[str, ...]:
    """Return the elevated scopes currently granted, sorted alphabetically."""
    return tuple(sorted(s for s in scopes if s in ELEVATED_SCOPES))


def scopes_not_used_by_any_bundle(
    scopes: "set[str] | frozenset[str]",
) -> tuple[str, ...]:
    """Scopes the bot has that no documented bundle accounts for.

    Useful for forward-compatibility — when Slack adds a new scope or
    OC's bundle list lags, this surface tells the operator "you have X
    but nothing in our checklist mentions it."
    """
    known: set[str] = set()
    for bundle in BUNDLES:
        known.update(bundle.scopes)
    return tuple(sorted(s for s in scopes if s not in known))


__all__ = [
    "BUNDLES",
    "BundleStatus",
    "ELEVATED_SCOPES",
    "ScopeBundle",
    "elevated_scopes_present",
    "evaluate_bundles",
    "scopes_not_used_by_any_bundle",
]
