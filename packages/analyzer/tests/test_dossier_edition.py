"""Tests for the weekly dossier edition writer (the spine's clock-starter).

What is actually being pinned here, in the order the brief asked for it:

  1. a fixture pod yields a COMPLETE edition — every top-level block present
     and populated from the fixture's producers;
  2. absent producers yield NULLS — not zeros. This is the load-bearing one:
     the whole value of a longitudinal spine is that "quiet" and "not
     measured" stay distinguishable forever, and a zero written today can
     never be un-confused later;
  3. a re-run inside the same week is idempotent — byte-identical payload
     given the same clock and the same disk;
  4. week boundaries are computed in the POD's timezone, not UTC and not the
     test host's — including the case where the two disagree about which week
     a moment falls in.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dossier import store, window as win
from dossier.edition import SCHEMA_VERSION, build_edition
from dossier_edition import main, run_edition

TZ = "America/Los_Angeles"

# Mid-week inside ISO 2026-W35 (Mon 2026-08-24 .. Sun 2026-08-30, pod-local).
NOW_MIDWEEK = datetime(2026, 8, 27, 17, 0, tzinfo=timezone.utc)
# Monday 2026-08-31 04:10 pod-local == 11:10Z — the scheduled run's instant.
NOW_MONDAY = datetime(2026, 8, 31, 11, 10, tzinfo=timezone.utc)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _network(shared: Path, members: list[str], primary: str) -> dict:
    return {
        "sharedDir": str(shared),
        "members": members,
        "primary": primary,
        "timezone": TZ,
    }


@pytest.fixture
def bare_pod(tmp_path: Path) -> tuple[Path, dict]:
    """A pod with a roster and nothing else — no producer has written a byte."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net = _network(shared, ["bot_a", "bot_b"], "bot_a")
    (shared / "network.json").write_text(json.dumps(net))
    return shared, net


@pytest.fixture
def fixture_pod(tmp_path: Path) -> tuple[Path, dict, dict]:
    """A pod where every producer this edition reads has written something.

    Returns ``(shared_dir, network, homes)`` — ``homes`` maps bot id to the
    fake home dir so the test can inject manifest locations without touching
    ``pwd``.
    """
    shared = tmp_path / "shared"
    (shared / "metrics" / "bot_a").mkdir(parents=True)
    (shared / "bot_a").mkdir(parents=True)
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "log").mkdir(parents=True)
    net = _network(shared, ["bot_a", "bot_b"], "bot_a")
    (shared / "network.json").write_text(json.dumps(net))

    # ── cost rollups: two of the week's seven days, one bot ──────────────
    for day, usd in (("2026-08-24", 0.5), ("2026-08-26", 0.25)):
        (shared / "metrics" / "bot_a" / f"cost-{day}.json").write_text(json.dumps({
            "schema_version": 1, "bot_id": "bot_a", "date": day,
            "total_usd": usd, "input_tokens": 100, "output_tokens": 20,
            "cache_read_tokens": 5, "cache_write_tokens": 1, "event_count": 3,
            "by_model": {
                "claude-opus-5": {
                    "cost_usd": usd, "input_tokens": 100, "output_tokens": 20,
                    "cache_read_tokens": 5, "cache_write_tokens": 1,
                    "event_count": 3,
                }
            },
        }))

    # ── usage-by-app rollup ──────────────────────────────────────────────
    (shared / "bot_a" / "usage-by-app.json").write_text(json.dumps({
        "schema_version": 4, "bot_id": "bot_a", "as_of_date": "2026-08-27",
        "coverage": {
            "d7": {"attributed_turns": 10, "inferred_turns": 2,
                   "unattributed_turns": 6, "legacy_schema_turns": 1,
                   "evolve_overhead_turns": 4, "app_turns_total": 18,
                   "unattributed_turns_share": 0.3333},
            "d30": {"attributed_turns": 40, "inferred_turns": 5,
                    "unattributed_turns": 10, "legacy_schema_turns": 2,
                    "evolve_overhead_turns": 9, "app_turns_total": 55,
                    "unattributed_turns_share": 0.1818},
        },
        "apps": {
            "morning_brief": {
                "first_seen_ts": "2026-08-01T00:00:00Z",
                "last_seen_ts": "2026-08-26T00:00:00Z",
                "d7": {"total": {"turns": 10, "cost_estimated": 0.2},
                       "inferred": {"turns": 2, "cost_estimated": 0.01}},
                "d30": {"total": {"turns": 40, "cost_estimated": 0.9},
                        "inferred": {"turns": 5, "cost_estimated": 0.05}},
            }
        },
    }))

    # ── annotation footprint: bot_a worked, bot_b did not ────────────────
    ann = shared / "annotations" / "bot_a"
    ann.mkdir(parents=True)
    (shared / "annotations" / "bot_b").mkdir(parents=True)
    for day in ("2026-08-23", "2026-08-24", "2026-08-26", "2026-08-27"):
        rows = [{"type": "turn_annotation", "ts": f"{day}T12:00:00Z"}]
        # A daily schedule that fired on three of these four days — 08-25 is
        # the miss the fire history has to name. (08-23 is outside the
        # activity window's own count but inside the 28-day fire window.)
        if day != "2026-08-26":
            rows.append({"type": "turn_annotation", "ts": f"{day}T07:00:00Z",
                         "app_id": "morning_brief",
                         "app_attribution": "scheduled"})
        # A person running the app by hand is NOT a scheduled run.
        rows.append({"type": "turn_annotation", "ts": f"{day}T15:00:00Z",
                     "app_id": "morning_brief", "app_attribution": "explicit"})
        (ann / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
    (ann / "cost_events-2026-08-24.jsonl").write_text("{}\n")
    # Well outside the window — must not be counted.
    (ann / "2026-07-01.jsonl").write_text("{}\n")

    # ── signals: one firing + its opening transition inside the window ───
    (shared / "signals" / "firing" / "sig1.json").write_text(json.dumps({
        "id": "sig1", "signature": "audit:x:pod", "producer": "audit",
        "type": "x", "severity": "warn", "scope": "bot", "bot_id": "bot_a",
        "state": "firing",
    }))
    (shared / "signals" / "log" / "2026-08-25.jsonl").write_text(
        json.dumps({"at": "2026-08-25T12:00:00Z", "signal_id": "sig1",
                    "producer": "audit", "type": "x", "scope": "bot",
                    "bot_id": "bot_a", "from_state": None,
                    "to_state": "firing", "actor": "monitor",
                    "reason": ""}) + "\n"
    )

    # ── manifests: one defined app, one discovered draft ─────────────────
    homes = {}
    for bot in ("bot_a", "bot_b"):
        home = tmp_path / "homes" / bot
        homes[bot] = home
        (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
        # bot_a matches the baseline below; bot_b runs a looser exec policy,
        # so the census has one conform row and one real deviation to count.
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps({
            "tools": {"exec": {"security": "allowlist" if bot == "bot_a" else "full"}},
        }))

    # ── pod baseline: one declared surface, four undeclared ──────────────
    (shared / "pod-baseline.json").write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-08-01T00:00:00Z",
        "surfaces": {"exec_policy": "allowlist"},
        "undeclared": ["tool_profile", "browser", "context_profile",
                       "model_policy"],
        "exceptions": [],
    }))
    mdir = homes["bot_a"] / ".openclaw" / "workspace" / "manifests"
    (mdir / "morning_brief.json").write_text(json.dumps({
        "app_id": "morning_brief", "name": "Morning Brief",
        "definition_status": "defined",
    }))
    (mdir / "draft_thing.json").write_text(json.dumps({
        "draft_id": "draft_thing", "name": "Draft Thing",
        "definition_status": "discovered",
    }))
    # A dotfile the scanner leaves beside the manifests; counting it would
    # inflate every draft number in the spine.
    (mdir / ".scan-status.json").write_text(json.dumps({"ok": True}))

    return shared, net, homes


