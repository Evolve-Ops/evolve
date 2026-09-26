"""tests/test_identity_discovery.py — auto-discovery of a bot's owner.

Covers the helper in ``evolve_admin/evo/identity_discovery.py`` that
scans turn-collector JSONL files to suggest likely primary-user
candidates from real interaction history. We seed a fake bot home
under tmp_path (the helper accepts ``bot_user`` and rejects nothing,
so we monkeypatch ``_bot_home`` to point at our fake dir).

The helper is intentionally defensive — non-existent dirs, corrupt
JSON, missing fields, cron/subagent rows, unsupported channels —
all return empty lists / drop rows rather than raise. Most of the
tests below stress those defensive paths.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.evo import identity_discovery  # noqa: E402


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_turns(
    turns_dir: Path, day: date, rows: list[dict],
) -> None:
    """Write a turns-<YYYY-MM-DD>.jsonl file with the given rows."""
    turns_dir.mkdir(parents=True, exist_ok=True)
    path = turns_dir / f"turns-{day.isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def fake_bot_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ``_turn_dir_candidates`` to a tmp turn dir."""
    turns = tmp_path / "turns"

    def _candidates(_bot_id, _bot_user):
        return [turns]

    monkeypatch.setattr(
        identity_discovery, "_turn_dir_candidates", _candidates,
    )
    return turns


# ── discover_candidates: happy path ───────────────────────────────────────────


def test_discover_single_user_dominant(fake_bot_home: Path):
    """A bot where one Telegram ID wrote every turn returns just that one."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc) - timedelta(hours=h)),
         "channel": "telegram", "user_id": "111",
         "source": "human", "session_id": f"s{h}"}
        for h in range(5)
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert len(out) == 1
    assert out[0]["channel"] == "telegram"
    assert out[0]["external_id"] == "111"
    assert out[0]["turn_count"] == 5


def test_discover_orders_by_count_desc(fake_bot_home: Path):
    """Two users, the more active one ranks first."""
    today = date.today()
    rows = (
        [{"ts": _iso(datetime.now(timezone.utc)),
          "channel": "telegram", "user_id": "222",
          "source": "human", "session_id": f"a{i}"} for i in range(2)]
        + [{"ts": _iso(datetime.now(timezone.utc)),
            "channel": "telegram", "user_id": "111",
            "source": "human", "session_id": f"b{i}"} for i in range(10)]
    )
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert [c["external_id"] for c in out] == ["111", "222"]
    assert out[0]["turn_count"] == 10
    assert out[1]["turn_count"] == 2


def test_discover_tiebreak_by_last_seen(fake_bot_home: Path):
    """Equal counts → most-recently-active wins."""
    today = date.today()
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": _iso(now - timedelta(days=2)),
         "channel": "telegram", "user_id": "stale",
         "source": "human", "session_id": "s1"},
        {"ts": _iso(now),
         "channel": "telegram", "user_id": "fresh",
         "source": "human", "session_id": "s2"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=7)
    assert out[0]["external_id"] == "fresh"
    assert out[1]["external_id"] == "stale"


def test_discover_top_k_truncates(fake_bot_home: Path):
    """``top_k`` caps the list."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "telegram", "user_id": f"u{i}",
         "source": "human", "session_id": f"s{i}"}
        for i in range(7)
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates(
        "team_bot_a", lookback_days=3, top_k=3,
    )
    assert len(out) == 3


def test_discover_first_and_last_seen_tracked(fake_bot_home: Path):
    """The earliest + latest ts for each user is recorded."""
    today = date.today()
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": _iso(now - timedelta(days=3)),
         "channel": "telegram", "user_id": "111",
         "source": "human", "session_id": "s1"},
        {"ts": _iso(now - timedelta(days=1)),
         "channel": "telegram", "user_id": "111",
         "source": "human", "session_id": "s2"},
        {"ts": _iso(now),
         "channel": "telegram", "user_id": "111",
         "source": "human", "session_id": "s3"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=7)
    assert len(out) == 1
    c = out[0]
    assert c["first_seen"] < c["last_seen"]
    assert c["turn_count"] == 3


# ── discover_candidates: filtering ───────────────────────────────────────────


