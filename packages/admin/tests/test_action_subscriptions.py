"""tests/test_action_subscriptions.py — action.subscriptions.set tool.

Write tool that POSTs to admin-ui's ``/api/alerts/subscriptions`` so
evo can act on "unsubscribe me from X" without trying (and failing) to
touch the evolve-only alerts/ tree directly. The HTTP indirection
honors the deploy.py contract:

    EVO_WRITE_SHARED_SUBDIRS = ("proposals", "signals")
    # Other shared subdirs (profiles/, observations/, alerts/, …)
    # stay evolve-only; evo reads them via either world-readable
    # perms or admin-daemon HTTP endpoints.

Tests cover the contract:
  - registered in the tool registry under the expected dotted name
  - non-safety-critical mute POSTs to the route and returns previous +
    applied (enabled, frequency)
  - frequency change POSTs to the route and returns previous + applied
  - unknown key returns a useful error message listing valid keys
    BEFORE any HTTP call
  - safety-critical mute without confirmed=True returns
    confirmation_required=True with a clear warning, NO HTTP call
  - safety-critical mute with confirmed=True does POST and writes
  - frequency-only change on a safety-critical event does NOT require
    confirmation (the event still fires, just at a different cadence)
  - no-op call (no enabled, no frequency) returns an error, no HTTP
  - malformed pre-existing subscriptions.json returns error and does
    NOT touch the file (route never invoked)
  - intent_reason parameter is recorded in the local evo audit log
  - HTTP error response surfaces verbatim + http_status to the operator
  - HTTP transport unreachable returns a clear error
  - the test stub uses the SAME write helper the real UI route uses,
    so the on-disk file matches what the UI would have produced

Spec: ``internal/diagnosis-evo-subscription-awareness-2026-06-02.md``.
"""

from __future__ import annotations

import json
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

from evolve_admin.alerts import subscriptions as _subs  # noqa: E402
from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import action_subscriptions  # noqa: E402


# ─── Test fixtures ──────────────────────────────────────────────────────────


_FAKE_POST_URL = "http://127.0.0.1:5050/api/alerts/subscriptions"


def _route_stub_factory(shared_dir: Path, call_log: list[dict[str, Any]]):
    """Build a post_json stub that simulates admin-ui's POST route.

    The real route is a thin shim: it calls
    ``alerts.subscriptions.write_subscription`` and returns the
    ``_subscription_view`` of the result. We mirror that here so the
    on-disk file lands exactly where the production route would have
    landed it — preserves the "same file as the UI" invariant the
    tool's docstring promises.

    Records each call into ``call_log`` for assertion. Test isolation
    is per-call: each invocation uses the same shared_dir, so multiple
    writes within one test see each other's state.
    """
    def stub(url: str, body: dict[str, Any], timeout: int):
        call_log.append({"url": url, "body": body, "timeout": timeout})
        # Phase 2: group toggle ({subscription_id, enabled}) vs per-event
        # frequency ({event_key, frequency}). Mirror the real route's branches.
        sub_id = body.get("subscription_id")
        if sub_id:
            if not isinstance(body.get("enabled"), bool):
                return 400, {"error": "group toggle requires enabled: bool"}, None
            try:
                group = _subs.write_subscription_group(
                    shared_dir, sub_id, enabled=body["enabled"],
                )
            except KeyError as exc:
                return 404, {"error": str(exc), "subscription_id": sub_id}, None
            return 200, {"updated_groups": [{
                "id": group.id, "enabled": group.enabled,
                "is_overridden": group.is_overridden,
            }], "updated_events": []}, None

        event_key = body.get("event_key")
        if not event_key:
            return 400, {"error": "update missing subscription_id or event_key"}, None
        if "enabled" in body:
            return 400, {"error": "per-event enabled is no longer the on/off surface"}, None
        try:
            sub = _subs.write_subscription(
                shared_dir, event_key,
                enabled=None,
                frequency=body.get("frequency"),
            )
        except KeyError as exc:
            return 404, {"error": str(exc), "event_key": event_key}, None
        except ValueError as exc:
            return 400, {"error": str(exc), "event_key": event_key}, None
        return 200, {"updated_groups": [], "updated_events": [{
            "event_key":     sub.event_key,
            "enabled":       sub.enabled,
            "frequency":     sub.frequency.value,
            "is_overridden": sub.is_overridden,
        }]}, None
    return stub


