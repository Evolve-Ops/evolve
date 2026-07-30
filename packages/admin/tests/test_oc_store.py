"""Unit tests for ``evolve_admin.oc_store`` — the OC auth-store reader.

Covers the four source branches of ``read_auth_store`` (sqlite present /
legacy-json-only / bak-only / none), the provider-match through the shared
parser (``anthropic:api`` vs ``anthropic:api_key`` both resolve), and the
privileged-read guard (a symlinked store component is refused).

The sqlite fixtures are built with the REAL schema verified live on the mini
(OpenClaw 2026.6.x), in WAL mode, so the read path exercises the same shape
production hits:

    CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY,
                                     store_json TEXT NOT NULL,
                                     updated_at INTEGER NOT NULL);
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Mirror the scanner's analyzer-on-path setup so ``primary_bot`` (the parser)
# imports the same way oc_store resolves it at runtime.
_ANALYZER_DIR = Path(__file__).resolve().parents[2] / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin import oc_store  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────────────


def _agent_dir(home: Path) -> Path:
    d = home.joinpath(".openclaw", "agents", "main", "agent")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profiles_json(profile_key: str = "anthropic:api_key", key: str = "sk-ant-live") -> str:
    return json.dumps(
        {
            "version": 1,
            "profiles": {
                profile_key: {"type": "api_key", "provider": "anthropic", "key": key},
            },
        }
    )


def _build_sqlite_store(agent_dir: Path, store_json: str) -> Path:
    """Create openclaw-agent.sqlite with the real schema, in WAL mode."""
    db = agent_dir / "openclaw-agent.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store (store_key, store_json, updated_at) "
            "VALUES (?, ?, ?)",
            ("primary", store_json, 1_700_000_000),
        )
        conn.commit()
    finally:
        conn.close()
    return db


# ── (a) sqlite present ────────────────────────────────────────────────────────


def test_read_auth_store_from_sqlite(tmp_path):
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    _build_sqlite_store(agent, _profiles_json())

    raw = oc_store.read_auth_store("bot", home=home)
    assert raw is not None
    assert json.loads(raw)["profiles"]["anthropic:api_key"]["key"] == "sk-ant-live"
    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-live"


def test_sqlite_wins_over_legacy_json(tmp_path):
    """When BOTH the sqlite store and a legacy json exist, sqlite is the
    live source (post-migration) and wins."""
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    _build_sqlite_store(agent, _profiles_json(key="sk-ant-sqlite"))
    (agent / "auth-profiles.json").write_text(_profiles_json(key="sk-ant-stale-json"))

    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-sqlite"


def test_sqlite_read_is_wal_safe(tmp_path):
    """A write left in the WAL (uncommitted-to-main-db) must still be read —
    the library open resolves the -wal sidecar; a raw byte read would miss it."""
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    db = _build_sqlite_store(agent, _profiles_json(key="sk-ant-initial"))
    # Update the row but DO NOT checkpoint — the new value lives in the WAL.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(
            "UPDATE auth_profile_store SET store_json=? WHERE store_key='primary'",
            (_profiles_json(key="sk-ant-walonly"),),
        )
        conn.commit()
        assert (agent / "openclaw-agent.sqlite-wal").exists(), "expected a live WAL sidecar"
        assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-walonly"
    finally:
        conn.close()


# ── (b) legacy json only ──────────────────────────────────────────────────────


def test_read_auth_store_from_legacy_json(tmp_path):
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    (agent / "auth-profiles.json").write_text(_profiles_json(key="sk-ant-legacy"))

    raw = oc_store.read_auth_store("bot", home=home)
    assert raw is not None
    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-legacy"


# ── (c) bak only (transitional) ───────────────────────────────────────────────


def test_read_auth_store_from_bak(tmp_path):
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    (agent / "auth-profiles.json.sqlite-import.1700000000000.bak").write_text(
        _profiles_json(key="sk-ant-bak")
    )

    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-bak"


def test_newest_bak_wins(tmp_path):
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    (agent / "auth-profiles.json.sqlite-import.1700000000000.bak").write_text(
        _profiles_json(key="sk-ant-old")
    )
    (agent / "auth-profiles.json.sqlite-import.1700000999999.bak").write_text(
        _profiles_json(key="sk-ant-new")
    )

    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-new"


# ── (d) none ──────────────────────────────────────────────────────────────────


def test_read_auth_store_none_when_no_source(tmp_path, caplog):
    home = tmp_path / "bot"
    _agent_dir(home)  # empty agent dir, no store of any kind

    import logging

    with caplog.at_level(logging.ERROR, logger="evolve.oc_store"):
        assert oc_store.read_auth_store("bot", home=home) is None
    # Total failure is LOUD — never a silent empty string.
    assert any("NO auth source resolved" in r.message for r in caplog.records)
    assert oc_store.read_anthropic_key("bot", home=home) == ""


# ── provider-match: the parser tolerance is preserved through oc_store ────────


@pytest.mark.parametrize("profile_key", ["anthropic:api", "anthropic:api_key"])
def test_provider_match_both_profile_key_strings(tmp_path, profile_key):
    """The profile-key string is inconsistent across bots (one bot stores the
    profile under ``anthropic:api_key``, another under ``anthropic:api``). Both
    must resolve — the match is on the ``provider`` field, not the key string."""
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    _build_sqlite_store(agent, _profiles_json(profile_key=profile_key, key="sk-ant-x"))

    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-x"


def test_gateway_auth_bot_has_store_but_no_anthropic_key(tmp_path):
    """A gateway-auth bot has a real store but no raw anthropic api_key.
    read_auth_store returns the JSON (no LOUD failure); the key resolves to ""
    — a valid degrade signal, not an error."""
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    _build_sqlite_store(
        agent,
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "google:oauth": {
                        "type": "oauth",
                        "provider": "google",
                        "access_token": "ya29.fake",
                    }
                },
            }
        ),
    )

    assert oc_store.read_auth_store("bot", home=home) is not None  # store IS present
    assert oc_store.read_anthropic_key("bot", home=home) == ""  # but no anthropic key


# ── privileged-read guard: symlinked component refused ────────────────────────


def test_symlinked_db_is_refused(tmp_path):
    """A bot that swaps openclaw-agent.sqlite for a symlink (to redirect the
    privileged read at another file) is refused at the sqlite branch — the read
    falls through to legacy json instead of following the link."""
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    # A real sqlite store the attacker wants the reader to follow a link to.
    other = tmp_path / "victim.sqlite"
    _build_sqlite_store(tmp_path, _profiles_json(key="sk-ant-victim"))
    (tmp_path / "openclaw-agent.sqlite").rename(other)
    # Symlink the bot's db path at the victim.
    (agent / "openclaw-agent.sqlite").symlink_to(other)
    # Provide a legitimate legacy json so we can prove the read fell THROUGH
    # the refused symlink rather than following it.
    (agent / "auth-profiles.json").write_text(_profiles_json(key="sk-ant-legit"))

    # _validated_sqlite_path must reject the symlink…
    assert oc_store._validated_sqlite_path(home) is None
    # …and the public read falls through to the legacy json, never the victim.
    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-legit"


def test_owner_mismatch_refused(tmp_path, monkeypatch):
    """A db whose owner differs from the home owner is refused — the guard that
    catches a HARDLINK redirect (a hardlink is a regular, non-symlink file by
    every stat, so only the owner check distinguishes it). Simulated by forcing
    a foreign uid on the db's lstat, since a real foreign-owned file needs root.
    """
    home = tmp_path / "bot"
    agent = _agent_dir(home)
    _build_sqlite_store(agent, _profiles_json(key="sk-ant-foreign"))
    (agent / "auth-profiles.json").write_text(_profiles_json(key="sk-ant-legit"))
    db = agent / "openclaw-agent.sqlite"

    real_lstat = oc_store.os.lstat

    class _Stat:
        def __init__(self, real, uid):
            self._r, self.st_uid = real, uid

        def __getattr__(self, n):
            return getattr(self._r, n)

    def fake_lstat(p):
        r = real_lstat(p)
        if str(p) == str(db):
            return _Stat(r, r.st_uid + 54321)  # pretend a foreign owner
        return r

    monkeypatch.setattr(oc_store.os, "lstat", fake_lstat)
    assert oc_store._validated_sqlite_path(home) is None
    # The read falls through to the legacy json, never the owner-mismatched db.
    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-legit"


def test_symlinked_intermediate_dir_is_refused(tmp_path):
    """A symlinked INTERMEDIATE component (the agent dir) is refused too."""
    home = tmp_path / "bot"
    real_openclaw = home / ".openclaw"
    (real_openclaw / "agents" / "main").mkdir(parents=True)
    # Make `agent` a symlink to an attacker-controlled dir holding a store.
    evil = tmp_path / "evil_agent"
    evil.mkdir()
    _build_sqlite_store(evil, _profiles_json(key="sk-ant-evil"))
    (real_openclaw / "agents" / "main" / "agent").symlink_to(evil)

    assert oc_store._validated_sqlite_path(home) is None


# ── agent-dir discovery (main-first, non-main agents found) ───────────────────


def _agent_dir_for(home: Path, agent_id: str) -> Path:
    d = home.joinpath(".openclaw", "agents", agent_id, "agent")
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_discover_agent_ids_main_first(tmp_path):
    """``main`` always sorts first; other agents follow in name order. ``main``
    is included as a fallback even when it has no dir on disk."""
    home = tmp_path / "bot"
    _agent_dir_for(home, "email-reader")
    _agent_dir_for(home, "calendar")
    ids = oc_store._discover_agent_ids(home)
    assert ids[0] == "main"
    assert set(ids) >= {"main", "email-reader", "calendar"}
    # No agents dir at all → still tries main (never worse than the old hardcode).
    assert oc_store._discover_agent_ids(tmp_path / "empty") == ["main"]


def test_read_auth_store_discovers_non_main_agent(tmp_path):
    """Some bots run their primary under a non-``main`` agent id. The store lives
    under that dir; a hardcoded ``main`` would miss it entirely."""
    home = tmp_path / "bot"
    agent = _agent_dir_for(home, "email-reader")  # NO main agent dir at all
    _build_sqlite_store(agent, _profiles_json(key="sk-ant-nonmain"))

    raw = oc_store.read_auth_store("bot", home=home)
    assert raw is not None
    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-nonmain"


def test_read_auth_store_main_wins_over_other_agents(tmp_path):
    """When several agents have stores, ``main`` (the conventional primary) is
    tried first so its key resolves — discovery is additive, not reordering."""
    home = tmp_path / "bot"
    _build_sqlite_store(_agent_dir(home), _profiles_json(key="sk-ant-main"))
    _build_sqlite_store(
        _agent_dir_for(home, "email-reader"), _profiles_json(key="sk-ant-secondary")
    )

    assert oc_store.read_anthropic_key("bot", home=home) == "sk-ant-main"


def test_auth_store_present_discovers_non_main_agent(tmp_path):
    """Presence probe must see a store under a non-``main`` agent too, so a
    secondary-agent bot's bring-up isn't mis-classified as having no store."""
    home = tmp_path / "bot"
    _build_sqlite_store(_agent_dir_for(home, "email-reader"), _profiles_json())
    assert oc_store.auth_store_present("bot", home=home) is True


def test_auth_store_present_false_for_empty_non_main_agent(tmp_path):
    """A discovered agent dir with no artifacts still reports absent (False),
    not a false-positive present."""
    home = tmp_path / "bot"
    _agent_dir_for(home, "email-reader")  # empty
    assert oc_store.auth_store_present("bot", home=home) is False
