"""Regression: our own POD_CONDUCT.md must scan clean against the default catalog.

The April 2026 scanner caught HTML comments with markers outside the
allowlist. Initially POD_CONDUCT.md used `<!-- summary-start -->` /
`<!-- summary-end -->` while the allowlist contained
`<!-- evolve-pod-conduct:begin/end -->` — a meta-bug where our own
pod-wide injection file failed our own scan with a "warn" severity. This
test guards against any future drift between the file we ship and the
allowlist we ship.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md
"""
from __future__ import annotations

from pathlib import Path

from content_scan.default_patterns import default_catalog
from content_scan.patterns import scan_file


REPO_ROOT = Path(__file__).resolve().parents[3]
POD_CONDUCT = REPO_ROOT / "docs" / "system" / "POD_CONDUCT.md"


def test_pod_conduct_scans_clean_against_default_catalog():
    """POD_CONDUCT.md must produce zero matches against the shipped catalog."""
    assert POD_CONDUCT.exists(), f"missing source file: {POD_CONDUCT}"
    text = POD_CONDUCT.read_text()
    catalog = default_catalog()
    matches = scan_file(
        text=text,
        filename="POD_CONDUCT.md",
        patterns=catalog.deny_patterns,
        evolve_markers_allowlist=catalog.evolve_markers_allowlist,
    )
    # If this fails, the failure message includes which pattern fired so an
    # operator can either fix POD_CONDUCT.md or extend the allowlist.
    fired = [(m.pattern_id, m.line, m.excerpt[:80] if m.excerpt else "") for m in matches]
    assert not matches, (
        f"POD_CONDUCT.md triggered {len(matches)} content-scan match(es) — our own "
        f"injected file fails our own scan. Patterns that fired:\n  "
        + "\n  ".join(f"{pid} at line {ln}: {snip!r}" for pid, ln, snip in fired)
    )


def test_pod_conduct_summary_markers_are_in_allowlist():
    """The markers session_surface.py reads must match the scan allowlist.

    session_surface._SUMMARY_BEGIN / _SUMMARY_END drive the conduct-injection
    text; if these drift from the content-scan allowlist, POD_CONDUCT.md will
    fail its own scan (the meta-bug this test guards against).
    """
    from session_surface import _SUMMARY_BEGIN, _SUMMARY_END
    catalog = default_catalog()
    allowlist = catalog.evolve_markers_allowlist
    assert _SUMMARY_BEGIN in allowlist, (
        f"session_surface marker {_SUMMARY_BEGIN!r} not in content-scan allowlist; "
        "POD_CONDUCT.md will warn on every scan"
    )
    assert _SUMMARY_END in allowlist, (
        f"session_surface marker {_SUMMARY_END!r} not in content-scan allowlist; "
        "POD_CONDUCT.md will warn on every scan"
    )
