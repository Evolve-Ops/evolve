"""End-to-end credential-source coverage for engine LLM resolution (#3475).

The bug: on the reference Linux pod ``resolve_infra_llm()`` returned ``None``
("no provider credentialed") while the pod's Anthropic credential sat readable
on disk, so every engine LLM feature migrated in #3466 was silently dark. Two
independent defects stacked:

  1. ``primary_bot._load_network_default()`` hardcoded the **macOS** canonical
     ``network.json`` path, so on Linux (``/var/lib/evolve``) it returned ``{}``
     → no primary bot → no home → no keys. This was THE live cause, and it hits
     every reader that resolves ``network=None`` (which is how the engine call
     sites reach ``resolve_infra_llm``).
  2. ``primary_bot`` carried its OWN narrower auth-store ladder (JSON first,
     a hardcoded ``main``-agent sqlite path, no ``.bak`` rung, no non-``main``
     agent discovery) while ``evolve_admin.oc_store`` carried the full one —
     "two readers, one blind". Both now walk ``oc_auth_store``.

These tests pin the resolution end-to-end from real on-disk fixtures — no
monkeypatched key reader — one test per credential source, so a future storage
move fails a specific, legible test instead of going quietly dark.

Every key here is an obvious fake; nothing real is read or logged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ANALYZER_DIR = Path(__file__).resolve().parents[1]
if str(ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZER_DIR))

import infra_llm  # noqa: E402
import oc_auth_store  # noqa: E402
import primary_bot  # noqa: E402

_SQLITE_KEY = "sk-ant-sqlite-source-test"
_JSON_KEY = "sk-ant-json-source-test"
_BAK_KEY = "sk-ant-bak-source-test"
_ENV_KEY = "sk-ant-env-source-test"
_OAI_KEY = "sk-oai-sqlite-source-test"

_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}

_ALL_KEY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "MOONSHOT_API_KEY",
    "EVOLVE_NETWORK",
    "EVOLVE_NETWORK_JSON",
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_ambient_state(monkeypatch):
    """No ambient provider keys / network overrides, and never spawn ``sudo``.

    The sudo ``sqlite3 -readonly`` belt is a production fallback for a pre-ACL
    bot; a test must never shell out to it (it would prompt, or silently depend
    on the runner's sudoers). Stubbed to ``None`` so the library read is the
    only path exercised. monkeypatch tears every one of these down.
    """
    for var in _ALL_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(oc_auth_store, "_read_sqlite_store_via_sudo", lambda db: None)


@pytest.fixture
def primary_home(tmp_path, monkeypatch):
    """Point the primary bot's home at a tmp dir (no account lookup in tests)."""
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda network: tmp_path)
    return tmp_path


def _profiles_blob(**provider_keys: str) -> str:
    """Canonical OpenClaw auth-profiles JSON carrying one api_key per provider."""
    return json.dumps(
        {
            "version": 1,
            "profiles": {
                f"{prov}:api_key": {"type": "api_key", "provider": prov, "key": key}
                for prov, key in provider_keys.items()
            },
        }
    )


def _agent_dir(home: Path, agent_id: str = "main") -> Path:
    d = home / ".openclaw" / "agents" / agent_id / "agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_sqlite_store(agent_dir: Path, store_json: str) -> Path:
    """Create ``openclaw-agent.sqlite`` with OpenClaw's real auth-store schema.

    Schema verified live on both pods (OC 2026.6.x):
        CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY,
                                         store_json TEXT NOT NULL,
                                         updated_at INTEGER NOT NULL);
    Built in WAL mode, as OpenClaw does.
    """
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


def _write_legacy_json(agent_dir: Path, store_json: str) -> Path:
    p = agent_dir / "auth-profiles.json"
    p.write_text(store_json)
    return p


def _write_bak(agent_dir: Path, store_json: str, epoch_ms: int = 1_782_196_735_301) -> Path:
    """The snapshot OpenClaw's auth doctor leaves when it REMOVES the JSON."""
    p = agent_dir / f"auth-profiles.json.sqlite-import.{epoch_ms}.bak"
    p.write_text(store_json)
    return p


# ── the reported failure: a sqlite-ONLY pod (the live VPS state) ───────────────


def test_sqlite_only_pod_resolves_infra_llm(primary_home):
    """A primary bot whose ONLY credential source is the OC sqlite store —
    exactly the post-migration state on the reference pod (no
    ``auth-profiles.json``, a ``.sqlite-import`` bak, and the live store).
    ``resolve_infra_llm`` must return a target WITH the key."""
    _build_sqlite_store(_agent_dir(primary_home), _profiles_blob(anthropic=_SQLITE_KEY))

    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None, "sqlite-stored credential must resolve an infra LLM target"
    assert target.provider == "anthropic"
    assert target.model.startswith("anthropic/")
    assert target.api_key == _SQLITE_KEY

    standard = infra_llm.resolve_infra_llm("standard", network=dict(_NET))
    assert standard is not None and standard.api_key == _SQLITE_KEY


