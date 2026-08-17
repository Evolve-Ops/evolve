"""Per-bot write ACE on ``{sharedDir}/{botId}/`` for user-tier-prefs.json (owner-direct).

The grant + self-heal for the bot-user write ACE on the bot's own per-bot
shared directory. ``deploy.fix_shared_dir_permissions`` imports
:func:`ensure_bot_tier_prefs_acl` (the at-deploy grant);
``deploy.ensure_pod_perms`` imports :func:`check_bot_tier_prefs_acl` (the
self-heal backstop); tests import the constants + both helpers directly.

THE BUG (PR #3562 follow-up). The plugin's
``ModelRouter.setStandingUserTierDefault`` runs as the BOT user and writes
``{sharedDir}/{botId}/user-tier-prefs.json`` via same-dir temp + rename. On
real pods ``{sharedDir}/{botId}/`` is owned by the ``evolve`` user (only leaf
subdirs like ``turns/``, ``spans/``, ``cascade/`` are pre-created bot-owned by
``fix_shared_dir_permissions``), so both the tmp-file create and the
rename-over-dest hit EACCES and the tool falls back to a loud "use ``evo
tier-default``" message. The admin-side writer (``evo tier-default`` →
``evo.user_tier_prefs.set_user_pref``) runs as ``evolve`` and must KEEP
working: both writers rename into the same directory and read-merge each
other's file.

THE FIX. Grant THIS bot's unix user a write ACE on THIS bot's own
``{sharedDir}/{botId}/`` dir — applied DIRECTLY as the dir-owning ``evolve``
user (or root via ``sudo evolve-admin``), with NO sudo wrapper. The file/dir
owner may set ACLs on its own inode without privilege, so this needs no
sudoers grant — the same owner-direct mechanism as
``evo_socket_acl._ensure_bot_socket_acl`` (whose predecessor #3223 routed
through the Perms seam's sudo-wrapped ``setfacl`` and was silently denied for
want of a sudoers rule).

The VERBS are exactly what temp + rename needs:

* create the tmp file       → dir write (macOS ``add_file`` / Linux ``w``)
* rename over the dest      → implicit unlink of the (possibly evolve-owned)
  dest, which on a NON-sticky dir is dir write; macOS additionally honours a
  ``delete_child`` ACE on the parent
* traverse + list           → ``search``/``list`` (``x``/``r``) — usually
  already present via the dir's 0755 mode, asserted for robustness

Deliberately NO inherit flags and NO default ACL (Linux ``-d``): the bot only
needs *directory-level* write for the rename dance; minting bot-write ACEs
onto every future evolve-written file in the dir would widen for zero
benefit. And deliberately bot-scoped: the ACE names exactly one unix user on
exactly its own per-bot dir — no grant on ``{sharedDir}`` root, no other
bot's dir, no group/other widening (cross-bot stays closed).

──────────────────────────────────────────────────────────────────────────────
THE FILE GRANULARITY IS NOT NARROW (#3566 audit A-1 — read before adding a
file to this directory).

This docstring used to claim the grant is "what temp + rename needs, and
nothing more". That is true of the *verbs* and false of the *files*: a macOS
ACL names a DIRECTORY, not a filename, so ``add_file`` + ``delete_child``
authorises create/replace/delete of EVERY entry at the top of
``{sharedDir}/{botId}/`` — not just ``user-tier-prefs.json``. Reproduced with
the literal :data:`BOT_TIER_PREFS_ACL_PERMS` string on a 0500 directory: the
same ``echo > .tmp && mv .tmp <name>`` dance that the grant exists for also
replaced a 0444 sibling the grant was never for. #3571 hardened the grant's
TARGET (symlink-free, contained) but did NOT change this reach.

#3571 filed the same observation as a docstring overstatement rather than a
defect, on the finding that everything else at that level was already
bot-owned. That inventory was incomplete. ``{sharedDir}/{botId}/tiers.json``
is evolve-authored and operator-authoritative, and it is in reach:

* ``evolve-admin models cap <bot> <n>`` and ``models user-tier-control <bot>
  off`` write ``tiers.json::userTierOverride`` (``cli.py``);
* ``home_chat_routes._read_user_tier_override`` reads it, and the Power gate
  in ``/api/home/chat`` uses ``dailyCap`` to downgrade Power → standard —
  ``dailyCap: 0`` being the operator's per-bot "Power disabled" opt-out.

So a bot could rewrite its own cost cap. Mitigated (not closed) at the READER
in #3566 A-1: a ``tiers.json`` not owned by an operator uid has its
``dailyCap`` clamped to the product default, so the untrusted direction is
one-way. The reader cannot resurrect a clobbered ``enabled: false`` — that
value exists nowhere else.

WHY THE ACE IS NOT NARROWED HERE. The obvious fix — move the bot's file to
``{sharedDir}/{botId}/tier-prefs/`` and grant there — was tested and does
confine correctly (writes inside the subdir succeed; unlink/replace of
``../tiers.json`` and ``rmdir`` of the subdir itself both EACCES). It is not
sufficient on its own, because ``user-tier-prefs.json`` is not the only
bot-written file here: the plugin's ``appScriptRegistry.writeCoverage()``
does the identical same-dir temp + rename onto
``{sharedDir}/{botId}/app-integrity-coverage.json`` (whose evolve-side
consumer, ``applications/app_integrity_coverage``, decides whether to
suppress a "May misreport failures" badge). Moving one file leaves the parent
grant load-bearing for the other, so the reach is unchanged. Narrowing for
real means relocating BOTH bot-written files under one bot-writable subdir
and dropping the parent ACE — a plugin + admin + migration change across two
languages, with a mid-upgrade window where the two sides disagree about the
path. That is a separate PR, deliberately not started here.

Until then, treat this directory as a SHARED-WRITE boundary: any new
evolve-authored file placed at this level is bot-replaceable, and any
security decision made from one must be validated at the reader.
──────────────────────────────────────────────────────────────────────────────

Degrades gracefully: when the dir is missing (bot not yet deployed) both
helpers no-op; when the grant fails (dir owner drifted off evolve and the
caller isn't root) the deploy logs + continues and the plugin's loud
fallback instruction still works. The hourly pod-perms drift monitor keeps
flagging until a root-context ``sudo evolve-admin ensure-pod-perms`` or the
next deploy repairs ownership.
"""
from __future__ import annotations

