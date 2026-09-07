"""Tests for evolve_admin.safe_upgrade — read-only preflight gates.

Covers:
- Semver-range parser handles the shapes openclaw declares in engines.node
- Each gate's pass / fail logic against synthetic metadata + filesystem
- Report persistence: report file, latest.json symlink, 20-report retention
- Concurrency: a second start_background_check while one runs returns the
  in-flight id rather than starting a new one
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import safe_upgrade as su


# ── Semver range parser ──────────────────────────────────────────────────────

@pytest.mark.parametrize("current,range_spec,expected", [
    ("20.11.1", ">=18", True),
    ("20.11.1", ">=22", False),
    ("18.0.0", ">=18", True),
    ("17.9.9", ">=18", False),
    ("20.11.1", ">=18 <22", True),
    ("22.0.0", ">=18 <22", False),
    ("18.5.0", "^18.0.0", True),
    ("19.0.0", "^18.0.0", False),
    ("20.11.5", "~20.11.0", True),
    ("20.12.0", "~20.11.0", False),
    ("18.0.0", ">=18 || >=20", True),
    ("16.0.0", ">=18 || >=20", False),
    ("20.0.0", "20", True),
    ("20.0.1", "=20.0.0", False),
])
def test_satisfies_range(current, range_spec, expected):
    assert su._satisfies_range(current, range_spec) is expected


def test_satisfies_range_unparseable():
    assert su._satisfies_range("20.11.1", "weird-range") is None
    assert su._satisfies_range("20.11.1", "") is None


# ── gate_node_version ────────────────────────────────────────────────────────

def test_gate_node_version_pass():
    with patch.object(su, "_node_version", return_value="20.11.1"):
        result, reqs = su.gate_node_version({"engines": {"node": ">=18"}})
    assert result.ok is True
    assert reqs == []
    assert result.details["current"] == "20.11.1"


def test_gate_node_version_fails_on_unmet_range():
    with patch.object(su, "_node_version", return_value="18.0.0"):
        result, reqs = su.gate_node_version({"engines": {"node": ">=22"}})
    assert result.ok is False
    assert len(reqs) == 1
    assert reqs[0].id == "node-version-mismatch"
    assert reqs[0].blocking is True


def test_gate_node_version_fails_on_missing_engines():
    """Defensive: missing engines.node is itself a fail (we want the pin explicit)."""
    with patch.object(su, "_node_version", return_value="20.11.1"):
        result, reqs = su.gate_node_version({"version": "1.0.0"})
    assert result.ok is False
    assert reqs[0].id == "node-version-unpinned"


# ── gate_stub_install ────────────────────────────────────────────────────────

def test_gate_stub_install_pass():
    metadata = {
        "version": "2026.4.15",
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {"unpackedSize": 5_000_000},
    }
    result, reqs = su.gate_stub_install(metadata, installed_version="2026.4.13")
    assert result.ok is True
    assert reqs == []


def test_gate_stub_install_fails_on_zero_version():
    """The 2026-04-24 fingerprint: openclaw@0.0.1 squat with no bin field."""
    metadata = {"version": "0.0.1", "bin": None, "dist": {"unpackedSize": 12_000}}
    result, reqs = su.gate_stub_install(metadata, installed_version="2026.4.13")
    assert result.ok is False
    assert reqs[0].id == "stub-install-pin-target"
    assert "2026.4.13" in reqs[0].remediation  # last good version surfaced


def test_gate_stub_install_fails_on_empty_bin():
    metadata = {"version": "2026.4.15", "bin": {}, "dist": {"unpackedSize": 5_000_000}}
    result, reqs = su.gate_stub_install(metadata, installed_version="2026.4.13")
    assert result.ok is False
    assert "bin field is missing or empty" in reqs[0].summary


def test_gate_stub_install_fails_on_tiny_tarball():
    metadata = {
        "version": "2026.4.15",
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {"unpackedSize": 50_000},  # 50 KB — way too small
    }
    result, reqs = su.gate_stub_install(metadata, installed_version="2026.4.13")
    assert result.ok is False
    assert "tarball is suspiciously small" in reqs[0].summary


# ── gate_user_launchagents (read-only sibling of _remove_conflicting_user_agents) ──

def test_gate_user_launchagents_pass(tmp_path, monkeypatch):
    """No orphan agents → gate passes."""
    monkeypatch.setattr(su, "scan_user_launchagents",
                        lambda network: {"scanned_users": ["team_bot_a", "admin_bot"], "found_agents": []})
    result, reqs = su.gate_user_launchagents({"members": ["team_bot_a", "admin_bot"]})
    assert result.ok is True
    assert reqs == []


def test_gate_user_launchagents_fails_with_orphans(monkeypatch):
    monkeypatch.setattr(su, "scan_user_launchagents",
                        lambda network: {
                            "scanned_users": ["team_bot_a", "admin_bot"],
                            "found_agents": [
                                {"bot_id": "team_bot_a", "user": "team_bot_a", "path": "/Users/team_bot_a/Library/LaunchAgents/ai.openclaw.gateway.plist"},
                            ],
                        })
    result, reqs = su.gate_user_launchagents({"members": ["team_bot_a", "admin_bot"]})
    assert result.ok is False
    assert reqs[0].id == "user-launchagents-cleanup"
    assert "team_bot_a" in reqs[0].summary


# ── gate_config_references ───────────────────────────────────────────────────

def _candidate_metadata(version: str = "2026.5.12") -> dict:
    """Synthetic registry metadata pointing at a dummy tarball URL.
    Tests monkeypatch _fetch_candidate_plugin_ids so the URL is never opened."""
    return {
        "version": version,
        "dist": {"tarball": f"https://registry.example/openclaw-{version}.tgz"},
    }


def _bot_cfg_enabling(*plugin_ids: str) -> dict:
    return {
        "plugins": {
            "entries": {pid: {"enabled": True} for pid in plugin_ids},
        },
    }


def test_enabled_plugin_refs_only_returns_enabled():
    cfg = {
        "plugins": {
            "entries": {
                "brave": {"enabled": True},
                "telegram": {"enabled": False},
                "google": {"enabled": True},
                "weird":   "not-a-dict",        # tolerate junk shapes
            },
        },
    }
    assert su._enabled_plugin_refs(cfg) == ["brave", "google"]


def test_enabled_plugin_refs_empty_when_no_plugins_block():
    assert su._enabled_plugin_refs({}) == []
    assert su._enabled_plugin_refs({"plugins": {}}) == []
    assert su._enabled_plugin_refs({"plugins": {"entries": "scalar"}}) == []


def test_enabled_plugin_refs_picks_up_channel_signal():
    """channels.<id>.enabled = true implies plugin <id> is needed, even with
    no plugins.entries record. This is the 2026-05-15 team_bot_c false-negative:
    team_bot_c had `channels.slack.enabled = true` but no `plugins.entries.slack`
    and the gate missed slack on team_bot_c."""
    cfg = {
        "plugins": {"entries": {"anthropic": {"enabled": True}}},
        "channels": {
            "slack":    {"enabled": True, "botToken": "xoxb-..."},
            "telegram": {"enabled": False},  # off — should not be flagged
        },
    }
    assert su._enabled_plugin_refs(cfg) == ["anthropic", "slack"]


def test_enabled_plugin_refs_picks_up_web_search_provider():
    """tools.web.search.provider names a plugin id."""
    cfg = {
        "plugins": {"entries": {"anthropic": {"enabled": True}}},
        "tools":   {"web": {"search": {"provider": "brave"}}},
    }
    assert su._enabled_plugin_refs(cfg) == ["anthropic", "brave"]


def test_enabled_plugin_refs_disabled_entry_overrides_channel_signal():
    """If the operator explicitly set `plugins.entries.slack.enabled = false`,
    that's a clear opt-out — we shouldn't flag slack as needed even if the
    channel config still has it enabled."""
    cfg = {
        "plugins": {"entries": {"slack": {"enabled": False}}},
        "channels": {"slack": {"enabled": True}},
    }
    assert su._enabled_plugin_refs(cfg) == []


def test_is_phantom_install_returns_false_when_dist_index_js_exists(tmp_path):
    """A normal compiled plugin install has dist/index.js → NOT phantom."""
    plugin = tmp_path / "real-plugin"
    (plugin / "dist").mkdir(parents=True)
    (plugin / "dist" / "index.js").write_text("module.exports = {};")
    assert su._is_phantom_install(str(plugin)) is False


def test_is_phantom_install_returns_true_for_ts_source_only(tmp_path):
    """The 2026.5.1-beta.1 brave-plugin failure mode: install path exists,
    has TS source + empty dist stamp, NO dist/index.js. The runtime
    refuses to load this → treat as phantom so the gate flags brave as
    missing despite the install record."""
    plugin = tmp_path / "phantom-plugin"
    (plugin / "dist").mkdir(parents=True)
    (plugin / "index.ts").write_text("export const x = 1;")
    (plugin / "src" / "lib.ts").parent.mkdir(parents=True, exist_ok=True)
    (plugin / "src" / "lib.ts").write_text("export const y = 2;")
    # dist contains only tsc build artifacts, no runtime entry
    (plugin / "dist" / ".boundary-tsc.stamp").write_text("")
    (plugin / "dist" / ".boundary-tsc.tsbuildinfo").write_text("{}")
    assert su._is_phantom_install(str(plugin)) is True


def test_is_phantom_install_returns_false_when_path_does_not_exist(tmp_path):
    """Missing install path is uncertain — could be just-installed and the
    next runtime read will populate it, or the operator may have manually
    cleaned up. Don't false-positive."""
    assert su._is_phantom_install(str(tmp_path / "does-not-exist")) is False


