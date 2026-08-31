"""Read the Slack-relevant slice of a bot's openclaw.json + workspace .env.

**OC openclaw.json Slack shape** (verified 2026-05-13 against team_bot_a + admin_bot on
the production mini — see ``internal/spec-slack-policy-2026-05-13.md`` §3.1):

::

    channels:
      slack:
        botToken:        "xoxb-..."          # credential
        appToken:        "xapp-..."          # credential (socket-mode)
        userTokenReadOnly: "xoxp-..."        # credential
        mode:            "socket"            # transport mode
        enabled:         true
        webhookPath:     "/slack/events"
        groupPolicy:     "allowlist"         # channel routing policy
        dmPolicy:        "pairing"           # DM behavior
        streaming:       {...}
        allowFrom:       ["U0...", ...]      # user allowlist (~who can talk to bot)
        channels:
          C0AL2GDUA7J:                       # channel allowlist (KEYED BY SLACK ID)
            requireMention: false
          G0T79FGSE:
            requireMention: false
          ...
      telegram:
        botToken: ...                        # separate provider sub-block
        ...
      whatsapp:                              # other providers same shape
        ...
    messages:
      groupChat:
        visibleReplies: "automatic"          # spec §3 bug-2 trap
    directMessages:                          # legacy / unused on current OC; left for compat
      allowedUserIds: [...]

**What this module extracts** for the doctor + writer:

- ``bot_token`` — from ``channels.slack.botToken`` or workspace .env fallback
- ``group_policy`` — ``channels.slack.groupPolicy``
- ``channel_entries`` — entries from ``channels.slack.channels.<KEY>``
- ``slack_allow_from_user_ids`` — ``channels.slack.allowFrom`` (user allowlist)
- ``visible_replies_default`` — ``messages.groupChat.visibleReplies``
- ``other_provider_keys`` — top-level non-slack sub-block names (telegram,
  discord, whatsapp, etc.) for surfacing stale providers

**Historical note.** The original spec assumed Slack channel entries lived
at top-level ``channels.<SLACK_ID>``. That was wrong — entries are nested
under ``channels.slack.channels``. PR #1074 shipped with the wrong shape;
verified ground-truth against team_bot_a and admin_bot on 2026-05-13 and fixed here.
The takeaway: build against an actual openclaw.json, not against a written
post-mortem. (See ``feedback_rsi_design_approach`` — verify-or-don't-ship.)

Read pattern follows ``upstream_version._read_openclaw_json_meta_version``:
direct ``Path.read_text()`` first, fall back to ``sudo /bin/cat`` (with
``cwd=/tmp``) when the bot's home dir is ACL-restricted. Both paths return
plain ``str`` content; JSON parsing is up to the caller.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# File reads (mirrors the openclaw.json pattern in upstream_version.py)
# ─────────────────────────────────────────────────────────────────────────────


def read_bot_text_file(
    path: Path,
    *,
    sudo_cat_timeout: float = 5.0,
) -> tuple[str | None, str | None]:
    """Return ``(text, error)`` for a file under a bot's home dir.

    Mirrors :func:`upstream_version._read_openclaw_json_meta_version` —
    direct read first, then ``sudo /bin/cat`` with ``cwd=/tmp``. Both
    paths must work because:

    - Bots deployed via the new ACL path expose ``.openclaw/`` to
      ``evolve`` via macOS ACL inheritance (deploy.set_evolve_read_acl).
    - Pre-ACL bots and any file outside ``.openclaw/`` (e.g. the workspace
      ``.env`` if it's not under ``.openclaw/workspace/``) still need
      the sudo fallback.
    """
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
            capture_output=True,
            text=True,
            timeout=sudo_cat_timeout,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired:
        return None, "sudo_cat_timeout"
    except OSError as exc:
        return None, f"sudo_cat_error: {exc}"
    if r.returncode != 0:
        return None, f"sudo_cat_rc={r.returncode}"
    return r.stdout, None


# ─────────────────────────────────────────────────────────────────────────────
# Parsed views the doctor consumes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ChannelEntry:
    """One entry from openclaw.json ``channels.slack.channels.<key>``.

    ``key`` is whatever string the operator put in the JSON — could be a
    Slack channel ID (correct) or a channel name (bug 1, silent drop).
    The doctor classifies this in :func:`is_slack_channel_id`.
    """
    key: str
    require_mention: bool | None = None
    visible_replies: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenclawSlackView:
    """The slice of openclaw.json the doctor cares about.

    Fields default to safe sentinel values (``None``, empty lists, empty
    dicts) when the underlying JSON path is missing, so callers can
    branch on presence without juggling KeyErrors.
    """
    bot_id: str

    # Read state
    openclaw_path: Path
    openclaw_present: bool
    read_error: str | None = None
    parse_error: str | None = None

    # Slack-section fields (all under ``channels.slack.*`` in openclaw.json)
    bot_token: str | None = None
    bot_token_source: str | None = None
    """Where the token was found: ``'openclaw_channels'`` | ``'workspace_dotenv'`` | ``None``"""
    group_policy: str | None = None
    """``channels.slack.groupPolicy`` (e.g. ``"allowlist"``)."""
    dm_policy: str | None = None
    """``channels.slack.dmPolicy`` (e.g. ``"pairing"``)."""
    slack_enabled: bool | None = None
    """``channels.slack.enabled`` — when ``False`` the bot won't process Slack at all."""
    transport_mode: str | None = None
    """``channels.slack.mode`` — ``"socket"`` or ``"http"`` (the two upstream-supported modes)."""
    has_signing_secret: bool = False
    """True if ``channels.slack.signingSecret`` is set (required for HTTP Request URL mode)."""
    has_app_token: bool = False
    """True if ``channels.slack.appToken`` is set (required for socket mode)."""
    streaming_mode: str | None = None
    """``channels.slack.streaming.mode`` — ``"partial"``, ``"block"``, or ``"off"``.

    ``partial`` streams intermediate tool calls as live message edits.
    Designed for personal-bot DMs where the user *wants* the feedback;
    catastrophic in a team channel where the bot processes every message
    (every conversation becomes a wall of internal tool-output noise).
    SLK015 fires when ``partial`` is paired with a channel mix that
    broadcasts to a team."""
    streaming_native_transport: bool | None = None
    """``channels.slack.streaming.nativeTransport``. When ``true`` (with
    ``mode: "partial"``) OC uses Slack's native streaming API which
    exposes tool-call boundaries explicitly. The team_bot_a-2026-05-14
    incident shape."""
    streaming_command_text: str | None = None
    """``channels.slack.streaming.progress.commandText``. ``"raw"``
    (default) renders the literal command/exec text into the streaming
    message body — that's the wall of ``• command run python3 ...``
    bullets a team channel sees during the bot's work. ``"status"``
    keeps compact tool-progress lines while hiding the raw command
    text. SLK016 fires when this is ``"raw"`` (explicit or via default)
    AND streaming is active in a team channel."""
    channel_entries: list[ChannelEntry] = field(default_factory=list)
    """Entries from ``channels.slack.channels.<KEY>``."""
    visible_replies_default: str | None = None
    """``messages.groupChat.visibleReplies`` (the bug-2 trap)."""
    slack_allow_from_user_ids: list[str] = field(default_factory=list)
    """``channels.slack.allowFrom`` — user IDs allowed to interact with the bot."""

    # Top-level provider sub-blocks other than slack (telegram, whatsapp,
    # discord, etc.) — surfaced so the doctor can flag stale ones the
    # operator probably forgot to remove.
    other_provider_keys: list[str] = field(default_factory=list)

    # Whole-block escape hatches for checks that need adjacent fields the
    # focused view doesn't surface yet. Kept opt-in to discourage
    # ad-hoc reads in the doctor body.
    raw_channels_block: dict[str, Any] = field(default_factory=dict)
    raw_slack_block: dict[str, Any] = field(default_factory=dict)
    raw_messages_block: dict[str, Any] = field(default_factory=dict)

    @property
    def slack_provider_present(self) -> bool:
        """True if the bot has any Slack configuration intent.

        A bot is treated as "uses Slack" when either ``channels.slack``
        is present with content in openclaw.json, or a Slack bot token
        was resolvable (inline, SecretRef, or workspace .env fallback).
        Telegram-only / Discord-only / unconfigured bots return False
        and the doctor should skip Slack-specific findings against them
        — firing SLK003 on a bot that has no Slack at all is pure noise
        (see personal_bot-2026-05-19).
        """
        return bool(self.raw_slack_block) or bool(self.bot_token)

    def __repr__(self) -> str:
        # Redact the bot token — the view is sometimes logged via
        # tracebacks or `repr(view)` in error paths. Token is the leak
        # vector; the raw blocks may also embed credentials so we don't
        # render them at all.
        token_marker = "<redacted>" if self.bot_token else None
        return (
            f"OpenclawSlackView(bot_id={self.bot_id!r}, "
            f"openclaw_present={self.openclaw_present}, "
            f"bot_token={token_marker!r}, bot_token_source={self.bot_token_source!r}, "
            f"group_policy={self.group_policy!r}, "
            f"dm_policy={self.dm_policy!r}, "
            f"slack_enabled={self.slack_enabled!r}, "
            f"channel_entries={len(self.channel_entries)}, "
            f"visible_replies_default={self.visible_replies_default!r}, "
            f"allow_from={len(self.slack_allow_from_user_ids)}, "
            f"other_providers={self.other_provider_keys!r})"
        )


def resolve_token_value(
    value: Any,
    *,
    env_lookup: "Any" = None,
) -> tuple[str | None, str | None]:
    """Return ``(token, source_hint)`` from an OC token field.

    Per docs.openclaw.ai/channels/slack, ``botToken``/``appToken``/
    ``userToken``/``signingSecret`` accept either a plain string or a
    SecretRef object::

        { "source": "env", "provider": "default", "id": "SLACK_BOT_TOKEN" }

    Plain string → return verbatim with ``source_hint="inline"``.
    SecretRef → resolve through ``env_lookup`` (default ``os.environ.get``).
    Anything else → ``(None, None)``.

    ``env_lookup`` is injected for tests so we don't depend on
    ``os.environ`` at module load time.
    """
    if isinstance(value, str):
        v = value.strip()
        return (v or None), ("inline" if v else None)
    if isinstance(value, dict):
        if (value.get("source") or "").lower() != "env":
            return None, None
        key = value.get("id") or value.get("name") or value.get("var")
        if not isinstance(key, str) or not key:
            return None, None
        if env_lookup is None:
            import os
            env_lookup = os.environ.get
        try:
            resolved = env_lookup(key)
        except Exception:
            return None, None
        if not isinstance(resolved, str) or not resolved.strip():
            return None, None
        return resolved.strip(), f"secretref:env:{key}"
    return None, None


def is_slack_channel_id(key: str) -> bool:
    """Return True if ``key`` looks like a Slack channel ID (``C…``/``G…``/``D…``).

    Slack channel IDs are uppercase alphanumeric starting with ``C``
    (public), ``G`` (private), or ``D`` (DM). Lowercased keys, keys with
    a leading ``#``, and keys containing whitespace or punctuation are
    not IDs.

    The doctor uses this exclusively for SLK001 — a name-keyed entry
    silently drops messages under ``groupPolicy: "allowlist"``. False
    positives here would FAIL a working config, so the heuristic is
    deliberately strict: minimum length 9 (typical IDs are 9–11 chars),
    uppercase A-Z + 0-9 only, leading prefix in {C, G, D}.
    """
    if not key or len(key) < 9:
        return False
    if key[0] not in ("C", "G", "D"):
        return False
    return all(ch.isupper() or ch.isdigit() for ch in key)


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────


def load_openclaw_view(
    bot_id: str,
    bot_home: Path,
) -> OpenclawSlackView:
    """Build an :class:`OpenclawSlackView` from disk.

    Always returns a view object — never raises. Callers branch on
    ``openclaw_present`` and ``parse_error`` to decide whether to run
    Slack-dependent checks.
    """
    oc_path = bot_home / ".openclaw" / "openclaw.json"
    view = OpenclawSlackView(
        bot_id=bot_id,
        openclaw_path=oc_path,
        openclaw_present=False,
    )

    text, read_err = read_bot_text_file(oc_path)
    if read_err == "not_found":
        # No openclaw.json — the bot isn't installed yet. The doctor
        # treats this as a no-op (NO_EVIDENCE-equivalent), not a fail.
        view.read_error = read_err
        return view
    if text is None:
        view.read_error = read_err or "unknown_read_error"
        return view

    view.openclaw_present = True
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        view.parse_error = f"json_decode: {exc.msg}"
        return view
    if not isinstance(data, dict):
        view.parse_error = "root_not_object"
        return view

    channels_block = data.get("channels") or {}
    if isinstance(channels_block, dict):
        view.raw_channels_block = dict(channels_block)

        # Top-level provider sub-blocks other than 'slack' — surfaced as
        # data so the doctor can flag stale ones (e.g. team_bot_a had a
        # leftover 'whatsapp' block from a pre-Slack era).
        view.other_provider_keys = sorted(
            key for key, value in channels_block.items()
            if key != "slack" and isinstance(value, dict)
        )

        slack_block = channels_block.get("slack")
        if isinstance(slack_block, dict):
            view.raw_slack_block = dict(slack_block)

            # botToken can be a plain string OR a SecretRef object
            # (docs.openclaw.ai/channels/slack). Handle both shapes so
            # a SecretRef-configured bot doesn't get a false-positive
            # "no token configured" report.
            token, source_hint = resolve_token_value(slack_block.get("botToken"))
            if token:
                view.bot_token = token
                view.bot_token_source = (
                    "openclaw_channels" if source_hint == "inline"
                    else f"openclaw_channels:{source_hint}"
                )
            # Surface presence of signingSecret (HTTP Request URL mode)
            # without recording the value. Used by the status header
            # to confirm HTTP-mode setups have the secret installed.
            sig_secret, _ = resolve_token_value(slack_block.get("signingSecret"))
            view.has_signing_secret = bool(sig_secret)
            # appToken presence (socket mode — required if mode is socket)
            app_token, _ = resolve_token_value(slack_block.get("appToken"))
            view.has_app_token = bool(app_token)
            mode_value = slack_block.get("mode")
            if isinstance(mode_value, str):
                view.transport_mode = mode_value

            policy = slack_block.get("groupPolicy")
            if isinstance(policy, str):
                view.group_policy = policy

            dm_policy = slack_block.get("dmPolicy")
            if isinstance(dm_policy, str):
                view.dm_policy = dm_policy

            enabled = slack_block.get("enabled")
            if isinstance(enabled, bool):
                view.slack_enabled = enabled

            streaming = slack_block.get("streaming")
            if isinstance(streaming, dict):
                sm = streaming.get("mode")
                if isinstance(sm, str):
                    view.streaming_mode = sm
                nt = streaming.get("nativeTransport")
                if isinstance(nt, bool):
                    view.streaming_native_transport = nt
                progress = streaming.get("progress")
                if isinstance(progress, dict):
                    ct = progress.get("commandText")
                    if isinstance(ct, str):
                        view.streaming_command_text = ct

            # User allowlist: ``channels.slack.allowFrom`` is the actual
            # gate on who can interact with the bot. (Legacy
            # ``directMessages.allowedUserIds`` was the wrong field —
            # see module docstring.)
            allow_from = slack_block.get("allowFrom")
            if isinstance(allow_from, list):
                view.slack_allow_from_user_ids = [
                    str(u) for u in allow_from if isinstance(u, str) and u.strip()
                ]

            # Channel allowlist: ``channels.slack.channels.<KEY>``.
            # KEY should be a Slack channel ID; name-keyed entries are
            # bug 1 and get FAIL'd by SLK001.
            slack_channels = slack_block.get("channels")
            if isinstance(slack_channels, dict):
                for key, value in slack_channels.items():
                    if not isinstance(value, dict):
                        continue
                    entry = ChannelEntry(
                        key=key,
                        require_mention=value.get("requireMention")
                        if isinstance(value.get("requireMention"), bool) else None,
                        visible_replies=value.get("visibleReplies")
                        if isinstance(value.get("visibleReplies"), str) else None,
                        raw=dict(value),
                    )
                    view.channel_entries.append(entry)

    messages_block = data.get("messages") or {}
    if isinstance(messages_block, dict):
        view.raw_messages_block = dict(messages_block)
        group_chat = messages_block.get("groupChat") or {}
        if isinstance(group_chat, dict):
            vr = group_chat.get("visibleReplies")
            if isinstance(vr, str):
                view.visible_replies_default = vr

    if view.bot_token is None:
        # Fall back to the workspace .env (team_bot_a-style layout)
        env_token = _read_dotenv_slack_token(bot_home)
        if env_token:
            view.bot_token = env_token
            view.bot_token_source = "workspace_dotenv"

    return view


def _read_dotenv_slack_token(bot_home: Path) -> str | None:
    """Best-effort read of ``SLACK_BOT_TOKEN`` from the workspace .env.

    Mirrors the lookup in ``probes.DotenvProbe`` (names only — values
    are *only* extracted for the bot the doctor is examining; never
    surfaced beyond the in-memory ``OpenclawSlackView``). Returns the
    token value or ``None`` if the file is absent / unreadable / lacks
    a non-empty assignment.
    """
    env_path = bot_home / ".openclaw" / "workspace" / ".env"
    text, err = read_bot_text_file(env_path)
    if text is None:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # `export FOO=bar` is common in shell-style .env files; strip the
        # prefix before name comparison.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        if name.strip() != "SLACK_BOT_TOKEN":
            continue
        # Strip optional surrounding quotes; trim whitespace
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


__all__ = [
    "ChannelEntry",
    "OpenclawSlackView",
    "is_slack_channel_id",
    "load_openclaw_view",
    "read_bot_text_file",
    "resolve_token_value",
]
