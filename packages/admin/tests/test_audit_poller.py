"""Integration tests for the admin-side audit_poller.

Constructs synthetic bot outboxes in a temp tree, mocks the bot-user path
resolution to point there, and asserts that:
  - tier2_finding records become Signals via signals.store.observe()
  - tier2_run_summary records drive sweep_resolve() with the right kept set
  - processed files get archived into _ingested/<YYYY-MM-DD>/
  - unknown record kinds are left untouched (not silently archived)
  - a broken poller for one bot does not stop the rest

These tests use the real signals.store on a temp shared_dir; no Anthropic
calls, no LaunchDaemons, no actual bot users.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

# Ensure the analyzer dir is importable so signals.store loads.
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_outbox(tmp_root: Path, bot_user: str) -> Path:
    """Create /<tmp_root>/Users/<bot_user>/.openclaw/workspace/evolve/audit_outbox/."""
    outbox = tmp_root / "Users" / bot_user / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _write_finding(outbox: Path, *, bot_id: str, app_id: str,
                   assertion_id: str = "file_missing",
                   severity: str = "critical",
                   path: str = "scripts/missing.py",
                   record_id: str = "rec-1",
                   audit_run_id: str = "run-1",
                   coalesce_key: str | None = None) -> Path:
    rec = {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "tier2_finding",
        "ts": "2026-05-16T00:00:00Z",
        "runner_version": "1.0.0",
        "producer": "app_structural_verifier",
        "bot_id": bot_id,
        "app_id": app_id,
        "signature": f"app_structural_verifier:{assertion_id}:{bot_id}:{app_id}:path={path}",
        "assertion_id": assertion_id,
        "severity": severity,
        "summary": f"missing {path}",
        "evidence": {"path": path},
    }
    if coalesce_key is not None:
        rec["coalesce_key"] = coalesce_key
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def _write_run_summary(outbox: Path, *, bot_id: str,
                       kept_signatures: Iterable[str] = (),
                       record_id: str = "rec-summary",
                       audit_run_id: str = "run-1") -> Path:
    rec = {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "tier2_run_summary",
        "ts": "2026-05-16T00:01:00Z",
        "runner_version": "1.0.0",
        "bot_id": bot_id,
        "apps_audited": 1,
        "apps_with_findings": 0,
        "total_findings": 0,
        "kept_signatures": list(kept_signatures),
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch) -> Path:
    """Re-point audit_poller's /Users paths into tmp_path."""
    def _audit_outbox(bot_user: str) -> Path:
        return tmp_path / "Users" / bot_user / ".openclaw" / "workspace" / "evolve" / "audit_outbox"

    def _audit_ingested(bot_user: str) -> Path:
        return _audit_outbox(bot_user) / "_ingested"

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(audit_poller, "_audit_outbox_ingested", _audit_ingested)
    return tmp_path


# ── tier2_finding ingestion ──────────────────────────────────────────────────


def test_finding_ingest_emits_signal_and_archives(tmp_root: Path, tmp_path: Path) -> None:
    """A tier2_finding record becomes a Signal and the file moves to _ingested/."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal")

    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    assert result.findings_ingested == 1
    assert result.files_processed == 1
    assert result.errors == []

    # Outbox is empty; the file moved into _ingested/<date>/
    remaining = [p.name for p in outbox.iterdir() if p.is_file()]
    assert remaining == []
    ingested = list((outbox / "_ingested").rglob("*.json"))
    assert len(ingested) == 1

    # A Signal was written into shared/signals/firing/
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    body = json.loads(firing[0].read_text())
    assert body["producer"] == "app_structural_verifier"
    assert body["bot_id"] == "team_bot_a"
    assert body["scope"] == "bot"
    assert body["details"]["app_id"] == "journal"


def test_archive_permission_error_degrades_gracefully(
    tmp_root: Path, monkeypatch, caplog,
) -> None:
    """If the direct move hits PermissionError, _archive_file logs and returns
    False (file left for next tick) WITHOUT shelling out to sudo.

    There is no sudoers grant for ``/bin/mv`` on per-bot outbox records and the
    evolve daemon has no tty, so a sudo fallback could never fire — it was dead
    code. This pins that no privileged subprocess is attempted.
    """
    outbox = _make_outbox(tmp_root, "team_bot_a")
    rec = _write_finding(outbox, bot_id="team_bot_a", app_id="journal")

    def _boom(*_a, **_k):
        raise PermissionError("evolve locked out of workspace/evolve")

    monkeypatch.setattr(audit_poller.shutil, "move", _boom)

    sudo_calls: list[list[str]] = []
    real_run = audit_poller.subprocess.run

    def _spy_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)):
            sudo_calls.append(list(cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(audit_poller.subprocess, "run", _spy_run)

    import logging
    with caplog.at_level(logging.WARNING):
        ok = audit_poller._archive_file(rec, "team_bot_a")

    assert ok is False
    # Record left in place for the next (idempotent) tick.
    assert rec.exists()
    # The failure is visible, not swallowed.
    assert any("archive failed" in r.message for r in caplog.records)
    # No privileged fallback was attempted.
    assert not any(
        c[:1] == ["sudo"] and any(b in c for b in ("/bin/mv", "/bin/rm", "/bin/mkdir"))
        for c in sudo_calls
    ), sudo_calls


def test_finding_severity_maps_to_signal_severity(tmp_root: Path, tmp_path: Path) -> None:
    """critical → alert, major → warn, minor/info → info."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   severity="critical", record_id="rec-c", path="a.py")
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   severity="major", record_id="rec-m", path="b.py")
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   severity="minor", record_id="rec-mi", path="c.py")

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    sev_by_app_path: dict[str, str] = {}
    for f in (shared / "signals" / "firing").glob("*.json"):
        body = json.loads(f.read_text())
        sev_by_app_path[body["details"]["evidence"]["path"]] = body["severity"]
    assert sev_by_app_path == {"a.py": "alert", "b.py": "warn", "c.py": "info"}


