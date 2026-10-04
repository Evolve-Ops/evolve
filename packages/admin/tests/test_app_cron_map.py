"""AL-1.2 Lane A deploy side — the app-cron-map writer.

``merge_app_cron_map`` runs at the ``_merge_cron_entries`` call sites in
deploy.py and records ``{cron name: app_id}`` into
``{shared}/{bot}/app-cron-map.json`` so the plugin can join a firing cron's
session (`` cron:<job.id> `` → name via the bot's jobs.json) to its app.
Covers: merge semantics (additive, other apps' entries preserved), the 0644
mode pin (mkstemp+rename would otherwise land 0600 and lock the bot-user
reader out), corrupt/missing-map tolerance, junk-entry filtering, and the
best-effort never-raise contract.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.app_cron_map import (  # noqa: E402
    APP_CRON_MAP_FILENAME,
    merge_app_cron_map,
    read_app_cron_map,
    remove_app_cron_map_entries,
)


class _Result:
    def __init__(self):
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(msg)


def _map_path(shared: Path) -> Path:
    return shared / "team_bot_a" / APP_CRON_MAP_FILENAME


def test_merge_creates_map_with_0644_and_entries(tmp_path):
    res = _Result()
    ok = merge_app_cron_map(
        tmp_path, "team_bot_a", {"daily-digest": "morning-briefing"}, res
    )
    assert ok is True
    p = _map_path(tmp_path)
    assert json.loads(p.read_text()) == {"daily-digest": "morning-briefing"}
    # Mode pin: the plugin reads this file as the BOT user — 0600 (what a bare
    # mkstemp+rename mints) would silently kill Lane A on a real pod.
    assert stat.S_IMODE(p.stat().st_mode) == 0o644
    assert any("app-cron-map" in line for line in res.lines)


def test_merge_is_additive_across_apps_and_refreshes_own_names(tmp_path):
    merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a"})
    merge_app_cron_map(tmp_path, "team_bot_a", {"cron-b": "app-b"})
    assert read_app_cron_map(tmp_path, "team_bot_a") == {
        "cron-a": "app-a",
        "cron-b": "app-b",
    }
    # A re-deploy that remaps its own cron name wins for that name only.
    merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a-v2"})
    assert read_app_cron_map(tmp_path, "team_bot_a") == {
        "cron-a": "app-a-v2",
        "cron-b": "app-b",
    }


def test_merge_noop_when_nothing_changes(tmp_path):
    merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a"})
    before = _map_path(tmp_path).stat().st_mtime_ns
    assert merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a"}) is True
    assert _map_path(tmp_path).stat().st_mtime_ns == before
    # Empty / junk-only entries are a success no-op that creates nothing.
    assert merge_app_cron_map(tmp_path, "fresh_bot", {}) is True
    assert merge_app_cron_map(tmp_path, "fresh_bot", {"": "x", "  ": "y", "n": ""}) is True
    assert not (tmp_path / "fresh_bot").exists()


def test_corrupt_existing_map_is_replaced_not_fatal(tmp_path):
    (tmp_path / "team_bot_a").mkdir(parents=True)
    _map_path(tmp_path).write_text("{corrupt")
    assert merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a"}) is True
    assert read_app_cron_map(tmp_path, "team_bot_a") == {"cron-a": "app-a"}


def test_read_missing_or_nondict_map_returns_empty(tmp_path):
    assert read_app_cron_map(tmp_path, "team_bot_a") == {}
    (tmp_path / "team_bot_a").mkdir(parents=True)
    _map_path(tmp_path).write_text("[1, 2]")
    assert read_app_cron_map(tmp_path, "team_bot_a") == {}


def test_write_failure_warns_but_never_raises(tmp_path):
    # shared_dir/bot collides with an existing FILE → mkdir fails.
    (tmp_path / "team_bot_a").write_text("not a dir")
    res = _Result()
    ok = merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a"}, res)
    assert ok is False
    assert any("WARNING" in line for line in res.lines)


def test_remove_entries_for_cleanup(tmp_path):
    merge_app_cron_map(tmp_path, "team_bot_a", {"cron-a": "app-a", "cron-b": "app-b"})
    assert remove_app_cron_map_entries(tmp_path, "team_bot_a", ["cron-a", "ghost"]) is True
    assert read_app_cron_map(tmp_path, "team_bot_a") == {"cron-b": "app-b"}
    # Removing from a missing map is success (idempotent teardown).
    assert remove_app_cron_map_entries(tmp_path, "no_such_bot", ["x"]) is True


def test_install_evolve_app_call_site_writes_the_map(tmp_path):
    """Drive the REAL deploy path (install_evolve_app → _merge_cron_entries
    call site) against the real security-cve-scan manifest and assert the
    cron name → app_id row lands in the primary bot's app-cron-map.json.
    Same harness shape (and same sudo_dest_refusal caveat) as
    test_evolve_app_cron_delivery.py::test_real_cve_scan_manifest_installs_
    no_delivery_cron."""
    from evolve_admin import deploy

    net = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}

    def _fake_run_sudo(cmd, result, check=True):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_cat(cmd, *a, **k):
        path = cmd[-1] if isinstance(cmd, (list, tuple)) else ""
        if str(path).endswith("openclaw.json"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"tools": {}}), "")
        return subprocess.CompletedProcess(cmd, 1, "", "absent")

    from platform_profile import LINUX, get_profile, set_profile
    prev = get_profile()
    set_profile(LINUX)
    try:
        with patch.object(deploy, "load_network", return_value=net), \
             patch.object(deploy, "sudo_dest_refusal", return_value=""), \
             patch.object(deploy, "_run_sudo", _fake_run_sudo), \
             patch.object(deploy.subprocess, "run", _fake_cat), \
             patch("evolve_admin.secret_config_perms.chmod_secret_config", lambda p: True):
            result = deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)
    finally:
        set_profile(prev)

    assert result.success, result.errors
    assert read_app_cron_map(tmp_path, "evo") == {
        "security:cve-scan-discover": "security-cve-scan",
    }
