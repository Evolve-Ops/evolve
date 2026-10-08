"""Daily launchd-stdout rotator for OC gateway logs.

Background
----------
OC's own logger writes to ``logging.file`` (typically
``/Users/<user>/.openclaw/logs/openclaw.log``) and rotates at
``logging.maxFileBytes`` × 5 archives. But OC also writes startup
banners, occasional unhandled errors, and anything emitted before its
logger initializes to stdout/stderr. The per-bot gateway plist (owned
by the OC installer, not Evolve) captures those into
``/Users/<user>/.openclaw/logs/gateway.log`` and ``gateway.err.log``.
launchd has no built-in rotation for ``StandardOut/ErrorPath``, so
those files grow forever.

Once ``logging.file`` is set (seeded by setup_wizard and gap-filled
by ``deploy.ensure_plugin_config``), the launchd-stdout remainder is
slow — startup banner, occasional crashes. This rotator keeps that
remainder bounded by truncating to zero any time a file crosses the
threshold. Truncation (not rotation) is fine here: the bounded
content is OC's own ``openclaw.log`` archives; gateway.log/.err.log
hold only what OC couldn't route through its logger, and that's
typically transient noise we don't need to keep.

Access model
------------
``gateway.log`` and ``gateway.err.log`` are bot-owned mode 600.
evolve has ACL read but not write. ``/usr/bin/truncate -s 0 <file>``
runs under ``sudo`` via a narrow sudoers grant pinned to the two
specific filenames under ``/Users/*/.openclaw/logs/``.

Separate from ``packages/analyzer/log_cap.py`` because that module's
in-process ``os.truncate`` + ``shutil.copyfile`` need write access
to the file and parent dir — fine for evolve-owned shared logs
(``/Users/Shared/evolve/logs/audit.log`` etc.), impossible for
bot-owned gateway logs. Once the bot-side ACL inverts (granting
evolve write under ``.openclaw/logs/``) the two paths can merge.

Bot enumeration
---------------
network.json is the only source of truth (per
``feedback_explicit_pod_membership``). For each bot, we resolve the
macOS account name via ``get_bot_user`` (bot_id may differ from the
account name; see ``feedback_bot_id_not_account_name``).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .applications.infra_audit import _is_sudo_escalation_error

# 10 MiB — well below the noise floor where launchd-stdout would
# actually matter, and far enough from the 25MB logging.file ceiling
# that the rotator doesn't trip on transient gateway-startup spikes.
_THRESHOLD_BYTES = 10 * 1024 * 1024

# Limited to the two specific files the gateway plist captures.
# Matches the sudoers grant in setup_wizard._render_evolve_sudoers.
_TARGETS = ("gateway.log", "gateway.err.log")


def _file_size_with_sudo(path: Path) -> int | None:
    """Return size in bytes, or None if the file can't be stat'd.

    Tries direct stat first (ACL read is enough for st_size on macOS).
    No sudo fallback needed — the parent dir is bot-owned drwxr-xr-x+
    with an evolve read ACL, so evolve can stat children directly.
    """
    try:
        return path.stat().st_size
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _truncate(path: Path) -> tuple[bool, str]:
    """sudo -n /usr/bin/truncate -s 0 <path>. Returns (ok, message).

    ``-n`` so a missing NOPASSWD grant fails immediately with a
    classifiable "a password is required" stderr instead of prompting
    (manual TTY runs would otherwise hang into the timeout).
    """
    r = subprocess.run(
        ["sudo", "-n", "/usr/bin/truncate", "-s", "0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip() or "truncate failed"
    return True, "truncated"


def rotate_one_bot(bot_id: str, bot_user: str) -> list[dict]:
    """Check the two target files for one bot; truncate any that exceed
    the threshold. Returns a list of per-file result dicts.
    """
    home = Path(f"/Users/{bot_user}")
    logs_dir = home / ".openclaw" / "logs"
    results: list[dict] = []
    for name in _TARGETS:
        path = logs_dir / name
        size = _file_size_with_sudo(path)
        if size is None:
            results.append({"bot": bot_id, "file": str(path), "skipped": "stat_failed"})
            continue
        if size <= _THRESHOLD_BYTES:
            continue
        ok, msg = _truncate(path)
        results.append({
            "bot": bot_id, "file": str(path),
            "size_before": size, "ok": ok, "message": msg,
        })
    return results


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report which files would be truncated, don't write.")
    args = parser.parse_args(argv)

    from .config import get_bot_user, load_network
    network = load_network()
    bots = network.get("bots") or {}
    if not bots:
        print("[oc-log-rotate] network.json has no bots; nothing to do.")
        return 0

    all_results: list[dict] = []
    for bot_id in sorted(bots.keys()):
        try:
            bot_user = get_bot_user(bot_id, network)
        except Exception as e:  # noqa: BLE001
            all_results.append({"bot": bot_id, "skipped": f"resolve_user_failed: {e}"})
            continue
        if args.dry_run:
            logs_dir = Path(f"/Users/{bot_user}") / ".openclaw" / "logs"
            for name in _TARGETS:
                size = _file_size_with_sudo(logs_dir / name)
                if size is not None and size > _THRESHOLD_BYTES:
                    print(f"[oc-log-rotate] would truncate: {logs_dir / name} ({size} bytes)")
        else:
            all_results.extend(rotate_one_bot(bot_id, bot_user))

    truncated = [r for r in all_results if r.get("ok")]
    failed = [r for r in all_results if r.get("ok") is False]
    skipped = [r for r in all_results if "skipped" in r]
    for r in truncated:
        print(f"[oc-log-rotate] truncated: {r['file']} (was {r['size_before']} bytes)")
    for r in failed:
        hint = ""
        if _is_sudo_escalation_error(r["message"]):
            hint = (" — sudo denied; run `sudo evolve-admin refresh-sudoers` "
                    "to reinstall the truncate grants")
        print(f"[oc-log-rotate] FAILED to truncate {r['file']} "
              f"({r['size_before']} bytes): {r['message']}{hint}", file=sys.stderr)
    for r in skipped:
        print(f"[oc-log-rotate] skipped {r['bot']}: {r['skipped']}")
    print(f"[oc-log-rotate] done. truncated={len(truncated)} "
          f"failed={len(failed)} skipped={len(skipped)}")
    # Non-zero on truncation failure so launchd/operator tooling sees it —
    # the file is over threshold and still growing; exiting 0 here is how
    # the #1956 grant revert went unnoticed. Skips stay exit-0: stat_failed
    # is normal for bots without gateway logs yet.
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover — launchd entry point
    sys.exit(_main(sys.argv[1:]))
