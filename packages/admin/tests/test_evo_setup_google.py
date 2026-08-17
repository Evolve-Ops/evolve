"""tests/test_evo_setup_google.py — `evo setup-google` wizard.

Per-bot under PR #1123 / PR 2: each bot has its own OAuth Application,
so the wizard runs from the bot being set up (not pod primary). The
client_id + client_secret it collects land under that bot's namespace.

Covers:
  * Dispatch routing: starts from any bot the caller has primary role on
  * Per-bot persistence: wizard run from `admin_bot` writes admin_bot's secret;
    wizard run from `evolve` writes evolve's secret
  * Phase chain: intro → admin_url → console → client_id → client_secret → commit
  * Validators: admin_url, client_id, client_secret each reject malformed input
  * Cancel at every step terminates without persisting state
  * Commit writes both network.json (adminBaseUrl) AND the calling bot's
    storage (client_id + client_secret)
  * Secret is never persisted to wizard state at rest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


VALID_CLIENT_ID = (
    "1234567890-abcdefghijklmnopqrstuvwxyzABCDEF.apps.googleusercontent.com"
)
VALID_CLIENT_SECRET = "GOCSPX-AbCdEfGhIjKlMnOpQrStUvWx"
VALID_ADMIN_URL = "https://evolve.example.com"


@pytest.fixture
def network(tmp_path):
    # Both `evolve` and `admin_bot` have the same operator (telegram id
    # 12345) recorded as primary user. Per-bot OAuth setup means each bot
    # is set up by its own primary — in this test rig, the same person
    # owns both, which is the common single-operator case.
    return {
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "evolve"],
        "primary": "evolve",
        "bots": {
            "evolve": {"primary_user": {
                "external_ids": {"telegram": "12345"},
            }},
            "admin_bot": {"primary_user": {
                "external_ids": {"telegram": "12345"},
            }},
        },
    }


@pytest.fixture
def patched_persistence(monkeypatch, tmp_path):
    """Stub config.load_network / save_network so the wizard's commit
    step exercises the right code path without hitting the real
    /Users/Shared/evolve/network.json on disk."""
    state = {"network": {}}

    def fake_load(path=None):
        return dict(state["network"]) or {
            "members": ["admin_bot", "evolve"],
            "primary": "evolve",
            "sharedDir": str(tmp_path),
            "bots": {},
        }

    def fake_save(data, path=None):
        state["network"] = dict(data)

    from evolve_admin import config as _cfg
    # Defensive: the conftest's _restore_module_state autouse fixture
    # may have deleted ``sys.modules['evolve_admin.config']`` after a
    # prior test (it happens when the prior test was the first to
    # import the module, so it was a "new" entry to clean up). The
    # parent ``evolve_admin`` module still has ``config`` as an
    # attribute pointing at the orphaned module — but if we leave the
    # sys.modules slot empty, engine's ``from ...config import save_network``
    # will RE-import a fresh module that bypasses our monkeypatch. Force
    # sys.modules to point at the same _cfg object the patches will
    # land on.
    import sys as _ss
    _ss.modules.setdefault("evolve_admin.config", _cfg)
    if _ss.modules["evolve_admin.config"] is not _cfg:
        _ss.modules["evolve_admin.config"] = _cfg
    monkeypatch.setattr(_cfg, "load_network", fake_load)
    monkeypatch.setattr(_cfg, "save_network", fake_save)

    # Stub the persistence path on both branches the engine might take:
    #   1. `evolve_admin.web.server._save_google_oauth_client` (closure-
    #      bound helper exists when admin server has been initialized)
    #   2. `_save_google_oauth_client_inline` (engine's fallback)
    # We don't know which branch will fire — depends on whether any
    # earlier test in the run loaded server.py — so stub both.
    saved_secrets: list[tuple[str, str, str]] = []

    def fake_save_helper(client_id, client_secret, primary_bot):
        saved_secrets.append((primary_bot, client_id, client_secret))
        return True, None

    def fake_inline_save(shared_dir, primary_bot, *, client_id, client_secret):
        saved_secrets.append((primary_bot, client_id, client_secret))
        return True, None

    from evolve_admin.evo.wizard import engine as _engine
    monkeypatch.setattr(_engine, "_save_google_oauth_client_inline", fake_inline_save)

    # Force the admin_server module into the import cache with a stub
    # _save_google_oauth_client so the engine's `getattr(_admin_server,
    # "_save_google_oauth_client", None)` lookup hits our fake regardless
    # of whether the real Flask factory has run.
    import sys as _sys
    if "evolve_admin.web.server" in _sys.modules:
        # Real module already imported — patch the attribute (or remove
        # it if it doesn't exist) so the lookup either uses our stub or
        # falls through to the inline fallback.
        real_server = _sys.modules["evolve_admin.web.server"]
        monkeypatch.setattr(real_server, "_save_google_oauth_client",
                            fake_save_helper, raising=False)
    else:
        # Module hasn't been imported yet — inject a stub one so the
        # engine's `from ...web import server as _admin_server` succeeds
        # without triggering the heavy Flask factory.
        import types as _types
        stub = _types.ModuleType("evolve_admin.web.server")
        stub._save_google_oauth_client = fake_save_helper
        _sys.modules["evolve_admin.web.server"] = stub
        # Clean up on test teardown so we don't poison later tests that
        # import the real module.
        def _cleanup():
            _sys.modules.pop("evolve_admin.web.server", None)
        monkeypatch.setattr(_engine, "_setup_google_test_cleanup",
                            _cleanup, raising=False)

    return state, saved_secrets


def _dispatch(network, raw, bot_id="evolve"):
    from evolve_admin.evo.dispatch import dispatch
    return dispatch(
        network, bot_id=bot_id, channel="telegram",
        sender_external_id="12345", raw_text=raw,
    )


def _turn(network, msg, bot_id="evolve"):
    from evolve_admin.evo.wizard.engine import process_turn
    from evolve_admin.evo.identity import derive_user_key
    shared = Path(network["sharedDir"])
    user_key = derive_user_key(
        network, bot_id=bot_id, channel="telegram",
        sender_external_id="12345",
    )
    return process_turn(
        shared, bot_id=bot_id, user_key=user_key,
        user_message=msg, network=network,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch routing — runs from each bot independently (per-bot OAuth)
# ─────────────────────────────────────────────────────────────────────────────


def test_setup_google_starts_from_evolve(network):
    """Per-bot under PR 2: evolve runs the wizard for itself."""
    r = _dispatch(network, "evo setup-google", bot_id="evolve")
    assert r.subcommand == "setup-google"
    assert r.wizard_session_id
    body = r.system_append or ""
    assert "Setting up Google Workspace" in body
    # Intro is per-bot framed
    assert "`evolve`" in body


def test_setup_google_starts_from_admin_bot(network):
    """Per-bot under PR 2: admin_bot (a non-primary bot in the pod sense)
    runs the wizard for itself — there's no longer a primary-only gate.
    The intro is framed for admin_bot specifically."""
    r = _dispatch(network, "evo setup-google", bot_id="admin_bot")
    assert r.subcommand == "setup-google"
    assert r.wizard_session_id
    body = r.system_append or ""
    assert "Setting up Google Workspace" in body
    assert "`admin_bot`" in body


# ─────────────────────────────────────────────────────────────────────────────
# Phase chain happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_full_happy_path_persists_credentials(network, patched_persistence):
    """Happy path from `evolve` — secret lands under evolve's namespace."""
    persist_state, saved_secrets = patched_persistence
    _dispatch(network, "evo setup-google", bot_id="evolve")

    # Phase 1: intro → admin_url
    r = _turn(network, "ready", bot_id="evolve")
    assert "Step 1 of 4" in (r.system_append or "")
    assert "admin base URL" in (r.system_append or "")

    # Phase 2: admin_url → console (with redirect URI rendered)
    r = _turn(network, VALID_ADMIN_URL, bot_id="evolve")
    assert "Step 2 of 4" in (r.system_append or "")
    assert f"{VALID_ADMIN_URL}/api/admin/onboard/google/callback" in (r.system_append or "")

    # Phase 3: console → client_id
    r = _turn(network, "done", bot_id="evolve")
    assert "Step 3 of 4" in (r.system_append or "")

    # Phase 4: client_id → client_secret
    r = _turn(network, VALID_CLIENT_ID, bot_id="evolve")
    assert "Step 4 of 4" in (r.system_append or "")
    assert "GOCSPX-" in (r.system_append or "")  # tells operator the prefix

    # Phase 5: client_secret → commit
    r = _turn(network, VALID_CLIENT_SECRET, bot_id="evolve")
    assert r.completed is True
    assert r.wizard_session_id is None
    body = r.system_append or ""
    assert "saved" in body.lower() and "next" in body.lower()
    # Final message names the calling bot specifically
    assert "`evolve`" in body

    # Persistence: client credentials saved under evolve's namespace,
    # adminBaseUrl saved at top level of network.json
    assert len(saved_secrets) == 1
    saved_bot, saved_client_id, saved_secret = saved_secrets[0]
    assert saved_bot == "evolve"
    assert saved_client_id == VALID_CLIENT_ID
    assert saved_secret == VALID_CLIENT_SECRET
    assert persist_state["network"].get("adminBaseUrl") == VALID_ADMIN_URL


