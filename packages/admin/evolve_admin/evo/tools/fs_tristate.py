"""Errno-aware, TRI-STATE file/stat read primitive for Evo's bot-file reads.

Evo inspects bot-owned files under ``/Users/<bot>/.openclaw/`` (config,
manifests, workspace state) to ground its answers. Those homes are mode
0700 to anyone but the bot user, so a read or stat from the admin/evo
process can fail with ``EACCES``/``EPERM`` *even when the file exists*.

The hazard this module exists to kill: ``Path.exists()`` and ``os.stat``
collapse a permission denial into ``False``/``None``, which is
indistinguishable from "the file genuinely isn't there." Evo then
confabulates a wrong root cause — e.g. it reads a 0700-shielded manifest,
gets EACCES, decides "the manifest doesn't exist yet," and flips a correct
diagnosis (scanner wrote the file as the wrong user) into a wrong one.
This is the documented ``exists() lies under 0700 parents`` bug
(``feedback_exists_check_lies_under_0700_parents``), now guarded at the
tool layer.

The fix is to never fold a permission error into "absent." Every read or
stat resolves to one of THREE explicit states:

* ``exists``         — the stat/read succeeded; the file is there.
* ``absent``         — ``ENOENT``; the file is genuinely missing.
* ``indeterminate``  — ``EACCES``/``EPERM`` (or any other OSError that
                       isn't ENOENT); we *cannot determine* existence
                       because the OS denied the lookup. NOT "absent."

The same three-way split applies to the privileged ``sudo /bin/cat``
fallback: its stderr is classified from the kernel's English strerror
("No such file or directory" → ``absent``; "Permission denied" / "a
password is required" → ``indeterminate``). This reuses the same
classification the analyzer's ``home_artifacts_monitor`` already does for
``sudo cp`` stderr (the original 0700 fix, #2589) rather than re-deriving
it per call site.
"""

from __future__ import annotations

import errno
import json
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ReadState(str, Enum):
    """Tri-state outcome of a file/stat read. ``str`` mixin so the value
    serializes straight into a tool-result JSON payload (``state.value``
    is the bare string the model sees)."""

    EXISTS = "exists"
    ABSENT = "absent"
    INDETERMINATE = "indeterminate"


# errnos that mean "the OS refused to tell us whether this exists" — a
# permission wall, not an absence. Kept as a named set so the rule reads
# the same everywhere and a new permission-class errno is a one-line add.
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})


def stat_status(path: "Path | str") -> ReadState:
    """Classify a path's existence WITHOUT collapsing permission denials.

    * stat succeeds                  → ``ReadState.EXISTS``
    * ``ENOENT`` (FileNotFoundError) → ``ReadState.ABSENT``
    * ``EACCES``/``EPERM``           → ``ReadState.INDETERMINATE``
    * any other ``OSError``          → ``ReadState.INDETERMINATE``
      (e.g. EIO, ELOOP — we can't tell from outside whether the path
      exists, so we never claim it does or doesn't)

    Note: a file under a non-traversable 0700 parent raises ``EACCES`` on
    the *parent lookup*, surfacing here as ``PermissionError`` →
    ``INDETERMINATE`` — exactly the case ``Path.exists()`` gets wrong.
    """
    try:
        Path(path).stat()
        return ReadState.EXISTS
    except FileNotFoundError:
        # ENOENT is the ONLY error that proves absence. Everything else —
        # EACCES/EPERM (permission wall, incl. a non-traversable 0700
        # parent) and any other OSError (EIO, ELOOP, …) — leaves existence
        # undetermined. We never downgrade those to "absent".
        return ReadState.ABSENT
    except OSError:
        return ReadState.INDETERMINATE


def classify_privileged_stderr(returncode: int, stderr: "str | None") -> ReadState:
    """Map a ``sudo /bin/cat`` (or ``cp``) result to a tri-state.

    * ``returncode == 0``                → ``EXISTS`` (read succeeded)
    * stderr names ``No such file …``    → ``ABSENT`` (genuinely missing)
    * anything else (Permission denied,
      "a password is required" = a
      missing sudoers grant, timeout)    → ``INDETERMINATE``

    English strerror matching is safe in production: the LaunchDaemons run
    with no ``LANG`` set (C locale). The conservative default — anything
    not provably ENOENT is ``indeterminate`` — fails loud (tooling
    problem) rather than silent (false "absent"). Mirrors
    ``home_artifacts_monitor._sudo_copy_quarantine_db``.
    """
    if returncode == 0:
        return ReadState.EXISTS
    if "No such file or directory" in (stderr or ""):
        return ReadState.ABSENT
    return ReadState.INDETERMINATE


