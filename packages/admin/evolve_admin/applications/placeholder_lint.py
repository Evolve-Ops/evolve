"""
placeholder_lint.py — residual install-template placeholder detection.

Spec: docs/spec-apps-meta-2026-06-13.md (helper-script substitution honesty).

The Atlas Daily Digest incident (silent non-delivery 2026-05-30 →
2026-06-16): a launchd cron wrapper (``scripts/atlas-digest-cron.sh``)
shipped with literal, never-substituted template placeholders —

    WORKSPACE="/Users/{bot_id}/.openclaw/workspace"
    python3 "$WORKSPACE/scripts/atlas_digest.py" send \\
      --chat-id "{telegram_chat_id}" --time-zone "{time_zone}" ...
    exit 0

— so every 07:00 run failed with ``can't open file '/Users/{bot_id}/...'``
while the trailing ``exit 0`` reported success to launchd. Nothing in the
system caught it for two weeks: forge substitutes the *plist command* but
not the helper-script *body* it points at, and the monitor was blind to
the action.

This module is the shared "is this script still a template?" check used by
two guards (single source of truth so "broken here" means "broken there"):

  * forge / install_helpers (install-time): refuse to bootstrap a launchd
    job whose helper script still contains unsubstituted placeholders —
    never silently install a broken wrapper.
  * delivery_monitor (runtime): fire ``app_script_template_unresolved``
    for an installed launchd action whose on-disk helper script still has
    residual placeholders (the exit-0-masked failure mode).

Placeholder syntaxes recognised:

  ``{name}``    brace style (files-pack / ``str.format``). In a SHELL
                wrapper this is *never* valid (bash has no bare ``{name}``
                interpolation), so any ``{identifier}`` is a leak.
                ``{{name}}`` escapes to a literal ``{name}`` — same
                convention as ``files_pack.substitute_placeholders``.
  ``${name}``   shell style. Only the known install variables (``bot_id``,
                ``workspace``, …) are flagged — a real ``${PATH}`` /
                ``${var:-x}`` / ``${arr[@]}`` passes through untouched.

The renderer (:func:`render_command_wrapper`) is the canonical, honest
shape a generated launchd wrapper must take: fully-resolved values, output
redirected to a resolved log path, and ``exit $?`` so launchd's
``LastExitStatus`` reflects the real outcome.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# The install-context variables that the materializer
# (install_helpers._substitute_install_vars / files_pack) resolves. Reuse
# the files-pack set as the single source of truth — bot_id, bot_user,
# workspace, shared_dir, pkg_id, app_id, installed_at — so a new install
# variable is declared in exactly one place.
from .files_pack import KNOWN_PLACEHOLDERS as INSTALL_VAR_NAMES

# ``{name}`` not part of a ``{{escape}}`` and not the ``{name}`` tail of a
# ``${name}`` shell expansion (that's the _DOLLAR_RE's job — otherwise
# ``${workspace}`` would be reported twice). A single lowercase identifier
# only, so bash brace expansion (``{a,b}``, ``{1..9}``, ``find … {} \;``)
# never matches.
_BRACE_RE = re.compile(r"(?<![{$])\{([a-z_][a-z0-9_]*)\}(?!\})")

# ``${name}`` — pure identifier only, so ``${var:-x}`` / ``${arr[@]}`` /
# ``${VAR}`` (uppercase, not an install var) don't match.
_DOLLAR_RE = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")

# Command first-tokens that mean "the next path argument is a script we
# execute" — used to resolve install.command → on-disk helper script.
_SHELL_INTERPRETERS = frozenset({"/bin/bash", "/bin/sh", "/bin/zsh", "bash", "sh", "zsh"})
# ``env`` wrappers: ``/usr/bin/env [VAR=val ...] <real-interp> <script>``.
# The real interpreter (and the script) come AFTER env + any assignments.
_ENV_WRAPPERS = frozenset({"/usr/bin/env", "env"})
# A leading ``VAR=value`` environment assignment that precedes the real
# interpreter in an ``env`` command.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def scan_residual_placeholders(text: str, *, shell_script: bool) -> list[str]:
    """Return the residual install-template placeholders left in *text*.

    Args:
        text: file content (a rendered helper script).
        shell_script: True when *text* is a shell wrapper, where a bare
            ``{identifier}`` is always a leak. False (e.g. a Python script
            run directly) → only the known install variables are flagged in
            brace form, so a Python f-string's ``{n}`` / ``.format`` slot is
            not a false positive. ``${install_var}`` is always flagged
            regardless of *shell_script* (neither bash-as-template nor Python
            uses it legitimately for an install var name).

    Returns: a sorted, de-duplicated list of the literal residual tokens
        (e.g. ``["${workspace}", "{bot_id}", "{detail}"]``); empty when the
        text is fully resolved.

    Boundary (deliberate): a config placeholder accidentally written in
    shell form for a name that is NOT a known install var — e.g.
    ``${telegram_chat_id}`` — is NOT flagged, because ``${...}`` for an
    arbitrary lowercase name is indistinguishable from a real shell
    variable (``${var:-x}`` etc.) and flagging all of them would false-
    positive on legitimate wrappers. The Atlas incident used brace form
    (``{telegram_chat_id}``), which IS caught in a shell wrapper. Authors
    should template config values in brace form so this guard sees them.
    """
    if not isinstance(text, str) or not text:
        return []
    found: set[str] = set()
    for m in _BRACE_RE.finditer(text):
        name = m.group(1)
        if shell_script or name in INSTALL_VAR_NAMES:
            found.add("{" + name + "}")
    for m in _DOLLAR_RE.finditer(text):
        name = m.group(1)
        if name in INSTALL_VAR_NAMES:
            found.add("${" + name + "}")
    return sorted(found)


def helper_script_from_command(
    command: str, *, workspace_root: Path | str,
) -> tuple[Path | None, bool]:
    """Resolve a launchd ``install.command`` to its on-disk helper script.

    The command is expected to already have ``${bot_id}`` / ``${workspace}``
    substituted (callers do this before reaching here). Returns
    ``(script_path, is_shell)``:

      * ``script_path`` — the absolute path of the script the command
        executes, when the command is an interpreter (bash / sh / python)
        invoking a script that lives under *workspace_root*. ``None`` when
        the command isn't an interpreter+script shape we can resolve (a
        bare binary, an absolute non-workspace path, …) — there is no
        in-workspace helper body to lint.
      * ``is_shell`` — True when the interpreter is a shell, so the caller
        knows whether bare ``{name}`` placeholders are leaks.

    Best-effort: returns ``(None, False)`` on an unparseable command.
    """
    workspace_root = Path(workspace_root)
    try:
        parts = shlex.split(command)
    except ValueError:
        return None, False
    if not parts:
        return None, False

    def _is_shell(tok: str) -> bool:
        return tok in _SHELL_INTERPRETERS or Path(tok).name in _SHELL_INTERPRETERS

    interp = parts[0]
    interp_base = Path(interp).name
    is_shell = _is_shell(interp)

    # ``/usr/bin/env [VAR=val ...] <real-interp> <script>``: the real
    # interpreter and the script come AFTER env + any leading assignments.
    # Without this, ``env python3 scripts/x.py`` would resolve to the
    # interpreter token instead of the script — a silent false-negative.
    rest_start = 1
    if interp in _ENV_WRAPPERS or interp_base in _ENV_WRAPPERS:
        i = 1
        while i < len(parts) and _ASSIGNMENT_RE.match(parts[i]):
            i += 1
        if i < len(parts):
            is_shell = _is_shell(parts[i])  # the real interpreter
            i += 1
        rest_start = i

    # Find the first argument that looks like a script path (not a flag).
    script_arg = ""
    for arg in parts[rest_start:]:
        if arg.startswith("-"):
            continue
        script_arg = arg
        break
    if not script_arg:
        return None, is_shell

    candidate = Path(script_arg)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate

    # Only return scripts that live under the workspace — an absolute
    # /bin/foo or a system path is not an app-owned helper body to lint.
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        return None, is_shell

    # If the interpreter wasn't a shell, only treat a .sh as shell; a .py
    # run via python is non-shell (brace f-strings are legitimate there).
    if not is_shell and candidate.suffix == ".sh":
        is_shell = True
    return candidate, is_shell


def render_command_wrapper(
    *, inner_command: str, log_path: str, header: str = "",
) -> str:
    """Render a canonical, honest launchd cron wrapper.

    Args:
        inner_command: the fully-resolved command line to run (a single
            shell line — the caller is responsible for substituting every
            install/config variable; this function does not substitute).
        log_path: a resolved absolute path for combined stdout+stderr. Must
            contain no placeholders (the caller resolves it).
        header: optional one-line description for the comment banner.

    The wrapper redirects output to *log_path* and **propagates the inner
    command's exit code** (``exit $?``) — never a blanket ``exit 0``, so a
    failing run surfaces in launchd's ``LastExitStatus`` (and the
    cron_exit_monitor Signal) instead of masquerading as success.

    The output is guaranteed placeholder-free for resolved inputs; callers
    SHOULD assert ``scan_residual_placeholders(out, shell_script=True) == []``
    before installing (see install_helpers).
    """
    banner = f"# {header}\n" if header else ""
    return (
        "#!/bin/bash\n"
        f"{banner}"
        "# Generated by evolve_admin.applications.placeholder_lint — do not edit by hand.\n"
        "# Propagates the inner command's exit code so launchd sees real failures\n"
        "# (no blanket `exit 0` — that masked the Atlas Daily Digest outage).\n"
        f'{inner_command} >> "{log_path}" 2>&1\n'
        "exit $?\n"
    )
