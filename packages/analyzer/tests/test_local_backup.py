"""tests/test_local_backup.py — Time Machine status wrapper tests."""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import local_backup as lb  # noqa: E402


# ─── Fixtures for fake tmutil output ────────────────────────────────────────

_DESTINFO_TWO_DESTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Destinations</key>
  <array>
    <dict>
      <key>Name</key><string>Backup SSD</string>
      <key>Kind</key><string>Local</string>
      <key>MountPoint</key><string>/Volumes/Backup SSD</string>
      <key>ID</key><string>ABC-DEF-123</string>
      <key>BytesAvailable</key><integer>500000000000</integer>
      <key>BytesUsed</key><integer>200000000000</integer>
      <key>LastDestination</key><integer>1</integer>
    </dict>
    <dict>
      <key>Name</key><string>NAS Volume</string>
      <key>Kind</key><string>Network</string>
      <key>ID</key><string>XYZ-789</string>
    </dict>
  </array>
</dict>
</plist>"""

_DESTINFO_EMPTY = b""

_STATUS_RUNNING = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Running</key><integer>1</integer>
  <key>BackupPhase</key><string>Copying</string>
</dict>
</plist>"""

_STATUS_IDLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Running</key><integer>0</integer>
  <key>BackupPhase</key><string>BackupNotRunning</string>
</dict>
</plist>"""


def _ok(stdout: bytes | str) -> subprocess.CompletedProcess:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8")
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _make_runner(by_subcmd: dict[str, subprocess.CompletedProcess]):
    """Build a fake _run that dispatches by tmutil subcommand name."""
    def _run(args, *, timeout=8.0):
        # tmutil_path, subcmd, *rest
        sub = args[1] if len(args) > 1 else ""
        if sub == "isexcluded":
            # Look up by full args key if present, else fallback to "isexcluded"
            key = f"isexcluded:{args[2]}" if len(args) > 2 else "isexcluded"
            return by_subcmd.get(key) or by_subcmd.get("isexcluded") or _fail()
        return by_subcmd.get(sub) or _fail()
    return _run


# ─── _parse_snapshot_path ──────────────────────────────────────────────────

def test_parse_snapshot_path_typical():
    assert lb._parse_snapshot_path(
        "/Volumes/Backup SSD/Backups.backupdb/MyMac/2026-05-28-120530",
    ) == "2026-05-28T12:05:30Z"


def test_parse_snapshot_path_trailing_slash():
    assert lb._parse_snapshot_path(
        "/Volumes/Backup SSD/Backups.backupdb/MyMac/2026-05-28-120530/",
    ) == "2026-05-28T12:05:30Z"


def test_parse_snapshot_path_invalid():
    assert lb._parse_snapshot_path("/Volumes/Backup SSD/random-not-a-snapshot") is None
    assert lb._parse_snapshot_path("") is None
    assert lb._parse_snapshot_path("2026-13-01-120000") is None  # invalid month


# ─── _hours_ago ────────────────────────────────────────────────────────────

def test_hours_ago_recent():
    now = _dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=_dt.timezone.utc)
    assert lb._hours_ago("2026-05-28T06:00:00Z", now=now) == 6.0


def test_hours_ago_none_input():
    assert lb._hours_ago(None) is None
    assert lb._hours_ago("") is None


def test_hours_ago_malformed():
    assert lb._hours_ago("not-an-iso") is None


# ─── _parse_destinations ───────────────────────────────────────────────────

def test_parse_destinations_two_entries():
    dests = lb._parse_destinations(_DESTINFO_TWO_DESTS)
    assert len(dests) == 2
    assert dests[0].name == "Backup SSD"
    assert dests[0].kind == "Local"
    assert dests[0].mount_point == "/Volumes/Backup SSD"
    assert dests[0].last_destination is True
    assert dests[0].bytes_available == 500_000_000_000
    assert dests[1].name == "NAS Volume"
    assert dests[1].kind == "Network"
    assert dests[1].mount_point is None
    assert dests[1].last_destination is False


def test_parse_destinations_empty():
    assert lb._parse_destinations(b"") == []
    assert lb._parse_destinations(b"<not><valid>plist") == []


def test_parse_destinations_missing_keys_uses_defaults():
    minimal = b"""<?xml version="1.0"?>
<plist version="1.0"><dict><key>Destinations</key><array>
<dict></dict>
</array></dict></plist>"""
    dests = lb._parse_destinations(minimal)
    assert len(dests) == 1
    assert dests[0].name == "(unnamed)"
    assert dests[0].kind == "Unknown"


# ─── _parse_status ─────────────────────────────────────────────────────────

def test_parse_status_running():
    assert lb._parse_status(_STATUS_RUNNING) is True


def test_parse_status_idle():
    assert lb._parse_status(_STATUS_IDLE) is False


def test_parse_status_phase_without_running_is_idle():
    """Regression for the 2026-05-29 review-session fix.

    The earlier code treated any non-BackupNotRunning ``BackupPhase`` as
    ``in_progress=True``, which false-positived on phases like
    ``Idle`` / ``ThinningPostBackup`` / ``Starting``. ``Running`` is
    authoritative; ``BackupPhase`` alone is not.
    """
    payload = b"""<?xml version="1.0"?>
<plist version="1.0"><dict>
  <key>BackupPhase</key><string>Idle</string>
