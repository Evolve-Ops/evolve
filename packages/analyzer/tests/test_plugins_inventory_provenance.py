"""Tests for the install-records provenance merge in plugins.inventory.

Pins the 2026-06-06 follow-up to internal/spec-plugin-posture-rework-2026-06-06.md
§1.4: read_inventory merges install records from
``~/.openclaw/plugins/installs.json[.migrated]`` (and the legacy
``openclaw.json::plugins.installs`` block) onto each PluginEntry so the
Plugins page can show provenance. No alerts attached — the data is
advisory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from plugins import inventory  # noqa: E402


def _make_bot_home(
    tmp_path: Path,
    *,
    bot_id: str = "alice",
    oc_payload: dict | None = None,
    installs_payload: dict | None = None,
    installs_migrated_payload: dict | None = None,
) -> Path:
    """Build a tmp bot home with the requested files. Returns the home path."""
    home = tmp_path / bot_id
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True)
    plugins_dir = oc_dir / "plugins"
    plugins_dir.mkdir()
    if oc_payload is not None:
        (oc_dir / "openclaw.json").write_text(json.dumps(oc_payload))
    if installs_payload is not None:
        (plugins_dir / "installs.json").write_text(json.dumps(installs_payload))
    if installs_migrated_payload is not None:
        (plugins_dir / "installs.json.migrated").write_text(
            json.dumps(installs_migrated_payload)
        )
    return home


def test_provenance_merged_from_installs_json_migrated(tmp_path, monkeypatch):
    """The OC v2026.5.28 migration snapshot is the dominant source today
    — installs.json.migrated is the only install-records file present on
    every bot on the live pod. Provenance must come through from that file."""
    home = _make_bot_home(
        tmp_path,
        oc_payload={
            "plugins": {"entries": {"brave": {"enabled": True}}},
        },
        installs_migrated_payload={
            "installRecords": {
                "brave": {
                    "source": "npm",
                    "spec": "@openclaw/brave-plugin@2026.5.18",
                    "installPath": "/Users/alice/.openclaw/npm/node_modules/@openclaw/brave-plugin",
                    "resolvedName": "@openclaw/brave-plugin",
                    "resolvedVersion": "2026.5.18",
                    "clawhubChannel": "official",
                    "clawhubFamily": "code-plugin",
                },
            },
        },
    )
    monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert len(inv.entries) == 1
    e = inv.entries[0]
    assert e.name == "brave"
    assert e.install_source == "npm"
    assert e.install_spec == "@openclaw/brave-plugin@2026.5.18"
    assert e.install_path.endswith("/@openclaw/brave-plugin")
    assert e.resolved_name == "@openclaw/brave-plugin"
    assert e.resolved_version == "2026.5.18"
    assert e.clawhub_channel == "official"
    assert e.clawhub_family == "code-plugin"


def test_installs_json_wins_over_migrated(tmp_path, monkeypatch):
    """When both files exist, installs.json (the current OC location)
    overrides installs.json.migrated (the one-shot migration snapshot)."""
    home = _make_bot_home(
        tmp_path,
        oc_payload={
            "plugins": {"entries": {"codex": {"enabled": True}}},
        },
        installs_migrated_payload={
            "installRecords": {
                "codex": {"source": "npm", "resolvedVersion": "2026.5.18"},
            },
        },
        installs_payload={
            "installRecords": {
                "codex": {"source": "npm", "resolvedVersion": "2026.6.1"},
            },
        },
    )
    monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert inv.entries[0].resolved_version == "2026.6.1"


def test_provenance_absent_leaves_fields_none(tmp_path, monkeypatch):
    """No installs file + no legacy installs block → all provenance None.
    The entry still appears (enabled state, name, config_signature)."""
    home = _make_bot_home(
        tmp_path,
        oc_payload={
            "plugins": {"entries": {"evolve": {"enabled": True}}},
        },
    )
    monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    e = inv.entries[0]
    assert e.name == "evolve"
    assert e.enabled is True
    assert e.install_source is None
    assert e.resolved_name is None
    assert e.clawhub_channel is None


def test_legacy_in_config_installs_block_fallback(tmp_path, monkeypatch):
    """Older bots may still carry installs in openclaw.json::plugins.installs.
    Fall back to that when no external file exists."""
    home = _make_bot_home(
        tmp_path,
        oc_payload={
            "plugins": {
                "entries": {"evolve": {"enabled": True}},
                "installs": {
                    "evolve": {
                        "source": "path",
                        "sourcePath": "/Users/Shared/evolve-plugin",
                        "installPath": "/Users/Shared/evolve-plugin",
                    },
                },
            },
        },
    )
    monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    e = inv.entries[0]
    assert e.install_source == "path"
    assert e.install_spec == "/Users/Shared/evolve-plugin"
    assert e.install_path == "/Users/Shared/evolve-plugin"


def test_provenance_does_not_trigger_alerts(tmp_path, monkeypatch):
    """Belt-and-suspenders pin on the §1.4 amendment: even with a
    'suspicious-looking' install source (an arbitrary string OC could
    plausibly write tomorrow), the monitor produces no
    plugin_unverified_source finding. The signal type is retired from
    active emission."""
    from plugins import monitor
    from plugins import baseline
    home = _make_bot_home(
        tmp_path,
        oc_payload={
            "plugins": {
                "entries": {"shady": {"enabled": True}},
                "load": {"paths": ["/Users/Shared/evolve-plugin"]},
            },
        },
        installs_migrated_payload={
            "installRecords": {
                "shady": {"source": "totally-made-up-source"},
            },
        },
    )
    monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None: home)
    bl = baseline.PluginBaseline(
        expected_load_paths=["/Users/Shared/evolve-plugin"],
    )
    resolved = baseline.resolve_for(bl, "alice")
    inv = inventory.read_inventory("alice")
    findings = monitor._diff_one_bot(inv, resolved)
    # Should produce zero findings on this clean inventory.
    types = {f["type"] for f in findings}
    assert "plugin_unverified_source" not in types
    # Provenance still made it through.
    assert inv.entries[0].install_source == "totally-made-up-source"
