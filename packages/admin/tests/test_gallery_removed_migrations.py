"""Gallery removal migration tests.

When a gallery app is retired because Evolve grew the same capability as
built-in functionality (the 2026-06-05 case: workspace-backup and
github-integration), existing bots may still have it installed. The
gallery resolver must surface a clear "this is now built-in to Evolve"
message instead of silently treating the install as up-to-date.

Spec: ``docs/gallery-removals-2026-06-05.md``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.gallery import (  # noqa: E402
    REMOVED_PKG_MIGRATIONS,
    get_removed_status,
)
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


# ── REMOVED_PKG_MIGRATIONS registry shape ───────────────────────────────────


def test_registry_includes_2026_06_05_retirements():
    """The 2026-06-05 retirements (workspace-backup + github-integration)
    must remain in the registry — they were the original reason for the
    map. If a future cleanup drops them, existing pod installs from
    before the registry change would 404 silently."""
    assert "p-f9bce546" in REMOVED_PKG_MIGRATIONS  # Workspace Backup
    assert "p-f047a60f" in REMOVED_PKG_MIGRATIONS  # GitHub Integration


def test_every_entry_has_required_fields():
    """Each entry must carry display_name, replaced_by, and reason so
    the consistency endpoint can render an operator-facing message
    without null-checks. Same shape contract that callers depend on."""
    for pkg_id, entry in REMOVED_PKG_MIGRATIONS.items():
        assert pkg_id.startswith("p-"), f"{pkg_id} not a pkg_id"
        for field in ("display_name", "replaced_by", "reason"):
            assert entry.get(field), f"{pkg_id} missing {field!r}"


def test_no_retired_pkg_id_still_in_gallery_index():
    """Sanity check: a pkg_id in the removal registry must NOT also
    have a live gallery package on disk. The migration entry is the
    replacement, not a sidekick — a stale gallery file would let the
    update path resolve first and hide the removal message."""
    import json

    repo_root = Path(__file__).resolve().parents[3]
    index_path = repo_root / "gallery" / "index.json"
    if not index_path.exists():
        return  # gallery index optional in some checkouts
    index = json.loads(index_path.read_text())
    live_pkg_ids = {entry.get("pkg_id") for entry in index}
    for retired in REMOVED_PKG_MIGRATIONS:
        assert retired not in live_pkg_ids, (
            f"{retired} is both retired (in REMOVED_PKG_MIGRATIONS) and "
            f"still listed in gallery/index.json — remove one or the other"
        )


# ── get_removed_status — returns None for live / no-pkg_id manifests ────────


def _make_manifest(pkg_id: str) -> ApplicationManifest:
    return ApplicationManifest(
        id="some-app",
        name="Some App",
        bot_id="bot-a",
        pkg_id=pkg_id,
        pkg_version="2026.04.16-1.0",
    )


def test_returns_none_for_manifest_without_pkg_id():
    m = ApplicationManifest(id="scanner-only", name="Scanner Only", bot_id="bot-a")
    assert m.pkg_id == ""
    assert get_removed_status(m) is None


def test_returns_none_for_live_gallery_pkg_id():
    """A pkg_id that's NOT in the removal map (still live in the
    gallery) must not be misreported as removed."""
    m = _make_manifest("p-9bfa1c84")  # Task Manager — live
    assert get_removed_status(m) is None


# ── get_removed_status — returns full record for retired pkg_ids ────────────


def test_returns_record_for_retired_workspace_backup():
    m = _make_manifest("p-f9bce546")
    result = get_removed_status(m)
    assert result is not None
    assert result["pkg_id"] == "p-f9bce546"
    assert result["display_name"] == "Workspace Backup"
    # The replaced_by string mentions the built-in capability so the
    # operator knows which surface now covers the use case.
    assert "backup" in result["replaced_by"].lower()
    assert "built-in to Evolve" in result["message"]


def test_returns_record_for_retired_github_integration():
    m = _make_manifest("p-f047a60f")
    result = get_removed_status(m)
    assert result is not None
    assert result["pkg_id"] == "p-f047a60f"
    assert result["display_name"] == "GitHub Integration"
    assert "github" in result["replaced_by"].lower()
    assert "built-in to Evolve" in result["message"]


def test_message_is_operator_actionable():
    """The pre-formatted message must tell the operator what to do
    ('uninstall the gallery copy') rather than just stating the fact.
    Mirrors the operator-actionability rule for alerts catalog entries.
    """
    m = _make_manifest("p-f9bce546")
    result = get_removed_status(m)
    assert result is not None
    assert "uninstall" in result["message"].lower()
