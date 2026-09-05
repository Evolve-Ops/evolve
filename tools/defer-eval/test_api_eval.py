"""Mechanical tests for api_eval.py — extraction + system prompt assembly.

The actual API call is exercised by `python3 api_eval.py --limit 1` with a
real key; these tests cover everything else (no network, no API key needed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import api_eval as a


# ── load_pod_conduct_summary ─────────────────────────────────────────────


class TestLoadPodConductSummary:
    def test_returns_summary_block_only(self):
        text = a.load_pod_conduct_summary()
        # The summary contains the defer rule (rule #1)
        assert "defer" in text.lower()
        # The summary mentions structural lack of persistence
        assert "persistence between turns" in text.lower()
        # And it includes the closing reference to POD_CONDUCT.md
        assert "Full rules: POD_CONDUCT.md" in text


# ── build_system_prompt ──────────────────────────────────────────────────


class TestBuildSystemPrompt:
    def test_includes_pod_conduct_and_current_time(self):
        prompt = a.build_system_prompt()
        # Pod conduct content present
        assert "defer" in prompt.lower()
        # Current time injected for relative-offset arithmetic
        assert "Current time:" in prompt
        # Time block explains the offset rule
        assert "ISO 8601" in prompt


# ── extract_defer_from_response ──────────────────────────────────────────


class TestExtractDefer:
    def test_returns_none_when_no_tool_use(self):
        parsed = {
            "content": [
                {"type": "text", "text": "Hello! Sure thing."},
            ]
        }
        assert a.extract_defer_from_response(parsed) is None

    def test_returns_none_when_tool_use_is_different_tool(self):
        parsed = {
            "content": [
                {"type": "tool_use", "name": "some_other_tool", "input": {"x": 1}},
            ]
        }
        assert a.extract_defer_from_response(parsed) is None

    def test_extracts_message_mode_defer(self):
        parsed = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "defer",
                    "input": {
                        "due_at": "2026-05-06T16:35:00Z",
                        "message": "My favorite color is blue 🎨",
                    },
                },
                {"type": "text", "text": "Got it, will get back to you."},
            ]
        }
        row = a.extract_defer_from_response(parsed)
        assert row is not None
        assert row["mode"] == "message"
        assert row["fires_at"] == "2026-05-06T16:35:00Z"
        assert row["message"] == "My favorite color is blue 🎨"
        assert row["action"] is None

    def test_extracts_action_mode_defer(self):
        parsed = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_002",
                    "name": "defer",
                    "input": {
                        "due_at": "2026-05-06T17:00:00Z",
                        "action": "Check build status, summarize",
                    },
                },
            ]
        }
        row = a.extract_defer_from_response(parsed)
        assert row is not None
        assert row["mode"] == "action"
        assert row["action"] == "Check build status, summarize"
        assert row["message"] is None

    def test_handles_neither_message_nor_action(self):
        """If the model calls defer with only due_at and no message/action,
        mode is None — the row would be malformed, but extraction shouldn't
        crash. score_case will mark mode-mismatch downstream."""
        parsed = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_003",
                    "name": "defer",
                    "input": {"due_at": "2026-05-06T17:00:00Z"},
                },
            ]
        }
        row = a.extract_defer_from_response(parsed)
        assert row is not None
        assert row["mode"] is None

    def test_picks_first_defer_when_multiple_tool_uses(self):
        parsed = {
            "content": [
                {
                    "type": "tool_use", "id": "tu_a", "name": "defer",
                    "input": {"due_at": "2026-05-06T16:30:00Z", "message": "first"},
                },
                {
                    "type": "tool_use", "id": "tu_b", "name": "defer",
                    "input": {"due_at": "2026-05-06T17:00:00Z", "message": "second"},
                },
            ]
        }
        row = a.extract_defer_from_response(parsed)
        assert row["message"] == "first"


# ── DEFER_TOOL shape ─────────────────────────────────────────────────────


class TestDeferToolShape:
    def test_has_required_keys(self):
        assert a.DEFER_TOOL["name"] == "defer"
        assert "description" in a.DEFER_TOOL
        assert "input_schema" in a.DEFER_TOOL

    def test_input_schema_marks_due_at_required(self):
        schema = a.DEFER_TOOL["input_schema"]
        assert schema["type"] == "object"
        assert "due_at" in schema["properties"]
        assert "due_at" in schema["required"]

    def test_input_schema_documents_both_modes(self):
        props = a.DEFER_TOOL["input_schema"]["properties"]
        assert "message" in props
        assert "action" in props
        assert "Mutually exclusive" in props["message"]["description"]
        assert "Mutually exclusive" in props["action"]["description"]
