"""Discovery reads OpenClaw's own cron store, not just ``crontab -l``.

ALPHA journey audit F1 (``internal/audit-alpha-journey-2026-08.md`` §4.1):
``docs/help/apps.md`` tells the operator that a cron is the strongest evidence
a workspace can carry, and the scanner's only cron source was the user
crontab. On a pod whose operator schedules work through OpenClaw — which is
where a stranger's schedules actually live — the crontab is empty, so the
inventory reported ``cron_jobs=0`` for a bot with a dozen live jobs and the
lead wow undercounted the pod's real habits.

What is pinned here:

* both backends of the store are read — ``cron/jobs.json`` on a pre-2026.7
  pod, the ``cron_jobs`` SQLite table once the gateway has ingested the seed;
* every OpenClaw payload kind yields evidence (``command`` argv, ``agentTurn``
  message, ``systemEvent`` text), including the ones with no script behind
  them at all;
* a job that cannot fire is not evidence, and infrastructure stays filtered;
* crontab entries are unchanged, and an absent/unreadable store is a silent
  skip with a log line — never a failed scan and never "this bot has none";
* the fixture pod's own OC stores are read by the product's reader.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import cron_manager as _cron_manager  # noqa: E402
from evolve_admin.applications import scanner as _scanner  # noqa: E402

_REPO_ROOT = _ADMIN_DIR.parent.parent


# ── Fixtures ────────────────────────────────────────────────────────────────


def _job(
    job_id: str,
    name: str,
    payload: dict,
    *,
    expr: str = "0 9 * * *",
    tz: str | None = "America/Los_Angeles",
    enabled: bool = True,
) -> dict:
    schedule: dict = {"kind": "cron", "expr": expr}
    if tz:
        schedule["tz"] = tz
    return {
        "id": job_id,
        "name": name,
        "enabled": enabled,
        "createdAtMs": 1_750_000_000_000,
        "schedule": schedule,
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": payload,
        "state": {},
    }


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """A bot home whose OpenClaw store the readers resolve to.

    ``cron_manager`` is the one module that resolves the store's location, so
    pinning its ``_bot_home`` is enough for both it and the scanner.
    """
    home = tmp_path / "homes" / "personal-bot"
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(_cron_manager, "_bot_home", lambda bot_id: home)
    # No crontab in a unit test — the point of every case below is what the
    # OTHER source contributes. The merge itself is pinned separately.
    monkeypatch.setattr(_scanner, "_collect_crontab_crons", lambda bot_id: [])
    return home


def _write_jobs_json(home: Path, payload: object) -> Path:
    path = home / ".openclaw" / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _write_sqlite_store(home: Path, jobs: list[dict]) -> Path:
    db = home / ".openclaw" / "state" / "openclaw.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE cron_jobs (store_key TEXT, job_id TEXT, name TEXT, "
        "job_json TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0)"
    )
    for order, job in enumerate(jobs):
        conn.execute(
            "INSERT INTO cron_jobs (store_key, job_id, name, job_json, sort_order) "
            "VALUES (?,?,?,?,?)",
            ("store", job["id"], job["name"], json.dumps(job), order),
        )
    conn.commit()
    conn.close()
    return db


def _ws(home: Path) -> Path:
    return home / ".openclaw" / "workspace"


# ── read_oc_cron_jobs — the store reader ────────────────────────────────────


def test_reads_jobs_json_wrapped_shape(bot):
    _write_jobs_json(bot, {"jobs": [_job("a", "morning-brief", {"kind": "agentTurn", "message": "hi"})]})
    jobs = _cron_manager.read_oc_cron_jobs("personal-bot")
    assert [j["name"] for j in jobs] == ["morning-brief"]


def test_reads_jobs_json_bare_list_shape(bot):
    """Forge's own merge writes a bare list; OpenClaw writes {"jobs": [...]}."""
    _write_jobs_json(bot, [_job("a", "release-notes", {"kind": "agentTurn", "message": "hi"})])
    jobs = _cron_manager.read_oc_cron_jobs("personal-bot")
    assert [j["name"] for j in jobs] == ["release-notes"]


