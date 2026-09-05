"""tests/test_action_keys.py — action.keys.{add,rotate,remove} guards.

These tools wrap the admin server's ``/api/admin/keys/...`` HTTP
family. They close 10 Pattern B gaps from
``internal/audit-evo-tool-coverage-2026-06-02.md`` (keystore touches via
admin-ui HTTP).

The tools' load-bearing contract: NEVER echo a key value back in the
response, and NEVER write a raw key value into the audit log. The
audit log records ``"key_value": "[REDACTED]"`` literally. These
tests verify both invariants on the success path and on the error
paths.

Tests stub ``urllib.request.urlopen`` so we exercise the POST / DELETE
path without standing up a Flask app. Same fixture shape as
``test_action_plugin.py``.
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


_SECRET_LITERAL = "sk-test-this-is-the-secret-do-not-log-me-12345"
_BOT_ID = "team_bot_b"


class _FakeUrlopenResp:
    """Mimics urllib's HTTPResponse just enough for action_keys."""

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


def _read_audit_records(shared_dir: Path) -> list[dict]:
    """Read every audit record written to the keystore audit log,
    one per line."""
    p = shared_dir / "keys" / "keys-audit.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _assert_no_secret_anywhere(value, secret: str = _SECRET_LITERAL) -> None:
    """Recursively walk a value and assert the secret string never
    appears. Used to defend the no-echo invariant on every response
    and every audit-log record.

    The implementation is deliberately conservative: any dict / list /
    tuple / str gets inspected. If a future field accidentally carries
    the secret through (e.g. an admin-ui response body leaked back),
    this catches it.
    """
    if isinstance(value, str):
        assert secret not in value, (
            f"secret leaked into a string: {value!r}"
        )
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_no_secret_anywhere(k, secret)
            _assert_no_secret_anywhere(v, secret)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _assert_no_secret_anywhere(item, secret)
        return
    # bool / int / float / None — no string content, nothing to leak.


# ─── action.keys.add ─────────────────────────────────────────────────────────


def test_add_posts_to_admin_server(monkeypatch, tmp_path):
    """Happy path: tool POSTs to /api/admin/keys/<bot>/<provider>
    with key_value + key_type, returns success shape, NEVER echoes
    the key value back."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({
            "ok": True,
            "profile_id": "brave:api_key",
        }))

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fake_urlopen,
    )

    result = action_keys._add_handler(
        bot_id=_BOT_ID, provider="brave",
        key_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )

    # Success shape
    assert result["ok"] is True
    assert result["action"] == "add"
    assert result["bot_id"] == _BOT_ID
    assert result["provider"] == "brave"
    assert result["key_type"] == "api_key"
    assert result["profile_id"] == "brave:api_key"
    # Verify the route was hit with the secret in the body — the
    # secret SHOULD be in the request (we're delivering it to admin-ui),
    # but only there.
    assert captured["url"] == f"http://test-host:5050/api/admin/keys/{_BOT_ID}/brave"
    assert captured["method"] == "POST"
    assert captured["body"]["key_value"] == _SECRET_LITERAL
    assert captured["body"]["key_type"] == "api_key"

    # The crucial invariant: the secret MUST NOT appear in the response.
    _assert_no_secret_anywhere(result)
    # And the explicit redaction marker is present where a naive
    # consumer might look.
    assert result["applied"]["key_value"] == "[REDACTED]"


def test_add_audit_log_redacts_key_value(monkeypatch, tmp_path):
    """The audit-log entry MUST carry '[REDACTED]' for the key value,
    NEVER the actual secret."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "ok": True, "profile_id": "brave:api_key",
        })),
    )

    action_keys._add_handler(
        bot_id=_BOT_ID, provider="brave",
        key_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )

    records = _read_audit_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["action"] == "add"
    assert record["bot_id"] == _BOT_ID
    assert record["provider"] == "brave"
    assert record["key_value"] == "[REDACTED]"
    assert record["actor"] == "evo:action.keys.add"

    # Defense in depth: scan the ENTIRE record for any leakage.
    _assert_no_secret_anywhere(record)


