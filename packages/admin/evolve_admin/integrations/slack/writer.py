"""Render a :class:`SlackPolicy` down to a bot's openclaw.json.

The writer reads the bot's existing openclaw.json, applies only the
Slack-owned sections from the policy, and writes the result back.
Everything outside the Slack ownership boundary is preserved verbatim.

**Ownership boundary** (verified against ground-truth openclaw.json on
2026-05-13; see ``oc_config`` module docstring for the canonical shape):

The writer OVERWRITES (all paths under ``channels.slack.*``):

- ``channels.slack.groupPolicy`` → ``"allowlist"`` (only mode emitted in Phase 2)
- ``channels.slack.channels.<KEY>`` — channel allowlist (one entry per
  ``policy.channels.entries[]``). KEY must be a Slack ID; the writer
  refuses to render name-keyed entries because ``validate_policy``
  catches them upstream. Stale entries (including legacy name-keyed
  bug-1 leftovers) are swept on every render.
- ``messages.groupChat.visibleReplies`` ← ``policy.messaging.visible_replies_default``
- ``channels.slack.allowFrom`` — user allowlist when ``dm_allowlist.mode``
  is ``"explicit"``; cleared on ``"open"`` and on mode-changes from
  ``"explicit"`` to anything else (safe-state-on-mode-switch).

The writer PRESERVES (everything else):

- ``channels.slack.botToken|appToken|userTokenReadOnly`` — credentials
- ``channels.slack.mode|dmPolicy|enabled|webhookPath|streaming`` — other
  slack-provider fields the operator (or installer) configured
- ``channels.telegram``, ``channels.discord``, ``channels.whatsapp``, etc.
  — sibling provider sub-blocks under ``channels.*``
- ``messages.*`` sub-keys other than ``groupChat.visibleReplies``
- All other top-level openclaw.json fields (``hooks``, ``permissions``,
  ``meta``, ``agent``, etc.)
- Unknown per-channel fields under ``channels.slack.channels.<KEY>`` —
  writer only touches ``requireMention``/``visibleReplies``/``displayName``;
  any other operator-set fields on an entry survive a re-render

**Atomicity:** stage to ``/tmp``, validate the JSON parses, then
``sudo /bin/cp`` to the bot's openclaw.json. Same pattern as the
existing rotate path and forge writer.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .oc_config import is_slack_channel_id
from .policy import SlackPolicy, validate_policy

log = logging.getLogger(__name__)


GROUP_POLICY = "allowlist"  # The only mode the writer emits in Phase 2


@dataclass
class WriteResult:
    """Outcome of one :func:`render_to_openclaw_json` call."""
    bot_id: str
    target_path: Path
    written: bool
    """True if the file was rewritten; False if the existing content already matched."""
    pre_validate_errors: list[str]
    write_error: str | None = None
    removed_channel_ids: list[str] = None  # type: ignore[assignment]
    added_channel_ids: list[str] = None  # type: ignore[assignment]
    updated_channel_ids: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.removed_channel_ids is None:
            self.removed_channel_ids = []
        if self.added_channel_ids is None:
            self.added_channel_ids = []
        if self.updated_channel_ids is None:
            self.updated_channel_ids = []

    @property
    def ok(self) -> bool:
        return not self.pre_validate_errors and self.write_error is None


# ─────────────────────────────────────────────────────────────────────────────
# Pure-function core (testable without disk I/O)
# ─────────────────────────────────────────────────────────────────────────────


def merge_policy_into_openclaw(
    *,
    existing: dict[str, Any],
    policy: SlackPolicy,
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Return ``(merged, added_ids, updated_ids, removed_ids)``.

    ``existing`` is the current openclaw.json contents (a dict — caller
    is responsible for parsing). ``merged`` is a fresh dict the caller
    can serialize and write. The original ``existing`` is not mutated.

    Channel diff buckets reflect changes under ``channels.slack.channels``:

    - ``added_ids`` — in ``policy.channels.entries``, not in existing
    - ``updated_ids`` — in both, but per-channel settings changed
    - ``removed_ids`` — Slack-ID-keyed entry in existing not in policy,
      plus any name-keyed (bug-1) entries that get swept
    """
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}

    # ── channels.slack sub-block ──────────────────────────────────────────
    channels_block = merged.get("channels")
    if not isinstance(channels_block, dict):
        channels_block = {}
        merged["channels"] = channels_block

    slack_sub = channels_block.get("slack")
    if not isinstance(slack_sub, dict):
        slack_sub = {}
        channels_block["slack"] = slack_sub

    slack_sub["groupPolicy"] = GROUP_POLICY

    # The channel allowlist lives at ``channels.slack.channels`` per
    # the verified OC shape. Preserve unknown peer keys on the slack
    # sub-block (mode, dmPolicy, streaming, webhookPath, credentials).
    slack_channels = slack_sub.get("channels")
    if not isinstance(slack_channels, dict):
        slack_channels = {}
        slack_sub["channels"] = slack_channels

    # Diff against the prior render. Slack-ID-keyed entries are policy
    # targets; name-keyed entries (bug 1) are stale and must be swept.
    existing_id_keyed: dict[str, dict[str, Any]] = {
        key: value for key, value in slack_channels.items()
        if isinstance(value, dict) and is_slack_channel_id(key)
    }
    name_keyed_stale: list[str] = [
        key for key, value in slack_channels.items()
        if isinstance(value, dict) and not is_slack_channel_id(key)
    ]

    policy_entries: dict[str, dict[str, Any]] = {}
    for entry in policy.channels.entries:
        # Preserve unknown per-channel fields the writer doesn't manage —
        # start from the existing entry dict (if present) and set only
        # the three fields we own.
        rendered = dict(existing_id_keyed.get(entry.channel_id, {}))
        rendered["requireMention"] = bool(entry.require_mention)
        if entry.visible_replies:
            rendered["visibleReplies"] = entry.visible_replies
        else:
            rendered.pop("visibleReplies", None)
        if entry.channel_name:
            rendered["displayName"] = entry.channel_name
        else:
            rendered.pop("displayName", None)
        policy_entries[entry.channel_id] = rendered

    added_ids: list[str] = []
    updated_ids: list[str] = []
    removed_ids: list[str] = []
    for cid, new_value in policy_entries.items():
        if cid not in existing_id_keyed:
            added_ids.append(cid)
        elif existing_id_keyed[cid] != new_value:
            updated_ids.append(cid)
    for cid in existing_id_keyed:
        if cid not in policy_entries:
            removed_ids.append(cid)
    removed_ids.extend(name_keyed_stale)

    for key in removed_ids:
        slack_channels.pop(key, None)
    for cid, value in policy_entries.items():
        slack_channels[cid] = value

    # ── messages.groupChat.visibleReplies ──────────────────────────────────
    messages_block = merged.get("messages")
    if not isinstance(messages_block, dict):
        messages_block = {}
        merged["messages"] = messages_block
    group_chat = messages_block.get("groupChat")
    if not isinstance(group_chat, dict):
        group_chat = {}
        messages_block["groupChat"] = group_chat
    group_chat["visibleReplies"] = policy.messaging.visible_replies_default

    # ── channels.slack.allowFrom (user allowlist) ─────────────────────────
    dm_mode = policy.access.dm_allowlist.mode
    if dm_mode == "open":
        # Open = no user gate. If a previous render wrote allowFrom,
        # remove it. We keep the slack_sub key itself.
        slack_sub.pop("allowFrom", None)
    elif dm_mode == "explicit":
        # Sorted for deterministic output — diff stability matters when
        # the operator inspects openclaw.json by hand.
        slack_sub["allowFrom"] = sorted(policy.access.dm_allowlist.user_ids)
    else:
        # user_group / channel_derived — resolution requires a Slack
        # client (writer is intentionally offline-runnable). Safe state
        # when we can't resolve is "no allowlist", not "stale allowlist".
        slack_sub.pop("allowFrom", None)

    return merged, added_ids, updated_ids, removed_ids