def test_falls_back_to_sqlite_when_jobs_json_is_gone(bot):
    """The ≥2026.7 shape: the seed was ingested and renamed, table is truth."""
    _write_sqlite_store(bot, [
        _job("a", "release-notes", {"kind": "agentTurn", "message": "hi"}),
        _job("b", "repo-watch", {"kind": "agentTurn", "message": "hi"}),
    ])
    (bot / ".openclaw" / "cron").mkdir(parents=True, exist_ok=True)
    (bot / ".openclaw" / "cron" / "jobs.json.migrated").write_text("{}")
    jobs = _cron_manager.read_oc_cron_jobs("personal-bot")
    assert [j["name"] for j in jobs] == ["release-notes", "repo-watch"]


def test_jobs_json_wins_over_sqlite_when_both_are_present(bot):
    """A seed the gateway has not ingested yet holds jobs the table lacks."""
    _write_jobs_json(bot, {"jobs": [_job("a", "from-file", {"kind": "agentTurn", "message": "hi"})]})
    _write_sqlite_store(bot, [_job("b", "from-table", {"kind": "agentTurn", "message": "hi"})])
    jobs = _cron_manager.read_oc_cron_jobs("personal-bot")
    assert [j["name"] for j in jobs] == ["from-file"]


def test_no_store_at_all_is_none_not_empty(bot):
    """None ("could not see it") must stay distinct from [] ("saw it; empty")."""
    assert _cron_manager.read_oc_cron_jobs("personal-bot") is None


def test_empty_jobs_json_reads_as_empty_not_none(bot):
    _write_jobs_json(bot, {"jobs": []})
    assert _cron_manager.read_oc_cron_jobs("personal-bot") == []


def test_corrupt_sqlite_is_none_not_a_raise(bot):
    db = bot / ".openclaw" / "state" / "openclaw.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a database")
    assert _cron_manager.read_oc_cron_jobs("personal-bot") is None


# ── Evidence shape ──────────────────────────────────────────────────────────


def test_command_payload_yields_the_same_evidence_shape_as_a_crontab_line(bot):
    script = _ws(bot) / "morning-brief" / "brief.py"
    script.parent.mkdir(parents=True)
    script.write_text("# assemble the morning brief\nprint('brief')\n")
    _write_jobs_json(bot, {"jobs": [
        _job("a", "morning-brief",
             {"kind": "command", "argv": ["python3", str(script)]},
             expr="45 6 * * *"),
    ]})

    (entry,) = _scanner._collect_crons("personal-bot", _ws(bot))
    assert set(entry) >= {"schedule", "script_path", "script_content", "is_infrastructure"}
    assert entry["schedule"] == "45 6 * * * (America/Los_Angeles)"
    assert entry["script_path"] == str(script)
    assert "assemble the morning brief" in entry["script_content"]
    assert entry["is_infrastructure"] is False
    assert entry["source"] == "openclaw"
    assert entry["job_name"] == "morning-brief"


def test_agent_turn_payload_mines_the_script_named_in_its_message(bot):
    script = _ws(bot) / "oncall" / "rota.py"
    script.parent.mkdir(parents=True)
    script.write_text("# who is on call this week\n")
    _write_jobs_json(bot, {"jobs": [
        _job("a", "oncall-roll", {
            "kind": "agentTurn",
            "message": f"Run python3 {script} and post who is on call.",
        }),
    ]})

    (entry,) = _scanner._collect_crons("personal-bot", _ws(bot))
    assert entry["script_path"] == str(script)
    assert "who is on call this week" in entry["script_content"]


def test_payload_with_no_script_still_counts_as_schedule_evidence(bot):
    """An agent-turn job IS a habit; the instruction text is the evidence."""
    _write_jobs_json(bot, {"jobs": [
        _job("a", "photo-digest", {
            "kind": "agentTurn",
            "message": "Send me the week's best photos from the shared album.",
        }, expr="0 18 * * 0"),
    ]})

    (entry,) = _scanner._collect_crons("personal-bot", _ws(bot))
    assert entry["script_path"] == "openclaw:cron/photo-digest"
    assert "best photos" in entry["script_content"]
    assert entry["is_infrastructure"] is False


def test_system_event_payload_text_is_read(bot):
    _write_jobs_json(bot, {"jobs": [
        _job("a", "album-sweep", {"kind": "systemEvent", "text": "Sweep the album."}),
    ]})
    (entry,) = _scanner._collect_crons("personal-bot", _ws(bot))
    assert "Sweep the album." in entry["script_content"]


