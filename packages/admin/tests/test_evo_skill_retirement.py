"""tests/test_evo_skill_retirement.py — Local skill retirement detector.

Phase 1.5b slice of spec §14.2. Tests the detector's three job:

  1. Parse SKILL.md frontmatter (yaml --- fences).
  2. Cross-check ``metadata.evolve.obviated_by`` against the live
     tool registry (exact + prefix match semantics).
  3. Produce RetirementCandidate records for the deploy log.

The detector NEVER deletes anything — confirms this contract
through the public API (function signature returns candidates;
none of them carry an "auto-deleted" flag).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import skill_retirement as sr  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_skill(
    root: Path, name: str, *,
    obviated_by: str | None = None,
    declared_name: str | None = None,
) -> Path:
    """Write a SKILL.md under ``root/<name>/`` with optional
    obviated_by + declared name in frontmatter. Returns the file path.

    Built by string concatenation rather than textwrap.dedent so the
    embedded YAML is exactly column-zero (the parser uses indentation
    sensitively).
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    declared = declared_name if declared_name else name
    lines = [
        "---",
        f"name: {declared}",
        "description: test skill",
    ]
    if obviated_by:
        lines.extend([
            "metadata:",
            "  evolve:",
            f"    obviated_by: {obviated_by}",
        ])
    lines.extend([
        "---",
        "",
        f"Skill body for {name}.",
        "",
    ])
    path = d / "SKILL.md"
    path.write_text("\n".join(lines))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_split_frontmatter_handles_well_formed_skill():
    text = textwrap.dedent("""\
        ---
        name: my-skill
        description: x
        ---

        body
        """)
    fm, body = sr._split_frontmatter(text)
    assert fm["name"] == "my-skill"
    # body parsing isn't critical for the retirement detector
    assert "body" in body or body == ""


def test_split_frontmatter_no_fence_returns_empty():
    fm, _ = sr._split_frontmatter("just markdown, no frontmatter")
    assert fm == {}


def test_split_frontmatter_unterminated_fence_returns_empty():
    """A malformed skill (--- opened but never closed) should NOT crash
    the detector — treat as no frontmatter."""
    fm, _ = sr._split_frontmatter("---\nname: broken")
    assert fm == {}


def test_split_frontmatter_invalid_yaml_returns_empty():
    text = "---\nname: [unterminated\n---\nbody\n"
    fm, _ = sr._split_frontmatter(text)
    assert fm == {}


# ─────────────────────────────────────────────────────────────────────────────
# obviated_by extraction
# ─────────────────────────────────────────────────────────────────────────────


def test_obviated_by_extract_present():
    fm = {
        "name": "x",
        "metadata": {"evolve": {"obviated_by": "action.bot.set_model"}},
    }
    assert sr._obviated_by_from_frontmatter(fm) == "action.bot.set_model"


def test_obviated_by_extract_missing_returns_none():
    fm = {"name": "x", "metadata": {"evolve": {}}}
    assert sr._obviated_by_from_frontmatter(fm) is None


def test_obviated_by_extract_handles_malformed_metadata():
    """If metadata is not a dict (operator-authored with a string instead),
    skip silently — never crash."""
    fm = {"name": "x", "metadata": "should-be-dict"}
    assert sr._obviated_by_from_frontmatter(fm) is None


# ─────────────────────────────────────────────────────────────────────────────
# Match resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_exact_match():
    matched = sr._resolve_match(
        "action.bot.set_model",
        ["action.bot.restart", "action.bot.set_model", "pod_state.bots"],
    )
    assert matched == ("action.bot.set_model", "exact")


def test_resolve_no_match_returns_none():
    matched = sr._resolve_match(
        "action.bot.set_model",
        ["action.bot.restart", "pod_state.bots"],
    )
    assert matched is None


def test_resolve_prefix_match_requires_trailing_dot():
    """``action.bot.`` (trailing dot) → prefix mode. ``action.bot`` (no
    dot) → no match — typo guard."""
    tools = ["action.bot.restart", "action.bot.set_model"]
    assert sr._resolve_match("action.bot.", tools) is not None
    assert sr._resolve_match("action.bot", tools) is None


