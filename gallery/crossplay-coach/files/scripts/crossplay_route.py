#!/usr/bin/env python3
"""Crossplay Coach app route: the deterministic half of one ``xplay`` move.

Evolve's plugin claims an ``xplay`` + screenshot turn before the bot's session
model runs, so the move never becomes a conversational tool loop. The plugin
owns transport only — one vision call, one commentary call, the chat reply.
Everything the app knows lives here:

``prepare --game ID``
    The game's last CONFIRMED board and the vision instructions to use with
    it. With a confirmed board the vision call is asked only for what changed
    since (``delta``); without one, for the whole board (``full``).

``resolve`` (request JSON on stdin)
    Merge the vision JSON onto the confirmed board, decide in code how far
    behind that board was, audit ONCE, save the result as the new confirmed
    board on PASS, solve, and hand back the reply text plus the commentary
    request. A failed audit ends the move with its text; it never loops.

Every outcome is one JSON object on stdout with exit status 0. Exit 2 means
the install itself is unusable (no lexicon, bad request shape); the plugin
then answers with that message rather than guessing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crossplay_coach as coach  # noqa: E402  (sibling module, path set above)


ROUTE_SCHEMA_VERSION = 1
# A confirmed board this many plays behind the screenshot is diffed; more
# than this and the delta is too large to trust, so the board is re-read
# whole. Two = your last move plus your opponent's reply.
MAX_DIFFABLE_PLAYS = 2
TOP_N = 5


class RouteRefusal(Exception):
    """A move the app declines, with the text the player should see."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Vision contract ────────────────────────────────────────────────────────

_WORD_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "position": {"type": "string"},
        "direction": {"type": "string", "enum": ["across", "down"]},
        "blanks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["word", "position", "direction"],
}


def vision_schema(mode: str) -> dict:
    properties: dict = {
        "board_words": {"type": "array", "items": _WORD_ITEM_SCHEMA},
        "rack": {"type": "string"},
        "bag_count": {"type": ["integer", "null"]},
        "scores": {
            "type": ["object", "null"],
            "properties": {
                "you": {"type": ["integer", "null"]},
                "opponent": {"type": ["integer", "null"]},
            },
        },
        "uncertain": {"type": "array", "items": {"type": "string"}},
    }
    if mode == "delta":
        properties["confirmed_words_missing"] = {
            "type": "array", "items": {"type": "string"},
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["board_words", "rack", "bag_count", "scores", "uncertain"],
    }


_READING_RULES = """\
The board is 15x15. Rows are numbered 1-15 top to bottom, columns lettered A-O
left to right; the centre square is H8. Return ONLY the JSON object.

- board_words: committed words of two or more letters, horizontal and
  vertical, each as {"word", "position" (its first cell, e.g. "H8"),
  "direction" ("across" or "down"), "blanks" (cells in that word showing a
  0-point blank tile; omit when none)}. List both directions at crossings.
- Green-outlined tiles are a tentative play, NOT committed: never put them in
  board_words; their letters go back into the rack.
- rack: the player's whole effective rack — tray tiles plus any green
  tentative tiles. Use "?" for a blank.
- bag_count: the number in the yellow circle at the top. It is the tiles left
  in the bag, not a timer. null if it is not visible.
- scores: {"you", "opponent"} as displayed; null if not visible.
- uncertain: every cell you cannot read with confidence, as coordinates.
  Never guess a letter — list the cell here instead."""


def vision_instructions(confirmed_words: Optional[list[dict]]) -> str:
    if not confirmed_words:
        return (
            "Transcribe this Crossplay screenshot.\n\n" + _READING_RULES
        )
    listed = "\n".join(
        f"- {item['word']} {item['position']} {item['direction']}"
        + (f" (blanks {', '.join(item['blanks'])})" if item.get("blanks") else "")
        for item in confirmed_words
    )
    return (
        "Transcribe what has CHANGED on this Crossplay board. These words were "
        "already confirmed on it and must not be repeated:\n"
        f"{listed}\n\n"
        "In board_words return ONLY words that contain at least one tile that "
        "is not part of a confirmed word — new plays, including extensions "
        "and crossings through confirmed tiles (write the whole word). If a "
        "confirmed word is no longer on the board or reads differently, name "
        "it in confirmed_words_missing.\n\n" + _READING_RULES
    )