@dataclass
class ReadResult:
    """Outcome of a tri-state text read.

    ``state`` is authoritative. ``text`` is populated only when
    ``state == EXISTS``; on ``ABSENT``/``INDETERMINATE`` it is ``None`` and
    the caller MUST NOT treat that as empty content. ``detail`` is a short
    human-readable reason suitable for surfacing in a tool result so the
    model can say *why* it couldn't read (permission vs missing).
    """

    state: ReadState
    text: "str | None" = None
    detail: str = ""

    @property
    def exists(self) -> bool:
        return self.state is ReadState.EXISTS

    @property
    def indeterminate(self) -> bool:
        return self.state is ReadState.INDETERMINATE

    def as_dict(self) -> dict[str, Any]:
        """Project the read outcome (sans content) into a tool-result blob
        so the model can distinguish 'cannot read (permission)' from 'does
        not exist'. Content is intentionally omitted — callers add the
        parsed projection themselves."""
        return {"read_state": self.state.value, "read_detail": self.detail}


def read_text(
    path: "Path | str",
    *,
    sudo_fallback: bool = True,
    timeout: int = 5,
    encoding: str = "utf-8",
) -> ReadResult:
    """Read a bot file as text, tri-state.

    Direct read first (works when the evolve user holds the ``.openclaw``
    read ACL). On a permission denial, optionally fall back to
    ``sudo /bin/cat`` and classify *its* outcome — so a missing sudoers
    grant surfaces as ``indeterminate`` (tooling broken), never as a false
    ``absent``.

    The key invariant: a ``PermissionError`` is NEVER reported as
    ``absent``. If neither the direct read nor the privileged fallback can
    prove the file is missing, the result is ``indeterminate``.
    """
    p = Path(path)

    # Direct read — the common, ACL-granted path.
    try:
        return ReadResult(
            state=ReadState.EXISTS,
            text=p.read_text(encoding=encoding),
            detail="direct read succeeded",
        )
    except FileNotFoundError:
        return ReadResult(ReadState.ABSENT, None, f"{p} does not exist (ENOENT)")
    except IsADirectoryError:
        return ReadResult(
            ReadState.INDETERMINATE, None, f"{p} is a directory, not a file"
        )
    except OSError as exc:
        if exc.errno not in _PERMISSION_ERRNOS:
            # Non-permission OSError on a direct read (EIO, etc.). Can't
            # prove absence; don't lie.
            if not sudo_fallback:
                return ReadResult(
                    ReadState.INDETERMINATE, None, f"{p}: {exc.strerror or exc}"
                )
        # Permission wall (or any error when fallback is on) → try sudo.

    if not sudo_fallback:
        return ReadResult(
            ReadState.INDETERMINATE,
            None,
            f"{p}: permission denied and sudo fallback disabled",
        )

    # Privileged fallback. Full path to /bin/cat per the sudoers grant.
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ReadResult(
            ReadState.INDETERMINATE, None, f"{p}: sudo /bin/cat timed out"
        )
    except OSError as exc:
        return ReadResult(
            ReadState.INDETERMINATE, None, f"{p}: sudo /bin/cat failed: {exc}"
        )

    state = classify_privileged_stderr(r.returncode, r.stderr)
    if state is ReadState.EXISTS:
        return ReadResult(ReadState.EXISTS, r.stdout, "sudo /bin/cat succeeded")
    if state is ReadState.ABSENT:
        return ReadResult(ReadState.ABSENT, None, f"{p} does not exist (ENOENT)")
    return ReadResult(
        ReadState.INDETERMINATE,
        None,
        f"{p}: cannot determine existence — {(r.stderr or '').strip() or 'permission denied'}",
    )


def read_json(
    path: "Path | str",
    *,
    sudo_fallback: bool = True,
    timeout: int = 5,
) -> "tuple[ReadResult, Any | None]":
    """``read_text`` + JSON parse. Returns ``(ReadResult, parsed | None)``.

    On a successful read that fails to parse, the ``ReadResult`` stays
    ``EXISTS`` (the file IS there) but the parsed value is ``None`` and the
    detail explains the parse failure — distinct from absent/indeterminate.
    """
    res = read_text(path, sudo_fallback=sudo_fallback, timeout=timeout)
    if res.state is not ReadState.EXISTS or res.text is None:
        return res, None
    try:
        return res, json.loads(res.text)
    except json.JSONDecodeError as exc:
        return (
            ReadResult(ReadState.EXISTS, res.text, f"{path}: invalid JSON: {exc}"),
            None,
        )