def test_discover_drops_cron_source(fake_bot_home: Path):
    """``source != 'human'`` rows are excluded."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "telegram", "user_id": "111",
         "source": "cron", "session_id": "s1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "telegram", "user_id": "111",
         "source": "subagent", "session_id": "s2"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert out == []


def test_discover_drops_unknown_channel(fake_bot_home: Path):
    """Non-human channel labels are excluded (cron, unknown, heartbeat, …)."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "unknown", "user_id": "111",
         "source": "human", "session_id": "s1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "cron", "user_id": "111",
         "source": "human", "session_id": "s2"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "subagent", "user_id": "111",
         "source": "human", "session_id": "s3"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "heartbeat", "user_id": None,
         "source": "heartbeat", "session_id": "s4"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert out == []


def test_discover_drops_empty_user_id(fake_bot_home: Path):
    """Rows with no user_id can't be candidates."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "telegram", "user_id": "",
         "source": "human", "session_id": "s1"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert out == []


def test_discover_dedup_across_same_session(fake_bot_home: Path):
    """If a turn is mirrored across multiple candidate dirs we dedup."""
    turns_a = fake_bot_home
    turns_b = fake_bot_home.parent / "mirror"

    # Override candidates to return both dirs
    def _two_dirs(_b, _u):
        return [turns_a, turns_b]

    today = date.today()
    row = {"ts": "2026-01-01T10:00:00Z", "channel": "telegram",
           "user_id": "111", "source": "human", "session_id": "same"}
    _seed_turns(turns_a, today, [row])
    _seed_turns(turns_b, today, [row])

    # Re-monkeypatch via direct attr swap inside the test
    import evolve_admin.evo.identity_discovery as ID
    orig = ID._turn_dir_candidates
    ID._turn_dir_candidates = _two_dirs
    try:
        out = ID.discover_candidates("team_bot_a", lookback_days=3)
    finally:
        ID._turn_dir_candidates = orig
    assert len(out) == 1
    assert out[0]["turn_count"] == 1


def test_discover_includes_slack_discord_whatsapp(fake_bot_home: Path):
    """All four human-channel types are accepted."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": ch, "user_id": f"u-{ch}",
         "source": "human", "session_id": f"s-{ch}"}
        for ch in ("telegram", "slack", "discord", "whatsapp")
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert {c["channel"] for c in out} == {
        "telegram", "slack", "discord", "whatsapp",
    }


# ── plugin-TurnObserver format ──────────────────────────────────────────────
#
# The plugin (packages/plugin/.../TurnObserver.ts) emits a different shape
# than the OC turn-collector: source="user" instead of "human", and the
# channel field holds the gateway-supplied id (Telegram chat_id, Slack
# D…/U… id, Discord snowflake) with user_id hard-coded to null. Security_bot
# and Admin_bot on the live pod only have plugin-format records, so any
# regression here re-breaks the operator's "Discover from history" button.


def test_discover_plugin_format_telegram_human_inferred(fake_bot_home: Path):
    """source='user' + numeric channel + user_id=null → telegram identity."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc) - timedelta(hours=h)),
         "channel": "1260193629", "user_id": None,
         "source": "user", "session_id": f"plug-{h}"}
        for h in range(3)
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("security_bot", lookback_days=3)
    assert len(out) == 1
    assert out[0]["channel"] == "telegram"
    assert out[0]["external_id"] == "1260193629"
    assert out[0]["turn_count"] == 3


def test_discover_plugin_format_telegram_group_chat(fake_bot_home: Path):
    """Telegram group chat_ids are negative — the leading '-' is preserved."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "-1001234567", "user_id": None,
         "source": "user", "session_id": "g1"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("security_bot", lookback_days=3)
    assert len(out) == 1
    assert out[0]["channel"] == "telegram"
    assert out[0]["external_id"] == "-1001234567"


def test_discover_plugin_format_discord_snowflake(fake_bot_home: Path):
    """A 19-digit numeric channel is classified as Discord, not Telegram."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "1479517965366853643", "user_id": None,
         "source": "user", "session_id": "d1"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_b", lookback_days=3)
    assert len(out) == 1
    assert out[0]["channel"] == "discord"
    assert out[0]["external_id"] == "1479517965366853643"


def test_discover_plugin_format_slack_dm_and_user(fake_bot_home: Path):
    """Slack D… (DM) and U… (user) ids are recognized as slack."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "D0AKM4LTT99", "user_id": None,
         "source": "user", "session_id": "k1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "U9ZL3JYR3", "user_id": None,
         "source": "user", "session_id": "k2"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    channels = {(c["channel"], c["external_id"]) for c in out}
    assert ("slack", "D0AKM4LTT99") in channels
    assert ("slack", "U9ZL3JYR3") in channels


