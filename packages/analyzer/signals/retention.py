"""signals.retention — pod-wide archive/log pruning.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 6).

Retention rules:

  - active signal states (firing | snoozed): kept indefinitely until terminal
  - signals/archived/ (resolved | dismissed): keep 90 days
  - signals/log/<YYYY-MM-DD>.jsonl: keep 1 year, rolling
  - watchdog/<YYYY-MM-DD>.jsonl: keep 1 year
  - proposals/archived/<id>.json: keep 90 days
  - alerts/{dispatcher,dispatcher-suppressed,delivery-failures}/<YYYY-MM-DD>.jsonl:
    keep 30 days (added 2026-06-01 — the dispatcher-suppressed stream
    was previously unrotated and filled the pod's disk at 14 MB/day
    under a stuck producer)
  - incidents/<YYYY-MM-DD>/: keep 30 days (heal.py daily incident
    directories; longest active reader is gateway_diagnostician at 7d)
  - audit_outbox/_ingested/<YYYY-MM-DD>/ (per-bot) and
    infra_audit_outbox/_ingested/<YYYY-MM-DD>/ (pod-wide): keep 30 days
    (drained forensic audit-record archives; ~500/day pod-wide, nothing
    reads them on the hot path — owned by signals.audit_outbox_retention,
    called from prune_retention() so it shares this daily cron)
  - annotations/<bot>/<YYYY-MM-DD>.jsonl: keep 90 days (per-turn
    annotation records minted by the TS gateway; the cost/metrics
    readers only ever look at a trailing ≤7-day window, so anything
    older is never read again — admin-side prune of evolve-owned files)
  - candidates/synthesis_log/<YYYY-MM-DD>.jsonl + candidates/dropped/
    <YYYY-MM-DD>.jsonl: keep 30 days (synthesizer decision log has no
    production reader; the dropped-log reader clamps its lookback to ≤30
    days, so older records are physically unreadable)
  - cascade/labels/<YYYY-MM-DD>.jsonl: keep 90 days (tier-cascade
    labeled outcomes; write_labels dedups-on-write per session, the
    cron bounds the daily-file history for the future tuner)

(Historical note: the spec's ``signature_index.json`` was never
implemented — signature lookup is a directory scan in signals.store,
serialized by the store lock since 7.1 Phase A. Nothing to prune.)

Retention deliberately does NOT take the store lock
(``signals/.store.lock``): mtime-based pruning of 90-day-old archived
files cannot overlap the 1-hour reopen window observe() cares about,
and the worst interleaving — resurrecting a being-pruned dismissed
bump — is benign and self-heals on the next pass. See
internal/spec-state-store-and-deploy-resilience-2026-06-10.md §1.4.
The lock files themselves (``signals/.store.lock``,
``proposals/.store.lock``) are dotfiles, invisible to the ``*.json``
globs here — never prune them, and never repair them by
replace/rename (chmod/chown in place only), or mutual exclusion
silently splits across two inodes.

This module is also the home for the cross-store retention sweep —
watchdog JSONLs (1 year), archived proposals (90 days), alert
dispatcher logs (30 days), and heal.py incident day-dirs (30 days).
They share the same daily cron via ``run_retention.py``.

This module provides ``prune_retention()`` for cron use plus a CLI:

    python3 -m signals.retention --shared-dir /Users/Shared/evolve

Idempotent. Safe to run multiple times per day.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from signals import audit_outbox_retention
from signals import store as signals_store


DEFAULT_ARCHIVED_RETENTION_DAYS = 90
DEFAULT_LOG_RETENTION_DAYS = 365
DEFAULT_WATCHDOG_RETENTION_DAYS = 365
DEFAULT_PROPOSALS_RETENTION_DAYS = 90
# Alert dispatcher logs (dispatcher/, dispatcher-suppressed/,
# delivery-failures/) — noisier than signals/log (one record per dispatch
# vs one per state transition), shorter horizon is appropriate.
# Surfacing: the UI's Reports → Subscriptions → Messages list reads these,
# but only "recent" (~last 100 records). 30 days covers operator triage
# without paying disk for a year of low-value chat-send history.
DEFAULT_ALERTS_RETENTION_DAYS = 30
# Longest active reader is gateway_diagnostician at 7 days; 30 days gives
# a comfortable buffer plus a recent-history view for the operator.
DEFAULT_INCIDENTS_RETENTION_DAYS = 30
# delivery_monitor/ledger/<YYYY-MM-DD>.jsonl — one row per classified
# delivery window (spec-proactive-delivery-monitor-2026-06-10.md §6.5).
# U0's "proactive deliveries per week" metric reads this; 90 days covers
# the longest planned consumer (the delivery_reliability generator's
# chronic-pattern lookback) with room.
DEFAULT_DELIVERY_LEDGER_RETENTION_DAYS = 90
# breakers/runner-log/<YYYY-MM-DD>.jsonl — the activity-shape detector's
# per-cycle "what would have tripped" log. Post-F-5-A the runner source-cuts
# the steady-state no-op (only actionable/changed decisions are written), but
# the surface still needs a retention floor: nothing reads it after the fact
# (calibration's backtest.py reads the turns corpus, not this log) and it was
# previously unbounded (15 MB / 17 files observed on the mini, oldest
# 2026-06-12, never pruned). 14 days comfortably covers a calibration-soak
# review window.
DEFAULT_RUNNER_LOG_RETENTION_DAYS = 14
# audit_outbox/_ingested/<date>/ — drained forensic audit-record archives,
# per-bot + pod-wide infra. High write rate (~500/day pod-wide), nothing
# reads it on the hot path, grows unbounded. 30 days matches the
# alerts/incidents tier (closest analog). Owned by
# signals.audit_outbox_retention; called from prune_retention() so it
# rides the same daily cron.
DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS = (
    audit_outbox_retention.DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS
)

# archived-bots/<bot>-<YYYY-MM-DD>[-<n>]/ — graceful-retirement archives
# written by evolve_admin.retire. Each is a closure record + a copy of the
# bot's .openclaw tree (node_modules excluded since 2026-06-28, so ~10-30 MB
# rather than the historical ~350 MB). Before this category there was NO
# prune path: the dir grew by one archive per retirement, forever (1.4 GB of
# 4 archives observed on the mini, 2026-06-28). Forensic value of a retired
# bot's working tree decays; 180 days covers a full revive-or-forget cycle
# with comfortable margin. Operators who want longer can pass a larger
# --archived-bots-days (or a very large value to effectively disable).
DEFAULT_ARCHIVED_BOTS_RETENTION_DAYS = 180

# annotations/<bot>/<YYYY-MM-DD>.jsonl — per-turn annotation records minted by
# the TS gateway's TurnObserver. The cost/metrics layer reads only a trailing
# window (cost_ledger at 7 days; measure.load_annotations per-day; the
# exec_outcome_watchdog), so anything beyond ~a week is never read again, yet the
# files accumulated unbounded (19 MB pod-wide, oldest 2026-05-29, observed
# 2026-06-28). 90 days is well past the longest lookback with comfortable margin.
# The files are evolve-owned (gateway writes via the workspace ACL), so the
# admin-side cron prunes them directly; the TS WRITER is unchanged.
DEFAULT_ANNOTATIONS_RETENTION_DAYS = 90

# candidates/synthesis_log/<date>.jsonl + candidates/dropped/<date>.jsonl —
# the proposal-synthesizer's per-run decision log and the gate's drop log.
# synthesis_log has no production reader (only tests reference the path helper);
# the dropped-log reader (api_candidates_dropped) hard-clamps its lookback to
# days≤30, so records older than 30 days are physically unreadable. 30 days
# matches that ceiling and keeps both bounded instead of growing forever.
DEFAULT_CANDIDATES_RETENTION_DAYS = 30

# cascade/labels/<date>.jsonl — tier-cascade labeled outcomes. write_labels now
# dedups-on-write (one row per session/day), but the daily files still
# accumulate forever; the only reader counts lines for today/yesterday and the
# dedup-tuner that would consume the history does not exist yet. 90 days leaves
# a generous backlog for that future tuner without unbounded growth.
DEFAULT_CASCADE_LABELS_RETENTION_DAYS = 90

# Trailing ``-YYYY-MM-DD`` (with optional ``-<n>`` same-day collision suffix)
# of an archived-bot directory name, e.g. ``nova-2026-06-11`` or
# ``nova-2026-06-11-2``. The date group is the retirement date we age on.
# Anchored at end so a bot id that itself contains digits can't false-match.
_ARCHIVED_BOTS_DIR_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})(?:-\d+)?$")

# Date-partitioned subdirs that hold the dispatcher's append-only log
# streams. Each entry sweeps ``alerts/<name>/<YYYY-MM-DD>.jsonl``.
_ALERTS_LOG_STREAMS: tuple[str, ...] = (
    "dispatcher",
    "dispatcher-suppressed",
    "delivery-failures",
    # Permanently-undeliverable messages (dispatcher._record_dead_letter).
    # Same 30-day window as the other dispatcher streams.
    "dead-letter",
)


def _remove_archived_bot_dir(target: Path, archive_root: Path) -> bool:
    """Delete one archived-bot directory, returning True iff it is gone after.

    Two guards keep this from ever deleting the wrong thing:

    * **Containment** — ``target`` must resolve to a *direct child* of
      ``archive_root`` (the pod's ``archived-bots/`` dir). We never rmtree
      anything outside it; the caller has already matched the date regex.
    * **Privilege fallback** — archives written by the admin-UI API path are
      owned by ``evolve`` and a plain ``shutil.rmtree`` removes them. Archives
      written by the ``sudo evolve-admin retire`` CLI path are root-owned, so
      rmtree hits EACCES; we then fall back to the existing
      ``sudo /bin/rm -rf .../archived-bots/*`` sudoers grant. That grant's
      wildcard matches the directory name but not a deeper path — exactly the
      granularity we delete at.
    """
    try:
        resolved = target.resolve()
        if resolved.parent != archive_root.resolve():
            return False
    except OSError:
        return False
    if not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
        return not target.exists()
    except OSError:
        # Likely a root-owned tree (CLI retire path) — evolve can't unlink
        # it directly, so escalate to the sudoers `rm -rf` grant rather than
        # swallowing the error.
        return _sudo_rm_archived_bot_dir(target)


def _sudo_rm_archived_bot_dir(target: Path) -> bool:
    """Escalate deletion of a root-owned archive dir to the
    ``sudo /bin/rm -rf .../archived-bots/*`` sudoers grant. Containment is
    already enforced by the caller (``_remove_archived_bot_dir``)."""
    try:
        proc = subprocess.run(
            ["sudo", "/bin/rm", "-rf", str(target)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and not target.exists()


@dataclass
class PruneResult:
    archived_pruned: int
    log_files_pruned: int
    archived_kept: int
    log_files_kept: int
    watchdog_pruned: int = 0
    watchdog_kept: int = 0
    proposals_pruned: int = 0
    proposals_kept: int = 0
    alerts_pruned: int = 0
    alerts_kept: int = 0
    incidents_dirs_pruned: int = 0
    incidents_dirs_kept: int = 0
    delivery_ledger_pruned: int = 0
    delivery_ledger_kept: int = 0
    runner_log_pruned: int = 0
    runner_log_kept: int = 0
    audit_outbox_bot_dirs_pruned: int = 0
    audit_outbox_bot_dirs_kept: int = 0
    audit_outbox_infra_dirs_pruned: int = 0
    audit_outbox_infra_dirs_kept: int = 0
    archived_bots_pruned: int = 0
    archived_bots_kept: int = 0
    annotations_pruned: int = 0
    annotations_kept: int = 0
    candidates_pruned: int = 0
    candidates_kept: int = 0
    cascade_labels_pruned: int = 0
    cascade_labels_kept: int = 0


def prune_retention(
    shared_dir: Path,
    *,
    archived_days: int = DEFAULT_ARCHIVED_RETENTION_DAYS,
    log_days: int = DEFAULT_LOG_RETENTION_DAYS,
    watchdog_days: int = DEFAULT_WATCHDOG_RETENTION_DAYS,
    proposals_days: int = DEFAULT_PROPOSALS_RETENTION_DAYS,
    alerts_days: int = DEFAULT_ALERTS_RETENTION_DAYS,
    incidents_days: int = DEFAULT_INCIDENTS_RETENTION_DAYS,
    delivery_ledger_days: int = DEFAULT_DELIVERY_LEDGER_RETENTION_DAYS,
    runner_log_days: int = DEFAULT_RUNNER_LOG_RETENTION_DAYS,
    audit_outbox_days: int = DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS,
    archived_bots_days: int = DEFAULT_ARCHIVED_BOTS_RETENTION_DAYS,
    annotations_days: int = DEFAULT_ANNOTATIONS_RETENTION_DAYS,
    candidates_days: int = DEFAULT_CANDIDATES_RETENTION_DAYS,
    cascade_labels_days: int = DEFAULT_CASCADE_LABELS_RETENTION_DAYS,
    now: datetime | None = None,
) -> PruneResult:
    """Apply retention rules to the Signal store, watchdog logs, proposals,
    alert dispatcher logs, and heal.py incidents.

    Pruning targets:

    1. ``signals/archived/<id>.json`` — files older than ``archived_days``
       (by file mtime; proxy for resolved_at since archived files are not
       rewritten after archival).
    2. ``signals/log/<YYYY-MM-DD>.jsonl`` — files whose date is older than
       ``log_days`` from ``now``.
    3. ``watchdog/<YYYY-MM-DD>.jsonl`` — daily watchdog event logs older
       than ``watchdog_days`` (matched by filename date, same as signals
       log pruning).
    4. ``proposals/archived/<id>.json`` — terminal proposals older than
       ``proposals_days`` (by file mtime, same as signals archived pruning).
    5. ``alerts/<stream>/<YYYY-MM-DD>.jsonl`` — date-partitioned
       dispatcher log streams (``dispatcher``, ``dispatcher-suppressed``,
       ``delivery-failures``) older than ``alerts_days``. The
       dispatcher-suppressed stream is the one that fills the disk
       (~36k entries/day under a stuck producer); the other two grow
       slower but get the same sweep for symmetry.
    6. ``incidents/<YYYY-MM-DD>/`` — heal.py daily incident directories
       older than ``incidents_days`` (matched by directory-name date).
       The whole day-dir is removed via ``shutil.rmtree``.
    7. ``delivery_monitor/ledger/<YYYY-MM-DD>.jsonl`` — per-window
       delivery outcomes older than ``delivery_ledger_days`` (matched by
       filename date). Spec-proactive-delivery-monitor-2026-06-10.md §6.5.
    7a. ``breakers/runner-log/<YYYY-MM-DD>.jsonl`` — the activity-shape
       detector's per-cycle decision log, older than ``runner_log_days``
       (matched by filename date). Footprint F-5-B retention floor; the
       ``.last-decision.json`` sidecar is a dotfile and never matched.
    8. ``audit_outbox/_ingested/<YYYY-MM-DD>/`` (per-bot) and
       ``infra_audit_outbox/_ingested/<YYYY-MM-DD>/`` (pod-wide) — drained
       audit-record archives older than ``audit_outbox_days``. Delegated to
       ``signals.audit_outbox_retention.prune_audit_outbox`` (enumerates
       bots from network.json, resolves homes via ``evolve_config`` —
       platform-portable). The LIVE outbox roots (un-drained records) are
       never touched.
    9. ``archived-bots/<bot>-<YYYY-MM-DD>[-<n>]/`` — graceful-retirement
       archives older than ``archived_bots_days`` (matched by the date in
       the directory name). The whole archive dir is removed; root-owned
       archives (CLI retire path) fall back to the ``sudo /bin/rm -rf``
       grant. Non-conforming names are kept, never deleted.
    10. ``annotations/<bot>/<YYYY-MM-DD>.jsonl`` — per-turn annotation
       JSONL (TS gateway) older than ``annotations_days`` (matched by
       filename date, per-bot subdir). Cost/metrics readers only use a
       trailing window, so older records are dead weight.
    11. ``candidates/{synthesis_log,dropped}/<YYYY-MM-DD>.jsonl`` —
       synthesizer decision log + gate drop log older than
       ``candidates_days`` (matched by filename date). synthesis_log has
       no production reader; the dropped-log reader clamps to days≤30.
    12. ``cascade/labels/<YYYY-MM-DD>.jsonl`` — tier-cascade labeled
       outcomes older than ``cascade_labels_days`` (matched by filename
       date). write_labels dedups-on-write; this bounds the history.

    Active signal/proposal states are never pruned.
    """
    cutoff_now = now or datetime.now(timezone.utc)
    archived_cutoff = (cutoff_now - timedelta(days=archived_days)).timestamp()
    log_cutoff_date: date = (cutoff_now - timedelta(days=log_days)).date()
    watchdog_cutoff_date: date = (cutoff_now - timedelta(days=watchdog_days)).date()
    proposals_cutoff = (cutoff_now - timedelta(days=proposals_days)).timestamp()
    alerts_cutoff_date: date = (cutoff_now - timedelta(days=alerts_days)).date()
    incidents_cutoff_date: date = (cutoff_now - timedelta(days=incidents_days)).date()
    delivery_cutoff_date: date = (
        cutoff_now - timedelta(days=delivery_ledger_days)
    ).date()
    runner_log_cutoff_date: date = (
        cutoff_now - timedelta(days=runner_log_days)
    ).date()
    archived_bots_cutoff_date: date = (
        cutoff_now - timedelta(days=archived_bots_days)
    ).date()
    annotations_cutoff_date: date = (
        cutoff_now - timedelta(days=annotations_days)
    ).date()
    candidates_cutoff_date: date = (
        cutoff_now - timedelta(days=candidates_days)
    ).date()
    cascade_labels_cutoff_date: date = (
        cutoff_now - timedelta(days=cascade_labels_days)
    ).date()

    # ── 1. signals/archived/ ─────────────────────────────────────────────────
    archived_pruned = 0
    archived_kept = 0
    archived_dir = signals_store.signals_root(shared_dir) / "archived"  # store-access-lint: store-internal retention (mtime-prune + unlink; iter_* can't express)
    if archived_dir.exists():
        for path in archived_dir.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < archived_cutoff:
                try:
                    path.unlink()
                    archived_pruned += 1
                except OSError:
                    archived_kept += 1
            else:
                archived_kept += 1

    # ── 2. signals/log/ ──────────────────────────────────────────────────────
    log_files_pruned = 0
    log_files_kept = 0
    log_dir = signals_store.signals_root(shared_dir) / "log"
    if log_dir.exists():
        for path in log_dir.glob("*.jsonl"):
            stem = path.stem
            try:
                file_date = date.fromisoformat(stem)
            except ValueError:
                # Non-conforming filename — keep, don't risk deleting.
                log_files_kept += 1
                continue
            if file_date < log_cutoff_date:
                try:
                    path.unlink()
                    log_files_pruned += 1
                except OSError:
                    log_files_kept += 1
            else:
                log_files_kept += 1

    # ── 3. watchdog/<YYYY-MM-DD>.jsonl ───────────────────────────────────────
    watchdog_pruned = 0
    watchdog_kept = 0
    watchdog_dir = Path(shared_dir) / "watchdog"
    if watchdog_dir.exists():
        for path in watchdog_dir.glob("*.jsonl"):
            stem = path.stem
            try:
                file_date = date.fromisoformat(stem)
            except ValueError:
                watchdog_kept += 1
                continue
            if file_date < watchdog_cutoff_date:
                try:
                    path.unlink()
                    watchdog_pruned += 1
                except OSError:
                    watchdog_kept += 1
            else:
                watchdog_kept += 1

    # ── 4. proposals/archived/ ───────────────────────────────────────────────
    proposals_pruned = 0
    proposals_kept = 0
    proposals_archived_dir = Path(shared_dir) / "proposals" / "archived"  # store-access-lint: store-internal retention (mtime-prune + unlink; iter_* can't express)
    if proposals_archived_dir.exists():
        for path in proposals_archived_dir.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < proposals_cutoff:
                try:
                    path.unlink()
                    proposals_pruned += 1
                except OSError:
                    proposals_kept += 1
            else:
                proposals_kept += 1

    # ── 5. alerts/<stream>/<YYYY-MM-DD>.jsonl ────────────────────────────────
    alerts_pruned = 0
    alerts_kept = 0
    alerts_root = Path(shared_dir) / "alerts"
    for stream in _ALERTS_LOG_STREAMS:
        stream_dir = alerts_root / stream
        if not stream_dir.is_dir():
            continue
        for path in stream_dir.glob("*.jsonl"):
            stem = path.stem
            try:
                file_date = date.fromisoformat(stem)
            except ValueError:
                # Non-conforming filename — keep, don't risk deleting.
                alerts_kept += 1
                continue
            if file_date < alerts_cutoff_date:
                try:
                    path.unlink()
                    alerts_pruned += 1
                except OSError:
                    alerts_kept += 1
            else:
                alerts_kept += 1

    # ── 6. incidents/<YYYY-MM-DD>/ ───────────────────────────────────────────
    incidents_dirs_pruned = 0
    incidents_dirs_kept = 0
    incidents_dir = Path(shared_dir) / "incidents"
    if incidents_dir.exists():
        for day_dir in incidents_dir.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                dir_date = date.fromisoformat(day_dir.name)
            except ValueError:
                # Non-conforming directory name — keep, don't risk deleting.
                incidents_dirs_kept += 1
                continue
            if dir_date < incidents_cutoff_date:
                try:
                    shutil.rmtree(day_dir)
                    incidents_dirs_pruned += 1
                except OSError:
                    incidents_dirs_kept += 1
            else:
                incidents_dirs_kept += 1

    # ── 7. delivery_monitor/ledger/<YYYY-MM-DD>.jsonl ────────────────────────
    delivery_ledger_pruned = 0
    delivery_ledger_kept = 0
    delivery_ledger_dir = Path(shared_dir) / "delivery_monitor" / "ledger"
    if delivery_ledger_dir.is_dir():
        for path in delivery_ledger_dir.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                # Non-conforming filename — keep, don't risk deleting.
                delivery_ledger_kept += 1
                continue
            if file_date < delivery_cutoff_date:
                try:
                    path.unlink()
                    delivery_ledger_pruned += 1
                except OSError:
                    delivery_ledger_kept += 1
            else:
                delivery_ledger_kept += 1

    # ── 7a. breakers/runner-log/<YYYY-MM-DD>.jsonl ───────────────────────────
    runner_log_pruned = 0
    runner_log_kept = 0
    runner_log_dir = Path(shared_dir) / "breakers" / "runner-log"
    if runner_log_dir.is_dir():
        # Only the date-stamped JSONL files age out; the .last-decision.json
        # change-detection sidecar is a dotfile and not matched by this glob.
        for path in runner_log_dir.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                # Non-conforming filename — keep, don't risk deleting.
                runner_log_kept += 1
                continue
            if file_date < runner_log_cutoff_date:
                try:
                    path.unlink()
                    runner_log_pruned += 1
                except OSError:
                    runner_log_kept += 1
            else:
                runner_log_kept += 1

    # ── 8. audit_outbox/_ingested/ (per-bot + pod-wide infra) ────────────────
    # Delegated — that module owns bot enumeration + platform-portable home
    # resolution and only ever touches _ingested/ (never the live outbox).
    audit_outbox_result = audit_outbox_retention.prune_audit_outbox(
        shared_dir,
        days=audit_outbox_days,
        now=cutoff_now,
    )

    # ── 9. archived-bots/<bot>-<YYYY-MM-DD>[-<n>]/ ───────────────────────────
    archived_bots_pruned = 0
    archived_bots_kept = 0
    archived_bots_dir = Path(shared_dir) / "archived-bots"
    if archived_bots_dir.is_dir():
        for entry in archived_bots_dir.iterdir():
            if not entry.is_dir():
                continue
            m = _ARCHIVED_BOTS_DIR_DATE_RE.search(entry.name)
            if m is None:
                # No parseable retirement date — keep, never risk deleting.
                archived_bots_kept += 1
                continue
            try:
                dir_date = date.fromisoformat(m.group(1))
            except ValueError:
                archived_bots_kept += 1
                continue
            if dir_date < archived_bots_cutoff_date:
                if _remove_archived_bot_dir(entry, archived_bots_dir):
                    archived_bots_pruned += 1
                else:
                    archived_bots_kept += 1
            else:
                archived_bots_kept += 1

    # ── 10. annotations/<bot>/<YYYY-MM-DD>.jsonl ─────────────────────────────
    # Per-turn annotation records (TS gateway). One subdir per bot; each file is
    # date-named, so we age on the filename like the other dated JSONL streams.
    annotations_pruned = 0
    annotations_kept = 0
    annotations_dir = Path(shared_dir) / "annotations"
    if annotations_dir.is_dir():
        for bot_dir in annotations_dir.iterdir():
            if not bot_dir.is_dir():
                continue
            for path in bot_dir.glob("*.jsonl"):
                try:
                    file_date = date.fromisoformat(path.stem)
                except ValueError:
                    # Non-conforming filename — keep, don't risk deleting.
                    annotations_kept += 1
                    continue
                if file_date < annotations_cutoff_date:
                    try:
                        path.unlink()
                        annotations_pruned += 1
                    except OSError:
                        annotations_kept += 1
                else:
                    annotations_kept += 1

    # ── 11. candidates/{synthesis_log,dropped}/<YYYY-MM-DD>.jsonl ─────────────
    # Synthesizer decision log (no prod reader) + gate drop log (reader clamps
    # lookback to days≤30). Both date-named; age on the filename.
    candidates_pruned = 0
    candidates_kept = 0
    candidates_root = Path(shared_dir) / "candidates"
    for sub in ("synthesis_log", "dropped"):
        sub_dir = candidates_root / sub
        if not sub_dir.is_dir():
            continue
        for path in sub_dir.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                # Non-conforming filename — keep, don't risk deleting.
                candidates_kept += 1
                continue
            if file_date < candidates_cutoff_date:
                try:
                    path.unlink()
                    candidates_pruned += 1
                except OSError:
                    candidates_kept += 1
            else:
                candidates_kept += 1

    # ── 12. cascade/labels/<YYYY-MM-DD>.jsonl ────────────────────────────────
    # Tier-cascade labeled outcomes; date-named, age on the filename.
    cascade_labels_pruned = 0
    cascade_labels_kept = 0
    cascade_labels_dir = Path(shared_dir) / "cascade" / "labels"
    if cascade_labels_dir.is_dir():
        for path in cascade_labels_dir.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                # Non-conforming filename — keep, don't risk deleting.
                cascade_labels_kept += 1
                continue
            if file_date < cascade_labels_cutoff_date:
                try:
                    path.unlink()
                    cascade_labels_pruned += 1
                except OSError:
                    cascade_labels_kept += 1
            else:
                cascade_labels_kept += 1

    return PruneResult(
        archived_pruned=archived_pruned,
        log_files_pruned=log_files_pruned,
        archived_kept=archived_kept,
        log_files_kept=log_files_kept,
        watchdog_pruned=watchdog_pruned,
        watchdog_kept=watchdog_kept,
        proposals_pruned=proposals_pruned,
        proposals_kept=proposals_kept,
        alerts_pruned=alerts_pruned,
        alerts_kept=alerts_kept,
        incidents_dirs_pruned=incidents_dirs_pruned,
        incidents_dirs_kept=incidents_dirs_kept,
        delivery_ledger_pruned=delivery_ledger_pruned,
        delivery_ledger_kept=delivery_ledger_kept,
        runner_log_pruned=runner_log_pruned,
        runner_log_kept=runner_log_kept,
        audit_outbox_bot_dirs_pruned=audit_outbox_result.bot_dirs_pruned,
        audit_outbox_bot_dirs_kept=audit_outbox_result.bot_dirs_kept,
        audit_outbox_infra_dirs_pruned=audit_outbox_result.infra_dirs_pruned,
        audit_outbox_infra_dirs_kept=audit_outbox_result.infra_dirs_kept,
        archived_bots_pruned=archived_bots_pruned,
        archived_bots_kept=archived_bots_kept,
        annotations_pruned=annotations_pruned,
        annotations_kept=annotations_kept,
        candidates_pruned=candidates_pruned,
        candidates_kept=candidates_kept,
        cascade_labels_pruned=cascade_labels_pruned,
        cascade_labels_kept=cascade_labels_kept,
    )


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prune resolved/dismissed Signals, watchdog logs, archived "
            "proposals, alert dispatcher logs, and old heal incident dirs."
        )
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
    )
    parser.add_argument(
        "--archived-days",
        type=int,
        default=DEFAULT_ARCHIVED_RETENTION_DAYS,
        help="Days to retain archived Signal files (default: 90)",
    )
    parser.add_argument(
        "--log-days",
        type=int,
        default=DEFAULT_LOG_RETENTION_DAYS,
        help="Days to retain Signal state-change log files (default: 365)",
    )
    parser.add_argument(
        "--watchdog-days",
        type=int,
        default=DEFAULT_WATCHDOG_RETENTION_DAYS,
        help="Days to retain watchdog JSONL files (default: 365)",
    )
    parser.add_argument(
        "--proposals-days",
        type=int,
        default=DEFAULT_PROPOSALS_RETENTION_DAYS,
        help="Days to retain archived proposal files (default: 90)",
    )
    parser.add_argument(
        "--alerts-days",
        type=int,
        default=DEFAULT_ALERTS_RETENTION_DAYS,
        help="Days to retain alert dispatcher log files (default: 30)",
    )
    parser.add_argument(
        "--incidents-days",
        type=int,
        default=DEFAULT_INCIDENTS_RETENTION_DAYS,
        help="Days to retain heal.py incident day-dirs (default: 30)",
    )
    parser.add_argument(
        "--runner-log-days",
        type=int,
        default=DEFAULT_RUNNER_LOG_RETENTION_DAYS,
        help="Days to retain breakers/runner-log JSONL files (default: 14)",
    )
    parser.add_argument(
        "--audit-outbox-days",
        type=int,
        default=DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS,
        help="Days to retain audit_outbox/_ingested/ day-dirs (default: 30)",
    )
    parser.add_argument(
        "--archived-bots-days",
        type=int,
        default=DEFAULT_ARCHIVED_BOTS_RETENTION_DAYS,
        help="Days to retain archived-bots/ retirement archives (default: 180)",
    )
    parser.add_argument(
        "--annotations-days",
        type=int,
        default=DEFAULT_ANNOTATIONS_RETENTION_DAYS,
        help="Days to retain annotations/<bot>/ turn-annotation JSONL (default: 90)",
    )
    parser.add_argument(
        "--candidates-days",
        type=int,
        default=DEFAULT_CANDIDATES_RETENTION_DAYS,
        help="Days to retain candidates/{synthesis_log,dropped}/ JSONL (default: 30)",
    )
    parser.add_argument(
        "--cascade-labels-days",
        type=int,
        default=DEFAULT_CASCADE_LABELS_RETENTION_DAYS,
        help="Days to retain cascade/labels/ JSONL (default: 90)",
    )
    args = parser.parse_args(argv)

    result = prune_retention(
        args.shared_dir,
        archived_days=args.archived_days,
        log_days=args.log_days,
        watchdog_days=args.watchdog_days,
        proposals_days=args.proposals_days,
        alerts_days=args.alerts_days,
        incidents_days=args.incidents_days,
        runner_log_days=args.runner_log_days,
        audit_outbox_days=args.audit_outbox_days,
        archived_bots_days=args.archived_bots_days,
        annotations_days=args.annotations_days,
        candidates_days=args.candidates_days,
        cascade_labels_days=args.cascade_labels_days,
    )
    audit_pruned = (
        result.audit_outbox_bot_dirs_pruned
        + result.audit_outbox_infra_dirs_pruned
    )
    audit_kept = (
        result.audit_outbox_bot_dirs_kept
        + result.audit_outbox_infra_dirs_kept
    )
    print(
        f"signals/archived pruned={result.archived_pruned} kept={result.archived_kept}"
        f" · signals/log pruned={result.log_files_pruned} kept={result.log_files_kept}"
        f" · watchdog pruned={result.watchdog_pruned} kept={result.watchdog_kept}"
        f" · proposals/archived pruned={result.proposals_pruned} kept={result.proposals_kept}"
        f" · alerts pruned={result.alerts_pruned} kept={result.alerts_kept}"
        f" · incidents pruned={result.incidents_dirs_pruned} kept={result.incidents_dirs_kept}"
        f" · breakers/runner-log pruned={result.runner_log_pruned} kept={result.runner_log_kept}"
        f" · audit_outbox/_ingested pruned={audit_pruned} kept={audit_kept}"
        f" · archived-bots pruned={result.archived_bots_pruned} kept={result.archived_bots_kept}"
        f" · annotations pruned={result.annotations_pruned} kept={result.annotations_kept}"
        f" · candidates pruned={result.candidates_pruned} kept={result.candidates_kept}"
        f" · cascade_labels pruned={result.cascade_labels_pruned} kept={result.cascade_labels_kept}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
