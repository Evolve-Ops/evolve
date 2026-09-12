"""tests/test_embedding_monitor.py — embedding_monitor parser + detectors.

Covers:
  - Log parser handles all observed gateway.err.log shapes (timestamped,
    untimestamped, multi-line JSON bodies, rate-limit retry lines)
  - classify_status maps HTTP codes to failure classes
  - detect_provider_failures fires per (provider, error_class) bucket and
    respects per-class thresholds + severity escalation
  - detect_rate_limit_storm fires only above its threshold
  - Signature/scope shape matches signal-store conventions
  - since= window filters out aged failures
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from embedding_monitor import (  # noqa: E402
    DEFAULTS,
    PRODUCER,
    classify_status,
    collect_for_bot,
    detect_provider_failures,
    detect_rate_limit_storm,
    parse_log,
)


# ── Fixtures (real log shapes captured from the mini) ────────────────────────

OPENAI_403_LINE = (
    "2026-03-30T23:56:25.736-07:00 [memory] sync failed (session-start): "
    "Error: openai embeddings failed: 403 {"
)
OPENAI_429_LINE = (
    "2026-03-31T08:00:52.105-07:00 [memory] sync failed (search): "
    "Error: openai embeddings failed: 429 {"
)
GEMINI_500_LINE = (
    "2026-04-01T10:00:00.000-07:00 [memory] sync failed (watch): "
    "Error: gemini embeddings failed: 500 {"
)
NO_TS_LINE = "[memory] sync failed (search): Error: openai embeddings failed: 429 {"
RATE_LIMIT_LINE = (
    "2026-04-30T00:10:01.585-07:00 [memory] embeddings rate limited; retrying in 574ms"
)
RATE_LIMIT_NO_TS = "[memory] embeddings rate limited; retrying in 580ms"
JSON_BODY_LINES = '    "error": {\n        "message": "stuff",\n    }\n}'

NOISE_LINE = "2026-04-01T12:00:00.000-07:00 [http] something else happened"


# ── classify_status ─────────────────────────────────────────────────────────


def test_classify_auth_codes():
    assert classify_status(401) == "auth_failed"
    assert classify_status(403) == "auth_failed"


def test_classify_quota():
    assert classify_status(429) == "quota_exceeded"


def test_classify_provider_error():
    assert classify_status(500) == "provider_error"
    assert classify_status(503) == "provider_error"


def test_classify_bad_request():
    assert classify_status(400) == "bad_request"
    assert classify_status(404) == "bad_request"


def test_classify_unknown():
    assert classify_status(200) == "unknown"
    assert classify_status(301) == "unknown"


# ── parse_log ───────────────────────────────────────────────────────────────


def test_parse_extracts_provider_and_status():
    parsed = parse_log(OPENAI_403_LINE)
    assert len(parsed["hard_fails"]) == 1
    fail = parsed["hard_fails"][0]
    assert fail["provider"] == "openai"
    assert fail["status"] == 403
    assert fail["error_class"] == "auth_failed"
    assert fail["reason"] == "session-start"


def test_parse_handles_multiple_failure_lines():
    text = "\n".join([OPENAI_403_LINE, OPENAI_429_LINE, GEMINI_500_LINE])
    parsed = parse_log(text)
    assert len(parsed["hard_fails"]) == 3
    providers = {f["provider"] for f in parsed["hard_fails"]}
    assert providers == {"openai", "gemini"}


def test_parse_skips_json_body_continuation_lines():
    """Lines that are part of a multi-line JSON body shouldn't double-count."""
    text = OPENAI_403_LINE + "\n" + JSON_BODY_LINES
    parsed = parse_log(text)
    assert len(parsed["hard_fails"]) == 1


def test_parse_drops_untimed_failure_with_no_anchor():
    """Untimed failure lines with no preceding timed line in the buffer are
    dropped — we can't tell whether they're 1 minute or 1 week old. This is
    the new behavior; the prior rule kept them, which caused false-positive
    `quota_exceeded` alerts from days-old multi-line OpenAI 429 blocks
    surviving in the log tail (see team_bot_a/admin_bot/team_bot_b, May 2026)."""
    parsed = parse_log(NO_TS_LINE)
    assert parsed["hard_fails"] == []


