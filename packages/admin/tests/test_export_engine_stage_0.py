"""tests/test_export_engine_stage_0.py — Stage 0a + 0b of the scanned-export pipeline.

Spec: docs/spec-scanned-export-2026-06-02.md.

Covers ``evolve_admin.applications.export_engine``:

* Stage 0a (deterministic mint):
    - pkg_id stability (same inputs -> same id)
    - pkg_id sensitivity (different bot / manifest / scan -> different id)
    - file_id stability across files in the same pkg
    - pkg_version: fresh exports vs re-exports (minor-bump shape)
    - mint_export_identifiers integrates these into one envelope
    - tolerates scanner files[] entries that are either dicts or bare
      path strings

* Stage 0b (LLM-driven build_spec derivation):
    - calls the llm with the deriver system prompt
    - user message includes the scanned-manifest anchors + every source
      file body
    - strip_source_specific flag flows through to the user message
    - empty LLM output raises RuntimeError (deriver contract)
    - api_key requirement when no override LLM is supplied
    - code-fence stripping when the LLM wraps its output in ```

* Stage 0 orchestrator (build_export_draft):
    - reads source files from workspace, skips missing / non-utf8
    - returns a draft with the scanner anchors AND the synthesised
      Stage-0 fields
    - export_meta carries deriver telemetry so the operator-review
      surface (S4) can show what was fed in
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import export_engine  # noqa: E402
from evolve_admin.applications.export_engine import (  # noqa: E402
    DERIVER_SYSTEM_PROMPT,
    DerivedBuildSpec,
    _format_deriver_user_message,
    _read_source_files,
    _strip_code_fence,
    build_export_draft,
    derive_build_spec,
    mint_export_file_id,
    mint_export_identifiers,
    mint_export_pkg_id,
    mint_export_run_id,
)


# ── Stage 0a — Mint identifiers ──────────────────────────────────────────────


def test_pkg_id_is_deterministic_for_same_inputs():
    """Same (bot, manifest, scan) -> same pkg_id. Required for
    re-exports of unchanged scans to land at the same gallery slot."""
    a = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    b = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    assert a == b
    assert a.startswith("p-")
    assert len(a) == 10  # "p-" + 8 hex


def test_pkg_id_differs_for_different_bots():
    """Different source bot must yield a different pkg_id even when
    everything else matches."""
    a = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    b = mint_export_pkg_id("team-bot-c", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    assert a != b


def test_pkg_id_differs_for_different_manifests():
    a = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    b = mint_export_pkg_id("team-bot-a", "i-aaaa1111", "2026-06-02T12:00:00Z")
    assert a != b


def test_pkg_id_differs_across_scan_runs():
    """Successive scan runs of the same manifest still get distinct
    pkg_ids — operator picks which scan to export."""
    a = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    b = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-03T12:00:00Z")
    assert a != b


def test_pkg_id_rejects_empty_inputs():
    with pytest.raises(ValueError):
        mint_export_pkg_id("", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    with pytest.raises(ValueError):
        mint_export_pkg_id("team-bot-a", "", "2026-06-02T12:00:00Z")
    with pytest.raises(ValueError):
        mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "")


def test_file_id_is_deterministic_and_pkg_scoped():
    pkg_id = "p-deadbeef"
    a = mint_export_file_id(pkg_id, "ops/tools/unified_task_system.py")
    b = mint_export_file_id(pkg_id, "ops/tools/unified_task_system.py")
    c = mint_export_file_id(pkg_id, "scripts/tasks.py")
    d = mint_export_file_id("p-cafebabe", "ops/tools/unified_task_system.py")
    assert a == b
    assert a != c   # different path -> different id
    assert a != d   # different pkg -> different id


def test_run_id_is_deterministic_and_distinct_from_pkg_id():
    pkg_id = "p-deadbeef"
    rid = mint_export_run_id(pkg_id, "2026-06-02T12:00:00Z")
    assert rid.startswith("r-")
    assert rid != pkg_id


# ── Stage 0a — Identifier envelope (mint_export_identifiers) ─────────────────


def _scanned_fixture(**overrides) -> dict:
    """A minimal scanner-shape manifest. Override fields per test."""
    base = {
        "id": "i-9c16b1c7",
        "name": "Unified Task System",
        "display_name": "Unified Task System",
        "description": "Persistent task management with expires-based pending todos.",
        "files": [
            {"path": "ops/tools/unified_task_system.py", "role": "build_artifact"},
            {"path": "ops/tasks/unified_tasks/tasks.json", "role": "data_file"},
        ],
        "identity": {
            "purpose": "Track todos, pending actions, cross-session work",
            "scope_includes": ["add", "list", "update", "complete"],
            "scope_excludes": [],
        },
        "success_criteria": {
            "observable_outcomes": ["expires drives expiry detection"],
            "failure_signals": ["tasks.json corrupted"],
        },
        "constraints": {"safety": ["atomic writes"]},
        "updated_at": "2026-06-02T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_mint_envelope_uses_manifest_updated_at_when_no_scan_timestamp():
    """If the caller doesn't pass scan_timestamp, the deterministic
    pkg_id uses the manifest's updated_at, so two operators exporting
    the same scan converge on the same id."""
    env = mint_export_identifiers("team-bot-a", _scanned_fixture())
    expected = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-02T12:00:00Z")
    assert env["pkg_id"] == expected


def test_mint_envelope_explicit_scan_timestamp_wins():
    env = mint_export_identifiers(
        "team-bot-a", _scanned_fixture(),
        scan_timestamp="2026-06-05T00:00:00Z",
    )
    expected = mint_export_pkg_id("team-bot-a", "i-9c16b1c7", "2026-06-05T00:00:00Z")
    assert env["pkg_id"] == expected


def test_mint_envelope_fresh_export_is_version_1_0():
    """No previous version -> {today}-1.0."""
    env = mint_export_identifiers("team-bot-a", _scanned_fixture())
    # Today's date is the only floating piece — assert the suffix.
    assert env["pkg_version"].endswith("-1.0")
    assert env["gallery_version"] == env["pkg_version"]


def test_mint_envelope_reexport_bumps_minor():
    """When operator passes the previous gallery version, the new one
    bumps the minor counter — major stays put (operator-controlled)."""
    env = mint_export_identifiers(
        "team-bot-a", _scanned_fixture(),
        previous_pkg_version="2026.06.02-1.3",
    )
    assert env["pkg_version"].endswith("-1.4")


def test_mint_envelope_source_and_detail_fields():
    env = mint_export_identifiers("team-bot-a", _scanned_fixture())
    assert env["source"] == "scanned_export"
    assert "bot:team-bot-a" in env["source_detail"]
    assert "manifest:i-9c16b1c7" in env["source_detail"]
    assert "scan:2026-06-02T12:00:00Z" in env["source_detail"]


def test_mint_envelope_attaches_file_ids_to_every_file():
    env = mint_export_identifiers("team-bot-a", _scanned_fixture())
    paths = {f["path"]: f for f in env["files"]}
    assert set(paths) == {
        "ops/tools/unified_task_system.py",
        "ops/tasks/unified_tasks/tasks.json",
    }
    for f in env["files"]:
        assert f["file_id"].startswith("f-")
        assert f["owned_by"] == env["pkg_id"]
        assert f["created_in_run"] == env["run_id"]


def test_mint_envelope_tolerates_bare_path_string_files():
    """Scanner sometimes emits files[] as bare path strings — handle
    both shapes without losing entries."""
    m = _scanned_fixture()
    m["files"] = ["scripts/tasks.py", {"path": "tasks.json"}]
    env = mint_export_identifiers("team-bot-a", m)
    assert {f["path"] for f in env["files"]} == {"scripts/tasks.py", "tasks.json"}


def test_mint_envelope_skips_blank_path_files():
    """Scanner-emitted empty entries should not produce broken file
    metadata — they're filtered."""
    m = _scanned_fixture()
    m["files"] = [{"path": ""}, {"path": "scripts/tasks.py"}, "  "]
    env = mint_export_identifiers("team-bot-a", m)
    assert [f["path"] for f in env["files"]] == ["scripts/tasks.py"]


