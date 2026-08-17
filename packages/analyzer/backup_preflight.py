"""backup_preflight — estimate the next backup's size + composition.

Phase 4d of the backup architecture rework
(spec docs/spec-backup-and-data-classification-2026-05-28.md
§"Phase 4 polish: Pre-flight backup size estimate").

Walks a bot's workspace and reports how much of it is classified each
way. The Cloud subtab calls this so the operator can sanity-check what
will get pushed *before* clicking "Backup now" — especially useful for
verifying that classification changes in the Data tab did what they
expected.

The walk is filesystem-direct (not git-based) so it accurately reflects
what ``git add -A`` would stage from the working tree, untracked
files included. The ``.git`` directory is skipped — it's never part of
a backup commit and walking it would inflate counts.

Hard cap at 100k files keeps responses snappy on pathological
workspaces. When the cap fires, ``truncated=true`` signals the UI to
show "~" rather than an exact number.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from data_classification import (
    PRIVACY_CLOUD,
    PRIVACY_EPHEMERAL,
    PRIVACY_LOCAL,
    ClassificationResolver,
)

# Hard cap on file count. Bigger than any realistic bot workspace; smaller
# than the kind of pathological case (terabyte of small files) that would
# make this hit a timeout. The UI shows "~X files" when this fires.
MAX_FILES_WALKED = 100_000

# Directories never walked, regardless of classification. ``.git`` is
# never in a backup commit. ``evolve-backup`` is the built-in ephemeral
# staging area — included in the count would just double the estimate
# every cycle.
_NEVER_WALK_DIRS = frozenset({".git", "evolve-backup"})


def _empty_bucket() -> dict[str, int]:
    return {"files": 0, "bytes": 0}


def compute_preflight_summary(
    workspace: Path,
    resolver: ClassificationResolver,
    *,
    max_files: int = MAX_FILES_WALKED,
) -> dict[str, Any]:
    """Walk ``workspace`` and return a classification × {files, bytes} summary.

    Output shape:

      {
        "by_classification": {
          "cloud":     {"files": int, "bytes": int},
          "local":     {"files": int, "bytes": int},
          "ephemeral": {"files": int, "bytes": int},
        },
        "total":     {"files": int, "bytes": int},
        "walked_files":   int,    # total files visited (== sum of buckets unless errors)
        "truncated":      bool,   # hit max_files
        "skipped_errors": int,    # paths whose stat failed
      }

    ``workspace`` must exist; an empty-or-missing dir returns the
    zero-filled shape. Symlinks are not followed (``os.walk`` default).
    """
    by_class: dict[str, dict[str, int]] = {
        PRIVACY_CLOUD:     _empty_bucket(),
        PRIVACY_LOCAL:     _empty_bucket(),
        PRIVACY_EPHEMERAL: _empty_bucket(),
    }
    walked = 0
    skipped_errors = 0
    truncated = False

    if not workspace.exists() or not workspace.is_dir():
        return _result(by_class, walked, truncated, skipped_errors)

    for root, dirs, files in os.walk(workspace):
        # Mutate ``dirs`` in place so os.walk doesn't descend into excluded
        # subtrees. Matching is by basename — equally affects every depth.
        dirs[:] = [d for d in dirs if d not in _NEVER_WALK_DIRS]

        for fname in files:
            if walked >= max_files:
                truncated = True
                break
            full = Path(root) / fname
            try:
                size = full.stat().st_size
            except OSError:
                skipped_errors += 1
                continue
            try:
                rel = full.relative_to(workspace)
            except ValueError:
                skipped_errors += 1
                continue
            classification = resolver.classify(str(rel))
            bucket = by_class.get(classification)
            if bucket is None:
                # Unknown classification — defensive; should never happen
                # because the resolver only returns one of three values.
                skipped_errors += 1
                continue
            bucket["files"] += 1
            bucket["bytes"] += size
            walked += 1
        if truncated:
            break

    return _result(by_class, walked, truncated, skipped_errors)


def _result(
    by_class: dict[str, dict[str, int]],
    walked: int,
    truncated: bool,
    skipped: int,
) -> dict[str, Any]:
    total_files = sum(b["files"] for b in by_class.values())
    total_bytes = sum(b["bytes"] for b in by_class.values())
    return {
        "by_classification": by_class,
        "total":              {"files": total_files, "bytes": total_bytes},
        "walked_files":       walked,
        "truncated":          truncated,
        "skipped_errors":     skipped,
    }
