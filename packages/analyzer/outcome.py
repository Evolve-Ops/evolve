#!/usr/bin/env python3
"""
evolve/outcome.py — Post-apply outcome tracking

After apply.py successfully applies a proposal, it writes a pending outcome
record. This script runs daily, finds outcomes whose check-in window has
arrived (7 days after apply), and sends a Telegram message to Pod_admin asking
whether the change helped.

Pod_admin taps 👍 or 👎. The result is written to feedback/outcomes.jsonl and
used by analyze.py to calibrate detector thresholds over time.

Scheduled via launchd: daily at 09:00 AM local time.

Usage:
    python3 outcome.py --network config/network.json
    python3 outcome.py --shared-dir /Users/Shared/evolve
    python3 outcome.py --shared-dir /Users/Shared/evolve --process-replies
"""

import argparse
import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from calibration import CalibrationLoader
from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path


OUTCOME_VERSION = 1


def _load_outcome_calibration(shared_dir: str) -> tuple[int, int]:
    """Return (check_in_days, window_days) from calibration, with defaults."""
    try:
        cal = CalibrationLoader(shared_dir)
        outcomes_cfg = cal.load("outcomes").get("outcomes", {})
        return (
            int(outcomes_cfg.get("check_in_days", 7)),
            int(outcomes_cfg.get("window_days", 3)),
        )
    except Exception:
        return 7, 3


# Module-level defaults; overridden in process_due_checkins when shared_dir is known
CHECK_IN_DAYS = 7
OUTCOME_WINDOW_DAYS = 3


def load_pending_outcomes(shared_dir: str) -> list[dict]:
    """Load all pending outcome check-ins."""
    path = Path(shared_dir) / "feedback" / "pending-outcomes.jsonl"
    if not path.exists():
        return []
    outcomes = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    outcomes.append(json.loads(line))
                except Exception:
                    pass
    return outcomes


def write_pending_outcome(outcome: dict, shared_dir: str) -> None:
    """Append a pending outcome record (called by apply.py on success)."""
    path = Path(shared_dir) / "feedback" / "pending-outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(outcome) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_outcome_result(outcome_id: str, result: str, shared_dir: str) -> None:
    """
    Record the final outcome (thumbs_up / thumbs_down / expired).
    Appends to feedback/outcomes.jsonl — the calibration dataset.
    """
    record = {
        "schema_version": OUTCOME_VERSION,
        "outcome_id": outcome_id,
        "recorded": datetime.now(timezone.utc).isoformat(),
        "result": result,  # "thumbs_up" | "thumbs_down" | "expired"
    }
    path = Path(shared_dir) / "feedback" / "outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    print(f"[evolve/outcome] Recorded {result} for outcome {outcome_id}")


