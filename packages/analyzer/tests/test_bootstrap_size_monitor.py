"""Tests for bootstrap_size_monitor.

The monitor compares each bot's OC-ingested workspace bootstrap files against
its effective ``bootstrapMaxChars`` / ``bootstrapTotalMaxChars``. We unit-test
the pure cap/assessment helpers directly, and the orchestration (fire,
escalate, sweep-resolve on trim, the unreadable fail-safe) with real tmp-dir
bot homes and the REAL signal store writing to a tmp shared dir.

Bot ids here are role placeholders (docs/PLACEHOLDER_NAMING.md) — never real
accounts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import bootstrap_size_monitor as monitor  # noqa: E402

TEAM_BOT = "team_bot_b"
OTHER_BOT = "team_bot_a"


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_effective_limits_defaults_when_absent():
    assert monitor.effective_limits({}) == (
        monitor.OC_DEFAULT_BOOTSTRAP_MAX_CHARS,
        monitor.OC_DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS,
    )


def test_effective_limits_reads_explicit_values():
    cfg = {"agents": {"defaults": {
        "bootstrapMaxChars": 40_000, "bootstrapTotalMaxChars": 100_000}}}
    assert monitor.effective_limits(cfg) == (40_000, 100_000)


def test_effective_limits_explicit_null_disables_the_cap():
    """The unrestricted-debug cost profile writes bootstrapTotalMaxChars: null
    — present-but-null means DISABLED, never 'fall back to the OC default'
    (that would false-fire on a deliberately uncapped bot)."""
    cfg = {"agents": {"defaults": {"bootstrapTotalMaxChars": None}}}
    per_file, total = monitor.effective_limits(cfg)
    assert per_file == monitor.OC_DEFAULT_BOOTSTRAP_MAX_CHARS
    assert total is None


def test_effective_limits_zero_disables_the_cap():
    cfg = {"agents": {"defaults": {"bootstrapMaxChars": 0}}}
    assert monitor.effective_limits(cfg)[0] is None


def test_assess_under_warn_threshold_is_clean():
    findings = monitor.assess_bot({"AGENTS.md": 33_000}, 40_000, None)
    assert findings == []


def test_assess_warn_at_85_percent():
    findings = monitor.assess_bot({"AGENTS.md": 34_000}, 40_000, None)
    assert [(f.surface, f.level) for f in findings] == [("AGENTS.md", "warn")]


def test_assess_alert_at_and_past_100_percent():
    """The live incident shape: 128k chars against a 40k cap."""
    findings = monitor.assess_bot({"AGENTS.md": 128_000}, 40_000, None)
    assert [(f.surface, f.level) for f in findings] == [("AGENTS.md", "alert")]
    assert findings[0].ratio == pytest.approx(3.2)


def test_assess_total_sums_post_truncation_sizes():
    """The total line must sum what OC would actually inject — each file
    clamped to the per-file cap — so one oversized file doesn't double-fire
    the total budget."""
    sizes = {"AGENTS.md": 128_000, "MEMORY.md": 10_000}
    findings = monitor.assess_bot(sizes, 40_000, 60_000)
    by_surface = {f.surface: f for f in findings}
    assert by_surface["AGENTS.md"].level == "alert"
    # Injected total = min(128k, 40k) + 10k = 50k → 83% of 60k → clean.
    assert "<total>" not in by_surface

    findings = monitor.assess_bot(sizes, 40_000, 55_000)
    by_surface = {f.surface: f for f in findings}
    # 50k of 55k → 91% → warn on the total.
    assert by_surface["<total>"].level == "warn"
    assert by_surface["<total>"].chars == 50_000


def test_assess_disabled_caps_check_nothing():
    assert monitor.assess_bot({"AGENTS.md": 10**9}, None, None) == []


def test_truncation_signal_escalates_to_alert_only_when_truncating():
    warn_sig = monitor._truncation_signal(
        TEAM_BOT, [monitor.Finding("AGENTS.md", 35_000, 40_000, "warn")])
    assert warn_sig["severity"] == "warn"
    assert "about to truncate" in warn_sig["title"]

    alert_sig = monitor._truncation_signal(TEAM_BOT, [
        monitor.Finding("AGENTS.md", 128_000, 40_000, "alert"),
        monitor.Finding("MEMORY.md", 35_000, 40_000, "warn"),
    ])
    assert alert_sig["severity"] == "alert"
    assert "silently dead" in alert_sig["title"]
    # Signature keys on the bot, so both severities dedup into one Signal.
    assert alert_sig["signature"] == warn_sig["signature"]
    assert alert_sig["bot_id"] == TEAM_BOT
    assert len(alert_sig["title"]) <= 80


# ── Orchestration (real store, tmp homes) ─────────────────────────────────


def _write_bot(
    homes: Path,
    bot_id: str,
    *,
    files: dict[str, int],
    per_file_cap: "int | None" = 40_000,
) -> None:
    ws = homes / bot_id / ".openclaw" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    oc: dict = {"agents": {"defaults": {}}}
    if per_file_cap is not None:
        oc["agents"]["defaults"]["bootstrapMaxChars"] = per_file_cap
    (homes / bot_id / ".openclaw" / "openclaw.json").write_text(json.dumps(oc))
    for name, chars in files.items():
        (ws / name).write_text("x" * chars)


@pytest.fixture()
def pod(monkeypatch, tmp_path):
    """(network_path, shared_dir, homes_dir) with bot_home routed to tmp."""
    shared = tmp_path / "shared"
    shared.mkdir()
    homes = tmp_path / "homes"
    homes.mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(shared),
        "members": [TEAM_BOT, OTHER_BOT],
        "bots": {TEAM_BOT: {}, OTHER_BOT: {}},
    }))
    monkeypatch.setattr(monitor, "bot_home", lambda bot_id, cfg=None: homes / bot_id)
    return net, shared, homes


def _firing(shared):
    from signals import store as signals_store
    return list(signals_store.iter_signals(shared, subdirs=("firing",)))


def test_clean_pod_fires_nothing(pod):
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 20_000})
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    summary = monitor.run(net)

    assert summary["bots_scanned"] == 2
    assert summary["readable"] == 2
    assert summary["at_risk_surfaces"] == 0
    assert _firing(shared) == []


def test_missing_bootstrap_files_are_a_clean_skip(pod):
    """Not every bot carries every file in the fixed ingested set."""
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 1_000})
    _write_bot(homes, OTHER_BOT, files={})

    summary = monitor.run(net)

    assert summary["readable"] == 2
    assert _firing(shared) == []


def test_truncating_file_fires_alert(pod):
    """ACCEPTANCE — the 2026-08-01 incident shape: AGENTS.md far past the
    bot's 40k cap must fire an alert naming the file and both numbers."""
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 128_000})
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    summary = monitor.run(net)

    assert summary["at_risk_surfaces"] == 1
    fired = _firing(shared)
    assert len(fired) == 1
    sig = fired[0]
    assert sig.type == monitor.TRUNCATION_TYPE
    assert sig.severity == "alert"
    assert sig.bot_id == TEAM_BOT
    assert sig.signature == f"{monitor.PRODUCER}:{monitor.TRUNCATION_TYPE}:{TEAM_BOT}"
    assert sig.incident_key == f"{monitor.PRODUCER}:{TEAM_BOT}"
    assert sig.details["findings"][0]["surface"] == "AGENTS.md"
    assert sig.details["findings"][0]["chars"] == 128_000
    assert sig.details["findings"][0]["limit"] == 40_000


