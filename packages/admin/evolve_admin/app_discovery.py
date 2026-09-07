"""app_discovery — make app discovery something the pod DOES, not something
an operator has to remember to click.

ALPHA-1, findings B1/U7 of ``internal/audit-alpha-journey-2026-08.md``.
Before this module, ``applications.sync.sync_bot`` — the only entry point
that discovers anything — had exactly two callers, both HTTP routes reached
only by a human clicking *Sync all bots* on Apps → Discovered (or by asking
evo). A stranger who installed Evolve onto a pod with months of real history
opened Discovered, found it empty, and it stayed empty forever. There was no
launchd job, no systemd timer, no generator charter, and no deploy-time first
scan.

Two producers live here, and nothing else:

**1. The arrival scan** (:func:`first_scan_on_deploy`) — one ``sync_bot`` per
bot at the end of a successful ``deploy_bot``, ONCE per bot ever, so a
freshly-adopted pod has a populated Discovered queue the first time the
operator opens it. Non-fatal by construction: a scan failure warns into the
deploy log and never fails the deploy (the same posture as
:mod:`evolve_admin.app_cron_map`).

**2. The weekly sweep** (:func:`run_sweep`, installed by
:func:`install_app_discovery_sweep`) — ``sync_bot`` over every bot in
network.json, Sunday 05:15, so a habit that forms in week three still becomes
a draft.

**Why ``sync_bot`` and not ``scan_workspace_pipeline``.** ``sync_bot`` always
runs a cheap no-LLM pre-pass (inventory + reflect) and escalates to the full
LLM scan only when on-disk evidence of undiscovered apps crosses
``sync.UNCOVERED_ESCALATION_THRESHOLD``. Scheduling the full scan directly
would have paid the ~70s LLM cost unconditionally, every week, on every bot,
with no signal at all in front of it.

**Why weekly — and what the escalation arithmetic actually says.** Do NOT
read the pre-pass as "quiet pods are free". ``UNCOVERED_ESCALATION_THRESHOLD``
is **1**, and ``sync.compute_uncovered`` counts every ``.py``/``.sh`` anywhere
under the workspace that no manifest's ``realized_files[]`` claims. A full
scan only mints manifests for what the classifier calls an app, so a scratch
script, a helper, or a one-off shell file stays uncovered *permanently* and
re-triggers escalation on every pass. That is not the degraded-scan pod of
finding B2 being unlucky — it is the ordinary case, and the audit measured it:
3 of 3 fixture bots escalated, all three finding nothing.

So the realistic steady state is one full scan per bot per **pass**, not per
"pod that changed". That is precisely why the cadence is weekly and why the
pre-pass is still worth having: weekly bounds the real cost at ~1 scan per bot
per week (~70s, one scan's tokens, ~$0.0x at discovery-tier rates), daily
would be 7x that for no gain — the signal it keys off does not move day to
day — and the pre-pass still buys the one thing that matters here, which is that a
bot whose workspace holds nothing unclaimed never reaches the LLM at all. (It
is not free — ``cheap_prepass`` walks the workspace, shells ``crontab -l``,
snapshots the scheduler and runs a full ``reflect()`` — but it is seconds and
no tokens.) Per ``feedback_rsi_low_cost_preference``, RSI infra must be cheap;
weekly is the cadence at which this one is.

**Why the arrival scan is once-ever, not every deploy.** The same degraded
pod would otherwise pay a full LLM scan on every ``deploy_bot`` — and the
repo-puller redeploys. The once-ever record (:data:`STATE_FILENAME`) is what
makes a re-deploy free; ``sync_bot``'s cheap pre-pass and its per-bot
``scan_lock`` handle the rest (concurrency, and no re-mint of an app the
catalog already covers). A bot that already carries a scanner
``.scan-status.json`` is treated as already-arrived and never gets an arrival
scan at all, so shipping this to an existing pod does not re-scan its fleet.

Lives outside ``deploy.py`` because that file is size-ratcheted
(tools/file-size-baseline.txt) — the call sites there are one line each.
This module must not import ``deploy`` at import time (deploy imports it);
the deploy-side symbols are imported lazily inside the two functions that
need them, exactly as :mod:`evolve_admin.analyzer_monitor_jobs` does.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from evolve_config import CANONICAL_NETWORK_JSON, CANONICAL_SHARED_DIR
from evolve_util import now_iso

from .config import bot_home, get_bot_user, is_planned_bot_block, load_network

if TYPE_CHECKING:
    from .deploy import DeployResult

logger = logging.getLogger(__name__)

# Pod-wide weekly sweep. Sunday 05:15 local: clear of every other weekly job
# (expansion 04:00, app-posture-review 04:30, weekly-review 03:00,
# weekly-bot-trends 03:30, vocabulary_expander 03:45) and 10 minutes past the
# nearest daily neighbour (model_swap_watch 05:05). It carries no
# StartInterval, so it shares no interval boundary with the fleet — the
# lockstep-fanout hazard jitter exists for does not apply; the small jitter
# below is only a courtesy desync from that 05:05 neighbour.
APP_DISCOVERY_SWEEP_LABEL = "ai.evolve.evolve.app-discovery-sweep"
_SWEEP_SCHEDULE = {"Weekday": 0, "Hour": 5, "Minute": 15}
_SWEEP_JITTER_SECONDS = 300

# Per-bot job state, beside app-cron-map.json under {shared_dir}/{bot_id}/.
# Records ONLY the arrival scan — "did Evolve's first scan of this bot already
# happen, and did it succeed". Scan provenance ("when was this bot last
# scanned, and was that scan degraded") is a different fact with a different
# owner: the scanner's own .scan-status.json and the surface ALPHA-2 builds
# over it. Deliberately not written by the sweep, which runs as `evolve` while
# deploy runs as root or `evolve` — a two-writer file under a per-bot shared
# subdir whose ownership varies is a cross-user rename hazard for no gain.
STATE_FILENAME = "app-discovery.json"

# ``first_scan.ok`` means EXACTLY "sync_bot returned without raising" — not
# "found an app", and NOT "the scan was un-degraded". A pod with no resolvable
# provider key completes structurally and returns 0 (finding B2), and that
# records ok=True here. Making degradation legible is ALPHA-2's surface over
# the scanner's own ``llm_degraded`` flag; this file is job state, not scan
# provenance, and must not grow a second answer to the same question.
MAX_FIRST_SCAN_ATTEMPTS = 3

# Wall-clock the arrival scan may spend across one BURST of deploys.
# `deploy --all` on a freshly-adopted 8-bot pod would otherwise serialize 8
# escalated scans (~70s each; scan_workspace_pipeline's own ceiling is ~6 min)
# into a single foreground run — and the repo-puller's lagging-bot sweep and
# the admin UI's POST /api/deploy both call deploy_bot in-process too. Once the
# budget is spent the remaining bots are DEFERRED, not skipped: they carry no
# first_scan record, so a later deploy picks them up and the weekly sweep
# covers them regardless.
#
# BURST-scoped, deliberately not process-scoped. The obvious implementation —
# a module global that only ever grows — is wrong in the one process that
# outlives a deploy: `ai.evolve.evolve.admin-ui` is a long-running daemon whose
# POST /api/deploy route calls deploy_bot directly, so a process-scoped budget
# would permanently stop arrival scanning after the daemon's first cumulative
# 300s, days or weeks into its uptime, while telling the operator the scan
# would happen "on the next deploy". FIRST_SCAN_BUDGET_IDLE_RESET_SECONDS of
# no scanning starts a new burst; consecutive bots in one `deploy --all` are
# far closer together than that, two separate operator actions are not.
#
# The enforced ceiling is `budget + one scan`, not `budget`: the check is made
# BEFORE a scan, so the bot that crosses the line still runs to completion.
FIRST_SCAN_BUDGET_SECONDS = 300.0
FIRST_SCAN_BUDGET_IDLE_RESET_SECONDS = 600.0

# The admin server runs `app.run(threaded=True)` and drives fleet upgrades from
# background job threads, so this read-modify-write needs the lock.
_budget_lock = threading.Lock()
_budget_spent = 0.0
_budget_last_charge = 0.0


def _budget_admits() -> bool:
    """Is there room in the current burst for one more arrival scan?"""
    global _budget_spent
    with _budget_lock:
        if (
            _budget_last_charge
            and time.time() - _budget_last_charge > FIRST_SCAN_BUDGET_IDLE_RESET_SECONDS
        ):
            _budget_spent = 0.0          # a new burst
        return _budget_spent < FIRST_SCAN_BUDGET_SECONDS


def _budget_charge(seconds: float) -> None:
    """Bill a completed (or failed) scan against the current burst."""
    global _budget_spent, _budget_last_charge
    with _budget_lock:
        _budget_spent += max(0.0, seconds)
        _budget_last_charge = time.time()


# ── Arrival-scan state ────────────────────────────────────────────────────────

def state_path(shared_dir: Path | str, bot_id: str) -> Path:
    """Where this bot's arrival-scan record lives."""
    return Path(shared_dir) / bot_id / STATE_FILENAME


