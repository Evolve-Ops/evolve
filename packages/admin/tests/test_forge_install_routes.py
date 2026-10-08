"""tests/test_forge_install_routes.py — forge install API endpoint tests.

Covers the four endpoints registered by
``evolve_admin.web.forge_install_routes.register_forge_install_routes``:

  * POST /api/forge/install/launch-agent
  * POST /api/forge/install/crontab
  * POST /api/forge/install/heartbeat-instruction   (v17, PR 13)
  * POST /api/forge/install/openclaw-patch          (DEPRECATED v17 — 410 Gone)

Locks two layers:

1. **Validation surface** (carried through from PR 1) — required-field
   checks return 400 with a ``missing`` list.

2. **Helper dispatch** (PR 4 + PR 13) — endpoints call into
   ``install_helpers`` and pass through the envelope; success → 200,
   helper failure → 502. The crontab endpoint stays 501 across all
   valid bodies (helper deliberately not implemented; see spec §4.1 +
   install_helpers.install_crontab_entry docstring). The openclaw-patch
   endpoint always returns 410 Gone in v17 — OpenClaw has no
   ``hooks.heartbeat[]`` array, so the prior hook-append path was
   structurally wrong (spec-heartbeat-instruction §1). The endpoint is
   kept for one schema version with a clear pointer at the replacement.

Helpers themselves are tested in ``test_install_helpers.py``; here we
mock them and assert the route translation layer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.web.forge_install_routes import (  # noqa: E402
    register_forge_install_routes,
)


@pytest.fixture
def client():
    app = Flask(__name__)
    register_forge_install_routes(app)
    return app.test_client()


# ── /api/forge/install/launch-agent ──────────────────────────────────────────


def test_launch_agent_requires_bot_id_label_and_plist(client):
    """Each required field surfaces in the ``missing`` list."""
    res = client.post("/api/forge/install/launch-agent", json={})
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "missing_fields"
    assert set(body["missing"]) == {"bot_id", "label", "plist_xml"}


def test_launch_agent_returns_200_on_helper_success(client):
    """A successful install returns 200 with artifact + loaded fields."""
    fake_result = {
        "ok": True,
        "artifact": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
        "error": "",
        "loaded": True,
    }
    with patch("evolve_admin.web.forge_install_routes.install_helpers.install_launch_agent",
               return_value=fake_result):
        res = client.post("/api/forge/install/launch-agent", json={
            "bot_id": "personal-bot",
            "label": "com.personal-bot.task-manager.check",
            "plist_xml": "<?xml version='1.0'?><plist><dict/></plist>",
        })
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["artifact"].endswith(".plist")
    assert body["loaded"] is True
    assert body["bot_id"] == "personal-bot"
    assert body["label"] == "com.personal-bot.task-manager.check"


def test_launch_agent_returns_502_on_helper_failure(client):
    fake_result = {"ok": False, "artifact": "", "error": "could not write plist"}
    with patch("evolve_admin.web.forge_install_routes.install_helpers.install_launch_agent",
               return_value=fake_result):
        res = client.post("/api/forge/install/launch-agent", json={
            "bot_id": "personal-bot",
            "label": "com.personal-bot.task-manager.check",
            "plist_xml": "<plist/>",
        })
    assert res.status_code == 502
    body = res.get_json()
    assert body["ok"] is False
    assert "could not write plist" in body["error"]


def test_launch_agent_partial_body_lists_only_missing(client):
    res = client.post("/api/forge/install/launch-agent", json={
        "bot_id": "personal-bot",
        "plist_xml": "<plist/>",
    })
    assert res.status_code == 400
    assert res.get_json()["missing"] == ["label"]


# ── /api/forge/install/crontab ───────────────────────────────────────────────


def test_crontab_requires_all_four_fields(client):
    res = client.post("/api/forge/install/crontab", json={})
    assert res.status_code == 400
    assert set(res.get_json()["missing"]) == {
        "bot_id", "label", "schedule", "command",
    }


def test_crontab_returns_501_with_deferred_error(client):
    """Even with a valid body, the endpoint returns 501 because the helper
    is deliberately unimplemented in PR 4 (sudoers gap)."""
    res = client.post("/api/forge/install/crontab", json={
        "bot_id": "team-bot-c",
        "label": "team-bot-c.task-check",
        "schedule": "0 */4 * * *",
        "command": "python3 /Users/team-bot-c/.openclaw/workspace/scripts/tasks.py check",
    })
    assert res.status_code == 501
    body = res.get_json()
    assert body["ok"] is False
    assert "not implemented" in body["error"].lower()
    assert body["entry_id"] is None
    assert body["label"] == "team-bot-c.task-check"


# ── /api/forge/install/heartbeat-instruction (v17, PR 13) ────────────────────


def test_heartbeat_instruction_requires_all_four_fields(client):
    """Each required field surfaces in the ``missing`` list."""
    res = client.post("/api/forge/install/heartbeat-instruction", json={})
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "missing_fields"
    assert set(body["missing"]) == {"bot_id", "file", "section_anchor", "body"}


def test_heartbeat_instruction_partial_body_lists_only_missing(client):
    res = client.post("/api/forge/install/heartbeat-instruction", json={
        "bot_id": "personal-bot",
        "file": "HEARTBEAT.md",
    })
    assert res.status_code == 400
    assert set(res.get_json()["missing"]) == {"section_anchor", "body"}


def test_heartbeat_instruction_returns_200_on_helper_success(client):
    """A successful install returns 200 with artifact + file echoed."""
    fake_result = {
        "ok": True,
        "artifact": "HEARTBEAT.md#Task Manager — Check",
        "error": "",
        "already_present": False,
        "created_file": False,
    }
    with patch(
        "evolve_admin.web.forge_install_routes.install_helpers.install_heartbeat_instruction",
        return_value=fake_result,
    ):
        res = client.post("/api/forge/install/heartbeat-instruction", json={
            "bot_id": "personal-bot",
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Check",
            "body": "Run `python3 scripts/tasks.py check` to surface overdue tasks.",
            "pkg_id": "p-9bfa1c84",
            "job_id": "j-abc123",
        })
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["artifact"] == "HEARTBEAT.md#Task Manager — Check"
    assert body["bot_id"] == "personal-bot"
    assert body["file"] == "HEARTBEAT.md"
    assert body["already_present"] is False
    assert body["created_file"] is False


def test_heartbeat_instruction_already_present_is_200_not_502(client):
    """Idempotency: a re-install of an up-to-date section returns ok with
    already_present=True, status 200."""
    fake_result = {
        "ok": True,
        "artifact": "HEARTBEAT.md#Task Manager — Check",
        "error": "",
        "already_present": True,
        "created_file": False,
    }
    with patch(
        "evolve_admin.web.forge_install_routes.install_helpers.install_heartbeat_instruction",
        return_value=fake_result,
    ):
        res = client.post("/api/forge/install/heartbeat-instruction", json={
            "bot_id": "personal-bot",
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Check",
            "body": "Run `python3 scripts/tasks.py check`.",
        })
    assert res.status_code == 200
    assert res.get_json()["already_present"] is True


def test_heartbeat_instruction_returns_502_on_helper_failure(client):
    """Helper failure (e.g. write rejected, marker mismatch) bubbles to 502."""
    fake_result = {
        "ok": False,
        "artifact": "",
        "error": "refusing to clobber section without evolve-managed marker",
    }
    with patch(
        "evolve_admin.web.forge_install_routes.install_helpers.install_heartbeat_instruction",
        return_value=fake_result,
    ):
        res = client.post("/api/forge/install/heartbeat-instruction", json={
            "bot_id": "personal-bot",
            "file": "HEARTBEAT.md",
            "section_anchor": "## Existing Operator Section",
            "body": "Body that would clobber an operator-authored block.",
        })
    assert res.status_code == 502
    body = res.get_json()
    assert body["ok"] is False
    assert "refusing to clobber" in body["error"]
    # File field is still echoed on failure for client convenience.
    assert body["file"] == "HEARTBEAT.md"


def test_heartbeat_instruction_pkg_and_job_id_optional(client):
    """pkg_id and job_id default to empty strings — helper is called with
    those as empty rather than raising 400."""
    fake_result = {
        "ok": True,
        "artifact": "AGENTS.md#Some Anchor",
        "error": "",
        "already_present": False,
        "created_file": True,
    }
    with patch(
        "evolve_admin.web.forge_install_routes.install_helpers.install_heartbeat_instruction",
        return_value=fake_result,
    ) as mock_helper:
        res = client.post("/api/forge/install/heartbeat-instruction", json={
            "bot_id": "personal-bot",
            "file": "AGENTS.md",
            "section_anchor": "## Some Anchor",
            "body": "Instruction body.",
        })
    assert res.status_code == 200
    # Helper was called with empty-string pkg_id/job_id (the defaults).
    _, kwargs = mock_helper.call_args
    assert kwargs["pkg_id"] == ""
    assert kwargs["job_id"] == ""


# ── /api/forge/install/openclaw-patch (DEPRECATED v17 — 410 Gone) ────────────


def test_openclaw_patch_requires_bot_id_pointer_and_value(client):
    """Required-field validation still runs before the 410. Empty body still
    yields 400 ``missing_fields`` because we need to know which fields are
    being attempted before we can give a useful deprecation message."""
    res = client.post("/api/forge/install/openclaw-patch", json={})
    assert res.status_code == 400
    missing = set(res.get_json()["missing"])
    assert "bot_id" in missing or "json_pointer" in missing


def test_openclaw_patch_value_omitted_returns_400(client):
    """``value`` is still presence-checked separately to surface a clean
    error before the 410."""
    res = client.post("/api/forge/install/openclaw-patch", json={
        "bot_id": "personal-bot",
        "json_pointer": "/hooks/heartbeat/-",
    })
    assert res.status_code == 400
    assert "value" in res.get_json()["missing"]


def test_openclaw_patch_well_formed_request_returns_410_gone(client):
    """The append-shape pointer that PR 4 supported now returns 410 Gone
    with a clear pointer at the v17 replacement endpoint."""
    res = client.post("/api/forge/install/openclaw-patch", json={
        "bot_id": "personal-bot",
        "json_pointer": "/hooks/heartbeat/-",
        "value": {"command": "python3 scripts/tasks.py check"},
    })
    assert res.status_code == 410
    body = res.get_json()
    assert body["ok"] is False
    assert body["method"] == "patch_openclaw_json"
    assert body["bot_id"] == "personal-bot"
    assert body["json_pointer"] == "/hooks/heartbeat/-"
    # Error message names the replacement endpoint + the spec.
    assert "heartbeat-instruction" in body["error"]
    assert "spec-heartbeat-instruction" in body["error"]


def test_openclaw_patch_any_pointer_shape_returns_410(client):
    """Even pointer shapes that PR 4 rejected as 501 now consistently 410.
    The endpoint is gone — there is no second-tier behaviour to preserve."""
    for pointer in (
        "/hooks/heartbeat/-",
        "/hooks/heartbeat/2",
        "/agents/defaults/model",
    ):
        res = client.post("/api/forge/install/openclaw-patch", json={
            "bot_id": "personal-bot",
            "json_pointer": pointer,
            "value": {"command": "noop"},
        })
        assert res.status_code == 410, f"expected 410 for pointer={pointer}"
        body = res.get_json()
        assert body["ok"] is False
        assert "heartbeat-instruction" in body["error"]


def test_openclaw_patch_does_not_call_install_oc_hook(client):
    """Defence in depth: ensure the deprecated endpoint never reaches the
    underlying helper, even when given a well-formed body."""
    with patch(
        "evolve_admin.web.forge_install_routes.install_helpers.install_oc_hook",
    ) as mock_helper:
        res = client.post("/api/forge/install/openclaw-patch", json={
            "bot_id": "personal-bot",
            "json_pointer": "/hooks/heartbeat/-",
            "value": {"command": "python3 scripts/tasks.py check"},
        })
    assert res.status_code == 410
    mock_helper.assert_not_called()


# ── Cross-cutting: empty body + non-JSON ─────────────────────────────────────


def test_empty_body_returns_400(client):
    """POST with no body at all is treated as missing all required fields."""
    res = client.post("/api/forge/install/launch-agent", data=b"", content_type="application/json")
    assert res.status_code == 400


def test_non_json_body_returns_400(client):
    """``silent=True`` on get_json means malformed JSON yields ``None`` rather
    than a 500. Validator then catches it as missing fields."""
    res = client.post(
        "/api/forge/install/crontab",
        data=b"not json at all",
        content_type="application/json",
    )
    assert res.status_code == 400
