"""Pin the upstream-false-positive suppression for plugins.code_safety.

Background: OpenClaw's `plugins.code_safety` audit scans bundled plugin
source — including `*.test.ts` / `*.spec.ts` / `*.mock.ts` — and the
`env-harvesting` rule matches `process.env` against any `fetch(`/`post(`/
`http.request(` anywhere in the file, including matches inside `it("…")`
descriptions. The result is CRITICAL findings on stock OC channel plugins
(observed for discord on team_bot_b, 2026-05-16). Filed upstream as
openclaw#82469. Until that ships, audit.py drops `plugins.code_safety`
findings whose hits all reference test/spec/mock files.

When upstream stops scanning test files, OC will stop emitting those
findings and the suppression silently no-ops.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


# The detail string OC emits — formatCodeSafetyDetails in
# src/security/audit-extra.async.ts produces:
#   "  - [rule-id] message (path/to/file.ext:LINE)"
_TEST_ONLY_DETAIL = (
    "Found 2 critical issue(s) in 495 scanned file(s):\n"
    "  - [env-harvesting] Environment variable access combined with network "
    "send — possible credential harvesting "
    "(src/monitor/provider.proxy.test.ts:140)\n"
    "  - [env-harvesting] Environment variable access combined with network "
    "send — possible credential harvesting "
    "(src/monitor/thread-bindings.lifecycle.test.ts:676)"
)

_REAL_PROD_DETAIL = (
    "Found 1 critical issue(s) in 31 scanned file(s):\n"
    "  - [potential-exfiltration] File read combined with network send — "
    "possible data exfiltration (src/engine.ts:2)"
)

_MIXED_DETAIL = (
    "Found 2 issue(s):\n"
    "  - [env-harvesting] foo (src/monitor/provider.proxy.test.ts:140)\n"
    "  - [potential-exfiltration] bar (src/engine.ts:2)"
)


def test_suppresses_when_all_hits_are_test_files():
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety", _TEST_ONLY_DETAIL
    ) is True


def test_emits_when_any_hit_is_production_code():
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety", _REAL_PROD_DETAIL
    ) is False
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety", _MIXED_DETAIL
    ) is False


def test_handles_spec_and_mock_variants():
    detail = (
        "Found 2 issue(s):\n"
        "  - [env-harvesting] x (src/api.spec.ts:10)\n"
        "  - [env-harvesting] x (src/utils/server.mock.js:55)"
    )
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety", detail
    ) is True


def test_only_suppresses_plugins_code_safety_check_id():
    assert audit._is_oc_code_safety_test_only_noise(
        "gateway.loopback_no_auth", _TEST_ONLY_DETAIL
    ) is False
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety.entry_escape", _TEST_ONLY_DETAIL
    ) is False


def test_empty_detail_is_not_suppressed():
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety", ""
    ) is False


def test_detail_without_parsable_lines_is_not_suppressed():
    """If OC ever reformats the detail, fail open (don't silently swallow)."""
    detail = "Some new free-form summary text that doesn't list paths."
    assert audit._is_oc_code_safety_test_only_noise(
        "plugins.code_safety", detail
    ) is False
