"""tests/test_evo_infra_tools.py — action.infra.daemon_restart guards.

Added in response to the May 2026 evo coverage audit. The
``infra_daemon_down`` chip is critical-severity but evo had no tool
to act on it — best the model could do was tell the operator to SSH.

The tool wraps ``sudo /bin/launchctl kickstart -k system/ai.evolve.<id>``
for a WHITELISTED set of pod-wide infra daemons. The whitelist is
load-bearing: a model that misreads "restart something" can't
accidentally kick a destructive launchd job because the validate +
handler both refuse anything outside the set.

Tests patch the path helper + subprocess so we never touch
``/Library/LaunchDaemons`` or spawn launchctl on the real host.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path


_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _seed_plist(plist_path: Path) -> None:
    """Write a minimal launchd plist (Label is the only required field)."""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as f:
        plistlib.dump({"Label": "ai.evolve.test.daemon"}, f)


def _patch_plist_path(monkeypatch, module, tmp_path: Path, daemon_id: str):
    """Redirect _daemon_plist_path so we don't touch /Library/LaunchDaemons."""
    fake = tmp_path / "LaunchDaemons" / f"ai.evolve.{daemon_id}.plist"
    monkeypatch.setattr(
        module, "_daemon_plist_path",
        lambda d: (
            fake if d == daemon_id
            else tmp_path / "LaunchDaemons" / f"ai.evolve.{d}.plist"
        ),
    )
    return fake


# ─── Registration ────────────────────────────────────────────────────────────


def test_daemon_restart_tool_registered():
    """Tool must appear in the registry under the expected name with
    WRITE_RISKY tier. Catches a refactor that drops the register() call
    or changes the tier silently."""
    from evolve_admin.evo.tools import RiskTier, lookup
    t = lookup("action.infra.daemon_restart")
    assert t is not None, (
        "action.infra.daemon_restart not registered. Did the import "
        "side-effect in evolve_admin.evo.tools.action_infra get removed?"
    )
    assert t.risk_tier == RiskTier.WRITE_RISKY, (
        "daemon_restart should be WRITE_RISKY — daemon restart is "
        "recoverable but disrupts in-flight work (heal mid-sweep, "
        "verify mid-poll, repo-puller mid-fetch). Auto-runs only "
        "under 'auto' authority."
    )
    assert t.validate is not None, (
        "WRITE_RISKY tools must define validate() so the proxy can "
        "render a clear-reason error instead of a button that errors "
        "at click time."
    )


def test_whitelist_contains_load_bearing_daemons():
    """The audit identified these as the high-impact daemons whose
    restart evo most often needs to perform. Pin them so a future
    whitelist refactor can't silently drop one."""
    from evolve_admin.evo.tools.action_infra import _ALLOWED_INFRA_DAEMONS
    for daemon in (
        "evolve.heal",
        "evolve.verify",
        "evolve.repo-puller",
        "evolve.admin-ui",
        "evolve.signal-notifier",
    ):
        assert daemon in _ALLOWED_INFRA_DAEMONS, (
            f"load-bearing infra daemon '{daemon}' is missing from "
            f"_ALLOWED_INFRA_DAEMONS. Removing it means evo can't "
            f"restart it when the chip fires."
        )


# ─── Validate gates ──────────────────────────────────────────────────────────


def test_validate_rejects_empty_daemon_id(tmp_path):
    """No daemon_id means no action — clear reason."""
    from evolve_admin.evo.tools import action_infra
    r = action_infra._daemon_restart_validate(daemon_id="")
    assert r["ok"] is False
    assert "daemon_id" in r["reason"]


def test_validate_rejects_unknown_daemon_id(tmp_path):
    """A daemon_id not in the whitelist must be rejected at validate
    time with a clear reason — the model needs to know this is a
    whitelist issue, not a 'try again' situation."""
    from evolve_admin.evo.tools import action_infra
    r = action_infra._daemon_restart_validate(daemon_id="evolve.ghost-daemon")
    assert r["ok"] is False
    assert "allowed" in r["reason"].lower() or "whitelist" in r["reason"].lower()
    # Common allowed values should be hinted to help the operator
    # mental-match a typo.
    assert "evolve.heal" in r["reason"]


def test_validate_rejects_not_in_whitelist_even_if_plist_exists(monkeypatch, tmp_path):
    """The whitelist check must come BEFORE the plist check. Even if
    a destructive plist file happens to exist at the expected path,
    the validator must refuse a non-whitelisted id. Defense in depth
    against a model that constructs a plausible-looking daemon name."""
    from evolve_admin.evo.tools import action_infra
    fake = _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.totally-fake")
    _seed_plist(fake)  # Plist exists on disk.

    r = action_infra._daemon_restart_validate(daemon_id="evolve.totally-fake")
    assert r["ok"] is False
    # Must be the whitelist error, NOT the plist error.
    assert "allowed" in r["reason"].lower() or "whitelist" in r["reason"].lower()
    # And NOT the install-infra-jobs hint, which would imply "this is
    # a valid daemon, just not installed yet".
    assert "install-infra-jobs" not in r["reason"]


