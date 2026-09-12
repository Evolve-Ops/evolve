"""tests/test_export_engine_stage_0d.py — Stage 0d of the scanned-export pipeline.

Spec: internal/spec-scanned-export-2026-06-02.md section 3.4.

Stage 0d does two passes to verify the derived draft can re-create
the source app:

  1. ``structural_diff`` — pure-Python string-level coverage check.
     Verifies the derived ``build_spec`` mentions every scanned file
     path (or its basename), every CLI command (subcommand-anchored),
     and every key_paths value. Catches obvious deriver drops/renames
     before paying for an LLM round-trip.

  2. ``dry_run_forge`` — LLM call. Given the derived ``build_spec``,
     ask Claude to enumerate the files that a forge run would create.
     The result is compared against the scanned files[] for
     missing / extra / overlap.

``round_trip_validate`` orchestrates both and produces a
:class:`RoundTripResult` with a ``verdict`` field
(``"good" | "drift" | "broken"``) the operator-review UI (S4) can
use to summarise the round-trip at a glance.

This test surface locks:
  - Structural diff hits: files, CLI, key_paths
  - Structural diff misses: full-path match, basename match, data
    files (which are documented as schemas not by name and so are
    exempt)
  - Dry-run prompt + parser: payload format, prose-preamble tolerance,
    empty/non-dict/non-list fallback to None
  - Verdict computation: good / drift / broken thresholds
  - skip_dry_run path keeps structural diff but reports
    ``model="(skipped)"``
  - Orchestrator wires Stage 0d by default + skip flag bypasses
  - export_meta carries round-trip telemetry
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.export_engine import (  # noqa: E402
    DRY_RUN_FORGE_SYSTEM_PROMPT,
    RoundTripResult,
    StructuralFinding,
    _format_dry_run_user_message,
    _parse_dry_run_response,
    build_export_draft,
    dry_run_forge,
    round_trip_validate,
    structural_diff,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _scanned_fixture(**overrides) -> dict:
    """Reuse the same shape as Stage 0a/0b/0c tests for consistency."""
    base = {
        "id": "i-9c16b1c7",
        "name": "Unified Task System",
        "display_name": "Unified Task System",
        "description": "Persistent task management.",
        "files": [
            {"path": "scripts/tasks.py", "role": "build_artifact"},
            {"path": "scripts/task_updater.py", "role": "build_artifact"},
            {"path": "tasks.json", "role": "data_file"},
        ],
        "interface_contract": {
            "cli": [
                {"command": "python3 scripts/tasks.py list", "key_flags": [],
                 "output_signals": []},
                {"command": "python3 scripts/tasks.py check", "key_flags": [],
                 "output_signals": ["TASK_DUE:", "FOLLOWUP_NEEDED:"]},
            ],
            "key_paths": {"active_db": "tasks.json"},
        },
        "identity": {"purpose": "Track todos."},
        "updated_at": "2026-06-02T12:00:00Z",
    }
    base.update(overrides)
    return base


def _build_spec_complete() -> str:
    """A build_spec that mentions every fixture file, CLI, key_path."""
    return (
        "# Task Manager — Build Specification\n\n"
        "## Overview\n\nA flat tag-based task system.\n\n"
        "## File Layout\n\n"
        "  scripts/tasks.py — main CLI\n"
        "  scripts/task_updater.py — bulk helper\n"
        "  tasks.json — active database\n\n"
        "## CLI Commands\n\n"
        "### `python3 scripts/tasks.py list`\n\n"
        "  List tasks.\n\n"
        "### `python3 scripts/tasks.py check`\n\n"
        "  Surface overdue tasks via TASK_DUE: lines.\n"
    )


def _good_dry_run_response() -> str:
    """An LLM response shape that round-trips to the fixture's files[]."""
    return (
        '{"files": ['
        '{"path": "scripts/tasks.py", "purpose": "main CLI"},'
        '{"path": "scripts/task_updater.py", "purpose": "bulk helper"}'
        ']}'
    )