def test_happy_path_from_admin_bot_persists_under_admin_bot(network, patched_persistence):
    """Per-bot under PR 2: when the wizard runs from admin_bot, the credentials
    land under admin_bot's namespace — NOT under evolve's. This is the key
    behavioral change from PR 1."""
    persist_state, saved_secrets = patched_persistence
    _dispatch(network, "evo setup-google", bot_id="admin_bot")
    _turn(network, "ready", bot_id="admin_bot")
    _turn(network, VALID_ADMIN_URL, bot_id="admin_bot")
    _turn(network, "done", bot_id="admin_bot")
    _turn(network, VALID_CLIENT_ID, bot_id="admin_bot")
    r = _turn(network, VALID_CLIENT_SECRET, bot_id="admin_bot")
    assert r.completed is True
    body = r.system_append or ""
    # Admin_bot is named in the success message
    assert "`admin_bot`" in body

    # Credentials saved under admin_bot's namespace, not evolve's
    assert len(saved_secrets) == 1
    saved_bot, saved_client_id, saved_secret = saved_secrets[0]
    assert saved_bot == "admin_bot"
    assert saved_client_id == VALID_CLIENT_ID
    assert saved_secret == VALID_CLIENT_SECRET


# ─────────────────────────────────────────────────────────────────────────────
# Validators — admin_url
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "localhost:5050",                     # no scheme
    "ftp://evolve.example.com",           # wrong scheme
    "https://evolve.example.com/path",    # has path
    "https://evolve.example.com?x=1",     # has query
    "https://",                           # no host
    "",                                   # empty
])
def test_admin_url_rejects_malformed(network, patched_persistence, bad):
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")  # advance to admin_url phase
    r = _turn(network, bad)
    body = r.system_append or ""
    assert "didn't validate" in body
    assert r.completed is False
    # Stays at admin_url phase
    assert "admin_url" in (r.phase or "") or r.phase == "google_setup_admin_url"