def test_sqlite_only_pod_resolves_credentialed_target(primary_home):
    """The pinned-model helper honors a sqlite-stored credential too."""
    _build_sqlite_store(_agent_dir(primary_home), _profiles_blob(anthropic=_SQLITE_KEY))

    target = infra_llm.credentialed_target(
        "anthropic/claude-haiku-4-5", network=dict(_NET)
    )
    assert target is not None and target.api_key == _SQLITE_KEY
    # An uncredentialed provider is still refused — no presuming.
    assert infra_llm.credentialed_target("openai/gpt-4o-mini", network=dict(_NET)) is None


# ── regression pins for the other rungs of the ladder ─────────────────────────


def test_legacy_json_only_pod_still_resolves(primary_home):
    """Pre-migration pod (the shape that always worked) must keep working."""
    _write_legacy_json(_agent_dir(primary_home), _profiles_blob(anthropic=_JSON_KEY))

    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _JSON_KEY


def test_bak_only_pod_resolves(primary_home):
    """Transitional state: the doctor removed the JSON and the sqlite store is
    gone/unreadable, leaving only the ``.sqlite-import.<ms>.bak`` snapshot.
    ``primary_bot`` had no bak rung at all before #3475."""
    _write_bak(_agent_dir(primary_home), _profiles_blob(anthropic=_BAK_KEY))

    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _BAK_KEY


def test_newest_bak_wins(primary_home):
    agent = _agent_dir(primary_home)
    _write_bak(agent, _profiles_blob(anthropic="sk-ant-old-bak-test"), epoch_ms=1_700_000_000_000)
    _write_bak(agent, _profiles_blob(anthropic=_BAK_KEY), epoch_ms=1_700_000_999_999)

    assert primary_bot.read_primary_bot_llm_keys(dict(_NET))["anthropic"] == _BAK_KEY


def test_sqlite_under_non_main_agent_resolves(primary_home):
    """Some bots run their primary under a non-``main`` agent id; the store lives
    under that dir. ``primary_bot``'s old hardcoded ``main`` path missed it."""
    _build_sqlite_store(
        _agent_dir(primary_home, "email-reader"), _profiles_blob(anthropic=_SQLITE_KEY)
    )

    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _SQLITE_KEY


def test_multi_provider_keys_read_from_sqlite(primary_home):
    """Every provider in the sqlite store is visible, not just anthropic —
    the point of the provider-agnostic engine path."""
    _build_sqlite_store(
        _agent_dir(primary_home),
        _profiles_blob(anthropic=_SQLITE_KEY, openai=_OAI_KEY),
    )

    keys = primary_bot.read_primary_bot_llm_keys(dict(_NET))
    assert keys == {"anthropic": _SQLITE_KEY, "openai": _OAI_KEY}
    # Both are usable as pinned targets.
    for model, expected in (
        ("anthropic/claude-haiku-4-5", _SQLITE_KEY),
        ("openai/gpt-4o-mini", _OAI_KEY),
    ):
        t = infra_llm.credentialed_target(model, network=dict(_NET))
        assert t is not None and t.api_key == expected, model


def test_keyless_sqlite_store_falls_through_to_legacy_json(primary_home):
    """A readable sqlite store carrying NO usable credential must not shadow a
    JSON that has one — the reader walks EVERY payload, not just the first
    readable source. (Guards the sqlite-before-json precedence change: it can
    reorder which key wins, never make a present key unreachable.)"""
    agent = _agent_dir(primary_home)
    _build_sqlite_store(agent, json.dumps({"version": 1, "profiles": {}}))
    _write_legacy_json(agent, _profiles_blob(anthropic=_JSON_KEY))

    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _JSON_KEY


def test_env_override_still_wins_over_sqlite(primary_home, monkeypatch):
    """The per-provider env override is the operator's escape hatch — it must
    still beat every on-disk source."""
    _build_sqlite_store(_agent_dir(primary_home), _profiles_blob(anthropic=_SQLITE_KEY))
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ENV_KEY)

    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _ENV_KEY


def test_no_credential_source_resolves_none(primary_home):
    """A genuinely keyless pod still resolves to None — callers keep their
    degrade paths; the fix must not manufacture a credential."""
    _agent_dir(primary_home)  # agent dir exists, no store of any kind
    assert infra_llm.resolve_infra_llm("fast", network=dict(_NET)) is None
    assert primary_bot.read_primary_bot_llm_keys(dict(_NET)) == {}


