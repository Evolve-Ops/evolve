"""generators.sysadmin_watchdog.signals — Signal spec factories.

Every platform-failure detector in this generator is fundamentally a
monitor: detect a broken substrate condition, attach the runbook that
recovers it. Per the alerts/signal-store consolidation
(docs/spec-alerts-signal-store-2026-05-07.md) those outputs route to
the Signal store rather than the Proposal queue.

Each factory returns a kwargs dict ready to unpack into
``signals.store.observe(**spec)``. The runner walks the list returned
by ``observe_signals(ctx)`` and emits each one before calling
``observe()`` for any actionable proposals.

Signal types here all live in the **maintenance** lane (something is
broken; an operator needs to act). Severity is ``alert`` for hard
outages (gateway down, daemon not loaded, plugin missing, malformed
config) and ``warn`` for soft drift (version behind, ACL drift). The
gateway signal escalates from warn → alert when the failure becomes
chronic; the same signature is reused so the Alerts UI shows one
incident, not two.
"""

from __future__ import annotations

from platform_profile import get_profile
from schema.signal import make_signature

PRODUCER = "sysadmin_watchdog"

# Severity-framework retrofit — every sysadmin_watchdog finding is on
# the `operations` vector. Anchored magnitudes follow the spec at
# docs/spec-severity-framework-2026-05-18.md §2.3:
#   1 = single bot degraded
#   2 = single bot down OR pod-wide degraded
#   3 = pod-wide degraded OR multi-bot down
#   4 = pod-wide hard outage
_VECTOR = "operations"


# ─────────────────────────────────────────────────────────────────────────────
# Gateway
# ─────────────────────────────────────────────────────────────────────────────


