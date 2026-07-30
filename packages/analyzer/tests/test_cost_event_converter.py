"""Tests for cost_event_converter — schema mapping + idempotent writes.

The converter replaces the broken plugin llm_output → cost_event emission
path. These tests pin:

  1. Schema mapping (turn-collector → cost_event) — provider/model split,
     trigger_kind from source/channel, cache_state via the same rules the
     plugin's CostLedger.ts uses.
  2. Skipping zero-token rows (matches the old plugin behavior at
     TurnObserver.ts:299).
  3. Idempotency — running over the same source twice yields no
     duplicates, keyed by (ts, session_id).
  4. Mixed input/output ts handling — the legacy turn-collector writes
     ``+00:00`` suffixes; the plugin always wrote ``Z``. The converter
     normalizes to ``Z`` so historical and new records compare cleanly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_event_converter as cec  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Pure mappers
# ─────────────────────────────────────────────────────────────────────────────


def test_split_provider_model_canonical():
    assert cec._split_provider_model("anthropic/claude-sonnet-4-6") == (
        "anthropic", "claude-sonnet-4-6"
    )


def test_split_provider_model_no_prefix():
    # Not seen in real records, but the fallback shouldn't crash rollups.
    assert cec._split_provider_model("claude-haiku") == ("unknown", "claude-haiku")


def test_split_provider_model_empty():
    assert cec._split_provider_model("") == ("unknown", "unknown")
    assert cec._split_provider_model(None) == ("unknown", "unknown")  # type: ignore[arg-type]


def test_infer_trigger_kind_human():
    assert cec._infer_trigger_kind("human", "telegram") == "user_turn"
    assert cec._infer_trigger_kind("human", "slack") == "user_turn"


def test_infer_trigger_kind_heartbeat_source():
    """source=heartbeat is the bot's hourly autonomous wake-up.

    Previously fell through to 'unknown' because the classifier didn't
    match it — that bug accounted for ~95% of misclassified events on
    security_bot/admin_bot/team_bot_b before the fix.
    """
    assert cec._infer_trigger_kind("heartbeat", "heartbeat") == "heartbeat"
    assert cec._infer_trigger_kind("heartbeat", None) == "heartbeat"


def test_infer_trigger_kind_cron_app():
    """source=cron is a scheduled cron-app invocation, distinct from heartbeat."""
    assert cec._infer_trigger_kind("cron", None) == "cron_app"
    assert cec._infer_trigger_kind("cron", "telegram") == "cron_app"


def test_infer_trigger_kind_subagent():
    """source=subagent now lands in its own bucket instead of 'unknown'."""
    assert cec._infer_trigger_kind("subagent", "subagent") == "subagent"
    assert cec._infer_trigger_kind("subagent", None) == "subagent"


def test_infer_trigger_kind_forge():
    """source=forge is set by forge_sessions.retag_turn_source for OC
    `--local --agent main` dispatches from the forge engine. Without the
    branch they fall through to "unknown"; with it they get their own
    Usage-tab bucket instead of inflating Human (channel:unknown)."""
    assert cec._infer_trigger_kind("forge", "unknown") == "forge"
    assert cec._infer_trigger_kind("forge", None) == "forge"


def test_infer_trigger_kind_channel_fallback():
    """When source is missing/unknown, the channel still tells us a lot."""
    assert cec._infer_trigger_kind(None, "heartbeat") == "heartbeat"
    assert cec._infer_trigger_kind("", "cron-event") == "cron_app"
    assert cec._infer_trigger_kind(None, "exec-event") == "subagent"


def test_infer_trigger_kind_unknown_fallthrough():
    assert cec._infer_trigger_kind(None, None) == "unknown"
    assert cec._infer_trigger_kind("", "") == "unknown"
    assert cec._infer_trigger_kind("weird", "stuff") == "unknown"


def test_infer_cache_state_fresh_with_input():
    # No cache participation but did do real input → fresh, not unknown.
    # Mirrors CostLedger.ts inferCacheState first branch.
    assert cec._infer_cache_state(0, 0, 100) == "fresh"


def test_infer_cache_state_unknown_when_truly_empty():
    assert cec._infer_cache_state(0, 0, 0) == "unknown"


def test_infer_cache_state_warm_at_threshold():
    # 80% read ratio is the cliff per CostLedger.ts:74. Anything below
    # falls through to invalidated/unknown depending on writes.
    assert cec._infer_cache_state(80, 0, 20) == "warm"
    assert cec._infer_cache_state(79, 0, 21) == "unknown"


def test_infer_cache_state_invalidated():
    # Lots of cacheWrite but cacheRead is <=10% of prompt → key rotation
    # / config churn fingerprint. Threshold per CostLedger.ts:78.
    assert cec._infer_cache_state(5, 100, 50) == "invalidated"


def test_normalize_ts_strips_plus_00_to_Z():
    assert cec._normalize_ts("2026-05-03T03:22:09.688000+00:00") == (
        "2026-05-03T03:22:09.688000Z"
    )


def test_normalize_ts_passes_z_through():
    assert cec._normalize_ts("2026-05-03T03:22:09Z") == "2026-05-03T03:22:09Z"


def test_normalize_ts_handles_missing():
    assert cec._normalize_ts(None) is None
    assert cec._normalize_ts("") is None


# ─────────────────────────────────────────────────────────────────────────────
# turn_to_cost_event — full record mapping
# ─────────────────────────────────────────────────────────────────────────────


def _example_turn() -> dict:
    """A real-shaped turn-collector record — copied from admin_bot's 2026-05-03 file
    so the schema test stays grounded in observed data, not invention."""
    return {
        "ts": "2026-05-03T00:07:30.652000+00:00",
        "instance": "admin_bot",
        "channel": "telegram",
        "user_id": "unknown",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "cost": 0.01911945,
        "input_tokens": 3,
        "output_tokens": 82,
        "cache_read_tokens": 13014,
        "cache_write_tokens": 3727,
        "session_id": "504a4d14-a34b-41c7-ba15-414a771131b2",
        "auth_mode": "api_key",
        "auth_note": "inferred from primary profile",
    }


def test_turn_to_cost_event_full_record():
    event = cec.turn_to_cost_event(_example_turn(), bot_id="admin_bot")
    assert event is not None
    # Schema v2 — added optional user/channel/timestamp_local fields. The
    # core v1 keys still match exactly; the new keys are all present with
    # None for this fixture (no user_id field set, channel=telegram).
    assert event["schema_version"] == 2
    assert event["type"] == "cost_event"
    assert event["ts"] == "2026-05-03T00:07:30.652000Z"  # +00:00 → Z
    assert event["bot_id"] == "admin_bot"
    assert event["session_id"] == "504a4d14-a34b-41c7-ba15-414a771131b2"
    assert event["trigger_kind"] == "user_turn"  # from source=human
    assert event["model"] == "claude-sonnet-4-6"
    assert event["provider"] == "anthropic"
    # ratio = 13014/(3+13014+3727) ≈ 0.777 — below 0.80, write != 0,
    # readRatio (~0.78) > 0.10, so it lands in "unknown" not "invalidated".
    # This is the correct CostLedger.ts behavior; the threshold is strict.
    assert event["cache_state"] == "unknown"
    assert event["input_tokens"] == 3
    assert event["output_tokens"] == 82
    assert event["cache_read_tokens"] == 13014
    assert event["cache_write_tokens"] == 3727
    assert event["cost_usd"] == pytest.approx(0.01911945)
    # v2 fields always present in the output dict, even when source data
    # is missing them — UI relies on consistent shape (renders "(unknown)"
    # for None, drops the row when the key is absent entirely).
    assert "timestamp_local" in event
    assert "user_id" in event
    assert "user_display_name" in event
    assert "channel_id" in event
    assert "channel_kind" in event
    # The fixture has user_id="unknown" (turn-collector sentinel) which
    # the converter normalizes to None.
    assert event["user_id"] is None
    # channel="telegram" + trigger=user_turn → telegram_dm by default
    # (no channel_id present in fixture; converter assumes DM).
    assert event["channel_kind"] == "telegram_dm"
    # No name cache → user_display_name stays None.
    assert event["user_display_name"] is None


def test_turn_to_cost_event_skips_zero_token():
    """Plugin used to filter input+output+cache_*=0; converter must too,
    or we'd flood rollups with health-probe noise."""
    bare = {
        "ts": "2026-05-03T00:00:00Z",
        "session_id": "x",
        "model": "anthropic/claude-haiku-4-5",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert cec.turn_to_cost_event(bare, bot_id="team_bot_a") is None


def test_turn_to_cost_event_skips_missing_ts():
    """No timestamp → no record. Rolled-up by-day analytics rely on ts."""
    bad = _example_turn()
    bad["ts"] = None
    assert cec.turn_to_cost_event(bad, bot_id="admin_bot") is None


def test_turn_to_cost_event_handles_string_int_tokens():
    """turn-collector usually writes ints, but a stray string from a
    malformed log line shouldn't crash the converter."""
    weird = _example_turn()
    weird["input_tokens"] = "3"  # type: ignore[assignment]
    weird["output_tokens"] = "82"  # type: ignore[assignment]
    event = cec.turn_to_cost_event(weird, bot_id="admin_bot")
    assert event is not None
    assert event["input_tokens"] == 3
    assert event["output_tokens"] == 82


# ─────────────────────────────────────────────────────────────────────────────
# convert_day — file I/O + idempotency
# ─────────────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_convert_day_no_source_file(tmp_path, monkeypatch):
    """Quiet bot has no turns file — that's normal, not an error.

    monkeypatch _bot_home so the converter looks under tmp_path and
    not /Users/<bot>/.openclaw."""
    monkeypatch.setattr(cec, "_bot_home", lambda bot_id: tmp_path / "bots" / bot_id)
    from datetime import date
    report = cec.convert_day("team_bot_a", date(2026, 5, 3), tmp_path / "shared")
    assert report["found"] == 0
    assert report["written"] == 0


def test_convert_day_writes_and_dedups(tmp_path, monkeypatch):
    """Writing twice over the same source produces no duplicates."""
    from datetime import date
    target = date(2026, 5, 3)

    monkeypatch.setattr(cec, "_bot_home", lambda bot_id: tmp_path / "bots" / bot_id)

    src = tmp_path / "bots" / "admin_bot" / ".openclaw" / "workspace" / "memory" / f"turns-{target.isoformat()}.jsonl"
    turn = _example_turn()
    other = _example_turn()
    other["ts"] = "2026-05-03T00:08:00.000000+00:00"
    other["session_id"] = "different-session"
    _write_jsonl(src, [turn, other])

    shared = tmp_path / "shared"
    r1 = cec.convert_day("admin_bot", target, shared)
    assert r1["found"] == 2
    assert r1["written"] == 2

    out = shared / "annotations" / "admin_bot" / f"cost_events-{target.isoformat()}.jsonl"
    assert out.exists()
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2

    # Re-run — should detect both and skip both.
    r2 = cec.convert_day("admin_bot", target, shared)
    assert r2["found"] == 2
    assert r2["written"] == 0
    assert r2["skipped"] == 2

    # File still has only the original 2 lines.
    assert len(out.read_text().strip().splitlines()) == 2


def test_convert_day_appends_new_record(tmp_path, monkeypatch):
    """Adding a new turn to the source file → only the new one gets written.

    Models the production pattern: turn-collector.py overwrites the source
    file every day, so the source typically grows over the day. The
    converter runs every 15 min and should pick up only the additions."""
    from datetime import date
    target = date(2026, 5, 3)

    monkeypatch.setattr(cec, "_bot_home", lambda bot_id: tmp_path / "bots" / bot_id)

    src = tmp_path / "bots" / "admin_bot" / ".openclaw" / "workspace" / "memory" / f"turns-{target.isoformat()}.jsonl"
    first = _example_turn()
    _write_jsonl(src, [first])

    shared = tmp_path / "shared"
    r1 = cec.convert_day("admin_bot", target, shared)
    assert r1["written"] == 1

    second = _example_turn()
    second["ts"] = "2026-05-03T00:08:00.000000+00:00"
    second["session_id"] = "second-session"
    _write_jsonl(src, [first, second])

    r2 = cec.convert_day("admin_bot", target, shared)
    assert r2["found"] == 2
    assert r2["written"] == 1
    assert r2["skipped"] == 1

    out = shared / "annotations" / "admin_bot" / f"cost_events-{target.isoformat()}.jsonl"
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    keys = {(json.loads(l)["ts"], json.loads(l)["session_id"]) for l in lines}
    assert (
        "2026-05-03T00:07:30.652000Z",
        "504a4d14-a34b-41c7-ba15-414a771131b2",
    ) in keys
    assert ("2026-05-03T00:08:00.000000Z", "second-session") in keys


def test_convert_day_dry_run_writes_nothing(tmp_path, monkeypatch):
    from datetime import date
    target = date(2026, 5, 3)

    monkeypatch.setattr(cec, "_bot_home", lambda bot_id: tmp_path / "bots" / bot_id)

    src = tmp_path / "bots" / "admin_bot" / ".openclaw" / "workspace" / "memory" / f"turns-{target.isoformat()}.jsonl"
    _write_jsonl(src, [_example_turn()])

    shared = tmp_path / "shared"
    report = cec.convert_day("admin_bot", target, shared, dry_run=True)
    assert report["found"] == 1
    assert report["written"] == 1  # would-have-been count
    out = shared / "annotations" / "admin_bot" / f"cost_events-{target.isoformat()}.jsonl"
    assert not out.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Schema v2 enrichment — channel_kind, user_id, display name, local time
# ─────────────────────────────────────────────────────────────────────────────


def test_infer_channel_kind_telegram_dm():
    # Positive integer chat id → DM. Default also DM when id absent.
    assert cec._infer_channel_kind("telegram", "123456789", "user_turn") == "telegram_dm"
    assert cec._infer_channel_kind("telegram", None, "user_turn") == "telegram_dm"


def test_infer_channel_kind_telegram_group():
    # Telegram groups use negative ids.
    assert cec._infer_channel_kind("telegram", "-100123", "user_turn") == "telegram_group"


def test_infer_channel_kind_slack_dm_vs_channel():
    # Slack channel id prefix carries the type:
    #   D… = DM, C…/G… = (private/public) channel.
    assert cec._infer_channel_kind("slack", "D0AKX41HELU", "user_turn") == "slack_dm"
    assert cec._infer_channel_kind("slack", "C12345", "user_turn") == "slack_channel"
    assert cec._infer_channel_kind("slack", "G98765", "user_turn") == "slack_channel"


def test_infer_channel_kind_internal_for_heartbeat():
    """Heartbeats / cron / subagent → always 'internal' regardless of channel."""
    assert cec._infer_channel_kind("telegram", "123", "heartbeat") == "internal"
    assert cec._infer_channel_kind("slack", "D0AKX", "cron_app") == "internal"
    assert cec._infer_channel_kind("heartbeat", None, "heartbeat") == "internal"
    assert cec._infer_channel_kind(None, None, "subagent") == "internal"


def test_infer_channel_kind_discord_default_channel():
    # Discord ids don't encode DM-vs-channel; default to channel.
    assert cec._infer_channel_kind("discord", "1234567890", "user_turn") == "discord_channel"


def test_user_id_from_turn_normalizes_sentinels():
    """turn-collector writes the literal 'unknown' when it can't resolve."""
    assert cec._user_id_from_turn({"user_id": "unknown"}) is None
    assert cec._user_id_from_turn({"user_id": "UNKNOWN"}) is None
    assert cec._user_id_from_turn({"user_id": ""}) is None
    assert cec._user_id_from_turn({"user_id": None}) is None
    assert cec._user_id_from_turn({"user_id": "U0518A544N5"}) == "U0518A544N5"


def test_channel_id_from_turn_prefers_explicit():
    """Schema v2: explicit channel_id wins over Telegram-DM fallback."""
    assert cec._channel_id_from_turn(
        {"channel_id": "D0AKX41HELU", "channel": "slack", "user_id": "U123"}
    ) == "D0AKX41HELU"
    # Telegram-DM fallback: channel_id == user_id when not explicit.
    assert cec._channel_id_from_turn(
        {"channel": "telegram", "user_id": "987654321"}
    ) == "987654321"
    # Slack with no explicit channel_id: no fallback (user_id != channel id).
    assert cec._channel_id_from_turn(
        {"channel": "slack", "user_id": "U123"}
    ) is None


def test_to_local_iso_converts():
    tz = cec.ZoneInfo("America/Los_Angeles")
    # 2026-05-29 00:52 UTC → 2026-05-28 17:52 PT (PDT, UTC-7)
    out = cec._to_local_iso("2026-05-29T00:52:00Z", tz)
    assert out is not None
    assert out.startswith("2026-05-28T17:52")
    # +00:00 form works too.
    out2 = cec._to_local_iso("2026-05-29T00:52:00+00:00", tz)
    assert out2 == out


def test_to_local_iso_returns_none_on_bad_input():
    tz = cec.ZoneInfo("UTC")
    assert cec._to_local_iso(None, tz) is None
    assert cec._to_local_iso("", tz) is None
    assert cec._to_local_iso("not-a-date", tz) is None


def test_turn_to_cost_event_kit_session_3d5cde22_shape(tmp_path, monkeypatch):
    """Pin the team-bot-a session 3d5cde22 motivating example (PR E body).

    The original triage chain:
      session_id 3d5cde22 / channel D0AKX41HELU / user U0518A544N5 / Peter

    cost_event_converter must propagate all four through so the cost
    alert can render "Triggered by Peter (Slack DM) at 2026-05-28 17:52 PT".
    """
    # Pre-populate the network.json display-name cache so the converter
    # can resolve U0518A544N5 → Peter without an API call.
    network = {
        "timezone": "America/Los_Angeles",
        "pod": {
            "admins": {
                "resolved_names": {
                    "slack:U0518A544N5": {
                        "username": "peter",
                        "name": "Peter",
                        "cached_at": "2026-05-28T00:00:00Z",
                    }
                }
            }
        },
    }
    tz = cec.ZoneInfo("America/Los_Angeles")
    turn = {
        "ts": "2026-05-29T00:52:00Z",
        "instance": "team-bot-a",
        "channel": "slack",
        "channel_id": "D0AKX41HELU",
        "user_id": "U0518A544N5",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "cost": 0.45,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 0,
        "session_id": "3d5cde22-1111-2222-3333-444455556666",
    }
    event = cec.turn_to_cost_event(turn, bot_id="team-bot-a", network=network, pod_tz=tz)
    assert event is not None
    assert event["user_id"] == "U0518A544N5"
    assert event["channel_id"] == "D0AKX41HELU"
    assert event["channel_kind"] == "slack_dm"
    assert event["user_display_name"] == "Peter"
    # 00:52 UTC on May 29 → 17:52 PT on May 28.
    assert event["timestamp_local"] is not None
    assert event["timestamp_local"].startswith("2026-05-28T17:52")


def test_turn_to_cost_event_heartbeat_is_internal():
    """Heartbeat sessions never carry external user / channel ids."""
    turn = {
        "ts": "2026-05-29T00:52:00Z",
        "instance": "team-bot-a",
        "channel": "heartbeat",
        "user_id": "U0518A544N5",  # source might have something stale
        "model": "anthropic/claude-haiku-4-5",
        "source": "heartbeat",
        "cost": 0.001,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "session_id": "hb-1",
    }
    event = cec.turn_to_cost_event(turn, bot_id="team-bot-a")
    assert event is not None
    assert event["trigger_kind"] == "heartbeat"
    assert event["channel_kind"] == "internal"
    # Defensive zeroing: even if the source record had a user_id, the
    # converter strips it for internal triggers so a stray field on the
    # source can't accidentally pin a heartbeat to a person.
    assert event["user_id"] is None
    assert event["channel_id"] is None


def test_display_name_dnt_honored_per_bot():
    """Per-bot DNT opt-out skips display-name cache lookups."""
    network = {
        "bots": {"team-bot-a": {"costAlertDisplayName": False}},
        "pod": {
            "admins": {
                "resolved_names": {
                    "slack:U0518A544N5": {
                        "username": "peter",
                        "name": "Peter",
                        "cached_at": "2026-05-28T00:00:00Z",
                    }
                }
            }
        },
    }
    # Cache lookup direct via helper — confirms the DNT predicate is
    # what's skipping the resolution, not a missing cache entry.
    assert cec._display_name_dnt_enabled("team-bot-a", network) is True
    assert cec._display_name_dnt_enabled("admin_bot", network) is False
    name = cec._resolve_display_name(
        network=network, channel="slack", channel_kind="slack_dm",
        user_id="U0518A544N5", bot_id="team-bot-a",
    )
    assert name is None  # DNT honored — even though cache has the entry
    # Same call for a non-DNT bot returns the cached name.
    name2 = cec._resolve_display_name(
        network=network, channel="slack", channel_kind="slack_dm",
        user_id="U0518A544N5", bot_id="admin_bot",
    )
    assert name2 == "Peter"


def test_display_name_dnt_pod_wide():
    """Pod-wide costAlertDisplayName=false overrides all bots."""
    network = {"costAlertDisplayName": False, "bots": {}}
    assert cec._display_name_dnt_enabled("any_bot", network) is True


def test_display_name_cache_miss_returns_none():
    """Cache miss yields None — converter never calls the platform API."""
    network = {"pod": {"admins": {"resolved_names": {}}}}
    name = cec._resolve_display_name(
        network=network, channel="slack", channel_kind="slack_dm",
        user_id="U_NOT_CACHED", bot_id="team-bot-a",
    )
    assert name is None


def test_backward_compat_old_v1_record_still_parses():
    """Old v1 cost_event records (no v2 fields) must read cleanly.

    Simulates a cost_events JSONL file with a pre-PR-E record. The
    rollups + watchdog detectors must tolerate missing v2 keys without
    KeyError. This is the safety check for the 90-day archive window —
    historical data stays readable through the schema bump.
    """
    v1_record = {
        "schema_version": 1,
        "type": "cost_event",
        "ts": "2026-05-01T12:00:00Z",
        "bot_id": "team-bot-a",
        "session_id": "old-session",
        "trigger_kind": "user_turn",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "cache_state": "warm",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 0,
        "cost_usd": 0.05,
        # No timestamp_local, user_id, etc.
    }
    # The cost_ledger rollups must still work over this record.
    import cost_ledger as cl
    by_session = cl.rollup_per_session([v1_record])
    assert "old-session" in by_session
    # v2 fields land in the rollup as None — UI renders "(unknown)".
    assert by_session["old-session"]["first_user_id"] is None
    assert by_session["old-session"]["first_channel_kind"] is None
    assert by_session["old-session"]["first_timestamp_local"] is None
    # And the daily/per-bot rollup still aggregates the cost correctly.
    by_day = cl.rollup_per_bot_day([v1_record])
    assert by_day[("team-bot-a", "2026-05-01")]["cost_usd"] == pytest.approx(0.05)
