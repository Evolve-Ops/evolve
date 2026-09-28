"""Phase 3 of spec-config-intent-system-2026-05-21 — intent_inference.

These tests exercise the inference module without ever invoking a real
LLM. The single-call boundary is ``_call_llm`` which every test
monkey-patches to return a canned string — typical assertions check
both the prompt the helper received (so prompt-engineering regressions
get flagged) and the InferenceResult derived from the parsed output.

Failure-path coverage is the load-bearing part: the spec calls out
multiple fall-through modes (model unreachable, malformed JSON,
contradictions) and each one needs a deterministic test pinning the
resulting confidence to ``low`` + queued=True so the config_intent
write path keeps surfacing the intent even when inference can't
produce a useful answer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER = Path(__file__).resolve().parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from permissions import intent_inference as _inf  # noqa: E402


# ── Fixtures + helpers ──────────────────────────────────────────────────────


@pytest.fixture
def deps_path(tmp_path: Path) -> Path:
    """Minimal plugin → field deps YAML for the test fixtures."""
    path = tmp_path / "plugin_field_deps.yaml"
    path.write_text(
        """\
codex:
  required_fields:
    - field: tools.exec.security
      values: [allowlist, full]
      rationale: codex executes generated code locally; requires exec
brave:
  required_fields:
    - field: tools.web.search.enabled
      values: [true]
      rationale: brave is a web-search provider
