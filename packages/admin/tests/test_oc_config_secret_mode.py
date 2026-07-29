"""Guard: token-bearing bot config files are written/healed at mode 0600.

Regression test for the 2026-06-12 privilege-boundary fix. The channel-
connect path (``write_oc_config``) and the deploy config-write helpers were
leaving ``openclaw.json`` world-readable — a bare ``sudo /bin/cp`` (no ``-p``)
creates the dest at root's umask (0644), and ``openclaw.json`` holds the
gateway token + every messaging-channel bot token. On a multi-user box that
exposed every freshly channel-connected bot's token until a later deploy
re-healed the mode.

Two layers are covered:
  1. SOURCE — ``write_oc_config`` issues ``chmod 600`` (never 644) on the dest.
  2. SELF-HEAL — ``ensure_pod_perms``' ``_check_bot_secret_modes`` flags a
     drifted 0644 ``openclaw.json`` and its fix runs ``sudo /bin/chmod 600``.

Placeholder bot names per docs/PLACEHOLDER_NAMING.md; no real tokens.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from evolve_admin import secret_config_perms as scp
from evolve_admin.skills import _oc_install_common as _oc_common


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestWriteOcConfigMode:
    """``write_oc_config`` is the channel-connect chokepoint (#2707). It must
    tighten openclaw.json to 0600, never leave it world-readable 0644."""

    def test_chmods_600_not_644(self, monkeypatch):
        # Resolve a deterministic dest without consulting the real network.json.
        monkeypatch.setattr("evolve_admin.config.load_network", lambda *a, **k: {})
        monkeypatch.setattr(
            "evolve_admin.config.get_bot_user", lambda *a, **k: "team-bot-a"
        )
        monkeypatch.setattr(
            "evolve_admin.config.bot_home", lambda *a, **k: Path("/Users/team-bot-a")
        )
        # No prior config on disk (skips the sudo-cat read); no real
        # briefing-activation hook.
        monkeypatch.setattr(_oc_common, "read_oc_config", lambda bot_id: ({}, None))
        _oc_common.set_channel_connect_hook(lambda *a, **k: None)

        calls: list[list[str]] = []

        # Seam: write_oc_config calls subprocess.run directly in THIS module,
        # so patching <module>.subprocess.run is the correct interception
        # point — the code is not moving out of _oc_install_common (cf. the
        # subprocess-patch-fakes-break-on-module-move lesson).
        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(_oc_common.subprocess, "run", fake_run)

        try:
            ok, err = _oc_common.write_oc_config(
                "team-bot-a",
                {"channels": {"telegram": {"botToken": "PLACEHOLDER"}}},
            )
        finally:
            _oc_common.set_channel_connect_hook(None)

        assert ok and err is None
        dest = "/Users/team-bot-a/.openclaw/openclaw.json"
        chmods = [c for c in calls if c[:2] == ["sudo", "/bin/chmod"]]
        # The fix: openclaw.json is tightened to 0600.
        assert ["sudo", "/bin/chmod", "600", dest] in calls, calls
        # The bug guarded against: any 0644 chmod of the token file.
        assert not any(
            c[:3] == ["sudo", "/bin/chmod", "644"] for c in chmods
        ), chmods


class TestCheckBotSecretModes:
    """``secret_config_perms.check_bot_secret_modes`` is the deploy-time
    self-heal (wired into ensure_pod_perms) that converges drifted token
    files back to 0600 and surfaces drift as a Signal via the hourly
    pod_perms_drift_monitor."""

    @staticmethod
    def _make_oc(home: Path, mode: int) -> Path:
        oc_dir = home / ".openclaw"
        oc_dir.mkdir(parents=True, exist_ok=True)
        oc_json = oc_dir / "openclaw.json"
        oc_json.write_text("{}")
        os.chmod(oc_json, mode)
        return oc_json

    def _oc_check(self, checks):
        oc = [c for c in checks if c.target.endswith("/openclaw.json")]
        assert oc, "no openclaw.json check produced"
        return oc[0]

    def test_passes_when_600(self, tmp_path, monkeypatch):
        self._make_oc(tmp_path, 0o600)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        oc = self._oc_check(scp.check_bot_secret_modes("team-bot-a"))
        assert oc.ok, oc.detail

    def test_flags_644_and_repairs_to_600(self, tmp_path, monkeypatch):
        oc_json = self._make_oc(tmp_path, 0o644)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        oc = self._oc_check(scp.check_bot_secret_modes("team-bot-a"))
        assert not oc.ok
        assert "0o644" in oc.detail
        assert oc.apply is not None

        # The fix issues `sudo /bin/chmod 600 <path>` — fake the sudo seam so
        # the test never shells out to real sudo. chmod_secret_config calls
        # subprocess.run directly in secret_config_perms, so patch it there.
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert oc.apply() is True
        assert ["sudo", "/bin/chmod", "600", str(oc_json)] in calls, calls

    def test_missing_file_is_noop_ok(self, tmp_path, monkeypatch):
        (tmp_path / ".openclaw").mkdir(parents=True)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        oc = self._oc_check(scp.check_bot_secret_modes("team-bot-a"))
        assert oc.ok
        assert "not present" in oc.detail

    def test_bak_backup_is_also_enforced(self, tmp_path, monkeypatch):
        # openclaw.json.bak is a copy of the same token file (safe_write_bot_config
        # makes it), so it carries the same exposure and must converge to 0600.
        assert "openclaw.json.bak" in scp.BOT_SECRET_CONFIG_RELPATHS
        oc_dir = tmp_path / ".openclaw"
        oc_dir.mkdir(parents=True)
        bak = oc_dir / "openclaw.json.bak"
        bak.write_text("{}")
        os.chmod(bak, 0o644)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        checks = scp.check_bot_secret_modes("team-bot-a")
        bak_check = [c for c in checks if c.target.endswith("openclaw.json.bak")]
        assert bak_check and not bak_check[0].ok
        assert bak_check[0].apply is not None
