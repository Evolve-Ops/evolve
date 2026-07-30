"""Post-condition verifications for the Evolve deploy pipeline.

Each verifier asks the same question: "I just claimed to do X — is X
actually the state of the system now?" The upgrade pipeline used to
fire commands and assume they worked, which produced a class of
"deploy says success but reality says no" bugs across multiple PRs
in May 2026:

  * Plugin "rebuilt" but install dir kept the old build (PR #1146)
  * Orphan plists "removed" but actually still on disk if rm
    silently failed (PR #1144)
  * Bots "redeployed" but their gateway PIDs predated the deploy
    (no PR yet — surfaced by this module)

The pattern: every step prints "✓" the instant the underlying command
returns, never checking whether the command's effect actually
landed. This module gives each step a post-condition check it can
call right before printing "✓", so reality and reporting stay in
sync.

Each helper returns ``VerificationResult(ok, summary, detail)``.
``ok`` is the boolean for "did this work"; ``summary`` is the short
line operators see in the upgrade output; ``detail`` is the longer
diagnostic surfaced when ``ok=False``.

Dependency-light by design — no imports from deploy.py or cli.py
so verifications can be unit-tested in isolation against fake
filesystems / fake subprocess outputs.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one post-condition check.

    ``ok``      — boolean for "did this work"
    ``summary`` — one-line description shown in the upgrade summary
    ``detail``  — long-form diagnostic shown when ok=False, or empty
                  string when ok=True
    """

    ok: bool
    summary: str
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Plugin install verification
# ─────────────────────────────────────────────────────────────────────────────


def _md5_file(path: Path) -> str | None:
    """Return the hex md5 of a file. None if the file is missing or
    unreadable — callers treat that as "doesn't exist" rather than
    propagating a permission error."""
    try:
        with path.open("rb") as f:
            h = hashlib.md5()
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
            return h.hexdigest()
    except (FileNotFoundError, PermissionError, OSError):
        return None


# Files we check for the source ↔ install sync. Picked deliberately to
# cover the entry surface that mattered for the bugs we've seen:
#   * index.js — the plugin's main entry; changes whenever the public
#     plugin API changes
#   * observer/TurnObserver.js — the largest behavioral file, where
#     PR #1142's before_prompt_build hook landed and PR #1127's
#     wizard direct-send code lives; the canonical "did the new build
#     actually deploy" signal
#
# Not exhaustive — a partial / corrupt install could have some files
# match and others not. We accept that as low-probability noise; the
# bug class we're catching is "the whole install dir is stale," which
# both files reflect together.
_PLUGIN_VERIFY_FILES: tuple[str, ...] = (
    "index.js",
    "observer/TurnObserver.js",
)


