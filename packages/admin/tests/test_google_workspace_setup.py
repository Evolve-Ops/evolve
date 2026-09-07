"""tests/test_google_workspace_setup.py — unit tests for google_workspace_setup.

Tests mock subprocess so no real gcloud is required.  The file write /
permission path (install_sa_key, write_network_config) uses tmp_path-scoped
directories so nothing escapes into the real filesystem.

Coverage:
  - check_gcloud: gcloud not found, not authed, no project, happy path
  - ensure_sa: existing SA, freshly created SA
  - enable_apis: already enabled, newly enabled, failure
  - create_sa_key: happy path, gcloud failure
  - install_sa_key: happy path (file write + mode), bad secret_ref, missing key_path
  - write_network_config: happy path, load failure, save failure, bad args
  - run_setup: happy path end-to-end, failure at each major step
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from evolve_admin import google_workspace_setup as gws


# ── Subprocess mock helpers ────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a minimal CompletedProcess-like mock."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _proc_json(data: Any, returncode: int = 0) -> MagicMock:
    return _proc(returncode=returncode, stdout=json.dumps(data))


# ── check_gcloud ──────────────────────────────────────────────────────────────


def test_check_gcloud_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = gws.check_gcloud(project="my-proj")
    assert not result.ok
    assert "not found" in result.error.lower() or "gcloud" in result.error


def test_check_gcloud_version_fails(monkeypatch, tmp_path):
    """gcloud binary present but 'gcloud version' returns non-zero."""
    fake_bin = tmp_path / "gcloud"
    fake_bin.write_text("#!/bin/sh\nexit 0")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _: str(fake_bin))
    with patch("subprocess.run", return_value=_proc(returncode=1, stderr="version error")):
        result = gws.check_gcloud(project="my-proj")
    assert not result.ok
    assert "version" in result.error.lower() or "gcloud" in result.error


def test_check_gcloud_not_authed(monkeypatch, tmp_path):
    """gcloud version ok but no active account in auth list."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    version_resp = _proc_json({"Google Cloud SDK": "450.0.0"})
    auth_resp = _proc_json([])  # no accounts

    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            return version_resp
        return auth_resp
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.check_gcloud(project="my-proj")
    assert not result.ok
    assert "auth" in result.error.lower() or "authenticated" in result.error.lower()


def test_check_gcloud_no_project(monkeypatch, tmp_path):
    """Authed but no project in active config and none passed."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    responses = [
        _proc_json({"Google Cloud SDK": "450.0.0"}),
        _proc_json([{"account": "user@example.com", "status": "ACTIVE"}]),
        _proc_json(""),  # config get-value project → empty string
    ]
    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        return responses[n] if n < len(responses) else _proc(returncode=1)
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.check_gcloud(project="")
    assert not result.ok
    assert "project" in result.error.lower()


def test_check_gcloud_happy_path(monkeypatch):
    """All checks pass — project supplied by caller."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    responses = [
        _proc_json({"Google Cloud SDK": "450.0.0"}),
        _proc_json([{"account": "user@example.com", "status": "ACTIVE"}]),
    ]
    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        return responses[n] if n < len(responses) else _proc()
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.check_gcloud(project="my-proj")
    assert result.ok
    assert result.account == "user@example.com"
    assert result.project == "my-proj"
    assert "450" in result.version


# ── ensure_sa ─────────────────────────────────────────────────────────────────


