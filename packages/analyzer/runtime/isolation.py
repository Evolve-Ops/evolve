"""isolation — the IsolationProvider seam (roadmap 4.3 C, Track I).

Evolve isolates each bot behind a macOS user account today. The dscl /
createhomedir lifecycle ritual was semi-funnelled (``provisioning.py`` plus
three near-copies in ``setup_wizard.py`` / ``wizard.py`` / ``cli.py``) and the
read probes (``dscl . -read /Users/<u> UniqueID``) were scattered across
another half-dozen files. This module formalizes the boundary into an
``IsolationProvider`` interface so the macOS-user coupling becomes a swappable
adapter (``LinuxUserIsolation`` is the useradd/getent adapter — Linux port
L1, docs/design-linux-port-2026-06-10.md §2; tests supply
``FakeIsolation``). Design:
``docs/design-phase4.3c-scheduler-isolation-seams-2026-06-10.md`` (Track I).

Deviations from the design-doc sketch, on purpose:

* **Lifecycle is keyed on the macOS *username*, not bot_id.** The sketch had
  ``create(bot_id)`` / ``delete(bot_id)``, but every real call site operates
  on an account name — which may differ from bot_id (the shared-account
  pattern) and may not be a bot at all (the ``evolve`` service user, the
  ``evo`` gateway user). Keying on bot_id would re-introduce the
  bot_id==account-name bug class this codebase already fought.
* **``resolve()`` / ``home_dir()`` WRAP the blessed helpers** in
  ``evolve_config`` (``get_bot_user`` / ``bot_home``) rather than duplicating
  them — those names are dup-primitive-lint enforced and stay the single
  source of bot→account truth.
* Extra read primitives (``user_exists`` / ``used_uids`` / ``next_free_uid``
  / ``group_members``) and account attributes (``set_password`` /
  ``add_to_group``) exist because the migrated call sites need them; they are
  part of the closed, typed surface a non-macOS adapter must implement.

Canonical argv forms (``MacOSIsolation``): privileged verbs use the dash form
with full binary paths — ``sudo /usr/bin/dscl . -create /Users/<u> …`` /
``-delete`` / ``sudo /usr/sbin/createhomedir -c -u <u>`` — because the
sudoers grants rendered by ``setup_wizard._render_evolve_sudoers()`` match on
exactly those strings (see ``tests/test_provision_pipeline_sudoers.py``).
Unprivileged probes use bare ``dscl`` (on every caller's PATH; no sudo, so
secure_path doesn't apply).

Out of scope here: ``oc_cli``'s internal ``_resolve_user`` / ``_should_sudo``
(the AgentRuntime seam owns that boundary) and the ~170 ad-hoc
``sudo -u <user>`` run-as call sites outside the lifecycle files — ``run_as``
is the primitive they migrate onto in a follow-up.

``get_isolation()`` / ``set_isolation()`` mirror
``runtime.agent_runtime.get_runtime()`` — process-wide singleton with test
injection. Still **behavior-preserving**: every ``MacOSIsolation`` method is
the same subprocess ritual the call sites ran inline before.

Home: this module lives in the analyzer package (beside
``runtime.agent_runtime``) so analyzer-side S2 migrations never import
``evolve_admin`` — admin depends on analyzer, never the reverse.
``evolve_admin.runtime`` re-exports the public surface as a
compatibility shim; the singleton state lives here only.
"""

from __future__ import annotations

import logging
import os
import pwd
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

# Module-level, mirroring runtime.scheduler / runtime.perms (which key their
# get_scheduler / get_perms defaults off the same profile): platform_profile is
# the analyzer-side sibling leaf, so this import stays inside the package and
# never reaches into evolve_admin.
from platform_profile import get_profile

_log = logging.getLogger(__name__)

# Default UID range start for new bot accounts — matches the historical
# behavior of provisioning.DEFAULT_UID_START and setup_wizard._next_free_uid.
DEFAULT_UID_START = 502

# Linux regular users start at 1000 (Ubuntu's UID_MIN) the way macOS
# regular users start at ~502 — LinuxUserIsolation.next_free_uid defaults
# here so bot accounts look like normal users in listings (design §2).
LINUX_DEFAULT_UID_START = 1000

# Every bot account joins this group at creation — the pod's user
# inventory ("which users are bots?" = `getent group evolve-bots`),
# design-linux-port §2. groupadd -f makes membership setup idempotent.
EVOLVE_BOTS_GROUP = "evolve-bots"


