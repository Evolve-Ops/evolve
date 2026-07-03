"""Uninstall teardown of Phase-4.5 scheduled artifacts (audit S4).

Regression for the gallery-framework audit finding
(docs/audit-gallery-framework-2026-07-02.md §2 S4): the uninstall stack
never read ``scheduled_actions[].installed_artifact``, so launchd plists
(and, since #3399, systemd units) stayed bootstrapped after a full
uninstall and kept firing against the deleted scripts, the
HEARTBEAT.md/AGENTS.md managed sections survived, and INSTALLED_APPS.md
kept a stale Tier-1 menu entry.

Pinned invariants:
  - ``plan_manifest_deletion`` enumerates unit labels (both platform
    artifact shapes), python-signal wrapper files, and heartbeat sections;
  - execution goes through the scheduler seam's ``remove`` and is gated to
    the ``ai.evolve.<bot_id>.*`` label namespace — a tampered artifact
    pointing at a system daemon or another bot's unit is skipped, never
    removed;
  - wrapper unlink is confined to ``{workspace}/evolve/scheduled/``;
  - a failed unit removal aborts the uninstall BEFORE any file unlink
    (manifest + files stay as the resumable checklist);
  - finalize regenerates INSTALLED_APPS.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.runtime import FakeScheduler, JobSpec, set_scheduler  # noqa: E402


BOT = "team_bot_a"
APP = "task-manager"
PKG = "p-aaaa1111"


def _seed_manifest(workspace: Path, scheduled_actions: list[dict],
                   files: dict[str, str] | None = None) -> Path:
    files = files if files is not None else {"scripts/tasks.py": "script"}
    manifests_dir = workspace / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": APP,
        "name": "Task Manager",
        "bot_id": BOT,
        "pkg_id": PKG,
        "status": "active",
        "files": [
            {"file_id": f"f-{i:04x}", "path": p, "layer": layer,
             "owned_by": PKG, "shared_with": []}
            for i, (p, layer) in enumerate(files.items())
        ],
        "scheduled_actions": scheduled_actions,
    }
    mp = manifests_dir / f"{APP}.json"
    mp.write_text(json.dumps(manifest))
    for rel in files:
        f = workspace / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# stub for {rel}\n")
    return mp


@pytest.fixture()
def fake_sched():
    sched = FakeScheduler()
    set_scheduler(sched)
    yield sched
    set_scheduler(None)


@pytest.fixture()
def env(tmp_path, monkeypatch, fake_sched):
    """Shared harness: tmp workspace + shared dir, seams redirected."""
    home = tmp_path / "home"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()

    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id, user=None: workspace,
    )
    # app_registry (finalize's regen) binds get_bot_workspace at its own
    # import, so the config-module patch above only reaches it when
    # app_registry is imported AFTER this fixture runs. Patch the bound
    # name directly so the regen resolves the tmp workspace regardless of
    # test collection order (the CI-shard flake, 2026-07-03).
    monkeypatch.setattr(
        "evolve_admin.applications.app_registry.get_bot_workspace",
        lambda bot_id, user=None: workspace,
        raising=False,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.get_bot_user",
        lambda bot_id, network: BOT,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.load_network",
        lambda: {},
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.user_home",
        lambda user: home,
    )
    return workspace, shared, fake_sched


# ── Plan enumeration ──────────────────────────────────────────────────────────


def _actions_all_shapes() -> list[dict]:
    return [
        {   # macOS seam artifact
            "id": "daily-digest",
            "mechanism": "launchd",
            "install": {"plist_label": f"ai.evolve.{BOT}.task-manager",
                        "command": "/bin/bash scripts/tasks.py",
                        "schedule": {"cron": {"Hour": 9}}},
            "installed_artifact":
                f"/Library/LaunchDaemons/ai.evolve.{BOT}.task-manager.plist",
        },
        {   # Linux seam artifact (stamped by the #3399 systemd materializer)
            "id": "weekly-report",
            "mechanism": "launchd",
            "install": {"plist_label": f"ai.evolve.{BOT}.weekly-report",
                        "command": "/bin/bash scripts/tasks.py",
                        "schedule": {"cron": {"Weekday": 1}}},
            "installed_artifact":
                f"/etc/systemd/system/ai.evolve.{BOT}.weekly-report.service",
        },
        {   # python-signal compound artifact: plist + wrapper
            "id": "check-overdue",
            "mechanism": "launchd_python_signal",
            "install": {"label": f"ai.evolve.{BOT}.check-overdue",
                        "command": "/usr/bin/true"},
            "installed_artifact": (
                f"/Library/LaunchDaemons/ai.evolve.{BOT}.check-overdue.plist"
                "+{ws}/evolve/scheduled/check-overdue.py"
            ),
        },
        {   # heartbeat managed section
            "id": "hb-check",
            "mechanism": "oc_heartbeat_instruction",
            "install": {"file": "HEARTBEAT.md",
                        "section_anchor": "## Task Manager — Check",
                        "body": "Check tasks."},
            "installed_artifact": "HEARTBEAT.md#Task Manager — Check",
        },
        {   # TAMPERED: system daemon outside the bot's namespace
            "id": "evil-apple",
            "mechanism": "launchd",
            "install": {},
            "installed_artifact": "/Library/LaunchDaemons/com.apple.sshd.plist",
        },
        {   # TAMPERED: another bot's unit
            "id": "evil-crossbot",
            "mechanism": "launchd",
            "install": {},
            "installed_artifact":
                "/Library/LaunchDaemons/ai.evolve.other_bot.backup.plist",
        },
        {   # failed install: config label only, no artifact
            "id": "never-landed",
            "mechanism": "launchd",
            "install": {"plist_label": "ai.evolve.${bot_id}.never-landed",
                        "command": "/bin/true",
                        "schedule": {"every_minutes": 5}},
        },
    ]


def test_plan_enumerates_all_artifact_shapes(env):
    workspace, shared, _sched = env
    actions = _actions_all_shapes()
    actions[2]["installed_artifact"] = actions[2]["installed_artifact"].replace(
        "{ws}", str(workspace))
    _seed_manifest(workspace, actions)

    from evolve_admin.applications.manifest import plan_manifest_deletion
    plan = plan_manifest_deletion(APP, BOT, shared, workspace_path=workspace)
    assert plan["ok"] is True
    items = plan["scheduled_teardown"]
    by_kind = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)

    unit_labels = {u["label"]: u for u in by_kind["scheduled_unit"]}
    # Both platform shapes derive labels; eligibility inside the namespace.
    assert unit_labels[f"ai.evolve.{BOT}.task-manager"]["eligible"] is True
    assert unit_labels[f"ai.evolve.{BOT}.weekly-report"]["eligible"] is True
    assert unit_labels[f"ai.evolve.{BOT}.check-overdue"]["eligible"] is True
    # Config-label fallback for a never-landed install, ${bot_id} expanded.
    assert unit_labels[f"ai.evolve.{BOT}.never-landed"]["eligible"] is True
    # Tampered artifacts derive labels but are INELIGIBLE, with a reason.
    assert unit_labels["com.apple.sshd"]["eligible"] is False
    assert "namespace" in unit_labels["com.apple.sshd"]["reason"]
    assert unit_labels["ai.evolve.other_bot.backup"]["eligible"] is False

    wrappers = by_kind["wrapper_file"]
    assert wrappers[0]["path"].endswith("evolve/scheduled/check-overdue.py")

    hb = by_kind["heartbeat_section"][0]
    assert hb["file"] == "HEARTBEAT.md"
    assert hb["section_anchor"] == "## Task Manager — Check"


def test_plan_is_pure(env):
    workspace, shared, sched = env
    _seed_manifest(workspace, _actions_all_shapes()[:2])
    from evolve_admin.applications.manifest import plan_manifest_deletion
    plan_manifest_deletion(APP, BOT, shared, workspace_path=workspace)
    assert sched.calls == [], "plan must make no scheduler calls"


# ── Execute ───────────────────────────────────────────────────────────────────


def _spec(label: str) -> JobSpec:
    return JobSpec(label=label, program_args=["/bin/true"],
                   run_at_load=False, keep_alive=False)


def test_execute_removes_eligible_units_only(env):
    workspace, shared, sched = env
    actions = _actions_all_shapes()
    actions[2]["installed_artifact"] = actions[2]["installed_artifact"].replace(
        "{ws}", str(workspace))
    _seed_manifest(workspace, actions)

    # Live units for the app + a system daemon + another bot's unit.
    for label in (f"ai.evolve.{BOT}.task-manager",
                  f"ai.evolve.{BOT}.weekly-report",
                  f"ai.evolve.{BOT}.check-overdue",
                  "com.apple.sshd",
                  "ai.evolve.other_bot.backup"):
        sched.seed_job(_spec(label))
    # Wrapper on disk + heartbeat section on disk.
    wrapper = workspace / "evolve" / "scheduled" / "check-overdue.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env python3\n")
    (workspace / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Task Manager — Check\n"
        f"<!-- evolve-managed: pkg={PKG} -->\n\nCheck tasks.\n\n"
        "## Operator notes\nkeep me\n"
    )

    from evolve_admin.applications.manifest import execute_scheduled_teardown
    res = execute_scheduled_teardown(APP, BOT, shared)
    assert res["ok"] is True

    removed = {c[1] for c in sched.calls if c[0] == "remove"}
    assert removed == {
        f"ai.evolve.{BOT}.task-manager",
        f"ai.evolve.{BOT}.weekly-report",
        f"ai.evolve.{BOT}.check-overdue",
        f"ai.evolve.{BOT}.never-landed",   # idempotent no-op remove
    }
    # The tampered targets were never touched.
    assert "com.apple.sshd" in sched.jobs
    assert "ai.evolve.other_bot.backup" in sched.jobs

    assert not wrapper.exists()
    hb_text = (workspace / "HEARTBEAT.md").read_text()
    assert "Task Manager — Check" not in hb_text
    assert "Operator notes" in hb_text, "non-managed sections must survive"

    statuses = {(r["kind"], r.get("label") or r.get("path") or r.get("file")):
                r["status"] for r in res["results"]}
    assert statuses[("scheduled_unit", "com.apple.sshd")] == "skipped"


def test_infra_daemon_labels_are_reserved(env):
    """A tampered artifact aiming at the bot's OWN infra daemon (the
    per-bot backup daemon shares the ai.evolve.<bot>.* namespace) is
    refused by the infra deny-list, plan and execute alike."""
    workspace, shared, sched = env
    infra = f"ai.evolve.{BOT}.backup"
    _seed_manifest(workspace, [{
        "id": "evil-infra", "mechanism": "launchd", "install": {},
        "installed_artifact": f"/Library/LaunchDaemons/{infra}.plist",
    }])
    sched.seed_job(_spec(infra))

    from evolve_admin.applications.manifest import (
        execute_scheduled_teardown, plan_manifest_deletion,
    )
    plan = plan_manifest_deletion(APP, BOT, shared, workspace_path=workspace)
    unit = [i for i in plan["scheduled_teardown"]
            if i["kind"] == "scheduled_unit"][0]
    assert unit["eligible"] is False
    assert "infra" in unit["reason"]

    res = execute_scheduled_teardown(APP, BOT, shared)
    assert res["ok"] is True
    assert infra in sched.jobs, "infra daemon must never be removed by uninstall"


def test_execute_wrapper_symlinked_dir_refused(env):
    """A bot that swaps evolve/scheduled for a symlink (e.g. into a
    shared, evolve-writable dir) must not get files there unlinked."""
    workspace, shared, _sched = env
    outside = workspace.parent / "outside"
    outside.mkdir()
    victim = outside / "victim.py"
    victim.write_text("precious\n")
    (workspace / "evolve").mkdir()
    (workspace / "evolve" / "scheduled").symlink_to(outside)

    _seed_manifest(workspace, [{
        "id": "evil-symlink", "mechanism": "launchd_python_signal",
        "install": {},
        "installed_artifact": (
            f"/Library/LaunchDaemons/ai.evolve.{BOT}.x.plist"
            f"+{workspace / 'evolve' / 'scheduled' / 'victim.py'}"
        ),
    }])
    from evolve_admin.applications.manifest import execute_scheduled_teardown
    res = execute_scheduled_teardown(APP, BOT, shared)
    assert res["ok"] is True
    assert victim.exists(), "symlinked scheduled dir must be refused"
    wr = [r for r in res["results"] if r["kind"] == "wrapper_file"][0]
    assert wr["status"] == "skipped"


def test_execute_wrapper_containment(env):
    """A tampered wrapper path outside evolve/scheduled/ is never unlinked."""
    workspace, shared, sched = env
    victim = workspace / "IMPORTANT.md"
    victim.write_text("do not delete\n")
    _seed_manifest(workspace, [{
        "id": "evil-wrapper",
        "mechanism": "launchd_python_signal",
        "install": {},
        "installed_artifact": (
            f"/Library/LaunchDaemons/ai.evolve.{BOT}.evil.plist"
            f"+{victim}"
        ),
    }])
    from evolve_admin.applications.manifest import execute_scheduled_teardown
    res = execute_scheduled_teardown(APP, BOT, shared)
    assert res["ok"] is True
    assert victim.exists(), "containment must protect files outside evolve/scheduled/"
    wr = [r for r in res["results"] if r["kind"] == "wrapper_file"][0]
    assert wr["status"] == "skipped"


def test_execute_heartbeat_pkg_mismatch_and_unmarked(env):
    """Another pkg's managed section and operator-authored sections survive."""
    workspace, shared, _sched = env
    _seed_manifest(workspace, [{
        "id": "hb", "mechanism": "oc_heartbeat_instruction",
        "install": {"file": "HEARTBEAT.md",
                    "section_anchor": "## Other App — Check"},
        "installed_artifact": "HEARTBEAT.md#Other App — Check",
    }, {
        "id": "hb2", "mechanism": "oc_session_instruction",
        "install": {"file": "AGENTS.md", "section_anchor": "## Handwritten"},
    }])
    (workspace / "HEARTBEAT.md").write_text(
        "## Other App — Check\n<!-- evolve-managed: pkg=p-zzzz9999 -->\n\nx\n")
    (workspace / "AGENTS.md").write_text("## Handwritten\nno marker here\n")

    from evolve_admin.applications.manifest import execute_scheduled_teardown
    res = execute_scheduled_teardown(APP, BOT, shared)
    assert res["ok"] is True  # skips are benign
    assert "Other App — Check" in (workspace / "HEARTBEAT.md").read_text()
    assert "Handwritten" in (workspace / "AGENTS.md").read_text()
    assert all(r["status"] == "skipped" for r in res["results"])


