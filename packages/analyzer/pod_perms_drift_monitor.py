"""pod_perms_drift_monitor — hourly check + Signal for pod-perm drift.

Runs ``ensure_pod_perms(check_only=True)`` against the live pod and
emits a Signal when any of the perm-contract invariants drift away
from the canonical state.

Why a separate daemon (rather than fixing in-place)
====================================================

``ensure_pod_perms`` itself can apply fixes — but requires sudo
(chmod +a, chown, sudo mkdir). Running an unattended apply-daemon as
root every hour is heavier than this class of drift warrants.

The drift pattern we're catching: between deploys, a per-bot
analyzer daemon (running as the bot user, e.g. ``security_bot``) is the
first writer to a shared dir like ``{sharedDir}/proposals/pending/``.
Python's ``Path.mkdir(parents=True, exist_ok=True)`` creates the dir
owned by the bot user. Mode is 1777 (sticky), but the OWNER drift
matters — in a sticky-bit dir, only the dir owner can rename files
created by other users. Once ownership drifts to a non-evolve user,
the admin server can no longer move proposals between subdirs
(e.g. dismissing a proposal owned by a bot daemon fails with EACCES
on the atomic rename).

``ensure_pod_perms`` codifies the right contract (``dir_owner ==
evolve``) and DOES apply the fix when invoked with ``check_only=
False`` — but the only call sites today are ``deploy_bot()`` and the
pod-wide deploy branch. Between deploys, drift sits broken.

This daemon closes the gap: hourly, check-only pass; Signal emitted
on drift; operator runs ``sudo evolve-admin ensure-pod-perms`` to
apply the fix (per-existing CLI; existing sudoers grant).

One exception is APPLIED here, not just checked: the per-bot
**evolve read/traverse** contract on ``~/.openclaw``. The OC gateway
re-hardens that dir to 0700 on its own runtime ops (gateway restart;
every ``openclaw`` invocation an hourly Evolve daemon makes against a
bot), and on Linux that chmod clamps the POSIX-ACL mask so evolve loses
read+traverse until the mask is re-widened — an HOURLY flap that used to
surface as repeated ACL-drift Signals + Sysadmin-Watchdog restore
Proposals. Re-widening is a single ``setfacl -m m::rwX`` that uses only
evolve's OWN sudoers grant (no root chmod/chown), so unlike the pod-wide
owner/mode fixes it is cheap+safe to apply every hour. We do so via
``secret_config_perms.reassert_pod_evolve_access``; a bot that self-heals
emits no Signal (that silence ends the flap), and only a bot still locked
out after the escalated full re-grant rides the drift Signal. macOS has
no ACL mask, so this is a structural no-op there.

Signal shape
============

One Signal per run if drift is detected — type
``pod_perms_drift``, scope ``pod``, producer ``pod_perms_drift``.
Body lists every drifting target with the proposed fix string from
``ensure_pod_perms`` (the same text the CLI would print).

Sweep-resolve: the same signature is re-emitted while drift persists;
once the operator runs the apply pass and the next monitor tick sees
no drift, the signature drops out of ``kept_signatures`` and the
Signal auto-archives.

This run ALSO owns the clear side of a Signal it never fires: the
``credential_access`` alert (#3477), raised by ``oc_auth_store`` when
its own inline ACL heal failed and a bot's credential store stayed
unreadable. This daemon already computes, per bot, whether the
evolve read/traverse contract holds after the periodic reassert, so it
is the natural sweeper — a bot that is readable again has its alert
auto-archived within ≤1 cycle.

Run as
======

    sudo -u evolve python3 packages/analyzer/pod_perms_drift_monitor.py \\
        --network /Users/Shared/evolve/network.json

The launchd plist (installed by ``_install_launchd_pod_perms_drift_
monitor`` in deploy.py) wires this with hourly cadence + run_at_load.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "pod_perms_drift"


def _drift_signal(drifted: list) -> dict:
    """Build the per-pod drift Signal payload.

    ``drifted`` is a list of ``_PermCheck`` records (from
    ensure_pod_perms's result). We extract just the fields the
    operator needs to triage; the full record is large and includes
    a callable that won't JSON-serialize.
    """
    # Stable sort by (category, target) so the Signal's body order is
    # deterministic — operators reading "this Signal again" see the
    # same line ordering across runs, and the signature stays stable
    # while the drift persists.
    drifted_summary = sorted(
        [
            {
                "category": str(getattr(c, "category", "?")),
                "target": str(getattr(c, "target", "?")),
                "detail": str(getattr(c, "detail", "")),
                "fix_description": str(getattr(c, "fix_description", "") or ""),
            }
            for c in drifted
        ],
        key=lambda d: (d["category"], d["target"]),
    )
    n = len(drifted_summary)
    title = (
        f"pod perm contract drifted: {n} target{'s' if n != 1 else ''} "
        f"need ensure-pod-perms"
    )
    # Group by category for the body so the operator reads layers
    # (ACL / dir-owner / dir-mode / proposals-dir) as coherent groups
    # rather than a flat list.
    #
    # Newline arithmetic (the body is read as Markdown on the Alerts page,
    # where a run-together line is unreadable): entries carry NO newline of
    # their own and are joined with "\n", so each drift target lands on its
    # own line. A category header carries a LEADING "\n" so the join renders
    # it as `…entry\n\n**next-cat**` — a blank line separating groups. The
    # preceding f-string ends with "\n", so the FIRST header gets that same
    # blank-line separation from the intro paragraph.
    lines: list[str] = []
    cur_cat: str | None = None
    for d in drifted_summary:
        if d["category"] != cur_cat:
            cur_cat = d["category"]
            lines.append(f"\n**{cur_cat}**")
        fix = f"  →  fix: `{d['fix_description']}`" if d["fix_description"] else ""
        lines.append(f"- `{d['target']}` — {d['detail']}{fix}")
    body = (
        f"The pod-perms contract has drifted on {n} target"
        f"{'s' if n != 1 else ''}. ensure_pod_perms only runs at "
        f"deploy time; this monitor catches drift between deploys.\n"
        + "\n".join(lines)
        + "\n\nThe canonical cause: a per-bot daemon (running as a bot "
        "user) was the first writer to a shared dir, so the dir got "
        "created with the bot's ownership. With sticky 1777, only the "
        "dir owner can rename foreign files — so cross-user operations "
        "(e.g. dismissing a proposal owned by a different bot daemon) "
        "fail with EACCES.\n\nFix: `sudo evolve-admin ensure-pod-perms`. "
        "Idempotent and surgical — applies only the listed fixes. "
        "After it runs the next tick of this monitor sees clean state "
        "and the Signal auto-resolves."
    )
    return dict(
        signature=make_signature(PRODUCER, "pod_perms_drift", "pod"),
        producer=PRODUCER,
        type="pod_perms_drift",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        bot_id=None,
        title=title,
        body=body,
        details=dict(
            drift_count=n,
            drifted=drifted_summary,
            vector="operations",
            magnitude=3,
            severity_active=True,
            what_it_means=(
                "Filesystem permissions on shared pod state have drifted "
                "from the canonical contract (mode 1777 + owner=evolve "
                "on multi-writer dirs; evolve-allow ACL on shared "
                "subdirs). Cross-user operations through the admin "
                "server start failing once drift accumulates."
            ),
            fix_steps=(
                "1. Run `sudo evolve-admin ensure-pod-perms --check-only` "
                "to preview the exact set of fixes.\n"
                "2. Run `sudo evolve-admin ensure-pod-perms` to apply "
                "(idempotent).\n"
                "3. Wait for the next hourly tick of this monitor — "
                "the Signal auto-resolves when the next pass sees no "
                "drift."
            ),
        ),
    )


def run(
    network_path: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """One pass: check pod perms; emit drift Signal; sweep-resolve.

    Returns a small report dict matching the cascade audit_runner shape
    so the launchd job log reads consistently across the pod's
    Signal-emitting daemons.
    """
    now = now or datetime.now(timezone.utc)

    # Import lazily so this script can boot on a host where the admin
    # package isn't installed yet (early bootstrap; degrades to no-op).
    try:
        from evolve_admin.deploy import ensure_pod_perms, _PermCheck  # type: ignore
        from evolve_admin.config import load_network, get_bot_user  # type: ignore
        from evolve_admin.secret_config_perms import (  # type: ignore
            reassert_pod_evolve_access,
        )
    except ImportError as exc:
        print(
            json.dumps({
                "status": "skipped",
                "reason": f"evolve_admin not importable: {exc}",
            }),
            flush=True,
        )
        return {"checks_run": 0, "drifted": 0, "signals_fired": 0}

    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    # Pod-wide pass — bot_id=None covers the multi-writer dirs that
    # this daemon exists to monitor. Owner/mode drift on these is fixed
    # only by a root chmod/chown apply, which is too heavy to run
    # unattended hourly — so this stays check-only and Signals for the
    # operator to run `ensure-pod-perms`.
    perm_result = ensure_pod_perms(
        bot_id=None,
        network_path=network_path,
        check_only=True,
    )

    drifted = [c for c in perm_result.checks if not c.ok]
    n_checks = len(perm_result.checks)

    # Per-bot evolve-access self-heal (APPLY, not check-only). Distinct from
    # the pod-wide pass above: the OC gateway re-hardens each bot's .openclaw
    # to 0700 on its own runtime ops (gateway restart, every `openclaw`
    # invocation an hourly daemon makes), and on Linux that clamps the POSIX
    # ACL mask → evolve loses read+traverse until the mask is re-widened.
    # There is no single Evolve-side trigger to couple the reassert to, so we
    # re-widen periodically here. The heal uses ONLY evolve's own `setfacl`
    # grant (no root chmod/chown), so applying it every hour is cheap and
    # safe — unlike the pod-wide apply this daemon withholds. macOS is a
    # structural no-op (no ACL mask). A bot that SELF-HEALS produces no
    # Signal (that silence ends the hourly ACL-drift flap); only a bot that
    # is STILL locked out after the escalated heal rides the drift Signal.
    members = network.get("members") or list((network.get("bots") or {}).keys())
    bot_pairs = [(m, get_bot_user(m, network)) for m in members]
    reasserted = reassert_pod_evolve_access(bot_pairs)
    healed = [b for b, (ok, _f) in reasserted.items() if ok]
    unhealed = {b: f for b, (ok, f) in reasserted.items() if not ok}
    for bot_id, failures in unhealed.items():
        drifted.append(_PermCheck(
            category="evolve-access",
            target=f"/home/{bot_id}/.openclaw",
            ok=False,
            detail="; ".join(failures) or "evolve read/traverse not restored",
            fix_description=(
                "evolve still cannot reach .openclaw after the periodic mask "
                "reassert + full re-grant — run `sudo evolve-admin "
                "ensure-pod-perms` and check the bot's sudoers setfacl grants"
            ),
        ))
    n_drifted = len(drifted)

    # Clear side of the inline credential-clamp alert (#3477). The credential
    # reader fires a per-bot ``credential_access`` Signal when its OWN inline
    # heal failed and the store stayed unreadable; that alert needs a periodic
    # sweeper to auto-archive it, and this daemon is the one that already knows,
    # per bot, whether the evolve read/traverse contract holds. Bots still
    # locked out after the reassert keep their Signal; every other bot's clears.
    # Deliberately not resolved from the reader's success path — that would put
    # a Signal-store scan on every credential read. ``checked_bots`` scopes the
    # sweep to the bots this run actually evaluated, so a run with no members
    # cannot mass-resolve the pod's alerts.
    try:
        from credential_access_signal import sweep_resolve_healthy

        cred_resolved = (
            0 if dry_run else sweep_resolve_healthy(
                shared_dir,
                checked_bots=set(reasserted),
                still_unreadable_bots=set(unhealed),
            )
        )
    except Exception as exc:  # noqa: BLE001 — one sweep must not fail the run
        print(f"[pod_perms_drift] credential_access sweep failed: {exc}", flush=True)
        cred_resolved = 0

    if reasserted:
        print(json.dumps({
            "evolve_access_reassert": {
                "bots": len(reasserted),
                "healed": sorted(healed),
                "unhealed": sorted(unhealed),
            },
        }, default=str), flush=True)

    signals_fired = 0
    signals_resolved = 0
    signals_withheld = 0
    kept_signatures: set[str] = set()
    pod_signature = make_signature(PRODUCER, "pod_perms_drift", "pod")
    if drifted:
        signal = _drift_signal(drifted)
        if dry_run:
            kept_signatures.add(signal["signature"])
            print(json.dumps({"would_observe": signal}, default=str), flush=True)
        else:
            # Two-stage transient suppression (spec:
            # docs/spec-transient-signal-suppression-2026-06-23.md):
            #   1. Bring-up settle — withhold this warn-level perms drift while
            #      the pod is still settling (the deploy re-asserts perms then).
            #   2. Flap hysteresis — once settled, require the drift to persist
            #      across N consecutive hourly ticks before paging, so an
            #      ACL-mask re-clamp that auto-restores within the hour never
            #      reaches the Alerts page.
            from signals import flap_gate, settle_gate

            if settle_gate.should_withhold(
                shared_dir, severity="warn", transient=True, now=now
            ):
                signals_withheld = 1
                print(
                    json.dumps(
                        {"withheld": "settle", "signature": signal["signature"]}
                    ),
                    flush=True,
                )
            else:
                verdict = flap_gate.note_observed(
                    shared_dir,
                    signature=signal["signature"],
                    transient=True,
                    type="pod_perms_drift",
                    now=now,
                )
                if verdict.page:
                    # Keep the signature BEFORE observing: if observe() throws
                    # transiently while a Signal is already firing for this
                    # drift, the sweep_resolve below must not archive that live
                    # Signal just because this tick's write failed.
                    kept_signatures.add(signal["signature"])
                    try:
                        signals_store.observe(shared_dir, **signal)
                        signals_fired = 1
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"[pod_perms_drift] observe failed: {exc}",
                            flush=True,
                        )
                else:
                    signals_withheld = 1
                    print(
                        json.dumps(
                            {
                                "withheld": verdict.reason,
                                "signature": signal["signature"],
                            }
                        ),
                        flush=True,
                    )
    elif not dry_run:
        # No drift this tick — reset any in-flight dwell so a later re-clamp
        # starts counting from zero rather than promoting on a stale count.
        from signals import flap_gate

        flap_gate.note_cleared(shared_dir, pod_signature)

    # Sweep-resolve: when this run sees no drift OR the drift footprint
    # has changed enough that the prior signature drops out, archive
    # the stale Signal. signals.store.sweep_resolve handles the
    # types-allowlist + producer scoping.
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                types={"pod_perms_drift"},
                reason="auto-resolve: pod perms drift cleared",
            )
            signals_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[pod_perms_drift] sweep_resolve failed: {exc}",
                flush=True,
            )
        # Reap any flap-dwell ledger entry whose producer stopped observing
        # without a clean tick (crash backstop). Cheap; idempotent.
        try:
            from signals import flap_gate

            flap_gate.sweep_stale(shared_dir, now=now)
        except Exception as exc:  # noqa: BLE001
            print(f"[pod_perms_drift] sweep_stale failed: {exc}", flush=True)

    report = {
        "checks_run": n_checks,
        "drifted": n_drifted,
        "signals_fired": signals_fired,
        "signals_resolved": signals_resolved,
        "credential_access_resolved": cred_resolved,
        "signals_withheld": signals_withheld,
        "ran_at": now.isoformat(),
    }
    print(json.dumps(report, default=str), flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--network",
        default="/Users/Shared/evolve/network.json",
        help="Path to network.json (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Signal that would be observed but don't write it.",
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