def test_discover_plugin_format_slack_thread_collapses_to_channel(
    fake_bot_home: Path,
):
    """``c<id>:thread:<ts>`` collapses to the base channel id."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "c08c5n1c3gw:thread:1779305097.577679",
         "user_id": None, "source": "user", "session_id": "t1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "c08c5n1c3gw:thread:1779400949.209459",
         "user_id": None, "source": "user", "session_id": "t2"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert len(out) == 1
    assert out[0]["channel"] == "slack"
    assert out[0]["external_id"] == "c08c5n1c3gw"
    assert out[0]["turn_count"] == 2


def test_discover_plugin_format_drops_heartbeat_and_cron(fake_bot_home: Path):
    """Heartbeat and cron rows must not contribute candidates."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "heartbeat", "user_id": None,
         "source": "heartbeat", "session_id": "h1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "1260193629", "user_id": None,
         "source": "cron", "session_id": "c1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "unknown", "user_id": None,
         "source": "unknown", "session_id": "u1"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("security_bot", lookback_days=3)
    assert out == []


def test_discover_plugin_format_drops_literal_platform_without_id(
    fake_bot_home: Path,
):
    """``channel="telegram"`` with no user_id is useless — drop, don't
    emit a candidate with empty external_id."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "telegram", "user_id": None,
         "source": "user", "session_id": "n1"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("security_bot", lookback_days=3)
    assert out == []


def test_discover_drops_unknown_user_id_sentinel(fake_bot_home: Path):
    """OC turn-collector sometimes writes user_id='unknown' as a
    sentinel when the chat_id couldn't be inferred. Don't surface that
    as a candidate — it ranks high (lots of turns) and isn't actionable
    by the operator. Regression test for the admin_bot chip (2026-05-29)."""
    today = date.today()
    rows = (
        [{"ts": _iso(datetime.now(timezone.utc)),
          "channel": "telegram", "user_id": "unknown",
          "source": "human", "session_id": f"sentinel-{i}"} for i in range(50)]
        + [{"ts": _iso(datetime.now(timezone.utc)),
            "channel": "telegram", "user_id": "1260193629",
            "source": "human", "session_id": f"real-{i}"} for i in range(3)]
    )
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("admin_bot", lookback_days=3)
    assert len(out) == 1
    assert out[0]["external_id"] == "1260193629"
    assert out[0]["turn_count"] == 3


def test_discover_slack_group_sender_with_user_id(fake_bot_home: Path):
    """Real Slack group turns carry a conversation-shaped ``channel``
    PLUS the sender's ``user_id``. We must tally the PERSON, not the
    conversation — the regression that hid group senders from the Users
    page. Two senders in the same thread surface as two candidates."""
    today = date.today()
    rows = [
        {"ts": _iso(datetime.now(timezone.utc) - timedelta(hours=h)),
         "channel": "c0fakeconv1:thread:1781000000.0001",
         "user_id": "U0FAKEGRP01",
         "source": "human", "session_id": f"grp-a{h}"}
        for h in range(4)
    ] + [
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "c0fakeconv1:thread:1781000000.0001",
         "user_id": "U0FAKEGRP02",
         "source": "user", "session_id": "grp-b1"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    by_id = {c["external_id"]: c for c in out}
    assert set(by_id) == {"U0FAKEGRP01", "U0FAKEGRP02"}
    assert all(c["channel"] == "slack" for c in out)
    assert by_id["U0FAKEGRP01"]["turn_count"] == 4
    assert by_id["U0FAKEGRP02"]["turn_count"] == 1


def test_discover_mixed_plugin_and_oc_collector_formats(fake_bot_home: Path):
    """A bot that has both schemas in the same window tallies correctly."""
    today = date.today()
    rows = [
        # OC turn-collector style
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "telegram", "user_id": "1260193629",
         "source": "human", "session_id": "oc1"},
        # Plugin TurnObserver style — same user, different schema
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "1260193629", "user_id": None,
         "source": "user", "session_id": "plug1"},
        {"ts": _iso(datetime.now(timezone.utc)),
         "channel": "1260193629", "user_id": None,
         "source": "user", "session_id": "plug2"},
    ]
    _seed_turns(fake_bot_home, today, rows)

    out = identity_discovery.discover_candidates("security_bot", lookback_days=3)
    assert len(out) == 1
    assert out[0]["channel"] == "telegram"
    assert out[0]["external_id"] == "1260193629"
    assert out[0]["turn_count"] == 3


# ── discover_candidates: robustness ──────────────────────────────────────────


def test_discover_no_history_returns_empty(fake_bot_home: Path):
    """Bot with no turn dir → empty list, no error."""
    # Note: fake_bot_home points at a path that doesn't exist yet.
    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert out == []


def test_discover_corrupt_jsonl_skipped(fake_bot_home: Path):
    """Bad JSON lines don't crash — they get skipped."""
    today = date.today()
    fake_bot_home.mkdir(parents=True, exist_ok=True)
    path = fake_bot_home / f"turns-{today.isoformat()}.jsonl"
    path.write_text(
        "{not json}\n"
        '{"ts": "2026-01-01", "channel": "telegram", "user_id": "111", '
        '"source": "human", "session_id": "s1"}\n'
        "another broken line\n",
        encoding="utf-8",
    )
    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=3)
    assert len(out) == 1
    assert out[0]["external_id"] == "111"


