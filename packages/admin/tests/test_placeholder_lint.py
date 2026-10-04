"""Tests for ``placeholder_lint`` — the shared residual-template detector.

Spec: internal/spec-apps-meta-2026-06-13.md (helper-script substitution honesty).

The Atlas Daily Digest incident (silent non-delivery 2026-05-30 →
2026-06-16): a cron wrapper shipped with literal ``{bot_id}`` /
``{telegram_chat_id}`` placeholders, failed every morning, and its
trailing ``exit 0`` reported success. These tests pin the detector that
both the installer (forge) and the runtime monitor use to refuse / flag
such wrappers.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.placeholder_lint import (  # noqa: E402
    helper_script_from_command,
    render_command_wrapper,
    scan_residual_placeholders,
)

# The exact wrapper body found on the live pod (2026-06-16).
_ATLAS_BROKEN_WRAPPER = """\
#!/bin/bash
# atlas-digest-cron.sh — daily-digest cron trigger.
# Installed by atlas-daily-digest app. LaunchDaemon fires once per day at {digest_time}.
# Silent on success; logs errors to /tmp.

WORKSPACE="/Users/{bot_id}/.openclaw/workspace"
LOGFILE="/tmp/{bot_id}-atlas-digest.log"

python3 "$WORKSPACE/scripts/atlas_digest.py" send \\
  --bot-id "{bot_id}" \\
  --chat-id "{telegram_chat_id}" \\
  --time-zone "{time_zone}" \\
  --detail "{detail}" \\
  >> "$LOGFILE" 2>&1

