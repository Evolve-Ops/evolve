"""tests/test_oc_keys.py — credential-presence source ladder (sqlite → json → bak).

Covers the OpenClaw 2026.6.x sqlite-store migration: ``json_keys`` must read the
new ``openclaw-agent.sqlite`` store (where secrets are NOT file-readable, so a
CONFIGURED profile is the presence signal), fall back to the legacy
``auth-profiles.json`` on un-migrated pods, and — when NO readable source exists
at all — return the LOUD ``source="none"`` marker rather than a silent empty set.

Fixtures build a temp ``.openclaw`` tree (and a temp sqlite store) so no live pod
is needed; ``oc_root`` pins discovery to the temp tree.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

import oc_keys  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _agent_dir(oc_root: Path, agent_id: str = "main") -> Path:
    d = oc_root / "agents" / agent_id / "agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_sqlite_store(agent_dir: Path, profiles: dict) -> Path:
    """Create an ``openclaw-agent.sqlite`` with one primary store row."""
    db = agent_dir / "openclaw-agent.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        store_json = json.dumps({"version": 1, "profiles": profiles})
        conn.execute(
            "INSERT INTO auth_profile_store VALUES ('primary', ?, 0)",
            (store_json,),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _write_legacy_json(agent_dir: Path, profiles: dict) -> Path:
    path = agent_dir / "auth-profiles.json"
    path.write_text(json.dumps({"profiles": profiles}))
    return path


# ── sqlite primary path ───────────────────────────────────────────────────────

def test_sqlite_store_reports_each_provider(tmp_path):
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    # Secrets are NOT present in the sqlite blob — only the provider field.
    _write_sqlite_store(agent_dir, {
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key"},
        "google:default": {"provider": "google", "type": "default"},
        "brave_api_key": {"provider": "brave", "type": "api_key"},
    })

    result = oc_keys.json_keys("evo", oc_root=oc_root)

    assert result["source"] == "sqlite"
    assert "error" not in result
    keys = result["keys"]
    assert set(keys) == {"anthropic", "google", "brave"}
    # A configured profile counts the provider downstream
    # (pdata.api_key or pdata.token is truthy).
    for prov in ("anthropic", "google", "brave"):
        assert keys[prov]["api_key"] or keys[prov]["token"], prov


def test_sqlite_provider_field_drives_key_not_profile_id(tmp_path):
    """The provider must come from the ``provider`` FIELD, never from parsing
    the profileId — a profileId that disagrees with the field defers to the
    field."""
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    _write_sqlite_store(agent_dir, {
        # profileId says "openai" but the authoritative field says anthropic.
        "openai:weird_alias": {"provider": "anthropic", "type": "api_key"},
    })

    keys = oc_keys.json_keys("evo", oc_root=oc_root)["keys"]

    assert "anthropic" in keys
    assert "openai" not in keys


def test_sqlite_token_mode_sets_token_flag(tmp_path):
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    _write_sqlite_store(agent_dir, {
        "anthropic:token": {"provider": "anthropic", "type": "token"},
    })

    entry = oc_keys.json_keys("evo", oc_root=oc_root)["keys"]["anthropic"]

    assert entry["token"] is True


def test_sqlite_discovers_non_main_agent_dir(tmp_path):
    """The sqlite path must not hardcode ``main`` when a non-main agent dir is
    the only one present."""
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root, agent_id="email-reader")
    _write_sqlite_store(agent_dir, {
        "google:default": {"provider": "google", "type": "default"},
    })

    result = oc_keys.json_keys("team_bot_b", oc_root=oc_root)

    assert result["source"] == "sqlite"
    assert "google" in result["keys"]


def test_sqlite_wins_over_legacy_json(tmp_path):
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    _write_sqlite_store(agent_dir, {
        "google:default": {"provider": "google", "type": "default"},
    })
    # A stale legacy json with a different provider must be ignored.
    _write_legacy_json(agent_dir, {
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key", "key": "sk-x"},
    })

    result = oc_keys.json_keys("evo", oc_root=oc_root)

    assert result["source"] == "sqlite"
    assert set(result["keys"]) == {"google"}


# ── legacy json fallback ──────────────────────────────────────────────────────

def test_legacy_json_path_when_sqlite_absent(tmp_path):
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    _write_legacy_json(agent_dir, {
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key", "key": "sk-ant"},
        "openai:api_key": {"provider": "openai", "type": "api_key", "key": ""},
    })

    result = oc_keys.json_keys("evo", oc_root=oc_root)

    assert result["source"] == "legacy_json"
    # Legacy semantics: presence is a NON-EMPTY value, so the empty openai key
    # reports api_key False (un-migrated pods still carry raw values).
    assert result["keys"]["anthropic"]["api_key"] is True
    assert result["keys"]["openai"]["api_key"] is False


def test_explicit_auth_path_still_reads_legacy(tmp_path):
    """The pre-existing ``auth_path=`` override keeps working and does not leak
    discovery into the real ~/.openclaw."""
    agent_dir = _agent_dir(tmp_path / ".openclaw")
    legacy = _write_legacy_json(agent_dir, {
        "brave_api_key": {"provider": "brave", "type": "api_key", "key": "BSAabc"},
    })

    result = oc_keys.json_keys("evo", auth_path=legacy)

    assert result["source"] == "legacy_json"
    assert result["keys"]["brave"]["api_key"] is True


# ── bak last resort ───────────────────────────────────────────────────────────

def test_bak_last_resort_when_sqlite_and_json_absent(tmp_path):
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    bak = agent_dir / "auth-profiles.json.sqlite-import.1719240000000.bak"
    bak.write_text(json.dumps({"profiles": {
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key", "key": "sk-bak"},
    }}))

    result = oc_keys.json_keys("evo", oc_root=oc_root)

    assert result["source"] == "bak"
    assert result["keys"]["anthropic"]["api_key"] is True


# ── loud-fail marker ──────────────────────────────────────────────────────────

def test_no_readable_source_returns_loud_marker(tmp_path):
    oc_root = tmp_path / ".openclaw"
    _agent_dir(oc_root)  # empty agent dir — no sqlite, no json, no bak

    result = oc_keys.json_keys("evo", oc_root=oc_root)

    assert result["source"] == "none"
    assert result["keys"] == {}
    assert "error" in result  # NOT a silent empty set


def test_missing_oc_root_entirely_is_loud(tmp_path):
    result = oc_keys.json_keys("evo", oc_root=tmp_path / "does-not-exist")
    assert result["source"] == "none"
    assert result["keys"] == {}


def test_empty_sqlite_store_is_not_loud(tmp_path):
    """A readable store that legitimately holds zero profiles is NOT the loud
    marker — source is sqlite, keys empty, no error."""
    oc_root = tmp_path / ".openclaw"
    agent_dir = _agent_dir(oc_root)
    _write_sqlite_store(agent_dir, {})

    result = oc_keys.json_keys("evo", oc_root=oc_root)

    assert result["source"] == "sqlite"
    assert result["keys"] == {}
    assert "error" not in result