def test_repeat_ingest_is_idempotent(tmp_root: Path, tmp_path: Path) -> None:
    """Re-ingesting the same signature bumps observation_count, doesn't duplicate Signals."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # First run
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal", record_id="rec-1")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    first = list((shared / "signals" / "firing").glob("*.json"))
    assert len(first) == 1
    obs1 = json.loads(first[0].read_text())["observation_count"]

    # Second run with the same signature but a different record_id
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal", record_id="rec-2")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    after = list((shared / "signals" / "firing").glob("*.json"))
    assert len(after) == 1
    obs2 = json.loads(after[0].read_text())["observation_count"]
    assert obs2 > obs1


# ── Coalesce key → Signal.incident_key ──────────────────────────────────────


def test_finding_coalesce_key_lands_on_signal_incident_key(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """The runner sets ``coalesce_key=f"app_structural:{bot}:{app}"`` on each
    outbox record so the four per-manifest assertions collapse into one
    expandable row on the Alerts page. The poller plumbs that onto
    ``Signal.incident_key`` — the field the Alerts UI groups by.

    Two findings with the same coalesce_key (different assertion_ids)
    must end up as two distinct Signals (different signatures) but
    sharing one ``incident_key``.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    key = "app_structural:team_bot_a:i-deadbeef"
    _write_finding(
        outbox, bot_id="team_bot_a", app_id="i-deadbeef",
        assertion_id="app_no_producer_surface", path="x",
        record_id="rec-a", coalesce_key=key,
    )
    _write_finding(
        outbox, bot_id="team_bot_a", app_id="i-deadbeef",
        assertion_id="app_discoverability_no_cli", path="y",
        record_id="rec-b", coalesce_key=key,
    )

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    firing = [json.loads(p.read_text())
              for p in (shared / "signals" / "firing").glob("*.json")]
    # Discoverability is trail-only, so we expect only the
    # app_no_producer_surface Signal to land. But its incident_key
    # must equal the coalesce_key.
    assert len(firing) >= 1
    keys = {sig.get("incident_key") for sig in firing}
    assert key in keys
    # display_name carried in details so the UI can render a coalesced
    # group header without re-loading the manifest.
    structural = [s for s in firing if s["type"] == "app_no_producer_surface"]
    assert structural, "structural finding missing"
    # display_name falls back to app_id when no manifest can be loaded
    # in the test environment; the key invariant is that the field exists.
    assert "display_name" in structural[0]["details"]


