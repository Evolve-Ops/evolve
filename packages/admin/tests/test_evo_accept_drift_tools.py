"""tests/test_evo_accept_drift_tools.py — pod_state.config_drift +
action.security.accept_drift guards.

These two tools were added in response to a 2026-05-20 transcript
where the operator on the Security → Backups subtab asked "can you
accept all of the current configs as baseline?" Evo, lacking both
an enumeration tool and an action tool for this specific
mechanism, confabulated that "accept as baseline" maps to
audit-finding Mute (a different concept on the same page) and to
``audit.py --reset-baselines`` (a different baseline entirely).

Structural fix:

  * ``pod_state.config_drift`` reads ``/api/backup/cloud/status``
    and projects the drift list.
  * ``action.security.accept_drift(bot_id)`` POSTs to
    ``/api/security/accept-drift``. WRITE_RISKY because it shifts
    a security-relevant invariant (the trusted baseline).

Tests stub ``urllib.request.urlopen`` so we exercise the HTTP path
without standing up a Flask app. Same fixture pattern as the
``action_plugin`` + ``pod_state.audit`` HTTP-fallback tests.
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
    """Mimics urllib's HTTPResponse just enough for HTTP-wrapper tools."""

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
        "bots": {
            "team_bot_a": {"role": "member"},
            "team_bot_c": {"role": "member"},
            "evo": {"role": "primary"},
        },
    }))
    return p


# ─── pod_state.config_drift ──────────────────────────────────────────────────


def test_config_drift_projects_backup_status(monkeypatch, tmp_path):
    """Happy path: tool GETs /api/backup/cloud/status and splits
    bots into drifted_bots[] + clean_bots[] based on drifted_keys."""
    from evolve_admin.evo.tools import pod_state_config_drift
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeUrlopenResp(json.dumps({
            "bots": {
                "team_bot_a": {
                    "drifted_keys": ["tools"],
                    "stale": False,
                    "last_backup": "2026-05-19T08:00:00Z",
                },
                "team_bot_c": {
                    "drifted_keys": ["tools", "agents"],
                    "stale": True,
                    "last_backup": "2026-05-15T08:00:00Z",
                },
                "evo": {"drifted_keys": [], "stale": False},
            },
        }))

    monkeypatch.setattr(
        pod_state_config_drift.urllib.request, "urlopen", _fake_urlopen,
    )

    r = pod_state_config_drift._config_drift_handler(network_path=network_path)
    assert r["ok"] is True
    assert r["total_drifted"] == 2
    assert r["total_clean"] == 1
    # Drifted bots sorted by bot_id deterministically.
    ids = [b["bot_id"] for b in r["drifted_bots"]]
    assert ids == ["team_bot_a", "team_bot_c"]
    # Each drifted item carries the keys + stale flag.
    team_bot_a = next(b for b in r["drifted_bots"] if b["bot_id"] == "team_bot_a")
    assert team_bot_a["drifted_keys"] == ["tools"]
    assert team_bot_a["stale_backup"] is False
    team_bot_c = next(b for b in r["drifted_bots"] if b["bot_id"] == "team_bot_c")
    assert team_bot_c["stale_backup"] is True
    # Clean bots present too.
    assert r["clean_bots"] == ["evo"]
    # Verify URL.
    assert captured["url"] == "http://test-host:5050/api/backup/cloud/status"
    assert captured["method"] == "GET"


def test_config_drift_handles_no_drift(monkeypatch, tmp_path):
    """When all bots are clean, total_drifted=0 and drifted_bots is
    an empty list (not None — that distinction matters for the model)."""
    from evolve_admin.evo.tools import pod_state_config_drift
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        pod_state_config_drift.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "bots": {
                "team_bot_a": {"drifted_keys": [], "stale": False},
                "evo": {"drifted_keys": [], "stale": False},
            },
        })),
    )

    r = pod_state_config_drift._config_drift_handler(network_path=network_path)
    assert r["ok"] is True
    assert r["drifted_bots"] == []
    assert r["total_drifted"] == 0
    assert "team_bot_a" in r["clean_bots"] and "evo" in r["clean_bots"]


def test_config_drift_surfaces_http_error(monkeypatch, tmp_path):
    """When the admin server returns 500, the tool returns ok=False
    with the body surfaced — model can reason about the failure."""
    from evolve_admin.evo.tools import pod_state_config_drift
    network_path = _seed_network(tmp_path)

    def _fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {},
            BytesIO(b'{"error":"heal not importable"}'),
        )

    monkeypatch.setattr(
        pod_state_config_drift.urllib.request, "urlopen", _fake_urlopen,
    )
    r = pod_state_config_drift._config_drift_handler(network_path=network_path)
    assert r["ok"] is False
    assert "500" in r["error"]