def read_state(shared_dir: Path | str, bot_id: str) -> dict:
    """The bot's arrival-scan record ({} when absent, unreadable or corrupt)."""
    try:
        data = json.loads(state_path(shared_dir, bot_id).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(shared_dir: Path | str, bot_id: str, state: dict) -> bool:
    """Atomically write the record, mode pinned 0644. Best-effort.

    The 0644 pin is not cosmetic: ``mkstemp`` creates 0600 and ``os.replace``
    carries that mode onto the destination, which would make a record written
    by one writer unreadable to the other (deploy runs as root or as `evolve`
    depending on whether the operator used the CLI or the admin UI).
    """
    dest_dir = Path(shared_dir) / bot_id
    tmp = ""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest_dir), prefix=".app-discovery-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, state_path(shared_dir, bot_id))
        return True
    except OSError as exc:
        logger.warning("app-discovery state write failed for %s: %s", bot_id, exc)
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError as exc:
                logger.debug("app-discovery tmp cleanup: %s", exc)  # gone after replace


def _has_scanner_status(bot_id: str, network: Optional[dict] = None) -> bool:
    """True when the scanner has already stamped this bot's manifests dir.

    The second, ownership-independent arrival gate. An existing pod whose bots
    were scanned before this module shipped carries ``.scan-status.json``
    under ``workspace/manifests/`` — treating that as "Evolve already arrived
    here" is what keeps this change from re-scanning a whole live fleet on the
    deploy that introduces it.

    Unreachable reads as *not scanned* — deliberately the OPPOSITE of
    ``secret_config_perms.exists_or_unreachable``, which the ``.openclaw``
    EACCES convention normally points at. That helper answers "does this
    exist?" and treats an unreachable path as present so a clamped mask cannot
    make a real file look absent. Here the question is inverted: this is a
    SUPPRESSION gate, and reading unreachable as "already scanned" would
    silently restore the B1 defect on exactly the platform where the clamp
    bites (Linux/Py3.12, #3184) — the arrival scan would never run and no one
    would see why. Attempting instead costs at most
    :data:`MAX_FIRST_SCAN_ATTEMPTS` failures, and the scan itself runs as
    ``evolve``, which holds the read ACL.
    """
    try:
        home = bot_home(bot_id, network)
    except Exception as exc:  # noqa: BLE001 — a suppression gate must not raise
        # Not an OSError: bot_home falls back to load_network(), which raises
        # RuntimeError on a corrupt network.json.
        logger.debug("app-discovery: bot_home unresolved for %s: %s", bot_id, exc)
        return False
    try:
        return (home / ".openclaw" / "workspace" / "manifests" / ".scan-status.json").is_file()
    except (PermissionError, OSError) as exc:
        logger.debug("app-discovery: scan-status probe failed for %s: %s", bot_id, exc)
        return False


