"""tests/test_wizard_routes.py — Wizard PR β backend endpoint tests.

Tests the HTTP layer registered by `register_wizard_routes()`:

  POST /api/wizard/preview
  POST /api/wizard/provision
  POST /api/credentials/borrow
  GET  /api/wizard/borrow-candidates?provider=...

Strategy: build a minimal Flask app with the wizard routes registered
against a fake network.json. Mock the provision pipeline + filesystem
auth-profiles reads where needed. Each endpoint gets:
  - happy path
  - validation error path
  - permission / shape mismatch path

Helpers live in this file; no shared fixtures with the broader suite
beyond what conftest.py provides.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.web import wizard_routes  # noqa: E402
from evolve_admin.web.wizard_routes import (  # noqa: E402
    borrow_credentials,
    borrow_candidates,
    register_wizard_routes,
    suggest_bot_id_heuristic,
)
from evolve_admin import provisioning  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "port": 19000, "multiUser": False},
            "team_bot_a": {"role": "member", "port": 19010, "multiUser": False},
        },
        "members": ["evo", "team_bot_a"],
    }))
    return p


@pytest.fixture
def app(network_path: Path) -> Flask:
    flask_app = Flask(__name__)
    # Need a /api/jobs endpoint analog so provision tests can poll.
    # The real server registers it from server.py; our test stubs it.
    _jobs: dict[str, dict] = {}

    @flask_app.get("/api/jobs/<job_id>")
    def _stub_jobs(job_id: str):
        from flask import jsonify
        j = _jobs.get(job_id)
        if j is None:
            return jsonify({"error": "job not found"}), 404
        return jsonify(j)

    # Stash the dict on the app so tests can inject and provision()
    # can find it via the patched-in helpers.
    flask_app._test_jobs = _jobs  # type: ignore[attr-defined]
    register_wizard_routes(flask_app, network_path)
    return flask_app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


# ── suggest_bot_id_heuristic ─────────────────────────────────────────────────


def test_suggest_bot_id_heuristic_basic():
    assert suggest_bot_id_heuristic("research bot for openclaw enthusiasts") == "research"
    assert suggest_bot_id_heuristic("A travel planning assistant") == "travel"


def test_suggest_bot_id_heuristic_handles_empty_and_filler():
    assert suggest_bot_id_heuristic("") == "newbot"
    assert suggest_bot_id_heuristic("the and of an") == "newbot"
    # Pure punctuation + filler
    assert suggest_bot_id_heuristic("?!?") == "newbot"


def test_suggest_bot_id_heuristic_strips_punctuation():
    assert suggest_bot_id_heuristic("Atlas — community research") == "atlas"


def test_suggest_bot_id_heuristic_truncates_long_words():
    long_desc = "supercalifragilisticexpialidocious news"
    # First word truncated to 16 chars
    assert suggest_bot_id_heuristic(long_desc) == "supercalifragili"


def test_suggest_bot_id_heuristic_handles_digit_start():
    # Word starts with digit → prefixed with bot_
    assert suggest_bot_id_heuristic("1password integration") == "bot_1password"


# ── POST /api/wizard/preview ─────────────────────────────────────────────────


def test_preview_happy_path(client):
    """Clean request: returns resolved values + ok=true."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="atlas"), \
         patch.object(wizard_routes, "_user_exists", create=True, return_value=False), \
         patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510):

        r = client.post("/api/wizard/preview", json={
            "description": "research bot for openclaw enthusiasts",
        })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["suggested_bot_id"] == "atlas"
    assert body["resolved"]["bot_id"] == "atlas"
    assert body["resolved"]["user"] == "atlas"
    assert body["resolved"]["uid"] == 510
    # Next free port not 19000 (evo) or 19010 (team_bot_a)
    assert body["resolved"]["port"] == 19001
    assert body["resolved"]["role"] == "member"
    assert body["validation"]["issues"] == []


