"""AL-1.8b — the detail-fill reads (internal/build-AL-1.8b-detail-fill.md §4).

    GET /api/apps/<app_id>          + package.files[] with per-bot states,
                                      + requires / exclusive_tools
    GET /api/apps/discovered/<ref>  the drawer: evidence, readiness, offer

THE FIXTURE IS THE TEST. Two bots and two apps, chosen so every state the
Files panel can render is produced by a REAL condition rather than asserted
from a stub:

  ``ea-brief``   installed from a files-pack, so its recorded digests are
                 pre-substitution SOURCE digests. One file declares no
                 placeholder (identical on both bots -> ``ok``) and one
                 declares ``{bot_id}`` (differs on both bots, and the
                 difference is fully reproducible from the source bytes +
                 the declared placeholder + the bot's context -> the
                 ``differs_placeholder`` state, per AL-1.5c §9.6).
  ``note-filer`` no pack — the shape 231 of the pod's 232 artifacts have.
                 Its digests come from hashing a bot's workspace, so one
                 file is ``ok`` on both, one is present on team-bot-a only
                 (``missing`` on team-bot-b), and one is present on
                 team-bot-b only, which leaves the app with NO recorded
                 digest for it: the row reads "not hashed" and team-bot-b's
                 cell reads ``cant_measure``, not ``ok``.

The last case is the one worth being explicit about. A file the app cannot
put a digest against is not a file that matches — the honest cell is "we
have nothing to compare this to", and a version of this panel that answered
``ok`` there would be claiming verification it never performed.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import gallery as _gallery  # noqa: E402
from evolve_admin.applications.files_pack import (  # noqa: E402
    install_files_pack_to_workspace,
    load_files_pack_metadata,
    resolve_install_context,
)
from evolve_admin.web import server as _server  # noqa: E402
from evolve_admin.web.routes_apps import register_apps_routes  # noqa: E402

BOT_A = "team-bot-a"
BOT_B = "team-bot-b"
PKG_ID = "p-ea-brief"

STEADY = "scripts/steady.py"
GREET = "scripts/greet.sh"

_STEADY_SRC = "print('the same everywhere')\n"
_GREET_SRC = "#!/bin/sh\necho \"hello from {bot_id}\"\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_pack(root: Path) -> Path:
    """A minimal but REAL files-pack: two files, one placeholder-bearing."""
    pack = root / "gallery" / "ea-brief" / "files"
    (pack / "scripts").mkdir(parents=True)
    (pack / STEADY).write_text(_STEADY_SRC)
    (pack / GREET).write_text(_GREET_SRC)
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "files": [
            {"path": STEADY, "mode": "0644", "sha256": _sha(_STEADY_SRC),
             "size_bytes": len(_STEADY_SRC), "placeholders": []},
            {"path": GREET, "mode": "0755", "sha256": _sha(_GREET_SRC),
             "size_bytes": len(_GREET_SRC), "placeholders": ["bot_id"]},
        ],
    }))
    return pack


def _manifest(app_id: str, name: str, *, status: str = "defined", **extra) -> dict:
    data = {
        "id": app_id,
        "app_id": app_id,
        "name": name,
        "definition_status": status,
        "identity": {"purpose": f"{name} does one thing. And says so."},
        "created_at": "2026-08-01T00:00:00Z",
        "schema_version": 30,
    }
    data.update(extra)
    return data


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    pack = _write_pack(tmp_path)

    workspaces: dict[str, Path] = {}
    for bot in (BOT_A, BOT_B):
        ws = tmp_path / "Users" / bot / ".openclaw" / "workspace"
        (ws / "manifests").mkdir(parents=True)
        workspaces[bot] = ws

    def _write(bot: str, stem: str, data: dict) -> None:
        (workspaces[bot] / "manifests" / f"{stem}.json").write_text(json.dumps(data))

    # ── ea-brief: a real files-pack install on both bots ────────────────────
    meta = load_files_pack_metadata(pack)
    assert meta is not None
    for bot in (BOT_A, BOT_B):
        result = install_files_pack_to_workspace(
            meta, pack, workspaces[bot],
            resolve_install_context(
                bot_id=bot, bot_user=bot, workspace=str(workspaces[bot]),
                pkg_id=PKG_ID, app_id="ea-brief",
                installed_at="2026-08-01T00:00:00Z", shared_dir=str(shared),
            ),
        )
        assert not result.errors, result.errors
        _write(bot, "ea-brief", _manifest(
            "ea-brief", "EA Brief", pkg_id=PKG_ID,
            files=[STEADY, GREET],
            dependencies={
                "oc_skills": [{"skill_id": "calendar-read"}],
                "integrations": [{"integration_id": "google-calendar"}],
                "credentials": [{"name": "GOOGLE_OAUTH_TOKEN"}],
            },
            provided_capabilities=[{"requires_mcp_tools": ["calendar.list"]}],
        ))

    # ── note-filer: no pack; digests come from the workspaces ───────────────
    (workspaces[BOT_A] / "apps" / "note-filer").mkdir(parents=True)
    (workspaces[BOT_B] / "apps" / "note-filer").mkdir(parents=True)
    (workspaces[BOT_A] / "apps/note-filer/run.py").write_text("run()\n")
    (workspaces[BOT_B] / "apps/note-filer/run.py").write_text("run()\n")
    (workspaces[BOT_A] / "apps/note-filer/helper.py").write_text("help()\n")
    (workspaces[BOT_B] / "apps/note-filer/local.py").write_text("local()\n")
    for bot in (BOT_A, BOT_B):
        _write(bot, "note-filer", _manifest("note-filer", "Note Filer", files=[
            {"path": "apps/note-filer/run.py", "marker_state": "vital_to_blueprint"},
            {"path": "apps/note-filer/helper.py"},
            {"path": "apps/note-filer/local.py"},
        ]))

    # ── one draft, with conversation-only evidence (AL-1.6c's producer) ─────
    _write(BOT_B, "receipt-sorter", _manifest(
        "receipt-sorter", "Receipt Sorter", status="discovered",
        draft_id="d-receipts",
        description="Files receipts into monthly folders when asked.",
        conversation_only=True,
        conversation_evidence={
            "label": "sort my receipts", "days_seen": 6, "window_days": 10,
            "occurrences": 9, "first_day": "2026-08-05",
            "last_day": "2026-08-19", "center_hour": 9,
            "primary_requester": "the primary user",
            "requesters": ["the primary user"],
        },
        evidence_files=["workspace/receipts/inbox.md"],
        crons=[{"schedule": "0 9 * * *", "script": "receipts.py"}],
        heartbeat_evidence={"file_path": "HEARTBEAT.MD",
                            "section_anchors": ["## Receipts"]},
    ))

    (shared / "network.json").write_text(json.dumps({
        "sharedDir": str(shared), "members": [BOT_A, BOT_B],
    }))

    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(workspaces.get(bid, tmp_path / "none"))},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)
    monkeypatch.setattr(_gallery, "find_files_pack_dir",
                        lambda pkg_id: pack if pkg_id == PKG_ID else None)

    app = Flask(__name__)
    register_apps_routes(app, shared / "network.json")
    app.testing = True
    return {"client": app.test_client(), "shared": shared,
            "workspaces": workspaces, "pack": pack, "tmp": tmp_path}


def _reader(pod):
    """``read_manifests`` over the fixture's on-disk manifests."""
    def _read(bot_id: str) -> "list[tuple[str, dict]]":
        directory = pod["workspaces"][bot_id] / "manifests"
        return [(p.stem, json.loads(p.read_text()))
                for p in sorted(directory.glob("*.json"))]
    return _read