# ── structural_diff ──────────────────────────────────────────────────────────


def test_structural_diff_clean_when_build_spec_covers_everything():
    draft = {"build_spec": _build_spec_complete(),
             "interface_contract": _scanned_fixture()["interface_contract"]}
    findings = structural_diff(_scanned_fixture(), draft)
    assert findings == []


def test_structural_diff_flags_missing_file_in_build_spec():
    """If the deriver forgot one of the scanned scripts, flag it."""
    spec = (
        "# Build Specification\n\n## Overview\n\nDescribed only the main script.\n"
        "## File Layout\n\n  scripts/tasks.py — main CLI\n"
    )
    draft = {"build_spec": spec,
             "interface_contract": _scanned_fixture()["interface_contract"]}
    findings = structural_diff(_scanned_fixture(), draft)
    kinds = {f.kind for f in findings}
    assert "missing_file_in_build_spec" in kinds
    # task_updater.py is the missing one (tasks.py IS in the spec).
    targets = [f.detail for f in findings if f.kind == "missing_file_in_build_spec"]
    assert any("task_updater" in t for t in targets)
    assert not any("scripts/tasks.py" in t for t in targets)


def test_structural_diff_accepts_basename_match_for_files():
    """Deriver may relocate a file (e.g. ops/foo.py -> scripts/foo.py).
    The basename still appears in build_spec — that's not drift."""
    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "ops/tools/unified_task_system.py",
                          "role": "build_artifact"}]
    spec = (
        "# Build Specification\n\n## Overview\n\nA task system.\n"
        "## File Layout\n\n  scripts/unified_task_system.py — main CLI\n"
    )
    findings = structural_diff(manifest,
                               {"build_spec": spec, "interface_contract": {}})
    # Basename matched -> no missing_file finding.
    assert not any(f.kind == "missing_file_in_build_spec" for f in findings)


def test_structural_diff_skips_data_files_from_file_coverage():
    """Data files (e.g. tasks.json populated by use) are not expected
    to be mentioned by literal name in the build_spec under the
    files-coverage check — they're documented under the data-format
    section as schemas. (The key_paths check is independent and still
    runs.)"""
    manifest = _scanned_fixture()  # has tasks.json as data_file
    # Remove tasks.json mention but ALSO from key_paths so we isolate
    # the files-coverage rule under test.
    manifest["interface_contract"] = {
        "cli": manifest["interface_contract"]["cli"],
        "key_paths": {},  # cleared so this test is scoped to files-coverage
    }
    spec_without_data_file = _build_spec_complete().replace("tasks.json", "the active DB")
    findings = structural_diff(
        manifest,
        {"build_spec": spec_without_data_file,
         "interface_contract": manifest["interface_contract"]},
    )
    # No missing_file_in_build_spec finding citing tasks.json.
    assert not any(
        f.kind == "missing_file_in_build_spec" and "tasks.json" in f.detail
        for f in findings
    )


def test_structural_diff_flags_missing_cli_command():
    """If a CLI subcommand from the extracted contract isn't
    mentioned in build_spec, flag it — downstream apps and operator
    docs depend on the surface staying complete."""
    spec = (
        "# Build Specification\n\n## Overview\n\nA task system.\n"
        "## File Layout\n\n  scripts/tasks.py — main CLI\n"
        "  scripts/task_updater.py — bulk helper\n"
        "## CLI Commands\n\n"
        "### `python3 scripts/tasks.py list`\n\nList tasks.\n"
        # 'check' subcommand intentionally missing
    )
    findings = structural_diff(
        _scanned_fixture(),
        {"build_spec": spec,
         "interface_contract": _scanned_fixture()["interface_contract"]},
    )
    kinds = {f.kind for f in findings}
    assert "missing_cli_in_build_spec" in kinds
    misses = [f.detail for f in findings if f.kind == "missing_cli_in_build_spec"]
    assert any("check" in d for d in misses)


