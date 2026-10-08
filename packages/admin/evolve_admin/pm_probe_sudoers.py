"""``evolve-admin install-pm-probe-sudoers`` — the privileged half of the PM probe.

Brief: internal/dispatch/done/pm-mini-probe-readonly-mcp.md.
Client: ``tools/pm-mini-probe`` (a read-only stdio MCP server on the operator's
laptop that runs one allowlisted ``ssh <pod> <cmd>`` per call).

Most of what the PM measures needs no privilege at all: the deploy checkout is
world-readable and ``readlink``/``ls``/``stat`` answer as the admin account.
Two read shapes need root on the pod, and this file is the ONE place they are
written down:

1. **File reads**, through :mod:`evolve_admin.pm_probe_cat` — a root-owned,
   fixed-argv reader installed at ``/usr/local/libexec/pm-probe-cat``. One
   grant line names it, and the reader itself enforces one operand, a
   ``realpath`` root check, no symlink at any component, no credential-shaped
   path, and a 64 KB cap.
2. The **enumerated** ``evolve-admin`` read forms
   (:data:`PM_PROBE_EVOLVE_ADMIN_READS`) — each rendered as the EXACT argument
   string it is, never with a trailing wildcard.

Why there is no ``cat`` grant and no wildcard argument anywhere
--------------------------------------------------------------
The first cut granted ``/bin/cat`` on a ladder of depth patterns
(``{root}/*``, ``{root}/*/*``, …) and documented the ladder as the cap. That
was false. Per sudoers(5) § Wildcards, ``/`` is excluded from wildcard
matching only in the *file name portion of the command*; command-line
ARGUMENTS are matched as a single space-separated string, so ``*`` crosses
``/`` and spaces both — the man page's own example admits
``cat /var/log/messages /etc/shadow`` under ``/bin/cat /var/log/messages*``.
Every rung was therefore passwordless root read of any file on the pod, and
``sudo cat`` follows symlinks out of bot-writable trees. The same reading
condemned ``<verb> *`` and ``context_census.py *``: those grant root an
arbitrary argument STRING, which is how ``audit-acls --apply``,
``health --fix`` and ``context_census.py --json-out /etc/...`` were reachable
without a password even though the probe refuses all three.

So: no wildcard occupies an argument position in this file. What sudo cannot
constrain, the reader constrains as root; what can be written out exactly is
written out exactly. The reader's own grant deliberately carries NO argument
specification — in sudoers that means "any arguments", which is the honest
shape for a program whose entire job is to vet its operand, and it is not a
pattern that pretends to bound anything.

``python3`` is granted nothing. ``context_census.py`` requires ``--bot <id>``,
a free value that cannot be enumerated and would need exactly the wildcard
this file no longer contains; the probe still runs it UNPRIVILEGED, as the
admin account, which reads bots' ``.openclaw/`` through the ACL that
``ensure-pod-perms`` maintains. If a measurement is ever proven to need root,
the honest way back is a fixed-argv wrapper like the reader — not a ``*``.

House rules this render obeys (CLAUDE.md § sudoers, and the same discipline as
``setup_wizard._render_evolve_sudoers``):

* every binary path and platform root comes from
  ``platform_profile.get_profile()`` — one command table, so "what the probe
  may sudo" and "where those binaries actually live" cannot drift apart;
* **no trailing ``/*``** and no escaped dots — macOS visudo rejects both;
* every grant is a read, and the file is validated with ``visudo -c`` before
  it is installed.

The renderer is pure. ``install_pm_probe_sudoers`` stages to a temp file,
validates with ``visudo -c``, installs — the same shape, and the same "never
hand-written" rule, as the two sudoers files that came before it — and then
installs the reader the grant names.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from rich.console import Console

from .pm_probe_install import SUDOERS_PATH, WRAPPER_PATH, install_wrapper

console = Console()

# ``evolve-admin`` invocations granted root, as EXACT argument strings. No
# trailing wildcard: sudo would match any tail after the verb, and several of
# these verbs carry a write flag one token away (``audit-acls --apply``,
# ``health --fix``). Flags here are the read-only ones their click parsers
# actually accept — read once, on 2026-09-07, and pinned.
#
# Deliberately absent: `list-rollback-points` and `lifecycle inventory`, whose
# required positional BOT_ID cannot be written out without a wildcard. The
# probe still runs both unprivileged. `board token` mints a bearer token, so
# the whole `board` group is absent, as is every deploy / rollback / retire /
# delete / refresh-sudoers verb.
PM_PROBE_EVOLVE_ADMIN_READS: tuple[str, ...] = (
    "health",
    "health --json",
    "audit-acls",
    "audit-acls --json",
    "recovery-status",
    "recovery-status --json",
    "list-rollbacks",
    "list-rollbacks --json",
    "ensure-pod-perms --check-only",
    "release status",
    "release status --json",
    "breaker status",
    "breaker status --json",
    "features list",
)


def render_pm_probe_sudoers(admin_user: str) -> str:
    """Render /etc/sudoers.d/pm-probe. Pure — does not touch disk."""
    from platform_profile import get_profile

    profile = get_profile()
    venv = profile.venv_dir
    evolve_admin = f"{venv}/bin/evolve-admin"

    lines: list[str] = [
        f"# {SUDOERS_PATH}",
        "# PM read-only pod probe — narrow NOPASSWD grants for the admin account.",
        "# Rendered by: evolve-admin install-pm-probe-sudoers  (never hand-written)",
        "# Client:      tools/pm-mini-probe (stdio MCP server on the operator's laptop)",
        "# Brief:       internal/dispatch/done/pm-mini-probe-readonly-mcp.md",
        "#",
        "# Every grant below is a READ. There is deliberately no write verb, no",
        "# service verb, and no interpreter that can be handed a script path.",
        "# Removing this file returns the probe to unprivileged reads only;",
        "# removing the MCP block on the laptop closes the door entirely.",
        "#",
        "# NO WILDCARD OCCUPIES AN ARGUMENT POSITION HERE. sudo matches command",
        "# arguments as one space-separated string, where '*' crosses '/' and",
        "# spaces (sudoers(5) Wildcards) — so a '*' anywhere below would grant",
        "# root an arbitrary argument string, not a bounded subtree.",
        "",
        f"Defaults:{admin_user} !requiretty",
        "",
        "# -- 1. Read one file, through the fixed-argv reader ------------------------",
        "# The ONLY root reader. It takes exactly one operand, realpaths it, refuses",
        "# anything outside the pod's own trees AFTER resolution, refuses a symlink at",
        "# every component AND a hard link at the leaf (a second name for an inode is",
        "# the same read channel without the symlink), refuses credential-shaped paths,",
        "# and caps output at 64 KB.",
        "# The grant carries no argument specification because sudo cannot express that",
        "# contract; the reader enforces it as root. Source: evolve_admin/pm_probe_cat.py,",
        "# installed root-owned 0755 by install-pm-probe-sudoers and kept current by",
        "# `evolve-admin ensure-pod-perms`.",
        f"{admin_user} ALL=(root) NOPASSWD: {WRAPPER_PATH}",
        "",
        "# -- 2. evolve-admin read forms ----------------------------------------------",
        "# Enumerated as EXACT argument strings. 'ensure-pod-perms' appears only in its",
        "# --check-only form (without the flag it APPLIES); 'audit-acls' and 'health'",
        "# appear without --apply / --fix, which a trailing wildcard would have handed",
        "# back. Verbs needing a free positional (list-rollback-points, lifecycle",
        "# inventory) are absent — the probe runs those unprivileged.",
    ]
    for verb in PM_PROBE_EVOLVE_ADMIN_READS:
        lines.append(f"{admin_user} ALL=(root) NOPASSWD: {evolve_admin} {verb}")

    lines.append("")
    return "\n".join(lines) + "\n"


def install_pm_probe_sudoers(admin_user: str) -> bool:
    """Validate the render with ``visudo -c`` and install it. Returns True on success."""
    from platform_profile import get_profile

    profile = get_profile()
    c = profile.commands
    content = render_pm_probe_sudoers(admin_user)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # sudo-grant: ungranted-by-design: installing a sudoers file is a root
        # action the operator runs by hand; no service user may reach it.
        check = subprocess.run(
            ["sudo", c["visudo"], "-c", "-f", tmp_path], capture_output=True, text=True
        )
        if check.returncode != 0:
            console.print(f"[red]x pm-probe sudoers failed visudo -c: {check.stderr.strip()}[/]")
            return False

        # sudo-grant: ungranted-by-design: root-only install, operator-invoked.
        cp = subprocess.run(
            ["sudo", c["cp"], tmp_path, SUDOERS_PATH], capture_output=True, text=True
        )
        if cp.returncode != 0:
            console.print(f"[red]x could not install {SUDOERS_PATH}: {cp.stderr.strip()}[/]")
            return False
    finally:
        os.unlink(tmp_path)

    root_group = "root:wheel" if profile.name == "macos" else "root:root"
    # sudo-grant: ungranted-by-design: root-only install, operator-invoked.
    subprocess.run(["sudo", c["chmod"], "440", SUDOERS_PATH], capture_output=True)
    # sudo-grant: ungranted-by-design: root-only install, operator-invoked.
    subprocess.run(["sudo", c["chown"], root_group, SUDOERS_PATH], capture_output=True)

    # The grant names a file; install it in the same step, or the first
    # `sudo pm-probe-cat` fails with "command not found" and the operator has
    # a sudoers file that grants nothing.
    if not install_wrapper(profile):
        console.print(f"[red]x installed {SUDOERS_PATH} but could not install {WRAPPER_PATH}[/]")
        return False
    return True


@click.command("install-pm-probe-sudoers")
@click.option("--admin-user", default=None,
              help="Admin account the probe sshes in as (default: the invoking user).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the would-be sudoers file instead of installing it.")
def install_pm_probe_sudoers_cmd(admin_user: str | None, dry_run: bool) -> None:
    """Install /etc/sudoers.d/pm-probe + the root reader it grants.

    Run this on the pod, as root. The client half is tools/pm-mini-probe on
    the operator's laptop; without it this file grants nothing to anyone new.

    To revoke: `sudo rm /etc/sudoers.d/pm-probe /usr/local/libexec/pm-probe-cat`
    (the probe keeps working for unprivileged reads), or remove the MCP block
    on the laptop (the probe stops existing and the PM is back to asking).
    """
    user = admin_user or os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    if not user:
        console.print("[red]x could not determine the admin account — pass --admin-user[/]")
        sys.exit(1)

    if dry_run:
        console.print(render_pm_probe_sudoers(user))
        return

    if os.geteuid() != 0:
        console.print("[red]x install-pm-probe-sudoers must run as root (sudo evolve-admin ...)[/]")
        console.print("  [dim]or pass --dry-run to see the file it would install[/]")
        sys.exit(1)

    if not install_pm_probe_sudoers(user):
        sys.exit(1)
    console.print(f"[green]v {SUDOERS_PATH} installed for {user}[/]")
    console.print(f"  [dim]{len(Path(SUDOERS_PATH).read_text().splitlines())} lines; all reads[/]")
    console.print(f"  [dim]root reader installed at {WRAPPER_PATH}[/]")