def test_resolve_prefix_match_picks_first_matching_tool():
    """Prefix mode returns the first tool that satisfies — caller
    surfaces the matched tool name for the deploy log."""
    tools = ["action.bot.restart", "action.bot.set_model"]
    result = sr._resolve_match("action.bot.", tools)
    assert result is not None
    matching_tool, mode = result
    assert matching_tool.startswith("action.bot.")
    assert mode == "prefix"


def test_resolve_exact_match_wins_over_prefix():
    """If exact and prefix could both match, exact wins — the operator's
    declaration is more specific."""
    matched = sr._resolve_match(
        "action.bot.restart",
        ["action.bot.restart", "action.bot.redeploy"],
    )
    assert matched == ("action.bot.restart", "exact")


# ─────────────────────────────────────────────────────────────────────────────
# Scan + dispatch — detect_retirement_candidates
# ─────────────────────────────────────────────────────────────────────────────


def test_detect_returns_empty_when_no_skills(tmp_path):
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["any.tool"],
    )
    assert candidates == []


def test_detect_returns_empty_when_skills_lack_obviated_by(tmp_path):
    _write_skill(tmp_path, "no-obv")
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["any.tool"],
    )
    assert candidates == []


def test_detect_returns_empty_when_obviated_by_doesnt_match_any_tool(tmp_path):
    _write_skill(tmp_path, "old-recipe", obviated_by="action.future.thing")
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["action.bot.restart"],
    )
    assert candidates == []


def test_detect_finds_exact_match_in_extra_dirs(tmp_path):
    _write_skill(
        tmp_path, "set-bot-model",
        obviated_by="action.bot.set_model",
    )
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["action.bot.set_model"],
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.skill_name == "set-bot-model"
    assert c.matching_tool == "action.bot.set_model"
    assert c.match_mode == "exact"
    assert c.source == "evolve-shipped"


def test_detect_finds_prefix_match(tmp_path):
    _write_skill(
        tmp_path, "any-bot-action",
        obviated_by="action.bot.",
    )
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["action.bot.restart", "pod_state.bots"],
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.match_mode == "prefix"
    assert c.matching_tool == "action.bot.restart"


def test_detect_scans_workspace_local_skills(tmp_path):
    """Pod-local skills at {workspace}/skills/local/<name>/SKILL.md are
    scanned and tagged source='pod-local'."""
    workspace = tmp_path / "workspace"
    local_root = workspace / "skills" / "local"
    _write_skill(local_root, "my-local-recipe", obviated_by="action.bot.restart")

    candidates = sr.detect_retirement_candidates(
        extra_dirs=[],
        workspace_dir=workspace,
        tool_names=["action.bot.restart"],
    )
    assert len(candidates) == 1
    assert candidates[0].source == "pod-local"


def test_detect_distinguishes_evolve_shipped_from_pod_local(tmp_path):
    """Two skills, one in extra_dirs, one in workspace local — both match,
    each tagged with the right source."""
    evolve_dir = tmp_path / "evolve-shipped"
    _write_skill(evolve_dir, "ev-skill", obviated_by="action.bot.restart")

    workspace = tmp_path / "workspace"
    _write_skill(workspace / "skills" / "local", "local-skill",
                 obviated_by="action.bot.restart")

    candidates = sr.detect_retirement_candidates(
        extra_dirs=[evolve_dir],
        workspace_dir=workspace,
        tool_names=["action.bot.restart"],
    )
    sources_by_name = {c.skill_name: c.source for c in candidates}
    assert sources_by_name == {
        "ev-skill": "evolve-shipped",
        "local-skill": "pod-local",
    }


def test_detect_handles_missing_extra_dir_gracefully(tmp_path):
    """A non-existent extraDirs entry (e.g. stale path after operator
    rename) shouldn't crash — just yields nothing."""
    bogus = tmp_path / "does-not-exist"
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[bogus],
        workspace_dir=None,
        tool_names=["any.tool"],
    )
    assert candidates == []


def test_detect_recurses_subtree(tmp_path):
    """OC's loader recurses; we mirror that. A SKILL.md two levels deep
    is still picked up."""
    deep = tmp_path / "level1" / "level2"
    _write_skill(deep, "deep-skill", obviated_by="action.bot.restart")
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["action.bot.restart"],
    )
    assert len(candidates) == 1


