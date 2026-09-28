"""tests/test_observability_signal_adapter.py — V1.5-1 signal adapter.

Covers signals.store.observe_from_opik: end-to-end span → Signal
including the dedup/find-or-create semantics inherited from observe().
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from observability.opik_client import OpikSpan  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _error_span(**overrides) -> OpikSpan:
    base = dict(
        name="embedding_call",
        start_time=datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 14, 0, 1, tzinfo=timezone.utc),
        type="llm",
        producer="embedding_monitor",
        bot_id="admin_bot",
        provider="openai",
        error_info={"http_status": 429, "error_class": "quota_exceeded"},
        tags=["maintenance", "warn"],
        metadata={"context": "memory_search"},
        attributes={"event_type": "provider_failing", "details": {"provider": "openai", "error_class": "quota_exceeded"}},
    )
    base.update(overrides)
    return OpikSpan(**base)


def test_observe_from_opik_creates_signal_with_expected_signature(tmp_path: Path):
    span = _error_span()
    sig = signals_store.observe_from_opik(
        tmp_path,
        span,
        type="provider_failing",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        signature_suffix="openai/quota_exceeded",
    )
    assert sig.producer == "embedding_monitor"
    assert sig.type == "provider_failing"
    assert sig.bot_id == "admin_bot"
    # Signature: producer:type:bot_id/suffix
    assert sig.signature == "embedding_monitor:provider_failing:admin_bot/openai/quota_exceeded"
    # State + flavor + severity
    assert sig.state == "firing"
    assert sig.flavor == "maintenance"
    assert sig.severity == "warn"
    # Details include observability metadata
    obs = sig.details["observability"]
    assert obs["name"] == "embedding_call"
    assert obs["provider"] == "openai"
    assert obs["metadata"] == {"context": "memory_search"}
    # error_info promoted to top-level details
    assert sig.details["error_info"]["http_status"] == 429


def test_observe_from_opik_dedup_on_repeat(tmp_path: Path):
    # First span -> creates Signal
    s1 = signals_store.observe_from_opik(
        tmp_path, _error_span(),
        type="provider_failing",
        flavor="maintenance",
        signature_suffix="openai/quota_exceeded",
    )
    # Same signature -> bumps observation_count, returns same id
    s2 = signals_store.observe_from_opik(
        tmp_path, _error_span(),
        type="provider_failing",
        flavor="maintenance",
        signature_suffix="openai/quota_exceeded",
    )
    assert s1.id == s2.id
    assert s2.observation_count == 2


def test_observe_from_opik_different_suffix_creates_distinct_signals(tmp_path: Path):
    s1 = signals_store.observe_from_opik(
        tmp_path, _error_span(provider="openai"),
        type="provider_failing",
        flavor="maintenance",
        signature_suffix="openai/quota_exceeded",
    )
    s2 = signals_store.observe_from_opik(
        tmp_path, _error_span(provider="gemini"),
        type="provider_failing",
        flavor="maintenance",
        signature_suffix="gemini/auth_failed",
    )
    assert s1.id != s2.id
    assert s1.signature != s2.signature


def test_observe_from_opik_uses_producer_override(tmp_path: Path):
    span = _error_span(producer="default_producer")
    sig = signals_store.observe_from_opik(
        tmp_path, span,
        type="provider_failing",
        flavor="maintenance",
        producer="explicit_override",
    )
    assert sig.producer == "explicit_override"
    assert sig.signature.startswith("explicit_override:provider_failing:")


def test_observe_from_opik_default_title_includes_bot(tmp_path: Path):
    span = _error_span(bot_id="team_bot_a")
    sig = signals_store.observe_from_opik(
        tmp_path, span,
        type="rate_limit_storm",
        flavor="maintenance",
    )
    assert "Rate limit storm" in sig.title
    assert "team_bot_a" in sig.title


def test_observe_from_opik_explicit_title_overrides(tmp_path: Path):
    sig = signals_store.observe_from_opik(
        tmp_path, _error_span(),
        type="provider_failing",
        flavor="maintenance",
        title="Custom title here",
    )
    assert sig.title == "Custom title here"