def test_preview_rejects_existing_bot_id(client):
    """If operator typed a bot_id already in network.json, surface that."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="evo"), \
         patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510):
        r = client.post("/api/wizard/preview", json={
            "bot_id": "evo",
            "description": "another bot",
        })
    body = r.get_json()
    assert body["ok"] is False
    issues = body["validation"]["issues"]
    assert any("already registered" in i["message"] for i in issues)
    assert any(i["field"] == "bot_id" for i in issues)


def test_preview_rejects_port_collision(client):
    """Explicit port that another bot uses → validation issue."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="atlas"), \
         patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510):
        r = client.post("/api/wizard/preview", json={
            "bot_id": "atlas",
            "port": 19010,  # team_bot_a's port
            "description": "atlas",
        })
    body = r.get_json()
    assert body["ok"] is False
    issues = body["validation"]["issues"]
    assert any("already used by 'team_bot_a'" in i["message"] for i in issues)


def test_preview_rejects_bad_bot_id_format(client):
    """Non-matching pattern → bot_id validation issue."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="ok_id"), \
         patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510):
        r = client.post("/api/wizard/preview", json={
            "bot_id": "Bad-Id!",
            "description": "x",
        })
    body = r.get_json()
    assert body["ok"] is False
    assert any(i["field"] == "bot_id" for i in body["validation"]["issues"])


def test_preview_uses_suggestion_when_bot_id_omitted(client):
    """No explicit bot_id → use the suggested one as resolved bot_id."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="suggested_name"), \
         patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_next_free_uid", return_value=510):
        r = client.post("/api/wizard/preview", json={"description": "x"})
    body = r.get_json()
    assert body["resolved"]["bot_id"] == "suggested_name"
    assert body["suggested_bot_id"] == "suggested_name"


