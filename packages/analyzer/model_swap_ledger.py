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

A sibling ledger, ``{shared_dir}/model_swap_pins.jsonl``, records which
(bot, tier, model) pairs were **behavior-rejected** by ``models rollback`` —
the sticky-rollback pin the 2026-08-21 recurrence showed was missing (see
the "Behavior pins" section below).

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
PIN_LEDGER_NAME = "model_swap_pins.jsonl"


def model_key(model) -> str:
    """Bare, lowercased model name — ``anthropic/claude-sonnet-5`` →
    ``claude-sonnet-5``.

    The single normalization every (bot, tier, model) comparison in this
    module and its consumers uses. Configs, advisories and annotations spell
    the same model with and without the provider prefix (both spellings
    appear in live data); comparing bare names keeps a pin recorded against
    one spelling from silently missing the other.
    """
    return str(model or "").split("/")[-1].strip().lower()


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


# ── Behavior pins (sticky rollback) ──────────────────────────────────────────
# Why this exists (2026-08-21 recurrence of the 2026-08-14 group-chat silence
# incident): the post-incident ``models rollback`` restored the previous model,
# but nothing recorded that the swapped-in model had been REJECTED FOR
# BEHAVIOR. The next Model Freshness "Apply All" (``admin_ui_bulk``,
# 2026-08-21T22:28:53Z in the swap ledger) saw the rung as merely stale and
# re-applied the same model; the deliberation leaks resumed the next day. A
# rollback that a routine freshness sweep silently undoes is not an undo.
#
# A pin says: this (bot, tier, model) was behavior-rejected — the tier-write
# endpoints must refuse to reintroduce it without an explicit operator
# override. Same append-only JSONL shape as the swap ledger, sibling file
# ``{shared_dir}/model_swap_pins.jsonl``; latest action per
# (bot, tier, model_key) wins, so a pin is lifted by appending an ``unpin``
# rather than rewriting history.


class PinLedgerUnreadable(OSError):
    """The pin ledger EXISTS but could not be read or holds no valid records.

    Deliberately distinct from the file being absent (the normal state of a
    pod that never rolled back — no pins). Enforcement call sites must treat
    this as "pin state unknown" and fail toward REFUSING the write with a
    loud, distinct error — an unreadable pin file silently reading as "no
    pins" is exactly the non-sticky rollback this ledger exists to fix.
    """


def pin_ledger_path(shared_dir: "str | Path | None" = None) -> Path:
    """Path to the append-only behavior-pin ledger (sibling of the swap ledger)."""
    return swap_ledger_path(shared_dir).with_name(PIN_LEDGER_NAME)


def record_pin_event(
    bot_id: str,
    tier: str,
    model: str,
    *,
    action: str,
    reason: str,
    source: str,
    shared_dir: "str | Path | None" = None,
) -> bool:
    """Append one pin/unpin event. Returns True iff the line was written.

    Unlike :func:`record_swap`, callers should be LOUD about a False return
    when ``action="pin"``: a rollback whose pin failed to write is exactly as
    non-sticky as before this ledger existed.
    """
    if action not in ("pin", "unpin"):
        raise ValueError(f"action must be 'pin' or 'unpin', got {action!r}")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "bot_id": bot_id,
        "tier": tier,
        "model": model,
        "action": action,
        "reason": reason,
        "source": source,
    }
    try:
        path = pin_ledger_path(shared_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except OSError:
        return False


def read_pin_events(shared_dir: "str | Path | None" = None) -> list:
    """Every pin/unpin event, oldest first. Absent file → ``[]``.

    A file that exists but cannot be read (or parses to zero valid records —
    truncation/corruption) raises :class:`PinLedgerUnreadable`: unlike the
    swap ledger, the pin ledger is an ENFORCEMENT input, and unreadable must
    not silently read as unpinned. Torn final lines are still tolerated as
    long as at least one record survives.
    """
    path = pin_ledger_path(shared_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PinLedgerUnreadable(f"cannot read {path}: {exc}") from exc
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and rec.get("bot_id") and rec.get("tier")
                and rec.get("model") and rec.get("action") in ("pin", "unpin")):
            out.append(rec)
    if not out and text.strip():
        raise PinLedgerUnreadable(f"{path} exists but holds no valid pin records")
    return out


def active_pins(shared_dir: "str | Path | None" = None) -> dict:
    """Currently-pinned pairs: ``{(bot_id, tier, model_key): pin_record}``.

    Replays the event log; the latest action per (bot, tier, model_key) wins,
    so an ``unpin`` lifts the pin without erasing the history. Raises
    :class:`PinLedgerUnreadable` (see :func:`read_pin_events`).
    """
    state: dict = {}
    for rec in read_pin_events(shared_dir):
        key = (rec["bot_id"], rec["tier"], model_key(rec["model"]))
        if rec["action"] == "pin":
            state[key] = rec
        else:
            state.pop(key, None)
    return state


def find_active_pin(
    bot_id: str,
    tier: str,
    model: str,
    shared_dir: "str | Path | None" = None,
) -> "dict | None":
    """The active pin record for (bot, tier, model), or None. May raise
    :class:`PinLedgerUnreadable`."""
    return active_pins(shared_dir).get((bot_id, tier, model_key(model)))


def previously_rejected_models(shared_dir: "str | Path | None" = None) -> dict:
    """Models ever behavior-rejected per rung: ``{(bot_id, tier): {model_key}}``.

    The union of two histories, deliberately ignoring later unpins:

      * every model a ``cli_rollback`` swap record backed OUT of a rung
        (``previous_models`` minus the restored ``new_models``), and
      * every model ever named in a ``pin`` event.

    This is the model_swap_watch input for the repeat-swap early-warning
    floor: a model that was rolled back once carries a strong prior, and an
    operator's explicit override (which unpins) re-applies the model but does
    not erase that prior — the watcher should judge the repeat SOONER, not
    forget it happened. Unreadable pin ledger degrades to the rollback-derived
    set alone (the watcher is an observer, not an enforcement gate).
    """
    rejected: dict = {}
    for rec in read_swaps(shared_dir):
        if rec.get("source") != ROLLBACK_SOURCE:
            continue
        restored = {model_key(m) for m in (rec.get("new_models") or [])}
        backed_out = {
            model_key(m) for m in (rec.get("previous_models") or [])
            if model_key(m) not in restored
        }
        if backed_out:
            rejected.setdefault((rec["bot_id"], rec["tier"]), set()).update(backed_out)
    try:
        events = read_pin_events(shared_dir)
    except PinLedgerUnreadable:
        events = []
    for rec in events:
        if rec["action"] == "pin":
            rejected.setdefault((rec["bot_id"], rec["tier"]), set()).add(
                model_key(rec["model"]))
    return rejected
