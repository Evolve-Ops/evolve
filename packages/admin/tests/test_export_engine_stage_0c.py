"""tests/test_export_engine_stage_0c.py — Stage 0c of the scanned-export pipeline.

Spec: internal/spec-scanned-export-2026-06-02.md section 3.3.

Stage 0c reuses the canonical ``BUILTIN_EXTRACTOR_PROMPT`` from
``forge_engine`` so the ``interface_contract`` shape an export
produces is structurally identical to what forge stamps on a forged
manifest. The test surface here locks:

  * The user message is built from the concatenated source files with
    per-file ``### `path``` headers (so the extractor LLM can ground
    each CLI/schema/key_path against its source).
  * The extractor prompt from ``forge_engine`` is used verbatim (not a
    re-typed near-copy that could drift over time).
  * On parse success, the LLM dict surfaces in the manifest with
    ``populated_by_export=True``, ``populated_by_forge=False``, and an
    ``extracted_at`` timestamp.
  * On parse failure (LLM returns prose, or empty, or non-JSON), the
    deriver gracefully falls back to the scanner's
    ``interface_contract`` rather than blowing up the pipeline. It
    marks ``extraction_failed=True`` so the operator-review UI (S4)
    can surface that the contract may be incomplete.
  * The orchestrator wires Stage 0c in by default, with a
    ``skip_interface_contract=True`` escape hatch for tests / cost
    reasons.
  * Extractor telemetry lands in ``export_meta`` for operator-review
    visibility.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.export_engine import (  # noqa: E402
    DerivedInterfaceContract,
    _format_extractor_user_message,
    _parse_extractor_response,
    _strip_extractor_wrappers,
    build_export_draft,
    derive_interface_contract,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _scanned_fixture(**overrides) -> dict:
    """Minimal scanner-shape manifest. Same shape as Stage 0a/0b tests."""
    base = {
        "id": "i-9c16b1c7",
        "name": "Unified Task System",
        "display_name": "Unified Task System",
        "description": "Persistent task management with expires-based pending todos.",
        "files": [
            {"path": "ops/tools/unified_task_system.py", "role": "build_artifact"},
            {"path": "ops/tasks/unified_tasks/tasks.json", "role": "data_file"},
        ],
        "interface_contract": {
            # Scanner pre-populates a partial contract; the extractor
            # may overwrite or augment.
            "data_files": [{"path": "ops/tasks/unified_tasks/tasks.json"}],
            "cli": [],
            "populated_by_forge": False,
        },
        "identity": {"purpose": "Track todos."},
        "updated_at": "2026-06-02T12:00:00Z",
    }
    base.update(overrides)
    return base


VALID_EXTRACTOR_RESPONSE = """\
{
  "data_files": [
    {"path": "tasks.json", "description": "Active task database",
     "schema": {"storage_format": "JSON dict keyed by id",
                "fields": {"id": "string", "status": "enum"}}}
  ],
  "cli": [
    {"command": "python3 scripts/tasks.py list",
     "key_flags": ["--status STATUS"],
     "output_signals": []},
    {"command": "python3 scripts/tasks.py check",
     "key_flags": [],
     "output_signals": ["TASK_DUE:", "FOLLOWUP_NEEDED:"]}
  ],
  "key_paths": {"active_db": "tasks.json"},
  "enums": {"status": ["open", "complete"]},
  "signal_prefixes": ["TASK_DUE:", "FOLLOWUP_NEEDED:"]
}
"""


# ── Stage 0c — User-message formatter ────────────────────────────────────────


def test_extractor_user_message_concatenates_files_with_path_headers():
    files = {
        "scripts/tasks.py": "def main(): pass\n",
        "scripts/util.py": "def helper(): pass\n",
    }
    msg = _format_extractor_user_message(files)
    assert "Implementation to Analyse" in msg
    assert "`scripts/tasks.py`" in msg
    assert "`scripts/util.py`" in msg
    assert "def main()" in msg
    assert "def helper()" in msg


def test_extractor_user_message_empty_files_returns_no_source_marker():
    msg = _format_extractor_user_message({})
    assert "no source files were provided" in msg


def test_extractor_user_message_files_sorted_for_determinism():
    """Two scans of the same workspace must produce the same user
    message so the extractor's output is reproducible. Dict iteration
    order would silently leak runtime entropy if we didn't sort."""
    files = {"z.py": "z", "a.py": "a", "m.py": "m"}
    msg = _format_extractor_user_message(files)
    a_pos = msg.index("`a.py`")
    m_pos = msg.index("`m.py`")
    z_pos = msg.index("`z.py`")
    assert a_pos < m_pos < z_pos


