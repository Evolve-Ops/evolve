"""Tests for ``preflight_prior`` — the offline routing-prior learner.

Decision: ``internal/decision-evolve-overhead-2026-09-07.md`` D-OH2.

Five slices:

1. **Refusal is the default.** A bot with thin history, or with no rung
   whose share clears the confidence bar, gets NO prior and stays on its
   primary. That is the property that makes shipping this safe under the
   standing rule (*nothing changes which model answers*).
2. **The prior is written with its evidence** — turn count, confidence,
   window, per-surface splits, hour histogram, computed_at.
3. **Per-surface entries** appear only where a surface both has enough
   turns and disagrees with the bot-wide value.
4. **Only user turns count.** Heartbeats and cron are routed by the
   trigger rule and contribute nothing to a prior about people.
5. **Writing is opt-in and idempotent** — ``--apply`` plus
   ``cascade.prior.enabled``, and an unchanged prior is not rewritten.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import preflight_prior  # noqa: E402
from preflight_prior import (  # noqa: E402
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_TURNS,
    apply_prior,
    build_role_index,
    compute_prior,
    iter_user_turns,
    models_block_for_bot,
    prior_writes_enabled,
    role_for_model,
    run,
    turns_paths,
)

BOT = "team-bot-a"
HAIKU = "anthropic/claude-haiku-4-5"
SONNET = "anthropic/claude-sonnet-4-6"
OPUS = "anthropic/claude-opus-4-8"

MODELS = {
    "rungs": [
        {"id": "haiku-class", "models": [HAIKU], "costClass": "low"},
        {"id": "sonnet-class", "models": [SONNET], "costClass": "medium"},
        {"id": "opus-class", "models": [OPUS], "costClass": "high"},
    ],
    "roles": {"fast": "haiku-class", "standard": "sonnet-class", "power": "opus-class"},
}


def _index():
    return build_role_index(MODELS)


def _turn(model=SONNET, source="user", channel="slack", hour=9):
    ts = datetime(2026, 9, 1, hour, 0, tzinfo=timezone.utc).isoformat()
    return {"ts": ts, "model": model, "source": source, "channel": channel}


# ── Model → role ─────────────────────────────────────────────────────────────


def test_role_index_maps_every_configured_model():
    idx = _index()
    assert idx[HAIKU] == "fast"
    assert idx[SONNET] == "standard"
    assert idx[OPUS] == "power"


def test_role_for_model_matches_the_bare_id_a_provider_echoes_back():
    # A turns file records whatever string came back, which is not always
    # the fully-qualified id the catalog lists.
    assert role_for_model("claude-sonnet-4-6", _index()) == "standard"


def test_role_for_model_returns_none_for_an_unknown_model():
    assert role_for_model("someone-elses/model-v1", _index()) is None
    assert role_for_model(None, _index()) is None


def test_models_block_prefers_the_bot_override():
    network = {
        "models": MODELS,
        "bots": {BOT: {"models": {"roles": {"fast": "sonnet-class"}}}},
    }
    merged = models_block_for_bot(network, BOT)
    assert merged["roles"]["fast"] == "sonnet-class"
    assert merged["rungs"] == MODELS["rungs"], "the pod's rungs are inherited"


# ── Refusal ──────────────────────────────────────────────────────────────────


def test_refuses_below_the_turn_floor_and_says_why():
    rows = [_turn() for _ in range(DEFAULT_MIN_TURNS - 1)]
    out = compute_prior(rows, _index())
    assert "bot_prior" not in out
    assert "too few resolved user turns" in out["refused"]


def test_refuses_when_no_rung_clears_the_confidence_bar():
    # A 50/50 split between two rungs: there is no answer, so there is no
    # prior. The bot stays on its primary, which is what it does today.
    rows = [_turn(model=SONNET) for _ in range(60)] + [
        _turn(model=HAIKU) for _ in range(60)
    ]
    out = compute_prior(rows, _index())
    assert "bot_prior" not in out
    assert "confidence bar" in out["refused"]


def test_refuses_when_every_model_is_unrecognised():
    rows = [_turn(model="unknown/model") for _ in range(200)]
    out = compute_prior(rows, _index())
    assert "bot_prior" not in out
    assert out["unresolved"] == 200
    assert out["resolved"] == 0


def test_the_plugin_and_the_job_agree_on_the_confidence_bar():
    # Both ends refuse below the same number, so a mis-set threshold on
    # one side cannot quietly route turns the other side would refuse.
    ts_src = (
        _ANALYZER_DIR.parent / "plugin" / "src" / "observer" / "routingRule.ts"
    ).read_text()
    assert f"DEFAULT_PRIOR_MIN_CONFIDENCE = {DEFAULT_MIN_CONFIDENCE}" in ts_src


# ── The prior, with evidence ─────────────────────────────────────────────────


def test_writes_the_modal_rung_with_its_evidence():
    rows = [_turn(model=SONNET, hour=9) for _ in range(90)] + [
        _turn(model=HAIKU, hour=22) for _ in range(10)
    ]
    out = compute_prior(rows, _index(), computed_at="2026-09-07T03:20:00+00:00")
    assert out["bot_prior"] == "standard"
    ev = out["prior_evidence"]
    assert ev["turns"] == 100
    assert ev["confidence"] == 0.9
    assert ev["window_days"] == 14
    assert ev["computed_at"] == "2026-09-07T03:20:00+00:00"
    assert ev["hours"] == {"9": 90, "22": 10}


def test_hours_are_read_off_utc_never_local():
    # Turn files are UTC-named and UTC-stamped; a reader that localises
    # them silently shifts a bot's evening into someone else's morning.
    rows = [
        {"ts": "2026-09-01T23:30:00Z", "model": SONNET, "source": "user",
         "channel": "slack"}
        for _ in range(DEFAULT_MIN_TURNS)
    ]
    ev = compute_prior(rows, _index())["prior_evidence"]
    assert ev["hours"] == {"23": DEFAULT_MIN_TURNS}


def test_per_surface_entry_recorded_only_when_it_disagrees():
    rows = (
        [_turn(model=SONNET, channel="telegram") for _ in range(80)]
        + [_turn(model=HAIKU, channel="slack") for _ in range(30)]
    )
    out = compute_prior(rows, _index(), min_confidence=0.7)
    assert out["bot_prior"] == "standard"
    assert out["prior_evidence"]["surfaces"] == {"slack": "fast"}


def test_a_thin_surface_gets_no_entry_of_its_own():
    rows = [_turn(model=SONNET, channel="telegram") for _ in range(90)] + [
        _turn(model=HAIKU, channel="sms") for _ in range(5)
    ]
    out = compute_prior(rows, _index())
    assert out["prior_evidence"]["surfaces"] == {}


def test_a_surface_that_agrees_is_not_recorded():
    rows = [
        _turn(model=SONNET, channel=("slack" if i % 2 else "telegram"))
        for i in range(120)
    ]
    out = compute_prior(rows, _index())
    assert out["prior_evidence"]["surfaces"] == {}


# ── Only user turns ──────────────────────────────────────────────────────────


def test_only_user_turns_are_read(tmp_path):
    turns_dir = tmp_path / BOT / "turns"
    turns_dir.mkdir(parents=True)
    day = datetime.now(timezone.utc).date().isoformat()
    (turns_dir / f"turns-{day}.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _turn(source="user"),
                _turn(source="human"),
                _turn(source="heartbeat", model=HAIKU, channel="heartbeat"),
                _turn(source="cron", model=HAIKU, channel="cron-event"),
                _turn(source="subagent", model=HAIKU, channel="subagent"),
                _turn(source="summarizer", model=HAIKU, channel="subagent"),
            ]
        )
        + "\n"
    )
    rows = iter_user_turns(turns_paths(tmp_path, BOT, 1))
    assert len(rows) == 2, "heartbeat/cron/scaffolding are the trigger rule's job"


def test_a_torn_final_line_does_not_lose_the_fortnight(tmp_path):
    turns_dir = tmp_path / BOT / "turns"
    turns_dir.mkdir(parents=True)
    day = datetime.now(timezone.utc).date().isoformat()
    (turns_dir / f"turns-{day}.jsonl").write_text(
        json.dumps(_turn()) + "\n" + '{"ts": "2026-09-0'
    )
    assert len(iter_user_turns(turns_paths(tmp_path, BOT, 1))) == 1


def test_turns_paths_skips_days_with_no_file(tmp_path):
    turns_dir = tmp_path / BOT / "turns"
    turns_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    for offset in (0, 3):
        (turns_dir / f"turns-{(today - timedelta(days=offset)).isoformat()}.jsonl").write_text("")
    assert len(turns_paths(tmp_path, BOT, 14)) == 2


# ── Writing ──────────────────────────────────────────────────────────────────


def _pod(tmp_path, *, prior_enabled=False, days=14, per_day=10):
    """A shared dir with one bot, a network.json, and enough user turns."""
    shared = tmp_path / "shared"
    turns_dir = shared / BOT / "turns"
    turns_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    for offset in range(days):
        d = today - timedelta(days=offset)
        rows = [
            {
                "ts": datetime(d.year, d.month, d.day, 9, 0, tzinfo=timezone.utc).isoformat(),
                "model": SONNET, "source": "user", "channel": "slack",
            }
            for _ in range(per_day)
        ]
        (turns_dir / f"turns-{d.isoformat()}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
    network = {"models": MODELS, "bots": {BOT: {}}}
    if prior_enabled:
        network["cascade"] = {"prior": {"enabled": True}}
    (shared / "network.json").write_text(json.dumps(network, indent=2))
    return shared


def test_prior_writes_enabled_is_off_by_default():
    assert prior_writes_enabled({}) is False
    assert prior_writes_enabled({"cascade": {"prior": {}}}) is False
    assert prior_writes_enabled({"cascade": {"prior": {"enabled": True}}}) is True


def test_apply_writes_nothing_without_the_switch(tmp_path, capsys):
    shared = _pod(tmp_path, prior_enabled=False)
    assert run(["--shared-dir", str(shared), "--apply"]) == 0
    network = json.loads((shared / "network.json").read_text())
    assert "preflight" not in network["bots"][BOT]
    assert "fails closed" in capsys.readouterr().out


def test_apply_writes_the_prior_when_the_switch_is_on(tmp_path):
    shared = _pod(tmp_path, prior_enabled=True)
    assert run(["--shared-dir", str(shared), "--apply"]) == 0
    preflight = json.loads((shared / "network.json").read_text())["bots"][BOT]["preflight"]
    assert preflight["bot_prior"] == "standard"
    assert preflight["prior_evidence"]["turns"] == 140
    assert preflight["prior_evidence"]["confidence"] == 1.0


def test_a_second_run_over_the_same_turns_does_not_rewrite(tmp_path):
    shared = _pod(tmp_path, prior_enabled=True)
    network_path = shared / "network.json"
    run(["--shared-dir", str(shared), "--apply"])
    first = network_path.read_text()
    run(["--shared-dir", str(shared), "--apply"])
    assert network_path.read_text() == first, (
        "computed_at alone must not churn network.json every night"
    )


def test_apply_prior_preserves_other_preflight_keys(tmp_path):
    shared = _pod(tmp_path, prior_enabled=True)
    network_path = shared / "network.json"
    network = json.loads(network_path.read_text())
    network["bots"][BOT]["preflight"] = {"enabled": False}
    network_path.write_text(json.dumps(network, indent=2))

    apply_prior(network_path, BOT, "fast", {"turns": 10, "confidence": 0.9})
    preflight = json.loads(network_path.read_text())["bots"][BOT]["preflight"]
    assert preflight["enabled"] is False, "an operator's own key must survive"
    assert preflight["bot_prior"] == "fast"


def test_reports_one_line_per_bot_including_refusals(tmp_path, capsys):
    shared = _pod(tmp_path, prior_enabled=True, days=1, per_day=3)
    assert run(["--shared-dir", str(shared)]) == 0
    out = capsys.readouterr().out
    assert BOT in out
    assert "no prior" in out and "stays on its primary" in out


def test_json_mode_emits_a_parseable_report(tmp_path, capsys):
    shared = _pod(tmp_path, prior_enabled=True)
    assert run(["--shared-dir", str(shared), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report[0]["bot_id"] == BOT
    assert report[0]["written"] is False, "--json without --apply must not write"


def test_missing_network_json_exits_nonzero(tmp_path):
    assert run(["--shared-dir", str(tmp_path)]) == 2