def is_render_up_to_date(
    *,
    existing: dict[str, Any],
    policy: SlackPolicy,
) -> bool:
    """True if ``existing`` already matches what the policy would render.

    Used by the doctor's drift check (SLK011) — fires when a policy
    exists but openclaw.json has drifted (someone hand-edited, or a
    previous write failed). Lets us avoid rewriting an already-correct
    file in :func:`render_to_openclaw_json`.
    """
    merged, added, updated, removed = merge_policy_into_openclaw(
        existing=existing, policy=policy,
    )
    if added or updated or removed:
        return False
    return merged == existing


# ─────────────────────────────────────────────────────────────────────────────
# I/O wrapper
# ─────────────────────────────────────────────────────────────────────────────


def render_to_openclaw_json(
    *,
    bot_id: str,
    bot_home: Path,
    policy: SlackPolicy,
    skip_if_up_to_date: bool = True,
) -> WriteResult:
    """Read openclaw.json, merge policy, write atomically.

    Returns a :class:`WriteResult` describing what happened. Never
    raises — every failure path produces a result with a populated
    ``write_error`` field. Callers (CLI, evo DM handler, deploy) can
    decide whether to surface or retry.
    """
    target = bot_home / ".openclaw" / "openclaw.json"
    pre_errors = validate_policy(policy)
    if pre_errors:
        return WriteResult(
            bot_id=bot_id, target_path=target, written=False,
            pre_validate_errors=pre_errors,
            write_error="policy_invalid",
        )

    existing_text, read_err = _read_with_sudo_fallback(target)
    if existing_text is None:
        return WriteResult(
            bot_id=bot_id, target_path=target, written=False,
            pre_validate_errors=[],
            write_error=f"read_failed: {read_err}",
        )
    try:
        existing = json.loads(existing_text)
    except json.JSONDecodeError as exc:
        return WriteResult(
            bot_id=bot_id, target_path=target, written=False,
            pre_validate_errors=[],
            write_error=f"existing_json_decode: {exc.msg}",
        )
    if not isinstance(existing, dict):
        return WriteResult(
            bot_id=bot_id, target_path=target, written=False,
            pre_validate_errors=[],
            write_error="existing_root_not_object",
        )

    merged, added, updated, removed = merge_policy_into_openclaw(
        existing=existing, policy=policy,
    )

    if skip_if_up_to_date and merged == existing:
        return WriteResult(
            bot_id=bot_id, target_path=target, written=False,
            pre_validate_errors=[],
        )

    serialized = json.dumps(merged, indent=2)
    write_err = _write_with_sudo(target, serialized)
    return WriteResult(
        bot_id=bot_id, target_path=target,
        written=write_err is None,
        pre_validate_errors=[],
        write_error=write_err,
        added_channel_ids=added,
        updated_channel_ids=updated,
        removed_channel_ids=removed,
    )


