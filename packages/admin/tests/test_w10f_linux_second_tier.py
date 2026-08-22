"""test_w10f_linux_second_tier.py — the W10-F Linux fresh-install fixes.

W10-F was the SECOND tier of Linux fresh-install gaps found on a live
DigitalOcean Ubuntu VPS after W10-E merged (diag:
docs/diag-w10f-second-tier-residuals-2026-06-19.md). These are fast,
host-mutation-free locks for the high-value ones — the live e2e
(tests/e2e_linux/test_ubuntu_e2e.py) exercises the same paths against real
systemd/setfacl on the CI Linux runner, but these run in the normal suite on
any host by pinning the LINUX profile + injecting fakes.

Coverage:
  #1  the evolve→bot-home traverse ACL (grant_traverse seam + the
      set_evolve_read_acl wiring + the Linux sudoers grant)
  #3  pod_health/_check_launchd routes through the Scheduler seam on Linux
      (systemd units) instead of stat-ing /Library/LaunchDaemons plists
  #4  NODE_EXTRA_CA_CERTS platform-keyed via PlatformProfile.ca_bundle
  #5  the mcp-bridge enabled-default matches between install and runtime
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import deploy, health, setup_wizard  # noqa: E402
from evolve_admin.runtime import (  # noqa: E402
    FakePerms,
    LinuxPerms,
    MacOSPerms,
    set_perms,
    set_scheduler,
)


# ── shared fakes ──────────────────────────────────────────────────────────────


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


def _recording_runner():
    calls: "list[list[str]]" = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    return _run, calls


@pytest.fixture(autouse=True)
def _restore_seams():
    yield
    set_profile(None)
    set_perms(None)
    set_scheduler(None)


# ════════════════════════════════════════════════════════════════════════════
# #1 — the evolve→bot-home traverse ACL
# ════════════════════════════════════════════════════════════════════════════


def test_grant_traverse_linux_emits_execute_only_setfacl():
    """LinuxPerms.grant_traverse is exactly `setfacl -m u:<user>:--x <path>`
    — execute-only, no read/list of the home dir's other dotfiles, matching
    the live-verified hotfix argv. No default ACL, no recursion."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    assert perms.grant_traverse(Path("/home/examplebot"), "evolve")
    assert calls == [
        ["sudo", "/usr/bin/setfacl", "-m", "u:evolve:--x", "/home/examplebot"],
    ]


