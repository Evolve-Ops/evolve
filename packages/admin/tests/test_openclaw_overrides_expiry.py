"""Tests for evolve_admin.openclaw_overrides_expiry.

Phase 5 of docs/spec-openclaw-json-derived-artifact-2026-05-24.md.

The daily enforcer walks {shared}/sandbox/overrides/<bot>.json. For
each override with an expires_at, it:

  - Deletes the override when expires_at < now → emits one-shot
    "expired" Signal.
  - Emits "pre_expiry" Signal when 0 < expires_at - now <= 7 days.
  - Skips otherwise.

These tests pin all three branches + the edge cases (date-only vs
datetime expires_at, unparseable strings, idempotency, sweep_resolve
of pre_expiry when operator extends).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.config_sandbox import (
    OverrideEntry,
    path_for_bot,
    read_bot_overrides,
    write_override,
)
from evolve_admin.openclaw_overrides_expiry import (
    DEFAULT_PRE_EXPIRY_WINDOW_DAYS,
    PRODUCER,
    TYPE_EXPIRED,
    TYPE_PRE_EXPIRY,
    _parse_expires_at,
    format_summary,
    scan,
)


_TIER_KEY = "openclaw.plugins.evolve.tier"
_SUMMARIZER_KEY = "openclaw.plugins.evolve.summarizerMinTurns"


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    """tmp shared_dir with the signals subtree initialized so observe()
    can write there."""
    sd = tmp_path / "evolve"
    (sd / "signals" / "firing").mkdir(parents=True)
    (sd / "signals" / "snoozed").mkdir(parents=True)
    (sd / "signals" / "archived").mkdir(parents=True)
    return sd


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


# ─── _parse_expires_at ─────────────────────────────────────────────────────


def test_parse_iso_date_treats_as_end_of_day():
    """A date-only ``"2026-05-26"`` expires at 2026-05-26 23:59:59 UTC —
    so a scan at 12:00 on the same date does NOT consider it expired."""
    dt = _parse_expires_at("2026-05-26")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 26
    assert dt.hour == 23  # end-of-day
    assert dt.tzinfo == timezone.utc


def test_parse_iso_datetime_with_z():
    dt = _parse_expires_at("2026-08-01T14:00:00Z")
    assert dt is not None
    assert dt.hour == 14
    assert dt.tzinfo == timezone.utc


def test_parse_naive_datetime_assumed_utc():
    dt = _parse_expires_at("2026-08-01T14:00:00")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_invalid_returns_none():
    assert _parse_expires_at("not a date") is None
    assert _parse_expires_at("") is None
    assert _parse_expires_at(None) is None
    assert _parse_expires_at(12345) is None


# ─── scan: expired branch ──────────────────────────────────────────────────


def test_expired_override_deleted_and_signal_emitted(shared_dir, now):
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator",
        expires_at="2026-05-20",   # 6 days before now
        note="experimental",
        now=now - timedelta(days=10),
    )
    result = scan(shared_dir, now=now)
    assert len(result.expired) == 1
    bot_id, key, entry = result.expired[0]
    assert bot_id == "team_bot_a"
    assert key == _TIER_KEY
    assert entry.value == "monitor"
    # Override is gone from the file
    bo = read_bot_overrides(shared_dir, "team_bot_a")
    assert bo.get(_TIER_KEY) is None
    # Signal landed
    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["producer"] == PRODUCER
    assert sig["type"] == TYPE_EXPIRED
    assert sig["bot_id"] == "team_bot_a"
    assert _TIER_KEY in sig["body"]
    assert "experimental" in sig["body"]   # original note surfaced


def test_expired_signal_persists_across_scans(shared_dir, now):
    """After the override is deleted, subsequent scans don't see it any
    more — but the one-shot ``expired`` Signal must NOT be auto-resolved
    by sweep_resolve. Operator dismisses or it ages out via retention."""
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    scan(shared_dir, now=now)
    # Second scan: nothing left to do, but the expired Signal still fires.
    scan(shared_dir, now=now + timedelta(hours=1))
    firing = list((shared_dir / "signals" / "firing").iterdir())
    archived = list((shared_dir / "signals" / "archived").iterdir())
    assert len(firing) == 1   # still firing
    assert len(archived) == 0


# ─── scan: pre_expiry branch ───────────────────────────────────────────────


def test_pre_expiry_within_window_emits_info_signal(shared_dir, now):
    """Override expires in 3 days → pre_expiry Signal at severity=info."""
    expires_in_3d = (now + timedelta(days=3)).date().isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_3d,
        note="haiku pin until OC#84825",
        now=now - timedelta(days=2),
    )
    result = scan(shared_dir, now=now)
    assert len(result.pre_expiry) == 1
    bot_id, key, _entry, days_left = result.pre_expiry[0]
    assert bot_id == "team_bot_a"
    assert days_left in (2, 3)   # tolerance for "end-of-day" parsing
    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["type"] == TYPE_PRE_EXPIRY
    assert sig["severity"] == "info"   # advisory per-emit override
    assert "haiku pin" in sig["body"]


def test_pre_expiry_at_boundary_includes_seven_days(shared_dir, now):
    """An override expiring within the 7-day pre-expiry window fires;
    an override expiring well outside it doesn't. Uses datetime values
    (not date-only) so we're testing the threshold, not the
    end-of-day-rounding behavior of the parser.
    """
    in_window = (now + timedelta(days=7, hours=-1)).isoformat()
    out_of_window = (now + timedelta(days=7, hours=2)).isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=in_window,
    )
    write_override(
        shared_dir, "security_bot", _SUMMARIZER_KEY, 5,
        set_by="operator", expires_at=out_of_window,
    )
    result = scan(shared_dir, now=now)
    assert {b for b, _, _, _ in result.pre_expiry} == {"team_bot_a"}


def test_pre_expiry_outside_window_skipped(shared_dir, now):
    """An override expiring 30 days out is silent."""
    expires_in_30d = (now + timedelta(days=30)).date().isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_30d,
    )
    result = scan(shared_dir, now=now)
    assert result.pre_expiry == []
    assert result.expired == []
    assert list((shared_dir / "signals" / "firing").iterdir()) == []


def test_pre_expiry_sweep_resolves_when_operator_extends(shared_dir, now):
    """Operator extends expires_at past the window → next scan should
    sweep_resolve the previously-firing pre_expiry Signal."""
    expires_in_3d = (now + timedelta(days=3)).date().isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_3d,
    )
    scan(shared_dir, now=now)
    assert len(list((shared_dir / "signals" / "firing").iterdir())) == 1

    # Operator extends the expiry well past the window.
    expires_in_60d = (now + timedelta(days=60)).date().isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_60d,
    )
    scan(shared_dir, now=now)
    # Pre_expiry Signal was swept to archived.
    assert list((shared_dir / "signals" / "firing").iterdir()) == []
    assert len(list((shared_dir / "signals" / "archived").iterdir())) == 1


def test_pre_expiry_sweep_resolves_when_operator_reverts(shared_dir, now):
    """Operator deletes the override entirely → no pre_expiry condition
    any more → sweep_resolve archives the prior Signal."""
    from evolve_admin.config_sandbox import delete_override
    expires_in_3d = (now + timedelta(days=3)).date().isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_3d,
    )
    scan(shared_dir, now=now)
    assert len(list((shared_dir / "signals" / "firing").iterdir())) == 1

    delete_override(shared_dir, "team_bot_a", _TIER_KEY)
    scan(shared_dir, now=now)
    assert list((shared_dir / "signals" / "firing").iterdir()) == []


# ─── scan: skip branches ───────────────────────────────────────────────────


def test_override_without_expires_at_is_silent(shared_dir, now):
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=None,
    )
    result = scan(shared_dir, now=now)
    assert result.expired == []
    assert result.pre_expiry == []
    assert result.scanned_entry_count == 1


def test_unparseable_expires_at_recorded_not_acted(shared_dir, now, caplog):
    """Phase 2's _validate_expires_at would have rejected it, so this
    only happens when someone manually edits the file. Skip with a warn
    rather than crash."""
    # Write a valid override, then mangle the file in place.
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    p = path_for_bot(shared_dir, "team_bot_a")
    data = json.loads(p.read_text())
    data["overrides"][_TIER_KEY]["expires_at"] = "next tuesday"
    p.write_text(json.dumps(data))

    import logging
    with caplog.at_level(logging.WARNING, logger="evolve_admin.openclaw_overrides_expiry"):
        result = scan(shared_dir, now=now)
    assert result.expired == []
    assert result.pre_expiry == []
    assert len(result.unparseable) == 1
    assert result.unparseable[0] == ("team_bot_a", _TIER_KEY, "next tuesday")
    assert any("unparseable" in r.message.lower() for r in caplog.records)
    # And the override is NOT deleted.
    bo = read_bot_overrides(shared_dir, "team_bot_a")
    assert bo.get(_TIER_KEY) is not None


def test_empty_pod_no_action(shared_dir, now):
    """No overrides anywhere → result is empty, no Signals."""
    result = scan(shared_dir, now=now)
    assert result.scanned_entry_count == 0
    assert result.expired == []
    assert result.pre_expiry == []
    assert list((shared_dir / "signals" / "firing").iterdir()) == []


# ─── scan: combined / realistic scenarios ──────────────────────────────────


def test_multiple_bots_mixed_states(shared_dir, now):
    """One bot has an expired override, another has a pre_expiry, a
    third is silent. Single scan handles all three correctly."""
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    expires_in_3d = (now + timedelta(days=3)).date().isoformat()
    write_override(
        shared_dir, "security_bot", _SUMMARIZER_KEY, 5,
        set_by="operator", expires_at=expires_in_3d,
    )
    write_override(
        shared_dir, "evolve", _TIER_KEY, "manage",
        set_by="operator", expires_at=None,
    )
    result = scan(shared_dir, now=now)
    assert {b for b, _, _ in result.expired} == {"team_bot_a"}
    assert {b for b, _, _, _ in result.pre_expiry} == {"security_bot"}
    # team_bot_a's override deleted, security_bot's intact, evolve's intact.
    assert read_bot_overrides(shared_dir, "team_bot_a").get(_TIER_KEY) is None
    assert read_bot_overrides(shared_dir, "security_bot").get(_SUMMARIZER_KEY) is not None
    assert read_bot_overrides(shared_dir, "evolve").get(_TIER_KEY) is not None
    # 2 Signals firing (team_bot_a:expired, security_bot:pre_expiry)
    assert len(list((shared_dir / "signals" / "firing").iterdir())) == 2


def test_idempotent_double_scan(shared_dir, now):
    """Running scan twice in quick succession: nothing duplicates, no
    extra Signals, no extra deletions."""
    expires_in_3d = (now + timedelta(days=3)).date().isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_3d,
    )
    scan(shared_dir, now=now)
    scan(shared_dir, now=now)
    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1   # observe() dedups by signature


# ─── format_summary ────────────────────────────────────────────────────────


def test_format_summary_renders_all_sections(shared_dir, now):
    """The CLI output mentions every category that fired."""
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    expires_in_3d = (now + timedelta(days=3)).date().isoformat()
    write_override(
        shared_dir, "security_bot", _SUMMARIZER_KEY, 5,
        set_by="operator", expires_at=expires_in_3d,
    )
    result = scan(shared_dir, now=now)
    out = format_summary(result)
    assert "Expired and removed" in out
    assert "team_bot_a" in out
    assert "Approaching expiry" in out
    assert "security_bot" in out
    assert "scanned 2 entries" in out


def test_format_summary_quiet_when_nothing(shared_dir, now):
    result = scan(shared_dir, now=now)
    out = format_summary(result)
    assert "no action" in out


def test_format_summary_includes_lapse_timestamps(shared_dir, now):
    """B7 fix: the cron log needs to show *when* an override lapsed —
    that's the actionable info for "operator reads daily log and decides
    if anything needs follow-up.""" ""
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    result = scan(shared_dir, now=now)
    out = format_summary(result)
    assert "2026-05-20" in out
    assert "operator" in out   # set_by is now in the summary


def test_format_time_left_renders_today_tomorrow_and_days():
    """B5 fix: `delta.days` truncation produced 'lapses in 0 days' for
    sub-24h expirations. The friendly formatter uses today/tomorrow."""
    from evolve_admin.openclaw_overrides_expiry import _format_time_left
    assert _format_time_left(timedelta(hours=12)) == ("today", 1)
    assert _format_time_left(timedelta(hours=23, minutes=59)) == ("today", 1)
    assert _format_time_left(timedelta(hours=25)) == ("tomorrow", 1)
    assert _format_time_left(timedelta(days=1, hours=23)) == ("tomorrow", 1)
    assert _format_time_left(timedelta(days=2, hours=1)) == ("in 2 days", 2)
    assert _format_time_left(timedelta(days=7)) == ("in 7 days", 7)


def test_pre_expiry_signal_uses_friendly_time_string(shared_dir, now):
    """A pre_expiry < 24h surfaces as 'today' in the Signal title/body,
    not 'in 0 days'."""
    expires_in_18h = (now + timedelta(hours=18)).isoformat()
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at=expires_in_18h,
    )
    scan(shared_dir, now=now)
    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert "today" in sig["title"]
    assert "today" in sig["body"]
    assert "0 days" not in sig["body"]


def test_cli_nonzero_exit_on_bot_error(shared_dir, now, monkeypatch):
    """B8 fix: scanner exit code is 1 when any bot's overrides file is
    corrupt so external 'cron passed?' checks see the failure."""
    from evolve_admin.openclaw_overrides_expiry import _main

    # Plant a state where delete_override will raise OverrideStateError.
    # Easiest path: write a valid override (so iter_all_overrides yields
    # it), then corrupt the file before the scanner can re-read it.
    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    # Monkeypatch the now-source so the scanner sees this as lapsed.
    import evolve_admin.openclaw_overrides_expiry as m
    monkeypatch.setattr(m, "datetime", _FixedNow(now))
    # Corrupt the file in-place after iter_all_overrides reads it.
    # Simulate by monkeypatching delete_override to raise.
    from evolve_admin.config_sandbox import OverrideStateError as _OSE
    def boom(*args, **kw):
        raise _OSE("simulated corruption")
    monkeypatch.setattr(m, "delete_override", boom)

    rc = _main(["--shared-dir", str(shared_dir)])
    assert rc == 1   # non-zero


class _FixedNow:
    """Drop-in for datetime that pins datetime.now() and proxies the rest."""
    def __init__(self, fixed):
        self._fixed = fixed
    def now(self, tz=None):
        if tz is not None and self._fixed.tzinfo is None:
            return self._fixed.replace(tzinfo=tz)
        return self._fixed
    def __getattr__(self, name):
        import datetime as _dt
        return getattr(_dt.datetime, name)


# ─── Signal-store integration: log-only fallback ──────────────────────────


def test_signal_import_failure_does_not_crash(shared_dir, now, monkeypatch):
    """If signals.store can't be imported, the scanner still processes
    overrides (deleting expired) — just doesn't emit Signals."""
    import evolve_admin.openclaw_overrides_expiry as m
    monkeypatch.setattr(m, "_import_signals", lambda: (None, None))

    write_override(
        shared_dir, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-05-20",
        now=now - timedelta(days=10),
    )
    result = scan(shared_dir, now=now)
    assert len(result.expired) == 1
    # No Signals emitted (the firing dir stays empty)
    assert list((shared_dir / "signals" / "firing").iterdir()) == []
    # But the override IS deleted — the scanner's core duty was done.
    assert read_bot_overrides(shared_dir, "team_bot_a").get(_TIER_KEY) is None
