"""AL-3.2 — the surface: the two write routes, and what the detail read says.

Brief: ``internal/dispatch/done/al-3-2-install-to.md``. Engine claims live in
``test_al_3_2_install_to.py``; this file is about what the Apps page can
actually see and press.

THE APPS-ROW RULE, which is the claim the brief singles out. After a
deterministic install the target bot must appear as a row of the SAME app —
not a second app — and its files must read as verified rather than as
unexplained drift. That second half is not free: the Files panel resolved a
files-pack by ``pkg_id`` through the gallery, and an app installed from its own
AL-3.1 pack has no ``pkg_id`` at all. Without the app-keyed carrier every
placeholder-bearing file on the target would render ``differs`` — the panel
reporting a machine-verified install as drift, which is worse than saying
nothing.

  ``test_the_installed_bot_is_a_row_of_the_same_app``
  ``test_the_target_reads_verified_not_drifted``
  ``test_the_digest_column_says_source_because_a_pack_exists``

Plus the route contract: ``dry_run`` defaults TRUE on both writes, and a
refusal is 4xx — a well-formed request about a state that cannot be acted on
is not a server error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_install import install_app_to_bot  # noqa: E402
from evolve_admin.applications.app_snapshot import snapshot_app  # noqa: E402
from evolve_admin.web import server as _server  # noqa: E402
from evolve_admin.web.routes_app_install import (  # noqa: E402
    register_app_install_routes,
)
from evolve_admin.web.routes_apps import register_apps_routes  # noqa: E402

APP = "task-manager"
SOURCE = "atlas"
SOURCE_USER = "atlas-user"
TARGET = "beacon"
TARGET_USER = "beacon-user"


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    """A two-bot pod with the app defined on one of them, plus the routes.

    The bots' account names differ from their ids on purpose: the workspace
    resolver, the install context and the privileged helper all key on
    different one of the two, and a fixture where they coincide would let a
    mix-up pass.
    """
    homes = {SOURCE: tmp_path / "home" / SOURCE_USER,
             TARGET: tmp_path / "home" / TARGET_USER}
    users = {SOURCE: SOURCE_USER, TARGET: TARGET_USER}
    workspaces = {b: h / ".openclaw" / "workspace" for b, h in homes.items()}
    for ws in workspaces.values():
        (ws / "manifests").mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()

    ws = workspaces[SOURCE]
    (ws / "scripts").mkdir()
    (ws / "scripts" / "tasks.py").write_text(
        f"WORKSPACE = '{ws}'\nOWNER = '{SOURCE}'\n", encoding="utf-8")
    (ws / "scripts" / "steady.py").write_text(
        "print('the same everywhere')\n", encoding="utf-8")
    (ws / "manifests" / f"{APP}.json").write_text(json.dumps({
        "app_id": APP, "name": "Task Manager", "definition_status": "defined",
        "identity": {"purpose": "Tracks tasks. And says so."},
        "files": [
            {"path": "scripts/tasks.py", "role": "vital_to_blueprint"},
            {"path": "scripts/steady.py", "role": "reference_only"},
        ],
    }), encoding="utf-8")

    network = {"sharedDir": str(shared), "members": [SOURCE, TARGET],
               "bots": {SOURCE: {"user": SOURCE_USER},
                        TARGET: {"user": TARGET_USER}}}
    network_path = shared / "network.json"
    network_path.write_text(json.dumps(network), encoding="utf-8")

    monkeypatch.setattr("evolve_admin.config.get_bot_user",
                        lambda bot_id, network=None: users[bot_id])
    monkeypatch.setattr("evolve_admin.config.bot_home",
                        lambda bot_id, network=None: homes[bot_id])
    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(workspaces[bid])})
    monkeypatch.setattr(_server, "_resolve_bot_user",
                        lambda bid, *a, **kw: users.get(bid, bid))

    app = Flask(__name__)
    register_apps_routes(app, network_path)
    register_app_install_routes(app, network_path)
    app.testing = True

    class Pod:
        pass

    p = Pod()
    p.client = app.test_client()
    p.shared, p.network, p.workspaces = shared, network, workspaces
    return p


def _detail(pod) -> dict:
    response = pod.client.get(f"/api/apps/{APP}")
    assert response.status_code == 200, response.data
    return response.get_json()


def _install(pod) -> dict:
    return install_app_to_bot(APP, TARGET, shared_dir=pod.shared,
                              network=pod.network, dry_run=False)


# ── the apps-row rule ───────────────────────────────────────────────────────


def test_the_installed_bot_is_a_row_of_the_same_app(pod):
    before = _detail(pod)
    assert [b["bot_id"] for b in before["bots"]] == [SOURCE]
    # ``bots_without`` is every bot the route can see minus the ones that have
    # it — which includes the ``evolve`` service account, because it can hold a
    # manifest (``routes_apps._bots``). The claim here is about the target.
    assert TARGET in before["bots_without"]

    result = _install(pod)
    assert result["ok"], result

    after = _detail(pod)
    assert sorted(b["bot_id"] for b in after["bots"]) == sorted([SOURCE, TARGET])
    assert TARGET not in after["bots_without"]
    assert after["definition_states"][TARGET] == "defined"
    # ONE app, not two: the pod list must not have grown a second row.
    listing = pod.client.get("/api/apps").get_json()
    assert [a["app_id"] for a in listing["apps"]] == [APP]


def test_the_target_reads_verified_not_drifted(pod):
    """Every file on the target is ``ok`` or explained — never unexplained.

    ``scripts/steady.py`` declares no placeholder and must land byte-identical
    (``ok``); ``scripts/tasks.py`` declares the bot's own tokens and must land
    DIFFERENT and be explained by re-substitution (``differs_placeholder``).
    Both are real conditions produced by the real install, not stubs.
    """
    assert _install(pod)["ok"]
    package = _detail(pod)["package"]
    states = {f["path"]: f["bots"][TARGET]["state"] for f in package["files"]}
    assert states["scripts/steady.py"] == "ok"
    assert states["scripts/tasks.py"] == "differs_placeholder", (
        "a deterministic install whose difference IS the declared substitution "
        "must not render as unexplained drift"
    )
    assert "differs" not in set(states.values())


def test_the_digest_column_says_source_because_a_pack_exists(pod):
    """The carrier is reported, not guessed (AL-1.5c §9.2).

    ``app_snapshot`` writes the Spec's ``package.files[].sha256`` FROM the
    pack's source digests, so where the pack exists the Spec's digests ARE
    source digests — and the column has to say so, or the operator reads a hex
    string that could be either side of the substitution boundary.
    """
    assert _install(pod)["ok"]
    assert _detail(pod)["package"]["sha_kind"] == "source"


def test_the_detail_says_whether_the_app_can_be_installed_elsewhere(pod):
    before = _detail(pod)["install"]
    assert before["state"] == "snapshot_needed"
    assert before["sources"] == [SOURCE]

    snapshot_app(SOURCE, APP, shared_dir=pod.shared, network=pod.network,
                 dry_run=False)
    after = _detail(pod)["install"]
    assert after["state"] == "pack" and after["pack_files"] == 2


# ── the route contract ──────────────────────────────────────────────────────


def test_install_defaults_to_a_dry_run(pod):
    response = pod.client.post(f"/api/apps/{APP}/install", json={"bot_id": TARGET})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] and payload["dry_run"] is True
    assert not (pod.workspaces[TARGET] / "scripts").exists(), (
        "a caller that omits dry_run must not have written to a bot"
    )


def test_install_applies_when_asked(pod):
    response = pod.client.post(
        f"/api/apps/{APP}/install", json={"bot_id": TARGET, "dry_run": False})
    assert response.status_code == 200, response.data
    payload = response.get_json()
    assert payload["ok"] and payload["dry_run"] is False
    assert sorted(payload["installed"]) == [
        "scripts/steady.py", "scripts/tasks.py"]
    assert payload["proof"]["explained"] is True
    assert (pod.workspaces[TARGET] / "scripts" / "tasks.py").is_file()


def test_a_missing_bot_id_is_a_400(pod):
    response = pod.client.post(f"/api/apps/{APP}/install", json={})
    assert response.status_code == 400
    assert response.get_json()["missing"] == ["bot_id"]


def test_a_refusal_is_a_4xx_not_a_500(pod):
    """A healthy decline must not read like an outage on a status page."""
    # Update before the app is there: a well-formed request about a bot that
    # does not have it.
    response = pod.client.post(
        f"/api/apps/{APP}/update", json={"bot_id": TARGET, "dry_run": False})
    assert response.status_code == 409, response.data
    assert response.get_json()["error"].startswith("not_installed")

    assert _install(pod)["ok"]
    response = pod.client.post(
        f"/api/apps/{APP}/install", json={"bot_id": TARGET, "dry_run": False})
    assert response.status_code == 409, response.data
    assert response.get_json()["error"].startswith("already_installed")


def test_update_defaults_to_a_dry_run_and_reports_the_merge(pod):
    assert _install(pod)["ok"]
    local = pod.workspaces[TARGET] / "scripts" / "tasks.py"
    local.write_text(local.read_text() + "# local\n", encoding="utf-8")

    response = pod.client.post(f"/api/apps/{APP}/update", json={"bot_id": TARGET})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True and payload["adapted"] is True
    assert [c["rel"] for c in payload["conflicts"]] == ["scripts/tasks.py"]
    assert local.read_text().endswith("# local\n")


def test_an_update_that_would_flatten_is_a_409_until_confirmed(pod):
    assert _install(pod)["ok"]
    local = pod.workspaces[TARGET] / "scripts" / "tasks.py"
    local.write_text(local.read_text() + "# local\n", encoding="utf-8")

    response = pod.client.post(
        f"/api/apps/{APP}/update", json={"bot_id": TARGET, "dry_run": False})
    assert response.status_code == 409
    assert response.get_json()["error"].startswith("would_overwrite_local_changes")
    assert "# local" in local.read_text()

    response = pod.client.post(f"/api/apps/{APP}/update", json={
        "bot_id": TARGET, "dry_run": False, "confirm_overwrite": True})
    assert response.status_code == 200, response.data
    assert "# local" not in local.read_text()
