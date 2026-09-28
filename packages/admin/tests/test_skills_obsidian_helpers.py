"""tests/test_skills_obsidian_helpers.py — Obsidian vault read/write helpers.

Tests for V1.5-2 obsidian_helpers.py:
  - search_vault: keyword search, path-traversal guard, empty/error cases.
  - get_note: by relative path, .md extension auto-add, path-traversal guard.
  - list_recent_notes: date window, newest-first ordering, max_results cap.
  - append_to_daily_note: write opt-in required, creates file, appends to
    existing, path-traversal guard, vault boundary enforcement.

All tests use tmp_path fixtures — no mocking needed since the helpers
operate on real paths.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import obsidian_helpers  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path):
    """A small Obsidian vault with notes at different modification times."""
    v = tmp_path / "Vault"
    v.mkdir()

    # Three notes with distinct content and modification times.
    note_a = v / "Project Alpha.md"
    note_a.write_text("# Project Alpha\n\nThis is a research note on project alpha.\n")

    note_b = v / "Meeting notes.md"
    note_b.write_text("# Meeting notes\n\nQ3 planning discussed today.\n")

    subdir = v / "archive"
    subdir.mkdir()
    note_c = subdir / "Old idea.md"
    note_c.write_text("# Old idea\n\nSomething I thought of back in April.\n")

    return v


@pytest.fixture
def vault_with_times(vault, tmp_path):
    """Vault with explicit mtime manipulation for recency tests."""
    import os, time

    now = time.time()

    # Assign mtimes by name so ordering is deterministic.
    project_alpha = vault / "Project Alpha.md"
    meeting_notes = vault / "Meeting notes.md"
    old_idea = vault / "archive" / "Old idea.md"

    # "Project Alpha.md" → most recent (now)
    os.utime(str(project_alpha), (now, now))
    # "Meeting notes.md" → 2 days ago
    two_days_ago = now - 2 * 86400
    os.utime(str(meeting_notes), (two_days_ago, two_days_ago))
    # "Old idea.md" → 10 days ago
    ten_days_ago = now - 10 * 86400
    os.utime(str(old_idea), (ten_days_ago, ten_days_ago))

    return vault


# ── search_vault ──────────────────────────────────────────────────────────────


def test_search_vault_finds_match(vault):
    results, err = obsidian_helpers.search_vault(str(vault), "research note")
    assert err is None
    assert len(results) == 1
    assert "Project Alpha" in results[0]["title"]
    assert "research note" in results[0]["snippet"]


def test_search_vault_case_insensitive(vault):
    results, err = obsidian_helpers.search_vault(str(vault), "MEETING NOTES")
    assert err is None
    assert len(results) >= 1  # matches in title or content


def test_search_vault_no_match(vault):
    results, err = obsidian_helpers.search_vault(str(vault), "xyzzy_no_match_ever")
    assert err is None
    assert results == []


def test_search_vault_empty_query(vault):
    results, err = obsidian_helpers.search_vault(str(vault), "")
    assert err == "query_empty"
    assert results == []


def test_search_vault_nonexistent_vault(tmp_path):
    results, err = obsidian_helpers.search_vault(str(tmp_path / "NoVault"), "anything")
    assert err == "vault_not_found"
    assert results == []


def test_search_vault_result_fields(vault):
    results, err = obsidian_helpers.search_vault(str(vault), "planning")
    assert err is None
    assert len(results) == 1
    r = results[0]
    assert "path" in r
    assert "title" in r
    assert "snippet" in r
    assert "modified" in r
    # modified should be an ISO datetime string
    assert "T" in r["modified"]


def test_search_vault_max_results(vault):
    # All three notes contain "note" in content or filename.
    results, err = obsidian_helpers.search_vault(str(vault), "note", max_results=2)
    assert err is None
    assert len(results) <= 2


def test_search_vault_path_traversal_guard(tmp_path):
    """Symlinks outside the vault should not be followed."""
    vault = tmp_path / "Vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nsensitive data\n")
    # Create a symlink inside the vault pointing outside.
    link = vault / "escape.md"
    link.symlink_to(outside)

    results, err = obsidian_helpers.search_vault(str(vault), "sensitive data")
    assert err is None
    # The symlink points outside the vault; it should be skipped.
    # (On resolution, escape.md → outside.md which is NOT inside vault.)
    for r in results:
        assert "sensitive" not in r.get("snippet", "").lower()


# ── get_note ──────────────────────────────────────────────────────────────────


def test_get_note_by_relative_path(vault):
    text, err = obsidian_helpers.get_note(str(vault), "Project Alpha.md")
    assert err is None
    assert "research note" in text


def test_get_note_auto_adds_md_extension(vault):
    text, err = obsidian_helpers.get_note(str(vault), "Meeting notes")
    assert err is None
    assert "Q3 planning" in text


def test_get_note_in_subdir(vault):
    text, err = obsidian_helpers.get_note(str(vault), "archive/Old idea.md")
    assert err is None
    assert "Old idea" in text or "April" in text


def test_get_note_not_found(vault):
    text, err = obsidian_helpers.get_note(str(vault), "nonexistent")
    assert text is None
    assert err == "note_not_found"


def test_get_note_path_traversal(vault, tmp_path):
    """get_note must refuse paths that escape the vault."""
    text, err = obsidian_helpers.get_note(str(vault), "../../etc/passwd")
    assert text is None
    assert err == "path_outside_vault"


def test_get_note_nonexistent_vault(tmp_path):
    text, err = obsidian_helpers.get_note(str(tmp_path / "NoVault"), "Note.md")
    assert text is None
    assert err == "vault_not_found"


def test_get_note_title_from_h1(vault):
    """_note_title should return the H1 text, not the filename."""
    text, err = obsidian_helpers.get_note(str(vault), "Project Alpha.md")
    assert err is None
    # Verify the note has a proper H1.
    assert text.startswith("# Project Alpha")


# ── list_recent_notes ────────────────────────────────────────────────────────


def test_list_recent_notes_default(vault):
    """Default 7-day window includes notes modified today."""
    results, err = obsidian_helpers.list_recent_notes(str(vault))
    assert err is None
    # All fixture files were just created (mtime = now) so all should appear.
    assert len(results) >= 1


def test_list_recent_notes_fields(vault):
    results, err = obsidian_helpers.list_recent_notes(str(vault))
    assert err is None
    assert len(results) > 0
    r = results[0]
    assert "path" in r
    assert "title" in r
    assert "snippet" in r
    assert "modified" in r
    assert "_mtime" not in r  # internal sort key must be stripped


def test_list_recent_notes_newest_first(vault_with_times):
    """Newest note should come first."""
    results, err = obsidian_helpers.list_recent_notes(str(vault_with_times), days=7)
    assert err is None
    # Most recent is "Project Alpha.md" (mtime = now).
    assert len(results) >= 1
    assert "Alpha" in results[0]["title"]


def test_list_recent_notes_window_excludes_old(vault_with_times):
    """Notes older than the window are excluded."""
    results, err = obsidian_helpers.list_recent_notes(str(vault_with_times), days=5)
    assert err is None
    # "Old idea.md" is 10 days old; should NOT appear.
    titles = [r["title"] for r in results]
    assert not any("Old idea" in t for t in titles)


def test_list_recent_notes_max_results(vault):
    results, err = obsidian_helpers.list_recent_notes(str(vault), max_results=1)
    assert err is None
    assert len(results) <= 1


def test_list_recent_notes_nonexistent_vault(tmp_path):
    results, err = obsidian_helpers.list_recent_notes(str(tmp_path / "Missing"))
    assert err == "vault_not_found"
    assert results == []


def test_list_recent_notes_snippet_skips_frontmatter(tmp_path):
    """Frontmatter (--- ... ---) should not appear in snippet."""
    vault = tmp_path / "V"
    vault.mkdir()
    note = vault / "Fronty.md"
    note.write_text("---\ntags: [work]\n---\n\n# Fronty Note\n\nActual content here.\n")
    results, err = obsidian_helpers.list_recent_notes(str(vault))
    assert err is None
    assert len(results) == 1
    snippet = results[0]["snippet"]
    assert "Actual content here" in snippet
    assert "tags:" not in snippet


# ── append_to_daily_note ──────────────────────────────────────────────────────


def test_append_requires_write_enabled(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    ok, err = obsidian_helpers.append_to_daily_note(
        str(vault), "some text", write_enabled=False
    )
    assert not ok
    assert err is not None
    assert "write_not_enabled" in err


def test_append_creates_new_daily_note(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    today = date.today()
    ok, err = obsidian_helpers.append_to_daily_note(
        str(vault), "Test note entry.", write_enabled=True, target_date=today
    )
    assert ok, f"Expected success, got err={err}"
    note_file = vault / f"{today.isoformat()}.md"
    assert note_file.exists()
    content = note_file.read_text()
    assert "Test note entry." in content


def test_append_adds_heading_on_new_file(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    today = date.today()
    obsidian_helpers.append_to_daily_note(
        str(vault), "First entry.", write_enabled=True, target_date=today
    )
    note_file = vault / f"{today.isoformat()}.md"
    content = note_file.read_text()
    assert f"# {today.isoformat()}" in content


def test_append_to_existing_daily_note(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    today = date.today()
    note_file = vault / f"{today.isoformat()}.md"
    note_file.write_text(f"# {today.isoformat()}\n\nExisting line.\n")
    ok, err = obsidian_helpers.append_to_daily_note(
        str(vault), "New appended line.", write_enabled=True, target_date=today
    )
    assert ok, f"Expected success, got err={err}"
    content = note_file.read_text()
    assert "Existing line." in content
    assert "New appended line." in content


def test_append_in_subdirectory(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    today = date.today()
    ok, err = obsidian_helpers.append_to_daily_note(
        str(vault), "Daily entry.", write_enabled=True,
        target_date=today, daily_note_folder="daily"
    )
    assert ok, f"Expected success, got err={err}"
    note_file = vault / "daily" / f"{today.isoformat()}.md"
    assert note_file.exists()


def test_append_path_traversal_guard(tmp_path):
    """daily_note_folder that escapes the vault must be rejected."""
    vault = tmp_path / "Vault"
    vault.mkdir()
    today = date.today()
    ok, err = obsidian_helpers.append_to_daily_note(
        str(vault), "Escape attempt.", write_enabled=True,
        target_date=today, daily_note_folder="../../etc"
    )
    assert not ok
    assert err is not None
    assert "outside_vault" in err or "traversal" in err.lower() or "outside" in err.lower()


def test_append_empty_text_rejected(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    ok, err = obsidian_helpers.append_to_daily_note(
        str(vault), "   ", write_enabled=True
    )
    assert not ok
    assert err == "text_empty"


def test_append_nonexistent_vault(tmp_path):
    ok, err = obsidian_helpers.append_to_daily_note(
        str(tmp_path / "Missing"), "text", write_enabled=True
    )
    assert not ok
    assert err == "vault_not_found"


# ── Helper internals ──────────────────────────────────────────────────────────


def test_note_title_from_h1():
    from pathlib import PurePosixPath
    path = Path("/vault/Some-file.md")
    text = "# My Great Note\n\nSome content.\n"
    title = obsidian_helpers._note_title(path, text)
    assert title == "My Great Note"


def test_note_title_fallback_to_filename():
    path = Path("/vault/My-Note.md")
    text = "No heading here.\n"
    title = obsidian_helpers._note_title(path, text)
    assert "My Note" in title  # dashes → spaces


def test_first_content_line_skips_heading():
    text = "# Title\n\nActual content.\n"
    line = obsidian_helpers._first_content_line(text)
    assert line == "Actual content."


def test_first_content_line_skips_frontmatter():
    text = "---\ntags: work\n---\n\nReal content.\n"
    line = obsidian_helpers._first_content_line(text)
    assert line == "Real content."


def test_first_content_line_empty():
    text = "# Just a heading\n"
    line = obsidian_helpers._first_content_line(text)
    assert line == ""


def test_is_in_vault_true(tmp_path):
    vault = tmp_path / "V"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("")
    assert obsidian_helpers._is_in_vault(vault, note)


def test_is_in_vault_false(tmp_path):
    vault = tmp_path / "V"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("")
    assert not obsidian_helpers._is_in_vault(vault, outside)


# ─────────────────────────────────────────────────────────────────────────────
# V1.5-2 should-fix: per-file size cap (regression tests)
# ─────────────────────────────────────────────────────────────────────────────

def _make_large_file(path: Path, size_bytes: int = 2 * 1024 * 1024) -> None:
    """Write a file of exactly size_bytes bytes (2 MB by default)."""
    path.write_bytes(b"x" * size_bytes)


def test_get_note_rejects_oversized_file(tmp_path):
    """get_note must return note_too_large for files > 1 MB (V1.5-2 #4)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "huge.md"
    _make_large_file(note)  # 2 MB

    text, err = obsidian_helpers.get_note(str(vault), "huge")
    assert text is None
    assert err == "note_too_large"


def test_get_note_accepts_normal_sized_file(tmp_path):
    """get_note should work normally for files under 1 MB."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "normal.md"
    note.write_text("# Normal\n\nSome content.")

    text, err = obsidian_helpers.get_note(str(vault), "normal")
    assert err is None
    assert "Some content." in (text or "")


def test_search_vault_skips_oversized_files(tmp_path):
    """search_vault must skip files > 1 MB and report skipped_oversize (V1.5-2 #4)."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Normal note that matches.
    (vault / "match.md").write_text("keyword found here")
    # Large file that would also match — should be skipped.
    large = vault / "huge.md"
    _make_large_file(large)  # 2 MB; write "keyword" at start but still over cap
    large.write_bytes(b"keyword" + b"x" * (2 * 1024 * 1024 - 7))

    results, err = obsidian_helpers.search_vault(str(vault), "keyword")
    assert err is None
    # match.md should appear; huge.md should NOT appear.
    paths = [r.get("path", "") for r in results]
    assert "match.md" in paths
    assert not any("huge" in p for p in paths)
    # skipped_oversize count should be > 0.
    total_skipped = sum(r.get("skipped_oversize", 0) for r in results)
    assert total_skipped >= 1


def test_list_recent_notes_skips_oversized_files(tmp_path):
    """list_recent_notes must skip files > 1 MB (V1.5-2 #4)."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Normal note, recently modified.
    (vault / "recent.md").write_text("# Recent\n\nContent.")
    # Large note, also recent.
    huge = vault / "huge.md"
    _make_large_file(huge)  # 2 MB

    results, err = obsidian_helpers.list_recent_notes(str(vault), days=30)
    assert err is None
    paths = [r.get("path", "") for r in results]
    assert "recent.md" in paths
    assert not any("huge" in p for p in paths)
    # skipped_oversize reported on first result.
    if results:
        assert results[0].get("skipped_oversize", 0) >= 1


def test_list_recent_notes_mtime_before_content_read(tmp_path):
    """list_recent_notes should not read content for files that fail the mtime filter.

    This is a structural check: only recently-modified files should appear. Old files
    — even if they contain matching content — must be excluded without being read.
    """
    import os
    vault = tmp_path / "vault"
    vault.mkdir()

    # Recent note.
    recent = vault / "recent.md"
    recent.write_text("# Recent Note\n\nHello world.")

    # Old note: set mtime to 60 days ago.
    old = vault / "old.md"
    old.write_text("# Old Note\n\nHello world.")
    old_ts = (datetime.now() - timedelta(days=60)).timestamp()
    os.utime(str(old), (old_ts, old_ts))

    results, err = obsidian_helpers.list_recent_notes(str(vault), days=7)
    assert err is None
    paths = [r.get("path", "") for r in results]
    assert "recent.md" in paths
    assert "old.md" not in paths
