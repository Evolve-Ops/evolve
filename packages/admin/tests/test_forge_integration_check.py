"""Forge integration-check phase (non-blocking; between Test and Gate).

Spec: internal/spec-export-import-forge-2026-05-26.md §3.3.

For manifests declaring `shared_modules`, `app_dependencies`, or
`recursive_llm`, verify that what the manifest promises is actually present
in the bot's workspace. Findings are logged; never blocks approval.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.forge_engine import (  # noqa: E402
    _ast_top_level_names,
    _check_app_dependencies,
    _check_recursive_llm,
    _check_shared_modules,
)


# ── _ast_top_level_names ──────────────────────────────────────────────────────


class TestAstTopLevelNames:
    def test_function_def(self):
        names = _ast_top_level_names("def foo(): pass\n")
        assert names == {"foo"}

    def test_async_function_def(self):
        names = _ast_top_level_names("async def bar(): pass\n")
        assert names == {"bar"}

    def test_class_def(self):
        names = _ast_top_level_names("class Baz:\n    pass\n")
        assert names == {"Baz"}

    def test_assignment(self):
        names = _ast_top_level_names("FOO = 1\nBAR = 2\n")
        assert names == {"FOO", "BAR"}

    def test_annotated_assignment(self):
        names = _ast_top_level_names("FOO: int = 1\n")
        assert names == {"FOO"}

    def test_nested_def_not_top_level(self):
        # Nested defs should not appear as top-level names
        names = _ast_top_level_names("def outer():\n    def inner(): pass\n")
        assert names == {"outer"}

    def test_mixed(self):
        text = (
            "import os\n"
            "CONST = 'x'\n"
            "def foo(): pass\n"
            "class Bar: pass\n"
            "async def baz(): pass\n"
        )
        assert _ast_top_level_names(text) == {"CONST", "foo", "Bar", "baz"}

    def test_syntax_error_returns_empty(self):
        # Malformed Python must not raise
        names = _ast_top_level_names("def (: pass\n")
        assert names == set()


# ── _check_shared_modules ─────────────────────────────────────────────────────


class TestCheckSharedModules:
    def test_no_modules_no_issues(self, tmp_path):
        assert _check_shared_modules([], tmp_path) == []

    def test_module_file_missing(self, tmp_path):
        issues = _check_shared_modules(
            [{"name": "atlas_lib.guard", "expected_exports": ["classify"]}],
            workspace=tmp_path,
        )
        assert len(issues) == 1
        assert "atlas_lib.guard" in issues[0]
        assert "not found" in issues[0]

    def test_module_present_all_exports_present(self, tmp_path):
        mod = tmp_path / "scripts" / "atlas_lib" / "guard.py"
        mod.parent.mkdir(parents=True)
        mod.write_text(
            "def classify(): pass\n"
            "def read_operator_config(): pass\n"
        )
        issues = _check_shared_modules(
            [{"name": "atlas_lib.guard", "expected_exports": ["classify", "read_operator_config"]}],
            workspace=tmp_path,
        )
        assert issues == []

    def test_module_present_one_export_missing(self, tmp_path):
        mod = tmp_path / "scripts" / "atlas_lib" / "guard.py"
        mod.parent.mkdir(parents=True)
        mod.write_text("def classify(): pass\n")
        issues = _check_shared_modules(
            [{"name": "atlas_lib.guard", "expected_exports": ["classify", "read_operator_config"]}],
            workspace=tmp_path,
        )
        assert len(issues) == 1
        assert "read_operator_config" in issues[0]
        assert "classify" not in issues[0] or "['read_operator_config']" in issues[0]

    def test_empty_name_flagged(self, tmp_path):
        issues = _check_shared_modules([{"name": "", "expected_exports": []}], tmp_path)
        assert any("no `name`" in i for i in issues)

    def test_non_dict_entry_flagged(self, tmp_path):
        issues = _check_shared_modules(["not-a-dict"], tmp_path)
        assert any("not a dict" in i for i in issues)


# ── _check_app_dependencies ───────────────────────────────────────────────────


class TestCheckAppDependencies:
    def test_no_deps_no_issues(self, tmp_path):
        assert _check_app_dependencies([], "atlas", tmp_path) == []

    def test_dep_manifest_missing(self, tmp_path):
        issues = _check_app_dependencies(
            [{"spec_id": "p-atlas-daily-digest"}],
            bot_id="atlas",
            shared_dir=tmp_path,
        )
        assert len(issues) == 1
        assert "p-atlas-daily-digest" in issues[0]
        assert "not found" in issues[0]

    def test_dep_manifest_present(self, tmp_path):
        apps_dir = tmp_path / "applications" / "atlas"
        apps_dir.mkdir(parents=True)
        (apps_dir / "p-atlas-daily-digest.json").write_text("{}")
        issues = _check_app_dependencies(
            [{"spec_id": "p-atlas-daily-digest"}],
            bot_id="atlas",
            shared_dir=tmp_path,
        )
        assert issues == []

    def test_missing_spec_id_flagged(self, tmp_path):
        issues = _check_app_dependencies([{}], "atlas", tmp_path)
        assert any("no `spec_id`" in i for i in issues)


# ── _check_recursive_llm ──────────────────────────────────────────────────────


class TestCheckRecursiveLlm:
    def test_no_llm_no_issues(self, tmp_path):
        assert _check_recursive_llm({}, tmp_path) == []

    def test_empty_purposes_flagged(self, tmp_path):
        issues = _check_recursive_llm({"purposes": []}, tmp_path)
        assert any("no `purposes`" in i for i in issues)

    def test_missing_api_key_source_file(self, tmp_path):
        issues = _check_recursive_llm(
            {
                "purposes": [{"name": "classifier"}],
                "api_key_source": "atlas/llm-config.json",
                "fallback_required": True,
            },
            workspace=tmp_path,
        )
        assert any("api_key_source" in i and "not present" in i for i in issues)

    def test_api_key_source_present(self, tmp_path):
        cfg_path = tmp_path / "atlas" / "llm-config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("{}")
        issues = _check_recursive_llm(
            {
                "purposes": [{"name": "classifier"}],
                "api_key_source": "atlas/llm-config.json",
                "fallback_required": True,
            },
            workspace=tmp_path,
        )
        assert issues == []

    def test_missing_fallback_required_flagged(self, tmp_path):
        # api_key_source exists; purposes set; only fallback_required is absent
        cfg_path = tmp_path / "x.json"
        cfg_path.write_text("{}")
        issues = _check_recursive_llm(
            {
                "purposes": [{"name": "classifier"}],
                "api_key_source": "x.json",
            },
            workspace=tmp_path,
        )
        assert any("fallback_required" in i for i in issues)


# ── Atlas-shape end-to-end check ──────────────────────────────────────────────


class TestAtlasShape:
    """Atlas's daily-digest declares shared_modules + recursive_llm. Verify the
    integration check passes when atlas_lib/* is correctly populated and exports
    what we claim.
    """

    def test_atlas_shape_clean(self, tmp_path):
        # Set up a workspace that looks like a healthy atlas install
        scripts = tmp_path / "scripts" / "atlas_lib"
        scripts.mkdir(parents=True)
        (scripts / "classifier.py").write_text(
            "def classify(item, llm_cfg): pass\n"
            "CLASSIFY_THRESHOLD = 0.6\n"
        )
        (scripts / "guard.py").write_text(
            "def classify(user_id, chat_id, chat_type, bot_id): pass\n"
            "def read_operator_config(bot_id): pass\n"
        )
        (tmp_path / "atlas").mkdir()
        (tmp_path / "atlas" / "llm-config.json").write_text("{}")

        shared_modules = [
            {"name": "atlas_lib.classifier",
             "expected_exports": ["classify", "CLASSIFY_THRESHOLD"]},
            {"name": "atlas_lib.guard",
             "expected_exports": ["classify", "read_operator_config"]},
        ]
        recursive_llm = {
            "purposes": [{"name": "5-bucket classifier", "model": "claude-haiku-4-5-20251001"}],
            "api_key_source": "atlas/llm-config.json",
            "fallback_required": True,
        }
        assert _check_shared_modules(shared_modules, tmp_path) == []
        assert _check_recursive_llm(recursive_llm, tmp_path) == []

    def test_atlas_shape_missing_export_surfaces_clearly(self, tmp_path):
        scripts = tmp_path / "scripts" / "atlas_lib"
        scripts.mkdir(parents=True)
        # guard.py exists but is missing read_operator_config
        (scripts / "guard.py").write_text("def classify(*args): pass\n")

        issues = _check_shared_modules(
            [{"name": "atlas_lib.guard",
              "expected_exports": ["classify", "read_operator_config"]}],
            tmp_path,
        )
        assert len(issues) == 1
        # Operator should be able to read this and understand
        assert "atlas_lib.guard" in issues[0]
        assert "read_operator_config" in issues[0]