def test_near_limit_fires_warn_and_trim_sweeps_it(pod):
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 36_000})
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    monitor.run(net)
    fired = _firing(shared)
    assert len(fired) == 1
    assert fired[0].severity == "warn"

    # Operator trims the file back under 85% → next tick auto-resolves.
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 20_000})
    summary = monitor.run(net)
    assert summary["signals_resolved"] == 1
    assert _firing(shared) == []


def test_oc_default_cap_applies_when_config_has_no_override(pod):
    """A bot with no bootstrapMaxChars key rides OC's compiled 12k default."""
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 13_000}, per_file_cap=None)
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    monitor.run(net)

    fired = _firing(shared)
    assert len(fired) == 1
    assert fired[0].severity == "alert"
    assert fired[0].details["findings"][0]["limit"] == (
        monitor.OC_DEFAULT_BOOTSTRAP_MAX_CHARS)


def test_unreadable_config_fires_blind_signal_and_preserves_prior(pod, monkeypatch):
    """The fail-safe: a blind tick must fire the unreadable type AND leave the
    bot's prior truncation Signal untouched (unreadable ≠ clean)."""
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 128_000})
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    monitor.run(net)
    assert [s.type for s in _firing(shared)] == [monitor.TRUNCATION_TYPE]

    # The bot goes unreadable (ACL clamp): openclaw.json read now fails.
    real_reader = monitor._read_openclaw_config

    def _blind(bot_id, network):
        return None if bot_id == TEAM_BOT else real_reader(bot_id, network)

    monkeypatch.setattr(monitor, "_read_openclaw_config", _blind)
    summary = monitor.run(net)

    assert summary["unreadable"] == 1
    by_type = {s.type: s for s in _firing(shared)}
    # Prior truncation Signal persists — the blind bot was excluded from the
    # truncation sweep — and the blind spot itself is now a Signal.
    assert monitor.TRUNCATION_TYPE in by_type
    assert monitor.UNREADABLE_TYPE in by_type
    assert by_type[monitor.UNREADABLE_TYPE].bot_id == TEAM_BOT

    # Read restored → unreadable auto-resolves, truncation keeps firing.
    monkeypatch.setattr(monitor, "_read_openclaw_config", real_reader)
    monitor.run(net)
    assert [s.type for s in _firing(shared)] == [monitor.TRUNCATION_TYPE]


def test_unreadable_workspace_file_goes_blind_not_partial(pod, monkeypatch):
    """A single unreadable ingested file makes the whole bot blind — a partial
    assessment could miss the one oversized file and wrongly sweep-resolve."""
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 1_000, "MEMORY.md": 1_000})
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    real_read = monitor._read_chars

    def _flaky(path):
        if path.name == "MEMORY.md" and TEAM_BOT in str(path):
            return None  # unreadable, NOT missing
        return real_read(path)

    monkeypatch.setattr(monitor, "_read_chars", _flaky)
    summary = monitor.run(net)

    assert summary["unreadable"] == 1
    fired = _firing(shared)
    assert [s.type for s in fired] == [monitor.UNREADABLE_TYPE]
    assert "MEMORY.md" in fired[0].details["error"]


def test_dry_run_writes_nothing(pod, capsys):
    net, shared, homes = pod
    _write_bot(homes, TEAM_BOT, files={"AGENTS.md": 128_000})
    _write_bot(homes, OTHER_BOT, files={"AGENTS.md": 1_000})

    monitor.run(net, dry_run=True)

    assert _firing(shared) == []
    assert "would_observe" in capsys.readouterr().out


def test_missing_network_json_skips_cleanly(tmp_path, capsys):
    rc = monitor.main(["--network", str(tmp_path / "nope.json")])
    assert rc == 0
    assert "skipped" in capsys.readouterr().out
