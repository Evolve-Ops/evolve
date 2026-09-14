"""monitor_coverage — fire a Signal when an Evolve monitor goes silent.

Port of Security_bot's SELF_AUDIT pattern. Security_bot asked itself weekly: "am I
doing everything I should be? Is execution actually happening? Should
I add anything? Should I stop anything?" Evolve had no equivalent,
and the case for needing one was made by Security_bot itself: as of
2026-05-26, 8 of 9 Security_bot cron jobs were disabled with consecutive
errors and the weekly self-audit had been failing on Telegram delivery
for weeks. Nobody noticed because nothing watches the watchers.

This monitor walks `/Library/LaunchDaemons/ai.evolve.evolve.*.plist`,
extracts each daemon's StartInterval (or StartCalendarInterval period)
and StandardOutPath, then compares the log's mtime against the
expected cadence. A daemon whose stdout log is older than ~3× its
configured interval is "silent" — either crash-looped, unloaded, or
hung. The single rollup Signal lists every silent daemon so the
operator gets one notification per monitor-coverage problem, not one
per silent producer.

Producer: ``monitor_coverage``
Signal type: ``monitor_silent``  (pod-scoped)
Severity policy:
  - 0 silent → no signal (sweep-resolves any existing)
  - 1-2 silent → warn
  - 3+ silent → critical
  - any silent daemon in CRITICAL_DAEMONS → critical regardless of count

Designed for weekly cadence; cheap enough to run hourly if the operator
wants tighter detection of pod-health/signal-notifier going silent.

────────────────────────────────────────────────────────────────────────
Producer-liveness layer (signal type ``producer_silent``)
────────────────────────────────────────────────────────────────────────

The mtime check above watches a hand-maintained ``WATCHED_DAEMONS``
allowlist. That allowlist is exactly how the capability_gap_monitor /
engagement_amplifier_monitor crash went unnoticed for 18 days
(2026-06-05 → 06-23, a phantom ``from evolve_admin.config import
all_bot_ids``): the two monitors emit the Signals the RSI
recommendation chain is grounded on, but nobody ever added them to the
allowlist, so monitor_coverage skipped them. ``cron_exit_monitor`` DID
fire ``cron_job_failed`` for both — but that signal sat buried among a
dozen identical maintenance-lane cron failures, with no hint that THESE
two mean a whole capability went dark, and it structurally can't see the
"exited 0 but did nothing" failure mode.

So in addition to the daemon-silence rollup, this module watches a
*declarative registry* of Signal-PRODUCING analyzer monitors
(``SIGNAL_PRODUCER_MONITORS``) — the calendar-scheduled monitors that
print a JSON run-summary to stdout on every successful run. Their stdout
log therefore advances only on a clean completion, so a frozen stdout
past the cadence threshold means the producer crash-looped OR returned
early without doing its job. When one is dark, a separate
``producer_silent`` rollup fires that NAMES the starved downstream
capability per monitor ("→ app_suggester capability-gap recommendations
no longer generated"). Registry membership is the watch trigger (not the
flat allowlist), and a coverage lint
(``test_all_summary_printing_monitors_are_classified``) fails if a new
summary-printing monitor is added without being either watched or
explicitly excluded — closing the drift that let these two slip.
Complements ``cron_exit_monitor`` (exit-status, all calendar jobs,
maintenance lane); this layer is producer-semantic and catches the
silent-success mode the exit-status probe misses.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from evolve_config import get_shared_dir, load_config
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "monitor_coverage"
SIGNAL_TYPE = "monitor_silent"

DEFAULT_LAUNCHD_DIR = Path("/Library/LaunchDaemons")
# Two prefixes in active use: ai.evolve.evolve.* (admin-installed) and
# ai.openclaw.evolve.* (analyzer-pipeline pod-wide monitors). Watch both.
EVOLVE_LABEL_PREFIXES = ("ai.evolve.evolve.", "ai.openclaw.evolve.")

# Allowlist of daemons confirmed to write a log line on every successful
# wake. The log-mtime silence check is only reliable for these — many
# Evolve daemons (pod-health, signal-notifier) currently exit silently
# on success, so log mtime stays static even when they're healthy.
#
# Adding a daemon to this set should be paired with a quick check on
# the mini that its log mtime advances per cadence. Removing a daemon
# (because it stopped logging) should be paired with a per-daemon PR
# that adds heartbeat logging back — silent daemons are a coverage
# gap, not a feature.
#
# v1 set: the 6 daemons whose log activity I verified on 2026-05-26.
# Expansion is a deliberate per-daemon decision so monitor_coverage
# doesn't flood the alerts UI with findings about silent-on-purpose
# daemons before their heartbeat logging is fixed.
WATCHED_DAEMONS = frozenset({
    "ai.evolve.evolve.audit",            # writes per check, every 15min
    "ai.evolve.evolve.heal",             # writes per probe, every 5min
    "ai.evolve.evolve.cost_watchdog",    # writes per signal/cycle, hourly
    "ai.evolve.evolve.spend-alert",      # writes per check, hourly
    "ai.evolve.evolve.repo-puller",      # writes per pull attempt, 15min
    "ai.evolve.evolve.digest-flush",     # writes per tick, hourly
    "ai.evolve.evolve.signal-notifier",  # heartbeat on every wake, every 60s
    "ai.evolve.evolve.pod-health",       # heartbeat on every wake, every 60s
    # Hourly Signal producers added when the pod-admin-side openclaw-
    # watchdog.py was decommissioned (2026-06-01). Each prints a one-line
    # "kept=… fired=… resolved=…" summary every cycle, so their stdout
    # advances even on a quiet day — safe to watch with the standard
    # 3× cadence rule.
    "ai.openclaw.evolve.oc_substrate_monitor",
    "ai.openclaw.evolve.home_artifacts_monitor",
    # Proactive-delivery monitor (U2.1): prints a one-line
    # "[delivery_monitor] N firings, M resolved" summary every 5-minute
    # tick, so stdout advances even on a quiet day. A delivery watchdog
    # that dies silently is exactly the failure mode it exists to catch.
    "ai.evolve.evolve.delivery-monitor",
})

# Daemons whose silence (when watched) is severe enough to escalate
# regardless of total count. signal-notifier silence means no Signal
# transitions reach the operator's chat channel; pod-health silence
# means no gateway-down detection. Both are load-bearing for the
# alerting pipeline itself, so they escalate on first miss.
CRITICAL_DAEMONS: frozenset[str] = frozenset({
    "ai.evolve.evolve.audit",
    "ai.evolve.evolve.heal",
    "ai.evolve.evolve.signal-notifier",
    "ai.evolve.evolve.pod-health",
})

# Silence threshold = StartInterval × this multiplier, bounded by floor/cap.
# 3× is forgiving enough to absorb single missed runs (transient errors,
# rate limits, brief launchd restarts) without false-firing.
SILENCE_MULTIPLIER = 3
SILENCE_FLOOR_SEC = 5 * 60          # don't alert before 5 minutes regardless
SILENCE_CAP_SEC = 7 * 24 * 3600     # 7d cap matches weekly audit cadence

# Daemons with StartCalendarInterval but no StartInterval (daily/weekly
# jobs) get a flat threshold — coarse enough to catch a missed daily run
# but tight enough to surface a week of silence.
CALENDAR_SILENCE_SEC = 48 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# Producer-liveness layer — see the module docstring's second section.
# ─────────────────────────────────────────────────────────────────────────────

PRODUCER_SILENT_TYPE = "producer_silent"

# A registered producer is "dark" once its stdout summary log is older than
# cadence × this. 2× tolerates one missed scheduled run before firing.
PRODUCER_SILENCE_MULTIPLIER = 2


# ─────────────────────────────────────────────────────────────────────────────
# Audit-drain liveness layer — signal type ``audit_drain_silent``
# ─────────────────────────────────────────────────────────────────────────────
#
# The admin-side audit poller (audit_poller.tick, hourly under the
# ``ai.evolve.evolve.audit-scheduler`` daemon) drains every bot's audit outbox
# into the Signal store. It can run with Result=success while ingesting ZERO
# records and emit no error line anywhere: a hardcoded ``/Users/{bot}`` outbox
# path (#3310) made every root resolve to a non-existent dir on the Linux VPS,
# so the drain processed nothing for DAYS while the bots' outboxes piled to
# ~195 records and ``_ingested/`` was never created.
#
# The producer-liveness layer above CANNOT catch this. It keys on stdout-summary
# mtime, but the audit-scheduler's stdout advanced on every (empty-but-
# successful) tick — the daemon is alive, the drain is semantically dead. So the
# poller instead drops a compact rolling heartbeat — ``(ts, processed, backlog)``
# per tick — and this layer reads it: a drain that ingested 0 records across
# several consecutive ticks while the outbox roots stayed non-empty (or whose
# heartbeat has gone stale) is silently stalled. One pod-scoped Signal under the
# same ``monitor_coverage`` producer with a distinct signature, so it rides the
# existing observe()/sweep_resolve in run() with no new wiring.
#
# Scope note: the heartbeat counts the backlog via the SAME path resolution the
# drain uses, so this catches the broad silent-stall class (ingest exceptions,
# permission failures, unhandled-kind pileup, and a correctly-resolved backlog
# that simply isn't draining) plus drain-not-running. It does NOT independently
# re-derive the outbox path, so a path-resolution regression that blinds both the
# drain and this probe to the same dir stays out of scope — that class is guarded
# by #3310's fix + its regression test, not by this monitor.
AUDIT_DRAIN_SILENT_TYPE = "audit_drain_silent"
AUDIT_DRAIN_HEARTBEAT_REL = "monitors/audit_drain_heartbeat.json"
# Hourly drain cadence; tolerate a few quiet/missed ticks before firing.
AUDIT_DRAIN_IDLE_TICKS = 3          # consecutive (processed==0 & backlog>0) ticks
AUDIT_DRAIN_STALE_SEC = 6 * 3600    # heartbeat older than this ⇒ drain not running
AUDIT_DRAIN_ALERT_BACKLOG = 50      # backlog at/above this escalates warn→alert
AUDIT_DRAIN_ALERT_TICKS = 24        # a full day stalled escalates warn→alert


@dataclass(frozen=True)
class ProducerMonitor:
    """A calendar-scheduled analyzer monitor that EMITS Signals and prints a
    JSON run-summary to stdout on every successful run.

    Because the summary is the last thing ``main()`` does, the stdout log
    advances ONLY on a clean completion: a crash (or an early silent return)
    leaves it frozen. That makes stdout-mtime a robust "did it actually
    complete a run" liveness signal — and one ``cron_exit_monitor``'s
    exit-status probe cannot reproduce when the bad run exits 0.
    """

    label: str
    cadence_sec: int
    downstream: str  # what goes silent downstream when this monitor is dark


# The watch set for the producer-liveness check. Membership here — not the
# flat WATCHED_DAEMONS allowlist — is what gets a Signal producer watched, so
# a newly-added monitor is loud-by-default the way a new monitor should be.
SIGNAL_PRODUCER_MONITORS: dict[str, ProducerMonitor] = {
    "ai.openclaw.evolve.capability_gap_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.capability_gap_monitor",
        cadence_sec=24 * 3600,  # daily 03:15
        downstream=(
            "app_suggester capability-gap recommendations — when a user keeps "
            "asking about a domain no installed app covers, the suggestion to "
            "add that capability stops being generated."
        ),
    ),
    "ai.openclaw.evolve.engagement_amplifier_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.engagement_amplifier_monitor",
        cadence_sec=24 * 3600,  # daily 03:30
        downstream=(
            "engagement_amplifier recommendations — working conversational "
            "patterns worth deepening stop being surfaced for amplification."
        ),
    ),
    "ai.openclaw.evolve.evo_path_probe_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.evo_path_probe_monitor",
        cadence_sec=30 * 60,  # every 30 min (StartInterval=1800)
        downstream=(
            "the synthetic 'evo' keyword probe — when this goes dark, a "
            "pod-wide break of the gateway plugin → /api/evo/dispatch path "
            "(device-auth 401, daemon down, admin-daemon.sock path/perm break) "
            "stops being caught by anything but a human typing `evo help`."
        ),
    ),
    "ai.openclaw.evolve.model_liveness_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.model_liveness_monitor",
        cadence_sec=24 * 3600,  # daily 04:40 (probes cost real tokens)
        downstream=(
            "the model liveness probe — when this goes dark, a routed model "
            "that resolves in the catalog but fails real dispatch (the #3489 "
            "wrong-transport class: rungs silently dead fleet-wide, fallback "
            "walking to costlier models) stops being caught by anything but "
            "a real turn failing in front of a user."
        ),
    ),
    "ai.openclaw.evolve.autogen_volume_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.autogen_volume_monitor",
        cadence_sec=24 * 3600,  # daily
        downstream=(
            "the auto-gen disk-volume backstop — when this goes dark, an "
            "auto-generated directory ballooning without bound (the 537 MB "
            "audit_outbox/_ingested leak class) stops being caught until a "
            "manual disk audit. The runtime budget check is the only thing "
            "watching existing producers' output volume."
        ),
    ),
    "ai.openclaw.evolve.roster_allowlist_drift_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.roster_allowlist_drift_monitor",
        cadence_sec=3600,  # hourly
        downstream=(
            "the out-of-band group-allowlist drift safety net (R1a PR3) — when "
            "this goes dark, an OpenClaw-CLI/hand-edit widening of "
            "channels.<ch>.allowFrom (new senders who can spend the bot's "
            "tokens in a channel) stops being caught between admin GETs. The "
            "enforcement gate is still fail-closed, but the operator loses "
            "visibility into who was added out-of-band."
        ),
    ),
    "ai.openclaw.evolve.roster_coherence_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.roster_coherence_monitor",
        cadence_sec=3600,  # hourly
        downstream=(
            "the roster<->OpenClaw coherence check (M1-B3) — when this goes "
            "dark, an identity that exists in a bot's openclaw.json (DM "
            "allowlist, group allowlist, or a nested guild/channel member list) "
            "but in no Evolve roster stops being caught at all. The drift "
            "sibling cannot cover it: it seeds its baseline from the live "
            "config, so a pre-existing identity is adopted as expected and stays "
            "permanently invisible."
        ),
    ),
    "ai.openclaw.evolve.orphaned_bot_account_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.orphaned_bot_account_monitor",
        cadence_sec=86400,  # daily
        downstream=(
            "the decommissioned-bot-account check — when this goes dark, an "
            "account that carries an Evolve-provisioned OpenClaw install but "
            "backs no roster member stops being reported, and its still-live "
            "channel tokens, gateway token, and SSH keys go unnamed. Nothing "
            "else covers it: retire-bot deliberately preserves the account and "
            "home, and audit_machine's baseline diff calls the same account a "
            "NEW user — which reads as a false positive and gets ignored (the "
            "2026-08-02 ledger finding sat unactioned for seven weeks)."
        ),
    ),
    "ai.openclaw.evolve.bootstrap_size_monitor": ProducerMonitor(
        label="ai.openclaw.evolve.bootstrap_size_monitor",
        cadence_sec=3600,  # hourly
        downstream=(
            "the bootstrap-truncation check — when this goes dark, an "
            "OC-ingested workspace file (AGENTS.md, MEMORY.md, …) growing past "
            "the bot's bootstrapMaxChars stops being caught, and everything "
            "past the cap is silently absent from every turn's context (the "
            "2026-08-01 incident: a 128k AGENTS.md against a 40k cap dropped "
            "every anti-confabulation rule with only a gateway log line as "
            "witness)."
        ),
    ),
}

# Signal-producing analyzer monitors deliberately NOT watched by the
# producer-liveness check, each with the reason. The coverage lint
# (test_all_summary_printing_monitors_are_classified) requires every
# summary-printing analyzer monitor to be either watched above or listed
# here — so a future RSI monitor cannot silently fall through the gap that
# hid capability_gap_monitor for 18 days.
EXCLUDED_SIGNAL_PRODUCERS: dict[str, str] = {
    "ai.openclaw.evolve.vocabulary_expander_monitor": (
        "weekly cadence + default-off (network.json::rsi.vocabulary_expansion."
        "enabled); it legitimately writes nothing for weeks, so a stdout-mtime "
        "liveness check would false-positive. Revisit when its LLM path ships."
    ),
}


@dataclass
class DaemonInfo:
    label: str
    plist_path: Path
    start_interval: int | None       # seconds, from StartInterval
    has_calendar_schedule: bool       # StartCalendarInterval present
    stdout_path: Path | None
    stderr_path: Path | None

    @property
    def expected_cadence_desc(self) -> str:
        if self.start_interval:
            return f"every {self.start_interval}s"
        if self.has_calendar_schedule:
            return "calendar-scheduled"
        return "no scheduled cadence"


@dataclass
class SilentMonitor:
    label: str
    log_path: str | None             # None if no log path was set
    silent_for_sec: int | None        # None if log path missing entirely
    expected_max_sec: int
    reason: str                       # human-readable cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "log_path": self.log_path,
            "silent_for_sec": self.silent_for_sec,
            "expected_max_sec": self.expected_max_sec,
            "reason": self.reason,
        }


@dataclass
class SilentProducer:
    """A Signal-producing monitor that has stopped completing successful runs."""

    label: str
    downstream: str
    log_path: str | None
    stderr_path: str | None
    silent_for_sec: int | None
    expected_max_sec: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "downstream": self.downstream,
            "log_path": self.log_path,
            "stderr_path": self.stderr_path,
            "silent_for_sec": self.silent_for_sec,
            "expected_max_sec": self.expected_max_sec,
            "reason": self.reason,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Discovery — pure-ish (filesystem walk + plistlib)
# ─────────────────────────────────────────────────────────────────────────────


def discover_evolve_daemons(launchd_dir: Path = DEFAULT_LAUNCHD_DIR) -> list[DaemonInfo]:
    """Walk launchd_dir for ai.evolve.evolve.*.plist and parse each.

    Returns parsed DaemonInfo entries. Plists that fail to parse are
    skipped silently — better to under-report than to crash the audit.
    """
    if not launchd_dir.exists():
        return []
    daemons: list[DaemonInfo] = []
    seen: set[Path] = set()
    for prefix in EVOLVE_LABEL_PREFIXES:
        for plist_path in sorted(launchd_dir.glob(f"{prefix}*.plist")):
            if plist_path in seen:
                continue
            seen.add(plist_path)
            info = _parse_plist(plist_path)
            if info is not None:
                daemons.append(info)
    return daemons


def _parse_plist(plist_path: Path) -> DaemonInfo | None:
    try:
        with plist_path.open("rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    label = data.get("Label")
    if not isinstance(label, str) or not any(
        label.startswith(p) for p in EVOLVE_LABEL_PREFIXES
    ):
        return None
    start_interval = data.get("StartInterval") if isinstance(
        data.get("StartInterval"), int) else None
    has_calendar = "StartCalendarInterval" in data
    stdout = data.get("StandardOutPath")
    stderr = data.get("StandardErrorPath")
    return DaemonInfo(
        label=label,
        plist_path=plist_path,
        start_interval=start_interval,
        has_calendar_schedule=has_calendar,
        stdout_path=Path(stdout) if isinstance(stdout, str) else None,
        stderr_path=Path(stderr) if isinstance(stderr, str) else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detection — pure function
# ─────────────────────────────────────────────────────────────────────────────


def _safe_stat(p: Path):
    """``Path.stat`` that returns None on failure (missing / permission)."""
    try:
        return p.stat()
    except (OSError, FileNotFoundError):
        return None


def silence_threshold_for(daemon: DaemonInfo) -> int | None:
    """Return expected max silence in seconds, or None to skip the check.

    StartInterval daemons: interval × SILENCE_MULTIPLIER, bounded.
    Calendar daemons: CALENDAR_SILENCE_SEC (flat).
    No schedule at all: None (can't compute expectation).
    """
    if daemon.start_interval and daemon.start_interval > 0:
        raw = daemon.start_interval * SILENCE_MULTIPLIER
        return max(SILENCE_FLOOR_SEC, min(SILENCE_CAP_SEC, raw))
    if daemon.has_calendar_schedule:
        return CALENDAR_SILENCE_SEC
    return None


def detect_silent_monitors(
    daemons: Iterable[DaemonInfo],
    now: float,
    *,
    stat_fn=None,
) -> list[SilentMonitor]:
    """Return the list of daemons whose stdout log is silent past threshold.

    ``stat_fn`` is injected for tests; defaults to ``Path.stat`` and
    returns ``None`` on failure (PermissionError, FileNotFoundError).
    """
    stat = stat_fn if stat_fn is not None else _safe_stat

    silent: list[SilentMonitor] = []
    for d in daemons:
        if d.label not in WATCHED_DAEMONS:
            continue  # not yet known to log on every wake — silent-by-design risk
        threshold = silence_threshold_for(d)
        if threshold is None:
            continue  # no cadence to measure against — skip
        if d.stdout_path is None:
            silent.append(SilentMonitor(
                label=d.label,
                log_path=None,
                silent_for_sec=None,
                expected_max_sec=threshold,
                reason="no StandardOutPath configured — cannot verify activity",
            ))
            continue
        st = stat(d.stdout_path)
        if st is None:
            silent.append(SilentMonitor(
                label=d.label,
                log_path=str(d.stdout_path),
                silent_for_sec=None,
                expected_max_sec=threshold,
                reason="log file missing — daemon may have never run",
            ))
            continue
        age = int(now - st.st_mtime)
        if age > threshold:
            silent.append(SilentMonitor(
                label=d.label,
                log_path=str(d.stdout_path),
                silent_for_sec=age,
                expected_max_sec=threshold,
                reason=(
                    f"log idle for {_human_duration(age)} "
                    f"(expected activity within {_human_duration(threshold)})"
                ),
            ))
    return silent


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _short_name(label: str) -> str:
    for prefix in EVOLVE_LABEL_PREFIXES:
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


# ─────────────────────────────────────────────────────────────────────────────
# Producer-liveness detection — pure function
# ─────────────────────────────────────────────────────────────────────────────


def producer_silence_threshold(pm: ProducerMonitor) -> int:
    """Expected max silence (seconds) for a registered Signal producer."""
    return pm.cadence_sec * PRODUCER_SILENCE_MULTIPLIER


def detect_silent_producers(
    daemons: Iterable[DaemonInfo],
    now: float,
    *,
    stat_fn=None,
) -> list[SilentProducer]:
    """Return the registered Signal producers that have gone dark.

    A producer is dark when its stdout summary log hasn't advanced within
    cadence × PRODUCER_SILENCE_MULTIPLIER — i.e. it has not completed a clean
    run (crash-loop OR exit-0-did-nothing). ``stat_fn`` is injected for tests.

    Whether a registered monitor's plist is *absent* is only a finding on a
    real pod (``daemons`` non-empty); on a dev/CI host with no Evolve daemons
    we report nothing rather than false-flag every monitor as uninstalled.
    """
    stat = stat_fn if stat_fn is not None else _safe_stat

    by_label = {d.label: d for d in daemons}
    have_pod = bool(by_label)
    silent: list[SilentProducer] = []
    for label, pm in SIGNAL_PRODUCER_MONITORS.items():
        threshold = producer_silence_threshold(pm)
        d = by_label.get(label)
        if d is None:
            if have_pod:
                silent.append(SilentProducer(
                    label=label,
                    downstream=pm.downstream,
                    log_path=None,
                    stderr_path=None,
                    silent_for_sec=None,
                    expected_max_sec=threshold,
                    reason=(
                        "launchd job not found — the monitor is not installed "
                        "or was unloaded (run sudo evolve-admin install-infra-jobs)"
                    ),
                ))
            continue
        if d.stdout_path is None:
            silent.append(SilentProducer(
                label=label,
                downstream=pm.downstream,
                log_path=None,
                stderr_path=str(d.stderr_path) if d.stderr_path else None,
                silent_for_sec=None,
                expected_max_sec=threshold,
                reason="no StandardOutPath configured — cannot verify successful runs",
            ))
            continue
        st = stat(d.stdout_path)
        if st is None:
            silent.append(SilentProducer(
                label=label,
                downstream=pm.downstream,
                log_path=str(d.stdout_path),
                stderr_path=str(d.stderr_path) if d.stderr_path else None,
                silent_for_sec=None,
                expected_max_sec=threshold,
                reason="stdout summary log missing — monitor has never completed a run",
            ))
            continue
        age = int(now - st.st_mtime)
        if age > threshold:
            silent.append(SilentProducer(
                label=label,
                downstream=pm.downstream,
                log_path=str(d.stdout_path),
                stderr_path=str(d.stderr_path) if d.stderr_path else None,
                silent_for_sec=age,
                expected_max_sec=threshold,
                reason=(
                    f"no successful run in {_human_duration(age)} (expected within "
                    f"{_human_duration(threshold)}) — crash-looping or exiting "
                    f"without doing its job"
                ),
            ))
    return silent


# ─────────────────────────────────────────────────────────────────────────────
# Signal construction
# ─────────────────────────────────────────────────────────────────────────────


def build_signal_spec(silent: list[SilentMonitor]) -> dict | None:
    """Build the rollup Signal spec from a list of silent monitors.

    Returns None when nothing is silent — the caller sweep-resolves.
    Severity: warn for 1-2 silent, critical for 3+, critical if any
    CRITICAL_DAEMONS member is silent regardless of total count.
    """
    if not silent:
        return None

    has_critical = any(s.label in CRITICAL_DAEMONS for s in silent)
    if has_critical or len(silent) >= 3:
        severity = "alert"
        sev_label = "CRITICAL"
    else:
        severity = "warn"
        sev_label = "warn"

    if len(silent) == 1:
        title = f"Evolve monitor silent: {silent[0].label}"
    else:
        title = f"{len(silent)} Evolve monitors silent past expected cadence"

    body_lines = [
        f"{sev_label}: monitor coverage gap detected.",
        "",
        "Silent daemons:",
    ]
    for s in sorted(silent, key=lambda x: x.label):
        marker = " 🔴" if s.label in CRITICAL_DAEMONS else ""
        body_lines.append(f"  - `{s.label}`{marker}")
        body_lines.append(f"      {s.reason}")
    body_lines.extend([
        "",
        "Investigation:",
        "",
        "```",
        "ssh pod_admin_user@mini sudo launchctl print system/<label>",
        "ssh pod_admin_user@mini sudo launchctl kickstart -k system/<label>",
        "```",
        "",
        "If a daemon is unloaded:",
        "",
        "```",
        "sudo evolve-admin install-infra-jobs",
        "```",
    ])

    return dict(
        signature=make_signature(PRODUCER, SIGNAL_TYPE, "pod"),
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        flavor="maintenance",
        severity=severity,
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details=dict(
            silent_count=len(silent),
            has_critical=has_critical,
            silent=[s.to_dict() for s in silent],
            vector="operations",
            magnitude=3 if has_critical else (2 if len(silent) >= 3 else 1),
        ),
    )


def build_producer_silent_spec(silent: list[SilentProducer]) -> dict | None:
    """Build the ``producer_silent`` rollup from dark Signal producers.

    Returns None when nothing is dark — the caller sweep-resolves. One pod-
    scoped Signal lists every dark producer with the downstream capability it
    starves, so the operator reads "RSI recommendations stopped" rather than
    "a scheduled job exited non-zero". Severity warn for 1-2 (a real but not
    pod-down breakage), alert for 3+ (systemic).
    """
    if not silent:
        return None

    severity = "alert" if len(silent) >= 3 else "warn"

    if len(silent) == 1:
        title = (
            f"RSI signal-producer dark: {_short_name(silent[0].label)} "
            f"— recommendations stalled"
        )
    else:
        title = f"{len(silent)} RSI signal-producers dark — recommendations stalled"

    body_lines = [
        "A monitor that *emits Signals* has stopped completing successful "
        "runs, so the recommendations it feeds are no longer being generated. "
        "This is distinct from a generic scheduled-job failure: the eye itself "
        "is dark, and nothing downstream will fire until it runs cleanly again.",
        "",
        "Dark producers:",
    ]
    for s in sorted(silent, key=lambda x: x.label):
        body_lines.append(f"  - `{s.label}`")
        body_lines.append(f"      {s.reason}")
        body_lines.append(f"      → starves: {s.downstream}")

    # Surface one concrete crash log to read first (the others follow the
    # same pattern); keep the operator playbook below it.
    err_hint = next((s.stderr_path for s in silent if s.stderr_path), None)
    body_lines.extend(["", "Investigate — read the crash, then restart:", "", "```"])
    if err_hint:
        body_lines.append(f"ssh pod_admin_user@mini sudo tail -30 {err_hint}")
    body_lines.extend([
        "ssh pod_admin_user@mini sudo /bin/launchctl print system/<label>",
        "ssh pod_admin_user@mini sudo /bin/launchctl kickstart -k system/<label>",
        "```",
    ])

    return dict(
        signature=make_signature(PRODUCER, PRODUCER_SILENT_TYPE, "pod"),
        producer=PRODUCER,
        type=PRODUCER_SILENT_TYPE,
        flavor="maintenance",
        severity=severity,
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details=dict(
            silent_count=len(silent),
            silent=[s.to_dict() for s in silent],
            vector="operations",
            magnitude=3 if len(silent) >= 3 else 2,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Audit-drain liveness — detection + Signal construction
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AuditDrainStall:
    """The audit poller is running but no longer moving records off the outbox."""

    reason: str            # "silent_stall" | "heartbeat_stale"
    detail: str            # human sentence for the Signal body
    backlog: int
    idle_ticks: int
    growing: bool
    stale_for_sec: int | None
    last_ts: int | None
    severity: str          # "warn" | "alert"

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "backlog": self.backlog,
            "idle_ticks": self.idle_ticks,
            "growing": self.growing,
            "stale_for_sec": self.stale_for_sec,
            "last_ts": self.last_ts,
            "severity": self.severity,
        }


def _read_audit_drain_heartbeat(shared_dir: Path) -> list[dict]:
    """Return the rolling ``[{ts, processed, backlog}, ...]`` heartbeat samples.

    Missing / unreadable / malformed → ``[]`` (correctly a no-op: a dev host or
    a brand-new pod whose poller has not ticked yet has nothing to assess).
    """
    path = Path(shared_dir) / AUDIT_DRAIN_HEARTBEAT_REL
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    recent = data.get("recent") if isinstance(data, dict) else None
    if not isinstance(recent, list):
        return []
    out: list[dict] = []
    for e in recent:
        if isinstance(e, dict) and isinstance(e.get("ts"), (int, float)):
            out.append({
                "ts": int(e["ts"]),
                "processed": int(e.get("processed", 0) or 0),
                "backlog": int(e.get("backlog", 0) or 0),
            })
    return out


def detect_audit_drain_stall(
    recent: list[dict], now: float,
) -> AuditDrainStall | None:
    """Decide whether the audit drain is silently stalled from its heartbeat.

    Two failure shapes, in priority order:

    1. **Heartbeat stale** — no tick recorded in ``AUDIT_DRAIN_STALE_SEC`` (the
       audit-scheduler is not running or never reaches the drain). Alert.
    2. **Silent stall** — ``processed == 0`` while ``backlog > 0`` for
       ``AUDIT_DRAIN_IDLE_TICKS`` consecutive ticks: the drain runs but records
       never leave the outbox. ``backlog == 0`` ticks are NOT idle, so a quiet
       pod with nothing to drain never false-fires.
    """
    if not recent:
        return None
    recent = sorted(recent, key=lambda e: e["ts"])
    latest = recent[-1]
    stale_for = int(now - latest["ts"])

    if stale_for > AUDIT_DRAIN_STALE_SEC:
        return AuditDrainStall(
            reason="heartbeat_stale",
            detail=(
                f"the audit drain has not recorded a tick in "
                f"{_human_duration(stale_for)} (expected hourly) — the "
                f"audit-scheduler is not running or never reaches the drain"
            ),
            backlog=latest["backlog"],
            idle_ticks=0,
            growing=False,
            stale_for_sec=stale_for,
            last_ts=latest["ts"],
            severity="alert",
        )

    idle = 0
    for e in reversed(recent):
        if e["processed"] == 0 and e["backlog"] > 0:
            idle += 1
        else:
            break
    if idle >= AUDIT_DRAIN_IDLE_TICKS:
        backlog = latest["backlog"]
        oldest_idle = recent[-idle]["backlog"]
        growing = backlog > oldest_idle
        severity = (
            "alert"
            if backlog >= AUDIT_DRAIN_ALERT_BACKLOG or idle >= AUDIT_DRAIN_ALERT_TICKS
            else "warn"
        )
        rec_word = "record" if backlog == 1 else "records"
        magnitude = (
            f"growing {oldest_idle}→{backlog} {rec_word}" if growing
            else f"{backlog} {rec_word} waiting"
        )
        return AuditDrainStall(
            reason="silent_stall",
            detail=(
                f"the audit drain ingested 0 records for {idle} consecutive "
                f"ticks while the outbox roots stayed non-empty ({magnitude}) — "
                f"bot audit findings are written but never reach the Signal store"
            ),
            backlog=backlog,
            idle_ticks=idle,
            growing=growing,
            stale_for_sec=None,
            last_ts=latest["ts"],
            severity=severity,
        )
    return None


def build_audit_drain_silent_spec(stall: AuditDrainStall | None) -> dict | None:
    """Build the ``audit_drain_silent`` Signal spec, or None when healthy."""
    if stall is None:
        return None

    if stall.reason == "silent_stall":
        title = "Audit drain silent — bot audit findings not reaching Signals"
    else:
        title = "Audit drain not running — audit-scheduler heartbeat stale"

    body_lines = [
        f"{stall.detail[:1].upper()}{stall.detail[1:]}.",
        "",
        "Downstream impact: app structural-audit findings (Tier-2 → Signals) and "
        "run-summary sweeps stop landing, so the Security / Alerts surface goes "
        "silently stale even though the bot-side audit_runner keeps writing.",
        "",
        "Investigate — confirm the drain, then watch a bot outbox drain to "
        "`_ingested/`:",
        "",
        "```",
        "ssh pod_admin_user@mini sudo /bin/launchctl print "
        "system/ai.evolve.evolve.audit-scheduler",
        "ssh pod_admin_user@mini sudo /bin/launchctl kickstart -k "
        "system/ai.evolve.evolve.audit-scheduler",
        "ssh pod_admin_user@mini sudo /bin/ls "
        "~<bot>/.openclaw/workspace/evolve/audit_outbox",
        "```",
    ]

    return dict(
        signature=make_signature(PRODUCER, AUDIT_DRAIN_SILENT_TYPE, "pod"),
        producer=PRODUCER,
        type=AUDIT_DRAIN_SILENT_TYPE,
        flavor="maintenance",
        severity=stall.severity,
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details=dict(
            stall=stall.to_dict(),
            vector="operations",
            magnitude=3 if stall.severity == "alert" else 2,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    now: float | None = None,
) -> list[dict]:
    """Walk daemons, detect silence, return zero-or-one Signal spec."""
    if now is None:
        now = time.time()
    daemons = discover_evolve_daemons(launchd_dir)
    silent = detect_silent_monitors(daemons, now)
    spec = build_signal_spec(silent)
    return [spec] if spec else []


def collect_producer_silence(
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    now: float | None = None,
) -> list[dict]:
    """Walk daemons, detect dark Signal producers, return zero-or-one spec."""
    if now is None:
        now = time.time()
    daemons = discover_evolve_daemons(launchd_dir)
    if not daemons:
        # No Evolve daemons here (dev/CI host) — nothing to assess.
        return []
    silent = detect_silent_producers(daemons, now)
    spec = build_producer_silent_spec(silent)
    return [spec] if spec else []


def collect_audit_drain_silence(
    shared_dir: Path,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    now: float | None = None,
) -> list[dict]:
    """Read the audit-drain heartbeat, return zero-or-one ``audit_drain_silent``.

    Guarded to real pods (no Evolve daemons ⇒ dev/CI host ⇒ nothing to assess),
    matching ``collect_producer_silence``. The heartbeat itself is also absent on
    a host whose poller has never ticked, so an empty read is a clean no-op.
    """
    if now is None:
        now = time.time()
    if not discover_evolve_daemons(launchd_dir):
        return []
    recent = _read_audit_drain_heartbeat(shared_dir)
    stall = detect_audit_drain_stall(recent, now)
    spec = build_audit_drain_silent_spec(stall)
    return [spec] if spec else []


def run(
    shared_dir: Path,
    *,
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
    dry_run: bool = False,
    now: float | None = None,
) -> tuple[int, int]:
    """Collect → write signal → sweep-resolve when clear.

    Returns ``(n_fired, n_resolved)`` across the daemon-silence rollup
    (``monitor_silent``), the producer-liveness rollup (``producer_silent``), and
    the audit-drain liveness rollup (``audit_drain_silent``). All three are
    pod-scoped Signals under the same ``monitor_coverage`` producer with distinct
    signatures, so the single sweep_resolve below clears each once its condition
    lifts.
    """
    detections = collect(launchd_dir, now=now)
    detections += collect_producer_silence(launchd_dir, now=now)
    detections += collect_audit_drain_silence(shared_dir, launchd_dir, now=now)
    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:  # noqa: BLE001
            print(f"[monitor_coverage] observe failed: {exc}", flush=True)

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: all monitors back within expected cadence",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[monitor_coverage] sweep_resolve failed: {exc}", flush=True)

    return len(detections), n_resolved


def print_coverage_report(launchd_dir: Path) -> None:
    """Print which discovered daemons are watched vs skipped + why.

    Useful for operator triage: shows the full landscape so silent-by-
    design daemons are identifiable, and so the operator can decide
    which to add to ``WATCHED_DAEMONS`` after verifying log activity.
    """
    daemons = discover_evolve_daemons(launchd_dir)
    watched = [d for d in daemons if d.label in WATCHED_DAEMONS]
    skipped = [d for d in daemons if d.label not in WATCHED_DAEMONS]
    print(f"Discovered {len(daemons)} Evolve daemon(s) under {launchd_dir}.\n")
    print(f"WATCHED ({len(watched)}) — log-mtime silence check active:")
    for d in sorted(watched, key=lambda x: x.label):
        threshold = silence_threshold_for(d)
        thr = _human_duration(threshold) if threshold else "no cadence"
        print(f"  - {d.label}")
        print(f"      cadence: {d.expected_cadence_desc}, silence threshold: {thr}")
        print(f"      stdout:  {d.stdout_path or '(none)'}")
    print(f"\nSKIPPED ({len(skipped)}) — not in WATCHED_DAEMONS allowlist:")
    for d in sorted(skipped, key=lambda x: x.label):
        print(f"  - {d.label}  ({d.expected_cadence_desc})")
    print(
        "\nTo add a daemon to the allowlist: confirm its stdout log mtime "
        "advances on every wake (ssh to mini, watch the log for ~2× its "
        "cadence), then add the label to WATCHED_DAEMONS in "
        "packages/analyzer/monitor_coverage.py."
    )
    print(
        f"\nPRODUCER-LIVENESS ({len(SIGNAL_PRODUCER_MONITORS)}) — Signal "
        f"producers watched via stdout-summary mtime (producer_silent):"
    )
    for label, pm in sorted(SIGNAL_PRODUCER_MONITORS.items()):
        thr = _human_duration(producer_silence_threshold(pm))
        print(f"  - {label}  (cadence {_human_duration(pm.cadence_sec)}, "
              f"silence threshold {thr})")
        print(f"      starves: {pm.downstream}")
    if EXCLUDED_SIGNAL_PRODUCERS:
        print(f"\nPRODUCER-LIVENESS EXCLUDED ({len(EXCLUDED_SIGNAL_PRODUCERS)}):")
        for label, why in sorted(EXCLUDED_SIGNAL_PRODUCERS.items()):
            print(f"  - {label}\n      {why}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="monitor_coverage — fire a Signal when an Evolve monitor goes silent",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--launchd-dir", default=str(DEFAULT_LAUNCHD_DIR),
        help=f"Directory of plists to scan (default {DEFAULT_LAUNCHD_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    parser.add_argument(
        "--list-coverage", action="store_true",
        help="Print which discovered daemons are watched vs skipped + why; exit",
    )
    args = parser.parse_args()
    if args.list_coverage:
        print_coverage_report(Path(args.launchd_dir))
        return
    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    n_fired, n_resolved = run(
        shared_dir,
        launchd_dir=Path(args.launchd_dir),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"[monitor_coverage] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[monitor_coverage] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
