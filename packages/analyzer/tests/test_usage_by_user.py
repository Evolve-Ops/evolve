"""tests/test_usage_by_user.py — per-user × per-app usage rollup (D-S2 track 3).

The load-bearing assertions are the honesty contract and the privacy
gate: two requesters roll up separately, a turn whose requester cannot
be resolved lands in ``unattributed_user`` (with the REASON split, never
a guess), the do-not-track chokepoint withholds rather than degrades,
the windows are AL-1.3's, and a bot with no session summaries produces
an honest empty rollup instead of failing.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_by_user  # noqa: E402
from usage_by_app import WINDOWS  # noqa: E402
from usage_by_user import (  # noqa: E402
    SCHEMA_VERSION,
    WITHHELD_GATE_UNAVAILABLE,
    load_usage_by_user,
    rollup_bot,
    run_usage_by_user,
    session_requesters,
    usage_by_user_path,
    write_usage_by_user,
)


TODAY = date(2026, 8, 17)
BOT = "team_bot_a"
ALICE = "slack:U-ALICE"
BOB = "telegram:998877"


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _annotation(
    *,
    app_id: str | None = None,
    grade: str | None = None,
    session_id: str = "s-1",
    tokens: int = 10,
    cost: float = 0.01,
    ts: str = "2026-08-17T12:00:00.000Z",
    schema_version: int = 5,
    **extra,
) -> dict:
    rec = {
        "type": "turn_annotation",
        "schema_version": schema_version,
        "ts": ts,
        "session_id": session_id,
        "input_tokens": tokens,
        "output_tokens": tokens * 2,
        "cost_estimated": cost,
        "app_id": app_id,
        "app_attribution": grade or "none",
    }
    rec.update(extra)
    return rec


def _summary(
    *,
    session_id: str,
    requester: str | None,
    bot_id: str = BOT,
    ts: str = "2026-08-17T13:00:00.000Z",
) -> dict:
    rec: dict = {
        "type": "session_summary",
        "schema_version": 2,
        "session_id": session_id,
        "bot_id": bot_id,
        "ts": ts,
    }
    if requester is not None:
        rec["recurring_request"] = {
            "label": "morning brief", "requester": requester, "hour": 7,
        }
    return rec


def _write(shared: Path, bot: str, day: date, records: list[dict]) -> None:
    ann_dir = shared / "annotations" / bot
    ann_dir.mkdir(parents=True, exist_ok=True)
    with (ann_dir / f"{day.isoformat()}.jsonl").open("a") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def _write_cost_events(shared: Path, bot: str, day: date, events: list[dict]) -> None:
    ann_dir = shared / "annotations" / bot
    ann_dir.mkdir(parents=True, exist_ok=True)
    path = ann_dir / f"cost_events-{day.isoformat()}.jsonl"
    with path.open("a") as handle:
        for event in events:
            handle.write(json.dumps({"type": "cost_event", **event}) + "\n")


def _network(shared: Path, **bots) -> None:
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "network.json").write_text(json.dumps({
        "sharedDir": str(shared), "members": [BOT], "bots": bots,
    }))


def _roster(shared: Path, bot: str, payload: dict) -> None:
    rosters = shared / "rosters"
    rosters.mkdir(parents=True, exist_ok=True)
    (rosters / f"{bot}.json").write_text(json.dumps(payload))


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    out = tmp_path / "evolve"
    _network(out)
    return out


# ── Two requesters roll up separately (the brief's first test) ───────────────

def test_two_requesters_roll_up_separately(shared: Path) -> None:
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _summary(session_id="s-b", requester=BOB),
        _annotation(session_id="s-a", app_id="morning-brief",
                    grade="scheduled", cost=0.10),
        _annotation(session_id="s-a", app_id="morning-brief",
                    grade="scheduled", cost=0.10),
        _annotation(session_id="s-b", app_id="morning-brief",
                    grade="explicit", cost=0.30),
    ])

    payload = rollup_bot(shared, BOT, today=TODAY)

    # Per-bot totals by user.
    assert set(payload["users"]) == {ALICE, BOB}
    assert payload["users"][ALICE]["d1"]["turns"] == 2
    assert payload["users"][ALICE]["d1"]["cost_estimated"] == pytest.approx(0.20)
    assert payload["users"][BOB]["d1"]["turns"] == 1
    assert payload["users"][BOB]["d1"]["cost_estimated"] == pytest.approx(0.30)
    # Tokens ride along, same accumulator as AL-1.3's.
    assert payload["users"][ALICE]["d1"]["input_tokens"] == 20
    assert payload["users"][ALICE]["d1"]["output_tokens"] == 40

    # Per-app × user.
    app_users = payload["apps"]["morning-brief"]["d1"]["users"]
    assert app_users[ALICE]["total"]["turns"] == 2
    assert app_users[BOB]["total"]["turns"] == 1

    coverage = payload["coverage"]["d1"]
    assert coverage["attributed_user_turns"] == 3
    assert coverage["distinct_users"] == 2
    assert coverage["unattributed_user_turns"] == 0
    assert coverage["unattributed_user_turns_share"] == pytest.approx(0.0)


def test_requester_key_is_stored_verbatim_never_reshaped(shared: Path) -> None:
    """`platform:id` as emitted — no platform re-derivation, no normalisation."""
    weird = "matrix:@person:example.org"
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=weird),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit"),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert list(payload["users"]) == [weird]


# ── The grade split survives the user dimension ──────────────────────────────

def test_inferred_never_folds_into_a_users_total(shared: Path) -> None:
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="scheduled", cost=0.10),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit", cost=0.20),
        _annotation(session_id="s-a", app_id="app-a", grade="inferred", cost=0.50),
    ])

    blocks = rollup_bot(shared, BOT, today=TODAY)["apps"]["app-a"]["d1"]["users"][ALICE]
    assert blocks["total"]["turns"] == 2
    assert blocks["total"]["cost_estimated"] == pytest.approx(0.30)
    assert blocks["inferred"]["turns"] == 1
    assert blocks["inferred"]["cost_estimated"] == pytest.approx(0.50)
    # No key sums the inferred turn into the deterministic one.
    assert set(blocks) == {"total", "inferred"}


def test_per_bot_user_total_spans_every_app_and_the_unattributed_app(
    shared: Path,
) -> None:
    """A person's per-bot total counts their turns whether or not the APP
    could be attributed — the two dimensions are independent."""
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit", cost=0.10),
        _annotation(session_id="s-a", app_id="app-b", grade="explicit", cost=0.10),
        _annotation(session_id="s-a", grade="none", cost=0.10),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"][ALICE]["d1"]["turns"] == 3
    assert payload["apps"]["app-a"]["d1"]["users"][ALICE]["total"]["turns"] == 1
    assert payload["apps"]["app-b"]["d1"]["users"][ALICE]["total"]["turns"] == 1
    # The app-less turn is a user turn, not an app row.
    assert set(payload["apps"]) == {"app-a", "app-b"}


# ── Unattributed users are shown, and the REASON is not guessed ──────────────

def test_absent_requester_lands_in_unattributed_user_with_the_reason(
    shared: Path,
) -> None:
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        # A summary that states NO requester — cron/heartbeat, unkeyable ask,
        # or an opt-out. Distinct from "no summary at all".
        _summary(session_id="s-cron", requester=None),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit", cost=0.10),
        _annotation(session_id="s-cron", app_id="app-a", grade="explicit", cost=0.20),
        # A session with no summary in the window (still open / ends later).
        _annotation(session_id="s-open", app_id="app-a", grade="explicit", cost=0.30),
        # An annotation with no session id at all.
        _annotation(session_id="", app_id="app-a", grade="explicit", cost=0.40),
    ])

    payload = rollup_bot(shared, BOT, today=TODAY)
    un = payload["unattributed_user"]["d1"]
    assert un["turns"] == 3
    assert un["summary_without_requester_turns"] == 1
    assert un["no_summary_turns"] == 1
    assert un["no_session_id_turns"] == 1
    # The three reasons account for every unattributed turn — no residue.
    assert (
        un["no_session_id_turns"]
        + un["no_summary_turns"]
        + un["summary_without_requester_turns"]
    ) == un["turns"]

    # Nobody was invented for them.
    assert list(payload["users"]) == [ALICE]
    # And the app carries its own unattributed-user bucket.
    app = payload["apps"]["app-a"]["d1"]
    assert app["unattributed_user"]["turns"] == 3
    assert app["unattributed_user"]["cost_estimated"] == pytest.approx(0.90)

    coverage = payload["coverage"]["d1"]
    assert coverage["attributed_user_turns"] == 1
    assert coverage["unattributed_user_turns"] == 3
    assert coverage["unattributed_user_turns_share"] == pytest.approx(0.75)
    assert coverage["unattributed_user_cost_share"] == pytest.approx(0.9)


def test_coverage_share_is_none_not_zero_when_nothing_measured(shared: Path) -> None:
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}
    # None means "nothing measured" — 0.0 would read as perfect coverage.
    assert payload["coverage"]["d7"]["unattributed_user_turns_share"] is None
    assert payload["coverage"]["d7"]["unattributed_user_cost_share"] is None


def test_bot_with_no_summaries_contributes_nothing_and_fails_nothing(
    shared: Path,
) -> None:
    """The brief's fourth test: turns but no session summaries at all."""
    _write(shared, BOT, TODAY, [
        _annotation(session_id="s-a", app_id="app-a", grade="explicit", cost=0.10),
        _annotation(session_id="s-b", grade="none", cost=0.20),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}
    assert payload["unattributed_user"]["d1"]["turns"] == 2
    assert payload["unattributed_user"]["d1"]["no_summary_turns"] == 2
    # Attribution was AVAILABLE — there was simply nothing to attribute.
    # That is a different fact from "withheld", and the payload says so.
    assert payload["user_attribution"]["available"] is True
    assert payload["user_attribution"]["reason"] is None
    assert payload["coverage"]["d7"]["attributed_user_turns"] == 0


def test_missing_annotation_dir_is_an_empty_rollup_not_an_error(
    tmp_path: Path,
) -> None:
    payload = rollup_bot(tmp_path / "nowhere", "ghost_bot", today=TODAY)
    assert payload["bot_id"] == "ghost_bot"
    assert payload["users"] == {}
    assert payload["apps"] == {}
    assert payload["schema_version"] == SCHEMA_VERSION


# ── Windows match AL-1.3's ───────────────────────────────────────────────────

def test_windows_match_al_1_3(shared: Path) -> None:
    assert usage_by_user.WINDOWS is WINDOWS
    for offset, cost in ((0, 1.0), (3, 2.0), (10, 4.0), (40, 8.0)):
        day = TODAY - timedelta(days=offset)
        _write(shared, BOT, day, [
            _summary(session_id=f"s-{offset}", requester=ALICE),
            _annotation(session_id=f"s-{offset}", app_id="app-a",
                        grade="explicit", cost=cost),
        ])

    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["windows"] == dict(WINDOWS)
    user = payload["users"][ALICE]
    assert user["d1"]["turns"] == 1
    assert user["d7"]["turns"] == 2
    # The 40-days-ago pair is outside every window.
    assert user["d30"]["turns"] == 3
    assert user["d30"]["cost_estimated"] == pytest.approx(7.0)
    app = payload["apps"]["app-a"]
    assert app["d7"]["users"][ALICE]["total"]["turns"] == 2


def test_summary_written_the_day_after_its_turns_still_joins(shared: Path) -> None:
    """A session's summary lands when it ENDS — often the next UTC file."""
    _write(shared, BOT, TODAY - timedelta(days=1), [
        _annotation(session_id="s-late", app_id="app-a", grade="explicit",
                    cost=0.10, ts="2026-08-16T23:50:00.000Z"),
    ])
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-late", requester=ALICE,
                 ts="2026-08-17T00:10:00.000Z"),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    # The turn is in yesterday's file (d7/d30, not d1) but IS attributed.
    assert payload["users"][ALICE]["d1"]["turns"] == 0
    assert payload["users"][ALICE]["d7"]["turns"] == 1


