"""Tests for constraint_critic — LLM verifier that maps each declared
constraint to an implementation site.

PR 6 of spec-forge-side-effects-2026-06-02.md §13.2.

The LLM call is mocked via the ``call_llm`` parameter — these tests
focus on extraction (pure), prompt assembly, response parsing, and
finding shape. Mocking is a callable injection rather than ``unittest.mock``
patch so the test reads as a contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.constraint_critic import (  # noqa: E402
    ConstraintFinding,
    ConstraintItem,
    _extract_customization_guidance,
    extract_constraint_items,
    verify_constraints,
)


# ── Extraction (pure) ───────────────────────────────────────────────────────


def test_extract_pulls_boundaries_safety_and_scope():
    manifest = {
        "constraints": {
            "boundaries": [
                "Fail silently with log entry when gateway unreachable",
                "Never write to /etc",
            ],
            "safety": ["Atomic writes only"],
        },
        "identity": {
            "scope_includes": [
                "Configurable timing for all scheduled behaviors via bot config",
            ],
        },
    }
    items = extract_constraint_items(manifest)
    sources = {(it.source, it.index) for it in items}
    assert ("constraints.boundaries", 0) in sources
    assert ("constraints.boundaries", 1) in sources
    assert ("constraints.safety", 0) in sources
    assert ("identity.scope_includes", 0) in sources
    assert len(items) == 4


def test_extract_skips_non_string_and_empty():
    manifest = {
        "constraints": {
            "boundaries": ["real boundary", "", 42, None, "  "],
        },
    }
    items = extract_constraint_items(manifest)
    assert [it.text for it in items] == ["real boundary"]


def test_extract_handles_missing_sections():
    """No constraints + no identity at all → no work."""
    assert extract_constraint_items({}) == []
    assert extract_constraint_items({"constraints": {}}) == []
    assert extract_constraint_items({"identity": {}}) == []


def test_extract_handles_malformed_sections():
    """Defensive: non-list / non-dict shapes don't crash."""
    manifest = {
        "constraints": "this is a string not a dict",
        "identity": ["this is a list not a dict"],
    }
    assert extract_constraint_items(manifest) == []


# ── Customization guidance injection (regression vs PR 0 audit) ─────────────


def test_extract_customization_guidance_basic():
    spec = (
        "## Overview\n\nBuild a thing.\n\n"
        "## Customization Guidance\n\n"
        "Adapt TAG_ALIASES for the bot's domain.\n\n"
        "## Provenance\n\nEmbed markers.\n"
    )
    out = _extract_customization_guidance(spec)
    assert "TAG_ALIASES" in out
    assert "Provenance" not in out


def test_extract_customization_guidance_empty_when_missing():
    assert _extract_customization_guidance("") == ""
    assert _extract_customization_guidance("## Overview\n\nx\n") == ""
    assert _extract_customization_guidance(None) == ""  # type: ignore[arg-type]


# ── Verification (LLM-driven, mocked) ──────────────────────────────────────


def _llm_fixture(canned_response: str):
    """Build a ``call_llm`` callable that returns the canned response
    and records each invocation for inspection."""
    calls: list[tuple[str, str]] = []

    def fake_llm(system_prompt: str, user_message: str) -> str:
        calls.append((system_prompt, user_message))
        return canned_response

    fake_llm.calls = calls  # type: ignore[attr-defined]
    return fake_llm


def test_verify_returns_one_finding_per_item():
    manifest = {
        "constraints": {
            "boundaries": ["Fail silently", "Atomic writes"],
        },
    }
    canned = '[{"verdict":"enforced","evidence":"scripts/x.py:42 catch block"},' \
             '{"verdict":"absent","evidence":""}]'
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert len(findings) == 2
    assert findings[0].verdict == "enforced"
    assert findings[1].verdict == "absent"
    assert findings[0].severity == "info"
    assert findings[1].severity == "should-fix"


