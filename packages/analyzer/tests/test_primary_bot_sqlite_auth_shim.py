"""Primary-bot Anthropic-key resolution across the OC auth-store migration.

OpenClaw 2026.6.x migrated each agent's ``auth-profiles.json`` into the
per-agent SQLite store ``openclaw-agent.sqlite`` (table ``auth_profile_store``,
row ``store_key='primary'``, column ``store_json`` = the same
``{"profiles": {...}}`` bytes), renaming the JSON to
``auth-profiles.json.sqlite-import.<ms>.bak``. Evolve's primary-bot key readers
(``read_primary_bot_anthropic_key`` / ``read_primary_bot_keys_by_provider``)
walked JSON-only, so every engine background reader (judge, summarizer,
classifier, spec extractor, help bot, …) returned ``""`` pod-wide once the
migration landed.

These tests pin the shim that closes that gap:
  - the read-only ``immutable=1`` SQLite recovery (``read_agent_auth_store_json``),
    including a live WAL sidecar (mirrors the running-gateway case), and
  - the reader precedence env → legacy JSON → SQLite store.

Keys here are obvious fakes (``sk-ant-...-test``); nothing real is ever logged.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ANALYZER_DIR = Path(__file__).resolve().parents[1]
if str(ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZER_DIR))

import primary_bot  # noqa: E402

# Fake, non-secret test keys — never real credentials.
_JSON_KEY = "sk-ant-json-test"
_SQLITE_KEY = "sk-ant-sqlite-test"
_ENV_KEY = "sk-ant-env-test"


# ── Fixtures / builders ───────────────────────────────────────────────────────


def _profiles_blob(key: str) -> str:
    """Canonical OpenClaw auth-profiles JSON string carrying an Anthropic key."""
    return json.dumps(
        {
            "profiles": {
                "anthropic:api_key": {
                    "type": "api_key",
                    "provider": "anthropic",
                    "key": key,
                }
            }
        }
    )


def _agent_dir(home: Path) -> Path:
    d = home / ".openclaw" / "agents" / "main" / "agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_sqlite_store(agent_dir: Path, store_json: str, *, wal: bool = True) -> Path:
    """Create ``openclaw-agent.sqlite`` with the real auth-store schema.

    Built in WAL mode (matching live OpenClaw) then closed — the last
    connection to close checkpoints the WAL into the main DB file, so an
    ``immutable=1`` reader (which ignores the WAL) sees the row.
    """
    db = agent_dir / "openclaw-agent.sqlite"
    conn = sqlite3.connect(db)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT, updated_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store (store_key, store_json, updated_at) "
            "VALUES (?, ?, ?)",
            ("primary", store_json, 0),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _build_sqlite_store_open(agent_dir: Path, store_json: str):
    """Create ``openclaw-agent.sqlite`` and RETURN THE OPEN WRITER connection
    with the row committed to an **un-checkpointed WAL** — the running-gateway
    case. Caller MUST close the returned connection. ``immutable=1`` cannot see
    a row in this state (it skips the WAL); a correct ``mode=ro`` read can.
    """
    db = agent_dir / "openclaw-agent.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE auth_profile_store ("
        "store_key TEXT PRIMARY KEY, store_json TEXT, updated_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO auth_profile_store (store_key, store_json, updated_at) "
        "VALUES (?, ?, ?)",
        ("primary", store_json, 0),
    )
    conn.commit()  # committed to the WAL, NOT checkpointed (connection stays open)
    return db, conn


def _write_legacy_json(agent_dir: Path, store_json: str) -> Path:
    p = agent_dir / "auth-profiles.json"
    p.write_text(store_json)
    return p


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """The env var beats every file source — keep it out unless a test sets it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def primary_home(tmp_path, monkeypatch):
    """Point the primary bot's home at a tmp dir so the path-walkers + the
    SQLite path resolve under the test sandbox."""
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda network: tmp_path)
    return tmp_path


_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}


# ── read_agent_auth_store_json (the immutable SQLite shim) ─────────────────────


def test_sqlite_shim_recovers_store(tmp_path):
    agent = _agent_dir(tmp_path)
    db = _build_sqlite_store(agent, _profiles_blob(_SQLITE_KEY))
    raw = primary_bot.read_agent_auth_store_json(db)
    assert primary_bot.extract_anthropic_key(raw) == _SQLITE_KEY


def test_sqlite_shim_none_when_absent(tmp_path):
    missing = _agent_dir(tmp_path) / "openclaw-agent.sqlite"
    assert primary_bot.read_agent_auth_store_json(missing) is None


def test_sqlite_shim_none_on_foreign_schema(tmp_path):
    """A DB without the expected table degrades to None, never raises."""
    agent = _agent_dir(tmp_path)
    db = agent / "openclaw-agent.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE something_else (a TEXT)")
    conn.commit()
    conn.close()
    assert primary_bot.read_agent_auth_store_json(db) is None


