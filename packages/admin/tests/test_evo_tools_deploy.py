"""tests/test_evo_tools_deploy.py — Phase 2.1 deploy hook.

Covers ``evolve_admin.evo.tools.deploy_integration`` — the idempotent
gap-fill that writes ``mcp.servers.evo_tools`` into the primary bot's
openclaw.json so the OC gateway can spawn the stdio MCP bridge.

Two surfaces under test:

* ``build_server_block`` — pure function, exact-shape contract.
* ``ensure_evo_tools_mcp_server`` — primary-bot gate + idempotency +
  preserve-other-servers behavior. Read and write are stubbed via
  monkeypatch so the tests don't touch real /Users/ paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo.tools import deploy_integration as di  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# build_server_block — pure shape contract
# ─────────────────────────────────────────────────────────────────────────────


def test_server_block_shape_exact():
    """The block goes into evo's openclaw.json verbatim — drift here
    means OC either can't spawn the bridge or spawns it with the wrong
    runtime context. Pin the exact shape."""
    blk = di.build_server_block(
        venv_python="/Users/Shared/evolve-venv/bin/python3",
        shared_dir="/Users/Shared/evolve",
        network_path="/Users/Shared/evolve/network.json",
    )
    assert blk == {
        "command": "/Users/Shared/evolve-venv/bin/python3",
        "args": ["-m", "evolve_admin.evo.tools"],
        "env": {
            "EVOLVE_SHARED_DIR": "/Users/Shared/evolve",
            "EVOLVE_NETWORK_PATH": "/Users/Shared/evolve/network.json",
        },
    }


def test_server_block_env_uses_kwargs():
    """sharedDir / network_path come from network.json, not hardcoded.
    A test pod on a non-default sharedDir must produce a block that
    points at the test sharedDir, otherwise the bridge reads the wrong
    signal store."""
    blk = di.build_server_block(
        venv_python="/opt/venv/bin/python3",
        shared_dir="/tmp/test-pod/shared",
        network_path="/tmp/test-pod/network.json",
    )
    assert blk["command"] == "/opt/venv/bin/python3"
    assert blk["env"]["EVOLVE_SHARED_DIR"] == "/tmp/test-pod/shared"
    assert blk["env"]["EVOLVE_NETWORK_PATH"] == "/tmp/test-pod/network.json"


def test_server_block_is_stable_for_idempotency():
    """Idempotency check compares dicts for equality. Calling
    build_server_block twice with the same inputs must produce equal
    dicts so the dict-equality path in ensure_evo_tools_mcp_server
    detects "unchanged" correctly."""
    a = di.build_server_block(
        venv_python="/x/python3", shared_dir="/y", network_path="/z",
    )
    b = di.build_server_block(
        venv_python="/x/python3", shared_dir="/y", network_path="/z",
    )
    assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: stub the openclaw.json read/write surface
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_oc_io(monkeypatch):
    """Replace _read_bot_oc_config + _safe_write_bot_oc_config with a
    capture-based fake. Tests get back a Recorder with .seed/.reads/.writes
    so they can assert on what would have been written to disk without
    actually shelling out to sudo.
    """

    class Recorder:
        def __init__(self):
            self.seeded: dict[str, dict] = {}        # bot_id -> openclaw.json dict
            self.writes: list[tuple[str, dict]] = [] # (bot_id, new_config)
            self.read_error: str | None = None
            self.write_error: str | None = None

        def seed(self, bot_id: str, cfg: dict):
            self.seeded[bot_id] = cfg

    rec = Recorder()

    def fake_read(bot_id, bot_user):
        if rec.read_error:
            return None, rec.read_error
        return rec.seeded.get(bot_id, {}), None

    def fake_write(bot_id, new_config, *, reason, bot_user):
        if rec.write_error:
            return False, rec.write_error
        rec.writes.append((bot_id, new_config))
        rec.seeded[bot_id] = new_config  # so a follow-up call sees the write
        return True, ""

    monkeypatch.setattr(di, "_read_bot_oc_config", fake_read)
    monkeypatch.setattr(di, "_safe_write_bot_oc_config", fake_write)
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# ensure_evo_tools_mcp_server — primary-bot gate
# ─────────────────────────────────────────────────────────────────────────────


def test_non_primary_bot_is_skipped(stub_oc_io):
    """Member bots get tools via the indirect plugin path (spec §1.3) —
    they MUST NOT receive the tool-registry MCP server entry. Even if
    we read the file, the read returns the gate-skip status without
    attempting a write."""
    network = {
        "primary": "evolve",
        "bots": {
            "evolve": {"role": "primary"},
            "team_bot_a": {"role": "member"},
        },
    }
    changed, status = di.ensure_evo_tools_mcp_server(
        "team_bot_a", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert changed is False
    assert status == "skipped:not-primary"
    assert stub_oc_io.writes == []


def test_no_primary_resolved_skips(stub_oc_io):
    """If network.json doesn't resolve a primary, treat any bot as
    non-primary (defensive — no surprise writes on a half-configured
    pod)."""
    network = {"bots": {"team_bot_a": {"role": "member"}}}  # no "evolve" fallback
    changed, status = di.ensure_evo_tools_mcp_server(
        "team_bot_a", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert (changed, status) == (False, "skipped:not-primary")
    assert stub_oc_io.writes == []


def test_legacy_evolve_fallback_treats_evolve_as_primary(stub_oc_io):
    """Pods predating the explicit ``primary`` field have a bot literally
    named ``evolve`` filling the role — primary_bot_id falls back to
    that. Make sure the deploy hook honors the same fallback."""
    network = {"bots": {"evolve": {}}}  # no role= field
    stub_oc_io.seed("evolve", {})
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert changed is True
    assert status == "created"


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency + write path
# ─────────────────────────────────────────────────────────────────────────────


def test_creates_block_when_missing(stub_oc_io):
    """Primary bot with no mcp.servers section gets the canonical block
    created. The write payload contains exactly the expected nested
    shape; existing config fields outside mcp.* are preserved."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    stub_oc_io.seed("evolve", {
        "gateway": {"mode": "local", "bind": "loopback"},
        "agents": {"defaults": {"model": {"primary": "claude"}}},
    })
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert (changed, status) == (True, "created")
    assert len(stub_oc_io.writes) == 1
    _, written = stub_oc_io.writes[0]
    # Pre-existing fields untouched
    assert written["gateway"]["mode"] == "local"
    assert written["agents"]["defaults"]["model"]["primary"] == "claude"
    # New section present with the right shape
    assert written["mcp"]["servers"]["evo_tools"] == {
        "command": "/Users/Shared/evolve-venv/bin/python3",
        "args": ["-m", "evolve_admin.evo.tools"],
        "env": {
            "EVOLVE_SHARED_DIR": "/Users/Shared/evolve",
            "EVOLVE_NETWORK_PATH": "/Users/Shared/evolve/network.json",
        },
    }


