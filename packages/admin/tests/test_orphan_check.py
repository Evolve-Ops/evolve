"""Tests for orphan_check — AST-based orphan-function detection for forge.

PR 6 of spec-forge-side-effects-2026-06-02.md §13.4. Pure-Python AST
pass; no LLM, no network. These tests pin behavior against synthetic
``tmp_path`` workspaces.

Coverage:
  * Bare orphan: a top-level function with no call sites is flagged.
  * Wired function: a function called somewhere is NOT flagged.
  * Indirect wiring via __all__ exports.
  * Decorator-registered entry points (Flask, click, pytest).
  * Conventional entry-point names (main, cli, run).
  * Test files / test_ prefix excluded.
  * Private (``_underscore``) functions excluded.
  * The ea_config() smoking-gun pattern from the 2026-06-02 audit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.orphan_check import (  # noqa: E402
    OrphanFinding,
    find_orphans,
)


def _write(ws: Path, rel: str, content: str) -> Path:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ── Smoking-gun: the ea_config pattern from the 2026-06-02 audit ────────────


def test_ea_config_pattern_is_caught(tmp_path: Path) -> None:
    """The exact failure mode from the personal-bot ea-pack forge:
    ``ea_config()`` is defined but never called. The audit flagged this
    as `dead_code`; orphan_check catches it at forge time before approval."""
    _write(tmp_path, "scripts/morning_brief.py", """\
import json
from pathlib import Path

def ea_config():
    \"\"\"Load ea-pack configuration from network.json\"\"\"
    return json.loads(Path("/Users/Shared/evolve/network.json").read_text())

def main():
    print("Morning briefing for today")

if __name__ == "__main__":
    main()
""")
    findings = find_orphans(tmp_path)
    assert len(findings) == 1
    assert findings[0].function == "ea_config"
    assert findings[0].file == "scripts/morning_brief.py"
    assert "wire it up or remove it" in findings[0].reason


# ── Basic positive + negative cases ─────────────────────────────────────────


def test_wired_function_is_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/journal.py", """\
def add_entry(text):
    return text.strip()

def main():
    print(add_entry("hello"))
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_called_via_attribute_not_flagged(tmp_path: Path) -> None:
    """``some_module.fn`` attribute access counts as a reference to fn."""
    _write(tmp_path, "scripts/lib.py", """\
def normalize(s):
    return s.lower()
""")
    _write(tmp_path, "scripts/caller.py", """\
import lib

def main():
    print(lib.normalize("HI"))
""")
    findings = find_orphans(tmp_path)
    orphan_names = {f.function for f in findings}
    assert "normalize" not in orphan_names