def first_scan_needed(
    shared_dir: Path | str, bot_id: str, network: Optional[dict] = None,
) -> tuple[bool, str]:
    """Should ``bot_id`` get its arrival scan? Returns (needed, why-not).

    Pass ``network`` when you already hold one: ``bot_home`` needs it to
    resolve a bot whose macOS account name differs from its bot id, and
    self-loading would answer from ``DEFAULT_NETWORK_CONFIG`` rather than the
    network the caller resolved against. That buys consistency with the
    CALLER, not with the scan — ``sync.cheap_prepass`` self-loads too. The two
    agree in production (``deploy_bot`` is always handed the canonical path,
    and the sweep unit sets ``EVOLVE_NETWORK`` to it), so this closes the gate
    side of a divergence that has no live trigger today.
    """
    state = read_state(shared_dir, bot_id)
    scan = state.get("first_scan")
    if isinstance(scan, dict):
        if scan.get("ok"):
            return False, "arrival scan already ran"
        attempts = scan.get("attempts")
        attempts = attempts if isinstance(attempts, int) else 0
        if attempts >= MAX_FIRST_SCAN_ATTEMPTS:
            return False, f"arrival scan failed {attempts}x — not retrying"
    if _has_scanner_status(bot_id, network):
        return False, "already scanned before Evolve tracked arrival"
    return True, ""