def _build(shared: Path, net: dict, *, now=NOW_MIDWEEK, homes=None, use_now=True):
    tz = win.resolve_timezone(net)
    year, week = (win.current_week(now, tz) if use_now
                  else win.previous_week(now, tz))
    w = win.window_for(year, week, tz, now=now)
    return build_edition(
        shared, net, w, now=now,
        bot_home=(lambda b: homes[b]) if homes else (lambda b: Path("/nonexistent") / b),
        home_overrides=dict(homes) if homes else None,
    )


def _zero_paths(node, path="") -> list[str]:
    """Every leaf path in ``node`` whose value is a literal ``0`` / ``0.0``.

    ``bool`` is excluded on purpose — ``False`` is a claim, not a count.
    """
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _zero_paths(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _zero_paths(v, f"{path}[{i}]")]
    if isinstance(node, (int, float)) and not isinstance(node, bool) and node == 0:
        return [path]
    return []


# ── 1. a fixture pod yields a complete edition ───────────────────────────────

def test_fixture_pod_yields_a_complete_edition(fixture_pod):
    shared, net, homes = fixture_pod
    ed = _build(shared, net, homes=homes)

    assert ed["schema_version"] == SCHEMA_VERSION
    assert ed["edition_id"] == "2026-W35"
    assert ed["computed_at"] == "2026-08-27T17:00:00Z"
    # Every block the brief names is present.
    for key in ("pod", "per_bot", "per_app", "users", "drafts", "drift",
                "costs", "signals", "window"):
        assert key in ed, key

    # costs: the week window summed the two days that had rollups, and says
    # so — days_with_data is what stops 2/7 reading as a full week.
    assert ed["costs"]["total_usd"] == pytest.approx(0.75)
    assert ed["costs"]["by_bot"]["bot_a"]["days_with_data"] == 2
    assert ed["costs"]["by_bot"]["bot_a"]["days_in_window"] == 7
    assert ed["costs"]["by_model"]["claude-opus-5"]["cost_usd"] == pytest.approx(0.75)

    # per_app: deterministic total and inferred kept apart, never merged.
    app = ed["per_app"]["apps"]["morning_brief"]
    assert app["d7"] == {"turns": 10, "cost_estimated": 0.2, "inferred_turns": 2}
    assert app["bots"] == ["bot_a"]
    assert ed["per_app"]["bots_without_rollup"] == ["bot_b"]

    # coverage rides beside apps — without it, an empty apps map is unreadable.
    cov = ed["per_app"]["coverage"]["d7"]
    assert cov["attributed_turns"] == 10
    assert cov["unattributed_turns"] == 6
    assert cov["legacy_schema_turns"] == 1
    assert cov["unattributed_turns_share"] == pytest.approx(6 / 18, abs=1e-4)
    assert ed["per_app"]["coverage_by_bot"]["bot_a"]["d30"]["attributed_turns"] == 40

    # drafts: the dotfile is not a manifest.
    assert ed["drafts"]["manifests"] == 2
    assert ed["drafts"]["definition_status"] == {"defined": 1, "discovered": 1}
    assert set(ed["drafts"]["bands"]) >= {"ready", "emerging", "weak", "unscored"}
    assert sum(ed["drafts"]["bands"].values()) == 2

    # signals: snapshot and windowed transitions are separate measurements.
    assert ed["signals"]["active"]["total"] == 1
    assert ed["signals"]["active"]["by_severity"]["warn"] == 1
    assert ed["signals"]["transitions"]["by_kind"] == {"opened": 1}

    # drift: one declared surface across two bots — bot_a conforms, bot_b is
    # loosened. The four undeclared surfaces are named, not scored.
    drift = ed["drift"]
    assert drift["bots"] == 2
    assert drift["counts"]["conform"] == 1
    assert drift["counts"]["loosened"] == 1
    assert drift["counts"]["undeclared"] == 8  # 4 surfaces x 2 bots
    assert sorted(drift["undeclared_surfaces"]) == [
        "browser", "context_profile", "model_policy", "tool_profile",
    ]
    assert drift["stale_exceptions"] == 0

    # roster activity, from the annotation footprint (present on every pod,
    # unlike cost_rollup) — and the payload says which definition it used.
    roster = ed["pod"]["roster"]
    assert roster["members"] == 2
    assert roster["active_in_window"] == 1
    assert roster["active_ids"] == ["bot_a"]
    assert "annotation" in roster["activity_definition"]
    act = ed["per_bot"]["bot_a"]["activity"]
    assert act["days_with_turn_annotation_files"] == 3
    assert act["days_with_cost_event_files"] == 1
    assert act["active"] is True
    assert ed["per_bot"]["bot_b"]["activity"]["active"] is False

    # per_bot covers the whole roster, including the silent bot.
    assert set(ed["per_bot"]) == {"bot_a", "bot_b"}
    assert ed["per_bot"]["bot_a"]["active_in_window"] is True
    assert ed["per_bot"]["bot_b"]["active_in_window"] is False


