"""Reactivation writes the "count from now" marker — on every operator path.

Chip: internal/dispatch/done/breaker-reactivate-accepts-the-window.md.

Reactivating a bot used to clear the breaker file and nothing else. The next
enforcement tick re-read today's raw spend, found it still over the cap, and
tripped again — on 2026-09-07 that happened three times in a row on one bot
against "$27.49 >= $5.00". The marker records the spend total at the moment
the operator brought the bot back; every later evaluation measures from
there.

What is NOT accepted matters as much as what is: a pod-wide trip (no per-bot
spend to accept), a `full` halt (not a spend judgement), and the TTL reaper
(nobody accepted anything) all leave the marker alone.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ANALYZER_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spend_caps  # noqa: E402

from evolve_admin import breakers_enforce  # noqa: E402

BOT = "team_bot_a"


@pytest.fixture
def network() -> dict:
    return {
        "primary": "team_bot_a",
        "members": [BOT],
        "bots": {BOT: {"user": BOT}},
    }


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    return d


@pytest.fixture
def spend_2749(monkeypatch: pytest.MonkeyPatch):
    """Today's spend reads $27.49 — the figure from the live incident."""
    monkeypatch.setattr(spend_caps, "get_today_spend", lambda *a, **k: 27.49)


def _trip(shared: Path, scope: str = BOT, breaker_type: str = "cost") -> None:
    from breakers import store

    store.trip(
        shared_dir=shared, scope=scope, breaker_type=breaker_type,
        duration=timedelta(hours=24), initiated_by="auto:spend_alert",
        reason="per-bot daily cap exceeded: $27.49 >= $5.00",
    )


@pytest.fixture
def no_enforce(monkeypatch: pytest.MonkeyPatch):
    """Bring-up is not what these tests are about — stub it at the boundary."""
    class _R:
        ok = True
        no_op = False
        notifications: list = []

        def to_dict(self):
            return {"ok": True}

    monkeypatch.setattr(
        breakers_enforce, "enforce_reset", lambda **kw: _R(),
    )


# ── The happy path ──────────────────────────────────────────────────────────


def test_reset_and_enforce_accepts_the_window(
    shared, network, spend_2749, no_enforce,
):
    _trip(shared)
    outcome = breakers_enforce.reset_and_enforce(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        network=network, initiated_by="web",
    )
    assert outcome.was_tripped is True
    marker = spend_caps.read_accepted_through(shared, BOT)
    assert marker is not None
    assert marker.accepted_usd == pytest.approx(27.49)


def test_the_cli_reset_accepts_the_same_window(
    shared, network, spend_2749, monkeypatch,
):
    """Both operator paths agree. A bot brought back with `evolve-admin
    breaker reset` must not re-trip where the button's would not."""
    _trip(shared)
    from breakers import store

    store.reset(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        initiated_by="cli", reason="manual reset",
    )
    breakers_enforce.record_reactivation_acceptance(
        shared_dir=shared, scope=BOT, breaker_type="cost",
    )
    marker = spend_caps.read_accepted_through(shared, BOT)
    assert marker is not None
    assert marker.accepted_usd == pytest.approx(27.49)


def test_a_second_reactivation_moves_the_origin(
    shared, network, no_enforce, monkeypatch,
):
    spends = iter([27.49, 31.02])
    monkeypatch.setattr(spend_caps, "get_today_spend", lambda *a, **k: next(spends))
    for _ in range(2):
        _trip(shared)
        breakers_enforce.reset_and_enforce(
            shared_dir=shared, scope=BOT, breaker_type="cost",
            network=network, initiated_by="web",
        )
    assert spend_caps.read_accepted_through(shared, BOT).accepted_usd == pytest.approx(31.02)


# ── What is deliberately NOT accepted ───────────────────────────────────────


def test_nothing_tripped_accepts_nothing(shared, network, spend_2749, no_enforce):
    outcome = breakers_enforce.reset_and_enforce(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        network=network, initiated_by="web",
    )
    assert outcome.was_tripped is False
    assert spend_caps.read_accepted_through(shared, BOT) is None


def test_a_full_halt_reset_is_not_a_spend_judgement(shared, network, spend_2749):
    breakers_enforce.record_reactivation_acceptance(
        shared_dir=shared, scope=BOT, breaker_type="full",
    )
    assert spend_caps.read_accepted_through(shared, BOT) is None


def test_a_pod_scope_accepts_nothing(shared, network, spend_2749):
    """A pod-wide trip has no per-bot spend total; inventing one per member
    would accept spend on bots the operator never looked at."""
    breakers_enforce.record_reactivation_acceptance(
        shared_dir=shared, scope="pod", breaker_type="cost",
    )
    assert spend_caps.read_accepted_through(shared, "pod") is None


def test_a_ttl_reap_accepts_nothing(shared, network, spend_2749, monkeypatch):
    """``heal._reap_expired_cost_breakers`` calls ``enforce_reset``
    directly. A breaker that timed out on its own is not an operator
    accepting anything, so no marker is written."""
    calls: list = []
    monkeypatch.setattr(
        breakers_enforce, "record_reactivation_acceptance",
        lambda **kw: calls.append(kw),
    )
    breakers_enforce.enforce_reset(
        scope=BOT, breaker_type="cost", network=network,
        shared_dir=shared, dry_run=True,
    )
    assert calls == []


# ── Failure directions ──────────────────────────────────────────────────────


def test_unreadable_spend_writes_no_marker(shared, network, monkeypatch, no_enforce):
    """Fail toward RE-TRIPPING. An unreadable spend figure must never buy
    an open-ended pass on a live bleed."""
    monkeypatch.setattr(spend_caps, "get_today_spend", lambda *a, **k: None)
    _trip(shared)
    breakers_enforce.reset_and_enforce(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        network=network, initiated_by="web",
    )
    assert spend_caps.read_accepted_through(shared, BOT) is None


def test_a_raising_spend_reader_never_breaks_the_reset(
    shared, network, monkeypatch, no_enforce,
):
    def boom(*a, **k):
        raise OSError("live JSONL gone")

    monkeypatch.setattr(spend_caps, "get_today_spend", boom)
    _trip(shared)
    outcome = breakers_enforce.reset_and_enforce(
        shared_dir=shared, scope=BOT, breaker_type="cost",
        network=network, initiated_by="web",
    )
    # The reset itself still committed — the marker is a consequence of a
    # reset, never a precondition for one.
    assert outcome.was_tripped is True
    from breakers import store
    assert store.read_trip(shared, BOT, "cost") is None
    assert spend_caps.read_accepted_through(shared, BOT) is None