import logging
import os
import re
import stat as _stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from platform_profile import get_profile as _get_profile

from .runtime import get_perms as _get_perms

if TYPE_CHECKING:
    from .deploy import _PermCheck

log = logging.getLogger(__name__)

TIER_PREFS_FILE_NAME = "user-tier-prefs.json"

# macOS directory-form ACL verbs for the grant — exactly what same-dir
# temp + rename requires (see module docstring). ``delete_child`` is the
# rename-over-dest unlink; NO inherit flags (dir-level ACE only).
BOT_TIER_PREFS_ACL_PERMS = "list,search,add_file,delete_child"

# Raw POSIX entry bits for the owner-direct Linux ``setfacl -m`` grant.
# Capital X = execute only because the target is a directory — matches the
# Perms seam's verb-collapse vocabulary (runtime.perms._linux_entry_bits).
BOT_TIER_PREFS_ACL_BITS = "rwX"

# ── Pre-grant safety gate (post-hoc audit of #3565) ──────────────────────────
#
# THE HAZARD. ``{sharedDir}`` is mode 1777 (sticky, world-writable — asserted on
# every deploy by ``fix_shared_dir_permissions``), so ANY bot user can create an
# entry in it, and the owner of an entry may rename/delete it under sticky
# semantics. Both grant argv forms follow symlinks given on the command line
# (macOS ``chmod`` unless ``-h``; GNU ``setfacl`` unless ``-P`` — "the default
# behavior is to follow symbolic link arguments"), and BOTH pre-grant guards
# were symlink-transparent: ``Path.is_dir()`` follows, and ``ls -lde <link>``
# reports the LINK's (empty) ACL, so the check-first skip never fires and the
# grant re-lands every pass. Net effect: a bot that owns its own per-bot dir
# entry — or that pre-plants a not-yet-created bot-id name — could point
# ``{sharedDir}/{botId}`` at any directory and have the next deploy /
# ``ensure_pod_perms`` / hourly drift-monitor pass (running as ``evolve`` or,
# under ``sudo evolve-admin``, as root) write ``add_file,delete_child`` for that
# bot onto the symlink TARGET — a cross-bot / out-of-tree write primitive.
#
# THE GATE. Refuse rather than grant unless ALL of the following hold, checked
# WITHOUT following symlinks:
#   * ``bot_id`` / ``bot_user`` match a strict identifier shape — ``bot_id`` is
#     interpolated into a privileged path (``Path(shared) / "/etc"`` silently
#     ROOT-ANCHORS; ``".."`` traverses) and ``bot_user`` is interpolated into
#     the macOS ACE string, where whitespace/commas could append verbs;
#   * the entry is a real directory, NOT a symlink (``os.lstat``);
#   * it is a direct child of the real ``{sharedDir}`` (containment, so a
#     mount/hardlink surprise still reads as unsafe).
#
# RESIDUAL RACE (deliberately stated, not papered over): lstat-then-subprocess
# is still TOCTOU — an attacker who wins the window between the gate and the
# ``chmod``/``setfacl`` exec can still swap the entry. The gate converts a
# PERSISTENT plant that fires reliably on every hourly pass into a narrow race
# that must be timed against a deploy. Closing it fully needs an
# ``O_NOFOLLOW``-anchored fd, which neither grant form accepts.
_SAFE_BOT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# Unix account names: no whitespace, no comma, no ``:`` — the three characters
# that would let a name change the meaning of the ACE string it lands in.
_SAFE_UNIX_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")