def test_ensure_sa_already_exists(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    sa_data = {"email": "evolve-google-integration@proj.iam.gserviceaccount.com", "oauth2ClientId": "123456"}
    with patch("subprocess.run", return_value=_proc_json(sa_data)):
        result = gws.ensure_sa(project="proj", sa_name="evolve-google-integration")
    assert result.ok
    assert not result.created
    assert result.client_id == "123456"
    assert "evolve-google-integration" in result.sa_email


def test_ensure_sa_creates_new(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    sa_data = {"email": "evolve-google-integration@proj.iam.gserviceaccount.com", "oauth2ClientId": "789"}
    responses = [
        _proc(returncode=1, stderr="NOT_FOUND"),  # describe → not found
        _proc_json({}),                            # create → ok
        _proc_json(sa_data),                       # describe after create
    ]
    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        return responses[n] if n < len(responses) else _proc()
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.ensure_sa(project="proj")
    assert result.ok
    assert result.created
    assert result.client_id == "789"


def test_ensure_sa_create_fails(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    responses = [
        _proc(returncode=1, stderr="NOT_FOUND"),         # describe
        _proc(returncode=1, stderr="quota exceeded"),    # create fails
    ]
    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        return responses[n] if n < len(responses) else _proc()
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.ensure_sa(project="proj")
    assert not result.ok
    assert "quota" in result.error.lower() or "Failed" in result.error


# ── enable_apis ───────────────────────────────────────────────────────────────


def test_enable_apis_all_already_enabled(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    items = [
        {"config": {"name": "gmail.googleapis.com"}},
        {"config": {"name": "calendar-json.googleapis.com"}},
        {"config": {"name": "drive.googleapis.com"}},
    ]
    with patch("subprocess.run", return_value=_proc_json(items)):
        result = gws.enable_apis(project="proj")
    assert result.ok
    assert result.enabled == []
    assert set(result.already_enabled) == set(gws.DEFAULT_APIS)


def test_enable_apis_some_new(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    items = [{"config": {"name": "gmail.googleapis.com"}}]  # only gmail pre-enabled
    responses = [_proc_json(items), _proc_json({})]  # list, enable
    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        return responses[n] if n < len(responses) else _proc()
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.enable_apis(project="proj")
    assert result.ok
    assert "gmail.googleapis.com" in result.already_enabled
    assert "calendar-json.googleapis.com" in result.enabled
    assert "drive.googleapis.com" in result.enabled


def test_enable_apis_enable_fails(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    responses = [_proc_json([]), _proc(returncode=1, stderr="permission denied")]
    call_count = [0]
    def fake_run(cmd, **kw):
        n = call_count[0]
        call_count[0] += 1
        return responses[n] if n < len(responses) else _proc()
    with patch("subprocess.run", side_effect=fake_run):
        result = gws.enable_apis(project="proj")
    assert not result.ok
    assert "permission" in result.error.lower() or "Failed" in result.error


def test_enable_apis_empty_list():
    """Passing an empty api tuple is a no-op that always succeeds."""
    result = gws.enable_apis(project="proj", apis=())
    assert result.ok
    assert result.enabled == []
    assert result.already_enabled == []


# ── create_sa_key ─────────────────────────────────────────────────────────────


def test_create_sa_key_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    key_json = {"private_key_id": "key-abc", "private_key": "-----BEGIN RSA..."}

    def fake_run(cmd, **kw):
        # The command writes to a temp file passed as the first non-flag arg.
        # Simulate gcloud writing the key JSON to that path.
        for i, a in enumerate(cmd):
            if a.endswith(".json") and "evolve-sa-key-" in a:
                Path(a).write_text(json.dumps(key_json))
                break
        return _proc_json({})

    with patch("subprocess.run", side_effect=fake_run):
        result = gws.create_sa_key(project="proj", key_dir=tmp_path)

    assert result.ok
    assert result.key_id == "key-abc"
    assert result.key_path is not None
    assert result.key_path.exists()
    # Mode must be 0600.
    assert (os.stat(result.key_path).st_mode & 0o777) == 0o600


def test_create_sa_key_gcloud_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    with patch("subprocess.run", return_value=_proc(returncode=1, stderr="UNAUTHENTICATED")):
        result = gws.create_sa_key(project="proj", key_dir=tmp_path)
    assert not result.ok
    assert "UNAUTHENTICATED" in result.error or "Failed" in result.error
    # Temp file must be cleaned up.
    remaining = list(tmp_path.glob("evolve-sa-key-*.json"))
    assert remaining == [], f"temp file not cleaned up: {remaining}"


# ── install_sa_key ────────────────────────────────────────────────────────────


def test_install_sa_key_happy_path(tmp_path):
    secrets_dir = tmp_path / "secrets"
    key_src = tmp_path / "sa-key.json"
    key_data = {"private_key_id": "abc", "private_key": "-----"}
    key_src.write_text(json.dumps(key_data))
    key_src.chmod(0o600)

    result = gws.install_sa_key(
        key_path=key_src,
        secret_ref="google-sa-example-corp",
        secrets_dir=secrets_dir,
    )

    assert result.ok
    assert result.secret_ref == "google-sa-example-corp"
    dest = secrets_dir / "google-sa-example-corp.json"
    assert result.dest == dest
    assert dest.exists()
    # File contents preserved.
    assert json.loads(dest.read_text()) == key_data
    # Mode must be 0600.
    assert (os.stat(dest).st_mode & 0o777) == 0o600
    # Source deleted after install.
    assert not key_src.exists()


def test_install_sa_key_secrets_dir_created(tmp_path):
    """install_sa_key creates secrets_dir if it doesn't exist."""
    nested = tmp_path / "a" / "b" / "secrets"
    key_src = tmp_path / "k.json"
    key_src.write_text('{"x":1}')

    result = gws.install_sa_key(
        key_path=key_src,
        secret_ref="my-ref",
        secrets_dir=nested,
    )
    assert result.ok
    assert nested.exists()
    assert (nested / "my-ref.json").exists()


def test_install_sa_key_missing_source(tmp_path):
    result = gws.install_sa_key(
        key_path=tmp_path / "nonexistent.json",
        secret_ref="ref",
        secrets_dir=tmp_path / "secrets",
    )
    assert not result.ok
    assert "does not exist" in result.error


def test_install_sa_key_bad_secret_ref(tmp_path):
    key_src = tmp_path / "k.json"
    key_src.write_text("{}")
    result = gws.install_sa_key(key_path=key_src, secret_ref="bad/ref", secrets_dir=tmp_path)
    assert not result.ok
    assert "slash" in result.error.lower() or "slashes" in result.error.lower()


def test_install_sa_key_empty_secret_ref(tmp_path):
    key_src = tmp_path / "k.json"
    key_src.write_text("{}")
    result = gws.install_sa_key(key_path=key_src, secret_ref="", secrets_dir=tmp_path)
    assert not result.ok


# ── write_network_config ──────────────────────────────────────────────────────


def test_write_network_config_happy_path(tmp_path):
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({"bots": {"lex": {"name": "Lex"}}}))

    result = gws.write_network_config(
        bot_id="lex",
        secret_ref="google-sa-corp",
        subject="lex@corp.com",
        workspace_domain="corp.com",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        network_path=net_path,
    )

    assert result["ok"] is True
    net = json.loads(net_path.read_text())
    gi = net["bots"]["lex"]["google_integration"]
    assert gi["mode"] == "service_account_dwd"
    assert gi["subject"] == "lex@corp.com"
    assert gi["workspace_domain"] == "corp.com"
    assert gi["service_account_secret_ref"] == "google-sa-corp"
    assert "https://www.googleapis.com/auth/gmail.readonly" in gi["scopes"]


def test_write_network_config_creates_bot_entry(tmp_path):
    """write_network_config creates the bot entry if it doesn't exist yet."""
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({}))

    result = gws.write_network_config(
        bot_id="newbot",
        secret_ref="google-sa-corp",
        subject="newbot@corp.com",
        workspace_domain="corp.com",
        scopes=["https://www.googleapis.com/auth/calendar"],
        network_path=net_path,
    )
    assert result["ok"] is True
    net = json.loads(net_path.read_text())
    assert "newbot" in net["bots"]


def test_write_network_config_invalid_json(tmp_path):
    """Returns ok=False when network.json contains invalid JSON."""
    bad_path = tmp_path / "network.json"
    bad_path.write_text("not valid json {{{")
    result = gws.write_network_config(
        bot_id="lex",
        secret_ref="ref",
        subject="lex@corp.com",
        workspace_domain="corp.com",
        scopes=["scope"],
        network_path=bad_path,
    )
    assert result["ok"] is False
    assert "network.json" in result["error"] or "load" in result["error"]


def test_write_network_config_raises_on_bad_args(tmp_path):
    net_path = tmp_path / "network.json"
    net_path.write_text("{}")
    with pytest.raises(gws.SetupError, match="bot_id"):
        gws.write_network_config(
            bot_id="",
            secret_ref="ref",
            subject="s@corp.com",
            workspace_domain="corp.com",
            scopes=["scope"],
            network_path=net_path,
        )
    with pytest.raises(gws.SetupError, match="scopes"):
        gws.write_network_config(
            bot_id="lex",
            secret_ref="ref",
            subject="s@corp.com",
            workspace_domain="corp.com",
            scopes=[],
            network_path=net_path,
        )


# ── run_setup (end-to-end orchestration) ─────────────────────────────────────


def _mock_gcloud_check_ok():
    return gws.GcloudCheck(ok=True, version="450", account="admin@corp.com", project="my-proj")


def _mock_ensure_sa_ok():
    return gws.EnsureSaResult(ok=True, sa_email="evolve-google-integration@my-proj.iam.gserviceaccount.com", created=True, client_id="9876543210")


def _mock_enable_apis_ok():
    return gws.EnableApisResult(ok=True, enabled=list(gws.DEFAULT_APIS))


def _make_key_file(tmp_path: Path) -> Path:
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"private_key_id": "k1", "private_key": "---"}))
    p.chmod(0o600)
    return p


def _mock_create_key_ok(key_path: Path):
    return gws.CreateKeyResult(ok=True, key_path=key_path, key_id="k1")


def test_run_setup_happy_path(monkeypatch, tmp_path):
    secrets_dir = tmp_path / "secrets"
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({"bots": {"lex": {}}}))
    key_file = _make_key_file(tmp_path)

    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: _mock_ensure_sa_ok())
    monkeypatch.setattr(gws, "enable_apis", lambda **kw: _mock_enable_apis_ok())
    monkeypatch.setattr(gws, "create_sa_key", lambda **kw: _mock_create_key_ok(key_file))
    monkeypatch.setattr(gws, "install_sa_key", lambda **kw: gws.InstallKeyResult(ok=True, dest=secrets_dir / "google-sa-corp.json", secret_ref="google-sa-corp"))

    result = gws.run_setup(
        project="my-proj",
        bot_id="lex",
        subject="lex@corp.com",
        workspace_domain="corp.com",
        secrets_dir=secrets_dir,
        network_path=net_path,
    )

    assert result.ok
    assert result.sa_email == "evolve-google-integration@my-proj.iam.gserviceaccount.com"
    assert result.secret_ref == "google-sa-corp"
    assert result.network_updated
    assert result.manual_steps  # non-empty Workspace Admin instructions
    # network.json updated
    net = json.loads(net_path.read_text())
    gi = net["bots"]["lex"]["google_integration"]
    assert gi["mode"] == "service_account_dwd"


