"""tests/test_person_link_routes.py — M1-B4a operator "same person" surface.

Covers ``/api/admin/bots/<bot_id>/person-link`` (GET + link + unlink),
registered by ``evolve_admin.web.routes_person_link.register_routes``.

The properties under test are the ones the surface exists to guarantee — not
the seam's own semantics (``roster_identity`` owns and tests those):

  * **Reachability renders multi-valued ``external_ids``.** One person, two
    platforms, one row (D1 / invariant 6).
  * **A collision is a named 409, and ``force`` is never the default.** The
    first POST refuses and names the conflicting row; only a second POST that
    explicitly carries ``force: true`` appends — and the other row is left
    untouched (append, not move).
  * **One id per channel.** Enforced server-side, so a hand-rolled POST cannot
    mint the ``"111,222"`` shape the deployed ``roleResolver`` mis-reads.
  * **``pod.admins`` is refused**, with the seam's reason surfaced verbatim.
  * **Unlink is the undo**, and touches only the row it was aimed at.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin import roster_identity as ri  # noqa: E402
from evolve_admin.web.routes_person_link import (  # noqa: E402
    apply_link,
    apply_unlink,
    person_label,
    person_link_state,
    register_routes,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _network(tmp_path: Path) -> dict:
    return {
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "pod": {
            "admins": {
                "external_ids": {"telegram": ["900001"]},
                "pod_users": ["pod_admin_user"],
            },
        },
        "bots": {
            # Discord-only bot — the M1 trigger shape: reachable on one
            # platform, needs a second.
            "team-bot-b": {
                "role": "member",
                "port": 19010,
                "multiUser": True,
                "primary_user": {
                    "name": "Sam",
                    "external_ids": {"discord": ["111222333"]},
                },
            },
            # Second bot whose owner already holds the id we will try to link
            # to team-bot-b's owner — the collision case.
            "lex": {
                "role": "member",
                "port": 19011,
                "primary_user": {
                    "name": "Robin",
                    "external_ids": {"telegram": ["555000"]},
                },
            },
            # A bot with no primary_user row at all — link must refuse rather
            # than conjure a row.
            "rowless": {"role": "member", "port": 19012},
        },
    }


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps(_network(tmp_path)))
    return p


@pytest.fixture
def client(network_path: Path):
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_routes(app, network_path)
    return app.test_client()


def _read(network_path: Path) -> dict:
    return json.loads(network_path.read_text())


def _ext(network_path: Path, bot_id: str) -> dict:
    return (_read(network_path)["bots"][bot_id]
            .get("primary_user", {}).get("external_ids", {}))


# ── GET: reachability + the linkable set ────────────────────────────────────


def test_get_renders_current_platform_reach(client) -> None:
    r = client.get("/api/admin/bots/team-bot-b/person-link")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["ref"] == "primary_user:team-bot-b"
    assert body["row_exists"] is True
    assert body["reachable"] == [
        {"channel": "discord", "label": "Discord", "ids": ["111222333"]},
    ]


def test_get_offers_only_channels_with_no_id_yet(client) -> None:
    """SCOPE LIMIT — a channel the person already holds an id on is not
    offered; a second id on one channel breaks the deployed roleResolver."""
    body = client.get("/api/admin/bots/team-bot-b/person-link").get_json()
    offered = {c["channel"] for c in body["linkable_channels"]}
    assert "discord" not in offered
    assert "telegram" in offered
    assert body["one_id_per_channel"] is True


def test_get_channels_come_from_the_registry(client) -> None:
    """Invariant 7 — the offered set is a registry projection, and it carries
    the per-channel pairing copy so the UI needs no channel table."""
    body = client.get("/api/admin/bots/team-bot-b/person-link").get_json()
    tg = next(c for c in body["linkable_channels"] if c["channel"] == "telegram")
    assert tg["label"] == "Telegram"
    assert tg["id_label"] and tg["id_hint"]


def test_get_unknown_bot_404s(client) -> None:
    assert client.get("/api/admin/bots/nope/person-link").status_code == 404


# ── Link: the happy path is the multi-platform case ─────────────────────────


def test_link_appends_a_second_platform_to_one_row(
        client, network_path: Path) -> None:
    r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                    json={"channel": "telegram", "external_id": "777888"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ids"] == ["777888"]
    # ONE row, now multi-valued — D1: platform is an attribute of the person.
    ext = _ext(network_path, "team-bot-b")
    assert ext == {"discord": ["111222333"], "telegram": ["777888"]}


def test_link_is_idempotent(client, network_path: Path) -> None:
    payload = {"channel": "telegram", "external_id": "777888"}
    client.post("/api/admin/bots/team-bot-b/person-link/link", json=payload)
    r = client.post("/api/admin/bots/team-bot-b/person-link/link", json=payload)
    assert r.status_code == 200
    assert _ext(network_path, "team-bot-b")["telegram"] == ["777888"]


def test_link_requires_channel_and_id(client) -> None:
    for body in ({"external_id": "1"}, {"channel": "telegram"},
                 {"channel": "telegram", "external_id": "   "}):
        r = client.post("/api/admin/bots/team-bot-b/person-link/link", json=body)
        assert r.status_code == 400
        assert r.get_json()["error"] == "invalid"


def test_link_refuses_a_second_id_on_an_occupied_channel(
        client, network_path: Path) -> None:
    r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                    json={"channel": "discord", "external_id": "444555666"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "channel_occupied"
    assert body["existing_ids"] == ["111222333"]
    # Unchanged on disk — the refusal is real, not cosmetic.
    assert _ext(network_path, "team-bot-b")["discord"] == ["111222333"]


def test_channel_occupied_is_not_forceable(client, network_path: Path) -> None:
    """``force`` is the collision escape hatch, NOT a way past the scope
    limit — the stringify hazard is not an operator judgment call."""
    r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                    json={"channel": "discord", "external_id": "444555666",
                          "force": True})
    assert r.status_code == 409
    assert r.get_json()["error"] == "channel_occupied"
    assert _ext(network_path, "team-bot-b")["discord"] == ["111222333"]


def test_link_refuses_when_the_row_does_not_exist(
        client, network_path: Path) -> None:
    r = client.post("/api/admin/bots/rowless/person-link/link",
                    json={"channel": "telegram", "external_id": "777888"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "refused"
    assert "primary_user" not in _read(network_path)["bots"]["rowless"]


# ── The collision, presented honestly ───────────────────────────────────────


def test_collision_409s_and_names_the_other_row(
        client, network_path: Path) -> None:
    r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                    json={"channel": "telegram", "external_id": "555000"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "conflict"
    assert body["appends_only"] is True
    keys = {c["key"] for c in body["conflicts"]}
    assert keys == {"primary_user:lex"}
    # Named in operator terms, not as a wire key.
    assert body["conflicts"][0]["label"] == "Robin — lex's owner"
    # Nothing written — a collision is a decision, not a delay.
    assert "telegram" not in _ext(network_path, "team-bot-b")


def test_collision_is_never_auto_forced(client, network_path: Path) -> None:
    """The route must not retry with force on the operator's behalf. Proof:
    N identical un-forced POSTs leave the row untouched every time."""
    for _ in range(3):
        r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                        json={"channel": "telegram", "external_id": "555000"})
        assert r.status_code == 409
        assert r.get_json()["error"] == "conflict"
    assert "telegram" not in _ext(network_path, "team-bot-b")


def test_force_is_a_second_explicit_request_and_appends(
        client, network_path: Path) -> None:
    payload = {"channel": "telegram", "external_id": "555000"}
    assert client.post(
        "/api/admin/bots/team-bot-b/person-link/link", json=payload,
    ).status_code == 409

    r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                    json={**payload, "force": True})
    assert r.status_code == 200
    assert r.get_json()["forced"] is True
    assert _ext(network_path, "team-bot-b")["telegram"] == ["555000"]
    # APPENDS, never moves — lex's owner keeps the id. Un-linking there is a
    # separate explicit action, so one confirm cannot strip another person's
    # admission key.
    assert _ext(network_path, "lex")["telegram"] == ["555000"]


def test_pod_admin_collision_is_named_as_the_bag(client) -> None:
    r = client.post("/api/admin/bots/team-bot-b/person-link/link",
                    json={"channel": "telegram", "external_id": "900001"})
    assert r.status_code == 409
    conflict = r.get_json()["conflicts"][0]
    assert conflict["kind"] == ri.POD_ADMIN
    assert conflict["is_person"] is False
    assert "Pod admins" in conflict["label"]


# ── pod.admins is never a link TARGET ───────────────────────────────────────


def test_pod_admins_is_refused_as_a_target(tmp_path: Path) -> None:
    """The UI never offers it (the ref is always the bot's primary row), but
    if one ever arrives the seam's reason is surfaced, not swallowed."""
    net = _network(tmp_path)
    payload, status = apply_link(net, ri.POD_ADMINS, "telegram", "777888")
    assert status == 400
    assert payload["error"] == "refused"
    assert "pod-admin" in payload["message"]
    # Untouched — no id was granted admin.
    assert net["pod"]["admins"]["external_ids"]["telegram"] == ["900001"]


def test_state_ref_is_always_the_primary_row(tmp_path: Path) -> None:
    state = person_link_state(_network(tmp_path), "team-bot-b")
    assert state["ref"] == "primary_user:team-bot-b"
    assert ri.PersonRef(ri.POD_ADMIN).key not in state["ref"]


# ── Unlink — the undo ───────────────────────────────────────────────────────


def test_unlink_removes_only_from_the_targeted_row(
        client, network_path: Path) -> None:
    client.post("/api/admin/bots/team-bot-b/person-link/link",
                json={"channel": "telegram", "external_id": "555000",
                      "force": True})
    r = client.post("/api/admin/bots/team-bot-b/person-link/unlink",
                    json={"channel": "telegram", "external_id": "555000"})
    assert r.status_code == 200
    assert r.get_json()["removed"] is True
    assert "telegram" not in _ext(network_path, "team-bot-b")
    # The other row is untouched by the undo, exactly as it was by the force.
    assert _ext(network_path, "lex")["telegram"] == ["555000"]


def test_unlink_of_an_absent_id_is_a_no_op(client, network_path: Path) -> None:
    r = client.post("/api/admin/bots/team-bot-b/person-link/unlink",
                    json={"channel": "telegram", "external_id": "nope"})
    assert r.status_code == 200
    assert r.get_json()["removed"] is False


def test_unlink_refuses_pod_admins(tmp_path: Path) -> None:
    net = _network(tmp_path)
    payload, status = apply_unlink(net, ri.POD_ADMINS, "telegram", "900001")
    assert status == 400
    assert payload["error"] == "refused"
    assert net["pod"]["admins"]["external_ids"]["telegram"] == ["900001"]


def test_unlink_requires_both_fields(client) -> None:
    r = client.post("/api/admin/bots/team-bot-b/person-link/unlink",
                    json={"channel": "telegram"})
    assert r.status_code == 400


# ── Labeling ────────────────────────────────────────────────────────────────


def test_person_label_falls_back_to_the_bot_id(tmp_path: Path) -> None:
    net = _network(tmp_path)
    net["bots"]["team-bot-b"]["primary_user"].pop("name")
    assert person_label(net, ri.primary_user_ref("team-bot-b")) == (
        "team-bot-b's owner")
