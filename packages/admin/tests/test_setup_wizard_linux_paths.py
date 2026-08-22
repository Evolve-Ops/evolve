"""Linux home-base path routing in the setup wizard (W8 / Phase 8.3).

8.3 Linux port (docs/design-linux-port-2026-06-10.md §6 + the wizard-port
census docs/census-linux-wizard-remainder-2026-06-11.md). The deploy/perms/
venv waves (W7/W7b/W7c) ported the seams, but the wizard's OWN account
provisioning still hardcoded ``/Users/...`` homes, ``/Users/Shared/evolve``,
and ``/usr/sbin/chown root:wheel`` — a real ``setup --fresh`` on Ubuntu
failed even with the seam-level e2e harness green.

These tests pin the LINUX profile and assert the wizard's home/shared/venv
literals now resolve through ``platform_profile`` (``user_home`` /
``DEFAULT_SHARED_DIR`` / ``profile.chown`` / ``profile.admin_group``):
``/home`` homes, ``/var/lib`` shared dir + venv, and ``/usr/bin/chown
root:root`` — with no ``/Users/`` leaking into a Linux pod.

Companion to test_setup_wizard_network_paths.py (the Step-14 security block,
whose rulesFile retired with review.py 2026-08-14) and
test_oc_candidates.py (the macOS discovery scan). conftest's autouse
fixture restores the MACOS pin on teardown.

Note on scope: the ``DEFAULT_SHARED_DIR``-routed sites (``_load_existing_config``
network/install probes, ``_log_admin_action``) are intentionally NOT asserted
here. ``DEFAULT_SHARED_DIR`` is an import-time snapshot of the profile's shared
dir, so a ``set_profile(LINUX)`` pin on a macOS test box can't flip it — it is
``/var/lib/evolve`` on a real Linux host (where ``sys.platform`` resolves LINUX
at import) and ``/Users/Shared/evolve`` here. The dynamic ``user_home`` /
``profile.chown`` sites below ARE flippable and carry the Linux assertions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard, wizard  # noqa: E402


# ── _evolve_gateway_plist_content: HOME + log paths ──────────────────────────

# Same positional args the parity golden uses (label, node_bin, oc_index,
# gw_port). The trailing (token, version) are passed per-test.
_GW_ARGS = (
    "ai.openclaw.evolve-gateway",
    "/usr/bin/node",
    "/usr/lib/node_modules/openclaw/dist/index.js",
    "19030",
)


def test_gateway_plist_home_and_logs_follow_linux_home():
    """Under the Linux gate the evolve gateway's HOME and log paths land on
    ``/home/evolve`` (not ``/Users/evolve``). PATH/CA stay macOS-shaped for
    now — that Linux gateway-runtime port is a shared follow-up (it matches
    deploy.install_bot_gateway_plist's current state)."""
    import plistlib

    set_profile(LINUX)
    xml = setup_wizard._evolve_gateway_plist_content(
        *_GW_ARGS, "tok", "2026.6.1",
    )
    parsed = plistlib.loads(xml.encode())
    env = parsed["EnvironmentVariables"]
    assert env["HOME"] == "/home/evolve"
    out = parsed["StandardOutPath"]
    err = parsed["StandardErrorPath"]
    assert out == "/home/evolve/.openclaw/logs/gateway.log"
    assert err == "/home/evolve/.openclaw/logs/gateway.err.log"
    assert "/Users/" not in out and "/Users/" not in err and "/Users/" not in env["HOME"]


def test_gateway_plist_macos_home_unchanged():
    """macOS byte-identity guard mirrored here (the golden parity test owns
    the full XML; this is the home-base half)."""
    import plistlib

    set_profile(MACOS)
    xml = setup_wizard._evolve_gateway_plist_content(
        *_GW_ARGS, "tok", "2026.6.1",
    )
    parsed = plistlib.loads(xml.encode())
    assert parsed["EnvironmentVariables"]["HOME"] == "/Users/evolve"
    assert parsed["StandardOutPath"] == "/Users/evolve/.openclaw/logs/gateway.log"


# ── _evolve_openclaw_config: the agents.defaults.workspace literal ───────────


def test_evolve_openclaw_workspace_follows_linux_home():
    set_profile(LINUX)
    cfg = setup_wizard._evolve_openclaw_config(
        "podname", "/var/lib/evolve", "", auth_profiles_flat={}, bot_id="evo",
    )
    ws = cfg["agents"]["defaults"]["workspace"]
    assert ws == "/home/evolve/.openclaw/workspace"
    assert "/Users/" not in ws


def test_evolve_openclaw_workspace_macos_unchanged():
    set_profile(MACOS)
    cfg = setup_wizard._evolve_openclaw_config(
        "podname", "/Users/Shared/evolve", "", auth_profiles_flat={}, bot_id="evo",
    )
    assert cfg["agents"]["defaults"]["workspace"] == "/Users/evolve/.openclaw/workspace"


# ── _fix_venv_ownership: venv dir + admin-group chown ────────────────────────


def test_fix_venv_ownership_linux_dir_and_root_group(monkeypatch):
    """Linux venv lives at ``/var/lib/evolve-venv`` and the chown uses the
    Linux binary (``/usr/bin/chown``) + admin group (``root:root`` — ``wheel``
    is not gid 0 on Ubuntu)."""
    set_profile(LINUX)
    calls: list[list[str]] = []
    monkeypatch.setattr(setup_wizard.Path, "exists", lambda self: True)
    monkeypatch.setattr(
        setup_wizard.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd)),
    )

    setup_wizard._fix_venv_ownership()

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:4] == ["sudo", "/usr/bin/chown", "-R", "root:root"]
    assert cmd[-1] == "/var/lib/evolve-venv"