def test_first_and_last_seen_span_the_window(shared: Path) -> None:
    _write(shared, BOT, TODAY - timedelta(days=5), [
        _summary(session_id="s-old", requester=ALICE),
        _annotation(session_id="s-old", app_id="app-a", grade="explicit",
                    ts="2026-08-12T08:00:00.000Z"),
    ])
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-new", requester=ALICE),
        _annotation(session_id="s-new", app_id="app-a", grade="explicit",
                    ts="2026-08-17T09:30:00.000Z"),
    ])
    entry = rollup_bot(shared, BOT, today=TODAY)["users"][ALICE]
    assert entry["first_seen_ts"] == "2026-08-12T08:00:00.000Z"
    assert entry["last_seen_ts"] == "2026-08-17T09:30:00.000Z"


# ── Evolve's own overhead is not a person ────────────────────────────────────

def test_evolve_overhead_is_split_out_of_unattributed_user(shared: Path) -> None:
    _write(shared, BOT, TODAY, [
        _annotation(session_id="s-sub", grade="none", cost=0.50),
        _annotation(session_id="s-plain", grade="none", cost=0.10),
    ])
    _write_cost_events(shared, BOT, TODAY, [
        {"session_id": "s-sub", "trigger_kind": "summarizer"},
    ])

    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["evolve_overhead"]["d1"]["turns"] == 1
    assert payload["evolve_overhead"]["d1"]["cost_estimated"] == pytest.approx(0.50)
    assert payload["unattributed_user"]["d1"]["turns"] == 1
    # Overhead is out of the coverage denominator, as in AL-1.3.
    coverage = payload["coverage"]["d1"]
    assert coverage["evolve_overhead_turns"] == 1
    assert coverage["user_turns_total"] == 1


