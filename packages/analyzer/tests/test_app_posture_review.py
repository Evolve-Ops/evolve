"""tests/test_app_posture_review.py — Weekly app-posture inventory layer.

Spec: docs/spec-manifest-reflex.md §"App posture review (PR4)".

Covers the gather + render layer. PR4 has no LLM — the LLM reflection
that lands on top of these snapshots is PR5's job.

Test surface:
  1. _collect_manifests respects window + handles missing/empty dirs
  2. _collect_signals filters by producer + bot_id + window
  3. _collect_orphan_files skips system surfaces, dedups against
     manifest paths in both v4 (str) and v5 (dict) shapes
  4. render_posture_markdown produces stable section headings
  5. write_posture creates the canonical doc + the per-week log copy
  6. End-to-end gather_bot_posture against a synthesized shared_dir
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture()
def workspace(tmp_path):
    """Tmp workspace dir we can populate with files and dirs."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_manifest(shared_dir: Path, bot_id: str, app_id: str, *,
                    files=None, source="bot_created", updated_at=None,
                    purpose="", crons=None, status="active") -> Path:
    apps_dir = shared_dir / "applications" / bot_id
    apps_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": app_id,
        "name": app_id.replace("-", " ").title(),
        "bot_id": bot_id,
        "source": source,
        "status": status,
        "purpose": purpose,
        "files": files or [],
        "crons": crons or [],
        "updated_at": updated_at or _iso(_now()),
        "created_at": updated_at or _iso(_now()),
    }
    p = apps_dir / f"{app_id}.json"
    p.write_text(json.dumps(data))
    return p


# ── _collect_manifests ───────────────────────────────────────────────────────


class TestCollectManifests:
    def test_empty_dir_returns_empty_list(self, shared_dir):
        from app_posture_review import _collect_manifests
        manifests = _collect_manifests(shared_dir, "admin_bot", _now() - timedelta(days=7))
        assert manifests == []

    def test_recent_manifest_marked_recent(self, shared_dir):
        from app_posture_review import _collect_manifests
        # Updated yesterday — within 7-day window.
        _write_manifest(shared_dir, "admin_bot", "habits",
                        updated_at=_iso(_now() - timedelta(days=1)))
        manifests = _collect_manifests(shared_dir, "admin_bot", _now() - timedelta(days=7))
        assert len(manifests) == 1
        assert manifests[0].is_recent is True
        assert manifests[0].app_id == "habits"

    def test_old_manifest_not_marked_recent(self, shared_dir):
        from app_posture_review import _collect_manifests
        _write_manifest(shared_dir, "admin_bot", "ancient",
                        updated_at=_iso(_now() - timedelta(days=30)))
        manifests = _collect_manifests(shared_dir, "admin_bot", _now() - timedelta(days=7))
        assert len(manifests) == 1
        assert manifests[0].is_recent is False

    def test_skips_history_subdir(self, shared_dir):
        """_history/ holds manifest archives — must not appear as
        first-class apps."""
        from app_posture_review import _collect_manifests
        apps_dir = shared_dir / "applications" / "admin_bot"
        history = apps_dir / "_history"
        history.mkdir(parents=True)
        (history / "old-version.json").write_text(json.dumps(
            {"id": "old-version", "bot_id": "admin_bot"}
        ))
        _write_manifest(shared_dir, "admin_bot", "current")
        manifests = _collect_manifests(shared_dir, "admin_bot", _now() - timedelta(days=7))
        assert {m.app_id for m in manifests} == {"current"}

    def test_extracts_v5_file_dicts(self, shared_dir):
        from app_posture_review import _collect_manifests
        _write_manifest(shared_dir, "admin_bot", "v5-app",
                        files=[
                            {"file_id": "f-1", "path": "ops/x.py"},
                            {"file_id": "f-2", "path": "ops/y.py"},
                        ])
        manifests = _collect_manifests(shared_dir, "admin_bot", _now() - timedelta(days=7))
        assert manifests[0].files == ["ops/x.py", "ops/y.py"]


# ── _collect_signals ─────────────────────────────────────────────────────────


