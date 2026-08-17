"""tests/test_content_scan_endpoints.py — Phase B endpoints.

Phase B of [spec-prompt-injection-scanner-2026-05-10.md](docs/spec-prompt-injection-scanner-2026-05-10.md)
adds:

  - ``POST /api/content-scan/mark-reviewed`` now accepts an optional
    ``ttl_days`` override (Phase A was fixed at 30 days)
  - ``POST /api/content-scan/graduate`` extends an existing
    suppression's TTL to ``PERMANENT_TTL_DAYS`` (~10 years)

Both endpoints share the underlying ``content_scan.suppressions.add``
helper; the tests pin the request shapes, error paths, and that the
graduated suppression actually carries a multi-year expiry.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network = {"members": [], "bots": {}, "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


# ── mark-reviewed: default 30-day TTL ────────────────────────────────────────


def test_mark_reviewed_default_ttl_is_30_days(app):
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/mark-reviewed",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "html_comment_unknown",
            "line_range": [42],
            "excerpt": "<!-- foo:bar -->",
            "reviewer_note": "first review",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    sup = payload["suppression"]
    expires = datetime.strptime(
        sup["expires_at"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
    )
    reviewed = datetime.strptime(
        sup["reviewed_at"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
    )
    delta_days = (expires - reviewed).total_seconds() / 86400
    assert 29.5 < delta_days < 30.5


# ── mark-reviewed: custom ttl_days ───────────────────────────────────────────


def test_mark_reviewed_accepts_custom_ttl_days(app):
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/mark-reviewed",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "html_comment_unknown",
            "line_range": [42],
            "excerpt": "x",
            "ttl_days": 90,
        },
    )
    assert resp.status_code == 200
    sup = resp.get_json()["suppression"]
    expires = datetime.strptime(
        sup["expires_at"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
    )
    reviewed = datetime.strptime(
        sup["reviewed_at"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
    )
    delta_days = (expires - reviewed).total_seconds() / 86400
    assert 89.5 < delta_days < 90.5


def test_mark_reviewed_rejects_non_int_ttl(app):
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/mark-reviewed",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "x",
            "line_range": [1],
            "ttl_days": "thirty",
        },
    )
    assert resp.status_code == 400
    assert "ttl_days must be an integer" in resp.get_json()["error"]


def test_mark_reviewed_rejects_ttl_above_permanent(app):
    """PERMANENT_TTL_DAYS is the upper bound. Anything higher rejects."""
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/mark-reviewed",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "x",
            "line_range": [1],
            "ttl_days": 10000,  # > PERMANENT_TTL_DAYS = 3650
        },
    )
    assert resp.status_code == 400
    assert "ttl_days" in resp.get_json()["error"]


def test_mark_reviewed_rejects_ttl_below_one(app):
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/mark-reviewed",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "x",
            "line_range": [1],
            "ttl_days": 0,
        },
    )
    assert resp.status_code == 400
    assert "ttl_days" in resp.get_json()["error"]


# ── graduate: existing suppression → permanent TTL ───────────────────────────


def test_graduate_extends_existing_suppression_to_permanent(app):
    client = app.test_client()

    # First mark a finding as reviewed at the default 30-day TTL.
    r1 = client.post(
        "/api/content-scan/mark-reviewed",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "html_comment_unknown",
            "line_range": [42],
            "excerpt": "<!-- foo:bar -->",
            "reviewer_note": "first review",
        },
    )
    assert r1.status_code == 200

    # Now graduate it. The endpoint reads the existing record's
    # excerpt; the operator only needs to supply the key fields.
    r2 = client.post(
        "/api/content-scan/graduate",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "html_comment_unknown",
            "line_range": [42],
        },
    )
    assert r2.status_code == 200
    payload = r2.get_json()
    assert payload["ok"] is True
    sup = payload["suppression"]
    # Excerpt was preserved from the original record
    assert sup["excerpt"] == "<!-- foo:bar -->"
    # TTL is now multi-year (PERMANENT_TTL_DAYS=3650 → ~9.9y)
    expires = datetime.strptime(
        sup["expires_at"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
    )
    reviewed = datetime.strptime(
        sup["reviewed_at"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
    )
    delta_days = (expires - reviewed).total_seconds() / 86400
    assert delta_days > 3000


def test_graduate_works_without_prior_suppression(app):
    """If the operator graduates a match that hasn't been reviewed
    yet, the endpoint still writes a permanent suppression (the
    excerpt is just empty)."""
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/graduate",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "html_comment_unknown",
            "line_range": [42],
            "reviewer_note": "operator-curated false positive",
        },
    )
    assert resp.status_code == 200
    sup = resp.get_json()["suppression"]
    assert sup["reviewer_note"] == "operator-curated false positive"


def test_graduate_requires_bot_id_file_pattern(app):
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/graduate",
        json={"bot_id": "team_bot_a", "file": "AGENTS.md", "line_range": [1]},
    )
    assert resp.status_code == 400
    assert "required" in resp.get_json()["error"].lower()


def test_graduate_requires_line_range(app):
    client = app.test_client()
    resp = client.post(
        "/api/content-scan/graduate",
        json={
            "bot_id": "team_bot_a",
            "file": "AGENTS.md",
            "pattern_id": "x",
            "line_range": [],
        },
    )
    assert resp.status_code == 400
    assert "line_range" in resp.get_json()["error"].lower()


# ── suppression module constants ─────────────────────────────────────────────


def test_permanent_ttl_constant_is_a_decade():
    from content_scan import suppressions

    assert suppressions.PERMANENT_TTL_DAYS == 3650
    # And default unchanged
    assert suppressions.DEFAULT_TTL_DAYS == 30
