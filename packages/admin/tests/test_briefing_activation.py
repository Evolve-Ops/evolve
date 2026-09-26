"""tests/test_briefing_activation.py — auto-activate-later (U1 fix).

2026-06-11 design sync decision (M4 finding 4 / §The 07:00 window):
offer-now, auto-activate-later. These tests cover the activate-later
half plus the loud post-wrap install outcomes:

  * ``briefing_activation.maybe_activate`` — the pure-Python check that
    queues the briefing install (through the normal gallery path) when
    a bot with a recorded briefing decision gains its first messaging
    channel; find-or-skip on every other state.
  * ``on_channels_registered`` — the zero→some transition filter the
    channel-registration chokepoint calls.
  * ``pack_driver`` outcome notifications — a job that fails after the
    wrap pushes ``system.app_install_failed``; a completed activation
    install pushes ``decisions.briefing_activated``.
  * The forge approval path's connected-channel declaration — the
    deterministic stamp that lets C-A4 verify real channel state.

All machinery seam-injected (no real bot homes, no sudo, no forge).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


_PKG_BRIEFING = "p-a9a74bf7"
_PKG_CALENDAR = "p-fe9acef3"


@pytest.fixture(autouse=True)
def seams():
    """Activation + pack-driver machinery seam-injected, reset after.
    Defaults: no manifests installed, telegram connected, install runner
    marks every job complete, dispatch recorded."""
    from evolve_admin import briefing_activation as ba
    from evolve_admin.applications.forge_jobs import load_job, save_job
    from evolve_admin.evo.wizard import pack_driver as pkd

    sent: list[dict] = []

    def runner(shared_dir, job_id, bot_id):
        job = load_job(job_id, shared_dir)
        job.status = "complete"
        save_job(job, shared_dir)

    ba.set_manifest_loader(lambda app_id, bot_id, shared_dir: None)
    ba.set_channels_reader(lambda bot_id: {"telegram"})
    pkd.set_force_sync(True)
    pkd.set_install_runner(runner)
    pkd.set_outcome_dispatch(lambda **kw: sent.append(kw))
    yield sent
    ba.set_manifest_loader(None)
    ba.set_channels_reader(None)
    pkd.set_force_sync(False)
    pkd.set_install_runner(None)
    pkd.set_outcome_dispatch(None)


def _network(*, briefing=None, purpose=None, primary=None):
    block = {}
    if briefing is not None:
        block["briefing"] = briefing
    if purpose is not None:
        block["purpose"] = purpose
    if primary is not None:
        block["primary_user"] = primary
    return {"members": ["ledgerbot"], "bots": {"ledgerbot": block}}


def _activate(tmp_path, network):
    from evolve_admin import briefing_activation as ba

    return ba.maybe_activate(
        "ledgerbot", shared_dir=tmp_path, network=network,
    )


# ─────────────────────────────────────────────────────────────────────────────
# maybe_activate — find-or-skip semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_no_recorded_decision_is_a_noop(tmp_path):
    assert _activate(tmp_path, _network()) == "not_decided"


def test_recorded_decline_is_honored(tmp_path):
    net = _network(briefing={"enabled": False, "decided_at": "x"})
    assert _activate(tmp_path, net) == "briefing_off"


def test_already_installed_is_a_noop(tmp_path):
    from evolve_admin import briefing_activation as ba

    ba.set_manifest_loader(
        lambda app_id, bot_id, shared_dir:
        object() if app_id == "morning-briefing" else None,
    )
    net = _network(briefing={"enabled": True, "time": "07:00"})
    assert _activate(tmp_path, net) == "already_installed"


def test_stranded_updating_manifest_does_not_block_activation(
    tmp_path, seams,
):
    """The Ledger state after the M4 run: the gate-refused install left
    the seeded manifest at status "updating" and a terminal failed job.
    Activation must treat that as not-installed and retry (the forge
    re-install path handles an existing manifest)."""
    from evolve_admin import briefing_activation as ba

    class _Stranded:
        status = "updating"

    ba.set_manifest_loader(
        lambda app_id, bot_id, shared_dir:
        _Stranded() if app_id == "morning-briefing" else None,
    )
    net = _network(briefing={"enabled": True, "time": "07:00"})
    assert _activate(tmp_path, net) == "queued"


def test_no_channel_waits_for_the_next_connect(tmp_path):
    from evolve_admin import briefing_activation as ba

    ba.set_channels_reader(lambda bot_id: set())
    net = _network(briefing={"enabled": True, "time": "07:00"})
    assert _activate(tmp_path, net) == "no_channel"


def test_activation_queues_briefing_with_calendar_foundation(tmp_path, seams):
    """The happy path: install queued through the normal gallery path
    (calendar foundation first), the mission + time seeded into the
    build spec, and the completed briefing pushes the activation
    receipt with the recipient caveat (no primary recorded)."""
    from evolve_admin.applications.forge_jobs import list_jobs_for_app

    net = _network(
        briefing={"enabled": True, "time": "07:00", "decided_at": "x"},
        purpose={"mission": "Keep the launch ledger."},
    )
    assert _activate(tmp_path, net) == "queued"

    briefing_jobs = list_jobs_for_app(_PKG_BRIEFING, "ledgerbot", tmp_path)
    assert len(briefing_jobs) == 1
    calendar_jobs = list_jobs_for_app(_PKG_CALENDAR, "ledgerbot", tmp_path)
    assert len(calendar_jobs) == 1
    spec = briefing_jobs[0].context_snapshot["build_spec"]
    assert "Operator-provided settings" in spec
    assert "mission: Keep the launch ledger." in spec
    assert "delivery_time: 07:00" in spec

    # The completed briefing install pushed the activation receipt.
    receipts = [s for s in seams
                if s.get("catalog_event") == "decisions.briefing_activated"]
    assert len(receipts) == 1
    payload = receipts[0]["payload"]
    assert payload["bot_id"] == "ledgerbot"
    assert payload["time"] == "07:00"
    assert "Set primary user" in payload["route_note"]
    # No failure events on the happy path.
    assert not [s for s in seams
                if s.get("catalog_event") == "system.app_install_failed"]


def test_activation_skips_calendar_when_already_installed(tmp_path, seams):
    from evolve_admin import briefing_activation as ba
    from evolve_admin.applications.forge_jobs import list_jobs_for_app

    ba.set_manifest_loader(
        lambda app_id, bot_id, shared_dir:
        object() if app_id == "calendar-sync" else None,
    )
    net = _network(briefing={"enabled": True, "time": "07:00"})
    assert _activate(tmp_path, net) == "queued"
    assert list_jobs_for_app(_PKG_CALENDAR, "ledgerbot", tmp_path) == []
    assert len(list_jobs_for_app(_PKG_BRIEFING, "ledgerbot", tmp_path)) == 1


def test_activation_receipt_omits_caveat_when_recipient_recorded(
    tmp_path, seams,
):
    net = _network(
        briefing={"enabled": True, "time": "07:00"},
        primary={"external_ids": {"telegram": "9999"}},
    )
    assert _activate(tmp_path, net) == "queued"
    receipts = [s for s in seams
                if s.get("catalog_event") == "decisions.briefing_activated"]
    assert receipts[0]["payload"]["route_note"] == ""


def test_second_call_sees_in_flight_install(tmp_path, seams):
    """Re-firing the hook (second channel, re-applied config) never
    double-queues: the active job is the dedup."""
    from evolve_admin.evo.wizard import pack_driver as pkd

    pkd.set_install_runner(lambda shared_dir, job_id, bot_id: None)  # stays queued
    net = _network(briefing={"enabled": True, "time": "07:00"})
    assert _activate(tmp_path, net) == "queued"
    assert _activate(tmp_path, net) == "install_in_flight"


# ─────────────────────────────────────────────────────────────────────────────
# on_channels_registered — the zero→some transition filter
# ─────────────────────────────────────────────────────────────────────────────


def test_hook_fires_only_on_first_channel(monkeypatch, tmp_path):
    from evolve_admin import briefing_activation as ba

    calls: list[str] = []
    monkeypatch.setattr(
        ba, "maybe_activate",
        lambda bot_id, *, shared_dir, network=None: calls.append(bot_id),
    )
    ba.on_channels_registered("ledgerbot", before=set(), after={"telegram"})
    assert calls == ["ledgerbot"]
    ba.on_channels_registered(
        "ledgerbot", before={"telegram"}, after={"telegram", "slack"},
    )
    ba.on_channels_registered("ledgerbot", before=set(), after=set())
    assert calls == ["ledgerbot"]  # no re-fire


def test_hook_never_raises(monkeypatch):
    from evolve_admin import briefing_activation as ba

    def boom(bot_id, *, shared_dir, network=None):
        raise RuntimeError("activation exploded")

    monkeypatch.setattr(ba, "maybe_activate", boom)
    ba.on_channels_registered("ledgerbot", before=set(), after={"telegram"})


def test_write_oc_config_wires_the_hook(monkeypatch, tmp_path):
    """The channel-registration chokepoint detects the zero→some
    transition and hands it to the (seam-injected) hook — and a hook
    crash never fails the write."""
    from evolve_admin.skills import _oc_install_common as oc

    fired: list[tuple] = []
    monkeypatch.setattr(
        oc, "read_oc_config", lambda bot_id: ({"channels": {}}, None),
    )
    monkeypatch.setattr(
        oc, "bot_oc_json_path", lambda bot_id: tmp_path / "openclaw.json",
    )

    class _R:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        oc.subprocess, "run", lambda *a, **kw: _R(), raising=True,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network", lambda *a, **kw: {"members": []},
    )
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_user", lambda bot_id, network: bot_id,
    )
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot_id, network=None: tmp_path,
    )
    oc.set_channel_connect_hook(
        lambda bot_id, *, before, after: fired.append((bot_id, before, after)),
    )
    try:
        ok, err = oc.write_oc_config(
            "ledgerbot",
            {"channels": {"telegram": {"enabled": True, "botToken": "x"}}},
        )
        assert ok, err
        assert fired == [("ledgerbot", set(), {"telegram"})]

        # A hook crash is contained — the write still reports success.
        def explode(bot_id, *, before, after):
            raise RuntimeError("hook exploded")
        oc.set_channel_connect_hook(explode)
        ok, err = oc.write_oc_config(
            "ledgerbot",
            {"channels": {"telegram": {"enabled": True, "botToken": "x"}}},
        )
        assert ok, err
    finally:
        oc.set_channel_connect_hook(None)


# ─────────────────────────────────────────────────────────────────────────────
# pack_driver — loud post-wrap outcomes
# ─────────────────────────────────────────────────────────────────────────────


def test_failed_install_job_notifies_operator(tmp_path, seams):
    """A job that fails mid-build after the wrap pushes the
    app_install_failed event — the C-A4 channel-gap refusal rendered in
    operator words with the self-healing promise."""
    from evolve_admin.applications.forge_jobs import load_job, save_job
    from evolve_admin.evo.wizard import pack_driver as pkd

    def failing_runner(shared_dir, job_id, bot_id):
        job = load_job(job_id, shared_dir)
        job.status = "failed"
        job.steps[-1].status = "failed"
        job.steps[-1].detail = (
            "coherence gate refused: scheduled_action 'morning-briefing' "
            "declares a messaging output but requirements.integrations[] "
            "contains no messaging-capable entry"
        )
        save_job(job, shared_dir)

    pkd.set_install_runner(failing_runner)
    queued = pkd.queue_pack_installs(
        tmp_path, bot_id="ledgerbot",
        apps=[{"pkg_id": _PKG_BRIEFING, "name": "Morning Briefing"}],
        network={"members": []},
    )
    assert queued[0].ok
    failures = [s for s in seams
                if s.get("catalog_event") == "system.app_install_failed"]
    assert len(failures) == 1
    payload = failures[0]["payload"]
    assert payload["bot_id"] == "ledgerbot"
    assert payload["app_name"] == "Morning Briefing"
    assert "no place to send messages yet" in payload["reason"]
    assert "sets itself up" in payload["reason"]


def test_runner_crash_notifies_operator(tmp_path, seams):
    from evolve_admin.evo.wizard import pack_driver as pkd

    def crashing_runner(shared_dir, job_id, bot_id):
        raise RuntimeError("forge fell over")

    pkd.set_install_runner(crashing_runner)
    pkd.queue_pack_installs(
        tmp_path, bot_id="ledgerbot",
        apps=[{"pkg_id": _PKG_CALENDAR, "name": "Calendar Sync"}],
        network={"members": []},
    )
    failures = [s for s in seams
                if s.get("catalog_event") == "system.app_install_failed"]
    assert len(failures) == 1
    assert "forge fell over" in failures[0]["payload"]["reason"]


def test_completed_install_without_announce_stays_quiet(tmp_path, seams):
    """The wizard-finalize path: completed installs are reported by the
    wrap, not by an extra push — only failures are events."""
    from evolve_admin.evo.wizard import pack_driver as pkd

    pkd.queue_pack_installs(
        tmp_path, bot_id="ledgerbot",
        apps=[{"pkg_id": _PKG_CALENDAR, "name": "Calendar Sync"}],
        network={"members": []},
    )
    assert seams == []


# ─────────────────────────────────────────────────────────────────────────────
# Forge approval — connected-channel declaration (the C-A4 stamp)
# ─────────────────────────────────────────────────────────────────────────────


def _messaging_manifest_kwargs():
    return dict(
        id="morning-briefing",
        name="Morning Briefing",
        bot_id="ledgerbot",
        scheduled_actions=[{
            "id": "morning-briefing",
            "state": "active",
            "outputs": [{"kind": "message", "target": "telegram dm"}],
        }],
    )


def test_stamp_declares_connected_channels(monkeypatch, tmp_path):
    from evolve_admin import channels as ch
    from evolve_admin.applications import forge_engine as fe
    from evolve_admin.applications.manifest import ApplicationManifest

    monkeypatch.setattr(
        ch, "enabled_messaging_channels",
        lambda bot_id, **kw: {"telegram"},
    )
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    manifest = ApplicationManifest(**_messaging_manifest_kwargs())
    stamped = fe._stamp_connected_messaging_channels(
        manifest, "ledgerbot", tmp_path,
    )
    assert stamped == ["telegram"]
    entries = manifest.requirements["integrations"]
    # Dict shape — bare strings get dropped by the v7-arc translation.
    assert [e["id"] for e in entries] == ["telegram"]
    assert all(e["required"] for e in entries)
    assert saved  # persisted

    # Now coherent: C-A4 passes on the stamped manifest.
    from dataclasses import asdict
    from evolve_admin.applications.coherence_pass_a import (
        check_c_a4_messaging_output_needs_integration,
    )
    assert check_c_a4_messaging_output_needs_integration(asdict(manifest)) == []


def test_stamp_leaves_channel_less_bot_refused(monkeypatch, tmp_path):
    """No connected channel → no stamp — the C-A4 refusal stands. The
    refusal is correct; the silence around it was the bug."""
    from evolve_admin import channels as ch
    from evolve_admin.applications import forge_engine as fe
    from evolve_admin.applications.manifest import ApplicationManifest

    monkeypatch.setattr(
        ch, "enabled_messaging_channels", lambda bot_id, **kw: set(),
    )
    manifest = ApplicationManifest(**_messaging_manifest_kwargs())
    stamped = fe._stamp_connected_messaging_channels(
        manifest, "ledgerbot", tmp_path,
    )
    assert stamped == []
    from dataclasses import asdict
    from evolve_admin.applications.coherence_pass_a import (
        check_c_a4_messaging_output_needs_integration,
    )
    findings = check_c_a4_messaging_output_needs_integration(asdict(manifest))
    assert findings and findings[0].severity == "critical"


def test_stamp_noop_for_non_messaging_manifest(monkeypatch, tmp_path):
    from evolve_admin import channels as ch
    from evolve_admin.applications import forge_engine as fe
    from evolve_admin.applications.manifest import ApplicationManifest

    monkeypatch.setattr(
        ch, "enabled_messaging_channels",
        lambda bot_id, **kw: {"telegram"},
    )
    manifest = ApplicationManifest(
        id="note-taker", name="Note Taker", bot_id="ledgerbot",
    )
    assert fe._stamp_connected_messaging_channels(
        manifest, "ledgerbot", tmp_path,
    ) == []
    assert manifest.requirements == {}


# ─────────────────────────────────────────────────────────────────────────────
# channels.py — config readers
# ─────────────────────────────────────────────────────────────────────────────


def test_enabled_messaging_channels_from_config():
    from evolve_admin.channels import (
        enabled_channels_from_config,
        enabled_messaging_channels_from_config,
    )

    cfg = {"channels": {
        "telegram": {"enabled": True, "botToken": "x"},
        "slack": {"enabled": False},
        "webhook": {"enabled": True},   # not messaging-capable per C-A4
    }}
    assert enabled_channels_from_config(cfg) == {"telegram", "webhook"}
    assert enabled_messaging_channels_from_config(cfg) == {"telegram"}
    assert enabled_messaging_channels_from_config(None) == set()
    assert enabled_messaging_channels_from_config({}) == set()


# ─────────────────────────────────────────────────────────────────────────────
# Forge approval — delivery-contract stamp (the proactive-delivery monitor,
# U1 re-proof 2026-06-12). Sibling to the C-A4 channel stamp above: that one
# declares the integration the gate needs; this one declares the delivery the
# monitor needs.
# ─────────────────────────────────────────────────────────────────────────────


_BRIEFING_SPEC_ACTION = {
    "id": "morning-briefing",
    "mechanism": "launchd",
    "outputs": [{"kind": "session_message", "channel": "primary"}],
    "delivery_contract": {
        "user_facing": True,
        "window_minutes": 30,
        "evidence": {
            "ran": {"kind": "scheduler_state"},
            "delivered": {
                "kind": "run_file",
                "path": "memory/briefing-runs/{date}.json",
            },
        },
        "heal": "rerun",
    },
}


def _write_builtin_spec(shared_dir, pkg_id, scheduled_actions,
                        version="2026.05.20-1.0"):
    """Seed a bound Spec the approval stamp resolves via find_existing_spec."""
    import json

    spec_dir = shared_dir / "gallery" / "builtin" / pkg_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"{version}.json").write_text(json.dumps({
        "spec_id": pkg_id,
        "spec_version": version,
        "scheduled_actions": scheduled_actions,
    }))


def test_delivery_stamp_makes_extracted_briefing_monitored(monkeypatch, tmp_path):
    """The ledger U1 gap (2026-06-12): the realized briefing manifest is
    extracted from the workspace with ``outputs:[]`` and no
    ``delivery_contract``, so delivery_monitor's ``_derived_user_facing()``
    skips it — a silent delivery failure. With the bound Spec declaring a
    user-facing delivery, the approval stamp re-asserts it from live channel
    state and the monitor then sees the briefing."""
    import delivery_monitor as dm
    from evolve_admin import channels as ch
    from evolve_admin.applications import forge_engine as fe
    from evolve_admin.applications.manifest import ApplicationManifest

    monkeypatch.setattr(
        ch, "enabled_messaging_channels", lambda bot_id, **kw: {"telegram"},
    )
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    _write_builtin_spec(tmp_path, _PKG_BRIEFING, [_BRIEFING_SPEC_ACTION])

    # Ledger-shaped realized action: extracted, no outputs, no contract.
    manifest = ApplicationManifest(
        id="morning-briefing", name="Morning Briefing", bot_id="ledgerbot",
        pkg_id=_PKG_BRIEFING,
        scheduled_actions=[{
            "id": "morning-briefing", "mechanism": "launchd",
            "state": "active", "quality": "extracted",
        }],
    )
    action = manifest.scheduled_actions[0]
    # Before: the monitor classifies it as NOT user-facing → unmonitored.
    assert dm.effective_contract({}, action).user_facing is False

    stamped = fe._stamp_scheduled_delivery_contracts(
        manifest, "ledgerbot", tmp_path,
    )
    assert stamped == ["morning-briefing"]
    assert saved  # persisted

    # outputs[] now carries the bot's live channel...
    assert any(o.get("channel") == "telegram" for o in action["outputs"])
    # ...and the delivery_contract (reused from the Spec) marks it user-facing.
    assert action["delivery_contract"]["user_facing"] is True
    assert (
        action["delivery_contract"]["evidence"]["delivered"]["kind"]
        == "run_file"
    )
    # After: the monitor now treats the briefing as a monitored delivery.
    assert dm.effective_contract({}, action).user_facing is True


def test_delivery_stamp_noop_without_live_channel(monkeypatch, tmp_path):
    """No connected channel → nothing routable yet → leave the manifest
    as-authored (briefing_activation reinstalls on the first connect)."""
    from evolve_admin import channels as ch
    from evolve_admin.applications import forge_engine as fe
    from evolve_admin.applications.manifest import ApplicationManifest

    monkeypatch.setattr(
        ch, "enabled_messaging_channels", lambda bot_id, **kw: set(),
    )
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    _write_builtin_spec(tmp_path, _PKG_BRIEFING, [_BRIEFING_SPEC_ACTION])
    manifest = ApplicationManifest(
        id="morning-briefing", name="Morning Briefing", bot_id="ledgerbot",
        pkg_id=_PKG_BRIEFING,
        scheduled_actions=[{
            "id": "morning-briefing", "mechanism": "launchd", "state": "active",
        }],
    )
    assert fe._stamp_scheduled_delivery_contracts(
        manifest, "ledgerbot", tmp_path,
    ) == []
    assert manifest.scheduled_actions[0].get("outputs") in (None, [])
    assert "delivery_contract" not in manifest.scheduled_actions[0]
    assert saved == []


def test_delivery_stamp_skips_non_delivery_action(monkeypatch, tmp_path):
    """A scheduled action that isn't a user-facing delivery (the Spec
    declares a data-file output, not a message) is left untouched — the
    stamp never invents a delivery."""
    from evolve_admin import channels as ch
    from evolve_admin.applications import forge_engine as fe
    from evolve_admin.applications.manifest import ApplicationManifest

    monkeypatch.setattr(
        ch, "enabled_messaging_channels", lambda bot_id, **kw: {"telegram"},
    )
    saved: list = []
    monkeypatch.setattr(fe, "save_manifest", lambda m, sd: saved.append(m))
    _write_builtin_spec(tmp_path, _PKG_CALENDAR, [{
        "id": "calendar-sync", "mechanism": "launchd",
        "outputs": [{"kind": "data_file", "path": "memory/calendar-today.json"}],
    }])
    manifest = ApplicationManifest(
        id="calendar-sync", name="Calendar Sync", bot_id="ledgerbot",
        pkg_id=_PKG_CALENDAR,
        scheduled_actions=[{
            "id": "calendar-sync", "mechanism": "launchd", "state": "active",
        }],
    )
    assert fe._stamp_scheduled_delivery_contracts(
        manifest, "ledgerbot", tmp_path,
    ) == []
    assert saved == []