def test_execute_reports_failure_on_remove_error(env, monkeypatch):
    workspace, shared, sched = env
    _seed_manifest(workspace, _actions_all_shapes()[:1])
    monkeypatch.setattr(
        FakeScheduler, "remove",
        lambda self, label, timeout=None: (False, "sudo: a password is required"),
    )
    from evolve_admin.applications.manifest import execute_scheduled_teardown
    res = execute_scheduled_teardown(APP, BOT, shared)
    assert res["ok"] is False
    assert any(r["status"] == "failed" for r in res["results"])


# ── Endpoint integration ──────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch, fake_sched):
    from evolve_admin.web.server import create_app

    home = tmp_path / "home"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()

    network = {"bots": {BOT: {"user": BOT}}, "sharedDir": str(shared)}
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id, user=None: workspace,
    )
    # app_registry (finalize's regen) binds get_bot_workspace at its own
    # import, so the config-module patch above only reaches it when
    # app_registry is imported AFTER this fixture runs. Patch the bound
    # name directly so the regen resolves the tmp workspace regardless of
    # test collection order (the CI-shard flake, 2026-07-03).
    monkeypatch.setattr(
        "evolve_admin.applications.app_registry.get_bot_workspace",
        lambda bot_id, user=None: workspace,
        raising=False,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.get_bot_user",
        lambda bot_id, network: BOT,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.load_network",
        lambda: {},
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.user_home",
        lambda user: home,
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, workspace, shared, fake_sched


def test_delete_endpoint_tears_down_units_and_regenerates_index(client):
    c, workspace, shared, sched = client
    label = f"ai.evolve.{BOT}.task-manager"
    manifest_path = _seed_manifest(workspace, [{
        "id": "daily", "mechanism": "launchd",
        "install": {"plist_label": label, "command": "/bin/true",
                    "schedule": {"every_minutes": 5}},
        "installed_artifact": f"/Library/LaunchDaemons/{label}.plist",
    }])
    sched.seed_job(_spec(label))

    resp = c.delete(
        f"/api/applications/{BOT}/{APP}",
        json={"delete_files": ["scripts/tasks.py"], "commit": True},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert ("remove", label) in sched.calls
    assert label not in sched.jobs
    td = {r["label"]: r["status"] for r in body["teardown_results"]
          if r["kind"] == "scheduled_unit"}
    assert td[label] == "ok"
    assert not manifest_path.exists()
    # Finalize regenerated the Tier-1 capability index.
    assert (workspace / "INSTALLED_APPS.md").exists()


def test_delete_preview_shows_teardown_without_acting(client):
    c, workspace, _shared, sched = client
    label = f"ai.evolve.{BOT}.task-manager"
    manifest_path = _seed_manifest(workspace, [{
        "id": "daily", "mechanism": "launchd",
        "install": {"plist_label": label, "command": "/bin/true",
                    "schedule": {"every_minutes": 5}},
        "installed_artifact": f"/Library/LaunchDaemons/{label}.plist",
    }])
    sched.seed_job(_spec(label))

    resp = c.delete(f"/api/applications/{BOT}/{APP}", json={"delete_files": []})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scheduled_teardown"][0]["label"] == label
    assert sched.calls == [c_ for c_ in sched.calls if c_[0] != "remove"]
    assert label in sched.jobs
    assert manifest_path.exists()


def test_unit_removal_failure_aborts_before_file_unlink(client, monkeypatch):
    """Resumability: a live unit that can't be removed must leave manifest
    AND files on disk — nothing is deleted out from under a firing unit."""
    c, workspace, _shared, sched = client
    label = f"ai.evolve.{BOT}.task-manager"
    manifest_path = _seed_manifest(workspace, [{
        "id": "daily", "mechanism": "launchd",
        "install": {"plist_label": label, "command": "/bin/true",
                    "schedule": {"every_minutes": 5}},
        "installed_artifact": f"/Library/LaunchDaemons/{label}.plist",
    }])
    sched.seed_job(_spec(label))
    monkeypatch.setattr(
        FakeScheduler, "remove",
        lambda self, label, timeout=None: (False, "launchctl: boo"),
    )

    resp = c.delete(
        f"/api/applications/{BOT}/{APP}",
        json={"delete_files": ["scripts/tasks.py"], "commit": True},
    )
    assert resp.status_code == 500
    body = resp.get_json()
    assert "teardown failed" in body["error"]
    assert manifest_path.exists(), "manifest is the resumable checklist"
    assert (workspace / "scripts/tasks.py").exists(), \
        "no file may be unlinked while its unit is still live"