def test_mint_envelope_rejects_manifest_without_id():
    """Without manifest.id we can't compute a deterministic pkg_id."""
    m = _scanned_fixture()
    del m["id"]
    with pytest.raises(ValueError):
        mint_export_identifiers("team-bot-a", m)


# ── Stage 0b — User-message formatter ────────────────────────────────────────


def test_user_message_includes_scanned_anchors_as_json():
    manifest = _scanned_fixture()
    msg = _format_deriver_user_message(
        manifest, {}, strip_source_specific=True,
    )
    assert "Scanned Manifest Anchors" in msg
    # The identity purpose anchor should reach the LLM verbatim.
    assert "cross-session work" in msg


def test_user_message_includes_each_source_file_with_path_header():
    files = {
        "scripts/a.py": "print('a')\n",
        "scripts/b.py": "print('b')\n",
    }
    msg = _format_deriver_user_message(
        _scanned_fixture(), files, strip_source_specific=True,
    )
    assert "`scripts/a.py`" in msg
    assert "`scripts/b.py`" in msg
    assert "print('a')" in msg
    assert "print('b')" in msg


def test_user_message_truncates_oversized_files_with_marker():
    """A 50KB file gets capped per-file so one giant file doesn't
    push the others out of the model's context window."""
    big = "X" * 200_000
    msg = _format_deriver_user_message(
        _scanned_fixture(), {"big.py": big},
        strip_source_specific=True, max_chars_per_file=1_000,
    )
    assert "truncated" in msg
    # Must not contain the full 200k payload.
    assert msg.count("X") <= 1_500