def test_extractor_user_message_caps_total_payload():
    """Several huge files together must not blow the model context.
    The formatter trims at max_total_chars and emits a marker."""
    files = {
        f"a{i}.py": "X" * 50_000 for i in range(5)
    }
    msg = _format_extractor_user_message(files, max_total_chars=10_000)
    assert len(msg) < 15_000  # generous slack for headers + footer
    # Either truncation marker counts as "context limit respected".
    assert "truncated" in msg or "further source files omitted" in msg


def test_extractor_user_message_always_ends_with_task_directive():
    """The extractor prompt expects a closing ``## Task`` block —
    don't drop it when files truncate the payload."""
    files = {"a.py": "X" * 100_000}
    msg = _format_extractor_user_message(files, max_total_chars=1_000)
    assert "## Task" in msg
    assert "Return only the JSON object" in msg


# ── Stage 0c — JSON parser ───────────────────────────────────────────────────


def test_parse_extractor_response_clean_json():
    parsed = _parse_extractor_response(VALID_EXTRACTOR_RESPONSE)
    assert isinstance(parsed, dict)
    assert parsed["cli"][1]["command"].endswith("check")
    assert "TASK_DUE:" in parsed["signal_prefixes"]


def test_parse_extractor_response_tolerates_prose_preamble():
    """Sometimes models prepend ``Here is the JSON:`` — extractor
    matches the outermost ``{…}`` and parses that, ignoring the prose."""
    raw = "Here is the JSON you requested:\n\n" + VALID_EXTRACTOR_RESPONSE + "\n\nLet me know if you want me to adjust."
    parsed = _parse_extractor_response(raw)
    assert parsed is not None
    assert "data_files" in parsed


def test_parse_extractor_response_empty_input_returns_none():
    assert _parse_extractor_response("") is None
    assert _parse_extractor_response("   \n   ") is None


def test_parse_extractor_response_no_json_returns_none():
    assert _parse_extractor_response("I couldn't extract anything useful.") is None


def test_parse_extractor_response_malformed_json_returns_none():
    """Truncated or syntax-broken JSON must not crash — caller
    decides to fall back to the scanner contract."""
    assert _parse_extractor_response("{ broken: not json") is None


def test_parse_extractor_response_non_dict_returns_none():
    """A JSON array at the top level isn't the contract shape — reject."""
    assert _parse_extractor_response('["a", "b"]') is None


def test_parse_extractor_response_strips_json_code_fence():
    """Empirically Sonnet 4.5 wraps the JSON in a ```json fence despite
    the prompt's "no markdown fences" instruction (verified against
    team-bot-a/i-9c16b1c7 on 2026-06-03). The parser must peel the
    fence before parsing."""
    fenced = "```json\n" + VALID_EXTRACTOR_RESPONSE.strip() + "\n```"
    parsed = _parse_extractor_response(fenced)
    assert parsed is not None
    assert parsed["cli"][1]["command"].endswith("check")


def test_parse_extractor_response_strips_bare_code_fence():
    """Same defense for the bare ``` fence (no language tag)."""
    fenced = "```\n" + VALID_EXTRACTOR_RESPONSE.strip() + "\n```"
    parsed = _parse_extractor_response(fenced)
    assert parsed is not None
    assert "data_files" in parsed


def test_parse_extractor_response_fence_plus_prose_preamble():
    """Composite of fence + prose preamble. Fence stripped first, then
    outermost-brace match handles the prose."""
    payload = (
        "Sure — here's the extracted contract:\n\n"
        "```json\n" + VALID_EXTRACTOR_RESPONSE.strip() + "\n```\n\n"
        "Hope that helps!"
    )
    parsed = _parse_extractor_response(payload)
    assert parsed is not None
    assert "data_files" in parsed


def test_parse_extractor_response_truncated_inside_fence_returns_none():
    """When the response runs into max_tokens mid-JSON, the closing ``}``
    and closing fence are gone — the parser must report failure
    (callers then fall back to scanner contract + capture the raw
    response for diagnosis)."""
    truncated = "```json\n" + VALID_EXTRACTOR_RESPONSE[:200]
    assert _parse_extractor_response(truncated) is None