def test_add_validate_rejects_empty_key_value():
    """Empty key_value is the most likely operator typo; surface it
    BEFORE the HTTP call so we don't write an empty profile."""
    from evolve_admin.evo.tools import action_keys
    result = action_keys._add_validate(
        bot_id=_BOT_ID, provider="brave", key_value="",
    )
    assert result["ok"] is False
    assert "key_value" in result["reason"]


def test_add_validate_rejects_whitespace_key_value():
    """Whitespace-only key_value gets caught at validate."""
    from evolve_admin.evo.tools import action_keys
    result = action_keys._add_validate(
        bot_id=_BOT_ID, provider="brave", key_value="   ",
    )
    assert result["ok"] is False


@pytest.mark.parametrize("bad_bot_id", [
    "",
    "a" * 65,
    "team_bot_b/etc/passwd",
    "team_bot_b;rm",
])
def test_add_validate_rejects_bad_bot_id(bad_bot_id):
    from evolve_admin.evo.tools import action_keys
    result = action_keys._add_validate(
        bot_id=bad_bot_id, provider="brave", key_value="ok",
    )
    assert result["ok"] is False


# ─── action.keys.rotate ──────────────────────────────────────────────────────


def test_rotate_posts_to_admin_server(monkeypatch, tmp_path):
    """Happy path: rotate POSTs to .../rotate with key_value, returns
    requires_restart + restart_endpoint, NEVER echoes the new value."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({
            "ok": True,
            "storage": "auth_profiles",
            "profile_id": "brave:api_key",
            "field_key": "api_key",
            "mirrored": True,
            "mirror_error": None,
            "requires_restart": True,
            "restart_endpoint": f"/api/admin/gateway/{_BOT_ID}/restart",
        }))

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fake_urlopen,
    )

    result = action_keys._rotate_handler(
        bot_id=_BOT_ID, provider="brave",
        new_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )

    assert result["ok"] is True
    assert result["action"] == "rotate"
    assert result["bot_id"] == _BOT_ID
    assert result["provider"] == "brave"
    assert result["profile_id"] == "brave:api_key"
    assert result["requires_restart"] is True
    assert result["restart_endpoint"] == f"/api/admin/gateway/{_BOT_ID}/restart"
    assert result["mirrored"] is True

    # Route hit + body shape
    assert captured["url"] == (
        f"http://test-host:5050/api/admin/keys/{_BOT_ID}/brave/rotate"
    )
    assert captured["method"] == "POST"
    assert captured["body"]["key_value"] == _SECRET_LITERAL

    # SECURITY: no secret anywhere in the response.
    _assert_no_secret_anywhere(result)
    assert result["previous"]["key_value"] == "[REDACTED]"
    assert result["applied"]["key_value"] == "[REDACTED]"


def test_rotate_audit_log_redacts_key_value(monkeypatch, tmp_path):
    """Audit log shows '[REDACTED]' even on the successful rotate."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "ok": True, "profile_id": "brave:api_key",
            "field_key": "api_key", "storage": "auth_profiles",
        })),
    )

    action_keys._rotate_handler(
        bot_id=_BOT_ID, provider="brave",
        new_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )

    records = _read_audit_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["action"] == "rotate"
    assert record["key_value"] == "[REDACTED]"
    _assert_no_secret_anywhere(record)