def test_user_message_carries_strip_directive_explicitly():
    msg_on = _format_deriver_user_message(
        _scanned_fixture(), {}, strip_source_specific=True,
    )
    msg_off = _format_deriver_user_message(
        _scanned_fixture(), {}, strip_source_specific=False,
    )
    assert "strip_source_specific = true" in msg_on
    assert "strip_source_specific = false" in msg_off


def test_user_message_handles_no_source_files_gracefully():
    msg = _format_deriver_user_message(
        _scanned_fixture(), {}, strip_source_specific=True,
    )
    assert "no source files were provided" in msg


# ── Stage 0b — System prompt contract ────────────────────────────────────────


def test_deriver_system_prompt_includes_canonical_structure_targets():
    """The downstream behavioural contract relies on these section
    headings being present in every derived build_spec. Lock the
    prompt so a future edit can't silently break the convention."""
    prompt = DERIVER_SYSTEM_PROMPT
    for target in (
        "Overview", "File Layout", "Data Format", "CLI Commands",
        "Heartbeat", "Test Suite", "Customization Guidance",
    ):
        assert target in prompt, f"deriver prompt missing target: {target}"


def test_deriver_system_prompt_specifies_strip_directives():
    prompt = DERIVER_SYSTEM_PROMPT
    # Things we want stripped in strip-mode:
    for needle in (
        "maintenance", "Slack", "severity", "watchdog",
    ):
        assert needle.lower() in prompt.lower(), f"prompt missing strip target: {needle}"
    # Things we want kept even in strip-mode:
    for needle in ("expires", "next", "summary", "tag_registry", "prune-expired"):
        assert needle in prompt, f"prompt missing keep-list: {needle}"


