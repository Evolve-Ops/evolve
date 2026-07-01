"""tests/test_app_posture_reflect.py — LLM reflection layer for the
weekly app posture (PR5).

Covers the things we control (prompt shape, validation, render,
append) and stubs the things we don't (Anthropic API). The actual
LLM behavior is iterated against production data; these tests pin
the contract around it.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _make_posture(*, bot_id: str = "admin_bot", manifests=None, orphans=None,
                  bot_created=None, unmanifested=None):
    """Build a minimal BotPosture object for prompt + validation tests."""
    from app_posture_review import (
        BotPosture, ManifestSummary, OrphanFile, SignalSummary,
    )
    now = _now()
    return BotPosture(
        bot_id=bot_id,
        generated_at=_iso(now),
        window_start=_iso(now - timedelta(days=7)),
        window_end=_iso(now),
        manifests=manifests or [],
        bot_created_signals=bot_created or [],
        unmanifested_signals=unmanifested or [],
        orphan_files=orphans or [],
        workspace_path="/Users/admin_bot/.openclaw/workspace",
        notes=[],
    )


def _manifest(app_id, files=None, **kw):
    from app_posture_review import ManifestSummary
    defaults = dict(
        app_id=app_id, name=app_id.title(), source="bot_created",
        status="active", purpose="", crons_count=0, updated_at="",
        is_recent=True, files=files or [],
    )
    defaults.update(kw)
    return ManifestSummary(**defaults)


def _orphan(path):
    from app_posture_review import OrphanFile
    return OrphanFile(path=path, size=512, mtime_iso=_iso(_now()))


def _bot_created_signal(app_id):
    from app_posture_review import SignalSummary
    return SignalSummary(
        type="bot_created_app",
        signature=f"bot_created_app:admin_bot:{app_id}",
        title=f"admin_bot built {app_id}",
        body="",
        first_observed_at=_iso(_now()),
        last_observed_at=_iso(_now()),
        observation_count=1,
        bot_id="admin_bot",
        details={"app_id": app_id, "session_id": "sess-1"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_prompt_includes_inventory_markdown(self):
        from app_posture_reflect import build_prompt
        posture = _make_posture(manifests=[_manifest("habits", files=["ops/habits.py"])])
        prompt = build_prompt(posture)
        # The inventory section is included verbatim.
        assert "[INVENTORY]" in prompt
        assert "[END INVENTORY]" in prompt
        assert "habits" in prompt
        assert "ops/habits.py" in prompt

    def test_prompt_includes_all_synthesis_questions(self):
        from app_posture_reflect import build_prompt
        prompt = build_prompt(_make_posture())
        # Each section heading the LLM is asked to produce.
        assert "### Clusters" in prompt
        assert "### Splits" in prompt
        assert "### Orphan dispositions" in prompt
        assert "### Missed signals" in prompt
        assert "### Forward guidance for next week" in prompt
        # Word budget is communicated.
        assert "1500 words" in prompt or "under" in prompt.lower()

    def test_prompt_addresses_the_bot_by_id(self):
        from app_posture_reflect import build_prompt
        prompt = build_prompt(_make_posture(bot_id="admin_bot"))
        assert "admin_bot" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateReflection:
    def test_known_app_id_is_clean(self):
        from app_posture_reflect import validate_reflection
        posture = _make_posture(manifests=[_manifest("habits"), _manifest("notes")])
        text = "Consider merging `habits` and `notes` — they overlap heavily."
        report = validate_reflection(text, posture)
        assert report.unknown_app_ids == []
        assert report.unknown_files == []

    def test_unknown_app_id_is_flagged(self):
        """The LLM hallucinates an app_id that isn't in the inventory.
        We don't reject the response — the validation note flags it for
        human review."""
        from app_posture_reflect import validate_reflection
        posture = _make_posture(manifests=[_manifest("habits")])
        text = "Consider merging `habits` and `frobinator`."
        report = validate_reflection(text, posture)
        assert report.unknown_app_ids == ["frobinator"]

    def test_known_file_path_is_clean(self):
        from app_posture_reflect import validate_reflection
        posture = _make_posture(
            manifests=[_manifest("habits", files=["ops/habits.py"])],
            orphans=[_orphan("loose.md")],
        )
        text = "Fold `loose.md` into the existing app, alongside `ops/habits.py`."
        report = validate_reflection(text, posture)
        assert report.unknown_files == []

    def test_unknown_file_path_is_flagged(self):
        from app_posture_reflect import validate_reflection
        posture = _make_posture(orphans=[_orphan("real.md")])
        text = "Fold `nonexistent.md` into the data app."
        report = validate_reflection(text, posture)
        assert report.unknown_files == ["nonexistent.md"]

    def test_does_not_flag_inline_code_tokens(self):
        """Tokens like `record_application` (a plugin tool name) or
        `bot_id` (a field name) shouldn't be treated as app_ids or files
        — they're prose. Only tokens shaped like app_ids or paths are
        candidates for validation."""
        from app_posture_reflect import validate_reflection
        posture = _make_posture(manifests=[_manifest("habits")])
        text = "Call `record_application(update=true)` next time. Note the `bot_id` field."
        report = validate_reflection(text, posture)
        # `record_application` and `update=true` and `bot_id` aren't valid
        # app_id slugs (have underscores or special chars), so they're
        # ignored — not flagged as unknown.
        assert report.unknown_app_ids == []
        assert report.unknown_files == []

    def test_dedups_repeated_unknown_refs(self):
        from app_posture_reflect import validate_reflection
        posture = _make_posture()
        text = "Bad `frobinator` and `frobinator` again, plus `frobinator`."
        report = validate_reflection(text, posture)
        assert report.unknown_app_ids == ["frobinator"]


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderReflectionSection:
    def test_no_validation_issues_renders_clean(self):
        from app_posture_reflect import (
            ValidationReport, render_reflection_section,
        )
        rendered = render_reflection_section(
            "## Reflection\n\n### Clusters\nNone.\n",
            ValidationReport([], []),
        )
        assert "## Reflection" in rendered
        # No validation footer when nothing was flagged.
        assert "Validation notes" not in rendered

    def test_unknown_refs_appear_in_validation_footer(self):
        from app_posture_reflect import (
            ValidationReport, render_reflection_section,
        )
        rendered = render_reflection_section(
            "## Reflection\n\nMerge `frobinator` and `widget`.\n",
            ValidationReport(["frobinator", "widget"], []),
        )
        assert "### Validation notes" in rendered
        assert "frobinator" in rendered
        assert "widget" in rendered

    def test_unknown_files_in_validation_footer(self):
        from app_posture_reflect import (
            ValidationReport, render_reflection_section,
        )
        rendered = render_reflection_section(
            "## Reflection\n\nDelete `bogus.md`.\n",
            ValidationReport([], ["bogus.md"]),
        )
        assert "Unknown file paths" in rendered
        assert "bogus.md" in rendered


# ─────────────────────────────────────────────────────────────────────────────
# Top-level reflect()
# ─────────────────────────────────────────────────────────────────────────────


class TestReflect:
    def test_dry_run_returns_synthetic_reflection(self):
        from app_posture_reflect import reflect
        result = reflect(_make_posture(), dry_run=True)
        assert result.ok is True
        assert result.text and "## Reflection" in result.text
        assert result.error is None
        assert result.model == "dry-run"

    def test_no_api_key_returns_failed_result(self, monkeypatch):
        from app_posture_reflect import reflect
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Block the auth-profiles fallback by pointing at a nonexistent user.
        monkeypatch.setattr("app_posture_reflect._bot_user", lambda b: "user-does-not-exist")
        result = reflect(_make_posture())
        assert result.ok is False
        assert result.text is None
        assert result.error is not None
        assert "API key" in result.error

    def test_api_error_bubbles_to_failed_result(self, monkeypatch):
        from app_posture_reflect import reflect
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        monkeypatch.setattr("app_posture_reflect._call_anthropic", lambda *a, **kw: "")
        result = reflect(_make_posture())
        assert result.ok is False
        assert "empty response" in (result.error or "")

    def test_successful_call_renders_with_validation(self, monkeypatch):
        """End-to-end: stub the LLM, verify the reflection text is
        rendered and validation runs against the posture."""
        from app_posture_reflect import reflect
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        canned = (
            "## Reflection\n\n"
            "### Clusters\nMaybe merge `habits` with `frobinator`.\n\n"
            "### Splits\nNone.\n\n"
            "### Orphan dispositions\nNo orphans.\n\n"
            "### Missed signals\nNone.\n\n"
            "### Forward guidance for next week\nWatch for habit signals.\n"
        )
        monkeypatch.setattr("app_posture_reflect._call_anthropic", lambda *a, **kw: canned)

        posture = _make_posture(manifests=[_manifest("habits")])
        result = reflect(posture)
        assert result.ok is True
        assert result.text is not None
        assert "## Reflection" in result.text
        # Validation flagged the hallucinated `frobinator`.
        assert "Validation notes" in result.text
        assert "frobinator" in result.text


# ─────────────────────────────────────────────────────────────────────────────
# append_reflection_to_doc
# ─────────────────────────────────────────────────────────────────────────────


class TestAppendReflectionToDoc:
    def test_appends_to_existing_doc_and_log(self, tmp_path):
        from app_posture_reflect import append_reflection_to_doc

        doc = tmp_path / "admin_bot.md"
        log = tmp_path / "log.md"
        original = "# App posture — admin_bot\n\nInventory body.\n"
        doc.write_text(original)
        log.write_text(original)

        append_reflection_to_doc(doc, log, "## Reflection\n\nstuff\n", model="claude-haiku-4-5")

        new = doc.read_text()
        assert new.startswith(original)
        assert "## Reflection" in new
        assert "claude-haiku-4-5" in new
        assert log.read_text() == new  # log mirrors doc

    def test_missing_doc_is_no_op(self, tmp_path):
        """If the inventory doc somehow vanished between write_posture and
        the append call, don't crash — silently no-op."""
        from app_posture_reflect import append_reflection_to_doc
        doc = tmp_path / "missing.md"
        log = tmp_path / "missing-log.md"
        # Should not raise.
        append_reflection_to_doc(doc, log, "## Reflection\n", model="x")
        assert not doc.exists()
        assert not log.exists()

    def test_missing_log_is_tolerated(self, tmp_path):
        """The doc append works even if the log file is gone."""
        from app_posture_reflect import append_reflection_to_doc
        doc = tmp_path / "doc.md"
        doc.write_text("# header\n")
        log = tmp_path / "missing-log.md"
        append_reflection_to_doc(doc, log, "## Reflection\n", model="x")
        assert "## Reflection" in doc.read_text()
        assert not log.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Integration: run_once with reflection