def test_every_number_in_the_edition_is_json_serialisable(fixture_pod):
    """The edition is a wire format before it is anything else."""
    shared, net, homes = fixture_pod
    ed = _build(shared, net, homes=homes)
    assert json.loads(json.dumps(ed)) == ed


# ── 2. absent producers yield nulls, never zeros ─────────────────────────────

def test_absent_producers_yield_nulls_not_zeros(bare_pod):
    shared, net = bare_pod
    ed = _build(shared, net)

    assert ed["costs"] is None
    assert ed["per_app"] is None
    assert ed["drafts"] is None
    assert ed["drift"] is None
    assert ed["signals"] is None
    assert ed["pod"]["roster"]["activity_definition"] is None

    # …and nowhere in the whole payload did a missing producer become a 0.
    # This is the strong form of the law: on a pod where nothing has been
    # measured, there is no number to report, so a literal 0 anywhere in the
    # edition is a bug by construction. (``window.days`` and the roster count
    # are the only counts that exist, and neither is zero.)
    assert _zero_paths(ed) == []

    # per_bot rows exist for the roster but carry nulls, and — the subtle one
    # — activity is UNKNOWN rather than False: with no cost producer we
    # cannot claim the bot was idle.
    for bot in ("bot_a", "bot_b"):
        assert ed["per_bot"][bot]["costs"] is None
        assert ed["per_bot"][bot]["drafts"] is None
        assert ed["per_bot"][bot]["activity"] is None
        assert ed["per_bot"][bot]["active_in_window"] is None
    assert ed["pod"]["roster"]["active_in_window"] is None
    assert ed["pod"]["roster"]["members"] == 2  # the roster itself IS known


def test_users_is_null_with_schema_until_its_producer_lands(bare_pod):
    """The field's shape is visible from day one; its values stay null."""
    shared, net = bare_pod
    users = _build(shared, net)["users"]
    assert users["available"] is False
    assert users["producer"] == "usage_by_user"
    assert users["by_app"] is None
    assert users["requesters"] is None


