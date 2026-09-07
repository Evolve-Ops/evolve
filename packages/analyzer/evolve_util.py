"""evolve_util — the blessed home for shared file/time primitives.

Phase 6.2 of internal/roadmap-80-to-100-2026-06-09.md. Before this module,
44 files defined their own ``_atomic_write*`` and 81 defined ``_now_iso``
— a bug fixed in one copy stayed broken in the others. These are the
single definitions; ``tools/dup-primitive-lint`` (CI-enforced) blocks new
local copies.

Import with an alias to keep existing call sites unchanged::

    from evolve_util import atomic_write_json as _atomic_write_json
    from evolve_util import now_iso as _now_iso

Bot identity resolution (``bot_home`` / ``get_bot_user``) deliberately
does NOT live here — its blessed home is ``evolve_config`` (it is
config-layer logic: reads network.json, knows bot_id ≠ macOS account).

Timestamp variants: three formats were in live use when this module was
created, and the format is persisted in stores/logs/signatures — blindly
unifying would change on-disk data mid-stream. Each caller migrated to
the variant matching its existing output; ``now_iso()`` (Z, seconds) is
the canonical choice for NEW code. Store-by-store unification is a
separate, data-migration-shaped task.

``assert_safe_sudo_dest`` is here for the same reason, one layer down:
it is not a duplicated helper being consolidated but a safety gate that
BOTH sides of the graph need before they shell out to a root ``cp`` /
``chown`` / ``chmod``, and the two callers cannot share anything higher.
``oc_model`` runs under the SYSTEM python as the bot user and cannot
import ``evolve_admin``; ``evolve_admin.migrate_model_roles`` runs under
the venv as ``evolve``. This module is the only place both can reach.

This module must stay stdlib-only — it sits at the bottom of the
dependency graph (analyzer and admin both import it; it imports nothing
of theirs).
"""

from __future__ import annotations

import json
import os
import stat as _stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_text",
    "atomic_write_json",
    "assert_safe_sudo_dest",
    "assert_no_symlink_in_path",
    "now_iso",
    "now_iso_offset",
    "now_iso_micro",
]


# ── Atomic writes ────────────────────────────────────────────────────────────
#
# Same-directory tempfile + os.replace: the rename is atomic on the same
# filesystem, so readers see either the old file or the new file, never a
# torn write. /tmp staging would cross filesystems and lose atomicity —
# that pattern exists only for sudo-mediated writes to OTHER users' files
# (see safe_write_bot_config in evolve_admin.deploy; not this module's job).


def atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Write ``content`` to ``path`` atomically (same-dir temp + os.replace).

    ``mode`` chmods the file before the rename (e.g. ``0o644`` for files
    other users must read; mkstemp's default is 0o600). Parent directory
    must exist. On failure the temp file is removed and the original
    ``path`` is untouched.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    mode: int | None = None,
) -> None:
    """Serialize ``data`` as JSON and write it atomically via atomic_write_text.

    Defaults mirror the dominant pre-consolidation cluster: ``indent=2``,
    insertion order preserved. Pass ``sort_keys=True`` where stable diffs
    matter (signature files, baselines).
    """
    atomic_write_text(
        path,
        json.dumps(data, indent=indent, sort_keys=sort_keys),
        mode=mode,
    )


# ── Root-mediated write safety ───────────────────────────────────────────────


def _assert_real_dir(d: Path) -> None:
    """lstat ``d`` and refuse unless it is a real directory (never a symlink).

    Shared by the ``anchor=`` chain walk and kept deliberately separate from the
    ``path.parent`` check below, whose message text is a pinned contract.
    """
    try:
        st = os.lstat(d)
    except OSError as e:
        raise PermissionError(
            f"refusing sudo write: cannot verify intermediate component {d} "
            f"({e}) — refusing to issue a root cp/chown against an "
            f"unverifiable destination"
        ) from e
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise PermissionError(
            f"refusing sudo write: intermediate component {d} is a symlink or "
            f"not a directory — it redirects the whole write out of tree"
        )


