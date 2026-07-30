"""Regression tests for heal._is_gateway_proc_line.

Pins the contract that callers (apply.py, heal.py, spend_caps.py,
audit.py) recognize the gateway under every openclaw process-title
shape we have observed in the wild.

Drift here is silent and dangerous: a single-marker check (the original
`openclaw-gateway` substring) returns False for openclaw >= 2026.4.29
where the process is `node .../openclaw/dist/entry.js`, which makes
- apply.py skip the SIGTERM (config-apply restarts no-op),
- heal.py report `gateway_running=False` on every cycle,
- spend_caps.py fail to suspend on cap-bust,
- audit.py miss admin-running-gateway anomalies.

If you add a new process-title shape, update _GATEWAY_PROC_MARKERS in
heal.py (and the mirror in evolve_admin.ocadmin) AND add a fixture
line below.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from heal import _GATEWAY_PROC_MARKERS, _is_gateway_proc_line  # noqa: E402


# Realistic ps auxww lines as captured on the mini.
# Format: USER PID %CPU %MEM VSZ RSS TT STAT STARTED TIME COMMAND
_OLD_LINE = (
    "team_bot_a       12345   0.1  0.5  4321000  98000  ??  S    Mon10AM  0:12.34 "
    "openclaw-gateway --config /Users/team_bot_a/.openclaw/openclaw.json"
)
_NEW_ENTRY_LINE = (
    "team_bot_a       54321   0.2  0.7  5400000 110000  ??  S    Mon10AM  0:45.10 "
    "node /Users/team_bot_a/.openclaw/node_modules/openclaw/dist/entry.js"
)
_OLD_INDEX_LINE = (
    "team_bot_a       11111   0.1  0.5  4321000  98000  ??  S    Mon10AM  0:01.00 "
    "node /Users/team_bot_a/.openclaw/node_modules/openclaw/dist/index.js"
)
_MJS_LINE = (
    "team_bot_a       22222   0.1  0.5  4321000  98000  ??  S    Mon10AM  0:01.00 "
    "node /Users/team_bot_a/.openclaw/node_modules/openclaw/openclaw.mjs"
)
_OTHER_USER_LINE = (
    "admin_bot     99999   0.2  0.7  5400000 110000  ??  S    Mon10AM  0:45.10 "
    "node /Users/admin_bot/.openclaw/node_modules/openclaw/dist/entry.js"
)
_UNRELATED_LINE = (
    "team_bot_a       88888   0.0  0.0   408000   1200  ??  S    11:00AM  0:00.01 "
    "node /Users/team_bot_a/some/other/script.js"
)


class TestIsGatewayProcLine:
    """Match every observed openclaw process-title shape, with user filter."""

    def test_legacy_openclaw_gateway_title(self):
        assert _is_gateway_proc_line(_OLD_LINE, "team_bot_a") is True

    def test_modern_node_entry_js(self):
        # The case that motivated this entire fix — openclaw >= 2026.4.29.
        assert _is_gateway_proc_line(_NEW_ENTRY_LINE, "team_bot_a") is True

    def test_older_node_index_js(self):
        assert _is_gateway_proc_line(_OLD_INDEX_LINE, "team_bot_a") is True

    def test_alternate_mjs_entrypoint(self):
        assert _is_gateway_proc_line(_MJS_LINE, "team_bot_a") is True

    def test_user_filter_rejects_other_user(self):
        # New process title for admin_bot, but caller asked for team_bot_a's gateway.
        assert _is_gateway_proc_line(_OTHER_USER_LINE, "team_bot_a") is False

    def test_unrelated_node_process(self):
        assert _is_gateway_proc_line(_UNRELATED_LINE, "team_bot_a") is False

    def test_empty_line(self):
        assert _is_gateway_proc_line("", "team_bot_a") is False


class TestPkillPatternForSpendCaps:
    """spend_caps._suspend_bot_gateway builds a pkill -f regex from the markers.

    Reproduces the build logic locally so we don't import spend_caps (which
    has heavy module-level deps). Keeps the contract pinned.
    """

    @staticmethod
    def _build_pattern() -> str:
        return "(" + "|".join(re.escape(m) for m in _GATEWAY_PROC_MARKERS) + ")"

    def test_pattern_matches_every_marker(self):
        rx = re.compile(self._build_pattern())
        for marker in _GATEWAY_PROC_MARKERS:
            assert rx.search(marker), f"pattern missed marker {marker!r}"

    def test_pattern_matches_modern_entry_line(self):
        assert re.search(self._build_pattern(), _NEW_ENTRY_LINE)

    def test_pattern_matches_legacy_line(self):
        assert re.search(self._build_pattern(), _OLD_LINE)

    def test_pattern_dots_are_escaped(self):
        # Important: `.js` in the marker would match `xjs`, `_js`, etc.
        # without escaping, generating false positives in pkill -f.
        assert "\\.js" in self._build_pattern(), \
            "dot must be escaped to avoid pkill false positives"
