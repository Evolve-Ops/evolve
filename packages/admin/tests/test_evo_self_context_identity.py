"""tests/test_evo_self_context_identity.py — the assistant's own
identity is injected into <session-context> on every admin-UI turn.

Background. The Evo admin-UI tray chat once answered a cross-bot
question (*"why is atlas failing backup?"*) by claiming it was "running
as a member bot" hitting "authorization walls" and deflecting the
operator to "the admin UI chat" — the surface they were already in. The
tool layer was already gating it correctly (``EVOLVE_CALLER_SURFACE=
admin_ui``); the gap was purely *what the model was told about itself*.
A bot cannot observe its own identity unless told each turn (memory
note ``feedback_bot_cannot_observe_own_routing``), so the fix injects
the bot identity into the session-context block.

This pins the invariant across the two layers the fix touches:

  1. Proxy (``proxy.py``) — ``format_session_context`` renders a
     ``Bot: <id> (admin) …`` identity line, gated on the supplied
     ``bot_id`` / ``is_admin_bot`` keys, and omits it entirely when
     they are absent (so non-admin / non-identity callers are not
     handed a privilege claim they didn't assert).
  2. Route (``home_chat_routes.py``) — ``/api/home/chat`` (the admin-UI
     tray) puts ``bot_id="evolve"`` + ``is_admin_bot=True`` into the
     ``session_context`` it hands the proxy. The *server* asserts this;
     the frontend drawer is never trusted to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import proxy as P  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# format_session_context renders the bot-identity line
# ─────────────────────────────────────────────────────────────────────────────


def test_session_context_renders_admin_bot_identity():
    """With bot_id + is_admin_bot, the block opens with a ``Bot:`` line
    that names the bot AND carries the admin / most-privileged framing.
    This is the line that stops the member-bot confabulation."""
    block = P.format_session_context({
        "bot_id": "evolve",
        "is_admin_bot": True,
        "surface": "admin_ui",
        "surface_type": "laptop",
        "authority": "ask",
    })
    assert "Bot: evolve (admin)" in block
    # The privilege framing the model needs to stop disavowing its reach.
    assert "most-privileged caller" in block
    # The anti-deflection clause — the actual bug was deflecting the
    # operator to a surface they were already on / to itself.
    assert "never deflect" in block
    assert "member bot" in block
    # Identity is framed before the operator (who-you-are before
    # who-you-serve), so the model reads it first.
    assert block.index("Bot: evolve") < block.index("Operator:")


def test_session_context_injects_capability_ground_truth_for_admin_bot():
    """Task D (2026-06-20 atlas-backup incident): the admin-bot framing
    must inject evo's FIXED capability/ACL map so the model reasons from
    its known grants instead of inferring access from a failed syscall
    (evo's contradiction: "the evo user can't even stat the file" then "I
    have file_inherit ACL on atlas's .openclaw"). The line states the
    read ACL, the /tmp-staging + sudo cp write path, and the macOS full
    paths (which also backstops the bare-command 'command not found'
    failure)."""
    block = P.format_session_context({
        "bot_id": "evolve",
        "is_admin_bot": True,
        "surface": "admin_ui",
        "surface_type": "laptop",
        "authority": "ask",
    })
    # Read ACL on every bot's .openclaw (so it never claims it can't read).
    assert "READ ACL" in block
    assert "`.openclaw/` directory" in block
    # Write path: /tmp staging + sudo /bin/cp, NOT sudo -u <bot>.
    assert "/tmp staging" in block and "sudo /bin/cp" in block
    assert "sudo -u <bot>" in block  # named as the thing it canNOT do
    # macOS full paths enumerated (Task C reinforcement at the prompt layer).
    assert "/usr/sbin/chown" in block and "/bin/chmod" in block
    # Reason-from-grants framing, not infer-from-syscall.
    assert "do NOT infer your access from one failed syscall" in block


def test_session_context_no_capability_map_for_non_admin_bot():
    """The capability/ACL map is admin-bot-only — a non-admin caller
    (bare bot_id) must not be handed evo's privileged grant description."""
    block = P.format_session_context({
        "bot_id": "atlas",
        "surface": "telegram",
    })
    assert "READ ACL" not in block
    assert "do NOT infer your access from one failed syscall" not in block


def test_session_context_renders_plain_bot_when_not_admin():
    """A bare ``bot_id`` with no ``is_admin_bot`` renders the name only
    — the admin / most-privileged privilege claim must NOT leak onto a
    caller that didn't assert it (defense for future non-admin callers)."""
    block = P.format_session_context({
        "bot_id": "atlas",
        "surface": "telegram",
    })
    assert "Bot: atlas" in block
    assert "(admin)" not in block
    assert "most-privileged caller" not in block


def test_session_context_omits_bot_line_when_absent():
    """No bot_id → no Bot line at all. Existing callers that don't supply
    identity are byte-unchanged, and no caller is implicitly handed an
    admin claim."""
    block = P.format_session_context({"authority": "ask", "surface": "admin_ui"})
    assert "Bot:" not in block
    assert "most-privileged caller" not in block


def test_session_context_admin_framing_gated_on_is_admin_bot_flag():
    """is_admin_bot must be the ONLY switch for the privilege framing —
    a falsy flag with a truthy bot_id renders the plain name. Encodes
    that the privilege claim is gated, never default-on."""
    block = P.format_session_context({
        "bot_id": "evolve",
        "is_admin_bot": False,
    })
    assert "Bot: evolve" in block
    assert "(admin)" not in block
    assert "most-privileged caller" not in block


# ─────────────────────────────────────────────────────────────────────────────
# /api/home/chat injects the admin-bot identity into session_context
# ─────────────────────────────────────────────────────────────────────────────


def _make_app(tmp_path: Path, primary_id: str = "evolve"):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "primary": primary_id,
        "bots": {primary_id: {"role": "primary"}},
        "members": [primary_id],
    }))
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from evolve_admin.web.server import create_app
    return create_app(net)