def test_grant_traverse_macos_is_a_noop_no_chmod():
    """macOS /Users/<acct> is already mode 0755 (others r-x), so traverse is a
    pure no-op — emitting a `chmod +a` here would move the macOS golden and
    need a new sudoers grant for zero benefit (byte-identity invariant)."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    assert perms.grant_traverse(Path("/Users/examplebot"), "evolve") is True
    assert calls == []


def test_set_evolve_read_acl_grants_home_traverse(tmp_path, monkeypatch):
    """The product path: set_evolve_read_acl must grant evolve traverse on the
    bot's HOME dir (not just the .openclaw read ACL). This is the bug that
    killed 14 evolve daemons on the live Linux pod — the rX ACL on .openclaw
    is unreachable without `x` on the 0750 parent."""
    home = tmp_path / "examplebot"
    (home / ".openclaw").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "examplebot")
    monkeypatch.setattr(deploy, "_user_home", lambda user: home)
    # Keep deploy's own residual subprocess calls (mkdir/chmod 700) inert.
    monkeypatch.setattr(deploy.subprocess, "run",
                        lambda *a, **k: _Result())

    fake = FakePerms()
    set_perms(fake)
    deploy.set_evolve_read_acl("examplebot")

    traverse_calls = [c for c in fake.calls if c[0] == "grant_traverse"]
    assert traverse_calls == [("grant_traverse", str(home), deploy.EVOLVE_SERVICE_USER)]
    # ...and it lands BEFORE the .openclaw read grant (the path must be
    # reachable before the recursive backfill runs).
    op_order = [c[0] for c in fake.calls]
    assert op_order.index("grant_traverse") < op_order.index("grant_read_recursive")


def test_linux_sudoers_grants_home_traverse_macos_does_not(monkeypatch):
    """The setfacl traverse grant is rendered on Linux and ABSENT on macOS
    (setfacl has no macOS analog; the grant must not leak into the mac file)."""
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path",
                        lambda: "/usr/lib/node_modules/openclaw/bin/openclaw")
    set_profile(LINUX)
    linux_render = setup_wizard._render_evolve_sudoers()
    assert r"/usr/bin/setfacl -m u\:evolve\:--x /home/*" in linux_render

    monkeypatch.setattr(setup_wizard, "_find_openclaw_path",
                        lambda: "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw")
    set_profile(MACOS)
    macos_render = setup_wizard._render_evolve_sudoers()
    assert "setfacl" not in macos_render
    assert "u\\:evolve\\:--x" not in macos_render


# ════════════════════════════════════════════════════════════════════════════
# #3 — pod_health inspects systemd units on Linux, not LaunchDaemon plists
# ════════════════════════════════════════════════════════════════════════════


class _FakeScheduler:
    """Minimal Scheduler stub: every label reports a fixed install/managed
    state, the shape SystemdScheduler.status() returns."""

    def __init__(self, *, installed: bool, managed: bool = True, running: bool = True):
        self._installed = installed
        self._managed = managed
        self._running = running

    def status(self, label, *, timeout=None):
        return {
            "installed": self._installed,
            "managed": self._managed if self._installed else False,
            "running": self._running if self._installed else False,
            "pid": 4242 if (self._installed and self._running) else None,
            "last_exit": None,
            "status_error": None,
        }


_LABELS = {"ai.evolve.evolve.admin-ui", "ai.openclaw.darwin-gateway"}


def _run_check_launchd(monkeypatch, *, installed: bool):
    monkeypatch.setattr(health, "expected_plist_labels",
                        lambda net, realized_only=False: set(_LABELS))
    set_profile(LINUX)
    set_scheduler(_FakeScheduler(installed=installed))
    report = health.HealthReport()
    health._check_launchd(report, {"members": ["darwin"], "sharedDir": "/var/lib/evolve"})
    return report


def test_check_launchd_linux_installed_units_no_false_missing(monkeypatch):
    """Units present as systemd services → NO "Plist not found", NO
    "0/N — not deployed". This is the 73-false-failure flood the live pod hit
    because the check stat'd /Library/LaunchDaemons unconditionally."""
    report = _run_check_launchd(monkeypatch, installed=True)
    msgs = [c.detail for c in report.checks]
    assert not any("Plist not found" in m for m in msgs), msgs
    assert not any("not deployed" in m for m in msgs), msgs
    # The summary records all expected units present-and-loaded.
    assert any(c.status == health.PASS and "launchd:all" in c.name
               for c in report.checks), msgs


def test_check_launchd_linux_missing_units_say_systemd_not_plist(monkeypatch):
    """When a unit really is absent on Linux, the FAIL wording is systemd
    ("systemd unit not found: /etc/systemd/system/...service"), never the
    macOS "Plist not found: /Library/LaunchDaemons/...plist"."""
    report = _run_check_launchd(monkeypatch, installed=False)
    fails = [c.detail for c in report.checks if c.status == health.FAIL]
    assert fails, "expected FAILs when no units are installed"
    assert any("systemd unit not found" in m and "/etc/systemd/system/" in m
               for m in fails), fails
    assert not any("Plist not found" in m or "/Library/LaunchDaemons" in m
                   for m in fails), fails


# ════════════════════════════════════════════════════════════════════════════
# #2 — gateway.mode seed-gate (gateway binds first-try, no crash-loop restart)
# ════════════════════════════════════════════════════════════════════════════


