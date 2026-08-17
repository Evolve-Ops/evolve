"""Regression: app_registry's AGENTS.md marker pair must scan clean.

`evolve_admin.applications.app_registry` injects an
`<!-- BEGIN EVOLVE-INSTALLED-APPS -->` / `<!-- END EVOLVE-INSTALLED-APPS -->`
block into every bot's AGENTS.md when apps are installed. Without an
allowlist entry, content_scan's `html_comment_unknown` pattern fires a
`warn` Signal on every bot in any pod with apps installed — Evolve
alerting on its own injection markers.

The shipped allowlist now contains `<!-- BEGIN EVOLVE-* -->` and
`<!-- END EVOLVE-* -->` wildcards. This test guards against drift in
either direction: the wildcard form disappearing, or the marker
constants in app_registry changing shape so they no longer match.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md
"""
from __future__ import annotations

from content_scan.default_patterns import default_catalog
from content_scan.patterns import _is_marker_allowed


def test_apps_injection_markers_are_allowlist_allowed():
    from evolve_admin.applications.app_registry import (
        _AGENTS_MARKER_BEGIN,
        _AGENTS_MARKER_END,
    )
    allowlist = default_catalog().evolve_markers_allowlist
    assert _is_marker_allowed(_AGENTS_MARKER_BEGIN, allowlist), (
        f"app_registry begin marker {_AGENTS_MARKER_BEGIN!r} not covered by "
        f"content-scan allowlist; every bot with apps installed will warn on "
        f"every scan"
    )
    assert _is_marker_allowed(_AGENTS_MARKER_END, allowlist), (
        f"app_registry end marker {_AGENTS_MARKER_END!r} not covered by "
        f"content-scan allowlist; every bot with apps installed will warn on "
        f"every scan"
    )


def test_provenance_markers_are_allowlist_allowed():
    """provenance.format_marker_string embeds a marker into every forge-
    managed file. Without an allowlist entry, html_comment_unknown fires on
    every bot with any installed app (the marker is on the file's first
    line). Covers both v6 (`pkg=`) and v7 (`spec=`) shapes."""
    from evolve_admin.applications.provenance import format_marker_string

    allowlist = default_catalog().evolve_markers_allowlist

    v6_marker = "<!-- " + format_marker_string(
        ["p-0bba4b9e"],
        "f-56c3106a",
        pkg_versions={"p-0bba4b9e": "2026.05.27-1.0"},
        file_version="2026.05.27.1",
        keyword="pkg",
    ) + " -->"
    assert _is_marker_allowed(v6_marker, allowlist), (
        f"v6 provenance marker {v6_marker!r} not covered by content-scan "
        f"allowlist; every forge-managed file warns on every scan"
    )

    v7_marker = "<!-- " + format_marker_string(
        ["p-0bba4b9e"],
        "f-56c3106a",
        pkg_versions={"p-0bba4b9e": "2026.05.27-1.0"},
        file_version="2026.05.27-1.0",
        keyword="spec",
    ) + " -->"
    assert _is_marker_allowed(v7_marker, allowlist), (
        f"v7 provenance marker {v7_marker!r} not covered by content-scan "
        f"allowlist; every forge-managed file warns on every scan"
    )
