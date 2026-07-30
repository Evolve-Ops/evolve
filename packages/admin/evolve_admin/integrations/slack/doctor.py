"""Pre-apply Slack config validator (``slack-doctor``).

Implements the 10 checks defined in
``docs/spec-slack-policy-2026-05-13.md`` §5. Each check returns one
:class:`Finding`; ``run_doctor`` aggregates them into a
:class:`DoctorResult`. The CLI, the pre-deploy hook, and the continuous
probe all call ``run_doctor`` — there's exactly one place where check
logic lives.

Check semantics:

- **FAIL** — silent-failure condition that will (or already does) drop
  messages or take the bot offline. The pre-write gate refuses to
  apply a policy with any FAIL finding.
- **WARN** — config drift or hygiene issue worth flagging but not
  blocking. The deploy hook surfaces WARNs as advisory output and
  proceeds.
- **INFO** — observation that doesn't require action (e.g. bot is a
  member of a channel not in the allowlist — could be intentional).

Tolerance rules (per the spec's "tolerance for half-installed state"):

- A bot with no Slack provider configured at all — no ``channels.slack``
  block in openclaw.json and no ``SLACK_BOT_TOKEN`` via .env fallback —
  produces NO findings (not even SLK007). Personal_bot/team_bot_c/team_bot_b are the
  motivating cases: pod members that use Telegram / Discord / etc.,
  never Slack. The doctor must not nag about ``messages.groupChat.visibleReplies``
  on bots that have no Slack presence to apply that field to (personal_bot-2026-05-19).
- A bot with a Slack block present but no token (operator started
  setup, hasn't installed the token yet) produces SLK007 INFO plus
  any pure-config findings that surface real intent errors (SLK001
  name-keyed channels, SLK010 empty allowlist, SLK013 enum drift).
- ``SLK010`` (empty allowlist black-hole) is only a FAIL when the bot
  is actually a member of channels in Slack — otherwise INFO.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from .oc_config import (
    ChannelEntry,
    OpenclawSlackView,
    is_slack_channel_id,
    load_openclaw_view,
)
from .slack_client import SlackClient, SlackError

log = logging.getLogger(__name__)

Severity = Literal["fail", "warn", "info"]


# Public catalog of expected `visibleReplies` defaults — single source of
# truth for SLK003. When OC ships a release that flips its built-in
# default again, update this set and the probe will fire on every bot
# whose policy doesn't agree.
EXPECTED_VISIBLE_REPLIES = "automatic"


@dataclass
class Finding:
    """One check outcome.

    ``code`` is the stable ``SLK001``..``SLK010`` identifier — Signals
    and operator-facing reporting use it as a join key.
    """
    code: str
    severity: Severity
    title: str
    detail: str
    # Anchor metadata — which channel / user / config path the finding
    # is about. Populated only when meaningful; Signal type strings are
    # built from this so the probe's signature stays stable across
    # multi-channel pods.
    channel_id: str | None = None
    user_id: str | None = None
    fixable: bool = False
    """True if ``run_doctor(fix=True)`` knows how to remediate this finding."""


@dataclass
class DoctorResult:
    """Aggregate result of running the doctor on one bot."""
    bot_id: str
    findings: list[Finding] = field(default_factory=list)
    bot_token_source: str | None = None
    """``'openclaw_channels'`` | ``'workspace_dotenv'`` | ``None`` (no token)."""
    auth_test: dict[str, Any] | None = None
    """The ``auth.test`` response when the token worked; surfaces ``team_id`` for callers."""
    bot_member_channels: list[dict[str, Any]] = field(default_factory=list)
    """The ``users.conversations`` result — the channels the bot has *joined*."""
    listening_channel_ids: list[str] = field(default_factory=list)
    """Channel IDs the bot is *listening to* per openclaw.json (``channels.slack.channels``)."""
    listening_channel_entries: list[dict[str, Any]] = field(default_factory=list)
    """Raw per-channel config from openclaw.json, for the status header."""
    allow_from_user_ids: list[str] = field(default_factory=list)
    """``channels.slack.allowFrom`` — user IDs allowed to talk to the bot."""
    other_provider_keys: list[str] = field(default_factory=list)
    """Top-level non-slack provider sub-blocks in openclaw.json (telegram, whatsapp, etc.)."""

    # ── State surfaced from the parsed view (Plex-test status header) ────
    slack_enabled: bool | None = None
    """``channels.slack.enabled`` — None means key was absent (treat as enabled by default)."""
    dm_policy: str | None = None
    group_policy: str | None = None
    transport_mode: str | None = None
    """``"socket"`` or ``"http"`` — drives which credentials we expect."""
    has_signing_secret: bool = False
    has_app_token: bool = False
    streaming_mode: str | None = None
    """``channels.slack.streaming.mode`` — drives SLK015."""
    streaming_native_transport: bool | None = None
    streaming_command_text: str | None = None
    """``channels.slack.streaming.progress.commandText`` — ``"raw"`` (default)
    leaks the literal command/exec text into the streaming message body
    (the wall of bullets pattern). Drives SLK016."""

    # ── OAuth scopes (from auth.test x-oauth-scopes header) ──────────────
    oauth_scopes: list[str] = field(default_factory=list)
    """The scopes granted to the bot token, as returned by Slack."""
    feature_bundles: list[Any] = field(default_factory=list)
    """``list[scopes.BundleStatus]`` — feature checklist per the bundle map."""
    elevated_scopes_granted: list[str] = field(default_factory=list)
    """Scopes flagged as broad-blast-radius in ``scopes.ELEVATED_SCOPES``."""

    fixes_applied: list[str] = field(default_factory=list)
    """SLK001 channel keys rewritten name→ID when ``fix=True``."""

    def has_fail(self) -> bool:
        return any(f.severity == "fail" for f in self.findings)

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def run_doctor(
    bot_id: str,
    *,
    bot_home: Path,
    shared_dir: Path | None = None,
    slack_client_factory: "Any" = None,
) -> DoctorResult:
    """Run every check against ``bot_id`` and return findings.

    ``shared_dir`` enables policy-aware mode (Phase 2). When supplied,
    the doctor checks for a ``{shared_dir}/bots/<bot_id>/slack-policy.json``
    and emits SLK011 if the policy exists but openclaw.json doesn't
    match what the writer would render. Phase 1 callers (and bots
    without a policy file) pass ``None`` and get the openclaw.json-only
    behavior.

    ``slack_client_factory`` is dependency-injected for tests — a
    callable that takes ``bot_token`` and returns a SlackClient-shaped
    object. Production callers leave it ``None``; ``run_doctor``
    constructs a real :class:`SlackClient`.

    The doctor itself is read-only. The CLI owns the ``--fix``
    rewrite path (via :func:`rewrite_openclaw_json_channel_keys`)
    because resolving name → Slack ID requires the live member-channel
    list, and the CLI is responsible for the two-pass run (validate
    → fix → re-validate).
    """
    result = DoctorResult(bot_id=bot_id)
    view = load_openclaw_view(bot_id, bot_home)
    result.bot_token_source = view.bot_token_source
    result.listening_channel_ids = [e.key for e in view.channel_entries]
    result.listening_channel_entries = [
        {
            "channel_id": e.key,
            "require_mention": e.require_mention,
            "visible_replies": e.visible_replies,
            "display_name": e.raw.get("displayName") if isinstance(e.raw.get("displayName"), str) else None,
        }
        for e in view.channel_entries
    ]
    result.allow_from_user_ids = list(view.slack_allow_from_user_ids)
    result.other_provider_keys = list(view.other_provider_keys)
    result.slack_enabled = view.slack_enabled
    result.dm_policy = view.dm_policy
    result.group_policy = view.group_policy
    result.transport_mode = view.transport_mode
    result.has_signing_secret = view.has_signing_secret
    result.has_app_token = view.has_app_token
    result.streaming_mode = view.streaming_mode
    result.streaming_native_transport = view.streaming_native_transport
    result.streaming_command_text = view.streaming_command_text

    # Policy-aware: SLK011 drift check (Phase 2). Always runs first so
    # subsequent checks can know whether the operator's source of truth
    # is openclaw.json (Phase 1) or the policy file (Phase 2).
    if shared_dir is not None:
        _check_policy_drift(bot_id, shared_dir, view, result)

    # Bot's openclaw.json absent — half-installed; doctor is a no-op.
    if not view.openclaw_present:
        if view.read_error and view.read_error != "not_found":
            result.findings.append(Finding(
                code="SLK000",
                severity="warn",
                title=f"Could not read openclaw.json for {bot_id}",
                detail=f"read error: {view.read_error}",
            ))
        return result

    if view.parse_error is not None:
        result.findings.append(Finding(
            code="SLK000",
            severity="fail",
            title=f"openclaw.json is unparseable for {bot_id}",
            detail=view.parse_error,
        ))
        return result

    # Telegram-only / Discord-only / unconfigured bots: stop here.
    # Without a `channels.slack` block AND without a Slack token, the
    # bot has no Slack presence and the rest of the doctor would either
    # no-op or false-positive (most notably SLK003 on the top-level
    # `messages.groupChat.visibleReplies` field — a Slack-specific
    # setting that fires on non-Slack bots otherwise, personal_bot-2026-05-19).
    # SLK011 above is the only check whose policy-file drift signal is
    # meaningful pre-render, so it stays before this gate.
    if not view.slack_provider_present:
        return result

    # SLK012 — Slack integration administratively disabled
    # (channels.slack.enabled === false). Surfaces explicitly so an
    # operator who set this months ago and forgot doesn't wonder why
    # nothing works. INFO severity, no FAIL — the operator may have
    # disabled it intentionally (admin_bot's pattern).
    #
    # When disabled we stop here: continuing would emit FAIL-level
    # findings (SLK001 channel-key shape, SLK007 auth.test) about
    # config the operator has explicitly turned off. admin_bot-2026-05-28
    # showed this as three loud ✗ lines per deploy on a Telegram-only
    # bot just because a dormant slack block was still present. The
    # operator's next step is either re-enable or remove the block,
    # both of which SLK012's detail already spells out.
    if view.slack_enabled is False:
        result.findings.append(Finding(
            code="SLK012",
            severity="info",
            title=f"{view.bot_id}: Slack integration is administratively disabled",
            detail=(
                "channels.slack.enabled is false. The bot will not process "
                "Slack events regardless of other config. Set enabled=true "
                "in openclaw.json to re-enable, or remove the slack block "
                "entirely if Slack isn't this bot's channel."
            ),
        ))
        return result

    # SLK013 — Slack policy enum validation. dmPolicy "open" is special:
    # docs require allowFrom to contain "*" — without that, "open" is
    # interpreted as "no one" and silently fails for actual users.
    _check_policy_enums(view, result)

    # SLK015 — any active streaming mode in a team channel broadcasts
    # intermediate tool output to the channel (team_bot_a-2026-05-14 / 05-15).
    # `block` is NOT the safe option earlier doctor logic claimed; only
    # `off` is truly silent.
    _check_streaming_mode_trap(view, result)

    # SLK016 — independent of mode, `streaming.progress.commandText: "raw"`
    # (the default) leaks the literal command preview into whatever
    # surface streams.
    _check_command_text_leak(view, result)

    # SLK017 — workspace directory presence + staleness.
    # SLK018 — users:read.email scope (enriches directory).
    # Both require shared_dir context; only run in policy-aware mode.
    if shared_dir is not None and view.bot_token:
        _check_workspace_directory(bot_id, shared_dir, result)

    # SLK001 — name-keyed channels under allowlist (always checked, even
    # without a token; we can detect this purely from openclaw.json).
    _check_name_keyed_channels(view, result)

    # SLK003 — reply visibility default. Pure-config check; no Slack API.
    _check_visible_replies_default(view, result)

    # SLK010 — `groupPolicy: allowlist` with empty entries. Pure-config.
    # (Promoted to FAIL only when SLK002 confirms the bot is in *some*
    # channel that should have routed.)
    slk010_state = _check_allowlist_emptiness(view, result)

    # Everything below needs a working bot token. If absent, we surface
    # SLK007 at INFO and stop — the bot isn't installed yet, that's not
    # a failure of the doctor.
    if not view.bot_token:
        result.findings.append(Finding(
            code="SLK007",
            severity="info",
            title=f"{bot_id} has no Slack bot token configured",
            detail="No xoxb-... token found in openclaw.json#channels.slack.botToken "
                   "or ~/.openclaw/workspace/.env#SLACK_BOT_TOKEN. "
                   "Connect Slack via the admin UI to enable the bot.",
        ))
        _resolve_pending_slk010(slk010_state, member_count=None, result=result)
        return result

    # Build the Slack client (factory hook for tests).
    if slack_client_factory is not None:
        client = slack_client_factory(view.bot_token)
    else:
        try:
            client = SlackClient(view.bot_token)
        except ValueError as exc:
            result.findings.append(Finding(
                code="SLK007",
                severity="fail",
                title=f"Invalid Slack bot token shape for {bot_id}",
                detail=str(exc),
            ))
            _resolve_pending_slk010(slk010_state, member_count=None, result=result)
            return result

    # SLK007 — auth.test
    auth, auth_err = client.auth_test()
    if auth_err is not None:
        result.findings.append(Finding(
            code="SLK007",
            severity="fail",
            title=f"Slack auth.test failed for {bot_id}",
            detail=str(auth_err),
        ))
        # Without auth, we can't run any of the channel/user checks.
        _resolve_pending_slk010(slk010_state, member_count=None, result=result)
        return result

    result.auth_test = auth or {}
    workspace_team_id = (auth or {}).get("team_id")

    # OAuth scope evaluation (no Finding emitted — purely informational).
    # The doctor can't know which features the operator *wants*, so
    # firing FAILs on missing scopes is over-eager. Instead we surface
    # the checklist on DoctorResult and let the status header render
    # "✓ Respond to @-mentions, ✗ React with emoji (missing
    # reactions:write)". The user's frustration during team_bot_a's setup was
    # discovering missing scopes one feature-break at a time — this
    # checklist is the antidote.
    scopes_list = (auth or {}).get("_scopes") or []
    if isinstance(scopes_list, list):
        result.oauth_scopes = [str(s) for s in scopes_list if isinstance(s, str)]
        from .scopes import (
            ELEVATED_SCOPES, elevated_scopes_present, evaluate_bundles,
        )
        scope_set = set(result.oauth_scopes)
        result.feature_bundles = evaluate_bundles(scope_set)
        result.elevated_scopes_granted = list(elevated_scopes_present(scope_set))

    # SLK008 — team_id mismatch (advisory: we don't currently store the
    # workspace team_id anywhere durable, so we surface this as INFO
    # carrying the observed team_id. Phase 2's policy file will gain a
    # ``workspace.team_id`` field and this check will compare against it.)
    if workspace_team_id:
        result.findings.append(Finding(
            code="SLK008",
            severity="info",
            title=f"Slack workspace for {bot_id}: {(auth or {}).get('team') or workspace_team_id}",
            detail=f"team_id={workspace_team_id} — Phase 2 will compare this "
                   f"against a stored team_id to catch token swaps.",
        ))

    # users.conversations — needed for SLK002, SLK005, and resolving SLK010
    channels, conv_err = client.users_conversations()
    if conv_err is not None:
        result.findings.append(Finding(
            code="SLK007",
            severity="warn",
            title=f"Could not list channels for {bot_id}",
            detail=f"users.conversations failed: {conv_err}",
        ))
        _resolve_pending_slk010(slk010_state, member_count=None, result=result)
        return result

    result.bot_member_channels = list(channels or [])
    member_by_id = {
        c.get("id"): c for c in (channels or []) if isinstance(c.get("id"), str)
    }

    # SLK002 — keyed ID points to a channel the bot isn't a member of.
    # (May issue a lazy ``users.conversations(types="mpim")`` call to tell a
    # misplaced group-DM entry apart from a genuinely-missing channel.)
    _check_bot_membership(view, member_by_id, client, result)

    # SLK004 — DM allowlist references unknown user IDs.
    _check_dm_allowlist(view, client, result)

    # SLK005 — bot is a member of a channel not in entries.
    _check_uncovered_channels(view, member_by_id, result)

    # SLK009 — channel name in `display_name` cache differs from current.
    # We don't yet cache a `channel_name` per entry in openclaw.json, so
    # this is best-effort: only fires when a Slack-ID-keyed entry has a
    # `displayName` sibling field and Slack reports a different name.
    _check_channel_renames(view, member_by_id, result)

    # Resolve SLK010 now that we know whether the bot is in any channels.
    _resolve_pending_slk010(slk010_state, member_count=len(member_by_id), result=result)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────


_VALID_DM_POLICIES = {"pairing", "allowlist", "open", "disabled"}
_VALID_GROUP_POLICIES = {"open", "allowlist", "disabled"}
_VALID_STREAMING_MODES = {"partial", "block", "off"}


def _check_streaming_mode_trap(
    view: OpenclawSlackView,
    result: DoctorResult,
) -> None:
    """SLK015 — any active streaming mode in a team channel broadcasts
    intermediate tool output to everyone in the channel.

    **Correction (post-#1109 verification on 2026-05-15):** per
    docs.openclaw.ai/channels/slack, ``"block"`` *also* streams
    "chunked preview updates" — it's not the clean 'thinking…'
    indicator that prior SLK015 logic (and team_bot_a's own self-primer)
    treated it as. Confirmed against team_bot_a on the production mini:
    team_bot_a with ``streaming.mode: "block"`` still posted walls of
    ``• command run ... • tool: exec ...`` into a team channel. Only
    ``"off"`` is truly silent.

    So the hazard is *any streaming mode != "off"* paired with a team
    channel, not just ``"partial"``:

    - ``streaming.mode != "off"`` AND any channel has
      ``requireMention: false`` → **FAIL** (every message triggers a
      streamed message visible to the whole channel).
    - ``streaming.mode != "off"`` AND only mention-only channels →
      **WARN** (still streams on @-mentions but only when explicitly
      addressed).
    - ``streaming.mode == "partial"`` is the noisiest end of the
      streaming spectrum; the detail names the recommendation
      (``"off"``) explicitly.
    - DM-only bot or empty channel list → no finding (streaming is
      designed for personal DMs).

    The check is conservative when streaming.mode is absent (INFO,
    so the operator locks the value explicitly — OC has flipped
    streaming defaults silently before).

    SLK016 (``progress.commandText``) runs independently — even if
    streaming.mode is acceptable, ``commandText: "raw"`` can still
    leak the literal command preview into whatever surface streams.
    """
    sm = view.streaming_mode

    listens_all = [e for e in view.channel_entries if e.require_mention is False]
    mention_only = [e for e in view.channel_entries if e.require_mention is True]
    has_team_channels = bool(listens_all or mention_only)

    if not isinstance(sm, str):
        # Unset → OC's default. We don't know it, and OC has flipped
        # defaults silently before (bug-2 visibleReplies). Surface as
        # INFO so the operator can opt to set it explicitly.
        if has_team_channels:
            result.findings.append(Finding(
                code="SLK015",
                severity="info",
                title=f"{view.bot_id}: channels.slack.streaming.mode is unset",
                detail=(
                    "OC's default may stream intermediate tool calls to "
                    "team channels (team_bot_a-2026-05-14 incident shape). Set "
                    "streaming.mode explicitly to \"off\" for any bot in a "
                    "team channel — \"partial\" and \"block\" both stream "
                    "content to the channel per OC docs."
                ),
            ))
        return

    if sm not in _VALID_STREAMING_MODES:
        result.findings.append(Finding(
            code="SLK015",
            severity="warn",
            title=f"{view.bot_id}: unknown streaming.mode value {sm!r}",
            detail=(
                f"channels.slack.streaming.mode={sm!r} is not one of the "
                f"documented values ({sorted(_VALID_STREAMING_MODES)}). "
                f"OC may treat this as a default; behavior is undefined."
            ),
        ))
        return

    if sm == "off":
        # The only fully clean mode. No finding regardless of channel mix.
        return

    if not has_team_channels:
        # DM-only bot — streaming is fine. No finding.
        return

    # streaming.mode in {"partial", "block"} AND has team channels.
    native_transport = view.streaming_native_transport is True
    detail_suffix = (
        " The combination with `nativeTransport: true` (team_bot_a-2026-05-14 shape) "
        "surfaces tool-call boundaries explicitly via Slack's native streaming "
        "API."
        if native_transport else ""
    )
    mode_explainer = {
        "partial": (
            "Per OC docs, \"partial\" replaces the draft message with "
            "the latest partial output as the response generates."
        ),
        "block": (
            "Per OC docs, \"block\" appends chunked preview updates — "
            "users see accumulating text blocks added sequentially. "
            "It's NOT a quiet 'thinking…' indicator (verified against "
            "team_bot_a on 2026-05-15)."
        ),
    }[sm]

    if listens_all:
        # The catastrophic combination — every message routes.
        channels_str = ", ".join(e.key for e in listens_all[:3])
        if len(listens_all) > 3:
            channels_str += f", + {len(listens_all) - 3} more"
        result.findings.append(Finding(
            code="SLK015",
            severity="fail",
            title=(
                f"{view.bot_id}: streaming.mode={sm!r} with "
                f"{len(listens_all)} listens-all team channel(s) "
                f"({channels_str}) — broadcasts internal tool calls"
            ),
            detail=(
                f"{mode_explainer} In a team channel where the bot processes "
                f"every message (requireMention: false), every conversation "
                f"becomes a wall of internal scaffolding visible to the whole "
                f"team. The only safe value for a bot in a team channel is "
                f"streaming.mode: \"off\". Keep \"partial\" or \"block\" "
                f"only for personal-bot DMs." + detail_suffix
            ),
        ))
    else:
        # mention-only channels — still leaks on @-mentions but only
        # when the bot is explicitly addressed.
        result.findings.append(Finding(
            code="SLK015",
            severity="warn",
            title=(
                f"{view.bot_id}: streaming.mode={sm!r} with "
                f"{len(mention_only)} team channel(s)"
            ),
            detail=(
                f"{mode_explainer} Even @-mention-only channels see the "
                f"streamed content when the bot is mentioned. For team "
                f"channels, prefer streaming.mode: \"off\" so the bot "
                f"replies once with a final answer." + detail_suffix
            ),
        ))


def _check_workspace_directory(
    bot_id: str,
    shared_dir: Path,
    result: DoctorResult,
) -> None:
    """SLK017 + SLK018 — workspace identity directory checks.

    SLK017 fires when the directory is missing or stale (>24h since
    last refresh). The directory is the team_bot_a-2026-05-15 fix: it maps
    every workspace user's full identity tuple (Slack ID, legacy name,
    display name, real name, email) so the bot doesn't confuse
    aliases. Without it, the bot is back to guessing.

    SLK018 (INFO) fires when ``users:read.email`` isn't in the bot's
    OAuth scope set — email is one of the most useful disambiguation
    anchors, but it requires the operator to add the scope and
    reinstall the app. Surfaced so the operator knows the option
    exists.
    """
    from .directory import (
        directory_age_hours,
        directory_path,
        is_stale,
        load_directory,
    )

    path = directory_path(shared_dir, bot_id)
    try:
        directory = load_directory(shared_dir, bot_id)
    except ValueError as exc:
        result.findings.append(Finding(
            code="SLK017",
            severity="fail",
            title=f"{bot_id}: slack-directory.json is malformed",
            detail=(
                f"{exc} — the workspace directory exists but cannot be loaded. "
                f"Until it's regenerated, the bot may confuse Slack aliases "
                f"(name vs display_name vs real_name). Run "
                f"[bold]evolve-admin slack-directory {bot_id} --refresh[/]."
            ),
        ))
        return
    if directory is None:
        result.findings.append(Finding(
            code="SLK017",
            severity="warn",
            title=f"{bot_id}: no workspace identity directory",
            detail=(
                f"No file at {path}. The bot may confuse Slack aliases "
                f"(e.g. 'pod_admin' vs 'Pod_admin' vs the Slack ID — team_bot_a-2026-05-15 "
                f"incident shape). Run "
                f"[bold]evolve-admin slack-directory {bot_id} --refresh[/] "
                f"to build one."
            ),
        ))
        return

    if is_stale(directory):
        age = directory_age_hours(directory)
        age_str = f"{age:.0f}h ago" if age is not None else "unknown"
        result.findings.append(Finding(
            code="SLK017",
            severity="warn",
            title=f"{bot_id}: workspace directory is stale (last refreshed {age_str})",
            detail=(
                f"Last refresh: {directory.last_refreshed_at}. New workspace "
                f"members or deactivated users won't be reflected. Run "
                f"[bold]evolve-admin slack-directory {bot_id} --refresh[/]."
            ),
        ))

    # SLK018 — users:read.email scope hint.
    # Only fire if we ran the doctor against live scopes and they don't
    # include users:read.email. We don't fire on a fresh directory that
    # was generated before the doctor ran, because we don't know the
    # current scope set in that branch.
    if result.oauth_scopes and "users:read.email" not in set(result.oauth_scopes):
        result.findings.append(Finding(
            code="SLK018",
            severity="info",
            title=f"{bot_id}: users:read.email scope not granted",
            detail=(
                "The bot's Slack app doesn't have `users:read.email` — the "
                "workspace directory can't include the `email` column, which "
                "is one of the strongest disambiguation anchors. Add the "
                "scope at api.slack.com/apps → OAuth & Permissions → Bot "
                "Token Scopes → Add `users:read.email`, reinstall, then "
                "refresh the directory."
            ),
        ))


def _check_command_text_leak(
    view: OpenclawSlackView,
    result: DoctorResult,
) -> None:
    """SLK016 — ``streaming.progress.commandText: "raw"`` (or unset)
    surfaces the literal command/exec text into the streamed message
    body when streaming is active in a team channel.

    Per docs.openclaw.ai/channels/slack:

      > set [commandText] to "status" to keep compact tool-progress
      > lines while hiding raw command/exec text (default: "raw")

    The default ``"raw"`` is what produces the
    ``• command run python3 maintenance/maintenance_tracker.py ...`` text
    in the team_bot_a-2026-05-15 incident. This check is independent of
    SLK015: even if the operator switches streaming.mode to a less
    aggressive value, ``commandText: "raw"`` still leaks the literal
    text into whatever surface streams.

    Fires only when streaming is *active* (mode != "off") AND the
    bot has any team channels. When mode == "off", no commands are
    rendered anywhere; commandText is moot.
    """
    sm = view.streaming_mode
    # Only fire when streaming is known to be active. When mode is
    # unset, unknown, or "off", SLK015 already covers the case (INFO
    # for unset, WARN for unknown, no finding for off). Avoiding
    # double-fire keeps the operator's signal-to-noise ratio clean.
    if sm not in {"partial", "block"}:
        return
    has_team_channels = bool(view.channel_entries)
    if not has_team_channels:
        return

    ct = view.streaming_command_text
    # "status" is the safe value. "raw", unset, or anything else
    # (per the docs the default is "raw") fires.
    if ct == "status":
        return

    actual = ct if isinstance(ct, str) else "<unset — defaults to 'raw'>"
    result.findings.append(Finding(
        code="SLK016",
        severity="warn",
        title=(
            f"{view.bot_id}: streaming.progress.commandText="
            f"{actual} leaks raw command text"
        ),
        detail=(
            f"channels.slack.streaming.progress.commandText is {actual}. "
            f"Per OC docs, this surfaces the literal command/exec text "
            f"in streamed updates — that's the source of the "
            f"\"• command run python3 ...\" wall the team sees. Set it to "
            f"\"status\" to keep compact progress lines while hiding the "
            f"raw command text, or set streaming.mode to \"off\" (SLK015's "
            f"recommendation) to suppress streaming entirely."
        ),
    ))


def _check_policy_enums(
    view: OpenclawSlackView,
    result: DoctorResult,
) -> None:
    """SLK013 — validate dmPolicy / groupPolicy values and their constraints.

    Per docs.openclaw.ai/channels/slack:
    - dmPolicy ∈ {pairing, allowlist, open, disabled}
    - groupPolicy ∈ {open, allowlist, disabled}
    - dmPolicy=="open" requires allowFrom contains "*" (otherwise
      "open" silently means "no one", a real silent-failure trap).

    Unknown values warn. The "disabled" values surface as INFO so an
    operator who set it months ago and forgot can find it.
    """
    dm = view.dm_policy
    if isinstance(dm, str) and dm not in _VALID_DM_POLICIES:
        result.findings.append(Finding(
            code="SLK013",
            severity="warn",
            title=f"{view.bot_id}: unknown dmPolicy value {dm!r}",
            detail=(
                f"channels.slack.dmPolicy={dm!r} is not one of the documented "
                f"values ({sorted(_VALID_DM_POLICIES)}). Slack/OC may treat "
                f"this as a default; behavior is undefined. Pick a documented "
                f"value or remove the field to use the default."
            ),
        ))
    if dm == "disabled":
        result.findings.append(Finding(
            code="SLK013",
            severity="info",
            title=f"{view.bot_id}: DMs to the bot are disabled (dmPolicy=disabled)",
            detail="No one can DM the bot — only channel interactions work.",
        ))
    if dm == "open":
        # Per docs: open requires allowFrom: ["*"]. Without the wildcard
        # entry, an "open" dmPolicy silently denies every actual user.
        if "*" not in view.slack_allow_from_user_ids:
            result.findings.append(Finding(
                code="SLK013",
                severity="fail",
                title=f"{view.bot_id}: dmPolicy=\"open\" but allowFrom does not contain \"*\"",
                detail=(
                    "OC documents that dmPolicy=\"open\" requires "
                    "allowFrom: [\"*\"] — otherwise no actual user passes the "
                    "gate and DMs silently fail. Either add \"*\" to "
                    "channels.slack.allowFrom or switch dmPolicy to \"pairing\"/"
                    "\"allowlist\" to make the user list authoritative."
                ),
            ))

    gp = view.group_policy
    if isinstance(gp, str) and gp not in _VALID_GROUP_POLICIES:
        result.findings.append(Finding(
            code="SLK013",
            severity="warn",
            title=f"{view.bot_id}: unknown groupPolicy value {gp!r}",
            detail=(
                f"channels.slack.groupPolicy={gp!r} is not one of the "
                f"documented values ({sorted(_VALID_GROUP_POLICIES)})."
            ),
        ))
    if gp == "disabled":
        result.findings.append(Finding(
            code="SLK013",
            severity="info",
            title=f"{view.bot_id}: channel routing is disabled (groupPolicy=disabled)",
            detail="The bot won't process any channel messages, even @-mentions.",
        ))


def _check_name_keyed_channels(
    view: OpenclawSlackView,
    result: DoctorResult,
) -> None:
    """SLK001 — name-keyed channel entries.

    Always FAIL when present. OC routes by Slack ID regardless of
    ``groupPolicy`` (default behavior has shifted at least once in the
    2026 release stream), so name-keying is a silent drop the operator
    won't catch any other way. Per spec §5 the severity is FAIL —
    we don't condition on ``groupPolicy`` because the operator's
    intent is unambiguous and the only outcomes are "matches" (FAIL
    here is wrong) or "doesn't match" (FAIL here is right). The
    historical OC mode where name-keys did match is not present in
    any version this pod runs.

    The ``--fix`` path doesn't run in the doctor — name→ID resolution
    requires the Slack client (to look up each name in the bot's
    member-channels list), so the CLI owns that pass after auth.test
    has succeeded.
    """
    bad_entries = [
        entry for entry in view.channel_entries
        if not is_slack_channel_id(entry.key)
    ]
    for entry in bad_entries:
        result.findings.append(Finding(
            code="SLK001",
            severity="fail",
            title=f"{view.bot_id}: channel keyed by name (\"{entry.key}\")",
            detail=(
                f"channels.\"{entry.key}\" is keyed by name, not Slack ID. "
                f"OC routing keys channels by ID (C… or G…); this entry "
                f"will not match incoming events. Rekey to the channel's "
                f"Slack ID. groupPolicy={view.group_policy!r}."
            ),
            fixable=True,
        ))


def _check_visible_replies_default(
    view: OpenclawSlackView,
    result: DoctorResult,
) -> None:
    """SLK003 — ``messages.groupChat.visibleReplies`` missing or unexpected.

    The bug-2 silent-suppression trap. We require an explicit value:
    omission means "inherit whatever the running OC version defaults
    to", and that default flipped under us once (2026.4.27).
    """
    vr = view.visible_replies_default
    if vr is None:
        result.findings.append(Finding(
            code="SLK003",
            severity="warn",
            title=f"{view.bot_id}: messages.groupChat.visibleReplies is unset",
            detail=(
                f"OpenClaw's built-in default has changed at least once in 2026 "
                f"(silently suppressing channel replies). Set "
                f"messages.groupChat.visibleReplies to {EXPECTED_VISIBLE_REPLIES!r} "
                f"explicitly so a future default change can't break this bot."
            ),
        ))
        return
    if vr != EXPECTED_VISIBLE_REPLIES:
        result.findings.append(Finding(
            code="SLK003",
            severity="info",
            title=f"{view.bot_id}: visibleReplies = {vr!r}",
            detail=(
                f"Non-default visibleReplies value ({vr!r}). This is allowed; "
                f"the doctor just confirms it's intentional rather than drift."
            ),
        ))


def _check_allowlist_emptiness(
    view: OpenclawSlackView,
    result: DoctorResult,
) -> "_PendingFinding | None":
    """SLK010 — ``groupPolicy: allowlist`` with no entries.

    Cannot be resolved until we know whether the bot is a member of
    *any* channels in Slack. Returns a pending-finding sentinel that
    :func:`_resolve_pending_slk010` finalizes once the API call completes
    (or doesn't). When the API never gets called, falls back to WARN.
    """
    is_allowlist = (view.group_policy or "").lower() == "allowlist"
    has_entries = any(view.channel_entries)
    if not is_allowlist or has_entries:
        return None
    # Pending — resolution depends on Slack member count.
    return _PendingFinding(
        code="SLK010",
        title=f"{view.bot_id}: allowlist policy with no channel entries",
        detail=(
            "channels.slack.groupPolicy is 'allowlist' but no channel entries are "
            "present. The bot will not route any incoming Slack events. "
            "Either add channel entries (one per channel the bot should listen "
            "in), or change groupPolicy to allow channel-level matching."
        ),
    )


def _check_bot_membership(
    view: OpenclawSlackView,
    member_by_id: dict[str, dict[str, Any]],
    client: SlackClient,
    result: DoctorResult,
) -> None:
    """SLK002 — keyed channel ID points to a channel the bot isn't in.

    Only inspects entries whose key is a Slack-ID-shaped string —
    name-keyed entries are handled by SLK001 and shouldn't double-fire
    here (a name like ``"management"`` is never a member-of result).

    **Group-DM (mpim) carve-out — SLK019.** ``member_by_id`` is built from
    ``users.conversations`` with the default
    ``types="public_channel,private_channel"``; it never contains group
    DMs. In newer workspaces a group DM's conversation ID is
    ``C``-prefixed (verified: ``C0B7PDLM2PJ``), so
    :func:`is_slack_channel_id` cannot tell a group DM apart from a real
    channel by shape. A group-DM ID in the channel allowlist would
    therefore always miss ``member_by_id`` and fire a false SLK002 FAIL —
    blocking a Phase-2 deploy — even when the bot is genuinely a
    participant in that group DM.

    OpenClaw never routes mpim messages through
    ``channels.slack.channels`` / ``groupPolicy``: group DMs are DM-policy
    territory (``dmPolicy`` + ``channels.slack.dm.groupEnabled`` +
    ``dm.groupChannels`` + ``allowFrom`` — verified against the OC config
    schema and docs.openclaw.ai/channels/slack). So a group-DM ID in the
    channel allowlist is an inert, misplaced entry, not a silent-drop
    failure. We resolve mpim membership *separately* — deliberately NOT
    merging it into ``member_by_id`` so it can't pollute the SLK005
    uncovered-channel scan (which is channel-allowlist territory) — and,
    for an ID the bot actually participates in as a group DM, emit SLK019
    (WARN, misplaced-entry hygiene) instead of SLK002 (FAIL).
    """
    # Candidates: ID-keyed entries absent from the public/private
    # membership map. Only these can fire SLK002 — and only these are
    # worth an mpim lookup, so the extra API call stays off the happy path.
    candidates = [
        entry for entry in view.channel_entries
        if is_slack_channel_id(entry.key) and entry.key not in member_by_id
    ]
    if not candidates:
        return

    # Lazily resolve group-DM (mpim) membership. ``None`` means the lookup
    # was inconclusive (missing mpim:read scope, transport error); we then
    # fall back to the historical SLK002 FAIL rather than risk suppressing
    # a genuinely-missing channel.
    mpim_member_ids: set[str] | None = None
    mpims, mpim_err = client.users_conversations(types="mpim")
    if mpim_err is None:
        mpim_member_ids = set()
        for c in (mpims or []):
            cid = c.get("id")
            if isinstance(cid, str):
                mpim_member_ids.add(cid)
    else:
        log.debug(
            "slack-doctor %s: mpim membership lookup failed (%s); "
            "SLK002 candidates fall back to FAIL",
            view.bot_id, mpim_err,
        )

    for entry in candidates:
        if mpim_member_ids is not None and entry.key in mpim_member_ids:
            # Bot IS a participant of this group DM. The channel-allowlist
            # entry is inert (OC ignores it for mpim routing) but not a
            # silent-drop failure — flag it as misplaced, not FAIL.
            result.findings.append(Finding(
                code="SLK019",
                severity="warn",
                title=f"{view.bot_id}: {entry.key} is a group DM, not a channel",
                detail=(
                    f"channels.\"{entry.key}\" is in the channels.slack.channels "
                    f"allowlist, but {entry.key} is a group DM (multi-person DM) "
                    f"the bot participates in — not a channel. OpenClaw routes "
                    f"group DMs through the DM subsystem (channels.slack.dmPolicy "
                    f"/ dm.groupEnabled / dm.groupChannels), never through the "
                    f"channel allowlist, so this entry has no effect. Remove it; "
                    f"to control the bot's behavior in group DMs use dmPolicy / "
                    f"dm.groupEnabled."
                ),
                channel_id=entry.key,
            ))
            continue
        result.findings.append(Finding(
            code="SLK002",
            severity="fail",
            title=f"{view.bot_id}: bot is not a member of {entry.key}",
            detail=(
                f"channels.\"{entry.key}\" is in openclaw.json but the bot's Slack "
                f"identity is not a member of that channel. Slack will not deliver "
                f"events to non-member bots regardless of config. "
                f"Invite the bot to the channel in Slack, or remove this entry."
            ),
            channel_id=entry.key,
        ))


def _check_dm_allowlist(
    view: OpenclawSlackView,
    client: SlackClient,
    result: DoctorResult,
) -> None:
    """SLK004 — Slack ``allowFrom`` user IDs the workspace doesn't recognize.

    Best-effort: one ``users.info`` per ID. Skips silently when the
    list is empty (the common case — DMs are open by default).

    On first transport/HTTP error we abort the per-user loop and
    emit a single aggregate WARN. The earlier per-user WARN pattern
    produced a signal storm when a workspace was briefly unreachable
    (N WARNs × 15-minute probe interval × every cycle until the
    network recovered).
    """
    for uid in view.slack_allow_from_user_ids:
        if not (uid.startswith("U") or uid.startswith("W")):
            # Not even ID-shaped — definitely won't resolve. Flag and skip.
            result.findings.append(Finding(
                code="SLK004",
                severity="fail",
                title=f"{view.bot_id}: channels.slack.allowFrom entry {uid!r} is not a user ID",
                detail=(
                    "Slack user IDs start with U or W. Either rekey this entry to "
                    "the user's Slack ID (use the admin UI's user picker), or "
                    "remove it from the allowlist."
                ),
                user_id=uid,
            ))
            continue
        _, err = client.users_info(uid)
        if err is None:
            continue
        if err.code == "api_error" and err.slack_error in {"user_not_found", "user_not_visible"}:
            result.findings.append(Finding(
                code="SLK004",
                severity="fail",
                title=f"{view.bot_id}: channels.slack.allowFrom references unknown user {uid}",
                detail=(
                    f"users.info returned {err.slack_error!r} for {uid}. "
                    f"The user may have left the workspace or been deactivated. "
                    f"Remove the entry or replace it with a current user ID."
                ),
                user_id=uid,
            ))
            continue
        # Transport/HTTP error or unfamiliar API error — abort the loop
        # with one aggregate WARN instead of N per-user WARNs. The probe
        # will re-run the whole check next cycle.
        result.findings.append(Finding(
            code="SLK004",
            severity="warn",
            title=f"{view.bot_id}: user allowlist verification inconclusive",
            detail=(
                f"users.info failed on {uid} ({err}); skipping remaining "
                f"allowlist entries this run to avoid signal storm. Will "
                f"retry next probe cycle."
            ),
        ))
        return


def _check_uncovered_channels(
    view: OpenclawSlackView,
    member_by_id: dict[str, dict[str, Any]],
    result: DoctorResult,
) -> None:
    """SLK005 — bot is a member of channels not in the allowlist.

    INFO severity by design — the operator may have intentionally
    invited the bot to a channel and chosen not to make it active.
    Phase 3's "bot invited, want me to listen?" DM flow will use the
    Signal this produces.

    Coverage set includes both ID-keyed entries (the canonical form)
    AND name-keyed entries whose stripped name resolves to a member
    channel's name. Otherwise SLK001 (name-keyed) and SLK005 (uncovered)
    double-fire on the same channel and the operator may "fix" SLK005
    by adding a redundant entry instead of fixing the underlying
    name-keying.
    """
    is_allowlist = (view.group_policy or "").lower() == "allowlist"
    if not is_allowlist:
        # Without allowlist, every channel is implicitly covered.
        return

    name_to_id = {
        ch.get("name"): cid for cid, ch in member_by_id.items()
        if isinstance(ch.get("name"), str)
    }
    covered_ids: set[str] = set()
    for entry in view.channel_entries:
        if is_slack_channel_id(entry.key):
            covered_ids.add(entry.key)
            continue
        # Best-effort name resolution — the operator wrote "project-x"
        # or "#project-x"; if Slack reports a channel with that name
        # the operator's intent is clearly to cover that channel.
        stripped = entry.key.lstrip("#")
        resolved = name_to_id.get(stripped)
        if isinstance(resolved, str):
            covered_ids.add(resolved)

    for cid, ch in member_by_id.items():
        if cid in covered_ids:
            continue
        name = ch.get("name") or cid
        result.findings.append(Finding(
            code="SLK005",
            severity="info",
            title=f"{view.bot_id}: invited to #{name} but not listening",
            detail=(
                f"Bot is a member of channel {cid} (#{name}) but no openclaw.json "
                f"entry routes it. Add an entry under channels.\"{cid}\" "
                f"({{\"requireMention\": true}} is the safe default for a busy channel), "
                f"or remove the bot from the channel in Slack."
            ),
            channel_id=cid,
        ))


def _check_policy_drift(
    bot_id: str,
    shared_dir: Path,
    view: OpenclawSlackView,
    result: DoctorResult,
) -> None:
    """SLK011 — policy file exists but openclaw.json doesn't match the render.

    Phase 2 only. Fires when:
    - the policy is present and valid
    - rendering it against the current openclaw.json would change
      something the writer owns

    Common causes: someone hand-edited openclaw.json, the writer
    crashed mid-write, a policy save raced a deploy. The remediation
    is to re-run the writer; the CLI exposes ``evolve-admin
    slack-policy apply <bot>`` for that.

    A malformed policy file is reported as FAIL — the bot's runtime
    config is intact but the operator's source of truth is broken and
    needs human attention.
    """
    from .policy import load_policy
    from .writer import is_render_up_to_date

    try:
        policy = load_policy(shared_dir, bot_id)
    except ValueError as exc:
        result.findings.append(Finding(
            code="SLK011",
            severity="fail",
            title=f"{bot_id}: slack-policy.json is malformed",
            detail=(
                f"{exc} — the policy file exists but cannot be loaded. "
                f"Until it's fixed, the writer cannot render and the bot's "
                f"openclaw.json may drift from operator intent."
            ),
        ))
        return
    if policy is None:
        return

    if not view.openclaw_present:
        # Policy exists, openclaw.json missing — bot hasn't been deployed.
        # Not a drift; just nothing to render against yet.
        return
    if view.parse_error:
        # Will be reported separately by the main loop; no SLK011.
        return

    # Use the same sudo-fallback reader the rest of the doctor uses —
    # otherwise drift goes invisible on bots where the openclaw user
    # has restrictive perms but ACL inheritance hasn't run yet
    # (deploy-time race during initial provisioning).
    from .oc_config import read_bot_text_file

    existing_text, _ = read_bot_text_file(view.openclaw_path)
    if existing_text is None:
        return
    try:
        existing = json.loads(existing_text)
    except json.JSONDecodeError:
        return
    if not isinstance(existing, dict):
        return

    if is_render_up_to_date(existing=existing, policy=policy):
        return

    result.findings.append(Finding(
        code="SLK011",
        severity="warn",
        title=f"{bot_id}: openclaw.json has drifted from slack-policy.json",
        detail=(
            "The policy file is the source of truth in Phase 2 mode, but "
            "the bot's openclaw.json doesn't match what the writer would "
            "render. Re-apply via [bold]evolve-admin slack-policy apply "
            f"{bot_id}[/]."
        ),
    ))


def _check_channel_renames(
    view: OpenclawSlackView,
    member_by_id: dict[str, dict[str, Any]],
    result: DoctorResult,
) -> None:
    """SLK009 — cached display name differs from current Slack name.

    Only fires when an entry's ``raw`` dict has a ``displayName``
    sibling (Phase 2's policy file will cache this; Phase 1 finds it
    only where an operator hand-recorded it). Strictly informational.
    """
    for entry in view.channel_entries:
        if not is_slack_channel_id(entry.key):
            continue
        cached = entry.raw.get("displayName")
        if not isinstance(cached, str) or not cached:
            continue
        current = member_by_id.get(entry.key, {}).get("name")
        if not isinstance(current, str) or not current:
            continue
        if cached.lstrip("#").lower() == current.lower():
            continue
        result.findings.append(Finding(
            code="SLK009",
            severity="info",
            title=f"{view.bot_id}: channel {entry.key} renamed (#{cached} → #{current})",
            detail=(
                f"openclaw.json caches the display name as #{cached}; Slack now "
                f"reports #{current}. The bot continues to route correctly "
                f"(routing is by ID, not name) — update the cached name when "
                f"convenient for clarity."
            ),
            channel_id=entry.key,
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — pending finding for SLK010, name→ID fix path
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _PendingFinding:
    code: str
    title: str
    detail: str


def _resolve_pending_slk010(
    pending: _PendingFinding | None,
    *,
    member_count: int | None,
    result: DoctorResult,
) -> None:
    if pending is None:
        return
    # If we never reached the API call (no token, auth failed, etc.),
    # downgrade to WARN — we know the policy is empty but can't confirm
    # whether it's an actual black-hole.
    if member_count is None:
        result.findings.append(Finding(
            code=pending.code, severity="warn",
            title=pending.title, detail=pending.detail,
        ))
        return
    # API call succeeded. If the bot is in zero channels, the empty
    # allowlist is consistent with not-yet-set-up — INFO. If the bot
    # is in channels, the allowlist is actively dropping their events
    # — FAIL.
    severity: Severity = "fail" if member_count > 0 else "info"
    result.findings.append(Finding(
        code=pending.code, severity=severity,
        title=pending.title, detail=pending.detail,
    ))


def rewrite_openclaw_json_channel_keys(
    *,
    bot_home: Path,
    view: OpenclawSlackView,
    name_to_id: dict[str, str],
) -> tuple[list[str], str | None]:
    """Rewrite ``channels.<name>`` → ``channels.<id>`` for each provided pair.

    Reads the file fresh (avoids racing the view's snapshot), applies
    the rename, writes via ``/tmp`` staging + ``sudo /bin/cp`` per
    CLAUDE.md, and returns ``(rewritten_keys, error_or_none)``.

    Refuses to write if any target ID is *already* a key (would clobber
    a deliberate entry); refuses if the source name isn't present
    (might have been fixed manually since the doctor ran).
    """
    oc_path = bot_home / ".openclaw" / "openclaw.json"
    text, err = _read_file_with_sudo_fallback(oc_path)
    if text is None:
        return [], f"read_failed: {err}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"json_decode: {exc.msg}"
    if not isinstance(data, dict):
        return [], "root_not_object"
    channels_block = data.get("channels")
    if not isinstance(channels_block, dict):
        return [], "no_channels_block"

    rewritten: list[str] = []
    for name, new_id in name_to_id.items():
        if name not in channels_block:
            continue
        if new_id in channels_block:
            return rewritten, f"collision: target key {new_id} already present"
        channels_block[new_id] = channels_block.pop(name)
        rewritten.append(name)

    if not rewritten:
        return [], None

    serialized = json.dumps(data, indent=2)
    write_err = _write_bot_file_with_sudo(oc_path, serialized)
    if write_err is not None:
        return [], f"write_failed: {write_err}"
    return rewritten, None


def _read_file_with_sudo_fallback(path: Path) -> tuple[str | None, str | None]:
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


def _write_bot_file_with_sudo(path: Path, content: str) -> str | None:
    """Write to a bot-owned file via /tmp staging + sudo /bin/cp (CLAUDE.md)."""
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-slack-doctor-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
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


__all__ = [
    "DoctorResult",
    "EXPECTED_VISIBLE_REPLIES",
    "Finding",
    "Severity",
    "rewrite_openclaw_json_channel_keys",
    "run_doctor",
]