def test_ensure_gateway_mode_seeded_fills_missing_mode(tmp_path, monkeypatch):
    """When the bot's openclaw.json lacks gateway.mode (the onboard-written
    pre-ensure_plugin_config state), the seed-gate gap-fills mode=local +
    bind=loopback BEFORE the gateway starts — so it binds on the first try
    instead of crash-looping "existing config is missing gateway.mode"."""
    import json as _json

    oc = tmp_path / "examplebot" / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "openclaw.json").write_text(_json.dumps({"agent": "x"}))
    monkeypatch.setattr(deploy, "_user_home", lambda user: tmp_path / "examplebot")

    captured: dict = {}

    def _fake_write(bot_id, cfg, reason="", bot_user=None):
        captured["cfg"] = cfg
        captured["reason"] = reason
        return True, ""

    monkeypatch.setattr(deploy, "safe_write_bot_config", _fake_write)
    deploy._ensure_gateway_mode_seeded("examplebot", "examplebot")

    assert captured["cfg"]["gateway"]["mode"] == "local"
    assert captured["cfg"]["gateway"]["bind"] == "loopback"


def test_ensure_gateway_mode_seeded_noop_when_present(tmp_path, monkeypatch):
    """No write (and no validate subprocess) when gateway.mode is already set —
    the steady state and the macOS path, where config is seeded before the
    gateway installs, are untouched."""
    import json as _json

    oc = tmp_path / "examplebot" / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "openclaw.json").write_text(
        _json.dumps({"gateway": {"mode": "local", "bind": "loopback"}}))
    monkeypatch.setattr(deploy, "_user_home", lambda user: tmp_path / "examplebot")

    called = {"n": 0}

    def _fake_write(*a, **k):
        called["n"] += 1
        return True, ""

    monkeypatch.setattr(deploy, "safe_write_bot_config", _fake_write)
    deploy._ensure_gateway_mode_seeded("examplebot", "examplebot")
    assert called["n"] == 0, "seed-gate wrote even though gateway.mode was present"


# ════════════════════════════════════════════════════════════════════════════
# #6 — enable-https verification retries first-cert provisioning
# ════════════════════════════════════════════════════════════════════════════


def _patch_clock(monkeypatch, https_setup):
    """No-op sleep + a monotonic that jumps a whole retry-interval each tick,
    so the deadline is reached in a handful of iterations without real sleeps."""
    import time as _t

    state = {"now": 0.0}
    monkeypatch.setattr(_t, "sleep",
                        lambda s: state.__setitem__("now", state["now"] + s))
    monkeypatch.setattr(_t, "monotonic", lambda: state["now"])


def test_https_verify_retries_then_succeeds_on_cold_cert(monkeypatch):
    """A fresh pod's first HTTPS GET raises (cert still provisioning); the
    verifier must RETRY, not fail on the first timeout, and succeed once the
    listener answers 200."""
    from unittest.mock import MagicMock, patch

    from evolve_admin import https_setup

    _patch_clock(monkeypatch, https_setup)
    ok = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    seq = [ConnectionError("cert not ready"), ConnectionError("cert not ready"), ok]

    def _get(*a, **k):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch("requests.get", side_effect=_get) as g:
        https_setup._verify_https_reachable("https://pod.ts.net")  # must NOT raise
    assert g.call_count == 3, "verifier did not retry the cold-cert timeouts"


def test_https_verify_gives_up_at_deadline(monkeypatch):
    """When the endpoint never answers, the verifier raises VerificationFailed
    after the deadline (not an unbounded loop)."""
    from unittest.mock import patch

    from evolve_admin import https_setup

    _patch_clock(monkeypatch, https_setup)
    with patch("requests.get", side_effect=TimeoutError("read timed out")):
        with pytest.raises(https_setup.VerificationFailed):
            https_setup._verify_https_reachable("https://pod.ts.net")


def test_https_verify_received_response_is_not_retried(monkeypatch):
    """A received HTTP response (even non-200) is a real verdict — it must
    break the loop immediately, never trigger the retry/backoff."""
    from unittest.mock import MagicMock, patch

    from evolve_admin import https_setup

    _patch_clock(monkeypatch, https_setup)
    bad = MagicMock(status_code=502, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=bad) as g:
        with pytest.raises(https_setup.VerificationFailed):
            https_setup._verify_https_reachable("https://pod.ts.net")
    assert g.call_count == 1, "a received HTTP response must not be retried"