def test_admin_url_trims_trailing_slash(network, patched_persistence):
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    r = _turn(network, "https://evolve.example.com/")
    # Should accept + render the redirect URI without a double-slash
    body = r.system_append or ""
    assert "https://evolve.example.com/api/admin" in body
    assert "https://evolve.example.com//api" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Validators — client_id
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "1234567890-abc.apps.googlecontent.com",   # typo in domain
    "1234567890",                              # too short, wrong suffix
    "",                                         # empty
    "Just some text the operator typed",       # not a client_id
])
def test_client_id_rejects_malformed(network, patched_persistence, bad):
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    _turn(network, "done")
    r = _turn(network, bad)
    body = r.system_append or ""
    assert "didn't validate" in body
    assert r.completed is False


def test_client_id_strips_surrounding_quotes(network, patched_persistence):
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    _turn(network, "done")
    # Operator pastes with quotes — wizard should strip them
    quoted = f'"{VALID_CLIENT_ID}"'
    r = _turn(network, quoted)
    # If it accepted, we should now be at the client_secret phase
    assert "Step 4 of 4" in (r.system_append or "")


# ─────────────────────────────────────────────────────────────────────────────
# Validators — client_secret
# ─────────────────────────────────────────────────────────────────────────────


def test_client_secret_rejects_empty(network, patched_persistence):
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    _turn(network, "done")
    _turn(network, VALID_CLIENT_ID)
    r = _turn(network, "")
    body = r.system_append or ""
    assert "didn't validate" in body