def test_config_drift_handles_unreachable_server(monkeypatch, tmp_path):
    """URLError = network down. Surface the reason so the model can
    say "the admin server appears unreachable" rather than a generic
    'something failed'."""
    from evolve_admin.evo.tools import pod_state_config_drift
    network_path = _seed_network(tmp_path)

    def _fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(
        pod_state_config_drift.urllib.request, "urlopen", _fake_urlopen,
    )
    r = pod_state_config_drift._config_drift_handler(network_path=network_path)
    assert r["ok"] is False
    assert "unreachable" in r["error"]
    assert "Connection refused" in r["error"]


def test_config_drift_tool_registered():
    """Tool is at the expected name and tier."""
    from evolve_admin.evo.tools import RiskTier, lookup
    t = lookup("pod_state.config_drift")
    assert t is not None
    assert t.risk_tier == RiskTier.READ
    assert t.validate is None


# ─── action.security.accept_drift ────────────────────────────────────────────


def test_accept_drift_validate_rejects_unknown_bot(tmp_path):
    """Bot not in network.json → validate fails with a clear reason."""
    from evolve_admin.evo.tools import action_security
    network_path = _seed_network(tmp_path)
    r = action_security._accept_drift_validate(
        network_path=network_path, bot_id="ghost",
    )
    assert r["ok"] is False
    assert "ghost" in r["reason"]


def test_accept_drift_validate_rejects_clean_bot(monkeypatch, tmp_path):
    """Bot exists but has no drift → validate refuses the action.
    Better than letting the operator click 'confirm' just to learn
    there was nothing to accept."""
    from evolve_admin.evo.tools import action_security
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_security.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "botId": "team_bot_a", "drifted_keys": [],
        })),
    )

    r = action_security._accept_drift_validate(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["ok"] is False
    assert "no config drift" in r["reason"]


def test_accept_drift_validate_accepts_drifted_bot(monkeypatch, tmp_path):
    """Bot exists + has drift → validate returns ok with the keys
    in context so the proxy can name them in the confirm button."""
    from evolve_admin.evo.tools import action_security
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_security.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "botId": "team_bot_a", "drifted_keys": ["tools"],
        })),
    )

    r = action_security._accept_drift_validate(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["ok"] is True
    assert r["context"]["drifted_keys"] == ["tools"]


def test_accept_drift_handler_posts_to_admin(monkeypatch, tmp_path):
    """Happy path: tool POSTs /api/security/accept-drift with
    {botId} and surfaces the message + verify_via."""
    from evolve_admin.evo.tools import action_security
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({
            "ok": True, "botId": "team_bot_a",
            "message": "baseline committed locally",
        }))

    monkeypatch.setattr(
        action_security.urllib.request, "urlopen", _fake_urlopen,
    )

    r = action_security._accept_drift_handler(
        network_path=network_path, bot_id="team_bot_a", reason="test",
    )
    assert r["ok"] is True
    assert r["bot_id"] == "team_bot_a"
    assert r["message"] == "baseline committed locally"
    # verify_via points at pod_state.config_drift — model uses this
    # to confirm the bot moved from drifted_bots[] → clean_bots[].
    assert r["verify_via"]["tool"] == "pod_state.config_drift"

    # Request shape.
    assert captured["url"] == "http://test-host:5050/api/security/accept-drift"
    assert captured["method"] == "POST"
    assert captured["body"] == {"botId": "team_bot_a"}


def test_accept_drift_handler_propagates_admin_error(monkeypatch, tmp_path):
    """When the admin server returns ok=False, surface that error in
    the tool response — don't claim success."""
    from evolve_admin.evo.tools import action_security
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_security.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "ok": False, "error": "workspace is not a git repo (run backup-init first)",
        })),
    )

    r = action_security._accept_drift_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["ok"] is False
    assert "not a git repo" in r["error"]


def test_accept_drift_handler_rejects_unknown_bot(tmp_path):
    """Defense in depth — even with validate bypassed, the handler
    must refuse for unknown bots."""
    from evolve_admin.evo.tools import action_security
    network_path = _seed_network(tmp_path)
    r = action_security._accept_drift_handler(
        network_path=network_path, bot_id="ghost",
    )
    assert r["ok"] is False
    assert "ghost" in r["error"]


def test_accept_drift_tool_registered():
    """Tool is at the expected name with WRITE_RISKY tier."""
    from evolve_admin.evo.tools import RiskTier, lookup
    t = lookup("action.security.accept_drift")
    assert t is not None, "action.security.accept_drift not registered"
    assert t.risk_tier == RiskTier.WRITE_RISKY, (
        "accept_drift should be WRITE_RISKY — it shifts a security-"
        "relevant invariant (the trusted baseline)."
    )
    assert t.validate is not None


# ─── Bridge contract: handlers accept network_path ───────────────────────────


def test_drift_handlers_accept_network_path():
    """The mcp_server bridge inspects handler signatures to decide
    whether to inject ``network_path``. Both new handlers must take
    that kwarg so the bridge wires them correctly."""
    import inspect
    from evolve_admin.evo.tools import action_security, pod_state_config_drift
    assert "network_path" in inspect.signature(
        pod_state_config_drift._config_drift_handler).parameters
    assert "network_path" in inspect.signature(
        action_security._accept_drift_handler).parameters