def test_discover_lookback_window_excludes_old_files(fake_bot_home: Path):
    """Files older than ``lookback_days`` are not read."""
    today = date.today()
    old = today - timedelta(days=60)
    rows = [
        {"ts": "2025-01-01T00:00:00Z", "channel": "telegram",
         "user_id": "ancient", "source": "human", "session_id": "s1"},
    ]
    _seed_turns(fake_bot_home, old, rows)

    # lookback_days=30 → 60-day-old file not visited
    out = identity_discovery.discover_candidates("team_bot_a", lookback_days=30)
    assert out == []


# ── resolve_with_names ──────────────────────────────────────────────────────


def test_resolve_with_names_attaches_username_when_cached(monkeypatch):
    """When a candidate's identity is cached in network, ``resolve_with_names``
    attaches the cached username + display name."""
    # cached_at omitted on purpose — per name_resolver._entry_is_stale,
    # missing cached_at is treated as fresh. Avoids the time-bomb
    # where a fixed date eventually crosses the 7-day TTL (which is
    # how this test silently started failing 2026-06-08).
    network = {
        "pod": {
            "admins": {
                "resolved_names": {
                    "telegram:111": {
                        "username": "cjuser",
                        "name": "Pod-Admin Person",
                    },
                },
            },
        },
    }
    cands = [
        {"channel": "telegram", "external_id": "111",
         "turn_count": 5, "first_seen": "", "last_seen": ""},
    ]
    out = identity_discovery.resolve_with_names(network, cands)
    assert len(out) == 1
    assert out[0]["username"] == "cjuser"
    assert out[0]["display_name"] == "Pod-Admin Person"


def test_resolve_with_names_keeps_none_on_failure(monkeypatch):
    """When the resolver returns None (unsupported channel, no token,
    API failure), the candidate keeps ``None`` for username/display_name
    instead of breaking the whole list."""
    network = {}
    cands = [
        {"channel": "discord", "external_id": "999",
         "turn_count": 1, "first_seen": "", "last_seen": ""},
    ]
    out = identity_discovery.resolve_with_names(network, cands)
    assert out[0]["username"] is None
    assert out[0]["display_name"] is None
    assert out[0]["external_id"] == "999"  # base fields preserved


# ── _extract_identity (unit) ────────────────────────────────────────────────


