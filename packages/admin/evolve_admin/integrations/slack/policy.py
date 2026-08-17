"""Slack policy schema + storage.

Phase 2 of ``docs/spec-slack-policy-2026-05-13.md``. The policy file is
the source of truth for Slack admin intent; the writer renders it down
to ``openclaw.json``'s ``channels`` + ``messages`` + ``directMessages``
sections.

Storage location:
  ``{shared_dir}/bots/<bot_id>/slack-policy.json``

owned by the ``evolve`` user (under shared_dir, which already has the
right ACL — no /tmp + sudo dance needed). Atomic write via temp file +
``os.replace``, mirroring ``arbiter.store.write_proposal``'s pattern.

Schema is conservative — only the fields the Phase 2 writer + doctor
need are required. Fields the spec defines but no consumer yet exists
for (quiet hours, mention style, self-enrollment codes) are accepted
and round-tripped but Phase 2 doesn't act on them. This keeps the
JSON forward-compatible: a Phase 3 DM-flow handler can read those
fields once it lands without a schema migration.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

from evolve_util import now_iso


SCHEMA_VERSION = 1
POLICY_FILENAME = "slack-policy.json"


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class WorkspaceMeta:
    """The Slack workspace this policy applies to.

    ``team_id`` is authoritative; ``team_name`` is human-display only
    (Slack renames are common and the doctor refreshes it on each run).
    """
    team_id: str = ""
    team_name: str = ""


@dataclass
class DmAllowlist:
    """Who can DM the bot.

    Modes:

    - ``open`` — every workspace member can DM the bot (default for
      team bots). No allowlist written to openclaw.json.
    - ``user_group`` — members of a Slack user group. The writer
      resolves the group → IDs at render time via ``usergroups.users.list``.
    - ``explicit`` — a hand-curated list of Slack user IDs.
    - ``channel_derived`` — anyone who is a member of any of the named
      channels. Writer resolves at render time.

    The Phase 2 writer only implements ``open`` and ``explicit`` —
    ``user_group`` and ``channel_derived`` need a working SlackClient
    in the writer, which we defer to a follow-up so the writer stays
    deterministic in tests and offline-runnable.
    """
    mode: Literal["open", "user_group", "explicit", "channel_derived"] = "open"
    user_group_id: str | None = None
    user_ids: list[str] = field(default_factory=list)
    derived_from_channel_ids: list[str] = field(default_factory=list)


@dataclass
class SelfEnrollmentCode:
    """A one-time code an admin gives a user to self-enroll into the DM allowlist.

    Phase 2 stores these; Phase 3's evo DM handler consumes them.
    """
    code: str
    created_by: str
    created_at: str
    expires_at: str
    consumed: bool = False
    consumed_by: str | None = None
    consumed_at: str | None = None


@dataclass
class AccessPolicy:
    dm_allowlist: DmAllowlist = field(default_factory=DmAllowlist)
    self_enrollment_codes: list[SelfEnrollmentCode] = field(default_factory=list)


@dataclass
class PolicyChannelEntry:
    """One channel the bot listens in.

    ``channel_id`` is authoritative — always a Slack ID, never a name.
    The writer refuses to render entries whose key fails
    ``oc_config.is_slack_channel_id``. ``channel_name`` is cached for
    display; refreshed by the doctor on every run.
    """
    channel_id: str
    channel_name: str = ""
    channel_type: Literal["public", "private"] = "public"
    require_mention: bool = True
    visible_replies: str | None = None     # override messaging.visible_replies_default
    added_at: str = ""
    added_by: str = ""                     # actor — user_id or auto:<reason>


@dataclass
class DefaultForNew:
    """Behavior when the bot is invited to a channel not in entries.

    Phase 2 stores the preference; Phase 3's ``bot_invited_unwatched``
    Signal handler consumes it. Spec default is ``ask_admin``.
    """
    behavior: Literal[
        "ask_admin", "auto_add_mention_only", "auto_add_listen_all", "ignore",
    ] = "ask_admin"
    ask_admin_via: Literal["evo_dm"] = "evo_dm"


@dataclass
class ChannelsPolicy:
    default_for_new: DefaultForNew = field(default_factory=DefaultForNew)
    entries: list[PolicyChannelEntry] = field(default_factory=list)


@dataclass
class QuietHours:
    enabled: bool = False
    timezone: str = "UTC"
    start: str = "22:00"
    end: str = "07:00"
    paged_overrides: bool = True


@dataclass
class MessagingPolicy:
    visible_replies_default: str = "automatic"
    thread_replies: bool = True
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    mention_style: Literal["strict", "fuzzy"] = "strict"


@dataclass
class SlackPolicy:
    """Top-level policy doc."""
    bot_id: str
    schema_version: int = SCHEMA_VERSION
    workspace: WorkspaceMeta = field(default_factory=WorkspaceMeta)
    access: AccessPolicy = field(default_factory=AccessPolicy)
    channels: ChannelsPolicy = field(default_factory=ChannelsPolicy)
    messaging: MessagingPolicy = field(default_factory=MessagingPolicy)
    last_reconciled_at: str | None = None
    drift_signals: list[str] = field(default_factory=list)

    # ── Convenience ────────────────────────────────────────────────────────

    def channel_id_set(self) -> set[str]:
        return {e.channel_id for e in self.channels.entries}

    def find_channel(self, channel_id: str) -> PolicyChannelEntry | None:
        for entry in self.channels.entries:
            if entry.channel_id == channel_id:
                return entry
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


# Canonical enum values per spec §3. Policy fields are constrained at
# load time (strict — unknown values raise) and at validate time
# (defensive — a hand-edit that bypasses the loader still gets caught
# before a render).
ALLOWED_VISIBLE_REPLIES = ("automatic", "thread_only", "ephemeral")
ALLOWED_DM_MODES = ("open", "user_group", "explicit", "channel_derived")
ALLOWED_DEFAULT_FOR_NEW = (
    "ask_admin", "auto_add_mention_only", "auto_add_listen_all", "ignore",
)
ALLOWED_CHANNEL_TYPES = ("public", "private")
ALLOWED_MENTION_STYLES = ("strict", "fuzzy")


def validate_policy(policy: SlackPolicy) -> list[str]:
    """Return a list of validation errors. Empty list = valid.

    Validation is structural, not Slack-semantic. The doctor is the
    place where "is this channel ID known to Slack?" gets answered;
    here we only check that the policy is internally consistent and
    cheap to render.
    """
    errors: list[str] = []
    from .oc_config import is_slack_channel_id

    if policy.schema_version != SCHEMA_VERSION:
        errors.append(
            f"schema_version {policy.schema_version} != expected {SCHEMA_VERSION}"
        )
    if not policy.bot_id.strip():
        errors.append("bot_id is required")

    seen_ids: set[str] = set()
    for entry in policy.channels.entries:
        if not is_slack_channel_id(entry.channel_id):
            errors.append(
                f"channels.entries[].channel_id={entry.channel_id!r} is not a Slack ID"
            )
        if entry.channel_id in seen_ids:
            errors.append(f"duplicate channel_id: {entry.channel_id}")
        seen_ids.add(entry.channel_id)
        # Per-channel visible_replies override — if set, must be a
        # canonical value. Typos (e.g. "thread_olny") would render
        # silently into openclaw.json and no-op at runtime.
        if entry.visible_replies is not None and entry.visible_replies not in ALLOWED_VISIBLE_REPLIES:
            errors.append(
                f"channels.entries[{entry.channel_id}].visible_replies="
                f"{entry.visible_replies!r} not in {ALLOWED_VISIBLE_REPLIES}"
            )

    if policy.messaging.visible_replies_default not in ALLOWED_VISIBLE_REPLIES:
        errors.append(
            f"messaging.visible_replies_default="
            f"{policy.messaging.visible_replies_default!r} "
            f"not in {ALLOWED_VISIBLE_REPLIES}"
        )

    mode = policy.access.dm_allowlist.mode
    if mode == "user_group" and not policy.access.dm_allowlist.user_group_id:
        errors.append("dm_allowlist.mode=user_group but user_group_id is empty")
    if mode == "channel_derived" and not policy.access.dm_allowlist.derived_from_channel_ids:
        errors.append("dm_allowlist.mode=channel_derived but derived_from_channel_ids is empty")
    if mode == "explicit" and not policy.access.dm_allowlist.user_ids:
        # Allowed — explicit-but-empty is "nobody can DM" which is a
        # legitimate (if unusual) configuration. Warn elsewhere if needed.
        pass

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────


def policy_path(shared_dir: Path, bot_id: str) -> Path:
    return shared_dir / "bots" / bot_id / POLICY_FILENAME


def load_policy(shared_dir: Path, bot_id: str) -> SlackPolicy | None:
    """Return the bot's stored policy, or ``None`` if absent.

    Raises ``ValueError`` on a present-but-malformed policy file —
    callers should treat that as a hard error (refusing to render
    against garbage is safer than silently overwriting with defaults).
    """
    path = policy_path(shared_dir, bot_id)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"policy file {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"policy file {path} root is not an object")
    return _from_dict(data, bot_id_default=bot_id)


def save_policy(shared_dir: Path, policy: SlackPolicy) -> Path:
    """Atomically write the policy. Returns the final path.

    Uses ``os.replace`` for atomic rename. Creates the bot's dir if
    it doesn't exist. Validates before write — callers don't get to
    persist invalid policies.
    """
    errors = validate_policy(policy)
    if errors:
        raise ValueError(f"policy is invalid: {'; '.join(errors)}")
    path = policy_path(shared_dir, policy.bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _to_dict(policy)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{POLICY_FILENAME}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Dict <-> dataclass mapping
# ─────────────────────────────────────────────────────────────────────────────


def _to_dict(policy: SlackPolicy) -> dict[str, Any]:
    """Convert policy → JSON-friendly dict. ``asdict`` works because
    every nested type is a dataclass with primitive fields.
    """
    return asdict(policy)


def _from_dict(data: dict[str, Any], *, bot_id_default: str) -> SlackPolicy:
    """Parse a stored JSON document back into dataclasses.

    Forward-compatible at the *field* level: unknown fields are dropped,
    missing fields use dataclass defaults. Strict at the *enum* level:
    an unknown enum value (e.g. ``dm_allowlist.mode="bogus"``) raises
    ``ValueError`` so a hand-edited policy can't silently coerce to a
    default and lose operator intent.
    """
    workspace_d = data.get("workspace") or {}
    workspace = WorkspaceMeta(
        team_id=str(workspace_d.get("team_id") or ""),
        team_name=str(workspace_d.get("team_name") or ""),
    )

    access_d = data.get("access") or {}
    dm_d = access_d.get("dm_allowlist") or {}
    dm = DmAllowlist(
        mode=_strict_literal(
            "access.dm_allowlist.mode", dm_d.get("mode"),
            ALLOWED_DM_MODES, default="open",
        ),
        user_group_id=dm_d.get("user_group_id") or None,
        user_ids=[str(u) for u in (dm_d.get("user_ids") or []) if isinstance(u, str)],
        derived_from_channel_ids=[
            str(c) for c in (dm_d.get("derived_from_channel_ids") or []) if isinstance(c, str)
        ],
    )
    codes = []
    for c in (access_d.get("self_enrollment_codes") or []):
        if not isinstance(c, dict):
            continue
        try:
            codes.append(SelfEnrollmentCode(
                code=str(c["code"]),
                created_by=str(c.get("created_by") or ""),
                created_at=str(c.get("created_at") or ""),
                expires_at=str(c.get("expires_at") or ""),
                consumed=bool(c.get("consumed", False)),
                consumed_by=c.get("consumed_by") or None,
                consumed_at=c.get("consumed_at") or None,
            ))
        except KeyError:
            continue
    access = AccessPolicy(dm_allowlist=dm, self_enrollment_codes=codes)

    channels_d = data.get("channels") or {}
    dfn_d = channels_d.get("default_for_new") or {}
    default_for_new = DefaultForNew(
        behavior=_strict_literal(
            "channels.default_for_new.behavior", dfn_d.get("behavior"),
            ALLOWED_DEFAULT_FOR_NEW, default="ask_admin",
        ),
    )
    entries: list[PolicyChannelEntry] = []
    for e in (channels_d.get("entries") or []):
        if not isinstance(e, dict):
            continue
        cid = e.get("channel_id")
        if not isinstance(cid, str) or not cid:
            continue
        entries.append(PolicyChannelEntry(
            channel_id=cid,
            channel_name=str(e.get("channel_name") or ""),
            channel_type=_strict_literal(
                f"channels.entries[{cid}].channel_type",
                e.get("channel_type"), ALLOWED_CHANNEL_TYPES, default="public",
            ),
            require_mention=bool(e.get("require_mention", True)),
            visible_replies=e.get("visible_replies") or None,
            added_at=str(e.get("added_at") or ""),
            added_by=str(e.get("added_by") or ""),
        ))
    channels = ChannelsPolicy(default_for_new=default_for_new, entries=entries)

    messaging_d = data.get("messaging") or {}
    qh_d = messaging_d.get("quiet_hours") or {}
    quiet_hours = QuietHours(
        enabled=bool(qh_d.get("enabled", False)),
        timezone=str(qh_d.get("timezone") or "UTC"),
        start=str(qh_d.get("start") or "22:00"),
        end=str(qh_d.get("end") or "07:00"),
        paged_overrides=bool(qh_d.get("paged_overrides", True)),
    )
    messaging = MessagingPolicy(
        visible_replies_default=str(messaging_d.get("visible_replies_default") or "automatic"),
        thread_replies=bool(messaging_d.get("thread_replies", True)),
        quiet_hours=quiet_hours,
        mention_style=_strict_literal(
            "messaging.mention_style", messaging_d.get("mention_style"),
            ALLOWED_MENTION_STYLES, default="strict",
        ),
    )

    return SlackPolicy(
        bot_id=str(data.get("bot_id") or bot_id_default),
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        workspace=workspace,
        access=access,
        channels=channels,
        messaging=messaging,
        last_reconciled_at=data.get("last_reconciled_at") or None,
        drift_signals=[
            str(s) for s in (data.get("drift_signals") or []) if isinstance(s, str)
        ],
    )


def _strict_literal(
    path: str, value: Any, allowed: tuple[str, ...], *, default: str,
) -> str:
    """Strict enum coercion: missing → default, unknown → raise.

    Distinguishes "field not set" (use the default — fine) from "field
    set to something unrecognized" (operator intent we can't preserve
    — surface so they can fix it). Silently coercing the latter to a
    default would lose operator intent (reviewer feedback #6).
    """
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected string, got {type(value).__name__}")
    if value not in allowed:
        raise ValueError(f"{path}={value!r} not in {allowed}")
    return value


def to_dict_redacted(policy: SlackPolicy) -> dict[str, Any]:
    """Like :func:`_to_dict` but redacts secret values.

    Use for any operator-facing output (CLI, evo DM, API responses).
    Currently redacts unconsumed self-enrollment codes. The code is
    a single-use credential — once issued, the admin should only need
    to know whether it's still active, not what the value is.
    """
    payload = _to_dict(policy)
    access = payload.get("access") or {}
    codes = access.get("self_enrollment_codes") or []
    for c in codes:
        if isinstance(c, dict) and not c.get("consumed"):
            c["code"] = "<redacted>"
    return payload


# now_iso (ISO-8601 UTC, "Z" seconds) is re-exported from evolve_util above
# for added_at / last_reconciled_at consumers.


__all__ = [
    "AccessPolicy",
    "ChannelsPolicy",
    "DefaultForNew",
    "DmAllowlist",
    "MessagingPolicy",
    "POLICY_FILENAME",
    "PolicyChannelEntry",
    "QuietHours",
    "SCHEMA_VERSION",
    "SelfEnrollmentCode",
    "SlackPolicy",
    "WorkspaceMeta",
    "load_policy",
    "now_iso",
    "policy_path",
    "save_policy",
    "to_dict_redacted",
    "validate_policy",
]