def test_unknown_payload_kind_still_yields_evidence(bot):
    """OpenClaw's kind vocabulary changed once already — do not switch on it."""
    _write_jobs_json(bot, {"jobs": [
        _job("a", "future-kind", {"kind": "somethingNew", "message": "Do the thing."}),
    ]})
    (entry,) = _scanner._collect_crons("personal-bot", _ws(bot))
    assert "Do the thing." in entry["script_content"]


def test_schedule_kinds_other_than_cron_render(bot):
    _write_jobs_json(bot, {"jobs": [
        {"id": "a", "name": "every-six-hours", "enabled": True,
         "schedule": {"kind": "every", "everyMs": 21_600_000},
         "payload": {"kind": "agentTurn", "message": "Check the queue."}},
    ]})
    (entry,) = _scanner._collect_crons("personal-bot", _ws(bot))
    assert entry["schedule"] == "every 6h"


def test_cron_entries_carry_the_openclaw_job_name_as_label(bot, monkeypatch):
    script = _ws(bot) / "standup" / "digest.py"
    script.parent.mkdir(parents=True)
    script.write_text("# digest\n")
    _write_jobs_json(bot, {"jobs": [
        _job("a", "standup-digest", {"kind": "command", "argv": ["python3", str(script)]}),
    ]})
    monkeypatch.setattr(_scanner, "bot_home", lambda bot_id, network=None: bot)

    inv = _scanner.collect_inventory(_ws(bot), "personal-bot")
    assert [(e["label"], e["script"]) for e in inv.cron_entries] == [
        ("standup-digest", str(script))
    ]


# ── What must NOT become evidence ───────────────────────────────────────────


def test_disabled_jobs_are_not_evidence(bot):
    """``enabled: false`` (OpenClaw) and ``disabled: true`` (Evolve's pause
    path) are both the store's version of a commented-out crontab line."""
    job_paused = _job("b", "paused-by-evolve", {"kind": "agentTurn", "message": "x"})
    job_paused["disabled"] = True
    _write_jobs_json(bot, {"jobs": [
        _job("a", "switched-off", {"kind": "agentTurn", "message": "x"}, enabled=False),
        job_paused,
        _job("c", "live-one", {"kind": "agentTurn", "message": "Send the digest."}),
    ]})

    entries = _scanner._collect_crons("personal-bot", _ws(bot))
    assert [e["job_name"] for e in entries] == ["live-one"]


def test_infrastructure_is_filtered_by_name_and_by_content(bot):
    probe = _ws(bot) / "bin" / "sentry_ping.sh"
    probe.parent.mkdir(parents=True)
    probe.write_text("#!/bin/sh\ncurl -sf http://127.0.0.1:19001/health\n")
    _write_jobs_json(bot, {"jobs": [
        _job("a", "liveness-ping", {"kind": "command", "argv": ["bash", str(probe)]}),
        _job("b", "gateway-watch", {
            "kind": "systemEvent",
            "text": "Probe the openclaw gateway and restart it if it is down.",
        }),
        _job("c", "release-notes", {
            "kind": "agentTurn", "message": "Draft this week's release notes.",
        }),
    ]})

    entries = _scanner._collect_crons("personal-bot", _ws(bot))
    assert {e["job_name"]: e["is_infrastructure"] for e in entries} == {
        "liveness-ping": True,
        "gateway-watch": True,
        "release-notes": False,
    }


# ── Merging with the crontab, and failing quietly ───────────────────────────


def test_crontab_entries_are_unaffected(tmp_path, monkeypatch):
    """The pre-existing source keeps its exact behaviour and ordering."""
    home = tmp_path / "homes" / "personal-bot"
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(_cron_manager, "_bot_home", lambda bot_id: home)
    crontab_entry = {
        "schedule": "0 7 * * *", "script_path": "/opt/x/hand_written.py",
        "script_content": "# by hand", "is_infrastructure": False,
        "source": "crontab", "job_name": "",
    }
    monkeypatch.setattr(_scanner, "_collect_crontab_crons", lambda bot_id: [crontab_entry])

    # No OpenClaw store at all — the crontab is still the whole answer.
    assert _scanner._collect_crons("personal-bot", home) == [crontab_entry]


