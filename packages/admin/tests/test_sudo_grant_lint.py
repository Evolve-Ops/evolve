"""Unit tests for tools/sudo-grant-lint.

The lint is the CONSUMER-side mirror of test_sudoers_platform_profile.py:
that test pins what evolve MAY sudo (the rendered grants); this one verifies
that what the code ACTUALLY sudos either matches a grant or carries an
inline annotation explaining why it doesn't need one.

The tool is an extensionless script under tools/, so we load it by path.
Grants come from the same golden fixtures the tool reads in production, so
the tests exercise the real grant set, not a stub.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "sudo-grant-lint"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("sudo_grant_lint", str(_TOOL))
    spec = importlib.util.spec_from_loader("sudo_grant_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the @dataclass decorator can resolve the module
    # (Python 3.10 dataclasses look the module up in sys.modules during class
    # processing; an extensionless script isn't auto-registered).
    sys.modules["sudo_grant_lint"] = mod
    loader.exec_module(mod)
    return mod


sgl = _load_tool()
GRANTS = sgl._parse_goldens()
IN_CONTRACT = sgl._in_contract_bases(GRANTS)


def _lint(src: str, tmp_path: Path):
    f = tmp_path / "snippet.py"
    f.write_text(src)
    return sgl.lint_file(f, GRANTS, IN_CONTRACT)


def _codes(findings):
    return {(f.tier, f.code) for f in findings}


# ── grants load at all ───────────────────────────────────────────────────


def test_goldens_parse_to_grants():
    assert GRANTS, "no grants parsed from the sudoers goldens"
    bases = {g.base for g in GRANTS}
    # A representative slice of evolve's sudo contract.
    assert {"cat", "cp", "chmod", "chown", "visudo", "openclaw"} <= bases
    assert "cat" in IN_CONTRACT and "git" not in IN_CONTRACT


# ── the five cases the spec calls out ────────────────────────────────────


def test_granted_call_passes(tmp_path):
    # cat of a granted bot-config path — matches §1 exactly.
    src = (
        "import subprocess\n"
        "def f():\n"
        '    subprocess.run(["sudo", "/bin/cat", "/Users/bot/.openclaw/openclaw.json"])\n'
    )
    assert _lint(src, tmp_path) == []


def test_ungranted_static_path_blocks(tmp_path):
    # cp to /etc/sudoers.d/evolve — cp is granted, but no cp grant covers
    # that destination (this is bug #2 from the investigation).
    src = (
        "import subprocess\n"
        "def f(tmp):\n"
        '    subprocess.run(["sudo", "/bin/cp", tmp, "/etc/sudoers.d/evolve"])\n'
    )
    assert ("block", "path-not-granted") in _codes(_lint(src, tmp_path))


def test_unclassified_runas_blocks(tmp_path):
    # `sudo -u <bot> cat` — cat is granted to root, never to a bot user, so
    # the evolve daemon can't run this NOPASSWD. No annotation → block.
    src = (
        "import subprocess\n"
        "def f(bot, p):\n"
        '    subprocess.run(["sudo", "-u", bot, "/bin/cat", str(p)])\n'
    )
    assert ("block", "no-grant-for-binary") in _codes(_lint(src, tmp_path))


def test_root_only_annotation_passes(tmp_path):
    src = (
        "import subprocess\n"
        "def f(tmp):\n"
        '    subprocess.run(["sudo", "/bin/cp", tmp, "/etc/sudoers.d/evolve"])'
        "  # sudo-grant: root-only\n"
    )
    assert _lint(src, tmp_path) == []


def test_tempfile_vs_fixed_grant_warns(tmp_path):
    # visudo on a NamedTemporaryFile — the only visudo grants name the FIXED
    # /etc/sudoers.d/evolve paths, which a temp path can never match (bug #1).
    src = (
        "import tempfile, subprocess\n"
        "def f():\n"
        '    with tempfile.NamedTemporaryFile(suffix=".sudoers") as tmp:\n'
        "        tmp_path = tmp.name\n"
        '    subprocess.run(["sudo", "/usr/sbin/visudo", "-c", "-f", tmp_path])\n'
    )
    codes = _codes(_lint(src, tmp_path))
    assert ("warn", "tempfile-vs-fixed-grant") in codes


# ── annotation vocabulary ────────────────────────────────────────────────


def test_ungranted_by_design_requires_reason(tmp_path):
    bare = (
        "import subprocess\n"
        "def f(tmp):\n"
        '    subprocess.run(["sudo", "/bin/cp", tmp, "/etc/sudoers.d/evolve"])'
        "  # sudo-grant: ungranted-by-design\n"
    )
    assert ("block", "ungranted-needs-reason") in _codes(_lint(bare, tmp_path))

    with_reason = bare.rstrip("\n") + ": Option B PR#2759\n"
    assert _lint(with_reason, tmp_path) == []


def test_known_gap_warns_and_needs_reason(tmp_path):
    bare = (
        "import subprocess\n"
        "def f(bot, p):\n"
        '    subprocess.run(["sudo", "-u", bot, "/bin/cat", str(p)])'
        "  # sudo-grant: known-gap\n"
    )
    assert ("block", "known-gap-needs-reason") in _codes(_lint(bare, tmp_path))

    tracked = bare.rstrip("\n") + ": evolve can't sudo -u bot; tracked\n"
    assert _codes(_lint(tracked, tmp_path)) == {("warn", "known-gap")}


def test_granted_annotation_suppresses_path_warning(tmp_path):
    # The static matcher can't confirm the destination, but the human asserts
    # the binary is granted — and cp genuinely is — so it passes.
    src = (
        "import subprocess\n"
        "def f(tmp):\n"
        '    subprocess.run(["sudo", "/bin/cp", tmp, "/etc/sudoers.d/evolve"])'
        "  # sudo-grant: granted\n"
    )
    assert _lint(src, tmp_path) == []


def test_granted_annotation_still_blocks_when_binary_ungranted(tmp_path):
    # `granted` is not a blanket override: cat has no run-as-bot grant, so
    # asserting `granted` on a `sudo -u bot cat` is wrong and still blocks.
    src = (
        "import subprocess\n"
        "def f(bot, p):\n"
        '    subprocess.run(["sudo", "-u", bot, "/bin/cat", str(p)])'
        "  # sudo-grant: granted\n"
    )
    assert ("block", "granted-but-no-grant") in _codes(_lint(src, tmp_path))


# ── scope: out-of-contract binaries are ignored ──────────────────────────


@pytest.mark.parametrize("binary", ["git", "/bin/mv", "/usr/bin/touch", "pkill", "env"])
def test_out_of_contract_binary_skipped(binary, tmp_path):
    src = (
        "import subprocess\n"
        "def f(p):\n"
        f'    subprocess.run(["sudo", {binary!r}, str(p)])\n'
    )
    assert _lint(src, tmp_path) == []


def test_run_as_bot_openclaw_passes(tmp_path):
    # The openclaw grant is `(ALL)` with no arg constraint → any args, any user.
    src = (
        "import subprocess\n"
        "def f(bot):\n"
        '    subprocess.run(["sudo", "-u", bot, "openclaw", "status", "--deep"])\n'
    )
    assert _lint(src, tmp_path) == []


def test_annotation_above_multiline_call(tmp_path):
    # A leading-comment-block annotation must attach to the call below it,
    # even when the call spans several lines.
    src = (
        "import subprocess\n"
        "def f(tmp):\n"
        "    # sudo-grant: root-only\n"
        "    subprocess.run(\n"
        '        ["sudo", "/bin/cp", tmp, "/etc/sudoers.d/evolve"],\n'
        "        capture_output=True,\n"
        "    )\n"
    )
    assert _lint(src, tmp_path) == []


# ── _run_sudo wrapper recognition (deploy.py's privileged-write path) ─────
#
# deploy._run_sudo(cmd, result, …) execs ["sudo"] + resolved INSIDE the wrapper,
# so the direct-subprocess matcher never sees a literal "sudo" argv. These tests
# pin that the lint models the wrapper shape — the gap that let install_evolve_app
# (#2923) and write_doc's doc-seeding writes (#2922) ship with no grant verifier.


def test_wrapper_granted_static_path_passes(tmp_path):
    # deploy._run_sudo(["mkdir","-p", <granted>], result): the install_evolve_app
    # shape. /Users/*/.openclaw/cron is granted (§19), so a bare "mkdir" (the
    # wrapper resolves to /bin/mkdir; we match on basename) is accounted for.
    src = (
        "def f(result):\n"
        '    _run_sudo(["mkdir", "-p", "/Users/evolve/.openclaw/cron"], result)\n'
    )
    assert _lint(src, tmp_path) == []


def test_wrapper_ungranted_static_path_blocks(tmp_path):
    # The exact class #2922/#2923 fixed: a wrapper-routed write to a path no
    # grant covers. Before the lint understood _run_sudo this slipped through
    # silently and password-prompted at runtime.
    src = (
        "def f(result, tmp):\n"
        '    _run_sudo(["cp", tmp, "/Users/bot/.openclaw/workspace/bin/backup.sh"], result)\n'
    )
    assert ("block", "path-not-granted") in _codes(_lint(src, tmp_path))


def test_wrapper_run_sudo_param_name_recognized(tmp_path):
    # bot_doc_seeding.write_doc receives deploy._run_sudo as the `run_sudo`
    # parameter and calls run_sudo([...], result) — same wrapper, different
    # binding name. chmod is in contract; /etc/cron.d/evolve is granted to none.
    src = (
        "def write_doc(run_sudo, result):\n"
        '    run_sudo(["chmod", "+x", "/etc/cron.d/evolve"], result)\n'
    )
    assert ("block", "path-not-granted") in _codes(_lint(src, tmp_path))


def test_wrapper_known_gap_annotation_warns(tmp_path):
    # The annotation vocabulary attaches to wrapper calls too — a known-gap
    # downgrades the otherwise-blocking ungranted path to a tracked warning.
    src = (
        "def f(result, tmp):\n"
        '    _run_sudo(["cp", tmp, "/Users/bot/.openclaw/workspace/bin/x.sh"], result)'
        "  # sudo-grant: known-gap: best-effort; tracked\n"
    )
    assert _codes(_lint(src, tmp_path)) == {("warn", "known-gap")}


def test_single_arg_run_sudo_not_treated_as_daemon_wrapper(tmp_path):
    # tools/audit_pod_acls has a same-named _run_sudo(argv) that runs under
    # `sudo evolve-admin` (operator root, NOPASSWD:ALL) — out of the evolve-grant
    # contract. It threads no DeployResult (single arg), so the lint leaves it
    # alone even on a path no evolve grant covers.
    src = (
        "def f(path):\n"
        '    _run_sudo(["/bin/chmod", "-t", str(path)])\n'
    )
    assert _lint(src, tmp_path) == []


def test_run_sudo_steps_not_recognized(tmp_path):
    # oc_cli_device._run_sudo_steps takes a TUPLE of already-"sudo"-prefixed
    # lists — a different wrapper, out of scope. The inner lists are plain
    # literals (not subprocess calls), so they aren't matched either.
    src = (
        "def f(path):\n"
        "    _run_sudo_steps((\n"
        '        ["sudo", "/bin/cp", "/tmp/x", str(path)],\n'
        '        ["sudo", "/bin/chmod", "600", str(path)],\n'
        "    ))\n"
    )
    assert _lint(src, tmp_path) == []


def test_wrapper_dynamic_path_optimistic(tmp_path):
    # A wholly dynamic destination (str(dst)) can't be pinned to a path, so it
    # matches a same-arity grant optimistically — no false block on the common
    # _run_sudo(["chown", owner, str(dst)], result) shape.
    src = (
        "def f(result, dst):\n"
        '    _run_sudo(["chown", "evolve:staff", str(dst)], result)\n'
    )
    assert _lint(src, tmp_path) == []


# ── the live tree is green (the gate, mirrored as a test) ─────────────────


def test_live_production_tree_has_zero_blocks():
    """Every in-contract sudo call in packages/ is accounted for. If this
    fails, someone added a sudo call to a granted binary without a matching
    grant or a `# sudo-grant:` annotation — the exact drift the gate exists
    to stop. Add the grant, or annotate root-only / ungranted-by-design /
    known-gap. See tools/sudo-grant-lint."""
    blocks = []
    for p in sgl._iter_production_files():
        for fnd in sgl.lint_file(p, GRANTS, IN_CONTRACT):
            if fnd.tier == "block":
                blocks.append(f"{fnd.path}:{fnd.lineno} [{fnd.code}] {fnd.message}")
    assert not blocks, "unaccounted in-contract sudo calls:\n" + "\n".join(blocks)
