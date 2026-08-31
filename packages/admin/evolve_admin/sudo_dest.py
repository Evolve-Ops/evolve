"""Refusal gate for deploy.py's root-mediated writes to bot-owned paths.

#3566 audit **D-2, remainder**. ``evolve_util.assert_safe_sudo_dest`` is the
primitive: it refuses a destination a non-root user can redirect — a symlink, a
symlinked/non-dir parent, a non-regular dest, or a dest that cannot be
``lstat``-ed at all — *before* a root ``cp`` / ``chown`` / ``chmod`` follows it.
#3597 wired that primitive into the chown/chmod legs inside
``secret_config_perms``. This module wires it into the strictly LARGER primitive
sitting beside them in ``deploy.py``: the ``cp`` that writes file CONTENT as
root, and the ``chown`` that hands ownership away permanently. Gating only the
trailing ``chmod`` refuses *after* the damage.

**HARD LINKS ARE NOT COVERED ON THIS BRANCH — read this before believing the
threat model below.** A hard link is a real regular file: ``S_ISREG`` passes,
``S_ISLNK`` does not fire, and ``lstat`` reports the victim inode's own metadata
because there is no indirection to see through. The ``st_nlink != 1`` check that
closes it is added to the shared primitive by **#3597**, which is open and NOT
in this branch's base; it is deliberately not duplicated here (same-aspect
duplicate-PR collision, and the primitive should have one owner). Until #3597
lands, macOS — which has no ``fs.protected_hardlinks`` equivalent, and is the
primary pod — keeps the persistent every-deploy plant via ``ln`` at every site
gated here. **Merge #3597 first.** Every call site below inherits that leg for
free the moment it does, with no further change.

The threat is not hypothetical positioning: the bot OWNS ``~/.openclaw`` and its
``workspace/``, so it can replace ``openclaw.json`` (or ``AGENTS.md``, or
``POD_CONDUCT.md``) with a link at any time, and every one of these writers runs
on a schedule against that exact path.

**Why a module rather than a helper inside deploy.py.** deploy.py is a frozen
hot-hazard file under a no-growth cap (``tools/file-size-baseline.txt``). The
gate has to cost its call sites ~2 lines each and nothing more.

**Why not inside** ``secret_config_perms``. That module's contract is "which
files are secret and how 0600 is enforced". Its own wrapper (#3597's
``_sudo_dest_ok``) returns a bare bool because its callers are best-effort
repairs whose return value is largely ignored — ``chmod_secret_config`` is
called for effect at ~8 sites. deploy.py's writers are the opposite: each one
already carries a failure channel with an operator on the other end — a
``(bool, str)`` tuple, a ``DeployResult.error``, a printed abort — so the reason
is worth returning, not discarding.

**Raise vs return.** Neither, deliberately: the gate is a value here, not an
exception. Raising would push every call site into a ``try/except`` (line cost
against the cap) and, worse, would land the refusal in surrounding
``except Exception`` blocks tuned for *subprocess* failure, where it would be
reported as "cp failed" rather than "destination is attacker-controlled".
Returning ``(False, reason)`` lets each site fail the way it already fails,
naming the real cause. This differs from #3597's fork only in the shape of the
value, not the principle — and loudness does not depend on either choice:
**every refusal is logged at ERROR here before it is returned**, so a refusal is
on the record even at a call site whose local channel is only a ``print``.
"""

from __future__ import annotations

import errno
import os
import stat as _stat
from pathlib import Path

from evolve_util import assert_no_symlink_in_path, assert_safe_sudo_dest

from .runtime import get_perms as _get_perms
from .telemetry import get_logger

__all__ = ["redirect_refusal", "sudo_dest_refusal"]

# Bounded so a pathological path cannot march the repair walk to "/", and so a
# tree that never becomes visible cannot spin on privileged setfacl calls.
_MAX_REPAIR_ROUNDS = 6
_MAX_ANCESTOR_WALK = 8

_log = get_logger(__name__)