#: ``_resolve_bot_dir`` sentinel: the entry does not exist yet (bot not
#: deployed). Distinct from "unsafe" — a missing dir is an informational no-op.
_MISSING = "missing"


def _resolve_bot_dir(
    shared_dir: Path, bot_id: str, bot_user: str,
) -> "tuple[Path | None, str]":
    """Gate ``{sharedDir}/{botId}`` for a privileged ACL grant.

    Returns ``(bot_dir, "")`` when the grant may proceed, ``(None, _MISSING)``
    when there is nothing there yet, and ``(None, <reason>)`` when the path
    must NOT be touched. See the module-level hazard note for why every check
    here is symlink-free (``os.lstat``, never ``Path.is_dir``).
    """
    if not _SAFE_BOT_ID_RE.fullmatch(bot_id or ""):
        return None, f"unsafe bot id {bot_id!r} — refusing to grant an ACL"
    if not _SAFE_UNIX_NAME_RE.fullmatch(bot_user or ""):
        return None, f"unsafe bot user {bot_user!r} — refusing to grant an ACL"
    bot_dir = Path(shared_dir) / bot_id
    try:
        st = os.lstat(bot_dir)
    except FileNotFoundError:
        return None, _MISSING
    except OSError as e:
        return None, f"cannot lstat {bot_dir}: {e}"
    if _stat.S_ISLNK(st.st_mode):
        return None, (
            f"{bot_dir} is a SYMLINK — refusing to grant an ACL through it "
            f"(a bot can plant one: {Path(shared_dir)} is sticky world-writable)"
        )
    if not _stat.S_ISDIR(st.st_mode):
        return None, f"{bot_dir} is not a directory — refusing to grant an ACL"
    # Containment: the real path must still be the direct child we named.
    try:
        real_parent = os.path.realpath(shared_dir)
        real_dir = os.path.realpath(bot_dir)
    except OSError as e:
        return None, f"cannot resolve {bot_dir}: {e}"
    if os.path.dirname(real_dir) != real_parent or os.path.basename(real_dir) != bot_id:
        return None, (
            f"{bot_dir} resolves outside {shared_dir} (to {real_dir}) — "
            f"refusing to grant an ACL"
        )
    return bot_dir, ""