def test_verify_smoking_gun_ea_config_pattern():
    """The actual case from the personal-bot ea-pack 2026-06-02 audit:
    'Configurable timing for all scheduled behaviors via bot config' was
    declared, but the code hardcoded times. critic verdict: absent."""
    manifest = {
        "identity": {
            "scope_includes": [
                "Configurable timing for all scheduled behaviors via bot config",
            ],
        },
    }
    files = {
        "scripts/morning_brief.py": "import schedule\nschedule.every().day.at('07:00').do(brief)\n",
    }
    canned = '[{"verdict":"absent","evidence":"no code reads bot config to override timing"}]'
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, files, llm)
    assert len(findings) == 1
    assert findings[0].verdict == "absent"
    assert findings[0].severity == "should-fix"
    assert findings[0].source == "identity.scope_includes"


def test_verify_passes_files_into_prompt():
    """The user_message must carry the implementation files so the LLM
    has something to grep against. Otherwise the verdict is always
    'unclear'."""
    manifest = {"constraints": {"boundaries": ["X"]}}
    files = {"scripts/x.py": "def x():\n    pass\n"}
    llm = _llm_fixture('[{"verdict":"enforced","evidence":"scripts/x.py:1"}]')
    verify_constraints(manifest, files, llm)
    # Inspect what got sent
    _system, user = llm.calls[0]
    assert "scripts/x.py" in user
    assert "def x()" in user


def test_verify_truncates_oversized_files():
    """Files bigger than the prompt budget get head-truncated with a marker
    so a single bloated file doesn't blow the context window."""
    manifest = {"constraints": {"boundaries": ["X"]}}
    files = {"scripts/big.py": "# line " * 5000}  # ~30 KB
    llm = _llm_fixture('[{"verdict":"unclear","evidence":""}]')
    verify_constraints(manifest, files, llm)
    _, user = llm.calls[0]
    assert "[truncated]" in user


def test_verify_injects_customization_guidance():
    """The customization_guidance field is in the user_message so the
    critic doesn't flag spec-blessed customizations as 'absent'."""
    manifest = {
        "constraints": {"boundaries": ["Use canonical category list"]},
        "build_spec": (
            "## Overview\n\nBuild it.\n\n"
            "## Customization Guidance\n\n"
            "Categories — replace with bot-specific list.\n"
        ),
    }
    llm = _llm_fixture('[{"verdict":"enforced","evidence":""}]')
    verify_constraints(manifest, {}, llm)
    _, user = llm.calls[0]
    assert "Customization Guidance" in user
    assert "Categories" in user


# ── Response parsing edge cases ─────────────────────────────────────────────


def test_verify_handles_fenced_response():
    manifest = {"constraints": {"boundaries": ["X"]}}
    canned = '```json\n[{"verdict":"enforced","evidence":"x.py:1"}]\n```'
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert findings[0].verdict == "enforced"


def test_verify_handles_prose_wrapped_response():
    """LLM sometimes forgets the 'no preamble' instruction."""
    manifest = {"constraints": {"boundaries": ["X"]}}
    canned = 'Here are the verdicts:\n[{"verdict":"absent"}]\n\nLet me know!'
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert findings[0].verdict == "absent"


def test_verify_handles_malformed_response_as_unclear():
    """If the LLM emits invalid JSON, every item defaults to ``unclear``
    rather than blocking the forge."""
    manifest = {"constraints": {"boundaries": ["X", "Y"]}}
    canned = "not json at all"
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert len(findings) == 2
    for f in findings:
        assert f.verdict == "unclear"


def test_verify_pads_too_short_response_array():
    """LLM returned fewer entries than items → pad with unclear so each
    item has a finding."""
    manifest = {"constraints": {"boundaries": ["A", "B", "C"]}}
    canned = '[{"verdict":"enforced"}]'   # only 1 entry for 3 items
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert len(findings) == 3
    assert findings[0].verdict == "enforced"
    assert findings[1].verdict == "unclear"
    assert findings[2].verdict == "unclear"


def test_verify_truncates_too_long_response_array():
    """LLM returned more entries than items → ignore the extras."""
    manifest = {"constraints": {"boundaries": ["A"]}}
    canned = '[{"verdict":"enforced"},{"verdict":"absent"}]'
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert len(findings) == 1


