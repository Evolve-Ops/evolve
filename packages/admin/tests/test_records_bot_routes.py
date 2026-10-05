"""The six records verbs through the daemon — ``/api/records-bot/<app_id>/…``.

Module: ``evolve_admin/web/records_bot_routes.py``. Brief:
``app-store-and-ledger-verbs`` item 3 ("the six verbs work through the daemon
and refuse correctly").

Technique mirrors ``test_board_bot_routes.py``: a Flask test client with the
REMOTE_TRANSPORT / REMOTE_PEER_UID environ overrides, and the bot's account set
to the CURRENT test user so the real peer resolver maps ``os.getuid()`` to it.
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_contract as ac  # noqa: E402
from evolve_admin import app_spec_schema as ass  # noqa: E402
from evolve_admin import app_store  # noqa: E402
from evolve_admin.applications.app_spec import AppSpec  # noqa: E402
from evolve_admin.applications.app_spec_store import write_spec  # noqa: E402
from evolve_admin.web.records_bot_routes import register_records_bot_routes  # noqa: E402

_ME = pwd.getpwuid(os.getuid()).pw_name
APP = "collection-tracker"
BOT = "personal-bot"
OTHER = "other-bot"
STORE = {"tables": {
    "games": {"columns": {"id": "text", "title": "text"}, "key": ["id"],
              "rollups": {"current_status": {"rollup": "status",
                                             "map": {"acquired": "owned", "sold": "gone"}}}},
    "events": {"ledger": True, "of": "games", "kinds": ["acquired", "sold"]},
}}


@pytest.fixture()
def pod(tmp_path: Path, monkeypatch):
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(shared),
        "bots": {BOT: {"role": "member", "user": _ME},
                 OTHER: {"role": "member", "user": "no_such_account_xyz"}},
    }))
    write_spec(AppSpec.from_dict({"app_id": APP, "name": "n", "purpose": "p",
                                  "store": STORE}), shared)
    app_store.ensure_store(shared, APP, BOT, ass.validate_store(STORE))
    app_store.ensure_store(shared, APP, OTHER, ass.validate_store(STORE))
    # Each bot's manifests dir, redirected under tmp. BOT declares APP; OTHER does not.
    from evolve_admin.applications import manifest as _manifest
    monkeypatch.setattr(_manifest, "applications_dir",
                        lambda _shared, bot: tmp_path / "ws" / bot / "manifests")
    _declare(tmp_path, BOT, APP)
    app = Flask(__name__)
    register_records_bot_routes(app, network)
    return {"client": app.test_client(), "shared": shared, "app": app, "tmp": tmp_path}


def _declare(tmp_path: Path, bot: str, app_id: str) -> None:
    d = tmp_path / "ws" / bot / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{app_id}.json").write_text(json.dumps({"id": app_id, "name": app_id}))


def _env(uid: "int | None" = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


def _call(pod, verb: str, body: dict, env: "dict | None" = None):
    r = pod["client"].post(f"/api/records-bot/{APP}/{verb}", json=body,
                           environ_overrides=_env() if env is None else env)
    return r.status_code, r.get_json()


def test_the_six_verbs_work_through_the_daemon(pod):
    assert _call(pod, "put", {"table": "games", "row": {"id": "g1", "title": "A"}}) == (
        200, {"app_id": APP, "instance": BOT, "table": "games",
              "row": {"id": "g1", "title": "A"}, "op": "insert"})
    code, out = _call(pod, "ledger/append", {"entry": {"thing_id": "g1", "kind": "acquired",
                                                       "at": "2026-09-01"}})
    assert code == 200 and out["entry"]["by"] == f"bot:{BOT}"
    code, out = _call(pod, "get", {"table": "games", "key": "g1"})
    assert code == 200 and out["rollups"] == {"current_status": "owned"}
    code, out = _call(pod, "list", {"table": "games", "filter": {"title": "A"}})
    assert code == 200 and out["total"] == 1
    code, out = _call(pod, "history", {"table": "games", "key": "g1"})
    assert code == 200 and [e["kind"] for e in out["entries"]] == ["acquired"]
    _call(pod, "put", {"table": "games", "row": {"id": "g2"}})
    code, out = _call(pod, "delete", {"table": "games", "key": "g2"})
    assert code == 200 and out["deleted"] == ["g2"]


def test_tcp_and_unknown_uids_are_refused_on_every_verb(pod):
    for verb in ("list", "get", "put", "delete", "history", "ledger/append"):
        assert pod["client"].post(f"/api/records-bot/{APP}/{verb}", json={}).status_code == 403
        assert _call(pod, verb, {}, env=_env(4_242_424))[0] == 403


def test_the_caller_is_the_peer_never_a_field(pod):
    """A body naming another bot, or another bot's instance, cannot reach it."""
    code, out = _call(pod, "put", {"table": "games", "row": {"id": "g1"},
                                   "bot": OTHER, "bot_id": OTHER})
    assert code == 200 and out["instance"] == BOT
    code, out = _call(pod, "list", {"table": "games", "instance": OTHER})
    assert (code, out["code"]) == (403, "forbidden")
    assert app_store.records_list(pod["shared"], APP, "games", caller=app_store.PLATFORM,
                                  instance=OTHER)["total"] == 0


