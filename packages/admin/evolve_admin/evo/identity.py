"""Resolve a calling user's role (primary vs secondary) for a given bot.

Primary identity is recorded in ``network.json`` per bot:

.. code-block:: jsonc

    {
      "bots": {
        "team_bot_a": {
          "primary_user": {
            "pod_user": "pod_admin_user",
            "name": "Pod_admin",                      // optional display name
            "external_ids": {
              "slack":    "U123ABC",
              "telegram": "4567890",
              "discord":  "987654321"
            }
          },
          "primary_passphrase": "newton"          // optional override of
                                                   // pod.primary_passphrase
        }
      },
      "pod": {
        "admin_passphrase":   "charles",
        "primary_passphrase": "darwin",
        "admins": {
          "external_ids": { "telegram": ["456"] },
          "pod_users":    ["pod_admin_user"],
          "names":        { "pod_admin_user": "Pod_admin" } // optional, keyed by pod_user
        }
      }
    }

Recording the primary at setup time is a follow-up PR
(``setup_wizard.py`` capture); until that lands, ``resolve_role`` falls back
to ``"primary"`` for every sender. This preserves existing behavior — bare
``evo`` continues to deliver Better Engine recommendations to whoever types
it — and only the new subcommands gain role-aware behavior once the wizard
captures identity.

The arbiter already has a ``bot_primary_user`` audience in
``packages/analyzer/arbiter/routing.py`` but no actual user-identity mapping;
this module is the missing piece, sized to v1's needs and intentionally not
trying to backfill the arbiter's full needs.
"""

from __future__ import annotations

from typing import Any, Literal


Role = Literal["admin", "primary", "secondary"]


def resolve_role(
    network: dict[str, Any],
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
) -> Role:
    """Return the caller's effective role for ``bot_id``.

    Priority order (highest first):

      1. ``"admin"`` — sender's external_id appears in pod-level
         ``pod.admins.external_ids[channel]``. Admin is pod-wide (claimed
         once, applies to every bot) and is a strict superset of primary
         permissions.
      2. ``"primary"`` — sender's external_id matches the bot's recorded
         primary, OR no primary is recorded yet (v1 fallback so an
         unconfigured bot doesn't hide commands from its owner).
      3. ``"secondary"`` — sender's external_id is recorded for *some
         channel* on this bot's primary but doesn't match this channel's
         entry; or a primary is recorded for this channel and the sender
         doesn't match. On single-user bots (``multiUser=false``) this
         tier is rarely reached — we prefer the primary fallback.

    All v1 fallbacks remain generous: missing data resolves toward
    primary, not toward secondary. Tightens as identity capture rolls
    out further.
    """
    if _is_pod_admin(network, channel, sender_external_id):
        return "admin"

    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    primary_block = bot_cfg.get("primary_user")
    if not isinstance(primary_block, dict):
        return "primary"

    external_ids = primary_block.get("external_ids") or {}
    if not isinstance(external_ids, dict) or not channel:
        return "primary"

    recorded = external_ids.get(channel)
    if recorded is None or sender_external_id is None:
        return "primary"

    return "primary" if str(recorded) == str(sender_external_id) else "secondary"


def _is_pod_admin(
    network: dict[str, Any],
    channel: str | None,
    sender_external_id: str | None,
) -> bool:
    """True if (channel, sender_external_id) appears in pod.admins."""
    if not channel or sender_external_id is None:
        return False
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return False
    admins = pod.get("admins") or {}
    if not isinstance(admins, dict):
        return False
    by_channel = admins.get("external_ids") or {}
    if not isinstance(by_channel, dict):
        return False
    ids = by_channel.get(channel)
    if not isinstance(ids, list):
        return False
    needle = str(sender_external_id)
    return any(str(x) == needle for x in ids)


def is_admin(
    network: dict[str, Any],
    channel: str | None,
    sender_external_id: str | None,
) -> bool:
    """Public wrapper of :func:`_is_pod_admin` — exported because
    callers (dispatch, CLI, future admin-UI) want to check admin status
    without conflating it with per-bot primary resolution."""
    return _is_pod_admin(network, channel, sender_external_id)