def test_validate_rejects_whitelisted_but_uninstalled_plist(monkeypatch, tmp_path):
    """When the daemon IS whitelisted but the plist isn't installed,
    the validator returns the install-infra-jobs hint. That's a
    different remediation path than the whitelist error."""
    from evolve_admin.evo.tools import action_infra
    _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.heal")
    # Note: not seeding the plist file.

    r = action_infra._daemon_restart_validate(daemon_id="evolve.heal")
    assert r["ok"] is False
    # Should mention the install command, not the whitelist.
    assert "install-infra-jobs" in r["reason"]


def test_validate_happy_path(monkeypatch, tmp_path):
    """When daemon IS whitelisted AND plist is installed, validate
    returns ok=True with informative context for the proxy's confirm
    button rendering."""
    from evolve_admin.evo.tools import action_infra
    fake = _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.heal")
    _seed_plist(fake)

    r = action_infra._daemon_restart_validate(daemon_id="evolve.heal")
    assert r["ok"] is True
    assert r["context"]["daemon_id"] == "evolve.heal"
    assert r["context"]["launchd_label"] == "ai.evolve.evolve.heal"


# ─── Handler ─────────────────────────────────────────────────────────────────


def test_handler_invokes_correct_launchctl_command(monkeypatch, tmp_path):
    """Happy path: handler runs ``sudo /bin/launchctl kickstart -k
    system/ai.evolve.<daemon_id>`` with the right exact shape."""
    from evolve_admin.evo.tools import action_infra
    fake = _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.heal")
    _seed_plist(fake)

    captured: dict = {}

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    r = action_infra._daemon_restart_handler(
        daemon_id="evolve.heal", reason="test",
    )
    assert r["ok"] is True
    assert r["daemon_id"] == "evolve.heal"
    assert r["launchd_label"] == "ai.evolve.evolve.heal"
    assert r["verify_via"]["tool"] == "pod_state.bots"

    # Exact command shape — this is the load-bearing assertion since
    # any drift here means the wrong daemon could be restarted.
    assert captured["cmd"][0] == "sudo"
    assert captured["cmd"][1] == "/bin/launchctl"
    assert captured["cmd"][2:4] == ["kickstart", "-k"]
    assert captured["cmd"][4] == "system/ai.evolve.evolve.heal"


def test_handler_propagates_non_zero_exit(monkeypatch, tmp_path):
    """If launchctl exits non-zero, the handler must surface the stderr
    text — operators need the actual error, not a generic 'failed'."""
    from evolve_admin.evo.tools import action_infra
    fake = _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.heal")
    _seed_plist(fake)

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 113, stdout="",
            stderr='Could not find service "ai.evolve.evolve.heal" in domain for system',
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    r = action_infra._daemon_restart_handler(daemon_id="evolve.heal")
    assert r["ok"] is False
    # Post Scheduler-seam migration the exit code itself is no longer
    # embedded (``Scheduler.restart`` reports combined output), but the
    # launchctl diagnostic text must still come through.
    assert "Could not find service" in r["error"]


def test_handler_rejects_unknown_daemon_id_defense_in_depth(monkeypatch, tmp_path):
    """Defense in depth: even if validate is bypassed (direct call from
    a hypothetical future caller), the handler ITSELF refuses unknown
    daemon ids. This is the same pattern as action.bot.remove's
    confirm:true gate."""
    from evolve_admin.evo.tools import action_infra
    # Seed a plist at the destructive path so a no-whitelist-check
    # implementation would happily kickstart it.
    fake = _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.something-bad")
    _seed_plist(fake)

    # Stub subprocess in case the handler reaches it (it shouldn't).
    spawned = []

    def _fake_run(cmd, **kw):
        spawned.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    r = action_infra._daemon_restart_handler(daemon_id="evolve.something-bad")
    assert r["ok"] is False
    assert "allowed" in r["error"].lower()
    # Critical: subprocess must NOT have been invoked.
    assert spawned == [], (
        "handler reached subprocess.run despite refusing the daemon_id. "
        "The whitelist check must short-circuit BEFORE any side effects."
    )


def test_handler_rejects_missing_plist_defense_in_depth(monkeypatch, tmp_path):
    """Defense in depth: even for a whitelisted daemon, the handler
    refuses if the plist isn't on disk. Direct calls bypassing
    validate still get a safe failure."""
    from evolve_admin.evo.tools import action_infra
    _patch_plist_path(monkeypatch, action_infra, tmp_path, "evolve.heal")
    # Not seeding the plist.

    r = action_infra._daemon_restart_handler(daemon_id="evolve.heal")
    assert r["ok"] is False
    assert "plist" in r["error"].lower()


def test_handler_signature_takes_no_network_path():
    """The mcp_server bridge injects ``network_path`` only when a
    handler's signature requests it. This tool reads no per-pod config
    (just kicks launchd) so it should NOT declare network_path —
    keeping the model-facing signature minimal."""
    import inspect
    from evolve_admin.evo.tools import action_infra
    params = inspect.signature(
        action_infra._daemon_restart_handler,
    ).parameters
    assert "network_path" not in params, (
        "action.infra.daemon_restart handler shouldn't take network_path "
        "— it doesn't read any per-pod config. If you added it for a "
        "real reason, update this test."
    )
    # And it must take daemon_id (the model-facing required arg).
    assert "daemon_id" in params
