"""
Diagnostic snapshot generation for Evolve Admin.

Collects structured data about the current installation (system info, config,
service status, log tail, recent errors) and writes it as a JSON snapshot in
``~/.evolve/reports/``. The in-app feedback flow (see ``feedback.py``) attaches
these snapshots to GitHub issues; users drag the file from disk onto the
issue comment box after the browser opens the new-issue page.

Email/SMTP delivery used to live here too. It was removed in favour of the
GitHub-issue handoff because the SMTP path was never reliably delivering and
required users to maintain a working app password.

Usage (CLI):
    evolve-admin report save --note "bots keep crashing after midnight"
    evolve-admin report show

Usage (web API):
    POST /api/report/save             { "note": "..." }
    POST /api/report/github-url       { kind, title, note, attach_snapshot }
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .telemetry import get_logger

_log = get_logger("reporter")

# ── Paths ──────────────────────────────────────────────────────────────────────

EVOLVE_DIR = Path.home() / ".evolve"
DISMISSED_STATE_PATH = EVOLVE_DIR / "report-dismissed.json"
# Separate from DISMISSED_STATE_PATH on purpose: that file backs the
# top-of-page "error detected" banner (fingerprint-based suppression with
# a short snooze). This file backs the Errors page's "Clear all" button —
# a coarser "hide anything whose last_seen is before this timestamp"
# filter. Different intent, different lifetime, kept separate so the
# banner snooze doesn't silently wipe the table view (and vice versa).
ERRORS_VIEW_STATE_PATH = EVOLVE_DIR / "errors-view-state.json"
REPORTS_DIR = EVOLVE_DIR / "reports"
LOG_DIR = EVOLVE_DIR / "logs"
EVOLVE_ADMIN_LOG = LOG_DIR / "evolve-admin.log"
ADMIN_SERVER_LOG = LOG_DIR / "admin-server.log"

# Log line timestamp prefix format (matches _DATE_FORMAT in telemetry.py)
_LOG_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


# ── Dismiss state ──────────────────────────────────────────────────────────────

def _load_dismissed_state() -> dict[str, Any]:
    """Load the dismiss tracking state."""
    if not DISMISSED_STATE_PATH.exists():
        return {"dismissed_at": None}
    try:
        return json.loads(DISMISSED_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"dismissed_at": None}


def _save_dismissed_state(state: dict[str, Any]) -> None:
    """Atomically save the dismiss tracking state."""
    EVOLVE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DISMISSED_STATE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(DISMISSED_STATE_PATH)
        DISMISSED_STATE_PATH.chmod(0o600)
    except OSError:
        pass


# ── Errors-page view state ─────────────────────────────────────────────────────
#
# Backs the "Clear all" button on the Errors page. Rows whose last_seen
# is on or before view_dismissed_at are hidden by /api/errors. Survives
# restarts and is shared across browsers (unlike the per-browser
# localStorage ack state).

def get_errors_view_dismissed_at() -> str | None:
    """Return the ISO timestamp before which Errors-page rows are hidden."""
    if not ERRORS_VIEW_STATE_PATH.exists():
        return None
    try:
        d = json.loads(ERRORS_VIEW_STATE_PATH.read_text())
        v = d.get("dismissed_at")
        return v if isinstance(v, str) else None
    except (json.JSONDecodeError, OSError):
        return None


def set_errors_view_dismissed_at(when_iso: str | None) -> None:
    """Set or clear the Errors-page view-dismissal cutoff."""
    EVOLVE_DIR.mkdir(parents=True, exist_ok=True)
    state = {"dismissed_at": when_iso} if when_iso else {}
    tmp = ERRORS_VIEW_STATE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(ERRORS_VIEW_STATE_PATH)
        ERRORS_VIEW_STATE_PATH.chmod(0o600)
    except OSError:
        pass


def _error_fingerprint(line: str) -> str:
    """
    Stable fingerprint for a log line, independent of timestamp and occurrence count.

    Strips the leading timestamp (chars 0-19) so that the same error logged
    at different times produces the same fingerprint.
    Examples:
      "2026-04-11T14:22:01 ERROR   evolve_admin.deploy  deploy_bot error [admin_bot]: ..."
      → "ERROR   evolve_admin.deploy  deploy_bot error [admin_bot]: ..."  (truncated to 200)
    """
    import re as _re
    core = line[20:].strip() if len(line) > 20 else line.strip()
    # Collapse whitespace so minor formatting differences don't split fingerprints
    core = _re.sub(r'\s+', ' ', core)
    return core[:200]


def mark_errors_dismissed(snooze_minutes: int = 5) -> str:
    """
    Record that the user has seen all errors up to now.

    Sets dismissed_at to now AND records fingerprints of all currently-visible
    errors so they will be suppressed even when they recur.

    Fingerprints expire after 24 hours so genuinely new occurrences of a
    previously-seen error type will surface again the next day.

    snooze_minutes: additional time-based quiet period on top of fingerprinting
    (default 5 min so a burst of identical errors doesn't fire multiple times).
    Use snooze_minutes=30 after the user files an issue / saves a snapshot.

    Returns the dismissed_at ISO timestamp.
    """
    from datetime import timedelta
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    state = _load_dismissed_state()

    # *** IMPORTANT: capture raw error lines using the OLD dismissed_at BEFORE
    # updating it to now.  If we set dismissed_at=now first and then call
    # _raw_error_lines_since(now), we get an empty list and no fingerprints
    # are ever recorded — which caused the banner to reappear every snooze cycle.
    old_dismissed_at = state.get("dismissed_at")
    raw_lines = _raw_error_lines_since(old_dismissed_at)
    new_fps = {_error_fingerprint(e) for e in raw_lines}
    _log.debug(
        "mark_errors_dismissed: %d raw error lines since %s → %d unique fingerprints",
        len(raw_lines), old_dismissed_at, len(new_fps),
    )

    state["dismissed_at"] = now

    if snooze_minutes > 0:
        state["snoozed_until"] = (now_dt + timedelta(minutes=snooze_minutes)).isoformat()
    else:
        state.pop("snoozed_until", None)

    existing_fps = set(state.get("seen_fingerprints", []))
    merged_fps = existing_fps | new_fps
    state["seen_fingerprints"] = list(merged_fps)
    state["fingerprints_valid_until"] = (now_dt + timedelta(hours=24)).isoformat()

    _log.info(
        "Error banner dismissed: %d new fingerprints recorded, %d total suppressed, snooze=%dmin",
        len(new_fps), len(merged_fps), snooze_minutes,
    )
    _save_dismissed_state(state)
    return now


# ── Pending error detection ────────────────────────────────────────────────────

def _raw_error_lines_since(dismissed_at_str: str | None) -> list[str]:
    """Return raw log lines that are ERROR/CRITICAL and newer than dismissed_at_str."""
    dismissed_dt: datetime | None = None
    if dismissed_at_str:
        try:
            dismissed_dt = datetime.fromisoformat(dismissed_at_str)
        except (ValueError, TypeError):
            pass
    lines = _read_tail(EVOLVE_ADMIN_LOG, 5000)
    result = []
    for line in lines:
        if " ERROR " not in line and " CRITICAL " not in line:
            continue
        ts_str = line[:19]
        try:
            line_dt = datetime.strptime(ts_str, _LOG_TS_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            if dismissed_dt is None:
                result.append(line)
            continue
        if dismissed_dt is None or line_dt > dismissed_dt:
            result.append(line)
    return result


def emit_error_signals(shared_dir: Path, *, lookback_hours: float = 1.0) -> int:
    """Mirror recent ERROR/CRITICAL log lines into the Signal store.

    Phase 5 of docs/spec-alerts-signal-store-2026-05-07.md. Ignores the
    error-banner dismiss state — the Signal store has its own snooze /
    dismiss surface, and shouldn't piggyback on the banner state.

    Each unique fingerprint becomes one rolling Signal (signature
    ``error_reporter:error_spike:<fingerprint16>``); repeat occurrences
    bump observation_count. Fingerprints not seen within the lookback
    window auto-resolve via sweep_resolve.

    Returns the number of Signal-emit attempts (mostly for diagnostics
    and tests). Best-effort — never raises into the caller.
    """
    try:
        # Lazy import: reporter.py is loaded by the admin daemon and
        # by the CLI; keep the analyzer import out of module load.
        import hashlib
        import importlib
        signals_store = importlib.import_module("signals.store")
        make_signature = importlib.import_module("schema.signal").make_signature
    except Exception:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_iso = cutoff.isoformat()

    raw_lines = _raw_error_lines_since(cutoff_iso)
    if not raw_lines:
        try:
            signals_store.sweep_resolve(
                shared_dir,
                producer="error_reporter",
                kept_signatures=set(),
                reason="auto-resolve: no errors in lookback window",
            )
        except Exception:
            pass
        return 0

    by_fp: dict[str, list[str]] = {}
    for line in raw_lines:
        fp = _error_fingerprint(line)
        by_fp.setdefault(fp, []).append(line)

    # v1.5-1: also resolve the observability client (best-effort) so we
    # can record an observability span alongside each error_spike Signal.
    obs_client = None
    try:
        import importlib as _importlib
        obs_mod = _importlib.import_module("observability")
        obs_client = obs_mod.get_client({}, shared_dir=shared_dir)
    except Exception:
        obs_client = None

    kept_signatures: set[str] = set()
    emitted = 0
    for fp, occurrences in by_fp.items():
        sample = occurrences[-1]  # most recent
        # Severity: CRITICAL log line → alert, ERROR → warn
        severity = "alert" if " CRITICAL " in sample else "warn"
        short = hashlib.sha256(fp.encode()).hexdigest()[:16]
        signature = make_signature("error_reporter", "error_spike", f"admin:{short}")
        kept_signatures.add(signature)
        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer="error_reporter",
                type="error_spike",
                flavor="maintenance",
                severity=severity,
                scope="host",
                title=f"Admin server: {fp[:80]}",
                body=sample[:500],
                details={
                    "fingerprint": fp,
                    "source_log": str(EVOLVE_ADMIN_LOG),
                    "occurrences_in_window": len(occurrences),
                    "sample": sample[:500],
                },
            )
            emitted += 1
        except Exception:
            continue

        # v1.5-1: triple-write to observability. Best-effort.
        if obs_client is not None:
            try:
                now = datetime.now(timezone.utc)
                obs_client.record_event(
                    name="error_reporter.error_spike",
                    producer="error_reporter",
                    bot_id=None,
                    start_time=now,
                    end_time=now,
                    tags=["maintenance", severity, "host"],
                    attributes={
                        "event_type": "error_spike",
                        "severity": severity,
                        "fingerprint": fp,
                        "fingerprint_short": short,
                        "occurrences_in_window": len(occurrences),
                        "sample": sample[:500],
                    },
                    error_info={"sample": sample[:500]},
                )
            except Exception:
                pass

    try:
        signals_store.sweep_resolve(
            shared_dir,
            producer="error_reporter",
            kept_signatures=kept_signatures,
            reason="auto-resolve: fingerprint absent from lookback window",
        )
    except Exception:
        pass

    return emitted


def get_pending_errors(_skip_fingerprint_filter: bool = False) -> dict[str, Any]:
    """
    Return the count of ERROR/CRITICAL log lines that have appeared since the
    last time the user dismissed the error banner.

    Returns:
        {
            "pending": bool,
            "count": int,
            "since": ISO str | None,       # the dismissed_at cutoff used
            "last_error": str | None,      # most recent error line (truncated)
            "last_error_at": ISO str | None,
        }
    """
    state = _load_dismissed_state()
    dismissed_at_str = state.get("dismissed_at")
    now_dt = datetime.now(timezone.utc)

    # Respect time-based snooze
    if not _skip_fingerprint_filter:
        snoozed_until_str = state.get("snoozed_until")
        if snoozed_until_str:
            try:
                if now_dt < datetime.fromisoformat(snoozed_until_str):
                    return {"pending": False, "count": 0, "since": dismissed_at_str,
                            "snoozed_until": snoozed_until_str, "last_error": None, "last_error_at": None}
            except (ValueError, TypeError):
                pass

    # Load seen fingerprints (valid for 24 h after last dismiss)
    seen_fps: set[str] = set()
    if not _skip_fingerprint_filter:
        valid_until_str = state.get("fingerprints_valid_until")
        if valid_until_str:
            try:
                if now_dt < datetime.fromisoformat(valid_until_str):
                    seen_fps = set(state.get("seen_fingerprints", []))
            except (ValueError, TypeError):
                pass

    raw_lines = _raw_error_lines_since(dismissed_at_str)

    # Keep only error types not previously seen (novel fingerprints)
    novel_lines: list[str] = []
    for line in raw_lines:
        fp = _error_fingerprint(line)
        if fp not in seen_fps:
            novel_lines.append(line)

    if not novel_lines:
        return {"pending": False, "count": 0, "since": dismissed_at_str,
                "last_error": None, "last_error_at": None}

    # Timestamp of most recent novel error
    latest_line = novel_lines[-1]
    latest_at: str | None = None
    try:
        latest_at = datetime.strptime(latest_line[:19], _LOG_TS_FORMAT).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        pass

    return {
        "pending": True,
        "count": len(novel_lines),
        "since": dismissed_at_str,
        "last_error": latest_line[:200],
        "last_error_at": latest_at,
    }


# ── Data collection ────────────────────────────────────────────────────────────

def _read_tail(path: Path, n: int) -> list[str]:
    """Return last n lines of a text file. Returns [] if file missing."""
    if not path.exists():
        return []
    try:
        text = path.read_text(errors="replace")
        lines = text.splitlines()
        return lines[-n:]
    except OSError:
        return []


def collect_system_info() -> dict[str, Any]:
    """Collect host OS, Python, and Evolve version info."""
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER", os.environ.get("LOGNAME", "unknown")),
        "platform": platform.platform(),
        "macos_version": platform.mac_ver()[0] or "unknown",
        "python_version": sys.version,
        "python_executable": sys.executable,
    }
    # Evolve version
    try:
        from .deploy import EVOLVE_VERSION
        info["evolve_version"] = EVOLVE_VERSION
    except Exception:
        info["evolve_version"] = "unknown"
    # Git HEAD
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--tags", "--dirty"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            info["git_ref"] = result.stdout.strip()
    except Exception:
        pass
    # Key installed packages
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show",
             "flask", "click", "rich", "evolve-admin"],
            capture_output=True, text=True, timeout=10,
        )
        info["pip_show"] = result.stdout.strip()
    except Exception:
        pass
    return info


def collect_log_tail(n: int = 300) -> list[str]:
    """Return last n lines of the evolve-admin structured log."""
    return _read_tail(EVOLVE_ADMIN_LOG, n)


def collect_server_log_tail(n: int = 100) -> list[str]:
    """Return last n lines of the launchd admin-server.log (stdout/stderr)."""
    return _read_tail(ADMIN_SERVER_LOG, n)


def collect_recent_errors(n: int = 60) -> list[str]:
    """Extract the most recent ERROR and CRITICAL lines from the evolve-admin log."""
    lines = _read_tail(EVOLVE_ADMIN_LOG, 2000)
    errors = [l for l in lines if " ERROR " in l or " CRITICAL " in l]
    return errors[-n:]


def collect_network_config_sanitized(network_path: Path) -> dict[str, Any]:
    """Load network.json and redact any sensitive fields."""
    try:
        from .config import load_network
        cfg = load_network(network_path)
    except Exception as e:
        return {"_error": f"Could not load network config: {e}"}

    import copy
    cfg = copy.deepcopy(cfg)

    # Redact alerts channel credentials
    alerts = cfg.get("alerts", {})
    if alerts.get("chatId"):
        alerts["chatId"] = "[REDACTED]"
    if alerts.get("token"):
        alerts["token"] = "[REDACTED]"

    # Redact any keys that look like API keys or secrets
    _SENSITIVE_KEYS = {"token", "apiKey", "api_key", "secret", "password",
                       "webhook", "webhookUrl"}
    def _redact(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: "[REDACTED]" if k in _SENSITIVE_KEYS else _redact(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_redact(i) for i in obj]
        return obj

    return _redact(cfg)


def collect_install_info(network_path: Path) -> dict[str, Any]:
    """Read install.json for version and deployment history."""
    try:
        from .config import load_network
        from .deploy import read_install_json
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        data = read_install_json(shared_dir)
        return data or {"_info": "install.json not found"}
    except Exception as e:
        return {"_error": str(e)}


def collect_service_status() -> dict[str, Any]:
    """Return launchd service status for the admin server."""
    try:
        from . import service as _svc
        return _svc.status()
    except Exception as e:
        return {"_error": str(e)}


def collect_environment() -> dict[str, str]:
    """Return a filtered set of environment variables useful for debugging."""
    _KEEP = {"PATH", "HOME", "USER", "SHELL", "LOGNAME", "LANG", "LC_ALL",
             "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    return {k: v for k, v in os.environ.items() if k in _KEEP}


# ── Saved report access ────────────────────────────────────────────────────────

def list_saved_reports(limit: int = 20) -> list[dict[str, Any]]:
    """
    Return metadata for the most recent saved reports, newest first.

    Each entry:
      { name, path, generated_at, note, error_count, hostname, evolve_version }
    """
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("report-*.json"), key=lambda p: p.name, reverse=True)[:limit]
    result = []
    for f in files:
        entry: dict[str, Any] = {"name": f.name, "path": str(f)}
        try:
            data = json.loads(f.read_text())
            entry["generated_at"] = data.get("generated_at")
            entry["note"] = data.get("note", "")
            entry["error_count"] = len(data.get("recent_errors", []))
            sys_info = data.get("system", {})
            entry["hostname"] = sys_info.get("hostname", "")
            entry["evolve_version"] = sys_info.get("evolve_version", "")
        except (json.JSONDecodeError, OSError):
            pass
        result.append(entry)
    return result


def read_report_as_text(name: str) -> str | None:
    """
    Return the formatted plain-text version of a saved report by filename.
    Returns None if the file does not exist or cannot be parsed.
    Rejects path traversal attempts.
    """
    # Safety: only allow bare filenames, no path components
    if "/" in name or "\\" in name or not name.startswith("report-") or not name.endswith(".json"):
        return None
    path = REPORTS_DIR / name
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return format_report_text(data)
    except (json.JSONDecodeError, OSError):
        return None


# ── Report assembly ────────────────────────────────────────────────────────────

def build_report(
    network_path: Path,
    note: str = "",
    include_config: bool = True,
) -> dict[str, Any]:
    """Assemble a full diagnostic report dict."""
    _log.info("Building diagnostic report (note=%r)", note[:80] if note else "")
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "system": collect_system_info(),
        "environment": collect_environment(),
        "service_status": collect_service_status(),
        "recent_errors": collect_recent_errors(60),
        "log_tail": collect_log_tail(300),
        "server_log_tail": collect_server_log_tail(100),
    }
    if include_config:
        report["network_config"] = collect_network_config_sanitized(network_path)
        report["install_info"] = collect_install_info(network_path)
    return report


# ── Formatting ─────────────────────────────────────────────────────────────────

def format_report_text(report: dict[str, Any]) -> str:
    """Render the report as a readable plain-text string."""
    lines: list[str] = []

    def _section(title: str) -> None:
        lines.append("")
        lines.append("=" * 72)
        lines.append(f"  {title}")
        lines.append("=" * 72)

    def _kv(key: str, val: Any) -> None:
        lines.append(f"  {key:<28} {val}")

    lines.append("EVOLVE ADMIN — DIAGNOSTIC REPORT")
    lines.append(f"Generated: {report.get('generated_at', 'unknown')}")
    if report.get("note"):
        lines.append(f"Note: {report['note']}")

    _section("SYSTEM")
    sys_info = report.get("system", {})
    for k in ["hostname", "user", "macos_version", "python_version",
               "evolve_version", "git_ref"]:
        if k in sys_info:
            _kv(k, sys_info[k])

    _section("SERVICE STATUS")
    svc = report.get("service_status", {})
    for k, v in svc.items():
        _kv(k, v)

    _section("ENVIRONMENT")
    for k, v in report.get("environment", {}).items():
        _kv(k, v)

    if report.get("network_config"):
        _section("NETWORK CONFIG (sanitized)")
        lines.append(json.dumps(report["network_config"], indent=2))

    if report.get("install_info"):
        _section("INSTALL INFO")
        lines.append(json.dumps(report["install_info"], indent=2))

    if report.get("recent_errors"):
        _section(f"RECENT ERRORS ({len(report['recent_errors'])} lines)")
        lines.extend(report["recent_errors"])
    else:
        _section("RECENT ERRORS")
        lines.append("  (none found)")

    _section(f"EVOLVE-ADMIN LOG TAIL ({len(report.get('log_tail', []))} lines)")
    lines.extend(report.get("log_tail", []))

    if report.get("server_log_tail"):
        _section(f"SERVER LOG TAIL ({len(report['server_log_tail'])} lines)")
        lines.extend(report["server_log_tail"])

    if sys_info.get("pip_show"):
        _section("INSTALLED PACKAGES")
        lines.append(sys_info["pip_show"])

    return "\n".join(lines)


# ── Save to disk ───────────────────────────────────────────────────────────────

def _ensure_dir(d: Path) -> bool:
    """Try to create directory d (with parents). Return True on success."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        return True
    except (PermissionError, OSError) as e:
        _log.warning("Cannot create directory %s: %s", d, e)
        return False


def _report_dirs() -> list[Path]:
    """
    Return candidate directories for saving reports, in preference order.

    Falls back from ~/.evolve/reports → /Users/Shared/evolve/reports → /tmp/evolve-reports
    so that a permission problem in one location doesn't prevent the report from
    being saved at all.
    """
    return [
        REPORTS_DIR,
        Path("/Users/Shared/evolve/reports"),
        Path(tempfile.gettempdir()) / "evolve-reports",
    ]


def save_report_to_file(report: dict[str, Any]) -> Path:
    """
    Save report as JSON to the first writable reports directory.

    Tries ~/.evolve/reports first, then /Users/Shared/evolve/reports, then
    /tmp/evolve-reports — so a permission problem on the home directory
    doesn't cause a 500 error.
    """
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    filename = f"report-{ts}.json"
    content = json.dumps(report, indent=2)

    last_err: Exception | None = None
    for candidate in _report_dirs():
        if not _ensure_dir(candidate):
            continue
        path = candidate / filename
        try:
            path.write_text(content)
            try:
                path.chmod(0o600)
            except OSError:
                pass  # non-fatal
            _log.info("Report saved to %s", path)
            return path
        except (PermissionError, OSError) as e:
            _log.warning("Cannot write report to %s: %s", path, e)
            last_err = e
            continue

    raise PermissionError(
        f"Cannot save report: no writable directory found. "
        f"Tried: {[str(d) for d in _report_dirs()]}. "
        f"Last error: {last_err}. "
        f"Fix: run 'sudo chown -R $USER ~/.evolve' in Terminal."
    )