def test_a_job_in_both_surfaces_counts_once(bot, monkeypatch):
    script = _ws(bot) / "standup" / "digest.py"
    script.parent.mkdir(parents=True)
    script.write_text("# digest\n")
    monkeypatch.setattr(_scanner, "_collect_crontab_crons", lambda bot_id: [{
        "schedule": "45 9 * * 1-5", "script_path": str(script),
        "script_content": "# digest", "is_infrastructure": False,
        "source": "crontab", "job_name": "",
    }])
    _write_jobs_json(bot, {"jobs": [
        _job("a", "standup-digest", {"kind": "command", "argv": ["python3", str(script)]}),
    ]})

    entries = _scanner._collect_crons("personal-bot", _ws(bot))
    assert [e["source"] for e in entries] == ["crontab"]


def test_unreadable_store_is_a_silent_skip_with_a_log_line(bot, capsys):
    """A bot whose store cannot be read contributes nothing and never fails."""
    assert _scanner._collect_crons("personal-bot", _ws(bot)) == []
    out = capsys.readouterr().out
    assert "OpenClaw cron store absent/unreadable for personal-bot" in out


def test_a_raising_store_reader_does_not_kill_the_scan(bot, monkeypatch, capsys):
    def _boom(bot_id):
        raise PermissionError("ACL clamp")

    monkeypatch.setattr(_scanner, "read_oc_cron_jobs", _boom)
    assert _scanner._collect_crons("personal-bot", _ws(bot)) == []
    assert "OpenClaw cron store unreadable for personal-bot" in capsys.readouterr().out


# ── The fixture pod ─────────────────────────────────────────────────────────


def _load_fixture_build():
    """Import ``tests/fixtures/pod/build.py`` by path.

    ``packages/admin/tests`` shadows the repo-root ``tests`` package on this
    suite's sys.path, so the module cannot be imported by dotted name here.
    """
    path = _REPO_ROOT / "tests" / "fixtures" / "pod" / "build.py"
    spec = importlib.util.spec_from_file_location("_fixture_pod_build", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_pod(tmp_path, monkeypatch):
    """The real fixture pod, with bot homes resolved by the product's own
    profile seam (the same pin ``tests/fixtures/pod/sitecustomize`` uses)."""
    import platform_profile

    build = _load_fixture_build()
    root = tmp_path / "pod"
    summary = build.build(root)
    platform_profile.set_profile(
        replace(
            platform_profile.get_profile(),
            user_home_root=str(root / "homes"),
            shared_dir_default=str(root / "shared"),
            scratch_dir=str(root / "scratch"),
        )
    )
    # There is no crontab behind these homes (no accounts, no sudo) — which is
    # exactly the stranger's-pod case F1 is about.
    monkeypatch.setattr(_scanner, "_collect_crontab_crons", lambda bot_id: [])
    return summary


def test_fixture_pod_schedules_are_visible_to_the_scan(fixture_pod):
    """Before F1's fix this was ``cron_jobs=0`` on all three bots."""
    counts = {}
    for bot_id in fixture_pod["bots"]:
        workspace = Path(fixture_pod["homes"]) / bot_id / ".openclaw" / "workspace"
        entries = _scanner._collect_crons(bot_id, workspace)
        counts[bot_id] = (
            len([e for e in entries if not e["is_infrastructure"]]),
            len([e for e in entries if e["is_infrastructure"]]),
        )

    # (app-crons, infra-crons) per bot. The personal bot's fourth job is
    # switched off, so it is not here; the two infra jobs are the ones the
    # filter is supposed to catch.
    assert counts == {
        "personal-bot": (3, 0),
        "team-bot-a": (2, 1),
        "admin-bot": (2, 1),
    }


def test_fixture_pod_covers_both_store_backends(fixture_pod):
    """One bot on the file, one on SQLite — both readable by the product."""
    homes = Path(fixture_pod["homes"])
    assert (homes / "personal-bot" / ".openclaw" / "cron" / "jobs.json").exists()

    ops = homes / "admin-bot" / ".openclaw"
    assert not (ops / "cron" / "jobs.json").exists(), "the seed was ingested"
    assert (ops / "cron" / "jobs.json.migrated").exists()
    assert (ops / "state" / "openclaw.sqlite").exists()

    names = [j["name"] for j in _cron_manager.read_oc_cron_jobs("admin-bot")]
    assert names == ["release-notes", "repo-watch", "gateway-watch"]
