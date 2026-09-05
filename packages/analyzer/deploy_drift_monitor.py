"""deploy_drift_monitor — emit a Signal when bots are running stale Evolve code.

L1 vocabulary note (internal/spec-drift-alert-taxonomy-2026-06-26.md). The
operator-facing word here is **version skew**, not "drift". "Drift" reads as
tampering; this condition is literally Evolve shipping, and the L2
authorized-change gate classifies it as explained by definition
(``drift_authorization.KIND_DEPLOY_VERSION``) — it can never be the security
alert. Only the wording changed: the module name, the producer name and the
Signal ``type`` all stay ``deploy_drift*``, because a type is half of every
Signal signature and renaming it would orphan every stored Signal, dwell
counter and operator snooze keyed on the old one. The catalog event operators
actually subscribe to has been ``updates.version_skew`` since the 2026-06-28
triage, so the surface already reads correctly.

Reads on-disk telemetry only (no LLM):
  - {shared_dir}/install.json   — per-bot deployed version (written by
    ``evolve-admin deploy <bot>``)
  - network.json                — pod membership list
  - evolve_admin.deploy.EVOLVE_VERSION — current admin-server checkout version

One signal type, under producer ``deploy_drift_monitor``:

  deploy_drift         — N member bots have ``deployed_version`` not equal
                         to ``EVOLVE_VERSION``. Pod-scoped. The wording is
                         direction-aware: bots BEHIND the admin server read as
                         "older / out of sync" (warn — the deploy sweep needs
                         to run); bots AHEAD of the admin server read as
                         "newer than admin server" (info — a benign,
                         self-resolving state where the admin daemon hasn't
                         reloaded its own pulled code yet).

Distinct from ``sysadmin_watchdog/version_behind`` (which measures release
lag in days). This monitor catches the gap between admin-side ``git pull``
and the next per-bot ``deploy`` sweep — a clean install on a 7-bot pod can
sit in this state for hours after a PR merge until the operator re-runs
``install-infra-jobs``. The Home narrative already calls this out from
``_statusData.any_bots_out_of_sync``; emitting a Signal makes the same
condition reachable from other surfaces (DM evo, the alert notifier).

Auto-resolves via ``signals.store.sweep_resolve()`` when every member bot
is back in sync.

Pod-scoped (no per-bot fanout). Cheap to run; designed for hourly launchd
cadence.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "deploy_drift_monitor"

# Bots with role=primary report their version from local code rather than
# install.json, so they will always look "out of sync" to this check.
# Mirrors the exclusion in release_notes.min_deployed_version and the
# Home renderer.
_EXCLUDED_ROLES = frozenset({"primary"})

# Evolve version string: ``YYYY.MMDD.PR`` (e.g. ``2026.0623.3187``). The PR
# number is the monotonic component, so a higher tuple == newer code. Mirrors
# release_notes.parse_version; kept local to avoid an analyzer→admin import.
_VERSION_RE = re.compile(r"^(\d{4})\.(\d{4})\.(\d+)$")


def _parse_version(s: str | None) -> tuple[int, int, int] | None:
    """Parse ``YYYY.MMDD.PR`` into a sortable tuple, or None if malformed.

    Returning None means "can't determine direction" — callers fall back to
    treating the bot as behind (the safe action is still "sync it")."""
    if not isinstance(s, str):
        return None
    m = _VERSION_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


# ─────────────────────────────────────────────────────────────────────────────
# Data readers — small enough to inline; isolated for unit-test injection
# ─────────────────────────────────────────────────────────────────────────────


def _read_install_json(shared_dir: Path) -> dict | None:
    """Read {shared_dir}/install.json. Returns None on any failure."""
    path = shared_dir / "install.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


def _current_version() -> str | None:
    """Return the admin-server's EVOLVE_VERSION, or None if evolve_admin
    isn't importable (e.g. running in a test env without it on path)."""
    try:
        from evolve_admin.deploy import EVOLVE_VERSION
        return EVOLVE_VERSION
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Detector — pure function over network + install + current version
# ─────────────────────────────────────────────────────────────────────────────