def sudo_dest_refusal(*paths: "str | Path") -> str:
    """Check every destination a root ``cp``/``chown``/``chmod`` is about to touch.

    Returns ``""`` (falsy) when all of ``paths`` are safe destinations, else the
    **reason** the first unsafe one was refused. A caller that gets a non-empty
    string MUST issue no privileged command at all — not the ``cp``, not the
    ``chown``, not the backup.

    The truthy-is-the-reason shape is for the call sites, which live in
    ``deploy.py`` under a no-growth cap: ``if why := sudo_dest_refusal(p):`` is
    one line and still carries the reason into whatever channel the caller
    already has. A caller with no channel of its own can drop the reason and
    just bail — the ERROR log below has already recorded it with the path.

    Pass **every** path in the privileged sequence, not just the headline one: a
    backup destination is as much an arbitrary-root-write sink as the config it
    backs up, and when the *source* of a root ``cp`` is itself bot-controlled, a
    link there turns the backup into an arbitrary root READ (root reads the
    victim, writes it somewhere the bot can read).

    Re-call it between a ``cp`` and the ``chown``/``chmod`` that repairs the
    mode — the primitive's own docstring asks callers to. A ``cp`` through a
    symlink leaves the LINK in place, so a plant that lands inside the write
    window is still sitting there for the ``chown`` to follow, and the ``chown``
    leg is the permanent one.

    TOCTOU is inherent and documented on the primitive: an ``lstat`` followed by
    a subprocess cannot be made atomic while the privileged tools are
    ``cp``/``chown``/``chmod`` (none accept an ``O_NOFOLLOW``-anchored fd). What
    this converts is a **persistent** plant that fires on every deploy into a
    race an attacker has to win against a single write.

    **The ACL clamp, and why EACCES gets one retry.** The ``lstat`` runs as
    ``evolve``, but several callers here deliberately work on paths ``evolve``
    cannot read directly — they fetch content with ``sudo /bin/cat`` precisely
    because a bot-user process that hardens ``.openclaw`` to 0700 caps evolve's
    traverse ACE on Linux (``feedback_exists_check_lies_under_0700_parents``).
    Fail-closed on that EACCES would take every one of those writers offline on
    the VPS pod the first time the gateway re-clamped — a self-inflicted outage,
    not a defence. So an ``EACCES``/``EPERM`` is treated as "we cannot see it
    *yet*": re-widen the mask once (``reassert_mask``, the same repair
    ``heal_evolve_access`` runs; no-op on macOS) and re-check.

    That retry repairs **our own** ability to look — it grants nothing to
    anybody else and does not touch the destination. If the look still fails,
    the refusal stands. A SYMLINK / non-regular verdict never retries: those are
    answers, not blindness.

    **The repair walks UP, because the clamp is usually above the parent.** The
    dir the gateway hardens is ``.openclaw``, but half these destinations live a
    level deeper (``workspace/AGENTS.md``, ``workspace/procedures/x.md``). Under
    the clamp it is the *parent's own* lstat that EACCESes, so repairing only
    ``path.parent`` — the first version of this code — provably never fired for
    those, and they failed closed on every clamped deploy. Worst at
    ``inject_session_surface_check``, whose ``sudo /bin/cat`` fallback exists
    *specifically* to survive that state: the gate would have refused on the same
    EACCES the fallback had just worked around, making it dead code. So the
    repair targets ``_verified_ancestor`` — the deepest ancestor we can actually
    ``lstat`` and confirm is a real directory — and re-checks, deepening each
    round as the widened mask exposes the next level down.

    **Every repair target is verified first**, because ``reassert_mask`` is a
    privileged ``setfacl`` and ``setfacl`` FOLLOWS symlinks. A bot owns its own
    home, so it can make ``.openclaw`` a symlink AND ``chmod 0700 ~`` so evolve
    cannot lstat far enough to notice; repairing blind would widen an ACL mask on
    whatever the link named. ``_verified_ancestor`` therefore stops dead at the
    first ancestor that is not a real directory rather than walking past it, and
    the walk is depth-bounded so a pathological path cannot march it to ``/``.
    RESIDUAL, stated rather than papered over: this is still
    lstat-then-subprocess, so a bot that wins the race between the two can steer
    that ``setfacl`` at a directory of its choice. The payoff is an ACL-mask
    widening — not content, not ownership — and it needs a target that already
    carries a capping mask.

    A caller that already knows it is under a clamp should still re-widen before
    calling, so the common path does not spend a doomed ``lstat`` first — see
    ``deploy.safe_write_bot_config``, where ``openclaw config validate`` has just
    run as the bot user and the clamp is guaranteed rather than possible.
    """
    for path in paths:
        reason = _refusal_for(Path(path))
        if reason:
            _log.error("D-2 gate refused a root-mediated write: %s", reason)
            return reason
    return ""


