"""tests/test_usage_analytics_linux_paths.py — platform-aware turn READER paths.

[META:model-tiers] regression for the Usage / Model Economics page showing
"No turn data found for this period" on a Linux VPS pod. The plugin's
TurnObserver WRITES turns to ``{sharedDir}/{bot}/turns/`` using its configured
shared dir (``/var/lib/evolve`` on Linux), but the READ side hardcoded the
macOS literal ``/Users/Shared/evolve/{bot}/turns`` — so on Linux the shared-dir
candidate pointed at a nonexistent path and every turn was invisible.

Same hardcoded-macOS-path family as the admin-daemon-socket (#3160) and
primary-gateway-label bugs. The fix derives the shared-dir candidate from
``evolve_config.CANONICAL_SHARED_DIR`` (platform-keyed: /Users/Shared/evolve on
macOS, /var/lib/evolve on Linux) and the default network.json from
``CANONICAL_NETWORK_JSON``.

``CANONICAL_SHARED_DIR`` is an import-time snapshot of the running profile's
shared dir (see the note in test_setup_wizard_linux_paths.py), so a
``set_profile(LINUX)`` pin can't flip it on a macOS CI box. We therefore
monkeypatch the ``evolve_config`` module attribute directly — exactly what the
reader's call-time ``from evolve_config import CANONICAL_SHARED_DIR`` reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import evolve_config  # noqa: E402
import usage_analytics  # noqa: E402


def _force_reader_fallback(monkeypatch):
    """Make ``from evolve_admin.web.server import resolve_bot_paths`` raise so
    ``_find_turns_dirs`` takes its self-contained fallback branch (the one that
    constructs candidates without server.py). Setting the module to ``None`` in
    sys.modules makes the import statement raise ImportError."""
    monkeypatch.setitem(sys.modules, "evolve_admin.web.server", None)


def test_find_turns_dirs_fallback_linux_shared_dir(monkeypatch):
    """On a Linux profile the shared-dir turns candidate lands under
    /var/lib/evolve/{bot}/turns — NOT the macOS /Users/Shared literal."""
    _force_reader_fallback(monkeypatch)
    monkeypatch.setattr(evolve_config, "CANONICAL_SHARED_DIR", Path("/var/lib/evolve"))

    dirs = usage_analytics._find_turns_dirs("darwin")

    assert dirs[0] == Path("/var/lib/evolve/darwin/turns")
    # No candidate may point at the macOS shared literal on a Linux pod.
    assert not any(str(d).startswith("/Users/Shared/evolve/") for d in dirs)


def test_find_turns_dirs_fallback_macos_shared_dir(monkeypatch):
    """macOS behaviour is unchanged: shared-dir candidate stays under
    /Users/Shared/evolve/{bot}/turns."""
    _force_reader_fallback(monkeypatch)
    monkeypatch.setattr(evolve_config, "CANONICAL_SHARED_DIR", Path("/Users/Shared/evolve"))

    dirs = usage_analytics._find_turns_dirs("darwin")

    assert dirs[0] == Path("/Users/Shared/evolve/darwin/turns")


def test_load_turns_default_network_uses_canonical(tmp_path, monkeypatch):
    """When ``network_path`` is omitted, the pod-wide ("all bots") path reads
    the default network.json from ``CANONICAL_NETWORK_JSON`` — not a hardcoded
    ``/Users/Shared/evolve/network.json``. We point CANONICAL_NETWORK_JSON at a
    tmp network listing one bot and assert that bot's turns dir is consulted."""
    net = tmp_path / "network.json"
    net.write_text('{"bots": {"darwin": {}}}')
    monkeypatch.setattr(evolve_config, "CANONICAL_NETWORK_JSON", net)

    seen: list[str] = []

    def _spy(bid, network_path=None):
        seen.append(bid)
        return []  # no dirs → no turns, but we only care which bots were probed

    monkeypatch.setattr(usage_analytics, "_find_turns_dirs", _spy)

    turns = usage_analytics.load_turns(None, days=1, network_path=None)

    assert turns == []
    assert seen == ["darwin"]  # discovered from the canonical (tmp) network.json