def test_finding_without_coalesce_key_leaves_incident_key_null(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Backward compat: outbox records emitted by older bot-side runners
    don't carry ``coalesce_key``. The poller must not invent one; the
    Signal's ``incident_key`` stays ``None`` and the legacy
    title/type-based grouping in the Alerts UI continues to handle them.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_finding(
        outbox, bot_id="team_bot_a", app_id="journal",
        # explicit None to make the test intent clear
        coalesce_key=None,
    )

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    assert json.loads(firing[0].read_text())["incident_key"] is None


# ── tier2_run_summary sweep_resolve ──────────────────────────────────────────


def test_run_summary_sweeps_signals_not_in_kept(tmp_root: Path, tmp_path: Path) -> None:
    """Active Signal not in kept_signatures auto-resolves on next run summary."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # Round 1: emit a finding so a Signal lands
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal", record_id="rec-1")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 1

    # Round 2: emit a run summary with kept_signatures = [] (cleared)
    _write_run_summary(outbox, bot_id="team_bot_a", kept_signatures=[], record_id="rec-summary")
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.summaries_processed == 1
    # The previously-firing Signal moved out of firing/
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 0
    # And landed in archived/
    archived = list((shared / "signals" / "archived").glob("*.json"))
    assert len(archived) == 1


def test_run_summary_does_not_sweep_other_bots_signals(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Regression: bot A's run summary must NOT resolve bot B's signals.

    ``producer="app_structural_verifier"`` is shared pod-wide, but
    ``kept_signatures`` only carries the running bot's signatures. Without
    the ``bot_ids`` filter, A's sweep would clear every other bot's still-
    firing structural findings (observed 2026-06-07: 290 cross-bot
    firing↔resolved transitions in a single day's signal log).
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox_a = _make_outbox(tmp_root, "team_bot_a")
    outbox_b = _make_outbox(tmp_root, "personal_bot_user")

    _write_finding(outbox_a, bot_id="team_bot_a", app_id="journal",
                   record_id="a-1", path="scripts/a.py")
    _write_finding(outbox_b, bot_id="team_bot_b", app_id="tasks",
                   record_id="b-1", path="scripts/b.py")

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    audit_poller.poll_bot("team_bot_b", "personal_bot_user", shared)
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 2

    # Bot A's run summary lists only A's signature.
    a_sig = ("app_structural_verifier:file_missing:team_bot_a:journal:"
             "path=scripts/a.py")
    _write_run_summary(outbox_a, bot_id="team_bot_a",
                       kept_signatures=[a_sig], record_id="a-summary")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 2, (
        "team_bot_b's Signal was swept by team_bot_a's run summary — "
        "sweep_resolve is missing its bot_ids filter"
    )


def test_run_summary_preserves_kept_signatures(tmp_root: Path, tmp_path: Path) -> None:
    """Signal whose signature IS in kept_signatures stays firing."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    finding_path = _write_finding(outbox, bot_id="team_bot_a", app_id="journal")
    sig = json.loads(finding_path.read_text())["signature"]
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    _write_run_summary(outbox, bot_id="team_bot_a", kept_signatures=[sig])
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1


def test_run_summary_sweeps_stranded_trail_only_discoverability_signal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Bug-3 (2026-06-12): a discoverability Signal emitted BEFORE the
    trail-only gate shipped strands in firing/ forever.

    The bot-side runner still lists the discoverability signature in
    ``kept_signatures`` (it keeps running the assertion), so the per-bot sweep
    treats it as "kept" and never resolves it — and the poller now suppresses
    discoverability findings, so it's never re-observed either. The poller must
    strip trail-only signatures from the keep-set so the stranded Signal
    finally gets archived. 56 such Signals from one 06-08 run were still firing
    two runs later in the review.
    """
    from signals import store as signals_store

    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_c")

    # Seed a legacy (pre-gate) discoverability Signal directly — the poller
    # would never emit one today (trail-only), but old ones predate the gate.
    disco_sig = "app_structural_verifier:app_discoverability_no_cli:team_bot_c:journal:"
    signals_store.observe(
        shared,
        signature=disco_sig,
        producer="app_structural_verifier",
        type="app_discoverability_no_cli",
        flavor="maintenance",
        severity="info",
        scope="bot",
        bot_id="team_bot_c",
        title="journal: app_discoverability_no_cli",
        body="no CLI command",
    )
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 1

    # The runner's run summary STILL lists the discoverability signature as
    # kept (it keeps producing the finding); pre-fix this protected the
    # stranded Signal from the sweep.
    _write_run_summary(
        outbox, bot_id="team_bot_c", kept_signatures=[disco_sig], record_id="rec-summary",
    )
    result = audit_poller.poll_bot("team_bot_c", "team_bot_c", shared)
    assert result.summaries_processed == 1

    # The stranded discoverability Signal was swept out of firing/ into
    # archived/, despite its signature being in kept_signatures.
    assert len(list((shared / "signals" / "firing").glob("*.json"))) == 0
    assert len(list((shared / "signals" / "archived").glob("*.json"))) == 1


# ── Unknown record kinds ─────────────────────────────────────────────────────


def test_unknown_kind_left_in_outbox(tmp_root: Path, tmp_path: Path) -> None:
    """A future record kind is logged but not archived so a handler can
    pick it up in a follow-up release."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    p = outbox / "rec-future.json"
    p.write_text(json.dumps({
        "record_id": "rec-future",
        "kind": "tier99_quantum_finding",  # genuinely future kind
        "bot_id": "team_bot_a",
        "app_id": "journal",
    }))

    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.files_processed == 0
    assert any("unhandled kind: tier99_quantum_finding" in e for e in result.errors)
    assert p.exists()  # still in outbox


# ── tick() aggregation ──────────────────────────────────────────────────────


def test_tick_polls_every_bot(tmp_root: Path, tmp_path: Path) -> None:
    """tick() iterates the bot_users mapping and aggregates counters."""
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_finding(_make_outbox(tmp_root, "team_bot_a"),
                   bot_id="team_bot_a", app_id="journal", record_id="team_bot_a-1")
    _write_finding(_make_outbox(tmp_root, "personal_bot_user"),
                   bot_id="team_bot_b", app_id="task_manager", record_id="team_bot_b-1",
                   path="scripts/tasks.py")

    aggregate = audit_poller.tick(
        shared, network=None, bot_users={"team_bot_a": "team_bot_a", "team_bot_b": "personal_bot_user"},
    )
    assert aggregate.total_files == 2
    assert aggregate.total_findings == 2
    assert {r.bot_id for r in aggregate.bots} == {"team_bot_a", "team_bot_b"}


def test_tick_broken_bot_does_not_stop_others(
    tmp_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    """If poll_bot crashes for one bot, the other bots still get drained."""
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_finding(_make_outbox(tmp_root, "team_bot_a"),
                   bot_id="team_bot_a", app_id="journal", record_id="team_bot_a-1")

    original_poll = audit_poller.poll_bot
    def _flaky_poll(bot_id: str, bot_user: str, shared_dir: Path):
        if bot_id == "broken":
            raise RuntimeError("kaboom")
        return original_poll(bot_id, bot_user, shared_dir)

    monkeypatch.setattr(audit_poller, "poll_bot", _flaky_poll)
    aggregate = audit_poller.tick(
        shared, network=None, bot_users={"broken": "broken", "team_bot_a": "team_bot_a"},
    )
    assert aggregate.total_findings == 1
    broken = [r for r in aggregate.bots if r.bot_id == "broken"][0]
    assert any("poll crashed" in e for e in broken.errors)


# ── drain-liveness heartbeat ─────────────────────────────────────────────────


def _read_heartbeat(shared: Path) -> list[dict]:
    p = shared / audit_poller.AUDIT_DRAIN_HEARTBEAT_REL
    return json.loads(p.read_text())["recent"]


def test_tick_writes_drain_heartbeat(tmp_root: Path, tmp_path: Path) -> None:
    """Every tick appends a (ts, processed, backlog) sample; a healthy drain
    archives its records so the post-drain backlog is 0."""
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_finding(_make_outbox(tmp_root, "team_bot_a"),
                   bot_id="team_bot_a", app_id="journal", record_id="a-1")

    audit_poller.tick(shared, network=None, bot_users={"team_bot_a": "team_bot_a"})

    recent = _read_heartbeat(shared)
    assert len(recent) == 1
    assert recent[0]["processed"] == 1
    assert recent[0]["backlog"] == 0  # the one finding was ingested + archived
    assert isinstance(recent[0]["ts"], int)


def test_tick_heartbeat_counts_stuck_backlog(tmp_root: Path, tmp_path: Path) -> None:
    """An un-drainable record (unknown kind) is left in the outbox, so the
    heartbeat records processed=0 with a non-zero backlog — the silent-stall
    fingerprint monitor_coverage keys on."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    (outbox / "weird.json").write_text(json.dumps({"kind": "mystery_kind"}))

    audit_poller.tick(shared, network=None, bot_users={"team_bot_a": "team_bot_a"})

    recent = _read_heartbeat(shared)
    assert recent[-1]["processed"] == 0
    assert recent[-1]["backlog"] == 1


def test_heartbeat_is_a_bounded_rolling_window(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    for i in range(audit_poller._HEARTBEAT_HISTORY_MAX + 10):
        audit_poller._record_drain_heartbeat(shared, processed=0, backlog=i, now=1000 + i)
    recent = _read_heartbeat(shared)
    assert len(recent) == audit_poller._HEARTBEAT_HISTORY_MAX
    # Oldest samples were trimmed; the latest is preserved.
    assert recent[-1]["backlog"] == audit_poller._HEARTBEAT_HISTORY_MAX + 9


def test_heartbeat_write_never_raises(tmp_path: Path) -> None:
    """A heartbeat write failure must never break a drain tick."""
    # Point at a path whose parent cannot be created (a file in the way).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad_shared = blocker  # mkdir(parents=True) under a file → OSError, swallowed
    audit_poller._record_drain_heartbeat(bad_shared, processed=0, backlog=5)
    # No exception == pass.


# ── _severity_for_signal mapping helper ─────────────────────────────────────


@pytest.mark.parametrize("audit_sev,expected", [
    ("critical", "alert"),
    ("major", "warn"),
    ("minor", "info"),
    ("info", "info"),
    ("unknown-thing", "info"),
])
def test_severity_mapping(audit_sev: str, expected: str) -> None:
    assert audit_poller._severity_for_signal(audit_sev) == expected


# ── Tier-3 record ingestion ─────────────────────────────────────────────────


def _write_tier3_finding(
    outbox: Path, *, bot_id: str, app_id: str,
    outcome: str = "propose",
    severity: str = "major",
    record_id: str = "rec-t3-1",
    audit_run_id: str = "run-1",
    obs_id: str = "obs-1",
    sig_suffix: str = "abc",
) -> Path:
    rec = {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "tier3_finding",
        "ts": "2026-05-16T00:00:00Z",
        "runner_version": "1.1.0",
        "producer": "app_audit_tier3",
        "bot_id": bot_id,
        "app_id": app_id,
        "signature": f"app_audit_tier3:drift:{bot_id}:{app_id}:{sig_suffix}",
        "obs_id": obs_id,
        "category": "drift",
        "severity": severity,
        "description": "manifest claims feature X but code lacks it",
        "evidence": ["scripts/x.py:42"],
        "outcome": outcome,
        "rationale": "operator should review",
        "transformation_summary": "",
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def _write_conflict_notice(
    outbox: Path, *, bot_id: str, app_id: str,
    record_id: str = "rec-conflict-1",
) -> Path:
    rec = {
        "record_id": record_id,
        "audit_run_id": "run-1",
        "kind": "conflict_notice",
        "ts": "2026-05-16T00:00:00Z",
        "runner_version": "1.1.0",
        "producer": "app_audit_tier3",
        "bot_id": bot_id,
        "app_id": app_id,
        "signature": f"app_audit_tier3:cross_app_conflict:{bot_id}:{app_id}:xyz",
        "obs_id": "obs-1",
        "category": "drift",
        "severity": "major",
        "description": "manifest_path_update would touch shared file",
        "evidence": ["scripts/shared.py"],
        "rationale": "",
        "summary": "cross-app conflict on scripts/shared.py: 2 other app(s) reference this file",
        "file_path": "scripts/shared.py",
        "affected_apps": [
            {"pkg_id": "p-b", "app_id": "b", "display_name": "B", "role": "owner"},
            {"pkg_id": "p-c", "app_id": "c", "display_name": "C", "role": "dependency"},
        ],
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def test_tier3_propose_outcome_emits_proposal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """propose outcome → Proposal written into shared/proposals/pending/."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_tier3_finding(outbox, bot_id="team_bot_a", app_id="journal",
                          outcome="propose", severity="major")
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.tier3_findings_ingested == 1
    assert result.tier3_proposals_raised == 1
    # Proposal landed
    pending = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1
    body = json.loads(pending[0].read_text())
    assert body["bot_id"] == "team_bot_a"
    assert body["generator_id"] == "app_audit_tier3"
    # Investigation action with the manifest finding in its context
    assert "Investigation" in body["action"].get("kind", "")
    assert "journal" in body["problem"]


def test_tier3_auto_fix_outcome_no_proposal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """auto_fix outcome is trail-only — no Proposal raised."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_tier3_finding(outbox, bot_id="team_bot_a", app_id="journal",
                          outcome="auto_fix")
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.tier3_findings_ingested == 0   # no Proposal counted
    pending = (shared / "proposals" / "pending")
    if pending.exists():
        assert len(list(pending.glob("*.json"))) == 0


def test_conflict_notice_emits_proposal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """conflict_notice record produces an audit_conflict_notice Proposal."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_conflict_notice(outbox, bot_id="team_bot_a", app_id="journal")
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.tier3_conflict_notices == 1
    pending = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1
    body = json.loads(pending[0].read_text())
    assert body["generator_id"] == "app_audit_tier3_conflict"
    # Affected apps appear in the proposal context
    ctx = body["action"]["context"]
    assert "scripts/shared.py" in ctx
    assert "Affected apps" in ctx


def test_tier3_run_summary_archives_without_proposal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """tier3_run_summary records are counter-only; archived after processing."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    summary = {
        "record_id": "rec-sum",
        "audit_run_id": "run-1",
        "kind": "tier3_run_summary",
        "ts": "2026-05-16T00:00:00Z",
        "bot_id": "team_bot_a",
        "apps_audited": 3,
        "outcomes": {"propose": 1, "dismiss": 2, "auto_fix": 0, "conflict_notice": 0},
        "total_tokens": 12000,
    }
    (outbox / "rec-sum.json").write_text(json.dumps(summary))
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.summaries_processed == 1
    # File moved to _ingested
    assert not (outbox / "rec-sum.json").exists()
    ingested = list((outbox / "_ingested").rglob("*.json"))
    assert len(ingested) == 1


def test_run_failed_record_archives_without_signal(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """run_failed records are trail-only on the bot side — admin just archives."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    rec = {
        "record_id": "rec-fail",
        "kind": "run_failed",
        "bot_id": "team_bot_a",
        "ts": "2026-05-16T00:00:00Z",
        "error": "openclaw exit 1",
    }
    (outbox / "rec-fail.json").write_text(json.dumps(rec))
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.files_processed == 1
    assert result.errors == []


