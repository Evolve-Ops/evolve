"""tests/test_admin_ui_chat_slash_rejection.py — Fix 4 of the exec-leak diagnosis.

The admin-UI chat endpoint (``POST /api/home/chat``) must reject leading-
slash messages with an operator-facing explanation instead of forwarding
them to evo / OC. Leading-slash text is OC's control-plane surface
(``/approve``, ``/grant``, ``/reload``, …) — letting it pass through
gives the operator (or evo asking the operator) a way to drive OC's
exec-approval workflow without going through evo's registered-tool gate.

The 2026-05-21 transcript at the root of this fix had evo asking the
operator to type four ``/approve <id>`` commands into the chat box; this
is the structural layer that closes that side channel regardless of
what evo's openclaw.json exec policy says.

See internal/diagnosis-evo-exec-approval-leak-2026-05-21.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


def _make_app(tmp_path: Path):
    """Identical-shape helper to test_home_chat._make_app — write a
    minimal network.json + return a Flask app via ``create_app``."""
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {"evolve": {"role": "primary"}},
        "members": ["evolve"],
    }))
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from evolve_admin.web.server import create_app
    return create_app(net)


def _patch_proxy(monkeypatch, *, text="ok"):
    """Replace send_to_evo with a fake; return the calls list. Lets us
    assert that rejected messages NEVER reach the proxy."""
    calls: list[dict] = []
    from evolve_admin.web import home_chat_routes as _routes  # noqa: F401
    from evolve_admin.evo import proxy as _proxy

    def fake_send(message, *, session_id, network_path, page_context=None, **kw):
        calls.append({
            "message": message,
            "session_id": session_id,
            "page_context": page_context,
        })
        return _proxy.ProxyResult(
            text=text, session_id=session_id,
            model="claude-sonnet-4-6", error=None,
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# Positive — leading-slash messages are rejected
# ─────────────────────────────────────────────────────────────────────────────


def test_rejects_oc_approve_command(tmp_path, monkeypatch):
    """The exact failure mode from the 2026-05-21 transcript:
    operator types ``/approve <id> allow-once`` into the chat box. The
    endpoint must NOT forward this to evo / OC; it must return a
    rejection message instead."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "/approve b47cf95f allow-once",
    })
    assert r.status_code == 200
    body = r.get_json()
    # Response shape: source=evo so the JS treats it like a normal reply.
    assert body["source"] == "evo"
    # The rejection message must mention OpenClaw control commands so the
    # operator knows WHY their input was rejected.
    assert "OpenClaw control command" in body["reply"]
    assert "/approve" in body["reply"]
    # And it MUST NOT have hit the proxy.
    assert calls == [], (
        "leading-slash message must not be forwarded to send_to_evo; "
        "that would route the operator into OC's slash-command surface."
    )


def test_rejects_leading_slash_with_whitespace(tmp_path, monkeypatch):
    """``lstrip()`` handles leading whitespace, so "   /approve abc"
    still gets rejected. Defense against operator typos where the
    chat input picks up a leading space from autocomplete."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "  /approve abc",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert "OpenClaw control command" in body["reply"]
    assert calls == []


def test_rejects_other_oc_slash_commands(tmp_path, monkeypatch):
    """The rule is structural — ``/approve`` is the most recently
    observed instance but any leading-slash control command is rejected
    on the same principle. Pins the structural shape."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    for command in ("/grant abc", "/reload", "/restart security_bot", "/foo"):
        r = app.test_client().post("/api/home/chat", json={"message": command})
        assert r.status_code == 200
        body = r.get_json()
        assert "OpenClaw control command" in body["reply"], (
            f"command {command!r} should be rejected with the same message"
        )
    assert calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Negative — non-slash messages must pass through unchanged
# ─────────────────────────────────────────────────────────────────────────────


def test_plain_message_is_not_rejected(tmp_path, monkeypatch):
    """The control test: ordinary messages still reach the proxy.
    Catches an over-broad regex / typo that would block normal chat."""
    calls = _patch_proxy(monkeypatch, text="hi from evo")
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={"message": "hello"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["reply"] == "hi from evo"
    assert len(calls) == 1
    assert calls[0]["message"] == "hello"


def test_message_with_mid_string_slash_is_not_rejected(tmp_path, monkeypatch):
    """Only LEADING slash blocks. A path mention mid-sentence ("in
    /tmp the file is...") must pass through — operators do this when
    describing what they're seeing."""
    calls = _patch_proxy(monkeypatch, text="ack")
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "in /tmp the file is missing",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["reply"] == "ack"
    assert len(calls) == 1
    assert calls[0]["message"] == "in /tmp the file is missing"


def test_message_starting_with_double_slash_is_not_rejected(
    tmp_path, monkeypatch,
):
    """``//`` is not an OC slash-command shape — let it through. Avoids
    rejecting an operator who happens to start a message with a code
    comment or URL fragment."""
    calls = _patch_proxy(monkeypatch, text="ack")
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "//this is a comment",
    })
    assert r.status_code == 200
    assert len(calls) == 1


def test_message_with_slash_in_question_passes(tmp_path, monkeypatch):
    """Questions like "what does team_bot_a/admin_bot do?" still reach the proxy.
    Generic guard against an over-eager slash filter."""
    calls = _patch_proxy(monkeypatch, text="team_bot_a and admin_bot are...")
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "what does team_bot_a/admin_bot do?",
    })
    assert r.status_code == 200
    assert len(calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Response-shape compatibility
# ─────────────────────────────────────────────────────────────────────────────


def test_rejection_response_shape_matches_normal_reply(tmp_path, monkeypatch):
    """The rejection payload must match the shape the JS chat-drawer
    code expects (``source``, ``reply``, ``authority``, ``session_id``
    keys present). Otherwise the client breaks on rejection — silent
    failure mode."""
    _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "/approve abc allow-once",
    })
    assert r.status_code == 200
    body = r.get_json()
    for key in ("source", "reply", "authority", "session_id"):
        assert key in body, (
            f"rejection response missing required field {key!r}; "
            f"frontend will break on this shape"
        )
    # source must be "evo" (not "proxy_error") so the JS renders this as
    # a normal reply bubble, not a red error bubble.
    assert body["source"] == "evo"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