def test_fix_venv_ownership_macos_unchanged(monkeypatch):
    set_profile(MACOS)
    calls: list[list[str]] = []
    monkeypatch.setattr(setup_wizard.Path, "exists", lambda self: True)
    monkeypatch.setattr(
        setup_wizard.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd)),
    )

    setup_wizard._fix_venv_ownership()

    assert calls[0][:4] == ["sudo", "/usr/sbin/chown", "-R", "root:wheel"]
    assert calls[0][-1] == "/Users/Shared/evolve-venv"


# ── find_oc_candidates: the discovery scan root (Deliverable B) ──────────────


def _make_home_root(tmp_path: Path, base_name: str, layout: dict) -> Path:
    root = tmp_path / base_name
    root.mkdir()
    for uname, spec in layout.items():
        d = root / uname
        d.mkdir()
        if spec is None:
            continue
        oc_dir = d / ".openclaw"
        oc_dir.mkdir()
        (oc_dir / "openclaw.json").write_text(json.dumps({"gateway": {"port": spec["port"]}}))
    return root


# ── W8 PR2: evo day-one — gateway JobSpec + config account threading ─────────


def test_gateway_jobspec_linux_runs_as_evo():
    """Day-one Linux: the gateway JobSpec runs as `evo` with /home/evo paths
    and the Linux PATH / CA bundle — no /Users, no Homebrew, no macOS cert."""
    set_profile(LINUX)
    spec = setup_wizard._evolve_gateway_jobspec(*_GW_ARGS, "tok", "2026.6.1", "evo")
    assert spec.user == "evo"
    assert spec.env["HOME"] == "/home/evo"
    assert spec.stdout_path == "/home/evo/.openclaw/logs/gateway.log"
    assert spec.stderr_path == "/home/evo/.openclaw/logs/gateway.err.log"
    assert spec.env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert spec.env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/ca-certificates.crt"
    assert "/Users/" not in spec.env["HOME"]
    assert "homebrew" not in spec.env["PATH"]


def test_gateway_jobspec_macos_runs_as_evolve_unchanged():
    """macOS byte-identity: default account `evolve`, Homebrew PATH, the
    unified cert.pem (the golden parity test owns the full XML)."""
    set_profile(MACOS)
    spec = setup_wizard._evolve_gateway_jobspec(*_GW_ARGS, "tok", "2026.6.1")
    assert spec.user == "evolve"
    assert spec.env["HOME"] == "/Users/evolve"
    assert spec.stdout_path == "/Users/evolve/.openclaw/logs/gateway.log"
    assert spec.env["PATH"] == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    assert spec.env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/cert.pem"


def test_gateway_plist_content_wraps_jobspec_unchanged():
    """The XML wrapper still renders the default-evolve macOS plist
    byte-identically (it now delegates to _evolve_gateway_jobspec)."""
    import plistlib

    set_profile(MACOS)
    xml = setup_wizard._evolve_gateway_plist_content(*_GW_ARGS, "tok", "2026.6.1")
    parsed = plistlib.loads(xml.encode())
    assert parsed["UserName"] == "evolve"
    assert parsed["EnvironmentVariables"]["HOME"] == "/Users/evolve"


def test_evolve_openclaw_config_evo_account_workspace_on_linux():
    """gateway_account='evo' puts the workspace on the evo home (Linux)."""
    set_profile(LINUX)
    cfg = setup_wizard._evolve_openclaw_config(
        "pod", "/var/lib/evolve", "", auth_profiles_flat={},
        bot_id="evo", gateway_account="evo",
    )
    assert cfg["agents"]["defaults"]["workspace"] == "/home/evo/.openclaw/workspace"
    # The logical bot id still lands in the plugin config (account ≠ bot id).
    assert cfg["plugins"]["entries"]["evolve"]["config"]["botId"] == "evo"


def test_find_oc_candidates_scans_home_on_linux(tmp_path, monkeypatch):
    """On Linux the OC-install scan walks ``/home`` (not ``/Users``), so real
    Linux installs are discoverable. The launchd-only plist matcher returns
    {} there, so candidates carry no suggested_bot_id — which is fine."""
    set_profile(LINUX)
    home_root = _make_home_root(tmp_path, "home", {
        "alice_bot": {"port": 18800},
        "evo": {"port": 19030},
        "noopenclaw": None,
    })
    real_path = wizard.Path
    monkeypatch.setattr(
        wizard, "Path",
        lambda *a, **kw: (home_root if a == ("/home",) else real_path(*a, **kw)),
    )
    monkeypatch.setattr(wizard, "_gateway_plists_by_user", lambda: {})
    monkeypatch.setattr(wizard, "_looks_like_admin_account", lambda u: False)

    users = sorted(c.user for c in wizard.find_oc_candidates())
    assert users == ["alice_bot", "evo"]


