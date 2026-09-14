"""dossier.profile — the operator's arrangement of the Pod Intelligence page.

The HTTP-level behaviour is pinned in
``packages/admin/tests/test_dossier_routes.py``. What is pinned HERE is the
store's own contract, which the route cannot express:

  * a profile written by a FUTURE schema is not guessed at,
  * an unreadable file degrades to "no preferences", never to "everything
    hidden" — the fail-safe direction for a rule whose failure mode is a
    blank page,
  * the write is atomic and lands 0644 beside the weeks it arranges,
  * and ``iter_module_ids`` — added for this page — lists what the reader
    can actually SAY something about.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from dossier import profile as prof
from dossier import store

NOW = datetime(2026, 8, 29, 17, 5, tzinfo=timezone.utc)


def test_an_untouched_pod_has_no_preferences(tmp_path: Path):
    loaded = prof.load_profile(tmp_path)
    assert loaded == {
        "schema_version": 1, "order": [], "hidden": [], "ratings": {},
        "updated_at": None,
    }


def test_save_then_load_round_trips(tmp_path: Path):
    prof.save_profile(tmp_path, {
        "order": ["cost_trajectory", "apps_leaderboard"],
        "hidden": ["users_activity"],
        "ratings": {"cost_trajectory": "useful",
                    "users_activity": "not_useful"},
    }, now=NOW)
    loaded = prof.load_profile(tmp_path)
    assert loaded["order"] == ["cost_trajectory", "apps_leaderboard"]
    assert loaded["hidden"] == ["users_activity"]
    assert loaded["ratings"] == {"cost_trajectory": "useful",
                                 "users_activity": "not_useful"}
    assert loaded["updated_at"] == "2026-08-29T17:05:00Z"


def test_the_file_lands_where_the_weeks_live_and_is_operator_readable(tmp_path: Path):
    path = prof.save_profile(tmp_path, {"order": ["apps_leaderboard"]}, now=NOW)
    assert prof.profile_path(tmp_path) == tmp_path / "dossier" / "profile.json"
    assert prof.profile_path(tmp_path).is_file()
    assert oct(prof.profile_path(tmp_path).stat().st_mode & 0o777) == "0o644"
    assert path["order"] == ["apps_leaderboard"]


def test_a_write_replaces_rather_than_merges(tmp_path: Path):
    """"Clear my ratings" has to be expressible, so an empty list means empty."""
    prof.save_profile(tmp_path, {"ratings": {"cost_trajectory": "useful"}}, now=NOW)
    prof.save_profile(tmp_path, {"ratings": {}}, now=NOW)
    assert prof.load_profile(tmp_path)["ratings"] == {}


def test_a_future_schema_is_not_guessed_at(tmp_path: Path):
    (tmp_path / "dossier").mkdir()
    prof.profile_path(tmp_path).write_text(json.dumps({
        "schema_version": 99, "order": ["apps_leaderboard"],
        "hidden": ["cost_trajectory"],
    }))
    # Reading it WRONG would hide a card on the strength of a field whose
    # meaning we do not know. Reading it as absent shows everything.
    assert prof.load_profile(tmp_path)["hidden"] == []


def test_an_unreadable_profile_fails_toward_showing_everything(tmp_path: Path):
    (tmp_path / "dossier").mkdir()
    prof.profile_path(tmp_path).write_text("{ not json")
    assert prof.load_profile(tmp_path) == prof.empty_profile()


def test_a_json_array_is_not_a_profile(tmp_path: Path):
    (tmp_path / "dossier").mkdir()
    prof.profile_path(tmp_path).write_text("[1, 2, 3]")
    assert prof.load_profile(tmp_path) == prof.empty_profile()


def test_normalise_filters_and_bounds(tmp_path: Path):
    cleaned = prof.normalise({
        "order": ["ok_one", "Bad-Id", "", 3, "ok_one", "ok_two"],
        "hidden": ["ok_one"] * 3,
        "ratings": {"ok_one": "useful", "ok_two": "maybe", 7: "useful"},
    })
    assert cleaned["order"] == ["ok_one", "ok_two"]   # deduped, filtered
    assert cleaned["hidden"] == ["ok_one"]
    assert cleaned["ratings"] == {"ok_one": "useful"}

    big = prof.normalise({"order": [f"m_{i}" for i in range(prof.MAX_ENTRIES * 3)]})
    assert len(big["order"]) == prof.MAX_ENTRIES


def test_an_id_the_server_has_never_heard_of_is_kept(tmp_path: Path):
    """Forward compatibility: a profile written by a NEWER page must survive.

    The renderer already ignores an id it has no card for, so filtering
    against today's module list here would silently delete a preference on
    every downgrade.
    """
    saved = prof.save_profile(
        tmp_path, {"order": ["a_module_from_the_future"]}, now=NOW,
    )
    assert saved["order"] == ["a_module_from_the_future"]


# ── the lister this page added to the store ────────────────────────────────


def test_iter_module_ids_lists_weeks_chronologically(tmp_path: Path):
    modules = tmp_path / "dossier" / "modules"
    modules.mkdir(parents=True)
    for name in ("2026-W35", "2026-W03", "2025-W52", "not-a-week", "README"):
        (modules / f"{name}.json").write_text("{}")
    assert store.iter_module_ids(tmp_path) == ["2025-W52", "2026-W03", "2026-W35"]


def test_iter_module_ids_is_empty_when_nothing_has_been_written(tmp_path: Path):
    assert store.iter_module_ids(tmp_path) == []