def detect_deploy_drift(
    network: dict,
    install: dict | None,
    current_version: str | None,
    exempt_bots: set[str] | None = None,
) -> dict | None:
    """Return a Signal-spec dict when one or more member bots are out of sync.

    Returns None when:
      - current_version is unknown (can't compare; refuse to fire)
      - install.json is missing or empty (fresh install — nothing to compare)
      - every member bot is in sync

    Mismatches are classified by DIRECTION:
      - bot BEHIND admin (deployed version < admin version) → "older / out of
        sync" — the per-bot deploy sweep needs to run (warn).
      - bot AHEAD of admin (deployed version > admin version) → "newer than
        admin server" — benign and usually self-resolving (the admin daemon
        hasn't reloaded its own pulled code yet). When this is the *only*
        condition the signal is downgraded to info/activity.
      - unparseable versions (can't prove direction) fall back to "behind",
        since the safe action is still "sync the bot".

    Bots with ``role=primary`` are excluded — their reported version is
    local code, not deploy state.
    """
    if not current_version:
        return None
    if install is None:
        return None
    members = network.get("members") if isinstance(network, dict) else None
    bots_cfg = network.get("bots", {}) if isinstance(network, dict) else {}
    if not isinstance(members, list) or not members:
        members = list(bots_cfg.keys())
    bot_versions = (install or {}).get("bot_versions") or {}

    current_v = _parse_version(current_version)
    out_of_sync: list[dict] = []   # bots BEHIND admin (older deployed version)
    ahead: list[dict] = []         # bots AHEAD of admin (newer deployed version)
    never_deployed: list[str] = []
    for bot_id in members:
        if exempt_bots and bot_id in exempt_bots:
            # Release-canary carve-out: while a candidate soaks, the
            # canary bot runs the candidate version BY DESIGN — firing
            # pod drift for it every soak would be noise (and the
            # lagging-bot sweep is likewise exempted; see
            # spec-state-store-and-deploy-resilience §2.4).
            continue
        role = (bots_cfg.get(bot_id) or {}).get("role", "")
        if role in _EXCLUDED_ROLES:
            continue
        bv = bot_versions.get(bot_id) or {}
        deployed = bv.get("version")
        if not deployed:
            never_deployed.append(bot_id)
            continue
        if deployed == current_version:
            continue
        entry = {"bot_id": bot_id, "deployed_version": deployed}
        deployed_v = _parse_version(deployed)
        if deployed_v is not None and current_v is not None and deployed_v > current_v:
            # Bot is on a NEWER build than the admin server — the inverse of
            # drift. Benign: the admin daemon just hasn't reloaded its own
            # pulled code yet.
            ahead.append(entry)
        else:
            # Older than admin, OR direction is indeterminate (unparseable
            # versions) — treat as behind; the safe action is "sync the bot".
            out_of_sync.append(entry)

    if not out_of_sync and not never_deployed and not ahead:
        return None

    behind_n = len(out_of_sync)
    ahead_n = len(ahead)
    never_n = len(never_deployed)
    affected_total = behind_n + never_n + ahead_n
    # Behind / never-deployed is the action-needed condition; bots ahead of
    # the admin server are a benign, self-resolving state. When AHEAD is the
    # only thing happening, the whole signal is informational.
    action_n = behind_n + never_n
    ahead_only = ahead_n > 0 and action_n == 0

    # Title — one short sentence; the body carries the per-bot detail.
    if behind_n and never_n:
        title = (
            f"{behind_n + never_n} bots out of sync with admin "
            f"({behind_n} behind, {never_n} never deployed)"
        )
    elif behind_n:
        title = (
            f"{behind_n} {'bot is' if behind_n == 1 else 'bots are'} "
            f"running an older Evolve version than the admin server"
        )
    elif never_n:
        title = (
            f"{never_n} {'bot has' if never_n == 1 else 'bots have'} "
            "never been deployed"
        )
    else:  # ahead only
        title = (
            f"{ahead_n} {'bot is' if ahead_n == 1 else 'bots are'} "
            "running a newer Evolve version than the admin server"
        )

    body_lines = [
        f"Admin server is on: `{current_version}`",
        "",
    ]
    if out_of_sync:
        body_lines.append("Out of sync (deployed an older version):")
        for entry in sorted(out_of_sync, key=lambda e: e["bot_id"]):
            body_lines.append(
                f"  - `{entry['bot_id']}`: `{entry['deployed_version']}`"
            )
        body_lines.append("")
    if never_deployed:
        body_lines.append("Never deployed:")
        for bot_id in sorted(never_deployed):
            body_lines.append(f"  - `{bot_id}`")
        body_lines.append("")
    if ahead:
        body_lines.append(
            "Running a newer Evolve version than the admin server:"
            if ahead_only
            else "Also ahead of the admin server (newer deployed version):"
        )
        for entry in sorted(ahead, key=lambda e: e["bot_id"]):
            body_lines.append(
                f"  - `{entry['bot_id']}`: `{entry['deployed_version']}`"
            )
        body_lines.append("")

    if ahead_only:
        body_lines.extend(
            [
                "This is the inverse of the usual version skew and is "
                "usually benign: "
                "the bot deployed a newer build than the admin server is "
                "currently running, which means the admin server hasn't "
                "reloaded its own pulled code yet (it `git pull`s every 15min, "
                "but the running daemon keeps the old module cache until it "
                "restarts). It typically clears on the next admin-server "
                "restart or deploy sweep — no action is usually needed.",
            ]
        )
    else:
        body_lines.extend(
            [
                "To sync the pod:",
                "",
                "```",
                "sudo evolve-admin install-infra-jobs",
                "```",
                "",
                "Or deploy individual bots:",
                "",
                "```",
                "sudo evolve-admin deploy <bot>",
                "```",
            ]
        )
    body = "\n".join(body_lines)

    if ahead_only:
        what_it_means = (
            "One or more member bots are running a NEWER deployed version of "
            "Evolve than the admin server's current code — the inverse "
            "of the usual version skew. This is usually benign: the admin "
            "server pulls "
            "main every 15min but its running daemon keeps the old module "
            "cache until restarted, so right after a deploy the fleet can "
            "briefly be ahead of the admin server. Bots are running fine; "
            "nothing is degraded. It normally clears on the next admin-server "
            "restart."
        )
        fix_steps = (
            "1. Usually no action — it self-resolves on the next admin-server "
            "restart or deploy sweep.\n"
            "2. If it persists, restart the admin-server daemon "
            "(`ai.evolve.evolve.admin-ui`) so it reloads the code it has "
            "already pulled (the stale-module-cache case documented in "
            "CLAUDE.md)."
        )
    else:
        what_it_means = (
            "Member bots are running an older deployed version of "
            "Evolve than the admin server's current code. This is "
            "the normal state for a few hours after a PR merge — "
            "the admin server pulls main every 15min, but per-bot "
            "deploys only happen via the `evolve-admin deploy` "
            "sweep. If this persists past ~1 hour, the deploy "
            "pipeline has stalled. Out-of-sync bots still function "
            "on the older code; only newly-shipped features and "
            "fixes are missing."
        )
        fix_steps = (
            "1. Run the per-bot deploy sweep:\n"
            "   ssh pod_admin_user@mini sudo evolve-admin install-infra-jobs\n"
            "2. Or deploy individual bots (faster if only one is "
            "behind):\n"
            "   ssh pod_admin_user@mini sudo evolve-admin deploy <bot>\n"
            "3. If deploy fails for a specific bot, check "
            "{shared_dir}/install.json for the last attempted "
            "version and the deploy log under "
            "/Users/Shared/evolve/logs/admin-actions.jsonl"
        )

    # Severity framework: operations vector. Magnitude scales with how
    # many bots need action (behind / never deployed) — 1-2 bots = mag 1
    # (skew, advisory), 3+ = mag 2 (pod-wide degradation of "running latest
    # code"). Never active outage — bots still function on the deployed code.
    # An ahead-only condition is purely informational (no action; admin will
    # catch up on its own).
    magnitude = 2 if action_n >= 3 else 1
    return dict(
        signature=make_signature(PRODUCER, "deploy_drift", "pod"),
        producer=PRODUCER,
        type="deploy_drift",
        flavor="activity" if ahead_only else "maintenance",
        severity="info" if ahead_only else "warn",
        scope="pod",
        title=title,
        body=body,
        details=dict(
            current_version=current_version,
            affected_total=affected_total,
            out_of_sync=out_of_sync,
            ahead=ahead,
            never_deployed=never_deployed,
            direction="ahead" if ahead_only else "behind",
            vector="operations",
            magnitude=magnitude,
            what_it_means=what_it_means,
            fix_steps=fix_steps,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    install_loader=_read_install_json,
    version_loader=_current_version,
) -> list[dict]:
    """Run the detector and return Signal specs (zero or one entry)."""
    install = install_loader(shared_dir)
    current = version_loader()
    spec = detect_deploy_drift(
        network, install, current,
        exempt_bots=_canary_exemption(shared_dir, network),
    )
    return [spec] if spec else []


def _canary_exemption(shared_dir: Path, network: dict) -> set[str] | None:
    """Bot(s) expected to run a non-stable version right now: the
    release canary during an active soak. Guarded import — the admin
    package may be absent in stripped test contexts."""
    try:
        from evolve_admin import release_manager as _relmgr
        canary = _relmgr.canary_bot_during_soak(shared_dir, network)
        return {canary} if canary else None
    except Exception:
        return None


def run(
    shared_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    install_loader=_read_install_json,
    version_loader=_current_version,
) -> tuple[set[str], int, int]:
    """Collect, write Signal, sweep-resolve when cleared.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    detections = collect(
        shared_dir,
        config,
        install_loader=install_loader,
        version_loader=version_loader,
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
                f"[deploy_drift_monitor] observe failed for {d['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: version skew cleared on next run",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[deploy_drift_monitor] sweep_resolve failed: {exc}", flush=True)

    return kept, len(detections), n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="deploy_drift_monitor — Signal producer for Evolve version skew",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    kept, n_fired, n_resolved = run(shared_dir, config, dry_run=args.dry_run)
    if args.dry_run:
        print(f"[deploy_drift_monitor] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[deploy_drift_monitor] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