# ── 1. The arrival scan (deploy call site) ────────────────────────────────────

def _record(
    shared_dir: Path, bot_id: str, result: "DeployResult",
    state: dict, first_scan: dict,
) -> None:
    """Persist the arrival-scan record, and SAY SO when it cannot be persisted.

    The record is what bounds the work: without it ``first_scan_needed`` says
    yes again on the next deploy, so both the once-ever guarantee and
    :data:`MAX_FIRST_SCAN_ATTEMPTS` quietly stop bounding anything. That has a
    real trigger — ``sudo evolve-admin deploy`` (root) can be the first writer
    under ``{shared_dir}/{bot_id}/``, after which an admin-UI deploy running as
    ``evolve`` cannot create a temp file there. A ``logger.warning`` alone is
    invisible in the deploy output an operator reads, so this warns into
    ``result`` too.
    """
    state["first_scan"] = first_scan
    if not _write_state(shared_dir, bot_id, state):
        result.log(
            f"[warn] App discovery: could not record the first scan of {bot_id} "
            f"under {shared_dir / bot_id} — the next deploy will scan it again. "
            f"Check the directory is writable by the deploying user."
        )


def first_scan_on_deploy(
    bot_id: str,
    network_path: Path | str,
    result: "DeployResult",
    *,
    pod_side_effects: bool = True,
) -> bool:
    """Run this bot's ONE arrival scan at the end of a successful deploy.

    Returns True when a scan ran this call, False when it was skipped,
    deferred or failed. Never raises: discovery is observation-layer work, so
    a failure warns into the deploy log and the deploy stands (same contract
    as ``app_cron_map.merge_app_cron_map``).

    ``pod_side_effects=False`` (a release-canary deploy) skips entirely — a
    candidate build must not spend a full LLM scan, or mint drafts an operator
    will see, before it has been promoted.

    Bounded by :data:`FIRST_SCAN_BUDGET_SECONDS` across a burst of deploys, so
    a multi-bot ``deploy --all`` cannot turn into a half-hour foreground scan
    queue. A bot deferred by the budget writes NO record and is picked up by a
    later deploy or the weekly sweep. On the synchronous
    ``POST /api/deploy`` route this adds at most one scan to a request that
    already runs a whole ``deploy_bot``.
    """
    if not pod_side_effects:
        return False
    try:
        network = load_network(Path(network_path))
        shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))
        needed, why_not = first_scan_needed(shared_dir, bot_id, network)
        if not needed:
            logger.debug("app-discovery: %s skipped (%s)", bot_id, why_not)
            return False
        if not _budget_admits():
            result.log(
                f"App discovery: first scan of {bot_id} deferred — this run of "
                f"deploys already spent {_budget_spent:.0f}s scanning "
                f"(budget {FIRST_SCAN_BUDGET_SECONDS:.0f}s). It will run on a "
                f"later deploy, or in the weekly sweep."
            )
            return False

        state = read_state(shared_dir, bot_id)
        prior = state.get("first_scan")
        attempts = (prior.get("attempts") if isinstance(prior, dict) else 0) or 0
        attempts = (attempts if isinstance(attempts, int) else 0) + 1

        result.log(f"App discovery: first scan of {bot_id} (Evolve has not scanned it before)")
        started = time.time()
        try:
            from .applications.sync import sync_bot
            sync = sync_bot(
                bot_id, shared_dir, network, user=get_bot_user(bot_id, network),
            )
        except Exception as exc:  # noqa: BLE001 — must never fail the deploy
            _budget_charge(time.time() - started)
            logger.warning("app-discovery first scan failed for %s: %s", bot_id, exc)
            result.log(
                f"[warn] App discovery: first scan of {bot_id} failed "
                f"(attempt {attempts}/{MAX_FIRST_SCAN_ATTEMPTS}): {exc}"
            )
            _record(shared_dir, bot_id, result, state, {
                "ok": False, "attempts": attempts,
                "at": now_iso(), "error": f"{type(exc).__name__}: {exc}",
            })
            return False

        elapsed = time.time() - started
        _budget_charge(elapsed)
        _record(shared_dir, bot_id, result, state, {
            "ok": True,
            "attempts": attempts,
            "at": now_iso(),
            "path": sync.get("path", ""),
            "reason": sync.get("reason", ""),
            "discovered_count": sync.get("discovered_count", 0),
            "elapsed_seconds": round(elapsed, 1),
        })
        result.log(
            f"App discovery: first scan of {bot_id} done in {elapsed:.0f}s — "
            f"{sync.get('reason', 'no reason recorded')}"
        )
        return True
    except Exception as exc:  # noqa: BLE001 — belt and braces around the whole hook
        logger.warning("app-discovery first-scan hook raised for %s: %s", bot_id, exc)
        try:
            result.log(f"[warn] App discovery: first-scan hook skipped for {bot_id}: {exc}")
        except Exception as log_err:  # pragma: no cover — result.log is a list append
            logger.debug("app-discovery result.log failed: %s", log_err)
        return False


