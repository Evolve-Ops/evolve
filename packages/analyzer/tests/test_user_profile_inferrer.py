"""Tests for user_profile_inferrer.

Covers:
- extractor: parses model output, applies confidence/section coercion
- filter_passing: per-section thresholds work
- hook: DNT short-circuit, mismatched-bot guard, fact application,
  Audit Log entries, idempotence on re-run
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from user_profile import (  # noqa: E402
    AUDIT_LOG_SECTION,
    UserProfile,
    UserProfileFrontmatter,
    load_profile,
    save_profile,
)
from generators.user_profile_inferrer.extractor import (  # noqa: E402
    DEFAULT_CONFIDENCE_THRESHOLDS,
    FactCandidate,
    extract_facts,
    filter_passing,
)
from generators.user_profile_inferrer.hook import run_hook  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Stub LLMs
# ─────────────────────────────────────────────────────────────────────────────


def _stub_llm(payload: dict | str):
    """Build an LLMCallable that returns a fixed payload, ignoring inputs."""
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def _call(system: str, user_msg: str, model: str, max_tokens: int) -> str:
        return text

    return _call


def _failing_llm(exc=None):
    def _call(system: str, user_msg: str, model: str, max_tokens: int) -> str:
        raise (exc or RuntimeError("boom"))

    return _call


# ─────────────────────────────────────────────────────────────────────────────
# extract_facts
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_facts_returns_empty_for_empty_transcript():
    facts = extract_facts(
        "",
        current_sections={},
        llm_call=_stub_llm({"facts": []}),
    )
    assert facts == []


def test_extract_facts_parses_well_formed_json():
    payload = {
        "facts": [
            {
                "section": "Vocation",
                "fact": "Software engineer",
                "confidence": 0.9,
                "rationale": "user said so",
            },
            {
                "section": "Goals",
                "fact": "Ship the book",
                "confidence": 0.8,
                "rationale": "stated in turn 4",
            },
        ]
    }
    facts = extract_facts(
        "user: I'm a software engineer.",
        current_sections={},
        llm_call=_stub_llm(payload),
    )
    assert len(facts) == 2
    assert facts[0].section == "Vocation"
    assert facts[0].confidence == 0.9
    assert facts[0].source == "inferrer"


def test_extract_facts_drops_unknown_sections():
    """Model wandering off-prompt to a non-canonical section gets dropped."""
    payload = {
        "facts": [
            {"section": "Hobbies", "fact": "rock climbing", "confidence": 0.9},
            {"section": "Vocation", "fact": "engineer", "confidence": 0.9},
        ]
    }
    facts = extract_facts("x", {}, llm_call=_stub_llm(payload))
    assert len(facts) == 1
    assert facts[0].section == "Vocation"


def test_extract_facts_drops_empty_fact_text():
    payload = {
        "facts": [
            {"section": "Vocation", "fact": "", "confidence": 0.9},
            {"section": "Vocation", "fact": "engineer", "confidence": 0.9},
        ]
    }
    facts = extract_facts("x", {}, llm_call=_stub_llm(payload))
    assert len(facts) == 1


def test_extract_facts_clamps_confidence_to_unit_interval():
    payload = {
        "facts": [
            {"section": "Vocation", "fact": "a", "confidence": 1.5},
            {"section": "Vocation", "fact": "b", "confidence": -0.2},
        ]
    }
    facts = extract_facts("x", {}, llm_call=_stub_llm(payload))
    assert facts[0].confidence == 1.0
    assert facts[1].confidence == 0.0


def test_extract_facts_handles_fenced_json():
    text = "```json\n" + json.dumps({"facts": [{"section": "Goals", "fact": "x", "confidence": 0.8}]}) + "\n```"
    facts = extract_facts("x", {}, llm_call=_stub_llm(text))
    assert len(facts) == 1
    assert facts[0].fact == "x"


def test_extract_facts_returns_empty_on_garbage_response():
    facts = extract_facts(
        "x", {}, llm_call=_stub_llm("this is not JSON at all"),
    )
    assert facts == []


def test_extract_facts_returns_empty_on_llm_failure():
    facts = extract_facts(
        "x", {}, llm_call=_failing_llm(),
    )
    assert facts == []


def test_extract_facts_ignores_facts_key_missing():
    facts = extract_facts(
        "x", {}, llm_call=_stub_llm({"something_else": []}),
    )
    assert facts == []


def test_extract_facts_passes_current_profile_to_prompt():
    """Sanity: the user message contains existing sections so the model
    can avoid re-extracting facts already in the profile."""
    seen_user_msgs: list[str] = []

    def _capture(system: str, user_msg: str, model: str, max_tokens: int) -> str:
        seen_user_msgs.append(user_msg)
        return json.dumps({"facts": []})

    extract_facts(
        "user: hi",
        current_sections={"Vocation": "Software engineer"},
        llm_call=_capture,
    )
    assert "Software engineer" in seen_user_msgs[0]


# ─────────────────────────────────────────────────────────────────────────────
# filter_passing
# ─────────────────────────────────────────────────────────────────────────────


def test_filter_passing_uses_per_section_thresholds():
    candidates = [
        FactCandidate(section="Vocation", fact="x", confidence=0.71),  # 0.70 → pass
        FactCandidate(section="Family", fact="y", confidence=0.71),    # 0.85 → fail
        FactCandidate(section="Family", fact="z", confidence=0.86),    # 0.85 → pass
    ]
    out = filter_passing(candidates)
    assert [c.fact for c in out] == ["x", "z"]


def test_filter_passing_default_for_unknown_section():
    """Unknown section uses the 0.70 fallback (handled in passes_threshold)."""
    candidates = [FactCandidate(section="Whatever", fact="x", confidence=0.71)]
    assert len(filter_passing(candidates)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# run_hook — DNT
# ─────────────────────────────────────────────────────────────────────────────


def test_hook_dnt_short_circuits_before_llm(tmp_path):
    """The LLM must NOT be called when tracking_enabled=false."""
    fm = UserProfileFrontmatter(
        user_key="alice", bot_id="team_bot_a", tracking_enabled=False
    )
    save_profile(UserProfile(frontmatter=fm), tmp_path)

    llm_calls = []

    def _spy(system, user_msg, model, max_tokens):
        llm_calls.append(1)
        return json.dumps({"facts": []})

    summary = run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="user: I'm vegetarian",
        llm_call=_spy,
    )
    assert summary["skipped"] == "dnt_enabled"
    assert llm_calls == []  # NOT called


def test_hook_skips_empty_transcript(tmp_path):
    summary = run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="   \n  ",
        llm_call=_stub_llm({"facts": []}),
    )
    assert summary["skipped"] == "empty_transcript"


# ─────────────────────────────────────────────────────────────────────────────
# run_hook — bot_id mismatch guard
# ─────────────────────────────────────────────────────────────────────────────


def test_hook_refuses_to_write_on_bot_id_mismatch(tmp_path):
    """Privacy guard: if a profile says bot_id=team_bot_c but the hook claims
    bot_id=team_bot_a, refuse to write. Mismatch should never happen in
    production but defensive refusal is the right default."""
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_c")
    save_profile(UserProfile(frontmatter=fm), tmp_path)

    summary = run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="user: I'm vegetarian",
        llm_call=_stub_llm({"facts": [{"section": "Vocation", "fact": "x", "confidence": 0.9}]}),
    )
    assert summary["skipped"] == "bot_id_mismatch"
    # Profile must be unchanged
    loaded = load_profile(tmp_path, "alice")
    assert loaded.frontmatter.bot_id == "team_bot_c"
    assert (loaded.sections.get("Vocation") or "").strip() == ""


# ─────────────────────────────────────────────────────────────────────────────
# run_hook — happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_hook_creates_profile_on_first_run(tmp_path):
    """No profile exists yet → hook creates one and writes facts into it."""
    payload = {
        "facts": [
            {"section": "Vocation", "fact": "Software engineer", "confidence": 0.9},
        ]
    }
    summary = run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="user: I work as a software engineer.",
        llm_call=_stub_llm(payload),
    )
    assert summary["facts_written"] == 1
    loaded = load_profile(tmp_path, "alice")
    assert loaded is not None
    assert "Software engineer" in loaded.sections["Vocation"]


def test_hook_appends_to_existing_section(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm, sections={"Interests": "- rock climbing"})
    save_profile(profile, tmp_path)

    payload = {
        "facts": [
            {"section": "Interests", "fact": "Rust programming", "confidence": 0.9},
        ]
    }
    run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="...",
        llm_call=_stub_llm(payload),
    )
    loaded = load_profile(tmp_path, "alice")
    body = loaded.sections["Interests"]
    assert "rock climbing" in body
    assert "Rust programming" in body


def test_hook_filters_low_confidence(tmp_path):
    """Family section requires 0.85+. A 0.80-confidence Family fact is dropped."""
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(UserProfile(frontmatter=fm), tmp_path)

    payload = {
        "facts": [
            {"section": "Family", "fact": "Has two kids", "confidence": 0.80},
            {"section": "Family", "fact": "Married", "confidence": 0.90},
        ]
    }
    summary = run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="...",
        llm_call=_stub_llm(payload),
    )
    assert summary["facts_extracted"] == 2
    assert summary["facts_written"] == 1
    loaded = load_profile(tmp_path, "alice")
    body = loaded.sections["Family"]
    assert "Married" in body
    assert "Has two kids" not in body


def test_hook_writes_audit_log_entries(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(UserProfile(frontmatter=fm), tmp_path)

    payload = {
        "facts": [
            {"section": "Vocation", "fact": "Engineer", "confidence": 0.9},
        ]
    }
    run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="...",
        llm_call=_stub_llm(payload),
    )
    loaded = load_profile(tmp_path, "alice")
    audit = loaded.sections.get(AUDIT_LOG_SECTION, "")
    assert "inferrer" in audit
    assert "Vocation" in audit
    assert "Engineer" in audit
    assert "0.90" in audit  # confidence formatted to 2 decimals


def test_hook_returns_section_writes_summary(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(UserProfile(frontmatter=fm), tmp_path)

    payload = {
        "facts": [
            {"section": "Vocation", "fact": "a", "confidence": 0.9},
            {"section": "Vocation", "fact": "b", "confidence": 0.9},
            {"section": "Goals", "fact": "c", "confidence": 0.9},
        ]
    }
    summary = run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="...",
        llm_call=_stub_llm(payload),
    )
    assert summary["section_writes"] == {"Vocation": 2, "Goals": 1}


def test_hook_idempotent_when_model_skips_dupes(tmp_path):
    """Run the hook twice. Second run, the model sees the existing fact in
    the profile (passed in `current_sections`) and returns []. Profile is
    unchanged on second run.

    This verifies the idempotence contract: the hook itself doesn't
    de-duplicate; it relies on the model honoring the 'do not re-extract
    these' instruction. The test fakes that behavior with a switched stub.
    """
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(UserProfile(frontmatter=fm), tmp_path)

    first_payload = {
        "facts": [{"section": "Vocation", "fact": "Engineer", "confidence": 0.9}]
    }
    second_payload = {"facts": []}  # well-behaved model returns nothing new

    run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="session 1",
        llm_call=_stub_llm(first_payload),
    )
    run_hook(
        bot_id="team_bot_a",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="session 2",
        llm_call=_stub_llm(second_payload),
    )
    loaded = load_profile(tmp_path, "alice")
    body = loaded.sections["Vocation"]
    # Engineer appears exactly once
    assert body.count("Engineer") == 1


# ─────────────────────────────────────────────────────────────────────────────
# run_hook — multi-user isolation (Team_bot_c)
# ─────────────────────────────────────────────────────────────────────────────


def test_hook_writes_to_correct_user_only(tmp_path):
    """In a multi-user bot, facts about Alice land in Alice's profile and
    DO NOT cross over to Bob's profile."""
    save_profile(
        UserProfile(frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_c")),
        tmp_path,
    )
    save_profile(
        UserProfile(frontmatter=UserProfileFrontmatter(user_key="bob", bot_id="team_bot_c")),
        tmp_path,
    )

    payload = {
        "facts": [{"section": "Vocation", "fact": "Rancher", "confidence": 0.9}]
    }
    run_hook(
        bot_id="team_bot_c",
        user_key="alice",
        home_dir=tmp_path,
        transcript_text="...",
        llm_call=_stub_llm(payload),
    )

    alice = load_profile(tmp_path, "alice")
    bob = load_profile(tmp_path, "bob")
    assert "Rancher" in (alice.sections.get("Vocation") or "")
    assert "Rancher" not in (bob.sections.get("Vocation") or "")