def test_every_turn_lands_in_exactly_one_top_level_bucket(shared: Path) -> None:
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit"),
        _annotation(session_id="s-a", grade="none"),
        _annotation(session_id="s-open", app_id="app-a", grade="explicit"),
        _annotation(session_id="s-sub", grade="none"),
    ])
    _write_cost_events(shared, BOT, TODAY, [
        {"session_id": "s-sub", "trigger_kind": "forge"},
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    attributed = sum(e["d1"]["turns"] for e in payload["users"].values())
    total = (
        attributed
        + payload["unattributed_user"]["d1"]["turns"]
        + payload["evolve_overhead"]["d1"]["turns"]
    )
    assert (attributed, total) == (2, 4)


# ── The do-not-track chokepoint ──────────────────────────────────────────────

def test_per_bot_signal_off_withholds_every_identity(shared: Path) -> None:
    _network(shared, **{BOT: {"recurringRequestSignal": False}})
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit", cost=0.10),
    ])

    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}
    assert payload["user_attribution"]["available"] is False
    assert payload["user_attribution"]["reason"] == "signal_disabled"
    # The turn is still counted — withheld attribution, not lost usage.
    assert payload["unattributed_user"]["d1"]["turns"] == 1
    assert payload["apps"]["app-a"]["d1"]["unattributed_user"]["turns"] == 1