def rewrite_pending_outcomes(outcomes: list[dict], shared_dir: str) -> None:
    """Atomically overwrite the pending-outcomes file.

    Writes a temp file in the same directory and ``os.replace()``s it into
    place rather than ``open(path, "w")``-truncating the existing inode. The
    truncate path needs write permission on the *file*; the replace path
    needs it only on the *directory*. The daily ``outcome`` job runs as the
    ``evolve`` user, but ``feedback/pending-outcomes.jsonl`` can be owned by a
    different (legacy) user whose mode lacks a group/other write bit — exactly
    the ``PermissionError`` that crashed every run, even when there was
    nothing to rewrite. The replace also leaves no half-written file on crash
    and re-homes the inode to the writing user, healing the ownership drift
    after the first successful run.
    """
    path = Path(shared_dir) / "feedback" / "pending-outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".pending-outcomes.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            for o in outcomes:
                f.write(json.dumps(o) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def send_checkin_telegram(
    outcome: dict, network_config: dict, *, shared_dir: str | Path,
) -> bool:
    """Send the 7-day post-apply check-in message.

    The message includes a unique outcome_id so the reply can be matched.
    Operator taps 👍 or 👎; the gateway routes the callback back to
    analyze.py on the next --process-replies run.

    Phase C of docs/spec-alert-subscriptions-2026-05-10.md: routes
    through alerts.dispatcher.send so operator preferences for
    decisions.proposal_outcome_checkin take effect. Returns True iff
    the dispatcher reported SENT (preserves the caller's flag-write
    contract on success).
    """
    bot_id = outcome.get("target_bot", "unknown")
    summary = outcome.get("proposal_summary", "a recent change")
    apply_date = outcome.get("applied_date", "recently")
    outcome_id = outcome.get("outcome_id", "")

    import sys as _sys
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        print(f"[evolve/outcome] dispatcher import failed; alert dropped: {exc}",
              file=_sys.stderr)
        return False

    try:
        # Phase F5: payload — catalog body_template renders the prompt;
        # the catalog's bot_action ("Reply 'yes / no / details' to record
        # the outcome") replaces the pre-Phase-F 👍/👎 footer prose.
        out = _dispatch_send(
            shared_dir=Path(shared_dir),
            network=network_config,
            source="outcome",
            severity=Severity.INFO,
            dedup_key=f"outcome/checkin/{outcome_id}",
            catalog_event="decisions.proposal_outcome_checkin",
            payload={
                "bot_id": bot_id,
                "summary": summary,
                "apply_date": apply_date,
            },
        )
    except Exception as exc:
        print(f"[evolve/outcome] dispatcher.send raised; alert dropped: {exc}",
              file=_sys.stderr)
        return False

    if out.result == DispatchResult.SENT:
        print(f"[evolve/outcome] Check-in sent for outcome {outcome_id[:8]} (bot: {bot_id})")
        return True
    print(f"[evolve/outcome] Check-in not sent ({out.result.value}): "
          f"{out.error or 'no error detail'}")
    return False


def process_due_checkins(shared_dir: str, network_config: dict) -> int:
    """
    Find outcomes whose check-in window has arrived and send Telegram check-ins.
    Expire outcomes that haven't received a response within the configured window.
    Returns count of check-ins sent.
    """
    check_in_days, window_days = _load_outcome_calibration(shared_dir)

    today = date.today()
    pending = load_pending_outcomes(shared_dir)
    remaining = []
    sent = 0

    for outcome in pending:
        apply_date_str = outcome.get("applied_date", "")
        if not apply_date_str:
            continue

        try:
            apply_date = date.fromisoformat(apply_date_str)
        except ValueError:
            remaining.append(outcome)
            continue

        days_since_apply = (today - apply_date).days
        checkin_sent = outcome.get("checkin_sent", False)
        checkin_sent_date_str = outcome.get("checkin_sent_date", "")

        # Expiry: check-in sent but no response after window_days
        if checkin_sent and checkin_sent_date_str:
            try:
                sent_date = date.fromisoformat(checkin_sent_date_str)
                if (today - sent_date).days > window_days:
                    write_outcome_result(outcome["outcome_id"], "expired", shared_dir)
                    print(f"[evolve/outcome] Expired outcome {outcome['outcome_id'][:8]} — no response in {window_days}d")
                    continue  # Remove from pending
            except ValueError:
                pass

        # Time to send check-in
        if not checkin_sent and days_since_apply >= check_in_days:
            if send_checkin_telegram(outcome, network_config, shared_dir=shared_dir):
                outcome["checkin_sent"] = True
                outcome["checkin_sent_date"] = today.isoformat()
                sent += 1
            remaining.append(outcome)
        else:
            remaining.append(outcome)

    rewrite_pending_outcomes(remaining, shared_dir)
    return sent


def process_reply(outcome_id_prefix: str, result: str, shared_dir: str) -> bool:
    """
    Record a reply to an outcome check-in (called when Pod_admin responds).
    result: "thumbs_up" or "thumbs_down"
    outcome_id_prefix: first 8 chars of the outcome_id (from Telegram reply)
    """
    pending = load_pending_outcomes(shared_dir)
    matched = None
    remaining = []

    for outcome in pending:
        if outcome.get("outcome_id", "").startswith(outcome_id_prefix):
            matched = outcome
        else:
            remaining.append(outcome)

    if not matched:
        print(f"[evolve/outcome] No pending outcome found matching {outcome_id_prefix}")
        return False

    write_outcome_result(matched["outcome_id"], result, shared_dir)

    # General-path calibration wire — mirror forge_jobs.approve_job/reject_job.
    # The operator's 👍/👎 on an ordinary detector/generator proposal now
    # nudges that detector's confidence_multiplier, closing the same
    # outcome→confidence loop the forge path already has. Wrapped so
    # calibration can never break reply recording.
    detector_name = matched.get("detector_name", "")
    if detector_name:
        try:
            from calibration import CalibrationLoader
            cal = CalibrationLoader(shared_dir)
            stats = cal.detector(detector_name).get("outcome_stats", {})
            thumbs_up = stats.get("thumbs_up", 0)
            thumbs_down = stats.get("thumbs_down", 0)
            expired = stats.get("expired", 0)
            if result == "thumbs_up":
                thumbs_up += 1
                cal.set_detector_outcome_stat(detector_name, "thumbs_up", thumbs_up)
            elif result == "thumbs_down":
                thumbs_down += 1
                cal.set_detector_outcome_stat(detector_name, "thumbs_down", thumbs_down)
            cal.update_confidence_multiplier(
                detector_name, thumbs_up, thumbs_down, expired,
            )
        except Exception:
            pass

    rewrite_pending_outcomes(remaining, shared_dir)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve post-apply outcome tracking")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", default=str(resolve_network_path()), help="Path to network.json")
    parser.add_argument(
        "--process-replies",
        action="store_true",
        help="Process pending replies (called after Pod_admin responds)",
    )
    parser.add_argument(
        "--reply-id",
        help="Outcome ID prefix from Pod_admin's reply (used with --process-replies)",
    )
    parser.add_argument(
        "--reply-result",
        choices=["thumbs_up", "thumbs_down"],
        help="Pod_admin's response (used with --process-replies)",
    )
    args = parser.parse_args()
    # Module enable/disable check
    try:
        from evolve_config import (
            load_config as _lc2,
            is_module_enabled as _ime,
            is_rsi_enabled as _ire,
        )
        _cfg2 = _lc2(getattr(args, 'network', None))
        if not _ire(_cfg2):
            print(f"[outcome] RSI feedback loop is disabled — exiting.")
            import sys as _sys2; _sys2.exit(0)
        if not _ime(_cfg2, "outcomes"):
            print(f"[outcome] outcomes module is disabled — exiting.")
            import sys as _sys2; _sys2.exit(0)
    except Exception:
        pass

    shared_dir = args.shared_dir
    network_config: dict = {"alerts": {}}

    if args.network:
        network_config = json.loads(Path(args.network).read_text())
        shared_dir = network_config.get("sharedDir", shared_dir)

    if args.process_replies:
        if not args.reply_id or not args.reply_result:
            print("[evolve/outcome] --reply-id and --reply-result required with --process-replies")
            return
        success = process_reply(args.reply_id, args.reply_result, shared_dir)
        if success:
            print("[evolve/outcome] Reply recorded successfully")
        return

    sent = process_due_checkins(shared_dir, network_config)
    print(f"[evolve/outcome] Done — {sent} check-in(s) sent")


if __name__ == "__main__":
    main()
