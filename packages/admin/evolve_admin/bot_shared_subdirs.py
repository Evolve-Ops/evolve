"""The per-bot shared-store subdir registry — ONE list, two consumers.

``deploy.fix_shared_dir_permissions`` pre-creates a handful of leaf
directories under ``{sharedDir}`` on every deploy, each bot-owned so the
bot's gateway plugin can write into it, most of them carrying an inheritable
``evolve`` read ACE so admin-side readers can read files the bot minted at
umask 077 (mode 0600).  Creating one takes three privileged steps the
``evolve`` service user cannot do unaided — ``mkdir`` (when the bot won the
creation race and owns the parent), ``chmod``, ``chown`` — plus a fourth,
``chmod +a``, for the read ACE.  Every one of those routes through
``sudo``, so every one needs a matching ``NOPASSWD`` line in
``/etc/sudoers.d/evolve``.

WHY THIS MODULE EXISTS (incident, 2026-09-03).  Until now the subdir list
lived twice: as a run of ``_create_bot_subdir(...)`` calls in ``deploy.py``
and as a hand-maintained run of grant lines in
``setup_wizard._render_evolve_sudoers``.  ``exec-failures/`` (2026-09-01,
#3923) and ``app-runs/`` (AL-1.2) were added to the first list and not the
second.  Both privileged steps then failed on every deploy — and
``_create_bot_subdir`` ran them ``check=False`` with the output discarded,
so nothing was logged anywhere.  ``mkdir`` succeeded unprivileged (evolve
owns the parent on most pods), which meant the directory *existed*, looked
plausible, and was silently wrong: ``evolve``-owned, ``0755``, no ACL.

The consequence was not a degraded feature but an inverted one.  The
plugin's ``ExecFailureAbsorber`` is armed on "record-or-deliver": it only
suppresses an exec-failure trailer once the ledger append has succeeded.
``appendFileSync`` into an evolve-owned ``0755`` dir gives the bot user
EACCES, so across the reference pod the absorber recorded nothing AND
absorbed nothing, while ``exec_failure_monitor`` read an empty ledger forever
and raised no Signal.  Two independent silences stacked.  ``app-runs/``
(AL-1.2 claim files) landed the same way in the same window, leaving
scheduled app turns silently unattributed.

The one bot whose ledger dir WAS correct is the tell: its parent grants the
bot ``add_file`` but not ``add_subdirectory``, so the gateway cannot have
created it — an operator ``sudo evolve-admin deploy <bot>`` did, hours after
that bot logged its last EACCES.  That path runs as real root and needs no
NOPASSWD line, which is exactly why the missing grants never surfaced when a
human was at the keyboard.

So the list is now singular.  ``deploy`` iterates it to create the dirs,
``_render_evolve_sudoers`` iterates it to render the grants, and
``check_bot_shared_subdirs`` iterates it to re-verify them on every
``ensure_pod_perms`` pass (deploy-time and hourly, via
``pod_perms_drift_monitor``).  Adding a subdir is one entry; forgetting its
grant is no longer possible.

WHAT THE REGISTRY DOES NOT FIX.  ``/etc/sudoers.d/evolve`` is refreshed by
an explicit operator ``evolve-admin refresh-sudoers`` — manual by design, so
the service user can never rewrite its own privilege boundary.  On a pod
that has already drifted, the checks below detect the drift and repair what
can be repaired without root (the ACE, which the dir's ``evolve`` owner may
set directly); restoring bot OWNERSHIP needs the refreshed grant, so the
check reports that in its fix description rather than failing silently the
way the original bug did.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from evolve_util import assert_no_symlink_in_path as _assert_no_symlink
from platform_profile import get_profile as _get_profile

from .runtime import get_perms as _get_perms

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .deploy import _PermCheck

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotSharedSubdir:
    """One per-bot leaf under ``{sharedDir}``.

    ``glob`` is the path relative to ``{sharedDir}`` with the bot id written
    as a single ``*`` — which is simultaneously the sudoers pattern (visudo
    globs do not cross ``/``, so one ``*`` pins exactly the per-bot level)
    and, with the ``*`` substituted, the real path.  Holding one string for
    both is the point: the grant and the path cannot describe different
    directories.
    """

    glob: str
    mode: int
    evolve_read_acl: bool
    why: str

    def path(self, shared_dir: Path, bot_id: str) -> Path:
        return Path(shared_dir).joinpath(*self.glob.replace("*", bot_id).split("/"))

    @property
    def mode_token(self) -> str:
        """The mode as ``chmod``'s argv writes it (``755``, ``1777``)."""
        return oct(self.mode)[2:]


