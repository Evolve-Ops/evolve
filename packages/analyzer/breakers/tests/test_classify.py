"""Tests for breakers.classify — turn-classification helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from breakers.classify import (
    AUTO_CHANNEL_TELLS,
    AUTO_SOURCES,
    HUMAN_CHANNELS,
    HUMAN_SOURCES,
    classify_model_tier,
    classify_turn,
    filter_window,
    is_auto_source,
    parse_ts,
)


class TestClassifyModelTier:
    @pytest.mark.parametrize("model", [
        "anthropic/claude-haiku-4-5",
        "claude-haiku-4-5",
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash",
        "xai/grok-4-mini",
    ])
    def test_low_tier_matches(self, model: str) -> None:
        assert classify_model_tier(model) == "low"

    @pytest.mark.parametrize("model", [
        "anthropic/claude-sonnet-4-6",
        "claude-opus-4",
        "openai/gpt-4o",
        "openai/gpt-4.1",
        "xai/grok-4",
        # NOTE: "google/gemini-2.0-pro" should be high tier but is
        # misclassified as low because the classifier substring-matches
        # "mini" inside "gemini". Pre-existing bug in
        # packages/analyzer/metrics/resolvers/cost_metrics.py mirrored
        # here. Worth a follow-up fix upstream (tokens should be word-
        # bounded), but out of scope for the breakers Phase 1 PR.
    ])
    def test_high_tier_matches(self, model: str) -> None:
        assert classify_model_tier(model) == "high"

    def test_gemini_misclassified_substring_bug(self) -> None:
        """Document the gemini/mini substring-collision: gemini-2.0-pro
        is classified as low because "mini" is a substring of "gemini".
        This mirrors the upstream cost_metrics.classify_model_tier bug.
        Phase 1 doesn't fix it — see the NOTE in test_high_tier_matches."""
        assert classify_model_tier("google/gemini-2.0-pro") == "low"

    def test_low_wins_over_high(self) -> None:
        # gpt-4o-mini contains both 'gpt-4' (high) and 'mini' (low). Low wins.
        assert classify_model_tier("openai/gpt-4o-mini") == "low"

    @pytest.mark.parametrize("model", [None, "", "unknown-model-name"])
    def test_unknown(self, model: str | None) -> None:
        assert classify_model_tier(model) == "unknown"


class TestIsAutoSource:
    @pytest.mark.parametrize("source", sorted(AUTO_SOURCES))
    def test_auto_source_vetoed(self, source: str) -> None:
        assert is_auto_source({"source": source, "channel": "slack"}) is True

    @pytest.mark.parametrize("channel", sorted(AUTO_CHANNEL_TELLS))
    def test_auto_channel_vetoed(self, channel: str) -> None:
        # Even with source=user, an auto-tell channel triggers veto —
        # channel=unknown is the load-bearing case (scheduler-spawned
        # turns missing routing identifier).
        assert is_auto_source({"source": "user", "channel": channel}) is True

    def test_human_through_slack_allowed(self) -> None:
        assert is_auto_source({"source": "human", "channel": "slack"}) is False

    def test_user_through_telegram_allowed(self) -> None:
        assert is_auto_source({"source": "user", "channel": "telegram"}) is False

    def test_missing_fields_fails_open(self) -> None:
        # Missing both source and channel — fail-open in user's favor.
        assert is_auto_source({}) is False

    def test_case_insensitive(self) -> None:
        assert is_auto_source({"source": "HEARTBEAT", "channel": "slack"}) is True
        assert is_auto_source({"source": "Human", "channel": "SLACK"}) is False


class TestClassifyTurn:
    def test_heartbeat_haiku(self) -> None:
        c = classify_turn({
            "source": "heartbeat", "channel": "heartbeat",
            "model": "anthropic/claude-haiku-4-5",
        })
        assert c.bucket == "auto"
        assert c.model_tier == "low"

    def test_telegram_human_sonnet(self) -> None:
        c = classify_turn({
            "source": "human", "channel": "telegram",
            "model": "anthropic/claude-sonnet-4-6",
        })
        assert c.bucket == "human"
        assert c.model_tier == "high"

    def test_cron_sonnet_classified_auto_high(self) -> None:
        c = classify_turn({
            "source": "cron", "channel": "unknown",
            "model": "anthropic/claude-sonnet-4-6",
        })
        assert c.bucket == "auto"
        assert c.model_tier == "high"

    def test_unknown_channel_user_source_is_ambiguous(self) -> None:
        # Detection-side: don't count this toward the auto-rate spike.
        # (Enforcement-side via is_auto_source DOES veto it — see above.)
        c = classify_turn({
            "source": "user", "channel": "unknown",
            "model": "anthropic/claude-haiku-4-5",
        })
        assert c.bucket == "ambiguous"

    def test_empty_turn_is_ambiguous(self) -> None:
        c = classify_turn({})
        assert c.bucket == "ambiguous"


class TestParseTs:
    def test_iso_with_z(self) -> None:
        dt = parse_ts({"ts": "2026-05-20T16:00:00Z"})
        assert dt == datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)

    def test_iso_with_offset(self) -> None:
        dt = parse_ts({"ts": "2026-05-20T16:00:00+00:00"})
        assert dt == datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)

    def test_naive_treated_as_utc(self) -> None:
        dt = parse_ts({"ts": "2026-05-20T16:00:00"})
        assert dt is not None
        assert dt.tzinfo is timezone.utc

    @pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345])
    def test_invalid_returns_none(self, bad) -> None:
        assert parse_ts({"ts": bad}) is None

    def test_missing_ts_returns_none(self) -> None:
        assert parse_ts({}) is None


class TestFilterWindow:
    def test_inclusive_start_exclusive_end(self) -> None:
        turns = [
            {"ts": "2026-05-20T15:59:59Z"},  # before
            {"ts": "2026-05-20T16:00:00Z"},  # start (inclusive)
            {"ts": "2026-05-20T16:30:00Z"},  # in
            {"ts": "2026-05-20T17:00:00Z"},  # end (exclusive)
            {"ts": "2026-05-20T17:00:01Z"},  # after
        ]
        out = filter_window(
            turns,
            start=datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 20, 17, 0, tzinfo=timezone.utc),
        )
        assert len(out) == 2
        assert out[0]["ts"] == "2026-05-20T16:00:00Z"
        assert out[1]["ts"] == "2026-05-20T16:30:00Z"

    def test_drops_untimestamped(self) -> None:
        turns = [{"ts": "2026-05-20T16:00:00Z"}, {}, {"ts": "bad"}]
        out = filter_window(
            turns,
            start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc),
        )
        assert len(out) == 1
