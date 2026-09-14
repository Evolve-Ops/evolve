"""Self-test for the platform-blind rule of tools/platform-path-lint.

The gate stops the "macOS check fires false CRITICAL on a Linux pod" bug
class from recurring: a producer/monitor that shells out to a macOS-only
binary, or bakes a macOS-only string into a user-facing Signal/Finding
message, with no platform branch. These tests pin the detection (macOS
binaries incl. sudo-prefixed argv; macOS/darwin/FileVault strings in
message/body/title), the proximity guard heuristic (get_profile /
sys.platform), and the deliberate non-flags (prose, seam adapters,
launchctl/systemctl).
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LINT = _REPO_ROOT / "tools" / "platform-path-lint"
_DEPLOY_PY = _REPO_ROOT / "packages" / "admin" / "evolve_admin" / "deploy.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_LINT), "--rule", "blind", *args],
        capture_output=True, text=True, cwd=_REPO_ROOT,
    )


def _lint_file(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    f = tmp_path / "candidate.py"
    f.write_text(source)
    return _run(str(f))


def _load_lint_module():
    loader = importlib.machinery.SourceFileLoader("platform_path_lint", str(_LINT))
    spec = importlib.util.spec_from_loader("platform_path_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ── binaries ──────────────────────────────────────────────────────────────────
def test_macos_binary_subprocess_is_blocked(tmp_path):
    src = (
        "import subprocess\n"
        "def check():\n"
        '    return subprocess.run(["/usr/bin/fdesetup", "status"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "fdesetup" in r.stdout


def test_sudo_prefixed_binary_is_blocked(tmp_path):
    # argv[0] is "sudo"; the macOS tool is argv[1] — must still trip.
    src = (
        "import subprocess\n"
        "def check():\n"
        '    return subprocess.run(["sudo", "pfctl", "-s", "rules"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1
    assert "pfctl" in r.stdout


def test_application_firewall_path_is_blocked(tmp_path):
    src = (
        "import subprocess\n"
        "def check():\n"
        '    return subprocess.run(\n'
        '        ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1
    assert "socketfilterfw" in r.stdout


def test_bare_dscl_argv_is_blocked(tmp_path):
    src = (
        "import subprocess\n"
        "def check():\n"
        '    return subprocess.run(["dscl", ".", "-list", "/Groups"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1
    assert "dscl" in r.stdout


# ── messages ──────────────────────────────────────────────────────────────────
def test_macos_string_in_message_is_blocked(tmp_path):
    src = (
        "def make():\n"
        '    return Finding(level="critical",\n'
        '                   message="🔴 CRITICAL: macOS host firewall is off")\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1
    assert "message" in r.stdout


def test_filevault_in_message_is_blocked(tmp_path):
    src = (
        "def make():\n"
        '    return Finding(message="FileVault is off — disk is not encrypted")\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1


def test_macos_string_in_signal_title_is_blocked(tmp_path):
    src = (
        "def kwargs(bot_id, user):\n"
        "    return dict(\n"
        '        title=f"macOS user {user!r} for bot {bot_id} does not exist",\n'
        '        body="...")\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1
    assert "title" in r.stdout


def test_macos_in_fstring_message_fragment_is_blocked(tmp_path):
    src = (
        "def make(n):\n"
        '    return Finding(message=f"machine: {n} recommended macOS update(s) pending")\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 1


# ── guards (properly gated → clean) ───────────────────────────────────────────
def test_get_profile_guard_is_clean(tmp_path):
    src = (
        "import subprocess\n"
        "from platform_profile import get_profile\n"
        "def check():\n"
        '    if get_profile().name != "macos":\n'
        "        return []\n"
        '    return subprocess.run(["/usr/bin/fdesetup", "status"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_sys_platform_guard_is_clean(tmp_path):
    src = (
        "import subprocess, sys\n"
        "def check():\n"
        '    if sys.platform != "darwin":\n'
        "        return []\n"
        '    return subprocess.run(["pfctl", "-s", "rules"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_guarded_macos_message_is_clean(tmp_path):
    src = (
        "from platform_profile import get_profile\n"
        "def make():\n"
        "    if get_profile().name != \"macos\":\n"
        "        return []\n"
        '    return Finding(message="🔴 CRITICAL: macOS host firewall is off")\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


# ── deliberate non-flags ──────────────────────────────────────────────────────
def test_prose_mentioning_binaries_is_clean(tmp_path):
    # macOS / dscl / softwareupdate in a docstring, comment, and a NON-token
    # message must not trip — only argv binaries and macOS/darwin/FileVault
    # message tokens count.
    src = (
        '"""Module doc: checks FileVault and runs softwareupdate on macOS."""\n'
        "def check():\n"
        '    """Reads dscl and pfctl output — prose, not behavior."""\n'
        "    # dscl, fdesetup, scutil mentioned in a comment\n"
        '    return Finding(message="account lookup failed (dscl unavailable)")\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_launchctl_systemctl_not_flagged(tmp_path):
    # Service managers go through the scheduler seam, not this rule.
    src = (
        "import subprocess\n"
        "def a():\n"
        '    return subprocess.run(["/bin/launchctl", "list"])\n'
        "def b():\n"
        '    return subprocess.run(["systemctl", "restart", "x"])\n'
    )
    r = _lint_file(tmp_path, src)
    assert r.returncode == 0, r.stdout + r.stderr


def test_seam_adapters_excluded_but_evolve_apps_in_scope():
    mod = _load_lint_module()
    # Seam adapters + the platform home stay excluded (legitimately per-OS).
    assert mod._blind_excluded("packages/analyzer/runtime/isolation.py")
    assert mod._blind_excluded("packages/analyzer/platform_profile.py")
    # evolve_apps/ is NO LONGER blanket-excluded: a pod-scheduled daemon there
    # (security-cve-scan/finalize.py, installed as the cve-scan-finalize unit
    # and run as evolve) is in scope. The blanket carve-out used to let it
    # dodge the gate while shelling out to the macOS-only sw_vers.
    assert not mod._blind_excluded(
        "packages/analyzer/evolve_apps/security-cve-scan/finalize.py"
    )
    # In-scope producer files are NOT excluded.
    assert not mod._blind_excluded("packages/analyzer/audit.py")
    assert not mod._blind_excluded(
        "packages/analyzer/generators/sysadmin_watchdog/signals.py"
    )


def test_inbot_exempt_hook_ships_empty_but_functions():
    """The explicit in-bot exempt list is the ONLY way to take an evolve_apps/
    script out of scope — and it ships empty (no shipped script is
    bot-workspace-only). The mechanism still works, so a future genuinely-
    in-bot script has a reviewable home; a pod daemon must never go here (the
    next test enforces that)."""
    mod = _load_lint_module()
    assert mod._BLIND_INBOT_EXEMPT == ()
    # Each _load_lint_module() call is a fresh module instance, so poking the
    # global here can't leak into other tests.
    mod._BLIND_INBOT_EXEMPT = ("packages/analyzer/evolve_apps/demo/in_bot.py",)
    assert mod._blind_excluded("packages/analyzer/evolve_apps/demo/in_bot.py")
    # A non-listed sibling stays in scope.
    assert not mod._blind_excluded("packages/analyzer/evolve_apps/demo/other.py")


# ── pod-daemon coverage (derived from deploy.py, the 2026-06 narrowing) ───────
def _flatten_div_chain(node: ast.AST) -> list[str]:
    """Trailing string segments of a ``Base / "a" / "b" / "c.py"`` Path chain
    (``Base`` is a Name like ANALYZER_DIR). [] if any segment right of the
    base isn't a plain string constant."""
    segs: list[str] = []
    cur = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        right = cur.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            segs.append(right.value)
        else:
            return []
        cur = cur.left
    segs.reverse()
    return segs