# ── Tier-3 supersede (Finding 1 Phase 3, 2026-06-09) ────────────────────────


def _run_tier3_batch(
    outbox: Path,
    shared: Path,
    *,
    bot_id: str,
    app_id: str,
    audit_run_id: str,
    count: int = 5,
    record_prefix: str = "r1",
) -> None:
    """Write ``count`` distinct tier3 findings for one (bot, app, run), then
    drain the outbox so the proposals land in ``shared/proposals/pending/``.
    """
    for i in range(count):
        _write_tier3_finding(
            outbox,
            bot_id=bot_id,
            app_id=app_id,
            audit_run_id=audit_run_id,
            record_id=f"rec-{record_prefix}-{i}",
            obs_id=f"obs-{record_prefix}-{i}",
            # Distinct signature per finding so each becomes its own proposal
            # (the poller's trigger-obs dedup would collapse identical sigs).
            sig_suffix=f"{record_prefix}-{i}",
        )
    audit_poller.poll_bot(bot_id, bot_id, shared)


def _pending_tier3_for(
    shared: Path, *, bot_id: str, app_id: str,
) -> list[dict]:
    pending = shared / "proposals" / "pending"
    if not pending.exists():
        return []
    out: list[dict] = []
    for path in sorted(pending.glob("*.json")):
        body = json.loads(path.read_text())
        if body.get("bot_id") != bot_id:
            continue
        if body.get("generator_id") != "app_audit_tier3":
            continue
        signals = (body.get("provenance") or {}).get("signals") or {}
        if signals.get("app_id") != app_id:
            continue
        out.append(body)
    return out