def _write_user_rollup(shared: Path, bot: str = "bot_a", *,
                       available: bool = True, reason: str | None = None,
                       withheld: int = 0) -> None:
    """The real per-user rollup file, in the shape ``usage_by_user`` writes."""
    (shared / bot).mkdir(parents=True, exist_ok=True)
    (shared / bot / "usage-by-user.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": bot, "as_of_date": "2026-08-27",
        "user_attribution": {"available": available, "reason": reason,
                             "requesters_withheld": withheld},
        "users": {
            "slack:U1": {"d1": {"turns": 2}, "d7": {"turns": 7, "cost_estimated": 0.11},
                         "d30": {"turns": 20}},
            "slack:U2": {"d1": {"turns": 1}, "d7": {"turns": 3, "cost_estimated": 0.04},
                         "d30": {"turns": 9}},
        },
        "apps": {
            "morning_brief": {
                "d7": {"users": {"slack:U1": {"total": {"turns": 7,
                                                        "cost_estimated": 0.11},
                                              "inferred": {"turns": 2}}},
                       "unattributed_user": {"turns": 1}},
            },
        },
    }))


def test_users_reads_the_producer_that_actually_writes_the_file(fixture_pod):
    """The rollup is a SIBLING file, not a key inside the per-app rollup.

    The regression this pins: the block used to look for ``users`` inside
    ``usage-by-app.json`` — a key ``usage_by_app`` has never written — so
    the card said "nothing here records who is using the pod" on a pod that
    had a per-person rollup sitting next to it. A block keyed on a name no
    writer emits is permanently, silently true.
    """
    shared, net, homes = fixture_pod
    _write_user_rollup(shared)

    users = _build(shared, net, homes=homes)["users"]
    assert users["available"] is True
    assert users["producer"] == "usage_by_user"
    assert users["requesters"] == ["slack:U1", "slack:U2"]
    assert users["by_person"]["slack:U1"]["turns"] == 7
    assert users["by_app"]["morning_brief"]["slack:U1"]["turns"] == 7


def test_a_users_key_inside_the_per_app_rollup_is_not_mistaken_for_a_rollup(
    fixture_pod,
):
    """Writing the OLD key must not resurrect the block it never filled."""
    shared, net, homes = fixture_pod
    path = shared / "bot_a" / "usage-by-app.json"
    payload = json.loads(path.read_text())
    payload["users"] = {
        "morning_brief": {"slack:U1": {"turns": 7, "cost_estimated": 0.11}}
    }
    path.write_text(json.dumps(payload))

    assert _build(shared, net, homes=homes)["users"]["available"] is False


def test_a_gate_that_withheld_everyone_is_not_reported_as_nobody(fixture_pod):
    """Withheld is withheld. Reporting it as "no people" inverts the fact."""
    shared, net, homes = fixture_pod
    _write_user_rollup(shared, available=False, reason="do-not-track is on",
                       withheld=2)

    users = _build(shared, net, homes=homes)["users"]
    assert users["available"] is False
    assert users["requesters"] is None
    assert users["requesters_withheld"] == 2
    assert "do-not-track" in users["note"]


def test_a_bot_with_no_cost_rollup_is_null_not_an_all_zero_row(fixture_pod):
    shared, net, homes = fixture_pod
    ed = _build(shared, net, homes=homes)
    assert ed["costs"]["by_bot"]["bot_b"] is None
    assert ed["costs"]["by_bot"]["bot_a"] is not None


def test_drift_is_null_when_the_pod_declared_no_baseline(fixture_pod):
    """"0 deviations" for a pod with no baseline would be the single most
    misleading number the edition could carry."""
    shared, net, homes = fixture_pod
    (shared / "pod-baseline.json").unlink()
    assert _build(shared, net, homes=homes)["drift"] is None


def test_unreadable_manifest_dir_is_reported_not_counted_as_zero(fixture_pod):
    shared, net, homes = fixture_pod
    ed = _build(shared, net, homes=homes)
    # bot_b's manifests dir exists but is empty -> a real zero.
    assert ed["drafts"]["by_bot"]["bot_b"]["manifests"] == 0
    assert ed["drafts"]["bots_unreadable"] == []

    # Point bot_b at a dir that does not exist -> null + named in the list.
    homes2 = dict(homes)
    homes2["bot_b"] = shared / "no-such-home"
    ed2 = _build(shared, net, homes=homes2)
    assert ed2["drafts"]["by_bot"]["bot_b"] is None
    assert ed2["drafts"]["bots_unreadable"] == ["bot_b"]


