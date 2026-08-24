"""tests/test_model_swap_watch.py — post-model-swap behavior divergence.

Design: internal/design-model-swap-behavior-guard-2026-08-19.md.

The monitor's whole value is that it is a DIFFERENTIAL: it fires when the
terse-reply rate collapses on the rung a swap moved *while the same bot's
other rungs hold steady*. Every gate below exists to keep it from firing on
something that is not a model regression, so each gate gets its own test —
plus an end-to-end ``run()`` over a synthetic shared dir shaped like the
2026-08-14 incident, so the Signal-emitting path is executed, not assumed.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_swap_ledger as ledger  # noqa: E402
import model_swap_watch as watch  # noqa: E402

SWAP_AT = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
NOW = SWAP_AT + timedelta(days=6)
OLD, NEW = "anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-5"
CONTROL = "anthropic/claude-haiku-4-5"


def _write_turns(shared_dir: Path, bot_id: str, turns) -> None:
    """Write ``(ts, model, output_tokens)`` triples into day-partitioned files."""
    base = Path(shared_dir) / "annotations" / bot_id
    base.mkdir(parents=True, exist_ok=True)
    for ts, model, out_tokens in turns:
        rec = {
            "type": "turn_annotation", "bot_id": bot_id,
            "ts": ts.isoformat(), "model_selected": model,
            "output_tokens": out_tokens,
        }
        with open(base / f"{ts.date().isoformat()}.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")


def _arm(model, count, terse_count, *, era, day_span=5):
    """``count`` turns on ``model``, ``terse_count`` of them terse."""
    sign = -1 if era == "pre" else 1
    out = []
    for i in range(count):
        ts = SWAP_AT + sign * timedelta(hours=1 + (i % (day_span * 24)))
        out.append((ts, model, 5 if i < terse_count else 300))
    return out


# ── measure_arms: binning ────────────────────────────────────────────────────


def test_turns_split_into_target_and_control_arms_either_side(tmp_path):
    _write_turns(tmp_path, "team-bot-a",
                 _arm(OLD, 10, 8, era="pre") + _arm(NEW, 10, 2, era="post")
                 + _arm(CONTROL, 6, 3, era="pre") + _arm(CONTROL, 6, 3, era="post"))
    arms = watch.measure_arms(tmp_path, "team-bot-a", SWAP_AT, [OLD, NEW], now=NOW)
    assert arms["pre"]["target"] == (8, 10)
    assert arms["post"]["target"] == (2, 10)
    assert arms["pre"]["control"] == (3, 6)
    assert arms["post"]["control"] == (3, 6)


def test_provider_qualified_and_bare_model_names_bin_together(tmp_path):
    """Live annotations spell the same model both ways; mis-binning would
    silently move target turns into the control arm."""
    _write_turns(tmp_path, "team-bot-a", _arm("claude-sonnet-5", 4, 4, era="post"))
    arms = watch.measure_arms(tmp_path, "team-bot-a", SWAP_AT, [NEW], now=NOW)
    assert arms["post"]["target"] == (4, 4)
    assert arms["post"]["control"] == (0, 0)


def test_turns_outside_the_window_are_excluded(tmp_path):
    far = SWAP_AT - timedelta(days=watch.WINDOW_DAYS + 3)
    _write_turns(tmp_path, "team-bot-a", [(far, OLD, 5)] + _arm(OLD, 3, 3, era="pre"))
    arms = watch.measure_arms(tmp_path, "team-bot-a", SWAP_AT, [OLD, NEW], now=NOW)
    assert arms["pre"]["target"] == (3, 3)


def test_terse_threshold_is_inclusive(tmp_path):
    _write_turns(tmp_path, "team-bot-a", [
        (SWAP_AT + timedelta(hours=1), NEW, watch.TERSE_MAX_OUTPUT_TOKENS),
        (SWAP_AT + timedelta(hours=2), NEW, watch.TERSE_MAX_OUTPUT_TOKENS + 1),
    ])
    assert watch.measure_arms(tmp_path, "team-bot-a", SWAP_AT, [NEW], now=NOW)["post"]["target"] == (1, 2)


# ── evaluate: the gates ──────────────────────────────────────────────────────


def _arms(pre_t, post_t, pre_c=(30, 200), post_c=(30, 200)):
    return {"pre": {"target": pre_t, "control": pre_c},
            "post": {"target": post_t, "control": post_c}}


def test_fires_on_the_incident_shape():
    v = watch.evaluate(_arms((80, 100), (25, 100)))
    assert v["diverged"] is True
    assert "target" in v["reason"] and "control" in v["reason"]


def test_below_sample_floor_does_not_fire():
    n = watch.MIN_TARGET_TURNS - 1
    v = watch.evaluate(_arms((n, n), (0, n)))
    assert v["diverged"] is False
    assert "sample floor" in v["reason"]


def test_no_prior_silence_behavior_does_not_fire():
    """A rung that was never staying quiet has no silence to lose."""
    v = watch.evaluate(_arms((5, 100), (0, 100)))
    assert v["diverged"] is False
    assert "no silence behavior to lose" in v["reason"]


def test_rate_that_held_does_not_fire():
    v = watch.evaluate(_arms((80, 100), (75, 100)))
    assert v["diverged"] is False
    assert "held" in v["reason"]


def test_control_arm_moving_too_does_not_fire():
    """Both arms moving is a pod-wide change, not a model regression."""
    v = watch.evaluate(_arms((80, 100), (20, 100), pre_c=(80, 100), post_c=(20, 100)))
    assert v["diverged"] is False
    assert "pod-wide" in v["reason"]


def test_missing_control_arm_does_not_fire():
    """A differential check without its differential must not fire."""
    v = watch.evaluate(_arms((80, 100), (20, 100), pre_c=(0, 0), post_c=(0, 0)))
    assert v["diverged"] is False
    assert "no control arm" in v["reason"]


def test_verdict_always_carries_the_measured_rates():
    v = watch.evaluate(_arms((80, 100), (25, 100)))
    assert v["pre_target_rate"] == 0.8 and v["post_target_rate"] == 0.25
    assert v["pre_target_n"] == 100 and v["post_target_n"] == 100


# ── run(): end to end over a synthetic pod ───────────────────────────────────


def _incident_pod(tmp_path, swap_at=SWAP_AT):
    """A pod shaped like 2026-08-14: team-bot-a diverges, team-bot-b holds."""
    ledger.record_swap("team-bot-a", "standard", "anthropic", [OLD], [NEW],
                       source="admin_ui_bulk", shared_dir=tmp_path)
    ledger.record_swap("team-bot-b", "standard", "anthropic", [OLD], [NEW],
                       source="admin_ui_bulk", shared_dir=tmp_path)
    # Backdate both ledger rows so they are past the settle window.
    path = ledger.swap_ledger_path(tmp_path)
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    for r in rows:
        r["ts"] = swap_at.isoformat()
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    n = watch.MIN_TARGET_TURNS + 20
    _write_turns(tmp_path, "team-bot-a",
                 _arm(OLD, n, int(n * 0.8), era="pre") + _arm(NEW, n, int(n * 0.2), era="post")
                 + _arm(CONTROL, n, int(n * 0.3), era="pre")
                 + _arm(CONTROL, n, int(n * 0.3), era="post"))
    _write_turns(tmp_path, "team-bot-b",
                 _arm(OLD, n, int(n * 0.8), era="pre") + _arm(NEW, n, int(n * 0.78), era="post")
                 + _arm(CONTROL, n, int(n * 0.3), era="pre")
                 + _arm(CONTROL, n, int(n * 0.3), era="post"))
    return tmp_path


def test_run_fires_for_the_diverged_bot_only(tmp_path):
    summary = watch.run(_incident_pod(tmp_path), now=NOW)
    assert summary["swaps_evaluated"] == 2
    assert summary["diverged"] == 1
    assert summary["signals_fired"] == 1
    fired = [e for e in summary["evaluations"] if e["diverged"]]
    assert [e["bot_id"] for e in fired] == ["team-bot-a"]


def test_run_writes_a_signal_the_operator_can_act_on(tmp_path):
    """The Signal must actually land in the store, naming the swap and the undo."""
    shared = _incident_pod(tmp_path)
    watch.run(shared, now=NOW)
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["type"] == "model_swap_behavior_divergence"
    assert sig["producer"] == "model_swap_watch"
    assert sig["bot_id"] == "team-bot-a"
    assert "claude-sonnet-4-6" in sig["body"] and "claude-sonnet-5" in sig["body"]
    assert sig["details"]["rollback_command"] == (
        "sudo evolve-admin models rollback team-bot-a --tier standard"
    )


def test_recovery_auto_resolves_the_signal(tmp_path):
    shared = _incident_pod(tmp_path)
    watch.run(shared, now=NOW)
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 1

    # team-bot-a's post-swap terse rate recovers — same swap, new annotations.
    n = watch.MIN_TARGET_TURNS + 20
    for f in (shared / "annotations" / "team-bot-a").glob("*.jsonl"):
        f.unlink()
    _write_turns(shared, "team-bot-a",
                 _arm(OLD, n, int(n * 0.8), era="pre") + _arm(NEW, n, int(n * 0.78), era="post")
                 + _arm(CONTROL, n, int(n * 0.3), era="pre")
                 + _arm(CONTROL, n, int(n * 0.3), era="post"))
    summary = watch.run(shared, now=NOW)
    assert summary["diverged"] == 0
    assert summary["signals_resolved"] == 1
    assert list((shared / "signals" / "firing").glob("*.json")) == []


def test_a_fresh_swap_is_not_judged_yet(tmp_path):
    fresh = NOW - timedelta(hours=6)
    summary = watch.run(_incident_pod(tmp_path, swap_at=fresh), now=NOW)
    assert summary["swaps_evaluated"] == 0
    assert summary["swaps_skipped"] == 2
    assert all("too fresh" in s["reason"] for s in summary["skipped"])


def test_an_aged_out_swap_is_not_swept(tmp_path):
    """Sweeping an unmeasured swap would auto-resolve a live regression the
    moment it aged past the lookback."""
    shared = _incident_pod(tmp_path)
    watch.run(shared, now=NOW)
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 1

    stale_now = SWAP_AT + timedelta(days=watch.LOOKBACK_DAYS + 5)
    summary = watch.run(shared, now=stale_now)
    assert summary["swaps_evaluated"] == 0
    assert summary["signals_resolved"] == 0
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 1


def test_empty_ledger_is_a_clean_noop(tmp_path):
    """The normal state of a pod that has never swapped a model."""
    summary = watch.run(tmp_path, now=NOW)
    assert summary == {**summary, "swaps_evaluated": 0, "diverged": 0,
                       "signals_fired": 0, "signals_resolved": 0}
