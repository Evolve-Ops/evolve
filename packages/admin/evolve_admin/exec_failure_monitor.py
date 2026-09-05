"""exec_failure_monitor.py — Signal producer for absorbed bot exec failures.

Design: internal/design-exec-failure-hygiene-2026-08-31.md (A2).

The plugin-side ExecFailureAbsorber (packages/plugin/src/observer/
ExecFailureAbsorber.ts) intercepts OpenClaw's raw ``⚠️ 🛠️ Exec failed …``
trailers at the ``message_sending`` hook and appends every match to a
per-bot ledger at ``{shared_dir}/{bot}/exec-failures/
exec-failures-<YYYY-MM-DD>.jsonl`` (UTC-dated, like the turns ledgers).
Absorbed is not vanished: this monitor aggregates the ledger into the
Signal store so recurring failure *shapes* stay operator-visible — one
Signal per (bot × command-shape), not one per occurrence — and
``sweep_resolve`` archives shapes that stop firing.

Shape derivation is VALUE-FREE (dedup-fingerprint doctrine) while keeping
the one diagnostic token: the leading command word of an inline-code span
survives, its arguments do not, and numbers / hex ids collapse — so
``Exec failed: `ls /a` (exit 1)`` and ``Exec failed: `ls /b` (exit 2)``
fold into one shape while ``git push`` failures stay a distinct shape.
The raw matched line stays in the LEDGER only; it is never copied into
the Signal body/details (a failing command line can embed an argv secret
— see upstream openclaw#125704 — and Signals flow to digests/chat).

Adjacent producer: ``exec_outcome_watchdog`` (packages/analyzer/
exec_outcome_watchdog.py) watches exec failures from the MODEL-facing
side (turn annotations: tool_error_burst, exec_denied, …) and feeds the
exec_outcome_investigator generator. This producer covers the CHANNEL
side — what raw trailer text was (or would have been) absorbed before
reaching a user. Same underlying incident can legitimately surface once
in each lane; this one is digest-only info, the watchdog owns paging.

Runs from the hourly audit-scheduler tick and from the Alerts page
``POST /api/signals/refresh`` fan-out. Purely local file reads — no
subprocess, no sudo (the admin daemon's ``evolve`` user reads the
per-bot ledger dirs via the inheritable read ACL that ``deploy.py``
grants on ``exec-failures/`` at deploy time).

Tri-state honesty: a ledger dir/file that cannot be READ (permissions,
mount hiccup) is not the same as "no failures". Any read error skips
``sweep_resolve`` entirely for the run — auto-resolving firing Signals
on a tooling failure would mask real breakage (the cron_exit_monitor
cannot-escalate doctrine).

Escalation policy (A3) is deliberately NOT here yet: signals land at the
producer-default severity/flavor (info / activity) and flow to the daily
digest via the ``meta.unclassified`` catalog fallback — quiet observation
first, and thresholds set only after the ledger shows real distributions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRODUCER = "bot_exec_failures"
TYPE_SHAPE = "exec_failure_shape"

# How far back the ledger scan looks. A shape with no occurrence inside the
# window drops out of kept_signatures and sweep_resolve archives it.
WINDOW_HOURS = 24

# Ledger rows that count as failure observations: armed absorptions and
# observe-only would-absorptions (the mode is carried per-row in ``armed``,
# and a fleet still in observe-only mode must already see its failure
# shapes). ``near_miss`` rows are drift telemetry, not failures — excluded.
_ACTIONS = {"absorbed", "stripped", "would_absorb", "would_strip"}


def _shape_of(line: str) -> str:
    """Collapse a matched trailer line to its value-free shape.

    Keeps the leading command word of each inline-code span (the most
    diagnostic token — without it every exec failure on a bot folds into
    one shape and A3 thresholds can't be derived per failure class), drops
    the arguments, then collapses hex ids and numbers case-insensitively.
    """
    s = line.strip()
    s = re.sub(r"`\s*([^`\s]+)[^`]*`", r"`\1 …`", s)  # keep command word only
    s = re.sub(r"\b[0-9a-f]{8,64}\b", "HEX", s, flags=re.IGNORECASE)
    s = re.sub(r"-?\d+", "N", s)
    s = re.sub(r"\s+", " ", s)
    return s[:200]


def _shape_key(shape: str) -> str:
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]


def _window_dates(now: datetime) -> list[str]:
    """UTC date-shard names covering the scan window (ledger files are
    UTC-named — never derive these from local time). Derived from
    WINDOW_HOURS so widening the window cannot silently skip shards."""
    n_days = int(WINDOW_HOURS // 24) + 1
    return sorted(
        {(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(n_days + 1)}
    )


def _collect_ledger_rows(
    shared_dir: Path, now: datetime
) -> tuple[list[tuple[str, dict]], int]:
    """Collect (bot_id, row) for every in-window ledger row on the pod.

    Returns ``(rows, read_errors)``. Bots are discovered from the ledger
    layout itself ({shared_dir}/<bot>/exec-failures/) rather than a roster
    read — a bot that wrote a ledger is exactly a bot whose failures we
    should aggregate, and a retired bot's stale shapes age out via the
    window + sweep. A missing shard file is normal (FileNotFoundError);
    any OTHER read error increments ``read_errors`` so the caller can
    refuse to sweep on unknown state.
    """
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    rows: list[tuple[str, dict]] = []
    read_errors = 0
    try:
        children = sorted(p for p in shared_dir.iterdir() if p.is_dir())
    except FileNotFoundError:
        return rows, 0
    except OSError:
        return rows, 1
    for bot_dir in children:
        ledger_dir = bot_dir / "exec-failures"
        if not ledger_dir.is_dir():
            continue
        for date_name in _window_dates(now):
            f = ledger_dir / f"exec-failures-{date_name}.jsonl"
            try:
                with f.open(encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except ValueError:
                            continue
                        if row.get("action") not in _ACTIONS:
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


def emit_exec_failure_signals(
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate the per-bot exec-failure ledgers into dedup'd Signals.

    Returns ``{"bots": int, "lines": int, "shapes": int, "read_errors":
    int}`` (``lines`` counts matched trailer lines, not ledger rows). One
    Signal per (bot × shape) via find-or-create signature dedup; shapes
    absent from the window are archived by ``sweep_resolve`` — unless any
    read error occurred, in which case the sweep is skipped (unknown
    state must not read as "condition cleared").
    """
    try:
        from schema.signal import make_signature
        from signals import store as signals_store
    except Exception as exc:
        logger.warning("exec_failure_monitor: signals import failed: %s", exc)
        return {"bots": 0, "lines": 0, "shapes": 0, "read_errors": 0}

    now = now or datetime.now(timezone.utc)
    ledger_rows, read_errors = _collect_ledger_rows(shared_dir, now)

    # (bot, shape) → aggregate
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    bots: set[str] = set()
    lines = 0
    for bot_id, row in ledger_rows:
        bots.add(bot_id)
        for line in row.get("matched_lines") or []:
            lines += 1
            shape = _shape_of(str(line))
            entry = agg.setdefault((bot_id, shape), {
                "count": 0,
                "first_ts": row.get("ts"),
                "channels": set(),
                "armed_seen": False,
            })
            entry["count"] += 1
            entry["last_ts"] = row.get("ts")
            if row.get("channel"):
                entry["channels"].add(str(row["channel"]))
            if row.get("armed") is True:
                entry["armed_seen"] = True

    kept: set[str] = set()
    for (bot_id, shape), entry in agg.items():
        signature = make_signature(
            PRODUCER, TYPE_SHAPE, f"{bot_id}:{_shape_key(shape)}"
        )
        kept.add(signature)
        absorbed = entry["armed_seen"]
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=TYPE_SHAPE,
            flavor="activity",
            scope="bot",
            bot_id=bot_id,
            title=(
                f"{bot_id}: recurring exec-failure shape "
                f"({entry['count']}× in {WINDOW_HOURS}h)"
            ),
            body=(
                f"The bot's shell commands keep failing with the same shape:\n"
                f"{shape}\n\n"
                f"Raw lines are in the per-bot ledger (not copied here — a "
                f"failing command line can embed a secret):\n"
                f"{bot_id}/exec-failures/ under the shared dir.\n\n"
                + (
                    "The raw trailer was absorbed before reaching the user "
                    "channel (exec-failure hygiene is armed); the bot itself "
                    "still sees each failure and can adapt."
                    if absorbed
                    else
                    "Exec-failure hygiene is in observe-only mode, so the raw "
                    "trailer also reached the channel. Arm it with plugin "
                    "config execFailureAbsorb: true once the ledger looks "
                    "trustworthy."
                )
            ),
            details={
                "shape": shape,
                "count_in_window": entry["count"],
                "window_hours": WINDOW_HOURS,
                "first_ts": entry["first_ts"],
                "last_ts": entry["last_ts"],
                "channels": sorted(entry["channels"]),
                "absorb_armed": absorbed,
                "ledger": f"{{shared_dir}}/{bot_id}/exec-failures/",
                "design": "internal/design-exec-failure-hygiene-2026-08-31.md",
            },
        )

    if read_errors:
        # Unknown state: a shape we failed to read is not a cleared shape.
        logger.warning(
            "exec_failure_monitor: %d ledger read error(s) — skipping "
            "sweep_resolve this run so unreadable ledgers don't auto-archive "
            "live failure shapes", read_errors,
        )
    else:
        # Comprehensive sweep: shapes with no in-window occurrence auto-archive.
        try:
            signals_store.sweep_resolve(
                shared_dir, producer=PRODUCER, kept_signatures=kept
            )
        except Exception as exc:
            logger.warning("exec_failure_monitor: sweep_resolve failed: %s", exc)

    return {
        "bots": len(bots),
        "lines": lines,
        "shapes": len(agg),
        "read_errors": read_errors,
    }
