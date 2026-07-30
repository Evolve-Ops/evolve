"""tests/test_home_artifacts_monitor.py — home_artifacts_monitor unit tests.

Workspace-artifact tests use tmp_path to fabricate a fake `.openclaw/`
tree. The Quarantine SQLite tests build a small DB matching the macOS
LSQuarantineEvent schema and inject it via the `db_copier` hook so
nothing in the test touches `/Users/.../Library/Preferences/`.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import home_artifacts_monitor as ham  # noqa: E402
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_macos_profile():
    """The Quarantine DB is a macOS LaunchServices artifact, so the
    quarantine arm of collect()/run() is platform-gated to macOS. Pin the
    macOS profile so those tests exercise the quarantine path on a Linux
    CI runner too. The dedicated Linux-gate tests below override with
    their own set_profile and restore on teardown. (Workspace-artifact
    tests call detect_* directly and are profile-agnostic.)"""
    set_profile(MACOS)
    try:
        yield
    finally:
        set_profile(None)


# ── Workspace artifact scan ─────────────────────────────────────────────────


def _stamp(path: Path, ts: float) -> None:
    os.utime(path, (ts, ts))


def test_no_findings_when_workspace_empty(tmp_path):
    workspace = tmp_path / ".openclaw"
    workspace.mkdir()
    marker = tmp_path / "marker"
    assert ham.detect_workspace_artifacts("bot", workspace, marker) is None
    assert marker.exists()


def test_executable_extension_flagged(tmp_path):
    workspace = tmp_path / ".openclaw"
    workspace.mkdir()
    marker = tmp_path / "marker"
    marker.touch()
    _stamp(marker, time.time() - 600)   # old marker — new files will be after

    payload = workspace / "payload.sh"
    payload.write_text("#!/bin/bash\necho gotcha")
    _stamp(payload, time.time() - 60)

    spec = ham.detect_workspace_artifacts("team-bot-a", workspace, marker)
    assert spec is not None
    assert spec["type"] == ham.SIGNAL_TYPE_WORKSPACE_ARTIFACT
    assert spec["bot_id"] == "team-bot-a"
    assert spec["scope"] == "bot"
    assert len(spec["details"]["findings"]) == 1
    finding = spec["details"]["findings"][0]
    assert finding["path"].endswith("payload.sh")
    assert any("executable" in r for r in finding["reasons"])


def test_large_file_flagged(tmp_path):
    workspace = tmp_path / ".openclaw"
    workspace.mkdir()
    marker = tmp_path / "marker"
    marker.touch()
    _stamp(marker, time.time() - 600)

    big = workspace / "big.bin.unusual"  # not in EXECUTABLE_EXTENSIONS
    # Note: .bin IS in the extension set; use a non-listed suffix to
    # exercise the size-only branch.
    big = workspace / "log.dat"
    with big.open("wb") as f:
        f.write(b"\x00" * (ham.LARGE_FILE_MB * 1024 * 1024 + 1024))
    _stamp(big, time.time() - 60)

    spec = ham.detect_workspace_artifacts("team-bot-a", workspace, marker)
    assert spec is not None
    finding = spec["details"]["findings"][0]
    assert any("MB" in r for r in finding["reasons"])


def test_files_older_than_marker_are_ignored(tmp_path):
    workspace = tmp_path / ".openclaw"
    workspace.mkdir()
    marker = tmp_path / "marker"
    marker.touch()
    _stamp(marker, time.time())   # fresh marker

    old_payload = workspace / "ancient.dmg"
    old_payload.write_text("old binary")
    _stamp(old_payload, time.time() - 3600)   # older than marker

    assert ham.detect_workspace_artifacts("team-bot-a", workspace, marker) is None


def test_excluded_paths_skipped(tmp_path):
    workspace = tmp_path / ".openclaw"
    workspace.mkdir()
    marker = tmp_path / "marker"
    marker.touch()
    _stamp(marker, time.time() - 600)

    git_objects = workspace / ".git" / "objects" / "ab"
    git_objects.mkdir(parents=True)
    pack = git_objects / "deadbeef.pack"
    pack.write_text("not a real pack")
    _stamp(pack, time.time() - 60)

    completions = workspace / ".openclaw" / "completions"
    completions.mkdir(parents=True)
    binary = completions / "huge.bin"
    binary.write_text("ignored")
    _stamp(binary, time.time() - 60)

    assert ham.detect_workspace_artifacts("team-bot-a", workspace, marker) is None


def test_marker_advances_even_on_clean_scan(tmp_path):
    workspace = tmp_path / ".openclaw"
    workspace.mkdir()
    marker = tmp_path / "marker"
    assert not marker.exists()
    ham.detect_workspace_artifacts("team-bot-a", workspace, marker)
    assert marker.exists()


def test_missing_workspace_returns_none(tmp_path):
    """Pre-deploy bots may not have a workspace yet — silent return."""
    marker = tmp_path / "marker"
    assert ham.detect_workspace_artifacts("ghost", tmp_path / "absent", marker) is None


def test_signature_is_per_bot(tmp_path):
    workspace_a = tmp_path / "a" / ".openclaw"
    workspace_b = tmp_path / "b" / ".openclaw"
    for ws in (workspace_a, workspace_b):
        ws.mkdir(parents=True)
        payload = ws / "p.sh"
        payload.write_text("x")
        _stamp(payload, time.time() - 60)

    marker_a = tmp_path / "a-marker"
    marker_b = tmp_path / "b-marker"
    for m in (marker_a, marker_b):
        m.touch()
        _stamp(m, time.time() - 600)

    spec_a = ham.detect_workspace_artifacts("bot_a", workspace_a, marker_a)
    spec_b = ham.detect_workspace_artifacts("bot_b", workspace_b, marker_b)
    assert spec_a is not None and spec_b is not None
    assert spec_a["signature"] != spec_b["signature"]


# ── Quarantine SQLite scan ──────────────────────────────────────────────────


def _build_quarantine_db(path: Path, events: list[tuple[str, str, float]]) -> None:
    """Build a stand-in LSQuarantineEvent DB.

    Each event is (url, agent, unix_ts). Stored as Mac-epoch in the
    LSQuarantineTimeStamp column so the production SELECT shape works.
    """
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE LSQuarantineEvent (
            LSQuarantineDataURLString TEXT,
            LSQuarantineAgentName TEXT,
            LSQuarantineTimeStamp REAL
        )
    """)
    for url, agent, ts in events:
        mac_ts = ts - ham.APPLE_EPOCH_OFFSET
        conn.execute(
            "INSERT INTO LSQuarantineEvent VALUES (?, ?, ?)",
            (url, agent, mac_ts),
        )
    conn.commit()
    conn.close()