exit 0
"""


# ── scan_residual_placeholders ───────────────────────────────────────────────


def test_scan_flags_every_residual_placeholder_in_atlas_wrapper() -> None:
    residual = scan_residual_placeholders(_ATLAS_BROKEN_WRAPPER, shell_script=True)
    # Each distinct placeholder is reported once (de-duped).
    assert residual == [
        "{bot_id}",
        "{detail}",
        "{digest_time}",
        "{telegram_chat_id}",
        "{time_zone}",
    ]


def test_scan_clean_shell_wrapper_has_no_residuals() -> None:
    clean = (
        "#!/bin/bash\n"
        'WORKSPACE="/Users/atlas/.openclaw/workspace"\n'
        'python3 "$WORKSPACE/scripts/atlas_digest.py" send '
        '--chat-id "-5223931757" >> "$LOGFILE" 2>&1\n'
        "exit $?\n"
    )
    assert scan_residual_placeholders(clean, shell_script=True) == []


def test_scan_does_not_flag_real_shell_vars() -> None:
    # $WORKSPACE / ${PATH} / ${var:-x} / ${arr[@]} are real shell, not leaks.
    body = (
        "#!/bin/bash\n"
        'echo "$WORKSPACE ${PATH} ${name:-default} ${arr[@]} ${HOME}"\n'
    )
    assert scan_residual_placeholders(body, shell_script=True) == []


def test_scan_dollar_known_install_var_is_flagged_even_in_non_shell() -> None:
    # ${workspace} (a known install var) is never a legitimate Python token.
    assert scan_residual_placeholders("path = '${workspace}/x'", shell_script=False) == [
        "${workspace}",
    ]


def test_scan_dollar_non_install_var_is_not_flagged() -> None:
    # Documented boundary: ${telegram_chat_id} (config, not an install var)
    # is indistinguishable from a real shell var → intentionally NOT flagged.
    # The brace form {telegram_chat_id} IS caught (test above).
    body = "#!/bin/bash\necho ${telegram_chat_id} ${time_zone}\n"
    assert scan_residual_placeholders(body, shell_script=True) == []


def test_scan_brace_in_python_is_not_flagged_unless_install_var() -> None:
    # An f-string slot {n} / {exc} is legitimate Python — must not trip.
    py = 'print(f"{n} items from {label}")\nx = "{:.2f}".format(v)\n'
    assert scan_residual_placeholders(py, shell_script=False) == []
    # But a known install var in brace form IS a leak even in Python.
    assert scan_residual_placeholders('p = "{bot_id}"', shell_script=False) == ["{bot_id}"]


def test_scan_honors_double_brace_escape() -> None:
    # {{bot_id}} is a literal {bot_id}, not a placeholder (files_pack convention).
    assert scan_residual_placeholders("echo {{bot_id}}", shell_script=True) == []


def test_scan_ignores_bash_brace_expansion() -> None:
    body = "cp file.{txt,bak}\nfor i in {1..9}; do :; done\nfind . -exec rm {} \\;\n"
    assert scan_residual_placeholders(body, shell_script=True) == []


def test_scan_empty_and_non_string() -> None:
    assert scan_residual_placeholders("", shell_script=True) == []
    assert scan_residual_placeholders(None, shell_script=True) == []  # type: ignore[arg-type]


# ── helper_script_from_command ───────────────────────────────────────────────


def test_helper_script_resolves_bash_wrapper_under_workspace() -> None:
    ws = "/Users/atlas/.openclaw/workspace"
    path, is_shell = helper_script_from_command(
        f"/bin/bash {ws}/scripts/atlas-digest-cron.sh", workspace_root=ws,
    )
    assert path == Path(ws) / "scripts" / "atlas-digest-cron.sh"
    assert is_shell is True


def test_helper_script_relative_path_joined_to_workspace() -> None:
    ws = "/Users/atlas/.openclaw/workspace"
    path, is_shell = helper_script_from_command(
        "/bin/bash scripts/cron.sh", workspace_root=ws,
    )
    assert path == Path(ws) / "scripts" / "cron.sh"
    assert is_shell is True


def test_helper_script_python_script_is_non_shell() -> None:
    ws = "/Users/atlas/.openclaw/workspace"
    path, is_shell = helper_script_from_command(
        f"/usr/bin/python3 {ws}/scripts/digest.py --flag", workspace_root=ws,
    )
    assert path == Path(ws) / "scripts" / "digest.py"
    assert is_shell is False


def test_helper_script_env_python_resolves_to_script_not_interpreter() -> None:
    # `/usr/bin/env python3 scripts/x.py` must resolve to x.py, NOT to a
    # bogus <workspace>/python3 (the env-wrapper false-negative).
    ws = "/Users/atlas/.openclaw/workspace"
    path, is_shell = helper_script_from_command(
        "/usr/bin/env python3 scripts/digest.py --flag", workspace_root=ws,
    )
    assert path == Path(ws) / "scripts" / "digest.py"
    assert is_shell is False


def test_helper_script_env_with_assignment_and_bash() -> None:
    ws = "/Users/atlas/.openclaw/workspace"
    path, is_shell = helper_script_from_command(
        "/usr/bin/env TZ=UTC bash scripts/cron.sh", workspace_root=ws,
    )
    assert path == Path(ws) / "scripts" / "cron.sh"
    assert is_shell is True


def test_helper_script_outside_workspace_returns_none() -> None:
    ws = "/Users/atlas/.openclaw/workspace"
    path, _ = helper_script_from_command(
        "/bin/bash /etc/some-system-script.sh", workspace_root=ws,
    )
    assert path is None


def test_helper_script_bare_binary_returns_none() -> None:
    ws = "/Users/atlas/.openclaw/workspace"
    path, _ = helper_script_from_command("/usr/bin/true", workspace_root=ws)
    assert path is None


# ── render_command_wrapper ───────────────────────────────────────────────────


def test_render_wrapper_propagates_exit_code_and_has_no_residuals() -> None:
    out = render_command_wrapper(
        inner_command=(
            '/usr/bin/python3 /Users/atlas/.openclaw/workspace/scripts/atlas_digest.py '
            'send --bot-id "atlas" --chat-id "-5223931757" --time-zone "UTC" '
            '--detail "standard"'
        ),
        log_path="/tmp/atlas-atlas-digest.log",
        header="atlas-daily-digest cron trigger",
    )
    # Honest exit semantics — never a blanket `exit 0` statement.
    assert out.rstrip().endswith("exit $?")
    assert "\nexit 0\n" not in out
    assert "exit 0" not in [ln.strip() for ln in out.splitlines()]
    # Fully resolved: a correct install writes a placeholder-free wrapper.
    assert scan_residual_placeholders(out, shell_script=True) == []
