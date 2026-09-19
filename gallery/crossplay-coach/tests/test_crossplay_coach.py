from __future__ import annotations

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = (
    Path(__file__).parents[1] / "files" / "scripts" / "crossplay_coach.py"
)
MANIFEST = Path(__file__).parents[1] / "p-be5885a2.json"
FIXTURES = Path(__file__).parent / "fixtures"
SPEC = importlib.util.spec_from_file_location("crossplay_coach", SCRIPT)
assert SPEC and SPEC.loader
coach = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coach
SPEC.loader.exec_module(coach)


def empty_board():
    return coach.parse_board(["." * 15 for _ in range(15)])


def test_premium_layout_is_symmetric_and_has_56_bonus_squares():
    premiums = coach._PREMIUM_ROWS
    assert sum(cell not in {".", "*"} for row in premiums for cell in row) == 56
    assert premiums[7][7] == "*"
    assert premiums == tuple(tuple(reversed(row)) for row in premiums)
    assert premiums == tuple(reversed(premiums))


def test_every_opening_move_covers_center():
    lexicon = coach.Lexicon(["AT", "ATE", "EAT", "TEA", "RATE", "TARE", "TEAR"])
    moves = coach.generate_moves(empty_board(), "RATE", lexicon)
    assert moves
    assert all(
        any((tile.row, tile.col) == coach.CENTER for tile in move.placed)
        for move in moves
    )


def test_existing_lowercase_tile_is_a_zero_point_blank():
    rows = ["." * 15 for _ in range(15)]
    rows[7] = "......." + "a" + "......."
    board = coach.parse_board(rows)
    lexicon = coach.Lexicon(["AT"])
    moves = coach.generate_moves(board, "T", lexicon)
    at_moves = [move for move in moves if move.word == "AT"]
    assert at_moves
    assert {move.score for move in at_moves} == {1}


def test_solver_does_not_spend_blank_when_matching_letter_is_available():
    lexicon = coach.Lexicon(["ATE"])
    moves = coach.generate_moves(empty_board(), "ATE?", lexicon)
    assert moves
    assert all(not any(tile.is_blank for tile in move.placed) for move in moves)


def test_cross_word_must_exist_in_lexicon():
    rows = ["." * 15 for _ in range(15)]
    rows[6] = "......." + "C" + "......."
    rows[8] = "......." + "T" + "......."
    board = coach.parse_board(rows)
    without_cat = coach.Lexicon(["CAR"])
    with_cat = coach.Lexicon(["CAR", "CAT"])
    invalid_moves = coach.generate_moves(board, "CAR", without_cat)
    valid_moves = coach.generate_moves(board, "CAR", with_cat)
    assert not any(
        any((tile.row, tile.col, tile.letter) == (7, 7, "A") for tile in move.placed)
        for move in invalid_moves
    )
    assert any(
        any((tile.row, tile.col, tile.letter) == (7, 7, "A") for tile in move.placed)
        for move in valid_moves
    )


def test_ranker_returns_distinct_moves_and_required_metrics():
    lexicon = coach.Lexicon(["AT", "ATE", "EAT", "TEA", "RATE", "TARE", "TEAR"])
    moves = coach.generate_moves(empty_board(), "RATE", lexicon)
    ranked = coach.rank_moves(empty_board(), moves, 5)
    keys = {
        (move.word, move.row, move.col, move.direction, move.placed)
        for move in ranked
    }
    assert len(keys) == len(ranked)
    assert 1 <= len(ranked) <= 5
    assert all(move.labels for move in ranked[:1])


def test_ranker_prefers_tile_economy_until_crossplay_final_turn():
    flexible = coach.Move(
        "SAVE",
        7,
        7,
        "Across",
        20,
        (coach.PlacedTile(7, 7, "A", False),),
        (),
        "?S",
    )
    points = coach.Move(
        "CASH",
        7,
        7,
        "Across",
        30,
        (coach.PlacedTile(7, 7, "A", False),),
        (),
        "Q",
    )
    ranked = coach.rank_moves(empty_board(), [flexible, points], bag_count=20)
    assert ranked[0].word == "SAVE"
    assert {"keeps blank", "keeps S"} <= set(ranked[0].labels)

    ranked = coach.rank_moves(empty_board(), [flexible, points], bag_count=0)
    assert ranked[0].word == "CASH"
    assert "final-turn scoring" in ranked[0].labels


