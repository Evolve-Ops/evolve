"""tests/test_google_oauth_state_persistence.py — disk-backed
``_GOOGLE_OAUTH_STATE`` lifecycle.

Replaces the prior in-memory dict with a per-state JSON file under
``{shared_dir}/oauth_state/``. The motivating bug: an admin-server
restart mid-OAuth-flow wiped the state map, and Google's redirect-back
hit the callback as "Unknown or expired state." Persistence to disk
survives restarts within the 10-min TTL.

Asserts:
  * create + read round-trip
  * set_result mutates the on-disk record
  * consume removes the file
  * expired entries are reaped on next read
  * GC sweeps stale files on create
  * cross-process shape: read after a fresh module import returns the
    same payload (proves persistence — no in-memory state required)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect ``_oauth_state_dir()`` to a tmp path so tests don't
    write into a real shared dir."""
    from evolve_admin.web import server as _srv
    state_dir = tmp_path / "oauth_state"
    monkeypatch.setattr(_srv, "_oauth_state_dir", lambda: state_dir)
    return state_dir


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle: create → get → set_result → consume
# ─────────────────────────────────────────────────────────────────────────────


def test_create_writes_file(state_dir):
    """``_google_state_create`` returns a token + writes a JSON file
    at ``{state_dir}/<token>.json`` containing the request payload."""
    from evolve_admin.web import server as _srv

    token = _srv._google_state_create(
        bot_id="admin_bot",
        services=["gmail"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri="https://example.com/callback",
    )
    assert token  # non-empty urlsafe base64

    path = state_dir / f"{token}.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["bot_id"] == "admin_bot"
    assert payload["services"] == ["gmail"]
    assert payload["redirect_uri"] == "https://example.com/callback"
    assert payload["result"] == {"status": "pending"}
    assert payload["expires_at"] > time.time()


def test_get_returns_persisted_entry(state_dir):
    """``_google_state_get`` reads the file and returns the dict."""
    from evolve_admin.web import server as _srv

    token = _srv._google_state_create(
        bot_id="admin_bot", services=[], scopes=[], redirect_uri="x",
    )
    entry = _srv._google_state_get(token)
    assert entry is not None
    assert entry["bot_id"] == "admin_bot"


def test_get_returns_none_for_unknown(state_dir):
    from evolve_admin.web import server as _srv
    assert _srv._google_state_get("nonexistent-token") is None


def test_set_result_persists_to_disk(state_dir):
    """After ``_google_state_set_result``, the on-disk file shows the
    new result. (This is the load-bearing fix — without persistence,
    the result lives only in process memory and dies on restart.)"""
    from evolve_admin.web import server as _srv

    token = _srv._google_state_create(
        bot_id="admin_bot", services=[], scopes=[], redirect_uri="x",
    )
    ok = _srv._google_state_set_result(
        token, {"status": "success", "google_account": "x@y.com"},
    )
    assert ok is True

    # Read the file directly (not via _google_state_get) — proves the
    # write hit disk, not just in-memory cache.
    path = state_dir / f"{token}.json"
    payload = json.loads(path.read_text())
    assert payload["result"]["status"] == "success"
    assert payload["result"]["google_account"] == "x@y.com"


def test_set_result_returns_false_for_unknown(state_dir):
    from evolve_admin.web import server as _srv
    ok = _srv._google_state_set_result(
        "nonexistent-token", {"status": "success"},
    )
    assert ok is False


def test_consume_returns_dict_and_removes_file(state_dir):
    """``_google_state_consume`` returns the prior contents AND deletes
    the file so the same state can't be redeemed twice."""
    from evolve_admin.web import server as _srv

    token = _srv._google_state_create(
        bot_id="admin_bot", services=["gmail"], scopes=[], redirect_uri="x",
    )
    _srv._google_state_set_result(token, {"status": "success"})

    path = state_dir / f"{token}.json"
    assert path.exists()

    consumed = _srv._google_state_consume(token)
    assert consumed is not None
    assert consumed["bot_id"] == "admin_bot"
    assert consumed["result"]["status"] == "success"

    # File is gone — second consume returns None
    assert not path.exists()
    assert _srv._google_state_consume(token) is None


# ─────────────────────────────────────────────────────────────────────────────
# Expiry + GC
# ─────────────────────────────────────────────────────────────────────────────


def test_expired_entry_returns_none_and_self_deletes(state_dir):
    """``_google_state_get`` on an expired entry returns None AND
    deletes the file as a side effect (next get is consistent)."""
    from evolve_admin.web import server as _srv

    # Hand-craft an expired entry
    state_dir.mkdir(parents=True, exist_ok=True)
    expired_token = "expired-token"
    path = state_dir / f"{expired_token}.json"
    path.write_text(json.dumps({
        "bot_id": "admin_bot",
        "services": [],
        "scopes": [],
        "redirect_uri": "x",
        "expires_at": time.time() - 60,  # 60s in the past
        "result": {"status": "pending"},
    }))

    # First get → None + file removed
    assert _srv._google_state_get(expired_token) is None
    assert not path.exists()


def test_set_result_rejects_expired_entry(state_dir):
    """Set_result on expired entry returns False without writing."""
    from evolve_admin.web import server as _srv

    state_dir.mkdir(parents=True, exist_ok=True)
    expired_token = "expired-token"
    path = state_dir / f"{expired_token}.json"
    path.write_text(json.dumps({
        "bot_id": "admin_bot", "services": [], "scopes": [], "redirect_uri": "x",
        "expires_at": time.time() - 1,
        "result": {"status": "pending"},
    }))

    ok = _srv._google_state_set_result(expired_token, {"status": "success"})
    assert ok is False
    # File untouched (we don't actively delete on set_result; get_path does)
    payload = json.loads(path.read_text())
    assert payload["result"]["status"] == "pending"


def test_create_runs_gc_on_expired_files(state_dir):
    """Creating a new state opportunistically GCs expired files."""
    from evolve_admin.web import server as _srv

    state_dir.mkdir(parents=True, exist_ok=True)
    # Seed an expired file
    expired = state_dir / "stale.json"
    expired.write_text(json.dumps({
        "bot_id": "x", "services": [], "scopes": [], "redirect_uri": "x",
        "expires_at": time.time() - 600,
        "result": {"status": "pending"},
    }))

    # Seed a non-expired file (should survive GC)
    fresh = state_dir / "fresh.json"
    fresh.write_text(json.dumps({
        "bot_id": "y", "services": [], "scopes": [], "redirect_uri": "x",
        "expires_at": time.time() + 600,
        "result": {"status": "pending"},
    }))

    # Trigger GC via create
    _srv._google_state_create(
        bot_id="z", services=[], scopes=[], redirect_uri="x",
    )

    assert not expired.exists(), "GC did not delete expired file"
    assert fresh.exists(), "GC incorrectly deleted non-expired file"


# ─────────────────────────────────────────────────────────────────────────────
# Persistence across "restart" — the load-bearing test for Bug 5
# ─────────────────────────────────────────────────────────────────────────────


def test_state_survives_module_reload_simulating_restart(state_dir, monkeypatch):
    """The motivating Bug 5 case: admin-server restart between
    ``_google_state_create`` (when the OAuth URL was generated) and
    ``_google_state_get`` (when Google's callback arrives) used to wipe
    the in-memory state, leaving the user on 'Unknown or expired state'.

    Simulate by importlib.reload-ing the server module after create —
    if persistence works, the file is still there and ``_google_state_get``
    finds it. (Module reload re-runs all module-level initialization,
    which is where the old in-memory dict was emptied.)
    """
    import importlib
    from evolve_admin.web import server as _srv

    token = _srv._google_state_create(
        bot_id="admin_bot", services=["gmail"], scopes=[],
        redirect_uri="https://example.com/cb",
    )

    # Confirm pre-restart visibility
    assert _srv._google_state_get(token) is not None

    # "Restart" the module (re-runs module-level init; would have
    # cleared an in-memory dict).
    _srv = importlib.reload(_srv)
    # Re-apply the monkeypatch onto the fresh module object — the
    # patch was on the old module reference. Production code wouldn't
    # need this; the test does because reload returns a fresh object.
    monkeypatch.setattr(_srv, "_oauth_state_dir", lambda: state_dir)

    # Post-"restart" the state is still there
    entry = _srv._google_state_get(token)
    assert entry is not None, (
        "State did not survive module reload — persistence is broken"
    )
    assert entry["bot_id"] == "admin_bot"
    assert entry["redirect_uri"] == "https://example.com/cb"
