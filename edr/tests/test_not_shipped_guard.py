"""edr/tests/test_not_shipped_guard.py — the G1 "not-shipped" guard.

EDR ships nothing a pod loads. It must never appear in any pod deploy or
install path. This test fails if any guarded path imports or path-references
``edr`` / ``edr/``.

The guarded set is the set of files that decide what lands on a pod:

  - ``packages/admin/evolve_admin/deploy.py`` — the bot deploy entrypoint.
  - the plist / launchd / JobSpec renderers
    (``packages/{admin/evolve_admin,analyzer}/runtime/scheduler.py``) — what
    becomes a launchd/systemd job on a pod host.
  - ``packages/plugin/**`` — the OpenClaw plugin code every gateway loads.

The matcher looks for ``edr`` as a Python import (``import edr`` /
``from edr``) or as a module/path token in a string or path literal
(``"edr"``, ``'edr'``, ``edr/``, ``/edr``). It deliberately does NOT match
``edr`` embedded in a longer word (``ledger``, ``considered``,
``rendered``) — those are not references to this package.

A positive control feeds the matcher a synthetic line that DOES reference
``edr/`` and asserts it is flagged, so the guard can never be vacuously
green (the failure mode that let ETR-class problems hide).

Design: docs/edr/design-edr-2026-06-11.md §1 (EDR ships nothing a pod loads).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root = three levels up from this file (edr/tests/<this> → edr → root).
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── The matcher ──────────────────────────────────────────────────────────────
#
# Each alternative anchors on a boundary so a bare "edr" inside a longer word
# (ledger, rendered, considered) does NOT match:
#   - `import edr`            / `from edr`            — Python import of the pkg
#   - `"edr"` / `'edr'`       — the module name as a string literal
#   - `edr/` preceded by a non-word char or start    — a path segment
#   - `/edr` followed by `/`, quote, or end          — a path segment
_EDR_REFERENCE = re.compile(
    r"""
    (?:^|[^A-Za-z0-9_])              # left boundary (start or non-word char)
    (?:
        import\s+edr\b               # import edr
      | from\s+edr\b                 # from edr import ...
      | ['"]edr['"]                  # "edr" / 'edr' string literal
      | edr/                         # edr/ path segment
      | /edr(?=['"/]|$)              # /edr path segment (terminated)
    )
    """,
    re.VERBOSE | re.MULTILINE,
)


def _references_edr(text: str) -> bool:
    """True iff *text* references the edr package/path (boundary-aware)."""
    return _EDR_REFERENCE.search(text) is not None


# ── The guarded set ──────────────────────────────────────────────────────────
def _guarded_files() -> list[Path]:
    files: list[Path] = []

    explicit = [
        _REPO_ROOT / "packages/admin/evolve_admin/deploy.py",
        # plist / launchd / JobSpec renderers (the single plist source +
        # the analyzer-side scheduler seam).
        _REPO_ROOT / "packages/admin/evolve_admin/runtime/scheduler.py",
        _REPO_ROOT / "packages/analyzer/runtime/scheduler.py",
    ]
    for path in explicit:
        assert path.exists(), f"guarded path moved or missing: {path}"
        files.append(path)

    # The whole plugin tree (source + manifest) — every gateway loads it.
    plugin_dir = _REPO_ROOT / "packages/plugin"
    assert plugin_dir.is_dir(), f"plugin dir missing: {plugin_dir}"
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        # Built/vendored output and node deps are not authored pod-install
        # code; skip them to keep the guard about source intent.
        rel_parts = path.relative_to(plugin_dir).parts
        if rel_parts and rel_parts[0] in {"node_modules", "dist"}:
            continue
        if path.suffix in {".ts", ".tsx", ".mjs", ".js", ".json", ".plist"}:
            files.append(path)

    return files


def test_guarded_set_is_nonempty():
    """A guard over an empty set is vacuously green — assert we actually
    found the deploy/renderer/plugin files."""
    files = _guarded_files()
    assert len(files) >= 4, f"guarded set too small ({len(files)}): {files}"
    names = {p.name for p in files}
    assert "deploy.py" in names
    assert "scheduler.py" in names


@pytest.mark.parametrize("path", _guarded_files(), ids=lambda p: str(p))
def test_pod_install_path_does_not_reference_edr(path: Path):
    """No pod deploy/install path may import or path-reference edr/."""
    text = path.read_text(encoding="utf-8", errors="replace")
    assert not _references_edr(text), (
        f"{path} references the edr package/path — EDR must never appear in a "
        f"pod deploy or install path (it ships nothing a pod loads). If this "
        f"is a false positive, tighten the matcher in this test."
    )


# ── Positive control — the guard is NOT vacuously green ──────────────────────
def test_matcher_flags_a_planted_edr_reference():
    """Feed the matcher synthetic lines that DO reference edr/ and assert it
    flags every one. Without this, a broken matcher would pass the guard
    silently."""
    planted = [
        "from edr.actuator import run_session",
        "import edr",
        'spec = JobSpec(program=["python3", "-m", "edr/runner"])',
        'plist_args = ["/Users/Shared/evolve-repo/edr/main.py"]',
        "modules = ['signals', 'edr', 'arbiter']",
    ]
    for line in planted:
        assert _references_edr(line), f"matcher missed planted reference: {line!r}"


def test_matcher_ignores_edr_inside_longer_words():
    """Boundary check: the matcher must NOT fire on words that merely contain
    the letters e-d-r (ledger, rendered, considered) — those are not
    references to this package."""
    benign = [
        "self.ledger.append(entry)",
        "the rendered plist is written to disk",
        "this was considered and rejected",
        "redrive = True  # retry the queue",
    ]
    for line in benign:
        assert not _references_edr(line), f"matcher false-positived on: {line!r}"