#: Inheritable evolve-read grant for bot-owned shared-dir subdirs: the bot's
#: processes write files at umask 077, and the admin-side readers (measure.py,
#: cost_rollup, pressure_watchdog, exec_failure_monitor, …) need read without
#: a per-file sudo. Perm-verb portion only — the Perms seam renders the
#: platform ACE.
BOT_SHARED_SUBDIR_READ_ACL_PERMS = (
    "read,readattr,list,search,file_inherit,directory_inherit"
)

#: Creation order is contractual: ``test_deploy_perms_seam`` pins the exact
#: sequence of ACL calls a deploy issues, and the order below is the order
#: these dirs have been created in since each was added.
BOT_SHARED_SUBDIRS: tuple[BotSharedSubdir, ...] = (
    BotSharedSubdir(
        glob="annotations/*", mode=0o755, evolve_read_acl=True,
        why=(
            "TurnObserver.ts creates this lazily, but pre-creating it here lets "
            "the bot write annotations from its very first session. Without it a "
            "permissions race lands the dir evolve-owned (from a previous run) "
            "and the bot user cannot write. Inheritable evolve-read ACE because "
            "TurnObserver.writeAnnotation appends at the bot process's umask "
            "(077 -> mode 600 on 2026-05-23+), which measure.py / cost_rollup / "
            "observations.access otherwise cannot read."
        ),
    ),
    BotSharedSubdir(
        glob="*/turns", mode=0o1777, evolve_read_acl=True,
        why=(
            "The parent {sharedDir}/{bot_id}/ may be evolve-owned (created by "
            "cron jobs writing tiers.json, metrics, etc.) — chowning it is "
            "fragile. We own only the turns/ leaf; the bot can write there "
            "regardless of parent owner as long as the parent is "
            "world-executable (755+). Inheritable evolve-read ACE so the admin "
            "service can read turn files written by the bot user (mode 600)."
        ),
    ),
    BotSharedSubdir(
        glob="*/recommendations", mode=0o1777, evolve_read_acl=False,
        why=(
            "Sticky world-writable so both the evolve user (running the daily "
            "cron analysis scripts usage_logger.py, profile_builder.py, "
            "gallery_recommender.py) AND the bot user can write without "
            "permission errors. current.json and usage-stats.json are written "
            "by the evolve-user analysis jobs."
        ),
    ),
    BotSharedSubdir(
        glob="metrics/*", mode=0o1777, evolve_read_acl=True,
        why=(
            "Two-writer dir: the evolve user (cost_rollup.refresh_all from "
            "better_engine_refresh every 15 min, writing cost-<date>.json) and "
            "the bot user (RecentTranscriptCapture.ts, writing "
            "recent-transcripts.json). Without pre-creation the dir was created "
            "lazily by whichever process arrived first; when the plugin won the "
            "race the dir landed bot-owned and cost_rollup running as evolve got "
            "PermissionError on every write — that is how personal_bot's dir "
            "broke on 2026-05-15, silently killing the rollup pass for every bot "
            "iterated after it for 10 days. Sticky bit prevents cross-user "
            "deletes. Inheritable evolve-read ACE because recent-transcripts.json "
            "is written by the bot's gateway plugin at the gateway umask (0600), "
            "and app_posture_reflect / generator_runner / pod_state.turns read it "
            "as evolve."
        ),
    ),
    BotSharedSubdir(
        glob="*/spans", mode=0o755, evolve_read_acl=True,
        why=(
            "Cascade telemetry, written by the plugin's CascadeTelemetry (one "
            "spans-YYYY-MM-DD.jsonl per day). Read by audit_runner, "
            "pressure_watchdog and the routes_cascade health endpoint via "
            "observability.session_rollup.iter_turn_spans. Without pre-creation "
            "the plugin's mkdirSync fails with EACCES (parent is evolve-owned "
            "drwxr-xr-x, bot user lacks write); the plugin warns once per process "
            "— visible in the bot's gateway.log but invisible operationally — and "
            "silently emits zero spans. That dropped the entire cascade pipeline "
            "on the mini before this pre-creation landed (2026-05-28). Mode 755: "
            "exactly one writer, the bot's plugin process."
        ),
    ),
    BotSharedSubdir(
        glob="*/cascade", mode=0o755, evolve_read_acl=True,
        why=(
            "The plugin's ModelRouter writes tier1_active.json here on every "
            "tier1 grant — the telemetry-coupled-failure defense for the pressure "
            "watchdog (when spans go dark the watchdog still has these in-process "
            "counts as a floor via max(spans, in_process)). Same EACCES failure "
            "as spans/ above, same fix. Inheritable evolve-read ACE so the "
            "pressure_watchdog daemon (running as evolve) can read the file."
        ),
    ),
    BotSharedSubdir(
        glob="*/exec-failures", mode=0o755, evolve_read_acl=True,
        why=(
            "Absorbed-trailer ledger (plugin ExecFailureAbsorber; "
            "design-exec-failure-hygiene-2026-08-31 A1). Same EACCES/0600 shape "
            "as spans/ above — pre-create plus an inheritable evolve-read ACE, "
            "else the plugin's mkdirSync EACCESes on an evolve-owned parent, the "
            "armed absorber refuses to absorb (record-or-deliver), and the bot's "
            "umask-077 files land 0600 so exec_failure_monitor (A2) reads "
            "nothing. Mode 755, single writer. This entry is the one whose "
            "missing grants caused the 2026-09-03 incident in the module "
            "docstring."
        ),
    ),
    BotSharedSubdir(
        glob="*/app-runs", mode=0o755, evolve_read_acl=False,
        why=(
            "AL-1.2 claim files, written by the app-run shim running as the BOT "
            "user (inside an app's cron process) and read+unlinked by the bot's "
            "own gateway plugin. The bot's ACE on the parent grants "
            "add_file/delete_child but NOT add_subdirectory, so on a pod where "
            "{sharedDir}/{bot_id}/ is evolve-owned the shim's own mkdir would "
            "EACCES and every scheduled turn would stay silently unattributed. "
            "No evolve ACE because no evolve-user job reads claims."
        ),
    ),
)


