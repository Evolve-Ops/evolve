"""The `changes` job's path-gating step, executed the way GitHub executes it.

WHY THIS FILE EXISTS. `ci.yml`'s `decide` step opens with `set -uo pipefail` and
a comment promising to "fail safe … on any git error", but GitHub invokes every
`run:` step as ``bash -e {0}`` — errexit is on FROM THE INVOCATION, and `set -uo
pipefail` does not clear it. The step then read `$?` from a `git diff` that is
*expected* to fail, so under the inherited `-e` a failing diff killed the step at
the assignment and the entire fail-safe branch was DEAD CODE: the documented
"run every suite when the diff is ambiguous" default could never fire.

That is the same bug that broke `publish-drift.yml`'s alarm on its first live run
(2026-08-12, run 31564806304, fixed in #3609), found by the follow-up sweep the
post-mortem called for.

The lesson from #3609 is that reading the script as TEXT does not catch it —
review passed that file twice, adversarially, while it was broken in production,
because everyone simulated it with a plain `bash script.sh`, which has no `-e`.
The blind spot was reproducing the INVOCATION, not the script. So these tests
extract the real step out of the real workflow, render its `${{ }}` expressions
the way Actions would, and run it under ``bash -e`` with a stubbed `git`,
asserting on what actually lands in `$GITHUB_OUTPUT`.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Every output the step is contractually required to set. If a new gate is added
# to the `changes` job, add it here — the fail-safe must set ALL of them, or the
# new gate silently skips on an ambiguous diff (fail-OPEN, the exact shape this
# job exists to prevent).
_GATES = ("python", "any_python", "web", "plugin", "linux_e2e", "edr", "publish")

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")


def _decide_step() -> dict:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["changes"]["steps"]
    return next(s for s in steps if s.get("id") == "decide")


def _render(script: str, event_name: str) -> str:
    """Substitute the `${{ }}` expressions the way Actions does before bash sees
    them. Bash cannot parse `${{ … }}` at all (it is an invalid parameter
    expansion), so an unrendered expression would fail as a syntax error rather
    than testing anything — hence the explicit allowlist below."""
    known = {
        "github.event_name": event_name,
        # The shas are only ever fed to the stubbed git, which ignores its args.
        "github.event.pull_request.base.sha": "b" * 40,
        "github.event.pull_request.head.sha": "h" * 40,
    }
    found = {m.group(1).strip() for m in _EXPRESSION.finditer(script)}
    unknown = found - set(known)
    assert not unknown, (
        f"the decide step grew an unrendered Actions expression: {sorted(unknown)}. "
        "Add it to `known` above — otherwise this harness stops reproducing the "
        "real step and the errexit regression it guards goes untested."
    )
    for expression, value in known.items():
        script = script.replace("${{ " + expression + " }}", value)
    return script


def _stub_git(tmp_path: Path, *, exit_code: int, names: str) -> Path:
    """A fake `git` on PATH: prints `names` on stdout and exits `exit_code`.

    The names go through a data FILE that the stub `cat`s, rather than being
    interpolated into the script. Embedding them inline is a trap: a Python-repr'd
    string puts a literal backslash-n inside bash single quotes, so the "file
    list" arrives as one line ending in `\\n` and every `$`-anchored predicate
    (`\\.pyi?$`, `…\\.py$`) silently stops matching — the harness would then be
    testing a shape the real step never sees."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    names_file = tmp_path / "changed-names.txt"
    names_file.write_text(names)
    stub = bin_dir / "git"
    stub.write_text(f'#!/bin/bash\ncat "{names_file}"\nexit {exit_code}\n')
    stub.chmod(0o755)
    return bin_dir


