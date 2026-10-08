"""The Project Manager application on the Tracker (D-TM9/11/13).

Fixture: one channel week (structured form-bot reports, a loose request, an
un-addressed message, an assignment by mention, a ✅) plus the old app's
tasks.json, all placeholder-named (docs/PLACEHOLDER_NAMING.md). The weekly
report is byte-compared against ``fixtures/project_manager/weekly_post.txt``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from evolve_admin import app_contract as ac
from evolve_admin import board_import as bi
from evolve_admin import board_project as pm
from evolve_admin import board_store as bs

FX = Path(__file__).parent / "fixtures" / "project_manager"
BOT = "team-bot-a"
NOW = datetime(2026, 9, 25, 19, 0, tzinfo=timezone.utc)  # Friday 12:00 PDT
TZ = ZoneInfo("America/Los_Angeles")
WINDOW_START = NOW - timedelta(days=7)
PROJECT = {"owner": "lead", "channel": "CMAINT", "report_bot": "UFORMBOT",
           "bot_user": "UTEAMBOT", "report_weekday": 4, "report_hour": 12,
           "report_title": "Weekly Maintenance Report", "area_label": "room"}
MEMBERS = [{"id": "member-a", "display": "Member A", "slack_user": "UMEMBERA"},
           {"id": "member-b", "display": "Member B", "slack_user": "UMEMBERB"},
           {"id": "lead", "display": "Lead", "slack_user": "ULEAD"}]


def _list(shared: Path) -> str:
    return bs.create_list(shared, BOT, name="Maintenance", shape="project", id_prefix="MN",
                          members=MEMBERS, defaults={"nag_days": 3, "project": PROJECT},
                          actor="test")["list_id"]


def _messages() -> list[dict]:
    return json.loads((FX / "channel_week.json").read_text(encoding="utf-8"))["messages"]


def _tasks() -> dict:
    return json.loads((FX / "tasks.json").read_text(encoding="utf-8"))


def _week(shared: Path) -> str:
    lid = _list(shared)
    bi.from_task_manager(shared, BOT, _tasks(), list_id=lid, apply=True, rename=False,
                         created_before=WINDOW_START)
    pm.ingest(shared, BOT, lid, _messages(), now=NOW)
    return lid


def _by_id(shared: Path, card_id: str) -> dict:
    card = bs.find_card(bs.load_board(shared, BOT), card_id)
    assert card is not None
    return card


def _card(shared: Path, hid: str) -> dict:
    return next(c for c in bs.load_board(shared, BOT)["cards"] if c.get("human_id") == hid)


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, str]] = []

    def fake(bot_id, network, channel, target, message):
        out.append((target, message))
        return True, None
    monkeypatch.setattr(ac, "send_to_channel", fake)
    return out


# ── ingest ─────────────────────────────────────────────────────────────────


def test_fixture_week_becomes_cards(tmp_path):
    lid = _week(tmp_path)
    cards = {c["human_id"]: c for c in bs.load_board(tmp_path, BOT)["cards"]
             if c.get("list_id") == lid}
    assert sorted(cards) == ["MN-0101", "MN-0102", "MN-0104", "MN-0105",
                             "MN-0106", "MN-0107", "MN-0108"]
    door = cards["MN-0104"]  # ✅ already on the report: resolved on arrival, logged
    assert door["lane"] == "done" and "resolved on arrival" in door["note"]
    panel = cards["MN-0105"]
    assert (panel["area"], panel["severity"], panel.get("reporter")) == ("North", 3, None)
    assert panel["source_id"].startswith("slack:CMAINT:")
    assert panel["created_at"] == "2026-09-20T21:47:00Z"
    assert panel["assignee"] == "member-b"  # "assign MN-0105 <@UMEMBERB>"
    assert panel["history"][-1] == {"at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "what": "assigned member-b", "actor": "slack:UMEMBERA"}
    assert cards["MN-0108"]["title"] == "replace the annex doormat"
    assert not any("fuses" in c["title"] for c in cards.values())  # not my lane


def test_ingest_is_idempotent_by_ts_and_a_later_check_resolves(tmp_path):
    lid = _list(tmp_path)
    msgs = _messages()
    pm.ingest(tmp_path, BOT, lid, msgs, now=NOW)
    pm.ingest(tmp_path, BOT, lid, msgs, now=NOW)
    cards = [c for c in bs.load_board(tmp_path, BOT)["cards"] if c.get("list_id") == lid]
    assert len(cards) == 5
    panel = next(c for c in cards if c["title"].startswith("Panel"))
    assert panel["lane"] == "inbox"
    msgs[1]["reactions"] = [{"name": "white_check_mark", "users": ["ULEAD"], "count": 1}]
    pm.ingest(tmp_path, BOT, lid, msgs, now=NOW)
    panel = _by_id(tmp_path, panel["id"])
    assert panel["lane"] == "done"
    assert panel["history"][-1]["actor"] == "slack:ULEAD"


def test_commands_answer_and_record_the_slack_actor(tmp_path):
    lid = _list(tmp_path)
    pm.ingest(tmp_path, BOT, lid, _messages()[:2], now=NOW)
    ts = f"{NOW.timestamp() - 60:.6f}"

    def say(text, n):
        return pm.ingest(tmp_path, BOT, lid, [{"ts": f"{float(ts) + n:.6f}", "user": "ULEAD",
                                                "text": f"<@UTEAMBOT> {text}"}], now=NOW)
    assert say("status MN-0002 blocked", 1)[0]["reply"] == "MN-0002 is blocked."
    assert say("status", 2)[0]["reply"] == "Maintenance: 1 open, 1 blocked."
    assert say("list", 3)[0]["reply"] == "• MN-0002 — Panel flickers during the finale"
    assert say("assign MN-0002 <@UNOBODY>", 4)[0]["reply"].startswith("Who should take MN-0002?")
    assert say("resolve MN-0009", 5)[0]["reply"] == "I can't find MN-0009 on Maintenance."
    assert say("resolve MN-0002", 6)[0]["reply"] == "MN-0002 resolved."
    shown = say("show MN-0002", 7)[0]["reply"]
    assert shown.splitlines()[1].startswith("Status: resolved")
    assert "status blocked (slack:ULEAD)" in shown and "moved to done (slack:ULEAD)" in shown


def _assign_bot(minutes_before: int) -> dict:
    return {"ts": f"{NOW.timestamp() - minutes_before * 60:.6f}", "user": "ULEAD",
            "text": "<@UTEAMBOT> assign MN-0106 bot"}


def _bot_threads(shared: Path) -> list[dict]:
    return [c for c in bs.load_board(shared, BOT)["cards"]
            if c.get("owner") == "bot" and c.get("source") == "handoff"]


def test_a_second_tick_over_the_same_history_posts_and_writes_nothing(tmp_path, sent, monkeypatch):
    # The review's measurement (pr-4535 finding 1): tick hourly over one
    # channel window. The first run answers the two commands; every later
    # run re-reads the same messages and must neither answer them again nor
    # touch the store — no repeated "assigned member-b" row, no second thread.
    lid = _list(tmp_path)
    bi.from_task_manager(tmp_path, BOT, _tasks(), list_id=lid, apply=True, rename=False,
                         created_before=WINDOW_START)
    history = _messages() + [_assign_bot(30)]
    monkeypatch.setattr(ac, "read_history", lambda *a: history)
    first = pm.tick(tmp_path, BOT, lid, {}, now=NOW, tz=TZ)
    assert first["replies"] == 3
    assert "MN-0106 is mine — I'll follow it through." in [m for _, m in sent]
    before = json.dumps(bs.load_board(tmp_path, BOT), sort_keys=True)
    del sent[:]
    for hours in (1, 2):
        again = pm.tick(tmp_path, BOT, lid, {}, now=NOW + timedelta(hours=hours), tz=TZ)
        assert again == {"replies": 0, "nags": [], "report_posted": False}
    assert sent == []
    assert json.dumps(bs.load_board(tmp_path, BOT), sort_keys=True) == before
    assert len(_bot_threads(tmp_path)) == 1
    assert [h["what"] for h in _card(tmp_path, "MN-0105")["history"]].count("assigned member-b") == 1


def test_a_check_behind_the_cursor_still_resolves(tmp_path):
    lid = _list(tmp_path)
    msgs = _messages()
    pm.ingest(tmp_path, BOT, lid, msgs, now=NOW)
    assert bs.load_board(tmp_path, BOT)["lists"][-1]["cursors"]["ingested_through"] == max(
        m["ts"] for m in msgs)
    msgs[1]["reactions"] = [{"name": "white_check_mark", "users": ["ULEAD"], "count": 1}]
    assert pm.ingest(tmp_path, BOT, lid, msgs, now=NOW + timedelta(hours=1)) == []
    assert next(c for c in bs.load_board(tmp_path, BOT)["cards"]
                if c["title"].startswith("Panel"))["lane"] == "done"


def test_assign_to_bot_seen_twice_mints_one_thread(tmp_path):
    # Two separate messages, both past the cursor: the cursor cannot help
    # here, so handoff_to_bot itself must hand back the thread it made.
    lid = _week(tmp_path)
    first = pm.ingest(tmp_path, BOT, lid, [_assign_bot(30)], now=NOW)
    second = pm.ingest(tmp_path, BOT, lid, [_assign_bot(20)], now=NOW)
    assert first[0]["reply"] == second[0]["reply"] == "MN-0106 is mine — I'll follow it through."
    threads = _bot_threads(tmp_path)
    assert len(threads) == 1
    assert _card(tmp_path, "MN-0106")["linked_card"] == threads[0]["id"]
    card_id = _card(tmp_path, "MN-0106")["id"]
    assert pm.handoff_to_bot(tmp_path, BOT, card_id, actor="test")["id"] == threads[0]["id"]
    assert len(_bot_threads(tmp_path)) == 1


# ── the weekly report ──────────────────────────────────────────────────────


def test_weekly_report_equals_the_fixture_post_byte_for_byte(tmp_path):
    lid = _week(tmp_path)
    got = pm.render_weekly_report(bs.load_board(tmp_path, BOT), lid, now=NOW, tz=TZ)
    assert got == (FX / "weekly_post.txt").read_text(encoding="utf-8")


def test_quiet_week_says_no_hot_spots(tmp_path):
    lid = _list(tmp_path)
    got = pm.render_weekly_report(bs.load_board(tmp_path, BOT), lid, now=NOW, tz=TZ)
    assert got.endswith(":white_check_mark: *No hot spots* detected "
                        "(3+ reports in past 7 days).")
    assert "_Total issues reported:_ 0" in got


def test_report_posts_on_the_list_schedule_only(tmp_path, sent, monkeypatch):
    lid = _list(tmp_path)
    monkeypatch.setattr(ac, "read_history", lambda *a: [])
    thursday = NOW - timedelta(days=1)
    assert pm.tick(tmp_path, BOT, lid, {}, now=thursday, tz=TZ)["report_posted"] is False
    assert pm.tick(tmp_path, BOT, lid, {}, now=NOW - timedelta(minutes=1), tz=TZ)["report_posted"] is False
    assert pm.tick(tmp_path, BOT, lid, {}, now=NOW, tz=TZ)["report_posted"] is True
    assert pm.tick(tmp_path, BOT, lid, {}, now=NOW + timedelta(hours=1), tz=TZ)["report_posted"] is False
    assert [m for _, m in sent if "Weekly Maintenance Report" in m]


# ── follow-through ─────────────────────────────────────────────────────────


def test_assignee_nag_fires_as_a_remind_touch_to_the_right_member(tmp_path, sent):
    lid = _week(tmp_path)
    fired = pm.send_nags(tmp_path, BOT, lid, {}, now=NOW)
    keypad = _card(tmp_path, "MN-0102")  # blocked, member-a, past its expires-date due
    assert {"card": "MN-0102", "to": "member-a"} in fired
    assert any(m.startswith("<@UMEMBERA> MN-0102 — Keypad above the exit is dead: blocked")
               for _, m in sent)
    assert keypad["touches"][-1]["action"] == "remind"
    assert keypad["touches"][-1]["result"].startswith("nag:member-a:blocked")
    assert "next_touch" not in keypad  # the list's nag_days, not the personal pace table
    assert all(t == "CMAINT" for t, _ in sent)
    sent.clear()
    def keypad_nagged(days):
        fired = pm.send_nags(tmp_path, BOT, lid, {}, now=NOW + timedelta(days=days))
        return any(f["card"] == "MN-0102" for f in fired)
    assert not keypad_nagged(1)  # nagged within nag_days: quiet
    assert keypad_nagged(4)


def test_unassigned_nag_goes_to_the_owner(tmp_path, sent):
    lid = _week(tmp_path)
    fired = pm.send_nags(tmp_path, BOT, lid, {}, now=NOW)
    assert {"card": "MN-0101", "to": "lead"} in fired  # 96 days, nobody on it
    assert any(m.startswith("<@ULEAD> MN-0101") and "unassigned 96 days" in m for _, m in sent)
    assert not any(f["card"] == "MN-0106" for f in fired)  # 3 days is not past nag_days


def test_bot_assigned_items_are_not_slack_nagged(tmp_path, sent):
    lid = _week(tmp_path)
    pm.handoff_to_bot(tmp_path, BOT, _card(tmp_path, "MN-0101")["id"], actor="test")
    assert not any(f["card"] == "MN-0101" for f in pm.send_nags(tmp_path, BOT, lid, {}, now=NOW))


# ── parity ─────────────────────────────────────────────────────────────────


def _old_post() -> dict:
    return {"ts": f"{NOW.timestamp():.6f}", "user": "UTEAMBOT",
            "text": (FX / "weekly_post.txt").read_text(encoding="utf-8")}


def test_parity_diff_is_empty_on_the_fixture(tmp_path):
    lid = _list(tmp_path)
    old, new, diff = bi.parity_diff(bs.load_board(tmp_path, BOT), BOT, lid,
                                    _messages() + [_old_post()], tasks=_tasks(), tz=TZ)
    assert diff == "" and old == new
    assert [c for c in bs.load_board(tmp_path, BOT)["cards"]] == []  # live store untouched


def test_parity_diff_is_non_empty_when_one_report_is_dropped(tmp_path):
    lid = _list(tmp_path)
    msgs = [m for m in _messages() if "Table leg" not in m["text"]] + [_old_post()]
    _, _, diff = bi.parity_diff(bs.load_board(tmp_path, BOT), BOT, lid, msgs,
                                tasks=_tasks(), tz=TZ)
    assert "-_Total issues reported:_ 5" in diff and "+_Total issues reported:_ 4" in diff
    assert "-• Sev 1 — Table leg is coming loose — North" in diff


def test_parity_refuses_without_an_old_report(tmp_path):
    lid = _list(tmp_path)
    with pytest.raises(ValueError, match="no weekly report"):
        bi.parity_diff(bs.load_board(tmp_path, BOT), BOT, lid, _messages())


# ── migration ──────────────────────────────────────────────────────────────


def test_import_preserves_ids_maps_fields_and_is_idempotent(tmp_path):
    lid = _list(tmp_path)
    src = tmp_path / "ws" / "tasks.json"
    src.parent.mkdir()
    src.write_text(json.dumps(_tasks()), encoding="utf-8")
    dry = bi.from_task_manager(tmp_path, BOT, src, list_id=lid)
    assert [r["action"] for r in dry] == ["create", "create", "skip: complete",
                                          "skip: prefix OP belongs to another list"]
    assert bs.load_board(tmp_path, BOT)["cards"] == [] and src.exists()  # dry run
    bi.from_task_manager(tmp_path, BOT, src, list_id=lid, apply=True, today="2026-09-26")
    assert not src.exists() and (src.parent / "tasks.json.imported-2026-09-26").exists()
    keypad = _card(tmp_path, "MN-0102")
    assert (keypad["due"], keypad["assignee"], keypad["pm_status"], keypad["severity"]) == (
        "2026-09-10", "member-a", "blocked", 3)
    assert keypad["created_at"] == "2026-09-01T16:00:00Z"
    again = bi.from_task_manager(tmp_path, BOT, _tasks(), list_id=lid, apply=True)
    assert [r["action"] for r in again][:2] == ["skip: already imported"] * 2
    assert len(bs.load_board(tmp_path, BOT)["cards"]) == 2
    # the next minted id never reuses an old number, closed ones included
    new = bs.create_card(tmp_path, BOT, title="New", cluster="admin", list_id=lid)
    assert new["human_id"] == "MN-0104"


# ── handoff (D-TM13) ───────────────────────────────────────────────────────


def test_handoff_assistant_to_member_and_closing_either_closes_both(tmp_path):
    lid = _list(tmp_path)
    mine = bs.create_card(tmp_path, BOT, title="Get the boiler serviced", cluster="admin",
                          outcome="boiler serviced and signed off")
    item = pm.handoff_to_member(tmp_path, BOT, mine["id"], lid, "member-b", actor="user")
    assert item["human_id"] == "MN-0001" and item["assignee"] == "member-b"
    assert item["outcome"] == "boiler serviced and signed off"
    bs.move_card(tmp_path, BOT, item["id"], "done", actor="slack:UMEMBERB")
    assert _by_id(tmp_path, mine["id"])["lane"] == "done"


def test_handoff_project_to_bot_and_closing_either_closes_both(tmp_path):
    lid = _list(tmp_path)
    item = bs.create_card(tmp_path, BOT, title="Order spare fuses", cluster="admin", list_id=lid)
    reply = pm.ingest(tmp_path, BOT, lid, [{"ts": f"{NOW.timestamp() - 5:.6f}", "user": "ULEAD",
                                            "text": "<@UTEAMBOT> assign MN-0001 bot"}], now=NOW)
    assert reply[0]["reply"] == "MN-0001 is mine — I'll follow it through."
    item = _by_id(tmp_path, item["id"])
    thread = _by_id(tmp_path, item["linked_card"])
    assert item["assignee"] == "bot"
    assert (thread["list_id"], thread["owner"], thread["outcome"]) == (
        "personal", "bot", "MN-0001 resolved")
    bs.move_card(tmp_path, BOT, thread["id"], "done", actor=BOT)
    assert _by_id(tmp_path, item["id"])["lane"] == "done"


# ── the operator CLI, through the real `main` group ────────────────────────


def _cli(tmp_path, monkeypatch, *args):
    from click.testing import CliRunner

    from evolve_admin.cli import main
    net = {"sharedDir": str(tmp_path), "timezone": "America/Los_Angeles"}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(net), encoding="utf-8")
    monkeypatch.setattr("evolve_admin.config.load_network", lambda path=None: net)
    return CliRunner().invoke(main, ["--network", str(network_path), "project", *args])


def test_cli_create_list_import_parity_handoff_and_tick(tmp_path, monkeypatch, sent):
    r = _cli(tmp_path, monkeypatch, "create-list", "--bot", BOT, "--name", "Maintenance",
             "--prefix", "MN", "--members", json.dumps(MEMBERS), "--project", json.dumps(PROJECT))
    assert r.exit_code == 0, r.output
    lid = "maintenance"
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"messages": _messages() + [_old_post()]}), encoding="utf-8")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps(_tasks()), encoding="utf-8")
    r = _cli(tmp_path, monkeypatch, "parity", "--bot", BOT, "--list", lid,
             "--history", str(history), "--tasks-json", str(tasks))
    assert (r.exit_code, r.output.strip()) == (0, "parity: no difference")
    r = _cli(tmp_path, monkeypatch, "import", "--bot", BOT, "--list", lid,
             "--tasks-json", str(tasks))
    assert "2 to create (dry run" in r.output and tasks.exists()
    r = _cli(tmp_path, monkeypatch, "import", "--bot", BOT, "--list", lid,
             "--tasks-json", str(tasks), "--apply")
    assert r.exit_code == 0 and not tasks.exists()
    mine = bs.create_card(tmp_path, BOT, title="Call the plumber", cluster="admin")
    r = _cli(tmp_path, monkeypatch, "handoff", "--bot", BOT, "--card", mine["id"][:8],
             "--list", lid, "--member", "member-a")
    assert r.exit_code == 0 and r.output.startswith("MN-0104 assigned to member-a")
    monkeypatch.setattr(ac, "read_history", lambda *a: _messages())
    r = _cli(tmp_path, monkeypatch, "tick", "--bot", BOT, "--list", lid)
    assert r.exit_code == 0 and json.loads(r.output)["replies"] == 2
