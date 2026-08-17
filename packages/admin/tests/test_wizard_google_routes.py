"""tests/test_wizard_google_routes.py — Wizard PR ε backend endpoint tests.

Covers the path-C Google integration wizard registered by
``register_google_wizard_routes()``:

  GET  /api/wizard/google/scopes
  GET  /api/wizard/google/state
  POST /api/wizard/google/upload-sa
  POST /api/wizard/google/configure-bot
  GET  /api/wizard/google/share-prompt

Strategy: build a tiny Flask app with the wizard routes registered
against a temp ``network.json`` and a temp ``secrets/`` dir. Where
sudo/cp would normally land, we monkeypatch ``subprocess.run`` to
treat ``sudo /bin/cp``, ``sudo /bin/mkdir``, ``sudo /usr/sbin/chown``,
and ``sudo /bin/chmod`` as direct filesystem ops scoped to the test
tree (so the test never actually invokes sudo).

The pre-flight Gmail call is mocked at the module-level
``_run_preflight`` symbol; we exercise both success and failure paths.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.web import wizard_google_routes as wgr  # noqa: E402
from evolve_admin.web.wizard_google_routes import (  # noqa: E402
    SCOPE_CATALOG,
    build_share_prompt,
    register_google_wizard_routes,
    validate_sa_json,
    _apply_bot_config,
    _validate_configure_payload,
    _validate_secret_ref,
)


# ── Subprocess stub that emulates sudo /bin/* into the local fs ─────────────


class _FakeRun:
    """Treat the four sudo calls the wizard makes as plain filesystem ops.

    Returns the same shape as ``subprocess.run(..., capture_output=True)``:
    a CompletedProcess with stdout/stderr/returncode. Anything that's not
    one of the four known invocations falls through to a 0-return-code
    no-op so the test isn't brittle to ancillary sudo calls.
    """

    class _CP:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = stderr

    def __call__(self, argv, **kwargs):
        # argv may be list[str] or str depending on shell=...
        if not isinstance(argv, list) or len(argv) < 2:
            return self._CP()
        # sudo /bin/cp <src> <dst>
        if argv[0] == "sudo" and argv[1] == "/bin/cp" and len(argv) >= 4:
            try:
                shutil.copy2(argv[2], argv[3])
                return self._CP(0)
            except Exception as exc:
                return self._CP(1, str(exc))
        # sudo /bin/mkdir -p <path>
        if argv[0] == "sudo" and argv[1] == "/bin/mkdir":
            target = argv[-1]
            try:
                Path(target).mkdir(parents=True, exist_ok=True)
                return self._CP(0)
            except Exception as exc:
                return self._CP(1, str(exc))
        # sudo /usr/sbin/chown ... → no-op for tests (we're not root)
        if argv[0] == "sudo" and argv[1] == "/usr/sbin/chown":
            return self._CP(0)
        # sudo /bin/chmod <mode> <path>
        if argv[0] == "sudo" and argv[1] == "/bin/chmod" and len(argv) >= 4:
            return self._CP(0)
        return self._CP(0)


@pytest.fixture
def fake_sudo():
    with patch.object(wgr.subprocess, "run", new=_FakeRun()):
        yield


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {
            "evo": {
                "role": "primary",
                "port": 19000,
                "display_name": "Evo",
                "primary_user": {"name": "Sam"},
            },
            "lex": {
                "role": "member",
                "port": 19010,
                "display_name": "Lex",
                "primary_user": {"name": "Sam"},
            },
        },
        "members": ["evo", "lex"],
    }))
    return p


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    d = tmp_path / "google_service_accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def app(network_path: Path, secrets_dir: Path) -> Flask:
    flask_app = Flask(__name__)
    register_google_wizard_routes(flask_app, network_path, secrets_dir=secrets_dir)
    # Patch save_network's sudo fallback so writes in the test tree don't
    # actually invoke sudo (the network.json is in tmp_path with mode
    # owned by the test user; the direct shutil.copy path succeeds).
    return flask_app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


def _sa_json(project_id: str = "evolve-test") -> dict:
    """Build a fake-but-shape-correct SA JSON."""
    return {
        "type": "service_account",
        "project_id": project_id,
        "private_key_id": "abc123",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": f"evolve-google-integration@{project_id}.iam.gserviceaccount.com",
        "client_id": "123456789012345678901",
    }


# ── validate_sa_json ────────────────────────────────────────────────────────


def test_validate_sa_json_happy():
    ok, err = validate_sa_json(_sa_json())
    assert ok and err is None


def test_validate_sa_json_rejects_oauth_client_mistake():
    # OAuth client JSON has type 'authorized_user' or 'web' / 'installed'
    ok, err = validate_sa_json({"type": "authorized_user", "client_id": "x"})
    assert not ok
    assert "service-account key" in err
    assert "OAuth client JSON" in err


def test_validate_sa_json_rejects_missing_fields():
    bad = _sa_json()
    del bad["private_key"]
    ok, err = validate_sa_json(bad)
    assert not ok
    assert "private_key" in err


def test_validate_sa_json_rejects_non_dict():
    ok, err = validate_sa_json("not a dict")
    assert not ok


# ── _validate_secret_ref ────────────────────────────────────────────────────


def test_validate_secret_ref_accepts_canonical():
    ok, err = _validate_secret_ref("google-sa-example-corp")
    assert ok and err is None


def test_validate_secret_ref_rejects_path_separators():
    ok, err = _validate_secret_ref("../evil")
    assert not ok
    assert "letters, digits" in err


def test_validate_secret_ref_rejects_empty():
    ok, err = _validate_secret_ref("")
    assert not ok


def test_validate_secret_ref_rejects_too_long():
    ok, err = _validate_secret_ref("x" * 65)
    assert not ok


# ── GET /scopes ─────────────────────────────────────────────────────────────


def test_get_scopes(client):
    r = client.get("/api/wizard/google/scopes")
    assert r.status_code == 200
    data = r.get_json()
    assert "scopes" in data
    assert len(data["scopes"]) == len(SCOPE_CATALOG)
    # Required scopes the wizard's default set always offers
    ids = {s["id"] for s in data["scopes"]}
    assert "https://www.googleapis.com/auth/gmail.send" in ids
    assert "https://www.googleapis.com/auth/calendar" in ids


# ── GET /state ──────────────────────────────────────────────────────────────


def test_state_empty(client):
    r = client.get("/api/wizard/google/state")
    assert r.status_code == 200
    data = r.get_json()
    assert data["secrets"] == []
    assert data["screen1_needed"] is True
    assert data["bot"] is None
    assert "https://www.googleapis.com/auth/gmail.send" in data["default_scopes"]


def test_state_with_bot_no_config(client):
    r = client.get("/api/wizard/google/state?bot_id=lex")
    data = r.get_json()
    assert data["bot"]["bot_id"] == "lex"
    assert data["bot"]["configured"] is False


def test_state_lists_installed_secret(client, secrets_dir: Path, fake_sudo):
    # Simulate a previously-installed SA + meta.
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))
    (secrets_dir / "google-sa-example-corp.meta.json").write_text(json.dumps({
        "workspace_domain": "example-corp.com",
        "client_id": "123456789012345678901",
        "client_email": "evolve-google-integration@evolve-test.iam.gserviceaccount.com",
    }))
    r = client.get("/api/wizard/google/state")
    data = r.get_json()
    assert data["screen1_needed"] is False
    assert len(data["secrets"]) == 1
    s = data["secrets"][0]
    assert s["secret_ref"] == "google-sa-example-corp"
    assert s["workspace_domain"] == "example-corp.com"
    assert s["bots_using"] == []


# ── POST /upload-sa ─────────────────────────────────────────────────────────


def test_upload_sa_happy(client, secrets_dir: Path, fake_sudo):
    r = client.post("/api/wizard/google/upload-sa", json={
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "sa_json": _sa_json(),
    })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["secret_ref"] == "google-sa-example-corp"
    # File should now be on disk
    assert (secrets_dir / "google-sa-example-corp.json").exists()
    assert (secrets_dir / "google-sa-example-corp.meta.json").exists()


def test_upload_sa_rejects_oauth_client_mistake(client, fake_sudo):
    r = client.post("/api/wizard/google/upload-sa", json={
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "sa_json": {"type": "authorized_user", "client_id": "x"},
    })
    assert r.status_code == 400
    assert "OAuth client JSON" in r.get_json()["error"]


def test_upload_sa_rejects_bad_secret_ref(client, fake_sudo):
    r = client.post("/api/wizard/google/upload-sa", json={
        "secret_ref": "../escape",
        "workspace_domain": "example-corp.com",
        "sa_json": _sa_json(),
    })
    assert r.status_code == 400


def test_upload_sa_rejects_bad_domain(client, fake_sudo):
    r = client.post("/api/wizard/google/upload-sa", json={
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "not-a-domain",
        "sa_json": _sa_json(),
    })
    assert r.status_code == 400


# ── _validate_configure_payload ─────────────────────────────────────────────


def test_validate_configure_happy():
    errors, cleaned = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "soft"},
    })
    assert errors == []
    assert cleaned["bot_id"] == "lex"
    assert cleaned["scopes"] == ["https://www.googleapis.com/auth/gmail.send"]
    assert cleaned["persona"]["name"] == "Jane"
    assert cleaned["persona"]["disclosure"] == "soft"


def test_validate_configure_rejects_off_domain_subject():
    errors, _ = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@OTHER-CORP.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "soft"},
    })
    assert any("not on the Workspace domain" in e for e in errors)


def test_validate_configure_requires_disclosure_reason_when_none():
    errors, _ = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "none"},
    })
    assert any("disclosure_override_reason" in e for e in errors)


def test_validate_configure_rejects_bad_disclosure():
    errors, _ = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "moderate"},
    })
    assert any("persona.disclosure" in e for e in errors)


def test_validate_configure_requires_scopes():
    errors, _ = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": [],
    })
    assert any("at least one scope" in e for e in errors)


def test_validate_configure_no_persona_no_correspondence():
    # Read-only scopes tolerate an empty persona — the bot won't send mail,
    # so the From header is never built and the missing alias never matters.
    errors, cleaned = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    })
    assert errors == []
    # No name → no persona block emitted (bot is internal-only)
    assert cleaned["persona"] == {}


def test_validate_configure_requires_persona_when_outbound_mail_scope():
    """Empty persona + gmail.send must fail validation — the 2026-06-02 regression.

    Discovered during a personal-bot path-C walkthrough: the alias
    field's "Jane" placeholder was mistaken for a pre-filled value,
    the wizard accepted the empty persona silently, and `gmail_send`
    failed weeks later when it tried to build the From header.
    Backend now refuses to write a half-configured bot.
    """
    errors, _ = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        # persona omitted on purpose
    })
    assert any("alias name is required" in e for e in errors), errors


def test_validate_configure_requires_persona_when_gmail_modify_scope():
    """gmail.modify also implies outbound (label/move/delete on user mail)."""
    errors, _ = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "persona": {},
    })
    assert any("alias name is required" in e for e in errors), errors


def test_validate_configure_readonly_scopes_tolerate_empty_persona():
    """Calendar / Drive / Gmail-read-only configs don't need an alias.

    Counterpart to the outbound-scope rule: it must not be a blanket
    "every wizard run needs a persona" check — that would block
    legitimate internal-only bots.
    """
    errors, cleaned = _validate_configure_payload({
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
    })
    assert errors == []
    assert cleaned["persona"] == {}


# ── _apply_bot_config ───────────────────────────────────────────────────────


def test_apply_bot_config_writes_google_integration_block():
    network = {"bots": {"lex": {"display_name": "Lex"}}}
    cleaned = {
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "soft"},
    }
    _apply_bot_config(network, cleaned)
    gi = network["bots"]["lex"]["google_integration"]
    assert gi["mode"] == "service_account_dwd"
    assert gi["subject"] == "lex@example-corp.com"
    assert gi["service_account_secret_ref"] == "google-sa-example-corp"
    assert gi["scopes"] == ["https://www.googleapis.com/auth/gmail.send"]
    # Correspondence persona block written alongside
    assert network["bots"]["lex"]["correspondence"]["name"] == "Jane"


def test_apply_bot_config_omits_correspondence_when_no_persona():
    network = {"bots": {"lex": {}}}
    _apply_bot_config(network, {
        "bot_id": "lex",
        "secret_ref": "x",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["s"],
        "persona": {},
    })
    assert "correspondence" not in network["bots"]["lex"]


# ── POST /configure-bot ─────────────────────────────────────────────────────


def test_configure_bot_happy(client, network_path: Path, secrets_dir: Path, fake_sudo):
    # SA must already be installed
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))

    # Mock the pre-flight call so we don't actually try to reach Google
    with patch.object(wgr, "_run_preflight", return_value={
        "ok": True,
        "profile": {"emailAddress": "lex@example-corp.com", "messagesTotal": 42, "threadsTotal": 8},
    }):
        r = client.post("/api/wizard/google/configure-bot", json={
            "bot_id": "lex",
            "secret_ref": "google-sa-example-corp",
            "workspace_domain": "example-corp.com",
            "subject": "lex@example-corp.com",
            "scopes": ["https://www.googleapis.com/auth/gmail.send"],
            "persona": {"name": "Jane", "disclosure": "soft"},
        })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["preflight"]["ok"] is True
    assert body["preflight"]["profile"]["emailAddress"] == "lex@example-corp.com"
    assert "share_prompt" in body
    assert "Jane" in body["share_prompt"] or "lex@example-corp.com" in body["share_prompt"]

    # Network.json must have been mutated on disk
    net = json.loads(network_path.read_text())
    gi = net["bots"]["lex"]["google_integration"]
    assert gi["mode"] == "service_account_dwd"
    assert gi["subject"] == "lex@example-corp.com"


def test_configure_bot_rejects_unknown_bot(client, secrets_dir: Path, fake_sudo):
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))
    r = client.post("/api/wizard/google/configure-bot", json={
        "bot_id": "nonexistent",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "x@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "soft"},
    })
    assert r.status_code == 400
    body = r.get_json()
    assert any("not found in network.json" in e for e in body["errors"])


def test_configure_bot_rejects_missing_sa(client, fake_sudo):
    # No SA file installed → must be rejected
    r = client.post("/api/wizard/google/configure-bot", json={
        "bot_id": "lex",
        "secret_ref": "google-sa-missing",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "persona": {"name": "Jane", "disclosure": "soft"},
    })
    assert r.status_code == 400
    assert any("not installed" in e for e in r.get_json()["errors"])


def test_configure_bot_validation_failures_surface(client, fake_sudo, secrets_dir: Path):
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))
    r = client.post("/api/wizard/google/configure-bot", json={
        "bot_id": "",
        "secret_ref": "../bad",
        "workspace_domain": "no-tld",
        "subject": "not-an-email",
        "scopes": "not-a-list",
    })
    assert r.status_code == 400
    body = r.get_json()
    msgs = "\n".join(body["errors"])
    assert "bot_id is required" in msgs
    assert "secret_ref" in msgs


def test_configure_bot_rejects_empty_persona_with_outbound_scope(
    client, network_path: Path, secrets_dir: Path, fake_sudo,
):
    """End-to-end: the 2026-06-02 regression must surface as a 400 at the endpoint.

    Before this fix, the wizard would silently accept the empty persona,
    write `google_integration` with no `correspondence` block, and
    return 200 with a green preflight — leaving the bot configured to
    receive mail but unable to send. We now refuse the write entirely.
    """
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))
    pre_state = json.loads(network_path.read_text())
    r = client.post("/api/wizard/google/configure-bot", json={
        "bot_id": "lex",
        "secret_ref": "google-sa-example-corp",
        "workspace_domain": "example-corp.com",
        "subject": "lex@example-corp.com",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        # persona deliberately omitted — the regression case
    })
    assert r.status_code == 400
    body = r.get_json()
    assert any("alias name is required" in e for e in body["errors"]), body
    # And — load-bearing — network.json was NOT mutated. The wizard
    # used to write the half-configured google_integration block before
    # the operator could see the mistake.
    post_state = json.loads(network_path.read_text())
    assert post_state == pre_state


def test_configure_bot_preflight_failure_still_saves(client, network_path: Path, secrets_dir: Path, fake_sudo):
    """Pre-flight failure must NOT roll back the config write.

    Spec §8 / §7.2 — the bot's config stays in place so the operator
    can fix the upstream issue (DwD authorization, key rotation) and
    re-verify; the wizard surfaces the error rather than discarding.
    """
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))
    with patch.object(wgr, "_run_preflight", return_value={
        "ok": False,
        "error": "pre-flight call failed: HttpError 403 unauthorized_client — the SA's DwD client ID isn't authorized…",
    }):
        r = client.post("/api/wizard/google/configure-bot", json={
            "bot_id": "lex",
            "secret_ref": "google-sa-example-corp",
            "workspace_domain": "example-corp.com",
            "subject": "lex@example-corp.com",
            "scopes": ["https://www.googleapis.com/auth/gmail.send"],
            "persona": {"name": "Jane", "disclosure": "soft"},
        })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True  # The write succeeded
    assert body["preflight"]["ok"] is False
    assert "unauthorized_client" in body["preflight"]["error"]
    # And the bot's google_integration block is still on disk
    net = json.loads(network_path.read_text())
    assert net["bots"]["lex"]["google_integration"]["subject"] == "lex@example-corp.com"


# ── GET /share-prompt ───────────────────────────────────────────────────────


def test_share_prompt_after_configure(client, network_path: Path, secrets_dir: Path, fake_sudo):
    (secrets_dir / "google-sa-example-corp.json").write_text(json.dumps(_sa_json()))
    with patch.object(wgr, "_run_preflight", return_value={"ok": True, "profile": {}}):
        client.post("/api/wizard/google/configure-bot", json={
            "bot_id": "lex",
            "secret_ref": "google-sa-example-corp",
            "workspace_domain": "example-corp.com",
            "subject": "lex@example-corp.com",
            "scopes": ["https://www.googleapis.com/auth/gmail.send"],
            "persona": {"name": "Jane", "disclosure": "soft"},
        })
    r = client.get("/api/wizard/google/share-prompt?bot_id=lex")
    assert r.status_code == 200
    body = r.get_json()
    assert "lex@example-corp.com" in body["share_prompt"]
    assert "Jane" in body["share_prompt"]
    assert "Sam" in body["share_prompt"]  # primary_user_name from network.json


def test_share_prompt_rejects_unconfigured(client):
    r = client.get("/api/wizard/google/share-prompt?bot_id=lex")
    assert r.status_code == 400
    assert "no google_integration.subject" in r.get_json()["error"]


# ── build_share_prompt unit ─────────────────────────────────────────────────


def test_build_share_prompt_mentions_calendar_and_drive():
    msg = build_share_prompt(
        bot_display_name="Jane",
        bot_subject="lex@example-corp.com",
        primary_user_name="Sam",
    )
    assert "Calendar" in msg
    assert "Drive" in msg
    assert "lex@example-corp.com" in msg
    assert "Sam" in msg
    assert "Jane" in msg


def test_build_share_prompt_handles_blank_user_name():
    msg = build_share_prompt(
        bot_display_name="Jane",
        bot_subject="lex@example-corp.com",
        primary_user_name="",
    )
    # Falls back to "you" so the message still reads naturally
    assert "you" in msg


# ── Secrets-install path: no-sudo + atomic-rename + mode-0600 ────────────────
#
# Pre-2026-06-01 these helpers shelled out to `sudo /bin/cp` + `sudo
# /usr/sbin/chown` + `sudo /bin/chmod` against the SA JSON path tree.
# Three problems:
#   FM-1: no NOPASSWD grant for /Users/Shared/evolve/secrets/ in
#         setup_wizard.py → wizard upload-sa 500s with
#         "sudo: a password is required"
#   FM-3: between `sudo cp` (mode 0o644 default umask) and
#         `sudo chmod 0600`, the SA JSON was briefly readable by
#         any user on the box
#   FM-12: chown/chmod were check=False — silent failures left the
#          file with wrong owner; later evolve-user reads hit
#          PermissionError, looked like a fresh bug
# Direct write + atomic rename + explicit O_NOFOLLOW + 0o600 from
# inception fixes all three.


def test_ensure_secrets_dir_creates_directly_without_sudo(
    tmp_path: Path, monkeypatch,
):
    """No sudo subprocess should be invoked on the happy path — /Users/Shared/
    evolve/ is evolve-owned so mkdir works directly."""
    sudo_calls = []

    def fake_run(cmd, **kwargs):
        sudo_calls.append(list(cmd))
        # Match the real subprocess.run return shape.
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(wgr.subprocess, "run", fake_run)

    target = tmp_path / "fresh-secrets" / "google_service_accounts"
    ok, err = wgr._ensure_secrets_dir(target)
    assert ok, err
    assert target.exists()
    assert sudo_calls == [], (
        f"_ensure_secrets_dir invoked sudo on the happy path. "
        f"Calls: {sudo_calls}. The /Users/Shared/evolve/ tree is "
        f"evolve-owned; sudo shouldn't be needed."
    )


def test_install_sa_file_writes_with_mode_0600_from_inception(
    tmp_path: Path,
):
    """The SA JSON must NEVER exist on disk with mode broader than 0600.
    The previous code path went through `sudo cp` (default umask 0o644)
    and a follow-up `chmod 0600` — leaving a brief world-readable window
    on a multi-user box."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    sa = {
        "type": "service_account",
        "project_id": "evolve-test",
        "private_key_id": "abc",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "evolve@evolve-test.iam.gserviceaccount.com",
        "client_id": "123456789",
    }
    ok, err = wgr._install_sa_file(secrets_dir, "google-sa-test", sa)
    assert ok, err

    dest = secrets_dir / "google-sa-test.json"
    assert dest.exists()
    mode = dest.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"SA JSON has mode {oct(mode)} — expected 0o600. The wider mode "
        f"creates a brief window during which other local users could "
        f"read the SA JSON (a Google-mailbox-impersonating credential)."
    )
    # And the temp .json.tmp file must NOT remain (atomic-rename cleanup).
    assert not (secrets_dir / "google-sa-test.json.tmp").exists()


