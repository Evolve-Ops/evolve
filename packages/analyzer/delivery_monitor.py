"""delivery_monitor — per-window delivery outcomes for scheduled user-facing apps.

U2.1 of the user-value roadmap (docs/roadmap-user-value-2026-06-10.md §5);
spec: docs/spec-proactive-delivery-monitor-2026-06-10.md. The #1 stated
killer of proactive setups is scheduled-task unreliability — the briefing
silently doesn't arrive and nothing says so. Every existing check is
static (audits), coarse (cron_alert's 2-day threshold), or aimed at
infrastructure (heal.py). This monitor answers the question the *user*
cares about: did the 7:00 briefing reach them by 7:30 today — and if
not, why?

How it works (spec §4)
----------------------
Every 5 minutes (LaunchDaemon ``ai.evolve.evolve.delivery-monitor``,
runs as ``evolve``):

1. Build the monitored set from installed manifests' ``scheduled_actions[]``
   (§5): the optional ``delivery_contract{}`` block is authoritative when
   present; otherwise Option-A defaults are derived (window = fire + 30
   min, user-facing iff ``outputs[]`` declares a channel, evidence from
   per-mechanism defaults, heal = none).
2. Compute each action's most recent due window in the job's own TZ
   (plist ``EnvironmentVariables.TZ``, falling back to host TZ).
3. Classify each elapsed window tri-state (§6.2): delivered on time /
   ``did_not_run`` / ``ran_undelivered`` / cannot determine. A failed
   probe is its own state (``app_delivery_unmeasurable``) — never
   coerced into "looks fine", and never used to claim a miss the
   monitor can't evidence (the PR #1579 distinguish-tooling-failure
   rule).
4. Heal where declared safe (§8): one attempt per missed window
   (enforced across restarts via ``state.json``), gated on the app's
   ``heal: "rerun"`` assertion AND the :data:`HEAL_CANARY_APP_IDS`
   soak gate, honest regardless of outcome. Kickstart (no ``-k``) for
   loaded-but-didn't-fire; ``plutil -lint`` + bootstrap + kickstart for
   plist-exists-not-loaded; a single deferred rerun once a gateway-down
   Signal clears. A heal that produces no delivery evidence within
   :data:`HEAL_WAIT_MINUTES` escalates to ``alert`` with "the restart
   didn't work" copy — heal NEVER reports success without delivery
   evidence; ``result: "restarted"`` means the command ran, nothing
   more. See :func:`attempt_heal`.
5. ``observe()`` / ``sweep_resolve()`` Signals (§7) and append one row
   per classified window to the delivery ledger (§6.5) — the ground
   truth for U0's "proactive deliveries per week" metric.

Evidence sources per mechanism (§6.1)
-------------------------------------
* ``launchd`` — stdout-log mtime (``/tmp/<label>.out.log``, the path
  ``install_launchd_command_action`` wires) plus the job's load state via
  ``sudo -n /bin/launchctl list <label>`` (the long-standing grant).
  The heal path uses ``launchctl print`` / ``kickstart`` / ``bootstrap``
  under their own grants (setup_wizard §9c) and probes the grant with
  ``sudo -n`` first — a denial is reported as "couldn't attempt the
  restart", never silence. Probe failure → unmeasurable.
* ``launchd_python_signal`` — the wrapper's stdout log at
  ``{workspace}/evolve/scheduled/logs/{action_id}.log`` plus launchd state.
* OpenClaw cron — ``jobs-state.json`` (``state.lastRunAtMs`` /
  ``lastRunStatus``) read via ``cron_manager.read_jobs_state``. No
  manifest mechanism value maps here yet (OC crons attach via the
  top-level ``crons[]`` field, which Tier-2's
  ``check_openclaw_cron_run_status`` owns); the classifier path is
  implemented and unit-tested so wiring is a mechanism-string away.
* ``oc_heartbeat_instruction`` / ``oc_session_instruction`` — no
  deterministic evidence (the LLM decides per heartbeat). Excluded from
  v1, visibly: one ``unmonitorable`` coverage row per action in the
  ledger, never a fake green.
* ``crontab`` / ``external`` / ``unknown`` — excluded, counted in the
  coverage denominator the same way.

File access (CLAUDE.md "File Access Pattern"): the evolve user reads bot
workspaces via the deploy-time ACL; ``sudo -n /bin/cat`` is the fallback;
if *that* fails the window is unmeasurable, never silently OK. Never
``sudo -u <bot>``. ``bot_home()`` resolves the account (bot_id ≠ macOS
account name).

Working state lives at ``{shared_dir}/delivery_monitor/state.json``
(idempotent across restarts — same pattern as the signal-subscriber
ledger). The delivery ledger is
``{shared_dir}/delivery_monitor/ledger/<YYYY-MM-DD>.jsonl``, 90-day
retention enforced by ``signals.retention``.

Producer: ``delivery_monitor``. Pure Python, no LLM.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import plistlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from evolve_config import bot_home, get_shared_dir, load_config
from evolve_util import now_iso_offset as _utc_now_iso
from schema.signal import make_signature
from signals import store as signals_store


_log = logging.getLogger(__name__)

PRODUCER = "delivery_monitor"

TYPE_MISSED = "app_delivery_missed"
TYPE_UNMEASURABLE = "app_delivery_unmeasurable"
TYPE_POD_REGRESSION = "pod_delivery_regression"
# An installed launchd cron wrapper whose on-disk body still carries
# unsubstituted ``{var}`` / ``${var}`` template placeholders — it fails on
# every run and (with a trailing ``exit 0``) lies to launchd about it.
# Fired independent of the user-facing gate: a broken wrapper is broken
# whether or not the action declares a delivery contract (Atlas Daily
# Digest silent non-delivery 2026-05-30 → 2026-06-16).
TYPE_TEMPLATE_UNRESOLVED = "app_script_template_unresolved"

# Pod-scope escalation (the 2026-06-11 P0 backstop): misses on
# ≥POD_REGRESSION_MIN_INSTANCES distinct (bot, app) instances spanning
# ≥POD_REGRESSION_MIN_BOTS bots inside one rolling day is a platform
# regression, not N independent app problems — OpenClaw 2026.6.1
# removed the delivery surface and the story stayed shredded across
# per-app rows for 8 days. One loud pod Signal names the shared cause.
# Rolling 24h (today + yesterday's ledger) rather than calendar-day so
# the Signal doesn't flap at midnight.
POD_REGRESSION_MIN_BOTS = 2
POD_REGRESSION_MIN_INSTANCES = 3
POD_REGRESSION_WINDOW_HOURS = 24

# Misses already attributed to a benign host-wide condition don't count
# toward the platform-regression aggregate — a 7am host-asleep morning
# misses every bot's briefing without being a regression.
POD_REGRESSION_EXCLUDED_CAUSES = frozenset({"host_asleep", "dst"})

# How recent an OpenClaw install must be for the pod Signal to name it
# as the likely cause.
POD_REGRESSION_OC_RECENCY_DAYS = 7

# Where the installed OC version manifest lives (first match wins) —
# module-level so tests can point it at fixtures.
OPENCLAW_PACKAGE_JSON_CANDIDATES = (
    Path("/opt/homebrew/lib/node_modules/openclaw/package.json"),   # macOS arm64
    Path("/usr/local/lib/node_modules/openclaw/package.json"),      # macOS x86_64
    Path("/usr/lib/node_modules/openclaw/package.json"),            # Linux
)

# §5: derived Option-A default grace window — matches the gallery
# briefing's own success criterion ("runs file exists by 30 minutes
# after delivery_time").
DEFAULT_WINDOW_MINUTES = 30

# §7: severities + escalation thresholds. Calendar jobs fire warn on the
# first miss and escalate to alert at >=2 consecutive missed windows.
# Interval jobs (every_minutes) hold the Signal until 2 consecutive
# missed ticks (spec §15 open question 1's proposal — one missed
# 15-minute tick is noise) and escalate one tick later.
MISS_ESCALATE_AFTER = 2
INTERVAL_MISS_SIGNAL_AFTER = 2
# §7: a single broken probe shouldn't page anyone; a chronically blind
# monitor must. info → warn after 3 consecutive unmeasurable windows.
UNMEASURABLE_ESCALATE_AFTER = 3

# §8: after a heal attempt, delivery evidence gets this long before the
# Signal escalates to alert with "the restart didn't work" copy. No
# retry loops — one attempt per window, then report (subprocess-hang
# house rule: a heal that doesn't work gets reported, not re-tried
# harder).
HEAL_WAIT_MINUTES = 10

# §13.3 canary (the canary-an-affected-bot rule): heal is armed only for
# Morning Briefing v2 during the first ≥1-week soak. Other apps that
# declare heal:"rerun" (evening sweep) stay detection-only and report
# result="canary_holdback" in details.heal — a visible holdback, never
# silence. Widen this set after the soak + the §13 proof drill.
HEAL_CANARY_APP_IDS = frozenset({"app_morning_briefing"})

# Heal results that count as "heal failed" (§7: escalate the miss to
# alert immediately). Distinct from "restarted" (command ran; delivery
# still unproven — the heal_wait clock owns that verdict) and from the
# not-attempted results (no_grant, canary_holdback, …) which keep the
# miss at its normal severity but are reported in copy + details.
HEAL_FAILURE_RESULTS = frozenset({
    "kickstart_failed",
    "bootstrap_failed",
    "plist_missing",
    "plist_invalid",
    "plist_unreadable",
    "plutil_unavailable",
})

# §6.1 mechanism buckets. Mechanism strings come from
# evolve_admin.applications.manifest.SCHEDULED_ACTION_MECHANISMS; spelled
# literally here so the daemon has no admin-package import at module
# level (lazy-import pattern, same as monitor_gmail_integration_health).
LAUNCHD_MECHANISMS = frozenset({"launchd", "launchd_python_signal"})
UNMONITORABLE_MECHANISMS = frozenset({
    "oc_heartbeat_instruction",
    "oc_session_instruction",
    # Deprecated v17 spellings — still present in unmigrated manifests.
    "oc_heartbeat_hook",
    "oc_session_hook",
})
EXCLUDED_MECHANISMS = frozenset({"crontab", "external", "unknown", ""})

# Ledger outcomes (§6.5).
OUTCOME_ON_TIME = "on_time"
OUTCOME_LATE = "late"
OUTCOME_MISSED = "missed"
OUTCOME_UNMEASURABLE = "unmeasurable"
OUTCOME_DISABLED = "disabled"
OUTCOME_UNMONITORABLE = "unmonitorable"

DIAGNOSIS_DID_NOT_RUN = "did_not_run"
DIAGNOSIS_RAN_UNDELIVERED = "ran_undelivered"

_PROBE_TIMEOUT_SECONDS = 10

# Where mechanism-"launchd" scheduled actions' plists live
# (install_launchd_command_action wiring). Module-level so tests can
# point it at a fixture directory.
LAUNCHD_PLIST_DIR = Path("/Library/LaunchDaemons")


# ─────────────────────────────────────────────────────────────────────────────
# Probe seam
# ─────────────────────────────────────────────────────────────────────────────


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run argv, return (rc, stdout, stderr). The ONLY subprocess call site."""
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class Probes:
    """Evidence probes with an injectable command runner.

    Tests inject ``runner`` (and pure in-memory filesystems via the
    higher-level classifier functions) instead of patching module-level
    ``subprocess`` — the patch("…subprocess.run") idiom breaks silently
    when code moves (see feedback_subprocess_patch_fakes_break_on_module_move).

    Every method returns ``(value, error)`` where a non-None ``error``
    means the probe itself failed — the caller must treat the window as
    unmeasurable, not as "no evidence" (tri-state rule).
    """

    def __init__(self, runner: Callable[[list[str]], tuple[int, str, str]] | None = None):
        self._run = runner or _default_runner

    # -- files ---------------------------------------------------------------

    @staticmethod
    def _parent_statable(path: Path) -> bool:
        try:
            os.stat(path.parent)
            return True
        except OSError:
            return False

    def read_text(self, path: Path) -> tuple[str | None, str | None]:
        """Read a file as the evolve user; ``sudo -n /bin/cat`` fallback.

        Returns (text, None) on success, (None, None) when the file does
        not exist (absence is evidence, not an error), (None, error) when
        the file exists but cannot be read even via the sudo grant.
        """
        try:
            return path.read_text(encoding="utf-8", errors="replace"), None
        except FileNotFoundError:
            # If the parent directory is statable, the file is genuinely
            # absent — absence is evidence, no sudo round-trip needed.
            # If the parent itself is unreachable, the exists() check
            # would lie (EACCES under a 0700 ancestor) — fall through to
            # the sudo probe to settle it.
            if self._parent_statable(path):
                return None, None
        except PermissionError:
            # ACL gap — the sudo grant below is exactly for this case.
            _log.debug("[%s] direct read EACCES on %s; trying sudo", PRODUCER, path)
        except OSError as exc:
            return None, f"read {path}: {exc}"
        try:
            rc, out, err = self._run(["sudo", "-n", "/bin/cat", str(path)])
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return None, f"sudo cat {path}: {exc}"
        if rc == 0:
            return out, None
        lowered = (err or "").lower()
        if "no such file" in lowered:
            return None, None
        return None, f"sudo cat {path}: rc={rc} {err.strip()[:200]}"

    def mtime(self, path: Path) -> tuple[float | None, str | None]:
        """File mtime. (None, None) when absent; (None, error) on EACCES."""
        try:
            return os.stat(path).st_mtime, None
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"stat {path}: {exc}"

    # -- launchd ---------------------------------------------------------------

    def launchctl_job(self, label: str) -> tuple[dict | None, str | None]:
        """Probe a system-domain job via ``sudo -n /bin/launchctl list <label>``.

        Returns ({"loaded": bool, "last_exit": int | None, "running":
        bool}, None) when the probe ran, or (None, error) when the probe
        itself failed (sudo -n denied, timeout) — the caller must go
        tri-state, not assume.

        ``last_exit`` is normalized from launchctl's raw wait-status
        encoding (exit(N) prints as N·256; killed-by-signal leaves the
        signal number in the low bits, returned as-is) back to the
        script's exit code. ``running`` is True when the job currently
        has a PID — its LastExitStatus then belongs to a PREVIOUS run,
        so callers must not attribute it to the run in flight.
        """
        try:
            rc, out, err = self._run(["sudo", "-n", "/bin/launchctl", "list", label])
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return None, f"launchctl list {label}: {exc}"
        if rc == 0:
            last_exit: int | None = None
            running = False
            for line in out.splitlines():
                if "LastExitStatus" in line:
                    digits = "".join(c for c in line if c.isdigit() or c == "-")
                    try:
                        last_exit = int(digits)
                    except ValueError:
                        last_exit = None
                elif '"PID"' in line:
                    running = True
            if last_exit is not None and last_exit >= 256 and last_exit % 256 == 0:
                last_exit >>= 8
            return {
                "loaded": True, "last_exit": last_exit, "running": running,
            }, None
        combined = f"{out}\n{err}".lower()
        if "could not find service" in combined:
            return {"loaded": False, "last_exit": None, "running": False}, None
        return None, f"launchctl list {label}: rc={rc} {err.strip()[:200]}"

    # -- heal (§8) — every call is sudo -n so a missing grant surfaces as
    # -- a probe error ("couldn't attempt the restart"), never a hang or
    # -- silence. Grants: setup_wizard._render_evolve_sudoers §9c.

    def launchctl_print(self, label: str) -> tuple[dict | None, str | None]:
        """Heal-path load probe via ``sudo -n /bin/launchctl print``.

        Doubles as the sudoers *grant* probe (§8): heal runs this before
        any kickstart/bootstrap, and a ``sudo -n`` denial comes back as
        an error so the monitor reports "couldn't attempt the restart"
        tri-state. ``launchctl list`` stays the detection-side probe
        (its grant predates this one).
        """
        try:
            rc, out, err = self._run(
                ["sudo", "-n", "/bin/launchctl", "print", f"system/{label}"],
            )
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return None, f"launchctl print {label}: {exc}"
        if rc == 0:
            return {"loaded": True}, None
        combined = f"{out}\n{err}".lower()
        if "could not find service" in combined:
            return {"loaded": False}, None
        return None, f"launchctl print {label}: rc={rc} {err.strip()[:200]}"

    def plutil_lint(self, plist_path: Path) -> tuple[bool | None, str | None]:
        """``/usr/bin/plutil -lint`` gate before bootstrap (§8).

        Returns (ok, None) when the lint ran, (None, error) when it
        could not run at all. No sudo: LaunchDaemon plists are
        world-readable.
        """
        try:
            rc, _out, err = self._run(["/usr/bin/plutil", "-lint", str(plist_path)])
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return None, f"plutil -lint {plist_path}: {exc}"
        return rc == 0, None

    def launchctl_kickstart(self, label: str) -> tuple[bool, str | None]:
        """One-shot rerun via ``sudo -n /bin/launchctl kickstart``.

        Deliberately NO ``-k``: these are run-once calendar jobs, not
        daemons — ``-k`` would also kill a possibly-in-flight run.
        """
        try:
            rc, out, err = self._run(
                ["sudo", "-n", "/bin/launchctl", "kickstart", f"system/{label}"],
            )
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return False, f"launchctl kickstart {label}: {exc}"
        if rc == 0:
            return True, None
        return False, (
            f"launchctl kickstart {label}: rc={rc} {(err or out).strip()[:200]}"
        )

    def launchctl_bootstrap(self, plist_path: Path) -> tuple[bool, str | None]:
        """``sudo -n /bin/launchctl bootstrap system <plist>`` (§8 row 2)."""
        try:
            rc, out, err = self._run(
                ["sudo", "-n", "/bin/launchctl", "bootstrap", "system", str(plist_path)],
            )
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return False, f"launchctl bootstrap {plist_path}: {exc}"
        if rc == 0:
            return True, None
        return False, (
            f"launchctl bootstrap {plist_path}: rc={rc} {(err or out).strip()[:200]}"
        )

    # -- OpenClaw cron ---------------------------------------------------------

    def oc_jobs_state(self, bot_id: str) -> tuple[dict | None, str | None]:
        """Read a bot's OpenClaw ``jobs-state.json`` jobs dict.

        Uses ``cron_manager.read_jobs_state`` (lazy import — admin
        package). That helper returns ``{}`` for both "no file" and
        "unreadable"; either way the monitor cannot see the cron state,
        so an empty result is reported as a probe error and the window
        goes unmeasurable rather than quietly OK.
        """
        try:
            from evolve_admin.applications.cron_manager import read_jobs_state  # type: ignore
        except ImportError as exc:
            return None, f"cron_manager unavailable: {exc}"
        try:
            jobs = read_jobs_state(bot_id)
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return None, f"read_jobs_state({bot_id}): {exc}"
        if not jobs:
            return None, f"jobs-state.json unavailable or empty for {bot_id}"
        return jobs, None


