"""Phase 6 of the 2026-06 cost-cap normalization (spec:
internal/spec-cost-caps-2026-06-05.md).

Pins the graduated remediation ladder added to spend_alert:
- ``_resolve_per_bot_caps`` returns all six threshold values from BE config
- ``_apply_tier_downgrade`` writes the bot's primary model + dedupes per day
- ``_trip_l2_breaker_for_cost_cap`` writes a cost_l2 breaker record + bootouts
- Dedup behavior for tier_downgrade (per-day flag file) and L2 (breaker file)

End-to-end orchestration via the main loop is exercised by
test_spend_alert_per_bot_cap.py (existing). This file isolates the new
phase-6 helpers so a regression in any one tier is caught without
re-running the full dispatcher fan-out.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spend_alert  # noqa: E402


# ─── _resolve_per_bot_caps: returns all 6 tiers ────────────────────────────


@pytest.fixture
def fake_shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _write_be(shared_dir: Path, bot_id: str, **budget) -> None:
    payload = {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {bot_id: {"budget": budget}},
    }
    (shared_dir / "better-engine-config.json").write_text(json.dumps(payload))


def test_resolve_caps_returns_six_keys(fake_shared):
    caps = spend_alert._resolve_per_bot_caps("team_bot_a", fake_shared)
    assert set(caps.keys()) == {
        "daily_warn", "weekly_warn", "tier_downgrade",
        "l1_breaker", "l2_breaker", "per_session",
    }


def test_resolve_caps_uses_compiled_defaults_when_no_config_file(fake_shared):
    """No config file → ``load()`` hands back the compiled product defaults,
    and the ladder now honors them as the pod-default layer.

    This used to be an all-None ladder — i.e. no enforcement at any rung on a
    pod with no config — while ``spend_caps.get_caps_config`` read the same
    compiled defaults and told the operator a $5 cap was in force. Enforcement
    matching the displayed cap is the point (see
    test_cost_cap_reader_parity.py). Rungs that are opt-in with no compiled
    value stay None.
    """
    caps = spend_alert._resolve_per_bot_caps("team_bot_a", fake_shared)
    assert caps["daily_warn"] == 2.00   # _COMPILED_DEFAULTS
    assert caps["l1_breaker"] == 5.00   # _COMPILED_DEFAULTS
    assert caps["weekly_warn"] is None
    assert caps["tier_downgrade"] is None
    assert caps["l2_breaker"] is None
    assert caps["per_session"] is None


def test_resolve_caps_all_none_when_pod_defaults_empty(fake_shared):
    """A config file with an empty ``pod_defaults`` genuinely means no
    enforcement — the fallback is to what is stored, not past it."""
    (fake_shared / "better-engine-config.json").write_text(
        json.dumps({"schema_version": 1, "pod_defaults": {}, "bots": {}})
    )
    caps = spend_alert._resolve_per_bot_caps("team_bot_a", fake_shared)
    assert all(v is None for v in caps.values())


def test_resolve_caps_returns_set_values(fake_shared):
    _write_be(
        fake_shared, "team_bot_a",
        per_bot_daily_warn_usd=2.0,
        weekly_warn_usd=10.0,
        tier_downgrade_usd=5.0,
        per_bot_daily_hard_usd=8.0,
        l2_breaker_usd=20.0,
        per_bot_session_cost_cap_usd=3.0,
    )
    caps = spend_alert._resolve_per_bot_caps("team_bot_a", fake_shared)
    assert caps == {
        "daily_warn": 2.0,
        "weekly_warn": 10.0,
        "tier_downgrade": 5.0,
        "l1_breaker": 8.0,
        "l2_breaker": 20.0,
        "per_session": 3.0,
    }


def test_resolve_caps_zero_negative_unparseable_read_as_none(fake_shared):
    _write_be(
        fake_shared, "team_bot_a",
        per_bot_daily_warn_usd=0,
        tier_downgrade_usd=-1.0,
        l2_breaker_usd="not-a-number",
    )
    caps = spend_alert._resolve_per_bot_caps("team_bot_a", fake_shared)
    assert caps["daily_warn"] is None
    assert caps["tier_downgrade"] is None
    assert caps["l2_breaker"] is None


def test_resolve_caps_none_when_shared_dir_none():
    caps = spend_alert._resolve_per_bot_caps("team_bot_a", None)
    assert all(v is None for v in caps.values())


def test_resolve_per_bot_cap_singular_returns_l1(fake_shared):
    """The legacy ``_resolve_per_bot_cap`` callers see the L1 tier."""
    _write_be(fake_shared, "team_bot_a", per_bot_daily_hard_usd=8.0)
    assert spend_alert._resolve_per_bot_cap(
        "team_bot_a", {}, fake_shared,
    ) == 8.0


# ─── _apply_tier_downgrade: writes model.primary + dedupes ──────────────────


@pytest.fixture
def fake_network() -> dict:
    return {
        "primary": "team_bot_a",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }


def test_tier_downgrade_writes_openclaw_cost_settings(
    fake_shared, fake_network, monkeypatch,
):
    """The writer call carries the tier-3 model id under
    agents.defaults.model.primary."""
    seen: list[dict] = []

    def fake_write(bot_id, settings):
        seen.append({"bot_id": bot_id, "settings": settings})
        return True, ""

    monkeypatch.setattr(spend_alert, "_TIER_DOWNGRADE_WRITER", fake_write)
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso="2026-06-05",
    )

    assert len(seen) == 1
    assert seen[0]["bot_id"] == "team_bot_a"
    primary = (
        seen[0]["settings"]["agents"]["defaults"]["model"]["primary"]
    )
    assert primary.startswith("anthropic/")


def test_tier_downgrade_writes_dedup_flag(
    fake_shared, fake_network, monkeypatch,
):
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER", lambda b, s: (True, ""),
    )
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso="2026-06-05",
    )
    flag = fake_shared / "cost_remediations" / "team_bot_a" / "tier_downgrade.flag"
    assert flag.exists()
    assert flag.read_text().strip() == "2026-06-05"


def test_tier_downgrade_dedups_within_same_day(
    fake_shared, fake_network, monkeypatch,
):
    """Second call same day does not re-write openclaw or re-dispatch."""
    writes: list[dict] = []
    dispatches: list[dict] = []

    def _w(b, s):
        writes.append({"b": b, "s": s})
        return True, ""

    def _d(**kw):
        dispatches.append(kw)
        return True

    monkeypatch.setattr(spend_alert, "_TIER_DOWNGRADE_WRITER", _w)
    monkeypatch.setattr(spend_alert, "_dispatch", _d)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso="2026-06-05",
    )
    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=15.0, cap=5.0, today_iso="2026-06-05",
    )
    assert len(writes) == 1  # first call only
    assert len(dispatches) == 1


def test_tier_downgrade_dedup_resets_on_new_day(
    fake_shared, fake_network, monkeypatch,
):
    """Stale flag from yesterday gets auto-removed; today's downgrade fires."""
    flag_dir = fake_shared / "cost_remediations" / "team_bot_a"
    flag_dir.mkdir(parents=True)
    (flag_dir / "tier_downgrade.flag").write_text("2025-01-01")  # stale

    writes: list[dict] = []
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER",
        lambda b, s: (writes.append({"b": b}), (True, ""))[1],
    )
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso=str(date.today()),
    )
    assert len(writes) == 1


