"""tests/test_action_plugin.py — action.plugin.{enable,disable} guards.

These tools wrap the admin server's ``/api/plugins-admin/propose-*``
HTTP endpoints. They're the canonical evo-driven path for plugin
management — created in response to a 2026-05-20 operator transcript
where evo, lacking these tools, fabricated a ``sudo -u team_bot_b python3
-c "..."`` shell snippet instead of pointing the operator at the
admin UI's Plugins page.

Tests stub ``urllib.request.urlopen`` so we exercise the POST path
without standing up a Flask app. Same fixture pattern as the
``pod_state.audit`` HTTP-fallback tests.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


class _FakeUrlopenResp:
    """Mimics urllib's HTTPResponse just enough for action_plugin."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _seed_network(tmp_path: Path, admin_base_url: str = "http://test-host:5050") -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "adminBaseUrl": admin_base_url,
    }))
    return p


# ── action.plugin.enable ─────────────────────────────────────────────────────


def test_enable_posts_to_admin_server(monkeypatch, tmp_path):
    """Happy path: tool POSTs to /api/plugins-admin/propose-enable
    with the right body shape and surfaces the returned proposal id."""
    from evolve_admin.evo.tools import action_plugin
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["content_type"] = req.headers.get("Content-type")
        return _FakeUrlopenResp(json.dumps({
            "ok": True,
            "proposal": {"id": "prop-abc123", "status": "pending"},
        }))

    monkeypatch.setattr(
        action_plugin.urllib.request, "urlopen", _fake_urlopen,
    )

    result = action_plugin._enable_handler(
        bot_id="team_bot_b", plugin_name="discord",
        network_path=network_path,
    )
    assert result["ok"] is True
    assert result["bot_id"] == "team_bot_b"
    assert result["plugin_name"] == "discord"
    assert result["proposal_id"] == "prop-abc123"
    assert result["proposal_status"] == "pending"
    # The verify_via hint points at the proposals.pending read tool
    # so the model can confirm the proposal landed.
    assert result["verify_via"]["tool"] == "pod_state.proposals.pending"

    # Verify the request shape.
    assert captured["url"] == "http://test-host:5050/api/plugins-admin/propose-enable"
    assert captured["method"] == "POST"
    assert captured["body"] == {"bot_id": "team_bot_b", "plugin_name": "discord"}
    assert captured["content_type"] == "application/json"


