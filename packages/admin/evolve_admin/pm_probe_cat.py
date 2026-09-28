#!/usr/bin/python3 -I
"""pm-probe-cat — the ONE privileged reader behind the PM's read-only pod probe.

Brief: internal/dispatch/done/pm-mini-probe-readonly-mcp.md.
Client: ``tools/pm-mini-probe`` (the stdio MCP server on the operator's laptop).
Installed to :data:`INSTALL_PATH` by ``evolve-admin install-pm-probe-sudoers``
and kept there, root-owned and 0755, by ``evolve-admin ensure-pod-perms``.

Why this file exists instead of a ``sudo /bin/cat`` grant
--------------------------------------------------------
The first cut of the probe's sudoers file granted ``/bin/cat`` on a "depth
ladder" of path patterns (``{root}/*``, ``{root}/*/*``, …) and claimed the
ladder bounded the grant. **It does not.** sudoers(5) § Wildcards: ``/`` is
excluded from wildcard matching only in the *file name portion of the
command*; command-line ARGUMENTS are matched as one space-separated string,
where ``*`` crosses ``/`` and spaces alike. The man page's own example says
so: ``/bin/cat /var/log/messages*`` also admits
``cat /var/log/messages /etc/shadow``. So a single ladder rung was already
passwordless root read of every file on the pod for anyone holding the admin
account, and ``sudo cat`` follows symlinks — a bot user who can write
``~/.openclaw/`` or ``/tmp/pm`` could plant a link and route any file on the
box into the PM's context.

sudo cannot express "one operand, under these roots, no symlinks". A
fixed-argv program can, so that is what the grant names. The sudoers line
carries **no argument specification at all** (which in sudoers means "any
arguments"), and every constraint that used to be pretended by the wildcards
is enforced here, as root, after resolution:

1. exactly one operand, and it may not look like a flag;
2. absolute, no ``..`` segment, no NUL;
3. ``realpath`` FIRST, then the resolved path must match :data:`ROOT_PATTERNS`
   — resolution before the root check is what makes a planted symlink useless
   rather than load-bearing;
4. the resolved path is re-opened one component at a time with ``O_NOFOLLOW``
   (and ``O_DIRECTORY`` on every component but the last), so a component
   swapped for a symlink between the ``realpath`` and the ``open`` fails
   closed instead of racing us into ``/etc``;
5. the leaf must be a regular file (a FIFO would hang root; a device would
   read the machine, not a file), and it must have exactly one link — a hard
   link is a second name for an inode, so it reopens the bot-to-root channel
   the symlink refusals close, with no link for ``realpath`` or ``O_NOFOLLOW``
   to refuse;
6. credential-shaped operands are refused outright (:data:`SECRET_SEGMENT_GLOBS`),
   so the masking on the laptop is a second net rather than the only one;
7. output is capped at :data:`MAX_OUTPUT_BYTES`, and a truncated read says so
   on stderr rather than silently returning a prefix.

Exit codes: ``0`` read (possibly truncated, flagged on stderr), ``2`` refused
(the reason names the rule), ``1`` an I/O error.

:data:`ROOT_PATTERNS` is EMPTY in this file and rendered in at install time by
``pm_probe_install.render_wrapper()`` from ``platform_profile.get_profile()``
— one home for platform divergence, and an unrendered or tampered copy grants
nothing at all rather than defaulting to something. The installed artifact is
this file with that one line rewritten; nothing else differs.

Stdlib only, no imports from the Evolve checkout: this runs as root, and the
checkout is writable by the ``evolve`` service user.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import re
import stat
import sys

# Matched against the operand AFTER realpath, anchored whole-string.
# Rendered at install time — see the module docstring.
ROOT_PATTERNS: tuple[str, ...] = ()

# 64 KB, the same cap the probe applies on the laptop. Enough for a turns-log
# slice or a census table; small enough that a runaway read cannot flood the
# PM's context or the laptop's memory.
MAX_OUTPUT_BYTES = 64 * 1024

# Credential-shaped path segments. Refused before the file is opened, so no
# key bytes are read at all rather than read-then-masked. Keep in step with
# ``SECRET_SEGMENT_GLOBS`` in tools/pm-mini-probe (a test pins them equal).
SECRET_SEGMENT_GLOBS: tuple[str, ...] = (
    "auth*", "credentials*", ".env*", "*.pem", "*.key", "id_*",
)

# A directory pair that means "private by construction" wherever it appears.
SECRET_DIR_PAIR: tuple[str, str] = ("docs", "private")

INSTALL_PATH = "/usr/local/libexec/pm-probe-cat"
INSTALL_DIR = "/usr/local/libexec"
PROG = "pm-probe-cat"


class Refused(Exception):
    """The operand did not survive the contract. ``rule`` names what caught it."""

    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


def _root_res() -> tuple[re.Pattern[str], ...]:
    """Compile :data:`ROOT_PATTERNS` as anchored whole-path matchers."""
    return tuple(re.compile(rf"^{p}$") for p in ROOT_PATTERNS)


def check_secret_path(path: str) -> None:
    """Refuse a credential-shaped path. Raises :class:`Refused`."""
    segs = [s for s in path.split("/") if s]
    for seg in segs:
        low = seg.lower()
        for glob in SECRET_SEGMENT_GLOBS:
            if fnmatch.fnmatchcase(low, glob):
                raise Refused(
                    "secret-path",
                    f"{seg!r} matches {glob!r} — credential-shaped files are not "
                    f"readable through this door, masked or not",
                )
    for a, b in zip(segs, segs[1:]):
        if (a.lower(), b.lower()) == SECRET_DIR_PAIR:
            raise Refused("secret-path", f"{'/'.join(SECRET_DIR_PAIR)}/ is private by construction")


def resolve_operand(operand: str) -> str:
    """Vet one operand and return its resolved absolute path.

    Raises :class:`Refused` for a flag-shaped, relative, traversing,
    credential-shaped, missing or out-of-root operand. The root check runs
    on the RESOLVED path, which is the half the sudoers ladder could not do.
    """
    if not operand:
        raise Refused("operand-empty", "no path given")
    if operand.startswith("-"):
        raise Refused("operand-flag", f"{operand!r} looks like a flag; this reader takes one path")
    if "\0" in operand:
        raise Refused("operand-nul", "the path contains a NUL byte")
    if not operand.startswith("/"):
        raise Refused("relative-path", f"{operand!r} is not an absolute path")
    if any(seg == ".." for seg in operand.split("/")):
        raise Refused("path-traversal", f"{operand!r} contains a '..' segment")
    check_secret_path(operand)
    # The `strict` keyword of `os.path.realpath` is 3.10+, and the pod runs macOS
    # Command Line Tools' interpreter — 3.9.6 — under this file's own shebang, so
    # the keyword was a TypeError on the one machine the grant exists for: every
    # `sudo cat` through this door tracebacked before it refused anything. The
    # 3.9-safe equivalent is non-strict resolution plus an explicit existence
    # check, which is what that keyword does internally, and it is used on every
    # interpreter rather than branching on the version — one code path is one
    # thing to review.
    #
    # Nothing is weakened by resolving non-strictly: a path that does not exist,
    # a dangling final symlink and an unresolvable component all fail the `lstat`
    # below and REFUSE here, and a symlink loop that non-strict resolution leaves
    # in place is caught by the O_NOFOLLOW walk in `open_no_follow`. Both
    # directions fail closed.
    resolved = os.path.realpath(operand)
    try:
        os.lstat(resolved)
    except OSError as exc:
        raise Refused("no-such-file", f"{operand!r} could not be resolved: {exc.strerror}") from exc
    check_secret_path(resolved)
    if not any(rx.match(resolved) for rx in _root_res()):
        raise Refused(
            "path-outside-allowlist",
            f"{operand!r} resolves to {resolved!r}, which is not under an allowed root",
        )
    return resolved


def _is_symlink_at(dir_fd: int, name: str) -> bool:
    """True when ``name`` inside ``dir_fd`` is a symbolic link."""
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


def open_no_follow(resolved: str) -> int:
    """Open ``resolved`` component by component, refusing a symlink at any one.

    ``resolved`` came out of ``realpath``, so it contains no symlink at the
    moment it was produced; walking it with ``O_NOFOLLOW`` proves that is
    still true at the moment we open it. Returns the leaf fd (caller closes).
    """
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parts = [p for p in resolved.split("/") if p]
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last:
                flags |= os.O_DIRECTORY
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                # A symlink refused by O_NOFOLLOW surfaces as ELOOP, or as
                # ENOTDIR when O_DIRECTORY is also set (a link is not a dir).
                # lstat tells the two apart, so the refusal names what happened.
                if _is_symlink_at(fd, part):
                    raise Refused(
                        "symlink",
                        f"{part!r} in {resolved!r} is a symbolic link — this reader "
                        f"never follows one",
                    ) from exc
                if exc.errno == errno.ENOTDIR:
                    raise Refused("not-a-directory", f"{part!r} in {resolved!r} is not a directory") from exc
                raise Refused("unreadable", f"{resolved!r}: {exc.strerror}") from exc
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_capped(fd: int) -> tuple[bytes, bool]:
    """Read at most :data:`MAX_OUTPUT_BYTES` + 1 and report whether it overflowed."""
    chunks: list[bytes] = []
    got = 0
    while got <= MAX_OUTPUT_BYTES:
        chunk = os.read(fd, MAX_OUTPUT_BYTES + 1 - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_OUTPUT_BYTES:
        return data[:MAX_OUTPUT_BYTES], True
    return data, False


def main(argv: list[str], out=None, err=None) -> int:
    """Read one allowlisted file. See the module docstring for the contract."""
    out = out if out is not None else sys.stdout.buffer
    err = err if err is not None else sys.stderr
    if len(argv) != 1:
        print(
            f"{PROG}: operand-count: expected exactly one path, got {len(argv)}",
            file=err,
        )
        return 2
    try:
        resolved = resolve_operand(argv[0])
        fd = open_no_follow(resolved)
    except Refused as exc:
        print(f"{PROG}: {exc}", file=err)
        return 2
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            print(f"{PROG}: not-a-regular-file: {resolved!r} is not a regular file", file=err)
            return 2
        # A hard link reopens the channel the symlink refusal closes. Refusing a
        # symlink stops a bot-writable tree POINTING at a root-only file; it does
        # nothing about a second NAME for that file's inode, which `ln` creates
        # inside the allowlisted tree with no link to follow and nothing for
        # `realpath` or `O_NOFOLLOW` to see. The check has to be on the inode, and
        # after `fstat` is where the inode is in hand.
        #
        # `st_nlink > 1` is the whole rule, deliberately: it refuses the extra
        # name rather than trying to decide which of the names is the legitimate
        # one, because that decision needs the directory the OTHER name lives in
        # and root is not going looking. A config or log with two names is rare
        # and reads fine through its own path; a planted link never does.
        if st.st_nlink > 1:
            print(
                f"{PROG}: hardlink: {resolved!r} has {st.st_nlink} links; a second "
                f"name for an inode is the symlink channel without the symlink",
                file=err,
            )
            return 2
        data, truncated = read_capped(fd)
    except OSError as exc:
        print(f"{PROG}: unreadable: {resolved!r}: {exc.strerror}", file=err)
        return 1
    finally:
        os.close(fd)
    out.write(data)
    flush = getattr(out, "flush", None)
    if flush is not None:
        flush()
    if truncated:
        print(
            f"{PROG}: truncated at {MAX_OUTPUT_BYTES} bytes ({st.st_size} on disk)",
            file=err,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via the rendered artifact
    sys.exit(main(sys.argv[1:]))
