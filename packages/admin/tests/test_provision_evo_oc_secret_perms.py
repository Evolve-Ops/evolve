"""tests/test_provision_evo_oc_secret_perms.py — gateway secret-config 0600.

`_provision_evo_oc` writes two token-bearing files to the gateway account's
``.openclaw/`` via ``sudo /bin/cp`` from a /tmp staging file:

  - ``openclaw.json``                                   (gateway auth token)
  - ``agents/main/agent/auth-profiles.json``            (provider API keys)

A bare ``cp`` (no ``-p``) lands a *fresh* dest at 0600, but on a wizard
RE-RUN over an existing 0644 dest it cp-PRESERVES 0644 — a world-readable
leak on a multi-user box. Both writes must therefore call
``secret_config_perms.chmod_secret_config`` (the single-source-of-truth helper
deploy.py's writers use) to enforce 0600 after the copy.

These tests pin that contract: drive ``_provision_evo_oc`` with every host-
touching call mocked, capture the subprocess argv across BOTH setup_wizard and
secret_config_perms (a global ``subprocess.run`` patch sees both), and assert a
``sudo /bin/chmod 600 <path>`` lands for each of the two secret files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def macos_profile():
    """Pin the macOS profile so Step H takes the launchd branch deterministically
    on a Linux CI runner. Restores autodetection afterwards."""
    import platform_profile
    platform_profile.set_profile(platform_profile.MACOS)
    try:
        yield
    finally:
        platform_profile.set_profile(None)


def _run_provision(tmp_path: Path):
    """Drive ``_provision_evo_oc`` with all host-touching calls mocked.

    Returns ``(ok, subprocess_calls)`` where subprocess_calls is the list of
    argv lists captured across every patched ``subprocess.run`` (setup_wizard
    AND secret_config_perms — a global patch on ``subprocess.run`` catches both,
    since both modules resolve ``subprocess.run`` off the shared stdlib module).
    """
    from evolve_admin import setup_wizard

    home = tmp_path / "Users" / "evolve"
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        # Model the FILESYSTEM EFFECT of the two root commands the flow depends
        # on, not just their argv. ``chmod_secret_config`` now gates on
        # ``evolve_util.assert_safe_sudo_dest`` (#3566 audit D-2), which lstats
        # the destination and its parent and fails closed when it cannot verify
        # them — so a fake that records ``sudo /bin/mkdir -p`` and ``sudo /bin/cp``
        # without creating anything leaves the wizard chmod'ing a path that does
        # not exist, which real root never does (the mkdir and cp ran first).
        if len(argv) > 2 and str(argv[1]).endswith("mkdir"):
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        elif len(argv) > 2 and str(argv[1]).endswith("cp"):
            dest = Path(argv[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = Path(argv[-2])
            dest.write_text(src.read_text() if src.is_file() else "")
        # text= callers read .stdout; return "" so the Step-H openclaw.json
        # read parses to {} (the function warns but continues).
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    sched = MagicMock()
    sched.status.return_value = {"managed": False}
    sched.raw.return_value = (0, "", "")
    sched.restart.return_value = (True, "")

    auth_profiles = {
        "version": 1,
        "profiles": {"anthropic": {"provider": "anthropic"}},
        "lastGood": {},
    }

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(setup_wizard, "user_home", lambda acct: home), \
         patch.object(setup_wizard, "_create_bot_account", return_value=True), \
         patch.object(setup_wizard, "_provision_evo_account", return_value=True), \
         patch.object(setup_wizard, "_log_admin_action"), \
         patch.object(setup_wizard, "_select_api_keys_for_evolve",
                      return_value=auth_profiles), \
         patch.object(setup_wizard, "_embedding_chain_for_credentials",
                      return_value=["openai"]), \
         patch.object(setup_wizard, "_evolve_openclaw_config",
                      return_value={"gateway": {"port": 19030,
                                                "auth": {"token": "tok"}}}), \
         patch.object(setup_wizard, "_evolve_gateway_jobspec",
                      return_value=MagicMock()), \
         patch.object(setup_wizard, "render_launchd_plist", return_value="plist"), \
         patch.object(setup_wizard, "get_launchd_scheduler", return_value=sched):
        ok = setup_wizard._provision_evo_oc(
            "testpod", tmp_path / "shared", "pod-admin", [], True,
            telegram_token="dummy", bot_id="evo", gateway_account="evolve",
        )
    return ok, home, calls


def test_chmod_600_enforced_for_openclaw_json(tmp_path, macos_profile):
    """openclaw.json (gateway auth token) gets a ``chmod 600`` after the cp."""
    ok, home, calls = _run_provision(tmp_path)
    assert ok is True
    oc_path = str(home / ".openclaw" / "openclaw.json")
    assert ["sudo", "/bin/chmod", "600", oc_path] in calls, (
        f"openclaw.json must be chmod 600'd after the sudo cp (a wizard re-run "
        f"over an existing 0644 dest would otherwise leave the gateway token "
        f"world-readable). chmod calls seen: "
        f"{[c for c in calls if 'chmod' in c[1:]]}"
    )


def test_chmod_600_enforced_for_auth_profiles_json(tmp_path, macos_profile):
    """auth-profiles.json (provider API keys) gets a ``chmod 600`` after the cp."""
    ok, home, calls = _run_provision(tmp_path)
    assert ok is True
    auth_path = str(
        home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    )
    assert ["sudo", "/bin/chmod", "600", auth_path] in calls, (
        f"auth-profiles.json must be chmod 600'd after the sudo cp (provider "
        f"API keys must never be world-readable). chmod calls seen: "
        f"{[c for c in calls if 'chmod' in c[1:]]}"
    )


def test_both_secret_files_in_relpaths_contract():
    """Both files the wizard writes are declared in the secret-config-perms
    single source of truth, so the deploy-time self-heal (check_bot_secret_modes
    via ensure_pod_perms) converges them too — defense in depth behind the
    wizard's inline chmod."""
    from evolve_admin.secret_config_perms import BOT_SECRET_CONFIG_RELPATHS

    assert "openclaw.json" in BOT_SECRET_CONFIG_RELPATHS
    assert "agents/main/agent/auth-profiles.json" in BOT_SECRET_CONFIG_RELPATHS