@pytest.mark.parametrize("channel,user_id,expected", [
    # OC turn-collector — literal platform names with explicit user_id
    ("telegram", "111", ("telegram", "111")),
    ("slack",    "U123ABC", ("slack", "U123ABC")),
    ("discord",  "999", ("discord", "999")),
    ("whatsapp", "+1555", ("whatsapp", "+1555")),
    # Plugin TurnObserver — numeric channel, no user_id
    ("1260193629", "", ("telegram", "1260193629")),
    ("-1001234567", "", ("telegram", "-1001234567")),  # group chat
    ("1479517965366853643", "", ("discord", "1479517965366853643")),
    # Plugin TurnObserver — Slack id shapes
    ("D0AKM4LTT99", "", ("slack", "D0AKM4LTT99")),
    ("U9ZL3JYR3",   "", ("slack", "U9ZL3JYR3")),
    ("c08c5n1c3gw:thread:1779305097.577679", "",
     ("slack", "c08c5n1c3gw")),
    # Drops — explicit non-human labels
    ("unknown", "anything", None),
    ("heartbeat", "", None),
    ("cron-event", "", None),
    ("exec-event", "", None),
    ("webchat", "", None),
    # Drops — empty channel
    ("", "anything", None),
    # Drops — literal platform without external_id (plugin path for
    # some Telegram emissions that fall back to the name)
    ("telegram", "", None),
    ("slack",    "", None),
    # Drops — OC turn-collector sentinel user_ids ("unknown", "none",
    # "null") that it emits when it can't infer the real chat_id.
    # Without this, admin_bot shows a top candidate of ("telegram",
    # "unknown") with thousands of turns that the operator can't act on.
    ("telegram", "unknown", None),
    ("telegram", "UNKNOWN", None),
    ("telegram", "none", None),
    ("slack",    "unknown", None),
    # Drops — unrecognized free-form label, no shape match
    ("freeform-label", "", None),
    # Prefer-user-id fix (2026-06-17): a turn that carries BOTH a
    # conversation-shaped channel AND a real sender user_id must
    # attribute to the PERSON, not the conversation. This is the bug
    # that hid high-activity Slack group/DM senders from the Users page
    # while Usage's By-User showed them.
    # Slack group/thread conversation + real U-id sender → the user.
    ("c0fakeconv1:thread:1781000000.0001", "U0FAKEGRP01",
     ("slack", "U0FAKEGRP01")),
    # Bare Slack channel id + sender.
    ("C0B7ATL89UJ", "U0FAKEGRP02", ("slack", "U0FAKEGRP02")),
    # Slack DM channel + sender → the user wins over the D-channel.
    ("D0AKM4LTT99", "U0FAKEGRP01", ("slack", "U0FAKEGRP01")),
    # Telegram group chat (negative chat_id) + member sender → member.
    ("-1001234567", "777888999", ("telegram", "777888999")),
    # Discord guild/channel snowflake + member id → the member.
    ("1479517965366853643", "550000000000000001",
     ("discord", "550000000000000001")),
    # A sentinel user_id never becomes the external_id — fall back to
    # the channel-shape extraction instead of ("slack", "unknown").
    ("c0fakeconv1:thread:1781000000.0001", "unknown",
     ("slack", "c0fakeconv1")),
    # user_id present but channel shape unrecognized → can't name a
    # platform for the id, so drop rather than guess.
    ("freeform-label", "U0FAKEGRP01", None),
])
def test_extract_identity_cases(channel, user_id, expected):
    assert identity_discovery._extract_identity(channel, user_id) == expected


def test_extract_identity_prefers_user_id_over_conversation_channel():
    """Regression for the Users-page blindness to group-DM senders.

    A live Slack group turn looks like ``channel="c…:thread:…"`` (a
    CONVERSATION id) plus ``user_id="U…"`` (the real sender). The old
    code returned the conversation; the route's Slack U/W-only filter
    then dropped it, so the person never surfaced in seen_recently —
    even though usage_analytics, which reads user_id directly, listed
    them under By-User. Assert we now attribute to the person."""
    out = identity_discovery._extract_identity(
        "c0fakeconv1:thread:1781000000.0001", "U0FAKEGRP01")
    assert out == ("slack", "U0FAKEGRP01")


def test_resolve_with_names_preserves_base_fields(monkeypatch):
    """The returned dicts have all the original candidate fields."""
    network = {}
    cands = [
        {"channel": "telegram", "external_id": "111",
         "turn_count": 7, "first_seen": "2026-05-01T00:00:00Z",
         "last_seen": "2026-05-15T00:00:00Z"},
    ]
    # Stub resolver to None so we exercise the None-handling branch
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None,
    )
    out = identity_discovery.resolve_with_names(network, cands)
    assert out[0]["turn_count"] == 7
    assert out[0]["first_seen"] == "2026-05-01T00:00:00Z"
    assert out[0]["last_seen"] == "2026-05-15T00:00:00Z"


