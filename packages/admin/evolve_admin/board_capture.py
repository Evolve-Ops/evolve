"""board_capture.py — D-TM4 capture from turns, stage two (the daemon).

Design ``design-pa-tasks-and-follow-through-2026-09-18.md`` D-TM4; D-AP1 row
``tracker.propose``. The plugin's free gate posts here only when it fires.
**Classify**: ONE fast-rung, tool-less call, priced against the Board app's
per-call cap before it runs (unpriced ⇒ refused, D-PA1). **Dedup**: same turn
⇒ nothing; a repeat title ⇒ "mentioned again". **Land**: the user's words ⇒ a
``kind: proposal`` card (no pace until kept); the bot's promise ⇒ an ``owner:
bot`` card paced now. Owner/``touch_action`` come from the speaker and a
stated day is parsed here — never left to the model. Writes: ``board_store``
under ``WRITE_LOCK`` only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import app_contract, board_store

#: The Board app (gallery ``p-aed7721c``) — the app the classifier's spend is attributed to.
CAPTURE_APP_ID = "app_morning_board"
PROPOSAL_KIND = "proposal"
PROPOSAL_TTL_DAYS = 7
EXPIRED_REASON = "expired"
SPEAKERS = ("user", "bot")
MAX_EXCERPT_CHARS = 600
MAX_OPEN_TITLES = 40
DUPLICATE_OVERLAP = 0.6
#: Per-call cap (USD), overridable at ``network.json::pod.board_capture.per_call_usd``.
DEFAULT_PER_CALL_USD = 0.01
CLASSIFIER_MAX_TOKENS = 300
GATE_EVENT = "capture_gate"

Classifier = Callable[..., "tuple[dict[str, Any] | None, dict[str, Any]]"]

_SYSTEM = ("You extract at most one commitment from one chat excerpt. "
           "Reply with ONLY a JSON object, no prose.")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.I)
_ORDINAL_RE = re.compile(r"\bthe (\d{1,2})(st|nd|rd|th)\b", re.I)


def _utc(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def pod_zone(network: dict[str, Any]) -> Any:
    from .config import resolve_pod_timezone
    try:
        return ZoneInfo(resolve_pod_timezone(network))
    except Exception:  # noqa: BLE001 — an unknown zone name is UTC, not a crash
        return timezone.utc


def stated_day(text: str, today: datetime) -> str | None:
    """The day named ("Thursday" → the next one, "tomorrow", "the 30th"), or None."""
    low = text.lower()
    base = today.date()
    if "tomorrow" in low:
        return (base + timedelta(days=1)).isoformat()
    m = _WEEKDAY_RE.search(low)
    if m:
        ahead = (_WEEKDAYS.index(m.group(1)) - base.weekday()) % 7 or 7
        return (base + timedelta(days=ahead)).isoformat()
    m = _ORDINAL_RE.search(low)
    days = (base + timedelta(days=k) for k in range(62))
    return next((d.isoformat() for d in days if m and d.day == int(m.group(1))), None)


def _valid_day(value: Any) -> str | None:
    return value if isinstance(value, str) and board_store._parse_date(value) else None  # noqa: SLF001


def _words(title: str) -> set[str]:
    return {w for w in board_store.normalise_title(title).split() if len(w) > 2}


def find_duplicate(open_cards: list[dict[str, Any]], title: str,
                   duplicate_of: str | None = None) -> dict[str, Any] | None:
    """An open card the candidate repeats: the model named its title, or the
    two titles share ≥ :data:`DUPLICATE_OVERLAP` of their words (the
    continuity engine's word-overlap rule — catches paraphrases)."""
    mine = _words(title)
    for card in open_cards:
        if duplicate_of and card.get("title") == duplicate_of:
            return card
        theirs = _words(card.get("title") or "")
        if mine and theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= DUPLICATE_OVERLAP:
            return card
    return None


def _open_cards(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in board.get("cards", []) if c.get("lane") not in board_store.SETTLED_LANES]


def build_prompt(excerpt: str, speaker: str, titles: list[str], today: datetime) -> str:
    who = "the user" if speaker == "user" else "the assistant bot (a promise it made)"
    listed = "\n".join(f"- {t}" for t in titles) or "- (none)"
    return (
        f"Today is {today:%Y-%m-%d} ({today:%A}). Speaker: {who}.\n"
        f"Open cards (if the excerpt repeats one, name it in duplicate_of):\n{listed}\n"
        f'Excerpt:\n"""\n{excerpt}\n"""\n'
        "If the excerpt holds a concrete commitment (something to be done later), reply "
        '{"title": "<imperative, <=80 chars>", "outcome": "<one sentence: what done looks like>", '
        '"cadence": "now|today|soon|later|someday", "due": "YYYY-MM-DD or null (hard deadline)", '
        '"when": "YYYY-MM-DD or null (the day promised)", "source_named": true|false, '
        '"duplicate_of": "<open card title or null>"}. Otherwise reply {"none": true}.'
    )


def fast_rung_classifier(*, prompt: str, shared_dir: Path, bot_id: str,
                         network: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The production classifier: the bot's fast rung, priced before the call."""
    chain = app_contract.model_tier_chain(network, bot_id, "fast")
    model = chain[0] if chain else ""
    provider, _, bare = model.partition("/")
    rec = app_contract.model_price(shared_dir, provider, bare) if bare else None
    in_raw = (rec or {}).get("input_cost_per_token")
    out_raw = (rec or {}).get("output_cost_per_token")
    if not model or in_raw is None or out_raw is None:
        return None, {"outcome": "unpriced", "model": model or None, "cost_usd": 0.0}
    in_rate, out_rate = float(in_raw), float(out_raw)
    est = (len(prompt) + len(_SYSTEM)) / 4 * in_rate + CLASSIFIER_MAX_TOKENS * out_rate
    cap = ((network.get("pod") or {}).get("board_capture") or {}).get("per_call_usd")
    if est > (cap if isinstance(cap, (int, float)) else DEFAULT_PER_CALL_USD):
        return None, {"outcome": "over_budget", "model": model, "cost_usd": 0.0,
                      "est_usd": round(est, 6)}
    from engine_llm import engine_complete, extract_json_object  # type: ignore
    text, outcome = engine_complete(prompt, job="board_capture", shared_dir=shared_dir,
                                    model_hint=model, role="fast", system=_SYSTEM,
                                    max_tokens=CLASSIFIER_MAX_TOKENS, timeout=30)
    cost = (len(prompt) + len(_SYSTEM)) / 4 * in_rate + len(text or "") / 4 * out_rate
    meta = {"outcome": outcome, "model": model, "cost_usd": round(cost if text else 0.0, 6)}
    if not text:
        return None, meta
    return extract_json_object(text, job="board_capture", shared_dir=shared_dir), meta


def validate_request(raw: Any) -> dict[str, Any]:
    """The plugin's capture request, normalised — or ``ValueError``."""
    if not isinstance(raw, dict):
        raise ValueError("capture must be an object")
    out = {k: str(raw.get(k) or "").strip() for k in ("session", "turn_id", "speaker", "text_excerpt")}
    if out["speaker"] not in SPEAKERS:
        raise ValueError(f"speaker must be one of {list(SPEAKERS)}")
    if not out["turn_id"] or not out["text_excerpt"]:
        raise ValueError("turn_id and text_excerpt are required")
    return {**out, "text_excerpt": out["text_excerpt"][:MAX_EXCERPT_CHARS],
            "session": out["session"][:200] or "unknown", "turn_id": out["turn_id"][:200]}


def record_gate(shared_dir: Path, bot_id: str, gate: Any, *, now: datetime | None = None) -> None:
    """The gate's counters, as reported by the plugin — the health control's evidence."""
    if not isinstance(gate, dict):
        return
    counts = {k: int(gate.get(k) or 0) for k in ("evaluated", "matched")
              if isinstance(gate.get(k), (int, float))}
    with board_store.WRITE_LOCK:
        board_store.append_event(shared_dir, bot_id, {"event": GATE_EVENT, **counts}, now=now)


def propose(shared_dir: Path, bot_id: str, network: dict[str, Any], request: dict[str, Any], *,
            classifier: Classifier | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """Stages two and three for one gated excerpt. Returns ``{"outcome": ...,
    "card": ...}`` — ``duplicate_turn`` / ``none`` / ``mentioned_again`` /
    ``proposed`` / ``promised`` / a classifier refusal (``unpriced`` …)."""
    now = _utc(now)
    req = validate_request(request)
    expire_proposals(shared_dir, bot_id, now=now)
    board = board_store.load_board(shared_dir, bot_id)
    if any((c.get("capture") or {}).get("turn_id") == req["turn_id"]
           and (c.get("capture") or {}).get("speaker") == req["speaker"] for c in board["cards"]):
        return {"outcome": "duplicate_turn", "card": None}
    tz = pod_zone(network)
    today = now.astimezone(tz)
    titles = [c.get("title") or "" for c in _open_cards(board)][-MAX_OPEN_TITLES:]
    prompt = build_prompt(req["text_excerpt"], req["speaker"], titles, today)
    kw = {"prompt": prompt, "shared_dir": Path(shared_dir), "bot_id": bot_id, "network": network}
    result, meta = classifier(**kw) if classifier else fast_rung_classifier(**kw)
    with board_store.WRITE_LOCK:
        board_store.append_event(shared_dir, bot_id, {
            "event": "capture_classified", "turn_id": req["turn_id"], "speaker": req["speaker"],
            "app_id": CAPTURE_APP_ID, "found": bool(result and result.get("title")), **meta,
        }, now=now)
    if not result or result.get("none") or not str(result.get("title") or "").strip():
        return {"outcome": meta.get("outcome") if meta.get("outcome") not in (None, "ok") else "none",
                "card": None}
    return _land(shared_dir, bot_id, req, result, tz=tz, now=now)


def _land(shared_dir: Path, bot_id: str, req: dict[str, Any], result: dict[str, Any], *,
          tz: Any, now: datetime) -> dict[str, Any]:
    title = str(result["title"]).strip()[:board_store.MAX_TITLE_CHARS]
    ts = board_store._utcnow(now)  # noqa: SLF001
    with board_store.WRITE_LOCK:
        board = board_store.load_board(shared_dir, bot_id)
        dup = find_duplicate(_open_cards(board), title, result.get("duplicate_of"))
        if dup is not None:
            dup.setdefault("touches", []).append({
                "at": ts, "action": "mention", "result": "mentioned again",
                "actor": "capture", "turn_id": req["turn_id"]})
            board_store.save_board(shared_dir, bot_id, board)
            board_store.append_event(shared_dir, bot_id, {
                "event": "mentioned_again", "card": dup["id"], "title": dup.get("title"),
                "turn_id": req["turn_id"], "actor": "capture"}, now=now)
            return {"outcome": "mentioned_again", "card": dup}
        local = now.astimezone(tz)
        bot = req["speaker"] == "bot"
        stated = stated_day(req["text_excerpt"], local)
        due = _valid_day(result.get("due")) or (None if bot else stated)
        cadence = result.get("cadence") if result.get("cadence") in board_store.CADENCE_CLASSES else "soon"
        touch_action = ("check_source" if result.get("source_named") else "ask") if bot else "remind"
        stamp = f"{local:%a %d %b %H:%M}"
        card = board_store.add_card(
            board, title=title, cluster="admin", lane="inbox", source="turn",
            owner="bot" if bot else "me", outcome=str(result.get("outcome") or "")[:500] or None,
            due=due, created_at=ts, enrichment={"why_saved": {
                "value": f"promised in chat {stamp}" if bot else f"proposed from chat {stamp} — keep?",
                "source": f"turn:{req['turn_id']}"}})
        card["capture"] = {"session": req["session"], "turn_id": req["turn_id"],
                           "speaker": req["speaker"], "excerpt": req["text_excerpt"],
                           "captured_at": ts}
        if bot:
            card["delegation"] = {"state": "accepted", "updated_at": ts}
            card["cadence"], card["touch_action"] = cadence, touch_action
            when = _valid_day(result.get("when")) or stated
            if when:
                day = datetime.strptime(when, "%Y-%m-%d").replace(hour=9, tzinfo=tz)
                card["next_touch"] = board_store._utcnow(max(day.astimezone(timezone.utc), now))  # noqa: SLF001
            else:
                card["cadence"] = "soon"
                card["next_touch"] = board_store.next_touch_for(card, now, tz=tz)
        else:
            card["kind"] = PROPOSAL_KIND
            card["proposed_pace"] = {"cadence": cadence, "touch_action": touch_action}
        board_store.save_board(shared_dir, bot_id, board)
        board_store.append_event(shared_dir, bot_id, {
            "event": "promised" if bot else "proposed", "card": card["id"], "title": title,
            "turn_id": req["turn_id"], "next_touch": card.get("next_touch"), "actor": "capture",
        }, now=now)
    return {"outcome": "promised" if bot else "proposed", "card": card}


def keep_proposal(shared_dir: Path, bot_id: str, card_id: str, *, actor: str,
                  tz: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Swipe up on a proposal: ``kind`` cleared, pace set from the table.
    Caller holds ``WRITE_LOCK``."""
    board = board_store.load_board(shared_dir, bot_id)
    card = board_store.resolve_card(board, card_id)
    if card.get("kind") != PROPOSAL_KIND:
        raise ValueError("that card is not a proposal")
    pace = card.pop("proposed_pace", None) or {}
    card.pop("kind", None)
    card.setdefault("capture", {})["kept_at"] = board_store._utcnow(now)  # noqa: SLF001
    board_store.save_board(shared_dir, bot_id, board)
    board_store.append_event(shared_dir, bot_id, {
        "event": "proposal_kept", "card": card["id"], "title": card.get("title"), "actor": actor,
    }, now=now)
    return board_store.set_pace(shared_dir, bot_id, card["id"], cadence=pace.get("cadence") or "soon",
                                touch_action=pace.get("touch_action") or "remind",
                                actor=actor, tz=tz, now=now)


def expire_proposals(shared_dir: Path, bot_id: str, *, now: datetime | None = None) -> int:
    """Drop, reason ``expired``, every proposal older than the TTL."""
    now = _utc(now)
    cutoff = now - timedelta(days=PROPOSAL_TTL_DAYS)
    with board_store.WRITE_LOCK:
        board = board_store.load_board(shared_dir, bot_id)
        stale = [c["id"] for c in board["cards"]
                 if c.get("kind") == PROPOSAL_KIND and c.get("lane") == "inbox"
                 and (board_store._parse_ts(c.get("created_at")) or now) <= cutoff]  # noqa: SLF001
        for cid in stale:
            board_store.move_card(shared_dir, bot_id, cid, "dropped", actor="system",
                                  reason=EXPIRED_REASON, now=now)
    return len(stale)


def weekly_capture_stats(shared_dir: Path, bot_id: str, *,
                         now: datetime | None = None) -> dict[str, int]:
    """D-TM10's capture half: proposals made in the last 7 days, and of
    those how many were kept and how many dropped (``expired`` included)."""
    since = _utc(now) - timedelta(days=7)
    board = board_store.load_board(shared_dir, bot_id)
    mine = [c for c in board["cards"] if (c.get("capture") or {}).get("speaker") == "user"
            and (board_store._parse_ts(c.get("created_at")) or since) > since]  # noqa: SLF001
    return {"captures_proposed": len(mine),
            "captures_kept": sum(1 for c in mine if c["capture"].get("kept_at")),
            "captures_dropped": sum(1 for c in mine if c.get("lane") == "dropped")}


def gate_verdict(shared_dir: Path, bot_id: str, *, turns_seen: bool,
                 now: datetime | None = None) -> tuple[str, str]:
    """"capture gate live": ``ok`` / ``unknown`` (turns seen, gate silent) / ``idle``."""
    now = _utc(now)
    events_dir = board_store.board_dir(shared_dir, bot_id) / "events"
    last: dict[str, Any] | None = None
    for d in range(7, -1, -1):
        p = events_dir / f"{(now - timedelta(days=d)).date().isoformat()}.jsonl"
        try:
            rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if GATE_EVENT in x]
        except (OSError, ValueError):
            continue
        last = next((r for r in reversed(rows) if r.get("event") == GATE_EVENT), last)
    if last is not None:
        return "ok", (f"live — last report {last.get('ts')}: {last.get('evaluated', 0)} turns "
                      f"evaluated, {last.get('matched', 0)} matched since the gateway started")
    if turns_seen:
        return "unknown", ("turns were observed this week but the capture gate never "
                           "reported — it may not be registered on this gateway")
    return "idle", "no turns observed this week"