def test_unchanged_when_block_already_matches(stub_oc_io):
    """Already-correct config doesn't write — that's the idempotency
    contract that lets this run on every deploy without churn."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    existing = di.build_server_block(
        venv_python="/Users/Shared/evolve-venv/bin/python3",
        shared_dir="/Users/Shared/evolve",
        network_path="/Users/Shared/evolve/network.json",
    )
    stub_oc_io.seed("evolve", {
        "mcp": {"servers": {"evo_tools": existing}},
    })
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert (changed, status) == (False, "unchanged")
    assert stub_oc_io.writes == []


def test_updates_when_block_drifted(stub_oc_io):
    """Operator hand-edit / stale sharedDir / interpreter rename →
    rewrite to the canonical block. Status reports ``updated`` so the
    deploy log shows a real change, not a quiet first-write."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    stub_oc_io.seed("evolve", {
        "mcp": {
            "servers": {
                "evo_tools": {
                    "command": "/old/python",
                    "args": ["-m", "evolve_admin.evo.tools"],
                    "env": {"EVOLVE_SHARED_DIR": "/old", "EVOLVE_NETWORK_PATH": "/old"},
                },
            },
        },
    })
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert (changed, status) == (True, "updated")
    assert len(stub_oc_io.writes) == 1
    _, written = stub_oc_io.writes[0]
    assert written["mcp"]["servers"]["evo_tools"]["command"] == \
        "/Users/Shared/evolve-venv/bin/python3"


