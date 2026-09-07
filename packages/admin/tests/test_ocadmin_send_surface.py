"""Post-upgrade send-surface verification (Wave-5 of the 2026-06-11 P0).

OpenClaw 2026.6.1 removed the delivery surface every gallery app sends
through, silently, for 8 days. `evolve-admin menu upgrade` now re-probes
the `openclaw message send` contract right after the install and proves
it end to end with one real operator-visible message. This file covers:

1. ``_verify_send_surface_post_upgrade`` outcome mapping — broken
   contract → pod-scope alert Signal; unverified → no Signal (honest
   "couldn't verify", not a false alarm); contract ok → one canary send
   via the dispatcher; canary FAILED → Signal; canary not-performed
   (no recipient / suppressed) → no Signal.
2. The Signal's operator copy passes the Plex test at the headline.
3. signal_notifier routing for the two new types, and the deny-list
   guard — neither producer may join ``_DIRECT_DISPATCH_PRODUCERS``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import ocadmin  # noqa: E402
from evolve_admin import safe_upgrade as su  # noqa: E402
from evolve_admin.alerts import dispatcher as _dispatcher  # noqa: E402
from evolve_admin.alerts import signal_notifier as sn  # noqa: E402
from signals import store as signals_store  # noqa: E402


_NETWORK = {"members": [], "bots": {}}


def _probe(status: str, reason: str | None = None) -> dict:
    return {
        "status": status,
        "cli_path": "/opt/x/bin/openclaw",
        "required_flags": list(su.SEND_SURFACE_REQUIRED_FLAGS),
        "missing_flags": [],
        "reason": reason,
        "rc": None,
        "output_excerpt": None,
    }


def _outcome(result: _dispatcher.DispatchResult, error: str | None = None):
    return _dispatcher.DispatchOutcome(
        result=result,
        source=ocadmin.SEND_SURFACE_PROBE_PRODUCER,
        severity=_dispatcher.Severity.INFO,
        dedup_key=None,
        channel="telegram" if result == _dispatcher.DispatchResult.SENT else None,
        error=error,
    )


def _pod_signals(shared_dir: Path) -> list:
    return list(signals_store.iter_active(
        shared_dir, producer=ocadmin.SEND_SURFACE_PROBE_PRODUCER,
    ))


@pytest.fixture()
def send_calls(monkeypatch):
    """Capture dispatcher.send calls; default outcome SENT."""
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        return _outcome(_dispatcher.DispatchResult.SENT)

    monkeypatch.setattr(_dispatcher, "send", fake_send)
    return calls


def test_broken_contract_fires_pod_alert_and_skips_canary(
    tmp_path, monkeypatch, send_calls,
):
    monkeypatch.setattr(
        su, "probe_send_surface", lambda **kw: _probe("failed", "cli_not_found"),
    )
    ocadmin._verify_send_surface_post_upgrade(
        "2026.5.20", "2026.6.1", _NETWORK, shared_dir=tmp_path,
    )
    sigs = _pod_signals(tmp_path)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == ocadmin.SEND_SURFACE_BROKEN_TYPE
    assert sig.scope == "pod"
    assert sig.severity == "alert"
    assert sig.details["stage"] == "contract_probe"
    assert sig.details["new_version"] == "2026.6.1"
    # No end-to-end attempt on a surface already proven broken.
    assert send_calls == []


def test_signal_headline_passes_plex_test(tmp_path, monkeypatch, send_calls):
    """Primary copy names the user impact in plain words — no endpoint,
    CLI, or subcommand jargon."""
    monkeypatch.setattr(
        su, "probe_send_surface",
        lambda **kw: _probe("failed", "contract_flags_missing"),
    )
    ocadmin._verify_send_surface_post_upgrade(
        "2026.5.20", "2026.6.1", _NETWORK, shared_dir=tmp_path,
    )
    sig = _pod_signals(tmp_path)[0]
    assert sig.title == "Messages from your bots may not be getting through"
    for jargon in ("/api/", "endpoint", "CLI", "subcommand", "--channel"):
        assert jargon not in sig.title
    assert "OpenClaw update" in sig.body


def test_unverified_probe_is_reported_not_alarmed(
    tmp_path, monkeypatch, send_calls,
):
    """Tri-state honesty: "couldn't verify" is neither a pass nor a
    pod-wide alarm — and no canary send is attempted on an unknown
    surface."""
    monkeypatch.setattr(
        su, "probe_send_surface", lambda **kw: _probe("unverified", "probe_timeout"),
    )
    ocadmin._verify_send_surface_post_upgrade(
        "2026.5.20", "2026.6.1", _NETWORK, shared_dir=tmp_path,
    )
    assert _pod_signals(tmp_path) == []
    assert send_calls == []


def test_ok_contract_sends_one_canary_message(tmp_path, monkeypatch, send_calls):
    monkeypatch.setattr(su, "probe_send_surface", lambda **kw: _probe("ok"))
    ocadmin._verify_send_surface_post_upgrade(
        "2026.5.20", "2026.6.1", _NETWORK, shared_dir=tmp_path,
    )
    assert _pod_signals(tmp_path) == []
    assert len(send_calls) == 1
    call = send_calls[0]
    assert call["source"] == ocadmin.SEND_SURFACE_PROBE_PRODUCER
    assert "2026.6.1" in call["message"]
    assert call["shared_dir"] == tmp_path


def test_failed_canary_send_fires_pod_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "probe_send_surface", lambda **kw: _probe("ok"))
    monkeypatch.setattr(
        _dispatcher, "send",
        lambda **kw: _outcome(
            _dispatcher.DispatchResult.FAILED, error="device not paired",
        ),
    )
    ocadmin._verify_send_surface_post_upgrade(
        "2026.5.20", "2026.6.1", _NETWORK, shared_dir=tmp_path,
    )
    sigs = _pod_signals(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].details["stage"] == "end_to_end_send"
    assert sigs[0].details["probe"]["end_to_end_error"] == "device not paired"


def test_unperformed_canary_is_not_a_failure(tmp_path, monkeypatch):
    """NO_RECIPIENT (and suppressed/deferred) means "couldn't verify end
    to end", not "broken" — no alarm on a pod with no alert channel."""
    monkeypatch.setattr(su, "probe_send_surface", lambda **kw: _probe("ok"))
    monkeypatch.setattr(
        _dispatcher, "send",
        lambda **kw: _outcome(_dispatcher.DispatchResult.NO_RECIPIENT),
    )
    ocadmin._verify_send_surface_post_upgrade(
        "2026.5.20", "2026.6.1", _NETWORK, shared_dir=tmp_path,
    )
    assert _pod_signals(tmp_path) == []


