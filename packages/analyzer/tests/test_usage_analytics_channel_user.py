"""compute_summary's By Channel + By User legibility (Usage page, Bite 1).

The Usage tab showed raw platform ids: By Channel shattered a Slack channel
across many ``:thread:<ts>`` rows and mixed in ``unknown`` / ``heartbeat``
system volume; By User keyed rows ``{channel}:{user_id}`` so the top rows were
``unknown:?`` / ``heartbeat:?`` (system traffic, NOT people).

These tests pin the data-layer half of the fix:

  - **By User = real people only.** Built from human-bucket turns; emits
    structured ``{platform, user_id, instance, calls}`` rows (no ``{channel}:``
    prefix); system/scheduled turns never appear.
  - **System volume relabeled in By Channel.** No-conversation turns (sentinel
    channels) bucket into NAMED categories (Heartbeat / Scheduled / Forge builds
    / Subagents / Evo / System), flagged ``system: True``.
  - **Thread roll-up.** ``<channel>:thread:<ts>`` rolls up to its parent with a
    ``threads`` child list.
  - **Platform inference** is shape-based and provider-neutral.

Placeholder ids/names only (no real bot/user names) so the scrub guard stays
green.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_analytics as ua  # noqa: E402


def _turn(
    *, source="human", channel="C0FAKECHAN", user_id="U0FAKEUSER",
    instance="bot_one", cost=0.5, session_id="s1", ts="2026-06-12T10:00:00Z",
    model="provx/used-lo",
):
    """Minimal valid turn record — only fields compute_summary reads."""
    return {
        "ts": ts,
        "instance": instance,
        "model": model,
        "provider": model.split("/")[0] if "/" in model else "provx",
        "source": source,
        "channel": channel,
        "user_id": user_id,
        "cost": cost,
        "input_tokens": 5,
        "output_tokens": 200,
        "cache_read_tokens": 10000,
        "cache_write_tokens": 0,
        "session_id": session_id,
        "auth_mode": "api_key",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Platform inference (shape-based, provider-neutral)
# ─────────────────────────────────────────────────────────────────────────────


def test_infer_platform_slack_prefixes():
    for cid in ("C0FAKECHAN", "D0FAKEDM11", "G0FAKEGRP1", "U0FAKEUSER", "W0ENT1"):
        assert ua._infer_platform(cid) == "slack"


def test_infer_platform_slack_thread_marker_any_case():
    # The conversation stem can be lower-cased in threaded ids; the :thread:
    # marker is the reliable signal regardless of case.
    assert ua._infer_platform("c0fakechan:thread:1781209840.707419") == "slack"


def test_infer_platform_telegram_numeric():
    assert ua._infer_platform("55501234") == "telegram"
    assert ua._infer_platform("-100123456") == "telegram"  # group, leading -


def test_infer_platform_unknown_shapes():
    assert ua._infer_platform("") == "unknown"
    assert ua._infer_platform(None) == "unknown"
    assert ua._infer_platform("weird-id") == "unknown"


def test_infer_platform_digitless_sentinel_words_are_not_slack():
    # F2 regression: a bare C/D/G/U/W prefix + alnum tail used to match ANY
    # English word starting with those letters, so these sentinel `channel`
    # values were misclassified as Slack and dumped into the "Slack user · ?"
    # By-User bucket (~63% of it). Real Slack ids always carry a digit; these
    # digit-free words must resolve to "unknown".
    for word in ("unknown", "webchat", "web", "discord", "cron", "Dummyname"):
        assert ua._infer_platform(word) == "unknown", word


def test_infer_platform_genuine_slack_ids_still_resolve():
    # Digit-bearing ids (the real shape) must remain "slack" after the fix —
    # uppercase, lowercased thread-parent stems, and a user id that leaked into
    # the channel field all count. Placeholder ids only.
    for cid in ("U9ZL3JYR3", "C0AK0FAKE1", "g0t7fake9", "c7t9fake6", "W0ENT1"):
        assert ua._infer_platform(cid) == "slack", cid


def test_compute_summary_webchat_unknown_not_in_slack_bucket():
    # End-to-end: human turns on channel=webchat / channel=unknown with no
    # user_id must NOT inflate a ("slack", "?") row. They belong in the
    # platform="unknown" bucket instead.
    turns = [
        _turn(channel="webchat", source="user", user_id=None,
              session_id="s-web-1"),
        _turn(channel="unknown", source="user", user_id=None,
              session_id="s-unk-1"),
        # A genuine Slack human turn for contrast — this one IS slack.
        _turn(channel="C0AK0FAKE1", source="user", user_id=None,
              session_id="s-slk-1"),
    ]
    summary = ua.compute_summary(turns)
    by_user = {(r["platform"], r["user_id"]): r["calls"] for r in summary["by_user"]}
    assert by_user.get(("slack", "?")) == 1  # only the real Slack turn
    assert by_user.get(("unknown", "?")) == 2  # webchat + unknown


def test_split_thread():
    assert ua._split_thread("C0FAKECHAN:thread:1781209840.707419") == (
        "C0FAKECHAN", "1781209840.707419")
    assert ua._split_thread("C0FAKECHAN") == ("C0FAKECHAN", None)


def test_system_category_mapping():
    assert ua._system_category("heartbeat") == "Heartbeat"
    assert ua._system_category("cron") == "Scheduled"
    assert ua._system_category("forge") == "Forge builds"
    assert ua._system_category("subagent") == "Subagents"
    assert ua._system_category("evo") == "Evo"
    assert ua._system_category("something-else") == "System"
    assert ua._system_category(None) == "System"


# ─────────────────────────────────────────────────────────────────────────────
# By User — real people only, structured rows
# ─────────────────────────────────────────────────────────────────────────────


def test_by_user_structured_rows_no_channel_prefix():
    rows = ua.compute_summary([_turn(source="human")])["by_user"]
    assert len(rows) == 1
    r = rows[0]
    assert r == {
        "platform": "slack",
        "user_id": "U0FAKEUSER",
        "instance": "bot_one",
        "calls": 1,
    }
    # The old `{channel}:{user_id}` key shape is gone.
    assert "user_key" not in r


def test_by_user_excludes_system_traffic():
    """heartbeat / cron / subagent / forge / unknown are NON-human and must not
    appear as users — this is what evicts the old `unknown:?` / `heartbeat:?`
    rows that topped the table."""
    summary = ua.compute_summary([
        _turn(source="human", user_id="U0FAKEUSER"),
        _turn(source="heartbeat", channel="heartbeat", user_id="?", session_id="s2"),
        _turn(source="cron", channel="unknown", user_id="?", session_id="s3"),
        _turn(source="subagent", channel="unknown", user_id="?", session_id="s4"),
        _turn(source="forge", channel="unknown", user_id="?", session_id="s5"),
        _turn(source="unknown", channel="unknown", user_id="?", session_id="s6"),
    ])
    rows = summary["by_user"]
    assert len(rows) == 1
    assert rows[0]["user_id"] == "U0FAKEUSER"
    assert all(r["user_id"] != "?" for r in rows)


def test_by_user_aggregates_same_person_across_channels():
    """The same person in two different conversations of the same platform
    collapses into one row (keyed by platform+user_id, not conversation)."""
    summary = ua.compute_summary([
        _turn(source="human", channel="C0FAKECHAN", user_id="U0FAKEUSER"),
        _turn(source="human", channel="D0FAKEDM11", user_id="U0FAKEUSER",
              session_id="s2"),
    ])
    rows = summary["by_user"]
    assert len(rows) == 1
    assert rows[0]["calls"] == 2


def test_by_user_dominant_instance_wins():
    """instance = the bot the person most interacts with (used for cache-only
    name resolution at the route)."""
    summary = ua.compute_summary([
        _turn(source="human", user_id="U0FAKEUSER", instance="bot_two"),
        _turn(source="human", user_id="U0FAKEUSER", instance="bot_one",
              session_id="s2"),
        _turn(source="human", user_id="U0FAKEUSER", instance="bot_one",
              session_id="s3"),
    ])
    assert summary["by_user"][0]["instance"] == "bot_one"


def test_by_user_telegram_user_id_inference():
    summary = ua.compute_summary([
        _turn(source="human", channel="55501234", user_id="55501234"),
    ])
    assert summary["by_user"][0]["platform"] == "telegram"


def test_by_user_falls_back_to_channel_platform_when_user_opaque():
    """A human turn with an opaque user_id still infers platform from the
    conversation id rather than collapsing to unknown."""
    summary = ua.compute_summary([
        _turn(source="human", channel="C0FAKECHAN", user_id="?"),
    ])
    assert summary["by_user"][0]["platform"] == "slack"


# ─────────────────────────────────────────────────────────────────────────────
# By Channel — system relabel + thread roll-up
# ─────────────────────────────────────────────────────────────────────────────


def test_by_channel_system_relabel():
    summary = ua.compute_summary([
        _turn(source="heartbeat", channel="heartbeat", user_id="?"),
        _turn(source="cron", channel="unknown", user_id="?", session_id="s2"),
        _turn(source="forge", channel="unknown", user_id="?", session_id="s3"),
        _turn(source="subagent", channel="", user_id="?", session_id="s4"),
        _turn(source="evo", channel="unknown", user_id="?", session_id="s5"),
        _turn(source="weird", channel="unknown", user_id="?", session_id="s6"),
    ])
    rows = summary["by_channel"]
    cats = {r["category"]: r for r in rows if r.get("system")}
    assert set(cats) == {
        "Heartbeat", "Scheduled", "Forge builds", "Subagents", "Evo", "System",
    }
    for r in cats.values():
        assert r["system"] is True
        assert r["calls"] == 1
        assert r["threads"] == []


def test_by_channel_no_system_rows_for_real_conversations():
    summary = ua.compute_summary([_turn(source="human", channel="C0FAKECHAN")])
    rows = summary["by_channel"]
    assert len(rows) == 1
    assert rows[0]["system"] is False
    assert rows[0]["channel"] == "C0FAKECHAN"


def test_by_channel_thread_rollup():
    """All threads of a channel roll into one parent row with a threads child
    list; parent totals sum the thread + direct legs."""
    summary = ua.compute_summary([
        # one direct (non-thread) turn on the parent channel
        _turn(source="human", channel="C0FAKECHAN", cost=1.0, session_id="s0"),
        # two turns in thread A
        _turn(source="human", channel="C0FAKECHAN:thread:111.1", cost=0.5,
              session_id="s1"),
        _turn(source="human", channel="C0FAKECHAN:thread:111.1", cost=0.5,
              session_id="s2"),
        # one turn in thread B
        _turn(source="human", channel="C0FAKECHAN:thread:222.2", cost=2.0,
              session_id="s3"),
    ])
    rows = [r for r in summary["by_channel"] if not r.get("system")]
    assert len(rows) == 1
    parent = rows[0]
    assert parent["channel"] == "C0FAKECHAN"
    assert parent["calls"] == 4
    assert abs(parent["cost"] - 4.0) < 1e-9
    threads = {t["thread_ts"]: t for t in parent["threads"]}
    assert set(threads) == {"111.1", "222.2"}
    assert threads["111.1"]["calls"] == 2
    assert abs(threads["111.1"]["cost"] - 1.0) < 1e-9
    assert threads["222.2"]["calls"] == 1
    # Thread legs sorted by calls desc.
    assert parent["threads"][0]["thread_ts"] == "111.1"


def test_by_channel_unthreaded_has_empty_threads_list():
    summary = ua.compute_summary([_turn(source="human", channel="C0FAKECHAN")])
    assert summary["by_channel"][0]["threads"] == []


def test_real_conversations_sorted_before_system_rows():
    summary = ua.compute_summary([
        _turn(source="heartbeat", channel="heartbeat", user_id="?"),
        _turn(source="heartbeat", channel="heartbeat", user_id="?", session_id="s2"),
        _turn(source="human", channel="C0FAKECHAN", session_id="s3"),
    ])
    rows = summary["by_channel"]
    # real conversation row(s) come first, system block after
    assert rows[0]["system"] is False
    assert rows[-1]["system"] is True


def test_channel_cost_totals_consistent_with_total_cost():
    """Every turn's cost lands in exactly one By Channel row (real OR system),
    so the by_channel cost total reconciles with the summary total."""
    turns = [
        _turn(source="human", channel="C0FAKECHAN", cost=1.0, session_id="s1"),
        _turn(source="human", channel="C0FAKECHAN:thread:1.1", cost=2.0,
              session_id="s2"),
        _turn(source="heartbeat", channel="heartbeat", user_id="?", cost=0.25,
              session_id="s3"),
    ]
    summary = ua.compute_summary(turns)
    chan_total = sum(r["cost"] for r in summary["by_channel"])
    assert abs(chan_total - summary["total_cost"]) < 1e-9


def test_empty_input_safe():
    summary = ua.compute_summary([])
    assert summary["by_user"] == []
    assert summary["by_channel"] == []
