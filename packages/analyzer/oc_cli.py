"""
oc_cli.py — Thin wrapper for running `openclaw <cmd> --json` as bot users.

All cross-bot OC CLI invocations go through this module.
Never call openclaw directly from other modules.

Usage:
    from oc_cli import oc_command, oc_status, oc_health, oc_models, oc_cron_list

    status = oc_status("admin_bot")   # runs: sudo -u admin_bot openclaw status --json
    models = oc_models("team_bot_a")     # runs: sudo -u team_bot_a openclaw models list --json

Standalone CLI:
    python3 oc_cli.py admin_bot status --json
    python3 oc_cli.py team_bot_a models list --json
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from platform_profile import find_openclaw_cli

# ── Cache + in-flight dedup ────────────────────────────────────────────────────
#
# The cache is shared across threads so admin-ui (Flask, multi-threaded) can
# coalesce concurrent identical requests instead of fan-out-spawning one
# `sudo openclaw status --json` per HTTP poll. Without this, a slow openclaw
# pile-up wedges the host (see issues/open/2026-04-26-010-admin-ui-oc-cli-pileup.md).

_cache: dict[str, tuple[float, Any]] = {}  # key → (timestamp, result_or_FAILED)
_CACHE_TTL = 30      # seconds — positive cache (successful result)
_NEG_CACHE_TTL = 60  # seconds — negative cache (timeout / error); longer than positive
                     # to avoid hammering an already-slow bot

# Sentinel for negative cache entries — lets us distinguish "we tried this and
# it failed" from "we haven't tried yet" without storing exceptions.
_FAILED = object()

# Per-key locks ensure only one subprocess is in flight per (bot,user,args).
# Concurrent callers wait on the lock and then read the freshly populated cache.
_inflight_locks: dict[str, threading.Lock] = {}
_inflight_locks_guard = threading.Lock()  # guards _inflight_locks itself

# Cache-lookup miss sentinel — distinct from None (which is a valid result of
# `oc_command(..., cache_ttl=0)` callers) and from _FAILED.
_MISS = object()


def _get_inflight_lock(key: str) -> threading.Lock:
    """Return a per-key lock, creating it on first use. Bounded by the small
    number of distinct (bot,args) tuples the system uses (~few dozen)."""
    with _inflight_locks_guard:
        lock = _inflight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _inflight_locks[key] = lock
        return lock


def _cache_lookup(key: str, ttl: int) -> Any:
    """Return cached value (or _FAILED) if fresh, else _MISS. Negative entries
    use the longer _NEG_CACHE_TTL regardless of caller's ttl."""
    entry = _cache.get(key)
    if entry is None:
        return _MISS
    ts, val = entry
    age = time.time() - ts
    if val is _FAILED:
        return _FAILED if age < _NEG_CACHE_TTL else _MISS
    return val if age < ttl else _MISS

# ── Network config resolution ──────────────────────────────────────────────────

_DEFAULT_NETWORK_PATHS = [
    "/Users/Shared/evolve/network.json",
]

_bots_cache: dict[str, Any] | None = None
_bots_cache_path: str | None = None


def _canonical_network_path() -> "str | None":
    """The PLATFORM's ``{shared_dir}/network.json``, or None if unresolvable.

    ``_DEFAULT_NETWORK_PATHS`` is macOS-shaped. On the Linux pod the canonical
    shared dir is ``/var/lib/evolve`` (``platform_profile.shared_dir_default``),
    so a caller that passes no ``network_path`` and has no ``EVOLVE_NETWORK``
    in its environment resolved NO roster there. Daemons get ``EVOLVE_NETWORK``
    from their jobspec, but an operator CLI does not — and ``sudo`` env_resets
    it anyway. That was harmless while the roster guard was fail-open (an empty
    roster skipped the check); with the guard failing closed it would refuse
    legitimate writes, so the default must be platform-correct. Lazy + tolerant
    so oc_cli keeps working wherever ``evolve_config`` isn't importable.
    """
    try:
        from evolve_config import CANONICAL_NETWORK_JSON  # type: ignore
        return str(CANONICAL_NETWORK_JSON)
    except Exception:
        return None


def _candidate_network_paths(network_path: str | None = None) -> list[str]:
    """Ordered network.json locations to try: explicit → env → pod → dev-local."""
    paths: list[str] = []
    if network_path:
        paths.append(network_path)
    env = os.environ.get("EVOLVE_NETWORK")
    if env:
        paths.append(env)
    canonical = _canonical_network_path()
    if canonical:
        paths.append(canonical)
    paths.extend(_DEFAULT_NETWORK_PATHS)
    # Dev-mode fallback: network.json adjacent to this file
    paths.append(str(Path(__file__).parent / "network.json"))
    # Preserve order, drop duplicates (on macOS the canonical path IS the
    # legacy default, so the two collapse to one).
    return list(dict.fromkeys(paths))


def _pod_auto_upgrade_block(network_path: str | None = None) -> dict:
    """Return ``network.json::models.autoUpgrade``, or ``{}`` when absent.

    Read fresh (no cache) rather than off ``_bots_cache``: this fires only on
    tier writes, which already spawn a sudo subprocess, so one small read is
    noise — and a stale auto-upgrade posture is exactly the thing we are
    trying not to hand to a bot. See ``oc_model.POD_AUTO_UPGRADE_KEY``.
    """
    for p in _candidate_network_paths(network_path):
        try:
            data = json.loads(Path(p).read_text())
            models = data.get("models")
        except Exception:
            # Inside the try like ``_load_bots``' ``data.get("bots", {})``: a
            # network.json that is valid JSON but not an object would otherwise
            # raise AttributeError out of oc_full_config_set_with_error, which
            # is contracted to return ``(None, msg)``. Fall through instead.
            continue
        block = models.get("autoUpgrade") if isinstance(models, dict) else None
        return dict(block) if isinstance(block, dict) else {}
    return {}


def _load_bots(network_path: str | None = None) -> dict:
    """Load bots config from network.json. Returns bots dict (empty on failure)."""
    global _bots_cache, _bots_cache_path

    for p in _candidate_network_paths(network_path):
        if _bots_cache is not None and _bots_cache_path == p:
            return _bots_cache
        try:
            data = json.loads(Path(p).read_text())
            bots = data.get("bots", {})
            _bots_cache = bots
            _bots_cache_path = p
            return bots
        except Exception:
            pass
    return {}


def _resolve_user(bot_id: str, network_path: str | None = None) -> str:
    """Resolve bot_id → macOS username via network.json bots[bot_id].user (fallback: bot_id)."""
    bots = _load_bots(network_path)
    return bots.get(bot_id, {}).get("user", bot_id)


def _resolve_role(bot_id: str, network_path: str | None = None) -> str | None:
    """Resolve bot_id → role ("primary" or "member") via network.json bots[bot_id].role.

    Returns None when network.json is unreadable or the bot has no
    explicit role. Used by oc_full_config_set to thread role through to
    oc_model.py so the writer can derive a role-aware default tierCascade
    (see L2 of the 2026-05-29 tier audit — member bots burning Sonnet
    primary because the legacy default was always workhorse-first).
    """
    bots = _load_bots(network_path)
    role = bots.get(bot_id, {}).get("role")
    if role in ("primary", "member"):
        return role
    return None