def _detail(pod, app_id: str) -> dict:
    res = pod["client"].get(f"/api/apps/{app_id}")
    assert res.status_code == 200, res.data
    return res.get_json()


def _rows(payload: dict) -> dict:
    return {f["path"]: f for f in payload["package"]["files"]}


# ── The Files panel: the app's files, the bots' realizations ────────────────


def test_files_are_app_level_with_one_cell_per_bot(pod):
    body = _detail(pod, "note-filer")
    rows = _rows(body)
    assert sorted(rows) == [
        "apps/note-filer/helper.py",
        "apps/note-filer/local.py",
        "apps/note-filer/run.py",
    ]
    # The app is the row; the bots are columns on it.
    assert sorted(rows["apps/note-filer/run.py"]["bots"]) == [BOT_A, BOT_B]


def test_a_file_both_bots_have_reads_ok_on_both(pod):
    run = _rows(_detail(pod, "note-filer"))["apps/note-filer/run.py"]
    assert run["sha256"], "a file present on both bots must carry a digest"
    assert run["bots"][BOT_A]["state"] == "ok"
    assert run["bots"][BOT_B]["state"] == "ok"


def test_a_file_one_bot_no_longer_has_reads_missing_there_only(pod):
    """The pod's known drift (AL-1.5c §9.4: 7 files a manifest still names)."""
    helper = _rows(_detail(pod, "note-filer"))["apps/note-filer/helper.py"]
    assert helper["bots"][BOT_A]["state"] == "ok"
    assert helper["bots"][BOT_B]["state"] == "missing"


