"""tests/test_resolve_bot_paths_linux_turns.py — platform-aware turns candidate.

[META:model-tiers] regression for the Usage page's empty "No turn data found"
on a Linux VPS pod. ``resolve_bot_paths`` built the primary turns-dir candidate
as a macOS literal ``/Users/Shared/evolve/{bot}/turns``; on Linux the plugin
writes to ``/var/lib/evolve/{bot}/turns``, so the candidate never matched and
the turn reader saw zero records. The fix derives the shared-dir candidate from
``evolve_config.CANONICAL_SHARED_DIR`` and the pre-account home fallback from
``platform_profile.get_profile().user_home_root``.

``CANONICAL_SHARED_DIR`` is an import-time snapshot (see
test_setup_wizard_linux_paths.py), so we monkeypatch the ``evolve_config``
module attribute directly — which is exactly what ``resolve_bot_paths``'s
call-time ``from evolve_config import CANONICAL_SHARED_DIR`` reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

import evolve_config  # noqa: E402
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin.web.server import resolve_bot_paths  # noqa: E402


def test_resolve_bot_paths_linux_turns_under_var_lib(monkeypatch):
    """Linux profile: the first turns candidate is /var/lib/evolve/{bot}/turns
    and no candidate leaks a /Users path (account absent → /home fallback)."""
    monkeypatch.setattr(evolve_config, "CANONICAL_SHARED_DIR", Path("/var/lib/evolve"))
    set_profile(LINUX)
    try:
        paths = resolve_bot_paths("darwin")
    finally:
        set_profile(MACOS)  # conftest autouse also restores, belt-and-suspenders

    candidates = paths["turns_dir_candidates"]
    assert candidates[0] == "/var/lib/evolve/darwin/turns"
    assert paths["turns_dir"] == "/var/lib/evolve/darwin/turns"
    assert not any(str(c).startswith("/Users/") for c in candidates)


def test_resolve_bot_paths_macos_turns_unchanged(monkeypatch):
    """macOS profile: shared-dir candidate stays /Users/Shared/evolve/{bot}/turns."""
    monkeypatch.setattr(evolve_config, "CANONICAL_SHARED_DIR", Path("/Users/Shared/evolve"))
    set_profile(MACOS)

    paths = resolve_bot_paths("darwin")

    assert paths["turns_dir_candidates"][0] == "/Users/Shared/evolve/darwin/turns"
    assert paths["turns_dir"] == "/Users/Shared/evolve/darwin/turns"