def _read_with_sudo_fallback(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(), None
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        pass
    except OSError as exc:
        return None, f"os_error: {exc}"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5.0, cwd="/tmp",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"sudo_cat_error: {exc}"
    if r.returncode != 0:
        return None, f"sudo_cat_rc={r.returncode}"
    return r.stdout, None


def _write_with_sudo(path: Path, content: str) -> str | None:
    """Stage in /tmp, sudo /bin/cp to target. Returns None on success."""
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-slack-writer-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # Validate the file we just wrote parses — if a bug ever
        # corrupted the JSON, refuse to install it.
        try:
            with open(tmp) as f:
                json.loads(f.read())
        except json.JSONDecodeError as exc:
            return f"post_write_validate: {exc.msg}"
        try:
            r = subprocess.run(
                ["sudo", "/bin/cp", tmp, str(path)],
                capture_output=True, text=True, timeout=10.0, cwd="/tmp",
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"sudo_cp_error: {exc}"
        if r.returncode != 0:
            return f"sudo_cp_rc={r.returncode}: {r.stderr.strip()}"
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap from existing openclaw.json
# ─────────────────────────────────────────────────────────────────────────────


def synthesize_policy_from_openclaw(
    *,
    bot_id: str,
    bot_home: Path,
) -> tuple["SlackPolicy | None", str | None]:
    """Build a :class:`SlackPolicy` from a bot's current openclaw.json.

    The transitional path: pod bots predate the policy file, so first-
    time policy adoption synthesizes the policy from openclaw.json
    (the prior source of truth) and saves it. The doctor switches to
    policy-aware mode automatically after that.

    Only Phase 1 SLK001-FAIL-clean openclaw.jsons should be
    synthesized — feeding a bug 1 file into synthesize would create a
    policy that re-renders the bug. The CLI is responsible for
    refusing to synthesize from an openclaw.json with FAIL findings;
    this helper is purely mechanical.
    """
    from .oc_config import load_openclaw_view
    from .policy import (
        AccessPolicy, ChannelsPolicy, DmAllowlist, MessagingPolicy,
        PolicyChannelEntry, SlackPolicy, WorkspaceMeta, now_iso,
    )

    view = load_openclaw_view(bot_id, bot_home)
    if not view.openclaw_present:
        return None, "openclaw_json_missing"
    if view.parse_error:
        return None, f"openclaw_parse_error: {view.parse_error}"

    entries: list[PolicyChannelEntry] = []
    for raw in view.channel_entries:
        if not is_slack_channel_id(raw.key):
            # Phase 1 SLK001 — refuse silently here; the CLI bootstrap
            # path will surface this via the doctor before invoking us.
            continue
        entries.append(PolicyChannelEntry(
            channel_id=raw.key,
            channel_name=str(raw.raw.get("displayName") or ""),
            channel_type="private" if raw.key.startswith("G") else "public",
            require_mention=bool(raw.require_mention) if raw.require_mention is not None else True,
            visible_replies=raw.visible_replies,
            added_at=now_iso(),
            added_by="bootstrap:openclaw_json",
        ))

    # Preserve the existing user allowlist (channels.slack.allowFrom) so
    # the synthesized policy is a faithful capture of operator intent.
    # Empty list → mode "open" (no gate); non-empty → mode "explicit".
    if view.slack_allow_from_user_ids:
        dm_allowlist = DmAllowlist(
            mode="explicit",
            user_ids=list(view.slack_allow_from_user_ids),
        )
    else:
        dm_allowlist = DmAllowlist(mode="open")

    policy = SlackPolicy(
        bot_id=bot_id,
        workspace=WorkspaceMeta(),  # team_id filled in on first doctor run
        access=AccessPolicy(dm_allowlist=dm_allowlist),
        channels=ChannelsPolicy(entries=entries),
        messaging=MessagingPolicy(
            visible_replies_default=view.visible_replies_default or "automatic",
        ),
        last_reconciled_at=now_iso(),
    )
    return policy, None


__all__ = [
    "GROUP_POLICY",
    "WriteResult",
    "is_render_up_to_date",
    "merge_policy_into_openclaw",
    "render_to_openclaw_json",
    "synthesize_policy_from_openclaw",
]
