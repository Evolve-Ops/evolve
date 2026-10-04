"""permissions.writer — atomically mutate a bot's openclaw.json on the mini.

Per CLAUDE.md, openclaw.json is owned by the bot user. The evolve user
writes via /tmp staging + ``sudo /bin/cp``; mode is preserved by chmod
+ chown. After the write the bot's gateway is kickstarted so the new
config takes effect.

For tests, ``write_openclaw_fields`` accepts an optional ``home_override``
that bypasses sudo (direct write to a synthetic ~/.openclaw tree).

The helper is field-set aware: it accepts a dotpath → value mapping,
walks the JSON tree, sets each path (creating intermediate dicts as
needed), and atomically swaps the file. Caller is responsible for
validating which paths are allowed — the writer just writes.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from runtime.scheduler import get_scheduler


def _dotpath_set(obj: dict, dotpath: str, value: Any) -> None:
    """Set ``obj[a][b][c] = value`` for dotpath "a.b.c", creating dicts."""
    parts = dotpath.split(".")
    cur = obj
    for seg in parts[:-1]:
        if seg not in cur or not isinstance(cur[seg], dict):
            cur[seg] = {}
        cur = cur[seg]
    cur[parts[-1]] = value


def _dotpath_unset(obj: dict, dotpath: str) -> None:
    """Remove obj[a][b][c] if present; leave empty intermediates intact."""
    parts = dotpath.split(".")
    cur = obj
    for seg in parts[:-1]:
        if not isinstance(cur, dict) or seg not in cur:
            return
        cur = cur[seg]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def read_openclaw_json(
    path: Path,
    *,
    timeout: float = 5.0,
) -> dict | None:
    """Read openclaw.json with direct-read + sudo /bin/cat fallback.

    Returns the parsed dict, or None if unreadable / malformed.
    Mirrors the inventory module's reader.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
            if r.returncode != 0:
                return None
            text = r.stdout
        except (subprocess.TimeoutExpired, OSError):
            return None
    except OSError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def write_openclaw_fields(
    bot_id: str,
    field_updates: dict[str, Any],
    *,
    field_unsets: list[str] | None = None,
    home_override: Path | None = None,
    bot_user: str | None = None,
    kickstart: bool = True,
) -> tuple[bool, str]:
    """Apply a dotpath → value mapping to a bot's openclaw.json atomically.

    Steps:
      1. Read current config (with sudo /bin/cat fallback).
      2. Deep-copy + apply each field update / unset.
      3. Serialize, write to /tmp with mkstemp.
      4. ``sudo /bin/cp`` the staged file onto the target path.
      5. ``sudo /usr/sbin/chown`` to bot:staff, ``sudo /bin/chmod 600``
         (openclaw.json is token-bearing — never world-readable).
      6. Gateway restart via the Scheduler seam (``kickstart -k`` on launchd).

    On ``home_override`` (test mode), steps 4–6 collapse into a direct
    Python file write and no kickstart.

    Returns (ok, message).
    """
    home = home_override or (Path("/Users") / (bot_user or bot_id))
    oc_path = home / ".openclaw" / "openclaw.json"

    current = read_openclaw_json(oc_path)
    if current is None:
        return False, f"openclaw.json unreadable at {oc_path}"

    updated = deepcopy(current)
    for dotpath, value in field_updates.items():
        _dotpath_set(updated, dotpath, value)
    for dotpath in (field_unsets or []):
        _dotpath_unset(updated, dotpath)

    serialized = json.dumps(updated, indent=2, sort_keys=False)

    # Direct write path (tests, or evolve user has write ACL)
    if home_override is not None:
        oc_path.write_text(serialized)
        return True, f"Wrote {oc_path} directly (test mode)."

    # Production path: /tmp staging + sudo /bin/cp
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(serialized)
        cp = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(oc_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return False, f"sudo cp failed: rc={cp.returncode} stderr={cp.stderr.strip()}"
        owner = bot_user or bot_id
        ch = subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{owner}:staff", str(oc_path)],
            capture_output=True, text=True, timeout=10,
        )
        if ch.returncode != 0:
            return False, f"sudo chown failed: rc={ch.returncode} stderr={ch.stderr.strip()}"
        # 0600, not 0644: openclaw.json holds the gateway + channel tokens, so it
        # must not be world-readable. The bot reads it as owner (chown above); the
        # evolve admin reads it via the inherited .openclaw ACL. Grant: §4 of
        # _render_evolve_sudoers (security fix 2026-06-12). NOTE: requires the new
        # sudoers to be live (`refresh-sudoers`) — otherwise this chmod is denied
        # and the write fails loudly here rather than silently leaving it 0644.
        cm = subprocess.run(
            ["sudo", "/bin/chmod", "600", str(oc_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cm.returncode != 0:
            return False, f"sudo chmod failed: rc={cm.returncode} stderr={cm.stderr.strip()}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if kickstart:
        # Scheduler-seam restart (kickstart -k). Result deliberately ignored:
        # restart can fail if the service is not loaded — that isn't a write
        # failure, so we don't bubble it as an error.
        get_scheduler().restart(f"ai.openclaw.{bot_id}-gateway")

    return True, f"Wrote {oc_path} ({len(field_updates)} field(s))."


# ── exec-approvals.json + cron/jobs.json writers ─────────────────────────────

def _bot_owned_write(
    bot_id: str,
    target_path: Path,
    serialized: str,
    *,
    bot_user: str | None = None,
    home_override: Path | None = None,
    kickstart_gateway: bool = True,
) -> tuple[bool, str]:
    """Shared bot-owned file writer — /tmp staging + sudo /bin/cp + chown + chmod."""
    if home_override is not None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(serialized)
        return True, f"Wrote {target_path} directly (test mode)."

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(serialized)
        cp = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(target_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return False, f"sudo cp failed: rc={cp.returncode} stderr={cp.stderr.strip()}"
        owner = bot_user or bot_id
        ch = subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{owner}:staff", str(target_path)],
            capture_output=True, text=True, timeout=10,
        )
        if ch.returncode != 0:
            return False, f"sudo chown failed: rc={ch.returncode} stderr={ch.stderr.strip()}"
        cm = subprocess.run(
            ["sudo", "/bin/chmod", "644", str(target_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cm.returncode != 0:
            return False, f"sudo chmod failed: rc={cm.returncode} stderr={cm.stderr.strip()}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if kickstart_gateway:
        # Scheduler-seam restart; result ignored (not loaded ≠ write failure).
        get_scheduler().restart(f"ai.openclaw.{bot_id}-gateway")

    return True, f"Wrote {target_path}."


def _bot_owned_read(path: Path, *, timeout: float = 5.0) -> dict | None:
    """Shared bot-owned JSON reader — direct read + sudo /bin/cat fallback."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
            if r.returncode != 0:
                return None
            text = r.stdout
        except (subprocess.TimeoutExpired, OSError):
            return None
    except OSError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def read_exec_approvals(
    bot_id: str,
    *,
    home_override: Path | None = None,
) -> dict | None:
    home = home_override or (Path("/Users") / bot_id)
    return _bot_owned_read(home / ".openclaw" / "exec-approvals.json")


def write_exec_approvals(
    bot_id: str,
    new_obj: dict,
    *,
    home_override: Path | None = None,
    kickstart_gateway: bool = True,
) -> tuple[bool, str]:
    home = home_override or (Path("/Users") / bot_id)
    target = home / ".openclaw" / "exec-approvals.json"
    return _bot_owned_write(
        bot_id, target, json.dumps(new_obj, indent=2, sort_keys=False),
        home_override=home_override,
        kickstart_gateway=kickstart_gateway,
    )


def read_cron_jobs(
    bot_id: str,
    *,
    home_override: Path | None = None,
) -> dict | None:
    home = home_override or (Path("/Users") / bot_id)
    return _bot_owned_read(home / ".openclaw" / "cron" / "jobs.json")


def write_cron_jobs(
    bot_id: str,
    new_obj: dict,
    *,
    home_override: Path | None = None,
    kickstart_gateway: bool = True,
) -> tuple[bool, str]:
    home = home_override or (Path("/Users") / bot_id)
    target = home / ".openclaw" / "cron" / "jobs.json"
    if home_override is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
    return _bot_owned_write(
        bot_id, target, json.dumps(new_obj, indent=2, sort_keys=False),
        home_override=home_override,
        kickstart_gateway=kickstart_gateway,
    )