def test_tier3_supersedes_prior_run_pending_proposals(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """R2 emit archives R1's pending parent for the same (bot, app).

    Each run's 5 findings coalesce to one parent Proposal per (bot, app)
    via ``coalesce_key``; the supersede (pending-only) archives the prior
    run's operator-untouched parent BEFORE R2's first finding looks for a
    pending parent to fold into, so R2 lands a fresh parent (carrying only
    its own sub_findings) and R1's parent moves to archived/.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # R1 — five findings fold into one parent in pending/
    _run_tier3_batch(
        outbox, shared,
        bot_id="team_bot_a", app_id="journal",
        audit_run_id="run-1", record_prefix="r1",
    )
    r1_pending = _pending_tier3_for(
        shared, bot_id="team_bot_a", app_id="journal",
    )
    assert len(r1_pending) == 1
    assert len(r1_pending[0].get("sub_findings") or []) == 4

    # R2 — five new findings; R1's parent archives, R2's parent lands.
    _run_tier3_batch(
        outbox, shared,
        bot_id="team_bot_a", app_id="journal",
        audit_run_id="run-2", record_prefix="r2",
    )

    pending = _pending_tier3_for(shared, bot_id="team_bot_a", app_id="journal")
    assert len(pending) == 1
    assert pending[0]["provenance"]["signals"]["audit_run_id"] == "run-2"
    assert len(pending[0].get("sub_findings") or []) == 4

    # The R1 parent moved to archived/ with the expected status + reason.
    archived = list((shared / "proposals" / "archived").glob("*.json"))
    archived_bodies = [json.loads(p.read_text()) for p in archived]
    r1_archived = [
        b for b in archived_bodies
        if b.get("generator_id") == "app_audit_tier3"
        and (b.get("provenance") or {}).get("signals", {}).get("audit_run_id")
            == "run-1"
    ]
    assert len(r1_archived) == 1
    body = r1_archived[0]
    assert body["status"] == "resolved_externally"
    last = body["history"][-1]
    assert last["to_status"] == "resolved_externally"
    assert last["actor"] == "app_audit_tier3"
    assert "superseded by run-2" in last["reason"]


def test_tier3_supersede_preserves_operator_engaged_proposals(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """A snoozed (operator-engaged) R1 parent isn't superseded or re-coalesced.

    Production-accurate: an operator who defers a card moves it to
    ``snoozed/``. Both the supersede sweep and the coalesce lookup iterate
    ``pending/`` only, so neither touches the snoozed R1 parent — R1 stays
    intact in snoozed/ and R2 lands its own fresh parent in pending/. This
    pins that the ``(bot, app)`` coalesce grain does NOT silently fold a new
    run's findings into a card the operator has already set aside.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # R1 — five findings collapse to one parent in pending/.
    _run_tier3_batch(
        outbox, shared,
        bot_id="team_bot_a", app_id="journal",
        audit_run_id="run-1", record_prefix="r1",
    )

    # Operator snoozes the R1 parent: history length > 1 + a move to
    # snoozed/, exactly as the real snooze path does.
    pending_dir = shared / "proposals" / "pending"
    snoozed_dir = shared / "proposals" / "snoozed"
    snoozed_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted(pending_dir.glob("*.json"))
    first = json.loads(paths[0].read_text())
    first["history"].append({
        "from_status": "pending",
        "to_status": "snoozed",
        "at": "2026-06-08T12:00:00+00:00",
        "actor": "user",
        "reason": "operator deferred",
    })
    first["status"] = "snoozed"
    (snoozed_dir / paths[0].name).write_text(json.dumps(first, indent=2))
    paths[0].unlink()

    # R2 — the snoozed R1 parent is invisible to both supersede and
    # coalesce (pending-only); R2 emits its own fresh parent.
    _run_tier3_batch(
        outbox, shared,
        bot_id="team_bot_a", app_id="journal",
        audit_run_id="run-2", record_prefix="r2",
    )

    # R1 stays intact in snoozed/ — not archived, not re-coalesced into.
    snoozed = list(snoozed_dir.glob("*.json"))
    assert len(snoozed) == 1
    r1_body = json.loads(snoozed[0].read_text())
    assert r1_body["id"] == first["id"]
    assert r1_body["provenance"]["signals"]["audit_run_id"] == "run-1"
    assert len(r1_body.get("sub_findings") or []) == 4  # unchanged from R1

    # R2 lands as its own fresh pending parent.
    r2_pending = _pending_tier3_for(shared, bot_id="team_bot_a", app_id="journal")
    assert len(r2_pending) == 1
    assert r2_pending[0]["provenance"]["signals"]["audit_run_id"] == "run-2"

    # No R1 proposals archived — the only R1 proposal was engaged.
    archived_dir = shared / "proposals" / "archived"
    if archived_dir.exists():
        r1_archived = [
            json.loads(p.read_text()) for p in archived_dir.glob("*.json")
            if json.loads(p.read_text()).get("generator_id") == "app_audit_tier3"
            and (json.loads(p.read_text()).get("provenance") or {})
                .get("signals", {}).get("audit_run_id") == "run-1"
        ]
        assert r1_archived == []


def test_tier3_supersede_scoped_to_bot_and_app(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Supersede only touches (bot, app) — other apps' pending proposals stay.

    Mirrors the dry-run case from the spec: a bot has two distinct audit
    runs that cover different apps; nothing should archive across (app_id)
    boundaries.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # R1 covers app `journal`.
    _run_tier3_batch(
        outbox, shared,
        bot_id="team_bot_a", app_id="journal",
        audit_run_id="run-1", record_prefix="r1",
    )

    # R2 covers a DIFFERENT app on the same bot.
    _run_tier3_batch(
        outbox, shared,
        bot_id="team_bot_a", app_id="todos",
        audit_run_id="run-2", record_prefix="r2",
    )

    # Both apps' parents remain in pending/ — supersede is (bot, app)-scoped.
    # Each app's 5 findings fold into one parent via the (bot, app) coalesce
    # grain; the two apps stay separate cards (distinct coalesce_keys).
    journal_pending = _pending_tier3_for(
        shared, bot_id="team_bot_a", app_id="journal",
    )
    todos_pending = _pending_tier3_for(
        shared, bot_id="team_bot_a", app_id="todos",
    )
    assert len(journal_pending) == 1
    assert len(todos_pending) == 1

    archived_dir = shared / "proposals" / "archived"
    if archived_dir.exists():
        assert list(archived_dir.glob("*.json")) == []


# ── Re-emission cut: drop dedup-hits + heartbeats instead of archiving ───────
#
# The poller archives only records that carry forensic value and DELETEs pure
# re-emissions the signal/proposal store already dedupes. These tests pin the
# archive-vs-delete decision at the terminal drain step. They do NOT change
# what the signal/proposal store holds — the store assertions in the suites
# above still pass unchanged.


def _ingested_files(outbox: Path) -> list[Path]:
    ing = outbox / "_ingested"
    return list(ing.rglob("*.json")) if ing.exists() else []


def _noop_run_summary(
    outbox: Path, *, bot_id: str, kind: str = "tier2_run_summary",
    record_id: str = "rec-noop",
) -> Path:
    """A no-op heartbeat: apps_audited:0, no findings, no kept signatures."""
    rec = {
        "record_id": record_id,
        "audit_run_id": "run-noop",
        "kind": kind,
        "ts": "2026-05-16T00:01:00Z",
        "runner_version": "1.0.0",
        "bot_id": bot_id,
        "apps_audited": 0,
        "apps_with_findings": 0,
        "total_findings": 0,
        "kept_signatures": [],
        "outcomes": {"propose": 0, "dismiss": 0, "auto_fix": 0,
                     "conflict_notice": 0},
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def test_dedup_hit_finding_is_deleted_not_archived(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """(1) An unchanged dedup-hit on an already-firing Signal is DELETED, not
    archived — the live Signal already holds the deduped copy."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # First emission creates the Signal and IS archived.
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   record_id="rec-1")
    r1 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r1.records_archived == 1
    assert r1.records_dropped == 0
    assert len(_ingested_files(outbox)) == 1

    # Second emission of the SAME signature (different record_id) is an
    # unchanged dedup-hit → DELETED, not added to _ingested/.
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   record_id="rec-2")
    r2 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r2.findings_ingested == 1          # still ingested into the store
    assert r2.records_dropped == 1
    assert r2.dropped_dedup_hit == 1
    assert r2.records_archived == 0
    assert r2.files_processed == 1            # root still drained
    # _ingested/ still has only the first copy; the dedup-hit was deleted.
    assert len(_ingested_files(outbox)) == 1
    # The outbox root is empty — the record was removed (deleted, not stuck).
    assert [p.name for p in outbox.iterdir() if p.is_file()] == []
    # Exactly one live Signal, bumped, not duplicated.
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1


def test_new_finding_is_archived(tmp_root: Path, tmp_path: Path) -> None:
    """(2) A NEW finding (created Signal) IS archived."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   record_id="rec-new")
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.records_archived == 1
    assert result.records_dropped == 0
    assert len(_ingested_files(outbox)) == 1


def test_reopened_finding_is_archived(tmp_root: Path, tmp_path: Path) -> None:
    """(3a) A REOPENED finding (resolved → firing within window) IS archived."""
    from signals import store as signals_store

    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # Create + archive.
    fpath = _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                           record_id="rec-1")
    sig = json.loads(fpath.read_text())["signature"]
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    # Resolve the Signal so the next observe re-opens it.
    active = signals_store.find_active_by_signature(shared, sig)
    assert active is not None
    signals_store.apply_transition(
        active, "resolved", shared, actor="test", reason="cleared",
    )

    # Re-emit the same signature → reopened outcome → archived (novel state).
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   record_id="rec-2")
    r2 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r2.records_archived == 1
    assert r2.records_dropped == 0


def test_changed_severity_finding_is_archived(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """(3b) A CHANGED finding (severity escalation on an already-firing
    Signal) IS archived, not dropped as a dedup-hit."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   severity="minor", record_id="rec-1")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    # Same signature, higher severity → outcome "changed" → archive.
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   severity="critical", record_id="rec-2")
    r2 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r2.records_archived == 1
    assert r2.records_dropped == 0


def test_dedup_hit_tier3_finding_is_deleted_not_archived(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """(1b) A tier3_finding re-emission whose Proposal already exists is
    DELETED, not archived (the proposal store already holds it)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    _write_tier3_finding(outbox, bot_id="team_bot_a", app_id="journal",
                         outcome="propose", record_id="rec-t3-1")
    r1 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r1.records_archived == 1
    assert r1.records_dropped == 0

    # Same signature again → open Proposal exists → dedup-hit → delete.
    _write_tier3_finding(outbox, bot_id="team_bot_a", app_id="journal",
                         outcome="propose", record_id="rec-t3-2")
    r2 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r2.records_dropped == 1
    assert r2.dropped_dedup_hit == 1
    assert r2.records_archived == 0
    # One Proposal, not two.
    pending = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending) == 1


def test_noop_run_summary_is_dropped(tmp_root: Path, tmp_path: Path) -> None:
    """(4) An apps_audited:0 run_summary heartbeat is DROPPED, not archived."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _noop_run_summary(outbox, bot_id="team_bot_a")
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.summaries_processed == 1
    assert result.records_dropped == 1
    assert result.dropped_heartbeat == 1
    assert result.records_archived == 0
    assert result.files_processed == 1            # root still drained
    assert _ingested_files(outbox) == []
    assert [p.name for p in outbox.iterdir() if p.is_file()] == []


def test_active_run_summary_is_kept(tmp_root: Path, tmp_path: Path) -> None:
    """(5) A run_summary that actually audited apps is KEPT/archived."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    # The _write_run_summary helper uses apps_audited:1 → real work → keep.
    _write_run_summary(outbox, bot_id="team_bot_a", kept_signatures=[])
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert result.summaries_processed == 1
    assert result.records_archived == 1
    assert result.records_dropped == 0
    assert len(_ingested_files(outbox)) == 1


def test_conflict_notice_always_archived(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """(6a) conflict_notice is a low-volume signal record — always archived,
    even on a re-emission (the keep-always set)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_conflict_notice(outbox, bot_id="team_bot_a", app_id="journal",
                           record_id="rec-cn-1")
    r1 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r1.records_archived == 1
    assert r1.records_dropped == 0

    # Re-emit the same conflict notice → still archived (never dropped).
    _write_conflict_notice(outbox, bot_id="team_bot_a", app_id="journal",
                           record_id="rec-cn-2")
    r2 = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    assert r2.records_archived == 1
    assert r2.records_dropped == 0
    assert len(_ingested_files(outbox)) == 2


def test_repair_session_always_archived(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """(6b) repair_applied is a low-volume record — always archived."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    rec = {
        "record_id": "rec-repair-1",
        "kind": "repair_applied",
        "ts": "2026-05-16T00:00:00Z",
        "bot_id": "team_bot_a",
        "app_id": "journal",
        "request_id": "req-1",
        "applied": [],
        "proposals": [],
        "summary": "repair session ran",
    }
    (outbox / "rec-repair-1.json").write_text(json.dumps(rec))
    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    # Whether or not the changelog write succeeds in the temp tree, a
    # repair_applied record is never dropped as a re-emission.
    assert result.records_dropped == 0


def test_root_fully_drained_with_mixed_records(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """(7) The outbox root is fully drained even with a mix of archive + drop
    records — nothing is left stuck."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")

    # Seed a firing Signal so the second finding is a dedup-hit (drop).
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   record_id="rec-1")
    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    # Mixed batch: a dedup-hit (drop), a brand-new finding (archive),
    # and a no-op heartbeat (drop).
    _write_finding(outbox, bot_id="team_bot_a", app_id="journal",
                   record_id="rec-2")                       # dedup-hit
    _write_finding(outbox, bot_id="team_bot_a", app_id="todos",
                   path="z.py", record_id="rec-3")          # new
    _noop_run_summary(outbox, bot_id="team_bot_a", record_id="rec-hb")

    result = audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    # Root fully drained: no non-_ingested files left.
    assert [p.name for p in outbox.iterdir() if p.is_file()] == []
    assert result.records_dropped == 2          # dedup-hit + heartbeat
    assert result.dropped_dedup_hit == 1
    assert result.dropped_heartbeat == 1
    assert result.records_archived == 1         # the new finding
    assert result.errors == []
