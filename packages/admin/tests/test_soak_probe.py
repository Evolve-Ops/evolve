"""tests/test_soak_probe.py — active-canary probe proof artifacts.

Spec: docs/spec-delta-active-canary-validation-2026-06-13.md §D5
(gateway liveness probe). The release-manager wiring (always-on plan,
fail-closed / fail-open) is proven in test_release_manager.py `-k d5`;
THIS file proves the probe's verdict LOGIC: the loopback /evolve/status
round-trip → ok / regression / error tri-state, with the never-fail-on-
our-own-tooling invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import soak_probe  # noqa: E402

# A canary with a configured gateway port.
_NETWORK = {"bots": {"canary_bot": {"port": 8787}}}


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """Never really sleep ~60s, and shrink the retry ceiling so the
    fail-path tests are fast and assert an exact attempt count."""
    monkeypatch.setattr(soak_probe.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(soak_probe, "_GATEWAY_LIVENESS_RETRIES", 2)


def _patch_probe_once(monkeypatch, fn):
    """Substitute the single-shot loopback round-trip the wrapper reuses."""
    import evolve_admin.wizard_verify as wv
    monkeypatch.setattr(wv, "_probe_gateway_once", fn)


# ── D5: the gateway serves on the new code → OK ───────────────────────────────


def test_d5_gateway_serving_is_ok(monkeypatch):
    """A canary whose gateway serves plugin-signed /evolve/status → OK
    (the healthy candidate)."""
    _patch_probe_once(monkeypatch, lambda port: (True, "plugin loaded (keys: status)"))
    verdict, detail = soak_probe._probe_gateway_liveness(_NETWORK, "canary_bot")
    assert verdict == soak_probe.OK
    assert ":8787" in detail


def test_d5_gateway_recovers_within_ceiling_is_ok(monkeypatch):
    """Warm-up tolerance: the gateway is slow to mount the evolve plugin
    route (SPA fallback for the first attempts) but comes up before the
    ceiling → OK. No false regression on a slow boot."""
    state = {"n": 0}

    def slow(port):
        state["n"] += 1
        if state["n"] < 3:
            return (False, "200 OK but body is not JSON (likely OC SPA fallback)")
        return (True, "plugin loaded (keys: bot_id)")

    _patch_probe_once(monkeypatch, slow)
    verdict, _ = soak_probe._probe_gateway_liveness(_NETWORK, "canary_bot")
    assert verdict == soak_probe.OK


# ── D5: the candidate broke the gateway on boot → REGRESSION (fail CLOSED) ─────


def test_d5_gateway_never_comes_up_is_regression(monkeypatch):
    """The 'imports clean (Gate 1), crashes the gateway on boot' class:
    /evolve/status never returns plugin-signed JSON within the ceiling →
    REGRESSION (fail CLOSED). Retried to the ceiling before the verdict —
    a single transient miss must not fail a release."""
    calls: list[int] = []

    def never(port):
        calls.append(port)
        return (False, "probe failed: Connection refused")

    _patch_probe_once(monkeypatch, never)
    verdict, detail = soak_probe._probe_gateway_liveness(_NETWORK, "canary_bot")
    assert verdict == soak_probe.REGRESSION
    assert "not serving" in detail
    # 1 initial + the (shrunk) retry ceiling — proves it retried, didn't
    # fail on the first miss.
    assert len(calls) == 1 + soak_probe._GATEWAY_LIVENESS_RETRIES


def test_d5_gateway_serving_non_plugin_html_is_regression(monkeypatch):
    """OC's SPA fallback (200 OK, HTML body) means the OC process is up but
    the evolve plugin route never mounted — the candidate broke the plugin
    boot. Persisting past the ceiling → REGRESSION, NOT a pass: a 200 that
    is not plugin-signed is exactly the boot-crash signature."""
    _patch_probe_once(
        monkeypatch,
        lambda port: (False, "200 OK but body is not JSON (likely OC SPA fallback — plugin not loaded)"),
    )
    verdict, _ = soak_probe._probe_gateway_liveness(_NETWORK, "canary_bot")
    assert verdict == soak_probe.REGRESSION


# ── D5: our OWN fault (couldn't probe) → ERROR (fail OPEN) ─────────────────────


def test_d5_unknown_port_is_tooling_error_not_regression(monkeypatch):
    """No gateway port for the bot → we COULDN'T probe (our fault) → ERROR
    (fail OPEN), never REGRESSION. The round-trip must not even be
    attempted. A release is never failed on our own tooling fault."""
    def boom(port):
        raise AssertionError("must not probe when the port is unknown")

    _patch_probe_once(monkeypatch, boom)
    verdict, detail = soak_probe._probe_gateway_liveness(
        {"bots": {"canary_bot": {}}}, "canary_bot")
    assert verdict == soak_probe.ERROR
    assert "no gateway port" in detail


def test_d5_probe_harness_raise_is_tooling_error(monkeypatch):
    """An unexpected raise from the round-trip itself is a tooling fault →
    ERROR (fail OPEN), not a candidate REGRESSION."""
    def raises(port):
        raise RuntimeError("urllib exploded")

    _patch_probe_once(monkeypatch, raises)
    verdict, detail = soak_probe._probe_gateway_liveness(_NETWORK, "canary_bot")
    assert verdict == soak_probe.ERROR
    assert "probe raised" in detail


# ── D5: dispatch + exit-code contract through run() ───────────────────────────


def test_d5_gateway_kind_dispatches_and_drives_exit_code(monkeypatch):
    """The 'gateway' kind is dispatched by run()/_run_one and its verdict
    drives the subprocess exit code: a REGRESSION gateway probe → exit 2
    (the release manager's fail-CLOSED code)."""
    _patch_probe_once(monkeypatch, lambda port: (False, "probe failed: Connection refused"))
    plan = [{"kind": "gateway", "target": None}]
    code, results = soak_probe.run(plan, Path("/tmp"), _NETWORK, "canary_bot")
    assert code == 2  # _EXIT[REGRESSION] — fail CLOSED
    assert results[0]["kind"] == "gateway"
    assert results[0]["verdict"] == soak_probe.REGRESSION


def test_d5_gateway_regression_outranks_other_ok_probes(monkeypatch):
    """A broken gateway fails the whole plan closed even when a diff-
    selected probe would pass — run() returns the most-severe verdict."""
    _patch_probe_once(monkeypatch, lambda port: (False, "probe failed: Connection refused"))
    # A route probe that imports clean (OK) alongside the broken gateway.
    plan = [
        {"kind": "gateway", "target": None},
        {"kind": "route", "target": "release_routes"},
    ]
    code, results = soak_probe.run(plan, Path("/tmp"), _NETWORK, "canary_bot")
    assert code == 2  # gateway REGRESSION dominates
    verdicts = {r["kind"]: r["verdict"] for r in results}
    assert verdicts["gateway"] == soak_probe.REGRESSION


# ── send-probe: SILENT contract probe ([META:deploy] 2026-06-23) ──────────────
#
# The send probe used to do a real dispatcher.send to the OPERATOR alert
# channel and read DispatchResult.SENT as its verdict — a vestigial green
# chat message that delivered on every delivery-path soak. The operator
# decision is SILENT SUCCESS: on a healthy soak the operator sees NOTHING.
# The verdict is now derived from the send-surface CONTRACT probe
# (safe_upgrade.probe_send_surface) + the always-on gateway-liveness probe;
# no live operator-channel send happens. These tests pin both halves:
#   (a) the SENT/OK path dispatches NOTHING to the operator, and
#   (b) the verdict is still derived correctly (FAILED → REGRESSION,
#       unverified → ERROR, ok → OK).


def _patch_send_surface(monkeypatch, result: dict):
    """Substitute the contract probe the send kind reads its verdict from."""
    import evolve_admin.safe_upgrade as su
    monkeypatch.setattr(su, "probe_send_surface", lambda *a, **k: result)


def _ban_dispatch(monkeypatch):
    """Make ANY dispatcher.send call a hard failure — the send probe must
    NOT push a message on the success (or any) path. A healthy soak is
    silent; the operator's watched inbox must never receive a soak probe."""
    import evolve_admin.alerts.dispatcher as dispatcher

    def _no_send(*a, **k):  # pragma: no cover — must never be reached
        raise AssertionError(
            "soak send-probe must not call dispatcher.send (silent success)"
        )

    monkeypatch.setattr(dispatcher, "send", _no_send)


def test_send_probe_contract_ok_is_silent_ok(monkeypatch):
    """Contract intact → OK, and NOTHING is dispatched to the operator.
    This is the silent-success path: the operator sees no message."""
    import evolve_admin.safe_upgrade as su
    _ban_dispatch(monkeypatch)
    _patch_send_surface(monkeypatch, {"status": su.SEND_SURFACE_OK})
    verdict, detail = soak_probe._probe_send(Path("/tmp"), _NETWORK)
    assert verdict == soak_probe.OK
    assert "silent" in detail  # documents the no-send proof shape


def test_send_probe_contract_broken_is_regression(monkeypatch):
    """A removed/renamed send surface (FAILED contract) → REGRESSION (the
    candidate's delivery path is broken). Still no operator-channel send —
    the regression is surfaced through the soak-failure path, not a chat
    message from the probe itself."""
    import evolve_admin.safe_upgrade as su
    _ban_dispatch(monkeypatch)
    _patch_send_surface(
        monkeypatch,
        {"status": su.SEND_SURFACE_FAILED, "reason": "flags gone from message send"},
    )
    verdict, detail = soak_probe._probe_send(Path("/tmp"), _NETWORK)
    assert verdict == soak_probe.REGRESSION
    assert "surface broken" in detail


def test_send_probe_unverified_is_tooling_error(monkeypatch):
    """"Couldn't verify" → tooling ERROR (fail OPEN), never a pass and never
    a regression — the never-fail-a-release-on-our-own-tooling invariant."""
    import evolve_admin.safe_upgrade as su
    _ban_dispatch(monkeypatch)
    _patch_send_surface(
        monkeypatch,
        {"status": su.SEND_SURFACE_UNVERIFIED, "reason": "openclaw not on PATH"},
    )
    verdict, detail = soak_probe._probe_send(Path("/tmp"), _NETWORK)
    assert verdict == soak_probe.ERROR
    assert "unverified" in detail


def test_send_probe_contract_raise_is_tooling_error(monkeypatch):
    """The contract probe itself raising is a tooling fault → ERROR (fail
    OPEN), not a candidate REGRESSION."""
    import evolve_admin.safe_upgrade as su
    _ban_dispatch(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(su, "probe_send_surface", boom)
    verdict, detail = soak_probe._probe_send(Path("/tmp"), _NETWORK)
    assert verdict == soak_probe.ERROR
    assert "contract probe raised" in detail


def test_send_kind_dispatches_through_run_silently(monkeypatch):
    """End-to-end through run(): a 'send' kind with an intact contract → OK
    exit (0), with NO operator-channel dispatch — the whole point of the
    silent-success change, exercised through the real dispatch path."""
    import evolve_admin.safe_upgrade as su
    _ban_dispatch(monkeypatch)
    _patch_send_surface(monkeypatch, {"status": su.SEND_SURFACE_OK})
    plan = [{"kind": "send", "target": None}]
    code, results = soak_probe.run(plan, Path("/tmp"), _NETWORK, "canary_bot")
    assert code == 0  # _EXIT[OK]
    assert results[0]["kind"] == "send"
    assert results[0]["verdict"] == soak_probe.OK