def test_find_oc_candidates_ignores_users_on_linux(tmp_path, monkeypatch):
    """A stray ``/Users`` tree on a Linux box must NOT be scanned — discovery
    follows the profile home base, so phantom macOS-shaped users don't leak
    into a Linux pod's candidate list."""
    set_profile(LINUX)
    # Build BOTH a populated /Users-style tree and an empty /home; only /home
    # should be consulted.
    _make_home_root(tmp_path, "Users", {"macghost": {"port": 18999}})
    home_root = _make_home_root(tmp_path, "home", {})
    real_path = wizard.Path
    monkeypatch.setattr(
        wizard, "Path",
        lambda *a, **kw: (home_root if a == ("/home",) else real_path(*a, **kw)),
    )
    monkeypatch.setattr(wizard, "_gateway_plists_by_user", lambda: {})
    monkeypatch.setattr(wizard, "_looks_like_admin_account", lambda u: False)

    assert wizard.find_oc_candidates() == []


# ── W10-E fix 4: telegram token must MERGE, not replace the plugins block ─────


def test_evolve_openclaw_config_telegram_keeps_evolve_plugin_with_botid():
    """A primary telegram token must MERGE the telegram entry alongside the
    evolve plugin — not replace the whole plugins block. The old
    ``cfg["plugins"] = {...telegram...}`` wiped the evolve entry (and its
    required ``botId``), so the evo gateway's ``plugins install`` later wrote
    ``config={}`` and failed schema validation ("must have required property
    'botId'"). W10-E."""
    set_profile(LINUX)
    cfg = setup_wizard._evolve_openclaw_config(
        "pod", "/var/lib/evolve", "tg-token-123",
        auth_profiles_flat={}, bot_id="evo", gateway_account="evo",
    )
    entries = cfg["plugins"]["entries"]
    assert entries["evolve"]["config"]["botId"] == "evo"
    assert entries["evolve"]["config"]["role"] == "primary"
    assert entries["telegram"]["enabled"] is True


# ── W10-E fix 3: repo-root traversability preflight ──────────────────────────


def test_preflight_repo_root_macos_is_noop(monkeypatch):
    """macOS never runs the preflight — /Users/Shared/evolve-repo is always
    operator-readable. A bogus _REPO_ROOT must not raise."""
    from evolve_admin import deploy

    set_profile(MACOS)
    monkeypatch.setattr(deploy, "_REPO_ROOT", Path("/root/unreadable/repo"))
    # No raise.
    setup_wizard._preflight_repo_root_traversable()


def test_preflight_repo_root_linux_rejects_unreadable_source(monkeypatch):
    """Linux + evolve account exists: when evolve cannot read a deep source
    file (the /root mode-0710 case), HARD-FAIL with SystemExit."""
    import pytest as _pytest

    from evolve_admin import deploy

    set_profile(LINUX)
    monkeypatch.setattr(deploy, "_REPO_ROOT", Path("/root/evolve"))
    monkeypatch.setattr(setup_wizard, "_user_exists", lambda u: True)
    monkeypatch.setattr(
        setup_wizard.subprocess, "run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )
    with _pytest.raises(SystemExit):
        setup_wizard._preflight_repo_root_traversable()


def test_preflight_repo_root_linux_accepts_readable_source(monkeypatch):
    """Linux + evolve account exists + evolve CAN read the sentinel → no raise."""
    from evolve_admin import deploy

    set_profile(LINUX)
    monkeypatch.setattr(deploy, "_REPO_ROOT", Path("/var/lib/evolve/repo"))
    monkeypatch.setattr(setup_wizard, "_user_exists", lambda u: True)
    monkeypatch.setattr(
        setup_wizard.subprocess, "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    setup_wizard._preflight_repo_root_traversable()  # no raise


def test_preflight_repo_root_linux_fresh_install_scans_modes(tmp_path, monkeypatch):
    """Fresh install (evolve not created yet): fall back to a world-traverse
    (o+x) scan of every ancestor. A 0o700 ancestor blocks traversal → SystemExit."""
    import os
    import pytest as _pytest

    from evolve_admin import deploy

    blocked = tmp_path / "blocked"
    repo = blocked / "repo"
    repo.mkdir(parents=True)
    os.chmod(blocked, 0o700)  # no o+x — soon-to-be-created users can't traverse

    set_profile(LINUX)
    monkeypatch.setattr(deploy, "_REPO_ROOT", repo)
    monkeypatch.setattr(setup_wizard, "_user_exists", lambda u: False)
    try:
        with _pytest.raises(SystemExit):
            setup_wizard._preflight_repo_root_traversable()
    finally:
        os.chmod(blocked, 0o755)  # restore so pytest can clean up