def test_a_file_with_no_recorded_digest_is_never_reported_as_ok(pod):
    """"Not hashed" + a real file on one bot = ``cant_measure``, not ``ok``.

    team-bot-b has this file and team-bot-a does not, so the app's own
    recorded digest is empty. Answering ``ok`` for team-bot-b would claim a
    comparison that never happened.
    """
    local = _rows(_detail(pod, "note-filer"))["apps/note-filer/local.py"]
    assert local["sha256"] == "", "expected the honest 'not hashed'"
    assert local["sha_kind"] is None, "an absent digest has no carrier to label"
    assert local["bots"][BOT_A]["state"] == "missing"
    assert local["bots"][BOT_B]["state"] == "cant_measure"
    assert "no recorded digest" in local["bots"][BOT_B]["note"]


def test_the_role_survives_the_hashing_pass(pod):
    """AL-1.5c §9.3a's regression, re-pinned at the surface.

    ``marker_state`` is the role carrier 333 live entries actually use;
    injecting hashed files makes the derivation take its whole-cloth branch,
    so a role dropped here would be invisible to a check written against
    ``role``.
    """
    run = _rows(_detail(pod, "note-filer"))["apps/note-filer/run.py"]
    assert run["role"] == "vital_to_blueprint"


def test_workspace_digests_are_labelled_as_realized_not_as_source(pod):
    """§9.2: the field has two carriers, so the payload says which one."""
    package = _detail(pod, "note-filer")["package"]
    assert package["sha_kind"] == "realized"
    assert package["sha_kind_bot"] == BOT_A
    assert _rows({"package": package})["apps/note-filer/run.py"]["sha_kind"] == "realized"


# ── The placeholder state, in AL-1.5c §9.6's machine-checkable sense ────────


def test_a_pack_installed_file_with_no_placeholder_matches_its_source(pod):
    steady = _rows(_detail(pod, "ea-brief"))[STEADY]
    assert steady["sha256"] == _sha(_STEADY_SRC), "expected the SOURCE digest"
    assert steady["bots"][BOT_A]["state"] == "ok"
    assert steady["bots"][BOT_B]["state"] == "ok"


def test_a_declared_placeholder_difference_is_named_as_such(pod):
    """Not "it has placeholders, so probably fine" — re-substituted and checked.

    The cell reads ``differs_placeholder`` only because re-running the pack
    source through the declared ``{bot_id}`` under each bot's own context
    reproduces that bot's digest exactly.
    """
    greet = _rows(_detail(pod, "ea-brief"))[GREET]
    assert greet["sha256"] == _sha(_GREET_SRC)
    for bot in (BOT_A, BOT_B):
        assert greet["bots"][bot]["state"] == "differs_placeholder", greet
        assert greet["bots"][bot]["realized_sha"] != greet["sha256"]
    # The two bots' realizations differ from each other, too — which is the
    # whole reason a plain sha comparison could not have answered this.
    assert (greet["bots"][BOT_A]["realized_sha"]
            != greet["bots"][BOT_B]["realized_sha"])


def test_pack_digests_are_labelled_source(pod):
    assert _detail(pod, "ea-brief")["package"]["sha_kind"] == "source"