def test_zero_apps_with_unattributed_turns_is_not_the_same_as_no_usage(fixture_pod):
    """The live 2026-W35 case, pinned.

    Both pod bots reported an empty ``apps`` map over ~147 turns that all
    landed unattributed. An edition that carried only ``apps: {}`` would tell
    a future reader the pod ran no apps. Coverage is what makes the two
    distinguishable, forever.
    """
    shared, net, homes = fixture_pod
    path = shared / "bot_a" / "usage-by-app.json"
    payload = json.loads(path.read_text())
    payload["apps"] = {}
    payload["coverage"]["d7"] = {
        "attributed_turns": 0, "inferred_turns": 0, "unattributed_turns": 74,
        "legacy_schema_turns": 0, "evolve_overhead_turns": 3,
        "app_turns_total": 74,
    }
    path.write_text(json.dumps(payload))

    per_app = _build(shared, net, homes=homes)["per_app"]
    assert per_app["apps"] == {}
    assert per_app["coverage"]["d7"]["unattributed_turns"] == 74
    assert per_app["coverage"]["d7"]["unattributed_turns_share"] == 1.0


def test_coverage_share_is_null_when_there_were_no_turns_at_all(fixture_pod):
    """No turns is not perfect attribution."""
    shared, net, homes = fixture_pod
    path = shared / "bot_a" / "usage-by-app.json"
    payload = json.loads(path.read_text())
    payload["apps"] = {}
    for w in ("d7", "d30"):
        payload["coverage"][w] = {k: 0 for k in payload["coverage"][w]}
    path.write_text(json.dumps(payload))

    cov = _build(shared, net, homes=homes)["per_app"]["coverage"]["d7"]
    assert cov["app_turns_total"] == 0
    assert cov["unattributed_turns_share"] is None


def test_activity_answers_even_when_cost_rollup_is_not_scheduled(fixture_pod):
    """The case the live pod actually exhibits.

    ``cost_rollup`` is not installed on every pod; where it isn't, the whole
    ``costs`` block is legitimately null while the bots were plainly busy. The
    annotation footprint is what keeps "was anyone working?" answerable there
    — and the answer must not silently borrow the cost block's definition.
    """
    shared, net, homes = fixture_pod
    for f in (shared / "metrics" / "bot_a").glob("cost-*.json"):
        f.unlink()
    ed = _build(shared, net, homes=homes)
    assert ed["costs"] is None
    assert ed["per_bot"]["bot_a"]["costs"] is None
    assert ed["per_bot"]["bot_a"]["active_in_window"] is True
    assert ed["pod"]["roster"]["active_in_window"] == 1


def test_activity_falls_back_to_cost_rollups_when_annotations_are_absent(fixture_pod):
    import shutil

    shared, net, homes = fixture_pod
    shutil.rmtree(shared / "annotations")
    ed = _build(shared, net, homes=homes)
    assert ed["per_bot"]["bot_a"]["activity"] is None
    assert ed["per_bot"]["bot_a"]["active_in_window"] is True   # from the rollup
    assert ed["per_bot"]["bot_b"]["active_in_window"] is False
    assert "cost rollup" in ed["pod"]["roster"]["activity_definition"]


def test_activity_scan_is_bounded_to_the_window(fixture_pod):
    """The July file in the fixture must not leak into an August edition."""
    shared, net, homes = fixture_pod
    act = _build(shared, net, homes=homes)["per_bot"]["bot_a"]["activity"]
    # Seven pod-local days span at most nine UTC-dated files (one extra at
    # each edge) — the payload documents that widening; it must not be wider.
    assert act["utc_days_scanned"] <= 9
    assert act["days_with_turn_annotation_files"] == 3


# ── fire history (the reliability module's measurement) ──────────────────────

def test_fire_history_counts_scheduled_runs_day_by_day(fixture_pod):
    """The reliability spine: which days a scheduled app actually showed up."""
    shared, net, homes = fixture_pod
    fires = _build(shared, net, homes=homes)["fires"]

    app = fires["apps"]["morning_brief"]
    assert app["bots"] == ["bot_a"]
    assert app["days_ran"] == 3
    assert app["first_run_date"] == "2026-08-23"
    assert app["runs_by_date"] == {"2026-08-23": 1, "2026-08-24": 1,
                                   "2026-08-27": 1}
    # Daily rhythm, read off its own runs — nothing declares one.
    assert app["cadence_days"] == 1
    # Coverage opens at the FIRST run, not at the window edge: an app
    # installed on Sunday did not miss the three weeks before it existed.
    assert app["days_covered"] == 5      # 08-23 .. 08-27, the day it ran
    assert app["missed_dates"] == ["2026-08-25", "2026-08-26"]
    assert app["days_missed"] == 2


def test_a_hand_run_app_is_not_counted_as_a_schedule_that_fired(fixture_pod):
    """``explicit`` is a person asking. Counting it would report a pod whose
    crons are all dead as perfectly reliable — the exact failure the module
    exists to catch."""
    shared, net, homes = fixture_pod
    app = _build(shared, net, homes=homes)["fires"]["apps"]["morning_brief"]
    # Four days carry an explicit turn; only the three scheduled ones count.
    assert app["runs_total"] == 3