def test_ranker_labels_parallel_duplicate_and_high_value_premium_tactics():
    rows = [["."] * 15 for _ in range(15)]
    rows[6][7], rows[8][7] = "C", "T"
    rows[6][8], rows[8][8] = "D", "G"
    board = coach.parse_board(["".join(row) for row in rows])
    parallel = coach.Move(
        "LO",
        7,
        7,
        "Across",
        25,
        (
            coach.PlacedTile(7, 7, "L", False),
            coach.PlacedTile(7, 8, "O", False),
        ),
        (),
        "L",
    )
    premium = coach.Move(
        "XI",
        9,
        10,
        "Across",
        24,
        (
            coach.PlacedTile(9, 10, "X", False),
            coach.PlacedTile(9, 11, "I", False),
        ),
        (coach.PlacedTile(9, 9, "A", False),),
        "L",
    )
    ranked = coach.rank_moves(board, [parallel, premium])
    by_word = {move.word: move for move in ranked}
    assert "parallel play" in by_word["LO"].labels
    assert "reduces duplicates" in by_word["LO"].labels
    assert "high-value tile premium" in by_word["XI"].labels
    assert "two-letter utility" in by_word["XI"].labels
    assert "hook or extension" in by_word["XI"].labels


def test_saved_game_keeps_text_history_without_duplicate_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    board = empty_board()
    lexicon = coach.Lexicon(["AT"])
    ranked = coach.rank_moves(board, coach.generate_moves(board, "AT", lexicon), 1)
    request = {"game_id": "alice", "bag_count": 86, "scores": {"you": 0}}
    path = coach.save_game(request, board, "AT", ranked)
    coach.save_game(request, board, "AT", ranked)
    data = json.loads(path.read_text())
    assert len(data["observations"]) == 1
    assert "image" not in path.read_text().lower()

    request["bag_count"] = 84
    changed_rows = ["." * 15 for _ in range(15)]
    changed_rows[7] = "......." + "A" + "......."
    coach.save_game(request, coach.parse_board(changed_rows), "T", ranked)
    data = json.loads(path.read_text())
    assert len(data["observations"]) == 2


def test_dense_screenshot_transcription_accepts_three_board_blanks():
    # Transcribed from the full-board fixture supplied for the MVP.
    occupied = {
        "D1": "a", "O1": "V", "D2": "I", "N2": "K", "O2": "I",
        "A3": "A", "D3": "O", "M3": "W", "N3": "I", "O3": "N",
        "A4": "M", "D4": "L", "J4": "E", "K4": "M", "L4": "B",
        "M4": "E", "N4": "D", "O4": "s", "A5": "I", "C5": "T",
        "D5": "I", "F5": "A", "J5": "Y", "L5": "A", "M5": "T",
        "A6": "N", "C6": "H", "F6": "L", "H6": "J", "I6": "O",
        "J6": "E", "L6": "I", "A7": "O", "B7": "W", "C7": "E",
        "F7": "I", "H7": "A", "I7": "Y", "L7": "T", "M7": "A",
        "N7": "G", "O7": "s", "B8": "U", "C8": "N", "D8": "C",
        "E8": "O", "F8": "V", "G8": "E", "H8": "R", "L8": "H",
        "O8": "I", "B9": "Z", "C9": "A", "E9": "F", "F9": "E",
        "G9": "L", "H9": "L", "O9": "G", "C10": "R", "D10": "E",
        "E10": "F", "O10": "N", "E11": "A", "F11": "G", "G11": "O",
        "J11": "Q", "M11": "S", "N11": "H", "O11": "E", "F12": "O",
        "G12": "D", "J12": "U", "M12": "P", "N12": "E", "O12": "R",
        "I13": "B", "J13": "O", "K13": "X", "M13": "A", "N13": "R",
        "J14": "T", "N14": "E", "J15": "A", "K15": "C", "L15": "R",
        "M15": "O", "N15": "S", "O15": "S",
    }
    rows = [["."] * 15 for _ in range(15)]
    for coordinate, letter in occupied.items():
        col = ord(coordinate[0]) - 65
        row = int(coordinate[1:]) - 1
        rows[row][col] = letter
    board = coach.parse_board(["".join(row) for row in rows])
    assert sum(cell is not None for row in board for cell in row) == 88
    assert sum(bool(cell and cell.is_blank) for row in board for cell in row) == 3
    assert coach.parse_rack("ETRDESN") == "ETRDESN"


