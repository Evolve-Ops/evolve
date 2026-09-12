"""Pause/archive ⇄ unpause/restore of Phase-4.5 scheduled units (audit S4 follow-up).

The uninstall path (test_app_uninstall_scheduled_teardown.py) tears units
DOWN; this covers the reversible half: pause/archive must persistently
DISABLE the launchd/systemd units an app installed (they otherwise keep
firing — OC ``cron/jobs.json`` was the only surface the lifecycle routes
disabled), and unpause/restore must ENABLE them again.

Pinned invariants (mirror the teardown gates):
  - only ``scheduled_unit`` items are acted on (wrapper/heartbeat left alone);
  - the ``ai.evolve.<bot_id>.*`` namespace + per-bot infra-reserve guards are
    re-checked at execute time — a tampered artifact aimed at a system daemon
    or another bot is skipped, never disabled;
  - disable/enable route through the scheduler seam (never a raw path);
  - a seam/enumeration error is caught, never 500s the lifecycle route.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.runtime import FakeScheduler, JobSpec, set_scheduler  # noqa: E402
from evolve_admin.applications.install_helpers import (  # noqa: E402
    set_app_scheduled_units,
    set_scheduled_units_enabled,
)

BOT = "team_bot_a"


@pytest.fixture()
def fake_sched():
    sched = FakeScheduler()
    set_scheduler(sched)
    yield sched
    set_scheduler(None)


def _manifest(actions: list[dict]) -> object:
    # set_app_scheduled_units only reads .scheduled_actions.
    return types.SimpleNamespace(scheduled_actions=actions)


def _seed(sched: FakeScheduler, label: str) -> None:
    sched.seed_job(JobSpec(label=label, program_args=["/bin/echo"],
                           start_interval=900))


def _actions() -> list[dict]:
    return [
        {   # eligible app unit
            "id": "digest",
            "mechanism": "launchd",
            "install": {"plist_label": f"ai.evolve.{BOT}.digest"},
            "installed_artifact":
                f"/Library/LaunchDaemons/ai.evolve.{BOT}.digest.plist",
        },
        {   # heartbeat section — must be ignored by the unit pause path
            "id": "hb",
            "mechanism": "oc_heartbeat_instruction",
            "install": {"file": "HEARTBEAT.md", "section_anchor": "## X",
                        "body": "x"},
            "installed_artifact": "HEARTBEAT.md#X",
        },
        {   # TAMPERED: another bot's unit — namespace gate must skip it
            "id": "evil",
            "mechanism": "launchd",
            "install": {},
            "installed_artifact":
                "/Library/LaunchDaemons/ai.evolve.other_bot.backup.plist",
        },
    ]


def test_pause_disables_only_eligible_units(fake_sched):
    _seed(fake_sched, f"ai.evolve.{BOT}.digest")
    res = set_app_scheduled_units(_manifest(_actions()), BOT, enable=False)
    assert res["ok"] is True

    # The eligible app unit is disabled through the seam...
    assert ("disable", f"ai.evolve.{BOT}.digest") in fake_sched.calls
    assert f"ai.evolve.{BOT}.digest" in fake_sched.disabled
    # ...and NOTHING was enabled, and the cross-bot label was never touched.
    assert not any(c[0] == "enable" for c in fake_sched.calls)
    assert not any(c[1] == "ai.evolve.other_bot.backup" for c in fake_sched.calls)

    unit = [r for r in res["results"] if r["label"] == f"ai.evolve.{BOT}.digest"][0]
    assert unit["status"] == "ok"
    evil = [r for r in res["results"]
            if r["label"] == "ai.evolve.other_bot.backup"][0]
    assert evil["status"] == "skipped"
    assert "namespace" in evil["detail"]


def test_restore_enables_eligible_units(fake_sched):
    label = f"ai.evolve.{BOT}.digest"
    _seed(fake_sched, label)
    set_app_scheduled_units(_manifest(_actions()), BOT, enable=False)
    fake_sched.calls.clear()

    res = set_app_scheduled_units(_manifest(_actions()), BOT, enable=True)
    assert res["ok"] is True
    assert ("enable", label) in fake_sched.calls
    assert label not in fake_sched.disabled


def test_uninstall_after_pause_clears_persistent_override(fake_sched):
    """pause → uninstall must leave NO stale persistent-disable behind.

    launchd's override DB is keyed by label and survives ``remove()`` (the
    plist rm doesn't touch it), so uninstalling a paused/archived app would
    otherwise brick the label: the next install's bootstrap fails with
    "Service is disabled". remove_scheduled_units clears it by calling the
    seam's enable() after a successful remove — with the unit already gone
    that only clears the override (no plist to load / systemd no-op)."""
    from evolve_admin.applications.install_helpers import remove_scheduled_units

    label = f"ai.evolve.{BOT}.digest"
    _seed(fake_sched, label)
    set_app_scheduled_units(_manifest(_actions()), BOT, enable=False)  # pause
    assert label in fake_sched.disabled
    fake_sched.calls.clear()

    items = [{"kind": "scheduled_unit", "action_id": "d", "label": label}]
    res = remove_scheduled_units(BOT, items)
    assert res[0]["status"] == "ok"
    assert ("remove", label) in fake_sched.calls
    assert ("enable", label) in fake_sched.calls, \
        "remove must clear the persistent-disable override"
    assert label not in fake_sched.disabled


def test_failed_remove_does_not_touch_override(fake_sched, monkeypatch):
    # A failed remove keeps the unit as the resumable checklist — the
    # paused state (override) must survive with it.
    from evolve_admin.applications.install_helpers import remove_scheduled_units

    label = f"ai.evolve.{BOT}.digest"
    _seed(fake_sched, label)
    set_app_scheduled_units(_manifest(_actions()), BOT, enable=False)  # pause

    def _boom(self, lab, *, timeout=None):
        return False, "plist delete failed: EPERM"

    monkeypatch.setattr(FakeScheduler, "remove", _boom)
    items = [{"kind": "scheduled_unit", "action_id": "d", "label": label}]
    res = remove_scheduled_units(BOT, items)
    assert res[0]["status"] == "failed"
    assert not any(c[0] == "enable" for c in fake_sched.calls)
    assert label in fake_sched.disabled


def test_infra_reserve_label_is_skipped(fake_sched, monkeypatch):
    # A per-bot Evolve infra daemon shares the ai.evolve.<bot>.* namespace but
    # must never be paused by an app lifecycle change.
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.per_bot_evolve_plist_labels",
        lambda bot_id: [f"ai.evolve.{BOT}.backup"],
    )
    items = [{"kind": "scheduled_unit", "action_id": "a",
              "label": f"ai.evolve.{BOT}.backup"}]
    res = set_scheduled_units_enabled(BOT, items, enable=False)
    assert res[0]["status"] == "skipped"
    assert "infra" in res[0]["detail"]
    assert fake_sched.calls == [], "infra label must never reach the seam"


def test_no_scheduled_actions_is_ok_noop(fake_sched):
    res = set_app_scheduled_units(_manifest([]), BOT, enable=False)
    assert res == {"ok": True, "results": []}
    assert fake_sched.calls == []


def test_seam_failure_is_surfaced_not_raised(fake_sched, monkeypatch):
    _seed(fake_sched, f"ai.evolve.{BOT}.digest")

    def _boom(self, label, *, timeout=None):
        return False, "systemctl disable --now failed (rc=1): boom"

    monkeypatch.setattr(FakeScheduler, "disable", _boom)
    res = set_app_scheduled_units(_manifest(_actions()), BOT, enable=False)
    assert res["ok"] is False, "a failed unit disable makes the batch not-ok"
    unit = [r for r in res["results"] if r["label"] == f"ai.evolve.{BOT}.digest"][0]
    assert unit["status"] == "failed"
    assert "boom" in unit["detail"]


def test_route_pause_disables_then_unpause_enables_through_seam(
    tmp_path, monkeypatch, fake_sched,
):
    """End-to-end wiring: POST /pause routes through _app_lifecycle into the
    seam's disable; /unpause re-enables. Proves the server.py hookup, not just
    the helper."""
    import json

    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    workspace = tmp_path / "bot-homes" / BOT / ".openclaw" / "workspace"
    manifests = workspace / "manifests"
    manifests.mkdir(parents=True)

    network = {"networkId": "pod-test-1", "sharedDir": str(shared_dir),
               "bots": {BOT: {"user": BOT}}, "members": [BOT]}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web import server as srv
    monkeypatch.setattr(
        srv, "resolve_bot_paths",
        lambda bot_id, user=None: {"workspace": str(workspace), "user": user or bot_id},
    )
    import evolve_admin.config as cfg
    import evolve_admin.applications.app_registry as app_registry
    monkeypatch.setattr(cfg, "get_bot_workspace", lambda bot_id, user=None: workspace)
    monkeypatch.setattr(app_registry, "get_bot_workspace", lambda bot_id, user=None: workspace)

    label = f"ai.evolve.{BOT}.digest"
    _seed(fake_sched, label)
    (manifests / "digest.json").write_text(json.dumps({
        "id": "digest", "name": "Digest", "bot_id": BOT, "status": "active",
        "scheduled_actions": [{
            "id": "d", "mechanism": "launchd",
            "install": {"plist_label": label},
            "installed_artifact": f"/Library/LaunchDaemons/{label}.plist",
        }],
    }))

    app = create_app(network_path)
    app.config["TESTING"] = True
    client = app.test_client()

    r = client.post(f"/api/applications/{BOT}/digest/pause")
    assert r.status_code == 200
    body = r.get_json()
    assert body["scheduled_units"]["ok"] is True
    assert ("disable", label) in fake_sched.calls
    assert label in fake_sched.disabled

    r = client.post(f"/api/applications/{BOT}/digest/unpause")
    assert r.status_code == 200
    assert ("enable", label) in fake_sched.calls
    assert label not in fake_sched.disabled


def test_enumeration_error_is_caught(fake_sched, monkeypatch):
    # A malformed manifest that makes derive_scheduled_teardown throw must be
    # caught inside the helper — the lifecycle route never 500s on this.
    def _explode(actions, bot_id):
        raise RuntimeError("bad manifest")

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.derive_scheduled_teardown",
        _explode,
    )
    res = set_app_scheduled_units(_manifest(_actions()), BOT, enable=False)
    assert res["ok"] is False
    assert "bad manifest" in res["error"]