def test_opted_out_requester_is_dropped_but_their_peer_survives(
    shared: Path,
) -> None:
    _roster(shared, BOT, {"do_not_track": {ALICE: {"at": "2026-08-16"}}})
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _summary(session_id="s-b", requester=BOB),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit", cost=0.10),
        _annotation(session_id="s-b", app_id="app-a", grade="explicit", cost=0.20),
    ])

    payload = rollup_bot(shared, BOT, today=TODAY)
    assert list(payload["users"]) == [BOB]
    # Attribution is still AVAILABLE — one identity opted out, not the bot.
    assert payload["user_attribution"]["available"] is True
    assert payload["user_attribution"]["requesters_withheld"] == 1
    # The opted-out person's turn is unattributed, not attributed to someone.
    assert payload["unattributed_user"]["d1"]["turns"] == 1
    # It joins the "summary states no requester" bucket rather than getting a
    # bucket of its own: a per-turn "withheld" count would tell the operator
    # session by session that somebody here opted out. Only the aggregate
    # identity count (asserted above) is reported.
    assert payload["unattributed_user"]["d1"]["summary_without_requester_turns"] == 1


def test_blocked_requester_is_dropped_too(shared: Path) -> None:
    _roster(shared, BOT, {"blocked": {BOB: {"reason": "spam"}}})
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-b", requester=BOB),
        _annotation(session_id="s-b", app_id="app-a", grade="explicit"),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}


def test_unreadable_roster_overlay_withholds_rather_than_guesses(
    shared: Path,
) -> None:
    _roster(shared, BOT, {})
    (shared / "rosters" / f"{BOT}.json").write_text("{ not json")
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit"),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}
    assert payload["user_attribution"]["available"] is False
    assert payload["user_attribution"]["reason"] == "overlay_unreadable"