def test_dense_regression_uses_existing_anchor_toward_triple_letter():
    fixture = Path(__file__).parent / "fixtures" / "dense-anchor-regression.json"
    _, board, rack = coach.load_request(fixture)
    lexicon = coach.Lexicon(
        [
            "AL", "YEZ", "DIVULGE", "JO", "THEORY", "ON", "CORBIES", "AXES",
            "VARY", "ADJOINED", "LION", "YET", "MOUTHS", "TRACKERS", "NIQABS",
            "AGOUTIES",
        ]
    )
    audit = coach.audit_board(board, lexicon)
    assert audit["status"] == "PASS"
    assert {word["word"] for word in audit["words"]} >= {
        "ADJOINED", "TRACKERS", "MOUTHS", "NIQABS",
    }

    ranked = coach.rank_moves(board, coach.generate_moves(board, rack, lexicon), 5)
    play = next(move for move in ranked if move.word == "AGOUTIES")
    assert play.score == 56
    assert len(play.placed) == 7
    assert [(tile.coordinate, tile.letter) for tile in play.anchors] == [("H10", "A")]
    assert any(
        tile.coordinate == "K10" and coach._PREMIUM_ROWS[tile.row][tile.col] == "3L"
        for tile in play.placed
    )


def test_audit_flags_disconnected_or_isolated_vision_transcription():
    rows = ["." * 15 for _ in range(15)]
    rows[0] = "A" + "." * 14
    rows[7] = "......." + "AT" + "......"
    result = coach.audit_board(coach.parse_board(rows), coach.Lexicon(["AT"]))
    assert result["status"] == "REVIEW"
    assert "A1" in " ".join(result["concerns"])
    assert any("disconnected" in concern for concern in result["concerns"])


def test_coordinate_words_avoid_row_math_and_find_factious_sweep():
    fixture = Path(__file__).parent / "fixtures" / "dense-anchor-followup.json"
    _, board, rack = coach.load_request(fixture)
    lexicon = coach.Lexicon(
        [
            "VARY", "AL", "YEZ", "DIVULGE", "JO", "THEORY", "ADJOINED",
            "LION", "YETI", "GIE", "TRACKERS", "MOUTHS", "CORBIES",
            "NIQABS", "AXES", "OXEN", "FACTIOUS",
        ]
    )
    assert sum(cell is not None for row in board for cell in row) == 61
    assert board[2][4] == coach.Cell("L", is_blank=True)
    ranked = coach.rank_moves(board, coach.generate_moves(board, rack, lexicon), 5)
    play = next(move for move in ranked if move.word == "FACTIOUS")
    assert (play.coordinate, play.direction, play.score) == ("G10", "Across", 56)
    assert [(tile.coordinate, tile.letter) for tile in play.anchors] == [("H10", "A")]


def test_coordinate_words_reject_conflicts_and_out_of_bounds():
    with pytest.raises(coach.CoachError, match="conflict at B1"):
        coach.parse_board_words(
            [
                {"word": "AT", "position": "A1", "direction": "across"},
                {"word": "NO", "position": "B1", "direction": "down"},
            ]
        )
    with pytest.raises(coach.CoachError, match="runs beyond the board"):
        coach.parse_board_words(
            [{"word": "TOO", "position": "N15", "direction": "across"}]
        )


def test_coordinate_words_normalize_case_and_accept_rack_list():
    board = coach.parse_board_words(
        [{"word": "at", "position": "H8", "direction": "across"}]
    )
    assert board[7][7] == coach.Cell("A", is_blank=False)
    assert coach.parse_rack(["c", "o", "f", "i", "s", "t", "u"]) == "COFISTU"


