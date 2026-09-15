"""tests/test_alerts_digest_live_census.py — D1's byte-identity claim, bound
to the live-log census rather than to hand-picked numbers.

spec-delta-digest-audit-noise-2026-08-25 D1 stops the digest lane re-enqueuing
a within-cooldown repeat. That is only content-preserving because
``digest_dispatcher._dedup_records`` keeps the MOST RECENT record of a
collapse group while the fix leaves only the FIRST — so the two agree exactly
when the body is CONSTANT across the ticks of one ``dedup_key``.

``test_delivered_digest_is_byte_identical_before_and_after_the_gate`` in
test_alerts_dispatcher.py pins the mechanism, and its docstring calls the
constant-body case "the live heal shape". That clause is the load-bearing
claim, and it was argued from ``heal._alert_backup_drift``'s render path
(fixed catalog template, no timestamp, no counter). PR #3801 — closed as
superseded by #3802, code equivalent — checked the pod log instead and found
exactly **3 distinct (dedup_key, message_excerpt) pairs across all 287 rows**:
one body per key, observed rather than inferred.

One step of inference remains, named so it is not mistaken for observation:
``message_excerpt`` is the body truncated at ``_AUDIT_EXCERPT_CHARS`` (240),
so a constant excerpt implies a constant body only for bodies under 240
chars. heal's drift lines are well inside that, and the direction is safe —
a body that varied past char 240 would make the pre/post digests differ,
which is the case the fixture would then need to record.

This module makes that observation durable. ``fixtures/
dispatcher_live_census_2026-08-25.json`` records the census (counts and key
names only — no rows, no bodies, no real ids), and the test below reads its
numbers, asserts the constant-body premise the byte-identity claim needs, and
then drives the real dispatcher at exactly that shape.

SCOPE — the constant-body premise is a fact about the CURRENT collapse
behaviour, not a permanent law. D1a (PR #3808, open at the time of writing)
makes a suppressed repeat REPLACE the pending record, after which the
delivered digest reads the latest body and byte-identity stops depending on
the body being constant. When that lands, this module keeps its value as the
live-shape record — the census is still what the 287 rows were — but the
"needs re-making" warning below applies only to the pre-D1a argument.

Deliberately a separate module: it is evidence about a specific observed day,
with a fixture of its own, and it has a different lifetime from the mechanism
tests it supports.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_CENSUS_PATH = (
    Path(__file__).parent / "fixtures" / "dispatcher_live_census_2026-08-25.json"
)
_DRIFT_EVENT = "security.config_drift"     # catalog default: DAILY_DIGEST


@pytest.fixture
def census():
    return json.loads(_CENSUS_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp shared_dir + a fake openclaw subprocess + a default-recipient network."""
    from evolve_admin.alerts import dispatcher

    shared = tmp_path / "evolve"
    shared.mkdir()
    sent: list[tuple[str, str, str]] = []

    def _fake_dispatch(channel, chat_id, message, gateway_port=None):
        sent.append((channel, chat_id, message))
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_dispatch)
    network = {"alerts": {"channel": "telegram", "chatId": "12345"}, "bots": {}}
    return {
        "shared": shared, "network": network,
        "dispatcher": dispatcher, "sent": sent,
    }


def _queue_records(shared, frequency="daily"):
    p = shared / "alerts" / "digest-pending" / f"{frequency}.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln]


def _queued_lane(shared):
    rows = []
    for f in sorted((shared / "alerts" / "dispatcher-queued").glob("*.jsonl")):
        rows.extend(
            json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()
        )
    return rows


def test_census_is_the_config_drift_share_the_spec_reports(census):
    """Guard the record itself. The census is cited by the byte-identity test
    below and by the D1 spec's "98.3% of the operator's message feed is one
    producer" line; an edit that changed the numbers without revisiting either
    should not pass silently."""
    drift = census["config_drift"]
    by_producer = {r["source"]: r for r in census["by_producer"]}

    assert sum(r["rows"] for r in census["by_producer"]) == census["total_rows"]
    assert by_producer["heal"]["rows"] == drift["rows"]
    assert by_producer["heal"]["catalog_event"] == _DRIFT_EVENT
    assert by_producer["heal"]["result"] == "deferred"
    # 287/292 — the share the spec quotes.
    assert round(100 * drift["rows"] / census["total_rows"], 1) == 98.3
    # The producer asked for one alert per key per day and ticks 288 times
    # per bot per day: the cooldown was declared and the digest lane ignored
    # it. (288 is ticks-per-bot-per-day, NOT the 287 rows — those are 2 bots
    # across 3 keys. Two different counts; the spec quotes both.)
    assert drift["producer_declared_cooldown_seconds"] == 86_400
    assert 86_400 // drift["producer_cadence_seconds"] == 288