def test_record_self_reporting_another_bot_is_gated_against_that_bot(
    shared: Path,
) -> None:
    """A summary sitting in this bot's directory but claiming another bot
    must clear the OTHER bot's switch too — it cannot shop for one that is
    still on."""
    _network(shared, **{"other_bot": {"recurringRequestSignal": False}})
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-x", requester=ALICE, bot_id="other_bot"),
        _annotation(session_id="s-x", app_id="app-a", grade="explicit"),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}


def test_gate_unavailable_fails_closed(shared: Path, monkeypatch) -> None:
    """No importable chokepoint ⇒ no attribution at all, and it says why."""
    monkeypatch.setattr(usage_by_user, "_gate_module", lambda: None)
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit"),
    ])
    payload = rollup_bot(shared, BOT, today=TODAY)
    assert payload["users"] == {}
    assert payload["user_attribution"]["available"] is False
    assert payload["user_attribution"]["reason"] == WITHHELD_GATE_UNAVAILABLE


def test_gate_report_is_emitted_even_on_the_all_clear(shared: Path) -> None:
    """A gate that says nothing when it withholds nothing is
    indistinguishable from a gate that is not running."""
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="app-a", grade="explicit"),
    ])
    attribution = rollup_bot(shared, BOT, today=TODAY)["user_attribution"]
    assert attribution["gate_report"]["rows_in"] == 1
    assert attribution["gate_report"]["rows_kept"] == 1
    assert attribution["gate_report"]["rows_excluded"] == 0
    assert attribution["requesters_in"] == 1
    assert attribution["sessions_with_requester"] == 1
    assert attribution["sessions_seen"] == 1


def test_session_requesters_reports_summaries_without_a_requester(
    shared: Path,
) -> None:
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _summary(session_id="s-cron", requester=None),
    ])
    mapping, summarised, report = session_requesters(shared, BOT, today=TODAY)
    assert mapping == {"s-a": ALICE}
    assert summarised == {"s-a", "s-cron"}
    assert report["sessions_seen"] == 2


# ── Output discipline (0644 / atomic / tri-state) ────────────────────────────

def test_write_is_0644_and_atomic(shared: Path) -> None:
    payload = rollup_bot(shared, BOT, today=TODAY)
    out = write_usage_by_user(shared, BOT, payload)
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert json.loads(out.read_text())["bot_id"] == BOT
    # No temp file survives the write.
    assert not list(out.parent.glob(".usage-by-user-*.tmp"))


def test_dry_run_writes_nothing(shared: Path) -> None:
    run_usage_by_user(BOT, shared, dry_run=True, today=TODAY)
    assert not usage_by_user_path(shared, BOT).exists()


def test_load_is_tri_state(shared: Path) -> None:
    # Absent → {} ("not measured"), never a zeroed payload.
    assert load_usage_by_user(shared, BOT) == {}
    payload = run_usage_by_user(BOT, shared, today=TODAY)
    assert load_usage_by_user(shared, BOT)["as_of_date"] == payload["as_of_date"]
    # Corrupt → {} as well; a half-written file must not crash a reader.
    usage_by_user_path(shared, BOT).write_text("{ truncated")
    assert load_usage_by_user(shared, BOT) == {}


def test_coverage_attribution_is_per_window(shared: Path) -> None:
    _write(shared, BOT, TODAY - timedelta(days=10), [
        _summary(session_id="s-old", requester=ALICE),
        _annotation(session_id="s-old", app_id="app-a", grade="explicit"),
    ])
    coverage = rollup_bot(shared, BOT, today=TODAY)["coverage"]
    assert coverage["d30"]["attributed_user_turns"] == 1
    assert coverage["d30"]["distinct_users"] == 1
    # The same person, outside the narrower window, contributes nothing to it.
    assert coverage["d7"]["attributed_user_turns"] == 0
    assert coverage["d7"]["distinct_users"] == 0


def test_report_mode_prints_and_writes_nothing(
    shared: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(shared, BOT, TODAY, [
        _summary(session_id="s-a", requester=ALICE),
        _annotation(session_id="s-a", app_id="morning-brief",
                    grade="scheduled", cost=0.10),
    ])
    run_usage_by_user(BOT, shared, report=True, today=TODAY)
    out = capsys.readouterr().out
    assert ALICE in out
    assert "morning-brief" in out
    assert not usage_by_user_path(shared, BOT).exists()
