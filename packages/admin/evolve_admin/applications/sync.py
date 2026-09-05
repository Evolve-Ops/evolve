"""
sync.py — Smart-sync orchestrator for the Applications feature.

[META:apps] Unifies the two co-equal Applications operations into a single
operator action whose cost is proportional to what actually changed:

  - "App Scan"  — disk → manifests, LLM-driven discovery, ~70s, writes.
                  (scanner.scan_workspace_pipeline)
  - "Reflect"   — manifest ↔ disk provenance audit, seconds, read-only.
                  (reflect.reflect)

Running a full LLM scan every time an operator wants to "sync" is wasteful:
the common case is that nothing new appeared on disk and all we need is the
cheap drift audit. So this orchestrator always runs a CHEAP pre-pass
(inventory + reflect — no LLM, no writes) and only escalates to the full
scan when there is on-disk evidence of *undiscovered* apps: code files or
named workspace directories that no manifest's ``realized_files[]`` accounts
for, plus Reflect's own ``orphan_file`` count.

Every result also says WHAT THE SCAN DID, not just what it found (ALPHA-2,
audit finding B2a). The three ``llm_phase`` values are distinct on purpose:

  not_run           the cheap path — no LLM phase was attempted, because none
                    was needed. NOT a degradation; nothing was skipped.
  ran               the full scan ran with a model behind it.
  skipped_degraded  the full scan ran WITHOUT a model (no provider key
                    resolved), so the pass that recognises apps never ran.
  unknown           the scan ran and we could not read its own record of what
                    it did (docs/principle-tri-state-status.md).

Before this, a pod with no resolvable provider key was told "ran full scan;
discovered 0 app(s)" in half a second. The scanner had written ``llm_degraded``
into ``.scan-status.json`` and nothing read it. ``applications.scan_provenance``
is the reader; ``reason`` below now stops claiming a full scan when one did not
happen.

The decision logic lives here (pure, unit-testable); the HTTP plumbing,
flock concurrency guard, and ``.scan-status.json`` status sink live in the
web server (see ``api_applications_sync`` in web/server.py). The full scan is
invoked in-process via ``scanner.scan_workspace_pipeline`` — the same entry
the ``evolve-admin application scan --auto-approve`` CLI uses — so the admin
server (``evolve`` user, owner of the shared gallery) mints native v7-arc
manifests directly. The flock lockfile name is shared with
``/api/applications/scan`` so the two endpoints mutually exclude.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from ..config import bot_home
from ..secret_config_perms import exists_or_unreachable
from .reflect import ReflectResult, _load_bot_instances, reflect
from .scan_provenance import (
    READ_OK,
    STATE_DEGRADED,
    STATE_UNREADABLE,
    classify,
    read_scan_status,
    scan_status_path,
)
from .scanner import (
    EVOLVE_PLATFORM_TREES,
    OC_INFRA_DIRS,
    collect_inventory,
    scan_workspace_pipeline,
)

# Module logger. The orchestrator walks bot ``.openclaw/`` trees and holds an
# fcntl flock; on Linux/Py3.12 those touch points can raise EACCES/OSError, so
# best-effort cleanup paths log rather than swallow silently.
_log = logging.getLogger(__name__)

# Named dirs that are never an "undiscovered app": the per-bot manifest index
# itself plus the infra/platform trees collect_inventory already filters. The
# manifests/ index in particular holds no realized_files, so without this it
# would always read as an uncovered dir and force a needless escalation.
_NAMED_DIR_SKIP = {"manifests"} | set(OC_INFRA_DIRS) | set(EVOLVE_PLATFORM_TREES)

# ── Escalation threshold ──────────────────────────────────────────────────────
#
# How many uncovered code files / named dirs (unioned with Reflect's
# orphan_file count) it takes to escalate from the cheap pre-pass to a full
# LLM scan.
#
# Start CONSERVATIVE at 1: any genuinely uncovered code file is, by
# definition, an app the catalog doesn't know about — exactly the thing a
# full scan exists to discover. The cheap pre-pass already filters out
# infrastructure (collect_inventory drops OC/Evolve platform scripts and
# OC_INFRA_DIRS) and files that ARE covered by a manifest, so a non-zero
# uncovered count is high-signal, not noise. Raising this would trade scan
# cost for the risk of a real app sitting undiscovered until the next daily
# generator sweep — the wrong trade for an operator-triggered "sync".
UNCOVERED_ESCALATION_THRESHOLD = 1

# The four Reflect finding kinds, pinned so ``drift_counts`` always has a
# stable shape (every key present, 0 when absent) for the frontend chip.
_DRIFT_KINDS = ("orphan_file", "missing_marker", "stale_pkg_marker", "missing_disk_file")


# ── Path normalization ────────────────────────────────────────────────────────

def _abs(path_str: str, workspace: Path) -> str:
    """Resolve a manifest/inventory path string to an absolute string.

    ``realized_files[].path`` is absolute when emitted by migrate_v7 and
    relative-to-workspace when emitted by extend_application; inventory
    relpaths are always relative-to-workspace. Resolving both against the
    workspace gives a single comparable key (mirrors recon_ledger._build_expected_files).
    """
    p = Path(path_str)
    if not p.is_absolute():
        p = workspace / p
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _covered_abs_paths(instances: list[dict], workspace: Path) -> set[str]:
    """Absolute paths claimed by ANY Instance's realized_files[]."""
    covered: set[str] = set()
    for inst in instances:
        for rf in inst.get("realized_files") or []:
            if not isinstance(rf, dict):
                continue
            p = rf.get("path") or ""
            if p:
                covered.add(_abs(p, workspace))
    return covered


