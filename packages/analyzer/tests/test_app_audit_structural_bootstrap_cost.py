"""Unit tests for the v22 bootstrap-cost assertions.

Three info-severity checks land in calibration phase: the goal here is to
pin the trip points and ensure the healthy baselines don't fire — once
real production data justifies promotion to warn/major, the thresholds
and severities will need re-pinning.

Pure function over (manifest, ctx). No filesystem, no Signal store, no LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_INFO,
    _BOT_GUIDANCE_BYTES_LIMIT,
    check_bot_guidance_size,
    check_cron_eligible_used_heartbeat,
    check_invocation_mode_subagent,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _cli_app_with_llm() -> dict:
    """A user-routed app that has CLI commands AND declares LLM intent —
    the canonical case where invocation_mode=subagent is required."""
    return {
        "id": "test-app",
        "name": "Test App",
        "usage": {"model": "user-initiated"},
        "interface_contract": {
            "cli": [{"command": "test-app run", "key_flags": ["--input"]}],
        },
        "recursive_llm": {
            "purposes": [
                {"intent": "classify the input"},
            ],
        },
        "invocation_mode": "subagent",
        "bot_guidance": [{"audience": "main", "text": "Short guidance."}],
    }


# ── check_bot_guidance_size ─────────────────────────────────────────────────


def test_bot_guidance_under_limit_passes() -> None:
    m = _cli_app_with_llm()
    m["bot_guidance"] = [{"audience": "main", "text": "x" * 800}]
    assert check_bot_guidance_size(m, {}) == []


def test_bot_guidance_exactly_at_limit_passes() -> None:
    m = _cli_app_with_llm()
    m["bot_guidance"] = [{"audience": "main", "text": "x" * _BOT_GUIDANCE_BYTES_LIMIT}]
    assert check_bot_guidance_size(m, {}) == []


def test_bot_guidance_one_byte_over_fires() -> None:
    m = _cli_app_with_llm()
    m["bot_guidance"] = [{"audience": "main", "text": "x" * (_BOT_GUIDANCE_BYTES_LIMIT + 1)}]
    findings = check_bot_guidance_size(m, {})
    assert len(findings) == 1
    assert findings[0].assertion_id == "app_bot_guidance_oversized"
    assert findings[0].severity == SEVERITY_INFO
    assert findings[0].evidence["bytes"] > _BOT_GUIDANCE_BYTES_LIMIT


def test_bot_guidance_as_plain_string_counts_correctly() -> None:
    """Older manifests may store bot_guidance as a string, not a list."""
    m = _cli_app_with_llm()
    m["bot_guidance"] = "x" * (_BOT_GUIDANCE_BYTES_LIMIT + 50)
    findings = check_bot_guidance_size(m, {})
    assert len(findings) == 1


def test_bot_guidance_missing_passes() -> None:
    m = _cli_app_with_llm()
    m.pop("bot_guidance", None)
    assert check_bot_guidance_size(m, {}) == []


def test_bot_guidance_utf8_multibyte_is_counted_in_bytes_not_chars() -> None:
    """Emoji and CJK characters are multi-byte in UTF-8; under the same
    character count an emoji-heavy block can still trip the byte limit."""
    m = _cli_app_with_llm()
    # Each emoji is 4 bytes UTF-8; 300 emojis = 1200 bytes > 1024.
    m["bot_guidance"] = [{"audience": "main", "text": "😀" * 300}]
    findings = check_bot_guidance_size(m, {})
    assert len(findings) == 1


# ── check_invocation_mode_subagent ──────────────────────────────────────────


def test_subagent_mode_passes() -> None:
    """The canonical happy path: CLI + LLM intent + invocation_mode=subagent."""
    assert check_invocation_mode_subagent(_cli_app_with_llm(), {}) == []


def test_main_mode_with_cli_and_llm_fires() -> None:
    m = _cli_app_with_llm()
    m["invocation_mode"] = "main"
    findings = check_invocation_mode_subagent(m, {})
    assert len(findings) == 1
    assert findings[0].assertion_id == "app_invocation_mode_not_subagent"
    assert findings[0].severity == SEVERITY_INFO


def test_unset_invocation_mode_with_cli_and_llm_fires() -> None:
    m = _cli_app_with_llm()
    m.pop("invocation_mode", None)
    findings = check_invocation_mode_subagent(m, {})
    assert len(findings) == 1


def test_no_llm_intent_skips_check() -> None:
    """A pure CLI app with no recursive_llm doesn't need subagent — it
    runs as a script, no LLM call to wrap."""
    m = _cli_app_with_llm()
    m.pop("recursive_llm", None)
    m["invocation_mode"] = "main"
    assert check_invocation_mode_subagent(m, {}) == []


def test_no_cli_skips_check() -> None:
    """An app with LLM intent but no CLI is scheduled / event-driven —
    it doesn't have a user-initiated entry point to subagent-wrap."""
    m = _cli_app_with_llm()
    m["interface_contract"]["cli"] = []
    m["invocation_mode"] = "main"
    assert check_invocation_mode_subagent(m, {}) == []


def test_scheduled_usage_model_skips_check() -> None:
    """Apps whose usage.model says they're not user-routed don't need
    invocation_mode=subagent — they're cron- or event-driven."""
    m = _cli_app_with_llm()
    m["usage"]["model"] = "scheduled"
    m["invocation_mode"] = "main"
    assert check_invocation_mode_subagent(m, {}) == []


# ── check_cron_eligible_used_heartbeat ──────────────────────────────────────


def test_heartbeat_with_llm_passes() -> None:
    """The legitimate heartbeat case: the app DOES have LLM intent so
    riding heartbeat may be defensible. Not the call this check makes."""
    m = _cli_app_with_llm()
    m["heartbeat_evidence"] = {"hook": "atlas/heartbeat-tick"}
    assert check_cron_eligible_used_heartbeat(m, {}) == []


def test_heartbeat_without_llm_fires() -> None:
    m = _cli_app_with_llm()
    m.pop("recursive_llm", None)
    m["heartbeat_evidence"] = {"hook": "atlas/heartbeat-tick"}
    findings = check_cron_eligible_used_heartbeat(m, {})
    assert len(findings) == 1
    assert findings[0].assertion_id == "app_cron_eligible_used_heartbeat"
    assert findings[0].severity == SEVERITY_INFO


def test_no_heartbeat_evidence_skips() -> None:
    """Pure cron app with no heartbeat anchor — nothing to flag."""
    m = _cli_app_with_llm()
    m.pop("recursive_llm", None)
    m.pop("heartbeat_evidence", None)
    assert check_cron_eligible_used_heartbeat(m, {}) == []


def test_empty_heartbeat_dict_skips() -> None:
    """An empty heartbeat_evidence dict is the manifest's idle state —
    don't flag it as if the app declared heartbeat anchoring."""
    m = _cli_app_with_llm()
    m.pop("recursive_llm", None)
    m["heartbeat_evidence"] = {}
    assert check_cron_eligible_used_heartbeat(m, {}) == []


def test_empty_purposes_list_treated_as_no_llm() -> None:
    """recursive_llm present but with an empty purposes list = no LLM intent."""
    m = _cli_app_with_llm()
    m["recursive_llm"] = {"purposes": []}
    m["heartbeat_evidence"] = {"hook": "x"}
    findings = check_cron_eligible_used_heartbeat(m, {})
    assert len(findings) == 1