def test_is_phantom_install_handles_none_and_empty():
    assert su._is_phantom_install(None) is False
    assert su._is_phantom_install("") is False


def test_is_phantom_install_accepts_top_level_index_js(tmp_path):
    """Some plugins ship the entry at the package root, not under dist/."""
    plugin = tmp_path / "plugin-root-entry"
    plugin.mkdir()
    (plugin / "index.js").write_text("module.exports = {};")
    assert su._is_phantom_install(str(plugin)) is False


def test_load_path_plugin_ids_reads_manifests(tmp_path):
    """Each path in plugins.load.paths declares its plugin id in
    `openclaw.plugin.json` — that's the file Evolve's own plugin uses to
    declare id=evolve at /Users/Shared/evolve-plugin/."""
    plugin_dir = tmp_path / "evolve-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "openclaw.plugin.json").write_text('{"id": "evolve", "main": "dist/index.js"}')

    cfg = {"plugins": {"load": {"paths": [str(plugin_dir)]}}}
    assert su._load_path_plugin_ids(cfg) == {"evolve"}


def test_load_path_plugin_ids_tolerates_missing_paths(tmp_path):
    """A configured load path that doesn't exist on disk shouldn't crash
    the gate — the plugin might be uninstalled or the path might be a typo."""
    cfg = {"plugins": {"load": {"paths": [str(tmp_path / "does-not-exist")]}}}
    assert su._load_path_plugin_ids(cfg) == set()


def test_load_path_plugin_ids_handles_legacy_list_shape(tmp_path):
    """Some older configs may use `plugins.load` as a bare list of paths
    rather than the {paths: [...]} dict shape."""
    plugin_dir = tmp_path / "p"
    plugin_dir.mkdir()
    (plugin_dir / "openclaw.plugin.json").write_text('{"id": "evolve"}')
    cfg = {"plugins": {"load": [str(plugin_dir)]}}
    assert su._load_path_plugin_ids(cfg) == {"evolve"}


def _stub_bot_ids(monkeypatch, bot_ids: list[str]) -> None:
    """Patch `_bot_ids` on the *exact* ocadmin module the gate's relative
    import will resolve through.

    The conftest's `_restore_module_state` fixture restores sys.modules between
    tests but leaves dangling attribute references on the ``evolve_admin``
    package. As a result, both monkeypatch.setattr("evolve_admin.ocadmin...")
    (resolves via getattr on the package) and `from evolve_admin import
    ocadmin` (same) can land on a *different* module object than the one
    `from .ocadmin import _bot_ids` inside the gate resolves via sys.modules.
    Patching sys.modules['evolve_admin.ocadmin'] directly side-steps the
    divergence — that's the same lookup path the gate takes.
    """
    import importlib
    import sys
    importlib.import_module("evolve_admin.ocadmin")
    target = sys.modules["evolve_admin.ocadmin"]
    monkeypatch.setattr(target, "_bot_ids", lambda network: list(bot_ids))


