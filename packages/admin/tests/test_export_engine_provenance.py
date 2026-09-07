"""F-P.9.x — deriver provenance suggestion tests.

Stage 0a (mint_export_identifiers) now suggests per-file provenance
based on file extension + role + path hints. Operator overrides
during Stage 0e review — these tests cover the heuristic itself
and the integration into the deriver.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.export_engine import (  # noqa: E402
    mint_export_identifiers,
    suggest_provenance,
)


# ── Heuristic — extension defaults ──────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("scripts/tasks.py", "bundled"),
    ("scripts/notify.sh", "bundled"),
    ("scripts/worker.rb", "bundled"),
    ("scripts/main.go", "bundled"),
    ("src/lib.rs", "bundled"),
    ("frontend/index.ts", "bundled"),
    ("frontend/Component.tsx", "bundled"),
    ("frontend/index.js", "bundled"),
    ("config/app.json", "bundled"),
    ("config/build.yaml", "bundled"),
    ("config/pyproject.toml", "bundled"),
    ("data/schema.sql", "bundled"),
])
def test_source_code_extensions_default_to_bundled(path, expected):
    assert suggest_provenance(path) == expected


@pytest.mark.parametrize("path,expected", [
    ("README.md", "forge"),
    ("AGENTS.md", "forge"),
    ("docs/intro.markdown", "forge"),
    ("docs/manual.txt", "forge"),
    ("docs/spec.rst", "forge"),
])
def test_markup_extensions_default_to_forge(path, expected):
    assert suggest_provenance(path) == expected


# ── Heuristic — path/name hints override extension ──────────────────────────


@pytest.mark.parametrize("path", [
    "templates/heartbeat.py",
    "prompts/system.py",
    "instructions/setup.sh",
])
def test_path_segment_forge_hint_overrides_extension(path):
    """Even when the extension would default to bundled, a 'template' /
    'prompt' / 'instructions' segment in the path forces forge."""
    assert suggest_provenance(path) == "forge"


@pytest.mark.parametrize("path", [
    "config/local.json",
    "config/personal.json",
    "data/private.yaml",
])
def test_basename_forge_hint_overrides_extension(path):
    """'local' / 'personal' / 'private' in the filename → forge."""
    assert suggest_provenance(path) == "forge"


# ── Heuristic — role overrides everything ──────────────────────────────────


def test_role_script_overrides_markup_extension():
    """A .md file the scanner classified as a script still gets
    bundled — the role is the strongest signal we have."""
    assert suggest_provenance("README.md", role="script") == "bundled"


def test_role_data_overrides_bundled_extension():
    """A .py file marked role=data_file by the scanner gets forge —
    runtime data shouldn't bundle."""
    assert suggest_provenance("data/state.py", role="data_file") == "forge"


def test_role_cron_marks_bundled():
    assert suggest_provenance("crontab", role="cron") == "bundled"


# ── Heuristic — edge cases ──────────────────────────────────────────────────


def test_unknown_extension_falls_back_to_bundled():
    """No extension match → default bundled (cheap install is safer
    default; operator can flip during review)."""
    assert suggest_provenance("docs/foo.xyz") == "bundled"
    assert suggest_provenance("scripts/no-extension") == "bundled"


def test_empty_path_returns_bundled():
    assert suggest_provenance("") == "bundled"
    assert suggest_provenance("   ") == "bundled"


def test_case_insensitive_matching():
    assert suggest_provenance("scripts/MAIN.PY") == "bundled"
    assert suggest_provenance("Templates/Prompt.py") == "forge"
    assert suggest_provenance("README.MD") == "forge"


def test_compound_extension_handled_via_path_hint():
    """*.template.md hits the path hint 'template' even though the
    final extension is .md (which is forge anyway here)."""
    assert suggest_provenance("scripts/heartbeat.template.md") == "forge"


# ── Integration with mint_export_identifiers ────────────────────────────────


def test_mint_identifiers_attaches_provenance_per_file():
    """Every entry in the returned files[] carries provenance."""
    out = mint_export_identifiers(
        bot_id="team-bot-a",
        scanned_manifest={
            "id": "task-manager",
            "files": [
                {"path": "scripts/tasks.py", "role": "script"},
                {"path": "templates/heartbeat.py"},
                {"path": "README.md"},
                "config/app.json",  # bare-string entry
            ],
        },
        scan_timestamp="2026-06-04T00:00:00Z",
    )
    by_path = {f["path"]: f for f in out["files"]}
    assert by_path["scripts/tasks.py"]["provenance"] == "bundled"
    assert by_path["templates/heartbeat.py"]["provenance"] == "forge"
    assert by_path["README.md"]["provenance"] == "forge"
    assert by_path["config/app.json"]["provenance"] == "bundled"


def test_mint_identifiers_respects_pre_existing_provenance():
    """If the scanner (or operator) already set provenance on an entry,
    Stage 0a doesn't clobber it. Operator override wins."""
    out = mint_export_identifiers(
        bot_id="team-bot-a",
        scanned_manifest={
            "id": "task-manager",
            "files": [
                # Scanner-suggested bundled overridden to forge by upstream.
                {"path": "scripts/tasks.py", "role": "script",
                 "provenance": "forge"},
                # Markup overridden to bundled.
                {"path": "README.md", "provenance": "bundled"},
            ],
        },
        scan_timestamp="2026-06-04T00:00:00Z",
    )
    by_path = {f["path"]: f for f in out["files"]}
    assert by_path["scripts/tasks.py"]["provenance"] == "forge"
    assert by_path["README.md"]["provenance"] == "bundled"


def test_mint_identifiers_empty_provenance_gets_filled_not_kept():
    """A blank provenance value should still get heuristically filled
    — `provenance: ""` is a not-set sentinel, not an explicit override."""
    out = mint_export_identifiers(
        bot_id="team-bot-a",
        scanned_manifest={
            "id": "task-manager",
            "files": [
                {"path": "scripts/tasks.py", "provenance": ""},
            ],
        },
        scan_timestamp="2026-06-04T00:00:00Z",
    )
    assert out["files"][0]["provenance"] == "bundled"