def compute_uncovered(inventory, instances: list[dict], workspace: Path) -> list[str]:
    """Inventory code files + named dirs that no manifest accounts for.

    "Uncovered" = the file's absolute path is in no Instance's
    realized_files[]; for a named dir, no realized file lives anywhere
    beneath it. Returns workspace-relative paths (dirs carry a trailing
    "/"), sorted and de-duplicated, suitable for the ``uncovered_files``
    field the frontend renders.
    """
    covered = _covered_abs_paths(instances, workspace)

    uncovered: list[str] = []
    # Code files (python + shell) — relpaths straight off the inventory.
    for rel in list(inventory.python_scripts) + list(inventory.shell_scripts):
        if _abs(rel, workspace) not in covered:
            uncovered.append(rel)

    # Named dirs — a whole undiscovered directory tree is evidence of an
    # undiscovered app even when no .py sits at the workspace root (e.g. a
    # data dir + a cron). Count the dir as uncovered when NOTHING beneath
    # it is claimed by a manifest. (collect_inventory already excludes
    # OC/Evolve infra dirs, so this won't fire on memory/, evolve/, etc.)
    for d in inventory.named_dirs:
        name = d.get("name") or ""
        if not name or name in _NAMED_DIR_SKIP:
            continue
        dir_abs = _abs(name, workspace)
        prefix = dir_abs + os.sep
        if not any(c == dir_abs or c.startswith(prefix) for c in covered):
            uncovered.append(name.rstrip("/") + "/")

    # De-dupe while preserving a stable (sorted) order.
    return sorted(set(uncovered))


def _count_manifest_files(bot_id: str) -> int:
    """Count real manifest JSONs in the bot's manifests dir (no hidden/history)."""
    mdir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    # exists_or_unreachable: a bare .is_dir() on a .openclaw path RAISES
    # PermissionError under an EACCES clamp on Linux/Py3.12 (#3184). Treat an
    # unreachable dir as present and let the glob below classify it.
    if not exists_or_unreachable(mdir):
        return 0
    try:
        return sum(
            1 for f in mdir.glob("*.json")
            if not f.name.startswith(".") and not f.name.startswith("_")
            and "_history" not in f.name
        )
    except (PermissionError, OSError) as exc:
        _log.warning("manifest count unavailable for %s: %s", bot_id, exc)
        return 0