# ── 2. The weekly sweep ───────────────────────────────────────────────────────

def sweep_bot_ids(network: dict[str, Any]) -> list[str]:
    """The bots the sweep visits — the same set *Sync all bots* visits.

    ``network["bots"]`` keys, matching ``POST /api/applications/sync/pod``, so
    the scheduled sweep and the operator's button cover the same pod. Blocks
    for bots that are declared but not yet created are dropped: they have no
    workspace, so a scan can only raise.
    """
    bots = network.get("bots")
    if not isinstance(bots, dict):
        return []
    return [
        bot_id for bot_id, cfg in bots.items()
        if isinstance(bot_id, str) and bot_id and not is_planned_bot_block(cfg)
    ]


def run_sweep(
    network_path: Path | str = CANONICAL_NETWORK_JSON,
    *,
    only_bot: Optional[str] = None,
    log=print,
) -> dict:
    """One weekly sweep: ``sync_bot`` over every bot, rolled up.

    Per-bot exceptions are captured into the rollup rather than aborting the
    sweep — one unreadable workspace must not cost the pod its discovery pass.
    Returns ``{bots, escalated, discovered, errors, results}``.
    """
    from .applications.sync import sync_bot

    network = load_network(Path(network_path))
    shared_dir = Path(network.get("sharedDir", str(CANONICAL_SHARED_DIR)))
    bot_ids = sweep_bot_ids(network)
    if only_bot:
        bot_ids = [b for b in bot_ids if b == only_bot]

    results: list[dict] = []
    escalated = discovered = errors = 0
    for bot_id in bot_ids:
        try:
            res = sync_bot(
                bot_id, shared_dir, network, user=get_bot_user(bot_id, network),
            )
        except Exception as exc:  # noqa: BLE001 — one bad bot must not end the sweep
            errors += 1
            logger.warning("app-discovery sweep failed for %s: %s", bot_id, exc)
            log(f"[app_discovery] {bot_id}: ERROR {type(exc).__name__}: {exc}")
            results.append({"bot_id": bot_id, "error": str(exc)})
            continue
        if res.get("path") == "escalated":
            escalated += 1
        discovered += int(res.get("discovered_count") or 0)
        log(f"[app_discovery] {bot_id}: {res.get('path')} — {res.get('reason')}")
        results.append(res)

    rollup = {
        "bots": len(bot_ids),
        "escalated": escalated,
        "discovered": discovered,
        "errors": errors,
        "results": results,
    }
    # One summary line per run, always — a sweep that visited zero bots is a
    # finding, not silence.
    log(
        f"[app_discovery] sweep done — bots={len(bot_ids)} escalated={escalated} "
        f"discovered={discovered} errors={errors}"
    )
    return rollup


