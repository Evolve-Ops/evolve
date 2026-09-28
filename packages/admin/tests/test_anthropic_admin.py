"""tests/test_anthropic_admin.py — Tier 2.2 Phase A.

Tests for the Anthropic Admin API client + the on-demand cost-report
endpoint. HTTP is mocked end-to-end via the injected ``transport``
callable; we never hit the real Anthropic API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Credentials loader ───────────────────────────────────────────────────────


def test_load_admin_api_key_from_json_object(tmp_path):
    from evolve_admin import anthropic_admin

    (tmp_path / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-ant-admin-test-1"})
    )
    assert anthropic_admin.load_admin_api_key(tmp_path) == "sk-ant-admin-test-1"


def test_load_admin_api_key_from_json_with_key_field(tmp_path):
    """Tolerance: accept "key" as well as "api_key"."""
    from evolve_admin import anthropic_admin

    (tmp_path / "anthropic-admin-key.json").write_text(
        json.dumps({"key": "sk-ant-admin-test-2"})
    )
    assert anthropic_admin.load_admin_api_key(tmp_path) == "sk-ant-admin-test-2"


def test_load_admin_api_key_from_bare_string(tmp_path):
    """Operator might just drop the key string in the file."""
    from evolve_admin import anthropic_admin

    (tmp_path / "anthropic-admin-key.json").write_text(
        json.dumps("sk-ant-admin-bare")
    )
    assert anthropic_admin.load_admin_api_key(tmp_path) == "sk-ant-admin-bare"


def test_load_admin_api_key_from_unquoted_string(tmp_path):
    """Bare string with no JSON quoting — falls through the parse failure."""
    from evolve_admin import anthropic_admin

    (tmp_path / "anthropic-admin-key.json").write_text("sk-ant-admin-raw\n")
    assert anthropic_admin.load_admin_api_key(tmp_path) == "sk-ant-admin-raw"


def test_load_admin_api_key_from_env_when_file_absent(tmp_path, monkeypatch):
    from evolve_admin import anthropic_admin

    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "sk-ant-admin-env")
    assert anthropic_admin.load_admin_api_key(tmp_path) == "sk-ant-admin-env"


def test_load_admin_api_key_returns_none_when_nothing_configured(tmp_path, monkeypatch):
    from evolve_admin import anthropic_admin

    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    assert anthropic_admin.load_admin_api_key(tmp_path) is None


# ── pod_uses_anthropic ───────────────────────────────────────────────────────


def test_pod_uses_anthropic_false_when_no_bots(tmp_path):
    from evolve_admin import anthropic_admin

    assert anthropic_admin.pod_uses_anthropic({"bots": {}}, users_root=tmp_path) is False


def test_pod_uses_anthropic_false_when_bot_has_no_anthropic_profile(tmp_path):
    from evolve_admin import anthropic_admin

    home = tmp_path / "team_bot_a" / ".openclaw" / "agents" / "main" / "agent"
    home.mkdir(parents=True)
    (home / "auth-profiles.json").write_text(
        json.dumps({"profiles": {"openai:api": {"type": "api_key", "key": "sk-openai"}}})
    )
    net = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    assert anthropic_admin.pod_uses_anthropic(net, users_root=tmp_path) is False


def test_pod_uses_anthropic_true_when_canonical_profile_present(tmp_path):
    from evolve_admin import anthropic_admin

    home = tmp_path / "team_bot_a" / ".openclaw" / "agents" / "main" / "agent"
    home.mkdir(parents=True)
    (home / "auth-profiles.json").write_text(
        json.dumps({"profiles": {"anthropic:api": {"type": "api_key", "key": "sk-ant-api03-xyz"}}})
    )
    net = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    assert anthropic_admin.pod_uses_anthropic(net, users_root=tmp_path) is True


def test_pod_uses_anthropic_falls_back_to_bot_id_when_user_unset(tmp_path):
    """``cfg.user`` missing → derive the OS user from the bot id itself."""
    from evolve_admin import anthropic_admin

    home = tmp_path / "admin_bot" / ".openclaw"
    home.mkdir(parents=True)
    (home / "auth-profiles.json").write_text(
        json.dumps({"anthropic": "sk-ant-api03-flat"})
    )
    net = {"bots": {"admin_bot": {}}}
    assert anthropic_admin.pod_uses_anthropic(net, users_root=tmp_path) is True


# ── save_admin_api_key ───────────────────────────────────────────────────────


def test_save_admin_api_key_writes_file_with_correct_mode(tmp_path):
    import stat
    from evolve_admin import anthropic_admin

    anthropic_admin.save_admin_api_key(tmp_path, "sk-ant-admin-saved")
    path = anthropic_admin.admin_key_path(tmp_path)
    assert path.exists()
    assert json.loads(path.read_text()) == {"api_key": "sk-ant-admin-saved"}
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_admin_api_key_overwrites_existing(tmp_path):
    from evolve_admin import anthropic_admin

    anthropic_admin.save_admin_api_key(tmp_path, "sk-ant-admin-first")
    anthropic_admin.save_admin_api_key(tmp_path, "sk-ant-admin-second")
    assert anthropic_admin.load_admin_api_key(tmp_path) == "sk-ant-admin-second"


# ── fetch_cost_report ────────────────────────────────────────────────────────


def _make_transport(responses):
    """Build a fake transport that returns ``responses`` in order.
    Each entry is ``(status, payload)``."""
    calls = []

    def transport(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not responses:
            raise AssertionError("transport called more times than scripted")
        return responses.pop(0)

    return transport, calls


def test_fetch_cost_report_parses_total_from_nested_amount():
    """Real Anthropic shape: each bucket has a results[] list, each
    row has an ``amount`` dict with ``value`` field."""
    from evolve_admin import anthropic_admin

    payload = {
        "data": [
            {
                "starting_at": "2026-05-01T00:00:00Z",
                "ending_at": "2026-05-02T00:00:00Z",
                "results": [
                    {"amount": {"value": 1.25, "currency": "USD"}},
                    {"amount": {"value": 0.75, "currency": "USD"}},
                ],
            },
            {
                "starting_at": "2026-05-02T00:00:00Z",
                "ending_at": "2026-05-03T00:00:00Z",
                "results": [{"amount": {"value": 2.00, "currency": "USD"}}],
            },
        ]
    }
    transport, calls = _make_transport([(200, payload)])

    report, err = anthropic_admin.fetch_cost_report(
        "sk-ant-admin-test",
        starting_at="2026-05-01T00:00:00Z",
        ending_at="2026-05-03T00:00:00Z",
        transport=transport,
    )
    assert err is None
    assert report is not None
    assert report.bucket_count == 2
    assert report.total_cost_usd == pytest.approx(4.0)
    # Headers carried the admin key + version
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-admin-test"
    assert calls[0]["headers"]["anthropic-version"]
    # URL has the query params
    assert "starting_at=2026-05-01T00%3A00%3A00Z" in calls[0]["url"]
    assert "bucket_width=1d" in calls[0]["url"]


def test_fetch_cost_report_parses_flat_amount():
    """Tolerance: some bucket rows might use flat ``cost_usd`` or
    ``amount_usd`` instead of nested amount.value."""
    from evolve_admin import anthropic_admin

    payload = {
        "data": [
            {
                "starting_at": "2026-05-01T00:00:00Z",
                "results": [{"cost_usd": 1.50}, {"amount_usd": 0.50}],
            },
        ]
    }
    transport, _ = _make_transport([(200, payload)])
    report, err = anthropic_admin.fetch_cost_report(
        "sk-ant-admin",
        starting_at="2026-05-01T00:00:00Z",
        ending_at="2026-05-02T00:00:00Z",
        transport=transport,
    )
    assert err is None
    assert report.total_cost_usd == pytest.approx(2.0)


def test_fetch_cost_report_passes_through_raw_payload():
    """The UI wants to see exactly what Anthropic returned for the
    'Raw API response' details panel."""
    from evolve_admin import anthropic_admin

    payload = {"data": [], "next_page": None, "weird_field": [1, 2, 3]}
    transport, _ = _make_transport([(200, payload)])
    report, _err = anthropic_admin.fetch_cost_report(
        "sk-x",
        starting_at="2026-05-01T00:00:00Z",
        ending_at="2026-05-02T00:00:00Z",
        transport=transport,
    )
    assert report.raw == payload


def test_fetch_cost_report_returns_error_on_401():
    from evolve_admin import anthropic_admin

    payload = {"error": {"type": "authentication_error", "message": "Invalid API Key"}}
    transport, _ = _make_transport([(401, payload)])
    report, err = anthropic_admin.fetch_cost_report(
        "sk-bogus",
        starting_at="2026-05-01T00:00:00Z",
        ending_at="2026-05-02T00:00:00Z",
        transport=transport,
    )
    assert report is None
    assert err is not None
    assert err.status == 401
    assert "Invalid API Key" in err.message


def test_fetch_cost_report_returns_error_on_500_with_string_error():
    """Some servers return a bare string in ``error``; client must
    still surface a usable message."""
    from evolve_admin import anthropic_admin

    transport, _ = _make_transport([(500, {"error": "internal_server_error"})])
    report, err = anthropic_admin.fetch_cost_report(
        "sk-x",
        starting_at="x",
        ending_at="y",
        transport=transport,
    )
    assert report is None
    assert err.status == 500
    assert "internal_server_error" in err.message


def test_fetch_cost_report_handles_empty_bucket_list():
    """Empty data array (no usage in window) — total is 0, no error."""
    from evolve_admin import anthropic_admin

    transport, _ = _make_transport([(200, {"data": []})])
    report, err = anthropic_admin.fetch_cost_report(
        "sk-x",
        starting_at="x",
        ending_at="y",
        transport=transport,
    )
    assert err is None
    assert report.bucket_count == 0
    assert report.total_cost_usd == 0.0


# ── HTTP endpoints ───────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network = {
        "members": [],
        "bots": {},
        "sharedDir": str(shared_dir),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir


def test_status_reports_not_configured_when_no_key(app):
    flask_app, _shared = app
    resp = flask_app.test_client().get("/api/anthropic-admin/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["configured"] is False
    assert payload["key_path"].endswith("anthropic-admin-key.json")


def test_status_reports_configured_when_key_file_present(app):
    flask_app, shared = app
    (shared / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-ant-admin-z"})
    )
    resp = flask_app.test_client().get("/api/anthropic-admin/status")
    assert resp.get_json()["configured"] is True


def test_status_includes_pod_uses_anthropic_field(app):
    flask_app, _shared = app
    payload = flask_app.test_client().get("/api/anthropic-admin/status").get_json()
    # Empty pod (no bots) → no anthropic usage detected.
    assert payload["pod_uses_anthropic"] is False


def test_status_pod_uses_anthropic_true_when_bot_has_key(app, monkeypatch):
    """Hand the endpoint a positive answer from the helper — exercises the
    pass-through in the status route without depending on /Users layout."""
    from evolve_admin import anthropic_admin

    monkeypatch.setattr(
        anthropic_admin, "pod_uses_anthropic", lambda *_a, **_kw: True
    )
    flask_app, _shared = app
    payload = flask_app.test_client().get("/api/anthropic-admin/status").get_json()
    assert payload["pod_uses_anthropic"] is True


def test_save_key_endpoint_writes_admin_key(app):
    flask_app, shared = app
    resp = flask_app.test_client().post(
        "/api/anthropic-admin/save-key",
        json={"api_key": "sk-ant-admin-from-ui"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    saved = json.loads((shared / "anthropic-admin-key.json").read_text())
    assert saved == {"api_key": "sk-ant-admin-from-ui"}


def test_save_key_endpoint_rejects_non_admin_key(app):
    flask_app, shared = app
    resp = flask_app.test_client().post(
        "/api/anthropic-admin/save-key",
        json={"api_key": "sk-ant-api03-user-key"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "sk-ant-admin-" in body["error"]
    assert not (shared / "anthropic-admin-key.json").exists()


def test_save_key_endpoint_rejects_empty_body(app):
    flask_app, _shared = app
    resp = flask_app.test_client().post(
        "/api/anthropic-admin/save-key", json={}
    )
    assert resp.status_code == 400
    assert "required" in resp.get_json()["error"]


def test_cost_report_endpoint_rejects_when_key_missing(app):
    flask_app, _shared = app
    resp = flask_app.test_client().get("/api/anthropic-admin/cost-report")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "not configured" in body["error"]


def test_cost_report_endpoint_rejects_bad_bucket_width(app):
    flask_app, shared = app
    (shared / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-x"})
    )
    resp = flask_app.test_client().get(
        "/api/anthropic-admin/cost-report?bucket_width=1month"
    )
    assert resp.status_code == 400
    assert "bucket_width" in resp.get_json()["error"]


def test_cost_report_endpoint_fetches_when_configured(app, monkeypatch):
    """End-to-end: key file present, transport mocked, endpoint
    returns ``{ok: true, report: {...}}``."""
    from evolve_admin import anthropic_admin

    flask_app, shared = app
    (shared / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-ant-admin-end-to-end"})
    )

    captured_headers = {}

    def fake_transport(method, url, headers, body):
        captured_headers.update(headers)
        return 200, {
            "data": [
                {"results": [{"amount": {"value": 0.50}}]},
                {"results": [{"amount": {"value": 1.50}}]},
            ]
        }

    monkeypatch.setattr(anthropic_admin, "_default_transport", fake_transport)

    resp = flask_app.test_client().get(
        "/api/anthropic-admin/cost-report?days=14&bucket_width=1d"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    report = body["report"]
    assert report["bucket_count"] == 2
    assert report["total_cost_usd"] == pytest.approx(2.0)
    # The endpoint forwarded the admin key
    assert captured_headers["x-api-key"] == "sk-ant-admin-end-to-end"


def test_cost_report_endpoint_returns_error_on_anthropic_401(app, monkeypatch):
    from evolve_admin import anthropic_admin

    flask_app, shared = app
    (shared / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-bogus"})
    )

    monkeypatch.setattr(
        anthropic_admin,
        "_default_transport",
        lambda *a, **kw: (401, {"error": {"message": "bad key"}}),
    )

    resp = flask_app.test_client().get("/api/anthropic-admin/cost-report?days=7")
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["ok"] is False
    assert "bad key" in body["error"]


# ── fetch_audit_logs (Phase B) ───────────────────────────────────────────────


def test_fetch_audit_logs_happy_path():
    """One page of events with header passthrough and URL encoding."""
    from evolve_admin import anthropic_admin

    payload = {
        "data": [
            {"id": "evt_1", "type": "workspace.created", "created_at": "2026-05-10T01:00:00Z"},
            {"id": "evt_2", "type": "api_key.deleted", "created_at": "2026-05-10T02:00:00Z"},
        ],
        "next_page": None,
    }
    transport, calls = _make_transport([(200, payload)])

    page, err = anthropic_admin.fetch_audit_logs(
        "sk-ant-admin-audit",
        starting_at="2026-05-10T00:00:00Z",
        ending_at="2026-05-11T00:00:00Z",
        limit=50,
        transport=transport,
    )
    assert err is None
    assert page is not None
    assert page.event_count == 2
    assert page.raw == payload
    # Header carried admin key + version
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-admin-audit"
    assert calls[0]["headers"]["anthropic-version"]
    # URL has the params (limit clamped to int, range params present)
    assert "limit=50" in calls[0]["url"]
    assert "starting_at=2026-05-10T00%3A00%3A00Z" in calls[0]["url"]
    assert "/v1/organizations/audit_logs" in calls[0]["url"]
    # events() helper exposes the data array
    assert page.events()[0]["id"] == "evt_1"


def test_fetch_audit_logs_clamps_limit_to_max_100():
    """limit > 100 must be clamped to 100 (API hard cap)."""
    from evolve_admin import anthropic_admin

    transport, calls = _make_transport([(200, {"data": []})])
    anthropic_admin.fetch_audit_logs(
        "sk-x",
        starting_at="a",
        ending_at="b",
        limit=500,
        transport=transport,
    )
    assert "limit=100" in calls[0]["url"]


def test_fetch_audit_logs_returns_error_on_401():
    from evolve_admin import anthropic_admin

    transport, _ = _make_transport(
        [(401, {"error": {"type": "authentication_error", "message": "Invalid"}})]
    )
    page, err = anthropic_admin.fetch_audit_logs(
        "sk-bogus",
        starting_at="a",
        ending_at="b",
        transport=transport,
    )
    assert page is None
    assert err.status == 401
    assert "Invalid" in err.message


def test_fetch_audit_logs_handles_empty_events():
    from evolve_admin import anthropic_admin

    transport, _ = _make_transport([(200, {"data": []})])
    page, err = anthropic_admin.fetch_audit_logs(
        "sk-x",
        starting_at="a",
        ending_at="b",
        transport=transport,
    )
    assert err is None
    assert page.event_count == 0
    assert page.events() == []


# ── Ingest helpers ───────────────────────────────────────────────────────────


def test_yesterday_window_picks_prior_utc_day():
    """For a 'now' of 2026-05-11T05:30Z, yesterday is 2026-05-10 with
    midnight-to-midnight UTC boundaries."""
    from datetime import datetime, timezone

    from evolve_admin import anthropic_admin_ingest

    now = datetime(2026, 5, 11, 5, 30, 0, tzinfo=timezone.utc)
    date, starting, ending = anthropic_admin_ingest.yesterday_window(now=now)
    assert date == "2026-05-10"
    assert starting == "2026-05-10T00:00:00Z"
    assert ending == "2026-05-11T00:00:00Z"


def test_compute_divergence_fraction_handles_zeros():
    from evolve_admin import anthropic_admin_ingest

    assert anthropic_admin_ingest.compute_divergence_fraction(0.0, 0.0) == 0.0
    assert anthropic_admin_ingest.compute_divergence_fraction(0.0, 10.0) == 1.0
    assert anthropic_admin_ingest.compute_divergence_fraction(10.0, 0.0) == 1.0


def test_compute_divergence_fraction_symmetric():
    from evolve_admin import anthropic_admin_ingest

    a = anthropic_admin_ingest.compute_divergence_fraction(8.0, 10.0)
    b = anthropic_admin_ingest.compute_divergence_fraction(10.0, 8.0)
    assert a == pytest.approx(0.2)
    assert a == b


def test_write_cost_report_snapshot_creates_file(tmp_path):
    from evolve_admin import anthropic_admin, anthropic_admin_ingest

    report = anthropic_admin.CostReport(
        bucket_width="1d",
        starting_at="2026-05-10T00:00:00Z",
        ending_at="2026-05-11T00:00:00Z",
        total_cost_usd=12.345,
        bucket_count=1,
        raw={"data": [{"x": 1}]},
    )
    path = anthropic_admin_ingest.write_cost_report_snapshot(tmp_path, "2026-05-10", report)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["total_cost_usd"] == 12.345
    assert data["raw"]["data"][0]["x"] == 1


def test_write_audit_log_snapshot_writes_jsonl(tmp_path):
    from evolve_admin import anthropic_admin, anthropic_admin_ingest

    page = anthropic_admin.AuditLogPage(
        starting_at="2026-05-10T00:00:00Z",
        ending_at="2026-05-11T00:00:00Z",
        event_count=2,
        raw={"data": [{"id": "a", "type": "x"}, {"id": "b", "type": "y"}]},
    )
    path = anthropic_admin_ingest.write_audit_log_snapshot(tmp_path, "2026-05-10", page)
    assert path.exists()
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "a"
    assert json.loads(lines[1])["id"] == "b"


def test_write_audit_log_snapshot_empty_writes_empty_file(tmp_path):
    from evolve_admin import anthropic_admin, anthropic_admin_ingest

    page = anthropic_admin.AuditLogPage(
        starting_at="x", ending_at="y", event_count=0, raw={"data": []}
    )
    path = anthropic_admin_ingest.write_audit_log_snapshot(tmp_path, "2026-05-10", page)
    assert path.exists()
    assert path.read_text() == ""


# ── ingest_yesterday end-to-end ──────────────────────────────────────────────


@pytest.fixture
def _shared(tmp_path):
    """Shared dir layout: signals/ subdirs exist so observe() can write."""
    s = tmp_path / "evolve"
    (s / "signals" / "firing").mkdir(parents=True)
    (s / "signals" / "snoozed").mkdir(parents=True)
    (s / "signals" / "archived").mkdir(parents=True)
    return s


def _ingest_transport(cost_payload, audit_payload, status_cost=200, status_audit=200):
    """Two-response transport: first call returns cost, second returns audit."""
    queue = [(status_cost, cost_payload), (status_audit, audit_payload)]
    calls = []

    def transport(method, url, headers, body):
        calls.append({"method": method, "url": url})
        return queue.pop(0)

    return transport, calls


def test_ingest_yesterday_no_key_skips(tmp_path, monkeypatch):
    """When no key configured, ingest returns a skip result, writes nothing."""
    from evolve_admin import anthropic_admin_ingest

    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    result = anthropic_admin_ingest.ingest_yesterday(tmp_path, {"members": []})
    assert result.cost_report_written is False
    assert result.audit_log_written is False
    assert result.divergence_signal_fired is False
    assert "not configured" in result.errors[0]


def test_ingest_yesterday_writes_both_snapshots_when_fetches_succeed(
    _shared, monkeypatch
):
    """Happy path: cost + audit both fetched, both snapshot files exist."""
    from datetime import datetime, timezone

    from evolve_admin import anthropic_admin_ingest

    transport, _calls = _ingest_transport(
        cost_payload={"data": [{"results": [{"amount": {"value": 5.0}}]}]},
        audit_payload={"data": [{"id": "e1", "type": "workspace.created"}]},
    )

    # Mock the local-total lookup so we don't need the analyzer dir's
    # cost_ledger to find observations on disk.
    monkeypatch.setattr(
        anthropic_admin_ingest, "local_total_for_window", lambda *a, **kw: 5.0
    )

    result = anthropic_admin_ingest.ingest_yesterday(
        _shared,
        {
            "members": ["security_bot", "team_bot_a"],
            "anthropic_admin": {"audit_logs_enabled": True},
        },
        transport=transport,
        api_key="sk-x",
        now=datetime(2026, 5, 11, 4, 15, tzinfo=timezone.utc),
    )

    assert result.cost_report_written is True
    assert result.audit_log_written is True
    assert result.anthropic_total_usd == pytest.approx(5.0)
    assert result.local_total_usd == pytest.approx(5.0)
    assert result.divergence_fraction == pytest.approx(0.0)
    assert result.divergence_signal_fired is False
    # Snapshot files exist at the expected paths
    assert (_shared / "anthropic_api" / "cost_report" / "2026-05-10.json").exists()
    assert (_shared / "anthropic_api" / "audit_logs" / "2026-05-10.jsonl").exists()


def test_ingest_yesterday_emits_divergence_signal_above_threshold(
    _shared, monkeypatch
):
    """When local and Anthropic totals disagree by >10%, fire a Signal."""
    from datetime import datetime, timezone

    from evolve_admin import anthropic_admin_ingest

    # Anthropic says $10, local says $5 → 50% divergence
    transport, _ = _ingest_transport(
        cost_payload={"data": [{"results": [{"amount": {"value": 10.0}}]}]},
        audit_payload={"data": []},
    )
    monkeypatch.setattr(
        anthropic_admin_ingest, "local_total_for_window", lambda *a, **kw: 5.0
    )

    result = anthropic_admin_ingest.ingest_yesterday(
        _shared,
        {"members": ["security_bot"]},
        transport=transport,
        api_key="sk-x",
        now=datetime(2026, 5, 11, 4, 15, tzinfo=timezone.utc),
    )

    assert result.divergence_fraction == pytest.approx(0.5)
    assert result.divergence_signal_fired is True

    # A firing Signal must now exist with our signature
    firing_dir = _shared / "signals" / "firing"
    files = list(firing_dir.glob("*.json"))
    assert len(files) == 1
    sig = json.loads(files[0].read_text())
    assert sig["signature"] == "cost_diverges_from_anthropic"
    assert sig["producer"] == "anthropic_admin_ingest"
    assert sig["severity"] == "warn"
    assert sig["scope"] == "pod"
    assert "Anthropic reports $10.00" in sig["body"]


def test_ingest_yesterday_resolves_signal_when_back_in_alignment(
    _shared, monkeypatch
):
    """Yesterday's divergence is resolved when today is back inside threshold."""
    from datetime import datetime, timezone

    from evolve_admin import anthropic_admin_ingest

    # First run: divergent → signal fires
    transport1, _ = _ingest_transport(
        cost_payload={"data": [{"results": [{"amount": {"value": 10.0}}]}]},
        audit_payload={"data": []},
    )
    monkeypatch.setattr(
        anthropic_admin_ingest, "local_total_for_window", lambda *a, **kw: 5.0
    )
    anthropic_admin_ingest.ingest_yesterday(
        _shared,
        {"members": ["security_bot"]},
        transport=transport1,
        api_key="sk-x",
        now=datetime(2026, 5, 11, 4, 15, tzinfo=timezone.utc),
    )
    firing_count_after_first = len(list((_shared / "signals" / "firing").glob("*.json")))
    assert firing_count_after_first == 1

    # Second run: aligned → signal must be auto-resolved into archived/
    transport2, _ = _ingest_transport(
        cost_payload={"data": [{"results": [{"amount": {"value": 10.0}}]}]},
        audit_payload={"data": []},
    )
    monkeypatch.setattr(
        anthropic_admin_ingest, "local_total_for_window", lambda *a, **kw: 10.0
    )
    result2 = anthropic_admin_ingest.ingest_yesterday(
        _shared,
        {"members": ["security_bot"]},
        transport=transport2,
        api_key="sk-x",
        now=datetime(2026, 5, 12, 4, 15, tzinfo=timezone.utc),
    )
    assert result2.divergence_signal_fired is False
    # The previously-firing signal is now archived
    assert len(list((_shared / "signals" / "firing").glob("*.json"))) == 0
    assert len(list((_shared / "signals" / "archived").glob("*.json"))) == 1


