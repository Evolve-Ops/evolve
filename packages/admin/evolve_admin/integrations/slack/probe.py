"""Continuous Slack-config reconciler.

Runs :func:`doctor.run_doctor` against every bot in the pod and mirrors
findings into the Signal store. Mirrors the pattern in
``web/server.py:_emit_integration_signals`` — observe per finding,
sweep-resolve at the end so cleared conditions auto-archive.

Per the spec, three findings get first-class Signal types so the
Alerts page can render contextual remediation UI:

- ``silent_blackhole`` — ``SLK010`` at FAIL (allowlist + bot in member channels)
- ``oc_default_drift`` — ``SLK003`` (visibleReplies unset or unexpected)
- ``bot_invited_unwatched`` — ``SLK005`` (bot in channel, no entry)

Other FAIL/WARN findings are emitted with a per-finding ``type`` derived
from the check code so the Alerts page can still group them sensibly.
INFO findings do NOT emit Signals — they're operator-visible from the
CLI but don't belong in the alert feed (per ``alerts_signal_store`` spec,
"activity / maintenance lanes — INFO is for the dashboard, not the
inbox").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .doctor import DoctorResult, Finding, run_doctor

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Signal-mapping policy
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SignalShape:
    type: str
    flavor: str          # "activity" | "maintenance"
    severity: str        # "info" | "warn" | "alert"


# Map (code, severity) → Signal shape, OR None to skip.
# Keys are tuples of (check_code, finding_severity). Anything not in
# this map is intentionally NOT emitted as a Signal.
_FINDING_TO_SIGNAL: dict[tuple[str, str], _SignalShape] = {
    # SLK001 — name-keyed channel (always FAIL — see doctor._check_name_keyed_channels)
    ("SLK001", "fail"): _SignalShape("name_keyed_channel", "maintenance", "alert"),
    # SLK002 — bot not in keyed channel
    ("SLK002", "fail"): _SignalShape("bot_not_member", "maintenance", "alert"),
    # SLK003 — visibleReplies default drift
    ("SLK003", "warn"): _SignalShape("oc_default_drift", "activity", "warn"),
    # SLK004 — DM allowlist unknown user
    ("SLK004", "fail"): _SignalShape("dm_allowlist_unknown_user", "maintenance", "warn"),
    # SLK005 — invited but unwatched
    ("SLK005", "info"): _SignalShape("bot_invited_unwatched", "maintenance", "warn"),
    # SLK007 — auth.test failed / bad token
    ("SLK007", "fail"): _SignalShape("slack_auth_failed", "maintenance", "alert"),
    # SLK010 — silent black-hole (empty allowlist while bot is in channels)
    ("SLK010", "fail"): _SignalShape("silent_blackhole", "maintenance", "alert"),
    ("SLK010", "warn"): _SignalShape("silent_blackhole_unverified", "maintenance", "warn"),
    # SLK011 — policy / openclaw.json drift (Phase 2)
    ("SLK011", "fail"): _SignalShape("policy_malformed", "maintenance", "alert"),
    ("SLK011", "warn"): _SignalShape("policy_render_drift", "maintenance", "warn"),
    # SLK015 — streaming.mode != "off" + team channel = noise broadcast.
    # Corrected post-team_bot_a-2026-05-15: `block` is also leaky, not just `partial`.
    ("SLK015", "fail"): _SignalShape("streaming_broadcasts_to_channel", "maintenance", "alert"),
    ("SLK015", "warn"): _SignalShape("streaming_in_team_channel", "maintenance", "warn"),
    # SLK016 — streaming.progress.commandText leaks raw command text
    ("SLK016", "warn"): _SignalShape("streaming_command_text_raw", "maintenance", "warn"),
    # SLK017 — workspace directory missing / stale / malformed
    ("SLK017", "fail"): _SignalShape("slack_directory_malformed", "maintenance", "alert"),
    ("SLK017", "warn"): _SignalShape("slack_directory_stale", "maintenance", "warn"),
    # SLK019 — group-DM (mpim) ID misplaced in the channel allowlist (inert entry)
    ("SLK019", "warn"): _SignalShape("group_dm_in_channel_allowlist", "maintenance", "warn"),
}


PRODUCER = "slack_config_probe"


@dataclass
class ProbeReport:
    """Summary of one probe run, returned for logging + tests."""
    bots_scanned: int = 0
    findings_total: int = 0
    signals_emitted: int = 0
    signals_swept: int = 0
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────


def run_probe(
    *,
    shared_dir: Path,
    bot_homes: dict[str, Path],
    slack_client_factory: "Any" = None,
    signals_module: "Any" = None,
    make_signature: "Callable[[str, str, str], str] | None" = None,
) -> ProbeReport:
    """Run the doctor across every bot and write Signals for findings.

    ``bot_homes`` maps ``bot_id`` → ``Path(home_dir)``. The probe is
    intentionally agnostic to *how* the caller picked which bots to
    scan — production wiring uses
    ``evolve_admin.config.load_network`` + ``bot_home`` to derive this;
    tests pass a fixture dict directly.

    ``signals_module`` and ``make_signature`` are dependency-injected
    for tests. In production:

    .. code-block:: python

        signals_module = _import_analyzer("signals.store")
        from schema.signal import make_signature

    These are loaded by the caller (CLI / scheduled task entry) so this
    module doesn't grow a hard dependency on the analyzer's import
    plumbing.
    """
    report = ProbeReport()
    kept_signatures: set[str] = set()
    seen_codes_emitted_per_bot: dict[str, set[str]] = {}

    for bot_id, home in bot_homes.items():
        try:
            result = run_doctor(
                bot_id,
                bot_home=home,
                shared_dir=shared_dir,
                slack_client_factory=slack_client_factory,
            )
        except Exception as exc:
            report.errors.append(f"{bot_id}: doctor crashed: {exc}")
            log.exception("slack_config_probe: doctor crashed for %s", bot_id)
            continue
        report.bots_scanned += 1
        report.findings_total += len(result.findings)

        if signals_module is None or make_signature is None:
            # Tests can run without the signal store; we still count
            # findings so callers can assert on the report shape.
            continue

        for finding in result.findings:
            shape = _FINDING_TO_SIGNAL.get((finding.code, finding.severity))
            if shape is None:
                continue
            sig = _emit_signal(
                shared_dir=shared_dir,
                signals_module=signals_module,
                make_signature=make_signature,
                bot_id=bot_id,
                finding=finding,
                shape=shape,
            )
            if sig is not None:
                kept_signatures.add(sig)
                report.signals_emitted += 1
                seen_codes_emitted_per_bot.setdefault(bot_id, set()).add(finding.code)

    # Sweep — anything we didn't observe this run, archive.
    if signals_module is not None:
        try:
            swept = signals_module.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: slack-doctor finding cleared",
            )
            report.signals_swept = len(swept or [])
        except Exception as exc:
            report.errors.append(f"sweep_resolve: {exc}")
            log.exception("slack_config_probe: sweep_resolve failed")

    return report


def _emit_signal(
    *,
    shared_dir: Path,
    signals_module: Any,
    make_signature: Callable[[str, str, str], str],
    bot_id: str,
    finding: Finding,
    shape: _SignalShape,
) -> str | None:
    """Best-effort write of one Signal. Returns the signature on success.

    ``scope_key`` combines the bot id with the per-finding anchor
    (channel id, user id) so the same check on two different channels
    of the same bot produces two distinct Signals — each can be
    snoozed / dismissed independently.
    """
    anchor = finding.channel_id or finding.user_id or ""
    scope_key = f"{bot_id}:{anchor}" if anchor else bot_id
    signature = make_signature(PRODUCER, shape.type, scope_key)
    try:
        signals_module.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=shape.type,
            flavor=shape.flavor,
            severity=shape.severity,
            scope="bot",
            bot_id=bot_id,
            title=finding.title,
            body=finding.detail,
            details={
                "bot_id": bot_id,
                "check_code": finding.code,
                "channel_id": finding.channel_id,
                "user_id": finding.user_id,
                "fixable": finding.fixable,
            },
        )
    except Exception as exc:
        log.warning(
            "slack_config_probe: observe failed for %s/%s: %s",
            bot_id, finding.code, exc,
        )
        return None
    return signature


__all__ = ["PRODUCER", "ProbeReport", "run_probe"]