# ─────────────────────────────────────────────────────────────────────────────
# Effective contract (§5 — Option B layered over A)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class EffectiveContract:
    user_facing: bool
    window_minutes: int
    delivered: dict | None      # {"kind": ..., "path"/"pattern"/"log": ...}
    heal: str                   # "rerun" | "none"
    source: str                 # "declared" | "derived"


def _validate_contract(contract: Any) -> list[str]:
    """Shape-check via the canonical validator; degrade gracefully."""
    try:
        from evolve_admin.applications.manifest import validate_delivery_contract  # type: ignore
    except ImportError:
        return [] if isinstance(contract, dict) else ["not a dict"]
    return validate_delivery_contract(contract)


def _derived_user_facing(action: dict) -> bool:
    """Option-A inference: user-facing iff outputs[] declares a channel."""
    for out in action.get("outputs") or []:
        if isinstance(out, dict) and out.get("channel"):
            return True
    return False


def _derived_delivered_evidence(manifest: dict, action: dict) -> dict | None:
    """Option-A delivered-evidence default: log-line heuristics on the
    app's declared ``interface_contract.signal_prefixes``.

    Prefers a prefix containing "SENT" (the gallery convention:
    BRIEFING_SENT:, SWEEP_SENT:, PREMEET_SENT:). With no usable prefix
    there is no deterministic delivery proof — return None and let the
    classifier report the window honestly as unmeasurable-after-run
    rather than coercing "ran" into "delivered".
    """
    prefixes = (manifest.get("interface_contract") or {}).get("signal_prefixes") or []
    prefixes = [p for p in prefixes if isinstance(p, str) and p.strip()]
    sent = [p for p in prefixes if "sent" in p.lower()]
    pattern = sent[0] if sent else (prefixes[0] if len(prefixes) == 1 else None)
    if not pattern:
        return None
    return {"kind": "signal_line", "pattern": pattern, "log": None}