@pytest.fixture
def http_stub(tmp_path):
    """Yields ``(stub_fn, call_log)`` for a fake admin-ui route bound
    to ``tmp_path``. The stub mirrors the real route's contract."""
    calls: list[dict[str, Any]] = []
    return _route_stub_factory(tmp_path, calls), calls


def _read_state(tmp_path: Path) -> dict:
    """Helper to read the raw on-disk subscriptions.json."""
    p = tmp_path / "alerts" / "subscriptions.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ─── Registration ────────────────────────────────────────────────────────────


def test_action_subscriptions_set_is_registered():
    tool = _tools.lookup("action.subscriptions.set")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    # Non-read tier MUST have validate (registry __post_init__ enforces
    # this; we re-check so a regression that removes validate but keeps
    # the tier WRITE_SAFE doesn't slip through).
    assert tool.validate is not None


def test_action_subscriptions_set_in_manifest():
    """Manifest renders with the standard shape — name, description,
    input_schema with `key` required and enabled/frequency optional."""
    manifest = _tools.build_tool_manifest()
    entry = next(
        (e for e in manifest if e["name"] == "action.subscriptions.set"),
        None,
    )
    assert entry is not None
    assert "key" in entry["input_schema"]["required"]
    assert "enabled" in entry["input_schema"]["properties"]
    assert "frequency" in entry["input_schema"]["properties"]
    assert "confirmed" in entry["input_schema"]["properties"]


# ─── Non-safety-critical writes ──────────────────────────────────────────────


def test_non_safety_critical_mute_writes_and_returns_diff(tmp_path, http_stub):
    """Phase 2: muting an event mutes its (non-safety-critical) GROUP →
    POSTs a subscription_id group toggle; response includes the resolved
    group + previous/applied for echo-back."""
    stub, calls = http_stub
    # decisions.proposal_ready → needs_decision group (not safety-critical),
    # defaults enabled.
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is True
    assert result["key"] == "decisions.proposal_ready"
    assert result["subscription_id"] == "needs_decision"
    assert result["is_safety_critical"] is False
    assert result["previous"]["enabled"] is True
    assert result["applied"]["enabled"] is False
    assert "verify_via" in result

    # Stub received exactly one POST — a GROUP toggle.
    assert len(calls) == 1
    assert calls[0]["url"] == _FAKE_POST_URL
    assert calls[0]["body"] == {
        "subscription_id": "needs_decision",
        "enabled": False,
    }
    # On-disk effect: the group is off (the event resolves disabled through it).
    state = _read_state(tmp_path)
    assert state["subscription_groups"]["needs_decision"]["enabled"] is False


def test_frequency_only_change_writes_override(tmp_path, http_stub):
    """Frequency change → POST is issued with just the frequency field;
    enabled is left at its pre-existing value (catalog default since no
    prior override)."""
    stub, calls = http_stub
    # Pick an event with multiple allowed frequencies.
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="security.audit_finding",
        frequency="daily_digest",
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is True
    assert result["applied"]["frequency"] == "daily_digest"
    # Frequency-only call: body MUST NOT include `enabled`.
    assert "enabled" not in calls[0]["body"]
    assert calls[0]["body"]["frequency"] == "daily_digest"

    state = _read_state(tmp_path)
    rec = state["subscriptions"]["security.audit_finding"]
    assert rec["frequency"] == "daily_digest"


# ─── Unknown key ─────────────────────────────────────────────────────────────


def test_unknown_key_returns_useful_error(tmp_path, http_stub):
    """Bogus key → error mentions invalid key + lists valid samples.
    The catalog check runs locally before any HTTP call, so the stub
    must NOT be invoked."""
    stub, calls = http_stub
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="security.fictional_event",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert "unknown subscription key" in result["error"].lower()
    assert "valid_keys_sample" in result
    assert isinstance(result["valid_keys_sample"], list)
    assert len(result["valid_keys_sample"]) > 0
    # Route was NOT called and file was NOT touched.
    assert calls == []
    assert not (tmp_path / "alerts" / "subscriptions.json").exists()


# ─── Safety-critical guardrail ───────────────────────────────────────────────


def test_safety_critical_mute_without_confirmed_returns_warning(tmp_path, http_stub):
    """Muting security.config_drift without confirmed=True must NOT
    POST to admin-ui — it returns confirmation_required=True with a
    warning. The gate runs LOCALLY so the operator sees the warning
    immediately."""
    stub, calls = http_stub
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="security.config_drift",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert result.get("confirmation_required") is True
    assert "safety-critical" in result["warning"].lower()
    assert result["key"] == "security.config_drift"
    # Route was NOT called, file was NOT touched.
    assert calls == []
    assert not (tmp_path / "alerts" / "subscriptions.json").exists()