def test_run_setup_fails_on_gcloud_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: gws.GcloudCheck(ok=False, error="not authenticated"))
    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@corp.com", workspace_domain="corp.com",
        network_path=tmp_path / "net.json",
    )
    assert not result.ok
    assert "not authenticated" in result.error


def test_run_setup_fails_on_ensure_sa(monkeypatch, tmp_path):
    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: gws.EnsureSaResult(ok=False, error="quota exceeded"))
    net_path = tmp_path / "network.json"
    net_path.write_text("{}")
    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@corp.com", workspace_domain="corp.com",
        network_path=net_path,
    )
    assert not result.ok
    assert "quota" in result.error


def test_run_setup_fails_on_enable_apis(monkeypatch, tmp_path):
    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: _mock_ensure_sa_ok())
    monkeypatch.setattr(gws, "enable_apis", lambda **kw: gws.EnableApisResult(ok=False, error="billing not enabled"))
    net_path = tmp_path / "network.json"
    net_path.write_text("{}")
    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@corp.com", workspace_domain="corp.com",
        network_path=net_path,
    )
    assert not result.ok
    assert "billing" in result.error


def test_run_setup_fails_on_create_key(monkeypatch, tmp_path):
    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: _mock_ensure_sa_ok())
    monkeypatch.setattr(gws, "enable_apis", lambda **kw: _mock_enable_apis_ok())
    monkeypatch.setattr(gws, "create_sa_key", lambda **kw: gws.CreateKeyResult(ok=False, error="key limit reached"))
    net_path = tmp_path / "network.json"
    net_path.write_text("{}")
    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@corp.com", workspace_domain="corp.com",
        network_path=net_path,
    )
    assert not result.ok
    assert "key limit" in result.error


