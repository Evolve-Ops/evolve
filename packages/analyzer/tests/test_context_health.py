"""Tests for context_health — the Phase 0 prefix-stability join.

Pins:
  1. The ordinal per-session pairing and the four-cell contingency.
  2. First-of-session turns are excluded (expected-cold, never a "miss").
  3. Per-block churn attribution names the block that changed.
  4. Presence flapping (block set → empty → back) counts as churn.
  5. Verdict thresholds: consistent / not-consistent / insufficient / no-data.
  6. Failure posture: unreadable files are reported UNREADABLE and excluded —
     never collapsed to "clean" (spec §Failure posture).
"""

from __future__ import annotations

import json
from pathlib import Path

from context_health import (
    DEFAULT_PREFIX_FLOOR_TOKENS,
    UNREADABLE,
    analyze,
    collect,
    load_jsonl,
    render,
    verdict,
)


def _hash_rec(session: str, ts: str, sha: "str | None", blocks: "dict | None" = None) -> dict:
    return {
        "type": "prefix_hash",
        "ts": ts,
        "session_id": session,
        "prefix_sha256": sha,
        "appended_block_shas": blocks
        or {"capabilities": sha, "digest": None, "narrative": None, "speaker": None},
    }


def _turn_rec(session: str, ts: str, cache_read: int, cache_write: int) -> dict:
    return {
        "ts": ts,
        "session_id": session,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def test_contingency_pairs_ordinally_and_skips_first_turn() -> None:
    turns = [
        _turn_rec("s1", "2026-07-31T00:00:00Z", 0, 20000),      # first: excluded
        _turn_rec("s1", "2026-07-31T00:05:00Z", 0, 20000),      # cold + changed
        _turn_rec("s1", "2026-07-31T00:10:00Z", 50000, 200),    # warm + stable
    ]
    hashes = [
        _hash_rec("s1", "2026-07-31T00:00:00Z", "aaa"),
        _hash_rec("s1", "2026-07-31T00:05:00Z", "bbb"),
        _hash_rec("s1", "2026-07-31T00:10:00Z", "bbb"),
    ]
    report = analyze(turns, hashes)
    assert report.cold_misses == 1
    assert report.warm_turns == 1
    assert report.cold_and_changed == 1
    assert report.cold_and_stable == 0
    assert report.warm_and_stable == 1
    assert report.warm_and_changed == 0
    assert report.paired_turns == 2


def test_low_cache_write_is_not_a_cold_miss() -> None:
    turns = [
        _turn_rec("s1", "2026-07-31T00:00:00Z", 0, 20000),
        # cache_read == 0 but write below the floor: heartbeat-ish, warm bucket.
        _turn_rec("s1", "2026-07-31T00:05:00Z", 0, DEFAULT_PREFIX_FLOOR_TOKENS),
    ]
    report = analyze(turns, [])
    assert report.cold_misses == 0
    assert report.warm_turns == 1


def test_per_block_attribution_names_the_churning_block() -> None:
    stable = {"capabilities": "c1", "digest": "d1", "narrative": None, "speaker": "s1"}
    churned = {"capabilities": "c1", "digest": "d2", "narrative": None, "speaker": "s1"}
    hashes = [
        _hash_rec("s1", "2026-07-31T00:00:00Z", "aaa", stable),
        _hash_rec("s1", "2026-07-31T00:03:00Z", "bbb", churned),
        _hash_rec("s1", "2026-07-31T00:06:00Z", "bbb", churned),
    ]
    report = analyze([], hashes)
    assert report.hash_transitions == 2
    assert report.hash_changes == 1
    assert report.block_change_counts["digest"] == 1
    assert report.block_change_counts["capabilities"] == 0
    assert report.presence_flaps == 0


def test_presence_flap_counts_as_churn() -> None:
    hashes = [
        _hash_rec("s1", "2026-07-31T00:00:00Z", "aaa"),
        _hash_rec("s1", "2026-07-31T00:03:00Z", None,
                  {"capabilities": None, "digest": None, "narrative": None, "speaker": None}),
        _hash_rec("s1", "2026-07-31T00:06:00Z", "aaa"),
    ]
    report = analyze([], hashes)
    assert report.hash_changes == 2
    assert report.presence_flaps == 2


def test_sessions_do_not_cross_contaminate() -> None:
    hashes = [
        _hash_rec("s1", "2026-07-31T00:00:00Z", "aaa"),
        _hash_rec("s2", "2026-07-31T00:01:00Z", "zzz"),
        _hash_rec("s1", "2026-07-31T00:05:00Z", "aaa"),
    ]
    report = analyze([], hashes)
    # s2's different sha must not register as a change inside s1's stream.
    assert report.hash_changes == 0
    assert report.sessions == 2


def test_verdict_consistent_and_refuted_and_insufficient() -> None:
    def rep(cold_changed: int, cold_stable: int):
        turns, hashes = [], []
        shas = []
        for i in range(cold_changed):
            shas.append((f"c{i}", "changed"))
        for i in range(cold_stable):
            shas.append((f"s{i}", "stable"))
        for idx, (sid, kind) in enumerate(shas):
            base = f"2026-07-31T0{idx % 2}:0{idx % 6}:00Z"
            turns += [
                _turn_rec(sid, base, 0, 5000),
                _turn_rec(sid, base, 0, 5000),
            ]
            first_sha = "aaa"
            second_sha = "bbb" if kind == "changed" else "aaa"
            hashes += [
                _hash_rec(sid, base, first_sha),
                _hash_rec(sid, base, second_sha),
            ]
        return analyze(turns, hashes)

    assert "CONSISTENT" in verdict(rep(5, 0))
    assert "NOT consistent" in verdict(rep(0, 5))
    assert "insufficient_data" in verdict(rep(2, 1))
    assert "no_data" in verdict(analyze([], []))


def test_unreadable_file_is_sentinel_not_empty(tmp_path: Path) -> None:
    bad = tmp_path / "turns-2026-07-31.jsonl"
    bad.write_text('{"broken json\n', encoding="utf-8")
    assert load_jsonl(bad) is UNREADABLE
    missing = tmp_path / "does-not-exist.jsonl"
    assert load_jsonl(missing) == []


def test_collect_reports_unreadable_and_still_analyzes_readable(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    turns_dir = tmp_path / "personal-bot" / "turns"
    turns_dir.mkdir(parents=True)
    good = [
        _turn_rec("s1", f"{today}T00:00:00Z", 0, 20000),
        _turn_rec("s1", f"{today}T00:05:00Z", 40000, 100),
    ]
    (turns_dir / f"turns-{today}.jsonl").write_text(
        "\n".join(json.dumps(rec) for rec in good) + "\n", encoding="utf-8"
    )
    (turns_dir / f"prefix-hashes-{today}.jsonl").write_text(
        '{"broken\n', encoding="utf-8"
    )
    report = collect(tmp_path, "personal-bot", days=1,
                     prefix_floor=DEFAULT_PREFIX_FLOOR_TOKENS)
    assert len(report.unreadable_files) == 1
    assert "prefix-hashes" in report.unreadable_files[0]
    assert report.total_turns == 2
    rendered = render(report, "personal-bot", 1)
    assert "UNREADABLE" in rendered
    assert "no_data" in rendered  # hash stream excluded, not treated as clean


# ── Phase A overhead decomposition (spec-evolve-overhead-budget) ────────────

def test_bootstrap_weights_tags_evolve_files(tmp_path: Path) -> None:
    from context_health import bootstrap_weights

    (tmp_path / "POD_CONDUCT.md").write_text("x" * 4000, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("y" * 800, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    rows, err = bootstrap_weights(tmp_path)
    assert err is None
    by_name = {name: (size, tokens, evolve, ingested)
               for name, size, tokens, evolve, ingested in rows}
    # POD_CONDUCT is Evolve-authored but NOT in OC's fixed bootstrap set —
    # on disk only, costs nothing per turn (post-mortem §5.5 correction).
    assert by_name["POD_CONDUCT.md"] == (4000, 1000, True, False)
    assert by_name["AGENTS.md"] == (800, 200, False, True)
    assert "notes.txt" not in by_name


def test_bootstrap_weights_unreadable_is_reported_not_zero(tmp_path: Path) -> None:
    from context_health import bootstrap_weights

    rows, err = bootstrap_weights(tmp_path / "no-such-workspace")
    assert rows == []
    assert err is not None


def test_injected_block_weight_and_prefix_estimate() -> None:
    from context_health import injected_block_weight, prefix_rewrite_estimate

    hashes = [{"combined_chars": c} for c in (100, 200, 300, 400)]
    n, mean, p95 = injected_block_weight(hashes)
    assert n == 4
    assert mean == 250.0
    assert p95 == 400

    turns = [
        {"cache_write_tokens": 21000},
        {"cache_write_tokens": 24000},
        {"cache_write_tokens": 150},     # below floor: heartbeat-ish, excluded
        {"cache_write_tokens": 0},
    ]
    assert prefix_rewrite_estimate(turns) == 21000
    assert prefix_rewrite_estimate([{"cache_write_tokens": 0}]) is None


def test_call_rollup_separates_evolve_from_bot() -> None:
    from context_health import EVOLVE_TRIGGER_KINDS, call_rollup

    records = [
        {"type": "cost_event", "trigger_kind": "user_turn", "cost_usd": 0.30},
        {"type": "cost_event", "trigger_kind": "classifier", "cost_usd": 0.01},
        {"type": "cost_event", "trigger_kind": "summarizer", "cost_usd": 0.02},
        {"type": "cost_event", "trigger_kind": "classifier", "cost_usd": "bad"},
        {"type": "session_annotation"},   # non-cost record: ignored
    ]
    rollup, seen = call_rollup(records)
    assert seen == 4
    assert rollup["classifier"]["calls"] == 2
    assert rollup["classifier"]["cost_usd"] == 0.01   # bad value skipped, call counted
    assert rollup["user_turn"]["cost_usd"] == 0.30
    assert "classifier" in EVOLVE_TRIGGER_KINDS and "user_turn" not in EVOLVE_TRIGGER_KINDS


def test_collect_overhead_and_render(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from context_health import collect_overhead, render_overhead

    today = datetime.now(timezone.utc).date().isoformat()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "POD_CONDUCT.md").write_text("x" * 2000, encoding="utf-8")
    ann = tmp_path / "annotations" / "team-bot-a"
    ann.mkdir(parents=True)
    (ann / f"{today}.jsonl").write_text(
        json.dumps({"type": "cost_event", "trigger_kind": "summarizer",
                    "cost_usd": 0.05}) + "\n",
        encoding="utf-8",
    )
    turns = [{"cache_write_tokens": 20000}]
    hashes = [{"combined_chars": 1200}]
    report = collect_overhead(tmp_path, "team-bot-a", 1, turns, hashes, workspace_dir=ws)
    assert report.evolve_ondisk_tokens == 500      # POD_CONDUCT: on disk, free
    assert report.ingested_bootstrap_tokens == 0   # nothing from OC's set present
    assert report.prefix_rewrite_estimate_tokens == 20000
    assert report.block_mean_chars == 1200.0
    assert abs(report.evolve_call_cost_usd - 0.05) < 1e-9
    out = render_overhead(report)
    assert "POD_CONDUCT.md" in out and "[Evolve, on-disk only]" in out
    assert "summarizer" in out
    # Footprint absent in this fixture: reported as unmeasured, never as zero.
    assert "unmeasured" in out and "context-footprint.json" in out


def test_collect_overhead_unknown_workspace_is_loud() -> None:
    from context_health import collect_overhead, render_overhead
    from pathlib import Path as _P

    report = collect_overhead(_P("/nonexistent-shared"), "team-bot-a", 1, [], [],
                              workspace_dir=None)
    assert report.workspace_unreadable is not None
    assert "UNREADABLE" in render_overhead(report)


def test_collect_overhead_prefers_converted_cost_events_file(tmp_path: Path) -> None:
    """cost_events-<date>.jsonl (converter output) wins over <date>.jsonl
    (plugin mixed annotations) for the same day — reading both would
    double-count, since the converter mirrors the plugin rows."""
    from datetime import datetime, timezone

    from context_health import collect_overhead

    today = datetime.now(timezone.utc).date().isoformat()
    ann = tmp_path / "annotations" / "team-bot-a"
    ann.mkdir(parents=True)
    (ann / f"cost_events-{today}.jsonl").write_text(
        json.dumps({"type": "cost_event", "trigger_kind": "classifier",
                    "cost_usd": 0.02}) + "\n",
        encoding="utf-8",
    )
    (ann / f"{today}.jsonl").write_text(
        json.dumps({"type": "cost_event", "trigger_kind": "classifier",
                    "cost_usd": 0.02}) + "\n",
        encoding="utf-8",
    )
    report = collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=None)
    assert report.call_rollup["classifier"]["calls"] == 1   # not 2


def test_split_injection_sections() -> None:
    from context_health import split_injection_sections

    text = (
        "[POD CONDUCT — rules]\nbody one\n\n"
        "[APPS REFERENCE — read on demand]\nbody two longer\n"
    )
    sections = dict(split_injection_sections(text))
    assert "[POD CONDUCT — rules]" in sections
    assert "[APPS REFERENCE — read on demand]" in sections
    assert sections["[APPS REFERENCE — read on demand]"] > 0


def test_render_overhead_reports_session_injection() -> None:
    from context_health import OverheadReport, render_overhead

    report = OverheadReport()
    report.session_injection = (2000, [("[POD CONDUCT — rules]", 1200),
                                       ("[APPS REFERENCE — read on demand]", 800)])
    out = render_overhead(report)
    assert "session-start injection" in out
    assert "[POD CONDUCT — rules]" in out
    report.session_injection = None
    assert "unmeasured" in render_overhead(report)


def test_overhead_reads_tool_footprint(tmp_path: Path) -> None:
    from context_health import collect_overhead, render_overhead

    bot_dir = tmp_path / "team-bot-a" / "turns"
    bot_dir.mkdir(parents=True)
    (bot_dir / "context-footprint.json").write_text(json.dumps({
        "schema_version": 1, "tier": "manage", "tool_count": 2,
        "total_chars": 3000,
        "tools": [{"name": "record_application", "chars": 2772},
                  {"name": "broken_factory", "chars": -1}],
    }), encoding="utf-8")
    report = collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=None)
    assert report.tool_footprint is not None
    out = render_overhead(report)
    assert "record_application" in out
    assert "tier=manage" in out


def test_overhead_missing_footprint_is_unmeasured_not_zero(tmp_path: Path) -> None:
    from context_health import collect_overhead, render_overhead

    report = collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=None)
    assert report.tool_footprint is None
    assert "unmeasured" in render_overhead(report)


def test_mcp_registration_status_yes_no_unknown(tmp_path: Path) -> None:
    from context_health import mcp_registration_status

    assert mcp_registration_status(None).startswith("unknown")

    ws = tmp_path / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    # No openclaw.json → unknown (unreadable ≠ "no")
    assert mcp_registration_status(ws).startswith("unknown")

    oc = tmp_path / ".openclaw" / "openclaw.json"
    oc.write_text(json.dumps({"mcp": {"servers": {"evo_tools": {}}}}), encoding="utf-8")
    assert mcp_registration_status(ws) == "yes"

    oc.write_text(json.dumps({"mcp": {"servers": {"other": {}}}}), encoding="utf-8")
    assert mcp_registration_status(ws) == "no"

    oc.write_text("{not json", encoding="utf-8")
    assert mcp_registration_status(ws).startswith("unknown")


def test_overhead_mcp_probe_gated_on_registration(tmp_path: Path, monkeypatch) -> None:
    """The manifest subprocess only runs for bots whose openclaw.json
    registers mcp.servers.evo_tools; others skip the probe entirely."""
    import context_health as ch

    calls: list[int] = []
    monkeypatch.setattr(ch, "mcp_manifest_weight", lambda: calls.append(1) or {
        "tool_count": 88, "total_chars": 92770,
        "families": [{"family": "action.bot", "tool_count": 12, "chars": 13756}],
    })

    ws = tmp_path / "home" / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    oc = tmp_path / "home" / ".openclaw" / "openclaw.json"

    oc.write_text(json.dumps({"mcp": {"servers": {}}}), encoding="utf-8")
    report = ch.collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=ws)
    assert report.mcp_registered == "no"
    assert report.mcp_manifest is None and not calls
    assert "not registered" in ch.render_overhead(report)

    oc.write_text(json.dumps({"mcp": {"servers": {"evo_tools": {}}}}), encoding="utf-8")
    report = ch.collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=ws)
    assert report.mcp_registered == "yes"
    assert calls and report.mcp_manifest is not None
    out = ch.render_overhead(report)
    assert "88 tools" in out
    assert "action.bot" in out


def test_overhead_mcp_unknown_registration_skips_probe(tmp_path: Path) -> None:
    from context_health import collect_overhead, render_overhead

    report = collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=None)
    assert report.mcp_registered.startswith("unknown")
    assert report.mcp_manifest is None
    assert "probe skipped" in render_overhead(report)


def test_overhead_mcp_registered_but_probe_failed_is_unmeasured(
    tmp_path: Path, monkeypatch,
) -> None:
    import context_health as ch

    monkeypatch.setattr(ch, "mcp_manifest_weight", lambda: None)
    ws = tmp_path / "home" / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    (tmp_path / "home" / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"mcp": {"servers": {"evo_tools": {}}}}), encoding="utf-8")
    report = ch.collect_overhead(tmp_path, "team-bot-a", 1, [], [], workspace_dir=ws)
    assert report.mcp_registered == "yes"
    assert report.mcp_manifest is None
    assert "unmeasured" in ch.render_overhead(report)
