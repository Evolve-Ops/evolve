"""evolve_util — the blessed home for duplicated file/time primitives.

Phase 6.2 of docs/roadmap-80-to-100-2026-06-09.md. Before this module,
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

This module must stay stdlib-only — it sits at the bottom of the
dependency graph (analyzer and admin both import it; it imports nothing
of theirs).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_text",
    "atomic_write_json",
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
