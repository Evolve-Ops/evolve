"""evolve_admin.recovery — Panic-button pause-all + per-bot rollback.

Spec: docs/spec-mvp-sprint-b2e-recovery.md (sprint pillar B2.e).

This module is the load-bearing safety surface for the MVP sprint's
flagship "vigilant by default, friendly by design" claim. Two
capabilities, both reversible, neither destructive:

  (1) PAUSE-ALL — disable every bot gateway within ~30 seconds; bot
      state is preserved; "resume-all" reverses it. Used as a panic
      button when something is going wrong pod-wide.

  (2) ROLLBACK <bot> <target> — revert a bot's openclaw.json to a
      previous state, drawing on the daily git-backup commits already
      written by ``packages/analyzer/backup.py`` into each bot's own
      ``.openclaw/workspace`` repo. Restarts the bot's gateway after
      writing the reverted config. Every rollback snapshots the
      *current* config first, so the rollback itself can be reversed.

Design discipline (per the high-stakes self-review framing):

  - The pause flag at ``{shared_dir}/recovery/pause-state.json`` is the
    SOURCE OF TRUTH. We write it BEFORE attempting to stop gateways, so
    even if launchctl partially fails, downstream monitors (heal.py,
    the dashboard) see "paused" and don't fight us by restarting bots.

  - All state writes are atomic (tmp-file + os.replace) — the flag must
    never be half-written when a reader checks it.

  - The pause-log JSONL is append-only and immutable; every state
    transition gets a row with timestamp + actor + bots + per-bot
    launchctl rc/stderr. Operators can audit "who panicked when".

  - Rollback writes the bot's openclaw.json via the canonical
    ``/tmp staging + sudo /bin/cp`` pattern used by apply.py — we
    never invent a new write path that bypasses the sudoers grants.

  - ``dry_run=True`` everywhere returns a fully-populated result
    WITHOUT touching anything mutable. Tests and the CLI ``--dry-run``
    flag rely on this.

  - Concurrent state writers are accepted: the last writer wins, but
    the pause-log preserves the history. We do NOT implement file
    locking; that would invite deadlocks on the small "evolve" service
    user, and the operational window where two operators race the
    panic button simultaneously is vanishingly small.

Failure modes flagged in self-review.md:

  - launchctl bootout returning rc=0 while the gateway process keeps
    running (orphaned PID). We log stderr verbatim and surface it in
    the result; heal.py will catch a continuing-up gateway as a drift
    signal on its next 5-min cycle.

  - git show returning empty for a stale commit (e.g. the bot was
    rotated and the prior commit's evolve-backup/openclaw.json was
    removed). We reject the rollback with a clear error rather than
    write empty bytes.

  - The shared-dir recovery/ subdir not existing. We mkdir on first
    write — every entry point ensures the directory exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso_micro as _now_iso
from platform_profile import get_profile

from .config import (
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_SHARED_DIR,
    bot_home as _bot_home,
    get_bot_user,
    load_network,
)
from .runtime import LaunchdScheduler, get_scheduler

# Per-bot launchctl calls are bounded by the Scheduler seam runner's
# timeout ceiling (30s) so a wedged daemon can't block the pause-all
# critical path indefinitely; bots are dispatched in parallel, so the
# ~30-sec pause-all SLA holds even when one daemon hangs at the ceiling.
# On timeout the bot is marked "pause attempted, may still be running"
# and the operator follows up — the flag file is already written so
# heal.py won't fight us.

# Concurrency for pause-all gateway-stop dispatch. 7 bots * <1 sec each
# is well under the 30-sec SLA serially, but we parallelize anyway so
# one slow bot doesn't slow the others. Conservative cap so we don't
# fork-bomb sudo on small hosts.
_PAUSE_MAX_WORKERS = 8

# Max age (days) of backup commits we show as rollback points. 30 days
# matches the de-facto retention on most operators' workspace-backup
# remotes; we don't enforce, just default to keep the UI tidy.
_DEFAULT_ROLLBACK_HORIZON_DAYS = 30


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class PerBotResult:
    """Outcome of a single-bot pause/resume operation."""

    bot_id: str
    label: str
    ok: bool
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: int = 0
    skipped: bool = False
    skip_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PauseResult:
    """Outcome of a pause-all or resume-all operation."""

    action: str            # "pause-all" | "resume-all"
    ok: bool
    initiated_by: str
    reason: str
    started_at: str
    finished_at: str
    elapsed_ms: int
    per_bot: list[PerBotResult] = field(default_factory=list)
    state_before: dict | None = None
    state_after: dict | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["per_bot"] = [b.to_dict() if not isinstance(b, dict) else b for b in self.per_bot]
        return d


@dataclass
class RollbackPoint:
    """One candidate rollback target in a bot's workspace git history."""

    bot_id: str
    commit_sha: str
    commit_date: str        # ISO 8601 UTC
    commit_date_local: str  # human-readable
    summary: str
    has_openclaw_json: bool
    is_backup_commit: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RollbackResult:
    """Outcome of a per-bot rollback."""

    rollback_id: str
    bot_id: str
    target_commit: str
    started_at: str
    finished_at: str
    ok: bool
    message: str
    pre_rollback_config_path: str = ""
    post_rollback_config_path: str = ""
    gateway_restart_ok: bool | None = None
    gateway_restart_msg: str = ""
    dry_run: bool = False
    reversed_from_rollback_id: str = ""   # set when this IS a reverse-rollback
    reversed_by_rollback_id: str = ""     # set when this rollback HAS been reversed

    def to_dict(self) -> dict:
        return asdict(self)


# ── Time helpers ─────────────────────────────────────────────────────────────


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ── Path resolution ──────────────────────────────────────────────────────────


def _resolve_shared_dir(shared_dir: Path | None) -> Path:
    return Path(shared_dir) if shared_dir else DEFAULT_SHARED_DIR


def recovery_dir(shared_dir: Path | None = None) -> Path:
    """Return the recovery state directory, creating it on demand.

    Lives under ``{shared_dir}/recovery/`` which is owned by the evolve
    user (per CLAUDE.md "shared_dir is writable by evolve"), so no
    sudo or /tmp staging is needed for state writes."""
    p = _resolve_shared_dir(shared_dir) / "recovery"
    p.mkdir(parents=True, exist_ok=True)
    (p / "rollbacks").mkdir(parents=True, exist_ok=True)
    return p


def pause_state_path(shared_dir: Path | None = None) -> Path:
    return recovery_dir(shared_dir) / "pause-state.json"


def _pause_log_path(shared_dir: Path | None = None) -> Path:
    return recovery_dir(shared_dir) / "pause-log.jsonl"


def _rollback_dir(shared_dir: Path | None = None) -> Path:
    return recovery_dir(shared_dir) / "rollbacks"


# ── Atomic write helpers ─────────────────────────────────────────────────────


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON record (one-line) to a JSONL log.

    POSIX guarantees a single write() of <PIPE_BUF bytes is atomic; our
    records are small JSON objects so concurrent writers won't interleave
    in practice. Worst case is a malformed line that a parser skips —
    acceptable for an audit log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    # Open in append mode; O_APPEND makes the seek-to-end + write atomic.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


# ── Pause state read ─────────────────────────────────────────────────────────


def read_pause_state(shared_dir: Path | None = None) -> dict | None:
    """Return the current pause-state dict, or None if not paused.

    Defensive: a corrupt or unparseable file returns None (treat as
    "not paused") rather than raising — same fail-open philosophy as
    tool_suspend.guard()."""
    p = pause_state_path(shared_dir)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        if not data.get("paused"):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def is_paused(shared_dir: Path | None = None) -> bool:
    return read_pause_state(shared_dir) is not None


# ── Bot iteration ────────────────────────────────────────────────────────────