def test_safety_critical_mute_with_confirmed_writes(tmp_path, http_stub):
    """confirmed=True bypasses the guardrail and POSTs the override."""
    stub, calls = http_stub
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="security.config_drift",
        enabled=False,
        confirmed=True,
        intent_reason="operator requested via evo on 2026-06-02",
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is True
    assert result["subscription_id"] == "security_findings"
    assert result["applied"]["enabled"] is False
    assert result["intent_reason"] == (
        "operator requested via evo on 2026-06-02"
    )
    assert len(calls) == 1
    state = _read_state(tmp_path)
    # The safety-critical GROUP is now off (config_drift resolves disabled
    # through security_findings).
    assert state["subscription_groups"]["security_findings"]["enabled"] is False


def test_safety_critical_frequency_only_does_not_require_confirm(tmp_path, http_stub):
    """Changing frequency on a safety-critical event (the event still
    fires, just at a different cadence) does NOT require confirmation.
    But security.config_drift only allows IMMEDIATE — pick
    security.audit_finding which is safety-critical and supports
    digest."""
    stub, calls = http_stub
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="security.audit_finding",
        frequency="daily_digest",
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is True
    assert "confirmation_required" not in result
    assert result["applied"]["frequency"] == "daily_digest"
    assert len(calls) == 1


# ─── No-op / validation ──────────────────────────────────────────────────────


def test_no_op_call_returns_error(tmp_path, http_stub):
    """Neither `enabled` nor `frequency` set → error, no HTTP, no
    write."""
    stub, calls = http_stub
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_applied",
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert "at least one of" in result["error"].lower()
    assert calls == []
    assert not (tmp_path / "alerts" / "subscriptions.json").exists()


def test_invalid_frequency_for_event_returns_allowed_list(tmp_path, http_stub):
    """Frequency not in this event's allowed_frequencies → error with
    the allowed list. The real route would return HTTP 400; the tool
    surfaces that as ok=False with allowed_frequencies enumerated from
    the catalog so the model can self-correct."""
    stub, calls = http_stub
    # updates.openclaw_blocked allows only IMMEDIATE (an upgrade blocker is
    # not deferrable). security.config_drift used to be immediate-only too,
    # but workstream D1 widened it to also allow daily_digest, so it no
    # longer exercises the rejection path.
    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="updates.openclaw_blocked",
        frequency="daily_digest",
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert "allowed_frequencies" in result
    assert "immediate" in result["allowed_frequencies"]
    # The route received the request and returned 400 — the tool
    # surfaces both the catalog list and the http_status.
    assert result.get("http_status") == 400


# ─── Malformed pre-existing file ─────────────────────────────────────────────


def test_malformed_subscriptions_json_refuses_write(tmp_path, http_stub):
    """Pre-existing malformed file → refuse the write LOCALLY rather
    than letting admin-ui silently rebuild from scratch and clobber any
    unparseable operator overrides. The route is never called."""
    stub, calls = http_stub
    alerts_dir = tmp_path / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    target = alerts_dir / "subscriptions.json"
    target.write_text("{this is not valid json", encoding="utf-8")
    pre_bytes = target.read_bytes()

    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert "malformed" in result["error"].lower()
    # Route was NOT called and file is byte-for-byte unchanged.
    assert calls == []
    assert target.read_bytes() == pre_bytes


# ─── HTTP error / transport surfaces ────────────────────────────────────────


def test_http_error_response_surfaces_status_and_body(tmp_path):
    """When admin-ui rejects the write (e.g. 500), the tool surfaces
    the HTTP status + the route's error envelope so the operator gets
    a clear pointer rather than a silent failure."""
    def failing_stub(url, body, timeout):
        return 500, {"error": "internal: write_subscription crashed"}, None

    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=failing_stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "write_subscription crashed" in result["error"]


def test_transport_unreachable_returns_clear_error(tmp_path):
    """If the admin-ui daemon is down (URLError from urllib), surface
    the unreachable error string verbatim so the operator can act on
    it."""
    def unreachable_stub(url, body, timeout):
        return 0, None, f"admin server unreachable at {url}: Connection refused"

    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=unreachable_stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is False
    assert "unreachable" in result["error"].lower()
    assert "http_status" not in result  # Transport never produced one.