# ════════════════════════════════════════════════════════════════════════════
# #4 — NODE_EXTRA_CA_CERTS / ca_bundle
# ════════════════════════════════════════════════════════════════════════════


def test_ca_bundle_profile_values():
    """The macOS unified bundle vs Debian/Ubuntu's ca-certificates path."""
    assert MACOS.ca_bundle == "/etc/ssl/cert.pem"
    assert LINUX.ca_bundle == "/etc/ssl/certs/ca-certificates.crt"


# ════════════════════════════════════════════════════════════════════════════
# #5 — mcp-bridge enabled default consistency (install side ↔ runtime side)
# ════════════════════════════════════════════════════════════════════════════


def test_mcp_bridge_runtime_enabled_default_is_true_matching_install():
    """deploy installs the bridge when mcp_bridge is absent/not-explicitly-
    disabled (`_mcp_cfg.get("enabled", True)`); the daemon entrypoint must use
    the SAME default, else a fresh pod (no mcp_bridge section) installs a unit
    that exit(1)s on every start and flaps and nothing binds :5051. Assert the
    actual source literal in BOTH sites so a revert to default-False (the
    pre-W10-F bug) fails this lock."""
    runtime_src = (
        _ADMIN_DIR / "evolve_admin" / "mcp_bridge" / "__main__.py"
    ).read_text()
    assert 'mcp_cfg.get("enabled", True)' in runtime_src, (
        "mcp-bridge runtime default must be True (W10-F #5) to match the "
        "install-side default; default-False reintroduces the exit-1 flap"
    )
    # Disabling must exit cleanly (0), not as a failure (1) that systemd flaps.
    assert "sys.exit(0)" in runtime_src

    deploy_src = (_ADMIN_DIR / "evolve_admin" / "deploy.py").read_text()
    assert '_mcp_cfg.get("enabled", True)' in deploy_src, (
        "install-side mcp-bridge gate default drifted from True"
    )


# ════════════════════════════════════════════════════════════════════════════
# Second-tier residuals (round-3, 2026-06-19) — A/B/C/D/E
# ════════════════════════════════════════════════════════════════════════════

import ast  # noqa: E402

_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"

# Daemon entrypoint scripts installed as launchd/systemd units. Each takes a
# --shared-dir / --network arg; the round-3 bug was their argparse DEFAULTS
# carrying a macOS /Users/Shared literal that won and broke a Linux pod even
# though deploy.py passed the right value (breakers-runner read the wrong
# network.json default; apply.darwin resolved the wrong shared dir). #A.
_DAEMON_ENTRYPOINTS = [
    _ANALYZER_DIR / "breakers" / "runner.py",
    _ANALYZER_DIR / "capability_gap_monitor.py",
    _ANALYZER_DIR / "measure.py",
    _ANALYZER_DIR / "weekly_bot_trends.py",
    _ANALYZER_DIR / "usage_logger.py",
    _ANALYZER_DIR / "signal_subscriber_runner.py",
    _ANALYZER_DIR / "manifest_reflex_runner.py",
    # apply.py dropped 2026-08-18 — the daemon was retired
    # (docs/design-proposal-signing-key-2026-08-18.md).
    # Same-shape siblings swept in the same pass (pass-B follow-up) — their
    # --shared-dir defaults were also macOS literals (lower-risk: they override
    # from network.json's sharedDir, but the literal was the last-resort
    # fallback). Migrated + locked here so the guard covers every installed
    # analyzer daemon entrypoint, not just the round-3 repro set.
    _ANALYZER_DIR / "analyze.py",
    _ANALYZER_DIR / "outcome.py",
    _ANALYZER_DIR / "extract_tuples.py",
    _ANALYZER_DIR / "slack_signals.py",
    _ANALYZER_DIR / "expansion.py",
]


