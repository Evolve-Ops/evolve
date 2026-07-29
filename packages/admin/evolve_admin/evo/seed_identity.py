"""Atomic, idempotent coherence primitive: seed a (bot, channel, external_id)
identity across all three stores that must agree for a bot to actually talk to
its owner.

Motivation
----------
When an operator supplies *their own* messaging id during their own pod setup,
that is the highest-trust identity signal in the whole flow. Yet historically
the add-bot path captured the operator's Telegram chat id, used it for a
throwaway test message + the alerts-chat default, and then *dropped* it. Three
downstream facts that should flow from that id never got written:

  1. the bot's owner / ``primary_user.external_ids.<channel>``  → Users page
     showed "no owner recorded";
  2. the chat id where the Credentials UI reads it  → "Chat ID: —";
  3. a DM approval (``allowFrom`` entry)  → the bot was mute (DM(0)) and the
     setup checklist item "Messaging channel paired" stayed Pending.

``seed_channel_identity`` performs all three writes for one
(bot, channel, external_id) tuple, each independently idempotent, tolerant of
partial prior state (e.g. owner already set but no allowFrom).

Where chat_id is written — and why NOT ``channels.<ch>.chatId``
-------------------------------------------------------------
The screenshot label says "stored in openclaw.json · channels.telegram", and
the runtime-mirror registry holds ``channels.telegram.botToken``. But OC's
config schema declares ``channels.telegram`` with ``additionalProperties:
false`` and **no ``chatId`` key** — writing ``channels.telegram.chatId`` via
``safe_write_bot_config`` is rejected by validation, and writing it raw would
poison every *future* validated config write for that bot (a latent
crash-loop). The Credentials UI's other reader, ``AuthProfilesTokenPairProbe``,
reads each token_pair field (``bot_token`` / ``chat_id``) straight off the
provider's profile dict in ``auth-profiles.json`` — a file Evolve owns and OC's
*channel* schema does not gate. That probe is the **first-checked winner** in
the token_pair cascade (auth-profiles > openclaw_channels > dotenv), and it
matches on ANY active declared field — including ``chat_id`` alone.

That last property is a trap: on a pod whose ``bot_token`` lives in
``openclaw.json#channels.<ch>`` (the common live shape — gateways read it
there) and NOT in auth-profiles, seeding a chat_id-only auth-profiles entry
would make ``AuthProfilesTokenPairProbe`` win the cascade and render the live
bot_token as **missing** — a regression that makes a working bot look
disconnected. So the chat_id write is **gated**: we write it to auth-profiles
ONLY when the bot_token already resolves from auth-profiles (so that probe is
the rightful winner anyway — chat_id is then co-located and rotate-safe). When
the token is in channels.<ch>, the chat_id write is SKIPPED. chat_id is
cosmetic for Telegram — OC carries the chat on each inbound message (no chatId
runtime config) and the field self-populates from the first inbound message —
so the field merely stays "—" rather than splitting the cascade. The
functional fix (owner + DM approval) is unaffected by this gate. See
``_token_pair_lives_in_auth_profiles``.

All three writes share the bot-owned-file rules from CLAUDE.md: reads are direct
(evolve has the ACL), writes go through /tmp staging + ``sudo /bin/cp`` + chown
(the ``evolve`` user cannot ``sudo -u <bot>``). We reuse the existing writers
rather than reinvent them:

  * owner      → :func:`evolve_admin.evo.identity.claim_primary` (caller persists
                 network.json via ``save_network``)
  * chat_id    → ``auth-profiles.json`` ``<channel>:token_pair`` profile, via the
                 shared ``_read_auth_profiles`` / ``_write_auth_profiles`` helpers
  * allowFrom  → ``<bot_home>/.openclaw/credentials/<channel>-default-allowFrom.json``
                 via the same ``_write_bot_json`` path ``_approve`` uses
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity import ClaimError, claim_primary


@dataclass
class SeedResult:
    """What ``seed_channel_identity`` actually changed.

    Each ``*_written`` flag is True only when that store was mutated; a
    re-run with the same id leaves all flags False (pure no-op). ``errors``
    collects non-fatal per-step failures so one broken store doesn't sink
    the other two — the caller decides whether to surface or ignore.
    """

    bot_id: str
    channel: str
    external_id: str
    owner_written: bool = False
    chat_id_written: bool = False
    allow_from_written: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.owner_written or self.chat_id_written or self.allow_from_written

    @property
    def ok(self) -> bool:
        return not self.errors


def seed_channel_identity(
    network: dict[str, Any],
    bot_id: str,
    channel: str,
    external_id: str,
    *,
    network_path: Path,
    display_name: str | None = None,
    write_chat_id: bool = True,
) -> SeedResult:
    """Seed owner + chat_id + DM approval for one (bot, channel, external_id).

    Idempotent and tolerant of partial prior state. Mutates ``network`` in
    place for the owner record; the caller is responsible for persisting it
    via :func:`evolve_admin.config.save_network` (we do NOT save here so the
    add-bot flow can batch one network write). The chat_id and allowFrom
    writes hit bot-owned files directly and are persisted immediately.

    Args:
      network:        loaded network.json dict (mutated in place for owner).
      bot_id:         pod member id (must already be in ``network["members"]``).
      channel:        messaging channel, e.g. ``"telegram"``.
      external_id:    the channel-native user/chat id (operator's own id).
      network_path:   path to network.json (needed by the bot-file helpers to
                      resolve the bot's home / user).
      display_name:   optional human name to record on the primary_user block.
      write_chat_id:  set False to skip the chat_id store write (e.g. when the
                      caller already wrote the credential through another path).

    Returns a :class:`SeedResult`. Never raises for a per-store failure —
    those land in ``result.errors``; only a programming error (bad arg type)
    propagates.
    """
    result = SeedResult(bot_id=bot_id, channel=channel, external_id=str(external_id))

    channel = (channel or "").strip().lower()
    external_id = str(external_id or "").strip()
    if not bot_id or not channel or not external_id:
        result.errors.append(
            "seed_channel_identity: bot_id, channel, and external_id are all required"
        )
        return result

    # ── 1. Owner / primary_user (idempotent via claim_primary) ────────────────
    # claim_primary is a no-op when the same id is already recorded on this
    # channel; it raises ClaimError on a *different* recorded id (no force) or
    # when the bot isn't a member. We treat "already recorded as me" as success
    # and surface a genuine conflict as an error rather than clobbering.
    bots = network.get("bots") or {}
    existing_ext = (
        ((bots.get(bot_id) or {}).get("primary_user") or {}).get("external_ids") or {}
    )
    already_owner = str(existing_ext.get(channel) or "") == external_id
    try:
        claim_primary(
            network, bot_id, channel, external_id,
            name=display_name if (display_name and display_name.strip()) else None,
        )
        # Mutation happened iff it wasn't already the recorded value. (A name
        # update on an already-owner record is a benign re-write; treat the
        # external-id match as the idempotency anchor.)
        result.owner_written = not already_owner
    except ClaimError as e:
        result.errors.append(f"owner: {e}")

    # ── 2. chat_id → auth-profiles.json <channel>:token_pair profile ─────────-
    # CASCADE-SAFETY GATE: the Credentials dashboard renders the messaging row
    # from a probe cascade where AuthProfilesTokenPairProbe (auth-profiles) is
    # checked BEFORE OpenclawChannelsTokenProbe (openclaw.json#channels) and
    # the FIRST match unconditionally wins (routes_admin.py token_pair branch:
    # `if matches: winner = matches[0]`). AuthProfilesTokenPairProbe matches on
    # ANY active declared field — including chat_id alone. So seeding a
    # chat_id-only auth-profiles entry on a pod whose bot_token lives in
    # channels.<ch> (the common live-pod shape) would make the auth-profiles
    # probe win the cascade and render the live bot_token as **missing** — a
    # regression of the token row (the bot looks disconnected). We therefore
    # only write chat_id into auth-profiles when the bot_token ALSO resolves
    # from auth-profiles (so that probe is already the rightful winner). When
    # the token is in channels.<ch>, we SKIP the chat_id write — chat_id is
    # cosmetic for Telegram (OC carries the chat on each inbound message; there
    # is no chatId runtime config) and also self-populates from the first
    # inbound message, so the field simply stays "—" rather than splitting the
    # cascade and blanking the token. The owner + DM-approval writes (the
    # functional fix) are unaffected.
    if write_chat_id:
        try:
            if _token_pair_lives_in_auth_profiles(
                bot_id, channel, network_path=network_path
            ):
                result.chat_id_written = _seed_chat_id_auth_profile(
                    bot_id, channel, external_id, network_path=network_path,
                )
            # else: token is in channels.<ch> (or nowhere yet) — writing
            # chat_id to auth-profiles would steal the cascade. Skip it;
            # chat_id_written stays False (a deliberate, documented no-op).
        except Exception as e:  # noqa: BLE001 — never let one store sink the rest
            result.errors.append(f"chat_id: {e}")

    # ── 3. allowFrom (DM auto-approval for the operator's own id) ─────────────
    try:
        result.allow_from_written = _seed_allow_from(
            network, bot_id, channel, external_id,
        )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"allowFrom: {e}")

    return result


def _token_pair_lives_in_auth_profiles(
    bot_id: str, channel: str, *, network_path: Path,
) -> bool:
    """Does the bot's secret token for ``channel`` resolve from auth-profiles?

    This is the cascade-safety predicate (see the long comment at the chat_id
    write site). It returns True iff a ``token_pair`` profile for ``channel``
    in ``auth-profiles.json`` already carries a non-empty *secret* field
    (``bot_token`` for telegram; ``bot_token`` / ``app_token`` / ``user_token``
    for slack). In that case ``AuthProfilesTokenPairProbe`` is already the
    rightful cascade winner, so co-locating chat_id there is safe and desired.

    Returns False when the token is absent from auth-profiles — including the
    common live-pod shape where it lives in ``openclaw.json#channels.<ch>`` and
    the fresh-wizard shape where it was just written to channels.<ch>. In those
    cases writing a chat_id-only auth-profiles entry would make the
    auth-profiles probe win the cascade and blank the (channels-stored) token
    row, so the caller skips the chat_id write.

    Conservative on read failure: any exception → False (skip the write rather
    than risk the regression). We never write the token here, so a false
    negative only costs a cosmetic "—" chat_id; a false positive would risk the
    token row.
    """
    try:
        from ..web.probes import OPENCLAW_CHANNELS_FIELDS
        from ..web.routes_admin_shared import _read_auth_profiles
    except Exception:
        return False

    # The secret field keys for this channel (the ones whose presence makes the
    # auth-profiles row "active" for a token). Fall back to {"bot_token"} for
    # channels not in the map.
    spec = OPENCLAW_CHANNELS_FIELDS.get(channel)
    secret_keys = (
        {fk for fk, _oc, secret in spec if secret} if spec else {"bot_token"}
    )

    try:
        data = _read_auth_profiles(bot_id, network_path=network_path)
    except Exception:
        return False
    profiles = (data or {}).get("profiles")
    if not isinstance(profiles, dict):
        return False

    for pdata in profiles.values():
        if not isinstance(pdata, dict):
            continue
        if str(pdata.get("provider") or "").strip().lower() != channel:
            continue
        if str(pdata.get("type") or "") != "token_pair":
            continue
        for sk in secret_keys:
            if str(pdata.get(sk) or "").strip():
                return True
    return False


def _seed_chat_id_auth_profile(
    bot_id: str, channel: str, external_id: str, *, network_path: Path,
) -> bool:
    """Write ``chat_id`` into the bot's ``<channel>:token_pair`` auth profile.

    Returns True if the file was mutated, False when the chat_id was already
    recorded (idempotent). Uses the SAME shared read/write helpers the
    Credentials rotate endpoint uses, so the value the display reads and the
    value rotation maintains are one and the same field.
    """
    from ..web.routes_admin_shared import (
        _read_auth_profiles,
        _write_auth_profiles,
    )

    data = _read_auth_profiles(bot_id, network_path=network_path)
    if not data:
        # No auth-profiles.json yet (or unreadable) — synthesize the canonical
        # shape. The write helper resolves the correct on-disk path itself.
        data = {"profiles": {}}
    profiles = data.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        data["profiles"] = profiles

    profile_id = f"{channel}:token_pair"
    # Prefer an existing token_pair profile for this provider regardless of its
    # id, so we co-locate chat_id with a wizard/rotate-written bot_token rather
    # than spawning a second profile the probe would have to merge.
    target_id = None
    for pid, pdata in profiles.items():
        if (
            isinstance(pdata, dict)
            and str(pdata.get("provider") or "").strip().lower() == channel
            and str(pdata.get("type") or "") == "token_pair"
        ):
            target_id = pid
            break
    if target_id is None:
        target_id = profile_id

    pdata = profiles.get(target_id)
    if not isinstance(pdata, dict):
        pdata = {}
    if str(pdata.get("chat_id") or "") == external_id:
        return False  # idempotent: already recorded

    pdata["provider"] = channel
    pdata["type"] = "token_pair"
    pdata["chat_id"] = external_id
    profiles[target_id] = pdata
    data["profiles"] = profiles

    if not _write_auth_profiles(bot_id, data, network_path=network_path):
        raise RuntimeError("failed to write auth-profiles.json")
    return True


def _seed_allow_from(
    network: dict[str, Any], bot_id: str, channel: str, external_id: str,
) -> bool:
    """Add ``external_id`` to the bot's ``<channel>-default-allowFrom.json``.

    Reuses the exact write path ``_approve`` uses (``_write_bot_json`` →
    /tmp staging + ``sudo /bin/cp`` + chown). Idempotent: a no-op when the id
    is already present. Returns True if the file was mutated.
    """
    from ..config import get_bot_user
    from ..web.routes_bot_users import (
        _allowfrom_path,
        _read_json_or_default,
        _write_bot_json,
    )

    ap = _allowfrom_path(network, bot_id, channel)
    allow = _read_json_or_default(ap, {"version": 1, "allowFrom": []})
    allowed = list(allow.get("allowFrom") or [])
    if external_id in allowed:
        return False  # idempotent
    allowed.append(external_id)
    allow["allowFrom"] = allowed
    allow.setdefault("version", 1)
    bot_user = get_bot_user(bot_id, network)
    _write_bot_json(ap, allow, bot_user=bot_user)
    return True