def test_gate_config_references_pass_when_all_enabled_plugins_ship(monkeypatch):
    """Every plugin a bot has enabled exists in the candidate → gate passes."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"brave", "google", "anthropic"}, None),
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("brave", "anthropic"),
    )
    _stub_bot_ids(monkeypatch, ["team_bot_a", "admin_bot"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is True
    assert reqs == []
    assert "brave" in result.details["candidate_plugins"]
    assert result.details["missing_by_bot"] == {}


def test_gate_config_references_fails_when_enabled_plugin_dropped(monkeypatch):
    """The 2026-05-15 fingerprint: candidate drops `brave`, bots still enable it."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"google", "anthropic"}, None),  # no brave
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("brave", "anthropic"),
    )
    _stub_bot_ids(monkeypatch, ["team_bot_a", "admin_bot"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is False
    assert len(reqs) == 1
    assert reqs[0].id == "config-references-missing-plugins"
    assert reqs[0].blocking is True
    assert "brave" in reqs[0].summary
    assert "team_bot_a" in reqs[0].summary and "admin_bot" in reqs[0].summary
    assert result.details["missing_by_bot"] == {"team_bot_a": ["brave"], "admin_bot": ["brave"]}


def test_gate_config_references_does_not_flag_npm_installed_plugins(monkeypatch):
    """`@openclaw/slack` (npm-installed via `openclaw plugins install`) shows
    up in the bot's installs.json. The gate must merge that into the available
    set so the operator can keep slack working post-2026.5.12 by installing
    the externalized package."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),  # no slack in stock
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("slack", "anthropic"),
    )
    monkeypatch.setattr(
        su, "_installed_plugin_ids",
        lambda bot_id, network: {"slack"},  # bot has run `oc plugins install @openclaw/slack`
    )
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is True
    assert reqs == []
    team_bot_a = next(b for b in result.details["bots"] if b["bot_id"] == "team_bot_a")
    assert team_bot_a["installed_plugins"] == ["slack"]


def test_gate_config_references_remediation_leads_with_install_for_externalized(
    monkeypatch,
):
    """When the missing plugin is one openclaw externalized (slack/brave),
    the remediation must lead with `openclaw plugins install` per affected
    bot — the right fix in 99% of cases — before falling through to
    pin/migrate/disable."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),  # no slack in stock
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("slack"),
    )
    monkeypatch.setattr(su, "_installed_plugin_ids", lambda b, n: set())
    _stub_bot_ids(monkeypatch, ["team_bot_a", "admin_bot"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is False
    r = reqs[0]
    # Summary surfaces the externalization story so the operator isn't alarmed
    assert "externalized" in r.summary
    # Remediation has per-bot install commands using the correct npm package name
    assert "openclaw plugins install @openclaw/slack" in r.remediation
    # sudo invocations must pass -H so HOME is set to the target user's
    # home (otherwise npm tries to write its cache under the invoking
    # user's $HOME and EACCES's — 2026-05-15 live regression).
    assert "sudo -u team_bot_a -H" in r.remediation
    assert "sudo -u admin_bot -H" in r.remediation
    # Pin/migrate/disable are present as fallbacks, in that order
    pin_idx = r.remediation.index("(b) Pin")
    migrate_idx = r.remediation.index("(c) Migrate")
    disable_idx = r.remediation.index("(d) Only if")
    assert pin_idx < migrate_idx < disable_idx


def test_gate_config_references_uses_actual_macos_user_not_bot_id(monkeypatch):
    """Bot ids and macOS users diverge — team_bot_b (bot id) runs as personal_bot_user
    (macOS user) per the gateway plist. The remediation must call sudo
    with the actual user, not the bot id, otherwise it errors with
    'unknown user' (2026-05-15 regression caught on the test pod)."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("discord"),
    )
    monkeypatch.setattr(su, "_installed_plugin_ids", lambda b, n: set())
    _stub_bot_ids(monkeypatch, ["team_bot_b"])
    # The gate calls ocadmin._gateway_runtime_user(bot_id, network). Patch the same way
    # we patch _bot_ids — through sys.modules to dodge the conftest's
    # attribute-vs-modules divergence.
    import importlib, sys as _sys
    importlib.import_module("evolve_admin.ocadmin")
    monkeypatch.setattr(
        _sys.modules["evolve_admin.ocadmin"], "_gateway_runtime_user",
        lambda bot_id, network: "personal_bot_user" if bot_id == "team_bot_b" else bot_id,
    )

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    rem = reqs[0].remediation
    assert "sudo -u personal_bot_user -H openclaw plugins install @openclaw/discord" in rem
    # And not the buggy fallback that hardcodes bot_id
    assert "sudo -u team_bot_b " not in rem


def test_gate_config_references_emits_one_install_invocation_per_package(monkeypatch):
    """`openclaw plugins install` takes exactly one package per invocation
    (caught 2026-05-15: a space-joined `... install @openclaw/brave-plugin
    @openclaw/slack` errored with "too many arguments for 'install'").
    The remediation must emit one `install <pkg>` per (bot, package) pair
    rather than joining them with spaces."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("brave", "slack"),
    )
    monkeypatch.setattr(su, "_installed_plugin_ids", lambda b, n: set())
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    rem = reqs[0].remediation
    # Two install invocations, one per package — joined for sequential exec
    assert rem.count("openclaw plugins install ") == 2, \
        f"expected 2 install invocations, got rendered:\n{rem}"
    # The pathological space-joined shape that triggered the original bug
    # must not appear ("install @openclaw/foo @openclaw/bar" — two packages).
    import re as _re
    space_joined = _re.search(
        r"openclaw plugins install @openclaw/\S+ @openclaw/", rem,
    )
    assert space_joined is None, (
        f"found space-joined packages — `openclaw plugins install` takes "
        f"only one arg per call. Rendered:\n{rem}"
    )


def test_gate_config_references_does_not_flag_load_path_plugins(monkeypatch, tmp_path):
    """`evolve` is loaded from plugins.load.paths — NOT in openclaw's stock
    tarball, but available to the bot at runtime. The gate must not
    false-positive on this case (2026-05-15 regression caught in
    operator-facing report)."""
    plugin_dir = tmp_path / "evolve-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "openclaw.plugin.json").write_text('{"id": "evolve"}')

    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic", "google"}, None),  # no evolve
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: {
            "plugins": {
                "entries": {"evolve": {"enabled": True}, "anthropic": {"enabled": True}},
                "load": {"paths": [str(plugin_dir)]},
            },
        },
    )
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is True, f"evolve loaded from {plugin_dir} should not be flagged"
    assert reqs == []
    team_bot_a = next(b for b in result.details["bots"] if b["bot_id"] == "team_bot_a")
    assert team_bot_a["load_path_plugins"] == ["evolve"]


def test_gate_config_references_ignores_disabled_plugins(monkeypatch):
    """Disabled plugins don't need to exist — that's the whole point of disabling them."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"google", "anthropic"}, None),
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: {
            "plugins": {"entries": {
                "brave":    {"enabled": False},  # off — shouldn't be flagged
                "anthropic": {"enabled": True},
            }},
        },
    )
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is True
    assert reqs == []


def test_gate_config_references_blocks_when_tarball_fetch_fails(monkeypatch):
    """Fetch failure must block — we'd rather refuse the upgrade than silent-pass."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: (None, "tarball fetch failed: connection refused"),
    )
    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is False
    assert reqs[0].id == "config-references-tarball-fetch"
    assert reqs[0].blocking is True
    assert "connection refused" in reqs[0].summary