def test_audit_reports_request_correction_without_exec_failure(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "board_words": [
                    {"word": "AT", "position": "A1", "direction": "across"},
                    {"word": "NO", "position": "B1", "direction": "down"},
                ],
                "rack": ["a", "t"],
            }
        )
    )
    assert coach.main(["audit", "--request", str(request)]) == 0
    output = capsys.readouterr().out
    assert "audit: REVIEW" in output
    assert "conflict at B1" in output


def _learn_request(tmp_path, name, word, position, game_id=None):
    """A single-word board that crosses centre, so only the lexicon concern fires."""
    request = tmp_path / name
    payload = {
        "schema_version": 1,
        "board_words": [
            {"word": word, "position": position, "direction": "across"},
        ],
        "rack": "IDOLLTH",
    }
    if game_id:
        payload["game_id"] = game_id
    request.write_text(json.dumps(payload))
    return request


def test_a_single_sighting_stays_pending_and_does_not_admit_the_word(
    tmp_path, monkeypatch
):
    """One sighting is indistinguishable from a first-pass OCR error.

    The pre-fix engine wrote the word straight into the effective lexicon and
    the same board then audited PASS; it must now stay out until a second
    game re-verifies it.
    """
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    base = tmp_path / "base-lexicon.txt"
    base.write_text("AT\n")
    request = _learn_request(tmp_path, "request.json", "YEZ", "G8", "alice")

    assert coach.main(
        ["learn-board-words", "--request", str(request), "--lexicon", str(base)]
    ) == 0
    store = tmp_path / "crossplay-data" / "learned-words.jsonl"
    record = json.loads(store.read_text().strip())
    assert record["word"] == "YEZ"
    assert record["status"] == "pending"
    assert (record["game_id"], record["position"]) == ("alice", "G8")
    datetime.fromisoformat(record["learned_at"])
    assert base.read_text() == "AT\n"

    _, board, _ = coach.load_request(request)
    effective = coach._load_lexicon(base)
    assert "YEZ" not in effective.words
    assert coach.audit_board(board, effective)["status"] == "REVIEW"


