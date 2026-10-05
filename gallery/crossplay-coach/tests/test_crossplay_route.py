"""The app route: one `xplay` move as prepare → one vision reading → resolve.

The plugin half (packages/plugin/src/apps/AppRoutes.ts) makes the model
calls; everything the app knows — the saved board, the diff, the audit, the
solve, the reply — is judged here, in code. The fixtures under
``fixtures/vision/`` are what the vision call returns for a screenshot: the
board, rack and scores as JSON. Three of them are one real game in sequence
(anchor-followup → FACTIOUS and WOWS played → wows-followup).
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).parents[1] / "files" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
VISION = FIXTURES / "vision"

sys.path.insert(0, str(SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("crossplay_route", SCRIPTS / "crossplay_route.py")
assert _SPEC and _SPEC.loader
route = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = route
_SPEC.loader.exec_module(route)
coach = route.coach

WORDS = [
    "AL", "YEZ", "DIVULGE", "JO", "THEORY", "ON", "CORBIES", "AXES", "VARY",
    "ADJOINED", "LION", "YET", "YETI", "MOUTHS", "TRACKERS", "NIQABS", "GIE",
    "HE", "OXEN", "WOWS", "WO", "AGOUTIES", "FACTIOUS", "HILLO",
]


def vision(name: str) -> dict:
    return json.loads((VISION / name).read_text())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    lexicon = tmp_path / "crossplay-data" / "lexicon.txt"
    lexicon.parent.mkdir(parents=True)
    lexicon.write_text("\n".join(WORDS) + "\n")
    return tmp_path


def game_file(workspace: Path, game_id: str) -> dict:
    return json.loads((workspace / "crossplay-data" / "games" / f"{game_id}.json").read_text())


def solve_full(game_id: str, name: str, **extra) -> dict:
    return route.resolve({"game_id": game_id, "mode": "full", "vision": vision(name), **extra})


# ── Parser: screenshots in, board / rack / scores out ─────────────────────

@pytest.mark.parametrize(
    ("name", "rack", "scores", "bag", "tiles", "poc_play"),
    [
        ("anchor-regression.full.json", "GSTOUEI", (218, 267), 31, 55,
         ("AGOUTIES", "H10 Across", 56)),
        ("anchor-followup.full.json", "COFISTU", (243, 289), 25, 61,
         ("FACTIOUS", "G10 Across", 56)),
        ("wows-followup.full.json", "IDOLLTH", (299, 321), 15, 71,
         ("HILLO", "O3 Down", 33)),
    ],
)
def test_each_fixture_screenshot_parses_and_solves_to_the_poc_play(
    workspace, name, rack, scores, bag, tiles, poc_play,
):
    result = solve_full("g", name)
    assert result["outcome"] == "solved"
    assert f"rack {rack}" in result["reply_head"]
    assert f"you {scores[0]}–{scores[1]}" in result["reply_head"]
    assert f"bag {bag}" in result["reply_head"]
    saved = game_file(workspace, "g")["confirmed"]
    assert saved["rack"] == rack
    assert saved["scores"] == {"you": scores[0], "opponent": scores[1]}
    assert saved["tiles_on_board"] == tiles

    # Move quality must not drop: the PoC's play is there with the same
    # placement and score, and nothing ranked first scores less than it.
    word, at, score = poc_play
    plays = {(m["word"], f"{m['position']} {m['direction']}"): m for m in result["recommendations"]}
    assert plays[(word, at)]["score"] == score
    assert result["recommendations"][0]["score"] >= score


def test_a_weaker_models_lowercase_words_and_list_rack_still_parse(workspace):
    reading = vision("anchor-regression.full.json")
    assert reading["board_words"][0]["word"].islower()
    assert isinstance(reading["rack"], list)
    assert solve_full("g", "anchor-regression.full.json")["outcome"] == "solved"


# ── Item 0: saved state first, and kept current ───────────────────────────

def test_prepare_asks_for_the_whole_board_when_nothing_is_saved(workspace):
    prepared = route.prepare("new-game")
    assert prepared["confirmed"] is None
    assert prepared["vision"]["mode"] == "full"
    assert "confirmed_words_missing" not in prepared["vision"]["json_schema"]["properties"]


def test_prepare_asks_only_for_the_delta_once_a_board_is_confirmed(workspace):
    solve_full("g", "anchor-followup.full.json")
    prepared = route.prepare("g")
    assert prepared["vision"]["mode"] == "delta"
    assert prepared["confirmed"]["scores"] == {"you": 243, "opponent": 289}
    instructions = prepared["vision"]["instructions"]
    assert "- THEORY G4 across" in instructions
    assert "- DIVULGE A3 across (blanks E3)" in instructions
    assert "confirmed_words_missing" in prepared["vision"]["json_schema"]["properties"]
    # `xplay fresh` ignores the saved board for this move.
    assert route.prepare("g", fresh=True)["vision"]["mode"] == "full"


def test_one_move_behind_is_diffed_onto_the_saved_board(workspace):
    solve_full("g", "anchor-followup.full.json")
    result = route.resolve({"game_id": "g", "mode": "delta", "vision": vision("wows-followup.delta.json")})
    assert result["outcome"] == "solved"
    assert result["lag"]["plays"] == 2
    assert "FACTIOUS G10 across" in result["reply_head"]
    assert "WOWS N7 down" in result["reply_head"]
    top = result["recommendations"][0]
    assert (top["word"], top["position"], top["direction"], top["score"]) == ("HILLO", "O3", "Down", 33)
    # The merged board is now the confirmed one: the next move diffs from it.
    confirmed = game_file(workspace, "g")["confirmed"]
    assert confirmed["source"] == "app-route:delta"
    assert confirmed["scores"] == {"you": 299, "opponent": 321}
    assert confirmed["tiles_on_board"] == 71


def test_several_moves_behind_says_so_and_asks_for_a_fresh_reading(workspace):
    solve_full("g", "anchor-regression.full.json")
    # Four plays since (GIE, OXEN, FACTIOUS, WOWS): too many to trust a diff.
    delta = vision("wows-followup.full.json")
    delta["confirmed_words_missing"] = []
    result = route.resolve({"game_id": "g", "mode": "delta", "vision": delta})
    assert result["outcome"] == "stale"
    assert result["reason"] == "several_moves_behind"
    assert result["detail"]["plays"] > route.MAX_DIFFABLE_PLAYS
    assert result["vision"]["mode"] == "full"
    # Nothing was saved from the delta reading.
    assert game_file(workspace, "g")["confirmed"]["scores"]["you"] == 218

    fresh = solve_full("g", "wows-followup.full.json", after_stale=result["detail"] | {"reason": result["reason"]})
    assert fresh["outcome"] == "solved"
    assert "read whole" in fresh["reply_head"] and "plays behind" in fresh["reply_head"]
    assert game_file(workspace, "g")["confirmed"]["scores"]["you"] == 299


def test_a_delta_that_misses_a_play_the_scores_show_is_read_again_whole(workspace):
    solve_full("g", "anchor-followup.full.json")
    delta = vision("wows-followup.delta.json")
    delta["board_words"] = delta["board_words"][:1]   # WOWS missed
    result = route.resolve({"game_id": "g", "mode": "delta", "vision": delta})
    assert result["outcome"] == "stale"
    assert result["reason"] == "delta_incomplete"


def test_a_delta_letter_that_contradicts_the_saved_board_is_refused_by_name(workspace):
    solve_full("g", "anchor-followup.full.json")
    delta = vision("wows-followup.delta.json")
    # FACTIOUS runs through the saved A at H10; read it as FOCTIOUS.
    delta["board_words"][0]["word"] = "FOCTIOUS"
    before = game_file(workspace, "g")["confirmed"]
    result = route.resolve({"game_id": "g", "mode": "delta", "vision": delta})
    assert result["outcome"] == "refused"
    assert result["code"] == "conflict"
    assert "H10 (saved A, screenshot O)" in result["reply"]
    assert game_file(workspace, "g")["confirmed"] == before


def test_a_whole_reading_that_drops_a_saved_tile_is_refused_by_name(workspace):
    solve_full("g", "anchor-followup.full.json")
    reading = vision("anchor-followup.full.json")
    reading["board_words"] = [w for w in reading["board_words"] if w["word"] != "OXEN"]
    reading["board_words"].append({"word": "AXES", "position": "A12", "direction": "across"})
    result = route.resolve({"game_id": "g", "mode": "full", "vision": reading})
    assert result["outcome"] == "refused"
    assert result["code"] == "conflict"
    assert "B11 (saved O, now empty)" in result["reply"]
    assert "xplay fresh" in result["reply"]


def test_a_score_that_went_down_means_a_different_game(workspace):
    solve_full("g", "anchor-followup.full.json")
    delta = vision("wows-followup.delta.json")
    delta["scores"] = {"you": 12, "opponent": 9}
    result = route.resolve({"game_id": "g", "mode": "delta", "vision": delta})
    assert result["code"] == "conflict"
    assert "went down" in result["reply"]


def test_confirmed_words_the_screenshot_no_longer_shows_are_refused(workspace):
    solve_full("g", "anchor-followup.full.json")
    delta = vision("wows-followup.delta.json")
    delta["confirmed_words_missing"] = ["THEORY"]
    result = route.resolve({"game_id": "g", "mode": "delta", "vision": delta})
    assert result["code"] == "conflict"
    assert "THEORY" in result["reply"]


def test_a_legacy_game_file_is_read_as_its_last_solve(workspace):
    games = workspace / "crossplay-data" / "games"
    games.mkdir(parents=True)
    _, board, _ = coach.load_request(FIXTURES / "dense-anchor-followup.json")
    (games / "old.json").write_text(json.dumps({
        "schema_version": 1, "game_id": "old", "observed_at": "2026-09-14T00:00:00+00:00",
        "board": coach.board_rows(board), "rack": "COFISTU", "bag_count": 25,
        "scores": {"you": 243, "opponent": 289}, "observations": [],
    }))
    confirmed = coach.load_confirmed("old")
    assert confirmed["source"] == "solve-save"
    assert route.prepare("old")["vision"]["mode"] == "delta"


# ── Refusals end the move; they never loop ────────────────────────────────

def test_uncertain_cells_are_asked_about_not_guessed(workspace):
    reading = vision("anchor-followup.full.json")
    reading["uncertain"] = ["H11"]
    result = route.resolve({"game_id": "g", "mode": "full", "vision": reading})
    assert result["code"] == "uncertain"
    assert "H11" in result["reply"]
    assert not (workspace / "crossplay-data" / "games" / "g.json").exists()


def test_a_short_rack_with_tiles_left_in_the_bag_is_inconsistent(workspace):
    reading = vision("anchor-followup.full.json")
    reading["rack"] = "COFIST"
    result = route.resolve({"game_id": "g", "mode": "full", "vision": reading})
    assert result["code"] == "inconsistent"
    assert "must hold 7" in result["reply"]


def test_a_structurally_broken_board_fails_its_one_audit_and_saves_nothing(workspace):
    reading = vision("anchor-followup.full.json")
    reading["board_words"].append({"word": "ON", "position": "N14", "direction": "across"})
    result = route.resolve({"game_id": "g", "mode": "full", "vision": reading})
    assert result["code"] == "audit"
    assert "disconnected" in result["reply"]
    assert not (workspace / "crossplay-data" / "games" / "g.json").exists()


def test_an_unknown_board_word_is_noted_pending_and_the_move_stops(workspace):
    lexicon = workspace / "crossplay-data" / "lexicon.txt"
    lexicon.write_text("\n".join(w for w in WORDS if w != "OXEN") + "\n")
    result = solve_full("g", "anchor-followup.full.json")
    assert result["code"] == "lexicon"
    assert "OXEN" in result["reply"]
    learned = (workspace / "crossplay-data" / "learned-words.jsonl").read_text()
    assert '"word": "OXEN"' in learned and '"status": "pending"' in learned


def test_cli_resolve_speaks_json_on_stdout(workspace, monkeypatch, capsys):
    request = {"game_id": "cli", "mode": "full", "vision": vision("anchor-followup.full.json")}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(request)))
    assert route.main(["resolve"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "solved"

    request["vision"] = copy.deepcopy(request["vision"]) | {"uncertain": ["A1"]}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(request)))
    assert route.main(["resolve"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "refused"


def test_cli_without_a_lexicon_is_an_install_error_not_a_guess(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    monkeypatch.delenv("CROSSPLAY_LEXICON", raising=False)
    request = {"game_id": "x", "mode": "full", "vision": vision("anchor-followup.full.json")}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(request)))
    assert route.main(["resolve"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "error"
    assert "no lexicon configured" in payload["reply"]


# ── The commentary request is bounded and names only solver plays ─────────

def test_commentary_request_is_small_toolless_and_limited_to_solver_words(workspace):
    result = solve_full("g", "wows-followup.full.json")
    request = result["commentary"]
    assert set(request) == {"system", "user", "max_tokens", "allowed_words"}
    assert request["max_tokens"] <= 200
    assert request["allowed_words"] == [m["word"] for m in result["recommendations"]]
    assert "never add, change or re-score a play" in request["system"]
    assert len(request["user"]) < 2000
    assert result["default_call"].startswith("Call: HILLO at O3 Down for 33")
    assert "NWL2023" in result["reply_tail"]


def test_the_lexicon_version_note_is_a_versioned_data_file():
    declared = json.loads((SCRIPTS / "crossplay_lexicon.json").read_text())
    assert declared["schema_version"] == 1
    assert declared["reference"] == "NWL2023"
    assert declared["version"]
    assert "operator supplies" in declared["note"]
