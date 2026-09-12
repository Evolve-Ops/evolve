#!/usr/bin/env python3
"""External liveness check for Evolve.

Designed to run from the admin user's crontab (pod_admin_user on the reference
pod), completely separate from Evolve's launchd / evolve user / python
venv infrastructure. Catches the case where Evolve's entire observation
stack is wedged: launchd unloaded, signal-notifier crashed, dispatcher
broken, admin UI hung — and no one is reading the alerts the broken
system can no longer send.

Design idea #2 of the Security_bot retirement coverage map. Security_bot-style
"independent verifier" pattern: Evolve's own monitor_coverage daemon
(design idea #1) can't catch the failure mode where the launchd subsystem
that runs it is itself the broken thing. THIS script runs under a
different user, through a different scheduling primitive (BSD cron, not
launchd), uses stdlib only (no Evolve imports), and reads keys directly
from a world-readable keystore — so anything wedged on the Evolve side
can't break it too.

Setup (one-time, as pod_admin_user, on the mini):

    crontab -e
    # Add the following line:
    */5 * * * * /usr/bin/python3 /Users/Shared/evolve-repo/scripts/evolve_liveness_external.py

What the script checks every 5 minutes:
    1. Admin UI /api/health reachable on http://localhost:5050
    2. Today's signal log under /Users/Shared/evolve/signals/log/ has
       been written in the last 2 hours
    3. /Users/Shared/evolve/logs/audit.log mtime within 30 minutes
       (audit runs every 15min)

On any failure: pages Pod_admin via Telegram using the security-alert keystore
(/Users/Shared/evolve/keystore/security-alert-{token,chat-id}, both
world-readable). Cooldown: 60 min between repeat pages so a sustained
outage doesn't spam.

Exit codes:
    0 — all checks passed (or in cooldown after a recent failure)
    1 — failures detected AND Telegram alert sent successfully
    2 — failures detected but Telegram delivery also failed (cron mail catches this)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# Cooldown: don't re-page if we paged in the last 60 min.
COOLDOWN_FILE = Path("/tmp/evolve-liveness-external-cooldown")
COOLDOWN_SEC = 60 * 60

# Thresholds — tuned for "obviously wedged" not "slightly behind".
HEALTH_URL = "http://localhost:5050/api/health"
HEALTH_TIMEOUT = 10
SIGNAL_LOG_MAX_AGE = 2 * 3600       # 2h — signal store should write something
AUDIT_LOG_MAX_AGE = 30 * 60          # 30 min — audit runs every 15 min

# Keystore (world-readable; the same credentials audit.py uses for its
# defense-in-depth fallback path).
SECURITY_TOKEN_PATH = Path("/Users/Shared/evolve/keystore/security-alert-token")
SECURITY_CHAT_ID_PATH = Path("/Users/Shared/evolve/keystore/security-alert-chat-id")


# ── Checks ──────────────────────────────────────────────────────────


def check_health() -> list[str]:
    """Run all checks. Return list of failure messages (empty list = all OK)."""
    failures: list[str] = []
    failures.extend(_check_admin_ui())
    failures.extend(_check_signal_store())
    failures.extend(_check_audit_log())
    return failures


def _check_admin_ui() -> list[str]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as r:
            if r.status == 200:
                return []
            return [f"admin-ui /api/health returned HTTP {r.status}"]
    except Exception as e:
        return [f"admin-ui /api/health unreachable: {type(e).__name__}: {e}"]


def _check_signal_store() -> list[str]:
    today = date.today().isoformat()
    log = Path("/Users/Shared/evolve/signals/log") / f"{today}.jsonl"
    if not log.exists():
        return [
            f"signal store: today's log ({log.name}) missing — "
            "nothing has written a signal today"
        ]
    try:
        age = time.time() - log.stat().st_mtime
    except OSError as e:
        return [f"signal store: cannot stat {log}: {e}"]
    if age > SIGNAL_LOG_MAX_AGE:
        return [
            f"signal store: today's log idle for {_human(age)} "
            f"(expected activity within {_human(SIGNAL_LOG_MAX_AGE)})"
        ]
    return []


def _check_audit_log() -> list[str]:
    log = Path("/Users/Shared/evolve/logs/audit.log")
    if not log.exists():
        return ["audit.log missing — audit daemon may have never run"]
    try:
        age = time.time() - log.stat().st_mtime
    except OSError as e:
        return [f"audit.log: cannot stat: {e}"]
    if age > AUDIT_LOG_MAX_AGE:
        return [
            f"audit.log idle for {_human(age)} "
            f"(expected activity within {_human(AUDIT_LOG_MAX_AGE)}; "
            "audit runs every 15 minutes)"
        ]
    return []


def _human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ── Cooldown ────────────────────────────────────────────────────────


def in_cooldown() -> bool:
    if not COOLDOWN_FILE.exists():
        return False
    try:
        age = time.time() - COOLDOWN_FILE.stat().st_mtime
    except OSError:
        return False
    return age < COOLDOWN_SEC


def stamp_cooldown() -> None:
    try:
        COOLDOWN_FILE.touch(exist_ok=True)
        os.utime(COOLDOWN_FILE, None)
    except OSError:
        pass


# ── Telegram delivery (direct Bot API; no Evolve imports) ────────────


def send_telegram(failures: list[str]) -> bool:
    try:
        token = SECURITY_TOKEN_PATH.read_text().strip()
        chat_id = SECURITY_CHAT_ID_PATH.read_text().strip()
    except OSError:
        return False
    if not token or not chat_id:
        return False
    msg = (
        "EVOLVE EXTERNAL LIVENESS CHECK FAILED\n"
        "\n"
        + "\n".join(f"  - {f}" for f in failures)
        + "\n\nFiring from pod_admin_user-side cron "
        "(independent of Evolve's launchd / evolve user). "
        "Most likely cause: Evolve's main daemon stack is wedged. "
        "Investigate launchctl + admin UI."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
        return True
    except Exception:
        return False


# ── Main ────────────────────────────────────────────────────────────


def main() -> int:
    failures = check_health()
    if not failures:
        return 0
    if in_cooldown():
        # Suppress duplicate page within the cooldown window. Still log
        # to stdout so the cron mail (if enabled) records the muted hit.
        print(
            json.dumps({"status": "failures_in_cooldown", "failures": failures}),
            flush=True,
        )
        return 0
    sent = send_telegram(failures)
    if sent:
        stamp_cooldown()
        print(
            json.dumps({"status": "alerted", "failures": failures}),
            flush=True,
        )
        return 1
    print(
        json.dumps({"status": "alert_delivery_failed", "failures": failures}),
        flush=True,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