def test_structural_diff_flags_missing_key_path():
    """Other apps depend on key_paths anchors (e.g. EA Pack reads
    Task Manager's active_db). Build_spec must mention them so a
    fresh consumer wires the right path."""
    manifest = _scanned_fixture()
    manifest["interface_contract"]["key_paths"] = {"active_db": "ops/tasks/tasks.json"}
    spec = (
        "# Build Specification\n\n## Overview\n\nA task system.\n"
        "## File Layout\n\n  scripts/tasks.py — main CLI\n"
        "  scripts/task_updater.py — bulk helper\n"
        "## CLI Commands\n\n"
        "### `python3 scripts/tasks.py list`\n\nList tasks.\n"
        "### `python3 scripts/tasks.py check`\n\nCheck overdue.\n"
    )
    findings = structural_diff(
        manifest, {"build_spec": spec, "interface_contract": manifest["interface_contract"]},
    )
    kinds = {f.kind for f in findings}
    assert "missing_key_path_in_build_spec" in kinds


def test_structural_diff_flags_completely_missing_build_spec():
    """Empty build_spec is a special-case finding — Stage 0b failed
    and the operator should re-run before anything else."""
    findings = structural_diff(_scanned_fixture(), {"build_spec": ""})
    assert len(findings) == 1
    assert findings[0].kind == "missing_build_spec"


def test_structural_diff_uses_draft_contract_in_preference_to_scanner():
    """When the draft has interface_contract populated by Stage 0c,
    use that for CLI/key_paths checks (not the scanner-stale one)."""
    manifest = _scanned_fixture()
    manifest["interface_contract"] = {"cli": [], "key_paths": {}}
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": {
            "cli": [{"command": "python3 scripts/tasks.py NEWcmd",
                     "key_flags": [], "output_signals": []}],
            "key_paths": {},
        },
    }
    findings = structural_diff(manifest, draft)
    # NEWcmd in draft contract, not in build_spec -> flag.
    assert any("NEWcmd" in f.detail for f in findings)


# ── dry-run user-message + parser ────────────────────────────────────────────


def test_dry_run_user_message_includes_build_spec_body():
    msg = _format_dry_run_user_message("# title\n\nbody text")
    assert "body text" in msg
    assert "Build Specification" in msg
    assert "## Task" in msg
    assert "Return only the JSON object" in msg


def test_dry_run_parser_clean_response():
    files = _parse_dry_run_response(_good_dry_run_response())
    assert isinstance(files, list)
    assert {f["path"] for f in files} == {"scripts/tasks.py", "scripts/task_updater.py"}


def test_dry_run_parser_tolerates_prose_preamble():
    raw = "Here are the files:\n\n" + _good_dry_run_response() + "\n\nLet me know."
    files = _parse_dry_run_response(raw)
    assert files is not None
    assert len(files) == 2


def test_dry_run_parser_empty_input_returns_none():
    assert _parse_dry_run_response("") is None
    assert _parse_dry_run_response("   ") is None


def test_dry_run_parser_non_json_returns_none():
    assert _parse_dry_run_response("I couldn't figure it out.") is None


def test_dry_run_parser_missing_files_key_returns_none():
    """JSON with no top-level files array isn't usable — caller
    treats as failure rather than silently producing empty list."""
    assert _parse_dry_run_response('{"other": "thing"}') is None


def test_dry_run_parser_non_array_files_returns_none():
    assert _parse_dry_run_response('{"files": "not an array"}') is None


def test_dry_run_parser_skips_blank_path_entries():
    raw = '{"files": [{"path": "", "purpose": "blank"}, ' \
          '{"path": "x.py", "purpose": "ok"}]}'
    files = _parse_dry_run_response(raw)
    assert files == [{"path": "x.py", "purpose": "ok"}]