# ─── Atomic write + temp-file cleanup (post-route invariants) ──────────────


def test_atomic_write_leaves_no_temp_files(tmp_path, http_stub):
    """The admin-ui route uses tempfile + os.replace for atomic writes
    (via alerts.subscriptions.write_subscription, which our stub
    invokes identically). After a successful write the alerts/ dir
    contains only the final subscriptions.json (plus the evo-side
    audit log) — NO leftover .tmp or dotfiles."""
    stub, _ = http_stub
    action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    alerts_dir = tmp_path / "alerts"
    entries = sorted(p.name for p in alerts_dir.iterdir())
    # Expected: subscriptions.json + subscriptions-audit.jsonl. NO
    # leftover dotfiles, NO trailing .tmp.
    assert "subscriptions.json" in entries
    assert "subscriptions-audit.jsonl" in entries
    for name in entries:
        assert not name.endswith(".tmp"), f"leftover temp file: {name}"
        assert not name.startswith(".subscriptions.json."), (
            f"leftover dotfile: {name}"
        )


# ─── Directory creation (post-route invariants) ────────────────────────────


def test_alerts_dir_created_when_missing(tmp_path, http_stub):
    """When ``{shared_dir}/alerts/`` doesn't exist yet, the route's
    write path creates it. The audit-log side effect on the evo side
    also creates the dir; this is the fresh-pod case end-to-end."""
    stub, _ = http_stub
    assert not (tmp_path / "alerts").exists()

    result = action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    assert result["ok"] is True
    assert (tmp_path / "alerts").is_dir()
    assert (tmp_path / "alerts" / "subscriptions.json").is_file()


# ─── Intent reason recording ────────────────────────────────────────────────


def test_intent_reason_lands_in_audit_log(tmp_path, http_stub):
    """intent_reason is accepted and recorded in subscriptions-audit.jsonl
    so a future config_intent system can attribute the override. The
    audit log stays evo-side (the route doesn't accept it today).
    """
    stub, _ = http_stub
    action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        intent_reason="too noisy during the team-bot-a migration",
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    audit = tmp_path / "alerts" / "subscriptions-audit.jsonl"
    assert audit.exists()
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert lines, "audit log has at least one entry"
    rec = json.loads(lines[-1])
    assert rec["key"] == "decisions.proposal_ready"
    assert rec["intent_reason"] == "too noisy during the team-bot-a migration"
    assert rec["actor"].startswith("evo:")


# ─── UI / tool surface drift guard ──────────────────────────────────────────


def test_writes_same_file_the_ui_writes(tmp_path, http_stub):
    """Regression: this tool must drive admin-ui's POST route, which
    in turn writes to the SAME on-disk file the UI's PUT form writes.
    If the URL path or request shape drifts, the UI and the tool stop
    seeing each other's edits."""
    stub, _ = http_stub
    # Apply via the tool.
    action_subscriptions._handler(
        shared_dir=tmp_path,
        key="decisions.proposal_ready",
        enabled=False,
        post_json=stub,
        post_url=_FAKE_POST_URL,
    )

    # Read via the module the UI uses — the group toggle must be visible
    # (the event resolves disabled through its group).
    sub = _subs.read_subscription(tmp_path, "decisions.proposal_ready")
    assert sub is not None
    assert sub.enabled is False
    group = _subs.read_subscription_group(tmp_path, "needs_decision")
    assert group is not None and group.enabled is False and group.is_overridden


# ─── Validate gate ──────────────────────────────────────────────────────────


def test_validate_rejects_empty_key(tmp_path):
    """Validate gate refuses missing `key` before any write."""
    result = action_subscriptions._validate(
        shared_dir=tmp_path,
        key="",
        enabled=False,
    )
    assert result["ok"] is False
    assert "key" in result["reason"].lower()


def test_validate_rejects_no_change(tmp_path):
    """Validate refuses a call with no enabled / no frequency."""
    result = action_subscriptions._validate(
        shared_dir=tmp_path,
        key="decisions.proposal_applied",
    )
    assert result["ok"] is False
    assert "at least one of" in result["reason"].lower()


def test_validate_passes_well_formed_request(tmp_path):
    """Happy path: validate returns ok=True with context."""
    result = action_subscriptions._validate(
        shared_dir=tmp_path,
        key="security.config_drift",
        enabled=False,
    )
    assert result["ok"] is True
    assert result["context"]["is_safety_critical"] is True
    assert result["context"]["label"] == "Bot configuration changed unexpectedly"
