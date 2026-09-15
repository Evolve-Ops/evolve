"""pod_baseline.store — load/save {shared_dir}/pod-baseline.json.

Q1 (decided 2026-08-15, spec-pod-plane §Question ledger): the baseline lives
in its own file — evolve-owned, atomic temp+rename write, ``schema_version``
field, single writer. NOT a network.json block (crowded multi-writer surface
with a clobber-prone save path).

The file lives under ``{shared_dir}`` which the evolve user owns — plain
reads, no sudo. Writes go through ``evolve_util.atomic_write_json`` with
``sort_keys=True`` (stable diffs) and an explicit 0o644 (mkstemp's default
0o600 would silently lock out cross-user readers — see
feedback_tempfile_rename_carries_0600_onto_dest).
"""
from __future__ import annotations

import json
from pathlib import Path

from evolve_util import atomic_write_json

from pod_baseline.schema import PodBaseline

BASELINE_FILENAME = "pod-baseline.json"


def baseline_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / BASELINE_FILENAME


def load_baseline(shared_dir: Path) -> "PodBaseline | None":
    """Return the pod baseline, or None if the file does not exist.

    A present-but-corrupt file raises ValueError — the operator must know
    the baseline is broken rather than seeing an all-drift census.
    """
    path = baseline_path(shared_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"pod-baseline unreadable at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pod-baseline corrupt at {path}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"pod-baseline corrupt at {path}: top level is not an object")
    return PodBaseline.from_dict(data)


def save_baseline(shared_dir: Path, baseline: PodBaseline) -> Path:
    """Atomically write the baseline. The ONE file this package writes.

    No ``parents=True``: a mistyped sharedDir must fail loudly, not
    fabricate a whole tree. File ownership is whoever runs the CLI (root
    under ``sudo evolve-admin``); the shared dir's evolve ownership is
    what the single-writer story rests on — per-file chown is deploy's
    ``ensure_pod_perms`` territory, not this store's.
    """
    path = baseline_path(shared_dir)
    path.parent.mkdir(exist_ok=True)
    atomic_write_json(path, baseline.to_dict(), sort_keys=True, mode=0o644)
    return path