class IsolationError(RuntimeError):
    """A user-lifecycle operation failed (non-zero exit or timeout).

    The message carries the failing command head + stderr so callers that
    re-wrap it (e.g. ``ProvisionError``) surface an actionable string.
    """


@dataclass
class Identity:
    """A bot's resolved isolation identity (macOS account today)."""

    bot_id: str
    user: str
    uid: "int | None"
    home: Path


@runtime_checkable
class IsolationProvider(Protocol):
    """Create/inspect/remove per-bot isolation identities.

    Lifecycle + reads are keyed on the platform *username*; ``resolve`` /
    ``home_dir`` translate a logical bot_id to that username via the blessed
    ``evolve_config`` helpers (bot_id ≠ account name).
    """

    # ── lifecycle (platform username) ────────────────────────────────────────
    def create_user(
        self, user: str, uid: int, *,
        real_name: "str | None" = None, create_home: bool = True,
    ) -> None: ...
    def delete_user(self, user: str, *, remove_home: bool = True) -> None: ...
    def set_password(self, user: str, password: str) -> bool: ...
    def add_to_group(self, user: str, group: str) -> bool: ...

    # ── reads ────────────────────────────────────────────────────────────────
    def user_exists(self, user: str) -> bool: ...
    def used_uids(self) -> "set[int]": ...
    def next_free_uid(self, start: int = DEFAULT_UID_START) -> int: ...
    def group_members(self, group: str) -> "list[str]": ...

    # ── bot-identity resolution (wraps blessed evolve_config helpers) ────────
    def resolve(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> "Identity | None": ...
    def home_dir(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> Path: ...

    # ── run-as ───────────────────────────────────────────────────────────────
    def run_as(
        self, user: str, argv: "list[str]", *, cwd: str = "/tmp", **kwargs: Any
    ) -> subprocess.CompletedProcess: ...


_Runner = Callable[..., subprocess.CompletedProcess]

# subprocess.Popen-compatible factory — the LinuxUserIsolation real-execution
# seam (tests inject a fake; production uses subprocess.Popen at call time).
_PopenFactory = Callable[..., Any]


def _kill_process_group(pid: int) -> None:
    """SIGKILL the whole process group led by *pid* (best-effort).

    Best-effort on purpose: ``ProcessLookupError`` means the group is
    already gone (every member exited between the timeout and the kill);
    ``PermissionError`` means no surviving member is signalable by this
    euid (e.g. only sudo's root-owned child remains and the caller is the
    unprivileged service user — killpg then degrades to exactly today's
    behavior). In both cases the caller still reaps the direct child and
    re-raises, so neither error may mask the timeout itself.
    """
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as e:
        # Not masking the timeout (the caller still reaps + re-raises); a
        # debug line keeps this off the silent-swallow ledger and records
        # the degrade-to-today's-behavior case (unprivileged caller vs a
        # root-owned sudo child) for the rare post-mortem.
        _log.debug("pgroup kill for pid %s is a no-op (%s)", pid, e)


def _run_in_own_pgroup(
    cmd: "list[str]",
    *,
    popen_factory: _PopenFactory,
    kill_pgroup: "Callable[[int], None]",
    input: "str | bytes | None" = None,  # noqa: A002 — subprocess.run parity
    capture_output: bool = False,
    timeout: "float | None" = None,
    check: bool = False,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess:
    """``subprocess.run``, except the child leads its OWN process group and
    a timeout SIGKILLs the WHOLE group.

    Why: ``subprocess.run(timeout=...)`` kills only the direct child, and
    ``TimeoutExpired`` doesn't expose the Popen, so a group kill can't be
    bolted on after the fact. Every privileged lifecycle command in
    ``LinuxUserIsolation`` is a ``sudo`` wrapper — observed in PR #2694's
    linux-e2e bring-up: the timeout killed sudo while its child (useradd)
    survived and completed in the background, leaving half-tracked account
    state. ``start_new_session=True`` puts sudo + descendants in a fresh
    process group; on timeout we SIGKILL that group, reap the wrapper, and
    re-raise ``TimeoutExpired`` so call sites keep their existing handling
    (``create_user`` converts it to ``IsolationError``; ``delete_user`` /
    ``run_as`` let it propagate, as before).

    Linux-only on purpose: ``MacOSIsolation`` keeps plain
    ``subprocess.run`` — no orphaning incident has been observed on macOS
    and its deploy-path risk stays zero.

    ``popen_factory`` / ``kill_pgroup`` are injection seams: tests fake
    privileged commands through them (or through the higher-level
    ``runner``) and must never patch ``subprocess`` attributes.
    """
    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
    proc = popen_factory(cmd, start_new_session=True, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_pgroup(proc.pid)
        # Mirror subprocess.run's own timeout cleanup: direct-child kill
        # (a PermissionError here propagates rather than hanging the reap
        # below — same contract as subprocess.run today) + reap so no
        # zombie outlives the raise.
        proc.kill()
        proc.wait()
        raise
    completed = subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, list(cmd), output=stdout, stderr=stderr
        )
    return completed


class MacOSIsolation:
    """The default adapter — dscl / createhomedir, ritual-for-ritual.

    ``runner`` is an injection point in the spirit of
    ``metrics.resolvers.users.set_account_checker``: pass a callable with
    ``subprocess.run``'s signature to fake every platform invocation. When
    unset, calls go through ``subprocess.run`` looked up at call time, so
    tests that monkeypatch ``subprocess.run`` keep intercepting.
    """

    def __init__(self, runner: "_Runner | None" = None) -> None:
        self._runner = runner

    def _run(self, cmd: "list[str]", **kwargs: Any) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(cmd, **kwargs)
        return subprocess.run(cmd, **kwargs)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def create_user(
        self, user: str, uid: int, *,
        real_name: "str | None" = None, create_home: bool = True,
    ) -> None:
        """Run the dscl (+ createhomedir) ritual to provision a macOS user.

        Raises ``IsolationError`` on the first failing step. Caller is
        responsible for not invoking this when the user already exists
        (check ``user_exists`` first — dscl -create is not idempotent on
        attributes like UniqueID).

        Full paths are load-bearing: sudo's secure_path on production pods
        omits ``/usr/sbin`` (bare ``createhomedir`` 404s under sudo), and the
        sudoers grants for the ``evolve`` user match the full-path dash-form
        command strings exactly.
        """
        cmds = [
            ["sudo", "/usr/bin/dscl", ".", "-create", f"/Users/{user}"],
            ["sudo", "/usr/bin/dscl", ".", "-create", f"/Users/{user}", "UserShell", "/bin/zsh"],
            ["sudo", "/usr/bin/dscl", ".", "-create", f"/Users/{user}", "RealName", real_name or user],
            ["sudo", "/usr/bin/dscl", ".", "-create", f"/Users/{user}", "UniqueID", str(uid)],
            ["sudo", "/usr/bin/dscl", ".", "-create", f"/Users/{user}", "PrimaryGroupID", "20"],
            ["sudo", "/usr/bin/dscl", ".", "-create", f"/Users/{user}", "NFSHomeDirectory", f"/Users/{user}"],
        ]
        if create_home:
            cmds.append(["sudo", "/usr/sbin/createhomedir", "-c", "-u", user])
        for cmd in cmds:
            try:
                proc = self._run(cmd, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise IsolationError(f"dscl timeout: {' '.join(cmd[1:4])}") from exc
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                raise IsolationError(f"{' '.join(cmd[1:4])} failed: {stderr}")

    def delete_user(self, user: str, *, remove_home: bool = True) -> None:
        """Delete a macOS user record (+ optionally its home directory).

        Best-effort: a failing dscl-delete (e.g. user already gone) is not an
        error — rollback/retire callers must be able to run this on a
        half-created account. ``remove_home=False`` preserves ``/Users/<user>``
        (the shared-account case where we didn't create the home).
        """
        self._run(
            ["sudo", "/usr/bin/dscl", ".", "-delete", f"/Users/{user}"],
            capture_output=True, timeout=30,
        )
        if remove_home:
            home = Path(f"/Users/{user}")
            # Guard rail: never `rm -rf /Users/` or anything shorter than
            # `/Users/<3+chars>`. dscl wouldn't accept such a name in
            # create_user, but a defense-in-depth check is cheap.
            if len(str(home)) > len("/Users/") + 2 and home.parent == Path("/Users"):
                self._run(
                    ["sudo", "/bin/rm", "-rf", str(home)],
                    capture_output=True, timeout=60,
                )

    def set_password(self, user: str, password: str) -> bool:
        proc = self._run(
            ["sudo", "/usr/bin/dscl", ".", "-passwd", f"/Users/{user}", password],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0

    def add_to_group(self, user: str, group: str) -> bool:
        proc = self._run(
            ["sudo", "/usr/bin/dscl", ".", "-append", f"/Groups/{group}", "GroupMembership", user],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0

    # ── reads ────────────────────────────────────────────────────────────────
    def user_exists(self, user: str) -> bool:
        """True if `dscl . -read /Users/<user> UniqueID` succeeds."""
        try:
            proc = self._run(
                ["dscl", ".", "-read", f"/Users/{user}", "UniqueID"],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def used_uids(self) -> "set[int]":
        """All UIDs currently registered in DirectoryService."""
        proc = self._run(
            ["dscl", ".", "-list", "/Users", "UniqueID"],
            capture_output=True, text=True,
        )
        used: "set[int]" = set()
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                used.add(int(parts[1]))
            except ValueError:
                continue  # non-numeric UniqueID record — skip it
        return used

    def next_free_uid(self, start: int = DEFAULT_UID_START) -> int:
        used = self.used_uids()
        uid = start
        while uid in used:
            uid += 1
        return uid

    def group_members(self, group: str) -> "list[str]":
        """Member names of a DirectoryService group ([] on any failure)."""
        try:
            proc = self._run(
                ["dscl", ".", "-read", f"/Groups/{group}", "GroupMembership"],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        members_line = next(
            (line for line in (proc.stdout or "").splitlines()
             if line.startswith("GroupMembership")),
            "",
        )
        return members_line.replace("GroupMembership:", "").split()

    # ── bot-identity resolution ──────────────────────────────────────────────
    def resolve(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> "Identity | None":
        """bot_id → Identity via the blessed resolver; None if no account.

        ``network`` overrides the network.json dict (callers that already
        loaded it); when None the blessed helper loads the canonical config.
        """
        from evolve_config import get_bot_user  # blessed home (dup-primitive-lint)

        user = get_bot_user(bot_id, network)
        import pwd
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return None
        return Identity(bot_id=bot_id, user=user, uid=pw.pw_uid, home=Path(pw.pw_dir))

    def home_dir(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> Path:
        from evolve_config import bot_home  # blessed home (dup-primitive-lint)

        return bot_home(bot_id, network)

    # ── run-as ───────────────────────────────────────────────────────────────
    def run_as(
        self, user: str, argv: "list[str]", *, cwd: str = "/tmp", **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """Run ``argv`` as ``user`` via ``sudo -u <user> -H``.

        ``cwd`` defaults to ``/tmp`` because the invoking account's home is
        usually untraversable by the target user — Node binaries call
        ``uv_cwd()`` at startup and die with EACCES (see CLAUDE.md §SSH).
        ``-H`` makes sudo set HOME from the target user's passwd entry without
        an ``env`` wrapper (which would break sudoers command matching).
        """
        return self._run(["sudo", "-u", user, "-H", *argv], cwd=cwd, **kwargs)


class LinuxUserIsolation:
    """The Linux adapter — per-bot Unix users via useradd / userdel / getent
    (Linux port L1, docs/design-linux-port-2026-06-10.md §2). A direct
    translation of the macOS per-bot-user model: same accounts, same
    sudoers shape, same threat-model tier table — containers stay a
    possible future second adapter behind this same interface.

    Verb map (full paths per Ubuntu 24.04 /usr-merge; the sudoers grants a
    Linux wizard renders must match these argv shapes exactly):

    - ``create_user`` → ``groupadd -f evolve-bots`` (idempotent inventory
      group, design §2) + ``useradd -m -u <uid> -s /bin/bash -c <real_name>
      -G evolve-bots <user>`` (``create_home=False`` → ``-M``; shell is a
      real bash because ``run_as`` and OpenClaw exec need a working shell,
      never ``nologin``) + a final ``chown <user> /home/<user>`` so the
      account owns its $HOME even when the dir pre-existed root-owned
      (``useradd -m`` only owns a home it creates; W10-F #11)
    - ``delete_user`` → ``userdel -r`` (``remove_home=False`` → ``userdel``;
      no separate ``rm -rf`` step — ``-r`` owns home removal)
    - ``set_password`` → ``chpasswd`` fed ``user:password`` on **stdin**
      (the password must NEVER appear in argv — /proc exposes argv to
      every local process; macOS's ``dscl -passwd`` shape does not carry over)
    - ``add_to_group`` → ``usermod -aG <group> <user>``
    - reads → unprivileged ``getent passwd`` / ``getent group`` (bare name,
      no sudo, so secure_path doesn't apply — same call shape as the bare
      ``dscl`` probes)
    - ``resolve`` / ``home_dir`` / ``run_as`` → identical to macOS: the
      blessed ``evolve_config`` helpers (``pwd.getpwnam`` is POSIX-portable)
      and ``sudo -H -u <user>`` with ``cwd=/tmp``.

    UID range: regular users start at 1000 on Linux (`LINUX_DEFAULT_UID_START`),
    matching the platform's regular-user convention the way 502 matches
    macOS's — ``next_free_uid()`` defaults there.

    Timeout semantics (Linux-only, see :func:`_run_in_own_pgroup`): real
    executions run in their own process group and a timeout SIGKILLs the
    whole group, so a hung ``sudo useradd`` can't orphan a root child that
    completes in the background (PR #2694 linux-e2e incident).

    ``runner`` is the same injection seam as :class:`MacOSIsolation` and
    takes absolute precedence — when set, the popen/pgroup machinery never
    engages. ``popen_factory`` / ``kill_pgroup`` are the lower-level seams
    for testing the real-execution path itself; both default to the real
    primitives looked up at call time.
    """

    def __init__(
        self,
        runner: "_Runner | None" = None,
        *,
        popen_factory: "_PopenFactory | None" = None,
        kill_pgroup: "Callable[[int], None] | None" = None,
    ) -> None:
        self._runner = runner
        self._popen_factory = popen_factory
        self._kill_pgroup = kill_pgroup

    def _run(self, cmd: "list[str]", **kwargs: Any) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(cmd, **kwargs)
        return _run_in_own_pgroup(
            cmd,
            # subprocess.Popen is looked up at call time so test guards
            # that monkeypatch it still intercept any unfaked spawn.
            popen_factory=self._popen_factory or subprocess.Popen,
            kill_pgroup=self._kill_pgroup or _kill_process_group,
            **kwargs,
        )

    # ── lifecycle ────────────────────────────────────────────────────────────
    def create_user(
        self, user: str, uid: int, *,
        real_name: "str | None" = None, create_home: bool = True,
    ) -> None:
        """Provision a Linux user (+ membership in the ``evolve-bots``
        inventory group). Raises ``IsolationError`` on the first failing
        step. Caller is responsible for not invoking this when the user
        already exists (check ``user_exists`` first — useradd is not
        idempotent)."""
        cmds = [
            ["sudo", "/usr/sbin/groupadd", "-f", EVOLVE_BOTS_GROUP],
            [
                "sudo", "/usr/sbin/useradd",
                "-m" if create_home else "-M",
                "-u", str(uid),
                "-s", "/bin/bash",
                "-c", real_name or user,
                "-G", EVOLVE_BOTS_GROUP,
                user,
            ],
        ]
        for cmd in cmds:
            try:
                proc = self._run(cmd, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise IsolationError(f"timeout: {' '.join(cmd[1:4])}") from exc
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                raise IsolationError(f"{' '.join(cmd[1:4])} failed: {stderr}")

        # W10-F #11 (round-4): own the home dir to the account. ``useradd -m``
        # sets ownership only on a home it CREATES; if /home/<user> already
        # exists (an earlier root-context ``sudo mkdir -p /home/<user>/.openclaw``
        # — the wizard's _write_bot_files, setup_wizard's evolve-tree
        # provisioning — made it before this call), useradd leaves it ``root:root``
        # and the account cannot write its own $HOME: npm cache (.npm), dotfiles,
        # and the brave (externalized) plugin gap-fill install all hit EACCES.
        # That broke ``darwin`` live (round-4: /home/darwin was drwxr-xr-x root
        # root). Non-recursive — chown ONLY the home dir, never the carefully
        # chowned/ACL'd .openclaw subtree underneath. pwd-first (the account
        # exists now) — never path-math the home root. macOS has no analogue
        # (createhomedir always owns the home to the user), so this lives in the
        # Linux adapter alone, keeping the macOS path byte-identical.
        if create_home:
            try:
                home = pwd.getpwnam(user).pw_dir
            except KeyError:
                home = ""
            if home and os.path.isdir(home):
                proc = self._run(
                    ["sudo", "/usr/bin/chown", user, home],
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode != 0:
                    stderr = (proc.stderr or "").strip()
                    raise IsolationError(f"chown {user} {home} failed: {stderr}")

    def delete_user(self, user: str, *, remove_home: bool = True) -> None:
        """Delete a Linux user record (+ optionally its home directory).

        Best-effort like the macOS adapter: a failing userdel (e.g. user
        already gone) is not an error — rollback/retire callers must be
        able to run this on a half-created account. ``remove_home=False``
        preserves the home (the shared-account case)."""
        argv = ["sudo", "/usr/sbin/userdel"]
        if remove_home:
            argv.append("-r")
        argv.append(user)
        self._run(argv, capture_output=True, timeout=60)

    def set_password(self, user: str, password: str) -> bool:
        """``chpasswd`` reading ``user:password`` from stdin — the password
        never enters argv (visible pod-wide via /proc on Linux)."""
        proc = self._run(
            ["sudo", "/usr/sbin/chpasswd"],
            input=f"{user}:{password}\n",
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0

    def add_to_group(self, user: str, group: str) -> bool:
        proc = self._run(
            ["sudo", "/usr/sbin/usermod", "-aG", group, user],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0

    # ── reads ────────────────────────────────────────────────────────────────
    def user_exists(self, user: str) -> bool:
        """True if ``getent passwd <user>`` succeeds."""
        try:
            proc = self._run(
                ["getent", "passwd", user],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def used_uids(self) -> "set[int]":
        """All UIDs currently known to NSS (``getent passwd``)."""
        proc = self._run(
            ["getent", "passwd"],
            capture_output=True, text=True,
        )
        used: "set[int]" = set()
        for line in (proc.stdout or "").splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            try:
                used.add(int(parts[2]))
            except ValueError:
                continue  # malformed passwd row — skip it
        return used

    def next_free_uid(self, start: int = LINUX_DEFAULT_UID_START) -> int:
        used = self.used_uids()
        uid = start
        while uid in used:
            uid += 1
        return uid

    def group_members(self, group: str) -> "list[str]":
        """Member names of a group ([] on any failure). Supplementary
        members only — exactly what ``usermod -aG`` / ``useradd -G`` set."""
        try:
            proc = self._run(
                ["getent", "group", group],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        rows = str(proc.stdout or "").strip().splitlines()
        parts = rows[0].split(":") if rows else []
        if len(parts) < 4 or not parts[3]:
            return []
        return parts[3].split(",")

    # ── bot-identity resolution (identical to macOS — already POSIX) ─────────
    def resolve(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> "Identity | None":
        """bot_id → Identity via the blessed resolver; None if no account."""
        from evolve_config import get_bot_user  # blessed home (dup-primitive-lint)

        user = get_bot_user(bot_id, network)
        import pwd
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return None
        return Identity(bot_id=bot_id, user=user, uid=pw.pw_uid, home=Path(pw.pw_dir))

    def home_dir(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> Path:
        from evolve_config import bot_home  # blessed home (dup-primitive-lint)

        return bot_home(bot_id, network)

    # ── run-as ───────────────────────────────────────────────────────────────
    def run_as(
        self, user: str, argv: "list[str]", *, cwd: str = "/tmp", **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """Run ``argv`` as ``user`` via ``sudo -u <user> -H`` from ``/tmp``
        — same invocation shape as macOS (the uv_cwd()/EACCES rationale in
        ``MacOSIsolation.run_as`` applies identically on Linux)."""
        return self._run(["sudo", "-u", user, "-H", *argv], cwd=cwd, **kwargs)


class FakeIsolation:
    """In-memory adapter for tests — user lifecycle with no dscl/macOS.

    ``users`` maps username → uid. Seed accounts via ``seed_user``; map a
    bot_id to a different account via ``seed_bot_user`` (the shared-account
    pattern). Mutating calls record into ``self.calls``.
    """

    def __init__(self) -> None:
        self.users: "dict[str, int]" = {}
        self.homes: "dict[str, Path]" = {}
        self.group_table: "dict[str, list[str]]" = {}
        self.passwords: "dict[str, str]" = {}
        self.bot_user_map: "dict[str, str]" = {}
        self.calls: "list[tuple]" = []

    def seed_user(self, user: str, uid: int = DEFAULT_UID_START, home: "str | Path | None" = None) -> None:
        self.users[user] = uid
        self.homes[user] = Path(home) if home else Path(f"/Users/{user}")

    def seed_bot_user(self, bot_id: str, user: str) -> None:
        self.bot_user_map[bot_id] = user

    # ── lifecycle ────────────────────────────────────────────────────────────
    def create_user(
        self, user: str, uid: int, *,
        real_name: "str | None" = None, create_home: bool = True,
    ) -> None:
        self.calls.append(("create_user", user, uid))
        if user in self.users:
            raise IsolationError(
                f"/usr/bin/dscl . -create failed: user {user!r} already exists"
            )
        self.users[user] = uid
        if create_home:
            self.homes[user] = Path(f"/Users/{user}")

    def delete_user(self, user: str, *, remove_home: bool = True) -> None:
        self.calls.append(("delete_user", user, remove_home))
        self.users.pop(user, None)
        if remove_home:
            self.homes.pop(user, None)

    def set_password(self, user: str, password: str) -> bool:
        self.calls.append(("set_password", user))
        if user not in self.users:
            return False
        self.passwords[user] = password
        return True

    def add_to_group(self, user: str, group: str) -> bool:
        self.calls.append(("add_to_group", user, group))
        members = self.group_table.setdefault(group, [])
        if user not in members:
            members.append(user)
        return True

    # ── reads ────────────────────────────────────────────────────────────────
    def user_exists(self, user: str) -> bool:
        return user in self.users

    def used_uids(self) -> "set[int]":
        return set(self.users.values())

    def next_free_uid(self, start: int = DEFAULT_UID_START) -> int:
        used = self.used_uids()
        uid = start
        while uid in used:
            uid += 1
        return uid

    def group_members(self, group: str) -> "list[str]":
        return list(self.group_table.get(group, []))

    # ── bot-identity resolution ──────────────────────────────────────────────
    def _user_for(self, bot_id: str, network: "dict[str, Any] | None") -> str:
        if network is not None:
            return (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
        return self.bot_user_map.get(bot_id, bot_id)

    def resolve(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> "Identity | None":
        user = self._user_for(bot_id, network)
        if user not in self.users:
            return None
        return Identity(
            bot_id=bot_id, user=user, uid=self.users[user],
            home=self.homes.get(user, Path(f"/Users/{user}")),
        )

    def home_dir(
        self, bot_id: str, network: "dict[str, Any] | None" = None
    ) -> Path:
        user = self._user_for(bot_id, network)
        return self.homes.get(user, Path(f"/Users/{user}"))

    # ── run-as ───────────────────────────────────────────────────────────────
    def run_as(
        self, user: str, argv: "list[str]", *, cwd: str = "/tmp", **kwargs: Any
    ) -> subprocess.CompletedProcess:
        self.calls.append(("run_as", user, list(argv)))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


# ── factory ───────────────────────────────────────────────────────────────────

_isolation: "IsolationProvider | None" = None


def get_isolation() -> IsolationProvider:
    """Return the process-wide isolation adapter.

    The default is keyed off the active platform profile (macOS → dscl,
    Linux → useradd/getent) so a pinned profile — or plain ``sys.platform``
    autodetection on a real pod — selects the matching adapter, the same
    shape as :func:`runtime.scheduler.get_scheduler` and
    :func:`runtime.perms.get_perms`.

    Before this keying, the default was an unconditional ``MacOSIsolation()``.
    ``set_isolation(LinuxUserIsolation())`` is only called from the *install*
    process (the wizard's Linux platform gate), so the long-running admin-ui
    daemon — which never runs that gate — got the macOS backend on every COLD
    ``get_isolation()`` caller (health.py, installer.py, wizard.py, retire.py,
    provisioning.py). On a Linux pod that made ``user_exists`` shell ``dscl``,
    which isn't on PATH, so accounts that DO exist (evolve/darwin/evo) were
    reported missing — the false "macOS user not found" Pod Health failures.
    Keying the default off the profile repairs every cold caller at once.

    A :func:`set_isolation` injection still wins over the default (tests inject
    ``FakeIsolation``; the wizard pins ``LinuxUserIsolation`` explicitly)."""
    global _isolation
    if _isolation is None:
        _isolation = (
            LinuxUserIsolation() if get_profile().name == "linux"
            else MacOSIsolation()
        )
    return _isolation


def set_isolation(provider: "IsolationProvider | None") -> None:
    """Swap the adapter (tests inject FakeIsolation; a future port injects
    another). Pass ``None`` to reset to the default on next ``get_isolation()``."""
    global _isolation
    _isolation = provider
