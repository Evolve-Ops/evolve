"""spend_attribution — which spend is a person working, which is the machine.

Brief: ``evolve-overhead-ledger-and-budget`` item 6 (operator observation,
2026-09-07). The daily cost cap treated every dollar alike, so a person
working a bot hard for an afternoon — the product doing its job — tripped the
same breaker as a heartbeat loop. The two are not the same kind of spend:

  * **attributed** — a turn a person is waiting on, with a resolved human
    speaker (``speaker`` = ``<channel>:<id>``, role ``primary_user`` /
    ``user``). Counted against a WIDE rolling window
    (default 7 days).
  * **unattributed** — no speaker: cron, heartbeat, plugin-internal calls,
    loops. Keeps the tight per-day cap.

Structural only. Nothing here changes which model answers a turn; it changes
which window a dollar is measured against, and both numbers are shown.

Fail direction: anything that cannot be proven attributed IS unattributed —
a turn with no resolvable speaker, an unknown source, an unreadable role.
Misclassifying a person's turn as a machine's costs a tighter cap on that
dollar (today's behaviour); misclassifying a loop's turn as a person's would
silently widen the cap on exactly the spend the cap exists for.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

#: Roles that make a human turn "the product working". Anything else —
#: ``participant``, ``blocked``, a missing or unrecognised role — is
#: unattributed (see :func:`is_attributed`).
ATTRIBUTED_ROLES: frozenset[str] = frozenset({"primary_user", "user"})

#: Turn ``source`` / ``trigger_kind`` values that mean "a person sent this".
_HUMAN_SOURCES: frozenset[str] = frozenset({"human", "user", "user_turn"})

#: user_id values a writer uses for "nobody": never a speaker.
_NO_SPEAKER: frozenset[str] = frozenset({"", "unknown", "none", "null", "system"})

#: Default window (days) for attributed spend. network.json
#: ``thresholds.attributedWindowDays`` moves it.
DEFAULT_ATTRIBUTED_WINDOW_DAYS = 7


def is_human_turn(turn: Mapping[str, Any]) -> bool:
    """True when the turn row says a person sent it."""
    for key in ("source", "trigger_kind"):
        val = turn.get(key)
        if isinstance(val, str) and val.strip().lower() in _HUMAN_SOURCES:
            return True
    return False


def speaker_of(turn: Mapping[str, Any]) -> str | None:
    """The resolved speaker as ``<channel>:<id>``, or ``None``.

    Prefers an explicit ``speaker`` field (the plugin's own resolution); else
    builds it from ``channel`` + ``user_id``. A missing channel falls back to
    ``unknown:<id>`` — the id alone still names one person.
    """
    explicit = turn.get("speaker")
    if isinstance(explicit, str) and ":" in explicit:
        ident = explicit.split(":", 1)[1].strip().lower()
        if ident not in _NO_SPEAKER:
            return explicit.strip()
    uid = turn.get("user_id")
    if uid is None:
        return None
    uid_s = str(uid).strip()
    if uid_s.lower() in _NO_SPEAKER:
        return None
    channel = str(turn.get("channel") or "unknown").strip().lower() or "unknown"
    return f"{channel}:{uid_s}"


def is_attributed(
    turn: Mapping[str, Any],
    role_of: Callable[[str], str | None] | None = None,
) -> bool:
    """True when ``turn`` is human-attributed spend.

    Needs a human turn, a resolved speaker, AND a known role in
    :data:`ATTRIBUTED_ROLES` (from the turn's ``speaker_role`` or ``role_of``).
    A missing or unknown role is unattributed: the exemption widens the cap, so
    "could not look up the role" must fail toward the tight cap — otherwise any
    resolved speaker (a group participant, a blocked sender) buys the window.
    """
    if not is_human_turn(turn):
        return False
    speaker = speaker_of(turn)
    if speaker is None:
        return False
    role = turn.get("speaker_role")
    if not isinstance(role, str) or not role:
        role = None
        if role_of is not None:
            try:
                role = role_of(speaker)
            except Exception:  # noqa: BLE001 — a failing resolver is "unknown"
                role = None
    if role is None:
        return False
    return role.strip().lower() in ATTRIBUTED_ROLES


def window_start(now: datetime, days: int, origin: datetime | None = None) -> datetime:
    """The start of the rolling window ending at ``now``.

    ``origin`` is the operator's "count from now" instant (a reactivation):
    the window never reaches back before it, so a reactivation cannot re-trip
    on the spend that tripped it.
    """
    start = now - timedelta(days=max(int(days), 1))
    if origin is not None and origin > start:
        return origin
    return start


def parse_ts(raw: Any) -> datetime | None:
    """A turn ``ts`` as an aware UTC datetime, or ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def split_spend(
    turns: list[dict],
    *,
    role_of: Callable[[str], str | None] | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Sum ``turns`` into attributed vs unattributed dollars.

    ``since`` drops turns before that instant (the window / acceptance
    origin). Returns plain numbers plus the unpriced counts, so a caller can
    keep "could not price" beside the figure rather than reading it as $0.
    """
    from turn_cost import sum_turn_costs  # type: ignore[import]

    attributed: list[dict] = []
    unattributed: list[dict] = []
    for t in turns:
        if since is not None:
            ts = parse_ts(t.get("ts"))
            if ts is None or ts < since:
                continue
        (attributed if is_attributed(t, role_of) else unattributed).append(t)
    a = sum_turn_costs(attributed)
    u = sum_turn_costs(unattributed)
    return {
        "attributed_usd": round(a.usd, 6),
        "unattributed_usd": round(u.usd, 6),
        "attributed_turns": a.priced_turns + a.unpriced_turns,
        "unattributed_turns": u.priced_turns + u.unpriced_turns,
        "unpriced_turns": a.unpriced_turns + u.unpriced_turns,
    }


def local_day_attributed_usd(
    turns: list[dict], day: date, tz: Any,
    *, role_of: Callable[[str], str | None] | None = None,
) -> float:
    """Attributed spend on one pod-local ``day`` (instant-bucketed, never a
    ``ts[:10]`` prefix — see ``live_spend`` for why)."""
    rows = []
    for t in turns:
        ts = parse_ts(t.get("ts"))
        if ts is not None and ts.astimezone(tz).date() == day:
            rows.append(t)
    return split_spend(rows, role_of=role_of)["attributed_usd"]


def attribution_for(
    bot_id: str,
    *,
    today: date,
    now: datetime,
    window_days: int = DEFAULT_ATTRIBUTED_WINDOW_DAYS,
    origin: datetime | None = None,
    exempt_subkinds: set[str] | None = None,
    role_of: Callable[[str], str | None] | None = None,
    turns: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Today's and the window's attributed spend for one bot, or ``None``.

    ``None`` means the turn files could not be read. The caller then applies
    NO exemption (the tight cap measures everything): "could not look" must
    never widen a cap.

    ``today`` is a pod-local date; the window is the rolling ``window_days``
    ending ``now``, never reaching back before ``origin`` (the operator's
    reactivation instant — "count from now").
    """
    import live_spend  # type: ignore[import]

    tz = live_spend.pod_tz_or_local()
    since = window_start(now, window_days, origin)

    def _keep(rows: list[dict]) -> list[dict]:
        if not exempt_subkinds:
            return rows
        return [t for t in rows if t.get("forge_subkind") not in exempt_subkinds]

    if turns is None:
        # Two UTC files cover pod-local "today". Only a bot with attributed
        # spend today has any use for the wide window, so the full week is
        # read for those alone — every tick of an idle bot stays two files.
        loaded = live_spend.load_live_turns(bot_id, days=2, end=now)
        if loaded is live_spend.LIVE_LOAD_FAILED or not isinstance(loaded, list):
            return None
        today_usd = local_day_attributed_usd(_keep(loaded), today, tz, role_of=role_of)
        if today_usd <= 0:
            return {
                "attributed_usd": 0.0, "window_attributed_usd": None,
                "window_days": max(int(window_days), 1),
                "window_since": since.isoformat(),
            }
        loaded = live_spend.load_live_turns(
            bot_id, days=max(int(window_days), 1) + 1, end=now,
        )
        if loaded is live_spend.LIVE_LOAD_FAILED or not isinstance(loaded, list):
            return None
        turns = loaded
    turns = _keep(turns)
    window = split_spend(turns, role_of=role_of, since=since)
    return {
        "attributed_usd": local_day_attributed_usd(turns, today, tz, role_of=role_of),
        "window_attributed_usd": window["attributed_usd"],
        "window_days": max(int(window_days), 1),
        "window_since": since.isoformat(),
    }
