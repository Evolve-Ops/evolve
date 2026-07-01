"""Per-user activity aggregation for the Users page "Last seen" column.

Phase D.2 of docs/spec-user-roster-and-roles-2026-06-07.md (operator
feedback after C.5 deploy: "in the permissions table would be nice to
see something like last active").

Walks ``{shared_dir}/<bot_id>/turns/turns-<YYYY-MM-DD>.jsonl`` for the
last N days, aggregates per ``user_id``, returns a dict keyed on the
platform-stable_id tuple. Phase D.1 ships the prerequisite — populating
``user_id`` in turn records from senderRegistry — without which most
records have ``user_id: null`` and only Telegram DMs aggregate.

The aggregator is read-only and bounded (max 30 days back); a fresh
read costs O(turns) which on the deployed pod is ~thousands of records
per bot. Cheap enough to run on every Users-page GET; cached for the
turn duration only — operator-driven, not hot path.

The keying convention matches roster_overlay's identity keys
(``"<platform>:<stable_id>"``) so the routes_bot_users join is a
single dict lookup per approved entry. Channel inference is per-bot
from network.json once we know what kind of bot this is — the turn
record's ``channel`` field is the source.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


# How far back to look. Older turns get pruned by the existing log-cap
# job anyway; 30 days is generous enough for "last active" to feel
# accurate without scanning ancient history.
DEFAULT_WINDOW_DAYS = 30

# Rolling window for the "turns_7d" stat. Separate from
# DEFAULT_WINDOW_DAYS so the operator can see "last seen 3 weeks ago"
# (within the 30d window) while the turns_7d count clearly excludes
# old activity.
TURNS_WINDOW_DAYS = 7


def _turns_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / bot_id / "turns"


def _iso_to_dt(ts: str) -> "_dt.datetime | None":
    """Parse the turn record's ts. The writer uses ``new Date().toISOString()``
    which always emits the trailing 'Z'; tolerant fromisoformat handles it
    on Python 3.11+, but 3.10 needs the swap."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.timezone.utc)


def aggregate(
    shared_dir: Path,
    bot_id: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: "_dt.datetime | None" = None,
) -> dict[str, dict[str, Any]]:
    """Walk this bot's turn rollups; return per-user activity stats.

    Returns ``{"<channel>:<user_id>": {last_ts, turns_7d, channels}}``
    where keys mirror the overlay's identity-key convention so callers
    can join by lookup. ``channels`` is a set-as-list of the distinct
    OC channel labels seen for this user — useful for diagnostics.

    The OC channel-label vocabulary in turn records ("telegram",
    "slack", "discord", "heartbeat", etc.) is what we use directly as
    the platform. Heartbeat / cron / unknown sources don't carry a
    user_id (they're auto-sourced) so they're naturally excluded.

    Returns an empty dict when the turns directory doesn't exist
    (fresh bot or first deploy before any turns landed).
    """
    out: dict[str, dict[str, Any]] = {}
    now_dt = now or _now_utc()
    horizon = now_dt - _dt.timedelta(days=window_days)
    turns_horizon = now_dt - _dt.timedelta(days=TURNS_WINDOW_DAYS)
    # Phase D.3 — daily-bucket counters covering the full window.
    # ``window_days`` slots, slot[0] == today, slot[N-1] == window_days-1
    # days ago. UI renders this as a sparkline string in the Last-seen
    # cell's tooltip.
    today_dt = _dt.datetime(
        now_dt.year, now_dt.month, now_dt.day, tzinfo=_dt.timezone.utc)

    td = _turns_dir(shared_dir, bot_id)
    if not td.is_dir():
        return out

    # Iterate filenames matching turns-YYYY-MM-DD.jsonl. Reverse-sorted
    # so the newest is read first — irrelevant for correctness (full
    # walk anyway) but readable.
    files = sorted(td.glob("turns-*.jsonl"), reverse=True)

    for f in files:
        # File-level horizon prune by name — saves a stat + open on
        # rollups older than the window.
        date_str = f.stem.removeprefix("turns-")
        try:
            file_date = _dt.datetime.fromisoformat(date_str).replace(
                tzinfo=_dt.timezone.utc)
        except ValueError:
            continue  # malformed filename — skip
        if file_date < horizon - _dt.timedelta(days=1):
            # +1 day grace so timezone boundaries don't skip the
            # edge-of-window file.
            continue

        try:
            with open(f, "r") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    raw_user_id = rec.get("user_id")
                    raw_channel = rec.get("channel") or ""
                    # G.6 — borrow identity_discovery's shape inference
                    # so we get the same per-identity attribution as
                    # the "Seen recently" code path. Without this,
                    # Slack DM turn records (channel="D0...",
                    # user_id=null) never enter the aggregate because
                    # the legacy key=<channel>:<user_id> path skipped
                    # null user_ids. Now we infer (platform, external_id)
                    # from the channel-field shape for plugin-format
                    # turns, matching what discover_candidates does.
                    from .evo import identity_discovery as _idsc
                    identity = _idsc._extract_identity(
                        raw_channel, raw_user_id or "")
                    if identity is None:
                        continue  # auto-source / unknown / non-human
                    platform, external_id = identity
                    ts_dt = _iso_to_dt(rec.get("ts"))
                    if ts_dt is None or ts_dt < horizon:
                        continue

                    key = f"{platform}:{external_id}"
                    # ``channels`` for diagnostics — use the original
                    # platform name now, not the raw runtime context.
                    channel = platform
                    entry = out.setdefault(key, {
                        "last_ts": None,
                        "last_ts_dt": None,  # private, dropped before return
                        "turns_7d": 0,
                        "turns_30d": 0,
                        "cost_30d": 0.0,
                        "sessions_30d": set(),
                        "channels": set(),
                        # window_days slots, today=0, days-ago grows right
                        "daily_buckets": [0] * window_days,
                    })
                    if entry["last_ts_dt"] is None or ts_dt > entry["last_ts_dt"]:
                        entry["last_ts_dt"] = ts_dt
                        entry["last_ts"] = ts_dt.isoformat().replace(
                            "+00:00", "Z")
                    if ts_dt >= turns_horizon:
                        entry["turns_7d"] += 1
                    entry["turns_30d"] += 1
                    # cost is estimated — see TurnObserver.writeTurnToShared
                    cost = rec.get("cost")
                    if isinstance(cost, (int, float)) and cost > 0:
                        entry["cost_30d"] += float(cost)
                    sid = rec.get("session_id")
                    if sid:
                        entry["sessions_30d"].add(sid)
                    entry["channels"].add(channel)
                    # Compute days-ago slot. ``ts_dt`` is UTC; we bucket
                    # against today_dt (also UTC midnight) so a turn at
                    # 23:59Z and 00:01Z of the next day land in
                    # different slots — operator wants daily
                    # granularity, not 24h-rolling.
                    days_ago = (today_dt - _dt.datetime(
                        ts_dt.year, ts_dt.month, ts_dt.day,
                        tzinfo=_dt.timezone.utc)).days
                    if 0 <= days_ago < window_days:
                        entry["daily_buckets"][days_ago] += 1
        except (OSError, PermissionError) as exc:
            log.warning("user_activity: skip %s (%s)", f, exc)
            continue

    # Finalize — drop the private datetime, convert sets to int/list,
    # round cost to a sensible precision for transport.
    for entry in out.values():
        entry.pop("last_ts_dt", None)
        entry["channels"] = sorted(entry["channels"])
        entry["sessions_30d"] = len(entry["sessions_30d"])
        entry["cost_30d"] = round(entry["cost_30d"], 4)
    return out


