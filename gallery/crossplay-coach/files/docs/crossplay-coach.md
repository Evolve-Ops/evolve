# Crossplay Coach

Crossplay Coach turns a screenshot transcription into legal, scored, ranked
NYT Crossplay plays. The vision model reads the image; deterministic code
validates the board, generates moves, calculates premiums and cross-words, and
ranks strategic trade-offs.

## Invoke

Send an image with `xplay`. Optionally name a continuing game:

```text
xplay
xplay game alice
```

The bot must inspect the image, create a temporary JSON request with its file
write tool, audit the transcription, run the solver, return its output, and
remove the temporary request. The app never receives or stores the image.

There is no seconds countdown in the screenshot. The yellow circled number at
the top is the number of tiles remaining in the bag. Never rush transcription
because of that number or describe it as a timer.

## Screenshot transcription contract

Use rows 1–15 from top to bottom and columns A–O from left to right. Prefer
`board_words`: it avoids error-prone dot counting and lets the script merge
intersections itself.

```json
{
  "schema_version": 1,
  "game_id": "alice",
  "board_words": [
    {"word": "VARY", "position": "L1", "direction": "down"},
    {"word": "DIVULGE", "position": "A3", "direction": "across", "blanks": ["E3"]},
    {"word": "THEORY", "position": "G4", "direction": "across"},
    {"word": "TRACKERS", "position": "D5", "direction": "down"}
  ],
  "rack": "ETRDESN",
  "bag_count": 42,
  "scores": {"you": 177, "opponent": 243}
}
```

- Include every visible horizontal and vertical word. The parser merges matching
  letters at intersections and rejects conflicts or words that cross an edge.
- Word capitalization does not matter. If a committed tile is a displayed
  0-point blank, list its coordinate in that word's optional `blanks` array.
- `board` with exactly 15 dot-and-letter row strings remains supported for
  trusted machine-generated positions, but vision should use `board_words`.
- In `board`, `.` means empty.
- Uppercase letters are ordinary committed board tiles.
- In legacy `board` rows only, lowercase letters are committed 0-point blanks.
- `?` means a blank in the rack.
- `bag_count` and `scores` may be `null` when cropped out.
- The rack must be the user's complete effective rack, not merely the tiles
  still visible in the tray.

### Tentative green tiles

Green-outlined tiles are an uncommitted candidate play. Do not include them in
`board_words`. Put every tentative tile back into `rack`, together with tiles
still visible in the tray. A committed tile under a crossing remains on the
board.

For example, if six green tiles make `BIRTHER` through a committed `E`, while
`S` remains in the tray, the effective rack is the six green tiles plus `S`.

If any cell, blank status, or rack tile is uncertain, ask the user to confirm
the coordinate or rack before running the solver. Never silently guess.

### Reliable image-reading sequence

Do not trust an automatic image caption's word list or coordinates. Inspect the
image pixels using at least two focused passes:

1. Identify the exact board bounds, rack, bag count, and all horizontal words.
2. Re-read by columns, using intersections to reconstruct every vertical word.
3. Record each visible word with its start coordinate and direction. Let the
   script merge intersections; never estimate runs of dots.
4. Run `audit` below. Fix disconnected groups, isolated letters, malformed
   intersections, or a missing H8 before solving.

If the full image is too small, make overlapping board crops or enlarged
copies beside the original attachment under its existing
`workspace/media/inbound/` staging directory. Image tools reject `/tmp` paths.
Delete every derived crop immediately after transcription. The app itself
still receives only JSON.

An existing word absent from the configured lexicon can be a dictionary-version
difference. Re-inspect its cells. If the board is structurally valid and the
word is clearly committed, run `learn-board-words`; do not rewrite it merely to
make an older lexicon accept it. Learning applies only to already-played board
words, never speculative rack words.

A first sighting is recorded `pending` and does **not** clear the audit — one
sighting cannot be told apart from a first-pass OCR error. Say which word is
pending, and stop. The word is admitted once a second, different game shows it
again on a clean board.

Malformed or conflicting `board_words` make `audit` print `REVIEW` with the
specific coordinate while still exiting successfully. Correct that coordinate
and audit once more. If it remains unresolved, ask the user about only those
coordinates. Do not run shell diagnostics or replace the solver with manual
anagrams.

## CLI

From the bot workspace:

```bash
scripts/crossplay_coach status
scripts/crossplay_coach validate --request crossplay-data/request.json
scripts/crossplay_coach audit --request crossplay-data/request.json
scripts/crossplay_coach learn-board-words --request crossplay-data/request.json
scripts/crossplay_coach learned --list
scripts/crossplay_coach forget YEZ
scripts/crossplay_coach solve --request crossplay-data/request.json --save
scripts/crossplay_coach games
scripts/crossplay_coach self-test
```