def _evolve_apps_pod_daemon_scripts() -> list[str]:
    """Repo-relative paths of evolve_apps/ scripts deploy.py installs as pod
    units — those whose path appears as a ``Path``-join chain (a
    ``script_path=`` / ``program_args`` value) in the deploy module. Derived
    from the AST so a newly-added pod daemon is covered with no edit here."""
    tree = ast.parse(_DEPLOY_PY.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        segs = _flatten_div_chain(node)
        if not segs or "evolve_apps" not in segs or not segs[-1].endswith(".py"):
            continue
        idx = segs.index("evolve_apps")
        found.add("packages/analyzer/" + "/".join(segs[idx:]))
    return sorted(found)


def test_meta_walker_finds_the_cve_scan_finalizer():
    """Sanity: the deploy.py walker actually finds the known pod daemon, so
    the in-scope assertion below can't pass vacuously on an empty set."""
    scripts = _evolve_apps_pod_daemon_scripts()
    assert (
        "packages/analyzer/evolve_apps/security-cve-scan/finalize.py" in scripts
    ), scripts


def test_evolve_apps_pod_daemons_are_blind_in_scope():
    """Every evolve_apps/ script deploy.py installs as a pod unit MUST be in
    scope for the blind rule. A pod daemon shelling out to a macOS-only binary
    (finalize.py → sw_vers) is exactly the false-CRITICAL-on-Linux class the
    rule guards. Regression for the blanket ``evolve_apps/`` exclusion that let
    finalize.py's hardcoded macOS path + unguarded sw_vers ship to a Linux pod
    (cve-scan-finalize exited 1; #3191 / this PR)."""
    mod = _load_lint_module()
    for rel in _evolve_apps_pod_daemon_scripts():
        assert not mod._blind_excluded(rel), (
            f"{rel} is installed as a pod unit by deploy.py but the blind rule "
            "excludes it — narrow tools/platform-path-lint's _BLIND_EXCLUDE or "
            "drop it from _BLIND_INBOT_EXEMPT so pod daemons stay linted"
        )


# ── baseline / CI contract ────────────────────────────────────────────────────
def test_baseline_exists_and_repo_is_at_or_below_it():
    # The CI contract: `--rule blind --all` is green at HEAD. If this fails
    # you added an unguarded macOS-only producer (gate it — see the lint's
    # fix guidance) or fixed some and should ratchet the baseline DOWN.
    assert (_REPO_ROOT / "tools" / "platform-blind-baseline.txt").exists()
    r = _run("--all")
    assert r.returncode == 0, r.stdout + r.stderr


def test_update_baseline_requires_all():
    r = _run("--update-baseline")
    assert r.returncode == 2
