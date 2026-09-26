"""Tests for negative_path_tests — constraint-derived test generation.

PR 6 of spec-forge-side-effects-2026-06-02.md §13.3. MVP scope: regex-
based pattern detection over constraints + shell skeleton generation.

Focus areas:
  * Pattern detection: "fail silently when X unreachable",
    "configurable X via bot config", and miss cases (no pattern match).
  * Skeleton generation: structure, escape safety, dependency-name
    interpolation.
  * Append-to-test-command: idempotency (re-running doesn't accumulate
    stale blocks), no-op when no tests, separation from existing
    test_command body.
  * The two specific ea-pack 2026-06-02 cases: "fail silently with log
    entry when gateway unreachable" and "Configurable timing for all
    scheduled behaviors via bot config".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.negative_path_tests import (  # noqa: E402
    NegativePathTest,
    append_to_test_command,
    extract_negative_path_tests,
)


# ── Pattern detection ──────────────────────────────────────────────────────


def test_extract_fail_silently_when_gateway_unreachable():
    """The exact ea-pack 2026-06-02 constraint that the smoke test missed."""
    manifest = {
        "constraints": {
            "boundaries": [
                "Fail silently with log entry when the gateway is unreachable",
            ],
        },
    }
    tests = extract_negative_path_tests(manifest)
    assert len(tests) == 1
    assert tests[0].family == "fail_silently"
    assert tests[0].constraint_source == "constraints.boundaries"
    assert tests[0].constraint_index == 0
    assert "gateway" in tests[0].shell_snippet.lower()
    # Snippet contains the exit-0 expectation.
    assert "exit 0" in tests[0].shell_snippet


def test_extract_configurable_timing_via_bot_config():
    """The other smoking-gun constraint: 'Configurable timing for all
    scheduled behaviors via bot config'."""
    manifest = {
        "identity": {
            "scope_includes": [
                "Configurable timing for all scheduled behaviors via bot config",
            ],
        },
    }
    tests = extract_negative_path_tests(manifest)
    assert len(tests) == 1
    assert tests[0].family == "configurable"
    assert tests[0].constraint_source == "identity.scope_includes"
    assert "EVOLVE_BOT_CONFIG_OVERRIDE" in tests[0].shell_snippet


def test_extract_multiple_constraints_yields_multiple_tests():
    manifest = {
        "constraints": {
            "boundaries": [
                "Fail silently when the messaging API is unreachable",
                "Use atomic writes only",                               # no match
                "Fall back gracefully when the database is offline",
            ],
            "safety": [
                "Never delete tasks, mark cancelled instead",            # no match
            ],
        },
        "identity": {
            "scope_includes": [
                "Configurable retry budget via bot config",
            ],
        },
    }
    tests = extract_negative_path_tests(manifest)
    families = {t.family for t in tests}
    assert "fail_silently" in families
    assert "configurable" in families
    # 2 fail_silently + 1 configurable
    assert sum(1 for t in tests if t.family == "fail_silently") == 2
    assert sum(1 for t in tests if t.family == "configurable") == 1


@pytest.mark.parametrize("text", [
    "Atomic writes only",
    "Never delete tasks",
    "Always validate input",
    "Uses Python 3.11",
    "Completes in under 2 seconds",
])
def test_extract_does_not_match_unrelated_constraints(text):
    """Pure-positive constraints that don't fit the X-when-Y shape don't
    produce false-positive negative-path tests."""
    manifest = {"constraints": {"boundaries": [text]}}
    assert extract_negative_path_tests(manifest) == []


def test_extract_handles_missing_sections():
    assert extract_negative_path_tests({}) == []
    assert extract_negative_path_tests({"constraints": {}}) == []
    assert extract_negative_path_tests({"identity": {"scope_includes": []}}) == []


def test_extract_handles_non_list_sections_defensively():
    manifest = {
        "constraints": "not a dict",
        "identity": {"scope_includes": "not a list"},
    }
    assert extract_negative_path_tests(manifest) == []


def test_extract_skips_empty_and_non_string_items():
    manifest = {
        "constraints": {
            "boundaries": ["", None, 42, "Fail silently when api is unreachable"],
        },
    }
    tests = extract_negative_path_tests(manifest)
    assert len(tests) == 1
    # The matched item's index is its position in the original list (3),
    # not the post-filter position.
    assert tests[0].constraint_index == 3


def test_extract_one_match_per_item_no_double_up():
    """A constraint hit by two regex variants doesn't produce two tests."""
    manifest = {
        "constraints": {
            "boundaries": [
                # Matches both _FAIL_SILENTLY_RE and _NO_EXCEPTION_RE shapes
                "Fail silently — no exception when gateway is unreachable",
            ],
        },
    }
    tests = extract_negative_path_tests(manifest)
    assert len(tests) == 1


# ── Regex pattern variants ─────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Fail silently when the gateway is unreachable",
    "Fall back gracefully when the gateway is unavailable",
    "Degrade silently when the gateway is down",
    "Fail cleanly when the gateway is offline",
    "fail silently when GATEWAY is missing",  # case-insensitive
])
def test_fail_silently_variant_patterns_all_match(text):
    manifest = {"constraints": {"boundaries": [text]}}
    assert len(extract_negative_path_tests(manifest)) == 1