def test_gate_config_references_blocks_on_unreadable_bot_config(monkeypatch):
    """If we can't read a bot's openclaw.json we can't certify anything — block."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),
    )
    monkeypatch.setattr(su, "_read_bot_openclaw_json", lambda b, n: None)
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    assert result.ok is False
    assert any(r.id == "config-references-unreadable" for r in reqs)
    assert result.details["unreadable_bots"] == ["team_bot_a"]


# ── install registry reader: sqlite-first (the OC 2026.6.x migration) ────────
#
# OpenClaw moved plugin install records from a flat ~/.openclaw/plugins/
# installs.json to a SQLite table (state/openclaw.sqlite →
# installed_plugin_index), leaving the JSON behind renamed to
# installs.json.migrated where it goes stale. The reader had been pinned to
# the old path, so it saw zero installed plugins on every bot and produced a
# false "plugin missing → NOT SAFE" blocker on the 2026.6.1 → 2026.6.5 check.

def _make_state_sqlite(path: Path, records: dict, *, generated_at_ms: int = 1) -> None:
    """Build a minimal OpenClaw state DB with one installed_plugin_index row,
    mirroring the columns the reader selects."""
    import sqlite3 as _sqlite
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE installed_plugin_index ("
            " index_key TEXT NOT NULL PRIMARY KEY,"
            " version INTEGER NOT NULL,"
            " host_contract_version TEXT NOT NULL,"
            " compat_registry_version TEXT NOT NULL,"
            " migration_version INTEGER NOT NULL,"
            " policy_hash TEXT NOT NULL,"
            " generated_at_ms INTEGER NOT NULL,"
            " refresh_reason TEXT,"
            " install_records_json TEXT NOT NULL,"
            " plugins_json TEXT NOT NULL,"
            " diagnostics_json TEXT NOT NULL,"
            " warning TEXT,"
            " updated_at_ms INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO installed_plugin_index VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"row-{generated_at_ms}", 1, "2026.6.1", "abc", 1, "h",
             generated_at_ms, "migration", json.dumps(records),
             "{}", "{}", None, generated_at_ms),
        )
        conn.commit()
    finally:
        conn.close()


def test_read_installs_from_sqlite_returns_records(tmp_path):
    """Pull records from installed_plugin_index.install_records_json and
    normalize to the legacy {"installRecords": ...} shape."""
    db = tmp_path / "openclaw.sqlite"
    _make_state_sqlite(db, {
        "brave": {"installPath": "/x/brave", "source": "npm", "version": "2026.6.1"},
    })
    data = su._read_installs_from_sqlite(db)
    assert data is not None
    assert data["installRecords"]["brave"]["version"] == "2026.6.1"
    assert data["_source"] == "state/openclaw.sqlite"


def test_read_installs_from_sqlite_picks_newest_row(tmp_path):
    """Several rows can accumulate; the reader takes the newest by
    generated_at_ms (the live one)."""
    db = tmp_path / "openclaw.sqlite"
    _make_state_sqlite(db, {"brave": {"version": "old"}}, generated_at_ms=100)
    import sqlite3 as _sqlite
    conn = _sqlite.connect(str(db))
    conn.execute(
        "INSERT INTO installed_plugin_index VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("newer", 1, "2026.6.1", "abc", 1, "h", 200, "refresh",
         json.dumps({"brave": {"version": "new"}}), "{}", "{}", None, 200),
    )
    conn.commit()
    conn.close()
    data = su._read_installs_from_sqlite(db)
    assert data["installRecords"]["brave"]["version"] == "new"


def test_read_installs_from_sqlite_missing_or_garbage_returns_none(tmp_path):
    assert su._read_installs_from_sqlite(tmp_path / "nope.sqlite") is None
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_text("definitely not a sqlite db")
    assert su._read_installs_from_sqlite(garbage) is None


def _point_registry_paths(monkeypatch, *, sqlite=None, legacy=None, migrated=None):
    """Redirect the three registry-path helpers at test fixtures."""
    monkeypatch.setattr(su, "_installs_sqlite_path",
                        lambda user: sqlite if sqlite else Path("/nonexistent/x.sqlite"))
    monkeypatch.setattr(su, "_installs_json_path",
                        lambda user: legacy if legacy else Path("/nonexistent/x.json"))
    monkeypatch.setattr(su, "_installs_json_migrated_path",
                        lambda user: migrated if migrated else Path("/nonexistent/x.migrated"))


def test_read_installs_json_prefers_sqlite_over_stale_legacy(tmp_path, monkeypatch):
    """When both the live DB and the (now-stale) installs.json exist, the
    SQLite source wins."""
    db = tmp_path / "openclaw.sqlite"
    _make_state_sqlite(db, {"brave": {"version": "from-sqlite"}})
    legacy = tmp_path / "installs.json"
    legacy.write_text(json.dumps({"installRecords": {"brave": {"version": "from-json"}}}))
    _point_registry_paths(monkeypatch, sqlite=db, legacy=legacy)
    data = su._read_installs_json("bot")
    assert data["installRecords"]["brave"]["version"] == "from-sqlite"


def test_read_installs_json_falls_back_to_legacy_json(tmp_path, monkeypatch):
    """Pre-6.x pods keep the flat installs.json; the reader still finds it."""
    legacy = tmp_path / "installs.json"
    legacy.write_text(json.dumps({"installRecords": {"slack": {"version": "5.18"}}}))
    _point_registry_paths(monkeypatch, legacy=legacy)
    data = su._read_installs_json("bot")
    assert data["installRecords"]["slack"]["version"] == "5.18"


def test_install_state_determinable_true_when_registry_readable(tmp_path, monkeypatch):
    db = tmp_path / "openclaw.sqlite"
    _make_state_sqlite(db, {"brave": {"version": "x"}})
    _point_registry_paths(monkeypatch, sqlite=db)
    _stub_bot_user(monkeypatch)
    assert su._install_state_determinable("bot", {}) is True


def test_install_state_determinable_true_when_no_registry_at_all(tmp_path, monkeypatch):
    """No registry file = a bot that never installed a plugin = determinate
    empty. A reference to a dropped plugin here IS a real blocker."""
    _point_registry_paths(monkeypatch)  # all nonexistent
    _stub_bot_user(monkeypatch)
    assert su._install_state_determinable("bot", {}) is True


def test_install_state_determinable_false_when_registry_present_but_unreadable(
    tmp_path, monkeypatch,
):
    """A registry IS present but unparseable → install state UNKNOWN. This is
    the signature of OpenClaw changing its on-disk layout under us; the gate
    must fail safe (warn, not block)."""
    bad = tmp_path / "openclaw.sqlite"
    bad.write_text("not a sqlite db")  # present, but no readable records
    _point_registry_paths(monkeypatch, sqlite=bad)
    _stub_bot_user(monkeypatch)
    assert su._install_state_determinable("bot", {}) is False


def _stub_bot_user(monkeypatch, user: str = "bot") -> None:
    import importlib, sys as _sys
    importlib.import_module("evolve_admin.ocadmin")
    monkeypatch.setattr(
        _sys.modules["evolve_admin.ocadmin"], "_gateway_runtime_user",
        lambda bot_id, network: user,
    )


def test_gate_config_references_warns_not_blocks_when_install_state_unknown(monkeypatch):
    """Process fail-safe (the heart of the 2026.6.1→2026.6.5 false positive):
    if a bot's install registry exists but can't be read in a recognized
    format, the gate must NOT hard-block — it downgrades the would-be-missing
    plugin to a NON-blocking warning so an unrecognized OpenClaw registry
    migration fails safe instead of blocking the fleet on a phantom finding."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),  # brave dropped from stock
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("brave", "anthropic"),
    )
    monkeypatch.setattr(su, "_installed_plugin_ids", lambda b, n: set())
    monkeypatch.setattr(su, "_install_state_determinable", lambda b, n: False)
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    # No hard blocker — every requirement is advisory
    assert all(r.blocking is False for r in reqs)
    ids = {r.id for r in reqs}
    assert "config-references-install-state-unknown" in ids
    assert "config-references-missing-plugins" not in ids
    warn = next(r for r in reqs if r.id == "config-references-install-state-unknown")
    assert "team_bot_a" in warn.summary and "brave" in warn.summary
    assert result.details["unknown_by_bot"] == {"team_bot_a": ["brave"]}
    assert result.details["missing_by_bot"] == {}


def test_gate_config_references_still_blocks_when_state_readable_and_missing(monkeypatch):
    """Don't over-correct: when install state IS readable and the plugin is
    genuinely absent, the gate still hard-blocks (the real 2026-05-15
    failure mode the gate exists for)."""
    monkeypatch.setattr(
        su, "_fetch_candidate_plugin_ids",
        lambda metadata, timeout=30: ({"anthropic"}, None),
    )
    monkeypatch.setattr(
        su, "_read_bot_openclaw_json",
        lambda bot_id, network: _bot_cfg_enabling("brave", "anthropic"),
    )
    monkeypatch.setattr(su, "_installed_plugin_ids", lambda b, n: set())
    monkeypatch.setattr(su, "_install_state_determinable", lambda b, n: True)
    _stub_bot_ids(monkeypatch, ["team_bot_a"])

    result, reqs = su.gate_config_references(_candidate_metadata(), {})
    blockers = [r for r in reqs if r.blocking]
    assert len(blockers) == 1
    assert blockers[0].id == "config-references-missing-plugins"
    assert result.details["unknown_by_bot"] == {}


def test_fetch_candidate_plugin_ids_parses_tarball_layout():
    """End-to-end check of the tarball parser against a synthetic in-memory .tgz."""
    import gzip
    import io as _io
    import tarfile as _tar

    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w:gz") as tf:
        # Real npm tarballs prefix every entry with 'package/'; we only care
        # about top-level dirs under package/dist/extensions/.
        for path in [
            "package/package.json",
            "package/dist/extensions/brave/index.js",
            "package/dist/extensions/google/index.js",
            "package/dist/extensions/anthropic/sub/nested.js",
            "package/dist/other-thing/foo.js",   # shouldn't be picked up
        ]:
            info = _tar.TarInfo(name=path)
            info.size = 0
            tf.addfile(info, _io.BytesIO(b""))
    data = buf.getvalue()

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._p

    with patch("urllib.request.urlopen", return_value=FakeResp(data)):
        ids, err = su._fetch_candidate_plugin_ids(
            {"dist": {"tarball": "https://example/foo.tgz"}},
        )
    assert err is None
    assert ids == {"brave", "google", "anthropic"}