def test_ingest_yesterday_continues_when_cost_fails_but_audit_works(
    _shared, monkeypatch
):
    """If cost_report 401s but audit_logs is fine, audit snapshot still
    writes; divergence skipped (no Anthropic total)."""
    from datetime import datetime, timezone

    from evolve_admin import anthropic_admin_ingest

    transport, _ = _ingest_transport(
        cost_payload={"error": {"message": "bad key"}},
        audit_payload={"data": [{"id": "e1"}]},
        status_cost=401,
        status_audit=200,
    )

    result = anthropic_admin_ingest.ingest_yesterday(
        _shared,
        {
            "members": [],
            "anthropic_admin": {"audit_logs_enabled": True},
        },
        transport=transport,
        api_key="sk-x",
        now=datetime(2026, 5, 11, 4, 15, tzinfo=timezone.utc),
    )

    assert result.cost_report_written is False
    assert result.audit_log_written is True
    assert result.anthropic_total_usd is None
    assert result.divergence_fraction is None
    assert result.divergence_signal_fired is False
    assert any("cost_report fetch" in e for e in result.errors)
    # No divergence signal got written
    assert list((_shared / "signals" / "firing").glob("*.json")) == []


# ── audit_logs_enabled gate ──────────────────────────────────────────────────


def test_audit_logs_enabled_defaults_off_for_missing_config():
    from evolve_admin import anthropic_admin_ingest

    assert anthropic_admin_ingest._audit_logs_enabled({}) is False
    assert anthropic_admin_ingest._audit_logs_enabled({"members": []}) is False
    assert (
        anthropic_admin_ingest._audit_logs_enabled({"anthropic_admin": {}}) is False
    )