def test_parse_untimed_failure_inherits_preceding_anchor():
    """OpenClaw writes the `[memory] sync failed (...)` header without a
    timestamp prefix. Each untimed match must inherit the timestamp of the
    most recent preceding timed line so the failure can be dated and the
    multi-line block ages out as a unit."""
    # `NOISE_LINE` is timed (2026-04-01T12:00 UTC-7) — used here purely
    # as an anchor source, not as a failure itself.
    text = "\n".join([NOISE_LINE, NO_TS_LINE])
    parsed = parse_log(text)
    assert len(parsed["hard_fails"]) == 1
    fail = parsed["hard_fails"][0]
    assert fail["provider"] == "openai"
    assert fail["status"] == 429
    # Inherited ts must equal the NOISE_LINE's parsed timestamp.
    expected = datetime(2026, 4, 1, 12, 0, tzinfo=timezone(timedelta(hours=-7))).astimezone(timezone.utc)
    assert fail["ts"] == expected


def test_parse_counts_rate_limit_retries():
    text = "\n".join([RATE_LIMIT_LINE, RATE_LIMIT_NO_TS, NOISE_LINE])
    parsed = parse_log(text)
    assert parsed["rate_limit_count"] == 2
    assert parsed["hard_fails"] == []


def test_parse_ignores_unrelated_log_lines():
    parsed = parse_log(NOISE_LINE)
    assert parsed["hard_fails"] == []
    assert parsed["rate_limit_count"] == 0


def test_parse_since_filter_drops_old_failures():
    """A timestamped failure before ``since`` must be excluded.

    OPENAI_403_LINE is 2026-03-30T23:56-07:00 → 2026-03-31T06:56 UTC.
    OPENAI_429_LINE is 2026-03-31T08:00-07:00 → 2026-03-31T15:00 UTC.
    A cutoff between them (noon UTC) keeps only the 429 line.
    """
    text = "\n".join([OPENAI_403_LINE, OPENAI_429_LINE])
    cutoff = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    parsed = parse_log(text, since=cutoff)
    assert len(parsed["hard_fails"]) == 1
    assert parsed["hard_fails"][0]["status"] == 429


def test_parse_drops_stale_block_via_inherited_anchor():
    """REGRESSION: untimed failure lines must be aged against their inherited
    anchor, not blindly kept. This is the team_bot_a-May-2 case: the OpenAI 429
    block header has no timestamp, but a preceding `[diagnostic] liveness
    warning` line establishes the block's age. With `since` set to one hour
    ago, the stale block must drop.

    The prior behavior — "untimed lines pass the `since` filter
    unconditionally" — caused the embedding_monitor to fire `quota_exceeded`
    alerts for team_bot_a/admin_bot/team_bot_b claiming "17 failures within the window" when
    in fact all 17 were from a May-2 untimed block that the log tail still
    happened to contain."""
    stale_anchor = "2026-05-02T14:09:16.720-07:00 [diagnostic] liveness warning"
    text = "\n".join([stale_anchor, NO_TS_LINE])
    cutoff = datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc)  # 10 days later
    parsed = parse_log(text, since=cutoff)
    assert parsed["hard_fails"] == []


def test_parse_keeps_fresh_block_via_inherited_anchor():
    """Mirror of the regression above: when the inherited anchor IS within
    the window, the untimed failure line is kept."""
    fresh_anchor_dt = datetime.now(timezone.utc) - timedelta(minutes=5)
    # Format to match what _parse_ts will read back.
    fresh_anchor = (
        fresh_anchor_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "+00:00 [diagnostic] something happened"
    )
    text = "\n".join([fresh_anchor, NO_TS_LINE])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    parsed = parse_log(text, since=cutoff)
    assert len(parsed["hard_fails"]) == 1
    assert parsed["hard_fails"][0]["provider"] == "openai"


def test_parse_rate_limit_inherits_anchor():
    """The same rule applies to rate-limit retry lines: untimed ones inherit
    the most recent preceding anchor, and are dropped if no anchor exists."""
    # No anchor → dropped.
    assert parse_log(RATE_LIMIT_NO_TS)["rate_limit_count"] == 0
    # Anchor present → inherited and counted.
    text = "\n".join([NOISE_LINE, RATE_LIMIT_NO_TS])
    assert parse_log(text)["rate_limit_count"] == 1


# ── detect_provider_failures ────────────────────────────────────────────────


def _thresholds(**overrides) -> dict:
    base = dict(DEFAULTS)
    base.update(overrides)
    return base


def _make_fails(provider: str, status: int, count: int) -> list[dict]:
    klass = classify_status(status)
    return [
        {"ts": None, "provider": provider, "status": status,
         "error_class": klass, "reason": "search"}
        for _ in range(count)
    ]