def test_run_setup_fails_on_install_key(monkeypatch, tmp_path):
    key_file = _make_key_file(tmp_path)
    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: _mock_ensure_sa_ok())
    monkeypatch.setattr(gws, "enable_apis", lambda **kw: _mock_enable_apis_ok())
    monkeypatch.setattr(gws, "create_sa_key", lambda **kw: _mock_create_key_ok(key_file))
    monkeypatch.setattr(gws, "install_sa_key", lambda **kw: gws.InstallKeyResult(ok=False, error="permission denied"))
    net_path = tmp_path / "network.json"
    net_path.write_text("{}")
    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@corp.com", workspace_domain="corp.com",
        network_path=net_path,
    )
    assert not result.ok
    assert "permission" in result.error


def test_run_setup_default_secret_ref_derived_from_domain(monkeypatch, tmp_path):
    """When secret_ref is empty, it's derived from workspace_domain."""
    key_file = _make_key_file(tmp_path)
    captured: dict[str, Any] = {}

    def fake_install(**kw):
        captured["secret_ref"] = kw["secret_ref"]
        return gws.InstallKeyResult(ok=True, dest=tmp_path / "x.json", secret_ref=kw["secret_ref"])

    net_path = tmp_path / "network.json"
    net_path.write_text('{"bots": {"lex": {}}}')

    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: _mock_ensure_sa_ok())
    monkeypatch.setattr(gws, "enable_apis", lambda **kw: _mock_enable_apis_ok())
    monkeypatch.setattr(gws, "create_sa_key", lambda **kw: _mock_create_key_ok(key_file))
    monkeypatch.setattr(gws, "install_sa_key", fake_install)

    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@example-corp.com", workspace_domain="example-corp.com",
        secret_ref="",
        network_path=net_path,
    )
    assert result.ok
    assert captured["secret_ref"] == "google-sa-example-corp"


