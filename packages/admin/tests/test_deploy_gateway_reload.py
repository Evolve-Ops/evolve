"""tests/test_deploy_gateway_reload.py — a plugin-only deploy must leave the
gateway running the freshly-installed plugin.

Incident (evolve-vps darwin, 2026-07-01, #3362 rollout): ``sudo evolve-admin
deploy <bot>`` completed every step with green output — including "Installing
gateway plist ✅" — but did NOT cycle the running OpenClaw gateway. The bounce
in ``_full_deploy`` is only a SIDE EFFECT of ``install_bot_gateway_plist``; on a
plugin-ONLY change the unit/plist is byte-identical, so the bounce is the
skip-path ``scheduler.restart()`` inside ``_install_job_ensuring_restart``. When
that silently no-ops the OLD gateway process keeps holding the port, the
install's own port-bind wait is satisfied by it, and the deploy reports success
while the gateway serves the OLD plugin until a human restarts it.

``verify_gateway_loaded_new_plugin`` (deploy_steps.py) is the post-condition
that closes the gap: it proves the gateway PID is newer than the deploy (forcing
a restart if not) and that the plugin actually answers ``/evolve/status`` — not
merely that a process holds the port. Locked here:

  1. plugin-only change (unchanged unit ⇒ install skipped its bounce ⇒ stale
     PID): the verifier FORCES exactly one ``restart_gateway`` and re-checks;
  2. happy path (install already bounced ⇒ fresh PID): NO forced restart;
  3. unrecoverable (still stale after the forced restart): exits non-zero;
  4. plugin never answers after restart: exits non-zero;
  5. no port ⇒ skip the HTTP probe but still verify (and force) the restart.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin import deploy_steps  # noqa: E402
from evolve_admin.deploy_verify import VerificationResult  # noqa: E402


def _patch_common(monkeypatch):
    """No-op sleeps + record restart_gateway calls. Returns the call list."""
    monkeypatch.setattr(deploy_steps.time, "sleep", lambda *a, **k: None)
    restarts: list[str] = []
    monkeypatch.setattr(
        deploy_steps, "restart_gateway", lambda bot_id: restarts.append(bot_id)
    )
    return restarts


def _stub_gateway_verdicts(monkeypatch, verdicts):
    """Make verify_bot_gateway_running_new_plugin return ``verdicts`` in order
    (the last verdict repeats once the list is exhausted)."""
    seq = list(verdicts)
    default = verdicts[-1]

    def _fake(*, bot_id, deploy_began_at_epoch, **_):
        return seq.pop(0) if seq else default

    monkeypatch.setattr(
        deploy_steps, "verify_bot_gateway_running_new_plugin", _fake
    )


_STALE = VerificationResult(
    ok=False,
    summary="Daemon ai.openclaw.darwin-gateway: still running pre-upgrade PID 4242",
)
_FRESH = VerificationResult(
    ok=True,
    summary="Daemon ai.openclaw.darwin-gateway: PID 5150 restarted after upgrade",
)


def test_plugin_only_change_forces_restart_when_install_skipped_bounce(monkeypatch):
    """The motivating bug: unchanged unit ⇒ install skipped its bounce ⇒ the
    gateway PID predates the deploy. The verifier must force ONE restart and,
    once the re-check passes and the plugin answers, complete cleanly."""
    restarts = _patch_common(monkeypatch)
    # First check (post-install) is stale; after the forced restart it's fresh.
    _stub_gateway_verdicts(monkeypatch, [_STALE, _FRESH])
    monkeypatch.setattr(
        deploy_steps, "verify_plugin_live", lambda bot_id, port: f"{bot_id} live at :{port}"
    )

    # Does not raise SystemExit.
    deploy_steps.verify_gateway_loaded_new_plugin("darwin", 8760, deploy_began_at=1000.0)

    assert restarts == ["darwin"], "must force exactly one restart to load the new plugin"


def test_happy_path_does_not_double_restart(monkeypatch):
    """When the install already bounced the gateway (fresh PID), the verifier
    must NOT restart again — no gratuitous second bounce on the healthy path."""
    restarts = _patch_common(monkeypatch)
    _stub_gateway_verdicts(monkeypatch, [_FRESH])
    monkeypatch.setattr(
        deploy_steps, "verify_plugin_live", lambda bot_id, port: f"{bot_id} live at :{port}"
    )

    deploy_steps.verify_gateway_loaded_new_plugin("darwin", 8760, deploy_began_at=1000.0)

    assert restarts == [], "healthy path must not force an extra restart"


def test_still_stale_after_forced_restart_exits_nonzero(monkeypatch):
    """If the gateway is STILL on a stale PID even after the forced restart,
    the deploy must fail loudly rather than report a phantom success."""
    _patch_common(monkeypatch)
    _stub_gateway_verdicts(monkeypatch, [_STALE, _STALE])
    monkeypatch.setattr(
        deploy_steps, "verify_plugin_live", lambda bot_id, port: "should-not-reach"
    )

    with pytest.raises(SystemExit):
        deploy_steps.verify_gateway_loaded_new_plugin("darwin", 8760, deploy_began_at=1000.0)


def test_plugin_not_live_after_restart_exits_nonzero(monkeypatch):
    """Process bounced but the plugin never answers /evolve/status — the deploy
    must fail: 'a process is up' is not proof the plugin loaded."""
    _patch_common(monkeypatch)
    _stub_gateway_verdicts(monkeypatch, [_FRESH])
    monkeypatch.setattr(deploy_steps, "verify_plugin_live", lambda bot_id, port: None)

    with pytest.raises(SystemExit):
        deploy_steps.verify_gateway_loaded_new_plugin("darwin", 8760, deploy_began_at=1000.0)


def test_no_port_skips_http_probe_but_still_verifies_restart(monkeypatch):
    """A port-less deploy can't probe /evolve/status, but the PID-restart
    guarantee still holds — and a stale gateway still forces a restart."""
    restarts = _patch_common(monkeypatch)
    _stub_gateway_verdicts(monkeypatch, [_STALE, _FRESH])
    # verify_plugin_live must NOT be consulted when there is no port.
    monkeypatch.setattr(
        deploy_steps, "verify_plugin_live",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not probe without a port")),
    )

    deploy_steps.verify_gateway_loaded_new_plugin("darwin", None, deploy_began_at=1000.0)

    assert restarts == ["darwin"]