def _argparse_default_nodes(src: str):
    """Yield (file-context, unparsed-default) for every add_argument(default=…)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            for kw in node.keywords:
                if kw.arg == "default":
                    yield ast.unparse(kw.value)


# ── #A: no daemon argparse default carries a /Users/Shared literal ──────────


@pytest.mark.parametrize("entry", _DAEMON_ENTRYPOINTS, ids=lambda p: p.name)
def test_daemon_argparse_defaults_are_platform_keyed(entry):
    """No installed-daemon entrypoint may DEFAULT --shared-dir/--network to a
    macOS /Users/Shared literal — defaults must derive from the platform
    profile (CANONICAL_SHARED_DIR / resolve_network_path). Round-3 #8c/#9: the
    literal defaults broke breakers-runner + apply on a Linux pod. AST-based so
    a re-hardcode anywhere in any add_argument fails the lock."""
    src = entry.read_text()
    offenders = [d for d in _argparse_default_nodes(src) if "/Users/Shared" in d]
    assert not offenders, (
        f"{entry.name} has argparse default(s) with a /Users/Shared literal: "
        f"{offenders} — derive from platform_profile / evolve_config instead"
    )


# test_apply_resolve_shared_dir_prefers_network_over_literal removed
# 2026-08-18 — apply.py was retired, so there is no _resolve_shared_dir
# left to pin. The class it guarded (a macOS literal beating the
# platform-keyed sharedDir) is still locked for every surviving daemon
# entrypoint by test_daemon_argparse_defaults_are_platform_keyed above.


def test_puller_job_spec_linux_has_no_users_path():
    """The repo-puller unit body, rendered under the LINUX profile, must carry
    no /Users leak (program args, log paths, env). Round-3: the daemon ran
    `evolve-admin repo-pull` whose --repo default was the macOS literal."""
    from evolve_admin import repo_puller

    set_profile(LINUX)
    spec = repo_puller._puller_job_spec()
    blob = repr(spec.program_args) + spec.stdout_path + spec.stderr_path + repr(spec.env)
    assert "/Users" not in blob, f"puller unit body leaks /Users on Linux: {blob}"


def test_repo_pull_cli_repo_default_is_not_a_users_literal():
    """`evolve-admin repo-pull` (what the daemon invokes) must NOT default
    --repo to the macOS /Users/Shared/evolve-repo literal — it resolves from
    the platform profile (repo_puller.DEFAULT_REPO) inside the command."""
    cli_src = (_ADMIN_DIR / "evolve_admin" / "cli.py").read_text()
    assert 'default="/Users/Shared/evolve-repo"' not in cli_src
    assert "str(_rp.DEFAULT_REPO)" in cli_src


# ── #B: bot auth-profiles land under the platform home, with the key ────────


def test_write_bot_files_lands_auth_profiles_under_platform_home_with_key(
    tmp_path, monkeypatch
):
    """HEADLINE round-3 bug B: the wizard wrote the bot's real API key to a
    hardcoded /Users/<bot> while the bot's agent looked under its real $HOME
    (/home/<bot> on Linux). _write_bot_files must resolve home via user_home()
    so auth-profiles.json lands at the bot's real home AND carries the key."""
    from evolve_admin import wizard as _wiz

    set_profile(LINUX)
    # Resolve home the Linux way without needing the account to exist.
    monkeypatch.setattr(_wiz, "user_home", lambda n: Path(f"/home/{n}"))

    written: "dict[str, str]" = {}

    def _fake_run(cmd, **kwargs):
        # Capture the staged content of every `cp tmp dst` before it's unlinked.
        if len(cmd) >= 4 and cmd[1] == "cp":
            tmp_src, dst = cmd[2], cmd[3]
            try:
                written[dst] = Path(tmp_src).read_text()
            except OSError:
                pass
        return _Result()

    monkeypatch.setattr(_wiz.subprocess, "run", _fake_run)

    auth = _wiz._new_bot_auth_profiles("anthropic", "api_key", "sk-ant-SECRET123")
    errs = _wiz._write_bot_files("darwin", {"gateway": {}}, auth, "soul", "agents")
    assert errs == []

    auth_dsts = [d for d in written if d.endswith("auth-profiles.json")]
    assert auth_dsts, "auth-profiles.json was never written"
    dst = auth_dsts[0]
    assert dst.startswith("/home/darwin/"), f"auth-profiles landed at {dst}, not /home"
    assert "/Users/" not in dst
    assert "sk-ant-SECRET123" in written[dst], "the key did not reach the bot's home"


