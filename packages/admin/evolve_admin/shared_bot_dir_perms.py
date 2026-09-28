"""The evolve-user write ACE on ``{sharedDir}/{botId}/`` — twin of ``tier_prefs_acl``.

``tier_prefs_acl`` grants the BOT user a dir-level write ACE on its own per-bot
shared dir so the plugin's same-dir temp + rename can land
``user-tier-prefs.json``. This module is the mirror image: the ``evolve``
service user needs the same dir-level write on the same directory, because
evolve-user jobs also write files *directly at that level* — today
``usage-by-app.json`` (AL-1.3's per-app rollup, written by
``analyzer/usage_by_app.py`` as ``evolve``).

WHY THIS EXISTS (incident, 2026-08-17). ``{sharedDir}/{botId}/`` has no
ownership guarantee — ``fix_shared_dir_permissions`` says so in as many words
("the parent {sharedDir}/{bot_id}/ may be evolve-owned … chowning it is
fragile. We only own the turns/ leaf"). Whoever mkdir's first owns it, and
``TurnObserver.writeTurnToShared`` does ``mkdirSync({shared}/{bot}/turns,
{recursive:true})`` as the BOT user — so on a bot where the plugin won that
race the dir is ``<bot>:wheel 0755`` and every evolve-user writer gets EACCES.
That is exactly what happened on the mini the first night AL-1.3's rollup ran:
8 of 9 bots wrote fine, ``atlas`` failed with
``[Errno 13] Permission denied: '/Users/Shared/evolve/atlas/.usage-by-app-*.tmp'``.

The leaf-subdir writers (``turns/``, ``recommendations/``, ``metrics/{bot}/``)
never noticed because ``fix_shared_dir_permissions`` pre-creates each leaf with
its own mode + ACL. Only the two writers at the PARENT level are exposed, and
until now only the bot-direction one had a fix.

WHY AN ACE AND NOT A CHOWN. Chowning the parent to ``evolve`` is what a human
would reach for (and is what unblocked ``atlas`` by hand on 2026-08-17), but
the codebase deliberately refuses it: the dir is co-owned in practice, the
chown races the plugin's mkdir, and a bot-owned dir that suddenly becomes
evolve-owned silently removes the bot's ability to create NEW subdirs there.
An ACE adds the one capability that is missing without taking anything away,
and it heals a dir in either ownership state.

WHY NOT STICKY 1777. The obvious "make it multi-writer like ``metrics/`` and
``annotations/``" is a CROSS-BOT WIDENING: every bot user could then create
and replace entries in every other bot's dir. ``tier_prefs_acl``'s grant is
deliberately scoped to one named user on one named dir for exactly that reason
("no group/other widening — cross-bot stays closed"), and this module holds
that line. Read that module's ``THE FILE GRANULARITY IS NOT NARROW`` note
before adding a file to this directory: a dir-level ACE authorises
create/replace of EVERY entry at the top of the dir, so an evolve-authored
file here is bot-replaceable and vice versa. Security decisions must be
validated at the reader, not assumed from the path.

SAFETY GATE. The privileged-target gate is ``tier_prefs_acl``'s — imported,
not re-implemented. Same call site (``{sharedDir}/{botId}``), same hazard
(``{sharedDir}`` is sticky world-writable, so a bot can plant a symlink at a
not-yet-created bot-id name and have a root-context deploy write an ACE onto
the target), so it must be the same gate. Re-implementing it here is how a
chokepoint ends up complete for one class and open for its sibling.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from platform_profile import get_profile as _get_profile

from .runtime import get_perms as _get_perms
from .tier_prefs_acl import _MISSING, resolve_bot_dir_for_acl

if TYPE_CHECKING:
    from .deploy import _PermCheck

log = logging.getLogger(__name__)

#: Files evolve-user jobs write directly at ``{sharedDir}/{botId}/`` (the
#: reason this grant exists). Informational — the ACE is dir-level and
#: reaches every entry, see the module docstring.
EVOLVE_PARENT_LEVEL_FILES = ("usage-by-app.json",)

#: macOS dir-form ACL verbs — same set as the bot-direction grant: create the
#: tmp file (``add_file``), rename over the dest (``delete_child``), traverse
#: + list. NO inherit flags: files written here get their mode from the
#: writer, and minting an inherited ACE onto every future file would widen
#: for zero benefit.
EVOLVE_BOT_DIR_ACL_PERMS = "list,search,add_file,delete_child"

#: Linux POSIX entry bits. Capital X = execute-only-for-directories, matching
#: the Perms seam's verb vocabulary. Access ACL only — never ``-d`` (a default
#: ACL here would mint group/world-readable files, the #3198 incident class).
EVOLVE_BOT_DIR_ACL_BITS = "rwX"


def _evolve_user() -> str:
    # Lazy: deploy imports this module at load, so a module-level import
    # would be a cycle. Same shape as tier_prefs_acl's _PermCheck import.
    from .deploy import EVOLVE_SERVICE_USER
    return EVOLVE_SERVICE_USER


def _apply_evolve_bot_dir_acl(bot_dir: Path) -> bool:
    """Grant the evolve user the write ACE on ``bot_dir``.

    Applied directly (no sudo): the caller is either the dir owner or root
    (deploys run under ``sudo evolve-admin``), and an inode's owner may set
    its ACL without privilege. Best-effort — any failure returns ``False``
    and the caller logs + continues; the drift check re-reports it on the
    next ``ensure_pod_perms`` pass rather than aborting a deploy.
    """
    profile = _get_profile()
    user = _evolve_user()
    if profile.name == "linux":
        if profile.setfacl is None:
            return False
        argv = [profile.setfacl, "-m",
                f"u:{user}:{EVOLVE_BOT_DIR_ACL_BITS}", str(bot_dir)]
    else:
        argv = [profile.chmod, "+a",
                f"user:{user} allow {EVOLVE_BOT_DIR_ACL_PERMS}", str(bot_dir)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — an ACL hiccup must never crash a deploy
        return False
    if proc.returncode == 0:
        return True
    # macOS answers a duplicate ACE with rc 1 + "exists" — already correct.
    return profile.name != "linux" and "exists" in (proc.stderr or "").lower()


def ensure_evolve_bot_dir_acl(shared_dir: Path, bot_id: str) -> bool:
    """Idempotently grant the evolve user the write ACE on ``{sharedDir}/{botId}/``.

    Check-first, so a correct dir never re-fires the grant. ``True`` (no-op)
    when the dir does not exist yet — the deploy that creates it applies the
    grant. ``False`` when the target is unsafe or the grant failed; callers
    log and continue.
    """
    bot_dir, reason = resolve_bot_dir_for_acl(shared_dir, bot_id, _evolve_user())
    if bot_dir is None:
        if reason == _MISSING:
            return True
        log.warning("evolve per-bot-dir ACL grant refused: %s", reason)
        return False
    if _get_perms().acl_user_effective(bot_dir, _evolve_user(), EVOLVE_BOT_DIR_ACL_PERMS):
        return True
    return _apply_evolve_bot_dir_acl(bot_dir)


def check_evolve_bot_dir_acl(shared_dir: Path, bot_id: str) -> "_PermCheck":
    """Drift check for the evolve write ACE on ``{sharedDir}/{botId}/``.

    Wired into ``ensure_pod_perms`` so the hourly pod-perms drift monitor
    catches a dir that the plugin re-created bot-owned between deploys —
    the failure mode this module exists for is silent (one bot's rollup
    file just stops updating), so it needs a watcher, not only a deploy-time
    repair.

    An UNSAFE target is reported with ``apply=None``: the self-heal must
    never be what lands an ACE on an attacker-chosen path. ``ensure_pod_perms``
    skips those silently and the drift monitor still rides the ``ok=False``
    into its Signal.
    """
    from .deploy import _PermCheck  # lazy: deploy imports this module at load

    category = "ACL"
    bot_dir, reason = resolve_bot_dir_for_acl(shared_dir, bot_id, _evolve_user())
    if bot_dir is None:
        target = str(Path(shared_dir) / bot_id)
        if reason == _MISSING:
            return _PermCheck(
                category=category, target=target, ok=True,
                detail="(per-bot shared dir not yet created — skipping evolve write ACL)",
            )
        return _PermCheck(
            category=category, target=target, ok=False, detail=reason,
            fix_description=(
                f"inspect {target} by hand (`ls -ld`) — the evolve write ACE is "
                f"NOT applied automatically to an unsafe target. Remove the "
                f"symlink / restore the real directory, then re-run "
                f"`ensure-pod-perms`."
            ),
            apply=None,
        )
    user = _evolve_user()
    if _get_perms().acl_user_effective(bot_dir, user, EVOLVE_BOT_DIR_ACL_PERMS):
        return _PermCheck(
            category=category, target=str(bot_dir), ok=True,
            detail=f"user:{user} per-bot-dir write ACL present",
        )
    profile = _get_profile()
    if profile.name == "linux":
        fix = (f"setfacl -m u:{user}:{EVOLVE_BOT_DIR_ACL_BITS} {bot_dir}  "
               f"(evolve write ACE for {', '.join(EVOLVE_PARENT_LEVEL_FILES)} "
               f"temp+rename; no default ACL)")
    else:
        fix = (f'chmod +a "user:{user} allow {EVOLVE_BOT_DIR_ACL_PERMS}" {bot_dir}  '
               f"(evolve write ACE for {', '.join(EVOLVE_PARENT_LEVEL_FILES)} "
               f"temp+rename; no inherit)")
    return _PermCheck(
        category=category, target=str(bot_dir), ok=False,
        detail=(
            f"user:{user} per-bot-dir write ACL missing — evolve-user jobs "
            f"writing {', '.join(EVOLVE_PARENT_LEVEL_FILES)} here will EACCES "
            f"if the dir is not evolve-owned"
        ),
        fix_description=fix,
        # Re-gate at APPLY time: checks all run before applies, so a target
        # that was safe at check time could have been swapped since.
        apply=lambda: ensure_evolve_bot_dir_acl(shared_dir, bot_id),
    )


def per_bot_dir_ids(shared_dir: Path) -> list[str]:
    """Bot ids whose ``{sharedDir}/{botId}/`` this contract covers.

    The union of two sources, because neither alone is complete:

    * ``network.json::members`` (+ the ``evolve`` bot, which runs the plugin
      like any other) — covers a bot whose dir does not exist yet, so the
      grant lands the moment the dir appears;
    * dirs that already look per-bot (they contain a ``turns/``) — covers a
      renamed or retired bot that still holds a dir with live files in it.

    Unreadable/absent network.json degrades to the on-disk half rather than
    failing the deploy.
    """
    ids: set[str] = set()
    shared = Path(shared_dir)
    try:
        import json
        net = json.loads((shared / "network.json").read_text())
        ids.update(net.get("members") or [])
        ids.add("evolve")
    except (OSError, ValueError):
        log.debug("per-bot dir perms: network.json unreadable — using on-disk dirs only")
    for turns in shared.glob("*/turns"):
        if turns.is_dir():
            ids.add(turns.parent.name)
    return sorted(ids)


def reassert_per_bot_dir_perms(shared_dir: Path, sudo_chmod) -> None:
    """Pod-wide pass over every ``{sharedDir}/{botId}/``.

    Two invariants, both idempotent:

    * ``{botId}/turns/`` stays sticky 1777 — each bot writes its own turn
      files and the sticky bit stops cross-bot deletes. (Moved here from
      ``deploy.deploy_shared_dir``, which had it inline as a glob loop.)
    * the parent ``{botId}/`` carries the evolve write ACE, so evolve-user
      jobs can write ``usage-by-app.json`` there whichever user won the
      mkdir race.

    ``sudo_chmod`` is the caller's privileged fallback for the chmod
    (``lambda path: _run_sudo([...], result)``) — the mode fix may need root
    when the dir owner is a bot user.
    """
    shared = Path(shared_dir)
    for bot_id in per_bot_dir_ids(shared):
        turns = shared / bot_id / "turns"
        if turns.is_dir():
            try:
                turns.chmod(0o1777)
            except (PermissionError, OSError):
                sudo_chmod(turns)
        # No-ops when the dir does not exist yet; the gate re-validates the id.
        ensure_evolve_bot_dir_acl(shared, bot_id)