def _intermediates_below_anchor(path: Path, anchor: Path) -> "list[Path]":
    """Every directory component strictly BETWEEN ``anchor`` and ``path.parent``.

    ``anchor`` itself is NOT returned (it is trusted by contract — see
    ``assert_safe_sudo_dest``) and neither is ``path.parent`` (the caller
    already checks it, with its own pinned message).

    Fails closed rather than returning a short list: a relative path, a ``..``
    component, or a ``path`` outside ``anchor`` all mean the caller cannot say
    which components are attacker-writable, and this is a gate in front of a
    root command.
    """
    if not anchor.is_absolute() or not path.is_absolute():
        raise PermissionError(
            f"refusing sudo write: anchored check needs absolute paths, got "
            f"path={path} anchor={anchor}"
        )
    if ".." in path.parts or ".." in anchor.parts:
        raise PermissionError(
            f"refusing sudo write: '..' in path={path} or anchor={anchor} — a "
            f"traversal component makes the trusted prefix unprovable"
        )
    try:
        rel = path.relative_to(anchor)
    except ValueError:
        raise PermissionError(
            f"refusing sudo write: {path} is not under the trusted anchor "
            f"{anchor} — refusing to issue a root cp/chown against a "
            f"destination whose ancestry this caller does not vouch for"
        ) from None
    if not rel.parts:
        raise PermissionError(
            f"refusing sudo write: {path} IS the anchor {anchor}, not a file "
            f"below it"
        )
    dirs: "list[Path]" = []
    cur = anchor
    for part in rel.parts[:-1]:
        cur = cur / part
        dirs.append(cur)
    # dirs[-1] is path.parent by construction — dropped, not skipped: the main
    # body lstats it under its own (pinned) message.
    return dirs[:-1]