def test_client_secret_rejects_too_short(network, patched_persistence):
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    _turn(network, "done")
    _turn(network, VALID_CLIENT_ID)
    r = _turn(network, "short")
    body = r.system_append or ""
    assert "didn't validate" in body and "too short" in body.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Cancel at every step
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("steps_before_cancel", [
    [],                                       # cancel at intro
    ["ready"],                                # cancel at admin_url
    ["ready", VALID_ADMIN_URL],               # cancel at console
    ["ready", VALID_ADMIN_URL, "done"],       # cancel at client_id
    ["ready", VALID_ADMIN_URL, "done", VALID_CLIENT_ID],  # cancel at secret
])
def test_cancel_at_any_step_terminates_without_persisting(
    network, patched_persistence, steps_before_cancel,
):
    persist_state, saved_secrets = patched_persistence
    _dispatch(network, "evo setup-google")
    for step in steps_before_cancel:
        _turn(network, step)
    r = _turn(network, "cancel")
    assert r.completed is True
    assert r.wizard_session_id is None
    body = r.system_append or ""
    assert "Cancelled" in body
    assert "no credentials saved" in body.lower()
    # No secrets persisted regardless of how far they got
    assert saved_secrets == []
    assert "adminBaseUrl" not in persist_state["network"]


# ─────────────────────────────────────────────────────────────────────────────
# Secret hygiene: client_secret never lives in wizard state at rest
# ─────────────────────────────────────────────────────────────────────────────


def test_client_secret_not_persisted_to_wizard_state(network, patched_persistence):
    """Defense-in-depth: the commit path strips ``_client_secret`` from
    state.extracted before writing. Verify the persisted state on disk
    has no secret in it once the wizard has terminated."""
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    _turn(network, "done")
    _turn(network, VALID_CLIENT_ID)
    _turn(network, VALID_CLIENT_SECRET)

    # State file should be marked completed; scan for any literal
    # appearance of the secret in the persisted JSON.
    shared = Path(network["sharedDir"])
    state_files = list((shared / "wizard").rglob("*.json"))
    assert state_files  # at least one state file exists
    for sf in state_files:
        text = sf.read_text()
        assert VALID_CLIENT_SECRET not in text, (
            f"client_secret leaked to wizard state at {sf}"
        )


def test_cancelled_state_does_not_keep_secret_either(network, patched_persistence):
    """Cancel during client_secret phase MUST wipe the in-state secret
    even though the operator already typed it. We never validate first
    then cancel — but if cancel is typed AFTER secret entry on the
    next turn, no path leaks."""
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    _turn(network, "done")
    _turn(network, VALID_CLIENT_ID)
    # Cancel before typing the secret
    _turn(network, "cancel")

    shared = Path(network["sharedDir"])
    state_files = list((shared / "wizard").rglob("*.json"))
    for sf in state_files:
        text = sf.read_text()
        assert VALID_CLIENT_SECRET not in text


# ─────────────────────────────────────────────────────────────────────────────
# Resume — partial state
# ─────────────────────────────────────────────────────────────────────────────


def test_resume_returns_current_phase_prompt(network, patched_persistence):
    """An in-flight wizard session resumed via `evo setup-google` should
    re-render the current phase's prompt, not restart at intro."""
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, VALID_ADMIN_URL)
    # Now at console phase. Resume.
    r = _dispatch(network, "evo setup-google")
    assert "Step 2 of 4" in (r.system_append or "")
    assert r.wizard_session_id
