"""roster_allowlist_drift_monitor — Signal producer for out-of-band changes to a
bot's group allowlist (``channels.<ch>`` group list).

R1a PR3 (spec docs/spec-users-meta-2026-06-15.md §"Deferred to a follow-up
(PR3)"). After PR2 the group allowlist the OpenClaw gateway enforces is the SAME
artifact the admin curates through ``/users/group-allowlist/{approve,revoke}``.
That closes the governance gap for changes made *through* the admin surface —
but an OpenClaw-CLI edit, a hand-edit, or a membership-sync that widens
``channels.<ch>.allowFrom`` **out-of-band** is invisible to the operator between
admin GETs, and those senders can spend the bot's tokens in the channel. This
monitor is the safety net: it compares each bot's live on-disk effective group
allowlist against the admin-recorded expected-state baseline
(``evolve_admin.roster_baseline``) and fires a Signal per drifted
``(bot, channel)``.

Boundary: **detection/observation only.** This does not auto-remediate the
allowlist — enforcement stays OpenClaw's fail-closed per-sender gate; correction
is the operator reverting the file or approving the new senders via the admin
route (either clears the Signal). Co-owned with META:edr (this is the
observability half).

Signal shape
============

  * ``roster_group_allowlist_changed`` — one per drifted ``(bot, channel)``,
    scope ``bot``, producer ``roster_allowlist_drift``. Signature keyed on
    ``{bot_id}/{channel}`` so a repeat of the same drift dedups and a corrected
    channel drops out of ``kept_signatures`` and auto-archives. ``details``
    carries ``added`` / ``removed`` sender ids — **added senders are the risk**
    (new identities that can spend the bot's tokens in the channel).
  * ``roster_group_allowlist_unreadable`` — one per bot whose ``openclaw.json``
    can't be read (missing / EACCES even via the sudo-cat fallback). A monitor
    that can't read must NOT look clean, so an unreadable bot fires this instead
    of silently passing, and its existing drift Signals are left untouched (we
    can't confirm they cleared). Auto-resolves when the bot reads cleanly again.

Baseline advance rule (see roster_baseline): the admin write path records the
baseline on every approve/revoke; this monitor only **seeds** it once on first
sight of a bot (adopting the current live config as expected — there is no prior
trusted state) and never advances it again. Seeding on first sight means the
first out-of-band edit *after* the seed is caught; a pre-existing edit made
before the monitor ever ran is adopted (there is no earlier trust anchor).

Run as
======

    sudo -u evolve python3 packages/analyzer/roster_allowlist_drift_monitor.py \\
        --network /Users/Shared/evolve/network.json

Installed hourly (evolve user, pod-wide) by
``analyzer_monitor_jobs.install_roster_allowlist_drift_monitor``; watched by
``monitor_coverage``'s producer-liveness layer.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "roster_allowlist_drift"
DRIFT_TYPE = "roster_group_allowlist_changed"
UNREADABLE_TYPE = "roster_group_allowlist_unreadable"


def _drift_signal(
    bot_id: str, channel: str, added: list[str], removed: list[str],
    baseline: list[str], current: list[str],
) -> dict:
    """Build the per-(bot, channel) drift Signal payload (pure)."""
    direction = (
        "widened" if added and not removed
        else "narrowed" if removed and not added
        else "changed"
    )
    n_add, n_rem = len(added), len(removed)
    title = (
        f"{bot_id} · {channel} group allowlist {direction} out-of-band "
        f"(+{n_add}/-{n_rem})"
    )
    add_line = f"Added (can now spend tokens): {', '.join(added)}" if added else ""
    rem_line = f"Removed: {', '.join(removed)}" if removed else ""
    body = (
        f"The `{channel}` group allowlist on `{bot_id}` diverged from the "
        f"admin-recorded baseline without going through the admin surface — an "
        f"OpenClaw-CLI edit, a hand-edit, or a membership sync.\n\n"
        + (add_line + "\n" if add_line else "")
        + (rem_line + "\n" if rem_line else "")
        + f"\nExpected (admin baseline): {baseline or '(none)'}\n"
        f"On disk now: {current or '(none)'}\n\n"
        "Added senders are the risk: they can spend the bot's tokens in this "
        "channel. Reconcile from the Users page — either revoke the "
        "out-of-band senders (reverts the file) or approve them (re-records "
        "the baseline). Either action clears this Signal on the next hourly "
        "tick."
    )
    return dict(
        signature=make_signature(PRODUCER, DRIFT_TYPE, f"{bot_id}/{channel}"),
        producer=PRODUCER,
        type=DRIFT_TYPE,
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            channel=channel,
            added=added,
            removed=removed,
            baseline=baseline,
            current=current,
            direction=direction,
            what_it_means=(
                f"The group allowlist `channels.{channel}` group list on "
                f"`{bot_id}` no longer matches what the admin last curated. "
                "After R1a PR2 this list is admin-managed, so a divergence "
                "means someone changed who can access the bot in this channel "
                "outside the admin surface (OpenClaw CLI, a hand-edit, or an "
                "external membership sync). Added senders can spend the bot's "
                "tokens; the operator is otherwise blind to the change until "
                "the next admin GET."
            ),
            fix_steps=(
                "1. Open the bot's Users page → Channel access (group) for "
                f"`{channel}`.\n"
                "2. If the added senders are illegitimate, revoke them "
                "(reverts the on-disk allowlist to the baseline).\n"
                "3. If they are legitimate, approve them via the admin route "
                "(re-records the baseline to match).\n"
                "4. Either action clears this Signal on the next hourly tick."
            ),
        ),
    )


def _unreadable_signal(bot_id: str) -> dict:
    """Build the per-bot 'openclaw.json unreadable' Signal payload (pure)."""
    return dict(
        signature=make_signature(PRODUCER, UNREADABLE_TYPE, bot_id),
        producer=PRODUCER,
        type=UNREADABLE_TYPE,
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: group allowlist unreadable — drift check blind",
        body=(
            f"Could not read `{bot_id}`'s openclaw.json (missing, or EACCES "
            "even via the sudo-cat fallback), so the out-of-band group-"
            "allowlist drift check cannot run for this bot. A monitor that "
            "can't read must not look clean — this Signal marks the blind "
            "spot rather than silently passing. Any existing drift Signals "
            "for this bot are left in place (we can't confirm they cleared).\n\n"
            "Fix: ensure the evolve read ACL on `~/.openclaw` is intact "
            "(`sudo evolve-admin ensure-pod-perms`) and that the bot has been "
            "deployed. The Signal auto-resolves once the config reads cleanly."
        ),
        details=dict(
            what_it_means=(
                f"The drift monitor could not read `{bot_id}`'s openclaw.json. "
                "The group-allowlist drift check is blind for this bot until "
                "the read is restored — typically an evolve read-ACL clamp on "
                "`~/.openclaw` (the OC gateway re-hardens it to 0700 on runtime "
                "ops; on Linux that clamps the POSIX-ACL mask)."
            ),
            fix_steps=(
                "1. Run `sudo evolve-admin ensure-pod-perms` to reassert the "
                "evolve read ACL on the bot's ~/.openclaw.\n"
                "2. Confirm the bot has been deployed (a fresh bot has no "
                "openclaw.json yet).\n"
                "3. The Signal auto-resolves on the next tick once the config "
                "reads cleanly."
            ),
        ),
    )


def _diff_channels(
    baseline: dict[str, list[str]], current: dict[str, list[str]],
) -> dict[str, tuple[list[str], list[str]]]:
    """Per-channel (added, removed) for channels that diverged.

    Compares as sets over the union of channel keys; a channel present in only
    one side counts its whole list as added/removed. Returns only channels with
    a non-empty diff, sorted for stable Signal ordering.
    """
    out: dict[str, tuple[list[str], list[str]]] = {}
    for ch in sorted(set(baseline) | set(current)):
        base_ids = set(baseline.get(ch, []))
        cur_ids = set(current.get(ch, []))
        added = sorted(cur_ids - base_ids)
        removed = sorted(base_ids - cur_ids)
        if added or removed:
            out[ch] = (added, removed)
    return out


def run(
    network_path: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """One pass: per bot compare live vs baseline group allowlists; emit + sweep."""
    now = now or datetime.now(timezone.utc)

    # Import lazily so this script can boot on a host where the admin package
    # isn't installed yet (early bootstrap; degrades to no-op) — same pattern as
    # pod_perms_drift_monitor.
    try:
        from evolve_admin.config import load_network  # type: ignore
        from evolve_admin import roster_baseline as rb  # type: ignore
    except ImportError as exc:
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"evolve_admin not importable: {exc}",
            }),
            flush=True,
        )
        return {"bots_scanned": 0, "drifted": 0, "signals_fired": 0}

    from platform_profile import get_profile

    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir") or get_profile().shared_dir_default)
    members = network.get("members") or list((network.get("bots") or {}).keys())

    attempted: set[str] = set()
    readable: set[str] = set()
    kept_drift: set[str] = set()
    kept_unreadable: set[str] = set()
    signals_fired = 0
    seeded = 0
    unreadable_count = 0

    for bot_id in members:
        attempted.add(bot_id)
        current = rb.read_current_allowlists(network, bot_id)
        if current is rb.UNREADABLE:
            unreadable_count += 1
            sig = _unreadable_signal(bot_id)
            kept_unreadable.add(sig["signature"])
            if dry_run:
                print(json.dumps({"would_observe": sig}, default=str), flush=True)
            else:
                try:
                    signals_store.observe(shared_dir, **sig)
                    signals_fired += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[roster_allowlist_drift] observe(unreadable) "
                          f"failed for {bot_id}: {exc}", flush=True)
            # Do NOT seed, diff, or sweep this bot's drift signals — blind tick.
            continue

        readable.add(bot_id)
        current_map: dict[str, list[str]] = current  # type: ignore[assignment]
        baseline = rb.read_baseline(shared_dir, bot_id)
        if baseline is None:
            # First sight — seed the baseline from the live config; do NOT fire
            # (no prior trusted state to call this drift).
            if not dry_run:
                try:
                    rb.write_baseline(
                        shared_dir, bot_id, current_map, source="monitor_seed")
                except Exception as exc:  # noqa: BLE001
                    print(f"[roster_allowlist_drift] seed baseline failed for "
                          f"{bot_id}: {exc}", flush=True)
            seeded += 1
            continue

        for ch, (added, removed) in _diff_channels(baseline, current_map).items():
            sig = _drift_signal(
                bot_id, ch, added, removed,
                baseline.get(ch, []), current_map.get(ch, []),
            )
            kept_drift.add(sig["signature"])
            if dry_run:
                print(json.dumps({"would_observe": sig}, default=str), flush=True)
            else:
                try:
                    signals_store.observe(shared_dir, **sig)
                    signals_fired += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[roster_allowlist_drift] observe(drift) failed for "
                          f"{bot_id}/{ch}: {exc}", flush=True)

    signals_resolved = 0
    if not dry_run:
        # Sweep drift Signals only for bots we READ this run — an unreadable
        # bot's prior drift Signals must persist (we can't confirm cleared).
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_drift,
                types={DRIFT_TYPE},
                bot_ids=readable,
                reason="auto-resolve: group allowlist drift cleared",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[roster_allowlist_drift] sweep_resolve(drift) failed: {exc}",
                  flush=True)
        # Sweep unreadable Signals over every bot we attempted, so a bot that
        # became readable this run has its stale unreadable Signal archived.
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_unreadable,
                types={UNREADABLE_TYPE},
                bot_ids=attempted,
                reason="auto-resolve: openclaw.json readable again",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[roster_allowlist_drift] sweep_resolve(unreadable) failed: "
                  f"{exc}", flush=True)

    summary = {
        "bots_scanned": len(attempted),
        "readable": len(readable),
        "unreadable": unreadable_count,
        "seeded": seeded,
        "drifted": len(kept_drift),
        "signals_fired": signals_fired,
        "signals_resolved": signals_resolved,
        "ran_at": now.isoformat(),
    }
    print(json.dumps(summary, default=str), flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    from platform_profile import get_profile

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--network",
        default=str(Path(get_profile().shared_dir_default) / "network.json"),
        help="Path to network.json (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Signals that would be observed but don't write them.",
    )
    args = parser.parse_args(argv)

    network_path = Path(args.network)
    if not network_path.exists():
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"network.json not found at {network_path}",
            }),
            flush=True,
        )
        return 0

    run(network_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
