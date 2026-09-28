"""tests/test_capability_block.py — the unified [INSTALLED CAPABILITIES] block.

Spec: internal/spec-bot-capability-awareness-2026-06-22.md §3 (PUSH). P1 builds a
per-bot capability push block from three sources — installed skills, configured-
integration tools (Google via google_service.TOOL_SPECS), and apps — each with
"use this; don't improvise". These tests pin:

  * block assembly from each source (skill SKILL.md, integration provider, apps)
  * the empty case (no capabilities → "")
  * the size cap (truncation at a line boundary)
  * soft-fail on a malformed / unreadable SKILL.md (never raises)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import capability_block as cb  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def skills_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _write_skill(root: Path, slug: str, frontmatter: str, body: str = "body") -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return p


# ── Skills source ─────────────────────────────────────────────────────────────


class TestCollectSkills:
    def test_parses_name_description_and_triggers(self, skills_dir):
        _write_skill(
            skills_dir, "email-helper",
            "name: email-helper\n"
            "description: Draft and triage email. Handles inbox zero.\n"
            "when_to_use:\n  - check my email\n  - send a note to Bob",
        )
        items = cb.collect_skill_capabilities(skills_dir)
        assert len(items) == 1
        it = items[0]
        assert it.name == "email-helper"
        assert it.source == "skill"
        # First sentence only — keeps the row short.
        assert it.purpose == "Draft and triage email."
        assert it.triggers == ["check my email", "send a note to Bob"]

    def test_name_falls_back_to_dir_when_frontmatter_omits_it(self, skills_dir):
        _write_skill(skills_dir, "my-skill", "description: Does a thing.")
        items = cb.collect_skill_capabilities(skills_dir)
        assert [i.name for i in items] == ["my-skill"]

    def test_string_when_to_use_becomes_single_trigger(self, skills_dir):
        _write_skill(
            skills_dir, "s",
            "name: s\ndescription: x\nwhen_to_use: when the user mentions foo",
        )
        items = cb.collect_skill_capabilities(skills_dir)
        assert items[0].triggers == ["when the user mentions foo"]

    def test_missing_dir_returns_empty(self):
        assert cb.collect_skill_capabilities(Path("/nope/does/not/exist")) == []

    def test_malformed_frontmatter_soft_fails_to_dir_name(self, skills_dir):
        # Broken YAML must not raise and must not crash the scan — the skill
        # still surfaces by its directory name (OC loads it that way).
        _write_skill(skills_dir, "broken", "name: [unterminated\n  : : bad")
        items = cb.collect_skill_capabilities(skills_dir)
        assert [i.name for i in items] == ["broken"]
        assert items[0].purpose == ""

    def test_unreadable_skill_is_skipped(self, skills_dir, monkeypatch):
        _write_skill(skills_dir, "ok", "name: ok\ndescription: fine")
        _write_skill(skills_dir, "bad", "name: bad\ndescription: nope")

        real_read = Path.read_text

        def boom(self, *a, **k):
            if self.parent.name == "bad":
                raise OSError("EACCES")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", boom)
        items = cb.collect_skill_capabilities(skills_dir)
        assert [i.name for i in items] == ["ok"]


# ── Integration source (provider registry) ────────────────────────────────────


class TestCollectIntegrations:
    def test_google_provider_lists_real_tool_specs_when_configured(self):
        # Sourced from the shared google_service.TOOL_SPECS — the same source
        # the OC plugin registers from, so the block can't list a tool the
        # bot doesn't have. free_gmail_oauth is a SUPPORTED_MODE.
        net = {"bots": {"member_bot": {"google_integration": {"mode": "free_gmail_oauth"}}}}
        items = cb.collect_integration_capabilities("member_bot", net)
        names = {i.name for i in items}
        assert "gmail_send" in names
        assert all(i.source == "integration" and i.group == "Google" for i in items)
        # The motivating directive must be present on the group.
        assert any("do NOT use a shell CLI" in i.group_hint for i in items)

    def test_unconfigured_bot_gets_no_integration_tools(self):
        net = {"bots": {"member_bot": {}}}
        assert cb.collect_integration_capabilities("member_bot", net) == []

    def test_one_failing_provider_does_not_sink_the_rest(self):
        def boom(bot_id, network):
            raise RuntimeError("provider blew up")

        def ok(bot_id, network):
            return [cb.CapabilityItem(name="t", purpose="p", source="integration", group="G")]

        items = cb.collect_integration_capabilities(
            "member_bot", {"bots": {}}, providers=[boom, ok]
        )
        assert [i.name for i in items] == ["t"]

    def test_bad_network_soft_fails(self):
        assert cb.collect_integration_capabilities("member_bot", None) == []  # type: ignore[arg-type]


# ── Rendering ─────────────────────────────────────────────────────────────────


class TestRender:
    def test_empty_items_render_empty_string(self):
        assert cb.render_capabilities_block([]) == ""

    def test_groups_by_source_in_order(self):
        items = [
            cb.CapabilityItem(name="App1", purpose="does app stuff", source="app"),
            cb.CapabilityItem(name="skill1", purpose="does skill stuff", source="skill"),
            cb.CapabilityItem(
                name="gmail_send", purpose="send mail", source="integration",
                group="Google", group_hint="use these, do NOT invent a CLI",
            ),
        ]
        block = cb.render_capabilities_block(items)
        assert block.startswith("[INSTALLED CAPABILITIES")
        # Source order: skills → integration → apps.
        s = block.index("skill1")
        g = block.index("gmail_send")
        a = block.index("App1")
        assert s < g < a
        assert "Google tools (configured integration" in block
        assert "use these, do NOT invent a CLI" in block

    def test_purpose_optional(self):
        items = [cb.CapabilityItem(name="bare", purpose="", source="skill")]
        block = cb.render_capabilities_block(items)
        assert "- bare" in block
        assert "- bare —" not in block

    def test_cap_truncates_at_line_boundary_with_pointer(self):
        big = [
            cb.CapabilityItem(name=f"tool_{i}", purpose="x" * 40, source="skill")
            for i in range(200)
        ]
        block = cb.render_capabilities_block(big, cap_bytes=600)
        assert len(block.encode("utf-8")) <= 600
        assert "truncated" in block.splitlines()[-1]
        # Never splits a bullet mid-line.
        assert not block.splitlines()[-2].endswith("x")


# ── Top-level assembly ────────────────────────────────────────────────────────


class TestBuildBlock:
    def test_combines_all_three_sources(self, skills_dir):
        _write_skill(skills_dir, "weather", "name: weather\ndescription: Forecasts.")
        net = {"bots": {"member_bot": {"google_integration": {"mode": "free_gmail_oauth"}}}}
        apps = [cb.CapabilityItem(name="Briefing", purpose="morning digest", source="app")]
        block = cb.build_capabilities_block(
            "member_bot", skills_dir=skills_dir, network=net, app_items=apps,
        )
        assert "weather" in block        # skill
        assert "gmail_send" in block     # integration
        assert "Briefing" in block       # app

    def test_no_capabilities_returns_empty(self, skills_dir):
        # Empty skills dir, no network, no apps → empty block (gate on count).
        block = cb.build_capabilities_block("member_bot", skills_dir=skills_dir)
        assert block == ""

    def test_assembly_never_raises(self, monkeypatch):
        # Force a hard failure inside item collection — block must soft-fail.
        monkeypatch.setattr(
            cb, "collect_skill_capabilities",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert cb.build_capabilities_block("member_bot", skills_dir=Path("/x")) == ""
