"""board_project.py — the Project Manager application on the Tracker (D-TM9/11/13).

Design: ``internal/design-pa-tasks-and-follow-through-2026-09-18.md`` §0, §3
(D-TM9 parity first, D-TM11 the two applications, D-TM13 handoff). The Task
Manager app's Slack surface, rebuilt on the Tracker's ``project`` list:

* **ingest** — structured form-bot reports (``<category> in <area> <text>
  SEVERITY: n Reported by: X. <mentions>``) become cards with ``reporter`` /
  ``severity`` / ``area`` and ``source_id = slack:<channel>:<ts>`` (dedup by
  ``ts``); a ✅ reaction on the source message resolves the card; a report
  that already carries ✅ when first seen is created ``done`` with a note
  (the operator's 09-04 ask: resolved reports are logged too).
* **commands** — only on a message addressed to the bot (``<@bot_user>``)
  or arriving from the report bot; everything else is not our lane and is
  ignored: ``create|add <text>``, "add <text> to the agenda", ``assign <id>
  <@member>|bot``, ``resolve <id>``, ``status [<id> <open|in_progress|
  blocked>]``, ``list``, ``show <id>``. Ids are the list's human ids.
* **weekly report** — :func:`render_weekly_report`, deterministic text from
  the list; posted by :func:`tick` on the list's ``report_weekday``/
  ``report_hour``.
* **follow-through** — :func:`due_nags`: blocked > ``nag_days``, open past
  ``due``, and unassigned > ``nag_days`` become D-TM3 ``remind`` touches
  addressed to the assignee (or the list owner when unassigned).
* **handoff** — :func:`handoff_to_member` / :func:`handoff_to_bot`: two
  linked cards; settling either settles both (the store enforces it).

PLATFORM RULE (D-AP1). This is an ``app`` module: it reaches the Tracker,
delivery and Slack history ONLY through :mod:`evolve_admin.app_contract`
(``tracker.*``, ``delivery.send_to_channel``, ``channels.read_history``). No
state of its own (the report and ingest cursors are list fields), no model —
ingest, report and nags are string work (``ZERO_MODEL_ROOTS`` pins it),
recipients come from the list's own ``members[].slack_user``.

Deviation, disclosed: the live app has no deterministic parser or report to
"port as-is". Its report is written by a model from a cron prompt (the bot
said so in Slack on 2026-09-04) and its wording changed three Fridays in a
row; the parser and the report format here are written from the posted
messages (the 09-11 post, which carries every section, plus the 09-04
hot-spot line). ``evolve-admin project parity`` diffs against whatever the
old app last posted, so every difference is shown for the operator to accept.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import app_contract as ac

#: Reactions that mean "resolved" on a source message.
CHECK_REACTIONS = ("white_check_mark", "heavy_check_mark", "ballot_box_with_check")

#: The form bot's message shape, as it arrives in the channel.
REPORT_RE = re.compile(
    r"^\s*(?P<category>.+?) in (?P<area>\S+) (?P<text>.*?)\s*"
    r"SEVERITY:\s*(?P<sev>\d)\s*Reported by:\s*(?P<reporter>[^.]*)\.\s*"
    r"(?P<mentions>.*)$", re.S)

#: Severity words as the posted report prints them ("Sev 3 (medium)").
SEVERITY_WORDS = {1: "minor", 2: "low", 3: "medium", 4: "high", 5: "critical"}

#: How many backlog lines the report prints before "… and N more".
BACKLOG_LINES = 15
HOT_SPOT_MIN = 3
DEFAULT_NAG_DAYS = 3
DEFAULT_TITLE = "Weekly Report"
DEFAULT_AREA_LABEL = "area"

_ID_RE = re.compile(r"\b([A-Z]{2,6}-\d{1,6})\b")
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_AGENDA_RE = re.compile(r"^add (?P<t>.+?) to the agenda\.?$", re.I | re.S)
_CREATE_RE = re.compile(r"^(?:create|add|track|log|report)\s*:?\s+(?P<t>.+)$", re.I | re.S)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: Any) -> datetime | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ts_dt(ts: Any) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def project_config(lst: dict[str, Any]) -> dict[str, Any]:
    return dict((lst.get("defaults") or {}).get("project") or {})


def _find_list(board: dict[str, Any], list_id: str) -> dict[str, Any]:
    for lst in board.get("lists") or []:
        if lst.get("list_id") == list_id and lst.get("shape") == "project":
            return lst
    raise ValueError(f"no project list {list_id!r}")


def _list_cards(board: dict[str, Any], list_id: str) -> list[dict[str, Any]]:
    return [c for c in board.get("cards") or [] if c.get("list_id") == list_id]


def _by_human_id(board: dict[str, Any], list_id: str, hid: str) -> dict[str, Any] | None:
    return next((c for c in _list_cards(board, list_id) if c.get("human_id") == hid), None)


def _checked_by(msg: dict[str, Any]) -> str | None:
    """The first user who ✅-reacted, or None (a ✅ with no users → "slack")."""
    for r in msg.get("reactions") or []:
        if r.get("name") in CHECK_REACTIONS:
            return (r.get("users") or ["slack"])[0]
    return None


def parse_report(text: str) -> dict[str, Any] | None:
    """One form-bot report → ``{title, area, severity, reporter, mentions,
    category}``, or None when the text is not in the form's shape."""
    m = REPORT_RE.match(text or "")
    if m is None or not m.group("text").strip():
        return None
    sev = int(m.group("sev"))
    reporter = m.group("reporter").strip()
    return {
        "title": " ".join(m.group("text").split()).rstrip(".")[:500],
        "area": m.group("area").strip().capitalize(),
        "severity": sev if 1 <= sev <= 5 else None,
        "reporter": None if reporter.upper() in ("", "NONE") else reporter,
        "mentions": _MENTION_RE.findall(m.group("mentions")),
        "category": m.group("category").strip(),
    }


