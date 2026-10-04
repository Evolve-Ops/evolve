"""Coherence Pass C1 tests — PR 15.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.coherence_pass_c1 import (  # noqa: E402
    CHECK_C1_INPUT,
    CHECK_C1_INTEGRATION,
    CHECK_C1_LLM,
    CHECK_C1_MISSING,
    CHECK_C1_PARSE,
    SEVERITY_MAJOR,
    SEVERITY_WARNING,
    CoherenceFinding,
    run_pass_c1,
    write_findings_to_manifest,
)


# ── No actions / no claims ──────────────────────────────────────────────

def test_empty_manifest_no_findings(tmp_path: Path) -> None:
    findings = run_pass_c1({}, tmp_path)
    assert findings == []


def test_action_without_impl_is_skipped(tmp_path: Path) -> None:
    """An action with no implemented_by isn't checkable; skip silently."""
    manifest = {
        "scheduled_actions": [{"id": "x", "summary": "does stuff"}],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


# ── Missing file ─────────────────────────────────────────────────────────

def test_missing_implementation_emits_major(tmp_path: Path) -> None:
    manifest = {
        "scheduled_actions": [{
            "id": "x", "summary": "does stuff",
            "implemented_by": "scripts/no-such-file.py",
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert len(findings) == 1
    assert findings[0].check_id == CHECK_C1_MISSING
    assert findings[0].severity == SEVERITY_MAJOR


# ── Heuristic 1: integration shape ──────────────────────────────────────

def test_telegram_action_with_telegram_import_passes(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "send.py").write_text(
        "import telegram\nbot = telegram.Bot()\n"
    )
    manifest = {
        "scheduled_actions": [{
            "id": "send",
            "implemented_by": "scripts/send.py",
            "output": {"kind": "telegram"},
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_telegram_action_without_telegram_flagged(tmp_path: Path) -> None:
    """Action says output → telegram, but script is just print() — flag."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "send.py").write_text("print('briefing')\n")
    manifest = {
        "scheduled_actions": [{
            "id": "send",
            "implemented_by": "scripts/send.py",
            "output": {"kind": "telegram"},
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert len(findings) == 1
    assert findings[0].check_id == CHECK_C1_INTEGRATION


def test_email_action_with_smtp_passes(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "send.py").write_text("import smtplib\n")
    manifest = {
        "scheduled_actions": [{
            "id": "send",
            "implemented_by": "scripts/send.py",
            "output": {"kind": "email"},
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_subprocess_cli_invocation_counts(tmp_path: Path) -> None:
    """Subprocess to a CLI satisfies the integration check."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "send.py").write_text(
        "import subprocess\nsubprocess.run(['gh', 'pr', 'list'])\n"
    )
    manifest = {
        "scheduled_actions": [{
            "id": "send",
            "implemented_by": "scripts/send.py",
            "output": {"kind": "github"},
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_unknown_integration_kind_skips_check(tmp_path: Path) -> None:
    """Conservative: unknown integration → no false positive."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "send.py").write_text("print('x')\n")
    manifest = {
        "scheduled_actions": [{
            "id": "send",
            "implemented_by": "scripts/send.py",
            "output": {"kind": "made-up-thing"},
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


# ── Heuristic 2: input shape ────────────────────────────────────────────

def test_action_declaring_file_input_with_read_text_passes(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "compose.py").write_text(
        "from pathlib import Path\nPath('x').read_text()\n"
    )
    manifest = {
        "scheduled_actions": [{
            "id": "compose",
            "implemented_by": "scripts/compose.py",
            "inputs": [{"kind": "file", "path": "x"}],
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_action_declaring_file_input_without_reads_flagged(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "compose.py").write_text("print('hi')\n")
    manifest = {
        "scheduled_actions": [{
            "id": "compose",
            "implemented_by": "scripts/compose.py",
            "inputs": [{"kind": "file", "path": "x"}],
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    flagged_ids = {f.check_id for f in findings}
    assert CHECK_C1_INPUT in flagged_ids


def test_action_with_non_file_inputs_skips_check(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "compose.py").write_text("print('hi')\n")
    manifest = {
        "scheduled_actions": [{
            "id": "compose",
            "implemented_by": "scripts/compose.py",
            "inputs": [{"kind": "stdin"}],
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    # No integration, no LLM trigger, no file input → no findings.
    assert findings == []


# ── Heuristic 3: LLM shape ──────────────────────────────────────────────

def test_summarize_action_with_anthropic_import_passes(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "sum.py").write_text(
        "import anthropic\nc = anthropic.Anthropic()\n"
    )
    manifest = {
        "scheduled_actions": [{
            "id": "sum",
            "summary": "summarizes daily logs",
            "implemented_by": "scripts/sum.py",
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_summarize_action_without_llm_flagged(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "sum.py").write_text("print(len('x'))\n")
    manifest = {
        "scheduled_actions": [{
            "id": "sum",
            "summary": "summarizes daily logs",
            "implemented_by": "scripts/sum.py",
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    flagged_ids = {f.check_id for f in findings}
    assert CHECK_C1_LLM in flagged_ids


def test_analyze_keyword_triggers_llm_check(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text("print('x')\n")
    manifest = {
        "scheduled_actions": [{
            "id": "x",
            "summary": "analyzes weekly trends",
            "implemented_by": "scripts/x.py",
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert any(f.check_id == CHECK_C1_LLM for f in findings)


def test_non_llm_summary_skips_check(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text("print('x')\n")
    manifest = {
        "scheduled_actions": [{
            "id": "x",
            "summary": "forwards file to backup",
            "implemented_by": "scripts/x.py",
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


# ── Heuristic 4: parse check ────────────────────────────────────────────

def test_python_cron_with_syntax_error_flagged(tmp_path: Path) -> None:
    (tmp_path / "cron.py").write_text("def x(:\n    pass\n")
    manifest = {
        "crons": [{"id": "broken", "script": "cron.py"}],
    }
    findings = run_pass_c1(manifest, tmp_path)
    parse_findings = [f for f in findings if f.check_id == CHECK_C1_PARSE]
    assert len(parse_findings) == 1
    assert parse_findings[0].severity == SEVERITY_MAJOR


def test_clean_python_cron_passes(tmp_path: Path) -> None:
    (tmp_path / "cron.py").write_text("def main():\n    print('ok')\n")
    manifest = {
        "crons": [{"id": "ok", "script": "cron.py"}],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_inline_shell_cron_not_path_checked(tmp_path: Path) -> None:
    """script field that's not a path (inline shell) is skipped."""
    manifest = {
        "crons": [{"id": "x", "script": "echo hello && date"}],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_openclaw_crons_alias_also_checked(tmp_path: Path) -> None:
    (tmp_path / "cron.py").write_text("def x():\n    pass\n")
    manifest = {
        "openclaw_crons": [{"id": "x", "script": "cron.py"}],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert findings == []


def test_missing_cron_script_emits_missing(tmp_path: Path) -> None:
    manifest = {
        "crons": [{"id": "x", "script": "no-such.py"}],
    }
    findings = run_pass_c1(manifest, tmp_path)
    assert any(f.check_id == CHECK_C1_MISSING for f in findings)


# ── Path-traversal safety ──────────────────────────────────────────────

def test_path_traversal_rejected(tmp_path: Path) -> None:
    """An implemented_by like '../../../etc/passwd' must not be read."""
    manifest = {
        "scheduled_actions": [{
            "id": "x",
            "implemented_by": "../../../etc/passwd",
            "output": {"kind": "telegram"},
        }],
    }
    findings = run_pass_c1(manifest, tmp_path)
    # Either the path-traversal guard or the file-not-found path treats
    # this as missing — either way, no integration-check finding leaks.
    assert all(f.check_id != CHECK_C1_INTEGRATION for f in findings)


# ── write_findings_to_manifest ───────────────────────────────────────

def test_write_findings_preserves_non_c1_findings() -> None:
    manifest = {
        "coherence": {
            "findings": [
                {"check_id": "A-1", "message": "from pass A"},
                {"check_id": "C1-1", "message": "stale"},
                {"check_id": "C2-3", "message": "from C2"},
            ],
        },
    }
    new_findings = [CoherenceFinding(
        check_id="C1-2", severity="warning", message="fresh",
    )]
    write_findings_to_manifest(manifest, new_findings,
                                checked_at="2026-06-06T00:00:00Z")
    kept = manifest["coherence"]["findings"]
    check_ids = [f["check_id"] for f in kept]
    assert "A-1" in check_ids
    assert "C2-3" in check_ids
    assert "C1-1" not in check_ids   # old C1 dropped
    assert "C1-2" in check_ids       # new C1 added
    assert manifest["coherence"]["c1_last_run"] == "2026-06-06T00:00:00Z"


def test_write_findings_to_empty_manifest() -> None:
    manifest: dict = {}
    new_findings = [CoherenceFinding(
        check_id="C1-1", severity="warning", message="x",
    )]
    write_findings_to_manifest(manifest, new_findings)
    assert manifest["coherence"]["findings"][0]["check_id"] == "C1-1"