def _iter_bots(network: dict) -> list[tuple[str, str]]:
    """Yield (bot_id, os_user) pairs for every bot in the pod.

    Includes primary + members + any other bot with config in
    ``network.bots`` (mirrors backup.py's iteration). Dedupes; preserves
    insertion order."""
    bots_cfg = network.get("bots") or {}
    members = network.get("members") or []
    primary = network.get("primary")
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(b: str | None) -> None:
        if b and b not in seen:
            seen.add(b)
            ordered.append(b)

    _add(primary)
    for m in members:
        _add(m)
    if isinstance(bots_cfg, dict):
        for b in bots_cfg.keys():
            _add(b)

    out: list[tuple[str, str]] = []
    for bot_id in ordered:
        user = get_bot_user(bot_id, network)
        out.append((bot_id, user))
    return out


def _gateway_label(bot_id: str) -> str:
    """Return the launchd label for a bot's gateway daemon.

    Mirrors deploy.py's ``install_bot_gateway_plist`` (label =
    ``ai.openclaw.{bot_id}-gateway``). The evolve user's sudoers grant
    is scoped to ``ai.openclaw.*`` so this label is bootout-able from
    the admin server context."""
    return f"ai.openclaw.{bot_id}-gateway"


def _gateway_plist_path(bot_id: str) -> Path:
    return Path(f"/Library/LaunchDaemons/{_gateway_label(bot_id)}.plist")


def _launchctl_n(*args: str) -> tuple[int, str, str]:
    """Run one launchctl verb through the Scheduler seam with ``sudo -n``;
    returns ``(rc, stdout, stderr)``.

    Derives a non-interactive system-domain adapter from the active seam,
    sharing its runner, so a test-injected
    ``set_scheduler(LaunchdScheduler(runner=fake))`` is honored — tests
    must never spawn a real launchctl here (bootout/kickstart are
    live-traffic destructive). ``sudo -n`` (vs the seam's default plain
    ``sudo``) is load-bearing: these paths run in daemon context with no
    TTY and must fail fast instead of blocking on a password prompt.

    The seam's runner never raises — timeouts and OS errors come back as
    rc=1 with the error text on stderr, so callers classify from the
    tuple alone. No-op success ``(0, "", "")`` when a non-launchd
    Scheduler is injected (pure-protocol fake / future port), matching
    ``deploy._scheduler_launchctl``.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        return 0, "", ""
    nsched = LaunchdScheduler(
        domain=sched.domain,
        plist_dir=sched.plist_dir,
        use_sudo=True,
        sudo_non_interactive=True,
        runner=sched._runner,  # share the seam's (possibly injected) runner
        unload_timeout=sched.unload_timeout,
    )
    return nsched.raw(*args)


# ── Pause-all / resume-all ───────────────────────────────────────────────────


def _bootout_gateway(bot_id: str, dry_run: bool) -> PerBotResult:
    """Stop one bot's gateway via ``sudo launchctl bootout``.

    Reversible: ``_bootstrap_gateway`` brings it back. The evolve user
    has a NOPASSWD sudoers grant for
    ``/bin/launchctl bootout system/ai.openclaw.*``.

    On failure we return ok=False with the rc + stderr; we never raise.
    The caller decides what to do (the pause-all critical path
    continues so other bots still get paused; the failure is logged in
    the per-bot result)."""
    label = _gateway_label(bot_id)
    t0 = time.monotonic()

    if dry_run:
        return PerBotResult(
            bot_id=bot_id,
            label=label,
            ok=True,
            rc=0,
            stdout="(dry-run)",
            stderr="",
            elapsed_ms=_elapsed_ms(t0),
        )

    # raw (not Scheduler.remove()): pause must stop the gateway but KEEP
    # the plist on disk so resume-all can re-bootstrap it — remove()
    # deletes the plist. Timeouts/OS errors surface as rc=1 + stderr
    # text (the seam runner never raises).
    rc, out, err = _launchctl_n("bootout", f"system/{label}")

    # bootout returns:
    #   0       — gateway was running, now stopped
    #   36      — "Could not find specified service" (already stopped)
    #             We treat this as a SUCCESS (the goal state is reached),
    #             but tag it for visibility.
    skipped = (rc == 36)
    ok = (rc == 0) or skipped
    return PerBotResult(
        bot_id=bot_id, label=label, ok=ok, rc=rc,
        stdout=(out or "").strip(),
        stderr=(err or "").strip(),
        elapsed_ms=_elapsed_ms(t0),
        skipped=skipped,
        skip_reason="already stopped" if skipped else "",
    )


def _bootstrap_gateway(bot_id: str, dry_run: bool) -> PerBotResult:
    """Re-start one bot's gateway via ``sudo launchctl bootstrap``.

    Reverses _bootout_gateway. Evolve sudoers grants:
    ``/bin/launchctl bootstrap system /Library/LaunchDaemons/ai.openclaw.*.plist``.

    If the plist is missing (bot was removed while paused), we return
    skipped=True so resume-all doesn't fail wholesale.

    rc=37 ("service already loaded") is treated as success."""
    label = _gateway_label(bot_id)
    plist = _gateway_plist_path(bot_id)
    t0 = time.monotonic()

    if dry_run:
        return PerBotResult(
            bot_id=bot_id, label=label, ok=True, rc=0,
            stdout="(dry-run)", stderr="", elapsed_ms=_elapsed_ms(t0),
        )

    if not plist.exists():
        return PerBotResult(
            bot_id=bot_id, label=label, ok=True, rc=None,
            stdout="", stderr="",
            skipped=True,
            skip_reason=f"plist not found at {plist}",
            elapsed_ms=_elapsed_ms(t0),
        )

    # raw (not Scheduler.install()): resume must re-register the EXISTING
    # deploy-owned plist verbatim — install() owns the plist render+write
    # from a JobSpec and skips byte-identical content, both wrong here.
    rc, out, err = _launchctl_n("bootstrap", "system", str(plist))

    # rc 37 = "Service is already loaded", which after a normal bootout
    # cycle should not happen, but if it does, the goal state is met.
    already = (rc == 37)
    ok = (rc == 0) or already
    return PerBotResult(
        bot_id=bot_id, label=label, ok=ok, rc=rc,
        stdout=(out or "").strip(),
        stderr=(err or "").strip(),
        elapsed_ms=_elapsed_ms(t0),
        skipped=already,
        skip_reason="already loaded" if already else "",
    )


def _dispatch_per_bot(
    bots: list[tuple[str, str]],
    fn,
    dry_run: bool,
) -> list[PerBotResult]:
    """Run fn(bot_id, dry_run) for each bot in parallel; preserve order."""
    if not bots:
        return []
    results: dict[str, PerBotResult] = {}
    workers = min(_PAUSE_MAX_WORKERS, max(1, len(bots)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, bot_id, dry_run): bot_id for bot_id, _user in bots}
        for fut in as_completed(futures):
            bot_id = futures[fut]
            try:
                results[bot_id] = fut.result()
            except Exception as e:
                # Catastrophic — shouldn't happen since fn catches its own
                # errors, but be defensive: report it as a failed result
                # rather than letting the executor swallow it.
                results[bot_id] = PerBotResult(
                    bot_id=bot_id,
                    label=_gateway_label(bot_id),
                    ok=False, rc=None, stderr=f"unhandled: {type(e).__name__}: {e}",
                )
    # Re-order to match input
    return [results[bot_id] for bot_id, _ in bots]


def pause_all(
    *,
    reason: str = "operator pause",
    initiated_by: str = "operator",
    shared_dir: Path | None = None,
    network: dict | None = None,
    dry_run: bool = False,
) -> PauseResult:
    """Disable all bot gateways within ~30 seconds.

    Order matters:
      1) Write the pause-state flag FIRST so any monitor that polls it
         sees paused=true even if step 2 partially fails.
      2) Dispatch ``launchctl bootout`` in parallel for every bot.
      3) Append an audit row to pause-log.jsonl.
      4) Return a result with per-bot outcomes.

    Idempotent: calling pause_all when already paused is fine — the
    flag is overwritten with a fresh timestamp/reason and any
    gateways still running are bootout'd again.
    """
    if network is None:
        network = load_network()

    started_at = _now_iso()
    t0 = time.monotonic()

    state_before = read_pause_state(shared_dir)
    state_after = {
        "paused": True,
        "paused_at": started_at,
        "initiated_by": initiated_by,
        "reason": reason,
    }

    # Step 1: write the flag first (atomic) — even if launchctl fails,
    # downstream callers see "paused".
    if not dry_run:
        _atomic_write_json(pause_state_path(shared_dir), state_after, sort_keys=True)

    # Step 2: bootout every gateway in parallel
    bots = _iter_bots(network)
    per_bot = _dispatch_per_bot(bots, _bootout_gateway, dry_run)

    finished_at = _now_iso()
    elapsed = _elapsed_ms(t0)
    overall_ok = all(r.ok for r in per_bot) and bool(per_bot or not bots)
    # If there are zero bots in the pod, "ok" is vacuously true.
    if not bots:
        overall_ok = True

    # Step 3: audit log
    if not dry_run:
        _append_jsonl(_pause_log_path(shared_dir), {
            "action": "pause-all",
            "timestamp": finished_at,
            "initiated_by": initiated_by,
            "reason": reason,
            "elapsed_ms": elapsed,
            "ok": overall_ok,
            "bots": [r.to_dict() for r in per_bot],
        })

    return PauseResult(
        action="pause-all",
        ok=overall_ok,
        initiated_by=initiated_by,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed,
        per_bot=per_bot,
        state_before=state_before,
        state_after=state_after,
        dry_run=dry_run,
    )


def resume_all(
    *,
    initiated_by: str = "operator",
    shared_dir: Path | None = None,
    network: dict | None = None,
    dry_run: bool = False,
) -> PauseResult:
    """Re-enable all bot gateways. Inverse of pause_all.

    Order matters:
      1) Bootstrap every gateway plist.
      2) Clear the pause-state flag (delete the file) so heal.py
         resumes its normal restart cadence.
      3) Append an audit row.
    """
    if network is None:
        network = load_network()

    started_at = _now_iso()
    t0 = time.monotonic()
    state_before = read_pause_state(shared_dir)

    bots = _iter_bots(network)
    per_bot = _dispatch_per_bot(bots, _bootstrap_gateway, dry_run)

    # Clear the pause flag AFTER attempting bootstrap. If bootstrap fails
    # for some bots, the flag stays so heal.py keeps its hands off; the
    # operator can investigate. If bootstrap succeeds for all, we clear it.
    overall_ok = all(r.ok for r in per_bot) if per_bot else True
    state_after: dict | None = state_before
    if overall_ok and not dry_run:
        p = pause_state_path(shared_dir)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Couldn't delete (race?) — best-effort overwrite to "paused=false"
            try:
                _atomic_write_json(p, {"paused": False, "cleared_at": _now_iso()}, sort_keys=True)
            except OSError:
                pass
        state_after = None
    elif not overall_ok and not dry_run:
        # Leave the flag in place; record that resume partially succeeded.
        state_after = {**(state_before or {}), "resume_partial_at": _now_iso()}
        try:
            _atomic_write_json(pause_state_path(shared_dir), state_after, sort_keys=True)
        except OSError:
            pass

    finished_at = _now_iso()
    elapsed = _elapsed_ms(t0)

    if not dry_run:
        _append_jsonl(_pause_log_path(shared_dir), {
            "action": "resume-all",
            "timestamp": finished_at,
            "initiated_by": initiated_by,
            "elapsed_ms": elapsed,
            "ok": overall_ok,
            "bots": [r.to_dict() for r in per_bot],
        })

    return PauseResult(
        action="resume-all",
        ok=overall_ok,
        initiated_by=initiated_by,
        reason="",
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed,
        per_bot=per_bot,
        state_before=state_before,
        state_after=state_after,
        dry_run=dry_run,
    )


def read_pause_log(shared_dir: Path | None = None, limit: int = 50) -> list[dict]:
    """Return the last ``limit`` pause-log records, newest first."""
    p = _pause_log_path(shared_dir)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ── Rollback: list candidates ───────────────────────────────────────────────


def _bot_workspace(bot_id: str, network: dict) -> Path:
    """Return the bot's workspace git repo (where backup.py committed)."""
    user = get_bot_user(bot_id, network)
    return Path(f"/Users/{user}/.openclaw/workspace")


