"""Tests for the pod-first Apps reads (AL-1.8a, docs/build-AL-1.8a-apps-shell.md §4).

    GET /api/apps                — one row per app_id, across bots
    GET /api/apps/<app_id>       — bots × facts + signals
    GET /api/apps/discovered     — pod-wide drafts
    GET /api/apps/activity       — authoring / promotion / publish feed

The fixture pod is the brief's: two bots, ONE app installed on both (so the
grouping-by-``app_id`` claim is actually exercised rather than asserted on a
single row), one bot-local app, and one discovered draft.

What these pin is mostly the HONESTY contract, because that is the part a
future edit can quietly break without any test noticing:

  * a bot with no usage rollup gets ``None``, never ``0.0``
  * ``last_run`` is ``cant_measure`` when AL-1.3 recorded no last-seen — the
    surface never claims an app "didn't run"
  * ``readiness`` / ``offer`` on a draft carry AL-1.6b's and AL-1.7's own
    answers (AL-1.8b displays them) — and an unmeasured readiness dimension
    stays unmeasured rather than scoring zero
  * ``inferred`` usage is carried in its own key and never folded into total
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import server as _server  # noqa: E402
from evolve_admin.web.routes_apps import register_apps_routes  # noqa: E402


BOT_A = "team-bot-a"
BOT_B = "team-bot-b"


def _manifest(app_id: str, name: str, *, status: str = "defined", **extra) -> dict:
    data = {
        "id": app_id,
        "app_id": app_id,
        "name": name,
        "definition_status": status,
        "identity": {"purpose": f"{name} keeps the thing running. Second sentence."},
        "schema_version": 30,
    }
    data.update(extra)
    return data


def _write_rollup(shared: Path, bot: str, apps: dict) -> None:
    """A usage-by-app.json shaped like analyzer/usage_by_app._assemble writes."""
    (shared / bot).mkdir(parents=True, exist_ok=True)
    (shared / bot / "usage-by-app.json").write_text(json.dumps({
        "schema_version": 1,
        "bot_id": bot,
        "apps": apps,
        "coverage": {"d7": {"attributed_turns": 4, "inferred_turns": 0}},
    }))


def _entry(*, turns: int, cost: float, last_seen: str, inferred_turns: int = 0) -> dict:
    return {
        "last_seen_ts": last_seen,
        "d7": {
            "total": {"turns": turns, "cost_estimated": cost},
            "scheduled": {"turns": turns, "cost_estimated": cost},
            "explicit": {"turns": 0, "cost_estimated": 0.0},
            "inferred": {"turns": inferred_turns, "cost_estimated": 0.0},
        },
    }


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    """Two bots, a shared app, a bot-local app, and a draft."""
    shared = tmp_path / "shared"
    shared.mkdir()

    workspaces: dict[str, Path] = {}
    for bot in (BOT_A, BOT_B):
        ws = tmp_path / "Users" / bot / ".openclaw" / "workspace"
        (ws / "manifests").mkdir(parents=True)
        workspaces[bot] = ws

    def _write(bot: str, stem: str, data: dict) -> None:
        (workspaces[bot] / "manifests" / f"{stem}.json").write_text(json.dumps(data))

    # The multi-bot app — same app_id on both bots.
    _write(BOT_A, "morning-brief", _manifest(
        "morning-brief", "Morning Brief",
        scheduled_actions=[{"id": "a1"}], files=["apps/morning-brief/run.py"],
    ))
    _write(BOT_B, "morning-brief", _manifest(
        "morning-brief", "Morning Brief", files=["apps/morning-brief/run.py"],
    ))
    # A bot-local defined app.
    _write(BOT_A, "note-filer", _manifest("note-filer", "Note Filer"))
    # A draft: discovered, no app_id, evidence = files + cron.
    _write(BOT_B, "draft-thing", _manifest(
        "draft-thing", "Draft Thing", status="discovered",
        evidence_files=["workspace/notes.md"], crons=[{"schedule": "0 9 * * *"}],
    ))

    # Only BOT_A has a rollup — BOT_B must read as "not measured", not zero.
    _write_rollup(shared, BOT_A, {
        "morning-brief": _entry(turns=6, cost=0.42,
                                last_seen="2026-08-19T09:00:00Z",
                                inferred_turns=3),
    })

    (shared / "network.json").write_text(json.dumps({
        "sharedDir": str(shared),
        "members": [BOT_A, BOT_B],
    }))

    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(workspaces.get(bid, tmp_path / "none"))},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)

    app = Flask(__name__)
    register_apps_routes(app, shared / "network.json")
    app.testing = True
    return {"client": app.test_client(), "shared": shared,
            "workspaces": workspaces, "tmp": tmp_path}


def _apps(pod, query: str = "") -> dict:
    res = pod["client"].get("/api/apps" + query)
    assert res.status_code == 200, res.data
    return res.get_json()


# ── /api/apps — grouping across bots ────────────────────────────────────────


def test_pod_list_groups_one_row_per_app_id_across_bots(pod):
    body = _apps(pod)
    assert body["ok"] is True
    ids = [a["app_id"] for a in body["apps"]]
    # Morning Brief is installed twice but appears ONCE — the whole point of
    # the pod-first transpose.
    assert ids == ["morning-brief", "note-filer"]
    brief = body["apps"][0]
    assert [b["bot_id"] for b in brief["bots"]] == [BOT_A, BOT_B]
    assert brief["bots_total"] == 2
    assert brief["name"] == "Morning Brief"
    # purpose is the one-sentence Tier-1 line, not the whole prose block.
    assert brief["purpose"] == "Morning Brief keeps the thing running."


def test_pod_list_excludes_discovered_drafts(pod):
    assert "draft-thing" not in [a["app_id"] for a in _apps(pod)["apps"]]


def test_bot_filter_narrows_rows_but_keeps_every_install_visible(pod):
    body = _apps(pod, f"?bot={BOT_B}")
    assert [a["app_id"] for a in body["apps"]] == ["morning-brief"]
    # Filtering to one bot must NOT hide the other bot from the row: "who else
    # has this?" is the question the old per-bot tabs could not answer.
    assert [b["bot_id"] for b in body["apps"][0]["bots"]] == [BOT_A, BOT_B]


# ── /api/apps — the honesty contract ────────────────────────────────────────


def test_unmeasured_bot_reads_as_none_not_zero(pod):
    brief = _apps(pod)["apps"][0]
    by_bot = {b["bot_id"]: b for b in brief["bots"]}
    assert by_bot[BOT_A]["cost_7d"] == 0.42
    assert by_bot[BOT_A]["turns_7d"] == 6
    assert by_bot[BOT_A]["usage_measured"] is True
    # BOT_B has no rollup file at all.
    assert by_bot[BOT_B]["cost_7d"] is None
    assert by_bot[BOT_B]["turns_7d"] is None
    assert by_bot[BOT_B]["usage_measured"] is False
    # And the pod row says how much of the pod it could actually measure.
    assert brief["usage_measured_bots"] == 1
    assert brief["bots_total"] == 2


def test_app_with_no_rollup_row_has_no_cost_at_all(pod):
    filer = [a for a in _apps(pod)["apps"] if a["app_id"] == "note-filer"][0]
    assert filer["cost_7d"] is None
    assert filer["turns_7d"] is None
    assert filer["bots"][0]["grade_breakdown"] == {}


def test_last_run_is_tri_state_and_never_claims_a_miss(pod):
    brief = _apps(pod)["apps"][0]
    by_bot = {b["bot_id"]: b for b in brief["bots"]}
    assert by_bot[BOT_A]["last_run"] == {
        "state": "seen", "ts": "2026-08-19T09:00:00Z",
    }
    # No last-seen for BOT_B → "can't measure". Never "didn't run": that
    # verdict needs the delivery ledger (AL-2.1) and is not derived here.
    assert by_bot[BOT_B]["last_run"] == {"state": "cant_measure", "ts": None}
    assert brief["last_run"]["state"] == "seen"


def test_inferred_usage_is_never_folded_into_the_total(pod):
    brief = _apps(pod)["apps"][0]
    facts = [b for b in brief["bots"] if b["bot_id"] == BOT_A][0]
    assert facts["turns_7d"] == 6                      # scheduled + explicit
    assert facts["grade_breakdown"]["inferred"]["turns"] == 3
    assert facts["grade_breakdown"]["scheduled"]["turns"] == 6


def test_payload_names_the_unmeasured_bots(pod):
    body = _apps(pod)
    assert body["usage_measured_bots"] == [BOT_A]
    assert BOT_B in body["usage_unmeasured_bots"]


# ── /api/apps/<app_id> ──────────────────────────────────────────────────────


def test_detail_returns_bots_facts_and_absent_bots(pod):
    res = pod["client"].get("/api/apps/note-filer")
    assert res.status_code == 200
    body = res.get_json()
    assert body["app_id"] == "note-filer"
    assert [b["bot_id"] for b in body["bots"]] == [BOT_A]
    # The "Install to…" domain: bots that do NOT have it. (Disabled in 1.8a —
    # deterministic install is AL-1.5b — but the surface still has to know.)
    assert body["bots_without"] == sorted([BOT_B, "evolve"])
    assert body["definition_states"] == {BOT_A: "defined"}
    assert body["signals"] == []


def test_detail_config_summary_is_plain_language(pod):
    body = pod["client"].get("/api/apps/morning-brief").get_json()
    by_bot = {b["bot_id"]: b for b in body["bots"]}
    assert by_bot[BOT_A]["config_summary"] == "1 schedule · 1 file"
    assert by_bot[BOT_B]["config_summary"] == "1 file"


def test_detail_carries_the_signals_that_name_this_app(pod):
    """The Signal-store lookup resolves for REAL — not through a mock.

    ``routes_apps._signals_for_app`` imports ``signals.store`` lazily and
    degrades to ``[]`` on any failure, which is the shape where a broken seam
    looks exactly like a quiet pod. So this test writes an actual Signal file
    and asserts it comes back: if the import, the iterator or the
    ``details.app_id`` match ever stops working, this fails instead of
    silently reporting "nothing flagged" forever.
    """
    from schema.signal import Signal, new_signal_id
    from signals import store as signals_store

    mine = Signal(
        id=new_signal_id(), signature="app_script_failure_audit:x:1",
        producer="app_script_failure_audit", type="app_script_failure",
        flavor="maintenance", severity="warn", scope="bot", bot_id=BOT_A,
        title="note-filer failed twice yesterday",
        details={"app_id": "note-filer", "failures": 2},
    )
    other = Signal(
        id=new_signal_id(), signature="app_script_failure_audit:y:1",
        producer="app_script_failure_audit", type="app_script_failure",
        flavor="maintenance", severity="warn", scope="bot", bot_id=BOT_A,
        title="a different app's problem",
        details={"app_id": "morning-brief"},
    )
    signals_store.write_signal(mine, pod["shared"])
    signals_store.write_signal(other, pod["shared"])

    body = pod["client"].get("/api/apps/note-filer").get_json()
    assert [s["title"] for s in body["signals"]] == [
        "note-filer failed twice yesterday"
    ], "the detail view must carry this app's signals and only this app's"


def test_detail_404s_for_an_unknown_app(pod):
    res = pod["client"].get("/api/apps/nope-not-here")
    assert res.status_code == 404
    assert res.get_json()["ok"] is False


def test_detail_reaches_a_draft_too(pod):
    """A draft has no app_id, but its manifest still resolves to one via the
    legacy chain — following a link to it must land on detail, not a dead end."""
    res = pod["client"].get("/api/apps/draft-thing")
    assert res.status_code == 200
    assert res.get_json()["definition_states"] == {BOT_B: "discovered"}


# ── /api/apps/discovered ────────────────────────────────────────────────────


def test_discovered_lists_only_drafts_with_derived_evidence(pod):
    body = pod["client"].get("/api/apps/discovered").get_json()
    assert body["count"] == 1
    draft = body["drafts"][0]
    assert draft["bot_id"] == BOT_B
    assert draft["manifest_stem"] == "draft-thing"
    assert draft["name"] == "Draft Thing"
    # Evidence is derived from what EXISTS, not from a stored label.
    assert draft["evidence"] == ["files", "cron"]


def test_discovered_readiness_and_offer_are_filled_by_their_own_chips(pod):
    """1.8a's two ``None`` placeholders became displays in AL-1.8b.

    The scorer (AL-1.6b) and the offer (AL-1.7) both landed, so the honest
    cell is no longer "not yet" — it is what those chips say. What must NOT
    change is the shape: readiness stays an envelope carrying its own
    measured-ness, and the offer stays a state rather than a boolean, so
    "nobody has been asked" and "we could not check" keep their distance.
    """
    draft = pod["client"].get("/api/apps/discovered").get_json()["drafts"][0]
    readiness = draft["readiness"]
    assert readiness is not None, "AL-1.6b's scorer is not reaching the surface"
    assert readiness["dimensions_measured"] < readiness["dimensions_total"], (
        "the fixture draft has no recurrence or stability producer — an "
        "unmeasured dimension must stay unmeasured, not become a zero"
    )
    assert draft["offer"] == {
        "state": "not_offered", "by": None, "at": None,
        "until": None, "outcome": None, "to": None,
    }


# ── /api/apps/activity ──────────────────────────────────────────────────────


def test_activity_unions_promotions_and_publishes_newest_first(pod):
    shared = pod["shared"]
    # A promotion stamp, as coherence_actions.promote_to_defined writes it.
    mf = pod["workspaces"][BOT_A] / "manifests" / "note-filer.json"
    data = json.loads(mf.read_text())
    data["provenance"] = {
        "last_promoted_at": "2026-08-18T10:00:00Z",
        "last_promoted_by": "ui:operator",
    }
    mf.write_text(json.dumps(data))
    # A publish, as share_routes writes it into the local gallery tier.
    spec_dir = shared / "gallery" / "local" / "s-morning-brief"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026.08.19-1.0.json").write_text(json.dumps({
        "app_id": "morning-brief",
        "source": {"bot_id": BOT_A, "shared_at": "2026-08-19T12:00:00Z"},
    }))

    body = pod["client"].get("/api/apps/activity").get_json()
    kinds = [(e["kind"], e["app_id"]) for e in body["entries"]]
    assert kinds == [("published", "morning-brief"), ("promoted", "note-filer")]
    assert body["truncated"] is False
    assert body["entries"][0]["detail"].startswith("version 2026.08.19-1.0")


def test_activity_reports_truncation_rather_than_slicing_silently(pod):
    spec_dir = pod["shared"] / "gallery" / "local" / "s-many"
    spec_dir.mkdir(parents=True)
    for n in range(5):
        (spec_dir / f"2026.08.0{n + 1}-1.0.json").write_text(json.dumps({
            "app_id": "many", "source": {"shared_at": f"2026-08-0{n + 1}T00:00:00Z"},
        }))
    body = pod["client"].get("/api/apps/activity?limit=2").get_json()
    assert body["count"] == 2
    assert body["total"] == 5
    assert body["truncated"] is True


def test_activity_marks_a_timestamp_it_had_to_infer_from_the_file(pod):
    """A Spec with no ``shared_at`` falls back to the file mtime — and SAYS so."""
    spec_dir = pod["shared"] / "gallery" / "local" / "s-undated"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026.01.01-1.0.json").write_text(json.dumps({"app_id": "undated"}))
    entries = pod["client"].get("/api/apps/activity").get_json()["entries"]
    undated = [e for e in entries if e["app_id"] == "undated"][0]
    assert "time from file, not recorded" in undated["detail"]
