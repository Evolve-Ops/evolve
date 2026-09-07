"""tests/test_action_scan.py — action.scan.run umbrella tool.

Umbrella tool that dispatches one-shot pod-wide / per-bot scan +
refresh triggers to the matching admin-ui HTTP route. Closes the
bulk of Pattern A in
``internal/audit-evo-tool-coverage-2026-06-02.md`` — 8 gaps via one
tool with a dispatch table.

Tests cover the contract:
  - registered in the tool registry under the expected dotted name
  - registered as WRITE_SAFE (re-runs are idempotent producer
    invocations; no config writes)
  - manifest renders with scope + kind in input_schema.required
  - each known (scope, kind) pair dispatches to the right route
    and surfaces the producer's response payload back
  - per-bot scope formats bot_id into the URL path
  - unknown scope → useful error listing 'pod' / 'bot:<id>'
  - unknown kind for a known scope → error listing valid kinds
  - HTTP 500 → error surfaces verbatim with http_status
  - HTTP unreachable → error surfaces verbatim without http_status
  - missing scope / kind → friendly error before any HTTP call
  - validate gate refuses unknown scope, unknown kind, missing bot_id
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import action_scan  # noqa: E402


_FAKE_BASE_URL = "http://127.0.0.1:5050"


# ─── Test fixtures ──────────────────────────────────────────────────────────


def _stub_factory(
    call_log: list[dict[str, Any]],
    *,
    status: int = 200,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
):
    """Build a post_json stub that records each call and returns a
    fixed (status, payload, error) triple. Defaults to 200 + an empty
    OK envelope.
    """
    def stub(url: str, body: dict[str, Any], timeout: int):
        call_log.append({"url": url, "body": body, "timeout": timeout})
        return status, payload if payload is not None else {"ok": True}, error
    return stub


# ─── Registration ────────────────────────────────────────────────────────────


def test_action_scan_run_is_registered():
    tool = _tools.lookup("action.scan.run")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    # Non-read tier MUST have validate (registry __post_init__ enforces
    # this; we re-check so a regression that removes validate but
    # keeps the WRITE_SAFE tier doesn't slip through).
    assert tool.validate is not None


def test_action_scan_run_in_manifest():
    """Manifest renders with the standard shape — name, description,
    input_schema with `scope` and `kind` required."""
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "action.scan.run"),
        None,
    )
    assert entry is not None
    assert "scope" in entry["input_schema"]["required"]
    assert "kind" in entry["input_schema"]["required"]
    assert "scope" in entry["input_schema"]["properties"]
    assert "kind" in entry["input_schema"]["properties"]


# ─── Per-(scope, kind) dispatch ──────────────────────────────────────────────


_POD_DISPATCH_EXPECTATIONS = [
    # (kind,                expected_path)
    ("recommendations",     "/api/better/refresh"),
    ("signals",             "/api/signals/refresh"),
    ("infra_audit",         "/api/pod-health/infra-audit/run"),
    ("integrations",        "/api/integrations/scan"),
    ("plugins",             "/api/plugins-admin/scan"),
    ("mcp",                 "/api/mcp-admin/scan"),
    ("content",             "/api/content-scan/scan"),
    ("permissions",         "/api/permissions/scan"),
    ("hooks",               "/api/hooks-admin/scan"),
]


@pytest.mark.parametrize("kind,expected_path", _POD_DISPATCH_EXPECTATIONS)
def test_pod_dispatch_routes_to_correct_endpoint(kind: str, expected_path: str):
    """Every (pod, kind) pair POSTs to its mapped route and returns
    ok=True with the producer's payload surfaced under `result`."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls, payload={
        "ok": True,
        "bots_checked": 7,
        "findings_count": 3,
    })

    result = action_scan._handler(
        scope="pod",
        kind=kind,
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True, result
    assert result["scope"] == "pod"
    assert result["kind"] == kind
    # Producer's payload makes it back so the model can quote counts.
    assert result["result"]["bots_checked"] == 7
    assert result["result"]["findings_count"] == 3
    assert "message" in result
    assert "ran_at" in result

    # Exactly one POST hit the expected URL.
    assert len(calls) == 1
    assert calls[0]["url"] == f"{_FAKE_BASE_URL}{expected_path}"


def test_bot_scope_dispatch_applications_scan_includes_bot_id():
    """Per-bot scope formats the bot_id into the URL via a query
    parameter (matching the /api/applications/scan?bot=<id> shape)."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls, payload={"status": "running"})

    result = action_scan._handler(
        scope="bot:team-bot-a",
        kind="applications",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["scope"] == "bot:team-bot-a"
    assert result["kind"] == "applications"
    assert len(calls) == 1
    assert calls[0]["url"] == (
        f"{_FAKE_BASE_URL}/api/applications/scan?bot=team-bot-a"
    )


def test_bot_scope_url_encodes_bot_id():
    """Defensively url-encodes the bot id — otherwise a stray '/' or
    '?' could re-route the request."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    action_scan._handler(
        scope="bot:weird/id with space",
        kind="applications",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert len(calls) == 1
    # The bot id is url-encoded → '/' becomes %2F, ' ' becomes %20.
    assert "weird%2Fid%20with%20space" in calls[0]["url"]
    assert "/api/applications/scan?bot=" in calls[0]["url"]


# ─── Unknown scope / kind ────────────────────────────────────────────────────


def test_unknown_scope_returns_useful_error():
    """Bogus scope → error mentions valid scopes, no HTTP call."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    result = action_scan._handler(
        scope="cluster",  # not 'pod' or 'bot:<id>'
        kind="signals",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "unknown scope" in result["error"].lower()
    assert "pod" in result["error"].lower()
    assert "valid_options" in result
    # Route was NOT called.
    assert calls == []


def test_unknown_kind_returns_useful_error_with_valid_list():
    """Bogus kind → error enumerates valid kinds for that scope, no
    HTTP call."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    result = action_scan._handler(
        scope="pod",
        kind="invented_kind",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()
    # The error lists the valid kinds for this scope so the model can
    # self-correct.
    assert "valid_options" in result
    assert "signals" in result["valid_options"]["pod"]
    assert "permissions" in result["valid_options"]["pod"]
    assert calls == []


def test_empty_scope_returns_useful_error():
    """Empty / missing scope → friendly error, no HTTP call."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    result = action_scan._handler(
        scope="",
        kind="signals",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "scope" in result["error"].lower()
    assert calls == []


def test_empty_kind_returns_useful_error():
    """Empty / missing kind → friendly error listing valid kinds."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    result = action_scan._handler(
        scope="pod",
        kind="",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "kind" in result["error"].lower()
    assert calls == []


def test_bot_scope_without_id_is_rejected():
    """`bot:` with no id → friendly error rather than a malformed URL."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    result = action_scan._handler(
        scope="bot:",
        kind="applications",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "unknown scope" in result["error"].lower()
    assert calls == []


def test_pod_kind_on_bot_scope_returns_error():
    """A pod-only kind requested under bot scope → error, not silent
    fallthrough. e.g. there is no per-bot 'signals' route."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(calls)
    result = action_scan._handler(
        scope="bot:team-bot-a",
        kind="signals",  # only valid under 'pod' scope
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()
    assert calls == []


# ─── HTTP error / transport surfaces ────────────────────────────────────────


def test_http_500_response_surfaces_status_and_body():
    """When admin-ui rejects the trigger (e.g. 500), the tool surfaces
    the HTTP status + the route's error envelope verbatim so the
    operator gets a clear pointer rather than a silent failure."""
    calls: list[dict[str, Any]] = []
    stub = _stub_factory(
        calls,
        status=500,
        payload={"ok": False, "error": "ImportError: plugins module not importable"},
    )
    result = action_scan._handler(
        scope="pod",
        kind="plugins",
        post_json=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "plugins module not importable" in result["error"]
    # Original scope/kind echoed for context.
    assert result["scope"] == "pod"
    assert result["kind"] == "plugins"


def test_transport_unreachable_returns_clear_error():
    """If admin-ui is down (URLError from urllib), surface the
    unreachable error string so the operator can act on it (e.g. by
    calling action.infra.daemon_restart)."""
    def unreachable_stub(url, body, timeout):
        return (
            0,
            None,
            f"admin server unreachable at {url}: Connection refused",
        )

    result = action_scan._handler(
        scope="pod",
        kind="signals",
        post_json=unreachable_stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"].lower()
    # Transport never produced an HTTP status code.
    assert "http_status" not in result
    # scope/kind echoed for context.
    assert result["scope"] == "pod"
    assert result["kind"] == "signals"


# ─── Validate gate ──────────────────────────────────────────────────────────


def test_validate_rejects_empty_scope():
    """Validate gate refuses missing `scope` before the proxy renders
    a confirm button."""
    result = action_scan._validate(scope="", kind="signals")
    assert result["ok"] is False
    assert "scope" in result["reason"].lower()


def test_validate_rejects_empty_kind():
    result = action_scan._validate(scope="pod", kind="")
    assert result["ok"] is False
    assert "kind" in result["reason"].lower()


def test_validate_rejects_unknown_scope():
    result = action_scan._validate(scope="cluster", kind="signals")
    assert result["ok"] is False
    assert "unknown scope" in result["reason"].lower()


def test_validate_rejects_unknown_kind():
    result = action_scan._validate(scope="pod", kind="not_a_kind")
    assert result["ok"] is False
    assert "unknown kind" in result["reason"].lower()


def test_validate_rejects_bot_scope_without_id():
    result = action_scan._validate(scope="bot:", kind="applications")
    assert result["ok"] is False
    # Empty `bot:` falls through to "unknown scope" since we only
    # accept `bot:<id>` where <id> is non-empty.
    assert "unknown scope" in result["reason"].lower() or "bot" in result["reason"].lower()


def test_validate_passes_well_formed_pod_request():
    """Happy path for the most common scope: pod + signals."""
    result = action_scan._validate(scope="pod", kind="signals")
    assert result["ok"] is True
    assert result["context"]["scope"] == "pod"
    assert result["context"]["kind"] == "signals"
    assert result["context"]["bot_id"] is None
    assert "human" in result["context"]


def test_validate_passes_well_formed_bot_request():
    """Happy path for per-bot scope."""
    result = action_scan._validate(
        scope="bot:team-bot-a", kind="applications",
    )
    assert result["ok"] is True
    assert result["context"]["scope"] == "bot"
    assert result["context"]["kind"] == "applications"
    assert result["context"]["bot_id"] == "team-bot-a"


# ─── Dispatch table completeness (regression guard) ─────────────────────────


def test_dispatch_table_covers_all_pattern_a_gaps():
    """Sentinel: the dispatch table must cover every Pattern A gap
    enumerated in the audit. If a row is removed by accident, this
    test surfaces it before the regression ships.

    Pattern A gaps (internal/audit-evo-tool-coverage-2026-06-02.md):
      - action.recommendations.refresh
      - action.signals.refresh
      - action.audit.run (infra)
      - action.scan.run (integrations, plugins, mcp, content,
                        permissions, hooks)
    Plus per-bot applications scan (parity with action.bot.rescan_apps).
    """
    required_pod_kinds = {
        "recommendations", "signals", "infra_audit",
        "integrations", "plugins", "mcp",
        "content", "permissions", "hooks",
    }
    actual_pod_kinds = {
        k for (s, k) in action_scan._SCAN_DISPATCH if s == "pod"
    }
    missing = required_pod_kinds - actual_pod_kinds
    assert not missing, f"dispatch table missing pod kinds: {missing}"

    assert ("bot", "applications") in action_scan._SCAN_DISPATCH