def test_deriver_system_prompt_references_v17_heartbeat_mechanism():
    """The derived build_spec needs to describe the v17 install
    mechanism in its heartbeat section. Make sure the prompt is wired
    to the v17 vocabulary."""
    assert "oc_heartbeat_instruction" in DERIVER_SYSTEM_PROMPT
    assert "HEARTBEAT.md" in DERIVER_SYSTEM_PROMPT


def test_deriver_system_prompt_requires_taxonomy_generalisation_in_strip_mode():
    """Follow-up to docs/audit-uts-export-2026-06-03.md finding #3:
    the audit caught source-domain default taxonomies (`game design`,
    `fabrication`, `venue`, …) surviving into the build_spec despite
    strip mode being enabled. The deriver prompt should explicitly
    direct the LLM to generalise such taxonomies in strip mode.

    Lock the rule so a future prompt edit can't silently regress."""
    # Normalise whitespace so a needle that word-wrapped across lines
    # (e.g. "tag\nregistries") still matches "tag registries".
    prompt_flat = " ".join(DERIVER_SYSTEM_PROMPT.split()).lower()

    # The rule's name appears as a header in the strip-mode block.
    assert "generalise source-domain taxonomies" in prompt_flat, (
        "deriver prompt should announce taxonomy generalisation as a "
        "strip-mode rule"
    )

    # The rule should call out the structures the LLM needs to rewrite —
    # not just say 'taxonomies' abstractly. Catches the case where a
    # future prompt edit reduces the rule to a vague one-liner.
    for needle in ("categories", "id prefix", "tag registr"):
        assert needle in prompt_flat, (
            f"deriver prompt should name the structure {needle!r} as in scope "
            "for strip-mode generalisation"
        )

    # The rule must explicitly preserve the Customization Guidance —
    # operator-customizable taxonomy is the whole point. If the LLM
    # interpreted "generalise" as "delete", the consumer would lose
    # the documented swap point.
    assert "preserve" in prompt_flat and "customization guidance" in prompt_flat, (
        "deriver prompt should require preserving Customization Guidance "
        "even when generalising the default taxonomy"
    )


# ── Stage 0b — derive_build_spec dispatch + envelope ─────────────────────────


def test_derive_build_spec_calls_llm_with_system_and_user_messages():
    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["system"] = system
        captured["user"] = user
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return "# Derived\n\nbody"

    result = derive_build_spec(
        _scanned_fixture(),
        {"scripts/tasks.py": "print('hello')\n"},
        call_llm=fake_llm,
        model="claude-haiku-x",
    )
    assert captured["system"] == DERIVER_SYSTEM_PROMPT
    assert "Scanned Manifest Anchors" in captured["user"]
    assert "scripts/tasks.py" in captured["user"]
    assert "print('hello')" in captured["user"]
    assert captured["model"] == "claude-haiku-x"
    assert isinstance(result, DerivedBuildSpec)
    assert result.build_spec == "# Derived\n\nbody"
    assert result.model == "claude-haiku-x"
    assert result.input_chars > 0
    assert result.output_chars == len("# Derived\n\nbody")


def test_derive_build_spec_strip_flag_reaches_user_message():
    """When strip_source_specific=True, the user message must mark it
    on so the LLM behaves accordingly."""
    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["user"] = user
        return "out"

    derive_build_spec(
        _scanned_fixture(), {},
        strip_source_specific=True, call_llm=fake_llm,
    )
    assert "strip_source_specific = true" in captured["user"]


def test_derive_build_spec_no_strip_flag_marks_off():
    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["user"] = user
        return "out"

    derive_build_spec(
        _scanned_fixture(), {},
        strip_source_specific=False, call_llm=fake_llm,
    )
    assert "strip_source_specific = false" in captured["user"]


def test_derive_build_spec_empty_response_raises():
    """A silent LLM failure must not silently produce a broken draft —
    the orchestrator needs an explicit signal so it can fail fast."""

    def fake_llm(system, user, model, api_key, max_tokens):
        return "   "

    with pytest.raises(RuntimeError, match="empty response"):
        derive_build_spec(_scanned_fixture(), {}, call_llm=fake_llm)


