"""Golden parity tests — Phase 4.3 C Track S Step S0 plist consolidation.

Every fixture under ``fixtures/plist_golden/`` was captured from the
PRE-refactor hand-rolled XML emitters (with environment-dependent values
pinned). Each test calls the post-refactor emitter with the identical
arguments and asserts **parsed-dict equality**:

    plistlib.loads(new_xml) == plistlib.loads(golden_xml)

launchd only sees parsed keys/values, so this catches every
behavior-relevant difference (missing keys, wrong value types, KeepAlive
shape, run-as UserName) without forcing the single renderer to reproduce
six different whitespace styles. Spec:
docs/design-phase4.3c-S0-plist-consolidation.md.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from evolve_admin.runtime import JobSpec, render_launchd_plist

FIXTURES = Path(__file__).parent / "fixtures" / "plist_golden"

# Pinned environment values — must match what the capture run used.
PIN_PY = "/Users/Shared/evolve-venv/bin/python3"
PIN_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PIN_ANALYZER = Path("/Users/Shared/evolve-repo/packages/analyzer")


def _golden(name: str) -> dict:
    return plistlib.loads((FIXTURES / f"{name}.plist").read_bytes())


def _assert_parity(name: str, new_xml: str) -> None:
    new = plistlib.loads(new_xml.encode("utf-8"))
    old = _golden(name)
    assert new == old, (
        f"{name}: parsed plist diverged from pre-refactor golden.\n"
        f"only-in-new: { {k: new[k] for k in new.keys() - old.keys()} }\n"
        f"only-in-old: { {k: old[k] for k in old.keys() - new.keys()} }\n"
        f"changed: { {k: (old[k], new[k]) for k in old.keys() & new.keys() if old[k] != new[k]} }"
    )


# ── 1. service.generate_plist ────────────────────────────────────────────────


def test_service_admin_ui_parity():
    from evolve_admin import service
    # LOG_PATH is Path.home()-derived at import; pin it so the fixture is
    # machine-independent (and free of real account names — scrub guard).
    with patch.object(sys, "executable", PIN_PY), \
         patch.dict(os.environ, {"PATH": PIN_PATH}), \
         patch.object(service, "LOG_PATH",
                      Path("/Users/pod-admin/.evolve/logs/admin-server.log")):
        xml = service.generate_plist(host="127.0.0.1", port=5050)
    _assert_parity("service_admin_ui", xml)


# ── 2. deploy._plist_content — every schedule branch + jitter ────────────────

_DEPLOY_CASES = [
    ("deploy_interval",
     ("ai.evolve.evolve.heal", "evolve",
      Path("/Users/Shared/evolve-repo/packages/analyzer/heal.py"),
      {"interval": 900}),
     {}),
    ("deploy_interval_args_runatload",
     ("ai.evolve.scan-status.team_bot_a", "team_bot_a",
      Path("/Users/Shared/evolve-repo/packages/analyzer/scan_status.py"),
      {"interval": 3600}),
     {"extra_args": ["--shared-dir", "/Users/Shared/evolve"], "run_at_load": True}),
    ("deploy_jitter",
     ("ai.evolve.audit.team_bot_b", "team_bot_b",
      Path("/Users/Shared/evolve-repo/packages/analyzer/audit.py"),
      {"interval": 1800}),
     {"extra_args": ["--mode", "full sweep"], "jitter_seconds": 300}),
    ("deploy_weekday",
     ("ai.evolve.evolve.weekly-report", "evolve",
      Path("/Users/Shared/evolve-repo/packages/analyzer/report.py"),
      {"Weekday": 1, "Hour": 3, "Minute": 30}),
     {}),
    ("deploy_times",
     ("ai.evolve.evolve.pod-report", "evolve",
      Path("/Users/Shared/evolve-repo/packages/analyzer/pod_report.py"),
      {"times": [{"Hour": 8, "Minute": 0}, {"Hour": 20, "Minute": 30}]}),
     {}),
    ("deploy_hourly_minute",
     ("ai.evolve.evolve.hourly-tick", "evolve",
      Path("/Users/Shared/evolve-repo/packages/analyzer/tick.py"),
      {"Minute": 15}),
     {}),
    ("deploy_daily",
     ("ai.evolve.evolve.daily-audit", "evolve",
      Path("/Users/Shared/evolve-repo/packages/analyzer/audit.py"),
      {"Hour": 2, "Minute": 5}),
     {}),
]


@pytest.mark.parametrize("name,args,kwargs", _DEPLOY_CASES,
                         ids=[c[0] for c in _DEPLOY_CASES])
def test_deploy_plist_content_parity(name, args, kwargs):
    from evolve_admin import deploy
    # _job_spec_for's EVOLVE_NETWORK env now derives from _CANONICAL_NETWORK_JSON
    # (import-frozen; on a Linux CI runner it caches before the conftest macOS
    # pin — same hazard the imessage parity test guards). Pin the macOS value.
    with patch.object(deploy, "VENV_PYTHON", PIN_PY), \
         patch.object(deploy, "ANALYZER_DIR", PIN_ANALYZER), \
         patch.object(deploy, "_CANONICAL_NETWORK_JSON",
                      Path("/Users/Shared/evolve/network.json")):
        xml = deploy._plist_content(*args, **kwargs)
    _assert_parity(name, xml)


# ── 3. deploy.install_bot_gateway_plist ──────────────────────────────────────


def test_gateway_plist_parity():
    """Intercept the staged plist at the ``sudo /bin/cp`` step.

    The plist write rides the Scheduler seam now (4.3C S2), so the capture
    happens in an injected runner — no real subprocess for the cp/launchctl
    ritual. node_bin is pinned via shutil.which; Path.exists is forced False
    so oc_index deterministically falls back to the first candidate — same
    pins the golden capture used.
    """
    from evolve_admin import deploy
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler
    captured: dict[str, str] = {}

    def runner(argv):
        argv = [str(c) for c in argv]
        if argv[:2] == ["sudo", "/bin/cp"] and len(argv) == 4 \
                and argv[3].startswith("/Library/LaunchDaemons/"):
            captured["xml"] = Path(argv[2]).read_text()
        return (0, "", "")

    def fake_run(cmd, **kw):
        # Log-dir mkdir/chown prep (still direct subprocess in deploy).
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    set_scheduler(LaunchdScheduler(runner=runner))
    try:
        with patch.object(deploy.subprocess, "run", side_effect=fake_run), \
             patch.object(deploy.shutil, "which", return_value="/opt/homebrew/bin/node"), \
             patch.object(deploy, "_wait_for_gateway_port", return_value=True), \
             patch("pathlib.Path.exists", return_value=False):
            ok, detail = deploy.install_bot_gateway_plist(
                "team_bot_b", port=18790, user="personal_bot_user",
            )
    finally:
        set_scheduler(None)
    assert ok, f"install failed under mocks: {detail}"
    _assert_parity("gateway", captured["xml"])


# ── 4/5. install_helpers ─────────────────────────────────────────────────────


def test_helpers_signal_every_parity():
    from evolve_admin.applications import install_helpers as ih
    with patch.object(ih, "VENV_PYTHON", PIN_PY):
        xml = ih._build_plist_xml(
            label="com.team_bot_a.evolve.scan",
            wrapper_path="/Users/team_bot_a/.openclaw/workspace/evolve/scheduled/scan.py",
            schedule={"every_minutes": 30},
            log_path="/Users/team_bot_a/.openclaw/logs/scan.log",
            err_log_path="/Users/team_bot_a/.openclaw/logs/scan.err.log",
        )
    _assert_parity("helpers_signal_every", xml)


def test_helpers_signal_cron_user_parity():
    from evolve_admin.applications import install_helpers as ih
    with patch.object(ih, "VENV_PYTHON", PIN_PY):
        xml = ih._build_plist_xml(
            label="com.team_bot_a.evolve.daily",
            wrapper_path="/Users/team_bot_a/.openclaw/workspace/evolve/scheduled/daily.py",
            schedule={"cron": {"Hour": 7, "Minute": 15}},
            log_path="/Users/team_bot_a/.openclaw/logs/daily.log",
            err_log_path="/Users/team_bot_a/.openclaw/logs/daily.err.log",
            user_name="team_bot_a",
        )
    _assert_parity("helpers_signal_cron_user", xml)


def test_helpers_command_minimal_parity():
    from evolve_admin.applications import install_helpers as ih
    xml = ih._build_command_plist_xml(
        label="com.team_bot_b.evolve.fetch",
        program_arguments=["/bin/bash", "-lc", "echo 'a & b' > /tmp/out"],
        schedule={"every_minutes": 5},
        log_path="/Users/team_bot_b/.openclaw/logs/fetch.log",
        err_log_path="/Users/team_bot_b/.openclaw/logs/fetch.err.log",
    )
    _assert_parity("helpers_command_minimal", xml)


def test_helpers_command_full_parity():
    from evolve_admin.applications import install_helpers as ih
    xml = ih._build_command_plist_xml(
        label="com.team_bot_b.evolve.report",
        program_arguments=["/opt/homebrew/bin/node", "/Users/team_bot_b/app/report.js"],
        schedule={"cron": {"Weekday": 5, "Hour": 17, "Minute": 0}},
        log_path="/Users/team_bot_b/.openclaw/logs/report.log",
        err_log_path="/Users/team_bot_b/.openclaw/logs/report.err.log",
        cwd="/Users/team_bot_b/app",
        env={"NODE_ENV": "prod", "Q": 'a"<>&'},
        user_name="team_bot_b",
        group_name="staff",
    )
    _assert_parity("helpers_command_full", xml)


# ── 6. digest_dispatcher / audit_scheduler ───────────────────────────────────


def test_digest_default_parity():
    from evolve_admin.alerts import digest_dispatcher
    _assert_parity("digest_default", digest_dispatcher.render_plist())


def test_audit_scheduler_default_parity():
    from evolve_admin.applications import audit_scheduler
    _assert_parity("audit_scheduler_default", audit_scheduler.render_plist())


# ── S1 sweep — the emitters S0 didn't cover ──────────────────────────────────
# Fixtures captured 2026-06-10 from the pre-JobSpec hand-rolled XML (after a
# verbatim extract-into-pure-helper refactor, verified content-identical).


def test_deploy_imessage_poller_parity():
    from evolve_admin import deploy
    # _CANONICAL_SHARED_DIR freezes at deploy-import time (module-level
    # constant in evolve_config), which on a Linux CI runner happens during
    # collection — before the conftest macOS profile pin takes effect. Pin
    # the golden's macOS value explicitly, same shape as the VENV_PYTHON pins.
    with patch.object(deploy, "_CANONICAL_SHARED_DIR", Path("/Users/Shared/evolve")):
        xml = deploy._imessage_poller_plist_content(
            label="ai.evolve.evolve.imessage-poller.team_bot_a",
            bot_id="team_bot_a", port=18790,
            analyzer_dir=PIN_ANALYZER, python3=PIN_PY,
        )
    _assert_parity("deploy_imessage_poller", xml)


def test_deploy_opik_parity():
    from evolve_admin import deploy
    xml = deploy._opik_plist_content(
        deploy.OPIK_LAUNCHD_LABEL, "/usr/local/bin/docker",
        deploy.OPIK_INSTALL_DIR / "deployment" / "docker-compose",
    )
    _assert_parity("deploy_opik", xml)


# The custom-daemon emitters now return a JobSpec (W10-C seam sweep); the
# parity goldens are the rendered launchd plist, so render the spec here. They
# derive shared paths from _CANONICAL_{SHARED_DIR,NETWORK_JSON} (import-frozen —
# pin the macOS values for Linux-CI stability, same as the imessage guard).
PIN_SHARED = Path("/Users/Shared/evolve")
PIN_NETWORK = Path("/Users/Shared/evolve/network.json")


def test_deploy_better_engine_parity():
    from evolve_admin import deploy
    with patch.object(deploy, "VENV_PYTHON", PIN_PY), \
         patch.object(deploy, "_CANONICAL_SHARED_DIR", PIN_SHARED):
        xml = render_launchd_plist(deploy._better_engine_jobspec(
            "ai.openclaw.evolve.better", "evolve", "/Users/evolve",
            PIN_ANALYZER / "better_engine_refresh.py",
        ))
    _assert_parity("deploy_better_engine", xml)


def test_deploy_signal_subscriber_parity():
    from evolve_admin import deploy
    with patch.object(deploy, "VENV_PYTHON", PIN_PY), \
         patch.object(deploy, "ANALYZER_DIR", PIN_ANALYZER), \
         patch.object(deploy, "_CANONICAL_SHARED_DIR", PIN_SHARED), \
         patch.object(deploy, "_CANONICAL_NETWORK_JSON", PIN_NETWORK):
        xml = render_launchd_plist(deploy._signal_subscriber_jobspec(
            "ai.evolve.evolve.signal-subscriber", "evolve",
            PIN_ANALYZER / "signal_subscriber_runner.py",
        ))
    _assert_parity("deploy_signal_subscriber", xml)


def test_deploy_admin_ui_parity():
    from evolve_admin import deploy
    with patch.object(deploy, "_CANONICAL_SHARED_DIR", PIN_SHARED), \
         patch.object(deploy, "_CANONICAL_NETWORK_JSON", PIN_NETWORK):
        xml = render_launchd_plist(
            deploy._admin_ui_jobspec("ai.evolve.evolve.admin-ui"))
    _assert_parity("deploy_admin_ui", xml)


def test_deploy_mcp_bridge_parity():
    from evolve_admin import deploy
    with patch.object(deploy, "VENV_PYTHON", PIN_PY), \
         patch.object(deploy, "_CANONICAL_NETWORK_JSON", PIN_NETWORK):
        xml = render_launchd_plist(deploy._mcp_bridge_jobspec(
            "ai.evolve.evolve.mcp-bridge", "evolve", 5051, "0.0.0.0",
        ))
    _assert_parity("deploy_mcp_bridge", xml)


def test_mcp_service_bridge_parity():
    from evolve_admin import mcp_service
    # LOG_PATH is now resolved lazily per-platform (user_home("evolve")); pin
    # the macOS value at the _log_path helper so the JobSpec's StandardOut*Path
    # is deterministic regardless of the test box's pwd/profile.
    with patch.object(mcp_service, "VENV_PYTHON", PIN_PY), \
         patch.dict(os.environ, {"PATH": PIN_PATH}), \
         patch.object(mcp_service, "_log_path",
                      lambda: Path("/Users/evolve/.evolve/logs/mcp-bridge.log")):
        xml = mcp_service.generate_plist(
            host="0.0.0.0", port=5051,
            network=Path("/Users/Shared/evolve/network.json"),
        )
    _assert_parity("mcp_service_bridge", xml)


def test_repo_puller_parity():
    from evolve_admin import repo_puller
    _assert_parity("repo_puller", repo_puller.render_plist())


def test_pairing_sweep_parity():
    from evolve_admin.pairing import auto_approver
    _assert_parity("pairing_sweep", auto_approver.render_plist())


def test_tunnel_persistent_parity():
    from evolve_admin import tunnel
    xml = tunnel._build_plist_xml(
        "/opt/homebrew/bin/autossh",
        ["-M", "0", "-i", "/Users/pod-admin/.ssh/id_ed25519",
         "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
         "-o", "ExitOnForwardFailure=yes", "-N",
         "-L", "5050:localhost:5050", "pod-admin@pod-host.local"],
        Path("/Users/pod-admin/.evolve/logs/tunnel.log"),
    )
    _assert_parity("tunnel_persistent", xml)


_WIZARD_GW_ARGS = (
    "ai.openclaw.evolve-gateway",
    "/opt/homebrew/bin/node",
    "/opt/homebrew/lib/node_modules/openclaw/dist/index.js",
    "19030",
)


def test_wizard_evolve_gateway_parity():
    from evolve_admin import setup_wizard
    xml = setup_wizard._evolve_gateway_plist_content(
        *_WIZARD_GW_ARGS, "placeholder-token-0123456789abcdef", "2026.6.1",
    )
    _assert_parity("wizard_evolve_gateway", xml)


def test_wizard_evolve_gateway_no_token_parity():
    """Empty token must still emit the OPENCLAW_GATEWAY_TOKEN env key (as an
    empty string) — the gateway reads the key's presence, and the original
    emitter's `_token_xml` branch emitted <string></string> for this case."""
    from evolve_admin import setup_wizard
    xml = setup_wizard._evolve_gateway_plist_content(
        *_WIZARD_GW_ARGS, "", "2026.6.1",
    )
    _assert_parity("wizard_evolve_gateway_no_token", xml)
    parsed = plistlib.loads(xml.encode())
    assert parsed["EnvironmentVariables"]["OPENCLAW_GATEWAY_TOKEN"] == ""