# ── sudo detection ─────────────────────────────────────────────────────────────

def _should_sudo(target_user: str) -> bool:
    """
    Return True if we need sudo to run as target_user.
    If we're already running as that user, skip sudo.
    """
    try:
        target_uid = pwd.getpwnam(target_user).pw_uid
        return os.getuid() != target_uid
    except KeyError:
        # User not found locally — try sudo anyway (might be remote/network user)
        return True


def _sudo_python() -> str:
    """Return a python3 interpreter that bot users can execute via sudo.

    Homebrew (/opt/homebrew) and venv pythons are only executable by the
    installing user. Bot users need a world-executable interpreter.
    oc_model.py and oc_keys.py only use stdlib, so the macOS system python
    (/usr/bin/python3) is sufficient and is always world-executable.

    Search order:
      1. /usr/bin/python3          — macOS system python (CLT), world-executable
      2. /usr/local/bin/python3    — fallback system location
      3. PATH excluding venv/Homebrew dirs — last resort
    """
    # Prefer system python locations that are world-executable
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # PATH fallback: skip venv and Homebrew bin dirs
    skip_prefixes = (
        os.path.join(os.environ.get("VIRTUAL_ENV", "\x00"), "bin"),
        "/opt/homebrew",
        "/usr/local/opt",
    )
    for d in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if any(d.startswith(p) for p in skip_prefixes):
            continue
        candidate = os.path.join(d, "python3")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return sys.executable


def _resolve_home_dir(user: str) -> str:
    """Resolve cwd for the openclaw subprocess.

    pwd first (production has real entries on both platforms); when the
    account has no passwd entry — dev machines without bot accounts, paths
    computed before account creation — fall back to the platform profile's
    home root, i.e. ``/Users/{user}`` on macOS and ``/home/{user}`` on Linux.
    Never hardcode ``/Users``: an unresolvable cwd makes ``subprocess.run``
    raise ``FileNotFoundError`` before the command runs, which surfaces as a
    bogus "[Errno 2] No such file or directory: '/Users/<bot>'" on a Linux pod.

    The returned dir is also guaranteed to EXIST — openclaw is a Node binary
    and Node calls ``uv_cwd()`` during startup, so a missing (or
    bot-untraversable) cwd kills the CLI before it emits anything. Falls back
    to ``/tmp``, which every account can traverse. Tests can patch this seam.
    """
    home: "str | None" = None
    try:
        import pwd as _pwd
        home = _pwd.getpwnam(user).pw_dir
    except Exception:
        try:
            from platform_profile import get_profile  # local import: optional dep
            home = str(Path(get_profile().user_home_root) / user)
        except Exception:
            home = None
    # Path.is_dir() already absorbs OSError/ValueError into False, so a weird
    # or unreachable path needs no guard here.
    if home and Path(home).is_dir():
        return home
    return "/tmp"


# ── CLI-misinvocation guard ────────────────────────────────────────────────────
#
# When openclaw rejects an argument shape (missing requiredOption,
# unknown flag, ...) the caller is using the CLI wrong — almost always
# because OpenClaw renamed/required a flag and the evolve caller hasn't
# caught up. The diagnosis doc (internal/diagnosis-spend-alert-openclaw-flag-2026-05-21.md
# § "The Plex-test failure") explains why this needed a louder surface:
# spend_alert.py hit this for 3 weeks via a "log to file and return
# None" silent failure, and a non-expert operator had no way to find out.


def _resolve_shared_dir(network_path: str | None) -> "Path | None":
    """Best-effort lookup of sharedDir for Signal writes. Returns None
    if the config can't be loaded; caller no-ops on the Signal emit."""
    try:
        from evolve_config import get_shared_dir, load_config  # local import

        cfg = load_config(network_path) if network_path else load_config()
        return Path(get_shared_dir(cfg))
    except Exception:
        return None


def _caller_module() -> str:
    """Return the first stack frame outside oc_cli + signals — the
    evolve module that issued the broken CLI call.

    Strips ``.py`` and returns just the basename; nested package paths
    aren't useful for the signature.
    """
    import inspect

    here = Path(__file__).resolve()
    signals_dir = here.parent / "signals"
    for frame in inspect.stack()[1:]:
        try:
            fpath = Path(frame.filename).resolve()
        except (OSError, ValueError):
            continue
        if fpath == here:
            continue
        if signals_dir in fpath.parents:
            continue
        return fpath.stem
    return "unknown"


