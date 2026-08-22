"""Regression: scanner cache must invalidate on catalog change.

Two real bugs observed on the deployed mini (2026-05-19) — the html_comment
``<!-- BEGIN EVOLVE-INSTALLED-APPS -->`` marker added in PR #1186 was being
flagged by 12 stuck ``content_scan_match`` signals despite PR #1261 adding
``<!-- BEGIN EVOLVE-* -->`` to the source default allowlist:

  1. The on-disk catalog (operator-curated) predated PR #1261's allowlist
     addition; ``write_default_if_missing`` only writes when absent, so
     the new entry never propagated to the live install.
  2. The per-file ScanResult cache keyed only on ``file_hash``; cached
     matches survived any catalog change for unchanged files.

The scanner now (a) unions the code-shipped default allowlist into the
effective allowlist at scan time, and (b) stamps a ``catalog_sig`` into
each ScanResult so cache hits require both file hash and catalog
signature to match.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md
"""
from __future__ import annotations

from pathlib import Path

from content_scan import scanner as _scanner
from content_scan.catalog import Catalog, Pattern, Scope


def _bare_catalog(allowlist: list[str] | None = None) -> Catalog:
    """Catalog with one html_comment pattern and the given operator allowlist.

    Excludes everything that would muddy the test (line_length, structural,
    other regexes) so the only thing that can fire is html_comment_unknown.
    """
    return Catalog(
        version=1,
        deny_patterns=[
            Pattern(
                id="html_comment_unknown",
                kind="html_comment",
                description="test",
                severity="warn",
            ),
        ],
        evolve_markers_allowlist=list(allowlist or []),
        scope=Scope(scanned_files_per_bot=[], scanned_pod_files=[]),
    )


def test_effective_allowlist_unions_defaults_and_operator():
    """Operator entries are merged on top of code-shipped defaults; defaults
    are always present so a PR adding a new evolve marker takes effect
    without an operator catalog edit."""
    from content_scan.default_patterns import default_catalog

    defaults = default_catalog().evolve_markers_allowlist
    extra = "<!-- operator-custom -->"
    cat = _bare_catalog(allowlist=[extra])

    effective = _scanner.effective_allowlist(cat)

    for entry in defaults:
        assert entry in effective, f"default {entry!r} dropped from effective allowlist"
    assert extra in effective, "operator-added entry dropped from effective allowlist"
    # No duplicates: order should be defaults first, then unique operator
    # additions only.
    assert len(effective) == len(set(effective)), "effective allowlist contains duplicates"


def test_effective_allowlist_dedups_operator_entry_already_in_default():
    """An operator catalog that re-states a default entry shouldn't double it."""
    from content_scan.default_patterns import default_catalog

    a_default = default_catalog().evolve_markers_allowlist[0]
    cat = _bare_catalog(allowlist=[a_default])
    effective = _scanner.effective_allowlist(cat)
    assert effective.count(a_default) == 1


def test_catalog_signature_changes_with_allowlist():
    """Adding an allowlist entry must produce a different signature so
    cached file results are invalidated on the next scan."""
    cat = _bare_catalog(allowlist=[])
    sig1 = _scanner.catalog_signature(cat, _scanner.effective_allowlist(cat))

    cat.evolve_markers_allowlist.append("<!-- new-marker -->")
    sig2 = _scanner.catalog_signature(cat, _scanner.effective_allowlist(cat))

    assert sig1 != sig2


def test_catalog_signature_stable_for_identical_input():
    cat = _bare_catalog(allowlist=["<!-- a -->", "<!-- b -->"])
    allowlist = _scanner.effective_allowlist(cat)
    assert _scanner.catalog_signature(cat, allowlist) == _scanner.catalog_signature(
        cat, allowlist
    )


