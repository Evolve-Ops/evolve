"""home_artifacts_monitor — Signal producer for bot home-dir security telemetry.

Two checks per bot:

  1. **Workspace artifacts** — sweeps each bot's ``.openclaw/`` tree for
     newly-appeared files that are either (a) large (>50 MB) or (b) carry
     a known-executable extension (``.dmg``, ``.pkg``, ``.app``, ``.sh``,
     ``.bash``, ``.command``, ``.mpkg``, ``.iso``, ``.bin``, ``.exe``,
     ``.run``). Catches the bot writing a payload into its own workspace —
     either via a misbehaved LLM session or a compromised plugin.

     This is **scope-reduced** from the watchdog's prior ``check_large_files
     _and_executables`` which scanned ``/Users/{bot_id}/`` recursively
     (catching e.g. ``~/Downloads/payload.dmg``). The broader scan
     required ``sudo /usr/bin/find /Users/*`` which carries a real
     privilege-escalation risk (``find -exec`` is a known sudo CVE
     vector). The narrower scan covers the LLM-self-tampering case —
     the high-likelihood scenario in a post-evo-separation pod where
     gateway-side compromise is the realistic threat. The user-account
     compromise case (someone with the bot account drops a payload in
     Downloads) is detected by the Quarantine check below.

  2. **Quarantine downloads** — reads each bot's
     ``~/Library/Preferences/com.apple.LaunchServices.QuarantineEventsV2``
     SQLite DB (via ``sudo /bin/cp`` to a tmp file the evolve user can
     read; the original lives under mode-0700 ``Library/``) and emits a
     Signal listing any download recorded in the last 6 hours. Catches
     drive-by downloads, payload delivery via the bot's browser, plugin
     installs that fetched a binary, etc.

Producer: ``home_artifacts_monitor``
Signal types (all ``flavor="maintenance"``, all ``severity="warn"``):

  - ``workspace_artifact_unexpected``   — bot-scoped, one per bot per cycle
  - ``recent_quarantine_download``      — bot-scoped, one per bot per cycle
  - ``quarantine_check_failed``         — bot-scoped tooling failure: the
    ``sudo cp`` snapshot of the Quarantine DB failed (most commonly a
    missing sudoers grant), so check 2 *cannot run*. Without this
    Signal a broken sudo path is indistinguishable from "no recent
    downloads".

All auto-resolve via :func:`signals.store.sweep_resolve` once the
condition clears (marker advances and the next scan finds nothing
new; quarantine entries age past the 6h lookback; the DB copy
succeeds again).

Cadence: hourly. Both checks tolerate a few cycles of latency — they're
catch-up telemetry, not page-the-operator liveness.

Pure Python, no LLM. Runs as the ``evolve`` user. Replaces the
``check_large_files_and_executables`` and ``check_quarantine_log``
checks from the out-of-tree ``/opt/homebrew/bin/openclaw-watchdog.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import bot_home, get_members, get_shared_dir, load_config
from platform_profile import get_profile
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "home_artifacts_monitor"

SIGNAL_TYPE_WORKSPACE_ARTIFACT = "workspace_artifact_unexpected"
SIGNAL_TYPE_QUARANTINE_DOWNLOAD = "recent_quarantine_download"
SIGNAL_TYPE_QUARANTINE_CHECK_FAILED = "quarantine_check_failed"

# Artifact-scan thresholds — preserved from the watchdog so behavior is
# the same; mtime markers are per-bot under shared_dir so we incrementally
# detect "new since last cycle".
LARGE_FILE_MB = 50
EXECUTABLE_EXTENSIONS = frozenset({
    ".dmg", ".pkg", ".app", ".sh", ".bash", ".command",
    ".mpkg", ".iso", ".bin", ".exe", ".run",
})
EXCLUDED_PATH_FRAGMENTS = (
    "/.git/objects/", "/.git/lfs/",
    "/.openclaw/completions/",
    "/node_modules/", "/__pycache__/", "/.npm/", "/.cache/",
)

# Quarantine DB lookback. Six hours matches the watchdog; this is "show me
# what was downloaded recently" not a forever-keep audit log.
QUARANTINE_LOOKBACK_HOURS = 6
# Source/destination shapes for the sudo cp snapshot. These MUST stay in
# lockstep with the section-3g grant in setup_wizard._render_evolve_sudoers;
# test_writer_sudoers_alignment.test_quarantine_db_copy_grant_matches_monitor
# rebuilds the grant line from these constants so either side drifting
# fails the build.
QUARANTINE_DB_PATH_TEMPLATE = (
    "/Users/{bot_user}/Library/Preferences/"
    "com.apple.LaunchServices.QuarantineEventsV2"
)
QUARANTINE_TMP_PREFIX = "evolve-quarantine-"
QUARANTINE_TMP_SUFFIX = ".sqlite"
# macOS uses Mac-epoch (2001-01-01 UTC) for LaunchServices timestamps.
APPLE_EPOCH_OFFSET = 978307200

# Max entries to include in a Signal body. Above this we summarize.
MAX_ARTIFACTS_IN_BODY = 25
MAX_DOWNLOADS_IN_BODY = 20


# ─────────────────────────────────────────────────────────────────────────────
# Detector — workspace large/exec scan
# ─────────────────────────────────────────────────────────────────────────────


def detect_workspace_artifacts(
    bot_id: str,
    workspace_root: Path,
    marker_path: Path,
    *,
    now: float | None = None,
) -> dict | None:
    """Return a Signal spec for a bot with new large/exec artifacts.

    ``marker_path`` is a sentinel file (zero bytes) whose mtime defines
    "what's new since last cycle". After the scan completes the marker
    is updated regardless of findings; that way a one-time finding
    reports once and clears on the next cycle.
    """
    now = now or time.time()
    if not workspace_root.exists() or not workspace_root.is_dir():
        return None

    cutoff = marker_path.stat().st_mtime if marker_path.exists() else 0.0
    large_bytes = LARGE_FILE_MB * 1024 * 1024
    findings: list[dict] = []

    for fpath in _iter_files_safely(workspace_root):
        s = str(fpath)
        if any(frag in s for frag in EXCLUDED_PATH_FRAGMENTS):
            continue
        try:
            st = fpath.stat()
        except OSError:
            continue
        if st.st_mtime <= cutoff:
            continue
        reasons: list[str] = []
        if st.st_size > large_bytes:
            reasons.append(f"{st.st_size // (1024 * 1024)}MB")
        if fpath.suffix.lower() in EXECUTABLE_EXTENSIONS:
            reasons.append(f"executable ({fpath.suffix.lower()})")
        if not reasons:
            continue
        findings.append({
            "path": str(fpath),
            "reasons": reasons,
            "mtime": st.st_mtime,
            "size_bytes": st.st_size,
        })

    # Always advance the marker, even when nothing was found, so a quiet
    # cycle bounds the next scan window. _touch_marker is best-effort and
    # never raises.
    _touch_marker(marker_path)

    if not findings:
        return None

    findings.sort(key=lambda f: f["mtime"], reverse=True)
    shown = findings[:MAX_ARTIFACTS_IN_BODY]
    truncated_note = (
        f"\n\n(showing {MAX_ARTIFACTS_IN_BODY} of {len(findings)} findings; "
        f"see details.findings for the full list)"
        if len(findings) > MAX_ARTIFACTS_IN_BODY
        else ""
    )
    lines = [
        f"`{bot_id}` has {len(findings)} new large/executable file"
        f"{'s' if len(findings) != 1 else ''} under "
        f"`{workspace_root}` since the last scan.",
        "",
    ]
    for f in shown:
        lines.append(f"  • `{f['path']}` — {', '.join(f['reasons'])}")
    body = "\n".join(lines) + truncated_note + (
        "\n\nThese files appeared in the bot's own workspace tree. If "
        "you don't recognize them, the most common explanations are:\n"
        "  1. A plugin install pulled a binary the bot now runs.\n"
        "  2. An LLM session wrote a payload (intended or unintended).\n"
        "  3. Backup/restore staging dropped a tarball.\n\n"
        "Audit-trace: check the workspace's git log around these "
        "files' mtimes (`cd " + str(workspace_root) + " && git log "
        "--since='6 hours ago' --name-status`) to attribute the write."
    )

    return dict(
        signature=make_signature(PRODUCER, SIGNAL_TYPE_WORKSPACE_ARTIFACT, bot_id),
        producer=PRODUCER,
        type=SIGNAL_TYPE_WORKSPACE_ARTIFACT,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: {len(findings)} new large/exec workspace file"
              f"{'s' if len(findings) != 1 else ''}",
        body=body,
        details={
            "bot_id": bot_id,
            "workspace_root": str(workspace_root),
            "findings": findings,
            "scan_cutoff_ts": cutoff,
        },
    )


def _iter_files_safely(root: Path):
    """Walk ``root`` yielding files; tolerate permission errors quietly."""
    try:
        stack = [root]
    except OSError:
        return
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError:
                continue


def _touch_marker(marker_path: Path) -> None:
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.touch(exist_ok=True)
        os.utime(marker_path, None)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Detector — Quarantine SQLite read
# ─────────────────────────────────────────────────────────────────────────────


def detect_quarantine_downloads(
    bot_id: str,
    bot_user: str,
    *,
    now: float | None = None,
    db_copier=None,
) -> dict | None:
    """Return a Signal spec when the bot has recent quarantined downloads.

    macOS's LaunchServices DB lives at mode 0644 under mode-0700
    ``~/Library/Preferences/``, so the ``evolve`` user can't open it
    directly. We snapshot it to /tmp via the narrow sudoers grant
    (``sudo /bin/cp ... /tmp/evolve-quarantine-*.sqlite``), open the
    copy read-only, then unlink.

    ``db_copier`` is injected for testing — it receives ``(src, dst)``
    paths and returns ``"ok"`` / ``"missing"`` / ``"error"``. Default is
    the real sudo cp. A ``"missing"`` source is not a finding; an
    ``"error"`` copy fires a ``quarantine_check_failed`` Signal so a
    broken sudo path is distinguishable from "no recent downloads".
    """
    now = now or time.time()
    src = Path(QUARANTINE_DB_PATH_TEMPLATE.format(bot_user=bot_user))
    copier = db_copier or _sudo_copy_quarantine_db

    fd, tmp = tempfile.mkstemp(
        dir="/tmp",
        prefix=f"{QUARANTINE_TMP_PREFIX}{bot_id}-",
        suffix=QUARANTINE_TMP_SUFFIX,
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        status = copier(src, tmp_path)
        if status == "missing":
            # Bot has never run a browser so quarantine was never
            # initialised. Not a finding.
            return None
        if status != "ok":
            return _quarantine_check_failed_spec(bot_id)

        cutoff_apple = (now - QUARANTINE_LOOKBACK_HOURS * 3600
                        - APPLE_EPOCH_OFFSET)
        try:
            with sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True) as conn:
                rows = list(conn.execute("""
                    SELECT LSQuarantineDataURLString,
                           LSQuarantineAgentName,
                           LSQuarantineTimeStamp
                    FROM LSQuarantineEvent
                    WHERE LSQuarantineTimeStamp > ?
                    ORDER BY LSQuarantineTimeStamp DESC
                """, (cutoff_apple,)))
        except sqlite3.Error as exc:
            # DB locked / corrupt / schema-changed — log but don't fire.
            print(
                f"[home_artifacts_monitor] {bot_id}: quarantine db "
                f"read failed: {exc}",
                flush=True,
            )
            return None

        if not rows:
            return None

        events: list[dict] = []
        for url, agent, ts_raw in rows:
            ts = (ts_raw or 0) + APPLE_EPOCH_OFFSET
            events.append({
                "url": url or "(no url)",
                "agent": agent or "(unknown)",
                "timestamp": ts,
                "ts_iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            })
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    shown = events[:MAX_DOWNLOADS_IN_BODY]
    truncated_note = (
        f"\n\n(showing {MAX_DOWNLOADS_IN_BODY} of {len(events)} entries; "
        f"see details.events for the full list)"
        if len(events) > MAX_DOWNLOADS_IN_BODY
        else ""
    )
    lines = [
        f"`{bot_id}` recorded {len(events)} download"
        f"{'s' if len(events) != 1 else ''} in the last "
        f"{QUARANTINE_LOOKBACK_HOURS}h via macOS LaunchServices.",
        "",
    ]
    for e in shown:
        ts_local = datetime.fromtimestamp(e["timestamp"]).astimezone().strftime(
            "%I:%M %p %Z"
        )
        lines.append(
            f"  • [{ts_local}] {e['agent']}: {e['url'][:140]}"
            f"{'…' if len(e['url']) > 140 else ''}"
        )
    body = "\n".join(lines) + truncated_note + (
        "\n\nLaunchServices records every file an installed app "
        "writes with a quarantine flag (browser downloads, AirDrop, "
        "Mail attachments, etc.). Most entries are benign. Skim the "
        "agent + URL columns — anything fetched by a non-obvious "
        "agent or from an unfamiliar host is worth a closer look."
    )

    return dict(
        signature=make_signature(PRODUCER, SIGNAL_TYPE_QUARANTINE_DOWNLOAD, bot_id),
        producer=PRODUCER,
        type=SIGNAL_TYPE_QUARANTINE_DOWNLOAD,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: {len(events)} recent download"
              f"{'s' if len(events) != 1 else ''} in last "
              f"{QUARANTINE_LOOKBACK_HOURS}h",
        body=body,
        details={
            "bot_id": bot_id,
            "lookback_hours": QUARANTINE_LOOKBACK_HOURS,
            "events": events,
        },
    )


def _quarantine_check_failed_spec(bot_id: str) -> dict:
    """Signal spec for "the quarantine check itself can't run".

    Fired when the sudo cp snapshot fails for a reason other than a
    missing source DB. Per the distinguish-tooling-failure-from-findings
    principle: silence here would make a broken sudo path read as "no
    recent downloads" forever. The signature is stable per bot, so the
    hourly re-fire dedups into one Signal, and sweep_resolve archives it
    on the first cycle where the copy works again.
    """
    return dict(
        signature=make_signature(
            PRODUCER, SIGNAL_TYPE_QUARANTINE_CHECK_FAILED, bot_id
        ),
        producer=PRODUCER,
        type=SIGNAL_TYPE_QUARANTINE_CHECK_FAILED,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: quarantine download check can't run",
        body=(
            f"The hourly Quarantine-download check for `{bot_id}` failed "
            "to snapshot ~/Library/Preferences/"
            "com.apple.LaunchServices.QuarantineEventsV2 via `sudo -n "
            "/bin/cp`. This is a tooling failure, **not** \"no recent "
            "downloads\" — recent downloads on this bot are currently "
            "invisible to the monitor.\n\n"
            "Most likely cause: /etc/sudoers.d/evolve is missing the "
            "Quarantine DB copy grant (section 3g). Re-render it with "
            "`sudo evolve-admin refresh-sudoers`, then confirm with:\n\n"
            "    sudo -l -U evolve | grep QuarantineEvents\n\n"
            "This Signal auto-resolves on the first cycle where the "
            "copy succeeds."
        ),
        details={"bot_id": bot_id},
    )


def _sudo_copy_quarantine_db(src: Path, dst: Path) -> str:
    """Snapshot the bot's quarantine DB to a tmp path readable by evolve.

    Returns ``"ok"`` on success, ``"missing"`` when the source doesn't
    exist (bot never initialised quarantine — not a finding), or
    ``"error"`` for any other failure (most commonly the sudoers grant
    in section 3g of ``_render_evolve_sudoers`` is absent, so ``sudo -n``
    dies with "a password is required"). Callers must surface ``"error"``
    — a copy that can never succeed is a tooling failure, not "no
    downloads".

    No ``src.exists()`` pre-check: ``~/Library`` is mode-0700 bot-owned,
    so the evolve user can't traverse it and ``exists()`` returns False
    even when the DB is present. cp (run as root) is the only reliable
    prober — a missing source is classified from its stderr.
    """
    try:
        r = subprocess.run(
            ["sudo", "-n", "/bin/cp", str(src), str(dst)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "error"
    if r.returncode != 0:
        # English strerror — safe in production because the LaunchDaemon
        # runs with no LANG set (C locale). An ad-hoc run under a
        # localized shell would misclassify a missing DB as "error",
        # which fails loud rather than silent.
        if "No such file or directory" in (r.stderr or ""):
            return "missing"
        return "error"
    # Ensure the evolve user can read the copy. dst was created by
    # mkstemp (owned by evolve), and cp truncates in place rather than
    # recreating, so ownership survives the root cp.
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    config: dict[str, Any],
    shared_dir: Path,
    *,
    bot_filter: str | None = None,
) -> list[dict]:
    """Run both detectors against every member bot and return specs."""
    specs: list[dict] = []
    bot_ids = (
        [bot_filter] if bot_filter else get_members(config)
    )
    for bot_id in bot_ids:
        try:
            home = bot_home(bot_id, config)
        except Exception:  # noqa: BLE001
            continue
        workspace_root = home / ".openclaw"
        marker = shared_dir / "home_artifacts" / f"{bot_id}.marker"
        try:
            artifact_spec = detect_workspace_artifacts(
                bot_id, workspace_root, marker
            )
            if artifact_spec is not None:
                specs.append(artifact_spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[home_artifacts_monitor] {bot_id}: workspace scan "
                f"crashed: {exc}",
                flush=True,
            )

        # Quarantine downloads are a macOS-only concept: the source DB is
        # ~/Library/Preferences/com.apple.LaunchServices.QuarantineEventsV2,
        # a LaunchServices artifact that does not exist on Linux. On Linux
        # the `sudo cp` of a never-present source returns "missing" today
        # (cp's "No such file or directory") — which is benign — but the
        # gate makes the no-op explicit and robust to any platform where
        # cp's error string differs (a localized/altered stderr would
        # misclassify the missing source as "error" and fire
        # `quarantine_check_failed` every hourly pass, the fresh-Linux-pod
        # false-fire this closes). macOS path is unchanged.
        if get_profile().name == "macos":
            bot_user = (config.get("bots", {})
                              .get(bot_id, {})
                              .get("user")) or bot_id
            try:
                quar_spec = detect_quarantine_downloads(bot_id, bot_user)
                if quar_spec is not None:
                    specs.append(quar_spec)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[home_artifacts_monitor] {bot_id}: quarantine "
                    f"check crashed: {exc}",
                    flush=True,
                )

    return specs


def run(
    config: dict[str, Any],
    shared_dir: Path,
    *,
    bot_filter: str | None = None,
    dry_run: bool = False,
) -> tuple[set[str], int, int]:
    specs = collect(config, shared_dir, bot_filter=bot_filter)
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        if spec["type"] == SIGNAL_TYPE_QUARANTINE_CHECK_FAILED:
            # The check couldn't run this cycle, so we can't confirm a
            # previously-firing recent_quarantine_download actually
            # cleared. Shield that signature from the sweep below —
            # otherwise breaking the tooling would mass-resolve real
            # findings. Extra entries in kept_signatures that match no
            # firing Signal are harmless.
            kept.add(make_signature(
                PRODUCER, SIGNAL_TYPE_QUARANTINE_DOWNLOAD, spec["bot_id"]
            ))
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[home_artifacts_monitor] observe failed for "
                f"{spec['signature']}: {exc}",
                flush=True,
            )

    # Restrict the sweep to the bots we actually scanned. Without this, a
    # --bot=<one> run would mass-resolve every other bot's still-firing
    # findings just because they weren't in kept_signatures.
    sweep_bots = {bot_filter} if bot_filter else None
    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                bot_ids=sweep_bots,
                reason="auto-resolve: home artifacts cleared",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[home_artifacts_monitor] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "home_artifacts_monitor — per-bot Signal producer for "
            "workspace large/exec files and recent macOS Quarantine "
            "downloads."
        ),
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=None,
        help="Override the network.json path.",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help="Override the shared dir.",
    )
    parser.add_argument(
        "--bot",
        type=str,
        default=None,
        help="Restrict scan to a single bot (default: every member bot).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Signal specs instead of writing them.",
    )
    args = parser.parse_args()

    config = load_config(str(args.network) if args.network else None)
    # get_shared_dir requires the loaded config (it reads config["sharedDir"]);
    # a no-arg call is a TypeError. config is already loaded above. (W10-G #3.)
    shared_dir = args.shared_dir or get_shared_dir(config)
    kept, n_fired, n_resolved = run(
        config, shared_dir, bot_filter=args.bot, dry_run=args.dry_run,
    )
    print(
        f"[home_artifacts_monitor] kept={len(kept)} fired={n_fired} "
        f"resolved={n_resolved}",
        flush=True,
    )


if __name__ == "__main__":
    main()