def verify_plugin_install_matches_source(
    src_dist_dir: Path,
    install_dist_dir: Path,
) -> VerificationResult:
    """Post-condition for ``build_plugin``: the install location's
    compiled JS matches the source-tree compiled JS.

    Catches the failure mode that motivated this module: ``npm run
    build`` updated the source dist but the sync to the install dir
    never happened (or partially happened). Operators saw "Plugin
    rebuilt" but bots loaded the old code from the install path.

    Compares md5 of two canonical files (see ``_PLUGIN_VERIFY_FILES``).
    Both must exist and match; mismatch or missing → not ok.
    """
    mismatches: list[str] = []
    missing: list[str] = []
    for rel in _PLUGIN_VERIFY_FILES:
        src_md5 = _md5_file(src_dist_dir / rel)
        inst_md5 = _md5_file(install_dist_dir / rel)
        if src_md5 is None:
            missing.append(f"source missing: {rel}")
            continue
        if inst_md5 is None:
            missing.append(f"install missing: {rel}")
            continue
        if src_md5 != inst_md5:
            mismatches.append(
                f"{rel} (src={src_md5[:8]} install={inst_md5[:8]})"
            )

    if missing or mismatches:
        return VerificationResult(
            ok=False,
            summary="Plugin install does NOT match source build",
            detail=(
                "\n".join(missing + mismatches)
                + f"\nsource dist: {src_dist_dir}\ninstall dist: {install_dist_dir}"
            ),
        )
    return VerificationResult(
        ok=True,
        summary=f"Plugin install matches source build ({len(_PLUGIN_VERIFY_FILES)} files checked)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Process restart verification
# ─────────────────────────────────────────────────────────────────────────────


def _ps_start_epoch(pid: int) -> float | None:
    """Return the start time (seconds since epoch) of a process. None
    if the PID doesn't exist or ps doesn't behave as expected."""
    try:
        proc = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    # ``lstart`` format: "Fri May 15 14:34:53 2026"
    import time
    try:
        return time.mktime(time.strptime(proc.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def _launchctl_pid_for_label(label: str) -> int | None:
    """Look up the PID of a loaded launchd job by label, via the
    Scheduler seam. None when the job is not loaded or not running
    (``status()`` parses the ``"PID" = <n>;`` line of
    ``launchctl list <label>``; the seam's runner reports exceptions
    as failures rather than raising)."""
    from .runtime import get_scheduler
    st = get_scheduler().status(label)
    return st["pid"] if st["running"] else None


def verify_process_restarted(
    *,
    label: str,
    began_at_epoch: float,
    pid_lookup=None,
    start_time_lookup=None,
) -> VerificationResult:
    """Post-condition for a daemon restart: the process running under
    ``label`` started AFTER ``began_at_epoch``.

    ``began_at_epoch`` should be captured BEFORE the restart command
    (``launchctl kickstart -k`` etc.) runs. If the new process's
    start time is earlier, the kickstart did not actually swap the
    process and the daemon is still on old code.

    ``pid_lookup`` and ``start_time_lookup`` are injection points for
    tests; production callers use the launchctl + ps defaults.
    """
    pid_lookup = pid_lookup or _launchctl_pid_for_label
    start_time_lookup = start_time_lookup or _ps_start_epoch

    pid = pid_lookup(label)
    if pid is None:
        return VerificationResult(
            ok=False,
            summary=f"Daemon {label}: not running",
            detail=f"launchctl list {label} returned no PID — job is not loaded",
        )
    started = start_time_lookup(pid)
    if started is None:
        return VerificationResult(
            ok=False,
            summary=f"Daemon {label}: PID {pid} exists but start time unreadable",
            detail="ps -o lstart returned no parseable output",
        )
    if started < began_at_epoch:
        import time
        return VerificationResult(
            ok=False,
            summary=f"Daemon {label}: still running pre-upgrade PID {pid}",
            detail=(
                f"PID {pid} started at {time.ctime(started)}, "
                f"upgrade began at {time.ctime(began_at_epoch)} — "
                "kickstart did not actually swap the process"
            ),
        )
    return VerificationResult(
        ok=True,
        summary=f"Daemon {label}: PID {pid} restarted after upgrade",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bot gateway verification
# ─────────────────────────────────────────────────────────────────────────────


def _gateway_label_for_bot(bot_id: str) -> str:
    """OpenClaw gateway launchd label convention. Mirrors
    ``deploy.per_bot_gateway_plist_label`` but redeclared here to keep
    this module dependency-free (avoids the circular import of
    deploy_verify ← deploy ← deploy_verify)."""
    return f"ai.openclaw.{bot_id}-gateway"


def verify_bot_gateway_running_new_plugin(
    *,
    bot_id: str,
    deploy_began_at_epoch: float,
    pid_lookup=None,
    start_time_lookup=None,
) -> VerificationResult:
    """Post-condition for ``deploy_bot``: the bot's openclaw gateway
    has restarted since the deploy began (so it picked up the freshly-
    installed plugin from ``/Users/Shared/evolve-plugin/``).

    Same shape as ``verify_process_restarted`` but with the right
    label and a bot-flavored summary message."""
    return verify_process_restarted(
        label=_gateway_label_for_bot(bot_id),
        began_at_epoch=deploy_began_at_epoch,
        pid_lookup=pid_lookup,
        start_time_lookup=start_time_lookup,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rendering: produce the verification summary block for upgrade output
# ─────────────────────────────────────────────────────────────────────────────


def render_summary_block(results: list[VerificationResult]) -> str:
    """Human-readable verification summary for the end of upgrade
    output. Returns plain ANSI-free text — caller adds Rich markup if
    they want color.

    Format::

        Verification:
          ✓ Plugin install matches source build (2 files checked)
          ✓ Daemon ai.evolve.evolve.admin-ui: PID 95223 restarted after upgrade
          ✗ Daemon ai.openclaw.team_bot_a-gateway: still running pre-upgrade PID 58223
              PID 58223 started at <time>, upgrade began at <time>
    """
    lines = ["Verification:"]
    for r in results:
        marker = "✓" if r.ok else "✗"
        lines.append(f"  {marker} {r.summary}")
        if not r.ok and r.detail:
            for d in r.detail.splitlines():
                lines.append(f"      {d}")
    return "\n".join(lines)


def all_ok(results: list[VerificationResult]) -> bool:
    """Convenience for ``upgrade`` to set its exit code based on the
    overall verification outcome."""
    return all(r.ok for r in results)