# ── Helpers ────────────────────────────────────────────────────────────────

def _lexicon_info(path: Path, lexicon: coach.Lexicon) -> dict:
    note = ""
    version_file = Path(__file__).resolve().parent / "crossplay_lexicon.json"
    try:
        declared = json.loads(version_file.read_text(encoding="utf-8"))
        note = str(declared.get("note") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        declared = {}
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        sha = digest.hexdigest()[:12]
    except OSError:
        sha = None
    return {
        "file": path.name,
        "words": len(lexicon.words),
        "sha256_12": sha,
        "reference": declared.get("reference") if isinstance(declared, dict) else None,
        "note": note,
    }


def _scores(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    out = {}
    for key in ("you", "opponent"):
        raw = value.get(key)
        out[key] = raw if isinstance(raw, int) and raw >= 0 else None
    return out


def _bag(value: object) -> Optional[int]:
    return value if isinstance(value, int) and 0 <= value <= 100 else None


def _merge_words(base: coach.Board, words: list[dict]) -> coach.Board:
    """Lay ``words`` over ``base``; a letter that disagrees is refused by name."""
    if not words:
        return base
    overlay = coach.parse_board_words(words)
    merged: list[list[Optional[coach.Cell]]] = [list(row) for row in base]
    conflicts: list[str] = []
    for row in range(coach.BOARD_SIZE):
        for col in range(coach.BOARD_SIZE):
            incoming = overlay[row][col]
            if incoming is None:
                continue
            existing = merged[row][col]
            if existing is not None and existing.letter != incoming.letter:
                conflicts.append(
                    f"{chr(65 + col)}{row + 1} (saved {existing.letter}, "
                    f"screenshot {incoming.letter})"
                )
                continue
            merged[row][col] = coach.Cell(
                incoming.letter,
                incoming.is_blank or bool(existing and existing.is_blank),
            )
    if conflicts:
        raise RouteRefusal(
            "conflict",
            "The screenshot contradicts the saved board at "
            + ", ".join(conflicts)
            + ". I stopped rather than guess which is right.",
        )
    return tuple(tuple(row) for row in merged)


def _placed_words(before: coach.Board, after: coach.Board) -> list[str]:
    """The words on ``after`` that use at least one tile ``before`` lacks."""
    names = []
    for item in coach.board_words_from_board(after):
        dr, dc = (0, 1) if item["direction"] == "across" else (1, 0)
        col = ord(item["position"][0]) - 65
        row = int(item["position"][1:]) - 1
        if any(
            before[row + dr * i][col + dc * i] is None
            for i in range(len(item["word"]))
        ):
            names.append(f"{item['word']} {item['position']} {item['direction']}")
    return names


def _check_consistency(rack: str, bag_count: Optional[int], uncertain: list) -> None:
    if uncertain:
        cells = ", ".join(str(cell) for cell in uncertain[:8])
        raise RouteRefusal(
            "uncertain",
            f"I couldn't read {cells} with confidence. What is on "
            f"{'that square' if len(uncertain) == 1 else 'those squares'}? "
            "Send xplay again once it's clearer (or tell me the letters).",
        )
    if bag_count is not None and bag_count > 0 and len(rack) != 7:
        raise RouteRefusal(
            "inconsistent",
            f"I read your rack as {rack} ({len(rack)} tiles), but with "
            f"{bag_count} tiles still in the bag it must hold 7. Please "
            "resend a screenshot with the whole rack visible.",
        )


def _default_call(best: coach.Move) -> str:
    reasons = []
    if best.labels:
        reasons.append(", ".join(best.labels[:2]))
    reasons.append(f"equity {best.equity:.1f}")
    return f"Call: {best.word} at {best.coordinate} {best.direction} for {best.score} — {'; '.join(reasons)}."


def _commentary_request(
    ranked: list[coach.Move], rack: str, bag_count: Optional[int], scores: Optional[dict],
) -> dict:
    compact = [
        {
            "word": move.word,
            "at": f"{move.coordinate} {move.direction}",
            "score": move.score,
            "leave": move.leave or "",
            "equity": round(move.equity, 1),
            "defense": round(move.defense, 1),
            "labels": list(move.labels),
        }
        for move in ranked
    ]
    return {
        "system": (
            "You write the one-line recommendation for a word-game coaching "
            "app. A solver already found, scored and ranked the plays; never "
            "add, change or re-score a play. Reply with exactly one line that "
            "starts with 'Call:', names ONE of the listed words, and says why "
            "in at most 40 words (points now vs. the tiles it leaves vs. what "
            "it opens for the opponent). No preamble, no list."
        ),
        "user": json.dumps(
            {"rack": rack, "bag": bag_count, "scores": scores, "plays": compact},
            separators=(",", ":"),
        ),
        "max_tokens": 160,
        "allowed_words": [move.word for move in ranked],
    }


# ── Commands ───────────────────────────────────────────────────────────────

def prepare(game_id: str, fresh: bool = False) -> dict:
    coach._game_path(game_id)  # validates the id
    confirmed = None if fresh else coach.load_confirmed(game_id)
    words: Optional[list[dict]] = None
    summary = None
    if confirmed is not None:
        try:
            board = coach.parse_board(confirmed["board"])
        except coach.CoachError:
            board = None
        if board is not None and coach._board_has_tiles(board):
            words = coach.board_words_from_board(board)
            summary = {
                "confirmed_at": confirmed.get("confirmed_at"),
                "source": confirmed.get("source"),
                "scores": confirmed.get("scores"),
                "bag_count": confirmed.get("bag_count"),
                "move_count": confirmed.get("move_count"),
                "words": len(words),
            }
    mode = "delta" if words else "full"
    return {
        "route_schema_version": ROUTE_SCHEMA_VERSION,
        "game_id": game_id,
        "confirmed": summary,
        "vision": {
            "mode": mode,
            "instructions": vision_instructions(words),
            "schema_name": f"crossplay_{mode}",
            "json_schema": vision_schema(mode),
        },
    }


def _stale(game_id: str, reason: str, detail: dict) -> dict:
    return {
        "outcome": "stale",
        "game_id": game_id,
        "reason": reason,
        "detail": detail,
        "vision": {
            "mode": "full",
            "instructions": vision_instructions(None),
            "schema_name": "crossplay_full",
            "json_schema": vision_schema("full"),
        },
    }


def _resolve(request: dict, lexicon_path: Optional[str] = None) -> dict:
    started = time.monotonic()
    game_id = str(request.get("game_id") or "default")
    coach._game_path(game_id)
    mode = request.get("mode")
    if mode not in {"delta", "full"}:
        raise coach.CoachError("resolve request needs mode 'delta' or 'full'")
    vision = request.get("vision")
    if not isinstance(vision, dict):
        raise RouteRefusal(
            "unreadable",
            "I couldn't get a usable reading of that screenshot. Please send "
            "it again with xplay.",
        )
    after_stale = request.get("after_stale")

    try:
        rack = coach.parse_rack(vision.get("rack"))
    except coach.CoachError as exc:
        raise RouteRefusal(
            "unreadable", f"I couldn't read your rack ({exc}). Please resend."
        ) from exc
    bag_count = _bag(vision.get("bag_count"))
    scores = _scores(vision.get("scores"))
    raw_uncertain = vision.get("uncertain")
    uncertain: list = raw_uncertain if isinstance(raw_uncertain, list) else []
    _check_consistency(rack, bag_count, uncertain)

    raw_words = vision.get("board_words")
    if not isinstance(raw_words, list):
        raw_words = []
    confirmed = None if request.get("fresh") else coach.load_confirmed(game_id)
    saved_board: Optional[coach.Board] = None
    if confirmed is not None:
        try:
            saved_board = coach.parse_board(confirmed["board"])
        except coach.CoachError:
            saved_board = None
    empty = coach.parse_board(["." * coach.BOARD_SIZE] * coach.BOARD_SIZE)
    base = saved_board if saved_board is not None else empty

    try:
        if mode == "delta":
            if saved_board is None:
                raise coach.CoachError("delta reading without a confirmed board")
            missing = vision.get("confirmed_words_missing") or []
            if isinstance(missing, list) and missing:
                raise RouteRefusal(
                    "conflict",
                    "The screenshot no longer shows "
                    + ", ".join(str(word) for word in missing[:6])
                    + f" from the saved game '{game_id}'. If this is a "
                    "different game, send `xplay game <name>`; to start this "
                    "one over, send `xplay fresh`.",
                )
            board = _merge_words(base, raw_words)
        else:
            board = coach.parse_board_words(raw_words) if raw_words else empty
    except coach.CoachError as exc:
        raise RouteRefusal(
            "unreadable",
            f"The board reading didn't hold together ({exc}). Please resend "
            "the screenshot with xplay.",
        ) from exc

    lag = {"new_tiles": 0, "plays": 0, "contradictions": []}
    if saved_board is not None:
        lag = coach.count_new_plays(saved_board, board)
        if lag["contradictions"]:
            raise RouteRefusal(
                "conflict",
                "This board contradicts the saved game "
                f"'{game_id}' at " + ", ".join(lag["contradictions"][:6])
                + ". If it is a different game, send `xplay game <name>`; to "
                "start this one over, send `xplay fresh`.",
            )
        saved_scores = _scores(confirmed.get("scores")) if confirmed else None
        if scores and saved_scores:
            for side in ("you", "opponent"):
                now, then = scores.get(side), saved_scores.get(side)
                if now is not None and then is not None and now < then:
                    raise RouteRefusal(
                        "conflict",
                        f"The {side} score went down since the saved game "
                        f"'{game_id}' ({then} → {now}), so this looks like a "
                        "different game. Send `xplay game <name>` for a new "
                        "one, or `xplay fresh` to start this one over.",
                    )
        if mode == "delta":
            expected = 0
            if scores and saved_scores:
                expected = sum(
                    1 for side in ("you", "opponent")
                    if scores.get(side) is not None
                    and saved_scores.get(side) is not None
                    and scores[side] > saved_scores[side]
                )
            if lag["plays"] > MAX_DIFFABLE_PLAYS:
                return _stale(game_id, "several_moves_behind", {**lag, "expected_plays": expected})
            if lag["plays"] < expected:
                return _stale(game_id, "delta_incomplete", {**lag, "expected_plays": expected})

    lexicon_file = coach._resolve_lexicon(lexicon_path)
    lexicon = coach._load_lexicon(lexicon_file)
    audit = coach.audit_board(board, lexicon)
    pending: list[dict] = []
    if audit["status"] != "PASS":
        structural = [
            concern for concern in audit["concerns"]
            if not concern.startswith("words absent from this lexicon:")
        ]
        if structural:
            raise RouteRefusal(
                "audit",
                "The board didn't pass its check, so I haven't suggested "
                "anything: " + "; ".join(structural) + ". Please resend a "
                "clearer screenshot with xplay.",
            )
        # Only dictionary-version differences. Record a sighting (pending
        # until a second game shows it — one reading is not proof), then
        # judge the same board again with whatever that admitted.
        _, pending = coach.learn_verified_board_words(
            board, lexicon, coach.sighting_game_key({"game_id": game_id}, board)
        )
        lexicon = coach._load_lexicon(lexicon_file)
        audit = coach.audit_board(board, lexicon)
        if audit["status"] != "PASS":
            words = ", ".join(audit["unknown_words"])
            raise RouteRefusal(
                "lexicon",
                f"{words} {'is' if len(audit['unknown_words']) == 1 else 'are'} "
                "on the board but not in my word list. I've noted "
                f"{'it' if len(audit['unknown_words']) == 1 else 'them'}; a "
                "played word is accepted once a second game shows it. Until "
                "then I can't score around it safely.",
            )

    coach.save_confirmed(
        game_id, board, rack=rack, bag_count=bag_count, scores=scores,
        source=f"app-route:{mode}",
    )
    moves = coach.generate_moves(board, rack, lexicon)
    ranked = coach.rank_moves(board, moves, TOP_N, bag_count=bag_count, scores=scores)
    if not ranked:
        raise RouteRefusal(
            "no_move",
            f"No legal play with {rack} on this board and my word list — "
            "passing or swapping may be the move.",
        )
    # History only: the confirmed board was saved above, before solving, so
    # a transcription that passed its audit is kept even if no play exists.
    coach.save_game(
        {"game_id": game_id, "bag_count": bag_count, "scores": scores},
        board, rack, ranked, audited=False,
    )

    new_words = _placed_words(base, board) if saved_board is not None else []
    header = [
        f"Crossplay Coach — {len(moves)} legal moves · rack {rack}"
        + (
            f" · you {scores['you']}–{scores['opponent']}"
            if scores and scores.get("you") is not None and scores.get("opponent") is not None
            else ""
        )
        + (f" · bag {bag_count}" if bag_count is not None else "")
    ]
    if saved_board is not None and mode == "delta":
        if new_words:
            header.append(
                f"Board: saved game '{game_id}' + {', '.join(new_words[:4])}"
            )
        else:
            header.append(f"Board: saved game '{game_id}', no new tiles")
    elif isinstance(after_stale, dict) and confirmed is not None:
        behind = after_stale.get("plays")
        header.append(
            f"Board: read whole — saved game '{game_id}' was "
            + (f"{behind} plays behind" if after_stale.get("reason") == "several_moves_behind"
               else "missing plays the scores show")
            + "; saved again now"
        )
    info = _lexicon_info(lexicon_file, lexicon)
    tail = [f"Lexicon: {info['file']} ({info['words']:,} words). {info['note']}".rstrip()]
    if pending:
        tail.append(
            "Noted (pending a second game): "
            + ", ".join(item["word"] for item in pending)
        )
    return {
        "outcome": "solved",
        "game_id": game_id,
        "mode": mode,
        "lag": lag,
        "legal_moves": len(moves),
        "recommendations": [move.as_dict() for move in ranked],
        "reply_head": "\n".join(header) + "\n\n" + "\n".join(
            coach._format_move(move, index) for index, move in enumerate(ranked, 1)
        ),
        "reply_tail": "\n".join(tail),
        "default_call": _default_call(ranked[0]),
        "commentary": _commentary_request(ranked, rack, bag_count, scores),
        "lexicon": info,
        "solve_ms": int((time.monotonic() - started) * 1000),
    }


def resolve(request: dict, lexicon_path: Optional[str] = None) -> dict:
    """One move's outcome: ``solved``, ``stale`` (read again whole) or
    ``refused`` (the reply says why). Install errors raise ``CoachError``."""
    try:
        return _resolve(request, lexicon_path)
    except RouteRefusal as refusal:
        return {
            "outcome": "refused",
            "game_id": str(request.get("game_id") or "default"),
            "code": refusal.code,
            "reply": refusal.message,
        }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Crossplay Coach app route: prepare and resolve one move."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--game", default="default")
    prep.add_argument("--fresh", action="store_true")
    res = sub.add_parser("resolve")
    res.add_argument("--lexicon")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(json.dumps(prepare(args.game, fresh=args.fresh)))
            return 0
        try:
            request = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as exc:
            raise coach.CoachError(f"resolve request is not JSON: {exc}") from exc
        if not isinstance(request, dict):
            raise coach.CoachError("resolve request must be a JSON object")
        print(json.dumps(resolve(request, args.lexicon)))
        return 0
    except coach.CoachError as exc:
        print(json.dumps({"outcome": "error", "reply": f"Crossplay Coach can't run: {exc}"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