def test_strip_extractor_wrappers_idempotent_on_clean_json():
    """The unwrap step must leave already-clean JSON alone."""
    assert _strip_extractor_wrappers(VALID_EXTRACTOR_RESPONSE).startswith("{")


# ── Stage 0c — derive_interface_contract dispatch + envelope ─────────────────


def test_derive_returns_extracted_contract_on_clean_parse():
    """Successful LLM dispatch + parse should produce a contract
    marked as populated_by_export with an extracted_at stamp."""

    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["system"] = system
        captured["user"] = user
        captured["model"] = model
        return VALID_EXTRACTOR_RESPONSE

    result = derive_interface_contract(
        _scanned_fixture(),
        {"scripts/tasks.py": "ok"},
        call_llm=fake_llm,
        model="extractor-model-x",
    )

    assert isinstance(result, DerivedInterfaceContract)
    assert result.extraction_failed is False
    assert result.used_fallback is False
    assert result.model == "extractor-model-x"
    contract = result.contract
    assert contract["cli"][1]["command"].endswith("check")
    assert contract["populated_by_export"] is True
    assert contract["populated_by_forge"] is False
    assert contract["extracted_at"]  # ISO timestamp set


def test_derive_uses_canonical_extractor_prompt_from_forge_engine():
    """Reuse — not re-type — the locked prompt. If forge_engine's
    BUILTIN_EXTRACTOR_PROMPT evolves, the export pipeline must follow.
    Anything else risks shape drift between forged and exported
    manifests."""
    from evolve_admin.applications.forge_engine import BUILTIN_EXTRACTOR_PROMPT

    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["system"] = system
        return VALID_EXTRACTOR_RESPONSE

    derive_interface_contract(
        _scanned_fixture(), {"scripts/tasks.py": "ok"}, call_llm=fake_llm,
    )

    assert captured["system"] == BUILTIN_EXTRACTOR_PROMPT


def test_derive_empty_files_skips_llm_and_uses_scanner_fallback():
    """If we have no source code to feed the extractor, don't burn
    an LLM call — preserve the scanner-populated contract verbatim
    with a fallback marker."""

    def should_not_be_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("derive should not invoke LLM when no files")

    result = derive_interface_contract(
        _scanned_fixture(), {}, call_llm=should_not_be_called,
    )
    assert result.used_fallback is True
    assert result.extraction_failed is False
    # Scanner-populated data_files came through.
    assert result.contract["data_files"][0]["path"] == "ops/tasks/unified_tasks/tasks.json"
    assert result.contract["populated_by_export"] is False


def test_derive_unparseable_response_falls_back_to_scanner_contract():
    """If the LLM ran but its output can't be parsed, log the
    extraction failure and keep the scanner's contract so the
    pipeline keeps moving."""
    fake_llm = lambda system, user, model, api_key, max_tokens: (  # noqa: E731
        "I don't think there's enough information here to extract."
    )
    result = derive_interface_contract(
        _scanned_fixture(),
        {"scripts/tasks.py": "ok"},
        call_llm=fake_llm,
    )
    assert result.extraction_failed is True
    assert result.used_fallback is True
    # Telemetry: we still record that the LLM ran.
    assert result.output_chars > 0
    # Scanner contract preserved.
    assert result.contract["data_files"][0]["path"] == "ops/tasks/unified_tasks/tasks.json"
    # Marked populated_by_export=False so the consumer knows to be
    # suspicious of completeness.
    assert result.contract["populated_by_export"] is False


def test_derive_unparseable_response_with_no_scanner_contract_returns_empty():
    """No scanner contract + extractor parse failure = empty contract
    with the failure flags set. Caller decides whether to retry."""
    manifest = _scanned_fixture()
    del manifest["interface_contract"]

    fake_llm = lambda *a, **kw: "nope"  # noqa: E731

    result = derive_interface_contract(
        manifest, {"scripts/tasks.py": "ok"}, call_llm=fake_llm,
    )
    assert result.extraction_failed is True
    assert result.used_fallback is True
    # Contract is the "stamped empty" shape: just the provenance flags.
    assert result.contract.get("data_files") is None
    assert result.contract["populated_by_export"] is False
    assert result.contract["populated_by_forge"] is False
    assert result.contract["extracted_at"]