def test_an_unexplained_difference_never_reads_as_placeholder(pod):
    """Edit one bot's copy by hand: the state must degrade to ``differs``.

    This is the guard on the claim above. If the check ever softened to "the
    entry declares a placeholder, so call it explained", this would stay
    green while the panel started laundering real drift.
    """
    (pod["workspaces"][BOT_B] / GREET).write_text("#!/bin/sh\necho tampered\n")
    greet = _rows(_detail(pod, "ea-brief"))[GREET]
    assert greet["bots"][BOT_A]["state"] == "differs_placeholder"
    assert greet["bots"][BOT_B]["state"] == "differs"
    assert "nothing on the pod explains" in greet["bots"][BOT_B]["note"]


def test_an_unreadable_workspace_is_cant_measure_not_ok(pod):
    """A bot whose workspace does not resolve contributes no verdict at all.

    Driven through ``build_app_detail`` with no ``workspace_for`` — the
    documented shape for a caller that cannot reach the bots — rather than
    by breaking the route's bot-path seam, which would also take the
    manifest reader down and answer 404 instead of answering honestly.
    """
    from evolve_admin.applications import pod_apps

    detail = pod_apps.build_app_detail(
        "note-filer", [BOT_A, BOT_B],
        read_manifests=_reader(pod), shared_dir=pod["shared"],
    )
    assert detail is not None
    for row in _rows(detail).values():
        for cell in row["bots"].values():
            assert cell["state"] == "cant_measure", row
            assert "workspace could not be read" in cell["note"]
    # …and the file list still names the files, so the panel reports what the
    # app HAS rather than rendering as though it had none.
    assert len(detail["package"]["files"]) == 3
    assert detail["package"]["sha_kind"] is None


def test_a_bot_whose_account_name_differs_is_still_measured(pod, monkeypatch):
    """The workspace lookup must pass the RESOLVED user, like the reader does.

    ``resolve_bot_paths`` falls back to the bot_id when given no user, so a
    bot whose macOS account differs from its bot_id (this pod has them)
    would resolve to a home that does not exist — and every one of its files
    would read "can't measure" while its manifests read fine. That failure
    is invisible from the outside: it looks exactly like a pod that cannot
    be measured.
    """
    real = {BOT_A: pod["workspaces"][BOT_A], BOT_B: pod["workspaces"][BOT_B]}
    accounts = {BOT_A: "acct-a", BOT_B: "acct-b"}
    by_account = {accounts[b]: w for b, w in real.items()}

    monkeypatch.setattr(_server, "_resolve_bot_user",
                        lambda bid, *a, **kw: accounts.get(bid, bid))
    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        # Only the ACCOUNT resolves; the bot_id does not, exactly as a real
        # pwd lookup behaves for a renamed account.
        lambda bid, user=None: {"workspace": str(
            by_account.get(user, pod["tmp"] / "nonexistent"))},
    )
    run = _rows(_detail(pod, "note-filer"))["apps/note-filer/run.py"]
    assert run["bots"][BOT_A]["state"] == "ok"
    assert run["bots"][BOT_B]["state"] == "ok"


# ── The Uses panel ─────────────────────────────────────────────────────────


def test_requires_groups_round_trip_and_secrets_are_names_only(pod):
    body = _detail(pod, "ea-brief")
    assert body["requires"] == {
        "skills": ["calendar-read"],
        "tools": ["calendar.list"],
        "integrations": ["google-calendar"],
        "secrets": ["GOOGLE_OAUTH_TOKEN"],
    }
    assert body["exclusive_tools"] == []
    assert body["requires_declared"] == 4
    # A name, never a value: nothing in the payload carries a secret's
    # contents, and the manifest never held one to begin with.
    assert "GOOGLE_OAUTH_TOKEN" in body["requires"]["secrets"]
    assert json.dumps(body).count("GOOGLE_OAUTH_TOKEN") == 1


def test_an_app_declaring_nothing_reports_empty_groups_not_a_missing_key(pod):
    body = _detail(pod, "note-filer")
    assert body["requires"] == {"skills": [], "tools": [],
                                "integrations": [], "secrets": []}
    assert body["requires_declared"] == 0


# ── The Discovered drawer ──────────────────────────────────────────────────


def _draft(pod, ref: str, query: str = ""):
    return pod["client"].get(f"/api/apps/discovered/{ref}" + query)