#: Linux POSIX entry spec for the evolve read ACE — ``LinuxPerms.grant``
#: collapses the macOS verb set above to exactly these bits, and issues both
#: the access (``-m``) and default (``-d -m``) form for an inheriting dir.
#: Named here so the sudoers writer grants the argv the seam actually runs.
LINUX_READ_ACL_ENTRY = "u:evolve:rX"


# ── creation ────────────────────────────────────────────────────────────────


def _run(argv: "list[str]") -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _report(step: str, target: Path, proc: subprocess.CompletedProcess) -> bool:
    """Log a failed privileged step and return False.

    Deliberately a WARNING and not a raise: a pod may legitimately be unable
    to chown a given leaf (an operator-managed mount, a half-migrated host),
    and aborting a deploy over one leaf would trade a silent subdir for a
    silent bot.  What the 2026-09-03 incident actually cost was the *absence
    of any record*, so the fix is that the failure is now on the record — in
    the daemon log at deploy time, and as a Signal within the hour via
    ``check_bot_shared_subdirs`` -> ``pod_perms_drift_monitor``.
    """
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = err[-1][:200] if err else f"exit {proc.returncode}"
    log.warning(
        "per-bot shared subdir %s: %s failed (%s) — the bot's writer will "
        "EACCES here until it is repaired; check the NOPASSWD grants in "
        "/etc/sudoers.d/evolve and re-run `evolve-admin refresh-sudoers`",
        target, step, detail,
    )
    return False