def test_derive_build_spec_requires_api_key_when_no_override():
    """Without an override call_llm AND without ANTHROPIC_API_KEY set,
    the deriver should fail at the dispatch stage rather than letting
    the underlying _call_anthropic hit a 401."""
    # We don't want to actually import forge_engine here. Mock the
    # environment.
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
        with pytest.raises(ValueError, match="api_key"):
            derive_build_spec(_scanned_fixture(), {})


def test_derive_build_spec_uses_env_var_when_no_explicit_key():
    """API key from the environment is acceptable in headless contexts
    (the admin daemon already exports it). We patch the underlying
    call_anthropic so we don't actually hit the network — just verify
    the dispatcher accepted the env-sourced key."""
    captured: dict = {}

    def fake_anthropic(system, user, model, api_key, max_tokens):
        captured["api_key"] = api_key
        return "ok"

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key-from-env"}):
        with patch(
            "evolve_admin.applications.export_engine._call_anthropic",
            new=fake_anthropic,
        ):
            derive_build_spec(_scanned_fixture(), {})
    assert captured["api_key"] == "key-from-env"


def test_strip_code_fence_unwraps_markdown_fence():
    fenced = "```markdown\n# Title\n\nbody\n```"
    assert _strip_code_fence(fenced) == "# Title\n\nbody"


def test_strip_code_fence_unwraps_bare_fence():
    assert _strip_code_fence("```\n# Title\n```") == "# Title"


def test_strip_code_fence_no_op_when_no_fence():
    assert _strip_code_fence("# Title\n\nbody\n") == "# Title\n\nbody"


# ── Stage 0 orchestrator (build_export_draft) ────────────────────────────────


