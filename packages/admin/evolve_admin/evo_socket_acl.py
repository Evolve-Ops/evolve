"""Admin-daemon unix socket: shared-bot-group connect ACL (owner-direct).

The grant + self-heal for the connect ACE on the admin-daemon unix socket.
``web.unix_socket_server`` imports :func:`_ensure_bot_socket_acl` (the at-bind
grant); ``deploy.ensure_pod_perms`` imports :func:`_check_bot_socket_acl` (the
self-heal backstop); tests import the constants + both helpers directly.

The admin daemon binds a second WSGI listener on a unix socket at
``{sharedDir}/admin-daemon.sock`` (web.unix_socket_server) so EVERY bot's
gateway subprocess can reach admin endpoints — the per-turn directory digest
render, RosterTools, and the evo/better socket transport. The socket is bound
mode 0660 owned by ``evolve``.

THE BUG (Linux, confirmed live on evolve-vps-pod 2026-06-25). Every bot user
(``evo``, ``darwin``, …) belongs to the shared ``evolve-bots`` group but NOT to
the socket-owning ``evolve`` group, so each lands on the socket's ``other``
class (``r--`` — no ``w``). A unix-socket ``connect()`` is evaluated by the
kernel as a WRITE on the socket inode → EACCES. Every plugin "directory digest
render" then fails pod-wide (``connect EACCES …/admin-daemon.sock``), breaking
RosterTools and inflating round-trips into the ``gateway_slow`` band.

THE FIX. Grant the shared bot group a connect (write) ACE on the socket —
``setfacl -m g:evolve-bots:rwx {socket}`` — applied DIRECTLY as the socket's
owning ``evolve`` process, with NO sudo. The file owner may set ACLs on its own
inode without privilege, so this needs no sudoers grant. That removes the exact
dependency that silently broke the predecessor (#3223): its ``u:evo`` grant
routed through the Perms seam's ``grant``, which sudo-wraps ``setfacl``; with no
matching sudoers rule the call was DENIED (``evolve : command not allowed ;
COMMAND=/usr/bin/setfacl -m u:evo:rwX …`` in the admin journal) and the ACE
never landed. Owner-direct fixes that AND generalizes from ``u:evo`` to the
shared GROUP, so every member bot is covered in one ACE (evo included — it is in
``evolve-bots`` too). We never widen ``other`` — the socket stays unreachable to
world; the in-process ``@require_trusted_peer`` uid-allowlist remains the second
gate (socket-layer reachability + app-layer authorization).

A socket is a single file with no children, so there is no inherit pair and no
default ACL — a plain non-recursive ``setfacl -m``. The socket is recreated on
every (re)bind (web.unix_socket_server clears the stale file then binds fresh),
so the grant must be (re)applied AT bind time; ensure_pod_perms re-asserts it as
the self-heal backstop between binds.

macOS: NO named ACE. Every macOS bot's PRIMARY group is ``staff`` and the bind
chgrp's the socket to group ``staff`` (mode 0660), so bots already reach it by
group OWNERSHIP — a macOS ``chmod +a`` group ACE would need a new sudoers grant
for zero benefit. The Linux ``evolve-bots`` group ACE is the only delta. The
shared-bot-group name is platform-keyed via
``platform_profile.bot_shared_group`` (macOS ``staff`` / Linux ``evolve-bots``).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from platform_profile import get_profile as _get_profile

from .runtime import get_perms as _get_perms

if TYPE_CHECKING:
    from .deploy import _PermCheck

log = logging.getLogger(__name__)

ADMIN_SOCKET_NAME = "admin-daemon.sock"

# Raw POSIX bits for the owner-direct ``setfacl -m g:<group>:<bits>`` grant.
# ``w`` is the load-bearing bit (a unix-socket connect() is a WRITE on the
# inode); ``r``/``x`` round it out to byte-match the operator stopgap
# (``setfacl -m g:evolve-bots:rwx``) so the durable grant is idempotent with a
# hand-applied one. ``x`` is inert on a socket (not a dir, not executable).
BOT_SOCKET_ACL_BITS = "rwx"

# macOS-verb required-perms for the EFFECTIVE drift check — the Perms seam's
# ``acl_group_effective`` speaks the macOS verb vocabulary. ``write`` is what
# connect() needs; ``read`` is checked alongside for symmetry with the grant.
BOT_SOCKET_ACL_PERMS = "read,write"


def _ensure_bot_socket_acl(socket_path: Path) -> bool:
    """Idempotently grant the shared bot group a connect(write) ACE on the
    admin socket — applied OWNER-DIRECT (no sudo).

    Linux only. The socket is bound + owned by the running admin daemon
    (``evolve``); the owner may ``setfacl`` its own inode without privilege, so
    this runs ``setfacl -m g:<bot-group>:rwx {socket}`` as the daemon's own user
    — NOT through the Perms seam (whose ``grant`` sudo-wraps setfacl and, with no
    matching sudoers rule, was silently denied: the #3223 failure this
    supersedes). No inherit flags (a socket has no children) and NO chown
    (re-chowning a live listener is a footgun).

    Returns ``False`` without acting on macOS (bots reach the socket via its
    ``staff`` group ownership — no named ACE) and on any platform whose profile
    carries no ``setfacl``. Best-effort: a setfacl failure (group absent, binary
    missing, EPERM) returns ``False``; the caller fail-soft logs + continues.
    """
    profile = _get_profile()
    if profile.name != "linux" or profile.setfacl is None:
        return False  # macOS / unknown: group-ownership reach, no named ACE
    spec = f"g:{profile.bot_shared_group}:{BOT_SOCKET_ACL_BITS}"
    try:
        proc = subprocess.run(
            [profile.setfacl, "-m", spec, str(socket_path)],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 — never let an ACL hiccup crash the bind
        return False
    return proc.returncode == 0


def _check_bot_socket_acl(shared_dir: Path) -> "_PermCheck":
    """Check {sharedDir}/admin-daemon.sock grants the shared bot group
    connect(write).

    Self-heal backstop for the AT-bind grant in
    ``web.unix_socket_server.server_bind``: the socket is recreated on every
    (re)bind, so the bind site is the primary enforcer; this drift check
    re-asserts the ACE on every deploy + the hourly pod-perms pass should a
    rebind ever land the socket without it (e.g. a bind before the
    ``evolve-bots`` group existed, then group creation, then no rebind).

    macOS (any non-Linux profile): informational pass — bots reach the socket
    via its ``staff`` group ownership, there is no named ACE to enforce. Linux
    with no socket file (TCP-only or daemon not yet bound): informational pass —
    nothing to enforce yet.
    """
    from .deploy import _PermCheck  # lazy: avoid import cycle (deploy imports this module at load)

    profile = _get_profile()
    target = shared_dir / ADMIN_SOCKET_NAME
    category = "ACL"
    group = profile.bot_shared_group
    if profile.name != "linux":
        return _PermCheck(
            category=category, target=str(target), ok=True,
            detail=(f"(macOS — bots reach socket via group:{group} ownership; "
                    "no named ACE to enforce)"),
        )
    try:
        is_sock = target.is_socket()
    except OSError:
        is_sock = False
    if not is_sock:
        return _PermCheck(
            category=category, target=str(target), ok=True,
            detail="(admin-daemon socket not bound — skipping socket ACL)",
        )
    if _get_perms().acl_group_effective(target, group, BOT_SOCKET_ACL_PERMS):
        return _PermCheck(
            category=category, target=str(target), ok=True,
            detail=f"group:{group} write(connect) ACL present",
        )
    return _PermCheck(
        category=category, target=str(target), ok=False,
        detail=f"group:{group} write(connect) ACL missing",
        fix_description=(
            f'setfacl -m g:{group}:{BOT_SOCKET_ACL_BITS} {target}  '
            f'(grant the bot group connect/write on the admin-daemon socket; '
            f'owner-direct, no sudo)'
        ),
        apply=lambda: _ensure_bot_socket_acl(target),
    )
