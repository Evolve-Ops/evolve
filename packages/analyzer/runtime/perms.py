"""perms — the two-backend ACL seam (Linux port W4a).

Design: docs/design-linux-port-2026-06-10.md §4. Evolve's read/write
contracts between the ``evolve`` service user, the ``evo`` gateway user,
and per-bot accounts are expressed as filesystem ACLs. macOS spells them
``chmod +a/-N``; Linux spells them ``setfacl``/``getfacl``. This module is
the seam: ``deploy.py``'s ACL entry points (``set_evolve_read_acl``,
``ensure_pod_perms``, ``fix_shared_dir_permissions``,
``_ensure_evo_write_acl``, ``_ensure_evolve_owned_dir_perms``, plus their
check/apply helpers) call :func:`get_perms` instead of open-coding argv.

**The macOS ACE strings are byte-exact contract.** Sudoers grants match
on the ``chmod +a <ace> <path>`` shapes (``_render_evolve_sudoers``), and
``chmod +a`` idempotence (duplicate-ACE → "exists") depends on the ACE
re-rendering identically on every pass. :class:`MacOSPerms` therefore
renders ``f"{user} allow {perms}"`` (or ``f"user:{user} allow {perms}"``
where the historical call site used the prefixed form) with the *exact*
perm-verb strings the call sites pass — never normalize, reorder, or
deduplicate them. Golden-pinned in ``test_perms_seam.py`` and
``test_deploy_perms_seam.py``.

**The POSIX ACL mask sharp edge** (design §4, "read this one"): on
Linux, once a path has a named-user ACE the group mode bits *become the
ACL mask*, and the mask caps every named entry's effective permissions.
Consequences this module absorbs so call sites stay ignorant:

- ``chmod`` that touches group bits silently disables the ACL — mode-
  change sites call :meth:`Perms.reassert_mask` afterwards (macOS no-op;
  Linux ``setfacl -m m::rwX``, guarded to no-op on paths without an
  extended ACL so it never *creates* one).
- presence checks must verify **effective** perms, not ACE presence —
  :meth:`Perms.acl_user_effective` parses ``getfacl``'s ``#effective:``
  annotations on Linux (ACE ∩ mask) and ``ls -lde`` on macOS as today.
- mode-assertion code lies on ACL'd files (the group triad displays the
  mask) — :meth:`Perms.effective_mode` substitutes the real ``group::``
  entry's effective bits on Linux; plain ``stat`` on macOS.

**Negative carve-outs are load-bearing**: ``credentials/`` and per-user
profile ``.md`` files are stripped of ACLs (``chmod -N`` / ``setfacl -b``
+ ``-k``) and tightened to 700/600 — that is what keeps the ``evolve``
service user out of bot API keys (threat model §3.1).
:meth:`Perms.clear_acl` is the carve-out primitive.

**Default-ACL inheritance rule** (Linux): POSIX default ACLs apply at
*creation time* in the directory — a file renamed in from elsewhere
keeps its source ACL. Writers must create-in-place or copy, never rename
across directory boundaries into an ACL'd tree (our stores already
mkstemp in the destination dir before ``os.replace``).

Linux full paths: ``/usr/bin/setfacl`` / ``/usr/bin/getfacl`` (Ubuntu
24.04 merged-/usr). The W4 sudoers writer must grant exactly these argv
shapes for the ``evolve`` user, the way the macOS grants match the
``/bin/chmod +a`` forms today.

``get_perms()`` / ``set_perms()`` mirror ``runtime.scheduler`` /
``runtime.isolation`` — process-wide adapter with test injection; the
default is keyed off :func:`platform_profile.get_profile` so a pinned
profile (tests, the wizard's platform gate) selects the matching backend.

Home: analyzer-side like the sibling seams (admin depends on analyzer,
never the reverse); ``evolve_admin.runtime`` re-exports the surface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from platform_profile import LINUX as _LINUX_PROFILE
from platform_profile import get_profile

# The pod-wide read contract for `.openclaw/` directories (macOS ACL verb
# vocabulary — the seam's canonical perm language; Linux backends derive
# POSIX bits from it). `evolve_admin.deploy.POD_ACL_PERMS` aliases this.
POD_READ_ACL_PERMS = "list,search,readattr,readextattr,readsecurity"

# Inherit flags appended to the read contract by grant_read_recursive —
# new files/dirs under the granted tree pick up the ACE automatically.
_INHERIT_FLAGS = "file_inherit,directory_inherit"

# Linux command locations (Ubuntu 24.04, merged /usr) — sourced from the
# platform-profile command table so this adapter's argv and the W4b sudoers
# grants (_render_evolve_sudoers) cannot drift apart (design §5: one writer,
# one table). Deliberately NOT bare names: sudoers matching and secure_path
# both require full paths, same discipline as the macOS table in CLAUDE.md.
assert _LINUX_PROFILE.setfacl is not None and _LINUX_PROFILE.getfacl is not None
SETFACL: str = _LINUX_PROFILE.setfacl
GETFACL: str = _LINUX_PROFILE.getfacl

# The named POSIX-ACL principals Evolve itself grants on a bot's ``.openclaw``
# tree — the ``evolve`` service user (read ACL, ``set_evolve_read_acl``), the
# ``evo`` gateway user (proposal/signal write ACL), and ``root`` (the read ACL
# grant plants ``user:root:r-x`` alongside ``user:evolve``; visible on a live
# pod as ``[default:]user:root:r-x``). These are the ONLY named ACEs Evolve
# plants; ``LinuxPerms.acl_masked_owner_only`` treats a named ACE for any OTHER
# principal as a real, non-Evolve grant (so an OpenClaw group/other-readable
# finding stays honest). A named ACE here being capped by the mask is exactly
# the false-positive the suppression exists to absorb.
#
# ``root`` MUST be here: it is inherently privileged (it bypasses POSIX perms
# entirely), so a ``user:root`` ACE is never an exposure — and omitting it made
# ``acl_masked_owner_only`` return False for EVERY clamped ``.openclaw`` file
# (they all carry ``user:root:r-x``), silently defeating the whole ACL-mask
# suppression on Linux so the readable/writable false positives fired every
# audit. See ``acl_masked_owner_only`` + audit.py's ``_FLAP_OR_MASK`` list.
EVOLVE_ACL_PRINCIPALS: frozenset[str] = frozenset({"evolve", "evo", "root"})

# macOS ACL verbs that imply POSIX write access. Used by the Linux
# backend to collapse a verb set to `rX` vs `rwX` (design §4: "verb
# collapse is honest" — the fine-grained macOS verbs are all facets of
# read+traverse or write).
_WRITE_VERBS = frozenset({
    "write", "add_file", "add_subdirectory", "delete", "append",
    "writeattr", "writeextattr", "chown",
})

# When `chmod +a "user:x allow <perms>" <dir>` runs on a directory, the
# macOS kernel translates file-form perm names into their directory
# equivalents before storing them; `ls -lde` then prints the resolved
# names. A literal-set check against the source names would miss (e.g.
# asking for `read` on a dir sees `list` and reports drift). Entries not
# in this map (delete, the *attr / *security names, chown, the inherit
# flags) are stored as-is.
_MACOS_DIR_ACL_PERM_MAP: dict[str, tuple[str, ...]] = {
    "read":    ("list",),
    "write":   ("add_file", "add_subdirectory"),
    "execute": ("search",),
    "append":  ("add_subdirectory",),
}


def _split_perms(perms: str) -> set[str]:
    return {p.strip() for p in perms.split(",") if p.strip()}


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` that treats unreachable as not-a-directory.

    ``is_dir()`` propagates permission errors (only ENOENT/ENOTDIR map
    to False); for ACL decisions an unreachable path gets the
    conservative file-shaped treatment — the dir-only extras (default
    ACLs, ``-k``) are skipped rather than crashing the grant."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _linux_entry_bits(perms: str) -> str:
    """Collapse a macOS verb set to the POSIX entry spec (design §4).

    `rX` for read/traverse grants, `rwX` once any write-ish verb appears.
    Capital X = execute only on directories (or already-executable files),
    so recursive application never makes plain files executable.
    """
    return "rwX" if (_split_perms(perms) & _WRITE_VERBS) else "rX"


def _linux_needed_bits(required: str) -> set[str]:
    """macOS verb set → the POSIX bits an effective-perm check must see."""
    verbs = _split_perms(required)
    needed: set[str] = set()
    if verbs & {"read", "list"}:
        needed.add("r")
    if verbs & _WRITE_VERBS:
        needed.add("w")
    if verbs & {"search", "execute"}:
        needed.add("x")
    return needed


@runtime_checkable
class Perms(Protocol):
    """Grant/strip/inspect filesystem ACLs, platform-neutrally.

    ``perms`` arguments use the macOS ACL verb vocabulary (the canonical
    perm language of this seam); the Linux backend collapses them to
    POSIX ``r``/``w``/``X`` bits. ``prefixed`` is a macOS ACE-rendering
    compatibility knob: call sites that historically emitted
    ``"user:<name> allow …"`` keep that byte shape (idempotence dedup +
    sudoers matching); Linux ignores it.

    ``restrict_group_other`` / ``share_group_other_read`` are the two
    Linux-only knobs that govern the *real* ``group::``/``other::`` base
    entries of a granted tree (macOS extended ACLs have no such base-entry
    inheritance and ignore both):

    - ``grant_read_recursive(..., restrict_group_other=True)`` is the
      bot-private ``.openclaw`` contract — clamp ``group::``/``other::`` to
      nothing so new children AND the existing tree are owner + named-evolve
      only (no genuine group/world read), with the mask pinned at ``rX`` so
      evolve's named entry stays effective.
    - ``grant_write_recursive(..., share_group_other_read=True)`` is the
      ``workspace/`` shared-channel exception — re-assert ``group::r-x`` /
      ``other::r-x`` so the BOT can still read evolve-written files it does
      not own and has no named ACE on (manifests, the defer/audit queues).
    """

    # ── grants ───────────────────────────────────────────────────────────────
    def grant_read_recursive(
        self, path: Path, user: str, *, restrict_group_other: bool = False
    ) -> bool: ...
    def grant_write_recursive(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False,
        share_group_other_read: bool = False,
    ) -> bool: ...
    def grant(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False
    ) -> bool: ...
    def grant_traverse(self, path: Path, user: str) -> bool: ...

    # ── carve-out ────────────────────────────────────────────────────────────
    def clear_acl(self, path: Path, *, recursive: bool = False) -> bool: ...

    # ── checks ───────────────────────────────────────────────────────────────
    def acl_user_effective(self, path: Path, user: str, required: str) -> bool: ...
    def acl_group_effective(self, path: Path, group: str, required: str) -> bool: ...
    def effective_mode(self, path: Path) -> int: ...
    def acl_masked_owner_only(self, path: Path) -> bool: ...

    # ── mask repair (Linux sharp edge; macOS no-op) ──────────────────────────
    def reassert_mask(self, path: Path, *, recursive: bool = False) -> bool: ...


_Runner = Callable[..., subprocess.CompletedProcess]


class MacOSPerms:
    """The default adapter — today's ``chmod +a/-N`` rituals, byte-exact.

    ``runner`` is the same injection seam as ``MacOSIsolation``: a
    callable with ``subprocess.run``'s signature that sees every argv.
    When unset, calls go through ``subprocess.run`` at call time.
    """

    def __init__(self, runner: "_Runner | None" = None) -> None:
        self._runner = runner

    def _run(self, cmd: "list[str]", **kwargs: Any) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(cmd, **kwargs)
        return subprocess.run(cmd, **kwargs)

    @staticmethod
    def _ace(user: str, perms: str, prefixed: bool) -> str:
        return f"user:{user} allow {perms}" if prefixed else f"{user} allow {perms}"

    def _chmod_plus_a(self, ace: str, path: Path, *, recursive: bool, timeout: int) -> bool:
        """One ``chmod +a`` (or ``-R +a``). ``rc==1`` + "exists" in stderr
        is macOS's duplicate-ACE response — already correct, treat as
        success (the `_add_acl` semantics, now uniform)."""
        argv = ["sudo", "/bin/chmod"] + (["-R"] if recursive else []) + ["+a", ace, str(path)]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return False
        if proc.returncode == 0:
            return True
        return "exists" in (proc.stderr or "").lower()

    # ── grants ───────────────────────────────────────────────────────────────
    def grant_read_recursive(
        self, path: Path, user: str, *, restrict_group_other: bool = False
    ) -> bool:
        """The `.openclaw/` read contract: inheritable ACE on the dir,
        recursive backfill onto the existing tree. Idempotent.

        ``restrict_group_other`` is a Linux POSIX-ACL concern (clamp the real
        ``group::``/``other::`` base entries that the default ACL would
        otherwise mint as ``r-x``). macOS extended ACLs carry no such base-
        entry inheritance, the POSIX mode is the file's own (secrets are set
        0600 by ``chmod_secret_config``), and the byte-exact ``+a`` golden must
        not change — so the flag is accepted and ignored here."""
        ace = self._ace(user, f"{POD_READ_ACL_PERMS},{_INHERIT_FLAGS}", prefixed=False)
        ok_root = self._chmod_plus_a(ace, path, recursive=False, timeout=10)
        self._chmod_plus_a(ace, path, recursive=True, timeout=30)  # best-effort backfill
        return ok_root

    def grant_write_recursive(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False,
        share_group_other_read: bool = False,
    ) -> bool:
        # share_group_other_read is the Linux workspace-channel knob; macOS has
        # no POSIX group/other base-entry inheritance and the +a golden is byte
        # contract — accept and ignore (same rationale as restrict_group_other).
        ace = self._ace(user, perms, prefixed)
        ok_root = self._chmod_plus_a(ace, path, recursive=False, timeout=10)
        self._chmod_plus_a(ace, path, recursive=True, timeout=30)  # best-effort backfill
        return ok_root

    def grant(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False
    ) -> bool:
        return self._chmod_plus_a(self._ace(user, perms, prefixed), path,
                                  recursive=False, timeout=10)

    def grant_traverse(self, path: Path, user: str) -> bool:
        """No-op on macOS: ``/Users/<account>`` is created mode 0755 (the
        ``+`` ACL aside, the POSIX bits give *other* ``r-x``), so the
        ``evolve`` service user can already traverse a bot's home to reach
        ``.openclaw``. Adding a ``chmod +a`` ACE here would change the
        macOS golden (and need a new sudoers grant) for zero benefit — the
        Linux backend is the one where home is 0750 and the traverse ACE
        is load-bearing. Returns True (the contract is already satisfied)."""
        return True

    # ── carve-out ────────────────────────────────────────────────────────────
    def clear_acl(self, path: Path, *, recursive: bool = False) -> bool:
        """``chmod -N`` (or ``-R -N``) — strip ACLs. ``recursive`` is the
        build-plugin dist-restore shape (``chmod -R -N dist/``); the longer
        timeout matches its historical call site."""
        argv = ["sudo", "/bin/chmod"] + (["-R"] if recursive else []) + ["-N", str(path)]
        try:
            proc = self._run(argv, capture_output=True, text=True,
                             timeout=30 if recursive else 10)
        except Exception:
            return False
        return proc.returncode == 0

    # ── checks ───────────────────────────────────────────────────────────────
    def _acl_lines(self, path: Path) -> "list[str]":
        """ACL entries on `path`, one per line (empty on none/error).

        Uses `ls -lde` (d = the directory itself, not contents) —
        unprivileged, like the Linux backend's bare getfacl. macOS
        prints each ACL entry below the stat line as
        ``0: user:evolve allow list,search,…``; strip the index prefix.
        """
        try:
            proc = self._run(["/bin/ls", "-lde", str(path)],
                             capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return []
        except Exception:
            return []
        entries: "list[str]" = []
        for line in proc.stdout.splitlines()[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(":", 1)
            if len(parts) == 2 and parts[0].strip().isdigit():
                stripped = parts[1].strip()
            entries.append(stripped)
        return entries

    def acl_user_effective(self, path: Path, user: str, required: str) -> bool:
        """True if `path` has an `allow` ACE for `user` covering at least
        `required`. "At least": operators may have hand-added more
        permissive entries; those already satisfy the contract. On
        directories, source-form names are resolved to the directory
        equivalents `ls -lde` prints (see `_MACOS_DIR_ACL_PERM_MAP`).

        macOS mode bits and ACLs are orthogonal (no mask), so ACE
        presence IS effective here — the Linux backend is the one that
        must intersect with the mask.
        """
        raw_needed = _split_perms(required)
        if _is_dir(path):
            needed: set[str] = set()
            for p in raw_needed:
                needed.update(_MACOS_DIR_ACL_PERM_MAP.get(p, (p,)))
        else:
            needed = raw_needed
        for entry in self._acl_lines(path):
            if not entry.startswith(f"user:{user} allow"):
                continue
            try:
                perms_part = entry.split("allow", 1)[1].strip()
            except IndexError:
                continue
            if needed.issubset({p.strip() for p in perms_part.split(",")}):
                return True
        return False

    def acl_group_effective(self, path: Path, group: str, required: str) -> bool:
        """Group analogue of :meth:`acl_user_effective` (matches a named
        ``group:<g> allow`` ACE in ``ls -lde``; macOS ACLs carry no mask, so
        ACE presence IS effective). Not exercised by the admin-socket path on
        macOS — bots reach the socket via its ``staff`` group OWNERSHIP, not a
        named ACE — but provided for Protocol symmetry with the Linux backend,
        where the bot-group connect ACE is load-bearing."""
        raw_needed = _split_perms(required)
        if _is_dir(path):
            needed: set[str] = set()
            for p in raw_needed:
                needed.update(_MACOS_DIR_ACL_PERM_MAP.get(p, (p,)))
        else:
            needed = raw_needed
        for entry in self._acl_lines(path):
            if not entry.startswith(f"group:{group} allow"):
                continue
            try:
                perms_part = entry.split("allow", 1)[1].strip()
            except IndexError:
                continue
            if needed.issubset({p.strip() for p in perms_part.split(",")}):
                return True
        return False

    def effective_mode(self, path: Path) -> int:
        """Plain stat — macOS mode bits are not entangled with ACLs.
        Raises OSError (incl. PermissionError) like ``Path.stat``."""
        return path.stat().st_mode & 0o7777

    def acl_masked_owner_only(self, path: Path) -> bool:
        """Always False on macOS — out of scope by construction.

        The "stat group bits are really the ACL mask" false positive this
        guards against is a *Linux POSIX-ACL* artifact: a named-user entry
        forces a ``mask`` whose value the group triad of ``st_mode`` then
        displays. macOS extended ACLs have no such mask, so a macOS group/
        other mode bit is always real — never suppress on its account. See
        :meth:`LinuxPerms.acl_masked_owner_only` for the substance."""
        return False

    # ── mask repair ──────────────────────────────────────────────────────────
    def reassert_mask(self, path: Path, *, recursive: bool = False) -> bool:
        """No-op: macOS has no POSIX ACL mask to clobber."""
        return True


class LinuxPerms:
    """The Linux adapter — POSIX access + default ACLs via setfacl/getfacl
    (design §4). Same injectable ``runner`` seam as :class:`MacOSPerms`.

    Inheritance: a macOS inheritable ACE becomes the *pair* of an access
    ACL (existing tree) and a default ACL (new children created in the
    dir). Default ACLs only exist on directories; single-shot ``grant``
    adds the ``-d`` entry only when the target is a dir whose macOS verb
    string carried inherit flags.
    """

    def __init__(self, runner: "_Runner | None" = None) -> None:
        self._runner = runner

    def _run(self, cmd: "list[str]", **kwargs: Any) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(cmd, **kwargs)
        return subprocess.run(cmd, **kwargs)

    def _setfacl(self, args: "list[str]", *, timeout: int = 10) -> bool:
        try:
            proc = self._run(["sudo", SETFACL, *args],
                             capture_output=True, text=True, timeout=timeout)
        except Exception:
            return False
        return proc.returncode == 0

    # ── grants ───────────────────────────────────────────────────────────────
    def grant_read_recursive(
        self, path: Path, user: str, *, restrict_group_other: bool = False
    ) -> bool:
        spec = f"u:{user}:rX"
        if restrict_group_other:
            # Bot-private ``.openclaw`` contract (the keystone that makes the
            # merged #3190 audit suppression effective on REAL files): clamp the
            # real ``group::``/``other::`` to nothing and PIN the mask at ``rX``,
            # in both the access (existing tree) and default (future children)
            # ACLs. Without this, ``setfacl -d`` auto-copies the dir's permissive
            # access base entries into the default, so every file the OC gateway
            # mints under ``.openclaw`` is born genuinely ``group::r-x`` /
            # ``other::r-x`` — a cross-bot read leak that #3190 deliberately does
            # NOT suppress (it fires on real group/other grants). After this clamp
            # the stat group triad still shows the mask (``r-x`` → OC reports
            # "650"), but the real ``group::``/``other::`` are ``---``, so
            # ``acl_masked_owner_only`` proves the finding a mask artifact and
            # #3190 suppresses it — with zero real exposure. The explicit
            # ``m::rX`` both keeps evolve's named ``rX`` effective AND performs the
            # same self-heal the bare ``-m`` recompute below does (re-widening a
            # gateway-0700-clamped ``mask::---`` back to ``rX``).
            #
            # ``g::---``/``o::---`` also recurse onto workspace/ here; the
            # ``share_group_other_read`` write grants that run AFTER
            # (workspace/evolve, workspace/manifests, evolve-backup) re-widen that
            # ONE shared channel — see set_evolve_read_acl.
            spec = f"{spec},g::---,o::---,m::rX"
        # ``setfacl -m`` WITHOUT ``-n`` RECOMPUTES the ACL mask on every path it
        # touches (mask := union of group:: and all named entries) — so the bare
        # (non-restrict) call also RE-WIDENS a mask that a prior 0700-harden (OC
        # gateway chmod / ``chmod 600`` on a secret) clamped to ``---``. That
        # implicit recalc is the load-bearing self-heal: re-running
        # grant_read_recursive after the gateway clamps restores evolve's
        # effective ``rX``. (The restrict path pins the same ``rX`` explicitly via
        # ``m::rX``, so the self-heal holds there too.) The ``-R -m`` emission is
        # pinned by test_perms_seam (…emits_access_plus_default_acl_pair); the
        # actual kernel mask-recompute under a real 0700 clamp is proven live by
        # the ubuntu e2e (test_step4h… / test_step6c2…). The harden→reassert
        # coupling for the gateway's *own* re-clamp lives at the call sites that
        # invoke it (safe_write_bot_config, heal_evolve_access) via reassert_mask.
        ok = self._setfacl(["-R", "-m", spec, str(path)], timeout=30)
        # Default ACL = inheritance for files/dirs created later. -R sets
        # it on every directory in the tree (files can't carry defaults).
        self._setfacl(["-R", "-d", "-m", spec, str(path)], timeout=30)
        return ok

    def grant_write_recursive(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False,
        share_group_other_read: bool = False,
    ) -> bool:
        spec = f"u:{user}:{_linux_entry_bits(perms)}"
        if share_group_other_read:
            # workspace/ shared-channel exception to the .openclaw group/other
            # clamp (grant_read_recursive restrict_group_other). The BOT owns the
            # channel DIR but not the evolve-WRITTEN files inside it (manifests,
            # rec-hints, the audit/fit-review queues), and has no named ACE on
            # them — it reads them through the ``other`` class. So re-assert
            # ``group::r-x``/``other::r-x`` (access + default) here; the mask is
            # left to recompute to the named entry's ``rwX`` (group/other stay
            # bounded by their own ``r-x`` base entries). Without this the clamp
            # would mint these ``other::---`` and starve the bot's reads.
            spec = f"{spec},g::r-x,o::r-x"
        ok = self._setfacl(["-R", "-m", spec, str(path)], timeout=30)
        self._setfacl(["-R", "-d", "-m", spec, str(path)], timeout=30)
        return ok

    def grant(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False
    ) -> bool:
        spec = f"u:{user}:{_linux_entry_bits(perms)}"
        ok = self._setfacl(["-m", spec, str(path)])
        wants_inherit = ("file_inherit" in perms or "directory_inherit" in perms)
        if wants_inherit and _is_dir(path):
            self._setfacl(["-d", "-m", spec, str(path)])
        return ok

    def grant_traverse(self, path: Path, user: str) -> bool:
        """Execute-only (``--x``) ACE so ``user`` can traverse ``path`` to
        reach an ACL'd child WITHOUT being able to read/list ``path`` itself.

        The Linux sharp edge the macOS backend never hits: Ubuntu's
        ``useradd -m`` honours ``/etc/login.defs`` ``HOME_MODE`` (0750 on
        modern Debian/Ubuntu), so ``/home/<bot>`` is ``drwxr-x--- <bot>:<bot>``
        — the ``evolve`` service user cannot even traverse it, and the
        ``rX`` ACL the read contract puts on ``.openclaw`` is unreachable
        (every ancestor needs ``x``). A plain ``rX`` grant on the home dir
        would also leak the directory *listing* (filenames of ``.ssh``,
        ``.bash_history``, …); ``--x`` is the least-privilege fix — pass
        through, see nothing. NO default ACL (single dir, not a tree) and
        NO recursion. Matches the W10-F live-verified hotfix argv
        (``setfacl -m u:evolve:--x /home/<bot>``); the W4b sudoers writer
        grants exactly this shape."""
        return self._setfacl(["-m", f"u:{user}:--x", str(path)])

    # ── carve-out ────────────────────────────────────────────────────────────
    def clear_acl(self, path: Path, *, recursive: bool = False) -> bool:
        """Strip the access ACL (and, on dirs, the default ACL) — design
        §4's translation of ``chmod -N`` / ``chmod -R -N``. Load-bearing
        for the credentials/ + profile-.md carve-outs (non-recursive) and
        the build-plugin dist-restore (recursive). ``-k`` (drop default
        ACL) only applies to directories; under ``-R`` setfacl walks the
        tree and applies ``-k`` to every directory it finds, so the
        single ``-R -k`` covers nested dirs that a top-level ``_is_dir``
        check would miss."""
        rflag = ["-R"] if recursive else []
        timeout = 30 if recursive else 10
        ok = self._setfacl([*rflag, "-b", str(path)], timeout=timeout)
        if recursive or _is_dir(path):
            self._setfacl([*rflag, "-k", str(path)], timeout=timeout)
        return ok

    # ── checks ───────────────────────────────────────────────────────────────
    def _getfacl_lines(self, path: Path) -> "list[str]":
        """Raw getfacl output lines ([] on error). Unprivileged, matching
        the macOS backend's bare ``ls -lde`` probe. ``-p`` keeps absolute
        names (no "Removing leading '/'" noise)."""
        try:
            proc = self._run([GETFACL, "-p", str(path)],
                             capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return []
        except Exception:
            return []
        return proc.stdout.splitlines()

    @staticmethod
    def _entry_effective(line: str) -> "tuple[str, str] | None":
        """Parse one access-ACL line → (qualifier, effective-perms).

        ``user:evo:rwx\t#effective:r--`` → ("user:evo", "r--"); entries
        without an annotation are their own effective value. Default-ACL
        lines and comments return None (presence checks are about the
        access ACL — the default ACL only shapes future children).
        """
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("default:"):
            return None
        effective: "str | None" = None
        if "#effective:" in text:
            text, _, eff = text.partition("#effective:")
            effective = eff.strip()
            text = text.strip()
        parts = text.split(":")
        if len(parts) < 3:
            return None
        qualifier = ":".join(parts[:-1])
        perms = effective if effective is not None else parts[-1]
        return qualifier, perms

    @staticmethod
    def _entry_stored(line: str) -> "tuple[str, str] | None":
        """Parse one access-ACL line → (qualifier, STORED-perms) — the grant as
        written, IGNORING any ``#effective:`` mask annotation.

        ``user:mallory:r--\t#effective:---`` → ("user:mallory", "r--"). Used by
        :meth:`acl_masked_owner_only` to judge a foreign named ACE by what it
        GRANTS, not by what a momentarily-clamped mask currently lets through —
        a latent grant is still an exposure once the mask widens. Default-ACL
        lines and comments return None (same scope as :meth:`_entry_effective`).
        """
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("default:"):
            return None
        if "#effective:" in text:
            text, _, _ = text.partition("#effective:")
            text = text.strip()
        parts = text.split(":")
        if len(parts) < 3:
            return None
        return ":".join(parts[:-1]), parts[-1]

    @classmethod
    def _stored_group_other_bits(cls, lines: "list[str]") -> "tuple[int, int]":
        """The owning ``group::`` and ``other::`` entries' STORED rwx bits
        (0-7 each), ignoring any ``#effective:`` mask annotation.

        Used by :meth:`acl_masked_owner_only` so a real ``group::r-x`` /
        ``other::r`` that a momentarily-clamped ``mask::---`` shows as
        ``#effective:---`` still counts as the latent exposure it is — it
        re-opens the instant the mask widens. Missing entries → 0 bits.
        """
        gbits = obits = 0
        for line in lines:
            parsed = cls._entry_stored(line)
            if parsed is None:
                continue
            qual, perms = parsed
            bits = (
                (4 if "r" in perms else 0)
                | (2 if "w" in perms else 0)
                | (1 if "x" in perms else 0)
            )
            if qual == "group:":
                gbits = bits
            elif qual == "other:":
                obits = bits
        return gbits, obits

    def acl_user_effective(self, path: Path, user: str, required: str) -> bool:
        """EFFECTIVE-perm check (design §4's "one place the Linux check
        must be stronger"): an ACE capped by the mask shows
        ``#effective:`` in getfacl — compare against that, not the
        stored entry, so a chmod-clobbered mask reads as drift."""
        needed = _linux_needed_bits(required)
        for line in self._getfacl_lines(path):
            parsed = self._entry_effective(line)
            if parsed is None or parsed[0] != f"user:{user}":
                continue
            if needed.issubset(set(parsed[1])):
                return True
        return False

    def acl_group_effective(self, path: Path, group: str, required: str) -> bool:
        """EFFECTIVE-perm check for a NAMED ``group:<g>`` ACE — the group
        analogue of :meth:`acl_user_effective`. The admin-daemon socket's
        shared-bot-group connect ACE (``group:evolve-bots:rwx``) is exactly the
        case the mask gotcha bites: a later ``setfacl``/``chmod`` that recomputes
        the mask could cap the named entry, which getfacl then annotates
        ``#effective:``. Compare against the EFFECTIVE bits (not the stored
        entry) so a clamped mask reads as drift. Matches the named group entry
        (``group:<g>``), never the owning-group base entry (``group:``)."""
        needed = _linux_needed_bits(required)
        for line in self._getfacl_lines(path):
            parsed = self._entry_effective(line)
            if parsed is None or parsed[0] != f"group:{group}":
                continue
            if needed.issubset(set(parsed[1])):
                return True
        return False

    def effective_mode(self, path: Path) -> int:
        """Mode with the ACL-mask lie corrected: on an ACL'd path the
        stat group triad displays the *mask*, not the group perms —
        substitute the real ``group::`` entry's effective bits so
        mode-assertion sites don't false-positive (design §4 sharp-edge
        consequence 2). Raises OSError like ``Path.stat``."""
        return self._effective_mode_from(path, self._getfacl_lines(path))

    def _effective_mode_from(self, path: Path, lines: "list[str]") -> int:
        """:meth:`effective_mode` over pre-fetched getfacl ``lines`` — so a
        caller that already ran ``getfacl`` (``acl_masked_owner_only``)
        doesn't spawn it twice (and reads a single consistent snapshot,
        closing a TOCTOU window). Raises OSError like ``Path.stat``."""
        base = path.stat().st_mode & 0o7777
        if not any(line.strip().startswith("mask::") for line in lines):
            return base  # no extended ACL — stat tells the truth
        for line in lines:
            parsed = self._entry_effective(line)
            if parsed is None or parsed[0] != "group:":
                continue
            gbits = 0
            perms = parsed[1]
            if "r" in perms:
                gbits |= 4
            if "w" in perms:
                gbits |= 2
            if "x" in perms:
                gbits |= 1
            return (base & ~0o070) | (gbits << 3)
        return base

    def acl_masked_owner_only(self, path: Path) -> bool:
        """True iff ``path``'s stat group/other bits are a pure ACL-MASK
        artifact of Evolve's own service-user ACL — the *real* ``group::``
        and ``other::`` entries grant nothing AND every named ACE that can
        still read/write is one of Evolve's trusted service principals
        (:data:`EVOLVE_ACL_PRINCIPALS`). The file is owner-only in fact;
        only ``user:evolve`` (capped by the mask, which the stat group triad
        then displays) reaches it.

        This is the ACL-grounded test behind ``audit.py``'s suppression of
        OpenClaw's ``fs.*.perms_*readable`` family on Linux. Evolve's
        evolve-read ACL adds ``user:evolve:r-x``, which forces ``mask::r-x``;
        the group triad of ``st_mode`` then *displays the mask*, so a 0600
        file reads as 0640/0650. OpenClaw's audit stats ``st_mode`` (it is
        not ACL-aware, and we do not fork it), so it reports "group/other
        readable" — but when the real ``group::``/``other::`` are ``---`` and
        the only named reader is ``evolve``, the finding is a false positive.

        The test is grounded in the *effective* (getfacl-derived) ACL, NOT in
        the raw ``st_mode`` group/other bits. Those bits flap under the
        evolve-read mask (the OC gateway clamps ``mask::---`` on the active log
        while Evolve's reassert restores ``mask::r-x``; ``st_mode`` oscillates
        600⇄650), so an owner-only ``st_mode`` is the STRONGEST proof of
        non-exposure — when getfacl confirms the real ``group::``/``other::``
        are ``---`` and the only named reader is a service principal, the
        finding is suppressed even if this stat caught the file back at 600.
        Reading the raw bits as "stale, must fire" was the TOCTOU bug behind
        the fresh-pod flap storm.

        Returns **False** — i.e. the finding must fire — when:

        - there is no extended ACL (no ``mask`` line): ``st_mode`` is then
          truthful, not a mask reflection;
        - the real ``group::`` OR ``other::`` GRANTS any access (judged by the
          stored entry, not the momentary ``#effective`` bits) — a GENUINE
          exposure, including a ``group::r-x`` the mask is currently clamping
          to ``---`` (it re-opens the instant the mask widens) and any real
          ``other::r`` (the mask never caps the *other* class). This is what
          keeps a 0644 config (real ``other::r``) firing — the 2026-06-12 fix
          that removed the blanket ``fs.config.perms_world_readable``
          suppression must not regress;
        - a NAMED ACE for a non-service principal (e.g. ``group:staff:r-x``
          or ``user:mallory:r``) is GRANTED access — likewise judged by the
          stored grant, so a clamped mask can't hide it. That is a real grant
          Evolve never makes, so the mask isn't merely capping the evolve ACL
          and the finding is honest.

        Best-effort and fail-closed: any getfacl/stat error → False (emit).
        """
        lines = self._getfacl_lines(path)
        if not self._has_access_mask(lines):
            return False  # no mask → st_mode group triad is the real group::
        try:
            path.stat()  # fail-closed existence/readability guard (mode unused)
        except OSError:
            return False  # vanished/unstatable between audit and re-check → emit
        # We deliberately do NOT gate on the raw st_mode group/other bits:
        # owner-only st_mode (raw & 0o077 == 0) is the STRONGEST proof of
        # non-exposure, not a reason to bail. Under the flapping ACL mask
        # (mask::--- ⇄ mask::r-x; the OC gateway clamps mask::--- on the active
        # log while Evolve's evolve-read reassert restores mask::r-x), st_mode
        # oscillates 600⇄650; OC catches a 650 window and emits perms_readable,
        # then this suppressor re-stats and may catch the file back at 600.
        # The OLD behaviour short-circuited to False there (raw & 0o077 == 0),
        # un-suppressing the flapped finding → fire→clear→fire storm. Instead,
        # judge exposure from the getfacl entries directly — and from the
        # entries' STORED grant, not the momentary mask-capped #effective bits:
        # a real group::r-x / other::r currently clamped to --- by mask::--- is
        # a LATENT exposure that re-opens the instant the mask widens, so it
        # must fire; whereas an entry that GRANTS nothing (group::---/other::---,
        # the genuine owner-only file) is no exposure at any mask. This reads
        # getfacl regardless of st_mode, closing the TOCTOU window.
        gbits, obits = self._stored_group_other_bits(lines)
        if (gbits | obits) & 0o7 != 0:
            return False  # real group:: or other:: grant — a genuine exposure
        # Owning group/other are clean; the apparent bits come from the mask.
        # The mask caps the GROUP CLASS, i.e. all NAMED ACEs too — so confirm
        # the only principals it's capping are Evolve's own (else a named grant
        # to some other user/group really can read, and OC is right to fire).
        #
        # Judge a NAMED non-service ACE by its STORED grant, not its current
        # ``#effective`` bits: under a flapped-clamped ``mask::---`` even a
        # ``user:mallory:r--`` shows ``#effective:---``, but that grant is
        # latent — it becomes a real read the instant the evolve-read reassert
        # widens the mask back to ``r-x``. A foreign principal that the ACL
        # GRANTS anything is an exposure regardless of the momentary mask, so
        # the finding must fire. (Evolve's own service ACEs are exempt — they
        # are the intended readers; that is the whole point of the suppression.)
        for line in lines:
            parsed = self._entry_stored(line)
            if parsed is None:
                continue
            qual, perms = parsed
            is_named_user = qual.startswith("user:") and qual != "user:"
            is_named_group = qual.startswith("group:") and qual != "group:"
            if not (is_named_user or is_named_group):
                continue  # owner / owning-group / owning-other / mask
            if set(perms) & {"r", "w", "x"} and qual.split(":", 1)[1] not in EVOLVE_ACL_PRINCIPALS:
                return False  # a non-service principal is granted access
        return True

    # ── mask repair ──────────────────────────────────────────────────────────
    @staticmethod
    def _has_access_mask(lines: "list[str]") -> bool:
        # Access-ACL mask only: "default:mask::…" lines (a dir's default
        # ACL) don't cap anything on the dir itself.
        return any(line.strip().startswith("mask::") for line in lines)

    @staticmethod
    def _mask_caps_entries(lines: "list[str]") -> bool:
        """True when the access mask is actually CAPPING some entry.

        getfacl annotates an entry with ``\t#effective:<bits>`` exactly when
        the mask reduces its stored bits — a block with a mask but no
        annotation is already fully effective and re-widening it is a wasted
        privileged exec. This is the guard that keeps the hourly Tier-1
        reassert quiet in steady state: without it, every ACL'd file under
        the swept subtrees got one ``sudo setfacl -m m::rwX`` per cycle
        (~12k/day on the two-bot VPS), healthy or not."""
        return any("#effective:" in line for line in lines)

    def _masked_paths_recursive(self, path: Path) -> "list[str]":
        """Paths under ``path`` (inclusive) whose ACCESS ACL carries a
        mask entry that CAPS at least one entry (``#effective:``
        annotation — see :meth:`_mask_caps_entries`). ``getfacl -R -s``
        skips base-entries-only files (so carve-outs like credentials/
        never appear); ``-p`` keeps paths absolute. Output parses
        regardless of exit code — ``-R`` returns nonzero if any one
        subpath errored but still lists the rest, and mask repair is
        best-effort."""
        try:
            proc = self._run([GETFACL, "-R", "-s", "-p", str(path)],
                             capture_output=True, text=True, timeout=30)
        except Exception:
            return []
        out: "list[str]" = []
        current: "str | None" = None
        has_mask = False
        capped = False
        for line in (proc.stdout or "").splitlines() + [""]:
            text = line.strip()
            if text.startswith("# file:"):
                current = text[len("# file:"):].strip()
                has_mask = capped = False
            elif text.startswith("mask::"):
                has_mask = True
            elif not text:  # blank line terminates a getfacl block
                if current is not None and has_mask and capped:
                    out.append(current)
                current, has_mask, capped = None, False, False
            if "#effective:" in line:
                capped = True
        return out

    def reassert_mask(self, path: Path, *, recursive: bool = False) -> bool:
        """Re-widen the ACL mask after a mode change (consequence 1 of
        the sharp edge: chmod's group bits BECOME the mask and silently
        cap every named ACE). ``m::rwX`` is safe-generous — the mask only
        caps named entries (whose grants are the intent) and the group
        class stays bounded by its own ``group::`` entry.

        Guarded per-path: paths with no access mask are left alone
        (``setfacl -m m::`` on a plain file would *create* an extended
        ACL where none belongs — e.g. the credentials/ carve-out), and
        paths whose mask isn't capping any entry (no ``#effective:``
        annotation) are already healthy — skipping them keeps the hourly
        reassert sweeps at zero privileged execs in steady state. The
        recursive form therefore never runs ``setfacl -R``: it
        enumerates the ACL'd paths under the tree (``getfacl -R -s``)
        and repairs only the actually-capped ones, so a tree whose root
        has no ACL still gets masked children repaired, and non-ACL'd
        children are never touched.
        """
        if not recursive:
            lines = self._getfacl_lines(path)
            if not (self._has_access_mask(lines) and self._mask_caps_entries(lines)):
                return True
            return self._setfacl(["-m", "m::rwX", str(path)])
        ok = True
        for masked in self._masked_paths_recursive(path):
            ok = self._setfacl(["-m", "m::rwX", masked]) and ok
        return ok


class FakePerms:
    """In-memory adapter for tests — ACL grants with zero subprocess.

    ``acl`` maps ``str(path)`` → user → granted verb set (source-form
    macOS vocabulary, inherit flags included). Mutations append to
    ``self.calls``; ``acl_user_effective`` answers from the table (seed
    pre-existing state via :meth:`seed_acl`). ``effective_mode`` is a
    real stat so tmp-path mode fixtures stay truthful.
    """

    def __init__(self) -> None:
        self.calls: "list[tuple]" = []
        self.acl: "dict[str, dict[str, set[str]]]" = {}
        self.group_acl: "dict[str, dict[str, set[str]]]" = {}
        self.cleared: "list[str]" = []
        self.masks: "list[tuple[str, bool]]" = []

    def seed_acl(self, path: "Path | str", user: str, perms: str) -> None:
        self.acl.setdefault(str(path), {}).setdefault(user, set()).update(
            _split_perms(perms))

    def seed_group_acl(self, path: "Path | str", group: str, perms: str) -> None:
        self.group_acl.setdefault(str(path), {}).setdefault(group, set()).update(
            _split_perms(perms))

    # ── grants ───────────────────────────────────────────────────────────────
    def grant_read_recursive(
        self, path: Path, user: str, *, restrict_group_other: bool = False
    ) -> bool:
        self.calls.append(
            ("grant_read_recursive", str(path), user, restrict_group_other))
        self.seed_acl(path, user, f"{POD_READ_ACL_PERMS},{_INHERIT_FLAGS}")
        return True

    def grant_write_recursive(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False,
        share_group_other_read: bool = False,
    ) -> bool:
        self.calls.append(
            ("grant_write_recursive", str(path), user, perms,
             share_group_other_read))
        self.seed_acl(path, user, perms)
        return True

    def grant(
        self, path: Path, user: str, perms: str, *, prefixed: bool = False
    ) -> bool:
        self.calls.append(("grant", str(path), user, perms))
        self.seed_acl(path, user, perms)
        return True

    def grant_traverse(self, path: Path, user: str) -> bool:
        self.calls.append(("grant_traverse", str(path), user))
        self.seed_acl(path, user, "search")
        return True

    # ── carve-out ────────────────────────────────────────────────────────────
    def clear_acl(self, path: Path, *, recursive: bool = False) -> bool:
        self.calls.append(("clear_acl", str(path), recursive))
        self.acl.pop(str(path), None)
        self.cleared.append(str(path))
        return True

    # ── checks ───────────────────────────────────────────────────────────────
    def acl_user_effective(self, path: Path, user: str, required: str) -> bool:
        granted = self.acl.get(str(path), {}).get(user, set())
        return _split_perms(required).issubset(granted)

    def acl_group_effective(self, path: Path, group: str, required: str) -> bool:
        granted = self.group_acl.get(str(path), {}).get(group, set())
        return _split_perms(required).issubset(granted)

    def effective_mode(self, path: Path) -> int:
        return Path(path).stat().st_mode & 0o7777

    def acl_masked_owner_only(self, path: Path) -> bool:
        # In-memory fake has no mask model; nothing is ever a mask artifact.
        return False

    # ── mask repair ──────────────────────────────────────────────────────────
    def reassert_mask(self, path: Path, *, recursive: bool = False) -> bool:
        self.calls.append(("reassert_mask", str(path), recursive))
        self.masks.append((str(path), recursive))
        return True


# ── factory (mirrors runtime.scheduler / runtime.isolation) ──────────────────

_override: "Perms | None" = None
_defaults: "dict[str, Perms]" = {}


def get_perms() -> Perms:
    """Return the process-wide perms adapter.

    Default is keyed off the active platform profile (macOS → chmod +a
    backend, Linux → setfacl backend) so a pinned profile selects the
    matching backend; a :func:`set_perms` injection wins over both.
    """
    if _override is not None:
        return _override
    name = get_profile().name
    if name not in _defaults:
        _defaults[name] = LinuxPerms() if name == "linux" else MacOSPerms()
    return _defaults[name]


def set_perms(perms: "Perms | None") -> None:
    """Swap the adapter (tests inject FakePerms / a recorded-runner
    backend). Pass ``None`` to restore the profile-keyed default."""
    global _override
    _override = perms