def test_derive_default_max_tokens_high_enough_for_real_responses():
    """Empirically the team-bot-a/i-9c16b1c7 extractor response is
    ~10 KB of JSON, well past the original 2048 ceiling that truncated
    mid-array (see 2026-06-03 fix). Lock the default at >= 4096 so the
    truncation regression can't sneak back in."""

    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["max_tokens"] = max_tokens
        return VALID_EXTRACTOR_RESPONSE

    derive_interface_contract(
        _scanned_fixture(), {"scripts/tasks.py": "ok"}, call_llm=fake_llm,
    )
    assert captured["max_tokens"] >= 4096


def test_derive_captures_raw_response_on_parse_failure(tmp_path: Path):
    """When parse fails and shared_dir + run_id are supplied, the raw
    LLM response is written to
    ``{shared_dir}/export/logs/{run_id}-extractor.txt`` so an operator
    can see what Sonnet actually said without re-running the export."""

    def fake_llm(system, user, model, api_key, max_tokens):
        return "I don't think there's enough information here."

    result = derive_interface_contract(
        _scanned_fixture(),
        {"scripts/tasks.py": "ok"},
        call_llm=fake_llm,
        shared_dir=tmp_path,
        run_id="r-deadbeef",
    )
    assert result.extraction_failed is True

    log_path = tmp_path / "export" / "logs" / "r-deadbeef-extractor.txt"
    assert log_path.is_file()
    assert "I don't think" in log_path.read_text()


def test_derive_skips_capture_when_shared_dir_not_provided(tmp_path: Path):
    """No shared_dir = no capture (test isolation, ad-hoc CLI use).
    The export/ subtree should never get created."""

    def fake_llm(*a, **kw):
        return "nope"

    derive_interface_contract(
        _scanned_fixture(), {"scripts/tasks.py": "ok"}, call_llm=fake_llm,
    )
    # No filesystem side-effects when shared_dir is omitted.
    assert not (tmp_path / "export").exists()


def test_derive_skips_capture_on_successful_parse(tmp_path: Path):
    """A successful parse must not leave behind an extractor log —
    captures are diagnostic-only, not telemetry."""

    def fake_llm(*a, **kw):
        return VALID_EXTRACTOR_RESPONSE

    derive_interface_contract(
        _scanned_fixture(),
        {"scripts/tasks.py": "ok"},
        call_llm=fake_llm,
        shared_dir=tmp_path,
        run_id="r-ok",
    )
    assert not (tmp_path / "export" / "logs" / "r-ok-extractor.txt").exists()


def test_derive_requires_api_key_when_no_override_and_files_present():
    """With files present, we'll hit the LLM — so the api_key
    fail-fast applies the same way as Stage 0b."""
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
        with pytest.raises(ValueError, match="api_key"):
            derive_interface_contract(
                _scanned_fixture(), {"scripts/tasks.py": "ok"},
            )


# ── Orchestrator wiring (build_export_draft) ─────────────────────────────────


def test_build_export_draft_invokes_stage_0c_by_default(tmp_path: Path):
    """Default path: the orchestrator runs Stage 0c, advances the
    export_stage marker, and overlays the contract."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("def main(): pass\n")

    derive_calls: list[dict] = []
    extract_calls: list[dict] = []

    def fake_build_spec_llm(system, user, model, api_key, max_tokens):
        derive_calls.append({"role": "build_spec"})
        return "# Title\n\n## Overview\n\nBody"

    def fake_extract_llm(system, user, model, api_key, max_tokens):
        extract_calls.append({"role": "extract", "user_chars": len(user)})
        return VALID_EXTRACTOR_RESPONSE

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_round_trip=True,  # Stage 0d tested separately
        derive_kwargs={"call_llm": fake_build_spec_llm, "model": "spec-m"},
        contract_kwargs={"call_llm": fake_extract_llm, "model": "extract-m"},
    )

    assert draft["export_stage"] == "0c"
    assert draft["interface_contract"]["populated_by_export"] is True
    assert draft["interface_contract"]["cli"][1]["command"].endswith("check")
    # Telemetry recorded.
    assert draft["export_meta"]["extractor_model"] == "extract-m"
    assert draft["export_meta"]["extractor_input_chars"] > 0
    assert draft["export_meta"]["extractor_output_chars"] > 0
    assert draft["export_meta"]["extractor_failed"] is False
    assert draft["export_meta"]["extractor_used_fallback"] is False
    # Both LLMs called exactly once.
    assert len(derive_calls) == 1
    assert len(extract_calls) == 1


def test_build_export_draft_skip_flag_bypasses_stage_0c(tmp_path: Path):
    """``skip_interface_contract=True`` (used by tests + cost-saving
    re-runs) keeps the scanner contract verbatim and marks
    export_stage as 0b."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("ok\n")

    def fake_build_spec_llm(*a, **kw):
        return "# t\n\n## Overview\n\nok"

    def must_not_call(*a, **kw):  # pragma: no cover
        raise AssertionError("Stage 0c should not run when skipped")

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_interface_contract=True,
        skip_round_trip=True,  # Stage 0d tested separately
        derive_kwargs={"call_llm": fake_build_spec_llm},
        contract_kwargs={"call_llm": must_not_call},
    )
    assert draft["export_stage"] == "0b"
    # Scanner contract preserved unchanged.
    assert draft["interface_contract"] == manifest["interface_contract"]
    # No extractor telemetry.
    for key in (
        "extractor_model", "extractor_input_chars", "extractor_output_chars",
        "extractor_failed", "extractor_used_fallback",
    ):
        assert key not in draft["export_meta"]


