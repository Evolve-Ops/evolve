"""board_store.py — the Board's single-writer store (slice 1).

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` (D-MB1: one
writer). The store lives under the pod's shared dir and is written ONLY by
the admin daemon (this module); the bot and the mobile page both go through
daemon endpoints. Layout:

    {shared_dir}/boards/<bot_id>/
    ├── board.json                # canonical card store
    ├── events/<YYYY-MM-DD>.jsonl # append-only interaction log (learning loop §1)
    └── token.sha256              # sha256 of the per-user board token, 0600

The token is minted operator-side and shown ONCE; only its hash is stored,
so a read of the shared dir never yields a usable credential. Verification
is constant-time. There is no "no token" open mode — an unminted board
refuses every request (fail closed).

OWNERSHIP. Every writer below is reachable BOTH in-process as the admin
daemon (``evolve``) and from ``sudo evolve-admin board …`` as root, so each
one adopts what it writes to the daemon user (``_adopt_store`` ->
``board_store_perms.adopt``; a no-op unless we are root). Without that, a
root-minted 0600 token hash is one the daemon cannot open, and the board
answers a plain 401 to a link that is perfectly valid — the 2026-09-04 phone
test. ``board_store_perms`` carries the full incident note and the
``ensure_pod_perms`` drift check that re-verifies it.

Operator entry points (module-runnable to keep the size-capped ``cli.py``
untouched in this slice):

    python3 -m evolve_admin.board_store mint --bot <id> [--network <path>]
    python3 -m evolve_admin.board_store revoke --bot <id> [--network <path>]
    python3 -m evolve_admin.board_store import-tasks --bot <id> --tasks-file <p> [--network <path>]

``sudo evolve-admin board token|revoke <bot_id>`` (board_cli.py) is the
operator-facing wrapper D-MB2 specified; this module stays the
implementation, so there is one mint path, not two.

``import-tasks`` is the D-MB6 seed: it parses a Task-Manager-style markdown
task list (tables with ``| # | Task | Context | Who |`` rows) into inbox
cards, skipping completed sections. It never deletes existing cards and is
idempotent per title (a card whose title already exists is not re-added).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .board_store_perms import REPAIR_COMMAND, adopt

log = logging.getLogger(__name__)

LANES = ("inbox", "today", "later", "bot", "done")
#: D-PA5 cluster vocabulary — health and fitness deliberately separate;
#: custom clusters are allowed in card data (validated as slugs, not
#: against this tuple), this is the canonical starter set.
CLUSTERS = (
    "health", "fitness", "travel", "work", "social",
    "hobbies", "family", "home", "admin",
)

_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

SCHEMA_VERSION = 1

#: Per-card and per-board bounds (review F-8). The write API caps the request
#: BODY at 16 KB, which bounds one write but not the store: 16 KB of title,
#: repeated, is a slow unbounded growth path for anyone holding the token.
#: These are the store-side bounds — generous for a human writing a task,
#: refusing anything that is plainly not one.
MAX_TITLE_CHARS = 500
MAX_NOTE_CHARS = 4000
MAX_CARDS = 5000


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_bot_id(bot_id: str) -> str:
    """Reject anything that could traverse out of ``boards/``."""
    if not _BOT_ID_RE.match(bot_id or ""):
        raise ValueError(f"invalid bot id: {bot_id!r}")
    return bot_id


def board_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "boards" / validate_bot_id(bot_id)


def board_path(shared_dir: Path, bot_id: str) -> Path:
    return board_dir(shared_dir, bot_id) / "board.json"


def _adopt_store(shared_dir: Path, bot_id: str, *files: Path) -> None:
    """Give this bot's store (and the files just written into it) to the
    daemon user — a no-op unless we are running as root.

    Every writer here is reachable from ``sudo evolve-admin board …`` as well
    as from the daemon, and a root-written 0600 token hash is a hash the
    daemon cannot verify against. See ``board_store_perms`` for the incident.
    """
    adopt(Path(shared_dir) / "boards", board_dir(shared_dir, bot_id), *files)


def _empty_board(bot_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bot_id": bot_id,
        "updated_at": _utcnow(),
        "cards": [],
    }


def load_board(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """The board, or an empty one when nothing has been written yet."""
    p = board_path(shared_dir, bot_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_board(bot_id)
    if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
        raise ValueError(f"corrupt board store: {p}")
    return data


def save_board(shared_dir: Path, bot_id: str, board: dict[str, Any]) -> None:
    """Atomic temp+rename write, the same discipline as the arbiter stores."""
    d = board_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    board["updated_at"] = _utcnow()
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".board-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(board, f, indent=1, ensure_ascii=False)
        os.replace(tmp, board_path(shared_dir, bot_id))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _adopt_store(shared_dir, bot_id, board_path(shared_dir, bot_id))


def add_card(
    board: dict[str, Any],
    *,
    title: str,
    cluster: str,
    lane: str = "inbox",
    note: str = "",
    source: str = "manual",
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Append a card and return it. Full-length ids: an id-keyed store with
    short random ids silently overwrites on collision, so these are 32 hex
    chars and checked against the board anyway."""
    if lane not in LANES:
        raise ValueError(f"invalid lane: {lane!r}")
    if not _SLUG_RE.match(cluster or ""):
        raise ValueError(f"invalid cluster: {cluster!r}")
    title = (title or "").strip()
    if not title:
        raise ValueError("card title is required")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"card title is too long (max {MAX_TITLE_CHARS} chars)")
    if len(note or "") > MAX_NOTE_CHARS:
        raise ValueError(f"card note is too long (max {MAX_NOTE_CHARS} chars)")
    if len(board["cards"]) >= MAX_CARDS:
        raise ValueError(f"board is full (max {MAX_CARDS} cards)")
    existing = {c["id"] for c in board["cards"]}
    card_id = uuid.uuid4().hex
    while card_id in existing:  # pragma: no cover — 2^128 coincidence
        card_id = uuid.uuid4().hex
    card = {
        "id": card_id,
        "title": title,
        "note": note or "",
        "cluster": cluster,
        "lane": lane,
        "source": source,
        "created_at": _utcnow(),
    }
    if parent_id:
        card["parent_id"] = parent_id
    board["cards"].append(card)
    return card


