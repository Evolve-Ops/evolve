"""One app on N bots, through the routes (ALPHA-3a; audit B3 + P4; D-I).

The pod here is the audit's, reproduced: `personal-bot` and `team-bot-a`
BOTH have a Morning Brief, each discovered independently and therefore each
carrying its own minted ``app_id`` — the two rows the audit measured as
``p-befb87e0 bots_total=1`` / ``p-049bf7ab bots_total=1``.

What these pin, beyond "the row count is 1":

  * the two ids' USAGE is joined per install, so the grouped row's cost is
    the sum of both bots' real numbers and not the lead's alone. That join
    runs on each install's own id; getting it wrong would turn a measured
    cost into a silent zero, which the honesty contract forbids.
  * the claim is WITHDRAWABLE: ``?grouped=0`` returns the pre-ALPHA-3a
    payload, and the grouped row carries the same rows as ``members`` so the
    surface can split without a second read.
  * the DETAIL agrees with the list. B3's actual screenshot was a detail
    page saying "Not on team-bot-a" beside a bot that had the app, so a list
    that grouped and a detail that did not would move the blocker, not fix it.
  * ``evolve`` is not offered as somewhere to install an app (P4).
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


PERSONAL = "personal-bot"
TEAM = "team-bot-a"

# The two independently minted ids the audit measured.
BRIEF_PERSONAL = "p-befb87e0"
BRIEF_TEAM = "p-049bf7ab"


def _manifest(app_id: str, name: str, files: "list[str]", **extra) -> dict:
    data = {
        "id": app_id,
        "app_id": app_id,
        "name": name,
        "definition_status": "defined",
        "identity": {"purpose": f"{name} does the thing. Second sentence."},
        "schema_version": 30,
        "files": list(files),
    }
    data.update(extra)
    return data


def _rollup(shared: Path, bot: str, apps: dict) -> None:
    (shared / bot).mkdir(parents=True, exist_ok=True)
    (shared / bot / "usage-by-app.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": bot, "apps": apps,
    }))


def _entry(cost: float, turns: int, last_seen: str) -> dict:
    return {
        "last_seen_ts": last_seen,
        "d7": {"total": {"turns": turns, "cost_estimated": cost},
               "scheduled": {"turns": turns, "cost_estimated": cost}},
    }


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()

    workspaces: dict[str, Path] = {}
    for bot in (PERSONAL, TEAM):
        ws = tmp_path / "Users" / bot / ".openclaw" / "workspace"
        (ws / "manifests").mkdir(parents=True)
        workspaces[bot] = ws

    def _write(bot: str, stem: str, data: dict) -> None:
        (workspaces[bot] / "manifests" / f"{stem}.json").write_text(json.dumps(data))

    # ── The B3 case: one app, two bots, two ids, two path shapes ────────────
    # personal-bot's manifest recorded ABSOLUTE paths; team-bot-a's are
    # workspace-relative. Both are real carriers on the pod, and comparing
    # the strings whole would score them at zero overlap.
    _write(PERSONAL, "morning-brief", _manifest(
        BRIEF_PERSONAL, "Morning Brief", [
            f"/Users/{PERSONAL}/.openclaw/workspace/apps/morning-brief/run.py",
            f"/Users/{PERSONAL}/.openclaw/workspace/apps/morning-brief/config.json",
        ],
        scheduled_actions=[{"id": "a1"}],
    ))
    _write(TEAM, "morning-brief", _manifest(
        BRIEF_TEAM, "morning_brief", [        # and a differently-cased name
            "apps/morning-brief/run.py",
            "apps/morning-brief/config.json",
        ],
    ))

    # ── Two apps that must NEVER group ──────────────────────────────────────
    # Different names, same files.
    _write(PERSONAL, "note-filer", _manifest(
        "p-note-1", "Note Filer", ["apps/shared/util.py"]))
    _write(TEAM, "receipt-sorter", _manifest(
        "p-receipt-1", "Receipt Sorter", ["apps/shared/util.py"]))
    # Same name, disjoint files.
    _write(PERSONAL, "report-a", _manifest(
        "p-report-a", "Report", ["apps/report/alpha.py"]))
    _write(TEAM, "report-b", _manifest(
        "p-report-b", "Report", ["apps/report/beta.py"]))

    # Real, different usage on each bot, keyed by each bot's OWN id.
    _rollup(shared, PERSONAL, {
        BRIEF_PERSONAL: _entry(0.2032, 16, "2026-08-19T09:00:00Z")})
    _rollup(shared, TEAM, {
        BRIEF_TEAM: _entry(0.0931, 7, "2026-08-20T09:00:00Z")})

    (shared / "network.json").write_text(json.dumps({
        "sharedDir": str(shared), "members": [PERSONAL, TEAM],
    }))

    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(workspaces.get(bid, tmp_path / "none"))},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)

    app = Flask(__name__)
    register_apps_routes(app, shared / "network.json")
    app.testing = True
    return {"client": app.test_client(), "shared": shared, "tmp": tmp_path}


def _apps(pod, query: str = "") -> dict:
    res = pod["client"].get("/api/apps" + query)
    assert res.status_code == 200, res.data
    return res.get_json()


def _by_name(body: dict, name: str) -> "list[dict]":
    return [a for a in body["apps"] if a["name"].lower().replace("_", " ") == name]


# ── The claim, at the route ─────────────────────────────────────────────────


def test_the_same_app_on_two_bots_is_one_row_on_two_bots(pod):
    """B3, inverted: `bots_total: 1` twice becomes `bots_total: 2` once."""
    body = _apps(pod)
    briefs = _by_name(body, "morning brief")
    assert len(briefs) == 1, [a["app_id"] for a in body["apps"]]
    row = briefs[0]
    assert row["bots_total"] == 2
    assert [b["bot_id"] for b in row["bots"]] == [PERSONAL, TEAM]
    assert row["grouped"] is True
    assert sorted(row["grouped_app_ids"]) == sorted([BRIEF_PERSONAL, BRIEF_TEAM])


def test_the_grouped_row_sums_both_bots_real_usage(pod):
    """The join runs per install, on each install's OWN id.

    Joining on the row's id instead would find no rollup entry for the
    non-lead member and report a measured cost as no turns — a fabricated
    zero, which is the exact failure the honesty contract exists to stop.
    """
    row = _by_name(_apps(pod), "morning brief")[0]
    by_bot = {b["bot_id"]: b for b in row["bots"]}
    assert by_bot[PERSONAL]["cost_7d"] == 0.2032
    assert by_bot[TEAM]["cost_7d"] == 0.0931
    assert row["cost_7d"] == pytest.approx(0.2963)
    assert row["turns_7d"] == 23
    assert row["usage_measured_bots"] == 2
    # And the row's last-run is the later of the two.
    assert row["last_run"] == {"state": "seen", "ts": "2026-08-20T09:00:00Z"}


def test_two_differently_named_apps_never_group(pod):
    body = _apps(pod)
    ids = {a["app_id"] for a in body["apps"]}
    assert {"p-note-1", "p-receipt-1"} <= ids
    for app_id in ("p-note-1", "p-receipt-1"):
        row = [a for a in body["apps"] if a["app_id"] == app_id][0]
        assert row["grouped"] is False
        assert row["bots_total"] == 1
        assert row["grouped_app_ids"] == [app_id]


def test_same_name_with_nothing_in_common_stays_two_rows(pod):
    """The name gate alone must not merge — the files have to agree too."""
    reports = [a for a in _apps(pod)["apps"] if a["name"] == "Report"]
    assert len(reports) == 2
    assert all(r["grouped"] is False for r in reports)


# ── The claim is withdrawable ───────────────────────────────────────────────


def test_grouped_zero_returns_the_ungrouped_payload(pod):
    body = _apps(pod, "?grouped=0")
    assert body["grouped"] is False
    briefs = _by_name(body, "morning brief")
    assert sorted(a["app_id"] for a in briefs) == sorted([BRIEF_PERSONAL, BRIEF_TEAM])
    assert all(a["bots_total"] == 1 for a in briefs)
    assert all(a["grouped"] is False for a in briefs)
    # No row anywhere carries a members list when the claim is withdrawn.
    assert all("members" not in a for a in body["apps"])


def test_the_grouped_row_carries_the_split_rows_it_stands_for(pod):
    """"Show separately" is a render, not a different answer."""
    row = _by_name(_apps(pod), "morning brief")[0]
    members = row["members"]
    assert [m["app_id"] for m in members] == row["grouped_app_ids"]
    assert all(m["bots_total"] == 1 for m in members)
    assert all(m["grouped"] is False for m in members)
    # They are the same rows the ungrouped read produces, field for field.
    ungrouped = {a["app_id"]: a for a in _apps(pod, "?grouped=0")["apps"]}
    for member in members:
        assert member == ungrouped[member["app_id"]]


def test_an_unparsable_grouped_value_keeps_the_claim_on(pod):
    """Failing toward the ungrouped answer would silently restore B3."""
    assert _apps(pod, "?grouped=maybe")["grouped"] is True
    assert _apps(pod, "?grouped=1")["grouped"] is True


# ── Detail agrees with the list ─────────────────────────────────────────────


def _detail(pod, app_id: str, query: str = "") -> dict:
    res = pod["client"].get(f"/api/apps/{app_id}{query}")
    assert res.status_code == 200, res.data
    return res.get_json()


@pytest.mark.parametrize("app_id", [BRIEF_PERSONAL, BRIEF_TEAM])
def test_detail_shows_both_bots_from_either_stored_id(pod, app_id):
    """Every member id stays a working link, and both show the whole app."""
    body = _detail(pod, app_id)
    assert body["app_id"] == app_id          # the id asked for keys the page
    assert [b["bot_id"] for b in body["bots"]] == [PERSONAL, TEAM]
    assert body["grouped"] is True
    # B3's screenshot: this list used to name a bot that visibly had the app.
    assert body["bots_without"] == []


def test_detail_can_be_split_too(pod):
    body = _detail(pod, BRIEF_PERSONAL, "?grouped=0")
    assert [b["bot_id"] for b in body["bots"]] == [PERSONAL]
    assert body["grouped"] is False
    # Split, team-bot-a genuinely does not have THIS record — and says so.
    assert body["bots_without"] == [TEAM]


def test_detail_files_panel_has_one_column_per_bot_not_two_for_one(pod):
    """The disjoint-bots rule is what keeps this panel well-formed."""
    body = _detail(pod, BRIEF_PERSONAL)
    bots = [b["bot_id"] for b in body["bots"]]
    assert len(bots) == len(set(bots))
    for row in (body["package"] or {}).get("files", []):
        assert set(row.get("bots", {})) <= set(bots)


# ── P4 ──────────────────────────────────────────────────────────────────────


def test_the_service_account_is_never_an_install_target(pod):
    """`evolve` can hold a manifest; it is not somewhere you install an app."""
    body = _detail(pod, "p-note-1")
    assert "evolve" not in body["bots_without"]
    assert body["bots_without"] == [TEAM]


def test_the_service_account_is_still_listed_as_a_bot_that_can_hold_apps(pod):
    """P4 narrows the install-target list only — it hides no real install."""
    assert "evolve" in _apps(pod)["bots"]


# ── B1 regression: v7-arc identity divergence in the usage join ─────────────
#
# A v7-arc Instance is the one shape where the RAW and HYDRATED manifests
# resolve to DIFFERENT ids: hydration sets ``id = instance_id`` and
# ``pkg_id = provenance.spec_id``, so the hydrated dict resolves to the Spec
# id while the rollup — and the grouping key — use the raw one
# (``resolve_app_id``: "feed it the RAW manifest").
#
# The usage join must therefore run on the raw-resolved id. Joining on
# anything derived from the hydrated dict would report "measured, no turns"
# for a bot with real attributed turns, under BOTH ``grouped`` values, since
# ``_app_row`` is shared by the grouped and ungrouped paths — which would also
# break the withdrawability premise D-I rests on.
#
# Nothing else in this suite or in test_app_grouping.py constructs a v7-arc
# manifest, so without these the join could be re-keyed to the hydrated id and
# CI would stay green over it.

V7_PERSONAL_INSTANCE = "inst-aaaa1111"
V7_TEAM_INSTANCE = "inst-bbbb2222"
V7_SPEC_ID = "morning-brief"
V7_SPEC_VERSION = "1.0.0"


def _v7_instance(instance_id: str, files: "list[str]") -> dict:
    """A v7-arc Instance: no top-level ``app_id``/``pkg_id``/``id``.

    Identity resolves down the legacy chain to ``instance_id``; the name and
    description live on the bound Spec, which is what hydration fetches.
    """
    return {
        "instance_id": instance_id,
        "manifest_shape": "v7-arc",
        "definition_status": "defined",
        "schema_version": 30,
        "identity": {"purpose": "Morning Brief does the thing. Second sentence."},
        "files": list(files),
        "provenance": {"spec_id": V7_SPEC_ID, "spec_version": V7_SPEC_VERSION},
    }


@pytest.fixture
def v7_pod(tmp_path: Path, monkeypatch):
    """Two bots, each carrying a v7-arc Instance of the same gallery app."""
    shared = tmp_path / "shared"
    shared.mkdir()

    spec_dir = shared / "gallery" / "local" / V7_SPEC_ID
    spec_dir.mkdir(parents=True)
    (spec_dir / f"{V7_SPEC_VERSION}.json").write_text(json.dumps({
        "name": "Morning Brief",
        "description": "The brief, every morning.",
        "app_version": V7_SPEC_VERSION,
    }))

    workspaces: dict[str, Path] = {}
    for bot in (PERSONAL, TEAM):
        ws = tmp_path / "Users" / bot / ".openclaw" / "workspace"
        (ws / "manifests").mkdir(parents=True)
        workspaces[bot] = ws

    def _write(bot: str, stem: str, data: dict) -> None:
        (workspaces[bot] / "manifests" / f"{stem}.json").write_text(json.dumps(data))

    _write(PERSONAL, V7_PERSONAL_INSTANCE, _v7_instance(
        V7_PERSONAL_INSTANCE, ["apps/morning-brief/run.py",
                               "apps/morning-brief/config.json"]))
    _write(TEAM, V7_TEAM_INSTANCE, _v7_instance(
        V7_TEAM_INSTANCE, ["apps/morning-brief/run.py",
                           "apps/morning-brief/config.json"]))

    # Real turns on each bot, keyed by that install's RAW id — which is what
    # AL-1.3's rollup writer records ("exactly as written").
    _rollup(shared, PERSONAL, {
        V7_PERSONAL_INSTANCE: _entry(0.5000, 21, "2026-08-19T09:00:00Z")})
    _rollup(shared, TEAM, {
        V7_TEAM_INSTANCE: _entry(0.1250, 4, "2026-08-20T09:00:00Z")})

    (shared / "network.json").write_text(json.dumps({
        "sharedDir": str(shared), "members": [PERSONAL, TEAM],
    }))

    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(workspaces.get(bid, tmp_path / "none"))},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)

    app = Flask(__name__)
    register_apps_routes(app, shared / "network.json")
    app.testing = True
    return {"client": app.test_client(), "shared": shared, "tmp": tmp_path}


def test_v7_arc_raw_and_hydrated_resolve_to_different_ids(v7_pod):
    """PRECONDITION for the two tests below — not a property of the product.

    If hydration ever stops rewriting the identity, this fails loudly rather
    than letting the two join tests keep passing while silently no longer
    exercising the divergence they exist to cover.
    """
    from evolve_admin.applications.app_identity import resolve_app_id
    from evolve_admin.applications.manifest import hydrate_v7_arc_instance

    raw = _v7_instance(V7_PERSONAL_INSTANCE, ["apps/morning-brief/run.py"])
    hydrated = hydrate_v7_arc_instance(raw, v7_pod["shared"])

    assert resolve_app_id(raw) == V7_PERSONAL_INSTANCE
    assert resolve_app_id(hydrated) == V7_SPEC_ID
    assert resolve_app_id(raw) != resolve_app_id(hydrated)


def test_v7_arc_grouped_row_reports_each_bots_real_turns(v7_pod):
    """The grouped row's usage join runs on each install's RAW id."""
    row = _by_name(_apps(v7_pod), "morning brief")[0]
    assert row["bots_total"] == 2
    by_bot = {b["bot_id"]: b for b in row["bots"]}
    assert by_bot[PERSONAL]["cost_7d"] == 0.5000
    assert by_bot[TEAM]["cost_7d"] == 0.1250


def test_v7_arc_ungrouped_rows_report_real_turns(v7_pod):
    """``?grouped=0`` runs the same join through the same helper.

    ``_app_row`` is shared, so a hydrated-id join would fabricate a zero here
    too — and the ungrouped payload is exactly what D-I's withdrawability
    promise is measured against.
    """
    rows = _by_name(_apps(v7_pod, "?grouped=0"), "morning brief")
    assert len(rows) == 2
    costs = {r["bots"][0]["bot_id"]: r["bots"][0]["cost_7d"] for r in rows}
    assert costs == {PERSONAL: 0.5000, TEAM: 0.1250}