# ── dry_run_forge dispatch ───────────────────────────────────────────────────


def test_dry_run_forge_calls_llm_with_system_and_user_messages():
    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["system"] = system
        captured["user"] = user
        captured["model"] = model
        return _good_dry_run_response()

    files, in_chars, out_chars = dry_run_forge(
        _build_spec_complete(),
        call_llm=fake_llm, model="dryrun-x",
    )
    assert captured["system"] == DRY_RUN_FORGE_SYSTEM_PROMPT
    assert "Task Manager" in captured["user"]
    assert captured["model"] == "dryrun-x"
    assert files is not None
    assert in_chars > 0
    assert out_chars > 0


def test_dry_run_forge_empty_build_spec_short_circuits():
    """No build_spec -> no LLM call -> None files, zero telemetry."""

    def should_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("dry_run_forge should not invoke LLM on empty spec")

    files, in_chars, out_chars = dry_run_forge(
        "", call_llm=should_not_be_called,
    )
    assert files is None
    assert in_chars == 0
    assert out_chars == 0


def test_dry_run_forge_api_key_required_when_no_override():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
        with pytest.raises(ValueError, match="api_key"):
            dry_run_forge(_build_spec_complete())


def test_dry_run_forge_unparseable_response_returns_none():
    fake_llm = lambda *a, **kw: "I don't have enough info."  # noqa: E731
    files, in_chars, out_chars = dry_run_forge(
        _build_spec_complete(), call_llm=fake_llm,
    )
    assert files is None
    assert in_chars > 0  # we DID send a request, telemetry stays accurate
    assert out_chars > 0


# ── round_trip_validate ──────────────────────────────────────────────────────


def test_round_trip_validates_returns_good_on_clean_inputs():
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": _scanned_fixture()["interface_contract"],
    }
    result = round_trip_validate(
        _scanned_fixture(), draft,
        dry_run_kwargs={
            "call_llm": lambda *a, **kw: _good_dry_run_response(),
        },
    )
    assert isinstance(result, RoundTripResult)
    assert result.verdict == "good"
    assert result.structural_findings == []
    assert result.dry_run_missing == []
    assert result.dry_run_extra == []
    assert result.dry_run_failed is False


def test_round_trip_drift_when_dry_run_misses_a_file():
    """Dry-run that omits a scanned file is drift, not broken."""
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": _scanned_fixture()["interface_contract"],
    }
    only_one_file_response = '{"files": [{"path": "scripts/tasks.py", "purpose": "main"}]}'

    result = round_trip_validate(
        _scanned_fixture(), draft,
        dry_run_kwargs={
            "call_llm": lambda *a, **kw: only_one_file_response,
        },
    )
    assert result.verdict == "drift"
    assert result.dry_run_missing == ["scripts/task_updater.py"]
    assert result.dry_run_extra == []
    assert result.dry_run_failed is False


def test_round_trip_drift_when_dry_run_adds_unexpected_file():
    """Extra file in dry-run that the scanner didn't see is drift —
    the deriver may have invented a script that doesn't exist on the
    source bot."""
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": _scanned_fixture()["interface_contract"],
    }
    extra_file_response = (
        '{"files": ['
        '{"path": "scripts/tasks.py", "purpose": "main"},'
        '{"path": "scripts/task_updater.py", "purpose": "bulk"},'
        '{"path": "scripts/imagined.py", "purpose": "not real"}'
        ']}'
    )

    result = round_trip_validate(
        _scanned_fixture(), draft,
        dry_run_kwargs={
            "call_llm": lambda *a, **kw: extra_file_response,
        },
    )
    assert result.verdict == "drift"
    assert result.dry_run_extra == ["scripts/imagined.py"]