def test_verify_unknown_verdict_string_defaults_to_unclear():
    manifest = {"constraints": {"boundaries": ["X"]}}
    canned = '[{"verdict":"maybe","evidence":""}]'
    llm = _llm_fixture(canned)
    findings = verify_constraints(manifest, {}, llm)
    assert findings[0].verdict == "unclear"


def test_verify_llm_exception_returns_unclear_for_all():
    """LLM call raises (network error, etc.) → critic surfaces unclear
    rather than blocking the forge. Operator sees the error in evidence."""
    manifest = {"constraints": {"boundaries": ["X", "Y"]}}

    def crashing_llm(s, u):
        raise RuntimeError("api timeout")

    findings = verify_constraints(manifest, {}, crashing_llm)
    assert len(findings) == 2
    for f in findings:
        assert f.verdict == "unclear"
        assert f.severity == "info"
        assert "api timeout" in f.evidence


def test_verify_with_no_constraints_returns_empty():
    """No constraints → no work → no LLM call."""
    manifest = {"identity": {}, "constraints": {}}
    called = False

    def llm(s, u):
        nonlocal called
        called = True
        return "[]"

    findings = verify_constraints(manifest, {}, llm)
    assert findings == []
    assert called is False


def test_finding_to_dict_round_trip():
    finding = ConstraintFinding(
        source="constraints.boundaries", index=0, text="Fail silently",
        verdict="absent", evidence="no code found",
    )
    d = finding.to_dict()
    assert d["source"] == "constraints.boundaries"
    assert d["index"] == 0
    assert d["text"] == "Fail silently"
    assert d["verdict"] == "absent"
    assert d["severity"] == "should-fix"


# ── verify_privacy_block (manifest-v7 Slice 2) ───────────────────────────────


def test_privacy_block_absent_skips_llm():
    from evolve_admin.applications.constraint_critic import verify_privacy_block
    called = False

    def llm(system, user):
        nonlocal called
        called = True
        return "{}"

    assert verify_privacy_block({"privacy": {}}, {}, llm) == []
    assert verify_privacy_block({}, {}, llm) == []
    assert called is False


def test_privacy_verdicts_and_undeclared_collections():
    import json as _json
    from evolve_admin.applications.constraint_critic import verify_privacy_block

    manifest = {
        "privacy": {
            "user_data_collected": ["intake_log", "weight"],
            "shareable_in_lessons": False,
        },
        "build_spec": "Track protein intake",
    }

    def llm(system, user):
        payload = _json.loads(user)
        assert payload["declared_items"] == ["intake_log", "weight"]
        return _json.dumps({
            "declared_verdicts": [
                {"verdict": "present", "evidence": "scripts/ingest.py:10"},
                {"verdict": "not_found", "evidence": ""},
            ],
            "undeclared_collections": [
                {"description": "logs sender telegram user id",
                 "evidence": "scripts/ingest.py:22"},
            ],
        })

    findings = verify_privacy_block(manifest, {"scripts/ingest.py": "..."}, llm)
    kinds = [f.kind for f in findings]
    assert kinds == ["declared_present", "declared_not_found", "undeclared_collection"]
    undeclared = findings[-1]
    assert undeclared.severity == "should-fix"
    assert "telegram user id" in undeclared.text
    # Over-declaration stays informational.
    assert findings[1].severity == "info"


def test_privacy_llm_failure_degrades_to_unclear():
    from evolve_admin.applications.constraint_critic import verify_privacy_block

    def llm(system, user):
        raise TimeoutError("llm down")

    findings = verify_privacy_block(
        {"privacy": {"user_data_collected": ["intake_log"]}}, {}, llm,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unclear"
    assert findings[0].severity == "info"


def test_privacy_garbage_llm_output_pads_unclear():
    from evolve_admin.applications.constraint_critic import verify_privacy_block

    findings = verify_privacy_block(
        {"privacy": {"user_data_collected": ["a", "b"]}}, {},
        lambda s, u: "not json at all",
    )
    assert [f.kind for f in findings] == ["unclear", "unclear"]