# ─── _trip_l2_breaker_for_cost_cap: writes cost_l2 + bootouts ────────────────


def test_l2_trip_writes_breaker_record(
    fake_shared, fake_network, monkeypatch,
):
    """Even when enforcement raises, the breaker file is on disk so the
    operator can inspect via the CLI."""
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    fake_enforce = MagicMock()
    fake_enforce.ok = True
    fake_enforce.no_op = False
    monkeypatch.setattr(
        spend_alert, "_L2_ENFORCE_TRIP", lambda **kw: fake_enforce,
    )

    spend_alert._trip_l2_breaker_for_cost_cap(
        bot_id="team_bot_a", spend=30.0, cap=25.0,
        shared_dir=fake_shared, network=fake_network, today_iso="2026-06-05",
    )

    # Breaker file should exist at breakers/team_bot_a/cost_l2.json.
    breaker_file = fake_shared / "breakers" / "team_bot_a" / "cost_l2.json"
    assert breaker_file.exists()
    rec = json.loads(breaker_file.read_text())
    assert rec["bot_id"] == "team_bot_a"
    assert rec["type"] == "cost_l2"
    assert rec["initiated_by"] == "auto:spend_alert"


def test_l2_trip_calls_enforce_trip_with_cost_l2(
    fake_shared, fake_network, monkeypatch,
):
    captured: list[dict] = []

    def fake_enforce_trip(**kw):
        captured.append(kw)
        m = MagicMock()
        m.ok = True
        m.no_op = False
        return m

    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)
    monkeypatch.setattr(spend_alert, "_L2_ENFORCE_TRIP", fake_enforce_trip)

    spend_alert._trip_l2_breaker_for_cost_cap(
        bot_id="team_bot_a", spend=30.0, cap=25.0,
        shared_dir=fake_shared, network=fake_network, today_iso="2026-06-05",
    )

    assert len(captured) == 1
    assert captured[0]["breaker_type"] == "cost_l2"
    assert captured[0]["scope"] == "team_bot_a"