def test_detect_auth_fires_on_first_occurrence():
    """auth_failed threshold defaults to 1 — and severity is alert always."""
    parsed = {"hard_fails": _make_fails("openai", 403, 1), "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "alert"
    assert sigs[0]["details"]["error_class"] == "auth_failed"


def test_detect_quota_requires_threshold():
    """quota_exceeded threshold defaults to 5 — 4 should NOT fire."""
    parsed = {"hard_fails": _make_fails("openai", 429, 4), "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    assert sigs == []


def test_detect_quota_warn_at_threshold_alert_at_double():
    parsed = {"hard_fails": _make_fails("openai", 429, 5), "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "warn"

    parsed = {"hard_fails": _make_fails("openai", 429, 10), "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    assert sigs[0]["severity"] == "alert"


def test_detect_buckets_per_provider_and_class():
    """Two providers + same class → two distinct signals."""
    fails = _make_fails("openai", 403, 1) + _make_fails("gemini", 403, 1)
    parsed = {"hard_fails": fails, "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    assert len(sigs) == 2
    providers = {s["details"]["provider"] for s in sigs}
    assert providers == {"openai", "gemini"}


def test_detect_signal_shape_matches_observe_kwargs():
    """Output dicts must be valid kwargs for signals_store.observe()."""
    parsed = {"hard_fails": _make_fails("openai", 403, 1), "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    sig = sigs[0]
    # Required observe() kwargs.
    for key in ("signature", "producer", "type", "flavor", "severity",
                "scope", "bot_id", "title", "body", "details"):
        assert key in sig, f"missing {key}"
    assert sig["producer"] == PRODUCER
    assert sig["scope"] == "bot"
    assert sig["bot_id"] == "team_bot_a"
    assert sig["flavor"] == "maintenance"
    # Signature must include bot/provider/error_class for de-dup uniqueness.
    assert "team_bot_a" in sig["signature"]
    assert "openai" in sig["signature"]


def test_detect_includes_config_hint_for_ui_jump():
    parsed = {"hard_fails": _make_fails("openai", 403, 1), "rate_limit_count": 0}
    sigs = detect_provider_failures("team_bot_a", parsed, thresholds=_thresholds())
    hint = sigs[0]["config_hint"]
    assert hint["page"] == "ai-optimization"
    assert hint["section"] == "embedding-providers"
    assert hint["bot_id"] == "team_bot_a"
    assert hint["provider"] == "openai"


# ── detect_rate_limit_storm ─────────────────────────────────────────────────


def test_rate_limit_storm_below_threshold():
    parsed = {"hard_fails": [], "rate_limit_count": 49}
    sigs = detect_rate_limit_storm("team_bot_a", parsed, thresholds=_thresholds())
    assert sigs == []


def test_rate_limit_storm_at_threshold():
    parsed = {"hard_fails": [], "rate_limit_count": 50}
    sigs = detect_rate_limit_storm("team_bot_a", parsed, thresholds=_thresholds())
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "warn"
    assert sigs[0]["type"] == "rate_limit_storm"


# ── End-to-end (synthetic file) ─────────────────────────────────────────────


def test_collect_for_bot_with_synthetic_log(monkeypatch, tmp_path):
    """Stub _bot_home + write a fake gateway.err.log; verify end-to-end wiring."""
    fake_bot_home = tmp_path / "fake_bot"
    log_dir = fake_bot_home / ".openclaw" / "logs"
    log_dir.mkdir(parents=True)
    log_text = "\n".join([
        OPENAI_403_LINE,
        OPENAI_429_LINE,
        OPENAI_429_LINE.replace("08:00:52", "08:01:00"),
        OPENAI_429_LINE.replace("08:00:52", "08:02:00"),
        OPENAI_429_LINE.replace("08:00:52", "08:03:00"),
        OPENAI_429_LINE.replace("08:00:52", "08:04:00"),
        RATE_LIMIT_LINE,
    ])
    (log_dir / "gateway.err.log").write_text(log_text)

    import embedding_monitor as em
    monkeypatch.setattr(em, "_bot_home", lambda bot, cfg=None: fake_bot_home)

    # Use a "now" far enough past the synthetic timestamps that they're outside
    # window, then a "now" right after them so they fall inside.
    inside_now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    detections = collect_for_bot("team_bot_a", {}, now=inside_now)
    # Window=1h: 2026-03-31 08:00–09:00 includes the five 429s (auth 403 from
    # 03-30 falls outside window). 5 quota fails == threshold → one signal.
    types = sorted(d["type"] for d in detections)
    assert "provider_failing" in types

    # Outside window: nothing fires.
    far_future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert collect_for_bot("team_bot_a", {}, now=far_future) == []