def primary_external_ids(
    network: dict[str, Any],
    bot_id: str,
) -> dict[str, str]:
    """Return the ``external_ids`` dict for a bot's primary, or ``{}``."""
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    primary_block = bot_cfg.get("primary_user")
    if not isinstance(primary_block, dict):
        return {}
    ids = primary_block.get("external_ids") or {}
    return {k: str(v) for k, v in ids.items() if isinstance(k, str)}


def primary_pod_user(
    network: dict[str, Any],
    bot_id: str,
) -> str | None:
    """Return the pod-level user identifier for a bot's primary, if recorded."""
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    primary_block = bot_cfg.get("primary_user")
    if not isinstance(primary_block, dict):
        return None
    pod_user = primary_block.get("pod_user")
    return pod_user if isinstance(pod_user, str) and pod_user else None


def primary_name(network: dict[str, Any], bot_id: str) -> str | None:
    """Return the display name for a bot's primary, when recorded.
    Lives alongside ``pod_user`` and ``external_ids`` in the same
    ``primary_user`` block.
    """
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    primary_block = bot_cfg.get("primary_user")
    if not isinstance(primary_block, dict):
        return None
    name = primary_block.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def set_primary_name(
    network: dict[str, Any], bot_id: str, name: str | None,
) -> None:
    """Set or clear a bot's primary display name. ``name=None`` (or
    empty) removes the field. Requires a recorded primary_user block;
    refuses otherwise so we don't accumulate orphan name fields on
    bots without a primary.
    """
    if not bot_id or not isinstance(bot_id, str):
        raise ClaimError("bot_id is required")
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id)
    if not isinstance(bot_cfg, dict):
        raise ClaimError(
            f"bot {bot_id!r} has no config block — record a primary "
            "via claim_primary() first"
        )
    primary_block = bot_cfg.get("primary_user")
    if not isinstance(primary_block, dict):
        raise ClaimError(
            f"bot {bot_id!r} has no primary_user — record one via "
            "claim_primary() first"
        )
    if name and isinstance(name, str) and name.strip():
        primary_block["name"] = name.strip()
    elif "name" in primary_block:
        del primary_block["name"]


def derive_user_key(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
) -> str:
    """Return a stable per-(bot, user) key suitable for naming wizard
    state files and user profiles.

    Preference order:
      1. ``pod:<pod_user>`` when this caller is the recorded primary AND
         a ``pod_user`` is on file — survives the user moving to a new
         channel later. Same lookup applies for admins who happen to
         have a pod_user recorded (admin pod_users live in
         ``pod.admins.pod_users``).
      2. ``ext:<channel>:<external_id>`` when we have both — stable per
         channel, the v1 default for secondaries (and for primaries /
         admins without a recorded pod_user)
      3. ``anon:<bot_id>`` fallback — happens when neither channel nor
         sender are known (rare; admin-driven CLI runs etc.). Reuses the
         bot id so multiple anon entries don't collide across bots.
    """
    role = resolve_role(network, bot_id, channel, sender_external_id)
    if role == "primary":
        pod_user = primary_pod_user(network, bot_id)
        if pod_user:
            return f"pod:{pod_user}"
    if role == "admin":
        # Admin pod_user lookup — if exactly one admin is recorded with a
        # pod_user we use it; otherwise we can't safely disambiguate by
        # channel alone, so fall through to ext: keying.
        pod = network.get("pod") or {}
        admins = (pod.get("admins") or {}) if isinstance(pod, dict) else {}
        pod_users = admins.get("pod_users") or [] if isinstance(admins, dict) else []
        if isinstance(pod_users, list) and len(pod_users) == 1:
            return f"pod:{pod_users[0]}"
    if channel and sender_external_id:
        return f"ext:{channel}:{sender_external_id}"
    return f"anon:{bot_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Pod admin claim writer
# ─────────────────────────────────────────────────────────────────────────────