def test_quarantine_recent_download_fires(tmp_path):
    db = tmp_path / "qdb.sqlite"
    now = time.time()
    _build_quarantine_db(db, [
        ("https://example.com/payload.dmg", "Safari", now - 30 * 60),  # 30m ago
    ])

    def copier(src, dst):
        # In production src is /Users/.../QuarantineEventsV2; in tests
        # we just hand dst the fake DB we built.
        dst.write_bytes(db.read_bytes())
        return "ok"

    spec = ham.detect_quarantine_downloads(
        "team-bot-a", "team-bot-a", now=now, db_copier=copier,
    )
    assert spec is not None
    assert spec["type"] == ham.SIGNAL_TYPE_QUARANTINE_DOWNLOAD
    assert spec["bot_id"] == "team-bot-a"
    assert len(spec["details"]["events"]) == 1
    assert spec["details"]["events"][0]["agent"] == "Safari"


def test_quarantine_old_event_ignored(tmp_path):
    db = tmp_path / "qdb.sqlite"
    now = time.time()
    # 8 hours ago — outside the 6h lookback
    _build_quarantine_db(db, [
        ("https://example.com/old.dmg", "Safari", now - 8 * 3600),
    ])

    def copier(src, dst):
        dst.write_bytes(db.read_bytes())
        return "ok"

    assert ham.detect_quarantine_downloads(
        "team-bot-a", "team-bot-a", now=now, db_copier=copier,
    ) is None


def test_quarantine_copy_error_fires_tooling_signal(tmp_path):
    """A failed copy is a tooling failure, not "no findings".

    Pre-2026-06-10 this returned None — the sudoers grant for the sudo
    cp never existed, so every cycle silently produced nothing and a
    broken check was indistinguishable from a quiet bot.
    """
    def failing_copier(src, dst):
        return "error"

    spec = ham.detect_quarantine_downloads(
        "ghost", "ghost", db_copier=failing_copier,
    )
    assert spec is not None
    assert spec["type"] == ham.SIGNAL_TYPE_QUARANTINE_CHECK_FAILED
    assert spec["bot_id"] == "ghost"
    assert "tooling failure" in spec["body"]


def test_quarantine_check_failed_signature_is_per_bot():
    def failing_copier(src, dst):
        return "error"

    spec_a = ham.detect_quarantine_downloads(
        "bot_a", "bot_a", db_copier=failing_copier,
    )
    spec_b = ham.detect_quarantine_downloads(
        "bot_b", "bot_b", db_copier=failing_copier,
    )
    assert spec_a["signature"] != spec_b["signature"]


