"""Guard: MANIFEST_SCHEMA_VERSION must cover every "Schema vN" field block.

The counter is a field-vocabulary stamp — migrations are presence-driven,
so nothing breaks at runtime when it drifts. But a drifted stamp
under-reports vocabulary in logs/UI, and the v21/v22 blocks shipping
against a constant stuck at 20 proved the discipline was aspirational.
This test converts the convention to CI: any field block labeled
"Schema vN" in manifest.py with N greater than the constant fails here.

Spec: internal/spec-manifest-v7-slicing-2026-06-10.md §2.3 rule 4.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import manifest as manifest_mod  # noqa: E402


_SCHEMA_LABEL = re.compile(r"Schema v(\d+)")


def _labeled_versions() -> dict[int, int]:
    """Map of N → first line number for every "Schema vN" label in manifest.py.

    Reads the source through the imported module's __file__ so the
    conftest worktree rebind keeps test and code pointing at the same
    checkout.
    """
    source = Path(manifest_mod.__file__).read_text()
    found: dict[int, int] = {}
    for lineno, line in enumerate(source.splitlines(), start=1):
        for m in _SCHEMA_LABEL.finditer(line):
            n = int(m.group(1))
            found.setdefault(n, lineno)
    return found


def test_schema_labels_exist() -> None:
    """Sanity: the scan finds the known historical labels (v15..v22).

    If this fails, the label convention changed and the guard below is
    scanning air — fix the regex, don't delete the guard.
    """
    found = _labeled_versions()
    for known in (15, 16, 17, 19, 20, 21, 22):
        assert known in found, (
            f"expected a 'Schema v{known}' label in manifest.py; the guard's "
            f"scan may no longer match the label convention"
        )


def test_schema_version_constant_covers_all_labeled_blocks() -> None:
    found = _labeled_versions()
    constant = manifest_mod.MANIFEST_SCHEMA_VERSION
    ahead = {n: line for n, line in found.items() if n > constant}
    assert not ahead, (
        f"MANIFEST_SCHEMA_VERSION is {constant} but manifest.py labels "
        f"field blocks beyond it: "
        + ", ".join(f"Schema v{n} (line {line})" for n, line in sorted(ahead.items()))
        + ". Bump the constant in the same PR that adds the fields "
        "(internal/spec-manifest-v7-slicing-2026-06-10.md §2.3)."
    )