def test_round_trip_broken_when_dry_run_fails_to_parse():
    """LLM ran but produced unusable output -> broken (operator
    must re-run or hand-edit before publish)."""
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": _scanned_fixture()["interface_contract"],
    }
    result = round_trip_validate(
        _scanned_fixture(), draft,
        dry_run_kwargs={
            "call_llm": lambda *a, **kw: "I don't know.",
        },
    )
    assert result.verdict == "broken"
    assert result.dry_run_failed is True
    # All scanned files surface as missing on a broken dry-run.
    assert "scripts/tasks.py" in result.dry_run_missing
    assert "scripts/task_updater.py" in result.dry_run_missing


def test_round_trip_broken_when_zero_overlap():
    """Catastrophic: dry-run produced files but none match the
    scanner's. Likely a wrong source manifest was paired with the
    wrong workspace."""
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": _scanned_fixture()["interface_contract"],
    }
    no_overlap = '{"files": [{"path": "completely/different.py", "purpose": "x"}]}'
    result = round_trip_validate(
        _scanned_fixture(), draft,
        dry_run_kwargs={
            "call_llm": lambda *a, **kw: no_overlap,
        },
    )
    assert result.verdict == "broken"


def test_round_trip_skip_dry_run_runs_only_structural_diff():
    """With ``skip_dry_run=True``, the structural pass still runs but
    no LLM call happens. Verdict is structural-only."""

    def should_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("Stage 0d dry-run should not run when skipped")

    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": _scanned_fixture()["interface_contract"],
    }
    result = round_trip_validate(
        _scanned_fixture(), draft,
        skip_dry_run=True,
        dry_run_kwargs={"call_llm": should_not_be_called},
    )
    assert result.verdict == "good"
    assert result.model == "(skipped)"
    assert result.dry_run_files == []
    assert result.input_chars == 0


def test_round_trip_skip_dry_run_still_reports_structural_drift():
    """Even when LLM is skipped, structural-only findings flip the
    verdict to drift."""
    manifest = _scanned_fixture()
    sparse_spec = (
        "# Build Specification\n\n## Overview\n\nOnly main.\n"
        "## File Layout\n\n  scripts/tasks.py — main CLI\n"
        "## CLI Commands\n\n### `python3 scripts/tasks.py list`\n\nList.\n"
        # task_updater.py and check subcommand intentionally missing
    )
    draft = {"build_spec": sparse_spec,
             "interface_contract": manifest["interface_contract"]}
    result = round_trip_validate(manifest, draft, skip_dry_run=True)
    assert result.verdict == "drift"
    assert any(f.kind == "missing_file_in_build_spec"
               for f in result.structural_findings)


def test_round_trip_skips_data_files_in_missing_list():
    """Dry-run typically omits user-data files (e.g. tasks.json) —
    don't flag those as missing."""
    manifest = _scanned_fixture()  # tasks.json is role=data_file
    draft = {
        "build_spec": _build_spec_complete(),
        "interface_contract": manifest["interface_contract"],
    }
    result = round_trip_validate(
        manifest, draft,
        dry_run_kwargs={
            "call_llm": lambda *a, **kw: _good_dry_run_response(),
        },
    )
    assert "tasks.json" not in result.dry_run_missing


# ── Orchestrator wiring (build_export_draft) ─────────────────────────────────


