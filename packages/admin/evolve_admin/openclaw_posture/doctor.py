"""OpenClaw Posture Doctor — rule registry and entry point.

Implements OCP-rules per ``docs/spec-openclaw-posture-doctor-2026-05-20.md``.

Phase 0.5 ships **OCP013** (approval-surface gap) only. Later phases add
the snapshot-based rules (OCP001 et al.) — those need pre/post snapshot
infrastructure that doesn't exist yet. OCP013 is single-state and ships
standalone, motivated by the security_bot-Telegram incident on 2026-05-20.

Rule semantics:

- **FAIL** — silent breakage of the bot's primary use case in some
  context. Operator must act before the bot is functional again.
- **WARN** — degradation worth flagging but the bot still works.
- **INFO** — observation, no action required.

The doctor is read-only. It produces ``Finding`` records; emission to
the Signal store happens in the calling layer (see :mod:`cli`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

log = logging.getLogger(__name__)

Severity = Literal["fail", "warn", "info"]


# ─────────────────────────────────────────────────────────────────────────────
# Channel approval-surface capability set
# ─────────────────────────────────────────────────────────────────────────────
#
# Which message channels can interactively surface an exec-approval prompt
# to the operator inside the channel itself, today. This is platform
# knowledge, not config — it lives here and is updated as OC or Evolve
# ships per-channel approval surfaces.
#
# Out-of-band approval surfaces (the OC Control UI, ``openclaw approvals``
# CLI, the TUI) are always available and are not message channels — they
# don't appear in this set. The set is only meaningful for the
# ``channels.*`` keys in openclaw.json.
#
# When a channel ships an approval surface upstream, add it here and the
# corresponding OCP013 findings auto-resolve on the next doctor run.

APPROVAL_SURFACE_CAPABILITY: frozenset[str] = frozenset({
    "slack",  # block-team_bot_a interactive components — works when bot has chat:write
})

# All other channel keys seen in openclaw.json default to NO approval
# surface: telegram, discord, matrix, irc, signal, imessage, feishu,
# msteams, mattermost, nextcloud-talk, googlechat, line, zalo, qqbot,
# whatsapp, etc.


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """One check outcome.

    ``code`` is the stable ``OCP001``..``OCPnnn`` identifier — Signals
    and operator-facing reporting use it as a join key.
    """
    code: str
    severity: Severity
    title: str
    detail: str
    bot_id: str | None = None

    # Anchor metadata — populated by rules whose semantics involve a
    # specific config field or a snapshot delta. OCP013 uses
    # ``channels_without_surface``; later snapshot-based rules use
    # ``field_path`` / ``from_value`` / ``to_value``.
    # Named ``field_path`` (not ``field``) to avoid shadowing
    # ``dataclasses.field`` within the class body.
    field_path: str | None = None
    from_value: Any = None
    to_value: Any = None

    # OCP013-specific: which enabled channels lacked an approval surface.
    # Empty for other rules.
    channels_without_surface: list[str] = field(default_factory=list)

    # Snapshot-based rules will populate; OCP013 leaves empty.
    oc_version_from: str | None = None
    oc_version_to: str | None = None

    # Blast radius — manifest IDs whose scripts/capabilities depend on
    # the regressed field. OCP013 leaves empty; future rules populate
    # via the script→manifest reverse index.
    affected_manifests: list[str] = field(default_factory=list)

    fixable: bool = False
    """True if the doctor has a structured remediation path."""

    fix_summary: str | None = None
    """One-line operator-readable description of what fixing will do."""


@dataclass
class DoctorResult:
    """Aggregate result of running the doctor on one bot."""
    bot_id: str
    findings: list[Finding] = field(default_factory=list)

    # Surfaced state (always populated when meaningful) — useful for the
    # caller's status header without re-parsing the config.
    exec_security: str | None = None
    exec_ask: str | None = None
    enabled_channels: list[str] = field(default_factory=list)
    oc_version: str | None = None

    def has_fail(self) -> bool:
        return any(f.severity == "fail" for f in self.findings)

    def has_warn(self) -> bool:
        return any(f.severity == "warn" for f in self.findings)

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _ask_flow_can_fire(exec_block: dict[str, Any]) -> bool:
    """True if the bot's tool-policy configuration can trigger an
    approval request at exec time.

    Two cases trigger an ask flow:
    1. ``security == "allowlist"`` — any command NOT in the approvals
       allowlist falls through to ask, regardless of the ``ask`` field.
    2. ``ask in {"on-miss", "always"}`` — even with ``security="full"``,
       these ask modes route exec calls through the approval surface.

    ``security="deny"`` short-circuits everything (no exec, no ask).
    ``security="full"`` + ``ask="off"`` is the no-approval-flow case.
    """
    if not isinstance(exec_block, dict):
        return False

    security = exec_block.get("security")
    if security == "allowlist":
        return True

    ask = exec_block.get("ask")
    if ask in ("on-miss", "always"):
        return True

    return False


def _enabled_message_channels(channels_block: dict[str, Any]) -> list[str]:
    """Return the list of message-channel keys whose ``enabled`` is
    truthy (or whose ``enabled`` is absent — OC's default for most
    channels is "on when present").

    OC's per-channel ``enabled`` convention is inconsistent across
    channel types. This helper applies the conservative read:

    - If ``channels.<key>.enabled is False`` → exclude.
    - Otherwise (True, missing, or any other falsy-but-not-False value
      like ``null``) → include.

    The over-inclusion is intentional — for OCP013 we'd rather warn
    once on a stale channel block than silently miss a real
    approval-surface gap.
    """
    if not isinstance(channels_block, dict):
        return []
    out: list[str] = []
    for key, sub in channels_block.items():
        if not isinstance(sub, dict):
            continue
        # Treat 'enabled: False' as the only suppressing value;
        # anything else (True, missing, None) means active.
        if sub.get("enabled") is False:
            continue
        out.append(key)
    return out


def _meta_version(oc_config: dict[str, Any]) -> str | None:
    meta = oc_config.get("meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("lastTouchedVersion")
    return raw if isinstance(raw, str) and raw.strip() else None


# ─────────────────────────────────────────────────────────────────────────────
# Rule: OCP013
# ─────────────────────────────────────────────────────────────────────────────


def _check_ocp013_approval_surface_gap(
    *,
    bot_id: str,
    oc_config: dict[str, Any],
) -> Finding | None:
    """OCP013 — ``ask`` policy active on a channel without an approval surface.

    Fires when:
    1. The bot's exec policy can trigger an ask flow (either
       ``security="allowlist"`` or ``ask in {"on-miss","always"}``),
       AND
    2. At least one enabled message channel lacks an approval surface
       (i.e., is not in :data:`APPROVAL_SURFACE_CAPABILITY`).

    Motivating incident: security_bot on 2026-05-20 had ``security=allowlist``
    + ``ask=on-miss`` and Telegram enabled. Every off-allowlist exec
    request fired ``exec.approval.request`` over the gateway WebSocket
    with no surface to respond on, stalling the agent turn. The
    operator-facing symptom was security_bot replying "approve from Web UI
    or TUI" — accurate but unactionable in-channel.

    Severity: **FAIL** — the bot's primary use case in that channel is
    silently broken. From the operator's perspective the bot just stops
    responding usefully.
    """
    tools = oc_config.get("tools") or {}
    exec_block = tools.get("exec") or {}

    if not _ask_flow_can_fire(exec_block):
        return None

    channels = oc_config.get("channels") or {}
    enabled = _enabled_message_channels(channels)
    if not enabled:
        return None

    gap_channels = [c for c in enabled if c not in APPROVAL_SURFACE_CAPABILITY]
    if not gap_channels:
        return None

    security = exec_block.get("security")
    ask = exec_block.get("ask")

    title = (
        f"{bot_id}: ask-policy active on channel(s) without an approval surface: "
        f"{', '.join(sorted(gap_channels))}"
    )

    why_lines = []
    if security == "allowlist":
        why_lines.append("`tools.exec.security = \"allowlist\"` — any command "
                         "not in the approvals allowlist triggers an ask flow.")
    if ask in ("on-miss", "always"):
        why_lines.append(f"`tools.exec.ask = \"{ask}\"` — explicit ask mode "
                         "routes exec calls through the approval surface.")

    surface_supported = sorted(APPROVAL_SURFACE_CAPABILITY)

    detail = (
        f"`{bot_id}` is configured to request operator approval for some or "
        f"all exec calls, but at least one of its enabled message channels "
        f"has no in-channel surface to receive that approval. The agent "
        f"turn will fire `exec.approval.request` over the gateway and then "
        f"stall — visible to the user as the bot stalling or replying with "
        f"a fabricated workaround like \"approve from the Web UI.\"\n\n"
        + "\n".join(f"- {line}" for line in why_lines)
        + f"\n\n**Enabled channels without approval surface:** "
        + ", ".join(f"`{c}`" for c in sorted(gap_channels))
        + f"\n\n**Channels with approval surface (today):** "
        + (", ".join(f"`{c}`" for c in surface_supported) if surface_supported else "_(none in-channel)_")
        + f"\n\n**Fix options** (operator picks per-bot intent):\n"
        + "1. Drop the ask flow entirely on this bot: "
        + "`openclaw exec-policy set --security full --ask off`. "
        + "Best when the bot is trusted in all its channels.\n"
        + "2. Widen the allowlist so the specific commands no longer trip "
        + "ask-on-miss. Use `openclaw approvals` to add entries; pair with "
        + "`security=allowlist`.\n"
        + "3. Disable the offending channel(s): "
        + f"`openclaw channels disable <channel>`. "
        + "Use when the bot's ask policy is correct but this channel was "
        + "added by mistake."
    )

    return Finding(
        code="OCP013",
        severity="fail",
        title=title,
        detail=detail,
        bot_id=bot_id,
        field_path="tools.exec",
        from_value=None,
        to_value={"security": security, "ask": ask},
        channels_without_surface=sorted(gap_channels),
        fixable=False,  # Three-option fix needs operator intent — no auto-fix.
        fix_summary=(
            "Operator-chosen remediation: drop ask flow, widen allowlist, "
            "or disable the gapped channel(s)."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def run_doctor(
    bot_id: str,
    *,
    oc_config: dict[str, Any],
) -> DoctorResult:
    """Run every OCP rule against ``bot_id`` and return findings.

    ``oc_config`` is the parsed contents of the bot's
    ``~/.openclaw/openclaw.json`` — the caller is responsible for
    loading it (different callers have different filesystem-access
    patterns; the doctor itself stays pure).

    Phase 0.5: only OCP013 ships. Later phases add OCP001–OCP012
    behind a snapshot-diff entry point (separate signature).
    """
    if not isinstance(oc_config, dict):
        raise TypeError(
            f"oc_config must be a dict (parsed openclaw.json); "
            f"got {type(oc_config).__name__}"
        )

    result = DoctorResult(bot_id=bot_id)

    # Surface state for the caller's status header.
    tools = oc_config.get("tools") or {}
    exec_block = tools.get("exec") or {}
    if isinstance(exec_block, dict):
        sec = exec_block.get("security")
        if isinstance(sec, str):
            result.exec_security = sec
        ask = exec_block.get("ask")
        if isinstance(ask, str):
            result.exec_ask = ask
    channels = oc_config.get("channels") or {}
    if isinstance(channels, dict):
        result.enabled_channels = _enabled_message_channels(channels)
    result.oc_version = _meta_version(oc_config)

    # Phase 0.5 rule(s).
    for check in (
        _check_ocp013_approval_surface_gap,
    ):
        finding = check(bot_id=bot_id, oc_config=oc_config)
        if finding is not None:
            result.findings.append(finding)

    return result


def iter_rules() -> Iterable[str]:
    """Return the rule codes the doctor knows about (Phase 0.5).

    Used by the CLI to display "checks run: N" headers and by tests
    to assert coverage.
    """
    return ("OCP013",)