def test_quarantine_missing_db_is_silent(tmp_path):
    def missing_copier(src, dst):
        # The real copier returns "missing" when src doesn't exist
        # (bot never initialised quarantine) — not a finding.
        return "missing"

    spec = ham.detect_quarantine_downloads(
        "team-bot-a", "nonexistent_user", db_copier=missing_copier,
    )
    assert spec is None


def test_quarantine_corrupt_db_is_silent(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_text("not a database")

    def copier(src, dst):
        dst.write_bytes(bad.read_bytes())
        return "ok"

    spec = ham.detect_quarantine_downloads(
        "team-bot-a", "team-bot-a", db_copier=copier,
    )
    assert spec is None


class _FakeCompleted:
    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def _patch_cp(monkeypatch, returncode: int, stderr: str = ""):
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["sudo", "-n", "/bin/cp"]
        return _FakeCompleted(returncode, stderr)
    monkeypatch.setattr(ham.subprocess, "run", fake_run)


def test_sudo_copier_classifies_grant_missing_as_error(monkeypatch, tmp_path):
    """sudo -n with no matching grant → "error", never "missing".

    This is the failure mode that motivated the tri-state: the sudoers
    grant didn't exist, every copy died with "a password is required",
    and the old bool contract collapsed it into the same False as a
    legitimately absent DB.
    """
    _patch_cp(monkeypatch, 1, "sudo: a password is required\n")
    dst = tmp_path / "snap.sqlite"
    dst.touch()
    assert ham._sudo_copy_quarantine_db(Path("/Users/x/Library/db"), dst) == "error"


def test_sudo_copier_classifies_absent_source_as_missing(monkeypatch, tmp_path):
    _patch_cp(
        monkeypatch, 1,
        "cp: /Users/x/Library/Preferences/com.apple.LaunchServices."
        "QuarantineEventsV2: No such file or directory\n",
    )
    dst = tmp_path / "snap.sqlite"
    dst.touch()
    assert ham._sudo_copy_quarantine_db(Path("/Users/x/Library/db"), dst) == "missing"


def test_sudo_copier_success_is_ok(monkeypatch, tmp_path):
    _patch_cp(monkeypatch, 0)
    dst = tmp_path / "snap.sqlite"
    dst.touch()
    assert ham._sudo_copy_quarantine_db(Path("/Users/x/Library/db"), dst) == "ok"


def test_quarantine_tmp_file_cleaned_up(tmp_path, monkeypatch):
    """The tmp DB copy must be unlinked even on a successful read."""
    db = tmp_path / "qdb.sqlite"
    now = time.time()
    _build_quarantine_db(db, [
        ("u", "a", now - 60),
    ])
    captured = {}

    def copier(src, dst):
        captured["dst"] = dst
        dst.write_bytes(db.read_bytes())
        return "ok"

    ham.detect_quarantine_downloads("team-bot-a", "team-bot-a", now=now, db_copier=copier)
    assert not captured["dst"].exists(), "tmp DB copy should be unlinked"


# ── Runner ─────────────────────────────────────────────────────────────────


def test_collect_yields_per_bot_specs(tmp_path, monkeypatch):
    # Two bots, one with a payload in workspace, one clean
    bot_a_home = tmp_path / "bot_a"
    bot_b_home = tmp_path / "bot_b"
    for h in (bot_a_home, bot_b_home):
        (h / ".openclaw").mkdir(parents=True)
    # marker dir lives under shared_dir/home_artifacts/<bot>.marker
    shared_dir = tmp_path / "shared"

    payload = bot_a_home / ".openclaw" / "payload.dmg"
    payload.write_text("x")
    _stamp(payload, time.time() - 60)

    # Stub the quarantine path so neither bot's DB read fires.
    monkeypatch.setattr(
        ham, "_sudo_copy_quarantine_db", lambda src, dst: "missing",
    )

    def fake_bot_home(bot_id, config):
        return {"bot_a": bot_a_home, "bot_b": bot_b_home}[bot_id]

    monkeypatch.setattr(ham, "bot_home", fake_bot_home)
    monkeypatch.setattr(ham, "get_members", lambda cfg: ["bot_a", "bot_b"])

    specs = ham.collect({}, shared_dir)
    # Exactly one — for bot_a's workspace artifact. bot_b clean.
    assert len(specs) == 1
    assert specs[0]["bot_id"] == "bot_a"
    assert specs[0]["type"] == ham.SIGNAL_TYPE_WORKSPACE_ARTIFACT


# ── Platform gate (quarantine arm) ──────────────────────────────────────────


def test_quarantine_check_skipped_on_linux(tmp_path, monkeypatch):
    """On Linux there is no LaunchServices QuarantineEventsV2 DB, so the
    `sudo cp` always failed → `quarantine_check_failed` fired every hourly
    pass on a fresh Linux pod. The macOS gate skips the quarantine arm
    entirely on Linux. We force the copier to return "error" (the firing
    case on macOS) to prove the gate, not a benign copier result, is what
    suppresses it. The workspace-artifact arm still runs on Linux."""
    bot_home_dir = tmp_path / "bot_a"
    (bot_home_dir / ".openclaw").mkdir(parents=True)
    shared_dir = tmp_path / "shared"

    payload = bot_home_dir / ".openclaw" / "payload.dmg"
    payload.write_text("x")
    _stamp(payload, time.time() - 60)

    # Would fire quarantine_check_failed on macOS; must be UNCALLED on Linux.
    called = {"n": 0}

    def _explode(src, dst):
        called["n"] += 1
        return "error"

    monkeypatch.setattr(ham, "_sudo_copy_quarantine_db", _explode)
    monkeypatch.setattr(ham, "bot_home", lambda bot_id, config: bot_home_dir)
    monkeypatch.setattr(ham, "get_members", lambda cfg: ["bot_a"])

    set_profile(LINUX)
    try:
        specs = ham.collect({}, shared_dir)
    finally:
        set_profile(MACOS)

    types = {s["type"] for s in specs}
    # Workspace arm runs cross-platform; quarantine arm is gated off.
    assert ham.SIGNAL_TYPE_WORKSPACE_ARTIFACT in types
    assert ham.SIGNAL_TYPE_QUARANTINE_CHECK_FAILED not in types
    assert ham.SIGNAL_TYPE_QUARANTINE_DOWNLOAD not in types
    assert called["n"] == 0, "quarantine copier must not be invoked on Linux"


def test_quarantine_check_runs_on_macos(tmp_path, monkeypatch):
    """macOS byte-identity: with the copier erroring, the quarantine arm
    still fires quarantine_check_failed. Pairs with the Linux no-op above."""
    bot_home_dir = tmp_path / "bot_a"
    (bot_home_dir / ".openclaw").mkdir(parents=True)
    shared_dir = tmp_path / "shared"

    monkeypatch.setattr(
        ham, "_sudo_copy_quarantine_db", lambda src, dst: "error",
    )
    monkeypatch.setattr(ham, "bot_home", lambda bot_id, config: bot_home_dir)
    monkeypatch.setattr(ham, "get_members", lambda cfg: ["bot_a"])

    set_profile(MACOS)
    try:
        specs = ham.collect({}, shared_dir)
    finally:
        set_profile(None)

    types = {s["type"] for s in specs}
    assert ham.SIGNAL_TYPE_QUARANTINE_CHECK_FAILED in types


def test_check_failed_shields_download_signature_from_sweep(tmp_path, monkeypatch):
    """A broken check must not sweep-resolve a still-firing download Signal.

    When the copy errors, detect_quarantine_downloads returns the
    check-failed spec INSTEAD of a download spec, so the download
    signature would drop out of kept_signatures and sweep_resolve would
    archive a real prior finding exactly when the tool lost the ability
    to confirm it cleared. run() shields it by keeping the download
    signature alongside the check-failed one.
    """
    bot_home_dir = tmp_path / "bot_a"
    (bot_home_dir / ".openclaw").mkdir(parents=True)
    shared_dir = tmp_path / "shared"

    monkeypatch.setattr(
        ham, "_sudo_copy_quarantine_db", lambda src, dst: "error",
    )
    monkeypatch.setattr(ham, "bot_home", lambda bot_id, config: bot_home_dir)
    monkeypatch.setattr(ham, "get_members", lambda cfg: ["bot_a"])

    kept, n_fired, _ = ham.run({}, shared_dir, dry_run=True)
    assert n_fired == 1
    assert ham.make_signature(
        ham.PRODUCER, ham.SIGNAL_TYPE_QUARANTINE_CHECK_FAILED, "bot_a"
    ) in kept
    # The shield: the download signature stays in the keep set even
    # though no download spec was emitted this cycle.
    assert ham.make_signature(
        ham.PRODUCER, ham.SIGNAL_TYPE_QUARANTINE_DOWNLOAD, "bot_a"
    ) in kept
