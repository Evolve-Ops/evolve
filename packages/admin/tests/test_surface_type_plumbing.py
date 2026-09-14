"""tests/test_surface_type_plumbing.py — Phase 1 of the surface-aware
help-style spec (internal/spec-surface-aware-help-style-2026-05-22.md).

Verifies the surface_type detection plumbs through three layers:

  1. Client side (skipped here — JS) — the home_chat POST includes
     ``surface_type`` when the client supplies it.
  2. Route handler (``home_chat_routes.py``) — receives
     ``page_context.surface_type`` from the body, threads it into
     ``session_context`` for the proxy.
  3. Proxy (``proxy.py``) — ``format_session_context`` renders a
     ``Surface: admin_ui / laptop`` line; ``format_page_context``
     emits a ``surface_type="laptop"`` attribute.

Telegram path is exercised by the no-surface tests: when neither
``surface`` nor ``surface_type`` is supplied, the session block
omits the Surface line entirely (which preserves the legacy
"absent ⇒ telegram" inference for callers that haven't been
updated yet).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import proxy as P  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# format_session_context renders the Surface line
# ─────────────────────────────────────────────────────────────────────────────


def test_session_context_renders_surface_laptop():
    """When surface + surface_type are supplied, the block has a
    one-line ``Surface:`` row formatted as ``<surface> / <surface_type>``."""
    block = P.format_session_context({
        "surface": "admin_ui",
        "surface_type": "laptop",
    })
    assert "Surface: admin_ui / laptop" in block


def test_session_context_renders_surface_mobile():
    """Mobile is the hard-block surface — must be legible in the
    block so the model can gate CLI emission on it."""
    block = P.format_session_context({
        "surface": "admin_ui",
        "surface_type": "mobile",
    })
    assert "Surface: admin_ui / mobile" in block


def test_session_context_renders_surface_telegram_without_type():
    """Telegram has no viewport — surface_type is omitted. The Surface
    line still renders so the model has an explicit anchor."""
    block = P.format_session_context({
        "surface": "telegram",
    })
    assert "Surface: telegram" in block
    # No trailing "/ <something>" when surface_type is absent
    assert "telegram /" not in block


def test_session_context_omits_surface_line_when_neither_set():
    """Absence of both surface + surface_type → no Surface line. This
    preserves the legacy "absent page-context ⇒ telegram" inference
    for callers that haven't been updated to set surface explicitly."""
    block = P.format_session_context({"authority": "ask"})
    assert "Surface:" not in block


def test_session_context_renders_surface_when_only_surface_set():
    """Surface alone (no surface_type) still renders. Defensive — a
    future caller might supply only ``surface`` for a surface that
    has no viewport concept."""
    block = P.format_session_context({"surface": "ide"})
    assert "Surface: ide" in block
    assert "ide /" not in block


# ─────────────────────────────────────────────────────────────────────────────
# format_page_context emits surface_type as a sibling attribute
# ─────────────────────────────────────────────────────────────────────────────


def test_page_context_surface_type_attribute_when_supplied():
    """``surface_type`` lands as the second XML attribute on the
    <page-context> tag, sitting next to ``surface``. The model has a
    cheap scan-target without descending into the body."""
    block = P.format_page_context({
        "page_id": "alerts",
        "summary": "x",
        "surface": "admin_ui",
        "surface_type": "mobile",
    })
    assert 'surface="admin_ui"' in block
    assert 'surface_type="mobile"' in block
    # surface_type follows surface
    assert block.index("surface=") < block.index("surface_type=")


def test_page_context_no_surface_type_attribute_when_absent():
    """Backward compatibility — when surface_type is omitted (legacy
    callers, tests, Telegram-like surfaces), the attribute is absent
    so the existing one-attribute block shape is preserved."""
    block = P.format_page_context({
        "page_id": "alerts",
        "summary": "x",
    })
    assert 'surface="admin_ui"' in block
    assert "surface_type" not in block


# ─────────────────────────────────────────────────────────────────────────────
# /api/home/chat route plumbs surface_type into the proxy session_context
# ─────────────────────────────────────────────────────────────────────────────


def _make_app(tmp_path: Path):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {"evolve": {"role": "primary"}},
        "members": ["evolve"],
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
            "session_id": session_id,
            "page_context": page_context,
            "session_context": session_context,
        })
        return _proxy.ProxyResult(
            text="ok", session_id=session_id, model="m",
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    return calls


def test_route_threads_surface_type_to_proxy_session_context(
    tmp_path, monkeypatch,
):
    """The client sends ``page_context.surface_type``; the route hands
    it to the proxy via ``session_context["surface_type"]`` so the
    session-context block can render the canonical surface line."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "hi",
        "page_context": {
            "page_id": "alerts",
            "surface": "admin_ui",
            "surface_type": "mobile",
        },
    })
    assert len(calls) == 1
    sc = calls[0]["session_context"]
    assert sc is not None
    assert sc["surface"] == "admin_ui"
    assert sc["surface_type"] == "mobile"


def test_route_defaults_surface_type_to_laptop_when_missing(
    tmp_path, monkeypatch,
):
    """When the client doesn't supply ``surface_type`` (legacy frontend,
    direct CLI POST), the route defaults to ``laptop``. Spec §2.2: the
    conservative bias intentionally favors laptop so a missed
    classification suppresses CLI rather than wrongly emitting it on
    a phone."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "hi",
        "page_context": {"page_id": "alerts"},
    })
    assert len(calls) == 1
    sc = calls[0]["session_context"]
    assert sc["surface_type"] == "laptop"


def test_route_surface_type_unknown_value_falls_to_laptop(
    tmp_path, monkeypatch,
):
    """If a malicious / malformed client supplies an unexpected
    surface_type, the route normalizes to ``laptop`` rather than
    propagating arbitrary strings into the session-context block.
    Defense-in-depth — the model should only ever see the two
    documented values."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "hi",
        "page_context": {
            "page_id": "alerts",
            "surface_type": "watch-os",  # not in the v1 taxonomy
        },
    })
    sc = calls[0]["session_context"]
    assert sc["surface_type"] == "laptop"