def claim_admin(
    network: dict[str, Any],
    *,
    channel: str,
    external_id: str,
    pod_user: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Add ``(channel, external_id)`` to ``pod.admins.external_ids`` and
    optionally record ``pod_user`` in ``pod.admins.pod_users``. Idempotent —
    re-claiming the same pair is a no-op. Returns the updated admins
    block.

    When ``name`` is provided AND ``pod_user`` is provided, the name is
    stored in ``pod.admins.names[pod_user]``. Names exist only when an
    admin has an associated pod_user — that's the join key. (No
    pod_user → no place to hang the name; pass pod_user alongside it.)

    Caller persists via :func:`evolve_admin.config.save_network`.
    """
    if not channel or not isinstance(channel, str):
        raise ClaimError("channel is required")
    channel = channel.strip().lower()
    if not channel:
        raise ClaimError("channel must not be empty after stripping")

    if not external_id or not isinstance(external_id, str):
        raise ClaimError("external_id is required (non-empty string)")
    external_id = external_id.strip()
    if not external_id:
        raise ClaimError("external_id must not be empty after stripping")

    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    admins = pod.setdefault("admins", {})
    if not isinstance(admins, dict):
        admins = {}
        pod["admins"] = admins

    by_channel = admins.setdefault("external_ids", {})
    if not isinstance(by_channel, dict):
        by_channel = {}
        admins["external_ids"] = by_channel

    ids = by_channel.setdefault(channel, [])
    if not isinstance(ids, list):
        ids = []
        by_channel[channel] = ids
    if external_id not in ids:
        ids.append(external_id)

    if pod_user:
        pod_users = admins.setdefault("pod_users", [])
        if not isinstance(pod_users, list):
            pod_users = []
            admins["pod_users"] = pod_users
        if pod_user not in pod_users:
            pod_users.append(pod_user)
        # Name attaches to the pod_user — only when both are present.
        if name and isinstance(name, str) and name.strip():
            names = admins.setdefault("names", {})
            if not isinstance(names, dict):
                names = {}
                admins["names"] = names
            names[pod_user] = name.strip()

    return admins


def revoke_admin(
    network: dict[str, Any],
    *,
    channel: str,
    external_id: str,
    drop_pod_user: str | None = None,
) -> bool:
    """Remove an admin's ``(channel, external_id)`` pair from
    ``pod.admins.external_ids``. Idempotent: returns True if a removal
    happened, False when the entry was already absent.

    When ``drop_pod_user`` is set, ALSO removes that pod_user from the
    ``pod_users`` list and its entry from ``names`` — use this when
    you're revoking the *whole person*, not just one of their
    channels. Skip ``drop_pod_user`` when the admin keeps other channel
    identities you don't want to forget.

    Caller persists via :func:`evolve_admin.config.save_network`.
    """
    if not channel or not isinstance(channel, str):
        raise ClaimError("channel is required")
    channel = channel.strip().lower()
    if not external_id or not isinstance(external_id, str):
        raise ClaimError("external_id is required (non-empty string)")
    external_id = external_id.strip()

    pod = network.get("pod")
    if not isinstance(pod, dict):
        return False
    admins = pod.get("admins")
    if not isinstance(admins, dict):
        return False
    by_channel = admins.get("external_ids")
    if not isinstance(by_channel, dict):
        return False
    ids = by_channel.get(channel)
    if not isinstance(ids, list) or external_id not in ids:
        # External-id wasn't present; still try to drop the pod_user if
        # asked — operator may be cleaning up a half-removed admin.
        removed_any = False
    else:
        ids.remove(external_id)
        removed_any = True

    if drop_pod_user:
        pod_users = admins.get("pod_users")
        if isinstance(pod_users, list) and drop_pod_user in pod_users:
            pod_users.remove(drop_pod_user)
            removed_any = True
        names = admins.get("names")
        if isinstance(names, dict) and drop_pod_user in names:
            del names[drop_pod_user]
            removed_any = True

    return removed_any


def admin_names(network: dict[str, Any]) -> dict[str, str]:
    """Return the ``{pod_user: name}`` mapping from ``pod.admins.names``,
    or ``{}`` when none recorded.
    """
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return {}
    admins = pod.get("admins") or {}
    if not isinstance(admins, dict):
        return {}
    names = admins.get("names") or {}
    if not isinstance(names, dict):
        return {}
    return {
        k: str(v) for k, v in names.items()
        if isinstance(k, str) and isinstance(v, str) and v
    }


def admin_name_for(network: dict[str, Any], pod_user: str) -> str | None:
    """Look up one admin's display name by pod_user. Returns ``None``
    when no name is recorded for them.
    """
    if not pod_user:
        return None
    return admin_names(network).get(pod_user)


def set_admin_name(
    network: dict[str, Any], pod_user: str, name: str | None,
) -> None:
    """Set or clear an admin's display name. ``name=None`` (or empty)
    removes the entry. Requires the pod_user to exist in the admins
    list — refuses to record a name for a non-admin pod_user (catches
    typos).
    """
    if not pod_user or not isinstance(pod_user, str):
        raise ClaimError("pod_user is required")
    pod_user = pod_user.strip()
    if not pod_user:
        raise ClaimError("pod_user must not be empty after stripping")

    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    admins = pod.setdefault("admins", {})
    if not isinstance(admins, dict):
        admins = {}
        pod["admins"] = admins
    pod_users = admins.get("pod_users") or []
    if not isinstance(pod_users, list) or pod_user not in pod_users:
        raise ClaimError(
            f"pod_user {pod_user!r} is not a recorded admin — "
            "claim them first via claim_admin(pod_user=...)"
        )

    names = admins.setdefault("names", {})
    if not isinstance(names, dict):
        names = {}
        admins["names"] = names

    if name and isinstance(name, str) and name.strip():
        names[pod_user] = name.strip()
    elif pod_user in names:
        del names[pod_user]


# ─────────────────────────────────────────────────────────────────────────────
# Passphrase helpers
# ─────────────────────────────────────────────────────────────────────────────


def _normalized_passphrase(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    return s or None


def admin_passphrase(network: dict[str, Any]) -> str | None:
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return None
    return _normalized_passphrase(pod.get("admin_passphrase"))


def primary_passphrase(network: dict[str, Any]) -> str | None:
    """Pod-wide default primary passphrase. Per-bot overrides live on
    the bot's config — see :func:`primary_passphrase_for_bot` for the
    resolution-aware lookup."""
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return None
    return _normalized_passphrase(pod.get("primary_passphrase"))


def bot_primary_passphrase_override(
    network: dict[str, Any], bot_id: str,
) -> str | None:
    """The per-bot override at ``bots.<id>.primary_passphrase``, or
    ``None`` when the bot inherits the pod default. Useful for the
    UI when distinguishing "inherits" from "custom".
    """
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    if not isinstance(bot_cfg, dict):
        return None
    return _normalized_passphrase(bot_cfg.get("primary_passphrase"))


def primary_passphrase_for_bot(
    network: dict[str, Any], bot_id: str,
) -> str | None:
    """Resolution-aware primary passphrase: per-bot override → pod
    default → None. Use this in the claim-flow matcher so a per-bot
    custom phrase (set via ``set_bot_primary_passphrase``) takes
    precedence over the pod default.
    """
    override = bot_primary_passphrase_override(network, bot_id)
    if override is not None:
        return override
    return primary_passphrase(network)


def matches_admin_passphrase(network: dict[str, Any], token: str) -> bool:
    expected = admin_passphrase(network)
    if expected is None or not token:
        return False
    return token.strip().lower() == expected


def matches_primary_passphrase(network: dict[str, Any], token: str) -> bool:
    """Match against the pod-wide primary passphrase. For per-bot
    matching (e.g. inside an ``evo claim X`` flow on a specific bot),
    use :func:`matches_primary_passphrase_for_bot` instead.
    """
    expected = primary_passphrase(network)
    if expected is None or not token:
        return False
    return token.strip().lower() == expected


def matches_primary_passphrase_for_bot(
    network: dict[str, Any], bot_id: str, token: str,
) -> bool:
    """Bot-aware primary-passphrase match. Returns True when ``token``
    matches the per-bot override OR (when no override is set) the pod
    default. This is the right matcher for the ``evo claim X`` flow.
    """
    expected = primary_passphrase_for_bot(network, bot_id)
    if expected is None or not token:
        return False
    return token.strip().lower() == expected


def set_pod_admin_passphrase(
    network: dict[str, Any], passphrase: str | None,
) -> None:
    """Set or clear the pod-wide admin passphrase.

    Passing ``None`` (or empty) removes the field — claim_admin via
    passphrase becomes impossible until a new one is set, but already-
    recorded admins keep their access. The default of "charles" is
    re-applied automatically on the next network.json load via the
    config module's ``_apply_pod_defaults`` backfill, so callers who
    want to disable passphrase-claim entirely should also set the
    config-file value to a non-default sentinel rather than rely on
    deletion.
    """
    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    if passphrase and isinstance(passphrase, str) and passphrase.strip():
        pod["admin_passphrase"] = passphrase.strip()
    elif "admin_passphrase" in pod:
        del pod["admin_passphrase"]


def set_pod_primary_passphrase(
    network: dict[str, Any], passphrase: str | None,
) -> None:
    """Set or clear the pod-wide default primary passphrase. Same
    backfill caveat as :func:`set_pod_admin_passphrase`."""
    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    if passphrase and isinstance(passphrase, str) and passphrase.strip():
        pod["primary_passphrase"] = passphrase.strip()
    elif "primary_passphrase" in pod:
        del pod["primary_passphrase"]


def set_bot_primary_passphrase(
    network: dict[str, Any], bot_id: str, passphrase: str | None,
) -> None:
    """Set or clear a per-bot primary-passphrase override.

    ``passphrase=None`` (or empty) removes the override so the bot
    inherits the pod default again. We omit the field on clear (rather
    than writing an explicit null) — fewer states, matches the
    inherit-by-default convention used elsewhere in network.json.
    """
    if not bot_id or not isinstance(bot_id, str):
        raise ClaimError("bot_id is required")
    members = network.get("members") or []
    if bot_id not in members:
        raise ClaimError(
            f"bot {bot_id!r} is not a pod member — passphrase override "
            f"aborted (network.members = {sorted(members)})"
        )
    bots = network.setdefault("bots", {})
    bot_cfg = bots.setdefault(bot_id, {})
    if not isinstance(bot_cfg, dict):
        bot_cfg = {}
        bots[bot_id] = bot_cfg
    if passphrase and isinstance(passphrase, str) and passphrase.strip():
        bot_cfg["primary_passphrase"] = passphrase.strip()
    elif "primary_passphrase" in bot_cfg:
        del bot_cfg["primary_passphrase"]


# ─────────────────────────────────────────────────────────────────────────────
# Writer
# ─────────────────────────────────────────────────────────────────────────────


class ClaimError(ValueError):
    """Raised when a primary-user claim cannot be applied (validation or
    conflict with an existing recorded value)."""


def claim_primary(
    network: dict[str, Any],
    bot_id: str,
    channel: str,
    external_id: str,
    *,
    pod_user: str | None = None,
    name: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Mutate ``network`` in place to record the primary user's external ID
    on ``channel`` for ``bot_id``. Return the updated ``primary_user`` block.

    The bot must already be a pod member (``network["members"]``); we don't
    invent bots here. The ``bots[bot_id]`` block is created if missing.

    ``name`` is an optional display name stored alongside ``pod_user`` and
    ``external_ids`` in the primary_user block. Re-claiming with a new name
    OVERWRITES the previous value — that's the right behavior for fixing
    typos; if a non-overwrite update is wanted, call
    :func:`set_primary_name` separately.

    Idempotent for re-claiming the same value. Refuses to overwrite a
    different existing value on the same channel unless ``force=True``.

    Caller is responsible for persisting the mutated ``network`` via
    :func:`evolve_admin.config.save_network`.
    """
    members = network.get("members") or []
    if bot_id not in members:
        raise ClaimError(
            f"bot {bot_id!r} is not a pod member — claim aborted "
            f"(network.members = {sorted(members)})"
        )

    if not channel or not isinstance(channel, str):
        raise ClaimError("channel is required")
    channel = channel.strip().lower()
    if not channel:
        raise ClaimError("channel must not be empty after stripping")

    if not external_id or not isinstance(external_id, str):
        raise ClaimError("external_id is required (non-empty string)")
    external_id = external_id.strip()
    if not external_id:
        raise ClaimError("external_id must not be empty after stripping")

    bots = network.setdefault("bots", {})
    bot_cfg = bots.setdefault(bot_id, {})
    primary_block = bot_cfg.setdefault("primary_user", {})
    if not isinstance(primary_block, dict):
        # Pre-existing non-dict primary_user is unexpected; replace.
        primary_block = {}
        bot_cfg["primary_user"] = primary_block

    external_ids = primary_block.setdefault("external_ids", {})
    if not isinstance(external_ids, dict):
        external_ids = {}
        primary_block["external_ids"] = external_ids

    existing = external_ids.get(channel)
    if existing is not None and str(existing) != external_id and not force:
        raise ClaimError(
            f"primary for {bot_id!r} on channel {channel!r} is already "
            f"recorded as {str(existing)!r}; pass force=True to overwrite"
        )

    external_ids[channel] = external_id
    if pod_user:
        primary_block["pod_user"] = pod_user
    if name and isinstance(name, str) and name.strip():
        primary_block["name"] = name.strip()

    return primary_block


