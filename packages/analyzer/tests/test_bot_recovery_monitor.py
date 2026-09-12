"""tests/test_bot_recovery_monitor.py — bot_recovery_monitor tests."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import bot_recovery_monitor as brm  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _entry(
    *,
    provider: str | None = "anthropic",
    condition: str = "context_overflow",
    error_ts: str = "2026-05-17T01:30:00Z",
    recovered_ts: str = "2026-05-17T02:00:00Z",
    error_line: str = (
        "[gateway] anthropic 400 context_too_long bytes=190000"
    ),
) -> dict:
    return {
        "provider": provider,
        "condition": condition,
        "error_ts": error_ts,
        "recovered_ts": recovered_ts,
        "error_line": error_line,
    }


# ── build_signal_for_recovery ────────────────────────────────────────────────


def test_signal_built_with_humanized_title():
    spec = brm.build_signal_for_recovery("team_bot_a", _entry())
    assert spec is not None
    assert spec["type"] == "bot_recovered"
    assert spec["severity"] == "info"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "team_bot_a"
    assert spec["title"].lower().startswith("team_bot_a recovered from anthropic")


def test_signal_omits_empty_condition():
    assert brm.build_signal_for_recovery("team_bot_a", _entry(condition="")) is None


def test_signal_signature_stable_per_bot_provider_condition():
    spec_a = brm.build_signal_for_recovery("team_bot_a", _entry())
    spec_b = brm.build_signal_for_recovery("team_bot_a", _entry(recovered_ts="2026-05-17T03:00:00Z"))
    # Same (bot, provider, condition) → same signature; recovered_ts shouldn't
    # cause the bucket to fragment.
    assert spec_a["signature"] == spec_b["signature"]


def test_signal_signatures_distinct_across_bots():
    a = brm.build_signal_for_recovery("team_bot_a", _entry())
    b = brm.build_signal_for_recovery("team_bot_b", _entry())
    assert a["signature"] != b["signature"]


def test_signal_signatures_distinct_across_conditions():
    a = brm.build_signal_for_recovery("team_bot_a", _entry(condition="context_overflow"))
    b = brm.build_signal_for_recovery("team_bot_a", _entry(condition="rate_limited"))
    assert a["signature"] != b["signature"]


def test_signal_body_includes_error_excerpt():
    spec = brm.build_signal_for_recovery(
        "team_bot_a",
        _entry(error_line="[gateway] anthropic 400 context_too_long bytes=190000"),
    )
    assert "context_too_long" in spec["body"]


def test_signal_truncates_long_error_line():
    long_line = "A" * 1000
    spec = brm.build_signal_for_recovery("team_bot_a", _entry(error_line=long_line))
    assert "A" * 280 in spec["body"]
    assert "A" * 281 not in spec["body"]


def test_provider_missing_falls_back_to_bare_condition():
    spec = brm.build_signal_for_recovery(
        "team_bot_a", _entry(provider=None, condition="rate_limited")
    )
    assert "team_bot_a recovered from" in spec["title"].lower()
    assert "anthropic" not in spec["title"].lower()


# ── collect_for_bot ──────────────────────────────────────────────────────────


def test_collect_no_recoveries_returns_empty():
    out = brm.collect_for_bot(
        Path("/nope"),
        "team_bot_a",
        status_loader=lambda _sd, _bot: {"recovered_alerts": []},
    )
    assert out == []


def test_collect_missing_status_returns_empty():
    out = brm.collect_for_bot(
        Path("/nope"),
        "team_bot_a",
        status_loader=lambda _sd, _bot: None,
    )
    assert out == []


def test_collect_malformed_entries_are_skipped():
    out = brm.collect_for_bot(
        Path("/nope"),
        "team_bot_a",
        status_loader=lambda _sd, _bot: {
            "recovered_alerts": [
                _entry(),
                None,
                {"missing_condition": True},
                "not a dict",
                _entry(condition="rate_limited"),
            ],
        },
    )
    # Two well-formed entries; the rest are silently dropped.
    types = sorted(d["details"]["condition"] for d in out)
    assert types == ["context_overflow", "rate_limited"]


# ── End-to-end run() ─────────────────────────────────────────────────────────


def test_run_writes_signals_per_bot(tmp_path):
    status_map = {
        "team_bot_a": {"recovered_alerts": [_entry()]},
        "team_bot_b": {"recovered_alerts": [_entry(condition="rate_limited")]},
        "admin_bot": {"recovered_alerts": []},
    }
    kept, n_fired, n_resolved = brm.run(
        tmp_path,
        config={},
        bots=["team_bot_a", "team_bot_b", "admin_bot"],
        status_loader=lambda _sd, bot: status_map.get(bot),
    )
    assert n_fired == 2
    assert n_resolved == 0
    assert len(kept) == 2
    sigs = list(signals_store.iter_active(tmp_path, producer="bot_recovery_monitor"))
    types = sorted((s.bot_id, s.details.get("condition")) for s in sigs)
    assert types == [("team_bot_a", "context_overflow"), ("team_bot_b", "rate_limited")]


def test_run_sweep_resolve_archives_when_recovery_decays(tmp_path):
    # Run 1: team_bot_a has a recovery entry.
    kept1, _, _ = brm.run(
        tmp_path,
        config={},
        bots=["team_bot_a"],
        status_loader=lambda _sd, _bot: {"recovered_alerts": [_entry()]},
    )
    assert len(kept1) == 1

    # Run 2: heal's 24h sticky window expired; entry no longer present.
    kept2, n_fired2, n_resolved2 = brm.run(
        tmp_path,
        config={},
        bots=["team_bot_a"],
        status_loader=lambda _sd, _bot: {"recovered_alerts": []},
    )
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert (
        list(signals_store.iter_active(tmp_path, producer="bot_recovery_monitor"))
        == []
    )


def test_run_dry_run_does_not_write(tmp_path):
    kept, n_fired, _ = brm.run(
        tmp_path,
        config={},
        bots=["team_bot_a"],
        status_loader=lambda _sd, _bot: {"recovered_alerts": [_entry()]},
        dry_run=True,
    )
    assert n_fired == 1
    assert (
        list(signals_store.iter_active(tmp_path, producer="bot_recovery_monitor"))
        == []
    )


def test_severity_default_is_info():
    from signals.producer_severity import default_severity
    assert default_severity("bot_recovery_monitor") == "info"