def test_new_bot_openclaw_config_workspace_is_platform_keyed(monkeypatch):
    """The new-bot openclaw.json `workspace` field must point at the bot's real
    home — on Linux /home/<bot>, not the /Users literal (the agent reads it)."""
    from evolve_admin import wizard as _wiz

    set_profile(LINUX)
    monkeypatch.setattr(_wiz, "user_home", lambda n: Path(f"/home/{n}"))
    cfg = _wiz._new_bot_openclaw_config("darwin", "anthropic", 19010)
    ws = cfg["agents"]["defaults"]["workspace"]
    assert ws == "/home/darwin/.openclaw/workspace"
    assert "/Users/" not in ws


def test_find_existing_keys_scans_platform_home_root():
    """_find_existing_keys (and _next_available_port) must walk the platform
    home root (/home on Linux) — round-3 bug B fed off a stale /Users/darwin
    leftover because the scan was hardcoded to /Users."""
    src = (_ADMIN_DIR / "evolve_admin" / "wizard.py").read_text()
    assert 'Path("/Users").iterdir()' not in src
    assert "get_profile().user_home_root" in src


# ── #C: the measure daemon is installed via the seam (both platforms) ───────


def test_install_launchd_measure_routes_via_seam():
    """`ai.openclaw.evolve.measure` is in expected_plist_labels but was only
    ever installed by the macOS-only `migrate-jobs` static-plist copy — so a
    Linux pod's health flagged "systemd unit not found …measure.service".
    _install_launchd_measure must route through the seam (→ plist on macOS,
    systemd unit on Linux) with the platform-keyed --shared-dir."""
    captured: "dict" = {}

    def _fake_install_launchd(**kwargs):
        captured.update(kwargs)

    import types

    orig = deploy._install_launchd
    deploy._install_launchd = _fake_install_launchd  # type: ignore[assignment]
    try:
        deploy._install_launchd_measure(types.SimpleNamespace(log=lambda *_: None))
    finally:
        deploy._install_launchd = orig  # type: ignore[assignment]

    assert captured["label"] == "ai.openclaw.evolve.measure"
    assert captured["user"] == "evolve"
    assert captured["script_path"].name == "measure.py"
    # --shared-dir is the platform-keyed canonical constant (the macOS-pinned
    # test sees /Users/Shared; a Linux pod sees /var/lib) — assert it IS that
    # constant, not a divergent hardcode.
    args = captured["extra_args"]
    assert args[args.index("--shared-dir") + 1] == str(deploy._CANONICAL_SHARED_DIR)


def test_install_evolve_infra_jobs_calls_measure():
    """The fresh-install path (not just the legacy migrate-jobs) installs the
    measure daemon — lock the call so a Linux pod gets the unit."""
    deploy_src = (_ADMIN_DIR / "evolve_admin" / "deploy.py").read_text()
    assert "_install_launchd_measure(result)" in deploy_src


# ── #D / #E: health checks the platform-keyed primary OC account ────────────


def test_oc_host_account_is_platform_keyed():
    """On macOS the gateway account IS `evolve`; on Linux the primary is `evo`
    and `evolve` is service-only (no openclaw.json + no gateway port). Health
    must check the account that actually has the instance, else it FAILs
    "Cannot read /home/evolve/.openclaw/openclaw.json" and WARNs "No port
    configured for evolve" (round-3 #D/#E)."""
    set_profile(MACOS)
    assert health._oc_host_account() == "evolve"
    set_profile(LINUX)
    assert health._oc_host_account() == "evo"


# ════════════════════════════════════════════════════════════════════════════
# User-account legibility — the "User Accounts" section's vocabulary tracks the
# host (no "macOS user 'X'" / "macOS User Accounts" on a Linux pod). Cosmetic:
# the check DETAIL wording (_check_users) AND the section LABEL the CLI report
# and the /api/pod-health JSON both render (category_labels, single source).
# ════════════════════════════════════════════════════════════════════════════