@pytest.mark.parametrize("verb, body, status, code", [
    ("put", {"table": "games", "row": {"id": "x", "colour": "red"}}, 400, "undeclared_column"),
    ("put", {"table": "nope", "row": {"id": "x"}}, 400, "unknown_table"),
    ("put", {"row": {"id": "x"}}, 400, "unknown_table"),
    ("get", {"table": "games", "key": "ghost"}, 404, "not_found"),
    ("list", {"table": "games", "filter": {"title": {"like": "%"}}}, 400, "bad_filter"),
    ("ledger/append", {"entry": {"thing_id": "g1", "kind": "stolen", "at": "2026-09-01"}},
     400, "unknown_kind"),
    ("ledger/append", {"entry": {"thing_id": "ghost", "kind": "sold", "at": "2026-09-01"}},
     400, "unknown_thing"),
])
def test_refusals_come_back_typed(pod, verb, body, status, code):
    _call(pod, "put", {"table": "games", "row": {"id": "g1"}})
    got_status, out = _call(pod, verb, body)
    assert (got_status, out["code"]) == (status, code), out
    assert out["error"]


def test_a_user_binding_is_refused_delete_through_the_daemon(pod):
    _call(pod, "put", {"table": "games", "row": {"id": "g1"}})
    app_store.set_binding(pod["shared"], APP, BOT, BOT, "user")
    code, out = _call(pod, "delete", {"table": "games", "key": "g1"})
    assert (code, out["code"]) == (403, "forbidden") and "records.delete" in out["error"]
    assert _call(pod, "get", {"table": "games", "key": "g1"})[0] == 200


def test_an_uninstalled_app_is_no_store(pod):
    (app_store.store_path(pod["shared"], APP, BOT)).unlink()
    code, out = _call(pod, "list", {"table": "games"})
    assert (code, out["code"]) == (409, "no_store")


def test_a_non_object_body_is_refused(pod):
    r = pod["client"].post(f"/api/records-bot/{APP}/list", data="[]",
                           content_type="application/json", environ_overrides=_env())
    assert r.status_code == 400


def test_every_records_route_carries_a_contract_row(pod):
    names = ac.route_names(pod["app"], "/api/records-bot/")
    assert len(names) == 6
    for n in names:
        assert ac.row(n, "daemon_endpoint").status == "shipped"


def test_the_records_prefix_is_a_peer_bound_prefix():
    from evolve_admin.web import peer_auth
    assert "/api/records-bot/" in peer_auth._PEER_BOT_ROUTE_PREFIXES  # noqa: SLF001


def _signals(pod) -> list[dict]:
    d = pod["shared"] / "signals" / "firing"
    return [json.loads(f.read_text()) for f in d.glob("*.json")] if d.exists() else []


@pytest.mark.parametrize("verb, body", [
    ("list", {"table": "people"}),
    ("get", {"table": "people", "key": "p1"}),
    ("put", {"table": "people", "row": {"id": "p1"}}),
    ("delete", {"table": "people", "key": "p1"}),
    ("history", {"table": "people", "key": "p1"}),
    ("ledger/append", {"entry": {"thing_id": "p1", "kind": "x", "at": "2026-09-01"}}),
])
def test_the_directory_is_refused_on_all_six_verbs(pod, verb, body):
    """D-AD8: a platform-owned store is never reachable through the generic tool,
    even when the calling bot's manifests name it."""
    from evolve_admin.user_directory.records import DIRECTORY_APP_ID
    _declare(pod["tmp"], BOT, DIRECTORY_APP_ID)
    r = pod["client"].post(f"/api/records-bot/{DIRECTORY_APP_ID}/{verb}", json=body,
                           environ_overrides=_env())
    out = r.get_json()
    assert (r.status_code, out["code"]) == (403, "forbidden"), out
    assert "platform-owned" in out["error"] and DIRECTORY_APP_ID in out["error"]
    sigs = _signals(pod)
    assert any(s["producer"] == "records_bot" and s["details"]["app_id"] == DIRECTORY_APP_ID
               for s in sigs)


def test_an_undeclared_app_is_refused_and_a_declared_one_is_served(pod):
    # OTHER's peer maps to no account here, so call as BOT against an app BOT
    # does not declare: a real, installed app whose store exists.
    write_spec(AppSpec.from_dict({"app_id": "second-app", "name": "n", "purpose": "p",
                                  "store": STORE}), pod["shared"])
    app_store.ensure_store(pod["shared"], "second-app", BOT, ass.validate_store(STORE))
    r = pod["client"].post("/api/records-bot/second-app/put", environ_overrides=_env(),
                           json={"table": "games", "row": {"id": "g1"}})
    assert (r.status_code, r.get_json()["code"]) == (403, "forbidden")
    assert "does not declare" in r.get_json()["error"]
    assert app_store.records_list(pod["shared"], "second-app", "games",
                                  caller=app_store.PLATFORM, instance=BOT)["total"] == 0
    assert any(s["details"]["app_id"] == "second-app" for s in _signals(pod))
    _declare(pod["tmp"], BOT, "second-app")
    r = pod["client"].post("/api/records-bot/second-app/put", environ_overrides=_env(),
                           json={"table": "games", "row": {"id": "g1"}})
    assert r.status_code == 200
