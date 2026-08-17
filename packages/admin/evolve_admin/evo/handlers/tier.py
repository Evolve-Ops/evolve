"""``evo tier`` and ``evo tier-default`` — user-driven tier control.

Audit follow-up #69 Phase B. Two surfaces, both invoked the same way
from any channel (admin chat, Telegram, Slack, Discord):

* ``evo tier {auto|fast|standard|power|max}`` — session-scoped. Takes
  effect from the NEXT user turn in this conversation; cleared by
  ``evo tier auto`` or by a gateway restart. Slots into the precedence
  ladder at the same level as the admin-UI tier chip (level 2 —
  "user override").
* ``evo tier-default {auto|fast|standard|power|max}`` — persistent.
  Writes to ``{sharedDir}/{botId}/user-tier-prefs.json`` keyed by the
  caller's user_key (Phase C). Slots in at level 4a — "user default"
  — so the user's per-session choice still wins.

``max`` is the frontier role (Fable-class, ~2× Power cost). It is
**pull-only** — no automatic path routes to it; only an explicit user
request or ``evo tier-default max`` can set it. Bot-initiated escalation
to max is blocked by ModelRouter. Per-USER defaults may be max; the
operator BOT-WIDE default is rejected by ModelRouter (it validates
defaultTier against {fast,standard,power}).

Implementation paths:

* Session (``evo tier``): the dispatch envelope grew a
  ``session_tier_override`` field (see ``dispatch.DispatchResult``).
  This handler populates it; the plugin's ``TurnObserver`` reads the
  field off the dispatch response and calls
  ``modelRouter.setUserTier(sessionKey, choice, "evo_keyword")``.
* Persistent (``evo tier-default``): the handler calls
  ``oc_full_config_set_with_error`` directly. No plugin write needed
  — ModelRouter reads ``userTierOverride`` on every turn (wired in
  Phase A's ``loadModelRouterConfig`` + ``reload`` updates).

Per-bot opt-out: both surfaces respect
``userTierOverride.enabled = false`` — when disabled, both commands
return a "this surface is turned off on this bot" message instead of
applying the change. Matches the admin-UI chip's behavior.

Auth: ``BOTH`` (primary + secondary). Tier control belongs to whoever
is having the conversation, not just the bot's admin — the user is
asking THEIR session to think harder or shorter, no privileged
operator state involved.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role
from ._shared import speak


_VALID_CHOICES: tuple[str, ...] = ("auto", "fast", "standard", "power", "max")

# Maps the user's choice to the human-readable name we echo back. The
# bot itself never sees these — only the user does — so they stay in
# the operator-friendly Fast/Standard/Power/Max language used
# everywhere else in the product. ``auto`` is special-cased
# ("classifier-driven routing") since it has no positive name attached.
_CHOICE_LABEL: dict[str, str] = {
    "auto": "Auto",
    "fast": "Fast",
    "standard": "Standard",
    "power": "Power",
    "max": "Max",
}


def _normalize_choice(args: str) -> str | None:
    """Return the canonical choice string, or ``None`` if invalid."""
    token = args.strip().split()[0].lower() if args.strip() else ""
    if not token:
        return None
    return token if token in _VALID_CHOICES else None


def _user_tier_override_enabled(bot_id: str) -> bool:
    """Best-effort read of ``userTierOverride.enabled`` for this bot.

    The plugin and AI Optimization page both treat absence as "enabled"
    (default-on); we do the same. Returns False ONLY when the operator
    has explicitly set ``enabled: false`` on this bot. Any read failure
    falls back to True — defaulting to "feature available" matches the
    operator-installed-the-plugin-so-they-opted-in stance.
    """
    # The bot's evolve-tiers.json is owned by the bot user. The admin
    # server (evolve user) has macOS ACL read access to it. Same path
    # the plugin reads.
    path = Path(f"/Users/{bot_id}/.openclaw/evolve-tiers.json")
    try:
        import json
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    override = (data or {}).get("userTierOverride") or {}
    if not isinstance(override, dict):
        return True
    enabled = override.get("enabled")
    if enabled is False:
        return False
    return True


def _usage_body(form: str) -> str:
    """Render the help/usage body for the `tier` or `tier-default` form."""
    if form == "tier":
        return (
            "Usage: `evo tier {auto|fast|standard|power|max}`\n\n"
            "Sets the model role for the rest of this conversation:\n"
            "  • Fast — quickest replies, cheapest (Haiku-class)\n"
            "  • Standard — balanced default (Sonnet-class)\n"
            "  • Power — high-complexity work, slower, expensive (Opus-class)\n"
            "  • Max — the frontier model, ~2× Power cost; only on explicit request (Fable-class)\n"
            "  • Auto — clear any prior choice; the classifier picks\n\n"
            "Clears on the next gateway restart. Use `evo tier-default` to "
            "set the ongoing default across restarts."
        )
    return (
        "Usage: `evo tier-default {auto|fast|standard|power|max}`\n\n"
        "Sets the ongoing default role for this user on this bot — persists "
        "across sessions and restarts. The session classifier still overrides "
        "this for background work (heartbeats, subagents). Use `evo tier X` "
        "for a one-conversation override.\n\n"
        "Choices: same as `evo tier`. Note: `max` (Fable-class) is pull-only "
        "— you can set it as YOUR personal default, but the operator's "
        "bot-wide default cannot be max."
    )


def _unknown_choice_body(form: str, raw_args: str) -> str:
    """Shared rejection for an unrecognized choice token.

    Framed around effort LEVELS, not model names — a user arriving via
    the `evo model` alias plausibly types a model name (`evo model
    opus`, `evo model claude`); the message teaches the boundary (users
    pick effort, the pod admin decides which models the levels map to)
    instead of just saying "unknown".
    """
    token = raw_args.strip().split()[0]
    return (
        f"`{token}` isn't one I know — pick an effort level: "
        "`auto`, `fast`, `standard`, `power`, or `max`. "
        "(You choose the level; your pod admin decides which models "
        f"those levels map to.) Type `evo {form}` for the full usage."
    )


def _disabled_body(form: str) -> str:
    """Both surfaces share the same disabled-state response."""
    return (
        f"`evo {form}` isn't available on this bot — the operator has "
        "turned off tier-control surfaces in evolve-tiers.json. Ask "
        "them to flip `userTierOverride.enabled` back to `true` in the "
        "AI Optimization page if you want this back."
    )


def _success_body_session(choice: str) -> str:
    """Friendly acknowledgment for ``evo tier X``."""
    label = _CHOICE_LABEL[choice]
    if choice == "auto":
        return (
            "OK — tier override cleared. The classifier will pick from "
            "the next turn on."
        )
    if choice == "max":
        return (
            "OK — using Max (Fable-class, ~2× Power cost) for the rest of "
            "this conversation. Type `evo tier auto` to clear it, or "
            "`evo tier-default max` to make it your ongoing default."
        )
    return (
        f"OK — using {label} tier for the rest of this conversation. "
        f"Type `evo tier auto` to clear it, or `evo tier-default {choice}` "
        f"to make it the ongoing default."
    )


def _success_body_default(choice: str, write_result: dict[str, Any]) -> str:
    """Friendly acknowledgment for ``evo tier-default X``."""
    label = _CHOICE_LABEL[choice]
    if choice == "auto":
        return (
            "OK — default tier reset to Auto. The session classifier "
            "drives routing now."
        )
    if choice == "max":
        return (
            "OK — your personal default is now Max (Fable-class, ~2× Power "
            "cost). It applies to YOUR turns on this bot across sessions; "
            "type `evo tier-default auto` to undo. "
            "(Use `evo tier max` for a one-conversation override.)"
        )
    return (
        f"OK — default tier is now {label} for this bot. It persists "
        f"across sessions and restarts; type `evo tier-default auto` "
        f"to undo. (Use `evo tier X` for a one-conversation override.)"
    )


# ── Session-scoped: evo tier X ─────────────────────────────────────────────


def render_tier(
    *, role: Role, bot_id: str, args: str, network: dict[str, Any],
) -> DispatchResult:
    """Session-scoped tier override (Phase B).

    Validates the user's choice and stamps ``session_tier_override`` on
    the DispatchResult. The plugin reads the field and calls
    ``modelRouter.setUserTier(sessionKey, choice, "evo_keyword")`` so
    the next routing decision sees the override.

    Bare ``evo tier`` (no arg) returns the usage body — no state change.
    Unknown choices return a "didn't recognize that" body — also no
    state change. The choice is normalized to lower-case before the
    field is set so the plugin's setUserTier branch matches cleanly.
    """
    if not args.strip():
        return speak("tier", _usage_body("tier"), role)

    choice = _normalize_choice(args)
    if choice is None:
        return speak("tier", _unknown_choice_body("tier", args), role)

    if not _user_tier_override_enabled(bot_id):
        return speak("tier", _disabled_body("tier"), role)

    result = speak("tier", _success_body_session(choice), role)
    # Stamp the in-memory override directive. The plugin TurnObserver
    # picks this off the response envelope and calls setUserTier.
    result.session_tier_override = {
        "choice": choice,
        "consent_source": "evo_keyword",
    }
    return result


# ── Persistent: evo tier-default X ─────────────────────────────────────────


def render_tier_default(
    *,
    role: Role,
    bot_id: str,
    args: str,
    network: dict[str, Any],
    channel: str | None = None,
    sender_external_id: str | None = None,
) -> DispatchResult:
    """Persistent default-tier write — per-user-per-bot (Phase C).

    Writes the caller's choice into
    ``{sharedDir}/{botId}/user-tier-prefs.json`` keyed by
    :func:`evo.identity.derive_user_key`. The plugin's ModelRouter
    consults this file on every turn, looks up the per-user pref by
    the session's (channel, sender_external_id) tuple, and applies it
    ABOVE the operator's bot-wide ``userTierOverride.defaultTier``
    (Phase A) in the precedence ladder.

    Phase C changes vs Phase B
    --------------------------
    * Phase B wrote ``userTierOverride.defaultTier`` (bot-wide). On a
      multi-user bot every user's "default" call clobbered every other
      user's setting. The operator's bot-wide default lived in the
      same field.
    * Phase C splits ownership: the operator's bot-wide default stays
      in evolve-tiers.json::userTierOverride.defaultTier (set via the
      AI Optimization dropdown, PR #1786). User-specific defaults move
      to the new ``user-tier-prefs.json`` file, keyed by derive_user_key.
    * Net effect: a user typing ``evo tier-default power`` on a team
      bot now affects only their own turns; the operator's choice
      still drives everyone else.

    Choice semantics
    ----------------
    * ``"auto"`` — DELETE the caller's entry from the prefs file.
      Means "stop applying my personal pref; defer to the operator
      default (or bot default) again."
    * ``"fast" | "standard" | "power" | "max"`` — write the entry. The
      plugin sees it on the next turn.

    Acks + audit
    ------------
    Same shape as Phase B except the audit payload now includes the
    resolved ``user_key`` so the operator can reconstruct "which user
    set what when" from the audit log. ``oc_keys`` is empty because
    this write doesn't touch openclaw.json or evolve-tiers.json — it's
    its own file under the shared dir.
    """
    if not args.strip():
        return speak("tier-default", _usage_body("tier-default"), role)

    choice = _normalize_choice(args)
    if choice is None:
        body = _unknown_choice_body("tier-default", args)
        return speak("tier-default", body, role)

    if not _user_tier_override_enabled(bot_id):
        return speak("tier-default", _disabled_body("tier-default"), role)

    # Resolve the caller's user_key from (channel, sender_external_id).
    # Falls back to ``anon:<bot_id>`` if either is missing — see
    # derive_user_key for the precedence ladder. We still write under
    # that key so admin-driven CLI runs that lack identity context
    # don't silently no-op; the plugin can still match by anon key on
    # turns where the same shape applies.
    from .. import user_tier_prefs as _utp

    user_key = _utp.derive_user_key_for_caller(
        network,
        bot_id=bot_id,
        channel=channel,
        sender_external_id=sender_external_id,
    )

    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    try:
        _utp.set_user_pref(shared_dir, bot_id, user_key, choice)
    except (OSError, ValueError) as e:
        return speak("tier-default", (
            f"Couldn't write the default tier: {e}. "
            "Try again, or ask the pod admin to set it from AI "
            "Optimization."
        ), role)

    # Audit log entry — same action / source convention as Phase B so
    # heal.py's drift detector and operator reconstruction logic stays
    # uniform. Empty oc_keys because user-tier-prefs.json is not in
    # openclaw.json or evolve-tiers.json (heal's drift detector reads
    # that field to know which on-disk files to watch).
    try:
        from evolve_admin.web.server import _audit_log_entry  # type: ignore
        _audit_log_entry(
            "config.user_tier_pref.set", bot_id,
            {
                "defaultTier": choice,
                "source": "evo_keyword",
                "user_key": user_key,
            },
            oc_keys=set(),
        )
    except Exception:
        # Best-effort audit — don't fail the user's command on log error.
        pass

    return speak(
        "tier-default", _success_body_default(choice, {}), role,
    )