def test_sqlite_shim_none_on_malformed_store_json(tmp_path):
    agent = _agent_dir(tmp_path)
    db = _build_sqlite_store(agent, "{not valid json")
    assert primary_bot.read_agent_auth_store_json(db) is None


def test_sqlite_shim_reads_uncheckpointed_wal(tmp_path):
    """The running-gateway case: the row is committed to the WAL but NOT
    checkpointed into the main DB (writer connection still open). A correct
    read-only reader (``mode=ro``, which consults the WAL) must still recover
    it. This is the regression guard for the migration bug — ``immutable=1``
    returns "no such table" here and would FAIL this test, which is the point.
    """
    agent = _agent_dir(tmp_path)
    db, writer = _build_sqlite_store_open(agent, _profiles_blob(_SQLITE_KEY))
    try:
        assert (agent / "openclaw-agent.sqlite-wal").exists(), "expected a live WAL"
        raw = primary_bot.read_agent_auth_store_json(db)
        assert primary_bot.extract_anthropic_key(raw) == _SQLITE_KEY
    finally:
        writer.close()


def test_sqlite_shim_reads_newer_uncheckpointed_value(tmp_path):
    """A key rotated by the live gateway lands in the WAL ahead of the
    checkpointed main file. The reader must return the CURRENT (WAL) value, not
    a stale checkpointed one — ``immutable=1`` would return the stale value."""
    agent = _agent_dir(tmp_path)
    # First value checkpointed into the main file (connection closed).
    _build_sqlite_store(agent, _profiles_blob("sk-ant-stale-test"))
    db = agent / "openclaw-agent.sqlite"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE auth_profile_store SET store_json=? WHERE store_key='primary'",
            (_profiles_blob(_SQLITE_KEY),),
        )
        writer.commit()  # newer value lives in the WAL, main file still stale
        assert (agent / "openclaw-agent.sqlite-wal").exists()
        raw = primary_bot.read_agent_auth_store_json(db)
        assert primary_bot.extract_anthropic_key(raw) == _SQLITE_KEY
    finally:
        writer.close()


# ── read_primary_bot_anthropic_key precedence ─────────────────────────────────


def test_anthropic_key_falls_back_to_sqlite(primary_home):
    """Migrated bot: no live JSON, key recovered from the SQLite store."""
    agent = _agent_dir(primary_home)
    _build_sqlite_store(agent, _profiles_blob(_SQLITE_KEY))
    assert primary_bot.read_primary_bot_anthropic_key(_NET) == _SQLITE_KEY


def test_anthropic_key_legacy_json_wins_over_sqlite(primary_home):
    """Un-migrated / older OpenClaw: live JSON takes precedence over the store."""
    agent = _agent_dir(primary_home)
    _write_legacy_json(agent, _profiles_blob(_JSON_KEY))
    _build_sqlite_store(agent, _profiles_blob(_SQLITE_KEY))
    assert primary_bot.read_primary_bot_anthropic_key(_NET) == _JSON_KEY


def test_anthropic_key_env_wins_over_everything(primary_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ENV_KEY)
    agent = _agent_dir(primary_home)
    _write_legacy_json(agent, _profiles_blob(_JSON_KEY))
    _build_sqlite_store(agent, _profiles_blob(_SQLITE_KEY))
    assert primary_bot.read_primary_bot_anthropic_key(_NET) == _ENV_KEY


def test_anthropic_key_empty_when_no_source(primary_home):
    _agent_dir(primary_home)  # dir exists, but no JSON and no sqlite
    assert primary_bot.read_primary_bot_anthropic_key(_NET) == ""


# ── read_primary_bot_keys_by_provider precedence ──────────────────────────────


def test_keys_by_provider_falls_back_to_sqlite(primary_home):
    agent = _agent_dir(primary_home)
    _build_sqlite_store(agent, _profiles_blob(_SQLITE_KEY))
    keys = primary_bot.read_primary_bot_keys_by_provider(_NET)
    assert keys.get("anthropic") == _SQLITE_KEY


def test_keys_by_provider_legacy_json_wins(primary_home):
    agent = _agent_dir(primary_home)
    _write_legacy_json(agent, _profiles_blob(_JSON_KEY))
    _build_sqlite_store(agent, _profiles_blob(_SQLITE_KEY))
    keys = primary_bot.read_primary_bot_keys_by_provider(_NET)
    assert keys.get("anthropic") == _JSON_KEY


def test_keys_by_provider_empty_when_no_source(primary_home):
    _agent_dir(primary_home)
    assert primary_bot.read_primary_bot_keys_by_provider(_NET) == {}


# ── _extract_keys_by_provider (shared extractor) parity ───────────────────────


def test_extract_keys_by_provider_prefers_api_key_over_token():
    raw = {
        "profiles": {
            "anthropic:oauth": {"type": "token", "token": "oauth-not-a-key", "provider": "anthropic"},
            "anthropic:api_key": {"type": "api_key", "key": _SQLITE_KEY, "provider": "anthropic"},
        }
    }
    assert primary_bot._extract_keys_by_provider(raw).get("anthropic") == _SQLITE_KEY