def test_l2_trip_dispatches_l2_catalog_event(
    fake_shared, fake_network, monkeypatch,
):
    dispatches: list[dict] = []

    def fake_dispatch(**kw):
        dispatches.append(kw)
        return True

    fake_enforce = MagicMock()
    fake_enforce.ok = True
    fake_enforce.no_op = False
    monkeypatch.setattr(
        spend_alert, "_L2_ENFORCE_TRIP", lambda **kw: fake_enforce,
    )
    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._trip_l2_breaker_for_cost_cap(
        bot_id="team_bot_a", spend=30.0, cap=25.0,
        shared_dir=fake_shared, network=fake_network, today_iso="2026-06-05",
    )

    assert len(dispatches) == 1
    assert dispatches[0]["catalog_event"] == "cost.gateway_stopped"
    assert dispatches[0]["payload"]["bot_id"] == "team_bot_a"


def test_l2_trip_dedups_when_already_tripped(
    fake_shared, fake_network, monkeypatch,
):
    """Second call doesn't re-enforce or re-alert."""
    enforcements: list[dict] = []
    dispatches: list[dict] = []

    def fake_dispatch(**kw):
        dispatches.append(kw)
        return True

    def fake_enforce_trip(**kw):
        enforcements.append(kw)
        m = MagicMock()
        m.ok = True
        m.no_op = False
        return m

    monkeypatch.setattr(spend_alert, "_L2_ENFORCE_TRIP", fake_enforce_trip)
    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._trip_l2_breaker_for_cost_cap(
        bot_id="team_bot_a", spend=30.0, cap=25.0,
        shared_dir=fake_shared, network=fake_network, today_iso="2026-06-05",
    )
    spend_alert._trip_l2_breaker_for_cost_cap(
        bot_id="team_bot_a", spend=35.0, cap=25.0,
        shared_dir=fake_shared, network=fake_network, today_iso="2026-06-05",
    )

    assert len(enforcements) == 1  # only the first call
    assert len(dispatches) == 1


# ─── Writer/reader resolution: real callables, no mocks ──────────────────────
# The 2026-08 regression: `_get_tier_downgrade_writer` imported from the
# non-existent `evolve_admin.cost_profiles`, so every production fire took
# the "writer unavailable" path and the bot's primary model was never
# switched. Every other test here monkeypatches the handle, which is what
# masked it — these two resolve the REAL callables so a future module move
# fails loudly.


def test_get_tier_downgrade_writer_resolves_real_callable(monkeypatch):
    import cost_profiles

    monkeypatch.setattr(spend_alert, "_TIER_DOWNGRADE_WRITER", None)
    writer = spend_alert._get_tier_downgrade_writer()
    assert writer is cost_profiles.write_openclaw_cost_settings


def test_get_tier_downgrade_reader_resolves_real_callable(monkeypatch):
    import cost_profiles

    monkeypatch.setattr(spend_alert, "_TIER_DOWNGRADE_READER", None)
    reader = spend_alert._get_tier_downgrade_reader()
    assert reader is cost_profiles.read_openclaw_primary_model


# ─── Midnight revert: restore record + _maybe_revert_tier_downgrade ──────────


def _restore_path(shared_dir: Path) -> Path:
    return (
        shared_dir / "cost_remediations" / "team_bot_a"
        / "tier_downgrade_restore.json"
    )


def _flag_path(shared_dir: Path) -> Path:
    return (
        shared_dir / "cost_remediations" / "team_bot_a"
        / "tier_downgrade.flag"
    )


def test_tier_downgrade_records_restore_state(
    fake_shared, fake_network, monkeypatch,
):
    """A successful downgrade write records the pre-downgrade primary."""
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-opus-5",
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER", lambda b, s: (True, ""),
    )
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso="2026-06-05",
    )
    rec = json.loads(_restore_path(fake_shared).read_text())
    assert rec["date"] == "2026-06-05"
    assert rec["original_model"] == "anthropic/claude-opus-5"
    assert rec["applied_model"] == "anthropic/claude-haiku-4-5"


def test_tier_downgrade_failed_write_records_no_restore(
    fake_shared, fake_network, monkeypatch,
):
    """A failed write changed nothing — no restore record must appear."""
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-opus-5",
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER", lambda b, s: (False, "boom"),
    )
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso="2026-06-05",
    )
    assert not _restore_path(fake_shared).exists()