def _patch_proxy(monkeypatch):
    """Capture send_to_evo kwargs so the test can assert on the
    session_context dict the route handler built."""
    calls: list[dict] = []
    from evolve_admin.evo import proxy as _proxy

    def fake_send(
        message, *, session_id, network_path,
        page_context=None, session_context=None, **kw,
    ):
        calls.append({
            "message": message,
            "session_context": session_context,
        })
        return _proxy.ProxyResult(
            text="ok", session_id=session_id, model="m",
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    return calls


def test_route_injects_admin_bot_identity_into_session_context(
    tmp_path, monkeypatch,
):
    """The admin-UI tray route stamps the bot identity into the
    session_context it hands the proxy — without the frontend needing to
    send it. This is the regression lock for the self-context bug: every
    turn injects the assistant's own identity."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "why is atlas failing backup?",
        "page_context": {"page_id": "backup", "surface": "admin_ui"},
    })
    assert len(calls) == 1
    sc = calls[0]["session_context"]
    assert sc is not None
    assert sc["bot_id"] == "evolve"
    assert sc["is_admin_bot"] is True


def test_route_identity_reaches_rendered_block_end_to_end(
    tmp_path, monkeypatch,
):
    """End-to-end: the keys the route injects actually render into the
    <session-context> block the model sees. Guards against a future
    rename desyncing the route's keys from the proxy's reader."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "hi",
        "page_context": {"page_id": "alerts", "surface": "admin_ui"},
    })
    sc = calls[0]["session_context"]
    block = P.format_session_context(sc)
    assert "Bot: evolve (admin)" in block
    assert "most-privileged caller" in block


def test_route_resolves_bot_id_from_network_not_hardcoded(
    tmp_path, monkeypatch,
):
    """Pod-portability: the injected identity must name the pod's REAL
    primary bot (network.json is the only source of truth — memory note
    ``feedback_explicit_pod_membership``), not a hardcoded "evolve".
    A pod whose admin/primary bot is named ``aurora`` must see
    ``Bot: aurora (admin)`` — naming "evolve" there would be the same
    confabulation this PR kills, inverted. The admin *privilege* flag
    stays true regardless (it's conferred by the surface, not the
    name)."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path, primary_id="aurora")
    app.test_client().post("/api/home/chat", json={
        "message": "hi",
        "page_context": {"page_id": "alerts", "surface": "admin_ui"},
    })
    sc = calls[0]["session_context"]
    assert sc["bot_id"] == "aurora"
    assert sc["is_admin_bot"] is True
    block = P.format_session_context(sc)
    assert "Bot: aurora (admin)" in block
    # The bot NAME must not be hardcoded to "evolve" — name forms only.
    # (The capability line legitimately references the `evolve` macOS
    # SERVICE user, which is a fixed system fact on every pod regardless
    # of the bot's name — CLAUDE.md "Runtime Context" — so a blanket
    # `"evolve" not in block` would wrongly reject that correct mention.)
    assert "Bot: evolve" not in block
    assert "the evolve bot" not in block
    assert "evolve (admin)" not in block
