#!/usr/bin/env python3
"""Crossplay Coach: deterministic move generation and strategic ranking.

The bot's vision model transcribes a screenshot to the request JSON consumed
here. This script never reads, copies, or stores screenshots.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Iterable, Optional


BOARD_SIZE = 15
CENTER = (7, 7)
SWEEP_BONUS = 40
# A learned board word enters the effective lexicon only after it has been
# seen on a clean-auditing board in this many DIFFERENT games. One sighting
# is indistinguishable from a first-pass OCR error, so it stays pending.
LEARNED_ADMISSION_GAMES = 2
LETTER_POINTS = {
    "A": 1, "B": 4, "C": 3, "D": 2, "E": 1, "F": 4, "G": 4,
    "H": 3, "I": 1, "J": 10, "K": 6, "L": 2, "M": 3, "N": 1,
    "O": 1, "P": 3, "Q": 10, "R": 1, "S": 1, "T": 1, "U": 2,
    "V": 6, "W": 5, "X": 8, "Y": 4, "Z": 10,
}

# Rows 1-15, columns A-O. "." is ordinary and "*" is the start square.
_PREMIUM_ROWS = (
    ("3L", ".", ".", "3W", ".", ".", ".", "2L", ".", ".", ".", "3W", ".", ".", "3L"),
    (".", "2W", ".", ".", ".", ".", "3L", ".", "3L", ".", ".", ".", ".", "2W", "."),
    (".", ".", ".", ".", "2L", ".", ".", ".", ".", ".", "2L", ".", ".", ".", "."),
    ("3W", ".", ".", "2L", ".", ".", ".", "2W", ".", ".", ".", "2L", ".", ".", "3W"),
    (".", ".", "2L", ".", ".", "3L", ".", ".", ".", "3L", ".", ".", "2L", ".", "."),
    (".", ".", ".", ".", "3L", ".", ".", "2L", ".", ".", "3L", ".", ".", ".", "."),
    (".", "3L", ".", ".", ".", ".", ".", ".", ".", ".", ".", ".", ".", "3L", "."),
    ("2L", ".", ".", "2W", ".", "2L", ".", "*", ".", "2L", ".", "2W", ".", ".", "2L"),
    (".", "3L", ".", ".", ".", ".", ".", ".", ".", ".", ".", ".", ".", "3L", "."),
    (".", ".", ".", ".", "3L", ".", ".", "2L", ".", ".", "3L", ".", ".", ".", "."),
    (".", ".", "2L", ".", ".", "3L", ".", ".", ".", "3L", ".", ".", "2L", ".", "."),
    ("3W", ".", ".", "2L", ".", ".", ".", "2W", ".", ".", ".", "2L", ".", ".", "3W"),
    (".", ".", ".", ".", "2L", ".", ".", ".", ".", ".", "2L", ".", ".", ".", "."),
    (".", "2W", ".", ".", ".", ".", "3L", ".", "3L", ".", ".", ".", ".", "2W", "."),
    ("3L", ".", ".", "3W", ".", ".", ".", "2L", ".", ".", ".", "3W", ".", ".", "3L"),
)


class CoachError(ValueError):
    """Operator-legible input or configuration error."""


@dataclass(frozen=True)
class Cell:
    letter: str
    is_blank: bool = False


@dataclass(frozen=True)
class PlacedTile:
    row: int
    col: int
    letter: str
    is_blank: bool

    @property
    def coordinate(self) -> str:
        return f"{chr(65 + self.col)}{self.row + 1}"


@dataclass
class Move:
    word: str
    row: int
    col: int
    direction: str
    score: int
    placed: tuple[PlacedTile, ...]
    anchors: tuple[PlacedTile, ...]
    leave: str
    leave_value: float = 0.0
    defense: float = 0.0
    efficiency: float = 0.0
    equity: float = 0.0
    labels: tuple[str, ...] = ()

    @property
    def coordinate(self) -> str:
        return f"{chr(65 + self.col)}{self.row + 1}"

    def as_dict(self, unaudited: bool = False) -> dict:
        payload = {
            "word": self.word,
            "position": self.coordinate,
            "direction": self.direction,
            "score": self.score,
            "placed_tiles": [
                {
                    "coordinate": tile.coordinate,
                    "letter": tile.letter,
                    "blank": tile.is_blank,
                }
                for tile in self.placed
            ],
            "existing_tiles_used": [
                {
                    "coordinate": tile.coordinate,
                    "letter": tile.letter,
                    "blank": tile.is_blank,
                }
                for tile in self.anchors
            ],
            "premiums_used": [
                {
                    "coordinate": tile.coordinate,
                    "premium": _PREMIUM_ROWS[tile.row][tile.col],
                }
                for tile in self.placed
                if _PREMIUM_ROWS[tile.row][tile.col] not in {".", "*"}
            ],
            "leave": self.leave or "(empty)",
            "metrics": {
                "equity": round(self.equity, 2),
                "leave": round(self.leave_value, 2),
                "defense": round(self.defense, 2),
                "efficiency": round(self.efficiency, 2),
            },
            "labels": list(self.labels),
        }
        if unaudited:
            # Only set on a --force solve: the board this move was generated
            # from did not pass the transcription audit.
            payload["unaudited"] = True
        return payload


class TrieNode:
    __slots__ = ("children", "terminal")

    def __init__(self) -> None:
        self.children: dict[str, TrieNode] = {}
        self.terminal = False


class Lexicon:
    def __init__(self, words: Iterable[str]) -> None:
        self.root = TrieNode()
        self.words: set[str] = set()
        for raw in words:
            word = raw.strip().upper()
            if not (2 <= len(word) <= BOARD_SIZE and word.isascii() and word.isalpha()):
                continue
            if word in self.words:
                continue
            self.words.add(word)
            node = self.root
            for letter in word:
                node = node.children.setdefault(letter, TrieNode())
            node.terminal = True

    @classmethod
    def from_path(cls, path: Path) -> "Lexicon":
        try:
            with path.open("r", encoding="utf-8", errors="strict") as handle:
                lexicon = cls(handle)
        except (OSError, UnicodeError) as exc:
            raise CoachError(f"could not read lexicon {path}: {exc}") from exc
        if not lexicon.words:
            raise CoachError(f"lexicon {path} contains no usable words")
        return lexicon


Board = tuple[tuple[Optional[Cell], ...], ...]


def _workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_config() -> dict:
    path = _workspace_root() / "config" / "crossplay.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoachError(f"invalid config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CoachError(f"config {path} must contain a JSON object")
    return data


def _resolve_lexicon(explicit: str | None) -> Path:
    configured = explicit or os.environ.get("CROSSPLAY_LEXICON")
    if not configured:
        configured = str(_load_config().get("lexicon_path") or "")
    if not configured:
        bundled_location = _workspace_root() / "crossplay-data" / "lexicon.txt"
        if bundled_location.is_file():
            return bundled_location
        raise CoachError(
            "no lexicon configured; pass --lexicon PATH, set CROSSPLAY_LEXICON, "
            "or place a word list at crossplay-data/lexicon.txt"
        )
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = _workspace_root() / path
    return path


def _learned_words_path() -> Path:
    """The learned-word store: one JSON record per line, keyed by word."""
    return _workspace_root() / "crossplay-data" / "learned-words.jsonl"


def _legacy_learned_words_path() -> Path:
    """The pre-2026.09.11 plain-text store, tolerated for one release."""
    return _workspace_root() / "crossplay-data" / "learned-words.txt"


def _retired_legacy_learned_words_path() -> Path:
    return _workspace_root() / "crossplay-data" / "learned-words.txt.migrated"


def _normalize_learnable(word: str) -> str:
    """Uppercase a candidate word, or return "" when it is not storable."""
    candidate = word.strip().upper()
    if not (
        2 <= len(candidate) <= BOARD_SIZE
        and candidate.isascii()
        and candidate.isalpha()
    ):
        return ""
    return candidate


def _record_games(record: dict) -> list[str]:
    """Distinct game keys this word has been sighted in, in first-seen order."""
    games: list[str] = []
    for sighting in record.get("sightings") or []:
        if not isinstance(sighting, dict):
            continue
        key = str(sighting.get("game_id") or "")
        if key and key not in games:
            games.append(key)
    return games


def _record_status(record: dict) -> str:
    """Derived, never trusted from the file — a hand-edited ``status`` field
    must not be able to admit a word the sighting history does not support.

    ``source: "legacy"`` records are grandfathered: they were already in the
    effective lexicon under the plain-text store, and the migration must not
    silently narrow the dictionary mid-release.
    """
    if record.get("source") == "legacy":
        return "admitted"
    if len(_record_games(record)) >= LEARNED_ADMISSION_GAMES:
        return "admitted"
    return "pending"


def _read_learned_records() -> list[dict]:
    path = _learned_words_path()
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise CoachError(f"could not read learned dictionary {path}: {exc}") from exc
    records: list[dict] = []
    seen: set[str] = set()
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CoachError(
                f"learned dictionary {path} line {number} is not valid JSON: "
                f"{exc}; repair or remove the line"
            ) from exc
        if not isinstance(data, dict):
            raise CoachError(
                f"learned dictionary {path} line {number} is not a JSON object"
            )
        word = _normalize_learnable(str(data.get("word") or ""))
        if not word:
            raise CoachError(
                f"learned dictionary {path} line {number} has no usable "
                f"'word' field"
            )
        if word in seen:
            continue
        seen.add(word)
        data["word"] = word
        records.append(data)
    return records


def _migrate_legacy_learned_words(records: list[dict]) -> list[str]:
    """Fold a pre-2026.09.11 plain-text store into ``records`` (in place).

    Returns the words added. The legacy file is retired by
    ``_commit_learned_store`` once the JSON-lines store is safely on disk.
    """
    legacy = _legacy_learned_words_path()
    if not legacy.is_file():
        return []
    try:
        lines = legacy.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CoachError(f"could not read learned dictionary {legacy}: {exc}") from exc
    known = {record["word"] for record in records}
    stamp = datetime.now(timezone.utc).isoformat()
    added: list[str] = []
    for line in lines:
        word = _normalize_learnable(line)
        if not word or word in known:
            continue
        known.add(word)
        added.append(word)
        records.append(
            {
                "word": word,
                "game_id": None,
                "position": None,
                "direction": None,
                "learned_at": stamp,
                "source": "legacy",
                "sightings": [],
            }
        )
    return sorted(added)


def _open_learned_store() -> tuple[list[dict], list[str]]:
    records = _read_learned_records()
    return records, _migrate_legacy_learned_words(records)


def _commit_learned_store(records: list[dict]) -> Path:
    path = _learned_words_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in sorted(records, key=lambda item: item["word"]):
        record["status"] = _record_status(record)
        lines.append(json.dumps(record, sort_keys=True))
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            ("\n".join(lines) + "\n") if lines else "", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise CoachError(f"could not update learned dictionary {path}: {exc}") from exc
    legacy = _legacy_learned_words_path()
    if legacy.is_file():
        # Migrated exactly once: the retired name is read by nothing, so a
        # later run neither re-migrates it nor merges it into the lexicon.
        try:
            os.replace(legacy, _retired_legacy_learned_words_path())
        except OSError as exc:
            raise CoachError(
                f"could not retire the legacy learned dictionary {legacy}: {exc}"
            ) from exc
    return path


def _admitted_learned_words() -> list[str]:
    return [
        record["word"]
        for record in _read_learned_records()
        if _record_status(record) == "admitted"
    ]


def list_learned_words() -> list[dict]:
    """Every stored word with its derived status, for ``learned --list``."""
    records, _ = _open_learned_store()
    rows = [
        {
            "word": record["word"],
            "status": _record_status(record),
            "games": _record_games(record),
            "source": record.get("source") or "board",
            "learned_at": record.get("learned_at"),
            "position": record.get("position"),
            "direction": record.get("direction"),
        }
        for record in records
    ]
    rows.sort(key=lambda row: row["word"])
    return rows


def forget_learned_word(word: str) -> tuple[Path, bool]:
    """Remove a word from the learned store so later solves stop using it."""
    target = _normalize_learnable(word)
    if not target:
        raise CoachError(
            f"{word!r} is not a storable word; pass 2-15 ASCII letters"
        )
    records, _ = _open_learned_store()
    kept = [record for record in records if record["word"] != target]
    removed = len(kept) != len(records)
    if not removed and not _legacy_learned_words_path().is_file():
        return _learned_words_path(), False
    return _commit_learned_store(kept), removed


def _load_lexicon(path: Path) -> Lexicon:
    try:
        words = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CoachError(f"could not read lexicon data: {exc}") from exc
    words.extend(_admitted_learned_words())
    legacy = _legacy_learned_words_path()
    if legacy.is_file():
        try:
            words.extend(legacy.read_text(encoding="utf-8", errors="strict").splitlines())
        except (OSError, UnicodeError) as exc:
            raise CoachError(f"could not read lexicon data: {exc}") from exc
    lexicon = Lexicon(words)
    if not lexicon.words:
        raise CoachError(f"lexicon {path} contains no usable words")
    return lexicon


def parse_board(rows: object) -> Board:
    if not isinstance(rows, list) or len(rows) != BOARD_SIZE:
        raise CoachError("board must be a list of exactly 15 row strings")
    board: list[tuple[Cell | None, ...]] = []
    for index, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, str) or len(raw_row) != BOARD_SIZE:
            raise CoachError(f"board row {index} must be a 15-character string")
        cells: list[Cell | None] = []
        for value in raw_row:
            if value == ".":
                cells.append(None)
            elif value.isascii() and value.isalpha():
                cells.append(Cell(value.upper(), is_blank=value.islower()))
            else:
                raise CoachError(
                    f"board row {index} contains invalid cell {value!r}; "
                    "use '.', uppercase letters, or lowercase letters for blanks"
                )
        board.append(tuple(cells))
    return tuple(board)


def parse_board_words(items: object) -> Board:
    """Build a board from visible words and coordinates, merging intersections."""
    if not isinstance(items, list) or not items:
        raise CoachError("board_words must be a non-empty list")
    cells: list[list[Optional[Cell]]] = [
        [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise CoachError(f"board_words[{index}] must be an object")
        raw_word = str(item.get("word") or "").strip()
        if (
            not 2 <= len(raw_word) <= BOARD_SIZE
            or not raw_word.isascii()
            or not raw_word.isalpha()
        ):
            raise CoachError(
                f"board_words[{index}].word must contain 2-15 ASCII letters"
            )
        position = str(item.get("position") or "").strip().upper()
        match = re.fullmatch(r"([A-O])(1[0-5]|[1-9])", position)
        if not match:
            raise CoachError(
                f"board_words[{index}].position must use A1-O15 coordinates"
            )
        col = ord(match.group(1)) - 65
        row = int(match.group(2)) - 1
        direction = str(item.get("direction") or "").strip().lower()
        if direction not in {"across", "down"}:
            raise CoachError(
                f"board_words[{index}].direction must be 'across' or 'down'"
            )
        dr, dc = ((0, 1) if direction == "across" else (1, 0))
        end_row = row + dr * (len(raw_word) - 1)
        end_col = col + dc * (len(raw_word) - 1)
        if end_row >= BOARD_SIZE or end_col >= BOARD_SIZE:
            raise CoachError(
                f"board_words[{index}] {raw_word.upper()} at {position} "
                f"{direction} runs beyond the board"
            )
        raw_blanks = item.get("blanks", [])
        if not isinstance(raw_blanks, list) or not all(
            isinstance(value, str) for value in raw_blanks
        ):
            raise CoachError(f"board_words[{index}].blanks must be a coordinate list")
        blanks = {value.strip().upper() for value in raw_blanks}
        word_coordinates = {
            f"{chr(65 + col + dc * offset)}{row + dr * offset + 1}"
            for offset in range(len(raw_word))
        }
        outside = sorted(blanks - word_coordinates)
        if outside:
            raise CoachError(
                f"board_words[{index}].blanks contains {outside[0]}, "
                "which is not part of that word"
            )
        for offset, raw_letter in enumerate(raw_word):
            r, c = row + dr * offset, col + dc * offset
            coordinate = f"{chr(65 + c)}{r + 1}"
            incoming = Cell(raw_letter.upper(), coordinate in blanks)
            existing = cells[r][c]
            if existing is not None and existing.letter != incoming.letter:
                raise CoachError(
                    f"board_words conflict at {coordinate}: "
                    f"{existing.letter} vs {incoming.letter}"
                )
            cells[r][c] = Cell(
                incoming.letter,
                incoming.is_blank or bool(existing and existing.is_blank),
            )
    return tuple(tuple(row) for row in cells)


def parse_rack(value: object) -> str:
    if isinstance(value, list) and all(isinstance(tile, str) for tile in value):
        value = "".join(value)
    if not isinstance(value, str):
        raise CoachError("rack must be a string or list of tile strings")
    rack = value.replace(" ", "").upper().replace("*", "?")
    if not (1 <= len(rack) <= 7):
        raise CoachError("rack must contain between 1 and 7 tiles")
    bad = [letter for letter in rack if letter != "?" and letter not in LETTER_POINTS]
    if bad:
        raise CoachError(f"rack contains invalid tile {bad[0]!r}")
    return rack


def load_request(path: Path) -> tuple[dict, Board, str]:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoachError(f"could not read request {path}: {exc}") from exc
    if not isinstance(request, dict):
        raise CoachError("request must contain a JSON object")
    has_rows = "board" in request
    has_words = "board_words" in request
    if has_rows == has_words:
        raise CoachError("request must contain exactly one of board or board_words")
    board = (
        parse_board(request["board"])
        if has_rows
        else parse_board_words(request["board_words"])
    )
    rack = parse_rack(request.get("rack"))
    return request, board, rack


def _premium_multipliers(row: int, col: int) -> tuple[int, int]:
    premium = _PREMIUM_ROWS[row][col]
    if premium == "2L":
        return 2, 1
    if premium == "3L":
        return 3, 1
    if premium == "2W":
        return 1, 2
    if premium == "3W":
        return 1, 3
    return 1, 1


def _coords(line: int, pos: int, direction: str) -> tuple[int, int]:
    return (line, pos) if direction == "Across" else (pos, line)


def _cross_word(
    board: Board,
    row: int,
    col: int,
    letter: str,
    direction: str,
    lexicon: Lexicon,
) -> tuple[bool, int, bool]:
    # Perpendicular to the main word.
    dr, dc = ((1, 0) if direction == "Across" else (0, 1))
    before: list[Cell] = []
    r, c = row - dr, col - dc
    while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c]:
        before.append(board[r][c])  # type: ignore[arg-type]
        r, c = r - dr, c - dc
    before.reverse()
    after: list[Cell] = []
    r, c = row + dr, col + dc
    while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c]:
        after.append(board[r][c])  # type: ignore[arg-type]
        r, c = r + dr, c + dc
    if not before and not after:
        return True, 0, False
    word = "".join(cell.letter for cell in before) + letter
    word += "".join(cell.letter for cell in after)
    if word not in lexicon.words:
        return False, 0, True
    letter_multiplier, word_multiplier = _premium_multipliers(row, col)
    score = sum(0 if cell.is_blank else LETTER_POINTS[cell.letter] for cell in before)
    score += LETTER_POINTS[letter] * letter_multiplier
    score += sum(0 if cell.is_blank else LETTER_POINTS[cell.letter] for cell in after)
    return True, score * word_multiplier, True


def _board_has_tiles(board: Board) -> bool:
    return any(cell is not None for row in board for cell in row)


def audit_board(board: Board, lexicon: Lexicon) -> dict:
    """Cross-check a vision transcription using board topology and words."""
    words: list[dict] = []
    covered: set[tuple[int, int]] = set()
    for direction, dr, dc in (("Across", 0, 1), ("Down", 1, 0)):
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if board[row][col] is None:
                    continue
                before_row, before_col = row - dr, col - dc
                if (
                    0 <= before_row < BOARD_SIZE
                    and 0 <= before_col < BOARD_SIZE
                    and board[before_row][before_col] is not None
                ):
                    continue
                cells: list[tuple[int, int, Cell]] = []
                r, c = row, col
                while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c]:
                    cells.append((r, c, board[r][c]))  # type: ignore[arg-type]
                    r, c = r + dr, c + dc
                if len(cells) < 2:
                    continue
                word = "".join(cell.letter for _, _, cell in cells)
                covered.update((r, c) for r, c, _ in cells)
                words.append(
                    {
                        "word": word,
                        "position": f"{chr(65 + col)}{row + 1}",
                        "direction": direction,
                        "accepted": word in lexicon.words,
                    }
                )

    occupied = {
        (row, col)
        for row in range(BOARD_SIZE)
        for col in range(BOARD_SIZE)
        if board[row][col] is not None
    }
    connected = True
    if occupied:
        seen: set[tuple[int, int]] = set()
        stack = [next(iter(occupied))]
        while stack:
            cell = stack.pop()
            if cell in seen:
                continue
            seen.add(cell)
            row, col = cell
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                neighbor = row + dr, col + dc
                if neighbor in occupied and neighbor not in seen:
                    stack.append(neighbor)
        connected = seen == occupied
    unknown = [word for word in words if not word["accepted"]]
    isolated = [
        f"{chr(65 + col)}{row + 1}"
        for row, col in sorted(occupied - covered)
    ]
    concerns: list[str] = []
    if occupied and CENTER not in occupied:
        concerns.append("the occupied board does not include center H8")
    if not connected:
        concerns.append("occupied tiles are split into disconnected groups")
    if isolated:
        concerns.append("isolated occupied cells: " + ", ".join(isolated))
    if unknown:
        concerns.append(
            "words absent from this lexicon: "
            + ", ".join(
                f"{item['word']} ({item['position']} {item['direction']})"
                for item in unknown
            )
        )
    return {
        "status": "PASS" if not concerns else "REVIEW",
        "occupied_cells": len(occupied),
        "words": words,
        "unknown_words": [item["word"] for item in unknown],
        "concerns": concerns,
    }


def _board_signature(board: Board) -> str:
    text = "".join(
        "." if cell is None
        else cell.letter.lower() if cell.is_blank
        else cell.letter
        for row in board
        for cell in row
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def sighting_game_key(request: dict, board: Board) -> str:
    """Identify the game a sighting belongs to.

    ``game_id`` when the request names one, so two turns of the same game
    count once. Otherwise the board itself, so re-running the SAME screenshot
    never manufactures a second independent sighting while a genuinely
    different position does.
    """
    raw = str(request.get("game_id") or "").strip()
    if not raw:
        return f"board:{_board_signature(board)}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", raw):
        raise CoachError("game_id must use 1-64 letters, numbers, '.', '_' or '-'")
    return raw


def learn_verified_board_words(
    board: Board, lexicon: Lexicon, game_key: str | None = None
) -> tuple[Path, list[dict]]:
    """Record unknown words from a structurally valid, re-verified board.

    A sighting is not an admission. A word enters the effective lexicon only
    after ``LEARNED_ADMISSION_GAMES`` distinct games have shown it on a board
    whose audit was otherwise clean; until then it is stored ``pending`` and
    ``_load_lexicon`` ignores it. Every record is reversible via ``forget``.
    """
    audit = audit_board(board, lexicon)
    structural_concerns = [
        concern
        for concern in audit["concerns"]
        if not concern.startswith("words absent from this lexicon:")
    ]
    if structural_concerns:
        raise CoachError(
            "refusing to learn from a structurally invalid board: "
            + "; ".join(structural_concerns)
        )
    if game_key is None:
        game_key = f"board:{_board_signature(board)}"
    records, migrated = _open_learned_store()
    by_word = {record["word"]: record for record in records}
    stamp = datetime.now(timezone.utc).isoformat()
    touched: list[dict] = []
    for item in audit["words"]:
        if item["accepted"]:
            continue
        word = _normalize_learnable(item["word"])
        if not word:
            continue
        sighting = {
            "game_id": game_key,
            "position": item["position"],
            "direction": item["direction"],
            "seen_at": stamp,
        }
        record = by_word.get(word)
        if record is None:
            record = {
                "word": word,
                "game_id": game_key,
                "position": item["position"],
                "direction": item["direction"],
                "learned_at": stamp,
                "source": "board",
                "sightings": [sighting],
            }
            records.append(record)
            by_word[word] = record
            touched.append(record)
        elif game_key not in _record_games(record):
            record.setdefault("sightings", []).append(sighting)
            touched.append(record)
    if not touched and not migrated:
        return _learned_words_path(), []
    path = _commit_learned_store(records)
    return path, [
        {
            "word": record["word"],
            "status": _record_status(record),
            "games": len(_record_games(record)),
        }
        for record in sorted(touched, key=lambda item: item["word"])
    ]


def _remaining_rack(counter: Counter[str]) -> str:
    order = "?ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(letter * counter[letter] for letter in order)


def generate_moves(board: Board, rack: str, lexicon: Lexicon) -> list[Move]:
    rack_counter = Counter(rack)
    board_started = _board_has_tiles(board)
    moves: dict[tuple, Move] = {}

    for direction in ("Across", "Down"):
        for line in range(BOARD_SIZE):
            for start in range(BOARD_SIZE):
                row, col = _coords(line, start, direction)
                if start > 0:
                    prev_row, prev_col = _coords(line, start - 1, direction)
                    if board[prev_row][prev_col] is not None:
                        continue

                def walk(
                    pos: int,
                    node: TrieNode,
                    available: Counter[str],
                    letters: str,
                    placed: tuple[PlacedTile, ...],
                    anchors: tuple[PlacedTile, ...],
                    main_sum: int,
                    main_multiplier: int,
                    cross_score: int,
                    connected: bool,
                    covers_center: bool,
                ) -> None:
                    if node.terminal and placed:
                        next_empty = pos >= BOARD_SIZE
                        if not next_empty:
                            nr, nc = _coords(line, pos, direction)
                            next_empty = board[nr][nc] is None
                        legal_connection = connected if board_started else covers_center
                        if next_empty and legal_connection:
                            score = main_sum * main_multiplier + cross_score
                            if len(placed) == 7:
                                score += SWEEP_BONUS
                            leave = _remaining_rack(available)
                            key = (
                                letters,
                                row,
                                col,
                                direction,
                                tuple((p.row, p.col, p.letter, p.is_blank) for p in placed),
                            )
                            moves[key] = Move(
                                word=letters,
                                row=row,
                                col=col,
                                direction=direction,
                                score=score,
                                placed=placed,
                                anchors=anchors,
                                leave=leave,
                            )
                    if pos >= BOARD_SIZE:
                        return
                    current_row, current_col = _coords(line, pos, direction)
                    existing = board[current_row][current_col]
                    if existing is not None:
                        child = node.children.get(existing.letter)
                        if child is None:
                            return
                        value = 0 if existing.is_blank else LETTER_POINTS[existing.letter]
                        walk(
                            pos + 1,
                            child,
                            available,
                            letters + existing.letter,
                            placed,
                            anchors + (
                                PlacedTile(
                                    current_row,
                                    current_col,
                                    existing.letter,
                                    existing.is_blank,
                                ),
                            ),
                            main_sum + value,
                            main_multiplier,
                            cross_score,
                            True,
                            covers_center,
                        )
                        return

                    for letter, child in node.children.items():
                        source: str | None = None
                        is_blank = False
                        if available[letter]:
                            source = letter
                        elif available["?"]:
                            source = "?"
                            is_blank = True
                        if source is None:
                            continue
                        valid_cross, added_cross_score, formed_cross = _cross_word(
                            board, current_row, current_col, letter, direction, lexicon
                        )
                        if not valid_cross:
                            continue
                        letter_multiplier, word_multiplier = _premium_multipliers(
                            current_row, current_col
                        )
                        tile_value = 0 if is_blank else LETTER_POINTS[letter]
                        available[source] -= 1
                        tile = PlacedTile(current_row, current_col, letter, is_blank)
                        walk(
                            pos + 1,
                            child,
                            available,
                            letters + letter,
                            placed + (tile,),
                            anchors,
                            main_sum + tile_value * letter_multiplier,
                            main_multiplier * word_multiplier,
                            cross_score + added_cross_score,
                            connected or formed_cross,
                            covers_center or (current_row, current_col) == CENTER,
                        )
                        available[source] += 1

                walk(
                    start,
                    lexicon.root,
                    rack_counter.copy(),
                    "",
                    (),
                    (),
                    0,
                    1,
                    0,
                    False,
                    False,
                )
    return list(moves.values())


def _leave_value(leave: str) -> float:
    values = {
        "?": 12.0, "S": 2.5, "E": 1.3, "A": 0.8, "I": 0.5, "O": 0.4,
        "R": 1.2, "N": 0.8, "T": 0.8, "L": 0.5, "U": -0.3,
        "D": 0.1, "G": -0.2, "B": -0.8, "C": -0.4, "F": -0.8,
        "H": -0.5, "M": -0.5, "P": -0.6, "V": -1.4, "W": -1.2,
        "Y": -0.6, "K": -1.0, "J": -2.5, "X": -1.7, "Q": -6.0, "Z": -2.0,
    }
    score = sum(values.get(tile, 0.0) for tile in leave)
    counts = Counter(leave)
    score -= sum(max(0, count - 1) * 1.1 for tile, count in counts.items() if tile != "?")
    vowels = sum(counts[vowel] for vowel in "AEIOU")
    consonants = sum(counts[letter] for letter in LETTER_POINTS if letter not in "AEIOU")
    if len(leave) >= 4:
        if vowels == 0 or consonants == 0:
            score -= 4.0
        elif vowels == 1 or consonants == 1:
            score -= 1.0
    if counts["Q"] and not counts["U"]:
        score -= 3.0
    return score


def _with_move(board: Board, move: Move) -> Board:
    mutable = [list(row) for row in board]
    for tile in move.placed:
        mutable[tile.row][tile.col] = Cell(tile.letter, tile.is_blank)
    return tuple(tuple(row) for row in mutable)


def _premium_exposure(board: Board) -> float:
    weights = {"2L": 1.5, "3L": 3.5, "2W": 5.0, "3W": 9.0}
    risk = 0.0
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            premium = _PREMIUM_ROWS[row][col]
            if premium not in weights or board[row][col] is not None:
                continue
            neighbors = 0
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                r, c = row + dr, col + dc
                if 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c]:
                    neighbors += 1
            if neighbors:
                risk += weights[premium] * (1.0 + 0.15 * (neighbors - 1))
    return risk


def _cross_word_count(board: Board, move: Move) -> int:
    """Count perpendicular words created by a move (parallel-play value)."""
    count = 0
    for tile in move.placed:
        neighbors = (
            ((-1, 0), (1, 0))
            if move.direction == "Across"
            else ((0, -1), (0, 1))
        )
        if any(
            0 <= tile.row + dr < BOARD_SIZE
            and 0 <= tile.col + dc < BOARD_SIZE
            and board[tile.row + dr][tile.col + dc] is not None
            for dr, dc in neighbors
        ):
            count += 1
    return count


def _rack_before_move(move: Move) -> Counter[str]:
    rack = Counter(move.leave)
    for tile in move.placed:
        rack["?" if tile.is_blank else tile.letter] += 1
    return rack


def rank_moves(
    board: Board,
    moves: list[Move],
    limit: int = 5,
    bag_count: int | None = None,
    scores: dict | None = None,
) -> list[Move]:
    if not moves:
        return []
    before_risk = _premium_exposure(board)
    if bag_count == 0:
        leave_weight = 0.0
        resource_weight = 0.0
    elif bag_count is not None and bag_count <= 7:
        leave_weight = 0.35
        resource_weight = 0.35
    else:
        leave_weight = 1.0
        resource_weight = 1.0
    score_gap = 0
    if isinstance(scores, dict):
        you, opponent = scores.get("you"), scores.get("opponent")
        if isinstance(you, int) and isinstance(opponent, int):
            score_gap = you - opponent
    defense_weight = 1.0 if score_gap > 0 else 0.55 if score_gap < -20 else 0.7

    for move in moves:
        move.leave_value = _leave_value(move.leave)
        after_risk = _premium_exposure(_with_move(board, move))
        move.defense = before_risk - after_risk
        used = len(move.placed)
        blanks_used = sum(tile.is_blank for tile in move.placed)
        s_used = sum(tile.letter == "S" and not tile.is_blank for tile in move.placed)
        sweep = used == 7
        parallel_words = _cross_word_count(board, move)
        premium_high_tiles = sum(
            1
            for tile in move.placed
            if tile.letter in "JQXZ"
            and _PREMIUM_ROWS[tile.row][tile.col] in {"2L", "3L"}
        )
        wasted_high_tiles = sum(
            1
            for tile in move.placed
            if tile.letter in "JQXZ"
            and _PREMIUM_ROWS[tile.row][tile.col] in {".", "*"}
        )
        valuable_use = sweep or (parallel_words >= 2 and move.score >= 25)
        resource_cost = 0.0 if valuable_use else blanks_used * 6.0 + s_used * 1.5
        tactical_value = (
            min(3, parallel_words) * 0.8
            + premium_high_tiles * 2.0
            - wasted_high_tiles * 0.8
        )
        move.efficiency = (
            move.score / math.sqrt(max(1, used))
            - resource_weight * resource_cost
            + tactical_value
        )
        move.equity = (
            move.score
            + leave_weight * move.leave_value
            + defense_weight * move.defense
            + tactical_value
            - resource_weight * resource_cost
        )

    by_score = sorted(moves, key=lambda m: (m.score, m.equity), reverse=True)
    viable_floor = max(1, by_score[0].score * 0.45)
    viable = [move for move in moves if move.score >= viable_floor]
    leaders = {
        "highest score": max(viable, key=lambda m: (m.score, m.equity)),
        "best overall": max(viable, key=lambda m: (m.equity, m.score)),
        "most efficient": max(viable, key=lambda m: (m.efficiency, m.score)),
        "best defense": max(viable, key=lambda m: (m.defense, m.equity)),
    }
    labels_by_key: dict[tuple, list[str]] = {}

    def move_key(move: Move) -> tuple:
        return (
            move.word,
            move.row,
            move.col,
            move.direction,
            tuple((p.row, p.col, p.letter, p.is_blank) for p in move.placed),
        )

    def strategic_signature(move: Move) -> tuple:
        """Collapse rotationally equivalent advice with identical trade-offs."""
        return (
            move.word,
            move.score,
            move.leave,
            round(move.defense, 2),
            tuple(sorted((tile.letter, tile.is_blank) for tile in move.placed)),
        )

    for move in viable:
        labels = labels_by_key.setdefault(move_key(move), [])
        cross_words = _cross_word_count(board, move)
        rack_before = _rack_before_move(move)
        if len(move.placed) == 7:
            labels.append("40-point sweep")
        if cross_words >= 2:
            labels.append("parallel play")
        if len(move.word) == 2:
            labels.append("two-letter utility")
        if move.anchors and len(move.placed) <= 2:
            labels.append("hook or extension")
        if any(
            tile.letter in "JQXZ"
            and _PREMIUM_ROWS[tile.row][tile.col] in {"2L", "3L"}
            for tile in move.placed
        ):
            labels.append("high-value tile premium")
        if move.defense > 1:
            labels.append("premium control")
        cleared = sorted(
            tile
            for tile, count in rack_before.items()
            if tile != "?" and count > 1 and Counter(move.leave)[tile] < count
        )
        if cleared:
            labels.append("reduces duplicates")
        if resource_weight and rack_before["?"] and "?" in move.leave:
            labels.append("keeps blank")
        if resource_weight and rack_before["S"] and "S" in move.leave:
            labels.append("keeps S")
        if bag_count == 0:
            labels.append("final-turn scoring")
        elif bag_count is not None and bag_count <= 7:
            labels.append("late-game scoring")

    for label, move in leaders.items():
        labels_by_key.setdefault(move_key(move), []).append(label)

    ordered: list[Move] = []
    for move in leaders.values():
        if strategic_signature(move) not in {
            strategic_signature(item) for item in ordered
        }:
            ordered.append(move)
    for move in sorted(moves, key=lambda m: (m.equity, m.score), reverse=True):
        if len(ordered) >= limit:
            break
        if strategic_signature(move) not in {
            strategic_signature(item) for item in ordered
        }:
            ordered.append(move)
    ordered.sort(key=lambda m: (m.equity, m.score), reverse=True)
    for move in ordered:
        move.labels = tuple(labels_by_key.get(move_key(move), ()))
    return ordered[:limit]


def _format_move(move: Move, index: int) -> str:
    labels = f" [{', '.join(move.labels)}]" if move.labels else ""
    placed = ", ".join(
        f"{tile.coordinate}={tile.letter}{' (blank)' if tile.is_blank else ''}"
        for tile in move.placed
    )
    anchors = ", ".join(
        f"{tile.coordinate}={tile.letter}{' (blank)' if tile.is_blank else ''}"
        for tile in move.anchors
    )
    premiums = ", ".join(
        f"{tile.coordinate}={_PREMIUM_ROWS[tile.row][tile.col]}"
        for tile in move.placed
        if _PREMIUM_ROWS[tile.row][tile.col] not in {".", "*"}
    )
    defense = (
        "blocks exposure" if move.defense > 1
        else "opens exposure" if move.defense < -1
        else "neutral defense"
    )
    details = (
        (f"   Uses board: {anchors}\n" if anchors else "")
        + (f"   Premiums: {premiums}\n" if premiums else "")
    )
    return (
        f"{index}. {move.word} — {move.coordinate} {move.direction} — "
        f"{move.score} points{labels}\n"
        f"   Place: {placed}\n"
        f"{details}"
        f"   Leave: {move.leave or '(empty)'} | equity {move.equity:.1f} | "
        f"{defense} ({move.defense:+.1f})"
    )


def save_game(request: dict, board: Board, rack: str, ranked: list[Move]) -> Path:
    game_id = str(request.get("game_id") or "default")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", game_id):
        raise CoachError("game_id must use 1-64 letters, numbers, '.', '_' or '-'")
    target = _workspace_root() / "crossplay-data" / "games" / f"{game_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    observation = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "board": [
            "".join(
                "." if cell is None
                else cell.letter.lower() if cell.is_blank
                else cell.letter
                for cell in row
            )
            for row in board
        ],
        "rack": rack,
        "bag_count": request.get("bag_count"),
        "scores": request.get("scores"),
        "recommendations": [move.as_dict() for move in ranked],
    }
    observations: list[dict] = []
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(existing.get("observations"), list):
                observations = existing["observations"]
        except (OSError, json.JSONDecodeError, AttributeError):
            observations = []
    # Retrying the same screenshot updates the latest analysis rather than
    # manufacturing a duplicate turn.
    if observations and (
        observations[-1].get("board"),
        observations[-1].get("rack"),
    ) == (observation["board"], observation["rack"]):
        observations[-1] = observation
    else:
        observations.append(observation)
    observations = observations[-200:]
    payload = {
        "schema_version": 1,
        "game_id": game_id,
        **observation,
        "observations": observations,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def _self_test() -> None:
    lexicon = Lexicon(
        ["AT", "ATE", "EAT", "TEA", "RATE", "TARE", "TEAR", "AGOUTIES"]
    )
    rows = ["." * BOARD_SIZE for _ in range(BOARD_SIZE)]
    board = parse_board(rows)
    moves = generate_moves(board, "RATE", lexicon)
    assert moves
    assert all(
        any((tile.row, tile.col) == CENTER for tile in move.placed)
        for move in moves
    )
    best = rank_moves(board, moves, 3)
    assert best and best[0].score > 0

    rows[7] = "......." + "A" + "......."
    board = parse_board(rows)
    moves = generate_moves(board, "TE", lexicon)
    assert any(move.word == "ATE" for move in moves)

    rows = ["." * BOARD_SIZE for _ in range(BOARD_SIZE)]
    rows[9] = "......." + "A" + "......."
    board = parse_board(rows)
    moves = generate_moves(board, "GOUTIES", lexicon)
    anchored = next(
        move
        for move in moves
        if move.word == "AGOUTIES"
        and move.coordinate == "H10"
        and move.direction == "Across"
    )
    assert anchored.score == 56
    assert [(tile.coordinate, tile.letter) for tile in anchored.anchors] == [
        ("H10", "A")
    ]
    from_words = parse_board_words(
        [
            {"word": "AT", "position": "H8", "direction": "across"},
            {"word": "TO", "position": "I8", "direction": "down"},
        ]
    )
    assert from_words[7][8] == Cell("T")
    assert from_words[8][8] == Cell("O")
    print("crossplay-coach: self-test PASS")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and rank legal NYT Crossplay moves from board JSON."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Read-only installation status.")
    status.add_argument("--lexicon")

    validate = subparsers.add_parser("validate", help="Validate request JSON.")
    validate.add_argument("--request", required=True)

    audit = subparsers.add_parser(
        "audit", help="Cross-check an extracted board's topology and existing words."
    )
    audit.add_argument("--request", required=True)
    audit.add_argument("--lexicon")
    audit.add_argument("--format", choices=("text", "json"), default="text")

    learn = subparsers.add_parser(
        "learn-board-words",
        help="Record re-verified, already-played board words in the supplemental lexicon.",
    )
    learn.add_argument("--request", required=True)
    learn.add_argument("--lexicon")

    learned = subparsers.add_parser(
        "learned", help="List learned board words and their admission status."
    )
    learned.add_argument(
        "--list",
        action="store_true",
        help="List every learned word (the only action; accepted for explicitness).",
    )

    forget = subparsers.add_parser(
        "forget", help="Remove a learned board word from the supplemental lexicon."
    )
    forget.add_argument("word")

    solve = subparsers.add_parser("solve", help="Solve and rank a position.")
    solve.add_argument("--request", required=True)
    solve.add_argument("--lexicon")
    solve.add_argument("--limit", type=int, default=5, choices=range(1, 6))
    solve.add_argument("--format", choices=("text", "json"), default="text")
    solve.add_argument("--save", action="store_true")
    solve.add_argument(
        "--force",
        action="store_true",
        help=(
            "Solve a board whose transcription audit did not pass. Every "
            "recommendation is marked unaudited."
        ),
    )

    subparsers.add_parser("games", help="List saved game-state JSON files.")
    subparsers.add_parser("self-test", help="Run dependency-free engine checks.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "status":
            try:
                lexicon_path = _resolve_lexicon(args.lexicon)
                lexicon = _load_lexicon(lexicon_path)
                detail = f"ready ({len(lexicon.words)} words at {lexicon_path})"
            except CoachError as exc:
                detail = f"installed; lexicon needed ({exc})"
            print(f"crossplay-coach: {detail}")
            return 0
        if args.command == "self-test":
            _self_test()
            return 0
        if args.command == "games":
            games_dir = _workspace_root() / "crossplay-data" / "games"
            for path in sorted(games_dir.glob("*.json")) if games_dir.exists() else []:
                print(path.stem)
            return 0
        if args.command == "learned":
            rows = list_learned_words()
            if not rows:
                print(f"learned board words: none ({_learned_words_path()})")
                return 0
            print(f"learned board words ({_learned_words_path()}):")
            for row in rows:
                games = len(row["games"])
                where = (
                    f"{row['position']} {row['direction']}"
                    if row["position"] and row["direction"]
                    else row["source"]
                )
                print(
                    f"  {row['word']} — {row['status']} "
                    f"({games} game{'' if games == 1 else 's'}; first seen {where})"
                )
            return 0
        if args.command == "forget":
            path, removed = forget_learned_word(args.word)
            word = args.word.strip().upper()
            if removed:
                print(f"forgot board word: {word} ({path})")
            else:
                print(f"forget: {word} is not in the learned dictionary ({path})")
            return 0

        try:
            request, board, rack = load_request(Path(args.request))
        except CoachError as exc:
            if args.command == "audit":
                if args.format == "json":
                    print(json.dumps({"status": "REVIEW", "concerns": [str(exc)]}, indent=2))
                else:
                    print("audit: REVIEW — request needs correction")
                    print(f"  REVIEW: {exc}")
                return 0
            raise
        if args.command == "validate":
            occupied = sum(cell is not None for row in board for cell in row)
            blanks = sum(bool(cell and cell.is_blank) for row in board for cell in row)
            print(
                f"valid: {occupied} occupied cells, {blanks} board blanks, "
                f"rack {rack}"
            )
            return 0

        lexicon_path = _resolve_lexicon(args.lexicon)
        lexicon = _load_lexicon(lexicon_path)
        if args.command == "learn-board-words":
            learned_path, recorded = learn_verified_board_words(
                board, lexicon, sighting_game_key(request, board)
            )
            if not recorded:
                print(f"learned board words: no additions ({learned_path})")
                return 0
            print(f"learned board words ({learned_path}):")
            for item in recorded:
                if item["status"] == "admitted":
                    detail = f"seen in {item['games']} games"
                else:
                    detail = (
                        f"seen in {item['games']} of the "
                        f"{LEARNED_ADMISSION_GAMES} different games required "
                        f"before it enters the lexicon"
                    )
                print(f"  {item['word']} — {item['status']} ({detail})")
            return 0
        audit_result = audit_board(board, lexicon)
        if args.command == "audit":
            if args.format == "json":
                print(json.dumps(audit_result, indent=2))
            else:
                print(
                    f"audit: {audit_result['status']} — "
                    f"{audit_result['occupied_cells']} occupied cells, "
                    f"{len(audit_result['words'])} existing words"
                )
                for word in audit_result["words"]:
                    marker = "✓" if word["accepted"] else "?"
                    print(
                        f"  {marker} {word['word']} — "
                        f"{word['position']} {word['direction']}"
                    )
                for concern in audit_result["concerns"]:
                    print(f"  REVIEW: {concern}")
            return 0
        # Fail-closed: a board this engine judged unsound yields no plays.
        # `audit` stays exit-0-with-REVIEW so a weak orchestration model can
        # read the correction; `solve` refuses, because a ranked list with an
        # exact score is indistinguishable from a trustworthy one downstream.
        if audit_result["status"] != "PASS" and not args.force:
            if args.format == "json":
                print(json.dumps({
                    "lexicon": str(lexicon_path),
                    "refused": True,
                    "transcription_audit": audit_result,
                }, indent=2))
            else:
                print("solve: REFUSED — the transcription audit did not pass")
                for concern in audit_result["concerns"]:
                    print(f"  REVIEW: {concern}")
                print(
                    "  Correct the transcription and re-run audit. A word "
                    "still pending re-verification needs a second game before "
                    "it counts; ask the user before overriding with --force."
                )
            return 2
        if audit_result["status"] != "PASS":
            # stderr, so `--format json` keeps a parseable stdout.
            print(
                "solve: --force overriding audit REVIEW: "
                + "; ".join(audit_result["concerns"]),
                file=sys.stderr,
            )
        moves = generate_moves(board, rack, lexicon)
        ranked = rank_moves(
            board,
            moves,
            args.limit,
            bag_count=request.get("bag_count"),
            scores=request.get("scores"),
        )
        if not ranked:
            raise CoachError(
                "no legal move found; verify the transcription and lexicon "
                "(passing or swapping may be necessary)"
            )
        saved_path = save_game(request, board, rack, ranked) if args.save else None
        if args.format == "json":
            unaudited = audit_result["status"] != "PASS"
            output = {
                "lexicon": str(lexicon_path),
                "refused": False,
                "forced": unaudited,
                "transcription_audit": audit_result,
                "legal_moves": len(moves),
                "recommendations": [
                    move.as_dict(unaudited=unaudited) for move in ranked
                ],
                "saved_state": str(saved_path) if saved_path else None,
            }
            print(json.dumps(output, indent=2))
        else:
            print(
                f"Crossplay Coach — {len(moves)} legal moves using "
                f"{len(lexicon.words)}-word lexicon"
            )
            for index, move in enumerate(ranked, start=1):
                print(_format_move(move, index))
            if saved_path:
                print(f"State: {saved_path}")
        return 0
    except CoachError as exc:
        print(f"crossplay-coach: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