def test_preview_flags_existing_macos_user_without_shared_flag(client):
    """User differs from bot_id AND exists, no allow_existing_user → issue."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="team_bot_b"), \
         patch.object(provisioning, "_user_exists", return_value=True), \
         patch.object(provisioning, "_next_free_uid", return_value=510):
        r = client.post("/api/wizard/preview", json={
            "bot_id": "team_bot_b",
            "user": "personal_bot_user",
            "description": "x",
        })
    body = r.get_json()
    assert body["ok"] is False
    assert any(i["field"] == "user" for i in body["validation"]["issues"])


def test_preview_accepts_existing_user_with_shared_flag(client):
    """Same situation + allow_existing_user → ok."""
    with patch.object(wizard_routes, "_suggest_bot_id_with_optional_llm",
                      return_value="team_bot_b"), \
         patch.object(provisioning, "_user_exists", return_value=True), \
         patch.object(provisioning, "_next_free_uid", return_value=510):
        r = client.post("/api/wizard/preview", json={
            "bot_id": "team_bot_b",
            "user": "personal_bot_user",
            "allow_existing_user": True,
            "description": "x",
        })
    body = r.get_json()
    assert body["ok"] is True


# ── POST /api/wizard/provision ────────────────────────────────────────────────


def test_provision_returns_job_id_immediately(client, app):
    """Endpoint kicks off a thread and returns 202 + jobId without
    blocking on the pipeline."""
    # Patch provision_bot to block, so we can verify the response
    # comes back before it completes.
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.bot_id = "atlas"
    mock_result.user = "atlas"
    mock_result.uid = 510
    mock_result.port = 19001
    mock_result.role = "member"
    mock_result.created_user = True
    mock_result.stage_log = []
    mock_result.rollback_log = []
    mock_result.failed_stage = None
    mock_result.error = None

    def _slow_provision(*args, **kwargs):
        on_stage = kwargs["on_stage"]
        on_stage("validate_inputs", "ok", "")
        return mock_result

    with patch.object(provisioning, "provision_bot",
                      side_effect=_slow_provision) as m_prov, \
         patch("evolve_admin.web.server._new_job",
               return_value="wizard-provision-test-123"), \
         patch("evolve_admin.web.server._job_log"), \
         patch("evolve_admin.web.server._job_finish"), \
         patch("evolve_admin.web.server._jobs", {"wizard-provision-test-123": {"result": {}}}), \
         patch("evolve_admin.web.server._jobs_lock", MagicMock()):
        r = client.post("/api/wizard/provision", json={
            "bot_id": "atlas",
            "port": 19031,
        })
    assert r.status_code == 202
    body = r.get_json()
    assert body["jobId"] == "wizard-provision-test-123"
    assert body["status"] == "started"
    # Give thread time to complete
    time.sleep(0.2)
    m_prov.assert_called_once()


def test_provision_rejects_missing_bot_id(client):
    r = client.post("/api/wizard/provision", json={})
    assert r.status_code == 400
    assert "bot_id required" in r.get_json()["error"]


def _provision_capture_run(client, body: dict) -> dict:
    """Helper: POST to /api/wizard/provision with a mocked provision_bot
    that captures the kwargs it was called with. Returns the captured
    kwargs dict for the caller to assert on."""
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.bot_id = body.get("bot_id", "atlas")
    mock_result.user = mock_result.bot_id
    mock_result.uid = 510
    mock_result.port = body.get("port", 19001)
    mock_result.role = "member"
    mock_result.created_user = True
    mock_result.stage_log = []
    mock_result.rollback_log = []
    mock_result.failed_stage = None
    mock_result.error = None

    captured_kwargs: dict = {}

    def _capture(*args, **kwargs):
        captured_kwargs.update(kwargs)
        kwargs["on_stage"]("validate_inputs", "ok", "")
        return mock_result

    job_id = f"wizard-provision-test-{body.get('bot_id', 'x')}"
    with patch.object(provisioning, "provision_bot", side_effect=_capture), \
         patch("evolve_admin.web.server._new_job", return_value=job_id), \
         patch("evolve_admin.web.server._job_log"), \
         patch("evolve_admin.web.server._job_finish"), \
         patch("evolve_admin.web.server._jobs", {job_id: {"result": {}}}), \
         patch("evolve_admin.web.server._jobs_lock", MagicMock()):
        r = client.post("/api/wizard/provision", json=body)
        assert r.status_code == 202, r.get_json()
        time.sleep(0.3)
    return captured_kwargs


def test_provision_threads_provider_api_key_and_auth_choice(client):
    """The wizard sends auth_choice + provider_api_key (provider-
    agnostic shape). Both must reach provision_bot as kwargs so
    _compose_onboard_args can emit the right --<provider>-api-key
    flag. This is the canonical shape after PR #1895's pivot to
    a provider chooser."""
    kwargs = _provision_capture_run(client, {
        "bot_id": "atlas",
        "port": 19031,
        "auth_choice": "openai-api-key",
        "provider_api_key": "sk-openai-test-key",
    })
    assert kwargs.get("auth_choice") == "openai-api-key", (
        f"Wizard endpoint dropped auth_choice — got {kwargs.get('auth_choice')!r}. "
        f"Without it, _compose_onboard_args can't pick the right "
        f"--<provider>-api-key flag."
    )
    assert kwargs.get("provider_api_key") == "sk-openai-test-key"


def test_provision_accepts_legacy_anthropic_api_key_field(client):
    """For one deprecation cycle the wizard endpoint accepts the old
    ``anthropic_api_key`` field name and maps it to provider_api_key
    with auth_choice defaulting to 'anthropic'. New callers should
    send auth_choice + provider_api_key explicitly; this back-compat
    path exists so CLI/script callers built against the pre-PR-#1895
    shape don't break overnight."""
    kwargs = _provision_capture_run(client, {
        "bot_id": "atlas",
        "port": 19031,
        "anthropic_api_key": "sk-ant-legacy-caller",
        # NOTE: no auth_choice sent — defaults to 'anthropic' for
        # back-compat when the legacy field is set.
    })
    assert kwargs.get("provider_api_key") == "sk-ant-legacy-caller"
    assert kwargs.get("auth_choice") == "anthropic"