# ── sqlite read rungs: mode=ro → sudo → immutable=1 ───────────────────────────


def test_sqlite_read_falls_back_to_immutable_when_ro_open_fails(primary_home, monkeypatch):
    """When the read-only open can't initialise the ``-shm`` sidecar (gateway
    down + read-only ACL) AND the root ``sqlite3`` grant is missing (sudoers
    refresh is manual by design), the ``immutable=1`` read of the main db file
    still recovers the credential. That rung was in ``primary_bot``'s old reader
    and must not be lost in the consolidation."""
    _build_sqlite_store(_agent_dir(primary_home), _profiles_blob(anthropic=_SQLITE_KEY))

    real_connect = oc_auth_store.sqlite3.connect

    def _connect(target, *a, **kw):
        if isinstance(target, str) and "mode=ro" in target:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(oc_auth_store.sqlite3, "connect", _connect)
    # The sudo rung is stubbed out by the autouse fixture, so immutable=1 is the
    # only remaining path.
    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _SQLITE_KEY


def test_sqlite_read_returns_nothing_when_every_rung_fails(primary_home, monkeypatch):
    """All three rungs blocked → the sqlite source yields nothing and the ladder
    falls through to the JSON, rather than raising or resolving a partial key."""
    agent = _agent_dir(primary_home)
    _build_sqlite_store(agent, _profiles_blob(anthropic=_SQLITE_KEY))
    _write_legacy_json(agent, _profiles_blob(anthropic=_JSON_KEY))

    def _connect(target, *a, **kw):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(oc_auth_store.sqlite3, "connect", _connect)
    target = infra_llm.resolve_infra_llm("fast", network=dict(_NET))
    assert target is not None and target.api_key == _JSON_KEY


# ── the platform-keyed network.json path (defect 1, the live cause) ────────────


def _write_network(path: Path) -> dict:
    net = {
        "evolveVersion": "0.1.0",
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "user": "evo"}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(net))
    return net


def test_load_network_default_uses_platform_keyed_canonical(tmp_path, monkeypatch):
    """``_load_network_default`` must resolve through ``evolve_config``'s
    platform-keyed canonical path (``/Users/Shared/evolve`` on macOS,
    ``/var/lib/evolve`` on Linux) — NOT a macOS literal. Pinned by moving that
    canonical constant and asserting the loader follows it."""
    import evolve_config

    net_path = tmp_path / "shared" / "network.json"
    _write_network(net_path)
    monkeypatch.setattr(evolve_config, "_SHARED_NETWORK", net_path)
    monkeypatch.setattr(evolve_config, "CANONICAL_NETWORK_JSON", net_path)

    loaded = primary_bot._load_network_default()
    assert loaded.get("primary") == "evo", (
        "the network.json path must follow the platform-keyed canonical constant; "
        "a hardcoded macOS path resolves to {} on every Linux pod"
    )


def test_load_network_default_honors_env_override(tmp_path, monkeypatch):
    """``EVOLVE_NETWORK`` (the repo-wide override) is honored, so an operator or
    a non-standard pod layout can still point the engine at its config."""
    import evolve_config

    net_path = tmp_path / "elsewhere" / "network.json"
    _write_network(net_path)
    # Canonical location deliberately absent, so only the env var can satisfy it.
    monkeypatch.setattr(evolve_config, "_SHARED_NETWORK", tmp_path / "nope" / "network.json")
    monkeypatch.setattr(evolve_config, "CANONICAL_NETWORK_JSON", tmp_path / "nope" / "network.json")
    monkeypatch.setenv("EVOLVE_NETWORK", str(net_path))

    assert primary_bot._load_network_default().get("primary") == "evo"


def test_sqlite_only_pod_resolves_with_network_from_disk(tmp_path, monkeypatch):
    """The two defects, together, on the shape that failed live: the network
    dict comes from the platform-keyed canonical path (nothing passed in) and
    the credential comes from the sqlite store only."""
    import evolve_config

    net_path = tmp_path / "shared" / "network.json"
    _write_network(net_path)
    monkeypatch.setattr(evolve_config, "_SHARED_NETWORK", net_path)
    monkeypatch.setattr(evolve_config, "CANONICAL_NETWORK_JSON", net_path)

    home = tmp_path / "home" / "evo"
    home.mkdir(parents=True)
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda network: home)
    _build_sqlite_store(_agent_dir(home), _profiles_blob(anthropic=_SQLITE_KEY))

    target = infra_llm.resolve_infra_llm("fast")  # network=None — the engine's own call shape
    assert target is not None, "engine call sites pass no network; that path must work"
    assert target.api_key == _SQLITE_KEY
