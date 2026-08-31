"""Regression: our own RUNTIME_NOTES.md must scan clean against the default catalog.

Parallel to test_pod_conduct_clean_scan — same meta-bug class. RUNTIME_NOTES.md
is pod-wide injected text; if its HTML-comment markers drift from the
content-scan allowlist, our own file fails our own scan and the prompt-injection
signal warns on every sweep.

Spec: internal/spec-prompt-injection-scanner-2026-05-10.md
"""
from __future__ import annotations

from pathlib import Path

from content_scan.default_patterns import default_catalog
from content_scan.patterns import scan_file


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_NOTES = REPO_ROOT / "docs" / "system" / "RUNTIME_NOTES.md"


def test_runtime_notes_scans_clean_against_default_catalog():
    """RUNTIME_NOTES.md must produce zero matches against the shipped catalog."""
    assert RUNTIME_NOTES.exists(), f"missing source file: {RUNTIME_NOTES}"
    text = RUNTIME_NOTES.read_text()
    catalog = default_catalog()
    matches = scan_file(
        text=text,
        filename="RUNTIME_NOTES.md",
        patterns=catalog.deny_patterns,
        evolve_markers_allowlist=catalog.evolve_markers_allowlist,
    )
    fired = [(m.pattern_id, m.line, m.excerpt[:80] if m.excerpt else "") for m in matches]
    assert not matches, (
        f"RUNTIME_NOTES.md triggered {len(matches)} content-scan match(es) — our own "
        f"injected file fails our own scan. Patterns that fired:\n  "
        + "\n  ".join(f"{pid} at line {ln}: {snip!r}" for pid, ln, snip in fired)
    )


def test_runtime_notes_markers_are_in_allowlist():
    """The markers session_surface.py reads must match the scan allowlist."""
    from session_surface import _RUNTIME_NOTES_BEGIN, _RUNTIME_NOTES_END
    catalog = default_catalog()
    allowlist = catalog.evolve_markers_allowlist
    assert _RUNTIME_NOTES_BEGIN in allowlist, (
        f"session_surface marker {_RUNTIME_NOTES_BEGIN!r} not in content-scan allowlist; "
        "RUNTIME_NOTES.md will warn on every scan"
    )
    assert _RUNTIME_NOTES_END in allowlist, (
        f"session_surface marker {_RUNTIME_NOTES_END!r} not in content-scan allowlist; "
        "RUNTIME_NOTES.md will warn on every scan"
    )


def test_runtime_notes_is_loaded_into_session_prefix():
    """RUNTIME_NOTES content must actually land in the session prefix.

    Catches the regression where the file/markers exist but the loader
    silently returns empty (path drift, marker typo). Loads via the public
    constant and asserts a known stable substring from the file.
    """
    from session_surface import RUNTIME_NOTES as LOADED
    # Stable substring from the marker block — if this disappears the file
    # has been edited in a way the test needs to know about.
    assert "Exec:" in LOADED, (
        "RUNTIME_NOTES loaded but missing the exec idioms rule — either the "
        "file was rewritten or the loader is reading the wrong markers."
    )
