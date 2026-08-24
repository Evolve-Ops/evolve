"""model_swap_ledger — append-only record of every model-rung change.

Motivated by the 2026-08-14 group-chat silence incident (design:
``internal/design-model-swap-behavior-guard-2026-08-19.md``). A fleet-wide bulk
tier update moved six bots' Workhorse rung to a new model. The write path
verified only that the model *string* landed in the tier; nothing recorded
what the rung held before, and nothing observed whether the bots still
behaved correctly afterwards. When one bot started leaking its
should-I-reply deliberation into four Slack channels, the operator had
neither an attribution artifact nor a one-command undo — the previous model
had to be reconstructed from the audit log by hand, days later.

This ledger is the durable seam both follow-ups hang off:

  * ``evolve-admin models rollback`` reads it to restore the previous
    ``models[]`` for a rung (see ``evolve_admin.model_swap_cli``).
  * ``model_swap_watch`` reads it to learn WHEN each bot's rung changed and
    FROM WHAT, so it can compare the bot's behavior either side of that
    instant — the ledger is what makes a post-swap behavior check possible
    at all.

On-disk shape — ``{shared_dir}/model_swaps.jsonl``, one JSON object per
line, appended in write order::

    {"ts": "2026-08-14T17:02:11+00:00", "bot_id": "team-bot-a", "tier": "standard",
     "provider": "anthropic",
     "previous_models": ["anthropic/claude-sonnet-4-6"],
     "new_models": ["anthropic/claude-sonnet-5"],
     "source": "admin_ui_bulk"}

Owned by the ``evolve`` user like the rest of ``{shared_dir}`` — plain
``open(..., "a")``, no ``/tmp`` staging or sudo. Append-only and small (one
line per rung change, a handful per pod per month), so no retention job:
the whole point is that a swap from months ago is still attributable.

Writes are best-effort by design. A failed ledger append NEVER fails the
config write that produced it — the swap already happened, and refusing to
report a completed write would be strictly worse than losing the record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MODEL_SWAP_LEDGER_NAME = "model_swaps.jsonl"


def swap_ledger_path(shared_dir: "str | Path | None" = None) -> Path:
    """Path to the append-only model-swap ledger.

    ``shared_dir`` defaults to the platform-keyed canonical shared dir
    (``/Users/Shared/evolve`` on macOS, ``/var/lib/evolve`` on Linux) — never
    a hardcoded ``/Users`` literal, which is the Linux-silent-break class.
    """
    if shared_dir is None:
        from platform_profile import get_profile  # type: ignore

        shared_dir = get_profile().shared_dir_default
    return Path(shared_dir) / MODEL_SWAP_LEDGER_NAME


def record_swap(
    bot_id: str,
    tier: str,
    provider: "str | None",
    previous_models: "list | None",
    new_models: "list | None",
    *,
    source: str,
    shared_dir: "str | Path | None" = None,
) -> bool:
    """Append one model-swap record. Returns True iff the line was written.

    ``previous_models`` is the tier's ``models[]`` as read BEFORE the write —
    capture it from the pre-write config, not from the post-write result.

    A no-op swap (``previous == new``) is deliberately NOT recorded: it
    carries no undo target, and it would hand the watcher a change instant at
    which nothing actually changed, splitting one behavioral window into two
    for no reason.
    """
    prev = list(previous_models or [])
    new = list(new_models or [])
    if prev == new:
        return False

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "bot_id": bot_id,
        "tier": tier,
        "provider": provider,
        "previous_models": prev,
        "new_models": new,
        "source": source,
    }
    try:
        path = swap_ledger_path(shared_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except OSError:
        return False


def read_swaps(shared_dir: "str | Path | None" = None) -> list:
    """Return every ledger record, oldest first. Unreadable ledger → ``[]``.

    Malformed lines are skipped rather than raising: the ledger is appended
    to by a long-lived web process, so a torn final line is a live
    possibility and must not blind every reader of the file.
    """
    try:
        text = swap_ledger_path(shared_dir).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("bot_id") and rec.get("tier") and rec.get("ts"):
            out.append(rec)
    return out


ROLLBACK_SOURCE = "cli_rollback"


def latest_swaps_by_rung(
    shared_dir: "str | Path | None" = None,
    *,
    exclude_sources: "set | frozenset | tuple | None" = None,
) -> dict:
    """Most recent ledger record per ``(bot_id, tier)``.

    Later records overwrite earlier ones for the same rung, so a rung swapped
    three times yields the last swap.

    ``exclude_sources`` skips records written by the named sources when
    choosing the winner. The rollback CLI passes ``{ROLLBACK_SOURCE}`` so that
    ``models rollback`` is IDEMPOTENT: without it, a rollback is itself the
    most recent change, and running the command twice would restore the model
    the operator just backed out of — a silent re-break dressed as an undo.
    Rollbacks are still recorded (the history stays honest) and are still
    visible to ``models swaps`` and to model_swap_watch, which correctly
    treats a rollback as a fresh behavioral boundary to re-evaluate.
    """
    excluded = set(exclude_sources or ())
    latest: dict = {}
    for rec in read_swaps(shared_dir):
        if rec.get("source") in excluded:
            continue
        latest[(rec["bot_id"], rec["tier"])] = rec
    return latest