</dict></plist>"""
    assert lb._parse_status(payload) is False


def test_parse_status_idle_phase_with_running_is_active():
    """Running=1 is authoritative even when phase is transient."""
    payload = b"""<?xml version="1.0"?>
<plist version="1.0"><dict>
  <key>Running</key><integer>1</integer>
  <key>BackupPhase</key><string>ThinningPostBackup</string>
</dict></plist>"""
    assert lb._parse_status(payload) is True


def test_parse_status_empty():
    assert lb._parse_status(b"") is False


# ─── _parse_isexcluded ─────────────────────────────────────────────────────

def test_parse_isexcluded_true():
    assert lb._parse_isexcluded("[Excluded]    /Users/Shared/evolve\n") is True


def test_parse_isexcluded_false():
    assert lb._parse_isexcluded("[Included]    /Users/Shared/evolve\n") is False


def test_parse_isexcluded_unknown():
    assert lb._parse_isexcluded("") is None
    assert lb._parse_isexcluded("garbage") is None


# ─── get_local_backup_status — integration shape ───────────────────────────

def test_status_non_macos_returns_unavailable(monkeypatch):
    # Force the tmutil path check to fail.
    monkeypatch.setattr("local_backup.Path.exists", lambda self: False)
    s = lb.get_local_backup_status()
    assert s.available is False
    assert s.configured is False
    assert s.destinations == []
    assert s.error is None


def test_status_no_destinations_configured(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)
    _run = _make_runner({
        "destinationinfo": _fail(1, "No destinations configured."),
    })
    s = lb.get_local_backup_status(_run=_run)
    assert s.available is True
    assert s.configured is False
    assert s.destinations == []
    assert s.last_backup_at is None
    assert s.in_progress is False


def test_status_full_happy_path(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)
    _run = _make_runner({
        "destinationinfo": _ok(_DESTINFO_TWO_DESTS),
        "latestbackup":    _ok("/Volumes/Backup SSD/Backups.backupdb/MyMac/2026-05-28-060000"),
        "status":          _ok(_STATUS_IDLE),
        "isexcluded":      _ok("[Included]    /Users/Shared/evolve"),
    })
    now = _dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=_dt.timezone.utc)
    s = lb.get_local_backup_status(
        pod_paths=[Path("/Users/Shared/evolve")],
        _run=_run, _now=now,
    )
    assert s.available is True
    assert s.configured is True
    assert len(s.destinations) == 2
    assert s.last_backup_at == "2026-05-28T06:00:00Z"
    assert s.last_backup_hours_ago == 6.0
    assert s.in_progress is False
    assert s.excluded_pod_paths == []
    assert s.settings_deeplink.startswith("x-apple.systempreferences:")


def test_status_running_backup_in_progress(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)
    _run = _make_runner({
        "destinationinfo": _ok(_DESTINFO_TWO_DESTS),
        "latestbackup":    _ok("/Volumes/Backup SSD/Backups.backupdb/MyMac/2026-05-28-060000"),
        "status":          _ok(_STATUS_RUNNING),
        "isexcluded":      _ok("[Included]    /Users/Shared/evolve"),
    })
    s = lb.get_local_backup_status(
        pod_paths=[Path("/Users/Shared/evolve")], _run=_run,
    )
    assert s.in_progress is True


def test_status_pod_path_excluded(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)
    _run = _make_runner({
        "destinationinfo": _ok(_DESTINFO_TWO_DESTS),
        "latestbackup":    _ok("/Volumes/Backup SSD/Backups.backupdb/MyMac/2026-05-28-060000"),
        "status":          _ok(_STATUS_IDLE),
        "isexcluded:/Users/Shared/evolve":      _ok("[Excluded]    /Users/Shared/evolve"),
        "isexcluded:/Users/Shared/evolve-repo": _ok("[Included]    /Users/Shared/evolve-repo"),
    })
    s = lb.get_local_backup_status(
        pod_paths=[Path("/Users/Shared/evolve"), Path("/Users/Shared/evolve-repo")],
        _run=_run,
    )
    assert s.excluded_pod_paths == ["/Users/Shared/evolve"]


def test_status_never_backed_up(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)
    _run = _make_runner({
        "destinationinfo": _ok(_DESTINFO_TWO_DESTS),
        "latestbackup":    _fail(1, "No backups available"),
        "status":          _ok(_STATUS_IDLE),
    })
    s = lb.get_local_backup_status(_run=_run)
    assert s.configured is True
    assert s.last_backup_at is None
    assert s.last_backup_hours_ago is None


def test_status_destinationinfo_timeout(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)

    def _run(args, *, timeout=8.0):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    s = lb.get_local_backup_status(_run=_run)
    assert s.available is True
    assert s.configured is False
    assert "timed out" in (s.error or "").lower() or "tmutil destinationinfo" in (s.error or "")


def test_status_to_dict_is_jsonable(monkeypatch):
    monkeypatch.setattr("local_backup.Path.exists", lambda self: True)
    _run = _make_runner({
        "destinationinfo": _ok(_DESTINFO_TWO_DESTS),
        "latestbackup":    _ok("/Volumes/Backup SSD/Backups.backupdb/MyMac/2026-05-28-060000"),
        "status":          _ok(_STATUS_IDLE),
    })
    import json
    s = lb.get_local_backup_status(_run=_run)
    blob = json.dumps(s.to_dict())  # must not raise
    assert "Backup SSD" in blob
    assert "settings_deeplink" in blob