def _run_decide(
    tmp_path: Path,
    *,
    event_name: str = "pull_request",
    git_exit: int = 0,
    changed: str = "",
) -> dict[str, str]:
    """Execute the decide step under `bash -e` (GitHub's `bash -e {0}`) and return
    what it wrote to `$GITHUB_OUTPUT`, plus the step's own exit code."""
    script = tmp_path / "decide.sh"
    script.write_text(_render(_decide_step()["run"], event_name))
    out = tmp_path / "github_output"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{_stub_git(tmp_path, exit_code=git_exit, names=changed)}"
        f"{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
    }
    proc = subprocess.run(
        ["bash", "-e", str(script)], capture_output=True, text=True, env=env
    )
    parsed = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    parsed["_step_rc"] = str(proc.returncode)
    parsed["_stderr"] = proc.stderr
    return parsed


@pytest.mark.parametrize("git_exit", [128, 1])
def test_an_unresolvable_diff_reaches_the_fail_safe_under_errexit(tmp_path, git_exit):
    """THE REGRESSION TEST. A failing `git diff` is the fail-safe's TRIGGER, not an
    error: the step must survive it and turn every gate on. Under the `-e` GitHub
    injects, the pre-fix step died at the assignment and set nothing at all."""
    got = _run_decide(tmp_path, git_exit=git_exit)
    assert got["_step_rc"] == "0", (
        f"the step died on a failing git diff (exit {git_exit}) instead of falling "
        f"back to running everything — errexit regression: {got['_stderr']}"
    )
    for gate in _GATES:
        assert got.get(gate) == "true", (
            f"{gate} was not forced on for an unresolvable diff; an ambiguous diff "
            f"must run every suite, not skip it. Got: {got}"
        )


def test_an_empty_diff_reaches_the_same_fail_safe(tmp_path):
    """The other half of the collapsed condition: git SUCCEEDED but reported
    nothing. Indistinguishable from a broken diff for gating purposes, and it took
    the same branch before the fix — it must still."""
    got = _run_decide(tmp_path, git_exit=0, changed="")
    assert got["_step_rc"] == "0", got["_stderr"]
    for gate in _GATES:
        assert got.get(gate) == "true", (gate, got)


def test_a_non_pull_request_event_runs_everything(tmp_path):
    """Pushes to main run the full matrix — the pre-existing fail-safe that was
    never at risk, asserted so the two paths cannot diverge."""
    got = _run_decide(tmp_path, event_name="push", git_exit=128)
    assert got["_step_rc"] == "0", got["_stderr"]
    for gate in _GATES:
        assert got.get(gate) == "true", (gate, got)


def test_a_resolved_diff_still_gates_narrowly(tmp_path):
    """The fix must not turn the step into an unconditional "run everything" —
    that would quietly cost ~18 jobs of runner time on every docs PR. A clean,
    resolvable, docs-only diff still gates the heavy suites OFF."""
    got = _run_decide(tmp_path, git_exit=0, changed="docs/some-note.md\n")
    assert got["_step_rc"] == "0", got["_stderr"]
    for gate in _GATES:
        assert got.get(gate) == "false", (gate, got)


def test_a_resolved_python_diff_turns_the_python_gates_on(tmp_path):
    """…and a real source change still turns its gates on, so the narrow path is
    exercised in both directions rather than only proving things can be false."""
    got = _run_decide(tmp_path, git_exit=0, changed="packages/admin/evolve_admin/deploy.py\n")
    assert got["_step_rc"] == "0", got["_stderr"]
    assert got.get("python") == "true", got
    assert got.get("any_python") == "true", got
    assert got.get("linux_e2e") == "true", got


def test_the_decide_step_never_reads_a_bare_dollar_question(tmp_path):
    """`$?` after a bare assignment is precisely the dead-code shape this file was
    written for. `set -uo pipefail` does NOT disable the errexit GitHub injects,
    so any `$?` read here is either unreachable or already-clobbered. Fail loudly
    if one comes back."""
    # Comments are stripped first — the fix's own comment explains the `$?`
    # hazard by name, and matching that would make this test unsatisfiable.
    run = "\n".join(
        line for line in _decide_step()["run"].splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "$?" not in run, (
        "the decide step reads `$?` again — under GitHub's `bash -e {0}` the "
        "command whose status it wants has already killed the step. Use "
        "`cmd || fallback`, or an `if` condition, where errexit is suspended."
    )