def test_preserves_other_mcp_servers(stub_oc_io):
    """Operator-installed MCP servers (github, linear, etc.) in
    mcp.servers must survive every sync. The evo_tools entry shares the
    namespace; the gap-fill touches only its own key."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    stub_oc_io.seed("evolve", {
        "mcp": {
            "servers": {
                "linear": {"command": "/usr/bin/linear-mcp", "args": []},
                "github": {"url": "https://github-mcp.example/.well-known/mcp"},
            },
        },
    })
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert (changed, status) == (True, "created")
    _, written = stub_oc_io.writes[0]
    servers = written["mcp"]["servers"]
    assert "linear" in servers
    assert "github" in servers
    assert "evo_tools" in servers
    # Untouched entries are byte-identical
    assert servers["linear"]["command"] == "/usr/bin/linear-mcp"
    assert servers["github"]["url"] == "https://github-mcp.example/.well-known/mcp"


def test_idempotent_on_consecutive_calls(stub_oc_io):
    """Run twice in a row → first call creates, second sees the just-
    written block and skips. Models the real deploy where
    ensure_plugin_config + ensure_evo_tools_mcp_server both run on
    every deploy without churn."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    stub_oc_io.seed("evolve", {})
    first = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    second = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert first == (True, "created")
    assert second == (False, "unchanged")
    # Only one write — the second call short-circuited.
    assert len(stub_oc_io.writes) == 1


def test_uses_network_shared_dir_in_env(stub_oc_io):
    """A test pod on /Users/Shared/test-evolve must produce a block
    pointing at that sharedDir, not the production default."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/test-evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    stub_oc_io.seed("evolve", {})
    di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/test-evolve/network.json",
    )
    _, written = stub_oc_io.writes[0]
    blk = written["mcp"]["servers"]["evo_tools"]
    assert blk["env"]["EVOLVE_SHARED_DIR"] == "/Users/Shared/test-evolve"
    assert blk["env"]["EVOLVE_NETWORK_PATH"] == \
        "/Users/Shared/test-evolve/network.json"


# ─────────────────────────────────────────────────────────────────────────────
# Error paths
# ─────────────────────────────────────────────────────────────────────────────


def test_read_failure_surfaces_as_error(stub_oc_io):
    """Read failure is non-fatal at the caller (deploy.py logs and
    continues), but we still return an error-tagged status so the log
    line is honest about why the registration didn't happen."""
    network = {"primary": "evolve", "bots": {"evolve": {"role": "primary"}}}
    stub_oc_io.read_error = "PermissionError on /Users/evolve/.openclaw/openclaw.json"
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert changed is False
    assert status.startswith("error: read failed:")
    assert stub_oc_io.writes == []


def test_write_failure_surfaces_as_error(stub_oc_io):
    """safe_write_bot_config can fail (schema validation rejects the
    new shape, sudo /bin/cp blocked, etc.). Surface the underlying
    message so the operator has a thread to pull on."""
    network = {
        "primary": "evolve",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {"evolve": {"role": "primary"}},
    }
    stub_oc_io.seed("evolve", {})
    stub_oc_io.write_error = "schema validation failed: mcp.servers must be object"
    changed, status = di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    assert changed is False
    assert status.startswith("error: write failed:")


# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────


def test_default_venv_python_matches_deploy(stub_oc_io):
    """The default interpreter path must match deploy.VENV_PYTHON — that's
    the evolve-managed venv with the mcp SDK installed. A drift here
    means the bridge subprocess fails to import."""
    # We can't import deploy.VENV_PYTHON without exercising deploy.py's
    # CLI-discovery code, so assert against the documented constant in
    # the module.
    assert di.DEFAULT_VENV_PYTHON == "/Users/Shared/evolve-venv/bin/python3"


def test_default_shared_dir_when_network_omits_it(stub_oc_io):
    """If network.json has no ``sharedDir``, fall back to the production
    default — same fallback the bridge uses, so the env-var value
    matches what the bridge would compute on its own."""
    network = {
        "primary": "evolve",
        "bots": {"evolve": {"role": "primary"}},
        # no sharedDir key
    }
    stub_oc_io.seed("evolve", {})
    di.ensure_evo_tools_mcp_server(
        "evolve", network, network_path="/Users/Shared/evolve/network.json",
    )
    _, written = stub_oc_io.writes[0]
    assert written["mcp"]["servers"]["evo_tools"]["env"]["EVOLVE_SHARED_DIR"] == \
        "/Users/Shared/evolve"