def test_audit_logs_enabled_when_flag_true():
    from evolve_admin import anthropic_admin_ingest

    assert (
        anthropic_admin_ingest._audit_logs_enabled(
            {"anthropic_admin": {"audit_logs_enabled": True}}
        )
        is True
    )


def test_ingest_yesterday_skips_audit_logs_by_default(_shared, monkeypatch):
    """No anthropic_admin block → audit_logs fetch must not fire.

    Scripts the transport for a single response (cost only). If the
    ingest tried to call audit, the helper would raise on the empty
    queue.
    """
    from datetime import datetime, timezone

    from evolve_admin import anthropic_admin_ingest

    queue = [(200, {"data": [{"results": [{"amount": {"value": 5.0}}]}]})]
    calls: list[str] = []

    def transport(method, url, headers, body):
        calls.append(url)
        if not queue:
            raise AssertionError(
                "transport called more times than scripted "
                "(audit fetch should be skipped by default)"
            )
        return queue.pop(0)

    monkeypatch.setattr(
        anthropic_admin_ingest, "local_total_for_window", lambda *a, **kw: 5.0
    )

    result = anthropic_admin_ingest.ingest_yesterday(
        _shared,
        {"members": ["security_bot"]},
        transport=transport,
        api_key="sk-x",
        now=datetime(2026, 5, 11, 4, 15, tzinfo=timezone.utc),
    )

    # Exactly one transport call — cost_report only
    assert len(calls) == 1
    assert "/v1/organizations/cost_report" in calls[0]
    # Cost flow still worked
    assert result.cost_report_written is True
    assert result.anthropic_total_usd == pytest.approx(5.0)
    # Audit was skipped, not failed → no audit error, no audit snapshot dir
    assert result.audit_log_written is False
    assert all("audit_log" not in e for e in result.errors)
    assert not (_shared / "anthropic_api" / "audit_logs").exists()