# ── signal_notifier routing + deny-list guard ───────────────────────────────

def _sig(producer: str, sig_type: str) -> SimpleNamespace:
    return SimpleNamespace(producer=producer, type=sig_type)


def test_send_surface_broken_routes_to_its_catalog_event() -> None:
    assert sn._catalog_event_for_signal(
        _sig("send_surface_probe", "send_surface_broken"),
    ) == "system.send_surface_broken"


def test_pod_delivery_regression_routes_before_per_app_catchall() -> None:
    assert sn._catalog_event_for_signal(
        _sig("delivery_monitor", "pod_delivery_regression"),
    ) == "system.pod_delivery_regression"
    # The per-app types keep their existing routes.
    assert sn._catalog_event_for_signal(
        _sig("delivery_monitor", "app_delivery_missed"),
    ) == "system.app_delivery_missed"
    assert sn._catalog_event_for_signal(
        _sig("delivery_monitor", "app_delivery_unmeasurable"),
    ) == "system.app_delivery_unmeasurable"


def test_producers_stay_off_the_deny_list() -> None:
    """Both producers emit via signals.store.observe(); under deny-list-
    by-default they are loud automatically. Joining
    _DIRECT_DISPATCH_PRODUCERS would silence them. (The dispatcher
    *source* named send_surface_probe is the success-path canary message
    — a different layer; it must not drag the producer onto the list.)"""
    assert "send_surface_probe" not in sn._DIRECT_DISPATCH_PRODUCERS
    assert "delivery_monitor" not in sn._DIRECT_DISPATCH_PRODUCERS