def test_provision_resolves_borrow_provider_from_bot_to_raw_key(client):
    """Regression for the Screen-2 borrow path: when the wizard sends
    ``borrow_provider_from_bot`` instead of pasting a raw key, the
    backend reads the source bot's auth-profiles.json and resolves
    it to a raw key server-side. The frontend never sees the key.

    This is the path that lets the operator pick "Borrow from team_bot_a"
    on Screen 2 without retyping the key. Before this PR the borrow
    flow only existed on Screen 4 (post-provision), where it was
    too late to help openclaw onboard."""
    fake_source_profile = {
        "profiles": {
            "anthropic:api_key": {
                "type": "api_key",
                "key": "sk-ant-borrowed-from-sibling",
            },
        }
    }

    def fake_read(user):
        # team_bot_a is the seeded sibling in the test fixture.
        if user == "team_bot_a":
            return fake_source_profile
        return None

    with patch.object(wizard_routes, "_read_auth_profiles_safe", side_effect=fake_read):
        kwargs = _provision_capture_run(client, {
            "bot_id": "atlas",
            "port": 19031,
            "auth_choice": "anthropic",
            # NO provider_api_key in body — operator picked a borrow source.
            "borrow_provider_from_bot": "team_bot_a",
        })

    assert kwargs.get("provider_api_key") == "sk-ant-borrowed-from-sibling", (
        f"Borrow path must resolve to a raw key via "
        f"read_provider_key_from_bot. Got "
        f"provider_api_key={kwargs.get('provider_api_key')!r}. If this "
        f"returned None, the wizard's 'Borrow from <bot>' radio on "
        f"Screen 2 will silently fail — provision_bot runs with no "
        f"key and openclaw onboard exits with 'Run claude auth login "
        f"first.'"
    )
    # auth_choice still flows through unchanged.
    assert kwargs.get("auth_choice") == "anthropic"


def test_provision_no_auth_choice_default_for_principled_callers(client):
    """Without auth_choice AND without the legacy anthropic_api_key
    field, the wizard endpoint does NOT default auth_choice. Evolve
    is LLM-provider-agnostic by principle — never presume. provision_bot
    receives auth_choice=None and openclaw onboard surfaces a clear
    error for the operator."""
    kwargs = _provision_capture_run(client, {
        "bot_id": "atlas",
        "port": 19031,
        # No auth_choice, no provider_api_key, no anthropic_api_key.
    })
    assert kwargs.get("auth_choice") is None, (
        f"Wizard endpoint must NOT default auth_choice when the caller "
        f"doesn't specify one — Evolve is provider-agnostic and never "
        f"presumes Anthropic. Got auth_choice={kwargs.get('auth_choice')!r}."
    )
    assert kwargs.get("provider_api_key") is None


def test_provision_threads_custom_provider_fields(client):
    """For ``auth_choice='custom-api-key'`` (OpenAI- or Anthropic-
    compatible third-party endpoints), the four ``custom_*`` fields
    from Screen 2 must reach provision_bot. Without them,
    _compose_onboard_args can't emit the ``--custom-base-url`` /
    ``--custom-compatibility`` pair OC requires for a custom provider,
    and onboard fails with a validation error before reaching auth
    setup.
    """
    kwargs = _provision_capture_run(client, {
        "bot_id": "homelab",
        "port": 19033,
        "auth_choice": "custom-api-key",
        "provider_api_key": "sk-custom-test",
        "custom_base_url": "https://llm.homelab.local/v1",
        "custom_compatibility": "openai",
        "custom_provider_id": "homelab-vllm",
        "custom_model_id": "llama3:8b",
    })
    assert kwargs.get("auth_choice") == "custom-api-key"
    assert kwargs.get("provider_api_key") == "sk-custom-test"
    assert kwargs.get("custom_base_url") == "https://llm.homelab.local/v1"
    assert kwargs.get("custom_compatibility") == "openai"
    assert kwargs.get("custom_provider_id") == "homelab-vllm"
    assert kwargs.get("custom_model_id") == "llama3:8b"


def test_provision_local_provider_no_api_key_required(client):
    """For local providers (auth_choice="ollama" / "lmstudio"), the
    wizard's Screen 2 disables the API-key input — OC discovers the
    daemon from the environment. The wizard endpoint must accept the
    pair without complaint and pass it straight through; the local-
    provider key suppression happens in _compose_onboard_args via the
    AUTH_CHOICE_TO_KEY_FLAG map (ollama → None)."""
    kwargs = _provision_capture_run(client, {
        "bot_id": "localbot",
        "port": 19034,
        "auth_choice": "ollama",
        # No provider_api_key — local provider.
    })
    assert kwargs.get("auth_choice") == "ollama"
    assert kwargs.get("provider_api_key") is None