def rewrite_slack_dm_keys(
    activity: dict[str, dict[str, Any]],
    network: dict,
    *,
    bot_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve Slack D-channel activity keys (``slack:D0...``) to their
    participating user (``slack:U0...``) via ``conversations.info``.

    G.6 — after ``aggregate()`` runs with the identity-shape inference,
    Slack DM turns surface as ``slack:D0AMYBZ4RM1`` because that's the
    channel field in the turn record. The Users page joins by the
    approved list's U-id, so without this rewrite the join still
    misses. Mirrors ``identity_discovery._rewrite_slack_dm_candidates``
    using the same name_resolver helpers (memoized per D-id).

    Mutating reformat: returns a new dict with the keys remapped where
    possible. Entries where the D→U lookup fails (no token, non-IM
    channel, network error) keep their D-id key — better to surface
    activity under a useless key than lose the data.

    When two paths attribute to the same user (one via D-channel
    rewrite, one via a direct U-id record — same situation as F.1's
    dedup for Seen Recently), the entries merge: sum turns_7d /
    turns_30d / cost / sessions, max last_ts, daily_buckets
    element-wise.
    """
    # Lazy imports — keep user_activity self-contained when callers
    # don't pass a network.
    from .evo import name_resolver

    out: dict[str, dict[str, Any]] = {}
    d_cache: dict[str, str | None] = {}
    for key, entry in activity.items():
        if not key.startswith("slack:D"):
            # Telegram, U-id Slack, etc. — pass through.
            _merge_into(out, key, entry)
            continue
        d_id = key.split(":", 1)[1]
        if d_id not in d_cache:
            token = name_resolver._channel_token(
                network, "slack", prefer_bot_id=bot_id)
            d_cache[d_id] = (
                name_resolver.slack_im_channel_to_user(token, d_id)
                if token else None
            )
        u_id = d_cache[d_id]
        if not u_id:
            _merge_into(out, key, entry)  # keep D-key on failure
            continue
        _merge_into(out, f"slack:{u_id}", entry)
    return out


def _merge_into(
    out: dict[str, dict[str, Any]], key: str, entry: dict[str, Any],
) -> None:
    """Merge an activity entry into ``out[key]``, summing the counters
    and bounding the timestamps when a key collides (D-channel and
    U-direct path both attribute to the same user)."""
    if key not in out:
        # Copy to avoid mutating the caller's dict.
        copy = dict(entry)
        copy["daily_buckets"] = list(entry.get("daily_buckets") or [])
        out[key] = copy
        return
    existing = out[key]
    existing["turns_7d"] = (
        int(existing.get("turns_7d") or 0)
        + int(entry.get("turns_7d") or 0)
    )
    existing["turns_30d"] = (
        int(existing.get("turns_30d") or 0)
        + int(entry.get("turns_30d") or 0)
    )
    existing["cost_30d"] = round(
        float(existing.get("cost_30d") or 0.0)
        + float(entry.get("cost_30d") or 0.0),
        4,
    )
    existing["sessions_30d"] = (
        int(existing.get("sessions_30d") or 0)
        + int(entry.get("sessions_30d") or 0)
    )
    l1 = existing.get("last_ts") or ""
    l2 = entry.get("last_ts") or ""
    existing["last_ts"] = max(l1, l2) or None
    # Element-wise sum of daily_buckets when they line up by length.
    eb = existing.get("daily_buckets") or []
    nb = entry.get("daily_buckets") or []
    if len(eb) == len(nb):
        existing["daily_buckets"] = [a + b for a, b in zip(eb, nb)]


def lookup(
    activity: dict[str, dict[str, Any]],
    channel: str,
    user_id: str,
) -> "dict[str, Any] | None":
    """Convenience: look up a single identity's activity record.

    Returns ``None`` when no activity for this user in the window.
    """
    return activity.get(f"{channel}:{user_id}")
