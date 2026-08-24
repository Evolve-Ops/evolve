"""
Centralized logging for Evolve Admin.

All modules should use:
    from .telemetry import get_logger
    _log = get_logger("module_name")

Logs are written to ~/.evolve/logs/evolve-admin.log (rotating, 10 MB × 5 files).
Call setup_logging() once at process startup (web/run.py and cli.py main entrypoint).

setup_access_logging() is a separate entrypoint for the Flask/Werkzeug access
logger. It must be called BEFORE app.run(), and routes HTTP request lines to
a dedicated rotating file (evolve-admin-ui.access.log) instead of stderr,
while dropping the highest-volume polling endpoints so the file stays small.
Without this redirect, every access line ends up in the LaunchDaemon's
StandardErrorPath, which is reserved for genuine errors.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

LOG_DIR = Path.home() / ".evolve" / "logs"
LOG_FILE = LOG_DIR / "evolve-admin.log"

# Production LaunchDaemon (admin-ui) writes to a shared, evolve-owned dir.
# Tests / dev runs fall back to ~/.evolve/logs alongside the main log.
_SHARED_LOG_DIR = Path("/Users/Shared/evolve/logs")
_ACCESS_LOG_NAME = "evolve-admin-ui.access.log"

# Werkzeug-emitted request lines containing any of these substrings are
# dropped before they reach the access log. The frontend polls
# /api/health every ~3s and /api/forge/jobs/<id> every ~10s while a forge
# job is open, so they dominate the file otherwise. Keep narrow — only
# endpoints that poll, not all 200s.
_ACCESS_LOG_SUPPRESS_SUBSTRINGS = (
    "GET /api/health ",
    "GET /api/forge/jobs/",
)

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-36s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_setup_done = False
_access_setup_done = False


def setup_logging(level: int = logging.INFO, console: bool = False) -> None:
    """Configure the evolve_admin logger hierarchy. Idempotent — safe to call multiple times."""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # Non-fatal: file logging will fail gracefully

    root = logging.getLogger("evolve_admin")
    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Rotating file handler — 10 MB per file, keep 5 backups (~50 MB total)
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except OSError:
        pass  # Non-fatal: log to console only if file is unavailable

    if console:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(formatter)
        root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the evolve_admin hierarchy."""
    return logging.getLogger(f"evolve_admin.{name}")


class _AccessLogSuppressFilter(logging.Filter):
    """Drop high-frequency polling endpoints from the Werkzeug access log."""

    def __init__(self, needles: tuple[str, ...] = _ACCESS_LOG_SUPPRESS_SUBSTRINGS):
        super().__init__()
        self._needles = needles

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(n in msg for n in self._needles)


def _pick_access_log_dir() -> Path:
    """Prefer /Users/Shared/evolve/logs (production); fall back to ~/.evolve/logs."""
    try:
        _SHARED_LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Confirm we can actually write — fall back otherwise (e.g. dev runs
        # without the evolve user / shared dir permissions).
        if os.access(_SHARED_LOG_DIR, os.W_OK):
            return _SHARED_LOG_DIR
    except OSError:
        pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def setup_access_logging(level: int = logging.INFO) -> Path | None:
    """Route Werkzeug request lines to a dedicated rotating file.

    Werkzeug attaches a stderr StreamHandler to ``logging.getLogger("werkzeug")``
    on first use, which means every access line lands in the LaunchDaemon's
    StandardErrorPath — drowning real exceptions under hundreds of MB of
    "GET /api/health 200" noise. This function installs a RotatingFileHandler
    on the werkzeug logger (10 MB × 5 files), sets propagate=False so lines
    never reach the root/stderr handler, and adds a filter that suppresses
    the highest-volume polling endpoints.

    Idempotent — safe to call multiple times. Returns the access log path,
    or None if the file handler could not be created.
    """
    global _access_setup_done
    if _access_setup_done:
        wlog = logging.getLogger("werkzeug")
        for h in wlog.handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler):
                return Path(h.baseFilename)
        return None
    _access_setup_done = True

    log_dir = _pick_access_log_dir()
    access_path = log_dir / _ACCESS_LOG_NAME

    wlog = logging.getLogger("werkzeug")
    wlog.setLevel(level)
    # Remove any pre-existing handlers (Werkzeug auto-adds a stderr one).
    for h in list(wlog.handlers):
        wlog.removeHandler(h)
    wlog.propagate = False  # don't bubble to root → stderr → .err.log
    wlog.addFilter(_AccessLogSuppressFilter())

    try:
        fh = logging.handlers.RotatingFileHandler(
            access_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(level)
        # Werkzeug formats each line itself; no asctime/levelname prefix.
        fh.setFormatter(logging.Formatter("%(message)s"))
        wlog.addHandler(fh)
    except OSError:
        return None

    return access_path