def parse_loose(text: str) -> str | None:
    """The loose-text detector: a create request's title, or None."""
    text = (text or "").strip()
    for rx in (_AGENDA_RE, _CREATE_RE):
        m = rx.match(text)
        if m:
            return " ".join(m.group("t").split()).rstrip(".")[:500] or None
    return None


# ── ingest ─────────────────────────────────────────────────────────────────


def ingest(
    shared_dir: Path, bot_id: str, list_id: str, messages: list[dict[str, Any]],
    *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Replay *messages* (Slack ``conversations.history`` rows, any order)
    into the list. Idempotent by ``ts``. Returns one ``{ts, reply}`` per
    command that answers in the channel.

    Two guards, because a tick re-reads a 14-day window every run: a card's
    ``source_id`` dedups what made a card, and the list's
    ``cursors.ingested_through`` (the newest ``ts`` already handled) keeps a
    command that made NO card — ``assign``, ``resolve``, ``show``, a "can't
    find" — from being answered again. Behind the cursor only the ✅ scan
    still runs: a reaction lands after its message, and the lane check makes
    it a no-op once the card is settled."""
    now = now or datetime.now(timezone.utc)
    board = ac.tracker_read(shared_dir, bot_id)
    lst = _find_list(board, list_id)
    cfg = project_config(lst)
    channel = cfg.get("channel") or "channel"
    cursor = float((lst.get("cursors") or {}).get("ingested_through") or 0)
    through: str | None = None
    replies: list[dict[str, Any]] = []
    for msg in sorted(messages, key=lambda m: float(m.get("ts") or 0)):
        ts = str(msg.get("ts") or "")
        if not ts or _ts_dt(ts) > now:
            continue
        if float(ts) > max(cursor, float(through or 0)):
            through = ts
        source_id = f"slack:{channel}:{ts}"
        board = ac.tracker_read(shared_dir, bot_id)
        existing = next((c for c in _list_cards(board, list_id)
                         if c.get("source_id") == source_id), None)
        checker = _checked_by(msg)
        if existing is not None:
            if checker and existing.get("lane") not in ("done", "dropped"):
                ac.tracker_move(shared_dir, bot_id, existing["id"], "done",
                                actor=f"slack:{checker}", now=now)
            continue
        if float(ts) <= cursor:
            continue  # handled on an earlier run — never answer it twice
        text = msg.get("text") or ""
        sender = msg.get("user") or msg.get("bot_id") or ""
        report = parse_report(text) if sender == cfg.get("report_bot") else None
        if report is not None:
            _create_from_report(shared_dir, bot_id, list_id, report, ts, source_id,
                                checker, now=now)
            continue
        bot_user = cfg.get("bot_user")
        if not bot_user or f"<@{bot_user}>" not in text:
            continue  # not addressed to us — not my lane
        body = _MENTION_RE.sub(lambda m: "" if m.group(1) == bot_user else m.group(0),
                               text, count=1).strip()
        reply = _command(shared_dir, bot_id, list_id, body, actor=f"slack:{sender}",
                         ts=ts, source_id=source_id, now=now)
        if reply:
            replies.append({"ts": ts, "reply": reply})
    if through is not None:
        ac.tracker_set_cursor(shared_dir, bot_id, list_id, "ingested_through", through)
    return replies


def _create_from_report(
    shared_dir: Path, bot_id: str, list_id: str, report: dict[str, Any], ts: str,
    source_id: str, checker: str | None, *, now: datetime,
) -> dict[str, Any]:
    note = f"{report['category']} · mentions: {' '.join(report['mentions']) or '-'}"
    if checker:
        note += " · resolved on arrival (✅ already on the report)"
    return ac.tracker_create_card(
        shared_dir, bot_id, title=report["title"], cluster="admin",
        lane="done" if checker else "inbox", note=note, source="slack",
        source_id=source_id, actor=f"slack:{report['reporter'] or 'report-bot'}",
        list_id=list_id, reporter=report["reporter"], severity=report["severity"],
        area=report["area"], created_at=_iso(_ts_dt(ts)), now=now)


def _member_for_slack(lst: dict[str, Any], slack_user: str) -> str | None:
    return next((m["id"] for m in lst.get("members") or []
                 if m.get("slack_user") == slack_user), None)


def _command(
    shared_dir: Path, bot_id: str, list_id: str, body: str, *, actor: str,
    ts: str, source_id: str, now: datetime,
) -> str | None:
    board = ac.tracker_read(shared_dir, bot_id)
    lst = _find_list(board, list_id)
    verb, _, rest = body.partition(" ")
    verb, rest = verb.lower(), rest.strip()
    hid_m = _ID_RE.search(rest)
    card = _by_human_id(board, list_id, hid_m.group(1)) if hid_m else None
    if verb in ("assign", "resolve", "show") or (verb == "status" and rest):
        if card is None:
            return f"I can't find {hid_m.group(1) if hid_m else 'that item'} on {lst['name']}."
        return _card_command(shared_dir, bot_id, lst, card, verb, rest, actor=actor, now=now)
    if verb == "status":
        open_ = [c for c in _list_cards(board, list_id) if not _settled(c)]
        blocked = sum(1 for c in open_ if c.get("pm_status") == "blocked")
        return f"{lst['name']}: {len(open_)} open, {blocked} blocked."
    if verb == "list":
        open_ = sorted((c for c in _list_cards(board, list_id) if not _settled(c)),
                       key=lambda c: c.get("human_id") or "")
        return "\n".join(f"• {c.get('human_id')} — {c['title']}" for c in open_) or "Nothing open."
    title = parse_loose(body)
    if title is None:
        return None
    new = ac.tracker_create_card(
        shared_dir, bot_id, title=title, cluster="admin", source="slack",
        source_id=source_id, actor=actor, list_id=list_id, reporter=actor,
        created_at=_iso(_ts_dt(ts)), now=now)
    return f"Added {new['human_id']} — {title}."


def _card_command(
    shared_dir: Path, bot_id: str, lst: dict[str, Any], card: dict[str, Any],
    verb: str, rest: str, *, actor: str, now: datetime,
) -> str:
    hid = card["human_id"]
    after = rest.split(hid, 1)[1].strip()
    if verb == "assign":
        if re.search(r"\bbot\b", after, re.I):
            handoff_to_bot(shared_dir, bot_id, card["id"], actor=actor, now=now)
            return f"{hid} is mine — I'll follow it through."
        who = _MENTION_RE.search(after)
        member = _member_for_slack(lst, who.group(1)) if who else None
        if who is None or member is None:
            return f"Who should take {hid}? Mention a member of {lst['name']}."
        ac.tracker_update(shared_dir, bot_id, card["id"], assignee=member, actor=actor, now=now)
        return f"{hid} assigned to <@{who.group(1)}>."
    if verb == "resolve":
        ac.tracker_move(shared_dir, bot_id, card["id"], "done", actor=actor, now=now)
        return f"{hid} resolved."
    if verb == "status":
        state = after.lower().replace(" ", "_")
        if state not in ("open", "in_progress", "blocked"):
            return f"Status for {hid} is one of open, in progress, blocked."
        ac.tracker_update(shared_dir, bot_id, card["id"], pm_status=state, actor=actor, now=now)
        return f"{hid} is {state.replace('_', ' ')}."
    lines = [f"{hid} — {card['title']}",
             f"Status: {'resolved' if _settled(card) else card.get('pm_status', 'open')}"
             f" · Sev {card.get('severity') or '?'} · {card.get('area') or 'Unknown'}"
             f" · assignee {card.get('assignee') or 'none'}"]
    lines += [f"  {h['at'][:10]} {h['what']} ({h['actor']})" for h in card.get("history") or []]
    return "\n".join(lines)


def _settled(card: dict[str, Any]) -> bool:
    return card.get("lane") in ("done", "dropped")


# ── weekly report ──────────────────────────────────────────────────────────


def _age_days(card: dict[str, Any], now: datetime) -> int | None:
    created = _parse_iso(card.get("created_at"))
    return None if created is None else (now - created).days


def _line(card: dict[str, Any], *, age: int | None = None) -> str:
    bits = [f"Sev {card.get('severity') or '?'}", card["title"]]
    if card.get("area"):
        bits.append(card["area"])
    tail = f" ({age}d old)" if age is not None and age > 0 else ""
    return "• " + " — ".join(bits) + tail


def _sev_key(card: dict[str, Any]) -> int:
    return -(card.get("severity") or 0)


def render_weekly_report(
    board: dict[str, Any], list_id: str, *, now: datetime, tz: Any = timezone.utc,
) -> str:
    """The Friday post, from the list alone — same sections in the same
    order every week: header + window, this week's count, resolved/open, by
    area, by severity, opened-this-week-still-open, full backlog with ages,
    hot spots. Deterministic: byte-equal for the same store and ``now``."""
    lst = _find_list(board, list_id)
    cfg = project_config(lst)
    cards = _list_cards(board, list_id)
    since = now - timedelta(days=7)
    week = [c for c in cards if (_parse_iso(c.get("created_at")) or now) > since]
    open_week = sorted((c for c in week if not _settled(c)), key=_sev_key)
    backlog = sorted((c for c in cards if not _settled(c)),
                     key=lambda c: (_sev_key(c) if c.get("severity") else 1,
                                    -(_age_days(c, now) or 0)))
    start, end = since.astimezone(tz), now.astimezone(tz)
    out = [f":bar_chart: _{cfg.get('report_title') or DEFAULT_TITLE}_",
           f"_{start:%b %d} – {end:%b %d, %Y}_", "",
           f"_Total issues reported:_ {len(week)}",
           f"_Resolved:_ {len(week) - len(open_week)} | _Still open:_ {len(open_week)}"]
    areas: dict[str, int] = {}
    for c in week:
        areas[c.get("area") or "Unknown"] = areas.get(c.get("area") or "Unknown", 0) + 1
    if areas:
        out += ["", f"_By {cfg.get('area_label') or DEFAULT_AREA_LABEL}:_"]
        out += [f"• {a}: {n}" for a, n in sorted(areas.items(), key=lambda kv: (-kv[1], kv[0]))]
    sevs: dict[int, int] = {}
    for c in week:
        if c.get("severity"):
            sevs[c["severity"]] = sevs.get(c["severity"], 0) + 1
    if sevs:
        out += ["", "_By severity:_"]
        out += [f"• Sev {s} ({SEVERITY_WORDS[s]}): {n}" for s, n in sorted(sevs.items(), reverse=True)]
    if open_week:
        out += ["", f"_Opened this week, still open ({len(open_week)}):_"]
        out += [_line(c) for c in open_week]
    if backlog:
        out += ["", f"_Full backlog still open (all-time, {len(backlog)}):_"]
        out += [_line(c, age=_age_days(c, now)) for c in backlog[:BACKLOG_LINES]]
        if len(backlog) > BACKLOG_LINES:
            out.append(f"… and {len(backlog) - BACKLOG_LINES} more")
    hot = sorted(((a, n) for a, n in areas.items() if n >= HOT_SPOT_MIN and a != "Unknown"),
                 key=lambda kv: (-kv[1], kv[0]))
    out.append("")
    if hot:
        out.append(f":fire: *Hot spots* ({HOT_SPOT_MIN}+ reports in past 7 days):")
        out += [f"• {a}: {n}" for a, n in hot]
    else:
        out.append(f":white_check_mark: *No hot spots* detected ({HOT_SPOT_MIN}+ reports in past 7 days).")
    return "\n".join(out)


def find_old_report(messages: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The newest weekly report the old app posted in *messages*."""
    title = cfg.get("report_title") or DEFAULT_TITLE
    posts = [m for m in messages if (m.get("user") == cfg.get("bot_user"))
             and title in (m.get("text") or "")]
    return max(posts, key=lambda m: float(m["ts"]), default=None)


# ── follow-through ─────────────────────────────────────────────────────────


def due_nags(board: dict[str, Any], list_id: str, *, now: datetime) -> list[dict[str, Any]]:
    """Every nag due now: ``{card, to, reason}`` (``to`` a member id).
    blocked > nag_days and open past due go to the assignee; an unassigned
    item past nag_days goes to the list's owner. A card nagged within the
    last nag_days is skipped; a ``bot`` assignee is the bot's own thread's
    job (D-TM13), never a Slack nag."""
    lst = _find_list(board, list_id)
    nag_days = (lst.get("defaults") or {}).get("nag_days") or DEFAULT_NAG_DAYS
    owner = project_config(lst).get("owner")
    horizon = timedelta(days=nag_days)
    out = []
    for c in _list_cards(board, list_id):
        if _settled(c) or c.get("assignee") == "bot":
            continue
        nagged = [d for t in c.get("touches") or []
                  if str(t.get("result", "")).startswith("nag:")
                  and (d := _parse_iso(t.get("at"))) is not None]
        last = max(nagged, default=None)
        if last is not None and now - last < horizon:
            continue
        since = _parse_iso(c.get("pm_status_since")) or now
        due = c.get("due")
        reason = None
        if c.get("pm_status") == "blocked" and now - since > horizon:
            reason = f"blocked {(now - since).days} days"
        elif due and due < now.strftime("%Y-%m-%d"):
            reason = f"past due ({due})"
        elif not c.get("assignee") and (_age_days(c, now) or 0) > nag_days:
            reason = f"unassigned {_age_days(c, now)} days"
        to = c.get("assignee") or owner
        if reason and to:
            out.append({"card": c, "to": to, "reason": reason})
    return out


def compose_nag(lst: dict[str, Any], nag: dict[str, Any]) -> str:
    member = next((m for m in lst.get("members") or [] if m["id"] == nag["to"]), {})
    who = f"<@{member['slack_user']}>" if member.get("slack_user") else member.get("display", nag["to"])
    c = nag["card"]
    return f"{who} {c.get('human_id')} — {c['title']}: {nag['reason']}."


def send_nags(
    shared_dir: Path, bot_id: str, list_id: str, network: dict[str, Any], *, now: datetime,
) -> list[dict[str, Any]]:
    """Fire :func:`due_nags` as ``remind`` touches (zero model): post in the
    list's channel naming the recipient, then log the touch on the card."""
    board = ac.tracker_read(shared_dir, bot_id)
    lst = _find_list(board, list_id)
    channel = project_config(lst).get("channel")
    fired = []
    for nag in due_nags(board, list_id, now=now):
        ok, err = ac.send_to_channel(bot_id, network, "slack", channel or "", compose_nag(lst, nag))
        if not ok:
            raise RuntimeError(f"nag delivery failed: {err}")
        ac.tracker_touch(shared_dir, bot_id, nag["card"]["id"], action="remind",
                         result=f"nag:{nag['to']}:{nag['reason']}", actor=bot_id,
                         now=now, reschedule=False)
        fired.append({"card": nag["card"].get("human_id"), "to": nag["to"]})
    return fired


# ── handoff (D-TM13) ───────────────────────────────────────────────────────


def handoff_to_member(
    shared_dir: Path, bot_id: str, card_id: str, list_id: str, member: str, *,
    actor: str,
) -> dict[str, Any]:
    """An assistant card goes to a person: a project item assigned to
    *member*, linked both ways to the card it came from."""
    board = ac.tracker_read(shared_dir, bot_id)
    src = next(c for c in board["cards"] if c["id"] == card_id)
    new = ac.tracker_create_card(
        shared_dir, bot_id, title=src["title"], cluster=src.get("cluster") or "admin",
        note=src.get("note") or "", source="handoff", source_id=f"card:{card_id}",
        actor=actor, list_id=list_id, assignee=member, outcome=src.get("outcome"))
    ac.tracker_link(shared_dir, bot_id, card_id, new["id"], actor=actor)
    return new


def handoff_to_bot(
    shared_dir: Path, bot_id: str, card_id: str, *, actor: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A project item assigned to the bot: a thread on the bot's personal
    board whose outcome is the item, linked both ways. Idempotent: a card
    that already carries its bot thread gets that thread back, never a
    second one."""
    board = ac.tracker_read(shared_dir, bot_id)
    card = next((c for c in board["cards"] if c["id"] == card_id), {})
    thread = next((c for c in board["cards"] if card and c["id"] == card.get("linked_card")
                   and c.get("owner") == "bot" and c.get("source_id") == f"card:{card_id}"),
                  None)
    if thread is not None:
        if card.get("assignee") != "bot":
            ac.tracker_update(shared_dir, bot_id, card_id, assignee="bot", actor=actor, now=now)
        return thread
    card = ac.tracker_update(shared_dir, bot_id, card_id, assignee="bot", actor=actor, now=now)
    thread = ac.tracker_create_card(
        shared_dir, bot_id, title=f"{card.get('human_id')}: {card['title']}",
        cluster="admin", lane="today", source="handoff", source_id=f"card:{card_id}",
        actor=actor, owner="bot", outcome=f"{card.get('human_id')} resolved", now=now)
    ac.tracker_link(shared_dir, bot_id, card_id, thread["id"], actor=actor)
    return thread


# ── the daemon tick ────────────────────────────────────────────────────────


def report_due(lst: dict[str, Any], *, now: datetime, tz: Any) -> bool:
    cfg = project_config(lst)
    if "report_weekday" not in cfg or "report_hour" not in cfg:
        return False
    local = now.astimezone(tz)
    slot = local.replace(hour=cfg["report_hour"], minute=0, second=0, microsecond=0)
    slot -= timedelta(days=(local.weekday() - cfg["report_weekday"]) % 7)
    if slot > local:
        slot -= timedelta(days=7)
    last = _parse_iso((lst.get("cursors") or {}).get("reported_through"))
    return last is not None and last < slot.astimezone(timezone.utc)


def tick(
    shared_dir: Path, bot_id: str, list_id: str, network: dict[str, Any], *,
    now: datetime | None = None, tz: Any = timezone.utc,
) -> dict[str, Any]:
    """One run for one project list: read the channel's last 14 days, ingest,
    answer commands, fire nags, and post the weekly report when its slot has
    passed. Zero model calls; every write is a Tracker verb."""
    now = now or datetime.now(timezone.utc)
    lst = _find_list(ac.tracker_read(shared_dir, bot_id), list_id)
    channel = project_config(lst).get("channel")
    if not channel:
        raise ValueError(f"list {list_id!r} has no defaults.project.channel")
    history = ac.read_history(bot_id, network, channel, (now - timedelta(days=14)).timestamp())
    replies = ingest(shared_dir, bot_id, list_id, history, now=now)
    for r in replies:
        ac.send_to_channel(bot_id, network, "slack", channel, r["reply"])
    nags = send_nags(shared_dir, bot_id, list_id, network, now=now)
    posted = False
    board = ac.tracker_read(shared_dir, bot_id)
    if not (lst.get("cursors") or {}).get("reported_through"):
        # First run: start the clock, never post a surprise mid-week report.
        ac.tracker_set_cursor(shared_dir, bot_id, list_id, "reported_through", _iso(now))
    elif report_due(_find_list(board, list_id), now=now, tz=tz):
        ok, err = ac.send_to_channel(bot_id, network, "slack", channel,
                                     render_weekly_report(board, list_id, now=now, tz=tz))
        if not ok:
            raise RuntimeError(f"weekly report delivery failed: {err}")
        ac.tracker_set_cursor(shared_dir, bot_id, list_id, "reported_through", _iso(now))
        posted = True
    return {"replies": len(replies), "nags": nags, "report_posted": posted}