class TestSignalSummaryId:
    """PR10: SignalSummary now carries the underlying Signal.id so
    proposals built from posture data can link back to the originating
    bot_created_app Signal in the alerts UI."""

    def test_collect_signals_populates_id_from_store(self, shared_dir):
        """The id field on SignalSummary should match Signal.id read
        from the on-disk signal store."""
        from app_posture_review import _collect_signals
        from signals.store import find_active_by_signature, observe

        observe(
            shared_dir,
            signature="bot_created_app:admin_bot:habits",
            producer="manifest_reflex_runner", type="bot_created_app",
            flavor="activity", severity="info", scope="bot",
            bot_id="admin_bot", title="t", body="",
            details={"app_id": "habits"},
        )
        # Pull the just-created signal back to discover its id; that's
        # what _collect_signals should report on the SignalSummary.
        underlying = find_active_by_signature(
            shared_dir, "bot_created_app:admin_bot:habits"
        )
        assert underlying is not None
        expected_id = underlying.id
        assert expected_id  # store assigns a non-empty id

        summaries = _collect_signals(
            shared_dir, "admin_bot", _now() - timedelta(days=7),
            producer="manifest_reflex_runner",
        )
        assert len(summaries) == 1
        assert summaries[0].id == expected_id


class TestCollectSignals:
    def test_filters_by_producer_and_bot(self, shared_dir):
        """Walks store entries; only matching producer + bot_id come back."""
        from app_posture_review import _collect_signals
        from signals.store import observe

        # Within window, matching producer+bot.
        observe(shared_dir, signature="bot_created_app:admin_bot:a",
                producer="manifest_reflex_runner", type="bot_created_app",
                flavor="activity", severity="info", scope="bot",
                bot_id="admin_bot", title="t1", body="", details={"app_id": "a"})
        # Different producer — must be excluded.
        observe(shared_dir, signature="other:admin_bot:b",
                producer="some_other_producer", type="other",
                flavor="activity", severity="info", scope="bot",
                bot_id="admin_bot", title="t2", body="")
        # Different bot — must be excluded.
        observe(shared_dir, signature="bot_created_app:team_bot_a:c",
                producer="manifest_reflex_runner", type="bot_created_app",
                flavor="activity", severity="info", scope="bot",
                bot_id="team_bot_a", title="t3", body="")

        sigs = _collect_signals(shared_dir, "admin_bot",
                                _now() - timedelta(days=7),
                                producer="manifest_reflex_runner")
        assert len(sigs) == 1
        assert sigs[0].details["app_id"] == "a"

    def test_filters_by_window(self, shared_dir):
        """A signal whose last_observed_at is older than the window is
        excluded — old events shouldn't dominate this week's posture."""
        from app_posture_review import _collect_signals
        from signals.store import observe, write_signal, iter_signals

        observe(shared_dir, signature="bot_created_app:admin_bot:old",
                producer="manifest_reflex_runner", type="bot_created_app",
                flavor="activity", severity="info", scope="bot",
                bot_id="admin_bot", title="old", body="")

        # Backdate last_observed_at to 30 days ago. find_signal() takes
        # the signal *id*, not the signature — locate by iterating firing/.
        sig = next(s for s in iter_signals(shared_dir, subdirs=("firing",))
                   if s.signature == "bot_created_app:admin_bot:old")
        sig.last_observed_at = _iso(_now() - timedelta(days=30))
        write_signal(sig, shared_dir)

        sigs = _collect_signals(shared_dir, "admin_bot",
                                _now() - timedelta(days=7),
                                producer="manifest_reflex_runner")
        assert sigs == []


# ── _collect_orphan_files ────────────────────────────────────────────────────