def test_provision_on_stage_survives_initial_none_result(client):
    """Regression: the wizard's on_stage callback used ``setdefault('result', {})``
    to lazily initialise the per-job result dict. But _new_job() in server.py
    seeds the job with ``result: None`` — a key that IS present, just with a
    None value. dict.setdefault only acts when the key is missing; when it
    exists with None, setdefault returns the existing None unchanged. The
    next line then tried ``j['result']['last_stage'] = stage`` and crashed
    with ``'NoneType' object does not support item assignment`` at the very
    first on_stage callback (validate_inputs:start). The wizard's progress
    pipeline died on every bot-creation attempt; surfaced 2026-05-31 mid-
    onboarding for the test pod's 9th bot.

    The fix normalises j['result'] to an empty dict before writing. This
    test exercises the on_stage path against a job seeded the same way
    _new_job seeds it (result: None) and asserts no exception escapes
    plus the result dict ends up populated with last_stage/last_status.
    """
    # Mock provision_bot so it triggers on_stage callbacks against
    # the _jobs[...] entry that _new_job would have created with
    # result=None. The crash happens INSIDE on_stage, in the wizard
    # thread, so the failure manifests as the thread silently dying
    # and the job stuck in "running" — we'll observe the populated
    # result dict as proof the callback path stayed alive.

    captured_result: dict = {}
    seeded_jobs: dict = {
        "wizard-provision-noneseed-456": {
            "jobId": "wizard-provision-noneseed-456",
            "type": "wizard-provision",
            "status": "running",
            "log": [],
            "progress": {"current": 0, "total": 0, "label": ""},
            "result": None,   # ← the bug repro: matches _new_job's init
            "error": None,
        },
    }

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.bot_id = "atlas"
    mock_result.user = "atlas"
    mock_result.uid = 510
    mock_result.port = 19001
    mock_result.role = "member"
    mock_result.created_user = True
    mock_result.stage_log = []
    mock_result.rollback_log = []
    mock_result.failed_stage = None
    mock_result.error = None

    def _provision_with_stages(*args, **kwargs):
        on_stage = kwargs["on_stage"]
        # Two callbacks: pre-fix, the first one crashed and the thread
        # died before the second ran. Post-fix, both write to result.
        on_stage("validate_inputs", "start", "")
        on_stage("validate_inputs", "ok", "uid=510 port=19001 user_exists=False")
        captured_result.update(
            seeded_jobs["wizard-provision-noneseed-456"]["result"] or {}
        )
        return mock_result

    with patch.object(provisioning, "provision_bot",
                      side_effect=_provision_with_stages), \
         patch("evolve_admin.web.server._new_job",
               return_value="wizard-provision-noneseed-456"), \
         patch("evolve_admin.web.server._job_log"), \
         patch("evolve_admin.web.server._job_finish"), \
         patch("evolve_admin.web.server._jobs", seeded_jobs), \
         patch("evolve_admin.web.server._jobs_lock", MagicMock()):
        r = client.post("/api/wizard/provision", json={
            "bot_id": "atlas",
            "port": 19031,
        })
        assert r.status_code == 202
        # Let the worker thread finish its two on_stage calls.
        time.sleep(0.3)

    # The first proof: provision_bot got both on_stage calls without the
    # callback raising (mock side_effect would have raised through).
    # The second proof: the result dict ended up populated with the
    # last_stage/last_status keys the callback writes, which means the
    # j['result'] = {} normalisation actually happened.
    final_result = seeded_jobs["wizard-provision-noneseed-456"]["result"]
    assert isinstance(final_result, dict), (
        f"After on_stage callbacks the job's result should be a dict, got "
        f"{type(final_result).__name__}. This is the regression — the bug "
        f"left it as None and the second on_stage call would have crashed."
    )
    assert final_result.get("last_stage") == "validate_inputs"
    assert final_result.get("last_status") == "ok"


# ── POST /api/credentials/borrow ─────────────────────────────────────────────


