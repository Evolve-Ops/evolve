"""doctor_lint_signal — turn per-bot `openclaw doctor --lint` findings into Signals.

Reads the artifact each bot's nightly `doctor_pass_runner` writes to
``{bot_home}/.openclaw/workspace/evolve/doctor-lint.json`` and emits one
Signal per (bot, checkId). Designed to run as part of ``pod_report`` so it
inherits that cadence and shared-dir context without an extra daemon — the
same arrangement ``bot_log_signal`` uses.

Why the file hop exists
───────────────────────
``doctor_pass_runner`` runs as the BOT user (the OC CLI is uid-coupled, not
just config-path-coupled), and a bot user cannot write the Signal store. So
the bot drops an artifact in its own workspace — which it owns and the
``evolve`` user can read through the standing ``.openclaw`` ACL — and this
module, running pod-side as ``evolve``, does the emitting.

Staleness is a finding, not a silence
─────────────────────────────────────
An artifact older than :data:`STALE_AFTER_HOURS` means the nightly lint has
stopped running for that bot. That is exactly the failure this whole line of
work exists to end (8 doctor-pass daemons sat unloaded for 7 days while the
pod reported healthy), so a stale artifact raises its own Signal rather than
being skipped quietly. A bot with no artifact at all is NOT flagged — that is
the pre-first-run state on a fresh pod, and flagging it would fire on every
new bot before its first 03:17.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRODUCER = "doctor_lint"

# The job runs nightly (03:17 + up to 30 min jitter). 36 hours gives a full
# missed night plus margin before we call it stale, so a single skipped run
# (host asleep, one-off timeout) doesn't cry wolf but a stopped daemon does.
STALE_AFTER_HOURS = 36

ARTIFACT_RELPATH = ".openclaw/workspace/evolve/doctor-lint.json"

# OC severity → (vector, magnitude), matching bot_log_signal's severity tags.
_SEVERITY_TAG = {
    "error": ("operations", 3),
    "warning": ("operations", 2),
    "info": ("operations", 1),
}

_DEEPLINK = "/admin/maintenance"


def _tag(severity: str) -> tuple[str, int]:
    return _SEVERITY_TAG.get(str(severity).lower(), ("operations", 2))


def _subject(finding: dict) -> str:
    """The thing a finding is ABOUT, for the Signal signature.

    ``checkId`` alone is not unique per finding: one lint of the reference pod
    returned two ``core/doctor/node-hosting-preconditions`` findings in the
    same run (``gateway.bind`` and
    ``plugins.entries.device-pair.enabled``). Keying on checkId alone
    collapsed them into one Signal and silently dropped the second.

    ``path``/``target`` is the subject OC itself names, and is stable across
    re-runs — unlike ``message``, which embeds mutable specifics (counts,
    lists of paths) and would mint a fresh un-resolvable Signal on every edit.
    Findings carrying neither are unique per checkId in practice (they are
    whole-config assertions like ``core/doctor/security``); if OC ever emits
    two such findings for one check, they coalesce into one Signal — visible,
    not silent, because the body still names the condition.
    """
    return str(finding.get("path") or finding.get("target") or "")


def _read_artifact(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_stale(ran_at: str | None, *, now: datetime) -> bool:
    """True when ``ran_at`` is missing/unparseable or older than the window.

    An unparseable timestamp counts as stale on purpose: we cannot show that
    the lint ran recently, and "cannot show" must not read as "fine".
    """
    if not ran_at:
        return True
    try:
        stamp = datetime.fromisoformat(str(ran_at))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < now - timedelta(hours=STALE_AFTER_HOURS)


def emit_doctor_lint_signals(
    shared_dir: Path,
    bot_homes: dict[str, Path],
    *,
    now: datetime | None = None,
) -> None:
    """Emit/sweep Signals from each bot's doctor-lint artifact.

    ``bot_homes`` maps bot id → that bot's home directory. Passed in rather
    than derived from the bot id: the account name is not the bot id, and
    building ``/Users/<bot_id>`` would read the wrong home (or none).

    Safe to call when the signals package is unavailable or artifacts are
    missing — the calling pod_report run is never affected.
    """
    try:
        from signals import store as signals_store
        from schema.signal import make_signature
    except ImportError:
        return

    now = now or datetime.now(timezone.utc)
    kept_signatures: set[str] = set()

    for bot_id, home in sorted(bot_homes.items()):
        path = Path(home) / ARTIFACT_RELPATH
        if not path.exists():
            # Pre-first-run, not a fault. See module docstring.
            continue

        data = _read_artifact(path)
        if data is None:
            sig = make_signature(PRODUCER, "doctor_lint_unreadable", bot_id)
            kept_signatures.add(sig)
            _observe(
                signals_store, shared_dir, signature=sig,
                signal_type="doctor_lint_unreadable", bot_id=bot_id,
                severity="error",
                title=f"{bot_id}: doctor-lint result is unreadable",
                body=(
                    f"{path} exists but could not be parsed as JSON. The "
                    f"nightly health lint for this bot is reporting nothing "
                    f"until this is resolved."
                ),
            )
            continue

        if _is_stale(data.get("ran_at"), now=now):
            sig = make_signature(PRODUCER, "doctor_lint_stale", bot_id)
            kept_signatures.add(sig)
            _observe(
                signals_store, shared_dir, signature=sig,
                signal_type="doctor_lint_stale", bot_id=bot_id,
                severity="warning",
                title=f"{bot_id}: nightly health lint has stopped running",
                body=(
                    f"The last doctor-lint for {bot_id} ran at "
                    f"{data.get('ran_at') or 'an unknown time'}, more than "
                    f"{STALE_AFTER_HOURS}h ago. Its launchd job "
                    f"(ai.openclaw.evolve.doctor-pass.{bot_id}) is probably "
                    f"not loaded — check Maintenance → LaunchD Services."
                ),
            )
            # Stale findings are not current findings: emitting them would
            # assert a present-tense condition from a stale file.
            continue

        for finding in data.get("findings") or []:
            check_id = str(finding.get("checkId") or "unknown")
            severity = str(finding.get("severity") or "warning")
            sig = make_signature(
                PRODUCER, f"doctor_lint_{severity}",
                f"{bot_id}:{check_id}:{_subject(finding)}",
            )
            kept_signatures.add(sig)
            _observe(
                signals_store, shared_dir, signature=sig,
                signal_type=f"doctor_lint_{severity}", bot_id=bot_id,
                severity=severity,
                title=f"{bot_id}: {check_id}",
                body=str(finding.get("message") or ""),
                extra_details={
                    "check_id": check_id,
                    "config_path": finding.get("path"),
                    "target": finding.get("target"),
                    "requirement": finding.get("requirement"),
                    "fix_steps": finding.get("fixHint"),
                },
            )

    # Anything that cleared since the last run auto-archives.
    signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=kept_signatures,
        reason="auto-resolve: no longer reported by openclaw doctor --lint",
    )


def _observe(
    signals_store,
    shared_dir: Path,
    *,
    signature: str,
    signal_type: str,
    bot_id: str,
    severity: str,
    title: str,
    body: str,
    extra_details: dict | None = None,
) -> None:
    vector, magnitude = _tag(severity)
    details: dict = {
        "deeplink": _DEEPLINK,
        "vector": vector,
        "magnitude": magnitude,
        "severity_active": True,
        "source": "openclaw doctor --lint",
    }
    if extra_details:
        # Drop empty keys so the Signal detail pane doesn't render a column of
        # "None" for the fields OC omits on a given finding.
        details.update({k: v for k, v in extra_details.items() if v})
    try:
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=signal_type,
            flavor="maintenance",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — one bad Signal must not stop the sweep
        print(f"[doctor_lint] observe failed for {bot_id}/{signal_type}: {exc}")
