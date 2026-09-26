"""tests/test_seed_pod_runtime_notes.py — the pod-doc seeder creates RUNTIME_NOTES.md.

Regression for the genuine ``content_scan_file_disappeared`` that fired for
``__pod__: RUNTIME_NOTES.md`` on evo-vps since 2026-06-23. RUNTIME_NOTES.md is
listed in the content-scan catalog's ``scanned_pod_files`` (resolved to
``shared_dir/RUNTIME_NOTES.md`` by content_scan/scanner.py), but — unlike
POD_CONDUCT.md — nothing ever seeded it there, so the scanner paged a real
"this pod doc is missing" condition on every sweep.

``_seed_pod_runtime_notes`` fixes that with create-if-absent semantics:
  * creates shared_dir/RUNTIME_NOTES.md from the canonical docs/system source
    when absent (fresh + existing pods self-heal on deploy);
  * is a no-op when the file already exists (never clobbers operator content).
"""

from __future__ import annotations

from pathlib import Path

from evolve_admin import deploy as _deploy


def _result():
    return _deploy.DeployResult(bot_id="shared", success=True)


def test_seeds_runtime_notes_when_absent(tmp_path: Path):
    """A pod with no RUNTIME_NOTES.md gets the canonical doc seeded."""
    shared = tmp_path / "shared"
    shared.mkdir()
    dest = shared / "RUNTIME_NOTES.md"
    assert not dest.exists()

    result = _result()
    _deploy._seed_pod_runtime_notes(shared, result)

    assert dest.exists(), "seeder did not create RUNTIME_NOTES.md"
    text = dest.read_text()
    # Seeded from the canonical source, so the runtime-notes marker block the
    # content scanner allowlists (and session_surface injects) is present.
    assert "<!-- evolve-runtime-notes:begin -->" in text
    assert "<!-- evolve-runtime-notes:end -->" in text
    # Matches the canonical doc the live injection reads.
    assert text.strip() == _deploy.RUNTIME_NOTES_SOURCE.read_text().strip()


def test_runtime_notes_seed_is_noop_when_present(tmp_path: Path):
    """An existing (possibly operator-edited) RUNTIME_NOTES.md is left untouched."""
    shared = tmp_path / "shared"
    shared.mkdir()
    dest = shared / "RUNTIME_NOTES.md"
    operator_content = "# Operator's own runtime notes\n\nDo not clobber me.\n"
    dest.write_text(operator_content)

    result = _result()
    _deploy._seed_pod_runtime_notes(shared, result)

    assert dest.read_text() == operator_content, "seeder clobbered existing content"
    assert any("no-op" in s for s in result.steps)


def test_seeded_file_matches_scanner_pod_file_target(tmp_path: Path):
    """The seeded path is exactly what the content scanner expects.

    Pins the path contract: catalog scope lists RUNTIME_NOTES.md in
    scanned_pod_files, which the scanner resolves to shared_dir/RUNTIME_NOTES.md.
    If either side drifts, the seeded file and the scanned file diverge and the
    disappeared-alert returns.
    """
    from content_scan.default_patterns import default_catalog

    catalog = default_catalog()
    assert "RUNTIME_NOTES.md" in catalog.scope.scanned_pod_files

    shared = tmp_path / "shared"
    shared.mkdir()
    _deploy._seed_pod_runtime_notes(shared, _result())

    # The scanner resolves each scanned_pod_files entry to shared_dir/<name>.
    scanned_target = shared / "RUNTIME_NOTES.md"
    assert scanned_target.exists()