# ── gate_plist_paths ─────────────────────────────────────────────────────────

def test_gate_plist_paths_with_empty_bot_list():
    """Empty bot list → trivially passes (no plists to inspect)."""
    result, reqs = su.gate_plist_paths({"members": [], "bots": {}})
    assert result.ok is True
    assert reqs == []


# ── gate_plist_paths: Linux systemd-unit routing (Blocker 1) ─────────────────
#
# On Linux the gateways are systemd units, not launchd plists. The pre-fix
# gate checked /Library/LaunchDaemons/<label>.plist — paths that never exist
# on Linux — so a HEALTHY fresh Linux pod reported "Gateway plists … reference
# paths that don't exist on disk" as a hard blocker. The fix routes the check
# through the platform-profile seam: macOS keeps the plist-path check; Linux
# verifies the systemd unit exists/loaded via the Scheduler seam.


class _StubScheduler:
    """Minimal Scheduler stand-in: returns a canned ``status()`` dict per
    label so a test can pin the installed / managed / status_error tri-state
    independently (FakeScheduler ties installed and managed together)."""

    def __init__(self, statuses: dict[str, dict]):
        self._statuses = statuses

    def status(self, label: str, **_kw) -> dict:
        return self._statuses.get(
            label,
            {"installed": False, "managed": False, "running": False,
             "pid": None, "last_exit": None, "status_error": None},
        )


def _with_linux_scheduler(statuses: dict[str, dict]):
    """Context-manager-ish helper: pin the LINUX profile + inject a stub
    scheduler, returning a cleanup callable."""
    import platform_profile
    from evolve_admin.runtime import set_scheduler

    platform_profile.set_profile(platform_profile.LINUX)
    set_scheduler(_StubScheduler(statuses))

    def _cleanup():
        platform_profile.set_profile(None)
        set_scheduler(None)

    return _cleanup


def test_gate_gateway_units_passes_when_units_loaded():
    """Blocker-1 happy path: under the Linux profile, gateway units that exist
    and are loaded pass — NO false "plist paths don't exist" blocker."""
    cleanup = _with_linux_scheduler({
        "ai.openclaw.evo-gateway": {"installed": True, "managed": True, "status_error": None},
        "ai.openclaw.darwin-gateway": {"installed": True, "managed": True, "status_error": None},
    })
    try:
        result, reqs = su.gate_plist_paths({"bots": {"evo": {}, "darwin": {}}})
        assert result.ok is True
        assert reqs == []
        # Detail records the systemd-unit shape, not plist paths.
        assert result.details["missing_units"] == []
        labels = {e["label"] for e in result.details["inspected"]}
        assert labels == {"ai.openclaw.evo-gateway", "ai.openclaw.darwin-gateway"}
    finally:
        cleanup()


def test_gate_gateway_units_blocks_when_unit_file_absent():
    """A genuinely absent unit file (sudo-free Path.exists() = False) is a
    determinate finding → blocking `regenerate-gateway-units` requirement."""
    cleanup = _with_linux_scheduler({
        "ai.openclaw.evo-gateway": {"installed": True, "managed": True, "status_error": None},
        "ai.openclaw.darwin-gateway": {"installed": False, "managed": False, "status_error": None},
    })
    try:
        result, reqs = su.gate_plist_paths({"bots": {"evo": {}, "darwin": {}}})
        assert result.ok is False
        assert len(reqs) == 1
        assert reqs[0].id == "regenerate-gateway-units"
        assert reqs[0].blocking is True
        assert reqs[0].source_gate == "plist_paths"
        assert "ai.openclaw.darwin-gateway" in reqs[0].summary
        assert "darwin" in reqs[0].remediation
    finally:
        cleanup()


def test_gate_gateway_units_blocks_when_unit_on_disk_but_not_loaded():
    """Unit file present but systemd doesn't know it (needs daemon-reload) is
    a conclusive finding → blocking, distinct from the inconclusive case."""
    cleanup = _with_linux_scheduler({
        "ai.openclaw.evo-gateway": {"installed": True, "managed": False, "status_error": None},
    })
    try:
        result, reqs = su.gate_plist_paths({"bots": {"evo": {}}})
        assert result.ok is False
        assert len(reqs) == 1
        assert reqs[0].id == "regenerate-gateway-units"
        assert reqs[0].blocking is True
    finally:
        cleanup()


def test_gate_gateway_units_unconfirmed_load_state_passes_without_blocker():
    """Unit file on disk but the load-state probe couldn't escalate
    (status_error) → PASS with no requirement. The unit file existing is the
    gateway-existence guarantee; a missing diagnostic capability must not gate
    the upgrade (#3188 principle), and the daemon hits this on every probe so
    a per-run requirement would be permanent noise. The degraded state is
    recorded in details for legibility."""
    cleanup = _with_linux_scheduler({
        "ai.openclaw.evo-gateway": {
            "installed": True, "managed": False, "status_error": "cannot_escalate",
        },
    })
    try:
        result, reqs = su.gate_plist_paths({"bots": {"evo": {}}})
        assert result.ok is True  # file present + unprobeable load state ≠ failure
        assert reqs == []          # no per-run requirement noise
        # The degraded probe is still legible in the persisted detail.
        assert result.details["load_unconfirmed_units"] == ["ai.openclaw.evo-gateway"]
        assert result.details["missing_units"] == []
        entry = result.details["inspected"][0]
        assert entry["status_error"] == "cannot_escalate"
        assert "load state unconfirmed" in entry["note"]
    finally:
        cleanup()


def test_gate_plist_paths_macos_still_uses_plist_check(monkeypatch, tmp_path):
    """macOS path unchanged: under the macOS profile the gate reads launchd
    plists from the profile's daemon_dir (NOT the scheduler seam) and flags a
    missing plist as `regenerate-gateway-plists`."""
    import platform_profile
    from dataclasses import replace

    # Point daemon_dir at an empty tmp dir so the plist is "missing".
    macos = replace(platform_profile.MACOS, daemon_dir=str(tmp_path))
    platform_profile.set_profile(macos)
    # If the macOS branch wrongly consulted the scheduler, this stub would
    # make every unit look healthy and the test would (wrongly) pass green.
    from evolve_admin.runtime import set_scheduler
    set_scheduler(_StubScheduler({
        "ai.openclaw.evo-gateway": {"installed": True, "managed": True, "status_error": None},
    }))
    try:
        result, reqs = su.gate_plist_paths({"bots": {"evo": {}}})
        assert result.ok is False
        assert len(reqs) == 1
        assert reqs[0].id == "regenerate-gateway-plists"  # plist check, not unit check
        assert reqs[0].blocking is True
    finally:
        platform_profile.set_profile(None)
        set_scheduler(None)


# ── gate_port_owners ─────────────────────────────────────────────────────────

def test_gate_port_owners_with_empty_bot_list():
    result, reqs = su.gate_port_owners({"members": [], "bots": {}})
    assert result.ok is True
    assert reqs == []


def _network_with_bots(*bot_ids: str) -> dict:
    return {"members": list(bot_ids),
            "bots": {b: {"user": b, "port": 18000 + i} for i, b in enumerate(bot_ids)}}


def _fake_lsof(stdout: str, returncode: int = 0):
    """Build a fake subprocess.run that returns canned lsof output."""
    from types import SimpleNamespace
    def runner(*a, **kw):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return runner