def test_the_fire_window_stops_at_today_not_at_the_end_of_an_open_week(
    fixture_pod,
):
    """A week still in progress must not count its own future as misses.

    The edition here is computed on the Thursday of an open week. Running
    the strip to the week's Sunday would paint three days that have not
    happened yet as days the app failed to run.
    """
    shared, net, homes = fixture_pod
    window = _build(shared, net, homes=homes)["fires"]["window"]
    assert window["days"] == 28
    assert len(window["dates"]) == 28
    assert window["last_date"] == "2026-08-27"    # the day the edition ran
    assert window["first_date"] == "2026-07-31"


def test_a_finished_week_runs_the_strip_to_its_last_day(fixture_pod):
    """And a week that has ended does use the whole week — the clamp is a
    ceiling on the future, not a shortening of the past."""
    shared, net, homes = fixture_pod
    ed = _build(shared, net, homes=homes, now=NOW_MONDAY, use_now=False)
    assert ed["fires"]["window"]["last_date"] == "2026-08-30"


def test_an_app_with_a_cron_and_no_runs_is_named_rather_than_zeroed(fixture_pod):
    """The explain-and-remediate row. "Evolve installed a cron for this and
    nothing here has recorded it running" is a to-do about the measuring,
    not a verdict about the app — so it is named, never folded in as a
    zero-run app."""
    shared, net, homes = fixture_pod
    (shared / "bot_a" / "app-cron-map.json").write_text(json.dumps({
        "morning-brief-0700": "morning_brief",
        "weekly-recap-mon": "weekly_recap",
    }))
    fires = _build(shared, net, homes=homes)["fires"]
    assert fires["apps_without_history"] == ["weekly_recap"]
    assert "weekly_recap" not in fires["apps"]
    assert fires["apps"]["morning_brief"]["installed_by_evolve"] is True


def test_a_weekly_rhythm_is_read_off_the_runs_rather_than_assumed_daily(
    fixture_pod,
):
    """Nothing on a real pod declares a schedule — every manifest on the live
    mini carries an empty ``configured_schedules``. So the cadence comes from
    the runs, and a weekly app is not reported as missing six days in seven.
    """
    shared, net, homes = fixture_pod
    ann = shared / "annotations" / "bot_a"
    for day in ("2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"):
        (ann / f"{day}.jsonl").write_text(json.dumps({
            "type": "turn_annotation", "ts": f"{day}T09:00:00Z",
            "app_id": "weekly_recap", "app_attribution": "scheduled"}) + "\n")

    app = _build(shared, net, homes=homes)["fires"]["apps"]["weekly_recap"]
    assert app["cadence_days"] == 7
    assert app["days_missed"] == 0
    assert app["days_covered"] == 4          # the four Mondays, not 25 days


def test_too_few_runs_yields_no_miss_claim_rather_than_a_guessed_one(
    fixture_pod,
):
    shared, net, homes = fixture_pod
    (shared / "annotations" / "bot_b").mkdir(parents=True, exist_ok=True)
    (shared / "annotations" / "bot_b" / "2026-08-26.jsonl").write_text(
        json.dumps({"type": "turn_annotation", "ts": "2026-08-26T09:00:00Z",
                    "app_id": "brand_new", "app_attribution": "scheduled"})
        + "\n")
    app = _build(shared, net, homes=homes)["fires"]["apps"]["brand_new"]
    assert app["cadence_days"] is None
    assert app["days_missed"] is None
    assert app["missed_dates"] is None
    assert app["days_ran"] == 1


def test_fires_is_null_when_the_annotation_store_does_not_exist(bare_pod):
    """Tri-state: "nothing recorded when apps run" is not "nothing ran"."""
    shared, net = bare_pod
    assert _build(shared, net)["fires"] is None


# ── 3. idempotent re-run ─────────────────────────────────────────────────────

def test_rerun_within_the_same_week_is_idempotent(fixture_pod):
    shared, net, homes = fixture_pod
    first = run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    on_disk_1 = store.edition_path(shared, "2026-W35").read_text()
    second = run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    on_disk_2 = store.edition_path(shared, "2026-W35").read_text()

    assert first == second
    assert on_disk_1 == on_disk_2
    assert store.iter_edition_ids(shared) == ["2026-W35"]


def test_a_run_writes_exactly_one_edition_and_leaves_neighbours_alone(fixture_pod):
    shared, net, homes = fixture_pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)      # 2026-W35
    w35_before = store.edition_path(shared, "2026-W35").read_text()
    run_edition(shared, net, now=NOW_MIDWEEK, week="2026-W30")   # a backfill

    assert store.iter_edition_ids(shared) == ["2026-W30", "2026-W35"]
    assert store.edition_path(shared, "2026-W35").read_text() == w35_before