def test_drawer_returns_the_concrete_evidence_for_a_draft(pod):
    body = _draft(pod, "d-receipts").get_json()
    assert body["ok"] is True
    assert body["bot_id"] == BOT_B
    assert body["name"] == "Receipt Sorter"
    assert body["description"].startswith("Files receipts")
    evidence = body["evidence"]
    assert evidence["files"] == ["workspace/receipts/inbox.md"]
    assert evidence["schedules"][0]["when"] == "0 9 * * *"
    assert evidence["memory"] == {"path": "HEARTBEAT.MD",
                                  "sections": ["## Receipts"]}


def test_a_conversation_only_draft_is_not_reported_as_having_no_evidence(pod):
    """AL-1.6c gave that carrier a producer, so the surface must show it.

    Before this, a draft that exists precisely BECAUSE someone keeps asking
    for it — no file, no cron, no standing instruction — read as "nothing
    recorded", which is the one thing it is not.
    """
    body = pod["client"].get("/api/apps/discovered").get_json()
    draft = [d for d in body["drafts"] if d["manifest_stem"] == "receipt-sorter"][0]
    assert "conversation" in draft["evidence"]
    assert _draft(pod, "d-receipts").get_json()["evidence"]["conversation_only"] is True


def test_drawer_carries_the_conversation_recurrence_verbatim(pod):
    """AL-1.6c's arithmetic is WHY the draft exists — so the drawer shows it."""
    conversation = _draft(pod, "d-receipts").get_json()["evidence"]["conversation"]
    assert conversation["days_seen"] == 6
    assert conversation["window_days"] == 10
    assert conversation["primary_requester"] == "the primary user"
    assert conversation["label"] == "sort my receipts"


def test_drawer_reports_readiness_and_offer_state(pod):
    body = _draft(pod, "d-receipts").get_json()
    assert body["readiness"] is not None
    assert body["readiness"]["dimensions_total"] == 3
    # AL-1.7 exists but nothing has been offered on this pod.
    assert body["offer"]["state"] == "not_offered"


def test_drawer_finds_a_draft_by_its_manifest_stem_too(pod):
    """The pod measured ``with_draft_id=0`` on 74 manifests (2026-08-21)."""
    body = _draft(pod, "receipt-sorter").get_json()
    assert body["ok"] is True and body["draft_id"] == "d-receipts"


def test_drawer_404s_for_something_that_is_not_a_draft(pod):
    # A DEFINED app is not a draft — it has its own detail view.
    assert _draft(pod, "note-filer").status_code == 404
    assert _draft(pod, "nothing-here").status_code == 404


def test_an_ambiguous_stem_asks_which_bot_rather_than_picking_one(pod):
    """The drawer's actions are per (bot, draft) — guessing would misfire."""
    for bot in (BOT_A, BOT_B):
        (pod["workspaces"][bot] / "manifests" / "twin.json").write_text(json.dumps(
            _manifest("twin", "Twin", status="discovered")))
    res = _draft(pod, "twin")
    assert res.status_code == 409
    assert [c["bot_id"] for c in res.get_json()["candidates"]] == [BOT_A, BOT_B]
    # …and naming the bot resolves it.
    assert _draft(pod, "twin", f"?bot={BOT_A}").status_code == 200


# ── The list keeps its shape ───────────────────────────────────────────────


def test_the_discovered_list_carries_the_offer_state_it_could_read(pod):
    body = pod["client"].get("/api/apps/discovered").get_json()
    assert body["offers_readable"] is True
    draft = [d for d in body["drafts"] if d["manifest_stem"] == "receipt-sorter"][0]
    assert draft["offer"]["state"] == "not_offered"
    assert draft["readiness"]["band"] in {"weak", "emerging", "ready", "unscored"}


def test_an_unreadable_proposal_store_reads_unknown_not_not_offered(pod, monkeypatch):
    """"We could not check" and "nobody asked" are different statements."""
    from evolve_admin.applications import app_offer_state

    monkeypatch.setattr(app_offer_state, "build_offer_index",
                        lambda shared_dir: ({}, False))
    body = pod["client"].get("/api/apps/discovered").get_json()
    assert body["offers_readable"] is False
    draft = [d for d in body["drafts"] if d["manifest_stem"] == "receipt-sorter"][0]
    assert draft["offer"]["state"] == "unknown"