def gateway_down_signal_kwargs(
    bot_id: str,
    *,
    consecutive_failures: int,
    chronic: bool,
) -> dict:
    """Gateway unreachable. Severity escalates when chronic; signature stable."""
    if chronic:
        title = f"Gateway for {bot_id} persistently down — recommend restart"
        body = (
            f"Bot `{bot_id}`'s gateway has failed {consecutive_failures}+ "
            f"consecutive checks. Most chronic outages clear with a kickstart:\n\n"
            f"```\nsudo /bin/launchctl kickstart -k system/ai.evolve.openclaw.{bot_id}\n```\n\n"
            "If the kickstart doesn't restore service, inspect `system.log` "
            "and the gateway's stderr log."
        )
        severity = "alert"
    else:
        title = f"Gateway for {bot_id} unreachable ({consecutive_failures} incident(s))"
        body = (
            f"Bot `{bot_id}`'s gateway has been unreachable across "
            f"{consecutive_failures} health-check incident(s) in the last 24h. "
            "Check logs and gateway health."
        )
        severity = "warn"
    # Chronic = magnitude 3 (single bot persistently down — still bot
    # scope, but the duration boosts it). Transient = magnitude 2.
    # severity_active is True for both — the gateway is currently down
    # in either case; chronic just signals duration.
    magnitude = 3 if chronic else 2
    return dict(
        signature=make_signature(PRODUCER, "gateway_down", bot_id),
        producer=PRODUCER,
        type="gateway_down",
        flavor="maintenance",
        severity=severity,
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            consecutive_failures=consecutive_failures,
            chronic=chronic,
            vector=_VECTOR,
            magnitude=magnitude,
            severity_active=True,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Plugin
# ─────────────────────────────────────────────────────────────────────────────


def plugin_missing_signal_kwargs(bot_id: str) -> dict:
    body = (
        f"Bot `{bot_id}`'s gateway is up but its status endpoint doesn't list "
        "the Evolve plugin. Most often a kickstart fixes it (the plugin can "
        "fail to load on cold-start race conditions and reloads cleanly after "
        "a restart):\n\n"
        f"```\nsudo /bin/launchctl kickstart -k system/ai.evolve.openclaw.{bot_id}\n```\n\n"
        "If the plugin still doesn't show up after restart, the gateway is "
        "likely running a different build than expected. Check:\n\n"
        f"  - The deploy version: `cat /Users/{bot_id}/.openclaw/version.json`\n"
        f"  - The plugin's stderr: `tail -100 /Users/{bot_id}/.openclaw/logs/plugin-*.log`\n"
    )
    return dict(
        signature=make_signature(PRODUCER, "plugin_missing", bot_id),
        producer=PRODUCER,
        type="plugin_missing",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"Evolve plugin not loaded on {bot_id}'s gateway",
        body=body,
        details=dict(vector=_VECTOR, magnitude=3, severity_active=True),
    )


# ─────────────────────────────────────────────────────────────────────────────
# LaunchDaemon
# ─────────────────────────────────────────────────────────────────────────────


def launchd_not_loaded_signal_kwargs(bot_id: str) -> dict:
    body = (
        f"The LaunchDaemon for `{bot_id}` is not loaded:\n\n"
        f"```\nsudo /bin/launchctl load /Library/LaunchDaemons/ai.evolve.openclaw.{bot_id}.plist\n```\n\n"
        "If the daemon fails to load, examine the plist for invalid paths "
        "or missing program arguments."
    )
    return dict(
        signature=make_signature(PRODUCER, "launchd_not_loaded", bot_id),
        producer=PRODUCER,
        type="launchd_not_loaded",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"LaunchDaemon for {bot_id} not loaded",
        body=body,
        details=dict(vector=_VECTOR, magnitude=3, severity_active=True),
    )


def systemd_not_loaded_signal_kwargs(bot_id: str) -> dict:
    """Linux analogue of :func:`launchd_not_loaded_signal_kwargs`.

    Same condition (the bot's gateway service unit is not loaded/running)
    with a systemd-shaped runbook. A SEPARATE signature/type from the
    launchd one — a pod is either macOS or Linux, never both, so the two
    never co-fire for the same bot; keeping them distinct means the Alerts
    UI title and the copy-paste recovery command are platform-correct, and
    the macOS signature stays byte-stable for pods already firing it."""
    unit = f"ai.openclaw.{bot_id}-gateway.service"
    body = (
        f"The systemd service for `{bot_id}` is not loaded or not running:\n\n"
        f"```\nsudo /usr/bin/systemctl enable --now {unit}\n```\n\n"
        "If the unit fails to start, inspect it with "
        f"`systemctl status {unit}` and `journalctl -u {unit}` for invalid "
        "paths or missing program arguments."
    )
    return dict(
        signature=make_signature(PRODUCER, "systemd_not_loaded", bot_id),
        producer=PRODUCER,
        type="systemd_not_loaded",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"systemd service for {bot_id} not loaded",
        body=body,
        details=dict(vector=_VECTOR, magnitude=3, severity_active=True),
    )


# ─────────────────────────────────────────────────────────────────────────────
# OpenClaw config validity
# ─────────────────────────────────────────────────────────────────────────────


def openclaw_config_invalid_signal_kwargs(bot_id: str) -> dict:
    body = (
        f"Bot `{bot_id}`'s `openclaw.json` is missing or malformed. "
        "Config corruption needs human review — do not auto-repair "
        "without understanding the root cause."
    )
    return dict(
        signature=make_signature(PRODUCER, "openclaw_config_invalid", bot_id),
        producer=PRODUCER,
        type="openclaw_config_invalid",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"openclaw.json for {bot_id} is malformed or unreadable",
        body=body,
        details=dict(vector=_VECTOR, magnitude=3, severity_active=True),
    )


# ─────────────────────────────────────────────────────────────────────────────
# OS user account
# ─────────────────────────────────────────────────────────────────────────────


def user_missing_signal_kwargs(bot_id: str, *, user: str) -> dict:
    # The detection is platform-valid — the account really is missing on
    # Linux too — so only the OS noun must track the pod's actual OS,
    # otherwise the message lies ("macOS account") on a Linux/VPS pod.
    # get_profile() autodetects from sys.platform; the watchdog runs ON the
    # host it audits, so this is the pod's real OS.
    os_noun = "Linux" if get_profile().name == "linux" else "macOS"
    body = (
        f"The {os_noun} account `{user}` for bot `{bot_id}` does not exist. "
        "The deploy flow creates it automatically:\n\n"
        f"```\nsudo evolve-admin deploy {bot_id}\n```\n\n"
        f"If the bot intentionally shares an account with another bot, "
        f"set `network.bots.{bot_id}.user` to that account's username "
        "and re-run deploy."
    )
    return dict(
        signature=make_signature(PRODUCER, "user_missing", bot_id),
        producer=PRODUCER,
        type="user_missing",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"{os_noun} user {user!r} for bot {bot_id} does not exist",
        body=body,
        details=dict(
            expected_user=user,
            vector=_VECTOR,
            magnitude=3,
            severity_active=True,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Version drift
# ─────────────────────────────────────────────────────────────────────────────


def version_behind_signal_kwargs(bot_id: str, *, days_behind: int) -> dict:
    body = (
        f"Bot `{bot_id}` is {days_behind} days behind the latest Evolve "
        "release. Review the changelog and schedule a deploy."
    )
    # Release lag is advisory — bot still functions, just not on latest
    # code. Magnitude 1; severity_active=False so it doesn't compete
    # with currently-firing incidents in priority order.
    return dict(
        signature=make_signature(PRODUCER, "version_behind", bot_id),
        producer=PRODUCER,
        type="version_behind",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id} is {days_behind} days behind latest Evolve release",
        body=body,
        details=dict(days_behind=days_behind, vector=_VECTOR, magnitude=1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ACL drift  (paired with a Proposal — see acl.py)
# ─────────────────────────────────────────────────────────────────────────────


def acl_drift_signal_kwargs(bot_id: str) -> dict:
    """Paired Signal for the ACL-drift Proposal.

    ACL drift is the one substrate-health condition that emits both a
    Signal and a Proposal — the Proposal carries an autonomous-eligible
    ConfigPatch and links back to this Signal via ``motivating_signals``.
    """
    # Derive the bot home from the platform seam, not a `/Users/` literal —
    # on a Linux pod the home is /home/<bot>, and a hardcoded macOS path makes
    # the alert read "lost read access to /Users/evo/.openclaw/" on a box where
    # that path doesn't exist (the 2026-06-23 fresh-evo-pod alert flurry).
    from evolve_config import bot_home

    oc_dir = bot_home(bot_id) / ".openclaw"
    body = (
        f"The `evolve` user has lost read access to `{oc_dir}/`. "
        "Sysadmin Watchdog is queueing an autonomous ACL-restore proposal; "
        "approval is on the Self-Improvement page."
    )
    # ACL drift blocks monitoring of this bot until restored — single
    # bot degraded (mag 2) until the autonomous proposal applies.
    return dict(
        signature=make_signature(PRODUCER, "acl_drift", bot_id),
        producer=PRODUCER,
        type="acl_drift",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"ACL drift on {bot_id}: evolve user cannot read .openclaw/",
        body=body,
        details=dict(vector=_VECTOR, magnitude=2, severity_active=True),
    )
