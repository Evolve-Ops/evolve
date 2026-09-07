"""tool_profile_monitor.py — Signal producer for out-of-profile tool calls.

Design: internal/design-pa-context-economy-2026-08-31.md §3 CE-2 (tool
profiles). Brief: internal/dispatch/done/tool-schema-diet-per-session-type.md.

The plugin-side tool-profile filter (packages/plugin/src/tools/ToolProfiles.ts)
gives a background / one-shot / Evolve-dispatch session a trimmed tool surface:
a tool the session's profile does not carry still EXISTS under its own name but
rides a one-line description and refuses when called. Every refusal is appended
to a per-bot ledger at
``{shared_dir}/{bot}/turns/tool-profile-refusals-<YYYY-MM-DD>.jsonl``.

A refusal is not a failure — it is EVIDENCE that a profile is too narrow, and
the only evidence there is. This monitor aggregates the ledger into the Signal
store so the operator sees which (bot × profile × tool) the trim is actually
costing, and can widen the table from measurement rather than from a guess.
One Signal per (bot, profile, tool) — not one per occurrence — and
``sweep_resolve`` archives a combination that stops recurring (a profile that
was widened, or a bot that stopped reaching for the tool).

``turns/`` on purpose: the plugin already writes the prefix-hash ledger and
context-footprint.json there, and the admin daemon's ``evolve`` user already
reads that dir, so this needs no new directory, no new ACL, and no deploy-time
pre-create.

Runs from the hourly audit-scheduler tick and from the Alerts page
``POST /api/signals/refresh`` fan-out — the same two hosts
``exec_failure_monitor`` uses. Purely local file reads: no subprocess, no sudo.

Tri-state honesty: a ledger file that cannot be READ is not the same as "no
refusals". Any read error skips ``sweep_resolve`` entirely for the run, so a
tooling failure can never archive a live Signal (the cron_exit_monitor
cannot-escalate doctrine).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRODUCER = "tool_profile"
TYPE_REFUSAL = "tool_profile_refusal"

#: Ledger dir + file stem, mirroring ToolProfiles.writeToolProfileRefusalLedger.
LEDGER_SUBDIR = "turns"
LEDGER_PREFIX = "tool-profile-refusals-"

#: How far back the ledger scan looks. A (bot, profile, tool) with no refusal
#: inside the window drops out of kept_signatures and sweep_resolve archives it.
WINDOW_HOURS = 24

#: How long a ledger shard is kept on disk. The Signal store is the durable
#: record (with its own 90-day archive retention); the shards are only the
#: input to this sweep, so anything older than the scan window plus a few days
#: of slack is sediment. Pruned by this monitor itself — a new accumulating
#: write path must carry its own retention, not wait for one.
RETENTION_DAYS = 7


def _window_dates(now: datetime) -> list[str]:
    """UTC date-shard names covering the scan window.

    Ledger files are UTC-named — deriving these from local time reads the wrong
    shard for part of every day. Derived from WINDOW_HOURS so widening the
    window cannot silently skip shards.
    """
    n_days = int(WINDOW_HOURS // 24) + 1
    return sorted(
        {(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(n_days + 1)}
    )


def _prune_old_shards(shared_dir: Path, now: datetime) -> int:
    """Delete refusal shards older than :data:`RETENTION_DAYS`. Returns the
    count removed.

    Only ever unlinks a file whose name is exactly ``<prefix><YYYY-MM-DD>.jsonl``
    with a parseable date — never a glob sweep, so nothing else in ``turns/``
    (the prefix-hash ledger, the tool footprint) can be caught by it.
    """
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date()
    removed = 0
    try:
        children = sorted(p for p in shared_dir.iterdir() if p.is_dir())
    except OSError:
        return 0
    for bot_dir in children:
        ledger_dir = bot_dir / LEDGER_SUBDIR
        if not ledger_dir.is_dir():
            continue
        try:
            names = sorted(p.name for p in ledger_dir.iterdir())
        except OSError:
            continue
        for name in names:
            if not (name.startswith(LEDGER_PREFIX) and name.endswith(".jsonl")):
                continue
            stamp = name[len(LEDGER_PREFIX):-len(".jsonl")]
            try:
                day = datetime.strptime(stamp, "%Y-%m-%d").date()
            except ValueError:
                continue  # not a dated shard — leave it alone
            if day >= cutoff:
                continue
            try:
                (ledger_dir / name).unlink()
                removed += 1
            except OSError:
                continue  # another writer, or not ours to remove
    return removed


def _collect_ledger_rows(
    shared_dir: Path, now: datetime
) -> "tuple[list[tuple[str, dict]], int]":
    """Collect ``(bot_id, row)`` for every in-window refusal on the pod.

    Returns ``(rows, read_errors)``. Bots are discovered from the ledger layout
    itself rather than a roster read — a bot that wrote a ledger is exactly a
    bot whose refusals matter, and a retired bot's rows age out via the window
    plus the sweep. A missing shard is normal; any OTHER read error increments
    ``read_errors`` so the caller can refuse to sweep on unknown state.
    """
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    rows: "list[tuple[str, dict]]" = []
    read_errors = 0
    try:
        children = sorted(p for p in shared_dir.iterdir() if p.is_dir())
    except FileNotFoundError:
        return rows, 0
    except OSError:
        return rows, 1
    for bot_dir in children:
        ledger_dir = bot_dir / LEDGER_SUBDIR
        if not ledger_dir.is_dir():
            continue
        for date_name in _window_dates(now):
            path = ledger_dir / f"{LEDGER_PREFIX}{date_name}.jsonl"
            try:
                with path.open(encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(row, dict) or not row.get("tool_name"):
                            continue
                        try:
                            when = datetime.fromisoformat(
                                str(row.get("ts")).replace("Z", "+00:00")
                            )
                        except ValueError:
                            continue
                        if when.tzinfo is None:
                            # Tolerate a naive stamp rather than letting the
                            # aware/naive comparison TypeError kill the sweep.
                            when = when.replace(tzinfo=timezone.utc)
                        if when < cutoff:
                            continue
                        rows.append((bot_dir.name, row))
            except FileNotFoundError:
                continue  # absent date shard — the normal case
            except OSError:
                read_errors += 1
    return rows, read_errors


def emit_tool_profile_signals(
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> "dict[str, Any]":
    """Aggregate the per-bot refusal ledgers into dedup'd Signals.

    Returns ``{"bots": int, "refusals": int, "combinations": int,
    "read_errors": int, "pruned": int}``.
    """
    try:
        from schema.signal import make_signature
        from signals import store as signals_store
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        logger.warning("tool_profile_monitor: signals import failed: %s", exc)
        return {"bots": 0, "refusals": 0, "combinations": 0, "read_errors": 0,
                "pruned": 0}

    now = now or datetime.now(timezone.utc)
    ledger_rows, read_errors = _collect_ledger_rows(shared_dir, now)

    agg: "dict[tuple[str, str, str], dict[str, Any]]" = {}
    bots: "set[str]" = set()
    for bot_id, row in ledger_rows:
        bots.add(bot_id)
        profile = str(row.get("profile") or "unknown")
        tool = str(row["tool_name"])
        entry = agg.setdefault((bot_id, profile, tool), {
            "count": 0,
            "first_ts": row.get("ts"),
            "kinds": set(),
        })
        entry["count"] += 1
        entry["last_ts"] = row.get("ts")
        if row.get("session_kind"):
            entry["kinds"].add(str(row["session_kind"]))

    kept: "set[str]" = set()
    for (bot_id, profile, tool), entry in agg.items():
        signature = make_signature(PRODUCER, TYPE_REFUSAL, f"{bot_id}:{profile}:{tool}")
        kept.add(signature)
        kinds = sorted(entry["kinds"]) or ["unrecorded"]
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=TYPE_REFUSAL,
            scope="bot",
            bot_id=bot_id,
            title=(
                f"{bot_id}: {tool} refused by tool profile {profile} "
                f"({entry['count']}x in {WINDOW_HOURS}h)"
            ),
            body=(
                f"A {'/'.join(kinds)} session reached for `{tool}`, which the "
                f"`{profile}` tool profile does not carry. The call was "
                f"refused with a message naming the profile — nothing ran, and "
                f"nothing was silently missing.\n\n"
                f"This is the evidence the profile table asks for. If these "
                f"sessions genuinely need `{tool}`, add it to that profile in "
                f"packages/plugin/src/tools/ToolProfiles.ts and the Signal "
                f"resolves on its own once the refusals stop. If they do not, "
                f"the trim is working and the model is guessing — leave it.\n\n"
                f"Ledger: {{shared_dir}}/{bot_id}/{LEDGER_SUBDIR}/"
                f"{LEDGER_PREFIX}<date>.jsonl"
            ),
            details={
                "tool": tool,
                "profile": profile,
                "session_kinds": kinds,
                "count_in_window": entry["count"],
                "window_hours": WINDOW_HOURS,
                "first_ts": entry["first_ts"],
                "last_ts": entry["last_ts"],
                "widen_at": "packages/plugin/src/tools/ToolProfiles.ts",
                "design": "internal/design-pa-context-economy-2026-08-31.md",
            },
        )

    # Retention, after the read: shards older than the window are sediment.
    # Skipped on a read error, for the same reason the sweep is — a shard we
    # could not read is not a shard we have finished with.
    pruned = 0 if read_errors else _prune_old_shards(shared_dir, now)

    if read_errors:
        # Unknown state: a refusal we failed to read is not a refusal that
        # stopped happening.
        logger.warning(
            "tool_profile_monitor: %d ledger read error(s) — skipping "
            "sweep_resolve this run so unreadable ledgers don't auto-archive "
            "live refusals", read_errors,
        )
    else:
        try:
            signals_store.sweep_resolve(
                shared_dir, producer=PRODUCER, kept_signatures=kept
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_profile_monitor: sweep_resolve failed: %s", exc)

    return {
        "bots": len(bots),
        "refusals": sum(int(e["count"]) for e in agg.values()),
        "combinations": len(agg),
        "read_errors": read_errors,
        "pruned": pruned,
    }