def clear_primary(network: dict[str, Any], bot_id: str) -> bool:
    """Remove the entire ``primary_user`` block on a bot.

    Returns True when something was actually removed, False when the
    bot had no primary recorded. Use this when the operator wants to
    fully reset the owner — e.g. before transferring the bot to a new
    person. Distinct from ``reassign_primary`` (which requires you to
    know the FROM value); ``clear_primary`` is the no-knowledge reset.

    Caller persists via :func:`evolve_admin.config.save_network`.
    """
    if not bot_id or not isinstance(bot_id, str):
        raise ClaimError("bot_id is required")
    bots = network.get("bots") or {}
    bot_cfg = bots.get(bot_id)
    if not isinstance(bot_cfg, dict):
        return False
    if "primary_user" not in bot_cfg:
        return False
    del bot_cfg["primary_user"]
    return True


def reassign_primary(
    network: dict[str, Any],
    bot_id: str,
    channel: str,
    *,
    from_external_id: str,
    to_external_id: str,
    pod_user: str | None = None,
) -> dict[str, Any]:
    """Reassign an existing primary on ``channel`` from ``from_external_id``
    to ``to_external_id``. Verifies the current value matches
    ``from_external_id`` before mutating — refuses to proceed otherwise so
    operators can't fat-finger a reassignment based on stale assumptions
    about the current state.

    Returns the updated ``primary_user`` block.

    Raises :class:`ClaimError` if:
      * the bot isn't a pod member,
      * inputs are empty,
      * no primary is currently recorded on this channel,
      * the recorded value doesn't match ``from_external_id``.

    Idempotent for ``from == to`` and the value already equals ``to``: it
    simply returns the existing block without mutation. (Caller should
    detect the no-op case if they want to skip an audit write.)
    """
    members = network.get("members") or []
    if bot_id not in members:
        raise ClaimError(
            f"bot {bot_id!r} is not a pod member — reassign aborted "
            f"(network.members = {sorted(members)})"
        )

    if not channel or not isinstance(channel, str):
        raise ClaimError("channel is required")
    channel = channel.strip().lower()
    if not channel:
        raise ClaimError("channel must not be empty after stripping")

    if not from_external_id or not isinstance(from_external_id, str):
        raise ClaimError("from_external_id is required (non-empty string)")
    from_external_id = from_external_id.strip()
    if not to_external_id or not isinstance(to_external_id, str):
        raise ClaimError("to_external_id is required (non-empty string)")
    to_external_id = to_external_id.strip()
    if not from_external_id or not to_external_id:
        raise ClaimError("from/to external_ids must not be empty after stripping")

    bots = network.setdefault("bots", {})
    bot_cfg = bots.setdefault(bot_id, {})
    primary_block = bot_cfg.setdefault("primary_user", {})
    if not isinstance(primary_block, dict):
        primary_block = {}
        bot_cfg["primary_user"] = primary_block

    external_ids = primary_block.setdefault("external_ids", {})
    if not isinstance(external_ids, dict):
        external_ids = {}
        primary_block["external_ids"] = external_ids

    current = external_ids.get(channel)
    if current is None:
        raise ClaimError(
            f"no primary is currently recorded for {bot_id!r} on channel "
            f"{channel!r} — use claim-primary to set one"
        )
    if str(current) != from_external_id:
        raise ClaimError(
            f"current primary for {bot_id!r} on channel {channel!r} is "
            f"{str(current)!r}, not {from_external_id!r} — reassign aborted "
            "(check `evo show-identity` for current state)"
        )

    external_ids[channel] = to_external_id
    if pod_user:
        primary_block["pod_user"] = pod_user

    return primary_block