def _patch_port_owner_helpers(monkeypatch, *, bot_id="team_bot_a", port=5050, expected_user="root"):
    """Stub out the network-shape helpers so gate_port_owners only needs subprocess.run mocked.

    Patches at the modules where the helpers are defined (gate_port_owners
    re-imports them lazily inside the function).
    """
    from evolve_admin import config as config_mod
    from evolve_admin import ocadmin as ocadmin_mod
    monkeypatch.setattr(ocadmin_mod, "_bot_ids", lambda network: [bot_id])
    monkeypatch.setattr(ocadmin_mod, "_gateway_runtime_user", lambda b, network: expected_user)
    monkeypatch.setattr(config_mod, "get_bot_port", lambda b, network: port)


def test_gate_port_owners_invokes_lsof_via_sudo(monkeypatch):
    """Regression: must invoke lsof via `sudo -n /usr/sbin/lsof` so the
    probe can see TCP sockets owned by other users.

    The admin server runs as the `evolve` user. macOS lsof from a non-root
    user only returns the calling user's sockets — bot gateways listen as
    per-bot users (team_bot_a, team_bot_c, admin_bot, …), so an unprivileged probe sees
    nothing and the gate falsely reports every gateway as 'not running'.
    The sudoers grant in /etc/sudoers.d/evolve permits this exact lsof
    invocation; without it the probe surfaces a `port-owners-probe-error`.
    """
    import platform_profile

    platform_profile.set_profile(platform_profile.MACOS)
    try:
        _patch_port_owner_helpers(monkeypatch)
        captured: dict[str, list] = {}

        class FakeCompleted:
            returncode = 0
            stdout = "p1234\ncopenclaw\nu0\nn*:5050\n"
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = args
            return FakeCompleted()

        monkeypatch.setattr(su.subprocess, "run", fake_run)
        su.gate_port_owners({"bots": {"team_bot_a": {}}})

        args = captured["args"]
        assert args[:3] == ["sudo", "-n", "/usr/sbin/lsof"], (
            f"gate must call `sudo -n /usr/sbin/lsof`, got {args[:3]!r}"
        )
    finally:
        platform_profile.set_profile(None)


def test_gate_port_owners_lsof_path_derives_from_profile_on_linux(monkeypatch):
    """Blocker-2 regression: the lsof binary in the probe argv must come from
    ``get_profile().lsof`` so it matches the rendered sudoers grant (which
    derives the same path from the same profile table). The pre-fix code
    hardcoded ``/usr/sbin/lsof`` (the macOS path); on a Linux pod the grant
    covers ``/usr/bin/lsof``, so ``sudo -n /usr/sbin/lsof`` did not match and
    failed with "a password is required" — the false blocker on every Linux pod.
    """
    import platform_profile

    platform_profile.set_profile(platform_profile.LINUX)
    try:
        _patch_port_owner_helpers(monkeypatch)
        captured: dict[str, list] = {}

        class FakeCompleted:
            returncode = 0
            stdout = "p1234\ncopenclaw\nu0\nn*:5050\n"
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = args
            return FakeCompleted()

        monkeypatch.setattr(su.subprocess, "run", fake_run)
        su.gate_port_owners({"bots": {"team_bot_a": {}}})

        args = captured["args"]
        assert args[:3] == ["sudo", "-n", "/usr/bin/lsof"], (
            f"on Linux the gate must call `sudo -n /usr/bin/lsof` (the profile "
            f"path that the grant covers), got {args[:3]!r}"
        )
        assert args[2] == platform_profile.LINUX.lsof
    finally:
        platform_profile.set_profile(None)


def test_gate_port_owners_surfaces_sudo_grant_missing_as_probe_error(monkeypatch):
    """If the sudoers grant for lsof isn't installed, sudo -n fails with
    `sudo: a password is required` on stderr. The gate must surface this
    as `port-owners-probe-error` (with refresh-sudoers remediation), NOT
    misclassify it as `port-owners-no-listener` — otherwise operators would
    chase a phantom gateway-down problem.

    Crucially the probe error is INCONCLUSIVE, not a blocker: a missing
    diagnostic capability must not gate the upgrade (the false-blocker that
    made a healthy fresh Linux pod report "Evolve needs work first"). The
    gate stays green (ok=True) and the requirement is non-blocking.
    """
    _patch_port_owner_helpers(monkeypatch, expected_user="team_bot_a")

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required\n"

    monkeypatch.setattr(su.subprocess, "run", lambda *a, **kw: FakeCompleted())
    result, reqs = su.gate_port_owners({"bots": {"team_bot_a": {}}})

    assert result.ok is True  # inconclusive, not a failure
    assert len(reqs) == 1
    assert reqs[0].id == "port-owners-probe-error"
    assert reqs[0].blocking is False  # a missing diagnostic must not block
    assert "sudo lsof failed" in reqs[0].summary
    assert "refresh-sudoers" in reqs[0].remediation


def test_gate_port_owners_pass_when_listener_user_matches(monkeypatch):
    """Happy path: lsof reports a listener whose UID resolves to the
    expected bot user → GateResult(ok=True), no requirements.

    Uses UID 0 (root) which resolves consistently on every Unix system,
    avoiding the need to mock pwd.getpwuid.
    """
    _patch_port_owner_helpers(monkeypatch, expected_user="root")

    class FakeCompleted:
        returncode = 0
        stdout = "p1234\ncopenclaw\nu0\nn*:5050\n"  # u0 → 'root'
        stderr = ""

    monkeypatch.setattr(su.subprocess, "run", lambda *a, **kw: FakeCompleted())
    result, reqs = su.gate_port_owners({"bots": {"team_bot_a": {}}})

    assert result.ok is True
    assert reqs == []
    assert result.details["ports"][0]["listener_user"] == "root"
    assert result.details["ports"][0]["listener_pid"] == "1234"
    assert result.details["ports"][0]["ok"] is True


def test_gate_port_owners_fails_on_user_mismatch(monkeypatch):
    """Failure path: listener UID resolves to a different user than expected
    → GateResult(ok=False) with a `port-owners-mismatch` blocking requirement.
    """
    # Expect bot to run as 'team_bot_a', but listener is uid 0 ('root') — mismatch.
    _patch_port_owner_helpers(monkeypatch, bot_id="team_bot_a", expected_user="team_bot_a")

    class FakeCompleted:
        returncode = 0
        stdout = "p1234\ncopenclaw\nu0\nn*:5050\n"  # u0 → 'root' ≠ 'team_bot_a'
        stderr = ""

    monkeypatch.setattr(su.subprocess, "run", lambda *a, **kw: FakeCompleted())
    result, reqs = su.gate_port_owners({"bots": {"team_bot_a": {}}})

    assert result.ok is False
    assert len(reqs) == 1
    assert reqs[0].id == "port-owners-mismatch"
    assert reqs[0].blocking is True
    assert reqs[0].source_gate == "port_owners"
    assert "team_bot_a" in reqs[0].summary
    # Per-bot detail records the actual vs expected for triage
    info = result.details["ports"][0]
    assert info["ok"] is False
    assert info["expected_user"] == "team_bot_a"
    assert info["listener_user"] == "root"


