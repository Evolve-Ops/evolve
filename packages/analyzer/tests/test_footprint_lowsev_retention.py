"""Bounded-retention tests for the 2026-06-28 cross-app anti-pattern batch.

Audit: docs/footprint-antipattern-cross-app-audit-2026-06-28.md. Each LOW-severity
auto-generated surface exhibited the unbounded / no-reader shape; this batch added
a bounded retention or cap. One focused test per surface pins the bound.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import json  # noqa: E402

from signals import retention  # noqa: E402
import recommendations  # noqa: E402


def _annotation_file(shared_dir: Path, bot: str, *, age_days: int) -> Path:
    d = shared_dir / "annotations" / bot
    d.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = d / f"{file_date.isoformat()}.jsonl"
    path.write_text('{"turn": 1}\n', encoding="utf-8")
    return path


# ── Surface 1: turn-annotations (90-day cron prune) ──────────────────────────
def test_annotations_prune_drops_old_keeps_recent(tmp_path):
    keep = _annotation_file(tmp_path, "nova", age_days=10)
    drop = _annotation_file(tmp_path, "nova", age_days=120)
    other_keep = _annotation_file(tmp_path, "atlas", age_days=5)

    result = retention.prune_retention(tmp_path, annotations_days=90)

    assert result.annotations_pruned == 1
    assert keep.exists()
    assert other_keep.exists()
    assert not drop.exists()


def test_annotations_prune_ignores_nonconforming_names(tmp_path):
    d = tmp_path / "annotations" / "nova"
    d.mkdir(parents=True, exist_ok=True)
    weird = d / "not-a-date.jsonl"
    weird.write_text("{}\n", encoding="utf-8")

    result = retention.prune_retention(tmp_path, annotations_days=90)

    assert weird.exists()
    assert result.annotations_kept >= 1


def _candidates_file(shared_dir: Path, sub: str, *, age_days: int) -> Path:
    d = shared_dir / "candidates" / sub
    d.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = d / f"{file_date.isoformat()}.jsonl"
    path.write_text('{"x": 1}\n', encoding="utf-8")
    return path


# ── Surface 2: candidates synthesis_log + dropped (30-day cron prune) ────────
def test_candidates_prune_drops_old_keeps_recent(tmp_path):
    keep_syn = _candidates_file(tmp_path, "synthesis_log", age_days=5)
    drop_syn = _candidates_file(tmp_path, "synthesis_log", age_days=45)
    keep_drop = _candidates_file(tmp_path, "dropped", age_days=10)
    drop_drop = _candidates_file(tmp_path, "dropped", age_days=60)

    result = retention.prune_retention(tmp_path, candidates_days=30)

    assert result.candidates_pruned == 2
    assert keep_syn.exists() and keep_drop.exists()
    assert not drop_syn.exists() and not drop_drop.exists()


# ── Surface 3: recommendations log.jsonl (90d line-cap + diff-gate) ──────────
def _read_events(log_path: Path) -> list[str]:
    return [
        json.loads(line)["event"]
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


def test_recommendations_log_diff_gate_suppresses_noop_updated(tmp_path):
    bot = "nova"
    rec = {"rec_id": "r1", "title": "T", "category": "c", "type": "x"}

    recommendations.write_recommendations(bot, [dict(rec)], tmp_path)        # created
    recommendations.write_recommendations(bot, [dict(rec)], tmp_path)        # no-op
    recommendations.write_recommendations(
        bot, [{**rec, "title": "T2"}], tmp_path                              # real change
    )

    events = _read_events(recommendations._log_path(bot, tmp_path))
    assert events.count("created") == 1
    assert events.count("updated") == 1  # the no-op second write is gated out


def test_recommendations_prune_log_drops_old_lines(tmp_path):
    log = tmp_path / "log.jsonl"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    new_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    log.write_text(
        json.dumps({"event": "created", "ts": old_ts}) + "\n"
        + json.dumps({"event": "updated", "ts": new_ts}) + "\n"
        + "{not json}\n",  # unparseable — kept, never risk dropping
        encoding="utf-8",
    )

    recommendations._prune_log(log, retain_days=90)

    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert old_ts not in log.read_text()
    assert new_ts in log.read_text()


# ── Surface 4: cascade labels (dedup-on-write + 90d cron prune) ──────────────
def test_cascade_labels_dedup_on_write_collapses_same_session(tmp_path):
    from cascade.labeler import write_labels, LabeledOutcome, Label, LabelSource

    def _o(sid, conf):
        return LabeledOutcome(
            session_id=sid, bot_id="team_bot_a", label=Label.CORRECTLY_ESCALATED,
            confidence=conf, source=LabelSource.STRUGGLE_RESOLUTION,
        )

    write_labels([_o("s1", 0.5)], tmp_path, day="2026-05-27")
    write_labels([_o("s1", 0.9)], tmp_path, day="2026-05-27")  # re-label same session

    path = tmp_path / "cascade" / "labels" / "2026-05-27.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1  # dedup collapsed the duplicate session
    assert json.loads(lines[0])["confidence"] == 0.9  # latest wins


def _cascade_label_file(shared_dir: Path, *, age_days: int) -> Path:
    d = shared_dir / "cascade" / "labels"
    d.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = d / f"{file_date.isoformat()}.jsonl"
    path.write_text('{"session_id": "s1"}\n', encoding="utf-8")
    return path


def test_cascade_labels_prune_drops_old(tmp_path):
    keep = _cascade_label_file(tmp_path, age_days=10)
    drop = _cascade_label_file(tmp_path, age_days=120)

    result = retention.prune_retention(tmp_path, cascade_labels_days=90)

    assert result.cascade_labels_pruned == 1
    assert keep.exists() and not drop.exists()


# ── Surface 5: weekly reviews (keep-newest-12) ───────────────────────────────
def test_weekly_review_keeps_newest_twelve(tmp_path):
    from datetime import date as _date

    import weekly_review

    # Write 15 weekly reports on distinct (descending) dates.
    for i in range(15):
        d = _date(2026, 1, 1) + timedelta(weeks=i)
        weekly_review.write_report(f"report {i}", tmp_path, d)

    reviews = sorted((tmp_path / "reviews").glob("*.md"))
    assert len(reviews) == weekly_review._KEEP_REVIEWS  # 12
    # The newest 12 survive; the 3 oldest are pruned.
    assert reviews[0].stem == (_date(2026, 1, 1) + timedelta(weeks=3)).isoformat()


# ── Surface 6: fit-review trail (soft line-cap 1000 -> 500) ──────────────────
def test_fit_review_trail_soft_line_cap(tmp_path):
    from fit_review import runner

    workspace = tmp_path
    # 1001 appends should trip the soft cap down to the most-recent 500.
    for i in range(1001):
        runner._append_trail(workspace, {"run": i})

    trail = runner._trail_path(workspace)
    lines = [ln for ln in trail.read_text().splitlines() if ln.strip()]
    assert len(lines) == 500
    assert json.loads(lines[-1])["run"] == 1000  # newest retained


# ── Surface 7: app-posture per-week log copies (keep-newest-12) ──────────────
def test_app_posture_log_keeps_newest_twelve(tmp_path):
    from datetime import datetime as _dt

    from app_posture_review import BotPosture, write_posture, posture_log_path

    bot = "admin_bot"
    for i in range(15):
        when = _dt(2026, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=i)
        posture = BotPosture(
            bot_id=bot, generated_at=when.isoformat(),
            window_start=(when - timedelta(days=7)).isoformat(),
            window_end=when.isoformat(),
            manifests=[], bot_created_signals=[], unmanifested_signals=[],
            orphan_files=[], workspace_path=None, notes=[],
        )
        write_posture(posture, tmp_path)

    log_dir = posture_log_path(tmp_path, bot).parent
    logs = sorted(log_dir.glob("*.md"))
    assert len(logs) == 12  # _KEEP_POSTURE_LOGS
    # canonical doc is untouched by the log prune
    assert (tmp_path / "app_posture" / f"{bot}.md").exists()