def test_a_second_game_admits_the_word_and_a_repeat_of_one_game_does_not(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    base = tmp_path / "base-lexicon.txt"
    base.write_text("AT\n")
    first = _learn_request(tmp_path, "first.json", "YEZ", "G8", "alice")
    again = _learn_request(tmp_path, "again.json", "YEZ", "H8", "alice")
    second = _learn_request(tmp_path, "second.json", "YEZ", "G8", "bob")

    for request in (first, again):
        assert coach.main(
            ["learn-board-words", "--request", str(request), "--lexicon", str(base)]
        ) == 0
    # Same game twice is one re-verification, not two.
    assert "YEZ" not in coach._load_lexicon(base).words

    assert coach.main(
        ["learn-board-words", "--request", str(second), "--lexicon", str(base)]
    ) == 0
    record = json.loads(
        (tmp_path / "crossplay-data" / "learned-words.jsonl").read_text().strip()
    )
    assert record["status"] == "admitted"
    assert "YEZ" in coach._load_lexicon(base).words
    _, board, _ = coach.load_request(second)
    assert coach.audit_board(board, coach._load_lexicon(base))["status"] == "PASS"


def test_an_unnamed_game_counts_the_board_itself_so_a_retry_is_not_a_second_game(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    base = tmp_path / "base-lexicon.txt"
    base.write_text("AT\n")
    request = _learn_request(tmp_path, "request.json", "YEZ", "G8")
    for _ in range(3):
        assert coach.main(
            ["learn-board-words", "--request", str(request), "--lexicon", str(base)]
        ) == 0
    assert "YEZ" not in coach._load_lexicon(base).words

    moved = _learn_request(tmp_path, "moved.json", "YEZ", "H8")
    assert coach.main(
        ["learn-board-words", "--request", str(moved), "--lexicon", str(base)]
    ) == 0
    assert "YEZ" in coach._load_lexicon(base).words


def test_a_learned_word_round_trips_through_forget(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    base = tmp_path / "base-lexicon.txt"
    base.write_text("AT\n")
    for name, position, game in (
        ("first.json", "G8", "alice"),
        ("second.json", "G8", "bob"),
    ):
        assert coach.main([
            "learn-board-words",
            "--request", str(_learn_request(tmp_path, name, "YEZ", position, game)),
            "--lexicon", str(base),
        ]) == 0
    assert "YEZ" in coach._load_lexicon(base).words

    assert coach.main(["learned", "--list"]) == 0
    assert "YEZ — admitted" in capsys.readouterr().out

    assert coach.main(["forget", "yez"]) == 0
    assert "forgot board word: YEZ" in capsys.readouterr().out
    assert "YEZ" not in coach._load_lexicon(base).words
    assert coach.list_learned_words() == []

    assert coach.main(["forget", "YEZ"]) == 0
    assert "is not in the learned dictionary" in capsys.readouterr().out


def test_legacy_plain_text_store_migrates_once_and_is_not_re_migrated(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    base = tmp_path / "base-lexicon.txt"
    base.write_text("AT\n")
    legacy = tmp_path / "crossplay-data" / "learned-words.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("QAT\nYEZ\n")
    # Tolerated in place until the first write.
    assert {"QAT", "YEZ"} <= coach._load_lexicon(base).words

    request = _learn_request(tmp_path, "request.json", "ZOA", "G8", "alice")
    assert coach.main(
        ["learn-board-words", "--request", str(request), "--lexicon", str(base)]
    ) == 0

    assert not legacy.exists()
    assert (tmp_path / "crossplay-data" / "learned-words.txt.migrated").exists()
    by_word = {row["word"]: row for row in coach.list_learned_words()}
    assert by_word["QAT"]["source"] == "legacy"
    assert by_word["QAT"]["status"] == "admitted"
    assert by_word["ZOA"]["status"] == "pending"
    # Still admitted, and re-writing the store does not resurrect the file.
    assert {"QAT", "YEZ"} <= coach._load_lexicon(base).words
    assert coach.main(["forget", "QAT"]) == 0
    assert not legacy.exists()
    assert "QAT" not in coach._load_lexicon(base).words


def test_a_corrupt_learned_store_line_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    store = tmp_path / "crossplay-data" / "learned-words.jsonl"
    store.parent.mkdir(parents=True)
    store.write_text('{"word": "QAT"}\nnot json\n')
    base = tmp_path / "base-lexicon.txt"
    base.write_text("AT\n")
    with pytest.raises(coach.CoachError, match="line 2 is not valid JSON"):
        coach._load_lexicon(base)


def test_learning_refuses_structurally_invalid_transcription(tmp_path, monkeypatch):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    board = coach.parse_board_words(
        [
            {"word": "AT", "position": "H8", "direction": "across"},
            {"word": "YEZ", "position": "A1", "direction": "across"},
        ]
    )
    with pytest.raises(coach.CoachError, match="structurally invalid"):
        coach.learn_verified_board_words(board, coach.Lexicon(["AT"]))
    assert not (tmp_path / "crossplay-data" / "learned-words.jsonl").exists()
    assert not (tmp_path / "crossplay-data" / "learned-words.txt").exists()


def _disconnected_solve_argv(tmp_path, *extra):
    lexicon = tmp_path / "lexicon.txt"
    lexicon.write_text("\n".join(["AT", "ATE", "TEA", "EAT", "YEZ", "TA", "ET"]) + "\n")
    return [
        "solve",
        "--request", str(FIXTURES / "disconnected-board.json"),
        "--lexicon", str(lexicon),
        *extra,
    ]


def test_solve_refuses_a_board_that_failed_its_transcription_audit(
    tmp_path, monkeypatch, capsys
):
    """The engine holds the information to refuse; it must not rank anyway.

    Pre-fix, this fixture printed "Transcription audit: REVIEW" and then
    returned five scored plays with exit 0 — the switch failed OPEN and
    enforcement lived entirely in AGENTS.md prose.
    """
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    assert coach.main(_disconnected_solve_argv(tmp_path, "--format", "json")) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is True
    assert "moves" not in payload
    assert "recommendations" not in payload
    assert any(
        "disconnected" in concern
        for concern in payload["transcription_audit"]["concerns"]
    )

    assert coach.main(_disconnected_solve_argv(tmp_path)) != 0
    text = capsys.readouterr().out
    assert "solve: REFUSED" in text
    assert "REVIEW: occupied tiles are split into disconnected groups" in text


def test_force_solves_the_same_board_and_marks_every_move_unaudited(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    assert coach.main(
        _disconnected_solve_argv(tmp_path, "--force", "--format", "json")
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is False
    assert payload["forced"] is True
    assert payload["recommendations"]
    assert all(move["unaudited"] is True for move in payload["recommendations"])

    assert coach.main(_disconnected_solve_argv(tmp_path, "--force")) == 0
    streams = capsys.readouterr()
    # The warning goes to stderr so --format json keeps a parseable stdout.
    assert "--force overriding audit REVIEW" in streams.err
    assert "disconnected groups" in streams.err
    assert "Crossplay Coach —" in streams.out


def test_a_clean_board_still_solves_and_is_not_marked_unaudited(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    lexicon = tmp_path / "lexicon.txt"
    lexicon.write_text(
        "\n".join(
            [
                "AL", "YEZ", "DIVULGE", "JO", "THEORY", "ON", "CORBIES", "AXES",
                "VARY", "ADJOINED", "LION", "YET", "MOUTHS", "TRACKERS",
                "NIQABS", "AGOUTIES",
            ]
        )
        + "\n"
    )
    assert coach.main([
        "solve",
        "--request", str(FIXTURES / "dense-anchor-regression.json"),
        "--lexicon", str(lexicon),
        "--format", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is False
    assert payload["forced"] is False
    assert payload["recommendations"]
    assert not any("unaudited" in move for move in payload["recommendations"])


def test_build_spec_fail_closed_claim_matches_the_branch_that_exists(
    tmp_path, monkeypatch, capsys
):
    """The 2026-09-10 defect was a doc/code disagreement, not only a code bug.

    build_spec claimed "Solve remains fail-closed" while main() ranked anyway.
    Assert the shipped sentence and the shipped behaviour together, so neither
    can drift without this failing.
    """
    build_spec = json.loads(MANIFEST.read_text())["build_spec"]
    assert "`solve` is fail-closed" in build_spec
    assert '"refused": true' in build_spec
    assert '"unaudited": true' in build_spec
    assert "Solve remains fail-closed." not in build_spec

    monkeypatch.setattr(coach, "_workspace_root", lambda: tmp_path)
    assert coach.main(_disconnected_solve_argv(tmp_path, "--format", "json")) != 0
    refused = json.loads(capsys.readouterr().out)
    assert refused["refused"] is True

    assert coach.main(
        _disconnected_solve_argv(tmp_path, "--force", "--format", "json")
    ) == 0
    forced = json.loads(capsys.readouterr().out)
    assert all(move["unaudited"] is True for move in forced["recommendations"])


def test_manifest_declares_the_tools_the_session_instruction_uses():
    pkg = json.loads(MANIFEST.read_text())
    tools = pkg["requirements"]["tools"]
    assert [tool["id"] for tool in tools] == ["file_write", "exec", "file_delete"]
    for tool in tools:
        assert tool["display_name"] and tool["reason"]
    body = pkg["scheduled_actions"][0]["install"]["body"]
    assert "FILE-WRITE TOOL" in body
    assert "scripts/crossplay_coach solve" in body


def test_wows_followup_finds_scored_crossplay_not_rack_anagram():
    fixture = Path(__file__).parent / "fixtures" / "dense-wows-followup.json"
    _, board, rack = coach.load_request(fixture)
    lexicon = coach.Lexicon(
        [
            "VARY", "AL", "YEZ", "DIVULGE", "JO", "THEORY", "ADJOINED",
            "LION", "YETI", "GIE", "TRACKERS", "MOUTHS", "CORBIES",
            "NIQABS", "AXES", "OXEN", "FACTIOUS", "WOWS", "WO", "HILLO",
        ]
    )
    ranked = coach.rank_moves(board, coach.generate_moves(board, rack, lexicon), 5)
    play = next(move for move in ranked if move.word == "HILLO")
    assert (play.coordinate, play.direction, play.score) == ("O3", "Down", 33)
    assert [(tile.coordinate, tile.letter) for tile in play.placed] == [
        ("O3", "H"), ("O4", "I"), ("O5", "L"), ("O6", "L"), ("O7", "O"),
    ]
