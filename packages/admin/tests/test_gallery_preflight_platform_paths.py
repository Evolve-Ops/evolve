"""tests/test_gallery_preflight_platform_paths.py

Slate S3 (internal/audit-gallery-framework-2026-07-02.md §2): the gallery
preflight checkers used to hardcode ``/Users/{bot}`` bot-home paths, so on
a Linux pod ``_read_oc_json`` read a nonexistent path, returned ``{}``, and
every openclaw.json-backed integration/secret check reported missing —
``ready_to_run=false`` noise and false OAuth detours before live-testing
even started.

These tests pin the fix: preflight path derivation goes through the
platform-keyed ``bot_home()`` seam, so

  • on the Linux profile the checks read under ``/home/<bot>``,
  • on the macOS profile behavior is unchanged (``/Users/<bot>``),
  • an integration that IS configured on a Linux-home bot reports
    satisfied, and
  • an unreadable network.json degrades to profile-keyed construction
    (checks run; no crash), matching the old literal's failure mode.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))

import platform_profile  # noqa: E402


BOT = "s3bot"  # deliberately not a real account anywhere


@pytest.fixture
def shared_dir(tmp_path):
    return tmp_path / "shared"


@pytest.fixture
def no_real_accounts(monkeypatch):
    """Force pwd resolution to fail so bot_home falls back to the
    profile-keyed construction — deterministic on any dev box / CI runner."""
    import pwd as _pwd

    def _raise(name):
        raise KeyError(name)

    monkeypatch.setattr(_pwd, "getpwnam", _raise)


@pytest.fixture
def linux_profile():
    platform_profile.set_profile(platform_profile.LINUX)
    yield platform_profile.LINUX
    platform_profile.set_profile(None)


@pytest.fixture
def macos_profile():
    platform_profile.set_profile(platform_profile.MACOS)
    yield platform_profile.MACOS
    platform_profile.set_profile(None)


@pytest.fixture
def patched_oc(monkeypatch):
    """Serve openclaw.json content by bot name regardless of home root,
    recording every openclaw.json path the checkers actually tried."""
    holder: dict[str, dict] = {}
    requested: list[str] = []

    real_read_text = Path.read_text
    real_exists = Path.exists

    def fake_read_text(self, *args, **kwargs):
        s = str(self)
        if s.endswith("/.openclaw/openclaw.json"):
            requested.append(s)
            bot = s.split("/")[2]
            if bot in holder:
                return json.dumps(holder[bot])
            raise FileNotFoundError(s)
        return real_read_text(self, *args, **kwargs)

    def fake_exists(self):
        s = str(self)
        if s.endswith("/.openclaw/openclaw.json"):
            requested.append(s)
            bot = s.split("/")[2]
            return bot in holder
        if s.endswith("/.openclaw/skills/telegram.json"):
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(Path, "exists", fake_exists)
    return holder, requested


TELEGRAM_OC = {
    "channels": {
        "telegram": {
            "enabled": True,
            "botToken": "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        }
    }
}

PKG = {
    "pkg_id": "s3-test",
    "requirements": {
        "integrations": [
            {
                "id": "telegram",
                "display_name": "Telegram",
                "required": True,
                "check_path": "openclaw.json → channels.telegram",
            }
        ]
    },
}


def _run_preflight(shared_dir):
    from evolve_admin.applications import gallery

    with patch.object(gallery, "load_gallery_package", return_value=PKG):
        return gallery.preflight_check("s3-test", BOT, shared_dir)


def test_linux_profile_reads_home_paths(
    no_real_accounts, linux_profile, patched_oc, shared_dir
):
    """On the Linux profile a configured integration is satisfied — the
    checker must derive /home/<bot>/…, not the old /Users/<bot> literal."""
    holder, requested = patched_oc
    holder[BOT] = TELEGRAM_OC

    result = _run_preflight(shared_dir)

    reqs = result["requirements"]
    assert len(reqs) == 1
    assert reqs[0]["state"] == "satisfied", reqs[0]["message"]
    assert result["ready_to_run"] is True

    oc_paths = [p for p in requested if p.endswith("/openclaw.json")]
    assert oc_paths, "checker never derived an openclaw.json path"
    assert all(p.startswith(f"/home/{BOT}/") for p in oc_paths), oc_paths


def test_macos_profile_unchanged(
    no_real_accounts, macos_profile, patched_oc, shared_dir
):
    holder, requested = patched_oc
    holder[BOT] = TELEGRAM_OC

    result = _run_preflight(shared_dir)

    assert result["requirements"][0]["state"] == "satisfied"
    oc_paths = [p for p in requested if p.endswith("/openclaw.json")]
    assert oc_paths and all(p.startswith(f"/Users/{BOT}/") for p in oc_paths), (
        oc_paths
    )


def test_linux_missing_integration_still_missing(
    no_real_accounts, linux_profile, patched_oc, shared_dir
):
    """No config anywhere → missing, with the check still probing /home."""
    holder, requested = patched_oc  # holder empty

    result = _run_preflight(shared_dir)

    assert result["requirements"][0]["state"] == "missing"
    assert result["ready_to_run"] is False


def test_unreadable_network_degrades_not_crashes(
    no_real_accounts, linux_profile, patched_oc, shared_dir, monkeypatch
):
    """A corrupt network.json (load_network raises) must not 500 the
    preflight — the checker falls back to profile-keyed construction and
    the configured integration still resolves under /home."""
    import evolve_admin.config as admin_config

    def _boom(*a, **k):
        raise RuntimeError("network.json unreadable")

    monkeypatch.setattr(admin_config, "load_network", _boom)

    holder, requested = patched_oc
    holder[BOT] = TELEGRAM_OC

    result = _run_preflight(shared_dir)

    assert result["requirements"][0]["state"] == "satisfied", (
        result["requirements"][0]["message"]
    )
    oc_paths = [p for p in requested if p.endswith("/openclaw.json")]
    assert oc_paths and all(p.startswith(f"/home/{BOT}/") for p in oc_paths)
