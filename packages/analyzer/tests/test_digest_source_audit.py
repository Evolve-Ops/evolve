"""Tests for ``digest_source_audit`` — the digest source-health daemon.

Five slices of coverage:

1. ``_per_source_consecutive_failures`` — the central state machine.
   Each transition (success → fail, fail → fail, fail → success,
   skipped → resumed, new source appearing mid-window) has a focused
   test so a refactor can't silently regress the threshold logic.

2. ``_iter_bot_health_files`` — lookback window enforcement,
   missing-dir tolerance, malformed-file resilience.

3. ``_spec_for_broken_source`` — spec shape, signature embeds
   bot + source_name, body carries the fix command.

4. ``collect`` end-to-end with a fake network + fake bot workspaces.

5. ``run`` against a tmp_path-backed signal store — dedup across
   invocations, sweep-resolve on recovery, dry-run writes nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from signals import store as signals_store  # noqa: E402

import digest_source_audit  # noqa: E402
from digest_source_audit import (  # noqa: E402
    CONSECUTIVE_FAILURE_THRESHOLD,
    LOOKBACK_DAYS,
    PRODUCER,
    SIGNAL_TYPE,
    _iter_bot_health_files,
    _per_source_consecutive_failures,
    _spec_for_broken_source,
    collect,
    main,
    run,
)


# ── _per_source_consecutive_failures ────────────────────────────────────────


def _src(name="foo", kind="rss", target="https://example.com/foo.rss",
         ok=True, items=5, **extra) -> dict:
    base = {"name": name, "kind": kind, "target": target, "ok": ok, "items": items}
    base.update(extra)
    return base


def _run(date: str, *sources: dict) -> tuple[str, dict]:
    return (date, {"date": date, "bot_id": "atlas", "sources": list(sources)})


def test_single_success_run_yields_zero_failures() -> None:
    state = _per_source_consecutive_failures([_run("2026-06-01", _src(ok=True))])
    assert state["foo"]["consecutive_failures"] == 0
    assert state["foo"]["last_success_date"] == "2026-06-01"
    assert state["foo"]["last_failure_date"] is None


def test_consecutive_failures_increment_across_runs() -> None:
    state = _per_source_consecutive_failures([
        _run("2026-06-01", _src(ok=False, items=0)),
        _run("2026-06-02", _src(ok=False, items=0)),
        _run("2026-06-03", _src(ok=False, items=0)),
    ])
    assert state["foo"]["consecutive_failures"] == 3


def test_success_resets_consecutive_failure_count() -> None:
    """A single success run wipes prior consecutive failures — the
    point of the counter is "currently dark", not "ever failed"."""
    state = _per_source_consecutive_failures([
        _run("2026-06-01", _src(ok=False)),
        _run("2026-06-02", _src(ok=False)),
        _run("2026-06-03", _src(ok=True)),
        _run("2026-06-04", _src(ok=False)),
    ])
    assert state["foo"]["consecutive_failures"] == 1
    assert state["foo"]["last_success_date"] == "2026-06-03"


def test_skipped_reason_doesnt_count_as_failure() -> None:
    """An operator deliberately disabling a source (no_brave_key,
    no_github_token) MUST NOT trigger the audit — that's config, not
    drift. The skipped_reason field is the signal."""
    state = _per_source_consecutive_failures([
        _run("2026-06-01", _src(ok=False, skipped_reason="no_brave_key")),
        _run("2026-06-02", _src(ok=False, skipped_reason="no_brave_key")),
        _run("2026-06-03", _src(ok=False, skipped_reason="no_brave_key")),
        _run("2026-06-04", _src(ok=False, skipped_reason="no_brave_key")),
    ])
    assert state["foo"]["consecutive_failures"] == 0
    assert "no_brave_key" in state["foo"]["skipped_reasons"]


def test_new_source_mid_window_starts_fresh() -> None:
    """A source that first appears in run 3 has consecutive_failures=1
    after one failure run — not 3 (treating its absence in earlier
    runs as a failure would be wrong)."""
    state = _per_source_consecutive_failures([
        _run("2026-06-01"),  # no sources
        _run("2026-06-02"),  # still no sources
        _run("2026-06-03", _src(name="new-feed", ok=False)),
    ])
    assert state["new-feed"]["consecutive_failures"] == 1


def test_target_field_updates_when_url_changes() -> None:
    """When the operator edits sources.json to update a URL, the
    Signal's target should reflect the latest URL, not the first
    one we saw."""
    state = _per_source_consecutive_failures([
        _run("2026-06-01", _src(target="https://old.example.com/feed", ok=False)),
        _run("2026-06-02", _src(target="https://new.example.com/feed", ok=False)),
    ])
    assert state["foo"]["target"] == "https://new.example.com/feed"


def test_multiple_sources_tracked_independently() -> None:
    state = _per_source_consecutive_failures([
        _run("2026-06-01",
             _src(name="a", ok=True),
             _src(name="b", ok=False)),
        _run("2026-06-02",
             _src(name="a", ok=False),
             _src(name="b", ok=False)),
    ])
    assert state["a"]["consecutive_failures"] == 1
    assert state["b"]["consecutive_failures"] == 2


def test_non_dict_source_entries_are_skipped() -> None:
    """Defensive against on-disk corruption — bad entries shouldn't
    crash the audit."""
    state = _per_source_consecutive_failures([
        ("2026-06-01", {"sources": [_src(name="ok"), None, 42, "garbage"]}),
    ])
    assert "ok" in state
    assert len(state) == 1


# ── _iter_bot_health_files ──────────────────────────────────────────────────


def _write_health_file(workspace: Path, date_str: str, sources: list) -> None:
    digest_dir = workspace / "digest"
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / f"source_health-{date_str}.json").write_text(
        json.dumps({"date": date_str, "bot_id": "atlas", "sources": sources})
    )


def test_iter_files_respects_lookback_window(tmp_path: Path) -> None:
    """Files outside the lookback window are silently dropped, even
    if they parse cleanly."""
    ws = tmp_path / "Users" / "atlas" / ".openclaw" / "workspace"
    ws.mkdir(parents=True)

    _write_health_file(ws, "2026-06-01", [_src(ok=True)])
    # Way out of window:
    _write_health_file(ws, "2025-01-01", [_src(ok=True)])

    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    bot_info = {"user": "atlas"}
    # Monkey-patch the workspace lookup so it points at tmp_path.
    import digest_source_audit as mod
    orig = mod._bot_workspace
    mod._bot_workspace = lambda bid, bi: ws
    try:
        files = _iter_bot_health_files("atlas", bot_info, now=now)
    finally:
        mod._bot_workspace = orig
    assert [d for d, _ in files] == ["2026-06-01"]


def test_iter_files_returns_empty_for_bot_without_digest_dir(tmp_path: Path) -> None:
    """Most bots don't run a digest app — that's not an error."""
    ws = tmp_path / "Users" / "atlas" / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    # No digest/ dir.

    import digest_source_audit as mod
    orig = mod._bot_workspace
    mod._bot_workspace = lambda bid, bi: ws
    try:
        files = _iter_bot_health_files("atlas", {"user": "atlas"})
    finally:
        mod._bot_workspace = orig
    assert files == []


def test_iter_files_skips_malformed_files(tmp_path: Path) -> None:
    ws = tmp_path / "Users" / "atlas" / ".openclaw" / "workspace"
    digest = ws / "digest"
    digest.mkdir(parents=True)
    # Three files: one good, one bad JSON, one wrong schema.
    _write_health_file(ws, "2026-06-01", [_src(ok=True)])
    (digest / "source_health-2026-06-02.json").write_text("not json")
    (digest / "source_health-2026-06-03.json").write_text('{"no_sources_key": true}')

    import digest_source_audit as mod
    orig = mod._bot_workspace
    mod._bot_workspace = lambda bid, bi: ws
    try:
        files = _iter_bot_health_files(
            "atlas", {"user": "atlas"},
            now=datetime(2026, 6, 5, tzinfo=timezone.utc),
        )
    finally:
        mod._bot_workspace = orig
    assert [d for d, _ in files] == ["2026-06-01"]


# ── _spec_for_broken_source ────────────────────────────────────────────────


def _broken_entry(failures=3) -> dict:
    return {
        "kind":                 "rss",
        "target":               "https://example.com/feed.rss",
        "consecutive_failures": failures,
        "last_failure_date":    "2026-06-05",
        "last_success_date":    "2026-06-01",
        "total_runs_seen":      5,
        "skipped_reasons":      set(),
    }


def test_spec_signature_embeds_bot_and_source() -> None:
    s1 = _spec_for_broken_source("atlas", "anthropic-blog", _broken_entry())
    s2 = _spec_for_broken_source("team-bot-a",  "anthropic-blog", _broken_entry())
    s3 = _spec_for_broken_source("atlas", "google-blog-ai", _broken_entry())
    s4 = _spec_for_broken_source("atlas", "anthropic-blog", _broken_entry(failures=5))
    assert s1["signature"] != s2["signature"]
    assert s1["signature"] != s3["signature"]
    assert s1["signature"] == s4["signature"]  # same (bot, source) → same sig


def test_spec_body_carries_fix_command() -> None:
    spec = _spec_for_broken_source("atlas", "anthropic-blog", _broken_entry())
    assert "sources.json" in spec["body"]
    assert "atlas_digest.py preview" in spec["body"]
    assert "auto-resolves" in spec["body"].lower() or "Auto-resolves" in spec["body"]


def test_spec_severity_matches_monitor_convention() -> None:
    spec = _spec_for_broken_source("atlas", "anthropic-blog", _broken_entry())
    assert spec["producer"] == PRODUCER
    assert spec["type"] == SIGNAL_TYPE
    assert spec["severity"] == "warn"
    assert spec["scope"] == "pod"
    assert spec["flavor"] == "maintenance"


def test_spec_details_pin_actionable_fields() -> None:
    spec = _spec_for_broken_source("atlas", "anthropic-blog", _broken_entry())
    d = spec["details"]
    assert d["bot_id"] == "atlas"
    assert d["source_name"] == "anthropic-blog"
    assert d["kind"] == "rss"
    assert d["consecutive_failures"] == 3


# ── collect + run end-to-end ───────────────────────────────────────────────


def _setup_bot(tmp_path: Path, bot_id: str, runs: list[tuple[str, list]]) -> Path:
    """Drop a sequence of source_health files for a fake bot."""
    ws = tmp_path / "Users" / bot_id / ".openclaw" / "workspace"
    for date_str, sources in runs:
        _write_health_file(ws, date_str, sources)
    return ws


def _patch_workspace(monkeypatch, tmp_path: Path) -> None:
    """Redirect _bot_workspace to look under tmp_path/Users/<user>/..."""
    def _ws(bot_id: str, bot_info: dict) -> Path:
        user = bot_info.get("user", bot_id) if isinstance(bot_info, dict) else bot_id
        return tmp_path / "Users" / user / ".openclaw" / "workspace"
    monkeypatch.setattr(digest_source_audit, "_bot_workspace", _ws)


def test_collect_returns_nothing_when_under_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_bot(tmp_path, "atlas", [
        ("2026-06-03", [_src(ok=False)]),
        ("2026-06-04", [_src(ok=False)]),
    ])
    _patch_workspace(monkeypatch, tmp_path)
    specs = collect(
        tmp_path / "evolve",
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )
    assert specs == []   # only 2 failures, threshold is 3


def test_collect_fires_at_or_above_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_bot(tmp_path, "atlas", [
        ("2026-06-03", [_src(name="anthropic-blog", ok=False)]),
        ("2026-06-04", [_src(name="anthropic-blog", ok=False)]),
        ("2026-06-05", [_src(name="anthropic-blog", ok=False)]),
    ])
    _patch_workspace(monkeypatch, tmp_path)
    specs = collect(
        tmp_path / "evolve",
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )
    assert len(specs) == 1
    assert specs[0]["details"]["source_name"] == "anthropic-blog"
    assert specs[0]["details"]["consecutive_failures"] == 3


def test_run_dedups_across_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "evolve"
    _setup_bot(tmp_path, "atlas", [
        ("2026-06-03", [_src(name="x", ok=False)]),
        ("2026-06-04", [_src(name="x", ok=False)]),
        ("2026-06-05", [_src(name="x", ok=False)]),
    ])
    _patch_workspace(monkeypatch, tmp_path)
    run(shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    run(shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    sigs = list(signals_store.iter_active(shared, producer=PRODUCER))
    assert len(sigs) == 1  # second run did not create a duplicate


def test_run_sweep_resolves_when_source_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Add a successful run after the failures → next audit run
    auto-resolves the Signal."""
    shared = tmp_path / "evolve"
    bot_ws_runs = [
        ("2026-06-03", [_src(name="x", ok=False)]),
        ("2026-06-04", [_src(name="x", ok=False)]),
        ("2026-06-05", [_src(name="x", ok=False)]),
    ]
    _setup_bot(tmp_path, "atlas", bot_ws_runs)
    _patch_workspace(monkeypatch, tmp_path)

    run(shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert len(list(signals_store.iter_active(shared, producer=PRODUCER))) == 1

    # Source recovered the next day — write a success file and re-run audit.
    ws = tmp_path / "Users" / "atlas" / ".openclaw" / "workspace"
    _write_health_file(ws, "2026-06-06", [_src(name="x", ok=True)])
    kept, n_fired, n_resolved = run(
        shared,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    assert n_fired == 0
    assert n_resolved == 1
    assert kept == set()
    assert len(list(signals_store.iter_active(shared, producer=PRODUCER))) == 0


def test_run_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    shared = tmp_path / "evolve"
    _setup_bot(tmp_path, "atlas", [
        ("2026-06-03", [_src(name="x", ok=False)]),
        ("2026-06-04", [_src(name="x", ok=False)]),
        ("2026-06-05", [_src(name="x", ok=False)]),
    ])
    _patch_workspace(monkeypatch, tmp_path)
    kept, n_fired, n_resolved = run(
        shared,
        dry_run=True,
        network={"bots": {"atlas": {"user": "atlas"}}},
        now=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )
    assert n_fired == 1
    assert n_resolved == 0
    assert len(list(signals_store.iter_active(shared, producer=PRODUCER))) == 0
    assert "would_observe" in capsys.readouterr().out


def test_main_returns_zero_with_no_drift(tmp_path: Path) -> None:
    # Empty shared dir; no bots; no drift. Daemon should exit cleanly.
    rc = main(["--shared-dir", str(tmp_path), "--once"])
    assert rc == 0


def test_main_returns_zero_with_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift surfaces via Signals; the daemon's own exit code is for
    launchd liveness, not the drift count."""
    _setup_bot(tmp_path, "atlas", [
        ("2026-06-03", [_src(name="x", ok=False)]),
        ("2026-06-04", [_src(name="x", ok=False)]),
        ("2026-06-05", [_src(name="x", ok=False)]),
    ])
    _patch_workspace(monkeypatch, tmp_path)
    # Write a fake network.json that the daemon can find at the
    # default shared-dir-relative path.
    (tmp_path / "network.json").write_text(
        json.dumps({"bots": {"atlas": {"user": "atlas"}}})
    )
    rc = main(["--shared-dir", str(tmp_path), "--once"])
    assert rc == 0