def test_rotate_token_pair_forwards_field_key(monkeypatch, tmp_path):
    """For token_pair providers, the operator's field_key MUST land
    in the request body — the admin route requires it and refuses to
    guess which field of a multi-field credential to rotate."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({
            "ok": True, "profile_id": "slack:token_pair",
            "field_key": "bot_token", "storage": "auth_profiles",
        }))

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fake_urlopen,
    )

    action_keys._rotate_handler(
        bot_id=_BOT_ID, provider="slack",
        new_value=_SECRET_LITERAL,
        field_key="bot_token",
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert captured["body"]["field_key"] == "bot_token"


def test_rotate_admin_error_surfaces_valid_fields(monkeypatch, tmp_path):
    """When the admin route returns 'field_key required for token_pair
    providers' with a valid_fields list, the tool MUST surface that
    list back to the model so it can re-prompt the operator with the
    right shape."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "error": "field_key required for token_pair providers",
            "valid_fields": ["app_token", "bot_token", "user_token"],
        })),
    )

    result = action_keys._rotate_handler(
        bot_id=_BOT_ID, provider="slack",
        new_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "field_key" in result["error"]
    assert result["valid_fields"] == ["app_token", "bot_token", "user_token"]


def test_rotate_validate_rejects_invalid_storage():
    """The storage enum is small (3 values); a typo here would silently
    write the wrong file. Surface a clear validation error."""
    from evolve_admin.evo.tools import action_keys
    result = action_keys._rotate_validate(
        bot_id=_BOT_ID, provider="brave", new_value="ok",
        storage="dotnev",  # typo
    )
    assert result["ok"] is False
    assert "storage" in result["reason"]


# ─── action.keys.remove ──────────────────────────────────────────────────────


def test_remove_sends_delete(monkeypatch, tmp_path):
    """Happy path: remove sends DELETE to /api/admin/keys/<bot>/<provider>
    and returns confirmation. Body carries optional profile_id."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({"ok": True}))

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fake_urlopen,
    )

    result = action_keys._remove_handler(
        bot_id=_BOT_ID, provider="brave",
        profile_id="brave:api_key",
        shared_dir=tmp_path,
        network_path=network_path,
    )

    assert result["ok"] is True
    assert result["action"] == "remove"
    assert result["bot_id"] == _BOT_ID
    assert result["provider"] == "brave"
    assert result["profile_id"] == "brave:api_key"
    assert captured["url"] == f"http://test-host:5050/api/admin/keys/{_BOT_ID}/brave"
    assert captured["method"] == "DELETE"
    assert captured["body"] == {"profile_id": "brave:api_key"}


def test_remove_without_profile_id_clears_all(monkeypatch, tmp_path):
    """Without profile_id, the route clears every profile for that
    provider — the 'Disconnect' semantic. Tool should not invent a
    profile_id."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeUrlopenResp(json.dumps({"ok": True}))

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fake_urlopen,
    )

    result = action_keys._remove_handler(
        bot_id=_BOT_ID, provider="brave",
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert result["ok"] is True
    assert captured["body"] == {}


def test_remove_audit_log_records_action(monkeypatch, tmp_path):
    """Removal doesn't carry a key_value but should still log."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({"ok": True})),
    )

    action_keys._remove_handler(
        bot_id=_BOT_ID, provider="brave",
        profile_id="brave:api_key",
        shared_dir=tmp_path,
        network_path=network_path,
    )

    records = _read_audit_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["action"] == "remove"
    # Even though no key_value was sent in the request, we still write
    # the redacted marker for shape consistency across the family.
    assert record["key_value"] == "[REDACTED]"


# ─── Shared HTTP failure modes ──────────────────────────────────────────────


def test_add_handles_http_500(monkeypatch, tmp_path):
    """A 5xx from admin-ui surfaces as a structured error including the
    server's response body — the operator needs the diagnosis pointer."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    def _fivexx(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {},
            BytesIO(b"auth-profiles.json write failed"),
        )

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fivexx,
    )

    result = action_keys._add_handler(
        bot_id=_BOT_ID, provider="brave",
        key_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "HTTP 500" in result["error"]
    assert "auth-profiles" in result["error"]
    # Even on error, the secret MUST NOT appear in the response.
    _assert_no_secret_anywhere(result)


def test_rotate_handles_http_500(monkeypatch, tmp_path):
    """Mirror for rotate."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    def _fivexx(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {},
            BytesIO(b"mirror failed"),
        )

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _fivexx,
    )

    result = action_keys._rotate_handler(
        bot_id=_BOT_ID, provider="brave",
        new_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "HTTP 500" in result["error"]
    _assert_no_secret_anywhere(result)


def test_add_handles_admin_unreachable(monkeypatch, tmp_path):
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    def _refuse(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen", _refuse,
    )

    result = action_keys._add_handler(
        bot_id=_BOT_ID, provider="brave",
        key_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"].lower()
    _assert_no_secret_anywhere(result)


def test_add_requires_network_path():
    """Without network_path, the tool can't resolve the admin URL —
    surface that without crashing."""
    from evolve_admin.evo.tools import action_keys
    result = action_keys._add_handler(
        bot_id=_BOT_ID, provider="brave", key_value="ok",
    )
    assert result["ok"] is False
    assert (
        "network_path" in result["error"].lower()
        or "admin base URL" in result["error"]
    )


# ─── Unknown / malformed inputs ─────────────────────────────────────────────


def test_add_validate_rejects_bad_provider():
    """Provider with shell metacharacters / spaces is rejected. The
    error message MUST list known providers so the model can recover."""
    from evolve_admin.evo.tools import action_keys
    result = action_keys._add_validate(
        bot_id=_BOT_ID, provider="brave;rm",
        key_value="ok",
    )
    assert result["ok"] is False
    assert "provider" in result["reason"]
    assert "known_providers" in result
    # The known list should include common items so the LLM can recover.
    known = result["known_providers"]
    assert "brave" in known
    assert "anthropic" in known
    assert "slack" in known


def test_rotate_handler_surfaces_admin_error_verbatim(monkeypatch, tmp_path):
    """ok:false from admin-ui flows through with the error message
    intact — the operator needs the original wording."""
    from evolve_admin.evo.tools import action_keys
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        action_keys.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp(json.dumps({
            "error": "value rejected: looks like a placeholder ('test-key')",
        })),
    )

    result = action_keys._rotate_handler(
        bot_id=_BOT_ID, provider="brave",
        new_value=_SECRET_LITERAL,
        shared_dir=tmp_path,
        network_path=network_path,
    )
    assert result["ok"] is False
    assert "placeholder" in result["error"]


# ─── Tool registration + auth tier ──────────────────────────────────────────


def test_action_keys_tools_registered():
    """All three tools must be in the registry under their canonical
    dotted names — the chat surface won't offer them otherwise."""
    from evolve_admin.evo.tools import lookup
    add = lookup("action.keys.add")
    rotate = lookup("action.keys.rotate")
    remove = lookup("action.keys.remove")
    assert add is not None
    assert rotate is not None
    assert remove is not None


def test_action_keys_tools_are_write_risky():
    """All three are write_risky — credential writes can take a bot
    offline. The admin-UI proxy uses the tier to decide whether to
    auto-execute or render a confirm button; write_risky requires
    'auto' authority to auto-execute and shows a button under 'ask'.
    This is the 'admin-only' classification from the audit applied at
    the tier level."""
    from evolve_admin.evo.tools import lookup
    for name in ("action.keys.add", "action.keys.rotate", "action.keys.remove"):
        tool = lookup(name)
        assert tool is not None
        assert tool.risk_tier.name == "WRITE_RISKY", (
            f"{name}: expected WRITE_RISKY, got {tool.risk_tier.name}"
        )
        # Non-read tier ⇒ validate is required (enforced in __init__,
        # so this should always hold — explicit check is defense in
        # depth).
        assert tool.validate is not None


def test_action_keys_handlers_accept_network_path_via_bridge():
    """The MCP bridge inspects the handler signature and injects
    ``network_path`` when present. Each handler MUST accept it; if not,
    the bridge silently runs the tool with no admin-server reach."""
    import inspect
    from evolve_admin.evo.tools import action_keys
    for fn in (
        action_keys._add_handler,
        action_keys._rotate_handler,
        action_keys._remove_handler,
    ):
        sig = inspect.signature(fn)
        assert "network_path" in sig.parameters, (
            f"{fn.__name__} lacks network_path parameter — the MCP "
            f"bridge can't inject the admin URL and the tool will be "
            f"useless in production."
        )
        # Same for shared_dir — the audit log won't land without it.
        assert "shared_dir" in sig.parameters, (
            f"{fn.__name__} lacks shared_dir parameter — the bridge "
            f"can't inject it and audit-log writes will silently drop."
        )