# ── F.1: Slack D-channel rewrite ────────────────────────────────────────


def test_slack_dm_candidate_rewritten_to_user_id(monkeypatch):
    """A Slack D-channel candidate is rewritten via conversations.info
    to the participating user. The candidate's external_id becomes the
    U-id; the original D-id is preserved as ``via_channel``. The name
    resolver then gets called with the U-id so users.info succeeds."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "D0AMYBZ4RM1",
         "turn_count": 19, "first_seen": "", "last_seen": ""},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token",
        lambda net, ch, **kw: "TOK_TEST")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: (
            "U_REWRITTEN" if channel_id == "D0AMYBZ4RM1" else None))
    # Resolver: succeed for the U-id so we can see the name attach.
    captured: list[str] = []
    def fake_resolve(net, *, channel, external_id, bot_id=None):
        captured.append(external_id)
        return {"username": "u_handle", "name": "Display Person",
                "cached_at": "2026-06-08T00:00:00Z"}
    monkeypatch.setattr(name_resolver, "resolve", fake_resolve)

    out = identity_discovery.resolve_with_names(network, cands)
    assert out[0]["external_id"] == "U_REWRITTEN"
    assert out[0]["via_channel"] == "D0AMYBZ4RM1"
    assert out[0]["username"] == "u_handle"
    assert out[0]["display_name"] == "Display Person"
    # Resolver got the U-id, not the D-id.
    assert captured == ["U_REWRITTEN"]


def test_slack_dm_rewrite_falls_back_when_im_lookup_fails(monkeypatch):
    """When conversations.info returns None (not an IM, missing scope,
    network failure), the candidate keeps its D-id. The name resolver
    won't find a name for it but the entry still renders so the operator
    sees the activity."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "D0AMYBZ4RM1",
         "turn_count": 1, "first_seen": "", "last_seen": ""},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK_TEST")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: None)
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    out = identity_discovery.resolve_with_names(network, cands)
    assert out[0]["external_id"] == "D0AMYBZ4RM1"
    assert "via_channel" not in out[0]
    assert out[0]["display_name"] is None


def test_slack_dm_rewrite_skipped_when_no_token(monkeypatch):
    """No Slack token available for this bot → D-id stays, no API call
    attempted."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "D0AKM4LTT99",
         "turn_count": 1, "first_seen": "", "last_seen": ""},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: None)
    called = []
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: called.append(channel_id) or None)
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    out = identity_discovery.resolve_with_names(network, cands)
    assert out[0]["external_id"] == "D0AKM4LTT99"
    # No conversations.info attempted since the token wasn't available.
    assert called == []


def test_slack_u_id_candidate_passes_through_unchanged(monkeypatch):
    """A Slack candidate whose external_id is already a U-id doesn't
    go through the D→U rewrite path — no conversations.info call,
    candidate is unmodified before name lookup."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "U0AN8B80AJY",
         "turn_count": 6, "first_seen": "", "last_seen": ""},
    ]
    from evolve_admin.evo import name_resolver
    called = []
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: called.append(channel_id) or None)
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    out = identity_discovery.resolve_with_names(network, cands)
    assert out[0]["external_id"] == "U0AN8B80AJY"
    assert "via_channel" not in out[0]
    # No D→U call attempted for a U-id.
    assert called == []