def _users_report(monkeypatch, profile, *, exists: bool, members=("darwin",)):
    set_profile(profile)
    monkeypatch.setattr(health, "_user_exists", lambda u: exists)
    monkeypatch.setattr(health, "get_bot_user", lambda bot_id, net: bot_id)
    report = health.HealthReport()
    health._check_users(report, list(members), {"members": list(members)})
    return report


def test_check_users_macos_wording_is_byte_identical(monkeypatch):
    """macOS keeps "macOS user 'X'" exactly — the byte-identity invariant the
    rest of W10-F holds to (the golden macOS report must not move)."""
    details = [c.detail for c in _users_report(monkeypatch, MACOS, exists=True).checks]
    assert "macOS user 'evolve' exists" in details
    assert "macOS user 'darwin' exists" in details


def test_check_users_linux_drops_the_macos_noun(monkeypatch):
    """A Linux pod's PASS detail reads "user account 'X' exists" — never the
    macOS noun (this is the cosmetic fix; behaviour is unchanged)."""
    details = [c.detail for c in _users_report(monkeypatch, LINUX, exists=True).checks]
    assert "user account 'evolve' exists" in details
    assert "user account 'darwin' exists" in details
    assert not any("macOS" in d for d in details), details


def test_check_users_linux_fail_wording_and_neutral_fix_cmds(monkeypatch):
    """FAIL details drop "macOS" on Linux too, and the fix_cmds stay the
    platform-neutral evolve-admin subcommands (no macOS-only hint)."""
    fails = [c for c in _users_report(monkeypatch, LINUX, exists=False).checks
             if c.status == health.FAIL]
    assert fails, "expected FAILs when no accounts exist"
    assert not any("macOS" in c.detail for c in fails), [c.detail for c in fails]
    assert any("user account 'evolve' not found" in c.detail for c in fails)
    assert any(c.fix_cmd == "sudo evolve-admin setup evolve-user" for c in fails)
    assert any("evolve-admin setup --fresh" in (c.fix_cmd or "") for c in fails)


def test_category_labels_are_platform_keyed():
    """The shared label map: macOS keeps its golden wording, Linux gets neutral
    /systemd vocabulary, platform-agnostic categories match across both."""
    set_profile(MACOS)
    mac = health.category_labels()
    assert mac["users"] == "macOS User Accounts"
    assert mac["launchd"] == "LaunchD Services"
    set_profile(LINUX)
    lin = health.category_labels()
    assert lin["users"] == "User Accounts"
    assert lin["launchd"] == "systemd Services"
    assert mac["network"] == lin["network"] == "Network Config"


def test_as_dict_carries_platform_category_labels():
    """The /api/pod-health JSON surfaces the platform-aware labels so the admin
    SPA renders Linux vocabulary instead of its own hardcoded macOS map (the
    drift that left the browser showing "macOS User Accounts" on a Linux pod)."""
    set_profile(LINUX)
    d = health.HealthReport().as_dict()
    assert d["category_labels"]["users"] == "User Accounts"
    assert d["category_labels"]["launchd"] == "systemd Services"


# ════════════════════════════════════════════════════════════════════════════
# Second-tier residuals (round-4, 2026-06-19) — #11 home-ownership, #12 /Users
# bot-home writers (workspace/evolve + exec-approvals.preview)
# ════════════════════════════════════════════════════════════════════════════


# ── #11 (HEADLINE): create_user owns the account's HOME to the account ───────