def _redirect_safe(subdir: Path) -> bool:
    """Refuse a target a privileged step would resolve through a planted link.

    ``{sharedDir}/{botId}/`` is not evolve-private — the bot user holds an
    ``add_file`` ACE on it (``shared_bot_dir_perms``), and a symlink is a file.
    A bot could therefore plant ``{sharedDir}/{botId}/exec-failures`` pointing
    somewhere else and have a root ``chown`` or ``chmod +a`` land on the
    target. The Perms seam gates its own emitters with this same check
    (``perms._redirect_safe``); the steps here that do NOT go through the seam
    — the chown, and the unprivileged ACE fallback below — need it explicitly,
    or the fallback would be a way around the gate rather than a way past a
    missing grant.
    """
    try:
        _assert_no_symlink(subdir)
        return True
    except Exception as e:  # noqa: BLE001 - the gate reports, never raises out
        log.error(
            "per-bot shared subdir %s: refusing the privileged perm steps — %s. "
            "Inspect it by hand (`ls -ld`); nothing is applied to a redirected "
            "path.", subdir, e,
        )
        return False


def ensure_bot_subdir(
    entry: BotSharedSubdir, subdir: Path, bot_user: str, admin_group: str,
    chown_bin: str,
) -> bool:
    """Create ``subdir`` per ``entry``: mkdir, chmod, chown, ACE.

    Returns True only when every step the entry asks for succeeded.  Each
    failing step is logged (see :func:`_report`) — the pre-2026-09-03 version
    of this helper ran all of them ``check=False`` with the output discarded,
    which is why the drift survived across the reference pod for two days
    without producing a single line anywhere.
    """
    try:
        subdir.mkdir(parents=True, exist_ok=True)
        subdir.chmod(entry.mode)
    except PermissionError:
        # The bot's gateway won the creation race and owns the parent.
        profile = _get_profile()
        proc = _run(["sudo", profile.mkdir, "-p", str(subdir)])
        if proc.returncode != 0:
            return _report("mkdir", subdir, proc)
        proc = _run(["sudo", profile.chmod, entry.mode_token, str(subdir)])
        if proc.returncode != 0:
            return _report("chmod", subdir, proc)
    except OSError as e:  # noqa: BLE001 - a full disk must not abort a deploy
        log.warning("per-bot shared subdir %s: mkdir/chmod failed: %s", subdir, e)
        return False

    if not _redirect_safe(subdir):
        return False

    ok = True
    proc = _run(["sudo", chown_bin, f"{bot_user}:{admin_group}", str(subdir)])
    if proc.returncode != 0:
        ok = _report("chown", subdir, proc)
    if entry.evolve_read_acl and not _grant_read_ace(subdir):
        log.warning(
            "per-bot shared subdir %s: evolve read ACE grant failed — "
            "admin-side readers will not see the bot's 0600 files here",
            subdir,
        )
        ok = False
    return ok


def _evolve_user() -> str:
    # Lazy: deploy imports this module at load, so a module-level import
    # would be a cycle. Same shape as tier_prefs_acl / shared_bot_dir_perms.
    from .deploy import EVOLVE_SERVICE_USER
    return EVOLVE_SERVICE_USER


def _grant_read_ace(subdir: Path) -> bool:
    """Apply the inheritable evolve-read ACE, preferring the unprivileged path.

    The Perms seam's ``grant`` goes through ``sudo chmod +a`` (it has to: on a
    bot-owned dir, evolve is not the owner).  But on a pod that drifted the
    other way — the dir is evolve-OWNED because the chown never landed — the
    service user may set the ACE on its own inode with no privilege at all.
    Trying that first is what lets the drift check below repair the read half
    on a pod whose sudoers has not been refreshed yet.
    """
    perms = _get_perms()
    user = _evolve_user()
    if perms.grant(subdir, user, BOT_SHARED_SUBDIR_READ_ACL_PERMS):
        return True
    profile = _get_profile()
    if profile.name == "linux":
        if profile.setfacl is None:
            return False
        # Access + default ACL, matching the file_inherit/directory_inherit
        # verbs above. Never touches group::/other:: — minting those into the
        # default ACL is the #3198 world-readable class.
        argvs = [
            [profile.setfacl, "-m", f"u:{user}:rX", str(subdir)],
            [profile.setfacl, "-d", "-m", f"u:{user}:rX", str(subdir)],
        ]
    else:
        argvs = [[
            profile.chmod, "+a",
            f"user:{user} allow {BOT_SHARED_SUBDIR_READ_ACL_PERMS}", str(subdir),
        ]]
    for argv in argvs:
        try:
            proc = _run(argv)
        except OSError:
            return False
        if proc.returncode != 0:
            # macOS answers a duplicate ACE with rc 1 + "exists".
            if "exists" in (proc.stderr or "").lower():
                continue
            return False
    return True


