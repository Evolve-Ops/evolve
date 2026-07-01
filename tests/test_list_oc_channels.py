"""Parser + diff tests for tools/list_oc_channels.py.

The script is the canonical OC-channel enumeration source after the 2026-06-04
audit-framework fix (see docs/skills-deep-audit-2026-05-30.md §Method update).
These tests fixture-inject OC's catalog + extension shapes so CI catches parser
regressions without needing OpenClaw installed on the runner.

The live drift gate (snapshot-vs-mini) fires post-OC-upgrade and runs on the
mini, where the OC bundle actually exists — see the PR description for the
remote-runner TODO.

Run with: python -m pytest tests/test_list_oc_channels.py -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_TOOL = _REPO_ROOT / "tools" / "list_oc_channels.py"


def _load_module():
    # Register in sys.modules BEFORE exec_module so @dataclass class lookups
    # during type-resolution (Python 3.10 `_is_type`) can find the module.
    spec = importlib.util.spec_from_file_location("list_oc_channels", _TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_oc_channels"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def loc():
    return _load_module()


@pytest.fixture
def fake_oc_dist(tmp_path):
    """Build a tiny on-disk fake OC dist with both a catalog and extensions."""
    dist = tmp_path / "dist"
    (dist / "extensions").mkdir(parents=True)

    catalog = {
        "entries": [
            {
                "name": "@openclaw/discord",
                "kind": "channel",
                "openclaw": {
                    "channel": {
                        "id": "discord",
                        "label": "Discord",
                        "selectionLabel": "Discord (Bot API)",
                    },
                    "install": {
                        "npmSpec": "@openclaw/discord",
                        "defaultChoice": "npm",
                        "minHostVersion": ">=2026.4.10",
                    },
                },
            },
            {
                "name": "@openclaw/whatsapp",
                "kind": "channel",
                "openclaw": {
                    "channel": {"id": "whatsapp", "label": "WhatsApp"},
                    "install": {
                        "npmSpec": "@openclaw/whatsapp",
                        "clawhubSpec": "clawhub:@openclaw/whatsapp",
                        "defaultChoice": "clawhub",
                        "minHostVersion": ">=2026.4.25",
                    },
                },
            },
        ]
    }
    (dist / "channel-catalog.json").write_text(json.dumps(catalog))

    # Bundled channel-shaped extension.
    (dist / "extensions" / "imessage").mkdir()
    (dist / "extensions" / "imessage" / "package.json").write_text(json.dumps({
        "name": "@openclaw/imessage",
        "openclaw": {
            "channel": {"id": "imessage", "label": "iMessage", "selectionLabel": "iMessage (imsg)"},
        },
    }))

    # Bundled non-channel extension — must be filtered out by default.
    (dist / "extensions" / "webhooks").mkdir()
    (dist / "extensions" / "webhooks" / "package.json").write_text(json.dumps({
        "name": "@openclaw/webhooks",
        "openclaw": {"extensions": ["./index.js"]},
    }))

    return dist


def test_catalog_parser_extracts_channel_fields(loc, fake_oc_dist):
    """channel-catalog.json entries map to Channel records with install metadata."""
    text = (fake_oc_dist / "channel-catalog.json").read_text()
    channels = loc.parse_catalog(text)
    by_id = {c.id: c for c in channels}
    assert set(by_id) == {"discord", "whatsapp"}
    assert by_id["discord"].source == "catalog"
    assert by_id["discord"].default_install == "npm"
    assert by_id["discord"].min_host_version == ">=2026.4.10"
    assert by_id["whatsapp"].clawhub_spec == "clawhub:@openclaw/whatsapp"
    assert by_id["whatsapp"].default_install == "clawhub"


def test_extensions_scan_only_returns_channel_shaped(loc, fake_oc_dist):
    """Bundled extensions with no openclaw.channel.id block are excluded by default."""
    reader = loc.OcReader(fake_oc_dist, ssh_host=None)
    channels, warnings = loc.read_extensions(reader, include_non_channel=False)
    assert [c.id for c in channels] == ["imessage"]
    assert warnings == []
    assert channels[0].bundled_in == "extensions/imessage"
    assert channels[0].default_install == "bundled"


def test_extensions_scan_includes_non_channel_when_flagged(loc, fake_oc_dist):
    """--include-non-channel surfaces bridge/runtime extensions for completeness audits."""
    reader = loc.OcReader(fake_oc_dist, ssh_host=None)
    channels, _ = loc.read_extensions(reader, include_non_channel=True)
    ids = {c.id for c in channels}
    # The non-channel entry uses the npm name as its identifier since it has no channel.id.
    assert "imessage" in ids
    assert "@openclaw/webhooks" in ids


def test_missing_extensions_dir_warns_not_errors(loc, tmp_path):
    """OC versions without dist/extensions/ should warn, not crash."""
    dist = tmp_path / "dist-no-extensions"
    dist.mkdir()
    reader = loc.OcReader(dist, ssh_host=None)
    channels, warnings = loc.read_extensions(reader, include_non_channel=False)
    assert channels == []
    assert len(warnings) == 1
    assert "no extensions/" in warnings[0]


def test_diff_against_snapshot_matches_round_trip(loc, fake_oc_dist, tmp_path):
    """A snapshot regenerated from the same source must round-trip to exit=0."""
    rc = loc.main([
        "--source=both",
        f"--oc-dist={fake_oc_dist}",
        "--json",
    ])
    # Exit code from main() with no --diff-against is always 0 on success.
    assert rc == 0

    # Build a snapshot from the same source and diff it.
    reader = loc.OcReader(fake_oc_dist, ssh_host=None)
    catalog_text = reader.read_file("channel-catalog.json")
    channels = loc.parse_catalog(catalog_text)
    ext, _ = loc.read_extensions(reader, include_non_channel=False)
    channels.extend(ext)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(loc.render_json(channels))

    matches, message = loc.diff_against_snapshot(channels, snapshot_path)
    assert matches, message


def test_diff_against_snapshot_flags_added_channel(loc, fake_oc_dist, tmp_path):
    """A new channel in the live source vs. a stale snapshot must be flagged."""
    reader = loc.OcReader(fake_oc_dist, ssh_host=None)
    full = loc.parse_catalog(reader.read_file("channel-catalog.json"))
    ext, _ = loc.read_extensions(reader, include_non_channel=False)
    full.extend(ext)

    # Stale snapshot: drop whatsapp.
    stale = [c for c in full if c.id != "whatsapp"]
    snap = tmp_path / "stale.json"
    snap.write_text(loc.render_json(stale))

    matches, message = loc.diff_against_snapshot(full, snap)
    assert not matches
    assert "whatsapp" in message
    assert "added" in message


def test_committed_snapshot_is_well_formed():
    """The committed snapshot must be parseable and shaped as expected.

    Catches accidental hand-edits that corrupt the file shape. Does NOT verify
    snapshot content against the live mini — that's the drift gate's job and
    runs post-OC-upgrade where the OC bundle is available.
    """
    snap_path = _REPO_ROOT / "docs" / "skills" / "oc-channel-coverage.json"
    assert snap_path.is_file(), f"missing snapshot at {snap_path}"
    data = json.loads(snap_path.read_text())
    assert "channels" in data
    assert isinstance(data["channels"], list)
    assert data["channels"], "snapshot has zero channels — almost certainly wrong"
    required = {"id", "label", "source", "default_install", "bundled_in", "npm_name"}
    for entry in data["channels"]:
        missing = required - entry.keys()
        assert not missing, f"channel {entry.get('id')} missing fields: {missing}"
        assert entry["source"] in ("catalog", "extensions")
