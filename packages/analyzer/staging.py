"""staging — TOCTOU-safe /tmp staging for sudo-cp writes (analyzer side).

Mirror of ``evolve_admin.deploy._secure_stage`` (roadmap 0.5 / 2.10). The
analyzer's heal/apply paths run in slim subprocess contexts where the admin
package may not be importable, so the helper lives here rather than being
imported across the package boundary; Phase 6.2's shared util library is the
long-term single home for both copies.

Why this exists: temporary files named like ``/tmp/evolve-<bot>-<purpose>.json``
are a local TOCTOU/symlink privilege-escalation surface on a multi-user box —
``/tmp`` is world-writable and ``cp`` follows symlinks, so an attacker could
pre-create the path (or swap it between our write and the root ``cp``) and have
root copy attacker content into a bot-owned file. ``mkstemp`` defeats both
halves: it opens with ``O_EXCL`` (the path cannot be pre-created) and uses an
unguessable random suffix.

The ``evolve-stage-`` prefix keeps the staged names inside the existing
sudoers source globs (``/tmp/evolve-*.json`` / ``/tmp/evolve-*.md``), so no
grant changes are needed at call sites.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def secure_stage(content: str, *, suffix: str = ".json", mode: int = 0o600) -> Path:
    """Stage ``content`` into an unpredictable, exclusively-created /tmp file.

    Default mode 0600 (root ``cp`` can always read it); pass 0644 only when a
    bot-user subprocess must read the staged file before the copy. Caller is
    responsible for unlinking the returned path.
    """
    fd, name = tempfile.mkstemp(dir="/tmp", prefix="evolve-stage-", suffix=suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(name, mode)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)