def find_card(board: dict[str, Any], card_id: str) -> dict[str, Any] | None:
    for c in board["cards"]:
        if c.get("id") == card_id:
            return c
    return None


def move_card(
    shared_dir: Path, bot_id: str, card_id: str, to_lane: str, *, actor: str,
) -> dict[str, Any]:
    """Move one card to a lane; append the triage event. Raises KeyError on
    an unknown card, ValueError on a bad lane."""
    if to_lane not in LANES:
        raise ValueError(f"invalid lane: {to_lane!r}")
    board = load_board(shared_dir, bot_id)
    card = find_card(board, card_id)
    if card is None:
        raise KeyError(card_id)
    from_lane = card.get("lane")
    card["lane"] = to_lane
    if to_lane == "bot" and not card.get("delegation"):
        # Dragging to Bot is an OFFER (board design §3) — the bot accepts or
        # declines from here; the page only queues.
        card["delegation"] = {"state": "offered", "updated_at": _utcnow()}
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "triaged", "card": card_id, "title": card.get("title"),
        "from": from_lane, "to": to_lane, "actor": actor,
    })
    return card


def split_card(
    shared_dir: Path, bot_id: str, card_id: str,
    *, user_part: str, bot_part: str, actor: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fork a card into linked siblings (D-PA4): the user part keeps the
    original's lane (or moves to today from inbox), the bot part goes to the
    bot lane as an offer. The original is REPLACED by its children — one
    card never sits in two lanes; the event row preserves its history."""
    user_part = (user_part or "").strip()
    bot_part = (bot_part or "").strip()
    if not user_part or not bot_part:
        raise ValueError("both user_part and bot_part are required")
    board = load_board(shared_dir, bot_id)
    card = find_card(board, card_id)
    if card is None:
        raise KeyError(card_id)
    user_lane = str(card.get("lane") or "")
    if user_lane not in ("today", "later"):
        user_lane = "today"
    kid_user = add_card(board, title=user_part, cluster=card["cluster"],
                        lane=user_lane, note=card.get("note", ""),
                        source=card.get("source", "manual"), parent_id=card_id)
    kid_bot = add_card(board, title=bot_part, cluster=card["cluster"],
                       lane="bot", source=card.get("source", "manual"),
                       parent_id=card_id)
    kid_bot["delegation"] = {"state": "offered", "updated_at": _utcnow()}
    board["cards"] = [c for c in board["cards"] if c.get("id") != card_id]
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "split", "card": card_id, "title": card.get("title"),
        "into": [kid_user["id"], kid_bot["id"]], "actor": actor,
    })
    return kid_user, kid_bot


def create_card(
    shared_dir: Path, bot_id: str,
    *, title: str, cluster: str, lane: str = "inbox", note: str = "",
    source: str = "manual", actor: str = "user",
) -> dict[str, Any]:
    """Add one card and log its stocking event."""
    board = load_board(shared_dir, bot_id)
    card = add_card(board, title=title, cluster=cluster, lane=lane,
                    note=note, source=source)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "stocked", "card": card["id"], "title": card["title"],
        "cluster": cluster, "source": source, "actor": actor,
    })
    return card


def append_event(shared_dir: Path, bot_id: str, event: dict[str, Any]) -> None:
    """One JSONL row per interaction — the learning loop's raw material."""
    d = board_dir(shared_dir, bot_id) / "events"
    d.mkdir(parents=True, exist_ok=True)
    event = {"ts": _utcnow(), **event}
    day = event["ts"][:10]
    with (d / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    _adopt_store(shared_dir, bot_id, d, d / f"{day}.jsonl")


# ── token ──────────────────────────────────────────────────────────────────

def _token_path(shared_dir: Path, bot_id: str) -> Path:
    return board_dir(shared_dir, bot_id) / "token.sha256"


#: Bots whose unreadable token hash has already been reported, so a phone
#: polling every 30s produces one line and not a log flood. Per process —
#: the daemon restarts on deploy, which is exactly when the state may have
#: been repaired and is worth saying again.
_UNREADABLE_WARNED: set[str] = set()
_UNREADABLE_LOCK = threading.Lock()


def _warn_unreadable(bot_id: str, path: Path) -> None:
    """Report a token hash this process cannot open — once per bot.

    This is the line the 2026-09-04 phone test did not have. An unreadable
    hash is a POD defect (a root-owned store the ``evolve`` daemon cannot
    read), not a bad credential, and it presents as an ordinary 401 to
    everyone holding a perfectly good link.
    """
    with _UNREADABLE_LOCK:
        if bot_id in _UNREADABLE_WARNED:
            return
        _UNREADABLE_WARNED.add(bot_id)
    log.warning(
        "board token hash for %s is present but unreadable by this process "
        "(%s) — every board request will 401 with a valid link until the "
        "store is owned by the admin daemon's user. Run `%s` on the pod host.",
        bot_id, path, REPAIR_COMMAND,
    )


def mint_token(shared_dir: Path, bot_id: str) -> str:
    """Mint (or rotate) the board token. Returns the token — the only time
    it is ever visible; the store keeps just the hash, mode 0600."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    d = board_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    p = _token_path(shared_dir, bot_id)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(digest + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # The mint CLI runs under sudo, so without this the hash is root-owned at
    # 0600 and the daemon that has to verify against it gets EACCES on every
    # request — the 2026-09-04 phone test. 0600 is unchanged; only the owner
    # moves, to the account that reads it.
    _adopt_store(shared_dir, bot_id, p)
    return token


def revoke_token(shared_dir: Path, bot_id: str) -> bool:
    """Delete the bot's token hash. Returns True if one was there.

    This is the hard kill for a leaked link: with no hash file the board
    refuses every request (fail closed), including one presenting a cookie
    minted from the old token. Minting again issues a fresh, unrelated
    credential — there is no way to "un-revoke".
    """
    p = _token_path(shared_dir, bot_id)
    try:
        p.unlink()
        had = True
    except FileNotFoundError:
        had = False
    # Revoking under sudo must not leave a root-owned store behind either:
    # the next mint would land in it, and a mint that leaves the daemon
    # locked out is the whole failure this store guards against.
    _adopt_store(shared_dir, bot_id)
    return had


def _read_token_digest(shared_dir: Path, bot_id: str) -> tuple[str | None, bool]:
    """``(stored digest or None, readable)`` for this bot's token hash.

    ``readable`` is False for exactly one case: the hash file EXISTS and this
    process cannot open it. That is a pod-side ownership defect, and it is
    the case the caller must not treat as a bad credential — see
    :func:`token_store_readable`. Everything else (no file at all, a bot id
    that cannot name one, an unreadable-for-other-reasons OSError) reports
    ``True``: those are honest, fail-closed "no token here" answers.
    """
    try:
        path = _token_path(shared_dir, bot_id)
    except ValueError:  # a bot id that could never have a hash file
        return None, True
    try:
        return path.read_text(encoding="utf-8").strip(), True
    except FileNotFoundError:
        return None, True
    except PermissionError:
        _warn_unreadable(bot_id, path)
        return None, False
    except (ValueError, OSError):
        return None, True


def token_store_readable(shared_dir: Path, bot_id: str) -> bool:
    """False only when this bot's token hash exists but cannot be read.

    The caller (``routes_board._auth``) uses this to decide whether a failed
    authentication is the CLIENT's fault. An unminted or revoked board is the
    client's problem (fail closed, charge the failed-auth limiter); a hash
    the daemon cannot open is the POD's, and charging the limiter for it is
    how one broken store turned into a window in which the correct token was
    also refused. Emits the one-per-bot warning as a side effect of the read.
    """
    return _read_token_digest(shared_dir, bot_id)[1]


def verify_token(shared_dir: Path, bot_id: str, presented: str | None) -> bool:
    """Constant-time check. Missing hash file, empty token, bad bot id — all
    False; nothing here raises on hostile input.

    An UNREADABLE hash file is also False (fail closed), but it is not
    silent: it warns once per bot per process, and
    :func:`token_store_readable` lets the route tell the two apart.
    """
    if not presented:
        return False
    stored, _readable = _read_token_digest(shared_dir, bot_id)
    if stored is None:
        return False
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(stored, digest)


# ── D-MB6 importer ─────────────────────────────────────────────────────────

#: Section-heading → skip. Completed work is history, not a card.
_DONE_HEADING = re.compile(r"^##.*COMPLETED", re.IGNORECASE)

#: Crude cluster guesser for imported rows. The user re-clusters by triage;
#: a wrong guess costs one drag, a missing card costs a forgotten task —
#: so the importer prefers guessing to dropping.
_CLUSTER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("health", ("dr.", "doctor", "dental", "crown", "cardio", "scan", "medical",
                "dexa", "rx", "appointment", "derm", "eye exam", "sleep")),
    ("travel", ("trip", "flight", "hotel", "vegas", "travel")),
    ("fitness", ("gym", "training", "workout", "tennis", "run ", "whoop")),
    ("work", ("integration", "backup", "cron", "api", "database", "system")),
)


def _guess_cluster(text: str, section: str) -> str:
    hay = text.lower()
    for cluster, words in _CLUSTER_HINTS:
        if any(w in hay for w in words):
            return cluster
    if "technical" in section.lower():
        return "work"
    return "admin"


def import_tasks_md(text: str) -> list[dict[str, str]]:
    """Parse ``| # | Task | Context | ... |`` markdown tables into
    ``{title, note, cluster}`` rows, skipping completed sections, header
    rows, separator rows, and struck-through (``~~``) items."""
    rows: list[dict[str, str]] = []
    section = ""
    skipping = False
    for line in text.splitlines():
        if line.startswith("##"):
            section = line
            skipping = bool(_DONE_HEADING.match(line))
            continue
        if skipping or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        title = cells[1]
        if (not title or set(title) <= {"-", " ", ":"}
                or title.lower() in ("task", "tasks")
                or title.startswith("~~") or title.startswith("✅")):
            continue
        note = cells[2] if len(cells) > 2 else ""
        rows.append({
            "title": title,
            "note": note,
            "cluster": _guess_cluster(f"{title} {note}", section),
        })
    return rows


def import_tasks_into_board(shared_dir: Path, bot_id: str, tasks_text: str) -> int:
    """Idempotent-by-title seed of the board from a markdown task list.
    Returns how many cards were added."""
    board = load_board(shared_dir, bot_id)
    have = {c["title"] for c in board["cards"]}
    added = 0
    for row in import_tasks_md(tasks_text):
        if row["title"] in have:
            continue
        add_card(board, title=row["title"], note=row["note"],
                 cluster=row["cluster"], lane="inbox", source="import")
        have.add(row["title"])
        added += 1
    if added:
        save_board(shared_dir, bot_id, board)
        append_event(shared_dir, bot_id,
                     {"event": "imported", "cards_added": added, "actor": "operator"})
    return added


# ── module entry points ────────────────────────────────────────────────────

def _resolve_shared_dir(network: str | None) -> Path:
    from .config import CANONICAL_SHARED_DIR, load_network
    if network:
        return Path(load_network(Path(network)).get("sharedDir", CANONICAL_SHARED_DIR))
    return Path(CANONICAL_SHARED_DIR)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evolve_admin.board_store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_mint = sub.add_parser("mint", help="mint (or rotate) a bot's board token")
    p_mint.add_argument("--bot", required=True)
    p_mint.add_argument("--network", default=None)
    p_rev = sub.add_parser("revoke", help="revoke a bot's board token")
    p_rev.add_argument("--bot", required=True)
    p_rev.add_argument("--network", default=None)
    p_imp = sub.add_parser("import-tasks", help="seed the board from a markdown task list")
    p_imp.add_argument("--bot", required=True)
    p_imp.add_argument("--tasks-file", required=True)
    p_imp.add_argument("--network", default=None)
    args = ap.parse_args(argv)
    shared = _resolve_shared_dir(args.network)
    if args.cmd == "mint":
        token = mint_token(shared, args.bot)
        print("Board token (shown once — the store keeps only its hash):")
        print(token)
        print(f"Page URL path: /board/{args.bot}?t={token}")
        return 0
    if args.cmd == "revoke":
        had = revoke_token(shared, args.bot)
        print(f"revoked board token for {args.bot}" if had
              else f"no board token to revoke for {args.bot}")
        return 0
    added = import_tasks_into_board(
        shared, args.bot, Path(args.tasks_file).read_text(encoding="utf-8"))
    print(f"imported {added} card(s) into {board_path(shared, args.bot)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
