"""Logging-setup tests for ``evolve_admin.mcp_bridge.__main__``.

Regression cover for unbounded ``mcp-bridge.log`` growth (2026-06-21): the
bridge's stdout/stderr is captured by launchd into
``~/.evolve/logs/mcp-bridge.log``, a path launchd never rotates, and the
entrypoint routed every log line there via ``basicConfig`` — so the file grew
without bound (observed 214 MB on the mini). ``_configure_logging`` now owns
that file with a ``RotatingFileHandler`` so it self-caps.

These tests assert the handler class and limits, and that the daemon (no tty)
does not also add a stderr handler that would re-bloat the launchd-captured
file.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.mcp_bridge import __main__ as bridge_main  # noqa: E402


class _FakeStderr:
    """Minimal stderr stand-in so the tty branch is exercised deterministically
    (pytest captures real stderr, whose ``isatty()`` is environment-dependent)."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, *_a) -> None:  # pragma: no cover - never emitted in tests
        pass

    def flush(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def clean_root_logger():
    """Give each test a handler-free root logger and restore the original set
    afterward — root logging is process-global, so leaks would pollute the
    rest of the suite."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for h in saved_handlers:
        root.removeHandler(h)
    yield root
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _rotating_handlers(root: logging.Logger) -> list[logging.handlers.RotatingFileHandler]:
    return [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]


def _bare_stream_handlers(root: logging.Logger) -> list[logging.StreamHandler]:
    return [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]


def test_configure_logging_installs_capped_rotating_handler(
    tmp_path, monkeypatch, clean_root_logger
):
    """The root logger gets exactly one RotatingFileHandler, pointed at the
    bridge log path, capped at 10 MiB × 3 backups (~40 MiB ceiling)."""
    log_path = tmp_path / ".evolve" / "logs" / "mcp-bridge.log"
    monkeypatch.setattr(bridge_main, "_bridge_log_path", lambda: log_path)

    bridge_main._configure_logging(logging.INFO)

    rotating = _rotating_handlers(clean_root_logger)
    assert len(rotating) == 1, "expected exactly one RotatingFileHandler on root"
    handler = rotating[0]
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 3
    assert Path(handler.baseFilename) == log_path
    assert clean_root_logger.level == logging.INFO
    # The log dir is created on demand (first daemon run after a fresh setup).
    assert log_path.parent.is_dir()


def test_configure_logging_no_stderr_handler_when_not_a_tty(
    tmp_path, monkeypatch, clean_root_logger
):
    """Under the LaunchDaemon (stderr is the launchd-captured file, not a tty)
    the bridge must NOT add a console handler — re-emitting to that stderr would
    re-bloat the very file the RotatingFileHandler just capped."""
    log_path = tmp_path / "mcp-bridge.log"
    monkeypatch.setattr(bridge_main, "_bridge_log_path", lambda: log_path)
    monkeypatch.setattr(sys, "stderr", _FakeStderr(tty=False))

    bridge_main._configure_logging(logging.INFO)

    assert len(_rotating_handlers(clean_root_logger)) == 1
    assert _bare_stream_handlers(clean_root_logger) == [], \
        "daemon (no tty) should not add a stderr handler"


def test_configure_logging_adds_console_handler_for_interactive_tty(
    tmp_path, monkeypatch, clean_root_logger
):
    """An interactive ``python -m evolve_admin.mcp_bridge`` run (tty) keeps a
    console handler so logs are still visible while debugging by hand."""
    log_path = tmp_path / "mcp-bridge.log"
    monkeypatch.setattr(bridge_main, "_bridge_log_path", lambda: log_path)
    monkeypatch.setattr(sys, "stderr", _FakeStderr(tty=True))

    bridge_main._configure_logging(logging.INFO)

    assert len(_rotating_handlers(clean_root_logger)) == 1
    assert len(_bare_stream_handlers(clean_root_logger)) == 1, \
        "interactive tty run should echo to the console"


def test_configure_logging_falls_back_to_stderr_when_file_unavailable(
    tmp_path, monkeypatch, clean_root_logger
):
    """If the rotating log can't be opened (e.g. no writable ``~/.evolve``),
    logging falls back to stderr instead of silently dropping — and installs no
    RotatingFileHandler. Simulated by pointing the path under a regular file so
    ``mkdir(parents=True)`` raises ``NotADirectoryError`` (an ``OSError``)."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    bad_path = blocker / "logs" / "mcp-bridge.log"
    monkeypatch.setattr(bridge_main, "_bridge_log_path", lambda: bad_path)

    # Must not raise.
    bridge_main._configure_logging(logging.INFO)

    assert _rotating_handlers(clean_root_logger) == []
