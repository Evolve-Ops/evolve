"""tests/test_log_cap.py — copy-then-truncate flat-file log rotation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import log_cap  # noqa: E402


def test_under_cap_is_noop(tmp_path):
    p = tmp_path / "audit.log"
    p.write_bytes(b"a" * 100)
    result = log_cap.cap_log(p, max_bytes=1024, keep=3)
    assert result.rotated is False
    assert result.size_before == 100
    assert p.read_bytes() == b"a" * 100


def test_over_cap_rotates_and_truncates_in_place(tmp_path):
    p = tmp_path / "audit.log"
    contents = b"x" * 2048
    p.write_bytes(contents)
    inode_before = p.stat().st_ino

    result = log_cap.cap_log(p, max_bytes=1024, keep=3)

    assert result.rotated is True
    assert result.size_before == 2048
    assert p.exists()
    assert p.read_bytes() == b""
    # Inode must be preserved so any open fd (notably launchd's
    # StandardOutPath fd on better_engine.log) keeps writing to the
    # now-empty on-disk file rather than going to an orphaned copy.
    assert p.stat().st_ino == inode_before

    backup = p.with_name("audit.log.1")
    assert backup.exists()
    assert backup.read_bytes() == contents


def test_open_fd_keeps_writing_to_same_file_after_rotation(tmp_path):
    """The load-bearing reason for copy-then-truncate: simulate a
    launchd StandardOutPath holding the file open across rotation."""
    p = tmp_path / "better_engine.log"
    p.write_bytes(b"old data " * 200)  # 1800 bytes

    # Open with O_APPEND like launchd does for StandardOutPath.
    fd = os.open(p, os.O_WRONLY | os.O_APPEND)
    try:
        result = log_cap.cap_log(p, max_bytes=1024, keep=3)
        assert result.rotated is True
        # Writes to the still-open fd land in the rotated file (now empty
        # on disk) — same path, same inode, just truncated.
        os.write(fd, b"new line\n")
    finally:
        os.close(fd)

    assert p.read_bytes() == b"new line\n"
    backup = p.with_name("better_engine.log.1")
    assert backup.exists()
    assert backup.read_bytes().startswith(b"old data ")


def test_backup_shift_preserves_history(tmp_path):
    p = tmp_path / "audit.log"
    # Pre-seed older backups.
    p.with_name("audit.log.1").write_bytes(b"prev-1")
    p.with_name("audit.log.2").write_bytes(b"prev-2")
    p.write_bytes(b"current" + b"y" * 2048)

    result = log_cap.cap_log(p, max_bytes=1024, keep=3)

    assert result.rotated is True
    assert p.with_name("audit.log.1").read_bytes().startswith(b"current")
    assert p.with_name("audit.log.2").read_bytes() == b"prev-1"
    assert p.with_name("audit.log.3").read_bytes() == b"prev-2"


def test_oldest_backup_dropped_at_keep_cap(tmp_path):
    p = tmp_path / "audit.log"
    p.with_name("audit.log.1").write_bytes(b"prev-1")
    p.with_name("audit.log.2").write_bytes(b"prev-2")
    p.with_name("audit.log.3").write_bytes(b"prev-3-oldest")
    p.write_bytes(b"z" * 2048)

    result = log_cap.cap_log(p, max_bytes=1024, keep=3)

    assert result.rotated is True
    # prev-3 is gone; shift moved prev-2→.3 and prev-1→.2.
    assert p.with_name("audit.log.1").read_bytes().startswith(b"z")
    assert p.with_name("audit.log.2").read_bytes() == b"prev-1"
    assert p.with_name("audit.log.3").read_bytes() == b"prev-2"
    assert not p.with_name("audit.log.4").exists()


def test_missing_file_is_silent_skip(tmp_path):
    p = tmp_path / "never-existed.log"
    result = log_cap.cap_log(p, max_bytes=1024, keep=3)
    assert result.rotated is False
    assert result.size_before is None
    assert result.error is None


def test_idempotent_when_already_under_cap_post_rotation(tmp_path):
    p = tmp_path / "audit.log"
    p.write_bytes(b"q" * 2048)
    log_cap.cap_log(p, max_bytes=1024, keep=3)
    # Second call should be a no-op since file is now empty.
    result = log_cap.cap_log(p, max_bytes=1024, keep=3)
    assert result.rotated is False
    assert result.size_before == 0


def test_jsonl_files_handled_identically(tmp_path):
    p = tmp_path / "audit-warns.jsonl"
    p.write_bytes(b'{"ts":"x"}\n' * 200)  # 2200 bytes
    result = log_cap.cap_log(p, max_bytes=1024, keep=3)
    assert result.rotated is True
    assert p.read_bytes() == b""
    assert p.with_name("audit-warns.jsonl.1").exists()


def test_cap_logs_batch(tmp_path):
    a = tmp_path / "a.log"
    b = tmp_path / "b.jsonl"
    a.write_bytes(b"a" * 2048)
    b.write_bytes(b"b" * 10)  # under cap
    missing = tmp_path / "nope.log"

    results = log_cap.cap_logs([a, b, missing], max_bytes=1024, keep=3)
    assert len(results) == 3
    assert results[0].rotated is True
    assert results[1].rotated is False
    assert results[1].size_before == 10
    assert results[2].size_before is None


def test_cli_main_no_paths_uses_defaults(tmp_path, monkeypatch, capsys):
    """With no positional paths, falls back to DEFAULT_TARGETS so the
    launchd plist only needs to invoke the script."""
    # Point DEFAULT_TARGETS at tmp-local files so we don't touch real ones.
    target = tmp_path / "audit.log"
    target.write_bytes(b"x" * 2048)
    monkeypatch.setattr(log_cap, "DEFAULT_TARGETS", (target,))

    rc = log_cap._main(["--max-bytes", "1024", "--keep", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rotated=1" in out
    assert target.read_bytes() == b""
    assert target.with_name("audit.log.1").read_bytes() == b"x" * 2048


def test_keep_must_be_at_least_one(tmp_path):
    p = tmp_path / "audit.log"
    p.write_bytes(b"x" * 2048)
    result = log_cap.cap_log(p, max_bytes=1024, keep=0)
    assert result.rotated is False
    assert result.error and "keep" in result.error


def test_default_targets_cover_the_known_logs():
    """Regression guard: the files this module was created to cap must
    stay in DEFAULT_TARGETS. Adding paths is fine; silently dropping one
    of these would re-expose the original disk-fillup risk."""
    target_names = {p.name for p in log_cap.DEFAULT_TARGETS}
    assert "audit.log" in target_names
    assert "better_engine.log" in target_names
    assert "audit-warns.jsonl" in target_names
    # The admin-ui daemon's launchd StandardErrorPath. Uncapped it grew
    # to 11.5 MB with no timestamps of its own, so the 2026-07-21..26
    # EMFILE storm (#3446) still read as a live failure weeks after the
    # fix shipped. Capping bounds the growth AND the misdiagnosis.
    assert "evolve-admin-ui.err.log" in target_names
