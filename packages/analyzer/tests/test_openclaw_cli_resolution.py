"""tests/test_openclaw_cli_resolution.py — every analyzer-side openclaw CLI
lookup goes through ``platform_profile.find_openclaw_cli``, resolved at CALL
time.

Three analyzer call sites used to hardcode (or re-copy) the macOS Homebrew
path:

  * ``doctor_pass_runner.OPENCLAW`` — a module-level
    ``/opt/homebrew/bin/openclaw``. The nightly per-bot ``doctor --fix``
    daemon also runs on the Linux VPS pod, where that path does not exist, so
    every invocation there gave up before doing any work. Module-level
    resolution also froze the path at import, defeating any test that pins a
    platform profile.
  * ``oc_cli._find_openclaw`` — a private re-copy of the candidate list that
    omitted the Linux ``/usr/bin/openclaw`` symlink.
  * ``audit._OC_BINARY_CANDIDATES`` — a two-entry macOS Homebrew list, so an
    npm/node_modules-only install read as "binary not found" and the mtime
    watch never armed. (The check itself stays ``applies_to={"macos"}``;
    widening it to Linux is a separate call, but its resolver no longer
    blocks that.)

These tests pin the collapse. The PATH-based cases resolve for real (no
stubbed resolver) so a future re-copy of the candidate list can't pass them
by accident.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import doctor_pass_runner  # noqa: E402
import oc_cli  # noqa: E402
import platform_profile  # noqa: E402


def _fake_openclaw_on_path(tmp_path: Path, monkeypatch) -> Path:
    """Put an executable named ``openclaw`` first on PATH and return it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "openclaw"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return fake


# ── the module-level constant is gone ────────────────────────────────────────


def test_no_module_level_openclaw_constants():
    """A module-level path is resolved at IMPORT time — it can't follow a
    platform profile, and it can't be steered by a test."""
    assert not hasattr(doctor_pass_runner, "OPENCLAW")
    assert not hasattr(audit, "_OC_BINARY_CANDIDATES")


# ── doctor_pass_runner ───────────────────────────────────────────────────────


def test_doctor_pass_runner_invokes_the_resolved_binary(tmp_path: Path, monkeypatch):
    """The argv's binary is whatever the resolver returned — not a constant."""
    fake = _fake_openclaw_on_path(tmp_path, monkeypatch)
    seen: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(doctor_pass_runner.subprocess, "run",
                        lambda argv, **_k: seen.append(argv) or _R())
    monkeypatch.setattr(sys, "argv", ["doctor_pass_runner.py", "--bot-id", "demo_bot"])

    assert doctor_pass_runner.main() == 0
    assert seen == [[str(fake), "doctor", "--fix"]]


def test_doctor_pass_runner_missing_cli_exits_nonzero(monkeypatch, capsys):
    """No openclaw anywhere → a clear error and a non-zero exit, so the
    service manager's last-exit-status surfaces it. Silently returning 0 is
    how the Linux-pod breakage stayed invisible."""
    monkeypatch.setattr(doctor_pass_runner, "find_openclaw_cli", lambda: None)
    monkeypatch.setattr(doctor_pass_runner.subprocess, "run",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("must not run doctor without a CLI")))
    monkeypatch.setattr(sys, "argv", ["doctor_pass_runner.py", "--bot-id", "demo_bot"])

    assert doctor_pass_runner.main() == 1
    assert "openclaw CLI not found" in capsys.readouterr().out


# ── oc_cli ───────────────────────────────────────────────────────────────────


def test_oc_cli_resolves_through_the_shared_resolver(tmp_path: Path, monkeypatch):
    fake = _fake_openclaw_on_path(tmp_path, monkeypatch)
    assert oc_cli._find_openclaw() == str(fake)
    # …and it is the same resolution the shared helper performs.
    assert oc_cli._find_openclaw() == platform_profile.find_openclaw_cli()


def test_oc_cli_covers_the_linux_symlink_candidate():
    """The private copy this replaced listed only the macOS symlinks plus the
    node_modules entrypoints — /usr/bin/openclaw (the Linux install) was
    missing, so a Linux pod fell through to "openclaw not found in PATH"."""
    assert "/usr/bin/openclaw" in platform_profile.OPENCLAW_CLI_CANDIDATES


# ── audit._check_oc_binary_mtime ─────────────────────────────────────────────


def test_oc_binary_mtime_uses_the_shared_resolver(tmp_path: Path, monkeypatch):
    """A binary the old macOS-only Homebrew list would have missed is still
    watched, because resolution now runs through the shared candidate list."""
    fake = _fake_openclaw_on_path(tmp_path, monkeypatch)
    monkeypatch.setattr(audit, "_read_openclaw_version", lambda _p: "openclaw 2026.4.29")

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "ok"
    assert "baseline created" in findings[0].message
    baseline = json.loads((tmp_path / "security" / "oc-binary-mtime.baseline").read_text())
    assert baseline["path"] == str(fake)


def test_oc_binary_mtime_path_move_rebases_instead_of_warning(tmp_path: Path, monkeypatch):
    """Baseline taken from a different binary → the two mtimes aren't
    comparable, so rebase rather than cry "replaced without a version bump"."""
    fake = _fake_openclaw_on_path(tmp_path, monkeypatch)
    os.utime(fake, (2000.0, 2000.0))
    monkeypatch.setattr(audit, "_read_openclaw_version", lambda _p: "openclaw 2026.4.29")
    baseline_path = tmp_path / "security" / "oc-binary-mtime.baseline"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps({
        "mtime": "1000",
        "version": "openclaw 2026.4.29",
        "path": "/opt/homebrew/bin/openclaw",
    }))

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "ok"
    assert "path moved" in findings[0].message
    assert json.loads(baseline_path.read_text())["path"] == str(fake)


def test_oc_binary_mtime_not_found_when_nothing_resolves(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "find_openclaw_cli", lambda: None)

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "warn"
    assert "not found" in findings[0].message
