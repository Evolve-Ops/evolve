"""Cross-file consistency check: writer subprocess shapes ⇔ sudoers grants.

The `permissions.writer` module issues `sudo /bin/cp`, `sudo /usr/sbin/chown`,
and `sudo /bin/chmod` against bot-owned config files. Each destination path
needs its own trio of grants in `_render_evolve_sudoers()` — macOS visudo
globs don't cross '/', so a grant for `.openclaw/openclaw.json` does not
cover `.openclaw/cron/jobs.json`.

The cron-caps applier "sudo wall" of 2026-05-20 happened because nothing
caught the missing-grant drift between writer.py and setup_wizard.py at PR
review time. This file is the regression check that would have caught it.

For each writer-target destination path (openclaw.json, exec-approvals.json,
cron/jobs.json, auth-profiles.json, skills/*.json, pod_config.json), assert
that the rendered sudoers content contains a `cp /tmp/evolve-*.json <dst>`
grant, a `chown * <dst>` grant, and a `chmod <mode> <dst>` grant with the
mode the writer actually uses. Most writers use chmod 644; auth-profiles.json
intentionally uses chmod 600 because it stores API keys (see
test_write_auth_profiles_chown.test_write_auth_profiles_chmod_is_600_not_644).
If a future writer is added that targets a new path, add a case to
WRITER_TARGETS — including the chmod mode the writer issues — and the test
will fail until the matching sudoers grants are added.

See docs/diagnosis-cron-caps-applier-sudo-wall-2026-05-20.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.setup_wizard import _render_evolve_sudoers  # noqa: E402


# ── Writer-target inventory ──────────────────────────────────────────────────
#
# (name, cp_destination_glob, chown_destination_glob, chmod_destination_glob, chmod_mode)
#
# Each tuple corresponds to a real writer in
# packages/analyzer/permissions/writer.py (or admin-side equivalents that
# follow the same /tmp + sudo cp + chown + chmod pattern). The destination
# globs are the exact patterns each grant line ends with; they're checked
# against the rendered sudoers content via substring match.
#
# ``chmod_mode`` is the file mode (string, "600" or "644") the writer actually
# passes to chmod for this target. sudoers matches the FULL command line, so
# the grant must specify the same mode. Most writers use "644"; auth-profiles
# intentionally uses "600" because it stores API keys (regression-pinned by
# test_write_auth_profiles_chmod_is_600_not_644).
#
# When adding a new bot-owned writer, add a case here so the cross-file
# consistency check covers it. The test failure message will tell the
# next operator exactly which grant they need to add to setup_wizard.
WRITER_TARGETS: list[tuple[str, str, str, str, str]] = [
    # write_openclaw_fields → /Users/<bot>/.openclaw/openclaw.json
    # chmod 600 (not 644): openclaw.json holds the gateway + channel tokens.
    # The evolve admin reads it via the inherited .openclaw ACL, not a world
    # bit — so it is treated like auth-profiles.json (security fix 2026-06-12).
    (
        "openclaw.json",
        "/Users/*/.openclaw/openclaw.json",
        "/Users/*/.openclaw/openclaw.json",
        "/Users/*/.openclaw/openclaw.json",
        "600",
    ),
    # write_exec_approvals → /Users/<bot>/.openclaw/exec-approvals.json
    (
        "exec-approvals.json",
        "/Users/*/.openclaw/exec-approvals.json",
        "/Users/*/.openclaw/exec-approvals.json",
        "/Users/*/.openclaw/exec-approvals.json",
        "644",
    ),
    # write_cron_jobs → /Users/<bot>/.openclaw/cron/jobs.json
    (
        "cron/jobs.json",
        "/Users/*/.openclaw/cron/jobs.json",
        "/Users/*/.openclaw/cron/jobs.json",
        "/Users/*/.openclaw/cron/jobs.json",
        "644",
    ),
    # auth-profiles writer (deploy.py / setup) → auth-profiles.json
    # API-key file — chmod 600, not 644. The chown grant intentionally
    # isn't rendered (writer relies on the parent-dir chown step).
    (
        "auth-profiles.json",
        "/Users/*/.openclaw/agents/main/agent/auth-profiles.json",
        "",
        "/Users/*/.openclaw/agents/main/agent/auth-profiles.json",
        "600",
    ),
    # filesystem-skill config writer → /Users/<bot>/.openclaw/skills/*.json
    (
        "skills/*.json",
        "/Users/*/.openclaw/skills/*.json",
        "/Users/*/.openclaw/skills/*.json",
        "/Users/*/.openclaw/skills/*.json",
        "644",
    ),
    # audit_pod_config writer → /Users/<bot>/.openclaw/workspace/evolve/pod_config.json
    # Note: the cp grant uses a longer tmp prefix (evolve-pod-config-*).
    (
        "pod_config.json",
        "/Users/*/.openclaw/workspace/evolve/pod_config.json",
        # No chown grant emitted for pod_config (writer relies on the
        # workspace/evolve ACL for ownership; sudoers chown is not needed).
        "",
        "/Users/*/.openclaw/workspace/evolve/pod_config.json",
        "644",
    ),
    # evolve-tiers.json writers (oc_model._save_tiers_file,
    # migrate_model_roles._write_bot_owned_json) + the ensure_pod_perms
    # self-heal (secret_config_perms.check_bot_tiers_ownership). chmod 644:
    # model-routing config, not a secret. The chown is load-bearing — a bare
    # `cp` to a *fresh* dest lands it root:wheel 0600 and the bot can no longer
    # read its OWN tier config, so every tier read/write 500s with [Errno 13]
    # (the 2026-06-16 fleet-wide repo-puller heal failure).
    (
        "evolve-tiers.json",
        "/Users/*/.openclaw/evolve-tiers.json",
        "/Users/*/.openclaw/evolve-tiers.json",
        "/Users/*/.openclaw/evolve-tiers.json",
        "644",
    ),
    # github onboard + rotate flows → /Users/<bot>/.openclaw/workspace/.git/config
    # Both _ensure_github_remote (onboard) and api_admin_rotate_github_integration_token
    # rewrite the per-bot workspace .git/config via /tmp staging.  The cp grant
    # uses the .gitconfig suffix; chown restores bot ownership after the cp;
    # chmod 644 is git's expected mode.  Missing this trio caused the 2026-05-29
    # "sudo: a password is required" failure on the Backup → Cloud wizard.
    (
        ".git/config",
        "/Users/*/.openclaw/workspace/.git/config",
        "/Users/*/.openclaw/workspace/.git/config",
        "/Users/*/.openclaw/workspace/.git/config",
        "644",
    ),
]


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sudoers_content() -> str:
    """Cached render of _render_evolve_sudoers() — exercised once per module."""
    content = _render_evolve_sudoers()
    assert content is not None, (
        "_render_evolve_sudoers returned None — openclaw not discoverable at "
        "test time. Test environment needs openclaw installed."
    )
    return content


@pytest.mark.parametrize("target", WRITER_TARGETS, ids=lambda t: t[0])
def test_writer_target_has_cp_grant(target: tuple[str, str, str, str, str], sudoers_content: str) -> None:
    name, cp_dst, _chown_dst, _chmod_dst, _mode = target
    needle = f"/bin/cp /tmp/evolve-"
    # The cp grants render as `... /bin/cp /tmp/evolve-*.json <dst>` (with
    # varying tmp prefixes — evolve-*.json, evolve-pod-config-*.json, etc.).
    # Check that some line ends with the destination glob and starts with cp.
    lines = [ln for ln in sudoers_content.splitlines() if needle in ln and ln.rstrip().endswith(cp_dst)]
    assert lines, (
        f"No `sudo /bin/cp /tmp/evolve-*.* {cp_dst}` grant found in rendered sudoers "
        f"for writer target {name!r}. Add a grant in setup_wizard._render_evolve_sudoers — "
        f"see docs/diagnosis-cron-caps-applier-sudo-wall-2026-05-20.md for the pattern."
    )


@pytest.mark.parametrize("target", WRITER_TARGETS, ids=lambda t: t[0])
def test_writer_target_has_chown_grant(target: tuple[str, str, str, str, str], sudoers_content: str) -> None:
    name, _cp_dst, chown_dst, _chmod_dst, _mode = target
    if not chown_dst:
        pytest.skip(f"{name} has no chown grant in current sudoers (writer relies on ACL).")
    needle = f"/usr/sbin/chown "
    lines = [ln for ln in sudoers_content.splitlines() if needle in ln and ln.rstrip().endswith(chown_dst)]
    assert lines, (
        f"No `sudo /usr/sbin/chown * {chown_dst}` grant found in rendered sudoers "
        f"for writer target {name!r}. Add a grant in setup_wizard._render_evolve_sudoers."
    )


@pytest.mark.parametrize("target", WRITER_TARGETS, ids=lambda t: t[0])
def test_writer_target_has_chmod_grant(target: tuple[str, str, str, str, str], sudoers_content: str) -> None:
    name, _cp_dst, _chown_dst, chmod_dst, mode = target
    # sudo matches the whole command line, so the grant must specify the same
    # mode the writer issues. Most writers use 644; auth-profiles.json
    # intentionally uses 600 (API-key file). The cron-caps "sudo wall" of
    # 2026-05-20 was triggered by writer.py asking for chmod 600 against
    # grants that all used 644 — fix #1376 follow-up; the per-target mode
    # field added 2026-05-30 makes that drift detectable for either direction.
    needle = f"/bin/chmod {mode} "
    lines = [ln for ln in sudoers_content.splitlines() if needle in ln and ln.rstrip().endswith(chmod_dst)]
    assert lines, (
        f"No `sudo /bin/chmod {mode} {chmod_dst}` grant found in rendered sudoers "
        f"for writer target {name!r}. If the writer uses a different mode, update both "
        f"writer.py and setup_wizard._render_evolve_sudoers to match — sudo matches the "
        f"full command line, not just the binary."
    )


def test_writer_does_not_use_unmatchable_chmod_mode() -> None:
    """The writer must not emit a chmod mode no sudoers grant covers.

    Historical drift (PR #986 era): writer.py asked for `chmod 600` but every
    sudoers grant used `chmod 644`. The chmod step failed silently in production
    for ~10 days until cron-caps proposals started auto-promoting through it.

    The 2026-06-12 security fix INVERTED this for openclaw.json specifically: it
    is now 600 on BOTH sides (token file), and the 644 grant for it was removed —
    so a lingering `chmod 644` on openclaw.json is now the unmatchable mode. The
    non-secret writers (exec-approvals.json, cron/jobs.json) stay 644.
    """
    _PACKAGES_DIR = _ADMIN_DIR.parent
    writer_path = _PACKAGES_DIR / "analyzer" / "permissions" / "writer.py"
    text = writer_path.read_text()
    # openclaw.json (oc_path) is a token file: chmod 600, never 644 — there is
    # no chmod-644 grant for openclaw.json anymore.
    assert '"/bin/chmod", "644", str(oc_path)' not in text, (
        "permissions/writer.py chmods openclaw.json to 644, but openclaw.json's "
        "only grant is chmod 600 (security fix 2026-06-12). Use 600 — it holds "
        "the gateway + channel tokens and must not be world-readable."
    )


# ── Reader-direction grants (bot file → /tmp snapshot) ──────────────────────


def test_quarantine_db_copy_grant_matches_monitor(sudoers_content: str) -> None:
    """home_artifacts_monitor's quarantine snapshot has a matching grant.

    Unlike the WRITER_TARGETS trios above, this grant runs in the
    reader direction: `sudo /bin/cp <bot's QuarantineEventsV2> /tmp/...`
    so the evolve user can read a DB that lives under mode-0700
    ~/Library/. No chown/chmod grants are needed — cp truncates the
    mkstemp-created (evolve-owned) destination in place.

    The monitor shipped 2026-06-09 with a docstring claiming this grant
    existed in "section 3g" while the renderer had no section 3g at all;
    every sudo -n cp failed and the quarantine check silently produced
    no Signals — a tooling failure indistinguishable from "no findings".
    The expected grant line is REBUILT from the monitor's own path
    constants, so either side drifting (monitor source path, mkstemp
    shape, or grant line) breaks the build instead of the check.

    The pinned source filename (not a bare `*`) is deliberate — see
    roadmap-80-to-100 item 2.10 (no wildcard-source cp grants).
    """
    _ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
    sys.path.insert(0, str(_ANALYZER_DIR))
    try:
        import home_artifacts_monitor as ham
    finally:
        sys.path.remove(str(_ANALYZER_DIR))

    src_glob = ham.QUARANTINE_DB_PATH_TEMPLATE.format(bot_user="*")
    dst_glob = f"/tmp/{ham.QUARANTINE_TMP_PREFIX}*{ham.QUARANTINE_TMP_SUFFIX}"
    expected = f"evolve ALL=(root) NOPASSWD: /bin/cp {src_glob} {dst_glob}"
    assert expected in sudoers_content, (
        f"Rendered sudoers is missing the Quarantine DB copy grant "
        f"(section 3g): expected line\n  {expected}\n"
        "home_artifacts_monitor._sudo_copy_quarantine_db runs `sudo -n "
        "/bin/cp` against exactly this source/destination shape; without "
        "the grant the hourly quarantine check fires "
        "quarantine_check_failed for every bot."
    )


# ── visudo regression guard ──────────────────────────────────────────────────


def test_rendered_sudoers_passes_visudo_check(sudoers_content: str, tmp_path) -> None:
    """The rendered sudoers file MUST pass `visudo -c` on macOS.

    Background: PR #1906 shipped two `chown` grants with `:` in the
    command-arg pattern (`*:staff`, `root:wheel`). macOS visudo
    rejected both with `syntax error` at end-of-line, which made
    `evolve-admin refresh-sudoers` abort with the whole file failing
    the check — leaving the mini's `/etc/sudoers.d/evolve` at its
    pre-#1906 state. The wizard kept failing for the operator with no
    indication that the fix had been shipped but not actually applied.

    The convention every other chown grant in the file already
    follows is bare `*` for the user:group argument; this test
    prevents regression to colon-bearing patterns and any other
    visudo quirk (trailing `/*`, escape-prefixed `.`, etc.) that
    only surfaces at install time.

    Skip cleanly if visudo isn't on PATH (CI environment where it
    isn't installed). This is a guard, not a hard dep.
    """
    import shutil as _shutil
    import subprocess as _sp
    visudo = _shutil.which("visudo")
    if not visudo:
        pytest.skip("visudo not on PATH — guard only fires where it's available")

    target = tmp_path / "rendered.sudoers"
    target.write_text(sudoers_content)
    r = _sp.run(
        [visudo, "-c", "-f", str(target)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, (
        f"visudo -c rejected the rendered sudoers file. This would brick "
        f"`evolve-admin refresh-sudoers` if shipped. visudo output:\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n"
        f"--- rendered file (first 60 lines) ---\n"
        + "\n".join(
            f"{i+1:4}  {ln}" for i, ln in enumerate(sudoers_content.splitlines()[:60])
        )
    )


def test_rendered_sudoers_has_no_colon_in_chown_arg(sudoers_content: str) -> None:
    """No `/usr/sbin/chown ROLE:GROUP` literals in the rendered file.

    macOS visudo rejects `:` in command-arg patterns with a syntax
    error. Every chown grant must use bare `*` for the user:group
    argument; the wildcard matches whatever subprocess.run passes
    (`<bot>:staff`, `root:wheel`, etc.) and the path argument is
    what actually constrains the grant. Belt-and-suspenders to the
    visudo check above so a regression localizes immediately to
    the chown line that introduced it (visudo's error message
    only points at end-of-line, which is unhelpful when the line
    has multiple suspects).
    """
    import re
    bad_lines = []
    for i, ln in enumerate(sudoers_content.splitlines(), start=1):
        if "/usr/sbin/chown" not in ln:
            continue
        # The chown command arg position — extract the token after
        # `/usr/sbin/chown ` and check it isn't `<word>:<word>`.
        m = re.search(r"/usr/sbin/chown\s+(-\w+\s+)?(\S+)", ln)
        if m and ":" in m.group(2):
            bad_lines.append((i, ln.strip()))
    assert not bad_lines, (
        "Found chown grants with `:` in the user:group argument. macOS "
        "visudo rejects these with a syntax error at end-of-line. Use bare "
        "`*` instead — the wildcard matches whatever user:group is passed "
        "at call time, and the path argument constrains the grant:\n"
        + "\n".join(f"  line {i}: {ln}" for i, ln in bad_lines)
    )