def create_bot_subdirs(
    shared_dir: Path, bot_id: str, bot_user: str, admin_group: str, chown_bin: str,
) -> None:
    """Pre-create every registered per-bot subdir. Idempotent; never raises."""
    for entry in BOT_SHARED_SUBDIRS:
        ensure_bot_subdir(
            entry, entry.path(shared_dir, bot_id), bot_user, admin_group, chown_bin,
        )


# ── drift check (ensure_pod_perms / pod_perms_drift_monitor) ────────────────


def _owner_of(path: Path) -> str | None:
    try:
        import pwd
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError, ImportError):
        return None


def check_bot_shared_subdirs(
    shared_dir: Path, bot_id: str, bot_user: str,
    apply_factory: "Callable[[BotSharedSubdir, Path], Callable[[], bool]] | None" = None,
) -> "list[_PermCheck]":
    """One ``_PermCheck`` per registered subdir: owner + evolve read ACE.

    This is the self-heal the 2026-09-03 incident lacked.  ``ensure_pod_perms``
    runs it on every deploy and ``pod_perms_drift_monitor`` runs it hourly with
    ``check_only=True``, so a subdir that lands wrong is a Signal within the
    hour instead of a silence that outlives the bots writing into it.

    A subdir that does not exist yet is an informational pass — the bot may
    never have been deployed.  Mode is deliberately NOT checked: the sticky
    1777 dirs are re-widened by other code paths and a mode-only difference
    never costs a writer its access, whereas re-``chmod``ing on Linux
    recalculates the ACL mask and can drop the evolve entry.
    """
    from .deploy import _PermCheck  # lazy: deploy imports this module at load

    checks: list[_PermCheck] = []
    perms = _get_perms()
    for entry in BOT_SHARED_SUBDIRS:
        target = entry.path(shared_dir, bot_id)
        if not target.is_dir():
            checks.append(_PermCheck(
                category="bot-shared-subdir", target=str(target), ok=True,
                detail="(not created yet — the bot's next deploy creates it)",
            ))
            continue
        if not _redirect_safe(target):
            checks.append(_PermCheck(
                category="bot-shared-subdir", target=str(target), ok=False,
                detail="path resolves through a non-root symlink or hard link",
                fix_description=(
                    f"inspect {target} by hand (`ls -ld`) — the self-heal must "
                    f"never be the thing that lands a chown or an ACE on an "
                    f"attacker-chosen path. Remove the link, restore the real "
                    f"directory, then re-run `ensure-pod-perms`."
                ),
                apply=None,
            ))
            continue
        problems: list[str] = []
        owner = _owner_of(target)
        if owner is not None and owner != bot_user:
            problems.append(f"owner is {owner}, expected {bot_user}")
        if entry.evolve_read_acl and not perms.acl_user_effective(
            target, _evolve_user(), BOT_SHARED_SUBDIR_READ_ACL_PERMS
        ):
            problems.append("evolve read ACE missing")
        if not problems:
            checks.append(_PermCheck(
                category="bot-shared-subdir", target=str(target), ok=True,
            ))
            continue
        apply = (apply_factory(entry, target) if apply_factory is not None
                 else _repair(entry, target, bot_user))
        checks.append(_PermCheck(
            category="bot-shared-subdir", target=str(target), ok=False,
            detail="; ".join(problems),
            fix_description=(
                f"re-apply the per-bot subdir contract on {target} "
                f"(chown {bot_user}, evolve read ACE). A chown that keeps "
                f"failing means /etc/sudoers.d/evolve predates this subdir — "
                f"run `sudo evolve-admin refresh-sudoers` on the pod host."
            ),
            apply=apply,
        ))
    return checks


def _repair(entry: BotSharedSubdir, target: Path, bot_user: str) -> "Callable[[], bool]":
    def _apply() -> bool:
        profile = _get_profile()
        return ensure_bot_subdir(
            entry, target, bot_user, profile.admin_group, profile.chown,
        )
    return _apply