def test_build_export_draft_invokes_stage_0d_by_default(tmp_path: Path):
    """Default path: orchestrator runs Stage 0d, advances
    ``export_stage`` to ``0d``, and attaches ``round_trip`` block."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("# tasks main\n")
    (tmp_path / "scripts" / "task_updater.py").write_text("# bulk\n")

    def fake_build_spec(*a, **kw):
        return _build_spec_complete()

    def fake_extract(*a, **kw):
        return (
            '{"cli": ['
            '{"command": "python3 scripts/tasks.py list",'
            ' "key_flags": [], "output_signals": []},'
            '{"command": "python3 scripts/tasks.py check",'
            ' "key_flags": [], "output_signals": ["TASK_DUE:"]}'
            '], "key_paths": {"active_db": "tasks.json"}}'
        )

    def fake_dry_run(*a, **kw):
        return _good_dry_run_response()

    manifest = _scanned_fixture()
    manifest["files"] = [
        {"path": "scripts/tasks.py", "role": "build_artifact"},
        {"path": "scripts/task_updater.py", "role": "build_artifact"},
    ]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        derive_kwargs={"call_llm": fake_build_spec},
        contract_kwargs={"call_llm": fake_extract},
        round_trip_kwargs={"dry_run_kwargs": {"call_llm": fake_dry_run}},
    )
    assert draft["export_stage"] == "0d"
    assert "round_trip" in draft
    assert draft["round_trip"]["verdict"] == "good"
    assert draft["round_trip"]["dry_run_failed"] is False
    # Telemetry recorded.
    assert draft["export_meta"]["round_trip_verdict"] == "good"
    assert draft["export_meta"]["round_trip_input_chars"] > 0


def test_build_export_draft_skip_round_trip_bypasses_stage_0d(tmp_path: Path):
    """``skip_round_trip=True`` keeps the draft at the earlier stage."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("ok\n")

    def fake_build_spec(*a, **kw):
        return _build_spec_complete()

    def must_not_call(*a, **kw):  # pragma: no cover
        raise AssertionError("Stage 0d should not run when skipped")

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py", "role": "build_artifact"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_interface_contract=True,
        skip_round_trip=True,
        derive_kwargs={"call_llm": fake_build_spec},
        round_trip_kwargs={"dry_run_kwargs": {"call_llm": must_not_call}},
    )
    assert draft["export_stage"] == "0b"
    assert "round_trip" not in draft
    # No round_trip telemetry.
    for key in ("round_trip_model", "round_trip_input_chars",
                "round_trip_output_chars", "round_trip_verdict"):
        assert key not in draft["export_meta"]


def test_build_export_draft_round_trip_kwargs_skip_dry_run(tmp_path: Path):
    """Stage 0d runs in structural-only mode when
    ``round_trip_kwargs={"skip_dry_run": True}`` is set — no LLM
    call for the dry-run, but findings still attach."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("ok\n")
    (tmp_path / "scripts" / "task_updater.py").write_text("ok\n")

    def fake_build_spec(*a, **kw):
        return _build_spec_complete()

    def must_not_call(*a, **kw):  # pragma: no cover
        raise AssertionError("dry_run should not be called when skip_dry_run=True")

    manifest = _scanned_fixture()
    manifest["files"] = [
        {"path": "scripts/tasks.py", "role": "build_artifact"},
        {"path": "scripts/task_updater.py", "role": "build_artifact"},
    ]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_interface_contract=True,
        derive_kwargs={"call_llm": fake_build_spec},
        round_trip_kwargs={
            "skip_dry_run": True,
            "dry_run_kwargs": {"call_llm": must_not_call},
        },
    )
    assert draft["export_stage"] == "0d"
    assert draft["round_trip"]["verdict"] == "good"
    assert draft["round_trip"]["dry_run_files"] == []


def test_build_export_draft_round_trip_broken_flags_in_meta(tmp_path: Path):
    """Broken round-trip (LLM parse failure) surfaces in
    ``export_meta.round_trip_verdict`` for operator triage."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("ok\n")

    def fake_build_spec(*a, **kw):
        return _build_spec_complete()

    def broken_dry_run(*a, **kw):
        return "the model refused to answer"

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py", "role": "build_artifact"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_interface_contract=True,
        derive_kwargs={"call_llm": fake_build_spec},
        round_trip_kwargs={"dry_run_kwargs": {"call_llm": broken_dry_run}},
    )
    assert draft["round_trip"]["dry_run_failed"] is True
    assert draft["round_trip"]["verdict"] == "broken"
    assert draft["export_meta"]["round_trip_verdict"] == "broken"
