"""Self-test for tools/context-budget-ratchet — the diff-relative char budgets
on the per-turn context surfaces (overhead-budget Phase D1).

Sibling of test_file_size_ratchet.py, and the property under test is the same
diff-relative contract: a branch fails ONLY if it grows a budgeted surface past
``max(budget, chars-on-base)``; an over-budget surface inherited from the base
never reds a branch that leaves it alone or shrinks it.

The generic surface kinds (``file:`` whole-file chars, ``marker:`` the
``<!-- name:begin/end -->`` block) are exercised in a throwaway git repo with
the REAL tool copied in, exactly how CI invokes it. The ``py:`` kinds
(session-static-blocks, evo-mcp-manifest) import real repo modules and can't
run in the throwaway repo — they are covered against the REAL repo baseline at
the end (the same invocation preflight/CI makes).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_TOOL = _REPO_ROOT / "tools" / "context-budget-ratchet"


def _doc(inner: str) -> str:
    """A doc with a budgeted marker block containing ``inner``."""
    return (
        "# heading\npreamble outside the block\n"
        "<!-- test-block:begin -->\n"
        f"{inner}\n"
        "<!-- test-block:end -->\n"
        "postamble\n"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _setup(
    tmp_path: Path,
    budgets: dict[str, int],
    base: dict[str, str],
    head: dict[str, str],
) -> tuple[Path, str]:
    """Build a git repo: baseline ``budgets`` (surface -> chars), file contents
    ``base`` committed, then rewritten to ``head`` in the working tree."""
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(_REAL_TOOL, repo / "tools" / "context-budget-ratchet")
    baseline = "# test baseline\n" + "".join(
        f"{chars}\t{surface}\n" for surface, chars in budgets.items()
    )
    (repo / "tools" / "context-budget-baseline.txt").write_text(baseline)

    for rel, text in base.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    (repo / "README").write_text("seed\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    for rel, text in head.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    return repo, base_sha


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repo / "tools" / "context-budget-ratchet"), *args],
        capture_output=True, text=True, cwd=repo,
    )


_MARKER = "marker:doc.md#test-block"
_FILE = "file:tmpl.md"


# --- the diff-relative contract ----------------------------------------------

def test_untouched_over_budget_surface_passes(tmp_path):
    # Over budget on base from a prior merge; this branch leaves it alone.
    text = _doc("x" * 50)
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 10}, base={"doc.md": text},
        head={"doc.md": text},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not this branch's debt" in r.stdout


def test_shrink_toward_budget_still_over_passes(tmp_path):
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 10}, base={"doc.md": _doc("x" * 50)},
        head={"doc.md": _doc("x" * 30)},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr


def test_grow_within_budget_passes(tmp_path):
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 50}, base={"doc.md": _doc("x" * 10)},
        head={"doc.md": _doc("x" * 30)},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr


def test_grow_past_budget_fails_and_names_the_surface(tmp_path):
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 20}, base={"doc.md": _doc("x" * 10)},
        head={"doc.md": _doc("x" * 40)},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    # The report must carry the offender, its budget, and the update command.
    assert _MARKER in r.stdout
    assert "budget allows 20" in r.stdout
    assert "--update-baseline" in r.stdout


def test_grow_further_while_over_budget_fails(tmp_path):
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 10}, base={"doc.md": _doc("x" * 30)},
        head={"doc.md": _doc("x" * 40)},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "on top of an over-budget surface" in r.stdout


def test_whole_file_surface_measured_in_chars(tmp_path):
    # file: budgets whole-file chars (the AGENTS.md-template shape).
    repo, base = _setup(
        tmp_path, budgets={_FILE: 5}, base={"tmpl.md": "12345"},
        head={"tmpl.md": "123456789"},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "9 chars" in r.stdout


def test_marker_only_growth_outside_block_passes(tmp_path):
    # Growth OUTSIDE the marker block is not this surface's business — the
    # budget prices what the injection builder extracts, nothing else.
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 20}, base={"doc.md": _doc("x" * 10)},
        head={"doc.md": _doc("x" * 10) + ("appendix " * 100)},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr


# --- fail-closed / fail-loud edges -------------------------------------------

def test_no_base_falls_back_to_absolute_budget(tmp_path):
    text = _doc("x" * 50)
    repo, _ = _setup(
        tmp_path, budgets={_MARKER: 10}, base={"doc.md": text},
        head={"doc.md": text},
    )
    r = _run(repo, "--all")
    assert "absolute-budget mode" in r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


def test_missing_markers_fail_loud(tmp_path):
    # A budgeted block whose markers vanish is exit 2 (unmeasurable), never a
    # silent pass — the injection builder would break the same way.
    repo, base = _setup(
        tmp_path, budgets={_MARKER: 20}, base={"doc.md": _doc("x" * 10)},
        head={"doc.md": "the markers are gone\n"},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "unmeasurable" in r.stdout


def test_update_baseline_refreezes(tmp_path):
    repo, _ = _setup(
        tmp_path, budgets={_MARKER: 500}, base={"doc.md": _doc("x" * 10)},
        head={"doc.md": _doc("x" * 10)},
    )
    r = _run(repo, "--update-baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    written = (repo / "tools" / "context-budget-baseline.txt").read_text()
    assert f"10\t{_MARKER}" in written


# --- the real repo: py: surfaces measurable, baseline honest ------------------

def test_real_repo_baseline_is_current_and_measurable():
    """Run the real tool against the real repo with --base pointing at HEAD
    (so the working tree IS the base → any over-budget state would be
    'inherited', never a red from unrelated in-flight edits). This proves the
    py: measurement subprocesses (session_surface import, evo tool manifest
    render) actually work in this environment — the throwaway-repo tests
    above can't cover them."""
    r = subprocess.run(
        [sys.executable, str(_REAL_TOOL), "--all", "--base", "HEAD"],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
