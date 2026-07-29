"""Tests for the V1.5-3 upstream-version capture in plugins.inventory.

The full inventory module has wide coverage at the integration level via
the permissions monitor + admin endpoint tests; this file pins the
specific behavior added in V1.5-3: ``read_inventory`` extracts
``meta.lastTouchedVersion`` into ``PluginInventory.upstream_version``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from plugins import inventory  # noqa: E402


def _make_bot_home(tmp_path: Path, payload: dict, *, bot_id: str = "alice") -> Path:
    home = tmp_path / bot_id
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps(payload))
    return home


def test_upstream_version_captured(tmp_path, monkeypatch):
    """meta.lastTouchedVersion → PluginInventory.upstream_version (canonicalized)."""
    home = _make_bot_home(tmp_path, {
        "meta": {"lastTouchedVersion": "2026.4.29"},
        "plugins": {},
    })
    monkeypatch.setattr(inventory, "bot_home",
                        lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert inv.upstream_version == "2026.4.29"
    assert inv.upstream_version_raw == "2026.4.29"
    assert inv.read_error is None


def test_upstream_version_canonicalizes_v_prefix(tmp_path, monkeypatch):
    home = _make_bot_home(tmp_path, {
        "meta": {"lastTouchedVersion": "v2026.4.12"},
        "plugins": {},
    })
    monkeypatch.setattr(inventory, "bot_home",
                        lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert inv.upstream_version == "2026.4.12"
    assert inv.upstream_version_raw == "v2026.4.12"


def test_upstream_version_prerelease_drops_to_release(tmp_path, monkeypatch):
    """A beta tag canonicalizes to its release version for display."""
    home = _make_bot_home(tmp_path, {
        "meta": {"lastTouchedVersion": "2026.5.12-beta.1"},
        "plugins": {},
    })
    monkeypatch.setattr(inventory, "bot_home",
                        lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert inv.upstream_version == "2026.5.12"
    assert inv.upstream_version_raw == "2026.5.12-beta.1"


def test_upstream_version_absent_meta_block(tmp_path, monkeypatch):
    home = _make_bot_home(tmp_path, {"plugins": {}})
    monkeypatch.setattr(inventory, "bot_home",
                        lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert inv.upstream_version is None
    assert inv.upstream_version_raw is None


def test_upstream_version_unparseable_raw_preserved(tmp_path, monkeypatch):
    """Unparseable strings leave canonical=None but preserve raw for debugging."""
    home = _make_bot_home(tmp_path, {
        "meta": {"lastTouchedVersion": "garbage"},
        "plugins": {},
    })
    monkeypatch.setattr(inventory, "bot_home",
                        lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    assert inv.upstream_version is None
    assert inv.upstream_version_raw == "garbage"


def test_upstream_version_serializes(tmp_path, monkeypatch):
    """PluginInventory.to_dict() includes both fields."""
    home = _make_bot_home(tmp_path, {
        "meta": {"lastTouchedVersion": "2026.4.29"},
        "plugins": {},
    })
    monkeypatch.setattr(inventory, "bot_home",
                        lambda bot_id, _cfg=None: home)
    inv = inventory.read_inventory("alice")
    d = inv.to_dict()
    assert d["upstream_version"] == "2026.4.29"
    assert d["upstream_version_raw"] == "2026.4.29"