# ── renderer unit behavior (not parity — new contracts) ─────────────────────


def test_run_at_load_always_emitted_explicitly():
    """pod-health-invariants.H7: RunAtLoad must land on disk even when False
    so "intentionally off" and "forgot to set it" stay distinguishable."""
    xml = render_launchd_plist(JobSpec(label="t", program_args=["/bin/true"]))
    assert plistlib.loads(xml.encode())["RunAtLoad"] is False


def test_keep_alive_dict_shape_passes_through():
    xml = render_launchd_plist(JobSpec(
        label="t", program_args=["/bin/true"],
        keep_alive={"SuccessfulExit": False},
    ))
    assert plistlib.loads(xml.encode())["KeepAlive"] == {"SuccessfulExit": False}


def test_jitter_wraps_program_in_bash_sleep_exec():
    xml = render_launchd_plist(JobSpec(
        label="t", program_args=["/usr/bin/foo", "--flag", "two words"],
        jitter_seconds=45,
    ))
    args = plistlib.loads(xml.encode())["ProgramArguments"]
    assert args[:2] == ["/bin/bash", "-c"]
    assert args[2] == "sleep $((RANDOM % 45)); exec /usr/bin/foo --flag 'two words'"


def test_program_args_must_be_strings():
    with pytest.raises(ValueError, match="must be strings"):
        render_launchd_plist(JobSpec(label="t", program_args=["/bin/echo", 5050]))