def test_referenced_in_dict_dispatch_not_flagged(tmp_path: Path) -> None:
    """Even ``handler = my_fn`` (bare reference) counts as wiring."""
    _write(tmp_path, "scripts/dispatch.py", """\
def handle_help():
    return "help"

DISPATCH = {"help": handle_help}

def main():
    return DISPATCH["help"]()
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_multiple_orphans_in_same_file(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/multi.py", """\
def helper_one():
    pass

def helper_two():
    pass

def helper_three():
    pass

def main():
    print("none of the helpers are called")
""")
    findings = find_orphans(tmp_path)
    names = {f.function for f in findings}
    assert names == {"helper_one", "helper_two", "helper_three"}


# ── __all__ exports ─────────────────────────────────────────────────────────


def test_function_in_dunder_all_not_flagged(tmp_path: Path) -> None:
    """Library code: a function in __all__ is an intentional export, even
    when no internal caller invokes it."""
    _write(tmp_path, "scripts/lib.py", """\
__all__ = ["public_api"]

def public_api():
    return "exported"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


# ── Decorator-registered entry points ───────────────────────────────────────


def test_flask_route_decorator_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/server.py", """\
from flask import Flask
app = Flask(__name__)

@app.route("/api/hello")
def hello():
    return "hi"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_flask_post_decorator_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/server.py", """\
from flask import Flask
app = Flask(__name__)

@app.post("/api/x")
def x_handler():
    return "x"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_click_command_decorator_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/cli.py", """\
import click

@click.command()
def serve():
    print("serving")
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_pytest_fixture_decorator_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/conftest.py", """\
import pytest

@pytest.fixture
def temp_dir():
    return "/tmp"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_bare_decorator_name_not_flagged(tmp_path: Path) -> None:
    """``@route`` (without ``app.`` prefix) is still a known entry-point
    decorator."""
    _write(tmp_path, "scripts/x.py", """\
@command
def serve():
    pass
""")
    findings = find_orphans(tmp_path)
    assert findings == []


# ── Conventional entry-point names ──────────────────────────────────────────


def test_main_function_not_flagged_even_without_call(tmp_path: Path) -> None:
    """``main()`` is the conventional script entry point. The
    ``if __name__ == "__main__": main()`` call site doesn't show as a
    Name in some AST shapes, so we always exclude main()."""
    _write(tmp_path, "scripts/x.py", """\
def main():
    print("entry point")
""")
    findings = find_orphans(tmp_path)
    assert findings == []


@pytest.mark.parametrize("name", ["main", "cli", "run", "app"])
def test_conventional_entry_point_names_excluded(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "scripts/x.py", f"""\
def {name}():
    return "ok"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


# ── Test files are entirely excluded ────────────────────────────────────────


def test_test_files_under_tests_dir_are_entirely_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_journal.py", """\
def test_add():
    assert 1 + 1 == 2

def helper_unused_in_test():
    return "fine"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_test_prefix_files_excluded(tmp_path: Path) -> None:
    """Files starting with ``test_`` at any path are pytest-discoverable."""
    _write(tmp_path, "scripts/test_smoke.py", """\
def test_basic():
    assert True

def some_helper():
    return "unwired"
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_underscore_test_prefix_function_excluded(tmp_path: Path) -> None:
    """Test helper functions named ``_test_x`` or ``test_x`` are pytest-
    discoverable wherever they live."""
    _write(tmp_path, "scripts/x.py", """\
def test_thing():
    assert True

def _test_helper():
    pass

def main():
    pass
""")
    findings = find_orphans(tmp_path)
    assert findings == []


# ── Private (underscore-prefixed) functions ─────────────────────────────────


def test_underscore_private_function_excluded(tmp_path: Path) -> None:
    """Spec §13.4 exclusion 5: convention-private functions are out of
    scope here (ruff/pyright catch them later if truly orphan)."""
    _write(tmp_path, "scripts/x.py", """\
def _internal_helper():
    return "lib-private"

def main():
    pass
""")
    findings = find_orphans(tmp_path)
    assert findings == []


# ── Nested / class scopes are NOT walked ────────────────────────────────────


def test_nested_function_inside_def_not_flagged(tmp_path: Path) -> None:
    """A closure is not a top-level def. We only consider module-level
    defs."""
    _write(tmp_path, "scripts/x.py", """\
def outer():
    def inner():   # not flagged — nested
        return 1
    return inner()

def main():
    outer()
""")
    findings = find_orphans(tmp_path)
    assert findings == []


def test_method_inside_class_not_flagged(tmp_path: Path) -> None:
    """Class methods are framework-dispatched; method-orphan detection
    is out of scope (high false-positive rate)."""
    _write(tmp_path, "scripts/x.py", """\
class Foo:
    def unused_method(self):
        return "fine"

def main():
    Foo()
""")
    findings = find_orphans(tmp_path)
    assert findings == []


# ── Explicit files= parameter ───────────────────────────────────────────────


def test_files_parameter_limits_scan_scope(tmp_path: Path) -> None:
    """Passing ``files=[...]`` from the manifest restricts the walk to
    only those files. Useful when forge wants to check the app's owned
    files only, not the whole workspace."""
    _write(tmp_path, "scripts/owned.py", """\
def orphan_in_owned():
    pass

def main():
    pass
""")
    _write(tmp_path, "scripts/unowned.py", """\
def orphan_in_unowned():
    pass
""")
    findings = find_orphans(tmp_path, files=["scripts/owned.py"])
    names = {f.function for f in findings}
    assert names == {"orphan_in_owned"}
    assert "orphan_in_unowned" not in names


def test_files_parameter_skips_non_python(tmp_path: Path) -> None:
    """Non-.py paths are silently dropped (heartbeat doc, plist, etc.)."""
    _write(tmp_path, "scripts/x.py", "def orphan(): pass\n")
    _write(tmp_path, "TASKS.md", "# not python")
    findings = find_orphans(
        tmp_path, files=["scripts/x.py", "TASKS.md", "scripts/missing.py"],
    )
    assert {f.function for f in findings} == {"orphan"}


# ── Defensive: malformed input doesn't crash ────────────────────────────────


def test_syntax_error_in_file_is_skipped_silently(tmp_path: Path) -> None:
    """A bot LLM might emit broken Python during build. orphan_check
    skips unparseable files rather than crashing the critic cycle."""
    _write(tmp_path, "scripts/broken.py", "def x(\n")  # SyntaxError
    _write(tmp_path, "scripts/good.py", """\
def orphan():
    pass

def main():
    pass
""")
    findings = find_orphans(tmp_path)
    assert {f.function for f in findings} == {"orphan"}


def test_empty_workspace_returns_empty(tmp_path: Path) -> None:
    assert find_orphans(tmp_path) == []


def test_no_python_files_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "no python here")
    _write(tmp_path, "config.json", "{}")
    assert find_orphans(tmp_path) == []


# ── OrphanFinding shape ─────────────────────────────────────────────────────


def test_finding_to_dict_round_trip(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/x.py", """\
def orphan_fn():
    pass

def main():
    pass
""")
    findings = find_orphans(tmp_path)
    assert len(findings) == 1
    d = findings[0].to_dict()
    assert d["function"] == "orphan_fn"
    assert d["file"] == "scripts/x.py"
    assert d["line"] >= 1
    assert d["severity"] == "minor"
    assert "wire it up" in d["reason"]