class TestCollectOrphanFiles:
    def test_finds_files_not_in_any_manifest(self, workspace):
        from app_posture_review import ManifestSummary, _collect_orphan_files

        (workspace / "ops").mkdir()
        (workspace / "ops" / "claimed.py").write_text("# in manifest\n")
        (workspace / "ops" / "orphan.py").write_text("# not in any manifest\n")
        (workspace / "loose-notes.md").write_text("scratch notes")

        manifests = [
            ManifestSummary(
                app_id="some-app", name="Some App", source="bot_created",
                status="active", purpose="", files=["ops/claimed.py"],
                crons_count=0, updated_at="", is_recent=True,
            )
        ]
        orphans = _collect_orphan_files(workspace, manifests)
        rel_paths = sorted(o.path for o in orphans)
        assert rel_paths == ["loose-notes.md", "ops/orphan.py"]

    def test_strips_workspace_prefix_when_comparing(self, workspace):
        """Bots sometimes record file paths as 'workspace/ops/x.py'; the
        orphan check must normalize to the workspace-relative form."""
        from app_posture_review import ManifestSummary, _collect_orphan_files

        (workspace / "ops").mkdir()
        (workspace / "ops" / "x.py").write_text("# claimed via workspace/-prefixed path\n")

        manifests = [
            ManifestSummary(
                app_id="x", name="X", source="bot_created", status="active",
                purpose="", files=["workspace/ops/x.py"], crons_count=0,
                updated_at="", is_recent=True,
            )
        ]
        orphans = _collect_orphan_files(workspace, manifests)
        assert orphans == []

    def test_skips_system_surfaces(self, workspace):
        """OS/OC infrastructure dirs and files must never be flagged as
        app-shaped orphans — keeps the doc focused on real apps."""
        from app_posture_review import _collect_orphan_files

        (workspace / ".git").mkdir()
        (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (workspace / "evolve").mkdir()
        (workspace / "evolve" / "manifest-reflex-queue.jsonl").write_text("{}\n")
        (workspace / "AGENTS.md").write_text("# bot system file\n")
        (workspace / ".dotfile").write_text("hidden\n")
        # One real orphan to prove the walk works.
        (workspace / "ops.py").write_text("# real orphan\n")

        orphans = _collect_orphan_files(workspace, manifests=[])
        rel_paths = [o.path for o in orphans]
        assert rel_paths == ["ops.py"]

    def test_skips_zero_byte_placeholders(self, workspace):
        from app_posture_review import _collect_orphan_files

        (workspace / "empty.txt").write_bytes(b"")
        (workspace / "real.py").write_text("# content\n")

        orphans = _collect_orphan_files(workspace, manifests=[])
        assert {o.path for o in orphans} == {"real.py"}

    def test_truncates_at_50_orphans(self, workspace):
        """Bots with thousands of orphans need scanner-driven cleanup,
        not a posture doc that's mostly orphan list. The cap keeps the
        rendered doc bounded."""
        from app_posture_review import _collect_orphan_files

        for i in range(60):
            (workspace / f"file-{i:02d}.py").write_text(f"# orphan {i}\n")

        orphans = _collect_orphan_files(workspace, manifests=[])
        # 50 real entries + 1 truncation note.
        assert len(orphans) == 51
        assert any(o.path.startswith("…") for o in orphans)

    def test_missing_workspace_returns_empty(self, tmp_path):
        from app_posture_review import _collect_orphan_files
        nonexistent = tmp_path / "does-not-exist"
        assert _collect_orphan_files(nonexistent, manifests=[]) == []

    def test_excluded_paths_skipped(self, workspace):
        """PR9: paths the operator previously approved a RetireOrphan on
        should be skipped by the orphan walk so they don't reappear in
        the next reflection."""
        from app_posture_review import _collect_orphan_files

        (workspace / "still-orphan.md").write_text("orphan\n")
        (workspace / "retired.md").write_text("previously retired\n")

        # Without exclusions: both surface.
        all_orphans = _collect_orphan_files(workspace, manifests=[])
        assert {o.path for o in all_orphans} == {"still-orphan.md", "retired.md"}

        # With exclusions: only the un-retired one surfaces.
        filtered = _collect_orphan_files(
            workspace, manifests=[], excluded_paths={"retired.md"},
        )
        assert {o.path for o in filtered} == {"still-orphan.md"}


# ── render_posture_markdown ──────────────────────────────────────────────────


class TestRenderPostureMarkdown:
    def _basic_posture(self):
        from app_posture_review import (
            BotPosture, ManifestSummary, OrphanFile, SignalSummary,
        )
        now = _now()
        return BotPosture(
            bot_id="admin_bot",
            generated_at=_iso(now),
            window_start=_iso(now - timedelta(days=7)),
            window_end=_iso(now),
            manifests=[
                ManifestSummary(app_id="habits", name="Habits", source="bot_created",
                                status="active", purpose="Track daily habits",
                                files=["ops/habits.py"], crons_count=1,
                                updated_at=_iso(now - timedelta(days=2)), is_recent=True),
                ManifestSummary(app_id="ancient", name="Ancient", source="discovered",
                                status="active", purpose="legacy", files=[],
                                crons_count=0,
                                updated_at=_iso(now - timedelta(days=60)), is_recent=False),
            ],
            bot_created_signals=[
                SignalSummary(type="bot_created_app", signature="bot_created_app:admin_bot:habits",
                              title="admin_bot built Habits mid-session", body="Daily habits log",
                              first_observed_at=_iso(now - timedelta(days=2)),
                              last_observed_at=_iso(now - timedelta(days=2)),
                              observation_count=1, bot_id="admin_bot",
                              details={"app_id": "habits", "session_id": "sess-1",
                                       "purpose": "Track daily habits"}),
            ],
            unmanifested_signals=[],
            orphan_files=[
                OrphanFile(path="loose-notes.md", size=1024,
                           mtime_iso=_iso(now - timedelta(days=1))),
            ],
            workspace_path="/Users/admin_bot/.openclaw/workspace",
            notes=["test note about deferred cron orphans"],
        )

    def test_contains_stable_section_headings(self):
        from app_posture_review import render_posture_markdown
        md = render_posture_markdown(self._basic_posture())
        # Stable headings let PR5's reflection layer locate where to
        # append without re-parsing the whole document.
        assert "# App posture — admin_bot" in md
        assert "## This week" in md
        assert "## You self-recorded 1 app(s) this week" in md
        assert "## Orphan files (1 not in any manifest)" in md
        assert "## Inventory summary" in md
        assert "## Caveats" in md

    def test_lists_only_recent_apps_in_this_week(self):
        from app_posture_review import render_posture_markdown
        md = render_posture_markdown(self._basic_posture())
        # `habits` (recent) is in This week; `ancient` is not but should
        # still be counted in the unchanged tally.
        assert "`habits`" in md
        # The tail of the heading marks ancient as quiet.
        assert "1 app(s) unchanged this week" in md

    def test_renders_inventory_summary(self):
        from app_posture_review import render_posture_markdown
        md = render_posture_markdown(self._basic_posture())
        assert "Total apps: 2" in md
        # Two distinct sources represented.
        assert "bot_created: 1" in md
        assert "discovered: 1" in md


# ── End-to-end ────────────────────────────────────────────────────────────────


class TestGatherBotPosture:
    def test_synthesized_world(self, shared_dir, monkeypatch, tmp_path):
        """Build a tiny shared_dir + workspace and check the gathered
        posture has the expected shape end-to-end."""
        from signals.store import observe
        import app_posture_review as apr

        # 1. Manifest, recent.
        _write_manifest(shared_dir, "admin_bot", "habits",
                        files=["ops/habits.py"],
                        updated_at=_iso(_now() - timedelta(days=1)))

        # 2. Signal from manifest_reflex_runner.
        observe(shared_dir, signature="bot_created_app:admin_bot:habits",
                producer="manifest_reflex_runner", type="bot_created_app",
                flavor="activity", severity="info", scope="bot",
                bot_id="admin_bot", title="admin_bot built habits", body="",
                details={"app_id": "habits", "session_id": "s-1"})

        # 3. Workspace with one claimed file + one orphan.
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "ops").mkdir()
        (ws / "ops" / "habits.py").write_text("# claimed by habits manifest\n")
        (ws / "loose.md").write_text("# orphan\n")
        monkeypatch.setattr(apr, "_resolve_bot_workspace", lambda bot_id: ws)

        posture = apr.gather_bot_posture("admin_bot", shared_dir)

        # Manifest came through.
        assert [m.app_id for m in posture.manifests] == ["habits"]
        assert posture.manifests[0].is_recent is True
        # Signal came through.
        assert len(posture.bot_created_signals) == 1
        assert posture.bot_created_signals[0].details["app_id"] == "habits"
        # Workspace orphan walk found `loose.md` but not `ops/habits.py`.
        rel_paths = [o.path for o in posture.orphan_files if o.size > 0]
        assert rel_paths == ["loose.md"]

        # Notes capture the deferred cron-orphan caveat.
        assert any("cron orphans" in n for n in posture.notes)


