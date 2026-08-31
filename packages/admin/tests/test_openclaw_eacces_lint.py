"""Self-test for tools/openclaw-eacces-lint — the .openclaw EACCES recurrence guard.

The lint freezes the current set of bare ``.exists()``/``.stat()``/``.is_file()``/
``.is_dir()``/``open()`` calls on a bot ``.openclaw`` path and BLOCKS any new one
(the #3141/#3145 manual sweeps missed the most-used write site, #3184). These
tests pin the detection semantics that make it trustworthy: the assignment-chain
tracking that catches the #3184 funnel shape, the guard rule (a broad
``except Exception`` is NOT a guard — that broad swallow IS the #3184 bug), and
the two false-positive classes the heuristics deliberately exclude (the
``ai.openclaw.<bot>-gateway`` launchd label; a tainted value used as a filename
segment).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LINT = _REPO_ROOT / "tools" / "openclaw-eacces-lint"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_LINT), *args],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )


def _lint_src(tmp_path: Path, src: str) -> subprocess.CompletedProcess:
    f = tmp_path / "candidate.py"
    f.write_text(src)
    return _run(str(f))


def _load_module():
    # SourceFileLoader (not spec_from_file_location) because the tool has no .py
    # extension; exec_module avoids the deprecated load_module() path.
    loader = SourceFileLoader("openclaw_eacces_lint", str(_LINT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ── positive cases (must be flagged) ──────────────────────────────────────────
def test_bare_exists_on_openclaw_is_flagged(tmp_path):
    r = _lint_src(
        tmp_path,
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    if oc.exists():\n"
        "        return 1\n",
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert ".openclaw" in r.stdout


def test_assignment_chain_tracked_is_flagged(tmp_path):
    # The #3184 funnel shape: config_path is derived from a .openclaw string and
    # carries no needle of its own — the per-scope assignment fixpoint must catch it.
    r = _lint_src(
        tmp_path,
        "def safe_write(bot_user):\n"
        "    config_path = _user_home(bot_user) / '.openclaw/openclaw.json'\n"
        "    if config_path.exists():\n"
        "        return 1\n",
    )
    assert r.returncode == 1, r.stdout + r.stderr


def test_bare_open_on_openclaw_is_flagged(tmp_path):
    r = _lint_src(
        tmp_path,
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    with open(oc) as fh:\n"
        "        return fh.read()\n",
    )
    assert r.returncode == 1
    assert "open()" in r.stdout


def test_broad_except_is_not_a_guard(tmp_path):
    # The exact #3184 shape: a bare .exists() under a function-level
    # `except Exception` that aborts. The broad swallow IS the bug, not the fix,
    # so it must still be flagged.
    r = _lint_src(
        tmp_path,
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    try:\n"
        "        if oc.exists():\n"
        "            return 1\n"
        "    except Exception:\n"
        "        return 0\n",
    )
    assert r.returncode == 1, r.stdout + r.stderr


def test_filenotfound_only_is_flagged(tmp_path):
    # FileNotFoundError is an OSError subclass but does NOT catch PermissionError,
    # so it does not guard against the EACCES raise.
    r = _lint_src(
        tmp_path,
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    try:\n"
        "        return oc.stat().st_size\n"
        "    except FileNotFoundError:\n"
        "        return 0\n",
    )
    assert r.returncode == 1


# ── negative cases (must NOT be flagged) ──────────────────────────────────────
def test_exists_or_unreachable_not_flagged(tmp_path):
    r = _lint_src(
        tmp_path,
        "from x import exists_or_unreachable\n"
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    if exists_or_unreachable(oc):\n"
        "        return 1\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_try_except_oserror_not_flagged(tmp_path):
    r = _lint_src(
        tmp_path,
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    try:\n"
        "        if oc.exists():\n"
        "            return 1\n"
        "    except (PermissionError, OSError):\n"
        "        return 0\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_launchd_label_not_flagged(tmp_path):
    # ai.openclaw.<bot>-gateway.plist lives in /Library/LaunchDaemons, never under
    # the clamped bot dir — the `.openclaw.` label form must NOT match.
    r = _lint_src(
        tmp_path,
        "from pathlib import Path\n"
        "def f(bot_id):\n"
        "    p = Path(f'/Library/LaunchDaemons/ai.openclaw.{bot_id}-gateway.plist')\n"
        "    if p.exists():\n"
        "        return 1\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_value_segment_taint_not_flagged(tmp_path):
    # A value extracted from a .openclaw-derived object, used only as a FILENAME
    # segment, must not taint an unrelated (gallery) directory — the #3184-PR
    # `draft → pkg_id → gallery/.../{pkg_id}.json` over-taint.
    r = _lint_src(
        tmp_path,
        "from pathlib import Path\n"
        "def f(workspace, draft):\n"
        "    ws = workspace / '.openclaw' / 'workspace'\n"
        "    pkg_id = draft.get('pkg_id')\n"
        "    target = Path('/gallery') / f'{pkg_id}.json'\n"
        "    if target.exists():\n"
        "        return 1\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_doc_path_substring_not_flagged(tmp_path):
    # `oc_path` is a name needle but must be segment-anchored: `doc_path` (where
    # `oc_path` is buried mid-segment) is NOT a .openclaw path.
    r = _lint_src(
        tmp_path,
        "from pathlib import Path\n"
        "def f():\n"
        "    doc_path = Path('/docs') / 'readme.md'\n"
        "    if doc_path.is_file():\n"
        "        return 1\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr


# ── baseline / ratchet contract ───────────────────────────────────────────────
def test_baseline_exists_and_repo_is_at_or_below_it():
    # The CI contract: --all is green at HEAD. If this fails you either added a
    # bare call (route it through exists_or_unreachable — see the lint's guidance)
    # or fixed some and should ratchet the baseline DOWN in this PR.
    assert (_REPO_ROOT / "tools" / "openclaw-eacces-baseline.txt").exists()
    r = _run("--all")
    assert r.returncode == 0, r.stdout + r.stderr


def test_update_baseline_requires_all():
    r = _run("--update-baseline")
    assert r.returncode == 2


def test_baseline_shrink_is_allowed(tmp_path, monkeypatch):
    # A file with FEWER hits than its baseline entry passes (the ratchet only
    # blocks growth) and the runner prints the ratchet-down hint.
    ocl = _load_module()
    cand = tmp_path / "candidate.py"
    cand.write_text(
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    if oc.exists():\n"          # exactly 1 real hit
        "        return 1\n"
    )
    assert ocl._count(cand) == 1
    key = ocl._rel(cand)
    bl = tmp_path / "baseline.txt"
    bl.write_text(f"# header\n\n5\t{key}\n")  # pretend 5 were allowed
    monkeypatch.setattr(ocl, "BASELINE_PATH", bl)
    monkeypatch.setattr(sys, "argv", ["openclaw-eacces-lint", str(cand)])
    assert ocl.main() == 0  # 1 < 5 → OK


def test_baseline_growth_is_blocked(tmp_path, monkeypatch):
    ocl = _load_module()
    cand = tmp_path / "candidate.py"
    cand.write_text(
        "def f(home):\n"
        "    oc = home / '.openclaw' / 'openclaw.json'\n"
        "    if oc.exists():\n"
        "        return 1\n"
    )
    key = ocl._rel(cand)
    bl = tmp_path / "baseline.txt"
    bl.write_text(f"# header\n\n0\t{key}\n")  # nothing allowed
    monkeypatch.setattr(ocl, "BASELINE_PATH", bl)
    monkeypatch.setattr(sys, "argv", ["openclaw-eacces-lint", str(cand)])
    assert ocl.main() == 1  # 1 > 0 → blocked