def test_scan_target_cache_invalidates_when_allowlist_grows(tmp_path: Path):
    """Two scans of the same unchanged file: first with a permissive catalog
    that flags ``<!-- foo -->``, second after the operator adds ``<!-- foo -->``
    to the allowlist. The match must clear on the second scan — the legacy
    file-hash-only cache would have re-emitted the stale match forever."""
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    target = tmp_path / "AGENTS.md"
    target.write_text("intro\n<!-- foo -->\ntail\n")

    # Scan 1: allowlist doesn't cover <!-- foo -->.
    cat = _bare_catalog(allowlist=[])
    allowlist = _scanner.effective_allowlist(cat)
    sig = _scanner.catalog_signature(cat, allowlist)
    result1, findings1 = _scanner._scan_target(
        shared_dir=shared_dir,
        bot_id="__pod__",
        file_relpath="AGENTS.md",
        absolute_path=target,
        catalog=cat,
        allowlist=allowlist,
        catalog_sig=sig,
        bot_suppressions=[],
    )
    assert result1 is not None
    assert any(m.get("pattern_id") == "html_comment_unknown" for m in result1.matches)
    assert any(f["type"] == "content_scan_match" for f in findings1)

    # Scan 2: operator added <!-- foo --> to the allowlist. File unchanged,
    # but catalog_sig changed → cache invalidates → matcher re-runs → clean.
    cat2 = _bare_catalog(allowlist=["<!-- foo -->"])
    allowlist2 = _scanner.effective_allowlist(cat2)
    sig2 = _scanner.catalog_signature(cat2, allowlist2)
    assert sig2 != sig
    result2, findings2 = _scanner._scan_target(
        shared_dir=shared_dir,
        bot_id="__pod__",
        file_relpath="AGENTS.md",
        absolute_path=target,
        catalog=cat2,
        allowlist=allowlist2,
        catalog_sig=sig2,
        bot_suppressions=[],
    )
    assert result2 is not None
    assert result2.matches == [], (
        f"cache should have invalidated when allowlist grew, but matches survived: "
        f"{result2.matches!r}"
    )
    assert findings2 == []
    assert result2.catalog_sig == sig2


def test_scan_target_cache_hits_when_file_and_catalog_unchanged(tmp_path: Path):
    """Two scans with same file + same catalog: second scan must reuse the
    first scan's scanned_at timestamp (proof we didn't re-run the matcher)."""
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    target = tmp_path / "AGENTS.md"
    target.write_text("clean content with no comments\n")

    cat = _bare_catalog(allowlist=[])
    allowlist = _scanner.effective_allowlist(cat)
    sig = _scanner.catalog_signature(cat, allowlist)

    result1, _ = _scanner._scan_target(
        shared_dir=shared_dir,
        bot_id="__pod__",
        file_relpath="AGENTS.md",
        absolute_path=target,
        catalog=cat,
        allowlist=allowlist,
        catalog_sig=sig,
        bot_suppressions=[],
    )
    assert result1 is not None
    first_scanned_at = result1.scanned_at

    result2, _ = _scanner._scan_target(
        shared_dir=shared_dir,
        bot_id="__pod__",
        file_relpath="AGENTS.md",
        absolute_path=target,
        catalog=cat,
        allowlist=allowlist,
        catalog_sig=sig,
        bot_suppressions=[],
    )
    assert result2 is not None
    # Cache hit preserves the original scanned_at.
    assert result2.scanned_at == first_scanned_at
    assert result2.catalog_sig == sig


def test_scan_target_treats_legacy_result_without_catalog_sig_as_miss(tmp_path: Path):
    """Cached results predating this fix have no catalog_sig field. On
    upgrade, the scanner must treat them as a cache miss so the
    post-upgrade allowlist actually takes effect on unchanged files.

    Mirrors the live-mini failure mode: ScanResult JSON written under
    the old code had ``file_hash`` but no ``catalog_sig``, so the new
    code must not honor it as a hit."""
    import json

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    target = tmp_path / "AGENTS.md"
    target.write_text("intro\n<!-- foo -->\ntail\n")

    file_hash = _scanner._sha256_text(target.read_text())
    legacy_dir = _scanner.results_dir(shared_dir) / "__pod__"
    legacy_dir.mkdir(parents=True)
    # Pre-fix shape: matches present, no catalog_sig field at all.
    (legacy_dir / "AGENTS.md.json").write_text(json.dumps({
        "bot_id": "__pod__",
        "file": "AGENTS.md",
        "absolute_path": str(target),
        "file_hash": file_hash,
        "scanned_at": "2026-05-17T00:00:00Z",
        "file_size_bytes": len(target.read_text()),
        "matches": [{
            "pattern_id": "html_comment_unknown",
            "pattern_kind": "html_comment",
            "severity": "warn",
            "line": 2,
            "column_start": 0,
            "excerpt": "<!-- foo -->",
            "context_lines": [],
        }],
        "all_clear": False,
    }))

    # Operator added <!-- foo --> to the allowlist; the legacy cache must
    # NOT mask that — the new scan should re-run and produce zero matches.
    cat = _bare_catalog(allowlist=["<!-- foo -->"])
    allowlist = _scanner.effective_allowlist(cat)
    sig = _scanner.catalog_signature(cat, allowlist)
    result, findings = _scanner._scan_target(
        shared_dir=shared_dir,
        bot_id="__pod__",
        file_relpath="AGENTS.md",
        absolute_path=target,
        catalog=cat,
        allowlist=allowlist,
        catalog_sig=sig,
        bot_suppressions=[],
    )
    assert result is not None
    assert result.matches == []
    assert findings == []
    assert result.catalog_sig == sig