def test_slack_dm_rewrite_memoizes_per_d_id(monkeypatch):
    """Two candidates keyed on the same D-id (rare but defensive) only
    hit conversations.info once AND merge via the dedup pass since
    they're the same identity post-rewrite."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "D0AMYBZ4RM1", "turn_count": 5,
         "first_seen": "", "last_seen": "a"},
        {"channel": "slack", "external_id": "D0AMYBZ4RM1", "turn_count": 1,
         "first_seen": "", "last_seen": "b"},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK")
    calls: list[str] = []
    def fake_im(token, channel_id):
        calls.append(channel_id)
        return "U_DEDUP"
    monkeypatch.setattr(name_resolver, "slack_im_channel_to_user", fake_im)
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    out = identity_discovery.resolve_with_names(network, cands)
    # Dedup pass merges the two D-id candidates into one U_DEDUP row.
    assert len(out) == 1
    assert out[0]["external_id"] == "U_DEDUP"
    assert out[0]["turn_count"] == 6  # 5 + 1
    # Memo: D-id resolved exactly once for both candidates.
    assert calls == ["D0AMYBZ4RM1"]


def test_bot_id_threads_through_to_channel_token(monkeypatch):
    """resolve_with_names(bot_id=X) → _channel_token(prefer_bot_id=X).
    Critical for Slack workspace-scoped token routing — the per-bot
    Slack app's token must be used for THIS bot's user lookups."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "D0XYZ", "turn_count": 1,
         "first_seen": "", "last_seen": ""},
    ]
    from evolve_admin.evo import name_resolver
    seen_kwargs: list[dict] = []
    def fake_token(net, ch, **kw):
        seen_kwargs.append(dict(kw))
        return "TOK"
    monkeypatch.setattr(name_resolver, "_channel_token", fake_token)
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: "U999")
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    identity_discovery.resolve_with_names(network, cands, bot_id="team-bot-c")
    # The D→U rewrite invocation got the prefer_bot_id we passed.
    assert any(kw.get("prefer_bot_id") == "team-bot-c" for kw in seen_kwargs)


# ── F.1 follow-up: dedup after D→U rewrite ─────────────────────────────


def test_dedup_after_d_to_u_rewrite_merges_same_user(monkeypatch):
    """A user appears via two paths: D-channel (TurnObserver writes the
    IM channel id into ``channel``; D→U rewrite maps to user) AND
    directly (D.1's senderRegistry wrote user_id straight into the
    turn record). Both should merge into ONE candidate with summed
    turn_count and bounded first/last_seen."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "D0AMYBZ4RM1",
         "turn_count": 6, "first_seen": "2026-06-05T10:00:00Z",
         "last_seen": "2026-06-05T12:00:00Z"},
        {"channel": "slack", "external_id": "U_DUP",
         "turn_count": 19, "first_seen": "2026-06-01T08:00:00Z",
         "last_seen": "2026-06-08T11:00:00Z"},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: (
            "U_DUP" if channel_id == "D0AMYBZ4RM1" else None))
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)

    out = identity_discovery.resolve_with_names(network, cands)
    assert len(out) == 1
    merged = out[0]
    assert merged["external_id"] == "U_DUP"
    assert merged["turn_count"] == 25
    assert merged["first_seen"] == "2026-06-01T08:00:00Z"
    assert merged["last_seen"] == "2026-06-08T11:00:00Z"
    assert merged["via_channel"] == "D0AMYBZ4RM1"


def test_dedup_preserves_order_of_first_occurrence(monkeypatch):
    """Merged entry sits at the position of the FIRST occurrence so
    the upstream sort (count desc) survives."""
    network = {}
    cands = [
        {"channel": "slack", "external_id": "U_A", "turn_count": 10,
         "first_seen": "a", "last_seen": "z"},
        {"channel": "slack", "external_id": "U_B", "turn_count": 5,
         "first_seen": "a", "last_seen": "y"},
        {"channel": "slack", "external_id": "U_A", "turn_count": 3,
         "first_seen": "a", "last_seen": "x"},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: None)
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)

    out = identity_discovery.resolve_with_names(network, cands)
    assert [c["external_id"] for c in out] == ["U_A", "U_B"]
    assert out[0]["turn_count"] == 13


def test_dedup_keys_by_both_channel_and_id(monkeypatch):
    """Same external_id on different channels shouldn't merge."""
    network = {}
    cands = [
        {"channel": "telegram", "external_id": "U123", "turn_count": 1,
         "first_seen": "", "last_seen": ""},
        {"channel": "slack", "external_id": "U123", "turn_count": 1,
         "first_seen": "", "last_seen": ""},
    ]
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: None)
    monkeypatch.setattr(
        name_resolver, "resolve",
        lambda net, *, channel, external_id, bot_id=None: None)
    out = identity_discovery.resolve_with_names(network, cands)
    assert len(out) == 2