def _drift_counts(result: ReflectResult) -> dict[str, int]:
    counts = {k: 0 for k in _DRIFT_KINDS}
    for f in result.findings:
        if f.kind in counts:
            counts[f.kind] += 1
        else:  # forward-compat: a new kind still shows up in the rollup
            counts[f.kind] = counts.get(f.kind, 0) + 1
    return counts


# ── Cheap pre-pass + decision ─────────────────────────────────────────────────

@dataclass
class PrePass:
    """Result of the cheap (no-LLM, read-only) pre-pass for one bot."""
    bot_id: str
    workspace: Path
    inventory: object                      # WorkspaceInventory
    instances: list[dict]
    reflect_result: ReflectResult
    uncovered: list[str] = field(default_factory=list)

    @property
    def drift_counts(self) -> dict[str, int]:
        return _drift_counts(self.reflect_result)


def cheap_prepass(bot_id: str, shared_dir: Path, user: Optional[str] = None) -> PrePass:
    """Run inventory + reflect (no LLM, no writes) and compute uncovered files."""
    workspace = bot_home(bot_id) / ".openclaw" / "workspace"
    inventory = collect_inventory(workspace, bot_id)
    if user:
        inventory.user = user
    instances = _load_bot_instances(bot_id)
    reflect_result = reflect(bot_id, shared_dir)
    uncovered = compute_uncovered(inventory, instances, workspace)
    return PrePass(
        bot_id=bot_id,
        workspace=workspace,
        inventory=inventory,
        instances=instances,
        reflect_result=reflect_result,
        uncovered=uncovered,
    )


def should_escalate(uncovered_count: int, orphan_count: int) -> bool:
    """Escalate to the full LLM scan when on-disk evidence of undiscovered
    apps crosses ``UNCOVERED_ESCALATION_THRESHOLD``.

    The signal is the union of (a) inventory code files / named dirs no
    manifest covers and (b) Reflect's orphan_file findings (a marked file
    no Instance claims — also a "wrote it without extend_application" tell).
    """
    return (uncovered_count + orphan_count) >= UNCOVERED_ESCALATION_THRESHOLD


# ── Full-scan escalation ──────────────────────────────────────────────────────

def run_full_scan(
    bot_id: str,
    workspace: Path,
    shared_dir: Path,
    network_config: dict,
    user: Optional[str] = None,
) -> int:
    """Run the full discovery pipeline in-process; return the count of apps
    newly discovered this run (manifest-file delta).

    Mirrors the ``evolve-admin application scan --auto-approve`` CLI call:
    ``scan_workspace_pipeline`` writes manifests to the bot's
    ``workspace/manifests/`` (output_dir=None → applications_dir resolves
    there) and stamps ``.scan-status.json`` alongside them.
    """
    before = _count_manifest_files(bot_id)
    scan_workspace_pipeline(
        workspace=workspace,
        bot_id=bot_id,
        shared_dir=shared_dir,
        config=network_config,
        use_llm=True,
        user=user,
    )
    after = _count_manifest_files(bot_id)
    return max(0, after - before)


# ── flock concurrency guard (shared with /api/applications/scan) ───────────────

def scan_lockfile(bot_id: str) -> Path:
    """The lockfile path /api/applications/scan also uses — same name so a
    full scan and a sync-triggered escalation mutually exclude on one bot."""
    return Path(tempfile.gettempdir()) / f".evolve-scan-{bot_id}.lock"