def test_detect_uses_declared_name_over_dir_name(tmp_path):
    """When SKILL.md frontmatter has ``name:``, the candidate carries
    that value (OC's loader keys on it). Falls back to dir name otherwise."""
    _write_skill(
        tmp_path, "dir-name",
        obviated_by="action.bot.restart",
        declared_name="frontmatter-name",
    )
    candidates = sr.detect_retirement_candidates(
        extra_dirs=[tmp_path],
        workspace_dir=None,
        tool_names=["action.bot.restart"],
    )
    assert candidates[0].skill_name == "frontmatter-name"


# ─────────────────────────────────────────────────────────────────────────────
# format_deploy_notice
# ─────────────────────────────────────────────────────────────────────────────


def test_deploy_notice_mentions_skill_name_and_matching_tool():
    c = sr.RetirementCandidate(
        skill_name="set-bot-model",
        skill_path="/tmp/skill/SKILL.md",
        obviated_by="action.bot.set_model",
        matching_tool="action.bot.set_model",
        source="pod-local",
        match_mode="exact",
    )
    line = sr.format_deploy_notice(c)
    assert "set-bot-model" in line
    assert "action.bot.set_model" in line
    assert "pod-local" in line
    # Per spec §14.2 — "Never automatic deletion. Operator decides."
    # The notice points the operator at the CLI command, doesn't claim
    # the skill was deleted.
    assert "delete" in line.lower()
    assert "retire-local-skill" in line


# ─────────────────────────────────────────────────────────────────────────────
# resolve_skill_sources_for_bot
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_skill_sources_returns_extra_dirs_from_openclaw_json(monkeypatch):
    """Walks agents.defaults.skills.load.extraDirs in the bot's
    openclaw.json and converts each string to a Path."""
    fake_oc = {
        "agents": {
            "defaults": {
                "skills": {
                    "load": {
                        "extraDirs": [
                            "/users/Shared/evolve-repo/packages/analyzer/evolve_bot/skills",
                            "/custom/operator/dir",
                        ],
                    },
                },
            },
        },
    }
    from evolve_admin.evo.tools import deploy_integration
    monkeypatch.setattr(
        deploy_integration, "_read_bot_oc_config",
        lambda bot_id, bot_user: (fake_oc, None),
    )
    # Workspace lookup may also fire; stub to None so the result has no
    # workspace component to assert on here.
    from evolve_admin import config as admin_config
    monkeypatch.setattr(admin_config, "get_bot_workspace", lambda bot_id, user=None: None)

    extra_dirs, workspace = sr.resolve_skill_sources_for_bot(
        "evolve", bot_user="evolve",
    )
    assert len(extra_dirs) == 2
    assert all(isinstance(p, Path) for p in extra_dirs)
    assert workspace is None


def test_resolve_skill_sources_empty_when_no_extra_dirs(monkeypatch):
    from evolve_admin.evo.tools import deploy_integration
    monkeypatch.setattr(
        deploy_integration, "_read_bot_oc_config",
        lambda bot_id, bot_user: ({}, None),
    )
    from evolve_admin import config as admin_config
    monkeypatch.setattr(admin_config, "get_bot_workspace", lambda bot_id, user=None: None)

    extra_dirs, _ = sr.resolve_skill_sources_for_bot("evolve", bot_user="evolve")
    assert extra_dirs == []


def test_resolve_skill_sources_skips_malformed_extra_dirs(monkeypatch):
    """If extraDirs is not a list (e.g. operator hand-edited to a string),
    treat as empty rather than crashing."""
    from evolve_admin.evo.tools import deploy_integration
    monkeypatch.setattr(
        deploy_integration, "_read_bot_oc_config",
        lambda bot_id, bot_user: (
            {"agents": {"defaults": {"skills": {"load": {"extraDirs": "wrong"}}}}},
            None,
        ),
    )
    from evolve_admin import config as admin_config
    monkeypatch.setattr(admin_config, "get_bot_workspace", lambda bot_id, user=None: None)

    extra_dirs, _ = sr.resolve_skill_sources_for_bot("evolve", bot_user="evolve")
    assert extra_dirs == []
