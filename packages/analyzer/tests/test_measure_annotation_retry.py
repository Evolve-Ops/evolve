"""Tests for _open_annotations_with_retry in measure.py.

Covers the transient-PermissionError race between annotation creation
(mode 600) and the set_evolve_read_acl step on macOS.  Uses monkeypatch
to avoid real sleeps and a counter-based mock for Path.open.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest

# ── Path setup (mirrors other tests in this package) ─────────────────────────
_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import measure  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_open_mock(fail_times: int, content: str = ""):
    """Return a callable that raises PermissionError the first *fail_times*
    calls, then returns an open-able StringIO on subsequent calls.

    Use as a replacement for Path.open().
    """
    call_count = 0

    def _open_mock(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= fail_times:
            raise PermissionError(f"[Errno 13] Permission denied (mock call {call_count})")
        return io.StringIO(content)

    _open_mock.call_count_ref = lambda: call_count
    return _open_mock


# ── Unit tests for _open_annotations_with_retry ───────────────────────────────

class TestOpenAnnotationsWithRetry:
    """Direct unit tests for the retry helper."""

    def test_happy_path_no_retry(self, monkeypatch, tmp_path):
        """File readable on first attempt — returns handle, no sleep, no log."""
        target = tmp_path / "ann.jsonl"
        target.write_text("")

        sleep_calls: list[float] = []
        monkeypatch.setattr(measure.time, "sleep", lambda s: sleep_calls.append(s))

        fh = measure._open_annotations_with_retry(target)
        fh.close()

        assert sleep_calls == [], "Should not sleep on success"

    def test_transient_permerror_retried(self, monkeypatch, tmp_path, capsys):
        """PermissionError on attempt 1 → retry → success on attempt 2."""
        target = tmp_path / "ann.jsonl"
        target.write_text("")

        mock_open = _make_open_mock(fail_times=1, content="")
        monkeypatch.setattr(Path, "open", lambda self, *a, **kw: mock_open(*a, **kw))

        sleep_calls: list[float] = []
        monkeypatch.setattr(measure.time, "sleep", lambda s: sleep_calls.append(s))

        fh = measure._open_annotations_with_retry(target, max_attempts=3, base_delay_seconds=60.0)
        fh.close()

        assert sleep_calls == [60.0], "Should sleep 60s before the retry"
        captured = capsys.readouterr()
        assert "PermissionError" in captured.err
        assert "attempt 1/3" in captured.err

    def test_persistent_permerror_raises(self, monkeypatch, tmp_path):
        """All 3 attempts fail → PermissionError propagated to caller."""
        target = tmp_path / "ann.jsonl"
        target.write_text("")

        mock_open = _make_open_mock(fail_times=99)
        monkeypatch.setattr(Path, "open", lambda self, *a, **kw: mock_open(*a, **kw))

        sleep_calls: list[float] = []
        monkeypatch.setattr(measure.time, "sleep", lambda s: sleep_calls.append(s))

        with pytest.raises(PermissionError):
            measure._open_annotations_with_retry(target, max_attempts=3, base_delay_seconds=60.0)

        # Two sleeps: after attempt 1 (60s) and after attempt 2 (120s); no sleep after last
        assert sleep_calls == [60.0, 120.0]

    def test_other_oserror_not_retried(self, monkeypatch, tmp_path):
        """Non-PermissionError OSError is raised immediately without retry."""
        target = tmp_path / "ann.jsonl"
        target.write_text("")

        call_count = 0

        def _raise_ioerror(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise OSError(f"[Errno 5] Input/output error (mock)")

        monkeypatch.setattr(Path, "open", lambda self, *a, **kw: _raise_ioerror(*a, **kw))

        sleep_calls: list[float] = []
        monkeypatch.setattr(measure.time, "sleep", lambda s: sleep_calls.append(s))

        with pytest.raises(OSError):
            measure._open_annotations_with_retry(target, max_attempts=3, base_delay_seconds=60.0)

        assert call_count == 1, "OSError should not be retried"
        assert sleep_calls == [], "Should not sleep for non-PermissionError"


# ── Integration tests via load_annotations ────────────────────────────────────

class TestLoadAnnotationsRetryIntegration:
    """Verify that load_annotations picks up the retry behaviour end-to-end."""

    def test_load_succeeds_after_transient_permerror(self, monkeypatch, tmp_path):
        """load_annotations returns data even when the first open raises PermissionError."""
        ann_dir = tmp_path / "annotations" / "bot_a"
        ann_dir.mkdir(parents=True)
        today = date(2026, 5, 12)
        ann_file = ann_dir / f"{today}.jsonl"
        record = {"type": "turn_annotation", "session_id": "s1", "session_class": "productive"}
        ann_file.write_text(json.dumps(record) + "\n")

        mock_open = _make_open_mock(fail_times=1, content=json.dumps(record) + "\n")
        monkeypatch.setattr(Path, "open", lambda self, *a, **kw: mock_open(*a, **kw))
        monkeypatch.setattr(measure.time, "sleep", lambda s: None)

        annotations = measure.load_annotations(str(tmp_path), "bot_a", today)
        assert len(annotations) == 1
        assert annotations[0]["session_class"] == "productive"

    def test_load_raises_after_persistent_permerror(self, monkeypatch, tmp_path):
        """load_annotations propagates PermissionError when all retries fail."""
        ann_dir = tmp_path / "annotations" / "bot_b"
        ann_dir.mkdir(parents=True)
        today = date(2026, 5, 12)
        ann_file = ann_dir / f"{today}.jsonl"
        ann_file.write_text("")

        mock_open = _make_open_mock(fail_times=99)
        monkeypatch.setattr(Path, "open", lambda self, *a, **kw: mock_open(*a, **kw))
        monkeypatch.setattr(measure.time, "sleep", lambda s: None)

        with pytest.raises(PermissionError):
            measure.load_annotations(str(tmp_path), "bot_b", today)