@contextmanager
def scan_lock(bot_id: str) -> Iterator[bool]:
    """Non-blocking fcntl.flock guard. Yields True if acquired (caller owns
    the scan), False if another scan already holds it. The kernel releases
    the flock if the admin process dies, so a crash can't leak the lock."""
    lock_file = scan_lockfile(bot_id)
    lock_fh = None
    acquired = False
    try:
        try:
            lock_fh = lock_file.open("a+")
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            try:
                lock_fh.seek(0)
                lock_fh.truncate()
                lock_fh.write(f"{os.getpid()}\nsync\n")
                lock_fh.flush()
            except OSError as exc:
                # Stamping the lock holder's pid is diagnostic-only — the flock
                # itself is already held, so a write failure must not abort.
                _log.debug("scan lock stamp failed for %s: %s", bot_id, exc)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                acquired = False
            else:
                raise
        yield acquired
    finally:
        if lock_fh is not None:
            try:
                lock_fh.close()  # releases the flock
            except OSError as exc:
                _log.debug("scan lock close failed for %s: %s", bot_id, exc)
            if acquired:
                try:
                    lock_file.unlink(missing_ok=True)
                except OSError as exc:
                    # Best-effort cleanup: the kernel already released the flock
                    # on close, so a leftover lockfile is harmless.
                    _log.debug("scan lockfile unlink failed for %s: %s", bot_id, exc)


# ── Unified result assembly ───────────────────────────────────────────────────

# ── What the scan did (ALPHA-2 / audit B2a) ───────────────────────────────────

#: No LLM phase was attempted. The cheap path's honest value — nothing was
#: skipped, so this is NOT a degradation and must never be reported as one.
LLM_PHASE_NOT_RUN = "not_run"
#: The full scan ran with a model behind it.
LLM_PHASE_RAN = "ran"
#: The full scan ran WITHOUT one: no provider key resolved, so the pass that
#: recognises apps never ran and a zero result means nothing.
LLM_PHASE_SKIPPED_DEGRADED = "skipped_degraded"
#: The scan ran and its own record of what it did could not be read.
LLM_PHASE_UNKNOWN = "unknown"


def _scan_provenance_fields(bot_id: str, shared_dir: Path) -> dict:
    """The escalated path's ``llm_*`` fields, read from the scan's own record.

    Called immediately after ``run_full_scan``, so the status file this reads
    is the one that run just wrote. A read failure yields ``unknown`` rather
    than ``ran``: claiming the model phase happened because we could not check
    is the exact shape of the bug this chip exists to remove.
    """
    try:
        status, read_state = read_scan_status(scan_status_path(bot_id, shared_dir))
    except Exception as exc:  # noqa: BLE001 — path resolution crosses config/sudo
        _log.info("sync: scan provenance unavailable for %s: %s", bot_id, exc)
        return {"llm_phase": LLM_PHASE_UNKNOWN, "llm_degraded": False,
                "llm_degraded_reason": None, "llm_degraded_note": None,
                "llm_degraded_remedy": None}
    prov = classify(bot_id, status, read_state)
    if prov.state == STATE_DEGRADED:
        phase = LLM_PHASE_SKIPPED_DEGRADED
    elif prov.state == STATE_UNREADABLE or read_state != READ_OK:
        phase = LLM_PHASE_UNKNOWN
    else:
        phase = LLM_PHASE_RAN
    return {
        "llm_phase": phase,
        "llm_degraded": prov.degraded,
        "llm_degraded_reason": prov.reason,
        "llm_degraded_note": prov.note,
        "llm_degraded_remedy": prov.remedy,
    }


def _result(
    bot_id: str,
    path: str,
    reason: str,
    discovered_count: int,
    uncovered: list[str],
    reflect_result: ReflectResult,
    *,
    already_running: bool = False,
    llm_phase: str = LLM_PHASE_NOT_RUN,
    llm_degraded: bool = False,
    llm_degraded_reason: str | None = None,
    llm_degraded_note: str | None = None,
    llm_degraded_remedy: str | None = None,
) -> dict:
    """The stable JSON shape the frontend chip consumes.

    The ``llm_*`` fields are always present, with the cheap path's honest
    default: no model phase was attempted, nothing was degraded. A caller that
    forgets to pass them therefore under-claims rather than over-claims.
    """
    out = {
        "bot_id": bot_id,
        "path": path,                       # "cheap" | "escalated"
        "reason": reason,
        "discovered_count": discovered_count,
        "uncovered_files": uncovered,
        "drift_findings": [asdict(f) for f in reflect_result.findings],
        "drift_counts": _drift_counts(reflect_result),
        "warnings": list(reflect_result.warnings),
        "llm_phase": llm_phase,
        "llm_degraded": llm_degraded,
        "llm_degraded_reason": llm_degraded_reason,
        "llm_degraded_note": llm_degraded_note,
        "llm_degraded_remedy": llm_degraded_remedy,
    }
    if already_running:
        out["already_running"] = True
    return out


