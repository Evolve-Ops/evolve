"""Tests for /api/lifecycle/{detach,retire,delete} + /api/remove deprecation.

Replaces the broken /api/remove endpoint with three explicit lifecycle
paths. The old endpoint expected ``botId`` (camelCase) while the only
caller sent ``bot_id`` (snake_case), so every click of "🗑 Remove Bot"
returned ``"botId required"``. The redesign:

  * removes /api/remove (returns 410 Gone with pointer)
  * adds /api/lifecycle/detach   → keep bot, stop Evolve
  * adds /api/lifecycle/retire   → archive + stop daemons (reversible)
  * adds /api/lifecycle/delete   → also nukes macOS user + home dir

The delete endpoint requires ``confirmation: "DELETE"`` server-side as
belt-and-suspenders to the UI modal's typed-DELETE field.

All three accept ``botId`` (preferred, matches the rest of the API) OR
``bot_id`` (fallback, so any in-flight callers from the broken era
don't break twice). The fallback also serves as the regression test
for the original mismatch bug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))


from evolve_admin.web.server import create_app  # noqa: E402
from evolve_admin.retire import RetireResult  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    """Minimal pod with a primary + a member bot. Tests target the
    member bot 'lex' so they don't tangle with primary-protection
    behavior (which is its own can of worms)."""
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "primary": "primary_bot",
        "members": ["primary_bot", "lex"],
        "bots": {
            "primary_bot": {"user": "primary_bot", "role": "primary"},
            "lex":         {"user": "lex",         "role": "member"},
        },
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()
    return path


@pytest.fixture
def client(network_path: Path):
    app = create_app(network_path)
    app.testing = True
    return app.test_client()


def _ok_result(bot_id: str) -> RetireResult:
    """A success RetireResult shape — same surface for all three paths."""
    r = RetireResult(bot_id=bot_id, dry_run=False)
    r.log("step 1 ok")
    r.plists_stopped = [f"ai.openclaw.{bot_id}-gateway"]
    return r


# ── /api/remove deprecation ──────────────────────────────────────────────────


class TestApiRemoveDeprecation:
    def test_returns_410_gone(self, client):
        r = client.post("/api/remove", json={"botId": "lex"})
        assert r.status_code == 410, r.get_data(as_text=True)

    def test_error_message_lists_replacement_endpoints(self, client):
        r = client.post("/api/remove", json={"botId": "lex"})
        body = r.get_json()
        assert "deprecated" in body["error"]
        # All three new endpoints must be named so callers know where to go
        assert "/api/lifecycle/detach" in body["error"]
        assert "/api/lifecycle/retire" in body["error"]
        assert "/api/lifecycle/delete" in body["error"]

    def test_deprecation_fires_even_when_botid_missing(self, client):
        """The 410 takes precedence over the old 400-missing-botId
        response — callers shouldn't see "botId required" only to chase
        the field name and find the whole endpoint is gone."""
        r = client.post("/api/remove", json={})
        assert r.status_code == 410


# ── /api/lifecycle/detach ────────────────────────────────────────────────────


class TestApiLifecycleDetach:
    def test_calls_remove_evolve_plugin(self, client):
        with patch("evolve_admin.retire.remove_evolve_plugin") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/detach", json={"botId": "lex"},
            )
        assert r.status_code == 200
        assert mock_fn.call_count == 1
        # First positional arg is the bot_id
        assert mock_fn.call_args.args[0] == "lex"

    def test_accepts_snake_case_bot_id(self, client):
        """Regression test for the original camelCase/snake_case bug.
        The old broken frontend sent {bot_id: ...} which the server
        threw away. We accept either now."""
        with patch("evolve_admin.retire.remove_evolve_plugin") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/detach", json={"bot_id": "lex"},
            )
        assert r.status_code == 200
        assert mock_fn.call_args.args[0] == "lex"

    def test_missing_bot_id_400(self, client):
        r = client.post("/api/lifecycle/detach", json={})
        assert r.status_code == 400
        assert "botId" in r.get_json()["error"]

    def test_passes_dry_run_through(self, client):
        with patch("evolve_admin.retire.remove_evolve_plugin") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            client.post(
                "/api/lifecycle/detach",
                json={"botId": "lex", "dryRun": True},
            )
        assert mock_fn.call_args.kwargs.get("dry_run") is True

    def test_response_includes_plists_stopped(self, client):
        with patch("evolve_admin.retire.remove_evolve_plugin") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/detach", json={"botId": "lex"},
            )
        body = r.get_json()
        # The richer RetireResult dict surfaces plists_stopped so the
        # UI can render "stopped N daemons" without a second call.
        assert "plists_stopped" in body
        assert body["plists_stopped"] == ["ai.openclaw.lex-gateway"]


# ── /api/lifecycle/retire ────────────────────────────────────────────────────


class TestApiLifecycleRetire:
    def test_calls_retire_bot(self, client):
        with patch("evolve_admin.retire.retire_bot") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/retire", json={"botId": "lex"},
            )
        assert r.status_code == 200
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.args[0] == "lex"

    def test_accepts_snake_case_bot_id(self, client):
        with patch("evolve_admin.retire.retire_bot") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/retire", json={"bot_id": "lex"},
            )
        assert r.status_code == 200

    def test_propagates_failure_with_500(self, client):
        bad = RetireResult(bot_id="lex", dry_run=False)
        bad.error("archive failed")
        with patch("evolve_admin.retire.retire_bot") as mock_fn:
            mock_fn.return_value = bad
            r = client.post(
                "/api/lifecycle/retire", json={"botId": "lex"},
            )
        assert r.status_code == 500
        assert r.get_json()["success"] is False

    def test_response_includes_archive_path(self, client):
        ok = _ok_result("lex")
        ok.archive_path = Path("/tmp/fake/retired/lex-2026-05-31")
        with patch("evolve_admin.retire.retire_bot") as mock_fn:
            mock_fn.return_value = ok
            r = client.post(
                "/api/lifecycle/retire", json={"botId": "lex"},
            )
        body = r.get_json()
        # archive_path is what the UI shows the operator after a
        # successful retire ("archive lives at …")
        assert body["archive_path"] == "/tmp/fake/retired/lex-2026-05-31"


# ── /api/lifecycle/delete ────────────────────────────────────────────────────


class TestApiLifecycleDelete:
    def test_requires_confirmation_field(self, client):
        """Without ``confirmation: "DELETE"`` the server refuses before
        touching anything. Belt-and-suspenders to the UI modal —
        a tampered frontend can't sneak past."""
        with patch("evolve_admin.retire.delete_bot") as mock_fn:
            r = client.post(
                "/api/lifecycle/delete", json={"botId": "lex"},
            )
        assert r.status_code == 400
        assert "DELETE" in r.get_json()["error"]
        # And the underlying function was never called — bail-before-touch
        assert mock_fn.call_count == 0

    def test_rejects_wrong_case_confirmation(self, client):
        """Confirmation must be exact, case-sensitive. 'delete' != 'DELETE'."""
        with patch("evolve_admin.retire.delete_bot") as mock_fn:
            r = client.post(
                "/api/lifecycle/delete",
                json={"botId": "lex", "confirmation": "delete"},
            )
        assert r.status_code == 400
        assert mock_fn.call_count == 0

    def test_calls_delete_bot_when_confirmation_correct(self, client):
        with patch("evolve_admin.retire.delete_bot") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/delete",
                json={"botId": "lex", "confirmation": "DELETE"},
            )
        assert r.status_code == 200
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.args[0] == "lex"

    def test_dry_run_skips_confirmation_gate(self, client):
        """Dry-run can't damage anything, so the typed-DELETE gate
        doesn't fire. Operators rely on dry-run as a safe preview."""
        with patch("evolve_admin.retire.delete_bot") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/delete",
                json={"botId": "lex", "dryRun": True},
            )
        assert r.status_code == 200
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.kwargs.get("dry_run") is True

    def test_accepts_snake_case_bot_id_with_confirmation(self, client):
        with patch("evolve_admin.retire.delete_bot") as mock_fn:
            mock_fn.return_value = _ok_result("lex")
            r = client.post(
                "/api/lifecycle/delete",
                json={"bot_id": "lex", "confirmation": "DELETE"},
            )
        assert r.status_code == 200

    def test_missing_bot_id_returns_400_before_confirmation_check(self, client):
        """Order matters: missing botId is reported as such, not as
        "confirmation required". The operator needs the actionable
        error first."""
        r = client.post(
            "/api/lifecycle/delete",
            json={"confirmation": "DELETE"},
        )
        assert r.status_code == 400
        assert "botId" in r.get_json()["error"]