# ─────────────────────────────────────────────────────────────────────────────


class TestRunOnceReflection:
    def test_reflect_dry_run_appends_synthetic_reflection(self, monkeypatch, tmp_path):
        """End-to-end: runner gathers, writes posture, runs reflect_dry_run,
        and the synthetic reflection lands in the doc + log."""
        import app_posture_review as apr
        # Stub workspace so orphan walk is a no-op.
        monkeypatch.setattr(apr, "_resolve_bot_workspace", lambda b: None)

        # Synthesize a manifest so the inventory section has content.
        shared_dir = tmp_path
        apps_dir = shared_dir / "applications" / "admin_bot"
        apps_dir.mkdir(parents=True)
        (apps_dir / "habits.json").write_text(json.dumps({
            "id": "habits", "bot_id": "admin_bot", "source": "bot_created",
            "status": "active", "files": [],
            "updated_at": _iso(_now() - timedelta(days=1)),
        }))

        cfg = {
            "bots": {"admin_bot": {}},
            "sharedDir": str(shared_dir),
        }
        totals = apr.run_once(cfg, reflect_override=True, reflect_dry_run=True)
        assert totals["reflections_ok"] == 1
        assert totals["reflections_failed"] == 0

        doc = shared_dir / "app_posture" / "admin_bot.md"
        text = doc.read_text()
        assert "# App posture — admin_bot" in text
        assert "## Reflection" in text
        assert "dry-run" in text  # synthetic placeholder marker

    def test_no_reflect_flag_skips_reflection(self, monkeypatch, tmp_path):
        import app_posture_review as apr
        monkeypatch.setattr(apr, "_resolve_bot_workspace", lambda b: None)
        cfg = {
            "bots": {"admin_bot": {}},
            "sharedDir": str(tmp_path),
            "app_posture": {"reflect_enabled": True},  # would normally enable
        }
        totals = apr.run_once(cfg, reflect_override=False)  # explicit off
        assert totals["reflections_ok"] == 0
        # The doc still exists (PR4 inventory) but with no reflection.
        doc = tmp_path / "app_posture" / "admin_bot.md"
        if doc.exists():
            assert "## Reflection" not in doc.read_text()

    def test_default_off_when_network_flag_unset(self, monkeypatch, tmp_path):
        """No reflect_enabled in network.json → reflection skipped by
        default. Operators have to opt in."""
        import app_posture_review as apr
        monkeypatch.setattr(apr, "_resolve_bot_workspace", lambda b: None)
        cfg = {"bots": {"admin_bot": {}}, "sharedDir": str(tmp_path)}
        totals = apr.run_once(cfg)  # no override; no flag
        assert totals["reflections_ok"] == 0
        assert totals["reflections_failed"] == 0