def _maybe_emit_misinvocation(
    args: list[str],
    stderr_text: str,
    network_path: str | None,
) -> None:
    """If stderr matches a CLI-shape error pattern, fire a Signal.

    Best-effort. Never raises — the guard must not break the underlying
    CLI caller. Subcommand is just the first arg (e.g. ``cron``, ``message``).
    """
    try:
        from signals.cli_misinvocation import (
            classify_stderr,
            observe_misinvocation,
        )

        pattern_key = classify_stderr(stderr_text)
        if pattern_key is None:
            return
        shared = _resolve_shared_dir(network_path)
        if shared is None:
            return
        subcommand = " ".join(a for a in args[:2] if not a.startswith("-")) or "?"
        observe_misinvocation(
            shared_dir=shared,
            caller_module=_caller_module(),
            oc_subcommand=subcommand,
            pattern_key=pattern_key,
            stderr_snip=stderr_text[:300].strip(),
            cmd_args=list(args),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[oc_cli] misinvocation-guard error (non-fatal): {exc}", file=sys.stderr)


def _run_oc_subprocess(
    cmd: list[str],
    cwd: str,
    timeout: float,
) -> "tuple[bytes, bytes, int | None, bool, dict | list | None]":
    """Spawn openclaw, capture stdout/stderr non-blockingly, return early on parseable JSON.

    Returns ``(stdout_bytes, stderr_bytes, returncode, timed_out, parsed_early)``
    where ``parsed_early`` is the JSON object decoded as soon as stdout
    became parseable (and the process was killed pre-emptively to avoid
    hanging on background children — see comment below). When
    ``parsed_early`` is None, the caller is responsible for parsing
    ``stdout_bytes`` itself and inspecting ``returncode``/``timed_out``
    to decide success vs failure.

    This is the swappable seam tests use to bypass real subprocess
    invocation while still exercising oc_cli's locking + caching.

    `openclaw security audit` (and potentially other subcommands) spawn
    a background child process (openclaw-security) that inherits stdout
    and runs indefinitely. communicate() waits for ALL holders of the
    write end to close it, which never happens. killpg would fix this,
    but the evolve user lacks permission to kill processes owned by
    other bot users.

    Fix: use select() to read stdout non-blockingly. openclaw writes its
    JSON output quickly (< 2s) before spawning the background child.
    Once we have parseable JSON we return immediately without waiting
    for EOF, leaving the background process running until the OS
    reclaims it or the next audit run kills the parent.
    """
    import os as _os
    import select as _select
    import signal as _signal

    def _kill_pg(p: "subprocess.Popen") -> None:
        """Kill the process group. Falls back to sudo /bin/kill when the
        group is owned by another user (e.g. a bot user spawned via sudo)."""
        try:
            pgid = _os.getpgid(p.pid)
            _os.killpg(pgid, _signal.SIGKILL)
        except PermissionError:
            # Process group is owned by a different user (bot user via sudo).
            # Use our sudoers grant to kill it as root.
            try:
                pgid = _os.getpgid(p.pid)
                subprocess.run(
                    ["sudo", "/bin/kill", "-9", f"-{pgid}"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
            try:
                p.kill()
            except Exception:
                pass
        except (ProcessLookupError, OSError):
            try:
                p.kill()
            except Exception:
                pass

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
    )
    stdout_buf = b""
    stderr_buf = b""
    deadline = time.time() + timeout
    timed_out = False

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            break
        try:
            r, _, _ = _select.select(
                [proc.stdout, proc.stderr], [], [], min(remaining, 0.5)
            )
        except (ValueError, OSError):
            break

        if not r:
            if proc.poll() is not None:
                break
            continue

        for fd in r:
            try:
                chunk = _os.read(fd.fileno(), 65536)
            except OSError:
                chunk = b""
            if chunk:
                if fd is proc.stdout:
                    stdout_buf += chunk
                else:
                    stderr_buf += chunk

        if stdout_buf:
            try:
                parsed = json.loads(stdout_buf.decode("utf-8", errors="replace"))
                _kill_pg(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                return stdout_buf, stderr_buf, proc.poll(), False, parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        if proc.poll() is not None:
            break

    _kill_pg(proc)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass

    return stdout_buf, stderr_buf, proc.poll(), timed_out, None


def _find_openclaw() -> str | None:
    """Find the openclaw binary, or None if it isn't installed.

    Thin delegate to :func:`platform_profile.find_openclaw_cli` — the single
    home for the candidate list (PATH first, then the six fixed absolute
    candidates). This module used to carry its own copy, which omitted the
    Linux ``/usr/bin/openclaw`` symlink; the wrapper stays only so the three
    call sites below keep reading as one local name.

    PATH-first matters because the canonical user-facing path on Apple Silicon
    Homebrew is a symlink (/opt/homebrew/bin/openclaw → ../lib/node_modules/
    openclaw/openclaw.mjs) while evolve's launchd PATH omits /opt/homebrew/bin
    entirely — so ``which()`` alone returns None even when the binary exists.
    """
    return find_openclaw_cli()


# ── Core command runner ────────────────────────────────────────────────────────

def oc_command(
    bot_id: str,
    args: list[str],
    timeout: int = 15,
    user: str | None = None,
    cache_ttl: int = _CACHE_TTL,
    network_path: str | None = None,
    _err_out: "list | None" = None,
) -> "dict | list | None":
    """
    Run: sudo -u {user} openclaw {args...}

    - Resolves bot_id → username via network.json (fallback: bot_id itself)
    - Parses JSON output
    - Returns parsed result or None on any failure
    - Caches results for cache_ttl seconds (default 30s); pass cache_ttl=0 to skip
    - Never raises
    """
    if user is None:
        user = _resolve_user(bot_id, network_path)

    cache_key = f"{bot_id}:{user}:{' '.join(args)}"

    # Fast path: cache hit (positive or negative) without taking any lock.
    if cache_ttl > 0:
        cached = _cache_lookup(cache_key, cache_ttl)
        if cached is not _MISS:
            return None if cached is _FAILED else cached

    # Slow path: serialise concurrent callers for this exact key. The first
    # one runs the subprocess; the rest wait on the lock and then read the
    # freshly populated cache. This prevents fan-out spawning under load.
    lock = _get_inflight_lock(cache_key)
    with lock:
        # Re-check cache now that we hold the lock — another thread may have
        # populated it while we were waiting.
        if cache_ttl > 0:
            cached = _cache_lookup(cache_key, cache_ttl)
            if cached is not _MISS:
                return None if cached is _FAILED else cached

        oc = _find_openclaw()
        if oc is None:
            msg = "openclaw not found in PATH"
            print(f"[oc_cli] {msg} for bot={bot_id}", file=sys.stderr)
            if _err_out is not None:
                _err_out.append(msg)
            # Don't negative-cache "openclaw not in PATH" — it's an env bug,
            # not a per-bot symptom; caller should see the failure every time.
            return None

        cmd: list[str]
        if _should_sudo(user):
            cmd = ["sudo", "-H", "-u", user, oc] + args
        else:
            cmd = [oc] + args

        cwd = _resolve_home_dir(user)
        stdout_buf, stderr_buf, returncode, timed_out, parsed_early = (
            _run_oc_subprocess(cmd, cwd, timeout)
        )
        if parsed_early is not None:
            _cache[cache_key] = (time.time(), parsed_early)
            return parsed_early

        stdout_text = stdout_buf.decode("utf-8", errors="replace")
        stderr_text = stderr_buf.decode("utf-8", errors="replace")

        try:
            if timed_out and not stdout_text.strip():
                msg = f"timed out after {timeout}s"
                print(f"[oc_cli] bot={bot_id} cmd={args} {msg}", file=sys.stderr)
                if _err_out is not None:
                    _err_out.append(msg)
                _cache[cache_key] = (time.time(), _FAILED)
                return None

            rc = returncode
            if rc is not None and rc not in (0, -9) and not stdout_text.strip():
                stderr_snip = stderr_text[:300].strip()
                print(
                    f"[oc_cli] bot={bot_id} cmd={args} exit={rc} "
                    f"stderr={stderr_snip!r}",
                    file=sys.stderr,
                )
                if _err_out is not None:
                    detail = stderr_snip or f"exit {rc}"
                    _err_out.append(detail)
                _cache[cache_key] = (time.time(), _FAILED)
                # CLI-misinvocation guard: when stderr matches a Commander
                # CLI shape-error pattern (missing requiredOption, unknown
                # flag, ...), an evolve caller is using OC wrong — fire a
                # Signal so it surfaces on the Alerts page instead of just
                # logging to a file. See diagnosis-spend-alert-openclaw-flag-2026-05-21.md.
                _maybe_emit_misinvocation(args, stderr_text, network_path)
                return None

            parsed = json.loads(stdout_text)
            _cache[cache_key] = (time.time(), parsed)
            return parsed
        except json.JSONDecodeError as e:
            msg = f"JSON parse error: {e}"
            print(f"[oc_cli] bot={bot_id} cmd={args} {msg}", file=sys.stderr)
            if _err_out is not None:
                _err_out.append(msg)
            _cache[cache_key] = (time.time(), _FAILED)
        except Exception as e:
            msg = str(e)
            print(f"[oc_cli] bot={bot_id} cmd={args} error: {msg}", file=sys.stderr)
            if _err_out is not None:
                _err_out.append(msg)
            _cache[cache_key] = (time.time(), _FAILED)
        return None


def oc_command_raw(
    bot_id: str,
    args: list[str],
    timeout: int = 15,
    network_path: str | None = None,
) -> "str | None":
    """
    Same as oc_command but returns raw stdout string (for non-JSON commands).
    Never raises.
    """
    user = _resolve_user(bot_id, network_path)
    oc = _find_openclaw()
    if oc is None:
        print(f"[oc_cli] openclaw not found in PATH for bot={bot_id}", file=sys.stderr)
        return None

    cmd: list[str]
    if _should_sudo(user):
        cmd = ["sudo", "-H", "-u", user, oc] + args
    else:
        cmd = [oc] + args

    _home = _resolve_home_dir(user)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=_home)
        if proc.returncode != 0:
            print(
                f"[oc_cli] raw bot={bot_id} cmd={args} exit={proc.returncode}",
                file=sys.stderr,
            )
            return None
        return proc.stdout
    except Exception as e:
        print(f"[oc_cli] raw bot={bot_id} cmd={args} error: {e}", file=sys.stderr)
        return None


# ── Convenience wrappers ───────────────────────────────────────────────────────

def oc_status(bot_id: str) -> "dict | None":
    """openclaw status --json — returns full status dict"""
    return oc_command(bot_id, ["status", "--json"])


def oc_health(bot_id: str, *, timeout: int | None = None) -> "dict | None":
    """openclaw health --json — returns gateway health.

    `timeout` (seconds) overrides oc_command's default 15s. Use a longer
    timeout for bots whose health check is slow because of plugin load —
    e.g. the primary bot with 7+ plugins routinely takes 7+ seconds and
    occasionally crosses 15s, causing heal.py to mark it down and kill
    the gateway in a self-cancelling restart cycle.
    """
    if timeout is not None:
        return oc_command(bot_id, ["health", "--json"], timeout=timeout)
    return oc_command(bot_id, ["health", "--json"])


def oc_models(bot_id: str) -> "list | None":
    """openclaw models list --json — returns model list"""
    return oc_command(bot_id, ["models", "list", "--json"])


def oc_channels(bot_id: str) -> "list | None":
    """openclaw channels list --json — returns channel list"""
    return oc_command(bot_id, ["channels", "list", "--json"])


def oc_cron_list(bot_id: str, *, _err_out: "list | None" = None) -> "list | None":
    """openclaw cron list --json — returns cron job list.

    ``_err_out`` (if a list) collects the underlying error string so callers
    can classify CLI-unavailable vs gateway-error vs format-error (the admin
    UI's fleet cron view needs this distinction)."""
    return oc_command(bot_id, ["cron", "list", "--json"], _err_out=_err_out)


def oc_cron_runs(
    bot_id: str, job_id: str, limit: int = 10
) -> "list | None":
    """openclaw cron runs --json --id job_id [--limit N]

    ``--id`` is a Commander ``requiredOption`` upstream — calling
    ``openclaw cron runs`` without it returns rc=1 with
    ``error: required option '--id <job_id>' not specified`` (the same
    CLI-misinvocation pattern that hid the spend_alert ``--to`` bug for
    23 days). Raise rather than make a doomed subprocess call.
    """
    if not job_id:
        raise ValueError("oc_cron_runs requires job_id — openclaw cron runs --id is requiredOption upstream")
    args = ["cron", "runs", "--json", "--limit", str(limit), "--id", job_id]
    return oc_command(bot_id, args)


def oc_security_audit(
    bot_id: str,
    *,
    deep: bool = False,
    timeout: int = 60,
    cache_ttl: int = 0,
    _err_out: "list | None" = None,
) -> "dict | list | None":
    """openclaw security audit [--deep] --json.

    The audit is heavyweight and must reflect live config, so it defaults to a
    generous ``timeout`` and ``cache_ttl=0`` (never serve a stale result) —
    matching how every caller invoked it through the old generic escape hatch.
    ``deep`` adds ``--deep`` (the full audit run by audit.py); ``_err_out``
    collects the error string for callers that branch on it (e.g. the
    "Config invalid" → ``doctor --fix`` → retry path)."""
    args = ["security", "audit"]
    if deep:
        args.append("--deep")
    args.append("--json")
    return oc_command(bot_id, args, timeout=timeout, cache_ttl=cache_ttl, _err_out=_err_out)


def oc_doctor_fix(bot_id: str, *, timeout: int = 20) -> "str | None":
    """openclaw doctor --fix — migrate legacy keys / drop unrecognized roots.

    Raw (non-JSON) command; returns stdout or None on failure. Used after an
    external openclaw.json write to repair config integrity before the next
    schema-validated command (openclaw rejects configs with unrecognized
    root-level keys)."""
    return oc_command_raw(bot_id, ["doctor", "--fix"], timeout=timeout)


def oc_set_identity(
    bot_id: str, *, agent_id: str, name: str, timeout: int = 20
) -> "str | None":
    """openclaw agents set-identity --agent <id> --name <name>.

    Sets the bot's display identity (propagates to OC's IDENTITY.md). Raw
    (non-JSON) command; returns stdout on success or None on failure."""
    return oc_command_raw(
        bot_id,
        ["agents", "set-identity", "--agent", agent_id, "--name", name],
        timeout=timeout,
    )


def oc_approvals(bot_id: str) -> "dict | None":
    """openclaw approvals get --json"""
    return oc_command(bot_id, ["approvals", "get", "--json"])


def oc_sessions(bot_id: str) -> "dict | None":
    """openclaw sessions --json"""
    return oc_command(bot_id, ["sessions", "--json"])


def oc_config_get(bot_id: str, key: str = "agents.defaults") -> "dict | None":
    """Get bot config at the given key path (defaults to agents.defaults).

    ``openclaw config get`` requires a path argument — omitting it causes
    "missing required argument 'path'" errors. The default ``agents.defaults``
    covers model-tier assignments, compaction settings, and other pod-wide
    diffable config without exposing gateway credentials or channel secrets.
    """
    args = ["config", "get", key, "--json"]
    return oc_command(bot_id, args, cache_ttl=0)


def oc_config_set(bot_id: str, key: str, value: str) -> bool:
    """Set a config value on a bot. Returns True on success."""
    result = oc_command(bot_id, ["config", "set", key, value], cache_ttl=0)
    return result is not None


def oc_keys_get(
    bot_id: str,
    network_path: str | None = None,
) -> "dict | None":
    """Return API key presence (True/False per provider) for a bot.

    Runs oc_keys.py as the bot user — reads auth-profiles.json directly.
    Key values are NEVER returned; only presence booleans.
    Returns None on failure.
    """
    user = _resolve_user(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_keys.py")
    if not Path(script).exists():
        print(f"[oc_cli] oc_keys.py not found at {script}", file=sys.stderr)
        return None

    cmd: list[str]
    if _should_sudo(user):
        cmd = ["sudo", "-H", "-u", user, _sudo_python(), script, "keys", bot_id]
    else:
        cmd = [sys.executable, script, "keys", bot_id]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(
                f"[oc_cli] oc_keys_get bot={bot_id} exit={proc.returncode} "
                f"stderr={proc.stderr[:300]!r}",
                file=sys.stderr,
            )
            return None
        return json.loads(proc.stdout)
    except Exception as e:
        print(f"[oc_cli] oc_keys_get bot={bot_id} error: {e}", file=sys.stderr)
        return None


def _bot_uid(user: str) -> "int | None":
    """The account's numeric uid, or None when it has no passwd entry.

    Only the launchd ``gui/<uid>`` domain needs this; a missing entry is an
    ordinary, expected state (bot not created yet, or a dev box), not an error
    worth swallowing at each call site.
    """
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def _discover_gateway_domain(svc: str, user: str, bot_uid: "int | None") -> "tuple[str, str] | None":
    """Discover the launchd domain for a gateway service by finding its plist file.

    The plist location is the authoritative source — it tells us exactly which
    domain launchd loaded the service into:

      /Library/LaunchDaemons/{svc}.plist  →  system
      /Library/LaunchAgents/{svc}.plist   →  gui/{uid}  (system-wide agent, runs as user)
      ~/Library/LaunchAgents/{svc}.plist  →  gui/{uid}  (user-specific agent)

    Returns (domain, plist_path) or None if plist not found anywhere.
    """
    checks: list[tuple[Path, str]] = [
        # LaunchDaemon (most common for always-on bots)
        (Path(f"/Library/LaunchDaemons/{svc}.plist"), "system"),
        # System-wide LaunchAgent (runs in each user's session on login)
        (Path(f"/Library/LaunchAgents/{svc}.plist"),
         f"gui/{bot_uid}" if bot_uid is not None else "system"),
    ]
    # User-specific LaunchAgent. Only checked when the account really exists:
    # _resolve_home_dir floors at /tmp for an unresolvable user, which would
    # fabricate a nonsense /tmp/Library/LaunchAgents candidate.
    if bot_uid is not None:
        checks.append((
            Path(_resolve_home_dir(user)) / "Library/LaunchAgents" / f"{svc}.plist",
            f"gui/{bot_uid}",
        ))
    for plist_path, domain in checks:
        if plist_path.exists():
            return (domain, str(plist_path))
    return None


def oc_gateway_restart(
    bot_id: str,
    network_path: str | None = None,
) -> "dict":
    """Restart the bot's openclaw gateway through the Scheduler seam.

    On a non-launchd adapter (systemd) this is a single ``restart()`` against
    ``ai.openclaw.{bot}-gateway`` — there is one unit in one place, so none of
    the domain resolution below applies.

    On launchd, domain resolution — in priority order:
      1. network.json bots[bot_id].service_domain  — explicit operator override
      2. Plist file location discovery              — authoritative, works for any install
      3. Brute-force: try gui/{uid} then system     — last resort if plist not found

    Service label: bots[bot_id].service  OR  ai.openclaw.{bot_id}-gateway
    Returns {ok: True, method, domain, service} or {ok: False, error, ...}.
    """
    bots = _load_bots(network_path)
    bot_cfg = bots.get(bot_id, {})
    user = bot_cfg.get("user", bot_id)
    svc = bot_cfg.get("service") or f"ai.openclaw.{bot_id}-gateway"
    port = bot_cfg.get("port")

    # Resolve bot user's UID (needed for gui/ domain)
    bot_uid = _bot_uid(user)

    # ── Priority 1: explicit domain from network.json ─────────────────────────
    explicit_domain = bot_cfg.get("service_domain")  # e.g. "system" or "gui/502"

    # ── Priority 2: discover from plist location ──────────────────────────────
    discovered = _discover_gateway_domain(svc, user, bot_uid)
    discovered_domain = discovered[0] if discovered else None
    discovered_plist = discovered[1] if discovered else None

    # Build ordered list of domains to try: explicit → discovered → brute-force
    domains_to_try: list[str] = []
    if explicit_domain:
        domains_to_try.append(explicit_domain)
    if discovered_domain and discovered_domain != explicit_domain:
        domains_to_try.append(discovered_domain)
    # Brute-force fallback: both gui/{uid} and system, not already in list
    for fallback in ([f"gui/{bot_uid}"] if bot_uid is not None else []) + ["system"]:
        if fallback not in domains_to_try:
            domains_to_try.append(fallback)

    errors: list[str] = []

    # ── Adapter resolution ────────────────────────────────────────────────────
    # Everything above this point is launchd vocabulary: domains (system vs
    # gui/<uid>) and plists on disk. A systemd pod has exactly ONE unit in ONE
    # place, so the discovery found nothing, the raw() probe loop was skipped
    # wholesale ("scheduler unavailable: ... requires the LaunchdScheduler
    # adapter"), and every Linux gateway restart collapsed into the openclaw-CLI
    # fallback. Drive the platform-neutral Scheduler verbs instead — the same
    # seam deploy._restart_gateway_linux already uses for this exact job.
    active = None
    sched = None  # the launchd-typed handle, set only under a real isinstance
    try:
        from runtime.scheduler import LaunchdScheduler, get_scheduler
        active = get_scheduler()
        if isinstance(active, LaunchdScheduler):
            sched = active
    except Exception as e:  # import failure — no scheduler at all
        errors.append(f"scheduler unavailable: {e}")

    is_launchd = sched is not None
    if active is not None and not is_launchd:
        # Protocol verbs only — no raw(), no domain, no plist.
        try:
            installed = bool(active.status(svc).get("installed"))
            if installed:
                ok, out = active.restart(svc)
                if ok:
                    result = {
                        "ok": True,
                        "bot": bot_id,
                        "service": svc,
                        "method": "scheduler-restart",
                    }
                    if port:
                        result["hint"] = f"Verify at http://localhost:{port}/health"
                    return result
                errors.append(f"{svc}: restart failed: {(out or '').strip()[:200]}")
            else:
                # No unit is a DEPLOY gap, not a restart failure. Return it
                # rather than falling through to the openclaw-CLI fallback:
                # that would start a gateway systemd does not supervise and
                # report success — the phantom "✓ Gateway restarted" that
                # deploy._restart_gateway_linux (W10-E) exists to prevent.
                return {
                    "ok": False,
                    "bot": bot_id,
                    "service": svc,
                    "error": (
                        f"Could not restart '{svc}': no unit installed at "
                        f"{active.artifact_path(svc)}."
                    ),
                    "plist_found": False,
                    "hint": "Run a deploy for this bot to install the gateway job.",
                }
        except Exception as e:
            errors.append(f"{svc}: scheduler restart: {type(e).__name__}: {e}")
        # Skip the launchd probe loop entirely — its domains mean nothing here.
        domains_to_try = []

    # Scheduler-seam escape hatch (counted S2 raw() debt): the domain varies
    # per attempt (explicit → discovered → gui/{uid} → system) while the
    # Scheduler verbs are pinned to the adapter's single configured domain,
    # so restart()/install() can't express this probe loop.
    for domain in domains_to_try:
        if sched is None:
            break
        try:
            rc, out, err = sched.raw("kickstart", "-k", f"{domain}/{svc}")
            if rc == 0:
                result: dict = {
                    "ok": True,
                    "bot": bot_id,
                    "service": svc,
                    "domain": domain,
                    "method": "discovered" if domain == discovered_domain else (
                        "explicit" if domain == explicit_domain else "brute-force"
                    ),
                }
                if discovered_plist:
                    result["plist"] = discovered_plist
                if port:
                    result["hint"] = f"Verify at http://localhost:{port}/health"
                return result

            kick_err = (err or out).strip()

            # "Could not find service" means the service exists on disk (plist
            # found) but was never bootstrapped into launchd — or was previously
            # booted out.  Bootstrap it from the plist, then kickstart.
            # raw() bootstrap (counted S2 debt): re-registers a plist the
            # caller discovered on disk; install() owns the plist write and
            # skips byte-identical files — wrong for re-registering an
            # unloaded job.
            if discovered_plist and "Could not find service" in kick_err:
                brc, bout, berr = sched.raw("bootstrap", domain, discovered_plist)
                if brc == 0:
                    krc, kout, kerr = sched.raw(
                        "kickstart", "-k", f"{domain}/{svc}"
                    )
                    if krc == 0:
                        return {
                            "ok": True,
                            "bot": bot_id,
                            "service": svc,
                            "domain": domain,
                            "method": "bootstrap+kickstart",
                            "plist": discovered_plist,
                        }
                    kick_err = f"bootstrap ok, kickstart failed: {(kerr or kout).strip()}"
                else:
                    kick_err = f"{kick_err}; bootstrap failed: {(berr or bout).strip()}"

            errors.append(f"{domain}: {kick_err}")
        except Exception as e:
            errors.append(f"{domain}: {e}")

    # ── Final fallback: openclaw gateway restart as the bot user ─────────────
    # Covers gateways not managed by launchd at all (dev installs, manual
    # starts) — mirrors the logic in deploy.py restart_gateway().
    #
    # No `env HOME=...` wrapper: `sudo -H` already sets HOME to the target
    # user's home dir, and PATH comes from `Defaults secure_path` in
    # /etc/sudoers.d/evolve. Wrapping with `env` would make sudo see the
    # command as `env` rather than `{oc_path}`, missing the
    # `evolve ALL=(ALL) NOPASSWD: SETENV: <oc_path>` grant and falling
    # through to a password prompt.
    oc = _find_openclaw()
    if oc:
        try:
            gr = subprocess.run(
                ["sudo", "-H", "-u", user, oc, "gateway", "restart"],
                capture_output=True, text=True, timeout=20,
                # openclaw is a Node binary and Node calls uv_cwd() at startup;
                # a nonexistent cwd raises FileNotFoundError here before the
                # command ever runs. _resolve_home_dir is pwd-first and
                # existence-checked, so this works on /home/<bot> too.
                cwd=_resolve_home_dir(user),
            )
            if gr.returncode == 0:
                return {
                    "ok": True,
                    "bot": bot_id,
                    "service": svc,
                    "method": "openclaw-gateway-restart",
                }
            errors.append(f"openclaw gateway restart: {(gr.stderr or gr.stdout).strip()[:200]}")
        except Exception as e:
            errors.append(f"openclaw gateway restart: {e}")

    # The plist/domain vocabulary below only means something on launchd — on a
    # systemd pod it reads as a phantom failure ("Plist not found in standard
    # locations") for a box that has no plists by design.
    if is_launchd:
        preamble = (
            f"Plist found at {discovered_plist}. " if discovered_plist
            else "Plist not found in standard locations. "
        )
        hint = None if discovered_plist else (
            "Add service_domain to this bot's entry in network.json to override "
            "domain detection. Valid values: 'system', 'gui/{uid}' (e.g. 'gui/502')."
        )
    else:
        preamble = ""
        hint = (
            f"Check the service manager directly: does a job for '{svc}' exist, "
            "and does the evolve user hold a restart grant for it?"
        )

    return {
        "ok": False,
        "error": f"Could not restart '{svc}'. {preamble}Tried: {'; '.join(errors)}",
        "bot": bot_id,
        "service": svc,
        "plist_found": bool(discovered_plist),
        "hint": hint,
    }


def oc_gateway_stop(
    bot_id: str,
    network_path: str | None = None,
) -> "dict":
    """Stop the bot's gateway WITHOUT deleting its on-disk job artifact.

    The counterpart to :func:`oc_gateway_restart` — for parking a crash-looping
    gateway without the service manager immediately respawning it. Deliberately
    NOT ``Scheduler.remove()``: remove deletes the plist/unit, and /restart
    needs it on disk to bring the gateway back.

    Platform semantics differ, and honestly so:
      * launchd — ``bootout``, tried in the gui/<uid> domain then system.
        TRANSIENT: a /Library/LaunchDaemons plist reloads at the next boot.
      * systemd — ``disable --now`` (the Protocol's ``disable`` verb).
        PERSISTENT: it survives reboot until something re-enables the unit;
        the next deploy's install() does exactly that.

    Returns {ok: True, service, domain?} or {ok: False, service, errors: [...]}.
    """
    bots = _load_bots(network_path)
    bot_cfg = bots.get(bot_id, {})
    svc = bot_cfg.get("service") or f"ai.openclaw.{bot_id}-gateway"
    user = bot_cfg.get("user", bot_id)

    try:
        from runtime.scheduler import LaunchdScheduler, get_scheduler
        active = get_scheduler()
    except Exception as e:
        return {"ok": False, "bot": bot_id, "service": svc,
                "errors": [f"scheduler unavailable: {e}"]}

    # Non-launchd (systemd): no domains, no bootout verb. Before this branch
    # existed the caller reached for the fail-fast launchd accessor, which
    # RAISES on a Linux pod — a hard 500 on every stop request there.
    if not isinstance(active, LaunchdScheduler):
        try:
            ok, out = active.disable(svc)
        except Exception as e:
            return {"ok": False, "bot": bot_id, "service": svc,
                    "errors": [f"{svc}: {type(e).__name__}: {e}"]}
        if not ok:
            return {"ok": False, "bot": bot_id, "service": svc, "errors": [out]}
        return {"ok": True, "bot": bot_id, "service": svc, "detail": out}

    # launchd: try the user GUI domain first, then system.
    domains: list[str] = ["system"]
    bot_uid = _bot_uid(user)
    if bot_uid is not None:
        domains.insert(0, f"gui/{bot_uid}")

    errors: list[str] = []
    for domain in domains:
        # raw(): a bare bootout that must NOT delete the plist (remove() would),
        # and the domain varies per iteration while an adapter is fixed to one.
        rc, out, err = active.raw("bootout", f"{domain}/{svc}")
        if rc == 0:
            return {"ok": True, "bot": bot_id, "service": svc, "domain": domain}
        errors.append(f"{domain}: {(err or out).strip()}")
    return {"ok": False, "bot": bot_id, "service": svc, "errors": errors}


def oc_model_get(
    bot_id: str,
    network_path: str | None = None,
) -> "dict | None":
    """Return model config dict with keys: bot, primary, fallback_order, catalog.

    Strategy (first success wins):
    1. ``sudo -u {user} python3 oc_model.py models {bot}`` — Evolve-native
       primary path that reads openclaw.json directly as the bot user.
       Returns full catalog + primary + fallback_order.
    2. ``openclaw config get agents.defaults.model --json`` — CLI fallback if
       the script is unavailable or fails.

    Returns None on total failure (both paths unavailable).
    """
    # ── Attempt 1: oc_model.py as bot user (reads full openclaw.json) ─────────
    user = _resolve_user(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_model.py")
    if Path(script).exists():
        cmd: list[str]
        if _should_sudo(user):
            cmd = ["sudo", "-H", "-u", user, _sudo_python(), script, "models", bot_id]
        else:
            cmd = [sys.executable, script, "models", bot_id]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                result = json.loads(proc.stdout)
                # Accept if we got a valid response dict (primary may be empty for bots
                # that haven't set a primary model yet — catalog/fallbacks still useful)
                if isinstance(result, dict) and "bot" in result and not result.get("error"):
                    return result
            else:
                print(
                    f"[oc_cli] oc_model_get (script) bot={bot_id} exit={proc.returncode} "
                    f"stderr={proc.stderr[:300]!r}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[oc_cli] oc_model_get (script) bot={bot_id} error: {e}", file=sys.stderr)
    else:
        print(f"[oc_cli] oc_model.py not found at {script}", file=sys.stderr)

    # ── Attempt 2: openclaw config get (CLI fallback) ─────────────────────────
    result = oc_command(
        bot_id,
        ["config", "get", "agents.defaults.model", "--json"],
        cache_ttl=0,
        network_path=network_path,
    )
    if isinstance(result, dict) and not result.get("error"):
        return {
            "bot": bot_id,
            "primary": result.get("primary", ""),
            "fallback_order": result.get("fallbacks", []),
            "catalog": result.get("catalog", []),
        }

    return None


def oc_full_config_get(
    bot_id: str,
    network_path: str | None = None,
) -> "dict | None":
    """Return full Evolve model config: tiers, routing, fallbackMode, tierCascade, catalog.

    Runs ``sudo -u {user} python3 oc_model.py config {bot}`` as the bot user.
    Returns None on failure.
    """
    user = _resolve_user(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_model.py")
    if not Path(script).exists():
        print(f"[oc_cli] oc_model.py not found at {script}", file=sys.stderr)
        return None

    cmd: list[str]
    if _should_sudo(user):
        cmd = ["sudo", "-H", "-u", user, _sudo_python(), script, "config", bot_id]
    else:
        cmd = [sys.executable, script, "config", bot_id]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            result = json.loads(proc.stdout)
            if isinstance(result, dict) and "bot" in result and not result.get("error"):
                return result
        print(
            f"[oc_cli] oc_full_config_get bot={bot_id} exit={proc.returncode} "
            f"stderr={proc.stderr[:300]!r}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[oc_cli] oc_full_config_get bot={bot_id} error: {e}", file=sys.stderr)
    return None


def oc_full_config_set_with_error(
    bot_id: str,
    updates: dict,
    network_path: str | None = None,
) -> "tuple[dict | None, str | None]":
    """Like ``oc_full_config_set`` but also returns an error message on failure.

    Returns ``(result, None)`` on success, ``(None, error_message)`` on failure.

    Used by callers (provisioning, UI handlers) that need to surface the
    real error to the operator instead of "check oc_model.py logs" —
    the admin daemon's stderr isn't exposed in the UI. The message
    extracts the structured error from oc_model.py's JSON stdout first
    (e.g. "unhashable type: 'dict'"), falling back to subprocess
    exit/stderr details when the subprocess itself failed.

    Role-awareness: the bot's role is read from network.json and passed
    to oc_model.py as a 5th positional arg, so the writer's recompute
    of openclaw.json's `primary` field uses a role-appropriate default
    tierCascade. See L2 of the 2026-05-29 tier audit + the
    default_tier_cascade_for_role docstring for the cost-bleed context.
    """
    # Refuse writes for bot_ids that aren't registered in network.json.
    # Without this, _resolve_user falls back to the bot_id itself as a
    # unix username, and we shell out to `sudo -u <bot_id> python3 …`
    # which fails with an opaque "unknown user" sudo error. Seen 2026-06-03
    # when a caller PUT /api/admin/config/forge/cascade — "forge" is a
    # feature name (Apps Forge), not a bot. Fail loud, fail early.
    #
    # FAIL CLOSED (#3566 audit C-2). The guard used to read ``if bots and
    # bot_id not in bots`` — so an EMPTY roster (network.json missing,
    # unreadable, not valid JSON, or valid JSON with no ``bots`` key) skipped
    # the check entirely and let an arbitrary bot_id through to
    # ``sudo -H -u <bot_id> <python3> …/oc_model.py config set <bot_id> …``,
    # because ``_resolve_user`` falls back to the bot_id itself as the unix
    # username. An unreadable roster is not permission to proceed: it is the
    # one state in which we know the LEAST about whether the bot_id is real.
    bots = _load_bots(network_path)
    if bot_id not in bots:
        if bots:
            msg = (
                f"unknown bot_id {bot_id!r} — not in network.json::bots "
                f"(known: {sorted(bots.keys())}). Refusing write."
            )
        else:
            msg = (
                f"cannot verify bot_id {bot_id!r} — network.json::bots is empty "
                f"or unreadable (tried: {_candidate_network_paths(network_path)}). "
                f"Refusing write."
            )
        print(f"[oc_cli] oc_full_config_set {msg}", file=sys.stderr)
        return None, msg

    user = _resolve_user(bot_id, network_path)
    role = _resolve_role(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_model.py")
    if not Path(script).exists():
        msg = f"oc_model.py not found at {script}"
        print(f"[oc_cli] {msg}", file=sys.stderr)
        return None, msg

    # Thread the POD's auto-upgrade block alongside any write that can leave
    # ``rungs`` on a file that had none. Carrying ``rungs`` is what makes a bot
    # Custom, and ``model_auto_upgrade.bot_policy`` does NOT give a Custom bot
    # the pod's ``enabled`` — a Custom bot that never set the key resolves to
    # the CODE default, ``false``. So the flip must bring the pod block with it
    # or the bot silently drops off auto-upgrade (lifecycle rule 1). oc_model.py
    # runs as the bot user and cannot read network.json, so the pod context is
    # supplied here — the same reason ``role`` is threaded through below.
    #
    # The trigger is a RUNGS write, not a ``tiers`` key (#3566 audit E-1). Two
    # payload shapes reach the flip THROUGH THIS SEAM (``migrate-model-roles``
    # writes evolve-tiers.json directly and is not covered — spec §"migration-
    # era hazard" treats that cohort separately):
    #   - a legacy-shaped ``tiers`` edit, which the writer's create/fold paths
    #     turn into rungs (#3566 follow-up: it no longer mints a deprecated
    #     file);
    #   - a wholesale NON-EMPTY ``rungs`` replacement — "Customize this bot" and
    #     the easy-setup wizard's bot scope. The wizard was the fourth carry
    #     site and had no ``tiers`` key at all, so it flipped bots to Custom
    #     with auto-upgrade resolving to false while the pod said true.
    # An EMPTY ``rungs`` must NOT trigger: that is the "Reset to pod defaults"
    # payload (``{"rungs": [], "roles": {}, "roleCaps": {}, "autoUpgrade": {}}``)
    # riding this same seam, and re-seeding a pod block there would undo
    # lifecycle rule 2 — a reset means the bot follows the pod IN FULL.
    #
    # Advisory only: never persisted verbatim, and an explicit ``autoUpgrade``
    # from the caller still wins (it merges on top of the pod block).
    _rungs_update = updates.get("rungs")
    _writes_rungs = isinstance(_rungs_update, list) and bool(_rungs_update)
    if ("tiers" in updates or _writes_rungs) and "podAutoUpgrade" not in updates:
        _pod_au = _pod_auto_upgrade_block(network_path)
        if _pod_au:
            updates = {**updates, "podAutoUpgrade": _pod_au}

    updates_json = json.dumps(updates)
    cmd: list[str]
    # Pass role as 5th positional when known. oc_model.py treats it as
    # optional — older callers / tests that don't pass it get the legacy
    # primary-cascade default, byte-for-byte the same as before.
    role_arg: list[str] = [role] if role else []
    if _should_sudo(user):
        cmd = [
            "sudo", "-H", "-u", user, _sudo_python(),
            script, "config", "set", bot_id, updates_json, *role_arg,
        ]
    else:
        cmd = [sys.executable, script, "config", "set", bot_id, updates_json, *role_arg]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        # Try to parse stdout as JSON regardless of exit code — oc_model.py
        # emits structured {"error": "...", "ok": false} on logical failures
        # even when the process exits cleanly.
        parsed_stdout: dict | None = None
        try:
            parsed_stdout = json.loads(proc.stdout) if proc.stdout.strip() else None
        except (ValueError, json.JSONDecodeError):
            parsed_stdout = None

        if proc.returncode == 0 and isinstance(parsed_stdout, dict) and not parsed_stdout.get("error"):
            return parsed_stdout, None

        # Failure path: build the most informative err_msg we can.
        if isinstance(parsed_stdout, dict) and parsed_stdout.get("error"):
            err_msg: str = str(parsed_stdout["error"])
        elif proc.stderr.strip():
            err_msg = f"exit={proc.returncode}: {proc.stderr.strip()[:300]}"
        else:
            err_msg = f"exit={proc.returncode} (no stderr); stdout={proc.stdout[:200]!r}"
        print(
            f"[oc_cli] oc_full_config_set bot={bot_id} exit={proc.returncode} "
            f"stderr={proc.stderr[:300]!r} stdout={proc.stdout[:300]!r}",
            file=sys.stderr,
        )
        return None, err_msg
    except Exception as e:
        print(f"[oc_cli] oc_full_config_set bot={bot_id} error: {e}", file=sys.stderr)
        return None, f"subprocess error: {e}"


def oc_full_config_set(
    bot_id: str,
    updates: dict,
    network_path: str | None = None,
) -> "dict | None":
    """Write one or more config sections to a bot's openclaw.json.

    ``updates`` may contain: catalog, tiers, routing, fallbackMode, tierCascade.
    Returns the full updated config on success, None on failure.

    For callers that need the error message (e.g. provisioning surfacing
    to the operator UI), see ``oc_full_config_set_with_error``.
    """
    result, _err = oc_full_config_set_with_error(bot_id, updates, network_path)
    return result


def oc_memory_get(
    bot_id: str,
    network_path: str | None = None,
) -> "dict | None":
    """Read agents.defaults.memorySearch.{provider, fallback} for a bot.

    Returns ``{"provider": str|None, "fallback": str|None}`` or None on error.
    Mirrors ``oc_model_get`` — runs ``sudo -u {user} python3 oc_model.py memory {bot}``.
    """
    user = _resolve_user(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_model.py")
    if not Path(script).exists():
        print(f"[oc_cli] oc_model.py not found at {script}", file=sys.stderr)
        return None

    cmd: list[str]
    if _should_sudo(user):
        cmd = ["sudo", "-H", "-u", user, _sudo_python(), script, "memory", bot_id]
    else:
        cmd = [sys.executable, script, "memory", bot_id]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(
                f"[oc_cli] oc_memory_get bot={bot_id} exit={proc.returncode} "
                f"stderr={proc.stderr[:300]!r}",
                file=sys.stderr,
            )
            return None
        result = json.loads(proc.stdout)
        return {
            "provider": result.get("provider"),
            "fallback": result.get("fallback"),
        }
    except Exception as e:
        print(f"[oc_cli] oc_memory_get bot={bot_id} error: {e}", file=sys.stderr)
        return None


def oc_memory_set(
    bot_id: str,
    provider: str,
    fallback: str | None = None,
    network_path: str | None = None,
) -> bool:
    """Set agents.defaults.memorySearch.{provider, fallback} for a bot.

    Empty ``provider`` clears the block (reverts to OpenClaw auto-detection).
    """
    user = _resolve_user(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_model.py")
    if not Path(script).exists():
        print(f"[oc_cli] oc_model.py not found at {script}", file=sys.stderr)
        return False

    cmd: list[str]
    if _should_sudo(user):
        cmd = ["sudo", "-H", "-u", user, _sudo_python(), script, "memory", "set", bot_id, provider or ""]
    else:
        cmd = [sys.executable, script, "memory", "set", bot_id, provider or ""]
    if fallback:
        cmd.append(fallback)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(
                f"[oc_cli] oc_memory_set bot={bot_id} exit={proc.returncode} "
                f"stderr={proc.stderr[:300]!r}",
                file=sys.stderr,
            )
            return False
        result = json.loads(proc.stdout)
        return bool(result.get("ok", False))
    except Exception as e:
        print(f"[oc_cli] oc_memory_set bot={bot_id} error: {e}", file=sys.stderr)
        return False


def oc_model_set(
    bot_id: str,
    primary: str,
    fallbacks: "list[str]",
    network_path: str | None = None,
) -> bool:
    """Set primary model and fallback list for a bot. Returns True on success.

    Calls ``sudo -u {user} python3 oc_model.py models set {bot} "{model_list}"``
    which runs as the bot user and writes openclaw.json directly — preserving
    file ownership and permissions.
    """
    model_list_str = " ".join([primary] + (fallbacks or []))
    user = _resolve_user(bot_id, network_path)
    script = str(Path(__file__).parent / "oc_model.py")
    if not Path(script).exists():
        print(f"[oc_cli] oc_model.py not found at {script}", file=sys.stderr)
        return False

    cmd: list[str]
    if _should_sudo(user):
        cmd = ["sudo", "-H", "-u", user, _sudo_python(), script, "models", "set", bot_id, model_list_str]
    else:
        cmd = [sys.executable, script, "models", "set", bot_id, model_list_str]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(
                f"[oc_cli] oc_model_set bot={bot_id} exit={proc.returncode} "
                f"stderr={proc.stderr[:300]!r}",
                file=sys.stderr,
            )
            return False
        result = json.loads(proc.stdout)
        return bool(result.get("ok", False))
    except Exception as e:
        print(f"[oc_cli] oc_model_set bot={bot_id} error: {e}", file=sys.stderr)
        return False


# ── Standalone CLI entrypoint ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run openclaw command as a bot user",
        epilog="Example: python3 oc_cli.py admin_bot status --json",
    )
    parser.add_argument("bot_id", help="Bot ID (e.g. admin_bot)")
    parser.add_argument("cmd", nargs="+", help="openclaw args (e.g. status --json)")
    parser.add_argument("--network", default=None, help="Path to network.json")
    parser.add_argument("--no-cache", action="store_true", help="Bypass cache")
    parser.add_argument("--raw", action="store_true", help="Return raw stdout (no JSON parse)")
    cli = parser.parse_args()

    if cli.raw:
        out = oc_command_raw(cli.bot_id, cli.cmd, network_path=cli.network)
        if out is None:
            print("null")
            sys.exit(1)
        print(out, end="")
    else:
        result = oc_command(
            cli.bot_id,
            cli.cmd,
            network_path=cli.network,
            cache_ttl=0 if cli.no_cache else _CACHE_TTL,
        )
        if result is None:
            print("null")
            sys.exit(1)
        print(json.dumps(result, indent=2))