def test_borrow_happy_path(client, tmp_path: Path):
    """Copy brave + anthropic from evo to atlas (where atlas is in the network)."""
    # Add atlas to the fixture so it's a valid to_bot
    import json as _json
    # Patch the load_network call from the borrow handler to include atlas
    fake_network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "port": 19000},
            "atlas": {"role": "member", "port": 19031},
        },
        "members": ["evo", "atlas"],
    }
    source_profiles = {
        "profiles": {
            "anthropic:api_key": {"type": "api_key", "key": "sk-ant-evo"},
            "brave:api": {"type": "api_key", "key": "brave-evo-key"},
            "github:token": {"type": "token", "key": "ghp_xxx"},
        }
    }
    captured: dict[str, dict] = {}

    def fake_read(user):
        if user == "evo":
            return source_profiles
        return None

    def fake_write(user, data):
        captured[user] = data
        return True

    with patch.object(wizard_routes, "load_network", return_value=fake_network), \
         patch.object(wizard_routes, "_read_auth_profiles_safe", side_effect=fake_read), \
         patch.object(wizard_routes, "_write_auth_profiles_as_bot", side_effect=fake_write):
        r = client.post("/api/credentials/borrow", json={
            "from_bot": "evo",
            "to_bot": "atlas",
            "providers": ["brave", "anthropic"],
        })

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert "anthropic:api_key" in body["copied"]
    assert "brave:api" in body["copied"]
    # github not requested, not copied
    assert all("github" not in c for c in body["copied"])
    # Written entries have audit stamps
    atlas_profiles = captured["atlas"]["profiles"]
    assert atlas_profiles["anthropic:api_key"]["borrowed_from"] == "evo"
    assert "borrowed_at" in atlas_profiles["anthropic:api_key"]
    assert atlas_profiles["anthropic:api_key"]["key"] == "sk-ant-evo"


def test_borrow_rejects_missing_from_bot(client):
    r = client.post("/api/credentials/borrow", json={
        "to_bot": "atlas", "providers": ["brave"],
    })
    assert r.status_code == 400
    assert "from_bot" in r.get_json()["error"]


def test_borrow_rejects_same_from_and_to(client):
    r = client.post("/api/credentials/borrow", json={
        "from_bot": "evo", "to_bot": "evo", "providers": ["brave"],
    })
    assert r.status_code == 400


def test_borrow_accepts_primary_source_not_listed_in_members(client, tmp_path: Path):
    """On current-schema pods the primary lives in the sibling `primary`
    key and is NOT in `members` — yet it is the usual from_bot (it holds
    the keys). A members-only gate 400'd every borrow from it."""
    fake_network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "port": 19030},
            "atlas": {"role": "member", "port": 19031},
        },
        "members": ["atlas"],
    }
    with patch.object(wizard_routes, "load_network", return_value=fake_network), \
         patch.object(wizard_routes, "_read_auth_profiles_safe",
                      return_value={"profiles": {"brave:api": {"key": "b-key"}}}), \
         patch.object(wizard_routes, "_write_auth_profiles_as_bot", return_value=True):
        r = client.post("/api/credentials/borrow", json={
            "from_bot": "evo", "to_bot": "atlas", "providers": ["brave"],
        })
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ok"] is True


def test_borrow_rejects_non_pod_member_source(client, tmp_path: Path):
    """from_bot must be in network.json members."""
    fake_network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19000}},
        "members": ["evo"],
    }
    with patch.object(wizard_routes, "load_network", return_value=fake_network):
        r = client.post("/api/credentials/borrow", json={
            "from_bot": "ghost", "to_bot": "evo", "providers": ["brave"],
        })
    assert r.status_code == 400
    assert "not a pod member" in r.get_json()["error"]


def test_borrow_empty_providers_rejected(client):
    r = client.post("/api/credentials/borrow", json={
        "from_bot": "evo", "to_bot": "atlas", "providers": [],
    })
    assert r.status_code == 400