def test_live_shape_is_one_body_per_dedup_key(census):
    """The premise D1's byte-identity claim rests on, as an OBSERVATION.

    3 distinct ``(dedup_key, message_excerpt)`` pairs across 3 distinct
    ``dedup_key``s means each key carried exactly one body for the whole day.
    Pre-fix ``_dedup_records`` kept the most recent record of a collapse
    group; post-fix only the first survives. Those two agree only when the
    body is constant — so if this ever ceases to hold for the live producer,
    the byte-identity test below stops describing the live lane and D1's
    content-preservation argument needs re-making, not re-asserting. (See the
    module docstring: what was observed is excerpt constancy, and D1a changes
    whether the argument depends on constancy at all.)
    """
    drift = census["config_drift"]
    assert len(drift["dedup_keys"]) == drift["distinct_dedup_keys"]
    assert drift["distinct_dedup_key_message_excerpt_pairs"] == drift[
        "distinct_dedup_keys"
    ]
    # …and the observation is about the whole day, not a sample.
    assert drift["rows"] == census["by_producer"][0]["rows"] == 287


def test_the_censused_day_collapses_and_renders_byte_identical(env, census):
    """Replay the censused day through the real dispatcher.

    Same key count, same total row count, and — per the premise asserted
    above — a body that is constant per key. Pre-fix queue = every tick
    enqueued (``_enqueue_digest`` directly, which is literally what the old
    short-circuit did); post-fix queue = the same ticks through ``send``.

    Asserts the two operator-visible claims at once: the audit feed collapses
    from ``rows`` to ``distinct_dedup_keys``, and the DELIVERED digest does
    not change by a single byte.
    """
    from evolve_admin.alerts.digest_dispatcher import _render_digest

    d = env["dispatcher"]
    drift = census["config_drift"]
    keys = drift["dedup_keys"]
    cadence = drift["producer_cadence_seconds"]
    # Ticks per key such that the replay writes the censused row count. The
    # per-key split was never recorded, so spread it evenly and top up — the
    # claim under test is about totals and distinct keys, not the split.
    per_key = [drift["rows"] // len(keys)] * len(keys)
    for i in range(drift["rows"] - sum(per_key)):
        per_key[i] += 1
    assert sum(per_key) == drift["rows"]

    # One body per key — the premise, as the fixture records it. The text is
    # illustrative (no real body was ever captured); its CONSTANCY is the
    # observed fact being replayed.
    bodies = {k: f"config drift on {k.split('/')[2]}: <drift set {i}>"
              for i, k in enumerate(keys)}

    t0 = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    pre_shared = env["shared"] / "pre"
    pre_shared.mkdir()
    for key, ticks in zip(keys, per_key):
        for tick in range(ticks):
            now = t0 + timedelta(seconds=cadence * tick)
            d._enqueue_digest(
                pre_shared, "daily_digest", now,
                source="heal", catalog_event=_DRIFT_EVENT,
                severity=d.Severity.ERROR, dedup_key=key, message=bodies[key],
            )
            d.send(
                shared_dir=env["shared"], network=env["network"],
                source="heal", message=bodies[key],
                catalog_event=_DRIFT_EVENT, dedup_key=key,
                severity=d.Severity.ERROR, now=now,
            )

    pre = _queue_records(pre_shared)
    post = _queue_records(env["shared"])
    assert len(pre) == drift["rows"]                      # the censused day
    assert len(post) == drift["distinct_dedup_keys"]      # what D1 leaves
    # The audit feed the operator actually reads shrinks the same way.
    assert len(_queued_lane(env["shared"])) == drift["distinct_dedup_keys"]
    assert env["sent"] == []                              # digest lane, no page

    assert _render_digest(post, "daily") == _render_digest(pre, "daily")