def test_run_setup_manual_steps_contain_scope_csv(monkeypatch, tmp_path):
    """The manual-steps list must include the scope CSV for Admin Console."""
    key_file = _make_key_file(tmp_path)
    net_path = tmp_path / "network.json"
    net_path.write_text('{"bots": {"lex": {}}}')

    monkeypatch.setattr(gws, "check_gcloud", lambda **kw: _mock_gcloud_check_ok())
    monkeypatch.setattr(gws, "ensure_sa", lambda **kw: _mock_ensure_sa_ok())
    monkeypatch.setattr(gws, "enable_apis", lambda **kw: _mock_enable_apis_ok())
    monkeypatch.setattr(gws, "create_sa_key", lambda **kw: _mock_create_key_ok(key_file))
    monkeypatch.setattr(gws, "install_sa_key", lambda **kw: gws.InstallKeyResult(ok=True, dest=tmp_path / "x.json", secret_ref="google-sa-corp"))

    result = gws.run_setup(
        project="my-proj", bot_id="lex",
        subject="lex@corp.com", workspace_domain="corp.com",
        network_path=net_path,
    )
    assert result.ok
    all_manual = " ".join(result.manual_steps)
    # Every default scope must appear in the manual steps for copy-paste.
    for scope in gws.DEFAULT_SCOPES:
        assert scope in all_manual, f"scope {scope!r} missing from manual steps"
    # Client ID must appear.
    assert result.sa_client_id in all_manual