def sync_bot(
    bot_id: str,
    shared_dir: Path,
    network_config: dict,
    *,
    user: Optional[str] = None,
    status_sink: Optional[dict] = None,
) -> dict:
    """Smart-sync one bot: cheap pre-pass, escalate to a full scan only when
    on-disk evidence warrants it, return the unified result dict.

    ``status_sink`` is the web server's in-memory ``_scan_status`` map; when
    provided, the escalated path mirrors running/done status into it so the
    existing ``/api/applications/scan/status`` poller stays meaningful.

    The result always carries ``llm_phase`` (see the module docstring). The
    cheap path's value is ``not_run`` and its ``reason`` makes no claim about a
    scan — the two must stay distinct, because "we did not need to look" and
    "we looked without a model" produce the same zero and mean opposite things.
    """
    pre = cheap_prepass(bot_id, shared_dir, user=user)
    orphan_count = pre.drift_counts.get("orphan_file", 0)

    if not should_escalate(len(pre.uncovered), orphan_count):
        drift_total = len(pre.reflect_result.findings)
        if drift_total:
            reason = (
                f"No undiscovered apps — catalog complete; "
                f"{drift_total} drift finding(s) to review"
            )
        else:
            reason = "No undiscovered apps — catalog complete"
        return _result(bot_id, "cheap", reason, 0, pre.uncovered, pre.reflect_result)

    # Escalate — but never run two scans on one bot at once.
    with scan_lock(bot_id) as acquired:
        if not acquired:
            # Not ``not_run``: a scan IS running, we simply did not run it and
            # cannot say what it will do. ``unknown`` keeps that distinct from
            # the cheap path's "no model phase was needed".
            return _result(
                bot_id, "escalated",
                "A full scan is already running for this bot — returning drift findings only",
                0, pre.uncovered, pre.reflect_result, already_running=True,
                llm_phase=LLM_PHASE_UNKNOWN,
            )

        if status_sink is not None:
            status_sink[bot_id] = {
                "status": "running", "phase": 1, "phase_total": 8,
                "phase_desc": "Smart-sync: full scan…", "eta_seconds": 70,
                "found": 0, "log": "",
            }
        discovered = run_full_scan(
            bot_id, pre.workspace, shared_dir, network_config, user=user,
        )
        # Re-run reflect on the freshly-written manifests so drift findings
        # and the remaining-uncovered set reflect post-scan reality.
        fresh = reflect(bot_id, shared_dir)
        remaining = compute_uncovered(
            pre.inventory, _load_bot_instances(bot_id), pre.workspace,
        )
        if status_sink is not None:
            status_sink[bot_id] = {"status": "done", "count": _count_manifest_files(bot_id)}

        n_trigger = len(pre.uncovered) + orphan_count
        prov = _scan_provenance_fields(bot_id, shared_dir)
        # The headline must not claim a full scan the pod did not run. On the
        # degraded path the structural pass DID run — it just cannot recognise
        # an app — so "discovered 0" is a consequence of the skip, not a
        # finding, and the sentence says which.
        if prov["llm_phase"] == LLM_PHASE_SKIPPED_DEGRADED:
            ran = "ran a structural scan only (no working model, so the part that recognises apps was skipped)"
        elif prov["llm_phase"] == LLM_PHASE_UNKNOWN:
            ran = "ran a scan whose own record could not be read"
        else:
            ran = "ran full scan"
        reason = (
            f"Found {n_trigger} uncovered code file(s)/dir(s) → {ran}; "
            f"discovered {discovered} app(s)"
        )
        return _result(bot_id, "escalated", reason, discovered, remaining, fresh, **prov)
