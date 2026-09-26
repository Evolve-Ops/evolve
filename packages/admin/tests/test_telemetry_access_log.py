"""Tests for telemetry.setup_access_logging — the Werkzeug access-log redirect.

Context: pre-2026-06-01 the admin-ui LaunchDaemon's StandardErrorPath
(``/Users/Shared/evolve/logs/evolve-admin-ui.err.log``) was growing to
100MB+ in a few days because Werkzeug's default access logger writes
to stderr, and the frontend polls ``/api/health`` every ~3s and
``/api/forge/jobs/<id>`` every ~10s. setup_access_logging() redirects
that stream to a dedicated rotating file and drops the highest-volume
polling endpoints. These tests guard the redirect.
"""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from evolve_admin import telemetry


@pytest.fixture(autouse=True)
def _reset_access_setup(monkeypatch, tmp_path):
    """Reset the idempotency flag and point the access log at tmp_path."""
    monkeypatch.setattr(telemetry, "_access_setup_done", False)
    monkeypatch.setattr(telemetry, "_SHARED_LOG_DIR", tmp_path)
    # Detach any pre-existing handlers on the werkzeug logger so we get
    # a clean baseline (tests may run in any order; module-level setup
    # in other tests can have already configured it).
    wlog = logging.getLogger("werkzeug")
    for h in list(wlog.handlers):
        wlog.removeHandler(h)
    for f in list(wlog.filters):
        wlog.removeFilter(f)
    wlog.propagate = True
    wlog.setLevel(logging.NOTSET)
    yield
    for h in list(wlog.handlers):
        wlog.removeHandler(h)
    for f in list(wlog.filters):
        wlog.removeFilter(f)
    wlog.propagate = True
    wlog.setLevel(logging.NOTSET)


def test_access_log_path_returned(tmp_path):
    path = telemetry.setup_access_logging()
    assert path == tmp_path / "evolve-admin-ui.access.log"


def test_werkzeug_logger_has_rotating_handler():
    telemetry.setup_access_logging()
    wlog = logging.getLogger("werkzeug")
    rotating = [h for h in wlog.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    h = rotating[0]
    assert h.maxBytes == 10 * 1024 * 1024
    assert h.backupCount == 5


def test_werkzeug_logger_does_not_propagate():
    """propagate=False keeps access lines off the root logger → stderr → .err.log."""
    telemetry.setup_access_logging()
    assert logging.getLogger("werkzeug").propagate is False


def test_health_poll_line_is_suppressed(tmp_path):
    """/api/health is the worst offender — must not reach the access log."""
    path = telemetry.setup_access_logging()
    wlog = logging.getLogger("werkzeug")
    wlog.info('127.0.0.1 - - [01/Jun/2026 03:09:48] "GET /api/health HTTP/1.1" 200 -')
    for h in wlog.handlers:
        h.flush()
    contents = path.read_text() if path.exists() else ""
    assert "/api/health" not in contents


def test_forge_job_poll_line_is_suppressed(tmp_path):
    """/api/forge/jobs/<id> polls every 10s while a forge job is open."""
    path = telemetry.setup_access_logging()
    wlog = logging.getLogger("werkzeug")
    wlog.info('127.0.0.1 - - [01/Jun/2026 03:09:48] "GET /api/forge/jobs/abc123 HTTP/1.1" 200 -')
    for h in wlog.handlers:
        h.flush()
    contents = path.read_text() if path.exists() else ""
    assert "/api/forge/jobs" not in contents


def test_non_polling_endpoint_is_kept(tmp_path):
    """Real requests (POSTs, page loads, etc.) must still appear so the log is useful."""
    path = telemetry.setup_access_logging()
    wlog = logging.getLogger("werkzeug")
    wlog.info('127.0.0.1 - - [01/Jun/2026 03:09:48] "POST /api/deploy HTTP/1.1" 200 -')
    wlog.info('127.0.0.1 - - [01/Jun/2026 03:09:48] "GET /api/bot/team-bot-a/audit HTTP/1.1" 200 -')
    for h in wlog.handlers:
        h.flush()
    contents = path.read_text()
    assert "/api/deploy" in contents
    assert "/api/bot/team-bot-a/audit" in contents


def test_idempotent(tmp_path):
    """Calling twice must not stack duplicate handlers."""
    path1 = telemetry.setup_access_logging()
    path2 = telemetry.setup_access_logging()
    assert path1 == path2
    wlog = logging.getLogger("werkzeug")
    rotating = [h for h in wlog.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1


def test_falls_back_to_home_log_dir_when_shared_unavailable(monkeypatch, tmp_path):
    """Dev runs without /Users/Shared/evolve/logs should not crash."""
    # Point _SHARED_LOG_DIR at an unwritable path.
    unwritable = tmp_path / "definitely-does-not-exist" / "and-cant-be-created"
    monkeypatch.setattr(telemetry, "_SHARED_LOG_DIR", unwritable)
    home_dir = tmp_path / "home_logs"
    monkeypatch.setattr(telemetry, "LOG_DIR", home_dir)

    # Force _pick_access_log_dir to fail on shared and pick LOG_DIR.
    # OSError on mkdir of unwritable parent triggers fallback.
    import os
    real_access = os.access

    def fake_access(p, mode):
        if str(p).startswith(str(unwritable)):
            return False
        return real_access(p, mode)
    monkeypatch.setattr(os, "access", fake_access)

    path = telemetry.setup_access_logging()
    assert path is not None
    assert str(path).startswith(str(home_dir))


def test_existing_stderr_handler_removed(tmp_path):
    """Werkzeug auto-attaches a StreamHandler on first use; redirect must clear it."""
    wlog = logging.getLogger("werkzeug")
    bogus = logging.StreamHandler()
    wlog.addHandler(bogus)
    telemetry.setup_access_logging()
    assert bogus not in wlog.handlers