def redirect_refusal(*paths: "str | Path") -> str:
    """``sudo_dest_refusal``'s DIRECTORY-shaped sibling: refuse a path whose
    resolution a bot can redirect, without asserting anything about its shape.

    Same truthy-is-the-reason contract and the same call-site idiom
    (``if why := redirect_refusal(p):``) — what differs is the primitive
    underneath, and that difference is the whole reason this exists.
    ``sudo_dest_refusal`` wraps ``assert_safe_sudo_dest``, which also asserts a
    SHAPE: absent, or a regular file with ``st_nlink == 1``. That is exactly
    wrong for the destinations gated here — ``.openclaw/credentials``,
    ``workspace/evolve``, ``workspace/.git`` — which are directories whose whole
    purpose is to have children. Pointing the file-shaped gate at them refuses
    every legitimate call, which is precisely why #3602 descoped these sites
    ("D-2 KNOWN-UNGATED, descoped: dir-shaped dest") rather than mis-gating them.

    So this wraps ``evolve_util.assert_no_symlink_in_path`` instead — the
    primitive #3605 built for the perms seam, which asserts ONE thing about
    EVERY component (nothing on the way here redirects) and nothing at all about
    what the leaf is. No fourth ancestor-walk variant is introduced: the walk
    lives in that primitive, which the seam already shares.

    **Why the call sites need it even though the perms seam is gated.** #3605
    closed the ACL emitters (``setfacl`` / ``chmod +a`` / ``chmod -N``), but the
    sibling ``mkdir`` / ``chown`` / ``chmod 700`` / ``touch`` legs interleaved
    with them in ``_apply_openclaw_read_contract`` shell out from deploy.py
    directly and never touch the seam. The sharpest instance:
    ``perms.clear_acl(creds_dir)`` REFUSES a symlinked ``credentials`` and logs
    that a root chmod would follow it — and the next statement issued exactly
    that root ``chmod 700``. Reachable from ``heal_evolve_access`` → the hourly
    reassert, ``oc_auth_store``'s EACCES recovery, forge, and every deploy.

    **No EACCES repair loop, deliberately** — the opposite choice from
    ``sudo_dest_refusal``, because the availability argument that justifies one
    there does not apply here. Each gated site is already skipped or already
    refused under a live clamp: the ``mkdir``/``chown`` pair sits behind
    ``exists_or_unreachable`` (EACCES reads as PRESENT → the block is skipped),
    the ``credentials`` legs behind a ``.exists()`` whose raise is caught by the
    enclosing ``except OSError``, and the write grant that follows is
    seam-gated and refuses on the same EACCES. Adding a privileged repair
    ``setfacl`` here would therefore buy no availability and would widen the
    residual for nothing. Note also that ``_apply_openclaw_read_contract`` runs
    the recursive read grant FIRST, so a gateway clamp is normally already
    re-widened by the time these run.
    """
    for path in paths:
        try:
            assert_no_symlink_in_path(path)
        except PermissionError as exc:
            reason = str(exc)
            _log.error("D-2 gate refused a root-mediated write: %s", reason)
            return reason
    return ""


def _refusal_for(path: Path) -> str:
    """``""`` if ``path`` is a safe dest, else the reason — repairing our own
    view of a clamped tree, and only that, before giving up."""
    exc = _look(path)
    if exc is None:
        return ""
    if not _is_unverifiable(exc):
        return str(exc)
    repaired: "set[Path]" = set()
    for _ in range(_MAX_REPAIR_ROUNDS):
        anchor = _verified_ancestor(path)
        if anchor is None or anchor in repaired:
            break  # nothing safe left to widen, or the last round made no progress
        repaired.add(anchor)
        try:
            _get_perms().reassert_mask(anchor)
        except Exception as exc2:  # noqa: BLE001 — best-effort; the re-check decides
            _log.warning("D-2 gate: mask repair on %s raised %s", anchor, exc2)
            break
        exc = _look(path)
        if exc is None:
            return ""
        if not _is_unverifiable(exc):
            break  # visible now, and the answer is a refusal
    return str(exc)


def _look(path: Path) -> "PermissionError | None":
    try:
        assert_safe_sudo_dest(path)
        return None
    except PermissionError as exc:
        return exc


def _verified_ancestor(path: Path) -> "Path | None":
    """Deepest ancestor of ``path`` we can ``lstat`` AND confirm is a real dir.

    ``lstat``, never ``is_dir()`` — the latter follows, which is the whole
    hazard. Returns None when an ancestor turns out to be a symlink or a
    non-directory (never point a privileged ``setfacl`` at it), when nothing up
    the chain is readable, or when the walk exceeds ``_MAX_ANCESTOR_WALK``.
    """
    cur = path.parent
    for _ in range(_MAX_ANCESTOR_WALK):
        try:
            st = os.lstat(cur)
        except OSError:
            if cur.parent == cur:
                return None
            cur = cur.parent
            continue
        if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
            return None
        return cur
    return None


def _is_unverifiable(exc: PermissionError) -> bool:
    """True when the refusal was "cannot look", not "looked and it is bad".

    ``assert_safe_sudo_dest`` raises ``from`` the underlying ``OSError`` only on
    its two unverifiable branches, so the cause's errno is an exact signal —
    no message-string matching, which would rot the moment the primitive is
    reworded.
    """
    cause = exc.__cause__
    return isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM)
