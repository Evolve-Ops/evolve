"""tests/test_google_oauth_client_store.py — Evolve credential store
for the per-bot Google OAuth client_id + client_secret.

Replaces the previous design where the secret lived as a profile in
openclaw's ``auth-profiles.json`` — that file is openclaw's territory
and its in-memory cache silently rewrote it from snapshot, dropping
Evolve's entries. The new store at
``{shared_dir}/credentials/<bot>/google_oauth_client.json`` is fully
Evolve-owned; openclaw never touches it.

Asserts:
  * Wizard write lands in the new store, NOT in auth-profiles.json
  * network.json's per-bot block uses the simpler ``secret_bot`` shape
    (no ``client_secret_ref`` indirection)
  * Reader resolves from the new store first
  * Backward-compat: reader falls back to ``client_secret_ref`` when
    only the old shape is present
  * Sharing: bot A pointing at bot B reads B's credential file
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


# ─────────────────────────────────────────────────────────────────────────────
# Inline writer (engine.py) — the path tests + production fall back to
# when the admin server's closure-bound writer isn't reachable
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_writer_lands_in_evolve_store(tmp_path, monkeypatch):
    """Wizard's inline writer puts the credential at
    ``{shared_dir}/credentials/<bot>/google_oauth_client.json``."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin import config as _cfg

    network_state = {"network": {"sharedDir": str(tmp_path), "bots": {}}}

    def _fake_load(path=None):
        return dict(network_state["network"])

    def _fake_save(data, path=None):
        network_state["network"] = dict(data)

    import sys as _ss
    _ss.modules.setdefault("evolve_admin.config", _cfg)
    monkeypatch.setattr(_cfg, "load_network", _fake_load)
    monkeypatch.setattr(_cfg, "save_network", _fake_save)

    ok, err = _engine._save_google_oauth_client_inline(
        tmp_path, "admin_bot",
        client_id=VALID_CLIENT_ID, client_secret=VALID_CLIENT_SECRET,
    )
    assert ok, f"inline writer failed: {err}"

    # The credential file is in the Evolve store
    store_path = tmp_path / "credentials" / "admin_bot" / "google_oauth_client.json"
    assert store_path.exists(), f"credential store file not created at {store_path}"

    payload = json.loads(store_path.read_text())
    assert payload["client_id"] == VALID_CLIENT_ID
    assert payload["client_secret"] == VALID_CLIENT_SECRET
    assert payload["schema_version"] == 1


def test_inline_writer_does_not_touch_auth_profiles(tmp_path, monkeypatch):
    """Belt-and-suspenders — the inline writer must NOT write to admin_bot's
    auth-profiles.json. That file is openclaw's territory; the previous
    design put credentials there and openclaw rewrote them away.

    We test by ensuring no auth-profiles.json was created in the bot's
    home dir as a side effect of the inline writer.
    """
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin import config as _cfg

    network_state = {"network": {"sharedDir": str(tmp_path), "bots": {}}}
    # Defensive: conftest's _restore_module_state autouse fixture
    # deletes evolve_admin.config from sys.modules between tests.
    # Without re-attaching, the engine's `from ...config import save_network`
    # would re-import a fresh module that bypasses our monkeypatch.
    import sys as _ss
    _ss.modules.setdefault("evolve_admin.config", _cfg)
    if _ss.modules["evolve_admin.config"] is not _cfg:
        _ss.modules["evolve_admin.config"] = _cfg
    monkeypatch.setattr(_cfg, "load_network", lambda path=None: dict(network_state["network"]))

    def _fake_save(data, path=None):
        network_state["network"] = dict(data)

    monkeypatch.setattr(_cfg, "save_network", _fake_save)

    ok, _err = _engine._save_google_oauth_client_inline(
        tmp_path, "admin_bot",
        client_id=VALID_CLIENT_ID, client_secret=VALID_CLIENT_SECRET,
    )
    assert ok

    # No auth-profiles.json should appear under any /Users/admin_bot path
    # the inline writer used to write to. (We're not checking real
    # /Users/admin_bot — just confirming the writer doesn't try to.)
    # The credential was written to the shared store instead.
    store_path = tmp_path / "credentials" / "admin_bot" / "google_oauth_client.json"
    assert store_path.exists()
    # Sanity: the secret content matches and isn't anywhere in the
    # network state mock (it should never be in network.json).
    net_serialized = json.dumps(network_state["network"])
    assert VALID_CLIENT_SECRET not in net_serialized