# ── /api/anthropic-admin/audit-logs endpoint ─────────────────────────────────


def test_audit_logs_endpoint_rejects_when_key_missing(app):
    flask_app, _ = app
    resp = flask_app.test_client().get("/api/anthropic-admin/audit-logs")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "not configured" in body["error"]


def test_audit_logs_endpoint_fetches_when_configured(app, monkeypatch):
    from evolve_admin import anthropic_admin

    flask_app, shared = app
    (shared / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-end-to-end-audit"})
    )

    captured_headers: dict = {}

    def fake_transport(method, url, headers, body):
        captured_headers.update(headers)
        return 200, {"data": [{"id": "evt_x", "type": "workspace.created"}]}

    monkeypatch.setattr(anthropic_admin, "_default_transport", fake_transport)

    resp = flask_app.test_client().get(
        "/api/anthropic-admin/audit-logs?days=1&limit=10"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["page"]["event_count"] == 1
    assert captured_headers["x-api-key"] == "sk-end-to-end-audit"


def test_audit_logs_endpoint_returns_error_on_anthropic_401(app, monkeypatch):
    from evolve_admin import anthropic_admin

    flask_app, shared = app
    (shared / "anthropic-admin-key.json").write_text(
        json.dumps({"api_key": "sk-bogus"})
    )

    monkeypatch.setattr(
        anthropic_admin,
        "_default_transport",
        lambda *a, **kw: (401, {"error": {"message": "bad key"}}),
    )

    resp = flask_app.test_client().get("/api/anthropic-admin/audit-logs")
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["ok"] is False
    assert "bad key" in body["error"]