""",
    )
    return path


@pytest.fixture
def patch_llm(monkeypatch):
    """Yields a recorder + setter so each test installs its own canned
    LLM output. The recorder captures every (prompt, model, tokens)
    invocation for prompt-shape assertions.
    """
    calls: list[dict] = []
    response: dict = {"raw": None}  # None = simulate model unreachable

    def _fake_call(prompt, *, model=_inf.DEFAULT_MODEL,
                   max_tokens=_inf.DEFAULT_MAX_TOKENS,
                   timeout=_inf.DEFAULT_TIMEOUT_SEC,
                   shared_dir=None):
        calls.append({"prompt": prompt, "model": model,
                      "max_tokens": max_tokens, "timeout": timeout,
                      "shared_dir": shared_dir})
        return response["raw"]

    monkeypatch.setattr(_inf, "_call_llm", _fake_call)

    class Patch:
        def respond_with(self, raw):
            response["raw"] = raw
        def calls(self):
            return calls
    return Patch()


# Disabled the analyzer-side plugin lookups (file read + sudo cat) by
# default so tests don't accidentally hit the host filesystem. Each
# test re-enables when needed via monkeypatch.
@pytest.fixture(autouse=True)
def isolate_plugin_discovery(monkeypatch):
    monkeypatch.setattr(_inf, "_enabled_plugins",
                        lambda bot_id, network: [])
    yield


# ── load_plugin_field_deps ──────────────────────────────────────────────────


class TestLoadPluginFieldDeps:
    def test_parses_well_formed_yaml(self, deps_path):
        deps = _inf.load_plugin_field_deps(deps_path)
        assert "codex" in deps
        assert "brave" in deps
        codex = deps["codex"]
        assert len(codex) == 1
        assert codex[0]["field"] == "tools.exec.security"
        assert codex[0]["values"] == ["allowlist", "full"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert _inf.load_plugin_field_deps(tmp_path / "absent.yaml") == {}

    def test_malformed_yaml_returns_empty(self, tmp_path):
        bad = tmp_path / "broken.yaml"
        bad.write_text(": this is not yaml :: at all")
        assert _inf.load_plugin_field_deps(bad) == {}

    def test_entries_without_required_fields_skipped(self, tmp_path):
        p = tmp_path / "deps.yaml"
        p.write_text(
            "codex:\n  not_a_required_fields_key: whatever\n"
            "brave:\n  required_fields:\n    - field: x\n      values: [true]\n"
            "      rationale: r\n",
        )
        deps = _inf.load_plugin_field_deps(p)
        assert "codex" not in deps
        assert "brave" in deps


# ── _parse_llm_output ───────────────────────────────────────────────────────


class TestParseLlmOutput:
    def test_extracts_object_from_clean_output(self):
        raw = '{"reason": "x", "confidence": "high"}'
        assert _inf._parse_llm_output(raw) == {
            "reason": "x", "confidence": "high",
        }

    def test_extracts_object_despite_preamble(self):
        """The model sometimes prefixes prose; we anchor on outer braces."""
        raw = (
            "Here's the JSON object you asked for:\n"
            '{"reason": "x", "confidence": "medium"}'
        )
        result = _inf._parse_llm_output(raw)
        assert result is not None
        assert result["confidence"] == "medium"

    def test_extracts_object_despite_markdown_fence(self):
        raw = '```json\n{"reason": "x", "confidence": "low"}\n```'
        result = _inf._parse_llm_output(raw)
        assert result is not None
        assert result["confidence"] == "low"

    def test_returns_none_on_empty(self):
        assert _inf._parse_llm_output("") is None

    def test_returns_none_on_no_braces(self):
        assert _inf._parse_llm_output("model says no") is None

    def test_returns_none_on_invalid_json(self):
        assert _inf._parse_llm_output("{ this is { not json }") is None

    def test_returns_none_on_non_object_json(self):
        assert _inf._parse_llm_output("[1, 2, 3]") is None


# ── infer() — happy paths ────────────────────────────────────────────────────


class TestInferHighConfidencePath:
    """Model returns a structured high-confidence verdict with a
    valid depends_on referencing a plugin actually enabled on the bot;
    the verdict survives the contradiction check unchanged."""

    def test_high_confidence_with_valid_plugin_reference(
        self, monkeypatch, patch_llm, deps_path,
    ):
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: ["codex"])
        patch_llm.respond_with(json.dumps({
            "reason": "codex plugin requires exec",
            "depends_on": {"plugin": "codex"},
            "confidence": "high",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        assert result.confidence == "high"
        assert result.set_by == "inferred:high"
        assert result.reason == "codex plugin requires exec"
        assert result.depends_on == {"plugin": "codex"}
        assert result.queued is False
        assert result.contradictions == []

    def test_prompt_includes_enabled_plugins_and_dep_map(
        self, monkeypatch, patch_llm, deps_path,
    ):
        """Prompt-shape regression guard. The dep map's rationale text
        must reach the model's prompt — without it the model has no
        documentation of why each plugin cares about each field."""
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: ["codex", "brave"])
        patch_llm.respond_with(json.dumps({
            "reason": "codex needs exec", "confidence": "high",
            "depends_on": {"plugin": "codex"},
        }))
        _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        prompt = patch_llm.calls()[0]["prompt"]
        assert "codex" in prompt
        assert "brave" in prompt
        assert "tools.exec.security" in prompt
        assert "executes generated code locally" in prompt  # rationale text
        assert "\"deny\"" in prompt
        assert "\"full\"" in prompt


# ── infer() — confidence downgrade paths ────────────────────────────────────


class TestInferContradictionDowngrade:
    def test_downgrades_when_claimed_plugin_not_enabled(
        self, monkeypatch, patch_llm, deps_path,
    ):
        """Model claims codex; codex isn't enabled. Result downgrades to
        low confidence, depends_on cleared, contradiction logged."""
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: [])  # nothing enabled
        patch_llm.respond_with(json.dumps({
            "reason": "codex plugin requires exec",
            "depends_on": {"plugin": "codex"},
            "confidence": "high",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        assert result.confidence == "low"
        assert result.set_by == "inferred:low"
        assert result.depends_on is None
        assert result.queued is True
        assert any("not enabled" in c for c in result.contradictions)
        # The reason text the model wrote IS preserved — operators
        # can still see what the model thought; the metadata flags
        # that we don't trust it.
        assert result.reason == "codex plugin requires exec"

    def test_downgrades_when_dep_map_doesnt_list_field(
        self, monkeypatch, patch_llm, deps_path,
    ):
        """codex is enabled, but the model claims it cares about
        tools.fs.workspaceOnly — which isn't in the dep map (codex's
        deps in our test seed only mention tools.exec.security).
        Contradiction → downgrade."""
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: ["codex"])
        patch_llm.respond_with(json.dumps({
            "reason": "codex needs workspaceOnly=false",
            "depends_on": {"plugin": "codex"},
            "confidence": "high",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.fs.workspaceOnly",
            old_value=True, new_value=False,
            deps_path=deps_path,
        )
        assert result.confidence == "low"
        assert any("doesn't list this field" in c for c in result.contradictions)

    def test_downgrades_when_new_value_outside_dep_map_values(
        self, monkeypatch, patch_llm, deps_path,
    ):
        """codex is enabled and the field IS in its dep map, but the
        new value isn't in the listed accepted values."""
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: ["codex"])
        # codex's dep map allows {allowlist, full} for tools.exec.security
        # but the model claims codex requires "deny".
        patch_llm.respond_with(json.dumps({
            "reason": "codex needs deny mode",
            "depends_on": {"plugin": "codex"},
            "confidence": "high",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="full", new_value="deny",
            deps_path=deps_path,
        )
        assert result.confidence == "low"
        assert any("allowed values" in c for c in result.contradictions)


# ── infer() — failure paths ─────────────────────────────────────────────────


class TestInferFailurePaths:
    def test_model_unreachable_returns_low_with_fallback_reason(
        self, patch_llm, deps_path,
    ):
        # Default state of patch_llm: response=None (simulating
        # _call_llm returning None on connection failure).
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        assert result.confidence == "low"
        assert result.queued is True
        assert "unreachable" in result.contradictions[0] or \
               "timed out" in result.contradictions[0]
        assert "Click 'Edit reason'" in result.reason  # fallback text

    def test_malformed_json_returns_low_with_fallback_reason(
        self, patch_llm, deps_path,
    ):
        patch_llm.respond_with("the model went off-script entirely")
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        assert result.confidence == "low"
        assert result.queued is True
        assert any("malformed" in c or "non-JSON" in c
                   for c in result.contradictions)

    def test_blank_reason_falls_through_to_low(
        self, patch_llm, deps_path,
    ):
        patch_llm.respond_with(json.dumps({
            "reason": "  ", "confidence": "high",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        assert result.confidence == "low"
        assert "no reason text" in result.contradictions[0]

    def test_unknown_confidence_collapses_to_low(
        self, patch_llm, deps_path,
    ):
        patch_llm.respond_with(json.dumps({
            "reason": "x", "confidence": "supreme",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        # Unknown confidence is silently coerced to low (no contradiction
        # since the model didn't claim a plugin) — the set_by tag still
        # surfaces the downgrade.
        assert result.confidence == "low"
        assert result.set_by == "inferred:low"


# ── infer() — medium confidence (no contradictions, no plugin link) ─────────


class TestInferMediumConfidence:
    def test_medium_passes_through_when_no_depends_on(
        self, patch_llm, deps_path,
    ):
        """Common shape when there's no plugin clearly implicated:
        the model returns medium confidence without depends_on. The
        result writes through with set_by=inferred:medium, queued=False
        (medium is recorded automatically; only low queues for
        operator follow-up)."""
        patch_llm.respond_with(json.dumps({
            "reason": "Plausibly aligned with the bot's documented role.",
            "depends_on": None,
            "confidence": "medium",
        }))
        result = _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
        )
        assert result.confidence == "medium"
        assert result.set_by == "inferred:medium"
        assert result.queued is False
        assert result.depends_on is None
        assert "documented role" in result.reason


# ── Phase 3.1 — _recent_activity ────────────────────────────────────────────


import datetime as _dt


def _utc(year, month, day, hour=0, minute=0, second=0):
    return _dt.datetime(year, month, day, hour, minute, second,
                        tzinfo=_dt.timezone.utc)


def _seed_admin_actions(shared_dir: Path, entries: list[dict]):
    path = shared_dir / "logs" / "admin-actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _seed_watchdog(shared_dir: Path, date_str: str, entries: list[dict]):
    path = shared_dir / "watchdog" / f"{date_str}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


class TestRecentActivity:
    """The activity helper reads two pod-wide log sources and filters
    them by bot_id + a time window. The Phase 3.1 add lets the model
    reason \"plugin install N min before this write → side effect\"."""

    def test_returns_events_for_bot_within_window(self, tmp_path):
        now = _utc(2026, 6, 6, 12, 0, 0)
        _seed_admin_actions(tmp_path, [
            {"ts": "2026-06-06T11:55:00Z", "action": "configure_codex",
             "bot": "team-bot-a", "initiated_by": "wizard", "result": "ok"},
            # bot mismatch — must NOT appear
            {"ts": "2026-06-06T11:55:00Z", "action": "configure_codex",
             "bot": "other-bot", "initiated_by": "wizard", "result": "ok"},
            # outside window — must NOT appear
            {"ts": "2026-06-06T11:30:00Z", "action": "old_action",
             "bot": "team-bot-a", "initiated_by": "wizard", "result": "ok"},
        ])
        _seed_watchdog(tmp_path, "2026-06-06", [
            {"id": "evt-1", "bot_id": "team-bot-a",
             "timestamp": "2026-06-06T11:58:00Z",
             "event_type": "plugin_install_complete",
             "details": {"summary": "codex installed"}},
        ])
        events = _inf._recent_activity(
            "team-bot-a", tmp_path, minutes=10, now=now,
        )
        sources = {e["source"] for e in events}
        assert sources == {"admin-actions", "watchdog"}
        # configure_codex is in
        admin_events = [e for e in events if e["source"] == "admin-actions"]
        assert len(admin_events) == 1
        assert "configure_codex" in admin_events[0]["summary"]
        assert admin_events[0]["minutes_ago"] == 5
        # plugin install is in
        wd = [e for e in events if e["source"] == "watchdog"]
        assert len(wd) == 1
        assert "codex installed" in wd[0]["summary"]
        assert wd[0]["minutes_ago"] == 2

    def test_filters_by_bot_id(self, tmp_path):
        now = _utc(2026, 6, 6, 12, 0, 0)
        _seed_admin_actions(tmp_path, [
            {"ts": "2026-06-06T11:58:00Z", "action": "x",
             "bot": "team-bot-a", "initiated_by": "p", "result": "ok"},
            {"ts": "2026-06-06T11:58:00Z", "action": "y",
             "bot": "team-bot-c", "initiated_by": "p", "result": "ok"},
        ])
        a_events = _inf._recent_activity(
            "team-bot-a", tmp_path, minutes=10, now=now,
        )
        c_events = _inf._recent_activity(
            "team-bot-c", tmp_path, minutes=10, now=now,
        )
        assert len(a_events) == 1 and "action=x" in a_events[0]["summary"]
        assert len(c_events) == 1 and "action=y" in c_events[0]["summary"]

    def test_drops_events_outside_window(self, tmp_path):
        now = _utc(2026, 6, 6, 12, 0, 0)
        _seed_admin_actions(tmp_path, [
            {"ts": "2026-06-06T11:30:00Z", "action": "older",
             "bot": "team-bot-a", "initiated_by": "p", "result": "ok"},
        ])
        events = _inf._recent_activity(
            "team-bot-a", tmp_path, minutes=10, now=now,
        )
        assert events == []

    def test_handles_midnight_crossing(self, tmp_path):
        """A 12:00-on-June-7 query with a 10min window should ALSO read
        the June-6 watchdog file because the window starts at 11:50pm
        the previous day in this fixture. The reader is supposed to
        scan both date files when the cutoff crosses midnight."""
        now = _utc(2026, 6, 7, 0, 5, 0)
        _seed_watchdog(tmp_path, "2026-06-06", [
            {"id": "evt-prev-day", "bot_id": "team-bot-a",
             "timestamp": "2026-06-06T23:58:00Z",
             "event_type": "alert", "details": {"summary": "late event"}},
        ])
        events = _inf._recent_activity(
            "team-bot-a", tmp_path, minutes=10, now=now,
        )
        assert len(events) == 1
        assert "late event" in events[0]["summary"]

    def test_returns_empty_when_no_logs(self, tmp_path):
        now = _utc(2026, 6, 6, 12, 0, 0)
        assert _inf._recent_activity(
            "team-bot-a", tmp_path, minutes=10, now=now,
        ) == []

    def test_returns_empty_when_shared_dir_none(self):
        assert _inf._recent_activity(
            "team-bot-a", None, minutes=10,
        ) == []

    def test_malformed_lines_silently_skipped(self, tmp_path):
        now = _utc(2026, 6, 6, 12, 0, 0)
        path = tmp_path / "logs" / "admin-actions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # First line broken; second valid in-window.
        path.write_text(
            "not json at all\n"
            + json.dumps({"ts": "2026-06-06T11:58:00Z", "action": "x",
                          "bot": "team-bot-a", "initiated_by": "p",
                          "result": "ok"}) + "\n"
        )
        events = _inf._recent_activity(
            "team-bot-a", tmp_path, minutes=10, now=now,
        )
        assert len(events) == 1


class TestInferUsesActivityContext:
    """Prompt-shape regression guard for the activity block. The infer
    contract is just that the prompt includes the events list — the
    model's own behavior is its concern."""

    def test_prompt_includes_recent_activity_block(
        self, monkeypatch, patch_llm, deps_path,
    ):
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: [])
        patch_llm.respond_with(json.dumps({
            "reason": "x", "confidence": "high",
        }))
        _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
            recent_activity=[
                {"source": "admin-actions", "at": "2026-06-06T11:58:00Z",
                 "minutes_ago": 2, "summary": "action=configure_codex"},
            ],
        )
        prompt = patch_llm.calls()[0]["prompt"]
        assert "Recent activity on this bot" in prompt
        assert "configure_codex" in prompt
        assert "2 min ago" in prompt
        assert "last 10 minutes" in prompt

    def test_prompt_empty_activity_falls_back_to_explicit_marker(
        self, monkeypatch, patch_llm, deps_path,
    ):
        """No events in the window → the model still sees a deterministic
        line so it can't be confused into thinking the prompt was
        truncated."""
        monkeypatch.setattr(_inf, "_enabled_plugins",
                            lambda bot_id, network: [])
        patch_llm.respond_with(json.dumps({
            "reason": "x", "confidence": "low",
        }))
        _inf.infer(
            bot_id="team-bot-a", field_path="tools.exec.security",
            old_value="deny", new_value="full",
            deps_path=deps_path,
            recent_activity=[],
        )
        prompt = patch_llm.calls()[0]["prompt"]
        assert "(no recent activity on this bot)" in prompt