@pytest.mark.parametrize("text", [
    "No exception when the api is unreachable",
    "Don't raise when the database is offline",
    "Don't crash when the integration is down",
])
def test_no_exception_variant_patterns_all_match(text):
    manifest = {"constraints": {"boundaries": [text]}}
    assert len(extract_negative_path_tests(manifest)) == 1


@pytest.mark.parametrize("text", [
    "Configurable timing via bot config",
    "Overridable retry budget via the bot config",
    "Adjustable cron schedule using bot-config",
    "configurable Output channel via bot config",
])
def test_configurable_variant_patterns_all_match(text):
    manifest = {"identity": {"scope_includes": [text]}}
    assert len(extract_negative_path_tests(manifest)) == 1


# ── Append to test_command ─────────────────────────────────────────────────


def test_append_is_noop_when_no_tests():
    """No matched patterns → no change to test_command (avoids manifest
    churn from generated empty blocks)."""
    original = "set -e\npython3 -m pytest tests/\n"
    assert append_to_test_command(original, []) == original


def test_append_adds_marked_block_to_existing_command():
    tests = [NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=0,
        constraint_text="Fail silently when API is unreachable",
        family="fail_silently",
        shell_snippet="echo 'mock api unreachable'\nexit 0\n",
    )]
    result = append_to_test_command("python3 -m pytest tests/", tests)
    assert "python3 -m pytest tests/" in result
    assert "BEGIN forge negative-path tests" in result
    assert "END forge negative-path tests" in result
    assert "Fail silently when API is unreachable" in result


def test_append_to_empty_test_command():
    tests = [NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=0,
        constraint_text="x", family="fail_silently",
        shell_snippet="exit 0\n",
    )]
    result = append_to_test_command("", tests)
    assert result.startswith("# --- BEGIN forge negative-path tests")
    assert "exit 0" in result


def test_append_is_idempotent_on_repeat_call():
    """Re-running with the same tests replaces the existing block rather
    than stacking duplicates."""
    tests = [NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=0,
        constraint_text="x", family="fail_silently",
        shell_snippet="exit 0\n",
    )]
    first = append_to_test_command("python3 -m pytest tests/", tests)
    second = append_to_test_command(first, tests)
    # The marker should appear exactly once.
    assert second.count("BEGIN forge negative-path tests") == 1
    assert second.count("END forge negative-path tests") == 1


def test_append_replaces_stale_block_with_new_tests():
    """When the constraint set has CHANGED between forge runs, the new
    block replaces the old one — operators don't have to manually prune."""
    old_tests = [NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=0,
        constraint_text="old constraint", family="fail_silently",
        shell_snippet="echo 'old'; exit 0\n",
    )]
    new_tests = [NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=0,
        constraint_text="new constraint", family="fail_silently",
        shell_snippet="echo 'new'; exit 0\n",
    )]
    first = append_to_test_command("python3 -m pytest", old_tests)
    second = append_to_test_command(first, new_tests)
    assert "old constraint" not in second
    assert "new constraint" in second


def test_append_preserves_existing_body_byte_identical():
    """The pre-block content is untouched (no whitespace normalization)."""
    original = "set -e\n\nexport FOO=bar\npython3 -m pytest tests/\n"
    tests = [NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=0,
        constraint_text="x", family="fail_silently",
        shell_snippet="exit 0\n",
    )]
    result = append_to_test_command(original, tests)
    # The body starts the same way it did.
    assert result.startswith("set -e\n\nexport FOO=bar\npython3 -m pytest tests/")


# ── Generated skeleton shape ────────────────────────────────────────────────


def test_fail_silently_snippet_contains_todo_marker():
    """The skeleton tells the bot LLM exactly where to fill in."""
    manifest = {"constraints": {"boundaries": [
        "Fail silently when the gateway is unreachable",
    ]}}
    tests = extract_negative_path_tests(manifest)
    snippet = tests[0].shell_snippet
    assert "TODO(forge)" in snippet
    assert "gateway" in snippet.lower()


def test_configurable_snippet_contains_two_todos():
    """The configurable family has two TODOs: the override key/value and
    the assertion against the output."""
    manifest = {"identity": {"scope_includes": [
        "Configurable timing via bot config",
    ]}}
    tests = extract_negative_path_tests(manifest)
    snippet = tests[0].shell_snippet
    assert snippet.count("TODO(forge)") >= 2


def test_snippet_uses_shell_safe_variable_suffix():
    """A dependency name with spaces / special chars gets safely
    normalised for shell variable use."""
    manifest = {"constraints": {"boundaries": [
        "Fail silently when the openai api is unreachable",
    ]}}
    tests = extract_negative_path_tests(manifest)
    snippet = tests[0].shell_snippet
    # Should contain a shell variable like SIMULATED_UNREACHABLE_openai_api
    # — not "SIMULATED_UNREACHABLE_openai api" with a space.
    assert " " not in [
        line.split("=")[0] for line in snippet.splitlines()
        if line.startswith("SIMULATED_UNREACHABLE_")
    ][0]


def test_finding_to_dict_round_trip():
    t = NegativePathTest(
        constraint_source="constraints.boundaries", constraint_index=2,
        constraint_text="x", family="fail_silently",
        shell_snippet="exit 0\n",
    )
    d = t.to_dict()
    assert d["constraint_source"] == "constraints.boundaries"
    assert d["constraint_index"] == 2
    assert d["family"] == "fail_silently"
    assert d["shell_snippet"] == "exit 0\n"
