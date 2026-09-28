"""stuck_proposal_monitor — emit a Signal when proposals sit in approved/ too long.

Reads on-disk telemetry only (no LLM):
  - {shared_dir}/proposals/approved/*.json — proposals waiting to be applied

One signal type, under producer ``stuck_proposal_monitor``:

  stuck_proposal       — One or more approved proposals are older than
                         ``threshold_days`` without progressing to applied
                         or archived. Pod-scoped; severity warn.

This catches the failure mode observed in the 2026-05-20 forensics:
``heal-team_bot_a-1776294604`` sat in ``approved/`` for a full month after a
quarantine action removed it then heal.py re-created it with the same
deterministic ID, and ``apply.py`` then skipped it forever because the
``apply-results/`` entry from the original quarantine made the idempotency
check fire. Without this monitor, stale approved proposals are invisible
unless an operator manually inspects the directory.

Auto-resolves via ``signals.store.sweep_resolve()`` when every approved
proposal is fresh again.

Pod-scoped (no per-bot fanout — one Signal lists all stuck proposals).
Cheap to run; safe for hourly launchd cadence.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "stuck_proposal_monitor"

# Default threshold. Proposals legitimately wait a few days for a forge
# result or for the operator to triage — a week is the point at which
# "waiting" turns into "stuck." Operators can override via --threshold-days.
DEFAULT_THRESHOLD_DAYS = 7


# ─────────────────────────────────────────────────────────────────────────────
# Detector — pure function over a list of (path, mtime, proposal_dict)
# ─────────────────────────────────────────────────────────────────────────────


def _load_approved_proposals(shared_dir: Path) -> list[tuple[Path, float, dict]]:
    """Read every JSON file in proposals/approved/ that parses.

    Returns ``[(path, mtime_epoch, parsed_dict), ...]``. Files that fail
    to parse are silently skipped — the directory may contain transient
    ``.tmp`` files from a partial write or scratch files an operator
    dropped in; we don't want a malformed file to halt the monitor.
    """
    approved = shared_dir / "proposals" / "approved"
    if not approved.exists():
        return []
    out: list[tuple[Path, float, dict]] = []
    for f in sorted(approved.iterdir()):
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        out.append((f, mtime, data))
    return out


def detect_stuck_proposals(
    approved: list[tuple[Path, float, dict]],
    *,
    threshold_days: int,
    now: datetime | None = None,
) -> dict | None:
    """Return a Signal-spec dict when one or more proposals are stuck.

    Returns None when no approved proposal exceeds the age threshold.
    Stuckness is measured from the JSON file's mtime — when the
    proposal was last touched on disk, which is the right "since when
    has nothing happened to it" signal.
    """
    if not approved:
        return None
    now_dt = now or datetime.now(timezone.utc)
    now_epoch = now_dt.timestamp()
    threshold_secs = threshold_days * 86400

    stuck: list[dict[str, Any]] = []
    for path, mtime, data in approved:
        age_secs = now_epoch - mtime
        if age_secs < threshold_secs:
            continue
        stuck.append(
            {
                "proposal_id": data.get("id") or path.stem,
                "target_bot": data.get("target_bot") or "?",
                "type": data.get("type") or "?",
                "age_days": round(age_secs / 86400, 1),
                "problem": (data.get("problem") or "")[:140],
            }
        )

    if not stuck:
        return None

    n = len(stuck)
    title = (
        f"{n} approved {'proposal' if n == 1 else 'proposals'} "
        f"stuck >{threshold_days}d without progressing"
    )

    body_lines = [
        f"Approved proposals that have not moved to applied/archived in {threshold_days}+ days:",
        "",
    ]
    for entry in sorted(stuck, key=lambda e: -e["age_days"]):
        body_lines.append(
            f"  - `{entry['proposal_id']}` "
            f"(`{entry['target_bot']}` · {entry['type']} · {entry['age_days']:.1f}d): "
            f"{entry['problem'] or '(no problem field)'}"
        )
    body_lines.extend(
        [
            "",
            "Investigate via the Proposals page, then either:",
            "",
            "  - Move to `archived/` if no longer relevant",
            "  - Check `apply-results/` for an old quarantine that's blocking",
            "    idempotent re-apply (the 2026-05-20 forensic case)",
            "  - Check the targeting bot's `evolve-apply.log` for skip reasons",
        ]
    )
    body = "\n".join(body_lines)

    # Severity framework: magnitude 1 for 1–2 stuck proposals (advisory),
    # magnitude 2 for 3+ (suggests the apply pipeline is broken pod-wide).
    magnitude = 2 if n >= 3 else 1
    return dict(
        signature=make_signature(PRODUCER, "stuck_proposal", "pod"),
        producer=PRODUCER,
        type="stuck_proposal",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=title,
        body=body,
        details=dict(
            threshold_days=threshold_days,
            stuck_count=n,
            stuck_proposals=stuck,
            vector="operations",
            magnitude=magnitude,
            what_it_means=(
                "One or more approved proposals have been sitting in "
                "approved/ for more than {days} days without progressing "
                "to applied or archived. The apply pipeline is either "
                "broken, gated on something, or skipping these proposals "
                "for an idempotency reason. The 2026-05-20 forensic case "
                "was a quarantine unlink that silently failed and pinned "
                "the proposal forever — the same shape will reproduce "
                "anytime apply.py's idempotency check matches an old "
                "apply-result that nothing cleared."
            ).format(days=threshold_days),
            fix_steps=(
                "1. Open Proposals → Approved in the admin UI to "
                "review each stuck proposal\n"
                "2. For each: check apply-results/ for an old quarantine "
                "blocking idempotent re-apply (rm the stale entry to "
                "unblock)\n"
                "3. Check the target bot's evolve-apply.log for skip "
                "reasons (idempotency, validation, conflict)\n"
                "4. If the proposal is no longer relevant, move it to "
                "archived/ manually\n"
                "5. If the apply daemon itself is stuck, restart with:\n"
                "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
                "system/ai.evolve.evolve.apply.<bot>"
            ),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    shared_dir: Path,
    *,
    threshold_days: int = DEFAULT_THRESHOLD_DAYS,
    proposals_loader=_load_approved_proposals,
) -> list[dict]:
    """Run the detector and return Signal specs (zero or one entry)."""
    approved = proposals_loader(shared_dir)
    spec = detect_stuck_proposals(approved, threshold_days=threshold_days)
    return [spec] if spec else []


def run(
    shared_dir: Path,
    *,
    threshold_days: int = DEFAULT_THRESHOLD_DAYS,
    dry_run: bool = False,
    proposals_loader=_load_approved_proposals,
) -> tuple[set[str], int, int]:
    """Collect, write Signal, sweep-resolve when cleared.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    detections = collect(
        shared_dir,
        threshold_days=threshold_days,
        proposals_loader=proposals_loader,
    )
    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[stuck_proposal_monitor] observe failed for {d['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: no approved proposals exceed threshold",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[stuck_proposal_monitor] sweep_resolve failed: {exc}", flush=True)

    return kept, len(detections), n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="stuck_proposal_monitor — Signal producer for stale approved proposals",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--threshold-days", type=int, default=DEFAULT_THRESHOLD_DAYS,
        help=f"Age (days) above which an approved proposal is 'stuck' (default {DEFAULT_THRESHOLD_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    kept, n_fired, n_resolved = run(
        shared_dir,
        threshold_days=args.threshold_days,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"[stuck_proposal_monitor] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[stuck_proposal_monitor] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