def test_gate_port_owners_tolerates_lsof_missing(monkeypatch):
    """Tolerance path: if subprocess.run raises FileNotFoundError (binary
    missing), the gate must NOT crash. It surfaces the failure as a blocking
    requirement so the operator sees real signal instead of a 500.

    This is the symptom that motivated PR #633: on the mini the bare-`lsof`
    invocation raised FileNotFoundError, which propagated as a generic OSError
    and the gate produced a misleading 'unexpected ownership' message.

    Probe failures (binary missing, lsof error, etc.) are emitted as
    `port-owners-probe-error` — distinct from the no-listener and
    wrong-owner cases, so the remediation matches the actual cause.
    """
    _patch_port_owner_helpers(monkeypatch, bot_id="team_bot_a", expected_user="team_bot_a")

    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory", "/usr/sbin/lsof")

    monkeypatch.setattr(su.subprocess, "run", boom)

    # Must not raise — the gate is supposed to degrade gracefully.
    result, reqs = su.gate_port_owners({"bots": {"team_bot_a": {}}})

    # A binary-missing probe is inconclusive, not a blocker: the gate stays
    # green and the requirement is non-blocking (degraded note only).
    assert result.ok is True
    assert len(reqs) == 1
    assert reqs[0].id == "port-owners-probe-error"
    assert reqs[0].blocking is False
    # Operator should see the actual error in the summary, not a misleading
    # 'wrong user' claim.
    assert "lsof failed" in reqs[0].summary
    info = result.details["ports"][0]
    assert info["ok"] is False
    assert info["error"] is not None and "lsof failed" in info["error"]


def test_gate_port_owners_no_listener_emits_restart_remediation(monkeypatch):
    """The 2026-05-03 fingerprint: gateways stopped on the mini. The gate
    must direct the operator to `restart-gateways`, not 'investigate stray
    processes' (there are none)."""
    monkeypatch.setattr(su.subprocess, "run", _fake_lsof("", returncode=1))
    # _bot_user reads /Library/LaunchDaemons; force network-config fallback
    monkeypatch.setattr("evolve_admin.ocadmin._gateway_runtime_user",
                        lambda bot_id, network: bot_id)

    result, reqs = su.gate_port_owners(_network_with_bots("team_bot_a", "admin_bot"))

    assert result.ok is False
    assert len(reqs) == 1
    req = reqs[0]
    assert req.id == "port-owners-no-listener"
    assert "Gateway not running" in req.summary
    assert "team_bot_a" in req.summary and "admin_bot" in req.summary
    assert "restart-gateways team_bot_a,admin_bot" in req.remediation
    assert "deploy --bot=team_bot_a,admin_bot" in req.remediation


def test_gate_port_owners_wrong_owner_keeps_stray_process_remediation(monkeypatch):
    """When a real listener exists but is owned by an unexpected user,
    the gate should keep pointing at `menu processes` for investigation."""
    # lsof prints PID, command, user-uid, name in -F format
    fake_out = "p4242\ncgateway\nu0\nn*:18000\n"  # uid 0 = root
    monkeypatch.setattr(su.subprocess, "run", _fake_lsof(fake_out))
    monkeypatch.setattr("evolve_admin.ocadmin._gateway_runtime_user",
                        lambda bot_id, network: bot_id)  # expect 'team_bot_a', listener is root

    result, reqs = su.gate_port_owners(_network_with_bots("team_bot_a"))

    assert result.ok is False
    assert len(reqs) == 1
    req = reqs[0]
    assert req.id == "port-owners-mismatch"
    assert "owned by unexpected user" in req.summary
    assert "menu processes" in req.remediation


