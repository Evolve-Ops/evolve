"""signals.audit_outbox_retention — prune aged drained audit-outbox archives.

Spec: docs/spec-apps-meta-2026-06-13.md (audit pipeline) · companion to
``signals.retention`` (the canonical pod-wide pruner whose idiom this
mirrors: cutoff computation, date-partitioned pruning, idempotency,
``PruneResult`` counters, ``--shared-dir`` CLI, injectable ``now=``).

What this prunes
----------------

The audit pipeline drains each bot's audit outbox and the pod-wide infra
outbox by *moving* every processed record into a date-partitioned
``_ingested/<YYYY-MM-DD>/`` archive (``audit_poller._archive_file`` /
``_archive_infra_file``). That archive is forensic-only — nothing reads it
on the hot path — and it grows unbounded (live mini counts on 2026-06-27:
one bot at 21k records, ~500 records/day pod-wide). Nothing else cleans it
up, so it is the audit pipeline's analog of the high-volume drained
streams ``signals.retention`` already rotates.

Two targets, both pruned the same way (whole aged date-dir via
``shutil.rmtree`` — already partitioned by ingest date, so the date stem
is an exact retention key and the delete is atomic per day):

  1. Per-bot:   ``{bot_home}/.openclaw/workspace/evolve/audit_outbox/_ingested/<YYYY-MM-DD>/``
  2. Pod-wide:  ``{shared_dir}/infra_audit_outbox/_ingested/<YYYY-MM-DD>/``

**Only ``_ingested/`` is in scope.** The live outbox roots
(``audit_outbox/`` and ``infra_audit_outbox/`` minus ``_ingested/``) hold
un-drained records the poller has not yet processed — they are NEVER
touched here.

Retention window
----------------

Default **30 days** (``--days N`` overrides). 30d matches the
alerts/incidents tier in ``signals.retention`` — the closest analog to
this high-volume drained forensic archive. A date-dir whose stem is
strictly older than ``now - days`` is removed.

Permissions
-----------

Runs as the ``evolve`` user. ``evolve`` holds a write ACL on each bot's
``workspace/evolve/`` subtree (``deploy.set_evolve_read_acl`` grants
read+write there) and owns ``{shared_dir}`` — so ``rmtree`` under
``_ingested/`` needs no sudo for either target. This module deliberately
shells out to nothing and never sudoes.

Idempotent. Safe to run any frequency. Wired into the same daily
retention job as ``signals.retention`` (see ``run_retention.py`` /
``deploy._install_launchd_retention``); ``prune_retention`` calls
``prune_audit_outbox`` so a single daily pass drains it.

Manual invocation::

    python3 -m signals.audit_outbox_retention --shared-dir /Users/Shared/evolve --days 30
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import evolve_config

logger = logging.getLogger(__name__)


DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS = 30

# Relative path from a bot home to its drained audit-outbox archive.
_BOT_INGESTED_RELPATH = (
    ".openclaw/workspace/evolve/audit_outbox/_ingested"
)
# Pod-wide infra outbox archive, relative to {shared_dir}.
_INFRA_INGESTED_RELNAME = "infra_audit_outbox"


@dataclass
class AuditOutboxPruneResult:
    """Counts of aged ``_ingested/<date>/`` day-dirs pruned vs kept.

    ``*_dirs_pruned`` counts removed date-dirs; ``*_dirs_kept`` counts
    date-dirs left in place (within the window, or skipped because the
    name is not a date, or a delete that failed). ``bots_scanned`` is how
    many bot homes were inspected (one entry per bot in network.json,
    regardless of whether it had an ``_ingested/`` dir).
    """

    bot_dirs_pruned: int = 0
    bot_dirs_kept: int = 0
    infra_dirs_pruned: int = 0
    infra_dirs_kept: int = 0
    bots_scanned: int = 0


def _prune_ingested_root(
    ingested_root: Path,
    *,
    cutoff_date: date,
    label: str,
) -> tuple[int, int]:
    """Prune ``_ingested/<YYYY-MM-DD>/`` day-dirs under ``ingested_root``.

    A day-dir whose name parses to a date strictly older than
    ``cutoff_date`` is removed via ``shutil.rmtree``. Non-date names are
    skipped (logged, never deleted). Returns ``(pruned, kept)``.

    Does NOT touch ``ingested_root``'s parent — the live outbox root holds
    un-drained records and is out of scope.
    """
    pruned = 0
    kept = 0
    if not ingested_root.is_dir():
        return pruned, kept
    for day_dir in ingested_root.iterdir():
        if not day_dir.is_dir():
            # Stray file (e.g. a dotfile or a half-moved record) — leave it.
            kept += 1
            continue
        try:
            dir_date = date.fromisoformat(day_dir.name)
        except ValueError:
            # Non-date directory name — keep, don't risk deleting unknown
            # data. Explicit handling (not a bare except) so the
            # silent-exception ratchet stays satisfied.
            logger.debug(
                "audit_outbox_retention: skipping non-date dir %s under %s",
                day_dir.name,
                label,
            )
            kept += 1
            continue
        if dir_date < cutoff_date:
            try:
                shutil.rmtree(day_dir)
                pruned += 1
            except OSError as exc:
                logger.warning(
                    "audit_outbox_retention: rmtree failed for %s: %s",
                    day_dir,
                    exc,
                )
                kept += 1
        else:
            kept += 1
    return pruned, kept


def prune_audit_outbox(
    shared_dir: Path,
    *,
    days: int = DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS,
    config: "dict | None" = None,
    now: datetime | None = None,
) -> AuditOutboxPruneResult:
    """Prune aged ``_ingested/<date>/`` archives for every bot + pod infra.

    Targets:

    1. Each bot's ``{bot_home}/.openclaw/workspace/evolve/audit_outbox/_ingested/<date>/``
       — bots enumerated from ``network.json`` (``evolve_config``), homes
       resolved via ``evolve_config.bot_home`` (pwd-backed, profile-keyed
       fallback). No ``/Users/`` is hardcoded — this works on the Linux
       VPS pod (``/home/<bot>``) too.
    2. The pod-wide ``{shared_dir}/infra_audit_outbox/_ingested/<date>/``.

    The live outbox roots (un-drained records) are never touched.

    ``days`` is the retention window (default 30). ``config`` overrides the
    network.json load (for tests). ``now`` is injectable for tests.
    """
    cutoff_now = now or datetime.now(timezone.utc)
    cutoff_date: date = (cutoff_now - timedelta(days=days)).date()

    if config is None:
        config = evolve_config.load_config()

    result = AuditOutboxPruneResult()

    # ── 1. per-bot drained archives ──────────────────────────────────────────
    bots = (config or {}).get("bots") or {}
    for bot_id in bots:
        result.bots_scanned += 1
        try:
            home = evolve_config.bot_home(bot_id, config)
        except Exception as exc:  # pwd/profile resolution is best-effort
            logger.warning(
                "audit_outbox_retention: could not resolve home for %s: %s",
                bot_id,
                exc,
            )
            continue
        ingested_root = home / _BOT_INGESTED_RELPATH
        pruned, kept = _prune_ingested_root(
            ingested_root, cutoff_date=cutoff_date, label=f"bot:{bot_id}"
        )
        result.bot_dirs_pruned += pruned
        result.bot_dirs_kept += kept

    # ── 2. pod-wide infra archive ────────────────────────────────────────────
    infra_ingested_root = Path(shared_dir) / _INFRA_INGESTED_RELNAME / "_ingested"
    infra_pruned, infra_kept = _prune_ingested_root(
        infra_ingested_root, cutoff_date=cutoff_date, label="infra"
    )
    result.infra_dirs_pruned = infra_pruned
    result.infra_dirs_kept = infra_kept

    return result


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prune aged audit_outbox/_ingested/<date>/ archives (per-bot "
            "and pod-wide infra). Mirrors signals.retention. Idempotent."
        )
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path(evolve_config.CANONICAL_SHARED_DIR),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_AUDIT_OUTBOX_RETENTION_DAYS,
        help="Days to retain drained _ingested/ day-dirs (default: 30)",
    )
    args = parser.parse_args(argv)

    result = prune_audit_outbox(args.shared_dir, days=args.days)
    print(
        f"audit_outbox/_ingested bots_scanned={result.bots_scanned}"
        f" · per-bot pruned={result.bot_dirs_pruned} kept={result.bot_dirs_kept}"
        f" · infra pruned={result.infra_dirs_pruned} kept={result.infra_dirs_kept}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