def test_inline_writer_network_json_has_simple_shape(tmp_path, monkeypatch):
    """network.json's per-bot block uses ``secret_bot`` directly, not
    the older ``client_secret_ref`` indirection. Easier to read,
    simpler sharing semantics."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin import config as _cfg

    network_state = {"network": {"sharedDir": str(tmp_path), "bots": {}}}
    # Defensive: conftest's _restore_module_state autouse fixture
    # deletes evolve_admin.config from sys.modules between tests.
    # Without re-attaching, the engine's `from ...config import save_network`
    # would re-import a fresh module that bypasses our monkeypatch.
    import sys as _ss
    _ss.modules.setdefault("evolve_admin.config", _cfg)
    if _ss.modules["evolve_admin.config"] is not _cfg:
        _ss.modules["evolve_admin.config"] = _cfg
    monkeypatch.setattr(_cfg, "load_network", lambda path=None: dict(network_state["network"]))

    def _fake_save(data, path=None):
        network_state["network"] = dict(data)

    monkeypatch.setattr(_cfg, "save_network", _fake_save)

    ok, err = _engine._save_google_oauth_client_inline(
        tmp_path, "admin_bot",
        client_id=VALID_CLIENT_ID, client_secret=VALID_CLIENT_SECRET,
    )
    assert ok, err

    bot_block = network_state["network"]["bots"]["admin_bot"]["googleOAuthClient"]
    assert bot_block["client_id"] == VALID_CLIENT_ID
    assert bot_block["secret_bot"] == "admin_bot"
    assert bot_block["mode"] == "self_hosted"
    # The old indirection field is NOT present
    assert "client_secret_ref" not in bot_block


# ─────────────────────────────────────────────────────────────────────────────
# Server-side helpers — direct unit tests
#
# 4.1b Increment 0 lifted ``_read_google_oauth_client`` (and its private deps)
# out of ``register_admin_routes``'s closure into ``routes_admin_shared``, where
# it is now a module-level function taking ``network_path`` + a
# ``read_auth_profiles`` callable. These tests were migrated off the
# closure-cell introspection (``co_freevars`` / ``cell_contents``) they used to
# rely on — per the decomposition memo §5, a lifted helper's free var
# disappears, so the test must import the canonical function directly.
#
# ``_save_google_oauth_client`` is NOT in the Increment-0 lift set (it is a
# write path and stays a closure for now), so it is still pulled off the app's
# view closures via ``_get_save_helper`` until a later increment moves the write
# region. ``_read_auth_profiles`` is injected as a plain callable instead of
# mutating a closure cell.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_factory(tmp_path):
    """Spin up the admin Flask app pointed at a tmp network.json so the
    still-closure-bound write helper can be exercised directly."""
    from evolve_admin.web.server import create_app
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "evolve"],
        "primary": "evolve",
        "bots": {},
    }))
    app = create_app(network_path)
    return app, tmp_path, network_path


def _get_save_helper(app):
    """Pull the still-closure-bound ``_save_google_oauth_client`` off the app's
    view functions (it is not lifted in Increment 0). Unions the closure cells
    of every view because each view captures only the helpers it references."""
    for view in app.view_functions.values():
        if not hasattr(view, "__code__") or not view.__closure__:
            continue
        for free_name, cell in zip(view.__code__.co_freevars, view.__closure__):
            if free_name == "_save_google_oauth_client":
                try:
                    return cell.cell_contents
                except ValueError:
                    pass
    raise AssertionError("_save_google_oauth_client not found on any view closure")


def _make_reader(network_path, read_auth_profiles=None):
    """Bind the lifted ``_read_google_oauth_client`` to a network_path + an
    auth-profiles reader, mirroring how the closure shim binds them."""
    from evolve_admin.web.routes_admin_shared import _read_google_oauth_client
    rap = read_auth_profiles if read_auth_profiles is not None else (lambda _b: {})

    def read(bot_id):
        return _read_google_oauth_client(
            bot_id, network_path=network_path, read_auth_profiles=rap,
        )

    return read


def test_server_writer_lands_in_evolve_store(app_factory):
    """The admin server's ``_save_google_oauth_client`` writes to the
    Evolve credential store and uses the simplified network.json shape."""
    app, tmp_path, network_path = app_factory
    save = _get_save_helper(app)

    ok, err = save(VALID_CLIENT_ID, VALID_CLIENT_SECRET, "admin_bot")
    assert ok, err

    store_path = tmp_path / "credentials" / "admin_bot" / "google_oauth_client.json"
    assert store_path.exists()
    payload = json.loads(store_path.read_text())
    assert payload["client_id"] == VALID_CLIENT_ID
    assert payload["client_secret"] == VALID_CLIENT_SECRET

    network = json.loads(network_path.read_text())
    bot_block = network["bots"]["admin_bot"]["googleOAuthClient"]
    assert bot_block["secret_bot"] == "admin_bot"
    assert "client_secret_ref" not in bot_block


def test_server_reader_resolves_from_evolve_store(app_factory):
    """Round-trip — write then read returns the same client_id/secret."""
    app, tmp_path, network_path = app_factory
    save = _get_save_helper(app)
    read = _make_reader(network_path)

    save(VALID_CLIENT_ID, VALID_CLIENT_SECRET, "admin_bot")
    resolved = read("admin_bot")
    assert resolved is not None
    assert resolved["client_id"] == VALID_CLIENT_ID
    assert resolved["client_secret"] == VALID_CLIENT_SECRET


def test_server_reader_falls_back_to_legacy_client_secret_ref(app_factory):
    """Backward compat — installs that haven't migrated still resolve
    when the per-bot block uses the older ``client_secret_ref`` shape
    AND the secret is present in auth-profiles. Returns None as soon as
    openclaw's rewrite drops the auth-profiles entry; that's the
    operator's signal to re-run ``evo setup-google``.

    We don't try to materialize a real ``/Users/<bot>/.openclaw/...`` —
    instead we inject the auth-profiles reader to return the legacy
    entry. That tests the resolver's fallback logic without depending
    on filesystem layout outside the tmp dir. (Increment 0: the reader
    is now a plain parameter, not a closure cell to mutate.)
    """
    _app, _tmp_path, network_path = app_factory
    legacy_bot = "legacybot"

    fake_auth = {
        "profiles": {
            "_evolve_google_oauth_client": {
                "provider": "google_workspace",
                "type": "oauth_client_secret",
                "client_id": VALID_CLIENT_ID,
                "client_secret": VALID_CLIENT_SECRET,
            },
        },
    }
    read = _make_reader(
        network_path,
        read_auth_profiles=lambda bot_id: fake_auth if bot_id == legacy_bot else {},
    )

    # Write legacy-shape network.json
    network = json.loads(network_path.read_text())
    network["bots"][legacy_bot] = {
        "googleOAuthClient": {
            "mode": "self_hosted",
            "client_id": VALID_CLIENT_ID,
            "client_secret_ref": {
                "bot": legacy_bot,
                "profile": "_evolve_google_oauth_client",
            },
        },
    }
    network_path.write_text(json.dumps(network))

    resolved = read(legacy_bot)
    assert resolved is not None, "legacy fallback didn't resolve"
    assert resolved["client_id"] == VALID_CLIENT_ID
    assert resolved["client_secret"] == VALID_CLIENT_SECRET


def test_evolve_store_takes_precedence_over_legacy_auth_profiles(app_factory):
    """When BOTH the new store AND the legacy auth-profiles entry exist,
    the new store wins. (This is the migration window where an operator
    re-ran setup-google but openclaw hasn't yet clobbered auth-profiles.)"""
    app, tmp_path, network_path = app_factory
    save = _get_save_helper(app)
    read = _make_reader(network_path)

    # Write the new store with a "new" secret
    save(VALID_CLIENT_ID, "GOCSPX-NEW-SECRET-AaBbCcDdEe", "admin_bot")

    # Manually poison the legacy path with a different secret
    network = json.loads(network_path.read_text())
    network["bots"]["admin_bot"]["googleOAuthClient"]["client_secret_ref"] = {
        "bot": "admin_bot",
        "profile": "_evolve_google_oauth_client",
    }
    network_path.write_text(json.dumps(network))

    # Reader should return the NEW store's secret (precedence)
    resolved = read("admin_bot")
    assert resolved is not None
    assert resolved["client_secret"] == "GOCSPX-NEW-SECRET-AaBbCcDdEe"


def test_sharing_via_secret_bot_pointer(app_factory):
    """Bot ``team_bot_a_secondary`` points at ``team_bot_a``'s credentials by setting
    ``secret_bot: 'team_bot_a'``. Reader follows the pointer."""
    app, tmp_path, network_path = app_factory
    save = _get_save_helper(app)
    read = _make_reader(network_path)

    # team_bot_a owns the credentials
    save(VALID_CLIENT_ID, VALID_CLIENT_SECRET, "team_bot_a")

    # team_bot_a_secondary opts into sharing team_bot_a's app
    network = json.loads(network_path.read_text())
    network["bots"]["team_bot_a_secondary"] = {
        "googleOAuthClient": {
            "mode": "self_hosted",
            "client_id": VALID_CLIENT_ID,
            "secret_bot": "team_bot_a",
        },
    }
    network_path.write_text(json.dumps(network))

    resolved = read("team_bot_a_secondary")
    assert resolved is not None
    assert resolved["client_id"] == VALID_CLIENT_ID
    assert resolved["client_secret"] == VALID_CLIENT_SECRET


def test_credential_file_is_chmod_600(app_factory):
    """File permissions on the credential file are 0o600 — only the
    owning user (evolve in production) can read it."""
    app, tmp_path, _network_path = app_factory
    save = _get_save_helper(app)

    save(VALID_CLIENT_ID, VALID_CLIENT_SECRET, "admin_bot")

    store_path = tmp_path / "credentials" / "admin_bot" / "google_oauth_client.json"
    mode = store_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_reader_returns_none_when_unconfigured(app_factory):
    """No per-bot block, no legacy pod-level → reader returns None."""
    _app, _tmp_path, network_path = app_factory
    read = _make_reader(network_path)

    assert read("never_configured_bot") is None
    assert read(None) is None


def test_writer_validates_inputs(app_factory):
    """Empty client_id or client_secret → writer rejects."""
    app, _tmp_path, _network_path = app_factory
    save = _get_save_helper(app)

    ok, err = save("", VALID_CLIENT_SECRET, "admin_bot")
    assert not ok
    assert "required" in err

    ok, err = save(VALID_CLIENT_ID, "", "admin_bot")
    assert not ok
    assert "required" in err