def test_install_sa_file_invokes_no_sudo(
    tmp_path: Path, monkeypatch,
):
    """The direct write path must not invoke any sudo subprocess. The old
    code's `sudo cp + chown + chmod` sequence hit the wizard-pattern
    sudo-grant-missing footgun."""
    sudo_calls = []

    def fake_run(cmd, **kwargs):
        sudo_calls.append(list(cmd))
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(wgr.subprocess, "run", fake_run)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    sa = {
        "type": "service_account",
        "project_id": "evolve-test",
        "private_key_id": "abc",
        "private_key": "k",
        "client_email": "a@b.iam.gserviceaccount.com",
        "client_id": "1",
    }
    ok, _err = wgr._install_sa_file(secrets_dir, "ref", sa)
    assert ok
    assert sudo_calls == [], (
        f"_install_sa_file invoked sudo. Calls: {sudo_calls}. The SA "
        f"JSON dest dir is evolve-owned so direct write works without sudo."
    )


def test_install_sa_file_atomic_no_partial_file_on_write_failure(
    tmp_path: Path, monkeypatch,
):
    """If the write fails mid-way, no .tmp file should be left behind to
    confuse the next attempt."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    # Force json.dump to fail by passing something not JSON-serializable.
    # The fdopen + json.dump path inside _install_sa_file will raise;
    # the except branch should unlink the .tmp.
    sa = {"unserializable": object()}
    ok, err = wgr._install_sa_file(secrets_dir, "ref", sa)
    assert not ok
    assert "install failed" in err
    assert not (secrets_dir / "ref.json").exists()
    assert not (secrets_dir / "ref.json.tmp").exists(), (
        "Partial .tmp file leaked; next install attempt may collide."
    )


# ── Preflight: scope-matched probe + operator-actionable hints ──────────────
#
# Pre-2026-06-01 the preflight (a) hardcoded gmail.readonly regardless
# of what the operator chose, and (b) had only three operator-visible
# error hints. Both produced confusing failures:
#   FM-13: bot configured with only `gmail.send` got false-negative
#          preflight failure with a misleading "DwD client_id" hint
#   FM-4:  several common error modes (API-not-enabled, personal-Gmail
#          subject, Group instead of User, alias subject) all collapsed
#          into the generic unauthorized_client bucket with the wrong fix


def test_pick_preflight_probe_prefers_readable_scope_from_bot_config():
    """The preflight scope must come from the bot's configured scopes,
    not a hardcoded default."""
    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar",
    ]
    result = wgr._pick_preflight_probe(scopes)
    assert result is not None
    probe_scopes, api, _version, _fn = result
    # gmail.readonly is the cheapest probe and the operator has it
    assert "gmail.readonly" in probe_scopes[0]
    assert api == "gmail"


def test_pick_preflight_probe_falls_back_to_calendar_when_no_gmail_read():
    """Bot has only gmail.send (write-only) but a calendar read scope —
    preflight must probe calendar, not gmail."""
    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]
    result = wgr._pick_preflight_probe(scopes)
    assert result is not None
    probe_scopes, api, _, _ = result
    assert api == "calendar"
    assert "calendar.readonly" in probe_scopes[0]


def test_pick_preflight_probe_returns_none_for_write_only_scopes():
    """Pure write-only scopes (gmail.send alone) → skip preflight rather
    than false-negative on a write-only API. The skip is honest — the
    first real tool call will reveal config issues."""
    scopes = ["https://www.googleapis.com/auth/gmail.send"]
    result = wgr._pick_preflight_probe(scopes)
    assert result is None


def test_preflight_error_hint_catches_personal_gmail_subject():
    """The subject @gmail.com with an unauthorized_client error should
    explain DwD doesn't apply to personal accounts — NOT tell the
    operator to re-check the DwD client_id."""
    hint = wgr._preflight_error_hint(
        "unauthorized_client: Client is unauthorized to retrieve access tokens",
        subject="someone@gmail.com",
        workspace_domain="gmail.com",
    )
    assert "personal" in hint.lower() or "@gmail.com" in hint
    assert "DwD doesn't apply" in hint or "doesn't apply" in hint.lower()


def test_preflight_error_hint_catches_api_not_enabled():
    """API-not-enabled returns the wrong fix under unauthorized_client; the
    'has not been used in project' substring must take priority."""
    msg = (
        "Gmail API has not been used in project 12345 before or it is "
        "disabled. Enable it by visiting https://console.developers.google.com/..."
    )
    hint = wgr._preflight_error_hint(
        msg, subject="lex@corp.com", workspace_domain="corp.com",
    )
    assert "API" in hint and "enable" in hint.lower()


def test_preflight_error_hint_catches_group_subject():
    """Impersonating a Workspace Group (not a User) gives a specific error;
    DwD is User-only."""
    hint = wgr._preflight_error_hint(
        "Domain policy exempts user from delegation",
        subject="team@corp.com",
        workspace_domain="corp.com",
    )
    assert "Group" in hint or "individual User" in hint


def test_preflight_error_hint_catches_alias_subject():
    """userNotFound means either propagation delay or operator typed an
    alias rather than the primary address. Hint must mention both."""
    hint = wgr._preflight_error_hint(
        "userNotFound: User not found",
        subject="alias@corp.com",
        workspace_domain="corp.com",
    )
    assert "alias" in hint.lower() or "primary" in hint.lower()
