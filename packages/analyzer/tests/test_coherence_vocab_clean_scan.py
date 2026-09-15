"""Regression: our own COHERENCE_VOCAB.md must scan clean against the default catalog.

Parallel to test_pod_conduct_clean_scan and test_runtime_notes_clean_scan —
same meta-bug class. COHERENCE_VOCAB.md is pod-wide injected text; if its
HTML-comment markers drift from the content-scan allowlist, our own file
fails our own scan and the prompt-injection signal warns on every sweep.

Spec: internal/spec-prompt-injection-scanner-2026-05-10.md
"""
from __future__ import annotations

from pathlib import Path

from content_scan.default_patterns import default_catalog
from content_scan.patterns import scan_file


REPO_ROOT = Path(__file__).resolve().parents[3]
COHERENCE_VOCAB = REPO_ROOT / "docs" / "system" / "COHERENCE_VOCAB.md"


def test_coherence_vocab_scans_clean_against_default_catalog():
    """COHERENCE_VOCAB.md must produce zero matches against the shipped catalog."""
    assert COHERENCE_VOCAB.exists(), f"missing source file: {COHERENCE_VOCAB}"
    text = COHERENCE_VOCAB.read_text()
    catalog = default_catalog()
    matches = scan_file(
        text=text,
        filename="COHERENCE_VOCAB.md",
        patterns=catalog.deny_patterns,
        evolve_markers_allowlist=catalog.evolve_markers_allowlist,
    )
    fired = [(m.pattern_id, m.line, m.excerpt[:80] if m.excerpt else "")
             for m in matches]
    assert not matches, (
        f"COHERENCE_VOCAB.md triggered {len(matches)} content-scan match(es) "
        f"— our own injected file fails our own scan. Patterns that fired:\n  "
        + "\n  ".join(f"{pid} at line {ln}: {snip!r}"
                     for pid, ln, snip in fired)
    )


def test_coherence_vocab_markers_are_in_allowlist():
    """The markers session_surface.py reads must match the scan allowlist."""
    from session_surface import _COHERENCE_VOCAB_BEGIN, _COHERENCE_VOCAB_END
    catalog = default_catalog()
    allowlist = catalog.evolve_markers_allowlist
    assert _COHERENCE_VOCAB_BEGIN in allowlist, (
        f"session_surface marker {_COHERENCE_VOCAB_BEGIN!r} not in "
        "content-scan allowlist; COHERENCE_VOCAB.md will warn on every scan"
    )
    assert _COHERENCE_VOCAB_END in allowlist


def test_coherence_vocab_is_loaded_into_session_prefix():
    """COHERENCE_VOCAB content must actually land in the session prefix.

    Catches the regression where the file/markers exist but the loader
    silently returns empty (path drift, marker typo).
    """
    from session_surface import COHERENCE_VOCAB as LOADED
    assert "C-A1" in LOADED
    assert "C1-1" in LOADED
