"""tests/test_briefing_vault_section.py — briefing.py vault section integration.

Tests for V1.5-2 additions to briefing.py:
  - fetch_vault_section: returns recent notes from a real tmpdir vault.
  - compose_briefing: vault_notes=None omits the section; list renders.
  - CLI --vault-path flag wires through to the composed message.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

# ── Load briefing.py (same pattern as test_briefing_weather_news.py) ─────────


def _load_briefing():
    script_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "gallery"
        / "bot-templates"
        / "morning-briefing"
        / "scripts"
        / "briefing.py"
    )
    if not script_path.exists():
        pytest.fail(f"briefing.py not found: {script_path}")
    spec = importlib.util.spec_from_file_location("briefing_vault_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


briefing = _load_briefing()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path):
    """Small vault with notes modified now (all within default 3-day window)."""
    v = tmp_path / "Vault"
    v.mkdir()
    (v / "Project Alpha.md").write_text("# Project Alpha\n\nMain research note.\n")
    (v / "Ideas.md").write_text("# Ideas\n\nLong list of things to try.\n")
    (v / "Meeting 2026-05-12.md").write_text("# Meeting 2026-05-12\n\nQ3 targets agreed.\n")
    return v


@pytest.fixture
def vault_with_old_note(vault):
    """Adds a note with mtime set to 10 days ago."""
    old = vault / "Old.md"
    old.write_text("# Old Note\n\nSomething from the past.\n")
    old_ts = time.time() - 10 * 86400
    import os
    os.utime(str(old), (old_ts, old_ts))
    return vault


# ── fetch_vault_section ───────────────────────────────────────────────────────


def test_fetch_vault_section_returns_notes(vault):
    notes = briefing.fetch_vault_section(str(vault))
    assert notes is not None
    assert len(notes) >= 1
    assert len(notes) <= 3  # default max_notes=3


def test_fetch_vault_section_omitted_when_empty():
    notes = briefing.fetch_vault_section("")
    assert notes is None


def test_fetch_vault_section_omitted_when_none():
    notes = briefing.fetch_vault_section(None)
    assert notes is None


def test_fetch_vault_section_nonexistent_vault(tmp_path):
    notes = briefing.fetch_vault_section(str(tmp_path / "NoVault"))
    assert notes is None


def test_fetch_vault_section_result_fields(vault):
    notes = briefing.fetch_vault_section(str(vault))
    assert notes is not None and len(notes) > 0
    n = notes[0]
    assert "title" in n
    assert "snippet" in n
    assert "modified" in n


def test_fetch_vault_section_max_3_notes(vault):
    """At most 3 notes, matching the spec: top 3 recent."""
    # Vault has 3 notes; all recent.
    notes = briefing.fetch_vault_section(str(vault), max_notes=3)
    assert notes is not None
    assert len(notes) <= 3


def test_fetch_vault_section_excludes_old_notes(vault_with_old_note):
    """Notes older than the look-back window are excluded."""
    notes = briefing.fetch_vault_section(str(vault_with_old_note), days=3)
    assert notes is not None
    titles = [n["title"] for n in notes]
    assert not any("Old Note" in t for t in titles)


def test_fetch_vault_section_empty_when_all_old(tmp_path):
    """If all notes are outside the window, returns empty list (not None)."""
    import os
    v = tmp_path / "Vault"
    v.mkdir()
    note = v / "Ancient.md"
    note.write_text("# Ancient\n\nVery old.\n")
    old_ts = time.time() - 30 * 86400
    os.utime(str(note), (old_ts, old_ts))

    notes = briefing.fetch_vault_section(str(v), days=3)
    # Returns empty list (vault configured, no recent notes), not None
    assert notes is not None
    assert notes == []


# ── compose_briefing with vault_notes ─────────────────────────────────────────


def test_compose_briefing_no_vault_section():
    """vault_notes=None → no vault heading in output."""
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
        vault_notes=None,
    )
    assert "Notes from your vault" not in msg


def test_compose_briefing_with_vault_notes():
    """vault_notes list → vault heading and note titles appear."""
    notes = [
        {"title": "Project Alpha", "snippet": "Main research note.", "modified": "2026-05-12T08:00:00"},
        {"title": "Ideas", "snippet": "Long list of things.", "modified": "2026-05-11T14:30:00"},
    ]
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
        vault_notes=notes,
    )
    assert "Notes from your vault" in msg
    assert "Project Alpha" in msg
    assert "Ideas" in msg


def test_compose_briefing_vault_empty_list():
    """vault_notes=[] → heading appears but says 'No notes in the last few days'."""
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
        vault_notes=[],
    )
    assert "Notes from your vault" in msg
    assert "No notes" in msg


def test_compose_briefing_vault_snippet_in_standard_detail():
    """Standard detail includes snippet under note title."""
    notes = [{"title": "My Note", "snippet": "First line of content.", "modified": "2026-05-12T10:00:00"}]
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="standard",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
        vault_notes=notes,
    )
    assert "First line of content." in msg


def test_compose_briefing_vault_no_snippet_in_concise():
    """Concise detail: note title appears but NOT snippet."""
    notes = [{"title": "My Note", "snippet": "Detail that should be hidden.", "modified": "2026-05-12T10:00:00"}]
    msg = briefing.compose_briefing(
        user_name="Alex",
        detail="concise",
        calendar_events=[],
        emails=[],
        weather=None,
        news_headlines=[],
        location="",
        vault_notes=notes,
    )
    assert "Notes from your vault" in msg
    assert "My Note" in msg
    assert "Detail that should be hidden." not in msg


# ── End-to-end: argument parser accepts --vault-path ─────────────────────────


def test_cli_parser_accepts_vault_path():
    """briefing.py's argument parser must accept --vault-path without error."""
    parser = briefing._build_parser()
    args = parser.parse_args([
        "preview",
        "--user-name", "Alex",
        "--time-zone", "America/Los_Angeles",
        "--vault-path", "/tmp/MyVault",
    ])
    assert args.vault_path == "/tmp/MyVault"