def test_gate_port_owners_separates_no_listener_from_wrong_owner(monkeypatch):
    """Mixed pod state: one bot's gateway is stopped, another is misowned.
    Both Requirements should be emitted with their own remediation."""
    fake_out = "p4242\ncgateway\nu0\nn*:18001\n"  # admin_bot's port → root

    def runner(cmd, *a, **kw):
        from types import SimpleNamespace
        # cmd contains '-iTCP:<port>'; route per-port
        port_arg = next(x for x in cmd if isinstance(x, str) and x.startswith("-iTCP:"))
        port = port_arg.split(":")[1]
        if port == "18000":  # team_bot_a → no listener
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if port == "18001":  # admin_bot → wrong owner
            return SimpleNamespace(returncode=0, stdout=fake_out, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(su.subprocess, "run", runner)
    monkeypatch.setattr("evolve_admin.ocadmin._gateway_runtime_user",
                        lambda bot_id, network: bot_id)

    result, reqs = su.gate_port_owners(_network_with_bots("team_bot_a", "admin_bot"))

    assert result.ok is False
    ids = {r.id for r in reqs}
    assert ids == {"port-owners-no-listener", "port-owners-mismatch"}


# ── Report persistence ──────────────────────────────────────────────────────

def test_report_written_with_symlink(tmp_path):
    """run_preflight writes <id>.json and updates latest.json."""
    fake_metadata = {
        "version": "2026.4.15",
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {
            "unpackedSize": 5_000_000,
            "tarball": "https://registry.example/openclaw-2026.4.15.tgz",
        },
        "engines": {"node": ">=18"},
    }
    with patch.object(su, "_fetch_registry_metadata", return_value=fake_metadata), \
         patch.object(su, "_node_version", return_value="20.11.1"), \
         patch.object(su, "_installed_version", return_value="2026.4.13"), \
         patch.object(su, "_fetch_candidate_plugin_ids", return_value=(set(), None)), \
         patch.object(su, "gate_send_surface",
                      return_value=(su.GateResult(ok=True, details={"status": "ok"}), [])):
        report = su.run_preflight(
            target_spec="2026.4.15",
            network={"members": [], "bots": {}},
            shared_dir=tmp_path,
        )

    root = tmp_path / "safe-upgrade" / "reports"
    report_file = root / f"{report.report_id}.json"
    assert report_file.exists(), "report file not written"

    data = json.loads(report_file.read_text())
    assert data["ok"] is True
    assert data["candidate"]["resolved_version"] == "2026.4.15"
    assert data["current"]["installed_version"] == "2026.4.13"

    latest = root / "latest.json"
    assert latest.exists(), "latest.json not created"
    latest_data = json.loads(latest.read_text())
    assert latest_data["report_id"] == report.report_id


def test_retention_keeps_only_20_most_recent(tmp_path):
    """Janitor sweep should keep at most 20 reports."""
    root = tmp_path / "safe-upgrade" / "reports"
    root.mkdir(parents=True)
    # Pre-seed 25 fake reports with sortable names
    for i in range(25):
        (root / f"20260101T000000Z-{i:08x}.json").write_text("{}")
    su._retention_sweep(root, keep=20)
    surviving = sorted(p.name for p in root.glob("*.json"))
    assert len(surviving) == 20
    # Should be the 20 lexicographically-largest names (most recent by sortable id)
    assert surviving[0] == "20260101T000000Z-00000005.json"


def test_load_latest_report_falls_back_to_newest_file(tmp_path):
    """If latest.json is missing, load_latest_report uses the newest file."""
    root = tmp_path / "safe-upgrade" / "reports"
    root.mkdir(parents=True)
    (root / "20260101T000000Z-aaaaaaaa.json").write_text(json.dumps({"report_id": "old"}))
    (root / "20260201T000000Z-bbbbbbbb.json").write_text(json.dumps({"report_id": "new"}))
    data = su.load_latest_report(shared_dir=tmp_path)
    assert data["report_id"] == "new"


# ── Concurrency: in-flight check ─────────────────────────────────────────────

def test_concurrent_check_returns_inflight_id(tmp_path, monkeypatch):
    """A second start_background_check while one is running returns the same id."""
    import threading

    barrier = threading.Event()
    release = threading.Event()

    fake_metadata = {
        "version": "2026.4.15",
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {"unpackedSize": 5_000_000},
        "engines": {"node": ">=18"},
    }

    def slow_fetch(*a, **kw):
        barrier.set()
        release.wait(timeout=5)
        return fake_metadata

    monkeypatch.setattr(su, "_fetch_registry_metadata", slow_fetch)
    monkeypatch.setattr(su, "_node_version", lambda: "20.11.1")
    monkeypatch.setattr(su, "_installed_version", lambda: "2026.4.13")
    monkeypatch.setattr(
        su, "gate_send_surface",
        lambda: (su.GateResult(ok=True, details={"status": "ok"}), []),
    )

    rid1, status1 = su.start_background_check(
        "latest", network={"members": [], "bots": {}}, shared_dir=tmp_path,
    )
    assert status1 == "running"
    assert barrier.wait(timeout=5), "first check never started"

    rid2, status2 = su.start_background_check(
        "latest", network={"members": [], "bots": {}}, shared_dir=tmp_path,
    )
    assert (rid2, status2) == (rid1, "running"), \
        "second start while one in-flight should return same id"

    release.set()
    # Let the worker finish; clean up state for other tests
    import time
    deadline = time.monotonic() + 5
    while su.inflight_report_id() is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert su.inflight_report_id() is None


# ── End-to-end smoke: run_preflight with mocked registry ────────────────────

def test_run_preflight_unsafe_path(tmp_path):
    """A stub-shaped registry payload should fail the stub_install gate and
    surface a blocker requirement, with overall ok=False."""
    stub_metadata = {
        "version": "0.0.1",  # fingerprint of the 2026-04-24 squat
        "bin": None,
        "dist": {"unpackedSize": 8_000},
        "engines": {"node": ">=18"},
    }
    with patch.object(su, "_fetch_registry_metadata", return_value=stub_metadata), \
         patch.object(su, "_node_version", return_value="20.11.1"), \
         patch.object(su, "_installed_version", return_value="2026.4.13"), \
         patch.object(su, "gate_send_surface",
                      return_value=(su.GateResult(ok=True, details={"status": "ok"}), [])):
        report = su.run_preflight(
            target_spec="latest",
            network={"members": [], "bots": {}},
            shared_dir=tmp_path,
        )

    assert report.ok is False
    assert any(r.source_gate == "stub_install" and r.blocking for r in report.requirements)
    assert "blocker" in report.summary

    # node_version still passes — gates are independent
    assert report.gates["node_version"].ok is True


# ── gate_send_surface (the 2026-06-11 delivery-P0 guard) ────────────────────

_SEND_HELP_OK = """Usage: openclaw message send [options]

Options:
  --channel <name>     channel to send on
  --target <id>        recipient id (use --target=<id> for negative ids)
  --message <text>     message text
  --json               machine-readable output
  --media <path>       attach media
"""


def test_probe_send_surface_ok_checks_contract_flags():
    calls = []

    def runner(argv, env):
        calls.append((argv, env))
        return 0, _SEND_HELP_OK, ""

    probe = su.probe_send_surface(runner=runner, cli_path="/opt/x/bin/openclaw")
    assert probe["status"] == su.SEND_SURFACE_OK
    assert calls[0][0] == ["/opt/x/bin/openclaw", "message", "send", "--help"]
    # The CLI's own bin dir (where node lives) must lead PATH — launchd/
    # sudo contexts have a minimal PATH and the `env node` shebang dies.
    assert calls[0][1]["PATH"].startswith("/opt/x/bin")


def test_probe_send_surface_cli_missing_is_failed(monkeypatch):
    monkeypatch.setattr(su, "_find_openclaw_cli", lambda: None)
    probe = su.probe_send_surface(runner=lambda a, e: (0, _SEND_HELP_OK, ""))
    assert probe["status"] == su.SEND_SURFACE_FAILED
    assert probe["reason"] == "cli_not_found"


def test_probe_send_surface_subcommand_gone_is_failed():
    probe = su.probe_send_surface(
        runner=lambda a, e: (1, "", "error: unknown command 'message'"),
        cli_path="/x/openclaw",
    )
    assert probe["status"] == su.SEND_SURFACE_FAILED
    assert probe["reason"] == "help_exited_nonzero"
    assert probe["rc"] == 1


def test_probe_send_surface_dropped_flag_is_failed():
    """The subcommand may survive an upgrade while a flag the gallery
    helpers pass is renamed — that still breaks every scheduled send."""
    help_without_target = _SEND_HELP_OK.replace(
        "  --target <id>        recipient id (use --target=<id> for negative ids)\n", "",
    )
    probe = su.probe_send_surface(
        runner=lambda a, e: (0, help_without_target, ""), cli_path="/x/openclaw",
    )
    assert probe["status"] == su.SEND_SURFACE_FAILED
    assert probe["reason"] == "contract_flags_missing"
    assert probe["missing_flags"] == ["--target"]


def test_probe_send_surface_timeout_is_unverified_never_ok():
    import subprocess as _subprocess

    def runner(argv, env):
        raise _subprocess.TimeoutExpired(argv, 30)

    probe = su.probe_send_surface(runner=runner, cli_path="/x/openclaw")
    assert probe["status"] == su.SEND_SURFACE_UNVERIFIED
    assert probe["reason"] == "probe_timeout"


def test_probe_send_surface_oserror_is_unverified_never_ok():
    def runner(argv, env):
        raise OSError("exec format error")

    probe = su.probe_send_surface(runner=runner, cli_path="/x/openclaw")
    assert probe["status"] == su.SEND_SURFACE_UNVERIFIED
    assert "exec format error" in probe["reason"]


def test_gate_send_surface_ok(monkeypatch):
    monkeypatch.setattr(
        su, "probe_send_surface",
        lambda **kw: {"status": "ok", "reason": None, "missing_flags": [], "rc": 0},
    )
    result, reqs = su.gate_send_surface()
    assert result.ok is True
    assert reqs == []


def test_gate_send_surface_failed_blocks(monkeypatch):
    monkeypatch.setattr(
        su, "probe_send_surface",
        lambda **kw: {"status": "failed", "reason": "cli_not_found",
                      "missing_flags": [], "rc": None},
    )
    result, reqs = su.gate_send_surface()
    assert result.ok is False
    assert len(reqs) == 1
    assert reqs[0].blocking is True
    assert reqs[0].id == "send-surface-broken"
    assert reqs[0].source_gate == "send_surface"


def test_gate_send_surface_unverified_is_advisory_never_ok(monkeypatch):
    """Tri-state honesty: a probe that couldn't run is reported as exactly
    that — gate not-ok (never a silent pass) but non-blocking."""
    monkeypatch.setattr(
        su, "probe_send_surface",
        lambda **kw: {"status": "unverified", "reason": "probe_timeout",
                      "missing_flags": [], "rc": None},
    )
    result, reqs = su.gate_send_surface()
    assert result.ok is False
    assert len(reqs) == 1
    assert reqs[0].blocking is False
    assert reqs[0].id == "send-surface-unverified"
    assert "NOT confirmed" in reqs[0].summary


def test_gate_send_surface_in_gate_order_and_preflight(tmp_path):
    """run_preflight carries the send_surface gate in the report."""
    assert "send_surface" in su.GATE_ORDER
    fake_metadata = {
        "version": "2026.4.15",
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {"unpackedSize": 5_000_000},
        "engines": {"node": ">=18"},
    }
    with patch.object(su, "_fetch_registry_metadata", return_value=fake_metadata), \
         patch.object(su, "_node_version", return_value="20.11.1"), \
         patch.object(su, "_installed_version", return_value="2026.4.13"), \
         patch.object(su, "_fetch_candidate_plugin_ids", return_value=(set(), None)), \
         patch.object(su, "probe_send_surface",
                      return_value={"status": "failed", "reason": "cli_not_found",
                                    "missing_flags": [], "rc": None}):
        report = su.run_preflight(
            target_spec="2026.4.15",
            network={"members": [], "bots": {}},
            shared_dir=tmp_path,
            persist=False,
        )
    assert report.gates["send_surface"].ok is False
    assert report.ok is False
    assert any(r.id == "send-surface-broken" and r.blocking for r in report.requirements)