def test_reapply_preserves_recorded_original(
    fake_shared, fake_network, monkeypatch,
):
    """Re-arming while a restore record exists must not clobber the true
    original with the already-downgraded model the reader now sees."""
    p = _restore_path(fake_shared)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "date": "2026-06-04",
        "original_model": "anthropic/claude-opus-5",
        "applied_model": "anthropic/claude-haiku-4-5",
    }))
    # Reader sees the downgraded model (yesterday's revert never ran).
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-haiku-4-5",
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER", lambda b, s: (True, ""),
    )
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **_: True)
    monkeypatch.setattr(spend_alert, "_flag_urgent_refresh", lambda *a, **k: None)

    spend_alert._apply_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, network=fake_network,
        spend=10.0, cap=5.0, today_iso="2026-06-05",
    )
    rec = json.loads(p.read_text())
    assert rec["original_model"] == "anthropic/claude-opus-5"
    assert rec["date"] == "2026-06-05"


def test_revert_noop_while_flag_covers_today(fake_shared, monkeypatch):
    p = _restore_path(fake_shared)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "date": "2026-06-05",
        "original_model": "anthropic/claude-opus-5",
        "applied_model": "anthropic/claude-haiku-4-5",
    }))
    _flag_path(fake_shared).write_text("2026-06-05")

    writes: list = []
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER",
        lambda b, s: (writes.append(s), (True, ""))[1],
    )

    spend_alert._maybe_revert_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, today_iso="2026-06-05",
    )
    assert writes == []
    assert p.exists()
    assert _flag_path(fake_shared).exists()


def test_revert_restores_original_after_date_roll(fake_shared, monkeypatch):
    p = _restore_path(fake_shared)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "date": "2026-06-05",
        "original_model": "anthropic/claude-opus-5",
        "applied_model": "anthropic/claude-haiku-4-5",
    }))
    _flag_path(fake_shared).write_text("2026-06-05")  # yesterday

    writes: list = []
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-haiku-4-5",
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER",
        lambda b, s: (writes.append(s), (True, ""))[1],
    )

    spend_alert._maybe_revert_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, today_iso="2026-06-06",
    )
    assert len(writes) == 1
    restored = writes[0]["agents"]["defaults"]["model"]["primary"]
    assert restored == "anthropic/claude-opus-5"
    assert not p.exists()
    assert not _flag_path(fake_shared).exists()  # stale flag swept too


def test_revert_after_midday_reset(fake_shared, monkeypatch):
    """Operator/evo reset removed the flag mid-day → next tick restores."""
    p = _restore_path(fake_shared)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "date": "2026-06-05",
        "original_model": "anthropic/claude-opus-5",
        "applied_model": "anthropic/claude-haiku-4-5",
    }))
    # No flag file — reset_remediation unlinked it.

    writes: list = []
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-haiku-4-5",
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER",
        lambda b, s: (writes.append(s), (True, ""))[1],
    )

    spend_alert._maybe_revert_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, today_iso="2026-06-05",
    )
    assert len(writes) == 1
    assert not p.exists()


def test_revert_skips_when_model_changed(fake_shared, monkeypatch):
    """Primary no longer matches what the downgrade wrote — don't clobber."""
    p = _restore_path(fake_shared)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "date": "2026-06-05",
        "original_model": "anthropic/claude-opus-5",
        "applied_model": "anthropic/claude-haiku-4-5",
    }))

    writes: list = []
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-sonnet-5",  # operator picked this
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER",
        lambda b, s: (writes.append(s), (True, ""))[1],
    )

    spend_alert._maybe_revert_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, today_iso="2026-06-06",
    )
    assert writes == []
    assert not p.exists()  # marker cleaned up anyway


def test_revert_keeps_record_on_write_failure(fake_shared, monkeypatch):
    """Failed restore write leaves the record so the next tick retries."""
    p = _restore_path(fake_shared)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "date": "2026-06-05",
        "original_model": "anthropic/claude-opus-5",
        "applied_model": "anthropic/claude-haiku-4-5",
    }))

    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_READER",
        lambda b: "anthropic/claude-haiku-4-5",
    )
    monkeypatch.setattr(
        spend_alert, "_TIER_DOWNGRADE_WRITER", lambda b, s: (False, "boom"),
    )

    spend_alert._maybe_revert_tier_downgrade(
        bot_id="team_bot_a", shared_dir=fake_shared, today_iso="2026-06-06",
    )
    assert p.exists()