def test_empty_program_args_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        render_launchd_plist(JobSpec(label="t", program_args=[]))


def test_watch_paths_is_a_real_field():
    """WatchPaths is modelled as a first-class JobSpec field (S1 sweep) —
    never smuggled through ``extra``. The better-engine daemon relies on it
    for its urgent-refresh trigger."""
    xml = render_launchd_plist(JobSpec(
        label="t", program_args=["/bin/true"],
        watch_paths=["/tmp/trigger", "/tmp/other"],
    ))
    assert plistlib.loads(xml.encode())["WatchPaths"] == ["/tmp/trigger", "/tmp/other"]
    # unset → key omitted entirely
    xml = render_launchd_plist(JobSpec(label="t", program_args=["/bin/true"]))
    assert "WatchPaths" not in plistlib.loads(xml.encode())


def test_extra_keys_emitted_with_warning(caplog):
    import logging
    # The renderer's home is runtime/scheduler.py in the analyzer package
    # (4.3C S2b0); evolve_admin.runtime is a re-export shim, so log records
    # carry the real module's __name__.
    with caplog.at_level(logging.WARNING, logger="runtime.scheduler"):
        xml = render_launchd_plist(JobSpec(
            label="t", program_args=["/bin/true"],
            extra={"LimitLoadToSessionType": "Aqua"},
        ))
    assert plistlib.loads(xml.encode())["LimitLoadToSessionType"] == "Aqua"
    assert any("unmodelled launchd keys" in r.message for r in caplog.records)
