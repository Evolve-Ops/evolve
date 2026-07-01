"""Self-test for tools/platform-path-lint — the /Users/ literal ratchet.

The gate is what keeps platform_profile from eroding (design doc
2026-06-10 §6): new code must not hardcode /Users/ paths. These tests pin
the detection semantics (plain strings, f-string fragments, occurrence
counting) and the deliberate exclusions (docstrings, comments).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LINT = _REPO_ROOT / "tools" / "platform-path-lint"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_LINT), *args],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )


def _lint_file(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    f = tmp_path / "candidate.py"
    f.write_text(source)
    return _run(str(f))


def test_clean_file_passes(tmp_path):
    r = _lint_file(
        tmp_path, "from evolve_config import bot_home\nx = bot_home('team-bot-a')\n"
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_plain_literal_is_blocked(tmp_path):
    r = _lint_file(tmp_path, 'p = "/Users/Shared/evolve/network.json"\n')
    assert r.returncode == 1
    assert "/Users/" in r.stdout


def test_fstring_construction_is_blocked(tmp_path):
    # The bot_id-path-math bug class — the highest-value catch.
    r = _lint_file(tmp_path, 'def f(bot_id):\n    return f"/Users/{bot_id}/.openclaw"\n')
    assert r.returncode == 1


def test_occurrences_counted_not_files(tmp_path):
    r = _lint_file(tmp_path, 'a = "/Users/a"\nb = "/Users/b /Users/c"\n')
    assert r.returncode == 1
    assert "3 literal(s)" in r.stdout


def test_docstrings_and_comments_do_not_count(tmp_path):
    src = (
        '"""Module doc mentioning /Users/Shared/evolve is fine."""\n'
        "def f():\n"
        '    """Reads /Users/<bot>/.openclaw — prose, not behavior."""\n'
        "    # /Users/ in a comment is invisible to the AST\n"
        "    return 1\n"
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_blessed_profile_home_is_allowlisted():
    r = _run(str(_REPO_ROOT / "packages" / "analyzer" / "platform_profile.py"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_baseline_exists_and_repo_is_at_or_below_it():
    # The CI contract: --all is green at HEAD. If this fails you either
    # added a /Users/ literal (migrate it — see the lint's fix guidance)
    # or fixed some and should ratchet the baseline DOWN in this PR.
    assert (_REPO_ROOT / "tools" / "platform-path-baseline.txt").exists()
    r = _run("--all")
    assert r.returncode == 0, r.stdout + r.stderr


def test_update_baseline_requires_all(tmp_path):
    r = _run("--update-baseline")
    assert r.returncode == 2


# ── repo rule (deploy-checkout literal /Users/Shared/evolve-repo) ─────────────


def _lint_repo(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    f = tmp_path / "candidate.py"
    f.write_text(source)
    return _run("--rule", "repo", str(f))


def test_repo_literal_in_path_ctor_is_blocked(tmp_path):
    r = _lint_repo(
        tmp_path,
        'from pathlib import Path\np = Path("/Users/Shared/evolve-repo")\n',
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "/Users/Shared/evolve-repo" in r.stdout


def test_repo_literal_in_subpath_path_ctor_is_blocked(tmp_path):
    r = _lint_repo(
        tmp_path,
        'from pathlib import Path\np = Path("/Users/Shared/evolve-repo/docs")\n',
    )
    assert r.returncode == 1, r.stdout + r.stderr


def test_repo_literal_in_cwd_kwarg_is_blocked(tmp_path):
    r = _lint_repo(
        tmp_path,
        'import subprocess\n'
        'subprocess.run(["git", "status"], cwd="/Users/Shared/evolve-repo")\n',
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cwd=" in r.stdout


def test_repo_ssh_deploy_key_path_not_flagged(tmp_path):
    # /Users/evolve/.ssh/evolve-repo is the SSH deploy key, NOT the deploy
    # checkout — the repo rule matches the full /Users/Shared/evolve-repo
    # literal, so this must pass.
    r = _lint_repo(
        tmp_path,
        'from pathlib import Path\nk = Path("/Users/evolve/.ssh/evolve-repo")\n',
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_repo_literal_in_log_string_not_flagged(tmp_path):
    # Operator-recipe / log strings are prose, not path resolution.
    r = _lint_repo(
        tmp_path,
        'print("to recover: cd /Users/Shared/evolve-repo && git pull")\n',
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_repo_literal_in_comment_or_docstring_not_flagged(tmp_path):
    src = (
        '"""Doc mentioning /Users/Shared/evolve-repo is prose."""\n'
        "from pathlib import Path\n"
        "# Path('/Users/Shared/evolve-repo') in a comment is AST-invisible\n"
        "x = 1\n"
    )
    r = _lint_repo(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_repo_clean_platform_keyed_usage_passes(tmp_path):
    # The blessed replacement must not trip the rule.
    r = _lint_repo(
        tmp_path,
        "from pathlib import Path\n"
        "from platform_profile import get_profile\n"
        "p = Path(get_profile().deploy_checkout_default)\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_repo_baseline_exists_and_repo_rule_is_green():
    # CI contract: --rule repo --all is green at HEAD. If this fails you
    # added a /Users/Shared/evolve-repo deploy-checkout literal in a
    # path-resolution context — resolve it via
    # platform_profile.get_profile().deploy_checkout_default.
    assert (_REPO_ROOT / "tools" / "platform-repo-path-baseline.txt").exists()
    r = _run("--rule", "repo", "--all")
    assert r.returncode == 0, r.stdout + r.stderr


# ── binpath rule (macOS command-binary path /usr/sbin/chown|lsof) ─────────────


def _lint_binpath(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    f = tmp_path / "candidate.py"
    f.write_text(source)
    return _run("--rule", "binpath", str(f))


def test_binpath_chown_argv_literal_is_blocked(tmp_path):
    r = _lint_binpath(
        tmp_path,
        'import subprocess\n'
        'subprocess.run(["sudo", "/usr/sbin/chown", "bot:staff", "/x"])\n',
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "/usr/sbin/chown" in r.stdout


def test_binpath_lsof_argv_literal_is_blocked(tmp_path):
    r = _lint_binpath(
        tmp_path,
        'import subprocess\n'
        'subprocess.run(["/usr/sbin/lsof", "-iTCP:22"])\n',
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "/usr/sbin/lsof" in r.stdout


def test_binpath_cmd_variable_list_is_blocked(tmp_path):
    # The exact-match-anywhere design catches the common cmd-var indirection
    # that a subprocess-argv-only check would miss.
    r = _lint_binpath(
        tmp_path,
        'import subprocess\n'
        'def f(p):\n'
        '    cmd = ["sudo", "/usr/sbin/chown", "bot:staff", p]\n'
        '    subprocess.run(cmd)\n',
    )
    assert r.returncode == 1, r.stdout + r.stderr


def test_binpath_embedded_fragment_not_flagged(tmp_path):
    # An operator-recipe / advisory string that merely CONTAINS the path is
    # prose, not a binary token — exact-match keeps it out of scope.
    r = _lint_binpath(
        tmp_path,
        'fix = f"sudo /usr/sbin/chown -R evolve:staff {repo}"\n',
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_binpath_docstring_and_comment_not_flagged(tmp_path):
    src = (
        '"""Doc mentioning /usr/sbin/chown is prose."""\n'
        "# /usr/sbin/chown in a comment is AST-invisible\n"
        "x = 1\n"
    )
    r = _lint_binpath(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_binpath_profile_routed_usage_passes(tmp_path):
    # The blessed replacement must not trip the rule.
    r = _lint_binpath(
        tmp_path,
        "import subprocess\n"
        "from platform_profile import get_profile\n"
        'subprocess.run(["sudo", get_profile().chown, "bot:staff", "/x"])\n',
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_binpath_baseline_exists_and_rule_is_green():
    # CI contract: --rule binpath --all is green at HEAD. If this fails you
    # added a hardcoded /usr/sbin/chown or /usr/sbin/lsof literal in the admin
    # daemon package — route it through platform_profile.get_profile().chown /
    # .lsof (resolved at call time).
    assert (_REPO_ROOT / "tools" / "platform-binpath-baseline.txt").exists()
    r = _run("--rule", "binpath", "--all")
    assert r.returncode == 0, r.stdout + r.stderr