def test_build_export_draft_records_extractor_failure_in_meta(tmp_path: Path):
    """When the extractor LLM ran but parse failed, the draft still
    advances to Stage 0c with the scanner contract as fallback AND
    the meta flag set so the operator-review UI can flag it."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("ok\n")

    def fake_build_spec_llm(*a, **kw):
        return "# t\n\n## Overview\n\nok"

    def broken_extract(*a, **kw):
        return "not JSON, just prose"

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_round_trip=True,  # Stage 0d tested separately
        derive_kwargs={"call_llm": fake_build_spec_llm},
        contract_kwargs={"call_llm": broken_extract},
    )
    assert draft["export_stage"] == "0c"
    assert draft["export_meta"]["extractor_failed"] is True
    assert draft["export_meta"]["extractor_used_fallback"] is True
    # Scanner data_files preserved.
    assert draft["interface_contract"]["data_files"][0]["path"] == \
        "ops/tasks/unified_tasks/tasks.json"


def test_build_export_draft_captures_raw_extractor_response_on_failure(tmp_path: Path):
    """End-to-end: when the operator passes ``shared_dir``, an
    unparseable extractor response gets written under
    ``{shared_dir}/export/logs/{run_id}-extractor.txt``. The run_id is
    the deterministic one Stage 0a mints, so re-running on the same
    scan yields the same log path (overwrite, not pile-up)."""
    (tmp_path / "workspace" / "scripts").mkdir(parents=True)
    (tmp_path / "workspace" / "scripts" / "tasks.py").write_text("ok\n")
    shared = tmp_path / "shared"
    shared.mkdir()

    def fake_build_spec_llm(*a, **kw):
        return "# t\n\n## Overview\n\nok"

    def broken_extract(*a, **kw):
        return "```json\n{ truncated mid"

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path / "workspace",
        skip_round_trip=True,
        shared_dir=shared,
        derive_kwargs={"call_llm": fake_build_spec_llm},
        contract_kwargs={"call_llm": broken_extract},
    )

    assert draft["export_meta"]["extractor_failed"] is True
    run_id = draft["export_meta"]["run_id"]
    log_path = shared / "export" / "logs" / f"{run_id}-extractor.txt"
    assert log_path.is_file()
    assert "truncated mid" in log_path.read_text()


def test_build_export_draft_extractor_uses_real_source_content(tmp_path: Path):
    """End-to-end: the actual file body must reach the extractor's
    user message so it can analyse real code, not a stub."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text(
        "import argparse\n\ndef cmd_check():\n    print('TASK_DUE:')\n"
    )

    captured: dict = {}

    def fake_build_spec_llm(*a, **kw):
        return "# t\n\n## Overview\n\nok"

    def fake_extract(system, user, model, api_key, max_tokens):
        captured["user"] = user
        return VALID_EXTRACTOR_RESPONSE

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/tasks.py"}]
    build_export_draft(
        "team-bot-a", manifest, tmp_path,
        skip_round_trip=True,  # Stage 0d tested separately
        derive_kwargs={"call_llm": fake_build_spec_llm},
        contract_kwargs={"call_llm": fake_extract},
    )
    assert "scripts/tasks.py" in captured["user"]
    assert "def cmd_check" in captured["user"]
    assert "TASK_DUE:" in captured["user"]