def test_cli_parser_vault_path_defaults_empty():
    """--vault-path defaults to '' (vault section omitted by default)."""
    parser = briefing._build_parser()
    args = parser.parse_args(["preview", "--user-name", "Alex"])
    assert getattr(args, "vault_path", "") == ""


# ─────────────────────────────────────────────────────────────────────────────
# V1.5-2 should-fix: vault budget cap (regression tests)
# ─────────────────────────────────────────────────────────────────────────────


def test_fetch_vault_section_returns_within_budget(tmp_path):
    """fetch_vault_section must return within budget_seconds + slack.

    Simulates slow I/O by monkeypatching Path.read_text; confirms the budget
    guard fires and returns None rather than hanging the briefing.
    """
    import time as _time
    from pathlib import Path as _Path

    vault = tmp_path / "vault"
    vault.mkdir()
    # Create enough notes to require multiple loop iterations.
    for i in range(5):
        note = vault / f"note{i}.md"
        note.write_text(f"# Note {i}\n\nContent {i}.")
        # Stamp mtime as recent (within 3 days).
        import os
        now_ts = _time.time()
        os.utime(str(note), (now_ts, now_ts))

    # Monkeypatch the per-file read_text to sleep (simulates slow network drive).
    original_read_text = _Path.read_text
    call_count = {"n": 0}

    def slow_read_text(self, *args, **kwargs):
        call_count["n"] += 1
        _time.sleep(0.5)  # 0.5s per file; 5 files = 2.5s total
        return original_read_text(self, *args, **kwargs)

    _Path.read_text = slow_read_text  # type: ignore[method-assign]
    try:
        t0 = _time.monotonic()
        result = briefing.fetch_vault_section(str(vault), budget_seconds=0.6)
        elapsed = _time.monotonic() - t0
    finally:
        _Path.read_text = original_read_text  # type: ignore[method-assign]

    # Should have returned within budget + 1s slack (not 2.5s for all files).
    assert elapsed < 1.6, f"fetch_vault_section took {elapsed:.2f}s, expected < 1.6s"
    # With a 0.6s budget and 0.5s per read, first read may or may not complete.
    # Either way, result should be None (budget exceeded) or a short list.
    assert result is None or isinstance(result, list)


def test_fetch_vault_section_returns_none_on_budget_exceeded(tmp_path, monkeypatch):
    """fetch_vault_section returns None and logs a warning when budget exceeded."""
    import time as _time

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Note\n\nContent.")

    captured_warns = []
    original_warn = briefing._warn

    def capturing_warn(msg):
        captured_warns.append(msg)
        original_warn(msg)

    monkeypatch.setattr(briefing, "_warn", capturing_warn)

    # Use a near-zero budget to force timeout after rglob.
    # We monkeypatch time.monotonic to advance rapidly after the first call.
    call_count = {"n": 0}
    import time as _time_mod
    original_monotonic = _time_mod.monotonic

    start_real = original_monotonic()

    def fast_clock():
        call_count["n"] += 1
        if call_count["n"] <= 1:
            return start_real  # first call: budget start
        return start_real + 100.0  # subsequent calls: far in the future

    monkeypatch.setattr(_time_mod, "monotonic", fast_clock)

    result = briefing.fetch_vault_section(str(vault), budget_seconds=10.0)

    assert result is None
    assert any("budget" in w.lower() for w in captured_warns), (
        f"Expected a budget warning, got: {captured_warns}"
    )