def test_read_source_files_loads_present_files(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("print('x')\n")
    (tmp_path / "TASKS.md").write_text("# tasks\n")
    out = _read_source_files(
        tmp_path,
        [{"path": "scripts/tasks.py"}, {"path": "TASKS.md"}],
    )
    assert out["scripts/tasks.py"] == "print('x')\n"
    assert out["TASKS.md"] == "# tasks\n"


def test_read_source_files_skips_missing_files_silently(tmp_path: Path):
    """A scanned manifest may reference files that have since been
    deleted. The export pipeline should keep going with whatever's
    still there rather than refusing to export."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "exists.py").write_text("ok\n")
    out = _read_source_files(
        tmp_path,
        [{"path": "scripts/exists.py"}, {"path": "scripts/gone.py"}],
    )
    assert set(out) == {"scripts/exists.py"}


def test_read_source_files_skips_binary_files(tmp_path: Path):
    """Binary artifacts (e.g. SQLite dbs) trip UnicodeDecodeError —
    they must not abort the read pass."""
    (tmp_path / "data.bin").write_bytes(b"\xff\xfe\x00\x01" * 100)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tasks.py").write_text("ok\n")
    out = _read_source_files(
        tmp_path,
        [{"path": "data.bin"}, {"path": "scripts/tasks.py"}],
    )
    assert set(out) == {"scripts/tasks.py"}


def test_read_source_files_caps_huge_files(tmp_path: Path):
    big = "Y" * 200_000
    (tmp_path / "big.py").write_text(big)
    out = _read_source_files(
        tmp_path, [{"path": "big.py"}], max_chars_per_file=500,
    )
    assert "big.py" in out
    assert "truncated" in out["big.py"]
    assert len(out["big.py"]) < 1_500


def test_build_export_draft_assembles_full_draft(tmp_path: Path):
    """End-to-end orchestration: scanner manifest + workspace -> draft
    with synthesised identifiers AND derived build_spec."""
    (tmp_path / "ops" / "tools").mkdir(parents=True)
    (tmp_path / "ops" / "tools" / "unified_task_system.py").write_text(
        "# 1000 lines of task management here\nprint('ok')\n"
    )
    (tmp_path / "ops" / "tasks" / "unified_tasks").mkdir(parents=True)
    (tmp_path / "ops" / "tasks" / "unified_tasks" / "tasks.json").write_text(
        '{"tasks": []}\n'
    )

    fake_calls: list[dict] = []

    def fake_llm(system, user, model, api_key, max_tokens):
        fake_calls.append({"system": system, "user": user, "model": model})
        return "# Task Manager — Build Specification\n\n## Overview\n\nDerived OK."

    draft = build_export_draft(
        "team-bot-a", _scanned_fixture(), tmp_path,
        # Stages 0c + 0d are tested separately in their own files —
        # this test is scoped to Stage 0a + 0b only.
        skip_interface_contract=True,
        skip_round_trip=True,
        derive_kwargs={"call_llm": fake_llm, "model": "test-model"},
    )

    # Identifier fields synthesised
    assert draft["pkg_id"].startswith("p-")
    assert draft["pkg_version"].endswith("-1.0")
    assert draft["gallery_version"] == draft["pkg_version"]
    assert draft["source"] == "scanned_export"

    # Files carry deterministic file_ids
    for f in draft["files"]:
        assert f["file_id"].startswith("f-")
        assert f["owned_by"] == draft["pkg_id"]

    # Build spec was derived
    assert "Task Manager" in draft["build_spec"]
    assert draft["build_spec"].startswith("# Task Manager")

    # Scanner anchors preserved
    assert draft["identity"]["purpose"].startswith("Track todos")

    # Top-level metadata for downstream pipeline
    assert draft["status"] == "draft"
    assert draft["author"] == "evolve"
    assert draft["manifest_type"] == "evolve_application"
    assert draft["export_stage"] == "0b"
    assert draft["export_meta"]["deriver_model"] == "test-model"
    assert draft["export_meta"]["deriver_strip_source_specific"] is True

    # LLM call actually happened, and saw the real source code
    assert len(fake_calls) == 1
    user_msg = fake_calls[0]["user"]
    assert "ops/tools/unified_task_system.py" in user_msg
    assert "1000 lines of task management" in user_msg


def test_build_export_draft_strip_flag_propagates(tmp_path: Path):
    """The orchestrator must thread strip_source_specific into the
    derive call AND record it in export_meta for operator visibility."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "t.py").write_text("ok\n")

    captured: dict = {}

    def fake_llm(system, user, model, api_key, max_tokens):
        captured["user"] = user
        return "# t\n\n## Overview\n\nok"

    manifest = _scanned_fixture()
    manifest["files"] = [{"path": "scripts/t.py"}]
    draft = build_export_draft(
        "team-bot-a", manifest, tmp_path,
        strip_source_specific=False,
        skip_interface_contract=True,  # focus this test on Stage 0b
        skip_round_trip=True,
        derive_kwargs={"call_llm": fake_llm},
    )
    assert draft["export_meta"]["deriver_strip_source_specific"] is False
    assert "strip_source_specific = false" in captured["user"]


def test_build_export_draft_carries_run_id_into_export_meta(tmp_path: Path):
    """The synthesised run_id (used as files[].created_in_run) must
    also surface in export_meta so operators can trace which scan
    produced which draft."""
    def fake_llm(*args, **kwargs):
        return "# ok\n\n## Overview\n\nok"

    draft = build_export_draft(
        "team-bot-a", _scanned_fixture(), tmp_path,
        skip_interface_contract=True,  # focus this test on Stage 0a
        skip_round_trip=True,
        derive_kwargs={"call_llm": fake_llm},
    )
    run_id = draft["export_meta"]["run_id"]
    assert run_id.startswith("r-")
    # Every file's created_in_run matches.
    for f in draft["files"]:
        assert f["created_in_run"] == run_id