def test_enable_surfaces_admin_error(monkeypatch, tmp_path):
    """When the admin server returns ok:false, the tool surfaces the
    error message verbatim rather than swallowing it."""
    from evolve_admin.evo.tools import action_plugin
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_plugin.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "ok": False,
            "error": "plugin 'codex' is in baseline required_plugins; cannot disable",
        })),
    )

    result = action_plugin._enable_handler(
        bot_id="team_bot_b", plugin_name="codex",
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "baseline required_plugins" in result["error"]


def test_enable_handles_http_500(monkeypatch, tmp_path):
    """5xx from the admin server flows through as a structured error
    with the admin-server body included for diagnosis."""
    from evolve_admin.evo.tools import action_plugin
    network_path = _seed_network(tmp_path)

    def _fivexx(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {},
            BytesIO(b"unexpected error"),
        )

    monkeypatch.setattr(
        action_plugin.urllib.request, "urlopen", _fivexx,
    )

    result = action_plugin._enable_handler(
        bot_id="team_bot_b", plugin_name="discord",
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "HTTP 500" in result["error"]
    assert "unexpected error" in result["error"]


def test_enable_handles_admin_unreachable(monkeypatch, tmp_path):
    """Admin server down → URLError → structured error pointing at
    the URL so the operator can sanity-check config."""
    from evolve_admin.evo.tools import action_plugin
    network_path = _seed_network(tmp_path)

    def _refuse(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(
        action_plugin.urllib.request, "urlopen", _refuse,
    )

    result = action_plugin._enable_handler(
        bot_id="team_bot_b", plugin_name="discord",
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"].lower()
    assert "test-host:5050" in result["error"]


def test_enable_handles_non_json_response(monkeypatch, tmp_path):
    """Misconfigured admin server returning HTML / text shouldn't
    crash the tool."""
    from evolve_admin.evo.tools import action_plugin
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_plugin.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp("<html>oops</html>"),
    )

    result = action_plugin._enable_handler(
        bot_id="team_bot_b", plugin_name="discord",
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "non-JSON" in result["error"] or "JSON" in result["error"]


def test_enable_requires_network_path():
    """Without network_path (no bridge injection / direct test call),
    we can't resolve the admin base URL. Surface that clearly."""
    from evolve_admin.evo.tools import action_plugin
    result = action_plugin._enable_handler(
        bot_id="team_bot_b", plugin_name="discord",
    )
    assert result["ok"] is False
    assert "network_path" in result["error"].lower() or "admin base URL" in result["error"]


# ── Validation (the third-gate dry-run) ──────────────────────────────────────


@pytest.mark.parametrize("bad_bot_id", [
    "",                # empty
    "a" * 65,          # too long
    "team_bot_b/etc/passwd", # path traversal
    "team_bot_b team_bot_a",        # space
    "team_bot_b;rm",         # shell metachar
    "тink",            # unicode (cyrillic т) — security drag
])
def test_enable_validate_rejects_bad_bot_id(bad_bot_id):
    """Bot ids must be plain Unix-username shape. Anything weird gets
    surfaced as a clear validation error before the HTTP call fires.
    Defense in depth — the admin endpoint also validates."""
    from evolve_admin.evo.tools import action_plugin
    result = action_plugin._enable_validate(
        bot_id=bad_bot_id, plugin_name="discord",
    )
    assert result["ok"] is False
    assert "bot_id" in result["reason"]


@pytest.mark.parametrize("bad_plugin", [
    "",                # empty
    "a" * 65,          # too long
    "discord/etc",     # path traversal
    "discord bot",     # space
    "discord;evil",    # shell metachar
])
def test_enable_validate_rejects_bad_plugin_name(bad_plugin):
    """Plugin names follow the same Unix-shape rule."""
    from evolve_admin.evo.tools import action_plugin
    result = action_plugin._enable_validate(
        bot_id="team_bot_b", plugin_name=bad_plugin,
    )
    assert result["ok"] is False
    assert "plugin_name" in result["reason"]


def test_enable_validate_accepts_real_input():
    """Sanity: a normal call passes validation."""
    from evolve_admin.evo.tools import action_plugin
    assert action_plugin._enable_validate(
        bot_id="team_bot_b", plugin_name="discord",
    )["ok"] is True


# ── action.plugin.disable ────────────────────────────────────────────────────


def test_disable_posts_to_admin_server(monkeypatch, tmp_path):
    """Same flow as enable but the URL goes to propose-disable."""
    from evolve_admin.evo.tools import action_plugin
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({
            "ok": True,
            "proposal": {"id": "prop-xyz", "status": "pending"},
        }))

    monkeypatch.setattr(
        action_plugin.urllib.request, "urlopen", _fake_urlopen,
    )

    result = action_plugin._disable_handler(
        bot_id="team_bot_b", plugin_name="codex",
        network_path=network_path,
    )
    assert result["ok"] is True
    assert result["proposal_id"] == "prop-xyz"
    assert captured["url"] == "http://test-host:5050/api/plugins-admin/propose-disable"
    assert captured["body"] == {"bot_id": "team_bot_b", "plugin_name": "codex"}


def test_disable_validate_rejects_bad_input():
    """Same validation as enable."""
    from evolve_admin.evo.tools import action_plugin
    assert action_plugin._disable_validate(
        bot_id="", plugin_name="codex",
    )["ok"] is False
    assert action_plugin._disable_validate(
        bot_id="team_bot_b", plugin_name="",
    )["ok"] is False
    assert action_plugin._disable_validate(
        bot_id="team_bot_b", plugin_name="codex",
    )["ok"] is True


# ── Tool registration ────────────────────────────────────────────────────────


def test_action_plugin_tools_registered():
    """Both tools must be in the registry — the chat surface won't
    surface them as offerable to the model otherwise."""
    from evolve_admin.evo.tools import lookup
    enable = lookup("action.plugin.enable")
    disable = lookup("action.plugin.disable")
    assert enable is not None
    assert disable is not None
    # Both are write_safe — reversible (operator can reject the
    # proposal before the applier runs), bounded blast radius (one
    # plugin entry on one bot).
    assert enable.risk_tier.name == "WRITE_SAFE"
    assert disable.risk_tier.name == "WRITE_SAFE"
    # Both expose a validate() for the third-gate dry-run.
    assert enable.validate is not None
    assert disable.validate is not None


def test_action_plugin_handlers_accept_network_path_via_bridge():
    """The MCP bridge inspects the handler signature and injects
    ``network_path`` when present. The tool MUST accept it; otherwise
    the bridge silently runs the tool with no admin-server reach."""
    import inspect
    from evolve_admin.evo.tools import action_plugin
    for fn in (action_plugin._enable_handler, action_plugin._disable_handler):
        sig = inspect.signature(fn)
        assert "network_path" in sig.parameters, (
            f"{fn.__name__} lacks network_path parameter — the MCP "
            f"bridge can't inject the admin URL and the tool will be "
            f"useless in production."
        )
