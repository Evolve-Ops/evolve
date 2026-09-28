"""Canonical workspace-relative path keys — the single home for the
``ws_rel_key`` contract.

A path-derived join/index key must use the **same canonical form on both
sides** or the join silently misses (registry side vs disk-walk side). The
historical regressions all share this shape:

  - #3303 — the recon-ledger join keyed one side via CWD-sensitive
    ``Path.resolve()`` on a workspace-relative path (the
    ``ai.evolve.evolve.admin-ui`` daemon sets no ``WorkingDirectory`` → CWD
    is ``/``), so ``scripts/foo.py`` became ``/scripts/foo.py`` and never
    matched a real absolute → "atlas 0-owned / 40-false-missing".
  - F-C1 — three ``scanner.py`` joins compared a workspace-relative disk path
    against raw manifest ``path`` strings (workspace-relative from
    migrate_v7/v13, but **absolute** from ``extend_application``) → a false
    ``unregistered_script`` finding, a duplicate stamp entry, and dropped
    misplaced-secret owner attribution.

The fix each time: funnel both sides through :func:`ws_rel_key`.

This module deliberately has **no dependency** on ``recon_ledger`` /
``scanner`` / ``app_ownership_policy`` so any consumer can import it without
the ``recon_ledger → app_ownership_policy → scanner`` import cycle. It is the
F-C2 "one canonicalizer home" cleanup, done minimally (only ``ws_rel_key`` is
lifted; ``sync._abs`` / ``manifest_hygiene._normalize_path`` — which canonicalize
to the *absolute* form — are left for the optional full F-C2 collapse).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ws_rel_key"]


def ws_rel_key(path_str: str, workspace: Path) -> str:
    """Canonical **workspace-relative** join key, computed purely LEXICALLY.

    Both join sides funnel through here so the registry
    (``realized_files[].path`` / ``files[].path`` — workspace-relative from
    migrate_v7/v13, or absolute from extend_application) and the disk walk
    (always absolute, under *workspace*) meet on the same key.

    The key is derived WITHOUT ``Path.resolve()`` / ``os.getcwd()`` / any other
    CWD-dependent op on a *relative* input. That is the whole point: the
    ``ai.evolve.evolve.admin-ui`` LaunchDaemon sets no ``WorkingDirectory`` so
    its CWD is ``/``; resolving a workspace-relative path against it produced
    ``/scripts/...`` and collapsed the registry↔disk join — the #3303
    "atlas 0-owned / 40-false-missing" regression.

      - workspace-relative input → normalized lexically (strip ``./``; collapse
        ``..`` via ``os.path.normpath``); NEVER resolved against CWD.
      - absolute input under *workspace* → reduced to its workspace-relative
        form. The workspace root is tried both as-given AND
        symlink-canonicalized, because a stored absolute path may have been
        recorded through ``/private`` (macOS) or a symlinked home while the
        live workspace handle is not. Reducing an *absolute* path is
        CWD-independent, so this does not reintroduce the #3303 bug.
      - absolute input NOT under *workspace* (foreign tree) → kept as a
        normalized absolute, which by construction matches no disk file in this
        workspace → the row honestly falls to ``missing_file`` /
        ``unregistered``.
    """
    p = (path_str or "").strip()
    if not p:
        return ""
    if not os.path.isabs(p):
        # Workspace-relative already — normalize LEXICALLY (never resolve()/CWD).
        return os.path.normpath(p)
    norm = os.path.normpath(p)
    # Try the workspace root both as-given and symlink-canonicalized (the macOS
    # /private case). resolve() on the workspace DIR is CWD-independent.
    roots = [
        os.path.normpath(str(workspace)),
        os.path.normpath(str(workspace.resolve())),
    ]
    for root in roots:
        try:
            return str(Path(norm).relative_to(root))
        except ValueError:
            continue
    return norm