def assert_safe_sudo_dest(
    path: "Path | str", *, anchor: "Path | str | None" = None
) -> None:
    """Refuse a root-mediated write/chown/chmod through a symlinked destination.

    #3566 audit D-2. The CLAUDE.md "Writes" pattern — stage in ``/tmp``, then
    ``sudo /bin/cp tmp dest`` (+ ``sudo chown``/``sudo chmod`` to repair the
    mode) — is how a non-root service user lands a file it does not own. Every
    one of those three commands FOLLOWS a symlink at ``dest``: ``cp`` has no
    flag that refuses to, and ``chown``/``chmod`` follow unless passed ``-h``.
    All three run as root. So an unchecked destination turns a legitimate
    config write into an arbitrary root-write primitive: plant a symlink where
    the writer expects its file, and the content lands on the link's target and
    the target gets relabelled to the config file's mode.

    The sudoers path pin does NOT help. sudo matches the literal argv, and the
    argv is the legitimate-looking link path; the kernel resolves the link only
    once ``cp`` is already running as root.

    Reproduced end-to-end (#3566 audit) against ``oc_model._save_tiers_file``
    on origin/main: with ``~/.openclaw/evolve-tiers.json`` replaced by a
    symlink to a victim file, the victim's CONTENT was overwritten through the
    link.

    Checked without following anything (``os.lstat``, never ``Path.exists`` /
    ``Path.is_file``, both of which follow):

      * the parent must be a real directory, not a symlink — otherwise
        ``<link>/<name>`` redirects the whole write out of tree;
      * the destination must be absent (a fresh ``cp`` creates a real file) or
        a real regular file — never a symlink, never a device/fifo;
      * the destination must have exactly ONE link. A HARD link needs no
        symlink and defeats every check above: it *is* a real regular file, so
        ``S_ISREG`` passes, and ``lstat`` reports the victim inode's own uid and
        mode because there is no indirection to see through. On macOS an
        unprivileged user may hard-link a file it neither owns nor can read
        (verified 2026-08-11: a non-admin account linked root-owned 0440
        ``/private/etc/sudoers`` into a directory it owns; ``/private/etc`` and
        ``/Users`` share the Data volume, so the same-filesystem constraint is
        no obstacle on the mini). The result is strictly worse than the symlink
        case: ``chown <bot>:staff`` through it transfers ownership of the victim
        INODE permanently, and the drift detectors read ``owner_uid = 0`` — the
        exact condition their repair exists to fix — so it is summoned on the
        next ``ensure_pod_perms`` pass with no race to win. Linux blocks the
        plant itself under the default ``fs.protected_hardlinks=1``; macOS has no
        equivalent, and macOS is the primary pod. A config file written by
        ``cp``/``os.replace`` always has ``st_nlink == 1``, so this refuses no
        legitimate destination. (#3566 audit D-2, second variant.)

    FAIL-CLOSED on an unreadable destination (EACCES and friends), with a
    message deliberately distinct from the symlink one so an operator can tell
    "unverifiable" from "attacked". When the caller cannot even ``lstat`` the
    destination, refusing beats issuing a root ``cp`` it cannot verify: an
    unverifiable dest is precisely the case an attacker controls, and in that
    same state the ordinary READ of the file is already failing, so this
    surfaces an existing outage more loudly rather than creating a new one.

    RESIDUAL, stated rather than papered over: lstat-then-subprocess is TOCTOU
    (same shape as ``tier_prefs_acl._resolve_bot_dir``, #3565 audit). This gate
    converts a PERSISTENT plant that fires on every write into a narrow race
    that has to be timed against one. Closing it fully needs an
    ``O_NOFOLLOW``-anchored fd, which ``cp``/``chown``/``chmod`` do not accept.
    Callers that follow the ``cp`` with a ``chown``/``chmod`` repair should
    re-assert between the two: ``cp`` through a symlink leaves the LINK in
    place, so a second check catches a plant that landed inside the window and
    stops the repair from relabelling the victim as well.

    ``anchor`` — the INTERMEDIATE-COMPONENT check, opt-in (#3566 audit D-2
    residual). Without it only ``path`` and ``path.parent`` are lstat'd, so
    every component above the parent is resolved by the kernel during those two
    calls. That is sound for a FLAT relpath like ``.openclaw/evolve-tiers.json``
    — its parent IS ``.openclaw`` and is checked, and ``<home>``/``/Users`` are
    root-owned, so a link at either needs root already. It is NOT sound for a
    nested one: in ``.openclaw/agents/main/agent/auth-profiles.json`` the
    ``agents`` and ``agents/main`` components live inside the bot-owned tree, so
    a symlink at one of them redirects the write while BOTH lstats see perfectly
    real objects (verified against this gate: with ``.openclaw/agents`` pointing
    at a victim tree, the unanchored call passes).

    Pass ``anchor`` and every directory component STRICTLY BELOW it — down to
    but excluding ``path.parent``, which the checks above already cover — is
    lstat'd and must be a real directory. The contract is deliberately
    one-sided: **``anchor`` and everything above it are trusted and never
    lstat'd; everything below it is attacker-writable until proven otherwise.**
    So the caller's job is to name the shallowest directory it is willing to
    vouch for. For the bot-config writers that is the BOT HOME: the bot owns it,
    but cannot replace the home ENTRY (that needs write on ``/Users`` /
    ``/home``, which is root's), so no symlink can appear AT it — while
    ``.openclaw`` and everything below it sits in a directory the bot can write.
    The walk then covers ``.openclaw`` and every nested component under it.
    ``secret_config_perms._bot_home_anchor`` derives
    it from the path's own ``.openclaw`` component; see its docstring for why not
    from the platform profile.

    Severity of what this closes, since the two legs differ: the
    ownership-TRANSFER caller (``secret_config_perms.chown_chmod_bot_config``) was
    never reachable this way — its relpaths are flat by contract. What was
    reachable is the root ``chmod 600`` landing on any file the bot can name whose
    path ends in ``/.git/config`` or ``/main/agent/auth-profiles.json``:
    DoS/relabel, not escalation — and still a root command an attacker aims.

    Why not the two alternatives, both of which were on the table:

      * ``os.path.realpath(path) == str(path)`` needs no anchor, but it cannot
        tell a HOST SHAPE from an attack: it also refuses when a component the
        attacker cannot touch is a symlink. A host whose home root sits behind
        one (``/home -> /export/home``, macOS homes on a mounted volume) would
        get a permanent false refusal — and a false refusal here stops the 0600
        self-heal and, on the tiers path, the repair that lets a bot read its
        own routing config. It buys nothing over the anchored walk, since the
        components it adds are exactly the root-owned ones. Both live pods
        happen to resolve ``/Users`` and ``/home`` to themselves (checked
        2026-08-11), which makes this look safe on the fleet of two and does not
        make it safe for an install base.
      * A REQUIRED anchor closes the same hole, but forces a signature change on
        every call site including ``oc_model``, which runs under the SYSTEM
        python as the bot user — a stale-module-cache pull (a real failure mode
        here) would then raise ``TypeError`` at the moment the gate is supposed
        to be guarding a root ``cp``. The two flat-relpath callers gain nothing
        from an anchor they would only be passing to satisfy the signature.

    Anchored mode fails closed on a relative ``path``/``anchor``, on a ``..``
    component in either, and on a ``path`` that is not under ``anchor`` — in all
    three the caller cannot state which components are attacker-writable, which
    is the one thing the anchor is for.

    Raises:
        PermissionError: the destination is unsafe or unverifiable; the caller
            must not write.
    """
    path = Path(path)
    parent = path.parent
    if anchor is not None:
        # Top-down, so the refusal names the SHALLOWEST planted component —
        # the one the operator has to go remove.
        for d in _intermediates_below_anchor(path, Path(anchor)):
            _assert_real_dir(d)
    try:
        pst = os.lstat(parent)
    except OSError as e:
        raise PermissionError(
            f"refusing sudo write: cannot verify {parent} ({e}) — refusing to "
            f"issue a root cp/chown against an unverifiable destination"
        ) from e
    if _stat.S_ISLNK(pst.st_mode) or not _stat.S_ISDIR(pst.st_mode):
        raise PermissionError(
            f"refusing sudo write: {parent} is a symlink or not a directory"
        )
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return  # fresh dest — cp creates a real file
    except OSError as e:
        raise PermissionError(
            f"refusing sudo write: cannot verify {path} ({e}) — refusing to "
            f"issue a root cp/chown against an unverifiable destination"
        ) from e
    if _stat.S_ISLNK(st.st_mode):
        raise PermissionError(
            f"refusing sudo write: {path} is a SYMLINK — a root cp/chown/chmod "
            f"would follow it onto {os.readlink(path)!r}"
        )
    if not _stat.S_ISREG(st.st_mode):
        raise PermissionError(f"refusing sudo write: {path} is not a regular file")
    if st.st_nlink != 1:
        # No indirection to see through — lstat already reported the victim's
        # own uid/mode. Named distinctly from the SYMLINK message so an operator
        # reading the log knows to go looking for the other name(s).
        raise PermissionError(
            f"refusing sudo write: {path} is a HARD LINK (st_nlink="
            f"{st.st_nlink}) — a root cp/chown/chmod would act on an inode that "
            f"is also reachable under another name"
        )


