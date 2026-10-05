"""Tests for the bot-facing checkpoint route — ``POST /api/cost-checkpoint/answer``.

Operator decision D-CC3
(internal/decision-cost-cap-checkpoint-2026-09-04.md): only ``owners``
(admin + primary_user) can answer "continue", and the role is **resolved by
the platform, never by the model**. This route is where that gate actually
lives — the plugin forwards the platform-captured identity and renders
whichever verdict comes back, so these tests are the authorization contract.

The security-critical surface, same primitive as ``directory_bot_routes`` /
``google_bot_routes``: the calling bot is bound server-side from the
unix-socket kernel peer uid, NEVER from a request field, so a bot can only
ever answer its own checkpoint.

Uses Flask's test client with manually-set request.environ
(REMOTE_TRANSPORT / REMOTE_PEER_UID) to simulate unix-socket peer
credentials — the same technique as test_applications_bot_routes.

FAKE ids only (docs/PLACEHOLDER_NAMING.md).
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from breakers import store as bstore  # noqa: E402
from evolve_admin import roster_overlay  # noqa: E402
from evolve_admin.web.cost_checkpoint_bot_routes import (  # noqa: E402
    OWNER_ROLES,
    register_cost_checkpoint_bot_routes,
)
import spend_caps  # noqa: E402


_ME = pwd.getpwuid(os.getuid()).pw_name
BOT = "team_bot_a"

ADMIN_ID = "999"          # pod admin, on the pod.admins block
OWNER_ID = "1260193629"   # this bot's primary_user
GUEST_ID = "5550001"      # an ordinary participant


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def network_path(tmp_path: Path, shared: Path) -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(shared),
        "bots": {
            BOT: {
                "role": "member", "user": _ME,
                "primary_user": {"external_ids": {"telegram": [OWNER_ID]}},
            },
        },
        "pod": {"admins": {"external_ids": {"telegram": [ADMIN_ID]}, "names": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


@pytest.fixture
def app(network_path: Path) -> Flask:
    a = Flask(__name__)
    register_cost_checkpoint_bot_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def _unix_env(uid: int | None = None) -> dict:
    return {
        "REMOTE_TRANSPORT": "unix-socket",
        "REMOTE_PEER_UID": os.getuid() if uid is None else uid,
    }


def _arm(shared: Path, *, checkpoint: str = "pending", cap: float = 20.0) -> None:
    bstore.trip(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="auto:spend_alert",
        reason=f"per-bot daily cap exceeded: $20.57 ≥ ${cap:.2f}",
        checkpoint=checkpoint,
    )
    spend_caps.write_enforcement_flag(
        shared, BOT, "checkpoint", spend_at_trigger=20.57, cap=cap,
    )


def _answer(app: Flask, answer: str, stable_id: str, **env) -> tuple[int, dict]:
    with app.test_client() as c:
        resp = c.post(
            "/api/cost-checkpoint/answer",
            json={"answer": answer, "platform": "telegram", "stable_id": stable_id},
            environ_overrides={**_unix_env(), **env},
        )
    return resp.status_code, (resp.get_json() or {})


# ── Identity binding (the cross-bot boundary) ────────────────────────────────


def test_tcp_caller_is_refused(app: Flask, shared: Path) -> None:
    """A browser session must never act as a bot."""
    _arm(shared)
    with app.test_client() as c:
        resp = c.post(
            "/api/cost-checkpoint/answer",
            json={"answer": "continue", "platform": "telegram",
                  "stable_id": ADMIN_ID},
            environ_overrides={"REMOTE_PEER_UID": os.getuid()},  # no transport
        )
    assert resp.status_code == 403
    assert bstore.read_trip(shared, BOT, "cost").checkpoint == "pending"


def test_unknown_peer_uid_is_refused(app: Flask, shared: Path) -> None:
    _arm(shared)
    status, _ = _answer(app, "continue", ADMIN_ID, REMOTE_PEER_UID=4242424)
    assert status == 403
    assert bstore.read_trip(shared, BOT, "cost").checkpoint == "pending"


# ── Owner-only continue (D-CC3) ──────────────────────────────────────────────


def test_owners_are_admin_and_primary_user(self=None) -> None:
    """``owners`` per design-app-access-2026-08-15.md §3."""
    assert OWNER_ROLES == {"admin", "primary_user"}


@pytest.mark.parametrize("who", [OWNER_ID, ADMIN_ID])
def test_owner_continue_grants_the_increment(
    app: Flask, shared: Path, who: str,
) -> None:
    _arm(shared)
    status, body = _answer(app, "continue", who)

    assert status == 200
    assert body["authorized"] is True
    assert body["state"] == "continued"
    assert body["role"] in OWNER_ROLES
    # +50% of a $20 cap, and the ledger records the same number.
    assert body["increment_usd"] == 10.0
    rec = bstore.read_trip(shared, BOT, "cost")
    assert rec.checkpoint == "continued"
    assert rec.checkpoint_increment_usd == 10.0
    assert rec.checkpoint_answered_by == f"user:telegram:{who}"


def test_owner_continue_writes_the_ledger_row(app: Flask, shared: Path) -> None:
    _arm(shared)
    _answer(app, "continue", OWNER_ID)
    rows = [
        r for r in bstore.read_audit_log(shared)
        if r.get("action") == "checkpoint_continued"
    ]
    assert len(rows) == 1
    assert rows[0]["initiated_by"] == f"user:telegram:{OWNER_ID}"
    assert rows[0]["role"] == "primary_user"
    assert rows[0]["increment_usd"] == 10.0
    assert rows[0]["expires_at"]


def test_participant_continue_is_refused_with_no_extension(
    app: Flask, shared: Path,
) -> None:
    _arm(shared)
    status, body = _answer(app, "continue", GUEST_ID)

    assert status == 200          # a verdict, not a transport error
    assert body["authorized"] is False
    assert body["role"] == "participant"
    rec = bstore.read_trip(shared, BOT, "cost")
    assert rec.checkpoint == "pending"          # still held
    assert rec.checkpoint_increment_usd is None  # nothing granted


def test_blocked_user_continue_is_refused(app: Flask, shared: Path) -> None:
    _arm(shared)
    overlay = roster_overlay.load_overlay(shared, BOT)
    roster_overlay.block_identity(overlay, "telegram", GUEST_ID, by="test")
    roster_overlay.save_overlay(shared, BOT, overlay)

    status, body = _answer(app, "continue", GUEST_ID)
    assert body["authorized"] is False
    assert bstore.read_trip(shared, BOT, "cost").checkpoint == "pending"


def test_unnamed_speaker_is_not_an_owner(app: Flask, shared: Path) -> None:
    """Resolve-or-refuse: no identity, no grant. Never a default platform —
    that would mis-attribute a foreign sender onto a privileged id-space."""
    _arm(shared)
    with app.test_client() as c:
        resp = c.post(
            "/api/cost-checkpoint/answer",
            json={"answer": "continue"},
            environ_overrides=_unix_env(),
        )
    assert resp.status_code == 200
    assert resp.get_json()["authorized"] is False
    assert bstore.read_trip(shared, BOT, "cost").checkpoint == "pending"


def test_role_never_comes_from_the_request(app: Flask, shared: Path) -> None:
    """A body claiming ``role: admin`` changes nothing — the route resolves
    the role itself, so a model that talks its way into emitting one gains
    no authority."""
    _arm(shared)
    with app.test_client() as c:
        resp = c.post(
            "/api/cost-checkpoint/answer",
            json={"answer": "continue", "platform": "telegram",
                  "stable_id": GUEST_ID, "role": "admin",
                  "authorized": True, "increment_usd": 10000},
            environ_overrides=_unix_env(),
        )
    body = resp.get_json()
    assert body["authorized"] is False
    assert body["role"] == "participant"
    assert bstore.read_trip(shared, BOT, "cost").checkpoint_increment_usd is None


# ── Stop, and the non-owner asymmetry ────────────────────────────────────────


def test_anyone_may_stop(app: Flask, shared: Path) -> None:
    """Declining costs nothing and needs no privilege — the asymmetry is
    deliberate: spending money is gated, stopping is not."""
    _arm(shared)
    status, body = _answer(app, "stop", GUEST_ID)
    assert status == 200
    assert body["state"] == "declined"
    assert bstore.read_trip(shared, BOT, "cost").checkpoint == "declined"


def test_owner_may_continue_after_a_decline(app: Flask, shared: Path) -> None:
    _arm(shared, checkpoint="declined")
    status, body = _answer(app, "continue", OWNER_ID)
    assert body["state"] == "continued"


# ── Preconditions and bad input ──────────────────────────────────────────────


def test_no_checkpoint_is_a_conflict_not_a_grant(
    app: Flask, shared: Path,
) -> None:
    """An answer must never CREATE a hold, nor grant against a manual trip
    that carries no checkpoint."""
    bstore.trip(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="cli", reason="manual",
    )
    status, body = _answer(app, "continue", ADMIN_ID)
    assert status == 409
    assert bstore.read_trip(shared, BOT, "cost").checkpoint_increment_usd is None


def test_untripped_breaker_is_a_conflict(app: Flask, shared: Path) -> None:
    status, _ = _answer(app, "continue", ADMIN_ID)
    assert status == 409


@pytest.mark.parametrize("answer", ["", "maybe", "continue please", "yes"])
def test_unknown_answer_is_a_400(
    app: Flask, shared: Path, answer: str,
) -> None:
    """The wire verb is one of exactly two literals. The plugin has already
    classified the user's words; anything else reaching here is a caller
    bug, and a caller bug must not move the cap."""
    _arm(shared)
    status, _ = _answer(app, answer, ADMIN_ID)
    assert status == 400
    assert bstore.read_trip(shared, BOT, "cost").checkpoint == "pending"


def test_the_verb_is_normalized(app: Flask, shared: Path) -> None:
    """Case and surrounding whitespace are tolerated — a transport-level
    nicety, not an intent parser."""
    _arm(shared)
    status, body = _answer(app, "  CONTINUE ", OWNER_ID)
    assert status == 200
    assert body["state"] == "continued"


def test_missing_flag_records_the_answer_without_inventing_a_grant(
    app: Flask, shared: Path,
) -> None:
    """Better a second checkpoint than an invented ceiling."""
    bstore.trip(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        duration=timedelta(hours=24), initiated_by="auto:spend_alert",
        reason="cap", checkpoint="pending",
    )
    status, body = _answer(app, "continue", OWNER_ID)
    assert status == 200
    assert body["state"] == "continued"
    assert body["increment_usd"] is None
    assert bstore.read_trip(shared, BOT, "cost").checkpoint_increment_usd is None