def install_app_discovery_sweep(result: "DeployResult") -> None:
    """Install the weekly discovery sweep (Sunday 05:15, evolve user).

    Goes through the Scheduler seam (``JobSpec`` → ``get_scheduler().install``)
    like every other daemon Evolve owns, so a Linux pod materializes a systemd
    timer and macOS a launchd plist from the same spec. Runs as ``evolve``: it
    reads each bot's ``.openclaw/`` through the read ACL and writes manifests
    through the ``workspace/manifests`` write ACL, exactly as the admin
    server's own sync route does.
    """
    from .deploy import ANALYZER_DIR, VENV_PYTHON, _install_spec_via_seam, _user_home
    from .runtime import JobSpec

    log_dir = _user_home("evolve") / ".openclaw/logs"
    _install_spec_via_seam(
        JobSpec(
            label=APP_DISCOVERY_SWEEP_LABEL,
            program_args=[
                str(VENV_PYTHON), "-m", "evolve_admin.app_discovery",
                "--network", str(CANONICAL_NETWORK_JSON),
            ],
            user="evolve",
            run_at_load=False,
            start_calendar=dict(_SWEEP_SCHEDULE),
            stdout_path=str(log_dir / "evolve-app-discovery-sweep.log"),
            stderr_path=str(log_dir / "evolve-app-discovery-sweep.err.log"),
            env={
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": str(ANALYZER_DIR),
                "EVOLVE_NETWORK": str(CANONICAL_NETWORK_JSON),
            },
            jitter_seconds=_SWEEP_JITTER_SECONDS,
        ),
        result,
    )


# ── Entry point (``python -m evolve_admin.app_discovery``) ────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly app-discovery sweep (ALPHA-1 / B1).")
    ap.add_argument("--network", default=str(CANONICAL_NETWORK_JSON))
    ap.add_argument("--bot", default=None, help="sweep one bot only (debugging)")
    args = ap.parse_args(argv)
    try:
        run_sweep(args.network, only_bot=args.bot)
    except Exception as exc:  # noqa: BLE001
        print(f"[app_discovery] sweep aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised by the launchd/systemd unit
    raise SystemExit(main())