# ── write_posture ────────────────────────────────────────────────────────────


class TestWritePosture:
    def test_writes_doc_and_log(self, shared_dir):
        from app_posture_review import (
            BotPosture, write_posture, posture_doc_path, posture_log_path,
        )
        now = _now()
        posture = BotPosture(
            bot_id="admin_bot", generated_at=_iso(now),
            window_start=_iso(now - timedelta(days=7)),
            window_end=_iso(now),
            manifests=[], bot_created_signals=[], unmanifested_signals=[],
            orphan_files=[], workspace_path=None, notes=[],
        )
        doc, log = write_posture(posture, shared_dir)

        assert doc == posture_doc_path(shared_dir, "admin_bot")
        assert doc.exists()
        assert "# App posture — admin_bot" in doc.read_text()

        # Log copy under app_posture/admin_bot/log/<YYYY-MM-DD>.md.
        assert log.exists()
        assert log.read_text() == doc.read_text()
        assert log.parent.parent == shared_dir / "app_posture" / "admin_bot"


# ── Bot enumeration ──────────────────────────────────────────────────────────


class TestBotIdParsing:
    def test_dict_shape(self):
        from app_posture_review import _bot_ids
        assert _bot_ids({"bots": {"admin_bot": {}, "team_bot_a": {}}}) == ["admin_bot", "team_bot_a"]

    def test_list_shape(self):
        from app_posture_review import _bot_ids
        assert _bot_ids({"bots": [{"id": "admin_bot"}]}) == ["admin_bot"]

    def test_empty(self):
        from app_posture_review import _bot_ids
        assert _bot_ids({}) == []