def effective_contract(manifest: dict, action: dict) -> EffectiveContract:
    """Resolve the action's delivery contract (§5: B layered over A).

    A declared ``delivery_contract`` is authoritative when well-formed.
    A malformed one falls back to the derived defaults entirely — Tier-2's
    ``delivery_contract_invalid`` assertion owns reporting the shape
    problem; the monitor must not half-honor a contract it can't trust.
    """
    declared = action.get("delivery_contract")
    if isinstance(declared, dict) and not _validate_contract(declared):
        evidence = declared.get("evidence") or {}
        delivered = evidence.get("delivered")
        if delivered is None:
            delivered = _derived_delivered_evidence(manifest, action)
        return EffectiveContract(
            user_facing=declared.get("user_facing", _derived_user_facing(action)),
            window_minutes=declared.get("window_minutes", DEFAULT_WINDOW_MINUTES),
            delivered=delivered,
            heal=declared.get("heal", "none"),
            source="declared",
        )
    return EffectiveContract(
        user_facing=_derived_user_facing(action),
        window_minutes=DEFAULT_WINDOW_MINUTES,
        delivered=_derived_delivered_evidence(manifest, action),
        heal="none",
        source="derived",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Window math (§6.2 / §6.3 — TZ from the plist, DST detection)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Window:
    start: datetime   # scheduled fire time (tz-aware)
    end: datetime     # fire + window_minutes (tz-aware)

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts <= self.end


def host_timezone() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None  # astimezone() always attaches one
    return tz


def job_timezone(tz_name: str | None, fallback: tzinfo) -> tuple[tzinfo | None, str | None]:
    """Resolve the job's TZ. Absent → host TZ (§6.3); invalid → error
    (the window itself is ambiguous → unmeasurable, §6.2c)."""
    if not tz_name:
        return fallback, None
    try:
        return ZoneInfo(tz_name), None
    except Exception:  # noqa: BLE001 — unknown TZ string is data
        return None, f"unknown timezone {tz_name!r} in job plist"


def plist_env_tz(plist_path: Path, probes: Probes) -> str | None:
    """Best-effort TZ from the installed plist's EnvironmentVariables.

    A missing/unreadable plist is NOT a probe error here — the plist may
    legitimately be gone (the bootout proof case); load-state probing
    owns that signal. Only an explicitly present-but-invalid TZ value is
    surfaced (by job_timezone above).
    """
    text, err = probes.read_text(plist_path)
    if text is None or err is not None:
        return None
    try:
        data = plistlib.loads(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed plist → fall back to host TZ
        return None
    env = data.get("EnvironmentVariables") or {}
    tz = env.get("TZ")
    return tz if isinstance(tz, str) and tz.strip() else None


def latest_calendar_fire(cron: dict, now: datetime) -> datetime | None:
    """Most recent StartCalendarInterval fire at or before ``now``.

    Supports the Hour/Minute (+ optional Weekday / Day) keys the install
    pipeline emits. ``now`` must be tz-aware in the job's TZ. Returns
    None for shapes we can't interpret (caller goes unmeasurable).
    """
    try:
        hour = int(cron.get("Hour", 0))
        minute = int(cron.get("Minute", 0))
        weekday = cron.get("Weekday")
        day = cron.get("Day")
        weekday = int(weekday) if weekday is not None else None
        day = int(day) if day is not None else None
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    for _ in range(370):  # bounded walk-back covers Weekday + Day-of-month
        ok = True
        if weekday is not None:
            # launchd Weekday: 0 and 7 are both Sunday; Python: Monday=0.
            launchd_wd = (candidate.weekday() + 1) % 7
            ok = launchd_wd == (weekday % 7)
        if ok and day is not None:
            ok = candidate.day == day
        if ok:
            return candidate
        candidate -= timedelta(days=1)
    return None


def window_straddles_dst(window: Window) -> bool:
    """True when the UTC offset changes between window start and end."""
    return window.start.utcoffset() != window.end.utcoffset()


# ─────────────────────────────────────────────────────────────────────────────
# Classification (§6.2 — the tri-state matrix; pure, fully injectable)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Classification:
    outcome: str                       # ledger outcome (§6.5)
    diagnosis: str | None = None       # did_not_run | ran_undelivered
    delivered_at: datetime | None = None
    suspected_cause: str | None = None
    probe_errors: list[str] = field(default_factory=list)
    caused_by_signal_id: str | None = None
    last_exit: int | None = None       # scheduler-reported exit status


# Surfaced on unmeasurable windows when the app declares no run_file /
# signal_line evidence (kept verbatim — operators and tests match on it).
UNDECLARED_EVIDENCE_NOTE = "no delivery evidence declared (run_file/signal_line)"


def classify_window(
    *,
    window: Window,
    delivered_at: datetime | None,
    delivered_probe_errors: list[str],
    ran: bool | None,
    ran_probe_errors: list[str],
    loaded: bool | None = None,
    last_exit: int | None = None,
    delivered_declared: bool = True,
    dst: bool = False,
    slept_overlap_signal_id: str | None = None,
    gateway_down_signal_id: str | None = None,
) -> Classification:
    """Classify one elapsed window tri-state (§6.2).

    Inputs are pre-gathered evidence so the matrix is a pure function:

    * ``delivered_at`` — timestamp of delivery proof, if any (may fall
      outside the window: later → late; earlier → stale, ignored).
    * ``ran`` — True (scheduler state shows a fire in the window), False
      (probes healthy and show no fire), None (a required probe failed).
    * ``loaded`` — launchd load state when known (False = the bootout /
      never-bootstrapped case).
    * ``last_exit`` — the scheduler's most recent exit status when known.
      A fire in the window plus a non-zero exit is a definite miss
      (``ran_undelivered``, cause ``script_error``) even when the app
      declares no delivery evidence — the script died before it could
      produce any proof.
    * ``delivered_declared`` — False when the effective contract carries
      no run_file/signal_line evidence. Replaces the old behavior of
      reporting that as a delivered-probe *error*: undeclared evidence
      only yields unmeasurable when the run itself looked healthy.

    Positive delivery evidence short-circuits probe failures: an on-time
    run file proves delivery regardless of a broken launchctl probe.
    Negative claims (states a/b) require their probes healthy; otherwise
    the window is unmeasurable — never coerced OK, never a guessed miss.
    """
    probe_errors = list(delivered_probe_errors) + list(ran_probe_errors)

    if delivered_at is not None and window.contains(delivered_at):
        return Classification(OUTCOME_ON_TIME, delivered_at=delivered_at)
    if delivered_at is not None and delivered_at > window.end:
        # Evidence appeared after the window (post-heal, host woke late,
        # or the monitor itself was down). Not a fourth state (§6.2).
        return Classification(
            OUTCOME_LATE,
            delivered_at=delivered_at,
            suspected_cause=(
                "host_asleep" if slept_overlap_signal_id else None
            ),
            caused_by_signal_id=slept_overlap_signal_id,
        )
    # delivered_at before the window start is stale evidence from an
    # earlier window — treat as no delivery proof for THIS window.

    if delivered_probe_errors:
        # We could not look where delivery proof would be — cannot
        # distinguish a/b/on-time. (c).
        return Classification(
            OUTCOME_UNMEASURABLE, probe_errors=probe_errors,
        )

    if ran is True and isinstance(last_exit, int) and last_exit != 0:
        # The scheduler fired and the script's most recent exit was
        # non-zero: it died before producing delivery proof. A definite
        # miss even without declared evidence — eight days of exit-1
        # crashes must never read as "can't tell" (how the OC-2026.6
        # /api/message regression stayed masked).
        cause: str | None = "script_error"
        caused_by = None
        if gateway_down_signal_id:
            cause, caused_by = "gateway_down", gateway_down_signal_id
        return Classification(
            OUTCOME_MISSED,
            diagnosis=DIAGNOSIS_RAN_UNDELIVERED,
            suspected_cause=cause,
            probe_errors=probe_errors,
            caused_by_signal_id=caused_by,
            last_exit=last_exit,
        )

    if not delivered_declared and ran is not False:
        # Ran cleanly (or the ran-probe broke) and the app declares no
        # deterministic delivery proof — genuinely cannot confirm the
        # user saw anything. (c), reached honestly. A no-fire window
        # falls through: did_not_run needs no delivery evidence.
        return Classification(
            OUTCOME_UNMEASURABLE,
            probe_errors=probe_errors + [UNDECLARED_EVIDENCE_NOTE],
        )

    if ran is True:
        cause = None
        caused_by = None
        if gateway_down_signal_id:
            cause, caused_by = "gateway_down", gateway_down_signal_id
        return Classification(
            OUTCOME_MISSED,
            diagnosis=DIAGNOSIS_RAN_UNDELIVERED,
            suspected_cause=cause,
            probe_errors=probe_errors,
            caused_by_signal_id=caused_by,
        )

    if ran is None:
        return Classification(
            OUTCOME_UNMEASURABLE, probe_errors=probe_errors,
        )

    # ran is False — probes healthy, no fire observed.
    cause = None
    caused_by = None
    if slept_overlap_signal_id:
        cause, caused_by = "host_asleep", slept_overlap_signal_id
    elif dst:
        cause = "dst"
    elif loaded is False:
        cause = "not_loaded"
    return Classification(
        OUTCOME_MISSED,
        diagnosis=DIAGNOSIS_DID_NOT_RUN,
        suspected_cause=cause,
        probe_errors=probe_errors,
        caused_by_signal_id=caused_by,
    )


def classify_oc_cron_ran(
    job_state: dict | None, window: Window,
) -> tuple[bool | None, list[str]]:
    """OpenClaw-cron "ran?" evidence from one jobs-state entry (§6.1).

    ``job_state`` is the per-job dict from ``read_jobs_state`` (or None
    when the job wasn't found — probes healthy but no registration ⇒
    did_not_run territory, the caller decides). ``lastRunAtMs`` inside
    the window ⇒ ran. ``lastRunStatus != ok`` on that run still means
    "ran" — the run fired and failed, which is the ran_undelivered shape.
    """
    if job_state is None:
        return False, []
    state = job_state.get("state") or {}
    last_ms = state.get("lastRunAtMs")
    if not isinstance(last_ms, (int, float)):
        return False, []
    last_run = datetime.fromtimestamp(last_ms / 1000.0, tz=window.start.tzinfo)
    return window.contains(last_run) or last_run > window.end, []


# ─────────────────────────────────────────────────────────────────────────────
# Heal (§8) — one attempt per missed window, gated, honest either way
# ─────────────────────────────────────────────────────────────────────────────


def _heal_state_for_window(entry: dict, window: "Window") -> dict:
    """Per-window heal slot in the action's state entry.

    "One attempt per missed window" (§8) is enforced by keying the slot
    on the window end and persisting it in ``state.json`` — a monitor
    restart mid-window finds the recorded attempt and does not rerun.
    A new window resets the slot.
    """
    hs = entry.get("heal_state")
    if not isinstance(hs, dict) or hs.get("window_end") != _iso(window.end):
        hs = {"window_end": _iso(window.end)}
        entry["heal_state"] = hs
    return hs


def _heal_not_attempted(action_name: str, result: str, **extra: Any) -> dict:
    return {"attempted": False, "action": action_name, "result": result, **extra}


def _heal_launchd(action: "MonitoredAction", probes: Probes) -> dict:
    """§8 rows 1–2: kickstart a loaded job; lint + bootstrap + kickstart
    a plist-exists-not-loaded one.

    The first command is the ``launchctl print`` grant probe — a
    ``sudo -n`` denial returns ``result: "no_grant"`` so the operator
    message can say "couldn't attempt the restart" (tri-state, never
    silence). ``result: "restarted"`` asserts only that the restart
    command succeeded; delivery is judged later, by evidence.
    """
    if action.mechanism not in LAUNCHD_MECHANISMS:
        # OpenClaw-cron forced runs: the deployed runtime seam
        # (AgentRuntime) exposes cron_list/cron_runs only — no "run job
        # now" interface. Report-only until upstream grows one (§15 OQ2).
        return _heal_not_attempted("none", "no_forced_run_interface")
    label = action.label
    if not label:
        return _heal_not_attempted("none", "no_label")

    job, err = probes.launchctl_print(label)
    if job is None:
        return _heal_not_attempted("kickstart", "no_grant", error=err)

    if job.get("loaded"):
        ok, err = probes.launchctl_kickstart(label)
        return {
            "attempted": True,
            "action": "kickstart",
            "result": "restarted" if ok else "kickstart_failed",
            **({"error": err} if err else {}),
        }

    # Plist exists but the label isn't loaded (the §13 deliberately-
    # broken proof case; also post-migration drift). Lint before
    # bootstrap — a malformed plist gets reported, not bootstrapped.
    plist = LAUNCHD_PLIST_DIR / f"{label}.plist"
    mt, mt_err = probes.mtime(plist)
    if mt_err:
        return _heal_not_attempted(
            "bootstrap+kickstart", "plist_unreadable", error=mt_err,
        )
    if mt is None:
        return _heal_not_attempted("bootstrap+kickstart", "plist_missing")
    lint_ok, lint_err = probes.plutil_lint(plist)
    if lint_ok is None:
        return _heal_not_attempted(
            "bootstrap+kickstart", "plutil_unavailable", error=lint_err,
        )
    if not lint_ok:
        return _heal_not_attempted("bootstrap+kickstart", "plist_invalid")
    ok, err = probes.launchctl_bootstrap(plist)
    if not ok:
        return {
            "attempted": True, "action": "bootstrap+kickstart",
            "result": "bootstrap_failed", **({"error": err} if err else {}),
        }
    ok, err = probes.launchctl_kickstart(label)
    return {
        "attempted": True,
        "action": "bootstrap+kickstart",
        "result": "restarted" if ok else "kickstart_failed",
        **({"error": err} if err else {}),
    }


def attempt_heal(
    action: "MonitoredAction",
    cls: Classification,
    *,
    window: "Window",
    entry: dict,
    probes: Probes,
    now: datetime,
) -> dict:
    """§8 heal policy: one attempt per missed window, gated on declared
    safety, honest regardless of outcome.

    Returns the §7 ``details.heal`` dict. Never reports success without
    delivery evidence — ``result: "restarted"`` means the command ran;
    the delivery verdict comes from later ticks (🟢 recovery, or the
    ``HEAL_WAIT_MINUTES`` escalation to 🔴 "the restart didn't work").
    """
    if action.contract.heal != "rerun":
        return _heal_not_attempted("none", "no_heal_declared")
    if action.app_id not in HEAL_CANARY_APP_IDS:
        return _heal_not_attempted("none", "canary_holdback")
    if cls.suspected_cause == "dst":
        # launchd's own DST behavior is the authority (§6.3) — never rerun.
        return _heal_not_attempted("none", "no_heal_for_dst")
    if cls.suspected_cause == "host_asleep":
        # launchd coalesces missed calendar jobs on wake; the run is
        # already queued (§6.3) — a kickstart would double-fire it.
        return _heal_not_attempted("none", "no_heal_while_asleep")
    if cls.suspected_cause == "script_error":
        # The script itself crashed (non-zero exit). A kickstart would
        # re-run the same crashing code — the fix is the app, not the
        # scheduler.
        return _heal_not_attempted("none", "no_heal_for_script_error")

    hs = _heal_state_for_window(entry, window)
    prior = hs.get("heal")
    if isinstance(prior, dict):
        return prior  # one attempt per window, restart-proof via state.json

    if cls.suspected_cause == "gateway_down" and cls.caused_by_signal_id:
        # §6.3: never rerun an app into a dead gateway. heal.py owns the
        # gateway restart; queue ONE deferred rerun for this window,
        # fired when the gateway Signal clears. §9.2's follow-up promise
        # is binding — this path ends in 🟢 or 🔴, never silence (the
        # pending-miss recheck in check_action owns both endings).
        hs["deferred_gateway_signal_id"] = cls.caused_by_signal_id
        heal = _heal_not_attempted("deferred_rerun", "waiting_for_gateway")
        hs["heal"] = heal
        return heal

    heal = _heal_launchd(action, probes)
    heal["attempted_at"] = _iso(now)
    hs["heal"] = heal
    return heal


def _gateway_signal_cleared(shared_dir: Path, signal_id: str) -> bool:
    """True when the linked gateway-down Signal is no longer active —
    the deferred rerun's go-condition (§6.3). A vanished Signal counts
    as cleared (retention prune); a store READ failure does not (keep
    waiting rather than rerun into a possibly-dead gateway).
    """
    try:
        found = signals_store.find_signal(shared_dir, signal_id)
    except Exception as exc:  # noqa: BLE001 — store read failure: stay deferred
        _log.warning("[%s] gateway-signal recheck failed: %s", PRODUCER, exc)
        return False
    if found is None:
        return True
    sig, _path, _subdir = found
    return sig.state not in ("firing", "snoozed")


# ─────────────────────────────────────────────────────────────────────────────
# Monitored set (manifests → actions)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MonitoredAction:
    bot_id: str
    app_id: str
    app_name: str
    action_id: str
    mechanism: str
    schedule: dict | None          # {"cron": {...}} | {"every_minutes": N} | None
    label: str | None              # substituted plist label (launchd mechanisms)
    workspace: Path
    contract: EffectiveContract
    action_state: str              # "active" | "paused" | "disabled"
    installed_at: str | None
    schedule_human: str

    @property
    def key(self) -> str:
        return f"{self.bot_id}/{self.app_id}/{self.action_id}"


def _substitute(value: str, *, bot_user: str, workspace: Path) -> str:
    return (
        value.replace("${bot_id}", bot_user)
        .replace("${workspace}", str(workspace))
    )


def _action_label(action: dict, *, bot_user: str, workspace: Path) -> str | None:
    label = (action.get("install") or {}).get("plist_label") or ""
    if not label:
        artifact = action.get("installed_artifact") or ""
        if artifact.endswith(".plist"):
            label = Path(artifact).stem
    if not label:
        return None
    return _substitute(label, bot_user=bot_user, workspace=workspace)


def _schedule_human(action: dict) -> str:
    trig = action.get("trigger") or {}
    if isinstance(trig, dict) and trig.get("schedule"):
        return str(trig["schedule"])
    sched = (action.get("install") or {}).get("schedule") or {}
    if "cron" in sched:
        cron = sched["cron"] or {}
        return f"{int(cron.get('Hour', 0)):02d}:{int(cron.get('Minute', 0)):02d}"
    if "every_minutes" in sched:
        return f"every {sched['every_minutes']} min"
    return ""


def _hydrate_if_v7_arc(data: dict, shared_dir: Path | None) -> dict:
    """Overlay a v7-arc Instance's bound Spec so the monitor sees the
    Spec's user-facing delivery declaration.

    The on-disk Instance's ``scheduled_actions[]`` are extracted from the
    workspace (``quality="extracted"``) and can land with ``outputs: []`` /
    no ``delivery_contract`` — silently dropping the gallery delivery out of
    the monitored set. ``hydrate_v7_arc_instance`` merges the Spec's
    per-action ``outputs[].channel`` + ``delivery_contract`` back in. No-op
    for non-v7-arc manifests and when shared_dir is unset. Best-effort: any
    failure leaves the raw manifest (read-only — never mutates on disk)."""
    if shared_dir is None:
        return data
    try:
        from evolve_admin.applications.manifest import (  # type: ignore
            hydrate_v7_arc_instance,
        )
        return hydrate_v7_arc_instance(data, shared_dir)
    except Exception as exc:  # noqa: BLE001 — hydration is enrichment, not load-bearing
        _log.warning("[%s] hydrate failed for %s: %s",
                     PRODUCER, data.get("id"), exc)
        return data


def iter_bot_manifests(
    bot_id: str, workspace: Path, probes: Probes,
    shared_dir: Path | None = None,
) -> tuple[list[dict], str | None]:
    """Load every manifest JSON for one bot.

    Returns (manifests, workspace_error). A directory we cannot even
    list is a workspace_error — the caller reports the whole bot
    unmeasurable instead of silently monitoring nothing (tri-state).
    A missing manifests dir is a normal empty result (fresh bot).

    When *shared_dir* is provided, v7-arc Instances are hydrated against
    their bound Spec so a scanner-extracted gallery delivery keeps the
    Spec's ``delivery_contract`` (and stays in the monitored set).
    """
    manifests_dir = workspace / "manifests"
    try:
        entries = sorted(manifests_dir.iterdir())
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"cannot list {manifests_dir}: {exc}"

    manifests: list[dict] = []
    for path in entries:
        name = path.name
        if not name.endswith(".json") or name.startswith((".", "_")):
            continue
        text, err = probes.read_text(path)
        if text is None:
            if err:
                _log.warning("[%s] %s unreadable: %s", PRODUCER, path, err)
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # A corrupt manifest drops its apps out of the monitored set;
            # say so rather than vanishing them silently. Tier-2 owns the
            # actual corruption finding.
            _log.warning("[%s] %s unparseable: %s", PRODUCER, path, exc)
            continue
        if isinstance(data, dict) and data.get("id"):
            manifests.append(_hydrate_if_v7_arc(data, shared_dir))
    return manifests, None


def build_monitored_set(
    network: dict[str, Any], probes: Probes,
    shared_dir: Path | None = None,
) -> tuple[list[MonitoredAction], list[dict], dict[str, str]]:
    """Walk network.json bots (explicit pod membership — never /Users
    scans) and collect monitorable actions.

    Returns (monitored, coverage_rows, workspace_errors_by_bot).
    coverage_rows are §6.1's visible exclusions: unmonitorable
    mechanisms and excluded schedulers, one row per action.

    When *shared_dir* is provided, v7-arc Instances are hydrated against
    their bound Spec (so a scanner-extracted gallery delivery keeps the
    Spec's ``delivery_contract`` and stays in scope).
    """
    monitored: list[MonitoredAction] = []
    coverage: list[dict] = []
    workspace_errors: dict[str, str] = {}

    bots = network.get("bots") or {}
    for bot_id in sorted(bots):
        try:
            home = bot_home(bot_id, network)
        except Exception as exc:  # noqa: BLE001 — misconfigured bot entry
            workspace_errors[bot_id] = f"bot_home({bot_id}): {exc}"
            continue
        workspace = home / ".openclaw" / "workspace"
        bot_user = home.name
        manifests, ws_err = iter_bot_manifests(
            bot_id, workspace, probes, shared_dir=shared_dir,
        )
        if ws_err:
            workspace_errors[bot_id] = ws_err
            continue
        for manifest in manifests:
            if manifest.get("status") in ("hidden", "dormant", "deprecated"):
                continue
            app_id = str(manifest.get("id"))
            app_name = manifest.get("display_name") or manifest.get("name") or app_id
            for action in manifest.get("scheduled_actions") or []:
                if not isinstance(action, dict) or not action.get("id"):
                    continue
                action_id = str(action["id"])
                mechanism = (action.get("mechanism") or "").strip()
                contract = effective_contract(manifest, action)
                base = dict(
                    bot_id=bot_id, app_id=app_id, app_name=app_name,
                    action_id=action_id, mechanism=mechanism,
                )
                if not contract.user_facing:
                    continue  # §2: only user-facing deliveries are in scope
                if mechanism in UNMONITORABLE_MECHANISMS or mechanism in EXCLUDED_MECHANISMS:
                    coverage.append({**base, "outcome": OUTCOME_UNMONITORABLE})
                    continue
                if mechanism not in LAUNCHD_MECHANISMS:
                    coverage.append({**base, "outcome": OUTCOME_UNMONITORABLE})
                    continue
                schedule = (action.get("install") or {}).get("schedule")
                if not isinstance(schedule, dict) or not schedule:
                    coverage.append({
                        **base, "outcome": OUTCOME_UNMONITORABLE,
                        "reason": "no install.schedule",
                    })
                    continue
                monitored.append(MonitoredAction(
                    bot_id=bot_id,
                    app_id=app_id,
                    app_name=app_name,
                    action_id=action_id,
                    mechanism=mechanism,
                    schedule=schedule,
                    label=_action_label(action, bot_user=bot_user, workspace=workspace),
                    workspace=workspace,
                    contract=contract,
                    action_state=str(action.get("state") or "active"),
                    installed_at=action.get("installed_at"),
                    schedule_human=_schedule_human(action),
                ))
    return monitored, coverage, workspace_errors


# ─────────────────────────────────────────────────────────────────────────────
# Evidence gathering for one action/window
# ─────────────────────────────────────────────────────────────────────────────


def _stdout_log_path(action: MonitoredAction) -> Path | None:
    """The mechanism's stdout log (install_helpers wiring)."""
    if action.mechanism == "launchd_python_signal":
        return action.workspace / "evolve" / "scheduled" / "logs" / f"{action.action_id}.log"
    if action.label:
        return Path(f"/tmp/{action.label}.out.log")
    return None


def _parse_sent_at(text: str, tz: tzinfo) -> datetime | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("sent_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def gather_delivered(
    action: MonitoredAction, window: Window, probes: Probes,
) -> tuple[datetime | None, list[str], bool, bool]:
    """Delivery evidence per the effective contract.

    Returns (delivered_at, probe_errors, use_ran, declared) where
    ``use_ran`` marks the ``scheduler_state`` delivered-kind
    (conditional-delivery polling apps: the poll running IS the
    strongest deterministic proof) and ``declared`` is False when the
    contract carries no delivery evidence at all — classification then
    decides between unmeasurable (healthy run, nothing to check) and a
    definite miss (non-zero exit / no fire).
    """
    evidence = action.contract.delivered
    tz = window.start.tzinfo or host_timezone()
    if evidence is None:
        return None, [], False, False

    kind = evidence.get("kind")
    if kind == "scheduler_state":
        return None, [], True, True

    if kind == "run_file":
        rel = str(evidence.get("path") or "")
        rel = rel.replace("{date}", window.start.strftime("%Y-%m-%d"))
        path = action.workspace / rel
        text, err = probes.read_text(path)
        if err:
            return None, [err], False, True
        if text is None:
            return None, [], False, True
        delivered_at = _parse_sent_at(text, tz)
        if delivered_at is None:
            mt, mt_err = probes.mtime(path)
            if mt is not None:
                delivered_at = datetime.fromtimestamp(mt, tz=tz)
            elif mt_err:
                return None, [mt_err], False, True
        return delivered_at, [], False, True

    if kind == "signal_line":
        log = evidence.get("log")
        path = (action.workspace / log) if log else _stdout_log_path(action)
        if path is None:
            return None, ["signal_line evidence has no resolvable log path"], False, True
        text, err = probes.read_text(path)
        if err:
            return None, [err], False, True
        if text is None:
            return None, [], False, True
        pattern = str(evidence.get("pattern") or "")
        if pattern and any(pattern in line for line in text.splitlines()):
            mt, mt_err = probes.mtime(path)
            if mt is None:
                return None, [mt_err or f"no mtime for {path}"], False, True
            return datetime.fromtimestamp(mt, tz=tz), [], False, True
        return None, [], False, True

    return None, [f"unknown delivered evidence kind {kind!r}"], False, True


def gather_ran(
    action: MonitoredAction, window: Window, probes: Probes,
) -> tuple[bool | None, bool | None, int | None, list[str]]:
    """Scheduler-state "ran?" evidence (§6.1).

    Returns (ran, loaded, last_exit, errors). ``ran`` is tri-state: a
    positive fire observation (stdout-log mtime inside/after the window)
    short-circuits; a NEGATIVE claim requires the launchctl probe
    healthy, otherwise ran=None (unmeasurable). ``last_exit`` is the
    job's most recent exit status when launchctl reports one.
    """
    errors: list[str] = []
    tz = window.start.tzinfo or host_timezone()

    log_path = _stdout_log_path(action)
    log_fired = False
    if log_path is not None:
        mt, mt_err = probes.mtime(log_path)
        if mt_err:
            errors.append(mt_err)
        elif mt is not None:
            log_fired = datetime.fromtimestamp(mt, tz=tz) >= window.start

    loaded: bool | None = None
    last_exit: int | None = None
    if action.label:
        job, job_err = probes.launchctl_job(action.label)
        if job_err:
            errors.append(job_err)
        elif job is not None:
            loaded = job.get("loaded")
            if not job.get("running"):
                # A live PID means LastExitStatus belongs to a PREVIOUS
                # run — attributing it to the in-flight one would flag
                # false script_error misses (or mask real ones).
                raw_exit = job.get("last_exit")
                last_exit = raw_exit if isinstance(raw_exit, int) else None
    else:
        errors.append("no plist label resolvable for launchd action")

    if log_fired:
        return True, loaded, last_exit, errors
    if errors:
        # No positive fire evidence AND at least one probe broke — we
        # cannot honestly assert "did not run".
        return None, loaded, last_exit, errors
    return False, loaded, last_exit, errors


# ─────────────────────────────────────────────────────────────────────────────
# Cross-signal context (§6.3 — host_slept overlap, gateway-down linkage)
# ─────────────────────────────────────────────────────────────────────────────


def active_sleep_signal(shared_dir: Path, window: Window) -> tuple[str | None, datetime | None]:
    """An active host_slept Signal overlapping the window, if any.

    Returns (signal_id, wake_at). host_health records the gap in
    details.last_sleep_at / last_wake_at (epoch seconds).
    """
    try:
        signals = list(signals_store.iter_active(
            shared_dir, producer="host_health", state="firing",
        ))
    except Exception as exc:  # noqa: BLE001 — store read failure: no linkage
        _log.warning("[%s] sleep-signal lookup failed: %s", PRODUCER, exc)
        return None, None
    tz = window.start.tzinfo
    for sig in signals:
        if sig.type != "host_slept":
            continue
        details = sig.details or {}
        slept_at = details.get("last_sleep_at")
        wake_at = details.get("last_wake_at")
        try:
            slept = datetime.fromtimestamp(float(slept_at), tz=tz) if slept_at else None
            woke = datetime.fromtimestamp(float(wake_at), tz=tz) if wake_at else None
        except (TypeError, ValueError, OSError):
            continue
        if slept is None:
            continue
        sleep_end = woke or window.end
        if slept <= window.end and sleep_end >= window.start:
            return sig.id, woke
    return None, None


def active_gateway_down_signal(shared_dir: Path, bot_id: str) -> str | None:
    """An active gateway-down-shaped Signal for this bot, if any (§6.3)."""
    try:
        for sig in signals_store.iter_active(
            shared_dir, bot_id=bot_id, state="firing",
        ):
            if "gateway" in (sig.type or ""):
                return sig.id
    except Exception as exc:  # noqa: BLE001 — store read failure: no linkage
        _log.warning("[%s] gateway-signal lookup failed: %s", PRODUCER, exc)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# State + ledger
# ─────────────────────────────────────────────────────────────────────────────


def _monitor_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "delivery_monitor"


def _state_path(shared_dir: Path) -> Path:
    return _monitor_dir(shared_dir) / "state.json"


def load_state(shared_dir: Path) -> dict[str, Any]:
    path = _state_path(shared_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def append_ledger(shared_dir: Path, row: dict[str, Any], now: datetime) -> None:
    """One JSONL row per classified window (§6.5). U0 reads this."""
    ledger_dir = _monitor_dir(shared_dir) / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ─────────────────────────────────────────────────────────────────────────────
# Signal specs (§7)
# ─────────────────────────────────────────────────────────────────────────────


def _signature(sig_type: str, action: MonitoredAction) -> str:
    # One active Signal per ACTION, not per window — repeat misses bump
    # observation_count; the per-window record lives in the ledger.
    return make_signature(
        PRODUCER, sig_type,
        f"{action.bot_id}:{action.app_id}:{action.action_id}",
    )


def find_template_unresolved(
    network: dict[str, Any], probes: Probes,
) -> list[dict]:
    """Scan installed launchd command actions for helper scripts whose
    on-disk body still carries unsubstituted template placeholders.

    This is the exit-0-masked failure mode (Atlas Daily Digest 2026-06-16):
    a cron wrapper shipped with literal ``{bot_id}`` / ``{telegram_chat_id}``
    placeholders fails on every run, but its trailing ``exit 0`` reports
    success to launchd, so ``check_action`` (even when it monitors the
    action) can't tell. Deliberately INDEPENDENT of the user-facing gate:
    a wrapper that never resolves is broken whether or not the action
    declares a delivery contract — and Atlas's extracted action declares
    none, so it would otherwise be invisible.

    Returns findings: ``{bot_id, app_id, app_name, action_id, script_path,
    residual: [tokens]}``. Read-only; never mutates manifests or scripts.
    """
    try:
        from evolve_admin.applications.placeholder_lint import (  # type: ignore
            helper_script_from_command,
            scan_residual_placeholders,
        )
    except Exception as exc:  # noqa: BLE001 — admin package not importable
        _log.warning("[%s] placeholder_lint unavailable: %s", PRODUCER, exc)
        return []

    findings: list[dict] = []
    bots = network.get("bots") or {}
    for bot_id in sorted(bots):
        try:
            home = bot_home(bot_id, network)
        except Exception:  # noqa: BLE001 — misconfigured bot entry
            continue
        workspace = home / ".openclaw" / "workspace"
        manifests, ws_err = iter_bot_manifests(bot_id, workspace, probes)
        if ws_err:
            continue
        for manifest in manifests:
            if manifest.get("status") in ("hidden", "dormant", "deprecated"):
                continue
            app_id = str(manifest.get("id"))
            app_name = (
                manifest.get("display_name") or manifest.get("name") or app_id
            )
            for action in manifest.get("scheduled_actions") or []:
                if not isinstance(action, dict) or not action.get("id"):
                    continue
                if (action.get("mechanism") or "").strip() not in LAUNCHD_MECHANISMS:
                    continue
                command = ((action.get("install") or {}).get("command") or "").strip()
                if not command:
                    continue
                # Resolve the two install vars the materializer resolves, so
                # the script path matches what the plist actually runs.
                resolved = command.replace("${bot_id}", bot_id).replace(
                    "${workspace}", str(workspace),
                )
                script_path, is_shell = helper_script_from_command(
                    resolved, workspace_root=workspace,
                )
                if script_path is None:
                    continue
                text, _err = probes.read_text(script_path)
                if text is None:
                    continue
                residual = scan_residual_placeholders(text, shell_script=is_shell)
                if residual:
                    findings.append({
                        "bot_id": bot_id,
                        "app_id": app_id,
                        "app_name": app_name,
                        "action_id": str(action["id"]),
                        "script_path": str(script_path),
                        "residual": residual,
                    })
    return findings


def _template_unresolved_spec(finding: dict) -> dict[str, Any]:
    """Signal spec for a launchd wrapper that's still a template."""
    residual = finding.get("residual") or []
    app_name = finding.get("app_name") or finding.get("app_id") or "an app"
    return {
        "signature": make_signature(
            PRODUCER, TYPE_TEMPLATE_UNRESOLVED,
            f"{finding['bot_id']}:{finding['app_id']}:{finding['action_id']}",
        ),
        "producer": PRODUCER,
        "type": TYPE_TEMPLATE_UNRESOLVED,
        "flavor": "activity",
        "severity": "warn",
        "scope": "bot",
        "bot_id": finding["bot_id"],
        "category": "platform",
        "title": f"{app_name}'s scheduled script never finished setup",
        "body": (
            f"{finding['bot_id']}'s {app_name} runs a scheduled script that "
            f"still has unfilled template placeholders "
            f"({', '.join(residual)}), so every run fails before doing "
            f"anything — and the script's `exit 0` hides that from the "
            f"scheduler. Re-forge the app (or fix the wrapper) so the "
            f"placeholders resolve to real values."
        ),
        "details": {
            "app_id": finding["app_id"],
            "app_name": app_name,
            "action_id": finding["action_id"],
            "script_path": finding["script_path"],
            "residual_placeholders": residual,
            "diagnosis": "script_template_unresolved",
        },
    }


def _missed_spec(
    action: MonitoredAction, window: Window, cls: Classification,
    consecutive_misses: int, heal: dict,
) -> dict[str, Any]:
    """Build the missed-delivery Signal spec, heal-aware (§8 / §9.2).

    Severity: ``alert`` on ≥2 consecutive missed windows OR a failed
    heal; otherwise ``warn`` (the first miss becomes a 🟢-only story
    when healed fast under the notifier's M2 path). Copy is operator-
    facing — Plex test, no launchd/kickstart/path jargon.
    """
    heal_result = heal.get("result")
    severity = (
        "alert"
        if consecutive_misses >= MISS_ESCALATE_AFTER
        or heal_result in HEAL_FAILURE_RESULTS
        else "warn"
    )
    deferred = heal.get("action") == "deferred_rerun"
    if cls.diagnosis == DIAGNOSIS_RAN_UNDELIVERED:
        headline = f"{action.app_name} ran but the message didn't reach you"
    else:
        headline = f"{action.app_name} didn't run on schedule"

    if cls.suspected_cause == "gateway_down":
        # §9.2 third message. The follow-up promise is made ONLY when a
        # deferred rerun is actually queued — a detection-only app must
        # not promise a retry it will never perform.
        body = (
            f"{action.bot_id} prepared today's {action.app_name}, but its "
            "messaging connection was down when it tried to send."
        )
        if deferred:
            body += (
                " Evolve is restarting the connection and will retry once "
                "it's back.\nNo action needed yet — you'll get a follow-up "
                "either way."
            )
        else:
            body += f"\nCheck {action.bot_id}'s Apps page for details."
    else:
        body = (
            f"{action.bot_id}'s {action.schedule_human or 'scheduled'} "
            f"{action.app_name} missed its delivery window "
            f"({window.start.strftime('%H:%M')}–{window.end.strftime('%H:%M')})."
        )
        if cls.suspected_cause == "host_asleep":
            body += " The computer was asleep during the window."
        elif cls.suspected_cause == "dst":
            body += " A daylight-saving clock change overlapped the window."
        elif cls.suspected_cause == "script_error":
            body += " The app hit an error while running" + (
                f" (exit status {cls.last_exit})."
                if cls.last_exit is not None
                else "."
            )
        if heal_result == "restarted":
            body += " Evolve restarted it and is watching for the delivery."
        elif heal_result in HEAL_FAILURE_RESULTS:
            body += " Evolve tried an automatic restart, but it didn't work."
        elif heal_result == "no_grant":
            body += " Evolve couldn't attempt an automatic restart."
        if consecutive_misses > 1:
            body += f" This is {consecutive_misses} missed windows in a row."
    return {
        "signature": _signature(TYPE_MISSED, action),
        "producer": PRODUCER,
        "type": TYPE_MISSED,
        "flavor": "activity",
        "severity": severity,
        "scope": "bot",
        "bot_id": action.bot_id,
        "category": "platform",
        "title": headline,
        "body": body,
        "details": {
            "app_id": action.app_id,
            "app_name": action.app_name,
            "action_id": action.action_id,
            "schedule_human": action.schedule_human,
            "window_start": _iso(window.start),
            "window_end": _iso(window.end),
            "diagnosis": cls.diagnosis,
            "suspected_cause": cls.suspected_cause,
            "last_exit": cls.last_exit,
            "consecutive_misses": consecutive_misses,
            "heal": heal,
            "recovery": None,
            "probe_errors": cls.probe_errors,
        },
        "caused_by_signal_id": cls.caused_by_signal_id,
    }


def _unmeasurable_spec(
    action: MonitoredAction, window: Window | None, cls: Classification,
    consecutive_unmeasurable: int,
) -> dict[str, Any]:
    severity = (
        "warn" if consecutive_unmeasurable >= UNMEASURABLE_ESCALATE_AFTER else "info"
    )
    return {
        "signature": _signature(TYPE_UNMEASURABLE, action),
        "producer": PRODUCER,
        "type": TYPE_UNMEASURABLE,
        "flavor": "activity",
        "severity": severity,
        "scope": "bot",
        "bot_id": action.bot_id,
        "category": "platform",
        "title": f"Can't confirm {action.app_name} is being delivered",
        "body": (
            f"Evolve couldn't check whether {action.bot_id}'s "
            f"{action.app_name} was delivered — the records it needs "
            f"aren't readable. It may still be arriving normally."
        ),
        "details": {
            "app_id": action.app_id,
            "app_name": action.app_name,
            "action_id": action.action_id,
            "schedule_human": action.schedule_human,
            "window_start": _iso(window.start) if window else None,
            "window_end": _iso(window.end) if window else None,
            "consecutive_unmeasurable": consecutive_unmeasurable,
            "probe_errors": cls.probe_errors,
        },
    }


def _heal_failed_spec(
    action: MonitoredAction, window: Window, cls: Classification,
    consecutive_misses: int, heal: dict,
) -> dict[str, Any]:
    """§8 heal_wait escalation: the restart (or deferred retry) produced
    no delivery evidence — escalate to alert with honest copy (§9.2's
    🔴 message). Reuses the missed-spec shape so observe() bumps the
    same Signal with the new severity/title/body.
    """
    spec = _missed_spec(action, window, cls, consecutive_misses, heal)
    spec["severity"] = "alert"
    spec["title"] = f"{action.app_name} didn't arrive"
    sched = action.schedule_human or "scheduled"
    if not heal.get("attempted"):
        middle = "and an automatic retry couldn't be attempted"
    elif heal.get("deferred"):
        middle = (
            "and a retry after its messaging connection came back "
            "didn't fix it"
        )
    else:
        middle = "and an automatic restart didn't fix it"
    spec["body"] = (
        f"{action.bot_id}'s {sched} {action.app_name} didn't go out as "
        f"scheduled, {middle}.\n"
        f"Check {action.bot_id}'s Apps page for details."
    )
    return spec


# ─────────────────────────────────────────────────────────────────────────────
# Pod-scope regression escalation
# ─────────────────────────────────────────────────────────────────────────────


def _installed_oc_info(
    candidates: tuple[Path, ...] = OPENCLAW_PACKAGE_JSON_CANDIDATES,
) -> dict[str, Any] | None:
    """{"version", "installed_at"} for the installed OpenClaw, else None.

    package.json's mtime is the install moment — npm rewrites the tree
    on every install, including manual `npm install -g` upgrades that
    bypass `evolve-admin menu upgrade` (the drift case this backstop
    exists for).
    """
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        return {
            "version": data.get("version") if isinstance(data, dict) else None,
            "installed_at": datetime.fromtimestamp(mtime, tz=timezone.utc),
        }
    return None


def check_pod_regression(
    shared_dir: Path,
    now: datetime,
    *,
    extra_rows: list[dict] | None = None,
) -> dict[str, Any] | None:
    """One pod-scope Signal spec when the last day's misses span enough
    apps and bots to be a platform story, else None.

    Reads the per-day ledger (today + yesterday — a rolling
    POD_REGRESSION_WINDOW_HOURS, so no midnight flap) plus this tick's
    not-yet-appended rows, and counts distinct (bot, app) instances with
    outcome=missed whose suspected cause isn't a benign host-wide
    condition (POD_REGRESSION_EXCLUDED_CAUSES).
    """
    cutoff = now - timedelta(hours=POD_REGRESSION_WINDOW_HOURS)
    rows: list[dict] = list(extra_rows or [])
    ledger_dir = _monitor_dir(shared_dir) / "ledger"
    for day_offset in (1, 0):
        day = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        try:
            text = (ledger_dir / f"{day}.jsonl").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)

    instances: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("outcome") != OUTCOME_MISSED:
            continue
        if row.get("suspected_cause") in POD_REGRESSION_EXCLUDED_CAUSES:
            continue
        ts = _parse_iso(row.get("ts"))
        if ts is None or ts < cutoff:
            continue
        bot_id, app_id = row.get("bot_id"), row.get("app_id")
        if bot_id and app_id:
            instances.add((str(bot_id), str(app_id)))

    bots = {b for b, _ in instances}
    if (
        len(bots) < POD_REGRESSION_MIN_BOTS
        or len(instances) < POD_REGRESSION_MIN_INSTANCES
    ):
        return None
    return _pod_regression_spec(instances, now)


def _pod_regression_spec(
    instances: set[tuple[str, str]], now: datetime,
) -> dict[str, Any]:
    """The single pod-scope Signal. Copy passes the Plex test — the
    headline names the user impact, and the OpenClaw update is named
    only when one was actually installed recently (no endpoint/CLI
    jargon in any of it)."""
    bots = sorted({b for b, _ in instances})
    n_inst, n_bots = len(instances), len(bots)
    oc = _installed_oc_info()
    recent_oc = None
    if oc is not None and (
        now - oc["installed_at"]
        <= timedelta(days=POD_REGRESSION_OC_RECENCY_DAYS)
    ):
        recent_oc = oc
    counts = (
        f"{n_inst} scheduled deliveries across {n_bots} of your bots were "
        "missed in the last day. Failures this broad point at "
    )
    if recent_oc is not None:
        body = (
            counts
            + "the platform, not the individual apps — the most likely "
            "cause is the OpenClaw update installed "
            f"{recent_oc['installed_at']:%b %d} "
            f"(version {recent_oc['version']}).\n"
            "The individual misses are on the Alerts page."
        )
    else:
        body = (
            counts
            + "a shared platform cause — an update, a settings change, or "
            "the messaging connection — not the individual apps.\n"
            "The individual misses are on the Alerts page."
        )
    return {
        "signature": make_signature(PRODUCER, TYPE_POD_REGRESSION, "pod"),
        "producer": PRODUCER,
        "type": TYPE_POD_REGRESSION,
        "flavor": "activity",
        "severity": "alert",
        "scope": "pod",
        "category": "platform",
        "title": "Messages from your bots may not be getting through",
        "body": body,
        "details": {
            "window_hours": POD_REGRESSION_WINDOW_HOURS,
            "missed_instances": sorted(f"{b}/{a}" for b, a in instances),
            "distinct_bots": bots,
            "thresholds": {
                "min_bots": POD_REGRESSION_MIN_BOTS,
                "min_instances": POD_REGRESSION_MIN_INSTANCES,
            },
            "openclaw": (
                {
                    "version": oc["version"],
                    "installed_at": _iso(oc["installed_at"]),
                    "recent": recent_oc is not None,
                }
                if oc is not None
                else None
            ),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-action tick
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TickResult:
    specs: list[dict] = field(default_factory=list)
    kept: set[str] = field(default_factory=set)
    ledger_rows: list[dict] = field(default_factory=list)


def _parse_iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=host_timezone())
    return parsed


def _bump_unmeasurable_once_per_day(entry: dict, now: datetime) -> int:
    """Bump the consecutive-unmeasurable counter at most once per local
    date. §7's escalation threshold counts WINDOWS, not 5-minute ticks;
    when the window itself is ambiguous (or the whole workspace is
    unreadable) there is no window cadence to count by, so a calendar
    day is the honest approximation — a daily briefing gets exactly one
    bump per missed observation day.
    """
    today = now.strftime("%Y-%m-%d")
    n = int(entry.get("consecutive_unmeasurable", 0))
    if entry.get("last_unmeasurable_bump") != today:
        n += 1
        entry["consecutive_unmeasurable"] = n
        entry["last_unmeasurable_bump"] = today
    return n


def _ledger_row(action: MonitoredAction, window: Window | None,
                cls: Classification, healed: bool = False) -> dict[str, Any]:
    return {
        "ts": _utc_now_iso(),
        "bot_id": action.bot_id,
        "app_id": action.app_id,
        "action_id": action.action_id,
        "window_start": _iso(window.start) if window else None,
        "window_end": _iso(window.end) if window else None,
        "outcome": cls.outcome,
        "diagnosis": cls.diagnosis,
        # The pod-regression aggregation excludes benign host-wide causes
        # (host_asleep/dst); rows predating this field count as unexcluded.
        "suspected_cause": cls.suspected_cause,
        "delivered_at": _iso(cls.delivered_at),
        "healed": healed,
    }


def _due_window(
    action: MonitoredAction, now: datetime, probes: Probes,
) -> tuple[Window | None, bool, list[str]]:
    """Most recent elapsed window, in the job's TZ.

    Returns (window, is_interval, probe_errors). window=None with errors
    ⇒ the window itself is ambiguous (unmeasurable); window=None without
    errors ⇒ nothing has come due yet.
    """
    tz_err: str | None = None
    tz: tzinfo | None = host_timezone()
    if action.label:
        tz_name = plist_env_tz(LAUNCHD_PLIST_DIR / f"{action.label}.plist", probes)
        tz, tz_err = job_timezone(tz_name, host_timezone())
    if tz is None:
        return None, False, [tz_err or "unresolvable job timezone"]

    grace = timedelta(minutes=action.contract.window_minutes)
    local_now = now.astimezone(tz)
    schedule = action.schedule or {}

    if "every_minutes" in schedule:
        try:
            every = int(schedule["every_minutes"])
        except (TypeError, ValueError):
            return None, False, [f"bad every_minutes {schedule.get('every_minutes')!r}"]
        if every < 1:
            return None, False, [f"bad every_minutes {every}"]
        start = local_now - timedelta(minutes=every) - grace
        return Window(start=start, end=local_now), True, []

    cron = schedule.get("cron")
    if isinstance(cron, dict) and cron:
        fire = latest_calendar_fire(cron, local_now)
        if fire is None:
            return None, False, [f"unparseable cron schedule {cron!r}"]
        window = Window(start=fire, end=fire + grace)
        if window.end > local_now:
            return None, False, []  # window still open — nothing due yet
        return window, False, []

    return None, False, [f"unrecognized schedule shape {schedule!r}"]


def check_action(
    action: MonitoredAction,
    *,
    shared_dir: Path,
    now: datetime,
    state: dict[str, Any],
    probes: Probes,
) -> TickResult:
    """Evaluate one monitored action at one monitor tick.

    Mutates the per-action entry in ``state`` (the caller persists).
    Idempotent across 5-minute ticks: each calendar window is classified
    once; a missed window stays "pending" and is re-checked for late
    delivery until the next window supersedes it.
    """
    result = TickResult()
    entry = state.setdefault("actions", {}).setdefault(action.key, {})

    def _keep_active_signal() -> None:
        active = entry.get("active_signal")
        if active == TYPE_MISSED:
            result.kept.add(_signature(TYPE_MISSED, action))
        elif active == TYPE_UNMEASURABLE:
            result.kept.add(_signature(TYPE_UNMEASURABLE, action))

    # Operator-disabled (§6.3): deliberately off is not a miss.
    if action.action_state in ("paused", "disabled"):
        if entry.get("last_disabled_logged") != action.action_state:
            entry["last_disabled_logged"] = action.action_state
            result.ledger_rows.append(_ledger_row(
                action, None, Classification(OUTCOME_DISABLED),
            ))
        entry["active_signal"] = None
        return result
    entry.pop("last_disabled_logged", None)

    window, is_interval, window_errors = _due_window(action, now, probes)

    if window is None and window_errors:
        cls = Classification(OUTCOME_UNMEASURABLE, probe_errors=window_errors)
        n = _bump_unmeasurable_once_per_day(entry, now)
        entry["active_signal"] = TYPE_UNMEASURABLE
        spec = _unmeasurable_spec(action, None, cls, n)
        result.specs.append(spec)
        result.kept.add(spec["signature"])
        result.ledger_rows.append(_ledger_row(action, None, cls))
        return result
    if window is None:
        _keep_active_signal()
        return result

    # Interval jobs: evaluate once per interval, not once per 5-minute
    # monitor tick — otherwise a 15-minute poller gets 288 sliding-window
    # ledger rows a day.
    if is_interval:
        last_end = _parse_iso(entry.get("last_window_end"))
        try:
            every = timedelta(minutes=int((action.schedule or {})["every_minutes"]))
        except (KeyError, TypeError, ValueError):
            every = timedelta(minutes=action.contract.window_minutes)
        if last_end is not None and window.end - last_end < every:
            _keep_active_signal()
            return result

    # First install (§6.3): no Signal until the first full window has
    # elapsed after we first saw (or the manifest says it installed) it.
    baseline = _parse_iso(action.installed_at) or _parse_iso(entry.get("first_seen_at"))
    if not entry.get("first_seen_at"):
        entry["first_seen_at"] = _iso(now)
    if baseline is None or baseline.astimezone(window.start.tzinfo) > window.start:
        # Installed (or first observed) mid-window or later — skip this
        # window; the next full one is the first we judge.
        entry["last_window_end"] = _iso(window.end)
        return result

    # Already classified this window? Re-check a pending miss for late
    # delivery (§6.2 "late is not a fourth state"), fire a queued
    # deferred rerun once its gateway Signal clears, and escalate a
    # heal whose wait expired (§8). Otherwise carry the active
    # condition forward unchanged.
    last_end = _parse_iso(entry.get("last_window_end"))
    if last_end is not None and last_end >= window.end:
        pending = entry.get("pending_miss")
        if pending:
            hs = entry.get("heal_state") or {}
            hs_current = hs.get("window_end") == _iso(window.end)
            # Live heal slot first (deferred reruns update it); fall back
            # to the fire-time snapshot in pending_miss so gate results
            # (no_heal_declared, canary_holdback) stay on the record.
            heal = ((hs.get("heal") or {}) if hs_current else {}) or (
                pending.get("heal") or {}
            )
            pending_cls = Classification(
                OUTCOME_MISSED,
                diagnosis=pending.get("diagnosis"),
                suspected_cause=pending.get("suspected_cause"),
                last_exit=pending.get("last_exit"),
            )

            # §6.3 deferred rerun: the single heal attempt for this
            # window, fired once the gateway-down Signal clears. The
            # §9.2 follow-up promise is binding — from here the window
            # ends in 🟢 (recovery below) or 🔴 (escalation below).
            if (
                hs_current
                and hs.get("deferred_gateway_signal_id")
                and heal.get("result") == "waiting_for_gateway"
                and _gateway_signal_cleared(
                    shared_dir, hs["deferred_gateway_signal_id"],
                )
            ):
                heal = _heal_launchd(action, probes)
                heal["attempted_at"] = _iso(now)
                heal["deferred"] = True
                hs["heal"] = heal
                if heal.get("result") == "restarted":
                    # Refresh details.heal on the firing Signal so the
                    # Alerts page shows the retry; same signal id and
                    # severity, so chat stays quiet until the verdict.
                    spec = _missed_spec(
                        action, window, pending_cls,
                        int(entry.get("consecutive_misses", 1)), heal,
                    )
                    result.specs.append(spec)
                    result.kept.add(spec["signature"])
                    return result
                # Retry couldn't run (no grant, plist gone, …) — that IS
                # the verdict; fall through to the escalation below.

            delivered_at, _errs, _use_ran, _declared = gather_delivered(action, window, probes)
            if delivered_at is not None and delivered_at > window.start:
                healed = bool(heal.get("attempted")) and heal.get("result") in (
                    "restarted", "restart_ineffective",
                )
                t = delivered_at.strftime("%H:%M")
                if healed and hs.get("escalated"):
                    # The restart had already been called ineffective —
                    # don't claim credit for a delivery 40 minutes later.
                    summary = f"It eventually arrived at {t}."
                elif healed and heal.get("deferred"):
                    summary = (
                        "Evolve retried it after its messaging connection "
                        f"came back, and it was delivered at {t}."
                    )
                elif healed:
                    summary = f"Evolve restarted it, and it was delivered at {t}."
                elif pending.get("suspected_cause") == "host_asleep":
                    summary = (
                        "The computer was asleep during the window; "
                        f"it was delivered at {t}."
                    )
                else:
                    summary = f"It was delivered at {t}."
                recovery = {
                    "delivered_at": _iso(delivered_at),
                    "summary": summary,
                    "healed": healed,
                }
                cls = Classification(OUTCOME_LATE, delivered_at=delivered_at)
                spec = _missed_spec(
                    action, window, cls,
                    int(entry.get("consecutive_misses", 1)),
                    heal or _heal_not_attempted("none", "not_attempted"),
                )
                spec["details"]["recovery"] = recovery
                spec["severity"] = "warn"
                result.specs.append(spec)   # write recovery before resolving (§7)
                result.ledger_rows.append(
                    _ledger_row(action, window, cls, healed=healed),
                )
                entry["pending_miss"] = None
                entry["consecutive_misses"] = 0
                entry["active_signal"] = None
                entry.pop("heal_state", None)
                return result

            # §8 heal_wait: a heal ran (or a deferred retry failed to)
            # and the window still has no delivery evidence — escalate
            # to alert ONCE with honest "the restart didn't work" copy.
            if hs_current and heal and not hs.get("escalated"):
                attempted_at = _parse_iso(heal.get("attempted_at"))
                wait_expired = (
                    attempted_at is not None
                    and now - attempted_at >= timedelta(minutes=HEAL_WAIT_MINUTES)
                )
                retry_dead = (
                    heal.get("deferred") and heal.get("result") != "restarted"
                    and heal.get("result") != "waiting_for_gateway"
                )
                if (heal.get("result") == "restarted" and wait_expired) or retry_dead:
                    if heal.get("result") == "restarted":
                        heal = {**heal, "result": "restart_ineffective"}
                    hs["heal"] = heal
                    hs["escalated"] = True
                    spec = _heal_failed_spec(
                        action, window, pending_cls,
                        int(entry.get("consecutive_misses", 1)), heal,
                    )
                    result.specs.append(spec)
                    result.kept.add(spec["signature"])
                    return result
        _keep_active_signal()
        return result

    # Host asleep through the window (§6.3): launchd coalesces missed
    # calendar jobs on wake — give the queued run until wake + grace
    # before classifying, so an about-to-land late delivery is reported
    # as "late (asleep)" rather than a premature miss.
    sleep_id, wake_at = active_sleep_signal(shared_dir, window)
    if sleep_id and not is_interval:
        allowance = (wake_at or now.astimezone(window.start.tzinfo)) + timedelta(
            minutes=action.contract.window_minutes,
        )
        if now.astimezone(window.start.tzinfo) < allowance:
            _keep_active_signal()
            return result  # window stays pending; classified next tick

    delivered_at, delivered_errors, use_ran, declared = gather_delivered(
        action, window, probes,
    )
    ran, loaded, last_exit, ran_errors = gather_ran(action, window, probes)

    if use_ran:
        # scheduler_state delivered-kind: the poll running is the proof —
        # but only a clean run; a non-zero exit proves nothing delivered.
        if ran is True and (last_exit is None or last_exit == 0):
            delivered_at = window.end if not is_interval else now.astimezone(window.start.tzinfo)
            delivered_at = min(delivered_at, window.end)
        delivered_errors = []

    gateway_id = None
    if delivered_at is None and ran is True:
        gateway_id = active_gateway_down_signal(shared_dir, action.bot_id)

    cls = classify_window(
        window=window,
        delivered_at=delivered_at,
        delivered_probe_errors=delivered_errors,
        ran=ran,
        ran_probe_errors=ran_errors,
        loaded=loaded,
        last_exit=last_exit,
        delivered_declared=declared,
        dst=window_straddles_dst(window),
        slept_overlap_signal_id=sleep_id,
        gateway_down_signal_id=gateway_id,
    )

    entry["last_window_end"] = _iso(window.end)

    if cls.outcome in (OUTCOME_ON_TIME, OUTCOME_LATE):
        result.ledger_rows.append(_ledger_row(action, window, cls))
        entry["consecutive_misses"] = 0
        entry["consecutive_unmeasurable"] = 0
        entry.pop("last_unmeasurable_bump", None)
        entry["pending_miss"] = None
        entry["active_signal"] = None
        entry.pop("heal_state", None)
        return result

    if cls.outcome == OUTCOME_UNMEASURABLE:
        result.ledger_rows.append(_ledger_row(action, window, cls))
        n = int(entry.get("consecutive_unmeasurable", 0)) + 1
        entry["consecutive_unmeasurable"] = n
        entry["active_signal"] = TYPE_UNMEASURABLE
        spec = _unmeasurable_spec(action, window, cls, n)
        result.specs.append(spec)
        result.kept.add(spec["signature"])
        return result

    # OUTCOME_MISSED
    entry["consecutive_unmeasurable"] = 0
    entry.pop("last_unmeasurable_bump", None)
    n = int(entry.get("consecutive_misses", 0)) + 1
    entry["consecutive_misses"] = n
    # diagnosis/cause ride along so the recovery + escalation paths can
    # reconstruct honest copy on later ticks without re-probing.
    entry["pending_miss"] = {
        "window_start": _iso(window.start), "window_end": _iso(window.end),
        "diagnosis": cls.diagnosis, "suspected_cause": cls.suspected_cause,
        "last_exit": cls.last_exit,
    }
    signal_after = INTERVAL_MISS_SIGNAL_AFTER if is_interval else 1
    heal: dict[str, Any] = {}
    if n >= signal_after:
        entry["active_signal"] = TYPE_MISSED
        heal = attempt_heal(
            action, cls, window=window, entry=entry, probes=probes, now=now,
        )
        entry["pending_miss"]["heal"] = heal  # fire-time snapshot (recovery copy)
        escalation_base = n if not is_interval else (n - INTERVAL_MISS_SIGNAL_AFTER + 1)
        spec = _missed_spec(action, window, cls, escalation_base, heal)
        result.specs.append(spec)
        result.kept.add(spec["signature"])
    else:
        # Interval job, first missed tick: ledger only (§15 OQ1 posture).
        entry["active_signal"] = None
    result.ledger_rows.append(
        _ledger_row(action, window, cls, healed=bool(heal.get("attempted"))),
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def run(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    dry_run: bool = False,
    probes: Probes | None = None,
    now: datetime | None = None,
) -> tuple[set[str], int, int]:
    """One monitor tick: collect → classify → observe/sweep → ledger.

    Returns (kept_signatures, n_fired, n_resolved).
    """
    probes = probes or Probes()
    now = now or datetime.now(tz=host_timezone())
    state = load_state(shared_dir)

    monitored, coverage_rows, workspace_errors = build_monitored_set(
        network, probes, shared_dir=shared_dir,
    )

    kept: set[str] = set()
    specs: list[dict] = []
    ledger_rows: list[dict] = []

    # §6.1: invisible-by-mechanism actions are reported once per action
    # in the ledger — visible exclusion, never a fake green.
    logged_coverage = state.setdefault("coverage_logged", {})
    for row in coverage_rows:
        cov_key = f"{row['bot_id']}/{row['app_id']}/{row['action_id']}"
        if logged_coverage.get(cov_key) == row["outcome"]:
            continue
        logged_coverage[cov_key] = row["outcome"]
        ledger_rows.append({
            "ts": _utc_now_iso(),
            "bot_id": row["bot_id"],
            "app_id": row["app_id"],
            "action_id": row["action_id"],
            "window_start": None,
            "window_end": None,
            "outcome": row["outcome"],
            "diagnosis": None,
            "delivered_at": None,
            "healed": False,
        })

    # A workspace we cannot even enumerate makes every app on that bot
    # unmeasurable — one signal per bot, not silence (§6.2c tri-state).
    # Its per-action conditions are UNKNOWN, not cleared: keep every
    # active signature for the bot so sweep_resolve doesn't archive a
    # real miss just because the monitor went blind.
    for bot_id in workspace_errors:
        try:
            for sig in signals_store.iter_active(
                shared_dir, producer=PRODUCER, bot_id=bot_id, state="firing",
            ):
                kept.add(sig.signature)
        except Exception as exc:  # noqa: BLE001 — store read failure
            print(f"[{PRODUCER}] kept-scan failed for {bot_id}: {exc}", flush=True)
    for bot_id, err in sorted(workspace_errors.items()):
        ws_action = MonitoredAction(
            bot_id=bot_id, app_id="_workspace", app_name="installed apps",
            action_id="_all", mechanism="", schedule=None, label=None,
            workspace=Path("/nonexistent"),
            contract=EffectiveContract(True, DEFAULT_WINDOW_MINUTES, None, "none", "derived"),
            action_state="active", installed_at=None, schedule_human="",
        )
        entry = state.setdefault("actions", {}).setdefault(ws_action.key, {})
        n = _bump_unmeasurable_once_per_day(entry, now)
        entry["active_signal"] = TYPE_UNMEASURABLE
        cls = Classification(OUTCOME_UNMEASURABLE, probe_errors=[err])
        spec = _unmeasurable_spec(ws_action, None, cls, n)
        specs.append(spec)
        kept.add(spec["signature"])
        ledger_rows.append(_ledger_row(ws_action, None, cls))

    for action in monitored:
        try:
            tick = check_action(
                action, shared_dir=shared_dir, now=now, state=state, probes=probes,
            )
        except Exception as exc:  # noqa: BLE001 — one action must not kill the sweep
            _log.exception("[%s] %s: check_action crashed", PRODUCER, action.key)
            cls = Classification(
                OUTCOME_UNMEASURABLE, probe_errors=[f"monitor error: {exc}"],
            )
            entry = state.setdefault("actions", {}).setdefault(action.key, {})
            n = _bump_unmeasurable_once_per_day(entry, now)
            entry["active_signal"] = TYPE_UNMEASURABLE
            spec = _unmeasurable_spec(action, None, cls, n)
            tick = TickResult(specs=[spec], kept={spec["signature"]},
                              ledger_rows=[_ledger_row(action, None, cls)])
        specs.extend(tick.specs)
        kept.update(tick.kept)
        ledger_rows.extend(tick.ledger_rows)

    # Drop state for actions that no longer exist (uninstalled apps) so
    # their signatures fall out of kept and sweep_resolve closes them.
    # Bots whose workspace listing failed keep ALL their state — their
    # actions are unknown this tick, not gone.
    live_keys = {a.key for a in monitored} | {
        f"{b}/_workspace/_all" for b in workspace_errors
    }
    actions_state = state.get("actions") or {}
    for stale in [k for k in actions_state if k not in live_keys]:
        if stale.split("/", 1)[0] in workspace_errors:
            continue
        actions_state.pop(stale, None)

    # Pod-scope escalation: enough distinct (bot, app) misses inside one
    # rolling day is ONE platform story (the 2026-06-11 P0 shape), not N
    # app stories. Aggregated from the ledger + this tick's rows; the
    # signature joins kept only while the condition holds, so
    # sweep_resolve archives the Signal as the day rolls past the misses.
    try:
        pod_spec = check_pod_regression(shared_dir, now, extra_rows=ledger_rows)
    except Exception as exc:  # noqa: BLE001 — aggregation must not kill the tick
        print(f"[{PRODUCER}] pod-regression check failed: {exc}", flush=True)
        pod_spec = None
    if pod_spec is not None:
        specs.append(pod_spec)
        kept.add(pod_spec["signature"])

    # Exit-0-masked failure: launchd cron wrappers whose on-disk body still
    # carries unsubstituted template placeholders (Atlas Daily Digest). Runs
    # over ALL launchd actions, not just the user-facing monitored set — a
    # broken wrapper is broken regardless of contract. Signatures join
    # `kept` so a re-forged/fixed wrapper auto-resolves the Signal.
    try:
        for finding in find_template_unresolved(network, probes):
            spec = _template_unresolved_spec(finding)
            specs.append(spec)
            kept.add(spec["signature"])
    except Exception as exc:  # noqa: BLE001 — detection must not kill the tick
        print(f"[{PRODUCER}] template-unresolved scan failed: {exc}", flush=True)

    n_fired = 0
    for spec in specs:
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(f"[{PRODUCER}] observe failed for {spec['signature']}: {exc}", flush=True)

    n_resolved = 0
    if not dry_run:
        for row in ledger_rows:
            try:
                append_ledger(shared_dir, row, now)
            except OSError as exc:
                print(f"[{PRODUCER}] ledger append failed: {exc}", flush=True)
        try:
            save_state(shared_dir, state)
        except Exception as exc:  # noqa: BLE001
            print(f"[{PRODUCER}] save_state failed: {exc}", flush=True)
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: delivery condition cleared",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[{PRODUCER}] sweep_resolve failed: {exc}", flush=True)

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "delivery_monitor — per-window delivery outcomes for scheduled "
            "user-facing apps. Spec: docs/spec-proactive-delivery-monitor-"
            "2026-06-10.md."
        ),
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write signals, state, or ledger",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    kept, n_fired, n_resolved = run(shared_dir, config, dry_run=args.dry_run)
    if args.dry_run:
        print(f"[{PRODUCER}] dry-run: {n_fired} would-fire", flush=True)
        return
    print(f"[{PRODUCER}] {n_fired} firings, {n_resolved} resolved", flush=True)


if __name__ == "__main__":
    main()