def test_borrow_no_matching_providers_returns_500(client, tmp_path: Path):
    """from_bot has no matching profiles → ok=false."""
    fake_network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "port": 19000},
            "atlas": {"role": "member", "port": 19031},
        },
        "members": ["evo", "atlas"],
    }
    with patch.object(wizard_routes, "load_network", return_value=fake_network), \
         patch.object(wizard_routes, "_read_auth_profiles_safe",
                      return_value={"profiles": {"github:token": {"key": "x"}}}):
        r = client.post("/api/credentials/borrow", json={
            "from_bot": "evo",
            "to_bot": "atlas",
            "providers": ["brave"],
        })
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    assert any(s["provider"] == "brave" for s in body["skipped"])


# ── borrow_credentials (unit) ────────────────────────────────────────────────


def test_borrow_credentials_unit_audit_stamp():
    """Every borrowed profile gets borrowed_from + borrowed_at."""
    network = {
        "members": ["evo", "atlas"],
        "bots": {
            "evo": {"role": "primary", "port": 19000},
            "atlas": {"role": "member", "port": 19031},
        },
    }
    src = {"profiles": {
        "brave:api": {"type": "api_key", "key": "secret"},
    }}
    captured: dict = {}
    with patch.object(wizard_routes, "_read_auth_profiles_safe",
                      side_effect=lambda u: src if u == "evo" else None), \
         patch.object(wizard_routes, "_write_auth_profiles_as_bot",
                      side_effect=lambda u, d: captured.update({u: d}) or True):
        result = borrow_credentials(
            from_bot="evo", to_bot="atlas",
            providers=["brave"], network=network,
        )
    assert result["ok"] is True
    entry = captured["atlas"]["profiles"]["brave:api"]
    assert entry["borrowed_from"] == "evo"
    assert entry["borrowed_at"].endswith("Z")
    # Original key preserved
    assert entry["key"] == "secret"
    # Source key NOT mutated (shallow copy)
    assert "borrowed_from" not in src["profiles"]["brave:api"]


# ── GET /api/wizard/borrow-candidates ────────────────────────────────────────


def test_borrow_candidates_lists_configured_bots(client, tmp_path: Path):
    fake_network = {
        "sharedDir": str(tmp_path),
        "members": ["evo", "team_bot_a", "atlas"],
        "bots": {
            "evo": {"role": "primary", "port": 19000},
            "team_bot_a": {"role": "member", "port": 19010},
            "atlas": {"role": "member", "port": 19031},
        },
    }

    def fake_read(user):
        if user == "evo":
            return {"profiles": {"brave:api": {"key": "x"}}}
        if user == "team_bot_a":
            return {"profiles": {"slack:bot_token": {"key": "y"}}}
        return None  # atlas has no profiles

    with patch.object(wizard_routes, "load_network", return_value=fake_network), \
         patch.object(wizard_routes, "_read_auth_profiles_safe", side_effect=fake_read):
        r = client.get("/api/wizard/borrow-candidates?provider=brave")
    assert r.status_code == 200
    body = r.get_json()
    assert body["provider"] == "brave"
    assert [b["bot_id"] for b in body["bots"]] == ["evo"]


def test_borrow_candidates_requires_provider_param(client):
    r = client.get("/api/wizard/borrow-candidates")
    assert r.status_code == 400


def test_borrow_candidates_empty_when_nobody_has_provider(client, tmp_path: Path):
    fake_network = {
        "sharedDir": str(tmp_path),
        "members": ["evo"],
        "bots": {"evo": {"role": "primary", "port": 19000}},
    }
    with patch.object(wizard_routes, "load_network", return_value=fake_network), \
         patch.object(wizard_routes, "_read_auth_profiles_safe",
                      return_value={"profiles": {}}):
        r = client.get("/api/wizard/borrow-candidates?provider=brave")
    assert r.status_code == 200
    assert r.get_json()["bots"] == []


# ── borrow_candidates (unit) ─────────────────────────────────────────────────


def test_borrow_candidates_unit_skips_bots_with_no_file():
    network = {
        "members": ["evo", "team_bot_a"],
        "bots": {
            "evo": {"role": "primary", "port": 19000},
            "team_bot_a": {"role": "member", "port": 19010},
        },
    }
    with patch.object(wizard_routes, "_read_auth_profiles_safe",
                      return_value=None):
        out = borrow_candidates("brave", network)
    assert out == []