def test_a_sealed_edition_is_immutable_without_force(fixture_pod):
    shared, net, homes = fixture_pod
    # Monday run over the completed week -> sealed.
    sealed = run_edition(shared, net, now=NOW_MONDAY)
    assert sealed["edition_id"] == "2026-W35"
    assert sealed["sealed"] is True
    body = store.edition_path(shared, "2026-W35").read_text()

    with pytest.raises(store.SealedEditionError):
        run_edition(shared, net, now=NOW_MONDAY)
    assert store.edition_path(shared, "2026-W35").read_text() == body

    run_edition(shared, net, now=NOW_MONDAY, force=True)
    assert store.load_edition(shared, "2026-W35")["sealed"] is True


def test_an_open_week_is_unsealed_and_freely_overwritten(fixture_pod):
    shared, net, homes = fixture_pod
    ed = run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    assert ed["sealed"] is False
    assert ed["window"]["complete"] is False
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)  # no raise


def test_edition_file_is_operator_readable(fixture_pod):
    shared, net, homes = fixture_pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    mode = store.edition_path(shared, "2026-W35").stat().st_mode & 0o777
    assert mode == store.EDITION_MODE == 0o644


# ── 4. week boundaries, in the pod's timezone ────────────────────────────────

def test_window_bounds_are_pod_local_monday_to_monday(bare_pod):
    shared, net = bare_pod
    w = _build(shared, net)["window"]
    assert w["timezone"] == TZ
    assert w["start"] == "2026-08-24T00:00:00-07:00"
    assert w["end"] == "2026-08-31T00:00:00-07:00"
    assert (w["first_date"], w["last_date"]) == ("2026-08-24", "2026-08-30")
    assert w["days"] == 7


def test_a_moment_utc_calls_next_week_still_belongs_to_this_pod_week():
    """The bug this exists to prevent: a pod west of UTC losing its Sunday.

    2026-08-30 22:00 in Los Angeles is 2026-08-31 05:00Z — Monday of the NEXT
    ISO week in UTC, but still Sunday of 2026-W35 for the pod. The edition
    must follow the pod's calendar.
    """
    tz = win.resolve_timezone({"timezone": TZ})
    moment = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    assert moment.isocalendar()[1] == 36           # UTC says week 36
    assert win.current_week(moment, tz) == (2026, 35)  # the pod says 35
    w = win.window_for(2026, 35, tz, now=moment)
    assert w.contains(moment)
    assert w.complete is False


def test_east_of_utc_pod_gets_its_own_week_boundary():
    """The mirror case — a pod east of UTC crosses into the new week first."""
    tz = win.resolve_timezone({"timezone": "Australia/Sydney"})
    moment = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)  # Mon 01:00 AEST
    assert moment.isocalendar()[1] == 35
    assert win.current_week(moment, tz) == (2026, 36)


def test_previous_week_is_stable_wherever_in_the_week_the_run_fires(bare_pod):
    """The scheduled Monday run and a Thursday re-run must target the same week."""
    shared, net = bare_pod
    tz = win.resolve_timezone(net)
    monday = datetime(2026, 8, 31, 11, 10, tzinfo=timezone.utc)
    thursday = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    assert win.previous_week(monday, tz) == win.previous_week(thursday, tz) == (2026, 35)


def test_the_scheduled_default_records_the_completed_week(fixture_pod):
    shared, net, homes = fixture_pod
    ed = run_edition(shared, net, now=NOW_MONDAY)
    assert ed["edition_id"] == "2026-W35"
    assert ed["window"]["complete"] is True
    assert ed["sealed"] is True


def test_signal_transitions_use_the_pod_window_not_the_utc_log_day(fixture_pod):
    """The log is UTC-named; membership is judged on the pod-local bounds.

    A transition at 2026-08-31T05:00Z lands in the UTC-named 08-31 log file,
    which is outside the window's UTC date range only if you compute that
    range naively — and it is pod-local Sunday of W35, so it must count.
    """
    shared, net, homes = fixture_pod
    (shared / "signals" / "log" / "2026-08-31.jsonl").write_text(
        json.dumps({"at": "2026-08-31T05:00:00Z", "signal_id": "sig2",
                    "producer": "audit", "from_state": "firing",
                    "to_state": "resolved"}) + "\n"
        # …and one that is genuinely past the window's end (pod-local Monday).
        + json.dumps({"at": "2026-08-31T18:00:00Z", "signal_id": "sig3",
                      "producer": "audit", "from_state": "firing",
                      "to_state": "resolved"}) + "\n"
    )
    trans = _build(shared, net, homes=homes, now=NOW_MONDAY,
                   use_now=False)["signals"]["transitions"]
    assert trans["by_kind"] == {"opened": 1, "resolved": 1}
    assert trans["total"] == 2


# ── window/id arithmetic ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["2026-W00", "2026-W54", "2026-35", "W35", "", "abc"])
def test_bad_edition_ids_are_rejected(raw):
    with pytest.raises(ValueError):
        win.parse_edition_id(raw)


def test_a_53_week_year_accepts_its_53rd_week():
    assert win.weeks_in_year(2026) == 53
    assert win.parse_edition_id("2026-W53") == (2026, 53)
    assert win.weeks_in_year(2025) == 52
    with pytest.raises(ValueError):
        win.parse_edition_id("2025-W53")


