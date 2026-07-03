"""tests/test_audit_outbox_retention.py — audit_outbox/_ingested pruner.

Mirrors test_signals_phase6_retention.py: fixtures create old + recent
date-dirs, assert old pruned + recent kept, live root untouched,
idempotent, and the --days boundary. Uses the injectable now=.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from signals import audit_outbox_retention as aor  # noqa: E402
from signals import retention  # noqa: E402


_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _ingested_date_dir(root: Path, *, age_days: int) -> Path:
    """Create root/_ingested/<date>/ backdated age_days, with one record."""
    file_date = (_NOW - timedelta(days=age_days)).date()
    day_dir = root / "_ingested" / file_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "rec-x.json").write_text("{}", encoding="utf-8")
    return day_dir


def _bot_outbox_root(home: Path) -> Path:
    return home / ".openclaw" / "workspace" / "evolve" / "audit_outbox"


def _infra_outbox_root(shared_dir: Path) -> Path:
    return shared_dir / "infra_audit_outbox"


# ── infra (pod-wide) target ───────────────────────────────────────────────────


def test_prunes_old_keeps_recent_infra(tmp_path):
    root = _infra_outbox_root(tmp_path)
    old = _ingested_date_dir(root, age_days=120)
    recent = _ingested_date_dir(root, age_days=5)

    result = aor.prune_audit_outbox(
        tmp_path, days=30, config={"bots": {}}, now=_NOW
    )
    assert result.infra_dirs_pruned == 1
    assert result.infra_dirs_kept == 1
    assert not old.exists()
    assert recent.exists()


def test_live_infra_outbox_root_untouched(tmp_path):
    """Un-drained records in the live outbox root are never touched."""
    root = _infra_outbox_root(tmp_path)
    _ingested_date_dir(root, age_days=120)
    live_rec = root / "undrained-rec.json"
    live_rec.write_text("{}", encoding="utf-8")
    live_subdir = root / "some-other-thing"
    live_subdir.mkdir()
    (live_subdir / "f.json").write_text("{}", encoding="utf-8")

    aor.prune_audit_outbox(tmp_path, days=30, config={"bots": {}}, now=_NOW)

    assert live_rec.exists(), "live un-drained record must survive"
    assert live_subdir.exists(), "non-_ingested sibling dir must survive"


# ── per-bot target ────────────────────────────────────────────────────────────


def test_prunes_old_keeps_recent_per_bot(tmp_path, monkeypatch):
    home = tmp_path / "home" / "team-bot-a"
    root = _bot_outbox_root(home)
    old = _ingested_date_dir(root, age_days=120)
    recent = _ingested_date_dir(root, age_days=5)
    live_rec = root / "undrained.json"
    live_rec.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(aor.evolve_config, "bot_home", lambda bot_id, cfg=None: home)

    result = aor.prune_audit_outbox(
        tmp_path,
        days=30,
        config={"bots": {"team-bot-a": {"user": "team-bot-a"}}},
        now=_NOW,
    )
    assert result.bots_scanned == 1
    assert result.bot_dirs_pruned == 1
    assert result.bot_dirs_kept == 1
    assert not old.exists()
    assert recent.exists()
    assert live_rec.exists()


# ── shared idiom: idempotent, boundary, malformed, no-op ──────────────────────


def test_idempotent(tmp_path):
    root = _infra_outbox_root(tmp_path)
    _ingested_date_dir(root, age_days=120)

    r1 = aor.prune_audit_outbox(tmp_path, days=30, config={"bots": {}}, now=_NOW)
    r2 = aor.prune_audit_outbox(tmp_path, days=30, config={"bots": {}}, now=_NOW)
    assert r1.infra_dirs_pruned == 1
    assert r2.infra_dirs_pruned == 0


def test_days_boundary(tmp_path):
    """A dir exactly `days` old is kept; strictly older is pruned."""
    root = _infra_outbox_root(tmp_path)
    on_edge = _ingested_date_dir(root, age_days=30)   # cutoff == its date → kept
    just_over = _ingested_date_dir(root, age_days=31)  # older than cutoff → pruned

    result = aor.prune_audit_outbox(
        tmp_path, days=30, config={"bots": {}}, now=_NOW
    )
    assert on_edge.exists()
    assert not just_over.exists()
    assert result.infra_dirs_pruned == 1
    assert result.infra_dirs_kept == 1


def test_malformed_date_dir_kept(tmp_path):
    """A non-date dir name under _ingested/ is skipped, never deleted."""
    root = _infra_outbox_root(tmp_path)
    weird = root / "_ingested" / "not-a-date"
    weird.mkdir(parents=True)
    (weird / "f.json").write_text("{}", encoding="utf-8")

    result = aor.prune_audit_outbox(
        tmp_path, days=30, config={"bots": {}}, now=_NOW
    )
    assert weird.exists()
    assert result.infra_dirs_kept == 1
    assert result.infra_dirs_pruned == 0


def test_no_ingested_dir_is_noop(tmp_path):
    result = aor.prune_audit_outbox(
        tmp_path, days=30, config={"bots": {}}, now=_NOW
    )
    assert result.infra_dirs_pruned == 0
    assert result.bot_dirs_pruned == 0


def test_custom_days_window(tmp_path):
    root = _infra_outbox_root(tmp_path)
    d10 = _ingested_date_dir(root, age_days=10)
    d3 = _ingested_date_dir(root, age_days=3)

    # 7-day window: 10d pruned, 3d kept.
    result = aor.prune_audit_outbox(
        tmp_path, days=7, config={"bots": {}}, now=_NOW
    )
    assert not d10.exists()
    assert d3.exists()
    assert result.infra_dirs_pruned == 1


# ── wiring: prune_retention() drives the audit_outbox prune ───────────────────


def test_prune_retention_includes_audit_outbox(tmp_path, monkeypatch):
    """The canonical daily pruner threads through the audit_outbox prune."""
    # Hermetic: no real network.json bots enumerated against the dev box.
    monkeypatch.setattr(aor.evolve_config, "load_config", lambda *a, **k: {"bots": {}})

    root = _infra_outbox_root(tmp_path)
    old = _ingested_date_dir(root, age_days=120)
    recent = _ingested_date_dir(root, age_days=5)

    result = retention.prune_retention(
        tmp_path, audit_outbox_days=30, now=_NOW
    )
    assert result.audit_outbox_infra_dirs_pruned == 1
    assert result.audit_outbox_infra_dirs_kept == 1
    assert not old.exists()
    assert recent.exists()
