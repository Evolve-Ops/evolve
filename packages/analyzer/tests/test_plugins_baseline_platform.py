"""tests/test_plugins_baseline_platform.py — platform-keyed plugin baseline.

The plugin baseline's ``expected_load_paths`` is the supply-chain
perimeter: OpenClaw loads the built plugin from the pod's stable plugin
install dir, so that directory legitimately appears in every bot's
``plugins.load.paths`` and the baseline MUST expect it — otherwise
``plugin_load_path_unexpected`` fires for a wholly normal config.

That directory is platform-divergent — ``/Users/Shared/evolve-plugin``
(macOS) vs ``/var/lib/evolve-plugin`` (Linux), per
``PlatformProfile.plugin_install_dir``. The old hardcoded macOS literal
made a fresh Linux pod fire ``plugin_load_path_unexpected`` pod-wide
(every bot's load path was ``/var/lib/evolve-plugin``). These tests pin
that the default is now derived from the active profile — byte-identical
on macOS, correct on Linux — under BOTH profiles.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402
from plugins import baseline as bl  # noqa: E402
from plugins import bootstrap as bs  # noqa: E402
from plugins import monitor  # noqa: E402
from plugins import inventory  # noqa: E402


@pytest.fixture
def _restore_profile():
    try:
        yield
    finally:
        set_profile(None)


def test_default_expected_load_paths_is_macos_literal_on_macos(_restore_profile):
    """Byte-identity: on macOS the default is exactly the prior literal."""
    set_profile(MACOS)
    assert bl.default_expected_load_paths() == ["/Users/Shared/evolve-plugin"]


def test_default_expected_load_paths_is_fhs_on_linux(_restore_profile):
    """On Linux the default is the FHS plugin install dir, not the macOS path."""
    set_profile(LINUX)
    assert bl.default_expected_load_paths() == ["/var/lib/evolve-plugin"]


def test_baseline_dataclass_default_follows_profile(_restore_profile):
    set_profile(LINUX)
    b = bl.PluginBaseline()
    assert b.expected_load_paths == ["/var/lib/evolve-plugin"]
    set_profile(MACOS)
    b = bl.PluginBaseline()
    assert b.expected_load_paths == ["/Users/Shared/evolve-plugin"]


def test_bootstrap_default_baseline_follows_profile(_restore_profile):
    set_profile(LINUX)
    assert bs.build_default_baseline().expected_load_paths == [
        "/var/lib/evolve-plugin"
    ]
    set_profile(MACOS)
    assert bs.build_default_baseline().expected_load_paths == [
        "/Users/Shared/evolve-plugin"
    ]


# ── End-to-end: load-path drift detector ────────────────────────────────────


def _bot_home_with_load_path(tmp_path: Path, load_path: str, bot_id: str = "alice") -> Path:
    home = tmp_path / bot_id
    oc_dir = home / ".openclaw"
    (oc_dir / "plugins").mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(
        '{"plugins": {"entries": {}, "load": {"paths": ["%s"]}}}' % load_path
    )
    return home


def test_no_load_path_drift_on_linux_default(tmp_path, monkeypatch, _restore_profile):
    """A fresh Linux pod whose bot loads from /var/lib/evolve-plugin must
    NOT fire plugin_load_path_unexpected when the baseline is the
    profile-derived default. This is the pod-wide false-fire the change
    closes (plugin_load_path_unexpected fired x2 on evo-vps)."""
    set_profile(LINUX)
    home = _bot_home_with_load_path(tmp_path, "/var/lib/evolve-plugin")
    monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None: home)

    b = bl.PluginBaseline()  # profile-derived default (Linux)
    resolved = bl.resolve_for(b, "alice")
    inv = inventory.read_inventory("alice")
    findings = monitor._diff_one_bot(inv, resolved)

    types = {f["type"] for f in findings}
    assert "plugin_load_path_unexpected" not in types


def test_load_path_drift_still_fires_for_genuinely_unexpected_dir(
    tmp_path, monkeypatch, _restore_profile
):
    """The detector still works: a path outside the baseline fires. Pin
    on both profiles so the change didn't blunt the real supply-chain
    alert."""
    for profile, install_dir in ((LINUX, "/var/lib/evolve-plugin"),
                                 (MACOS, "/Users/Shared/evolve-plugin")):
        set_profile(profile)
        home = _bot_home_with_load_path(
            tmp_path / profile.name, "/tmp/rogue-plugins", bot_id="alice"
        )
        monkeypatch.setattr(inventory, "bot_home", lambda bot_id, _cfg=None, _h=home: _h)

        b = bl.PluginBaseline()  # default == [install_dir]
        assert b.expected_load_paths == [install_dir]
        resolved = bl.resolve_for(b, "alice")
        inv = inventory.read_inventory("alice")
        findings = monitor._diff_one_bot(inv, resolved)

        types = {f["type"] for f in findings}
        assert "plugin_load_path_unexpected" in types, f"profile={profile.name}"