def _bot_oc_json(bot_id: str, network: dict) -> Path:
    user = get_bot_user(bot_id, network)
    return Path(f"/Users/{user}/.openclaw/openclaw.json")


def _git(args: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run git in cwd; never raises.

    Per-bot workspaces are owned by the bot user (e.g. ``/Users/team_bot_c/`` owned
    by ``team_bot_c``); the admin server runs as ``evolve`` and has ACL read access
    via ``set_evolve_read_acl`` but git's "dubious ownership" safety check
    refuses to operate on a repo whose working tree it doesn't own. That
    surfaces as ``rev-parse`` returning non-zero with no useful stderr,
    which ``_is_workspace_repo`` then interprets as "not a git repo" and
    the UI renders "workspace not initialized" for every bot — even when
    backups are pushing cleanly. ``-c safe.directory=*`` short-circuits
    the ownership check for this single invocation (no global git config
    needed). Safe here because every probe path is under our control
    (bot workspaces resolved by ``_bot_workspace`` from network.json).
    """
    try:
        return subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(cwd)] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # Synthesize a failed CompletedProcess so callers don't need to
        # special-case exceptions.
        cp = subprocess.CompletedProcess(args, 1, "", f"{type(e).__name__}: {e}")
        return cp


def _is_workspace_repo(workspace: Path) -> bool:
    if not workspace.exists():
        return False
    r = _git(["rev-parse", "--git-dir"], workspace, timeout=5)
    return r.returncode == 0


def _list_workspace_log(
    workspace: Path,
    *,
    limit: int,
    only_backup: bool,
) -> list[dict]:
    """Return git log entries for the workspace.

    Each entry: {sha, iso_date, subject, has_backup_path}. ``has_backup_path``
    is true iff the commit touched ``evolve-backup/openclaw.json`` — that's
    our reliable rollback-anchor marker."""
    if not _is_workspace_repo(workspace):
        return []
    # Use a format that's easy to parse but unambiguous: NUL between
    # records, unit-separator between fields. ASCII control chars never
    # appear in commit subjects.
    sep_rec = "\x1e"
    sep_fld = "\x1f"
    pretty = f"--pretty=format:%H{sep_fld}%aI{sep_fld}%s{sep_rec}"
    args = ["log", pretty, f"-n{limit}", "--name-only"]
    r = _git(args, workspace, timeout=15)
    if r.returncode != 0:
        return []
    out: list[dict] = []
    chunks = (r.stdout or "").split(sep_rec)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, files_blob = chunk.partition("\n")
        parts = head.split(sep_fld)
        if len(parts) < 3:
            continue
        sha, iso_date, subject = parts[0], parts[1], parts[2]
        files = [f for f in (files_blob or "").splitlines() if f.strip()]
        has_backup_path = any(f == "evolve-backup/openclaw.json" for f in files)
        is_backup_commit = subject.startswith("[backup]") or has_backup_path
        if only_backup and not is_backup_commit:
            continue
        out.append({
            "sha": sha,
            "iso_date": iso_date,
            "subject": subject,
            "has_backup_path": has_backup_path,
            "is_backup_commit": is_backup_commit,
        })
    return out


def list_rollback_points(
    bot_id: str,
    *,
    network: dict | None = None,
    limit: int = 14,
    only_backup: bool = True,
) -> list[RollbackPoint]:
    """Return candidate rollback targets for a bot, newest first.

    Walks the bot's workspace git log for commits that contain
    ``evolve-backup/openclaw.json``. Limited to ``limit`` rows for UI
    sanity; default 14 = roughly two weeks of daily backups."""
    if network is None:
        network = load_network()
    workspace = _bot_workspace(bot_id, network)
    entries = _list_workspace_log(workspace, limit=max(limit * 3, limit), only_backup=False)
    # If the caller wants only backup commits, filter here so the limit
    # applies to the post-filter list.
    if only_backup:
        entries = [e for e in entries if e["is_backup_commit"]]
    entries = entries[:limit]

    points: list[RollbackPoint] = []
    for e in entries:
        # For each candidate, verify evolve-backup/openclaw.json exists
        # at that sha. Otherwise the rollback would fail at apply time
        # — better to surface unavailability up front.
        has_oc = _commit_has_openclaw(workspace, e["sha"])
        # Local human-readable date (best-effort)
        try:
            dt = datetime.fromisoformat(e["iso_date"])
            local = dt.astimezone().strftime("%a %Y-%m-%d %H:%M %Z")
        except (ValueError, TypeError):
            local = e["iso_date"]
        points.append(RollbackPoint(
            bot_id=bot_id,
            commit_sha=e["sha"],
            commit_date=e["iso_date"],
            commit_date_local=local,
            summary=e["subject"],
            has_openclaw_json=has_oc,
            is_backup_commit=e["is_backup_commit"],
        ))
    return points


def _commit_has_openclaw(workspace: Path, sha: str) -> bool:
    """True iff ``evolve-backup/openclaw.json`` exists in the given commit."""
    if not _is_workspace_repo(workspace):
        return False
    r = _git(
        ["cat-file", "-e", f"{sha}:evolve-backup/openclaw.json"],
        workspace, timeout=5,
    )
    return r.returncode == 0


# ── Rollback: resolve target ────────────────────────────────────────────────


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _resolve_target(
    bot_id: str,
    target: str,
    network: dict,
) -> tuple[str | None, str]:
    """Translate a user-supplied target into a concrete commit SHA.

    target accepts:
      - a 7-40 char hex SHA prefix (rev-parse'd to the full SHA)
      - a YYYY-MM-DD date (matches the most recent backup commit on that
        UTC day; if no backup that day, picks the most recent backup
        commit *on or before* that date)

    Returns (sha_or_None, message_explaining_choice).
    """
    target = (target or "").strip()
    if not target:
        return None, "empty target"

    workspace = _bot_workspace(bot_id, network)
    if not _is_workspace_repo(workspace):
        return None, f"workspace at {workspace} is not a git repo"

    if _SHA_RE.match(target):
        # Resolve to full SHA via git rev-parse
        r = _git(["rev-parse", target], workspace, timeout=5)
        if r.returncode != 0:
            return None, f"sha {target!r} not found in workspace history"
        sha = (r.stdout or "").strip()
        if not _commit_has_openclaw(workspace, sha):
            return None, (
                f"commit {sha[:8]} does not contain "
                "evolve-backup/openclaw.json — cannot rollback to this point"
            )
        return sha, f"resolved sha {target!r} → {sha}"

    if _DATE_RE.match(target):
        # Pick most recent backup commit on or before end-of-day UTC of target.
        try:
            day_dt = datetime.strptime(target, "%Y-%m-%d")
        except ValueError:
            return None, f"invalid date {target!r}"
        # End of the target day in UTC, inclusive.
        end = day_dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        # Walk recent log entries and pick the newest whose timestamp <= end
        # AND that contains evolve-backup/openclaw.json. Cap the search at
        # 200 commits — well past any reasonable retention window.
        entries = _list_workspace_log(workspace, limit=200, only_backup=False)
        for e in entries:
            try:
                ent_dt = datetime.fromisoformat(e["iso_date"])
            except (ValueError, TypeError):
                continue
            if ent_dt > end:
                continue
            if not _commit_has_openclaw(workspace, e["sha"]):
                continue
            return e["sha"], (
                f"resolved date {target} → commit {e['sha'][:8]} "
                f"(at {e['iso_date']})"
            )
        return None, f"no backup commit found on or before {target}"

    return None, (
        f"target {target!r} is not a sha (7-40 hex) or date (YYYY-MM-DD)"
    )


# ── Rollback: write config ──────────────────────────────────────────────────


def _read_committed_openclaw(workspace: Path, sha: str) -> str | None:
    """Return the openclaw.json bytes at the given commit, or None."""
    r = _git(
        ["show", f"{sha}:evolve-backup/openclaw.json"],
        workspace, timeout=10,
    )
    if r.returncode != 0:
        return None
    out = r.stdout
    if not out or not out.strip():
        return None
    # Validate it parses as JSON before we ever write it as a config
    try:
        parsed = json.loads(out)
        if not isinstance(parsed, dict):
            return None
    except json.JSONDecodeError:
        return None
    return out


def _read_live_openclaw(bot_id: str, network: dict) -> str | None:
    """Read the bot's current openclaw.json (for pre-rollback snapshot).

    Try direct read first (evolve has ACL read on .openclaw/), fall
    back to ``sudo /bin/cat``. Same pattern as backup.py / heal.py."""
    path = _bot_oc_json(bot_id, network)
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None
    except OSError:
        return None


def _write_bot_openclaw(bot_id: str, network: dict, content: str) -> tuple[bool, str]:
    """Write the bot's openclaw.json via /tmp staging + sudo cp.

    Mirrors the canonical apply.py ``_atomic_write`` write path. The
    evolve user has a sudoers grant for ``/bin/cp /tmp/evolve-*.json
    /Users/*/.openclaw/openclaw.json`` (via the broader evolve sudoers
    block); see deploy.py:88 and setup_wizard.py:1100+.

    Returns (ok, error_or_msg)."""
    user = get_bot_user(bot_id, network)
    dest = Path(f"/Users/{user}/.openclaw/openclaw.json")

    # Validate JSON parses BEFORE writing — refuse to write garbage.
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return False, "rollback content is not a JSON object"
    except json.JSONDecodeError as e:
        return False, f"rollback content is not valid JSON: {e}"

    # Stage in /tmp with a recognizable prefix (sudoers grant matches
    # evolve-* names).
    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-rollback-{bot_id}-", suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Try direct copy first (when running as the bot user via the
        # CLI under sudo, or when ACLs permit). If that fails, fall back
        # to sudo /bin/cp.
        try:
            shutil.copy2(tmp, dest)
        except (PermissionError, OSError):
            r = subprocess.run(
                ["sudo", "-n", "/bin/cp", tmp, str(dest)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return False, f"sudo cp failed (rc={r.returncode}): {r.stderr.strip()}"
            # Best-effort chown; failure is non-fatal because the file
            # contents are right even if ownership lags. chown BINARY routes
            # through platform_profile (W7): /usr/sbin/chown (macOS) vs
            # /usr/bin/chown (Linux); the macOS literal never matches the Linux
            # NOPASSWD grant. `:staff` (bot primary group) stays literal.
            subprocess.run(
                ["sudo", "-n", get_profile().chown, f"{user}:staff", str(dest)],
                capture_output=True, text=True, timeout=5,
            )
        return True, f"wrote {dest}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _restart_bot_gateway(bot_id: str) -> tuple[bool, str]:
    """Restart a single bot's gateway after a config change.

    Uses ``launchctl kickstart -k`` on the system label — same mechanism
    apply.py uses post-patch. Evolve sudoers grant:
    ``/bin/launchctl kickstart -k system/ai.openclaw.*``."""
    label = _gateway_label(bot_id)
    # raw (not Scheduler.restart()): restart() needs plain sudo and returns
    # only ok+combined output — this path needs sudo -n (daemon context)
    # plus the raw rc for the audit-grade failure message below.
    rc, _out, err = _launchctl_n("kickstart", "-k", f"system/{label}")
    if rc != 0:
        return False, f"kickstart rc={rc}: {(err or '').strip()}"
    return True, "kickstart ok"


# ── Rollback: main entry ────────────────────────────────────────────────────


def rollback_bot(
    bot_id: str,
    target: str,
    *,
    network: dict | None = None,
    shared_dir: Path | None = None,
    initiated_by: str = "operator",
    skip_restart: bool = False,
    dry_run: bool = False,
) -> RollbackResult:
    """Revert ``bot_id``'s openclaw.json to ``target`` (sha or YYYY-MM-DD).

    Steps:
      1) Resolve target → commit SHA.
      2) Read the openclaw.json from that commit; validate it parses.
      3) Snapshot the CURRENT openclaw.json into
         ``{shared_dir}/recovery/rollbacks/{rollback_id}.json``. This is
         what makes the rollback itself reversible — to undo, we feed
         the snapshot back through rollback_bot via reverse_rollback.
      4) Write the target content to the bot's openclaw.json
         (/tmp + sudo cp).
      5) Restart the gateway (kickstart -k) so the new config takes
         effect immediately. Skippable via ``skip_restart=True``.
      6) Update the rollback record with the post-step outcome.
    """
    if network is None:
        network = load_network()

    rollback_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()

    # Step 1: resolve target
    sha, msg = _resolve_target(bot_id, target, network)
    if sha is None:
        return RollbackResult(
            rollback_id=rollback_id, bot_id=bot_id, target_commit="",
            started_at=started_at, finished_at=_now_iso(),
            ok=False, message=f"could not resolve rollback target: {msg}",
            dry_run=dry_run,
        )

    # Step 2: read the committed openclaw.json
    workspace = _bot_workspace(bot_id, network)
    target_content = _read_committed_openclaw(workspace, sha)
    if target_content is None:
        return RollbackResult(
            rollback_id=rollback_id, bot_id=bot_id, target_commit=sha,
            started_at=started_at, finished_at=_now_iso(),
            ok=False,
            message=(
                f"could not read evolve-backup/openclaw.json at {sha[:8]} "
                "— commit may be missing the file or content is invalid JSON"
            ),
            dry_run=dry_run,
        )

    # Step 3: snapshot the current config
    live_content = _read_live_openclaw(bot_id, network)
    if live_content is None:
        return RollbackResult(
            rollback_id=rollback_id, bot_id=bot_id, target_commit=sha,
            started_at=started_at, finished_at=_now_iso(),
            ok=False,
            message=(
                "could not read current openclaw.json for pre-rollback "
                "snapshot — refusing to proceed (rollback would not be "
                "reversible)"
            ),
            dry_run=dry_run,
        )

    rec_path = _rollback_dir(shared_dir) / f"{rollback_id}.json"
    rec: dict = {
        "rollback_id": rollback_id,
        "bot_id": bot_id,
        "target_commit": sha,
        "target_input": target,
        "resolved_message": msg,
        "started_at": started_at,
        "initiated_by": initiated_by,
        "pre_rollback_config": live_content,
        "post_rollback_config": target_content,
        "reversed_from_rollback_id": "",
        "reversed_by_rollback_id": "",
        "ok": False,           # set to True only after step 4 succeeds
        "dry_run": dry_run,
    }
    if not dry_run:
        _atomic_write_json(rec_path, rec, sort_keys=True)

    if dry_run:
        return RollbackResult(
            rollback_id=rollback_id, bot_id=bot_id, target_commit=sha,
            started_at=started_at, finished_at=_now_iso(),
            ok=True,
            message=f"DRY-RUN: would write {len(target_content)} bytes to {_bot_oc_json(bot_id, network)} and kickstart gateway",
            pre_rollback_config_path=str(rec_path),
            dry_run=True,
        )

    # Step 4: write the config
    write_ok, write_msg = _write_bot_openclaw(bot_id, network, target_content)
    if not write_ok:
        rec["ok"] = False
        rec["write_error"] = write_msg
        rec["finished_at"] = _now_iso()
        _atomic_write_json(rec_path, rec, sort_keys=True)
        return RollbackResult(
            rollback_id=rollback_id, bot_id=bot_id, target_commit=sha,
            started_at=started_at, finished_at=rec["finished_at"],
            ok=False, message=f"write failed: {write_msg}",
            pre_rollback_config_path=str(rec_path),
            dry_run=False,
        )

    # Step 5: restart the gateway so the new config takes effect
    restart_ok: bool | None = None
    restart_msg = ""
    if not skip_restart:
        restart_ok, restart_msg = _restart_bot_gateway(bot_id)
    rec["ok"] = True
    rec["finished_at"] = _now_iso()
    rec["gateway_restart_ok"] = restart_ok
    rec["gateway_restart_msg"] = restart_msg
    _atomic_write_json(rec_path, rec, sort_keys=True)

    return RollbackResult(
        rollback_id=rollback_id, bot_id=bot_id, target_commit=sha,
        started_at=started_at, finished_at=rec["finished_at"],
        ok=True,
        message=f"rolled back to {sha[:8]} ({msg})",
        pre_rollback_config_path=str(rec_path),
        post_rollback_config_path=str(_bot_oc_json(bot_id, network)),
        gateway_restart_ok=restart_ok,
        gateway_restart_msg=restart_msg,
        dry_run=False,
    )


def list_rollback_history(
    *,
    shared_dir: Path | None = None,
    bot_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return the rollback audit records, newest first.

    Each entry omits the full config blobs to keep the response
    small; the caller can read the per-rollback JSON file directly
    for that detail."""
    rd = _rollback_dir(shared_dir)
    entries: list[dict] = []
    for f in sorted(rd.glob("*.json"), reverse=True):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if bot_id and rec.get("bot_id") != bot_id:
            continue
        # Trim large fields for the list view
        slim = {k: v for k, v in rec.items() if k not in (
            "pre_rollback_config", "post_rollback_config",
        )}
        slim["has_pre_rollback_snapshot"] = bool(rec.get("pre_rollback_config"))
        entries.append(slim)
        if len(entries) >= limit:
            break
    return entries


def reverse_rollback(
    rollback_id: str,
    *,
    network: dict | None = None,
    shared_dir: Path | None = None,
    initiated_by: str = "operator",
    skip_restart: bool = False,
    dry_run: bool = False,
) -> RollbackResult:
    """Undo a previous rollback by restoring its pre-rollback snapshot.

    The new operation is recorded as its own rollback (so it too is
    reversible — rollbacks are a doubly-linked list of states). The
    original record's ``reversed_by_rollback_id`` field is updated.
    """
    if network is None:
        network = load_network()

    rec_path = _rollback_dir(shared_dir) / f"{rollback_id}.json"
    if not rec_path.exists():
        return RollbackResult(
            rollback_id="", bot_id="", target_commit="",
            started_at=_now_iso(), finished_at=_now_iso(),
            ok=False, message=f"no rollback record with id {rollback_id!r}",
        )
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return RollbackResult(
            rollback_id="", bot_id="", target_commit="",
            started_at=_now_iso(), finished_at=_now_iso(),
            ok=False, message=f"could not read rollback record: {e}",
        )
    if not rec.get("ok"):
        return RollbackResult(
            rollback_id="", bot_id=rec.get("bot_id", ""), target_commit="",
            started_at=_now_iso(), finished_at=_now_iso(),
            ok=False, message=(
                "original rollback did not succeed — there is nothing "
                "to reverse"
            ),
        )
    if rec.get("reversed_by_rollback_id"):
        return RollbackResult(
            rollback_id="", bot_id=rec.get("bot_id", ""), target_commit="",
            started_at=_now_iso(), finished_at=_now_iso(),
            ok=False, message=(
                f"already reversed by rollback "
                f"{rec['reversed_by_rollback_id']}"
            ),
        )

    bot_id = rec["bot_id"]
    pre_content = rec.get("pre_rollback_config", "")
    if not pre_content:
        return RollbackResult(
            rollback_id="", bot_id=bot_id, target_commit="",
            started_at=_now_iso(), finished_at=_now_iso(),
            ok=False, message="rollback record missing pre_rollback_config",
        )

    # Generate a new rollback id for the reverse operation
    new_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()

    if dry_run:
        return RollbackResult(
            rollback_id=new_id, bot_id=bot_id,
            target_commit=f"reverse-of-{rollback_id}",
            started_at=started_at, finished_at=_now_iso(),
            ok=True,
            message=(
                f"DRY-RUN: would restore pre-rollback snapshot for "
                f"rollback {rollback_id} (bot={bot_id}, "
                f"{len(pre_content)} bytes)"
            ),
            reversed_from_rollback_id=rollback_id,
            dry_run=True,
        )

    # Persist the new reverse-rollback record FIRST so we have an audit
    # row even if the write fails halfway through.
    new_rec: dict = {
        "rollback_id": new_id,
        "bot_id": bot_id,
        "target_commit": f"reverse-of-{rollback_id}",
        "target_input": f"reverse-of-{rollback_id}",
        "resolved_message": f"restoring pre-rollback snapshot of {rollback_id}",
        "started_at": started_at,
        "initiated_by": initiated_by,
        # The "pre" config for the reverse is the CURRENT state (which
        # is the original rollback's post_rollback_config in the happy
        # path; re-read live to be safe).
        "pre_rollback_config": _read_live_openclaw(bot_id, network) or rec.get("post_rollback_config", ""),
        "post_rollback_config": pre_content,
        "reversed_from_rollback_id": rollback_id,
        "reversed_by_rollback_id": "",
        "ok": False,
        "dry_run": False,
    }
    new_path = _rollback_dir(shared_dir) / f"{new_id}.json"
    _atomic_write_json(new_path, new_rec, sort_keys=True)

    write_ok, write_msg = _write_bot_openclaw(bot_id, network, pre_content)
    if not write_ok:
        new_rec["ok"] = False
        new_rec["write_error"] = write_msg
        new_rec["finished_at"] = _now_iso()
        _atomic_write_json(new_path, new_rec, sort_keys=True)
        return RollbackResult(
            rollback_id=new_id, bot_id=bot_id,
            target_commit=f"reverse-of-{rollback_id}",
            started_at=started_at, finished_at=new_rec["finished_at"],
            ok=False, message=f"write failed: {write_msg}",
            pre_rollback_config_path=str(new_path),
            reversed_from_rollback_id=rollback_id,
        )

    # Stamp the original record so callers see "this was reversed"
    rec["reversed_by_rollback_id"] = new_id
    _atomic_write_json(rec_path, rec, sort_keys=True)

    restart_ok: bool | None = None
    restart_msg = ""
    if not skip_restart:
        restart_ok, restart_msg = _restart_bot_gateway(bot_id)
    new_rec["ok"] = True
    new_rec["finished_at"] = _now_iso()
    new_rec["gateway_restart_ok"] = restart_ok
    new_rec["gateway_restart_msg"] = restart_msg
    _atomic_write_json(new_path, new_rec, sort_keys=True)

    return RollbackResult(
        rollback_id=new_id, bot_id=bot_id,
        target_commit=f"reverse-of-{rollback_id}",
        started_at=started_at, finished_at=new_rec["finished_at"],
        ok=True,
        message=f"reversed rollback {rollback_id}",
        pre_rollback_config_path=str(new_path),
        post_rollback_config_path=str(_bot_oc_json(bot_id, network)),
        gateway_restart_ok=restart_ok,
        gateway_restart_msg=restart_msg,
        reversed_from_rollback_id=rollback_id,
    )


# ── Pod-state commits (deploy checkout) ─────────────────────────────────────
#
# The deploy checkout (/Users/Shared/evolve-repo on macOS,
# /var/lib/evolve/repo on Linux — platform-keyed via _DEPLOY_REPO below) is
# pulled every 15 min
# by the repo-puller daemon.  Each pull is a fast-forward of git commits
# written by humans or CI.  These commits represent the authoritative pod
# state at each point in time — rolling one back is the highest-level
# "undo" available: it reverts code, configs, gallery manifests, etc.
#
# This is distinct from the per-bot openclaw.json rollback above, which is
# scoped to a single bot's config.  Pod-state rollback is pod-wide.
#
# Safety requirements (all non-negotiable):
#   • Confirmation token: short TTL (30 s), single-use, tied to a specific sha.
#   • Uncommitted worktree check: if the deploy checkout has local changes,
#     we refuse rather than trample.
#   • Audit log: every attempt (success and failure) is appended to
#     {shared_dir}/recovery/pod-rollback-log.jsonl.
#   • Daemon restarts: after resetting the repo, we kickstart the admin-ui
#     daemon so the new code is live (same as repo-puller does post-pull).

import secrets as _secrets

# Platform-keyed: macOS resolves /Users/Shared/evolve-repo byte-identically;
# a Linux pod resolves /var/lib/evolve/repo. The web callers
# (list_recent_pod_commits / rollback_pod_state) do NOT pass deploy_repo, so
# this default IS the live rollback target — a hardcoded macOS path made the
# operator's emergency-undo safety net no-op on Linux pods (empty
# recent-commits list, rollback against a nonexistent repo). See
# repo_puller.DEFAULT_REPO for the reference usage of this profile field.
_DEPLOY_REPO = Path(get_profile().deploy_checkout_default)

# High-impact paths: if a rollback touches these, we flag it so the operator
# knows what they're getting into.
_HIGH_IMPACT_PATHS = (
    "network.json",
    "packages/admin/",
    "packages/analyzer/",
    "packages/plugin/",
)

# Confirmation tokens: { sha: (token, expires_at) }
# In-process dict is safe here — the admin server is single-process.
# Single-use: we delete the entry on first valid use.
_PENDING_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_SEC = 30


def _pod_rollback_log_path(shared_dir: Path | None = None) -> Path:
    return recovery_dir(shared_dir) / "pod-rollback-log.jsonl"


def _git_repo(repo: Path, args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run git in the deploy repo; never raises."""
    return _git(args, repo, timeout=timeout)


def _check_clean_worktree(repo: Path) -> tuple[bool, str]:
    """Return (is_clean, description).

    Uses ``git status --porcelain`` — any output means dirty.  We check
    both tracked modifications and untracked files.  The deploy checkout
    is meant to be clean between pulls; if it's dirty, something
    unexpected happened and we refuse to reset."""
    r = _git(["status", "--porcelain"], repo, timeout=10)
    if r.returncode != 0:
        return False, f"git status failed (rc={r.returncode}): {(r.stderr or '').strip()}"
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    if lines:
        preview = "; ".join(lines[:5])
        return False, f"deploy checkout has {len(lines)} uncommitted change(s): {preview}"
    return True, "clean"


def _git_log_n(repo: Path, n: int, since_days: int | None = None) -> list[dict]:
    """Return up to n commits from the repo, newest first.

    Each entry: {sha, short_sha, subject, author, timestamp, files}.
    ``since_days`` limits to commits newer than N days ago (UTC).

    We use two separate git invocations: one for the structured commit
    metadata, one for the per-commit file lists (--name-only).  This avoids
    the parsing ambiguity of mixing --pretty format separators with file
    names that come after each commit in the same output stream.
    """
    sep_rec = "\x1e"
    sep_fld = "\x1f"
    # Pass 1: get structured metadata (sha, author, timestamp, subject).
    # Use format= (not pretty=format=) so git doesn't add a trailing newline.
    meta_fmt = f"%H{sep_fld}%aN{sep_fld}%aI{sep_fld}%s{sep_rec}"
    args_meta = ["log", f"--format={meta_fmt}", f"-n{n}"]
    if since_days is not None:
        args_meta += [f"--since={since_days} days ago"]
    r_meta = _git(args_meta, repo, timeout=20)
    if r_meta.returncode != 0:
        return []
    metas: list[tuple[str, str, str, str]] = []  # (sha, author, ts, subject)
    for chunk in (r_meta.stdout or "").split(sep_rec):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(sep_fld)
        if len(parts) < 4:
            continue
        metas.append((parts[0], parts[1], parts[2], parts[3]))
    if not metas:
        return []

    # Pass 2: get file lists per commit.  We query each sha individually
    # only if there are few commits (avoid N+1 for large counts); otherwise
    # use a single diff-tree sweep.  The results are small, so individual
    # calls are fine for our typical 7-day window.
    out: list[dict] = []
    for sha, author, ts, subject in metas:
        # diff-tree gives us the file names changed by this commit.
        # --no-commit-id suppresses the commit sha header; -r recurses into
        # subdirectories; --name-only gives us just names.
        r_files = _git(
            ["diff-tree", "--no-commit-id", "-r", "--name-only", sha],
            repo, timeout=10,
        )
        files = []
        if r_files.returncode == 0:
            files = [f for f in (r_files.stdout or "").splitlines() if f.strip()]
        out.append({
            "sha": sha,
            "short_sha": sha[:8],
            "subject": subject,
            "author": author,
            "timestamp": ts,
            "files": files,
        })
    return out


def _rollback_safe(repo: Path, sha: str) -> bool:
    """True iff rolling back to sha would not lose commits that diverged from main.

    In practice the deploy checkout is always a linear fast-forward of main,
    so this is almost always True.  We flag False when there are local commits
    on top of the target sha (i.e. the operator is trying to roll back past
    their own uncommitted work — which the clean-worktree check catches anyway,
    but belt-and-suspenders)."""
    # Count commits reachable from HEAD but not from sha — these would be lost.
    r = _git(["rev-list", "--count", f"{sha}..HEAD"], repo, timeout=10)
    if r.returncode != 0:
        return True  # Can't tell; optimistic
    try:
        ahead = int((r.stdout or "0").strip())
        # Rolling back loses commits if ahead > 0, but those commits are
        # just normal git history that will be re-pulled next time the
        # puller runs. That's fine and expected — we mark safe=True.
        # We mark safe=False only if the deploy repo has LOCAL commits
        # not on origin/main (which would be lost permanently).
        r2 = _git(["rev-list", "--count", "origin/main..HEAD"], repo, timeout=10)
        if r2.returncode == 0:
            local_only = int((r2.stdout or "0").strip())
            return local_only == 0
    except (ValueError, TypeError):
        pass
    return True


def _classify_files(files: list[str]) -> tuple[list[str], bool]:
    """Return (high_impact_list, any_high_impact).

    High-impact files are ones that touch load-bearing runtime paths."""
    hi = [f for f in files if any(f.startswith(p) or f == p for p in _HIGH_IMPACT_PATHS)]
    return hi, bool(hi)


def _estimate_rollback_size(repo: Path, sha: str) -> str:
    """Return a human-readable diff-stat string for HEAD..sha."""
    r = _git(["diff", "--stat", "HEAD", sha], repo, timeout=15)
    if r.returncode != 0:
        return "unknown"
    lines = [(l.strip()) for l in (r.stdout or "").splitlines() if l.strip()]
    if not lines:
        return "no changes"
    return lines[-1]  # last line is the summary ("N files changed, X insertions…")


@dataclass
class PodCommit:
    """One pod-state commit from the deploy checkout."""

    sha: str
    short_sha: str
    subject: str
    author: str
    timestamp: str           # ISO 8601
    files_changed: int
    high_impact_paths: list[str]
    rollback_safe: bool
    rollback_size_estimate: str
    confirm_token: str       # single-use, 30-sec TTL; empty if token generation failed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PodRollbackResult:
    """Outcome of a pod-state rollback."""

    rollback_id: str
    target_sha: str
    started_at: str
    finished_at: str
    ok: bool
    message: str
    files_changed: int = 0
    daemon_restart_ok: bool | None = None
    daemon_restart_msg: str = ""
    dry_run: bool = False
    initiated_by: str = "web"

    def to_dict(self) -> dict:
        return asdict(self)


def _issue_confirm_token(sha: str) -> str:
    """Generate a single-use 30-second confirmation token for the given sha.

    The token is a 32-byte URL-safe random string.  It is stored in
    _PENDING_TOKENS keyed by sha — only one pending token per sha is kept
    (a second call replaces the first, resetting the TTL)."""
    tok = _secrets.token_urlsafe(32)
    _PENDING_TOKENS[sha] = (tok, time.monotonic() + _TOKEN_TTL_SEC)
    return tok


def _consume_confirm_token(sha: str, token: str) -> tuple[bool, str]:
    """Validate and consume a confirmation token.

    Returns (valid, reason).  On success the token is deleted from the
    in-memory store (single-use)."""
    if sha not in _PENDING_TOKENS:
        return False, "no pending confirmation token for this sha"
    stored_tok, expires_at = _PENDING_TOKENS[sha]
    if time.monotonic() > expires_at:
        del _PENDING_TOKENS[sha]
        return False, "confirmation token expired (30-second window passed)"
    if not _secrets.compare_digest(stored_tok, token):
        # Don't delete on mismatch — preserve for the legitimate caller,
        # but this is unusual (possible double-submit race).
        return False, "confirmation token mismatch"
    # Valid — consume it
    del _PENDING_TOKENS[sha]
    return True, "ok"


def list_recent_pod_commits(
    *,
    days: int = 7,
    limit: int = 50,
    deploy_repo: Path | None = None,
    shared_dir: Path | None = None,
) -> list[PodCommit]:
    """Return recent commits from the deploy checkout, newest first.

    For each commit we:
      - classify which files changed and whether any are high-impact
      - compute a diff-stat estimate of the rollback cost
      - issue a fresh confirmation token (30 s TTL, single-use)

    The confirm_token is embedded in the response so the UI can send it
    back when the operator clicks "Rollback to here."
    """
    repo = deploy_repo or _DEPLOY_REPO
    if not repo.exists() or not (repo / ".git").exists():
        return []
    raw = _git_log_n(repo, n=limit, since_days=days)
    out: list[PodCommit] = []
    for entry in raw:
        sha = entry["sha"]
        files = entry["files"]
        hi_paths, _ = _classify_files(files)
        safe = _rollback_safe(repo, sha)
        size = _estimate_rollback_size(repo, sha)
        tok = _issue_confirm_token(sha)
        out.append(PodCommit(
            sha=sha,
            short_sha=entry["short_sha"],
            subject=entry["subject"],
            author=entry["author"],
            timestamp=entry["timestamp"],
            files_changed=len(files),
            high_impact_paths=hi_paths,
            rollback_safe=safe,
            rollback_size_estimate=size,
            confirm_token=tok,
        ))
    return out


def rollback_pod_state(
    *,
    commit_sha: str,
    confirm_token: str,
    shared_dir: Path | None = None,
    deploy_repo: Path | None = None,
    initiated_by: str = "web",
    dry_run: bool = False,
) -> PodRollbackResult:
    """Revert the deploy checkout to commit_sha via git reset --hard.

    Safety gates (all checked before touching anything):
      1. Token validation: confirm_token must match the one issued by
         list_recent_pod_commits for this sha, and must not be expired.
      2. Clean worktree: the deploy checkout must have no uncommitted changes.
      3. Sha exists: git cat-file -e sha.

    On success:
      - git reset --hard <sha> on the deploy checkout
      - Audit log row written
      - Admin-UI daemon kickstarted so the new code takes effect

    dry_run=True performs all checks but skips the reset and restart.
    """
    repo = deploy_repo or _DEPLOY_REPO
    log_path = _pod_rollback_log_path(shared_dir)
    rollback_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()

    def _fail(msg: str, **extra) -> PodRollbackResult:
        rec = {
            "rollback_id": rollback_id,
            "target_sha": commit_sha,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "ok": False,
            "initiated_by": initiated_by,
            "message": msg,
            "dry_run": dry_run,
            **extra,
        }
        if not dry_run:
            _append_jsonl(log_path, rec)
        return PodRollbackResult(
            rollback_id=rollback_id, target_sha=commit_sha,
            started_at=started_at, finished_at=rec["finished_at"],
            ok=False, message=msg, dry_run=dry_run, initiated_by=initiated_by,
        )

    # Gate 1: token
    tok_ok, tok_msg = _consume_confirm_token(commit_sha, confirm_token)
    if not tok_ok:
        return _fail(f"confirmation token invalid: {tok_msg}")

    # Gate 2: deploy repo exists
    if not repo.exists() or not (repo / ".git").exists():
        return _fail(f"deploy repo not found at {repo}")

    # Gate 3: sha must exist in the repo
    r_obj = _git(["cat-file", "-e", commit_sha], repo, timeout=5)
    if r_obj.returncode != 0:
        return _fail(f"commit {commit_sha[:8]} not found in deploy repo")

    # Gate 4: clean worktree
    clean, clean_msg = _check_clean_worktree(repo)
    if not clean:
        return _fail(f"refusing rollback — {clean_msg}")

    # Count files that will change (diff HEAD → sha)
    r_stat = _git(["diff", "--name-only", "HEAD", commit_sha], repo, timeout=15)
    files_changed = len([l for l in (r_stat.stdout or "").splitlines() if l.strip()])
    size_est = _estimate_rollback_size(repo, commit_sha)

    if dry_run:
        rec = {
            "rollback_id": rollback_id,
            "target_sha": commit_sha,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "ok": True,
            "initiated_by": initiated_by,
            "message": f"DRY-RUN: would reset to {commit_sha[:8]} ({files_changed} files, {size_est})",
            "files_changed": files_changed,
            "dry_run": True,
        }
        # Dry-run: don't log (nothing happened)
        return PodRollbackResult(
            rollback_id=rollback_id, target_sha=commit_sha,
            started_at=started_at, finished_at=rec["finished_at"],
            ok=True, message=rec["message"],
            files_changed=files_changed, dry_run=True, initiated_by=initiated_by,
        )

    # Canary release mode (spec-state-store-and-deploy-resilience §2.5):
    # a bare reset here desyncs the release pointer and the next release
    # tick repairs the checkout right back (exactly the fight the legacy
    # puller had with this function). Route through the release
    # manager's rollback instead — same end state, plus pointer update,
    # skip-listing, pinning, hook suite, and canary restore.
    try:
        from . import release_manager as _relmgr
        from .config import load_network as _load_net, DEFAULT_NETWORK_CONFIG as _NETCFG
        _rcfg = _relmgr.resolve_release_config(_load_net(_NETCFG))
    except Exception:
        _rcfg = None
    if _rcfg is not None and _rcfg.mode == "canary":
        t0 = time.monotonic()
        rel = _relmgr.release_rollback(
            repo,
            Path(shared_dir) if shared_dir else DEFAULT_SHARED_DIR,
            to_ref=commit_sha,
        )
        elapsed = _elapsed_ms(t0)
        if not rel.success:
            return _fail(f"release rollback failed: {rel.error}")
        rec = {
            "rollback_id": rollback_id,
            "target_sha": commit_sha,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "ok": True,
            "initiated_by": initiated_by,
            "message": (
                f"rolled back via release manager to {commit_sha[:8]} "
                f"({files_changed} files); release pinned"
            ),
            "files_changed": files_changed,
            "elapsed_ms": elapsed,
            "dry_run": False,
        }
        _append_jsonl(log_path, rec)
        return PodRollbackResult(
            rollback_id=rollback_id, target_sha=commit_sha,
            started_at=started_at, finished_at=rec["finished_at"],
            ok=True, message=rec["message"],
            files_changed=files_changed, dry_run=False,
            initiated_by=initiated_by,
        )

    # Execute: git reset --hard <sha>
    # The evolve user must have ownership of the repo for this to work.
    # In practice the repo-puller runs as evolve, so the .git dir is
    # owned by evolve.  The sudoers grant for /usr/bin/git is NOT present
    # (we don't shell out to sudo git — we run git directly as the evolve
    # process, which owns the repo after the initial chown by setup_wizard).
    t0 = time.monotonic()
    r_reset = _git(["reset", "--hard", commit_sha], repo, timeout=60)
    elapsed = _elapsed_ms(t0)

    if r_reset.returncode != 0:
        return _fail(
            f"git reset --hard failed (rc={r_reset.returncode}): "
            f"{(r_reset.stderr or '').strip()[:500]}"
        )

    # Restart the admin-ui daemon so the new code is live.
    # raw (not Scheduler.restart()): needs sudo -n (daemon context, no TTY)
    # and the stderr-only channel for the audit record below.
    restart_ok: bool | None = None
    restart_msg = ""
    rc_kick, _out_kick, err_kick = _launchctl_n(
        "kickstart", "-k", "system/ai.evolve.evolve.admin-ui",
    )
    restart_ok = (rc_kick == 0)
    restart_msg = (err_kick or "").strip()[:200] or "ok"

    finished_at = _now_iso()
    rec = {
        "rollback_id": rollback_id,
        "target_sha": commit_sha,
        "started_at": started_at,
        "finished_at": finished_at,
        "ok": True,
        "initiated_by": initiated_by,
        "message": f"reset to {commit_sha[:8]} ({files_changed} files, {size_est})",
        "files_changed": files_changed,
        "size_estimate": size_est,
        "elapsed_ms": elapsed,
        "daemon_restart_ok": restart_ok,
        "daemon_restart_msg": restart_msg,
        "dry_run": False,
    }
    _append_jsonl(log_path, rec)

    return PodRollbackResult(
        rollback_id=rollback_id, target_sha=commit_sha,
        started_at=started_at, finished_at=finished_at,
        ok=True,
        message=rec["message"],
        files_changed=files_changed,
        daemon_restart_ok=restart_ok,
        daemon_restart_msg=restart_msg,
        dry_run=False,
        initiated_by=initiated_by,
    )


def list_pod_rollback_log(
    shared_dir: Path | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return pod-state rollback audit records, newest first."""
    p = _pod_rollback_log_path(shared_dir)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ── Public summary for the dashboard ────────────────────────────────────────


def recovery_status(
    *,
    shared_dir: Path | None = None,
    network: dict | None = None,
) -> dict:
    """Return a compact status dict suitable for the dashboard tile."""
    if network is None:
        network = load_network()
    pause = read_pause_state(shared_dir)
    bots = _iter_bots(network)
    return {
        "paused": pause is not None,
        "pause_state": pause,
        "bot_count": len(bots),
        "bot_ids": [b for b, _u in bots],
        "recent_pause_events": read_pause_log(shared_dir, limit=10),
        "recent_rollbacks": list_rollback_history(
            shared_dir=shared_dir, limit=10,
        ),
    }