def _apply_bot_tier_prefs_acl(bot_dir: Path, bot_user: str) -> bool:
    """Grant ``bot_user`` the write ACE on ``bot_dir`` — OWNER-DIRECT (no sudo).

    Linux: ``setfacl -m u:<bot>:rwX <dir>`` — access ACL only, NO ``-d``
    (no default-ACL minting; see the group/world-readable incident class in
    the Linux default-ACL memory) and NEVER raw ``chmod`` (which recalculates
    the ACL mask and can lock evolve out).

    macOS: ``chmod +a "user:<bot> allow list,search,add_file,delete_child"``
    — dir-form verbs, no inherit flags. macOS answers a duplicate ACE with
    rc 1 + "exists" on stderr; treat that as success (idempotence, same
    contract as the Perms seam's ``_chmod_plus_a``).

    Best-effort: any failure (unknown user, EPERM because the dir owner
    drifted off evolve and we aren't root, missing binary) returns ``False``;
    callers fail-soft log + continue — the plugin's fallback message is the
    behavioural floor.
    """
    profile = _get_profile()
    if profile.name == "linux":
        if profile.setfacl is None:
            return False
        argv = [profile.setfacl, "-m",
                f"u:{bot_user}:{BOT_TIER_PREFS_ACL_BITS}", str(bot_dir)]
    else:
        argv = [profile.chmod, "+a",
                f"user:{bot_user} allow {BOT_TIER_PREFS_ACL_PERMS}",
                str(bot_dir)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — an ACL hiccup must never crash a deploy
        return False
    if proc.returncode == 0:
        return True
    # macOS duplicate-ACE response — already correct, treat as success.
    return profile.name != "linux" and "exists" in (proc.stderr or "").lower()


def ensure_bot_tier_prefs_acl(shared_dir: Path, bot_id: str, bot_user: str) -> bool:
    """Idempotently grant ``bot_user`` the write ACE on ``{sharedDir}/{botId}/``.

    Check-first so a correct dir never re-fires the grant (no unbounded
    duplicate-ACE growth on macOS, no redundant setfacl on Linux). No-op
    ``True`` when the per-bot dir doesn't exist yet — the first deploy that
    creates it (``fix_shared_dir_permissions`` pre-creates ``turns/`` with
    parents) applies the grant.

    Gated by :func:`_resolve_bot_dir`: an unsafe target (symlink, non-dir,
    out-of-tree, malformed id/user) returns ``False`` so the caller logs and
    continues — the plugin's loud fallback message is the behavioural floor,
    and ``check_bot_tier_prefs_acl`` surfaces the same condition as an
    operator-visible drift on the next ``ensure_pod_perms`` pass.
    """
    bot_dir, reason = _resolve_bot_dir(shared_dir, bot_id, bot_user)
    if bot_dir is None:
        if reason == _MISSING:
            return True
        log.warning("tier-prefs ACL grant refused: %s", reason)
        return False
    if _get_perms().acl_user_effective(bot_dir, bot_user, BOT_TIER_PREFS_ACL_PERMS):
        return True
    return _apply_bot_tier_prefs_acl(bot_dir, bot_user)


def check_bot_tier_prefs_acl(
    shared_dir: Path, bot_id: str, bot_user: str,
) -> "_PermCheck":
    """Check ``{sharedDir}/{botId}/`` grants ``bot_user`` the write ACE.

    Self-heal backstop for the at-deploy grant in
    ``fix_shared_dir_permissions`` — re-asserts the ACE on every
    ``ensure_pod_perms`` pass (per-bot deploy, pod-wide sweep, hourly drift
    monitor). Dir not yet created: informational pass — nothing to enforce
    until the bot's first deploy creates it.

    An UNSAFE target (symlink, non-dir, out-of-tree, malformed id/user — see
    :func:`_resolve_bot_dir`) is reported as a drift with **no** ``apply``: the
    self-heal must never be the thing that lands an ACE on an attacker-chosen
    path. ``ensure_pod_perms`` skips ``apply=None`` drifts silently, and
    ``pod_perms_drift_monitor`` rides every ``ok=False`` check into its Signal —
    so the refusal reaches the operator instead of being swallowed.
    """
    from .deploy import _PermCheck  # lazy: avoid import cycle (deploy imports this module at load)

    bot_dir_or_none, reason = _resolve_bot_dir(shared_dir, bot_id, bot_user)
    category = "ACL"
    if bot_dir_or_none is None:
        target = str(Path(shared_dir) / bot_id)
        if reason == _MISSING:
            return _PermCheck(
                category=category, target=target, ok=True,
                detail="(per-bot shared dir not yet created — skipping tier-prefs ACL)",
            )
        return _PermCheck(
            category=category, target=target, ok=False, detail=reason,
            fix_description=(
                f"inspect {target} by hand (`ls -ld`) — the tier-prefs write ACE "
                f"is NOT applied automatically to an unsafe target. Remove the "
                f"symlink / restore the real directory, then re-run "
                f"`ensure-pod-perms`."
            ),
            apply=None,
        )
    bot_dir = bot_dir_or_none
    if _get_perms().acl_user_effective(bot_dir, bot_user, BOT_TIER_PREFS_ACL_PERMS):
        return _PermCheck(
            category=category, target=str(bot_dir), ok=True,
            detail=f"user:{bot_user} tier-prefs write ACL present",
        )
    profile = _get_profile()
    if profile.name == "linux":
        fix = (f"setfacl -m u:{bot_user}:{BOT_TIER_PREFS_ACL_BITS} {bot_dir}  "
               f"(bot-user write ACE for {TIER_PREFS_FILE_NAME} temp+rename; "
               f"owner-direct, no sudo, no default ACL)")
    else:
        fix = (f'chmod +a "user:{bot_user} allow {BOT_TIER_PREFS_ACL_PERMS}" '
               f"{bot_dir}  (bot-user write ACE for {TIER_PREFS_FILE_NAME} "
               f"temp+rename; owner-direct, no sudo, no inherit)")
    return _PermCheck(
        category=category, target=str(bot_dir), ok=False,
        detail=f"user:{bot_user} tier-prefs write ACL missing",
        fix_description=fix,
        # Re-gate at APPLY time, not just at check time: ``ensure_pod_perms``
        # runs every check first and applies afterwards, so a target that was
        # safe during the check could have been swapped by then. Routing the
        # apply back through ``ensure_bot_tier_prefs_acl`` re-runs
        # ``_resolve_bot_dir`` against the path as it is NOW.
        apply=lambda: ensure_bot_tier_prefs_acl(shared_dir, bot_id, bot_user),
    )
