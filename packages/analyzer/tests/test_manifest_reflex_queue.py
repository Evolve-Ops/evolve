"""tests/test_manifest_reflex_queue.py — queue helpers.

Mirrors the structure of test_defer_queue: verify read/append/rewrite
behave correctly under EVOLVE_DEFER_HOME_OVERRIDE so we don't touch
real bot homes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def home_override(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EVOLVE_DEFER_HOME_OVERRIDE", tmp)
        yield Path(tmp)


class TestReflexRow:
    def test_to_json_roundtrip(self):
        from manifest_reflex_queue import ReflexRow, new_row
        r = new_row(
            "admin_bot", "protein-tracker",
            name="Protein Tracker", purpose="Daily protein log",
            files=["workspace/ops/tools/protein.py"],
            crons=[{"schedule": "0 21 * * *", "script": "workspace/ops/tools/protein.py"}],
            session_id="s-uuid",
        )
        assert r.bot_id == "admin_bot"
        assert r.app_id == "protein-tracker"
        assert r.status == "pending"
        assert r.update is False
        # Roundtrip
        decoded = ReflexRow.from_dict(json.loads(r.to_json()))
        assert decoded.reflex_id == r.reflex_id
        assert decoded.files == r.files
        assert decoded.crons == r.crons

    def test_new_row_requires_ids(self):
        from manifest_reflex_queue import new_row
        with pytest.raises(ValueError):
            new_row("", "x")
        with pytest.raises(ValueError):
            new_row("admin_bot", "")


class TestQueueIO:
    def test_append_and_read(self, home_override):
        from manifest_reflex_queue import (
            append_row, new_row, queue_path, read_queue,
        )
        r1 = new_row("admin_bot", "protein-tracker")
        r2 = new_row("admin_bot", "habits", purpose="Track daily habits")
        append_row(r1)
        append_row(r2)

        rows = read_queue("admin_bot")
        assert len(rows) == 2
        assert {r.app_id for r in rows} == {"protein-tracker", "habits"}
        # Each row was written as one JSONL line — file should have two lines.
        text = queue_path("admin_bot").read_text()
        assert text.count("\n") == 2

    def test_read_skips_malformed(self, home_override):
        from manifest_reflex_queue import (
            bot_evolve_dir, queue_path, read_queue, append_row, new_row,
        )
        bot_evolve_dir("admin_bot").mkdir(parents=True, exist_ok=True)
        # Write a valid row, then a malformed one, then another valid row.
        append_row(new_row("admin_bot", "first"))
        with queue_path("admin_bot").open("a") as f:
            f.write("{not valid json\n")
            f.write("\n")  # blank line — also tolerated
        append_row(new_row("admin_bot", "third"))

        rows = read_queue("admin_bot")
        assert [r.app_id for r in rows] == ["first", "third"]

    def test_rewrite_drops_processed(self, home_override):
        from manifest_reflex_queue import (
            append_row, new_row, read_queue, rewrite_queue,
        )
        r1 = new_row("admin_bot", "a")
        r2 = new_row("admin_bot", "b")
        r3 = new_row("admin_bot", "c")
        for r in (r1, r2, r3):
            append_row(r)
        # Simulate processing: keep only r2.
        rewrite_queue("admin_bot", [r2])
        remaining = read_queue("admin_bot")
        assert [r.app_id for r in remaining] == ["b"]

    def test_archive_appends(self, home_override):
        from manifest_reflex_queue import (
            append_archive, archive_path, new_row, STATUS_APPLIED,
        )
        r = new_row("admin_bot", "a")
        r.status = STATUS_APPLIED
        r.result = "created"
        append_archive("admin_bot", r)
        # File exists, one row.
        text = archive_path("admin_bot").read_text()
        assert "\"a\"" in text
        assert "\"applied\"" in text


class TestIterBotQueues:
    def test_yields_only_bots_with_queues(self, home_override):
        from manifest_reflex_queue import (
            append_row, iter_bot_queues, new_row,
        )
        append_row(new_row("admin_bot", "x"))
        # No queue file for team_bot_a.
        results = list(iter_bot_queues(["admin_bot", "team_bot_a"]))
        bots = [b for b, _ in results]
        assert bots == ["admin_bot"]