def _unverifiable(message: str) -> PermissionError:
    """A refusal the seam may log a rung quieter than an attack-shaped one.

    Same class, same "caller must not proceed" contract — the marker only
    separates "an ACL mask clamp raced us and we could not lstat" (benign and
    self-healing on the next pass) from "a component has been replaced" (an
    attack indicator). The perms seam mirrors ERROR admin-log lines into the
    Signal store, so logging both at ERROR would page an operator for the
    benign one.
    """
    exc = PermissionError(message)
    exc.unverifiable = True  # type: ignore[attr-defined]
    return exc


def assert_no_symlink_in_path(path: "Path | str") -> None:
    """Refuse a path that a privileged command would resolve through a symlink.

    The sibling of :func:`assert_safe_sudo_dest`, for the OTHER class of root
    command: not ``cp``/``chown``/``chmod`` against a *destination file*, but
    ``setfacl``/``chmod +a`` against an arbitrary *path argument* — a directory
    as often as a file, and one whose whole point may be to have children. The
    two gates are deliberately separate rather than one flag-laden helper:
    ``assert_safe_sudo_dest`` also asserts a *shape* (absent-or-regular-file),
    which is exactly wrong for an ACL target, and it checks only ``path.parent``
    on the theory that everything above a bot home is root-owned. This one
    asserts one thing and asserts it about EVERY component: nothing on the way
    to ``path`` redirects.

    Why the whole chain rather than the leaf: the perms seam's arguments are
    built by joining module constants onto a bot home
    (``<home>/.openclaw/agents/main/agent``), and the bot owns every directory
    from ``.openclaw`` down — so a plant at ANY of those components redirects
    the resolution, while the leaf and its parent stay perfectly real objects.

    **The uid-0 carve-out is the trust boundary, and it is derived from the
    filesystem rather than supplied by the caller.** A symlink whose ``lstat``
    uid is 0 cannot have been planted by an unprivileged attacker: ``ln -s``
    stamps the creating uid onto the link, and re-owning one needs root. So a
    root-owned symlink component is an OS or operator artifact (macOS's
    ``/tmp`` → ``/private/tmp`` and ``/var`` → ``/private/var``; an operator who
    relocated ``{shared_dir}`` onto another volume) and is allowed through,
    while every attacker-plantable link is refused. That is what lets this gate
    live at a seam with no notion of which prefix is trusted: it does not need
    to be told, it can see.

    ``evolve``-owned symlinks are NOT exempt either — only uid 0 is trusted. Be
    precise about what that does and does not buy, though: the boundary this
    gate enforces is against the BOT accounts, not against a compromised
    ``evolve``. ``evolve`` holds a ``sudo chown`` grant, so it could ``chown -h
    root`` a link straight past the carve-out — but it also holds the ``setfacl``
    grants outright, so it never needed the detour. Excluding ``evolve`` here is
    least-privilege hygiene, not containment; the containment claim is about the
    per-bot accounts, which have neither grant.

    FAIL-CLOSED on a component that cannot be ``lstat``'d. This costs nothing in
    availability terms at the one place it plausibly fires — a bot's
    ``.openclaw`` whose gateway-clamped ACL mask has taken away evolve's
    traverse. There the perms seam's own unprivileged ``getfacl`` probe ALREADY
    returns nothing and the repair ALREADY no-ops; the difference is that it now
    no-ops with a logged reason. (The reassert callers walk their targets
    shallow-first for exactly this reason: re-widening ``.openclaw`` restores the
    traverse that makes its children lstat-able in the same pass.)

    A HARD LINK at the leaf is refused on the same footing, when the leaf is a
    regular file. It needs no symlink and defeats the walk above by construction:
    it *is* a real regular file, and ``lstat`` reports the victim inode's own uid
    and mode because there is no indirection to see through. On macOS an
    unprivileged user may hard-link a file it neither owns nor can read (#3597,
    verified 2026-08-11 against root-owned 0440 ``/private/etc/sudoers``), and
    macOS is the primary pod; Linux blocks the plant under the default
    ``fs.protected_hardlinks=1``. The reachable legs are the FILE-targeted ACL
    calls — ``set_evolve_read_acl``'s ``workspace/`` retro-grant loop (which
    selects members with ``is_file()``, so it picks a hard link up as an ordinary
    member) and the ``profiles/*.md`` / secret-file ``clear_acl`` carve-outs. A
    root ``chmod +a "evolve allow …,write,delete,…"`` through one of those grants
    the service user write on the victim INODE. Directories are exempt because
    their ``st_nlink`` is structural (``2 + subdirs``) and unprivileged users
    cannot hard-link a directory at all; intermediate components are exempt for
    the same reason (a regular file mid-path just makes the command ENOTDIR).

    RESIDUAL, same as the sibling: lstat-then-subprocess is TOCTOU. This turns a
    persistent plant that fires on every hourly sweep into a race that has to be
    won against one. Closing it fully needs ``O_NOFOLLOW``-anchored fds, which
    ``setfacl``/``chmod`` do not accept.

    Raises:
        PermissionError: some component is an attacker-plantable symlink or hard
            link, or is unverifiable; the caller must not issue the privileged
            command. On the *unverifiable* case only, the exception carries
            ``.unverifiable = True`` — the seam logs that one a rung quieter,
            because a racing mask clamp can produce it benignly while a plant
            cannot. Callers must treat both as refusals.
    """
    raw = str(path)
    if ".." in Path(raw).parts:
        # ``abspath`` normalizes ``a/link/../b`` to ``a/b`` LEXICALLY while the
        # kernel resolves it PHYSICALLY through ``link`` — so a ``..`` would let
        # a component slip past the walk below. No caller of this gate builds
        # such a path (they join module constants onto a home dir), so refusing
        # costs nothing and removes the discrepancy rather than reasoning about
        # it. Checked on the raw string, before abspath erases the evidence.
        raise PermissionError(
            f"refusing privileged path {path}: contains a '..' component, which "
            f"resolves differently for the walk below than for the kernel"
        )
    abs_path = Path(os.path.abspath(raw))
    # Root → leaf. `parents` is leaf-first, so reverse it; then the leaf itself.
    for component in [*reversed(abs_path.parents), abs_path]:
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            # Nothing exists from here down, so nothing below can redirect. The
            # privileged command will fail on its own (ENOENT), which is the
            # caller's existing best-effort path — not a refusal.
            return
        except (OSError, ValueError) as e:
            # ValueError is an embedded NUL — unreachable from today's callers,
            # but this function's contract is "raises PermissionError", and an
            # escaping ValueError would crash a deploy rather than refuse a write.
            raise _unverifiable(
                f"refusing privileged path {path}: cannot verify component "
                f"{component} ({e})"
            ) from e
        if _stat.S_ISLNK(st.st_mode) and st.st_uid != 0:
            # readlink is best-effort DETAIL, not part of the decision: the link
            # can vanish between the lstat and here, and an OSError escaping a
            # gate whose whole contract is "raises PermissionError" would crash
            # a deploy instead of refusing one write.
            try:
                target = repr(os.readlink(component))
            except OSError:  # pragma: no cover — narrow race, message-only
                target = "<unreadable>"
            raise PermissionError(
                f"refusing privileged path {path}: component {component} is a "
                f"SYMLINK owned by uid {st.st_uid} → {target}; "
                f"a root setfacl/chmod would follow it"
            )
        if (component == abs_path and _stat.S_ISREG(st.st_mode)
                and st.st_nlink != 1):
            raise PermissionError(
                f"refusing privileged path {path}: it is a HARD LINK "
                f"(st_nlink={st.st_nlink}) — a root setfacl/chmod +a would put "
                f"the ACL on an inode that is also reachable under another name"
            )


# ── UTC timestamps ───────────────────────────────────────────────────────────


def now_iso() -> str:
    """UTC now as ``2026-06-10T14:23:45Z`` (seconds precision, Z suffix).

    The canonical format for new code.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso_offset() -> str:
    """UTC now as ``2026-06-10T14:23:45+00:00`` (seconds precision).

    Exists for callers whose persisted data already uses the ``+00:00``
    form — don't switch formats mid-store. New code: use ``now_iso()``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_iso_micro() -> str:
    """UTC now as ``2026-06-10T14:23:45.123456+00:00`` (microseconds).

    Exists for callers that need sub-second ordering (or whose stores
    already carry microseconds). New code: use ``now_iso()`` unless you
    genuinely order events within the same second.
    """
    return datetime.now(timezone.utc).isoformat()