def test_edition_ids_sort_chronologically_as_strings():
    ids = [win.edition_id(2026, w) for w in (1, 2, 9, 10, 35, 53)]
    assert ids == sorted(ids)
    assert ids[0] == "2026-W01"


def test_an_unknown_timezone_degrades_to_the_default_rather_than_raising():
    tz = win.resolve_timezone({"timezone": "Mars/Olympus_Mons"})
    assert str(tz) == win.DEFAULT_TZ_NAME


# ── store ────────────────────────────────────────────────────────────────────

def test_stray_files_in_the_editions_dir_are_ignored(fixture_pod):
    shared, net, homes = fixture_pod
    run_edition(shared, net, now=NOW_MIDWEEK, use_now=True)
    (store.editions_dir(shared) / "notes.json").write_text("{}")
    assert store.iter_edition_ids(shared) == ["2026-W35"]


def test_prune_keeps_the_declared_retention_window(fixture_pod):
    shared, net, homes = fixture_pod
    d = store.editions_dir(shared)
    d.mkdir(parents=True, exist_ok=True)
    for eid in ("2018-W01", "2020-W01", "2026-W35"):
        (d / f"{eid}.json").write_text(json.dumps({"edition_id": eid}))
    dropped = store.prune_editions(shared, keep_years=5)
    assert dropped == ["2018-W01", "2020-W01"]
    assert store.iter_edition_ids(shared) == ["2026-W35"]


def test_write_rejects_a_payload_without_a_valid_edition_id(tmp_path):
    with pytest.raises(ValueError):
        store.write_edition(tmp_path, {"edition_id": "nope"})


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_now_writes_the_current_open_week(fixture_pod, capsys, monkeypatch):
    shared, net, homes = fixture_pod
    monkeypatch.setattr(
        "dossier_edition.datetime",
        _FrozenDatetime(NOW_MIDWEEK),
    )
    rc = main(["--network", str(shared / "network.json"), "--now", "--report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-W35" in out and "OPEN" in out and "unsealed" in out
    written = store.load_edition(shared, "2026-W35")
    assert written["sealed"] is False
    # Pins the frozen clock, not the wall clock: without the monkeypatch this
    # assertion fails the moment the real date leaves 2026-W35, which is what
    # keeps the test from silently going vacuous.
    assert written["computed_at"] == "2026-08-27T17:00:00Z"


def test_cli_leaves_a_sealed_edition_alone_and_says_so(fixture_pod, capsys, monkeypatch):
    """A second firing for an already-recorded week is a no-op, not a failure.

    Green exit, because a weekly job that reports FAILED for "already
    measured" is noise. But not a SILENT no-op: the line names the seal and
    the flag that would override it, and the bytes on disk are unchanged.
    """
    shared, net, homes = fixture_pod
    monkeypatch.setattr("dossier_edition.datetime", _FrozenDatetime(NOW_MONDAY))
    assert main(["--network", str(shared / "network.json")]) == 0
    assert store.iter_edition_ids(shared) == ["2026-W35"]  # the completed week
    body = store.edition_path(shared, "2026-W35").read_text()
    capsys.readouterr()

    assert main(["--network", str(shared / "network.json")]) == 0
    out = capsys.readouterr().out
    assert "already recorded, not rewritten" in out and "--force" in out
    assert store.edition_path(shared, "2026-W35").read_text() == body


def test_the_store_api_still_raises_on_a_sealed_overwrite(fixture_pod):
    """The CLI's soft landing must not become a soft store contract."""
    shared, net, homes = fixture_pod
    run_edition(shared, net, now=NOW_MONDAY)
    with pytest.raises(store.SealedEditionError):
        store.write_edition(shared, store.load_edition(shared, "2026-W35"))


def test_cli_dry_run_writes_nothing(fixture_pod, monkeypatch):
    shared, net, homes = fixture_pod
    monkeypatch.setattr("dossier_edition.datetime", _FrozenDatetime(NOW_MIDWEEK))
    assert main(["--network", str(shared / "network.json"), "--now",
                 "--dry-run"]) == 0
    assert store.iter_edition_ids(shared) == []


def test_cli_rejects_a_malformed_week(fixture_pod, capsys):
    shared, net, homes = fixture_pod
    assert main(["--network", str(shared / "network.json"), "--week", "2026-W99"]) == 2
    assert "ISO weeks" in capsys.readouterr().err


class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now()`` is pinned — the CLI reads the wall clock."""

    _pinned: datetime

    def __new__(cls, pinned: datetime):  # type: ignore[override]
        obj = super().__new__(
            cls, pinned.year, pinned.month, pinned.day,
            pinned.hour, pinned.minute, tzinfo=pinned.tzinfo,
        )
        obj._pinned = pinned
        return obj

    def now(self, tz=None):  # type: ignore[override]
        return self._pinned if tz is None else self._pinned.astimezone(tz)