def test_linux_create_user_chowns_preexisting_root_home(tmp_path, monkeypatch):
    """`useradd -m` sets ownership only on a home it CREATES. If /home/<user>
    already exists root-owned (an earlier root-context `mkdir -p
    /home/<user>/.openclaw` made it before useradd), it stays root:root and the
    account can't write its own $HOME — npm cache (.npm), dotfiles, and the
    brave (externalized) plugin gap-fill install all EACCES (live round-4:
    /home/darwin was drwxr-xr-x root root). create_user must chown the home,
    NON-recursively (never clobbering the .openclaw subtree)."""
    import runtime.isolation as iso_mod

    home = tmp_path / "botacct"
    home.mkdir()  # the pre-existing (root-owned, in prod) home

    class _PW:
        pw_dir = str(home)

    monkeypatch.setattr(iso_mod.pwd, "getpwnam", lambda u: _PW())
    run, calls = _recording_runner()
    iso_mod.LinuxUserIsolation(runner=run).create_user("botacct", 1005)

    assert ["sudo", "/usr/bin/chown", "botacct", str(home)] in calls, calls
    chowns = [c for c in calls if c[:2] == ["sudo", "/usr/bin/chown"]]
    assert chowns and all("-R" not in c for c in chowns), (
        "the home chown must be non-recursive (the .openclaw subtree keeps its "
        f"own ownership/ACLs): {chowns}"
    )


def test_linux_create_user_no_home_skips_chown(monkeypatch):
    """The `-M` service-account path (create_home=False) has no home to own —
    no chown is emitted (and pwd is never consulted)."""
    import runtime.isolation as iso_mod

    def _boom(_u):
        raise AssertionError("pwd.getpwnam must not be called when create_home=False")

    monkeypatch.setattr(iso_mod.pwd, "getpwnam", _boom)
    run, calls = _recording_runner()
    iso_mod.LinuxUserIsolation(runner=run).create_user(
        "svc", 999, create_home=False)
    assert not any(c[:2] == ["sudo", "/usr/bin/chown"] for c in calls), calls


def test_linux_sudoers_grants_home_chown_macos_does_not(monkeypatch):
    """The home-dir chown grant is rendered on Linux (`/usr/bin/chown * /home/*`)
    and ABSENT on macOS — createhomedir owns the home there, so the macOS golden
    must not move (byte-identity invariant)."""
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path",
                        lambda: "/usr/lib/node_modules/openclaw/bin/openclaw")
    set_profile(LINUX)
    linux_render = setup_wizard._render_evolve_sudoers()
    assert "NOPASSWD: /usr/bin/chown * /home/*\n" in linux_render

    monkeypatch.setattr(setup_wizard, "_find_openclaw_path",
                        lambda: "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw")
    set_profile(MACOS)
    macos_render = setup_wizard._render_evolve_sudoers()
    # The exact home-dir chown grant (not the longer .openclaw forms) must not
    # appear on macOS — the trailing newline pins it to the bare-home pattern.
    assert "NOPASSWD: /usr/sbin/chown * /Users/*\n" not in macos_render


# ── #12: bot-home writers (reconciler + audit dispatch) never touch /Users ──


def test_reconciler_bot_home_is_platform_keyed():
    """The reconciler's prod preview-write + live-state read paths resolve via
    the pwd-first account home (/home/<bot> on Linux), not a /Users literal —
    it ran in deploy_bot and left a root-owned
    /Users/<bot>/.openclaw/exec-approvals.preview.json on a fresh Linux pod."""
    from evolve_admin.app_permissions import reconciler

    set_profile(LINUX)
    home = reconciler._acct_home("examplebot")
    assert str(home) == "/home/examplebot", home
    assert "/Users" not in str(home)


def test_audit_dispatch_bot_home_paths_are_platform_keyed():
    """The audit-inbox / investigation-trail / outbox builders resolve under the
    bot's real home — the inbox mkdir left a root-owned
    /Users/<bot>/.openclaw/workspace/evolve/ tree on a fresh Linux pod (round-4
    #12, the workspace/evolve half of the same /Users leak)."""
    from evolve_admin.applications import audit_dispatch

    set_profile(LINUX)
    inbox = audit_dispatch._audit_inbox_dir("examplebot")
    trail = audit_dispatch._investigations_trail_path("examplebot")
    outbox = audit_dispatch._investigations_outbox_dir("examplebot")
    for p in (inbox, trail, outbox):
        assert str(p).startswith("/home/examplebot/.openclaw/workspace/evolve/"), p
        assert "/Users" not in str(p)
    assert str(inbox).endswith("/audit_inbox")
