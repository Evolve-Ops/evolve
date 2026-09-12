"""Bot-private secret-file carve-out (#3452).

The recursive ``.openclaw`` evolve-read grant sweeps up bot-private app token
files (pm-inbox's ``pm-inbox-github-tokens.json``). On Linux the planted ACE
makes the stat group triad display the ACL MASK — a 0600 file reads as 640 —
and strict app-side ``mode == 0600`` gates refuse to run after every heal
(observed twice live on the reference VPS pod, 2026-07-28/29).

The fix is the file-shaped twin of the credentials/ + profiles carve-out:
``strip_bot_private_acl`` clears the ACE + restores 0600, and every recursive
grant path (deploy contract, drift-apply, heal) runs it afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import secret_config_perms as scp  # noqa: E402
from evolve_admin.runtime import FakePerms, set_perms  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_perms():
    """set_perms is a PROCESS-GLOBAL seam (same convention as
    test_deploy_perms_seam.py) — restore it after every test. Without this,
    the FakePerms installed by _with_fake_perms leaked into whatever test ran
    next in the same shard: set_evolve_read_acl's ACL grants silently went to
    the fake instead of subprocess, order-dependently failing
    test_deploy_manifests_acl.py::test_manifests_acl_grants_evolve_write
    (no chmod +a recorded) whenever this file preceded it."""
    yield
    set_perms(None)


def _with_fake_perms():
    fake = FakePerms()
    set_perms(fake)
    return fake


def test_registries_are_disjoint():
    """BOT_PRIVATE (evolve must NOT read) and BOT_SECRET_CONFIG (evolve MUST
    read via ACL) are opposite contracts — one relpath in both would make the
    heal fight itself."""
    overlap = set(scp.BOT_PRIVATE_SECRET_RELPATHS) & set(scp.BOT_SECRET_CONFIG_RELPATHS)
    assert overlap == set()


def test_strip_clears_acl_and_restores_0600(tmp_path, monkeypatch):
    fake = _with_fake_perms()
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    tokens = oc / scp.BOT_PRIVATE_SECRET_RELPATHS[0]
    tokens.write_text("{}")

    chmods: list[list[str]] = []

    def fake_run(cmd, **kw):
        chmods.append(cmd)

        class _P:
            returncode = 0
        return _P()

    monkeypatch.setattr(scp.subprocess, "run", fake_run)
    assert scp.strip_bot_private_acl(oc) is True
    assert str(tokens) in fake.cleared
    assert any(c[:3] == ["sudo", "/bin/chmod", "600"] and c[3] == str(tokens)
               for c in chmods)


def test_strip_skips_missing_files(tmp_path, monkeypatch):
    fake = _with_fake_perms()
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    monkeypatch.setattr(
        scp.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no chmod expected")),
    )
    assert scp.strip_bot_private_acl(oc) is True
    assert fake.cleared == []


def test_drift_apply_regrant_runs_the_carveout(tmp_path, monkeypatch):
    """_reassert_evolve_read_acl (the ensure-pod-perms drift apply) re-grants
    recursively — the exact path that re-planted the ACE on the live pod. It
    must strip the bot-private files afterwards."""
    fake = _with_fake_perms()
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    tokens = oc / scp.BOT_PRIVATE_SECRET_RELPATHS[0]
    tokens.write_text("{}")

    def fake_run(cmd, **kw):
        class _P:
            returncode = 0
        return _P()

    monkeypatch.setattr(scp.subprocess, "run", fake_run)
    assert scp._reassert_evolve_read_acl(oc) is True
    grant_calls = [c for c in fake.calls if c[0] == "grant_read_recursive"]
    assert grant_calls, "re-grant should still run"
    assert str(tokens) in fake.cleared
    # Order: the strip must come AFTER the recursive grant.
    grant_idx = fake.calls.index(grant_calls[0])
    clear_idx = fake.calls.index(("clear_acl", str(tokens), False))
    assert clear_idx > grant_idx


def test_heal_evolve_access_runs_the_carveout(tmp_path, monkeypatch):
    fake = _with_fake_perms()
    home = tmp_path / "team_bot_a"
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    tokens = oc / scp.BOT_PRIVATE_SECRET_RELPATHS[0]
    tokens.write_text("{}")

    def fake_run(cmd, **kw):
        class _P:
            returncode = 0
        return _P()

    monkeypatch.setattr(scp.subprocess, "run", fake_run)
    monkeypatch.setattr(scp, "_user_home", lambda u: home)
    monkeypatch.setattr(scp, "verify_evolve_access", lambda bu, p=None: [])

    called: dict = {}

    def fake_set_acl(bot_id):
        called["set_acl"] = bot_id

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl", fake_set_acl)

    assert scp.heal_evolve_access("team_bot_a", "team_bot_a") is True
    assert called["set_acl"] == "team_bot_a"
    assert str(tokens) in fake.cleared
