"""The sudo-`cat` fallback reads route the binary path through the
platform-profile seam, so the INVOKED path always matches the path granted in
/etc/sudoers.d/evolve (both derive from ``profile.cat``). Previously these two
sites hardcoded ``/bin/cat``; on Linux that happens to be a merged-/usr symlink
that works, but the contract must be "invoked == granted, from one source" so a
future profile change can't silently desync them. Both also keep ``-n`` so the
TTY-less admin server fails fast instead of hanging on a password prompt.

Lazy imports per feedback_module_level_routes_admin_import_pollutes_shard."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def linux_profile(monkeypatch):
    import platform_profile
    monkeypatch.setattr(platform_profile, "_override", platform_profile.LINUX)
    return platform_profile.LINUX


def test_safety_summary_read_json_cat_goes_through_seam(linux_profile, monkeypatch, tmp_path):
    """_read_json's PermissionError fallback runs `sudo -n <profile.cat> …`."""
    from evolve_admin import safety_summary as ss

    def _boom(self, *a, **k):
        raise PermissionError("ACL mask clamped")
    monkeypatch.setattr(Path, "read_text", _boom)

    captured: dict = {}

    def fake_run(args, **kw):
        captured["argv"] = list(args)
        return type("R", (), {"returncode": 0, "stdout": "{}"})()
    monkeypatch.setattr(ss.subprocess, "run", fake_run)

    result, err = ss._read_json(tmp_path / "openclaw.json")
    argv = captured["argv"]
    assert argv[0] == "sudo" and argv[1] == "-n", f"must stay non-interactive: {argv}"
    assert argv[2] == linux_profile.cat, f"cat path must be profile.cat: {argv}"
    assert err is None and result == {}


def test_neither_site_hardcodes_bin_cat_in_the_sudo_call():
    """Guard against regressing to a literal `"/bin/cat"` argv element next to
    `sudo` — the desync the seam routing prevents."""
    import evolve_admin.safety_summary as ss
    import evolve_admin.web.routes_content_scan as rcs

    for mod in (ss, rcs):
        src = Path(mod.__file__).read_text()
        assert 'get_profile().cat' in src, f"{mod.__name__} should use the seam"
        assert '"/bin/cat"' not in src, (
            f"{mod.__name__} still hardcodes /bin/cat in an argv — route it "
            "through get_profile().cat instead"
        )