Use the extensionless launcher shown above. It remains safe when a weaker model
prepends a workspace `cd`, while direct Python commands combined with shell
operators are rejected by OpenClaw's exec preflight. Do not append pipes,
redirection, or heredocs.

`audit` lists every existing horizontal and vertical word and flags structural
or lexicon concerns. If its only concerns are re-verified existing words absent
from the base lexicon, `learn-board-words` records a sighting of each in
`crossplay-data/learned-words.jsonl`. The command refuses to learn from a board
with disconnected groups, isolated tiles, or a missing center.

`solve` is **fail-closed**: a board whose audit did not report PASS yields no
recommendations and a non-zero exit, printing the same coordinate-specific
REVIEW so the correction is visible. `--format json` returns
`{"refused": true, "transcription_audit": {...}}` and never a recommendations
array. `--force` overrides the refusal, warns on stderr with every audit item
it is overriding, and marks each returned move `"unaudited": true` — ask the
user before using it, and tell them what is being overridden.

Otherwise `solve` returns up to five recommendations with conventional `A1`
coordinates, Across/Down orientation, exact new placements, existing board
letters used, premiums reached, score, rack leave, equity, and defensive
exposure.

### Learned board words

A sighting is not an admission. Each `learn-board-words` run appends the word,
its coordinate and direction, the game id, and an ISO timestamp; the word joins
the effective lexicon only after **two different games** have shown it on an
otherwise clean-auditing board. Until then it is `pending` and no audit or
solve sees it. A request with no `game_id` counts the board itself, so
re-running the same screenshot is never a second re-verification.

```bash
scripts/crossplay_coach learned --list   # word — admitted|pending (n games)
scripts/crossplay_coach forget YEZ       # remove it; later solves stop using it
```

An OCR misread is therefore reversible rather than a permanent widening of the
dictionary the solver generates from.

## Lexicon

The app deliberately does not redistribute a word list. Install a legitimately
obtained plain-text lexicon, one word per line, at:

```text
crossplay-data/lexicon.txt
```

Alternatively pass `--lexicon PATH`, set `CROSSPLAY_LEXICON`, or create
`config/crossplay.json`:

```json
{"lexicon_path": "/absolute/path/to/word-list.txt"}
```

The development lexicon may differ from Crossplay's NWL 2023 list. Tell the
user which lexicon was used when that distinction matters. Twice-verified words
accepted on the live board are retained separately in
`crossplay-data/learned-words.jsonl`; the operator-supplied base file is never
modified. A pre-1.7 plain-text `crossplay-data/learned-words.txt` is still
merged for one release and is migrated into the JSON-lines store on the first
write, after which it is renamed `learned-words.txt.migrated` and ignored.

## Data and privacy

Saved games live at `crossplay-data/games/<game-id>.json`. They retain up to
200 normalized observations: board, rack, visible scores/bag count, and
recommendations. Retrying the same board and rack replaces the latest
observation rather than duplicating it. They contain no image data.

The app cannot remove copies retained by Telegram, WhatsApp, Slack, Signal,
OpenClaw, or another messaging transport. It makes no network calls and never
interacts with the NYT app.

## Ranking

Recommendations expose several useful views instead of pretending one number
captures all strategy:

- exact immediate score;
- overall equity (score, game-phase-adjusted rack leave, and board control);
- efficiency relative to tiles and valuable resources spent;
- defense, measured as premium-square exposure created or removed;
- tactical labels for sweeps, parallel plays, hooks/extensions, two-letter
  utility, duplicate reduction, and high-value tiles placed on 2L/3L squares.

The move generator searches through existing board letters in both directions;
it does not merely anagram the rack. Recommendations explicitly list
`Uses board` anchors and every newly activated premium square. Existing letters
aligned with 2L, 3L, 2W, or 3W lanes are therefore included in exhaustive move
generation and exact scoring.

Board-control scoring strongly values occupying an exposed 2W/3W and penalizes
plays that create an easy route to one, especially while ahead. Parallel plays
receive tactical credit because they form multiple cross-words while keeping
the board compact. Short hooks and legal two-letter plays remain in the search
and are labeled rather than discarded in favor of long words.

Blank and S conservation is represented in rack leave and efficiency. Spending
either carries an opportunity cost unless the move is a seven-tile sweep or a
strong multi-cross parallel play. J, Q, X, and Z receive additional credit on
2L/3L squares and a small opportunity cost on ordinary squares. Duplicate
letters reduce leave quality, so otherwise-close plays that unload duplication
rank higher.

Game phase matters. With seven or fewer tiles in the bag, future rack value is
discounted. At bag zero it is ignored entirely: Crossplay gives both players
one final turn and does not subtract leftover tiles, so immediate points and
relevant board control take priority. A seven-tile play always includes
Crossplay's 40-point sweep bonus.
